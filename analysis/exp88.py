"""EXP-88's analysis: on a loaded primary, at three load levels and beyond memory, does pgdelta cost the load no
more than one pg_basebackup stream, and what does the WAL-summary fast path cost
(docs/experiments/EXP-88-slowdown-by-load-and-size.md).

    .venv/bin/python analysis/exp88.py [--runs DIR ...] [--figdir DIR] [--datadir DIR] [--tag T]

**Deciding, and nothing else: O4's two numbers,** as EXP-12's change 3 defines them and EXP-28 applied them. Per
run, over [t0, t1 + 10 s] on dest's clock in 1-second buckets, S_b the transactions scheduled in bucket b
(completion minus latency) and X_b those completed in it; the worst drop D = max(1 - X_b/S_b) and the work lost
W = sum(max(0, S_b - X_b)) / R.

**Cells are keyed by (arm, level)** -- L30, L60, L80 -- and every net value subtracts the median over *that same
level's* `none` runs (below 0 counts as 0): a level's null is never another level's. The verdict, the floors
delta_W = max(0.05 s, 0.15 x W_net(full)) and delta_D = 0.03, the readings at half and twice them (K7) and the
`near` flag are **EXP-28's own code**: this module hands one level's runs at a time to analysis/exp28.py's
cells() instead of restating O4, which is also what makes the null per level -- cells() can only net an arm
against the `none` runs it was given.

`none_nosum` (summarize_wal off) is a **control, not a competitor**: it is compared with `none` at the same level
-- EXP-83d's p99(on) / p99(off) ratio, `summarize_wal_p99_ratio` -- and carries no O4 verdict against `full`.

**Reported, never deciding:** p50, p95 and p99 over the window and their increase against the matched `none` cell
at the same level, (p_during / p_without - 1) x 100; TPS loss, (1 - TPS_during / TPS_baseline) x 100; the
backup's duration; the source's VM-wide CPU-seconds (/proc/stat); disk read and write bytes besides WAL on both
ends; and the **successful, the failed and the skipped transactions counted apart** (EXP-28's "failed" counts
failed *runs*, not failed transactions). A ratio of two percentiles compounds the tail noise of both, so the
percentiles sit beside the verdict and never instead of it.

**Three latencies, kept apart in the column names, because mixing them would mislead:**

- `lat_*` is what pgbench reports under -R: completion minus the time the transaction was *scheduled*. It is the
  **user-visible latency of an open loop and it INCLUDES the scheduling delay** the rate imposes. That is the
  right number for what a user feels, and it is **not** execution time.
- `lag_*` is the schedule lag alone, pgbench's last column under -R: a transaction's actual start minus its
  scheduled time. Reported beside the latency, never folded into it.
- `exec_*` is the difference, latency minus lag: what the server itself took. It has its own columns so that no
  one has to read an execution time out of `lat_*`.

**Clocks.** t0_utc/t1_utc are on dest's clock and the pgbench log is on the client's, so dest-minus-client
(chrony's offsets, EXP-28's shift()) is added to every log timestamp before anything is bucketed. A run whose
offsets are missing is **flagged and left out, never analysed** on the wrong clock.

**Guards and windows are data, not assumptions.** A `v1_fast` run that records `fast_path_used: false` is
reported apart: a cell where the *guard refused* is not evidence about the fast
path's cost. A cell whose window is under 2 s is reported as "too fast to measure" and its W and D are not
judged (prediction 6): fewer than two 1-second buckets cannot support either number.

Reads each session directory's run-*/run.json (the record contract), its run-*/load-log* (or EXP-28's single
session-wide load-log.gz), its run-*/diskstats.txt and its rates.json. Writes
docs/experiments/data/exp88-cells.csv and docs/figures/exp88-slowdown-by-load.svg.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

import exp28
import plotting
from exp28 import FLOOR_D, FLOOR_W, SHARE_W, TAIL, cpu_seconds
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from experiments.network import measure  # noqa: E402

ARMS = ['none', 'none_nosum', 'full', 'v1', 'v1_j4', 'v1_fast']
LEVELS = ['L30', 'L60', 'L80']
LEVEL_LABELS = {'L30': '0.3 of capacity', 'L60': '0.6 (EXP-28\'s)', 'L80': '0.8 of capacity'}
NULL, CONTROL = 'none', 'none_nosum'
PCTS = (50, 95, 99)
KINDS = ('lat', 'lag', 'exec')
MIN_JUDGED_S = 2.0   # prediction 6: under two 1-second buckets, W and D are not judged
RATE_TOLERANCE = 0.01
SECTOR_BYTES = 512
# /proc/diskstats fields after the device name (Documentation/admin-guide/iostats.rst), as analysis/exp68.py reads them.
DISKSTATS_FIELDS = ('reads', 'reads_merged', 'sectors_read', 'read_ms', 'writes', 'writes_merged',
                    'sectors_written', 'write_ms', 'in_flight', 'io_ms')

# The per-cell CSV's columns, in this order. Grouped: the cell, its condition, O4's deciding numbers, then
# everything that is reported and decides nothing.
COLUMNS = [
    'arm', 'level', 'runs', 'flagged_runs', 'judged',
    'summarize_wal', 'share', 'rate_r', 'capacity_c', 'seconds',
    'W', 'W_net', 'D', 'D_net', 'W_excess', 'D_excess', 'delta_W', 'delta_D',
    'meets_x0.5', 'meets_x1', 'meets_x2', 'near',
    'lat_p50_ms', 'lat_p95_ms', 'lat_p99_ms',
    'lat_p50_inc_pct', 'lat_p95_inc_pct', 'lat_p99_inc_pct',
    'lag_p50_ms', 'lag_p95_ms', 'lag_p99_ms',
    'exec_p50_ms', 'exec_p95_ms', 'exec_p99_ms',
    'tps', 'tps_loss_pct', 'txns_ok', 'txns_failed', 'txns_skipped',
    'summarize_wal_p99_ratio',
    'source_cpu_s',
    'source_read_bytes_besides_wal', 'source_write_bytes_besides_wal',
    'dest_read_bytes_besides_wal', 'dest_write_bytes_besides_wal', 'wal_device_separate',
    'fast_path_used', 'fast_path_note', 'notes',
]


# --- the logs -----------------------------------------------------------------------------------------------

def log_lines(path):
    """The lines of one pgbench log: gzip-compressed (EXP-28's load-log.gz), zstd-compressed, or plain."""
    path = Path(path)
    if path.suffix == '.gz':
        with gzip.open(path, 'rt', encoding='ascii', errors='replace') as stream:
            yield from stream
        return
    yield from measure.log_lines(path)


def log_paths(run_dir, root, level=None):
    """The pgbench log files of one run.

    Three shapes, in order: the run directory's own (`load-log*`, or `pgbench_log*` when pgbench -j wrote one
    per thread); then **this level's session-wide log** (`load-log-L60.gz`), which is what lab_exp88.sh writes --
    EXP-88 runs one load process per level, so its log covers every run of that level and each run is cut out of
    it by its own [t0, t1 + tail) window; then EXP-28's single session-wide log.

    The level-keyed name is load-bearing: without it a session would be read with no transactions at all and
    every cell would come out empty rather than erroring, which is the worst way for an analysis to fail.
    """
    own = sorted(p for p in Path(run_dir).glob('load-log*') if p.is_file())
    own = own or sorted(p for p in Path(run_dir).glob('pgbench_log*') if p.is_file())
    if own:
        return own
    if level:
        per_level = [Path(root) / f'load-log-{level}{suffix}' for suffix in ('.gz', '.zst', '')]
        found = [p for p in per_level if p.exists()]
        if found:
            return found
    return [p for p in (Path(root) / 'load-log.gz', Path(root) / 'load-log') if p.exists()]


def transactions(paths, offset):
    """Every logged transaction on dest's clock, read with measure.parse_pgbench_line -- the project's one
    pgbench log parser.

    'ok' holds (scheduled, completed, latency ms, lag ms, execution ms) for each successful transaction, and the
    failed and the skipped are kept apart as their own completion times. pgbench writes `failed` or `skipped` in
    the latency field and the parser returns None for the latency of **both**, so the two are told apart from
    the raw field, not from the parsed value.
    """
    out = {'ok': [], 'failed_at': [], 'skipped_at': [], 'unparsed': 0}
    for path in paths:
        for line in log_lines(path):
            parsed = measure.parse_pgbench_line(line)
            if parsed is None:
                out['unparsed'] += bool(line.strip())
                continue
            _, _, latency_us, _, end_us, lag_us = parsed
            completed = end_us / 1e6 + offset
            if latency_us is None:
                field = line.split()[2]
                out['skipped_at' if field == 'skipped' else 'failed_at'].append(completed)
                continue
            latency_ms, lag_ms = latency_us / 1e3, (lag_us or 0) / 1e3
            out['ok'].append((completed - latency_us / 1e6, completed, latency_ms, lag_ms, latency_ms - lag_ms))
    return out


def work_and_drop(scheduled, completed, t0, t1, rate):
    """EXP-28's W and D (analysis/exp28.py's runs_of), unchanged: 1-second buckets from t0 up to t1 + TAIL,
    W = sum(max(0, S_b - X_b)) / R and D = max(1 - X_b/S_b) over the buckets that scheduled anything, floored
    at 0."""
    n = int(math.floor(t1 + TAIL - t0))
    edges = t0 + np.arange(n + 1)
    s = np.histogram(np.asarray(scheduled, dtype=float), edges)[0]
    x = np.histogram(np.asarray(completed, dtype=float), edges)[0]
    w = float(np.maximum(0, s - x).sum()) / rate
    dmax = float(max(1 - xi / si for si, xi in zip(s, x) if si)) if s.any() else None
    return w, (None if dmax is None else max(dmax, 0.0))


def measure_window(txs, t0, t1, rate):
    """One run's numbers over [t0, t1 + TAIL): O4's W and D, the three sets of percentiles, the completed rate
    and the successful, failed and skipped transactions counted apart.

    The percentiles are over the transactions *completing* inside the window, EXP-28's rule. A failed or skipped
    transaction has no latency, so it enters none of them -- it is only counted.
    """
    end = t1 + TAIL
    ok = txs['ok']
    done = np.array([t[1] for t in ok], dtype=float)
    w, d = work_and_drop([t[0] for t in ok], done, t0, t1, rate)
    inside = (done >= t0) & (done < end)
    out = {'W': w, 'D': d, 'window_s': end - t0, 'txns_ok': int(inside.sum()),
           'txns_failed': sum(1 for u in txs['failed_at'] if t0 <= u < end),
           'txns_skipped': sum(1 for u in txs['skipped_at'] if t0 <= u < end),
           'unparsed_lines': txs['unparsed'],
           'tps': int(inside.sum()) / (end - t0) if end > t0 else None}
    for kind, column in zip(KINDS, (2, 3, 4)):
        sample = np.array([t[column] for t in ok], dtype=float)[inside]
        for p in PCTS:
            out[f'{kind}_p{p}_ms'] = float(np.percentile(sample, p)) if sample.size else None
    return out


# --- the records --------------------------------------------------------------------------------------------

def clock_shift(meta, root):
    """Seconds to add to a client timestamp to get dest's clock, by EXP-28's shift(): the difference between the
    two VMs' chrony offsets from the same reference.

    From the session's offsets-start.json / offsets-end.json pair when the driver wrote EXP-28's, else from the
    run's own `clock_offset_s` (EXP-88's record contract: an object of dest/source/client, or a start/end pair of
    them). None when either end's offset is missing -- the run is then flagged, not analysed.
    """
    root = Path(root)
    if (root / 'offsets-start.json').exists() and (root / 'offsets-end.json').exists():
        try:
            return exp28.shift(root)
        except (statistics.StatisticsError, KeyError, TypeError, ValueError):
            return None
    offsets = meta.get('clock_offset_s')
    readings = []
    if isinstance(offsets, dict):
        readings = [offsets[k] for k in ('start', 'end') if isinstance(offsets.get(k), dict)] or [offsets]
    elif isinstance(offsets, list):
        readings = [o for o in offsets if isinstance(o, dict)]
    pairs = [o['dest'] - o['client'] for o in readings
             if o.get('dest') is not None and o.get('client') is not None]
    return statistics.mean(pairs) if pairs else None


def diskstats(path):
    """{(side, role): {field: after - before}} from a run's diskstats.txt.

    EXP-68's format with the one addition this experiment needs: `before|after  dest|source  [data|wal]` and then
    a /proc/diskstats line. The role is optional and defaults to `data`, so EXP-68's two-word lines read
    unchanged. Only a `wal` line makes "besides WAL" separable: with WAL on the data device the data line's bytes
    include it, and the cell says so (`wal_device_separate`).
    """
    seen = {}
    for line in Path(path).read_text().splitlines():
        words = line.split()
        if len(words) < 3 or words[0] not in ('before', 'after'):
            continue
        when, side, rest = words[0], words[1], words[2:]
        role = 'data'
        if rest and not rest[0].isdigit():
            role, rest = rest[0], rest[1:]
        if len(rest) < 3 + len(DISKSTATS_FIELDS):
            continue
        seen[(when, side, role)] = [int(v) for v in rest[3:3 + len(DISKSTATS_FIELDS)]]
    out = {}
    for when, side, role in sorted(seen):
        if when == 'before' and ('after', side, role) in seen:
            before, after = seen[('before', side, role)], seen[('after', side, role)]
            out[(side, role)] = {f: b - a for f, a, b in zip(DISKSTATS_FIELDS, before, after) if f != 'in_flight'}
    return out


def inline_diskstats(before_text, after_text):
    """{side: {field: after - before}} from lab_exp88.sh's two `<side> <diskstats line>` blobs.

    One device per side (the one behind /data), so there is no role column and nothing to pick between.
    """
    def read(text):
        found = {}
        for line in (text or '').splitlines():
            words = line.split()
            if len(words) < 4 + len(DISKSTATS_FIELDS) or words[0] not in ('dest', 'source'):
                continue
            # words: side, major, minor, device, then the counters
            found[words[0]] = [int(v) for v in words[4:4 + len(DISKSTATS_FIELDS)]]
        return found

    first, second = read(before_text), read(after_text)
    out = {}
    for side in set(first) & set(second):
        out[side] = {f: b - a for f, a, b in zip(DISKSTATS_FIELDS, first[side], second[side])
                     if f != 'in_flight'}
    return out


def disk_bytes(run_dir, meta):
    """Bytes read and written besides WAL on both ends over the run's window, and whether WAL was separable.

    Either from `disk_besides_wal` in run.json when the driver worked it out itself
    ({'source': {'read_bytes', 'write_bytes'}, 'dest': ...}), or from the run's diskstats.txt: the data device's
    own counters, which exclude WAL exactly when WAL has a device of its own.
    """
    out = {f'{side}_{key}_bytes_besides_wal': None for side in ('source', 'dest') for key in ('read', 'write')}
    out['wal_device_separate'] = None
    given = meta.get('disk_besides_wal')
    if isinstance(given, dict):
        for side in ('source', 'dest'):
            counters = given.get(side) or {}
            for key in ('read', 'write'):
                value = counters.get(f'{key}_bytes')
                out[f'{side}_{key}_bytes_besides_wal'] = None if value is None else float(value)
        out['wal_device_separate'] = bool(meta.get('wal_device_separate', True))
        return out
    # lab_exp88.sh's shape: run.json carries the text of disks-before.txt and disks-after.txt, each a
    # `<side> <diskstats line>` per end. It samples only the device behind /data, and these clusters keep pg_wal
    # inside /data, so WAL is NOT separable from the rest -- wal_device_separate is false and the note says the
    # bytes include WAL. Reporting them as "besides WAL" would be a false label on a real measurement.
    if meta.get('disks_before') and meta.get('disks_after'):
        deltas = inline_diskstats(meta['disks_before'], meta['disks_after'])
        for side in ('source', 'dest'):
            fields = deltas.get(side)
            if fields:
                out[f'{side}_read_bytes_besides_wal'] = float(fields['sectors_read'] * SECTOR_BYTES)
                out[f'{side}_write_bytes_besides_wal'] = float(fields['sectors_written'] * SECTOR_BYTES)
        out['wal_device_separate'] = False
        return out
    path = Path(run_dir) / 'diskstats.txt'
    if not path.exists():
        return out
    stats = diskstats(path)
    out['wal_device_separate'] = any(role == 'wal' for _, role in stats)
    for side in ('source', 'dest'):
        data = stats.get((side, 'data'))
        if data:
            out[f'{side}_read_bytes_besides_wal'] = float(data['sectors_read'] * SECTOR_BYTES)
            out[f'{side}_write_bytes_besides_wal'] = float(data['sectors_written'] * SECTOR_BYTES)
    return out


def read_rates(root):
    """The session's rates.json: the capacity runs, C and each level's R. `capacity_c` or `C` for the capacity,
    `levels` or `rates` for the map of level -> R. None when the session wrote none."""
    path = Path(root) / 'rates.json'
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    # lab_exp88.sh writes levels as {level: {share, R}}; a bare {level: R} is also accepted. Normalising here
    # keeps every caller dealing in level -> rate, and stops a dict reaching the arithmetic that compares a
    # run's recorded rate with its level's.
    levels = {}
    for level, value in (data.get('levels') or data.get('rates') or {}).items():
        if isinstance(value, dict):
            rate = value.get('R', value.get('rate', value.get('rate_r')))
        else:
            rate = value
        levels[level] = None if rate is None else float(rate)
    return {'capacity_c': data.get('capacity_c', data.get('C')),
            'levels': levels,
            'capacity_runs': data.get('capacity_runs') or data.get('runs') or []}


def session_roots(path):
    """`path` itself when it holds run-* directories, else its subdirectories that do (one session each), so a
    stage directory of sessions and a single session both work."""
    path = Path(path)
    if not path.is_dir():
        return []
    if any(path.glob('run-*')):
        return [path]
    return [d for d in sorted(path.iterdir()) if d.is_dir() and any(d.glob('run-*'))]


def runs_of(root):
    """Every run under one session directory, with its numbers and the reasons it is flagged.

    A flagged run -- a non-zero exit code, missing chrony offsets, no log, no window or no transactions -- is
    reported and left out of its cell. It is never analysed on a clock we cannot map.
    """
    root = Path(root)
    rates = read_rates(root)
    out = []
    for d in sorted(root.glob('run-*')):
        path = d / 'run.json'
        if not path.exists():
            continue
        meta = json.loads(path.read_text())
        t0, t1, rate = meta.get('t0_utc'), meta.get('t1_utc'), meta.get('rate_r')
        offset, flags = clock_shift(meta, root), []
        paths = log_paths(d, root, meta.get('level'))
        row = {'run_dir': f'{root.name}/{d.name}', 'run_id': meta.get('run_id'), 'arm': meta.get('arm'),
               'level': meta.get('level'), 'repeat': meta.get('repeat'), 'order_index': meta.get('order_index'),
               'summarize_wal': meta.get('summarize_wal'), 'rate_r': rate, 'share': meta.get('share'),
               'capacity_c': meta.get('capacity_c', (rates or {}).get('capacity_c')),
               'exit_code': meta.get('exit_code'), 'fast_path_used': meta.get('fast_path_used'),
               # carried so a refusal (reasons present) can be told from an arm that never asked
               'fast_path_reasons': meta.get('fast_path_reasons'),
               'seconds': meta.get('seconds'), 'clock_shift_s': offset,
               'rate_expected': (rates or {}).get('levels', {}).get(meta.get('level')),
               'source_cpu_s': (cpu_seconds(meta['cpu0'], meta['cpu1']) if meta.get('cpu0') and meta.get('cpu1')
                                else meta.get('source_cpu_s'))}
        if row['seconds'] is None and None not in (t0, t1):
            row['seconds'] = t1 - t0
        row.update(disk_bytes(d, meta))
        if meta.get('exit_code') not in (0, None):
            flags.append(f'exit code {meta["exit_code"]}')
        if offset is None:
            flags.append('no chrony offsets')
        if not paths:
            flags.append('no pgbench log')
        if None in (t0, t1) or not rate:
            flags.append('no window or no rate')
        if not flags:
            row.update(measure_window(transactions(paths, offset), t0, t1, rate))
            if not row['txns_ok']:
                flags.append('no transactions in the window')
        row['short_window'] = None if row['seconds'] is None else row['seconds'] < MIN_JUDGED_S
        row['flags'] = flags
        row['ok'] = not flags
        out.append(row)
    return out


# --- the cells ----------------------------------------------------------------------------------------------

def med(rows, key):
    values = [r[key] for r in rows if r.get(key) is not None]
    return statistics.median(values) if values else None


def o4(runs):
    """O4's two numbers and its verdict, from EXP-28's own cells() (analysis/exp28.py), called once per level.

    One call per level is what makes the null per level: cells() nets every arm against the `none` runs it was
    given, and it is given one level's runs at a time, so an L80 arm can only ever be netted against L80's
    `none`. EXP-28's floors, its half and twice readings and its `near` flag therefore come from EXP-28's code
    rather than from a copy of it here. {(arm, level): EXP-28's row}.
    """
    out = {}
    levels = LEVELS + sorted({r['level'] for r in runs} - set(LEVELS) - {None})
    for level in levels:
        shim = [{'arm': r['arm'], 'speed': exp28.SPEEDS[0], 'ok': True, 'W': r.get('W'), 'D': r.get('D'),
                 'seconds': r.get('seconds'), 'p95_ms': r.get('lat_p95_ms'), 'p99_ms': r.get('lat_p99_ms'),
                 'cpu_s': r.get('source_cpu_s'), 'sender_cpu_s': None}
                for r in runs if r['level'] == level and r.get('W') is not None]
        if not shim:
            continue
        for row in exp28.cells(shim):
            out[(row['arm'], level)] = row
    return out


def increase(during, without):
    """The registered increase of a reported percentile: (p during backup / p without backup - 1) x 100."""
    if during is None or not without:
        return None
    return (during / without - 1) * 100


def cells(runs):
    """One row per (arm, level): the medians over the cell's counted runs, O4's verdict from EXP-28's cells(),
    and every reported number against the same level's `none` cell. COLUMNS gives the order."""
    by = defaultdict(list)
    for r in runs:
        by[(r['arm'], r['level'])].append(r)
    verdicts = o4([r for r in runs if r['ok']])
    levels = LEVELS + sorted({level for _, level in by} - set(LEVELS) - {None})
    arms = ARMS + sorted({arm for arm, _ in by} - set(ARMS) - {None})
    medians = ('share', 'rate_r', 'capacity_c', 'seconds', 'tps', 'txns_ok', 'txns_failed', 'txns_skipped',
               'source_cpu_s', 'source_read_bytes_besides_wal', 'source_write_bytes_besides_wal',
               'dest_read_bytes_besides_wal', 'dest_write_bytes_besides_wal')
    rows = []
    for level in levels:
        null = [r for r in by.get((NULL, level), []) if r['ok']]
        for arm in arms:
            every = by.get((arm, level), [])
            counted = [r for r in every if r['ok']]
            if not every:
                continue
            notes = []
            row = {'arm': arm, 'level': level, 'runs': len(counted), 'flagged_runs': len(every) - len(counted)}
            for key in medians:
                row[key] = med(counted, key)
            for kind in KINDS:
                for p in PCTS:
                    row[f'{kind}_p{p}_ms'] = med(counted, f'{kind}_p{p}_ms')
            for key in ('summarize_wal', 'wal_device_separate'):
                seen = sorted({str(r[key]) for r in counted if r.get(key) is not None})
                row[key] = seen[0] if len(seen) == 1 else (','.join(seen) if seen else None)
            # Reported against the matched `none` cell at the same level, never against another level's.
            for p in PCTS:
                row[f'lat_p{p}_inc_pct'] = increase(row[f'lat_p{p}_ms'], med(null, f'lat_p{p}_ms'))
            baseline = med(null, 'tps')
            row['tps_loss_pct'] = (None if row['tps'] is None or not baseline
                                   else (1 - row['tps'] / baseline) * 100)
            if arm == CONTROL:  # EXP-83d's direction, on ÷ off: `none` has summarize_wal on, `none_nosum` off.
                on, off = med(null, 'lat_p99_ms'), row['lat_p99_ms']
                row['summarize_wal_p99_ratio'] = on / off if on is not None and off else None
            if counted and not null:
                notes.append(f'no `none` runs at {level}: nothing is netted')
            verdict = verdicts.get((arm, level)) or {}
            for key in ('W', 'W_net', 'D', 'D_net', 'W_excess', 'D_excess', 'delta_W', 'delta_D',
                        'meets_x0.5', 'meets_x1', 'meets_x2', 'near'):
                if key in verdict:
                    row[key] = verdict[key]
            if arm == CONTROL:  # a control, not a competitor: compared with `none`, never judged against `full`.
                for key in ('W_excess', 'D_excess', 'delta_W', 'delta_D', 'meets_x0.5', 'meets_x1', 'meets_x2',
                            'near'):
                    row.pop(key, None)
                notes.append('a control, not a competitor: compared with `none` at this level, not with `full`')
            # Prediction 6: fewer than two 1-second buckets cannot support W or D.
            row['judged'] = bool(counted) and (row['seconds'] or 0) >= MIN_JUDGED_S
            if counted and not row['judged']:
                notes.append(f'too fast to measure: the window is {row["seconds"]:.2f} s, under '
                             f'{MIN_JUDGED_S:g} s, so W and D are not judged (prediction 6)')
                for key in ('meets_x0.5', 'meets_x1', 'meets_x2', 'near'):
                    if key in row:
                        row[key] = None
            used = [r['fast_path_used'] for r in counted]
            row['fast_path_used'] = ('' if all(u is None for u in used) else
                                     'true' if all(u is True for u in used) else
                                     'false' if all(u is False for u in used) else 'mixed')
            # "Did not activate" has two causes that must not be run together. An arm that never passed
            # --fast-path has nothing for a guard to refuse, and calling that a refusal would invent a
            # finding: the smoke run's v1 and v1_j4 arms both recorded false with no reasons, simply
            # because the fast path was never asked for. It is a refusal only when the sender said why.
            refused = [r for r in counted
                       if r['fast_path_used'] is False and r.get('fast_path_reasons')]
            not_asked = [r for r in counted
                         if r['fast_path_used'] is False and not r.get('fast_path_reasons')]
            if refused:
                why = sorted({str(r['fast_path_reasons']) for r in refused})[0][:120]
                row['fast_path_note'] = (
                    f'the guard refused the fast path in {len(refused)} of {len(used)} counted runs '
                    f'({why}), so this cell is not evidence about the fast path\'s cost')
            elif not_asked and len(not_asked) == len(used):
                row['fast_path_note'] = 'the fast path was not requested in this arm'
            if row['txns_failed'] or row['txns_skipped']:
                notes.append(f'{row["txns_failed"]:g} failed and {row["txns_skipped"]:g} skipped transactions '
                             f'(median per run): W and D count the successful ones, so they understate the loss '
                             f'(prediction 8)')
            if row['wal_device_separate'] == 'False':
                notes.append('WAL shares the data device, so the bytes reported are not besides WAL')
            off = [r for r in counted if r.get('rate_expected')
                   and abs(r['rate_r'] / r['rate_expected'] - 1) > RATE_TOLERANCE]
            if off:
                notes.append(f'{len(off)} run(s) ran at a rate rates.json does not give for {level}')
            flagged = [r for r in every if not r['ok']]
            if flagged:
                notes.append('; '.join(f'{r["run_dir"]} left out ({", ".join(r["flags"])})' for r in flagged))
            row['notes'] = ' | '.join(notes)
            rows.append(row)
    return rows


# --- the output ---------------------------------------------------------------------------------------------

def table(rows):
    """The printed summary: the verdict first, then what is only reported."""
    fmt = lambda v, p=3: '-' if v is None else (f'{v:.{p}f}' if isinstance(v, float) else str(v))
    print('arm, level: runs | time s | W s (net) | D (net) | excess over full W, D | meets O4 | p50/p95/p99 ms '
          '(latency, scheduling delay included) | its increase % | lag p50/p95/p99 ms | exec p99 ms | tps '
          '(loss %) | ok/failed/skipped | source CPU-s')
    for r in rows:
        line = (f'  {r["arm"]:10s} {r["level"]:4s} {r["runs"]}'
                f' | {fmt(r.get("seconds"), 2)} | {fmt(r.get("W"))} ({fmt(r.get("W_net"))})'
                f' | {fmt(r.get("D"))} ({fmt(r.get("D_net"))})')
        if r.get('W_excess') is not None:
            line += f' | {r["W_excess"]:+.3f}, {r["D_excess"]:+.3f}'
            line += (' | not judged' if not r['judged'] else
                     f' | x0.5 {r["meets_x0.5"]}, x1 {r["meets_x1"]}, x2 {r["meets_x2"]}'
                     + (' (near the line)' if r['near'] else ''))
        else:
            line += ' | - | -'
        line += (f' | {fmt(r.get("lat_p50_ms"), 1)}/{fmt(r.get("lat_p95_ms"), 1)}/{fmt(r.get("lat_p99_ms"), 1)}'
                 f' | {fmt(r.get("lat_p50_inc_pct"), 1)}/{fmt(r.get("lat_p95_inc_pct"), 1)}/'
                 f'{fmt(r.get("lat_p99_inc_pct"), 1)}'
                 f' | {fmt(r.get("lag_p50_ms"), 1)}/{fmt(r.get("lag_p95_ms"), 1)}/{fmt(r.get("lag_p99_ms"), 1)}'
                 f' | {fmt(r.get("exec_p99_ms"), 1)}'
                 f' | {fmt(r.get("tps"), 1)} ({fmt(r.get("tps_loss_pct"), 1)})'
                 f' | {fmt(r.get("txns_ok"), 0)}/{fmt(r.get("txns_failed"), 0)}/{fmt(r.get("txns_skipped"), 0)}'
                 f' | {fmt(r.get("source_cpu_s"), 1)}')
        print(line)
        if r.get('summarize_wal_p99_ratio') is not None:
            print(f'      summarize_wal p99 on / off at {r["level"]}: '
                  f'{r["summarize_wal_p99_ratio"]:.3f} (EXP-83d: [1.10, 1.35] beyond memory)')
        if r.get('fast_path_note'):
            print(f'      {r["fast_path_note"]}')
        if r.get('notes'):
            print(f'      {r["notes"]}')


def write_csv(rows, path):
    keys = COLUMNS + sorted({k for r in rows for k in r} - set(COLUMNS))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=keys, lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)
    return path


def figure(rows, tag=''):
    """EXP-28's figure, keyed by level instead of speed: the two deciding numbers per arm with one bar per load
    level and pg_basebackup's bar plus its tolerance dashed, then refresh time against the load's loss."""
    colors = [COLORS['blue'], COLORS['orange'], COLORS['green']]
    levels = [level for level in LEVELS if any(r['level'] == level for r in rows)]
    arms = [arm for arm in ARMS if arm != NULL and any(r['arm'] == arm for r in rows)]
    xs = np.arange(len(arms))
    width = 0.8 / max(len(levels), 1)
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.4))
    for ax, key, title, floor in ((axes[0], 'W_net', 'Work lost beyond load-only time (s)', FLOOR_W),
                                  (axes[1], 'D_net', 'Worst one-second drop beyond load-only time', FLOOR_D)):
        for j, level in enumerate(levels):
            pick = lambda arm: next((r.get(key) for r in rows if r['arm'] == arm and r['level'] == level), None)
            values = [pick(arm) for arm in arms]
            ax.bar(xs + (j - (len(levels) - 1) / 2) * width, [v or 0 for v in values], width,
                   color=colors[j % len(colors)], label=LEVEL_LABELS.get(level, level))
            full = pick('full')
            if full is not None:
                bar = full + (max(floor, SHARE_W * full) if key == 'W_net' else floor)
                ax.hlines(bar, -0.5, len(arms) - 0.5, color=colors[j % len(colors)], ls='--', lw=0.9)
        ax.set_xticks(xs, ['pg_base-\nbackup' if a == 'full' else a.replace('_', '\n') for a in arms], fontsize=8.5)
        ax.set_title(title + '\n(dashed: O4\'s bar, pg_basebackup plus its tolerance)', fontsize=9.5)
    axes[0].legend(fontsize=8)
    for j, level in enumerate(levels):
        points = [(r['seconds'], r.get('W_net') or 0, r['arm']) for r in rows
                  if r['level'] == level and r['arm'] != NULL and r.get('seconds') is not None]
        axes[2].scatter([p[0] for p in points], [p[1] for p in points], color=colors[j % len(colors)], zorder=3,
                        label=LEVEL_LABELS.get(level, level))
        for p in points:
            axes[2].annotate(p[2].replace('full', 'pg_basebackup'), (p[0], p[1]), textcoords='offset points',
                             xytext=(4, 3), fontsize=7.5)
    axes[2].legend(fontsize=8)
    axes[2].set_xlabel('refresh time (s)')
    axes[2].set_ylabel('work lost beyond load-only time (s)')
    axes[2].set_title('Time against the load\'s loss', fontsize=9.5)
    fig.tight_layout()
    return plotting.save(fig, f'exp88-slowdown-by-load{tag}')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    base = ROOT / 'results' / 'exp88'
    ap.add_argument('--runs', nargs='+', default=[str(base / s) for s in ('stage1', 'stage2', 'stage3')],
                    help='session directories, or directories of them')
    ap.add_argument('--figdir')
    ap.add_argument('--datadir', default=str(ROOT / 'docs' / 'experiments' / 'data'))
    ap.add_argument('--tag', default='')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    runs = []
    for given in a.runs:
        for root in session_roots(given):
            runs += runs_of(root)
            rates = read_rates(root)
            if rates:
                print(f'{root.name}: capacity C {rates["capacity_c"]} over {len(rates["capacity_runs"])} runs, '
                      f'levels {rates["levels"]}')
    if not runs:
        print(f'no runs under {a.runs}')
        return []
    flagged = [r for r in runs if not r['ok']]
    print(f'{len(runs)} runs, {len(runs) - len(flagged)} counted'
          + (f'; flagged: {[(r["run_dir"], r["arm"], r["flags"]) for r in flagged]}' if flagged else ''))
    rows = cells(runs)
    table(rows)
    print(write_csv(rows, Path(a.datadir) / f'exp88-cells{a.tag}.csv'))
    print(figure(rows, a.tag))
    return rows


if __name__ == '__main__':
    main()
