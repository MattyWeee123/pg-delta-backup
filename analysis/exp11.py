"""EXP-11 (T2: the fastest durable full copy, and whether v1's path still beats it), as pre-registered.

    .venv/bin/python analysis/exp11.py [--sweeps results/exp04/exp11-local results/exp04/exp11-uniform]
        [--audit FILE] [--idle-noise-bytes BYTES] [--tag ...] [--datadir DIR] [--figdir DIR]

Reads each sweep's summary.csv, per-run records, trial records and run.json from `remote.py fetch`
(the hot-and-append invocation and the uniform one), and EXP-09's curated
docs/experiments/data/exp09-10-cells.csv and exp09-10-runs.csv (the drift gates and v0's L3 and
L4). Writes the curated data docs/experiments/data/exp11-runs.csv and exp11-cells.csv, the figures
docs/figures/exp11-baselines.svg and exp11-phases.svg, and prints every prediction and rule of
docs/experiments/EXP-11-full-copy-baseline.md with its verdict: the pooled choice of each envelope's
arm, the R_* ratios and their views, EXP-09's HA rule re-judged with its qualifier and flags, the
crossovers, the drift gates, durability and the syscall audit, L1-L4, the ledger's rules, E1-E15
and A1-A9.

Two sets of runs, as the pre-registration says. The main block's runs (5 per cell) for every
prediction and rule, and for choosing each envelope's arm (pooled over the three workloads). For
the HA verdict, its ratios and A3, every run outside the warm-source block: 10 unthrottled runs per
extended arm in hot and append. Every statistic is a per-workload median, with the maximum beside it.

A run counts if it passed its verification and every durability check that applies (check (a) not
if A6 voided it; check (b) for full copies; parcopy's fsync count), and isn't void. Arms that fail
A4 (a copy the tool itself aborted, an error of kind 'tool', included) or that A5 reclassifies are
left out of every baseline. The syscall audit's results, when given
(--audit), are a JSON object mapping each arm to {"passed": bool, "uncovered": [paths],
"tool_uncovered": [paths its own flush left, covered only by a sync -f], and the audit's counts
"files", "uncovered_files", "directory_changes", "uncovered_directory_changes" (and the same prefixed
"tool_")}, or {"arms": that}, as `experiments/network/durability_audit.py summary --json RUN_DIR`
prints it; or that RUN_DIR itself. The paths are examples; the counts are what the audit table reports.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path

import exp04
import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from experiments.network import durability_audit, measure  # noqa: E402

DATA = ROOT / 'docs' / 'experiments' / 'data'
SWEEPS = [ROOT / 'results' / 'exp04' / 'exp11-local', ROOT / 'results' / 'exp04' / 'exp11-uniform']
PATTERNS = ['hot', 'append', 'uniform']  # the session's order
LOCAL = ['hot', 'append']  # the workloads EXP-09's HA rule reads
U = measure.UNTHROTTLED
SPEEDS = ['2g', '5g', '10g', U]
EXP09_SPEEDS = ['2g', '5g', U]  # v0's L3 and L4 are interpolated over these only
SPEED_LABEL = {'2g': '2 Gbit/s', '5g': '5 Gbit/s', '10g': '10 Gbit/s', U: 'unthrottled'}
V1, V1_J8 = 'pgdelta_direct_inplace', 'pgdelta_direct_inplace_j8'
ARMS = [('full', 'full: pg_basebackup, fsync (Omni today)', COLORS['grey'], '-', 'o'),
        ('full_nosync', 'full_nosync: --no-sync (not durable)', COLORS['grey'], ':', 'x'),
        ('full_syncfs', 'full_syncfs: --sync-method=syncfs', COLORS['black'], '-', 's'),
        ('full_wb', 'full_wb: write-back at 64 MiB on dest (host-tuned)', COLORS['purple'], '-', 'v'),
        ('probackup8', 'probackup8: pg_probackup catchup -j 8 + sync -f', COLORS['orange'], '-', '^'),
        ('probackup4', 'probackup4: -j 4', COLORS['yellow'], '-', '^'),
        ('parcopy8', 'parcopy8: the bound, 8 streams', COLORS['blue'], '-', 'D'),
        ('parcopy4', 'parcopy4: 4 streams', COLORS['sky'], '-', 'D'),
        (V1, 'v1: pgdelta direct, in place, 4 jobs', COLORS['green'], '-', 'o'),
        (V1_J8, 'v1 at 8 jobs', COLORS['red'], '--', 'o')]
ARM_NAMES = [arm for arm, *_ in ARMS]
# The envelopes (Baselines and ratios) by the tiers they admit (measure.TIERS). full_nosync
# (diagnostic) and v1 are in none.
ENVELOPES = {'B_fast': ('omni_today', 'released', 'bound'), 'B_rel': ('omni_today', 'released'),
             'B_tuned': ('omni_today', 'released', 'bound', 'host_tuned'), 'F0': ('omni_today',)}
RATIO_NAME = {'B_fast': 'R_fast', 'B_rel': 'R_rel', 'B_tuned': 'R_tuned', 'F0': 'R_F0'}
MATCHED_STREAMS = 4  # matched parallelism: v1 at 4 jobs against arms of at most 4 streams
TIE_S = 0.05  # ties in the pooled choice go to the lower pooled maximum
GATE_S, GATE_BALANCE = 1.0, 1.1  # A3: floor(U) + 1.0 s; the busiest stream at most 1.1 x the mean
NEAR = 0.03  # a deciding ratio this close to 0.90 or 1.00 is "near the line"
BELOW = 2.0  # L3: a crossover below the slowest speed is "below 2"
# The ratios the HA rows read, and the lines each is compared with: ('ge', 1.00) is Out's.
THRESHOLDS = {'fast': (('le', 0.90), ('ge', 1.00)), 'adj_fast': (('le', 0.90),), 'rel': (('le', 0.90),),
              'adj_rel': (('le', 0.90),)}
ROWS = ('Out on fast links', 'Confirmed', 'Bound missing', 'Undecided')
RELEASED = ('the released copiers tested here (`pg_basebackup` with fsync or syncfs, `pg_probackup catchup '
            '-j 4` and `-j 8`) at 2.1 GB on these VMs')
QUALIFIER = f'v1 is faster than {RELEASED}'
BOUND_MISSING = f'Within reach of {RELEASED}; bound not measured.'
VIEWS = {'main': ('main',), 'ha': ('main', 'extension'), 'warm': ('warm_source',)}

# The summary's columns this script reads (measure.CSV_FIELDS), by type.
TEXT = ('trial_id', 'pattern', 'speed', 'method', 'status', 'tier', 'block', 'source_cache', 'error', 'failure_kind')
FLAGS = ('verified', 'durable', 'durability_ok', 'void', 'bytes_identical', 'logical_match')
NUMERIC = ('order_index', 'target_gbit', 'measured_s2d_gbit', 'measured_s2d_gbit_8streams', 'total_s',
           'total_no_ssh_s', 'receiver_start_s', 'transfer_s', 'rebuild_s', 'fsync_s', 'verify_wait_s',
           'hash_basis_s', 'wire_s2d_bytes', 'wire_d2s_bytes', 'output_bytes', 'cpu_source_s', 'cpu_dest_s',
           'dirty_increase_mb', 'disk_write_dest_bytes', 'disk_read_dest_bytes', 'pbb_checkpoint_s',
           'pbb_stream_s', 'pbb_wal_end_s', 'pbb_sync_s', 'pbb_manifest_s', 'backup_mode_adder_s',
           'probackup_sync_s', 'probackup_copy_s', 'probackup_stop_s', 'probackup_files_sync_s',
           'parcopy_streams', 'parcopy_max_stream_bytes', 'parcopy_balance', 'parcopy_listening_s',
           'parcopy_first_byte_s', 'parcopy_last_byte_s', 'parcopy_write_s', 'parcopy_fsync_s', 'parcopy_fsync_calls',
           'parcopy_fsync_expected', 'usable_s', 'output_start_s', 'link_time_s', 'above_link_s',
           'sysctl_dirty_background_bytes', 'sysctl_dirty_background_ratio')
KEEP = ['sweep', 'trial_id', 'pattern', 'speed', 'method', 'repeat', 'block', 'source_cache', 'tier', 'status',
        'verified', 'void', 'usable', 'left_out', 'total_s', 'total_no_ssh_s', 'usable_s', 'output_start_s',
        'measured_s2d_gbit', 'measured_s2d_gbit_8streams', 'link_time_s', 'above_link_s', 'wire_s2d_bytes',
        'wire_d2s_bytes', 'output_bytes', 'cpu_source_s', 'cpu_dest_s', 'a_ok', 'b_ok', 'dirty_increase_bytes',
        'dirty_limit_bytes', 'disk_write_dest_bytes', 'pbb_start_s', 'pbb_checkpoint_s', 'pbb_stream_s',
        'pbb_wal_end_s', 'pbb_sync_s', 'pbb_manifest_s', 'backup_mode_adder_s', 'probackup_start_s',
        'probackup_copy_s', 'probackup_stop_s', 'probackup_files_sync_s', 'probackup_sync_s', 'parcopy_streams',
        'parcopy_balance', 'parcopy_listening_s', 'parcopy_first_byte_s', 'parcopy_last_byte_s', 'parcopy_write_s',
        'parcopy_fsync_s', 'parcopy_fsync_calls', 'parcopy_fsync_expected', 'fsync_ok', 'receiver_start_s', 'transfer_s', 'rebuild_s', 'fsync_s',
        'verify_wait_s', 'logical_match', 'bytes_identical', 'sysctl_dirty_background_bytes',
        'sysctl_dirty_background_ratio', 'error', 'failure_kind']
CELL_COLUMNS = ('total_s', 'total_no_ssh_s', 'usable_s', 'above_link_s', 'link_time_s', 'measured_s2d_gbit',
                'measured_s2d_gbit_8streams', 'wire_s2d_bytes', 'cpu_source_s', 'cpu_dest_s', 'pbb_checkpoint_s',
                'pbb_stream_s', 'pbb_wal_end_s', 'pbb_sync_s', 'backup_mode_adder_s', 'probackup_copy_s',
                'probackup_sync_s', 'parcopy_last_byte_s', 'parcopy_fsync_s', 'parcopy_balance',
                'receiver_start_s', 'transfer_s', 'dirty_increase_bytes')


# --- small helpers ------------------------------------------------------------

def number(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def flag(value):
    return {'True': True, 'False': False}.get(value)


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


def listing(items, digits=2):
    """'hot/2 Gbit/s 10.25, ...' from (pattern, speed, value) triples with a value."""
    return ', '.join(f'{p}/{SPEED_LABEL.get(s, s)} {fmt(v, digits)}' for p, s, v in items if v is not None) or 'no data'


def streams(method):
    """How many streams an arm moves its bytes over (pg_probackup's jobs, parcopy's streams; else 1)."""
    return ((measure.PROBACKUP_VARIANTS.get(method) or {}).get('jobs')
            or (measure.PARCOPY_VARIANTS.get(method) or {}).get('streams') or 1)


def envelope_arms(envelope, matched=False):
    """The arms an envelope admits (by tier), at matched parallelism only those of at most 4 streams."""
    return [arm for arm, tier in measure.TIERS.items() if tier in ENVELOPES[envelope]
            and (not matched or streams(arm) <= MATCHED_STREAMS)]


def choose(pooled):
    """The baseline arm from {arm: pooled times}: the lowest median; arms within TIE_S of it are tied,
    and a tie goes to the lower pooled maximum. None if no arm has a run."""
    stats = {arm: (statistics.median(times), max(times), len(times)) for arm, times in pooled.items() if times}
    if not stats:
        return None
    best = min(m for m, _, _ in stats.values())
    tied = sorted(arm for arm, (m, _, _) in stats.items() if m - best <= TIE_S + 1e-9)
    arm = min(tied, key=lambda a: (stats[a][1], stats[a][0], a))
    return {'arm': arm, 'median': stats[arm][0], 'max': stats[arm][1], 'n': stats[arm][2], 'tied': tied,
            'stats': stats}


# --- loading -----------------------------------------------------------------

def b_check(row):
    """Check (b) from the summary alone: a full copy's disk writes at least 0.98 x its output's bytes."""
    if row['method'] not in measure.FULL_COPY_METHODS:
        return None
    written, output = row['disk_write_dest_bytes'], row['output_bytes']
    if written is None or output is None:
        return None
    return written >= math.ceil(measure.DISK_WRITE_FRACTION * output)


def failure_kind(record, row):
    """An errored run's kind: 'tool' when a command of its own timed phases failed (A4 counts it for
    parcopy* and probackup*), 'harness' otherwise; the record's or the summary's, or, for a record
    from before the harness wrote it, found from its phases. None without a record to tell."""
    kind = record.get('failure_kind') or row.get('failure_kind')
    if kind is None and record and row.get('status') == 'error':
        failed = any((phase or {}).get('failures') for phase in (record.get('phases') or {}).values())
        kind = 'tool' if failed else 'harness'
    return kind


def from_record(record, row, run, idle_noise=None):
    """What a run's record adds to its summary row: checks (a) and (b) apart, the raw Dirty +
    Writeback increase and its limit (both recomputed from the summary without a record), parcopy's
    fsync count (A5) apart from check (a), the launch and exit of pg_basebackup's and pg_probackup's
    own phases, and an error's kind (failure_kind).

    With idle_noise (--idle-noise-bytes, the smoke run's), check (a) is judged again against the
    registered limit, max(4 MiB, idle_noise), whatever limit the sweep itself used."""
    durability = record.get('durability') or {}
    reported = record.get('tool_reported') or {}
    pbb = reported.get('basebackup_phases') or {}
    probackup = reported.get('probackup_phases') or {}
    increase = durability.get('dirty_writeback_increase_bytes')
    if increase is None and row['dirty_increase_mb'] is not None:
        increase = row['dirty_increase_mb'] * 1e6
    limit = durability.get('limit_bytes')
    if idle_noise is not None:
        limit = max(measure.DIRTY_LIMIT_BYTES, int(idle_noise))
    elif limit is None:
        limit = max(measure.DIRTY_LIMIT_BYTES, int(run.get('idle_noise_bytes') or 0))
    # measure.durability_check folds a parcopy run's fsync count into its a_ok; check (a) here is the
    # Dirty + Writeback increase alone, and the count (the receiver's and the output's) is A5's own.
    fsync_count = durability.get('fsync_count')
    if 'a_ok' in durability and fsync_count is None and idle_noise is None:
        a_ok = durability['a_ok']
    else:
        a_ok = None if increase is None else increase <= limit
    return {'a_ok': a_ok, 'b_ok': durability['b_ok'] if 'b_ok' in durability else b_check(row),
            'fsync_ok': (fsync_count or {}).get('ok'),
            'dirty_increase_bytes': increase, 'dirty_limit_bytes': limit,
            'pbb_start_s': pbb.get('start_s'), 'probackup_start_s': probackup.get('start_s'),
            'failure_kind': failure_kind(record, row)}


def load(sweep, idle_noise=None):
    """One sweep: (rows, trials, run). A row per summary line, typed, with what its record adds (check (a)
    against max(4 MiB, idle_noise) when it is given: from_record)."""
    sweep = Path(sweep)
    run = json.loads((sweep / 'run.json').read_text()) if (sweep / 'run.json').exists() else {}
    trials = {}
    for path in sorted((sweep / 'trials').glob('*.json')):
        trial = json.loads(path.read_text())
        trials[(sweep.name, trial['trial_id'])] = trial
    rows = []
    with (sweep / 'summary.csv').open(newline='') as f:
        for index, line in enumerate(csv.DictReader(f)):
            row = {'sweep': sweep.name, 'index': index, **{k: line.get(k) or None for k in TEXT},
                   **{k: flag(line.get(k)) for k in FLAGS}, **{k: number(line.get(k)) for k in NUMERIC},
                   'repeat': int(line['repeat'])}
            path = sweep / 'runs' / line['trial_id'] / f"r{line['repeat']}-{line['speed']}-{line['method']}.json"
            row.update(from_record(json.loads(path.read_text()) if path.exists() else {}, row, run, idle_noise))
            rows.append(row)
    return rows, trials, run


def load_exp09(cells_path, runs_path):
    """EXP-09's medians, (pattern, speed, method) -> column -> value, and its measured single-stream
    link per (pattern, speed): the median over every run there (one link test per cell)."""
    cells = {}
    with open(cells_path, newline='') as f:
        for row in csv.DictReader(f):
            cells[(row['pattern'], row['speed'], row['method'])] = {
                key[:-len('_median')]: number(value) for key, value in row.items() if key.endswith('_median')}
    links = {}
    with open(runs_path, newline='') as f:
        for row in csv.DictReader(f):
            links.setdefault((row['pattern'], row['speed']), []).append(number(row.get('measured_s2d_gbit')))
    return {'cells': cells, 'link': {key: median(values) for key, values in links.items()}}


# --- the session -------------------------------------------------------------

class Session:
    """Every run of the session with its standing under EXP-11's checks, and the trials and runs."""

    def __init__(self, rows, trials=None, runs=None, idle_noise=None):
        self.rows, self.trials, self.meta = rows, trials or {}, runs or []
        self.idle_noise = idle_noise  # --idle-noise-bytes, which load() judged check (a) with, if given
        self.audit = {}  # the syscall audit's results, by arm (verdicts() sets them)
        self._choices = {}
        self.assess()
        self.patterns = [p for p in PATTERNS if any(r['pattern'] == p for r in rows)] + sorted(
            {r['pattern'] for r in rows} - set(PATTERNS))

    def assess(self):
        """A6, then each run's durability, void and standing; A4's and A5's excluded arms."""
        rows = self.rows
        # A6: once full_nosync, the negative control, passes check (a), (a) is void for the session.
        self.a_void = any(r['a_ok'] for r in rows if r['method'] in measure.NON_DURABLE_METHODS)
        for r in rows:
            durable = r['method'] not in measure.NON_DURABLE_METHODS
            checks = {} if self.a_void else {'a': r['a_ok']}
            if r['method'] in measure.FULL_COPY_METHODS:
                checks['b'] = r['b_ok']
            if r['method'] in measure.PARCOPY_METHODS:
                # The record's count also checks the output's own (verify_direct); the summary's alone doesn't.
                calls, expected = r['parcopy_fsync_calls'], r['parcopy_fsync_expected']
                checks['fsync count'] = (r['fsync_ok'] if r.get('fsync_ok') is not None
                                         else None if calls is None or expected is None else calls == expected)
            r['durability_failures'] = [name for name, ok in checks.items() if durable and ok is False]
            r['durability_unchecked'] = [name for name, ok in checks.items() if durable and ok is None]
            # Void: the source changed during the trial (its reference digests differ), so a pg_probackup
            # logical difference isn't the tool's. The column is the harness's; the digests are a fallback.
            reference = (self.trials.get((r['sweep'], r['trial_id'])) or {}).get('reference_match')
            r['void'] = bool(r['void']) or (r['method'] in measure.PROBACKUP_METHODS
                                            and r['logical_match'] is False and reference is False)
        # A5: more than one failing run of an arm at one speed in one trial: not durable for the session.
        failing = Counter((r['sweep'], r['trial_id'], r['speed'], r['method']) for r in rows
                          if r['durability_failures'])
        self.not_durable = {}
        for (_, trial, speed, method), count in sorted(failing.items()):
            if count > 1:
                self.not_durable.setdefault(method, []).append(f'{count} runs at {SPEED_LABEL.get(speed, speed)} '
                                                               f'in {trial}')
        # A4: a parcopy run that fails verify_direct or doesn't start; a pg_probackup run that doesn't
        # start or whose logical digest differs while the reference digests match (not void); and a copy
        # of either that the tool itself aborted (an error of kind 'tool': its own timed command failed),
        # which can be neither verified nor started. An error of the harness around the tool fails
        # nothing; one whose kind is unknown (a summary without records) leaves A4 undecided for its arm.
        self.a4, self.a4_undecided = {}, {}
        for r in rows:
            if r['method'] not in measure.PARCOPY_METHODS and r['method'] not in measure.PROBACKUP_METHODS:
                continue
            aborted = r['status'] == 'error' and r.get('failure_kind') == 'tool'
            if (r['status'] == 'fail' and not r['void']) or aborted:
                self.a4.setdefault(r['method'], []).append(r)
            elif r['status'] == 'error' and r.get('failure_kind') is None:
                self.a4_undecided.setdefault(r['method'], []).append(r)
        self.excluded = {method: 'A5: not durable for the session (' + '; '.join(why) + ')'
                         for method, why in self.not_durable.items()}
        for method, failed in self.a4.items():
            aborted = sum(r['status'] == 'error' for r in failed)
            self.excluded[method] = (f'A4: {len(failed)} run(s) failed '
                                     + ('verify_direct or did not start' if method in measure.PARCOPY_METHODS
                                        else 'to start or the logical digest')
                                     + (f', {aborted} of them a copy the tool itself aborted' if aborted else ''))
        for r in rows:
            r['usable'] = self.counts(r)
            r['usable_void'] = self.counts(r, void=True)
            r['left_out'] = '' if r['usable'] else self.why(r)

    def counts(self, r, void=False):
        """Whether a run counts: verified, durable where it applies, not void (unless void runs are
        asked for; then a void run counts if it was verified and durable, whatever its status)."""
        if r['void']:
            if not void:
                return False
        elif r['status'] != 'pass':
            return False
        if not r['verified'] or r['total_s'] is None:
            return False
        return not (r['durability_failures'] or r['durability_unchecked'])

    def why(self, r):
        if r['void']:
            return 'void'
        if r['status'] != 'pass':
            return f'status {r["status"]}'
        if not r['verified']:
            return 'not verified'
        if r['durability_failures']:
            return 'durability failed: ' + ', '.join(r['durability_failures'])
        if r['durability_unchecked']:
            return 'durability unchecked: ' + ', '.join(r['durability_unchecked'])
        return 'no time'

    # Cells.

    def runs(self, method, pattern=None, speed=None, view='main', void=False):
        """The counting runs of one arm: view 'main' (the main block), 'ha' (every block but the
        warm-source one) or 'warm' (the warm-source block)."""
        blocks, key = VIEWS[view], 'usable_void' if void else 'usable'
        return [r for r in self.rows if r['method'] == method and r[key] and (r['block'] or 'main') in blocks
                and (pattern is None or r['pattern'] == pattern) and (speed is None or r['speed'] == speed)]

    def med(self, pattern, speed, method, col='total_s', view='main', void=False):
        return median(r[col] for r in self.runs(method, pattern, speed, view, void))

    def top(self, pattern, speed, method, col='total_s', view='main', void=False):
        return mx(r[col] for r in self.runs(method, pattern, speed, view, void))

    def link(self, pattern, speed, col='measured_s2d_gbit'):
        """This session's median measured rate at a speed in a workload (main block; every run carries
        its cell's link test): single stream, or col='measured_s2d_gbit_8streams'."""
        return median(r[col] for r in self.rows if r['pattern'] == pattern and r['speed'] == speed
                      and (r['block'] or 'main') == 'main')

    # Envelopes and ratios.

    def choice(self, envelope, speed, matched=False, void=False):
        """The envelope's arm at a speed: chosen on main-block runs pooled over the workloads."""
        key = (envelope, speed, matched, void)
        if key not in self._choices:
            self._choices[key] = choose({arm: [r['total_s'] for r in self.runs(arm, speed=speed, void=void)]
                                         for arm in envelope_arms(envelope, matched) if arm not in self.excluded})
        return self._choices[key]

    def baseline(self, envelope, pattern, speed, view='main', matched=False, void=False, col='total_s'):
        """(arm, its median in this workload) for an envelope at a speed; (None, None) where none ran."""
        chosen = self.choice(envelope, speed, matched, void)
        if chosen is None:
            return None, None
        return chosen['arm'], self.med(pattern, speed, chosen['arm'], col, view, void)

    def ratios(self, pattern, v1=V1, matched=False, void=False, view='ha'):
        """v1's unthrottled time over each envelope's arm's, in this workload: R from the medians, with
        R_adj (v1 plus full's backup-mode adder, over a live-source arm), and the robustness views:
        max (maxima), no_ssh (total_no_ssh_s) and paired (the median of v1's run over the arm's run in
        the same (repeat, speed) block; R_adj there adds that block's full run's adder)."""
        v1_runs = self.runs(v1, pattern, U, view, void)
        full_runs = self.runs('full', pattern, U, view, void)
        adder = median(r['backup_mode_adder_s'] for r in full_runs)
        block_adder = {(r['sweep'], r['trial_id'], r['repeat']): r['backup_mode_adder_s'] for r in full_runs}
        out = {'v1': median(r['total_s'] for r in v1_runs), 'v1_max': mx(r['total_s'] for r in v1_runs),
               'n_v1': len(v1_runs), 'adder': adder, 'n_adder': sum(r['backup_mode_adder_s'] is not None
                                                                 for r in full_runs)}
        for name in ENVELOPES:
            chosen = self.choice(name, U, matched, void)
            if chosen is None:
                out[name] = None
                continue
            arm = chosen['arm']
            base = self.runs(arm, pattern, U, view, void)
            live = arm not in measure.SOURCE_STOP_METHODS  # against a stopped source, R_adj is R

            def plus(value, add=adder if live else 0.0):
                return None if value is None or add is None else value + add
            by_block = {(r['sweep'], r['trial_id'], r['repeat']): r for r in base}
            pairs = [(v, by_block[key]) for v in v1_runs
                     if (key := (v['sweep'], v['trial_id'], v['repeat'])) in by_block]
            v1_med, v1_max = out['v1'], out['v1_max']
            b_med, b_max = median(r['total_s'] for r in base), mx(r['total_s'] for r in base)
            v1_ns, b_ns = median(r['total_no_ssh_s'] for r in v1_runs), median(r['total_no_ssh_s'] for r in base)
            out[name] = {
                'arm': arm, 'n': len(base), 'live': live, 'base': b_med, 'base_max': b_max,
                'median': ratio(v1_med, b_med), 'adj': ratio(plus(v1_med), b_med),
                'max': ratio(v1_max, b_max), 'adj_max': ratio(plus(v1_max), b_max),
                'no_ssh': ratio(v1_ns, b_ns), 'adj_no_ssh': ratio(plus(v1_ns), b_ns),
                'paired': median(ratio(v['total_s'], b['total_s']) for v, b in pairs),
                'adj_paired': median(ratio(plus(v['total_s'], block_adder.get((v['sweep'], v['trial_id'],
                                                                               v['repeat'])) or adder)
                                           if live else v['total_s'], b['total_s']) for v, b in pairs),
                'n_pairs': len(pairs)}
        return out

    def all_ratios(self, v1=V1, matched=False, void=False):
        return {p: self.ratios(p, v1, matched, void) for p in self.patterns}

    # Probes, floors and the bound.

    def trial_probes(self, patterns=LOCAL):
        return [(trial.get('pattern'), trial.get('trial_id'), trial.get('probes') or {})
                for _, trial in sorted(self.trials.items()) if trial.get('pattern') in patterns]

    def floor_u(self):
        """floor(U): the median of the durable write floor's measurements in hot and append (start, end)."""
        return median(w.get('seconds') for _, _, probes in self.trial_probes()
                      for w in probes.get('durable_write_floor') or () if isinstance(w, dict))

    def read_floor(self):
        return median((probes.get('read_floor') or {}).get('seconds') for _, _, probes in self.trial_probes())

    def copy_bytes(self):
        """A full copy's source-to-dest bytes: the median over the full-copy arms' main-block runs."""
        return median(r['wire_s2d_bytes'] for r in self.rows if r['method'] in measure.FULL_COPY_METHODS
                      and r['usable'] and (r['block'] or 'main') == 'main')

    def floor(self, speed):
        """floor(s) = max(link time at the 8-stream rate, floor(U), cold read floor); floor(U) unthrottled."""
        floor_u = self.floor_u()
        if speed == U:
            return floor_u
        rate = median(self.link(p, speed, 'measured_s2d_gbit_8streams') for p in self.patterns)
        size = self.copy_bytes()
        link = None if not rate or size is None else size * 8 / (rate * 1e9)
        values = [v for v in (link, floor_u, self.read_floor()) if v is not None]
        return max(values) if values else None

    def bound(self, void=False):
        """The bound and its gate (A3): the faster parcopy arm over its pooled unthrottled runs in hot
        and append (main and extension) against floor(U) + 1.0 s, and its busiest stream against 1.1 x
        the mean (the largest balance over those runs). measured: it ran and wasn't excluded (A4, A5);
        is_fast: B_fast's unthrottled arm is a parcopy arm."""
        pooled = {arm: [r['total_s'] for p in LOCAL for r in self.runs(arm, p, U, 'ha', void)]
                  for arm in measure.PARCOPY_METHODS if arm not in self.excluded}
        pooled = {arm: times for arm, times in pooled.items() if times}
        result = {'built': any(r['method'] in measure.PARCOPY_METHODS for r in self.rows),
                  'excluded': [arm for arm in measure.PARCOPY_METHODS if arm in self.excluded],
                  'measured': bool(pooled), 'arm': None, 'passes': None, 'is_fast': False}
        if not pooled:
            return result
        arm = min(pooled, key=lambda a: (statistics.median(pooled[a]), a))
        floor = self.floor_u()
        med = statistics.median(pooled[arm])
        balance = mx(r['parcopy_balance'] for p in LOCAL for r in self.runs(arm, p, U, 'ha', void))
        slow = None if floor is None else med > floor + GATE_S
        unbalanced = None if balance is None else balance > GATE_BALANCE
        passes = False if (slow or unbalanced) else None if slow is None or unbalanced is None else True
        fast = self.choice('B_fast', U, void=void)
        result.update(arm=arm, median=med, n=len(pooled[arm]), floor=floor, balance=balance, slow=slow,
                      unbalanced=unbalanced, passes=passes,
                      is_fast=bool(fast) and fast['arm'] in measure.PARCOPY_METHODS)
        return result

    # Crossovers.

    def crossover_table(self, exp09=None):
        """analysis/exp04's cells: (pattern, speed) -> {'speed_gbit': this session's median single-stream
        rate, arm: main-block median, each envelope: its chosen arm's median, 'link_model': a full copy's
        bytes at the measured rate, and EXP-09's v0 ('v0': total_s, 'v0_transfer': transfer_s) at
        EXP-09's speeds}."""
        table = {}
        size = self.copy_bytes()
        for p in self.patterns:
            for s in SPEEDS:
                x = self.link(p, s)
                if not x:
                    continue
                cell = {'speed_gbit': x}
                for arm in ARM_NAMES:
                    value = self.med(p, s, arm)
                    if value is not None:
                        cell[arm] = {'total_s': value, 'max': self.top(p, s, arm)}
                for name in ENVELOPES:
                    arm, value = self.baseline(name, p, s)
                    if value is not None:
                        cell[name] = {'total_s': value, 'arm': arm, 'max': self.top(p, s, arm)}
                if size:
                    cell['link_model'] = {'total_s': size * 8 / (x * 1e9)}
                cells = (exp09 or {}).get('cells') or {}
                if s in EXP09_SPEEDS and (p, s, 'pgdelta') in cells:
                    for key, col in (('v0', 'total_s'), ('v0_transfer', 'transfer_s')):
                        if cells[(p, s, 'pgdelta')].get(col):
                            cell[key] = {'total_s': cells[(p, s, 'pgdelta')][col]}
                table[(p, s)] = cell
        return table

    def provisional(self, envelope, speeds):
        """Why a crossover against an envelope is provisional: an arm of it run only at some speeds is
        absent at a speed the crossover rests on, though it beat or tied the arm chosen there wherever
        both ran (or never ran beside it)."""
        absent = {}
        members = [arm for arm in envelope_arms(envelope) if arm not in self.excluded]
        everywhere = [self.choice(envelope, s) for s in SPEEDS]
        for s in speeds:
            here = self.choice(envelope, s)
            if here is None:
                continue
            for arm in members:
                if arm in here['stats'] or not any(c and arm in c['stats'] for c in everywhere):
                    continue
                both = [c for c in everywhere if c and arm in c['stats'] and here['arm'] in c['stats']]
                if not both or any(c['stats'][arm][0] < c['stats'][here['arm']][0] + TIE_S for c in both):
                    absent.setdefault(arm, []).append(SPEED_LABEL.get(s, s))
        return [f'{arm} not run at {", ".join(where)}' for arm, where in absent.items()]

    # Order effects.

    def predecessors(self):
        """Each row's predecessor in its trial (the summary lists runs in the order they ran)."""
        last, before = {}, {}
        for r in sorted(self.rows, key=lambda r: (r['sweep'], r['index'])):
            key = (r['sweep'], r['trial_id'])
            before[id(r)] = last.get(key)
            last[key] = r
        return before


def bracketing(table, pattern, method, baseline):
    """The speeds a crossover rests on: the two around ratio 1, or every speed if it doesn't cross."""
    points = []
    for speed_gbit, value, _, speed in exp04.series(table, pattern, method):
        base = table[(pattern, speed)].get(baseline, {}).get('total_s')
        if base:
            points.append((value / base, speed))
    for (r0, s0), (r1, s1) in zip(points, points[1:]):
        if r0 < 1 <= r1:
            return [s0, s1]
    return [speed for _, speed in points]


def interval(value, how):
    """A crossover as the interval it lies in: exact where it crosses, below the slowest speed where the
    method is slower everywhere, above the fastest where it is faster everywhere; None otherwise."""
    if value is None:
        return None
    if how == 'crosses':
        return value, value
    if how.startswith('slower'):
        return 0.0, value
    if how.startswith('faster'):
        return value, math.inf
    return None


def describe(value, how):
    if how == 'crosses':
        return f'{value:.2f} Gbit/s'
    if how.startswith('faster'):
        return f'none: faster at every speed (up to {value:.2f})'
    if how.startswith('slower'):
        return f'below {value:.2f}: slower at every speed'
    return how


def inside(found, lo, hi):
    """Whether a crossover (value, how) lies in [lo, hi]; None where its interval can't say."""
    span = interval(*found)
    if span is None:
        return None
    if lo <= span[0] and span[1] <= hi:
        return True
    if span[1] < lo or span[0] > hi:
        return False
    return None


def l3_holds(fast, f0):
    """L3 in one workload: v0's crossover against B_fast at least 0.8 x its crossover against F0. A
    crossover below the slowest speed is judged only where 0.8 x the F0 crossover is 2 Gbit/s or more.
    None: not judged."""
    a, b = interval(*fast), interval(*f0)
    if a is None or b is None:
        return None
    if fast[1].startswith('slower'):
        return False if 0.8 * b[0] >= BELOW else None
    if a[0] >= 0.8 * b[1]:
        return True
    if a[1] < 0.8 * b[0]:
        return False
    return None


# --- EXP-09's HA rule, re-judged ------------------------------------------------

def deciding(ratios, view='median', fast='B_fast'):
    """workload -> the four ratios the HA rows read in one view (median, max, no_ssh, paired):
    R_fast, R_adj against B_fast, R_rel, R_adj against B_rel. fast='B_tuned' reads B_tuned instead (A2)."""
    adj = 'adj' if view == 'median' else f'adj_{view}'
    out = {}
    for pattern, r in ratios.items():
        f, rel = r.get(fast) or {}, r.get('B_rel') or {}
        out[pattern] = {'fast': f.get(view), 'adj_fast': f.get(adj), 'rel': rel.get(view), 'adj_rel': rel.get(adj)}
    return out


def verdict(r, bound):
    """The first HA row whose condition holds, checked in order, and Out's qualifier.

    r: workload -> {'fast', 'adj_fast', 'rel', 'adj_rel'} (deciding()); bound: Session.bound()'s
    'measured', 'passes' and 'is_fast'. 'Not judged' if hot's or append's R_fast is missing and
    neither decides Out.
    """
    def get(p, k):
        return (r.get(p) or {}).get(k)

    def le(p, k, line=0.90):
        return get(p, k) is not None and get(p, k) <= line
    released = all(le(p, 'rel') and le(p, 'adj_rel') for p in LOCAL)
    if any(get(p, 'fast') is not None and get(p, 'fast') >= 1.00 for p in LOCAL):
        row = ROWS[0]
    elif any(get(p, 'fast') is None for p in LOCAL):
        row = 'Not judged'
    elif all(le(p, 'fast') and le(p, 'adj_fast') and le(p, 'adj_rel') for p in LOCAL) \
            and bound.get('measured') and bound.get('passes'):
        row = ROWS[1]
    elif (not bound.get('measured') or (bound.get('passes') is False and not bound.get('is_fast'))) and released:
        row = ROWS[2]
    else:
        row = ROWS[3]
    return {'row': row, 'qualifier': row == ROWS[0] and released}


def flip(value, kind, line):
    """The value just across a line from where it is: 'le' rows hold at or below it, 'ge' at or above."""
    if kind == 'ge':
        return line - 1e-6 if value >= line else line
    return line + 1e-6 if value <= line else line


def near_line(r, bound, margin=NEAR):
    """The deciding ratios within the margin of 0.90 or 1.00, in hot or append: each one whose crossing to
    its line's other side, alone or together with other ratios within the margin, would change the row or
    its qualifier (it is needed in some such set of crossings), whatever the other workload shows. So two
    workloads both just above 1.00 are flagged, though neither alone decides Out, and so are R_rel and R_adj
    against B_rel just above 0.90 in both, which together decide the qualifier."""
    base = verdict(r, bound)
    near = [(p, key, value, kind, line) for p in LOCAL for key, lines in THRESHOLDS.items()
            for kind, line in lines if (value := (r.get(p) or {}).get(key)) is not None
            and abs(value - line) <= margin + 1e-9]

    def changes(crossed):
        moved = {q: dict(v or {}) for q, v in r.items()}
        for p, key, value, kind, line in crossed:
            moved[p][key] = flip(value, kind, line)
        return verdict(moved, bound) != base

    needed = set()
    for size in range(1, len(near) + 1):
        for crossed in itertools.combinations(range(len(near)), size):
            if changes([near[i] for i in crossed]):
                needed.update(i for i in crossed if not changes([near[j] for j in crossed if j != i]))
    return [(p, key, value, line) for i, (p, key, value, kind, line) in enumerate(near) if i in needed]


def fragile(ratios, bound, fast='B_fast'):
    """The robustness views (R_max, R_noSSH, R_paired) that would put the verdict in another row."""
    row = verdict(deciding(ratios, 'median', fast), bound)['row']
    out = []
    for view, name in (('max', 'R_max'), ('no_ssh', 'R_noSSH'), ('paired', 'R_paired')):
        other = verdict(deciding(ratios, view, fast), bound)['row']
        if other != row:
            out.append((name, other))
    return out


def ha_lines(ratios, bound, label, fast='B_fast'):
    """The rows in order with what each reads, the verdict, its qualifier and the flags."""
    r = deciding(ratios, 'median', fast)
    v = verdict(r, bound)
    fast_name = RATIO_NAME[fast]
    lines = [f'| Row | Condition | {" | ".join(LOCAL)} | Applies |', '|---|---|' + '---:|' * len(LOCAL) + '---|']

    def cells(*keys):
        return ' | '.join(', '.join(f'{k} {fmt((r.get(p) or {}).get(k))}' for k in keys) for p in LOCAL)
    lines.append(f'| Out on fast links | {fast_name} ≥ 1.00 for hot or append | {cells("fast")} | '
                 f'{"yes" if v["row"] == ROWS[0] else "no"} |')
    lines.append(f'| Confirmed | {fast_name} ≤ 0.90 for both, R_adj ≤ 0.90 against {fast} and B_rel, the bound '
                 f'passes A3 ({word(bound.get("passes")) if bound.get("measured") else "not measured"}) | '
                 f'{cells("fast", "adj_fast", "adj_rel")} | {"yes" if v["row"] == ROWS[1] else "no"} |')
    lines.append(f'| Bound missing | bound not measured, excluded, or failing A3 while slower than B_rel; R_rel and '
                 f'R_adj against B_rel ≤ 0.90 for both | {cells("rel", "adj_rel")} | '
                 f'{"yes" if v["row"] == ROWS[2] else "no"} |')
    lines.append(f'| Undecided | anything else | | | {"yes" if v["row"] == ROWS[3] else "no"} |')
    text = f'**Verdict ({label}): {v["row"]}.**'
    if v['row'] == ROWS[0]:
        text += ' ' + ' '.join(f'{p}: "Out against the fastest copy measured; R_rel = {fmt(r[p]["rel"])}".'
                               for p in LOCAL)
        if v['qualifier']:
            text += f' Qualifier: {QUALIFIER}.'
    elif v['row'] == ROWS[2]:
        text += ' ' + BOUND_MISSING
    elif v['row'] == ROWS[3]:
        text += ' Decided after T5 and T13.'
    lines.append(text)
    weak = fragile(ratios, bound, fast)
    lines.append('- *Fragile*: ' + ('; '.join(f'{name} would give {row}' for name, row in weak) if weak
                                    else 'no (R_max, R_noSSH and R_paired give the same row)'))
    near = near_line(r, bound)
    lines.append('- *Near the line*: ' + ('; '.join(f'{p} {k} {fmt(value, 3)} near {line:.2f}' for p, k, value, line
                                                    in near) if near else 'no deciding ratio within ±0.03'))
    return v, lines


# --- the report ----------------------------------------------------------------

def counts_lines(S):
    lines = ['## Runs']
    statuses = Counter(r['status'] for r in S.rows)
    blocks = Counter(r['block'] or 'main' for r in S.rows)
    lines.append(f'- {len(S.rows)} runs; status ' + ', '.join(f'{k} {n}' for k, n in sorted(statuses.items()))
                 + '; block ' + ', '.join(f'{k} {n}' for k, n in sorted(blocks.items())))
    left = Counter(r['left_out'].split(':')[0] for r in S.rows if r['left_out'])
    lines.append('- left out of every median: ' + (', '.join(f'{k} {n}' for k, n in sorted(left.items())) or 'none'))
    for r in S.rows:
        if r['status'] == 'error':
            kind = {'tool': 'the tool', 'harness': 'the harness'}.get(r.get('failure_kind'), 'cause not recorded')
            lines.append(f'  - error ({kind}): {r["trial_id"]} r{r["repeat"]} {r["speed"]} {r["method"]}: '
                         f'{r["error"] or ""}')
    return lines


def durability_lines(S, audit):
    lines = ['## Durability (checks (a) and (b) in every run; A5, A6; the negative control)']
    limits = sorted({r['dirty_limit_bytes'] for r in S.rows if r['dirty_limit_bytes'] is not None})
    noise = [run.get('idle_noise_bytes') for run in S.meta if run.get('idle_noise_bytes') is not None]
    registered = ('' if S.idle_noise is None else
                  f'; judged again here with the smoke run\'s {S.idle_noise / (1 << 20):.2f} MiB (--idle-noise-bytes)')
    lines.append(f'- check (a) limit: {", ".join(f"{v / (1 << 20):.2f} MiB" for v in limits) or "–"} '
                 f'(4 MiB or the idle noise: the sweeps\' '
                 f'{", ".join(f"{v / (1 << 20):.2f} MiB" for v in noise) or "–"}{registered}); '
                 f'check (a) {"VOID for the session (A6)" if S.a_void else "in force"}')
    lines += ['| Arm | runs | (a) pass / fail / unchecked | (b) pass / fail / unchecked | largest increase, MB | '
              'smallest writes ÷ output | fsync count wrong | runs failing | excluded |', '|---|' + '---|' * 8]
    for arm in [a for a in ARM_NAMES if any(r['method'] == a for r in S.rows)]:
        runs = [r for r in S.rows if r['method'] == arm]
        a = Counter(r['a_ok'] for r in runs)
        b = Counter(r['b_ok'] for r in runs) if arm in measure.FULL_COPY_METHODS else None
        writes = [r['disk_write_dest_bytes'] / r['output_bytes'] for r in runs
                  if r['disk_write_dest_bytes'] is not None and r['output_bytes']]
        wrong = sum('fsync count' in r['durability_failures'] for r in runs)
        largest = mx(r['dirty_increase_bytes'] for r in runs)
        lines.append(f'| {arm} | {len(runs)} | {a[True]} / {a[False]} / {a[None]} | '
                     + (f'{b[True]} / {b[False]} / {b[None]}' if b else 'n/a') + ' | '
                     f'{fmt(None if largest is None else largest / 1e6, 1)} | '
                     f'{fmt(min(writes) if writes else None, 3)} | '
                     f'{wrong if arm in measure.PARCOPY_METHODS else "n/a"} | '
                     f'{sum(bool(r["durability_failures"]) for r in runs)} | {S.excluded.get(arm, "")} |')
    control = [r for r in S.rows if r['method'] in measure.NON_DURABLE_METHODS]
    if control:
        increases = [r['dirty_increase_bytes'] for r in control if r['dirty_increase_bytes'] is not None]
        lines.append(f'- negative control: full_nosync fails (a) in {sum(r["a_ok"] is False for r in control)} of '
                     f'{len(control)} runs; smallest increase {fmt(min(increases) / 1e9 if increases else None)} GB '
                     f'(at least 1.5 GB in every run: {word(every(v >= 1.5e9 for v in increases))})')
    failing = [r for r in S.rows if r['durability_failures']]
    for r in failing:
        lines.append(f'  - A5: {r["trial_id"]} r{r["repeat"]} {r["speed"]} {r["method"]} ({r["block"]}): '
                     f'{", ".join(r["durability_failures"])} failed; left out')
    lines.append('\n## Syscall audit (smoke run, scale 5, one untimed run per arm)')
    if not audit:
        lines.append('- not given (--audit): to be filled from durability_audit.py\'s results')
    else:
        lines += ['| Arm | passed | uncovered | left by the tool\'s own flush |', '|---|---|---|---|']
        for arm, result in audit.items():
            passed = result.get('passed', result.get('ok'))
            lines.append(f'| {arm} | {"–" if passed is None else "yes" if passed else "no"} | '
                         f'{audit_cell(result)} | {audit_cell(result, "tool_")} |')
    return lines


def audit_cell(result, prefix=''):
    """What a view of the audit left uncovered: the audit's own counts of files and directory changes,
    with up to five example paths. The paths are examples only (the audit keeps 20 of each kind, and
    the files and directory entries are merged and deduplicated), so their number is never a count; a
    summary without the counts (an older --audit JSON) shows the number of example paths, marked so."""
    paths = result.get(f'{prefix}uncovered') or []
    examples = f' ({", ".join(paths[:5])}{", …" if len(paths) > 5 else ""})' if paths else ''
    files, changes = result.get(f'{prefix}uncovered_files'), result.get(f'{prefix}uncovered_directory_changes')
    if files is None or changes is None:
        return f'{len(paths)} paths, examples only{examples}' if paths else '0'
    return (f'{files} of {result.get(f"{prefix}files")} files, {changes} of '
            f'{result.get(f"{prefix}directory_changes")} directory changes{examples}')


def probe_lines(S):
    lines = ['## Probes and floors (E8; untimed, once per trial)',
             '| Trial | durable write floor, start / end s | cold read floor (4 readers) s, MB/s | one reader of '
             'pgbench_accounts, GB/s | bracket: first pair / median / median after the first, s |',
             '|---|---|---|---|---|']
    for pattern, trial_id, probes in S.trial_probes(S.patterns):
        writes = [w.get('seconds') if isinstance(w, dict) else None for w in probes.get('durable_write_floor') or ()]
        read, single = probes.get('read_floor') or {}, probes.get('single_read') or {}
        bracket = probes.get('bracket') or {}
        rate = single.get('mb_per_second')
        lines.append(f'| {trial_id} | {" / ".join(fmt(w) for w in writes) or "–"} | {fmt(read.get("seconds"))}, '
                     f'{fmt(read.get("mb_per_second"), 0)} | {fmt(None if rate is None else rate / 1000)} '
                     f'| {fmt(bracket.get("first_pair_s"), 3)} / {fmt(bracket.get("median_pair_s"), 3)} / '
                     f'{fmt(bracket.get("median_after_first_s"), 3)} |')
    rates = {s: median(S.link(p, s, 'measured_s2d_gbit_8streams') for p in S.patterns) for s in SPEEDS}
    lines.append('- 8-stream link, source to dest (median over the session): '
                 + ', '.join(f'{SPEED_LABEL[s]} {fmt(v)} Gbit/s' for s, v in rates.items() if v is not None))
    lines.append(f'- floor(U) (median of the durable write floor in hot and append): {fmt(S.floor_u())} s; '
                 f'cold read floor {fmt(S.read_floor())} s; a full copy\'s bytes '
                 f'{fmt(S.copy_bytes() and S.copy_bytes() / 1e9)} GB')
    lines.append('- floor(s) = max(8-stream link time, floor(U), cold read floor): '
                 + ', '.join(f'{SPEED_LABEL[s]} {fmt(S.floor(s))} s' for s in SPEEDS))
    return lines


def choice_lines(S):
    lines = ['## Baselines: the arm each envelope picks per speed (main block, pooled over the workloads; '
             f'ties within {TIE_S} s go to the lower pooled maximum)',
             '| Speed | Envelope | Arm | pooled median (max), runs | tied | every candidate: median (max, n) |',
             '|---|---|---|---|---|---|']
    for matched in (False, True):
        for s in SPEEDS:
            for name in ENVELOPES:
                chosen = S.choice(name, s, matched)
                if chosen is None:
                    continue
                label = name + (' (matched)' if matched else '')
                lines.append(f'| {SPEED_LABEL[s]} | {label} | {chosen["arm"]} | {fmt(chosen["median"])} '
                             f'({fmt(chosen["max"])}), {chosen["n"]} | {", ".join(chosen["tied"])} | '
                             + ', '.join(f'{arm} {fmt(m)} ({fmt(t)}, {n})'
                                         for arm, (m, t, n) in sorted(chosen['stats'].items(), key=lambda kv: kv[1]))
                             + ' |')
    for arm, why in sorted(S.excluded.items()):
        lines.append(f'- left out of every baseline: {arm}: {why}')
    voids = [r for r in S.rows if r['void']]
    if voids:
        lines.append(f'- {len(voids)} void run(s), left out of B_rel and B_fast: '
                     + ', '.join(f'{r["trial_id"]} r{r["repeat"]} {r["speed"]} {r["method"]}' for r in voids))
    return lines


def median_lines(S):
    lines = ['## Medians, seconds: main block, median (max) and runs']
    for p in S.patterns:
        arms = [a for a in ARM_NAMES if any(S.runs(a, p, s) for s in SPEEDS)]
        lines += [f'### {p}', '| Arm | ' + ' | '.join(SPEED_LABEL[s] for s in SPEEDS) + ' |',
                  '|---|' + '---:|' * len(SPEEDS)]
        for arm in arms:
            lines.append(f'| {arm} | ' + ' | '.join(
                f'{fmt(S.med(p, s, arm))} ({fmt(S.top(p, s, arm))}), {len(S.runs(arm, p, s))}'
                if S.runs(arm, p, s) else '' for s in SPEEDS) + ' |')
    lines += ['### Unthrottled outside the warm-source block (the HA verdict\'s runs)',
              '| Arm | ' + ' | '.join(S.patterns) + ' |', '|---|' + '---:|' * len(S.patterns)]
    for arm in ARM_NAMES:
        if any(S.runs(arm, p, U, 'ha') for p in S.patterns):
            lines.append(f'| {arm} | ' + ' | '.join(
                f'{fmt(S.med(p, U, arm, view="ha"))} ({fmt(S.top(p, U, arm, view="ha"))}), '
                f'{len(S.runs(arm, p, U, "ha"))}' if S.runs(arm, p, U, 'ha') else '' for p in S.patterns) + ' |')
    return lines


def ratio_table(title, by_pattern, patterns):
    lines = [title, '| Workload | Envelope | arm (runs) | R | R_adj | R_max | R_noSSH | R_paired (pairs) |',
             '|---|---|---|---:|---:|---:|---:|---:|']
    for p in patterns:
        r = by_pattern.get(p) or {}
        for name in ENVELOPES:
            e = r.get(name)
            if not e:
                continue
            lines.append(f'| {p} | {RATIO_NAME[name]} | {e["arm"]} ({e["n"]}) | {fmt(e["median"])} | '
                         f'{fmt(e["adj"])}{"" if e["live"] else " (= R)"} | {fmt(e["max"])} | {fmt(e["no_ssh"])} | '
                         f'{fmt(e["paired"])} ({e["n_pairs"]}) |')
    return lines


def ratio_lines(S):
    lines = ['## Ratios, unthrottled: v1\'s median over the baseline\'s (outside the warm-source block)']
    standard = S.all_ratios()
    lines.append('- the backup-mode adder (`full`\'s checkpoint wait plus its wait from the WAL end point to its '
                 'sync; median of its unthrottled runs per workload), with the bracket probe beside it: '
                 + '; '.join(f'{p} {fmt(standard[p]["adder"], 3)} s ({standard[p]["n_adder"]} runs)' for p in S.patterns)
                 + '; bracket median ' + ', '.join(
                     f'{trial_id} {fmt((probes.get("bracket") or {}).get("median_pair_s"), 3)} s'
                     for _, trial_id, probes in S.trial_probes(S.patterns)))
    lines += ratio_table('### v1 at 4 jobs (the verdict\'s arm)', standard, S.patterns)
    lines += ratio_table('### Matched parallelism: v1 at 4 jobs against arms of at most 4 streams (not the verdict)',
                         S.all_ratios(matched=True), S.patterns)
    if any(S.runs(V1_J8)):
        lines += ratio_table('### v1 at 8 jobs (not the verdict)', S.all_ratios(V1_J8), LOCAL)
    lines += ['### Warm source (not the verdict): v1 at 4 jobs over `full` and over `probackup8`, warm-source runs '
              'beside the same cold', '| Workload | over | warm (runs) | cold (runs) |', '|---|---|---:|---:|']
    for p in S.patterns:
        for arm in ('full', 'probackup8'):
            warm = ratio(S.med(p, U, V1, view='warm'), S.med(p, U, arm, view='warm'))
            cold = ratio(S.med(p, U, V1, view='ha'), S.med(p, U, arm, view='ha'))
            if warm is None and cold is None:
                continue
            lines.append(f'| {p} | {arm} | {fmt(warm)} ({len(S.runs(arm, p, U, "warm"))}) | {fmt(cold)} '
                         f'({len(S.runs(arm, p, U, "ha"))}) |')
    if any(r['void'] for r in S.rows):
        lines += ratio_table('### With void runs included (flagged)', S.all_ratios(void=True), S.patterns)
    return lines


def bound_state(bound):
    """The bound and its gate, in words."""
    if not bound['measured']:
        return 'not measured: ' + ('not built' if not bound['built'] else
                                   'excluded: ' + ', '.join(bound['excluded']) if bound['excluded'] else
                                   'no counting unthrottled run in hot or append')
    return (f'{bound["arm"]} {fmt(bound["median"])} s over {bound["n"]} runs against floor(U) {fmt(bound["floor"])} + '
            f'{GATE_S} s; busiest stream {fmt(bound["balance"], 3)} x the mean → A3 {word(bound["passes"])}'
            + ('; it is B_fast' if bound['is_fast'] else '; a released arm is faster'))


def verdict_lines(S):
    lines = ['## EXP-09\'s HA rule, re-judged (unthrottled, 10 runs per extended arm, v1 at 4 jobs)']
    bound = S.bound()
    lines.append(f'- the bound (A3): {bound_state(bound)}')
    undecided = {arm: runs for arm, runs in S.a4_undecided.items() if arm not in S.a4}
    if undecided:
        lines.append('- **A4 is undecided** for ' + ', '.join(f'{arm} ({len(runs)} errored run(s) whose cause isn\'t '
                                                             'recorded)' for arm, runs in undecided.items())
                     + ': each stays in the baselines, and the verdict below holds only if no such run was a copy '
                       'the tool itself aborted')
    ratios = S.all_ratios()
    v4, rows = ha_lines(ratios, bound, 'v1 at 4 jobs')
    lines += rows
    voids = sorted({r['trial_id'] for r in S.rows if r['void']})
    lines.append('- *With void runs*: ' + (f'trials {", ".join(voids)} have void pg_probackup runs; see below'
                                          if voids else 'no void runs'))
    if v4['row'] != ROWS[1]:
        lines += cut_lines(ratios)
    if any(S.runs(V1_J8)):
        lines.append('\n### Beside it: v1 at 8 jobs (not the verdict)')
        v8, rows = ha_lines(S.all_ratios(V1_J8), bound, 'v1 at 8 jobs')
        lines += rows
        lines.append('- ' + ('the same row' if v8 == v4 else
                             f'**the two differ**: {v4["row"]} at 4 jobs, {v8["row"]} at 8 jobs; both stated'))
    if voids:
        lines.append('\n### With void runs included (flagged)')
        lines += ha_lines(S.all_ratios(void=True), S.bound(void=True), 'with void runs, flagged')[1]
    return lines


def cut_lines(ratios):
    """What a verdict short of Confirmed asks of T5 and T13: v1 − 0.9 × B_fast in hot and append."""
    cuts, targets = {}, {}
    for p in LOCAL:
        r = ratios.get(p) or {}
        if r.get('B_fast') and r['B_fast']['base'] is not None and r['v1'] is not None:
            targets[p] = 0.9 * r['B_fast']['base']
            cuts[p] = r['v1'] - targets[p]
    if not cuts:
        return ['- the cut for T5 and T13: no data']
    rel = {p: ratios[p]['v1'] - 0.9 * ratios[p]['B_rel']['base'] for p in cuts
           if ratios[p].get('B_rel') and ratios[p]['B_rel']['base'] is not None}
    worst, target = max(cuts.values()), min(targets.values())
    if worst > 1.2 or target < 1.25:
        ask = ("more than 1.2 s, or a target under 1.25 s: v1's design can't reach the line on these VMs even with "
               'both (reading B cold once takes about 0.95 s plus about 0.3 s of start-up); the ledger says so')
    elif worst > 0.6:
        ask = '0.6–1.2 s: T13 and T5, ranked equal'
    else:
        ask = 'up to 0.6 s: T13 first'
    return [f'- the cut for T5 and T13 (v1 − 0.9 × B_fast): ' + ', '.join(f'{p} {fmt(c)} s (target {fmt(targets[p])} s)'
                                                                        for p, c in cuts.items())
            + '; against B_rel ' + ', '.join(f'{p} {fmt(c)} s' for p, c in rel.items()) + f' → {ask}']


def crossover_lines(S, table, bound):
    lines = ['## Crossovers: v1 at 4 jobs (EXP-04b\'s method; x is this session\'s median single-stream rate)',
             '| Workload | against | crossover | provisional |', '|---|---|---|---|']
    found = {}
    for p in S.patterns:
        for base in (*ENVELOPES, 'link_model'):
            value, how = exp04.crossover(table, p, V1, baseline=base)
            found[(p, base)] = (value, how)
            if how == 'no data':
                continue
            speeds = bracketing(table, p, V1, base)
            notes = S.provisional(base, speeds) if base in ENVELOPES else []
            text = describe(value, how)
            if base in ('B_fast', 'B_tuned') and bound.get('passes') is False and any(
                    table[(p, s)].get(base, {}).get('arm') in measure.PARCOPY_METHODS for s in speeds):
                text += ' (upper bound: the bound fails A3; a faster copy would cross lower)'
            label = 'link time of a full copy\'s bytes (a model)' if base == 'link_model' else base
            lines.append(f'| {p} | {label} | {text} | {"; ".join(notes) or "no"} |')
    return lines, found


def drift_gates(S, exp09):
    """A7: this session against EXP-09 at EXP-09's speeds, per workload: `full` within ±5% at 2 and 5
    Gbit/s and ±10% unthrottled; v1 at 4 jobs within ±10%; the measured link within ±2% at 2 and 5
    Gbit/s and ±5% unthrottled; wire bytes within ±1%."""
    gates = (('full total_s', 'full', 'total_s', {'2g': .05, '5g': .05, U: .10}),
             ('v1 total_s', V1, 'total_s', {'2g': .10, '5g': .10, U: .10}),
             ('measured link', None, 'measured_s2d_gbit', {'2g': .02, '5g': .02, U: .05}),
             ('full wire bytes', 'full', 'wire_s2d_bytes', {'2g': .01, '5g': .01, U: .01}),
             ('v1 wire bytes', V1, 'wire_s2d_bytes', {'2g': .01, '5g': .01, U: .01}))
    out = []
    for p in S.patterns:
        for s in EXP09_SPEEDS:
            for name, method, col, tolerance in gates:
                if method is None:
                    here, there = S.link(p, s), exp09['link'].get((p, s))
                else:
                    here = S.med(p, s, method, col)
                    there = (exp09['cells'].get((p, s, method)) or {}).get(col)
                change = None if here is None or not there else here / there - 1
                out.append({'pattern': p, 'speed': s, 'gate': name, 'here': here, 'exp09': there, 'change': change,
                            'tolerance': tolerance[s],
                            'ok': None if change is None else abs(change) <= tolerance[s] + 1e-12})
    return out


def drift_lines(gates):
    lines = ['## Drift gates against EXP-09 (A7; main-block medians)',
             '| Workload | Speed | gate | here | EXP-09 | change | tolerance | |', '|---|---|---|---:|---:|---:|---:|---|']
    for g in gates:
        scale, digits = (1e6, 1) if g['gate'].endswith('bytes') else (1, 3)  # bytes in MB
        here = None if g['here'] is None else g['here'] / scale
        there = None if g['exp09'] is None else g['exp09'] / scale
        change = '–' if g['change'] is None else format(g['change'], '+.1%')
        lines.append(f'| {g["pattern"]} | {SPEED_LABEL[g["speed"]]} | {g["gate"]}{" (MB)" if scale > 1 else ""} | '
                     f'{fmt(here, digits)} | {fmt(there, digits)} | {change} | ±{g["tolerance"]:.0%} | {word(g["ok"])} |')
    return lines


def code_lines(S):
    """Beside the drift gates: pgdelta/'s changes since EXP-09's deployed commit, as each sweep's deploy recorded
    them (remote.py's DEPLOYED.json pgdelta_since_exp09, kept in run.json's environment), since v1 is to run
    EXP-09's engine (Setup)."""
    lines = []
    for run in S.meta:
        name = run.get('name') or 'a sweep'
        found = {host: ((((run.get('environment') or {}).get(host) or {}).get('deployed') or {})
                        .get('pgdelta_since_exp09')) for host in ('dest', 'source')}
        since = found['dest'] or found['source']
        if not since:
            lines.append(f'- pgdelta/ since EXP-09\'s deployed commit ({name}): not recorded (deployed without it)')
        elif since.get('error'):
            lines.append(f'- pgdelta/ since EXP-09\'s deployed commit {since["base"][:7]} ({name}): '
                         f'git diff failed: {since["error"]}')
        else:
            files = since.get('files') or []
            changed = ', '.join(f'{f["path"]} +{"?" if f["added"] is None else f["added"]} '
                                f'−{"?" if f["removed"] is None else f["removed"]}' for f in files)
            lines.append(f'- pgdelta/ since EXP-09\'s deployed commit {since["base"][:7]} ({name}): '
                         + (f'{len(files)} file(s) changed: {changed}; diff SHA-256 {since["diff_sha256"][:12]}'
                            if files else 'unchanged'))
    return lines


def shifted(gates):
    """A7: the session is shifted if any gate fails, or none could be checked."""
    oks = [g['ok'] for g in gates if g['ok'] is not None]
    return not oks or not all(oks)


def phase_table_lines(S, required):
    lines = ['## Where `full`\'s time goes (its --verbose timers; main-block medians, seconds)'
             + (' — required: L1 failed or `--no-sync` saved under 0.5 s' if required else ''),
             '| Workload | Speed | arm | total | start | checkpoint | stream | WAL end | sync | manifest | above link |',
             '|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for p in S.patterns:
        for s in ('2g', U):
            for arm in ('full', 'full_nosync'):
                if not S.runs(arm, p, s):
                    continue
                lines.append(f'| {p} | {SPEED_LABEL[s]} | {arm} | ' + ' | '.join(
                    fmt(S.med(p, s, arm, col)) for col in ('total_s', 'pbb_start_s', 'pbb_checkpoint_s', 'pbb_stream_s',
                                                          'pbb_wal_end_s', 'pbb_sync_s', 'pbb_manifest_s',
                                                          'above_link_s')) + ' |')
    return lines


def above_link_lines(S):
    lines = ['## Time above link time (total_s minus the wire bytes at the measured rate: 8 streams for the '
             'parallel arms; main-block medians, seconds)', '| Workload | Speed | ' + ' | '.join(ARM_NAMES) + ' |',
             '|---|---|' + '---:|' * len(ARM_NAMES)]
    for p in S.patterns:
        for s in SPEEDS:
            values = [S.med(p, s, arm, 'above_link_s') for arm in ARM_NAMES]
            if any(v is not None for v in values):
                lines.append(f'| {p} | {SPEED_LABEL[s]} | ' + ' | '.join(fmt(v) for v in values) + ' |')
    return lines


def order_lines(S):
    """The notes' order effects: live-source copies after a source restart against the others, and the
    extension's runs against the main block's; differences over 0.1 s in the median."""
    before = S.predecessors()
    live = (*measure.FULL_VARIANTS, *measure.PROBACKUP_METHODS)
    lines = ['## Order effects (differences over 0.1 s in the median)']
    found = 0
    for p in S.patterns:
        for s in SPEEDS:
            for arm in live:
                runs = S.runs(arm, p, s)
                after = [r['total_s'] for r in runs
                         if (before.get(id(r)) or {}).get('method') in measure.SOURCE_STOP_METHODS]
                other = [r['total_s'] for r in runs
                         if (before.get(id(r)) or {}).get('method') not in measure.SOURCE_STOP_METHODS]
                if after and other and abs(median(after) - median(other)) > 0.1:
                    found += 1
                    lines.append(f'- {p} {SPEED_LABEL[s]} {arm}: after a source restart {fmt(median(after))} s '
                                 f'({len(after)}), otherwise {fmt(median(other))} s ({len(other)})')
        for arm in ARM_NAMES:
            ext = [r['total_s'] for r in S.runs(arm, p, U, 'ha') if r['block'] == 'extension']
            main = [r['total_s'] for r in S.runs(arm, p, U)]
            if ext and main and abs(median(ext) - median(main)) > 0.1:
                found += 1
                lines.append(f'- {p} {arm}: extension {fmt(median(ext))} s ({len(ext)}) against the main block '
                             f'{fmt(median(main))} s ({len(main)})')
    if not found:
        lines.append('- none')
    return lines


def ledger_lines(S, table, gates):
    """L1-L4 and the ledger's rules."""
    lines = ['## The ledger\'s predictions and rules (registered; main-block medians)']
    say = lines.append
    pats = S.patterns
    savings = {(p, s): (S.med(p, s, 'full') - S.med(p, s, 'full_nosync')
                        if S.med(p, s, 'full') is not None and S.med(p, s, 'full_nosync') is not None else None)
               for p in pats for s in ('2g', U)}
    above = {(p, s): S.med(p, s, 'full', 'above_link_s') for p in pats for s in ('2g', U)}
    l1 = {p: None if savings[(p, '2g')] is None or above[(p, '2g')] is None
          else savings[(p, '2g')] > above[(p, '2g')] / 2 for p in pats}
    say(f'- **L1** (`--no-sync` removes most of the time above link: at 2 Gbit/s full − full_nosync > half of full\'s '
        f'time above link, every workload): {word(every(l1.values()))} ('
        + ', '.join(f'{p}: saves {fmt(savings[(p, "2g")])} of {fmt(above[(p, "2g")])} s' for p in pats)
        + '); unthrottled, reported: '
        + ', '.join(f'{p} saves {fmt(savings[(p, U)])} of {fmt(above[(p, U)])} s' for p in pats))
    fires = {s: [p for p in pats if savings[(p, s)] is not None and savings[(p, s)] < 0.5] for s in ('2g', U)}
    say('- **Rule** (`--no-sync` saves under 0.5 s: the overhead lies elsewhere): '
        + '; '.join(f'{SPEED_LABEL[s]} {"fires in " + ", ".join(f) if f else "does not fire"}'
                    for s, f in fires.items()))
    required = every(l1.values()) is False or any(fires.values())
    if required:
        say('  - the phase timers below must say where the time goes before any parallel gain is credited to '
            'overlapping the flush')
    bound = S.bound()
    parallel = {}
    for part, arms in (('(i)', measure.PROBACKUP_METHODS),
                       ('(ii)', (*measure.PROBACKUP_METHODS, *measure.PARCOPY_METHODS))):
        parallel[part] = {p: min((v for v in (S.med(p, U, arm) for arm in arms if arm not in S.excluded)
                                  if v is not None), default=None) for p in pats}
    for part, values in parallel.items():
        ok = every(within(v, 2.0, 3.0) for v in values.values())
        if part == '(ii)' and not bound['measured']:
            ok_text = 'not judged (the bound is not measured)'
        else:
            ok_text = word(ok)
        say(f'- **L2 {part}** (a durable parallel copy takes 2.0–3.0 s unthrottled, the faster '
            f'{"released" if part == "(i)" else "parallel"} arm, every workload): {ok_text} ('
            + ', '.join(f'{p} {fmt(v)}' for p, v in values.items()) + ')')
    for part, values in parallel.items():
        at = [p for p, v in values.items() if v is not None and v >= 3.5]
        if part == '(ii)' and not bound['measured']:
            say('- **Rule (ii)** (a parallel copy takes 3.5 s or more): not judged (the bound is not measured)')
            continue
        if part == '(ii)' and bound['passes'] is not True:
            say('- **Rule (ii)** (a parallel copy takes 3.5 s or more): not judged: parcopy has not passed its gate '
                '(A3), so it is under-built; it stays in B_fast as A3 says, and a fixed version waits for a later '
                'session')
            continue
        say(f'- **Rule {part}** (a parallel copy takes 3.5 s or more: the single stream was representative): '
            + (f'fires in {", ".join(at)}' if at else 'does not fire'))
    if any(v is not None and v >= 3.5 for v in parallel['(i)'].values()) and not any(
            v is not None and v >= 3.5 for v in parallel['(ii)'].values()):
        say('  - D03: "the single stream represents the released parallel tools at this data shape, not a '
            'range-parallel copier"')
    for p, v in parallel['(i)'].items():
        if v is not None and 3.0 < v < 3.5:
            say(f'  - D03 ({p}): "the released parallel copy tested here took {v:.2f} s against one stream\'s '
                f'{fmt(S.med(p, U, "full"))} s at 2.1 GB on these VMs; per-file parallelism gains little at this '
                'data shape"')
    drift = not shifted(gates)
    v0 = {}
    for p in pats:
        v0[p] = (exp04.crossover(table, p, 'v0', baseline='B_fast'), exp04.crossover(table, p, 'v0', baseline='F0'))
    l3 = {p: l3_holds(*v0[p]) for p in pats}
    say(f'- **L3** (v0\'s crossovers fall by at most about 20%: against B_fast at least 0.8 × against F0, EXP-09\'s '
        f'speeds only, every workload): '
        + (f'{word(every(l3.values()))}' if drift else 'not judged (A7: the session is shifted)') + ' ('
        + ', '.join(f'{p}: {describe(*v0[p][0])} against {describe(*v0[p][1])}' for p in pats) + ')')
    l4 = {p: exp04.crossover(table, p, 'v0_transfer', baseline='B_fast') for p in pats}
    oks = {p: inside(found, 3.0, 4.5) for p, found in l4.items()}
    say(f'- **L4** (transfer-only crossovers 3–4.5 Gbit/s: EXP-09\'s v0 transfer_s against B_fast, EXP-09\'s speeds): '
        + (', '.join(f'{p} {word(ok)}' for p, ok in oks.items()) if drift else 'not judged (A7: the session is shifted)')
        + ' (' + ', '.join(f'{p} {describe(*found)}' for p, found in l4.items()) + '); beside it, the measured v1 '
        'path (4 jobs) against B_fast: '
        + ', '.join(f'{p} {describe(*exp04.crossover(table, p, V1, baseline="B_fast"))}' for p in pats))
    say('- **L5** and the rule "zstd gives 2 times or more": not judged here (moved to T4, change 3)')
    return lines, required


def e_lines(S, crossings):
    """E1-E15, as registered (E12 removed)."""
    lines = ['## Added predictions (main-block medians unless stated)']
    say = lines.append
    pats = S.patterns
    full = {'2g': (9.76, 10.79), '5g': (4.78, 5.28), '10g': (3.1, 3.6), U: (2.82, 3.46)}
    cells = [(p, s, S.med(p, s, 'full')) for p in pats for s in SPEEDS]
    say(f'- **E1** (`full` reproduces EXP-09: 9.76–10.79 s at 2, 4.78–5.28 at 5, 2.82–3.46 unthrottled; 3.1–3.6 at 10): '
        f'{word(every(within(v, *full[s]) for p, s, v in cells))} ({listing(cells)})')
    v1 = {'local': {s: (2.43, 2.98) for s in SPEEDS},
          'uniform': {'2g': (4.44, 5.43), '5g': (3.09, 3.77), '10g': (2.9, 3.6), U: (2.85, 3.48)}}
    cells = [(p, s, S.med(p, s, V1)) for p in pats for s in SPEEDS]
    say(f'- **E2** (v1 at 4 jobs within 10% of EXP-09: hot and append 2.43–2.98 s; uniform 4.44–5.43, 3.09–3.77, '
        f'2.85–3.48 at 2, 5, unthrottled; 2.9–3.6 at 10): '
        f'{word(every(within(v, *v1["uniform" if p == "uniform" else "local"][s]) for p, s, v in cells))} '
        f'({listing(cells)})')
    sync = [(p, s, S.med(p, s, 'full', 'pbb_sync_s')) for p in pats for s in SPEEDS]
    saves = [(p, s, S.med(p, s, 'full') - S.med(p, s, 'full_nosync')) for p in pats for s in ('2g', U)
             if S.med(p, s, 'full') is not None and S.med(p, s, 'full_nosync') is not None]
    usable = [(p, s, S.med(p, s, 'full_nosync', 'usable_s') - S.med(p, s, 'full', 'usable_s')) for p in pats
              for s in ('2g', U) if S.med(p, s, 'full_nosync', 'usable_s') is not None
              and S.med(p, s, 'full', 'usable_s') is not None]
    say(f'- **E3** `full`\'s sync phase 1.0–1.6 s at every speed: {word(every(within(v, 1.0, 1.6) for *_, v in sync))} '
        f'({listing(sync)}); `full_nosync` saves 1.1–1.5 s at 2 and unthrottled: '
        f'{word(every(within(v, 1.1, 1.5) for *_, v in saves))} ({listing(saves)}); its time until usable within '
        f'0.3 s of `full`\'s: {word(every(abs(v) <= 0.3 for *_, v in usable))} (nosync − full: {listing(usable)})')
    diffs = [(p, s, S.med(p, s, 'full_syncfs') - S.med(p, s, 'full')) for p in pats for s in SPEEDS
             if S.med(p, s, 'full_syncfs') is not None and S.med(p, s, 'full') is not None]
    say(f'- **E4** (`full_syncfs` within ±0.2 s of `full`): {word(every(abs(v) <= 0.2 for *_, v in diffs))} '
        f'(syncfs − full: {listing(diffs)})')
    wb = [(p, s, S.med(p, s, 'full_wb')) for p in pats for s in ('10g', U)]
    gain = [(p, s, S.med(p, s, 'full') - v) for p, s, v in wb if v is not None and S.med(p, s, 'full') is not None]
    ranges = {'10g': (2.2, 2.8), U: (1.9, 2.5)}
    say(f'- **E5** (`full_wb` 0.7–1.2 s faster than `full`; 1.9–2.5 s unthrottled, 2.2–2.8 at 10): faster by '
        f'{word(every(within(v, 0.7, 1.2) for *_, v in gain))} ({listing(gain)}); times '
        f'{word(every(within(v, *ranges[s]) for p, s, v in wb))} ({listing(wb)})')
    parcopy = [(p, s, min((v for v in (S.med(p, s, a) for a in measure.PARCOPY_METHODS) if v is not None),
                          default=None)) for p in pats for s in SPEEDS]
    ranges = {'2g': (9.1, 9.5), '5g': (3.8, 4.2), '10g': (2.1, 2.6), U: (1.6, 2.3)}
    pair = [(p, U, S.med(p, U, 'parcopy8') - S.med(p, U, 'parcopy4')) for p in pats
            if S.med(p, U, 'parcopy8') is not None and S.med(p, U, 'parcopy4') is not None]
    bound = S.bound()
    above = None if not bound['measured'] or bound['floor'] is None else bound['median'] - bound['floor']
    say(f'- **E6** (the faster parcopy arm: 1.6–2.3 s unthrottled, 2.1–2.6 at 10, 3.8–4.2 at 5, 9.1–9.5 at 2): '
        f'{word(every(within(v, *ranges[s]) for p, s, v in parcopy if v is not None))} ({listing(parcopy)}); '
        f'parcopy8 within ±0.15 s of parcopy4: {word(every(abs(v) <= 0.15 for *_, v in pair))} ({listing(pair)}); '
        f'at most 1.0 s above floor(U) (A3): {word(None if above is None else above <= GATE_S)} '
        f'({fmt(above)} s above)')
    pb = [(p, s, S.med(p, s, 'probackup8')) for p in pats for s in ('2g', U)]
    ranges = {'2g': (9.6, 10.6), U: (2.6, 3.8)}
    pair = [(p, U, S.med(p, U, 'probackup4') - S.med(p, U, 'probackup8')) for p in pats
            if S.med(p, U, 'probackup4') is not None and S.med(p, U, 'probackup8') is not None]
    sync = [(p, s, S.med(p, s, arm, 'probackup_sync_s')) for p in pats for s in SPEEDS for arm in
            measure.PROBACKUP_METHODS if S.med(p, s, arm, 'probackup_sync_s') is not None]
    say(f'- **E7** (`probackup8` 2.6–3.8 s unthrottled, 9.6–10.6 at 2): '
        f'{word(every(within(v, *ranges[s]) for p, s, v in pb))} ({listing(pb)}); `probackup4` within 0.2 s of it: '
        f'{word(every(abs(v) <= 0.2 for *_, v in pair))} ({listing(pair)}); `probackup_sync_s` under 0.2 s: '
        f'{word(every(v < 0.2 for *_, v in sync))} (largest median {fmt(mx(v for *_, v in sync), 3)} s)')
    probes = S.trial_probes(pats)
    writes = [w.get('seconds') for _, _, pr in probes for w in pr.get('durable_write_floor') or () if isinstance(w, dict)]
    reads = [(pr.get('read_floor') or {}).get('seconds') for _, _, pr in probes]
    single = [(pr.get('single_read') or {}).get('mb_per_second') for _, _, pr in probes]
    single = [v / 1000 for v in single if v is not None]
    links = [S.link(p, U, 'measured_s2d_gbit_8streams') for p in pats]
    adder = [(p, s, S.med(p, s, 'full', 'backup_mode_adder_s')) for p in pats for s in SPEEDS]
    brackets = [pr.get('bracket') or {} for _, _, pr in probes]
    first_slower = [None if b.get('first_pair_s') is None or b.get('median_after_first_s') is None
                    else b['first_pair_s'] > b['median_after_first_s'] for b in brackets]
    say(f'- **E8** probes: durable write floor 1.25–1.6 s {word(every(within(v, 1.25, 1.6) for v in writes))} '
        f'({", ".join(fmt(v) for v in writes if v is not None) or "–"}); cold read 0.65–0.9 s '
        f'{word(every(within(v, 0.65, 0.9) for v in reads))} ({", ".join(fmt(v) for v in reads if v is not None) or "–"}); '
        f'one reader 1.2–2.3 GB/s {word(every(within(v, 1.2, 2.3) for v in single))} '
        f'({", ".join(fmt(v) for v in single) or "–"}); 8-stream link 14.5–16 Gbit/s unthrottled '
        f'{word(every(within(v, 14.5, 16) for v in links))} ({", ".join(fmt(v) for v in links if v is not None) or "–"}); '
        f'`full`\'s adder 0.05–0.30 s {word(every(within(v, 0.05, 0.30) for *_, v in adder))} ({listing(adder, 3)}); '
        f'the bracket\'s median 0.03–0.20 s {word(every(within(b.get("median_pair_s"), 0.03, 0.20) for b in brackets))}, '
        f'its first pair slower {word(every(first_slower))}')
    standard, j8 = S.all_ratios(), S.all_ratios(V1_J8)
    fast = [(p, U, ((standard[p].get('B_fast') or {}).get('median'))) for p in LOCAL if p in standard]
    rel = [(p, U, ((standard[p].get('B_rel') or {}).get('median'))) for p in LOCAL if p in standard]
    f0 = [(p, U, ((standard[p].get('F0') or {}).get('median'))) for p in LOCAL if p in standard]
    tuned = [(p, U, ((standard[p].get('B_tuned') or {}).get('median'))) for p in LOCAL if p in standard]
    wb = [(p, U, ratio(S.med(p, U, V1, view='ha'), S.med(p, U, 'full_wb', view='ha'))) for p in LOCAL]
    fall = [(p, name, ratio((j8[p].get(name) or {}).get('median'), (standard[p].get(name) or {}).get('median')))
            for p in LOCAL if p in j8 for name in ENVELOPES]
    v = verdict(deciding(standard), bound)['row']
    say(f'- **E9** (hot and append, v1 at 4 jobs, outside the warm-source block): R_fast 1.15–1.65 '
        f'{word(every(within(x, 1.15, 1.65) for *_, x in fast))} ({listing(fast)}); R_rel 0.85–1.05 '
        f'{word(every(within(x, 0.85, 1.05) for *_, x in rel))} ({listing(rel)}); R_F0 0.78–0.95 '
        f'{word(every(within(x, 0.78, 0.95) for *_, x in f0))} ({listing(f0)}); R_tuned at least R_fast '
        f'{word(every(None if t is None or f is None else t >= f - 1e-12 for (_, _, t), (_, _, f) in zip(tuned, fast)))}; '
        f'`full_wb` alone 1.1–1.4 {word(every(within(x, 1.1, 1.4) for *_, x in wb))} ({listing(wb)}); at 8 jobs each '
        f'ratio falls by up to 15% {word(every(within(x, 0.85, 1.0) for *_, x in fall))} '
        f'({", ".join(f"{p} {RATIO_NAME[n]} x{fmt(x)}" for p, n, x in fall if x is not None) or "no data"}); '
        f'the expected verdict, Out on fast links: {word(v == ROWS[0] if v != "Not judged" else None)} ({v})')
    def cross(p, base):
        return crossings.get((p, base), (None, 'no data'))
    local = [inside(cross(p, 'B_fast'), 6, 9.5) for p in LOCAL if p in pats]
    uni = [inside(cross('uniform', 'B_fast'), 4.5, 7.5)] if 'uniform' in pats else []
    rel_local = [inside(cross(p, 'B_rel'), 10, math.inf) for p in LOCAL if p in pats]
    rel_uni = [inside(cross('uniform', 'B_rel'), 7, math.inf)] if 'uniform' in pats else []
    f0_local = [None if cross(p, 'F0')[1] == 'no data' else cross(p, 'F0')[1].startswith('faster') for p in LOCAL
                if p in pats]
    tie = (standard.get('uniform') or {}).get('F0') or {}
    say(f'- **E10** (v1\'s crossovers, 4 jobs): against B_fast 6–9.5 Gbit/s local {word(every(local))}, 4.5–7.5 '
        f'uniform {word(every(uni))}; against B_rel above 10 or none local {word(every(rel_local))}, 7 or more or none '
        f'uniform {word(every(rel_uni))}; against F0 none local {word(every(f0_local))}, uniform ties unthrottled '
        f'(R_F0 0.95–1.05) {word(within(tie.get("median"), 0.95, 1.05))} ({fmt(tie.get("median"))}); the link-only '
        f'bound, local (about 6.3 Gbit/s), reported: '
        + ', '.join(f'{p} {describe(*cross(p, "link_model"))}' for p in LOCAL if p in pats) + ' (see Crossovers)')
    groups = (('full arms', measure.FULL_VARIANTS, (1.6, 2.0), (1.7, 2.1)),
              ('parcopy', measure.PARCOPY_METHODS, (0.8, 2.5), (2, 5)),
              ('probackup', measure.PROBACKUP_METHODS, (3, 8), (3, 8)),
              ('v1 at 4 jobs', (V1,), (5.3, 6.0), (9, 11)), ('v1 at 8 jobs', (V1_J8,), None, (10, 15)))
    parts = []
    for name, arms, source, dest in groups:
        src = [S.med(p, s, a, 'cpu_source_s') for p in pats for s in SPEEDS for a in arms]
        dst = [S.med(p, s, a, 'cpu_dest_s') for p in pats for s in SPEEDS for a in arms]
        src, dst = [v for v in src if v is not None], [v for v in dst if v is not None]
        if not src and not dst:
            continue
        if source is None:  # v1 at 8 jobs: more on both than at 4 jobs, 10-15 on dest
            pairs = [(S.med(p, U, V1_J8, c), S.med(p, U, V1, c)) for p in LOCAL for c in ('cpu_source_s', 'cpu_dest_s')]
            more = every(None if eight is None or four is None else eight > four for eight, four in pairs)
            ok = every([more, every(within(v, *dest) for v in dst)])
        else:
            ok = every([every(within(v, *source) for v in src), every(within(v, *dest) for v in dst)])
        parts.append(f'{name} {word(ok)} (source {fmt(min(src, default=None), 1)}–{fmt(max(src, default=None), 1)}, '
                     f'dest {fmt(min(dst, default=None), 1)}–{fmt(max(dst, default=None), 1)})')
    say('- **E11** (VM-wide CPU-s per run, source / dest: full arms 1.6–2.0 / 1.7–2.1; parcopy 0.8–2.5 / 2–5; '
        'probackup 3–8 / 3–8; v1 4 jobs 5.3–6.0 / 9–11; v1 8 jobs more on both, 10–15 on dest): ' + '; '.join(parts))
    say('- **E12**: removed (TPROC-C\'s zstd timing moved to T4, change 3)')
    durable = [r for r in S.rows if r['method'] not in measure.NON_DURABLE_METHODS and r['status'] in ('pass', 'fail')]
    control = [r['dirty_increase_bytes'] for r in S.rows if r['method'] in measure.NON_DURABLE_METHODS]
    say(f'- **E13** (every durable arm passes (a) and (b) in every run): '
        f'{word(every(not r["durability_failures"] for r in durable))} '
        f'({sum(bool(r["durability_failures"]) for r in durable)} of {len(durable)} runs fail); `full_nosync`\'s increase '
        f'at least 1.5 GB in every run: {word(every(None if v is None else v >= 1.5e9 for v in control))}; the audit: '
        + audit_word(S.audit))
    j8_times = [(p, U, S.med(p, U, V1_J8)) for p in LOCAL]
    j8_gain = [(p, U, S.med(p, U, V1_J8) - S.med(p, U, V1)) for p in LOCAL
               if S.med(p, U, V1_J8) is not None and S.med(p, U, V1) is not None]
    say(f'- **E14** (v1 at 8 jobs 2.3–2.8 s unthrottled for hot and append, 0.4 s faster to 0.1 s slower than at 4): '
        f'{word(every(within(v, 2.3, 2.8) for *_, v in j8_times))} ({listing(j8_times)}); '
        f'{word(every(within(v, -0.4, 0.1) for *_, v in j8_gain))} (8 − 4 jobs: {listing(j8_gain)})')
    gains, deltas = [], []
    for p in pats:
        for arm, (lo, hi) in (('full', (0, 0.5)), ('probackup8', (0, 0.5)), (V1, (0, 0.15))):
            warm, cold = S.med(p, U, arm, view='warm'), S.med(p, U, arm)
            if warm is not None and cold is not None:
                gains.append((f'{p} {arm}', cold - warm, within(cold - warm, lo, hi)))
        for arm, hi in (('full', 0.17), ('probackup8', 0.25)):
            warm = ratio(S.med(p, U, V1, view='warm'), S.med(p, U, arm, view='warm'))
            cold = ratio(S.med(p, U, V1), S.med(p, U, arm))
            if warm is not None and cold is not None:
                deltas.append((f'{p} over {arm}', warm - cold, within(warm - cold, 0, hi)))
    say(f'- **E15** (warm source: `full` and `probackup8` 0–0.5 s faster than cold, v1 0–0.15 s): '
        f'{word(every(ok for *_, ok in gains))} ({", ".join(f"{n} {fmt(v)}" for n, v, _ in gains) or "no data"}); the '
        f'warm ratios up to 0.17 above the cold against `full`, up to 0.25 against `probackup8`: '
        f'{word(every(ok for *_, ok in deltas))} ({", ".join(f"{n} {v:+.3f}" for n, v, _ in deltas) or "no data"})')
    return lines


def audit_word(audit):
    """E13's audit clause: every arm passes, and pg_probackup's own flush leaves the streamed WAL,
    backup_label and a directory uncovered, which its sync -f covers."""
    if not audit:
        return 'no data (not given)'
    passed = every(result.get('passed', result.get('ok')) for result in audit.values())
    left = []
    for arm in measure.PROBACKUP_METHODS:
        tool = (audit.get(arm) or {}).get('tool_uncovered')
        if tool is not None:
            left.append(any(p.startswith('pg_wal/') or '/pg_wal/' in p for p in tool)
                        and any(p.endswith('backup_label') for p in tool))
    return f'every arm passes {word(passed)}; pg_probackup\'s own flush leaves WAL and backup_label to its sync -f ' \
           f'{word(every(left))}'


def a_lines(S, gates, verdict_fast, verdict_tuned):
    """A1-A9 (A8 removed)."""
    lines = ['## Added rules']
    say = lines.append
    pats = S.patterns
    faster = [(p, U, S.med(p, U, 'full') - S.med(p, U, 'full_syncfs')) for p in pats
              if S.med(p, U, 'full') is not None and S.med(p, U, 'full_syncfs') is not None]
    a1 = every(v >= 0.3 for *_, v in faster)
    say(f'- **A1** (`full_syncfs` 0.3 s or more faster than `full` unthrottled, every workload): '
        f'{"fires" if a1 else "does not fire" if a1 is False else "no data"} ({listing(faster)})'
        + ('; the fastest shipped copy is `full_syncfs`, a one-flag change for Omni' if a1 else ''))
    near = [(p, U, S.med(p, U, 'full_wb') - S.baseline('B_fast', p, U)[1]) for p in LOCAL
            if S.med(p, U, 'full_wb') is not None and S.baseline('B_fast', p, U)[1] is not None]
    differs = verdict_fast['row'] != verdict_tuned['row']
    a2 = any(v <= 0.15 for *_, v in near) or differs
    say(f'- **A2** (`full_wb` within 0.15 s of B_fast unthrottled (or faster), or the HA verdict differs between '
        f'B_tuned and B_fast): {"fires" if a2 else "does not fire"} (full_wb − B_fast: {listing(near)}; verdict against '
        f'B_tuned: {verdict_tuned["row"]})'
        + ('; the HA claim must name the destination\'s write-back setting' if a2 else ''))
    bound = S.bound()
    if not bound['measured']:
        say(f'- **A3** (the bound\'s gate): {bound_state(bound)}')
    else:
        fails = bound['passes'] is False
        state = 'fires' if fails else 'does not fire' if bound['passes'] else 'no data'
        say(f'- **A3** (the faster parcopy arm\'s pooled unthrottled median in hot and append more than floor(U) + 1.0 s, '
            f'or its busiest stream over 1.1 × the mean): {state} '
            f'({bound["arm"]} {fmt(bound["median"])} s over {bound["n"]} runs, floor(U) {fmt(bound["floor"])} s, '
            f'balance {fmt(bound["balance"], 3)})'
            + (('; Confirmed is blocked; parcopy stays in B_fast for Out and the crossovers, which are upper bounds'
                if bound['is_fast'] else '; a released arm is faster: the "bound missing" row applies') if fails else ''))
    fired = [f'fires for {arm}: ' + ', '.join(f'{r["trial_id"]} r{r["repeat"]} {r["speed"]}'
                                             + (' (the copy itself failed)' if r['status'] == 'error' else '')
                                             for r in runs) for arm, runs in S.a4.items()]
    undecided = [f'**undecided** for {arm}: {len(runs)} errored run(s) whose cause isn\'t recorded ('
                 + ', '.join(f'{r["trial_id"]} r{r["repeat"]} {r["speed"]}' for r in runs) + ')'
                 for arm, runs in S.a4_undecided.items() if arm not in S.a4]
    harness = sum(r['status'] == 'error' and r.get('failure_kind') == 'harness' for r in S.rows
                  if r['method'] in measure.PARCOPY_METHODS or r['method'] in measure.PROBACKUP_METHODS)
    say('- **A4** (a parcopy run fails verify_direct or doesn\'t start; a pg_probackup run doesn\'t start or differs '
        'logically while the reference digests match; either\'s copy aborted by the tool itself): '
        + ('; '.join(fired + undecided) or 'does not fire')
        + '; void runs (not failures): ' + str(sum(r['void'] for r in S.rows))
        + f'; errors of the harness, not the tool (not failures): {harness}')
    failing = [r for r in S.rows if r['durability_failures']]
    say(f'- **A5** (a durable arm fails check (a) or (b), or parcopy\'s fsync count is wrong): {len(failing)} run(s) '
        'left out' + ('; reclassified as not durable for the session: ' + '; '.join(
            f'{arm} ({", ".join(why)})' for arm, why in S.not_durable.items()) if S.not_durable else ''))
    control = [r for r in S.rows if r['method'] in measure.NON_DURABLE_METHODS]
    say(f'- **A6** (`full_nosync` passes check (a) in any run): '
        + ('fires: check (a) is void for the session; durability rests on (b)' if S.a_void else
           'does not fire' if control else 'no data'))
    failed = [g for g in gates if g['ok'] is False]
    say(f'- **A7** (drift gates against EXP-09): '
        + ('the session is **shifted** (' + (', '.join(f'{g["pattern"]} {SPEED_LABEL[g["speed"]]} {g["gate"]} '
                                                   f'{g["change"]:+.1%}' for g in failed) or 'no gate could be checked')
           + '); in-session verdicts stand; L3 and L4 are not judged' if shifted(gates) else 'every gate holds'))
    say('- **A8**: removed with the TPROC-C invocation (change 3)')
    counts = [(p, arm, len(S.runs(arm, p, U, 'ha'))) for p in LOCAL for arm in
              ('full', 'full_syncfs', 'probackup8', 'probackup4', 'parcopy8', 'parcopy4', V1, V1_J8)
              if any(r['method'] == arm and r['pattern'] == p for r in S.rows)]
    short = [(p, arm, n) for p, arm, n in counts if n < 10]
    say('- **A9** (the extension: 10 unthrottled runs per extended arm in hot and append, the verdict on all of them): '
        + (f'{len(counts)} cells, ' + ('all with 10 or more counting runs' if not short else 'fewer than 10 counting '
                                       'runs in ' + ', '.join(f'{p} {arm} ({n})' for p, arm, n in short))
           if counts else 'no data') + '; near the line: see the verdict')
    return lines


def verdicts(S, exp09, audit=None):
    """Every section, in order; returns (markdown lines, the crossover table)."""
    S.audit = audit or {}
    table = S.crossover_table(exp09)
    gates = drift_gates(S, exp09)
    bound = S.bound()
    lines = counts_lines(S) + [''] + durability_lines(S, S.audit) + [''] + probe_lines(S) + ['']
    lines += choice_lines(S) + [''] + median_lines(S) + [''] + ratio_lines(S) + [''] + verdict_lines(S) + ['']
    lines.append('### A2\'s reading: the HA verdict against B_tuned')
    ratios = S.all_ratios()
    v_fast = verdict(deciding(ratios), bound)
    v_tuned, rows = ha_lines(ratios, bound, 'against B_tuned', fast='B_tuned')
    lines += rows + ['']
    crossing, found = crossover_lines(S, table, bound)
    lines += crossing + [''] + drift_lines(gates) + [''] + code_lines(S) + ['']
    ledger, required = ledger_lines(S, table, gates)
    lines += ledger + [''] + e_lines(S, found) + [''] + a_lines(S, gates, v_fast, v_tuned) + ['']
    lines += phase_table_lines(S, required) + [''] + above_link_lines(S) + [''] + order_lines(S)
    return lines, table


# --- data and figures -------------------------------------------------------------

def write_data(S, datadir, tag):
    datadir = Path(datadir)
    datadir.mkdir(parents=True, exist_ok=True)
    runs = datadir / f'exp11{tag}-runs.csv'
    with runs.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=KEEP)
        writer.writeheader()
        order = {p: i for i, p in enumerate(PATTERNS)}
        for r in sorted(S.rows, key=lambda r: (order.get(r['pattern'], 9), r['pattern'], SPEEDS.index(r['speed'])
                                               if r['speed'] in SPEEDS else 9, r['method'], r['repeat'])):
            writer.writerow({k: r.get(k) for k in KEEP})
    cells = datadir / f'exp11{tag}-cells.csv'
    with cells.open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['view', 'pattern', 'speed', 'method', 'tier', 'n',
                         *(f'{c}_{s}' for c in CELL_COLUMNS for s in ('median', 'max'))])
        for view in VIEWS:
            for p in S.patterns:
                for s in SPEEDS:
                    for arm in ARM_NAMES:
                        group = S.runs(arm, p, s, view)
                        if not group or (view == 'ha' and not any(r['block'] == 'extension' for r in group)):
                            continue
                        values = [(median(r[c] for r in group), mx(r[c] for r in group)) for c in CELL_COLUMNS]
                        writer.writerow([view, p, s, arm, measure.TIERS.get(arm), len(group),
                                         *('' if v is None else round(v, 4) for pair in values for v in pair)])
    return runs, cells


def fig_baselines(S, table, tag):
    """Main-block medians against this session's measured single-stream rate, log-log, per workload, with
    B_fast's and B_rel's chosen arms and a full copy's bytes at the link rate."""
    pats = [p for p in S.patterns if any(key[0] == p for key in table)] or S.patterns[:1]
    fig, axes = plt.subplots(1, len(pats), figsize=(5.0 * len(pats), 5.0), sharey=True, squeeze=False)
    for ax, p in zip(axes[0], pats):
        for name, color in (('B_fast', COLORS['blue']), ('B_rel', COLORS['orange'])):
            pts = exp04.series(table, p, name)
            if pts:
                ax.plot([x for x, *_ in pts], [y for _, y, *_ in pts], color=color, lw=7, alpha=0.18,
                        solid_capstyle='round', label=f'{name}: its chosen arm at each speed')
        model = exp04.series(table, p, 'link_model')
        if model:
            ax.plot([x for x, *_ in model], [y for _, y, *_ in model], color=COLORS['black'], ls=':', lw=1,
                    label="a full copy's bytes at the link rate (model)")
        for arm, label, color, ls, marker in ARMS:
            pts = exp04.series(table, p, arm)
            if not pts:
                continue
            xs, ys = [x for x, *_ in pts], [y for _, y, *_ in pts]
            err = [[0] * len(pts), [entry['max'] - y for _, y, entry, _ in pts]]
            ax.errorbar(xs, ys, yerr=err, color=color, ls=ls, marker=marker, ms=4.5, capsize=2,
                        lw=2.2 if arm == V1 else 1.2, label=label)
        ax.set_xscale('log')
        ax.set_yscale('log')
        ax.set_xlabel('Measured link speed, one stream, source to destination (Gbit/s)')
        ax.set_title(f'{p} 1%', fontsize=11)
        exp04.plain_log(ax)
    axes[0][0].set_ylabel('Time, median of the main block (s; bar to maximum)')
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc='lower center', ncol=4, fontsize=8, bbox_to_anchor=(0.5, -0.12))
    fig.suptitle('EXP-11: durable full copies and v1\'s path by link speed', fontsize=12)
    return plotting.save(fig, f'exp11-baselines{tag}')


PHASE_PARTS = (('launch and connection (receiver start)', COLORS['grey']),
               ('backup mode: checkpoint, WAL end, stop', COLORS['purple']),
               ('copy (v1: its transfer outside fsync)', COLORS['blue']),
               ('flush: fsync, syncfs, sync -f, manifest', COLORS['red']),
               ('other: exit and untimed by the tool', COLORS['sky']))


def phase_parts(run):
    """A run's time in PHASE_PARTS' order, from its own timers; the rest of total_s is 'other'."""
    def z(*values):
        return sum(v or 0.0 for v in values)
    method = run['method']
    if method in measure.FULL_VARIANTS:
        parts = [z(run['pbb_start_s']), z(run['pbb_checkpoint_s'], run['pbb_wal_end_s']), z(run['pbb_stream_s']),
                 z(run['pbb_sync_s'], run['pbb_manifest_s'])]
    elif method in measure.PROBACKUP_METHODS:
        parts = [z(run['probackup_start_s']), z(run['probackup_stop_s']), z(run['probackup_copy_s']),
                 z(run['probackup_files_sync_s'], run['probackup_sync_s'])]
    else:  # parcopy* and v1: receiver start, then the transfer split by the tool's own timers
        transfer = z(run['transfer_s'])
        flush = min(z(run['parcopy_fsync_s'] if method in measure.PARCOPY_METHODS else run['fsync_s']), transfer)
        # parcopy's times count from its receiver's start; the transfer step starts once it listens.
        copy = (min(max(0.0, z(run['parcopy_last_byte_s']) - z(run.get('parcopy_listening_s'))), transfer - flush)
                if method in measure.PARCOPY_METHODS else transfer - flush)
        parts = [z(run['receiver_start_s']), 0.0, copy, flush]
    return parts + [max(0.0, z(run['total_s']) - sum(parts))]


def fig_phases(S, tag):
    """Each arm's median run (main block) split by its own timers, at 2 Gbit/s and unthrottled."""
    speeds = [s for s in ('2g', U) if any(S.runs(a, p, s) for a in ARM_NAMES for p in S.patterns)] or [U]
    pats = S.patterns or ['hot']
    fig, axes = plt.subplots(len(speeds), len(pats), figsize=(5.0 * len(pats), 3.6 * len(speeds)), squeeze=False)
    seen = set()
    for row, s in zip(axes, speeds):
        for ax, p in zip(row, pats):
            labels = []
            for y, arm in enumerate([a for a in ARM_NAMES if S.runs(a, p, s)]):
                runs = sorted(S.runs(arm, p, s), key=lambda r: r['total_s'])
                run = runs[(len(runs) - 1) // 2]
                left = 0.0
                for (name, color), value in zip(PHASE_PARTS, phase_parts(run)):
                    ax.barh(y, value, left=left, color=color, edgecolor='white', label=name if name not in seen else None)
                    seen.add(name)
                    left += value
                if run['link_time_s']:
                    ax.plot([run['link_time_s']] * 2, [y - 0.4, y + 0.4], color=COLORS['black'], lw=1.2)
                ax.text(left + 0.05, y, f'{run["total_s"]:.2f}', va='center', fontsize=7)
                labels.append(arm)
            ax.set_yticks(range(len(labels)), labels, fontsize=8)
            ax.invert_yaxis()
            ax.set_title(f'{p} 1%, {SPEED_LABEL[s]}', fontsize=10.5)
            ax.set_xlabel('Seconds (median run; black tick: its link time)')
            ax.grid(axis='y', visible=False)
    fig.legend(loc='lower center', ncol=3, fontsize=8.5, bbox_to_anchor=(0.5, -0.08))
    fig.suptitle('EXP-11: where each arm\'s time goes, by its own timers', fontsize=12)
    return plotting.save(fig, f'exp11-phases{tag}')


def load_audit(path):
    """The syscall audit's results by arm: a JSON file, or a run_exp04 results directory whose records
    hold an audit (--syscall-audit), read as `durability_audit.py summary --json` reads it."""
    if not path:
        return {}
    if Path(path).is_dir():
        return durability_audit.arm_results([path])['arms']
    data = json.loads(Path(path).read_text())
    return data.get('arms', data) if isinstance(data, dict) else {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--sweeps', nargs='+', default=[str(p) for p in SWEEPS],
                        help='results directories from remote.py fetch (default: exp11-local and exp11-uniform)')
    parser.add_argument('--exp09-cells', default=str(DATA / 'exp09-10-cells.csv'))
    parser.add_argument('--exp09-runs', default=str(DATA / 'exp09-10-runs.csv'))
    parser.add_argument('--audit', help="the syscall audit's results, from the smoke run: JSON (durability_audit.py "
                                        'summary --json), or the results directory of its --syscall-audit run')
    parser.add_argument('--idle-noise-bytes', type=int, metavar='BYTES',
                        help="check (a)'s idle noise as the smoke run measured it (its run.json "
                             'dirty_idle_noise.max_increase_bytes): every run\'s check (a) is judged again against '
                             'max(4 MiB, this), the registered limit, whatever limit its sweep used')
    parser.add_argument('--datadir', default=str(DATA))
    parser.add_argument('--figdir', help='write figures here instead of docs/figures')
    parser.add_argument('--tag', default='', help='suffix for data and figure names (e.g. -smoke)')
    args = parser.parse_args(argv)
    if args.idle_noise_bytes is not None and args.idle_noise_bytes < 0:
        parser.error('--idle-noise-bytes must be 0 or more')
    sweeps = [Path(s) for s in args.sweeps if Path(s, 'summary.csv').exists()]
    if not sweeps:
        raise SystemExit(f'no summary.csv under {", ".join(args.sweeps)}')
    if args.figdir:
        plotting.FIGURES = Path(args.figdir)
    plotting.style()
    rows, trials, runs = [], {}, []
    for sweep in sweeps:
        r, t, run = load(sweep, args.idle_noise_bytes)
        rows += r
        trials.update(t)
        runs.append(run)
    S = Session(rows, trials, runs, args.idle_noise_bytes)
    lines, table = verdicts(S, load_exp09(args.exp09_cells, args.exp09_runs), load_audit(args.audit))
    print(f'{len(rows)} runs from {", ".join(str(s) for s in sweeps)}')
    print(*write_data(S, args.datadir, args.tag), sep='\n')
    print('\n'.join(lines))
    print(fig_baselines(S, table, args.tag))
    print(fig_phases(S, args.tag))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
