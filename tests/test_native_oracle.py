import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from benchmarks.native_baseline import Trial, preflight, run_trial
from benchmarks.native_oracle import (backup_endpoint, check_operation_prefix,
                                      check_replay_endpoint, cutoff_options,
                                      lsn_value, successful_operations)


class EndpointTests(unittest.TestCase):
    def manifest(self):
        return {'System-Identifier': 1234, 'WAL-Ranges': [
            {'Timeline': 1, 'Start-LSN': '0/2000028', 'End-LSN': '0/3000050'}]}

    def read_endpoint(self, manifest, identity='1234'):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / 'backup_manifest').write_text(json.dumps(manifest))
            return backup_endpoint(path, identity)

    def test_endpoint_is_manifest_end_not_command_return_time(self):
        endpoint = self.read_endpoint(self.manifest())
        self.assertEqual(endpoint['lsn'], '0/3000050')
        self.assertEqual(endpoint['timeline'], 1)
        self.assertEqual(endpoint['system_identifier'], '1234')

    def test_wrong_cluster_rejected(self):
        with self.assertRaisesRegex(ValueError, 'identifier'):
            self.read_endpoint(self.manifest(), '5678')

    def test_missing_or_multiple_timelines_rejected(self):
        for ranges in (None, [], self.manifest()['WAL-Ranges'] * 2):
            with self.subTest(ranges=ranges), self.assertRaises(ValueError):
                self.read_endpoint({**self.manifest(), 'WAL-Ranges': ranges})

    def test_malformed_or_reversed_range_rejected(self):
        for change in ({'Timeline': True}, {'Timeline': 0}, {'End-LSN': 'bad'},
                       {'End-LSN': '0/1'}, {'End-LSN': '0/2000028'}):
            manifest = self.manifest()
            manifest['WAL-Ranges'][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.read_endpoint(manifest)

    def test_candidate_stops_immediately_reference_stops_exclusively(self):
        endpoint = self.read_endpoint(self.manifest())
        candidate = cutoff_options(endpoint)
        reference = cutoff_options(endpoint, reference=True)
        self.assertIn('recovery_target=immediate', candidate)
        self.assertIn('recovery_target_lsn=0/3000050', reference)
        self.assertIn('recovery_target_inclusive=off', reference)
        for options in (candidate, reference):
            self.assertIn('recovery_target_action=pause', options)
            self.assertIn('recovery_target_timeline=1', options)

    def test_replay_must_equal_target_not_merely_exceed_it(self):
        endpoint = self.read_endpoint(self.manifest())
        check_replay_endpoint(endpoint, '0/03000050')
        for lsn in ('0/300004F', '0/3000051', None):
            with self.subTest(lsn=lsn), self.assertRaises((ValueError, RuntimeError)):
                check_replay_endpoint(endpoint, lsn)

    def test_lsn_is_numeric_across_high_word(self):
        self.assertGreater(lsn_value('1/0'), lsn_value('0/FFFFFFFF'))


class OperationTests(unittest.TestCase):
    def log_operations(self, lines):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'pgbench.log'
            path.write_text(lines)
            return successful_operations([path])

    def test_pg17_counter_is_one_based(self):
        self.assertEqual(self.log_operations('0 1 100 0 100 0\n1 1 200 0 100 1\n'),
                         {(0, 1), (1, 1)})

    def test_duplicate_failure_and_truncation_rejected(self):
        for lines in ('0 1 100 0 100 0\n' * 2, '0 1 failed 0 100 0\n',
                      '0 1 skipped 0 100 0\n', 'truncated\n'):
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                self.log_operations(lines)

    def test_count_equality_does_not_hide_wrong_operation_ids(self):
        with self.assertRaises(RuntimeError):
            check_operation_prefix({(0, 1), (0, 3)}, {(0, 1), (0, 2)}, complete=True)

    def test_independent_worker_prefixes_allow_concurrent_ordering(self):
        acknowledged = {(0, 1), (0, 2), (1, 1), (1, 2)}
        check_operation_prefix({(0, 1), (0, 2), (1, 1)}, acknowledged)
        with self.assertRaises(RuntimeError):
            check_operation_prefix({(0, 2)}, acknowledged)
        with self.assertRaises(RuntimeError):
            check_operation_prefix({(0, 1)}, acknowledged, complete=True)


class OpenTransactionCleanupTests(unittest.TestCase):
    def test_cleanup_drains_and_closes_even_an_already_exited_probe(self):
        for status in (None, 1):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder:
                trial = Trial(Path(folder), Path(folder) / 'out', {}, None)
                process = mock.Mock()
                process.poll.return_value = status
                files = [mock.Mock(), mock.Mock()]
                trial.open_transaction = process
                trial.open_transaction_files = files
                trial.cleanup()
                process.communicate.assert_called_once_with(timeout=5)
                self.assertEqual(process.kill.call_count, int(status is None))
                for file in files:
                    file.close.assert_called_once()
                self.assertIsNone(trial.open_transaction)


class LiveRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = os.environ.get('PGDELTA_TEST_PG_BIN')
        if not binary:
            postgres = shutil.which('postgres')
            if not postgres:
                raise unittest.SkipTest('Set PGDELTA_TEST_PG_BIN to PostgreSQL 17 binaries')
            binary = str(Path(postgres).parent)
        try:
            cls.tools = preflight(binary)
        except ValueError as exc:
            if os.environ.get('PGDELTA_TEST_PG_BIN'):
                raise
            raise unittest.SkipTest(str(exc)) from exc

    def test_active_full_and_incremental_recover_exact_cutoff(self):
        for method in ('pg_basebackup_full', 'pg_basebackup_incremental'):
            with self.subTest(method=method), tempfile.TemporaryDirectory(prefix='oracle-') as folder:
                root = Path(folder)
                args = SimpleNamespace(rows=1000, change_fraction=.01, duration=6,
                                       rate=100, seed=20261029)
                trial = Trial(root, root / 'evidence', self.tools, args)
                try:
                    result = run_trial(trial, method, 'active', False)
                    self.assertEqual(result['active_package_exact_cutoff_oracle'], 'pass')
                    package = result['self_contained_restore']
                    reference = result['cutoff_reference_restore']
                    self.assertEqual(package['state'], reference['state'])
                    self.assertEqual(package['replay_lsn'], reference['replay_lsn'])
                    self.assertFalse(package['external_wal_allowed'])
                    self.assertTrue(reference['external_wal_allowed'])
                    self.assertEqual(package['logical_match'], 'pass')
                    self.assertGreater(package['state']['ledger']['rows'], 0)
                    self.assertLess(package['state']['ledger']['rows'], result['workload_all']['all_logged_successes'])
                    if method == 'pg_basebackup_full':
                        self.check_negative_controls(trial, result)
                finally:
                    trial.cleanup()

    def check_negative_controls(self, trial, result):
        cutoff = result['recovery_endpoint']
        expected = result['cutoff_reference_restore']['state']
        original_state = trial.state

        def lose_transfer(socket, label):
            # Fault injection after replay: lose both one acknowledged transfer
            # and its ledger entry, while preserving total balance exactly.
            trial.sql('SELECT pg_wal_replay_resume();', socket)
            deadline = time.monotonic() + 10
            while trial.sql('SELECT pg_is_in_recovery();', socket) != 'f':
                if time.monotonic() >= deadline:
                    raise RuntimeError('Fault injection could not promote')
                time.sleep(.05)
            worker, seq, a, b = map(int, trial.sql(
                'SELECT worker, seq, a, b FROM bench.ledger ORDER BY seq DESC LIMIT 1;', socket).split('|'))
            trial.sql(f'''BEGIN;
UPDATE bench.accounts SET balance=balance + CASE WHEN id={a} THEN -1 ELSE 1 END
 WHERE id IN ({a}, {b});
DELETE FROM bench.ledger WHERE worker={worker} AND seq={seq};
COMMIT;''', socket)
            self.assertEqual(int(trial.sql('SELECT sum(balance) FROM bench.accounts;', socket)),
                             trial.args.rows * 1000000)
            return original_state(socket, label)

        with mock.patch.object(trial, 'state', side_effect=lose_transfer):
            with self.assertRaisesRegex(RuntimeError, 'logical data/schema differs'):
                trial.restore(trial.root / 'output', 'lost-transfer', expected, cutoff=cutoff)
        trial.stop(trial.root / 'lost-transfer')

        # A live source's later state is deliberately not the cutoff oracle.
        later = json.loads((trial.output / 'expected-after-workload-state.json').read_text())
        with self.assertRaisesRegex(RuntimeError, 'logical data/schema differs'):
            trial.restore(trial.root / 'output', 'wrong-boundary', later, cutoff=cutoff)
        trial.stop(trial.root / 'wrong-boundary')

        # The reference archive still exists; the package must fail without its
        # own WAL instead of silently borrowing that archive.
        missing = trial.root / 'missing-package-wal'
        shutil.copytree(trial.root / 'output', missing)
        for path in (missing / 'pg_wal').iterdir():
            if path.is_file():
                path.unlink()
        with self.assertRaises(RuntimeError):
            trial.restore(missing, 'missing-wal-restore', expected, cutoff=cutoff)


if __name__ == '__main__':
    unittest.main()
