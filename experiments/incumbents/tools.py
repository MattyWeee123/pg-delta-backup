"""T11 tool adapters, and the bring-up that checks what a tool left behind.

Every incumbent works on its own fresh copy of B (``job.target``) and its own copy of
the scenario's kept source cluster, started for the run (``job.source``), so no run
sees another's WAL, backups or changes. pgdelta builds O from the scenario's S and B
through EXP-03's evaluate path and never writes to either.

An accepted copy is brought up the way its tool intends:
- the incumbents' copies as a standby of the source: recovery configuration (pg_rewind
  writes its own with --write-recovery-conf), start, wait until replay reaches the
  source's current LSN, compare the logical state as a hot standby, promote, pg_amcheck,
  CHECKPOINT and a clean stop;
- pgdelta's O as EXP-03 restores it: started from a disposable copy until recovery
  ends, logical state compared, then pg_amcheck, CHECKPOINT and a clean stop.
B's post-fork pages are compared with the copy twice: right after the tool
(diverged_before_recovery) and once the copy has stopped cleanly after that bring-up
(diverged_after_recovery, which the verdict uses).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from experiments.incumbents import checks
from experiments.tricky_copies import evaluate as evaluation
from experiments.tricky_copies.lsnscan import BLOCK, page_lsn, parse_lsn, relation_kind
from experiments.tricky_copies.logical import compare_logical_states, compute_logical_state
from experiments.tricky_copies.pgcluster import (SUPERUSER, Cluster, CommandError, clean_env,
                                                 free_port, run, short_tempdir)

TOOLS = ('pg_rewind', 'pg_probackup', 'walg', 'walg_send', 'pgdelta', 'lsn_skip')
# pg_probackup's documented catch-up of a lagging standby keeps these; catchup would
# otherwise overwrite them with the source's.
PROBACKUP_KEEP_CONFIG = ('postgresql.conf', 'postgresql.auto.conf', 'pg_hba.conf',
                         'pg_ident.conf')
# Files the lsn_skip control never copies from the source, nor deletes from the copy.
CONTROL_KEEP = PROBACKUP_KEEP_CONFIG + ('postmaster.pid', 'postmaster.opts', 'standby.signal',
                                        'recovery.signal')
# A standby whose log shows one of these will never reach the source by waiting longer.
HOPELESS = (
    r'requested WAL segment \S+ has already been removed',
    r"is not a child of this server's history",
    r'forked off current database system timeline',
    r'database system identifier differs',
    r'could not locate a valid checkpoint record',
    r'aborting startup due to startup process failure',
    r'PANIC:',
)


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def as_text(value):
    """Captured output as text; TimeoutExpired carries bytes even in text mode."""
    if value is None:
        return ''
    return value.decode('utf-8', 'replace') if isinstance(value, bytes) else value


# --------------------------------------------------------------------------
# Which tools exist
# --------------------------------------------------------------------------
def resolve_binary(value, names):
    """(path or None, reason). ``value`` is a path, 'auto' (search PATH for ``names``) or
    'none'."""
    if value in (None, '', 'none'):
        return None, 'disabled'
    if value == 'auto':
        for name in names:
            found = shutil.which(name)
            if found:
                return Path(found), None
        return None, f"not found on PATH (looked for {', '.join(names)}); pass its path"
    path = Path(value)
    if not (path.is_file() and os.access(path, os.X_OK)):
        return None, f'{path} is not an executable file'
    return path, None


def first_line(cmd):
    try:
        proc = run(cmd, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f'{type(exc).__name__}: {exc}'
    text = (proc.stdout or proc.stderr).strip()
    return (text.splitlines()[0] if text else None), proc.stdout + proc.stderr


def probe(tool, *, pgbin, pg_probackup='auto', wal_g='auto'):
    """{'available', 'path', 'version', 'reason', ...} for one tool."""
    info = {'tool': tool, 'available': False, 'path': None, 'version': None, 'reason': None}
    if tool == 'pg_rewind':
        path = pgbin.bindir / 'pg_rewind'
        if not path.is_file():
            return {**info, 'reason': f'{path} not found'}
        info.update(path=str(path), version=first_line([path, '--version'])[0], available=True)
    elif tool == 'pgdelta':
        if not evaluation.tool_importable(sys.executable, evaluation.REPO_ROOT):
            return {**info, 'reason': f'pgdelta is not importable from {evaluation.REPO_ROOT}'}
        from pgdelta import __version__
        info.update(path=f'{sys.executable} -m pgdelta', version=f'pgdelta {__version__}',
                    available=True)
    elif tool == 'lsn_skip':
        info.update(path='experiments/incumbents/tools.py', available=True,
                    version="EXP-03's LSN-skip shortcut (negative control)")
    elif tool == 'pg_probackup':
        names = (f'pg_probackup-{pgbin.major}', 'pg_probackup') if pg_probackup == 'auto' else ()
        path, reason = resolve_binary(pg_probackup, names)
        if path is None:
            return {**info, 'reason': f'pg_probackup: {reason}'}
        version = first_line([path, '--version'])[0]
        info.update(path=str(path), version=version, available=True)
        if version and f'PostgreSQL {pgbin.major}' not in version:
            info.update(available=False, reason=f'{version} is not built for PostgreSQL '
                                                f'{pgbin.major}')
    elif tool in ('walg', 'walg_send'):
        path, reason = resolve_binary(wal_g, ('wal-g', 'wal-g-pg'))
        if path is None:
            return {**info, 'reason': f'WAL-G: {reason}'}
        info.update(path=str(path), version=first_line([path, '--version'])[0])
        command = 'catchup-push' if tool == 'walg' else 'catchup-send'
        _, help_text = first_line([path, '--help'])
        if command not in (help_text or ''):
            return {**info, 'reason': f'this WAL-G has no {command} command'}
        info['available'] = True
        if tool == 'walg':
            _, push_help = first_line([path, 'catchup-push', '--help'])
            info['sysid_flag'] = checks.walg_sysid_flag(push_help)
            info['from_lsn_flag'] = checks.walg_from_lsn_flag(push_help)
    else:
        raise ValueError(f'unknown tool {tool!r}')
    return info


# --------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------
@dataclass
class ToolRun:
    commands: list = field(default_factory=list)
    returncode: Optional[int] = None
    timed_out: bool = False
    output: str = ''
    seconds: Optional[float] = None
    parsed: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    # Set when the harness, not the tool, kept the run from completing (e.g. no backup name
    # to fetch, or a receiver that never listened); the run then says nothing about the tool.
    harness_error: Optional[str] = None


class Job:
    """One (scenario, tool) run: its directories, servers and command log."""

    def __init__(self, *, scenario_id, scenario_dir, meta, expected, listed_pages, tool,
                 tool_info, run_dir, pgbin, tool_timeout=1800, catchup_timeout=120,
                 keep=False, verbose=True):
        self.scenario_id, self.scenario_dir = scenario_id, Path(scenario_dir)
        self.meta, self.expected, self.listed_pages = meta, expected, listed_pages
        self.tool, self.tool_info = tool, tool_info
        self.run_dir, self.pgbin = Path(run_dir), pgbin
        self.tool_timeout, self.catchup_timeout = tool_timeout, catchup_timeout
        self.keep, self.verbose = keep, verbose
        self.logs = self.run_dir / 'logs'
        self.work = self.run_dir / 'work'
        self.old_copy = self.scenario_dir / 'old_copy'
        self.source_backup = self.scenario_dir / 'source_backup'
        self.source = None
        self.target = None
        self.sockdirs = []
        self.servers = []
        self.counter = 0

    def say(self, text):
        if self.verbose:
            print(f'  {self.scenario_id}/{self.tool}: {text}', flush=True)

    def next_log(self, name):
        self.counter += 1
        return self.logs / f'{self.counter:02d}-{name}.log'

    def relative(self, path):
        return Path(path).relative_to(self.run_dir).as_posix()

    def command(self, name, cmd, *, env=None, timeout=None):
        """Run one tool command; return its record, its stdout and its stderr."""
        cmd = [str(part) for part in cmd]
        log = self.next_log(name)
        started = time.monotonic()
        returncode, timed_out = None, False
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, env=env or clean_env(),
                                  cwd=self.run_dir, timeout=timeout or self.tool_timeout)
            stdout, stderr, returncode = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            stdout, stderr, timed_out = as_text(exc.stdout), as_text(exc.stderr), True
        except OSError as exc:
            stdout, stderr = '', f'{type(exc).__name__}: {exc}'
        seconds = round(time.monotonic() - started, 2)
        log.write_text('$ ' + shlex.join(cmd) + '\n--- stdout\n' + stdout + '\n--- stderr\n'
                       + stderr)
        record = {'name': name, 'command': cmd, 'returncode': returncode,
                  'timed_out': timed_out, 'seconds': seconds,
                  'stdout_tail': checks.tail(stdout), 'stderr_tail': checks.tail(stderr),
                  'log': self.relative(log)}
        return record, stdout, stderr

    def new_server(self, name, datadir):
        sockdir = short_tempdir()
        self.sockdirs.append(sockdir)
        cluster = Cluster(name, self.pgbin, datadir, sockdir, free_port(),
                          self.logs / f'{name}-server.log')
        self.servers.append(cluster)
        return cluster

    def start_source(self):
        """Copy the scenario's kept, stopped source cluster and start the copy."""
        kept = self.meta.get('source_cluster') or {}
        datadir = self.scenario_dir / kept.get('datadir', '')
        if not kept.get('kept') or not (datadir / 'PG_VERSION').is_file():
            raise RuntimeError(f'{self.scenario_dir} has no kept source cluster; build it with '
                               '--keep')
        shutil.copytree(datadir, self.work / 'source', symlinks=True)
        self.source = self.new_server('source', self.work / 'source')
        self.source.start()

    def copy_old_copy(self):
        """A fresh copy of B. copytree keeps mtimes, which WAL-G's catchup-send compares."""
        self.target = self.work / 'target'
        shutil.copytree(self.old_copy, self.target, symlinks=True)
        os.chmod(self.target, 0o700)

    def close(self):
        for cluster in self.servers:
            cluster.ensure_stopped()
        for sockdir in self.sockdirs:
            shutil.rmtree(sockdir, ignore_errors=True)
        if not self.keep:
            shutil.rmtree(self.work, ignore_errors=True)


def source_conninfo(cluster):
    return f'host={cluster.sockdir} port={cluster.port} user={SUPERUSER} dbname=postgres'


def walg_env(job, storage=None):
    """WAL-G's environment: the source copy's socket, and the run's file storage. The
    caller's own WALG_* and WALE_* settings are dropped, so they can't redirect it."""
    extra = {'PGHOST': str(job.source.sockdir), 'PGPORT': str(job.source.port),
             'PGUSER': SUPERUSER, 'PGDATABASE': 'postgres'}
    if storage is not None:
        extra['WALG_FILE_PREFIX'] = str(storage)
    env = {key: value for key, value in clean_env().items()
           if not key.startswith(('WALG_', 'WALE_'))}
    return {**env, **extra}


# --------------------------------------------------------------------------
# Adapters
# --------------------------------------------------------------------------
def run_pg_rewind(job):
    cmd = [job.pgbin.tool('pg_rewind'), '--target-pgdata', job.target,
           '--source-server', source_conninfo(job.source), '--write-recovery-conf']
    record, stdout, stderr = job.command('pg_rewind', cmd)
    output = stdout + stderr
    return ToolRun([record], record['returncode'], record['timed_out'], output, record['seconds'],
                   checks.parse_pg_rewind(output))


def run_pg_probackup(job):
    source = job.source
    cmd = [job.tool_info['path'], 'catchup', '-b', 'DELTA', f'--source-pgdata={source.datadir}',
           f'--destination-pgdata={job.target}', '--stream', '-h', source.sockdir,
           '-p', source.port, '-U', SUPERUSER, '-d', 'postgres',
           *(f'--exclude-path={name}' for name in PROBACKUP_KEEP_CONFIG)]
    record, stdout, stderr = job.command('pg_probackup-catchup', cmd)
    output = stdout + stderr
    return ToolRun([record], record['returncode'], record['timed_out'], output, record['seconds'],
                   checks.parse_pg_probackup(output))


def walg_push_command(walg, pgdata, redo_location, sysid_flag=None, sysid=None):
    """catchup-push's command line. --from-lsn gets B's REDO location as a decimal number:
    v3.0.9 parses the flag as an unsigned integer and rejects the X/Y form, later versions
    take either, and WAL-G's own catch-up test passes a decimal number."""
    cmd = [walg, 'catchup-push', pgdata, '--from-lsn', str(parse_lsn(redo_location))]
    if sysid_flag:
        cmd += [sysid_flag, sysid]
    return cmd


def run_walg(job):
    """catchup-push from B's REDO location into local file storage, then catchup-fetch."""
    walg = job.tool_info['path']
    storage = job.work / 'walg-storage'
    storage.mkdir(parents=True)
    env = walg_env(job, storage)
    redo = job.meta['old_copy']['redo_location']
    sysid = job.meta['source_backup']['system_identifier']
    parsed = {'from_lsn': redo, 'from_lsn_argument': str(parse_lsn(redo)),
              'source_system_identifier': sysid,
              'old_copy_system_identifier': job.meta['old_copy']['system_identifier'],
              'sysid_flag': job.tool_info.get('sysid_flag')}
    push = walg_push_command(walg, job.source.datadir, redo, parsed['sysid_flag'], sysid)
    record, stdout, stderr = job.command('catchup-push', push, env=env)
    output = stdout + stderr
    result = ToolRun([record], record['returncode'], record['timed_out'], output,
                     record['seconds'], parsed)
    if record['returncode'] != 0:
        return result
    name = checks.parse_walg_backup_name(output)
    if name is None:
        listing, listed, _ = job.command('catchup-list', [walg, 'catchup-list', '--json'],
                                         env=env)
        result.commands.append(listing)
        names = checks.parse_walg_list_json(listed)
        name = names[-1] if names else None
    parsed['backup_name'] = name
    if name is None:
        result.harness_error = ('catchup-push exited 0, but neither its output nor '
                                'catchup-list --json named a backup to fetch')
        return result
    fetch, fetched, fetch_errors = job.command(
        'catchup-fetch', [walg, 'catchup-fetch', job.target, name], env=env)
    result.commands.append(fetch)
    result.output += fetched + fetch_errors
    result.returncode, result.timed_out = fetch['returncode'], fetch['timed_out']
    result.seconds = round(record['seconds'] + fetch['seconds'], 2)
    return result


def run_walg_send(job, attempts=10):
    """catchup-receive into the copy of B, then catchup-send from the source to it."""
    walg = job.tool_info['path']
    port = free_port()
    env = walg_env(job)
    receive_cmd = [str(part) for part in (walg, 'catchup-receive', job.target, port)]
    receive_log = job.next_log('catchup-receive')
    sends, receiver_timed_out = [], False
    started = time.monotonic()
    with receive_log.open('w') as log:
        log.write('$ ' + shlex.join(receive_cmd) + '\n')
        log.flush()
        receiver = subprocess.Popen(receive_cmd, stdout=log, stderr=subprocess.STDOUT, env=env,
                                    cwd=job.run_dir)
        try:
            deadline = time.monotonic() + 30
            while (receiver.poll() is None and 'Receiving' not in receive_log.read_text()
                   and time.monotonic() < deadline):
                time.sleep(0.1)
            for _ in range(attempts):
                time.sleep(0.5)  # the receiver logs before it listens
                send, out, err = job.command('catchup-send', [walg, 'catchup-send',
                                                              job.source.datadir,
                                                              f'127.0.0.1:{port}'], env=env)
                sent = out + err
                sends.append((send, sent))
                if not (send['returncode'] and 'connection refused' in sent.lower()
                        and receiver.poll() is None):
                    break
            receiver_gone = receiver.poll() is not None
            # After a successful send the receiver finishes writing; after a failed one it
            # may wait forever for a sender that is gone.
            try:
                receiver.wait(timeout=job.tool_timeout if send['returncode'] == 0 else 10)
            except subprocess.TimeoutExpired:
                receiver_timed_out = send['returncode'] == 0
                receiver.kill()
                receiver.wait()
        finally:
            if receiver.poll() is None:
                receiver.kill()
                receiver.wait()
    received = receive_log.read_text()
    receive_record = {'name': 'catchup-receive', 'command': receive_cmd,
                      'returncode': receiver.returncode, 'timed_out': receiver_timed_out,
                      'seconds': round(time.monotonic() - started, 2),
                      'stdout_tail': checks.tail(received), 'stderr_tail': [],
                      'log': job.relative(receive_log)}
    send, sent = sends[-1]
    returncode = send['returncode'] or receiver.returncode
    harness_error = None
    if send['returncode'] and 'connection refused' in sent.lower():
        # catchup-receive only listens before a sender connects, so the tools never met.
        state = (f'exited with status {receiver.returncode}' if receiver_gone
                 else 'was not listening')
        harness_error = (f'catchup-send never reached catchup-receive on port {port} after '
                         f'{len(sends)} attempts; the receiver {state}')
    return ToolRun([*(record for record, _ in sends), receive_record], returncode,
                   send['timed_out'] or receiver_timed_out, sent + '\n' + received,
                   receive_record['seconds'],
                   {'port': port, 'send_attempts': len(sends),
                    'receiver_returncode': receiver.returncode}, harness_error=harness_error)


def run_pgdelta(job):
    """EXP-03's tool step: pgdelta local on the scenario's S and B, O in this run's work."""
    output = job.work / 'output'
    log = job.next_log('pgdelta')
    job.target = output
    result = evaluation.run_tool(job.scenario_dir, python=sys.executable,
                                 repo_root=evaluation.REPO_ROOT,
                                 pg_controldata=job.pgbin.tool('pg_controldata'), output=output,
                                 stats_path=job.run_dir / 'tool_stats.json', log_path=log,
                                 timeout=job.tool_timeout)
    detail = result.get('detail') or ''
    record = {'name': 'pgdelta', 'command': result['command'],
              'returncode': result.get('returncode'),
              'timed_out': detail.startswith('TimeoutExpired'), 'seconds': result.get('seconds'),
              'stdout_tail': [], 'stderr_tail': result.get('stderr_tail', []),
              'log': job.relative(log) if log.is_file() else None, 'detail': detail or None}
    output_text = log.read_text() if log.is_file() else detail
    return ToolRun([record], record['returncode'], record['timed_out'], output_text,
                   record['seconds'], {'basis_unchanged': result.get('basis_unchanged'),
                                       'stats_summary': result.get('stats_summary')})


def lsn_skip_copy(source, target, threshold):
    """Refresh ``target`` from a stopped ``source`` the way EXP-03's shortcut would: in a
    relation file both hold, a source page whose LSN is below ``threshold`` is skipped
    (the target keeps its own); every other page and every other file is copied, and
    target files the source lacks are removed. CONTROL_KEEP files are left alone."""
    source, target = Path(source), Path(target)
    counts = dict.fromkeys(('pages_copied', 'pages_skipped', 'files_copied', 'files_removed'), 0)
    seen = set()
    for path in sorted(source.rglob('*')):
        rel = path.relative_to(source).as_posix()
        if path.name in CONTROL_KEEP and path.parent == source:
            continue
        seen.add(rel)
        dest = target / rel
        if path.is_dir():
            dest.mkdir(exist_ok=True)
            continue
        if relation_kind(rel) is None or not dest.is_file():
            shutil.copy2(path, dest)
            counts['files_copied'] += 1
            continue
        old_blocks = dest.stat().st_size // BLOCK
        with path.open('rb') as new, dest.open('r+b') as out:
            block = 0
            while page := new.read(BLOCK):
                lsn = page_lsn(page)
                if len(page) == BLOCK and block < old_blocks and lsn is not None and lsn < threshold:
                    counts['pages_skipped'] += 1
                else:
                    out.seek(block * BLOCK)
                    out.write(page)
                    counts['pages_copied'] += 1
                block += 1
            out.truncate(path.stat().st_size)
    for path in sorted(target.rglob('*'), reverse=True):
        rel = path.relative_to(target).as_posix()
        if rel in seen or (path.name in CONTROL_KEEP and path.parent == target):
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        counts['files_removed'] += 1
    return counts


def run_lsn_skip(job):
    """Negative control, not an incumbent: EXP-03's LSN-skip shortcut (threshold: B's
    REDO location, or its backup_label start) from the stopped source onto the copy of B.
    It keeps B's exposed pages, so the checks must call it wrong on S2, S2b and S2c."""
    threshold = job.meta['threshold']['lsn']
    started = time.monotonic()
    job.source.stop('fast')
    try:
        counts = lsn_skip_copy(job.source.datadir, job.target, parse_lsn(threshold))
    finally:
        job.source.start()
    seconds = round(time.monotonic() - started, 2)
    record = {'name': 'lsn_skip', 'command': ['lsn_skip_copy', str(job.source.datadir),
                                              str(job.target), threshold],
              'returncode': 0, 'timed_out': False, 'seconds': seconds,
              'stdout_tail': [json.dumps(counts)], 'stderr_tail': [], 'log': None}
    return ToolRun([record], 0, False, '', seconds, {'threshold': threshold, **counts})


ADAPTERS = {'pg_rewind': run_pg_rewind, 'pg_probackup': run_pg_probackup, 'walg': run_walg,
            'walg_send': run_walg_send, 'pgdelta': run_pgdelta, 'lsn_skip': run_lsn_skip}


# --------------------------------------------------------------------------
# Bring-up
# --------------------------------------------------------------------------
def log_match(path, patterns):
    """The first line of a server log matching one of ``patterns``, or None."""
    try:
        text = Path(path).read_text(errors='replace')
    except OSError:
        return None
    return checks.refusal_message(text, patterns)


def last_setting(conf_text, name):
    """The value of the last ``name = '...'`` line of a configuration file, or None."""
    value = None
    for line in conf_text.splitlines():
        match = re.match(rf"\s*{name}\s*=\s*'((?:[^']|'')*)'", line)
        if match:
            value = match.group(1).replace("''", "'")
    return value


def ensure_standby_config(copy, source):
    """Make ``copy`` a standby of ``source``. Returns who wrote the configuration: 'tool'
    when standby.signal exists and primary_conninfo already names the source's socket."""
    auto = Path(copy, 'postgresql.auto.conf')
    conninfo = last_setting(auto.read_text() if auto.is_file() else '', 'primary_conninfo')
    if (Path(copy, 'standby.signal').is_file() and conninfo
            and str(source.sockdir) in conninfo and f'port={source.port}' in conninfo):
        return 'tool'
    Path(copy, 'standby.signal').touch()
    with auto.open('a') as handle:
        quoted = source_conninfo(source).replace("'", "''")
        handle.write(f"\n# added by experiments/incumbents (T11)\nprimary_conninfo = '{quoted}'\n")
    return 'harness'


def run_amcheck(cluster, timeout=3600):
    cmd = [cluster.pgbin.tool('pg_amcheck'), '--all', '--heapallindexed', '--install-missing',
           '--no-password', '-h', cluster.sockdir, '-p', cluster.port, '-U', SUPERUSER,
           '--maintenance-db', 'postgres']
    try:
        proc = run(cmd, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'ok': False, 'returncode': None, 'command': [str(c) for c in cmd],
                'output_tail': ['timed out']}
    return {'ok': proc.returncode == 0, 'returncode': proc.returncode,
            'command': [str(c) for c in cmd],
            'output_tail': checks.tail(proc.stdout + proc.stderr)}


def wait_caught_up(job, cluster, target_lsn):
    """(caught up, reason, last replay LSN): poll until the copy, still in recovery, has
    replayed up to ``target_lsn``; give up early on a hopeless server log line."""
    started = time.monotonic()
    last = None
    while True:
        hopeless = log_match(cluster.logfile, HOPELESS)
        if hopeless:
            return False, f'server log: {hopeless}', last
        try:
            in_recovery, last, reached = cluster.query(
                "SELECT pg_is_in_recovery(), pg_last_wal_replay_lsn(), "
                f"pg_last_wal_replay_lsn() >= '{target_lsn}'::pg_lsn")[0]
            if in_recovery == 'f':
                return False, 'the copy left recovery before reaching the source', last
            if reached == 't':
                return True, None, last
        except (CommandError, ValueError):
            if time.monotonic() - started > 2 and not cluster.is_running():
                return False, 'the server exited', last
        if time.monotonic() - started > job.catchup_timeout:
            return False, f'replay did not reach {target_lsn} in {job.catchup_timeout} s', last
        time.sleep(0.25)


def bring_up_standby(job, copy):
    """Start ``copy`` as a standby of the source, compare it once it has caught up, then
    promote it, run pg_amcheck, CHECKPOINT and stop it cleanly. Returns (bring_up,
    logical, amcheck); bring_up['stopped_cleanly'] says whether the copy's pages can now
    be counted after recovery."""
    info = {'mode': 'standby of the source', 'recovery_conf': None, 'started': False,
            'target_lsn': None, 'replay_lsn': None, 'caught_up': False, 'promoted': False,
            'checkpointed': False, 'stopped_cleanly': False, 'seconds': None, 'error': None}
    logical, amcheck = None, None
    started = time.monotonic()
    cluster = job.new_server('copy', copy)
    try:
        info['recovery_conf'] = ensure_standby_config(copy, job.source)
        info['target_lsn'] = job.source.scalar('SELECT pg_current_wal_lsn()')
        run([job.pgbin.tool('pg_ctl'), '-D', copy, '-l', cluster.logfile, '-W',
             '-o', cluster.server_options(), 'start'])
        cluster.running = info['started'] = True
        caught_up, reason, info['replay_lsn'] = wait_caught_up(job, cluster, info['target_lsn'])
        info['caught_up'] = caught_up
        if not caught_up:
            info['error'] = reason
            info['log_tail'] = checks.tail(log_text(cluster.logfile))
            return info, None, None
        try:
            actual = compute_logical_state(cluster.query)
            differences = compare_logical_states(job.expected, actual)
        except CommandError as exc:  # a copy that can't be read doesn't hold the source's data
            differences = [f'reading the copy failed: {exc}']
            info['log_tail'] = checks.tail(log_text(cluster.logfile))
        logical = {'match': not differences, 'differences': differences[:50],
                   'read_as': 'hot standby'}
        cluster.promote()
        cluster.wait_for('SELECT NOT pg_is_in_recovery()', timeout=120)
        info['promoted'] = True
        amcheck = run_amcheck(cluster)
        cluster.psql('CHECKPOINT;')
        info['checkpointed'] = True
        cluster.stop('fast')
        info['stopped_cleanly'] = True
    except (CommandError, TimeoutError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        info['error'] = f'{type(exc).__name__}: {exc}'
        info['log_tail'] = checks.tail(log_text(cluster.logfile))
    finally:
        cluster.ensure_stopped()
        info['seconds'] = round(time.monotonic() - started, 2)
    return info, logical, amcheck


def log_text(path):
    try:
        return Path(path).read_text(errors='replace')
    except OSError:
        return ''


def count_after_recovery(job, copy, when):
    """B's listed post-fork pages compared with ``copy`` once it has reached consistency
    and stopped cleanly, by tuple data (checks.same_tuple_data)."""
    counted = checks.compare_listed_pages(copy, job.old_copy, job.source_backup,
                                          job.listed_pages, same=checks.same_tuple_data)
    return {**counted, 'counted_after': when, 'compared_by': 'tuple data (checks.tuple_data)'}


def bring_up_restore(job, output):
    """pgdelta's O, checked as EXP-03 does (byte comparison with S, pg_verifybackup, a
    restore to the end of recovery and the logical comparison), plus pg_amcheck, then a
    CHECKPOINT, a clean stop and the count of B's post-fork pages after recovery.
    Returns (bring_up, logical, amcheck, pgdelta_checks, diverged_after_recovery)."""
    extra = {'byte_comparison': evaluation.compare_trees(job.source_backup, output)}
    extra['byte_identical'] = extra['byte_comparison']['identical']
    verify = evaluation.verify_backup(output, job.pgbin)
    extra['verifybackup'], extra['verifybackup_detail'] = verify['ok'], verify
    started = time.monotonic()

    def after_recovery(cluster):
        found = {'amcheck': run_amcheck(cluster)}
        try:
            cluster.psql('CHECKPOINT;')
            found['checkpointed'] = True
            cluster.stop('fast')
            found['stopped_cleanly'] = True
            if job.listed_pages is not None:
                found['diverged_after_recovery'] = count_after_recovery(
                    job, cluster.datadir, 'end of recovery, pg_amcheck, CHECKPOINT and a clean '
                                          'stop of the restored O')
        except (CommandError, TimeoutError, OSError, RuntimeError,
                subprocess.TimeoutExpired) as exc:
            found['after_recovery_error'] = f'{type(exc).__name__}: {exc}'
        return found

    restore = evaluation.restore_and_compare(output, job.expected, job.pgbin, job.work,
                                             while_running=after_recovery)
    info = {'mode': 'restore of O (EXP-03)', 'recovery_conf': None,
            'started': restore['restore_ok'], 'caught_up': restore['restore_ok'],
            'promoted': not restore.get('in_recovery', True),
            'checkpointed': restore.get('checkpointed', False),
            'stopped_cleanly': restore.get('stopped_cleanly', False),
            'seconds': round(time.monotonic() - started, 2), 'error': restore.get('error')}
    if restore.get('after_recovery_error'):
        info['after_recovery_error'] = restore['after_recovery_error']
    if restore.get('log_tail'):
        info['log_tail'] = restore['log_tail']
    logical = None
    if restore['restore_ok'] and 'differences' in restore:
        logical = {'match': restore['logical_match'], 'differences': restore['differences'],
                   'read_as': 'restored primary'}
    return info, logical, restore.get('amcheck'), extra, restore.get('diverged_after_recovery')


def run_job(job):
    """Run one tool on one scenario and return the result.json document."""
    result = {'scenario': job.scenario_id, 'tool': job.tool,
              'scenario_dir': str(job.scenario_dir), 'rows': job.meta.get('rows'),
              'server_conf': job.meta.get('server_conf'),
              'tool_info': job.tool_info, 'started_at': now(), 'commands': [],
              'arm': checks.arm_of(checks.build_conf(job.meta)),
              'returncode': None, 'seconds': None, 'target_modified': None,
              'outcome': None, 'outcome_reason': None, 'refusal': None,
              'refusal_class': None, 'refusal_class_evidence': None, 'parsed': {},
              'diverged_before_recovery': None, 'diverged_after_recovery': None,
              'bring_up': None, 'logical_match': None, 'logical_differences': [],
              'amcheck': None, 'amcheck_ok': None, 'verdict': None, 'notes': []}
    if not job.tool_info.get('available'):
        result.update(outcome='skipped', outcome_reason=job.tool_info.get('reason'),
                      verdict='skipped', finished_at=now())
        return result
    job.logs.mkdir(parents=True, exist_ok=True)
    job.work.mkdir(parents=True, exist_ok=True)
    try:
        if job.tool != 'pgdelta':
            job.say('copy and start the kept source cluster; copy B')
            job.start_source()
            job.copy_old_copy()
            result['source'] = {'kept_datadir': job.meta['source_cluster']['datadir'],
                                'lsn_before_tool': job.source.scalar(
                                    'SELECT pg_current_wal_lsn()')}
            before = evaluation.tree_fingerprint(job.target)
        job.say('run the tool')
        tool = ADAPTERS[job.tool](job)
        result.update(commands=tool.commands, returncode=tool.returncode, seconds=tool.seconds,
                      parsed=tool.parsed, timed_out=tool.timed_out, target=str(job.target))
        result['notes'].extend(tool.notes)
        if job.tool == 'pgdelta':  # pgdelta writes O; this says whether it touched B
            result['target_modified'] = tool.parsed.get('basis_unchanged') is False
        else:
            result['target_modified'] = evaluation.tree_fingerprint(job.target) != before
        if tool.harness_error:
            result.update(outcome='harness_error', outcome_reason=tool.harness_error,
                          harness_error=tool.harness_error)
        else:
            outcome, reason, refusal = checks.classify_outcome(
                tool.returncode, result['target_modified'], tool.output,
                checks.REFUSAL_PATTERNS.get(job.tool, ()), tool.timed_out,
                checks.REFUSAL_EXIT_CODES.get(job.tool))
            result.update(outcome=outcome, outcome_reason=reason, refusal=refusal)
            result['refusal_class'], result['refusal_class_evidence'] = checks.refusal_class(
                outcome, tool.output, checks.IDENTITY_OR_HISTORY_PATTERNS.get(job.tool, ()))
        job.say(f"{result['outcome']}: {result['outcome_reason']}"
                + (f" ({result['refusal_class']})" if result['refusal_class'] else ''))
        if result['outcome'] == 'accepted':
            check_accepted(job, result)
    except Exception as exc:  # noqa: BLE001 - recorded in result.json
        result['harness_error'] = f'{type(exc).__name__}: {exc}'
        result['traceback'] = traceback.format_exc()
        if result['outcome'] is None:
            result.update(outcome='harness_error', outcome_reason=result['harness_error'])
        job.say(f'HARNESS ERROR: {result["harness_error"]}')
    finally:
        job.close()
    result['verdict'] = checks.verdict(
        result['outcome'], result['logical_match'], result['amcheck_ok'],
        (result['diverged_after_recovery'] or {}).get('diverged_left'),
        (result['bring_up'] or {}).get('caught_up'), job.listed_pages is not None)
    if result.get('harness_error') and result['verdict'] in ('correct', 'not_verified', None):
        result['verdict'] = 'not_verified'
    result['finished_at'] = now()
    return result


def check_accepted(job, result):
    """Count stale pages in the refreshed copy, bring it up and check it, then count the
    stale pages again once it has reached consistency and stopped cleanly."""
    listed = job.listed_pages is not None
    if listed:
        job.say(f'compare the {len(job.listed_pages)} post-fork pages of B with the copy')
        result['diverged_before_recovery'] = checks.compare_listed_pages(
            job.target, job.old_copy, job.source_backup, job.listed_pages)
    if job.tool == 'pgdelta':
        job.say('check O like EXP-03, restore it, compare, pg_amcheck, CHECKPOINT, stop')
        bring_up, logical, amcheck, extra, after = bring_up_restore(job, job.target)
        result['pgdelta_checks'] = extra
    else:
        job.say('start the copy as a standby of the source, compare, promote, pg_amcheck, '
                'CHECKPOINT, stop')
        bring_up, logical, amcheck = bring_up_standby(job, job.target)
        after = None
        if listed and bring_up.get('stopped_cleanly'):
            after = count_after_recovery(job, job.target, 'replay up to the source, '
                                         'promotion, pg_amcheck, CHECKPOINT and a clean stop')
    if listed and after is None and not bring_up.get('after_recovery_error'):
        bring_up['after_recovery_error'] = ('the copy never stopped cleanly after reaching '
                                            "consistency, so B's post-fork pages were not "
                                            'counted after recovery')
    result['bring_up'] = bring_up
    result['diverged_after_recovery'] = after
    if logical is not None:
        result['logical_match'] = logical['match']
        result['logical_differences'] = logical['differences']
        result['logical_read_as'] = logical['read_as']
    if amcheck is not None:
        result['amcheck'], result['amcheck_ok'] = amcheck, amcheck['ok']
    counts = ''
    if listed:
        before = result['diverged_before_recovery']['diverged_left']
        counts = (f", B's diverged pages left {before} before recovery, "
                  f"{None if after is None else after['diverged_left']} after")
    job.say(f"caught up {bring_up.get('caught_up')}, logical {result['logical_match']}, "
            f"amcheck {result['amcheck_ok']}{counts}"
            + (f", error {bring_up['error']}" if bring_up.get('error') else ''))


def write_result(run_dir, result):
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    (Path(run_dir) / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
