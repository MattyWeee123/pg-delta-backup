"""Resource evidence checks without requiring Docker or PostgreSQL."""
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from benchmarks.native_baseline import stats_delta
from benchmarks.observe_docker import bytes_value, parse_stats, sample
from benchmarks.resources import ResourceSampler


class ResourceSamplerTests(unittest.TestCase):
    def test_process_deltas_and_peak_are_sampled(self):
        with tempfile.TemporaryDirectory() as root:
            process = Path(root) / '101'
            process.mkdir()
            (process / 'io').write_text('read_bytes: 100\nwrite_bytes: 200\n')
            sampler = ResourceSampler({'source': 101}, Path(root) / 'samples.jsonl')
            sampler.stream = StringIO()
            sampler.ticks_per_second = 100
            sampler.page_size = 4096
            first = {101: {'ppid': 1, 'start': 9, 'ticks': 100,
                           'rss_pages': 2, 'path': process}}
            second = {101: {**first[101], 'ticks': 150, 'rss_pages': 3}}
            with mock.patch('benchmarks.resources._processes', side_effect=[first, second]):
                sampler.sample()
                (process / 'io').write_text('read_bytes: 180\nwrite_bytes: 260\n')
                sampler.sample()
            totals = sampler.totals['source']
            self.assertEqual(totals['cpu_seconds'], .5)
            self.assertEqual(totals['read_bytes'], 80)
            self.assertEqual(totals['write_bytes'], 60)
            self.assertEqual(totals['peak_sampled_rss_bytes'], 3 * 4096)
            self.assertEqual(sampler.observed_samples['source'], 2)

    def test_unavailable_procfs_is_explicit(self):
        with tempfile.TemporaryDirectory() as root:
            sampler = ResourceSampler({'runner': 1}, Path(root) / 'samples.jsonl',
                                      proc_root=Path(root))
            sampler.start()
            result = sampler.stop()
            self.assertIsNotNone(result['unavailable_reason'])
            self.assertIsNone(result['groups'])


class PostgreSQLStatsTests(unittest.TestCase):
    def test_subtracts_only_matching_numeric_counters(self):
        before = {'checkpointer': {'num_requested': 2, 'stats_reset': 'old'},
                  'wal': {'wal_bytes': 100},
                  'io': [{'backend_type': 'client backend', 'object': 'relation',
                          'context': 'normal', 'reads': 4}]}
        after = {'checkpointer': {'num_requested': 5, 'stats_reset': 'old'},
                 'wal': {'wal_bytes': 135},
                 'io': [{'backend_type': 'client backend', 'object': 'relation',
                         'context': 'normal', 'reads': 11}]}
        result = stats_delta(before, after)
        self.assertEqual(result['checkpointer'], {'num_requested': 3})
        self.assertEqual(result['wal'], {'wal_bytes': 35})
        self.assertEqual(result['io'][0]['reads'], 7)

    def test_reset_counters_are_not_reported_as_deltas(self):
        before = {'checkpointer': {'stats_reset': 'earlier'},
                  'wal': {'stats_reset': 'same'}, 'io': []}
        after = {'checkpointer': {'stats_reset': 'later'},
                 'wal': {'stats_reset': 'same'}, 'io': []}
        self.assertIn('unavailable_reason', stats_delta(before, after))


class DockerObserverTests(unittest.TestCase):
    def test_parses_docker_units_and_counters(self):
        self.assertEqual(bytes_value('1.5MiB'), 1572864)
        row = parse_stats({'ID': 'abc', 'Name': 'fixture-postgres-1',
                           'CPUPerc': '75.3%', 'MemUsage': '2MiB / 1GiB',
                           'BlockIO': '147kB / 3MiB', 'PIDs': '7'})
        self.assertEqual(row['memory_usage_bytes'], 2 * 1024 ** 2)
        self.assertEqual(row['block_read_bytes'], 147000)
        self.assertEqual(row['block_write_bytes'], 3 * 1024 ** 2)

    @mock.patch('benchmarks.observe_docker.docker')
    def test_discovers_compose_services(self, docker):
        docker.side_effect = ['source-id\n',
            '{"ID":"source-id","Name":"postgres","CPUPerc":"1%",'
            '"MemUsage":"1MiB / 1GiB","BlockIO":"0B / 2kB","PIDs":"5"}',
            '']
        result = sample('fixture')
        self.assertEqual(len(result['services']['postgres']), 1)
        self.assertNotIn('runner', result['services'])
        self.assertEqual(docker.call_count, 3)


if __name__ == '__main__':
    unittest.main()
