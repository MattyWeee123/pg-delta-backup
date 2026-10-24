"""Tests for EXP-11's analysis (analysis/exp11.py) and analysis/exp04.crossover's baseline argument.

They need the analysis venv's matplotlib and are skipped without it. The dry runs write a synthetic
session (exp11_smoke_data) and every output into a temporary directory, never into docs/.
"""

import csv
import io
import json
import math
from pathlib import Path
import statistics
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from experiments.network import measure, report

import exp11_smoke_data

try:
    import matplotlib  # noqa: F401
except ImportError:  # the analysis scripts need the analysis venv
    matplotlib = None
if matplotlib is not None:
    sys.path.append(str(Path(__file__).resolve().parents[1] / 'analysis'))
    import exp04
    import exp11
    import plotting

U = measure.UNTHROTTLED
V1, V1_J8 = 'pgdelta_direct_inplace', 'pgdelta_direct_inplace_j8'
EXP09_CELLS = Path(__file__).resolve().parents[1] / 'docs' / 'experiments' / 'data' / 'exp09-10-cells.csv'
EXP09_RUNS = EXP09_CELLS.with_name('exp09-10-runs.csv')
needs_venv = unittest.skipUnless(matplotlib is not None, 'needs the analysis venv (matplotlib)')


def make_row(method, pattern='hot', speed=U, total=1.0, **fields):
    """One run as exp11.load() returns it: passing, verified and durable unless fields say otherwise."""
    row = {key: None for key in (*exp11.TEXT, *exp11.FLAGS, *exp11.NUMERIC)}
    row.update(sweep='s', index=0, trial_id=f'00-{pattern}', pattern=pattern, speed=speed, method=method,
               status='pass', block='main', repeat=0, verified=True, total_s=total, total_no_ssh_s=total,
               measured_s2d_gbit=exp11_smoke_data.RATES.get(speed), a_ok=True,
               b_ok=True if method in measure.FULL_COPY_METHODS else None, dirty_increase_bytes=0,
               dirty_limit_bytes=4 << 20, pbb_start_s=None, probackup_start_s=None)
    if method in measure.PARCOPY_METHODS:
        row.update(parcopy_fsync_calls=10, parcopy_fsync_expected=10, parcopy_balance=1.02)
    row.update(fields)
    return row


def runs(method, times, pattern='hot', speed=U, block='main', first=0, **fields):
    return [make_row(method, pattern, speed, t, block=block, repeat=first + i, **fields) for i, t in enumerate(times)]


def trial(pattern, floors=(1.4, 1.4)):
    return {'trial_id': f'00-{pattern}', 'pattern': pattern, 'reference_match': True,
            'probes': {'durable_write_floor': [{'seconds': s} for s in floors],
                       'read_floor': {'seconds': 0.75}}}


def session(rows, patterns=('hot', 'append')):
    return exp11.Session(rows, {('s', f'00-{p}'): trial(p) for p in patterns})


@needs_venv
class Exp04Crossover(unittest.TestCase):
    """analysis/exp04.crossover: its behaviour against the full copy, and a named baseline."""

    def table(self):
        # x (Gbit/s) per speed; m is faster than full at 2 and slower from 5 on; B is a faster baseline.
        return {('hot', '2g'): {'speed_gbit': 2.0, 'full': {'total_s': 10.0}, 'm': {'total_s': 5.0},
                                'B': {'total_s': 8.0}},
                ('hot', '5g'): {'speed_gbit': 5.0, 'full': {'total_s': 4.0}, 'm': {'total_s': 5.0},
                                'B': {'total_s': 3.0}},
                ('hot', 'unthrottled'): {'speed_gbit': 15.0, 'full': {'total_s': 3.0}, 'm': {'total_s': 5.0},
                                         'B': {'total_s': 2.0}}}

    @staticmethod
    def expected(s0, r0, s1, r1):
        t = -math.log(r0) / (math.log(r1) - math.log(r0))
        return math.exp(math.log(s0) + t * (math.log(s1) - math.log(s0)))

    def test_against_the_full_copy_by_default(self):
        value, how = exp04.crossover(self.table(), 'hot', 'm')
        self.assertEqual(how, 'crosses')
        self.assertAlmostEqual(value, self.expected(2.0, 0.5, 5.0, 1.25))

    def test_against_a_named_baseline(self):
        value, how = exp04.crossover(self.table(), 'hot', 'm', baseline='B')
        self.assertEqual(how, 'crosses')
        self.assertAlmostEqual(value, self.expected(2.0, 5 / 8, 5.0, 5 / 3))
        # A speed without the baseline is left out of the interpolation.
        table = self.table()
        del table[('hot', '5g')]['B']
        value, _ = exp04.crossover(table, 'hot', 'm', baseline='B')
        self.assertAlmostEqual(value, self.expected(2.0, 5 / 8, 15.0, 5 / 2))

    def test_bounds_and_missing_data(self):
        table = self.table()
        for cell in table.values():
            cell['fast'] = {'total_s': 1.0}
            cell['slow'] = {'total_s': 20.0}
        self.assertEqual(exp04.crossover(table, 'hot', 'fast'), (15.0, 'faster at every speed (up to 15.0)'))
        self.assertEqual(exp04.crossover(table, 'hot', 'slow'), (2.0, 'slower at every speed (from 2.00)'))
        self.assertEqual(exp04.crossover(table, 'hot', 'absent'), (None, 'no data'))
        self.assertEqual(exp04.crossover(table, 'hot', 'm', baseline='absent'), (None, 'no data'))
        # Slower at the slow speed and faster at the fast one: not the shape a crossover reads.
        table[('hot', '2g')]['odd'] = {'total_s': 11.0}
        table[('hot', 'unthrottled')]['odd'] = {'total_s': 1.0}
        self.assertEqual(exp04.crossover(table, 'hot', 'odd'), (None, 'not monotonic'))


@needs_venv
class BaselineChoice(unittest.TestCase):
    def test_the_lowest_pooled_median(self):
        self.assertEqual(exp11.choose({'a': [3.0, 3.0, 3.0], 'b': [2.0, 2.2, 2.1]})['arm'], 'b')
        self.assertIsNone(exp11.choose({'a': []}))

    def test_a_tie_within_50_ms_goes_to_the_lower_maximum(self):
        chosen = exp11.choose({'a': [2.00, 2.00, 2.50], 'b': [2.04, 2.04, 2.10]})
        self.assertEqual((chosen['arm'], chosen['tied']), ('b', ['a', 'b']))
        # 0.06 s apart is no tie: the lower median wins whatever the maxima.
        self.assertEqual(exp11.choose({'a': [2.00, 2.00, 2.50], 'b': [2.06, 2.06, 2.10]})['arm'], 'a')

    def test_the_envelopes_admit_arms_by_tier(self):
        self.assertEqual(set(exp11.envelope_arms('B_rel')),
                         {'full', 'full_syncfs', 'probackup8', 'probackup4'})
        self.assertEqual(set(exp11.envelope_arms('B_fast')),
                         {'full', 'full_syncfs', 'probackup8', 'probackup4', 'parcopy8', 'parcopy4'})
        self.assertEqual(set(exp11.envelope_arms('B_tuned')) - set(exp11.envelope_arms('B_fast')), {'full_wb'})
        self.assertEqual(exp11.envelope_arms('F0'), ['full'])
        # Matched parallelism: at most 4 streams, as v1 at 4 jobs.
        self.assertEqual(set(exp11.envelope_arms('B_fast', matched=True)),
                         {'full', 'full_syncfs', 'probackup4', 'parcopy4'})
        for name in exp11.ENVELOPES:
            self.assertNotIn('full_nosync', exp11.envelope_arms(name))
            self.assertNotIn(V1, exp11.envelope_arms(name))

    def test_pooled_over_the_workloads_on_the_main_block_only(self):
        rows = (runs('probackup8', [1.0] * 3, 'hot') + runs('probackup8', [3.0] * 3, 'append')
                + runs('full', [2.1] * 3, 'hot') + runs('full', [2.1] * 3, 'append')
                # Fast runs outside the main block don't enter the choice.
                + runs('full', [0.5] * 5, 'hot', block='extension', first=5)
                + runs('full', [0.5] * 3, 'hot', block='warm_source', first=10))
        S = session(rows)
        chosen = S.choice('B_rel', U)
        self.assertEqual((chosen['arm'], chosen['median'], chosen['n']), ('probackup8', 2.0, 6))
        # Compared per workload against the chosen arm's median in that trial.
        self.assertEqual(S.baseline('B_rel', 'hot', U), ('probackup8', 1.0))
        self.assertEqual(S.baseline('B_rel', 'append', U), ('probackup8', 3.0))

    def test_void_runs_and_failed_or_undurable_arms_are_left_out(self):
        rows = (runs('full', [3.0] * 5)
                + runs('probackup8', [2.0] * 5, void=True, status='fail', logical_match=False)
                # A4: one failed parcopy run excludes the arm from every baseline.
                + runs('parcopy8', [1.0] * 4) + runs('parcopy8', [1.0], status='fail', first=4)
                # A5: two durability failures at one speed in one trial reclassify the arm.
                + runs('probackup4', [2.5] * 3) + runs('probackup4', [2.5] * 2, a_ok=False, first=3))
        S = session(rows)
        self.assertEqual(S.choice('B_fast', U)['arm'], 'full')
        self.assertIn('parcopy8', S.excluded)
        self.assertTrue(S.excluded['parcopy8'].startswith('A4'))
        self.assertTrue(S.excluded['probackup4'].startswith('A5'))
        # Void runs come back in the flagged view; they are not A4 failures.
        self.assertEqual(S.choice('B_fast', U, void=True)['arm'], 'probackup8')
        self.assertNotIn('probackup8', S.excluded)

    def test_a_copy_the_tool_aborted_fails_a4_and_an_error_of_the_harness_does_not(self):
        parcopy = runs('parcopy8', [1.0] * 4) + runs('parcopy8', [None], status='error', failure_kind='tool', first=4,
                                                      error='CommandError: dest: python3 exited 3')
        rows = runs('full', [3.0] * 5) + runs('probackup8', [2.5] * 5) + parcopy
        S = session(rows)
        self.assertEqual(list(S.a4), ['parcopy8'])
        self.assertIn('a copy the tool itself aborted', S.excluded['parcopy8'])
        self.assertEqual(S.choice('B_fast', U)['arm'], 'probackup8')
        self.assertEqual((S.bound()['measured'], S.bound()['excluded']), (False, ['parcopy8']))
        # An error of the harness around the copy fails nothing; the run is only left out.
        parcopy[-1]['failure_kind'] = 'harness'
        S = session(rows)
        self.assertEqual((S.a4, S.a4_undecided, S.excluded), ({}, {}, {}))
        self.assertEqual(S.choice('B_fast', U)['arm'], 'parcopy8')
        self.assertEqual(S.rows[-1]['left_out'], 'status error')
        # One whose kind isn't recorded leaves A4 undecided for its arm, which stays in the baselines.
        parcopy[-1]['failure_kind'] = None
        S = session(rows)
        self.assertEqual((S.a4, list(S.a4_undecided), S.excluded), ({}, ['parcopy8'], {}))
        text = '\n'.join(exp11.a_lines(S, [], {'row': 'Out on fast links'}, {'row': 'Out on fast links'}))
        self.assertIn('**undecided** for parcopy8: 1 errored run(s) whose cause isn\'t recorded', text)
        self.assertNotIn('does not fire', text.split('**A4**')[1].split('\n')[0])
        self.assertIn('**A4 is undecided** for parcopy8', '\n'.join(exp11.verdict_lines(S)))

    def test_an_errored_runs_kind_from_its_record(self):
        row = {'status': 'error', 'failure_kind': None}
        failed = {'phases': {measure.DIRECT_PHASE: {'failures': ['dest: python3 exited 3']}}}
        self.assertEqual(exp11.failure_kind(failed, row), 'tool')  # a record from before the harness wrote it
        self.assertEqual(exp11.failure_kind({'phases': {}}, row), 'harness')
        self.assertIsNone(exp11.failure_kind({}, row))  # a summary without its record can't tell
        self.assertEqual(exp11.failure_kind({'failure_kind': 'harness', **failed}, row), 'harness')
        self.assertEqual(exp11.failure_kind({}, {**row, 'failure_kind': 'tool'}), 'tool')
        self.assertIsNone(exp11.failure_kind(failed, {**row, 'status': 'pass'}))

    def test_a_void_run_is_found_from_the_reference_digests_too(self):
        rows = runs('probackup8', [2.0], status='fail', logical_match=False)
        S = exp11.Session(rows, {('s', '00-hot'): {**trial('hot'), 'reference_match': False}})
        self.assertTrue(S.rows[0]['void'])
        self.assertEqual(S.a4, {})
        S = exp11.Session(runs('probackup8', [2.0], status='fail', logical_match=False), {('s', '00-hot'): trial('hot')})
        self.assertFalse(S.rows[0]['void'])
        self.assertIn('probackup8', S.a4)

    def test_check_a_is_void_once_the_negative_control_passes_it(self):
        rows = runs('full', [3.0], a_ok=False) + runs('full_nosync', [2.0], a_ok=False, b_ok=False)
        self.assertFalse(session(rows).rows[0]['usable'])
        rows = runs('full', [3.0], a_ok=False) + runs('full_nosync', [2.0], a_ok=True, b_ok=False)
        S = session(rows)
        self.assertTrue(S.a_void)
        self.assertTrue(S.rows[0]['usable'])
        self.assertTrue(S.rows[1]['usable'])  # full_nosync is the control: never left out for durability

    def test_a_wrong_fsync_count_or_an_unchecked_run_is_left_out(self):
        rows = runs('parcopy8', [1.0], parcopy_fsync_calls=9) + runs('full', [3.0], b_ok=None)
        S = session(rows)
        self.assertEqual(S.rows[0]['left_out'], 'durability failed: fsync count')
        self.assertEqual(S.rows[1]['left_out'], 'durability unchecked: b')


def ratios(fast, rel, adj_fast=None, adj_rel=None, **views):
    """Session.ratios()-shaped entries for hot and append from (hot, append) pairs."""
    adj_fast, adj_rel = adj_fast or fast, adj_rel or rel
    out = {}
    for i, p in enumerate(('hot', 'append')):
        out[p] = {'B_fast': {'median': fast[i], 'adj': adj_fast[i]}, 'B_rel': {'median': rel[i], 'adj': adj_rel[i]}}
        for view, (f, r) in views.items():
            out[p]['B_fast'].update({view: f[i], f'adj_{view}': f[i]})
            out[p]['B_rel'].update({view: r[i], f'adj_{view}': r[i]})
    return out


MEASURED = {'measured': True, 'passes': True, 'is_fast': True}
MISSING = {'measured': False, 'passes': None, 'is_fast': False}


@needs_venv
class Verdict(unittest.TestCase):
    def row(self, r, bound=MEASURED):
        return exp11.verdict(exp11.deciding(r), bound)

    def test_out_is_judged_against_the_fastest_copy(self):
        v = self.row(ratios((1.20, 0.80), (0.95, 0.95)))
        self.assertEqual(v, {'row': 'Out on fast links', 'qualifier': False})
        # The qualifier: R_rel and R_adj against B_rel at most 0.90 in both workloads.
        v = self.row(ratios((1.20, 1.10), (0.85, 0.88)))
        self.assertEqual(v, {'row': 'Out on fast links', 'qualifier': True})
        v = self.row(ratios((1.20, 1.10), (0.85, 0.88), adj_rel=(0.85, 0.91)))
        self.assertFalse(v['qualifier'])
        # Out comes first: it holds even where the bound is missing and the released readings would pass.
        self.assertEqual(self.row(ratios((1.0, 0.5), (0.8, 0.8)), MISSING)['row'], 'Out on fast links')

    def test_confirmed_needs_both_workloads_both_adjusted_ratios_and_a_bound_that_passes(self):
        r = ratios((0.85, 0.88), (0.80, 0.80), adj_fast=(0.86, 0.89), adj_rel=(0.85, 0.85))
        self.assertEqual(self.row(r)['row'], 'Confirmed')
        self.assertEqual(self.row(r, {**MEASURED, 'passes': None})['row'], 'Undecided')
        self.assertEqual(self.row(ratios((0.85, 0.88), (0.80, 0.80), adj_fast=(0.86, 0.91)))['row'], 'Undecided')
        self.assertEqual(self.row(ratios((0.85, 0.88), (0.80, 0.80), adj_rel=(0.85, 0.92)))['row'], 'Undecided')

    def test_bound_missing(self):
        r = ratios((0.95, 0.95), (0.85, 0.88))
        self.assertEqual(self.row(r, MISSING)['row'], 'Bound missing')
        # A bound that fails its gate while a released arm is faster counts as missing ...
        self.assertEqual(self.row(r, {'measured': True, 'passes': False, 'is_fast': False})['row'], 'Bound missing')
        # ... but not while it is still B_fast (it stays there; only Confirmed is blocked).
        self.assertEqual(self.row(r, {'measured': True, 'passes': False, 'is_fast': True})['row'], 'Undecided')
        # It needs the released readings at most 0.90 in both workloads.
        self.assertEqual(self.row(ratios((0.95, 0.95), (0.85, 0.92)), MISSING)['row'], 'Undecided')
        # With Confirmed's ratios but no bound, the row is Bound missing, not Confirmed.
        self.assertEqual(self.row(ratios((0.85, 0.85), (0.85, 0.85)), MISSING)['row'], 'Bound missing')

    def test_undecided_and_not_judged(self):
        self.assertEqual(self.row(ratios((0.95, 0.92), (0.95, 0.95)))['row'], 'Undecided')
        self.assertEqual(self.row(ratios((0.95, None), (0.95, 0.95)))['row'], 'Not judged')
        self.assertEqual(self.row(ratios((1.05, None), (0.95, 0.95)))['row'], 'Out on fast links')

    def test_near_the_line_flags_only_a_deciding_ratio(self):
        # hot's R_fast decides Out alone: 1.02 is near 1.00.
        r = exp11.deciding(ratios((1.02, 0.80), (0.95, 0.95)))
        self.assertEqual(exp11.near_line(r, MEASURED), [('hot', 'fast', 1.02, 1.00)])
        # With append far above 1.00, hot's 1.02 decides nothing.
        self.assertEqual(exp11.near_line(exp11.deciding(ratios((1.02, 1.50), (0.95, 0.95))), MEASURED), [])
        # R_rel at 0.89 decides the qualifier beside Out.
        found = exp11.near_line(exp11.deciding(ratios((1.20, 1.20), (0.89, 0.85))), MEASURED)
        self.assertEqual([(p, k) for p, k, *_ in found], [('hot', 'rel'), ('hot', 'adj_rel')])
        # 0.04 from the line is not near it.
        self.assertEqual(exp11.near_line(exp11.deciding(ratios((1.04, 0.80), (0.95, 0.95))), MEASURED), [])

    def test_near_the_line_flags_ratios_that_decide_together(self):
        # Both workloads just above 1.00: neither alone decides Out, both together do.
        r = exp11.deciding(ratios((1.02, 1.01), (0.95, 0.95), adj_rel=(1.0, 1.0)))
        self.assertEqual(exp11.verdict(r, MEASURED)['row'], 'Out on fast links')
        self.assertEqual(exp11.near_line(r, MEASURED), [('hot', 'fast', 1.02, 1.00), ('append', 'fast', 1.01, 1.00)])
        # R_rel and R_adj against B_rel just above 0.90 in both: together they decide the qualifier.
        r = exp11.deciding(ratios((1.3, 1.3), (0.91, 0.91), adj_rel=(0.92, 0.92)))
        self.assertEqual(exp11.verdict(r, MEASURED), {'row': 'Out on fast links', 'qualifier': False})
        self.assertEqual([(p, k) for p, k, *_ in exp11.near_line(r, MEASURED)],
                         [('hot', 'rel'), ('hot', 'adj_rel'), ('append', 'rel'), ('append', 'adj_rel')])
        # One of them far from the line: the others can't decide the qualifier, whatever they do.
        r = exp11.deciding(ratios((1.3, 1.3), (0.91, 0.91), adj_rel=(0.92, 0.99)))
        self.assertEqual(exp11.near_line(r, MEASURED), [])
        # Undecided with R_fast and R_adj just above 0.90 in both: all four together decide Confirmed.
        r = exp11.deciding(ratios((0.92, 0.91), (0.85, 0.85)))
        self.assertEqual(exp11.verdict(r, MEASURED)['row'], 'Undecided')
        self.assertEqual([(p, k, line) for p, k, _, line in exp11.near_line(r, MEASURED)],
                         [('hot', 'fast', 0.90), ('hot', 'adj_fast', 0.90), ('append', 'fast', 0.90),
                          ('append', 'adj_fast', 0.90)])
        # R_fast just under 1.00 in one and just above 0.90 in the other: only the first can change the row,
        # since Confirmed needs the first at 0.90 too.
        r = exp11.deciding(ratios((0.98, 0.92), (0.85, 0.85)))
        self.assertEqual([(p, k, line) for p, k, _, line in exp11.near_line(r, MEASURED)], [('hot', 'fast', 1.00)])

    def test_fragile_when_a_robustness_view_changes_the_row(self):
        steady = ratios((0.95, 0.95), (0.95, 0.95), max=((0.97, 0.96), (0.97, 0.97)),
                        no_ssh=((0.94, 0.94), (0.94, 0.94)), paired=((0.95, 0.95), (0.95, 0.95)))
        self.assertEqual(exp11.fragile(steady, MEASURED), [])
        shaky = ratios((0.95, 0.95), (0.95, 0.95), max=((1.01, 0.96), (0.97, 0.97)),
                       no_ssh=((0.94, 0.94), (0.94, 0.94)), paired=((0.95, 0.95), (0.95, 0.95)))
        self.assertEqual(exp11.fragile(shaky, MEASURED), [('R_max', 'Out on fast links')])

    def test_flags_and_rows_in_the_report(self):
        r = ratios((1.02, 0.80), (0.95, 0.95), max=((0.99, 0.81), (0.96, 0.96)), no_ssh=((1.02, 0.8), (0.95, 0.95)),
                   paired=((1.02, 0.8), (0.95, 0.95)))
        v, lines = exp11.ha_lines(r, MEASURED, 'v1 at 4 jobs')
        text = '\n'.join(lines)
        self.assertEqual(v['row'], 'Out on fast links')
        self.assertLess(text.index('| Out on fast links'), text.index('| Confirmed'))
        self.assertLess(text.index('| Confirmed'), text.index('| Bound missing'))
        self.assertLess(text.index('| Bound missing'), text.index('| Undecided'))
        self.assertIn('hot: "Out against the fastest copy measured; R_rel = 0.95"', text)
        self.assertIn('*Fragile*: R_max would give Undecided', text)
        self.assertIn('*Near the line*: hot fast 1.020 near 1.00', text)
        _, lines = exp11.ha_lines(ratios((1.2, 1.2), (0.85, 0.85), max=((1.2, 1.2), (0.85, 0.85)),
                                         no_ssh=((1.2, 1.2), (0.85, 0.85)), paired=((1.2, 1.2), (0.85, 0.85))),
                                  MEASURED, 'v1 at 4 jobs')
        self.assertIn('Qualifier: v1 is faster than the released copiers tested here', '\n'.join(lines))
        _, lines = exp11.ha_lines(ratios((0.95, 0.95), (0.85, 0.85), max=((0.95, 0.95), (0.85, 0.85)),
                                         no_ssh=((0.95, 0.95), (0.85, 0.85)), paired=((0.95, 0.95), (0.85, 0.85))),
                                  MISSING, 'v1 at 4 jobs')
        self.assertIn(exp11.BOUND_MISSING, '\n'.join(lines))


@needs_venv
class Ratios(unittest.TestCase):
    def rows(self):
        return (runs(V1, [2.7] * 10) + runs('probackup8', [3.0] * 10) + runs('parcopy8', [2.0] * 10)
                + runs('full', [3.1] * 10, backup_mode_adder_s=0.14) + runs(V1, [2.7] * 10, 'append')
                + runs('probackup8', [3.0] * 10, 'append') + runs('parcopy8', [2.0] * 10, 'append')
                + runs('full', [3.1] * 10, 'append', backup_mode_adder_s=0.14))

    def test_r_adj_adds_the_adder_only_against_a_live_source(self):
        r = session(self.rows()).ratios('hot')
        self.assertAlmostEqual(r['B_fast']['median'], 2.7 / 2.0)
        self.assertAlmostEqual(r['B_fast']['adj'], 2.7 / 2.0)  # parcopy reads a stopped source: R_adj is R
        self.assertAlmostEqual(r['B_rel']['median'], 2.7 / 3.0)
        self.assertAlmostEqual(r['B_rel']['adj'], (2.7 + 0.14) / 3.0)
        self.assertAlmostEqual(r['F0']['adj'], (2.7 + 0.14) / 3.1)
        self.assertEqual(r['B_rel']['n_pairs'], 10)
        self.assertAlmostEqual(r['B_rel']['adj_paired'], (2.7 + 0.14) / 3.0)

    def test_the_verdict_uses_main_and_extension_runs(self):
        rows = runs(V1, [2.7] * 5) + runs(V1, [3.5] * 5, block='extension', first=5) + runs('full', [3.0] * 5)
        rows += runs(V1, [1.0] * 3, block='warm_source', first=10)
        r = session(rows).ratios('hot')
        self.assertAlmostEqual(r['v1'], (2.7 + 3.5) / 2)  # 10 runs, the warm-source block left out
        self.assertEqual(r['n_v1'], 10)

    def test_the_bound_and_its_gate(self):
        rows = self.rows()
        S = session(rows)
        bound = S.bound()
        self.assertEqual((bound['arm'], bound['n'], bound['passes'], bound['is_fast']), ('parcopy8', 20, True, True))
        # More than floor(U) + 1.0 s fails the gate.
        S = exp11.Session(rows, {('s', f'00-{p}'): trial(p, floors=(0.9, 0.9)) for p in ('hot', 'append')})
        self.assertIs(S.bound()['passes'], False)
        # So does a busiest stream above 1.1 x the mean in any run.
        rows = self.rows()
        rows[25]['parcopy_balance'] = 1.2
        self.assertIs(session(rows).bound()['passes'], False)
        # Without floor probes the gate can't pass.
        self.assertIsNone(exp11.Session(self.rows(), {}).bound()['passes'])
        self.assertFalse(session(runs('full', [3.0])).bound()['measured'])


@needs_venv
class Crossovers(unittest.TestCase):
    def rows(self, parcopy4=1.80):
        out = []
        for speed, full, parcopy, v1 in (('2g', 10.2, 9.3, 2.7), ('5g', 5.0, 4.0, 2.7), ('10g', 3.2, 2.3, 2.7),
                                          (U, 3.1, 1.9, 2.7)):
            out += runs('full', [full] * 3, speed=speed) + runs('parcopy8', [parcopy] * 3, speed=speed)
            out += runs(V1, [v1] * 3, speed=speed)
        return out + runs('parcopy4', [parcopy4] * 3)

    def test_v1_against_a_chosen_baseline(self):
        S = session(self.rows(parcopy4=2.5), ('hot',))
        table = S.crossover_table()
        self.assertEqual(table[('hot', '10g')]['B_fast'], {'total_s': 2.3, 'arm': 'parcopy8', 'max': 2.3})
        self.assertEqual(table[('hot', U)]['B_fast']['arm'], 'parcopy8')
        value, how = exp04.crossover(table, 'hot', V1, baseline='B_fast')
        x5, x10 = exp11_smoke_data.RATES['5g'], exp11_smoke_data.RATES['10g']
        expected = Exp04Crossover.expected(x5, 2.7 / 4.0, x10, 2.7 / 2.3)
        self.assertEqual(how, 'crosses')
        self.assertAlmostEqual(value, expected)
        self.assertEqual(exp11.bracketing(table, 'hot', V1, 'B_fast'), ['5g', '10g'])
        self.assertEqual(S.provisional('B_fast', ['5g', '10g']), [])
        # Against F0 v1 is faster everywhere: no crossover.
        self.assertTrue(exp04.crossover(table, 'hot', V1, baseline='F0')[1].startswith('faster'))
        # The link-time model: a full copy's bytes at the measured rate (none known here).
        self.assertNotIn('link_model', table[('hot', U)])

    def test_provisional_when_an_arm_run_only_at_some_speeds_might_have_won(self):
        S = session(self.rows(parcopy4=1.80), ('hot',))
        self.assertEqual(S.choice('B_fast', U)['arm'], 'parcopy4')
        self.assertEqual(S.provisional('B_fast', ['5g', '10g']), ['parcopy4 not run at 5 Gbit/s, 10 Gbit/s'])
        self.assertEqual(S.provisional('F0', ['5g', '10g']), [])

    def test_intervals_and_l3(self):
        self.assertEqual(exp11.interval(3.0, 'crosses'), (3.0, 3.0))
        self.assertEqual(exp11.interval(1.92, 'slower at every speed (from 1.92)'), (0.0, 1.92))
        self.assertEqual(exp11.interval(15.0, 'faster at every speed (up to 15.0)'), (15.0, math.inf))
        self.assertIsNone(exp11.interval(None, 'no data'))
        self.assertTrue(exp11.l3_holds((2.5, 'crosses'), (3.0, 'crosses')))
        self.assertFalse(exp11.l3_holds((2.3, 'crosses'), (3.0, 'crosses')))
        below = (1.92, 'slower at every speed (from 1.92)')
        # Below 2 is judged only where 0.8 x the F0 crossover is 2 Gbit/s or more.
        self.assertFalse(exp11.l3_holds(below, (2.6, 'crosses')))
        self.assertIsNone(exp11.l3_holds(below, (2.4, 'crosses')))
        self.assertIsNone(exp11.l3_holds(below, below))
        self.assertTrue(exp11.inside((4.0, 'crosses'), 3.0, 4.5))
        self.assertFalse(exp11.inside((4.9, 'crosses'), 3.0, 4.5))
        self.assertFalse(exp11.inside(below, 3.0, 4.5))
        self.assertTrue(exp11.inside((15.0, 'faster at every speed (up to 15.0)'), 10, math.inf))


@needs_venv
class DriftGates(unittest.TestCase):
    EXP09 = {'cells': {(p, s, m): {'total_s': t, 'wire_s2d_bytes': w}
                       for p in ('hot',) for s, full in (('2g', 10.0), ('5g', 5.0), (U, 3.0))
                       for m, t, w in (('full', full, 2.1e9), (V1, 2.7, 29e6))},
              'link': {('hot', '2g'): 1.917, ('hot', '5g'): 4.792, ('hot', U): 15.05}}

    def rows(self, full=(10.0, 5.0, 3.0), v1=2.7, link=1.0):
        out = []
        for speed, value in zip(('2g', '5g', U), full):
            rate = exp11_smoke_data.RATES[speed] * link
            out += runs('full', [value] * 5, speed=speed, wire_s2d_bytes=2.1e9, measured_s2d_gbit=rate)
            out += runs(V1, [v1] * 5, speed=speed, wire_s2d_bytes=29e6, measured_s2d_gbit=rate)
        return out

    def gates(self, **kwargs):
        return exp11.drift_gates(session(self.rows(**kwargs), ('hot',)), self.EXP09)

    def test_within_every_gate(self):
        gates = self.gates()
        self.assertEqual(len(gates), 15)
        self.assertTrue(all(g['ok'] for g in gates))
        self.assertFalse(exp11.shifted(gates))

    def test_each_gate_has_its_own_tolerance(self):
        # full: ±5% at 2 and 5 Gbit/s, ±10% unthrottled.
        failed = [(g['speed'], g['gate']) for g in self.gates(full=(10.6, 5.2, 3.27)) if not g['ok']]
        self.assertEqual(failed, [('2g', 'full total_s')])
        self.assertTrue(exp11.shifted(self.gates(full=(10.0, 5.0, 3.4))))
        # v1: ±10% everywhere.
        self.assertFalse(exp11.shifted(self.gates(v1=2.95)))
        self.assertTrue(exp11.shifted(self.gates(v1=3.0)))
        # The measured link: ±2% at 2 and 5 Gbit/s, ±5% unthrottled.
        failed = {(g['speed'], g['gate']) for g in self.gates(link=1.03) if not g['ok']}
        self.assertEqual(failed, {('2g', 'measured link'), ('5g', 'measured link')})

    def test_pgdelta_since_exp09_beside_the_gates(self):
        since = {'base': 'b77f33d8cd30b35ba6e9ded9976dc4c7b233c2b5', 'diff_sha256': 'ab' * 32,
                 'files': [{'path': 'pgdelta/receiver.py', 'added': 19, 'removed': 11},
                           {'path': 'pgdelta/README.md', 'added': 6, 'removed': 6}]}
        runs = [{'name': 'exp11-local', 'environment': {'dest': {'deployed': {'pgdelta_since_exp09': since}}}},
                {'name': 'exp11-uniform', 'environment': {'source': {'deployed': {'pgdelta_since_exp09': {
                    **since, 'files': []}}}}},
                {'name': 'older'},
                {'name': 'shallow', 'environment': {'dest': {'deployed': {'pgdelta_since_exp09': {
                    'base': since['base'], 'error': 'fatal: bad revision'}}}}}]
        self.assertEqual(exp11.code_lines(exp11.Session([], runs=runs)), [
            "- pgdelta/ since EXP-09's deployed commit b77f33d (exp11-local): 2 file(s) changed: pgdelta/receiver.py "
            '+19 −11, pgdelta/README.md +6 −6; diff SHA-256 abababababab',
            "- pgdelta/ since EXP-09's deployed commit b77f33d (exp11-uniform): unchanged",
            "- pgdelta/ since EXP-09's deployed commit (older): not recorded (deployed without it)",
            "- pgdelta/ since EXP-09's deployed commit b77f33d (shallow): git diff failed: fatal: bad revision"])

    def test_no_gate_checked_is_shifted(self):
        gates = exp11.drift_gates(session(runs('full', [3.0], 'append'), ('append',)), self.EXP09)
        self.assertTrue(all(g['ok'] is None for g in gates))
        self.assertTrue(exp11.shifted(gates))

    def test_l3_and_l4_are_not_judged_when_shifted(self):
        S = session(self.rows(full=(12.0, 5.0, 3.0)), ('hot',))
        exp09 = {'cells': {**self.EXP09['cells'], **{('hot', s, 'pgdelta'): {'total_s': 7.2, 'transfer_s': 3.9}
                                                     for s in ('2g', '5g', U)}}, 'link': self.EXP09['link']}
        lines, _ = exp11.ledger_lines(S, S.crossover_table(exp09), exp11.drift_gates(S, exp09))
        text = '\n'.join(lines)
        self.assertIn('**L3**', text)
        self.assertEqual(text.count('not judged (A7: the session is shifted)'), 2)
        lines, _ = exp11.ledger_lines(S, S.crossover_table(exp09), exp11.drift_gates(session(self.rows(), ('hot',)), exp09))
        self.assertNotIn('not judged (A7', '\n'.join(lines))


@needs_venv
class DryRun(unittest.TestCase):
    """The whole script on synthetic sessions, writing only into a temporary directory."""

    def run_script(self, tmp, sweeps, *extra):
        out = io.StringIO()
        figures = Path(tmp) / 'figures'
        with mock.patch.object(plotting, 'FIGURES', figures), redirect_stdout(out):
            code = exp11.main(['--sweeps', *map(str, sweeps), '--datadir', str(Path(tmp) / 'data'),
                               '--figdir', str(figures), '--exp09-cells', str(EXP09_CELLS),
                               '--exp09-runs', str(EXP09_RUNS), *extra])
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_the_whole_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            sweeps = exp11_smoke_data.write_session(Path(tmp) / 'results')
            audit = Path(tmp) / 'audit.json'
            audit.write_text(json.dumps({'arms': {
                'full': {'passed': True, 'uncovered': []},
                'probackup8': {'passed': True, 'uncovered': [],
                               'tool_uncovered': ['pg_wal/000000010000000000000003', 'backup_label', 'pg_wal']}}}))
            text = self.run_script(tmp, sweeps, '--audit', str(audit))
            for name in ('exp11-runs.csv', 'exp11-cells.csv'):
                self.assertTrue((Path(tmp) / 'data' / name).exists())
            for name in ('exp11-baselines.svg', 'exp11-phases.svg'):
                self.assertTrue((Path(tmp) / 'figures' / name).exists())
                self.assertIn(str(Path(tmp) / 'figures' / name), text)
            with open(Path(tmp) / 'data' / 'exp11-runs.csv', newline='') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 468)
            self.assertEqual(sum(row['block'] == 'extension' for row in rows), 80)
            with open(Path(tmp) / 'data' / 'exp11-cells.csv', newline='') as f:
                cells = list(csv.DictReader(f))
            ha = [c for c in cells if c['view'] == 'ha' and c['method'] == 'full' and c['pattern'] == 'hot']
            self.assertEqual([c['n'] for c in ha], ['10'])
        # Every registered and added prediction and rule, with its verdict.
        for name in ('L1', 'L2 (i)', 'L2 (ii)', 'L3', 'L4', 'L5', *(f'E{i}' for i in range(1, 16)),
                     *(f'A{i}' for i in range(1, 10))):
            self.assertIn(f'**{name}**', text)
        self.assertIn('**Verdict (v1 at 4 jobs): Out on fast links.**', text)
        self.assertIn('**Verdict (v1 at 8 jobs): Out on fast links.**', text)
        self.assertIn('**Verdict (against B_tuned): Out on fast links.**', text)
        for flag in ('*Fragile*', '*Near the line*', '*With void runs*: no void runs'):
            self.assertIn(flag, text)
        self.assertIn('the cut for T5 and T13', text)
        self.assertIn('**A7** (drift gates against EXP-09): every gate holds', text)
        self.assertIn("- pgdelta/ since EXP-09's deployed commit (exp11-local): not recorded", text)
        self.assertIn('**L3** (', text)
        self.assertNotIn('not judged (A7', text)
        # A JSON without the audit's counts: the number of example paths, said to be examples only.
        self.assertIn('| probackup8 | yes | 0 | 3 paths, examples only (pg_wal/000000010000000000000003, backup_label, '
                      'pg_wal) |', text)
        self.assertIn('pg_probackup\'s own flush leaves WAL and backup_label to its sync -f held', text)

    def test_the_smoke_runs_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = self.run_script(tmp, exp11_smoke_data.write_session(Path(tmp) / 'results', smoke=True),
                                   '--tag=-smoke')
            self.assertTrue((Path(tmp) / 'data' / 'exp11-smoke-runs.csv').exists())
            self.assertTrue((Path(tmp) / 'figures' / 'exp11-phases-smoke.svg').exists())
        self.assertIn('the session is **shifted**', text)
        self.assertIn('**L3** (', text)
        self.assertIn('not judged (A7: the session is shifted)', text)

    def test_void_runs_are_flagged_and_the_verdict_given_with_them(self):
        def void(record):
            if record['method'] == 'probackup8' and record['block'] == 'extension' and record['pattern'] == 'hot':
                record['status'] = 'fail'
                record['verification'].update(void=True, logical_match=False)
        with tempfile.TemporaryDirectory() as tmp:
            text = self.run_script(tmp, exp11_smoke_data.write_session(Path(tmp) / 'results', adjust=void))
        self.assertIn('*With void runs*: trials 00-hot-f0.01-s70 have void pg_probackup runs', text)
        self.assertIn('**Verdict (with void runs, flagged):', text)
        self.assertIn('### With void runs included (flagged)', text)
        self.assertIn('void runs (not failures): 5', text)

    def test_copies_the_tool_aborted_exclude_their_arm(self):
        def abort(record):
            # parcopy8's receiver caught a dropped range and exited 3, so the run ended in the tool's error before
            # any verification, in records from before the harness wrote its kind.
            if record['method'] == 'parcopy8' and record['speed']['label'] == U and record['repeat'] < 2:
                record['phases'][measure.DIRECT_PHASE]['failures'] = ['dest: python3 exited 3',
                                                                      'source: python3 exited 3']
                record.update(status='error', error='CommandError: dest: python3 exited 3; source: python3 exited 3',
                              output_bytes=None)
                del record['verification']
        with tempfile.TemporaryDirectory() as tmp:
            sweeps = exp11_smoke_data.write_session(Path(tmp) / 'results', adjust=abort)
            text = self.run_script(tmp, sweeps)
            rows, trials = [], {}
            for sweep in sweeps:
                loaded, found, _ = exp11.load(sweep)
                rows += loaded
                trials.update(found)
        S = exp11.Session(rows, trials)
        # Main-block repeats 0 and 1, unthrottled, in each of the three trials.
        self.assertIn('left out of every baseline: parcopy8: A4: 6 run(s) failed verify_direct or did not start, 6 of '
                      'them a copy the tool itself aborted', text)
        self.assertIn('fires for parcopy8: 00-hot-f0.01-s70 r0 unthrottled (the copy itself failed)', text)
        self.assertNotIn('does not fire; void runs', text)
        self.assertIn('  - error (the tool): 00-hot-f0.01-s70 r0 unthrottled parcopy8', text)
        self.assertEqual(S.choice('B_fast', U)['arm'], 'parcopy4')
        self.assertEqual(S.bound()['arm'], 'parcopy4')

    def test_check_a_is_judged_again_with_the_smoke_runs_noise(self):
        def noisy(record):
            # 5 MiB more Dirty + Writeback, in full's unthrottled runs in hot (main and extension): over the
            # 4 MiB the sweep judged with, under the 6 MiB idle noise the smoke run measured.
            if record['method'] == 'full' and record['pattern'] == 'hot' and record['speed']['label'] == U:
                memory = record['phases'][measure.FULL_PHASE]['memory']['dest']
                memory['dirty_writeback_bytes_after'] = memory['dirty_writeback_bytes_before'] + (5 << 20)
        with tempfile.TemporaryDirectory() as tmp:
            [sweep] = exp11_smoke_data.write_session(Path(tmp) / 'results', smoke=True, adjust=noisy)
            as_run = exp11.Session(exp11.load(sweep)[0])
            again = exp11.Session(exp11.load(sweep, 6 << 20)[0], idle_noise=6 << 20)
            text = self.run_script(tmp, [sweep], '--tag=-smoke', '--idle-noise-bytes', str(6 << 20))
        noisy_runs = [r for r in as_run.rows if r['method'] == 'full' and r['pattern'] == 'hot' and r['speed'] == U]
        self.assertEqual(len(noisy_runs), 3)  # the main block, the extension and the warm-source block
        self.assertEqual({(r['a_ok'], r['dirty_limit_bytes']) for r in noisy_runs}, {(False, 4 << 20)})
        self.assertIn('full', as_run.not_durable)  # A5: more than one failing run at one speed in one trial
        rows = [r for r in again.rows if r['method'] == 'full' and r['pattern'] == 'hot' and r['speed'] == U]
        self.assertEqual({(r['a_ok'], r['dirty_limit_bytes']) for r in rows}, {(True, 6 << 20)})
        self.assertEqual(again.not_durable, {})
        self.assertFalse(any(r['a_ok'] for r in again.rows if r['method'] == 'full_nosync'))  # still the control
        self.assertIn("judged again here with the smoke run's 6.00 MiB (--idle-noise-bytes)", text)
        self.assertNotIn('left out of every baseline: full:', text)

    def test_errors_whose_kind_is_unknown_leave_a4_undecided(self):
        def abort(record):
            if record['method'] == 'parcopy8' and record['speed']['label'] == U and record['repeat'] == 0:
                record.update(status='error', error='CommandError: dest: python3 exited 3')
        with tempfile.TemporaryDirectory() as tmp:
            sweeps = exp11_smoke_data.write_session(Path(tmp) / 'results', adjust=abort)
            for sweep in sweeps:  # the summary alone: no record to tell the tool's errors from the harness's
                for record in (sweep / 'runs').rglob('*.json'):
                    record.unlink()
            text = self.run_script(tmp, sweeps)
        self.assertIn('**undecided** for parcopy8: 3 errored run(s) whose cause isn\'t recorded', text)
        self.assertIn('**A4 is undecided** for parcopy8 (3 errored run(s)', text)
        self.assertIn('  - error (cause not recorded): 00-hot-f0.01-s70 r0 unthrottled parcopy8', text)

    def test_durability_failures_are_left_out_and_reclassify_an_arm(self):
        def fail(record):
            if record['method'] == 'full_syncfs' and record['pattern'] == 'append' and record['speed']['label'] == '10g' \
                    and record['repeat'] < 2:
                record['phases'][measure.FULL_PHASE]['memory']['dest']['dirty_writeback_bytes_after'] += 20 << 20
        with tempfile.TemporaryDirectory() as tmp:
            text = self.run_script(tmp, exp11_smoke_data.write_session(Path(tmp) / 'results', adjust=fail))
        self.assertIn('**A5** (a durable arm fails check (a) or (b), or parcopy\'s fsync count is wrong): 2 run(s) left '
                      'out; reclassified as not durable for the session: full_syncfs', text)
        self.assertIn('left out of every baseline: full_syncfs: A5', text)

    def test_the_csv_columns_read_are_the_harness_columns(self):
        for column in (*exp11.TEXT, *exp11.FLAGS, *exp11.NUMERIC):
            self.assertIn(column, measure.CSV_FIELDS)

    def test_load_recomputes_the_checks_without_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            sweep = exp11_smoke_data.write_sweep(Path(tmp), 'one', ('hot',), repeats=1, extension_repeats=0,
                                                 warm_repeats=0, speeds=(U,))
            for record in (sweep / 'runs').rglob('*.json'):
                record.unlink()
            rows, trials, run = exp11.load(sweep)
        self.assertEqual(run['idle_noise_bytes'], 4 << 20)
        self.assertEqual(len(trials), 1)
        nosync = next(r for r in rows if r['method'] == 'full_nosync')
        self.assertEqual((nosync['a_ok'], nosync['b_ok']), (False, False))
        full = next(r for r in rows if r['method'] == 'full')
        self.assertEqual((full['a_ok'], full['b_ok'], full['dirty_limit_bytes']), (True, True, 4 << 20))
        self.assertIsNone(next(r for r in rows if r['method'] == V1)['b_ok'])


class HarnessReport(unittest.TestCase):
    """experiments/network/report.py on a T2 summary: the warm-source block apart, T2's timers, durability."""

    def render(self, adjust=None):
        with tempfile.TemporaryDirectory() as tmp:
            sweep = exp11_smoke_data.write_sweep(Path(tmp), 'one', ('hot',), repeats=2, extension_repeats=2,
                                                 warm_repeats=1, adjust=adjust)
            return report.render(report.load(sweep / 'summary.csv')), report.load(sweep / 'summary.csv')

    def test_the_warm_source_block_has_rows_of_its_own(self):
        text, rows = self.render()
        self.assertIn('| full (warm source) |', text)
        self.assertIn('| probackup8 (warm source) |', text)
        table = report.medians(report.with_blocks(rows))
        unthrottled = table[('00-hot-f0.01-s70', U)]
        cold = [row['total_s'] for row in rows if row['method'] == 'full' and row['speed'] == U
                and row['block'] != 'warm_source']
        self.assertEqual(len(cold), 4)  # the main block's 2 and the extension's 2
        self.assertAlmostEqual(unthrottled['full'], statistics.median(cold))
        warm = [row['total_s'] for row in rows if row['method'] == 'full' and row['block'] == 'warm_source']
        self.assertEqual(unthrottled['full (warm source)'], warm[0])
        self.assertIn("'(warm source)' rows are EXP-11's warm-source block", text)

    def test_t2_timers_in_the_phase_table(self):
        text, _ = self.render()
        header = next(line for line in text.splitlines() if line.startswith('| method | speed | runs |'))
        for column in ('pbb_checkpoint_s', 'pbb_sync_s', 'probackup_copy_s', 'probackup_sync_s', 'parcopy_fsync_s',
                       'above_link_s'):
            self.assertIn(column, header)
        self.assertIn('`probackup_sync_s` is its timed sync -f', text)
        self.assertIn('pgdelta_direct_inplace_j8', report.DELTA_METHODS)

    def test_durability_failures_are_listed_and_the_control_counted_apart(self):
        text, _ = self.render()
        self.assertIn('The negative control (full_nosync, not durable) failed EXP-11\'s durability checks in 4 of 4 runs.',
                      text)
        self.assertIn("Every durable arm's run passed EXP-11's durability checks", text)

        def fail(record):
            if record['method'] == 'full' and record['repeat'] == 0 and record['speed']['label'] == '2g':
                record['phases'][measure.FULL_PHASE]['memory']['dest']['dirty_writeback_bytes_after'] += 20_000_000
        text, _ = self.render(fail)
        self.assertIn("1 run(s) of durable arms failed EXP-11's durability checks", text)
        self.assertIn('- 00-hot-f0.01-s70 r0 2g full: Dirty + Writeback up 20.', text)

    def test_older_summaries_render_as_before(self):
        rows = [{'trial_id': 't0', 'speed': '2g', 'target_gbit': 2.0, 'method': method, 'total_s': total,
                 'status': 'pass', 'measured_s2d_gbit': None, 'error': ''}
                for method, total in (('full', 4.0), ('pgdelta', 3.0))]
        text = report.render(rows)
        self.assertNotIn('warm source', text)
        self.assertNotIn('durability', text)
        self.assertNotIn('Median timed phases', text)


if __name__ == '__main__':
    unittest.main()
