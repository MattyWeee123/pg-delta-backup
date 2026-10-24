"""Tests for EXP-12's analysis (analysis/exp12.py).

- The logs: pgbench's lines read fast and line by line, W and D and the rest on a window with known answers, a pause of
  2 s at ρ giving W in [2 - (1 - ρ), 2] and D = 1 (the queue model of exp12_smoke_data), smoke item 1's tallies.
- The null: the block bootstrap on a pool with a known spread, each run at its own T, measurable, the bound, and δ
  centred for two cells of different T.
- Every row of every rule, on cells given as a table (TableCells): L1-L5's registered readings, R1-R3, decision 1's
  rows 1-7 with 7a, 7b and 7c, decision 2's rows in their order, "not measurable" on either side and "not judged"
  included; A2-A6, A13, near the line and fragile.
- Dry runs of the whole script on a synthetic session (exp12_smoke_data) and on the stubbed harness's own trial
  (test_exp12_load's LoadStub), writing only into a temporary directory.

They need the analysis venv's numpy and matplotlib and are skipped without them.
"""

import csv
import io
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from experiments.network import measure

try:
    import matplotlib  # noqa: F401
    import numpy as np
except ImportError:  # the analysis scripts need the analysis venv
    matplotlib = np = None
if matplotlib is not None:
    sys.path.append(str(Path(__file__).resolve().parents[1] / 'analysis'))
    import exp12
    import exp12_smoke_data
    import plotting

U = measure.UNTHROTTLED
PROXY, PROXY_S1 = 'v1proxy', 'v1proxy_s1'
V1 = 'pgdelta_direct_inplace'
needs_venv = unittest.skipUnless(matplotlib is not None, 'needs the analysis venv (numpy, matplotlib)')
T0 = 1_792_000_100.0


def line(client, number, end_s, latency_s, script=0, lag_s=0.0):
    end = int(round(end_s * 1e6))
    return f'{client} {number} {int(round(latency_s * 1e6))} {script} {end // 1_000_000} {end % 1_000_000} ' \
           f'{int(round(lag_s * 1e6))}\n'


def regular_log(path, start, stop, rate, latency=0.001, stall=None):
    """One transaction every 1/rate s scheduled from start to stop, each taking `latency`; those scheduled inside
    stall = (from, to) complete together at stall's `to` plus 0.2 s."""
    lines, number = [], 0
    count = int(round((stop - start) * rate))
    for index in range(count):
        scheduled = start + index / rate
        end = scheduled + latency
        if stall and stall[0] <= scheduled < stall[1]:
            end = stall[1] + 0.2
        number += 1
        lines.append((end, line(index % 4, number, end, end - scheduled)))
    Path(path).write_text(''.join(text for _, text in sorted(lines)))


# --- the logs -----------------------------------------------------------------------------------------

@needs_venv
class Logs(unittest.TestCase):
    def test_pgbench_lines_read_fast_and_line_by_line(self):
        lines = ['0 1 1075 0 1792846899 105638 346\n', '1 1 1745 1 1792846899 109648 1150\n']
        fast = exp12.parse_chunk(lines)
        self.assertEqual(list(fast['end']), [1792846899_105638, 1792846899_109648])
        self.assertEqual(list(fast['lag']), [346, 1150])
        self.assertEqual(list(fast['script']), [0, 1])
        # A failed transaction sends the chunk through measure.parse_pgbench_line, which counts it apart.
        slow = exp12.parse_chunk(lines + ['2 1 failed 0 1792846899 200000 5\n', 'garbage\n'])
        self.assertEqual((len(slow['end']), slow['failed'], slow['unparsed']), (2, 1, 1))
        self.assertEqual(list(slow['latency']), [1075, 1745])
        # Without -R (a capacity run) there is no lag column: 0.
        closed = exp12.parse_chunk(['2 7 144 0 1792847871 159000\n'])
        self.assertEqual((list(closed['lag']), list(closed['latency'])), ([0], [144]))
        self.assertEqual(len(exp12.parse_chunk([])['end']), 0)

    def span_of(self, tmp, rate=100, stall=(T0 + 2, T0 + 3), t1=T0 + 5):
        path = Path(tmp) / 'busy.1'
        regular_log(path, T0 - 20, T0 + 40, rate, stall=stall)
        span = exp12.Span(('window', 1), 'busy', T0 - exp12.PRE_S, T0 + exp12.window_length(T0, t1, 15),
                          [10_000, 20_000], seconds=True, core=(T0, t1), ends=True)
        tally = exp12.stream_run([path], [span], chunk_lines=700)
        span.finish()
        return span, tally

    def test_w_and_d_on_a_window_with_known_answers(self):
        # 100 per second; those scheduled in [t0 + 2, t0 + 3) complete together at t0 + 3.2. Bucket 2 completes
        # nothing (D = 1); bucket 3 catches up, which W doesn't let cancel: W = 100 / 100 = 1 s of work.
        with tempfile.TemporaryDirectory() as tmp:
            span, tally = self.span_of(tmp)
        stats = exp12.window_stats(span, T0, T0 + 5, 100)
        self.assertEqual((stats['T'], stats['T15']), (15, 20))
        self.assertAlmostEqual(stats['W'], 1.0)
        self.assertAlmostEqual(stats['D'], 1.0)
        self.assertEqual(stats['worst_bucket'], 2)
        self.assertAlmostEqual(stats['worst_bucket_lost_s'], 1.0)
        self.assertAlmostEqual(stats['D05'], 1.0)
        self.assertAlmostEqual(stats['D2'], 0.0)  # [2, 4) s: 200 scheduled, 200 completed
        self.assertAlmostEqual(stats['late_L'], 1.0)  # the stalled 100, each at least 0.2 s late
        self.assertAlmostEqual(stats['late_2L'], 1.0)
        self.assertAlmostEqual(stats['W15'], 1.0)
        self.assertEqual((stats['scheduled'], stats['completed'], stats['backlog_end']), (1500, 1500, 0))
        latency = stats['latency']
        self.assertEqual(latency['transactions'], 500)  # scheduled in [t0, t1]
        self.assertEqual(latency['p50_ms'], 1.0)
        self.assertEqual(latency['over_100ms'], 100)
        self.assertAlmostEqual(latency['max_ms'], 1200.0, places=1)
        self.assertGreater(stats['p99_worst5s_ms'], 1000)
        # The largest gap between completions: from the last before the stall to the stalled ones.
        self.assertAlmostEqual(stats['gap_utc'][0], T0 + 1.99 + 0.001, places=4)
        self.assertAlmostEqual(stats['gap_utc'][1], T0 + 3.0 + 0.001, places=4)
        self.assertEqual(stats['series']['first_s'], -10)
        self.assertEqual(stats['series']['ratio'][12], 0.0)  # t0 + 2
        self.assertEqual((tally['lines'], tally['lag_over_latency'], tally['not_rising']), (6000, 0, 0))

    def test_no_stall_no_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            span, _ = self.span_of(tmp, stall=None)
        stats = exp12.window_stats(span, T0, T0 + 5, 100)
        self.assertEqual((stats['W'], stats['D'], stats['late_L'], stats['backlog_end']), (0.0, 0.0, 0.0, 0))

    def test_a_pause_of_2_s_at_rho(self):
        # The queue model: Poisson arrivals at R, one server of capacity C stopped for 2.0 s. W lies in
        # [2 - (1 - ρ), 2] (the drain offsets at most (1 - ρ) s in the bucket where the pause ends) and D = 1.
        for rate, capacity in ((300, 1000), (600, 1000)):
            rho = rate / capacity
            with self.subTest(rho=rho), tempfile.TemporaryDirectory() as tmp:
                start = T0
                run = exp12_smoke_data.pause_log(tmp, rate, capacity, (10.3, 2.0), seconds=40, start=start)
                t0 = start + 10.3 - 0.05
                span = exp12.Span('w', 'p', t0 - exp12.PRE_S, t0 + exp12.window_length(t0, t0 + 2.1, 15),
                                  [10_000, 20_000], seconds=True, core=(t0, t0 + 2.1), ends=True)
                exp12.stream_run([Path(tmp) / name for name in run['log_files']], [span])
                span.finish()
                stats = exp12.window_stats(span, t0, t0 + 2.1, rate)
                self.assertGreaterEqual(stats['W'], 2 - (1 - rho) - 0.05)
                self.assertLessEqual(stats['W'], 2.0 + 0.05)
                self.assertEqual(stats['D'], 1.0)
                gap = stats['gap_utc'][1] - stats['gap_utc'][0]
                self.assertTrue(2.0 <= gap < 2.03, gap)
                # The late count at L: about 2 / (1 - ρ) s.
                self.assertAlmostEqual(stats['late_L'], 2 / (1 - rho), delta=0.35)

    def test_smoke_item_1s_tallies_and_compressed_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'r.1'
            path.write_text(line(0, 1, T0, 0.001, lag_s=0.0005) + line(0, 2, T0 - 1, 0.001)  # falls back in time
                            + line(1, 1, T0 + 1, 0.001, lag_s=0.002)  # lag above latency
                            + line(1, 2, T0 + 2, 0.001, script=1) + '2 1 failed 0 1792000101 0 0\n')
            tally = exp12.stream_run([path, Path(tmp) / 'r.1.1'], [])
            self.assertEqual((tally['lines'], tally['commits'], tally['aborts'], tally['failed']), (5, 3, 1, 1))
            self.assertEqual((tally['lag_over_latency'], tally['not_rising']), (1, 1))
            self.assertEqual(tally['missing'], ['r.1.1'])
            if shutil.which('zstd'):
                subprocess.run(['zstd', '-q', '--rm', str(path)], check=True)
                again = exp12.stream_run([measure.existing_log(tmp, 'r.1')], [])
                self.assertEqual((again['lines'], again['not_rising']), (5, 1))


# --- the null -------------------------------------------------------------------------------------

def periodic(seconds=400, every=10, deficit=1.0):
    """A pool span's series with a deficit of `deficit` s in every `every`-th bucket and nothing else."""
    W = np.zeros(seconds)
    W[::every] = deficit
    D = np.where(W > 0, 0.5, 0.0)
    zeros = np.zeros(seconds)
    return {'W': W, 'D': D, 'late_L': zeros, 'late_2L': zeros, 'D05': np.zeros(2 * seconds), 'D2': np.zeros(seconds // 2)}


@needs_venv
class Null(unittest.TestCase):
    def test_a_grid_joins_blocks_and_cuts_the_last_short(self):
        grid = exp12.Grid([np.arange(30, dtype=float)], 1.0, 'sum')
        self.assertEqual(grid.B, 10)
        self.assertEqual(len(grid.positions), 21)  # every start with 10 buckets after it, inside the span
        idx = np.array([[0, 20]])
        self.assertEqual(grid.window(idx, 15)[0], sum(range(10)) + sum(range(20, 25)))
        top = exp12.Grid([np.arange(30, dtype=float)], 1.0, 'max')
        self.assertEqual(top.window(idx, 15)[0], 24)
        # A window shorter than one block takes one block's beginning.
        self.assertEqual(grid.window(np.array([[5]]), 3)[0], 5 + 6 + 7)
        # Spans shorter than a block give no position; a pool of them can't be drawn from.
        self.assertEqual(len(exp12.Grid([np.ones(9)], 1.0, 'sum').positions), 0)
        self.assertFalse(exp12.Null('p', [periodic(9)], 100, 1).usable)

    def test_a_known_spread(self):
        # A deficit of 1 s in every 10th bucket: any block of 10 holds exactly one. A window of 20 buckets always
        # loses 2 s; one of 15 loses 2 s when its last 5 buckets catch a deficit (half the starts), else 1 s.
        null = exp12.Null('busy', [periodic()], 4000, 121)
        windows = null.windows('run-1', 15, 20)
        self.assertTrue(np.all(windows['W15'] == 2.0))
        share = float(np.mean(windows['W'] == 2.0))
        self.assertTrue(0.45 <= share <= 0.55, share)
        self.assertTrue(set(np.unique(windows['W'])) <= {1.0, 2.0})
        self.assertEqual(float(np.percentile(windows['W'], 95)), 2.0)
        self.assertTrue(np.all(windows['D'] == 0.5))
        # Seeded: the same key draws the same windows, another key others.
        self.assertTrue(np.array_equal(null.windows('run-1', 15, 20)['W'], windows['W']))
        self.assertFalse(np.array_equal(null.windows('run-2', 15, 20)['W'], windows['W']))

    def test_a_known_spread_from_a_log(self):
        # The same pool from pgbench's lines: 100 per second, and in every 10th second the transactions scheduled in
        # it complete together at its end plus 0.2 s, so that bucket loses 1 s of work and the next catches up.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'busy.1'
            lines, number = [], 0
            for index in range(100 * 200):
                scheduled = T0 + index / 100
                stalled = int(index // 100) % 10 == 0
                end = (T0 + index // 100 + 1.2) if stalled else scheduled + 0.001
                number += 1
                lines.append((end, line(index % 4, number, end, end - scheduled)))
            path.write_text(''.join(text for _, text in sorted(lines)))
            span = exp12.Span('settle', 'busy', T0, T0 + 200, [10_000, 20_000])
            exp12.stream_run([path], [span])
            span.finish()
        series = exp12.pool_series(span, 100)
        self.assertEqual(len(series['W']), 200)
        self.assertEqual(list(series['W'][:11]), [1.0] + [0.0] * 9 + [1.0])
        null = exp12.Null('busy', [series], 4000, 121)
        windows = null.windows('run-1', 15, 20)
        self.assertTrue(np.all(windows['W15'] == 2.0))
        self.assertTrue(0.45 <= float(np.mean(windows['W'] == 2.0)) <= 0.55)
        self.assertTrue(np.all(windows['D'] == 1.0))
        # The late count: the stalled bucket's 100 transactions, each at least 0.2 s late.
        self.assertTrue(np.all(windows['late_L'] >= 1.0))

    def test_measurable_net_and_the_bound(self):
        null = np.linspace(0.0, 0.1, 1001)  # median 0.05, 95th percentile 0.095
        found = exp12.null_summary(0.2, null)
        self.assertAlmostEqual(found['null_median'], 0.05)
        self.assertAlmostEqual(found['null_p95'], 0.095)
        self.assertAlmostEqual(found['net'], 0.15)
        self.assertTrue(found['measurable'])
        self.assertAlmostEqual(found['bound'], 0.045)
        inside = exp12.null_summary(0.08, null)
        self.assertFalse(inside['measurable'])
        self.assertAlmostEqual(inside['net'], 0.03)
        self.assertEqual(exp12.null_summary(0.01, null)['net'], 0.0)  # below the null's median: 0
        self.assertTrue(np.all(found['centred'] >= 0))

    def test_delta_is_centred_for_two_cells_of_different_T(self):
        # W's null grows with T: 2 s at T = 20, 4 s at T = 40, every time. Uncentred, the difference of the two cells'
        # nulls would be -2 s; centred, each is 0 about its own median, so the percentile is 0 and δ_W the floor.
        null = exp12.Null('busy', [periodic()], 2000, 121)
        short = np.median(np.stack([null.windows(f'a{i}', 20, 25)['W'] for i in range(5)], axis=1), axis=1)
        long = np.median(np.stack([null.windows(f'b{i}', 40, 45)['W'] for i in range(5)], axis=1), axis=1)
        a, b = exp12.null_summary(2.0, short), exp12.null_summary(4.0, long)
        self.assertEqual(float(np.percentile(short - long, 95)), -2.0)
        self.assertEqual(exp12.centred_spread(a['centred'], b['centred']), 0.0)
        # A spread that does differ comes through: a's centred null wider than b's.
        wide = exp12.null_summary(0.0, np.concatenate([np.zeros(900), np.full(100, 0.3)]))
        narrow = exp12.null_summary(0.0, np.zeros(1000))
        self.assertAlmostEqual(exp12.centred_spread(wide['centred'], narrow['centred']), 0.3)
        self.assertIsNone(exp12.centred_spread(None, narrow['centred']))


# --- the rules, on cells given as a table ----------------------------------------------------------

FULL = {'W_net': 0.0, 'D_net': 0.0, 'W_measurable': False, 'D_measurable': False, 'W_bound': 0.02, 'D_bound': 0.01}


def busy_table(**changes):
    """The busy block as E8 and E9 expect it above 50% busy: full not measurable, staged pgdelta and the capture well
    above it, v1proxy missing on D, its one-core arm meeting; the warm block as cold. `changes` maps a key to fields
    (or None to drop the cell)."""
    table = {('busy', '0.5g', 'full'): {**FULL, 'total_s': 40.0}, ('busy', '2g', 'full'): {**FULL, 'total_s': 10.8},
             ('busy', U, 'full'): {**FULL, 'total_s': 4.3},
             ('busy', '2g', 'capture'): {'W_net': 0.5, 'D_net': 0.45, 'W_measurable': True, 'D_measurable': True},
             ('warm', '2g', 'full'): dict(FULL), ('warm', '2g', PROXY): {'W_net': 0.03, 'D_net': 0.10,
                                                                        'D_measurable': True}}
    for speed in exp12.SPEEDS:
        table[('busy', speed, 'pgdelta')] = {'W_net': 0.6, 'D_net': 0.4, 'W_measurable': True, 'D_measurable': True,
                                             'total_s': 9.5}
        table[('busy', speed, PROXY)] = {'W_net': 0.03, 'D_net': 0.10, 'D_measurable': True, 'total_s': 3.8}
    for speed in exp12.REGISTERED:
        table[('busy', speed, PROXY_S1)] = {'W_net': 0.01, 'D_net': 0.01, 'total_s': 5.5}
    table[('busy', None, 'burn4')] = {'W_net': 0.05, 'D_net': 0.02, 'n': 2, 'W_measurable': True}
    for key, fields in changes.items():
        if fields is None:
            table.pop(key, None)
        else:
            table[key] = {**table.get(key, {}), **fields}
    return table


def context(**changes):
    ctx = {'blocked': {}, 'a4': {PROXY: 'pass', PROXY_S1: 'pass', 'warm': 'pass'}, 'reclassified': {}, 'r3': False,
           'cpu': {'pgdelta': 10.0, 'full': 2.5, PROXY: 6.5, PROXY_S1: 6.2, V1: 6.4, 'burn4': 8.1, 'capture': 4.5}}
    for key, value in changes.items():
        ctx[key] = {**ctx[key], **value} if isinstance(value, dict) and key in ('a4', 'cpu') else value
    return ctx


def apply(base, changes):
    for key, fields in changes.items():
        if fields is None:
            base.pop(key, None)
        else:
            base[key] = {**base.get(key, {}), **fields}
    return base


def cells(changes=None, spread=0.0):
    return exp12.TableCells(apply(busy_table(), changes or {}), spread=spread)


@needs_venv
class LedgerRules(unittest.TestCase):
    def test_r1_fires_where_pgdelta_wins_on_time_and_loses_more(self):
        found = exp12.r1(cells(), context())
        self.assertEqual((found['verdict'], found['speeds'], found['higher']), (True, ['0.5g', '2g'], ['0.5g', '2g']))
        # No higher than full within δ_W: it doesn't fire.
        same = {('busy', s, 'pgdelta'): {'W_net': 0.04} for s in exp12.REGISTERED}
        self.assertIs(exp12.r1(cells(same), context())['verdict'], False)
        # Where pgdelta is slower than full it doesn't win, and R1 looks only where it wins: at 0.5 Gbit/s here.
        slow = {('busy', '2g', 'pgdelta'): {'total_s': 12.0, 'W_net': 0.6}, ('busy', '0.5g', 'pgdelta'): {'W_net': 0.0}}
        found = exp12.r1(cells(slow), context())
        self.assertEqual((found['verdict'], found['speeds']), (False, ['0.5g']))
        # Slower at both: not judged.
        slower = {('busy', s, 'pgdelta'): {'total_s': 50.0} for s in exp12.REGISTERED}
        self.assertIsNone(exp12.r1(cells(slower), context())['verdict'])

    def test_r1_with_a13_and_short_cells(self):
        # pgdelta not durable at 2 Gbit/s: that speed doesn't count; full not durable: every speed is a win.
        found = exp12.r1(cells(), context(reclassified={'pgdelta': {'2g'}}))
        self.assertEqual(found['speeds'], ['0.5g'])
        slower = {('busy', s, 'pgdelta'): {'total_s': 50.0} for s in exp12.REGISTERED}
        self.assertEqual(exp12.r1(cells(slower), context(reclassified={'full': True}))['speeds'], ['0.5g', '2g'])
        # A short cell (fewer than 3 runs) leaves R1 not judged there.
        found = exp12.r1(cells({('busy', '0.5g', 'full'): {'n': 2}}), context())
        self.assertEqual(found['speeds'], ['2g'])
        both = {('busy', s, 'pgdelta'): {'n': 2} for s in exp12.REGISTERED}
        self.assertIsNone(exp12.r1(cells(both), context())['verdict'])
        # A2, A3 or the clock gate: not judged.
        self.assertIsNone(exp12.r1(cells(), context(blocked={'busy': 'not judged: instrument (A2)'}))['verdict'])

    def test_r2_every_case(self):
        # Exact: both W's measurable, the rate ratio (W_net / CPU-s) within [1/1.5, 1.5] tracks.
        measured = {('busy', s, 'full'): {'W_net': 0.1, 'W_measurable': True} for s in exp12.REGISTERED}
        tracks = {**measured, **{('busy', s, 'pgdelta'): {'W_net': 0.45} for s in exp12.REGISTERED}}
        found = exp12.r2(cells(tracks), context())  # (0.45 / 10) / (0.1 / 2.5) = 1.125
        self.assertIs(found['verdict'], True)
        self.assertAlmostEqual(found['cells']['2g'][1], 1.125)
        far = {**measured, **{('busy', s, 'pgdelta'): {'W_net': 0.7} for s in exp12.REGISTERED}}  # 1.75
        self.assertIs(exp12.r2(cells(far), context())['verdict'], False)
        low = {**measured, **{('busy', s, 'pgdelta'): {'W_net': 0.2} for s in exp12.REGISTERED}}  # 0.5 < 1/1.5
        self.assertIs(exp12.r2(cells(low), context())['verdict'], False)
        # Only pgdelta's measurable: full's W_net is at most its bound, so the ratio is at least
        # (0.6 / 10) / (0.02 / 2.5) = 7.5 > 1.5: doesn't track.
        found = exp12.r2(cells(), context())
        self.assertEqual(found['cells']['2g'][:2], ('lower', 7.5))
        self.assertIs(found['verdict'], False)
        # A lower bound that doesn't pass 1.5 decides nothing.
        wide = {('busy', s, 'full'): {'W_bound': 0.2} for s in exp12.REGISTERED}
        self.assertIsNone(exp12.r2(cells(wide), context())['verdict'])
        # Only full's measurable: the ratio is at most (bound_p / CPU_p) / (W_f / CPU_f), here 0.25 < 1/1.5.
        only_full = {**{('busy', s, 'full'): {'W_net': 0.4, 'W_measurable': True} for s in exp12.REGISTERED},
                     **{('busy', s, 'pgdelta'): {'W_net': 0.0, 'W_measurable': False, 'W_bound': 0.4}
                        for s in exp12.REGISTERED}}
        found = exp12.r2(cells(only_full), context())
        self.assertEqual(found['cells']['2g'][:2], ('upper', 0.25))
        self.assertIs(found['verdict'], False)
        # Neither measurable: not judged; one cell tracking and the other not judged: not judged overall.
        neither = {('busy', s, 'pgdelta'): {'W_net': 0.0, 'W_measurable': False} for s in exp12.REGISTERED}
        self.assertIsNone(exp12.r2(cells(neither), context())['verdict'])
        mixed = {**tracks, ('busy', '0.5g', 'full'): {'W_measurable': False, 'W_bound': 0.5}}
        self.assertIsNone(exp12.r2(cells(mixed), context())['verdict'])
        # A short cell, or no calibrated CPU-s: not judged there.
        self.assertIsNone(exp12.r2(cells({**tracks, ('busy', '2g', 'full'): {'n': 2}}), context())['verdict'])
        self.assertIsNone(exp12.r2(cells(tracks), context(cpu={'pgdelta': None}))['verdict'])

    def test_l2_l3_and_l4(self):
        self.assertEqual(exp12.l2(context()), (4.0, True))
        self.assertEqual(exp12.l2(context(cpu={'pgdelta': 15.0}))[1], False)
        # L3: full not measurable, pgdelta's lower bound 0.6 / 0.02 = 30 > 4: failed high.
        found = exp12.l3(cells(), context())
        self.assertIs(found['verdict'], False)
        self.assertIn('failed high', found['text'])
        exact = {('busy', '2g', 'full'): {'W_net': 0.2, 'W_measurable': True}}
        self.assertIs(exp12.l3(cells(exact), context())['verdict'], True)  # 0.6 / 0.2 = 3
        self.assertIn('failed low', exp12.l3(cells({('busy', '2g', 'full'): {'W_net': 0.5, 'W_measurable': True}}),
                                             context())['text'])
        upper = {('busy', '2g', 'full'): {'W_net': 0.5, 'W_measurable': True},
                 ('busy', '2g', 'pgdelta'): {'W_measurable': False, 'W_bound': 0.5}}
        self.assertIn('failed low', exp12.l3(cells(upper), context())['text'])  # at most 1
        loose = {('busy', '2g', 'full'): {'W_bound': 0.3}}
        self.assertIsNone(exp12.l3(cells(loose), context())['verdict'])  # at least 2: decides nothing
        # L4: exact at both, higher at 0.5 holds; the lower bound at 0.5 against the exact ratio at 2 holds or is not
        # judged; anything else not judged.
        both = {('busy', s, 'full'): {'W_net': 0.2, 'W_measurable': True} for s in exp12.REGISTERED}
        both[('busy', '0.5g', 'pgdelta')] = {'W_net': 0.8}
        self.assertIs(exp12.l4(cells(both), context())['verdict'], True)
        both[('busy', '0.5g', 'pgdelta')] = {'W_net': 0.4}
        self.assertIs(exp12.l4(cells(both), context())['verdict'], False)
        bound = {('busy', '2g', 'full'): {'W_net': 0.2, 'W_measurable': True}}  # 3 at 2; at least 30 at 0.5
        self.assertIs(exp12.l4(cells(bound), context())['verdict'], True)
        self.assertIsNone(exp12.l4(cells(), context())['verdict'])

    def test_a5_a6_and_the_ha_caveat(self):
        found = exp12.a5(cells(), context())
        self.assertIs(found['verdict'], True)  # 0.5 of 0.6
        self.assertIs(exp12.a5(cells({('busy', '2g', 'capture'): {'W_net': 0.2}}), context())['verdict'], False)
        self.assertIsNone(exp12.a5(cells({('busy', s, 'pgdelta'): {'W_measurable': False} for s in exp12.SPEEDS}),
                                   context())['verdict'])
        # A6: v1proxy's W measurable at 2 Gbit/s: 0.13 / 6.5 = 0.02 per CPU-s; v1proxy_s1 at its bound 0.02 / 6.2 and
        # burn4 0.05 / 8.1, each at most half: "cores".
        measurable = {('busy', '2g', PROXY): {'W_net': 0.13, 'W_measurable': True}}
        self.assertIs(exp12.a6(cells(measurable), context())['verdict'], True)
        hot_burn = {**measurable, ('busy', None, 'burn4'): {'W_net': 0.2}}
        self.assertIs(exp12.a6(cells(hot_burn), context())['verdict'], False)
        self.assertIsNone(exp12.a6(cells(), context())['verdict'])  # v1proxy's W measurable nowhere
        self.assertEqual(exp12.ha_caveat(cells(), context())[0], True)
        self.assertEqual(exp12.ha_caveat(cells({('busy', U, PROXY): {'D_net': 0.0}}), context())[0], False)
        self.assertIsNone(exp12.ha_caveat(cells({('busy', U, PROXY): {'n': 2}}), context())[0])


@needs_venv
class DecisionOne(unittest.TestCase):
    def row(self, changes=None, **ctx):
        c = cells(changes)
        return exp12.decision1(c, context(**ctx))

    def test_rows_1_to_3(self):
        self.assertEqual(self.row(r3=True)['row'], '1. Stop')
        self.assertEqual(self.row()['row'], '2. R1')
        # R1 doesn't fire (pgdelta slower everywhere: not judged) and R2 tracks: row 3.
        tracks = {**{('busy', s, 'full'): {'W_net': 0.1, 'W_measurable': True} for s in exp12.REGISTERED},
                  **{('busy', s, 'pgdelta'): {'W_net': 0.45, 'total_s': 50.0} for s in exp12.REGISTERED}}
        self.assertEqual(self.row(tracks)['row'], '3. R2')
        # Neither: the proxy decides (row 7 here, its cause from the warm block).
        neither = {('busy', s, 'pgdelta'): {'total_s': 50.0} for s in exp12.REGISTERED}
        found = self.row(neither)
        self.assertEqual((found['row'], found['cause']), ('7. v1 misses', '7a'))
        # Row 2 stands and the proxy is beside it.
        self.assertEqual(self.row()['proxy']['row'], '7. v1 misses')

    def neither(self, extra=None):
        return {**{('busy', s, 'pgdelta'): {'total_s': 50.0} for s in exp12.REGISTERED}, **(extra or {})}

    def test_row_4_proxy_not_judged(self):
        for name, ctx in (('A2', {'blocked': {'busy': 'not judged: instrument (A2)'}}),
                          ('A3', {'blocked': {'busy': 'not judged: overload (A3)'}}),
                          ('clock', {'blocked': {'busy': 'not judged: clock'}}),
                          ('A4', {'a4': {PROXY: 'fail'}})):
            with self.subTest(name):
                self.assertEqual(self.row(self.neither(), **ctx)['row'], '4. Proxy not judged')
        # A short v1proxy (or full) cell and no judged miss elsewhere: row 4.
        meets = {('busy', s, PROXY): {'D_net': 0.0} for s in exp12.REGISTERED}
        short = self.neither({**meets, ('busy', '2g', PROXY): {'D_net': 0.0, 'n': 2}})
        self.assertEqual(self.row(short)['row'], '4. Proxy not judged')
        short_full = self.neither({**meets, ('busy', '0.5g', 'full'): {'n': 1}})
        self.assertEqual(self.row(short_full)['row'], '4. Proxy not judged')
        # A judged miss counts before the short cell: row 7.
        missed = self.neither({('busy', '2g', PROXY): {'n': 2}})
        self.assertEqual(self.row(missed)['row'], '7. v1 misses')
        # A4 undecided: a judged miss still counts, a meet doesn't.
        self.assertEqual(self.row(self.neither(), a4={PROXY: 'undecided'})['row'], '7. v1 misses')
        self.assertEqual(self.row(self.neither(meets), a4={PROXY: 'undecided'})['row'], '4. Proxy not judged')

    def test_rows_5_and_6(self):
        quiet = {('busy', s, PROXY): {'D_net': 0.0, 'D_measurable': False} for s in exp12.REGISTERED}
        self.assertEqual(self.row(self.neither(quiet))['row'], '5. No measurable slowdown')
        # Row 5 is checked before row 7: a difference beyond δ that no null shows is no measurable slowdown.
        unseen = {('busy', s, PROXY): {'D_net': 0.05, 'D_measurable': False} for s in exp12.REGISTERED}
        self.assertEqual(self.row(self.neither(unseen))['row'], '5. No measurable slowdown')
        self.assertEqual(exp12.decision2(cells(self.neither(unseen)), context())['row'], 'No cap')
        # Measurable somewhere (full unthrottled doesn't count, the registered speeds do), but meeting: row 6.
        measurable = {('busy', s, PROXY): {'D_net': 0.02, 'D_measurable': True} for s in exp12.REGISTERED}
        self.assertEqual(self.row(self.neither(measurable))['row'], '6. v1 meets')
        full_measurable = {**quiet, ('busy', '2g', 'full'): {'W_measurable': True}}
        self.assertEqual(self.row(self.neither(full_measurable))['row'], '6. v1 meets')

    def test_row_7_and_its_cause(self):
        # 7a: warm v1proxy misses against warm full too.
        self.assertEqual(self.row(self.neither())['cause'], '7a')
        # 7b: warm meets, and on D (the number missed cold) its warm net value is below its cold one at 2 Gbit/s by
        # more than δ: 0.0 against 0.10.
        positive = self.neither({('warm', '2g', PROXY): {'D_net': 0.0}})
        found = self.row(positive)
        self.assertEqual((found['row'], found['cause']), ('7. v1 misses', '7b'))
        # 7c: warm meets without falling below its cold value by more than δ (cold 0.05 against full's 0.0, a miss;
        # warm 0.05 against warm full's 0.03, a meet; warm less cold 0).
        level = self.neither({('busy', s, PROXY): {'D_net': 0.05} for s in exp12.REGISTERED})
        level.update({('warm', '2g', 'full'): {'D_net': 0.03}, ('warm', '2g', PROXY): {'D_net': 0.05}})
        found = self.row(level)
        self.assertEqual((found['row'], found['cause']), ('7. v1 misses', '7c'))
        # Below by more than δ, but the warm reading itself misses: 7a comes first.
        self.assertEqual(self.row(self.neither({('warm', '2g', PROXY): {'D_net': 0.05}}))['cause'], '7a')
        # 7c: the warm reading can't be judged: a short warm cell, or A4 on the warm runs.
        self.assertEqual(self.row(self.neither({('warm', '2g', PROXY): {'n': 2}}))['cause'], '7c')
        self.assertEqual(self.row(positive, a4={'warm': 'fail'})['cause'], '7c')
        self.assertEqual(self.row(positive, a4={'warm': 'undecided'})['cause'], '7c')
        # On W and D both missed cold, 7b needs both below.
        both = self.neither({('busy', s, PROXY): {'W_net': 0.08, 'D_net': 0.1} for s in exp12.REGISTERED})
        half = {**both, ('warm', '2g', PROXY): {'W_net': 0.05, 'D_net': 0.0}}  # W only 0.03 below: within δ_W
        found = self.row(half)
        self.assertEqual((found['proxy']['roles'], found['cause']), (['W', 'D'], '7c'))
        full = {**both, ('warm', '2g', PROXY): {'W_net': 0.0, 'D_net': 0.0}}
        self.assertEqual(self.row(full)['cause'], '7b')

    def test_the_checks_example_now_gives_7c(self):
        # δ_D 0.03: cold 0.035 at 0.5 Gbit/s (a miss), 0.025 at 2 (meets), 0.025 again warm (meets): no positive
        # evidence, so not 7b.
        example = self.neither({('busy', '0.5g', PROXY): {'D_net': 0.035}, ('busy', '2g', PROXY): {'D_net': 0.025},
                                ('warm', '2g', PROXY): {'D_net': 0.025}})
        found = self.row(example)
        self.assertEqual((found['row'], found['cause']), ('7. v1 misses', '7c'))
        self.assertEqual(found['proxy']['missed'], {'0.5g': ['D']})


@needs_venv
class DecisionTwo(unittest.TestCase):
    def row(self, changes=None, **ctx):
        c = cells(changes)
        ctx = context(**ctx)
        return exp12.decision2(c, ctx)['row']

    def test_its_rows_in_order(self):
        self.assertEqual(self.row(), 'Cap meets')
        meets = {('busy', s, PROXY): {'D_net': 0.0} for s in exp12.REGISTERED}
        self.assertEqual(self.row(meets), 'No cap')
        quiet = {('busy', s, PROXY): {'D_net': 0.0, 'D_measurable': False} for s in exp12.REGISTERED}
        self.assertEqual(self.row(quiet), 'No cap')  # row 5's condition
        misses = {('busy', s, PROXY_S1): {'D_net': 0.05} for s in exp12.REGISTERED}
        self.assertEqual(self.row(misses), 'Cap fails')
        # A judged miss counts first: missing at 0.5 and short at 2 is "Cap fails", not "Cap not judged".
        first = {('busy', '0.5g', PROXY_S1): {'D_net': 0.05}, ('busy', '2g', PROXY_S1): {'n': 2}}
        self.assertEqual(self.row(first), 'Cap fails')
        self.assertEqual(self.row({('busy', '2g', PROXY_S1): {'n': 2}}), 'Cap not judged')
        self.assertEqual(self.row(a4={PROXY_S1: 'fail'}), 'Cap not judged')
        self.assertEqual(self.row(misses, a4={PROXY_S1: 'fail'}), 'Cap not judged')
        self.assertEqual(self.row(a4={PROXY_S1: 'undecided'}), 'Cap not judged')
        self.assertEqual(self.row(misses, a4={PROXY_S1: 'undecided'}), 'Cap fails')
        # Not judged where the proxy isn't (row 4), whichever row decides decision 1.
        self.assertIsNone(self.row(a4={PROXY: 'fail'}))
        self.assertIsNone(self.row(blocked={'busy': 'not judged: clock'}))


@needs_venv
class Flags(unittest.TestCase):
    def test_near_the_line_flags_a_deciding_difference_only(self):
        # v1proxy misses only at 2 Gbit/s, on D by 0.032 against δ 0.03: crossing it gives row 6, so it is near.
        changes = {('busy', '0.5g', PROXY): {'D_net': 0.0}, ('busy', '2g', PROXY): {'D_net': 0.032},
                   **{('busy', s, 'pgdelta'): {'total_s': 50.0} for s in exp12.REGISTERED}}
        c, ctx = cells(changes), context()
        base = exp12.outcome(c, ctx)
        self.assertEqual(base[0], '7. v1 misses')
        near = exp12.near_line(c, ctx, base)
        self.assertEqual([(kind, key[0], key[2]) for kind, key, *_ in near], [('diff', ('busy', '2g', PROXY), 'D')])
        # Missing at both speeds, one of them near the line: crossing it alone changes nothing.
        changes[('busy', '0.5g', PROXY)] = {'D_net': 0.2}
        c = cells(changes)
        base = exp12.outcome(c, ctx)
        self.assertEqual(exp12.near_line(c, ctx, base), [])
        # A rate ratio within ±0.15 of 1.5 that decides R2 (and so row 3) is near.
        tracks = {**{('busy', s, 'full'): {'W_net': 0.1, 'W_measurable': True} for s in exp12.REGISTERED},
                  **{('busy', s, 'pgdelta'): {'W_net': 0.58, 'total_s': 50.0} for s in exp12.REGISTERED}}
        c = cells(tracks)
        base = exp12.outcome(c, ctx)  # (0.58 / 10) / (0.1 / 2.5) = 1.45
        self.assertEqual(base[0], '3. R2')
        ratios = [key for kind, key, *_ in exp12.near_line(c, ctx, base) if kind == 'ratio']
        self.assertEqual(sorted(ratios), ['0.5g', '2g'])

    def test_fragile_when_a_view_changes_a_row(self):
        class Views:
            def __init__(self, tables):
                self.tables = tables

            def cells(self, view):
                return exp12.TableCells(self.tables.get(view, busy_table()))
        ctx = context()
        base = exp12.outcome(cells(), ctx)
        steady = Views({})
        self.assertEqual(exp12.fragile(steady, ctx, base), [])
        shaky = Views({'max': apply(busy_table(), {('busy', s, PROXY_S1): {'D_net': 0.05} for s in exp12.REGISTERED})})
        self.assertEqual([(view, found[2]) for view, found in exp12.fragile(shaky, ctx, base)], [('max', 'Cap fails')])

    def test_short_cells_and_the_controls_minimum(self):
        c = cells({('busy', '2g', PROXY): {'n': 2}, ('busy', None, 'burn4'): {'n': 2}})
        self.assertFalse(c.judged('busy', '2g', PROXY))
        self.assertTrue(c.judged('busy', None, 'burn4'))  # the controls run twice per block by design
        self.assertFalse(cells({('busy', None, 'burn4'): {'n': 1}}).judged('busy', None, 'burn4'))


# --- the context: A2, A3, the clock gate, A4, A13 and R3, on a session without logs ----------------------------

def record(method, block='busy', speed='2g', repeat=0, status='pass', **fields):
    out = {'trial_id': 't', 'method': method, 'block': block, 'repeat': repeat, 'block_repeat': 0,
           'speed': {'label': speed}, 'status': status, 'load_rate': 1000 if block != 'calibration' else None,
           'load_run': 'busy', 'clock_offset_s': 0.0,
           'window': {'t0_utc': T0, 't1_utc': T0 + 2.1, 'clock_offset_s': 0.0, 't0_source_utc': T0,
                      't1_source_utc': T0 + 2.1}}
    out.update(fields)
    return out


def session(records, trial=None, **kwargs):
    runs = [exp12.make_run('s', index, item) for index, item in enumerate(records)]
    trial = trial or {'trial_id': 't', 'load': {'capacity': {'capacity_tps': 2000}},
                      'clock': {'start': {'offset_s': 0.001}, 'end': {'offset_s': 0.001}}}
    with tempfile.TemporaryDirectory() as tmp:
        return exp12.Session(runs, {('s', 't'): trial}, [], sweeps=[Path(tmp) / 's'], resamples=200, **kwargs)


@needs_venv
class Context(unittest.TestCase):
    def pause(self, W=1.8, D=1.0, gap=(T0 + 0.05, T0 + 2.05), stop=T0 + 0.0499, cont=T0 + 2.05, p99=1.0,
              block='busy'):
        S = session([record('pause2', block=block, control_slot='busy_start', phases={measure.CONTROL_PHASE: {
            'control': {'sigstop_utc': [stop, stop + 0.0001], 'sigcont_utc': [cont, cont + 0.0001]}}})])
        S.runs[0]['stats'] = {'W': W, 'D': D, 'gap_utc': list(gap), 'late_L': 3.0}
        S.runs[0]['settle'] = {'p99_ms': p99}
        return exp12.a2_runs(S)[0]

    def test_a2_the_instrument(self):
        item = self.pause()
        self.assertEqual((item['ok'], item['rho']), (True, 0.5))
        self.assertAlmostEqual(item['low'], 1.3)
        self.assertIs(self.pause(W=1.2)['ok'], False)  # below 1.8 - (1 - ρ)
        self.assertIs(self.pause(W=2.4)['ok'], False)
        self.assertIs(self.pause(D=0.85)['ok'], False)
        # The gap against the agent's signals (each a [before, after] pair): 5 ms plus the settle's p99 (1 ms here)
        # at either end.
        self.assertIs(self.pause(stop=T0 + 0.0442)['ok'], True)  # 5.7 ms before the gap
        self.assertIs(self.pause(stop=T0 + 0.0435)['ok'], False)  # 6.4 ms
        self.assertIs(self.pause(cont=T0 + 2.0558)['ok'], True)
        self.assertIs(self.pause(cont=T0 + 2.0565)['ok'], False)
        self.assertIs(self.pause(p99=2.0, stop=T0 + 0.0435)['ok'], True)  # a slower settle widens the margin
        # Inside dest's phase mapped through the offset, within the same margin.
        self.assertIs(self.pause(gap=(T0 - 0.01, T0 + 1.99), stop=T0 - 0.0101, cont=T0 + 1.99)['ok'], False)

    def test_a2_blocks_its_blocks_loss_verdicts(self):
        S = session([record('pause2', control_slot='busy_start')])
        S.runs[0]['stats'] = {'W': 1.0, 'D': 1.0, 'gap_utc': None, 'late_L': 3.0}
        ctx = exp12.context(S)
        self.assertEqual(ctx['blocked']['busy'], 'not judged: instrument (A2)')
        self.assertEqual(ctx['blocked']['warm'], 'not judged: instrument (A2)')  # it shares the busy block's
        self.assertIsNone(ctx['blocked']['moderate'])

    def test_a3_overload(self):
        S = session([record('none', repeat=i) for i in range(3)])
        for r, completed in zip(S.runs, (0.97, 0.97, 1.0)):
            r['stats'] = {'completed_ratio': completed, 'backlog_end': 10}
        self.assertTrue(exp12.a3_blocks(S)['busy']['fires'])
        for r in S.runs:
            r['stats'] = {'completed_ratio': 1.0, 'backlog_end': 10}
        self.assertFalse(exp12.a3_blocks(S)['busy']['fires'])
        S.runs[0]['stats']['backlog_end'] = 1001  # above R x 1 s
        self.assertTrue(exp12.a3_blocks(S)['busy']['fires'])

    def test_the_clock_gate(self):
        self.assertFalse(exp12.clock_gate(session([]))[('s', 't')]['fires'])
        late = {'trial_id': 't', 'clock': {'start': {'offset_s': 0.001}, 'end': {'offset_s': -0.006}}}
        self.assertTrue(exp12.clock_gate(session([], late))[('s', 't')]['fires'])
        self.assertTrue(exp12.clock_gate(session([], {'trial_id': 't'}))[('s', 't')]['fires'])  # not measured
        self.assertEqual(exp12.context(session([], late))['blocked']['busy'], 'not judged: clock')

    def proxies(self, changes=None):
        held = {'backup': {'held': True}, 'hashed': {'all_hashed': True}}
        records = [record(PROXY, proxy=held), record(PROXY_S1, proxy=held), record(PROXY, block='warm', proxy=held)]
        records += [record(arm, block='calibration', repeat=9, proxy=held) for arm in (PROXY, PROXY_S1)]
        records += [record(V1, block='calibration', repeat=9)]
        S = session(records)
        changes = dict(changes or {})
        cpu = {PROXY: 6.5, PROXY_S1: 6.2, V1: 6.4, **changes.pop('cpu', {})}
        for r in S.runs:
            if r['block'] == 'calibration':
                r['cpu_calibrated'], r['counts'] = cpu[r['method']], True
        for (arm, block), proxy in changes.items():
            next(r for r in S.runs if r['method'] == arm and r['block'] == block)['record']['proxy'] = proxy
        return exp12.a4_state(S)

    def test_a4_the_proxys_gate(self):
        state, why, reference = self.proxies()
        self.assertEqual(state, {PROXY: 'pass', PROXY_S1: 'pass', 'warm': 'pass'})
        self.assertAlmostEqual(reference[PROXY_S1], 6.5 / 1.06)
        # (i) not every byte hashed, (ii) backup mode not held.
        state, why, _ = self.proxies({(PROXY, 'busy'): {'backup': {'held': True}, 'hashed': {'all_hashed': False}}})
        self.assertEqual(state[PROXY], 'fail')
        self.assertIn('(i)', why[PROXY][0])
        state, _, _ = self.proxies({(PROXY_S1, 'busy'): {'backup': {'held': False}, 'hashed': {'all_hashed': True}}})
        self.assertEqual((state[PROXY], state[PROXY_S1]), ('pass', 'fail'))
        # The warm runs are gated by (i) and (ii) only.
        state, _, _ = self.proxies({(PROXY, 'warm'): {'backup': {'held': False}, 'hashed': {'all_hashed': True}}})
        self.assertEqual((state[PROXY], state['warm']), ('pass', 'fail'))
        # (iii): v1proxy within 20% of v1 itself; v1proxy_s1 within 20% of v1proxy / 1.06.
        self.assertEqual(self.proxies({'cpu': {PROXY: 8.0}})[0][PROXY], 'fail')
        self.assertEqual(self.proxies({'cpu': {PROXY_S1: 4.5}})[0][PROXY_S1], 'fail')
        # A counter missing: undecided.
        state, why, _ = self.proxies({(PROXY, 'busy'): {'backup': {'held': True}, 'hashed': None}})
        self.assertEqual(state[PROXY], 'undecided')

    def test_a13_and_r3(self):
        failing = {'a_ok': False, 'b_ok': True, 'a_required': True}
        records = [record('full', repeat=i, durability=failing) for i in range(2)]
        records += [record('pgdelta', speed='0.5g', repeat=i, durability=failing) for i in range(2)]
        records += [record('pgdelta', speed='2g', repeat=5, durability=failing)]
        S = session(records)
        reclassified, notes = exp12.a13_state(S)
        self.assertEqual(reclassified, {'full': True, 'pgdelta': {'0.5g'}})
        self.assertEqual(len(notes), 2)
        self.assertEqual(S.runs[0]['left_out'], 'durability failed: a')
        # R3: one wrong output of pgdelta, the proxies or v1 itself; not full's (A1's), not an unverified one.
        S = session([record('full', status='fail'), record(PROXY, status='unverified')])
        self.assertFalse(exp12.context(S)['r3'])
        S = session([record(V1, block='calibration', status='fail')])
        self.assertTrue(exp12.context(S)['r3'])


def sampler_ticks(pid=7):
    """0.1-s ticks from t0 - 30 to t0 + 30: steal 0.05 CPU-s per s inside [t0, t0 + 10], none outside; two pgbench
    threads each running 0.95 s per s and waiting 0.01 s per s, 0.1 more in the window, 0.4 more in [t0 + 3, t0 + 4]."""
    ticks, steal, run, wait = [], 0.0, 0.0, 0.0
    for index in range(601):
        t = T0 - 30 + index / 10
        if index:
            inside = T0 < t <= T0 + 10
            steal += 0.05 * 0.1 * exp12.USER_HZ if inside else 0
            run += 0.95e8
            wait += (0.01 + (0.1 if inside else 0) + (0.4 if T0 + 3 < t <= T0 + 4 else 0)) * 1e8
        ticks.append({'t': t, 'cpu': [0, 0, 0, 0, 0, 0, 0, int(round(steal))], 'g': {}, 'pgbench': pid,
                      'th': {'7': [run, wait], '8': [run, wait]}})
    return ticks


@needs_venv
class SamplerAndGate(unittest.TestCase):
    def run_of(self, method='full'):
        return {'t0': T0, 'stats': {'T': 10, 'worst_bucket': 3}, 'settle_span': (T0 - 20, T0 - 5), 'tail_end': T0 + 25,
                'load_run': 'busy', 'method': method}

    def test_the_samplers_view_of_a_run(self):
        sql = [{'t': T0 - 30 + i, 'wal': {'wal_sync_time': i * 0.5},
                'checkpointer': {'num_timed': 0, 'num_requested': (T0 - 30 + i > T0 + 0.5) + (T0 - 30 + i > T0 + 5)},
                'autovacuum': [{'pid': 1, 'datname': 'loaddb'}] if i == 32 else []} for i in range(61)]
        sampler = exp12.Sampler(sampler_ticks(), sql)
        trial = {'load': {'runs': [{'run_id': 'busy', 'pid': 7}]}}
        view = sampler.view(self.run_of(), trial)
        self.assertAlmostEqual(view['steal_rate'], 0.05, places=3)
        self.assertTrue(view['steal'])  # above 0.02 CPU-s per s
        self.assertAlmostEqual(view['client_core_max'], 0.95, places=3)
        self.assertTrue(view['client'])  # a pgbench thread above 90% of a core in the settle
        # The threads' added wait: 0.1 x 10 + 0.4 in the window; 0.1 + 0.4 in the worst bucket (t0 + 3).
        self.assertAlmostEqual(view['client_wait_s'], 1.4, places=3)
        self.assertAlmostEqual(view['client_wait_worst_bucket_s'], 0.5, places=3)
        # Two checkpoints requested between the settle's start and the tail's end: full asked for one.
        self.assertEqual(view['checkpoints'], {'timed': 0, 'requested': 2, 'own': 1})
        self.assertTrue(view['checkpoint'])
        self.assertTrue(view['checkpoint'] and sampler.view(self.run_of('none'), trial)['checkpoint'])
        self.assertTrue(view['autovacuum'] and view['autovacuum_loaddb'])
        # Another load run's pgbench: no threads, no client readings.
        self.assertNotIn('client_wait_s', sampler.view(self.run_of(), {'load': {'runs': [{'run_id': 'busy', 'pid': 9}]}}))

    def test_the_client_wait_flags_need_a_measurable_cell(self):
        S = session([record('v1proxy', repeat=i) for i in range(3)])
        for r in S.runs:
            r['stats'] = {'W': 0.4, 'worst_bucket_lost_s': 0.2}
            r['sampler'] = {'client_wait_s': 0.1, 'client_wait_worst_bucket_s': 0.04}
        measurable = {'W_measurable': True, 'D_measurable': True}
        with mock.patch.object(exp12.Cells, 'cell', return_value=measurable):
            S.client_wait_flags()
        self.assertEqual({(r['flags']['client_wait_w'], r['flags']['client_wait_d']) for r in S.runs}, {(True, False)})
        with mock.patch.object(exp12.Cells, 'cell', return_value={'W_measurable': False, 'D_measurable': True}):
            S.client_wait_flags()
        self.assertEqual({r['flags']['client_wait_w'] for r in S.runs}, {None})

    def test_the_settle_gates_log_part(self):
        gate = {'passed': True, 'unsettled': False}
        S = session([record('full', settle={'gate': gate, 'unsettled': False})])
        r = S.runs[0]
        r['settle'] = {'p99_ms': 3.0, 'late_share': 0.0005, 'warmup': {'pool': {'p99_ms': 2.0}}}
        self.assertTrue(S.gate_log_ok(r) and S.settled(r))
        r['settle']['p99_ms'] = 4.5  # more than twice the warm-up's
        self.assertFalse(S.settled(r))
        r['settle'].update(p99_ms=3.0, late_share=0.002)  # more than 0.1% later than L
        self.assertFalse(S.settled(r))
        r['settle']['warmup'] = None  # its p99 part can't be judged; the late part still can
        self.assertFalse(S.gate_log_ok(r))
        r['settle']['late_share'] = 0.0
        self.assertTrue(S.settled(r))
        r['record']['settle']['gate'] = {'passed': False}  # the live part, capped at 60 s
        self.assertFalse(S.settled(r))

    def test_the_clock_mapping(self):
        # Dest's settle and tail are mapped to the source's clock with the run's offset (source minus dest); a window
        # without its source times is mapped the same way.
        item = record('full', clock_offset_s=-0.25, settle={'started_utc': T0 - 20, 'ended_utc': T0 - 5},
                      tail={'ended_utc': T0 + 20}, window={'t0_utc': T0, 't1_utc': T0 + 3, 'clock_offset_s': -0.25})
        run = exp12.make_run('s', 0, item)
        self.assertEqual((run['t0'], run['t1']), (T0 - 0.25, T0 + 2.75))
        self.assertEqual(run['settle_span'], (T0 - 20.25, T0 - 5.25))
        self.assertEqual(run['tail_end'], T0 + 19.75)


# --- dry runs -----------------------------------------------------------------------------------------

@needs_venv
class DryRun(unittest.TestCase):
    """The whole script on synthetic sessions, writing only into a temporary directory."""

    def run_script(self, tmp, sweeps, *extra):
        out = io.StringIO()
        figures = Path(tmp) / 'figures'
        with mock.patch.object(plotting, 'FIGURES', figures), redirect_stdout(out):
            code = exp12.main(['--sweeps', *map(str, sweeps), '--datadir', str(Path(tmp) / 'data'), '--figdir',
                               str(figures), '--null-resamples', '600', *extra])
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_the_whole_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            sweep = exp12_smoke_data.write_session(Path(tmp) / 'results')
            text = self.run_script(tmp, [sweep])
            for name in ('exp12-runs.csv', 'exp12-cells.csv'):
                self.assertTrue((Path(tmp) / 'data' / name).exists())
            for name in ('exp12-timeseries.svg', 'exp12-loss.svg'):
                self.assertTrue((Path(tmp) / 'figures' / name).exists())
                self.assertIn(str(Path(tmp) / 'figures' / name), text)
            with open(Path(tmp) / 'data' / 'exp12-runs.csv', newline='') as f:
                rows = list(csv.DictReader(f))
            with open(Path(tmp) / 'data' / 'exp12-cells.csv', newline='') as f:
                cell_rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 117)
        busy = [row for row in rows if row['block'] == 'busy' and row['method'] == 'pgdelta']
        self.assertEqual(len(busy), 15)
        # The columns the harness leaves to the analysis are filled from each record's `slowdown`.
        for column in measure.SLOWDOWN_FIELDS[:12]:
            self.assertTrue(all(row[column] != '' for row in busy), column)
        self.assertTrue(all(row['counts'] == 'True' for row in rows))
        self.assertEqual({row['view'] for row in cell_rows}, set(exp12.VIEWS))
        # Every registered and added prediction and rule, with its verdict, in the order of judgment.
        for name in ('L1', 'L2', 'L3', 'L4', 'L5', 'R1', 'R2', 'R3', *(f'E{i}' for i in range(1, 17)),
                     *(f'A{i}' for i in range(1, 14))):
            self.assertIn(f'**{name}**', text)
        order = [text.index(marker) for marker in ('**R3**', '## A1', '**A13**', '**A2** (busy block)', '**L1**',
                                                   '**R1**', '**R2**', '**Decision 1', '**Decision 2', '- **A5**',
                                                   '**HA caveat**', '*Fragile*')]
        self.assertEqual(order, sorted(order))
        self.assertIn('**Decision 1: 2. R1.**', text)
        self.assertIn('the proxy\'s reading (rows 4-7), beside it: 7. v1 misses', text)
        self.assertIn('warm reading 7a (Hashing)', text)
        self.assertIn('**Decision 2: Cap meets.**', text)
        self.assertIn('**R2** (the October 24 note; the rate ratio within [1/1.5, 1.5]): **doesn\'t track**', text)
        self.assertIn('**L3** ("its total throughput loss is 2-4 times the full copy\'s at 2 Gbit/s"): failed high', text)
        self.assertIn('**Load** (the moderate block at 2 Gbit/s): proxy 6. v1 meets', text)
        self.assertIn('**they differ**', text)
        self.assertIn('**HA caveat** (v1proxy misses O4 unthrottled, busy block): **fires**', text)
        self.assertIn('- **A2** (busy block): does not fire', text)
        self.assertIn('the coverage control (analysis only', text)
        self.assertIn('every one failed (v), as it must', text)
        # The rows rest on v1proxy's cells, which the model's client wait flags: fragile.
        self.assertIn('the rows rest on a cell flagged *client wait*', text)
        for item in range(1, 17):
            self.assertIn(f'\n{item}. **', text)

    def test_the_smoke_runs_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            sweep = exp12_smoke_data.write_session(Path(tmp) / 'results', smoke=True)
            text = self.run_script(tmp, [sweep], '--tag=-smoke', '--capacity-tps', '2400')
            self.assertTrue((Path(tmp) / 'data' / 'exp12-smoke-runs.csv').exists())
            self.assertTrue((Path(tmp) / 'figures' / 'exp12-loss-smoke.svg').exists())
        self.assertIn('**Decision 1: 4. Proxy not judged.**', text)
        self.assertIn('**Decision 2: not judged.**', text)
        self.assertIn('| no_wal | start | – | start | yes |', text)
        self.assertIn('| index_page | amcheck | atomic | amcheck | yes |', text)
        self.assertIn('every control behaved', text)
        self.assertIn('**A12** (capacity drift against the smoke run\'s C, --capacity-tps): **flagged** (-16.7%)', text)

    def test_a_wrong_pgdelta_output_stops_speed_work(self):
        def wrong(item):
            if item['method'] == 'pgdelta' and item['repeat'] == 3:
                item['status'] = 'fail'
                item['error'] = 'wrong: atomic'
                item['verification']['oracle']['atomic'] = 'wrong'
        with tempfile.TemporaryDirectory() as tmp:
            text = self.run_script(tmp, [exp12_smoke_data.write_session(Path(tmp) / 'results', adjust=wrong)])
        self.assertIn('**Decision 1: 1. Stop.**', text)
        self.assertIn('- **R3** ("Any wrong output stops speed work"', text)
        self.assertIn('FIRES', text)
        self.assertIn('**L5** ("Every output is correct"', text)
        self.assertIn('failed: 00-hot-f0.01-s70 r3 busy', text)

    def test_unsettled_settles_leave_the_null_and_come_back_in_its_view(self):
        unsettled = [('busy', 0, 'full'), ('busy', 1, 'full'), ('busy', 0, 'none')]
        with tempfile.TemporaryDirectory() as tmp:
            sweep = exp12_smoke_data.write_session(Path(tmp) / 'results', unsettled=unsettled)
            runs, trials, meta = exp12.load(sweep)
            S = exp12.Session(runs, trials, [meta], sweeps=[sweep], resamples=300)
        main, back = S.pool('busy'), S.pool('busy', 'unsettled')
        # full's settles at its 3 speeds in the busy block's first two repeats, and the first none's whole span.
        self.assertEqual(back.spans - main.spans, 7)
        self.assertGreater(back.seconds, main.seconds + 6 * 15 + 60)  # a none span holds its window too
        flagged = [r for r in S.runs if r['flags'].get('unsettled')]
        self.assertEqual(len(flagged), 7)
        self.assertTrue(all(r['counts'] for r in flagged))  # flagged runs stay in the medians
        # The robustness view draws its runs' windows from the pool with them put back.
        run = S.cells('main').cell('busy', '2g', 'full')['runs'][0]
        self.assertFalse(np.array_equal(S.null(run, 'unsettled')['W'], S.null(run)['W']))
        clean = S.cells('clean').cell('busy', '2g', 'full')
        self.assertLess(clean['n'], S.cells('main').cell('busy', '2g', 'full')['n'])

    def test_durability_failures_leave_the_medians_and_full_reclassified_wins_every_speed(self):
        def dirty(item):
            if item['method'] == 'full' and item['block'] == 'busy' and item['speed']['label'] == '0.5g' \
                    and item['block_repeat'] < 2:
                item['durability'] = {**item['durability'], 'a_ok': False, 'ok': False}
        with tempfile.TemporaryDirectory() as tmp:
            text = self.run_script(tmp, [exp12_smoke_data.write_session(Path(tmp) / 'results', adjust=dirty)])
        self.assertIn('**A13** (more than 1 run of an arm at one speed in one block fails (a) or (b)): fires: full: 2 '
                      'runs at 0.5 Gbit/s in the busy block', text)
        self.assertIn('left out of every median: durability failed 2', text)
        self.assertIn('failed; left out of every median', text)

    def test_a_dead_load_voids_the_run(self):
        def void(item):
            if item['method'] == 'v1proxy' and item['block'] == 'busy' and item['repeat'] == 2:
                item['void'] = True
                item['void_reason'] = 'the load (busy) was found dead after it'
        with tempfile.TemporaryDirectory() as tmp:
            text = self.run_script(tmp, [exp12_smoke_data.write_session(Path(tmp) / 'results', adjust=void)])
        self.assertIn('  - void: 00-hot-f0.01-s70 r2 busy 2g v1proxy: the load (busy) was found dead after it', text)
        self.assertIn('left out of every median: void 3', text)

    def test_a_missing_log_leaves_no_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            sweep = exp12_smoke_data.write_session(Path(tmp) / 'results', smoke=True)
            for path in (sweep / 'logs').rglob('moderate.*'):
                path.unlink()
            runs, trials, meta = exp12.load(sweep)
            S = exp12.Session(runs, trials, [meta], sweeps=[sweep], resamples=200)
        moderate = [r for r in S.runs if r['block'] == 'moderate']
        self.assertTrue(moderate)
        self.assertTrue(all(r['stats'] is None and r['left_out'].startswith('no numbers from the logs')
                            for r in moderate))

    def test_the_specifications_run_line(self):
        # EXP-12's "Run lines" block, split as a POSIX shell splits it (comments included), its variables substituted:
        # the analysis's line parses with every option it names.
        text = (Path(__file__).resolve().parents[1] / 'docs' / 'experiments' / 'EXP-12-source-slowdown.md').read_text()
        block = re.search(r'\*\*Run lines\*\*.*?```sh\n(.*?)```', text, re.S).group(1)
        variables, found = {}, None
        for row in block.replace('\\\n', ' ').splitlines():
            words = shlex.split(row, comments=True)
            if words and all(re.match(r'^\w+=', word) for word in words):
                variables.update(word.rstrip(';').split('=', 1) for word in words)
                continue
            words = shlex.split(re.sub(r'\$(\w+)', lambda m: variables[m.group(1)], row), comments=True)
            if words[:2] == ['.venv/bin/python', 'analysis/exp12.py']:
                found = exp12.build_parser().parse_args(words[2:])
        self.assertIsNotNone(found)
        self.assertEqual((found.sweeps, found.late_limit_ms, found.null_resamples, found.null_seed,
                          found.client_wait_share), (['results/exp04/exp12-hot'], 10.0, 10_000, 121, 0.25))

    def test_the_csv_columns_are_the_harness_columns(self):
        added = {'sweep', 'counts', 'left_out', 'late_count_2l_s', 'worst_drop_05s', 'worst_drop_2s',
                 'work_lost_15s_s', 'worst_drop_15s', 'completed_ratio', 'backlog_area_s', 'lat_over_10ms',
                 'lat_over_50ms', 'lat_over_100ms', 'settle_log_ok', 'settle_p50_ms', 'settle_p99_ms', 'wire_s2d_bytes'}
        for column in exp12.KEEP:
            self.assertTrue(column in measure.CSV_FIELDS or column in added, column)
        self.assertTrue(set(measure.SLOWDOWN_FIELDS) <= set(exp12.KEEP))


@needs_venv
class StubbedHarness(unittest.TestCase):
    """The stubbed harness's whole trial (test_exp12_load's LoadStub), its records and logs read by the analysis."""

    def test_the_harness_records_read(self):
        import test_exp12_load
        stub = test_exp12_load.WholeTrial('test_the_trials_order')
        stub.setUp()
        try:
            orchestrator, trial = stub.trial()
            out = Path(orchestrator.out)
            runs, trials, meta = exp12.load(out)
            self.assertEqual(len(runs), len(orchestrator.records))
            S = exp12.Session(runs, trials, [meta], sweeps=[out], resamples=200)
            loaded = [r for r in S.runs if r['block'] in exp12.LOADED_BLOCKS]
            self.assertTrue(loaded)
            self.assertTrue(all(r['t0'] is not None and r['settle_span'][0] is not None for r in loaded))
            self.assertTrue(all(tally['lines'] for tally in S.tallies.values()))
            # The stub's model commits 100 per second against its runs' 6000: no window is covered, and none reads as
            # a window without loss.
            self.assertTrue(all(r['stats'] is None for r in loaded))
            self.assertTrue(all('scheduled in its' in r['left_out'] for r in loaded if r['method'] != 'pgdelta'))
            with mock.patch.object(exp12, 'LOG_COVERAGE', 0.0):
                S = exp12.Session(runs, trials, [meta], sweeps=[out], resamples=200)
            self.assertTrue(all(r['stats'] is not None for r in loaded if r['status'] != 'error'))
            lines, _ = exp12.verdicts(S)
            text = '\n'.join(lines)
        finally:
            stub.doCleanups()
        self.assertIn('**Decision 1:', text)
        self.assertIn('- **R3** ("Any wrong output stops speed work"', text)


if __name__ == '__main__':
    unittest.main()
