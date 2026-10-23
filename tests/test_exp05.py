"""EXP-05 runner helpers: the run plan, the crossover model and the summary."""
import json
from pathlib import Path
import tempfile
import unittest

from experiments.parallel_hashing import run_exp05


def row(pattern, jobs, wall, *, trial='t-', s2d=1000, d2s=10, status='complete', identical=True):
    return {'pattern': pattern, 'trial_id': trial + pattern, 'jobs': jobs, 'status': status,
            'wall_external_s': wall, 'wall_stats_total_s': wall - 0.1, 'cpu_s': 2 * wall,
            'hash_source_s': 1.0, 'hash_basis_s': 1.0, 'verify_s': 0.5, 'bytes_s2d': s2d, 'bytes_d2s': d2s,
            'bytes_d2s_without_final_complete': d2s - 5, 'pages_sent': 7, 'identical': identical}


class Exp05Tests(unittest.TestCase):
    def test_plan_covers_every_cell_once_per_repeat_in_a_seeded_order(self):
        plan = run_exp05.plan_runs(3, [1, 2, 4, 8], 3, seed=5)
        self.assertEqual(sorted(plan), [(p, j, r) for p in range(3) for j in (1, 2, 4, 8) for r in range(3)])
        self.assertEqual(plan, run_exp05.plan_runs(3, [1, 2, 4, 8], 3, seed=5))
        self.assertNotEqual(plan, sorted(plan))

    def test_crossover_is_the_link_speed_where_both_take_as_long(self):
        # 1.25 GB saved in 1 s: 10 Gbit/s.
        self.assertAlmostEqual(run_exp05.crossover_gbps(2_000_000_000, 700_000_000, 50_000_000, 1.0), 10.0)

    def test_summary_takes_medians_and_ignores_failed_runs(self):
        pairs = [{'trial_id': 't-hot', 'full_bytes': 1_000_000_010}]
        rows = [row('hot', 1, 4.0), row('hot', 1, 5.0), row('hot', 1, 3.0), row('hot', 4, 2.0),
                row('hot', 4, 1.0, d2s=12), row('hot', 4, 9.0, status='failed')]
        summary = run_exp05.summarize(rows, pairs)
        self.assertEqual([(entry['jobs'], entry['runs']) for entry in summary], [(1, 3), (4, 2)])
        serial, parallel = summary
        self.assertEqual(serial['wall_external_s'], {'median': 4.0, 'min': 3.0, 'max': 5.0})
        self.assertEqual(parallel['wall_external_s']['median'], 1.5)
        self.assertAlmostEqual(parallel['speedup_vs_1'], 4.0 / 1.5)
        self.assertEqual(parallel['bytes_d2s'], [10, 12])
        self.assertAlmostEqual(serial['crossover_gbps'], (1_000_000_010 - 1010) * 8 / 4.0 / 1e9)
        self.assertTrue(serial['all_identical'])
        self.assertIn('| hot | 4 | 1.500 (1.000-2.000) | 2.67x |', run_exp05.markdown(summary))

    def test_pairs_come_from_passed_trials_with_kept_work_dirs(self):
        with tempfile.TemporaryDirectory() as temp:
            trials = Path(temp, 'trials')
            trials.mkdir()
            base = {'params': {'pattern': 'hot', 'fraction': 0.01, 'seed': 1},
                    'backups': {'B1': {'bytes': 42}}}
            (trials / 'a.json').write_text(json.dumps(dict(base, trial_id='a', status='pass',
                                                           kept_work_dir='/w/a')))
            (trials / 'b.json').write_text(json.dumps(dict(base, trial_id='b', status='error',
                                                           kept_work_dir='/w/b')))
            (trials / 'c.json').write_text(json.dumps(dict(base, trial_id='c', status='pass')))
            pairs = run_exp05.load_pairs(temp)
        self.assertEqual([(pair['trial_id'], pair['b0'], pair['b1'], pair['full_bytes']) for pair in pairs],
                         [('a', '/w/a/B0', '/w/a/B1', 42)])


if __name__ == '__main__':
    unittest.main()
