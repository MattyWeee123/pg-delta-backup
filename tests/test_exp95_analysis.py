"""EXP-95's bars, fixed in code before the data exists (rule 16).

These tests are the only thing holding `analysis/exp95.py` to the numbers written in the registration: a bar
loosened without the document disagreeing should not be possible, and a changed test hash is how that shows.
The fixture writes the layout `run_exp04.py` actually leaves - `runs/<sweep>/<name>.json` - because writing it
flat is what let EXP-94's scorer ship reading zero records.
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'analysis'))

import exp95  # noqa: E402

V1, RA, HA, FULL = exp95.V1, exp95.RA, exp95.HA, exp95.FULL
SWEEP = '00-hot-f0.01-s70'
C20, C120 = '2:20:default', '2:120:tuned'


def write_sweep(root: Path, rows, idle=None):
    """rows: (pattern, condition, arm, seconds). idle: {(condition, arm): leaf_wait_idle_s}."""
    (root / 'runs' / SWEEP).mkdir(parents=True, exist_ok=True)
    fields = ['run_name', 'pattern', 'speed', 'method', 'repeat', 'status', 'total_s']
    with open(root / 'summary.csv', 'w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for i, (pattern, condition, arm, seconds) in enumerate(rows):
            name = f'r{i}-{pattern}-{arm}'.replace(':', '_')
            writer.writerow({'run_name': name, 'pattern': pattern, 'speed': condition, 'method': arm,
                             'repeat': 0, 'status': 'pass', 'total_s': seconds})
            record = {'status': 'pass', 'repeat': 0, 'method': arm, 'pattern': pattern,
                      'speed': {'label': condition}, 'totals': {'wall_seconds': seconds},
                      'tool_reported': {'leaf_wait_idle_s': (idle or {}).get((condition, arm), 0.0)}}
            (root / 'runs' / SWEEP / f'{name}.json').write_text(json.dumps(record), encoding='utf-8')


class Prediction1Share(unittest.TestCase):
    """share = (1 - _ra/v1) / (1 - _ha/v1): how much of _ha's gain _ra delivers alone."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def cells_for(self, ra_seconds):
        write_sweep(self.tmp, [('uniform', C20, V1, 100.0), ('uniform', C20, HA, 20.0),
                               ('uniform', C20, RA, ra_seconds)])
        return exp95.exp94.cells(self.tmp)

    def test_read_ahead_delivering_almost_nothing_holds(self):
        # _ra 96 s of a 100 s baseline: gain 0.04 of _ha's 0.80 -> share 0.05
        row = exp95.prediction_1(self.cells_for(96.0))[0]
        self.assertAlmostEqual(row[3], 0.05, places=3)
        self.assertIn('held', row[4])

    def test_read_ahead_delivering_most_of_the_gain_fails(self):
        # _ra 30 s: gain 0.70 of _ha's 0.80 -> share 0.875, past the 0.65 falsifier
        row = exp95.prediction_1(self.cells_for(30.0))[0]
        self.assertAlmostEqual(row[3], 0.875, places=3)
        self.assertIn('FAILED', row[4])

    def test_a_middling_share_is_a_split_and_claims_nothing(self):
        # _ra 60 s: gain 0.40 of _ha's 0.80 -> share 0.50, between the bars
        row = exp95.prediction_1(self.cells_for(60.0))[0]
        self.assertAlmostEqual(row[3], 0.50, places=3)
        self.assertIn('split', row[4])
        self.assertNotIn('held', row[4])

    def test_the_bars_are_the_registered_ones(self):
        self.assertEqual((exp95.SHARE_HOLDS, exp95.SHARE_FAILS), (0.35, 0.65))

    def test_a_cell_where_ha_showed_no_gain_is_not_readable_rather_than_divided_by_zero(self):
        write_sweep(self.tmp, [('uniform', C20, V1, 100.0), ('uniform', C20, HA, 100.0),
                               ('uniform', C20, RA, 90.0)])
        row = exp95.prediction_1(exp95.exp94.cells(self.tmp))[0]
        self.assertIsNone(row[3])
        self.assertIn('not readable', row[4])


class Prediction2Hot(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_two_arms_that_agree_in_hot_hold(self):
        write_sweep(self.tmp, [('hot', C20, V1, 10.0), ('hot', C20, RA, 5.0), ('hot', C20, HA, 5.1)])
        row = exp95.prediction_2(exp95.exp94.cells(self.tmp))[0]
        self.assertIn('held', row[4])

    def test_hashing_ahead_winning_in_hot_fails(self):
        write_sweep(self.tmp, [('hot', C20, V1, 10.0), ('hot', C20, RA, 8.0), ('hot', C20, HA, 5.0)])
        row = exp95.prediction_2(exp95.exp94.cells(self.tmp))[0]
        self.assertIn('FAILED', row[4])

    def test_the_bar_is_the_registered_one(self):
        self.assertEqual(exp95.HOT_BAR, 1.05)


class Prediction3Mechanism(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_reading_ahead_leaving_the_idle_wait_alone_holds(self):
        write_sweep(self.tmp,
                    [('uniform', C20, V1, 100.0), ('uniform', C20, RA, 96.0), ('uniform', C20, HA, 20.0)],
                    idle={(C20, V1): 1.0, (C20, RA): 0.95, (C20, HA): 0.40})
        row = exp95.prediction_3(exp95.exp94.runs(self.tmp))[0]
        self.assertAlmostEqual(row[4], 0.95, places=3)
        self.assertIn('held', row[5])

    def test_reading_ahead_also_filling_the_stream_fails(self):
        write_sweep(self.tmp,
                    [('uniform', C20, V1, 100.0), ('uniform', C20, RA, 60.0), ('uniform', C20, HA, 20.0)],
                    idle={(C20, V1): 1.0, (C20, RA): 0.30, (C20, HA): 0.40})
        row = exp95.prediction_3(exp95.exp94.runs(self.tmp))[0]
        self.assertIn('FAILED', row[5])
        self.assertIn('not separable', row[5])

    def test_the_bar_is_the_registered_one(self):
        self.assertEqual(exp95.IDLE_BAR, 0.80)


class Prediction4Gate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_the_gate_holds_when_ha_reproduces_exp94s_ratio(self):
        write_sweep(self.tmp, [('uniform', C120, V1, 30.0), ('uniform', C120, HA, 6.3),
                               ('uniform', C120, RA, 29.0)])
        rows = {row[0]: row for row in exp95.prediction_4(exp95.exp94.cells(self.tmp))}
        self.assertIn('held', rows[C120][4])          # 6.3/30.0 = 0.210, EXP-94's own value

    def test_the_gate_goes_down_when_it_does_not(self):
        write_sweep(self.tmp, [('uniform', C120, V1, 30.0), ('uniform', C120, HA, 15.0),
                               ('uniform', C120, RA, 29.0)])
        rows = {row[0]: row for row in exp95.prediction_4(exp95.exp94.cells(self.tmp))}
        self.assertIn('FAILED', rows[C120][4])
        self.assertIn('gate is down', rows[C120][4])

    def test_the_expected_ratios_are_exp94s_published_ones(self):
        self.assertEqual(exp95.EXP94_HA_OVER_V1, {'2:20:default': 0.422, '2:120:tuned': 0.210})


class Prediction5CrossSession(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_full_within_one_percent_holds_and_beyond_it_fails(self):
        write_sweep(self.tmp, [('uniform', C20, FULL, 16.0), ('uniform', C120, FULL, 20.0)])
        cell = exp95.exp94.cells(self.tmp)
        rows = {(row[0], row[1]): row for row in exp95.prediction_5(cell, {('uniform', C20): 16.1,
                                                                           ('uniform', C120): 13.2})}
        self.assertIn('held', rows[('uniform', C20)][5])        # 16.0/16.1 = 0.994
        self.assertIn('FAILED', rows[('uniform', C120)][5])     # 20.0/13.2 = 1.515

    def test_exp94s_full_times_are_read_from_its_committed_csv_not_retyped(self):
        prior = exp95.exp94_full()
        if not prior:
            self.skipTest('EXP-94 has not been scored in this checkout')
        for condition in exp95.CONDITIONS:
            self.assertIn(('uniform', condition), prior)
            self.assertGreater(prior[('uniform', condition)], 0)

    def test_the_bar_is_the_registered_one(self):
        self.assertEqual(exp95.FULL_BAR, 0.01)


class Conditions(unittest.TestCase):
    def test_only_exp94s_two_slow_mode_cells_are_scored(self):
        """The cells were chosen by EXP-23's mechanical rule on EXP-94's runs, not by outcome."""
        self.assertEqual(exp95.CONDITIONS, ('2:20:default', '2:120:tuned'))


if __name__ == '__main__':
    unittest.main()
