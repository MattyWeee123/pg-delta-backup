"""Tests for EXP-13's analysis (analysis/exp13.py) and its model (analysis/exp13_model.py).

They need the analysis venv's matplotlib and are skipped without it. The dry runs write a synthetic session
(exp13_smoke_data) and every output into a temporary directory, never into docs/. The rules are tested on sessions
built from rows as exp13.load() returns them (make_row), every verdict row and rule on synthetic medians.
"""

import csv
import io
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from experiments.network import measure, report

try:
    import matplotlib  # noqa: F401
except ImportError:  # the analysis scripts need the analysis venv
    matplotlib = None
if matplotlib is not None:
    sys.path.append(str(Path(__file__).resolve().parents[1] / 'analysis'))
    import exp13
    import exp13_model as model
    import exp13_smoke_data

needs_venv = unittest.skipUnless(matplotlib is not None, 'needs the analysis venv (matplotlib)')
V1 = 'pgdelta_direct_inplace'
W1, W16, HC, TLS = measure.T12_V1_ARMS
WORKLOAD = {'hot': ('hot', 0.01), 'uniform1': ('uniform', 0.01), 'uniform10': ('uniform', 0.1)}


def make_row(method, label='2:0:default', workload='hot', total=1.0, repeat=0, **fields):
    """One run as exp13.load() returns it: passing, verified, durable and through the network checks unless the
    fields say otherwise. A baseline's trial is its workload's, as in the session."""
    row = {key: None for key in (*exp13.TEXT, *exp13.FLAGS, *exp13.NUMERIC)}
    c = exp13.condition(label)
    pattern, fraction = WORKLOAD[workload]
    row.update(sweep='s', index=0, trial_id=f'00-{workload}', pattern=pattern, fraction=fraction, workload=workload,
               speed=label, method=method, status='pass', repeat=repeat, verified=True, total_s=total,
               network_ok=True, rate_gbit=c['gbit'], rtt_ms=c['rtt_ms'], host_tcp=c['host'], a_ok=True,
               b_ok=True if method in measure.FULL_COPY_METHODS else None,
               fsync_ok=True if method in measure.PARCOPY_METHODS else None, dirty_increase_bytes=0,
               dirty_limit_bytes=4 << 20, noise_beyond_table=False, failure_kind=None, network_problems=[],
               network_unknown=[], s2d_8_steady_gbit=1.9, s2d_16_steady_gbit=1.9, zero_delay_bound_gbit=1.92,
               zero_delay_one_gbit=1.92 if c['gbit'] == 2 else 0.96, page_bytes=None, files_changed=None,
               drain_s=None, run_wall_s=None, wire_s2d_bytes=2.11e9 if method in measure.FULL_COPY_METHODS else None,
               parcopy_balance=1.02 if method in measure.PARCOPY_METHODS else None)
    control = method in measure.PARCOPY_METHODS or method in exp13.V1_ARMS
    row['trig_s'] = None if total is None else total + (c['rtt_s'] if control else 0.0)
    row.update(fields)
    for key, value in exp13.protocol_parts(row).items():
        row.setdefault(key, value)
        if row[key] is None:
            row[key] = value
    return row


def runs(method, times, label='2:0:default', workload='hot', first=0, **fields):
    return [make_row(method, label, workload, t, repeat=first + i, **fields) for i, t in enumerate(times)]


def session(rows, trials=None):
    return exp13.Session(rows, trials or {})


def anchors(v1=(3.0, 5.0), full=10.0, parcopy=9.0, tuned_full=None):
    """Both anchors: v1 hot and uniform 1% (5 runs each), `full` (6 runs, pooled) and parcopy8 at the default one."""
    rows = []
    for host in ('default', 'tuned'):
        label = f'2:0:{host}'
        rows += runs(V1, [v1[0]] * 5, label, 'hot') + runs(V1, [v1[1]] * 5, label, 'uniform1')
        rows += runs('full', [full if host == 'default' else (tuned_full or full)] * 6, label)
    rows += runs('parcopy8', [parcopy] * 6, '2:0:default')
    return rows


def cell(rtt, host, hot, uniform1, bound=10.0, full=None, parcopy='parcopy8'):
    """One condition: v1 at hot's and uniform 1%'s ratio over a bound of `bound` s, and `full` (slower, or as given),
    each baseline 3 runs in each workload's trial."""
    label = f'2:{rtt}:{host}'
    rows = runs(V1, [hot * bound] * 5, label, 'hot') + runs(V1, [uniform1 * bound] * 5, label, 'uniform1')
    for workload in exp13.DECIDING:
        rows += runs(parcopy, [bound] * 3, label, workload) + runs('full', [full or bound * 3] * 3, label, workload)
    return rows


@needs_venv
class Model(unittest.TestCase):
    """analysis/exp13_model.py: its ranges, from the inputs written in the specification, inside the written ones."""

    def test_the_written_inputs_give_ranges_inside_the_written_ones(self):
        inputs = model.written_inputs()
        self.assertEqual(inputs['window_bytes'][1], 3145728)  # half of Debian's 6 MiB, tcp_adv_win_scale 1
        self.assertAlmostEqual(inputs['window_bytes'][0], 0.85 * 3145728)
        for rtt, (low, high) in inputs['rates_gbit'].items():  # W / r, inside E3's one-stream ranges
            written = model.WRITTEN['E3'][('one stream', f'2:{rtt}:default')]
            self.assertTrue(written[0] <= low <= high <= written[1], (rtt, low, high))
        rows = model.compare(model.Model(inputs).ranges())
        self.assertEqual(len(rows), sum(len(group) for group in model.WRITTEN.values()))
        outside = {(row['prediction'], row['key']) for row in rows if not row['inside']}
        # Every range is inside, read at the written precision, but the known gaps: each named, none missed.
        self.assertEqual(outside, set(model.KNOWN_GAPS))
        for row in rows:
            if (row['prediction'], row['key']) in model.KNOWN_GAPS:
                # Each gap is the written range's arithmetic, a few percent at most.
                for mine, written in zip(row['model'], row['written']):
                    self.assertLess(abs(mine - written), 0.07 * abs(written), row)
            self.assertIsNotNone(row['model'], row)

    def test_the_derivation_reads_the_written_arithmetic(self):
        m = model.Model(model.written_inputs())
        # full at 60 ms, the low end: its start, 2.12 GB at W / r, its flush once past 30 s (0.5 s), 6 round trips,
        # and 0.6 x log2(W / 10 segments) - 1 round trips of slow start.
        rate = 3145728 / 0.06
        expected = 0.12 + 2.12e9 / rate + 0.5 + 6 * 0.06 + (0.6 * math.log2(3145728 / 14.5e3) - 1) * 0.06
        low, high = m.full('2:60:default')
        self.assertAlmostEqual(low, expected)
        self.assertGreater(low, 41)
        self.assertLess(high, 52)
        # One tuned stream is link-bound: 2.12 GB at 240 MB/s plus the anchor's work and a few round trips.
        self.assertAlmostEqual(m.stream('2:120:tuned'), 240e6)
        self.assertEqual(m.rate_range('2:120:default', 16), (1.92, 1.92))
        # At the anchor nothing is added: no slow start, no burst.
        self.assertEqual(m.slow_start('2:0:default', 1, model.LOW), 0.0)
        self.assertEqual(m.hot_added('2:0:default'), (0.0, 0.0))
        # parcopy16 is faster than parcopy8 where eight default windows are under the link.
        self.assertLess(m.parcopy('2:120:default', 16)[1], m.parcopy('2:120:default', 8)[0])
        # E14: uniform 1% crosses between 1 and 2 Gbit/s at 60 ms, is below 1 at 120 ms; hot is above 2.
        x = m.crossover('uniform1', 60, 'default')
        self.assertTrue(0 <= x[0] <= x[1] <= 1.3)
        self.assertEqual(m.crossover('uniform1', 120, 'default'), (0.0, 0.0))
        self.assertEqual(m.crossover('hot', 20, 'default'), (2.0, math.inf))

    def test_window_from_the_sysctls(self):
        self.assertEqual(model.window(6291456, 1), 3145728)
        self.assertEqual(model.window(6291456, 2), 4718592)
        self.assertEqual(model.window(6291456, -2), 1572864)

    def test_measured_inputs_move_the_ranges(self):
        written = model.Model(model.written_inputs()).ranges()
        slow = model.measured_inputs(6291456, 1, {20: 0.6, 60: 0.2, 120: 0.1})
        self.assertEqual(slow['rates_gbit'][60], (0.2, 0.2))
        ranges = model.Model(slow).ranges()
        key = ('uniform1', '2:60:default')
        self.assertGreater(ranges['E7'][key][0], written['E7'][key][1])
        self.assertFalse(model.inside(ranges['E7'][key], model.WRITTEN['E7'][key]))
        # A rate not measured falls back to W / r.
        partial = model.measured_inputs(6291456, 1, {60: 0.4})
        self.assertEqual(partial['rates_gbit'][20], model.written_inputs()['rates_gbit'][20])

    def test_inside_reads_the_written_precision(self):
        self.assertTrue(model.inside((10.96, 11.5), (11.0, 12.2)))  # 10.96 rounds to 11.0
        self.assertFalse(model.inside((10.94, 11.5), (11.0, 12.2)))
        self.assertTrue(model.inside((1.9, 5.0), (1.80, math.inf)))
        self.assertEqual(model.slack(15), 0.5)
        self.assertEqual(model.slack(0.35), 0.005)

    def test_the_smoke_runs_inputs_and_the_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            sweeps = exp13_smoke_data.write_session(Path(tmp), smoke=True, invocations=('exp13-uniform1',))
            found = model.sweep_inputs(sweeps)
            self.assertEqual(found['source'], 'measured')
            self.assertEqual(found['window_bytes'][1], 3145728)
            self.assertAlmostEqual(found['rates_measured'][60], exp13_smoke_data.one_stream(
                measure.parse_condition('2:60:default')))
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(model.main(['--sweeps', *map(str, sweeps)]), 0)
            self.assertIn('measured inputs', out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            model.main([])
        self.assertIn('written inputs', out.getvalue())
        self.assertIn('| E4 | full | 2:60:default |', out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            model.main(['--rate', '60:0.3', '--json'])
        data = json.loads(out.getvalue())
        self.assertEqual(data['inputs']['rates_gbit']['60'], [0.3, 0.3])
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            model.main(['--rate', '45:0.3'])


@needs_venv
class Rows(unittest.TestCase):
    """The DR verdict's rows on synthetic R_fast values, checked in order: every row reachable."""

    BOTH = ('default', 'tuned')

    def row(self, default, tuned=None, design=BOTH):
        values = {'default': dict(zip(exp13.DECIDING, default))}
        if tuned is not None:
            values['tuned'] = dict(zip(exp13.DECIDING, tuned))
        return exp13.dr_row(values, design)

    def test_every_row(self):
        # (hot, uniform1) on default hosts, then on tuned hosts.
        self.assertEqual(self.row((None, 0.5), (0.4, 0.5)), (exp13.ROWS[1], []))  # Not judged
        self.assertEqual(self.row((0.4, 0.9), (0.4, 0.5)), (exp13.ROWS[2], []))  # 1. Holds as shipped
        self.assertEqual(self.row((0.4, 1.7), (0.4, 0.7)), (exp13.ROWS[3], []))  # 2. Holds with tuned buffers
        self.assertEqual(self.row((0.4, 1.7), (0.4, 1.1)), (exp13.ROWS[4], []))  # 3. Local changes only
        self.assertEqual(self.row((1.0, 1.7), (1.2, 1.1)), (exp13.ROWS[5], []))  # 4. Out at DR latency
        self.assertEqual(self.row((0.4, 1.7), (0.4, 0.95)), (exp13.ROWS[6], []))  # 5. Undecided
        # Row 3 needs uniform 1% at 1.00 or more on every setting, hot at 0.90 or less on some.
        self.assertEqual(self.row((0.95, 1.7), (0.85, 1.1))[0], exp13.ROWS[4])
        self.assertEqual(self.row((0.95, 1.7), (0.95, 1.1))[0], exp13.ROWS[6])

    def test_20_ms_reads_default_hosts_alone(self):
        design = exp13.DESIGN[20]
        self.assertEqual(self.row((0.4, 0.8), design=design)[0], exp13.ROWS[2])
        self.assertEqual(self.row((0.4, 1.2), design=design)[0], exp13.ROWS[4])  # row 2 can't apply
        self.assertEqual(self.row((1.1, 1.2), design=design)[0], exp13.ROWS[5])

    def test_a_lost_tuned_cell(self):
        # Row 2 isn't judged; row 3 needs every setting the design runs, so the reading goes on.
        self.assertEqual(self.row((0.4, 1.7), (0.4, None)), (exp13.ROWS[6], ['tuned not measured']))
        self.assertEqual(self.row((0.4, 1.7), (None, 0.5)), (exp13.ROWS[6], ['tuned not measured']))
        # Row 4 reads the settings measured at r: a lost tuned cell can't turn an Out into Undecided.
        self.assertEqual(self.row((1.1, 1.7), (None, None)), (exp13.ROWS[5], ['tuned not measured']))
        self.assertEqual(self.row((1.1, 1.7), (0.95, None)), (exp13.ROWS[6], ['tuned not measured']))

    def test_near_the_line(self):
        values = {'default': {'hot': 0.40, 'uniform1': 0.88}, 'tuned': {'hot': 0.40, 'uniform1': 0.50}}
        near = exp13.near_line(values, self.BOTH)
        self.assertEqual([(h, w) for h, w, *_ in near], [('default', 'uniform1')])
        # A value near a line whose crossing changes nothing isn't flagged.
        values['tuned']['uniform1'] = 0.89
        self.assertEqual([(h, w) for h, w, *_ in exp13.near_line(values, self.BOTH)], [('default', 'uniform1')])
        values['default']['uniform1'] = 0.80
        self.assertEqual(exp13.near_line(values, self.BOTH), [])


@needs_venv
class Baselines(unittest.TestCase):
    def test_b_fast_is_the_lowest_pooled_median_with_ties_to_the_lower_maximum(self):
        rows = (runs('parcopy8', [9.0, 9.0, 9.6], '2:60:default') + runs('full', [9.04, 9.04, 9.1], '2:60:default')
                + runs('probackup8', [30.0] * 3, '2:60:default'))
        S = session(rows)
        fast = S.baseline('B_fast', '2:60:default')
        self.assertEqual((fast['arm'], fast['tied']), ('full', ['full', 'parcopy8']))
        self.assertEqual(S.baseline('B_rel', '2:60:default')['arm'], 'full')
        self.assertEqual(S.baseline('F0', '2:60:default')['arm'], 'full')
        # Pooled over the trials: a baseline's runs in every workload's trial count at the condition.
        rows = (runs('parcopy8', [9.0, 9.0], '2:60:default', 'hot') + runs('parcopy8', [11.0, 11.0], '2:60:default',
                                                                           'uniform1'))
        self.assertEqual(session(rows).baseline('B_fast', '2:60:default')['median'], 10.0)

    def test_full_at_1_gbit_is_inferred_from_2(self):
        rows = runs('full', [45.0] * 4, '2:60:default') + runs('parcopy8', [18.5] * 5, '1:60:default')
        S = session(rows)
        f0 = S.baseline('F0', '1:60:default')
        self.assertEqual((f0['arm'], f0['inferred'], f0['where'], f0['median']), ('full', True, '2:60:default', 45.0))
        self.assertEqual(S.baseline('B_fast', '1:60:default')['arm'], 'parcopy8')
        # Where full ran at 1 Gbit/s (A16 said so), its own runs are used.
        S = session(rows + runs('full', [80.0] * 2, '1:60:default'))
        self.assertEqual((S.baseline('F0', '1:60:default')['inferred'], S.baseline('F0', '1:60:default')['median']),
                         (False, 80.0))

    def test_arms_a13_and_a15_exclude_leave_every_baseline(self):
        # A13: two runs of parcopy8 at one condition fail durability, over the session (two trials here).
        rows = (runs('full', [10.0] * 4, '2:60:default') + runs('parcopy8', [9.0] * 2, '2:60:default', 'hot')
                + runs('parcopy8', [9.0], '2:60:default', 'hot', first=2, a_ok=False)
                + runs('parcopy8', [9.0], '2:60:default', 'uniform1', first=0, a_ok=False))
        S = session(rows)
        self.assertIn('parcopy8', S.not_durable)
        self.assertTrue(S.excluded['parcopy8'].startswith('A13'))
        self.assertEqual(S.baseline('B_fast', '2:60:default')['arm'], 'full')
        # A15: a probackup8 run that failed (not void), or whose copy its tool aborted.
        rows = (runs('full', [40.0] * 4, '2:60:default') + runs('probackup8', [30.0] * 2, '2:60:default')
                + runs('probackup8', [None], '2:60:default', first=2, status='error', failure_kind='tool'))
        S = session(rows)
        self.assertTrue(S.excluded['probackup8'].startswith('A15'))
        self.assertEqual(S.baseline('B_rel', '2:60:default')['arm'], 'full')
        # An error of the harness fails nothing: the run is only left out.
        rows[-1]['failure_kind'] = 'harness'
        S = session(rows)
        self.assertEqual((S.excluded, S.baseline('B_rel', '2:60:default')['arm']), ({}, 'probackup8'))

    def test_the_void_rule_leaves_void_runs_out_without_failing_the_arm(self):
        rows = (runs('full', [40.0] * 4, '2:60:default') + runs('probackup8', [30.0] * 2, '2:60:default')
                + runs('probackup8', [20.0], '2:60:default', first=2, status='void', void=True))
        S = session(rows)
        self.assertEqual(S.excluded, {})
        self.assertEqual(S.rows[-1]['left_out'], 'void')
        self.assertEqual(S.baseline('B_rel', '2:60:default')['n'], 2)
        # A logical difference while the user tables matched (status fail, not void) fails A15.
        rows[-1].update(status='fail', void=False)
        self.assertIn('probackup8', session(rows).excluded)

    def test_not_measured_cells(self):
        label = '2:60:default'
        bad = dict(network_ok=False, network_problems=["netem's own drops on source: 3"])
        # One A11 exclusion leaves the cell measured; two make it "not measured".
        rows = runs(V1, [4.0] * 4, label) + runs(V1, [4.0], label, first=4, **bad)
        S = session(rows)
        self.assertEqual((S.cell(V1, label, 'hot')['a11'], S.cell(V1, label, 'hot')['measured']), (1, True))
        self.assertEqual(S.rows[-1]['left_out'], "A11: netem's own drops on source: 3")
        S = session(runs(V1, [4.0] * 3, label) + runs(V1, [4.0] * 2, label, first=3, **bad))
        self.assertFalse(S.cell(V1, label, 'hot')['measured'])
        # A run whose checks couldn't be made is left out, unchecked, but isn't an A11 exclusion.
        S = session(runs(V1, [4.0] * 3, label) + runs(V1, [4.0] * 2, label, first=3, network_ok=None))
        self.assertEqual((S.cell(V1, label, 'hot')['a11'], S.cell(V1, label, 'hot')['n']), (0, 3))
        self.assertEqual(S.rows[-1]['left_out'], 'network unchecked')
        # B_fast: a candidate not measured and not clearly slower makes it not measured; a much slower one doesn't.
        rows = runs('full', [40.0] * 4, label) + runs('parcopy8', [9.0] * 2, label) + runs('parcopy8', [9.0] * 2, label,
                                                                                         first=2, **bad)
        fast = session(rows).baseline('B_fast', label)
        self.assertEqual((fast['arm'], fast['measured']), ('parcopy8', False))
        rows = runs('full', [40.0] * 2, label) + runs('full', [40.0] * 2, label, first=2, **bad) + runs(
            'parcopy8', [9.0] * 4, label)
        fast = session(rows).baseline('B_fast', label)
        self.assertEqual((fast['arm'], fast['measured']), ('parcopy8', True))
        rows = runs('full', [9.02] * 2, label) + runs('full', [9.02] * 2, label, first=2, **bad) + runs(
            'parcopy8', [9.0] * 4, label)
        self.assertFalse(session(rows).baseline('B_fast', label)['measured'])

    def test_ratios_and_their_views(self):
        label = '2:60:default'
        rows = (runs(V1, [4.0, 4.0, 4.2], label) + runs('parcopy8', [10.0, 10.0, 10.5], label, 'hot')
                + runs('parcopy8', [8.0, 8.0], label, 'uniform1') + runs('full', [40.0] * 4, label)
                + runs('full', [10.0] * 4, '2:0:default', backup_mode_adder_s=0.1))
        r = session(rows).ratios('hot', label)
        fast = r['B_fast']
        self.assertEqual(fast['arm'], 'parcopy8')
        self.assertAlmostEqual(fast['median'], 4.0 / 10.0)
        self.assertAlmostEqual(fast['max'], 4.2 / 10.5)
        self.assertAlmostEqual(fast['trial'], 4.0 / 10.0)  # against hot's own two runs
        self.assertEqual(fast['n_trial'], 3)
        self.assertAlmostEqual(fast['trig'], 4.06 / 10.06)  # both launched over the control path: one r each
        self.assertAlmostEqual(fast['adj'], 0.4)  # against a stopped source R_adj is R
        f0 = r['F0']
        self.assertAlmostEqual(f0['trig'], 4.06 / 40.0)  # full is local on dest: no trigger
        self.assertAlmostEqual(f0['adj'], 4.1 / 40.0)  # the default anchor's adder, over a live source


@needs_venv
class Verdict(unittest.TestCase):
    """The verdict at each r: R1's precedence, the flags, the qualifier, v1 lost to A13."""

    def base(self, **kwargs):
        rows = anchors(tuned_full=kwargs.pop('tuned_full', 10.0))
        rows += cell(60, 'default', 0.4, 1.7, full=40.0) + cell(60, 'tuned', 0.4, 0.7, full=kwargs.pop('full60t', 30.0))
        return rows

    def test_row_2_and_r1_precedence(self):
        S = session(self.base())
        r1 = exp13.r1_reading(S)
        self.assertFalse(r1['fires'])
        v = exp13.dr_verdict(S, 60, r1)
        self.assertEqual((v['row'], v['provisional']), (exp13.ROWS[3], []))
        # R1 fires where v1 slows down more than full on tuned hosts: rows 1-3 provisional, at every r.
        S = session(self.base(full60t=10.5))  # Δ_full 0.5 s; Δ_v1 hot 4 − 3 = 1 s
        r1 = exp13.r1_reading(S)
        self.assertTrue(r1['fires'])
        self.assertEqual({(c['workload'], c['label']) for c in r1['cells'] if c['fires']},
                         {('hot', '2:60:tuned'), ('uniform1', '2:60:tuned')})
        v = exp13.dr_verdict(S, 60, r1)
        self.assertEqual((v['row'], v['provisional']), (exp13.ROWS[3], ['R1 fired']))
        # Row 4 isn't a claim for v1: R1 leaves it as it is.
        rows = anchors(tuned_full=10.0) + cell(60, 'default', 1.2, 1.7, full=40.0) + cell(60, 'tuned', 1.1, 1.2,
                                                                                            full=10.5)
        S = session(rows)
        v = exp13.dr_verdict(S, 60)
        self.assertEqual((v['row'], v['provisional']), (exp13.ROWS[5], []))
        # Each Δ is from its own host setting's anchor: a slow tuned anchor makes R1's tuned Δs smaller.
        S = session(anchors(tuned_full=9.0) + cell(60, 'tuned', 0.4, 0.7, full=10.5))
        self.assertEqual(S.delta('full', '2:60:tuned'), (1.5, '2:0:tuned'))

    def test_v1_lost_to_a13_reads_v1_not_durable(self):
        rows = self.base()
        for r in [r for r in rows if r['method'] == V1 and r['speed'] == '2:60:default'][:2]:
            r['a_ok'] = False
        S = session(rows)
        self.assertIn(V1, S.not_durable)
        for rtt in exp13.RTTS:
            v = exp13.dr_verdict(S, rtt)
            self.assertEqual(v['row'], exp13.ROWS[0])
        lines, verdicts = exp13.verdict_lines(S, exp13.r1_reading(S), {}, [], [])
        # The rows are still computed: its two failed runs leave hot's cell 3 runs, still measured.
        self.assertEqual(verdicts[60]['computed'], exp13.ROWS[3])
        self.assertIn('**v1 is not durable for the session**', '\n'.join(lines))
        self.assertIn('**60 ms: v1 not durable.** no claim for v1', '\n'.join(lines))

    def test_not_judged_and_the_bound(self):
        rows = self.base()
        bad = dict(network_ok=False, network_problems=['minrtt'])
        for r in [r for r in rows if r['method'] == V1 and r['speed'] == '2:60:default' and r['workload'] == 'hot'][:2]:
            r.update(bad)
        self.assertEqual(exp13.dr_verdict(session(rows), 60)['row'], exp13.ROWS[1])
        # A bound that fails A9 at a 2 Gbit/s condition at r makes rows 1 and 2 there provisional.
        rows = anchors() + cell(60, 'default', 0.4, 0.8, bound=14.0, full=40.0) + cell(60, 'tuned', 0.4, 0.7)
        v = exp13.dr_verdict(session(rows), 60)
        self.assertEqual(v['row'], exp13.ROWS[2])
        self.assertIn('bound suspect', v['provisional'])

    def test_fragile_tls_sensitive_unstable_and_the_qualifier(self):
        rows = self.base()
        # One slow v1 run in uniform 1% on tuned hosts: its median stays 0.85 of the bound, its maximum 0.95.
        tuned = [r for r in rows if r['method'] == V1 and r['speed'] == '2:60:tuned' and r['workload'] == 'uniform1']
        for r in tuned:
            r['total_s'] = 8.5
        tuned[0]['total_s'] = 9.5
        S = session(rows)
        v = exp13.dr_verdict(S, 60)
        self.assertEqual(v['row'], exp13.ROWS[3])
        self.assertEqual((v['flags']['fragile'], v['flags']['unread']), ([('R_max', exp13.ROWS[6])], []))
        self.assertTrue(v['qualifier'])  # R_F0 under 0.90 for both on default hosts
        # A view without its runs (R_trial without the baseline's runs in v1's trial) gives no row: not fragile.
        rows = [r for r in rows if not (r['method'] == 'parcopy8' and r['speed'] == '2:60:tuned'
                                        and r['workload'] == 'uniform1')]
        v = exp13.dr_verdict(session(rows), 60)
        self.assertEqual(v['flags']['unread'], ['R_trial'])
        # TLS-sensitive at 120 ms: R_tls (A6's reading) in place of v1's tuned ratio changes the row.
        rows = (anchors() + cell(120, 'default', 0.5, 2.5, full=90.0) + cell(120, 'tuned', 0.5, 0.85, full=30.0)
                + runs(TLS, [9.5] * 5, '2:120:tuned', 'uniform1') + runs(TLS, [5.1] * 5, '2:120:tuned', 'hot'))
        v = exp13.dr_verdict(session(rows), 120)
        self.assertEqual((v['row'], v['flags']['tls_sensitive']), (exp13.ROWS[3], exp13.ROWS[6]))
        self.assertEqual(exp13.dr_verdict(session(rows), 60)['flags']['tls_sensitive'], None)  # 120 ms only
        # A17 (i): a baseline's per-trial medians more than 3% apart flags the pooled baseline.
        rows = (anchors() + cell(60, 'tuned', 0.4, 0.7) + runs(V1, [4.0] * 5, '2:60:default', 'hot')
                + runs(V1, [17.0] * 5, '2:60:default', 'uniform1') + runs('parcopy8', [10.0] * 2, '2:60:default', 'hot')
                + runs('parcopy8', [10.5] * 2, '2:60:default', 'uniform1') + runs('full', [40.0] * 4, '2:60:default'))
        v = exp13.dr_verdict(session(rows), 60)
        self.assertEqual([(u['arm'], u['label']) for u in v['flags']['unstable']], [('parcopy8', '2:60:default')])

    def test_the_tuned_cell_lost_and_a12(self):
        rows = anchors() + cell(60, 'default', 1.1, 1.7, full=40.0)
        v = exp13.dr_verdict(session(rows), 60)
        self.assertEqual((v['row'], v['flags']['notes']), (exp13.ROWS[5], ['tuned not measured']))
        # A12: a row 2 that fails where one tuned stream was emulator-limited is flagged.
        rows = anchors() + cell(60, 'default', 0.4, 1.7, full=40.0) + cell(60, 'tuned', 0.4, 0.95)
        limited = [{'trial': 't', 'label': '2:60:tuned', 'rate': 1.5, 'reference': 1.9, 'limited': True}]
        v = exp13.dr_verdict(session(rows), 60, a12=limited)
        self.assertEqual((v['row'], len(v['flags']['emulator_limited'])), (exp13.ROWS[6], 1))
        rows = anchors() + cell(60, 'default', 0.4, 1.7, full=40.0) + cell(60, 'tuned', 0.4, 0.7)
        self.assertEqual(exp13.dr_verdict(session(rows), 60, a12=limited)['flags']['emulator_limited'], [])


@needs_venv
class LedgerRules(unittest.TestCase):
    def test_l1_l2_and_r1(self):
        rows = (anchors() + cell(60, 'default', 0.45, 1.6, full=40.0) + cell(60, 'tuned', 0.4, 0.7, full=11.0)
                + runs(V1, [10.0] * 5, '2:0:default', 'uniform10') + runs(V1, [12.5] * 5, '2:60:default', 'uniform10'))
        S = session(rows)
        lines, state = exp13.ledger_lines(S, exp13.r1_reading(S))
        text = '\n'.join(lines)
        # Δ_v1 at 60 ms default: hot 4.5 − 3 = 1.5 s, uniform 1% 16 − 5 = 11 s, uniform 10% 2.5 s.
        self.assertIs(state['l1'], False)
        self.assertIn('uniform1 11.00 s', text)
        self.assertIs(state['l2'], True)  # Δ_full 30 s
        self.assertIn('**fires**', text)  # tuned: Δ_v1 hot 1.0 > Δ_full 1.0? uniform 2.0 > 1.0
        rows = anchors() + cell(60, 'default', 0.35, 0.5, full=40.0)
        S = session(rows)
        _, state = exp13.ledger_lines(S, exp13.r1_reading(S))
        self.assertEqual((state['l1'], state['l2']), (True, True))
        self.assertFalse(exp13.r1_reading(S)['fires'])

    def test_r1_judges_a_not_measured_cell_and_flags_it(self):
        rows = anchors() + cell(60, 'tuned', 0.4, 0.7, full=10.2)
        for r in [r for r in rows if r['method'] == V1 and r['speed'] == '2:60:tuned' and r['workload'] == 'hot'][:2]:
            r.update(network_ok=False, network_problems=['minrtt'])
        r1 = exp13.r1_reading(session(rows))
        hot = next(c for c in r1['cells'] if c['workload'] == 'hot')
        self.assertEqual((hot['fires'], hot['measured']), (True, False))


@needs_venv
class AddedRules(unittest.TestCase):
    def rows120(self, w1=(5.5, 8.5), w16=(4.9, 8.1), hc=(10.6, 9.0), tls=(5.2, 8.3)):
        label = exp13.SIDE_CELL
        rows = anchors() + cell(120, 'tuned', 0.5, 0.8, bound=10.0, full=12.0)
        rows += runs(V1, [10.0] * 5, label, 'uniform10')
        for arm, values in ((W1, w1), (W16, w16), (TLS, tls)):
            rows += runs(arm, [values[0]] * 5, label, 'hot') + runs(arm, [values[1]] * 5, label, 'uniform1')
        rows += runs(HC, [hc[0]] * 5, label, 'uniform1') + runs(HC, [hc[1]] * 5, label, 'uniform10')
        return rows

    def test_a2_mixes(self):
        S = session(anchors())
        def a2(rows60, rows120, provisional=()):
            verdicts = {60: {'computed': rows60, 'provisional': list(provisional), 'row': rows60},
                        120: {'computed': rows120, 'provisional': [], 'row': rows120}}
            lines = exp13.a_lines(S, {'fires': False, 'cells': []}, {'l1': True}, verdicts, {}, [], [], [],
                                  exp13.a16_reading(S))
            return next(line for line in lines if line.startswith('- **A2**'))
        self.assertIn('**contradicted**', a2(exp13.ROWS[3], exp13.ROWS[2]))
        self.assertIn('**contradicted**', a2(exp13.ROWS[2], exp13.ROWS[3], ['R1 fired']))  # provisional or not
        self.assertIn('D36 stands', a2(exp13.ROWS[2], exp13.ROWS[2]))
        self.assertIn('stays at C', a2(exp13.ROWS[2], exp13.ROWS[2], ['R1 fired']))
        self.assertIn('stays at C', a2(exp13.ROWS[2], exp13.ROWS[5]))
        self.assertIn('stays at C', a2(exp13.ROWS[1], exp13.ROWS[1]))

    def test_a3_parts_add_up_to_delta(self):
        timers = dict(connect_s=0.0, accept_wait_busy_s=0.0, accept_wait_idle_s=0.0, basis_wait_busy_s=0.0,
                      basis_wait_idle_s=0.0, leaf_wait_busy_s=0.0, leaf_wait_idle_s=0.1, complete_wait_busy_s=0.0,
                      complete_wait_idle_s=0.0, drain_s=0.01, page_send_s=1.0)
        at_r = dict(connect_s=0.12, accept_wait_busy_s=0.01, accept_wait_idle_s=0.12, basis_wait_busy_s=0.0,
                    basis_wait_idle_s=0.06, leaf_wait_busy_s=0.3, leaf_wait_idle_s=0.5, complete_wait_busy_s=0.2,
                    complete_wait_idle_s=0.12, drain_s=0.13, page_send_s=1.5)
        rows = runs(V1, [3.0] * 5, '2:0:tuned', **timers) + runs(V1, [5.2] * 5, '2:120:tuned', **at_r)
        parts = exp13.a3_parts(session(rows), 'hot', '2:120:tuned')
        self.assertAlmostEqual(parts['delta'], 2.2)
        self.assertAlmostEqual(parts['idle_waits_s'], 0.46)  # basis and leaf idle
        self.assertAlmostEqual(parts['fixed_rtts_s'], 0.12 + 0.12 + 0.12 + 0.12)  # connect, accept, complete, drain
        self.assertAlmostEqual(parts['sending_s'], 0.5 + 0.51)  # page_send and every busy part
        self.assertAlmostEqual(parts['idle_waits_s'] + parts['fixed_rtts_s'] + parts['sending_s'] + parts['residual'],
                               parts['delta'])
        self.assertEqual(parts['largest'], 'sending_s')
        # Without the busy and idle parts (macOS, pipes) nothing is split.
        rows = runs(V1, [3.0] * 5, '2:0:tuned') + runs(V1, [5.2] * 5, '2:120:tuned')
        parts = exp13.a3_parts(session(rows), 'hot', '2:120:tuned')
        self.assertEqual((parts['residual'], parts['largest']), (None, None))
        # The residual names no fix when it is the largest part.
        rows = runs(V1, [3.0] * 5, '2:0:tuned', **timers) + runs(V1, [9.0] * 5, '2:120:tuned', **at_r)
        self.assertEqual(exp13.a3_parts(session(rows), 'hot', '2:120:tuned')['largest'], 'residual')

    def test_a4_in_order(self):
        # v1 at 120 ms tuned: hot 5.0, uniform 1% 8.0 (rows120).
        self.assertEqual(exp13.a4_reading(session(self.rows120(w16=(4.6, 8.05))))['part'], '(i)')
        self.assertEqual(exp13.a4_reading(session(self.rows120(w16=(4.6, 8.2))))['part'], '(iii)')
        self.assertEqual(exp13.a4_reading(session(self.rows120(w1=(5.05, 7.95), w16=(4.9, 8.1))))['part'], '(ii)')
        self.assertEqual(exp13.a4_reading(session(self.rows120(w1=(5.5, 8.5), w16=(4.9, 8.1))))['part'], '(iii)')
        self.assertIsNone(exp13.a4_reading(session(anchors()))['part'])
        # A cell A11 leaves "not measured" isn't read: A4 is not judged.
        rows = self.rows120(w16=(4.6, 8.05))
        for r in [r for r in rows if r['method'] == W16 and r['workload'] == 'hot'][:2]:
            r.update(network_ok=False, network_problems=['minrtt'])
        self.assertIsNone(exp13.a4_reading(session(rows))['part'])

    def test_a5_in_order(self):
        # v1 at 120 ms tuned: uniform 1% 8.0, uniform 10% 10.0.
        self.assertEqual(exp13.a5_reading(session(self.rows120(hc=(8.05, 9.6))))['part'], '(i)')
        self.assertEqual(exp13.a5_reading(session(self.rows120(hc=(10.0, 9.6))))['part'], '(ii)')
        self.assertEqual(exp13.a5_reading(session(self.rows120(hc=(8.0, 9.9))))['part'], '(iii)')

    def test_a6_measured_and_priced(self):
        label = exp13.SIDE_CELL
        rows = self.rows120(tls=(5.2, 8.2))
        for r in rows:
            if r['method'] == V1 and r['speed'] == label:
                r.update(prefetch_bytes=100e6, prefetch_s=0.25)  # 400 MB/s
            if r['method'] == TLS:
                r.update(prefetch_bytes=180e6, prefetch_s=0.45)  # 80 MB more: 0.2 s at v1's rate
        reading = exp13.a6_reading(session(rows))
        hot = reading['cells'][('hot', label)]
        self.assertAlmostEqual(hot['measured_d'], 0.2)
        self.assertAlmostEqual(hot['priced_d'], 0.4)
        self.assertTrue(hot['priced_fires'] and not hot['measured_fires'])
        self.assertEqual((reading['fires'], reading['differ']), (True, True))
        self.assertAlmostEqual(hot['reading'], 5.4)  # R_tls takes the slower
        # Fewer bytes prefetched through the proxy price nothing back.
        for r in rows:
            if r['method'] == TLS:
                r.update(prefetch_bytes=50e6)
        hot = exp13.a6_reading(session(rows))['cells'][('hot', label)]
        self.assertEqual((hot['extra_bytes'], hot['reading']), (0.0, hot['tls']))
        self.assertIs(exp13.a6_reading(session(rows))['fires'], False)

    def test_a7_and_a12_from_the_trial_records(self):
        def trial(probe60, probe120, one=1.9, anchor=1.92):
            visits = [{'condition': label, 'link_tests': [{'steady_gbit': rate}]} for label, rate in
                      (('2:0:default', anchor), ('2:60:tuned', one), ('2:120:tuned', one))]
            first = {'2:60:tuned': {'ssh_probe': {'last_gbit': probe60}},
                     '2:120:tuned': {'ssh_probe': {'last_gbit': probe120}}}
            return {('s', '00-uniform1'): {'visits': visits, 'first_visit': first}}
        S = session(anchors(), trial(0.25, 0.12))
        self.assertIs(exp13.a7_reading(S)['fires'], True)
        S = session(anchors(), trial(1.2, 0.12))
        self.assertIs(exp13.a7_reading(S)['fires'], False)
        self.assertEqual([e['limited'] for e in exp13.a12_reading(S)], [False, False])
        S = session(anchors(), trial(1.2, 0.12, one=1.6))
        self.assertEqual([e['limited'] for e in exp13.a12_reading(S)], [True, True])

    def test_a9_the_bound(self):
        label = '2:120:default'
        rows = (anchors() + runs('parcopy8', [12.5] * 4, label, s2d_8_steady_gbit=1.5, wire_s2d_bytes=2.12e9)
                + runs('parcopy16', [10.2] * 4, label, s2d_16_steady_gbit=1.92, wire_s2d_bytes=2.12e9)
                + runs('full', [90.0] * 4, label))
        gate = exp13.bound_gate(session(rows), label)
        # 2.12 GB at 1.5 Gbit/s is 11.31 s, + 1.0 + 0.72: 13.03 s; at 1.92 Gbit/s 8.83 + 1.72 = 10.55 s.
        self.assertAlmostEqual(gate['arms']['parcopy8']['limit'], 2.12e9 * 8 / 1.5e9 + 1.72)
        self.assertEqual((gate['arms']['parcopy8']['suspect'], gate['arms']['parcopy16']['suspect']), (False, False))
        self.assertTrue(gate['arms']['parcopy8']['window_limited'])  # 1.5 < 0.9 x 1.92
        self.assertEqual((gate['b_fast'], gate['reasons']), ('parcopy16', []))  # B_fast's arm isn't window-limited
        rows = [r for r in rows if r['method'] != 'parcopy16']
        self.assertEqual(exp13.bound_gate(session(rows), label)['reasons'], ['window-limited bound'])
        for r in rows:
            if r['method'] == 'parcopy8':
                r['parcopy_balance'] = 1.2
        self.assertIn('bound suspect', exp13.bound_gate(session(rows), label)['reasons'])
        # No durable parcopy arm left at r: "bound not durable".
        for r in [r for r in rows if r['method'] == 'parcopy8'][:2]:
            r['a_ok'] = False
        self.assertIn('bound not durable', exp13.bound_gate(session(rows), label)['reasons'])

    def test_a9_reports_parcopys_connection_set_up_beside_each_bound_at_latency(self):
        rows = (anchors() + runs('parcopy8', [10.4] * 4, '2:60:default', s2d_8_steady_gbit=1.9, parcopy_connect_s=0.061)
                + runs('full', [45.0] * 4, '2:60:default'))
        S = session(rows)
        gates = {label: exp13.bound_gate(S, label) for label in ('2:0:default', '2:60:default')}
        self.assertAlmostEqual(gates['2:60:default']['arms']['parcopy8']['connect_s'], 0.061)
        text = '\n'.join(exp13.gate_lines(gates))
        self.assertIn('2:60:default parcopy8: 10.40 s over 4 runs', text)
        self.assertIn('its connection set-up 0.061 s (parcopy_connect_s)', text)
        anchor_line = next(line for line in text.splitlines() if '2:0:default parcopy8' in line)
        self.assertNotIn('connection set-up', anchor_line)

    def test_a10_drift_gates(self):
        exp11 = {('hot', 'full'): {'total_s': 10.27, 'wire_s2d_bytes': 2.1e9, 'measured_s2d_gbit': 1.917},
                 ('hot', 'parcopy8'): {'total_s': 9.3, 'measured_s2d_gbit': 1.917},
                 ('hot', V1): {'total_s': 3.11, 'wire_s2d_bytes': 29.2e6, 'measured_s2d_gbit': 1.917}}
        rows = anchors(v1=(3.1, 5.3), full=10.3, parcopy=9.3)
        for r in rows:
            r.update(measured_s2d_gbit=1.92, wire_s2d_bytes=2.1e9 if r['method'] == 'full' else 29.2e6)
        gates = exp13.a10_gates(session(rows), exp11)
        self.assertFalse(exp13.shifted(gates))
        self.assertEqual({g['gate'] for g in gates if g['ok'] is not None},
                         {'full total_s', 'parcopy8 total_s', 'v1 total_s', 'one stream, Gbit/s', 'full wire bytes',
                          'v1 wire bytes'})
        for r in rows:
            if r['method'] == V1 and r['workload'] == 'hot':
                r['total_s'] = 3.5  # 12.5% over EXP-11's 3.11 s
        self.assertTrue(exp13.shifted(exp13.a10_gates(session(rows), exp11)))
        self.assertTrue(exp13.shifted([]))  # no gate could be checked

    def test_a13_a14_a16_a17(self):
        rows = anchors() + runs('full_nosync', [9.0], '2:0:default', a_ok=False, dirty_increase_bytes=1.8e9)
        S = session(rows)
        self.assertFalse(S.a_void)
        rows[-1]['a_ok'] = True
        self.assertTrue(session(rows).a_void)  # A14: check (a) is void for the session
        # Under A14 a run failing (a) still counts; (b) still applies to full copies.
        rows += runs('parcopy8', [9.0], '2:0:default', first=6, a_ok=False)
        S = session(rows)
        self.assertTrue(S.rows[-1]['usable'])
        # A16: one default stream at 1 Gbit/s at most 0.8 at 60 and 120 ms: full doesn't run there.
        trials = {('s', '00-uniform1'): {'visits': [{'condition': '1:60:default', 'link_tests': [{'steady_gbit': 0.4}]},
                                                    {'condition': '1:120:default', 'link_tests': [{'steady_gbit': 0.2}]}]}}
        a16 = exp13.a16_reading(session(anchors(), trials))
        self.assertEqual((a16['skip'], a16['consistent']), (True, True))
        a16 = exp13.a16_reading(session(anchors() + runs('full', [80.0] * 2, '1:60:default'), trials))
        self.assertEqual((a16['skip'], a16['consistent']), (True, False))
        # A17 (ii): the tuned anchor more than 0.1 s from the default one.
        gaps = {(a['arm'], a['workload']): a['reported'] for a in exp13.a17_anchors(session(anchors(tuned_full=10.2)))}
        self.assertEqual(gaps[('full', None)], True)
        self.assertEqual(gaps[(V1, 'hot')], False)


@needs_venv
class Reports(unittest.TestCase):
    """The lines A8, A11 (i), A13 and A15 print, the qualifier at 60 ms, and E3's stunnel ratio."""

    def a_lines(self, S):
        return '\n'.join(exp13.a_lines(S, exp13.r1_reading(S), {'l1': True}, {}, {}, [], [], [], exp13.a16_reading(S)))

    def test_a8_reads_uniform_10_at_each_r(self):
        rows = (anchors() + runs(V1, [8.0] * 5, '2:20:default', 'uniform10') + runs('parcopy8', [10.0] * 6, '2:20:default')
                + runs(V1, [9.5] * 5, '2:60:tuned', 'uniform10') + runs('parcopy8', [10.0] * 6, '2:60:tuned')
                + runs(V1, [12.0] * 5, '2:120:tuned', 'uniform10') + runs('parcopy8', [10.0] * 6, '2:120:tuned'))
        text = self.a_lines(session(rows))
        line = next(line for line in text.splitlines() if line.startswith('- **A8**'))
        self.assertIn("2:20:default R_fast 0.80: v1's DR claim covers 10% scattered changes there", line)
        self.assertIn('2:60:tuned R_fast 0.95: reported', line)
        self.assertIn('2:120:tuned R_fast 1.20: a case for a full copy', line)

    def test_a11_visits_a13_and_a15_file_classes(self):
        retry = {'condition': '2:60:default', 'repeat': 1, 'ok': True, 'attempts': [
            {'checks': {'problems': {'pings': ['the ping minimum 62.4 ms is outside [60, 61] ms'], 'tree': []}}},
            {'checks': {'problems': {}}}]}
        trials = {('s', '00-hot'): {'visits': [retry], 'infrastructure_fault': 'the visit to 2:120:tuned failed twice',
                                    'reference_classes': {'counts': {'user_table': 1, 'catalog': 5, 'other': 0},
                                                          'paths': {'user_table': ['base/5/16400']},
                                                          'user_tables_match': False}}}
        S = session(anchors(), trials)
        text = '\n'.join(exp13.visit_lines(S))
        self.assertIn('1 needed the reset and a second check', text)
        self.assertIn('pings: the ping minimum 62.4 ms is outside [60, 61] ms', text)
        self.assertIn('**infrastructure fault** in 00-hot', text)
        text = self.a_lines(S)
        self.assertIn('infrastructure fault in 00-hot', text)
        self.assertIn('reference digests differ in user_table 1, catalog 5, other 0 files; user tables match: failed '
                      '(user-table files: base/5/16400)', text)
        # A13 counts a measured failure of (a) even where the run errored after it.
        rows = anchors() + runs(V1, [4.0] * 3, '2:20:default') + runs(V1, [4.0] * 2, '2:20:default', first=3,
                                                                      status='error', a_ok=False)
        S = session(rows)
        self.assertIn(V1, S.not_durable)
        self.assertIn('reclassified as not durable for the session: pgdelta_direct_inplace (2 runs at 2:20:default)',
                      self.a_lines(S))

    def test_the_qualifier_names_probackup8_at_60_ms(self):
        rows = anchors() + cell(60, 'default', 0.4, 0.8, full=40.0) + runs('probackup8', [30.0] * 3, '2:60:default',
                                                                             'uniform1')
        v = exp13.dr_verdict(session(rows), 60)
        self.assertEqual((v['qualifier'], v['probackup']), (True, True))
        rows = anchors() + cell(60, 'default', 0.4, 0.8, full=40.0) + runs('probackup8', [8.0] * 3, '2:60:default',
                                                                             'uniform1')
        self.assertIs(exp13.dr_verdict(session(rows), 60)['probackup'], False)

    def test_e3s_stunnel_ratio_is_each_trials_against_its_own_direct_test(self):
        trials = {('s', '00-uniform1'): {
            'visits': [{'condition': '2:60:default', 'link_tests': [{'steady_gbit': 0.40}]},
                       {'condition': '2:60:default', 'link_tests': [{'steady_gbit': 0.30}]}],
            'first_visit': {'2:60:default': {'stunnel_link_test': {'steady_gbit': 0.38}}}}}
        lines = exp13.Predictions(session(anchors(), trials)).all({'cells': []}, {}, {})
        e3 = next(line for line in lines if line.startswith('- **E3**'))
        self.assertIn('stunnel / direct at 2:60:default 0.950 (written 0.90–1.10) held', e3)


@needs_venv
class Crossovers(unittest.TestCase):
    def test_d78_where_both_rates_ran_and_where_only_2_did(self):
        def rows(v1_1g, v1_2g):
            return (runs(V1, [v1_1g] * 5, '1:60:default', 'uniform1') + runs(V1, [v1_2g] * 5, '2:60:default', 'uniform1')
                    + runs('parcopy8', [19.0] * 5, '1:60:default') + runs('parcopy8', [10.0] * 6, '2:60:default')
                    + runs('full', [45.0] * 4, '2:60:default'))
        x = exp13.crossover(session(rows(17.0, 17.0)), 'uniform1', 60)  # R 0.89 at 1, 1.7 at 2: crosses
        self.assertEqual(x['text'][-6:], 'Gbit/s')
        self.assertTrue(0.96 < x['range'][0] < 1.92)
        self.assertEqual(exp13.crossover(session(rows(20.0, 17.0)), 'uniform1', 60)['text'], 'below 1')
        self.assertEqual(exp13.crossover(session(rows(10.0, 8.0)), 'uniform1', 60)['text'], 'above 2')
        only2 = runs(V1, [4.0] * 5, '2:60:default', 'hot') + runs('parcopy8', [10.0] * 6, '2:60:default')
        self.assertEqual(exp13.crossover(session(only2), 'hot', 60)['text'], 'above 2')
        only2 = runs(V1, [12.0] * 5, '2:60:default', 'hot') + runs('parcopy8', [10.0] * 6, '2:60:default')
        self.assertEqual(exp13.crossover(session(only2), 'hot', 60)['text'], 'below 2')


@needs_venv
class Records(unittest.TestCase):
    def test_paths_in_records(self):
        record = {'visits': [{'link_tests': [{'steady_gbit': 1.0}]}, {'link_tests': []}],
                  'first_visit': {'2:60:default': {'ssh_probe': {'last_gbit': 0.2}}}}
        self.assertTrue(exp13.get_path(record, 'visits[].link_tests[].steady_gbit'))
        self.assertTrue(exp13.get_path(record, 'first_visit.*.ssh_probe'))
        self.assertFalse(exp13.get_path(record, 'first_visit.*.stunnel_link_test'))
        self.assertFalse(exp13.get_path(record, 'visits[].pings'))

    def test_report_orders_the_conditions(self):
        rows = [{'speed': label, 'target_gbit': float(label.split(':')[0]), 'measured_s2d_gbit': None}
                for label in ('2:60:tuned', '2:120:default', '2:20:default', '1:60:default', '2:0:default',
                              '2:60:default', '2:0:tuned')]
        self.assertEqual(report.speed_order(rows), ['1:60:default', '2:0:default', '2:0:tuned', '2:20:default',
                                                    '2:60:default', '2:60:tuned', '2:120:default'])

    def test_predictions_flag_a_shifted_session_and_an_input_off(self):
        S = session(anchors() + cell(60, 'default', 0.4, 1.7, full=45.0))
        p = exp13.Predictions(S, {}, shifted_session=True, drift={60: {'rate': 0.2, 'off': True}})
        self.assertEqual(p.flags('E4', '2:60:default'), ' [shifted session (A10); input off by more than 25%]')
        self.assertEqual(p.flags('E6', '2:60:tuned'), '')
        lines = exp13.Predictions(S).all(exp13.r1_reading(S), {}, {})
        text = '\n'.join(lines)
        for name in [f'E{i}' for i in range(1, 18)]:
            self.assertIn(f'- **{name}**', text)
        self.assertIn('full at 2:60:default 45.00 (written 41.00–52.00) held', text)


@needs_venv
class DryRun(unittest.TestCase):
    def run_main(self, tmp, sweeps, *extra):
        out = io.StringIO()
        with redirect_stdout(out):
            code = exp13.main(['--sweeps', *map(str, sweeps), '--datadir', str(Path(tmp) / 'data'), '--figdir',
                               str(Path(tmp) / 'figures'), *extra])
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_the_whole_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            sweeps = exp13_smoke_data.write_session(Path(tmp) / 'sweeps')
            text = self.run_main(tmp, sweeps)
            self.assertIn('254 runs from', text)
            for heading in ('## Runs', '## Visits (A11 (i))', '## Durability', '## Baselines', '## Ratios',
                            "## The ledger's prediction and rule", '## The DR verdict', '## Crossovers',
                            '## Added rules', '## Added predictions', '## The network', "## The model's ranges"):
                self.assertIn(heading, text)
            for rule in ['L1', 'L2', 'R1'] + [f'A{i}' for i in range(1, 18)] + [f'E{i}' for i in range(1, 18)]:
                self.assertIn(f'**{rule}**', text)
            # The synthetic session is the expected outcome: R1 fires on tuned hosts, rows 1, 2, 2, all provisional.
            self.assertIn('**20 ms: 1. Holds as shipped.**', text)
            self.assertIn('**60 ms: 2. Holds with tuned buffers.**', text)
            self.assertIn('**120 ms: 2. Holds with tuned buffers.**', text)
            self.assertEqual(text.count('**Provisional: R1 fired'), 3)
            self.assertIn('D36 is **contradicted**', text)
            data = Path(tmp) / 'data'
            with (data / 'exp13-runs.csv').open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 254)
            self.assertEqual(set(rows[0]), set(exp13.KEEP))
            with (data / 'exp13-cells.csv').open() as f:
                cells = list(csv.DictReader(f))
            self.assertTrue(any(c['workload'] == 'pooled' and c['method'] == 'full' for c in cells))
            self.assertTrue((Path(tmp) / 'figures' / 'exp13-rtt.svg').exists())
            self.assertTrue((Path(tmp) / 'figures' / 'exp13-ratios.svg').exists())

    def test_the_smoke_runs_shape_and_its_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            sweeps = exp13_smoke_data.write_session(Path(tmp) / 'sweeps', smoke=True)
            text = self.run_main(tmp, sweeps, '--smoke', '--tag=-smoke')
            self.assertIn(f'- {len(exp13.SMOKE_RECORDS)} of {len(exp13.SMOKE_RECORDS)} paths found', text)
            self.assertIn('smoke item 21', text)
            self.assertTrue((Path(tmp) / 'data' / 'exp13-smoke-runs.csv').exists())

        def drop(record):  # the sampler's reading lost: smoke item 14's record missing
            record.pop('ss', None)
        with tempfile.TemporaryDirectory() as tmp:
            sweeps = exp13_smoke_data.write_session(Path(tmp) / 'sweeps', smoke=True, adjust=drop,
                                                    invocations=('exp13-hot',))
            text = self.run_main(tmp, sweeps, '--smoke')
            self.assertIn('item 14: run ss', text)
            # Without ss the minrtt check can't be made: every run is unchecked, not an A11 exclusion.
            self.assertIn('0 run(s) excluded by the network checks', text)
            self.assertIn('**20 ms: Not judged.**', text)

    def test_the_emulator_overflowing_leaves_a_cell_not_measured(self):
        def overflow(record):
            if record['method'] == V1 and record['speed']['label'] == '2:60:default' and record['repeat'] < 2:
                for phase in record['phases'].values():
                    phase['qdisc']['source']['netem_own_drops'] = 5
        with tempfile.TemporaryDirectory() as tmp:
            sweeps = exp13_smoke_data.write_session(Path(tmp) / 'sweeps', adjust=overflow,
                                                    invocations=('exp13-uniform1', 'exp13-hot'))
            text = self.run_main(tmp, sweeps)
            # Two of hot's v1 runs at 60 ms default and two of uniform 1%'s.
            self.assertIn('4 run(s) excluded by the network checks', text)
            self.assertIn("netem's own drops on source: 5", text)
            self.assertIn('pgdelta_direct_inplace hot at 2:60:default (2 excluded, 3 counting)', text)
            self.assertIn('**60 ms: Not judged.**', text)
            self.assertIn('**20 ms: 1. Holds as shipped.**', text)

    def test_a_durability_failure_of_v1_reads_v1_not_durable(self):
        def dirty(record):
            if record['method'] == V1 and record['speed']['label'] == '2:20:default' and record['repeat'] < 2:
                record['durability'].update(a_ok=False, ok=False, dirty_writeback_increase_bytes=30_000_000)
        with tempfile.TemporaryDirectory() as tmp:
            sweeps = exp13_smoke_data.write_session(Path(tmp) / 'sweeps', adjust=dirty, invocations=('exp13-hot',))
            text = self.run_main(tmp, sweeps)
            self.assertIn('**20 ms: v1 not durable.**', text)
            self.assertIn('**120 ms: v1 not durable.**', text)
            self.assertIn('reclassified as not durable for the session: pgdelta_direct_inplace', text)


if __name__ == '__main__':
    unittest.main()
