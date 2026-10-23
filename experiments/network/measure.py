"""Pure helpers for EXP-04: counters, speeds, digests, pgBackRest's configuration and the CSV summary.

Nothing here runs a command. The agent (agent.py) and the orchestrator
(run_exp04.py) read /proc and the filesystem through these functions, and the
tests exercise them on fixed text.
"""
from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import stat
import struct

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
# T3 (the destination's write path): staged like pgdelta, each with one switch changed.
T3_METHODS = ('pgdelta_gapfill_seq', 'pgdelta_seq', 'pgdelta_fsync_wb', 'pgdelta_fsync_worker',
              'pgdelta_inplace')
# T8a: the sender reads the cleanly stopped source's data directory; no capture.
PGDELTA_DIRECT_METHODS = ('pgdelta_direct', 'pgdelta_direct_inplace')
# T11: pgBackRest restore --delta from a repository on the source, its backup counted as paid.
PGBACKREST_METHODS = ('pgbackrest_zst', 'pgbackrest_none', 'pgbackrest_block')
# Variants run only when named in --methods.
OPTIONAL_METHODS = ('pgdelta_1thread', *T3_METHODS, *PGDELTA_DIRECT_METHODS, *PGBACKREST_METHODS)
ALL_METHODS = METHODS + OPTIONAL_METHODS
# pgdelta methods that capture S on the source first, with the receiver started beside the capture.
PGDELTA_STAGED_METHODS = ('pgdelta', 'pgdelta_1thread', *T3_METHODS)
PGDELTA_METHODS = PGDELTA_STAGED_METHODS + PGDELTA_DIRECT_METHODS
RSYNC_METHODS = ('rsync_8k', 'rsync_default')
STAGED_METHODS = PGDELTA_STAGED_METHODS + RSYNC_METHODS
# pgdelta refreshing B itself (--in-place) rather than writing a new copy O.
IN_PLACE_METHODS = ('pgdelta_inplace', 'pgdelta_direct_inplace')
# Methods that patch B, a copy of B0 made before the cache drop and not timed: in a real
# refresh B already exists. The copy keeps B0 itself unchanged for the next method.
BASIS_COPY_METHODS = IN_PLACE_METHODS + PGBACKREST_METHODS
# Methods whose verification starts the output on dest (on run_exp04.OUTPUT_PG_PORT).
OUTPUT_START_METHODS = PGDELTA_DIRECT_METHODS + PGBACKREST_METHODS
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
#
# T3's and T8a's methods (decided October 23, 2026) keep EXP-06's 4 jobs and the prefetch,
# and change one thing each. They pin the receiver's --fsync too, so that no change of its
# default can move them; pgdelta and pgdelta_1thread are left as they were, so they still run
# on a pgdelta without --fsync, and the mode their statistics report is recorded and checked.
# - fsync: the receiver's --fsync, where O's files are flushed: in the main loop (today's
#   write path), by early write-back, or on a worker thread.
# - in_place: the receiver's --in-place: B itself is refreshed, with no --output and no
#   --rebuild (a copy of B0 made before the cache drop stands in for B, untimed).
# - source_kind: the sender's --source-kind: 'datadir' reads the cleanly stopped source's data
#   directory instead of a capture S.
PGDELTA_ENGINES = {
    'pgdelta': {'jobs': 4, 'rebuild': 'copy-patch', 'verify': 'pipelined', 'prefetch_basis': 'on'},
    'pgdelta_1thread': {'jobs': 1, 'rebuild': 'gap-fill', 'verify': 'sequential', 'prefetch_basis': 'on'},
    # T3 (a): gap-fill against copy-then-patch, both with sequential verification.
    'pgdelta_gapfill_seq': {'jobs': 4, 'rebuild': 'gap-fill', 'verify': 'sequential',
                            'prefetch_basis': 'on', 'fsync': 'main'},
    # T3 (a) and (c): copy-then-patch with sequential verification (against pgdelta's pipelined).
    'pgdelta_seq': {'jobs': 4, 'rebuild': 'copy-patch', 'verify': 'sequential', 'prefetch_basis': 'on',
                    'fsync': 'main'},
    # T3 (b): pgdelta with the fsync moved out of the main loop.
    'pgdelta_fsync_wb': {'jobs': 4, 'rebuild': 'copy-patch', 'verify': 'pipelined', 'prefetch_basis': 'on',
                         'fsync': 'writeback'},
    'pgdelta_fsync_worker': {'jobs': 4, 'rebuild': 'copy-patch', 'verify': 'pipelined',
                             'prefetch_basis': 'on', 'fsync': 'worker'},
    # T3 (e): B patched in place, the rest as pgdelta.
    'pgdelta_inplace': {'jobs': 4, 'rebuild': None, 'verify': 'pipelined', 'prefetch_basis': 'on',
                        'fsync': 'main', 'in_place': True},
    # T8a: today's write path (a new copy O) and in place, both reading the stopped source directly.
    'pgdelta_direct': {'jobs': 4, 'rebuild': 'copy-patch', 'verify': 'pipelined', 'prefetch_basis': 'on',
                       'fsync': 'main', 'source_kind': 'datadir'},
    'pgdelta_direct_inplace': {'jobs': 4, 'rebuild': None, 'verify': 'pipelined', 'prefetch_basis': 'on',
                               'fsync': 'main', 'in_place': True, 'source_kind': 'datadir'},
}


def pgdelta_engine_args(engine: dict) -> dict:
    """{'send': argv, 'receive': argv}: the hashing and rebuilding flags of `engine` on each end."""
    jobs = ('--jobs', str(engine['jobs']))
    rebuild = ('--rebuild', engine['rebuild']) if engine.get('rebuild') else ()
    return {'send': jobs, 'receive': (*jobs, *rebuild, '--verify', engine['verify'])}


def pgdelta_args(engine: dict) -> dict:
    """{'send': argv, 'receive': argv}: every flag `engine` pins, the receiver's prefetch included."""
    args = pgdelta_engine_args(engine)
    receive = (*args['receive'], '--prefetch-basis', engine['prefetch_basis'])
    if engine.get('fsync'):
        receive += ('--fsync', engine['fsync'])
    if engine.get('in_place'):
        receive += ('--in-place',)
    send = args['send']
    if engine.get('source_kind'):
        send += ('--source-kind', engine['source_kind'])
    return {'send': send, 'receive': receive}


def pgdelta_switches(engine: dict) -> dict:
    """The `switches` pgdelta's statistics report when `engine` is in effect.

    An in-place engine pins no --rebuild, so the receiver's rebuild is left out
    (pgdelta_switches_match compares only the switches named here).
    """
    backend = 'process' if engine['jobs'] > 1 else 'serial'
    verify = engine['verify'] if engine['jobs'] > 1 else 'sequential'  # no workers: always sequential
    receiver = {'jobs': engine['jobs'], 'hash_backend': backend, 'rebuild': engine.get('rebuild'),
                'verify': verify}
    if receiver['rebuild'] is None:
        del receiver['rebuild']
    return {'sender': {'jobs': engine['jobs'], 'hash_backend': backend}, 'receiver': receiver}


def pgdelta_switches_match(expected: dict, reported) -> bool:
    """Whether the reported switches include every switch `expected` names, with its value.

    Switches a newer pgdelta reports beyond these (its fsync mode, say) don't count against it.
    """
    if not isinstance(reported, dict):
        return False
    return all(isinstance(reported.get(side), dict)
               and all(reported[side].get(key) == value for key, value in values.items())
               for side, values in expected.items())


PGDELTA_ARGS = {method: pgdelta_args(engine) for method, engine in PGDELTA_ENGINES.items()}
# Options each end's `pgdelta <command> --help` must list for PGDELTA_ARGS to work.
PGDELTA_OPTIONS = {command: tuple(arg for arg in args if arg.startswith('--'))
                   for command, args in PGDELTA_ARGS['pgdelta'].items()}
# The module that lists what pgdelta's datadir mode reads (pgdelta_direct*'s verification):
# `python -m pgdelta.datadir list DIR --pg-controldata PATH` prints the selected relative paths.
PGDELTA_DATADIR_MODULE = 'pgdelta.datadir'


def pgdelta_required_options(methods) -> dict:
    """{'send': options, 'receive': options}: what `pgdelta <command> --help` must list for `methods`.

    The union of the flags every selected pgdelta method passes, so a run of the default
    methods asks for exactly PGDELTA_OPTIONS and a run without pgdelta asks for nothing.
    """
    required: dict[str, list] = {}
    for method in methods:
        for command, args in PGDELTA_ARGS.get(method, {}).items():
            options = required.setdefault(command, [])
            options += [arg for arg in args if arg.startswith('--') and arg not in options]
    return {command: tuple(options) for command, options in required.items()}


def pgdelta_modes(engine: dict) -> dict:
    """The read and write modes pgdelta's statistics should report for `engine`.

    pgdelta and pgdelta_1thread pin no mode: they expect today's write path (a new copy,
    fsync in the main loop) and a capture S.
    """
    return {'in_place': bool(engine.get('in_place')), 'fsync_mode': engine.get('fsync') or 'main',
            'source_kind': engine.get('source_kind') or 'backup'}


def pgdelta_reported(stats: dict, *keys, accept=None):
    """The first of `keys` that pgdelta's statistics report, wherever this version keeps it.

    The mode keys (in_place, fsync_mode, source_kind, fsync_wait_seconds) may be at the top
    level, in the receiver's part, in the switches or with the timings; None if absent.
    With `accept`, only a value it accepts counts (the timings' `fsync` is seconds, not a mode).
    """
    receiver = stats.get('receiver') if isinstance(stats.get('receiver'), dict) else {}
    places = (stats, receiver, _get(stats, 'switches', 'receiver'), _get(stats, 'switches', 'sender'),
              stats.get('timings_seconds'), receiver.get('timings_seconds'))
    for key in keys:
        for place in places:
            if isinstance(place, dict) and place.get(key) is not None \
                    and (accept is None or accept(place[key])):
                return place[key]
    return None


# The receiver's --fsync choices. pgdelta's receiver reports `fsync` (the mode asked for) and
# `fsync_effective` (the one in effect: writeback falls back to main where sync_file_range is
# missing or refused); the harness's contract named `fsync_mode`, which counts too.
FSYNC_MODES = ('main', 'writeback', 'worker')


def _fsync_mode(value) -> bool:
    return value in FSYNC_MODES


def pgdelta_modes_report(stats: dict, engine: dict) -> dict:
    """The modes pgdelta reported, the ones `engine` expects, and whether they agree.

    The fsync mode compared is the one in effect, so a writeback run that fell back to main
    fails modes_ok; the one asked for is kept as fsync_requested. modes_ok is None when this
    pgdelta reports none of the modes (before the modes existed).
    """
    expected = pgdelta_modes(engine)
    reported = {key: pgdelta_reported(stats, key) for key in expected if key != 'fsync_mode'}
    reported['fsync_mode'] = pgdelta_reported(stats, 'fsync_effective', 'fsync_mode', 'fsync',
                                              accept=_fsync_mode)
    reported = {key: reported[key] for key in expected}
    known = {key: value for key, value in reported.items() if value is not None}
    return {'modes': reported, 'modes_expected': expected,
            'modes_ok': None if not known else all(expected[key] == value for key, value in known.items()),
            'fsync_requested': pgdelta_reported(stats, 'fsync', 'fsync_mode', accept=_fsync_mode),
            'fsync_wait_seconds': pgdelta_reported(stats, 'fsync_wait_seconds', 'fsync_wait')}
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
for _method in T3_METHODS:
    PROC_SCOPE[_method] = PROC_SCOPE['pgdelta']
for _method in PGDELTA_DIRECT_METHODS:
    PROC_SCOPE[_method] = ('source: pgdelta send, reading the stopped data directory (no capture, no '
                           'walsender); dest: pgdelta receive. At --jobs 4 each starts 4 worker processes '
                           'with spawn, as its own children, so CPU includes them. Peak RSS: largest single '
                           'process (pgdelta and its 4 workers not summed)')
for _method in PGBACKREST_METHODS:
    PROC_SCOPE[_method] = ('dest: pgbackrest restore, with its 4 local processes and their SSH clients '
                           '(its children, reaped, so CPU includes them); source: none (no walsender '
                           'serves a restore, and the remote pgbackrest processes SSH starts there to read '
                           'the repository are not our children; VM-wide CPU covers them). Peak RSS: '
                           'largest single process (pgbackrest and its local processes not summed)')
del _method
# rsync's copy of B0 (prep, on dest) and its capture of S (on source) touch
# different machines and the link is idle during both, so they run at the same
# time in one timed phase, as a real cp-then-rsync refresh would.
CONCURRENT_PHASE = 'prep_capture'
# pgdelta's counterpart: its receiver starts on dest, and prefetches B0 while it
# waits for the sender, at the same time as the capture of S on source.
RECEIVER_PHASE = 'receiver_capture'
# pgdelta_direct*: no capture to start beside, so the receiver's start and the transfer are one
# timed phase, one counter window, with no untimed gap between them in which the receiver would
# prefetch B0 off the clock. Its steps, one after the other: receiver_start (from the launch until
# the receiver listens), then transfer (the sender, until both ends exit).
DIRECT_PHASE = 'receiver_transfer'
DIRECT_RECEIVER_STEP = 'receiver_start'
# pgbackrest_*: the one timed phase, on dest.
RESTORE_PHASE = 'restore'
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


def parse_method_speeds(specs, methods, speeds) -> dict[str, list[str]]:
    """--method-speeds: method -> the labels of the speeds it runs at, for the methods it limits.

    Each spec is METHODS=SPEEDS, the methods joined with '+' and the speeds a comma list
    as in --speeds, for example 'pgdelta_seq+pgdelta_inplace=unthrottled'. Every method
    must be in `methods` and named once, and every speed must be one of `speeds`; a method
    no spec names runs at every speed.
    """
    labels = [speed['label'] for speed in speeds]
    limited: dict[str, list[str]] = {}
    for spec in specs:
        names, equals, values = spec.partition('=')
        names = [name.strip() for name in names.split('+') if name.strip()]
        if not equals or not names:
            raise ValueError(f'--method-speeds wants METHODS=SPEEDS, got {spec!r}')
        chosen = [speed['label'] for speed in parse_speeds(values)]
        unknown = [label for label in chosen if label not in labels]
        if unknown:
            raise ValueError(f'--method-speeds {spec!r}: {", ".join(unknown)} not in --speeds')
        for name in names:
            if name not in methods:
                raise ValueError(f'--method-speeds {spec!r}: {name} is not in --methods')
            if name in limited:
                raise ValueError(f'--method-speeds names {name} twice')
            limited[name] = [label for label in labels if label in chosen]  # in --speeds' order
    return limited


def methods_at(methods, limited: dict, label: str) -> list[str]:
    """The methods that run at the speed `label`, in their --methods order."""
    return [method for method in methods if method not in limited or label in limited[method]]


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


def files_of(digest: dict) -> dict:
    """A tree digest with its regular files only (directories, links and the rest left out)."""
    entries = {path: entry for path, entry in digest['entries'].items() if entry[0] == 'file'}
    return {**digest, 'entries': entries, 'files': len(entries),
            'bytes': sum(entry[1] for entry in entries.values())}


def files_digest(root: Path, paths) -> dict:
    """tree_digest's entries for the listed relative paths only, each one a regular file.

    What pgdelta's datadir mode reads from a stopped data directory is a selection of its
    files (PostgreSQL's base-backup exclusions applied), so only those are hashed; a listed
    path that is missing or not a regular file goes in `missing`. `paths` may map each listed
    path to the path whose bytes pgdelta sends for it (an unlogged relation's main fork is sent
    with its init fork's bytes); each entry is then keyed by the listed path and hashes the
    other, and `read_from` names those that differ.
    """
    root = Path(root)
    sources = dict(paths) if isinstance(paths, dict) else {rel: rel for rel in paths}
    entries, missing, total = {}, [], 0
    for rel in sorted(sources):
        path = root / sources[rel]
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            missing.append(rel)
            continue
        if not stat.S_ISREG(info.st_mode):
            missing.append(rel)
            continue
        digest = hashlib.sha256()
        with open(path, 'rb') as stream:
            while chunk := stream.read(DIGEST_CHUNK):
                digest.update(chunk)
        entries[rel] = ['file', info.st_size, digest.hexdigest(), stat.S_IMODE(info.st_mode),
                        info.st_ino, info.st_nlink]
        total += info.st_size
    return {'root': str(root), 'device': os.lstat(root).st_dev, 'entries': entries,
            'files': len(entries), 'bytes': total, 'missing': missing,
            'read_from': {rel: read for rel, read in sorted(sources.items()) if read != rel}}


def parse_datadir_sources(text: str) -> dict[str, str]:
    """{listed path: path read} from `python -m pgdelta.datadir list [--sources]`, as JSON.

    With --sources it prints [path, path read] pairs, which differ for the main fork of an
    unlogged relation: pgdelta sends it with its init fork's bytes, the empty relation that
    recovery's reset would leave. Without, a list of paths, each read as itself. Either may be
    held under 'files' or 'paths' in an object. If the whole output doesn't parse, its last
    non-empty line is tried.
    """
    try:
        value = json.loads(text)
    except ValueError:
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            raise ValueError('pgdelta.datadir list printed nothing') from None
        value = json.loads(lines[-1])
    if isinstance(value, dict):
        value = next((value[key] for key in ('files', 'paths') if isinstance(value.get(key), list)), None)
    if not isinstance(value, list):
        raise ValueError('pgdelta.datadir list must print a JSON list of relative paths')
    sources = {}
    for item in value:
        if isinstance(item, str):
            sources[item] = item
        elif isinstance(item, list) and len(item) == 2 and all(isinstance(part, str) for part in item):
            sources[item[0]] = item[1]
        else:
            raise ValueError('pgdelta.datadir list must print relative paths or [path, path read] pairs')
    return sources


def parse_datadir_list(text: str) -> list[str]:
    """The relative paths `python -m pgdelta.datadir list` printed as JSON (see parse_datadir_sources)."""
    return list(parse_datadir_sources(text))


# --- shared extents (reflink clones) ----------------------------------------------

# FS_IOC_FIEMAP maps a file's extents and flags each one whose blocks another file shares too:
# a reflink clone on XFS. Since coreutils 9.0 (Debian 12 ships 9.1) cp defaults to
# --reflink=auto, and copy_file_range clones as well, so on dest's XFS a plain `cp -a B0 B`
# makes B share every block with B0. ext4 has no reflinks and never sets the flag.
FS_IOC_FIEMAP = 0xC020660B  # _IOWR('f', 11, struct fiemap)
FIEMAP_MAX_OFFSET = (1 << 64) - 1
FIEMAP_FLAG_SYNC = 0x1
FIEMAP_EXTENT_LAST = 0x1
FIEMAP_EXTENT_SHARED = 0x2000
# struct fiemap: fm_start, fm_length, fm_flags, fm_mapped_extents, fm_extent_count, fm_reserved.
FIEMAP_HEADER = struct.Struct('=QQIIII')
# struct fiemap_extent: fe_logical, fe_physical, fe_length, fe_reserved64[2], fe_flags, fe_reserved[3].
FIEMAP_EXTENT = struct.Struct('=QQQQQIIII')
FIEMAP_BATCH = 256


def parse_fiemap(buffer) -> list[tuple[int, int, int]]:
    """(logical offset, length, flags) of each extent an FS_IOC_FIEMAP call filled in."""
    mapped = FIEMAP_HEADER.unpack_from(buffer, 0)[3]
    extents = []
    for index in range(mapped):
        logical, _, length, _, _, flags, _, _, _ = FIEMAP_EXTENT.unpack_from(
            buffer, FIEMAP_HEADER.size + index * FIEMAP_EXTENT.size)
        extents.append((logical, length, flags))
    return extents


def shared_extent_bytes(path) -> int:
    """Bytes of `path` in extents that share their blocks with another file; OSError without FIEMAP."""
    shared, start = 0, 0
    with open(path, 'rb') as stream:
        while True:
            request = bytearray(FIEMAP_HEADER.size + FIEMAP_BATCH * FIEMAP_EXTENT.size)
            FIEMAP_HEADER.pack_into(request, 0, start, FIEMAP_MAX_OFFSET - start, FIEMAP_FLAG_SYNC, 0,
                                    FIEMAP_BATCH, 0)
            fcntl.ioctl(stream.fileno(), FS_IOC_FIEMAP, request)
            extents = parse_fiemap(request)
            shared += sum(length for _, length, flags in extents if flags & FIEMAP_EXTENT_SHARED)
            if not extents or extents[-1][2] & FIEMAP_EXTENT_LAST:
                return shared
            start = extents[-1][0] + extents[-1][1]


def shared_extents(root, limit: int = 20) -> dict:
    """How much of the regular files below root shares disk blocks with other files (FIEMAP).

    A copy made with `cp --reflink=never` must share nothing; one made with cp's default or
    copy_file_range on XFS shares the blocks of whatever it was cloned from, B0 here.
    `supported` is False where FIEMAP isn't available (macOS, some filesystems), and None
    when there was no file to ask about.
    """
    checked = shared_files = shared = 0
    examples = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                count = shared_extent_bytes(path)
            except OSError as exc:
                return {'supported': False, 'error': f'{type(exc).__name__}: {exc}', 'files': checked,
                        'shared_files': None, 'shared_bytes': None, 'examples': examples}
            checked += 1
            if count:
                shared_files += 1
                shared += count
                if len(examples) < limit:
                    examples.append(path.relative_to(root).as_posix())
    return {'supported': True if checked else None, 'error': None, 'files': checked,
            'shared_files': shared_files, 'shared_bytes': shared, 'examples': examples}


# What an in-place refresh writes into B while it runs, and must leave no trace of.
IN_PLACE_MARKER = 'pgdelta-incomplete.json'


def in_place_leftovers(digest: dict) -> list[str]:
    """What a finished in-place refresh must not leave in B.

    While it runs, pgdelta --in-place writes B/pgdelta-incomplete.json and keeps
    global/pg_control under another name, so PostgreSQL refuses to start on a half-done B.
    Afterwards neither may remain: no marker, no entry named after pgdelta, no other
    global/pg_control* file, and global/pg_control itself back in place.
    """
    leftovers = []
    for path in sorted(digest['entries']):
        name = path.rsplit('/', 1)[-1]
        renamed_control = (path.startswith('global/') and name.startswith('pg_control')
                           and path != 'global/pg_control')
        if name == IN_PLACE_MARKER or 'pgdelta' in name or renamed_control:
            leftovers.append(path)
    if 'global/pg_control' not in digest['entries']:
        leftovers.append('global/pg_control is missing')
    return leftovers


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


# --- logical digest (pgbackrest_*'s verification) ---------------------------------

# EXP-01's tables (run_study.USER_TABLES) with their primary keys; pgbench_history has none.
LOGICAL_TABLES = (('pgbench_accounts', 'aid'), ('pgbench_branches', 'bid'), ('pgbench_tellers', 'tid'),
                  ('pgbench_history', None), ('events', 'id'))
# Rows are hashed in blocks of this many key values, so no aggregated string nears
# PostgreSQL's 1 GB limit at any scale.
LOGICAL_BLOCK_KEYS = 65536
# PGOPTIONS for every digest session: a row's text must not depend on the server's settings
# (events.ts is a timestamptz).
LOGICAL_SESSION_OPTIONS = '-c TimeZone=UTC -c DateStyle=ISO,YMD -c extra_float_digits=3'


def table_digest_sql(table: str, key: str | None, block: int = LOGICAL_BLOCK_KEYS) -> str:
    """A scalar subquery: the table's row count and an md5 over its rows in a fixed order.

    With a primary key, each block of `block` key values hashes its rows' md5s in key
    order, and the table's md5 is that of the block hashes in block order: the rows in
    key order, in two levels. Without one, the rows' md5s are taken in the order of the
    rows' text (all rows sorted).
    """
    if key is None:
        return (f"(SELECT json_build_object('rows', count(*), 'md5', "
                f"md5(coalesce(string_agg(md5(r), '' ORDER BY r), ''))) "
                f"FROM (SELECT t::text AS r FROM {table} t) s)")
    return (f"(SELECT json_build_object('rows', coalesce(sum(n), 0), 'md5', "
            f"md5(coalesce(string_agg(h, '' ORDER BY b), ''))) "
            f"FROM (SELECT {key} / {block} AS b, count(*) AS n, "
            f"md5(string_agg(md5(t::text), '' ORDER BY {key})) AS h "
            f"FROM {table} t GROUP BY 1) s)")


def logical_digest_sql(tables=LOGICAL_TABLES, block: int = LOGICAL_BLOCK_KEYS) -> str:
    """One query printing {table: {rows, md5}} as JSON for every table in `tables`."""
    parts = ', '.join(f"'{table}', {table_digest_sql(table, key, block)}" for table, key in tables)
    return f'SELECT json_build_object({parts})::text;'


def compare_logical(expected: dict, actual: dict) -> tuple[bool, list[str]]:
    """Same tables with the same row counts and md5s."""
    differences = []
    for table in sorted(expected.keys() | actual.keys()):
        left, right = expected.get(table), actual.get(table)
        if left is None or right is None:
            differences.append(f'{table}: only in {"the output" if left is None else "the source"}')
        elif (int(left['rows']), left['md5']) != (int(right['rows']), right['md5']):
            differences.append(f'{table}: {left["rows"]} rows md5 {left["md5"]} on the source, '
                               f'{right["rows"]} rows md5 {right["md5"]} in the output')
    return not differences, differences


# --- pgBackRest (T11) -------------------------------------------------------------

# One stanza per variant, named after its method, each with its own repository on the
# source. What differs between them: compression, and block incremental with bundling.
# pgbackrest_none is uncompressed on the network too (EXP-08): with compress-type=none and the
# repository on another host, pgBackRest compresses what it sends at compress-level-network
# (default 3) unless that is 0. The zst variants send their files as stored, and pgBackRest
# ignores the network level for them.
PGBACKREST_VARIANTS = {
    'pgbackrest_zst': {'compress-type': 'zst'},
    'pgbackrest_none': {'compress-type': 'none', 'compress-level-network': '0'},
    'pgbackrest_block': {'compress-type': 'zst', 'repo1-bundle': 'y', 'repo1-block': 'y'},
}
# The variant options that act on dest, where the restore and the recovery's archive-get run and
# reach the repository over SSH: they go in dest's stanza sections too.
PGBACKREST_DEST_OPTIONS = ('compress-level-network',)
PGBACKREST_PROCESS_MAX = 4
PGBACKREST_LOG_LEVELS = {'log-level-console': 'info', 'log-level-file': 'detail'}


def pgbackrest_paths(work) -> dict:
    """Where pgBackRest's files go, at the same paths on both VMs (the work directory's)."""
    root = f'{work}/pgbackrest'
    return {'root': root, 'config': f'{root}/pgbackrest.conf', 'log': f'{root}/log',
            'lock': f'{root}/lock', 'spool': f'{root}/spool', 'ssh': f'{root}/ssh',
            'ssh_config': f'{root}/ssh_config'}


def pgbackrest_repo(work, stanza: str) -> str:
    return f'{pgbackrest_paths(work)["root"]}/repo-{stanza}'


def _ini(header: list[str], sections: dict) -> str:
    lines = list(header)
    for name, options in sections.items():
        lines += ['', f'[{name}]', *(f'{key}={value}' for key, value in options.items())]
    return '\n'.join(lines) + '\n'


def pgbackrest_common(work) -> dict:
    paths = pgbackrest_paths(work)
    return {'log-path': paths['log'], 'lock-path': paths['lock'], 'spool-path': paths['spool'],
            **PGBACKREST_LOG_LEVELS}


def pgbackrest_source_config(work, stanzas, *, pg_path, socket_path, port, user) -> str:
    """pgbackrest.conf on pgd-source, the repository host: one stanza per variant."""
    sections = {'global': {**pgbackrest_common(work), 'start-fast': 'y',
                           'process-max': str(PGBACKREST_PROCESS_MAX), 'repo1-retention-full': '1'}}
    for stanza in stanzas:
        sections[stanza] = {'pg1-path': pg_path, 'pg1-socket-path': socket_path, 'pg1-port': str(port),
                            'pg1-user': user, 'repo1-path': pgbackrest_repo(work, stanza),
                            **PGBACKREST_VARIANTS[stanza]}
    return _ini(['# EXP-04 (T11): pgBackRest on pgd-source, the repository host. One stanza per',
                 '# variant, each with its own repository under the work directory.'], sections)


def pgbackrest_dest_config(work, stanzas, *, source_ip, ssh_user) -> str:
    """pgbackrest.conf on pgd-dest: restores from the repositories on pgd-source over SSH.

    pgBackRest's SSH goes through cmd-ssh, a wrapper that uses its own SSH configuration
    with the harness key (pgbackrest_ssh_config), so ~/.ssh/config is left alone.
    """
    paths = pgbackrest_paths(work)
    sections = {'global': {**pgbackrest_common(work), 'cmd-ssh': paths['ssh'], 'repo1-host': source_ip,
                           'repo1-host-user': ssh_user, 'repo1-host-config': paths['config']}}
    for stanza in stanzas:
        sections[stanza] = {'repo1-path': pgbackrest_repo(work, stanza), **pgbackrest_dest_options(stanza)}
    return _ini(['# EXP-04 (T11): pgBackRest on pgd-dest, restoring from the repositories on',
                 '# pgd-source over SSH. pg1-path is given on each restore command line.'], sections)


def pgbackrest_dest_options(stanza: str) -> dict:
    """The variant's options that dest's own pgBackRest commands use (PGBACKREST_DEST_OPTIONS)."""
    return {key: value for key, value in PGBACKREST_VARIANTS[stanza].items() if key in PGBACKREST_DEST_OPTIONS}


def pgbackrest_ssh_config(source_ip: str, user: str, key: str) -> str:
    """The SSH configuration pgBackRest uses on pgd-dest: the harness key, never a prompt."""
    return '\n'.join([
        '# EXP-04 (T11): how pgBackRest on pgd-dest reaches the repository host, pgd-source.',
        f'Host {source_ip}', f'    User {user}', f'    IdentityFile {key}', '    IdentitiesOnly yes',
        '    BatchMode yes', '    StrictHostKeyChecking accept-new', '    ServerAliveInterval 15', ''])


def pgbackrest_ssh_wrapper(ssh_config: str) -> str:
    """pgBackRest's cmd-ssh on pgd-dest: ssh with its own configuration file."""
    return ('#!/bin/sh\n# EXP-04 (T11): pgBackRest\'s SSH client on pgd-dest (cmd-ssh).\n'
            f'exec ssh -F {ssh_config} "$@"\n')


def pgbackrest_archive_command(exe: str, config: str, stanzas) -> str:
    """archive_command pushing every WAL file to every configured stanza's repository."""
    return ' && '.join(f'{exe} --config={config} --stanza={stanza} archive-push %p' for stanza in stanzas)


def pgbackrest_restore_argv(exe: str, config: str, stanza: str, pg_path) -> list[str]:
    """The timed restore on pgd-dest: B patched from the repository (--delta), 4 processes."""
    return [exe, f'--config={config}', f'--stanza={stanza}', '--delta',
            f'--process-max={PGBACKREST_PROCESS_MAX}', f'--pg1-path={pg_path}', 'restore']


def parse_pgbackrest_restore(text: str) -> dict:
    """What pgBackRest's restore log says: size, files, how many matched, its end and statistics.

    The console log (info) has the summary line and the end line; the file log (detail)
    has one line per file. Counts are None when the log has no such lines.
    """
    size = re.search(r'restore size = ([^,\s]+), file total = (\d+)', text)
    end = re.search(r'restore command end: (completed successfully|aborted with exception \[\d+\])'
                    r'(?: \((\d+)ms\))?', text)
    begin = re.search(r'restore command begin ([0-9][^:\s]*)', text)
    backup = re.search(r'restore backup set (\S+)', text)
    file_lines = [line for line in text.splitlines() if 'restore file ' in line]
    statistics = None
    for line in reversed(text.splitlines()):
        found = re.search(r'statistics: (\{.*\})\s*$', line)
        if found:
            try:
                statistics = json.loads(found.group(1))
            except ValueError:
                pass
            break
    return {'version': begin.group(1) if begin else None,
            'backup_set': backup.group(1).rstrip(',') if backup else None,
            'restore_size': size.group(1) if size else None,
            'file_total': int(size.group(2)) if size else None,
            'file_lines': len(file_lines) if file_lines else None,
            'files_matched': (sum('exists and matches' in line for line in file_lines)
                              if file_lines else None),
            'completed': None if not end else end.group(1) == 'completed successfully',
            'reported_ms': int(end.group(2)) if end and end.group(2) else None,
            'statistics': statistics}


def pgbackrest_backup_summary(info_text: str) -> dict:
    """The latest backup of `pgbackrest info --output=json` for one stanza: sizes and times."""
    info = json.loads(info_text)
    stanza = info[0] if isinstance(info, list) and info else {}
    backups = stanza.get('backup') or []
    if not backups:
        return {'stanza': stanza.get('name'), 'status': stanza.get('status'), 'backup': None}
    latest = backups[-1]
    sizes = latest.get('info') or {}
    repository = sizes.get('repository') or {}
    return {'stanza': stanza.get('name'), 'label': latest.get('label'), 'type': latest.get('type'),
            'size_bytes': sizes.get('size'), 'delta_bytes': sizes.get('delta'),
            'repo_size_bytes': repository.get('size'), 'repo_delta_bytes': repository.get('delta'),
            'start': (latest.get('timestamp') or {}).get('start'),
            'stop': (latest.get('timestamp') or {}).get('stop')}


# PostgreSQL's server log, for each WAL file restore_command fetched (pgBackRest's archive-get).
RESTORED_WAL = re.compile(r'restored log file "([0-9A-F]{24}(?:\.partial)?|[0-9A-F]{8}\.history)" from archive')


def restored_wal_files(server_log: str) -> int:
    """How many WAL files (segments and timeline histories) the server's restore_command fetched."""
    return len(RESTORED_WAL.findall(server_log))


def with_recovery_bytes(totals: dict, recovery: dict) -> dict:
    """pgBackRest's totals with its recovery's bytes on the wire added, and kept apart too.

    The restore is timed; starting the restored copy, whose recovery fetches WAL from the
    repository through archive-get, is not. EXP-08 counts that WAL in pgBackRest's wire bytes,
    as pgdelta's WAL travels inside its timed transfer. Wall time and CPU stay the restore's.
    """
    net = recovery.get('net') or {}
    extra = {'wire_bytes_source_to_dest': _get(net, 'source', 'tx_bytes'),
             'wire_bytes_dest_to_source': _get(net, 'dest', 'tx_bytes'),
             'wire_bytes_source_to_dest_dest_rx': _get(net, 'dest', 'rx_bytes'),
             'wire_bytes_dest_to_source_source_rx': _get(net, 'source', 'rx_bytes')}
    result = dict(totals)
    for key, value in extra.items():
        result[key] = None if result.get(key) is None or value is None else result[key] + value
    result['recovery_wire_bytes_source_to_dest'] = extra['wire_bytes_source_to_dest']
    result['recovery_wire_bytes_dest_to_source'] = extra['wire_bytes_dest_to_source']
    result['wire_bytes_scope'] = ("the timed restore plus the untimed start of the restored copy, whose "
                                  "recovery fetches WAL through archive-get (recovery_wire_bytes_*)")
    return result


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
    'combine_s', 'restore_s', 'ssh_overhead_s', 'untimed_setup_s',
    'prefetch_bytes', 'prefetch_basis_bytes', 'prefetch_s', 'prefetch_wait_s', 'prefetch_stopped',
    'untimed_gap_s', 'prefetch_untimed_s', 'prefetch_untimed_bytes_est', 'hash_basis_s',
    'rebuild_s', 'fsync_s', 'fsync_wait_s', 'verify_wait_s', 'in_place', 'fsync_mode', 'source_kind',
    'wire_s2d_bytes', 'wire_d2s_bytes', 'wire_s2d_bytes_dest_rx', 'wire_d2s_bytes_source_rx',
    'recovery_wire_s2d_bytes', 'recovery_wire_d2s_bytes', 'recovery_wal_files',
    'tool_s2d_bytes', 'tool_d2s_bytes', 'output_bytes', 'output_minus_wal_padding_bytes',
    'output_shared_bytes', 'capture_bytes', 'wal_bytes', 'wal_stream_bytes', 'wal_padding_bytes',
    'cpu_source_s', 'cpu_dest_s', 'prep_capture_cpu_dest_s', 'receiver_capture_cpu_dest_s',
    'counter_windows',
    'proc_cpu_source_s', 'proc_cpu_dest_s', 'peak_rss_source_kb', 'peak_rss_dest_kb',
    'proc_scope',
    'verified', 'identical_to_s', 'logical_match', 'output_start_s', 'caches_dropped', 'error',
)


def _get(value, *keys):
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def phase_seconds(record: dict, name: str):
    """Wall seconds of a timed phase, of a part of a concurrent phase (prep, capture), or of a
    step of a sequential one (pgdelta_direct*'s receiver_start and transfer)."""
    phases = record.get('phases') or {}
    if name in phases:
        return _get(phases, name, 'wall_seconds')
    for phase in phases.values():
        for kind in ('parts', 'steps'):
            if name in (phase.get(kind) or {}):
                return _get(phase, kind, name, 'wall_seconds')
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
        # pgdelta only: from launching the receiver until it listened (a part of receiver_capture
        # for the staged methods, the first step of pgdelta_direct*'s one phase), and the pair's wall.
        'receiver_start_s': (phase_seconds(record, 'receiver') if phase_seconds(record, 'receiver')
                             is not None else phase_seconds(record, DIRECT_RECEIVER_STEP)),
        'receiver_capture_s': phase_seconds(record, RECEIVER_PHASE),
        'transfer_s': phase_seconds(record, 'transfer'),
        'combine_s': phase_seconds(record, 'combine'),
        # pgbackrest_* only: the restore on dest, the method's one timed phase.
        'restore_s': phase_seconds(record, RESTORE_PHASE),
        'ssh_overhead_s': totals.get('remote_overhead_seconds'),
        # Untimed, before the cache drop: the rsync daemon's start, the copy of B0 to B that the
        # in-place methods and pgBackRest patch, the clean stop of the source for pgdelta_direct*.
        'untimed_setup_s': _get(record, 'preparation', 'wall_seconds'),
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
        # pgdelta only, its own timers inside the transfer: the write phase, of which the fsyncs,
        # the main loop's wait for fsyncs done elsewhere (T3 (b)), and its wait for verification.
        'rebuild_s': _get(record, 'tool_reported', 'timings_seconds', 'rebuild'),
        'fsync_s': _get(record, 'tool_reported', 'timings_seconds', 'fsync'),
        'fsync_wait_s': _get(record, 'tool_reported', 'fsync_wait_seconds'),
        'verify_wait_s': _get(record, 'tool_reported', 'timings_seconds', 'verify_wait'),
        # pgdelta only: the modes its statistics report (empty before pgdelta reported them); the
        # fsync mode in effect, which for writeback can fall back to main (tool_reported.fsync_requested).
        'in_place': _get(record, 'tool_reported', 'modes', 'in_place'),
        'fsync_mode': _get(record, 'tool_reported', 'modes', 'fsync_mode'),
        'source_kind': _get(record, 'tool_reported', 'modes', 'source_kind'),
        'wire_s2d_bytes': totals.get('wire_bytes_source_to_dest'),
        'wire_d2s_bytes': totals.get('wire_bytes_dest_to_source'),
        'wire_s2d_bytes_dest_rx': totals.get('wire_bytes_source_to_dest_dest_rx'),
        'wire_d2s_bytes_source_rx': totals.get('wire_bytes_dest_to_source_source_rx'),
        # pgbackrest_* only: of the wire bytes above, those its restored copy's recovery fetched
        # (archive-get, untimed), and how many WAL files that was.
        'recovery_wire_s2d_bytes': totals.get('recovery_wire_bytes_source_to_dest'),
        'recovery_wire_d2s_bytes': totals.get('recovery_wire_bytes_dest_to_source'),
        'recovery_wal_files': _get(verification, 'recovery', 'wal_files_restored'),
        'tool_s2d_bytes': _get(record, 'tool_reported', 'bytes_source_to_dest'),
        'tool_d2s_bytes': _get(record, 'tool_reported', 'bytes_dest_to_source'),
        'output_bytes': record.get('output_bytes'),
        'output_minus_wal_padding_bytes': record.get('output_minus_wal_padding_bytes'),
        # The output's bytes in extents it shares with another file (FIEMAP), after the run: a clone
        # of B0 on XFS (rsync's cp -a, pgdelta's copy_file_range); 0 on ext4; empty without FIEMAP.
        'output_shared_bytes': _get(verification, 'shared_extents', 'shared_bytes'),
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
        # Staged methods: O (or B) against the capture S; pgdelta_direct*: against the files
        # pgdelta's datadir mode selects in the stopped source.
        'identical_to_s': (verification['identical_to_capture'] if 'identical_to_capture' in verification
                           else verification.get('identical_to_source')),
        # pgbackrest_* only: the restored copy's per-table digest against the source's (None
        # until the trial's last method has run).
        'logical_match': verification.get('logical_match'),
        # pgdelta_direct* and pgbackrest_*: starting the output on dest (untimed), recovery included.
        'output_start_s': _get(verification, 'output_start', 'ready_seconds'),
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

    pgdelta_direct* has no gap: its receiver start and transfer are one timed
    phase, the sender starting as soon as the receiver listens, so this is
    None for it.

    Both clocks are pgd-dest's: the phases' started_utc/ended_utc are the
    orchestrator's time.time(), and the prefetch's started_utc/ended_utc the
    receiver's (pgdelta's statistics). `untimed_bytes_estimate` assumes the
    prefetch's average read rate. None if the run lacks either phase or the
    prefetch has no wall-clock times (it did not run, or an older pgdelta).
    """
    first = phases.get(RECEIVER_PHASE)
    second = phases.get('transfer')
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
