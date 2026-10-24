"""EXP-12 (T10a: how much each refresh slows a busy source, and is every output correct under load), as pre-registered.

    .venv/bin/python analysis/exp12.py [--sweeps results/exp04/exp12-hot] [--late-limit-ms 10]
        [--null-resamples 10000] [--null-seed 121] [--client-wait-share 0.25] [--capacity-tps C]
        [--busy-cpu SHARE] [--idle-noise-bytes BYTES] [--tag ...] [--datadir DIR] [--figdir DIR]

Reads each sweep as `run_exp04 --load bank` lays it out and `remote.py fetch` brings it back: the run records
(runs/<trial>/r<repeat>-<speed>-<method>.json, in summary.csv's order), the trial records, run.json, and the load's
logs (logs/<trial>/load/): pgbench's per-transaction logs (zstd-compressed or not), and the sampler's ticks and SQL
lines. Each logged pgbench run is read once, CHUNK_LINES lines at a time, and every transaction goes to the stretches
of the source's clock that need it: each run's window (from 10 s before t0 to 15 s after t1), its settle, every
`none` span and the warm-ups' last minute.

Writes the curated data docs/experiments/data/exp12-runs.csv (every run, with what its record's `slowdown` gives
measure.summary_row) and exp12-cells.csv (every cell in every view), the figures docs/figures/exp12-timeseries.svg
and exp12-loss.svg, and prints every check, prediction and rule of docs/experiments/EXP-12-source-slowdown.md with
its verdict, in its order of judgment: G4 and R3; A1; A13; the instrument and the load (A2, A3, the clock gate);
L1-L5; R1; R2; decision 1, with A4 and the warm reading inside it; decision 2; A5, A6; the rows beside; the flags;
then E1-E16, A7-A12, the source's CPU, the sampler's mechanism view, the order effects and the smoke run's sixteen
items, each from the records.

O4's two numbers, per run, from the log on the source's clock: the window [t0, t1 + 10 s] in T 1-s buckets from t0
(T = ceil(t1 + 10 - t0)); S_b and X_b the transactions scheduled and completed in bucket b; D = max(1 - X_b / S_b);
W = sum(max(0, S_b - X_b)) / R. The null is a block bootstrap of the block's load-only time (every `none` span from
its settle's start to its tail's end, every settle whose gate passed, the warm-ups' last 60 s), in blocks of 10
consecutive buckets drawn with replacement, each from inside one span, joined to each run's own T; a cell's null is
the median (or, in the maxima view, the maximum) of one window per run, `--null-resamples` times from
`--null-seed`. Net values, "measurable", the bound and the tolerances follow the pre-registration's definitions;
every robustness view uses the null of its own statistic.

How this script reads what the text leaves open (recorded in the write-up before the sweep):
- A window's last bucket ends at t0 + T, so it can run up to 1 s past t1 + 10 s; the 15-s view's at t0 + T15, up to
  1 s past t1 + 15 s, and the harness's quiet tail waits that out (its view_end_utc). The robustness views' D at 0.5-s
  and 2-s buckets and the null of them use blocks of 10 s too (20 and 5 buckets); a 2-s bucket at a window's odd end
  holds its last second alone in the run, and its null draws whole 2-s buckets.
- Latency percentiles are nearest-rank over every logged transaction; a transaction's lateness is its latency from
  its schedule. The settle gate's log part compares the settle's p99 with its load run's warm-up's last minute.
- The controls' cells (`pause2`, `burn4`) are judged with the 2 runs per block they are registered with; every other
  cell needs 3.
- A4 that can't be decided (a counter or a calibration missing) is "undecided": the proxy's judged misses still
  count, its meets don't, so it can't hide a miss or make a meet.
- Row 7b's δ for the warm cell against the cold one uses the larger of the warm and the cold `full`'s W_net in its
  0.15 x W_net(full) term. The paired view's net value is the median paired difference minus its null's median,
  unclipped, against δ from that null.
- A run's own net (`work_lost_net_s`, `worst_drop_net`) is its W or D minus the median of one null window at its
  own T, clipped at 0; a cell's is the pre-registration's.
- Decision 1's rows are checked strictly in order, so row 5 (nothing measurable) comes before row 7 even where a
  difference exceeds δ. A window whose log holds less than half of its rate's schedule has no numbers (it would read
  as a window without loss). A2 fires on a pause2 run that fails a check it could make; a block without one says so.
- A1's coverage control pairs outputs taken under the load only: without it a copy lacks no commit, its cut is open
  after its last held one, and any later window would cover it.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import statistics
import sys
import zlib
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from experiments.network import measure  # noqa: E402

DATA = ROOT / 'docs' / 'experiments' / 'data'
SWEEPS = [ROOT / 'results' / 'exp04' / 'exp12-hot']
U = measure.UNTHROTTLED
SPEEDS = ['0.5g', '2g', U]
REGISTERED = ('0.5g', '2g')  # the registered speeds; unthrottled is reported beside
SPEED_LABEL = {'0.5g': '0.5 Gbit/s', '2g': '2 Gbit/s', U: 'unthrottled', None: 'any link'}
WARM_SPEED = '2g'
V1 = 'pgdelta_direct_inplace'
PROXY, PROXY_S1 = measure.V1PROXY_METHODS
LIVE = measure.G4_METHODS  # full and staged pgdelta: the live outputs G4 checks
OUTPUT_ARMS = measure.TOOL_ABORT_WRONG_METHODS  # the arms with an output: a copy their tool aborted is wrong
CONTROLS = measure.CONTROL_METHODS
LOADED_BLOCKS = ('busy', 'warm', 'moderate')
BLOCKS = (*LOADED_BLOCKS, 'calibration')
POOL_OF = {'busy': 'busy', 'warm': 'busy', 'moderate': 'moderate'}  # the warm block shares the busy block's null
ARMS = [('none', 'none: no backup (the null)', COLORS['black'], 'o'),
        ('full', "full: pg_basebackup, one stream (O4's bar)", COLORS['grey'], 'o'),
        ('capture', 'capture: the staged capture alone', COLORS['orange'], 's'),
        ('pgdelta', 'pgdelta: staged v0', COLORS['red'], 's'),
        ('bracket', 'bracket: backup mode alone', COLORS['purple'], 'v'),
        (PROXY, "v1proxy: v1's source side, 4 workers", COLORS['green'], 'D'),
        (PROXY_S1, 'v1proxy_s1: one source worker', COLORS['sky'], 'D'),
        ('pause2', 'pause2: 2.0 s pause (positive control)', COLORS['blue'], 'x'),
        ('burn4', 'burn4: 4 busy processes (CPU reference)', COLORS['yellow'], '^')]
ARM_NAMES = [arm for arm, *_ in ARMS]
ARM_COLOR = {arm: color for arm, _, color, _ in ARMS}
CALIBRATION_ARMS = ('full', 'capture', 'pgdelta', PROXY, PROXY_S1, V1, 'burn4')

# O4's two numbers (The two O4 numbers): 1-s buckets from t0, the window to 10 s after t1 (15 s in a view).
TAIL_S, TAIL15_S = 10, 15
PRE_S = 10  # what each window keeps before t0 (the time series figure)
HALF_US, SECOND_US = 500_000, 1_000_000
BLOCK_S = 10  # the null's blocks: 10 consecutive 1-s buckets
FLOOR_W, FLOOR_D = 0.05, 0.03  # the tolerances' floors, fixed before running
FULL_SHARE = 0.15  # δ_W's third term: 0.15 x W_net(full)
PERCENTILE = 95
MIN_RUNS, MIN_CONTROL_RUNS = 3, 2  # a cell with fewer isn't judged (Short cells)
NEAR = 0.25  # near the line: a deciding difference within ±0.25 x δ of its δ
RATIO_NEAR = 0.15  # or a rate ratio within ±0.15 of 1.5 or 1/1.5
TRACK = 1.5  # R2: the rate ratio within [1/1.5, 1.5]
REGISTERED_PROJECTION = (5.3, 6.0, 1.6, 1.9)  # R2's consequence: v1's CPU-s (EXP-09) over full's
# A8's flags and the settle gate's log part.
CLOCK_GATE_S = 0.005
STEAL_RATE = 0.02  # CPU-s per s in the window
CLIENT_CORE = 0.9  # a pgbench thread above 90% of a core in the settle
SETTLE_P99_FACTOR, SETTLE_LATE_SHARE = 2.0, 0.001
GATE_WITHIN_S = 30  # E2 and smoke item 10: the settle gate passes within 30 s
CACHE_RESIDUAL = 0.01  # smoke item 8: under 1% of the static files resident after the eviction
# A2 (the instrument), A3 (overload).
A2_HIGH, A2_D, A2_MARGIN_S = 2.3, 0.9, 0.005
A3_RATIO = 0.98
A4_CPU = 0.20  # A4 (iii): calibrated CPU-s within 20% of its reference
D48 = 1.06  # 4 processes use 1.06 times the CPU of 1
A12_DRIFT = 0.10
LATENCY_LIMITS_MS = (10, 50, 100)
CHUNK_LINES = 1 << 20
USER_HZ = 100  # /proc/stat's ticks per second, on every Linux
LOG_COVERAGE = 0.5  # a window whose log holds less than half its rate's schedule has no numbers
GROUPS = ('pgbench', 'backend', 'walsender', 'checkpointer', 'pg_basebackup', 'pgdelta', 'burn')
STAGED = ('pgdelta', 'capture')  # the order effects: runs after an arm that wrote and removed S on the source
D1_ROWS = ('1. Stop', '2. R1', '3. R2', '4. Proxy not judged', '5. No measurable slowdown', '6. v1 meets',
           '7. v1 misses')
D1_FOLLOWS = {
    D1_ROWS[0]: 'Speed work stops until the cause is known (registered)',
    D1_ROWS[1]: 'Slowdown is reported next to time, and T5 moves ahead of T8 (registered; change 4)',
    D1_ROWS[2]: 'T5 moves ahead of T8 (registered)',
    D1_ROWS[3]: "The order waits for v1's own arm; T5 stays behind T8 (registered)",
    D1_ROWS[4]: "T5 stays behind T8. At this load neither copy's slowdown is measurable; D04's gap closes for this "
                'load; D81 gets no basis from it',
    D1_ROWS[5]: "By proxy, v1's source side meets O4's bar at this load. T5 stays behind T8, and T8 confirms with v1's "
                'own arm',
    D1_ROWS[6]: 'Decision 2 says what else'}
CAUSES = {'7a': ('Hashing', 'T5 moves ahead of T8: a faster hash per core shortens the hashing that slows the load'),
          '7b': ('The cold read', 'The cold read slows the load: its I/O and its kernel CPU. T5 keeps its place; a '
                                  "read-rate limit on v1's sender (D81), which T10b tests"),
          '7c': ('Cause not shown', 'T5 moves ahead of T8, as for 7a, with "cause not shown"; a read-rate limit is '
                                    'listed beside it')}
D2_ROWS = ('No cap', 'Cap fails', 'Cap meets', 'Cap not judged')
D2_FOLLOWS = {
    'No cap': 'No cap is needed at 4 source workers at this load. T10b keeps its place',
    'Cap fails': "One source worker slows a busy primary more than pg_basebackup too. v1 needs a rate limit on its "
                 "source work (D81) before T8's speed claims",
    'Cap meets': 'v1 at 4 source workers slows a busy primary more than pg_basebackup; one source core does not. v1 '
                 'needs a source worker cap on busy primaries. T10b moves ahead of T8, as "source workers by load"',
    'Cap not judged': 'v1 at 4 source workers slows a busy primary more than pg_basebackup; whether a cap fixes it '
                      'is not known. T10b moves ahead of T8, to find out'}
D2_LIMIT = {'7a': 'on hashing', '7b': 'on reading', '7c': 'on both'}
# The robustness views (A9), each with the null of its own statistic.
VIEWS = {'main': {'W': 'W', 'D': 'D', 'reduce': 'median', 'pool': 'main', 'clean': False, 'paired': False},
         'max': {'W': 'W', 'D': 'D', 'reduce': 'max', 'pool': 'main', 'clean': False, 'paired': False},
         'paired': {'W': 'W', 'D': 'D', 'reduce': 'median', 'pool': 'main', 'clean': False, 'paired': True},
         'd05': {'W': 'W', 'D': 'D05', 'reduce': 'median', 'pool': 'main', 'clean': False, 'paired': False},
         'd2': {'W': 'W', 'D': 'D2', 'reduce': 'median', 'pool': 'main', 'clean': False, 'paired': False},
         'late_L': {'W': 'late_L', 'D': 'D', 'reduce': 'median', 'pool': 'main', 'clean': False, 'paired': False},
         'late_2L': {'W': 'late_2L', 'D': 'D', 'reduce': 'median', 'pool': 'main', 'clean': False, 'paired': False},
         'tail15': {'W': 'W15', 'D': 'D15', 'reduce': 'median', 'pool': 'main', 'clean': False, 'paired': False},
         'clean': {'W': 'W', 'D': 'D', 'reduce': 'median', 'pool': 'main', 'clean': True, 'paired': False},
         'unsettled': {'W': 'W', 'D': 'D', 'reduce': 'median', 'pool': 'unsettled', 'clean': False,
                       'paired': False}}
VIEW_LABEL = {'main': 'medians', 'max': 'maxima', 'paired': "paired with the same repeat's full run",
              'd05': 'D in 0.5-s buckets', 'd2': 'D in 2-s buckets', 'late_L': 'W as the late count at L',
              'late_2L': 'W as the late count at 2L', 'tail15': 'a 15-s tail', 'clean': 'flagged runs left out',
              'unsettled': 'the null with the unsettled settles put back'}
ROBUSTNESS = tuple(view for view in VIEWS if view != 'main')
# The null's statistics: (grid width in seconds, how buckets combine).
NULL_STATS = {'W': (1.0, 'sum'), 'D': (1.0, 'max'), 'late_L': (1.0, 'sum'), 'late_2L': (1.0, 'sum'),
              'D05': (0.5, 'max'), 'D2': (2.0, 'max')}
RUN_FLAGS = ('unsettled', 'checkpoint', 'steal', 'cache', 'client', 'client_wait_w', 'client_wait_d', 'autovacuum')
# Arms that ask for a checkpoint of their own (backup mode with a fast checkpoint); the others ask for none.
CHECKPOINT_ARMS = ('full', 'capture', 'pgdelta', 'bracket', PROXY, PROXY_S1)


# --- small helpers ------------------------------------------------------------------

def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def mx(values):
    values = [v for v in values if v is not None]
    return max(values) if values else None


def ratio(a, b):
    return None if a is None or not b else a / b


def fmt(value, digits=2):
    return '–' if value is None else f'{value:.{digits}f}'


def word(ok):
    return 'no data' if ok is None else 'held' if ok else 'failed'


def within(value, lo, hi):
    return None if value is None else lo <= value <= hi


def every(oks):
    """True if every judged item holds, False if one fails, None if nothing was judged."""
    oks = [ok for ok in oks if ok is not None]
    return all(oks) if oks else None


def yes(value):
    return '–' if value is None else 'yes' if value else 'no'


def meet_word(meets):
    return 'not judged' if meets is None else 'meets' if meets else 'misses'


def speed_name(speed):
    return SPEED_LABEL.get(speed, speed)


def nearest_rank(values, q):
    """The q-th percentile of an array, nearest-rank (the smallest value with at least q% at or below it)."""
    values = np.asarray(values)
    if values.size == 0:
        return None
    return float(np.percentile(values, q, method='inverted_cdf'))


def utc(text):
    """Seconds since the epoch of an ISO time (utc_now()'s) or a number; None otherwise."""
    if isinstance(text, (int, float)):
        return float(text)
    try:
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None


def get(value, *keys):
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


# --- the load's logs -------------------------------------------------------------------

def parse_chunk(lines):
    """pgbench -l lines as arrays (client, latency us, script, end us, lag us), with the failed transactions' count.

    The fast path reads every line at once (numpy); a chunk that doesn't read that way (a failed transaction, whose
    latency is the word 'failed', or a line of another shape) is read line by line with measure.parse_pgbench_line.
    Without -R (a capacity run) the lag is 0."""
    empty = np.zeros(0, np.int64)
    if not lines:
        return {'client': empty, 'latency': empty, 'script': empty, 'end': empty, 'lag': empty, 'failed': 0,
                'unparsed': 0}
    try:
        values = np.loadtxt(lines, dtype=np.int64, ndmin=2)
        if values.shape[1] not in (6, 7):
            raise ValueError(f'{values.shape[1]} columns')
        lag = values[:, 6] if values.shape[1] == 7 else np.zeros(len(values), np.int64)
        return {'client': values[:, 0], 'latency': values[:, 2], 'script': values[:, 3],
                'end': values[:, 4] * SECOND_US + values[:, 5], 'lag': lag, 'failed': 0, 'unparsed': 0}
    except ValueError:
        pass
    rows, failed, unparsed = [], 0, 0
    for line in lines:
        parsed = measure.parse_pgbench_line(line)
        if parsed is None:
            unparsed += bool(line.strip())
        elif parsed[2] is None:
            failed += 1
        else:
            rows.append((parsed[0], parsed[2], parsed[3], parsed[4], parsed[5] or 0))
    values = np.array(rows, np.int64).reshape(-1, 5)
    return {'client': values[:, 0], 'latency': values[:, 1], 'script': values[:, 2], 'end': values[:, 3],
            'lag': values[:, 4], 'failed': failed, 'unparsed': unparsed}


class Span:
    """A stretch of the source's clock the logs are read for (whole microseconds since the epoch).

    It keeps, per half second from its start, the transactions scheduled in it, those completed in it, and those
    scheduled in it that ended later than each late limit; and the latencies of the transactions scheduled in it,
    per second from its start (seconds=True) or all together. `core` (a run's window, t0 to t1) keeps those
    scheduled inside it apart; `ends` keeps the completion times (pause2's gap)."""

    def __init__(self, key, run_id, start_s, stop_s, limits_us, *, seconds=False, core=None, ends=False):
        self.key, self.run_id = key, run_id
        self.start, self.stop = int(round(start_s * SECOND_US)), int(round(stop_s * SECOND_US))
        self.halves = max(0, -(-(self.stop - self.start) // HALF_US))
        self.limits = list(limits_us)
        self.scheduled = np.zeros(self.halves, np.int64)
        self.completed = np.zeros(self.halves, np.int64)
        self.late = np.zeros((len(self.limits), self.halves), np.int64)
        self.seconds = seconds
        self.core = None if core is None else (int(round(core[0] * SECOND_US)), int(round(core[1] * SECOND_US)))
        self._latency, self._core, self._ends = [], [], [] if ends else None
        self.failed = 0
        self.latency = self.core_latency = self.by_second = self.ends = None

    def feed(self, chunk, scheduled):
        """One parsed chunk (parse_chunk's), its schedule (the completion minus the latency) given."""
        if self.halves == 0:
            return
        end, latency = chunk['end'], chunk['latency']
        mask = (scheduled >= self.start) & (scheduled < self.stop)
        offsets = scheduled[mask] - self.start
        halves = offsets // HALF_US
        self.scheduled += np.bincount(halves, minlength=self.halves)[:self.halves]
        lat = latency[mask]
        for index, limit in enumerate(self.limits):
            self.late[index] += np.bincount(halves[lat > limit], minlength=self.halves)[:self.halves]
        done = (end >= self.start) & (end < self.stop)
        self.completed += np.bincount((end[done] - self.start) // HALF_US, minlength=self.halves)[:self.halves]
        if self.seconds:
            self._latency.append((offsets // SECOND_US, lat.astype(np.int32)))
        else:
            self._latency.append((None, lat.astype(np.int32)))
        if self.core is not None:
            inside = (scheduled[mask] >= self.core[0]) & (scheduled[mask] < self.core[1])
            self._core.append(lat[inside].astype(np.int32))
        if self._ends is not None:
            self._ends.append(end[done])

    def finish(self):
        """Joins what the chunks left: the latencies (per second where asked), the core's, the completion times."""
        parts = [lat for _, lat in self._latency]
        self.latency = np.concatenate(parts) if parts else np.zeros(0, np.int32)
        if self.seconds:
            count = -(-(self.stop - self.start) // SECOND_US)
            index = np.concatenate([sec for sec, _ in self._latency]) if parts else np.zeros(0, np.int64)
            order = np.argsort(index, kind='stable')
            bounds = np.searchsorted(index[order], np.arange(count + 1))
            ordered = self.latency[order]
            self.by_second = [ordered[bounds[i]:bounds[i + 1]] for i in range(count)]
        if self.core is not None:
            self.core_latency = np.concatenate(self._core) if self._core else np.zeros(0, np.int32)
        if self._ends is not None:
            self.ends = np.sort(np.concatenate(self._ends)) if self._ends else np.zeros(0, np.int64)
        self._latency, self._core, self._ends = [], [], None


def log_tally():
    return {'lines': 0, 'commits': 0, 'aborts': 0, 'failed': 0, 'unparsed': 0, 'lag_over_latency': 0,
            'not_rising': 0, 'first_end_us': None, 'last_end_us': None, 'bytes': 0, 'files': 0, 'missing': []}


def stream_run(paths, spans, chunk_lines=CHUNK_LINES):
    """One logged pgbench run, its thread files one after another, each transaction fed to every span it overlaps.

    Returns smoke item 1's tally, from every line: lines, commits and aborts, failures, lines whose latency is under
    their schedule lag (latency is from the schedule, so never), and completion times that fall within a client."""
    tally = log_tally()
    spans = sorted(spans, key=lambda span: span.start)
    for path in paths:
        if not path.exists():
            tally['missing'].append(path.name)
            continue
        tally['files'] += 1
        tally['bytes'] += path.stat().st_size
        last: dict = {}
        lines = measure.log_lines(path)
        while True:
            block = list(itertools.islice(lines, chunk_lines))
            if not block:
                break
            chunk = parse_chunk(block)
            tally['lines'] += len(block)
            tally['failed'] += chunk['failed']
            tally['unparsed'] += chunk['unparsed']
            if not len(chunk['end']):
                continue
            end, latency, client = chunk['end'], chunk['latency'], chunk['client']
            tally['commits'] += int(np.sum(chunk['script'] == measure.COMMIT_SCRIPT))
            tally['aborts'] += int(np.sum(chunk['script'] == measure.ABORT_SCRIPT))
            tally['lag_over_latency'] += int(np.sum(chunk['lag'] > latency))
            low, high = int(end.min()), int(end.max())
            tally['first_end_us'] = low if tally['first_end_us'] is None else min(tally['first_end_us'], low)
            tally['last_end_us'] = high if tally['last_end_us'] is None else max(tally['last_end_us'], high)
            # Completion times rise within each client (its lines in file order).
            order = np.argsort(client, kind='stable')
            ordered, clients = end[order], client[order]
            same = clients[1:] == clients[:-1]
            tally['not_rising'] += int(np.sum(same & (np.diff(ordered) < 0)))
            firsts = np.flatnonzero(np.concatenate(([True], ~same)))
            for index in firsts:
                key = int(clients[index])
                if key in last and ordered[index] < last[key]:
                    tally['not_rising'] += 1
            lasts = np.concatenate((firsts[1:] - 1, [len(ordered) - 1]))
            for index in lasts:
                last[int(clients[index])] = int(ordered[index])
            scheduled = end - latency
            low = int(scheduled.min())
            for span in spans:
                if span.start > high:
                    break
                if span.stop > low:
                    span.feed(chunk, scheduled)
    return tally


# --- per run: O4's numbers and the rest, from its spans ------------------------------------

def buckets(span, offset_halves, count_halves):
    """(scheduled, completed, late) half-second counts from offset_halves on, padded with zeros to count_halves."""
    def cut(values):
        part = values[..., offset_halves:offset_halves + count_halves]
        missing = count_halves - part.shape[-1]
        if missing > 0:
            part = np.concatenate([part, np.zeros((*part.shape[:-1], missing), part.dtype)], axis=-1)
        return part
    return cut(span.scheduled), cut(span.completed), cut(span.late)


def drops(scheduled, completed):
    """1 - X/S per bucket; -inf where nothing was scheduled (such a bucket has no drop)."""
    scheduled = np.asarray(scheduled, float)
    with np.errstate(divide='ignore', invalid='ignore'):
        return np.where(scheduled > 0, 1.0 - np.asarray(completed, float) / np.where(scheduled > 0, scheduled, 1.0),
                        -np.inf)


def worst(values):
    finite = values[np.isfinite(values)] if len(values) else values
    return float(finite.max()) if len(finite) else None


def pair(values):
    """Half-second counts summed into whole seconds (an even count of halves)."""
    return values.reshape(*values.shape[:-1], -1, 2).sum(axis=-1)


def twos(values):
    """1-s counts summed into 2-s buckets, the last one a single second where the count is odd."""
    return np.add.reduceat(values, np.arange(0, values.shape[-1], 2), axis=-1) if values.shape[-1] else values


def latency_summary(latency):
    """p50, p95, p99, p99.9 and the maximum (ms), and the counts above 10, 50 and 100 ms, of latencies in us."""
    lat = np.asarray(latency)
    out = {'transactions': int(lat.size)}
    for name, q in (('p50', 50), ('p95', 95), ('p99', 99), ('p999', 99.9)):
        value = nearest_rank(lat, q)
        out[f'{name}_ms'] = None if value is None else round(value / 1000, 4)
    out['max_ms'] = None if not lat.size else round(float(lat.max()) / 1000, 4)
    for limit in LATENCY_LIMITS_MS:
        out[f'over_{limit}ms'] = int(np.sum(lat > limit * 1000))
    return out


def window_length(t0, t1, tail):
    return max(1, math.ceil(t1 + tail - t0 - 1e-9))


def window_stats(span, t0, t1, rate):
    """One run's numbers from its window span (which starts PRE_S before t0 and ends at t0 + T15).

    W, D (1-s buckets), D in 0.5-s and 2-s buckets, the late counts at L and 2L (in seconds of work), W and D with a
    15-s tail, completed over scheduled, the backlog at the window's end, the worst bucket and its lost time, the
    latencies of the transactions scheduled in [t0, t1], the p99 of the worst 5-s span, and per second around t0
    (from PRE_S before it to the window's end) completed over scheduled and p99, for the figure."""
    T, T15 = window_length(t0, t1, TAIL_S), window_length(t0, t1, TAIL15_S)
    pre = 2 * PRE_S
    s_half, x_half, late_half = buckets(span, pre, 2 * T15)
    s1, x1 = pair(s_half), pair(x_half)
    late1 = pair(late_half)
    drop1 = drops(s1, x1)
    stats = {'T': T, 'T15': T15, 'scheduled': int(s1[:T].sum()), 'completed': int(x1[:T].sum())}
    stats['W'] = float(np.maximum(0, s1[:T] - x1[:T]).sum()) / rate
    stats['D'] = worst(drop1[:T])
    stats['W15'] = float(np.maximum(0, s1 - x1).sum()) / rate
    stats['D15'] = worst(drop1)
    stats['D05'] = worst(drops(s_half[:2 * T], x_half[:2 * T]))
    stats['D2'] = worst(drops(twos(s1[:T]), twos(x1[:T])))
    stats['late_L'] = float(late1[0, :T].sum()) / rate if late1.shape[0] else None
    stats['late_2L'] = float(late1[1, :T].sum()) / rate if late1.shape[0] > 1 else None
    stats['completed_ratio'] = ratio(stats['completed'], stats['scheduled'])
    stats['backlog_end'] = stats['scheduled'] - stats['completed']
    finite = np.isfinite(drop1[:T])
    if finite.any():
        index = int(np.argmax(np.where(finite, drop1[:T], -np.inf)))
        stats['worst_bucket'] = index
        stats['worst_bucket_lost_s'] = max(0.0, float(s1[index] - x1[index]) / rate)
    else:
        stats['worst_bucket'] = stats['worst_bucket_lost_s'] = None
    stats['latency'] = latency_summary(span.core_latency if span.core_latency is not None else [])
    seconds = span.by_second or []
    p99s = [nearest_rank(part, 99) for part in seconds[PRE_S:PRE_S + T]]
    spans5 = []
    for first in range(max(1, T - 4)):
        joined = [part for part in seconds[PRE_S + first:PRE_S + first + 5] if len(part)]
        spans5.append(nearest_rank(np.concatenate(joined), 99) if joined else None)
    best = mx(spans5)
    stats['p99_worst5s_ms'] = None if best is None else round(best / 1000, 4)
    s_pre, x_pre, _ = buckets(span, 0, pre + 2 * T)
    stats['series'] = {'first_s': -PRE_S,
                       'ratio': [None if s == 0 else round(x / s, 4) for s, x in zip(pair(s_pre), pair(x_pre))],
                       'p99_ms': [None if (v := nearest_rank(part, 99)) is None else round(v / 1000, 3)
                                  for part in seconds[:PRE_S + T]]}
    stats['p99_by_second_ms'] = [None if v is None else round(v / 1000, 3) for v in p99s]
    if span.ends is not None:
        inside = span.ends[(span.ends >= span.start + PRE_S * SECOND_US)
                           & (span.ends < span.start + (PRE_S + T) * SECOND_US)]
        if len(inside) > 1:
            gaps = np.diff(inside)
            index = int(np.argmax(gaps))
            stats['gap_utc'] = [inside[index] / SECOND_US, inside[index + 1] / SECOND_US]
    return stats


def covered(span, rate):
    """Whether a span's log holds at least LOG_COVERAGE of its rate's schedule over its length."""
    return int(span.scheduled.sum()) >= LOG_COVERAGE * rate * (span.stop - span.start) / SECOND_US


def settle_stats(span, rate, late_limit_us):
    """A settle's (or any load-only span's) latencies, its late share at L and its completions over its schedule."""
    lat = span.latency if span.latency is not None else np.zeros(0)
    scheduled, completed = int(span.scheduled.sum()), int(span.completed.sum())
    out = latency_summary(lat)
    out.update(seconds=round((span.stop - span.start) / SECOND_US, 3), scheduled=scheduled, completed=completed,
               late_share=None if not lat.size else float(np.sum(lat > late_limit_us)) / lat.size,
               completed_ratio=ratio(completed, scheduled))
    return out


def pool_series(span, rate):
    """A load-only span's buckets for the null: per 1-s bucket from its start (whole seconds only) the deficit
    max(0, S - X) / R, the drop 1 - X/S, the late counts at L and 2L over R; and the drops in 0.5-s and 2-s buckets."""
    seconds = int((span.stop - span.start) // SECOND_US)
    s_half, x_half, late_half = buckets(span, 0, 2 * seconds)
    s1, x1, late1 = pair(s_half), pair(x_half), pair(late_half)
    two = 2 * (seconds // 2)
    return {'W': np.maximum(0, s1 - x1) / rate, 'D': drops(s1, x1),
            'late_L': late1[0] / rate if late1.shape[0] else np.zeros(seconds),
            'late_2L': late1[1] / rate if late1.shape[0] > 1 else np.zeros(seconds),
            'D05': drops(s_half, x_half), 'D2': drops(s1[:two].reshape(-1, 2).sum(1), x1[:two].reshape(-1, 2).sum(1))}


# --- the null: a block bootstrap of the load-only time ---------------------------------------

class Grid:
    """One statistic's buckets over a pool's spans, as blocks of BLOCK_S seconds: for every position a block can
    start at (inside one span), the running sum (or maximum) over its first 1..B buckets."""

    def __init__(self, series, width, kind):
        self.B, self.kind = round(BLOCK_S / width), kind
        values = [np.asarray(item, float) for item in series]
        positions, offset = [], 0
        for item in values:
            if len(item) >= self.B:
                positions.append(np.arange(offset, offset + len(item) - self.B + 1))
            offset += len(item)
        self.positions = np.concatenate(positions) if positions else np.zeros(0, np.int64)
        if len(self.positions):
            flat = np.concatenate(values)
            blocks = flat[self.positions[:, None] + np.arange(self.B)]
            self.acc = np.cumsum(blocks, axis=1) if kind == 'sum' else np.maximum.accumulate(blocks, axis=1)
        else:
            self.acc = np.zeros((0, self.B))

    def draw(self, rng, length, resamples):
        """Block positions for windows of `length` buckets: one row per resample."""
        return rng.integers(0, len(self.positions), size=(resamples, max(1, -(-length // self.B))))

    def window(self, idx, length):
        """The statistic of the windows that join idx's blocks to `length` buckets (the last block cut short)."""
        q, r = divmod(length, self.B)
        if q == 0:
            value = self.acc[idx[:, 0], length - 1]
        else:
            full = self.acc[idx[:, :q], self.B - 1]
            value = full.sum(axis=1) if self.kind == 'sum' else full.max(axis=1)
            if r:
                part = self.acc[idx[:, q], r - 1]
                value = value + part if self.kind == 'sum' else np.maximum(value, part)
        return np.where(np.isfinite(value), value, 0.0)


class Null:
    """One pool's bootstrap: windows drawn per run, at its own T, from a seed of their own."""

    def __init__(self, name, series, resamples, seed):
        self.name, self.resamples, self.seed = name, resamples, seed
        self.grids = {stat: Grid([item[stat] for item in series], width, kind)
                      for stat, (width, kind) in NULL_STATS.items()}
        self.spans = len(series)
        self.seconds = sum(len(item['W']) for item in series)

    @property
    def usable(self):
        return all(len(grid.positions) for grid in self.grids.values())

    def rng(self, key):
        return np.random.default_rng([self.seed, zlib.crc32(f'{self.name}/{key}'.encode())])

    def windows(self, key, T, T15):
        """Each statistic of `resamples` null windows at T (W and D also at T15), from blocks drawn for `key`."""
        rng = self.rng(key)
        out = {}
        grid = self.grids['W']
        idx = grid.draw(rng, T15, self.resamples)
        for stat in ('W', 'D', 'late_L', 'late_2L'):
            out[stat] = self.grids[stat].window(idx, T).astype(np.float32)
        out['W15'] = self.grids['W'].window(idx, T15).astype(np.float32)
        out['D15'] = self.grids['D'].window(idx, T15).astype(np.float32)
        for stat, length in (('D05', 2 * T), ('D2', -(-T // 2))):
            grid = self.grids[stat]
            out[stat] = grid.window(grid.draw(rng, length, self.resamples), length).astype(np.float32)
        return out


# --- loading ------------------------------------------------------------------------

def aborted_copy(record):
    """A run of an arm with an output whose own tool aborted it (failure_kind 'tool': a timed command exited non-zero
    or timed out). Its copy doesn't exist, so it doesn't start: the pre-registration's "wrong", which R3 and L5 read.
    The harness writes it as status 'fail' (run_exp04's tool_aborted); records written before that say 'error'."""
    return record.get('failure_kind') == 'tool' and record.get('method') in OUTPUT_ARMS


def harness_error(r):
    """An error of the harness around an arm with an output (SSH, a timeout of a check, a missing binary): its output
    is unverified, not wrong (What a result means)."""
    return r['status'] == 'error' and r['failure_kind'] != 'tool' and r['method'] in OUTPUT_ARMS


def make_run(sweep, index, record):
    """One run of the session from its record: what the analysis reads, keyed for the cells."""
    window = record.get('window') or {}
    settle = record.get('settle') or {}
    offset = window.get('clock_offset_s', record.get('clock_offset_s'))
    t0, t1 = window.get('t0_source_utc'), window.get('t1_source_utc')
    if t0 is None and window.get('t0_utc') is not None and offset is not None:
        t0 = window['t0_utc'] + offset
    if t1 is None and window.get('t1_utc') is not None and offset is not None:
        t1 = window['t1_utc'] + offset

    def source(value):
        return None if value is None or offset is None else value + offset
    totals = record.get('totals') or {}
    method = record.get('method')
    status, aborted = record.get('status'), aborted_copy(record)
    if aborted:
        status = 'fail'  # a copy its own tool aborted doesn't start: wrong (as tool_aborted writes it since)
    return {'sweep': sweep, 'index': index, 'record': record, 'trial_id': record.get('trial_id'),
            'block': record.get('block'), 'repeat': record.get('repeat'), 'block_repeat': record.get('block_repeat'),
            'control_slot': record.get('control_slot'), 'speed': (record.get('speed') or {}).get('label'),
            'method': method, 'status': status, 'aborted': aborted, 'void': bool(record.get('void')),
            'error': record.get('error'), 'failure_kind': record.get('failure_kind'), 'rate': record.get('load_rate'),
            'load_run': record.get('load_run'), 'offset': offset, 't0': t0, 't1': t1,
            'settle_span': (source(settle.get('started_utc')), source(settle.get('ended_utc'))),
            'tail_end': source(get(record, 'tail', 'ended_utc')), 'total_s': totals.get('wall_seconds'),
            'cpu_calibrated': measure.calibrated_cpu(record) if record.get('block') == 'calibration' else None,
            # Under load (a view): the window's, from the sampler (run_numbers); the timed phases' alone beside.
            'cpu_excess': None, 'cpu_excess_phases': measure.excess_cpu(record) if record.get('load_rate') else None,
            'cpu_busy': measure.phases_cpu(record), 'wire_s2d': totals.get('wire_bytes_source_to_dest'),
            'started_utc': utc(record.get('started_utc')), 'finished_utc': utc(record.get('finished_utc')),
            'stats': None, 'settle': None, 'flags': {}, 'sampler': {}}


def load(sweep):
    """One sweep: (runs, trials, run.json). The runs in summary.csv's order (the order they ran), each from its
    record; without a summary, every record found, by its start."""
    sweep = Path(sweep)
    run = read_json(sweep / 'run.json', {})
    trials = {}
    for path in sorted((sweep / 'trials').glob('*.json')):
        trial = read_json(path)
        if trial:
            trials[(sweep.name, trial['trial_id'])] = trial
    paths = []
    if (sweep / 'summary.csv').exists():
        with (sweep / 'summary.csv').open(newline='') as f:
            for line in csv.DictReader(f):
                paths.append(sweep / 'runs' / line['trial_id'] / f"r{line['repeat']}-{line['speed']}-{line['method']}.json")
    else:
        found = [(read_json(path) or {}, path) for path in (sweep / 'runs').glob('*/*.json')]
        paths = [path for record, path in sorted(found, key=lambda item: str(item[0].get('started_utc')))]
    runs = []
    for path in paths:
        record = read_json(path)
        if record:
            runs.append(make_run(sweep.name, len(runs), record))
    return runs, trials, run


# --- the session ------------------------------------------------------------------------

class Session:
    """Every run of the session with its numbers from the logs, its standing and flags, the null of each block and
    the context the rules read (A1-A4, A8's clock gate, A13, R3)."""

    def __init__(self, runs, trials=None, meta=None, *, sweeps=(), late_limit_ms=10.0, resamples=10_000, seed=121,
                 client_wait_share=0.25, idle_noise=None, busy_cpu=None, capacity_tps=None):
        self.runs, self.trials, self.meta = runs, trials or {}, meta or []
        self.sweeps = {Path(sweep).name: Path(sweep) for sweep in sweeps}
        self.late_limit_ms, self.resamples, self.seed = late_limit_ms, resamples, seed
        self.client_wait_share, self.idle_noise = client_wait_share, idle_noise
        self.capacity_tps_given = capacity_tps
        self.limits_us = [int(round(late_limit_ms * 1000)), int(round(2 * late_limit_ms * 1000))]
        self.spans, self.tallies, self.samplers, self.warmups = {}, {}, {}, {}
        self.nulls: dict = {}
        self._null_cache: dict = {}
        self._cells: dict = {}
        self._unsettled: dict = {}
        self.busy_cpu_given = busy_cpu
        # The smoke run's sweep (its oracle controls ran): its own settles give the busy CPU smoke item 2 records.
        self.smoke = any(get(trial, 'oracle_controls', 'controls') for trial in self.trials.values())
        self.read_logs()
        self.assess()
        self.client_wait_flags()
        self.ctx = context(self)

    # Reading the logs.

    def trial_offset(self, trial):
        return (get(trial, 'clock', 'start', 'offset_s') if get(trial, 'clock', 'start', 'offset_s') is not None
                else get(trial, 'probes', 'clock', 'offset_s'))

    def read_logs(self):
        """Each trial's spans, then each logged pgbench run read once (stream_run), then each run's numbers."""
        for (sweep, trial_id), trial in sorted(self.trials.items()):
            directory = self.sweeps.get(sweep, Path(sweep)) / 'logs' / trial_id / 'load'
            runs = [run for run in self.runs if run['sweep'] == sweep and run['trial_id'] == trial_id]
            spans = self.make_spans(trial, runs)
            by_run: dict = {}
            for span in spans.values():
                by_run.setdefault(span.run_id, []).append(span)
            for load_run in get(trial, 'load', 'runs') or []:
                names = load_run.get('log_files') or []
                paths = [measure.existing_log(directory, name) for name in names]
                if not names:
                    continue
                try:
                    tally = stream_run(paths, by_run.get(load_run['run_id'], []))
                except (OSError, ValueError) as exc:
                    tally = {**log_tally(), 'error': f'{type(exc).__name__}: {exc}'}
                self.tallies[(sweep, trial_id, load_run['run_id'])] = tally
            for span in spans.values():
                span.finish()
            self.spans[(sweep, trial_id)] = spans
            self.samplers[(sweep, trial_id)] = Sampler.read(directory / 'sampler')
            self.run_numbers((sweep, trial_id), trial, runs, spans)

    def make_spans(self, trial, runs):
        """The spans a trial's analysis needs, each keyed and tied to the load run whose log holds it."""
        spans = {}
        offset = self.trial_offset(trial) or 0.0
        for index, warmup in enumerate(get(trial, 'load', 'warmups') or []):
            started, ended = warmup.get('started_utc'), warmup.get('ended_utc')
            if started is None or ended is None:
                continue
            pool = warmup.get('pool_utc') or [max(started, ended - 60), ended]
            spans[('warmup', index)] = Span(('warmup', index), warmup.get('run_id'), started + offset, ended + offset,
                                            self.limits_us)
            spans[('warmup_pool', index)] = Span(('warmup_pool', index), warmup.get('run_id'), pool[0] + offset,
                                                 pool[1] + offset, self.limits_us)
        for run in runs:
            key = (run['repeat'], run['speed'], run['method'])
            if run['block'] not in LOADED_BLOCKS or not run['load_run'] or not run['rate']:
                continue
            start, end = run['settle_span']
            if start is not None and end is not None and end > start:
                spans[('settle', key)] = Span(('settle', key), run['load_run'], start, end, self.limits_us)
            if run['t0'] is not None and run['t1'] is not None:
                T15 = window_length(run['t0'], run['t1'], TAIL15_S)
                spans[('window', key)] = Span(('window', key), run['load_run'], run['t0'] - PRE_S, run['t0'] + T15,
                                              self.limits_us, seconds=True, core=(run['t0'], run['t1']),
                                              ends=run['method'] == 'pause2')
            if run['method'] == 'none' and start is not None and run['tail_end'] is not None:
                spans[('none', key)] = Span(('none', key), run['load_run'], start, run['tail_end'], self.limits_us)
        return spans

    def run_numbers(self, trial_key, trial, runs, spans):
        """Each loaded run's numbers (window_stats), its settle's (settle_stats), and the gate's log part; the
        warm-ups' latencies; the sampler's view of each window and settle."""
        sampler = self.samplers.get(trial_key)
        warmups = []
        offset = self.trial_offset(trial) or 0.0
        for index, warmup in enumerate(get(trial, 'load', 'warmups') or []):
            whole, pool = spans.get(('warmup', index)), spans.get(('warmup_pool', index))
            rate = self.rate_of(trial, warmup.get('run_id'))
            entry = {'index': index, 'run_id': warmup.get('run_id'), 'rate': rate,
                     'ended': None if warmup.get('ended_utc') is None else warmup['ended_utc'] + offset,
                     'whole': None if whole is None or not rate else settle_stats(whole, rate, self.limits_us[0]),
                     'pool': None if pool is None or not rate else settle_stats(pool, rate, self.limits_us[0]),
                     'span': pool}
            warmups.append(entry)
        self.warmups[trial_key] = warmups
        for run in runs:
            key = (run['repeat'], run['speed'], run['method'])
            window, settle = spans.get(('window', key)), spans.get(('settle', key))
            if window is not None and run['rate']:
                stats = window_stats(window, run['t0'], run['t1'], run['rate'])
                # A window the logs don't cover (a missing or short log) would read as no loss at all.
                if stats['scheduled'] < LOG_COVERAGE * run['rate'] * stats['T']:
                    run['log_gap'] = (f'{stats["scheduled"]} transactions scheduled in its {stats["T"]} s against '
                                      f'{run["rate"]} per second')
                else:
                    run['stats'] = stats
            if settle is not None and run['rate']:
                run['settle'] = settle_stats(settle, run['rate'], self.limits_us[0])
                run['settle']['span'] = settle
                run['settle']['warmup'] = next((w for w in reversed(warmups) if w['run_id'] == run['load_run']
                                                and (w['ended'] is None or w['ended'] <= run['settle_span'][0] + 1)),
                                               None)
                if run['stats'] is not None and run['settle'].get('p50_ms') is not None and window is not None:
                    above = window.core_latency.astype(np.int64) - run['settle']['p50_ms'] * 1000
                    run['stats']['backlog_area_s'] = round(float(np.maximum(0, above).sum()) / 1e6, 4)
            if sampler is not None and run['stats'] is not None:
                run['sampler'] = sampler.view(run, trial)
                run['cpu_excess'] = run['sampler'].get('cpu_excess_s')

    @staticmethod
    def rate_of(trial, run_id):
        found = next((run for run in get(trial, 'load', 'runs') or [] if run.get('run_id') == run_id), None)
        return (found or {}).get('rate')

    # Each run's standing.

    def assess(self):
        """Durability (checks (a) and (b), the negative control), the settle gate's log part, each run's flags, and
        whether it counts in its cell's median."""
        runs = self.runs
        control = [r for r in runs if r['method'] in measure.NON_DURABLE_METHODS]
        increases = [get(r['record'], 'durability', 'dirty_writeback_increase_bytes') for r in control]
        self.a_void = any(self.a_ok(r) for r in control) or any(v is not None and v < 1.5e9 for v in increases)
        for r in runs:
            r['durability_failures'], r['durability_unchecked'] = self.durability(r)
        for r in runs:
            r['flags'] = self.flags(r)
        for r in runs:
            r['counts'] = not self.why(r)
            r['left_out'] = self.why(r)

    def a_ok(self, r):
        """Check (a), judged again against max(4 MiB, --idle-noise-bytes) when it is given."""
        durability = r['record'].get('durability') or {}
        increase = durability.get('dirty_writeback_increase_bytes')
        if self.idle_noise is not None and increase is not None:
            return increase <= max(measure.DIRTY_LIMIT_BYTES, int(self.idle_noise))
        return durability.get('a_ok')

    def durability(self, r):
        """(failures, unchecked): check (a) where the arm writes on dest (unless the negative control voided it), check
        (b) for full copies and staged pgdelta's new O (measure.DISK_WRITE_CHECK_METHODS)."""
        method = r['method']
        if method in measure.NON_DURABLE_METHODS or r['status'] == 'error' or r['aborted']:
            return [], []
        durability = r['record'].get('durability') or {}
        checks = {}
        if durability.get('a_required', method not in measure.NO_DEST_WRITE_METHODS) and not self.a_void:
            checks['a'] = self.a_ok(r)
        if method in measure.DISK_WRITE_CHECK_METHODS:
            checks['b'] = durability.get('b_ok')
        return ([name for name, ok in checks.items() if ok is False],
                [name for name, ok in checks.items() if ok is None])

    def why(self, r):
        """Why a run leaves its cell's median, or '' if it counts: wrong, unverified or durability-failed runs leave
        it (Short cells), and so do errors, void runs (the load died) and runs whose window the logs don't cover."""
        if r['status'] == 'error':
            return 'status error'
        if r['void']:
            return 'void: ' + (r['record'].get('void_reason') or 'the load died')
        if r['aborted']:
            return 'wrong: its copy aborted by its tool'
        if r['status'] != 'pass':
            return {'fail': 'wrong', 'unverified': 'unverified'}.get(r['status'], f'status {r["status"]}')
        if r['durability_failures']:
            return 'durability failed: ' + ', '.join(r['durability_failures'])
        if r['durability_unchecked']:
            return 'durability unchecked: ' + ', '.join(r['durability_unchecked'])
        if r['block'] in LOADED_BLOCKS and r['stats'] is None:
            return 'no numbers from the logs' + (f' ({r["log_gap"]})' if r.get('log_gap') else '')
        return ''

    def gate_log_ok(self, r):
        """The settle gate's log part (A8): the settle's p99 at most twice its load run's warm-up's (its last minute),
        and at most 0.1% of its transactions later than L. None where it can't be judged; False where its log holds
        less than half its rate's schedule (LOG_COVERAGE), as a window then has no numbers: a settle the log doesn't
        cover would read as one without loss."""
        settle = r['settle']
        if settle and settle.get('span') is not None and r['rate'] and not covered(settle['span'], r['rate']):
            return False
        if not settle or settle.get('p99_ms') is None:
            return None
        warmup = (settle.get('warmup') or {}).get('pool') or {}
        p99_ok = None if warmup.get('p99_ms') is None else settle['p99_ms'] <= SETTLE_P99_FACTOR * warmup['p99_ms']
        late_ok = None if settle.get('late_share') is None else settle['late_share'] <= SETTLE_LATE_SHARE
        return every([p99_ok, late_ok])

    def settled(self, r):
        """Whether a run's settle passed its gate, both parts (the live one capped at 60 s, then the log's)."""
        gate = get(r['record'], 'settle', 'gate')
        if gate is None:
            return None
        return bool(gate.get('passed')) and not get(r['record'], 'settle', 'unsettled') and self.gate_log_ok(r) is not False

    def flags(self, r):
        """A8's flags per run, but client wait, which needs its cell's measurability (client_wait_flags)."""
        if r['block'] not in LOADED_BLOCKS:
            return {}
        sampler = r.get('sampler') or {}
        settled = self.settled(r)
        cache = get(r['record'], 'source_untimed', 'cache', 'cache_flag')
        return {'unsettled': None if settled is None else not settled, 'checkpoint': sampler.get('checkpoint'),
                'steal': sampler.get('steal'), 'cache': cache, 'client': sampler.get('client'),
                'client_wait_w': None, 'client_wait_d': None, 'autovacuum': sampler.get('autovacuum')}

    def flagged(self, r):
        return [name for name in RUN_FLAGS if r['flags'].get(name)]

    # The cells' runs, the pools and the nulls.

    def cell_runs(self, block, speed, arm, clean=False, counting=True):
        return [r for r in self.runs if r['block'] == block and r['method'] == arm
                and (speed is None or arm in CONTROLS or r['speed'] == speed)
                and (not counting or r['counts']) and (not clean or not self.flagged(r))]

    def pool_name(self, run):
        return POOL_OF.get(run['block'])

    def pool(self, name, variant='main'):
        """The block's null (Null): its `none` spans, its settles that passed their gate (with the unsettled ones too in
        the 'unsettled' variant) and its warm-ups' last minute."""
        key = (name, variant)
        if key in self.nulls:
            return self.nulls[key]
        series, rate = [], None
        for r in self.runs:
            if self.pool_name(r) != name or not r['rate']:
                continue
            rate = rate or r['rate']
            spans = self.spans.get((r['sweep'], r['trial_id'])) or {}
            run_key = (r['repeat'], r['speed'], r['method'])
            settled = self.settled(r)
            if r['method'] == 'none':
                # Its span holds its settle: left out with an unsettled settle, back in the 'unsettled' variant.
                span = spans.get(('none', run_key))
                if span is not None and not r['void'] and r['status'] != 'error' and covered(span, r['rate']) and (
                        settled is not False or variant == 'unsettled'):
                    series.append(pool_series(span, r['rate']))
                continue
            # A span the log doesn't cover (its load run's log holds nothing then) enters no variant: it would read
            # as load-only time without any loss.
            span = spans.get(('settle', run_key))
            if span is not None and covered(span, r['rate']) and (
                    settled or (variant == 'unsettled' and settled is False)):
                series.append(pool_series(span, r['rate']))
        for trial_key, warmups in self.warmups.items():
            for warmup in warmups:
                if warmup['span'] is None or not warmup['rate'] or self.warmup_pool(trial_key, warmup) != name \
                        or not covered(warmup['span'], warmup['rate']):
                    continue
                series.append(pool_series(warmup['span'], warmup['rate']))
        null = Null(f'{name}/{variant}', series, self.resamples, self.seed) if series else None
        self.nulls[key] = null
        return null

    def warmup_pool(self, trial_key, warmup):
        """A warm-up's block: the busy load's (busy, warm) or the moderate one's, by the runs that ran on its load run."""
        found = next((r for r in self.runs if (r['sweep'], r['trial_id']) == trial_key
                      and r['load_run'] == warmup['run_id'] and r['block'] in POOL_OF), None)
        if found is not None:
            return self.pool_name(found)
        return {'busy': 'busy', 'moderate': 'moderate'}.get(warmup['run_id'])

    def null(self, run, variant='main'):
        """A run's null windows (Null.windows) at its own T and T15, drawn from its block's pool."""
        if run['stats'] is None:
            return None
        variant = variant if variant == 'main' or self.has_unsettled(self.pool_name(run)) else 'main'
        key = (run['sweep'], run['trial_id'], run['repeat'], run['speed'], run['method'], variant)
        if key not in self._null_cache:
            pool = self.pool(self.pool_name(run), variant)
            self._null_cache[key] = (None if pool is None or not pool.usable else
                                     pool.windows('/'.join(map(str, key)), run['stats']['T'], run['stats']['T15']))
        return self._null_cache[key]

    def has_unsettled(self, name):
        if name not in self._unsettled:
            self._unsettled[name] = any(self.pool_name(r) == name and self.settled(r) is False for r in self.runs)
        return self._unsettled[name]

    def cells(self, view='main'):
        if view not in self._cells:
            self._cells[view] = (PairedCells if VIEWS[view]['paired'] else Cells)(self, view)
        return self._cells[view]

    def client_wait_flags(self):
        """A8's client wait, on W and on D, judged where the arm's W (or D) is measurable in its cell (main view):
        the pgbench threads' added run-queue wait at least --client-wait-share of the run's W, or of its worst bucket's
        lost time."""
        cells = self.cells('main')
        for r in self.runs:
            if r['block'] not in LOADED_BLOCKS or r['stats'] is None:
                continue
            cell = cells.cell(r['block'], cell_speed(r), r['method'])
            sampler = r.get('sampler') or {}
            wait, bucket = sampler.get('client_wait_s'), sampler.get('client_wait_worst_bucket_s')
            w_measurable = (cell or {}).get('W_measurable')
            d_measurable = (cell or {}).get('D_measurable')
            W, lost = r['stats']['W'], r['stats'].get('worst_bucket_lost_s')
            r['flags']['client_wait_w'] = (None if not w_measurable or wait is None or not W
                                           else wait >= self.client_wait_share * W)
            r['flags']['client_wait_d'] = (None if not d_measurable or bucket is None or not lost
                                           else bucket >= self.client_wait_share * lost)
        self._cells = {}  # the clean view's runs depend on the flags

    # Session-wide readings.

    def calibrated(self, arm):
        """An arm's calibrated CPU-s: the median of its counting calibration runs."""
        return median(r['cpu_calibrated'] for r in self.runs if r['block'] == 'calibration' and r['method'] == arm
                      and r['counts'])

    def idle_time(self, arm):
        return median(r['total_s'] for r in self.runs if r['block'] == 'calibration' and r['method'] == arm
                      and r['counts'])

    def capacity(self):
        """The trial's own C (step 7), the median over the trials."""
        return median(get(trial, 'load', 'capacity', 'capacity_tps') for trial in self.trials.values())

    def rate(self, block):
        return median(r['rate'] for r in self.runs if r['block'] == block and r['rate'])

    def cpus(self):
        return median(get(run, 'environment', 'source', 'cpu_count') for run in self.meta) or 8

    def busy_cpu(self, block='busy'):
        """The source's VM-wide busy CPU at the block's rate, as a share of its CPUs: --busy-cpu for the busy block if
        given (smoke item 2's registered value), else the median of the block's settles."""
        if block == 'busy' and self.busy_cpu_given is not None:
            return self.busy_cpu_given
        value = median(get(r['record'], 'settle', 'busy_rate_no_steal') for r in self.runs if r['block'] == block)
        return None if value is None else value / self.cpus()

    def high_cpu(self):
        """Which of E4's and E7's ranges apply: the busy CPU at R_busy above 50%. On the main sweep only the value smoke
        item 2 recorded before it (--busy-cpu) selects them, so its own settles can't pick another branch than the
        registered one: None without it."""
        if self.busy_cpu_given is None and not self.smoke:
            return None
        share = self.busy_cpu('busy')
        return None if share is None else share > 0.5


def cell_speed(run):
    """A run's cell's speed: the controls' cells take every link (they use none)."""
    return None if run['method'] in CONTROLS else run['speed']


def cell_keys(blocks=LOADED_BLOCKS, arms=None):
    """Every (block, speed, arm) a cell can have: the arms at each speed, the controls at 'any link' (None)."""
    for block in blocks:
        for speed in [*SPEEDS, None]:
            for arm in arms or ARM_NAMES:
                if (arm in CONTROLS) == (speed is None):
                    yield block, speed, arm


# --- the sampler -----------------------------------------------------------------------------

class Sampler:
    """The sampler's data on the source's clock: its 0.1-s ticks (VM-wide CPU, the groups' CPU, each pgbench thread's
    run and run-queue wait) and its 1-s SQL lines (pg_stat_wal, pg_stat_checkpointer, the autovacuum workers)."""

    def __init__(self, ticks, sql):
        ticks = sorted(ticks, key=lambda tick: tick['t'])
        self.t = np.array([tick['t'] for tick in ticks], float)
        cpu = np.array([tick.get('cpu') or [0] * 8 for tick in ticks], float).reshape(-1, 8)
        self.steal = cpu[:, 7] if len(cpu) else np.zeros(0)
        # VM-wide busy CPU, steal left out: user, nice, system, irq, softirq (measure.CPU_BUSY_NO_STEAL).
        self.busy = cpu[:, [0, 1, 2, 5, 6]].sum(axis=1) if len(cpu) else np.zeros(0)
        self.groups = {group: np.cumsum([(tick.get('g') or {}).get(group, 0) for tick in ticks]) for group in GROUPS}
        self.pgbench = [tick.get('pgbench') for tick in ticks]
        self.threads: dict = {}
        for tick in ticks:
            for tid, values in (tick.get('th') or {}).items():
                self.threads.setdefault((tick.get('pgbench'), tid), []).append((tick['t'], *values))
        self.threads = {key: np.array(values, float) for key, values in self.threads.items()}
        self.sql = sorted(sql, key=lambda line: line.get('t') or 0)
        self.sql_t = np.array([line.get('t') or 0 for line in self.sql], float)

    @classmethod
    def read(cls, directory):
        """The sampler's files below the load's logs (compressed or not); None without its ticks."""
        def lines(name):
            path = measure.existing_log(directory, name)
            if not path.exists():
                return []
            out = []
            for line in measure.log_lines(path):
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    out.append(value)
            return out
        try:
            ticks = lines('sampler.jsonl')
        except (OSError, ValueError):
            ticks = []
        if not ticks:
            return None
        try:
            sql = lines('sampler-sql.jsonl')
        except (OSError, ValueError):
            sql = []
        sampler = cls(ticks, sql)
        try:  # compressed with the logs at step 15, as everything but the registry is
            sampler.final = (lines('sampler-final.json') or [None])[0]
        except (OSError, ValueError):
            sampler.final = None
        return sampler

    def increase(self, values, start, end, t=None):
        """A cumulative counter's increase from start to end (linear between ticks)."""
        t = self.t if t is None else t
        if len(t) == 0 or end <= start:
            return None
        if start < t[0] - 1 or end > t[-1] + 1:
            return None
        return float(np.interp(end, t, values) - np.interp(start, t, values))

    def thread_rates(self, pid, start, end):
        """{tid: (run s, wait s)} of the load run's pgbench threads from start to end."""
        out = {}
        for (owner, tid), values in self.threads.items():
            if owner != pid or len(values) < 2:
                continue
            run = self.increase(values[:, 1], start, end, values[:, 0])
            wait = self.increase(values[:, 2], start, end, values[:, 0])
            if run is not None and wait is not None:
                out[tid] = (run / 1e9, wait / 1e9)
        return out

    def sql_at(self, moment, before=True):
        """The SQL line last before (or first after) a moment."""
        if not len(self.sql_t):
            return None
        if before:
            index = int(np.searchsorted(self.sql_t, moment, side='right')) - 1
            return self.sql[index] if index >= 0 else None
        index = int(np.searchsorted(self.sql_t, moment, side='left'))
        return self.sql[index] if index < len(self.sql) else None

    def view(self, run, trial):
        """What the sampler shows of one run: steal in its window (A8), its pgbench threads' CPU in its settle (client)
        and their added run-queue wait in the window and in its worst bucket (client wait), checkpoints between the
        settle's start and the tail's end (checkpoint), autovacuum workers in the window, and, as a mechanism, each
        group's added CPU, WAL sync time and the run queue."""
        stats = run['stats']
        t0, T = run['t0'], stats['T']
        end = t0 + T
        out = {}
        steal = self.increase(self.steal, t0, end)
        out['steal_rate'] = None if steal is None else steal / USER_HZ / T
        out['steal'] = None if steal is None else out['steal_rate'] > STEAL_RATE
        settle_start, settle_end = run['settle_span']
        # Source CPU under load, as registered (a view): the window's [t0, t1 + 10 s] VM-wide busy CPU minus the
        # settle's rate times the window's length, both from these ticks, so the load's own extra CPU after the arm
        # (a catch-up, the full-page images after its checkpoint) counts too.
        window_end = run['t1'] + TAIL_S if run.get('t1') is not None else end
        busy = self.increase(self.busy, t0, window_end)
        before = (None if settle_start is None or settle_end is None or settle_end <= settle_start
                  else self.increase(self.busy, settle_start, settle_end))
        if busy is not None and before is not None:
            out['cpu_excess_s'] = (busy - before / (settle_end - settle_start) * (window_end - t0)) / USER_HZ
        load_run = next((item for item in get(trial, 'load', 'runs') or [] if item.get('run_id') == run['load_run']),
                        {})
        pid = load_run.get('pid')
        if settle_start is not None and settle_end is not None and settle_end > settle_start and pid is not None:
            length = settle_end - settle_start
            settle = self.thread_rates(pid, settle_start, settle_end)
            window = self.thread_rates(pid, t0, end)
            if settle:
                out['client_core_max'] = max(run_s / length for run_s, _ in settle.values())
                out['client'] = out['client_core_max'] > CLIENT_CORE
            common = [tid for tid in window if tid in settle]
            if common:
                rates = {tid: settle[tid][1] / length for tid in common}
                out['client_wait_s'] = sum(window[tid][1] - rates[tid] * T for tid in common) / len(common)
                if stats.get('worst_bucket') is not None:
                    first = t0 + stats['worst_bucket']
                    bucket = self.thread_rates(pid, first, first + 1)
                    waits = [bucket[tid][1] - rates[tid] for tid in common if tid in bucket]
                    out['client_wait_worst_bucket_s'] = sum(waits) / len(waits) if waits else None
            for group in GROUPS:
                inside = self.increase(self.groups[group], t0, end)
                before = self.increase(self.groups[group], settle_start, settle_end)
                out[f'cpu_{group}_s'] = (None if inside is None or before is None
                                         else (inside - before / length * T) / USER_HZ)
        if run['tail_end'] is not None and settle_start is not None:
            first, last = self.sql_at(settle_start, before=True), self.sql_at(run['tail_end'], before=False)
            if first and last:
                timed = (get(last, 'checkpointer', 'num_timed') or 0) - (get(first, 'checkpointer', 'num_timed') or 0)
                requested = ((get(last, 'checkpointer', 'num_requested') or 0)
                             - (get(first, 'checkpointer', 'num_requested') or 0))
                own = 1 if run['method'] in CHECKPOINT_ARMS else 0
                out['checkpoints'] = {'timed': timed, 'requested': requested, 'own': own}
                out['checkpoint'] = timed > 0 or requested > own
        inside = [line for line in self.sql if t0 <= (line.get('t') or 0) <= end]
        if inside:
            workers = [worker for line in inside for worker in line.get('autovacuum') or []]
            out['autovacuum'] = bool(workers)
            out['autovacuum_loaddb'] = any(worker.get('datname') == measure.LOAD_DATABASE for worker in workers)
            first, last = self.sql_at(t0, before=True), self.sql_at(end, before=False)
            if first and last:
                out['wal_sync_ms'] = (get(last, 'wal', 'wal_sync_time') or 0) - (get(first, 'wal', 'wal_sync_time') or 0)
        return out


# --- the cells ------------------------------------------------------------------------------

def null_summary(observed, null):
    """A cell's statistic against its null (The two O4 numbers): the null's median and 95th percentile; the net value,
    observed less the median, clipped at 0; measurable, above the 95th percentile; the bound for an unmeasurable one,
    the 95th percentile less the median; and the centred null, each resample less the median, clipped at 0."""
    null = np.asarray(null, float)
    middle, top = float(np.median(null)), float(np.percentile(null, PERCENTILE))
    return {'null': null, 'null_median': middle, 'null_p95': top, 'net': max(0.0, observed - middle),
            'measurable': observed > top, 'bound': top - middle, 'centred': np.maximum(0.0, null - middle)}


def centred_spread(centred_a, centred_b):
    """The tolerances' percentile for two cells: the 95th of a's centred null minus b's, resample by resample."""
    if centred_a is None or centred_b is None:
        return None
    return float(np.percentile(np.asarray(centred_a) - np.asarray(centred_b), PERCENTILE))


class CellsBase:
    """What every rule reads of a view: each cell (block, speed, arm) with its statistic, null, net value,
    measurability and bound, and the comparison of two cells (δ, meets). `consulted` records each comparison a rule
    made; `overrides` moves one across its line (near the line)."""

    def __init__(self):
        self.consulted: list = []
        self.overrides: dict = {}

    def cell(self, block, speed, arm):
        raise NotImplementedError

    def judged(self, block, speed, arm):
        cell = self.cell(block, speed, arm)
        return cell is not None and not cell['short']

    def measurable(self, block, speed, arm, role):
        cell = self.cell(block, speed, arm)
        return None if cell is None or cell['short'] else cell.get(f'{role}_measurable')

    def spread(self, a, b, role):
        """The 95th percentile of a's centred null minus b's (Tolerances)."""
        raise NotImplementedError

    def compare(self, a_key, b_key=None, full_keys=None):
        """{'W': {...}, 'D': {...}} for cell a against cell b (default: full in a's block and speed): the difference of
        their net values, δ, and its parts. None where either cell is missing or short, or has no null."""
        b_key = b_key or (a_key[0], a_key[1], 'full')
        self.consulted.append((a_key, b_key, tuple(full_keys) if full_keys else None))
        a, b = self.cell(*a_key), self.cell(*b_key)
        if a is None or b is None or a['short'] or b['short']:
            return None
        fulls = [self.cell(*key) for key in (full_keys or [b_key])]
        out = {}
        for role, floor in (('W', FLOOR_W), ('D', FLOOR_D)):
            a_net, b_net = a.get(f'{role}_net'), b.get(f'{role}_net')
            spread = self.spread(a, b, role)
            if a_net is None or b_net is None or spread is None:
                return None
            terms = [floor, spread]
            if role == 'W':
                terms += [FULL_SHARE * full['W_net'] for full in fulls if full and full.get('W_net') is not None]
            delta = max(terms)
            diff = self.overrides.get((a_key, b_key, role), a_net - b_net)
            out[role] = {'diff': diff, 'delta': delta, 'spread': spread, 'a_net': a_net, 'b_net': b_net}
        return out

    def meets(self, a_key, b_key=None, full_keys=None):
        """O4: W_net(arm) - W_net(full) <= δ_W and D_net(arm) - D_net(full) <= δ_D; None where it can't be judged."""
        found = self.compare(a_key, b_key, full_keys)
        if found is None:
            return None
        return all(found[role]['diff'] <= found[role]['delta'] + 1e-12 for role in ('W', 'D'))

    def missed(self, a_key, b_key=None, full_keys=None):
        """The numbers an arm missed on (W, D, or both); None where it can't be judged."""
        found = self.compare(a_key, b_key, full_keys)
        if found is None:
            return None
        return [role for role in ('W', 'D') if found[role]['diff'] > found[role]['delta'] + 1e-12]


class Cells(CellsBase):
    """One view's cells from the session's runs and nulls (VIEWS)."""

    def __init__(self, session, view='main'):
        super().__init__()
        self.S, self.name, self.view = session, view, VIEWS[view]
        self._cache: dict = {}

    def cell(self, block, speed, arm):
        if speed is None and arm not in CONTROLS:
            return None
        key = (block, None if arm in CONTROLS else speed, arm)
        if key not in self._cache:
            self._cache[key] = self._cell(*key)
        return self._cache[key]

    def _cell(self, block, speed, arm):
        runs = self.S.cell_runs(block, speed, arm, clean=self.view['clean'])
        runs = [r for r in runs if block == 'calibration' or r['stats'] is not None]
        if not runs:
            return None
        minimum = MIN_CONTROL_RUNS if arm in CONTROLS else MIN_RUNS
        cell = {'block': block, 'speed': speed, 'arm': arm, 'n': len(runs), 'short': len(runs) < minimum,
                'runs': runs, 'total_s': median(r['total_s'] for r in runs), 'total_max': mx(r['total_s'] for r in runs),
                'flagged': [r for r in runs if self.S.flagged(r)],
                'client_wait': sum(bool(r['flags'].get('client_wait_w') or r['flags'].get('client_wait_d'))
                                   for r in runs) > 1}
        cell['flag_cell'] = len(cell['flagged']) > 1
        if block == 'calibration':
            return cell
        reduce = np.median if self.view['reduce'] == 'median' else np.max
        nulls = [self.S.null(r, self.view['pool']) for r in runs]
        for role in ('W', 'D'):
            stat = self.view[role]
            values = [r['stats'].get(stat) for r in runs]
            observed = (None if any(v is None for v in values)
                        else float(reduce(np.array(values, float))))
            cell[role], cell[f'{role}_stat'] = observed, stat
            if any(null is None for null in nulls) or observed is None:
                cell.update({f'{role}_null': None, f'{role}_null_median': None, f'{role}_null_p95': None,
                             f'{role}_net': None, f'{role}_measurable': None, f'{role}_bound': None,
                             f'{role}_centred': None})
                continue
            null = reduce(np.stack([item[stat] for item in nulls], axis=1).astype(float), axis=1)
            cell.update({f'{role}_{key}': value for key, value in null_summary(observed, null).items()})
        for name in ('p50_ms', 'p95_ms', 'p99_ms', 'max_ms', 'over_50ms', 'over_100ms'):
            cell[f'lat_{name}'] = median(r['stats']['latency'].get(name) for r in runs)
        cell['p99_worst5s_ms'] = median(r['stats'].get('p99_worst5s_ms') for r in runs)
        cell['cpu_excess_s'] = median(r['cpu_excess'] for r in runs)
        cell['cpu_excess_phases_s'] = median(r['cpu_excess_phases'] for r in runs)
        return cell

    def spread(self, a, b, role):
        return centred_spread(a.get(f'{role}_centred'), b.get(f'{role}_centred'))


class PairedCells(Cells):
    """The paired view: each run of an arm against the same repeat's full run at the same speed in its block. The
    statistic is the median paired difference; its null the median of the same differences of null windows, each at
    its own run's T; net of that null's median, unclipped, against δ from it (with W_net(full)'s term from the main
    view). Only that pairing is defined: any other comparison (row 7b's warm v1proxy against its own cold cell, in
    another block) is the main view's, so the view can't make a row fragile on a pairing of unrelated runs."""

    def compare(self, a_key, b_key=None, full_keys=None):
        b_key = b_key or (a_key[0], a_key[1], 'full')
        if b_key != (a_key[0], a_key[1], 'full'):
            return super().compare(a_key, b_key, full_keys)
        self.consulted.append((a_key, b_key, tuple(full_keys) if full_keys else None))
        main = self.S.cells('main')
        a, b = main.cell(*a_key), main.cell(*b_key)
        if a is None or b is None or a['short'] or b['short']:
            return None
        by_repeat = {r['block_repeat']: r for r in b['runs']}
        pairs = [(r, by_repeat[r['block_repeat']]) for r in a['runs'] if r['block_repeat'] in by_repeat]
        if len(pairs) < MIN_RUNS:
            return None
        fulls = [main.cell(*key) for key in (full_keys or [b_key])]
        out = {}
        for role, floor in (('W', FLOOR_W), ('D', FLOOR_D)):
            nulls = [(self.S.null(x), self.S.null(y)) for x, y in pairs]
            if any(n is None for pair_ in nulls for n in pair_):
                return None
            observed = statistics.median(x['stats'][role] - y['stats'][role] for x, y in pairs)
            null = np.median(np.stack([nx[role].astype(float) - ny[role].astype(float) for nx, ny in nulls], axis=1),
                             axis=1)
            middle = float(np.median(null))
            spread = float(np.percentile(null - middle, PERCENTILE))
            terms = [floor, spread] + ([FULL_SHARE * full['W_net'] for full in fulls
                                        if full and full.get('W_net') is not None] if role == 'W' else [])
            diff = self.overrides.get((a_key, b_key, role), observed - middle)
            out[role] = {'diff': diff, 'delta': max(terms), 'spread': spread, 'a_net': None, 'b_net': None,
                         'pairs': len(pairs)}
        return out


class TableCells(CellsBase):
    """Cells given as a table, for the rules' tests: {(block, speed, arm): {'n', 'W_net', 'D_net', 'W_measurable',
    'D_measurable', 'W_bound', 'D_bound', 'total_s', ...}}; every spread is `spread` (default 0)."""

    def __init__(self, table, spread=0.0):
        super().__init__()
        self.table, self._spread = table, spread

    def cell(self, block, speed, arm):
        found = self.table.get((block, None if arm in CONTROLS else speed, arm))
        if found is None:
            return None
        cell = {'n': 5, 'short': False, 'W_net': 0.0, 'D_net': 0.0, 'W_measurable': False, 'D_measurable': False,
                'W_bound': 0.02, 'D_bound': 0.01, 'total_s': None, 'p99_worst5s_ms': None, 'client_wait': False,
                **found}
        cell['short'] = cell['n'] < (MIN_CONTROL_RUNS if arm in CONTROLS else MIN_RUNS)
        return cell

    def spread(self, a, b, role):
        return self._spread


# --- the rules -------------------------------------------------------------------------------------

def blocked(ctx, block):
    """Why a block's loss verdicts aren't judged (A2: instrument, A3: overload, A8's clock gate), or None."""
    return (ctx.get('blocked') or {}).get(block)


def r1(cells, ctx):
    """R1: at the registered speeds where staged pgdelta's median total_s under load is below full's (a speed where
    pgdelta is reclassified as not durable doesn't count; full reclassified counts as a win), is W_net(pgdelta) -
    W_net(full) <= δ_W? fires: it is higher at a speed where it wins."""
    reason = blocked(ctx, 'busy')
    reclassified = ctx.get('reclassified') or {}
    wins, notes, higher, values = [], [], [], {}
    for speed in REGISTERED:
        p, f = cells.cell('busy', speed, 'pgdelta'), cells.cell('busy', speed, 'full')
        if p is None or f is None or p['short'] or f['short']:
            notes.append(f'{speed_name(speed)}: a short cell')
            continue
        if speed in reclassified.get('pgdelta', ()):
            notes.append(f'{speed_name(speed)}: pgdelta not durable here (A13)')
            continue
        win = reclassified.get('full') is True or (p['total_s'] is not None and f['total_s'] is not None
                                                   and p['total_s'] < f['total_s'])
        if not win:
            continue
        wins.append(speed)
        if reason:
            continue
        found = cells.compare(('busy', speed, 'pgdelta'))
        if found is None:
            notes.append(f'{speed_name(speed)}: no null')
            continue
        values[speed] = found['W']
        if found['W']['diff'] > found['W']['delta'] + 1e-12:
            higher.append(speed)
    if reason:
        return {'verdict': None, 'why': reason, 'speeds': wins, 'higher': [], 'values': {}}
    judged = [speed for speed in wins if speed in values]
    if not judged:
        return {'verdict': None, 'why': 'no registered speed where pgdelta wins on time' + (
            f' ({"; ".join(notes)})' if notes else ''), 'speeds': wins, 'higher': [], 'values': values}
    return {'verdict': bool(higher), 'speeds': judged, 'higher': higher, 'values': values, 'notes': notes}


def rate_ratio(cells, ctx, speed, block='busy'):
    """R2 in one cell: the rate ratio (W_net per calibrated CPU-s, pgdelta's over full's) where both W's are
    measurable, or its bound where one is: (kind, value, verdict) with kind 'exact', 'lower' or 'upper' and verdict
    True (tracks), False (doesn't) or None (not judged)."""
    cpu = ctx.get('cpu') or {}
    cpu_p, cpu_f = cpu.get('pgdelta'), cpu.get('full')
    p, f = cells.cell(block, speed, 'pgdelta'), cells.cell(block, speed, 'full')
    if p is None or f is None or p['short'] or f['short'] or not cpu_p or not cpu_f:
        return None, None, None
    mp, mf = p.get('W_measurable'), f.get('W_measurable')
    override = (ctx.get('ratio_overrides') or {}).get(speed)
    if mp and mf:
        value = override if override is not None else ratio(p['W_net'] / cpu_p, f['W_net'] / cpu_f)
        return 'exact', value, None if value is None else 1 / TRACK - 1e-12 <= value <= TRACK + 1e-12
    if mp and f.get('W_bound') is not None:
        value = math.inf if f['W_bound'] <= 0 else (p['W_net'] / cpu_p) / (f['W_bound'] / cpu_f)
        value = override if override is not None else value
        return 'lower', value, False if value > TRACK else None
    if mf and p.get('W_bound') is not None:
        value = (p['W_bound'] / cpu_p) / (f['W_net'] / cpu_f) if f['W_net'] else None
        value = override if override is not None else value
        return 'upper', value, False if value is not None and value < 1 / TRACK else None
    return None, None, None


def r2(cells, ctx):
    """R2 (the October 24 note): tracks if both registered cells are judged and track; doesn't track if either judged
    cell doesn't; not judged otherwise."""
    reason = blocked(ctx, 'busy')
    if reason:
        return {'verdict': None, 'why': reason, 'cells': {}}
    found = {speed: rate_ratio(cells, ctx, speed) for speed in REGISTERED}
    verdicts = [verdict for _, _, verdict in found.values()]
    if any(v is False for v in verdicts):
        verdict = False
    elif all(v is True for v in verdicts):
        verdict = True
    else:
        verdict = None
    return {'verdict': verdict, 'cells': found}


def l2(ctx):
    cpu = ctx.get('cpu') or {}
    value = ratio(cpu.get('pgdelta'), cpu.get('full'))
    return value, within(value, 3, 5)


def l3_cell(cells, speed, block='busy'):
    """(kind, value): pgdelta's W_net over full's at a speed, exact where both are measurable, else the bound the
    unmeasurable side gives ('lower' where only pgdelta's is, 'upper' where only full's is); (None, None) otherwise."""
    p, f = cells.cell(block, speed, 'pgdelta'), cells.cell(block, speed, 'full')
    if p is None or f is None or p['short'] or f['short']:
        return None, None
    mp, mf = p.get('W_measurable'), f.get('W_measurable')
    if mp and mf:
        return 'exact', ratio(p['W_net'], f['W_net'])
    if mp and f.get('W_bound') is not None:
        return 'lower', math.inf if f['W_bound'] <= 0 else p['W_net'] / f['W_bound']
    if mf and p.get('W_bound') is not None:
        return 'upper', ratio(p['W_bound'], f['W_net'])
    return None, None


def l3(cells, ctx):
    """L3: the ratio within [2, 4] at 2 Gbit/s; 'failed high' or 'failed low' on its bound; not judged otherwise."""
    reason = blocked(ctx, 'busy')
    if reason:
        return {'verdict': None, 'text': f'not judged ({reason})'}
    kind, value = l3_cell(cells, '2g')
    if kind == 'exact':
        if value is None:
            return {'verdict': None, 'text': 'not judged (no ratio)', 'kind': kind}
        if 2 <= value <= 4:
            return {'verdict': True, 'text': f'holds (ratio {fmt(value)})', 'kind': kind, 'value': value}
        return {'verdict': False, 'text': f'failed {"high" if value > 4 else "low"} (ratio {fmt(value)})',
                'kind': kind, 'value': value}
    if kind == 'lower' and value > 4:
        return {'verdict': False, 'text': f'failed high (`full` not measurable; the ratio is at least {fmt(value)})',
                'kind': kind, 'value': value}
    if kind == 'upper' and value is not None and value < 2:
        return {'verdict': False, 'text': f'failed low (`pgdelta` not measurable; the ratio is at most {fmt(value)})',
                'kind': kind, 'value': value}
    return {'verdict': None, 'text': 'not judged' + (f' (the ratio\'s {kind} bound {fmt(value)} decides nothing)'
                                                     if kind else ' (neither W measurable, or a short cell)'),
            'kind': kind, 'value': value}


def l4(cells, ctx):
    """L4: the ratio at 0.5 Gbit/s above the ratio at 2, where both are defined; with full's W measurable at 2 but not
    at 0.5 and pgdelta's at both, it holds if the 0.5 Gbit/s ratio's lower bound exceeds the ratio at 2."""
    reason = blocked(ctx, 'busy')
    if reason:
        return {'verdict': None, 'text': f'not judged ({reason})'}
    k05, r05 = l3_cell(cells, '0.5g')
    k2, r2_ = l3_cell(cells, '2g')
    if k05 == 'exact' and k2 == 'exact' and r05 is not None and r2_ is not None:
        return {'verdict': r05 > r2_, 'text': f'{"holds" if r05 > r2_ else "failed"} ({fmt(r05)} at 0.5 against '
                                              f'{fmt(r2_)} at 2)'}
    if k05 == 'lower' and k2 == 'exact' and r2_ is not None and r05 > r2_:
        return {'verdict': True, 'text': f'holds (the lower bound {fmt(r05)} at 0.5 above {fmt(r2_)} at 2)'}
    return {'verdict': None, 'text': f'not judged (0.5 Gbit/s: {k05 or "–"} {fmt(r05)}; 2 Gbit/s: {k2 or "–"} '
                                     f'{fmt(r2_)})'}


def warm_reading(cells, ctx, missed):
    """Row 7's cause from the warm block, in order: 7a if warm v1proxy also misses against warm full; 7b only on
    positive evidence (warm meets, and on each number it missed cold its warm net value is below its own cold net
    value at 2 Gbit/s by more than δ); 7c in every other case (A4 on the warm runs, a short warm cell)."""
    if (ctx.get('a4') or {}).get('warm') != 'pass':
        return '7c', 'A4 on the warm runs: ' + str((ctx.get('a4') or {}).get('warm'))
    warm = ('warm', WARM_SPEED, PROXY)
    meets = cells.meets(warm)
    if meets is None:
        return '7c', 'the warm reading cannot be judged (a warm cell short of 3 runs, or no null)'
    if meets is False:
        return '7a', 'warm v1proxy also misses against warm full'
    cold = cells.compare(warm, ('busy', WARM_SPEED, PROXY),
                         full_keys=[('warm', WARM_SPEED, 'full'), ('busy', WARM_SPEED, 'full')])
    if cold is None:
        return '7c', 'warm meets, but its own cold cell at 2 Gbit/s cannot be judged'
    below = {role: cold[role]['diff'] < -cold[role]['delta'] - 1e-12 for role in missed}
    if all(below.values()):
        return '7b', 'warm meets, and below its cold value by more than δ on ' + ', '.join(missed)
    return '7c', 'warm meets without falling below its cold value by more than δ on ' + ', '.join(
        role for role, ok in below.items() if not ok)


def proxy_reading(cells, ctx, block='busy', speeds=REGISTERED):
    """Decision 1's rows 4-7 for a block: whether its proxy can be judged, and if so what it shows (decision 2 reads
    it too). A judged miss counts before a short cell; A4 undecided keeps a miss and drops a meet."""
    reasons = []
    pool_block = 'busy' if block == 'warm' else block
    if blocked(ctx, pool_block):
        reasons.append(blocked(ctx, pool_block))
    a4 = (ctx.get('a4') or {}).get(PROXY)
    if a4 == 'fail':
        reasons.append('A4 fires on v1proxy')
    judged = {speed: cells.meets((block, speed, PROXY)) for speed in speeds}
    missed = {speed: cells.missed((block, speed, PROXY)) or [] for speed in speeds if judged[speed] is False}
    if reasons:
        return {'row': D1_ROWS[3], 'why': '; '.join(reasons), 'judged': judged}
    short = [speed for speed, value in judged.items() if value is None]
    if not missed and short:
        return {'row': D1_ROWS[3], 'why': 'a v1proxy or full cell short of 3 runs (or without a null) at '
                                          + ', '.join(map(speed_name, short)) + ', and no judged miss',
                'judged': judged}
    if not missed and a4 != 'pass':
        return {'row': D1_ROWS[3], 'why': f'A4 on v1proxy is {a4 or "not known"}, and no judged miss',
                'judged': judged}
    # Row 5 before rows 6 and 7, in the registered order (every one of its cells judged and nothing measurable).
    if all(cells.measurable(block, speed, arm, role) is False for speed in speeds for arm in ('full', PROXY)
           for role in ('W', 'D')):
        return {'row': D1_ROWS[4], 'why': "neither full's nor v1proxy's W or D is measurable", 'judged': judged}
    if not missed:
        return {'row': D1_ROWS[5], 'why': 'v1proxy meets O4 at ' + ', '.join(map(speed_name, speeds)),
                'judged': judged}
    roles = sorted({role for found in missed.values() for role in found}, key=('W', 'D').index)
    out = {'row': D1_ROWS[6], 'why': 'v1proxy misses at ' + ', '.join(f'{speed_name(s)} (on {", ".join(m)})'
                                                                      for s, m in missed.items()),
           'judged': judged, 'missed': missed, 'roles': roles}
    if block == 'busy':
        out['cause'], out['cause_why'] = warm_reading(cells, ctx, roles)
    return out


def decision1(cells, ctx, r1v=None, r2v=None):
    """Decision 1 (the T5/T8 order), its rows checked in order; the proxy's reading (rows 4-7) always beside it."""
    r1v = r1v or r1(cells, ctx)
    r2v = r2v or r2(cells, ctx)
    proxy = proxy_reading(cells, ctx)
    if ctx.get('r3'):
        row = D1_ROWS[0]
    elif r1v['verdict'] is True:
        row = D1_ROWS[1]
    elif r2v['verdict'] is True:
        row = D1_ROWS[2]
    else:
        row = proxy['row']
    return {'row': row, 'cause': proxy.get('cause') if row == D1_ROWS[6] else None, 'proxy': proxy, 'r1': r1v,
            'r2': r2v}


def decision2(cells, ctx, proxy=None, block='busy', speeds=REGISTERED):
    """Decision 2 (a cap on v1's source workers), judged whenever the proxy is, its rows in order: No cap, Cap fails,
    Cap meets, Cap not judged."""
    proxy = proxy or proxy_reading(cells, ctx, block, speeds)
    if proxy['row'] == D1_ROWS[3]:
        return {'row': None, 'why': 'not judged: the proxy is not (' + proxy['why'] + ')'}
    if proxy['row'] in (D1_ROWS[4], D1_ROWS[5]):
        return {'row': 'No cap', 'why': proxy['why']}
    a4 = (ctx.get('a4') or {}).get(PROXY_S1)
    judged = {speed: cells.meets((block, speed, PROXY_S1)) for speed in speeds}
    if a4 != 'fail' and any(v is False for v in judged.values()):
        return {'row': 'Cap fails', 'why': 'v1proxy_s1 misses at ' + ', '.join(
            speed_name(s) for s, v in judged.items() if v is False), 'judged': judged}
    if a4 == 'pass' and all(v is True for v in judged.values()):
        return {'row': 'Cap meets', 'why': 'v1proxy_s1 meets at ' + ', '.join(map(speed_name, speeds)),
                'judged': judged}
    why = []
    if a4 != 'pass':
        why.append(f'A4 on v1proxy_s1: {a4 or "not known"}')
    short = [speed_name(s) for s, v in judged.items() if v is None]
    if short:
        why.append('its cell short of 3 runs (or without a null) at ' + ', '.join(short))
    return {'row': 'Cap not judged', 'why': '; '.join(why) or 'v1proxy_s1 cannot be judged', 'judged': judged}


def outcome(cells, ctx):
    """What fragile and near the line compare: decision 1's row (with its cause), decision 2's row, R2's verdict."""
    d1 = decision1(cells, ctx)
    d2 = decision2(cells, ctx, d1['proxy'])
    return (d1['row'], d1['cause'], d2['row'], d1['r2']['verdict'])


def a5(cells, ctx):
    """A5: W_net(capture) at least half of W_net(pgdelta) in every cell where pgdelta's W is measurable."""
    reason = blocked(ctx, 'busy')
    if reason:
        return {'verdict': None, 'text': f'not judged ({reason})'}
    capture = cells.cell('busy', '2g', 'capture')
    if capture is None or capture['short'] or capture.get('W_net') is None:
        return {'verdict': None, 'text': 'not judged (no judged capture cell)'}
    checked = []
    for speed in SPEEDS:
        p = cells.cell('busy', speed, 'pgdelta')
        if p is None or p['short'] or not p.get('W_measurable'):
            continue
        checked.append((speed, p['W_net'], capture['W_net'] >= 0.5 * p['W_net']))
    if not checked:
        return {'verdict': None, 'text': "not judged (pgdelta's W measurable in no cell)"}
    verdict = all(ok for *_, ok in checked)
    detail = ', '.join(f'{speed_name(s)} {fmt(capture["W_net"], 3)} of {fmt(w, 3)} s' for s, w, _ in checked)
    return {'verdict': verdict, 'text': ('"Most of staged pgdelta\'s slowdown is its capture, which v1 removes"'
                                         if verdict else '"Most is its transfer"') + f' ({detail})'}


def a6(cells, ctx):
    """A6: v1proxy_s1's and burn4's W_net per calibrated CPU-s each at most half of v1proxy's, at 0.5 and 2 Gbit/s,
    judged where v1proxy's W is measurable and both of the others' values exist (a short cell or a missing
    calibrated CPU-s leaves that speed not judged); an unmeasurable W counts at its bound."""
    reason = blocked(ctx, 'busy')
    if reason:
        return {'verdict': None, 'text': f'not judged ({reason})'}
    cpu = ctx.get('cpu') or {}

    def per_cpu(cell, arm):
        if cell is None or cell['short'] or not cpu.get(arm):
            return None
        value = cell['W_net'] if cell.get('W_measurable') else cell.get('W_bound')
        return None if value is None else value / cpu[arm]
    burn = per_cpu(cells.cell('busy', None, 'burn4'), 'burn4')
    parts, oks = [], []
    for speed in REGISTERED:
        proxy = cells.cell('busy', speed, PROXY)
        if proxy is None or proxy['short'] or not proxy.get('W_measurable') or not cpu.get(PROXY):
            continue
        rate = proxy['W_net'] / cpu[PROXY]
        one = per_cpu(cells.cell('busy', speed, PROXY_S1), PROXY_S1)
        # Both parts of the registered condition, or this speed isn't judged (a short cell, no calibrated CPU-s):
        # burn4's alone would give "cores" on half the rule.
        ok = None if one is None or burn is None else one <= rate / 2 and burn <= rate / 2
        oks.append(ok)
        parts.append(f'{speed_name(speed)}: v1proxy {fmt(rate, 4)}, v1proxy_s1 {fmt(one, 4)}, burn4 {fmt(burn, 4)} '
                     's per CPU-s')
    verdict = every(oks)
    if verdict is None:
        return {'verdict': None, 'text': "not judged (v1proxy's W measurable at neither registered speed, or a "
                                         'short cell): D81 stays D' + (f' ({"; ".join(parts)})' if parts else '')}
    return {'verdict': verdict, 'text': ('"Loss follows how many cores run at once, not CPU-seconds": D81\'s budget is '
                                         'stated in cores' if verdict else
                                         '"CPU-seconds is an adequate unit at this load"') + f' ({"; ".join(parts)})'}


def ha_caveat(cells, ctx):
    """Beside the decisions: v1proxy misses O4 unthrottled in the busy block."""
    reason = blocked(ctx, 'busy')
    if reason:
        return None, reason
    if (ctx.get('a4') or {}).get(PROXY) == 'fail':
        return None, 'A4 fires on v1proxy'
    meets = cells.meets(('busy', U, PROXY))
    return (None if meets is None else not meets), ('a short cell' if meets is None else '')


def near_line(cells, ctx, base):
    """Each deciding difference within ±0.25 x δ of its δ, and each rate ratio within ±0.15 of 1.5 or 1/1.5, whose
    crossing, alone or with other near ones, would change decision 1's row or cause, decision 2's row, or R2's verdict
    (EXP-11's reading). A ratio is R2's exact one (either line), or its bound where one W isn't measurable: the
    lower bound against 1.5 and the upper against 1/1.5, the only line each decides at."""
    near = []
    for a_key, b_key, full_keys in dict.fromkeys(cells.consulted):
        found = cells.compare(a_key, b_key, full_keys and list(full_keys))
        if not found:
            continue
        for role in ('W', 'D'):
            diff, delta = found[role]['diff'], found[role]['delta']
            if abs(diff - delta) <= NEAR * delta + 1e-12:
                near.append(('diff', (a_key, b_key, role), diff, delta))
    lines_of = {'exact': (TRACK, 1 / TRACK), 'lower': (TRACK,), 'upper': (1 / TRACK,)}
    for speed in REGISTERED:
        kind, value, _ = rate_ratio(cells, ctx, speed)
        if kind in lines_of and value is not None and math.isfinite(value):
            for line in lines_of[kind]:
                if abs(value - line) <= RATIO_NEAR + 1e-12:
                    near.append(('ratio', speed, value, line))
    cells.consulted = []

    def changes(crossed):
        saved, ctx_overrides = dict(cells.overrides), dict(ctx.get('ratio_overrides') or {})
        for kind, key, value, line in crossed:
            if kind == 'diff':
                cells.overrides[key] = line + 1e-6 if value <= line else line - 1e-6
            else:
                inside = 1 / TRACK <= value <= TRACK
                ctx_overrides[key] = (line + 1e-6 if line == TRACK else line - 1e-6) if inside else (
                    line - 1e-6 if line == TRACK else line + 1e-6)
        try:
            return outcome(cells, {**ctx, 'ratio_overrides': ctx_overrides}) != base
        finally:
            cells.overrides = saved
            cells.consulted = []
    needed = set()
    indices = range(len(near))
    sizes = range(1, len(near) + 1) if len(near) <= 8 else (1,)
    for size in sizes:
        for crossed in itertools.combinations(indices, size):
            if changes([near[i] for i in crossed]):
                needed.update(i for i in crossed if not changes([near[j] for j in crossed if j != i]))
    return [near[i] for i in sorted(needed)]


def outcome_text(found):
    row, cause, d2, r2v = found
    return (f'decision 1 {row}' + (f' ({cause})' if cause else '') + f', decision 2 {d2 or "not judged"}, R2 '
            + {True: 'tracks', False: "doesn't track", None: 'not judged'}[r2v])


def fragile(S, ctx, base):
    """The robustness views whose decisions would land in another row, or R2 in another verdict."""
    out = []
    for view in ROBUSTNESS:
        other = outcome(S.cells(view), ctx)
        if other != base:
            out.append((view, other))
    return out


# --- the context the rules read (A1-A4, A8's clock gate, A13, R3) ----------------------------------

def a2_runs(S):
    """A2 per pause2 run: W within [1.8 - (1 - ρ), 2.3] s, D at least 0.9, and its gap in the log against the agent's
    SIGSTOP and SIGCONT times (5 ms plus the settle's p99 at either end) and inside dest's phase mapped through the
    offset, within the same margin."""
    capacity = S.capacity()
    out = []
    for r in S.runs:
        if r['method'] != 'pause2' or r['block'] not in LOADED_BLOCKS or r['status'] == 'error':
            continue
        stats, control = r['stats'] or {}, get(r['record'], 'phases', measure.CONTROL_PHASE, 'control') or {}
        rho = ratio(r['rate'], capacity)
        W, D = stats.get('W'), stats.get('D')
        low = None if rho is None else 1.8 - (1 - rho)
        w_ok = None if W is None else (W <= A2_HIGH + 1e-9 and (low is None or W >= low - 1e-9))
        d_ok = None if D is None else D >= A2_D
        p99 = (r['settle'] or {}).get('p99_ms')
        margin = A2_MARGIN_S + (p99 or 0) / 1000
        gap = stats.get('gap_utc')
        stop, cont = control.get('sigstop_utc'), control.get('sigcont_utc')

        def distance(moment, interval):
            if moment is None or not interval:
                return None
            lo, hi = min(interval), max(interval)
            return 0.0 if lo <= moment <= hi else min(abs(moment - lo), abs(moment - hi))
        start_off = distance(gap[0] if gap else None, stop)
        end_off = distance(gap[1] if gap else None, cont)
        signals_ok = None if start_off is None or end_off is None else start_off <= margin and end_off <= margin
        inside = (None if not gap or r['t0'] is None or r['t1'] is None
                  else gap[0] >= r['t0'] - margin and gap[1] <= r['t1'] + margin)
        out.append({'run': r, 'block': 'busy' if r['block'] == 'warm' else r['block'], 'rho': rho, 'W': W, 'D': D,
                    'low': low, 'w_ok': w_ok, 'd_ok': d_ok, 'gap': gap, 'start_off': start_off, 'end_off': end_off,
                    'margin': margin, 'signals_ok': signals_ok, 'inside': inside,
                    'ok': every([w_ok, d_ok, signals_ok, inside]),
                    'late_L': stats.get('late_L')})
    return out


def a3_blocks(S):
    """A3 per block: none's median completed over scheduled below 0.98, or a none window ending with a backlog above R."""
    out = {}
    for block in ('busy', 'moderate'):
        runs = [r for r in S.runs if r['block'] == block and r['method'] == 'none' and r['stats'] is not None
                and r['status'] != 'error']
        if not runs:
            out[block] = {'fires': None, 'runs': 0}
            continue
        ratio_ = median(r['stats']['completed_ratio'] for r in runs)
        backlog = [r['stats']['backlog_end'] for r in runs]
        rate = runs[0]['rate']
        fires = (ratio_ is not None and ratio_ < A3_RATIO) or any(b > rate * 1.0 for b in backlog)
        out[block] = {'fires': fires, 'runs': len(runs), 'ratio': ratio_, 'backlog_max': max(backlog), 'rate': rate}
    return out


def clock_gate(S):
    """A8's per-trial clock flag: the clock probe's offset above 5 ms at the trial's start or end (or not measured)."""
    out = {}
    for key, trial in S.trials.items():
        offsets = [get(trial, 'clock', when, 'offset_s') for when in ('start', 'end')]
        fires = any(value is None or abs(value) > CLOCK_GATE_S for value in offsets)
        out[key] = {'offsets': offsets, 'fires': fires}
    return out


def a4_state(S):
    """A4 for v1proxy and v1proxy_s1: 'fail', 'undecided' or 'pass', with why; and the warm runs' gate ((i), (ii))."""
    out, why = {}, {}
    reference = {PROXY: S.calibrated(V1), PROXY_S1: None if S.calibrated(PROXY) is None else S.calibrated(PROXY) / D48}
    for arm, blocks, cpu_check in ((PROXY, ('busy', 'moderate', 'calibration'), True),
                                   (PROXY_S1, ('busy', 'moderate', 'calibration'), True),
                                   ('warm', ('warm',), False)):
        method = PROXY if arm == 'warm' else arm
        fails, unknown = [], []
        for r in S.runs:
            if r['method'] != method or r['block'] not in blocks:
                continue
            if r['aborted']:  # wrong too (R3): A4's exclusion is a second consequence
                fails.append(f'r{r["repeat"]} {r["speed"]}: its copy aborted by the tool')
                continue
            if r['status'] == 'error':
                if r['failure_kind'] is None:
                    unknown.append(f'r{r["repeat"]} {r["speed"]}: an error of unknown kind')
                continue
            held = get(r['record'], 'proxy', 'backup', 'held')
            hashed = get(r['record'], 'proxy', 'hashed', 'all_hashed')
            if hashed is False:
                fails.append(f'r{r["repeat"]} {r["speed"]}: (i) not every byte hashed')
            if held is False:
                fails.append(f'r{r["repeat"]} {r["speed"]}: (ii) backup mode not held across the transfer')
            if hashed is None:
                unknown.append(f'r{r["repeat"]} {r["speed"]}: (i) no counters')
            if held is None:
                unknown.append(f'r{r["repeat"]} {r["speed"]}: (ii) no backup record')
        if cpu_check:
            own, ref = S.calibrated(method), reference[method]
            if own is None or ref is None:
                unknown.append('(iii) no calibrated CPU-s for it or its reference')
            elif abs(own / ref - 1) > A4_CPU:
                fails.append(f'(iii) calibrated CPU-s {fmt(own)} against {fmt(ref)} ({own / ref - 1:+.0%})')
        out[arm] = 'fail' if fails else 'undecided' if unknown else 'pass'
        why[arm] = fails + unknown
    return out, why, reference


def a13_state(S):
    """A13: more than 1 run of an arm at one speed in one block failing check (a) or (b) reclassifies it: full for the
    session, any other arm at that speed for the session."""
    failing = Counter((r['method'], r['block'], r['speed']) for r in S.runs if r['durability_failures'])
    reclassified, notes = {}, []
    for (method, block, speed), count in sorted(failing.items(), key=str):
        if count > 1:
            notes.append(f'{method}: {count} runs at {speed_name(speed)} in the {block} block')
            if method == 'full':
                reclassified['full'] = True
            else:
                reclassified.setdefault(method, set()).add(speed)
    return reclassified, notes


def context(S):
    """The session's context for the rules: A2, A3 and the clock gate (blocked blocks), A4, A13, R3, calibrated CPU."""
    a2 = a2_runs(S)
    a3 = a3_blocks(S)
    clock = clock_gate(S)
    blocked_ = {}
    clock_fires = any(item['fires'] for item in clock.values())
    for block in LOADED_BLOCKS:
        base = 'busy' if block == 'warm' else block
        reasons = []
        if any(item['ok'] is False for item in a2 if item['block'] == base):
            reasons.append('not judged: instrument (A2)')
        if (a3.get(base) or {}).get('fires'):
            reasons.append('not judged: overload (A3)')
        if clock_fires:
            reasons.append('not judged: clock')
        blocked_[block] = '; '.join(reasons) or None
    a4, a4_why, a4_reference = a4_state(S)
    reclassified, a13_notes = a13_state(S)
    wrong = [r for r in S.runs if r['method'] in ('pgdelta', PROXY, PROXY_S1, V1) and r['status'] == 'fail']
    cpu = {arm: S.calibrated(arm) for arm in CALIBRATION_ARMS}
    return {'blocked': blocked_, 'a2': a2, 'a3': a3, 'clock': clock, 'a4': a4, 'a4_why': a4_why,
            'a4_reference': a4_reference, 'reclassified': reclassified, 'a13': a13_notes, 'r3': bool(wrong),
            'r3_runs': wrong, 'cpu': cpu}


# --- the report -------------------------------------------------------------------------------

def run_name(r):
    return (f'{r["trial_id"]} r{r["repeat"]} {r["block"]}' + (f' {r["control_slot"]}' if r['control_slot'] else '')
            + f' {r["speed"]} {r["method"]}')


def counts_lines(S):
    lines = ['## Runs']
    statuses = Counter(r['status'] for r in S.runs)
    blocks = Counter(r['block'] for r in S.runs)
    lines.append(f'- {len(S.runs)} runs; status ' + ', '.join(f'{k} {n}' for k, n in sorted(statuses.items(), key=str))
                 + '; block ' + ', '.join(f'{k} {n}' for k, n in sorted(blocks.items(), key=str)))
    left = Counter(r['left_out'].split(':')[0] for r in S.runs if r['left_out'])
    lines.append('- left out of every median: ' + (', '.join(f'{k} {n}' for k, n in sorted(left.items())) or 'none'))
    for r in S.runs:
        if r['aborted']:
            lines.append(f'  - wrong (its copy aborted by its tool): {run_name(r)}: {r["error"] or ""}')
        elif r['status'] == 'error':
            kind = {'tool': 'the tool', 'harness': 'the harness'}.get(r['failure_kind'], 'cause not recorded')
            lines.append(f'  - error ({kind}): {run_name(r)}: {r["error"] or ""}')
        elif r['void']:
            lines.append(f'  - void: {run_name(r)}: {r["record"].get("void_reason") or "the load died"}')
    return lines


def load_lines(S):
    lines = ['## The load (Setup; smoke items 1-3)']
    for (sweep, trial_id), trial in sorted(S.trials.items()):
        load = trial.get('load') or {}
        capacity = (load.get('capacity') or {}).get('capacity_tps')
        rates = {block: S.rate(block) for block in ('busy', 'moderate')}
        lines.append(f'### {trial_id}')
        lines.append(f'- C (step 7) {fmt(capacity, 0)} transactions per second; R_busy {fmt(rates["busy"], 0)} '
                     f'(ρ = {fmt(ratio(rates["busy"], capacity), 3)}), R_mod {fmt(rates["moderate"], 0)} '
                     f'(ρ = {fmt(ratio(rates["moderate"], capacity), 3)}); the source\'s busy CPU at each '
                     f'{fmt(S.busy_cpu("busy"), 3)} and {fmt(S.busy_cpu("moderate"), 3)} of its {S.cpus()} CPUs '
                     '(the settles\' median' + (', the busy one given by --busy-cpu' if S.busy_cpu_given is not None
                                                else '') + ')')
        lines += ['| run | kind | rate | commits | complete | failed | log lines | commits logged | aborts | failed '
                  'lines | latency under lag | not rising | MB per hour |', '|---|---|---:|---:|---|---:|---:|---:|---:|'
                  '---:|---:|---:|---:|']
        for run in load.get('runs') or []:
            tally = S.tallies.get((sweep, trial_id, run['run_id'])) or {}
            hours = None
            if tally.get('first_end_us') and tally.get('last_end_us'):
                hours = (tally['last_end_us'] - tally['first_end_us']) / SECOND_US / 3600
            per_hour = None if not hours else tally['bytes'] / 1e6 / hours
            lines.append(f'| {run["run_id"]} | {run.get("kind")} | {run.get("rate") or "closed"} | '
                         f'{run.get("commits_total")} | {yes(run.get("complete"))}'
                         + (' (SIGINT\'s rule: every line up to 1 s before the signal)'
                            if get(run, 'completeness', 'criterion') == 'sigint' else '') + f' | {run.get("failed")} | '
                         f'{tally.get("lines", "–")} | {tally.get("commits", "–")} | {tally.get("aborts", "–")} | '
                         f'{tally.get("failed", "–")} | {tally.get("lag_over_latency", "–")} | '
                         f'{tally.get("not_rising", "–")} | {fmt(per_hour, 1)} |'
                         + (f' missing: {", ".join(tally["missing"])}' if tally.get('missing') else '')
                         + (f' {tally["error"]}' if tally.get('error') else ''))
        for warmup in S.warmups.get((sweep, trial_id)) or []:
            whole, pool = warmup['whole'] or {}, warmup['pool'] or {}
            lines.append(f'- warm-up of {warmup["run_id"]}: p99 {fmt(whole.get("p99_ms"), 3)} ms over its '
                         f'{fmt(whole.get("seconds"), 0)} s, {fmt(pool.get("p99_ms"), 3)} ms over its last minute (the '
                         'null pool\'s part)')
        for restart in load.get('restarts') or []:
            lines.append(f'- restart: {restart.get("died")} died; {restart.get("restart")} started; '
                         f'voided {restart.get("voided")}')
        for death in load.get('died_before_stop') or []:
            lines.append(f'- {death.get("died")} had died before its planned stop; voided {death.get("voided")}')
        check = get(load, 'end', 'check') or {}
        lines.append(f'- load-check (step 15): {word(check.get("ok"))} (Σabalance {check.get("sum_abalance")}, Σn '
                     f'{check.get("sum_n")}, logged commits {check.get("expected_commits")}; incomplete '
                     f'{check.get("incomplete") or "none"}; gaps {check.get("gaps") or "none"})')
    return lines


def oracle_cells(runs):
    """Counts of each oracle check's verdicts over live outputs: {check: Counter}."""
    out = {check: Counter() for check in ('bytes', *measure.ORACLE_CHECKS)}
    for r in runs:
        oracle = get(r['record'], 'verification', 'oracle') or {}
        for check in out:
            out[check][oracle.get(check)] += 1
    return out


def g4_lines(S, ctx):
    lines = ['## G4 and R3: every output taken under load',
             '(G4\'s boundaries per live output, medians: the capture\'s end (its write-ahead log end point) before '
             'publication (the clock\'s stop), usable after publication (the start\'s time to consistency), and '
             'staleness, publication less the cut.)',
             '| Block | Arm | runs | ' + ' | '.join(('bytes', *measure.ORACLE_CHECKS))
             + ' | static digest | amcheck relations | capture end before publication s | usable after s | staleness s '
               '(max) |', '|---|---|---:|' + '---|' * 13]
    for block in BLOCKS:
        for arm in LIVE:
            runs = [r for r in S.runs if r['block'] == block and r['method'] == arm and r['status'] != 'error']
            if not runs:
                continue
            counts = oracle_cells(runs)
            cells = ' | '.join(' / '.join(f'{n} {k or "pending"}' for k, n in sorted(counts[check].items(), key=str))
                               for check in ('bytes', *measure.ORACLE_CHECKS))
            digests = sum(bool(get(r['record'], 'verification', 'oracle', 'static_digest')) for r in runs)
            relations = sorted({get(r['record'], 'verification', 'oracle', 'amcheck_relations') for r in runs}, key=str)
            stale = [get(r['record'], 'g4', 'staleness_s') for r in runs]
            g4 = [r['record'].get('g4') or {} for r in runs]
            ahead = [None if get(g, 'capture_end', 'utc') is None or g.get('publication_utc') is None
                     else g['publication_utc'] - g['capture_end']['utc'] for g in g4]
            usable = [None if g.get('usable_utc') is None or g.get('publication_utc') is None
                      else g['usable_utc'] - g['publication_utc'] for g in g4]
            lines.append(f'| {block} | {arm} | {len(runs)} | {cells} | {digests} | {", ".join(map(str, relations))} | '
                         f'{fmt(median(ahead))} | {fmt(median(usable))} | {fmt(median(stale))} ({fmt(mx(stale))}) |')
    lines.append('| Block | Arm | runs | pass / fail / unverified / pending / error |')
    lines.append('|---|---|---:|---|')
    for block in BLOCKS:
        for arm in [*ARM_NAMES, V1]:
            if arm in LIVE:
                continue
            runs = [r for r in S.runs if r['block'] == block and r['method'] == arm]
            if runs:
                count = Counter(r['status'] for r in runs)
                lines.append(f'| {block} | {arm} | {len(runs)} | {count["pass"]} / {count["fail"]} / '
                             f'{count["unverified"]} / {count["pending"]} / {count["error"]} |')
    wrong = ctx['r3_runs']
    lines.append(f'- **R3** ("Any wrong output stops speed work": one wrong output of pgdelta, v1proxy, v1proxy_s1 or '
                 f'v1 itself, a copy its own tool aborted included, since it doesn\'t start): '
                 f'{"FIRES" if wrong else "does not fire"}'
                 + (': ' + '; '.join(f'{run_name(r)} ({"aborted by its tool: " if r["aborted"] else ""}{r["error"]})'
                                     for r in wrong) if wrong else ''))
    # Unverified: a check couldn't run for the harness's reason, and an error of the harness around an output.
    unverified_runs = [r for r in S.runs if r['status'] == 'unverified' or harness_error(r)]
    unverified = Counter((r['block'], cell_speed(r), r['method']) for r in unverified_runs)
    flagged = [key for key, count in unverified.items() if count > 1]
    lines.append('- unverified runs (a harness error around an output included): '
                 + (', '.join(run_name(r) + (' (error)' if r['status'] == 'error' else '') for r in unverified_runs)
                    or 'none') + ('; more than 1 in a cell flags it: ' + ', '.join(
                        f'{b} {speed_name(s)} {m}' for b, s, m in flagged) if flagged else ''))
    return lines


def coverage_control(S):
    """A1's analysis-only control: each live output's cut checked against another repeat's window (the next output
    of its arm in its trial, or the one before the last) must fail (v). [(run, other, ok)], ok True when it does fail.

    Only outputs taken under the load, whose cut has both ends: without the load a copy lacks no commit, so its cut
    is open after its last held one and any later window would cover it."""
    out = []
    for arm in LIVE:
        runs = [r for r in S.runs if r['method'] == arm and r['block'] in LOADED_BLOCKS
                and None not in (get(r['record'], 'g4', 'cut_utc') or [None])
                and get(r['record'], 'g4', 'coverage_window_utc')]
        for index, r in enumerate(runs):
            others = [o for o in runs if o['trial_id'] == r['trial_id'] and o is not r]
            if not others:
                continue
            following = [o for o in runs[index + 1:] if o in others]
            other = following[0] if following else others[-1]
            lo, hi = get(r['record'], 'g4', 'cut_utc')
            cut = {'lo_us': None if lo is None else lo * 1e6, 'hi_us': None if hi is None else hi * 1e6}
            window = get(other['record'], 'g4', 'coverage_window_utc')
            found = measure.coverage(cut, window[0], window[1], other['offset'] or 0.0)
            out.append((r, other, not found['ok']))
    return out


def a1_lines(S, ctx):
    lines = ['## A1: the oracle']
    wrong_full = [r for r in S.runs if r['method'] == 'full' and r['status'] == 'fail']
    lines.append('- a wrong `full` output: ' + ('**yes**: G4\'s checks are void for the session until it is '
                                                'explained (' + '; '.join(run_name(r) for r in wrong_full) + ')'
                                                if wrong_full else 'none'))
    control = coverage_control(S)
    passed = [(r, o) for r, o, ok in control if not ok]
    lines.append(f'- the coverage control (analysis only: each live output\'s cut against another repeat\'s window must '
                 f'fail (v)): {len(control)} checked, ' + (f'**{len(passed)} passed** (A1 fires: '
                                                          + '; '.join(f'{run_name(r)} against {run_name(o)}'
                                                                      for r, o in passed) + ')' if passed
                                                          else 'every one failed (v), as it must'))
    for (sweep, trial_id), trial in sorted(S.trials.items()):
        controls = (trial.get('oracle_controls') or {}).get('controls') or []
        if not controls:
            continue
        lines.append(f'- the oracle\'s negative controls in {trial_id} (smoke item 5):')
        lines += ['| control | must fail through | must pass | failed | ok |', '|---|---|---|---|---|']
        for control_ in controls:
            verdict = control_.get('verdict') or {}
            lines.append(f'| {control_.get("name")} | {verdict.get("target", "– (the good copy passes every check)")} '
                         f'| {", ".join(verdict.get("must_pass") or []) or "–"} | '
                         f'{", ".join(verdict.get("failed") or []) or verdict.get("status") or "–"} | '
                         f'{yes(verdict.get("ok"))} |')
        bad = [c.get('name') for c in controls if (c.get('verdict') or {}).get('ok') is not True]
        lines.append('  - ' + (f'**A1 fires**: {", ".join(bad)} did not behave; the sweep waits until the oracle or '
                               'the harness is fixed' if bad else 'every control behaved'))
    good = [r for r in S.runs if r['method'] in LIVE and r['status'] == 'fail']
    lines.append('- a good output that fails any check (in the smoke run, blocks the sweep): '
                 + (', '.join(run_name(r) for r in good) if good else 'none'))
    return lines


def durability_lines(S, ctx):
    lines = ['## Durability on dest (checks (a) and (b); A13; the negative control)']
    noise = [run.get('idle_noise_bytes') for run in S.meta if run.get('idle_noise_bytes') is not None]
    lines.append(f'- check (a)\'s limit: max(4 MiB, the idle noise: ' + (', '.join(f'{v / (1 << 20):.2f} MiB'
                                                                             for v in noise) or '–')
                 + (f'; judged again with --idle-noise-bytes {S.idle_noise / (1 << 20):.2f} MiB'
                    if S.idle_noise is not None else '') + '); check (a) '
                 + ('VOID for the session (full_nosync passed it, or left less than 1.5 GB)' if S.a_void
                    else 'in force'))
    lines += ['| Block | Arm | runs | (a) pass / fail / unchecked | (b) pass / fail / unchecked | largest increase, MB | '
              'smallest md writes ÷ output |', '|---|---|---:|---|---|---:|---:|']
    for block in BLOCKS:
        for arm in [*ARM_NAMES, V1, *measure.NON_DURABLE_METHODS]:
            runs = [r for r in S.runs if r['block'] == block and r['method'] == arm and r['status'] != 'error']
            if not runs or arm in measure.NO_DEST_WRITE_METHODS:
                continue
            a = Counter(get(r['record'], 'durability', 'a_ok') for r in runs)
            b = (Counter(get(r['record'], 'durability', 'b_ok') for r in runs)
                 if arm in measure.DISK_WRITE_CHECK_METHODS else None)
            largest = mx(get(r['record'], 'durability', 'dirty_writeback_increase_bytes') for r in runs)
            ratios = [get(r['record'], 'durability', 'md_write_ratio') for r in runs]
            lines.append(f'| {block} | {arm} | {len(runs)} | {a[True]} / {a[False]} / {a[None]} | '
                         + (f'{b[True]} / {b[False]} / {b[None]}' if b else 'n/a') + ' | '
                         f'{fmt(None if largest is None else largest / 1e6, 1)} | {fmt(min((v for v in ratios if v is not None), default=None), 3)} |')
    for r in S.runs:
        if r['durability_failures']:
            lines.append(f'  - {run_name(r)}: {", ".join(r["durability_failures"])} failed; left out of every median')
    lines.append('- **A13** (more than 1 run of an arm at one speed in one block fails (a) or (b)): '
                 + ('fires: ' + '; '.join(ctx['a13']) + ' (full: not durable for the session, R1 counts every speed as '
                                                        'a win for pgdelta; any other arm: at that speed)'
                    if ctx['a13'] else 'does not fire'))
    return lines


def instrument_lines(S, ctx):
    lines = ['## The instrument and the load (A2, A3, the clock gate)']
    lines += ['| pause2 run | ρ | W s (bounds) | D | gap in the log (source UTC) | from SIGSTOP / SIGCONT, ms | margin, ms '
              '| inside the mapped phase | late count at L, s | A2 |', '|---|---:|---|---:|---|---|---:|---|---:|---|']
    for item in ctx['a2']:
        r = item['run']
        gap = item['gap']
        lines.append(f'| {run_name(r)} | {fmt(item["rho"], 3)} | {fmt(item["W"], 3)} ([{fmt(item["low"])}, '
                     f'{A2_HIGH}]) | {fmt(item["D"], 3)} | '
                     + (f'{gap[0]:.3f} to {gap[1]:.3f}' if gap else '–') + ' | '
                     f'{fmt(None if item["start_off"] is None else item["start_off"] * 1000, 2)} / '
                     f'{fmt(None if item["end_off"] is None else item["end_off"] * 1000, 2)} | '
                     f'{fmt(item["margin"] * 1000, 2)} | {yes(item["inside"])} | {fmt(item["late_L"], 2)} | '
                     f'{word(item["ok"])} |')
    for block in ('busy', 'moderate'):
        items = [item for item in ctx['a2'] if item['block'] == block]
        fires = any(item['ok'] is False for item in items)
        unchecked = [item for item in items if item['ok'] is None]
        lines.append(f'- **A2** ({block} block): ' + ('**fires**: the block\'s loss verdicts are "not judged: '
                                                       'instrument"' if fires else 'does not fire' if items
                                                       else 'no pause2 run: the instrument is unchecked')
                     + (f'; {len(unchecked)} pause2 run(s) without every reading (no data)' if unchecked else ''))
    for block, a3 in ctx['a3'].items():
        if a3.get('runs'):
            lines.append(f'- **A3** ({block} block; none\'s median completed over scheduled {fmt(a3["ratio"], 4)}, '
                         f'its largest window-end backlog {a3["backlog_max"]} against R = {fmt(a3["rate"], 0)}): '
                         + ('**fires**: the block was overloaded; its O4 rows are not judged' if a3['fires']
                            else 'does not fire'))
        else:
            lines.append(f'- **A3** ({block} block): no none run')
    for (sweep, trial_id), item in sorted(ctx['clock'].items()):
        lines.append(f'- the clock gate ({trial_id}; the probe\'s offset at the start and end, ms: '
                     + ' / '.join(fmt(None if v is None else v * 1000, 3) for v in item['offsets']) + '): '
                     + ('**fires**: the trial\'s loss verdicts are "not judged: clock"' if item['fires']
                        else 'within 5 ms'))
    return lines


def pool_null(S, name, T, n=1, stat='W'):
    """The null of the median of n windows at T from a block's pool, for E2 and smoke item 3."""
    pool = S.pool(name)
    if pool is None or not pool.usable:
        return None
    windows = [pool.windows(f'pool-check/{T}/{n}/{index}', T, T)[stat] for index in range(n)]
    return np.median(np.stack(windows, axis=1).astype(float), axis=1)


def pool_latency(S, name):
    """The null's latencies: every transaction scheduled in the block's pool spans."""
    parts = []
    for r in S.runs:
        if S.pool_name(r) != name:
            continue
        spans = S.spans.get((r['sweep'], r['trial_id'])) or {}
        key = (r['repeat'], r['speed'], r['method'])
        span = spans.get(('none', key)) if r['method'] == 'none' else (
            spans.get(('settle', key)) if S.settled(r) else None)
        if span is not None and span.latency is not None:
            parts.append(span.latency)
    for trial_key, warmups in S.warmups.items():
        for warmup in warmups:
            if warmup['span'] is not None and S.warmup_pool(trial_key, warmup) == name:
                parts.append(warmup['span'].latency)
    return latency_summary(np.concatenate(parts) if parts else [])


def null_lines(S):
    lines = ['## The null (block bootstrap of the load-only time)']
    for name in ('busy', 'moderate'):
        pool = S.pool(name)
        if pool is None:
            lines.append(f'- {name}: no load-only time')
            continue
        unsettled = S.pool(name, 'unsettled')
        more = (unsettled.seconds if unsettled is not None else pool.seconds) - pool.seconds
        latency = pool_latency(S, name)
        lines.append(f'- {name} (the warm block\'s too)' * (name == 'busy') + (f'- {name}' if name != 'busy' else '')
                     + f': {pool.spans} spans, {pool.seconds} s of 1-s buckets ({more} s '
                       f'more with the unsettled settles); {S.resamples} resamples from seed {S.seed}; its latencies: '
                       f'p50 {fmt(latency["p50_ms"], 3)}, p95 {fmt(latency["p95_ms"], 3)}, p99 {fmt(latency["p99_ms"], 3)},'
                       f' p99.9 {fmt(latency["p999_ms"], 3)}, max {fmt(latency["max_ms"], 3)} ms over '
                       f'{latency["transactions"]} transactions')
        for T in (15, 30, 50):
            W, D = pool_null(S, name, T), pool_null(S, name, T, stat='D')
            if W is None:
                continue
            lines.append(f'  - one window at T = {T} s: W median {fmt(float(np.median(W)), 4)} s, 95th percentile '
                         f'{fmt(float(np.percentile(W, PERCENTILE)), 4)} s; D median {fmt(float(np.median(D)), 4)}, '
                         f'95th percentile {fmt(float(np.percentile(D, PERCENTILE)), 4)}')
        W5 = pool_null(S, name, 15, 5)
        if W5 is not None:
            lines.append(f'  - the median of 5 windows at T = 15 s: W\'s 95th percentile {fmt(float(np.percentile(W5, PERCENTILE)), 4)} s')
    return lines


def cell_table(S, cells, blocks=LOADED_BLOCKS):
    lines = ['| Block | Speed | Arm | n (of) | time s | W s med (max) | W null med / p95 | W_net | meas. | D med (max) | '
             'D null med / p95 | D_net | meas. | ΔW / δ_W | ΔD / δ_D | meets | p95 ms | worst-5-s p99 ms | flags |',
             '|---|---|---|---|---:|---|---|---:|---|---|---|---:|---|---|---|---|---:|---:|---|']
    for block, speed, arm in cell_keys(blocks):
        cell = cells.cell(block, speed, arm)
        total = len([r for r in S.runs if r['block'] == block and r['method'] == arm
                     and (speed is None or r['speed'] == speed)])
        if cell is None:
            if total:
                lines.append(f'| {block} | {speed_name(speed)} | {arm} | 0 ({total}) | | | | | | | | | | | | '
                             'not judged | | | |')
            continue
        found = cells.compare((block, speed, arm)) if arm != 'full' and arm not in CONTROLS else None
        meets = None if found is None else all(found[k]['diff'] <= found[k]['delta'] + 1e-12 for k in 'WD')
        top_w = mx(r['stats'][cell['W_stat']] for r in cell['runs'] if r['stats']) if cell.get('W_stat') else None
        top_d = mx(r['stats'][cell['D_stat']] for r in cell['runs'] if r['stats']) if cell.get('D_stat') else None
        flags = Counter(name for r in cell['runs'] for name in S.flagged(r))
        lines.append(
            f'| {block} | {speed_name(speed)} | {arm} | {cell["n"]} ({total}){" short" if cell["short"] else ""}'
            f' | {fmt(cell["total_s"])} | {fmt(cell.get("W"), 3)} ({fmt(top_w, 3)}) | '
            f'{fmt(cell.get("W_null_median"), 3)} / {fmt(cell.get("W_null_p95"), 3)} | {fmt(cell.get("W_net"), 3)}'
            f' | {yes(cell.get("W_measurable"))} | {fmt(cell.get("D"), 3)} ({fmt(top_d, 3)}) | '
            f'{fmt(cell.get("D_null_median"), 3)} / {fmt(cell.get("D_null_p95"), 3)} | {fmt(cell.get("D_net"), 3)}'
            f' | {yes(cell.get("D_measurable"))} | '
            + (f'{found["W"]["diff"]:+.3f} / {found["W"]["delta"]:.3f} | {found["D"]["diff"]:+.3f} / '
               f'{found["D"]["delta"]:.3f}' if found else '– | –')
            + f' | {yes(meets)} | {fmt(cell.get("lat_p95_ms"), 2)} | {fmt(cell.get("p99_worst5s_ms"), 2)} | '
            + (', '.join(f'{k} {n}' for k, n in sorted(flags.items())) or '–')
            + (' **cell flagged**' if cell['flag_cell'] else '') + ' |')
    cells.consulted = []
    return lines


def cell_lines(S):
    lines = ['## The cells: O4\'s two numbers, their null, net values, δ against `full` and "meets"',
             '(Medians of the counting runs; a cell with fewer than 3 runs (2 for a control) is short and not judged; '
             'δ_W = max(0.05 s, the centred spread, 0.15 x W_net(full)), δ_D = max(0.03, the centred spread).)']
    return lines + cell_table(S, S.cells('main'))


def ledger_lines(S, cells, ctx, d1):
    lines = ['## The ledger\'s prediction and rules (registered; busy block, medians of 5 runs per cell)']
    say = lines.append
    r1v, r2v = d1['r1'], d1['r2']
    l1 = r2v['verdict']
    say(f'- **L1** ("the slowdown tracks source CPU"; by R2): '
        + ('holds (tracks)' if l1 is True else 'failed (doesn\'t track)' if l1 is False
           else f'not judged{" (" + r2v["why"] + ")" if r2v.get("why") else ""}'))
    value, ok = l2(ctx)
    under = {speed: median(r['cpu_excess'] for r in cells.cell('busy', speed, 'pgdelta')['runs'])
             if cells.cell('busy', speed, 'pgdelta') else None for speed in REGISTERED}
    under_f = {speed: median(r['cpu_excess'] for r in cells.cell('busy', speed, 'full')['runs'])
               if cells.cell('busy', speed, 'full') else None for speed in REGISTERED}
    say(f'- **L2** ("pgdelta uses about 4 times the full copy\'s source CPU": calibrated CPU-s of pgdelta over full\'s '
        f'within [3, 5]): {word(ok)} ({fmt(ctx["cpu"].get("pgdelta"))} over {fmt(ctx["cpu"].get("full"))} = '
        f'{fmt(value)}); under load, beside: ' + ', '.join(
            f'{speed_name(s)} {fmt(ratio(under[s], under_f[s]))} ({fmt(under[s])} over {fmt(under_f[s])} CPU-s)'
            for s in REGISTERED))
    say(f'- **L3** ("its total throughput loss is 2-4 times the full copy\'s at 2 Gbit/s"): {l3(cells, ctx)["text"]}')
    say(f'- **L4** ("and still higher at 0.5 Gbit/s"): {l4(cells, ctx)["text"]}')
    live = [r for r in S.runs if r['status'] != 'error' or harness_error(r)]
    wrong = [r for r in live if r['status'] == 'fail']  # a copy its own tool aborted included (aborted_copy)
    unknown = [r for r in live if r['status'] in ('unverified', 'pending') or harness_error(r)]
    l5 = False if wrong else (None if unknown else True)
    say(f'- **L5** ("Every output is correct": every run of every arm verified, none wrong; G4\'s checks for full and '
        f'pgdelta): ' + ('failed: ' + '; '.join(run_name(r) for r in wrong) if wrong else
                         f'not shown: {len(unknown)} run(s) unverified or pending' if unknown else 'holds')
        + f' ({len(live)} runs)')
    if r1v['verdict'] is None:
        r1_text = f'not judged ({r1v.get("why")})'
    else:
        r1_text = ('**fires**: staged pgdelta\'s loss is higher where it wins on time, at '
                   + ', '.join(map(speed_name, r1v['higher'])) if r1v['verdict']
                   else 'does not fire: no higher where it wins on time')
        r1_text += ' (' + '; '.join(f'{speed_name(s)}: ΔW {v["diff"]:+.3f} s against δ_W {v["delta"]:.3f}'
                                    for s, v in r1v['values'].items()) + ')'
    times = '; '.join(f'{speed_name(s)} pgdelta {fmt((cells.cell("busy", s, "pgdelta") or {}).get("total_s"))} s, full '
                      f'{fmt((cells.cell("busy", s, "full") or {}).get("total_s"))} s' for s in REGISTERED)
    say(f'- **R1** (under load, the speeds where pgdelta\'s median total_s is below full\'s: {", ".join(map(speed_name, r1v.get("speeds") or [])) or "none"}; '
        f'{times}): {r1_text}. Recorded as "staged pgdelta: source CPU '
        + ('is' if r1v['verdict'] else 'isn\'t' if r1v['verdict'] is False else 'is (not judged)') + ' a blocker". '
        'Slowdown is reported next to time in every case (A7)')
    parts = []
    for speed, (kind, value, verdict) in r2v.get('cells', {}).items():
        parts.append(f'{speed_name(speed)}: ' + (f'{kind} {fmt(value)} → '
                                                 + ('tracks' if verdict else 'doesn\'t track' if verdict is False
                                                    else 'not judged') if kind else 'not judged'))
    r2_word = {True: 'tracks', False: 'doesn\'t track', None: 'not judged'}[r2v['verdict']]
    say(f'- **R2** (the October 24 note; the rate ratio within [1/1.5, 1.5]): **{r2_word}**'
        + (f' ({r2v["why"]})' if r2v.get('why') else f' ({"; ".join(parts)})'))
    if r2v['verdict'] is True:
        projected = []
        for speed in REGISTERED:
            full = (cells.cell('busy', speed, 'full') or {}).get('W_net')
            if full is not None:
                lo, hi, f_lo, f_hi = REGISTERED_PROJECTION
                session = ratio(ctx['cpu'].get(V1), ctx['cpu'].get('full'))
                projected.append(f'{speed_name(speed)}: {fmt(full * lo / f_hi, 3)}-{fmt(full * hi / f_lo, 3)} s '
                                 f'(this session\'s CPU-s: {fmt(None if session is None else full * session, 3)} s)')
        say('  - as registered: v1\'s loss projected as W_net(full) x 5.3-6.0 / 1.6-1.9, 2.8-3.75 times full\'s, so T5 '
            'moves ahead of T8: ' + '; '.join(projected))
    else:
        say('  - as registered: "the T5/T8 order waits for v1\'s own arm", unless R1 has moved T5 (change 4)')
    return lines


def decision_lines(S, cells, ctx, d1, d2):
    lines = ['## Decision 1: the T5/T8 order (busy block, 0.5 and 2 Gbit/s; rows in order)']
    proxy = d1['proxy']
    a4 = ctx['a4']
    lines.append(f'- A4 (the proxy\'s gate): v1proxy {a4.get(PROXY)}, v1proxy_s1 {a4.get(PROXY_S1)}, the warm runs '
                 f'{a4.get("warm")}' + ''.join(f'; {arm}: ' + ', '.join(why) for arm, why in ctx['a4_why'].items()
                                               if why))
    applies = {row: row == d1['row'] for row in D1_ROWS}
    lines += ['| Row | Applies | What follows |', '|---|---|---|']
    for row in D1_ROWS:
        lines.append(f'| {row} | {"**yes**" if applies[row] else "no"} | {D1_FOLLOWS[row]} |')
    text = f'**Decision 1: {d1["row"]}.** {D1_FOLLOWS.get(d1["row"], "")}.'
    if d1['row'] == D1_ROWS[6]:
        name, follows = CAUSES[d1['cause']]
        text += f' Row {d1["cause"]}, {name}: {follows} ({proxy.get("cause_why")}).'
    lines.append(text)
    lines.append(f'- the proxy\'s reading (rows 4-7), beside it: {proxy["row"]}: {proxy["why"]}'
                 + (f'; warm reading {proxy["cause"]} ({CAUSES[proxy["cause"]][0]}): {proxy["cause_why"]}'
                    if proxy.get('cause') else ''))
    if d1['row'] in D1_ROWS[1:3] and proxy['row'] != d1['row']:
        lines.append('  - the registered row stands; the proxy is reported beside it' + (
            ', and **they disagree** (the proxy keeps T5 behind T8): both are stated; the registered row stands unless '
            'the ledger records an override under rule 8' if (proxy['row'] in D1_ROWS[4:6] or proxy.get('cause') == '7b')
            else ''))
    lines.append('## Decision 2: a cap on v1\'s source workers (rows in order)')
    lines += ['| Row | Applies | What follows |', '|---|---|---|']
    for row in D2_ROWS:
        lines.append(f'| {row} | {"**yes**" if row == d2["row"] else "no"} | {D2_FOLLOWS[row]} |')
    if d2['row'] is None:
        lines.append(f'**Decision 2: not judged.** {d2["why"]}')
    else:
        extra = ''
        if d2['row'] == 'Cap fails' and proxy.get('cause'):
            extra = f' The rate limit: {D2_LIMIT[proxy["cause"]]} (row {proxy["cause"]}).'
        lines.append(f'**Decision 2: {d2["row"]}.** {D2_FOLLOWS[d2["row"]]} ({d2["why"]}).{extra}')
    return lines


def beside_lines(S, cells, ctx, d1, d2, a5v):
    lines = ['## Beside the decisions']
    r1v = d1['r1']
    lines.append(f'- **O4 for staged pgdelta** (R1\'s verdict, recorded on D04 and D10p): '
                 + ('misses O4 where it wins on time' if r1v['verdict'] else 'no higher where it wins on time'
                    if r1v['verdict'] is False else 'not judged') + f'; A5: {a5v["text"]}')
    fires, why = ha_caveat(cells, ctx)
    found = cells.compare(('busy', U, PROXY))
    lines.append('- **HA caveat** (v1proxy misses O4 unthrottled, busy block): '
                 + ('**fires**: EXP-11\'s qualifier gets "and it slows a busy primary more than pg_basebackup does"'
                    if fires else 'does not fire' if fires is False else f'not judged ({why})')
                 + (f' (ΔW {found["W"]["diff"]:+.3f} / δ {found["W"]["delta"]:.3f}, ΔD {found["D"]["diff"]:+.3f} / δ '
                    f'{found["D"]["delta"]:.3f})' if found else ''))
    moderate_speeds = tuple(s for s in REGISTERED if cells.cell('moderate', s, PROXY) is not None) or ('2g',)
    moderate = proxy_reading(cells, ctx, 'moderate', moderate_speeds)
    moderate_d2 = decision2(cells, ctx, moderate, 'moderate', moderate_speeds)
    busy_rows = (d1['proxy']['row'], d2['row'])
    lines.append(f'- **Load** (the moderate block at {", ".join(map(speed_name, moderate_speeds))}): proxy '
                 f'{moderate["row"]} ({moderate["why"]}), decision 2 {moderate_d2["row"] or "not judged"}; the busy '
                 f'block: {busy_rows[0]}, {busy_rows[1] or "not judged"} → '
                 + ('**they differ**: both are stated; the busy block decides' if (moderate['row'], moderate_d2['row'])
                    != busy_rows else 'the same rows'))
    warm = None if blocked(ctx, 'warm') else cells.meets(('warm', WARM_SPEED, PROXY))
    busy = None if blocked(ctx, 'busy') else cells.meets(('busy', WARM_SPEED, PROXY))
    lines.append(f'- **Warm source** (v1proxy against full at 2 Gbit/s): warm {meet_word(warm)}, cold {meet_word(busy)} '
                 + ('→ **another verdict**: reported beside; the cold reading decides' if warm is not None
                    and busy is not None and warm != busy else '→ the same verdict' if warm is not None
                    and busy is not None else '→ not judged'))
    return lines, moderate


def flag_lines(S, cells, ctx, d1, d2, base):
    lines = ['## Flags (A8, A9)']
    weak = fragile(S, ctx, base)
    cells.consulted = []
    outcome(cells, ctx)
    rested = sorted({key for a_key, b_key, _ in cells.consulted for key in (a_key, b_key)}, key=str)
    near = near_line(cells, ctx, base)
    cells.consulted = []
    client = [key for key in rested if (cells.cell(*key) or {}).get('client_wait')]
    lines.append('- *Fragile*: ' + ('; '.join(f'{VIEW_LABEL[view]} would give {outcome_text(other)}'
                                              for view, other in weak) if weak else
                                    'no (every robustness view gives the same rows and R2 verdict)')
                 + ('; the rows rest on a cell flagged *client wait*: ' + ', '.join(
                     f'{b} {speed_name(s)} {a}' for b, s, a in client) if client else ''))
    lines.append('- *Near the line*: ' + ('; '.join(
        (f'{key[0][0]} {speed_name(key[0][1])} {key[0][2]} against {key[1][2]} ({key[1][0]}): Δ{key[2]} {value:+.4f} '
         f'near δ {line:.4f}' if kind == 'diff' else f'R2 at {speed_name(key)}: ratio {value:.3f} near {line:.3f}')
        for kind, key, value, line in near) if near else 'no deciding difference within ±0.25 x δ of its δ, and no '
                                                         'rate ratio within ±0.15 of 1.5 or 1/1.5'))
    caveats = []
    for block in LOADED_BLOCKS:
        for speed in SPEEDS:
            for arm in ('pgdelta', PROXY, PROXY_S1, 'capture', 'bracket'):
                if cells.meets((block, speed, arm)) is True:
                    cell, full_cell = cells.cell(block, speed, arm) or {}, cells.cell(block, speed, 'full') or {}
                    mine, full = cell.get('p99_worst5s_ms'), full_cell.get('p99_worst5s_ms')
                    if mine is not None and full is not None and mine > 2 * full:
                        caveats.append(f'{block} {speed_name(speed)} {arm}: {fmt(mine, 2)} ms against full\'s '
                                       f'{fmt(full, 2)} ms (above 50 and 100 ms: {fmt(cell.get("lat_over_50ms"), 0)} '
                                       f'and {fmt(cell.get("lat_over_100ms"), 0)} transactions against '
                                       f'{fmt(full_cell.get("lat_over_50ms"), 0)} and '
                                       f'{fmt(full_cell.get("lat_over_100ms"), 0)})')
    cells.consulted = []
    lines.append('- *Latency caveat* (an arm meets O4 but the p99 of its worst 5-s span is more than twice full\'s): '
                 + ('; '.join(caveats) if caveats else 'none'))
    counts = Counter(name for r in S.runs for name in S.flagged(r))
    lines.append('- A8\'s run flags: ' + (', '.join(f'{name} {counts[name]}' for name in RUN_FLAGS if counts[name])
                                          or 'none'))
    flagged = []
    for block, speed, arm in cell_keys():
        cell = cells.cell(block, speed, arm)
        if cell and cell['flag_cell']:
            flagged.append(f'{block} {speed_name(speed)} {arm} ({len(cell["flagged"])} runs'
                           + (', client wait' if cell['client_wait'] else '') + ')')
    lines.append('- cells flagged (more than 1 flagged run): ' + ('; '.join(flagged) if flagged else 'none'))
    return lines, weak, near, caveats


def e3_check(item, late=True):
    """E3 for one pause2 (a2_runs' item): W 1.6-2.0 s at ρ = 0.6 (1.3-2.0 at 0.3), D at least 0.95, the gap within
    5 ms plus one latency of the signals and inside dest's mapped phase; with `late`, the late count at L 4-6 s at
    0.6. None without its W or D (no numbers from the logs). Smoke item 12 judges by it, without the late count."""
    if item['W'] is None or item['D'] is None:
        return None
    busy = item['block'] == 'busy'
    return every([within(item['W'], 1.6 if busy else 1.3, 2.0), item['D'] >= 0.95,
                  within(item['late_L'], 4.0, 6.0) if busy and late else None, item['signals_ok'], item['inside']])


def e_lines(S, cells, ctx, d1, d2, a5v, a6v, moderate, caveats, near):
    lines = ['## Added predictions (busy block unless stated)']
    say = lines.append
    high = S.high_cpu()
    share = S.busy_cpu('busy')
    side = (('above 50%' if high else '50% or less') if high is not None
            else 'not selected (--busy-cpu, smoke item 2\'s value, not given)' if S.busy_cpu_given is None and not S.smoke
            else 'unknown')
    capacity = S.capacity()
    say(f'- **E1** (C 10,000-40,000; busy CPU at R_busy 40-75%, at R_mod 20-45%): C {word(within(capacity, 1e4, 4e4))} '
        f'({fmt(capacity, 0)}); at R_busy {word(within(share, 0.40, 0.75))} ({fmt(share, 3)}); at R_mod '
        f'{word(within(S.busy_cpu("moderate"), 0.20, 0.45))} ({fmt(S.busy_cpu("moderate"), 3)}); the busy CPU at R_busy is '
        f'{side}, which selects E4\'s, E7\'s, E9\'s, E14\'s and E16\'s ranges')
    nones = [r['stats']['completed_ratio'] for r in S.runs if r['method'] == 'none' and r['stats']
             and r['status'] != 'error']
    singles = {T: pool_null(S, 'busy', T) for T in (15, 30, 50)}
    w_med = every(None if v is None else float(np.median(v)) <= 0.02 for v in singles.values())
    w_top = every(None if v is None else float(np.percentile(v, PERCENTILE)) <= 0.05 for v in singles.values())
    d15 = pool_null(S, 'busy', 15, stat='D')
    five = pool_null(S, 'busy', 15, 5)
    deltas = [found['W']['delta'] for speed in SPEEDS for arm in ('pgdelta', PROXY, PROXY_S1, 'capture', 'bracket')
              if (found := cells.compare(('busy', speed, arm)))]
    settles = [get(r['record'], 'settle') for r in S.runs if r['block'] in LOADED_BLOCKS and get(r['record'], 'settle', 'gate')]
    within30 = [bool(s['gate'].get('passed')) and (s.get('seconds') or 0) <= GATE_WITHIN_S for s in settles]
    share30 = None if not within30 else sum(within30) / len(within30)
    say(f'- **E2** (the null): none\'s completed over scheduled 0.995-1.005 per window '
        f'{word(every(within(v, 0.995, 1.005) for v in nones))} ({", ".join(fmt(v, 4) for v in nones) or "–"}); one '
        f'window\'s W null median at most 0.02 s {word(w_med)} and 95th percentile at most 0.05 s {word(w_top)} for T = '
        f'15, 30, 50 (' + ', '.join(f'{T}: {fmt(None if v is None else float(np.median(v)), 4)} / '
                                     f'{fmt(None if v is None else float(np.percentile(v, PERCENTILE)), 4)}'
                                     for T, v in singles.items()) + f'); D\'s null median at T = 15 0.005-0.03 '
        f'{word(None if d15 is None else within(float(np.median(d15)), 0.005, 0.03))} '
        f'({fmt(None if d15 is None else float(np.median(d15)), 4)}); a median of 5 windows\' W 95th percentile at most '
        f'0.03 s {word(None if five is None else float(np.percentile(five, PERCENTILE)) <= 0.03)} '
        f'({fmt(None if five is None else float(np.percentile(five, PERCENTILE)), 4)}); δ_W the 0.05-s floor in every '
        f'comparison {word(every(abs(d - FLOOR_W) < 1e-9 for d in deltas))} (largest {fmt(mx(deltas), 4)}); the gate '
        f'passes within 30 s in at least 95% of runs {word(None if share30 is None else share30 >= 0.95)} '
        f'({fmt(None if share30 is None else 100 * share30, 1)}%)')
    pauses = ctx['a2']
    e3 = [e3_check(item) for item in pauses]
    say(f'- **E3** (pause2: W 1.6-2.0 s at ρ = 0.6, 1.3-2.0 at 0.3; D at least 0.95; the late count at L 4-6 s at 0.6; the '
        f'gap within 5 ms plus one latency of the signals, inside the mapped phase): {word(every(e3))} ('
        + ', '.join(f'{item["block"]} W {fmt(item["W"], 3)} D {fmt(item["D"], 3)} late {fmt(item["late_L"], 2)}'
                    for item in pauses) + ')')
    burn = cells.cell('busy', None, 'burn4')
    ratios = [ratio(r['stats']['latency'].get('p99_ms'), (r['settle'] or {}).get('p99_ms'))
              for r in (burn or {}).get('runs', [])]
    if burn is None or high is None:
        e4 = None
    elif high:
        e4 = every([within((burn or {}).get('W_net'), 0, 0.2), within((burn or {}).get('D_net'), 0, 0.15),
                    every(within(v, 2, 30) for v in ratios)])
    else:
        e4 = every([None if burn.get('W_measurable') is None else not burn['W_measurable'],
                    None if burn.get('D_measurable') is None else not burn['D_measurable'],
                    every(within(v, 1.5, 10) for v in ratios)])
    say(f'- **E4** (burn4, by the busy CPU at R_busy, {side}: ' + ('W_net at most 0.2 s, D_net at most 0.15, window p99 '
                                                                   '2-30 times the settle\'s' if high else
                                                                   'neither W nor D measurable, window p99 1.5-10 times '
                                                                   'the settle\'s') + f'): {word(e4)} '
        f'(W_net {fmt((burn or {}).get("W_net"), 3)}, D_net {fmt((burn or {}).get("D_net"), 3)}, measurable '
        f'{yes((burn or {}).get("W_measurable"))} / {yes((burn or {}).get("D_measurable"))}; p99 over the settle\'s '
        + ', '.join(fmt(v, 1) for v in ratios) + ')')
    cpu = ctx['cpu']
    ranges = {'full': (2.2, 3.0), 'capture': (3.5, 6.0), 'pgdelta': (8.5, 11.5), PROXY: (5.5, 7.5),
              PROXY_S1: (5.0, 7.0), V1: (5.5, 7.5), 'burn4': (7.9, 8.5)}
    oks = [within(cpu.get(arm), *bounds) for arm, bounds in ranges.items()]
    v1_proxy = ratio(cpu.get(V1), cpu.get(PROXY))
    oks += [None if v1_proxy is None else abs(v1_proxy - 1) <= 0.2, within(ratio(cpu.get(V1), cpu.get('full')), 1.8, 3.4),
            within(ratio(cpu.get('pgdelta'), cpu.get('full')), 2.8, 5.2)]
    say('- **E5** (calibrated CPU-s per run: full 2.2-3.0, capture 3.5-6.0, pgdelta 8.5-11.5, v1proxy 5.5-7.5, '
        'v1proxy_s1 5.0-7.0, v1 itself 5.5-7.5 and within 20% of v1proxy, burn4 7.9-8.5; v1 over full 1.8-3.4, pgdelta '
        f'over full 2.8-5.2): {word(every(oks))} (' + ', '.join(f'{arm} {fmt(cpu.get(arm))}' for arm in ranges)
        + f'; v1 over full {fmt(ratio(cpu.get(V1), cpu.get("full")))}, pgdelta over full '
        f'{fmt(ratio(cpu.get("pgdelta"), cpu.get("full")))})')
    times = {('full', '0.5g'): (38, 42), ('full', '2g'): (10.3, 11.3), ('full', U): (3.8, 5.0), ('capture', '2g'): (4.0, 5.5),
             **{('pgdelta', s): (8.3, 11.5) for s in SPEEDS}, **{(PROXY, s): (3.2, 4.5) for s in SPEEDS},
             **{(PROXY_S1, s): (4.5, 6.5) for s in REGISTERED}, ('bracket', '2g'): (0.1, 0.6)}
    found = [(arm, s, (cells.cell('busy', s, arm) or {}).get('total_s')) for arm, s in times]
    against = []
    for arm in ('full', 'capture', 'pgdelta', PROXY, PROXY_S1, 'bracket'):
        loaded, idle = (cells.cell('busy', '2g', arm) or {}).get('total_s'), S.idle_time(arm)
        change = None if loaded is None or not idle else loaded / idle - 1
        against.append((arm, change, None if change is None else (abs(change) <= 0.05 if arm == 'full'
                                                                 else 0 <= change <= 0.35)))
    say('- **E6** (times under load: full 38-42 s at 0.5, 10.3-11.3 at 2, 3.8-5.0 unthrottled; capture 4.0-5.5; pgdelta '
        '8.3-11.5 and v1proxy 3.2-4.5 at every speed; v1proxy_s1 4.5-6.5; bracket 0.1-0.6; against the idle calibration '
        f'at 2 Gbit/s full within ±5%, the others 0-35% longer): {word(every(within(v, *times[(a, s)]) for a, s, v in found))}'
        f' (' + ', '.join(f'{a} {speed_name(s)} {fmt(v)}' for a, s, v in found if v is not None) + '); against idle '
        f'{word(every(ok for *_, ok in against))} (' + ', '.join(f'{a} {change:+.1%}' for a, change, _ in against
                                                                 if change is not None) + ')')

    def net(speed, arm, role):
        return (cells.cell('busy', speed, arm) or {}).get(f'{role}_net')

    def null_p99(arm, speed):
        cell = cells.cell('busy', speed, arm)
        null = pool_latency(S, 'busy').get('p99_ms')
        return None if cell is None or not null or cell.get('lat_p99_ms') is None else cell['lat_p99_ms'] / null
    e7 = []
    e7 += [None if net(s, 'full', 'W') is None else net(s, 'full', 'W') <= 0.05 for s in REGISTERED]
    e7 += [None if net(s, 'full', 'D') is None else net(s, 'full', 'D') <= 0.03 for s in REGISTERED]
    e7 += [within(net(U, 'full', 'W'), 0.02, 0.3), within(net(U, 'full', 'D'), 0.02, 0.15)]
    e7 += [within(net('2g', 'bracket', 'W'), 0, 0.1), within(net('2g', 'bracket', 'D'), 0, 0.10)]
    e7 += [within(net('2g', 'capture', 'W'), 0.2, 3), None if net('2g', 'capture', 'D') is None
           else net('2g', 'capture', 'D') >= 0.3]
    e7 += [within(net(s, 'pgdelta', 'W'), 0.3, 3.5) for s in SPEEDS]
    e7 += [None if net(s, 'pgdelta', 'D') is None else net(s, 'pgdelta', 'D') >= 0.3 for s in SPEEDS]
    if high is None:
        e7.append(None)
    elif high:
        e7 += [within(net(s, PROXY, 'W'), 0.02, 0.30) for s in SPEEDS]
        e7 += [within(net(s, PROXY, 'D'), 0.02, 0.15) for s in SPEEDS]
        e7 += [within(null_p99(PROXY, s), 3, 30) for s in SPEEDS]
        e7 += [None if net(s, PROXY_S1, 'W') is None else net(s, PROXY_S1, 'W') <= 0.05 for s in REGISTERED]
        e7 += [None if net(s, PROXY_S1, 'D') is None else net(s, PROXY_S1, 'D') <= 0.03 for s in REGISTERED]
    else:
        e7 += [within(net(s, PROXY, 'W'), 0.01, 0.10) for s in SPEEDS]
        e7 += [within(net(s, PROXY, 'D'), 0.01, 0.06) for s in SPEEDS]
        e7 += [within(null_p99(PROXY, s), 3, 20) for s in SPEEDS]
        e7 += [None if cells.measurable('busy', s, PROXY_S1, role) is None else not cells.measurable(
            'busy', s, PROXY_S1, role) for s in REGISTERED for role in ('W', 'D')]
    say(f'- **E7** (net medians, by the busy CPU at R_busy, {side}): {word(every(e7))} ('
        + ', '.join(f'{arm} {speed_name(s)} {fmt(net(s, arm, "W"), 3)} / {fmt(net(s, arm, "D"), 3)}'
                    for arm in ('full', 'bracket', 'capture', 'pgdelta', PROXY, PROXY_S1) for s in SPEEDS
                    if cells.cell('busy', s, arm)) + '; W_net s / D_net)')
    l3v, l4v = l3(cells, ctx), l4(cells, ctx)
    _, l2ok = l2(ctx)
    e8 = [None if d1['r2']['verdict'] is None else d1['r2']['verdict'] is False, l2ok,
          None if l3v['verdict'] is None else (l3v['verdict'] is False and 'high' in l3v['text']),
          l4v['verdict'] is None, None if d1['r1']['verdict'] is None else d1['r1']['verdict'] is True]
    live = [r for r in S.runs if r['status'] != 'error']
    e8.append(not any(r['status'] == 'fail' for r in live) if live else None)
    say(f'- **E8** (the registered verdicts: L1 doesn\'t track, L2 holds, L3 fails high, L4 not judged, L5 holds, R1 fires, '
        f'R2 doesn\'t track): {word(every(e8))}')
    proxy = d1['proxy']
    expect_proxy = (proxy['row'] == D1_ROWS[6] and proxy.get('roles') == ['D'] and proxy.get('cause') == '7a'
                    if high else proxy['row'] == D1_ROWS[5]) if high is not None else None
    expect_d2 = (d2['row'] == ('Cap meets' if high else 'No cap')) if high is not None else None
    ha, _ = ha_caveat(cells, ctx)
    warm_same = None
    warm, busy2 = cells.meets(('warm', WARM_SPEED, PROXY)), cells.meets(('busy', WARM_SPEED, PROXY))
    if warm is not None and busy2 is not None:
        warm_same = warm == busy2
    e9 = [d1['row'] == D1_ROWS[1], expect_proxy, expect_d2, None if high or ha is None else ha is False,
          None if moderate['row'] == D1_ROWS[3] else moderate['row'] == D1_ROWS[5], warm_same]
    say(f'- **E9** (decision 1 row 2, R1; the proxy {side}: ' + ('"v1 misses" on D, warm reading 7a, decision 2 "Cap '
                                                                 'meets"' if high else '"v1 meets", decision 2 "No cap"')
        + '; the HA caveat not firing at 50% or less; the moderate block meets; the warm block the same proxy row): '
        f'{word(every(e9))} (decision 1 {d1["row"]}; the proxy {proxy["row"]}'
        + (f' on {", ".join(proxy.get("roles") or [])}, {proxy.get("cause")}' if proxy.get('cause') else '')
        + f'; decision 2 {d2["row"] or "not judged"}; the HA caveat {yes(ha)}; moderate {moderate["row"]}; near the line '
        f'{"yes" if near else "no"}; latency caveat {"yes" if caveats else "no"})')
    outputs = [r for r in S.runs if r['method'] in LIVE and r['status'] != 'error']
    ahead = []
    for r in outputs:
        if r['method'] != 'full':
            continue
        cut, end = get(r['record'], 'g4', 'cut_utc'), get(r['record'], 'g4', 'capture_end', 'utc')
        if cut and None not in cut and end is not None and r['offset'] is not None:
            ahead.append((end + r['offset']) - (cut[0] + cut[1]) / 2)
    stale = [get(r['record'], 'g4', 'staleness_s') for r in outputs if r['method'] == 'pgdelta']
    say(f'- **E10** (every output passes; full\'s cut within 0.3 s before its write-ahead log end point; pgdelta\'s '
        f'staleness at publication 4-8 s): every output passes {word(every(r["status"] == "pass" for r in outputs))}; '
        f'the cut {word(every(within(v, 0, 0.3) for v in ahead))} ({", ".join(fmt(v, 3) for v in ahead) or "–"} s '
        f'before); staleness {word(every(within(v, 4, 8) for v in stale))} ({", ".join(fmt(v) for v in stale if v is not None) or "–"} s)')
    say(f'- **E11** (at least half of pgdelta\'s W from its capture, A5 "most is its capture"): {word(a5v["verdict"])} '
        f'({a5v["text"]})')
    wire = {('full', '0.5g'): (2.15e9, 2.45e9), **{(PROXY, s): (27.6e6, 29.2e6) for s in SPEEDS},
            **{('pgdelta', s): (60e6, 130e6) for s in SPEEDS}}
    values = [(arm, s, median(r['wire_s2d'] for r in (cells.cell('busy', s, arm) or {}).get('runs', [])))
              for arm, s in wire]
    say(f'- **E12** (wire bytes: full 2.15-2.45 GB at 0.5 Gbit/s; v1proxy 27.6-29.2 MB; pgdelta 60-130 MB): '
        f'{word(every(within(v, *wire[(a, s)]) for a, s, v in values))} ('
        + ', '.join(f'{a} {speed_name(s)} {fmt(None if v is None else v / 1e6, 1)} MB' for a, s, v in values
                    if v is not None) + ')')
    growth = [get(trial, 'references', 'end', 'loaddb_growth') for trial in S.trials.values()]
    failures = sum((tally.get('failed') or 0) for tally in S.tallies.values()) + sum(
        (run.get('failed') or 0) for trial in S.trials.values() for run in get(trial, 'load', 'runs') or [])
    checkpoint = [r for r in S.runs if r['flags'].get('checkpoint')]
    autovacuum = [r for r in S.runs if (r.get('sampler') or {}).get('autovacuum_loaddb')]
    say(f'- **E13** (loaddb grows at most 5%; no pgbench failures; no checkpoint in a window but an arm\'s own; no '
        f'autovacuum on loaddb): {word(every([every(None if g is None else g <= 0.05 for g in growth), failures == 0, not checkpoint, not autovacuum]))}'
        f' (growth {", ".join(fmt(None if g is None else 100 * g, 2) + "%" for g in growth)}; failures {failures}; '
        f'checkpoints {len(checkpoint)}; autovacuum on loaddb {len(autovacuum)})')
    if high is None:
        e14 = None
    elif high:
        nets = [net(s, PROXY, 'W') for s in REGISTERED if cells.measurable('busy', s, PROXY, 'W')]
        e14 = (True if a6v['verdict'] is True else every(within(v, 0.02, 0.05) for v in nets)
               if a6v['verdict'] is False else False)
    else:
        e14 = a6v['verdict'] is not False
    say(f'- **E14** (A6, {side}: ' + ('"cores", or "CPU-seconds is an adequate unit" where v1proxy\'s W_net is near the '
                                      'low end of E7\'s range' if high else 'not judged, or "cores"')
        + f'): {word(e14)} ({a6v["text"]})')
    warm_cell, cold_cell = cells.cell('warm', WARM_SPEED, PROXY), cells.cell('busy', WARM_SPEED, PROXY)
    if warm_cell and cold_cell and not warm_cell['short'] and not cold_cell['short'] and warm_cell.get('W_net') is not None \
            and cold_cell.get('W_net') is not None:
        e15 = every([warm_cell['W_net'] <= cold_cell['W_net'] + 0.02, abs(warm_cell['D_net'] - cold_cell['D_net']) <= 0.03,
                     None if d1['proxy']['row'] != D1_ROWS[6] else d1['proxy'].get('cause') == '7a'])
    else:
        e15 = None
    say(f'- **E15** (warm v1proxy: W_net at most its cold W_net at 2 Gbit/s plus 0.02 s, D_net within ±0.03 of its cold '
        f'D_net; if row 7 applies, the warm reading is 7a): {word(e15)} (warm {fmt((warm_cell or {}).get("W_net"), 3)} / '
        f'{fmt((warm_cell or {}).get("D_net"), 3)}, cold {fmt((cold_cell or {}).get("W_net"), 3)} / '
        f'{fmt((cold_cell or {}).get("D_net"), 3)})')
    waits = [(b, s, a) for b, s, a in cell_keys() if (cells.cell(b, s, a) or {}).get('client_wait')]
    e16 = None if high is None else (all(a in ('burn4', PROXY) for _, _, a in waits) if high else not waits)
    say(f'- **E16** ("client wait" flags no cell at 50% busy or less; above, at most burn4\'s and v1proxy\'s): '
        f'{word(e16)} (' + (', '.join(f'{b} {speed_name(s)} {a}' for b, s, a in waits) or 'no cell flagged') + ')')
    return lines


def a_lines(S, cells, ctx, a5v, a6v, weak, near, caveats):
    lines = ['## Added rules']
    say = lines.append
    control = coverage_control(S)
    wrong_full = [r for r in S.runs if r['method'] == 'full' and r['status'] == 'fail']
    bad = [c.get('name') for trial in S.trials.values() for c in (trial.get('oracle_controls') or {}).get('controls') or []
           if (c.get('verdict') or {}).get('ok') is not True]
    fires = bool(wrong_full) or any(not ok for *_, ok in control) or bool(bad)
    say(f'- **A1** (the oracle): {"**fires**" if fires else "does not fire"} (wrong full outputs {len(wrong_full)}; the '
        f'coverage control {sum(ok for *_, ok in control)} of {len(control)} failed (v) as they must; negative controls '
        f'that misbehaved: {", ".join(bad) or "none"})')
    a2_fires = [block for block in ('busy', 'moderate') if any(item['ok'] is False for item in ctx['a2']
                                                               if item['block'] == block)]
    say(f'- **A2** (the instrument): ' + (f'**fires** in {", ".join(a2_fires)}' if a2_fires else 'does not fire'))
    a3_fires = [block for block, item in ctx['a3'].items() if item.get('fires')]
    say(f'- **A3** (overload): ' + (f'**fires** in {", ".join(a3_fires)}' if a3_fires else 'does not fire'))
    say(f'- **A4** (the proxy\'s gate): v1proxy {ctx["a4"].get(PROXY)}, v1proxy_s1 {ctx["a4"].get(PROXY_S1)}, warm runs '
        f'{ctx["a4"].get("warm")}; (iii)\'s references: v1 itself {fmt(ctx["a4_reference"].get(PROXY))}, v1proxy / 1.06 '
        f'{fmt(ctx["a4_reference"].get(PROXY_S1))} CPU-s')
    say(f'- **A5** (where staged pgdelta\'s loss comes from): {a5v["text"]}')
    say(f'- **A6** (cores or CPU-seconds): {a6v["text"]}')
    say('- **A7** (time next to slowdown): always; see the table below')
    counts = Counter(name for r in S.runs for name in S.flagged(r))
    clock = [key[1] for key, item in ctx['clock'].items() if item['fires']]
    say('- **A8** (flags): run flags ' + (', '.join(f'{name} {counts[name]}' for name in RUN_FLAGS if counts[name])
                                         or 'none') + '; the clock gate ' + (f'**fires** ({", ".join(clock)})' if clock
                                                                            else 'does not fire'))
    say(f'- **A9** (fragile, near the line, latency caveat): fragile {"yes" if weak else "no"}, near the line '
        f'{"yes" if near else "no"}, latency caveat {"yes" if caveats else "no"} (see Flags)')
    idle_full = S.idle_time('full')
    v1_cpu = ctx['cpu'].get(V1)
    links = [get(r['record'], 'link', 'source_to_dest_gbit') for r in S.runs if r['block'] == 'busy' and r['speed'] == '2g']
    link = median(links)
    gates = [('full\'s idle time at 2 Gbit/s', idle_full, 10.27 * 0.95, 10.32 * 1.05),
             ('v1\'s calibrated CPU-s', v1_cpu, 6.1 * 0.85, 7.2 * 1.15),
             ('the single-stream link test at 2 Gbit/s', link, 1.917 * 0.98, 1.917 * 1.02)]
    shifted = [name for name, value, lo, hi in gates if value is not None and not lo <= value <= hi]
    say('- **A10** (drift against EXP-11, reported only): ' + ('the session is **shifted** (' + ', '.join(shifted) + ')'
                                                              if shifted else 'within every gate') + ' ('
        + ', '.join(f'{name} {fmt(value, 3)} in [{fmt(lo, 3)}, {fmt(hi, 3)}]' for name, value, lo, hi in gates) + ')')
    say('- **A11** (no fix and rerun once the main sweep starts): every run is read as recorded; the write-up says '
        'whether anything was rerun')
    capacity, smoke = S.capacity(), S.capacity_tps_given
    drift = None if capacity is None or not smoke else capacity / smoke - 1
    say('- **A12** (capacity drift against the smoke run\'s C, --capacity-tps): '
        + ('not judged (the smoke run\'s C not given: --capacity-tps, as the registered command passes it)'
           if drift is None else
           f'**flagged** ({drift:+.1%})' if abs(drift) > A12_DRIFT else f'within 10% ({drift:+.1%})')
        + f'; the trial\'s C {fmt(capacity, 0)}')
    say('- **A13** (durability): ' + ('; '.join(ctx['a13']) if ctx['a13'] else 'does not fire'))
    return lines


def time_lines(S, cells):
    lines = ['## A7: time next to slowdown (median total_s under load; W_net, D_net, p95 ms; idle at 2 Gbit/s)',
             '| Arm | ' + ' | '.join(f'busy {speed_name(s)}' for s in SPEEDS) + ' | warm 2 Gbit/s | moderate 2 Gbit/s '
             '| moderate unthrottled | idle (calibration) |', '|---|' + '---|' * 7]
    for arm in [a for a in ARM_NAMES if a not in CONTROLS]:
        parts = []
        for block, speed in [('busy', s) for s in SPEEDS] + [('warm', '2g'), ('moderate', '2g'), ('moderate', U)]:
            cell = cells.cell(block, speed, arm)
            parts.append('' if cell is None else f'{fmt(cell["total_s"])} s; {fmt(cell.get("W_net"), 3)}, '
                                                 f'{fmt(cell.get("D_net"), 3)}, {fmt(cell.get("lat_p95_ms"), 2)}')
        lines.append(f'| {arm} | ' + ' | '.join(parts) + f' | {fmt(S.idle_time(arm))} |')
    return lines


def cpu_lines(S, ctx, cells):
    lines = ['## Source CPU (calibrated CPU-s without load; under load, a view: the window\'s [t0, t1 + 10 s] VM-wide busy '
             'CPU over the settle\'s rate, from the sampler, with the timed phases\' alone in parentheses)',
             '| Arm | calibrated CPU-s (runs) | idle time s | ' + ' | '.join(f'under load {speed_name(s)}' for s in SPEEDS)
             + ' |', '|---|---|---:|' + '---:|' * len(SPEEDS)]
    for arm in CALIBRATION_ARMS:
        runs = [r for r in S.runs if r['block'] == 'calibration' and r['method'] == arm]
        found = [cells.cell('busy', None if arm in CONTROLS else s, arm) or {} for s in SPEEDS]
        under = [f'{fmt(cell.get("cpu_excess_s"))} ({fmt(cell.get("cpu_excess_phases_s"))})' for cell in found]
        lines.append(f'| {arm} | {fmt(ctx["cpu"].get(arm))} ({len(runs)}) | {fmt(S.idle_time(arm))} | '
                     + ' | '.join(under) + ' |')
    return lines


def mechanism_lines(S, cells):
    """The sampler's view (a mechanism; no verdict uses it): each group's added CPU-s in the window, the pgbench
    threads' added run-queue wait, WAL sync time and steal; busy block medians."""
    lines = ['## The sampler (a mechanism; no verdict uses it): added CPU-s in the window over the settle\'s rate',
             '| Speed | Arm | ' + ' | '.join(GROUPS) + ' | client wait s | worst-bucket wait s | WAL sync ms | steal per s |',
             '|---|---|' + '---:|' * (len(GROUPS) + 4)]
    for _, speed, arm in cell_keys(('busy',)):
        cell = cells.cell('busy', speed, arm)
        samples = [r.get('sampler') or {} for r in (cell or {}).get('runs', [])]
        if not any(samples):
            continue
        lines.append(f'| {speed_name(speed)} | {arm} | '
                     + ' | '.join(fmt(median(s.get(f'cpu_{g}_s') for s in samples), 2) for g in GROUPS)
                     + f' | {fmt(median(s.get("client_wait_s") for s in samples), 3)} | '
                     f'{fmt(median(s.get("client_wait_worst_bucket_s") for s in samples), 3)} | '
                     f'{fmt(median(s.get("wal_sync_ms") for s in samples), 1)} | '
                     f'{fmt(median(s.get("steal_rate") for s in samples), 4)} |')
    return lines


def order_lines(S, cells):
    """Runs that follow a staged arm (S written and removed on the source) against the others: a median difference
    over 0.05 s in W or 0.03 in D is reported."""
    lines = ['## Order effects (runs after a staged arm against the others; differences over 0.05 s in W or 0.03 in D)']
    before, last = {}, {}
    for r in S.runs:
        key = (r['sweep'], r['trial_id'])
        before[id(r)] = last.get(key)
        last[key] = r
    found = 0
    for block, speed, arm in cell_keys(arms=[arm for arm in ARM_NAMES if arm not in CONTROLS]):
        cell = cells.cell(block, speed, arm)
        if cell is None:
            continue
        after = [r for r in cell['runs'] if (before.get(id(r)) or {}).get('method') in STAGED]
        other = [r for r in cell['runs'] if (before.get(id(r)) or {}).get('method') not in STAGED]
        if not after or not other:
            continue
        dw = median(r['stats']['W'] for r in after) - median(r['stats']['W'] for r in other)
        dd = median(r['stats']['D'] for r in after) - median(r['stats']['D'] for r in other)
        if abs(dw) > 0.05 or abs(dd) > 0.03:
            found += 1
            lines.append(f'- {block} {speed_name(speed)} {arm}: after a staged arm W {dw:+.3f} s, D {dd:+.3f} '
                         f'({len(after)} against {len(other)} runs)')
    if not found:
        lines.append('- none')
    return lines


def smoke_lines(S, cells, ctx):
    """The smoke run's sixteen items, each printed from the records (EXP-11's lesson)."""
    lines = ['## Smoke items, from the records']
    say = lines.append
    tallies = list(S.tallies.values())
    runs_ = [run for trial in S.trials.values() for run in get(trial, 'load', 'runs') or []]
    say(f'1. **pgbench\'s log**: failures {sum(t.get("failed") or 0 for t in tallies)} in the logs and '
        f'{sum(run.get("failed") or 0 for run in runs_)} by pgbench\'s summaries; latency under its schedule lag on '
        f'{sum(t.get("lag_over_latency") or 0 for t in tallies)} lines; completion times falling within a client '
        f'{sum(t.get("not_rising") or 0 for t in tallies)} times; every run complete (n at its stop minus n at its start '
        f'equals its log\'s commits, per client) {word(every(run.get("complete") for run in runs_))} '
        f'({sum(bool(run.get("complete")) for run in runs_)} of {len(runs_)}); scripts both ran '
        f'{word(every([sum(t.get("commits") or 0 for t in tallies) > 0, sum(t.get("aborts") or 0 for t in tallies) > 0]))}'
        f'; the logs per hour in the load table')
    capacity = S.capacity()
    clients = [r for r in S.runs if r['flags'].get('client')]
    say(f'2. **Capacity and rates**: C {fmt(capacity, 0)}; R_busy {fmt(S.rate("busy"), 0)} '
        f'({fmt(ratio(S.rate("busy"), capacity), 3)} x C), R_mod {fmt(S.rate("moderate"), 0)} '
        f'({fmt(ratio(S.rate("moderate"), capacity), 3)} x C); busy CPU {fmt(S.busy_cpu("busy"), 3)} and '
        f'{fmt(S.busy_cpu("moderate"), 3)}; a pgbench thread above 90% of a core in {len(clients)} settles')
    busy_warmups = [w for ws in S.warmups.values() for w in ws if w['run_id'] == 'busy' and w['whole']]
    p99 = median(w['whole'].get('p99_ms') for w in busy_warmups)
    limit = None if p99 is None else math.ceil(max(10.0, 10 * p99) / 5 - 1e-9) * 5
    single = pool_null(S, 'busy', 15)
    top = None if single is None else float(np.percentile(single, PERCENTILE))
    say(f'3. **L and the null**: p99 over the busy warm-up {fmt(p99, 3)} ms, so L = max(10 ms, 10 x p99) rounded up to '
        f'5 ms = {fmt(limit, 0)} ms (this analysis used {fmt(S.late_limit_ms, 0)} ms); one window\'s W null at T = 15 s, '
        f'95th percentile {fmt(top, 4)} s ' + ('(above 0.1 s: R_busy becomes 0.5 x C, once)' if top is not None and top > 0.1
                                               else '(at most 0.1 s)'))
    chrony = []
    for run in S.meta:
        for host in ('source', 'dest'):
            text = get(run, 'environment', host, 'chrony') or ''
            for line in str(text).splitlines():
                if line.startswith('System time'):
                    chrony.append(f'{host}: {line.split(":", 1)[1].strip()}')
    offsets = [v for item in ctx['clock'].values() for v in item['offsets']]
    say(f'4. **Clocks**: the probe\'s offsets {", ".join(fmt(None if v is None else v * 1000, 3) for v in offsets)} ms, '
        f'within 5 ms {word(every(None if v is None else abs(v) <= CLOCK_GATE_S for v in offsets))}; chrony '
        + ('; '.join(chrony) or 'not recorded'))
    versions = [get(run, 't10a_preflight', 'versions') for run in S.meta]
    same = every(None if not v else v.get('source', {}).get('postgres') == v.get('dest', {}).get('postgres')
                 for v in versions)
    dumps = [get(trial, 'references', 'end', 'schema_match') for trial in S.trials.values()]
    controls = [c for trial in S.trials.values() for c in (trial.get('oracle_controls') or {}).get('controls') or []]
    coverage = coverage_control(S)
    say(f'5. **The oracle**: one PostgreSQL minor version on both VMs {word(same)}; the schema dumps at the start and end '
        f'hash equal {word(every(dumps))}; the controls ' + (
            f'{sum(bool((c.get("verdict") or {}).get("ok")) for c in controls)} of {len(controls)} behaved'
            if controls else 'not run (--oracle-controls)') + f'; the coverage control failed (v) in '
        f'{sum(ok for *_, ok in coverage)} of {len(coverage)}; good outputs that failed: '
        f'{sum(r["status"] == "fail" for r in S.runs if r["method"] in LIVE)}')
    proxies = [r for r in S.runs if r['method'] in measure.V1PROXY_METHODS and r['status'] != 'error']
    stopped = [r for r in proxies if get(r['record'], 'source_untimed', 'stop') or r['record'].get('source_restart')]
    say(f'6. **The proxy**: runs {len(proxies)}; backup mode held {sum(bool(get(r["record"], "proxy", "backup", "held")) for r in proxies)}, '
        f'every byte hashed {sum(bool(get(r["record"], "proxy", "hashed", "all_hashed")) for r in proxies)}, verified and '
        f'started {sum(r["status"] == "pass" for r in proxies)}; the source stopped for a proxy {len(stopped)} times')
    jobs = sorted({get(r['record'], 'tool_reported', 'switches', 'sender', 'jobs') for r in proxies
                   if r['method'] == PROXY_S1}, key=str)
    say(f'7. **The one-core sender**: v1proxy_s1\'s sender reported --jobs {", ".join(map(str, jobs)) or "–"} '
        f'({word(None if not jobs else jobs == [1])})')
    shares = []
    for r in S.runs:
        cache = get(r['record'], 'source_untimed', 'cache') or {}
        if cache.get('policy') == 'evict':
            fincore = cache.get('fincore') or {}
            shares.append((get(fincore, 'benchmark', 'resident_share'), get(fincore, 'snapshot', 'resident_share'),
                           get(fincore, 'loaddb', 'resident_share'), bool(cache.get('evicted_again'))))
    residual = mx(max(v for v in s[:2] if v is not None) if any(v is not None for v in s[:2]) else None for s in shares)
    say(f'8. **Eviction**: {len(shares)} evictions; static files resident at most {fmt(None if residual is None else 100 * residual, 2)}% '
        f'(under 1% {word(None if residual is None else residual < CACHE_RESIDUAL)}); loaddb resident at least '
        f'{fmt(None if not shares else 100 * min((s[2] for s in shares if s[2] is not None), default=0), 2)}%; evicted '
        f'again after sync -f {sum(s[3] for s in shares)} times')
    staged = [r for r in S.runs if r['method'] in ('pgdelta', 'capture') and r['status'] != 'error']
    ends = sum(bool(get(r['record'], 'g4', 'capture_end', 'lsn') or get(r['record'], 'tool_reported',
                                                                        'basebackup_phases')) for r in staged)
    say(f'9. **The staged capture\'s --verbose lines** parse with the write-ahead log end point: {ends} of {len(staged)}')
    gates = [get(r['record'], 'settle') for r in S.runs if r['block'] in LOADED_BLOCKS and get(r['record'], 'settle', 'gate')]
    passed30 = [bool(g['gate'].get('passed')) and (g.get('seconds') or 0) <= GATE_WITHIN_S for g in gates]
    say(f'10. **The settle gate** passes within 30 s in {sum(passed30)} of {len(gates)} runs '
        f'({word(None if not gates else sum(passed30) / len(gates) >= 0.95)} against 95%)')
    finals = [get(trial, 'load', 'end', 'sampler', 'final') or (S.samplers.get(key) and getattr(S.samplers[key], 'final', None))
              for key, trial in S.trials.items()]
    finals = [f for f in finals if f]
    say('11. **The sampler\'s cost**: ' + ('; '.join(f'{f.get("cpu_seconds")} CPU-s over {f.get("seconds")} s '
                                                  f'({fmt(ratio(f.get("cpu_seconds"), f.get("seconds")), 4)} of a core, '
                                                  f'under 1% {word(None if not f.get("seconds") else f["cpu_seconds"] / f["seconds"] < 0.01)}), '
                                                  f'{f.get("ticks")} ticks at {f.get("interval_s")} s, {f.get("late_ticks")} late'
                                                  for f in finals) or 'no sampler data')
        + '; p99 with and without it: compare the warm-ups of a sweep run with --sampler-interval-s 0')
    pauses = [item for item in ctx['a2'] if item['block'] == 'busy']
    checks = [e3_check(item, late=False) for item in pauses]
    say(f'12. **pause2** at R_busy: {len(pauses)} runs; W 1.6-2.0 s and D at least 0.95 (E3\'s ranges), the gap '
        f'matching the agent\'s signals and inside dest\'s mapped phase: {word(every(checks) if checks else None)} ('
        + ', '.join(f'W {fmt(item["W"], 3)} D {fmt(item["D"], 3)} signals {yes(item["signals_ok"])} inside '
                    f'{yes(item["inside"])}' for item in pauses)
        + ('; a run without numbers' if None in checks else '') + '); if not, the window mapping or the arithmetic '
        'is fixed and the item repeats')
    noise = [get(run, 'dirty_idle_noise', 'max_increase_bytes') for run in S.meta]
    nosync = [get(r['record'], 'durability', 'dirty_writeback_increase_bytes') for r in S.runs
              if r['method'] in measure.NON_DURABLE_METHODS]
    audits = [(r['method'], get(r['record'], 'audit', 'passed')) for r in S.runs if r['record'].get('audit')]
    say(f'13. **Dest\'s checks**: idle noise {", ".join(fmt(None if v is None else v / (1 << 20), 2) for v in noise)} MiB; '
        f'full_nosync left {", ".join(fmt(None if v is None else v / 1e9, 2) for v in nosync) or "–"} GB dirty (at least 1.5 '
        f'GB {word(every(None if v is None else v >= 1.5e9 for v in nosync))}); the syscall audit '
        + (', '.join(f'{m} {yes(p)}' for m, p in audits) or 'not in these records'))
    growth = [get(trial, 'references', 'end', 'loaddb_growth') for trial in S.trials.values()]
    timed = sum((get(r.get('sampler') or {}, 'checkpoints', 'timed') or 0) for r in S.runs)
    extra = sum(max(0, (get(r.get('sampler') or {}, 'checkpoints', 'requested') or 0)
                    - (get(r.get('sampler') or {}, 'checkpoints', 'own') or 0)) for r in S.runs)
    logical = [get(trial, 'references', 'end', 'logical_match') for trial in S.trials.values()]
    say(f'14. **Stability**: loaddb growth {", ".join(fmt(None if g is None else 100 * g, 2) + "%" for g in growth)} (at most '
        f'5% {word(every(None if g is None else g <= 0.05 for g in growth))}); checkpoints by the timer {timed}, requested '
        f'beyond the arms\' own {extra}; autovacuum on loaddb in '
        f'{sum(bool((r.get("sampler") or {}).get("autovacuum_loaddb")) for r in S.runs)} windows; the benchmark '
        f'database\'s logical digest at the end equal to the start\'s {word(every(logical))}')
    projection = time_projection(S.runs)
    say(f'15. **The time projection**: the registered {projection["planned"]} runs at this session\'s median run times '
        f'(each run from its start to its record): {fmt(projection["minutes"], 1)} minutes for the '
        f'{projection["runs"]} runs whose kind ran here, {projection["borrowed"]} of them warm or moderate runs at the '
        f'busy block\'s median for their arm (the smoke run\'s pass is busy only); set-up and the trial\'s end not '
        f'included')
    say('16. **analysis/exp12.py** ran end to end on these records: this output')
    return lines


# Smoke item 15: the registered session's runs by (block, arm), the controls in their slots.
PLANNED_RUNS = {('busy', 'full'): 15, ('busy', 'pgdelta'): 15, ('busy', PROXY): 15, ('busy', PROXY_S1): 10,
                ('busy', 'none'): 5, ('busy', 'capture'): 5, ('busy', 'bracket'): 5, ('warm', 'full'): 5,
                ('warm', PROXY): 5, ('moderate', 'full'): 6, ('moderate', PROXY): 6, ('moderate', 'none'): 3,
                ('moderate', PROXY_S1): 3, ('busy', 'pause2'): 2, ('busy', 'burn4'): 2, ('moderate', 'pause2'): 1,
                **{('calibration', arm): 2 for arm in CALIBRATION_ARMS}}


def time_projection(runs, plan=PLANNED_RUNS) -> dict:
    """Smoke item 15: the registered runs at this session's median run times (each run from its start to its record),
    by (block, arm). The smoke run makes one busy pass and one calibration pass, so a warm or moderate kind that
    didn't run here takes the busy block's median for its arm (`borrowed`); a kind with neither is left out."""
    durations = {}
    for r in runs:
        if r['started_utc'] is not None and r['finished_utc'] is not None:
            durations.setdefault((r['block'], r['method']), []).append(r['finished_utc'] - r['started_utc'])
    known, counted, borrowed = 0.0, 0, 0
    for (block, arm), count in plan.items():
        value = median(durations.get((block, arm), []))
        if value is None and block in ('warm', 'moderate'):
            value = median(durations.get(('busy', arm), []))
            borrowed += count if value is not None else 0
        if value is not None:
            known += count * value
            counted += count
    return {'planned': sum(plan.values()), 'runs': counted, 'borrowed': borrowed,
            'minutes': known / 60 if counted else None}


def verdicts(S):
    """Every section, in the order of judgment; returns (markdown lines, the main view's cells)."""
    cells, ctx = S.cells('main'), S.ctx
    cells.consulted = []
    d1 = decision1(cells, ctx)
    d2 = decision2(cells, ctx, d1['proxy'])
    base = (d1['row'], d1['cause'], d2['row'], d1['r2']['verdict'])
    cells.consulted = []
    a5v, a6v = a5(cells, ctx), a6(cells, ctx)
    lines = counts_lines(S) + [''] + load_lines(S) + [''] + g4_lines(S, ctx) + [''] + a1_lines(S, ctx) + ['']
    lines += durability_lines(S, ctx) + [''] + instrument_lines(S, ctx) + [''] + null_lines(S) + ['']
    lines += cell_lines(S) + [''] + ledger_lines(S, cells, ctx, d1) + [''] + decision_lines(S, cells, ctx, d1, d2) + ['']
    beside, moderate = beside_lines(S, cells, ctx, d1, d2, a5v)
    flags, weak, near, caveats = flag_lines(S, cells, ctx, d1, d2, base)
    lines += [f'- **A5**: {a5v["text"]}', f'- **A6**: {a6v["text"]}', ''] + beside + [''] + flags + ['']
    lines += e_lines(S, cells, ctx, d1, d2, a5v, a6v, moderate, caveats, near) + ['']
    lines += a_lines(S, cells, ctx, a5v, a6v, weak, near, caveats) + [''] + time_lines(S, cells) + ['']
    lines += cpu_lines(S, ctx, cells) + [''] + mechanism_lines(S, cells) + [''] + order_lines(S, cells) + ['']
    lines += smoke_lines(S, cells, ctx)
    lines += ['', '## The robustness views\' cells (busy block)']
    for view in ROBUSTNESS:
        if VIEWS[view]['paired']:
            continue
        lines += [f'### {VIEW_LABEL[view]}'] + cell_table(S, S.cells(view), ('busy',))
    return lines, {'d1': d1, 'd2': d2, 'a5': a5v, 'a6': a6v, 'weak': weak, 'near': near}


# --- data and figures ---------------------------------------------------------------------------

KEEP = ['sweep', 'trial_id', 'block', 'repeat', 'block_repeat', 'control_slot', 'speed', 'method', 'tier', 'status',
        'void', 'load_void', 'counts', 'left_out', 'load_rate', 'load_run', 'total_s', 't0_utc', 't1_utc',
        'clock_offset_s', 'window_buckets', 'worst_drop', 'work_lost_s', 'worst_drop_net', 'work_lost_net_s',
        'late_count_s', 'late_count_2l_s', 'worst_drop_05s', 'worst_drop_2s', 'work_lost_15s_s', 'worst_drop_15s',
        'completed_ratio', 'backlog_end', 'backlog_area_s', 'lat_p50_ms', 'lat_p95_ms', 'lat_p99_ms', 'lat_p999_ms',
        'lat_max_ms', 'lat_over_10ms', 'lat_over_50ms', 'lat_over_100ms', 'lat_p99_worst5s_ms', 'settle_s',
        'settle_gate_passed', 'settle_log_ok', 'settle_p50_ms', 'settle_p99_ms', 'dest_wait_s', 'tail_verification_s',
        'cpu_source_busy_s', 'cpu_source_calibrated_s', 'cpu_source_excess_s', 'steal_s', 'client_wait_s',
        'client_wait_worst_bucket_s', 'dirty_increase_bytes', 'md_write_ratio', 'flag_unsettled', 'flag_checkpoint',
        'flag_steal', 'flag_cache', 'flag_client', 'flag_client_wait_w', 'flag_client_wait_d', 'flag_autovacuum',
        'g4_bytes', 'g4_start', 'g4_atomic', 'g4_cut', 'g4_coverage', 'g4_rows', 'g4_static_digest', 'g4_schema',
        'amcheck_status', 'capture_end_lsn', 'staleness_s', 'proxy_backup_held', 'proxy_all_hashed', 'wire_s2d_bytes',
        'error']
CELL_KEEP = ('n', 'short', 'total_s', 'total_max', 'W', 'W_null_median', 'W_null_p95', 'W_net', 'W_measurable',
             'W_bound', 'D', 'D_null_median', 'D_null_p95', 'D_net', 'D_measurable', 'D_bound', 'lat_p50_ms',
             'lat_p95_ms', 'lat_p99_ms', 'p99_worst5s_ms', 'cpu_excess_s', 'flag_cell', 'client_wait')


def slowdown(S, r):
    """What a run's record gets as `slowdown` (measure.SLOWDOWN_FIELDS), from the logs, the null and the sampler."""
    stats, sampler = r['stats'], r.get('sampler') or {}
    if stats is None:
        return None
    null = S.null(r)
    lat = stats['latency']

    def net(value, stat):
        if value is None or null is None:
            return None
        return round(max(0.0, value - float(np.median(null[stat]))), 4)
    return {'window_buckets': stats['T'], 'worst_drop': stats['D'], 'work_lost_s': round(stats['W'], 4),
            'worst_drop_net': net(stats['D'], 'D'), 'work_lost_net_s': net(stats['W'], 'W'),
            'late_count_s': stats['late_L'], 'lat_p50_ms': lat['p50_ms'], 'lat_p95_ms': lat['p95_ms'],
            'lat_p99_ms': lat['p99_ms'], 'lat_p999_ms': lat['p999_ms'], 'lat_max_ms': lat['max_ms'],
            'lat_p99_worst5s_ms': stats['p99_worst5s_ms'], 'backlog_end': stats['backlog_end'],
            'client_wait_s': sampler.get('client_wait_s'),
            'client_wait_worst_bucket_s': sampler.get('client_wait_worst_bucket_s'),
            **{f'flag_{name}': r['flags'].get(name) for name in ('checkpoint', 'steal', 'client', 'client_wait_w',
                                                                  'client_wait_d', 'autovacuum')}}


def run_row(S, r):
    """One run's row: measure.summary_row of its record with its `slowdown` set, and what the analysis adds."""
    record = dict(r['record'])
    record['slowdown'] = slowdown(S, r)
    row = measure.summary_row(record)
    stats, settle = r['stats'] or {}, r['settle'] or {}
    lat = stats.get('latency') or {}
    row.update(sweep=r['sweep'], counts=r['counts'], left_out=r['left_out'], late_count_2l_s=stats.get('late_2L'),
               worst_drop_05s=stats.get('D05'), worst_drop_2s=stats.get('D2'), work_lost_15s_s=stats.get('W15'),
               worst_drop_15s=stats.get('D15'), completed_ratio=stats.get('completed_ratio'),
               backlog_area_s=stats.get('backlog_area_s'), lat_over_10ms=lat.get('over_10ms'),
               lat_over_50ms=lat.get('over_50ms'), lat_over_100ms=lat.get('over_100ms'),
               settle_log_ok=S.gate_log_ok(r), settle_p50_ms=settle.get('p50_ms'), settle_p99_ms=settle.get('p99_ms'),
               flag_unsettled=r['flags'].get('unsettled'), wire_s2d_bytes=r['wire_s2d'], total_s=r['total_s'],
               cpu_source_excess_s=None if r['cpu_excess'] is None else round(r['cpu_excess'], 3))
    return {key: row.get(key) for key in KEEP}


def write_data(S, datadir, tag):
    datadir = Path(datadir)
    datadir.mkdir(parents=True, exist_ok=True)
    runs = datadir / f'exp12{tag}-runs.csv'
    order = {block: i for i, block in enumerate(BLOCKS)}
    with runs.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=KEEP)
        writer.writeheader()
        for r in sorted(S.runs, key=lambda r: (r['sweep'], r['trial_id'] or '', order.get(r['block'], 9),
                                                r['repeat'] if r['repeat'] is not None else -1,
                                                SPEEDS.index(r['speed']) if r['speed'] in SPEEDS else 9, r['method'])):
            writer.writerow(run_row(S, r))
    cells_path = datadir / f'exp12{tag}-cells.csv'
    with cells_path.open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['view', 'block', 'speed', 'method', 'tier', *CELL_KEEP, 'W_diff', 'W_delta', 'D_diff',
                         'D_delta', 'meets', 'cpu_calibrated_s'])
        def value(v):
            if isinstance(v, bool) or v is None:
                return '' if v is None else v
            return round(v, 5) if isinstance(v, float) else v
        for view in VIEWS:
            cells = S.cells(view)
            for block, speed, arm in cell_keys():
                cell = (S.cells('main') if view == 'paired' else cells).cell(block, speed, arm)
                if cell is None:
                    continue
                found = cells.compare((block, speed, arm)) if arm not in ('full', *CONTROLS) else None
                meets = None if found is None else all(found[k]['diff'] <= found[k]['delta'] + 1e-12 for k in 'WD')
                writer.writerow([view, block, speed or '', arm, measure.TIERS.get(arm),
                                 *(value(cell.get(k)) for k in CELL_KEEP),
                                 *(value(found[k][part]) if found else '' for k in 'WD' for part in ('diff', 'delta')),
                                 value(meets), value(S.ctx['cpu'].get(arm))])
            cells.consulted = []
    return runs, cells_path


def fig_timeseries(S, tag):
    """Completed over scheduled and p99 per second around t0: the median run (by W) of each arm and speed, busy block;
    the controls apart."""
    cells = S.cells('main')
    columns = [*SPEEDS, None]
    fig, axes = plt.subplots(2, len(columns), figsize=(4.4 * len(columns), 6.2), sharex='col', squeeze=False)
    for column, speed in enumerate(columns):
        top, bottom = axes[0][column], axes[1][column]
        for arm, label, color, marker in ARMS:
            if (arm in CONTROLS) != (speed is None):
                continue
            cell = cells.cell('busy', speed, arm)
            if cell is None:
                continue
            runs = sorted(cell['runs'], key=lambda r: r['stats']['W'])
            series = runs[(len(runs) - 1) // 2]['stats']['series']
            xs = [series['first_s'] + i for i in range(len(series['ratio']))]
            top.plot(xs, [np.nan if v is None else v for v in series['ratio']], color=color, lw=1.2, label=label)
            bottom.plot(xs[:len(series['p99_ms'])], [np.nan if v is None else v for v in series['p99_ms']],
                        color=color, lw=1.2)
        top.axvline(0, color=COLORS['black'], lw=0.6, ls=':')
        bottom.axvline(0, color=COLORS['black'], lw=0.6, ls=':')
        top.set_title(f'busy block, {speed_name(speed) if speed else "the controls"}', fontsize=10.5)
        bottom.set_xlabel('Seconds from t0 (the first timed phase\'s start)')
        bottom.set_yscale('log')
    axes[0][0].set_ylabel('Completed / scheduled, per s')
    axes[1][0].set_ylabel('p99 per s (ms, log)')
    handles, labels = [], []
    for ax in axes[0]:
        for handle, label in zip(*ax.get_legend_handles_labels()):
            if label not in labels:
                handles.append(handle)
                labels.append(label)
    fig.legend(handles, labels, loc='lower center', ncol=3, fontsize=8, bbox_to_anchor=(0.5, -0.1))
    fig.suptitle('EXP-12: the load around each refresh (the median run of each arm and speed)', fontsize=12)
    return plotting.save(fig, f'exp12-timeseries{tag}')


def fig_loss(S, tag):
    """W_net and D_net per arm and speed (busy block), each over its null's band (the 95th percentile less the median),
    with full's net value plus δ (the line an arm must stay under) marked."""
    cells = S.cells('main')
    arms = [arm for arm in ARM_NAMES if arm not in CONTROLS]
    fig, axes = plt.subplots(1, 2, figsize=(12.5, 4.6), squeeze=False)
    for ax, role, unit in ((axes[0][0], 'W', 'W_net (s of the load\'s work)'), (axes[0][1], 'D', 'D_net')):
        x, ticks, labels = 0, [], []
        for speed in SPEEDS:
            for arm in arms:
                cell = cells.cell('busy', speed, arm)
                if cell is None or cell.get(f'{role}_net') is None:
                    continue
                ax.bar(x, cell.get(f'{role}_bound') or 0, width=0.8, color=COLORS['grey'], alpha=0.2,
                       label='the null\'s band (95th percentile less the median)' if not ticks else None)
                ax.bar(x, cell[f'{role}_net'], width=0.5, color=ARM_COLOR[arm], hatch='' if not cell['short'] else '//')
                found = cells.compare(('busy', speed, arm)) if arm != 'full' else None
                if found:
                    ax.plot([x - 0.35, x + 0.35], [found[role]['b_net'] + found[role]['delta']] * 2,
                            color=COLORS['black'], lw=1.4, label='full\'s net value plus δ' if not any(
                                'plus δ' in text for text in ax.get_legend_handles_labels()[1]) else None)
                ticks.append(x)
                labels.append(f'{arm}\n{speed_name(speed)}')
                x += 1
            x += 0.6
        ax.set_xticks(ticks, labels, fontsize=6.5, rotation=90)
        ax.set_ylabel(unit)
        ax.grid(axis='x', visible=False)
        ax.legend(fontsize=7.5, loc='upper left')
    cells.consulted = []
    fig.suptitle('EXP-12: each arm\'s net loss in the busy block (medians; hatched: a short cell)', fontsize=12)
    return plotting.save(fig, f'exp12-loss{tag}')


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--sweeps', nargs='+', default=[str(p) for p in SWEEPS],
                        help='results directories from remote.py fetch (default: exp12-hot)')
    parser.add_argument('--late-limit-ms', type=float, default=10.0,
                        help='L, the late count\'s limit (smoke item 3); 2L is the second view')
    parser.add_argument('--null-resamples', type=int, default=10_000)
    parser.add_argument('--null-seed', type=int, default=121)
    parser.add_argument('--client-wait-share', type=float, default=0.25,
                        help='A8\'s client wait: the share of W (or of the worst bucket\'s lost time) that flags')
    parser.add_argument('--capacity-tps', type=float, help='the smoke run\'s C, for A12')
    parser.add_argument('--busy-cpu', type=float, metavar='SHARE',
                        help='the busy CPU at R_busy as smoke item 2 recorded it (a share of the source\'s CPUs); it '
                             'selects E4\'s and E7\'s ranges (without it: the smoke run\'s busy settles\' median; on '
                             'the main sweep, no range)')
    parser.add_argument('--idle-noise-bytes', type=int, metavar='BYTES',
                        help='check (a) judged again against max(4 MiB, this), the smoke run\'s idle noise')
    parser.add_argument('--datadir', default=str(DATA))
    parser.add_argument('--figdir', help='write figures here instead of docs/figures')
    parser.add_argument('--tag', default='', help='suffix for data and figure names (e.g. -smoke)')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.null_resamples < 100:
        parser.error('--null-resamples must be at least 100')
    if args.late_limit_ms <= 0 or not 0 < args.client_wait_share <= 1:
        parser.error('--late-limit-ms must be positive and --client-wait-share in (0, 1]')
    # The run line's placeholders (C=0; BUSY_CPU=0) until smoke items 2 and 13 give the values.
    if args.capacity_tps is not None and args.capacity_tps <= 0:
        parser.error("--capacity-tps must be the smoke run's C (positive), not the run line's placeholder")
    if args.busy_cpu is not None and not 0 < args.busy_cpu <= 1:
        parser.error("--busy-cpu must be smoke item 2's busy CPU at R_busy, a share in (0, 1], not the placeholder")
    if args.idle_noise_bytes is not None and args.idle_noise_bytes < 0:
        parser.error('--idle-noise-bytes cannot be negative')
    sweeps = [Path(s) for s in args.sweeps if Path(s, 'summary.csv').exists() or Path(s, 'runs').is_dir()]
    if not sweeps:
        raise SystemExit(f'no summary.csv or runs/ under {", ".join(args.sweeps)}')
    if args.figdir:
        plotting.FIGURES = Path(args.figdir)
    plotting.style()
    runs, trials, metas = [], {}, []
    for sweep in sweeps:
        r, t, meta = load(sweep)
        runs += r
        trials.update(t)
        metas.append(meta)
    S = Session(runs, trials, metas, sweeps=sweeps, late_limit_ms=args.late_limit_ms, resamples=args.null_resamples,
                seed=args.null_seed, client_wait_share=args.client_wait_share, idle_noise=args.idle_noise_bytes,
                busy_cpu=args.busy_cpu, capacity_tps=args.capacity_tps)
    lines, _ = verdicts(S)
    print(f'{len(runs)} runs from {", ".join(str(s) for s in sweeps)}; L = {args.late_limit_ms:g} ms; the null: '
          f'{args.null_resamples} resamples from seed {args.null_seed}')
    print(*write_data(S, args.datadir, args.tag), sep='\n')
    print('\n'.join(lines))
    print(fig_timeseries(S, args.tag))
    print(fig_loss(S, args.tag))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
