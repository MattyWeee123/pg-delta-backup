"""Tests for EXP-92's analysis (analysis/exp92.py): which cells are valid to score. A failed, unmatched, missing or
misconfigured run must void the predictions that use its cell, never pass them.

They need the analysis venv's matplotlib and numpy and are skipped without them. Nothing is written to docs/.
"""

import sys
import unittest
from pathlib import Path

try:
    import matplotlib  # noqa: F401
except ImportError:
    matplotlib = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'analysis'))


def run(n, arm, status=0, receiver_status=0, match=True, options=None):
    import exp92
    return {'run': n, 'arm': arm, 'status': status, 'receiver_status': receiver_status,
            'match': None if arm.startswith('full') else match,
            'options': exp92.OPTIONS.get(arm.split('_')[0]) if options is None else options}


@unittest.skipIf(matplotlib is None, 'needs the analysis venv (matplotlib, numpy)')
class CellValidity(unittest.TestCase):
    def test_two_good_runs_are_valid(self):
        import exp92
        cell = {('uniform', 'hashAVw_1g'): [run(1, 'hashAVw_1g'), run(2, 'hashAVw_1g')],
                ('uniform', 'full_1g'): [run(3, 'full_1g'), run(4, 'full_1g')]}
        self.assertEqual(exp92.problems(cell, ('uniform', 'hashAVw_1g'), 2), [])
        self.assertEqual(exp92.problems(cell, ('uniform', 'full_1g'), 2), [])

    def test_a_bad_or_missing_run_makes_a_cell_invalid(self):
        import exp92
        key = ('uniform', 'hashAVw_1g')
        cases = {
            'one run': [run(1, 'hashAVw_1g')],
            'no runs': [],
            'the sender failed': [run(1, 'hashAVw_1g'), run(2, 'hashAVw_1g', status=1)],
            'the receiver failed': [run(1, 'hashAVw_1g'), run(2, 'hashAVw_1g', receiver_status=1)],
            'the copy differs': [run(1, 'hashAVw_1g'), run(2, 'hashAVw_1g', match=False)],
            'not checked': [run(1, 'hashAVw_1g'), run(2, 'hashAVw_1g', match=None)],
            # an arm name the lab didn't parse runs as plain hashing
            'the wrong options': [run(1, 'hashAVw_1g'), run(2, 'hashAVw_1g', options=(None, 'main', 'written'))],
        }
        for name, runs in cases.items():
            with self.subTest(name):
                self.assertTrue(exp92.problems({key: runs} if runs else {}, key, 2))

    def test_the_window_is_checked(self):
        import exp92
        key = ('uniform', 'hashA2GVw')
        wrong = (64 << 30, 'worker', 'as-written')
        self.assertTrue(exp92.problems({key: [run(1, 'hashA2GVw'), run(2, 'hashA2GVw', options=wrong)]}, key, 2))
        self.assertEqual(exp92.problems({key: [run(1, 'hashA2GVw'), run(2, 'hashA2GVw')]}, key, 2), [])


if __name__ == '__main__':
    unittest.main()
