"""Live adapter contracts, verification faults, and optional real PG17 runs."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from benchmarks import benchmark, live_verify, methods, native_baseline
from benchmarks.backup import CHECKSUM_POLICY, run_backup


def phase(**extra):
    return {'exit_code': 0, 'error': None, 'duration_seconds': .01, **extra}


def evidence(output):
    digest = 'a' * 64
    stats = {'schema_version': 1, 'status': 'complete', 'exit_code': 0,
             'source_kind': 'live', 'in_place': True, 'files_digest': digest,
             'source_identity': {'system_identifier': '1234'},
             'receiver': {'in_place': True, 'files_digest': digest, 'files': 3},
             'live': {'start_lsn': '0/2000028', 'stop_lsn': '0/3000050',
                      'timeline': 1, 'cleanup': 'finished: slot dropped, session closed'},
             'bytes_source_to_dest': {'total': 100}, 'bytes_dest_to_source': {'total': 7}}
    receipt = {'schema_version': 1, 'tool': 'pgdelta', 'status': 'committed',
               'in_place': True, 'output': str(Path(output).absolute()),
               'committed_at': 'now', 'verified_at': 'now', 'files': 3,
               'files_digest': digest}
    return stats, receipt


class EvidenceTests(unittest.TestCase):
    def test_valid_receipt_has_explicit_nonmanifest_boundary(self):
        stats, receipt = evidence('/out')
        endpoint = live_verify.validate_evidence('/out', stats, receipt)
        self.assertEqual(endpoint['lsn'], '0/3000050')
        self.assertEqual(endpoint['source'], 'pgdelta.live.stop_lsn')

    def test_failed_wrong_output_incomplete_and_mismatched_evidence_rejected(self):
        for owner, key, value in (
            ('stats', 'status', 'failed'), ('stats', 'exit_code', True),
            ('stats', 'source_kind', 'backup'), ('stats', 'schema_version', 99),
            ('receipt', 'status', 'verified'), ('receipt', 'output', '/another'),
            ('receipt', 'files_digest', 'b' * 64), ('receipt', 'files', None),
            ('receipt', 'verified_at', None)):
            stats, receipt = evidence('/out')
            (stats if owner == 'stats' else receipt)[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                live_verify.validate_evidence('/out', stats, receipt)

    def test_unsafe_variants_cleanup_and_invalid_ranges_rejected(self):
        for key, value in (('test_variant', 'before_start'), ('read_before_start', True),
                           ('timed_out', True), ('cleanup', None), ('timeline', True),
                           ('stop_lsn', '0/1'), ('stop_lsn', 'invalid')):
            stats, receipt = evidence('/out')
            stats['live'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                live_verify.validate_evidence('/out', stats, receipt)

    def fixture(self, root):
        output = root / 'output'
        output.mkdir()
        (output / 'pg_wal').mkdir()
        (output / 'backup_label').write_text(
            'START WAL LOCATION: 0/2000028 (file 000000010000000000000002)\n')
        stats, receipt = evidence(output)
        stats_path = root / 'stats.json'
        stats_path.write_text(json.dumps(stats))
        output.with_name(output.name + '.pgdelta-receipt.json').write_text(json.dumps(receipt))
        return output, stats_path

    def test_wal_and_identity_checks_are_independent_of_receipt(self):
        for fail_wal, identity in ((True, '1234'), (False, '9876')):
            with self.subTest(wal=fail_wal), tempfile.TemporaryDirectory() as folder:
                output, stats = self.fixture(Path(folder))
                replies = [SimpleNamespace(returncode=0, stderr='',
                    stdout=f'Database system identifier: {identity}\n'),
                    SimpleNamespace(returncode=1 if fail_wal else 0, stderr='bad WAL', stdout='')]
                with mock.patch.object(live_verify.subprocess, 'run', side_effect=replies):
                    with self.assertRaises(ValueError):
                        live_verify.verify(output, stats, 'waldump', 'controldata')

    def test_aliases_and_incomplete_outputs_are_rejected_before_wal(self):
        for defect in ('marker', 'hardlink', 'symlink'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                output, stats = self.fixture(root)
                if defect == 'marker':
                    (output / 'pgdelta-incomplete.json').touch()
                else:
                    source = root / 'source'
                    source.write_text('data')
                    if defect == 'hardlink':
                        os.link(source, output / 'data')
                    else:
                        (output / 'data').symlink_to(source)
                with mock.patch.object(live_verify.subprocess, 'run') as run:
                    with self.assertRaises(ValueError):
                        live_verify.verify(output, stats, 'waldump', 'controldata')
                    run.assert_not_called()


class AdapterTests(unittest.TestCase):
    def run_method(self, method='pg_delta_live', *, bad_stats=False, verify_failure=False,
                   bad_report=False):
        calls = []
        stats, _ = evidence('/out')
        logs = {}

        def execute(stage, argv):
            calls.append((stage, argv))
            report = {'status': 'pass'} if bad_report else {
                'status': 'pass', 'kind': 'pgdelta_receipt_and_wal',
                'recovery_endpoint': {'source': 'pgdelta.live.stop_lsn', 'timeline': 1,
                                      'lsn': '0/3000050', 'system_identifier': '1234'}}
            logs[stage] = '$ command header\n\n' + json.dumps(report) + '\n'
            return phase(exit_code=4 if verify_failure and stage == 'verification' else 0)

        def read(path):
            return '{}' if bad_stats else json.dumps(stats)

        result, code = run_backup(method, run_id='run', output='/out', basis='/basis',
            source_pgdata='/source', method_options={'jobs': 2},
            tools={'python': sys.executable, 'pg_verifybackup': 'verify',
                   'pg_controldata': 'control', 'psql': 'psql', 'pg_waldump': 'waldump'},
            execute=execute, read_file=read, read_log=lambda stage: logs[stage],
            host="/socket's\\path", user='bench',
            basis_verification=phase(checksum_policy=CHECKSUM_POLICY))
        return result, code, calls

    def test_live_never_stages_a_full_backup_and_does_not_modify_basis(self):
        result, code, calls = self.run_method()
        self.assertEqual(code, 0)
        self.assertEqual([stage for stage, _ in calls], ['output_preparation', 'backup', 'verification'])
        command = calls[1][1]
        self.assertEqual(command[command.index('--basis') + 1], '/out')
        self.assertIn('--in-place', command)
        self.assertIn('--source-kind', command)
        self.assertEqual(command[command.index('--jobs') + 1], '2')
        self.assertNotIn('--output', command)
        self.assertNotIn('pg_basebackup', command)
        self.assertEqual(result['status'], 'PASS_ENGINE_ONLY')
        self.assertFalse(result['restore_tested'])
        self.assertEqual(result['verification']['kind'], 'pgdelta_receipt_and_wal')
        self.assertEqual(result['network_bytes'], 107)

    def test_summary_request_is_explicit(self):
        _, code, calls = self.run_method('pg_delta_live_summaries')
        self.assertEqual(code, 0)
        command = calls[1][1]
        self.assertEqual(command[command.index('--fast-path') + 1], 'summaries')

    def test_bad_statistics_or_wal_never_become_success(self):
        for options, status in (({'bad_stats': True}, 'FAIL_TRANSFER'),
                                ({'verify_failure': True}, 'FAIL_VERIFY'),
                                ({'bad_report': True}, 'FAIL_VERIFY')):
            result, code, _ = self.run_method(**options)
            with self.subTest(options=options):
                self.assertNotEqual(code, 0)
                self.assertEqual(result['status'], status)
                self.assertFalse(result['integrity_verified'])
                self.assertIsNone(result['time_to_verified_seconds'])

    def test_conninfo_uses_libpq_escaping_not_shell_escaping(self):
        self.assertEqual(methods.libpq_conninfo(host="/a'b\\c", port=5432, user='u'),
                         "host='/a\\'b\\\\c' port='5432' user='u' dbname='postgres'")

    def test_missing_source_or_required_control_tool_is_rejected_early(self):
        from contextlib import redirect_stderr
        from io import StringIO
        for args in ([], ['--source-pgdata', '/source']):
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                benchmark.parse_args(['--method', 'pg_delta_live', '--basis', '/basis', *args])


class LiveAdapterRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = os.environ.get('PGDELTA_TEST_PG_BIN')
        if not binary:
            postgres = shutil.which('postgres')
            if not postgres:
                raise unittest.SkipTest('Set PGDELTA_TEST_PG_BIN to PostgreSQL 17 binaries')
            binary = str(Path(postgres).parent)
        try:
            cls.tools = native_baseline.preflight(binary)
        except ValueError as exc:
            if os.environ.get('PGDELTA_TEST_PG_BIN'):
                raise
            raise unittest.SkipTest(str(exc)) from exc
        cls.tools.update(python=sys.executable, pg_controldata=str(Path(binary) / 'pg_controldata'))

    def test_both_live_methods_match_independent_reference_under_load(self):
        for method in ('pg_delta_live', 'pg_delta_live_summaries'):
            with self.subTest(method=method), tempfile.TemporaryDirectory(prefix='livebench-') as folder:
                root = Path(folder)
                args = SimpleNamespace(rows=1000, change_fraction=.01, duration=8,
                                       rate=100, seed=20261029, pgdelta_jobs=1)
                trial = native_baseline.Trial(root, root / 'evidence', self.tools, args)
                try:
                    result = native_baseline.run_trial(trial, method, 'active', False)
                    self.assertEqual(result['active_package_exact_cutoff_oracle'], 'pass')
                    backup = result['backup_result']
                    self.assertEqual(backup['status'], 'PASS_ENGINE_ONLY')
                    self.assertEqual(backup['verification']['report']['wal_parse'], 'pass')
                    self.assertEqual(result['self_contained_restore']['state'],
                                     result['cutoff_reference_restore']['state'])
                    self.assertFalse(result['self_contained_restore']['external_wal_allowed'])
                    if method == 'pg_delta_live_summaries':
                        self.assertTrue(backup['transfer_stats']['reported']['fast_path']['used'])
                    self.check_faults(trial)
                    if method == 'pg_delta_live':
                        self.check_public_cli(trial)
                finally:
                    trial.cleanup()

    def check_faults(self, trial):
        output = trial.root / 'output'
        stats_path = methods.pgdelta_stats_path(output)
        verify = lambda: live_verify.verify(output, stats_path, self.tools['pg_waldump'],
                                           self.tools['pg_controldata'])
        receipt_path = output.with_name(output.name + '.pgdelta-receipt.json')
        original = receipt_path.read_text()
        receipt = json.loads(original)
        receipt['files_digest'] = '0' * 64
        receipt_path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, 'digests'):
            verify()
        receipt_path.write_text(original)
        stats = json.loads(stats_path.read_text())
        wal = output / 'pg_wal' / stats['live']['wal_first']
        hidden = trial.root / 'hidden-wal'
        wal.rename(hidden)
        try:
            with self.assertRaisesRegex(ValueError, 'WAL'):
                verify()
        finally:
            hidden.rename(wal)

    def check_public_cli(self, trial):
        expected = trial.state(trial.socket, 'cli-expected')
        _, stdout = trial.command('python', ['-m', 'benchmarks.benchmark',
            '--method', 'pg_delta_live_summaries', '--host', str(trial.socket), '--user', 'bench',
            '--source-pgdata', str(trial.source), '--basis', str(trial.root / 'basis'),
            '--out', str(trial.root / 'cli-runs'), '--pgdelta-jobs', '1',
            '--pg-controldata', self.tools['pg_controldata'], '--psql', self.tools['psql'],
            '--pg-verifybackup', self.tools['pg_verifybackup'], '--pg-waldump', self.tools['pg_waldump']],
            timeout=45)
        result = json.loads(stdout.read_text())
        self.assertEqual(result['status'], 'PASS_ENGINE_ONLY')
        self.assertFalse(result['restore_tested'])
        restored = trial.restore(trial.root / 'cli-runs' / result['run_id'] / 'backup',
                                 'cli-live-restore', expected)
        self.assertEqual(restored['logical_match'], 'pass')


if __name__ == '__main__':
    unittest.main()
