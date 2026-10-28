"""Tests for --selector (pgdelta.selector, EXP-74): its counts of each fill's pages and requests against
fastpath.fill_gaps and the sender's runs, its predictions and choice against EXP-74's analysis model, and the CLI."""
import math
import random
import sys
import unittest
from argparse import Namespace
from pathlib import Path

from pgdelta import cli, fastpath, selector
from pgdelta.errors import InvalidInput

sys.path.append(str(Path(__file__).resolve().parents[1] / 'analysis'))
import fastpath_model as model  # noqa: E402


def random_pages(rng, size, density, clustered):
    """Sorted pages of a file of `size` pages: scattered at `density`, or in clusters of consecutive pages."""
    if not clustered:
        return sorted(rng.sample(range(size), int(size * density)))
    pages = set()
    while len(pages) < size * density:
        start = rng.randrange(size)
        pages.update(range(start, min(size, start + rng.randint(1, 300))))
    return sorted(pages)


class Counts(unittest.TestCase):
    def test_each_fill_has_the_pages_and_requests_of_the_filled_plan(self):
        rng = random.Random(74)
        for trial in range(40):
            pages = random_pages(rng, rng.randint(1, 5000), rng.choice((0.01, 0.1, 0.4, 0.9)), trial % 2 == 1)
            lengths, gaps = selector.segments(pages)
            for gap in (0, 1, 2, 3, 4, 9):
                plan = fastpath.fill_gaps(pages, gap)
                requests = sum(math.ceil(n / 21) for n in model.runs_of(plan))
                self.assertEqual(selector.filled(lengths, gaps, gap, 128, 21), (len(plan), requests), (trial, gap))

    def test_long_stretches_split_into_runs_then_requests(self):
        # 300 consecutive pages: runs of 128, 128 and 44, so 7 + 7 + 3 requests of at most 21 pages.
        self.assertEqual(selector.filled(*selector.segments(list(range(300))), 0, 128, 21), (300, 17))
        self.assertEqual(selector.filled([], [], 2, 128, 21), (0, 0))
        self.assertEqual(selector.segments([3, 4, 5, 9, 11, 12]), ([3, 1, 2], [3, 1]))


class Choice(unittest.TestCase):
    def test_it_predicts_and_chooses_as_exp74s_model(self):
        rng = random.Random(740)
        for trial in range(25):
            plan = {f'base/5/{n}': random_pages(rng, rng.randint(100, 20000), rng.choice((0.02, 0.2, 0.6, 0.95)),
                                                trial % 3 == 0) for n in range(rng.randint(1, 4))}
            whole = rng.randint(0, 50) * 10**6
            listed = whole + sum(len(p) for p in plan.values()) * model.PAGE * rng.choice((1, 1.2, 3))
            guard = rng.uniform(0, 5)
            expected, pick = model.select(plan, whole, listed, guard)
            options, fill = selector.choose(list(plan.values()), whole, listed, guard, model.PAGE, model.MAX_RUN)
            self.assertEqual(set(options), set(expected))
            for name, seconds in expected.items():
                self.assertAlmostEqual(options[name], seconds, delta=0.03, msg=(trial, name))
            if abs(sorted(expected.values())[1] - expected[pick]) > 0.05:  # no near-tie the model's rounding decides
                self.assertEqual(selector.FULL if fill is None else f'fill{fill}', pick, trial)

    def test_less_filling_wins_a_tie_and_a_full_copy_wins_where_little_is_left_unchanged(self):
        # Nothing chosen: every fill predicts the same, and fill 0 wins the tie.
        options, fill = selector.choose([], 0, 10**9, 1.0, 8192, 128)
        self.assertEqual(fill, 0)
        self.assertEqual(len({options[f'fill{k}'] for k in selector.FILLS}), 1)
        # Every page of an 8.2 GB file chosen and nothing else listed: the fast path pays its overhead per MB on top
        # of the same bytes, which outweighs a full copy's fixed 5.7 s above about 4.2 GB.
        options, fill = selector.choose([list(range(1000000))], 0, 1000000 * 8192, 1.0, 8192, 128)
        self.assertIsNone(fill)
        self.assertLess(options['full'], options['fill0'])
        # Disks slow at operations favour filling: every other page of a file.
        _, fill = selector.choose([list(range(0, 20000, 2))], 0, 10**10, 0.5, 8192, 128,
                                  selector.Disks(read_ops=500, write_ops=500))
        self.assertIn(fill, (1, 2, 4))

    def test_disks_take_positive_numbers_only(self):
        for bad in ({'read_ops': 0}, {'write_ops': -1}, {'bytes_per_second': float('nan')}, {'read_ops': True}):
            with self.assertRaises(ValueError):
                selector.Disks(**bad)


class Cli(unittest.TestCase):
    def test_off_unless_asked_and_never_beside_a_fill_given(self):
        self.assertIsNone(cli._selector(Namespace()))
        self.assertIsNone(cli._selector(Namespace(selector=False, fill_gaps=None)))
        disks = cli._selector(Namespace(selector=True, fill_gaps=None, selector_read_ops=1000.0,
                                        selector_write_ops=2000.0, selector_mb_per_second=100.0))
        self.assertEqual(disks, selector.Disks(1000.0, 2000.0, 100e6))
        with self.assertRaises(InvalidInput):
            cli._selector(Namespace(selector=True, fill_gaps=2, selector_read_ops=1.0, selector_write_ops=1.0,
                                    selector_mb_per_second=1.0))

    def test_it_needs_the_fast_path(self):
        with self.assertRaises(InvalidInput):
            fastpath.check_mode(None, 'live', False, selector=selector.Disks())
        fastpath.check_mode('summaries', 'live', False, selector=selector.Disks())


if __name__ == '__main__':
    unittest.main()
