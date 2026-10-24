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
import math
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

    def finish(self, tmp, how, signalled, lost_s=0.2, gap=False, stalled=None):
        """finish_load_run on a run of 2 clients on 2 threads, each committing every 10 ms from 100 s to the signal at
        105 s, whose logs lost their last `lost_s` (pgbench's buffered lines, under SIGINT), with every commit in the
        counters; `gap` drops a line in the middle, `stalled` ends client 1's log at that time."""
        load_dir = Path(tmp)
        run = {'run_id': 'busy', 'pid': 100, 'threads': 2, 'start': {'n': {'0': 0, '1': 0}}, 'stop': None}
        measure.write_load_state(load_dir, {'runs': [run], 'current': 'busy'})
        commits = 500
        for client, name in ((0, 'busy.100'), (1, 'busy.100.1')):
            ends = [100.0 + (index + 1) * 0.01 for index in range(commits)]
            last = (stalled if client == 1 and stalled is not None else 105.0) - lost_s
            (load_dir / name).write_text(''.join(log_line(client, index + 1, end, 400)
                                                 for index, end in enumerate(ends)
                                                 if end <= last and not (gap and client == 0 and index == 100)))
        stop = {'utc': 106.0, 'n': {'0': commits, '1': commits}, 'sum_n': 2 * commits, 'sum_abalance': 2 * commits}
        args = Namespace(root=tmp, bin_dir=tmp, load_dir=tmp)
        with mock.patch.object(source_cluster, 'load_counters', return_value=stop):
            return source_cluster.finish_load_run(args, 'busy', how, signalled), args, stop

    def test_sigints_fallback_is_judged_by_its_own_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            # SIGALRM: the counters against the log, which lost its last lines: incomplete.
            run, _, _ = self.finish(tmp, 'SIGALRM', 105.0)
            self.assertEqual((run['complete'], run['completeness']['criterion']), (False, 'counters'))
        with tempfile.TemporaryDirectory() as tmp:
            # SIGINT: every line up to 1 s before the signal is there, so it is complete, and its commits are the
            # counters' (step 15's load-check sums them).
            run, args, stop = self.finish(tmp, 'SIGINT', 105.0)
            self.assertEqual((run['complete'], run['completeness']['criterion']), (True, 'sigint'))
            self.assertEqual(run['committed'], 1000)
            self.assertLess(run['commits_total'], 1000)
            with mock.patch.object(source_cluster, 'load_counters', return_value=stop):
                check = source_cluster.cmd_load_check(args)
            self.assertEqual((check['ok'], check['expected_commits'], check['incomplete']), (True, 1000, []))
        for changes, why in (({'lost_s': 1.5}, 'before the signal less 1 s'),
                             ({'gap': True}, 'a gap in its transaction numbers'),
                             ({'stalled': 103.0}, 'before the signal less 1 s')):
            with self.subTest(**changes), tempfile.TemporaryDirectory() as tmp:
                run, _, _ = self.finish(tmp, 'SIGINT', 105.0, **changes)
                self.assertFalse(run['complete'])
                self.assertTrue(any(why in text for text in run['completeness']['short'].values()), run)

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
        if sql == source_cluster.LOAD_RELATIONS_SQL:
            return json.dumps({'pgbench_accounts': {'pages': 3637}, 'load_progress': {'pages': 2}})
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

    def quiet_tail(self, clock_stopped, clock_stopped_utc, seconds, window=None):
        result = super().quiet_tail(clock_stopped, clock_stopped_utc, seconds, window)
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
        self.assertEqual(trial['references']['end']['loaddb_growth'], 0.0)
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

    def test_the_quiet_tail_outlasts_the_15_s_views_last_bucket(self):
        # The analysis's 15-s view ends at t0 + ceil(t1 + 15 - t0), up to 1 s past t1 + 15: the tail waits it out,
        # so the next step on the source falls outside every view.
        orchestrator, _ = self.trial()
        loaded = [record for record in orchestrator.records if record['block'] != 'calibration']
        fractional = 0
        for record in loaded:
            window, tail = record['window'], record['tail']
            view_end = window['t0_utc'] + math.ceil(window['t1_utc'] + 15 - window['t0_utc'] - 1e-9)
            fractional += view_end > record['window']['t1_utc'] + 15 + 1e-6
            self.assertGreaterEqual(tail['ended_utc'], view_end + run_exp04.TAIL_VIEW_MARGIN_S - 1e-6, record['method'])
            self.assertAlmostEqual(tail['view_end_utc'], view_end + run_exp04.TAIL_VIEW_MARGIN_S, places=5)
        self.assertGreater(fractional, 0)  # runs whose view does reach past t1 + 15
        # Without the load (calibration) there is no tail to wait.
        calibration = [record for record in orchestrator.records if record['block'] == 'calibration']
        self.assertTrue(all(record['tail']['view_end_utc'] is None for record in calibration))

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

    def test_an_eviction_that_leaves_more_than_1_percent_runs_again_after_a_sync(self):
        class Sticky(LoadStub):
            readings = 0

            def _source_agent(self, *args, timeout=600):
                if args[0] == 'fincore' and len(args) > 2:  # a run's reading, not step 6's probe
                    self.readings += 1
                    share = 0.03 if self.readings == 1 else 0.0
                    self.at('source', 'agent fincore')
                    return {'paths': {path: {'resident_share': 1.0 if '16384' in path else share} for path in args[1:]}}
                return super()._source_agent(*args, timeout=timeout)

        orchestrator, _ = self.trial(cls=Sticky)
        cache = orchestrator.records[0]['source_untimed']['cache']
        self.assertTrue(measure.cache_flag(cache['first_fincore'], warm=False))
        self.assertIn('evicted_again', cache)
        self.assertFalse(cache['cache_flag'])
        self.assertNotIn('evicted_again', orchestrator.records[1]['source_untimed']['cache'])
        events = [what for kind, what, _ in orchestrator.timeline if what in ('agent evict', 'agent fincore')]
        first = events.index('agent evict')  # after step 6's probe
        self.assertEqual(events[first:first + 5], ['agent evict', 'agent fincore', 'agent evict', 'agent fincore',
                                                   'agent evict'])

    def test_a_check_that_times_out_leaves_the_output_unverified_and_an_error_from_the_copy_makes_it_wrong(self):
        class Flaky(LoadStub):
            calls = 0

            def query(self, sql, *, host, port, log_name, timeout=7200, database='postgres'):
                if sql == measure.load_oracle_sql() and host != self.source.ip:
                    self.calls += 1
                    if self.calls == 1:
                        raise subprocess.TimeoutExpired(['psql'], 7200)
                    if self.calls == 2:
                        raise run_exp04.CommandError('psql failed (1): ERROR:  invalid page in block 7')
                return super().query(sql, host=host, port=port, log_name=log_name, timeout=timeout,
                                     database=database)

        orchestrator, trial = self.trial(cls=Flaky)
        live = [record for record in orchestrator.records if 'oracle' in (record.get('verification') or {})]
        first, second = live[0], live[1]
        self.assertEqual(first['verification']['oracle']['atomic'], 'unverified')
        self.assertEqual((first['verification']['oracle']['cut'], first['status']), ('unverified', 'unverified'))
        self.assertEqual((second['verification']['oracle']['atomic'], second['status']), ('wrong', 'fail'))
        self.assertIn('atomic', second['error'])
        self.assertEqual(trial['status'], 'fail')

    def test_a_copy_its_tool_aborted_is_wrong_and_a_failed_ssh_is_the_harnesss(self):
        class Aborting(LoadStub):
            aborted = ssh = bracket = 0

            def pgdelta_transfer(self, method, paths, receiver, launched_in):
                commands = super().pgdelta_transfer(method, paths, receiver, launched_in)
                if method == 'pgdelta' and not self.aborted:  # the receiver finds a bad page and exits 3
                    self.aborted += 1
                    commands[0] = {**commands[0], 'exit_code': 3}
                return commands

            def stop_backup_hold(self, hold):
                command = super().stop_backup_hold(hold)
                if not self.ssh:  # ssh itself failed under backup-hold
                    self.ssh += 1
                    command.update(exit_code=run_exp04.SSH_FAILED, over_ssh=True)
                return command

            def remote_command(self, argv, log_name, *, cwd=None):
                result = super().remote_command(argv, log_name, cwd=cwd)
                if Path(str(argv[0])).name == 'psql' and log_name.startswith('bracket') and not self.bracket:
                    self.bracket += 1
                    result['exit_code'] = 3  # backup mode refused, on an arm with no output
                return result

        orchestrator, trial = self.trial(cls=Aborting)
        records = orchestrator.records
        aborted = next(r for r in records if r['method'] == 'pgdelta' and r['failure_kind'] == 'tool')
        # Wrong, and still wrong after its source half (S removed) ran before the next settle.
        self.assertEqual(aborted['status'], 'fail')
        self.assertIn('the copy itself failed', aborted['error'])
        self.assertNotIn('verification', aborted)
        self.assertIn('tail', aborted)  # the quiet tail still waited out
        self.assertEqual(next(r['status'] for r in trial['runs'] if (r['repeat'], r['method']) == (
            aborted['repeat'], 'pgdelta') and r['speed'] == aborted['speed']['label']), 'fail')
        ssh = next(r for r in records if r['method'] in measure.V1PROXY_METHODS and r['status'] == 'error')
        self.assertEqual(ssh['failure_kind'], 'harness')
        bracket = next(r for r in records if r['method'] == 'bracket' and r['status'] != 'pass')
        self.assertEqual((bracket['status'], bracket['failure_kind']), ('error', 'tool'))
        self.assertEqual(trial['status'], 'fail')

    def failing_trial(self, failure, fetch_fails=False):
        """A trial whose link step fails when the warm block starts (an SSH failure, or remote.py stop's SIGTERM as
        the KeyboardInterrupt _terminate raises); (orchestrator, trial, removed paths, the error raised)."""
        class Failing(LoadStub):
            block = None
            removed: list = []

            def run_load_block(self, trial, block, *args):
                self.block = block
                return super().run_load_block(trial, block, *args)

            def set_speed(self, speed):
                if self.block == 'warm':
                    raise failure
                return super().set_speed(speed)

            def _source_agent(self, *args, timeout=600):
                if args[0] == 'remove':
                    self.removed.extend(args[3:])
                return super()._source_agent(*args, timeout=timeout)

            def fetch_load_logs(self):
                if fetch_fails:
                    self.at('dest', 'fetch the load logs')
                    raise run_exp04.CommandError('tar: connection reset')
                return super().fetch_load_logs()

        Failing.removed = []
        orchestrator = self.orchestrator(Failing, ['--repeats', '1', '--warm-source-repeats', '1',
                                                   '--warm-source-speeds', '2', '--warm-source-methods', 'full,v1proxy',
                                                   '--moderate-repeats', '0', '--calibration-repeats', '0'])
        raised = None
        try:
            trial = orchestrator.run_trial(0, 'hot', 0.01)
        except BaseException as exc:  # the interrupt goes on up, past the trial's clean-up
            raised, trial = exc, orchestrator.current_trial
        return orchestrator, trial, Failing.removed, raised

    def test_an_error_or_an_interrupt_mid_trial_keeps_the_loads_logs(self):
        for failure in (run_exp04.CommandError('ssh: connect to host source port 22: Connection timed out'),
                        KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                orchestrator, trial, removed, raised = self.failing_trial(failure)
                self.assertIs(raised, None if isinstance(failure, Exception) else failure)
                busy = [record for record in orchestrator.records if record['block'] == 'busy']
                self.assertTrue(busy)
                events = [what for kind, what, _ in orchestrator.timeline]
                tail = events[events.index('script load-stop'):]
                self.assertEqual([what for what in tail if what in ('script load-stop', 'agent sampler',
                                                                    'agent compress', 'fetch the load logs')],
                                 ['script load-stop', 'agent sampler', 'agent compress', 'fetch the load logs'])
                # The logs are on dest, the busy runs' cuts were read from them, and only then was the directory
                # removed.
                self.assertTrue(any(orchestrator.load_logs.iterdir()))
                self.assertTrue(trial['load']['kept_after_error']['fetched'])
                self.assertEqual(trial['load_checks']['outputs'],
                                 sum(1 for r in busy if (r.get('verification') or {}).get('oracle')))
                self.assertIn(orchestrator.load_dir, removed)
                self.assertGreater(len(events), events.index('fetch the load logs'))
                saved = json.loads((orchestrator.out / 'trials' / f'{orchestrator.trial_id}.json').read_text())
                self.assertTrue(saved['load']['kept_after_error']['fetched'])
                if raised is None:
                    self.assertEqual(trial['status'], 'error')

    def test_a_failed_compress_at_step_15_fetches_the_logs_as_they_are(self):
        class NoZstd(LoadStub):
            def _source_agent(self, *args, timeout=600):
                if args[0] == 'compress':
                    self.at('source', 'agent compress')
                    raise run_exp04.CommandError('source agent compress failed (1): zstd: not found')
                return super()._source_agent(*args, timeout=timeout)

        orchestrator, trial = self.trial(cls=NoZstd)
        self.assertEqual(trial['status'], 'error')
        events = [what for _, what, _ in orchestrator.timeline]
        # Step 15 stopped the sampler once; the clean-up compressed again, then fetched the files as they are.
        self.assertEqual(events.count('agent sampler'), 2)  # its start and its one stop
        self.assertEqual(events.count('agent compress'), 2)
        self.assertEqual(events.count('fetch the load logs'), 1)
        self.assertTrue(trial['load']['kept_after_error']['fetched'])
        self.assertIn('check', trial['load']['end'])
        self.assertTrue(any(error.startswith('compress the load logs') for error in trial['cleanup_errors']))

    def test_logs_that_cannot_be_fetched_stay_on_the_source(self):
        orchestrator, trial, removed, _ = self.failing_trial(run_exp04.CommandError('ssh: broken pipe'),
                                                             fetch_fails=True)
        self.assertNotIn(orchestrator.load_dir, removed)
        self.assertIn(f'the load directory {orchestrator.load_dir} was left on the source: its logs are not on dest',
                      trial['cleanup_errors'])
        self.assertTrue(any(error.startswith('fetch the load logs: ') for error in trial['cleanup_errors']))

    def test_the_clock_probe_is_tried_again_and_without_an_offset_the_load_never_starts(self):
        def flaky(failures):
            class Flaky(LoadStub):
                calls = 0

                def clock_probe(self):
                    Flaky.calls += 1
                    if Flaky.calls <= failures:
                        self.at('both', 'clock probe failed')
                        raise run_exp04.CommandError('the clock probe did not start on the source: ')
                    return super().clock_probe()
            return Flaky
        orchestrator, trial = self.trial(cls=flaky(2))
        self.assertEqual(trial['status'], 'pass')
        self.assertEqual(trial['clock']['start']['offset_s'], 0.0)
        self.assertEqual(len(trial['clock']['start']['failed_attempts']), 2)
        self.assertTrue(all(record['window']['t0_source_utc'] is not None for record in orchestrator.records
                            if record['block'] != 'calibration'))
        # Three failures: no offset, so no load and no run; the trial ends in an error that says why.
        orchestrator = self.orchestrator(flaky(3), ['--repeats', '1'])
        orchestrator.clock_offset_s = 0.002  # an earlier trial's offset is not this one's
        trial = orchestrator.run_trial(0, 'hot', 0.01)
        self.assertEqual(trial['status'], 'error')
        self.assertIn('no clock offset between the VMs', trial['error'])
        self.assertIn('failed 3 times', trial['error'])
        events = [what for _, what, _ in orchestrator.timeline]
        self.assertEqual(events.count('clock probe failed'), 3)
        self.assertNotIn('script load-start', events)
        self.assertNotIn('script load-capacity', events)
        self.assertEqual(orchestrator.records, [])

    def test_a_load_dead_at_a_planned_stop_voids_the_run_before_it(self):
        class Ended(LoadStub):
            def _source_script(self, script, *args, timeout=7200):
                result = super()._source_script(script, *args, timeout=timeout)
                if args[0] == 'load-stop':
                    # The busy load had died during the last warm run: load-stop found it gone, sent no signal.
                    result['how'] = 'ended' if result['run_id'] == 'busy' else 'SIGALRM'
                return result

        orchestrator, trial = self.trial(cls=Ended)
        last = [record for record in orchestrator.records if record['load_run'] == 'busy'][-1]
        self.assertEqual(last['block'], 'warm')
        self.assertTrue(last['void'])
        self.assertEqual(last['void_reason'], 'the load (busy) died before its stop')
        self.assertEqual(trial['load']['died_before_stop'],
                         [{'died': 'busy', 'voided': orchestrator.run_key(last), 'signalled_utc': None,
                           'stopped_utc': None}])
        self.assertEqual(sum(record['void'] for record in orchestrator.records), 1)  # the moderate stop was a signal
        saved = json.loads(orchestrator.record_path(last).read_text())
        self.assertTrue(saved['void'])

    def test_without_the_sampler(self):
        orchestrator, trial = self.trial(['--sampler-interval-s', '0'])
        self.assertEqual(trial['status'], 'pass')
        self.assertIsNone(trial['load']['sampler'])
        self.assertNotIn('agent sampler', [what for _, what, _ in orchestrator.timeline])

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
                result = super()._source_script(script, *args, timeout=timeout)
                if args[0] == 'load-stop' and result['run_id'] == 'busy':
                    result['how'] = 'ended'  # load-stop finds the dead pgbench gone; check_load voided already
                return result

        orchestrator, trial = self.trial(cls=Dying)
        [restart] = trial['load']['restarts']
        self.assertEqual(restart['restart'], 'restart-1')
        voided = [record for record in orchestrator.records if record['void']]
        self.assertEqual(len(voided), 1)
        self.assertEqual(orchestrator.run_key(voided[0]), restart['voided'])
        self.assertIn('restart-1', [run['run_id'] for run in trial['load']['runs']])
        # The run whose step 1 found the load dead has its settle and window on the restart: its log is the restart's.
        found = orchestrator.records[3]
        self.assertEqual(found['source_untimed']['load']['restart'], 'restart-1')
        self.assertEqual((found['load_run'], found['load_rate']), ('restart-1', orchestrator.args.load_rate))
        self.assertFalse(found['void'])
        self.assertEqual(orchestrator.records[4]['load_run'], 'restart-1')


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

    def test_the_window_mapped_to_the_sources_clock_with_a_known_offset(self):
        orchestrator = self.orchestrator()
        orchestrator.clock_offset_s = -0.0035  # the source's clock 3.5 ms behind dest's
        phases = {'receiver_capture': {'started_utc': EPOCH + 10.0, 'ended_utc': EPOCH + 14.0},
                  'transfer': {'started_utc': EPOCH + 14.12, 'ended_utc': EPOCH + 20.0}}
        window = orchestrator.window(phases)
        self.assertEqual((window['t0_utc'], window['t1_utc']), (EPOCH + 10.0, EPOCH + 20.0))
        self.assertAlmostEqual(window['t0_source_utc'], EPOCH + 9.9965, places=6)
        self.assertAlmostEqual(window['t1_source_utc'], EPOCH + 19.9965, places=6)
        self.assertEqual(window['tail_s'], 10.0)
        # Staged pgdelta's coverage window is its capture's; full's its phase's.
        record = {'phases': {'receiver_capture': {'parts': {'capture': {'started_utc': EPOCH + 10.0,
                                                                         'wall_seconds': 4.0}}}}}
        self.assertEqual(orchestrator.coverage_window('pgdelta', record), [EPOCH + 10.0, EPOCH + 14.0])
        self.assertEqual(orchestrator.coverage_window('full', {'phases': {'transfer': phases['transfer']}}),
                         [EPOCH + 14.12, EPOCH + 20.0])

    def test_execute_writes_the_pins_and_refuses_what_exp12_cannot_run(self):
        class Executing(LoadStub):
            versions = {'postgres': 'postgres (PostgreSQL) 17.11', 'pg_dump': 'pg_dump (PostgreSQL) 17.11',
                        'pgbench': 'pgbench (PostgreSQL) 17.11', 'pg_amcheck': 'pg_amcheck (PostgreSQL) 17.11'}
            dest_versions = versions

            def _source_agent(self, *args, timeout=600):
                if args[0] == 'iface':
                    return {'iface': 'ens4'}
                return super()._source_agent(*args, timeout=timeout)

            def environment(self):
                common = {'deployed': {'tree_sha256': 'x'}, 'strace': 'strace -- version 6.1',
                          'data_mount': '/dev/md127 ext4 rw,noatime 1.5T', 'pg_basebackup_sync_method': True}
                return {'source': {**common, 'postgresql': self.versions, 'zstd': 'zstd v1.5.4',
                                   'fincore': 'fincore from util-linux 2.38.1', 'chrony': 'Reference ID ...'},
                        'dest': {**common, 'postgresql': self.dest_versions}}

            def check_pgdelta_jobs(self):
                return {}

            def run_trial(self, index, pattern, fraction):
                return {'trial_id': f'{index:02d}-{pattern}', 'status': 'pass'}

        def execute(cls, extra=()):
            with tempfile.TemporaryDirectory() as tmp:
                orchestrator = cls(tmp, self.clock, ['--patterns', 'hot', '--disk-test-mib', '0', '--idle-seconds', '0',
                                                     '--phase-floor-repeats', '0', *extra])
                self.addCleanup(orchestrator.pool.shutdown)
                route = mock.Mock(stdout='10.0.0.12 dev ens4 src 10.0.0.13 uid 1000\n', returncode=0)
                with mock.patch.object(run_exp04.subprocess, 'run', return_value=route), \
                        mock.patch.object(orchestrator.source, 'open'), mock.patch.object(orchestrator.source, 'close'):
                    code = orchestrator.execute()
                return code, json.loads((orchestrator.out / 'run.json').read_text())

        code, run = execute(Executing, ['--calibration-repeats', '2', '--calibration-methods',
                                        'full,pgdelta_direct_inplace,burn4'])
        self.assertEqual(code, 0)
        self.assertEqual(set(run['method_flags']), {'none', 'capture', 'bracket', 'full', 'pgdelta', 'v1proxy',
                                                    'v1proxy_s1', 'pgdelta_direct_inplace', 'burn4', 'pause2'})
        self.assertEqual(run['method_flags']['v1proxy_s1']['send'], ['--jobs', '1', '--source-kind', 'datadir'])
        self.assertEqual(run['t10a']['rates']['busy'], 6000)
        self.assertEqual(run['t10a']['controls'], ['pause2', 'burn4'])
        self.assertIn('settle', run['t10a']['timeline'])
        self.assertEqual(run['t10a_preflight']['versions']['source']['pg_dump'], 'pg_dump (PostgreSQL) 17.11')
        self.assertEqual(run['t2_preflight']['writeback_defaults']['vm.dirty_background_bytes'], 0)
        self.assertEqual(run['idle_noise_bytes'], 0)

        class Older(Executing):
            dest_versions = {**Executing.versions, 'pg_dump': 'pg_dump (PostgreSQL) 17.10'}

        with self.assertRaises(SystemExit) as refused:
            execute(Older)
        self.assertIn('one minor version', str(refused.exception))

        class NoZstd(Executing):
            def environment(self):
                env = super().environment()
                env['source']['zstd'] = None
                return env

        with self.assertRaises(SystemExit) as refused:
            execute(NoZstd)
        self.assertIn('zstd', str(refused.exception))
        with self.assertRaises(SystemExit) as refused:
            execute(Executing, ['--load-rate', '0'])
        self.assertIn('R_busy', str(refused.exception))

    def test_the_controls_record_the_agents_signal_times(self):
        orchestrator = self.orchestrator()
        orchestrator.load_current, orchestrator.load_rate = {'run_id': 'busy'}, 6000
        orchestrator.database_oids = {'postgres': 5, 'loaddb': 16384}
        orchestrator.current_trial = {'runs': [], 'load': {'restarts': [], 'warmups': []}}
        orchestrator.b0_digest, orchestrator.b0_stat = fake_digest(orchestrator.b0), {}
        link = {'block': 'busy', 'source_cache': 'cold', 'control_slot': 'busy_start'}
        record = orchestrator.run_method({'pattern': 'hot', 'fraction': 0.01, 'scale': 70}, 0,
                                         measure.parse_speed('unthrottled'), link, 'pause2', 0, ['pause2'])
        self.assertEqual(record['status'], 'pass', record.get('traceback'))
        phase = record['phases'][measure.CONTROL_PHASE]
        self.assertEqual(set(phase['control']), {'sigstop_utc', 'sigcont_utc'})
        self.assertEqual(record['tool_reported']['control'], phase['control'])
        self.assertEqual(phase['commands'][0]['argv'][1:3], ['load-pause', '2.0'])
        self.assertEqual(record['control_slot'], 'busy_start')

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

# backup-hold's stand-in: its start line, then a line of stdin, then its result line (source_cluster's protocol).
BACKUP_HOLD_STANDIN = """\
import json, sys
print('BACKUP-STARTED ' + json.dumps({'lsn': '2/A0000028', 'utc': 1.0}), flush=True)
line = sys.stdin.readline()
print('@@EXP04@@ ' + json.dumps({'start': {'lsn': '2/A0000028'}, 'stop': {'lsn': '2/A0000158', 'by': line.strip()},
                                 'one_session': True}), flush=True)
"""
# The orchestrator's real start_backup_hold and stop_backup_hold against the stand-in, run by another interpreter.
BACKUP_HOLD_CALL = """\
import json, sys, types
sys.path.insert(0, sys.argv[1])
from experiments.network import run_exp04
fake = types.SimpleNamespace(backup_hold_argv=lambda: [sys.executable, sys.argv[2]],
                             source=types.SimpleNamespace(argv=lambda argv: argv))
hold = run_exp04.Orchestrator.start_backup_hold(fake)
print(json.dumps(run_exp04.Orchestrator.stop_backup_hold(fake, hold)['backup']))
"""


class FlushingPopen(subprocess.Popen):
    """Popen as Python before 3.13 communicates (dest's Debian 12 runs 3.11): stdin flushed first, which raises
    ValueError on a pipe closed already (gh-74389, fixed in 3.13)."""

    def communicate(self, input=None, timeout=None):
        if self.stdin and not self._communication_started:
            self.stdin.flush()
        return super().communicate(input, timeout)


def older_pythons() -> list[str]:
    """Interpreters older than 3.13 here (EXP12_OLD_PYTHON first), whose communicate() still flushes stdin first."""
    found = []
    uv = map(str, Path.home().glob('.local/share/uv/python/cpython-3.1[12]*/bin/python'))
    for candidate in (os.environ.get('EXP12_OLD_PYTHON'), '/usr/bin/python3', shutil.which('python3.11'),
                      shutil.which('python3.12'), *uv):
        if not candidate or not Path(candidate).exists() or str(Path(candidate).resolve()) in found:
            continue
        candidate = str(Path(candidate).resolve())
        try:
            version = subprocess.run([candidate, '-c', 'import sys; print(sys.version_info[:2] < (3, 13))'],
                                     capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            continue
        if version == 'True':
            found.append(candidate)
    return found


class BackupHoldPipe(unittest.TestCase):
    """The proxy's backup mode over a real pipe: the orchestrator's start_backup_hold and stop_backup_hold against a
    stand-in for backup-hold (no PostgreSQL needed)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.standin = Path(self.tmp.name) / 'standin.py'
        self.standin.write_text(BACKUP_HOLD_STANDIN)

    def hold(self):
        fake = Namespace(backup_hold_argv=lambda: [sys.executable, str(self.standin)],
                         source=Namespace(argv=lambda argv: argv))
        hold = run_exp04.Orchestrator.start_backup_hold(fake)
        self.assertEqual(hold.get('start'), {'lsn': '2/A0000028', 'utc': 1.0})
        return fake, hold

    def test_the_stop_line_reaches_backup_hold_as_python_before_3_13_communicates(self):
        with mock.patch.object(run_exp04.subprocess, 'Popen', FlushingPopen):
            fake, hold = self.hold()
            command = run_exp04.Orchestrator.stop_backup_hold(fake, hold)
        self.assertEqual(command['exit_code'], 0)
        self.assertEqual(command['backup']['stop'], {'lsn': '2/A0000158', 'by': 'stop'})
        self.assertTrue(command['over_ssh'])

    def test_under_an_older_python(self):
        pythons = older_pythons()
        if not pythons:
            self.skipTest('no Python older than 3.13 here (EXP12_OLD_PYTHON names one)')
        call = Path(self.tmp.name) / 'call.py'
        call.write_text(BACKUP_HOLD_CALL)
        for python in pythons:
            with self.subTest(python=python):
                result = subprocess.run([python, '-I', str(call), str(Path(__file__).resolve().parents[1]),
                                         str(self.standin)], capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                self.assertEqual(json.loads(result.stdout.splitlines()[-1])['stop']['by'], 'stop')


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


# --- the agent's commands ----------------------------------------------------------------------

def agent_call(*argv) -> dict:
    """agent.py's command in this process, its result line parsed."""
    out = io.StringIO()
    with redirect_stdout(out):
        agent.main(list(argv))
    return run_exp04.agent_result(out.getvalue())


class AgentCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def tree(self):
        for name, size in (('base/5/1', 8192), ('base/5/2', 100), ('base/5/pg_internal.init', 10), ('PG_VERSION', 3)):
            path = self.root / 'data' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'x' * size)
        return self.root / 'data'

    def test_evict_advises_every_file_but_the_excluded(self):
        data = self.tree()
        advised = []
        with mock.patch.object(agent.os, 'posix_fadvise', create=True,
                               side_effect=lambda fd, offset, length, advice: advised.append((offset, length, advice))), \
                mock.patch.object(agent.os, 'POSIX_FADV_DONTNEED', 4, create=True):
            result = agent_call('evict', str(data / 'base'), '--exclude', 'pg_internal.init')
        self.assertEqual((result['files'], result['bytes']), (2, 8292))
        self.assertEqual(advised, [(0, 0, 4), (0, 0, 4)])
        with mock.patch.object(agent, 'os', mock.Mock(spec=['path'])), self.assertRaises(SystemExit):
            agent.cmd_evict(Namespace(paths=[str(data)], exclude=[], sync=False))

    def test_fincore_per_path_through_util_linuxs_fincore(self):
        data = self.tree()
        stub = self.root / 'bin' / 'fincore'
        stub.parent.mkdir()
        # util-linux 2.38's fincore --json --bytes: each file resident in full but PG_VERSION.
        stub.write_text('#!/bin/sh\nshift 2\nprintf \'{"fincore": [\'\nsep=""\nfor f in "$@"; do\n'
                        '  size=$(wc -c < "$f" | tr -d " ")\n'
                        '  res=$size; case "$f" in *PG_VERSION) res=0;; esac\n'
                        '  printf \'%s{"res": %s, "pages": 1, "size": %s, "file": "%s"}\' "$sep" "$res" "$size" "$f"\n'
                        '  sep=", "\ndone\nprintf \']}\\n\'\n')
        stub.chmod(0o755)
        with mock.patch.dict(os.environ, {'PATH': f'{stub.parent}:{os.environ["PATH"]}'}), \
                mock.patch.object(agent, 'FINCORE_BATCH', 2):
            result = agent_call('fincore', str(data), str(data / 'base'))
        self.assertEqual(result['paths'][str(data)], {'files': 4, 'bytes': 8305, 'resident_bytes': 8302,
                                                      'resident_share': round(8302 / 8305, 6)})
        self.assertEqual(result['paths'][str(data / 'base')]['resident_share'], 1.0)

    def test_burn_runs_busy_processes_for_its_seconds(self):
        result = agent_call('burn', '--procs', '2', '--seconds', '0.3')
        self.assertEqual((result['procs'], result['nice']), (2, 0))
        self.assertGreater(result['cpu_seconds'], 0.3)
        self.assertGreaterEqual(result['ended_utc'] - result['started_utc'], 0.3)

    def registry(self, pid, run_id='busy'):
        load = self.root / 'shm'
        measure.write_load_state(load, {'runs': [{'run_id': run_id, 'pid': pid, 'threads': 1}], 'current': run_id})
        return load

    def test_load_pause_stops_and_continues_the_loads_pgbench(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time\nwhile True: time.sleep(0.01)'])
        self.addCleanup(process.kill)
        load = self.registry(process.pid)
        result = agent_call('load-pause', '0.3', '--load-dir', str(load))
        self.assertEqual((result['pid'], result['run_id']), (process.pid, 'busy'))
        self.assertGreaterEqual(result['sigcont_utc'][0] - result['sigstop_utc'][1], 0.29)
        self.assertLessEqual(result['sigstop_utc'][0], result['sigstop_utc'][1])
        self.assertIsNone(process.poll())  # running again
        stopped = []
        with mock.patch.object(agent.os, 'kill', side_effect=lambda pid, sig: stopped.append(sig)), \
                mock.patch.object(agent.time, 'sleep', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                agent.cmd_load_pause(Namespace(load_dir=str(load), seconds=2.0))
        self.assertEqual(stopped, [signal.SIGSTOP, signal.SIGCONT])  # SIGCONT whatever happens

    def test_the_settle_gate_on_the_loads_progress_lines(self):
        load = self.registry(1)
        now = time.time()
        lines = ''.join(f'progress: {now - index:.3f} s, {tps} tps, lat 1.000 ms stddev 0.500, 0 failed, lag 0.300 ms\n'
                        for index, tps in enumerate([6000] * 11))
        measure.progress_path(load, 'busy').write_text('pgbench: setting random seed to 1\n' + lines)
        result = agent_call('settle-gate', '--rate', '6000', '--load-dir', str(load), '--min-s', '0', '--max-s', '5')
        self.assertTrue(result['passed'])
        self.assertFalse(result['unsettled'])
        self.assertEqual(result['lines'], 10)
        slow = lines.replace(' 6000 tps', ' 5000 tps', 1)
        measure.progress_path(load, 'busy').write_text(slow)
        result = agent_call('settle-gate', '--rate', '6000', '--load-dir', str(load), '--min-s', '0', '--max-s', '0')
        self.assertTrue(result['unsettled'])
        self.assertEqual(result['checks'], 1)

    def test_the_clock_probe_over_loopback(self):
        port = free_port()
        listener = {}
        thread = threading.Thread(target=lambda: listener.update(result=agent_call(
            'clock-probe', '--listen', f'127.0.0.1:{port}', '--timeout', '20')))
        with redirect_stdout(io.StringIO()):
            thread.start()
            for _ in range(200):
                try:
                    result = agent_call('clock-probe', '--connect', f'127.0.0.1:{port}', '--samples', '20')
                    break
                except ConnectionRefusedError:
                    time.sleep(0.02)
            thread.join(20)
        self.assertLess(abs(result['offset_s']), 0.01)
        self.assertLess(result['rtt_s'], 0.05)
        self.assertEqual((result['samples'], listener['result']['answered']), (20, 20))

    def test_compress_leaves_the_registry(self):
        if not shutil.which('zstd'):
            self.skipTest('no zstd here')
        load = self.registry(1)
        (load / 'busy.1').write_text(PGBENCH_LOG * 100)
        (load / 'sampler').mkdir()
        (load / 'sampler' / 'sampler.jsonl').write_text('{}\n' * 100)
        result = agent_call('compress', str(load), '--keep', measure.LOAD_STATE)
        self.assertEqual(result['files'], 2)
        self.assertTrue((load / 'busy.1.zst').exists() and (load / measure.LOAD_STATE).exists())
        self.assertFalse((load / 'busy.1').exists())
        self.assertEqual(measure.count_commits([measure.existing_log(load, 'busy.1')]), {0: 200, 1: 100})

    def test_run_tee_copies_the_output_as_it_comes_and_exits_with_the_commands_code(self):
        log = self.root / 'tee.log'
        result = subprocess.run([sys.executable, str(Path(agent.__file__)), 'run', '--tee', '--log', str(log), '--',
                                 'sh', '-c', 'echo first; echo second; exit 3'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 3)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[:2], ['first', 'second'])
        self.assertEqual(run_exp04.agent_result(result.stdout)['exit_code'], 3)
        self.assertEqual(log.read_text(), 'first\nsecond\n')

    def test_env_records_the_clocks_and_the_new_tools(self):
        for tool in ('pgbench', 'pg_amcheck', 'pg_dump', 'pg_test_fsync'):
            self.assertIn(tool, agent.PG_TOOLS)
        with mock.patch.object(agent, 'metadata', return_value=None), \
                mock.patch.object(agent, 'text_of', side_effect=lambda argv, **kwargs: ' '.join(argv)):
            env = agent.cmd_env(Namespace(iface='lo', bin_dir='/b', pg_probackup='/p'))
        self.assertEqual(env['chrony'], 'chronyc tracking')
        self.assertEqual((env['zstd'], env['fincore']), ('zstd --version', 'fincore --version'))
        self.assertEqual(env['postgresql']['pg_amcheck'], '/b/pg_amcheck --version')


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class FakeProc:
    """A /proc tree with what the sampler reads: the aggregate CPU line and run queue, the disks, and processes."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.set_stat(1000, 3)
        (root / 'diskstats').write_text(' 9 127 md127 16000 0 4096000 0 32000 0 16384000 0 0 0 0\n'
                                        ' 7 0 loop0 52 0 2214 13 0 0 0 0 0 36 13\n')

    def set_stat(self, user, running):
        (self.root / 'stat').write_text(f'cpu  {user} 0 300 50000 40 0 60 7 0 0\nprocs_running {running}\n')

    def process(self, pid, comm, cmdline, ticks, ppid=1, threads=None):
        directory = self.root / str(pid)
        directory.mkdir(exist_ok=True)
        fields = ['S', str(ppid)] + ['0'] * 9 + [str(ticks), '0'] + ['0'] * 10
        (directory / 'stat').write_text(f'{pid} ({comm}) {" ".join(fields)}\n')
        (directory / 'cmdline').write_text(cmdline.replace(' ', '\0') + '\0')
        for tid, (run, wait) in (threads or {}).items():
            (directory / 'task' / str(tid)).mkdir(parents=True, exist_ok=True)
            (directory / 'task' / str(tid) / 'schedstat').write_text(f'{run} {wait} 10\n')


class SamplerTicks(unittest.TestCase):
    def test_groups_threads_and_disks_from_proc(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = FakeProc(Path(tmp) / 'proc')
            load = Path(tmp) / 'shm'
            measure.write_load_state(load, {'runs': [{'run_id': 'busy', 'pid': 100, 'threads': 1}],
                                            'current': 'busy'})
            proc.process(100, 'pgbench', 'pgbench -n -M prepared', 50, threads={100: (5, 1), 101: (900, 40)})
            proc.process(200, 'postgres', 'postgres: checkpointer', 10)
            proc.process(201, 'postgres', 'postgres: study loaddb [local] UPDATE', 70)
            proc.process(300, 'sshd', 'sshd: bench', 5)
            sampler = agent.Sampler(Path(tmp), load, proc=proc.root)
            first = sampler.sample()
            self.assertEqual(first['g'], {})  # the processes already there count from now on
            self.assertEqual(first['run'], 3)
            self.assertEqual(first['th'], {'100': [5, 1], '101': [900, 40]})
            self.assertEqual(first['d'], {'md127': [16000, 4096000, 32000, 16384000]})
            # A tick later: pgbench used 4 ticks, a backend 6; a pgdelta sender and its spawned worker appear.
            proc.process(100, 'pgbench', 'pgbench -n -M prepared', 54, threads={100: (5, 1), 101: (950, 70)})
            proc.process(201, 'postgres', 'postgres: study loaddb [local] UPDATE', 76)
            proc.process(400, 'python3', 'python3 -m pgdelta send --source /w/snap', 30)
            proc.process(401, 'python3', 'python3 -c from multiprocessing.spawn import spawn_main', 12, ppid=400)
            proc.set_stat(1100, 7)
            sampler.rescanned -= agent.SAMPLER_RESCAN  # its next tick rescans
            second = sampler.sample()
            self.assertEqual(second['g'], {'pgbench': 4, 'backend': 6, 'pgdelta': 42})
            self.assertEqual(second['th']['101'], [950, 70])
            self.assertEqual(second['cpu'][0], 1100)
            self.assertEqual(second['pgbench'], 100)
            self.assertNotIn(300, sampler.groups)  # sshd isn't followed


# --- on a real PostgreSQL 17 ---------------------------------------------------------------------

try:
    from pgdelta_cluster import PG_BIN
except ImportError:  # pragma: no cover
    PG_BIN = Path('/opt/homebrew/opt/postgresql@17/bin')
LOAD_TOOLS = ('initdb', 'pg_ctl', 'psql', 'pgbench', 'pg_basebackup', 'pg_amcheck', 'pg_dump', 'pg_verifybackup',
              'pg_controldata')
needs_pg = unittest.skipUnless(all((PG_BIN / tool).exists() for tool in LOAD_TOOLS),
                               f'PostgreSQL 17 binaries ({", ".join(LOAD_TOOLS)}) not found in {PG_BIN}')
ENV = dict(os.environ, LC_ALL='C', LANG='C')


def cluster_call(*argv) -> dict:
    """source_cluster.py's command in this process, its result line parsed."""
    out = io.StringIO()
    with redirect_stdout(out):
        source_cluster.main([str(part) for part in argv])
    return run_exp04.agent_result(out.getvalue())


class LocalSource:
    """A source cluster as source_cluster.py keeps one: ROOT/data, its socket in ROOT/s (port 5432, socket only), user
    study, data checksums, wal_level replica; the benchmark's pgbench tables and an events table in postgres, and
    loaddb with 4 clients' rows. Under /tmp: a socket path must stay under 104 bytes."""

    CLIENTS = 4

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix='x12', dir='/tmp'))
        self.load_dir = self.root / 'shm'
        subprocess.run([str(PG_BIN / 'initdb'), '-D', str(self.root / 'data'), '-U', 'study', '--auth-local=trust',
                        '--data-checksums', '-E', 'UTF8', '--locale=C', '--no-sync'], check=True, capture_output=True,
                       env=ENV, timeout=120)
        with open(self.root / 'data' / 'postgresql.conf', 'a') as stream:
            stream.write("listen_addresses = ''\nwal_level = replica\nmax_wal_senders = 10\nfsync = off\n")
        self.common = ['--root', str(self.root), '--bin-dir', str(PG_BIN)]
        cluster_call('start', *self.common, '--log-dir', self.root / 'logs')
        self.sql('postgres', 'SELECT 1')
        subprocess.run([str(PG_BIN / 'pgbench'), '-i', '-q', '-s', '1', '-h', str(self.root / 's'), '-U', 'study',
                        'postgres'], check=True, capture_output=True, env=ENV, timeout=300)
        self.sql('postgres', 'CREATE TABLE events (id bigint PRIMARY KEY, payload text); INSERT INTO events '
                             "SELECT g, md5(g::text) FROM generate_series(1, 1000) g;")
        self.setup = cluster_call('loaddb-setup', *self.common, '--log-dir', self.root / 'logs', '--load-dir',
                                  self.load_dir, '--clients', self.CLIENTS)

    def sql(self, database, statement):
        return subprocess.run([str(PG_BIN / 'psql'), '-X', '-A', '-t', '-q', '-v', 'ON_ERROR_STOP=1', '-h',
                               str(self.root / 's'), '-U', 'study', '-d', database, '-c', statement], check=True,
                              capture_output=True, text=True, env=ENV, timeout=120).stdout.strip()

    def load(self, *argv):
        return cluster_call(*argv[:1], *self.common, '--load-dir', self.load_dir, *argv[1:])

    def pgbench(self):
        return ['--clients', str(self.CLIENTS), '--threads', '2', '--seed', '7']

    def close(self):
        state = measure.read_load_state(self.load_dir)
        current = measure.load_run(state)
        if current is not None and source_cluster.process_alive(current['pid']):
            os.kill(current['pid'], signal.SIGKILL)
        subprocess.run([str(PG_BIN / 'pg_ctl'), '-D', str(self.root / 'data'), '-m', 'immediate', '-w', 'stop'],
                       capture_output=True, env=ENV, timeout=60)
        shutil.rmtree(self.root, ignore_errors=True)


@needs_pg
class TheLoadOnARealCluster(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = LocalSource()

    @classmethod
    def tearDownClass(cls):
        cls.source.close()

    def test_1_loaddb(self):
        setup = self.source.setup
        self.assertEqual(setup['settings'], measure.LOAD_SERVER_CONFIG)
        relations = setup['relations']
        self.assertEqual(relations['load_progress']['pages'], LocalSource.CLIENTS)  # one row per page
        self.assertEqual(relations['pgbench_accounts']['options'], ['fillfactor=90', 'autovacuum_enabled=false'])
        self.assertEqual(relations['load_progress']['options'], ['fillfactor=10', 'autovacuum_enabled=false'])
        # And off on every TOAST table they have (load_progress's 900-byte filler gives it one).
        tables = ', '.join(f"'{table}'" for table in measure.LOAD_TABLES)
        self.assertEqual(self.source.sql('loaddb', "SELECT count(*), count(*) FILTER (WHERE 'autovacuum_enabled=false' "
                                                   "= ANY(t.reloptions)) FROM pg_class c JOIN pg_class t ON t.oid = "
                                                   f"c.reltoastrelid WHERE c.relname IN ({tables})"), '1|1')
        self.assertEqual(setup['counters']['n'], {str(client): 0 for client in range(LocalSource.CLIENTS)})
        self.assertEqual((setup['counters']['sum_n'], setup['counters']['sum_abalance']), (0, 0))
        self.assertIn('loaddb', setup['databases'])
        self.assertEqual(self.source.sql('loaddb', "SELECT extname FROM pg_extension WHERE extname = 'amcheck'"),
                         'amcheck')

    def test_2_the_scripts_invariant_under_concurrent_clients(self):
        source = self.source
        capacity = source.load('load-capacity', *source.pgbench(), '--run-id', 'capacity', '--warmup-s', '1',
                               '--seconds', '2', '--runs', '1')
        self.assertTrue(capacity['all_complete'])
        self.assertGreater(capacity['capacity_tps'], 0)
        run = source.load('load-start', *source.pgbench(), '--run-id', 'busy', '--rate', '400', '--duration-s', '600')
        self.assertEqual(run['start']['n'], capacity['runs'][-1]['stop']['n'])  # the offset: where the last stopped
        time.sleep(1.5)
        paused = agent_call('load-pause', '0.5', '--load-dir', str(source.load_dir))
        self.assertEqual(paused['pid'], run['pid'])
        time.sleep(1.0)
        status = cluster_call('load-status', '--load-dir', source.load_dir)
        self.assertTrue(status['running'])
        stopped = source.load('load-stop')
        self.assertEqual(stopped['how'], 'SIGALRM')
        self.assertTrue(stopped['complete'], stopped['mismatched'])  # SIGALRM leaves a complete log
        self.assertGreater(stopped['commits_total'], 400)
        self.assertFalse(stopped['aborted'])
        check = source.load('load-check')
        self.assertTrue(check['ok'], check)
        self.assertEqual(check['sum_abalance'], check['sum_n'])
        self.assertEqual(check['expected_commits'], sum(r['commits_total'] for r in measure.read_load_state(
            source.load_dir)['runs']))
        # Rolled-back transactions ran, and none leaked: the oracle's invariant holds on the live source.
        logs = [source.load_dir / name for name in stopped['log_files']]
        scripts = [measure.parse_pgbench_line(line)[3] for path in logs for line in path.read_text().splitlines()]
        self.assertIn(measure.ABORT_SCRIPT, scripts)
        answer = json.loads(source.sql('loaddb', measure.load_oracle_sql()))
        self.assertTrue(measure.atomicity(answer)['ok'], answer)
        # pause2's gap is in the log: no completion while pgbench was stopped.
        ends = sorted(measure.transaction_times(measure.parse_pgbench_line(line))[2] / 1e6
                      for path in logs for line in path.read_text().splitlines())
        stop, cont = paused['sigstop_utc'][1], paused['sigcont_utc'][0]
        self.assertFalse([end for end in ends if stop + 0.005 < end < cont])
        # A second start while one runs is refused.
        source.load('load-start', *source.pgbench(), '--run-id', 'again', '--rate', '100', '--duration-s', '600')
        with self.assertRaises(SystemExit):
            source.load('load-start', *source.pgbench(), '--run-id', 'again2', '--rate', '100')
        source.load('load-stop')

    def test_2b_sigints_fallback_on_a_real_pgbench(self):
        # pgbench 17 doesn't catch SIGINT: its threads' buffered last lines can be lost, so the run is judged by the
        # fallback's own rule (every line up to 1 s before the signal), and load-check sums its counters.
        source = self.source
        source.load('load-start', *source.pgbench(), '--run-id', 'interrupted', '--rate', '2000', '--duration-s', '600')
        time.sleep(3.0)
        stopped = source.load('load-stop', '--signal', 'INT')
        self.assertEqual(stopped['how'], 'SIGINT')
        self.assertEqual(stopped['completeness']['criterion'], 'sigint')
        self.assertTrue(stopped['complete'], stopped['completeness'])
        self.assertEqual(stopped['committed'], sum(int(n) for n in stopped['stop']['n'].values())
                         - sum(int(n) for n in stopped['start']['n'].values()))
        self.assertGreaterEqual(stopped['committed'], stopped['commits_total'])
        check = source.load('load-check')
        self.assertTrue(check['ok'], check)

    def test_3_backup_hold_closes_on_stdin(self):
        script = Path(source_cluster.__file__)
        for close in ('line', 'end of stdin'):
            with self.subTest(close=close):
                hold = subprocess.Popen([sys.executable, str(script), 'backup-hold', *self.source.common, '--label',
                                         't10a'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=ENV)
                line = hold.stdout.readline()
                self.assertTrue(line.startswith(source_cluster.BACKUP_STARTED), line)
                start = json.loads(line[len(source_cluster.BACKUP_STARTED):])
                # Backup mode is held: its session shows in pg_stat_activity until the stop.
                self.assertEqual(self.source.sql('postgres', "SELECT count(*) FROM pg_stat_activity WHERE query "
                                                             "LIKE 'SELECT pg_backup_start%'"), '1')
                if close == 'line':
                    hold.stdin.write('stop\n')
                hold.stdin.close()
                result = run_exp04.agent_result(hold.stdout.read())
                self.assertEqual(hold.wait(30), 0)
                self.assertEqual(result['stop']['by'], close)
                self.assertEqual(result['start']['lsn'], start['lsn'])
                self.assertGreaterEqual(measure.parse_lsn(result['stop']['lsn']), measure.parse_lsn(start['lsn']))
                self.assertTrue(result['one_session'])

    def test_4_two_schema_dumps_of_one_state_hash_equal(self):
        def dump(*extra):
            return subprocess.run([str(PG_BIN / 'pg_dump'), '--schema-only', *extra, '-h', str(self.source.root / 's'),
                                   '-U', 'study', '-d', 'loaddb'], check=True, capture_output=True, text=True,
                                  env=ENV).stdout

        key = measure.schema_dump_argv('pg_dump', host='h', port=1, user='u', database='d')[2]
        first, second = dump(key), dump(key)
        self.assertEqual(first, second)
        self.assertIn(r'\restrict exp12', first)
        if r'\restrict' in dump():  # PostgreSQL 17.6 and later: a random key without --restrict-key
            self.assertNotEqual(dump(), dump())

    def test_5_the_snapshot(self):
        snapshot = self.source.root / 'snap'
        copy = ('cp', '-a') if sys.platform == 'darwin' else source_cluster.SNAPSHOT_COPY  # BSD cp has no --reflink
        with mock.patch.object(source_cluster, 'SNAPSHOT_COPY', copy):
            result = cluster_call('snapshot', *self.source.common, '--log-dir', self.source.root / 'logs',
                                  '--snapshot', snapshot)
        self.assertEqual(result['stop']['state'], 'shut down')
        self.assertTrue(result['restart']['started'])
        self.assertFalse((snapshot / 'postmaster.pid').exists())
        digest = result['digest']['digest']
        self.assertIn('global/pg_control', digest['entries'])
        self.assertEqual(digest['files'], len(result['digest']['selected']))
        self.assertEqual(self.source.sql('postgres', 'SELECT 1'), '1')  # running again


@needs_pg
class TheOracleOnRealCopies(unittest.TestCase):
    """A good full copy taken under the load passes every check; each of the seven negative controls fails through
    its own check (smoke item 5), with the cut and coverage judged from the load's logs."""

    @classmethod
    def setUpClass(cls):
        cls.source = source = LocalSource()
        cls.work = Path(tempfile.mkdtemp(prefix='x12w', dir='/tmp'))
        args = run_exp04.parse_args([
            '--source-ip', str(source.root / 's'), '--out', str(cls.work / 'out'), '--work-dir', str(cls.work / 'w'),
            '--bin-dir', str(PG_BIN), '--gnu-time', 'none', '--load', 'bank', '--load-rate', '300',
            '--manifest-checksums', 'CRC32C', '--methods', 'full'])
        cls.orchestrator = orchestrator = run_exp04.Orchestrator(args)
        orchestrator.work.mkdir(parents=True)
        orchestrator.outputs.mkdir(parents=True)
        orchestrator.trial_id = 'local'
        with mock.patch.object(run_exp04, 'log'):
            orchestrator.take_references('start')
            source.load('load-capacity', *source.pgbench(), '--run-id', 'capacity', '--warmup-s', '0',
                        '--seconds', '1', '--runs', '1')
            source.load('load-start', *source.pgbench(), '--run-id', 'busy', '--rate', '300', '--duration-s', '600')
            time.sleep(1.0)
            cls.result = orchestrator.run_oracle_controls()
            time.sleep(0.5)
            source.load('load-stop')
            orchestrator.load_runs = measure.read_load_state(source.load_dir)['runs']
            orchestrator.load_logs.mkdir(parents=True)
            for run in orchestrator.load_runs:
                for name in run['log_files']:
                    shutil.copy(source.load_dir / name, orchestrator.load_logs / name)
            cls.trial = {'oracle_controls': cls.result}
            cls.finish = orchestrator.finish_load_checks(cls.trial)

    @classmethod
    def tearDownClass(cls):
        cls.orchestrator.pool.shutdown()
        cls.source.close()
        shutil.rmtree(cls.work, ignore_errors=True)

    def controls(self):
        return {control['name']: control for control in self.result['controls']}

    def test_the_good_copy_passes_every_check(self):
        good = self.controls()['good']
        self.assertTrue(good['verdict']['ok'], good['oracle'])
        self.assertTrue(good['oracle']['static_digest'])
        self.assertGreater(good['oracle']['amcheck_relations'], 10)
        self.assertEqual(good['oracle']['cut'], 'ok')
        self.assertEqual(good['oracle']['coverage'], 'ok', good['oracle'].get('coverage_detail'))
        # The good copy's cut lies inside the logged busy run.
        detail = good['oracle']['cut_detail']
        self.assertIsNotNone(detail['lo_us'])
        self.assertIsNotNone(detail['hi_us'])

    def test_each_negative_control_fails_through_its_own_check(self):
        controls = self.controls()
        self.assertEqual(set(controls) - {'good'}, set(measure.ORACLE_CONTROLS))
        for name, target in measure.ORACLE_CONTROLS.items():
            with self.subTest(control=name):
                control = controls[name]
                self.assertTrue(control['verdict']['ok'], (control['verdict'], control['oracle'],
                                                           control['output_start'].get('error')))
                self.assertEqual(control['oracle'][target], 'wrong')

    def test_what_each_control_shows(self):
        controls = self.controls()
        self.assertEqual(controls['no_wal']['oracle']['start'], 'wrong')
        self.assertEqual(controls['n_raised']['output_start']['checks']['invariant']['difference'], -1)
        for name in ('cut_extended', 'cut_back'):
            self.assertEqual(controls[name]['oracle']['atomic'], 'ok')  # the balance moved with n
        self.assertIn('invalid page', controls['heap_page']['output_start']['checks']['invariant']['error'])
        self.assertEqual(controls['index_page']['oracle']['atomic'], 'ok')
        self.assertEqual(controls['schema']['output_start']['checks']['schema']['differ'], ['loaddb'])


if __name__ == '__main__':
    unittest.main()
