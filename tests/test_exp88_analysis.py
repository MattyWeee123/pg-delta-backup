"""Tests for EXP-88's analysis (analysis/exp88.py), which is committed before any run and registered under rule 16.

Every number asserted here is worked out by hand in the test from a synthetic pgbench log, never read back from
the code: the logs are built one transaction at a time (`steady`, `varied`), so each bucket's scheduled and
completed counts, each latency and each schedule lag is known in advance.

- W and D: a hand-built bucket series whose late transactions give W = 1.2 s and D = 0.80, and the clock mapping
  shown to be applied by the count of transactions that land inside the window at all.
- The null is per level: an L80 arm netted against L80's `none` runs and never L60's, with nulls that differ.
- O4's verdict at the floors, at half and at twice them, including a cell that meets at twice but not at one,
  and the `near` flag firing inside 20% of a delta (and not firing outside it).
- The reported numbers: the percentiles against numpy's on the same sample, the registered percentage increase
  and the TPS loss on known inputs.
- The failed and the skipped transactions counted apart -- told apart from the raw latency field, because the
  parser returns None for the latency of both -- and left out of every percentile.
- The schedule lag reported apart from the latency and from the execution time, on a log where all three differ.
- A run with missing chrony offsets flagged rather than analysed, a `v1_fast` run whose guard refused reported as
  such, and a window too short to judge reported as "too fast to measure".
- A dry run of the whole script on a synthetic session, into a temporary directory, never into docs/.

They need the analysis venv's numpy and matplotlib and are skipped without them.
"""

import csv
import gzip
import io
import json
import math
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from experiments.network import measure

try:
    import matplotlib  # noqa: F401
    import numpy as np
except ImportError:  # the analysis scripts need the analysis venv
    matplotlib = np = None
if matplotlib is not None:
    sys.path.append(str(Path(__file__).resolve().parents[1] / 'analysis'))
    import exp88

needs_venv = unittest.skipUnless(matplotlib is not None, 'needs the analysis venv (numpy, matplotlib)')

T0 = 1_792_000_100.0          # the window's start, on dest's clock
WINDOW = 2.0                  # t1 - t0, so [t0, t1 + 10 s) is 12 one-second buckets
BUCKETS = 12
RATE = 100                    # transactions a second, so W is in hundredths and D in whole percent
# dest minus client, exaggerated so that a dropped or mis-signed clock mapping shows up in the counts.
SHIFT = 3.0
OFFSETS = {'dest': 3.25, 'source': 3.1, 'client': 0.25}
# Nothing is scheduled on a bucket edge, where a microsecond of rounding would move it, and each bucket's
# transactions fill only 0.9 s of it, so a latency under 0.1 s still completes inside the bucket it belongs to.
EDGE, SPREAD = 0.005, 0.9


def line(client, number, end_s, latency_s, script=0, lag_s=0.0):
    """One pgbench -l line under -R: client, transaction, latency us, script, completion s and us, schedule lag us."""
    end = int(round(end_s * 1e6))
    return (f'{client} {number} {int(round(latency_s * 1e6))} {script} {end // 1_000_000} '
            f'{end % 1_000_000} {int(round(lag_s * 1e6))}\n')


def refused(client, number, end_s, kind, script=0, lag_s=0.0):
    """pgbench's line for a transaction it failed or skipped: the word itself stands in the latency field."""
    end = int(round(end_s * 1e6))
    return (f'{client} {number} {kind} {script} {end // 1_000_000} {end % 1_000_000} '
            f'{int(round(lag_s * 1e6))}\n')


def steady(t0, buckets=BUCKETS, rate=RATE, latency_s=0.002, lag_s=0.0, late=None):
    """`rate` transactions scheduled evenly inside each of `buckets` seconds from t0, each taking latency_s.

    late = {bucket: n} holds the last n of that bucket's transactions back a whole second, so they are scheduled
    in their own bucket and complete in the next one: bucket b then has S_b = rate and X_b = rate - n. The
    on-time ones all complete in their own bucket, the last bucket's included, so the counts over
    [t0, t1 + 10 s) are the whole log.
    """
    late, out, number = late or {}, [], 0
    for b in range(buckets):
        moved = late.get(b, 0)
        for i in range(rate):
            scheduled = t0 + b + EDGE + i * (SPREAD / rate)
            latency = latency_s + (1.0 if i >= rate - moved else 0.0)
            number += 1
            out.append(line(i % 4, number, scheduled + latency, latency, lag_s=lag_s))
    return out


def varied(t0, latencies, buckets=BUCKETS):
    """One transaction per given latency, spread inside the buckets, each completing in its own bucket."""
    out, per = [], math.ceil(len(latencies) / buckets)
    for k, latency in enumerate(latencies):
        b, i = divmod(k, per)
        scheduled = t0 + b + EDGE + i * (SPREAD / per)
        out.append(line(k % 4, k + 1, scheduled + latency, latency))
    return out


def write_run(root, index, arm, level, lines, **record):
    """One run directory as the driver's record contract writes it: run.json and the client's gzipped log."""
    folder = Path(root) / f'run-{index:02d}'
    folder.mkdir(parents=True)
    meta = {'run_id': f'{arm}-{level}-{index}', 'arm': arm, 'level': level, 'repeat': 0, 'order_index': index,
            't0_utc': T0, 't1_utc': T0 + WINDOW, 'clock_offset_s': dict(OFFSETS), 'rate_r': RATE,
            'capacity_c': RATE / 0.6, 'share': 0.6, 'summarize_wal': 'on', 'exit_code': 0,
            'fast_path_used': None}
    meta.update(record)
    meta.setdefault('seconds', meta['t1_utc'] - meta['t0_utc'])
    (folder / 'run.json').write_text(json.dumps(meta))
    with gzip.open(folder / 'load-log.gz', 'wt') as stream:
        stream.writelines(lines)
    return folder


LOG_KEYS = ('buckets', 'rate', 'latency_s', 'lag_s', 'late')


def write_session(root, spec):
    """A session directory, one run per entry of `spec`: `arm` and `level`, the log's shape (`late`, `rate`,
    `latency_s`, `lag_s`, `buckets`, or `lines` outright, plus `extra` lines), and every other key into run.json.
    The log is written on the client's clock, SHIFT behind dest's."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for index, given in enumerate(spec):
        entry = dict(given)
        log = {key: entry.pop(key) for key in LOG_KEYS if key in entry}
        extra, lines = entry.pop('extra', []), entry.pop('lines', None)
        arm, level = entry.pop('arm'), entry.pop('level')
        if lines is None:
            lines = steady(entry.get('t0_utc', T0) - SHIFT, **log)
        write_run(root, index, arm, level, list(lines) + list(extra), **entry)
    return root


def rows_of(root, spec):
    """{(arm, level): the cell's row} for a session built from `spec`."""
    write_session(root, spec)
    return {(row['arm'], row['level']): row for row in exp88.cells(exp88.runs_of(root))}


def runs_of(root, spec):
    write_session(root, spec)
    return exp88.runs_of(root)


# --- O4's two numbers ---------------------------------------------------------------------------------------

@needs_venv
class WorkAndDrop(unittest.TestCase):
    def test_work_lost_and_the_worst_drop_are_the_hand_computed_integral_and_maximum(self):
        """12 buckets of 100 transactions a second: 40 of bucket 3's and 80 of bucket 7's complete a second late.

        So S_b = 100 in every bucket; X_3 = 60 and X_7 = 20, and no other bucket completes fewer than it
        scheduled. By hand, with R = 100:
            W = (40 + 80) / 100 = 1.20 s;
            D = max(1 - 60/100, 1 - 20/100) = 0.80.
        The log is written on the client's clock, 3 s behind dest's, so all 1200 transactions land inside
        [t0, t1 + 10 s) only if the chrony mapping is applied: that is what the count and the rate check.
        """
        with tempfile.TemporaryDirectory() as tmp:
            rows = rows_of(tmp, [{'arm': 'v1', 'level': 'L60', 'late': {3: 40, 7: 80}}])
        row = rows[('v1', 'L60')]
        self.assertAlmostEqual(row['W'], 1.20, places=9)
        self.assertAlmostEqual(row['D'], 0.80, places=9)
        self.assertEqual(row['txns_ok'], BUCKETS * RATE)
        self.assertAlmostEqual(row['tps'], 100.0, places=9)
        # No `none` runs at this level, so nothing is netted and the analysis says so instead of guessing.
        self.assertNotIn('W_net', row)
        self.assertIn('no `none` runs at L60', row['notes'])

    def test_a_cell_is_netted_against_its_own_levels_none_runs_and_never_another_levels(self):
        """L60's null loses 20 transactions, L80's loses 50, and the arm under test runs at L80.

        By hand: none at L60 W = 0.20, D = 0.20; none at L80 W = 0.50, D = 0.50; v1 at L80 W = 1.20, D = 0.80.
        Netted against L80's own null, W_net = 1.20 - 0.50 = 0.70 and D_net = 0.80 - 0.50 = 0.30. Netted
        against L60's it would be 1.00 and 0.60, which is what this test forbids.
        """
        spec = [{'arm': 'none', 'level': 'L60', 'late': {3: 20}},
                {'arm': 'none', 'level': 'L80', 'late': {3: 50}},
                {'arm': 'v1', 'level': 'L80', 'late': {3: 40, 7: 80}}]
        with tempfile.TemporaryDirectory() as tmp:
            rows = rows_of(tmp, spec)
        self.assertAlmostEqual(rows[('none', 'L60')]['W'], 0.20, places=9)
        self.assertAlmostEqual(rows[('none', 'L80')]['W'], 0.50, places=9)
        row = rows[('v1', 'L80')]
        self.assertAlmostEqual(row['W_net'], 0.70, places=9)
        self.assertAlmostEqual(row['D_net'], 0.30, places=9)
        self.assertNotAlmostEqual(row['W_net'], 1.00, places=3)  # L60's null, which must never be used here
        self.assertNotAlmostEqual(row['D_net'], 0.60, places=3)
        # Each level's own null nets to zero, and neither level sees the other's.
        self.assertEqual((rows[('none', 'L60')]['W_net'], rows[('none', 'L80')]['W_net']), (0.0, 0.0))


@needs_venv
class Verdict(unittest.TestCase):
    """O4 at the floors and at half and twice them. The null loses nothing, so each arm's net value is its own.

    By hand at L60: full W = 0.20 and D = 0.20, so W_net(full) = 0.20 and D_net(full) = 0.20, and the floors are
    delta_W = max(0.05, 0.15 x 0.20) = 0.05 and delta_D = 0.03.
        v1_fast  W = 0.20, D = 0.20 -> excess (0.00, 0.00): meets at 0.5x, 1x and 2x;
        v1_j4    W = 0.24, D = 0.22 -> excess (0.04, 0.02): misses 0.5x (0.04 > 0.025), meets 1x and 2x;
        v1       W = 0.29, D = 0.20 -> excess (0.09, 0.00): misses 0.5x and 1x (0.09 > 0.05), meets 2x (0.09 <= 0.10).
    """

    SPEC = [{'arm': 'none', 'level': 'L60'},
            {'arm': 'full', 'level': 'L60', 'late': {3: 20}},
            {'arm': 'v1_fast', 'level': 'L60', 'late': {3: 20}},
            {'arm': 'v1_j4', 'level': 'L60', 'late': {3: 22, 7: 2}},
            {'arm': 'v1', 'level': 'L60', 'late': {3: 20, 7: 9}}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rows = rows_of(self.tmp.name, self.SPEC)

    def test_the_floors_are_exp12s_and_pg_basebackups_own_cost_sets_them(self):
        full = self.rows[('full', 'L60')]
        self.assertAlmostEqual(full['W_net'], 0.20, places=9)
        self.assertAlmostEqual(full['D_net'], 0.20, places=9)
        for arm in ('v1', 'v1_j4', 'v1_fast'):
            self.assertAlmostEqual(self.rows[(arm, 'L60')]['delta_W'], 0.05, places=9)
            self.assertAlmostEqual(self.rows[(arm, 'L60')]['delta_D'], 0.03, places=9)

    def test_the_o4_verdict_is_given_at_the_floors_and_at_half_and_at_twice_them(self):
        expected = {'v1_fast': (0.00, 0.00, True, True, True),
                    'v1_j4': (0.04, 0.02, False, True, True),
                    'v1': (0.09, 0.00, False, False, True)}
        for arm, (w, d, half, one, twice) in expected.items():
            row = self.rows[(arm, 'L60')]
            self.assertAlmostEqual(row['W_excess'], w, places=9, msg=arm)
            self.assertAlmostEqual(row['D_excess'], d, places=9, msg=arm)
            self.assertEqual((row['meets_x0.5'], row['meets_x1'], row['meets_x2']), (half, one, twice), arm)

    def test_a_cell_that_meets_o4_only_at_twice_the_floors_is_reported_as_such(self):
        row = self.rows[('v1', 'L60')]
        self.assertTrue(row['meets_x2'])
        self.assertFalse(row['meets_x1'] or row['meets_x0.5'])

    def test_the_near_flag_fires_when_an_excess_is_within_twenty_percent_of_its_delta(self):
        """At L30 pg_basebackup loses a full second, so delta_W is its share, 0.15 x 1.00 = 0.15, not the floor.

        v1's excess over it is 1.13 - 1.00 = 0.13, which is 0.867 of delta_W: inside 20% of the line, so `near`
        fires even though the cell misses O4 on D (0.13 against delta_D = 0.03). v1_fast's excess is 0.00, a
        whole delta away, so `near` stays off.
        """
        spec = [{'arm': 'none', 'level': 'L30'},
                {'arm': 'full', 'level': 'L30', 'late': {3: 50, 7: 50}},
                {'arm': 'v1', 'level': 'L30', 'late': {3: 50, 7: 63}},
                {'arm': 'v1_fast', 'level': 'L30', 'late': {3: 50, 7: 50}}]
        with tempfile.TemporaryDirectory() as tmp:
            rows = rows_of(tmp, spec)
        self.assertAlmostEqual(rows[('full', 'L30')]['W_net'], 1.00, places=9)
        row = rows[('v1', 'L30')]
        self.assertAlmostEqual(row['delta_W'], 0.15, places=9)
        self.assertAlmostEqual(row['W_excess'], 0.13, places=9)
        self.assertTrue(row['near'])
        self.assertFalse(row['meets_x1'])          # near the line is not the same thing as over it
        self.assertFalse(rows[('v1_fast', 'L30')]['near'])


# --- what is reported and decides nothing -------------------------------------------------------------------

@needs_venv
class Reported(unittest.TestCase):
    def test_the_window_percentiles_are_numpys_on_the_same_sample(self):
        latencies = [0.001 + (k % 37) * 0.0005 for k in range(400)]
        sample = [round(latency * 1e6) / 1e3 for latency in latencies]   # the microseconds the log carries, in ms
        with tempfile.TemporaryDirectory() as tmp:
            rows = rows_of(tmp, [{'arm': 'v1', 'level': 'L60', 'lines': varied(T0 - SHIFT, latencies)}])
        row = rows[('v1', 'L60')]
        self.assertEqual(row['txns_ok'], len(latencies))
        for p in (50, 95, 99):
            self.assertAlmostEqual(row[f'lat_p{p}_ms'], float(np.percentile(sample, p)), places=9, msg=p)
        self.assertAlmostEqual(row['lat_p50_ms'], 10.0, places=9)   # the sample's own median, by hand

    def test_the_percentage_increase_is_the_registered_ratio_against_the_matched_none_cell(self):
        """none: 1200 transactions at 10 ms, 100 a second. v1: 1080 at 13 ms, 90 a second.

        By hand: every percentile increase is (13 / 10 - 1) x 100 = 30%, and the TPS loss is
        (1 - 90 / 100) x 100 = 10%. The null's own increase and loss are 0.
        """
        spec = [{'arm': 'none', 'level': 'L60', 'latency_s': 0.010},
                {'arm': 'v1', 'level': 'L60', 'latency_s': 0.013, 'rate': 90}]
        with tempfile.TemporaryDirectory() as tmp:
            rows = rows_of(tmp, spec)
        null, row = rows[('none', 'L60')], rows[('v1', 'L60')]
        self.assertAlmostEqual(null['lat_p95_ms'], 10.0, places=9)
        self.assertAlmostEqual(row['lat_p95_ms'], 13.0, places=9)
        for p in (50, 95, 99):
            self.assertAlmostEqual(row[f'lat_p{p}_inc_pct'], 30.0, places=6, msg=p)
            self.assertAlmostEqual(null[f'lat_p{p}_inc_pct'], 0.0, places=9, msg=p)
        self.assertAlmostEqual(row['tps'], 90.0, places=9)
        self.assertAlmostEqual(row['tps_loss_pct'], 10.0, places=9)
        self.assertAlmostEqual(null['tps_loss_pct'], 0.0, places=9)

    def test_schedule_lag_is_reported_apart_from_the_latency_and_from_the_execution_time(self):
        """Under -R pgbench's latency already contains the scheduling delay: 25 ms of latency over 20 ms of lag
        is 5 ms of execution. All three differ, so folding any two together fails this test."""
        with tempfile.TemporaryDirectory() as tmp:
            rows = rows_of(tmp, [{'arm': 'v1', 'level': 'L60', 'latency_s': 0.025, 'lag_s': 0.020}])
        row = rows[('v1', 'L60')]
        for p in (50, 95, 99):
            self.assertAlmostEqual(row[f'lat_p{p}_ms'], 25.0, places=9, msg=p)
            self.assertAlmostEqual(row[f'lag_p{p}_ms'], 20.0, places=9, msg=p)
            self.assertAlmostEqual(row[f'exec_p{p}_ms'], 5.0, places=9, msg=p)
        # And the distinction is written down where a reader of the output will meet it.
        self.assertIn('includes the scheduling delay', exp88.__doc__.lower())
        self.assertTrue({'lat_p99_ms', 'lag_p99_ms', 'exec_p99_ms'} <= set(exp88.COLUMNS))

    def test_the_summarize_wal_control_is_compared_with_none_and_never_judged_against_full(self):
        """none (summaries on) at 10 ms p99 against none_nosum (off) at 8 ms gives EXP-83d's on/off ratio
        10 / 8 = 1.25, and the control carries no O4 verdict."""
        spec = [{'arm': 'none', 'level': 'L60', 'latency_s': 0.010},
                {'arm': 'none_nosum', 'level': 'L60', 'latency_s': 0.008, 'summarize_wal': 'off'},
                {'arm': 'full', 'level': 'L60', 'latency_s': 0.010, 'late': {3: 20}}]
        with tempfile.TemporaryDirectory() as tmp:
            rows = rows_of(tmp, spec)
        row = rows[('none_nosum', 'L60')]
        self.assertEqual(row['summarize_wal'], 'off')
        self.assertAlmostEqual(row['summarize_wal_p99_ratio'], 1.25, places=9)
        for key in ('W_excess', 'D_excess', 'meets_x0.5', 'meets_x1', 'meets_x2', 'near'):
            self.assertNotIn(key, row)
        self.assertIn('a control, not a competitor', row['notes'])
        self.assertIsNone(rows[('none', 'L60')].get('summarize_wal_p99_ratio'))


@needs_venv
class Transactions(unittest.TestCase):
    def test_failed_and_skipped_transactions_are_counted_apart_and_left_out_of_the_percentiles(self):
        """7 failed and 3 skipped inside the window, 2 failed before it, and 1200 successful at 25 ms over 20 ms
        of lag. The refused ones carry 900 ms of lag, so leaking them into any percentile would show.

        They cannot be told apart from the parsed value -- measure.parse_pgbench_line returns None for the
        latency of both -- so the analysis reads the raw field, which this test pins.
        """
        inside = [refused(0, n, T0 - SHIFT + 5.5 + n * 0.01, 'failed', lag_s=0.9) for n in range(7)]
        inside += [refused(1, n, T0 - SHIFT + 6.5 + n * 0.01, 'skipped', lag_s=0.9) for n in range(3)]
        before = [refused(2, n, T0 - SHIFT - 2.0 - n * 0.01, 'failed', lag_s=0.9) for n in range(2)]
        self.assertIsNone(measure.parse_pgbench_line(inside[0])[2])
        self.assertIsNone(measure.parse_pgbench_line(inside[-1])[2])
        spec = [{'arm': 'v1', 'level': 'L60', 'latency_s': 0.025, 'lag_s': 0.020,
                 'extra': inside + before}]
        with tempfile.TemporaryDirectory() as tmp:
            rows = rows_of(tmp, spec)
        row = rows[('v1', 'L60')]
        self.assertEqual((row['txns_ok'], row['txns_failed'], row['txns_skipped']), (BUCKETS * RATE, 7, 3))
        for p in (50, 95, 99):
            self.assertAlmostEqual(row[f'lat_p{p}_ms'], 25.0, places=9, msg=p)
            self.assertAlmostEqual(row[f'lag_p{p}_ms'], 20.0, places=9, msg=p)
        self.assertIn('7 failed and 3 skipped', row['notes'])


# --- what must be flagged rather than analysed --------------------------------------------------------------

@needs_venv
class Flags(unittest.TestCase):
    def test_a_run_with_missing_chrony_offsets_is_flagged_rather_than_analysed(self):
        """One run has no dest offset and one has no client key at all: neither can be put on dest's clock, so
        neither is given a W. The third run is sound and is the only one the cell counts."""
        spec = [{'arm': 'v1', 'level': 'L60', 'late': {3: 20},
                 'clock_offset_s': {'dest': None, 'source': 3.1, 'client': 0.25}},
                {'arm': 'v1', 'level': 'L60', 'late': {3: 20}, 'clock_offset_s': {'dest': 3.25, 'source': 3.1}},
                {'arm': 'v1', 'level': 'L60', 'late': {3: 20}}]
        with tempfile.TemporaryDirectory() as tmp:
            runs = runs_of(tmp, spec)
            rows = {(r['arm'], r['level']): r for r in exp88.cells(runs)}
        for run in runs[:2]:
            self.assertFalse(run['ok'])
            self.assertIn('no chrony offsets', run['flags'])
            self.assertIsNone(run.get('W'))          # not analysed on a clock we cannot map
            self.assertIsNone(run['clock_shift_s'])
        self.assertTrue(runs[2]['ok'])
        self.assertAlmostEqual(runs[2]['clock_shift_s'], SHIFT, places=9)
        row = rows[('v1', 'L60')]
        self.assertEqual((row['runs'], row['flagged_runs']), (1, 2))
        self.assertIn('no chrony offsets', row['notes'])
        self.assertAlmostEqual(row['W'], 0.20, places=9)   # the sound run's own value, the flagged ones left out

    def test_a_fast_path_run_whose_guard_refused_is_reported_as_not_having_activated(self):
        spec = [{'arm': 'none', 'level': 'L60'},
                {'arm': 'full', 'level': 'L60', 'late': {3: 20}},
                {'arm': 'v1_fast', 'level': 'L60', 'late': {3: 20}, 'fast_path_used': False},
                {'arm': 'v1_fast', 'level': 'L80', 'late': {3: 20}, 'fast_path_used': True},
                {'arm': 'none', 'level': 'L80'},
                {'arm': 'v1', 'level': 'L80', 'late': {3: 20}, 'fast_path_used': None}]
        with tempfile.TemporaryDirectory() as tmp:
            rows = rows_of(tmp, spec)
        refused_cell = rows[('v1_fast', 'L60')]
        self.assertEqual(refused_cell['fast_path_used'], 'false')
        self.assertIn('did not activate in 1 of 1', refused_cell['fast_path_note'])
        self.assertIn('not evidence about the fast path', refused_cell['fast_path_note'])
        self.assertEqual(rows[('v1_fast', 'L80')]['fast_path_used'], 'true')
        self.assertIsNone(rows[('v1_fast', 'L80')].get('fast_path_note'))
        self.assertEqual(rows[('v1', 'L80')]['fast_path_used'], '')

    def test_a_window_too_short_to_judge_is_reported_as_too_fast_to_measure(self):
        """Prediction 6: under two 1-second buckets, W and D are not judged -- the cell says so instead of
        passing."""
        spec = [{'arm': 'none', 'level': 'L60'},
                {'arm': 'full', 'level': 'L60', 'late': {3: 20}},
                {'arm': 'v1_fast', 'level': 'L60', 'late': {3: 20}, 't1_utc': T0 + 1.0}]
        with tempfile.TemporaryDirectory() as tmp:
            rows = rows_of(tmp, spec)
        row = rows[('v1_fast', 'L60')]
        self.assertFalse(row['judged'])
        self.assertIn('too fast to measure', row['notes'])
        for key in ('meets_x0.5', 'meets_x1', 'meets_x2', 'near'):
            self.assertIsNone(row[key], key)
        self.assertIsNotNone(row['W'])              # reported, just not judged
        self.assertTrue(rows[('full', 'L60')]['judged'])


# --- the whole script ---------------------------------------------------------------------------------------

@needs_venv
class DryRun(unittest.TestCase):
    SPEC = ([{'arm': 'none', 'level': level, 'latency_s': 0.010} for level in ('L60', 'L80')]
            + [{'arm': 'none_nosum', 'level': 'L60', 'latency_s': 0.008, 'summarize_wal': 'off'}]
            + [{'arm': 'full', 'level': level, 'latency_s': 0.010, 'late': {3: 20}} for level in ('L60', 'L80')]
            + [{'arm': 'v1', 'level': level, 'latency_s': 0.010, 'late': {3: 20, 7: 9}}
               for level in ('L60', 'L80')]
            + [{'arm': 'v1_fast', 'level': 'L80', 'latency_s': 0.010, 'late': {3: 20},
                'fast_path_used': True},
               {'arm': 'v1_j4', 'level': 'L60', 'latency_s': 0.010, 'late': {3: 22, 7: 2}}])

    def test_the_script_writes_its_cells_and_its_figure_without_touching_docs(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = write_session(Path(tmp) / 'stage1', self.SPEC)
            # The synthetic load runs at one rate at every level, so rates.json gives that rate for each.
            (session / 'rates.json').write_text(json.dumps(
                {'capacity_c': RATE / 0.6, 'levels': {'L30': RATE, 'L60': RATE, 'L80': RATE},
                 'capacity_runs': [{'tps': RATE / 0.6}] * 3}))
            out = io.StringIO()
            with redirect_stdout(out):
                rows = exp88.main(['--runs', str(session), '--figdir', tmp, '--datadir', tmp])
            data = Path(tmp) / 'exp88-cells.csv'
            figure = Path(tmp) / 'exp88-slowdown-by-load.svg'
            self.assertTrue(data.exists() and figure.exists())
            with open(data, newline='') as stream:
                read = list(csv.DictReader(stream))
                stream.seek(0)
                header = next(csv.reader(stream))
        self.assertEqual(header, exp88.COLUMNS)        # the documented order, and nothing outside it
        self.assertEqual(len(read), len(rows))
        self.assertEqual([(r['arm'], r['level']) for r in read],
                         [(r['arm'], r['level']) for r in rows])
        printed = out.getvalue()
        self.assertIn(f'{len(self.SPEC)} runs, {len(self.SPEC)} counted', printed)
        self.assertIn(f'capacity C {RATE / 0.6}', printed)
        self.assertIn('v1_j4', printed)
        # The arms' verdicts are the hand-computed ones of the Verdict case, now at two levels.
        by = {(r['arm'], r['level']): r for r in rows}
        for level in ('L60', 'L80'):
            self.assertAlmostEqual(by[('none', level)]['W'], 0.0, places=9)
            self.assertAlmostEqual(by[('full', level)]['W_net'], 0.20, places=9)
            self.assertAlmostEqual(by[('v1', level)]['W_excess'], 0.09, places=9)
            self.assertEqual((by[('v1', level)]['meets_x1'], by[('v1', level)]['meets_x2']), (False, True))
        self.assertEqual(by[('v1_j4', 'L60')]['meets_x1'], True)


if __name__ == '__main__':
    unittest.main()
