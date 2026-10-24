"""Tests for EXP-12's harness (T10a, the source's slowdown under a fixed-rate load): experiments/network.

- The pure parts (measure): pgbench's log and progress lines as PostgreSQL 17 writes them, the settle gate, the
  consistent cut mapped across logged runs by their offsets, coverage, the oracle's verdicts, the sampler's groups.
- The arms' tables and command lines, run.json's pins, and EXP-12's run lines split as a POSIX shell splits them.
- The orchestration with every VM step stubbed (test_exp04_network's and test_exp11_core's stubs): a whole trial in
  its order, the timeline (nothing on the source in a settle, a window or a tail), check (c), the proxies never
  stopping the source, the source's part of each verification before the next settle, the load's restart.
- The agent's commands on temporary files, a fake /proc and loopback.
- Where PostgreSQL 17's binaries exist (tests/pgdelta_cluster.PG_BIN), a real cluster: the scripts' invariant under
  concurrent clients, SIGALRM's complete logs, backup-hold, the snapshot, two schema dumps of one state, and the
  oracle on real copies: a good one passes, and each of the seven negative controls fails through its own check.
"""

import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from unittest import mock

from experiments.network import agent, measure, remote, run_exp04, source_cluster

from test_exp04_network import FakeChild, FakeClock
from test_exp11_core import T2Stub

SPEC = Path(__file__).resolve().parents[1] / 'docs' / 'experiments' / 'EXP-12-source-slowdown.md'
NEW_ARMS = ('none', 'capture', 'bracket', 'v1proxy', 'v1proxy_s1', 'pause2', 'burn4')
EPOCH = 1_792_000_000.0

# pgbench 17 -l lines under -R (client, transaction, latency us, script, epoch s, us, schedule lag us), from a run of
# the load's scripts on PostgreSQL 17.11; and its -P 1 --progress-timestamp lines.
PGBENCH_LOG = """\
0 1 1075 0 1792846899 105638 346
1 1 1745 0 1792846899 109648 1150
0 2 3855 0 1792846899 131750 3545
1 2 2262 1 1792846899 140101 2000
"""
PGBENCH_CLOSED = '2 7 144 0 1792847871 159000\n'
PROGRESS = """\
pgbench: setting random seed to 7
progress: 1792846900.078 s, 191.9 tps, lat 3.424 ms stddev 2.828, 0 failed, lag 2.912 ms
progress: 1792847871.159 s, 54386.7 tps, lat 0.144 ms stddev 0.047, 0 failed
"""
# pg_amcheck --heapallindexed --verbose of a database without the extension (PostgreSQL 17.11).
AMCHECK_SKIPPED = """\
pg_amcheck: including database "postgres"
pg_amcheck: warning: skipping database "postgres": amcheck is not installed
pg_amcheck: error: no relations to check
"""
AMCHECK_OK = """\
pg_amcheck: including database "loaddb"
pg_amcheck: checking heap table "loaddb.public.pgbench_accounts"
pg_amcheck: checking btree index "loaddb.public.pgbench_accounts_pkey"
pg_amcheck: checking heap table "loaddb.public.load_progress"
"""


def epoch_us(seconds):
    return int(round(seconds * 1_000_000))


def log_line(client, transaction, end_s, latency_us, script=0, lag_us=0):
    end = epoch_us(end_s)
    return f'{client} {transaction} {latency_us} {script} {end // 1_000_000} {end % 1_000_000} {lag_us}\n'


def registry_run(run_id, start, stop, pid=100, threads=1):
    """A load run as load-start and load-stop record it, with each client's n at its start and stop."""
    return {'run_id': run_id, 'pid': pid, 'threads': threads, 'start': {'n': {str(k): v for k, v in start.items()}},
            'stop': {'n': {str(k): v for k, v in stop.items()}},
            'log_files': [f'{run_id}.{pid}'] + [f'{run_id}.{pid}.{t}' for t in range(1, threads)]}


# --- the pure parts --------------------------------------------------------------------------

class PgbenchLines(unittest.TestCase):
    def test_log_lines_as_postgresql_17_writes_them(self):
        lines = [measure.parse_pgbench_line(line) for line in PGBENCH_LOG.splitlines()]
        self.assertEqual(lines[0], (0, 1, 1075, 0, 1792846899_105638, 346))
        scheduled, started, ended = measure.transaction_times(lines[0])
        self.assertEqual((ended - scheduled, started - scheduled), (1075, 346))
        # The latency is from the schedule: at least the lag, on every line.
        self.assertTrue(all(line[2] >= line[5] for line in lines))
        self.assertEqual(lines[3][3], measure.ABORT_SCRIPT)
        # A closed-loop run (no -R) has no lag column: the start is the schedule.
        closed = measure.parse_pgbench_line(PGBENCH_CLOSED)
        self.assertIsNone(closed[5])
        self.assertEqual(measure.transaction_times(closed)[0], measure.transaction_times(closed)[1])
        self.assertIsNone(measure.parse_pgbench_line('progress: 1 s'))
        self.assertIsNone(measure.parse_pgbench_line('0 1 failed 0 1792846899 1')[2])

    def test_progress_lines(self):
        lines = measure.parse_progress(PROGRESS)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], {'t': 1792846900.078, 'tps': 191.9, 'lat_ms': 3.424, 'stddev_ms': 2.828,
                                    'failed': 0, 'lag_ms': 2.912, 'skipped': 0})
        self.assertIsNone(lines[1]['lag_ms'])

    def test_commits_counted_and_picked_per_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / 'busy.10', Path(tmp) / 'busy.10.1'
            first.write_text(PGBENCH_LOG)
            second.write_text(log_line(2, 1, 100.0, 500) + log_line(2, 2, 100.1, 500, script=1)
                              + log_line(2, 3, 100.2, 500))
            self.assertEqual(measure.count_commits([first, second]), {0: 2, 1: 1, 2: 2})
            picked = measure.pick_commits([first, second], {0: {1}, 2: {1}})
            self.assertEqual(set(picked), {(0, 1), (2, 1)})
            self.assertEqual(picked[(2, 1)][2], epoch_us(100.2))  # the abort between doesn't count
            # Compressed logs read the same, through zstd -dc.
            if shutil.which('zstd'):
                subprocess.run(['zstd', '-q', '--rm', str(first)], check=True)
                found = measure.existing_log(tmp, 'busy.10')
                self.assertEqual(found.name, 'busy.10.zst')
                self.assertEqual(measure.count_commits([found]), {0: 2, 1: 1})

    def test_the_load_command_line(self):
        argv = measure.pgbench_load_argv('/b/pgbench', rate=6000, clients=32, threads=4, duration_s=14400, seed=122,
                                         prefix='/dev/shm/exp12/busy', scripts='/r/load', host='/w/src/s', port=5432,
                                         user='study')
        self.assertEqual(argv, ['/b/pgbench', '-n', '-M', 'prepared', '-c', '32', '-j', '4', '-R', '6000', '-T',
                                '14400', '--random-seed', '122', '-f', '/r/load/commit.sql@99', '-f',
                                '/r/load/abort.sql@1', '-l', '--log-prefix', '/dev/shm/exp12/busy', '-P', '1',
                                '--progress-timestamp', '-h', '/w/src/s', '-p', '5432', '-U', 'study', 'loaddb'])
        closed = measure.pgbench_load_argv('/b/pgbench', rate=0, clients=32, threads=4, duration_s=60, seed=1,
                                           prefix='p', scripts='s', host='h', port=5432, user='study')
        self.assertNotIn('-R', closed)
        self.assertEqual(measure.pgbench_log_names({'run_id': 'busy', 'pid': 7, 'threads': 4}),
                         ['busy.7', 'busy.7.1', 'busy.7.2', 'busy.7.3'])

    def test_the_scripts_are_the_specifications(self):
        scripts = Path(source_cluster.LOAD_SCRIPT_DIR)
        commit = [line for line in (scripts / 'commit.sql').read_text().splitlines() if not line.startswith('--')]
        self.assertEqual(commit, [
            r'\set a1 random(1, 200000)', r'\set a2 random(1, 200000)', r'\set lo least(:a1, :a2)',
            r'\set hi greatest(:a1, :a2)', r'\set d random(2, 5000)', 'BEGIN;',
            'UPDATE pgbench_accounts SET abalance = abalance + :d WHERE aid = :lo;',
            'UPDATE pgbench_accounts SET abalance = abalance - :d + 1 WHERE aid = :hi;',
            'UPDATE load_progress SET n = n + 1 WHERE client = :client_id;', 'END;'])
        abort = [line for line in (scripts / 'abort.sql').read_text().splitlines() if not line.startswith('--')]
        self.assertEqual(abort, [r'\set a random(1, 200000)', 'BEGIN;',
                                 'UPDATE pgbench_accounts SET abalance = abalance + 1000000000 WHERE aid = :a;',
                                 'ROLLBACK;'])
        self.assertEqual(measure.LOAD_SCRIPTS, (('commit.sql', 99), ('abort.sql', 1)))


class SettleGate(unittest.TestCase):
    def lines(self, tps, lag=0.4, now=1000.0, count=10):
        return [{'t': now - index, 'tps': tps, 'lag_ms': lag, 'lat_ms': 1.0} for index in range(count)]

    def test_ten_seconds_within_5_percent_and_under_1_ms_of_lag(self):
        self.assertTrue(measure.settle_gate(self.lines(6000), 1000.0, 6000)['passed'])
        self.assertTrue(measure.settle_gate(self.lines(6290), 1000.0, 6000)['passed'])
        self.assertFalse(measure.settle_gate(self.lines(6310), 1000.0, 6000)['passed'])
        self.assertFalse(measure.settle_gate(self.lines(6000, lag=1.0), 1000.0, 6000)['passed'])
        self.assertFalse(measure.settle_gate(self.lines(6000, count=9), 1000.0, 6000)['passed'])
        # One slow second fails it; a stale window (no line in the last 10 s) fails it too.
        lines = self.lines(6000)
        lines[3]['tps'] = 5000
        self.assertEqual(measure.settle_gate(lines, 1000.0, 6000)['off_rate'], [5000])
        self.assertFalse(measure.settle_gate(self.lines(6000), 1020.0, 6000)['passed'])


class ConsistentCut(unittest.TestCase):
    """The cut: each client's n in a copy points at its commit in the run whose span holds it."""

    def logs(self, tmp, runs, times):
        """One log per run; times[(run_id, client)] the completion times of its commits, each 0.4 ms long."""
        for run in runs:
            lines = []
            for (run_id, client), ends in times.items():
                if run_id == run['run_id']:
                    lines += [(end, log_line(client, index + 1, end, 400)) for index, end in enumerate(ends)]
            (Path(tmp) / run['log_files'][0]).write_text(''.join(line for _, line in sorted(lines)))

    def judge(self, tmp, progress, runs):
        queries = measure.cut_queries(progress, runs)
        commits = {}
        for run_id, wanted in measure.wanted_commits([queries]).items():
            run = next(run for run in runs if run['run_id'] == run_id)
            paths = [Path(tmp) / name for name in run['log_files']]
            for (client, position), value in measure.pick_commits(paths, wanted).items():
                commits[(run_id, client, position)] = value
        return queries, measure.consistent_cut(queries, commits)

    def test_the_cut_mapped_across_a_capacity_run_and_a_load_run_by_their_offsets(self):
        # The capacity run left client 0 at 3 and client 1 at 2; the load run starts there.
        runs = [registry_run('capacity-1', {0: 0, 1: 0}, {0: 3, 1: 2}),
                registry_run('busy', {0: 3, 1: 2}, {0: 7, 1: 6})]
        times = {('capacity-1', 0): [1.0, 2.0, 3.0], ('capacity-1', 1): [1.5, 2.5],
                 ('busy', 0): [10.0, 11.0, 12.0, 13.0], ('busy', 1): [10.5, 11.5, 12.5, 13.5]}
        with tempfile.TemporaryDirectory() as tmp:
            self.logs(tmp, runs, times)
            # A copy at 11.7 s: client 0's 2nd busy commit (n=5), client 1's 2nd (n=4).
            queries, cut = self.judge(tmp, {'0': 5, '1': 4}, runs)
            self.assertEqual(queries[0]['held'], ('busy', 1))
            self.assertEqual(queries[1]['lacking'], ('busy', 2))
            self.assertTrue(cut['ok'], cut)
            self.assertEqual(cut['lo_us'], epoch_us(11.5) - 400)  # the latest start of a last held commit
            self.assertEqual(cut['hi_us'], epoch_us(12.0))  # the earliest end of a first lacking one
            # Between the two runs: the capacity run's last commits held, the busy run's first lacking.
            queries, cut = self.judge(tmp, {'0': 3, '1': 2}, runs)
            self.assertEqual((queries[0]['held'], queries[0]['lacking']), (('capacity-1', 2), ('busy', 0)))
            self.assertTrue(cut['ok'])
            # A prefix extended by 2 (client 0 at 7) holds a commit that started after one it lacks ended.
            _, cut = self.judge(tmp, {'0': 7, '1': 4}, runs)
            self.assertFalse(cut['ok'])
            _, cut = self.judge(tmp, {'0': 3, '1': 4}, runs)  # cut back
            self.assertFalse(cut['ok'])
            # An n no logged run explains fails it.
            queries, cut = self.judge(tmp, {'0': 9, '1': 4}, runs)
            self.assertTrue(queries[0]['unmapped'])
            self.assertEqual(cut['unmapped_clients'], [0])
            self.assertFalse(cut['ok'])
            # Within 1 ms: overlapping by less than that still passes.
            self.assertTrue(measure.consistent_cut({0: {'held': ('r', 0), 'lacking': None, 'unmapped': False},
                                                    1: {'held': None, 'lacking': ('r', 0), 'unmapped': False}},
                                                   {('r', 0, 0): (0, 2000, 2400), ('r', 1, 0): (0, 400, 1200)})['ok'])

    def test_coverage(self):
        cut = {'lo_us': epoch_us(100.0), 'hi_us': epoch_us(100.2)}
        self.assertTrue(measure.coverage(cut, 99.0, 101.0, 0.0)['ok'])
        # Mapped through the offset (the source's clock minus dest's).
        self.assertTrue(measure.coverage(cut, 102.0, 104.0, -2.5)['ok'])
        self.assertFalse(measure.coverage(cut, 102.0, 104.0, 0.0)['ok'])  # a copy of an old state
        self.assertFalse(measure.coverage(cut, 95.0, 99.0, 0.0)['ok'])  # the cut after the clock stopped
        self.assertTrue(measure.coverage(cut, 100.24, 104.0, 0.0)['ok'])  # within 50 ms
        self.assertFalse(measure.coverage(cut, 100.26, 104.0, 0.0)['ok'])
        self.assertTrue(measure.coverage({'lo_us': epoch_us(100.0), 'hi_us': None}, 120.0, 130.0, 0.0)['ok'])
        self.assertAlmostEqual(measure.cut_time(cut), 100.1)


class OracleVerdicts(unittest.TestCase):
    def test_atomicity(self):
        self.assertTrue(measure.atomicity({'sum_abalance': 1234, 'sum_n': 1234, 'leaked': 0})['ok'])
        torn = measure.atomicity({'sum_abalance': 1234 + 4000, 'sum_n': 1234, 'leaked': 0})
        self.assertEqual((torn['ok'], torn['difference']), (False, 4000))
        leaked = measure.atomicity({'sum_abalance': 1_000_001_234, 'sum_n': 1234, 'leaked': 1})
        self.assertFalse(leaked['ok'])
        self.assertIn('500000000', measure.load_oracle_sql())

    def test_amcheck(self):
        self.assertEqual(measure.amcheck_result(0, AMCHECK_OK)['status'], 'ok')
        self.assertEqual(measure.amcheck_result(0, AMCHECK_OK)['relations_checked'], 3)
        skipped = measure.amcheck_result(1, AMCHECK_SKIPPED)
        self.assertEqual((skipped['status'], len(skipped['skipped'])), ('unverified', 1))
        corrupt = measure.amcheck_result(2, AMCHECK_OK + 'btree index "loaddb.public.pgbench_accounts_pkey":\n'
                                         '    ERROR:  invalid page in block 1\n')
        self.assertEqual(corrupt['status'], 'wrong')
        self.assertTrue(corrupt['problems'])
        self.assertEqual(measure.amcheck_result(0, 'pg_amcheck: including database "loaddb"\n')['status'],
                         'unverified')

    def test_row_counts_and_schema_commands(self):
        reference = {'pgbench_accounts': {'rows': 10, 'md5': 'x'}, 'events': {'rows': 3, 'md5': 'y'}}
        self.assertEqual(measure.compare_rows(reference, {'pgbench_accounts': 10, 'events': 3}), (True, []))
        same, differences = measure.compare_rows(reference, {'pgbench_accounts': 10, 'events': 4})
        self.assertFalse(same)
        self.assertIn('events', differences[0])
        self.assertEqual(measure.schema_dump_argv('/b/pg_dump', host='/s', port=5433, user='study', database='loaddb'),
                         ['/b/pg_dump', '--schema-only', '--restrict-key=exp12', '-h', '/s', '-p', '5433', '-U',
                          'study', '-d', 'loaddb'])
        self.assertEqual(measure.amcheck_argv('/b/pg_amcheck', host='/s', port=5433, user='study'),
                         ['/b/pg_amcheck', '--heapallindexed', '--verbose', '-h', '/s', '-p', '5433', '-U', 'study',
                          '-d', 'loaddb'])

    def test_a_live_outputs_status(self):
        good = {'bytes': 'ok', 'start': 'ok', 'atomic': 'ok', 'cut': 'ok', 'coverage': 'ok', 'rows': 'ok',
                'amcheck': 'ok', 'schema': 'ok'}
        self.assertEqual(measure.oracle_status(good), 'pass')
        self.assertEqual(measure.oracle_status({**good, 'cut': None}), 'pending')
        self.assertEqual(measure.oracle_status({**good, 'amcheck': 'unverified'}), 'unverified')
        self.assertEqual(measure.oracle_status({**good, 'amcheck': 'unverified', 'cut': None, 'schema': 'wrong'}),
                         'fail')

    def test_each_negative_control_by_its_own_check(self):
        self.assertEqual(len(measure.ORACLE_CONTROLS), 7)
        statuses = {'start': 'ok', 'atomic': 'ok', 'cut': 'wrong', 'coverage': 'ok', 'rows': 'ok', 'amcheck': 'ok',
                    'schema': 'ok'}
        self.assertTrue(measure.oracle_control_verdict('cut_back', statuses)['ok'])
        self.assertFalse(measure.oracle_control_verdict('schema', statuses)['ok'])
        # The index page must fail pg_amcheck with the invariant passing.
        self.assertTrue(measure.oracle_control_verdict('index_page', {**statuses, 'cut': 'ok', 'amcheck': 'wrong'})['ok'])
        self.assertFalse(measure.oracle_control_verdict('index_page', {**statuses, 'atomic': 'wrong',
                                                                       'amcheck': 'wrong'})['ok'])


class SourceState(unittest.TestCase):
    def test_fincore_and_the_cache_flag(self):
        rows = measure.parse_fincore_json(json.dumps({'fincore': [
            {'res': 8192, 'pages': 2, 'size': 819200, 'file': '/d/base/5/1'},
            {'res': 0, 'pages': 0, 'size': 0, 'file': '/d/base/5/2'}]}))
        self.assertEqual(measure.residency(rows), {'files': 2, 'bytes': 819200, 'resident_bytes': 8192,
                                                   'resident_share': 0.01})
        cold = {'benchmark': {'resident_share': 0.004}, 'snapshot': {'resident_share': 0.0},
                'loaddb': {'resident_share': 1.0}}
        self.assertFalse(measure.cache_flag(cold, warm=False))
        self.assertTrue(measure.cache_flag({**cold, 'snapshot': {'resident_share': 0.02}}, warm=False))
        self.assertTrue(measure.cache_flag({**cold, 'loaddb': {'resident_share': 0.98}}, warm=False))
        self.assertTrue(measure.cache_flag(cold, warm=True))
        warm = {name: {'resident_share': 0.97} for name in ('benchmark', 'snapshot')}
        self.assertFalse(measure.cache_flag({**warm, 'loaddb': {'resident_share': 1.0}}, warm=True))
        self.assertIsNone(measure.cache_flag({}, warm=False))

    def test_the_samplers_groups_from_real_process_titles(self):
        cases = {('postgres', 'postgres: walsender study 10.0.0.13(41234) streaming 2/9A000158'): 'walsender',
                 ('postgres', 'postgres: checkpointer'): 'checkpointer',
                 ('postgres', 'postgres: study loaddb [local] UPDATE'): 'backend',
                 ('postgres', 'postgres: study postgres 10.0.0.13(5512) idle'): 'backend',
                 ('postgres', 'postgres: autovacuum worker loaddb'): 'autovacuum',
                 ('postgres', 'postgres: background writer'): 'bgwriter',
                 ('postgres', 'postgres: autovacuum launcher'): 'postgres_other',
                 ('postgres', '/usr/lib/postgresql/17/bin/postgres -D /data/exp04/src/data -k s'): 'postgres_other',
                 ('pgbench', '/usr/lib/postgresql/17/bin/pgbench -n -M prepared -c 32'): 'pgbench',
                 ('pg_basebackup', '/usr/lib/postgresql/17/bin/pg_basebackup --pgdata /data/exp04/S'): 'pg_basebackup',
                 ('python3', 'python3 -m pgdelta send --source /data/exp04/snap'): 'pgdelta',
                 ('python3', 'python3 /home/u/pgd/experiments/network/agent.py burn --procs 4'): 'burn',
                 ('sshd', 'sshd: bench@notty'): None}
        for (comm, cmdline), group in cases.items():
            with self.subTest(cmdline=cmdline):
                self.assertEqual(measure.sampler_group(comm, cmdline), group)
        # A spawned worker is pgdelta's by its parent.
        self.assertEqual(measure.sampler_group('python3', 'python3 -c from multiprocessing.spawn import spawn_main',
                                               'pgdelta'), 'pgdelta')

    def test_schedstat_run_queue_and_clock(self):
        self.assertEqual(measure.parse_schedstat('123456789 2345678 901\n'), (123456789, 2345678))
        self.assertEqual(measure.parse_proc_running('cpu  1 2 3\nprocs_running 5\nprocs_blocked 0\n'), 5)
        # The far clock 2 ms ahead; the shortest round trip wins.
        samples = [(10.0, 10.0035, 10.001), (11.0, 11.0021, 11.0002), (12.0, 12.009, 12.01)]
        self.assertEqual(measure.clock_offset(samples), {'offset_s': 0.002, 'rtt_s': 0.0002, 'samples': 3})

    def test_busy_cpu_without_steal(self):
        cpu = {f'{field}_seconds': 1.0 for field in measure.CPU_FIELDS}
        self.assertEqual(measure.busy_seconds(cpu), 5.0)
        self.assertIsNone(measure.busy_seconds(None))
        record = {'phases': {'a': {'cpu': {'source': cpu}}, 'b': {'cpu': {'source': cpu}}}}
        self.assertEqual(measure.phases_cpu(record), 10.0)

    def test_the_end_point_line(self):
        lines = [(0.01, 'pg_basebackup: checkpoint completed'),
                 (1.7, 'pg_basebackup: write-ahead log end point: 2/9A000158')]
        self.assertEqual(measure.wal_end_point(lines, EPOCH), {'lsn': '2/9A000158', 'seconds': 1.7,
                                                               'utc': round(EPOCH + 1.7, 6)})
        self.assertIsNone(measure.wal_end_point(lines[:1]))


# --- the arms' tables ----------------------------------------------------------------------------

class Arms(unittest.TestCase):
    def test_the_proxies_take_v1s_path_without_stopping_the_source(self):
        for method in measure.V1PROXY_METHODS:
            with self.subTest(method=method):
                self.assertIn(method, measure.PGDELTA_DIRECT_METHODS)
                self.assertIn(method, measure.PGDELTA_METHODS)
                self.assertIn(method, measure.IN_PLACE_METHODS)
                self.assertIn(method, measure.BASIS_COPY_METHODS)
                self.assertIn(method, measure.OUTPUT_START_METHODS)
                self.assertNotIn(method, measure.SOURCE_STOP_METHODS)
                self.assertNotIn(method, measure.DISK_WRITE_CHECK_METHODS)
        self.assertEqual(set(measure.SOURCE_STOP_METHODS), {'pgdelta_direct', 'pgdelta_direct_inplace',
                                                            'pgdelta_direct_inplace_j8', 'parcopy8', 'parcopy4'})

    def test_per_end_jobs(self):
        self.assertEqual(measure.PGDELTA_ENGINES['v1proxy'], measure.PGDELTA_ENGINES['pgdelta_direct_inplace'])
        self.assertEqual(measure.PGDELTA_ARGS['v1proxy'], measure.PGDELTA_ARGS['pgdelta_direct_inplace'])
        self.assertEqual(measure.PGDELTA_ARGS['v1proxy_s1'],
                         {'send': ('--jobs', '1', '--source-kind', 'datadir'),
                          'receive': ('--jobs', '4', '--verify', 'pipelined', '--prefetch-basis', 'on', '--fsync',
                                      'main', '--in-place')})
        self.assertEqual(measure.pgdelta_switches(measure.PGDELTA_ENGINES['v1proxy_s1']),
                         {'sender': {'jobs': 1, 'hash_backend': 'serial'},
                          'receiver': {'jobs': 4, 'hash_backend': 'process', 'verify': 'pipelined'}})
        self.assertEqual(measure.send_jobs(measure.PGDELTA_ENGINES['pgdelta']), 4)

    def test_check_b_covers_staged_pgdeltas_new_copy_and_exp11s_set_is_unchanged(self):
        self.assertEqual(set(measure.DISK_WRITE_CHECK_METHODS), {*measure.FULL_COPY_METHODS, 'pgdelta'})
        self.assertNotIn('pgdelta', measure.FULL_COPY_METHODS)
        record = {'method': 'pgdelta', 'phases': {'a': {'memory': {'dest': {'dirty_writeback_bytes_before': 0}},
                                                        'disk': {'dest': {'md127': {'write_bytes': 50}}}},
                                                  'b': {'memory': {'dest': {'dirty_writeback_bytes_after': 1 << 20}},
                                                        'disk': {'dest': {'md127': {'write_bytes': 50}}}}}}
        check = measure.durability_check(record, 100)
        self.assertEqual((check['b_required'], check['b_ok'], check['a_ok'], check['md_write_ratio']),
                         (True, True, True, 1.0))
        self.assertFalse(measure.durability_check({**record, 'method': 'v1proxy'}, 100)['b_required'])
        self.assertFalse(measure.durability_check({**record, 'method': 'none'}, None)['a_required'])

    def test_each_new_arm_is_known_optional_tiered_and_scoped(self):
        envelopes = {'omni_today', 'released', 'bound', 'host_tuned'}  # EXP-11's
        for method in NEW_ARMS:
            with self.subTest(method=method):
                self.assertIn(method, measure.OPTIONAL_METHODS)
                self.assertNotIn(method, measure.METHODS)
                self.assertNotIn(measure.TIERS[method], envelopes)
                self.assertIn(method, measure.PROC_SCOPE)
        self.assertNotIn(measure.TIERS['pgdelta'], envelopes)
        self.assertEqual(len(set(measure.ALL_METHODS)), len(measure.ALL_METHODS))
        self.assertIn('--jobs 1', measure.PROC_SCOPE['v1proxy_s1'])
        self.assertIn('backup-hold', measure.PROC_SCOPE['v1proxy'])

    def test_every_new_arm_by_its_block(self):
        """For every new arm: its run_phases and verify path, its stats path, whether it stops the source and
        whether check (b) applies."""
        class Dispatch:
            t10a = True

            def __getattr__(self, name):
                if name.startswith(('run_', 'verify_')):
                    return lambda *args: name
                raise AttributeError(name)

        arms = {'none': ('run_none', 'verify_nothing', False, False, False),
                'capture': ('run_capture', 'verify_nothing', False, False, False),
                'bracket': ('run_bracket', 'verify_nothing', False, False, False),
                'pause2': ('run_control', 'verify_nothing', False, False, False),
                'burn4': ('run_control', 'verify_nothing', False, False, False),
                'v1proxy': ('run_v1proxy', 'verify_direct', True, False, False),
                'v1proxy_s1': ('run_v1proxy', 'verify_direct', True, False, False),
                'full': ('run_full', 'verify_live', False, False, True),
                'pgdelta': ('run_pgdelta', 'verify_live', True, False, True)}
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = LoadStub(tmp, FakeClock())
            for arm, (runner, verifier, stats, stops, b) in arms.items():
                with self.subTest(arm=arm):
                    self.assertEqual(run_exp04.Orchestrator.run_phases(Dispatch(), arm, {}, {}), runner)
                    self.assertEqual(run_exp04.Orchestrator.verify(Dispatch(), arm, {}, {}), verifier)
                    paths = orchestrator.method_paths(arm)
                    self.assertEqual('stats' in paths, stats)
                    self.assertEqual(arm in measure.SOURCE_STOP_METHODS, stops)
                    self.assertEqual(arm in measure.DISK_WRITE_CHECK_METHODS, b)
            orchestrator.pool.shutdown()


# --- the orchestration, stubbed ----------------------------------------------------------------

RUN_LINE_TEST_VALUES = {'R_BUSY': '6000', 'R_MOD': '3000', 'NOISE': '4', 'L_MS': '10'}
LOAD_ARGS = ['--load', 'bank', '--load-rate', '6000', '--load-clients', '2', '--load-threads', '1', '--snapshot',
             '--controls', 'pause2,burn4', '--speeds', '0.5,2,unthrottled', '--methods',
             'none,capture,bracket,full,pgdelta,v1proxy,v1proxy_s1', '--method-speeds', 'none+capture+bracket=2',
             '--method-speeds', 'v1proxy_s1=0.5,2', '--gnu-time', 'none']
SNAPSHOT_ENTRIES = {'base/5/16397': ['file', 16384, 'a' * 64, 0o600, 1, 1],
                    'global/pg_control': ['file', 8192, 'c' * 64, 0o600, 3, 1]}
SNAPSHOT_DIGEST = {'selected': list(SNAPSHOT_ENTRIES), 'read_from': {},
                   'digest': {'root': '/w/snap', 'device': 1, 'files': 2, 'bytes': 24576,
                              'entries': SNAPSHOT_ENTRIES, 'missing': [], 'read_from': {}}}
LOGICAL = {'pgbench_accounts': {'rows': 7_000_000, 'md5': 'a'}, 'events': {'rows': 3, 'md5': 'b'}}
COMMIT_RATE = 50.0  # each stub client's commits per second


def fake_digest(root):
    """measure.tree_digest's stand-in: every tree holds the snapshot's files, each tree its own inodes."""
    base = 1000 * (sum(map(ord, str(root))) % 1000)
    return {'root': str(root), 'device': 2, 'entries': {path: [*entry[:3], 0o600, base + index, 1]
                                                        for index, (path, entry) in enumerate(SNAPSHOT_ENTRIES.items())},
            'files': 2, 'bytes': 24576}


class LoadStub(T2Stub):
    """The orchestrator under EXP-12's load with every VM step stubbed.

    Each event is (kind, what, the fake clock's time): ('source', 'agent evict', t), ('dest', 'pg_verifybackup', t),
    ('mark', 'window_start', t) and so on, so the timeline can be checked. time.time() runs on the same fake clock
    (Clocked patches it), so every UTC in a record is the fake clock's plus EPOCH. The load is a model: each client
    commits COMMIT_RATE per second from its run's start, and fetch_load_logs writes the logs that model gives.
    """

    def __init__(self, tmp, clock, extra_args=()):
        super().__init__(tmp, clock, [*LOAD_ARGS, *extra_args])
        self.timeline: list = []
        self.load_alive = True
        self.stub_runs: dict = {}
        self.copy_at: float | None = None
        self.gate_extra_s = 0.0
        self.fail_source_check = None

    def at(self, kind, what):
        self.timeline.append((kind, what, round(self.clock.now, 6)))

    # The source.
    def _source_agent(self, *args, timeout=600):
        self.at('source', f'agent {args[0]}')
        command = args[0]
        if command == 'settle-gate':
            wait = float(args[args.index('--min-s') + 1]) + self.gate_extra_s
            self.clock.now += wait
            passed = wait <= float(args[args.index('--max-s') + 1]) + 1e-9
            return {'passed': passed, 'unsettled': not passed, 'waited_s': wait, 'lines': 10}
        if command == 'fincore':
            return {'paths': {path: {'resident_share': 1.0 if '16384' in path else 0.0} for path in args[1:]}}
        if command in ('load-pause', 'burn'):
            self.clock.now += 2.0
            return {'sigstop_utc': [EPOCH + self.clock.now - 2.0] * 2, 'sigcont_utc': [EPOCH + self.clock.now] * 2}
        if command == 'run' and 'cat' in args:
            return {'exit_code': 0, 'log_tail': json.dumps({
                'bytes_source_to_dest': 100, 'bytes_dest_to_source': 10, 'files': {'total': 2},
                'pages': {'total': 3}, 'switches': measure.pgdelta_switches(measure.PGDELTA_ENGINES['v1proxy']),
                'receiver': {'prefetch': {'switch': 'on'}}})}
        if command == 'run' and any('pg_verifybackup' in str(part) for part in args):
            return {'exit_code': 1 if self.fail_source_check == 'verify' else 0, 'log_tail': '', 'wall_seconds': 0.1}
        if command == 'digest':
            return fake_digest(args[1])
        if command == 'datadir-digest':
            return SNAPSHOT_DIGEST
        if command == 'sampler':
            return {'pid': 1}
        return {'exit_code': 0, 'log_tail': '', 'wall_seconds': 0.1}

    def counters(self, run_id):
        run = self.stub_runs.get(run_id)
        if run is None:
            return {'n': {'0': 0, '1': 0}}
        elapsed = (run['stopped'] if run.get('stopped') is not None else self.clock.now) - run['started']
        n = {key: value + int(elapsed * COMMIT_RATE) for key, value in run['start'].items()}
        return {'n': n, 'sum_n': sum(n.values()), 'sum_abalance': sum(n.values())}

    def _source_script(self, script, *args, timeout=7200):
        command = args[0]
        self.at('source', f'script {command}')
        if command == 'setup':
            return {'spec': {'accounts_rows': 7_000_000, 'events_rows': 1_000_000}}
        if command == 'loaddb-setup':
            return {'databases': {'postgres': 5, 'loaddb': 16384, 'template1': 1}}
        if command == 'snapshot':
            return {'digest': json.loads(json.dumps(SNAPSHOT_DIGEST))}
        if command == 'load-start':
            run_id = args[args.index('--run-id') + 1]
            previous = [run for run in self.stub_runs.values() if run.get('stopped') is not None]
            start = self.counters(previous[-1]['run_id'])['n'] if previous else {'0': 0, '1': 0}
            self.stub_runs[run_id] = {'run_id': run_id, 'started': self.clock.now, 'start': start, 'stopped': None}
            return {'run_id': run_id, 'pid': 4000 + len(self.stub_runs), 'threads': 1, 'kind': args[-1],
                    'start': {'n': start}, 'stop': None, 'argv': ['pgbench']}
        if command == 'load-stop':
            run = next(run for run in self.stub_runs.values() if run['stopped'] is None)
            stop = self.counters(run['run_id'])
            run['stopped'] = self.clock.now
            return {'run_id': run['run_id'], 'pid': 4000, 'threads': 1, 'start': {'n': run['start']}, 'stop': stop,
                    'log_files': [f'{run["run_id"]}.4000'], 'complete': True, 'commits_total': stop['sum_n']}
        if command == 'load-capacity':
            self.stub_runs['capacity-1'] = {'run_id': 'capacity-1', 'started': self.clock.now,
                                            'start': {'0': 0, '1': 0}, 'stopped': self.clock.now + 1.0}
            self.clock.now += 1.0
            stop = self.counters('capacity-1')
            return {'capacity_tps': 10_000, 'all_complete': True, 'runs': [
                {'run_id': 'capacity-1', 'pid': 4000, 'threads': 1, 'start': {'n': {'0': 0, '1': 0}}, 'stop': stop,
                 'log_files': ['capacity-1.4000'], 'complete': True, 'tps': 10_000}]}
        if command == 'load-status':
            return {'running': self.load_alive, 'last_progress': {'t': EPOCH + self.clock.now}}
        if command == 'load-check':
            return {'ok': True, 'sum_abalance': 1, 'sum_n': 1, 'expected_commits': 1}
        return {}

    def fetch_load_logs(self):
        """The logs the model gives: client k's i-th commit of a run ends at its start + (i + 1) / COMMIT_RATE + 1 us
        (and takes 0.2 ms)."""
        target = self.load_logs
        target.mkdir(parents=True, exist_ok=True)
        for run_id, run in self.stub_runs.items():
            count = int((run['stopped'] - run['started']) * COMMIT_RATE)
            lines = [log_line(int(key), index + 1, EPOCH + run['started'] + (index + 1) / COMMIT_RATE, 200)
                     for index in range(count) for key in sorted(run['start'])]
            (target / f'{run_id}.4000').write_text(''.join(lines))
        self.at('dest', 'fetch the load logs')
        return {'dir': str(target)}

    # Queries, tools and copies.
    def query(self, sql, *, host, port, log_name, timeout=7200, database='postgres'):
        self.at('source' if host == self.source.ip else 'dest', f'query {log_name}')
        if sql == measure.logical_digest_sql():
            return json.dumps(LOGICAL)
        if sql == measure.load_oracle_sql():
            # A copy cuts at its window's middle: the run going then, or after the last run, its stop.
            moment = self.copy_at if host != self.source.ip else self.clock.now
            current = next((run for run in self.stub_runs.values() if run['started'] <= (moment or 0)
                            and (run['stopped'] is None or run['stopped'] >= moment)), None)
            stopped = [run for run in self.stub_runs.values() if run['stopped'] is not None]
            n = self.counters(stopped[-1]['run_id'])['n'] if stopped else {'0': 0, '1': 0}
            if current is not None:
                elapsed = moment - current['started']
                n = {key: value + int(elapsed * COMMIT_RATE) for key, value in current['start'].items()}
            return json.dumps({'sum_abalance': sum(n.values()), 'sum_n': sum(n.values()), 'leaked': 0, 'clients': 2,
                               'n': n})
        if sql == measure.row_counts_sql():
            return json.dumps({table: value['rows'] for table, value in LOGICAL.items()})
        return '1'

    def schema_hashes(self, host, port, label):
        self.at('source' if host == self.source.ip else 'dest', f'schema {label}')
        return {'loaddb': 'a', 'postgres': 'b'}

    def tool_text(self, argv, log_name, *, timeout=3600):
        self.at('dest', Path(str(argv[0])).name)
        return 0, AMCHECK_OK, ''

    def check_output(self, method, data, *, recovery, query=run_exp04.OUTPUT_QUERY, checks=None):
        self.at('dest', 'start the output')
        result = {'ok': True, 'ready_seconds': 1.5, 'answer': '1'}
        if checks is not None:
            result['checks'] = checks()
        return result

    def remote_stamped_command(self, argv, log_name, *, cwd=None):
        self.at('source', 'stamped pg_basebackup')
        log = self.dest_log(log_name)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('[+0.010000] pg_basebackup: checkpoint completed\n'
                       '[+2.000000] pg_basebackup: write-ahead log end point: 2/9A000158\n'
                       '[+2.900000] pg_basebackup: syncing data to disk ...\n'
                       '[+3.000000] @@EXP04@@ ' + json.dumps({'wall_seconds': 2.99, 'exit_code': 0,
                                                              'rusage': {'cpu_seconds': 2.0}}) + '\n')
        started = time.time()
        self.clock.now += 3.0
        return {'host': 'source', 'argv': [str(part) for part in argv], 'exit_code': 0, 'wall_seconds': 3.0,
                'started_utc': started, 'rusage': None, 'log': str(log), 'stamped_over_ssh': True}

    def start_backup_hold(self):
        self.at('source', 'backup start')
        self.clock.now += 0.2
        return {'argv': self.backup_hold_argv(), 'started_utc': time.time() - 0.2, 'started': self.clock.now - 0.2,
                'received_utc': time.time(), 'start': {'lsn': '2/A0000028', 'utc': time.time()},
                'process': mock.Mock(poll=lambda: 0)}

    def stop_backup_hold(self, hold):
        self.at('source', 'backup stop')
        requested = time.time()
        self.clock.now += 0.1
        return {'host': 'source', 'argv': hold['argv'], 'exit_code': 0, 'wall_seconds': 0.3,
                'started_utc': hold['started_utc'], 'rusage': None,
                'role': 'backup mode held in one session around the transfer (backup-hold)',
                'start_line': hold['start'], 'start_received_utc': hold['received_utc'],
                'stop_requested_utc': requested,
                'backup': {'start': hold['start'], 'stop': {'lsn': '2/A0000158', 'utc': time.time()},
                           'one_session': True}}

    def clock_probe(self):
        self.at('both', 'clock probe')
        self.clock_offset_s = 0.0
        return {'offset_s': 0.0, 'rtt_s': 0.0001}

    def set_speed(self, speed):
        self.at('both', f'link {speed["label"]}')
        self.current_speed = speed
        return {'speed': speed, 'source_to_dest_gbit': 1.0, 'dest_to_source_gbit': 1.0}

    def qdisc_stats(self):
        return {}

    def throttle(self, rate):
        return {}

    def take_b0(self, trial):
        self.at('dest', 'take B0')
        self.b0_digest, self.b0_stat = fake_digest(self.b0), {}

    def verify(self, method, paths, phases):
        return run_exp04.Orchestrator.verify(self, method, paths, phases)

    def tool_reported(self, method, paths, phases):
        return run_exp04.Orchestrator.tool_reported(self, method, paths, phases)

    def cleanup_trial(self, trial):
        self.at('both', 'cleanup')

    def pause(self, seconds):
        if seconds > 0:
            self.clock.now += seconds

    def drop_caches(self, source=True):
        self.at('dest' if not source else 'both', 'drop caches')
        return {'source': None, 'dest': {'dropped': True}}

    def snapshot_source(self):
        self.at('counter', 'source')
        return super().snapshot_source()

    def snapshot_dest(self):
        self.at('counter', 'dest')
        return super().snapshot_dest()

    def window(self, phases):
        window = super().window(phases)
        self.copy_at = (window['t0_utc'] + window['t1_utc']) / 2 - EPOCH  # where the stub's copies cut
        return window

    def timed_phase(self, name, body):
        self.at('mark', f'phase {name} start')
        try:
            return super().timed_phase(name, body)
        finally:
            self.at('mark', f'phase {name} end')

    def settle(self, method, paths, record, daemon_holder):
        self.at('mark', 'settle start')
        try:
            return super().settle(method, paths, record, daemon_holder)
        finally:
            self.at('mark', 'settle end')

    def run_load_method(self, trial, repeat, speed, link, method, order_index, order):
        self.at('mark', f'run {link["block"]}')
        return super().run_load_method(trial, repeat, speed, link, method, order_index, order)

    def quiet_tail(self, clock_stopped, clock_stopped_utc, seconds):
        result = super().quiet_tail(clock_stopped, clock_stopped_utc, seconds)
        self.at('mark', 'tail end')
        return result

    def local_command(self, argv, log_name, *, timeout=None, cwd=None, env=None, stamp_lines=False):
        self.at('dest', Path(str(argv[0])).name)
        return super().local_command(argv, log_name, timeout=timeout, cwd=cwd, env=env, stamp_lines=stamp_lines)

    def remote_command(self, argv, log_name, *, cwd=None):
        self.at('source', Path(str(argv[0])).name)
        started = time.time()
        result = super().remote_command(argv, log_name, cwd=cwd)
        result['started_utc'] = started
        if Path(str(argv[0])).name == 'psql':
            result['log_tail'] = 'Time: 81.250 ms\n2/A0000028\nTime: 3.125 ms\n'
        return result


class Stubbed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        for patcher in (mock.patch.object(run_exp04.time, 'perf_counter', self.clock),
                        mock.patch.object(run_exp04.time, 'time', lambda: EPOCH + self.clock.now),
                        mock.patch.object(run_exp04, 'log'), mock.patch.object(run_exp04, 'Child', FakeChild),
                        mock.patch.object(run_exp04.measure, 'tree_digest', fake_digest),
                        mock.patch.object(run_exp04.measure, 'shared_extents', return_value={'shared_files': 0})):
            patcher.start()
            self.addCleanup(patcher.stop)
        FakeChild.started.clear()

    def orchestrator(self, cls=LoadStub, extra_args=()):
        orchestrator = cls(self.tmp.name, self.clock, extra_args)
        self.addCleanup(orchestrator.pool.shutdown)
        orchestrator.methods = run_exp04.method_list(orchestrator.args.methods)
        orchestrator.speeds = measure.parse_speeds(orchestrator.args.speeds)
        orchestrator.method_speeds = measure.parse_method_speeds(orchestrator.args.method_speeds,
                                                                 orchestrator.methods, orchestrator.speeds)
        return orchestrator

    def trial(self, extra=(), cls=LoadStub):
        orchestrator = self.orchestrator(cls, ['--repeats', '1', '--warm-source-repeats', '1',
                                               '--warm-source-speeds', '2', '--warm-source-methods', 'full,v1proxy',
                                               '--moderate-rate', '3000', '--moderate-repeats', '1',
                                               '--moderate-speeds', '2,unthrottled', '--moderate-methods',
                                               'none,full,v1proxy,v1proxy_s1', '--moderate-method-speeds',
                                               'none+v1proxy_s1=2', '--calibration-repeats', '1',
                                               '--calibration-methods', 'full,v1proxy,pgdelta_direct_inplace,burn4',
                                               *extra])
        trial = orchestrator.run_trial(0, 'hot', 0.01)
        return orchestrator, trial


class WholeTrial(Stubbed):
    def test_the_trials_order(self):
        orchestrator, trial = self.trial()
        self.assertEqual(trial['status'], 'pass', (trial.get('error'), trial.get('traceback'),
                                                   [(r['method'], r['status'], r['error']) for r in orchestrator.records
                                                    if r['status'] != 'pass']))
        steps = [what for kind, what, _ in orchestrator.timeline if what in (
            'script setup', 'script loaddb-setup', 'take B0', 'script workload', 'query start-logical-digest.log',
            'query start-checkpoint.log', 'script snapshot', 'clock probe', 'script load-capacity', 'agent sampler',
            'script load-start', 'script load-stop', 'script load-check', 'agent compress', 'fetch the load logs',
            'query end-logical-digest.log', 'script verify')]
        self.assertEqual(steps, ['script setup', 'script loaddb-setup', 'take B0', 'script workload',
                                 'query start-logical-digest.log', 'query start-checkpoint.log', 'script snapshot',
                                 'clock probe', 'script load-capacity', 'agent sampler', 'script load-start',
                                 'script load-stop', 'script load-start', 'script load-stop', 'script load-check',
                                 'agent sampler', 'agent compress', 'fetch the load logs',
                                 'query end-logical-digest.log', 'clock probe', 'script verify'])
        blocks = [(record['block'], record.get('control_slot'), record['method']) for record in orchestrator.records]
        self.assertEqual(blocks[:2], [('busy', 'busy_start', m) for m in ('pause2', 'burn4')]
                         if blocks[0][2] == 'pause2' else [('busy', 'busy_start', m) for m in ('burn4', 'pause2')])
        busy = [method for block, slot, method in blocks if block == 'busy' and slot is None]
        self.assertEqual(sorted(busy), sorted(['full'] * 3 + ['pgdelta'] * 3 + ['v1proxy'] * 3 + ['v1proxy_s1'] * 2
                                              + ['none', 'capture', 'bracket']))
        self.assertEqual([slot for _, slot, _ in blocks if slot], ['busy_start'] * 2 + ['busy_end'] * 2
                         + ['moderate_start'])
        self.assertEqual(sorted(m for b, _, m in blocks if b == 'warm'), ['full', 'v1proxy'])
        self.assertEqual(sorted(m for b, s, m in blocks if b == 'moderate' and not s),
                         ['full', 'full', 'none', 'v1proxy', 'v1proxy', 'v1proxy_s1'])
        self.assertEqual(sorted(m for b, _, m in blocks if b == 'calibration'),
                         ['burn4', 'full', 'pgdelta_direct_inplace', 'v1proxy'])
        # Repeats are numbered on across the blocks and the control slots, so each record has a path of its own.
        paths = [orchestrator.record_path(record) for record in orchestrator.records]
        self.assertEqual(len(paths), len(set(paths)))
        # The load's rate in each block, none in the calibration.
        rates = {record['block']: record['load_rate'] for record in orchestrator.records}
        self.assertEqual(rates, {'busy': 6000, 'warm': 6000, 'moderate': 3000, 'calibration': None})
        self.assertTrue(trial['references']['end']['logical_match'])
        self.assertTrue(trial['snapshot']['end_match'])
        self.assertEqual(trial['load_checks']['statuses'], {'pass': 10})

    def test_every_live_output_passes_its_oracle_with_the_cut_and_coverage(self):
        orchestrator, trial = self.trial()
        live = [record for record in orchestrator.records if record['method'] in ('full', 'pgdelta')]
        self.assertTrue(live)
        for record in live:
            with self.subTest(block=record['block'], method=record['method'], speed=record['speed']['label']):
                oracle = record['verification']['oracle']
                self.assertEqual({check: oracle[check] for check in (*measure.ORACLE_CHECKS, 'bytes')},
                                 dict.fromkeys((*measure.ORACLE_CHECKS, 'bytes'), 'ok'), oracle)
                self.assertEqual(record['status'], 'pass')
                self.assertIsNotNone(record['g4']['staleness_s'])
                self.assertEqual(record['g4']['capture_end']['lsn'], '2/9A000158')
                self.assertEqual(json.loads((orchestrator.record_path(record)).read_text())['status'], 'pass')
        # (vi)'s digest in each (arm, speed) cell's first repeat only, under --static-digest first_repeat.
        orchestrator, _ = self.trial(['--static-digest', 'first_repeat'])
        digests = [(r['method'], r['speed']['label']) for r in orchestrator.records
                   if (r.get('verification') or {}).get('oracle', {}).get('static_digest')]
        self.assertEqual(len(digests), len(set(digests)))

    def test_the_proxies_never_stop_the_source_and_read_the_snapshot(self):
        orchestrator, _ = self.trial()
        stops = [record for record in orchestrator.records if 'stop' in (record.get('source_untimed') or {})]
        self.assertEqual({record['method'] for record in stops}, {'pgdelta_direct_inplace'})
        self.assertEqual({record['block'] for record in stops}, {'calibration'})
        proxies = [record for record in orchestrator.records if record['method'] in measure.V1PROXY_METHODS]
        for record in proxies:
            with self.subTest(block=record['block'], method=record['method']):
                self.assertEqual(record['paths']['source_data'], orchestrator.snapshot_data)
                phase = record['phases'][measure.V1PROXY_PHASE]
                self.assertEqual(list(phase['steps']), list(measure.V1PROXY_STEPS))
                self.assertTrue(record['proxy']['backup']['held'], record['proxy']['backup'])
                self.assertTrue(record['proxy']['hashed']['all_hashed'], record['proxy']['hashed'])
                self.assertEqual(record['status'], 'pass')
                self.assertEqual(record['verification']['reference']['against'],
                                 "the snapshot's selection, as stored at the trial's step 5")
        # A4 (i): a sender that hashed fewer pages than the snapshot's selection holds fails it.
        self.assertEqual(orchestrator.proxy_hashed({'files': {'total': 2}, 'pages': {'total': 3}})['pages_selected'], 3)
        self.assertFalse(orchestrator.proxy_hashed({'files': {'total': 2}, 'pages': {'total': 2}})['all_hashed'])
        self.assertFalse(orchestrator.proxy_hashed({'files': {'total': 1}, 'pages': {'total': 3}})['all_hashed'])
        # A4 (ii): the stop asked for before the transfer ended, or the start after the sender's launch, fails it.
        hold = {'role': 'backup mode held in one session', 'exit_code': 0, 'start_received_utc': 10.0,
                'stop_requested_utc': 20.0, 'backup': {'start': {'lsn': '0/1'}, 'stop': {'lsn': '0/2'},
                                                       'one_session': True}}
        sender = {'host': 'source', 'started_utc': 11.0, 'wall_seconds': 5.0}
        self.assertTrue(orchestrator.backup_held([sender, hold])['held'])
        self.assertFalse(orchestrator.backup_held([{**sender, 'started_utc': 9.0}, hold])['held'])
        self.assertFalse(orchestrator.backup_held([{**sender, 'wall_seconds': 9.5}, hold])['held'])

    def test_the_timeline_keeps_the_source_quiet_in_every_settle_window_and_tail(self):
        """Inside a settle, a window and a tail, the source sees only the counter reads, the gate, and the arm's own
        commands inside its window; dest's work never overlaps a timed phase."""
        orchestrator, _ = self.trial()
        allowed_in_window = {'agent load-pause', 'agent burn', 'stamped pg_basebackup', 'backup start', 'backup stop',
                             'python3', 'psql'}
        state, calibration = None, False
        for kind, what, _ in orchestrator.timeline:
            if kind == 'mark' and what.startswith('run '):
                calibration = what == 'run calibration'  # no load there: v1 itself is checked on its stopped source
            if calibration:
                continue
            if kind == 'mark':
                if what == 'settle start':
                    state = 'settle'
                elif what.startswith('phase ') and what.endswith(' start'):
                    state = 'window'
                elif what.startswith('phase ') and what.endswith(' end'):
                    state = 'tail'
                elif what in ('settle end',):
                    state = 'between'
                elif what == 'tail end':
                    state = None
                continue
            if kind == 'source' and state == 'settle':
                self.assertEqual(what, 'agent settle-gate', orchestrator.timeline)
            if kind == 'source' and state == 'window':
                self.assertIn(what, allowed_in_window)
            if kind == 'source' and state == 'tail':
                self.fail(f'{what} reached the source in a tail')
            if kind == 'dest' and state == 'window':
                self.assertIn(what, ('pg_basebackup',), f'dest work {what} inside a timed phase')

    def test_check_c_flushes_inside_the_phase_and_verification_after_the_last_read(self):
        orchestrator, _ = self.trial()
        events = orchestrator.timeline
        for record in orchestrator.records:
            if record['method'] not in ('full', 'pgdelta'):
                continue
            with self.subTest(method=record['method'], block=record['block'], repeat=record['repeat']):
                last = [index for index, (kind, what, _) in enumerate(events)
                        if (kind, what) == ('mark', f'phase {list(record["phases"])[-1]} end')]
                verify = [index for index, (kind, what, _) in enumerate(events) if what == 'pg_verifybackup']
                # The phase's last counter read (dest's, for (a) and (b)) comes right before its end mark ...
                end = next(index for index in last if any(v > index for v in verify))
                self.assertEqual(events[end - 2][:2], ('counter', 'dest'))
                # ... and the run's flush is inside its phase: pg_basebackup's own (its stamped lines show its sync), or
                # the receiver's, which exits after its fsyncs, in the transfer phase.
                if record['method'] == 'full':
                    self.assertIsNotNone(measure.summary_row(record)['pbb_sync_s'])
                else:
                    self.assertEqual(list(record['phases']), [measure.RECEIVER_PHASE, 'transfer'])
                    self.assertIn('dest', [command['host'] for command in record['phases']['transfer']['commands']])
                # dest's verification starts after that mark.
                self.assertGreater(next(v for v in verify if v > end), end)
        # (a) and (b) are judged on the run's own counters, and pgdelta's (b) applies to its new O.
        record = next(record for record in orchestrator.records if record['method'] == 'pgdelta')
        self.assertTrue(record['durability']['b_required'])

    def test_the_source_half_of_each_verification_runs_before_the_next_settle(self):
        orchestrator, _ = self.trial()
        events = orchestrator.timeline
        # Every pg_verifybackup of S and every statistics read falls in a run's step 1: after a tail, before a settle.
        state = 'between'
        for kind, what, _ in events:
            if kind == 'mark' and what == 'settle start':
                state = 'settle'
            elif kind == 'mark' and what == 'tail end':
                state = 'between'
            elif kind == 'source' and what in ('agent digest', 'agent remove'):
                self.assertEqual(state, 'between', what)
        captures = [record for record in orchestrator.records if record['method'] == 'capture']
        self.assertTrue(all(record['status'] == 'pass' for record in captures))
        staged = [record for record in orchestrator.records if record['method'] == 'pgdelta']
        self.assertTrue(all(record['verification']['identical_to_capture'] for record in staged))
        self.assertTrue(all(record['tool_reported'].get('bytes_source_to_dest') == 100 for record in staged))

    def test_a_failed_check_of_s_makes_the_run_wrong(self):
        class BadS(LoadStub):
            def __init__(self, *args):
                super().__init__(*args)
                self.fail_source_check = 'verify'

        orchestrator, _ = self.trial(cls=BadS)
        for record in orchestrator.records:
            if record['method'] == 'pgdelta':
                self.assertEqual((record['status'], record['verification']['oracle']['bytes']), ('fail', 'wrong'))
            if record['method'] == 'capture':
                self.assertEqual(record['status'], 'fail')

    def test_each_run_records_its_window_settle_and_tail(self):
        orchestrator, _ = self.trial()
        record = next(record for record in orchestrator.records if record['method'] == 'none')
        window = record['window']
        self.assertAlmostEqual(window['t1_utc'] - window['t0_utc'], 60.0, places=3)
        self.assertEqual((window['tail_s'], window['clock_offset_s']), (10.0, 0.0))
        self.assertGreaterEqual(record['settle']['seconds'], 15.0)
        self.assertFalse(record['settle']['unsettled'])
        self.assertEqual(record['tail']['seconds'], 15.0)
        self.assertEqual(record['source_untimed']['cache']['policy'], 'evict')
        self.assertFalse(record['source_untimed']['cache']['cache_flag'])
        calibration = next(record for record in orchestrator.records if record['block'] == 'calibration')
        self.assertIsNone(calibration['settle']['gate'])
        self.assertEqual(calibration['tail']['seconds'], 0.0)
        warm = next(record for record in orchestrator.records if record['block'] == 'warm')
        self.assertEqual(warm['source_untimed']['cache']['policy'], 'warm')
        row = measure.summary_row(record)
        self.assertEqual((row['block'], row['load_rate'], row['window_buckets']), ('busy', 6000, None))

    def test_an_unsettled_settle_is_flagged_and_the_window_still_starts(self):
        class Slow(LoadStub):
            def __init__(self, *args):
                super().__init__(*args)
                self.gate_extra_s = 50.0  # past the 60-s cap

        orchestrator, _ = self.trial(cls=Slow)
        record = next(record for record in orchestrator.records if record['method'] == 'none')
        self.assertTrue(record['settle']['unsettled'])
        self.assertIn('none', record['phases'])

    def test_a_dead_load_voids_the_run_before_and_restarts(self):
        class Dying(LoadStub):
            def _source_script(self, script, *args, timeout=7200):
                if args[0] == 'load-status' and not getattr(self, 'died', False) and len(self.records) == 3:
                    self.died = True
                    self.at('source', 'script load-status')
                    return {'running': False, 'last_progress': None}
                return super()._source_script(script, *args, timeout=timeout)

        orchestrator, trial = self.trial(cls=Dying)
        [restart] = trial['load']['restarts']
        self.assertEqual(restart['restart'], 'restart-1')
        voided = [record for record in orchestrator.records if record['void']]
        self.assertEqual(len(voided), 1)
        self.assertEqual(orchestrator.run_key(voided[0]), restart['voided'])
        self.assertIn('restart-1', [run['run_id'] for run in trial['load']['runs']])


class CommandLines(Stubbed):
    def test_run_json_pins_each_new_arms_command_line(self):
        orchestrator = self.orchestrator(extra_args=['--calibration-repeats', '2', '--calibration-methods',
                                                     'full,pgdelta_direct_inplace,burn4'])
        flags = {method: orchestrator.method_flags(method) for method in orchestrator.pinned_methods}
        self.assertEqual(set(flags), {'none', 'capture', 'bracket', 'full', 'pgdelta', 'v1proxy', 'v1proxy_s1',
                                      'pgdelta_direct_inplace', 'burn4', 'pause2'})
        self.assertEqual(flags['none'], {'none': {'window_s': 60.0, 'commands': []}})
        capture = flags['capture']['argv']
        self.assertEqual(capture[capture.index('--pgdata') + 1], orchestrator.source_capture)
        self.assertEqual(capture[capture.index('--manifest-checksums') + 1], 'CRC32C')
        self.assertIn('--verbose', capture)
        self.assertEqual(flags['pgdelta']['capture_argv'], capture)
        self.assertIn("SELECT pg_backup_start('t10a', true);", flags['bracket']['psql'])
        self.assertIn('SELECT lsn FROM pg_backup_stop(false);', flags['bracket']['psql'])
        self.assertEqual(flags['pause2']['agent'], ['load-pause', '2.0', '--load-dir', '/dev/shm/exp12'])
        self.assertEqual(flags['burn4']['agent'], ['burn', '--procs', '4', '--seconds', '2.0'])
        for method, jobs in (('v1proxy', '4'), ('v1proxy_s1', '1')):
            send, receive = flags[method]['send_argv'], flags[method]['receive_argv']
            self.assertEqual(send[send.index('--source') + 1], orchestrator.snapshot_data)
            self.assertEqual(send[send.index('--jobs') + 1], jobs)
            self.assertEqual(receive[receive.index('--jobs') + 1], '4')
            self.assertEqual(receive[receive.index('--basis') + 1], str(orchestrator.outputs / method))
            self.assertIn('--in-place', receive)
            self.assertEqual(flags[method]['backup_hold'][2:4], ['backup-hold', '--root'])
            self.assertEqual(flags[method]['backup_statements'], ["SELECT pg_backup_start('t10a', true);",
                                                                  'SELECT lsn FROM pg_backup_stop(false);'])
        # v1 itself still reads the live source's (stopped) data directory.
        send = orchestrator.pgdelta_send_argv('pgdelta_direct_inplace',
                                              orchestrator.method_paths('pgdelta_direct_inplace'))
        self.assertEqual(send[send.index('--source') + 1], orchestrator.source_data)

    def test_run_json_holds_the_load_and_its_blocks(self):
        orchestrator = self.orchestrator(extra_args=['--moderate-rate', '3000', '--moderate-repeats', '3',
                                                     '--moderate-speeds', '2,unthrottled', '--moderate-methods',
                                                     'none,full'])
        record = orchestrator.load_run_record()
        busy = record['pgbench_argv']['busy']
        self.assertEqual(busy[busy.index('-R') + 1], '6000')
        self.assertEqual(busy[busy.index('--log-prefix') + 1], '/dev/shm/exp12/busy')
        self.assertEqual(busy[busy.index('--random-seed') + 1], str(4 + 1))  # --order-seed's default, busy's offset
        moderate = record['pgbench_argv']['moderate']
        self.assertEqual(moderate[moderate.index('-R') + 1], '3000')
        self.assertNotIn('-R', record['pgbench_argv']['capacity'])
        self.assertEqual(record['blocks']['moderate']['speeds'], ['2g', 'unthrottled'])
        self.assertEqual(record['server_config'], measure.LOAD_SERVER_CONFIG)
        self.assertEqual(record['stop_signal'], 'SIGALRM')


# --- the run lines -------------------------------------------------------------------------------

def spec_run_lines() -> dict:
    """{invocation name: run_exp04's arguments} from EXP-12's "Run lines" block, split as a POSIX shell would (comments
    included), its placeholders given test values, then as remote.py start hands them to run_exp04.py."""
    text = SPEC.read_text()
    block = re.search(r'\*\*Run lines\*\*.*?```sh\n(.*?)```', text, re.S).group(1)
    variables, lines = {}, {}
    for line in block.replace('\\\n', ' ').splitlines():
        words = shlex.split(line, comments=True)
        if words and all(re.match(r'^\w+=', word) for word in words):
            for word in words:  # R_BUSY=0; R_MOD=0: assignments, ';' ending each
                name, value = word.rstrip(';').split('=', 1)
                variables[name] = RUN_LINE_TEST_VALUES.get(name, value)
            continue
        line = re.sub(r'\$(\w+)', lambda m: variables[m.group(1)], line)
        words = shlex.split(line, comments=True)
        if words[:2] != ['python3', 'experiments/network/remote.py']:
            continue
        args = remote.build_parser().parse_args(words[2:])
        lines[args.name] = args.args[1:] if args.args[:1] == ['--'] else args.args
    return lines


class RunLines(unittest.TestCase):
    def parsed(self):
        lines = spec_run_lines()
        self.assertEqual(sorted(lines), ['exp12-hot'])
        return run_exp04.parse_args(['--source-ip', '192.0.2.2', '--out', '/nonexistent/exp12', *lines['exp12-hot']])

    def test_it_parses_with_every_option_the_harness_needs(self):
        args = self.parsed()
        self.assertEqual((args.load, args.load_rate, args.moderate_rate), ('bank', 6000, 3000))
        self.assertEqual(args.manifest_checksums, 'CRC32C')
        self.assertEqual((args.settle_s, args.settle_max_s, args.tail_s, args.none_window_s), (15, 60, 15, 60))
        self.assertEqual((args.source_cache, args.snapshot, args.static_digest), ('evict', True, 'first_repeat'))
        self.assertEqual(run_exp04.method_list(args.controls), ['pause2', 'burn4'])
        self.assertEqual(args.dirty_noise_mib, 4.0)
        methods = run_exp04.method_list(args.methods)
        self.assertEqual(run_exp04.load_problems(args, methods), [])
        self.assertIsNone(run_exp04.checksum_problem(methods, args.manifest_checksums, args.load))

    def test_it_plans_the_registered_117_runs(self):
        args = self.parsed()
        speeds = measure.parse_speeds(args.speeds)
        methods = run_exp04.method_list(args.methods)
        limited = measure.parse_method_speeds(args.method_speeds, methods, speeds)
        busy = args.repeats * sum(len(measure.methods_at(methods, limited, s['label'])) for s in speeds)
        warm = args.warm_source_repeats * len(run_exp04.method_list(args.warm_source_methods)) * len(
            measure.parse_speeds(args.warm_source_speeds))
        moderate_methods = run_exp04.method_list(args.moderate_methods)
        moderate_speeds = measure.parse_speeds(args.moderate_speeds)
        moderate_limited = measure.parse_method_speeds(args.moderate_method_speeds, moderate_methods, moderate_speeds)
        moderate = args.moderate_repeats * sum(len(measure.methods_at(moderate_methods, moderate_limited, s['label']))
                                               for s in moderate_speeds)
        calibration = args.calibration_repeats * len(run_exp04.method_list(args.calibration_methods))
        controls = 2 + 2 + 1  # pause2 and burn4 at the busy block's start and end, pause2 at the moderate's
        self.assertEqual((busy, warm, moderate, calibration, controls), (70, 10, 18, 14, 5))
        self.assertEqual(busy + warm + moderate + calibration + controls, 117)

    def test_the_placeholders_refuse_to_run(self):
        lines = spec_run_lines()
        argv = [part if part not in ('6000', '3000') else '0' for part in lines['exp12-hot']]
        args = run_exp04.parse_args(['--source-ip', '192.0.2.2', '--out', '/nonexistent/x', *argv])
        self.assertEqual(args.load_rate, 0)  # the shape parses; execute refuses it

    def test_exp12_options_need_the_load(self):
        base = ['--source-ip', 'x', '--out', 'y']
        for extra in (['--methods', 'full,v1proxy', '--snapshot'], ['--controls', 'pause2'],
                      ['--methods', 'full', '--calibration-repeats', '1', '--calibration-methods', 'full']):
            with self.subTest(extra=extra), redirect_stderr_quiet(), self.assertRaises(SystemExit):
                run_exp04.parse_args(base + extra)
        load = [*base, '--load', 'bank', '--load-rate', '100', '--manifest-checksums', 'CRC32C']
        for extra, message in ((['--methods', 'full,v1proxy'], 'need --snapshot'),
                               (['--methods', 'full,pause2'], '--controls'),
                               (['--methods', 'full', '--controls', 'pause3'], '--controls must be'),
                               (['--methods', 'full', '--moderate-repeats', '1', '--moderate-methods', 'v1proxy'],
                                '--moderate-methods must be')):
            with self.subTest(extra=extra):
                problems = run_exp04.load_problems(*self.raw(load + extra))
                self.assertTrue(any(message in problem for problem in problems), problems)
                with redirect_stderr_quiet(), self.assertRaises(SystemExit):
                    run_exp04.parse_args(load + extra)
        # Without CRC32C, a run under the load refuses (rule 7).
        self.assertIn('CRC32C', run_exp04.checksum_problem(['full'], 'SHA256', 'bank'))

    @staticmethod
    def raw(argv):
        """parse_args' namespace without its checks: the parser alone."""
        with mock.patch.object(run_exp04, 'load_problems', return_value=[]), \
                mock.patch.object(run_exp04, 'block_problems', return_value=[]):
            args = run_exp04.parse_args(argv)
        return args, run_exp04.method_list(args.methods)


class redirect_stderr_quiet:
    """argparse's error message kept off the test output."""

    def __enter__(self):
        self.patch = mock.patch('sys.stderr', io.StringIO())
        self.patch.start()

    def __exit__(self, *exc):
        self.patch.stop()
        return False


if __name__ == '__main__':
    unittest.main()
