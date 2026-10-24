"""EXP-04: refresh methods over a throttled link between two VMs.

Runs on pgd-dest, the machine that receives. It drives pgd-source over SSH
(one multiplexed connection, internal addresses) and, for every trial
(pattern, fraction, scale):

1. builds a fresh cluster on the source with EXP-01's own code and dataset;
2. takes B0, the stale old copy, on the destination with pg_basebackup over
   the network, unthrottled, and never writes to it again;
3. applies EXP-01's workload (same SQL generator and seed) and CHECKPOINTs;
4. for each repeat, visits the link speeds in a new seeded random order
   (--speed-order); at each, shapes both VMs' egress with tc tbf
   (infra/gcp/throttle.sh), measures the achieved rate in both directions
   with a timed bulk TCP stream, then runs every method in a new random
   order. Before each method's timed phases the OS page cache is dropped on
   both VMs, once.

Methods (each output is verified after the timer stops, then deleted):

    full           dest: pg_basebackup -h source into a new directory
    incremental    dest: pg_basebackup --incremental=B0/backup_manifest, then
                   pg_combinebackup --copy B0 + increment (capture and combine timed apart)
    pgdelta        at the same time, in one timed phase (receiver_capture): on
                   dest pgdelta receive --listen, until it listens (receiver;
                   it then reads B0 into the page cache while it waits,
                   --prefetch-basis on), and on source a local pg_basebackup S
                   (capture); then pgdelta send on source, until both ends exit
                   (transfer). EXP-06's engine is pinned: --jobs 4 on both ends,
                   and --rebuild copy-patch --verify pipelined on the receiver
    pgdelta_1thread  the same with EXP-02's single-threaded engine pinned:
                   --jobs 1, and --rebuild gap-fill --verify sequential, with the
                   same prefetch (optional; both need a pgdelta with these options)
    rsync_8k       at the same time, in one timed phase (prep_capture): on dest
    rsync_default  cp -a B0 O, then sync -f O (prep), and on source a local
                   pg_basebackup S (capture); then rsync --fsync from S to an
                   rsync daemon on dest, unencrypted, with and without
                   --block-size=8192 (transfer)

Optional methods for T3, T8a and T11 (named in --methods; see the README):

    pgdelta_gapfill_seq, pgdelta_seq, pgdelta_fsync_wb, pgdelta_fsync_worker,
    pgdelta_inplace
                   T3: staged as pgdelta, each with one write-path switch changed;
                   pgdelta_inplace refreshes B, a copy of B0 made before the cache
                   drop (untimed), with --in-place
    incremental_cfr
                   T3 (d): incremental, combined with pg_combinebackup --copy-file-range,
                   which clones B0's unchanged blocks on dest's XFS
    pgdelta_direct, pgdelta_direct_inplace
                   T8a: the source cluster is stopped cleanly before the cache drop
                   (untimed); one timed phase: receiver_start (dest), then at once pgdelta send
                   --source-kind datadir on the stopped data directory (transfer);
                   the source is restarted afterwards, untimed, whatever happened
    pgbackrest_zst, pgbackrest_none, pgbackrest_block
                   T11: pgbackrest restore --delta of B, a copy of B0 made before the
                   cache drop, from a repository on the source over SSH (restore); the
                   repositories, archiving and one full backup each are set up per
                   trial, untimed, and only when one of them is selected

The end-to-end total is every timed phase, rsync's prep included. The prep
and the capture run on different VMs with the link idle, so a real
cp-then-rsync refresh overlaps them: rsync's total is max(prep, capture) +
transfer. pgdelta's receiver start-up is its counterpart: its total is
max(receiver start, capture) + transfer. For both, the total with the two
parts one after the other is kept as an upper bound (totals_serial); for
rsync, its total without the prep is the in-place lower bound
(totals_in_place).

Measured per method run and phase: wall time, bytes on the wire per direction
from /proc/net/dev of the internal interface on both VMs, CPU seconds per VM
from /proc/stat and per process from rusage, each tool's own peak RSS from GNU
time, the WAL in each backup, and the bytes each tool reports. One JSON per
(trial, repeat, speed, method) under runs/, a summary.csv, trials/<trial>.json
and run.json with the environment.

See experiments/network/README.md.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, wait as wait_futures
from datetime import datetime, timezone
import fcntl
import itertools
import json
import os
from pathlib import Path
import random
import re
import resource
import shlex
import signal
import statistics
import subprocess
import sys
import threading
import time
import traceback

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.backup import atomic_json, basebackup_command, combine_command  # noqa: E402
from benchmarks.methods import RSYNC_OPTIONS, parse_pgdelta_stats, rsync_transfer_stats  # noqa: E402
from experiments.change_patterns import workloads  # noqa: E402
from experiments.network import measure  # noqa: E402
from experiments.network.agent import RESULT_PREFIX  # noqa: E402

SCHEMA_RUN = 'exp04-method-run/1'
SCHEMA_TRIAL = 'exp04-trial/1'
SCHEMA = 'exp04-run/1'
PG_PORT = 5432
PG_USER = 'study'  # experiments.change_patterns.run_study.USER
PG_DATABASE = 'postgres'  # run_study.DATABASE
# A verified output is started on dest on this port, with its own socket directory and no
# TCP listener (pgdelta_direct*, pgbackrest_*).
OUTPUT_PG_PORT = 5433
OUTPUT_START_TIMEOUT = 1800  # seconds; a pgBackRest copy replays the archive before it is up
OUTPUT_QUERY = 'SELECT count(*) FROM pgbench_branches;'
PGDELTA_PORT = 7000
BLAST_PORT = 7100
RSYNC_PORT = 8730
RSYNC_MODULE = 'exp04'
AGENT = 'experiments/network/agent.py'
SOURCE_CLUSTER = 'experiments/network/source_cluster.py'
DEFAULT_SPEEDS = '0.1,0.25,0.5,1,2,5,10,unthrottled'
# EXP-04's rsync writes O durably, as pg_basebackup, pg_combinebackup and
# pgdelta do; benchmarks/methods.py's local EXP-02 options are left as they are.
RSYNC_EXTRA_OPTIONS = ('--fsync',)
# The in-place methods' and pgBackRest's B never shares blocks with B0 (EXP-10: on XFS the copy
# uses --reflink=never). rsync's timed prep keeps cp's default, which clones on XFS: T3 (d)
# measures that clone, and each output's shared extents are recorded in its verification.
BASIS_COPY_REFLINK = '--reflink=never'
# /bin/true under GNU time must report well under this, or the peak-RSS
# figures would not be the tools' own (see measure.GNU_TIME_FORMAT).
RSS_PROBE_LIMIT_KB = 16 * 1024
# Child(stamp_lines=True): how long, after the command exited, to wait for its last lines.
STAMP_READER_TIMEOUT = 30
FULL_BYTES_SCOPE = ('pg_basebackup reports no byte counts; see output_minus_wal_padding_bytes '
                    '(output_bytes includes the zero-padded WAL segment, which is not sent)')
CACHE_POLICY = ("sync; echo 3 > /proc/sys/vm/drop_caches on both VMs, once, before every "
                "method's timed phases. rsync's prep (cp -a B0 O; sync -f O on dest) runs at "
                'the same time as its capture of S (on source) after that one drop, and O '
                'stays in the page cache as cp left it, as in a real cp-then-rsync refresh. '
                "pgdelta's receiver starts at the same time as its capture after that one drop, "
                'and reads B0 into the page cache while it waits for the sender '
                '(--prefetch-basis on), as a real refresh would. The in-place methods and '
                'pgBackRest patch B, a copy of B0 made (cp -a --reflink=never; sync -f) before the '
                'drop, so B starts cold and shares no block with B0; pgdelta_direct* stops the '
                "source cluster cleanly before the drop, so the sender reads the source's data "
                'directory cold')
os.environ['PATH'] = os.environ.get('PATH', '/usr/bin:/bin') + ':/usr/sbin:/sbin'


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    print(f'[{datetime.now(timezone.utc).strftime("%H:%M:%S")}] {message}', file=sys.stderr,
          flush=True)


def save_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, value)


def agent_result(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_PREFIX):
            return json.loads(line[len(RESULT_PREFIX):])
    raise ValueError('no result line in agent output')


class CommandError(RuntimeError):
    pass


class Source:
    """pgd-source over one multiplexed SSH connection."""

    def __init__(self, ip: str, user: str, key: str, repo: str, python: str = 'python3'):
        self.ip, self.repo, self.python = ip, repo, python
        self.control = f'/tmp/exp04-ssh-{os.getpid()}'
        self.options = ['-i', key, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new',
                        '-o', f'ControlPath={self.control}', '-o', 'ServerAliveInterval=15',
                        '-o', 'ServerAliveCountMax=8']
        self.target = f'{user}@{ip}'

    def open(self) -> None:
        # Start the master explicitly: a ControlPersist master forked from a
        # captured command would keep its pipes open.
        subprocess.run(['ssh', '-M', '-N', '-f', '-o', 'ControlPersist=yes', *self.options,
                        self.target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True, timeout=60)

    def close(self) -> None:
        subprocess.run(['ssh', '-O', 'exit', *self.options, self.target],
                       capture_output=True, timeout=30)

    def argv(self, command) -> list[str]:
        return ['ssh', '-o', 'ControlMaster=no', *self.options, self.target,
                shlex.join(str(part) for part in command)]

    def agent_argv(self, *args) -> list[str]:
        return self.argv([self.python, f'{self.repo}/{AGENT}', *args])

    def agent(self, *args, timeout: float = 600) -> dict:
        result = subprocess.run(self.agent_argv(*args), capture_output=True, text=True,
                                timeout=timeout, stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            raise CommandError(f'source agent {args[0]} failed ({result.returncode}): '
                               f'{result.stderr.strip()[-2000:]}')
        return agent_result(result.stdout)

    def script(self, script: str, *args, timeout: float = 7200) -> dict:
        result = subprocess.run(self.argv([self.python, f'{self.repo}/{script}', *args]),
                                capture_output=True, text=True, timeout=timeout,
                                stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            raise CommandError(f'{script} {args[0]} failed ({result.returncode}): '
                               f'{result.stderr.strip()[-3000:]}')
        return agent_result(result.stdout)

    def write(self, path: str, text: str) -> None:
        """Write a small text file on the source (its directory is created)."""
        script = 'mkdir -p "$(dirname "$1")" && cat > "$1"'
        result = subprocess.run(self.argv(['sh', '-c', script, 'sh', path]), input=text,
                                capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise CommandError(f'writing {path} on the source failed ({result.returncode}): '
                               f'{result.stderr.strip()[-2000:]}')


def group_members(pgid: int, proc: Path = Path('/proc')) -> list[int] | None:
    """PIDs in process group pgid, from /proc/*/stat; None without a /proc."""
    if not (proc / 'self').exists():
        return None
    members = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / 'stat').read_text()
        except OSError:
            continue
        # pid (comm) state ppid pgrp ...; comm may contain spaces and parentheses.
        fields = text[text.rindex(')') + 2:].split()
        if len(fields) > 2 and int(fields[2]) == pgid:
            members.append(int(entry.name))
    return members


def server_env(**extra) -> dict:
    """This environment without libpq's PG* settings, with LC_ALL=C (as run_study's clusters run)."""
    env = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
    return {**env, 'LC_ALL': 'C', **extra}


def pgdelta_supports(help_text: str, options=('--jobs',)) -> bool:
    """Whether `pgdelta <command> --help` output lists every one of `options`."""
    return all(re.search(rf'(^|\s){re.escape(option)}\b', help_text) is not None for option in options)


class Child:
    """A local process whose exit is observed with wait4, for its rusage.

    With gnu_time, the command runs under GNU time, which reports the
    command's own peak RSS (wait4's ru_maxrss would be this Python process's
    high-water mark, inherited across fork). Signals other than SIGKILL then
    go to the command's process group except GNU time itself, so GNU time
    outlives the command and writes its report.

    With stamp_lines, the command's output (stdout and stderr) comes through a
    pipe that a thread of this process reads, writing each line to the log as
    it arrives, prefixed with the seconds since the launch on this clock
    (measure.stamp_line). No other process runs for it, inside the clock or out.
    """

    def __init__(self, argv, log_path: Path, *, cwd=None, env=None, gnu_time=None, stamp_lines=False):
        self.argv = [str(part) for part in argv]
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.time_path = Path(f'{self.log_path}.time') if gnu_time else None
        launch = self.argv
        if self.time_path is not None:
            self.time_path.unlink(missing_ok=True)
            launch = measure.gnu_time_argv(gnu_time, self.time_path, self.argv)
        self.launcher = launch[:5] if gnu_time else None
        self.stream = open(self.log_path, 'wb')
        self.reader = None
        self.started_utc = time.time()
        self.started = time.perf_counter()
        self.process = subprocess.Popen(launch, stdout=subprocess.PIPE if stamp_lines else self.stream,
                                        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=cwd, env=env,
                                        start_new_session=True)
        if stamp_lines:
            self.reader = threading.Thread(target=self._copy_stamped, name=f'stamp-{self.process.pid}',
                                           daemon=True)
            self.reader.start()
        self.result = None
        self.timed_out = False

    def _copy_stamped(self) -> None:
        """The reader thread: each output line to the log as it arrives, with when it arrived."""
        pipe = self.process.stdout
        try:
            for line in iter(pipe.readline, b''):
                self.stream.write(measure.stamp_line(time.perf_counter() - self.started, line))
                self.stream.flush()
        except ValueError:  # the log was closed after STAMP_READER_TIMEOUT
            pass
        finally:
            pipe.close()

    def _finish(self, status, usage) -> dict:
        wall = time.perf_counter() - self.started
        self.process.returncode = os.waitstatus_to_exitcode(status)
        if self.reader is not None:
            # The pipe ends once every process holding it has exited; a straggler that keeps it
            # open (a daemonised grandchild) costs this wait at most, after the clock.
            self.reader.join(STAMP_READER_TIMEOUT)
        self.stream.close()
        timing = measure.read_gnu_time(self.time_path) if self.time_path else None
        self.result = {'host': 'dest', 'argv': self.argv, 'exit_code': self.process.returncode,
                       'wall_seconds': round(wall, 6), 'started_utc': self.started_utc,
                       'rusage': measure.rusage_record(usage),
                       'peak_rss_kb': timing['maxrss_kb'] if timing else None,
                       'gnu_time': timing, 'launcher': self.launcher,
                       'log': str(self.log_path), 'timed_out': self.timed_out,
                       'stamped_lines': self.reader is not None}
        return self.result

    def poll(self):
        if self.result is None:
            pid, status, usage = os.wait4(self.process.pid, os.WNOHANG)
            if pid:
                self._finish(status, usage)
        return self.result

    def signal(self, signum=signal.SIGTERM) -> None:
        if self.result is not None:
            return
        if self.time_path is not None and signum != signal.SIGKILL:
            members = group_members(self.process.pid)
            if members is not None:
                for pid in members:
                    if pid != self.process.pid:
                        try:
                            os.kill(pid, signum)
                        except ProcessLookupError:
                            pass
                return
        try:
            os.killpg(self.process.pid, signum)
        except ProcessLookupError:
            pass

    def _expire(self) -> None:
        self.timed_out = True
        self.signal(signal.SIGTERM)
        time.sleep(10)
        self.signal(signal.SIGKILL)

    def wait(self, timeout: float) -> dict:
        if self.result is not None:
            return self.result
        timer = threading.Timer(timeout, self._expire)
        timer.daemon = True
        timer.start()
        try:
            _, status, usage = os.wait4(self.process.pid, 0)
        except BaseException:
            # Interrupted (SIGTERM to the orchestrator): stop the child, which runs in
            # its own session and would not see the signal, before cleanup removes
            # the directories it writes.
            self.stop()
            raise
        finally:
            timer.cancel()
        return self._finish(status, usage)

    def stop(self) -> None:
        self.signal(signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.poll() is not None:
                return
            time.sleep(0.1)
        self.signal(signal.SIGKILL)
        try:
            _, status, usage = os.wait4(self.process.pid, 0)
            self._finish(status, usage)
        except ChildProcessError:
            pass

    def log_text(self) -> str:
        try:
            return self.log_path.read_text(errors='replace')
        except OSError:
            return ''


def tree_bytes(root: Path) -> int:
    total = 0
    for directory, _, filenames in os.walk(root):
        for name in filenames:
            path = Path(directory) / name
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
    return total


def settle(future) -> None:
    """Wait until `future` has finished, through any further KeyboardInterrupt meanwhile."""
    while True:
        try:
            wait_futures([future])
            return
        except BaseException:  # another SIGTERM or SIGINT: the call is still running, keep waiting
            continue


def failed(commands) -> list[str]:
    return [f"{command['host']}: {Path(command['argv'][0]).name} exited "
            f"{command['exit_code']}" + (' (timed out)' if command.get('timed_out') else '')
            for command in commands if command['exit_code'] != 0]


class Orchestrator:
    def __init__(self, args):
        self.args = args
        self.work = Path(args.work_dir)
        self.out = Path(args.out)
        self.bin = Path(args.bin_dir)
        self.repo = str(REPO_ROOT)
        self.remote_repo = args.remote_repo or self.repo
        self.source = Source(args.source_ip, args.ssh_user, args.ssh_key, self.remote_repo)
        self.rng = random.Random(args.order_seed)
        # Its own stream, so the speed orders do not shift the method orders.
        self.speed_rng = random.Random(f'exp04-speed-order-{args.order_seed}')
        self.gnu_time = None if args.gnu_time in ('', 'none') else args.gnu_time
        self.records: list[dict] = []
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.current_speed = None
        self.run: dict = {}
        self.trial_id = None
        self.log_prefix = ''  # r<repeat>-<speed>- while a method runs
        self.methods: list[str] = []
        self.method_speeds: dict[str, list[str]] = {}  # --method-speeds: method -> speed labels
        self.pgbackrest: dict | None = None  # this trial's pgBackRest set-up and backups
        self.source_down = None  # why the source cluster could not be restarted, if it couldn't
        # An output started on dest for its verification that pg_ctl could not stop: its data
        # directory is never removed, and no other output is started while it holds the port.
        self.output_stuck: dict | None = None

    # --- paths -------------------------------------------------------------

    @property
    def b0(self) -> Path:
        return self.work / 'B0'

    @property
    def outputs(self) -> Path:
        return self.work / 'out'

    @property
    def source_root(self) -> str:
        return f'{self.work}/src'

    @property
    def source_capture(self) -> str:
        return f'{self.work}/S'

    @property
    def source_data(self) -> str:
        """The source cluster's data directory, which pgdelta_direct* reads once it is stopped."""
        return f'{self.source_root}/data'

    @property
    def output_socket(self) -> Path:
        """The socket directory of an output started on dest for its verification."""
        return self.work / 'vs'

    @property
    def pgbackrest_exe(self) -> str:
        return self.args.pgbackrest

    def dest_log(self, name: str) -> Path:
        return self.out / 'logs' / self.trial_id / 'dest' / (self.log_prefix + name)

    def source_log(self, name: str) -> str:
        return f'{self.work}/logs/{self.trial_id}/{self.log_prefix}{name}'

    # --- primitives --------------------------------------------------------

    def local_agent(self, *args, timeout: float = 600) -> dict:
        result = subprocess.run([sys.executable, str(REPO_ROOT / AGENT), *args],
                                capture_output=True, text=True, timeout=timeout,
                                stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            raise CommandError(f'dest agent {args[0]} failed ({result.returncode}): '
                               f'{result.stderr.strip()[-2000:]}')
        return agent_result(result.stdout)

    def both(self, local, remote):
        """Run a local and a source-side call at the same time; return (source, dest).

        Whatever ends this call early (the local call failing, or an interrupt
        such as SIGTERM's KeyboardInterrupt while the local call runs or while
        the source-side call is awaited), the source-side call is waited for
        before the exception propagates, so cleanup never races a command
        that is still writing. Further interrupts during that wait don't cut
        it short; the first exception is the one raised.
        """
        future = self.pool.submit(remote)
        try:
            dest = local()
            source = future.result()
        except BaseException:
            settle(future)
            raise
        return source, dest

    def concurrently(self, local, remote):
        """both(), for the two parts of a concurrent timed phase (tests replace it)."""
        return self.both(local, remote)

    def snapshot_source(self) -> dict:
        return self.source.agent('snapshot', '--iface', self.source_iface, timeout=60)

    def snapshot_dest(self) -> dict:
        return measure.snapshot(self.dest_iface)

    def local_command(self, argv, log_name: str, *, timeout: float | None = None,
                      cwd=None, env=None, stamp_lines: bool = False) -> dict:
        child = Child(argv, self.dest_log(log_name), cwd=cwd, env=env, gnu_time=self.gnu_time,
                      stamp_lines=stamp_lines)
        return child.wait(timeout or self.args.command_timeout)

    def remote_command(self, argv, log_name: str, *, cwd: str | None = None) -> dict:
        argv = [str(part) for part in argv]
        extra = ['--cwd', cwd] if cwd else []
        if self.gnu_time:
            extra += ['--gnu-time', self.gnu_time]
        started_utc = time.time()
        started = time.perf_counter()
        try:
            result = self.source.agent('run', '--log', self.source_log(log_name), *extra, '--',
                                       *argv, timeout=self.args.command_timeout)
        except subprocess.TimeoutExpired:
            return {'host': 'source', 'argv': argv, 'exit_code': None, 'timed_out': True,
                    'wall_seconds': round(time.perf_counter() - started, 6),
                    'started_utc': started_utc, 'rusage': None, 'log': self.source_log(log_name)}
        wall = time.perf_counter() - started
        return {'host': 'source', 'argv': argv, 'exit_code': result['exit_code'],
                'wall_seconds': round(wall, 6), 'inner_wall_seconds': result['wall_seconds'],
                'started_utc': started_utc, 'rusage': result['rusage'],
                'peak_rss_kb': result.get('peak_rss_kb'), 'gnu_time': result.get('gnu_time'),
                'launcher': result.get('launcher'), 'log': result['log'],
                'log_tail': result['log_tail'][-8000:], 'timed_out': False}

    def timed_phase(self, name: str, body) -> dict:
        """Counters on both VMs around body(); body returns its command records."""
        before_source = self.snapshot_source()
        before_dest = self.snapshot_dest()
        started_utc = time.time()
        started = time.perf_counter()
        commands = body()
        wall = time.perf_counter() - started
        ended_utc = time.time()
        after_dest = self.snapshot_dest()
        after_source = self.snapshot_source()
        source = measure.snapshot_delta(before_source, after_source)
        dest = measure.snapshot_delta(before_dest, after_dest)
        return {'name': name, 'wall_seconds': round(wall, 3), 'started_utc': started_utc,
                'ended_utc': ended_utc, 'commands': commands,
                'net': {'source': source['net'], 'dest': dest['net']},
                'cpu': {'source': source['cpu'], 'dest': dest['cpu']},
                'counter_window_seconds': {'source': source['interval_seconds'],
                                           'dest': dest['interval_seconds']},
                'failures': failed(commands)}

    def concurrent_phase(self, name: str, dest_part: tuple, source_part: tuple) -> dict:
        """One timed phase whose dest part and source part run at the same time.

        dest_part and source_part are (part name, body); each body runs
        commands on its own VM only and returns their records, which are
        tagged with the part's name. The phase's wall is the pair's, about the
        longer part's; each part's own wall and start offset are kept in
        parts. The counters are read once around the pair, so each VM's CPU is
        that of the part that ran there (plus the other part's SSH client on
        dest), and no part adds a counter window of its own.
        """
        parts = {}

        def run_part(part, host, body, origin):
            started_utc = time.time()
            started = time.perf_counter()
            commands = body()
            for command in commands:
                command['part'] = part
            parts[part] = {'host': host, 'started_utc': started_utc,
                           'offset_seconds': round(started - origin, 3),
                           'wall_seconds': round(time.perf_counter() - started, 3)}
            return commands

        def body():
            origin = time.perf_counter()
            source, dest = self.concurrently(
                lambda: run_part(dest_part[0], 'dest', dest_part[1], origin),
                lambda: run_part(source_part[0], 'source', source_part[1], origin))
            return dest + source

        phase = self.timed_phase(name, body)
        phase['parts'] = {part: parts[part] for part in (dest_part[0], source_part[0])}
        return phase

    def drop_caches(self) -> dict:
        source, dest = self.both(lambda: self.local_agent('drop-caches'),
                                 lambda: self.source.agent('drop-caches'))
        return {'source': source, 'dest': dest}

    def remove_dest(self, *paths) -> None:
        self.local_agent('remove', '--under', str(self.work), *map(str, paths))

    def remove_source(self, *paths) -> None:
        self.source.agent('remove', '--under', str(self.work), *map(str, paths))

    # --- link --------------------------------------------------------------

    def throttle(self, rate: str) -> dict:
        source, dest = self.both(
            lambda: self.local_agent('throttle', rate, '--iface', self.dest_iface),
            lambda: self.source.agent('throttle', rate, '--iface', self.source_iface))
        return {'source': source, 'dest': dest}

    def blast(self, direction: str) -> dict:
        seconds = str(self.args.link_test_seconds)
        if direction == 'source_to_dest':
            receiver = subprocess.Popen(
                [sys.executable, str(REPO_ROOT / AGENT), 'blast-recv', '--listen',
                 f'{self.dest_ip}:{BLAST_PORT}', '--timeout', '120'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            sender = lambda: self.source.agent('blast-send', '--connect',  # noqa: E731
                                               f'{self.dest_ip}:{BLAST_PORT}',
                                               '--seconds', seconds, timeout=300)
        else:
            receiver = subprocess.Popen(
                self.source.agent_argv('blast-recv', '--listen', f'{self.source.ip}:{BLAST_PORT}',
                                       '--timeout', '120'),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                stdin=subprocess.DEVNULL)
            sender = lambda: self.local_agent('blast-send', '--connect',  # noqa: E731
                                              f'{self.source.ip}:{BLAST_PORT}',
                                              '--seconds', seconds, timeout=300)
        try:
            ready = receiver.stdout.readline()
            if 'LISTENING' not in ready:
                raise CommandError(f'link test receiver did not start: {receiver.stderr.read()}')
            sent = sender()
            stdout, stderr = receiver.communicate(timeout=300)
        finally:
            if receiver.poll() is None:
                receiver.kill()
                receiver.wait()
        if receiver.returncode != 0:
            raise CommandError(f'link test receiver failed: {stderr.strip()[-2000:]}')
        return {'sender': sent, 'receiver': agent_result(stdout)}

    def set_speed(self, speed: dict) -> dict:
        log(f'link {speed["label"]}: tc {speed["tc_rate"]} on both VMs')
        tc = self.throttle(speed['tc_rate'])
        tests = {direction: self.blast(direction)
                 for direction in ('source_to_dest', 'dest_to_source')}
        link = {'speed': speed, 'tc': tc, 'tests': tests, 'utc': utc_now(),
                'source_to_dest_gbit': tests['source_to_dest']['receiver']['gbit'],
                'dest_to_source_gbit': tests['dest_to_source']['receiver']['gbit'],
                'source_to_dest_steady_gbit': tests['source_to_dest']['receiver'].get(
                    'steady_gbit'),
                'dest_to_source_steady_gbit': tests['dest_to_source']['receiver'].get(
                    'steady_gbit')}
        log(f'link {speed["label"]}: measured {link["source_to_dest_gbit"]} Gbit/s source->dest, '
            f'{link["dest_to_source_gbit"]} Gbit/s dest->source')
        self.current_speed = speed
        return link

    def qdisc_stats(self) -> dict:
        source, dest = self.both(
            lambda: self.local_agent('throttle', 'show', '--iface', self.dest_iface),
            lambda: self.source.agent('throttle', 'show', '--iface', self.source_iface))
        return {'source': source['qdisc'], 'dest': dest['qdisc']}

    # --- methods -----------------------------------------------------------

    def basebackup(self, output, *, host, basis=None) -> list[str]:
        return basebackup_command(self.bin / 'pg_basebackup', output, host=host, port=PG_PORT,
                                  user=PG_USER, basis=basis, checksum=self.args.manifest_checksums)

    def capture_commands(self, method: str) -> list[dict]:
        """Local pg_basebackup S on the source (the staged methods' capture).

        The log is named after the method, so the staged methods at one
        (repeat, speed) keep their own capture logs and GNU time reports.
        """
        argv = self.basebackup(self.source_capture, host=f'{self.source_root}/s')
        return [self.remote_command(argv, f'{method}-capture.log')]

    def method_paths(self, method: str) -> dict:
        # For the in-place methods and pgBackRest, 'output' is B: the copy of B0 they patch.
        paths = {'output': self.outputs / method}
        if method in measure.INCREMENTAL_METHODS:
            paths['increment'] = self.outputs / f'{method}-increment'
        if method in measure.STAGED_METHODS:
            paths['capture'] = self.source_capture
        if method in measure.PGDELTA_DIRECT_METHODS:
            paths['source_data'] = self.source_data  # read in place; never removed
        if method in measure.PGDELTA_METHODS:
            # In place, the receipt is beside B, which is this same path.
            paths['receipt'] = self.outputs / f'{method}.pgdelta-receipt.json'
            paths['stats'] = f'{self.work}/stats/{method}-send.json'
        return paths

    def clean_method(self, paths: dict) -> None:
        stuck = (self.output_stuck or {}).get('data')
        self.remove_dest(*[paths[key] for key in ('output', 'increment', 'receipt')
                           if key in paths and str(paths[key]) != stuck])
        remote = [paths[key] for key in ('capture', 'stats') if key in paths]
        if remote:
            self.remove_source(*remote)

    def run_full(self, paths) -> dict:
        argv = self.basebackup(paths['output'], host=self.source.ip)
        return {'transfer': self.timed_phase(
            'transfer', lambda: [self.local_command(argv, 'full.log')])}

    def run_incremental(self, method, paths) -> dict:
        phases = {}
        argv = self.basebackup(paths['increment'], host=self.source.ip, basis=self.b0)
        phases['capture'] = self.timed_phase(
            'capture', lambda: [self.local_command(argv, f'{method}-capture.log')])
        if phases['capture']['failures']:
            return phases
        argv = self.combine_argv(method, paths)
        phases['combine'] = self.timed_phase(
            'combine', lambda: [self.local_command(argv, f'{method}-combine.log')])
        return phases

    def combine_argv(self, method, paths) -> list[str]:
        """pg_combinebackup B0 + increment into O: --copy, or --copy-file-range for incremental_cfr."""
        return combine_command(self.bin / 'pg_combinebackup', self.b0, paths['increment'], paths['output'],
                               checksum=self.args.manifest_checksums, mode=measure.COMBINE_MODES[method])

    def pgdelta_receive_argv(self, method, paths) -> list:
        if method in measure.IN_PLACE_METHODS:
            # B, the copy of B0 made before the cache drop, refreshed itself (--in-place, among
            # the engine's flags): no --output.
            target = ['--basis', paths['output']]
        else:
            target = ['--basis', self.b0, '--output', paths['output']]
        return [sys.executable, '-m', 'pgdelta', 'receive', *target,
                '--listen', f'{self.dest_ip}:{PGDELTA_PORT}',
                '--pg-controldata', self.bin / 'pg_controldata', *measure.PGDELTA_ARGS[method]['receive']]

    def pgdelta_send_argv(self, method, paths) -> list:
        # pgdelta_direct* reads the stopped source's data directory (--source-kind datadir,
        # among the engine's flags); the staged methods read the capture S.
        source = self.source_data if method in measure.PGDELTA_DIRECT_METHODS else self.source_capture
        return [self.source.python, '-m', 'pgdelta', 'send', '--source', source,
                '--connect', f'{self.dest_ip}:{PGDELTA_PORT}',
                '--pg-controldata', self.bin / 'pg_controldata', '--stats-json', paths['stats'],
                *measure.PGDELTA_ARGS[method]['send']]

    def start_receiver(self, method, paths, holder: dict) -> list[dict]:
        """pgdelta's receiver part: launch `pgdelta receive --listen` and wait until it listens.

        The receiver keeps running after this part, reading B0 into the page
        cache (--prefetch-basis on) until the sender connects in the transfer
        phase; holder['receiver'] is its Child. Returns no command records when
        it listens (its record, with its rusage, goes into the transfer phase
        once it exits), or its final record if it exited or never listened.
        """
        receiver = Child(self.pgdelta_receive_argv(method, paths),
                         self.dest_log(f'{method}-receive.log'), cwd=self.repo,
                         gnu_time=self.gnu_time)
        holder['receiver'] = receiver
        deadline = time.monotonic() + 600
        while 'listening on' not in receiver.log_text():
            if receiver.poll() is not None or time.monotonic() > deadline:
                receiver.signal()
                return [receiver.wait(60)]
            time.sleep(0.005)
        return []

    def run_pgdelta(self, method, paths) -> dict:
        # The receiver (dest) and the capture of S (source) share no machine and the
        # link is idle during both, so they start together, as rsync's prep and capture
        # do: the receiver's start-up is on the critical path only where it is longer
        # than the capture, and it reads B0 into the page cache while it waits.
        holder: dict = {}
        try:
            phase = self.concurrent_phase(
                measure.RECEIVER_PHASE,
                ('receiver', lambda: self.start_receiver(method, paths, holder)),
                ('capture', lambda: self.capture_commands(method)))
            phases = {measure.RECEIVER_PHASE: phase}
            receiver = holder['receiver']
            # The part ends when the receiver's log says "listening on"; it keeps running.
            phase['parts']['receiver'].update(argv=receiver.argv, log=str(receiver.log_path))
            if phase['failures']:
                if receiver.result is None:  # the capture failed: stop the waiting receiver, untimed
                    receiver.stop()
                    phase['commands'].append({**receiver.result, 'part': 'receiver',
                                              'role': 'stopped after the capture failed (untimed)'})
                return phases
            phases['transfer'] = self.timed_phase(
                'transfer', lambda: self.pgdelta_transfer(method, paths, receiver, measure.RECEIVER_PHASE))
            return phases
        except BaseException:
            # Never leave a receiver holding the port for the next run.
            receiver = holder.get('receiver')
            if receiver is not None and receiver.result is None:
                receiver.stop()
            raise

    def pgdelta_transfer(self, method, paths, receiver: Child, launched_in: str) -> list[dict]:
        """pgdelta's transfer: the sender on the source, until both ends have exited."""
        sender = self.remote_command(self.pgdelta_send_argv(method, paths),
                                     f'{method}-send.log', cwd=self.remote_repo)
        if sender['exit_code'] != 0:
            receiver.signal()
        result = receiver.wait(self.args.command_timeout)
        return [{**result, 'role': f'receiver, launched in {launched_in}: its wall '
                                   'and rusage cover its whole life'}, sender]

    def run_pgdelta_direct(self, method, paths) -> dict:
        """T8a: no capture. One timed phase: the receiver starts, and the transfer follows at once.

        The source cluster was stopped cleanly before the cache drop (prepare), so the sender
        reads its data directory directly and cold (--source-kind datadir). With no capture to
        overlap, the receiver's start and the transfer are one span on one counter window: the
        sender starts as soon as the receiver listens, with no untimed gap in which the receiver
        would prefetch B0 off the clock. Each step's wall and start offset are in `steps`.
        """
        holder: dict = {}
        steps: dict = {}

        def body():
            origin = time.perf_counter()
            commands = self.start_receiver(method, paths, holder)
            steps[measure.DIRECT_RECEIVER_STEP] = {'host': 'dest', 'offset_seconds': 0.0,
                                                   'wall_seconds': round(time.perf_counter() - origin, 3)}
            if commands:  # the receiver exited, or never listened: no transfer
                return commands
            started = time.perf_counter()
            commands = self.pgdelta_transfer(method, paths, holder['receiver'], measure.DIRECT_PHASE)
            steps['transfer'] = {'host': 'source', 'offset_seconds': round(started - origin, 3),
                                 'wall_seconds': round(time.perf_counter() - started, 3)}
            return commands

        try:
            phase = self.timed_phase(measure.DIRECT_PHASE, body)
            phase['steps'] = steps
            receiver = holder['receiver']
            phase['receiver'] = {'argv': receiver.argv, 'log': str(receiver.log_path)}
            return {measure.DIRECT_PHASE: phase}
        except BaseException:
            receiver = holder.get('receiver')
            if receiver is not None and receiver.result is None:
                receiver.stop()
            raise

    def run_pgbackrest(self, method, paths) -> dict:
        """T11: pgbackrest restore --delta of B (a copy of B0) on dest, from the source's repository.

        The repository's full backup was taken once per trial, untimed (counted as already
        paid); the restore reaches it over SSH (repo1-host) with 4 processes.
        """
        argv = measure.pgbackrest_restore_argv(self.pgbackrest_exe, self.pgbackrest_paths['config'],
                                               method, paths['output'])
        return {measure.RESTORE_PHASE: self.timed_phase(
            measure.RESTORE_PHASE, lambda: [self.local_command(argv, f'{method}-restore.log')])}

    def rsync_config(self, method: str) -> Path:
        # One configuration and one daemon log per method, so the second rsync
        # method at a speed does not overwrite the first one's.
        conf = self.work / f'rsyncd-{method}.conf'
        conf.write_text('\n'.join([
            '# EXP-04 rsync daemon on pgd-dest (unencrypted, like pg_basebackup and pgdelta)',
            'use chroot = no', 'munge symlinks = no', 'read only = no', 'list = no',
            f'log file = {self.dest_log(f"{method}-rsyncd-transfers.log")}', '',
            f'[{RSYNC_MODULE}]', f'path = {self.outputs}', f'hosts allow = {self.source.ip}',
            '']))
        return conf

    def start_rsyncd(self, method: str) -> Child:
        log = self.dest_log(f'{method}-rsyncd.log')
        log.parent.mkdir(parents=True, exist_ok=True)
        conf = self.rsync_config(method)
        daemon = Child(['rsync', '--daemon', '--no-detach', f'--config={conf}',
                        f'--port={RSYNC_PORT}', f'--address={self.dest_ip}'],
                       log, gnu_time=self.gnu_time)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            listening = subprocess.run(['ss', '-Hltn', f'sport = :{RSYNC_PORT}'],
                                       capture_output=True, text=True).stdout
            if listening.strip():
                return daemon
            if daemon.poll() is not None:
                break
            time.sleep(0.05)
        daemon.signal()
        daemon.wait(30)
        raise CommandError(f'rsync daemon did not start: {daemon.log_text()[-2000:]}')

    def stop_rsyncd(self, daemon: Child) -> dict:
        daemon.signal(signal.SIGTERM)
        result = daemon.wait(60)
        return {**result, 'role': 'rsync daemon (its rusage CPU includes the reaped '
                                  'per-connection generator and receiver; its peak RSS is '
                                  'the largest single one of the three, not their sum)'}

    def rsync_argv(self, method: str) -> list[str]:
        options = [option for option in RSYNC_OPTIONS
                   if method == 'rsync_8k' or not option.startswith('--block-size')]
        return ['rsync', *options, *RSYNC_EXTRA_OPTIONS, f'{self.source_capture}/',
                f'rsync://{self.dest_ip}:{RSYNC_PORT}/{RSYNC_MODULE}/{method}/']

    def run_rsync(self, method, paths, daemon_holder: dict) -> dict:
        # The copy of B0 (dest) and the capture of S (source) share no machine and
        # the link is idle during both, so they run at the same time, as a real
        # cp-then-rsync refresh would: the copy is on the critical path only where
        # it is longer than the capture.
        phases = {measure.CONCURRENT_PHASE: self.concurrent_phase(
            measure.CONCURRENT_PHASE, ('prep', lambda: self.prep_commands(method, paths)),
            ('capture', lambda: self.capture_commands(method)))}
        if phases[measure.CONCURRENT_PHASE]['failures']:
            return phases
        argv = self.rsync_argv(method)
        phases['transfer'] = self.timed_phase(
            'transfer', lambda: [self.remote_command(argv, f'{method}.log')])
        daemon = daemon_holder.pop('daemon', None)
        if daemon is not None:
            # Outside the timer; only adds the daemon's rusage to the phase.
            phases['transfer']['commands'].append(self.stop_rsyncd(daemon))
        return phases

    def prep_commands(self, method, paths) -> list[dict]:
        """rsync's copy of B0 (ext4 has no instant clone), made durable inside the timer.

        pgdelta, pg_basebackup and pg_combinebackup each write a complete O and
        fsync it inside their timers; rsync patches a copy, so the copy and its
        writeback (sync -f: syncfs of O's filesystem) are its counterpart. O is
        left in the page cache as cp left it, as in a real cp-then-rsync run.
        """
        output = paths['output']
        copy = self.local_command(['cp', '-a', self.b0, output], f'{method}-prep-copy.log')
        if copy['exit_code'] != 0:
            return [copy]
        return [copy, self.local_command(['sync', '-f', output], f'{method}-prep-sync.log')]

    def prepare(self, method, paths, daemon_holder: dict) -> dict | None:
        """Untimed set-up before the cache drop.

        pgdelta's stats directory; rsync's daemon; for the in-place methods and pgBackRest,
        B, a copy of B0 (B already exists in a real refresh, and B0 must stay unchanged for
        the next method); for pgdelta_direct*, a clean stop of the source cluster, recorded
        in daemon_holder so that run_method restarts it whatever happens next.
        """
        started = time.perf_counter()
        steps = []
        if method in measure.PGDELTA_METHODS:
            self.source.agent('run', '--log', self.source_log(f'{method}-mkdir-stats.log'), '--',
                              'mkdir', '-p', f'{self.work}/stats')
        if method in measure.BASIS_COPY_METHODS:
            steps.append(self.copy_basis(method, paths))
        if method in measure.PGBACKREST_METHODS:
            # pgBackRest appends to its file log; start each run's afresh (tool_reported keeps it).
            Path(self.pgbackrest_file_log(method)).unlink(missing_ok=True)
        if method in measure.PGDELTA_DIRECT_METHODS:
            daemon_holder['source_stopped'] = True  # set first: a failed stop is still restarted
            steps.append({'what': 'stop the source cluster cleanly (pg_ctl stop -m fast)',
                          **self.source.script(SOURCE_CLUSTER, 'stop-clean', '--root', self.source_root,
                                               '--bin-dir', str(self.bin))})
        if steps:
            return {'what': 'untimed set-up before the cache drop', 'steps': steps,
                    'wall_seconds': round(time.perf_counter() - started, 3)}
        if method not in measure.RSYNC_METHODS:
            return None
        daemon_holder['daemon'] = self.start_rsyncd(method)
        return {'what': 'start the rsync daemon (untimed, a standing service; the copy of B0 '
                        'is the timed prep, run at the same time as the capture)',
                'wall_seconds': round(time.perf_counter() - started, 3)}

    def copy_basis(self, method, paths) -> dict:
        """B, a durable copy of B0 that the method patches, made before the cache drop (untimed).

        A real old copy shares no blocks with anything, so B must not either: --reflink=never,
        since cp's default (--reflink=auto since coreutils 9.0) clones on dest's XFS, and every
        page then written into B would be a copy-on-write unshare of a block of B0. FIEMAP
        checks the result; a B that shares blocks fails the run before the cache drop.
        """
        output = paths['output']
        copy = self.local_command(['cp', '-a', BASIS_COPY_REFLINK, self.b0, output],
                                  f'{method}-setup-copy.log')
        if copy['exit_code'] != 0:
            raise CommandError(f'the copy of B0 to {output} failed: {failed([copy])}')
        sync = self.local_command(['sync', '-f', output], f'{method}-setup-sync.log')
        if sync['exit_code'] != 0:
            raise CommandError(f'sync -f {output} failed: {failed([sync])}')
        sharing = measure.shared_extents(output)
        if sharing['shared_files']:
            raise CommandError(f'the copy of B0 to {output} shares disk blocks with another file despite '
                               f'{BASIS_COPY_REFLINK}: {sharing["shared_files"]} files, '
                               f'{sharing["shared_bytes"]} bytes (for example {sharing["examples"][:3]})')
        return {'what': f'copy B0 to B (cp -a {BASIS_COPY_REFLINK}, then sync -f), the copy the method '
                        'patches; FIEMAP finds no block of B shared with another file',
                'commands': [copy, sync], 'shared_extents': sharing}

    def restart_source(self) -> dict:
        """Start the source cluster again after pgdelta_direct*'s clean stop (untimed)."""
        return self.source.script(SOURCE_CLUSTER, 'start', '--root', self.source_root, '--bin-dir',
                                  str(self.bin), '--log-dir', self.source_log('cluster'))

    def run_phases(self, method, paths, daemon_holder: dict) -> dict:
        if method == 'full':
            return self.run_full(paths)
        if method in measure.INCREMENTAL_METHODS:
            return self.run_incremental(method, paths)
        if method in measure.PGDELTA_DIRECT_METHODS:
            return self.run_pgdelta_direct(method, paths)
        if method in measure.PGDELTA_METHODS:
            return self.run_pgdelta(method, paths)
        if method in measure.RSYNC_METHODS:
            return self.run_rsync(method, paths, daemon_holder)
        if method in measure.PGBACKREST_METHODS:
            return self.run_pgbackrest(method, paths)
        raise CommandError(f'unknown method {method}')

    def verify(self, method, paths, phases) -> dict:
        if method in measure.PGDELTA_DIRECT_METHODS:
            return self.verify_direct(method, paths)
        if method in measure.PGBACKREST_METHODS:
            return self.verify_pgbackrest(method, paths)
        output = Path(paths['output'])
        check = self.local_command([self.bin / 'pg_verifybackup', output],
                                   f'{method}-verify.log')
        result = {'pg_verifybackup_exit_code': check['exit_code'],
                  'pg_verifybackup_seconds': check['wall_seconds']}
        if method in measure.STAGED_METHODS:
            capture, digest = self.both(lambda: measure.tree_digest(output),
                                        lambda: self.source.agent('digest', paths['capture'],
                                                                  timeout=3600))
            identical, differences = measure.compare_digests(capture, digest)
            independent, problems = measure.independence(digest, self.b0_digest)
            result.update(identical_to_capture=identical, differences=differences,
                          independent_of_basis=independent, independence_problems=problems,
                          capture_digest=measure.digest_summary(capture),
                          output_digest=measure.digest_summary(digest),
                          capture_bytes=capture['bytes'], output_bytes=digest['bytes'])
            if method in measure.IN_PLACE_METHODS:
                # B, refreshed in place, keeps neither the marker nor the renamed control file.
                result['in_place_leftovers'] = measure.in_place_leftovers(digest)
            wal_files = measure.wal_files_from_digest(capture)  # S's pg_wal, as sent
        else:
            result['output_bytes'] = tree_bytes(output)
            if 'increment' in paths:
                result['capture_bytes'] = tree_bytes(Path(paths['increment']))
            wal_files = measure.wal_files(output)
        try:
            manifest = (output / 'backup_manifest').read_text()
        except OSError:
            manifest = None
        try:
            result['wal'] = measure.wal_summary(wal_files, manifest)
        except (ValueError, KeyError, TypeError) as exc:
            result['wal'] = {'wal_bytes': sum(wal_files.values()), 'error': str(exc)}
        # Which kind of copy O is: on XFS, rsync's cp -a and pgdelta's copy_file_range clone B0.
        result['shared_extents'] = measure.shared_extents(output)
        after = measure.tree_stat(self.b0)
        result['basis_changes'] = measure.stat_changes(self.b0_stat, after)
        result['verified'] = (check['exit_code'] == 0 and not result['basis_changes']
                              and result.get('identical_to_capture', True)
                              and result.get('independent_of_basis', True)
                              and not result.get('in_place_leftovers'))
        return result

    def verify_direct(self, method, paths) -> dict:
        """pgdelta_direct*: O (or B) against the files pgdelta's datadir mode selects, then a start.

        Untimed, while the source is still stopped: the output's regular files must be
        those `python -m pgdelta.datadir list` selects in the source's data directory,
        byte for byte (SHA-256); it shares no inode with B0; in place, no marker or renamed
        control file is left. Then the output is started on dest on a spare port and must
        answer a query (it is stopped again), and B0 must be unchanged.
        """
        output = Path(paths['output'])
        source, digest = self.both(
            lambda: measure.tree_digest(output),
            lambda: self.source.agent('datadir-digest', self.source_data, '--pg-controldata',
                                      str(self.bin / 'pg_controldata'), timeout=3600))
        expected = source['digest']
        identical, differences = measure.compare_digests(expected, measure.files_of(digest))
        differences = [f'selected but not a regular file on the source: {path}'
                       for path in expected.get('missing', [])] + differences
        independent, problems = measure.independence(digest, self.b0_digest)
        result = {'identical_to_source': identical and not expected.get('missing'),
                  'differences': differences[:20], 'selected_files': len(source['selected']),
                  # Listed files sent with another file's bytes (an unlogged relation's main fork).
                  'read_from': source.get('read_from'),
                  'independent_of_basis': independent, 'independence_problems': problems,
                  'source_digest': measure.digest_summary(expected),
                  'output_digest': measure.digest_summary(digest),
                  'capture_bytes': expected['bytes'], 'output_bytes': digest['bytes'],
                  'wal': measure.wal_summary(measure.wal_files_from_digest(digest), None)}
        if method in measure.IN_PLACE_METHODS:
            result['in_place_leftovers'] = measure.in_place_leftovers(digest)
        result['shared_extents'] = measure.shared_extents(output)
        # Only now, with the files compared: starting the output changes it.
        result['output_start'] = self.check_output(method, output, recovery=False)
        result['basis_changes'] = measure.stat_changes(self.b0_stat, measure.tree_stat(self.b0))
        result['verified'] = (result['identical_to_source'] and independent
                              and not result.get('in_place_leftovers')
                              and result['output_start']['ok'] and not result['basis_changes'])
        return result

    def verify_pgbackrest(self, method, paths) -> dict:
        """pgbackrest_*: start the restored B, let it replay the archive, and take its digest.

        Recovery fetches WAL through archive-get from the source's repository, over the link:
        both VMs' counters are read around the start (untimed), and run_method adds those bytes
        to pgBackRest's wire bytes (EXP-08: pgBackRest pays for the WAL it needs, as pgdelta
        does inside its transfer). The restored copy's per-table digest is compared with the
        source's at the end of the trial (logical_match, None until then): scanning the source
        before the last method would set hint bits and change its pages under the other methods.
        """
        output = Path(paths['output'])
        result = {'output_bytes': tree_bytes(output), 'shared_extents': measure.shared_extents(output)}
        before_source, before_dest = self.snapshot_source(), self.snapshot_dest()
        result['output_start'] = self.check_output(method, output, recovery=True,
                                                   query=measure.logical_digest_sql())
        after_dest, after_source = self.snapshot_dest(), self.snapshot_source()
        source = measure.snapshot_delta(before_source, after_source)
        dest = measure.snapshot_delta(before_dest, after_dest)
        try:
            server_log = self.dest_log(f'{method}-output-server.log').read_text(errors='replace')
        except OSError:
            server_log = ''
        result['recovery'] = {
            'what': "starting the restored copy, untimed: recovery's archive-get over SSH, then the digest",
            'net': {'source': source['net'], 'dest': dest['net']},
            'counter_window_seconds': {'source': source['interval_seconds'], 'dest': dest['interval_seconds']},
            'wal_files_restored': measure.restored_wal_files(server_log)}
        if result['output_start']['ok']:
            result['logical'] = json.loads(result['output_start']['answer'])
        result['logical_match'] = None
        result['logical_check'] = ("compared with the source's digest after the trial's last method "
                                   '(a scan of the source sets hint bits)')
        result['basis_changes'] = measure.stat_changes(self.b0_stat, measure.tree_stat(self.b0))
        result['verified'] = result['output_start']['ok'] and not result['basis_changes']
        return result

    # --- outputs started on dest (verification) ------------------------------

    def output_start_argv(self, data: Path, log: Path) -> list:
        """pg_ctl start of a verified output on dest: a spare port, its own socket, no TCP, no archiving.

        The output's postgresql.conf is the source's: it listens on the source's address,
        which dest doesn't have, and in pgBackRest trials it archives into the source's
        repositories. Command-line settings override both.
        """
        options = ['-p', str(OUTPUT_PG_PORT), '-k', str(self.output_socket),
                   '-c', 'listen_addresses=', '-c', 'archive_mode=off']
        return [self.bin / 'pg_ctl', '-D', data, '-l', log, '-o', shlex.join(options), '-w', '-t',
                str(OUTPUT_START_TIMEOUT), 'start']

    def output_stop_argv(self, data: Path, mode: str = 'fast') -> list:
        return [self.bin / 'pg_ctl', '-D', data, '-m', mode, '-w', '-t', '300', 'stop']

    def psql_argv(self, host, port: int, sql: str) -> list:
        return [self.bin / 'psql', '-X', '-A', '-t', '-q', '-v', 'ON_ERROR_STOP=1', '-h', host,
                '-p', str(port), '-U', PG_USER, '-d', PG_DATABASE, '-c', sql]

    def query(self, sql: str, *, host, port: int, log_name: str, timeout: float = 7200) -> str:
        """One psql query from dest; its output, or CommandError. The session is pinned to UTC."""
        argv = [str(part) for part in self.psql_argv(host, port, sql)]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                stdin=subprocess.DEVNULL,
                                env=server_env(PGOPTIONS=measure.LOGICAL_SESSION_OPTIONS))
        log = self.dest_log(log_name)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f'$ {shlex.join(argv)}\n{result.stdout}{result.stderr}')
        if result.returncode != 0:
            raise CommandError(f'psql on {host}:{port} failed ({result.returncode}): '
                               f'{result.stderr.strip()[-1000:]}')
        return result.stdout.strip()

    def wait_recovery(self, method, data: Path, timeout: float = OUTPUT_START_TIMEOUT) -> None:
        """Until the started output has finished recovery (pg_is_in_recovery() is false)."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self.query('SELECT pg_is_in_recovery();', host=self.output_socket,
                              port=OUTPUT_PG_PORT, log_name=f'{method}-output-recovery.log') == 'f':
                    return
            except CommandError:
                pass  # not accepting connections yet
            if not (data / 'postmaster.pid').exists():
                raise CommandError('the output stopped during recovery')
            if time.monotonic() > deadline:
                raise CommandError(f'the output was still in recovery after {timeout} s')
            time.sleep(0.5)

    def check_output(self, method, data: Path, *, recovery: bool, query: str = OUTPUT_QUERY) -> dict:
        """Start an output on dest, query it, and stop it again (untimed).

        With recovery, wait until it has replayed what it needs and left recovery first.
        The server is stopped whatever happens, before cleanup can remove its directory.
        """
        self.output_socket.mkdir(parents=True, exist_ok=True)
        result = {'ok': False, 'answer': None, 'error': None}
        started = time.perf_counter()
        try:
            if self.output_stuck:
                raise CommandError(f'an output started earlier still runs on port {OUTPUT_PG_PORT} '
                                   f'({self.output_stuck["data"]}); not starting another')
            argv = self.output_start_argv(data, self.dest_log(f'{method}-output-server.log'))
            start = self.local_command(argv, f'{method}-output-start.log',
                                       timeout=OUTPUT_START_TIMEOUT + 60, env=server_env())
            result['start'] = start
            if start['exit_code'] != 0:
                raise CommandError(f'the output did not start: {failed([start])}')
            if recovery:
                self.wait_recovery(method, data)
            result['ready_seconds'] = round(time.perf_counter() - started, 3)
            result['answer'] = self.query(query, host=self.output_socket, port=OUTPUT_PG_PORT,
                                          log_name=f'{method}-output-query.log')
            result['ok'] = True
        except (CommandError, subprocess.TimeoutExpired) as exc:
            result['error'] = f'{type(exc).__name__}: {exc}'
        finally:
            result['stop'] = self.stop_output(method, data)
        return result

    def stop_output(self, method, data: Path) -> dict:
        """pg_ctl stop -m fast, then immediate; refuses to return while the output still runs.

        If it still runs, the output is marked stuck (output_stuck): run_method and
        cleanup_trial then never remove its data directory under the running server, and no
        later output is started on its port.
        """
        stops = []
        for mode in ('fast', 'immediate'):
            if not (data / 'postmaster.pid').exists():
                break
            stops.append(self.local_command(self.output_stop_argv(data, mode),
                                            f'{method}-output-stop-{mode}.log', env=server_env()))
        if (data / 'postmaster.pid').exists():
            self.output_stuck = {'data': str(data), 'method': method, 'trial_id': self.trial_id,
                                 'port': OUTPUT_PG_PORT, 'stops': stops}
            log(f'  ERROR the output at {data} could not be stopped; it is left in place')
            raise CommandError(f'the output at {data} could not be stopped')
        return {'commands': stops}

    def stale_output_server(self) -> str | None:
        """A server an earlier, interrupted run left on the output port, from its socket's lock file.

        The lock file holds the postmaster's PID and data directory; None if there is no lock
        file or its PID no longer runs (PostgreSQL replaces a stale one itself).
        """
        lock = self.output_socket / f'.s.PGSQL.{OUTPUT_PG_PORT}.lock'
        try:
            lines = lock.read_text().splitlines()
            pid = int(lines[0])
        except (OSError, ValueError, IndexError):
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass
        data = lines[1] if len(lines) > 1 else 'unknown'
        return f'PID {pid}, data directory {data}'

    def tool_reported(self, method, paths, phases) -> dict:
        transfer = phases.get('transfer')
        if method in measure.PGDELTA_METHODS:
            text = self.source.agent('run', '--log', self.source_log(f'{method}-stats.json.log'),
                                     '--', 'cat', paths['stats'])['log_tail']
            stats = parse_pgdelta_stats(text)
            reported = stats['reported']
            prefetch = (reported.get('receiver') or {}).get('prefetch')
            engine = measure.PGDELTA_ENGINES[method]
            return {**measure.pgdelta_modes_report(reported, engine),
                    'bytes_source_to_dest': stats['bytes_source_to_dest'],
                    'bytes_dest_to_source': stats['bytes_dest_to_source'],
                    'scope': 'pgdelta --stats-json frame bytes (no TCP/IP overhead)',
                    'mode': reported.get('mode'), 'gate_reason': reported.get('gate_reason'),
                    'pages': reported.get('pages'), 'files': reported.get('files'),
                    'timings_seconds': reported.get('timings_seconds'),
                    'jobs': reported.get('jobs'),
                    # The engine in effect on each end, against the one the method pins.
                    'flags': {command: list(args) for command, args in measure.PGDELTA_ARGS[method].items()},
                    'switches': reported.get('switches'),
                    'switches_ok': measure.pgdelta_switches_match(measure.pgdelta_switches(engine),
                                                                  reported.get('switches')),
                    # The receiver's read of B0 into the page cache while it waited for the
                    # sender, and whether its switch is the one the method pins.
                    'prefetch': prefetch,
                    'prefetch_ok': (isinstance(prefetch, dict) and prefetch.get('switch')
                                    == measure.PGDELTA_ENGINES[method]['prefetch_basis']),
                    'worker_start_method': {'source': reported.get('worker_start_method'),
                                            'dest': (reported.get('receiver') or {}).get('worker_start_method')},
                    'by_category': {'source_to_dest': reported.get('bytes_source_to_dest'),
                                    'dest_to_source': reported.get('bytes_dest_to_source')}}
        if method in measure.RSYNC_METHODS and transfer:
            sender = next(c for c in transfer['commands'] if c['host'] == 'source')
            stats = rsync_transfer_stats(sender.get('log_tail', ''))
            return {**stats, 'scope': 'rsync --stats on the sending client (protocol stream, '
                                      'no TCP/IP overhead)'}
        if method in measure.PGBACKREST_METHODS:
            return self.pgbackrest_reported(method, phases)
        return {'bytes_source_to_dest': None, 'bytes_dest_to_source': None,
                'scope': FULL_BYTES_SCOPE}

    def run_method(self, trial: dict, repeat: int, speed: dict, link: dict, method: str,
                   order_index: int, order: list[str]) -> dict:
        paths = self.method_paths(method)
        record = {'schema': SCHEMA_RUN, 'run_name': self.out.name, 'trial_id': self.trial_id,
                  'pattern': trial['pattern'], 'fraction': trial['fraction'],
                  'scale': trial['scale'], 'rows_changed': trial.get('rows_changed'),
                  'repeat': repeat, 'speed': speed, 'speed_index': link.get('speed_index'),
                  'speed_order': link.get('speed_order'), 'method': method,
                  'order_index': order_index, 'method_order': order,
                  'link': {key: link[key] for key in link if key.endswith('gbit')},
                  'paths': {key: str(value) for key, value in paths.items()},
                  'started_utc': utc_now(), 'status': 'running', 'error': None}
        log(f'{self.trial_id} r{repeat} {speed["label"]} [{order_index + 1}/{len(order)}] {method}')
        daemon_holder: dict = {}
        self.log_prefix = f'r{repeat}-{speed["label"]}-'
        try:
            stuck = self.output_stuck
            if stuck and (str(paths['output']) == stuck['data'] or method in measure.OUTPUT_START_METHODS):
                # Its directory holds the running server, or its verification would start one on its port.
                raise CommandError(f'an output started on port {OUTPUT_PG_PORT} for an earlier verification '
                                   f'could not be stopped ({stuck["data"]}); {method} cannot run')
            self.clean_method(paths)
            phases: dict = {}
            record['phases'] = phases
            record['preparation'] = self.prepare(method, paths, daemon_holder)
            # One drop on both VMs: every method's timed phases start from cold
            # caches. rsync's copy of B0 (in its end-to-end time, EXP-04's design)
            # runs after it, beside the capture, and is synced inside its timer.
            record['cache_drop'] = self.drop_caches()
            record['caches_dropped'] = True
            phases.update(self.run_phases(method, paths, daemon_holder))
            record['totals'] = measure.phase_totals(phases)
            if method in measure.RSYNC_METHODS:
                record['totals_in_place'] = measure.phase_totals(
                    measure.without_part(phases, 'prep'))
            if method in measure.STAGED_METHODS:
                # The concurrent phase's two parts one after the other: the upper bound.
                record['totals_serial'] = measure.serial_totals(phases)
            problems = [problem for phase in phases.values() for problem in phase['failures']]
            if problems:
                raise CommandError('; '.join(problems))
            record['tool_reported'] = self.tool_reported(method, paths, phases)
            if method in measure.PGDELTA_METHODS:
                # B0 the receiver read between its two timed phases, off the clock.
                record['prefetch_untimed'] = measure.prefetch_untimed(
                    phases, (record['tool_reported'] or {}).get('prefetch'))
                if (record['prefetch_untimed'] or {}).get('spilled'):
                    log(f'  prefetch read B0 for {record["prefetch_untimed"]["untimed_seconds"]} s '
                        'between the timed phases')
            verification = self.verify(method, paths, phases)
            record['verification'] = verification
            if verification.get('recovery'):
                # pgbackrest_*: the WAL its recovery fetched, untimed, counts in its wire bytes.
                record['totals'] = measure.with_recovery_bytes(record['totals'], verification['recovery'])
            record['output_bytes'] = verification.get('output_bytes')
            record['capture_bytes'] = verification.get('capture_bytes')
            record['wal'] = verification.get('wal')
            padding = (record['wal'] or {}).get('wal_padding_bytes')
            record['output_minus_wal_padding_bytes'] = (
                None if record['output_bytes'] is None or padding is None
                else record['output_bytes'] - padding)
            if verification['basis_changes']:
                raise CommandError('B0 changed: ' + '; '.join(verification['basis_changes']))
            record['status'] = 'pass' if verification['verified'] else 'fail'
            if not verification['verified']:
                record['error'] = 'output verification failed'
            elif 'logical_match' in verification and verification['logical_match'] is None:
                # pgbackrest_*: passes or fails once the source's digest is taken, after the
                # trial's last method (finish_logical_checks).
                record['status'] = 'pending'
        except Exception as exc:  # record, clean up, carry on with the next method
            record['status'] = 'error'
            record['error'] = f'{type(exc).__name__}: {exc}'
            record['traceback'] = traceback.format_exc()
            log(f'  ERROR {record["error"]}')
        finally:
            daemon = daemon_holder.pop('daemon', None)
            if daemon is not None:
                self.stop_rsyncd(daemon)
            if daemon_holder.pop('source_stopped', False):
                # pgdelta_direct*: the source runs again for the next method, whatever happened.
                try:
                    record['source_restart'] = self.restart_source()
                except Exception as exc:
                    self.source_down = f'{type(exc).__name__}: {exc}'
                    record['source_restart'] = {'error': self.source_down}
                    record['status'] = 'error'
                    record['error'] = (record['error'] + '; ' if record['error'] else '') + \
                        f'the source cluster could not be restarted: {self.source_down}'
                    log(f'  ERROR the source cluster could not be restarted: {self.source_down}')
            if self.output_stuck and self.output_stuck['data'] == str(paths['output']):
                record['output_stuck'] = self.output_stuck  # left in place; later output starts refused
            if not self.args.keep_outputs:
                try:
                    self.clean_method(paths)
                except Exception as exc:
                    record['cleanup_error'] = str(exc)
        self.log_prefix = ''
        record['finished_utc'] = utc_now()
        totals = record.get('totals') or {}
        log(f'  {record["status"]}: {totals.get("wall_seconds")} s, '
            f'{totals.get("wire_bytes_source_to_dest")} B source->dest, '
            f'{totals.get("wire_bytes_dest_to_source")} B dest->source')
        save_json(self.record_path(record), record)
        self.records.append(record)
        measure.write_csv(self.out / 'summary.csv', self.records)
        return record

    # --- pgBackRest (T11) ---------------------------------------------------

    @property
    def pgbackrest_methods(self) -> list[str]:
        return [method for method in self.methods if method in measure.PGBACKREST_METHODS]

    @property
    def pgbackrest_paths(self) -> dict:
        return measure.pgbackrest_paths(self.work)

    def pgbackrest_file_log(self, stanza: str) -> str:
        return f'{self.pgbackrest_paths["log"]}/{stanza}-restore.log'

    def pgbackrest_texts(self) -> dict:
        """Every file the pgBackRest set-up writes, and the archive_command, for this run."""
        stanzas = self.pgbackrest_methods
        paths = self.pgbackrest_paths
        return {
            'source_config': measure.pgbackrest_source_config(
                self.work, stanzas, pg_path=self.source_data, socket_path=f'{self.source_root}/s',
                port=PG_PORT, user=PG_USER),
            'dest_config': measure.pgbackrest_dest_config(self.work, stanzas, source_ip=self.source.ip,
                                                          ssh_user=self.args.ssh_user),
            'ssh_config': measure.pgbackrest_ssh_config(self.source.ip, self.args.ssh_user,
                                                        self.args.ssh_key),
            'ssh_wrapper': measure.pgbackrest_ssh_wrapper(paths['ssh_config']),
            'archive_command': measure.pgbackrest_archive_command(self.pgbackrest_exe, paths['config'],
                                                                  stanzas)}

    def pgbackrest_run(self, *args, log_name: str) -> dict:
        """pgbackrest on the source (untimed set-up); CommandError if it fails."""
        result = self.source.agent('run', '--log', self.source_log(log_name), '--', self.pgbackrest_exe,
                                   f'--config={self.pgbackrest_paths["config"]}', *args, timeout=3600)
        if result['exit_code'] != 0:
            raise CommandError(f'pgbackrest {" ".join(args)} on the source failed ({result["exit_code"]}): '
                               f'{result["log_tail"][-2000:]}')
        return result

    def setup_pgbackrest(self) -> dict:
        """Untimed, before B0, in trials with a pgbackrest method only.

        On the source: a fresh configuration with one stanza per selected variant, each
        with its repository under the work directory; stanza-create for every stanza; then
        archive_mode = on with an archive_command that pushes every WAL file to every
        stanza (one restart of the source cluster); then check. On dest: pgBackRest's
        configuration, with the source as repository host, and its own SSH configuration
        using the harness key; `info` per stanza proves it reaches the repository without a
        prompt.
        """
        texts = self.pgbackrest_texts()
        paths = self.pgbackrest_paths
        started = time.perf_counter()
        self.remove_source(paths['root'])
        self.remove_dest(paths['root'])
        self.source.write(paths['config'], texts['source_config'])
        Path(paths['root']).mkdir(parents=True, exist_ok=True)
        Path(paths['config']).write_text(texts['dest_config'])
        Path(paths['ssh_config']).write_text(texts['ssh_config'])
        Path(paths['ssh']).write_text(texts['ssh_wrapper'])
        Path(paths['ssh']).chmod(0o755)
        for key in ('log', 'lock', 'spool'):
            Path(paths[key]).mkdir(parents=True, exist_ok=True)
        self.source.agent('run', '--log', self.source_log('pgbackrest-mkdir.log'), '--', 'mkdir', '-p',
                          paths['log'], paths['lock'], paths['spool'])
        # Every stanza exists before archiving starts, since each WAL file goes to all of them.
        created = {stanza: self.pgbackrest_run(f'--stanza={stanza}', 'stanza-create',
                                               log_name=f'pgbackrest-{stanza}-stanza-create.log')
                   for stanza in self.pgbackrest_methods}
        archive = self.source.script(SOURCE_CLUSTER, 'archive', '--root', self.source_root, '--bin-dir',
                                     str(self.bin), '--log-dir', self.source_log('cluster'),
                                     '--command', texts['archive_command'])
        stanzas = {}
        for stanza in self.pgbackrest_methods:
            create = created[stanza]
            check = self.pgbackrest_run(f'--stanza={stanza}', 'check',
                                        log_name=f'pgbackrest-{stanza}-check.log')
            info = self.local_command([self.pgbackrest_exe, f'--config={paths["config"]}',
                                       f'--stanza={stanza}', 'info'], f'pgbackrest-{stanza}-dest-info.log')
            if info['exit_code'] != 0:
                raise CommandError(f'pgbackrest info on dest cannot reach the repository of {stanza}: '
                                   f'{failed([info])}')
            stanzas[stanza] = {'stanza_create_exit_code': create['exit_code'],
                               'check_exit_code': check['exit_code'],
                               'dest_info_exit_code': info['exit_code'],
                               'repository': measure.pgbackrest_repo(self.work, stanza),
                               'options': measure.PGBACKREST_VARIANTS[stanza]}
        return {**texts, 'archive': archive, 'stanzas': stanzas, 'backups': {},
                'wall_seconds': round(time.perf_counter() - started, 3)}

    def pgbackrest_backups(self) -> dict:
        """One full backup per selected variant, untimed, after the workload's CHECKPOINT."""
        backups = {}
        for stanza in self.pgbackrest_methods:
            started = time.perf_counter()
            backup = self.pgbackrest_run(f'--stanza={stanza}', '--type=full', 'backup',
                                         log_name=f'pgbackrest-{stanza}-backup.log')
            seconds = round(time.perf_counter() - started, 3)
            info = self.pgbackrest_run(f'--stanza={stanza}', '--output=json', 'info',
                                       log_name=f'pgbackrest-{stanza}-info.json')
            try:
                summary = measure.pgbackrest_backup_summary(info['log_tail'])
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                summary = {'error': f'{type(exc).__name__}: {exc}'}
            backups[stanza] = {**summary, 'wall_seconds': seconds, 'exit_code': backup['exit_code'],
                               'log_tail': backup['log_tail'][-4000:]}
            log(f'{self.trial_id}: pgBackRest {stanza} full backup in {seconds} s '
                f'({summary.get("repo_size_bytes")} bytes in the repository)')
        return backups

    def pgbackrest_reported(self, method, phases) -> dict:
        """pgBackRest's own account of a restore: its console log and its detailed file log."""
        restore = (phases.get(measure.RESTORE_PHASE) or {}).get('commands') or [{}]
        console = ''
        if restore[0].get('log'):
            try:
                console = Path(restore[0]['log']).read_text(errors='replace')
            except OSError:
                pass
        kept = self.dest_log(f'{method}-pgbackrest-restore-file.log')
        try:
            detail = Path(self.pgbackrest_file_log(method)).read_text(errors='replace')
            kept.parent.mkdir(parents=True, exist_ok=True)
            kept.write_text(detail)
        except OSError:
            detail, kept = '', None
        return {'bytes_source_to_dest': None, 'bytes_dest_to_source': None,
                'scope': 'pgBackRest reports no network byte counts; the wire columns are the only '
                         "byte measure (its SSH is encrypted, with SSH's compression off; pgbackrest_none "
                         "also turns pgBackRest's own network compression off, compress-level-network=0)",
                'flags': self.method_flags(method),
                'restore': measure.parse_pgbackrest_restore(console + '\n' + detail),
                'backup': ((self.pgbackrest or {}).get('backups') or {}).get(method),
                'file_log': None if kept is None else str(kept), 'console_tail': console[-8000:]}

    def finish_logical_checks(self, trial: dict) -> dict | None:
        """The source's per-table digest, against every pending pgBackRest run of this trial.

        Taken once the trial's last method has run, as the row-level ground truth is: a scan
        of the source earlier would set hint bits and change pages under the other methods.
        Each pending run becomes 'pass' or 'fail'; its record and the CSV are rewritten.
        """
        pending = [record for record in self.records
                   if record.get('trial_id') == self.trial_id and record.get('status') == 'pending']
        if not pending:
            return None
        started = time.perf_counter()
        source = json.loads(self.query(measure.logical_digest_sql(), host=self.source.ip, port=PG_PORT,
                                       log_name='source-logical-digest.log'))
        check = {'source': source, 'seconds': round(time.perf_counter() - started, 3), 'runs': []}
        for record in pending:
            verification = record['verification']
            match, differences = measure.compare_logical(source, verification.get('logical') or {})
            verification.update(logical_match=match, logical_differences=differences, source_logical=source)
            record['status'] = 'pass' if match else 'fail'
            if not match:
                record['error'] = "the restored copy's digest differs from the source's"
            save_json(self.record_path(record), record)
            for run in trial['runs']:
                if (run['repeat'], run['speed'], run['method']) == (
                        record['repeat'], record['speed']['label'], record['method']):
                    run['status'] = record['status']
            check['runs'].append({'repeat': record['repeat'], 'speed': record['speed']['label'],
                                  'method': record['method'], 'match': match})
        measure.write_csv(self.out / 'summary.csv', self.records)
        return check

    def pgbackrest_run_record(self) -> dict | None:
        """What run.json says about pgBackRest: nothing unless a pgbackrest method is selected."""
        if not self.pgbackrest_methods:
            return None
        return {'methods': self.pgbackrest_methods, 'exe': self.pgbackrest_exe,
                'archiving': ('archive_mode = on in every trial of this run, set up before B0 because a '
                              'pgbackrest method is selected; every WAL file is pushed to every stanza. '
                              'Runs without a pgbackrest method do not archive'),
                'repository_host': 'pgd-source, under the work directory',
                **self.pgbackrest_texts()}

    def method_flags(self, method: str) -> dict:
        """The flags a method pins, for run.json and its records."""
        if method in measure.PGDELTA_ARGS:
            return {command: list(args) for command, args in measure.PGDELTA_ARGS[method].items()}
        if method in measure.PGBACKREST_METHODS:
            return {'restore': ['--delta', f'--process-max={measure.PGBACKREST_PROCESS_MAX}'],
                    'stanza': dict(measure.PGBACKREST_VARIANTS[method]),
                    'dest_stanza': measure.pgbackrest_dest_options(method),
                    'backup': ['--type=full', 'start-fast=y',
                               f'process-max={measure.PGBACKREST_PROCESS_MAX}']}
        if method in measure.RSYNC_METHODS:
            return {'rsync': self.rsync_argv(method)[1:-2]}
        flags = {'pg_basebackup': ['--manifest-checksums', self.args.manifest_checksums]}
        if method in measure.INCREMENTAL_METHODS:
            flags['pg_combinebackup'] = [measure.COMBINE_MODES[method],
                                         f'--manifest-checksums={self.args.manifest_checksums}']
        return flags

    # --- trials ------------------------------------------------------------

    def record_path(self, record: dict) -> Path:
        return (self.out / 'runs' / record['trial_id']
                / f'r{record["repeat"]}-{record["speed"]["label"]}-{record["method"]}.json')

    def cluster_args(self, trial: dict) -> list[str]:
        argv = ['--root', self.source_root, '--bin-dir', str(self.bin),
                '--log-dir', self.source_log('cluster'), '--scale', str(trial['scale']),
                '--pattern', trial['pattern'], '--fraction', repr(trial['fraction']),
                '--seed', str(self.args.seed), '--hot-percent', repr(self.args.hot_percent),
                '--batch-size', str(self.args.batch_size)]
        if self.args.events_rows is not None:
            argv += ['--events-rows', str(self.args.events_rows)]
        return argv

    def take_b0(self, trial: dict) -> None:
        self.remove_dest(self.b0)
        log(f'{self.trial_id}: B0 over the unthrottled link')
        backup = self.timed_phase('b0', lambda: [self.local_command(
            self.basebackup(self.b0, host=self.source.ip), 'b0.log')])
        if backup['failures']:
            raise CommandError('B0 failed: ' + '; '.join(backup['failures']))
        check = self.local_command([self.bin / 'pg_verifybackup', self.b0], 'b0-verify.log')
        if check['exit_code'] != 0:
            raise CommandError('pg_verifybackup failed on B0')
        self.b0_digest = measure.tree_digest(self.b0)
        self.b0_stat = measure.tree_stat(self.b0)
        trial['b0'] = {'backup': backup, 'pg_verifybackup_exit_code': check['exit_code'],
                       'digest': measure.digest_summary(self.b0_digest)}

    def run_trial(self, index: int, pattern: str, fraction: float) -> dict:
        args = self.args
        # A source that could not be restarted stops its own trial; this one builds a new cluster.
        self.source_down = None
        self.trial_id = f'{index:02d}-{pattern}-f{fraction:g}-s{args.scale}'
        trial = {'schema': SCHEMA_TRIAL, 'trial_id': self.trial_id, 'pattern': pattern,
                 'fraction': fraction, 'scale': args.scale, 'seed': args.seed,
                 'started_utc': utc_now(), 'status': 'running', 'links': [], 'runs': []}
        save_json(self.out / 'trials' / f'{self.trial_id}.json', trial)
        try:
            self.throttle('off')
            log(f'{self.trial_id}: source cluster and dataset (scale {args.scale})')
            setup = self.source.script(SOURCE_CLUSTER, 'setup', *self.cluster_args(trial),
                                       '--listen-ip', self.source.ip, '--client-ip', self.dest_ip)
            trial['setup'] = setup
            trial['rows_changed'] = workloads.target_rows(
                pattern, fraction, accounts_rows=setup['spec']['accounts_rows'],
                events_rows=setup['spec']['events_rows'])
            self.pgbackrest = None
            if self.pgbackrest_methods:
                # Only in a run with a pgbackrest method: repositories, archiving (one restart of
                # the source) and stanzas, before B0, so B0 is taken from the archiving cluster.
                log(f'{self.trial_id}: pgBackRest set-up ({", ".join(self.pgbackrest_methods)})')
                self.pgbackrest = trial['pgbackrest'] = self.setup_pgbackrest()
            self.take_b0(trial)
            log(f'{self.trial_id}: workload {pattern} k={trial["rows_changed"]}')
            trial['workload'] = self.source.script(SOURCE_CLUSTER, 'workload',
                                                   *self.cluster_args(trial))
            if self.pgbackrest is not None:
                # The repository is counted as already paid: one full backup per variant, untimed.
                self.pgbackrest['backups'] = self.pgbackrest_backups()
            save_json(self.out / 'trials' / f'{self.trial_id}.json', trial)
            trial['speed_orders'] = []
            for repeat in range(args.repeats):
                speeds = measure.order_speeds(self.speeds, self.speed_rng, args.speed_order,
                                              repeat)
                labels = [speed['label'] for speed in speeds]
                trial['speed_orders'].append(labels)
                self.run.setdefault('speed_orders', {}).setdefault(self.trial_id, []).append(
                    labels)
                log(f'{self.trial_id} r{repeat}: speed order {", ".join(labels)}')
                for speed_index, speed in enumerate(speeds):
                    link = self.set_speed(speed)
                    link.update(repeat=repeat, speed_index=speed_index, speed_order=labels)
                    here = measure.methods_at(self.methods, self.method_speeds, speed['label'])
                    order = measure.method_orders(here, self.rng, 1)[0]
                    link['method_order'] = order
                    for order_index, method in enumerate(order):
                        record = self.run_method(trial, repeat, speed, link, method,
                                                 order_index, order)
                        trial['runs'].append({'repeat': repeat, 'speed': speed['label'],
                                              'method': method, 'status': record['status']})
                        if self.source_down:
                            raise CommandError(f'the source cluster is down after {method}: '
                                               f'{self.source_down}')
                        if record['status'] not in ('pass', 'pending') and args.stop_on_error:
                            raise CommandError(f'{method} at {speed["label"]}: {record["error"]}')
                    link['qdisc_after'] = self.qdisc_stats()
                    trial['links'].append(link)
                    self.run.setdefault('links', []).append(
                        {'trial_id': self.trial_id, 'repeat': repeat, 'speed': speed['label'],
                         'speed_index': speed_index,
                         **{key: link[key] for key in link if key.endswith('gbit')}})
                    save_json(self.out / 'trials' / f'{self.trial_id}.json', trial)
                    if self.out.joinpath('run.json').exists():
                        save_json(self.out / 'run.json', self.run)
            self.throttle('off')
            # pgBackRest's restored copies against the source, now that no method is left to run.
            logical_check = self.finish_logical_checks(trial)
            if logical_check is not None:
                trial['logical_check'] = logical_check
            trial['verify'] = self.source.script(SOURCE_CLUSTER, 'verify', *self.cluster_args(trial))
            final = measure.tree_digest(self.b0)
            same, differences = measure.compare_digests(self.b0_digest, final)
            trial['b0_unchanged'] = same
            trial['b0_differences'] = differences
            if not same:
                raise CommandError('B0 changed during the trial: ' + '; '.join(differences))
            bad = [run for run in trial['runs'] if run['status'] != 'pass']
            trial['status'] = 'pass' if not bad else 'fail'
        except Exception as exc:
            trial['status'] = 'error'
            trial['error'] = f'{type(exc).__name__}: {exc}'
            trial['traceback'] = traceback.format_exc()
            log(f'{self.trial_id}: ERROR {trial["error"]}')
        finally:
            self.cleanup_trial(trial)
        trial['finished_utc'] = utc_now()
        save_json(self.out / 'trials' / f'{self.trial_id}.json', trial)
        return trial

    def cleanup_trial(self, trial: dict) -> None:
        steps = (('throttle off', lambda: self.throttle('off')),
                 ('stop source cluster', lambda: self.source.script(
                     SOURCE_CLUSTER, 'stop', '--root', self.source_root, '--bin-dir', str(self.bin),
                     *([] if self.args.keep_cluster else ['--remove']))),
                 ('fetch source logs', self.fetch_source_logs),
                 ('remove B0 and outputs', lambda: None if self.args.keep_cluster else
                  self.remove_dest(self.b0, *self.removable_outputs())),
                 ('remove pgBackRest repositories', lambda: None if self.args.keep_cluster
                  or not self.pgbackrest_methods else (self.remove_source(self.pgbackrest_paths['root']),
                                                       self.remove_dest(self.pgbackrest_paths['root']))),
                 ('recreate output directory', lambda: self.outputs.mkdir(exist_ok=True)))
        for name, step in steps:
            try:
                step()
            except Exception as exc:
                trial.setdefault('cleanup_errors', []).append(f'{name}: {exc}')
                log(f'{self.trial_id}: cleanup step {name} failed: {exc}')

    def removable_outputs(self) -> list:
        """The outputs directory, or, while an output server is stuck, everything in it but that output."""
        stuck = (self.output_stuck or {}).get('data')
        if not stuck:
            return [self.outputs]
        log(f'{self.trial_id}: leaving {stuck}: its server could not be stopped')
        return [path for path in sorted(self.outputs.iterdir()) if str(path) != stuck] \
            if self.outputs.is_dir() else []

    def fetch_source_logs(self) -> None:
        target = self.out / 'logs' / self.trial_id / 'source'
        target.mkdir(parents=True, exist_ok=True)
        remote = f'{self.work}/logs/{self.trial_id}'
        pack = subprocess.run(self.source.argv(['tar', '-C', remote, '-cf', '-', '.']),
                              capture_output=True, timeout=600)
        if pack.returncode != 0:
            raise CommandError(pack.stderr.decode(errors='replace')[-1000:])
        subprocess.run(['tar', '-C', str(target), '-xf', '-'], input=pack.stdout, check=True)
        self.remove_source(remote)

    # --- run ---------------------------------------------------------------

    def environment(self) -> dict:
        source, dest = self.both(
            lambda: self.local_agent('env', '--iface', self.dest_iface, '--bin-dir', str(self.bin)),
            lambda: self.source.agent('env', '--iface', self.source_iface, '--bin-dir',
                                      str(self.bin)))
        return {'source': source, 'dest': dest}

    def rss_probe(self) -> dict:
        """/bin/true through the launchers every tool uses, with and without GNU time.

        Without it, wait4 reports the launcher's own high-water mark (this
        orchestrator on dest, the agent on source); with it, a few MB. A run
        refuses to start if GNU time is missing or reports more than
        RSS_PROBE_LIMIT_KB for /bin/true.
        """
        logs = self.out / 'logs' / 'rss-probe'
        plain = Child(['/bin/true'], logs / 'dest-wait4.log').wait(60)
        timed = Child(['/bin/true'], logs / 'dest-gnu-time.log', gnu_time=self.gnu_time).wait(60)
        remote_log = f'{self.work}/logs/rss-probe'
        remote_plain = self.source.agent('run', '--log', f'{remote_log}/wait4.log', '--',
                                         '/bin/true')
        remote_timed = self.source.agent('run', '--gnu-time', self.gnu_time, '--log',
                                         f'{remote_log}/gnu-time.log', '--', '/bin/true')
        probe = {
            'command': '/bin/true', 'gnu_time': self.gnu_time,
            'dest': {'launcher': 'run_exp04.py (Child)',
                     'launcher_maxrss_kb': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                     'wait4_maxrss_kb': plain['rusage']['wait4_maxrss_kb'],
                     'gnu_time_maxrss_kb': timed['peak_rss_kb']},
            'source': {'launcher': 'agent.py run',
                       'wait4_maxrss_kb': remote_plain['rusage']['wait4_maxrss_kb'],
                       'gnu_time_maxrss_kb': remote_timed.get('peak_rss_kb')},
            'limit_kb': RSS_PROBE_LIMIT_KB}
        log(f'peak-RSS probe (/bin/true): dest {probe["dest"]["gnu_time_maxrss_kb"]} KiB under '
            f'GNU time vs {probe["dest"]["wait4_maxrss_kb"]} KiB from wait4; source '
            f'{probe["source"]["gnu_time_maxrss_kb"]} vs {probe["source"]["wait4_maxrss_kb"]}')
        bad = [host for host in ('dest', 'source')
               if probe[host]['gnu_time_maxrss_kb'] is None
               or probe[host]['gnu_time_maxrss_kb'] > RSS_PROBE_LIMIT_KB]
        if bad:
            raise SystemExit(f'GNU time at {self.gnu_time} gives no usable peak RSS on '
                             f'{", ".join(bad)} ({probe}); install it with sudo apt-get '
                             'install time, or pass --gnu-time none')
        return probe

    def check_pgdelta_jobs(self) -> dict:
        """The selected methods' pinned flags must be in `pgdelta <command> --help` on both VMs.

        The default methods need `send --jobs` and `receive --jobs --rebuild --verify
        --prefetch-basis`; T3's add the receiver's --fsync and --in-place, T8a's the
        sender's --source-kind and the pgdelta.datadir module that their verification runs.
        """
        required = measure.pgdelta_required_options(self.methods)
        found = {}
        for command, options in required.items():
            local = subprocess.run([sys.executable, '-m', 'pgdelta', command, '--help'],
                                   cwd=self.repo, capture_output=True, text=True, timeout=120)
            remote = self.source.agent(
                'run', '--log', f'{self.work}/logs/pgdelta-{command}-help.log', '--cwd',
                self.remote_repo, '--', self.source.python, '-m', 'pgdelta', command, '--help')
            found[command] = {'dest': pgdelta_supports(local.stdout, options),
                              'source': pgdelta_supports(remote.get('log_tail', ''), options)}
        if any(method in measure.PGDELTA_DIRECT_METHODS for method in self.methods):
            module = measure.PGDELTA_DATADIR_MODULE
            local = subprocess.run([sys.executable, '-c', f'import {module}'], cwd=self.repo,
                                   capture_output=True, text=True, timeout=120)
            remote = self.source.agent(
                'run', '--log', f'{self.work}/logs/pgdelta-datadir-import.log', '--cwd',
                self.remote_repo, '--', self.source.python, '-c', f'import {module}')
            found[module] = {'dest': local.returncode == 0, 'source': remote['exit_code'] == 0}
        if not all(all(hosts.values()) for hosts in found.values()):
            raise SystemExit(f'the selected methods need pgdelta options {required}'
                             + (f' and {measure.PGDELTA_DATADIR_MODULE}' if measure.PGDELTA_DATADIR_MODULE
                                in found else '') + f', which the deployed pgdelta lacks: {found}')
        return found

    def background(self) -> dict:
        """Interface and CPU counters over an idle window: the measurement floor."""
        seconds = self.args.idle_seconds
        before_source, before_dest = self.snapshot_source(), self.snapshot_dest()
        time.sleep(seconds)
        after_dest, after_source = self.snapshot_dest(), self.snapshot_source()
        result = {}
        for host, before, after in (('source', before_source, after_source),
                                    ('dest', before_dest, after_dest)):
            delta = measure.snapshot_delta(before, after)
            window = delta['interval_seconds']
            result[host] = {**delta, 'rx_bytes_per_second': round(delta['net']['rx_bytes'] / window),
                            'tx_bytes_per_second': round(delta['net']['tx_bytes'] / window),
                            'busy_cpu_fraction': round(delta['cpu']['busy_seconds'] / window, 4)}
        return result

    def phase_floor(self, repeats: int) -> dict:
        """What an empty timed phase records: the counter snapshots' own cost.

        Each timed phase's window holds the exit of its 'before' snapshot agent
        and the start of its 'after' one (python3 over SSH on the source), so
        every phase adds about this much to the VM-wide CPU columns. The totals
        do not subtract it; counter_windows in each row says how many floors a
        total holds (full 1; incremental, pgdelta and rsync 2).
        """
        samples = [self.timed_phase('floor', lambda: []) for _ in range(repeats)]
        result = {'repeats': repeats}
        for host in ('source', 'dest'):
            busy = [sample['cpu'][host]['busy_seconds'] for sample in samples]
            result[host] = {
                'busy_seconds': busy, 'median_busy_seconds': statistics.median(busy),
                'tx_bytes': [sample['net'][host]['tx_bytes'] for sample in samples],
                'window_seconds': [sample['counter_window_seconds'][host] for sample in samples]}
        return result

    def execute(self) -> int:
        args = self.args
        self.speeds = measure.parse_speeds(args.speeds)
        self.methods = [m.strip() for m in args.methods.split(',') if m.strip()]
        unknown = [m for m in self.methods if m not in measure.ALL_METHODS]
        if unknown or not self.methods or len(set(self.methods)) != len(self.methods):
            raise SystemExit('--methods must be distinct names from '
                             f'{", ".join(measure.ALL_METHODS)}')
        try:
            self.method_speeds = measure.parse_method_speeds(args.method_speeds, self.methods, self.speeds)
        except ValueError as exc:
            raise SystemExit(str(exc))
        # A speed at which --method-speeds leaves no method is not visited at all.
        self.speeds = [speed for speed in self.speeds
                       if measure.methods_at(self.methods, self.method_speeds, speed['label'])]
        patterns = [p.strip() for p in args.patterns.split(',') if p.strip()]
        fractions = [float(f) for f in args.fractions.split(',') if f.strip()]
        for pattern in patterns:
            if pattern not in workloads.PATTERNS or pattern == 'tpcb':
                raise SystemExit(f'unsupported pattern {pattern!r}')
        self.work.mkdir(parents=True, exist_ok=True)
        lock = open(self.work / '.exp04.lock', 'w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(f'another run_exp04.py holds {self.work}/.exp04.lock')
        if self.out.exists() and any(p.name not in ('driver.log', 'driver.pid')
                                     for p in self.out.iterdir()):
            raise SystemExit(f'output directory is not empty: {self.out}')
        self.out.mkdir(parents=True, exist_ok=True)
        self.outputs.mkdir(parents=True, exist_ok=True)
        stale = self.stale_output_server()
        if stale and any(method in measure.OUTPUT_START_METHODS for method in self.methods):
            raise SystemExit(f'a server still runs on the output port {OUTPUT_PG_PORT} ({stale}), left by '
                             'an earlier run: stop it (pg_ctl -D <its data directory> stop) first')

        self.source.open()
        try:
            route = subprocess.run(['ip', '-o', 'route', 'get', self.source.ip],
                                   capture_output=True, text=True, check=True).stdout.split()
            self.dest_iface = route[route.index('dev') + 1]
            self.dest_ip = args.dest_ip or route[route.index('src') + 1]
            self.source_iface = self.source.agent('iface', '--peer', self.dest_ip)['iface']
            self.source.agent('run', '--log', f'{self.work}/logs/mkdir.log', '--', 'mkdir', '-p',
                              str(self.work))
            self.run = {
                'schema': SCHEMA, 'name': self.out.name, 'argv': sys.argv[1:],
                'started_utc': utc_now(), 'args': vars(args),
                'hosts': {'source': {'ip': self.source.ip, 'iface': self.source_iface},
                          'dest': {'ip': self.dest_ip, 'iface': self.dest_iface}},
                'speeds': self.speeds, 'methods': self.methods, 'patterns': patterns,
                'fractions': fractions, 'order_seed': args.order_seed,
                'speed_order': args.speed_order, 'speed_orders': {},
                'cache_policy': CACHE_POLICY,
                'timing': 'wall time on pgd-dest (time.perf_counter) around each phase. '
                          'totals.wall_seconds (total_s) is end to end: every timed phase, '
                          "rsync's prep included. rsync's prep (dest) and capture (source) "
                          'run at the same time in one phase, prep_capture, whose wall is '
                          "the pair's, so rsync's total is max(prep, capture) + transfer. "
                          "pgdelta's receiver (dest; from its launch until it listens, then it "
                          'prefetches B0 while it waits) and its capture (source) run at the '
                          'same time in receiver_capture, so its total is max(receiver start, '
                          'capture) + transfer. totals_serial (serial_total_s, rsync and '
                          'pgdelta) adds the two parts instead, an upper bound, and '
                          'totals_in_place (inplace_total_s, rsync only) leaves out the prep, '
                          'the in-place lower bound. Remote commands include the '
                          'multiplexed SSH round trip and agent start-up; '
                          'wall_seconds_less_remote_overhead (total_no_ssh_s) takes out '
                          'outer minus inner wall of each where it is on the critical path. '
                          'T3, T8a and T11 methods: pgdelta_inplace and pgbackrest_* patch B, a copy '
                          'of B0 made before the cache drop and not timed; pgdelta_direct* is one '
                          'phase, receiver_transfer, whose steps receiver_start and transfer follow '
                          'each other with no untimed gap, after a clean stop of the source that is '
                          'not timed; pgbackrest_* is the restore alone, its backup counted as '
                          'already paid',
                'wire_bytes': '/proc/net/dev of the internal interface on both VMs, read just '
                              'before and after each phase; source->dest is the source tx '
                              'counter, dest->source the dest tx counter. pgbackrest_* add the '
                              'bytes read around the untimed start of the restored copy, whose '
                              'recovery fetches WAL through archive-get (recovery_wire_*)',
                'peak_rss': (f'each tool run under GNU time ({self.gnu_time} -f %M); the '
                             'per-host figure is the largest of its commands. '
                             + measure.PEAK_RSS_SCOPE + ' Server-side walsenders are not '
                             'child processes and are not covered'
                             if self.gnu_time else 'not measured (--gnu-time none)'),
                'method_speeds': self.method_speeds,
                'methods_by_speed': {speed['label']: measure.methods_at(self.methods, self.method_speeds,
                                                                        speed['label'])
                                     for speed in self.speeds},
                'method_flags': {method: self.method_flags(method) for method in self.methods},
                'pgbackrest': self.pgbackrest_run_record(),
                'trials': {}, 'status': 'running'}
            self.throttle('off')
            self.run['environment'] = self.environment()
            deployed = {host: (env.get('deployed') or {}).get('tree_sha256')
                        for host, env in self.run['environment'].items()}
            self.run['same_code_on_both'] = len(set(deployed.values())) == 1 and None not in \
                deployed.values()
            if not self.run['same_code_on_both'] and not args.allow_code_mismatch:
                raise SystemExit(f'the two VMs run different code: {deployed}')
            if any(method in measure.RSYNC_METHODS for method in self.methods):
                missing = [host for host, env in self.run['environment'].items()
                           if not env.get('rsync_has_fsync')]
                if missing:
                    raise SystemExit(f'rsync on {", ".join(missing)} has no --fsync')
            if self.pgbackrest_methods:
                missing = [host for host, env in self.run['environment'].items()
                           if not env.get('pgbackrest')]
                if missing:
                    raise SystemExit(f'pgbackrest is not installed on {", ".join(missing)} '
                                     '(infra/gcp/startup.sh installs it)')
            if self.gnu_time:
                self.run['rss_probe'] = self.rss_probe()
            if any(method in measure.PGDELTA_METHODS for method in self.methods):
                self.run['pgdelta_required_options'] = measure.pgdelta_required_options(self.methods)
                self.run['pgdelta_jobs_check'] = self.check_pgdelta_jobs()
            if args.disk_test_mib:
                self.run['disk_test'] = dict(zip(('source', 'dest'), self.both(
                    lambda: self.local_agent('disk-test', str(self.work), '--mib',
                                             str(args.disk_test_mib)),
                    lambda: self.source.agent('disk-test', str(self.work), '--mib',
                                              str(args.disk_test_mib)))))
            if args.idle_seconds:
                self.run['background'] = self.background()
            if args.phase_floor_repeats:
                self.run['phase_floor'] = self.phase_floor(args.phase_floor_repeats)
            save_json(self.out / 'run.json', self.run)
            combos = list(itertools.product(patterns, fractions))
            for index, (pattern, fraction) in enumerate(combos):
                trial = self.run_trial(index, pattern, fraction)
                self.run['trials'][trial['trial_id']] = trial['status']
                save_json(self.out / 'run.json', self.run)
                if trial['status'] == 'error' and args.stop_on_error:
                    break
        finally:
            try:
                self.throttle('off')
            except Exception as exc:
                log(f'could not remove the throttle: {exc}')
            self.source.close()
            self.pool.shutdown(wait=False)
        statuses = list(self.run['trials'].values())
        self.run['status'] = 'pass' if statuses and all(s == 'pass' for s in statuses) else 'fail'
        self.run['finished_utc'] = utc_now()
        save_json(self.out / 'run.json', self.run)
        log(f'done: {self.run["status"]}; results in {self.out}')
        return 0 if self.run['status'] == 'pass' else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--source-ip', required=True, help="pgd-source's internal address")
    parser.add_argument('--dest-ip', help="this VM's internal address (default: from the route)")
    parser.add_argument('--out', required=True, help='new or empty results directory')
    parser.add_argument('--work-dir', default='/data/exp04',
                        help='scratch directory on both VMs (same path)')
    parser.add_argument('--bin-dir', default='/usr/lib/postgresql/17/bin')
    parser.add_argument('--remote-repo', help='repository path on the source (default: same as here)')
    parser.add_argument('--ssh-user', default=os.environ.get('USER', 'bench'))
    parser.add_argument('--ssh-key', default=os.path.expanduser('~/.ssh/exp04_ed25519'))
    parser.add_argument('--patterns', default='uniform,hot,append')
    parser.add_argument('--fractions', default='0.01')
    parser.add_argument('--scale', type=int, default=70)
    parser.add_argument('--seed', type=int, default=20261022, help="workload seed (EXP-01's)")
    parser.add_argument('--hot-percent', type=float, default=1.0)
    parser.add_argument('--events-rows', type=int)
    parser.add_argument('--batch-size', type=int, default=100)
    parser.add_argument('--speeds', default=DEFAULT_SPEEDS,
                        help='comma list of Gbit/s values and/or "unthrottled"')
    parser.add_argument('--methods', default=','.join(measure.METHODS),
                        help='comma list from ' + ', '.join(measure.ALL_METHODS) +
                             f' (default: {",".join(measure.METHODS)}; pgdelta_1thread is '
                             "EXP-02's single-threaded engine, --jobs 1 --rebuild gap-fill "
                             '--verify sequential; the T3, T8a and T11 methods are in the README)')
    parser.add_argument('--method-speeds', action='append', default=[], metavar='METHODS=SPEEDS',
                        help="run some methods at some of --speeds only, e.g. "
                             "'pgdelta_seq+pgdelta_inplace=unthrottled' (methods joined with +, speeds "
                             'as in --speeds); repeatable. Methods not named run at every speed')
    parser.add_argument('--pgbackrest', default='/usr/bin/pgbackrest',
                        help='pgBackRest on both VMs (pgbackrest_* methods)')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--order-seed', type=int, default=4,
                        help='seed for the per-speed random method order and the per-repeat '
                             'speed order')
    parser.add_argument('--speed-order', choices=measure.SPEED_ORDERS, default='shuffle',
                        help='order of the speeds within each repeat: a new seeded random '
                             'order per repeat (default), alternately as given and reversed, '
                             'or as given')
    parser.add_argument('--manifest-checksums', default='SHA256',
                        choices=('NONE', 'CRC32C', 'SHA224', 'SHA256', 'SHA384', 'SHA512'),
                        help="backup manifest checksums for every pg_basebackup and pg_combinebackup "
                             "(B0, the full copy, incremental, the staged captures). Default SHA256, as "
                             "EXP-04's sweep ran; PostgreSQL's own default is CRC32C (EXP-07)")
    parser.add_argument('--gnu-time', default='/usr/bin/time',
                        help="GNU time, for each tool's own peak RSS ('none' to skip)")
    parser.add_argument('--link-test-seconds', type=float, default=3.0)
    parser.add_argument('--idle-seconds', type=float, default=5.0,
                        help='idle window to measure background traffic (0 to skip)')
    parser.add_argument('--phase-floor-repeats', type=int, default=3,
                        help='empty timed phases at the start, to measure the CPU each '
                             "phase's counter snapshots add (0 to skip)")
    parser.add_argument('--disk-test-mib', type=int, default=1024,
                        help='dd write/read test size on both VMs at the start (0 to skip)')
    parser.add_argument('--command-timeout', type=float, default=4 * 3600)
    parser.add_argument('--keep-outputs', action='store_true',
                        help='keep each method output (needs disk space for all of them)')
    parser.add_argument('--keep-cluster', action='store_true',
                        help='keep the source cluster and B0 after each trial')
    parser.add_argument('--stop-on-error', action='store_true')
    parser.add_argument('--allow-code-mismatch', action='store_true')
    args = parser.parse_args(argv)
    if args.scale < 1 or args.repeats < 1:
        parser.error('--scale and --repeats must be at least 1')
    try:
        speeds = measure.parse_speeds(args.speeds)
        methods = [method.strip() for method in args.methods.split(',') if method.strip()]
        measure.parse_method_speeds(args.method_speeds, methods, speeds)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def _terminate(signum, frame):
    raise KeyboardInterrupt(f'signal {signum}')


def main(argv=None) -> int:
    args = parse_args(argv)
    signal.signal(signal.SIGTERM, _terminate)
    return Orchestrator(args).execute()


if __name__ == '__main__':
    raise SystemExit(main())
