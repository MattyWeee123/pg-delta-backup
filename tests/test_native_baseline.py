import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.native_baseline import atomic_json, parse_pgbench_logs, percentile, same_state
from benchmarks.wal_copy import copy_wal


class NativeBaselineTests(unittest.TestCase):
    def test_transaction_window_and_scheduled_latency(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'log'
            path.write_text('0 0 1000 0 100 0 200\n0 1 5000 0 101 0 400\n'
                            '1 0 failed 0 101 500000 0\n1 1 skipped 0 101 600000 0\n'
                            '0 2 9000 0 102 0 500\n')
            result = parse_pgbench_logs([path], 101, 102)
            self.assertEqual(result['completed'], 1)
            self.assertEqual(result['p95_ms'], 5)
            self.assertEqual(result['schedule_lag_p95_ms'], .4)
            self.assertEqual(result['failures'], 1)
            self.assertEqual(result['skipped'], 1)
            self.assertEqual(result['all_logged_successes'], 3)

    def test_missing_latency_is_not_zero(self):
        result = parse_pgbench_logs([], 1, 2)
        self.assertIsNone(result['p95_ms'])
        self.assertIsNone(result['p99_ms'])

    def test_malformed_log_does_not_silently_pass(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'log'
            path.write_text('truncated\n')
            with self.assertRaises(ValueError):
                parse_pgbench_logs([path], 1, 2)

    def test_invalid_window(self):
        with self.assertRaises(ValueError):
            parse_pgbench_logs([], 2, 2)

    def test_nearest_rank_percentile(self):
        self.assertEqual(percentile(list(range(1, 101)), .95), 95)
        self.assertIsNone(percentile([], .95))

    def test_changed_value_with_unchanged_count_is_detected(self):
        before = {'accounts': {'rows': 100, 'sha256': 'original'}}
        after = {'accounts': {'rows': 100, 'sha256': 'modified'}}
        self.assertFalse(same_state(before, after))
        self.assertTrue(same_state(before, before.copy()))

    def test_atomic_result_rejects_nonfinite_numbers(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'result.json'
            atomic_json(path, {'status': 'running'})
            with self.assertRaises(ValueError):
                atomic_json(path, {'value': float('nan')})
            self.assertEqual(json.loads(path.read_text()), {'status': 'running'})

    def test_wal_copy_and_conflicting_archive(self):
        with tempfile.TemporaryDirectory() as root:
            source, dest = Path(root) / 'source', Path(root) / 'destination'
            source.write_bytes(b'wal-record')
            self.assertEqual(copy_wal('archive', source, dest), 0)
            self.assertEqual(copy_wal('archive', source, dest), 0)
            source.write_bytes(b'conflicting-record')
            self.assertEqual(copy_wal('archive', source, dest), 1)
            self.assertEqual(dest.read_bytes(), b'wal-record')
            self.assertEqual(copy_wal('restore', source, dest), 0)
            self.assertEqual(dest.read_bytes(), b'conflicting-record')

    def test_missing_wal_is_not_success(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(copy_wal('restore', Path(root) / 'missing', Path(root) / 'dest'), 1)


if __name__ == '__main__':
    unittest.main()
