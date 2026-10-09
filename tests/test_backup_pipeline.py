"""Contract tests for the shared phase boundary, not performance thresholds."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from benchmarks.backup import run_backup
from benchmarks.benchmark import assert_no_overlap, connection_env


class SharedPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tools = {name: name for name in ('pg_basebackup', 'pg_verifybackup', 'pg_combinebackup')}

    def pipeline(self, *, incremental=True, failing=None):
        calls = []

        def execute(stage, command):
            calls.append((stage, command))
            return {'exit_code': 1 if failing == stage else 0,
                    'duration_seconds': .1, 'error': None}

        result, code = run_backup(
            'pg_basebackup_incremental' if incremental else 'pg_basebackup_full',
            run_id='test', output=self.root / 'output', tools=self.tools,
            execute=execute, host='fixture',
            basis=self.root / 'basis' if incremental else None,
            increment=self.root / 'increment' if incremental else None)
        return result, code, calls

    def test_incremental_verifies_inputs_before_combining_and_output_after(self):
        result, code, calls = self.pipeline()
        self.assertEqual(code, 0)
        self.assertEqual([s for s, _ in calls], ['basis_verification', 'backup',
            'increment_verification', 'combination', 'verification'])
        self.assertIn('--incremental', calls[1][1])
        self.assertIn('--copy', calls[3][1])
        self.assertIn('--manifest-checksums=SHA256', calls[3][1])
        self.assertEqual(result['status'], 'PASS_MANIFEST_ONLY')
        self.assertFalse(result['restore_tested'])
        self.assertEqual(result['schema_version'], 2)

    def test_full_has_no_incremental_phases(self):
        result, code, calls = self.pipeline(incremental=False)
        self.assertEqual(code, 0)
        self.assertEqual([s for s, _ in calls], ['backup', 'verification'])
        self.assertNotIn('--incremental', calls[0][1])
        self.assertIsNone(result['combination'])
        self.assertIsNone(result['basis_verification'])

    def test_each_failed_phase_stops_and_cannot_report_verified_time(self):
        stages = ['basis_verification', 'backup', 'increment_verification', 'combination', 'verification']
        expected = ['FAIL_BASIS', 'FAIL_BACKUP', 'FAIL_INCREMENT_VERIFY', 'FAIL_COMBINE', 'FAIL_VERIFY']
        for index, stage in enumerate(stages):
            with self.subTest(stage=stage):
                result, code, calls = self.pipeline(failing=stage)
                self.assertNotEqual(code, 0)
                self.assertEqual([s for s, _ in calls], stages[:index + 1])
                self.assertEqual(result['status'], expected[index])
                self.assertIsNone(result['time_to_verified_seconds'])
                self.assertFalse(result['integrity_verified'])

    def test_invalid_method_inputs_never_execute(self):
        execute = mock.Mock()
        for method, basis, increment in [('pg_basebackup_full', self.root, self.root),
                                         ('pg_basebackup_incremental', None, None)]:
            with self.assertRaises(ValueError):
                run_backup(method, run_id='bad', output=self.root / 'out', tools=self.tools,
                           execute=execute, host='fixture', basis=basis, increment=increment)
        execute.assert_not_called()

    def test_supplied_failed_basis_proof_cannot_bypass_validation(self):
        execute = mock.Mock()
        result, code = run_backup('pg_basebackup_incremental', run_id='bad',
            output=self.root / 'out', tools=self.tools, execute=execute, host='fixture',
            basis=self.root / 'basis', increment=self.root / 'increment',
            basis_verification={'exit_code': 1, 'duration_seconds': .1, 'error': None})
        self.assertEqual(result['status'], 'FAIL_BASIS')
        self.assertEqual(code, 4)
        execute.assert_not_called()

    def test_symlink_alias_cannot_bypass_overlap_guard(self):
        source = self.root / 'source'
        source.mkdir()
        alias = self.root / 'alias'
        try:
            alias.symlink_to(source, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable')
        with self.assertRaises(ValueError):
            assert_no_overlap(alias / 'backup', source)

    def test_connection_env_preserves_auth_but_removes_implicit_routes(self):
        with mock.patch.dict('os.environ', {'PGHOSTADDR': 'wrong', 'PGSERVICE': 'wrong',
                             'PGPASSFILE': '/fixture/passfile', 'PGSSLMODE': 'verify-full'}):
            env = connection_env()
        self.assertNotIn('PGHOSTADDR', env)
        self.assertNotIn('PGSERVICE', env)
        self.assertEqual(env['PGPASSFILE'], '/fixture/passfile')
        self.assertEqual(env['PGSSLMODE'], 'verify-full')
