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

Optional methods for T2 (EXP-11, the fastest durable full copy; they need
--manifest-checksums CRC32C):

    full_nosync, full_syncfs, full_wb
                   full with --no-sync, with --sync-method=syncfs, and on a dest whose
                   vm.dirty_background_bytes is 64 MiB for the run. Every pg_basebackup arm,
                   full included, runs with --verbose, its lines timestamped as they arrive,
                   and its output is started on dest after pg_verifybackup
    probackup8, probackup4
                   pg_probackup catchup -b FULL --stream --temp-slot -j 8 or 4 on dest from
                   the live source over SSH, then sync -f of the output, in one timed phase;
                   its started copy's per-table digest is settled at the trial's end (pass,
                   fail, or void if the source changed during the trial)
    parcopy8, parcopy4
                   a range-parallel copier built in the harness, 8 or 4 TCP streams, from
                   the cleanly stopped source, in pgdelta_direct*'s frame
    pgdelta_direct_inplace_j8
                   v1's path, pgdelta_direct_inplace, with --jobs 8 on both ends

A run that selects any of them also takes, in each trial, reference digests of the
stopped source before and after the methods, and untimed probes (the durable write
floor, the read floors, the backup-mode bracket, an 8-stream link test). Its
extension and warm-source blocks (--extension-*, --warm-source-*) follow the main
block, unthrottled. --syscall-audit (EXP-11's audit, untimed) runs every timed
command on dest under strace and checks each run's traces against the durability
definition (experiments/network/durability_audit.py).

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
import hashlib
import itertools
import json
import math
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
from experiments.network import durability_audit, measure, source_cluster  # noqa: E402
from experiments.network.agent import RESULT_PREFIX, qdisc_counters  # noqa: E402

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
PARCOPY_PORT = 7200
# check_parcopy: what each VM's Python offers parcopy (sendfile, early write-back), printed as JSON.
PARCOPY_CAPABILITIES = (f'import json; from {measure.PARCOPY_MODULE} import capabilities; '
                        'print(json.dumps(capabilities()))')
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
                'directory cold. T2 (EXP-11): parcopy* stops the source as pgdelta_direct* does; '
                "full_wb's dest write-back setting is set before the drop. In the warm-source block "
                "only dest's cache is dropped: the source runs sync and reads its data directory "
                'without pg_wal once instead, untimed, so the copy reads it warm')
# T2 (EXP-11), rule 7: every pg_basebackup's manifest has CRC32C checksums, B0's included.
T2_MANIFEST_CHECKSUMS = 'CRC32C'
LINK_TEST_STREAMS = 8  # T2: the multi-stream link test, source to dest, at each speed's first visit
WRITE_FLOOR_WRITERS = 4  # T2: the durable write floor's writers on dest
READ_FLOOR_READERS = 4  # T2: the cold read floor's readers on the source (EXP-07's C4)
BRACKET_REPEATS = 10  # T2: backup-mode pairs on the idle source
# T2: the file the single-reader probe reads, pgbench_accounts's, found from the catalog only (no
# user table is read), after the workload and before the start reference digest.
PROBE_RELATION = 'pgbench_accounts'
PROBE_RELATION_SQL = f"SELECT pg_relation_filepath('{PROBE_RELATION}');"
# T2: the blocks of a trial, in order. The source's cache is dropped before each run in the first
# two and read, warm, in the third.
BLOCKS = ('main', 'extension', 'warm_source')
# EXP-12 (T10a): its blocks, in order, under the load (busy at --load-rate, warm at the same rate, moderate at
# --moderate-rate) and then without it (calibration). The source's static files are evicted before each run,
# or, in the warm block, read once.
LOAD_BLOCKS = ('busy', 'warm', 'moderate', 'calibration')
WARM_BLOCKS = ('warm_source', 'warm')
# EXP-12's control slots: pause2 and burn4 at the busy block's start and end, pause2 at the moderate block's start.
CONTROL_SLOTS = (('busy_start', 'busy'), ('busy_end', 'busy'), ('moderate_start', 'moderate'))
CLOCK_PORT = 7300  # EXP-12: the clock probe's TCP exchange
TAIL_VIEW_MARGIN_S = 0.05  # EXP-12: the quiet tail runs this far past the 15-s view's last bucket (quiet_tail)
PROCESSES_SNAPSHOT_S = 5.0  # EXP-13: probackup8's ps of both VMs, this far into its clock (smoke item 16)
# EXP-12: the clock probe tried this many times, the back-off growing by this much each time; no offset, no load.
CLOCK_PROBE_ATTEMPTS, CLOCK_PROBE_BACKOFF_S = 3, 2.0
BACKUP_LABEL = 't10a'  # EXP-12: the label of bracket's and the proxies' backups
LOAD_SCRIPT_DIR = 'experiments/network/load'  # under the repository, on the source
# EXP-12: pgbench's --random-seed by run, from --load-seed (default --order-seed): each run its own.
LOAD_SEED_OFFSETS = {'capacity': 0, 'busy': 1, 'moderate': 2, 'restart': 10}
TIMELINE_POLICY = (
    "EXP-12's run under the load: (1) the source, untimed: the previous runs' source work (S verified with "
    "pg_verifybackup and, for staged pgdelta, hashed against O; pgdelta's statistics read; S removed), the load "
    'checked alive, the eviction (POSIX_FADV_DONTNEED of the benchmark database and the snapshot; the warm block reads '
    'them once instead) and fincore; (2) the settle: dest\'s set-up and cache drop (uncapped, dest_wait_s), then the '
    "source's gate (agent.py settle-gate: at least --settle-s, at most --settle-max-s, else unsettled), with nothing "
    'on the source but the load, the sampler and the counter reads; (3) the timed phase or phases, dest\'s counters '
    'for checks (a) and (b) read just before the clock starts and first after it stops; t0 and t1 in UTC on dest\'s '
    "clock, mapped to the source's with the clock probe's offset; (4) the quiet tail (--tail-s from the clock's "
    "stop): dest's verification, which reaches nothing on the source, then the rest waited out. The calibration "
    'block has no load, no gate and no tail; the source is stopped only for v1 itself, there')
DURABILITY_POLICY = (
    "EXP-11's checks, in every run's record (durability) and the CSV: (a) dest's Dirty + Writeback "
    '(/proc/meminfo), read just before the clock starts and first after it stops, grows by at most '
    '4 MiB or the idle noise (run.json dirty_idle_noise, --dirty-noise-mib), whichever is larger; '
    "(b) for a full copy, dest's md array writes (/proc/diskstats) in the window are at least 0.98 x "
    "the output's bytes. full_nosync is the negative control. parcopy* (A5): its receiver's fsync calls must "
    "equal its inventory's files plus directories plus one, and what its output holds, or (a) fails. A run's "
    "status is its verification's; durability_ok is apart")
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


def last_json(text: str):
    """The JSON value a command printed on its last non-empty line, or None."""
    lines = [line for line in (text or '').splitlines() if line.strip()]
    try:
        return json.loads(lines[-1]) if lines else None
    except ValueError:
        return None


class CommandError(RuntimeError):
    pass


class InfrastructureFault(CommandError):
    """EXP-13's A11 (i): a visit's checks failed twice, before any run; the trial stops, recorded as such."""


class Source:
    """pgd-source over one multiplexed SSH connection.

    With `port` (EXP-13's --control-port), the connection goes to the source's sshd on that port, which the
    emulated WAN (infra/gcp/wan.sh) leaves neither delayed nor shaped; port 22 stays for the methods' own SSH.
    """

    def __init__(self, ip: str, user: str, key: str, repo: str, python: str = 'python3', port: int | None = None):
        self.ip, self.repo, self.python, self.port = ip, repo, python, port
        self.control = f'/tmp/exp04-ssh-{os.getpid()}'
        self.options = ['-i', key, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new',
                        '-o', f'ControlPath={self.control}', '-o', 'ServerAliveInterval=15',
                        '-o', 'ServerAliveCountMax=8', *(['-o', f'Port={port}'] if port else [])]
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


def messages_env() -> dict:
    """This environment with untranslated messages (LC_MESSAGES=C), for tools whose lines are parsed."""
    env = {key: value for key, value in os.environ.items() if key != 'LC_ALL'}
    return {**env, 'LC_MESSAGES': 'C'}


def checksum_problem(methods, checksums: str, load=None, conditions=None) -> str | None:
    """Why the selected methods can't run with these manifest checksums, or None.

    EXP-11's rule 7: CRC32C for every pg_basebackup, B0 included. --manifest-checksums still defaults
    to SHA256, EXP-04's setting, so T2's methods refuse to run without CRC32C. So do EXP-12's arms and any
    run under its load (--load): B0, full and the staged capture (its rule 7); and EXP-13's arms and any run at
    its conditions (--conditions).
    """
    t2 = [method for method in methods if method in measure.T2_METHODS or method in measure.T10A_METHODS
          or method in measure.T12_METHODS]
    if load and not t2:
        t2 = [f'--load {load}']
    if conditions and not t2:
        t2 = ['--conditions']
    if t2 and checksums != T2_MANIFEST_CHECKSUMS:
        return (f'{", ".join(t2)} need --manifest-checksums {T2_MANIFEST_CHECKSUMS} (EXP-11, EXP-12 and EXP-13, '
                f'rule 7), not {checksums}')
    return None


def method_list(text: str) -> list[str]:
    return [method.strip() for method in (text or '').split(',') if method.strip()]


def find_window(reported: dict):
    """The request window pgdelta's statistics report (its sender's), or None."""
    return measure.find_stat(reported, 'window')


def block_problems(args, methods) -> list[str]:
    """What is wrong with the extension and warm-source options for these --methods."""
    problems = []
    for block in BLOCKS[1:]:
        flag = block.replace('_', '-')
        repeats, chosen = getattr(args, f'{block}_repeats'), method_list(getattr(args, f'{block}_methods'))
        if repeats < 0:
            problems.append(f'--{flag}-repeats must be 0 or more')
        if bool(repeats) != bool(chosen):
            problems.append(f'--{flag}-repeats and --{flag}-methods go together')
        unknown = [method for method in chosen if method not in methods]
        if unknown or len(set(chosen)) != len(chosen):
            problems.append(f'--{flag}-methods must be distinct names from --methods, not {", ".join(unknown)}'
                            if unknown else f'--{flag}-methods names a method twice')
    return problems


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
    The pipe ends only once every process holding it has exited, so a process
    that outlives the command (a grandchild such as an ssh it started) keeps the
    reader going: settle() waits for it, at most STAMP_READER_TIMEOUT, then
    closes the log, and the wait is the result's reader_wait_seconds. wait()
    settles at once, unless Child.deferred is a list (Orchestrator.timed_phase
    sets one around each timed phase): then the Child goes on it, and is
    settled after the window, so the clock stops when the command exits.

    While Child.trace is set (EXP-11's syscall audit, run_exp04 --syscall-audit: Orchestrator.timed_phase
    sets it around each timed phase), the command runs under strace (durability_audit.strace_argv), inside
    GNU time, its trace beside its log; argv stays the tool's own.
    """

    trace = False
    deferred: list | None = None

    def __init__(self, argv, log_path: Path, *, cwd=None, env=None, gnu_time=None, stamp_lines=False):
        self.argv = [str(part) for part in argv]
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.time_path = Path(f'{self.log_path}.time') if gnu_time else None
        self.trace_path = Path(f'{self.log_path}.strace') if Child.trace else None
        launch = self.argv
        if self.trace_path is not None:
            self.trace_path.unlink(missing_ok=True)
            launch = [*durability_audit.strace_argv(self.trace_path), *launch]
        if self.time_path is not None:
            self.time_path.unlink(missing_ok=True)
            launch = measure.gnu_time_argv(gnu_time, self.time_path, launch)
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
        timing = measure.read_gnu_time(self.time_path) if self.time_path else None
        self.result = {'host': 'dest', 'argv': self.argv, 'exit_code': self.process.returncode,
                       'wall_seconds': round(wall, 6), 'started_utc': self.started_utc,
                       'rusage': measure.rusage_record(usage),
                       'peak_rss_kb': timing['maxrss_kb'] if timing else None,
                       'gnu_time': timing, 'launcher': self.launcher,
                       'log': str(self.log_path), 'timed_out': self.timed_out,
                       'stamped_lines': self.reader is not None}
        if self.trace_path is not None:
            self.result['trace'] = str(self.trace_path)
        if self.reader is not None and Child.deferred is not None:
            Child.deferred.append(self)  # settled after the timed phase's window
        else:
            self.settle()
        return self.result

    def settle(self) -> None:
        """The stamping reader waited for until the output's pipe ends (at most STAMP_READER_TIMEOUT), then
        the log closed; how long that took is the result's reader_wait_seconds."""
        if self.reader is not None and not self.stream.closed:
            started = time.perf_counter()
            self.reader.join(STAMP_READER_TIMEOUT)
            self.result['reader_wait_seconds'] = round(time.perf_counter() - started, 6)
        self.stream.close()

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


SSH_FAILED = 255  # ssh's own exit status when the connection fails


def harness_failure(command: dict) -> bool:
    """A failed timed command whose failure is the harness's, not the tool's (EXP-12's "unverified" causes): SSH
    itself failing for a command run straight over it (ssh exits 255: the proxy's backup-hold), or a command stamped
    over SSH whose agent never wrote its result line (the SSH session or the agent ended first). A timeout or any
    other exit is the tool's: the copy didn't finish."""
    if command.get('exit_code') == 0 or command.get('timed_out'):
        return False
    if command.get('over_ssh') and command.get('exit_code') == SSH_FAILED:
        return True
    return bool(command.get('stamped_over_ssh')) and command.get('reported_exit_code') is None


class Orchestrator:
    def __init__(self, args):
        self.args = args
        self.work = Path(args.work_dir)
        self.out = Path(args.out)
        self.bin = Path(args.bin_dir)
        self.repo = str(REPO_ROOT)
        self.remote_repo = args.remote_repo or self.repo
        self.source = Source(args.source_ip, args.ssh_user, args.ssh_key, self.remote_repo,
                             port=getattr(args, 'control_port', None))
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
        # T2 (EXP-11). The blocks after the main one, run unthrottled: method lists.
        self.extension_methods = method_list(args.extension_methods)
        self.warm_source_methods = method_list(args.warm_source_methods)
        # dest's vm.dirty_background_ratio and _bytes as found at the start (execute), which every
        # run's guard requires and full_wb's reset restores; None outside T2 runs.
        self.writeback_defaults: dict | None = None
        # Why dest's write-back settings could not be returned to those defaults, even by the guard's own
        # reset: it stops the trial and the invocation, since every later run would be refused.
        self.writeback_stuck: str | None = None
        # Check (a)'s idle noise in bytes: --dirty-noise-mib, or what the start-up windows measured.
        self.idle_noise_bytes: int | None = None
        # This trial's 8-stream link test per speed label (taken at each speed's first visit).
        self.multi_stream_links: dict = {}
        # This trial's reference digests of the stopped source ('start', 'end'): reference_view()s.
        self.reference_digests: dict = {}
        # The file the single-reader probe reads, relative to the source's data directory.
        self.probe_relation_path: str | None = None
        # The (repeat, speed label) of the method run under way (run_method).
        self.current_run: tuple | None = None
        # This trial's probackup* outputs' byte views, by (repeat, speed label, method), for the byte
        # diagnostic against the end reference digest if the source changed (finish_logical_checks).
        self.byte_views: dict = {}
        # EXP-12 (T10a), under the load (--load); each is set again per trial (run_load_trial).
        self.snapshot_digest: dict | None = None  # the snapshot's selection, hashed at step 5 (datadir-digest's)
        self.database_oids: dict = {}  # the source's databases: name -> OID (their files are base/<OID>)
        self.references: dict = {}  # step 4's, without load: the logical digest, the invariant, the schema hashes
        self.clock_offset_s: float | None = None  # the source's clock minus dest's, from the latest clock probe
        self.idle_rate: float | None = None  # the source's idle busy CPU-s per second, steal left out (step 6)
        self.load_runs: list = []  # every pgbench run of the trial, as load-start and load-stop reported it
        self.load_current: dict | None = None
        self.load_rate = None  # the current run's -R
        self.load_used = False  # the load's directory on the source holds logs to keep (from step 7 on)
        # Source work left by a run for the next run's untimed step (before its settle): S verified and removed,
        # pgdelta's statistics read. Each is {'record', 'paths'} with what it needs.
        self.pending_source: list = []
        self.output_digests: dict = {}  # staged pgdelta's O digests (dest), waiting for S's (source)
        self.static_digests: set = set()  # (method, speed) cells whose first repeat took (vi)'s digest
        self.repeat_counter = 0  # the trial's repeats and control slots, numbered on across its blocks
        # The controls' order within a slot, from a stream of its own so the arms' orders don't shift.
        self.control_rng = random.Random(f'exp12-controls-{args.order_seed}')
        self.current_trial: dict | None = None
        # EXP-12's blocks after the busy one, and its controls (parse_args has checked them).
        self.controls = method_list(getattr(args, 'controls', ''))
        self.warm_speeds = measure.parse_speeds(getattr(args, 'warm_source_speeds', '') or measure.UNTHROTTLED)
        self.moderate_methods = method_list(getattr(args, 'moderate_methods', ''))
        self.moderate_speeds = (measure.parse_speeds(args.moderate_speeds)
                                if getattr(args, 'moderate_speeds', '') else [])
        self.moderate_method_speeds = (measure.parse_method_speeds(args.moderate_method_speeds, self.moderate_methods,
                                                                   self.moderate_speeds)
                                       if getattr(args, 'moderate_method_speeds', None) else {})
        self.calibration_methods = method_list(getattr(args, 'calibration_methods', ''))
        self.calibration_speed = measure.parse_speed(getattr(args, 'calibration_speed', '2') or '2')
        # EXP-13 (T12), at the emulated WAN's conditions (--conditions); parse_args has checked its options.
        self.conditions: list = measure.parse_conditions(args.conditions) if getattr(args, 'conditions', '') else []
        self.t12_plan: dict = t12_plan(args) if self.conditions else {}
        self.condition: dict | None = None  # the condition visited now, with its visit's readings (set_condition)
        self.tcp_defaults: dict | None = None  # each VM's TCP settings as found at the start: host -> name -> value
        self.control_reference: list | None = None  # the control no-ops' seconds with the tree at the anchor
        self.noise_table: dict | None = None  # check (a)'s noise by window length (--dirty-noise-table and measured)
        self.busy_flag = False  # whether both VMs' pgdelta send takes --busy-inflight-bytes
        self.tls: dict | None = None  # the session's TLS material and stunnel configurations (preflight_t12)
        self.visited: set = set()  # this trial's condition labels visited so far
        self.zero_delay: dict = {}  # this trial's zero-delay rates by tbf rate: {'one_stream', 'bound'} (Gbit/s)
        self.current_trial_record: dict | None = None

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

    @property
    def t2(self) -> bool:
        """Whether this run selects any of EXP-11's arms (measure.T2_METHODS)."""
        return any(method in measure.T2_METHODS for method in self.methods)

    @property
    def t10a(self) -> bool:
        """Whether this run is EXP-12's: under the fixed-rate load (--load)."""
        return getattr(self.args, 'load', None) is not None

    @property
    def t12(self) -> bool:
        """Whether this run is EXP-13's: at the emulated WAN's conditions (--conditions)."""
        return bool(self.conditions)

    @property
    def pinned_methods(self) -> list[str]:
        """Every method this run may run, for run.json's pins and pgdelta's checks: --methods, and under EXP-12 its
        calibration block's (v1 itself among them) and its controls."""
        extra = [*self.calibration_methods, *self.controls] if self.t10a else []
        control = (self.t12_plan.get('negative_control') or {}).get('method') if self.t12 else None
        return list(dict.fromkeys([*self.methods, *extra, *([control] if control else [])]))

    @property
    def snapshot_data(self) -> str:
        """EXP-12's snapshot: a cleanly stopped copy of the source's data directory on its /data (the proxies read it)."""
        return f'{self.work}/snap'

    @property
    def load_dir(self) -> str:
        """The load's logs, progress lines and registry on the source (tmpfs), with the sampler's."""
        return self.args.load_log_dir

    @property
    def sampler_dir(self) -> str:
        return f'{self.load_dir}/sampler'

    def dest_log(self, name: str) -> Path:
        # Before the first trial (EXP-13's preflight), under logs/preflight.
        return self.out / 'logs' / (self.trial_id or 'preflight') / 'dest' / (self.log_prefix + name)

    def source_log(self, name: str) -> str:
        return f'{self.work}/logs/{self.trial_id or "preflight"}/{self.log_prefix}{name}'

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
        # EXP-13: nstat's TCP counters and the emulated WAN's qdisc counters too, from the same snapshot.
        extra = ['--tcp', '--qdisc'] if self.t12 else []
        return self.source.agent('snapshot', '--iface', self.source_iface, *extra, timeout=60)

    def snapshot_dest(self) -> dict:
        if self.t12:
            return {**measure.snapshot(self.dest_iface, tcp=True), 'qdisc': qdisc_counters(self.dest_iface)}
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
        """Counters on both VMs around body(); body returns its command records.

        With --syscall-audit, every command launched on dest meanwhile runs under strace (Child.trace).
        The window ends when body's last command has exited: a command whose output a thread stamps is
        settled (its reader waited for until the output's pipe ends, Child.settle) after the window, so a
        process that outlives it holding that pipe adds nothing to the clock, nor delays the next step.
        """
        before_source = self.snapshot_source()
        before_dest = self.snapshot_dest()
        started_utc = time.time()
        started = time.perf_counter()
        deferred: list = []
        Child.trace, Child.deferred = self.args.syscall_audit, deferred
        try:
            try:
                commands = body()
            finally:
                Child.trace, Child.deferred = False, None
            wall = time.perf_counter() - started
            ended_utc = time.time()
            after_dest = self.snapshot_dest()
            after_source = self.snapshot_source()
        finally:
            for child in deferred:
                child.settle()
        source = measure.snapshot_delta(before_source, after_source)
        dest = measure.snapshot_delta(before_dest, after_dest)
        phase = {'name': name, 'wall_seconds': round(wall, 3), 'started_utc': started_utc,
                 'ended_utc': ended_utc, 'commands': commands,
                 'net': {'source': source['net'], 'dest': dest['net']},
                 'cpu': {'source': source['cpu'], 'dest': dest['cpu']},
                 'counter_window_seconds': {'source': source['interval_seconds'],
                                            'dest': dest['interval_seconds']},
                 'failures': failed(commands)}
        # Dirty and Writeback at each end of the window, and each disk's bytes (EXP-11's checks). dest's
        # are read just before the clock starts and first after it stops.
        # EXP-13: the window's TCP counters (nstat) and the emulated WAN's qdisc counters on each VM too.
        for key in ('memory', 'disk', 'nstat', 'qdisc'):
            if key in source or key in dest:
                phase[key] = {'source': source.get(key), 'dest': dest.get(key)}
        return phase

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

    def drop_caches(self, source: bool = True) -> dict:
        """sync; echo 3 > drop_caches on both VMs, or with source=False on dest only (the warm-source block)."""
        if not source:
            return {'source': None, 'dest': self.local_agent('drop-caches')}
        source, dest = self.both(lambda: self.local_agent('drop-caches'),
                                 lambda: self.source.agent('drop-caches'))
        return {'source': source, 'dest': dest}

    def warm_source(self) -> dict:
        """The warm-source block's stand-in for the source's cache drop (untimed, before dest's drop).

        sync, then one read of the data directory without pg_wal, with the read floor's readers, so
        the copy finds the source's files in its page cache. A stopped source (v1, parcopy) is read
        after its clean stop.
        """
        return self.source.agent('read-test', self.source_data, '--readers', str(READ_FLOOR_READERS),
                                 '--exclude', 'pg_wal', '--sync', timeout=3600)

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

    def blast(self, direction: str, streams: int = 1, *, listen: str | None = None,
              connect: str | None = None) -> dict:
        """The link test in one direction: one bulk TCP stream, or `streams` at once (T2's 8).

        EXP-13 (--link-test-warmup): the receiver's warmup is that, it counts each second's bytes, and each stream
        sends for --link-test-seconds from its own connect. listen and connect (dest's and the source's ends)
        replace the direct path: the stunnel pair's link test.
        """
        seconds = str(self.args.link_test_seconds)
        many = ['--streams', str(streams)] if streams > 1 else []
        warmup = getattr(self.args, 'link_test_warmup', None)
        receive_extra = ['--warmup', f'{warmup:g}'] if warmup is not None else []
        send_extra = ['--after-connect'] if self.t12 else []
        if direction == 'source_to_dest':
            receiver = subprocess.Popen(
                [sys.executable, str(REPO_ROOT / AGENT), 'blast-recv', '--listen',
                 listen or f'{self.dest_ip}:{BLAST_PORT}', '--timeout', '120', *many, *receive_extra],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            sender = lambda: self.source.agent('blast-send', '--connect',  # noqa: E731
                                               connect or f'{self.dest_ip}:{BLAST_PORT}',
                                               '--seconds', seconds, *many, *send_extra, timeout=300)
        else:
            receiver = subprocess.Popen(
                self.source.agent_argv('blast-recv', '--listen', f'{self.source.ip}:{BLAST_PORT}',
                                       '--timeout', '120', *many, *receive_extra),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                stdin=subprocess.DEVNULL)
            sender = lambda: self.local_agent('blast-send', '--connect',  # noqa: E731
                                              f'{self.source.ip}:{BLAST_PORT}',
                                              '--seconds', seconds, *many, *send_extra, timeout=300)
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
        if self.t2:
            link.update(self.multi_stream_link(speed))
        self.current_speed = speed
        return link

    def multi_stream_link(self, speed: dict) -> dict:
        """T2: the 8-stream link test from source to dest at this speed, under keys ending in gbit.

        Run at the speed's first visit in the trial (each trial's first repeat; the extension's and
        the warm-source block's unthrottled visits reuse it); later visits carry that measurement,
        so each record's link time can use it. One tbf caps the sum of all streams. Like the trial's
        other probes, a test that fails is recorded ({'error': ...}, and None for its rates, so those
        records have no 8-stream link time) and the trial goes on; it isn't tried again at that speed.
        """
        label = speed['label']
        measured = label not in self.multi_stream_links
        if measured:
            test = self.probe(f'the {LINK_TEST_STREAMS}-stream link test at {label}',
                              lambda: self.blast('source_to_dest', streams=LINK_TEST_STREAMS))
            receiver = test.get('receiver') or {}
            self.multi_stream_links[label] = {
                'streams': LINK_TEST_STREAMS, 'utc': utc_now(), 'test': test,
                'source_to_dest_8streams_gbit': receiver.get('gbit'),
                'source_to_dest_8streams_steady_gbit': receiver.get('steady_gbit')}
            if 'error' not in test:
                log(f'link {label}: {LINK_TEST_STREAMS} streams {receiver.get("gbit")} Gbit/s source->dest')
        multi = self.multi_stream_links[label]
        result = {key: multi[key] for key in ('source_to_dest_8streams_gbit',
                                              'source_to_dest_8streams_steady_gbit')}
        result['multi_stream'] = {'streams': multi['streams'], 'utc': multi['utc'], 'measured_here': measured,
                                  **({'test': multi['test']} if measured else {}),
                                  **({'error': multi['test']['error']} if 'error' in multi['test'] else {})}
        return result

    def qdisc_stats(self) -> dict:
        source, dest = self.both(
            lambda: self.local_agent('throttle', 'show', '--iface', self.dest_iface),
            lambda: self.source.agent('throttle', 'show', '--iface', self.source_iface))
        return {'source': source['qdisc'], 'dest': dest['qdisc']}

    # --- EXP-13 (T12): the emulated WAN, each visit's checks, the samplers and the TLS stand-in ---------------

    def wan(self, action: str, condition: dict | None = None) -> dict:
        """infra/gcp/wan.sh on both VMs (agent.py wan): apply a condition's tree, off, or show; each VM's peer is
        the other. {'source', 'dest'}."""
        def argv(iface, peer):
            if action == 'apply':
                return ('wan', 'apply', condition['tc_rate'], f'{condition["delay_ms"]:g}', '--peer', peer,
                        '--control-port', str(self.args.control_port), '--iface', iface)
            return ('wan', action, '--iface', iface)

        source, dest = self.both(lambda: self.local_agent(*argv(self.dest_iface, self.source.ip)),
                                 lambda: self.source.agent(*argv(self.source_iface, self.dest_ip)))
        return {'source': source, 'dest': dest}

    def tcp_settings(self) -> dict:
        """Both VMs' TCP settings now (measure.TCP_SYSCTLS)."""
        source, dest = self.both(lambda: self.local_agent('sysctl', 'get', *measure.TCP_SYSCTLS)['values'],
                                 lambda: self.source.agent('sysctl', 'get', *measure.TCP_SYSCTLS)['values'])
        return {'source': source, 'dest': dest}

    def wanted_tcp(self, host_setting: str) -> dict:
        """The tcp_rmem and tcp_wmem a host setting wants on each VM: its recorded defaults, maxima raised if tuned."""
        return {vm: measure.condition_tcp_settings(host_setting, self.tcp_defaults[vm], self.args.tuned_tcp_max_bytes)
                for vm in ('source', 'dest')}

    def set_tcp(self, host_setting: str) -> dict:
        """Both VMs' tcp_rmem and tcp_wmem set to a host setting's values (agent.py sysctl set, sudo)."""
        wanted = self.wanted_tcp(host_setting)

        def assignments(vm):
            return [f'{name}={value}' for name, value in wanted[vm].items()]

        source, dest = self.both(lambda: self.local_agent('sysctl', 'set', *assignments('dest')),
                                 lambda: self.source.agent('sysctl', 'set', *assignments('source')))
        return {'host_tcp': host_setting, 'source': source, 'dest': dest}

    def tcp_check(self, host_setting: str) -> dict:
        """Both VMs' TCP settings now against a host setting's: every setting in force, what was wanted, the
        differences, and whether there are none (ok)."""
        wanted, found = self.wanted_tcp(host_setting), self.tcp_settings()
        problems = {vm: measure.tcp_settings_problems(found[vm], wanted[vm]) for vm in found}
        return {**found, 'host_tcp': host_setting, 'wanted': wanted, 'problems': problems,
                'ok': not any(problems.values())}

    def tcp_guard(self, host_setting: str) -> dict:
        """The guard before every EXP-13 run: both VMs' tcp_rmem and tcp_wmem at the condition's values. Settings
        found otherwise are set once more (guard_reset) and read again; run_method refuses the run (A11 (ii): a
        failed guard) if they still differ."""
        result = self.tcp_check(host_setting)
        if not result['ok']:
            reset = {'found': result['problems']}
            try:
                reset['set'] = self.set_tcp(host_setting)
            except Exception as exc:
                reset['error'] = f'{type(exc).__name__}: {exc}'
            result = {**self.tcp_check(host_setting), 'guard_reset': reset}
            log(f"  the TCP settings were not the condition's ({reset['found']}); "
                + ('reset by the guard' if result['ok'] else 'the guard could not reset them'))
        return result

    def reset_tcp(self) -> dict:
        """Both VMs' tcp_rmem and tcp_wmem back to their recorded defaults, then checked (CommandError if not)."""
        written = self.set_tcp('default')
        check = self.tcp_check('default')
        if not check['ok']:
            raise CommandError(f'the TCP settings did not return to their defaults: {check["problems"]}')
        return {**written, 'check': check}

    def network_off(self) -> dict:
        """The tree off and the recorded TCP defaults back, on both VMs: outside every visit, at the end of the
        repeats, and in every finally. Both are tried whatever the other did; CommandError if either failed."""
        result, errors = {}, []
        for name, step in (('wan', lambda: self.wan('off')),
                           ('tcp', lambda: self.reset_tcp() if self.tcp_defaults is not None else None)):
            try:
                result[name] = step()
            except Exception as exc:
                result[name] = {'error': f'{type(exc).__name__}: {exc}'}
                errors.append(f'{name}: {exc}')
        if errors:
            raise CommandError('the network could not be reset: ' + '; '.join(errors))
        return result

    def tcp_metrics_flush(self) -> dict:
        """ip tcp_metrics flush all on both VMs, before every run and every link test: no connection then starts
        from a slow-start threshold Linux cached from the last one to the same host."""
        source, dest = self.both(lambda: self.local_agent('tcp-metrics-flush'),
                                 lambda: self.source.agent('tcp-metrics-flush'))
        return {'source': source, 'dest': dest}

    def ping_check(self) -> dict:
        """measure.PING_COUNT pings from dest to the source, PING_INTERVAL_S apart, as root: the delayed band both ways."""
        return self.local_agent('ping-check', self.source.ip, '--count', str(measure.PING_COUNT), '--interval',
                                f'{measure.PING_INTERVAL_S:g}', timeout=120)

    def control_noops(self, count: int = measure.CONTROL_NOOPS) -> list[float]:
        """`count` no-ops over the control path (`true` through the multiplexed SSH connection): each one's seconds."""
        seconds = []
        for _ in range(count):
            started = time.perf_counter()
            subprocess.run(self.source.argv(['true']), check=True, capture_output=True, timeout=60,
                           stdin=subprocess.DEVNULL)
            seconds.append(round(time.perf_counter() - started, 6))
        return seconds

    def band_counters(self) -> dict:
        """Both VMs' packets so far in the plain band and in the delayed one (netem's), from one tc reading each:
        what the visit records around its pings and its control no-ops (smoke item 1)."""
        shown = self.wan('show')
        return {vm: {'plain': (shown[vm]['tree'].get('plain') or {}).get('packets'),
                     'delayed': (shown[vm]['tree'].get('netem') or {}).get('packets')} for vm in shown}

    def visit_checks(self, condition: dict) -> dict:
        """The checks at every visit, before any run: the tree on both VMs (measure.wan_problems: the filters,
        netem with r/2 and its limit, the tbf with its rate and queue), 20 pings (minimum in [r, r + 1 ms], median
        at most minimum + 2 ms), 5 control no-ops each within 5 ms of the anchor's, and both VMs' TCP settings. The
        bands' packet counters are read around the pings and the no-ops."""
        peers = {'source': self.dest_ip, 'dest': self.source.ip}
        shown = self.wan('show')
        tree = {}
        for vm, peer in peers.items():
            expected = measure.wan_expected(condition, peer, self.args.control_port)
            tree[vm] = {'tree': shown[vm]['tree'], 'filters': shown[vm]['filters'], 'expected': expected,
                        'problems': measure.wan_problems(shown[vm]['tree'], shown[vm]['filters'], expected)}
        bands = {'before_pings': {vm: {'plain': (shown[vm]['tree'].get('plain') or {}).get('packets'),
                                       'delayed': (shown[vm]['tree'].get('netem') or {}).get('packets')}
                                  for vm in shown}}
        pings = self.ping_check()
        bands['after_pings'] = self.band_counters()
        noops = self.control_noops()
        bands['after_noops'] = self.band_counters()
        tcp = self.tcp_check(condition['host_tcp'])
        problems = {'tree': [f'{vm}: {problem}' for vm in tree for problem in tree[vm]['problems']],
                    'pings': measure.ping_problems(pings, condition['rtt_ms']),
                    'control_noops': measure.control_noop_problems(noops, self.control_reference),
                    'sysctl': [f'{vm}: {problem}' for vm, found in tcp['problems'].items() for problem in found]}
        return {'ok': not any(problems.values()), 'problems': problems, 'tree': tree, 'pings': pings,
                'control_noops': {'seconds': noops, 'reference': self.control_reference}, 'sysctl': tcp,
                'bands': bands}

    def set_condition(self, condition: dict, trial: dict, repeat: int) -> dict:
        """One visit (EXP-13's step 4): the tree and the TCP settings on both VMs, the visit's checks (A11 (i): if
        they fail, the tree and the settings are reset and set and checked once more, before any run; a second
        failure stops the trial as an infrastructure fault), then the one-stream link test from source to dest, and
        at the trial's first visit to the condition its first-visit probes. The visit goes to the trial record's
        `visits` (and `first_visit`); self.condition holds what each run's record carries of it. Returns the link
        dict the runs' records keep (the rates under keys ending in gbit)."""
        label = condition['label']
        log(f'condition {label}: tc {condition["tc_rate"]}, netem {condition["delay_ms"]:g} ms on each VM, '
            f'{condition["host_tcp"]} TCP settings')
        attempts = []
        for attempt in range(2):
            entry: dict = {}
            attempts.append(entry)
            if attempt:
                entry['reset'] = self.probe('the network reset', self.network_off)
            entry['applied'] = {'wan': self.wan('apply', condition), 'tcp': self.set_tcp(condition['host_tcp'])}
            entry['checks'] = checks = self.visit_checks(condition)
            if checks['ok']:
                break
            log(f'  the visit to {label} failed its checks: {checks["problems"]}')
        visit = {'condition': label, 'repeat': repeat, 'utc': utc_now(), 'attempts': attempts,
                 'tree': checks['tree'], 'pings': checks['pings'], 'control_noops': checks['control_noops'],
                 'sysctl': checks['sysctl'], 'bands': checks['bands'], 'ok': checks['ok']}
        trial.setdefault('visits', []).append(visit)
        if not checks['ok']:
            raise InfrastructureFault(f'the visit to {label} failed its checks twice (A11 (i)): {checks["problems"]}')
        one = self.link_test('source_to_dest', 1)
        visit['link_tests'] = [one]
        visit['bands']['after_link_test'] = self.band_counters()
        first = trial.setdefault('first_visit', {})
        if label not in self.visited:
            self.visited.add(label)
            first[label] = self.first_visit_probes(condition, trial)
        if condition['rtt_ms'] == 0 and condition['host_tcp'] == 'default':
            # The zero-delay rates at this tbf rate (the crossovers' x; A9's comparison), unless a probe gave them.
            zero = self.zero_delay.setdefault(condition['gbit'], {})
            zero.setdefault('one_stream', one['steady_gbit'])
            if 'bound' not in zero:
                zero['bound'] = next((test.get('steady_gbit') for test in (first.get(label) or {}).get('link_tests', ())
                                      if test.get('streams') == LINK_TEST_STREAMS
                                      and test.get('direction') == 'source_to_dest'), None)
        ping_min = checks['pings'].get('min_ms')
        steady = one['steady_gbit']
        zero = self.zero_delay.get(condition['gbit'], {})
        self.condition = {
            **{key: condition[key] for key in ('label', 'gbit', 'rtt_ms', 'delay_ms', 'host_tcp')},
            'visit_index': len(trial['visits']) - 1, 'ping_min_ms': ping_min, 'visit_one_stream_gbit': steady,
            'zero_delay_gbit': zero.get('one_stream'), 'zero_delay_bound_gbit': zero.get('bound'),
            # pgdelta's busy threshold: what the path holds without queueing, the visit's one-stream rate times
            # its ping minimum (passed to each pgdelta sender as --busy-inflight-bytes where it takes it).
            'busy_inflight_bytes': (None if steady is None or ping_min is None
                                    else int(steady * 1e9 / 8 * ping_min / 1000))}
        visit['busy_inflight_bytes'] = self.condition['busy_inflight_bytes']
        link = {'condition': label, 'visit_index': self.condition['visit_index'],
                'source_to_dest_gbit': one['gbit'], 'source_to_dest_steady_gbit': steady,
                'zero_delay_gbit': zero.get('one_stream'), 'zero_delay_bound_gbit': zero.get('bound')}
        for test in (first.get(label) or {}).get('link_tests', ()):
            if test.get('error'):
                continue
            if test['direction'] == 'dest_to_source':
                link.update(dest_to_source_gbit=test['gbit'], dest_to_source_steady_gbit=test['steady_gbit'])
            elif test['streams'] > 1:
                link.update({f'source_to_dest_{test["streams"]}streams_gbit': test['gbit'],
                             f'source_to_dest_{test["streams"]}streams_steady_gbit': test['steady_gbit']})
        self.current_speed = condition
        return link

    def link_test(self, direction: str, streams: int, *, qdisc: bool = False, listen: str | None = None,
                  connect: str | None = None, name: str | None = None) -> dict:
        """One EXP-13 link test, after a TCP metrics flush: its rate, the per-second rates and the steady rate (the
        median of the last 3 after the warmup), each stream's connect time; with qdisc, both VMs' tree counters
        before and after (netem's and the tbf's drops from the same snapshots) and the tc sampler's largest
        netem backlog meanwhile (smoke item 5)."""
        flush = self.tcp_metrics_flush()
        samplers = self.start_net_samplers(name or f'link-{direction}-{streams}', ss=False) if qdisc else None
        before = self.wan('show') if qdisc else None
        try:
            test = self.blast(direction, streams, listen=listen, connect=connect)
        finally:
            sampled = self.stop_net_samplers(samplers) if samplers else None
        receiver, sender = test['receiver'], test['sender']
        result = {'direction': direction, 'streams': streams, 'utc': utc_now(), 'tcp_metrics': flush,
                  'gbit': receiver.get('gbit'),
                  'steady_gbit': receiver.get('steady_last3_gbit', receiver.get('steady_gbit')),
                  'per_second_gbit': receiver.get('per_second_gbit'), 'connect_s': sender.get('connect_s'),
                  'per_stream_connect_s': sender.get('per_stream_connect_s'), 'test': test}
        if qdisc:
            after = self.wan('show')
            result['qdisc'] = {}
            for vm in ('source', 'dest'):
                first, last = (measure.qdisc_counters(shown[vm]['tree']) for shown in (before, after))
                result['qdisc'][vm] = {'before': first, 'after': last, 'delta': measure.qdisc_delta(first, last),
                                       'samples': (sampled or {}).get(vm, {}).get('qdisc')}
        return result

    def first_visit_probes(self, condition: dict, trial: dict) -> dict:
        """At a trial's first visit to a condition: one stream from dest to source, 8 streams from source to dest
        (and any --first-visit-link-streams), each with the tree's counters; the SSH probe at r > 0 (with
        --ssh-probe-port); the stunnel link test where --stunnel-link-test names the condition. Each is a probe: its
        error is recorded and the visit goes on."""
        label, slug = condition['label'], condition['slug']
        planned = [('dest_to_source', 1, False)] + [
            ('source_to_dest', streams, True)
            for streams in (LINK_TEST_STREAMS, *self.t12_plan['first_visit_streams'].get(label, ()))]
        tests = []
        for direction, streams, qdisc in planned:
            test = self.probe(f'the {streams}-stream link test ({direction}) at {label}',
                              lambda: self.link_test(direction, streams, qdisc=qdisc,
                                                     name=f'{slug}-{direction}-{streams}'))
            tests.append({'direction': direction, 'streams': streams, **test})
        first = {'condition': label, 'utc': utc_now(), 'link_tests': tests}
        if getattr(self.args, 'ssh_probe_port', None) and condition['rtt_ms'] > 0:
            first['ssh_probe'] = self.probe(f'the SSH probe at {label}', self.ssh_probe)
        if label in self.t12_plan['stunnel_link_test']:
            first['stunnel_link_test'] = self.probe(f'the stunnel link test at {label}',
                                                    lambda: self.stunnel_link_test(slug))
        return first

    def zero_delay_tests(self) -> dict:
        """EXP-13's step 3: at each --zero-delay-link-rates rate, one stream from source to dest through the tree at
        r = 0 on default hosts; its steady rate is the crossovers' x there (and A9's zero-delay rate). The tree is
        off again after."""
        results = {}
        try:
            for gbit in self.t12_plan['zero_delay_rates']:
                condition = measure.parse_condition(f'{gbit:g}:0:default')
                self.wan('apply', condition)
                test = self.probe(f'the zero-delay link test at {gbit:g} Gbit/s',
                                  lambda: self.link_test('source_to_dest', 1, name=f'{condition["slug"]}-zero-delay'))
                results[condition['label']] = test
                if test.get('steady_gbit') is not None:
                    self.zero_delay[condition['gbit']] = {'one_stream': test['steady_gbit'],
                                                          'bound': test['steady_gbit'], 'from': 'probe'}
        finally:
            self.wan('off')
        return results

    def ssh_probe(self) -> dict:
        """The SSH probe (agent.py ssh-probe on dest, after a TCP metrics flush): one channel from the source's
        sshd on --ssh-probe-port (22, delayed), cat /dev/zero for the link test's seconds; its rate over the last
        seconds after the warmup."""
        flush = self.tcp_metrics_flush()
        warmup = getattr(self.args, 'link_test_warmup', None)
        probe = self.local_agent('ssh-probe', '--host', self.source.ip, '--user', self.args.ssh_user, '--key',
                                 self.args.ssh_key, '--port', str(self.args.ssh_probe_port), '--seconds',
                                 f'{self.args.link_test_seconds:g}', '--warmup', f'{3 if warmup is None else warmup:g}',
                                 timeout=300)
        return {**probe, 'tcp_metrics': flush}

    def start_net_samplers(self, name: str, ss: bool = True) -> dict:
        """The network samplers, started before a clock: on the source, `ss -tin` of its sockets to dest (the
        control port left out; with ss) and the tree's qdisc counters; on dest, its qdisc counters. Every 0.5 s."""
        out = {'source': self.source_log(f'net-sampler-{name}'), 'dest': str(self.dest_log(f'net-sampler-{name}'))}
        ss_args = ['--peer', self.dest_ip, '--exclude-port', str(self.args.control_port)] if ss else []
        source, dest = self.both(
            lambda: self.local_agent('net-sampler', 'start', '--out', out['dest'], '--qdisc-iface', self.dest_iface),
            lambda: self.source.agent('net-sampler', 'start', '--out', out['source'], *ss_args, '--qdisc-iface',
                                      self.source_iface))
        return {'source': source, 'dest': dest, 'out': out}

    def stop_net_samplers(self, started: dict) -> dict:
        """The samplers stopped after the clock, and what they read (agent.py net-sampler stop's summary)."""
        source, dest = self.both(lambda: self.local_agent('net-sampler', 'stop', '--out', started['out']['dest']),
                                 lambda: self.source.agent('net-sampler', 'stop', '--out', started['out']['source']))
        return {'source': source, 'dest': dest}

    @property
    def tls_dir(self) -> str:
        """The TLS material's directory, the same path on both VMs (never in the results)."""
        return f'{self.work}/tls'

    def setup_tls(self) -> dict:
        """The session's TLS material, made on dest (agent.py tls-material), and the client's certificate and key,
        the CA's certificate and the wrong CA's client pair copied to the source over the control path (keys
        0600). It never leaves the VMs: the record keeps the certificates' subjects and fingerprints only."""
        material = self.local_agent('tls-material', '--dir', self.tls_dir)
        names = ('ca.crt', 'client.crt', 'client.key', 'wrong-client.crt', 'wrong-client.key')
        for name in names:
            self.source.write(f'{self.tls_dir}/{name}', Path(material['files'][name]).read_text())
        self.source.agent('run', '--log', f'{self.work}/logs/tls-chmod.log', '--', 'chmod', '600',
                          f'{self.tls_dir}/client.key', f'{self.tls_dir}/wrong-client.key')
        return {'dir': self.tls_dir, 'certificates': material['certificates'], 'copied_to_source': list(names)}

    def stunnel_configs(self, server_log, client_log) -> dict:
        """The pair's configurations (measure.stunnel_config): dest's server on its address's STUNNEL_SERVER_PORT,
        forwarding to the receiver on localhost; the source's client on localhost's STUNNEL_CLIENT_PORT, opening
        TLS 1.3 to dest's."""
        tls = self.tls_dir
        return {'server': measure.stunnel_config(
                    'server', accept=f'{self.dest_ip}:{measure.STUNNEL_SERVER_PORT}', connect=f'127.0.0.1:{PGDELTA_PORT}',
                    cert=f'{tls}/server.crt', key=f'{tls}/server.key', ca=f'{tls}/ca.crt', log=server_log),
                'client': measure.stunnel_config(
                    'client', accept=f'127.0.0.1:{measure.STUNNEL_CLIENT_PORT}',
                    connect=f'{self.dest_ip}:{measure.STUNNEL_SERVER_PORT}', cert=f'{tls}/client.crt',
                    key=f'{tls}/client.key', ca=f'{tls}/ca.crt', log=client_log)}

    def start_stunnels(self, tag: str, server_only: bool = False) -> dict:
        """The stunnel pair started before a clock, each with its configuration and log beside the run's logs; under
        strace -e trace=setsockopt with --stunnel-strace (smoke item 12)."""
        logs = {'server': str(self.dest_log(f'{tag}stunnel-server.log')),
                'client': self.source_log(f'{tag}stunnel-client.log')}
        configs = self.stunnel_configs(logs['server'], logs['client'])
        paths = {'server': str(self.dest_log(f'{tag}stunnel-server.conf')),
                 'client': self.source_log(f'{tag}stunnel-client.conf')}
        Path(paths['server']).parent.mkdir(parents=True, exist_ok=True)
        Path(paths['server']).write_text(configs['server'])
        started = {'configs': configs, 'logs': logs, 'tag': tag}

        def start(name, port):
            argv = ['stunnel', 'start', '--name', name, '--dir', self.tls_dir, '--config', paths[name], '--port',
                    str(port), '--log', logs[name], '--stunnel', self.args.stunnel]
            if self.args.stunnel_strace:
                argv += ['--strace-log', logs[name].replace('.log', '.strace')]
            return argv

        started['server'] = self.local_agent(*start('server', measure.STUNNEL_SERVER_PORT))
        if not server_only:
            try:
                self.source.write(paths['client'], configs['client'])
                started['client'] = self.source.agent(*start('client', measure.STUNNEL_CLIENT_PORT))
            except BaseException:
                self.stop_stunnels(started)
                raise
        return started

    def stop_stunnels(self, started: dict) -> dict:
        """The pair stopped after the clock: each one's log read (connections, protocol, suite) and its own CPU.
        The TLS connection's protocol and suite are dest's (the server's) log's."""
        result: dict = {'configs': started['configs'], 'logs': started['logs']}
        errors = []
        for name, stop in (('server', lambda: self.local_agent('stunnel', 'stop', '--name', 'server', '--dir',
                                                               self.tls_dir, '--log', started['logs']['server'])),
                           ('client', lambda: self.source.agent('stunnel', 'stop', '--name', 'client', '--dir',
                                                                self.tls_dir, '--log', started['logs']['client']))):
            if name not in started:
                continue
            try:
                result[name] = stop()
            except Exception as exc:
                result[name] = {'error': f'{type(exc).__name__}: {exc}'}
                errors.append(name)
        server = (result.get('server') or {}).get('log') or {}
        result.update(tls_version=server.get('tls_version'), tls_suite=server.get('tls_suite'),
                      connections=server.get('connections'), stop_errors=errors)
        return result

    def stunnel_link_test(self, slug: str) -> dict:
        """A one-stream link test through the stunnel pair (smoke item 12): the receiver on dest's localhost behind
        its stunnel, the sender into the source's stunnel on localhost."""
        started = self.start_stunnels(f'{slug}-link-test-')
        try:
            test = self.link_test('source_to_dest', 1, listen=f'127.0.0.1:{PGDELTA_PORT}',
                                  connect=f'127.0.0.1:{measure.STUNNEL_CLIENT_PORT}')
        finally:
            stopped = self.stop_stunnels(started)
        return {**test, 'stunnel': stopped}

    def tls_refusals(self) -> dict:
        """Smoke item 12's refusals: dest's stunnel alone, then from the source openssl s_client with no client
        certificate, and with one from a wrong CA. Each attempt's exit and output, and dest's stunnel's log: its
        refusal lines, and the connections it forwarded to the receiver's port (there should be none)."""
        results = {}
        ca = f'{self.tls_dir}/ca.crt'
        for case, extra in (('no_certificate', ''),
                            ('wrong_ca', f' -cert {self.tls_dir}/wrong-client.crt -key {self.tls_dir}/wrong-client.key')):
            started = self.start_stunnels(f'refusal-{case}-', server_only=True)
            try:
                attempt = self.source.agent(
                    'run', '--log', f'{self.work}/logs/tls-refusal-{case}.log', '--', 'sh', '-c',
                    f'echo x | timeout 20 openssl s_client -connect {self.dest_ip}:{measure.STUNNEL_SERVER_PORT} '
                    f'-tls1_3 -CAfile {ca}{extra} -quiet', timeout=120)
            finally:
                stopped = self.stop_stunnels(started)
            log_text = (stopped.get('server') or {}).get('log_tail') or ''
            results[case] = {'client_exit_code': attempt.get('exit_code'),
                             'client_tail': (attempt.get('log_tail') or '')[-2000:],
                             'refusals': ((stopped.get('server') or {}).get('log') or {}).get('refusals'),
                             'forwarded': [line for line in log_text.splitlines()
                                           if f':{PGDELTA_PORT}' in line and 'connected' in line]}
            results[case]['refused'] = bool(results[case]['refusals']) and not results[case]['forwarded']
        return results

    def processes_snapshot(self) -> dict:
        """A `ps` of both VMs (probackup8 at EXP-13: its SSH connections, mid-run), the lines of SSH, pg_probackup
        and walsenders."""
        argv = ['ps', '-eo', 'pid,ppid,etimes,comm,args', '--sort=pid']
        keep = ('ssh', 'probackup', 'walsender', 'psql')
        dest = subprocess.run(argv, capture_output=True, text=True, timeout=60).stdout
        source = self.source.agent('run', '--log', self.source_log('processes.log'), '--', *argv)['log_tail']
        return {'utc': time.time(), 'argv': argv,
                **{vm: [line for line in text.splitlines() if any(word in line for word in keep)]
                   for vm, text in (('source', source), ('dest', dest))}}

    def t12_after_clock(self, record: dict, daemon_holder: dict) -> None:
        """After an EXP-13 run's clock: the samplers stopped and their readings in the record (`ss`: the source's
        sockets to dest, per connection in ss_connections; `qdisc.samples`: netem's largest sampled backlog on each
        VM), and v1_tls's stunnel pair stopped, its logs read (`stunnel`: the connection, the protocol and suite).
        Each once, whichever path gets here first (run_method's, or its finally after a failure)."""
        samplers, stunnel = daemon_holder.pop('net_samplers', None), daemon_holder.pop('stunnel', None)
        errors = []
        if samplers is not None:
            try:
                stopped = self.stop_net_samplers(samplers)
                record['ss'] = stopped['source'].get('ss')
                record['ss_connections'] = stopped['source'].get('ss_connections')
                record.setdefault('qdisc', {})['samples'] = {vm: stopped[vm].get('qdisc') for vm in ('source', 'dest')}
                record['net_samplers'] = {vm: {key: stopped[vm].get(key) for key in ('pid', 'final', 'samples')}
                                          for vm in ('source', 'dest')}
            except Exception as exc:
                errors.append(f'the samplers: {type(exc).__name__}: {exc}')
        if stunnel is not None:
            try:
                record['stunnel'] = self.stop_stunnels(stunnel)
            except Exception as exc:
                errors.append(f'the stunnel pair: {type(exc).__name__}: {exc}')
        if errors:
            raise CommandError('; '.join(errors))

    def t12_checks(self, record: dict, method: str, verification: dict) -> None:
        """An EXP-13 run's own checks, after its verification: the receiver's wall (its launch to its exit; the drain
        is that less publish_s) for pgdelta's arms; for v1_tls, stunnel's log showing the connection, TLS 1.3 and its
        suite, or the run fails verification; and A11 (ii)'s network checks (measure.network_checks)."""
        if method in measure.PGDELTA_METHODS:
            receiver = next((command for phase in (record.get('phases') or {}).values()
                             for command in phase.get('commands', ())
                             if command.get('host') == 'dest' and 'receiver' in str(command.get('role', ''))), None)
            record['receiver_wall_s'] = None if receiver is None else receiver.get('wall_seconds')
        if method in measure.TLS_METHODS:
            tls = record.get('stunnel') or {}
            ok = (tls.get('tls_version') == 'TLSv1.3' and bool(tls.get('tls_suite'))
                  and (tls.get('connections') or 0) >= 1)
            verification['tls'] = {'tls_version': tls.get('tls_version'), 'tls_suite': tls.get('tls_suite'),
                                   'connections': tls.get('connections'), 'ok': ok}
            verification['verified'] = bool(verification.get('verified')) and ok
        if method in measure.PROBACKUP_METHODS:
            record['processes'] = next((phase['processes'] for phase in (record.get('phases') or {}).values()
                                        if phase.get('processes')), None)
        record['network_checks'] = measure.network_checks(record)

    def t12_methods_at(self, condition: dict, repeat: int) -> list[str]:
        """The arms that run at a condition in a repeat (counted from 0 here, from 1 in the options)."""
        return measure.t12_methods_at(self.methods, condition['label'], repeat + 1, self.t12_plan)

    def user_table_files(self) -> dict:
        """A15's user tables' files (measure.USER_TABLE_FILES_SQL, from the catalog: no user table is read)."""
        answer = self.query(measure.USER_TABLE_FILES_SQL, host=self.source.ip, port=PG_PORT,
                            log_name='user-table-files.log')
        rows = json.loads(answer or '[]')
        return {'relations': rows, 'paths': sorted({row['path'] for row in rows})}

    # --- methods -----------------------------------------------------------

    def basebackup(self, output, *, host, basis=None, extra=()) -> list[str]:
        return basebackup_command(self.bin / 'pg_basebackup', output, host=host, port=PG_PORT,
                                  user=PG_USER, basis=basis, checksum=self.args.manifest_checksums,
                                  extra=extra)

    def capture_argv(self) -> list[str]:
        """The staged capture: a local pg_basebackup of S on the source. Under EXP-12's load with --verbose, its
        lines stamped as they arrive over SSH (G4's capture end: its write-ahead log end point)."""
        return self.basebackup(self.source_capture, host=f'{self.source_root}/s',
                               extra=measure.BASEBACKUP_VERBOSE if self.t10a else ())

    def capture_commands(self, method: str) -> list[dict]:
        """Local pg_basebackup S on the source (the staged methods' capture).

        The log is named after the method, so the staged methods at one
        (repeat, speed) keep their own capture logs and GNU time reports.
        """
        if self.t10a:
            return [self.remote_stamped_command(self.capture_argv(), f'{method}-capture.log')]
        return [self.remote_command(self.capture_argv(), f'{method}-capture.log')]

    def method_paths(self, method: str) -> dict:
        # For the in-place methods and pgBackRest, 'output' is B: the copy of B0 they patch.
        paths = {'output': self.outputs / method}
        if method in measure.INCREMENTAL_METHODS:
            paths['increment'] = self.outputs / f'{method}-increment'
        if method in measure.STAGED_METHODS:
            paths['capture'] = self.source_capture
        if method in measure.PGDELTA_DIRECT_METHODS:
            # Read in place; never removed. EXP-12's proxies read the snapshot, the live source running.
            paths['source_data'] = self.snapshot_data if method in measure.V1PROXY_METHODS else self.source_data
        if method == 'capture':
            paths['capture'] = self.source_capture  # EXP-12's capture alone: S, verified and removed later
        if method in measure.PGDELTA_METHODS:
            # In place, the receipt is beside B, which is this same path.
            paths['receipt'] = self.outputs / f'{method}.pgdelta-receipt.json'
            paths['stats'] = f'{self.work}/stats/{method}-send.json'
        if method in measure.PARCOPY_METHODS:
            # T2's bound reads the stopped source in place, as pgdelta_direct* does. Its sender's
            # --stats-json is on the source (stats), its receiver's on dest (receive_stats).
            paths['source_data'] = self.source_data
            paths['stats'] = f'{self.work}/stats/{method}-send.json'
            paths['receive_stats'] = self.work / 'stats' / f'{method}-receive.json'
        if method in measure.PROBACKUP_METHODS:
            # pg_probackup on dest reads the live source's data directory over SSH, with its own SSH
            # configuration (setup_probackup_ssh writes it) and file logs, on dest.
            probackup = measure.probackup_paths(self.work)
            paths.update(source_data=self.source_data, ssh_config=probackup['ssh_config'],
                         probackup_log_dir=probackup['log_dir'])
        return paths

    def clean_method(self, paths: dict) -> None:
        stuck = (self.output_stuck or {}).get('data')
        self.remove_dest(*[paths[key] for key in ('output', 'increment', 'receipt', 'receive_stats')
                           if key in paths and str(paths[key]) != stuck])
        remote = [paths[key] for key in ('capture', 'stats') if key in paths]
        if remote:
            self.remove_source(*remote)

    def full_argv(self, method, paths) -> list[str]:
        """A pg_basebackup arm's command line: the full copy, then its options (measure.FULL_VARIANTS)."""
        return self.basebackup(paths['output'], host=self.source.ip, extra=measure.FULL_VARIANTS[method])

    def run_full(self, method, paths) -> dict:
        """A pg_basebackup arm (full and T2's variants): one timed phase, one counter window.

        pg_basebackup's flush (or, with --no-sync, its absence) is inside it. Its --verbose lines
        come through a pipe that a thread of this process timestamps as they arrive (Child
        stamp_lines), untranslated, so basebackup_phases can split the run.
        """
        argv = self.full_argv(method, paths)
        return {measure.FULL_PHASE: self.timed_phase(measure.FULL_PHASE, lambda: [self.local_command(
            argv, f'{method}.log', env=messages_env(), stamp_lines=True)])}

    # --- T2's other copies: their blocks fill these in ---------------------------

    def probackup_argv(self, method, paths) -> list[str]:
        """pg_probackup catchup on dest for probackup8 and probackup4 (measure.probackup_catchup_argv)."""
        return measure.probackup_catchup_argv(
            self.args.pg_probackup, method, source_pgdata=paths['source_data'], dest_pgdata=paths['output'],
            source_ip=self.source.ip, port=PG_PORT, user=PG_USER, database=PG_DATABASE,
            ssh_user=self.args.ssh_user, ssh_config=paths['ssh_config'])

    def probackup_sync_argv(self, paths) -> list[str]:
        """The timed flush after pg_probackup catchup: syncfs(2) of the output's filesystem."""
        return ['sync', '-f', str(paths['output'])]

    def wal_ticker_argv(self) -> list:
        """psql on dest, connected to the source, emitting one empty WAL message per tick.

        pg_logical_emit_message(false, ..., flush => true) writes and flushes a non-transactional WAL
        record and nothing else: no table, no data file and no transaction status changes (EXP-11's smoke
        run, October 24). It keeps WAL arriving at pg_probackup's stream, as on a live primary.

        EXP-13 runs it on the source instead, over its Unix socket, launched over the control path, so its pace
        doesn't change with the round trip: psql reads the script on the source, and stops when the SSH channel's
        stdin closes (the shell around it kills it then).
        """
        if self.t12:
            psql = [str(part) for part in self.psql_argv(f'{self.source_root}/s', PG_PORT, 'SELECT 1')[:-2]]
            return self.source.argv(['sh', '-c', '"$@" -f "$0" & p=$!; read x; kill $p; wait $p',
                                     self.wal_ticker_script, *psql])
        return [str(part) for part in self.psql_argv(self.source.ip, PG_PORT, 'SELECT 1')[:-2]]

    @property
    def wal_ticker_script(self) -> str:
        """EXP-13: the WAL ticker's script on the source."""
        return f'{self.work}/wal-ticker.sql'

    def start_wal_ticker(self, method):
        """probackup*: the WAL ticker, started untimed before the phase, or None when it is off."""
        interval = self.args.probackup_wal_ticker
        if not interval:
            return None
        log_path = self.dest_log(f'{method}-wal-ticker.log')
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open('w')
        script = (f"SELECT pg_logical_emit_message(false, 'pgd-tick', '', true) \\watch {interval}\n")
        if self.t12:
            self.source.write(self.wal_ticker_script, script)
        process = subprocess.Popen(self.wal_ticker_argv(), stdin=subprocess.PIPE, stdout=handle,
                                   stderr=subprocess.STDOUT, text=True, env=server_env())
        if not self.t12:
            process.stdin.write(script)
            process.stdin.flush()
        return {'process': process, 'log': handle, 'interval_s': interval, 'started': time.perf_counter(),
                'on': 'source' if self.t12 else 'dest'}

    def stop_wal_ticker(self, ticker) -> dict | None:
        """Stops the ticker after the phase; its record (interval, seconds, exit). A ticker that died early
        is reported, not fatal: the run then shows what pg_probackup does on an idle source. On the source
        (EXP-13) the SSH channel's stdin is closed first, which stops it there."""
        if ticker is None:
            return None
        process = ticker['process']
        early = process.poll()
        if early is None and ticker.get('on') == 'source':
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        if early is None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        ticker['log'].close()
        return {'interval_s': ticker['interval_s'], 'seconds': round(time.perf_counter() - ticker['started'], 3),
                'exited_early': early is not None, 'exit_code': early}

    def run_probackup(self, method, paths) -> dict:
        """probackup*: one timed phase (measure.PROBACKUP_PHASE), one counter window, two steps.

        catchup: probackup_argv on dest, reading the live source over SSH with its own configuration
        (setup_probackup_ssh), its console lines timestamped as they arrive, untranslated
        (measure.probackup_phases). sync: at once, sync -f of the output, syncfs(2) of dest's /data.
        catchup 2.5.16 fsyncs only the source's file list and pg_control, so the streamed WAL, backup_label
        and every directory are flushed by this step (EXP-11: "tool + sync -f"; probackup_sync_s). A
        catchup that failed gets no sync. Each step's wall and start offset are in `steps`.
        """
        steps: dict = {}
        catchup_step, sync_step = measure.PROBACKUP_STEPS

        def body():
            origin = time.perf_counter()
            catchup = self.local_command(self.probackup_argv(method, paths), f'{method}.log', env=messages_env(),
                                         stamp_lines=True)
            steps[catchup_step] = {'host': 'dest', 'offset_seconds': 0.0,
                                   'wall_seconds': round(time.perf_counter() - origin, 3)}
            if catchup['exit_code'] != 0:
                return [catchup]
            started = time.perf_counter()
            sync = self.local_command(self.probackup_sync_argv(paths), f'{method}-sync.log')
            steps[sync_step] = {'host': 'dest', 'offset_seconds': round(started - origin, 3),
                                'wall_seconds': round(time.perf_counter() - started, 3)}
            return [catchup, sync]

        # EXP-13 (smoke item 16): a ps of both VMs mid-run, PROCESSES_SNAPSHOT_S into the clock: its SSH connections.
        processes: dict = {}
        timer = None
        if self.t12:
            def take():
                try:
                    processes.update(self.processes_snapshot())
                except Exception as exc:
                    processes['error'] = f'{type(exc).__name__}: {exc}'

            timer = threading.Timer(PROCESSES_SNAPSHOT_S, take)
            timer.daemon = True
        # The WAL ticker (--probackup-wal-ticker; added after EXP-11's smoke run): on an idle source,
        # pg_probackup's WAL stream notices the stop LSN only at its next timer tick, about 10 s after the
        # stream started, so an idle source alone added up to 8 s to every catchup. Started before the clock.
        ticker = self.start_wal_ticker(method)
        try:
            if timer is not None:
                timer.start()
            phase = self.timed_phase(measure.PROBACKUP_PHASE, body)
        finally:
            if timer is not None:
                timer.cancel()
                timer.join(60)
            ticked = self.stop_wal_ticker(ticker)
        phase['steps'] = steps
        phase['wal_ticker'] = ticked
        if timer is not None:
            phase['processes'] = processes or None
        return {measure.PROBACKUP_PHASE: phase}

    def verify_probackup(self, method, paths) -> dict:
        """probackup*: the output's bytes counted, the byte diagnostic, the start; its digest pending.

        Untimed. First, with the output as catchup and sync -f left it: its bytes (durability check (b)),
        its WAL, and the byte diagnostic against the trial's start reference digest
        (probackup_byte_diagnostic). Then the output is started on dest as a full copy is, recovering
        from its backup_label with the WAL it streamed: no archive, no standby.signal, no network, so no
        WAL is fetched after the clock stopped (the server log's restored files are counted, and any
        fails it). Its
        per-table digest is taken then, and compared with the source's at the trial's step 8
        (finish_logical_checks, logical_match None until then): an output that doesn't start fails at once.
        """
        output = Path(paths['output'])
        result = {'output_bytes': tree_bytes(output), 'wal': measure.wal_summary(measure.wal_files(output), None),
                  'shared_extents': measure.shared_extents(output),
                  'recovery_signals': [name for name in ('standby.signal', 'recovery.signal')
                                       if (output / name).exists()]}
        result['byte_diagnostic'] = self.probackup_byte_diagnostic(method, output)
        result['bytes_identical'] = (result['byte_diagnostic'] or {}).get('identical')
        result['output_start'] = self.check_output(method, output, recovery=True,
                                                   query=measure.logical_digest_sql())
        try:
            server_log = self.dest_log(f'{method}-output-server.log').read_text(errors='replace')
        except OSError:
            server_log = ''
        result['wal_files_restored'] = measure.restored_wal_files(server_log)
        started = result['output_start']['ok']
        if started:
            result['logical'] = json.loads(result['output_start']['answer'])
        result['logical_match'] = None
        # Only a logical difference can be void, and only at the trial's end; a copy that doesn't start fails.
        result['void'] = None if started else False
        result['logical_check'] = ("compared with the source's digest at the trial's step 8, after the end "
                                   'reference digest; void if the source changed during the trial')
        result['basis_changes'] = measure.stat_changes(self.b0_stat, measure.tree_stat(self.b0))
        # It must reach consistency from what the tool wrote: WAL restored at the start fails it.
        result['verified'] = started and not result['basis_changes'] and not result['wal_files_restored']
        return result

    def probackup_byte_diagnostic(self, method, output: Path) -> dict | None:
        """EXP-11's byte diagnostic of a probackup* output, before its start; it decides nothing.

        Every file the trial's start reference digest selects under base/ and global/ (global/pg_control
        left out) against the output's, by SHA-256, and the output's extra files listed
        (measure.byte_diagnostic). The output's view is kept for the trial, so finish_logical_checks can
        compare it with the end reference too if the source changed. --probackup-byte-diagnostic
        first_repeat (EXP-11's first cut) leaves it to each trial's first repeat; None without a reference.
        """
        reference = self.reference_digests.get('start')
        if reference is None:
            return None
        repeat, speed = self.current_run or (None, None)
        if self.args.probackup_byte_diagnostic == 'first_repeat' and repeat != 0:
            return {'skipped': '--probackup-byte-diagnostic first_repeat', 'identical': None}
        started = time.perf_counter()
        view = measure.byte_view(measure.files_digest(output, measure.byte_diagnostic_paths(output)))
        self.byte_views[(repeat, speed, method)] = view
        return {**measure.byte_diagnostic(reference, view), 'reference': 'start',
                'seconds': round(time.perf_counter() - started, 3)}

    def setup_probackup_ssh(self) -> dict:
        """pg_probackup's SSH configuration on dest (measure.probackup_ssh_config), and its log directory.

        Written once, in the preflight (check_probackup), under the work directory, which no cleanup
        removes. probackup_argv passes it to ssh with -F (--ssh-options); ssh refuses a configuration
        others can write, so it is 0600.
        """
        paths = measure.probackup_paths(self.work)
        text = measure.probackup_ssh_config(self.source.ip, self.args.ssh_user, self.args.ssh_key)
        Path(paths['log_dir']).mkdir(parents=True, exist_ok=True)
        config = Path(paths['ssh_config'])
        config.write_text(text)
        config.chmod(0o600)
        return {'ssh_config': str(config), 'text': text}

    def check_probackup(self) -> dict:
        """execute()'s preflight for probackup*: the same pg_probackup on both VMs, able to run catchup.

        The version each VM's env reported (run.json environment, `--version`) must be found and the same
        on both: its remote mode runs the source's binary as its agent. `catchup --help` on both must list
        -b FULL among its modes, -j and every long option probackup_argv passes (measure.
        probackup_help_problems). Then dest must reach the source's binary the way catchup will
        (measure.probackup_ssh_argv, with setup_probackup_ssh's configuration). SystemExit otherwise.
        """
        environment = self.run['environment']
        texts = {host: environment[host].get('pg_probackup') for host in ('source', 'dest')}
        versions = {host: measure.probackup_version(text) for host, text in texts.items()}
        missing = [host for host, version in versions.items() if version is None]
        if missing:
            raise SystemExit(f'pg_probackup ({self.args.pg_probackup}) is not installed on {", ".join(missing)} '
                             f'({texts}); remote.py provision runs infra/gcp/install_incumbents.sh on both VMs, '
                             f'which links {measure.PROBACKUP_EXE} to the binary whether the package or the source '
                             'build installed it; or pass --pg-probackup with a path that runs it on both VMs')
        if versions['source'] != versions['dest']:
            raise SystemExit(f'pg_probackup differs between the VMs ({texts}); its remote mode needs the same '
                             'release on both')
        argv = self.probackup_argv('probackup8', self.method_paths('probackup8'))
        dest_help = subprocess.run([self.args.pg_probackup, 'catchup', '--help'], capture_output=True, text=True,
                                   timeout=120, stdin=subprocess.DEVNULL)
        source_help = self.source.agent('run', '--log', f'{self.work}/logs/pg_probackup-catchup-help.log', '--',
                                        self.args.pg_probackup, 'catchup', '--help')
        problems = {host: measure.probackup_help_problems(text, argv) for host, text in (
            ('dest', dest_help.stdout + dest_help.stderr), ('source', source_help.get('log_tail', '')))}
        if any(problems.values()):
            raise SystemExit(f'pg_probackup catchup --help lacks what probackup* pass: {problems}')
        ssh = self.setup_probackup_ssh()
        reach = subprocess.run(measure.probackup_ssh_argv(self.args.pg_probackup, source_ip=self.source.ip,
                                                          ssh_user=self.args.ssh_user, ssh_config=ssh['ssh_config']),
                               capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        if reach.returncode != 0 or measure.probackup_version(reach.stdout) != versions['source']:
            raise SystemExit(f"pg_probackup's SSH from dest does not reach {self.args.pg_probackup} on the source "
                             f'({reach.returncode}): {(reach.stdout + reach.stderr).strip()[-1000:]}')
        # as_read: the release whose catchup EXP-11 read (its flush, its log lines); another is recorded only.
        return {'versions': texts, 'version': versions['dest'],
                'as_read': versions['dest']['version'] == measure.PROBACKUP_VERSION,
                'ssh_config': ssh['ssh_config'], 'reach': reach.stdout.strip()}

    def parcopy_receive_argv(self, method, paths) -> list:
        """parcopy's receiver on dest: listens, then writes O from the sender's streams."""
        return [sys.executable, '-m', measure.PARCOPY_MODULE, 'receive', '--output', paths['output'],
                '--listen', f'{self.dest_ip}:{PARCOPY_PORT}', '--stats-json', paths['receive_stats'],
                *measure.parcopy_args(method)['receive']]

    def parcopy_send_argv(self, method, paths) -> list:
        """parcopy's sender on the source: pgdelta.datadir's selection of the stopped data directory."""
        return [self.source.python, '-m', measure.PARCOPY_MODULE, 'send', '--source', paths['source_data'],
                '--pg-controldata', self.bin / 'pg_controldata', '--connect', f'{self.dest_ip}:{PARCOPY_PORT}',
                '--stats-json', paths['stats'], *measure.parcopy_args(method)['send']]

    def run_parcopy(self, method, paths) -> dict:
        """parcopy*: one timed phase in pgdelta_direct*'s frame (measure.PARCOPY_PHASE), one counter window.

        The source was stopped cleanly before the cache drop (prepare). Its steps: receiver_start, from
        launching parcopy's receiver on dest (parcopy_receive_argv) until it listens, then transfer, its
        sender on the source over the harness's SSH (parcopy_send_argv), until both ends have exited. The
        receiver's fsync tail is inside: it exits after its last fsync.
        """
        return self.receiver_transfer(method, paths)

    def check_parcopy(self) -> dict:
        """execute()'s preflight for parcopy*: its command line, sendfile on the source and write-back on dest.

        `python -m experiments.network.parcopy receive --help` on dest and `send --help` on the source must
        list every option parcopy_receive_argv and parcopy_send_argv pass. The source's Python must have
        os.sendfile, and dest's must be able to start write-back (pgdelta.flush, sync_file_range), or the
        bound would run without its early write-back. SystemExit if not.
        """
        method = next(method for method in self.methods if method in measure.PARCOPY_METHODS)
        paths = self.method_paths(method)
        options = {'receive': [str(part) for part in self.parcopy_receive_argv(method, paths)
                               if str(part).startswith('--')],
                   'send': [str(part) for part in self.parcopy_send_argv(method, paths) if str(part).startswith('--')]}
        local = subprocess.run([sys.executable, '-m', measure.PARCOPY_MODULE, 'receive', '--help'], cwd=self.repo,
                               capture_output=True, text=True, timeout=120)
        local_capabilities = subprocess.run([sys.executable, '-c', PARCOPY_CAPABILITIES], cwd=self.repo,
                                            capture_output=True, text=True, timeout=120)
        remote = self.source.agent('run', '--log', f'{self.work}/logs/parcopy-send-help.log', '--cwd',
                                   self.remote_repo, '--', self.source.python, '-m', measure.PARCOPY_MODULE, 'send',
                                   '--help')
        remote_capabilities = self.source.agent('run', '--log', f'{self.work}/logs/parcopy-capabilities.log',
                                                '--cwd', self.remote_repo, '--', self.source.python, '-c',
                                                PARCOPY_CAPABILITIES)
        found = {'dest': {'options': pgdelta_supports(local.stdout, options['receive']),
                          'capabilities': last_json(local_capabilities.stdout)},
                 'source': {'options': pgdelta_supports(remote.get('log_tail', ''), options['send']),
                            'capabilities': last_json(remote_capabilities.get('log_tail', ''))}}
        problems = [f'parcopy {command} on {host} lacks {" ".join(options[command])}'
                    for host, command in (('dest', 'receive'), ('source', 'send')) if not found[host]['options']]
        if not (found['dest']['capabilities'] or {}).get('writeback'):
            problems.append("dest can't start write-back (pgdelta.flush.start_writeback)")
        if not (found['source']['capabilities'] or {}).get('sendfile'):
            problems.append('the source has no os.sendfile')
        if problems:
            raise SystemExit(f'parcopy cannot run: {"; ".join(problems)} ({found})')
        return {'options': options, **found}

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
        # EXP-13's v1_tls: the receiver is reachable only through dest's stunnel, on localhost.
        listen = (f'127.0.0.1:{PGDELTA_PORT}' if method in measure.TLS_METHODS else f'{self.dest_ip}:{PGDELTA_PORT}')
        return [sys.executable, '-m', 'pgdelta', 'receive', *target,
                '--listen', listen,
                '--pg-controldata', self.bin / 'pg_controldata', *measure.PGDELTA_ARGS[method]['receive']]

    def pgdelta_send_argv(self, method, paths) -> list:
        # pgdelta_direct* reads a stopped data directory (--source-kind datadir, among the engine's
        # flags): the source's, or EXP-12's snapshot for the proxies (method_paths' source_data); the
        # staged methods read the capture S.
        source = paths['source_data'] if method in measure.PGDELTA_DIRECT_METHODS else self.source_capture
        # EXP-13's v1_tls connects to the source's stunnel on localhost, which opens TLS 1.3 to dest's.
        connect = (f'127.0.0.1:{measure.STUNNEL_CLIENT_PORT}' if method in measure.TLS_METHODS
                   else f'{self.dest_ip}:{PGDELTA_PORT}')
        # EXP-13: the busy threshold of the sender's waits (the visit's one-stream rate times its ping minimum),
        # where both VMs' pgdelta takes it (preflight_t12).
        busy = (self.condition or {}).get('busy_inflight_bytes') if self.t12 and self.busy_flag else None
        return [self.source.python, '-m', 'pgdelta', 'send', '--source', source,
                '--connect', connect,
                '--pg-controldata', self.bin / 'pg_controldata', '--stats-json', paths['stats'],
                *measure.PGDELTA_ARGS[method]['send'],
                *(['--busy-inflight-bytes', str(busy)] if busy is not None else [])]

    def receive_argv(self, method, paths) -> list:
        """The receiver a method launches on dest: parcopy's for parcopy*, pgdelta's otherwise."""
        if method in measure.PARCOPY_METHODS:
            return self.parcopy_receive_argv(method, paths)
        return self.pgdelta_receive_argv(method, paths)

    def send_argv(self, method, paths) -> list:
        """The sender a method runs on the source: parcopy's for parcopy*, pgdelta's otherwise."""
        if method in measure.PARCOPY_METHODS:
            return self.parcopy_send_argv(method, paths)
        return self.pgdelta_send_argv(method, paths)

    def start_receiver(self, method, paths, holder: dict) -> list[dict]:
        """pgdelta's (or parcopy's) receiver part: launch it with --listen and wait until it listens.

        The receiver keeps running after this part, reading B0 into the page
        cache (--prefetch-basis on) until the sender connects in the transfer
        phase; holder['receiver'] is its Child. Returns no command records when
        it listens (its record, with its rusage, goes into the transfer phase
        once it exits), or its final record if it exited or never listened.
        """
        receiver = Child(self.receive_argv(method, paths),
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
        """pgdelta's (or parcopy's) transfer: the sender on the source, until both ends have exited."""
        sender = self.remote_command(self.send_argv(method, paths),
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
        return self.receiver_transfer(method, paths)

    def receiver_transfer(self, method, paths) -> dict:
        """pgdelta_direct*'s and parcopy*'s frame: one timed phase (measure.DIRECT_PHASE), one counter
        window, whose steps follow each other at once: receiver_start (the receiver's launch until it
        listens, start_receiver), then transfer (the sender, until both ends exit, pgdelta_transfer)."""
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

        pgdelta's and parcopy's stats directories; rsync's daemon; for the in-place methods and
        pgBackRest, B, a copy of B0 (B already exists in a real refresh, and B0 must stay
        unchanged for the next method); for pgdelta_direct* and parcopy* (SOURCE_STOP_METHODS),
        a clean stop of the source cluster, recorded in daemon_holder so that run_method restarts
        it whatever happens next; for full_wb (HOST_TUNED_METHODS), dest's write-back setting,
        recorded there too so that run_method resets it whatever happens next.
        """
        started = time.perf_counter()
        steps = []
        if method in measure.PGDELTA_METHODS or method in measure.PARCOPY_METHODS:
            self.source.agent('run', '--log', self.source_log(f'{method}-mkdir-stats.log'), '--',
                              'mkdir', '-p', f'{self.work}/stats')
        if method in measure.PARCOPY_METHODS:
            Path(paths['receive_stats']).parent.mkdir(parents=True, exist_ok=True)
        if method in measure.BASIS_COPY_METHODS:
            steps.append(self.copy_basis(method, paths))
        if method in measure.PGBACKREST_METHODS:
            # pgBackRest appends to its file log; start each run's afresh (tool_reported keeps it).
            Path(self.pgbackrest_file_log(method)).unlink(missing_ok=True)
        if method in measure.SOURCE_STOP_METHODS:
            daemon_holder['source_stopped'] = True  # set first: a failed stop is still restarted
            steps.append({'what': 'stop the source cluster cleanly (pg_ctl stop -m fast)',
                          **self.source.script(SOURCE_CLUSTER, 'stop-clean', '--root', self.source_root,
                                               '--bin-dir', str(self.bin))})
        if method in measure.HOST_TUNED_METHODS:
            if self.writeback_defaults is None:
                raise CommandError(f"{method}: dest's write-back defaults were not recorded, so they could "
                                   'not be reset')
            daemon_holder['dirty_writeback'] = None  # set first: a failed set is still reset
            daemon_holder['dirty_writeback'] = self.set_dirty_writeback(method)
            steps.append({'what': "set dest's write-back for the run (reset after it)",
                          **daemon_holder['dirty_writeback']})
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

    # --- dest's write-back settings (T2's full_wb) --------------------------------

    def dirty_writeback(self) -> dict:
        """dest's vm.dirty_background_ratio and vm.dirty_background_bytes now."""
        return self.local_agent('sysctl', 'get', measure.DIRTY_BACKGROUND_RATIO,
                                measure.DIRTY_BACKGROUND_BYTES)['values']

    def record_writeback_defaults(self) -> dict:
        """At the start of a T2 run: dest's write-back settings, which every run then requires.

        The bytes setting must be 0 (the ratio in effect), or there is no ratio to reset to.
        """
        values = self.dirty_writeback()
        if values.get(measure.DIRTY_BACKGROUND_BYTES) != 0:
            raise SystemExit(f'{measure.DIRTY_BACKGROUND_BYTES} is not 0 on dest ({values}): set '
                             f'{measure.DIRTY_BACKGROUND_RATIO} back to its default first')
        self.writeback_defaults = values
        return values

    def writeback_at_defaults(self, values: dict) -> bool:
        """Whether the ratio is at its recorded default and the bytes setting at 0."""
        default = self.writeback_defaults[measure.DIRTY_BACKGROUND_RATIO]
        return (values.get(measure.DIRTY_BACKGROUND_RATIO) == default
                and values.get(measure.DIRTY_BACKGROUND_BYTES) == 0)

    def check_dirty_writeback(self, sysctl: dict | None = None) -> dict:
        """The guard before every T2 run: the ratio at its recorded default and the bytes setting at 0.

        Settings found otherwise (full_wb's reset failed after its run, say on a transient sudo error) are
        reset once here, the attempt recorded in sysctl['guard_reset'], and the run goes on if that took
        them back. If it didn't, the run is refused (CommandError) and self.writeback_stuck says why, which
        stops the trial (run_cell) and the invocation (execute): every later run would be refused too.
        Returns the settings the run starts with.
        """
        values = self.dirty_writeback()
        if self.writeback_at_defaults(values):
            return values
        try:
            reset = self.reset_dirty_writeback()
        except Exception as exc:
            reset = {'error': f'{type(exc).__name__}: {exc}'}
        if sysctl is not None:
            sysctl['guard_reset'] = {'found': values, **reset}
        if 'error' not in reset:
            log(f"  dest's write-back settings were not at their defaults ({values}); reset by the guard")
            return reset['values']
        self.writeback_stuck = (f"dest's write-back settings are not at their defaults ({values}; want "
                                f'{measure.DIRTY_BACKGROUND_RATIO}='
                                f'{self.writeback_defaults[measure.DIRTY_BACKGROUND_RATIO]}, '
                                f"{measure.DIRTY_BACKGROUND_BYTES}=0), and the guard's reset did not take them "
                                f"back: {reset['error']}")
        raise CommandError(self.writeback_stuck)

    def set_dirty_writeback(self, method) -> dict:
        """full_wb's setting on dest (measure.HOST_TUNED_METHODS), set before the cache drop.

        Both settings are read back after it ('values', the record's sysctl.during): `agent.py sysctl set`
        reports only the one it wrote, and the ratio's 0, which the bytes setting puts in force, is recorded too.
        """
        settings = measure.HOST_TUNED_METHODS[method]
        result = self.local_agent('sysctl', 'set', *(f'{name}={value}' for name, value in settings.items()))
        return {**result, 'written': result.get('values'), 'values': self.dirty_writeback()}

    def reset_dirty_writeback(self) -> dict:
        """Back to the defaults: the ratio written to its recorded default, which sets the bytes setting to 0.

        Linux refuses 0 written to vm.dirty_background_bytes. Checked after: CommandError unless both
        are at their defaults.
        """
        default = self.writeback_defaults[measure.DIRTY_BACKGROUND_RATIO]
        result = self.local_agent('sysctl', 'set', f'{measure.DIRTY_BACKGROUND_RATIO}={default}')
        values = self.dirty_writeback()
        if not self.writeback_at_defaults(values):
            raise CommandError(f"dest's write-back settings did not return to their defaults: {values}")
        return {**result, 'values': values}

    def run_phases(self, method, paths, daemon_holder: dict) -> dict:
        # EXP-12's arms first: the proxies are in PGDELTA_DIRECT_METHODS, so they must be taken before it.
        if method in measure.V1PROXY_METHODS:
            return self.run_v1proxy(method, paths)
        if method == 'none':
            return self.run_none(method, paths)
        if method == 'capture':
            return self.run_capture(method, paths)
        if method == 'bracket':
            return self.run_bracket(method, paths)
        if method in measure.CONTROL_METHODS:
            return self.run_control(method, paths)
        if method in measure.FULL_VARIANTS:
            return self.run_full(method, paths)
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
        if method in measure.PROBACKUP_METHODS:
            return self.run_probackup(method, paths)
        if method in measure.PARCOPY_METHODS:
            return self.run_parcopy(method, paths)
        raise CommandError(f'unknown method {method}')

    def verify(self, method, paths, phases) -> dict:
        if method in measure.G4_METHODS and self.t10a:
            # EXP-12: the bytes, then the started copy against the load's oracle (G4).
            return self.verify_live(method, paths, phases)
        if method in (*measure.T10A_ARMS, *measure.CONTROL_METHODS) and method not in measure.V1PROXY_METHODS:
            # none, bracket and the controls write nothing; the capture's S is verified on the source later.
            return self.verify_nothing(method, paths)
        if method in measure.PGDELTA_DIRECT_METHODS or method in measure.PARCOPY_METHODS:
            # parcopy* copies v1's selection of the stopped source: checked as pgdelta_direct* is.
            return self.verify_direct(method, paths)
        if method in measure.PGBACKREST_METHODS:
            return self.verify_pgbackrest(method, paths)
        if method in measure.PROBACKUP_METHODS:
            return self.verify_probackup(method, paths)
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
        if method in measure.FULL_VARIANTS:
            # T2: only now, with its bytes counted: O is started on dest, recovers from its backup_label
            # to consistency, answers a query, and is stopped (time until usable).
            result['output_start'] = self.check_output(method, output, recovery=True)
        after = measure.tree_stat(self.b0)
        result['basis_changes'] = measure.stat_changes(self.b0_stat, after)
        result['verified'] = (check['exit_code'] == 0 and not result['basis_changes']
                              and result.get('identical_to_capture', True)
                              and result.get('independent_of_basis', True)
                              and not result.get('in_place_leftovers')
                              and (result['output_start']['ok'] if 'output_start' in result else True))
        return result

    def verify_direct(self, method, paths) -> dict:
        """pgdelta_direct*: O (or B) against the files pgdelta's datadir mode selects, then a start.

        Untimed, while the source is still stopped: the output's regular files must be
        those `python -m pgdelta.datadir list` selects in the source's data directory,
        byte for byte (SHA-256); it shares no inode with B0; in place, no marker or renamed
        control file is left. Then the output is started on dest on a spare port and must
        answer a query (it is stopped again), and B0 must be unchanged.

        EXP-12's proxies are checked against the snapshot's selection as its digest was stored at the
        trial's step 5 (snapshot_digest), so their verification never reads the source.
        """
        output = Path(paths['output'])
        if method in measure.V1PROXY_METHODS:
            if self.snapshot_digest is None:
                raise CommandError(f"{method}: the snapshot's digest was not taken (--snapshot)")
            source, digest = self.snapshot_digest, measure.tree_digest(output)
        else:
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
        if method in measure.PARCOPY_METHODS:
            # A5: the fsyncs parcopy owes for what O holds, counted apart from its own report.
            result['fsync_expected_from_output'] = measure.fsyncs_expected(digest)
        if method in measure.V1PROXY_METHODS:
            result['reference'] = {'against': "the snapshot's selection, as stored at the trial's step 5",
                                   'summary': measure.digest_summary(expected)}
        elif self.reference_digests.get('start'):
            # T2: this run's own stopped-source digest against the trial's reference, reported only.
            result['reference'] = self.source_against_reference(expected)
        result['shared_extents'] = measure.shared_extents(output)
        # Only now, with the files compared: starting the output changes it.
        result['output_start'] = self.check_output(method, output, recovery=False)
        result['basis_changes'] = measure.stat_changes(self.b0_stat, measure.tree_stat(self.b0))
        result['verified'] = (result['identical_to_source'] and independent
                              and not result.get('in_place_leftovers')
                              and result['output_start']['ok'] and not result['basis_changes'])
        return result

    def source_against_reference(self, digest: dict) -> dict:
        """A stopped-source run's (v1, parcopy) own digest of the source against the trial's start reference.

        EXP-11: a difference is reported only, and the run's own verification, against the source at the
        time, stands. Both are compared without what a clean stop rewrites (measure.reference_view); the
        summary lets finish_logical_checks compare with the end reference too.
        """
        view = measure.reference_view(digest)
        same, differences = measure.compare_digests(self.reference_digests['start'], view)
        return {'summary': measure.digest_summary(view), 'start_match': same, 'start_differences': differences}

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

    def psql_argv(self, host, port: int, sql: str, database: str = PG_DATABASE) -> list:
        return [self.bin / 'psql', '-X', '-A', '-t', '-q', '-v', 'ON_ERROR_STOP=1', '-h', host,
                '-p', str(port), '-U', PG_USER, '-d', database, '-c', sql]

    def query(self, sql: str, *, host, port: int, log_name: str, timeout: float = 7200,
              database: str = PG_DATABASE) -> str:
        """One psql query from dest; its output, or CommandError. The session is pinned to UTC."""
        argv = [str(part) for part in self.psql_argv(host, port, sql, database)]
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

    def check_output(self, method, data: Path, *, recovery: bool, query: str = OUTPUT_QUERY, checks=None) -> dict:
        """Start an output on dest, query it, and stop it again (untimed).

        With recovery, wait until it has replayed what it needs and left recovery first.
        The server is stopped whatever happens, before cleanup can remove its directory.
        EXP-12: `checks`, if given, runs once the query has answered, while the output is still up, and its
        result is the result's `checks` (the load's oracle, which classifies its own errors).
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
            if checks is not None:
                result['checks'] = checks()
        except (CommandError, subprocess.TimeoutExpired) as exc:
            result['error'] = f'{type(exc).__name__}: {exc}'
            result['error_kind'] = 'timeout' if isinstance(exc, subprocess.TimeoutExpired) else 'copy'
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
                                    'dest_to_source': reported.get('bytes_dest_to_source')},
                    # EXP-13: the protocol timers (each sender wait with its _busy_s and _idle_s; the receiver's
                    # under `receiver`), the leaf requests, the window and the fallback, read defensively; and
                    # whether the window and the fallback in effect are the ones the method pins.
                    **measure.pgdelta_protocol_report(reported),
                    'window_ok': (None if engine.get('window') is None or find_window(reported) is None
                                  else find_window(reported) == engine['window']),
                    'high_churn_ok': (reported.get('high_churn_node_fraction') == engine['high_churn']
                                      if engine.get('high_churn') is not None else None)}
        if method in measure.RSYNC_METHODS and transfer:
            sender = next(c for c in transfer['commands'] if c['host'] == 'source')
            stats = rsync_transfer_stats(sender.get('log_tail', ''))
            return {**stats, 'scope': 'rsync --stats on the sending client (protocol stream, '
                                      'no TCP/IP overhead)'}
        if method in measure.PGBACKREST_METHODS:
            return self.pgbackrest_reported(method, phases)
        if method in measure.FULL_VARIANTS:
            return self.basebackup_reported(method, phases)
        if method in measure.PROBACKUP_METHODS:
            return self.probackup_reported(method, phases)
        if method in measure.PARCOPY_METHODS:
            return self.parcopy_reported(method, paths)
        return {'bytes_source_to_dest': None, 'bytes_dest_to_source': None,
                'scope': FULL_BYTES_SCOPE}

    def syscall_audit(self, method, paths, phases) -> dict:
        """EXP-11's syscall audit of this run (--syscall-audit): its timed commands' traces on dest, read in
        order, against the definition, for the output (and pgdelta's receipt beside it), on
        --audit-filesystem. probackup* also get the catchup's trace alone ('tool_only'): what the tool's
        own flush leaves to its sync -f. The result decides no status."""
        traces = [command['trace'] for phase in phases.values() for command in phase.get('commands', ())
                  if command.get('trace')]
        scope = [str(paths[key]) for key in ('output', 'receipt') if key in paths]
        options = {'filesystem': self.args.audit_filesystem, 'cwd': self.repo, 'ignore': [str(self.out)]}
        audit = {'untimed': 'the commands ran under strace: this run times nothing', 'scope': scope,
                 'result': durability_audit.audit_files(traces, scope, **options)}
        if method in measure.PROBACKUP_METHODS and len(traces) > 1:
            audit['tool_only'] = durability_audit.audit_files(traces[:-1], scope, **options)
        result = audit['result']
        log(f'  audit: {"ok" if result["ok"] else "NOT durable"}: {result["files_written"]} files '
            f'({result["uncovered_file_count"]} uncovered), {result["directory_changes"]} directory changes '
            f'({result["uncovered_directory_change_count"]} uncovered)')
        return audit

    @staticmethod
    def stamped_command(phases: dict, phase: str, tool: str) -> tuple[dict | None, list]:
        """The first command of `phase` whose program name starts with `tool`, and its stamped lines."""
        commands = (phases.get(phase) or {}).get('commands') or []
        command = next((command for command in commands
                        if Path(str(command['argv'][0])).name.startswith(tool)), None)
        if command is None:
            return None, []
        try:
            text = Path(command['log']).read_text(errors='replace')
        except (OSError, KeyError, TypeError):
            return command, []
        return command, measure.parse_stamped_lines(text)

    def basebackup_reported(self, method, phases) -> dict:
        """A pg_basebackup arm: its phases from its timestamped --verbose lines (measure.basebackup_phases)."""
        command, lines = self.stamped_command(phases, measure.FULL_PHASE, 'pg_basebackup')
        return {'bytes_source_to_dest': None, 'bytes_dest_to_source': None, 'scope': FULL_BYTES_SCOPE,
                'flags': self.method_flags(method),
                'basebackup_phases': measure.basebackup_phases(lines, (command or {}).get('wall_seconds')),
                'lines': [[seconds, text] for seconds, text in lines]}

    def probackup_reported(self, method, phases) -> dict:
        """probackup*: its catchup's phases from its timestamped console lines (measure.probackup_phases)."""
        command, lines = self.stamped_command(phases, measure.PROBACKUP_PHASE, 'pg_probackup')
        return {'bytes_source_to_dest': None, 'bytes_dest_to_source': None,
                'scope': 'pg_probackup reports no network byte counts; the wire columns are the only byte '
                         'measure (its SSH is encrypted)',
                'flags': self.method_flags(method),
                'probackup_phases': measure.probackup_phases(lines, (command or {}).get('wall_seconds')),
                'lines': [[seconds, text] for seconds, text in lines]}

    def parcopy_reported(self, method, paths) -> dict:
        """parcopy*: its receiver's --stats-json (dest) and its sender's (the source), and their summary."""
        try:
            received = json.loads(Path(paths['receive_stats']).read_text())
        except (OSError, ValueError):
            received = None
        try:
            text = self.source.agent('run', '--log', self.source_log(f'{method}-stats.json.log'), '--',
                                     'cat', paths['stats'])['log_tail']
            sent = json.loads(text)
        except (CommandError, ValueError, KeyError, TypeError):
            sent = None
        summary = measure.parcopy_summary(received)
        return {'bytes_source_to_dest': summary['bytes'], 'bytes_dest_to_source': None,
                'scope': "parcopy's receiver: payload bytes over its streams (no framing or TCP/IP overhead)",
                'flags': self.method_flags(method), 'parcopy_summary': summary,
                'receive_stats': received, 'send_stats': sent}

    def run_method(self, trial: dict, repeat: int, speed: dict, link: dict, method: str,
                   order_index: int, order: list[str]) -> dict:
        if self.t10a:
            # EXP-12: eviction, the settle, the window and the quiet tail (run_load_method).
            return self.run_load_method(trial, repeat, speed, link, method, order_index, order)
        paths = self.method_paths(method)
        record = {'schema': SCHEMA_RUN, 'run_name': self.out.name, 'trial_id': self.trial_id,
                  'pattern': trial['pattern'], 'fraction': trial['fraction'],
                  'scale': trial['scale'], 'rows_changed': trial.get('rows_changed'),
                  'repeat': repeat, 'speed': speed, 'speed_index': link.get('speed_index'),
                  'speed_order': link.get('speed_order'), 'method': method,
                  'order_index': order_index, 'method_order': order,
                  'link': {key: link[key] for key in link if key.endswith('gbit')},
                  'paths': {key: str(value) for key, value in paths.items()},
                  # T2: the trial's block (main, extension, warm_source) and the source's cache state.
                  'block': link.get('block', 'main'), 'source_cache': link.get('source_cache', 'cold'),
                  'started_utc': utc_now(), 'status': 'running', 'error': None,
                  # An error's kind: 'tool' when a command of the method's own timed phases failed,
                  # 'harness' for anything else (EXP-11's A4 counts the first for parcopy* and probackup*).
                  'failure_kind': None}
        log(f'{self.trial_id} r{repeat} {speed["label"]} [{order_index + 1}/{len(order)}] {method}'
            + ('' if record['block'] == 'main' else f' ({record["block"]})'))
        daemon_holder: dict = {}
        self.log_prefix = f'r{repeat}-{speed.get("slug") or speed["label"]}-'
        self.current_run = (repeat, speed['label'])
        if self.t12:
            # EXP-13: the condition and what its visit measured (the ping minimum, the one-stream rate, ...).
            record['condition'] = dict(self.condition or {})
        try:
            stuck = self.output_stuck
            if stuck and (str(paths['output']) == stuck['data'] or method in measure.OUTPUT_START_METHODS):
                # Its directory holds the running server, or its verification would start one on its port.
                raise CommandError(f'an output started on port {OUTPUT_PG_PORT} for an earlier verification '
                                   f'could not be stopped ({stuck["data"]}); {method} cannot run')
            self.clean_method(paths)
            phases: dict = {}
            record['phases'] = phases
            if self.writeback_defaults is not None:
                # T2: dest's write-back settings at their defaults before every run (full_wb's are
                # reset after its own, and by the guard if that failed); both recorded, and those in
                # effect during the run.
                sysctl = record['sysctl'] = {}
                sysctl['before'] = self.check_dirty_writeback(sysctl)
            if self.t12:
                # EXP-13's guard before every run: both VMs' tcp_rmem and tcp_wmem at the condition's values (every
                # TCP setting in force recorded). A failed guard refuses the run (A11 (ii)).
                tcp = record.setdefault('sysctl', {})['tcp'] = self.tcp_guard(speed['host_tcp'])
                if not tcp['ok']:
                    raise CommandError(f"the TCP settings are not the condition's: {tcp['problems']}")
            record['preparation'] = self.prepare(method, paths, daemon_holder)
            if daemon_holder.get('dirty_writeback'):
                record.setdefault('sysctl', {})['during'] = daemon_holder['dirty_writeback']['values']
            if self.t12:
                if method in measure.TLS_METHODS:
                    # v1_tls's stunnel pair, started before the clock (stopped after it: t12_after_clock).
                    daemon_holder['stunnel'] = self.start_stunnels('')
                # The samplers start before the clock, the same in every arm; then a TCP metrics flush, the last
                # step before the cache drop, so no connection starts from a cached slow-start threshold.
                daemon_holder['net_samplers'] = self.start_net_samplers('run')
                record['tcp_metrics'] = self.tcp_metrics_flush()
            if record['source_cache'] == 'warm':
                # T2's warm-source block: the source is read instead of dropped; dest is dropped.
                record['warm_source'] = self.warm_source()
                record['cache_drop'] = self.drop_caches(source=False)
                record['caches_dropped'] = 'dest'
            else:
                # One drop on both VMs: every method's timed phases start from cold
                # caches. rsync's copy of B0 (in its end-to-end time, EXP-04's design)
                # runs after it, beside the capture, and is synced inside its timer.
                record['cache_drop'] = self.drop_caches()
                record['caches_dropped'] = True
            phases.update(self.run_phases(method, paths, daemon_holder))
            if self.t12:
                self.t12_after_clock(record, daemon_holder)
            record['totals'] = measure.phase_totals(phases)
            if method in measure.RSYNC_METHODS:
                record['totals_in_place'] = measure.phase_totals(
                    measure.without_part(phases, 'prep'))
            if method in measure.STAGED_METHODS:
                # The concurrent phase's two parts one after the other: the upper bound.
                record['totals_serial'] = measure.serial_totals(phases)
            problems = [problem for phase in phases.values() for problem in phase['failures']]
            if problems:
                # A command of the method's own timed phases exited non-zero or timed out: for parcopy* and
                # probackup*, a copy the tool itself aborted, which can be neither verified nor started (A4).
                record['failure_kind'] = 'tool'
                raise CommandError('; '.join(problems))
            record['tool_reported'] = self.tool_reported(method, paths, phases)
            if self.args.syscall_audit:
                record['audit'] = self.syscall_audit(method, paths, phases)
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
            # T2's checks (a) and (b) on the run's one counter window; durability_ok, apart from status.
            # EXP-13: with a noise table, (a)'s limit follows the run's clock.
            record['durability'] = measure.durability_check(record, record['output_bytes'],
                                                            self.idle_noise_bytes, self.noise_table)
            if self.t12:
                self.t12_checks(record, method, verification)
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
            record['failure_kind'] = record['failure_kind'] or 'harness'
            record['error'] = f'{type(exc).__name__}: {exc}'
            record['traceback'] = traceback.format_exc()
            log(f'  ERROR {record["error"]}')
        finally:
            if self.t12:
                # EXP-13: samplers and stunnels a failure left running are stopped, and the run's network checks
                # recorded whatever happened (A11 (ii)).
                try:
                    self.t12_after_clock(record, daemon_holder)
                except Exception as exc:
                    record['t12_cleanup_error'] = f'{type(exc).__name__}: {exc}'
                if 'network_checks' not in record:
                    record['network_checks'] = measure.network_checks(record)
            if 'dirty_writeback' in daemon_holder:
                # full_wb: dest's write-back settings back to their defaults, whatever happened.
                del daemon_holder['dirty_writeback']
                try:
                    record.setdefault('sysctl', {})['reset'] = self.reset_dirty_writeback()
                except Exception as exc:
                    problem = f'{type(exc).__name__}: {exc}'
                    record['sysctl']['reset'] = {'error': problem}
                    record['status'] = 'error'
                    record['failure_kind'] = record['failure_kind'] or 'harness'
                    record['error'] = (record['error'] + '; ' if record['error'] else '') + \
                        f"dest's write-back settings could not be reset: {problem}"
                    log(f"  ERROR dest's write-back settings could not be reset: {problem}")
            daemon = daemon_holder.pop('daemon', None)
            if daemon is not None:
                self.stop_rsyncd(daemon)
            if daemon_holder.pop('source_stopped', False):
                # pgdelta_direct* and parcopy*: the source runs again for the next method, whatever happened.
                try:
                    record['source_restart'] = self.restart_source()
                except Exception as exc:
                    self.source_down = f'{type(exc).__name__}: {exc}'
                    record['source_restart'] = {'error': self.source_down}
                    record['status'] = 'error'
                    record['failure_kind'] = record['failure_kind'] or 'harness'
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
        self.current_run = None
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
        """The source's per-table digest against every pending run of this trial, and EXP-11's verdicts.

        Taken once the trial's last method has run, as the row-level ground truth is: a scan of the
        source earlier would set hint bits and change pages under the other methods. In a T2 trial it
        follows the end reference digest (trial['reference_match']).
        - pgbackrest_*: 'pass' or 'fail'.
        - probackup* (EXP-11's Verification): a match passes. A difference is void if the two reference
          digests differ: the source changed during the trial, so the tool can't be blamed. Otherwise it
          fails (A4); an output that didn't start failed already (measure.probackup_outcome).
        - Reported only: a probackup* output's bytes against the end reference too, when the source
          changed (bytes_identical: identical to either); a v1 or parcopy run's own stopped-source digest
          against the end reference too. Neither changes a status.
        - A4: the probackup* and parcopy* runs that fail their standard (a parcopy* that failed verify_direct
          or didn't start; a probackup* failed as above), and the arms they exclude from every baseline;
          runs that ended in an error are listed beside them.
        Changed records and the CSV are rewritten. None when the trial has nothing to settle.
        """
        records = [record for record in self.records if record.get('trial_id') == self.trial_id]
        pending = [record for record in records if record.get('status') == 'pending']
        pending_ids = {id(record) for record in pending}
        settled = measure.PROBACKUP_METHODS + measure.PARCOPY_METHODS + measure.PGDELTA_DIRECT_METHODS
        if not pending and not (self.t2 and any(record.get('method') in settled for record in records)):
            return None
        reference_match = trial.get('reference_match')
        check: dict = {'reference_match': reference_match, 'runs': []}
        # EXP-13's A15: a probackup* difference is void only if a user-table file differs between the reference
        # digests (catalogs change in every trial); EXP-11's rule voided it whenever any file did.
        void_match = trial.get('reference_user_tables_match') if self.t12 else reference_match
        if self.t12:
            check.update(void_rule='A15: user-table files only', reference_user_tables_match=void_match)
        source = None
        if pending:
            started = time.perf_counter()
            source = json.loads(self.query(measure.logical_digest_sql(), host=self.source.ip, port=PG_PORT,
                                           log_name='source-logical-digest.log'))
            check.update(source=source, seconds=round(time.perf_counter() - started, 3))
        changed = []
        for record in records:
            verification = record.get('verification') or {}
            method = record.get('method')
            updated = id(record) in pending_ids
            if updated:
                match, differences = measure.compare_logical(source, verification.get('logical') or {})
                verification.update(logical_match=match, logical_differences=differences, source_logical=source)
                if method in measure.PROBACKUP_METHODS:
                    record['status'] = measure.probackup_outcome(True, match, void_match)
                    verification['void'] = record['status'] == 'void'
                    if record['status'] == 'void':
                        record['void_reason'] = ("its digest differs from the source's, and the source changed "
                                                 'during the trial (its reference digests differ'
                                                 + (' in a user-table file)' if self.t12 else ')'))
                else:
                    record['status'] = 'pass' if match else 'fail'
                if record['status'] == 'fail':
                    record['error'] = ("the restored copy's digest differs from the source's" if method in
                                       measure.PGBACKREST_METHODS else "the copy's digest differs from the source's")
                check['runs'].append({'repeat': record['repeat'], 'speed': record['speed']['label'],
                                      'method': method, 'match': match, 'status': record['status']})
            if reference_match is False and 'end' in self.reference_digests:
                updated = self.against_end_reference(record, verification) or updated
            if updated:
                changed.append(record)
        for record in changed:
            save_json(self.record_path(record), record)
            for run in trial['runs']:
                if (run['repeat'], run['speed'], run['method']) == (
                        record['repeat'], record['speed']['label'], record['method']):
                    run['status'] = record['status']
        if self.t2:
            check['a4'] = self.a4_summary(records)
            check['void_runs'] = [self.run_key(record) for record in records if record.get('status') == 'void']
            # v1's and parcopy's own digests of the stopped source that differ from the start reference.
            check['source_differs_from_reference'] = [
                self.run_key(record) for record in records
                if ((record.get('verification') or {}).get('reference') or {}).get('start_match') is False]
        if changed:
            measure.write_csv(self.out / 'summary.csv', self.records)
        return check

    @staticmethod
    def run_key(record: dict) -> dict:
        return {'repeat': record.get('repeat'), 'speed': (record.get('speed') or {}).get('label'),
                'method': record.get('method')}

    def against_end_reference(self, record: dict, verification: dict) -> bool:
        """When the source changed during the trial: the reported-only comparisons against the end reference.

        Whether it added any to the record.
        """
        end, added = self.reference_digests['end'], False
        key = (record.get('repeat'), (record.get('speed') or {}).get('label'), record.get('method'))
        view = self.byte_views.get(key)
        diagnostic = verification.get('byte_diagnostic')
        if view is not None and isinstance(diagnostic, dict) and 'end' not in diagnostic:
            diagnostic['end'] = measure.byte_diagnostic(end, view)
            verification['bytes_identical'] = bool(diagnostic.get('identical')) or diagnostic['end']['identical']
            added = True
        reference = verification.get('reference')
        if isinstance(reference, dict) and 'end_match' not in reference:
            reference['end_match'] = (reference['summary']['listing_sha256']
                                      == measure.digest_summary(end)['listing_sha256'])
            added = True
        return added

    def a4_summary(self, records) -> dict:
        """EXP-11's A4 for this trial: the parcopy* and probackup* runs that fail their standard.

        A run fails it when its output didn't start, failed verify_direct, or (probackup*) holds a digest
        that differs from an unchanged source's; and when the copy itself failed, its own timed command
        exiting non-zero or timing out (failure_kind 'tool': parcopy's receiver or sender, pg_probackup's
        catchup or its sync -f), since such an output can be neither verified nor started. Errors of the
        harness around the tool (failure_kind 'harness') fail nothing and are listed apart.
        """
        failed, errors = [], []
        for record in records:
            method = record.get('method')
            if method not in measure.PROBACKUP_METHODS and method not in measure.PARCOPY_METHODS:
                continue
            verification = record.get('verification') or {}
            if record.get('status') == 'fail':
                start = verification.get('output_start') or {}
                reason = ("the output didn't start" if not start.get('ok')
                          else "its digest differs from the source's, which didn't change"
                          if method in measure.PROBACKUP_METHODS else 'verify_direct failed')
                failed.append({**self.run_key(record), 'reason': reason})
            elif record.get('status') == 'error' and record.get('failure_kind') == 'tool':
                failed.append({**self.run_key(record), 'reason': f"the copy itself failed: {record.get('error')}"})
            elif record.get('status') == 'error':
                errors.append({**self.run_key(record), 'error': record.get('error'),
                               'failure_kind': record.get('failure_kind')})
        return {'failed_runs': failed, 'excluded_arms': sorted({run['method'] for run in failed}),
                'errors': errors,
                'rule': ('A4: an arm with a failed run is excluded from every baseline, and a copy the tool itself '
                         'aborted (its own timed command failed) is a failed run. A void run is not a failure, byte '
                         'differences fail nothing, and errors of the harness around the tool are listed apart')}

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
        # EXP-12's arms (the proxies with pgdelta's, below): each one's whole command line.
        if method == 'none':
            return {'none': {'window_s': self.args.none_window_s, 'commands': []}}
        if method == 'capture':
            return {'pg_basebackup': ['--manifest-checksums', self.args.manifest_checksums,
                                      *measure.BASEBACKUP_VERBOSE],
                    'argv': [str(part) for part in self.capture_argv()],
                    'output': 'agent.py run --tee on the source; stamped here as each line arrives over SSH'}
        if method == 'bracket':
            return {'psql': self.bracket_argv()}
        if method in measure.CONTROL_METHODS:
            return {'agent': self.control_argv(method), 'control': dict(measure.CONTROLS[method])}
        if method in measure.PGDELTA_ARGS:
            flags = {command: list(args) for command, args in measure.PGDELTA_ARGS[method].items()}
            if method in measure.V1PROXY_METHODS:
                paths = self.method_paths(method)
                flags.update(send_argv=[str(part) for part in self.pgdelta_send_argv(method, paths)],
                             receive_argv=[str(part) for part in self.pgdelta_receive_argv(method, paths)],
                             backup_hold=self.backup_hold_argv(), backup_statements=list(
                                 source_cluster.backup_hold_statements(BACKUP_LABEL)))
            elif method in measure.PGDELTA_STAGED_METHODS and self.t10a:
                flags['capture_argv'] = [str(part) for part in self.capture_argv()]
            elif self.t12:
                # EXP-13: each arm's whole command line (each run's own, with the visit's busy threshold where
                # pgdelta takes it, is in its record), and v1_tls's stunnel pair.
                paths = self.method_paths(method)
                flags.update(send_argv=[str(part) for part in self.pgdelta_send_argv(method, paths)],
                             receive_argv=[str(part) for part in self.pgdelta_receive_argv(method, paths)])
                if method in measure.TLS_METHODS:
                    flags['stunnel'] = self.stunnel_configs("<the run's server log>", "<the run's client log>")
            return flags
        if method in measure.PGBACKREST_METHODS:
            return {'restore': ['--delta', f'--process-max={measure.PGBACKREST_PROCESS_MAX}'],
                    'stanza': dict(measure.PGBACKREST_VARIANTS[method]),
                    'dest_stanza': measure.pgbackrest_dest_options(method),
                    'backup': ['--type=full', 'start-fast=y',
                               f'process-max={measure.PGBACKREST_PROCESS_MAX}']}
        if method in measure.RSYNC_METHODS:
            return {'rsync': self.rsync_argv(method)[1:-2]}
        if method in measure.FULL_VARIANTS:
            # The pg_basebackup arms: the variant's options (--verbose, --no-sync, --sync-method) and
            # the whole command line; full_wb its kernel setting and how it is reset.
            flags = {'pg_basebackup': ['--manifest-checksums', self.args.manifest_checksums,
                                       *measure.FULL_VARIANTS[method]],
                     'argv': [str(part) for part in self.full_argv(method, self.method_paths(method))],
                     'output': 'stdout and stderr timestamped line by line as they arrive (LC_MESSAGES=C)'}
            if method in measure.HOST_TUNED_METHODS:
                flags['sysctl'] = dict(measure.HOST_TUNED_METHODS[method])
                flags['sysctl_reset'] = (f'{measure.DIRTY_BACKGROUND_RATIO}=<its recorded default>, which '
                                         f'returns {measure.DIRTY_BACKGROUND_BYTES} to 0')
            return flags
        if method in measure.PROBACKUP_METHODS:
            paths = self.method_paths(method)
            return {'pg_probackup': [str(part) for part in self.probackup_argv(method, paths)],
                    'catchup': list(measure.probackup_args(method)),
                    'ssh_options': [f'{option}={value}' for option, value in measure.PROBACKUP_SSH_OPTIONS],
                    'ssh_config': measure.probackup_ssh_config(self.source.ip, self.args.ssh_user,
                                                               self.args.ssh_key),
                    'sync': self.probackup_sync_argv(paths),
                    'wal_ticker': ({'argv': self.wal_ticker_argv(), 'interval_s': self.args.probackup_wal_ticker,
                                    'sql': "SELECT pg_logical_emit_message(false, 'pgd-tick', '', true)"}
                                   if self.args.probackup_wal_ticker else None)}
        if method in measure.PARCOPY_METHODS:
            paths = self.method_paths(method)
            return {'receive_argv': [str(part) for part in self.parcopy_receive_argv(method, paths)],
                    'send_argv': [str(part) for part in self.parcopy_send_argv(method, paths)],
                    **{command: list(args) for command, args in measure.parcopy_args(method).items()}}
        flags = {'pg_basebackup': ['--manifest-checksums', self.args.manifest_checksums]}
        if method in measure.INCREMENTAL_METHODS:
            flags['pg_combinebackup'] = [measure.COMBINE_MODES[method],
                                         f'--manifest-checksums={self.args.manifest_checksums}']
        return flags

    # --- trials ------------------------------------------------------------

    def record_path(self, record: dict) -> Path:
        # EXP-13's conditions are named by their slug (2g-60ms-tuned) in file names, their label (2:60:tuned) inside.
        speed = record['speed']
        return (self.out / 'runs' / record['trial_id']
                / f'r{record["repeat"]}-{speed.get("slug") or speed["label"]}-{record["method"]}.json')

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
        """One trial. Under EXP-12's load (--load), its own order (run_load_trial). In a T2 run, EXP-11's order
        within a trial:

        1. the cluster and dataset, B0, the workload and its CHECKPOINT;
        2. the start reference digest (a clean stop, the selected files hashed, a restart);
        3. the probes: the durable write floor (dest), the cold read floor and the single reader
           (source), the backup-mode bracket;
        4. the main block; 5. the extension, then the warm-source block (when asked for);
        6. the durable write floor again; 7. the end reference digest;
        8. the source's logical digest (the pending logical checks); 9. EXP-01's row-level ground
           truth; 10. B0's full digest.

        Outside T2 runs, steps 2, 3, 6 and 7 are left out. Between steps 2 and 7 no SQL reads a
        user table on the source.
        """
        if self.t10a:
            return self.run_load_trial(index, pattern, fraction)
        args = self.args
        # A source that could not be restarted stops its own trial; this one builds a new cluster.
        self.source_down = None
        self.trial_id = f'{index:02d}-{pattern}-f{fraction:g}-s{args.scale}'
        self.multi_stream_links = {}
        self.reference_digests = {}
        self.byte_views = {}
        self.probe_relation_path = None
        self.visited, self.zero_delay = set(), {}
        trial = {'schema': SCHEMA_TRIAL, 'trial_id': self.trial_id, 'pattern': pattern,
                 'fraction': fraction, 'scale': args.scale, 'seed': args.seed,
                 'started_utc': utc_now(), 'status': 'running', 'links': [], 'runs': []}
        if self.t12:
            # EXP-13: every visit (the tree, pings, control no-ops, link tests) and each condition's first-visit probes.
            trial.update(visits=[], first_visit={})
        save_json(self.out / 'trials' / f'{self.trial_id}.json', trial)
        try:
            if self.t12:
                # EXP-13: the cluster, B0 and the workload with the tree off and Debian's TCP settings.
                trial['network_off'] = self.network_off()
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
            if self.t2:
                # The single reader's file, from the catalog, before the start reference digest.
                trial['probe_relation'] = self.probe('the probe relation', self.probe_relation)
                if self.t12:
                    # A15: the user tables' files, from the catalog too, for the void rule at the trial's end.
                    trial['user_table_files'] = self.probe("the user tables' files", self.user_table_files)
                log(f'{self.trial_id}: the start reference digest')
                trial['reference_digests'] = {'start': self.reference_digest('start')}
                trial['probes'] = self.trial_probes()
                save_json(self.out / 'trials' / f'{self.trial_id}.json', trial)
            if self.t12 and self.t12_plan['zero_delay_rates']:
                # EXP-13's step 3: the zero-delay one-stream rates (the crossovers' x where no anchor runs).
                trial['zero_delay_link_tests'] = self.zero_delay_tests()
                save_json(self.out / 'trials' / f'{self.trial_id}.json', trial)
            trial['speed_orders'] = []
            for repeat in range(args.repeats):
                speeds = measure.order_speeds(self.speeds, self.speed_rng, args.speed_order,
                                              repeat)
                if self.t12:
                    # EXP-13: a condition where no arm runs in this repeat isn't visited in it.
                    speeds = [speed for speed in speeds if self.t12_methods_at(speed, repeat)]
                labels = [speed['label'] for speed in speeds]
                trial['speed_orders'].append(labels)
                self.run.setdefault('speed_orders', {}).setdefault(self.trial_id, []).append(
                    labels)
                log(f'{self.trial_id} r{repeat}: speed order {", ".join(labels)}')
                for speed_index, speed in enumerate(speeds):
                    here = (self.t12_methods_at(speed, repeat) if self.t12
                            else measure.methods_at(self.methods, self.method_speeds, speed['label']))
                    self.run_cell(trial, 'main', repeat, speed, here,
                                  {'speed_index': speed_index, 'speed_order': labels})
            # T2: the extension, then the warm-source block, unthrottled, each repeat in its own order.
            # Their repeats are numbered on from the main block's, so each (repeat, speed, method) is one run.
            unthrottled = measure.parse_speed(measure.UNTHROTTLED)
            repeat = args.repeats
            for block, repeats, methods in (('extension', args.extension_repeats, self.extension_methods),
                                            ('warm_source', args.warm_source_repeats, self.warm_source_methods)):
                for block_repeat in range(repeats):
                    log(f'{self.trial_id} r{repeat}: {block} repeat {block_repeat + 1} of {repeats}')
                    self.run_cell(trial, block, repeat, unthrottled, methods,
                                  {'speed_index': 0, 'speed_order': [unthrottled['label']],
                                   'block_repeat': block_repeat})
                    repeat += 1
            if self.t12:
                # EXP-13's step 5: the tree off and Debian's TCP settings, before the trial's end.
                trial['network_off_end'] = self.network_off()
                self.condition = None
            self.throttle('off')
            if self.t2:
                trial['probes']['durable_write_floor'].append(
                    self.probe('the durable write floor', self.durable_write_floor))
                log(f'{self.trial_id}: the end reference digest')
                trial['reference_digests']['end'] = self.reference_digest('end')
                same, differences = measure.compare_digests(self.reference_digests['start'],
                                                            self.reference_digests['end'])
                # If they differ, the source changed during the trial: a probackup* logical difference is void.
                trial['reference_match'] = same
                trial['reference_differences'] = differences
                if self.t12:
                    # A15 (EXP-13): void only if a user-table file differs; the differences by file class.
                    user = trial.get('user_table_files') or {}
                    classes = measure.reference_classes(self.reference_digests['start'],
                                                        self.reference_digests['end'], user.get('paths'))
                    trial['reference_classes'] = classes
                    trial['reference_user_tables_match'] = classes['user_tables_match']
                save_json(self.out / 'trials' / f'{self.trial_id}.json', trial)
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
            # A void run (probackup*: the source changed during the trial) is neither a pass nor a failure.
            bad = [run for run in trial['runs'] if run['status'] not in ('pass', 'void')]
            trial['status'] = 'pass' if not bad else 'fail'
        except Exception as exc:
            trial['status'] = 'error'
            trial['error'] = f'{type(exc).__name__}: {exc}'
            trial['traceback'] = traceback.format_exc()
            if isinstance(exc, InfrastructureFault):
                trial['infrastructure_fault'] = str(exc)  # EXP-13's A11 (i): recorded as such, nothing rerun
            log(f'{self.trial_id}: ERROR {trial["error"]}')
        finally:
            self.cleanup_trial(trial)
        trial['finished_utc'] = utc_now()
        save_json(self.out / 'trials' / f'{self.trial_id}.json', trial)
        return trial

    def run_cell(self, trial: dict, block: str, repeat: int, speed: dict, methods, extra: dict) -> None:
        """One (block, repeat, speed): the link set and tested, then each method once, in a new random order.
        EXP-13: the speed is a condition, set and checked by set_condition."""
        args = self.args
        link = self.set_condition(speed, trial, repeat) if self.t12 else self.set_speed(speed)
        link.update(repeat=repeat, block=block, source_cache='warm' if block in WARM_BLOCKS else 'cold',
                    **extra)
        order = measure.method_orders(methods, self.rng, 1)[0]
        link['method_order'] = order
        for order_index, method in enumerate(order):
            record = self.run_method(trial, repeat, speed, link, method, order_index, order)
            trial['runs'].append({'repeat': repeat, 'speed': speed['label'], 'method': method,
                                  'status': record['status'], 'block': block})
            if self.source_down:
                raise CommandError(f'the source cluster is down after {method}: '
                                   f'{self.source_down}')
            if self.writeback_stuck:
                raise CommandError(f'stopped after {method}: {self.writeback_stuck}')
            if record['status'] not in ('pass', 'pending') and args.stop_on_error:
                raise CommandError(f'{method} at {speed["label"]}: {record["error"]}')
        link['qdisc_after'] = self.qdisc_stats()
        trial['links'].append(link)
        self.run.setdefault('links', []).append(
            {'trial_id': self.trial_id, 'repeat': repeat, 'speed': speed['label'],
             'speed_index': link.get('speed_index'), 'block': block,
             **{key: link[key] for key in link if key.endswith('gbit')}})
        save_json(self.out / 'trials' / f'{self.trial_id}.json', trial)
        if self.out.joinpath('run.json').exists():
            save_json(self.out / 'run.json', self.run)

    # --- T2's per-trial steps: reference digests and probes (untimed) --------------

    def reference_digest(self, when: str) -> dict:
        """Steps 2 and 7: the source stopped cleanly, the files pgdelta.datadir selects hashed, restarted.

        The digest leaves out what every clean stop rewrites (measure.reference_view: global/pg_control,
        pg_wal/ and pg_stat/). It is kept whole in reference_digests[when] for the comparisons; the
        trial's record holds its summary. The source is restarted whatever happened; if it can't be,
        the trial stops.
        """
        started = time.perf_counter()
        result: dict = {'when': when}
        try:
            result['stop'] = self.source.script(SOURCE_CLUSTER, 'stop-clean', '--root', self.source_root,
                                                '--bin-dir', str(self.bin))
            listing = self.source.agent('datadir-digest', self.source_data, '--pg-controldata',
                                        str(self.bin / 'pg_controldata'), timeout=3600)
        finally:
            try:
                result['restart'] = self.restart_source()
            except Exception as exc:
                self.source_down = f'{type(exc).__name__}: {exc}'
                raise CommandError(f'the source cluster could not be restarted after the {when} reference '
                                   f'digest: {self.source_down}') from exc
        view = measure.reference_view(listing['digest'])
        self.reference_digests[when] = view
        return {**result, 'summary': measure.digest_summary(view), 'excluded': view['excluded'],
                'missing': view['missing'], 'read_from': listing.get('read_from'),
                'wall_seconds': round(time.perf_counter() - started, 3)}

    def probe(self, name: str, body) -> dict:
        """One untimed probe: its error is recorded and the trial goes on (no probe decides a run)."""
        try:
            return body()
        except Exception as exc:
            log(f'{self.trial_id}: {name} failed: {exc}')
            return {'error': f'{type(exc).__name__}: {exc}'}

    def trial_probes(self) -> dict:
        """Step 3: the durable write floor (dest), the cold read floor and the single reader (source; they
        read files only), and the backup-mode bracket. The write floor runs again at step 6."""
        return {'durable_write_floor': [self.probe('the durable write floor', self.durable_write_floor)],
                'read_floor': self.probe('the read floor', self.read_floor),
                'single_read': self.probe('the single reader', self.single_read_probe),
                'bracket': self.probe('the bracket probe', self.bracket_probe)}

    def probe_relation(self) -> dict:
        """pgbench_accounts's file, the largest relation, relative to the data directory (from the catalog)."""
        path = self.query(PROBE_RELATION_SQL, host=self.source.ip, port=PG_PORT, log_name='probe-relation.log')
        self.probe_relation_path = path
        return {'relation': PROBE_RELATION, 'path': path}

    def durable_write_floor(self) -> dict:
        """dest: as many bytes as B0 holds, by WRITE_FLOOR_WRITERS writers, buffered, with write-back every
        8 MiB, then an fsync of each (agent durable-write-test), after a drop of dest's cache."""
        size = (getattr(self, 'b0_digest', None) or {}).get('bytes')
        if not size:
            raise CommandError("B0's size is unknown")
        drop = self.local_agent('drop-caches')
        result = self.local_agent('durable-write-test', str(self.work / 'write-floor'), '--writers',
                                  str(WRITE_FLOOR_WRITERS), '--bytes', str(size), '--writeback-bytes',
                                  str(measure.PARCOPY_WRITEBACK_BYTES), timeout=3600)
        return {**result, 'drop': drop, 'utc': utc_now()}

    def read_floor(self) -> dict:
        """The source: its data directory without pg_wal, by READ_FLOOR_READERS readers, cold (EXP-07's C4)."""
        drop = self.source.agent('drop-caches')
        result = self.source.agent('read-test', self.source_data, '--readers', str(READ_FLOOR_READERS),
                                   '--exclude', 'pg_wal', timeout=3600)
        return {**result, 'drop': drop}

    def single_read_probe(self) -> dict:
        """The source: one reader of pgbench_accounts's file (with any further segments), cold."""
        if not self.probe_relation_path:
            raise CommandError(f"{PROBE_RELATION}'s file is unknown")
        drop = self.source.agent('drop-caches')
        result = self.source.agent('read-test', f'{self.source_data}/{self.probe_relation_path}',
                                   '--readers', '1', timeout=3600)
        return {**result, 'relation': PROBE_RELATION, 'drop': drop}

    def bracket_probe(self) -> dict:
        """The idle source: BRACKET_REPEATS x (drop its cache; backup start; backup stop), a cross-check."""
        return self.source.script(SOURCE_CLUSTER, 'backup-mode-probe', '--root', self.source_root,
                                  '--bin-dir', str(self.bin), '--repeats', str(BRACKET_REPEATS), timeout=3600)

    def cleanup_trial(self, trial: dict) -> None:
        steps = ((('network off', self.network_off),) if self.t12 else ()) + (
                 ('throttle off', lambda: self.throttle('off')),
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

    # --- T10a (EXP-12): the arms --------------------------------------------------------------

    def pause(self, seconds: float) -> None:
        """Wait: none's window, the settle's minimum, the quiet tail, the load's warm-ups (tests replace it)."""
        if seconds > 0:
            time.sleep(seconds)

    def remote_stamped_command(self, argv, log_name: str, *, cwd: str | None = None) -> dict:
        """A command on the source whose output lines are stamped here as they arrive over SSH.

        `agent.py run --tee` runs it on the source, logging there and copying each line to its stdout as it comes;
        a Child here (stamp_lines) stamps each on arrival, on this clock, as a dest-side pg_basebackup's are, with
        no extra process on the source. The record is the source command's (host 'source', its argv, its exit
        code, which `run --tee` exits with); its rusage and inner wall arrive in the agent's result line, which
        complete_stamped reads once the window has closed and the reader has settled.
        """
        argv = [str(part) for part in argv]
        extra = ['--cwd', cwd] if cwd else []
        if self.gnu_time:
            extra += ['--gnu-time', self.gnu_time]
        child = Child(self.source.agent_argv('run', '--tee', '--log', self.source_log(log_name), *extra, '--', *argv),
                      self.dest_log(log_name), env=messages_env(), stamp_lines=True)
        result = child.wait(self.args.command_timeout)
        result.update(host='source', argv=argv, ssh_rusage=result.get('rusage'), rusage=None, peak_rss_kb=None,
                      gnu_time=None, launcher=None, source_log=self.source_log(log_name), stamped_over_ssh=True)
        return result

    def complete_stamped(self, phases: dict) -> None:
        """Each stamped source command completed from the agent's result line at the end of its stamped log."""
        for phase in phases.values():
            for command in phase.get('commands', ()):
                if not command.get('stamped_over_ssh') or command.get('inner_wall_seconds') is not None:
                    continue
                try:
                    lines = measure.parse_stamped_lines(Path(command['log']).read_text(errors='replace'))
                    result = agent_result('\n'.join(text for _, text in lines))
                except (OSError, ValueError, KeyError):
                    command['agent_result'] = None
                    continue
                command.update(inner_wall_seconds=result.get('wall_seconds'), rusage=result.get('rusage'),
                               peak_rss_kb=result.get('peak_rss_kb'), gnu_time=result.get('gnu_time'),
                               launcher=result.get('launcher'), reported_exit_code=result.get('exit_code'))

    def run_none(self, method, paths) -> dict:
        """none: one empty timed phase of --none-window-s, with the counter reads every arm makes."""
        seconds = self.args.none_window_s
        return {measure.NONE_PHASE: self.timed_phase(measure.NONE_PHASE, lambda: self.pause(seconds) or [])}

    def run_capture(self, method, paths) -> dict:
        """capture: the staged capture alone, a local pg_basebackup of S on the source (CRC32C, -c fast, --verbose,
        stamped over SSH), flushed by pg_basebackup itself. S is verified and removed before the next settle."""
        return {measure.CAPTURE_PHASE: self.timed_phase(measure.CAPTURE_PHASE, lambda: self.capture_commands(method))}

    def bracket_argv(self) -> list[str]:
        """bracket: pg_backup_start('t10a', true), then pg_backup_stop(false), in one psql session on the source."""
        return source_cluster.backup_mode_argv(self.source_root, self.bin, label=BACKUP_LABEL)

    def run_bracket(self, method, paths) -> dict:
        """bracket: backup mode alone, one timed phase; psql's \\timing splits it into its start and stop."""
        return {measure.BRACKET_PHASE: self.timed_phase(
            measure.BRACKET_PHASE, lambda: [self.remote_command(self.bracket_argv(), f'{method}.log')])}

    def control_argv(self, method) -> list[str]:
        """A control's agent command on the source (measure.CONTROLS)."""
        control = measure.CONTROLS[method]
        if control['agent'] == 'load-pause':
            return ['load-pause', str(control['seconds']), '--load-dir', self.load_dir]
        return ['burn', '--procs', str(control['procs']), '--seconds', str(control['seconds'])]

    def run_control(self, method, paths) -> dict:
        """pause2 or burn4: the agent's command on the source as the one timed phase. pause2's signal times, on the
        source's clock, are the phase's `control` (A2 matches them with the log's gap)."""
        argv = self.control_argv(method)
        holder: dict = {}

        def body():
            started_utc, started = time.time(), time.perf_counter()
            holder['result'] = self.source.agent(*argv, timeout=600)
            return [{'host': 'source', 'argv': [AGENT, *argv], 'exit_code': 0,
                     'wall_seconds': round(time.perf_counter() - started, 6), 'started_utc': started_utc,
                     'rusage': None, 'role': f'the control, agent.py {argv[0]} (its result is the phase\'s control)'}]

        phase = self.timed_phase(measure.CONTROL_PHASE, body)
        phase['control'] = holder.get('result')
        return {measure.CONTROL_PHASE: phase}

    def backup_hold_argv(self) -> list[str]:
        return [self.source.python, f'{self.remote_repo}/{SOURCE_CLUSTER}', 'backup-hold', '--root', self.source_root,
                '--bin-dir', str(self.bin), '--label', BACKUP_LABEL]

    def start_backup_hold(self) -> dict:
        """The proxy's backup mode: backup-hold over SSH, until it prints its start LSN (pg_backup_start(t10a, true)
        has returned in its one session, which stays open)."""
        argv = self.backup_hold_argv()
        hold = {'argv': argv, 'started_utc': time.time(), 'started': time.perf_counter()}
        process = subprocess.Popen(self.source.argv(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        hold['process'] = process
        line = process.stdout.readline()
        hold['received_utc'] = time.time()
        if line.startswith(source_cluster.BACKUP_STARTED):
            hold['start'] = json.loads(line[len(source_cluster.BACKUP_STARTED):])
        else:
            hold['error'] = f'backup mode did not start: {line.strip()[-500:]!r}'
        return hold

    def stop_backup_hold(self, hold: dict) -> dict:
        """pg_backup_stop(false) in the held session (a line on backup-hold's stdin), until it reports the stop LSN.
        The command record of the backup mode: host 'source', its stop and start in `backup`."""
        process = hold['process']
        requested = time.time()
        try:
            # communicate() writes the line and closes stdin itself, and ignores a backup-hold that has already
            # exited. Closing stdin here first would make it fail before Python 3.13: it flushes stdin first, and a
            # closed pipe raises ValueError there (gh-74389), as on dest's Debian 12 python3 (3.11). A second call
            # (run_v1proxy's clean-up after an interrupted first) sends nothing more: communicate() refuses input once
            # communication has started.
            line = None if hold.get('stop_sent') else 'stop\n'
            hold['stop_sent'] = True
            stdout, stderr = process.communicate(input=line, timeout=600)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        try:
            backup = agent_result(stdout or '')
        except ValueError:
            backup = None
        return {'host': 'source', 'argv': hold['argv'], 'exit_code': process.returncode, 'over_ssh': True,
                'wall_seconds': round(time.perf_counter() - hold['started'], 6), 'started_utc': hold['started_utc'],
                'rusage': None, 'role': 'backup mode held in one session around the transfer (backup-hold)',
                'start_line': hold.get('start'), 'start_received_utc': hold.get('received_utc'),
                'stop_requested_utc': requested, 'backup': backup, 'stderr_tail': (stderr or '')[-2000:]}

    def run_v1proxy(self, method, paths) -> dict:
        """v1proxy*: one timed phase (measure.V1PROXY_PHASE), one counter window, in four steps.

        backup_start: backup-hold over SSH until pg_backup_start(t10a, true) has returned; receiver_start: pgdelta's
        receiver on dest, refreshing B in place, until it listens; transfer: pgdelta send on the source reading the
        snapshot (method_paths' source_data; the live source is never stopped), until both ends exit;
        backup_stop: pg_backup_stop(false) in the same session. The phase's `backup` holds A4 (ii): the start LSN
        before the sender's launch, and the stop asked for after both ends have exited.
        """
        holder: dict = {}
        steps: dict = {}
        start_step, receiver_step, transfer_step, stop_step = measure.V1PROXY_STEPS

        def step(name, host, origin, started):
            steps[name] = {'host': host, 'offset_seconds': round(started - origin, 3),
                           'wall_seconds': round(time.perf_counter() - started, 3)}

        def body():
            origin = time.perf_counter()
            hold = holder['hold'] = self.start_backup_hold()
            step(start_step, 'source', origin, origin)
            commands = []
            if 'error' not in hold:
                started = time.perf_counter()
                commands = self.start_receiver(method, paths, holder)
                step(receiver_step, 'dest', origin, started)
                if not commands:
                    started = time.perf_counter()
                    commands = self.pgdelta_transfer(method, paths, holder['receiver'], measure.V1PROXY_PHASE)
                    step(transfer_step, 'source', origin, started)
            started = time.perf_counter()
            commands.append(self.stop_backup_hold(hold))
            step(stop_step, 'source', origin, started)
            return commands

        try:
            phase = self.timed_phase(measure.V1PROXY_PHASE, body)
        except BaseException:
            receiver, hold = holder.get('receiver'), holder.get('hold')
            if receiver is not None and receiver.result is None:
                receiver.stop()
            if hold is not None and hold['process'].poll() is None:
                self.stop_backup_hold(hold)
            raise
        phase['steps'] = steps
        receiver = holder.get('receiver')
        if receiver is not None:
            phase['receiver'] = {'argv': receiver.argv, 'log': str(receiver.log_path)}
        phase['backup'] = self.backup_held(phase['commands'])
        return {measure.V1PROXY_PHASE: phase}

    @staticmethod
    def backup_held(commands) -> dict:
        """A4 (ii) for a proxy run: backup mode held across the transfer, in one session.

        The start LSN reached dest before the sender was launched, and the stop was asked for after both ends had
        exited (dest's clock for both); both LSNs are the session's own (backup-hold's).
        """
        hold = next((command for command in commands if command.get('role', '').startswith('backup mode held')), None)
        sender = next((command for command in commands if command.get('host') == 'source'
                       and command is not hold), None)
        receiver = next((command for command in commands if command.get('host') == 'dest'), None)
        if hold is None:
            return {'held': False, 'why': 'no backup-hold record'}
        backup = hold.get('backup') or {}
        start, stop = backup.get('start') or hold.get('start_line') or {}, backup.get('stop') or {}
        ends = [command['started_utc'] + command['wall_seconds'] for command in (sender, receiver)
                if command and command.get('started_utc') is not None]
        before = (sender is not None and hold.get('start_received_utc') is not None
                  and hold['start_received_utc'] <= sender['started_utc'])
        after = bool(ends) and hold.get('stop_requested_utc') is not None and hold['stop_requested_utc'] >= max(ends)
        return {'start_lsn': start.get('lsn'), 'start_utc': start.get('utc'), 'stop_lsn': stop.get('lsn'),
                'stop_utc': stop.get('utc'), 'one_session': bool(backup.get('one_session')),
                'started_before_sender': before, 'stopped_after_transfer': after,
                'held': bool(start.get('lsn') and stop.get('lsn') and backup.get('one_session') and before and after
                             and hold.get('exit_code') == 0)}

    def proxy_hashed(self, reported: dict) -> dict:
        """A4 (i) for a proxy run: its sender hashed every byte of the snapshot's selection, by pgdelta's own
        counters (files and pages hashed) against the snapshot's digest (its files, and their pages of 8 kB)."""
        entries = ((self.snapshot_digest or {}).get('digest') or {}).get('entries') or {}
        block = 8192
        pages = sum(-(-entry[1] // block) for entry in entries.values())
        files_hashed = (reported.get('files') or {}).get('total')
        pages_hashed = (reported.get('pages') or {}).get('total')
        return {'files_selected': len(entries), 'files_hashed': files_hashed, 'pages_selected': pages,
                'pages_hashed': pages_hashed,
                'all_hashed': bool(entries) and files_hashed == len(entries) and pages_hashed == pages}

    def verify_nothing(self, method, paths) -> dict:
        """EXP-12's arms with nothing on dest (none, bracket, the controls, capture): B0 unchanged. The capture's S
        is verified on the source with the next run's untimed source work (verified None until then)."""
        changes = measure.stat_changes(self.b0_stat, measure.tree_stat(self.b0))
        result = {'basis_changes': changes, 'verified': not changes}
        if method == 'capture':
            result.update(verified=False if changes else None,
                          capture_check="pending: pg_verifybackup of S on the source, before the next settle")
        return result

    # --- T10a (EXP-12): the oracle (G4) ------------------------------------------------------

    def copy_check(self, body) -> dict:
        """One check of the load's oracle on a started copy, by its own verdict ('ok' or 'wrong'). An error the copy's
        server or tools return is 'wrong'; only a harness-side failure, a timeout or a missing binary, 'unverified'."""
        try:
            return body()
        except (CommandError, ValueError) as exc:  # an error, or an answer that doesn't read: the copy's
            return {'status': 'wrong', 'error': f'{type(exc).__name__}: {exc}'}
        except subprocess.TimeoutExpired as exc:
            return {'status': 'unverified', 'error': f'timed out: {exc}'}
        except OSError as exc:
            return {'status': 'unverified', 'error': f'{type(exc).__name__}: {exc}'}

    def tool_text(self, argv, log_name: str, *, timeout: float = 3600):
        """A PostgreSQL tool run here against a copy or the source: (exit code, stdout, stderr), logged."""
        argv = [str(part) for part in argv]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                                env=server_env())
        log = self.dest_log(log_name)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f'$ {shlex.join(argv)}\n{result.stdout}{result.stderr}')
        return result.returncode, result.stdout, result.stderr

    def schema_hashes(self, host, port: int, label: str) -> dict:
        """(vii): SHA-256 of `pg_dump --schema-only --restrict-key=exp12` of loaddb and of postgres; CommandError if
        pg_dump fails. Each dump is kept beside the logs."""
        hashes = {}
        for database in measure.SCHEMA_DATABASES:
            argv = measure.schema_dump_argv(self.bin / 'pg_dump', host=host, port=port, user=PG_USER, database=database)
            code, out, err = self.tool_text(argv, f'{label}-{database}.sql')
            if code != 0:
                raise CommandError(f'pg_dump of {database} failed ({code}): {err.strip()[-1000:]}')
            hashes[database] = hashlib.sha256(out.encode()).hexdigest()
        return hashes

    def oracle_checks(self, method, *, static_digest: bool, mutate=None) -> dict:
        """The load's oracle on a started copy (port OUTPUT_PG_PORT): (iii) the invariant, with each client's n for
        the cut; (vi) the benchmark database's row counts, or its logical digest; pg_amcheck --heapallindexed of
        loaddb; (vii) the schema dumps' hashes. Each check classified by copy_check; none raises.

        `mutate`, for the oracle's negative controls, changes the started copy first.
        """
        host, port = self.output_socket, OUTPUT_PG_PORT
        checks = {}
        if mutate is not None:
            checks['mutated'] = self.copy_check(lambda: {'status': 'ok', 'statements': mutate()})

        def invariant():
            answer = json.loads(self.query(measure.load_oracle_sql(), host=host, port=port, database=measure.LOAD_DATABASE,
                                           log_name=f'{method}-oracle-invariant.log'))
            verdict = measure.atomicity(answer)
            return {'status': 'ok' if verdict['ok'] else 'wrong', **verdict, 'sum_abalance': answer.get('sum_abalance'),
                    'sum_n': answer.get('sum_n'), 'clients': answer.get('clients'), 'progress': answer.get('n')}

        def benchmark():
            reference = self.references['logical']
            if static_digest:
                digest = json.loads(self.query(measure.logical_digest_sql(), host=host, port=port,
                                               log_name=f'{method}-oracle-digest.log'))
                same, differences = measure.compare_logical(reference, digest)
                return {'status': 'ok' if same else 'wrong', 'digest': True, 'differences': differences}
            counts = json.loads(self.query(measure.row_counts_sql(), host=host, port=port,
                                           log_name=f'{method}-oracle-rows.log'))
            same, differences = measure.compare_rows(reference, counts)
            return {'status': 'ok' if same else 'wrong', 'digest': False, 'counts': counts, 'differences': differences}

        def amcheck():
            argv = measure.amcheck_argv(self.bin / 'pg_amcheck', host=host, port=port, user=PG_USER)
            code, out, err = self.tool_text(argv, f'{method}-oracle-amcheck.log')
            return measure.amcheck_result(code, out + err)

        def schema():
            hashes = self.schema_hashes(host, port, f'{method}-oracle-schema')
            differs = sorted(db for db, digest in hashes.items() if digest != self.references['schema'].get(db))
            return {'status': 'wrong' if differs else 'ok', 'hashes': hashes, 'differ': differs}

        checks.update(invariant=self.copy_check(invariant), benchmark=self.copy_check(benchmark),
                      amcheck=self.copy_check(amcheck), schema=self.copy_check(schema))
        return checks

    @staticmethod
    def oracle_summary(start: dict) -> dict:
        """A started copy's oracle as one status per check (measure.ORACLE_CHECKS), the cut and coverage pending.

        A copy that doesn't start or answer is 'wrong' on start, unless its start timed out ('unverified'); the
        checks it then couldn't run stay None.
        """
        checks = start.get('checks') or {}
        if start.get('ok'):
            started = 'ok'
        else:
            started = 'unverified' if start.get('error_kind') == 'timeout' or start.get('harness_error') else 'wrong'
        status = {'start': started, 'cut': None, 'coverage': None}
        for check, key in (('atomic', 'invariant'), ('rows', 'benchmark'), ('amcheck', 'amcheck'),
                           ('schema', 'schema')):
            status[check] = (checks.get(key) or {}).get('status') if started == 'ok' else None
        invariant = checks.get('invariant') or {}
        return {**status, 'progress': invariant.get('progress'), 'static_digest': (checks.get('benchmark') or {}).get(
            'digest'), 'amcheck_relations': (checks.get('amcheck') or {}).get('relations_checked')}

    def static_digest_due(self, method) -> bool:
        """(vi)'s logical digest in each (arm, speed) cell's first repeat (--static-digest first_repeat), in every
        run (every), or never (none); row counts are taken in every run."""
        policy = self.args.static_digest
        if policy == 'none':
            return False
        if policy == 'every':
            return True
        cell = (method, (self.current_run or (None, None))[1])
        if cell in self.static_digests:
            return False
        self.static_digests.add(cell)
        return True

    def verify_live(self, method, paths, phases) -> dict:
        """EXP-12's verification of a live output, full or staged pgdelta (G4), on dest only, after the clock.

        (i) pg_verifybackup with its CRC32C manifest (O with S's, for pgdelta, whose O is byte-compared with S on the
        source before the next settle: identical_to_capture is None until then); its bytes and WAL counted; then
        (ii) started on port 5433 with no TCP, no archive and no restore_command, recovering from its backup_label
        with only the WAL it holds; the oracle on it while it runs (oracle_checks); B0 unchanged. The cut (iv) and
        coverage (v) wait for the logs, at the trial's end.
        """
        output = Path(paths['output'])
        check = self.local_command([self.bin / 'pg_verifybackup', output], f'{method}-verify.log')
        result = {'pg_verifybackup_exit_code': check['exit_code'], 'pg_verifybackup_seconds': check['wall_seconds']}
        if method in measure.STAGED_METHODS:
            digest = measure.tree_digest(output)
            independent, problems = measure.independence(digest, self.b0_digest)
            self.output_digests[(*self.current_run, method)] = digest
            result.update(identical_to_capture=None, independent_of_basis=independent, independence_problems=problems,
                          output_digest=measure.digest_summary(digest), output_bytes=digest['bytes'])
            wal_files = measure.wal_files_from_digest(digest)
        else:
            result['output_bytes'] = tree_bytes(output)
            wal_files = measure.wal_files(output)
        try:
            manifest = (output / 'backup_manifest').read_text()
        except OSError:
            manifest = None
        try:
            result['wal'] = measure.wal_summary(wal_files, manifest)
        except (ValueError, KeyError, TypeError) as exc:
            result['wal'] = {'wal_bytes': sum(wal_files.values()), 'error': str(exc)}
        result['shared_extents'] = measure.shared_extents(output)
        digest_due = self.static_digest_due(method)
        try:
            result['output_start'] = self.check_output(
                method, output, recovery=True, checks=lambda: self.oracle_checks(method, static_digest=digest_due))
        except OSError as exc:  # a binary missing on dest: the harness's side
            result['output_start'] = {'ok': False, 'error': f'{type(exc).__name__}: {exc}', 'harness_error': True}
        result['oracle'] = self.oracle_summary(result['output_start'])
        result['basis_changes'] = measure.stat_changes(self.b0_stat, measure.tree_stat(self.b0))
        bytes_ok = (check['exit_code'] == 0 and result.get('independent_of_basis', True))
        result['oracle']['bytes'] = ('wrong' if not bytes_ok else None if method in measure.STAGED_METHODS else 'ok')
        result['verified'] = bytes_ok and result['output_start'].get('ok') and not result['basis_changes']
        return result

    def g4_boundaries(self, method, record) -> dict:
        """G4's boundaries of a live output: its capture's end (the write-ahead log end point's LSN, and when
        pg_basebackup printed it, on dest's clock), publication (the clock's stop) and usable (plus the start's time to
        consistency). Coverage and staleness follow from the cut, at the trial's end."""
        phases = record.get('phases') or {}
        command = None
        for phase in phases.values():
            command = next((item for item in phase.get('commands', ())
                            if Path(str(item['argv'][0])).name == 'pg_basebackup'), None) or command
        end = None
        if command is not None:
            try:
                lines = measure.parse_stamped_lines(Path(command['log']).read_text(errors='replace'))
                end = measure.wal_end_point(lines, command.get('started_utc'))
            except (OSError, KeyError):
                end = None
        window = record.get('window') or {}
        ready = measure._get(record, 'verification', 'output_start', 'ready_seconds')
        return {'capture_end': end, 'publication_utc': window.get('t1_utc'),
                'usable_utc': None if ready is None or window.get('t1_utc') is None else window['t1_utc'] + ready,
                'coverage_window_utc': self.coverage_window(method, record)}

    @staticmethod
    def coverage_window(method, record) -> list | None:
        """(v)'s window on dest's clock: full's from its backup's start (its phase's) to its clock stop; staged
        pgdelta's its capture's start to its end."""
        phases = record.get('phases') or {}
        if method == 'pgdelta':
            part = measure._get(phases, measure.RECEIVER_PHASE, 'parts', 'capture')
            if not part or part.get('started_utc') is None:
                return None
            return [part['started_utc'], part['started_utc'] + part['wall_seconds']]
        first = next(iter(phases.values()), None)
        last = list(phases.values())[-1] if phases else None
        if not first or first.get('started_utc') is None or last.get('ended_utc') is None:
            return None
        return [first['started_utc'], last['ended_utc']]

    def set_load_status(self, record: dict) -> None:
        """A run's status under EXP-12 from its verification: a live output's by its oracle (measure.oracle_status),
        the capture's by its S check, any other's by verify (pass or fail). An error, or a copy its own tool aborted
        (wrong already: tool_aborted), keeps its status."""
        verification = record.get('verification') or {}
        if record.get('status') == 'error' or record.get('failure_kind') == 'tool':
            return
        if 'oracle' in verification:
            status = measure.oracle_status(verification['oracle'])
            record['status'] = status
            wrong = [check for check, value in verification['oracle'].items()
                     if value == 'wrong' and check in (*measure.ORACLE_CHECKS, 'bytes')]
            record['error'] = f'wrong: {", ".join(wrong)}' if wrong else None
            return
        verified = verification.get('verified')
        record['status'] = 'pending' if verified is None else 'pass' if verified else 'fail'
        record['error'] = None if verified is not False else (verification.get('error') or 'output verification failed')

    # --- T10a (EXP-12): one run under the load ------------------------------------------------

    @property
    def benchmark_files(self) -> str:
        """The benchmark database's files on the source (base/ of postgres, about 2.1 GB)."""
        return f'{self.source_data}/base/{self.database_oids["postgres"]}'

    @property
    def loaddb_files(self) -> str:
        return f'{self.source_data}/base/{self.database_oids[measure.LOAD_DATABASE]}'

    def static_files(self) -> list[str]:
        """What every cold window reads cold: the benchmark database's files and, with --snapshot, the snapshot."""
        return [self.benchmark_files, *([self.snapshot_data] if self.args.snapshot else [])]

    def cache_state(self) -> dict:
        """fincore of the benchmark database's files, the snapshot and loaddb's files: what is resident now."""
        names = {'benchmark': self.benchmark_files, 'loaddb': self.loaddb_files}
        if self.args.snapshot:
            names['snapshot'] = self.snapshot_data
        result = self.source.agent('fincore', *names.values(), timeout=600)
        return {name: result['paths'].get(path) for name, path in names.items()}

    def source_cache_step(self, warm: bool) -> dict:
        """The source's cache before a run's settle: the static files evicted (POSIX_FADV_DONTNEED; the load's
        database, pg_xact and pg_wal left alone), or in the warm block read once instead; then fincore of each."""
        if self.args.source_cache == 'keep':
            return {'policy': 'keep: the source cache is left as it is (no eviction, no fincore)'}
        started = time.perf_counter()
        if warm:
            action = {'read': [self.source.agent('read-test', path, '--readers', str(READ_FLOOR_READERS), timeout=3600)
                               for path in self.static_files()]}
        else:
            action = {'evicted': self.source.agent('evict', *self.static_files(), timeout=600)}
        fincore = self.cache_state()
        if not warm and measure.cache_flag(fincore, warm):
            # EXP-12's smoke item 8: more than 1% still resident; once more, after a sync -f of the files (dirty pages
            # can't be dropped), and the second reading is the one that counts.
            action['first_fincore'] = fincore
            action['evicted_again'] = self.source.agent('evict', '--sync', *self.static_files(), timeout=600)
            fincore = self.cache_state()
        return {'policy': 'warm' if warm else 'evict', **action, 'fincore': fincore,
                'cache_flag': measure.cache_flag(fincore, warm), 'seconds': round(time.perf_counter() - started, 3)}

    def before_settle(self, method, paths, record, daemon_holder) -> dict:
        """A run's step 1, on the source, untimed, before its settle: the previous runs' source work (S verified and
        removed, pgdelta's statistics read); the load checked alive (restarted if it died); this method's leftovers
        removed and its stats directory made; the source stopped cleanly for v1 itself (calibration); the eviction
        (or the warm read) and fincore."""
        started = time.perf_counter()
        result = {'pending': self.flush_pending_source()}
        if self.load_current is not None:
            result['load'] = self.check_load()
        remote = [paths[key] for key in ('capture', 'stats') if key in paths]
        if remote:
            self.remove_source(*remote)
        if method in measure.PGDELTA_METHODS or method in measure.PARCOPY_METHODS:
            self.source.agent('run', '--log', self.source_log(f'{method}-mkdir-stats.log'), '--', 'mkdir', '-p',
                              f'{self.work}/stats')
        if method in measure.SOURCE_STOP_METHODS:
            daemon_holder['source_stopped'] = True  # set first: a failed stop is still restarted
            result['stop'] = self.source.script(SOURCE_CLUSTER, 'stop-clean', '--root', self.source_root,
                                                '--bin-dir', str(self.bin))
        result['cache'] = self.source_cache_step(record['source_cache'] == 'warm')
        result['seconds'] = round(time.perf_counter() - started, 3)
        return result

    def dest_setup(self, method, paths, daemon_holder) -> dict:
        """Dest's work in the settle: this run's set-up (B for the proxies and v1 itself: cp -a --reflink=never B0 B;
        sync -f B), then dest's cache drop. Never inside a window."""
        started = time.perf_counter()
        steps = []
        if method in measure.BASIS_COPY_METHODS:
            steps.append(self.copy_basis(method, paths))
        if method in measure.HOST_TUNED_METHODS:
            daemon_holder['dirty_writeback'] = None
            daemon_holder['dirty_writeback'] = self.set_dirty_writeback(method)
            steps.append({'what': "set dest's write-back for the run (reset after it)",
                          **daemon_holder['dirty_writeback']})
        steps.append({'what': "dest's cache dropped", **self.drop_caches(source=False)})
        return {'steps': steps, 'seconds': round(time.perf_counter() - started, 3)}

    def settle(self, method, paths, record, daemon_holder) -> dict:
        """A run's settle: at least --settle-s with nothing on the source but the load, the sampler and the counter
        reads, until the source's gate passes (agent.py settle-gate, capped at --settle-max-s: then 'unsettled')
        and dest's work is done (uncapped; its wait is dest_wait_s). Dest works first, so no window starts before it
        ends; the gate then waits out what is left of the minimum. Without the load (calibration) there is no gate.
        The source's CPU over the settle gives its rate (cpu_source_excess_s's)."""
        started_utc, started = time.time(), time.perf_counter()
        before = self.snapshot_source()
        dest = self.dest_setup(method, paths, daemon_holder)
        dest_wait = time.perf_counter() - started
        gate = None
        if self.load_current is not None:
            elapsed = time.perf_counter() - started
            gate = self.source.agent('settle-gate', '--rate', str(self.load_rate), '--load-dir', self.load_dir,
                                     '--min-s', f'{max(0.0, self.args.settle_s - elapsed):.3f}',
                                     '--max-s', f'{max(0.0, self.args.settle_max_s - elapsed):.3f}',
                                     timeout=self.args.settle_max_s + self.args.settle_s + 120)
        else:
            self.pause(self.args.settle_s - (time.perf_counter() - started))
        after = self.snapshot_source()
        cpu = measure.snapshot_delta(before, after)
        busy = measure.busy_seconds(cpu['cpu'])
        interval = cpu['interval_seconds']
        return {'started_utc': started_utc, 'ended_utc': time.time(),
                'seconds': round(time.perf_counter() - started, 3), 'dest': dest, 'dest_wait_s': round(dest_wait, 3),
                'gate': gate, 'unsettled': bool(gate and gate.get('unsettled')),
                'cpu_source': cpu['cpu'], 'cpu_window_seconds': interval,
                'busy_rate_no_steal': round(busy / interval, 6) if busy is not None and interval else None}

    def window(self, phases: dict) -> dict:
        """The run's window: the UTC start of its first timed phase (t0) and end of its last (t1), on dest's clock and
        mapped to the source's with the latest offset; the slowdown window runs to t1 plus --window-tail-s."""
        values = list(phases.values())
        t0, t1 = values[0].get('started_utc'), values[-1].get('ended_utc')
        offset = self.clock_offset_s
        return {'t0_utc': t0, 't1_utc': t1, 'clock_offset_s': offset,
                't0_source_utc': None if t0 is None or offset is None else round(t0 + offset, 6),
                't1_source_utc': None if t1 is None or offset is None else round(t1 + offset, 6),
                'tail_s': self.args.window_tail_s}

    def quiet_tail(self, clock_stopped: float, clock_stopped_utc: float, seconds: float, window=None) -> dict:
        """The quiet tail: `seconds` from the clock's stop with nothing on the source but the load. Dest's
        verification ran inside it, from the clock's stop (and after (a) and (b)); what is left is waited out.

        Then, with the run's window, until the last 1-s bucket of the analysis's 15-s-tail view has ended too: it
        ends at t0 + ceil(t1 + seconds - t0), up to 1 s past t1 + seconds, and the clock stops a counter read after
        t1, so the view would otherwise hold the harness's next step on the source (`view_end_utc`, on dest's clock;
        the offset maps t0 and the view's end alike, so it cancels), plus TAIL_VIEW_MARGIN_S."""
        used = time.perf_counter() - clock_stopped
        self.pause(seconds - used)
        view_end = None
        if seconds > 0 and window and window.get('t0_utc') is not None and window.get('t1_utc') is not None:
            t0, t1 = window['t0_utc'], window['t1_utc']
            view_end = t0 + max(1, math.ceil(t1 + seconds - t0 - 1e-9)) + TAIL_VIEW_MARGIN_S
            self.pause(view_end - time.time())
        return {'started_utc': clock_stopped_utc, 'ended_utc': time.time(), 'seconds': seconds,
                'view_end_utc': None if view_end is None else round(view_end, 6),
                'verification_s': round(used, 3), 'overran_s': round(max(0.0, used - seconds), 3)}

    def load_tool_reported(self, method, paths, phases) -> dict:
        """What a run reports that dest holds (pgdelta's statistics, on the source, are read before the next settle)."""
        if method in measure.FULL_VARIANTS:
            return self.basebackup_reported(method, phases)
        if method == 'capture':
            command, lines = self.stamped_command(phases, measure.CAPTURE_PHASE, 'pg_basebackup')
            return {'bytes_source_to_dest': None, 'bytes_dest_to_source': None, 'scope': 'a local capture: no link',
                    'flags': self.method_flags(method),
                    'basebackup_phases': measure.basebackup_phases(lines, (command or {}).get('wall_seconds')),
                    'lines': [[seconds, text] for seconds, text in lines]}
        if method == 'bracket':
            command = next(iter(phases[measure.BRACKET_PHASE]['commands']), {})
            timings = measure.psql_timings(command.get('log_tail', ''))
            return {'bytes_source_to_dest': None, 'bytes_dest_to_source': None, 'scope': 'no copy',
                    'pg_backup_start_s': timings[0] if timings else None,
                    'pg_backup_stop_s': timings[1] if len(timings) > 1 else None}
        if method in measure.CONTROL_METHODS:
            return {'bytes_source_to_dest': None, 'bytes_dest_to_source': None, 'scope': 'a control',
                    'control': phases[measure.CONTROL_PHASE].get('control')}
        return {'bytes_source_to_dest': None, 'bytes_dest_to_source': None, 'scope': 'nothing runs'}

    def tool_aborted(self, method, phases, record) -> bool:
        """A command of the run's timed phases failed (exited non-zero or timed out). The harness's own failure
        (harness_failure: SSH, or a stamped command's agent without its result) raises, an error of kind 'harness'
        (unverified). Any other is the tool's (failure_kind 'tool'): for an arm with an output
        (measure.TOOL_ABORT_WRONG_METHODS) the copy it aborted doesn't exist, so it can't start, and the run is
        wrong (status 'fail'; R3 reads it); for any other arm it raises, an error of kind 'tool'. True when the run
        is wrong this way, so nothing is verified."""
        problems = [problem for phase in phases.values() for problem in phase['failures']]
        if not problems:
            return False
        commands = [command for phase in phases.values() for command in phase.get('commands', ())]
        if any(harness_failure(command) for command in commands):
            raise CommandError('; '.join(problems))
        record['failure_kind'] = 'tool'
        if method not in measure.TOOL_ABORT_WRONG_METHODS:
            raise CommandError('; '.join(problems))
        record['status'] = 'fail'
        record['error'] = 'wrong: the copy itself failed (no output to start): ' + '; '.join(problems)
        log(f'  WRONG {record["error"]}')
        return True

    def verify_load_run(self, method, paths, phases, record) -> None:
        """Dest's verification of a run under EXP-12, inside the tail: after the clock and after (a) and (b)'s last
        read; then the run's status (set_load_status)."""
        record['tool_reported'] = (None if method in measure.PGDELTA_METHODS
                                   else self.load_tool_reported(method, paths, phases))
        if self.args.syscall_audit:
            record['audit'] = self.syscall_audit(method, paths, phases)
        verification = self.verify(method, paths, phases)
        record['verification'] = verification
        record['output_bytes'] = verification.get('output_bytes')
        record['capture_bytes'] = verification.get('capture_bytes')
        record['wal'] = verification.get('wal')
        padding = (record['wal'] or {}).get('wal_padding_bytes')
        record['output_minus_wal_padding_bytes'] = (
            None if record['output_bytes'] is None or padding is None else record['output_bytes'] - padding)
        record['durability'] = measure.durability_check(record, record['output_bytes'], self.idle_noise_bytes)
        if method in measure.G4_METHODS:
            record['g4'] = self.g4_boundaries(method, record)
        if method in measure.V1PROXY_METHODS:
            record['proxy'] = {'backup': phases[measure.V1PROXY_PHASE].get('backup'), 'hashed': None}
        if verification['basis_changes']:
            raise CommandError('B0 changed: ' + '; '.join(verification['basis_changes']))
        self.set_load_status(record)

    def run_load_method(self, trial: dict, repeat: int, speed: dict, link: dict, method: str,
                        order_index: int, order: list[str]) -> dict:
        """One run under EXP-12's timeline (its "Each run"):

        1. the source, untimed, before the settle (before_settle): the previous runs' source work, the load
           checked, this method's source set-up, the eviction (or the warm read) and fincore;
        2. the settle (settle): dest's set-up and cache drop, then the source's gate; nothing on the source but the
           load, the sampler and the counter reads;
        3. the timed phase or phases, dest's counters for checks (a) and (b) read just before the clock starts and
           first after it stops (timed_phase); t0 and t1 in UTC (window);
        4. the quiet tail (quiet_tail): dest's verification, which starts only now and touches nothing on the source,
           then the rest of --tail-s waited out. The source's part of the verification waits for the next run's
           step 1 (pending_source).

        In the calibration block (no load) there is no gate and no tail. The source is stopped and restarted only for
        v1 itself (SOURCE_STOP_METHODS), and only there.
        """
        paths = self.method_paths(method)
        loaded = self.load_current is not None
        record = {'schema': SCHEMA_RUN, 'run_name': self.out.name, 'trial_id': self.trial_id,
                  'pattern': trial['pattern'], 'fraction': trial['fraction'], 'scale': trial['scale'],
                  'rows_changed': trial.get('rows_changed'), 'repeat': repeat, 'speed': speed,
                  'speed_index': link.get('speed_index'), 'speed_order': link.get('speed_order'), 'method': method,
                  'order_index': order_index, 'method_order': order,
                  'link': {key: link[key] for key in link if key.endswith('gbit')},
                  'paths': {key: str(value) for key, value in paths.items()},
                  'block': link.get('block'), 'block_repeat': link.get('block_repeat'),
                  'control_slot': link.get('control_slot'), 'source_cache': link.get('source_cache', 'cold'),
                  'load_rate': self.load_rate if loaded else None,
                  'load_run': (self.load_current or {}).get('run_id'), 'clock_offset_s': self.clock_offset_s,
                  'idle_rate_no_steal': self.idle_rate, 'started_utc': utc_now(), 'status': 'running', 'error': None,
                  'failure_kind': None, 'void': False}
        log(f'{self.trial_id} r{repeat} {speed["label"]} [{order_index + 1}/{len(order)}] {method} ({record["block"]}'
            + (f', {record["control_slot"]}' if record['control_slot'] else '') + ')')
        daemon_holder: dict = {}
        self.log_prefix = f'r{repeat}-{speed["label"]}-'
        self.current_run = (repeat, speed['label'])
        tail_seconds = self.args.tail_s if loaded else 0.0
        try:
            stuck = self.output_stuck
            if stuck and (str(paths['output']) == stuck['data'] or method in measure.OUTPUT_START_METHODS
                          or method in measure.G4_METHODS):
                raise CommandError(f'an output started on port {OUTPUT_PG_PORT} for an earlier verification '
                                   f'could not be stopped ({stuck["data"]}); {method} cannot run')
            self.remove_dest(*[paths[key] for key in ('output', 'increment', 'receipt', 'receive_stats')
                               if key in paths])
            phases: dict = {}
            record['phases'] = phases
            if self.writeback_defaults is not None:
                sysctl = record['sysctl'] = {}
                sysctl['before'] = self.check_dirty_writeback(sysctl)
            record['source_untimed'] = self.before_settle(method, paths, record, daemon_holder)
            # A load found dead in step 1 was restarted there (check_load): this run's settle and window are the
            # restart's, so its log is the one the analysis reads them from.
            record['load_run'] = (self.load_current or {}).get('run_id')
            record['load_rate'] = self.load_rate if self.load_current is not None else None
            record['caches_dropped'] = 'dest'
            record['settle'] = self.settle(method, paths, record, daemon_holder)
            if daemon_holder.get('dirty_writeback'):
                record.setdefault('sysctl', {})['during'] = daemon_holder['dirty_writeback']['values']
            phases.update(self.run_phases(method, paths, daemon_holder))
            stopped, stopped_utc = time.perf_counter(), time.time()
            self.complete_stamped(phases)
            record['window'] = self.window(phases)
            record['totals'] = measure.phase_totals(phases)
            if method in measure.STAGED_METHODS:
                record['totals_serial'] = measure.serial_totals(phases)
            if not self.tool_aborted(method, phases, record):
                # 4. Dest's verification, inside the tail: after the clock and after (a) and (b)'s last read.
                self.verify_load_run(method, paths, phases, record)
            record['tail'] = self.quiet_tail(stopped, stopped_utc, tail_seconds, record['window'])
        except Exception as exc:  # record, clean up, carry on with the next method
            record['status'] = 'error'
            record['failure_kind'] = record['failure_kind'] or 'harness'
            record['error'] = f'{type(exc).__name__}: {exc}'
            record['traceback'] = traceback.format_exc()
            log(f'  ERROR {record["error"]}')
        finally:
            if 'dirty_writeback' in daemon_holder:
                del daemon_holder['dirty_writeback']
                try:
                    record.setdefault('sysctl', {})['reset'] = self.reset_dirty_writeback()
                except Exception as exc:
                    record['sysctl']['reset'] = {'error': f'{type(exc).__name__}: {exc}'}
                    record['status'], record['failure_kind'] = 'error', record['failure_kind'] or 'harness'
            if daemon_holder.pop('source_stopped', False):
                try:
                    record['source_restart'] = self.restart_source()
                except Exception as exc:
                    self.source_down = f'{type(exc).__name__}: {exc}'
                    record['source_restart'] = {'error': self.source_down}
                    record['status'], record['failure_kind'] = 'error', record['failure_kind'] or 'harness'
                    record['error'] = (record['error'] + '; ' if record['error'] else '') + \
                        f'the source cluster could not be restarted: {self.source_down}'
            if self.output_stuck and self.output_stuck['data'] == str(paths['output']):
                record['output_stuck'] = self.output_stuck
            if not self.args.keep_outputs:
                try:
                    stuck = (self.output_stuck or {}).get('data')
                    self.remove_dest(*[paths[key] for key in ('output', 'increment', 'receipt', 'receive_stats')
                                       if key in paths and str(paths[key]) != stuck])
                except Exception as exc:
                    record['cleanup_error'] = str(exc)
            # The source's part: S verified and removed, pgdelta's statistics read, before the next settle.
            if any(key in paths for key in ('capture', 'stats')):
                self.pending_source.append({'record': record, 'paths': paths})
        self.log_prefix = ''
        self.current_run = None
        record['finished_utc'] = utc_now()
        totals = record.get('totals') or {}
        log(f'  {record["status"]}: {totals.get("wall_seconds")} s'
            + (', unsettled' if (record.get('settle') or {}).get('unsettled') else ''))
        save_json(self.record_path(record), record)
        self.records.append(record)
        measure.write_csv(self.out / 'summary.csv', self.records)
        return record

    def flush_pending_source(self) -> list:
        """The source's part of the runs before this one, each in order (source_half); their records rewritten."""
        done = []
        while self.pending_source:
            item = self.pending_source.pop(0)
            try:
                done.append(self.source_half(item))
            except Exception as exc:  # the harness's own failure, on that run: the next run goes ahead
                record = item['record']
                record.update(status='error', failure_kind=record.get('failure_kind') or 'harness',
                              error=f'its source part failed: {type(exc).__name__}: {exc}',
                              traceback=traceback.format_exc())
                save_json(self.record_path(record), record)
                self.update_trial_run(record)
                done.append({'run': self.run_key(record), 'error': record['error']})
        if done:
            measure.write_csv(self.out / 'summary.csv', self.records)
        return done

    def source_half(self, item: dict) -> dict:
        """One run's source work, untimed, with nothing measured: pgdelta's statistics (tool_reported, the untimed
        prefetch, the proxy's A4 (i)); S checked with pg_verifybackup on the source and, for staged pgdelta, its
        SHA-256 digest against O's, taken on dest; then S and the statistics removed. An SSH failure leaves the S
        check unverified; S's own failure makes the run wrong."""
        record, paths = item['record'], item['paths']
        method = record['method']
        verification = record.get('verification')
        done: dict = {'run': self.run_key(record)}
        prefix, self.log_prefix = self.log_prefix, f'r{record["repeat"]}-{record["speed"]["label"]}-'
        try:
            if 'stats' in paths and record.get('status') != 'error' and verification is not None:
                try:
                    reported = self.tool_reported(method, paths, record.get('phases') or {})
                    record['tool_reported'] = reported
                    if method in measure.STAGED_METHODS:
                        record['prefetch_untimed'] = measure.prefetch_untimed(record['phases'], reported.get('prefetch'))
                    if method in measure.V1PROXY_METHODS:
                        record.setdefault('proxy', {})['hashed'] = self.proxy_hashed(reported)
                    done['stats'] = True
                except (CommandError, ValueError, KeyError) as exc:
                    record['tool_reported'] = {'error': f'{type(exc).__name__}: {exc}'}
                    done['stats'] = False
            if 'capture' in paths and record.get('status') != 'error' and verification is not None:
                done['capture'] = self.check_capture(method, paths, record, verification)
        finally:
            try:
                self.remove_source(*[paths[key] for key in ('capture', 'stats') if key in paths])
            except Exception as exc:
                done['cleanup_error'] = str(exc)
            self.log_prefix = prefix
        self.set_load_status(record)
        save_json(self.record_path(record), record)
        self.update_trial_run(record)
        return done

    def check_capture(self, method, paths, record, verification) -> dict:
        """S on the source: pg_verifybackup, and for staged pgdelta O's digest against S's (identical_to_capture)."""
        try:
            check = self.source.agent('run', '--log', self.source_log(f'{method}-capture-verify.log'), '--',
                                      str(self.bin / 'pg_verifybackup'), paths['capture'], timeout=3600)
            verification['capture_verifybackup_exit_code'] = check['exit_code']
            s_ok = check['exit_code'] == 0
            if method in measure.STAGED_METHODS:
                capture = self.source.agent('digest', paths['capture'], timeout=3600)
                output = self.output_digests.pop((record['repeat'], record['speed']['label'], method), None)
                identical, differences = (measure.compare_digests(capture, output) if output is not None
                                          else (None, ['no digest of O was taken']))
                verification.update(identical_to_capture=identical, differences=differences,
                                    capture_digest=measure.digest_summary(capture), capture_bytes=capture['bytes'])
                record['capture_bytes'] = capture['bytes']
                verification['oracle']['bytes'] = ('ok' if s_ok and identical and verification['oracle'].get('bytes')
                                                   is None else 'wrong')
            else:
                verification['verified'] = s_ok and not verification['basis_changes']
            return {'pg_verifybackup': check['exit_code'], 'identical': verification.get('identical_to_capture')}
        except (CommandError, subprocess.TimeoutExpired) as exc:  # SSH or a timeout: the harness's side
            verification['capture_check_error'] = f'{type(exc).__name__}: {exc}'
            if 'oracle' in verification:
                verification['oracle']['bytes'] = 'unverified'
            else:
                verification['verified'] = False
                verification['error'] = f'S could not be checked: {exc}'
            return {'error': verification['capture_check_error']}

    def update_trial_run(self, record: dict) -> None:
        """The trial's list of runs, with this run's status as it now stands."""
        for run in (self.current_trial or {}).get('runs', ()):
            if (run['repeat'], run['speed'], run['method']) == (record['repeat'], record['speed']['label'],
                                                                 record['method']):
                run['status'] = record['status']

    # --- T10a (EXP-12): the load ---------------------------------------------------------------

    def load_args(self) -> list[str]:
        return ['--root', self.source_root, '--bin-dir', str(self.bin), '--load-dir', self.load_dir]

    def load_pgbench_args(self, role: str) -> list[str]:
        """pgbench's clients, threads, scripts, and a seed per role (LOAD_SEED_OFFSETS) from --load-seed."""
        return ['--clients', str(self.args.load_clients), '--threads', str(self.args.load_threads), '--seed',
                str(self.load_seed(role)), '--scripts', f'{self.remote_repo}/{LOAD_SCRIPT_DIR}']

    def load_seed(self, role: str) -> int:
        """pgbench's --random-seed for a run: --load-seed (default --order-seed) plus its role's offset; each restart
        its own."""
        base = self.args.load_seed if self.args.load_seed is not None else self.args.order_seed
        if role == 'restart':
            return base + LOAD_SEED_OFFSETS['restart'] + len(self.load_runs)
        return base + LOAD_SEED_OFFSETS[role]

    def load_start(self, run_id: str, rate: int, role: str) -> dict:
        """A pgbench run at `rate` (load-start: the counters first, then pgbench under setsid on the source)."""
        log(f'{self.trial_id}: the load at {rate}/s ({run_id})')
        run = self.source.script(SOURCE_CLUSTER, 'load-start', *self.load_args(), *self.load_pgbench_args(role),
                                 '--run-id', run_id, '--rate', str(rate), '--duration-s',
                                 str(self.args.load_duration_s), '--kind', role)
        self.load_runs.append(run)
        self.load_current, self.load_rate = run, rate
        return run

    def load_stop(self, *, death_known: bool = False) -> dict:
        """The current run stopped (SIGALRM, as its -T timer would), counted, its counters read again. If pgbench had
        already ended (load-stop's `how` 'ended') it died while the run before this stop ran, unnoticed, since
        check_load looks only before a run: died_before_stop voids that run (unless the death is known already:
        check_load's own stop)."""
        run = self.source.script(SOURCE_CLUSTER, 'load-stop', *self.load_args(), '--signal', self.args.load_signal)
        self.load_runs[:] = [run if item['run_id'] == run['run_id'] else item for item in self.load_runs]
        self.load_current, self.load_rate = None, None
        log(f'{self.trial_id}: the load stopped ({run["run_id"]}, {run.get("commits_total")} commits, '
            f'{"complete" if run.get("complete") else "INCOMPLETE"})')
        if run.get('how') == 'ended' and not death_known:
            self.died_before_stop(run)
        return run

    def died_before_stop(self, run: dict) -> None:
        """A planned stop (the switch to the moderate load, step 15, the clean-up) found the load's pgbench gone: the
        latest run of this trial on that load run may hold the death in its window, so it is void (A8), as check_load
        voids the run before a death it finds; recorded in trial['load']['died_before_stop'], beside the restarts."""
        died = run['run_id']
        previous = next((record for record in reversed(self.records) if record.get('trial_id') == self.trial_id
                         and record.get('load_run') == died), None)
        voided = None
        if previous is not None:
            previous['void'] = True
            previous['void_reason'] = f'the load ({died}) died before its stop'
            save_json(self.record_path(previous), previous)
            measure.write_csv(self.out / 'summary.csv', self.records)
            voided = self.run_key(previous)
        log(f'{self.trial_id}: the load ({died}) had died before its stop; voided {voided}')
        if self.current_trial is not None:
            self.current_trial.setdefault('load', {}).setdefault('died_before_stop', []).append(
                {'died': died, 'voided': voided, 'signalled_utc': run.get('signalled_utc'),
                 'stopped_utc': run.get('stopped_utc')})

    def load_warmup(self, trial: dict) -> dict:
        """--load-warmup-s with the load running and nothing else; its last 60 s go into the block's null pool."""
        started = time.time()
        self.pause(self.args.load_warmup_s)
        ended = time.time()
        warmup = {'run_id': (self.load_current or {}).get('run_id'), 'started_utc': started, 'ended_utc': ended,
                  'seconds': self.args.load_warmup_s, 'pool_utc': [max(started, ended - 60), ended]}
        trial['load']['warmups'].append(warmup)
        return warmup

    def load_capacity(self, trial: dict) -> dict:
        """Step 7: the trial's own C, closed loop, each run logged and offset; then --capacity-rest-s of rest."""
        result = self.source.script(SOURCE_CLUSTER, 'load-capacity', *self.load_args(),
                                    *self.load_pgbench_args('capacity'), '--run-id', 'capacity', '--warmup-s',
                                    str(self.args.capacity_warmup_s), '--seconds', str(self.args.capacity_s),
                                    '--runs', str(self.args.capacity_runs),
                                    timeout=(self.args.capacity_warmup_s + self.args.capacity_s)
                                    * (self.args.capacity_runs + 1) + 600)
        self.load_runs.extend(result['runs'])
        log(f'{self.trial_id}: C = {result.get("capacity_tps")} transactions per second')
        self.pause(self.args.capacity_rest_s)
        return {'capacity_tps': result.get('capacity_tps'), 'all_complete': result.get('all_complete'),
                'runs': [run['run_id'] for run in result['runs']], 'rest_s': self.args.capacity_rest_s}

    def check_load(self) -> dict:
        """Before each run: is the load's pgbench still running? If it died, the run before is void (its window may
        hold the death), and the load restarts at its rate, counters recorded, with its warm-up (A8)."""
        status = self.source.script(SOURCE_CLUSTER, 'load-status', '--load-dir', self.load_dir)
        if status.get('running'):
            return {'running': True}
        rate, died = self.load_rate, self.load_current['run_id']
        voided = None
        previous = next((record for record in reversed(self.records) if record.get('trial_id') == self.trial_id
                         and record.get('load_run') == died), None)
        if previous is not None:
            previous['void'] = True
            previous['void_reason'] = f'the load ({died}) was found dead after it'
            save_json(self.record_path(previous), previous)
            voided = self.run_key(previous)
        log(f'{self.trial_id}: the load ({died}) died; restarting it at {rate}/s')
        stopped = self.load_stop(death_known=True)
        restart = self.load_start(f'restart-{len(self.current_trial["load"]["restarts"]) + 1}', rate, 'restart')
        warmup = self.load_warmup(self.current_trial)
        entry = {'died': died, 'last_progress': status.get('last_progress'), 'voided': voided,
                 'stopped': stopped['run_id'], 'restart': restart['run_id'], 'warmup': warmup}
        self.current_trial['load']['restarts'].append(entry)
        return {'running': False, **entry}

    def sampler_start(self) -> dict | None:
        """The sampler on the source, from the busy load's start to the load's end (--sampler-interval-s 0: none, as
        smoke item 11's run without it)."""
        if self.args.sampler_interval_s <= 0:
            return None
        return self.source.agent('sampler', 'start', '--out', self.sampler_dir, '--load-dir', self.load_dir,
                                 '--interval', str(self.args.sampler_interval_s), '--psql', str(self.bin / 'psql'),
                                 '--host', f'{self.source_root}/s', '--port', str(PG_PORT), '--user', PG_USER,
                                 '--database', PG_DATABASE)

    def sampler_stop(self) -> dict | None:
        if self.args.sampler_interval_s <= 0:
            return None
        return self.source.agent('sampler', 'stop', '--out', self.sampler_dir, timeout=120)

    @property
    def load_logs(self) -> Path:
        """The load's logs and the sampler's data on dest, once fetched (step 15)."""
        return self.out / 'logs' / self.trial_id / 'load'

    def fetch_load_logs(self) -> dict:
        """The load's directory (its logs zstd-compressed, the registry and the sampler's) from the source's tmpfs."""
        target = self.load_logs
        target.mkdir(parents=True, exist_ok=True)
        pack = subprocess.run(self.source.argv(['tar', '-C', self.load_dir, '-cf', '-', '.']), capture_output=True,
                              timeout=3600)
        if pack.returncode != 0:
            raise CommandError(pack.stderr.decode(errors='replace')[-1000:])
        subprocess.run(['tar', '-C', str(target), '-xf', '-'], input=pack.stdout, check=True)
        files = [path for path in target.rglob('*') if path.is_file()]
        return {'dir': str(target), 'files': len(files), 'bytes': sum(path.stat().st_size for path in files)}

    def end_load(self, trial: dict) -> dict:
        """Step 15: the load stopped; --load-check-wait-s later, the source against every logged run (load-check);
        the sampler stopped; the logs and the sampler's data compressed and fetched to dest. Each step's result goes
        into trial['load']['end'] as it is done, so end_load_quietly knows what is left after a failure."""
        result = trial['load']['end'] = {}
        result['stop'] = self.load_stop()
        self.pause(self.args.load_check_wait_s)
        result['check'] = self.source.script(SOURCE_CLUSTER, 'load-check', *self.load_args())
        log(f'{self.trial_id}: load-check {"ok" if result["check"].get("ok") else "FAILED"}: '
            f'sum(abalance) {result["check"].get("sum_abalance")}, sum(n) {result["check"].get("sum_n")}, '
            f'{result["check"].get("expected_commits")} commits logged')
        result['sampler'] = self.sampler_stop()
        result['compress'] = self.compress_load_logs()
        result['fetched'] = self.fetch_load_logs()
        return result

    def compress_load_logs(self) -> dict:
        """The load's logs and the sampler's data zstd-compressed in place on the source (the registry kept)."""
        return self.source.agent('compress', self.load_dir, '--keep', measure.LOAD_STATE, timeout=3600)

    # --- T10a (EXP-12): the trial ----------------------------------------------------------------

    def next_repeat(self) -> int:
        repeat, self.repeat_counter = self.repeat_counter, self.repeat_counter + 1
        return repeat

    def run_load_block(self, trial: dict, block: str, repeats: int, speeds, methods, limited: dict) -> None:
        """One of EXP-12's blocks: each repeat visits its speeds in a new seeded order, and at each runs every selected
        arm once, in a seeded random order (run_cell). Repeats are numbered on across the trial's blocks."""
        for block_repeat in range(repeats):
            repeat = self.next_repeat()
            order = measure.order_speeds(speeds, self.speed_rng, self.args.speed_order, block_repeat)
            order = [speed for speed in order if measure.methods_at(methods, limited, speed['label'])]
            labels = [speed['label'] for speed in order]
            trial['speed_orders'].append({'block': block, 'repeat': repeat, 'block_repeat': block_repeat,
                                          'speeds': labels})
            log(f'{self.trial_id} r{repeat}: {block} repeat {block_repeat + 1} of {repeats}, {", ".join(labels)}')
            for speed_index, speed in enumerate(order):
                self.run_cell(trial, block, repeat, speed, measure.methods_at(methods, limited, speed['label']),
                              {'speed_index': speed_index, 'speed_order': labels, 'block_repeat': block_repeat})

    def run_controls(self, trial: dict, slot: str) -> None:
        """pause2 and burn4 (--controls) in a seeded order: at the busy block's start and end; pause2 alone at the
        moderate block's start. Each is a run with its own settle and tail, at whatever link is set."""
        block = dict(CONTROL_SLOTS)[slot]
        methods = [method for method in self.controls if slot != 'moderate_start' or method == 'pause2']
        if not methods:
            return
        repeat = self.next_repeat()
        speed = self.current_speed or measure.parse_speed(measure.UNTHROTTLED)
        order = measure.method_orders(methods, self.control_rng, 1)[0]
        link = {'repeat': repeat, 'block': block, 'source_cache': 'cold', 'control_slot': slot,
                'speed_index': None, 'speed_order': None}
        trial['controls'].append({'slot': slot, 'block': block, 'repeat': repeat, 'order': order})
        for order_index, method in enumerate(order):
            record = self.run_method(trial, repeat, speed, link, method, order_index, order)
            trial['runs'].append({'repeat': repeat, 'speed': speed['label'], 'method': method,
                                  'status': record['status'], 'block': block, 'control_slot': slot})
            if self.source_down:
                raise CommandError(f'the source cluster is down after {method}: {self.source_down}')
        save_json(self.out / 'trials' / f'{self.trial_id}.json', trial)

    def take_references(self, when: str) -> dict:
        """Steps 4 and 17, without load: the benchmark database's logical digest (then, at the start, a CHECKPOINT),
        loaddb's invariant (at the start, both sums 0), and the schema dumps' hashes. At the end each is compared
        with the start's."""
        started = time.perf_counter()
        logical = json.loads(self.query(measure.logical_digest_sql(), host=self.source.ip, port=PG_PORT,
                                        log_name=f'{when}-logical-digest.log'))
        if when == 'start':
            self.query('CHECKPOINT;', host=self.source.ip, port=PG_PORT, log_name='start-checkpoint.log')
        invariant = json.loads(self.query(measure.load_oracle_sql(), host=self.source.ip, port=PG_PORT,
                                          database=measure.LOAD_DATABASE, log_name=f'{when}-invariant.log'))
        schema = self.schema_hashes(self.source.ip, PG_PORT, f'{when}-schema')
        # Smoke item 14: loaddb's size, before and after (at most 5% growth); its relations' pages, from the catalog.
        sizes = json.loads(self.query(source_cluster.LOAD_RELATIONS_SQL, host=self.source.ip, port=PG_PORT,
                                      database=measure.LOAD_DATABASE, log_name=f'{when}-loaddb-sizes.log'))
        result = {'logical': logical, 'invariant': {**invariant, **measure.atomicity(invariant)}, 'schema': schema,
                  'loaddb_pages': {name: value.get('pages') for name, value in (sizes or {}).items()},
                  'seconds': round(time.perf_counter() - started, 3)}
        if when == 'start':
            result['zero'] = int(invariant.get('sum_abalance') or 0) == 0 == int(invariant.get('sum_n') or 0)
            self.references = result
        else:
            same, differences = measure.compare_logical(self.references['logical'], logical)
            before = sum(pages or 0 for pages in self.references.get('loaddb_pages', {}).values())
            after = sum(pages or 0 for pages in result['loaddb_pages'].values())
            result.update(logical_match=same, logical_differences=differences,
                          schema_match=schema == self.references['schema'],
                          loaddb_growth=round(after / before - 1, 6) if before else None)
        return result

    def take_snapshot(self) -> dict:
        """Step 5: the snapshot (source_cluster.py snapshot), its selection's digest kept for the proxies'
        verification and the trial's end; the trial's record holds its summary."""
        result = self.source.script(SOURCE_CLUSTER, 'snapshot', '--root', self.source_root, '--bin-dir',
                                    str(self.bin), '--log-dir', self.source_log('cluster'), '--snapshot',
                                    self.snapshot_data, timeout=7200)
        self.snapshot_digest = result.pop('digest')
        result['digest'] = {'summary': measure.digest_summary(self.snapshot_digest['digest']),
                            'selected': len(self.snapshot_digest['selected']),
                            'read_from': self.snapshot_digest.get('read_from')}
        return result

    def idle_rate_probe(self) -> dict:
        """Step 6: the source's idle busy CPU rate over --idle-rate-s, steal left out (the calibrated CPU-s' baseline)."""
        before = self.snapshot_source()
        self.pause(self.args.idle_rate_s)
        after = self.snapshot_source()
        delta = measure.snapshot_delta(before, after)
        busy = measure.busy_seconds(delta['cpu'])
        self.idle_rate = round(busy / delta['interval_seconds'], 6) if delta['interval_seconds'] else None
        return {'cpu': delta['cpu'], 'interval_seconds': delta['interval_seconds'], 'busy_rate_no_steal': self.idle_rate}

    def clock_probe(self) -> dict:
        """The two clocks: a TCP timestamp exchange with the source's agent; the offset (the source's clock minus
        dest's) of the shortest round trip, which every later record maps its times with."""
        samples = str(self.args.clock_samples)
        listener = subprocess.Popen(self.source.agent_argv('clock-probe', '--listen', f'{self.source.ip}:{CLOCK_PORT}',
                                                           '--samples', samples),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    stdin=subprocess.DEVNULL)
        try:
            if 'LISTENING' not in listener.stdout.readline():
                raise CommandError(f'the clock probe did not start on the source: {listener.stderr.read()[-1000:]}')
            result = self.local_agent('clock-probe', '--connect', f'{self.source.ip}:{CLOCK_PORT}', '--samples',
                                      samples)
            stdout, _ = listener.communicate(timeout=120)
        finally:
            if listener.poll() is None:
                listener.kill()
                listener.wait()
        self.clock_offset_s = result['offset_s']
        log(f'{self.trial_id}: the source\'s clock is {result["offset_s"] * 1000:+.3f} ms from dest\'s '
            f'(round trip {result["rtt_s"] * 1000:.3f} ms)')
        return {**result, 'source': agent_result(stdout)}

    def clock_probe_retried(self) -> dict:
        """The clock probe, tried up to CLOCK_PROBE_ATTEMPTS times with a growing back-off: one TCP exchange that
        failed (an SSH hiccup, a listener that didn't answer) would leave every window of the trial unmapped. The
        earlier attempts' errors are kept; after the last, its error is raised."""
        errors = []
        for attempt in range(CLOCK_PROBE_ATTEMPTS):
            try:
                result = self.clock_probe()
            except Exception as exc:
                errors.append(f'{type(exc).__name__}: {exc}')
                log(f'{self.trial_id}: the clock probe failed (attempt {attempt + 1} of {CLOCK_PROBE_ATTEMPTS}): {exc}')
                if attempt + 1 < CLOCK_PROBE_ATTEMPTS:
                    self.pause(CLOCK_PROBE_BACKOFF_S * (attempt + 1))
                continue
            return {**result, 'failed_attempts': errors} if errors else result
        raise CommandError(f'the clock probe failed {CLOCK_PROBE_ATTEMPTS} times: ' + '; '.join(errors))

    def wal_sync_probe(self) -> dict:
        """Step 6: pg_test_fsync -s 2 on the source's /data."""
        result = self.source.agent('run', '--log', self.source_log('pg_test_fsync.log'), '--',
                                   str(self.bin / 'pg_test_fsync'), '-s', '2', '-f', f'{self.work}/pg_test_fsync.tmp',
                                   timeout=900)
        self.source.agent('remove', '--under', str(self.work), f'{self.work}/pg_test_fsync.tmp')
        return {'exit_code': result['exit_code'], 'wall_seconds': result['wall_seconds'], 'output': result['log_tail']}

    def load_probes(self) -> dict:
        """Step 6, with no load: the idle rate, the clock offset, fincore of the data directory, the WAL sync probe.
        Each one's error is recorded and the trial goes on."""
        return {'idle': self.probe('the idle rate', self.idle_rate_probe),
                'clock': self.probe('the clock probe', self.clock_probe_retried),
                'fincore': self.probe('fincore', lambda: self.source.agent('fincore', self.source_data, timeout=900)),
                'wal_sync': self.probe('the WAL sync probe', self.wal_sync_probe)}

    def run_load_trial(self, index: int, pattern: str, fraction: float) -> dict:
        """One trial under EXP-12's load, in its order (its "Order within the trial"):

        1. the cluster, the benchmark data and loaddb (loaddb-setup: amcheck, the settings, a restart, VACUUM ANALYZE,
           CHECKPOINT); 2. B0 (CRC32C), verified, hashed; 3. the workload and its CHECKPOINT; 4. the start references;
        5. the snapshot (--snapshot); 6. the probes; 7. the trial's own C, then rest; [the oracle's negative controls,
        --oracle-controls, the smoke run's]; 8. the sampler and the load at --load-rate, its warm-up; 9. the controls;
        10. the busy block; 11. the controls again; 12. the warm block; 13. the load restarted at --moderate-rate, its
        warm-up, one pause2; 14. the moderate block; 15. the load stopped, load-check, the logs fetched; 16. idle, then
        the calibration block; 17. the end references, EXP-01's ground truth, the snapshot's and B0's digests, then
        the checks that wait for the logs: each live output's cut and coverage (finish_load_checks).
        """
        args = self.args
        self.source_down = None
        self.trial_id = f'{index:02d}-{pattern}-f{fraction:g}-s{args.scale}'
        self.multi_stream_links, self.reference_digests, self.byte_views = {}, {}, {}
        self.snapshot_digest, self.database_oids, self.references = None, {}, {}
        self.load_runs, self.load_current, self.load_rate = [], None, None
        self.load_used, self.clock_offset_s = False, None  # an earlier trial's offset is no offset for this one
        self.pending_source, self.output_digests, self.static_digests = [], {}, set()
        self.repeat_counter, self.current_speed = 0, None
        trial = {'schema': SCHEMA_TRIAL, 'trial_id': self.trial_id, 'pattern': pattern, 'fraction': fraction,
                 'scale': args.scale, 'seed': args.seed, 'started_utc': utc_now(), 'status': 'running', 'links': [],
                 'runs': [], 'speed_orders': [], 'controls': [],
                 'load': {'runs': self.load_runs, 'warmups': [], 'restarts': []}}
        self.current_trial = trial
        save_json(self.out / 'trials' / f'{self.trial_id}.json', trial)
        try:
            self.throttle('off')
            log(f'{self.trial_id}: source cluster, dataset and loaddb (scale {args.scale})')
            trial['setup'] = self.source.script(SOURCE_CLUSTER, 'setup', *self.cluster_args(trial),
                                                '--listen-ip', self.source.ip, '--client-ip', self.dest_ip)
            trial['rows_changed'] = workloads.target_rows(
                pattern, fraction, accounts_rows=trial['setup']['spec']['accounts_rows'],
                events_rows=trial['setup']['spec']['events_rows'])
            trial['loaddb'] = self.source.script(SOURCE_CLUSTER, 'loaddb-setup', *self.load_args(), '--log-dir',
                                                 self.source_log('cluster'), '--clients', str(args.load_clients))
            self.database_oids = trial['loaddb']['databases']
            self.take_b0(trial)
            log(f'{self.trial_id}: workload {pattern} k={trial["rows_changed"]}')
            trial['workload'] = self.source.script(SOURCE_CLUSTER, 'workload', *self.cluster_args(trial))
            log(f'{self.trial_id}: the start references')
            trial['references'] = {'start': self.take_references('start')}
            if args.snapshot:
                log(f'{self.trial_id}: the snapshot')
                trial['snapshot'] = self.take_snapshot()
            trial['probes'] = self.load_probes()
            trial['clock'] = {'start': trial['probes']['clock']}
            if self.clock_offset_s is None:
                # Every window maps to the source's clock through it: without one, no run could be judged.
                raise CommandError('no clock offset between the VMs (' + str(trial['clock']['start'].get('error'))
                                   + '); the load is not started')
            self.load_used = True  # from the capacity runs on, the load's directory holds logs
            trial['load']['capacity'] = self.load_capacity(trial)
            save_json(self.out / 'trials' / f'{self.trial_id}.json', trial)
            trial['load']['sampler'] = self.sampler_start()
            self.load_start('busy', args.load_rate, 'busy')
            self.load_warmup(trial)
            if args.oracle_controls:
                trial['oracle_controls'] = self.run_oracle_controls()
            self.run_controls(trial, 'busy_start')
            self.run_load_block(trial, 'busy', args.repeats, self.speeds, self.methods, self.method_speeds)
            self.run_controls(trial, 'busy_end')
            self.run_load_block(trial, 'warm', args.warm_source_repeats, self.warm_speeds, self.warm_source_methods,
                                {})
            self.flush_pending_source()
            if args.moderate_repeats:
                self.load_stop()
                self.load_start('moderate', args.moderate_rate, 'moderate')
                self.load_warmup(trial)
                self.run_controls(trial, 'moderate_start')
                self.run_load_block(trial, 'moderate', args.moderate_repeats, self.moderate_speeds,
                                    self.moderate_methods, self.moderate_method_speeds)
                self.flush_pending_source()
            self.end_load(trial)
            save_json(self.out / 'trials' / f'{self.trial_id}.json', trial)
            if args.calibration_repeats:
                self.pause(args.calibration_idle_s)
                self.run_load_block(trial, 'calibration', args.calibration_repeats, [self.calibration_speed],
                                    self.calibration_methods, {})
                self.flush_pending_source()
            self.throttle('off')
            log(f'{self.trial_id}: the end references')
            trial['references']['end'] = self.take_references('end')
            trial['clock']['end'] = self.probe('the clock probe', self.clock_probe_retried)
            if args.snapshot:
                digest = self.source.agent('datadir-digest', self.snapshot_data, '--pg-controldata',
                                           str(self.bin / 'pg_controldata'), timeout=3600)
                same, differences = measure.compare_digests(self.snapshot_digest['digest'], digest['digest'])
                trial['snapshot']['end_match'], trial['snapshot']['end_differences'] = same, differences
            trial['verify'] = self.source.script(SOURCE_CLUSTER, 'verify', *self.cluster_args(trial))
            final = measure.tree_digest(self.b0)
            same, differences = measure.compare_digests(self.b0_digest, final)
            trial['b0_unchanged'], trial['b0_differences'] = same, differences
            trial['load_checks'] = self.finish_load_checks(trial)
            if not same:
                raise CommandError('B0 changed during the trial: ' + '; '.join(differences))
            bad = [run for run in trial['runs'] if run['status'] not in ('pass',)]
            trial['status'] = 'pass' if not bad else 'fail'
        except Exception as exc:
            trial['status'] = 'error'
            trial['error'] = f'{type(exc).__name__}: {exc}'
            trial['traceback'] = traceback.format_exc()
            log(f'{self.trial_id}: ERROR {trial["error"]}')
        finally:
            self.end_load_quietly(trial)
            self.cleanup_trial(trial)
        trial['finished_utc'] = utc_now()
        save_json(self.out / 'trials' / f'{self.trial_id}.json', trial)
        return trial

    def end_load_quietly(self, trial: dict) -> None:
        """After the trial, whatever ended it (an error, or an interrupt: remote.py stop's SIGTERM): the pending source
        work, the load and the sampler stopped, and the load's logs kept. Unless step 15 fetched them, they are
        compressed (best effort: a failure is recorded and the files go as they are), fetched to dest, and read for
        the checks that wait for them (finish_load_checks); the runs recorded so far keep their numbers. The load's
        directory is removed only once its logs are on dest; otherwise it is left on the source and named in
        cleanup_errors. Each failure is recorded, and the trial's record saved."""
        load = trial.setdefault('load', {})
        end = load.get('end') or {}

        def attempt(name, step):
            try:
                return step()
            except Exception as exc:
                trial.setdefault('cleanup_errors', []).append(f'{name}: {exc}')
                return None
        attempt('the pending source work', self.flush_pending_source)
        if self.load_current is not None:
            attempt('stop the load', self.load_stop)
        if load.get('sampler') and 'sampler' not in end:
            attempt('stop the sampler', self.sampler_stop)
        fetched = end.get('fetched')
        if not fetched and self.load_used:
            log(f'{self.trial_id}: keeping the load\'s logs after the trial\'s end')
            kept = load['kept_after_error'] = {}
            if not end.get('compress'):
                kept['compress'] = attempt('compress the load logs (they are fetched as they are)',
                                           self.compress_load_logs)
            fetched = kept['fetched'] = attempt('fetch the load logs', self.fetch_load_logs)
        if fetched and not trial.get('load_checks'):  # also after an error past step 15, before step 17's checks
            trial['load_checks'] = attempt('the checks that wait for the logs', lambda: self.finish_load_checks(trial))
        attempt('remove the snapshot', lambda: self.remove_source(self.snapshot_data))
        if fetched or not self.load_used:
            attempt('remove the load directory', lambda: self.source.agent(
                'remove', '--under', str(Path(self.load_dir).parent), self.load_dir))
        else:
            trial.setdefault('cleanup_errors', []).append(
                f'the load directory {self.load_dir} was left on the source: its logs are not on dest')
        attempt('save the trial', lambda: save_json(self.out / 'trials' / f'{self.trial_id}.json', trial))

    def finish_load_checks(self, trial: dict) -> dict:
        """The checks that wait for the load's logs (fetched at step 15), for every live output of the trial and the
        oracle's controls: (iv) the consistent cut and (v) coverage; G4's staleness; each run's final status.

        Each copy's n (its oracle's progress) is mapped to the logged run whose span holds it, and the commits on
        either side of its cut are read from that run's log in one pass per run (measure.pick_commits). Without the
        logs, cut and coverage are unverified.
        """
        for record in self.records:
            oracle = (record.get('verification') or {}).get('oracle')
            if record.get('trial_id') == self.trial_id and oracle and oracle.get('progress') is None \
                    and oracle.get('cut') is None:
                # No n to cut at (its invariant query couldn't run, or the copy didn't start): the cut and coverage
                # can't be judged; whatever else made it wrong still decides.
                oracle['cut'] = oracle['coverage'] = 'unverified'
                self.set_load_status(record)
                save_json(self.record_path(record), record)
                self.update_trial_run(record)
        records = [record for record in self.records if record.get('trial_id') == self.trial_id
                   and (record.get('verification') or {}).get('oracle', {}).get('progress') is not None]
        copies = [(record['verification']['oracle'], record) for record in records]
        copies += [(control['oracle'], None) for control in (trial.get('oracle_controls') or {}).get('controls', [])
                   if (control.get('oracle') or {}).get('progress') is not None]
        logs = self.load_logs
        queries = [measure.cut_queries(oracle['progress'], self.load_runs) for oracle, _ in copies]
        wanted = measure.wanted_commits(queries)
        commits, unreadable = {}, []
        for run in self.load_runs:
            if run['run_id'] not in wanted:
                continue
            paths = [measure.existing_log(logs, name) for name in run.get('log_files') or []]
            try:
                for (client, position), times in measure.pick_commits(paths, wanted[run['run_id']]).items():
                    commits[(run['run_id'], client, position)] = times
            except (OSError, ValueError) as exc:
                unreadable.append(f'{run["run_id"]}: {exc}')
        for (oracle, record), query in zip(copies, queries):
            if unreadable:
                oracle['cut'] = oracle['coverage'] = 'unverified'
                oracle['cut_detail'] = {'unreadable': unreadable}
                continue
            cut = measure.consistent_cut(query, commits)
            oracle['cut'], oracle['cut_detail'] = ('ok' if cut['ok'] else 'wrong'), cut
            window = (oracle.get('coverage_window_utc') if record is None
                      else (record.get('g4') or {}).get('coverage_window_utc'))
            offset = (record or {}).get('clock_offset_s', self.clock_offset_s)
            if window is None:
                oracle['coverage'] = 'unverified'
            else:
                covered = measure.coverage(cut, window[0], window[1], offset or 0.0)
                oracle['coverage'], oracle['coverage_detail'] = ('ok' if covered['ok'] else 'wrong'), covered
            if record is not None:
                g4 = record.setdefault('g4', {})
                t1 = (record.get('window') or {}).get('t1_source_utc')
                moment = measure.cut_time(cut)
                g4['cut_utc'] = [None if cut['lo_us'] is None else cut['lo_us'] / 1e6,
                                 None if cut['hi_us'] is None else cut['hi_us'] / 1e6]
                g4['staleness_s'] = None if t1 is None or moment is None else round(t1 - moment, 6)
                self.set_load_status(record)
                save_json(self.record_path(record), record)
                self.update_trial_run(record)
        for control in (trial.get('oracle_controls') or {}).get('controls', []):
            if control['name'] in measure.ORACLE_CONTROLS:
                control['verdict'] = measure.oracle_control_verdict(control['name'], control['oracle'])
            else:  # the good copy passes every check
                status = measure.oracle_status(control['oracle'])
                control['verdict'] = {'control': control['name'], 'status': status, 'ok': status == 'pass'}
        measure.write_csv(self.out / 'summary.csv', self.records)
        return {'outputs': len(records), 'commits_read': len(commits), 'unreadable': unreadable,
                'statuses': {status: sum(1 for record in records if record['status'] == status)
                             for status in sorted({record['status'] for record in records})}}

    # --- T10a (EXP-12): the oracle's negative controls (smoke item 5) --------------------------

    def relation_path(self, relation: str) -> str:
        """A relation's file in loaddb, relative to the data directory, from the source's catalog."""
        return self.query(f"SELECT pg_relation_filepath('{relation}');", host=self.source.ip, port=PG_PORT,
                          database=measure.LOAD_DATABASE, log_name=f'relation-{relation}.log')

    def oracle_copy(self, name: str, extra=()) -> dict:
        """A full copy of the live source, untimed (pg_basebackup, CRC32C), into outputs/oracle-<name>, with its window
        on dest's clock (the coverage the copy must show)."""
        output = self.outputs / f'oracle-{name}'
        self.remove_dest(output)
        started_utc = time.time()
        command = self.local_command(self.basebackup(output, host=self.source.ip, extra=extra), f'oracle-{name}.log')
        return {'output': output, 'command': command, 'window_utc': [started_utc, time.time()]}

    def corrupt_page(self, data: Path, relative: str, block: int) -> dict:
        """One 8-kB page of a relation's file overwritten with a non-zero pattern: its checksum then fails."""
        path = Path(data) / relative
        with open(path, 'r+b') as stream:
            stream.seek(block * 8192)
            stream.write(b'\xa5' * 8192)
            stream.flush()
            os.fsync(stream.fileno())
        return {'file': relative, 'block': block}

    def oracle_control(self, name: str, good: dict) -> dict:
        """One of the oracle's negative controls (measure.ORACLE_CONTROLS) on a copy of the good output.

        The copy is cp -a of it (or, for no_wal, its own pg_basebackup -X none). Started copies are changed with SQL
        before the checks; the page controls start and recover it once, stop it cleanly, overwrite the page, and
        start it again, so no WAL replay can restore the page. The cut and coverage wait for the logs.
        """
        log(f'{self.trial_id}: the oracle\'s negative control {name}')
        data = self.outputs / f'oracle-{name}'
        self.remove_dest(data)
        prefix, self.log_prefix = self.log_prefix, f'oracle-{name}-'
        try:
            if name == 'no_wal':
                copy = self.oracle_copy(name, extra=('--wal-method', 'none'))
                window = copy['window_utc']
            else:
                subprocess.run(['cp', '-a', str(good['output']), str(data)], check=True)
                window = good['window_utc']
            shift = measure.ORACLE_CONTROL_SHIFT
            statements = {'n_raised': ['UPDATE load_progress SET n = n + 1 WHERE client = 0;'],
                          'cut_extended': [f'UPDATE load_progress SET n = n + {shift} WHERE client = 0;',
                                           f'UPDATE pgbench_accounts SET abalance = abalance + {shift} WHERE aid = 1;'],
                          'cut_back': [f'UPDATE load_progress SET n = n - {shift} WHERE client = 0;',
                                       f'UPDATE pgbench_accounts SET abalance = abalance - {shift} WHERE aid = 1;'],
                          'schema': ['ALTER TABLE load_progress ADD COLUMN extra integer;']}.get(name)
            detail: dict = {}
            if name in ('heap_page', 'index_page'):
                first = self.check_output(f'oracle-{name}-first', data, recovery=True)
                relation = 'pgbench_accounts' if name == 'heap_page' else 'pgbench_accounts_pkey'
                relative = self.relation_path(relation)
                block = 1 if name == 'index_page' else max(1, os.stat(data / relative).st_size // 8192 // 2)
                detail = {'first_start_ok': first.get('ok'), 'relation': relation,
                          **self.corrupt_page(data, relative, block)}

            def mutate():
                for statement in statements or ():
                    self.query(statement, host=self.output_socket, port=OUTPUT_PG_PORT,
                               database=measure.LOAD_DATABASE, log_name=f'oracle-{name}-mutate.log')
                return statements

            start = self.check_output(f'oracle-{name}', data, recovery=True, checks=lambda: self.oracle_checks(
                f'oracle-{name}', static_digest=False, mutate=mutate if statements else None))
            oracle = self.oracle_summary(start)
            oracle['bytes'] = 'ok'
            oracle['coverage_window_utc'] = window
            return {'name': name, 'detail': detail, 'output_start': start, 'oracle': oracle}
        finally:
            self.log_prefix = prefix
            if not self.args.keep_outputs:
                self.remove_dest(data)

    def run_oracle_controls(self) -> dict:
        """EXP-12's smoke item 5 (--oracle-controls), under the load: a good full copy, which must pass every check,
        and the seven negative controls, each of which must fail through its own check (measure.ORACLE_CONTROLS);
        their cut and coverage are judged with the live outputs at the trial's end (finish_load_checks)."""
        good = self.oracle_copy('good')
        result = {'good': None, 'controls': [], 'copy': {'window_utc': good['window_utc'],
                                                         'exit_code': good['command']['exit_code']}}
        try:
            # The good copy, then each control, each on a copy of the unstarted backup, each recovering on its own.
            names = ['good', *measure.ORACLE_CONTROLS]
            for name in names:
                if name == 'good':
                    data = self.outputs / 'oracle-checked'
                    subprocess.run(['cp', '-a', str(good['output']), str(data)], check=True)
                    try:
                        checked = self.check_output('oracle-checked', data, recovery=True, checks=lambda: self.oracle_checks(
                            'oracle-checked', static_digest=True))
                    finally:
                        if not self.args.keep_outputs:
                            self.remove_dest(data)
                    oracle = {**self.oracle_summary(checked), 'bytes': 'ok', 'coverage_window_utc': good['window_utc']}
                    result['good'] = {'name': 'good', 'output_start': checked, 'oracle': oracle}
                    result['controls'].append(result['good'])
                else:
                    result['controls'].append(self.oracle_control(name, good))
        finally:
            if not self.args.keep_outputs:
                self.remove_dest(good['output'])
        return result

    def load_run_record(self) -> dict:
        """What run.json says about EXP-12's load, blocks, timeline and oracle."""
        args = self.args

        def pgbench(run_id, rate):
            return measure.pgbench_load_argv(self.bin / 'pgbench', rate=rate, clients=args.load_clients,
                                             threads=args.load_threads, duration_s=args.load_duration_s,
                                             seed=self.load_seed(run_id), prefix=f'{self.load_dir}/{run_id}',
                                             scripts=f'{self.remote_repo}/{LOAD_SCRIPT_DIR}',
                                             host=f'{self.source_root}/s', port=PG_PORT, user=PG_USER)

        return {'load': args.load, 'scripts': [f'{name}@{weight}' for name, weight in measure.LOAD_SCRIPTS],
                'database': measure.LOAD_DATABASE, 'clients': args.load_clients, 'threads': args.load_threads,
                'rates': {'busy': args.load_rate, 'moderate': args.moderate_rate or None},
                'pgbench_argv': {'busy': pgbench('busy', args.load_rate),
                                 **({'moderate': pgbench('moderate', args.moderate_rate)} if args.moderate_repeats
                                    else {}),
                                 'capacity': pgbench('capacity', 0)},
                'stop_signal': f'SIG{args.load_signal}', 'log_dir': self.load_dir, 'server_config':
                measure.LOAD_SERVER_CONFIG, 'warmup_s': args.load_warmup_s,
                'capacity': {'warmup_s': args.capacity_warmup_s, 'seconds': args.capacity_s,
                             'runs': args.capacity_runs, 'rest_s': args.capacity_rest_s},
                'settle': {'min_s': args.settle_s, 'max_s': args.settle_max_s, 'window_s': measure.SETTLE_WINDOW_S,
                           'tolerance': measure.SETTLE_TOLERANCE, 'lag_ms': measure.SETTLE_LAG_MS},
                'tail_s': args.tail_s, 'window_tail_s': args.window_tail_s, 'none_window_s': args.none_window_s,
                'source_cache': args.source_cache, 'snapshot': self.snapshot_data if args.snapshot else None,
                'static_digest': args.static_digest, 'controls': self.controls,
                'control_slots': [slot for slot, _ in CONTROL_SLOTS], 'sampler_interval_s': args.sampler_interval_s,
                'blocks': {'busy': {'repeats': args.repeats, 'speeds': [speed['label'] for speed in self.speeds],
                                    'methods': self.methods, 'method_speeds': self.method_speeds},
                           'warm': {'repeats': args.warm_source_repeats,
                                    'speeds': [speed['label'] for speed in self.warm_speeds],
                                    'methods': self.warm_source_methods, 'source_cache': 'warm'},
                           'moderate': {'repeats': args.moderate_repeats, 'rate': args.moderate_rate or None,
                                        'speeds': [speed['label'] for speed in self.moderate_speeds],
                                        'methods': self.moderate_methods,
                                        'method_speeds': self.moderate_method_speeds},
                           'calibration': {'repeats': args.calibration_repeats,
                                           'speed': self.calibration_speed['label'],
                                           'methods': self.calibration_methods, 'load': None}},
                'oracle': {'checks': list(measure.ORACLE_CHECKS), 'restrict_key': measure.SCHEMA_RESTRICT_KEY,
                           'amcheck': measure.amcheck_argv(self.bin / 'pg_amcheck', host='<copy socket>',
                                                           port=OUTPUT_PG_PORT, user=PG_USER),
                           'controls': dict(measure.ORACLE_CONTROLS) if args.oracle_controls else None},
                'timeline': TIMELINE_POLICY}

    def preflight_t10a(self) -> dict:
        """What an EXP-12 run checks before its first trial: the same PostgreSQL minor version on both VMs (check
        (vii)'s dumps name it), and on the source zstd and util-linux's fincore."""
        environment = self.run['environment']
        versions = {host: {tool: (environment[host].get('postgresql') or {}).get(tool)
                           for tool in ('postgres', 'pg_dump', 'pgbench', 'pg_amcheck')} for host in ('source', 'dest')}
        if versions['source']['postgres'] != versions['dest']['postgres'] \
                or versions['source']['pg_dump'] != versions['dest']['pg_dump']:
            raise SystemExit(f'the VMs run different PostgreSQL versions ({versions}); EXP-12 needs one minor version '
                             'on both (its schema dumps name it): upgrade the older one from PGDG first')
        missing = [tool for tool in ('zstd', 'fincore') if not environment['source'].get(tool)]
        missing += [tool for tool in ('pgbench', 'pg_amcheck') if not versions['source'][tool]]
        missing += [f'{tool} on dest' for tool in ('pg_amcheck', 'pg_dump') if not versions['dest'][tool]]
        if missing:
            raise SystemExit(f'EXP-12 needs {", ".join(missing)} on the source (infra/gcp/startup.sh installs them)')
        return {'versions': versions, 'zstd': environment['source'].get('zstd'),
                'fincore': environment['source'].get('fincore'),
                'chrony': {host: environment[host].get('chrony') for host in ('source', 'dest')}}

    # --- run ---------------------------------------------------------------

    def environment(self) -> dict:
        probackup = ('--pg-probackup', self.args.pg_probackup)
        source, dest = self.both(
            lambda: self.local_agent('env', '--iface', self.dest_iface, '--bin-dir', str(self.bin), *probackup),
            lambda: self.source.agent('env', '--iface', self.source_iface, '--bin-dir',
                                      str(self.bin), *probackup))
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
        T2 runs need that module too: parcopy* and the reference digests use its selection.
        """
        required = measure.pgdelta_required_options(self.pinned_methods)
        found = {}
        for command, options in required.items():
            local = subprocess.run([sys.executable, '-m', 'pgdelta', command, '--help'],
                                   cwd=self.repo, capture_output=True, text=True, timeout=120)
            remote = self.source.agent(
                'run', '--log', f'{self.work}/logs/pgdelta-{command}-help.log', '--cwd',
                self.remote_repo, '--', self.source.python, '-m', 'pgdelta', command, '--help')
            found[command] = {'dest': pgdelta_supports(local.stdout, options),
                              'source': pgdelta_supports(remote.get('log_tail', ''), options)}
        if self.t2 or self.t10a or any(method in measure.PGDELTA_DIRECT_METHODS for method in self.pinned_methods):
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

    def dirty_idle_noise(self, windows: int) -> dict:
        """Check (a)'s idle noise: dest's Dirty + Writeback increase over idle windows of --idle-seconds."""
        seconds = self.args.idle_seconds or 5.0
        increases = []
        for _ in range(windows):
            before = self.snapshot_dest()
            time.sleep(seconds)
            after = self.snapshot_dest()
            increases.append(measure.memory_delta(before['meminfo'], after['meminfo'])
                             ['dirty_writeback_increase_bytes'])
        return {'windows': windows, 'window_seconds': seconds, 'increases_bytes': increases,
                'max_increase_bytes': max(increases)}

    def dirty_idle_noise_plan(self, plan) -> dict:
        """EXP-13's check (a) noise (--idle-noise-plan): dest's Dirty + Writeback increase over each idle window, by
        window length (smoke item 18: 20 windows of 5 s, 6 of 30 s, 3 of 120 s); each length's largest."""
        increases: dict = {}
        for seconds, windows in plan:
            for _ in range(windows):
                before = self.snapshot_dest()
                time.sleep(seconds)
                after = self.snapshot_dest()
                increases.setdefault(f'{seconds:g}', []).append(
                    measure.memory_delta(before['meminfo'], after['meminfo'])['dirty_writeback_increase_bytes'])
        return {'plan': [[seconds, windows] for seconds, windows in plan], 'increases_bytes': increases,
                'max_increase_bytes': {length: max(values) for length, values in increases.items()}}

    def t12_run_record(self) -> dict:
        """What run.json says about EXP-13: its conditions, each one's arms by repeat, the plan's options, and the
        policies: the tree, the control path, the checks and the clock."""
        return {'conditions': self.conditions,
                'plan': self.t12_plan,
                'tuned_tcp_max_bytes': self.args.tuned_tcp_max_bytes,
                'control_port': self.args.control_port,
                'link_test': {'seconds': self.args.link_test_seconds, 'warmup': self.args.link_test_warmup,
                              'steady': 'the median of the last 3 one-second rates after the warmup',
                              'after_connect': True},
                'wan': ('infra/gcp/wan.sh on both VMs: a prio root; u32 filters send to the delayed band only traffic '
                        'to the peer VM, except TCP with the control port as source or destination port; the delayed '
                        "band is netem delay r/2 limit 1000000, then a tbf at the condition's rate with throttle.sh's "
                        'burst and a queue of max(50 ms, r). Off outside every visit'),
                'checks': {'visit': 'the tree on both VMs, 20 pings (min in [r, r + 1 ms], median at most min + 2 ms), '
                                    "5 control no-ops (each at most 5 ms above the anchor's), both VMs' TCP settings; "
                                    'A11 (i): reset and checked once more, then the trial stops',
                           'run': "the guard (both VMs' tcp_rmem and tcp_wmem), a TCP metrics flush, the samplers "
                                  "(ss on the source every 0.5 s, tc on both); A11 (ii): netem's own drops 0, its "
                                  "sampled backlog below its limit, netem's bytes at least 0.98 x the wire bytes, the "
                                  "sending sockets' minrtt in [r, r + 2 ms] (network_checks)"},
                'void_rule': 'A15: a probackup* run is void only if a user-table file differs between the reference '
                             'digests; catalog differences are reported by file class',
                'wal_ticker': 'on the source over its Unix socket, launched over the control path',
                'busy_threshold': ("each pgdelta sender gets --busy-inflight-bytes, the visit's one-stream steady rate "
                                   'times its ping minimum, where both VMs\' pgdelta takes it')}

    def preflight_t12(self) -> dict:
        """What an EXP-13 run checks and records before its first trial:
        - each VM's TCP settings as found: Debian's defaults, which every default visit and every reset write back;
        - the tc commands of every condition (wan.sh's dry run), for run.json;
        - the control no-ops' reference, with the tree at the anchor (the first r = 0 condition's rate) and default
          settings;
        - whether pgdelta send takes --busy-inflight-bytes on both VMs (without it, only unsent bytes are busy);
        - where v1_tls or a stunnel link test runs: stunnel and openssl on both VMs, and the session's TLS material
          (setup_tls); with --tls-refusal-check, smoke item 12's refusals.
        """
        args = self.args
        environment = self.run['environment']
        result: dict = {}
        self.tcp_defaults = self.tcp_settings()
        unknown = {vm: name for vm, values in self.tcp_defaults.items() for name in measure.TCP_MEM_SYSCTLS
                   if measure.tcp_mem(values.get(name)) is None}
        if unknown:
            raise SystemExit(f'cannot read the TCP settings EXP-13 changes: {unknown}')
        result['tcp_defaults'] = self.tcp_defaults
        result['tc_commands'] = {
            condition['label']: self.local_agent('wan', 'commands', condition['tc_rate'],
                                                 f'{condition["delay_ms"]:g}', '--peer', self.source.ip,
                                                 '--control-port', str(args.control_port), '--iface',
                                                 self.dest_iface)['commands']
            for condition in self.conditions}
        anchor = next((condition for condition in self.conditions
                       if condition['rtt_ms'] == 0 and condition['host_tcp'] == 'default'),
                      measure.parse_condition(f'{self.conditions[0]["gbit"]:g}:0:default'))
        try:
            self.wan('apply', anchor)
            self.control_reference = self.control_noops()
        finally:
            self.network_off()
        result['control_reference'] = {'condition': anchor['label'], 'seconds': self.control_reference}
        helps = {'dest': subprocess.run([sys.executable, '-m', 'pgdelta', 'send', '--help'], cwd=self.repo,
                                        capture_output=True, text=True, timeout=120).stdout,
                 'source': self.source.agent('run', '--log', f'{self.work}/logs/pgdelta-send-help-t12.log', '--cwd',
                                             self.remote_repo, '--', self.source.python, '-m', 'pgdelta', 'send',
                                             '--help').get('log_tail', '')}
        self.busy_flag = all(pgdelta_supports(text, ('--busy-inflight-bytes',)) for text in helps.values())
        result['busy_inflight_flag'] = self.busy_flag
        if not self.busy_flag:
            log("pgdelta send has no --busy-inflight-bytes on both VMs: its waits' busy parts count unsent bytes only")
        tls = (any(method in measure.TLS_METHODS for method in self.methods) or self.t12_plan['stunnel_link_test']
               or args.tls_refusal_check)
        if tls:
            missing = [f'{vm}: {tool}' for vm, env in environment.items() for tool in ('stunnel', 'openssl')
                       if not env.get(tool)]
            if missing:
                raise SystemExit(f'v1_tls needs stunnel4 and openssl on both VMs (infra/gcp/startup.sh installs '
                                 f'them): {", ".join(missing)} missing')
            self.tls = result['tls'] = self.setup_tls()
            result['stunnel_configs'] = self.stunnel_configs('<the run\'s server log>', '<the run\'s client log>')
            if args.tls_refusal_check:
                result['tls_refusals'] = self.tls_refusals()
        return result

    def preflight_t2(self) -> dict:
        """What a T2 run checks before its first trial, and dest's write-back defaults.

        /data on both VMs ext4 in data=ordered mode (measure.data_mount_problem), on which EXP-11's
        directory rule rests; strace on dest (EXP-11's syscall audit); pg_basebackup's --sync-method on
        dest where full_syncfs runs; the pg_probackup and parcopy blocks' own checks where those arms run;
        dest's vm.dirty_background_* (record_writeback_defaults), which every run's guard requires.
        """
        environment = self.run['environment']
        mounts = {host: environment[host].get('data_mount') for host in ('source', 'dest')}
        wrong = {host: problem for host, text in mounts.items() if (problem := measure.data_mount_problem(text))}
        if wrong:
            raise SystemExit('EXP-11 needs /data on ext4 in data=ordered mode with barriers, as infra/gcp/startup.sh '
                             'makes it (its directory rule rests on the ordered journal): '
                             + '; '.join(f'{host}: {problem}' for host, problem in wrong.items())
                             + ' (infra/gcp/reformat_dest_ext4.sh makes dest ext4 again)')
        dest = environment['dest']
        if not dest.get('strace'):
            raise SystemExit("strace is not installed on dest (infra/gcp/startup.sh installs it); EXP-11's "
                             'syscall audit needs it')
        result = {'data_mount': mounts, 'strace': dest['strace']}
        if 'full_syncfs' in self.methods and not dest.get('pg_basebackup_sync_method'):
            raise SystemExit("pg_basebackup on dest has no --sync-method (PostgreSQL 17's), which full_syncfs "
                             'needs')
        if any(method in measure.PROBACKUP_METHODS for method in self.methods):
            result['probackup'] = self.check_probackup()
        if any(method in measure.PARCOPY_METHODS for method in self.methods):
            result['parcopy'] = self.check_parcopy()
        result['writeback_defaults'] = self.record_writeback_defaults()
        return result

    def preflight_audit(self) -> dict:
        """--syscall-audit: strace on dest, and what run.json says about the audit."""
        strace = self.run['environment']['dest'].get('strace')
        if not strace:
            raise SystemExit('--syscall-audit needs strace on dest (infra/gcp/startup.sh installs it)')
        return {'strace': strace, 'argv': durability_audit.strace_argv('<log>.strace'),
                'filesystem': self.args.audit_filesystem,
                'what': ("EXP-11's syscall audit: every command of every timed phase on dest runs under strace, "
                         "its trace beside its log, and each run's record holds the audit of its traces against "
                         "the durability definition (experiments/network/durability_audit.py). The runs time "
                         'nothing')}

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
        problems = block_problems(args, self.methods) + load_problems(args, self.methods) + \
            t12_problems(args, self.methods) + [
                problem for problem in [checksum_problem(self.methods, args.manifest_checksums, args.load,
                                                         getattr(args, 'conditions', ''))] if problem]
        if self.t10a and (args.load_rate <= 0 or (args.moderate_repeats and args.moderate_rate <= 0)):
            problems.append('EXP-12 needs --load-rate (and --moderate-rate for its moderate block) above 0: the smoke '
                            "run's R_busy and R_mod (its item 2)")
        if problems:
            raise SystemExit('; '.join(problems))
        if self.t12:
            # EXP-13: its conditions take the speeds' place; one where no method runs in any repeat isn't visited.
            self.speeds = [condition for condition in self.conditions
                           if any(self.t12_methods_at(condition, repeat) for repeat in range(args.repeats))]
        # A speed at which --method-speeds leaves no method is not visited at all.
        self.speeds = [speed for speed in self.speeds
                       if self.t12 or measure.methods_at(self.methods, self.method_speeds, speed['label'])]
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
        if stale and any(method in measure.OUTPUT_START_METHODS or (self.t10a and method in measure.G4_METHODS)
                         for method in self.pinned_methods):
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
                          'already paid. T2 (EXP-11): every arm is one timed phase, one counter '
                          'window: the pg_basebackup arms transfer (their --verbose lines '
                          'timestamped as they arrive split it: pbb_*_s), probackup* catchup_sync '
                          '(steps catchup, then sync -f of the output), parcopy* receiver_transfer '
                          "as pgdelta_direct*; starting the copy is outside, and usable_s adds the "
                          "start's time to consistency",
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
                'methods_by_speed': {speed['label']: (measure.methods_at(self.methods, self.method_speeds,
                                                                         speed['label']) if not self.t12 else
                                                      {repeat + 1: self.t12_methods_at(speed, repeat)
                                                       for repeat in range(args.repeats)})
                                     for speed in self.speeds},
                'method_flags': {method: self.method_flags(method) for method in self.pinned_methods},
                'pgbackrest': self.pgbackrest_run_record(),
                # T2 (EXP-11): whether this run is one, and its blocks after the main one (unthrottled).
                't2': self.t2,
                'blocks': {'main': {'repeats': args.repeats},
                           'extension': {'repeats': args.extension_repeats, 'methods': self.extension_methods,
                                         'speed': measure.UNTHROTTLED, 'source_cache': 'cold'},
                           'warm_source': {'repeats': args.warm_source_repeats,
                                          'methods': self.warm_source_methods, 'speed': measure.UNTHROTTLED,
                                          'source_cache': 'warm'}},
                'durability': DURABILITY_POLICY,
                # EXP-12 (T10a): its load, blocks, timeline and oracle; None outside it.
                't10a': self.load_run_record() if self.t10a else None,
                # EXP-13 (T12): its conditions, schedule and the control path; None outside it.
                't12': self.t12_run_record() if self.t12 else None,
                'control': {'port': getattr(args, 'control_port', 0) or 22, 'multiplexed': True,
                            'control_path': self.source.control, 'ssh_options': self.source.options},
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
            if self.t2 or self.t10a:
                # EXP-12 holds dest to EXP-11's durability standard in full: the same preflight.
                self.run['t2_preflight'] = self.preflight_t2()
            if self.t10a:
                self.run['t10a_preflight'] = self.preflight_t10a()
            if self.t12:
                self.run['t12_preflight'] = self.preflight_t12()
                save_json(self.out / 'run.json', self.run)
            if args.syscall_audit:
                self.run['syscall_audit'] = self.preflight_audit()
            if self.gnu_time:
                self.run['rss_probe'] = self.rss_probe()
            if self.t2 or any(method in measure.PGDELTA_METHODS for method in self.pinned_methods):
                self.run['pgdelta_required_options'] = measure.pgdelta_required_options(self.pinned_methods)
                self.run['pgdelta_jobs_check'] = self.check_pgdelta_jobs()
            if args.disk_test_mib:
                self.run['disk_test'] = dict(zip(('source', 'dest'), self.both(
                    lambda: self.local_agent('disk-test', str(self.work), '--mib',
                                             str(args.disk_test_mib)),
                    lambda: self.source.agent('disk-test', str(self.work), '--mib',
                                              str(args.disk_test_mib)))))
            if args.idle_seconds:
                self.run['background'] = self.background()
            if self.t2 or self.t10a:
                # Check (a)'s idle noise: given (the smoke run's), or measured now, or 0 (the 4 MiB floor).
                if args.idle_noise_windows:
                    self.run['dirty_idle_noise'] = self.dirty_idle_noise(args.idle_noise_windows)
                measured = (self.run.get('dirty_idle_noise') or {}).get('max_increase_bytes') or 0
                self.idle_noise_bytes = max(int(args.dirty_noise_mib * (1 << 20)), measured, 0)
                self.run['idle_noise_bytes'] = self.idle_noise_bytes
            if args.idle_noise_plan or args.dirty_noise_table:
                # EXP-13: check (a)'s noise per window length, given and measured; each run's limit follows its clock.
                measured = {}
                if args.idle_noise_plan:
                    # In place of EXP-11's one window length (--idle-noise-windows, refused beside it).
                    self.run['dirty_idle_noise'] = self.dirty_idle_noise_plan(
                        measure.parse_noise_plan(args.idle_noise_plan))
                    measured = {float(length): value for length, value in
                                self.run['dirty_idle_noise']['max_increase_bytes'].items()}
                self.noise_table = measure.noise_table(measure.parse_noise_table(args.dirty_noise_table), measured)
                self.run['noise_table_bytes'] = {f'{length:g}': value for length, value in self.noise_table.items()}
            if args.phase_floor_repeats:
                self.run['phase_floor'] = self.phase_floor(args.phase_floor_repeats)
            save_json(self.out / 'run.json', self.run)
            combos = list(itertools.product(patterns, fractions))
            for index, (pattern, fraction) in enumerate(combos):
                trial = self.run_trial(index, pattern, fraction)
                self.run['trials'][trial['trial_id']] = trial['status']
                if self.writeback_stuck:
                    # Every later run would be refused by the guard: the invocation stops here.
                    self.run['stopped'] = self.writeback_stuck
                    log(f'stopping: {self.writeback_stuck}')
                save_json(self.out / 'run.json', self.run)
                if self.writeback_stuck or (trial['status'] == 'error' and args.stop_on_error):
                    break
        finally:
            if self.t12 and self.run:
                # EXP-13: no tree and Debian's TCP settings on both VMs, whatever happened; what tc then shows.
                try:
                    off = self.network_off()
                    self.run['teardown'] = {vm: {'qdiscs': off['wan'][vm].get('qdiscs'),
                                                 'tree_is_wan': (off['wan'][vm].get('tree') or {}).get('is_wan')}
                                            for vm in ('source', 'dest')}
                    self.run['teardown']['tcp'] = (off.get('tcp') or {}).get('check')
                except Exception as exc:
                    self.run['teardown'] = {'error': f'{type(exc).__name__}: {exc}'}
                    log(f'could not reset the network: {exc}')
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
                             '--verify sequential; the T2, T3, T8a and T11 methods are in the README)')
    parser.add_argument('--method-speeds', action='append', default=[], metavar='METHODS=SPEEDS',
                        help="run some methods at some of --speeds only, e.g. "
                             "'pgdelta_seq+pgdelta_inplace=unthrottled' (methods joined with +, speeds "
                             'as in --speeds); repeatable. Methods not named run at every speed')
    parser.add_argument('--pgbackrest', default='/usr/bin/pgbackrest',
                        help='pgBackRest on both VMs (pgbackrest_* methods)')
    parser.add_argument('--pg-probackup', default=measure.PROBACKUP_EXE,
                        help='pg_probackup on both VMs (probackup* methods; its remote mode runs it on the '
                             'source from the same directory)')
    parser.add_argument('--probackup-byte-diagnostic', choices=('every', 'first_repeat'), default='every',
                        help="T2: probackup*'s byte diagnostic (against the trial's start reference digest) in "
                             "every run, or only in each trial's first repeat (EXP-11's first cut)")
    parser.add_argument('--probackup-wal-ticker', type=float, default=0.1, metavar='SECONDS',
                        help='T2: during probackup* runs, emit one empty WAL message on the source every SECONDS '
                             "(pg_logical_emit_message), so pg_probackup's stream sees WAL arrive as on a live "
                             'primary; 0 turns it off (added after EXP-11\'s smoke run)')
    parser.add_argument('--syscall-audit', action='store_true',
                        help="T2: EXP-11's syscall audit: every command of every timed phase on dest runs under "
                             'strace -f -tt -y -e trace=%%file,%%desc, and each record holds the audit of its '
                             'traces (experiments/network/durability_audit.py). Untimed: the times mean nothing')
    parser.add_argument('--audit-filesystem', default=durability_audit.DEFAULT_FILESYSTEM,
                        help="--syscall-audit: the outputs' mount point, which a syncfs of it covers")
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--extension-repeats', type=int, default=0,
                        help='T2: after the main block, this many more unthrottled repeats of '
                             '--extension-methods in each trial, each repeat in its own random order')
    parser.add_argument('--extension-methods', default='', metavar='METHODS',
                        help='T2: comma list from --methods for the extension')
    parser.add_argument('--warm-source-repeats', type=int, default=0,
                        help="T2: after the extension, this many unthrottled repeats of --warm-source-methods "
                             "with the source's page cache warm: no drop there, one untimed read of its data "
                             "directory without pg_wal instead; dest's cache is dropped as usual")
    parser.add_argument('--warm-source-methods', default='', metavar='METHODS',
                        help='T2: comma list from --methods for the warm-source block')
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
                             "EXP-04's sweep ran; PostgreSQL's own default is CRC32C (EXP-07), which "
                             "T2's methods require")
    parser.add_argument('--gnu-time', default='/usr/bin/time',
                        help="GNU time, for each tool's own peak RSS ('none' to skip)")
    parser.add_argument('--link-test-seconds', type=float, default=3.0)
    parser.add_argument('--idle-seconds', type=float, default=5.0,
                        help='idle window to measure background traffic (0 to skip)')
    parser.add_argument('--idle-noise-windows', type=int, default=0,
                        help="T2: measure dest's Dirty + Writeback increase over this many idle windows of "
                             "--idle-seconds at the start (the smoke run's 20); its largest is check (a)'s "
                             'noise')
    parser.add_argument('--dirty-noise-mib', type=float, default=0.0,
                        help="T2: check (a)'s idle noise in MiB, as the smoke run measured it (the limit is "
                             'the larger of 4 MiB, this and --idle-noise-windows')
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
    # EXP-13 (T12): latency and transport, at the emulated WAN's conditions (infra/gcp/wan.sh).
    wan = parser.add_argument_group('EXP-13 (T12)')
    wan.add_argument('--conditions', default='', metavar='RATE:RTT_MS:HOST,...',
                     help='EXP-13: comma list of conditions in place of --speeds, each a link rate (Gbit/s), an added '
                          'round trip (ms; half on each VM) and a host TCP setting (default or tuned), e.g. '
                          '2:0:default,2:60:tuned. Each repeat visits them in a seeded random order')
    wan.add_argument('--method-conditions', action='append', default=[], metavar='METHODS=CONDITIONS',
                     help='run some methods at some conditions only (methods joined with +); repeatable')
    wan.add_argument('--method-repeats', action='append', default=[], metavar='METHODS=REPEATS',
                     help='run some methods in some repeats only (counted from 1), e.g. probackup8=1,3,5')
    wan.add_argument('--baseline-methods', default='', metavar='METHODS',
                     help='these methods (those of --methods) run in --baseline-repeats only')
    wan.add_argument('--baseline-repeats', default='', metavar='REPEATS', help='e.g. 2,4 (counted from 1)')
    wan.add_argument('--baseline-every-repeat', default='', metavar='CONDITIONS',
                     help='where the baseline methods run in every repeat')
    wan.add_argument('--negative-control', default='', metavar='METHOD@CONDITIONS',
                     help="e.g. full_nosync@2:0:default: that method at those conditions in each trial's first repeat")
    wan.add_argument('--zero-delay-link-rates', default='', metavar='GBITS',
                     help="the trial's one-stream link tests through the tree at r = 0, before the repeats, at these "
                          "rates (the crossovers' x where no anchor runs)")
    wan.add_argument('--first-visit-link-streams', action='append', default=[], metavar='N@CONDITIONS',
                     help="at a trial's first visit to those conditions, an N-stream link test too (besides the "
                          '8-stream one); repeatable')
    wan.add_argument('--ssh-probe-port', type=int, default=0,
                     help="the SSH probe at each trial's first visit to a condition with r > 0, on this port (22)")
    wan.add_argument('--stunnel-link-test', default='', metavar='CONDITIONS',
                     help="a one-stream link test through the stunnel pair at the trial's first visit there")
    wan.add_argument('--control-port', type=int, default=0,
                     help="the source's sshd port for the harness's control path (2222), which wan.sh leaves undelayed")
    wan.add_argument('--tuned-tcp-max-bytes', type=int, default=measure.TUNED_TCP_MAX_BYTES,
                     help="tuned hosts' tcp_rmem and tcp_wmem maxima, on both VMs")
    wan.add_argument('--link-test-warmup', type=float, default=None, metavar='SECONDS',
                     help="the link test receiver's warmup (blast-recv --warmup); the steady rate is then the median "
                          'of the last 3 one-second rates after it')
    wan.add_argument('--idle-noise-plan', default='', metavar='SECONDS:WINDOWS,...',
                     help="check (a)'s idle noise measured at the start over this many idle windows of each length, "
                          "e.g. 5:20,30:6,120:3 (in place of --idle-noise-windows)")
    wan.add_argument('--dirty-noise-table', default='', metavar='SECONDS:MIB,...',
                     help="check (a)'s idle noise per window length, as the smoke run measured it (in place of "
                          "--dirty-noise-mib): a run's limit is max(4 MiB, the noise of the shortest length that "
                          'covers its clock)')
    wan.add_argument('--stunnel', default='/usr/bin/stunnel4', help="v1_tls's proxy (Debian's stunnel4)")
    wan.add_argument('--stunnel-strace', action='store_true',
                     help='smoke item 12: run each stunnel under strace -e trace=setsockopt (TCP_NODELAY)')
    wan.add_argument('--tls-refusal-check', action='store_true',
                     help="smoke item 12: before the first trial, dest's stunnel refuses a client without a "
                          'certificate and one from a wrong CA')
    # EXP-12 (T10a): the source's slowdown under a fixed-rate load. Its blocks: busy (--repeats, --speeds,
    # --methods, --method-speeds), warm (--warm-source-*), moderate (--moderate-*), calibration (--calibration-*).
    load = parser.add_argument_group('EXP-12 (T10a)')
    load.add_argument('--load', choices=('bank',),
                      help="EXP-12: a fixed-rate pgbench load on the source's loaddb (bank: commit.sql@99 and "
                           'abort.sql@1, experiments/network/load), with its trial order, its timeline and its arms')
    load.add_argument('--load-rate', type=int, default=0, metavar='R',
                      help="the busy and warm blocks' -R, transactions per second (smoke item 2's R_busy); 0 refuses "
                           'to run')
    load.add_argument('--load-clients', type=int, default=32)
    load.add_argument('--load-threads', type=int, default=4)
    load.add_argument('--load-warmup-s', type=float, default=120, help='after each load start, before anything else')
    load.add_argument('--load-duration-s', type=int, default=14400, help="pgbench's -T (each run is stopped before)")
    load.add_argument('--load-seed', type=int, help="pgbench's --random-seed base (default: --order-seed)")
    load.add_argument('--load-log-dir', default=measure.LOAD_LOG_DIR,
                      help="the load's per-transaction logs and the sampler's data on the source (tmpfs)")
    load.add_argument('--load-signal', choices=('ALRM', 'INT'), default='ALRM',
                      help="how a run is stopped: SIGALRM, the signal pgbench's own -T timer uses (smoke item 1's "
                           'fallback is INT)')
    load.add_argument('--capacity-warmup-s', type=int, default=30)
    load.add_argument('--capacity-s', type=int, default=60)
    load.add_argument('--capacity-runs', type=int, default=1, help='closed-loop runs for C (the smoke run takes 3)')
    load.add_argument('--capacity-rest-s', type=float, default=30)
    load.add_argument('--settle-s', type=float, default=15, help="each run's settle: at least this")
    load.add_argument('--settle-max-s', type=float, default=60,
                      help="the source's settle gate's cap (then the run is unsettled); dest's work has none")
    load.add_argument('--tail-s', type=float, default=15, help="each run's quiet tail, from its clock's stop")
    load.add_argument('--window-tail-s', type=float, default=10,
                      help='the slowdown window runs this long past the clock (recorded for the analysis)')
    load.add_argument('--none-window-s', type=float, default=60, help="none's empty timed phase")
    load.add_argument('--source-cache', choices=('evict', 'keep'), default='evict',
                      help='evict the static files before each cold run (keep: leave the cache alone, tests only)')
    load.add_argument('--snapshot', action='store_true',
                      help="take the proxies' snapshot of the data directory at the trial's step 5")
    load.add_argument('--static-digest', choices=('every', 'first_repeat', 'none'), default='every',
                      help="(vi)'s logical digest of the benchmark database: every live output, or each (arm, speed) "
                           "cell's first repeat; row counts are taken in every run")
    load.add_argument('--controls', default='', metavar='CONTROLS',
                      help=f'comma list from {", ".join(measure.CONTROL_METHODS)}: at the busy block\'s start and end, '
                           "and pause2 at the moderate block's start")
    load.add_argument('--warm-source-speeds', default='',
                      help="EXP-12: the warm block's speeds (default, as EXP-11's: unthrottled)")
    load.add_argument('--moderate-rate', type=int, default=0, metavar='R', help="the moderate block's -R")
    load.add_argument('--moderate-repeats', type=int, default=0)
    load.add_argument('--moderate-speeds', default='')
    load.add_argument('--moderate-methods', default='', metavar='METHODS')
    load.add_argument('--moderate-method-speeds', action='append', default=[], metavar='METHODS=SPEEDS')
    load.add_argument('--calibration-repeats', type=int, default=0)
    load.add_argument('--calibration-speed', default='2')
    load.add_argument('--calibration-methods', default='', metavar='METHODS',
                      help="the calibration block's arms, without load: any methods and controls")
    load.add_argument('--calibration-idle-s', type=float, default=30, help='idle before the calibration block')
    load.add_argument('--load-check-wait-s', type=float, default=20, help='from the load stop to load-check')
    load.add_argument('--idle-rate-s', type=float, default=20, help="step 6's idle background CPU window")
    load.add_argument('--sampler-interval-s', type=float, default=0.1)
    load.add_argument('--clock-samples', type=int, default=50)
    load.add_argument('--oracle-controls', action='store_true',
                      help="EXP-12's smoke item 5: the oracle's seven negative controls, under the load")
    args = parser.parse_args(argv)
    if args.scale < 1 or args.repeats < 1:
        parser.error('--scale and --repeats must be at least 1')
    if args.idle_noise_windows < 0 or args.dirty_noise_mib < 0:
        parser.error('--idle-noise-windows and --dirty-noise-mib must be 0 or more')
    try:
        speeds = measure.parse_speeds(args.speeds)
        methods = method_list(args.methods)
        measure.parse_method_speeds(args.method_speeds, methods, speeds)
    except ValueError as exc:
        parser.error(str(exc))
    problems = block_problems(args, methods) + load_problems(args, methods) + t12_problems(args, methods) + [
        problem for problem in [checksum_problem(methods, args.manifest_checksums, args.load, args.conditions)]
        if problem]
    if problems:
        parser.error('; '.join(problems))
    return args


def t12_plan(args) -> dict:
    """EXP-13's schedule and probes from its options (t12_problems has checked them): each arm's conditions and
    repeats, the baselines' repeats, the negative control, the first-visit link tests, the stunnel link tests and
    the zero-delay rates. Repeats are counted from 1, as the specification counts them."""
    conditions = measure.parse_conditions(args.conditions)
    methods = method_list(args.methods)

    def labels(text):
        return [condition['label'] for condition in measure.parse_conditions(text)] if text else []

    return {'method_conditions': measure.parse_method_conditions(args.method_conditions, methods, conditions),
            'method_repeats': measure.parse_method_repeats(args.method_repeats, methods),
            'baseline_methods': [method for method in method_list(args.baseline_methods) if method in methods],
            'baseline_repeats': measure.parse_repeats(args.baseline_repeats),
            'baseline_every': labels(args.baseline_every_repeat),
            'negative_control': measure.parse_method_at(args.negative_control, conditions),
            'first_visit_streams': measure.parse_count_at(args.first_visit_link_streams, conditions),
            'stunnel_link_test': labels(args.stunnel_link_test),
            'zero_delay_rates': [measure.parse_speed(rate)['gbit'] for rate in method_list(args.zero_delay_link_rates)]}


T12_OPTIONS = ('method_conditions', 'method_repeats', 'baseline_methods', 'baseline_repeats', 'baseline_every_repeat',
               'negative_control', 'zero_delay_link_rates', 'ssh_probe_port', 'stunnel_link_test',
               'first_visit_link_streams', 'tls_refusal_check')


def t12_problems(args, methods) -> list[str]:
    """What is wrong with EXP-13's options for these --methods (an empty list if nothing is).

    Its arms and options need --conditions; --conditions needs --control-port (the harness's SSH must stay
    outside the emulated WAN) and replaces --speeds and --method-speeds; EXP-12's load and EXP-11's later blocks
    don't run with it. Every condition an option names must be one of --conditions.
    """
    problems = []
    given = [f'--{name.replace("_", "-")}' for name in T12_OPTIONS if getattr(args, name, None)]
    t12 = [method for method in methods if method in measure.T12_METHODS]
    for name, text in (('--idle-noise-plan', args.idle_noise_plan), ('--dirty-noise-table', args.dirty_noise_table)):
        try:
            (measure.parse_noise_plan if name == '--idle-noise-plan' else measure.parse_noise_table)(text)
        except ValueError as exc:
            problems.append(f'{name}: {exc}')
    if args.link_test_warmup is not None and not 0 <= args.link_test_warmup < args.link_test_seconds:
        problems.append('--link-test-warmup must be 0 or more and less than --link-test-seconds')
    if args.idle_noise_plan and args.idle_noise_windows:
        problems.append('--idle-noise-plan replaces --idle-noise-windows: give one')
    if args.dirty_noise_table and args.dirty_noise_mib:
        problems.append('--dirty-noise-table replaces --dirty-noise-mib: give one')
    if not args.conditions:
        if t12 or given:
            problems.append(f'{", ".join(t12 + given)} need --conditions (EXP-13)')
        return problems
    if not args.control_port:
        problems.append("--conditions needs --control-port: the harness's SSH stays outside the emulated WAN")
    if args.method_speeds:
        problems.append('--method-speeds is for --speeds; with --conditions, use --method-conditions')
    if getattr(args, 'load', None) is not None or args.extension_repeats or args.warm_source_repeats:
        problems.append("--conditions doesn't run with --load, --extension-repeats or --warm-source-repeats")
    if args.tuned_tcp_max_bytes < 1:
        problems.append('--tuned-tcp-max-bytes must be positive')
    try:
        plan = t12_plan(args)
    except ValueError as exc:
        return problems + [str(exc)]
    labels = [condition['label'] for condition in measure.parse_conditions(args.conditions)]
    for name, chosen in (('--baseline-every-repeat', plan['baseline_every']),
                         ('--stunnel-link-test', plan['stunnel_link_test'])):
        unknown = [label for label in chosen if label not in labels]
        if unknown:
            problems.append(f'{name}: {", ".join(unknown)} not in --conditions')
    unknown = [method for method in method_list(args.baseline_methods) if method not in measure.ALL_METHODS]
    if unknown:
        problems.append(f'--baseline-methods: {", ".join(unknown)} are not methods')
    control = plan['negative_control']
    if control and control['method'] not in measure.ALL_METHODS:
        problems.append(f'--negative-control: {control["method"]} is not a method')
    if not methods and not control:
        problems.append('--conditions needs --methods')
    return problems


def load_problems(args, methods) -> list[str]:
    """What is wrong with EXP-12's options for these --methods (an empty list if nothing is).

    Its arms and options need --load; the controls run only through --controls (and the calibration block); the
    moderate block's methods come from --methods, the calibration's from any method or control; the proxies need
    --snapshot. A rate of 0 parses (the run line's shape before the smoke run) and execute refuses it.
    """
    problems = []
    loaded = getattr(args, 'load', None) is not None
    t10a = [method for method in methods if method in measure.T10A_METHODS]
    options = {'--controls': args.controls, '--moderate-repeats': args.moderate_repeats,
               '--calibration-repeats': args.calibration_repeats, '--snapshot': args.snapshot,
               '--oracle-controls': args.oracle_controls, '--warm-source-speeds': args.warm_source_speeds}
    if not loaded:
        given = [name for name, value in options.items() if value]
        if t10a or given:
            problems.append(f'{", ".join(t10a + given)} need --load (EXP-12)')
        return problems
    controls, moderate = method_list(args.controls), method_list(args.moderate_methods)
    calibration = method_list(args.calibration_methods)
    if [method for method in methods if method in measure.CONTROL_METHODS]:
        problems.append('the controls run through --controls, not --methods')
    unknown = [method for method in controls if method not in measure.CONTROL_METHODS]
    if unknown or len(set(controls)) != len(controls):
        problems.append(f'--controls must be distinct names from {", ".join(measure.CONTROL_METHODS)}')
    if min(args.load_rate, args.moderate_rate) < 0 or args.load_clients < 1 or args.load_threads < 1:
        problems.append('--load-rate and --moderate-rate must be 0 or more, and --load-clients and --load-threads '
                        'at least 1')
    for block, repeats, chosen in (('moderate', args.moderate_repeats, moderate),
                                   ('calibration', args.calibration_repeats, calibration)):
        if repeats < 0:
            problems.append(f'--{block}-repeats must be 0 or more')
        if bool(repeats) != bool(chosen):
            problems.append(f'--{block}-repeats and --{block}-methods go together')
    if [method for method in moderate if method not in methods] or len(set(moderate)) != len(moderate):
        problems.append('--moderate-methods must be distinct names from --methods')
    if args.moderate_repeats and not args.moderate_speeds:
        problems.append('--moderate-repeats needs --moderate-speeds')
    known = (*measure.ALL_METHODS, *measure.CONTROL_METHODS)
    if [method for method in calibration if method not in known] or len(set(calibration)) != len(calibration):
        problems.append('--calibration-methods must be distinct method or control names')
    try:
        moderate_speeds = measure.parse_speeds(args.moderate_speeds) if args.moderate_speeds else []
        measure.parse_method_speeds(args.moderate_method_speeds, moderate, moderate_speeds)
        if args.warm_source_speeds:
            measure.parse_speeds(args.warm_source_speeds)
        measure.parse_speed(args.calibration_speed)
    except ValueError as exc:
        problems.append(str(exc))
    proxies = [method for method in (*methods, *calibration) if method in measure.V1PROXY_METHODS]
    if proxies and not args.snapshot:
        problems.append(f'{", ".join(sorted(set(proxies)))} read the snapshot: they need --snapshot')
    if args.settle_s < 0 or args.settle_max_s < args.settle_s or args.tail_s < 0:
        problems.append('--settle-s, --settle-max-s (at least --settle-s) and --tail-s must be 0 or more')
    return problems


def _terminate(signum, frame):
    raise KeyboardInterrupt(f'signal {signum}')


def main(argv=None) -> int:
    args = parse_args(argv)
    signal.signal(signal.SIGTERM, _terminate)
    return Orchestrator(args).execute()


if __name__ == '__main__':
    raise SystemExit(main())
