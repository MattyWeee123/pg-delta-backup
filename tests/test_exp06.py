"""EXP-06 runner helpers: configurations, run plan, summary, consistency check and isolation test."""
import csv
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from experiments.engine_throughput import run_exp06
from pgdelta.tree import hash_file


def row(pattern, config, wall, *, jobs=1, trial='t-', s2d=1000, d2s=10, status='complete', identical=True,
        pages=7, cpu=None):
    return {'pattern': pattern, 'trial_id': trial + pattern, 'config': config, 'jobs': jobs, 'status': status,
            'wall_external_s': wall, 'cpu_s': cpu if cpu is not None else 2 * wall, 'cpu_user_s': wall,
            'cpu_sys_s': wall, **{f'{phase}_s': 0.5 for phase in run_exp06.PHASES},
            'bytes_s2d': s2d, 'bytes_d2s': d2s, 'bytes_d2s_without_final_complete': d2s - 5,
            'page_data_bytes': s2d - 100, 'pages_sent': pages, 'pages_total': 99, 'nodes_descended': 3,
            'basis_copy_bytes': 4096, 'basis_copies': 2, 'identical': identical, 'switches_ok': True,
            'leaked_processes': 0, 'load1_before': 3.0, 'resident_at_start': 1.0, 'resident_after': 0.99}


class Exp06ConfigTests(unittest.TestCase):
    def test_default_configurations_are_the_pre_registered_ones(self):
        self.assertEqual(run_exp06.DEFAULT_CONFIGS,
                         ('baseline', 'A-2', 'A-4', 'A-8', 'B', 'ABC-1', 'ABC-2', 'ABC-4', 'ABC-8'))
        flags = {name: run_exp06.CONFIGS[name]['flags'] for name in run_exp06.DEFAULT_CONFIGS}
        self.assertEqual(flags['baseline'], ('--jobs', '1', '--rebuild', 'gap-fill', '--verify', 'sequential'))
        self.assertEqual(flags['A-4'], ('--jobs', '4', '--rebuild', 'gap-fill', '--verify', 'sequential'))
        self.assertEqual(flags['B'], ('--jobs', '1', '--rebuild', 'copy-patch'))
        self.assertEqual(flags['ABC-8'], ('--jobs', '8'))
        self.assertEqual(run_exp06.CONFIGS['exp05-4']['flags'],
                         ('--jobs', '4', '--hash-backend', 'thread', '--rebuild', 'gap-fill', '--verify',
                          'sequential'))

    def test_review_configurations_separate_c_from_b(self):
        # A + B (sequential verification) next to A + B + C, so C's share is ABC-N - AB-N.
        self.assertEqual(run_exp06.REVIEW_CONFIGS, run_exp06.DEFAULT_CONFIGS + ('AB-2', 'AB-4', 'AB-8'))
        self.assertEqual(run_exp06.CONFIGS['AB-4']['flags'],
                         ('--jobs', '4', '--rebuild', 'copy-patch', '--verify', 'sequential'))
        self.assertEqual(run_exp06.CONFIGS['AB-4']['expected_switches']['receiver'],
                         {'jobs': 4, 'hash_backend': 'process', 'rebuild': 'copy-patch', 'verify': 'sequential'})
        self.assertEqual(run_exp06.CONFIGS['AC-2']['expected_switches']['receiver'],
                         {'jobs': 2, 'hash_backend': 'process', 'rebuild': 'gap-fill', 'verify': 'pipelined'})
        self.assertEqual(run_exp06.parse_args(['--pairs', 'p', '--configs', 'review']).configs,
                         list(run_exp06.REVIEW_CONFIGS))
        # B and ABC-1 are one engine: --jobs 1 has no workers to verify on.
        self.assertEqual(run_exp06.CONFIGS['B']['expected_switches'], run_exp06.CONFIGS['ABC-1']['expected_switches'])

    def test_expected_switches_are_what_pgdelta_reports_as_in_effect(self):
        expected = {name: run_exp06.CONFIGS[name]['expected_switches'] for name in run_exp06.CONFIGS}
        self.assertEqual(expected['ABC-1']['receiver'],
                         {'jobs': 1, 'hash_backend': 'serial', 'rebuild': 'copy-patch', 'verify': 'sequential'})
        self.assertEqual(expected['ABC-4']['receiver'],
                         {'jobs': 4, 'hash_backend': 'process', 'rebuild': 'copy-patch', 'verify': 'pipelined'})
        self.assertEqual(expected['A-2']['sender'], {'jobs': 2, 'hash_backend': 'process'})
        self.assertEqual(expected['baseline']['sender'], {'jobs': 1, 'hash_backend': 'serial'})
        self.assertEqual(expected['exp05-8']['receiver']['hash_backend'], 'thread')

    def test_plan_covers_every_cell_once_per_repeat_in_a_seeded_order(self):
        configs = list(run_exp06.DEFAULT_CONFIGS)
        plan = run_exp06.plan_runs(3, configs, 3, seed=5)
        self.assertEqual(sorted(plan), sorted((p, c, r) for p in range(3) for c in configs for r in range(3)))
        self.assertEqual(plan, run_exp06.plan_runs(3, configs, 3, seed=5))
        self.assertNotEqual(plan, run_exp06.plan_runs(3, configs, 3, seed=6))


class Exp06SummaryTests(unittest.TestCase):
    def test_summary_takes_medians_compares_with_the_baseline_and_ignores_failed_runs(self):
        pairs = [{'trial_id': 't-hot', 'full_bytes': 1_000_001_010}]
        rows = [row('hot', 'baseline', 4.0), row('hot', 'baseline', 5.0), row('hot', 'baseline', 3.0),
                row('hot', 'ABC-4', 2.0, jobs=4, cpu=10.0), row('hot', 'ABC-4', 9.0, status='failed')]
        summary = run_exp06.summarize(rows, pairs, list(run_exp06.DEFAULT_CONFIGS))
        self.assertEqual([entry['config'] for entry in summary], ['baseline', 'ABC-4'])
        base, abc = summary
        self.assertEqual(base['wall_external_s'], {'median': 4.0, 'min': 3.0, 'max': 5.0})
        self.assertEqual(abc['runs'], 1)
        self.assertAlmostEqual(abc['speedup_vs_baseline'], 2.0)
        self.assertAlmostEqual(abc['cpu_vs_baseline'], 10.0 / 8.0)
        # (1_000_001_010 - 1010) = 1e9 bytes saved in 2 s: 4 Gbit/s.
        self.assertAlmostEqual(abc['crossover_gbps'], 4.0)
        self.assertEqual(abc['phases_median_s']['rebuild'], 0.5)

    def test_a_cell_with_a_cold_run_is_marked_not_comparable(self):
        # The warm-once pass compared --jobs 4 runs that started cached with --jobs 8 runs that
        # started cold (EXP-06 review): a cell is comparable only if every run started warm.
        pairs = [{'trial_id': 't-hot', 'full_bytes': 10_000}]
        rows = [row('hot', 'ABC-4', 1.3), row('hot', 'ABC-4', 1.2), row('hot', 'ABC-8', 1.6), row('hot', 'ABC-8', 1.0)]
        rows[2]['resident_at_start'] = 0.0
        rows[3]['resident_at_start'] = run_exp06.WARM_SHARE
        summary = {entry['config']: entry for entry in run_exp06.summarize(rows, pairs)}
        self.assertEqual((summary['ABC-4']['cold_runs'], summary['ABC-4']['cache_controlled']), (0, True))
        self.assertEqual((summary['ABC-8']['cold_runs'], summary['ABC-8']['cache_controlled']), (1, False))
        rows[0]['resident_at_start'] = None  # unknown counts as cold
        self.assertFalse(run_exp06.summarize(rows, pairs)[0]['cache_controlled'])
        self.assertIn('(not comparable)', run_exp06.markdown(run_exp06.summarize(rows, pairs)))

    def test_cpu_is_flagged_when_it_cannot_count_the_workers(self):
        pairs = [{'trial_id': 't-hot', 'full_bytes': 10_000}]
        rows = [row('hot', 'ABC-4', 1.3), row('hot', 'ABC-4', 1.2)]
        self.assertTrue(run_exp06.summarize(rows, pairs)[0]['cpu_counts_workers'])
        rows[1]['cpu_counts_workers'] = False
        self.assertFalse(run_exp06.summarize(rows, pairs)[0]['cpu_counts_workers'])

    def test_consistency_reports_one_value_per_invariant_and_the_reference_match(self):
        reference = run_exp06.REFERENCE_BYTES['hot']
        same = dict(s2d=reference['bytes_s2d'], d2s=reference['bytes_d2s_without_final_complete'] + 5,
                    pages=reference['pages_sent'])
        rows = [row('hot', 'baseline', 3.0, **same), row('hot', 'ABC-8', 1.0, jobs=8, **same)]
        for item in rows:
            item['page_data_bytes'] = reference['page_data_bytes']
        result = run_exp06.consistency(rows)['hot']
        self.assertTrue(result['one_value_each'])
        self.assertTrue(result['all_identical'])
        self.assertTrue(all(result['matches_exp02_exp05'].values()))
        rows[1]['pages_sent'] += 1
        result = run_exp06.consistency(rows)['hot']
        self.assertFalse(result['one_value_each'])
        self.assertFalse(result['matches_exp02_exp05']['pages_sent'])


class Exp06IsolationTests(unittest.TestCase):
    def test_isolation_file_has_the_size_asked_for_and_is_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = run_exp06.make_isolation_file(Path(tmp, 'a'), 3 << 20 | 5, seed=1)
            second = run_exp06.make_isolation_file(Path(tmp, 'b'), 3 << 20 | 5, seed=1)
            self.assertEqual(first.stat().st_size, 3 << 20 | 5)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_every_pool_gives_the_serial_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = run_exp06.make_isolation_file(Path(tmp, 'file'), 20 << 20, seed=2)
            variants = (('serial', 1), ('process', 1), ('process', 2), ('thread', 2))
            result = run_exp06.isolation(path, variants, repeats=1, seed=3)
            self.assertEqual([(item['backend'], item['jobs']) for item in result['rows']], list(variants))
            for item in result['rows']:
                self.assertGreater(item['gb_per_s'], 0)
                self.assertEqual(len(item['walls_s']), 1)
            one = next(item for item in result['rows'] if item['backend'] == 'process' and item['jobs'] == 1)
            self.assertEqual(one['speedup_vs_1_process'], 1.0)
            self.assertEqual(one['speedup_vs_1_process_with_start'], 1.0)
            for item in result['rows']:
                if item['backend'] == 'process':  # every worker started: the start-up is known
                    self.assertGreater(item['pool_ready_s'], 0)
                    self.assertLessEqual(item['pool_first_ready_s'], item['pool_ready_s'])
                else:
                    self.assertEqual(item['pool_ready_s'], 0.0)
                expected = (one['best_s'] + one['pool_ready_s']) / (item['best_s'] + item['pool_ready_s'])
                self.assertAlmostEqual(item['speedup_vs_1_process_with_start'], expected, places=2)

    def test_cpu_of_the_timed_passes_is_kept_apart_from_start_up_where_proc_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = run_exp06.make_isolation_file(Path(tmp, 'file'), 20 << 20, seed=5)
            result = run_exp06.isolation(path, (('serial', 1), ('process', 1), ('process', 2), ('thread', 2)),
                                         repeats=2, seed=6)
            linux = Path('/proc/self/stat').exists()
            for item in result['rows']:
                with self.subTest(pool=(item['backend'], item['jobs'])):
                    timed = item['cpu_s_per_timed_pass']
                    if item['backend'] == 'process' and not linux:
                        self.assertIsNone(timed)  # live workers' CPU needs /proc
                    else:
                        self.assertGreater(timed, 0)
            one = next(item for item in result['rows'] if item['backend'] == 'process' and item['jobs'] == 1)
            self.assertEqual(one['cpu_pass_vs_1_process'], 1.0)
            serial = next(item for item in result['rows'] if item['backend'] == 'serial')
            self.assertEqual((serial['cpu_pass_vs_serial'], serial['cpu_timed_pass_vs_serial']), (1.0, 1.0))
            csv_path = Path(tmp, 'isolation.csv')
            run_exp06.write_isolation_csv(csv_path, result)
            with csv_path.open(newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(tuple(rows[0]), run_exp06.ISOLATION_CSV_FIELDS)
            self.assertEqual([(row['backend'], row['jobs']) for row in rows],
                             [('serial', '1'), ('process', '1'), ('process', '2'), ('thread', '2')])
            self.assertEqual(len(rows[0]['walls_s'].split()), 2)

    def test_environment_needs_neither_git_nor_sysctl(self):
        # The VMs run a deployed tree with no .git and no git binary.
        from experiments.parallel_hashing import run_exp05
        self.assertEqual(run_exp05.command_output(['no-such-binary-for-pgdelta-tests']), '')
        real = run_exp05.subprocess.run

        def missing(argv, *args, **kwargs):
            if argv[0] in ('git', 'sysctl', 'lscpu'):
                raise FileNotFoundError(2, 'No such file or directory', argv[0])
            return real(argv, *args, **kwargs)

        with mock.patch.object(run_exp05.subprocess, 'run', missing):
            env = run_exp06.environment(Path('/nonexistent'), 'python3')
        self.assertIsNone(env['git_commit'])
        # Unknown, not clean.
        self.assertIsNone(env['git_dirty'])
        self.assertIsNone(env['git_dirty_files'])

    def test_git_state_is_unknown_outside_a_repository(self):
        # git installed, but the deployed tree has no .git: git exits 128 with nothing on stdout.
        from experiments.parallel_hashing import run_exp05
        real = run_exp05.subprocess.run

        def no_repository(argv, *args, **kwargs):
            if argv[0] == 'git':
                return run_exp05.subprocess.CompletedProcess(argv, 128, '', 'fatal: not a git repository')
            return real(argv, *args, **kwargs)

        with mock.patch.object(run_exp05.subprocess, 'run', no_repository):
            env = run_exp06.environment(Path('/nonexistent'), 'python3')
        self.assertEqual((env['git_commit'], env['git_dirty'], env['git_dirty_files']), (None, None, None))
        # In a checkout (these tests run in one), it is known either way.
        if run_exp05.command_output(['git', 'rev-parse', 'HEAD']):
            env = run_exp06.environment(Path('/nonexistent'), 'python3')
            self.assertIn(env['git_dirty'], (True, False))
            self.assertIsInstance(env['git_dirty_files'], list)
            self.assertEqual(env['git_dirty'], bool(env['git_dirty_files']))

    def test_isolation_only_needs_no_pairs(self):
        self.assertIsNone(run_exp06.parse_args(['--isolation-only']).pairs)
        with mock.patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
            run_exp06.parse_args([])
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(run_exp06, 'wait_until_idle'), \
                mock.patch('sys.stdout', io.StringIO()):
            self.assertEqual(run_exp06.main(['--isolation-only', '--out', tmp, '--isolation-bytes', str(4 << 20),
                                             '--isolation-repeats', '1']), 0)
            record = json.loads(Path(tmp, 'results.json').read_text())
            self.assertTrue(record['isolation']['generated'])
            self.assertEqual(record['isolation']['bytes'], 4 << 20)
            self.assertFalse(Path(tmp, 'isolation.bin').exists())  # the generated file is removed
            self.assertTrue(Path(tmp, 'isolation.csv').is_file())

    def test_one_worker_pool_hashes_in_a_worker_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = run_exp06.make_isolation_file(Path(tmp, 'file'), 20 << 20, seed=4)
            pool = run_exp06.make_pool('process', 1)
            try:
                self.assertTrue(pool.parallel)
                self.assertEqual(pool.backend, 'process')
                calls = []
                submit = pool._executor.submit
                pool._executor.submit = lambda *args: calls.append(args) or submit(*args)
                tree = hash_file(path, run_exp06.ISOLATION_KEY, pool=pool)
            finally:
                pool.close()
            self.assertGreater(len(calls), 1)
            self.assertEqual(tree.digest, hash_file(path, run_exp06.ISOLATION_KEY).digest)

    def test_residency_is_a_share_of_the_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'empty').write_bytes(b'')
            Path(tmp, 'data').write_bytes(os.urandom(1 << 20))
            share = run_exp06.resident_fraction([tmp])
            if share is not None:  # None where mmap/mincore are unavailable
                self.assertGreaterEqual(share, 0.0)
                self.assertLessEqual(share, 1.0)
            self.assertIsNone(run_exp06.resident_fraction([Path(tmp, 'missing')]))


if __name__ == '__main__':
    unittest.main()
