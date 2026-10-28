"""Tests for the fast path's time model and selector (analysis/fastpath_model.py; EXP-74): the sender's runs, the
requests they reach the disks as, each disk's two limits, and the selector's choice."""
import sys
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / 'analysis'))
import fastpath_model as model  # noqa: E402


class Model(unittest.TestCase):
    def test_runs_are_consecutive_pages_of_at_most_128(self):
        self.assertEqual(model.runs_of([]), [])
        self.assertEqual(model.runs_of([3, 4, 5, 9, 11, 12]), [3, 1, 2])
        self.assertEqual(model.runs_of(list(range(300))), [128, 128, 44])

    def test_requests_split_runs_at_168_kib_and_the_busier_limit_binds(self):
        # One run of 128 pages is 7 requests of up to 21 pages; 2,000 single pages are 2,000 requests.
        big = model.transfer({'base/5/1': list(range(128))}, 0)
        self.assertEqual((big['runs'], big['requests']), (1, 7))
        scattered = model.transfer({'base/5/1': list(range(0, 4000, 2))}, 0)
        self.assertEqual(scattered['requests'], 2000)
        # 2,000 reads at 4,100 a second outweigh 16.4 MB at 210 MB/s; the writes' rate is higher.
        self.assertAlmostEqual(scattered['source_s'], round(2000 / model.READ_OPS, 2))
        self.assertAlmostEqual(scattered['dest_s'], round(2000 / model.WRITE_OPS, 2))
        self.assertEqual(scattered['transfer_s'], scattered['source_s'])
        # Whole files count as bytes only.
        self.assertEqual(model.transfer({}, 210_000_000)['transfer_s'], 1.0)

    def test_the_selector_fills_a_scattered_plan_and_copies_a_full_one(self):
        # Every other page of a file: filling the one-page gaps turns 50,000 requests into a few thousand.
        sparse = {'base/5/1': list(range(0, 100000, 2))}
        options, choice = model.select(sparse, 0, 100000 * model.PAGE * 10, guard_s=1.0)
        self.assertGreater(options['fill0'], options['fill1'])
        self.assertIn(choice, ('fill1', 'fill2', 'fill4'))
        # Every page changed: no fill adds anything, and a copy of a file little larger than the pages wins.
        dense = {'base/5/1': list(range(100000))}
        options, choice = model.select(dense, 0, 100000 * model.PAGE, guard_s=5.0)
        self.assertEqual(choice, 'full')
        self.assertEqual(options['full'], model.predict_full(100000 * model.PAGE))


if __name__ == '__main__':
    unittest.main()
