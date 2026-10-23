"""Pure helpers for EXP-04: counters, speeds, digests and the CSV summary.

Nothing here runs a command. The agent (agent.py) and the orchestrator
(run_exp04.py) read /proc and the filesystem through these functions, and the
tests exercise them on fixed text.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import stat

NETDEV_FIELDS = ('rx_bytes', 'rx_packets', 'rx_errs', 'rx_drop', 'rx_fifo', 'rx_frame',
                 'rx_compressed', 'rx_multicast', 'tx_bytes', 'tx_packets', 'tx_errs',
                 'tx_drop', 'tx_fifo', 'tx_colls', 'tx_carrier', 'tx_compressed')
CPU_FIELDS = ('user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq', 'steal',
              'guest', 'guest_nice')
# guest and guest_nice are already included in user and nice (proc(5)).
CPU_BUSY_FIELDS = ('user', 'nice', 'system', 'irq', 'softirq', 'steal')
UNTHROTTLED = 'unthrottled'
# The default sweep.
METHODS = ('full', 'incremental', 'pgdelta', 'rsync_8k', 'rsync_default')
# Variants run only when named in --methods.
OPTIONAL_METHODS = ('pgdelta_1thread',)
ALL_METHODS = METHODS + OPTIONAL_METHODS
PGDELTA_METHODS = ('pgdelta', 'pgdelta_1thread')
RSYNC_METHODS = ('rsync_8k', 'rsync_default')
STAGED_METHODS = PGDELTA_METHODS + RSYNC_METHODS
# The engine each pgdelta method runs, named by its flags, so that no change of pgdelta's
# defaults can change what a method measures (as compare_methods pins EXP-02's):
# - pgdelta: EXP-06's choice, which is also the command line's default on these 8-vCPU
#   VMs: 4 worker processes on each end, copy-then-patch, pipelined verification.
# - pgdelta_1thread: the single-threaded engine EXP-02 measured and EXP-04's smoke runs ran
#   (EXP-06's baseline): in-process hashing, gap-fill, sequential verification.
# --jobs goes to both ends; --rebuild and --verify only to the receiver, which builds O.
# prefetch_basis is the receiver's --prefetch-basis: reading B0 into the page cache while it
# waits for the sender (decided October 22, 2026; see EXP-04's decision note on pgdelta's
# receiver). It is on for both methods: it is a receiver feature that doesn't depend on how
# either end hashes, and with it off pgdelta_1thread would read B0 cold where pgdelta doesn't.
# The orchestrator checks that both VMs' pgdelta has these options before a run starts.
PGDELTA_ENGINES = {
    'pgdelta': {'jobs': 4, 'rebuild': 'copy-patch', 'verify': 'pipelined', 'prefetch_basis': 'on'},
    'pgdelta_1thread': {'jobs': 1, 'rebuild': 'gap-fill', 'verify': 'sequential', 'prefetch_basis': 'on'},
}


def pgdelta_engine_args(engine: dict) -> dict:
    """{'send': argv, 'receive': argv}: the hashing and rebuilding flags of `engine` on each end."""
    jobs = ('--jobs', str(engine['jobs']))
    return {'send': jobs, 'receive': (*jobs, '--rebuild', engine['rebuild'], '--verify', engine['verify'])}


def pgdelta_args(engine: dict) -> dict:
    """{'send': argv, 'receive': argv}: every flag `engine` pins, the receiver's prefetch included."""
    args = pgdelta_engine_args(engine)
    return {**args, 'receive': (*args['receive'], '--prefetch-basis', engine['prefetch_basis'])}


def pgdelta_switches(engine: dict) -> dict:
    """The `switches` pgdelta's statistics report when `engine` is in effect."""
    backend = 'process' if engine['jobs'] > 1 else 'serial'
    verify = engine['verify'] if engine['jobs'] > 1 else 'sequential'  # no workers: always sequential
    return {'sender': {'jobs': engine['jobs'], 'hash_backend': backend},
            'receiver': {'jobs': engine['jobs'], 'hash_backend': backend, 'rebuild': engine['rebuild'],
                         'verify': verify}}


PGDELTA_ARGS = {method: pgdelta_args(engine) for method, engine in PGDELTA_ENGINES.items()}
# Options each end's `pgdelta <command> --help` must list for PGDELTA_ARGS to work.
PGDELTA_OPTIONS = {command: tuple(arg for arg in args if arg.startswith('--'))
                   for command, args in PGDELTA_ARGS['pgdelta'].items()}
# What the per-process CPU and peak-RSS columns cover. Every method's capture
# is served by a walsender, a PostgreSQL server process on the source that no
# per-process figure sees, and the methods launch different clients, so these
# columns are not comparable across methods; the VM-wide CPU columns are.
# Peak RSS is GNU time's %M: the peak of the largest single process in each
# tool's tree (getrusage(2) gives the largest child's ru_maxrss, not a sum), so
# tools that run several processes at once are reported by their largest one.
PEAK_RSS_SCOPE = ("GNU time's %M: the peak RSS of the largest single process in each tool's "
                  "process tree, not the tree's combined footprint. pg_basebackup -X stream "
                  "forks a WAL-streaming child that runs beside its main process, and the "
                  "rsync daemon forks a per-connection generator that forks the receiver; "
                  "those concurrent processes are not summed. pgdelta at --jobs 4 is not one "
                  "process: each end starts 4 hashing worker processes with spawn, as its own "
                  "children, beside its main process, and only the largest of the five is "
                  "reported. pgdelta_1thread (--jobs 1, no workers), pg_combinebackup, cp, sync "
                  "and the sending rsync client are single processes.")
PROC_SCOPE = {
    'full': 'dest: pg_basebackup; source: none (its walsender is not per-process). Peak RSS: '
            "largest single process (pg_basebackup's WAL-streaming child not added)",
    'incremental': 'dest: pg_basebackup, pg_combinebackup; source: none (walsender not '
                   "per-process). Peak RSS: largest single process (pg_basebackup's "
                   'WAL-streaming child not added)',
    'pgdelta': 'source: local pg_basebackup client, pgdelta send (capture walsender not per-process); '
               'dest: pgdelta receive. At --jobs 4 pgdelta is not one process on either end: each starts '
               '4 worker processes with spawn, as its own children, which it reaps when its pool closes, so CPU '
               "(wait4's rusage, and GNU time's user and system time) includes them. Peak RSS: GNU "
               "time's %M, the largest single process (pgdelta and its 4 workers not summed; "
               "pg_basebackup's WAL-streaming child not added)",
    'rsync_8k': 'source: local pg_basebackup client, rsync (capture walsender not '
                'per-process); dest: cp, sync, rsync daemon with its per-connection '
                'generator and receiver. Peak RSS: largest single process (the daemon, '
                "generator and receiver are not summed, nor pg_basebackup's WAL streamer)",
}
PROC_SCOPE['pgdelta_1thread'] = ('source: local pg_basebackup client, pgdelta send (capture walsender '
                                  'not per-process); dest: pgdelta receive. --jobs 1 starts no worker '
                                  'processes, so each pgdelta is one process. Peak RSS: largest single '
                                  "process (pg_basebackup's WAL-streaming child not added)")
PROC_SCOPE['rsync_default'] = PROC_SCOPE['rsync_8k']
# rsync's copy of B0 (prep, on dest) and its capture of S (on source) touch
# different machines and the link is idle during both, so they run at the same
# time in one timed phase, as a real cp-then-rsync refresh would.
CONCURRENT_PHASE = 'prep_capture'
# pgdelta's counterpart: its receiver starts on dest, and prefetches B0 while it
# waits for the sender, at the same time as the capture of S on source.
RECEIVER_PHASE = 'receiver_capture'
SPEED_ORDERS = ('shuffle', 'alternate', 'ascending')
DIGEST_CHUNK = 1 << 20
DEFAULT_WAL_SEGMENT = 16 << 20
WAL_SEGMENT_NAME = re.compile(r'^[0-9A-F]{24}$')
# GNU time (Debian package `time`) reports the peak RSS of the command it runs.
# A child's own ru_maxrss is useless here: Linux carries the parent's high-water
# mark into a forked child, so a tool launched from the Python orchestrator or
# agent would report the launcher's memory. GNU time is a small C program, so
# the floor it passes on is about 1 MB (measured on pgd-dest from a 420 MB
# parent: /bin/true 1144 KiB under GNU time, 420644 KiB from wait4 directly).
GNU_TIME_PREFIX = 'EXP04-TIME'
GNU_TIME_FORMAT = (GNU_TIME_PREFIX + ' maxrss_kb=%M wall_seconds=%e user_seconds=%U '
                   'system_seconds=%S exit=%x')


# --- /proc parsing ----------------------------------------------------------

def parse_netdev(text: str) -> dict[str, dict[str, int]]:
    """Interface -> counters from /proc/net/dev (two header lines, then one per interface)."""
    interfaces = {}
    for line in text.splitlines():
        name, colon, rest = line.partition(':')
        if not colon or '|' in line:
            continue
        values = rest.split()
        if len(values) < len(NETDEV_FIELDS):
            raise ValueError(f'short /proc/net/dev line: {line!r}')
        interfaces[name.strip()] = {field: int(value)
                                    for field, value in zip(NETDEV_FIELDS, values)}
    if not interfaces:
        raise ValueError('no interfaces in /proc/net/dev text')
    return interfaces


def parse_proc_stat(text: str) -> dict[str, int]:
    """Aggregate CPU ticks (USER_HZ units) from the first 'cpu ' line of /proc/stat."""
    for line in text.splitlines():
        if line.startswith('cpu '):
            values = [int(value) for value in line.split()[1:]]
            # Older kernels print fewer columns; missing ones are zero.
            values += [0] * (len(CPU_FIELDS) - len(values))
            return dict(zip(CPU_FIELDS, values))
    raise ValueError("no aggregate 'cpu ' line in /proc/stat text")


def counter_delta(before: dict, after: dict, fields) -> dict[str, int]:
    delta = {}
    for field in fields:
        change = after[field] - before[field]
        if change < 0:
            raise ValueError(f'counter {field} went backwards ({before[field]} -> {after[field]})')
        delta[field] = change
    return delta


def snapshot_delta(before: dict, after: dict) -> dict:
    """Net bytes and packets and CPU seconds between two snapshot() results of one host."""
    if before['iface'] != after['iface']:
        raise ValueError('snapshots are of different interfaces')
    net = counter_delta(before['net'], after['net'],
                        ('rx_bytes', 'tx_bytes', 'rx_packets', 'tx_packets'))
    ticks = counter_delta(before['cpu_ticks'], after['cpu_ticks'], CPU_FIELDS)
    hz = after['clk_tck']
    cpu = {f'{field}_seconds': round(value / hz, 3) for field, value in ticks.items()}
    cpu['busy_seconds'] = round(sum(ticks[field] for field in CPU_BUSY_FIELDS) / hz, 3)
    return {'net': net, 'cpu': cpu,
            'interval_seconds': round(after['monotonic'] - before['monotonic'], 6)}


def snapshot(iface: str, proc: Path = Path('/proc')) -> dict:
    """Counters of one interface and the aggregate CPU line, with clocks."""
    import time
    netdev = parse_netdev((proc / 'net' / 'dev').read_text())
    if iface not in netdev:
        raise ValueError(f'interface {iface} not in /proc/net/dev ({", ".join(netdev)})')
    return {'iface': iface, 'net': netdev[iface],
            'cpu_ticks': parse_proc_stat((proc / 'stat').read_text()),
            'clk_tck': os.sysconf('SC_CLK_TCK'), 'monotonic': time.monotonic(),
            'utc': time.time()}


# --- link speeds ------------------------------------------------------------

def parse_speed(text: str) -> dict:
    """'0.5' (Gbit/s) or 'unthrottled' -> label, target and tc rate."""
    text = text.strip().lower()
    if text in (UNTHROTTLED, 'off', 'none'):
        return {'label': UNTHROTTLED, 'gbit': None, 'tc_rate': 'off'}
    gbit = float(text.rstrip('g'))
    if not math.isfinite(gbit) or gbit <= 0:
        raise ValueError(f'link speed must be a positive Gbit/s value, got {text!r}')
    mbit = round(gbit * 1000)
    if mbit < 1 or abs(mbit - gbit * 1000) > 1e-6:
        raise ValueError(f'link speed {text} Gbit/s is not a whole number of Mbit/s')
    return {'label': f'{gbit:g}g', 'gbit': gbit, 'tc_rate': f'{mbit}mbit'}


def parse_speeds(text: str) -> list[dict]:
    speeds = [parse_speed(item) for item in text.split(',') if item.strip()]
    labels = [speed['label'] for speed in speeds]
    if len(set(labels)) != len(labels):
        raise ValueError(f'duplicate link speeds in {text!r}')
    if not speeds:
        raise ValueError('no link speeds given')
    return speeds


def gbit_per_second(byte_count: int, seconds: float) -> float | None:
    return round(byte_count * 8 / seconds / 1e9, 4) if seconds > 0 else None


def method_orders(methods, rng: random.Random, count: int) -> list[list[str]]:
    """count independent random orders of methods, drawn from rng in sequence."""
    orders = []
    for _ in range(count):
        order = list(methods)
        rng.shuffle(order)
        orders.append(order)
    return orders


def order_speeds(speeds, rng: random.Random, mode: str, repeat: int) -> list:
    """The speeds in the order one repeat visits them.

    'shuffle' (the default) draws a new order from rng for every repeat, so
    drift over a long sweep (cache or disk state, noisy neighbours, the
    source's growing WAL) is not confounded with link speed; 'alternate' runs
    the given order on even repeats and its reverse on odd ones; 'ascending'
    keeps the given order.
    """
    speeds = list(speeds)
    if mode == 'shuffle':
        rng.shuffle(speeds)
        return speeds
    if mode == 'alternate':
        return speeds if repeat % 2 == 0 else speeds[::-1]
    if mode == 'ascending':
        return speeds
    raise ValueError(f'unknown speed order {mode!r}')


# --- per-process peak memory (GNU time) ---------------------------------------

def gnu_time_argv(gnu_time, output, argv) -> list[str]:
    """argv run under GNU time, which writes GNU_TIME_FORMAT to output when it ends."""
    return [str(gnu_time), '-f', GNU_TIME_FORMAT, '-o', str(output), *map(str, argv)]


def parse_gnu_time(text: str) -> dict | None:
    """The GNU_TIME_FORMAT line of a GNU time -o file, or None if it has none.

    GNU time writes 'Command exited with non-zero status N' (or 'terminated
    by signal') before the format line; only the format line is read.
    """
    for line in reversed(text.splitlines()):
        if not line.startswith(GNU_TIME_PREFIX + ' '):
            continue
        fields = dict(item.partition('=')[::2] for item in line.split()[1:])
        try:
            return {'maxrss_kb': int(fields['maxrss_kb']),
                    'wall_seconds': float(fields['wall_seconds']),
                    'user_seconds': float(fields['user_seconds']),
                    'system_seconds': float(fields['system_seconds']),
                    'exit_code': int(fields['exit'])}
        except (KeyError, ValueError):
            return None
    return None


def read_gnu_time(path) -> dict | None:
    try:
        return parse_gnu_time(Path(path).read_text(errors='replace'))
    except OSError:
        return None


# --- trees ------------------------------------------------------------------

def tree_digest(root: Path) -> dict:
    """Every entry below root: path -> [kind, size, sha256 or link target, mode, ino, nlink].

    Symlinks are not followed. 'dir' entries carry no size or hash.
    """
    root = Path(root)
    entries = {}
    total = 0
    for directory, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames) + sorted(filenames):
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            info = os.lstat(path)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                entries[rel] = ['link', 0, os.readlink(path), mode, info.st_ino, info.st_nlink]
            elif stat.S_ISDIR(info.st_mode):
                entries[rel] = ['dir', 0, None, mode, info.st_ino, info.st_nlink]
            elif stat.S_ISREG(info.st_mode):
                digest = hashlib.sha256()
                with open(path, 'rb') as stream:
                    while chunk := stream.read(DIGEST_CHUNK):
                        digest.update(chunk)
                entries[rel] = ['file', info.st_size, digest.hexdigest(), mode, info.st_ino,
                                info.st_nlink]
                total += info.st_size
            else:
                entries[rel] = ['other', 0, None, mode, info.st_ino, info.st_nlink]
    top = os.lstat(root)
    return {'root': str(root), 'device': top.st_dev, 'entries': entries, 'files': sum(
        1 for entry in entries.values() if entry[0] == 'file'), 'bytes': total}


def tree_stat(root: Path) -> dict[str, list]:
    """A cheap fingerprint of a tree without reading it: path -> [kind, size, mtime_ns, mode].

    The orchestrator compares B0's before and after every method run, and
    hashes it fully (tree_digest) at the start and end of each trial.
    """
    root = Path(root)
    entries = {}
    for directory, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(directory) / name
            info = os.lstat(path)
            kind = ('link' if stat.S_ISLNK(info.st_mode) else 'dir' if stat.S_ISDIR(info.st_mode)
                    else 'file' if stat.S_ISREG(info.st_mode) else 'other')
            entries[path.relative_to(root).as_posix()] = [
                kind, info.st_size if kind == 'file' else 0, info.st_mtime_ns,
                stat.S_IMODE(info.st_mode)]
    return entries


def stat_changes(before: dict, after: dict, limit: int = 20) -> list[str]:
    changes = []
    for path in sorted(before.keys() | after.keys()):
        if before.get(path) != after.get(path):
            changes.append(f'{path}: {before.get(path)} -> {after.get(path)}')
            if len(changes) >= limit:
                break
    return changes


def compare_digests(expected: dict, actual: dict, limit: int = 20) -> tuple[bool, list[str]]:
    """Same paths, kinds, sizes and contents (modes, inodes and link counts ignored)."""
    left, right = expected['entries'], actual['entries']
    differences = []
    for path in sorted(left.keys() | right.keys()):
        if path not in right:
            differences.append(f'missing in output: {path}')
        elif path not in left:
            differences.append(f'extra in output: {path}')
        elif left[path][:3] != right[path][:3]:
            differences.append(f'differs: {path} {left[path][:2]} vs {right[path][:2]}')
        if len(differences) >= limit:
            break
    return not differences, differences


def independence(output: dict, basis: dict, limit: int = 20) -> tuple[bool, list[str]]:
    """O shares no inode with B (same device) and none of its files has a second link."""
    problems = []
    basis_inodes = ({entry[4] for entry in basis['entries'].values()}
                    if basis['device'] == output['device'] else set())
    for path, entry in sorted(output['entries'].items()):
        if entry[4] in basis_inodes:
            problems.append(f'shares an inode with the basis: {path}')
        elif entry[0] == 'file' and entry[5] > 1:
            problems.append(f'has {entry[5]} hard links: {path}')
        if len(problems) >= limit:
            break
    return not problems, problems


# --- WAL in a backup ----------------------------------------------------------

def parse_lsn(text: str) -> int:
    high, _, low = text.partition('/')
    return (int(high, 16) << 32) | int(low, 16)


def wal_files(root: Path) -> dict[str, int]:
    """Relative path -> size of every regular file under root/pg_wal."""
    root = Path(root)
    files = {}
    for directory, _, filenames in os.walk(root / 'pg_wal'):
        for name in filenames:
            path = Path(directory) / name
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode):
                files[path.relative_to(root).as_posix()] = info.st_size
    return files


def wal_files_from_digest(digest: dict) -> dict[str, int]:
    return {path: entry[1] for path, entry in digest['entries'].items()
            if entry[0] == 'file' and path.startswith('pg_wal/')}


def manifest_wal_ranges(text: str) -> list[dict]:
    """The WAL-Ranges of a backup_manifest."""
    return [{'timeline': item['Timeline'], 'start_lsn': item['Start-LSN'],
             'end_lsn': item['End-LSN']} for item in json.loads(text).get('WAL-Ranges', [])]


def wal_summary(files: dict[str, int], manifest_text: str | None) -> dict:
    """The WAL a backup holds, and how much of it is real.

    A plain backup's pg_wal holds whole segment files (16 MiB), zero-padded
    past the backup's end LSN. pg_basebackup -X stream sends only the WAL from
    the start of the first segment to the end LSN (wal_stream_bytes, from the
    manifest's WAL-Ranges); pgdelta and rsync send S's new segment files whole,
    so a staged run moves about wal_padding_bytes more than a streaming copy.
    """
    segments = {path: size for path, size in files.items()
                if WAL_SEGMENT_NAME.match(path.rsplit('/', 1)[-1])}
    segment_bytes = max(segments.values()) if segments else DEFAULT_WAL_SEGMENT
    ranges = manifest_wal_ranges(manifest_text) if manifest_text else []
    stream = None
    if ranges:
        stream = 0
        for item in ranges:
            start = parse_lsn(item['start_lsn'])
            stream += parse_lsn(item['end_lsn']) - (start - start % segment_bytes)
    wal_bytes = sum(files.values())
    return {'wal_bytes': wal_bytes, 'wal_files': len(files), 'wal_segments': len(segments),
            'wal_segment_bytes': segment_bytes, 'wal_ranges': ranges,
            'wal_stream_bytes': stream,
            'wal_padding_bytes': None if stream is None else max(0, wal_bytes - stream)}


def digest_summary(digest: dict) -> dict:
    """A short, comparable form of a tree digest for the result records."""
    listing = hashlib.sha256()
    for path, entry in sorted(digest['entries'].items()):
        listing.update(f'{path}\0{entry[0]}\0{entry[1]}\0{entry[2]}\n'.encode())
    return {'files': digest['files'], 'bytes': digest['bytes'],
            'entries': len(digest['entries']), 'listing_sha256': listing.hexdigest()}


# --- results ----------------------------------------------------------------

CSV_FIELDS = (
    'run_name', 'trial_id', 'pattern', 'fraction', 'scale', 'rows_changed', 'repeat',
    'speed', 'speed_index', 'target_gbit', 'measured_s2d_gbit', 'measured_d2s_gbit',
    'method', 'order_index', 'status',
    'total_s', 'total_no_ssh_s', 'inplace_total_s', 'inplace_total_no_ssh_s',
    'serial_total_s', 'serial_total_no_ssh_s',
    'prep_s', 'capture_s', 'prep_capture_s', 'receiver_start_s', 'receiver_capture_s', 'transfer_s',
    'combine_s', 'ssh_overhead_s',
    'prefetch_bytes', 'prefetch_basis_bytes', 'prefetch_s', 'prefetch_wait_s', 'prefetch_stopped',
    'untimed_gap_s', 'prefetch_untimed_s', 'prefetch_untimed_bytes_est', 'hash_basis_s',
    'wire_s2d_bytes', 'wire_d2s_bytes', 'wire_s2d_bytes_dest_rx', 'wire_d2s_bytes_source_rx',
    'tool_s2d_bytes', 'tool_d2s_bytes', 'output_bytes', 'output_minus_wal_padding_bytes',
    'capture_bytes', 'wal_bytes', 'wal_stream_bytes', 'wal_padding_bytes',
    'cpu_source_s', 'cpu_dest_s', 'prep_capture_cpu_dest_s', 'receiver_capture_cpu_dest_s',
    'counter_windows',
    'proc_cpu_source_s', 'proc_cpu_dest_s', 'peak_rss_source_kb', 'peak_rss_dest_kb',
    'proc_scope',
    'verified', 'identical_to_s', 'caches_dropped', 'error',
)


def _get(value, *keys):
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def phase_seconds(record: dict, name: str):
    """Wall seconds of a timed phase, or of a part of a concurrent phase (prep, capture)."""
    phases = record.get('phases') or {}
    if name in phases:
        return _get(phases, name, 'wall_seconds')
    for phase in phases.values():
        if name in (phase.get('parts') or {}):
            return _get(phase, 'parts', name, 'wall_seconds')
    return None


def summary_row(record: dict) -> dict:
    """One CSV row for one (trial, repeat, speed, method) record."""
    totals = record.get('totals') or {}
    in_place = record.get('totals_in_place') or {}
    serial = record.get('totals_serial') or {}
    verification = record.get('verification') or {}
    link = record.get('link') or {}
    speed = record.get('speed') or {}
    wal = record.get('wal') or {}
    prefetch = _get(record, 'tool_reported', 'prefetch') or {}
    untimed = record.get('prefetch_untimed') or {}
    return {
        'run_name': record.get('run_name'),
        'trial_id': record.get('trial_id'),
        'pattern': record.get('pattern'),
        'fraction': record.get('fraction'),
        'scale': record.get('scale'),
        'rows_changed': record.get('rows_changed'),
        'repeat': record.get('repeat'),
        'speed': speed.get('label'),
        'speed_index': record.get('speed_index'),
        'target_gbit': speed.get('gbit'),
        'measured_s2d_gbit': link.get('source_to_dest_gbit'),
        'measured_d2s_gbit': link.get('dest_to_source_gbit'),
        'method': record.get('method'),
        'order_index': record.get('order_index'),
        'status': record.get('status'),
        # End to end: every timed phase, rsync's copy of B0 included. rsync's copy
        # runs on dest while its capture runs on source, so it counts only where it
        # is the longer of the two: max(prep, capture) + transfer. pgdelta's receiver
        # starts beside its capture the same way: max(receiver start, capture) + transfer.
        'total_s': totals.get('wall_seconds'),
        'total_no_ssh_s': totals.get('wall_seconds_less_remote_overhead'),
        # rsync only: without the copy of B0, the lower bound for patching B0 in place.
        'inplace_total_s': in_place.get('wall_seconds'),
        'inplace_total_no_ssh_s': in_place.get('wall_seconds_less_remote_overhead'),
        # rsync and pgdelta: the two parts of the concurrent phase and the transfer one
        # after the other, the upper bound.
        'serial_total_s': serial.get('wall_seconds'),
        'serial_total_no_ssh_s': serial.get('wall_seconds_less_remote_overhead'),
        'prep_s': phase_seconds(record, 'prep'),
        'capture_s': phase_seconds(record, 'capture'),
        'prep_capture_s': phase_seconds(record, CONCURRENT_PHASE),
        # pgdelta only: from launching the receiver until it listened, and the pair's wall.
        'receiver_start_s': phase_seconds(record, 'receiver'),
        'receiver_capture_s': phase_seconds(record, RECEIVER_PHASE),
        'transfer_s': phase_seconds(record, 'transfer'),
        'combine_s': phase_seconds(record, 'combine'),
        'ssh_overhead_s': totals.get('remote_overhead_seconds'),
        # pgdelta only: what the receiver read of B0 into the page cache while it waited
        # for the sender (pgdelta's statistics, receiver.prefetch).
        'prefetch_bytes': prefetch.get('bytes'),
        'prefetch_basis_bytes': prefetch.get('basis_bytes'),
        'prefetch_s': prefetch.get('seconds'),
        'prefetch_wait_s': prefetch.get('waited_seconds'),
        'prefetch_stopped': prefetch.get('stopped'),
        # pgdelta only: the untimed gap between receiver_capture and the transfer (the counters
        # are read there), and how much of the prefetch fell in it: B0 read off the clock
        # (prefetch_untimed). The bytes are an estimate at the prefetch's average rate.
        'untimed_gap_s': untimed.get('gap_seconds'),
        'prefetch_untimed_s': untimed.get('untimed_seconds'),
        'prefetch_untimed_bytes_est': untimed.get('untimed_bytes_estimate'),
        # pgdelta only: the receiver hashing B0 for the sender (its own timer, inside the transfer).
        'hash_basis_s': _get(record, 'tool_reported', 'timings_seconds', 'hash_basis'),
        'wire_s2d_bytes': totals.get('wire_bytes_source_to_dest'),
        'wire_d2s_bytes': totals.get('wire_bytes_dest_to_source'),
        'wire_s2d_bytes_dest_rx': totals.get('wire_bytes_source_to_dest_dest_rx'),
        'wire_d2s_bytes_source_rx': totals.get('wire_bytes_dest_to_source_source_rx'),
        'tool_s2d_bytes': _get(record, 'tool_reported', 'bytes_source_to_dest'),
        'tool_d2s_bytes': _get(record, 'tool_reported', 'bytes_dest_to_source'),
        'output_bytes': record.get('output_bytes'),
        'output_minus_wal_padding_bytes': record.get('output_minus_wal_padding_bytes'),
        'capture_bytes': record.get('capture_bytes'),
        'wal_bytes': wal.get('wal_bytes'),
        'wal_stream_bytes': wal.get('wal_stream_bytes'),
        'wal_padding_bytes': wal.get('wal_padding_bytes'),
        'cpu_source_s': totals.get('cpu_seconds_source'),
        'cpu_dest_s': totals.get('cpu_seconds_dest'),
        # rsync only: dest CPU over the prep+capture window. The prep runs only on
        # dest; the capture's share there is its SSH client.
        'prep_capture_cpu_dest_s': _get(record, 'phases', CONCURRENT_PHASE, 'cpu', 'dest',
                                        'busy_seconds'),
        # pgdelta only: dest CPU over the receiver+capture window: the receiver's start-up
        # and prefetch, plus the capture's SSH client.
        'receiver_capture_cpu_dest_s': _get(record, 'phases', RECEIVER_PHASE, 'cpu', 'dest',
                                            'busy_seconds'),
        # Timed phases summed into the totals, each with its own counter window
        # (and its snapshot floor, run.json phase_floor).
        'counter_windows': len(record['phases']) if record.get('phases') else None,
        'proc_cpu_source_s': totals.get('process_cpu_seconds_source'),
        'proc_cpu_dest_s': totals.get('process_cpu_seconds_dest'),
        'peak_rss_source_kb': totals.get('peak_rss_kb_source'),
        'peak_rss_dest_kb': totals.get('peak_rss_kb_dest'),
        'proc_scope': PROC_SCOPE.get(record.get('method')),
        'verified': verification.get('verified'),
        'identical_to_s': verification.get('identical_to_capture'),
        'caches_dropped': record.get('caches_dropped'),
        'error': record.get('error'),
    }


def write_csv(path: Path, records) -> None:
    """Rewrite the summary CSV from all records so far (atomically)."""
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with open(temporary, 'w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction='raise')
        writer.writeheader()
        for record in records:
            writer.writerow(summary_row(record))
    temporary.replace(path)


def command_overhead(commands) -> float:
    """Outer minus inner wall of the remote commands: SSH and the agent's start-up."""
    return sum(command['wall_seconds'] - command['inner_wall_seconds'] for command in commands
               if command.get('inner_wall_seconds') is not None)


def part_commands(phase: dict, name: str) -> list:
    return [command for command in phase.get('commands', ()) if command.get('part') == name]


def phase_overhead(phase: dict) -> float:
    """The remote-launch time on a phase's critical path.

    For a sequential phase that is every remote command's overhead. A
    concurrent phase lasts as long as its longest part, so only what the
    overhead adds beyond the other parts counts: max(part walls) minus
    max(part walls without their overhead).
    """
    parts = phase.get('parts')
    if not parts:
        return command_overhead(phase.get('commands', ()))
    walls = [part['wall_seconds'] for part in parts.values()]
    lean = [part['wall_seconds'] - command_overhead(part_commands(phase, name))
            for name, part in parts.items()]
    return max(0.0, max(walls) - max(lean))


def phase_totals(phases: dict) -> dict:
    """Sum the timed phases: wall, wire bytes per direction and CPU per VM.

    Source-to-destination bytes are the source's transmit counter; the
    destination's receive counter is kept as a cross-check (and the other way
    round for the return direction). A concurrent phase counts once, with the
    wall of its parts together.
    """
    def total(*keys):
        values = [_get(phase, *keys) for phase in phases.values()]
        if not values or any(value is None for value in values):
            return None
        return round(sum(values), 3) if any(isinstance(v, float) for v in values) \
            else sum(values)

    def commands(host):
        return [command for phase in phases.values() for command in phase.get('commands', ())
                if command.get('host') == host]

    def process_cpu(host):
        values = [command['rusage']['cpu_seconds'] for command in commands(host)
                  if command.get('rusage')]
        return round(sum(values), 3) if values else None

    def peak_rss(host):
        # GNU time's figure for each tool; unknown if any command lacks one.
        values = [command.get('peak_rss_kb') for command in commands(host)]
        if not values or any(value is None for value in values):
            return None
        return max(values)

    remote = any(command.get('inner_wall_seconds') is not None
                 for phase in phases.values() for command in phase.get('commands', ()))
    wall = total('wall_seconds')
    remote_overhead = (round(sum(phase_overhead(phase) for phase in phases.values()), 3)
                       if remote else None)
    return {
        'wall_seconds': wall,
        # Time in SSH and the agent's start-up around remote commands, where it
        # is on the critical path; it is inside wall_seconds (the destination's view).
        'remote_overhead_seconds': remote_overhead,
        # wall_seconds with that remote-launch overhead (outer minus inner wall
        # of the remote commands) taken out; equal for local-only methods.
        'wall_seconds_less_remote_overhead': None if wall is None else
        round(wall - (remote_overhead or 0), 3),
        'wire_bytes_source_to_dest': total('net', 'source', 'tx_bytes'),
        'wire_bytes_dest_to_source': total('net', 'dest', 'tx_bytes'),
        'wire_bytes_source_to_dest_dest_rx': total('net', 'dest', 'rx_bytes'),
        'wire_bytes_dest_to_source_source_rx': total('net', 'source', 'rx_bytes'),
        'cpu_seconds_source': total('cpu', 'source', 'busy_seconds'),
        'cpu_seconds_dest': total('cpu', 'dest', 'busy_seconds'),
        'process_cpu_seconds_source': process_cpu('source'),
        'process_cpu_seconds_dest': process_cpu('dest'),
        'peak_rss_kb_source': peak_rss('source'),
        'peak_rss_kb_dest': peak_rss('dest'),
    }


def without_part(phases: dict, leave_out: str) -> dict:
    """phases with one part of each concurrent phase left out (rsync in place: no copy of B0).

    The other parts become a phase of their own, with the longest one's wall
    and their commands. A window's counters are per VM, not per part: its
    network counters are kept (the left-out part must not use the link, and
    rsync's local copy of B0 does not), and so is the CPU of every VM the
    left-out part did not run on; the CPU of the VM it ran on is unknown (None).
    """
    result = {}
    for name, phase in phases.items():
        parts = phase.get('parts') or {}
        if leave_out not in parts:
            result[name] = phase
            continue
        kept = {part: info for part, info in parts.items() if part != leave_out}
        if not kept:
            continue
        busy_host = parts[leave_out]['host']
        label = '+'.join(kept)
        result[label] = {
            'name': label,
            'wall_seconds': max(info['wall_seconds'] for info in kept.values()),
            'commands': [command for part in kept for command in part_commands(phase, part)],
            'net': phase.get('net'),
            'cpu': {host: (None if host == busy_host else counters)
                    for host, counters in (phase.get('cpu') or {}).items()},
            **({'parts': kept} if len(kept) > 1 else {})}
    return result


def serial_totals(phases: dict) -> dict:
    """Wall time as if every concurrent phase's parts ran one after the other: an upper bound."""
    split = {}
    for name, phase in phases.items():
        parts = phase.get('parts')
        if not parts:
            split[name] = phase
            continue
        for part, info in parts.items():
            split[f'{name}.{part}'] = {'wall_seconds': info['wall_seconds'],
                                       'commands': part_commands(phase, part)}
    totals = phase_totals(split)
    return {key: totals[key] for key in ('wall_seconds', 'remote_overhead_seconds',
                                         'wall_seconds_less_remote_overhead')}


def prefetch_untimed(phases: dict, prefetch) -> dict | None:
    """The part of pgdelta's prefetch of B0 that fell between its two timed phases.

    The receiver reads B0 from when it listens until the sender connects.
    receiver_capture ends once it listens and the capture has ended; the
    transfer phase starts the sender. Between the two, outside any timer, the
    orchestrator reads both VMs' counters (an SSH call to the source on each
    side of the gap), and the prefetch keeps reading if it hasn't finished.
    B0 read then is read off the clock; the transfer would otherwise have read
    it, so total_s can understate by about `untimed_seconds`. No other method
    runs anything in that gap: rsync's copy of B0 ends inside prep_capture.

    Both clocks are pgd-dest's: the phases' started_utc/ended_utc are the
    orchestrator's time.time(), and the prefetch's started_utc/ended_utc the
    receiver's (pgdelta's statistics). `untimed_bytes_estimate` assumes the
    prefetch's average read rate. None if the run lacks either phase or the
    prefetch has no wall-clock times (it did not run, or an older pgdelta).
    """
    first, second = phases.get(RECEIVER_PHASE), phases.get('transfer')
    if not first or not second or not isinstance(prefetch, dict):
        return None
    start, end = prefetch.get('started_utc'), prefetch.get('ended_utc')
    if start is None or end is None or first.get('started_utc') is None \
            or second.get('started_utc') is None:
        return None
    gap_start = first.get('ended_utc')
    if gap_start is None:  # records from before ended_utc was kept
        gap_start = first['started_utc'] + first['wall_seconds']
    gap_end = second['started_utc']
    untimed = round(max(0.0, min(end, gap_end) - max(start, gap_start)), 3)
    seconds, read = prefetch.get('seconds') or 0, prefetch.get('bytes') or 0
    estimate = min(read, round(read * untimed / seconds)) if seconds > 0 else 0
    return {'gap_seconds': round(gap_end - gap_start, 3),
            # Negative when the prefetch ended inside receiver_capture.
            'end_after_phase_seconds': round(end - gap_start, 3),
            'untimed_seconds': untimed,
            'untimed_bytes_estimate': estimate,
            'spilled': untimed > 0}


def rusage_record(usage) -> dict:
    """CPU seconds from a resource.struct_rusage (the process and its reaped children).

    ru_maxrss is kept, as wait4_maxrss_kb, only for the record: it includes the
    high-water mark inherited from the launching process, so it is not the
    command's own peak memory (see GNU_TIME_FORMAT).
    """
    return {'user_seconds': round(usage.ru_utime, 3), 'system_seconds': round(usage.ru_stime, 3),
            'cpu_seconds': round(usage.ru_utime + usage.ru_stime, 3),
            'wait4_maxrss_kb': usage.ru_maxrss}
