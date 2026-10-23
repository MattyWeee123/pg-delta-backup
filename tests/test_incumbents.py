"""Tests for T11's incumbent-tool harness (experiments/incumbents) and EXP-03's S2b."""

import contextlib
import csv
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from experiments.incumbents import checks, run_t11, tools
from experiments.tricky_copies import lsnscan, pgcluster, run_scenarios, scenarios
from experiments.tricky_copies.lsnscan import BLOCK
from test_tricky_copies import heap_page

PG_BIN = Path(os.environ.get('PGDELTA_TEST_PG_BIN', '/opt/homebrew/opt/postgresql@17/bin'))

PG_REWIND_S2 = """\
pg_rewind: servers diverged at WAL location 0/415CDA8 on timeline 1
pg_rewind: rewinding from last common checkpoint at 0/415CD30 on timeline 1
pg_rewind: Done!
"""
PG_REWIND_S1 = """\
pg_rewind: source and target cluster are on the same timeline
pg_rewind: no rewind required
"""
PG_REWIND_CRASHED = """\
pg_rewind: executing "/usr/lib/postgresql/17/bin/postgres" for target server to complete crash recovery
LOG:  database system was not properly shut down; automatic recovery in progress
""" + PG_REWIND_S2
PROBACKUP_OK = """\
INFO: Catchup start, pg_probackup version: 2.5.16, PostgreSQL version: 17.11, remote: false, catchup-mode: DELTA
INFO: syncLSN = 0/415F070
INFO: pg_start_backup START LSN 0/7000028
INFO: Databases synchronized. Transfered datafiles size: 1290kB, transfered wal size: 16MB, time elapsed: 1s
INFO: Catchup incremental ratio (less is better): 4% (1290kB/31MB)
"""
PROBACKUP_REFUSED = """\
INFO: Catchup start, pg_probackup version: 2.5.16, PostgreSQL version: 17.11, remote: false, catchup-mode: DELTA
ERROR: Destination is not in source timeline history
"""
# What WAL-G v3.0.9 prints for `wal-g catchup-push --help`: --from-lsn is a pflag Uint64Var.
WALG_PUSH_HELP = """\
Creates incremental backup from lsn

Usage:
  wal-g catchup-push PGDATA --from-lsn LSN [flags]

Flags:
      --from-lsn uint   LSN to start incremental backup
  -h, --help            help for catchup-push
"""
# A wal-g stand-in that parses catchup-push's --from-lsn the way v3.0.9 does (pflag's
# strconv.ParseUint(s, 0, 64)), logs a backup name, and has catchup-fetch leave a mark.
FAKE_WALG = r"""#!/bin/sh
case "$1" in
catchup-push)
  case "$4" in
  ''|*[!0-9]*) echo "invalid argument \"$4\" for \"--from-lsn\" flag: strconv.ParseUint: parsing \"$4\": invalid syntax"; exit 1;;
  esac
  echo "INFO: Wrote backup with name base_000000020000000000000007_D_000000010000000000000004" >&2;;
catchup-fetch) touch "$2/fetched-$3";;
*) exit 1;;
esac
"""


def page(lsn, fill=b'x'):
    """An 8 KiB page whose pd_lsn is ``lsn``."""
    return (lsn >> 32).to_bytes(4, 'little') + (lsn & 0xFFFFFFFF).to_bytes(4, 'little') + (
        fill * BLOCK)[:BLOCK - 8]


def write(root, relpath, data):
    path = Path(root, relpath)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class OutcomeTest(unittest.TestCase):
    def test_classify_outcome(self):
        patterns = checks.REFUSAL_PATTERNS['pg_probackup']
        self.assertEqual(checks.classify_outcome(0, True, '', patterns),
                         ('accepted', 'exit status 0'))
        outcome, reason = checks.classify_outcome(1, True, PROBACKUP_REFUSED, patterns)
        self.assertEqual(outcome, 'refused')
        self.assertIn('Destination is not in source timeline history', reason)
        self.assertEqual(checks.classify_outcome(2, False, 'boom', patterns)[0], 'refused')
        self.assertEqual(checks.classify_outcome(2, True, 'boom', patterns),
                         ('failed', 'exit status 2 after the copy was modified'))
        self.assertEqual(checks.classify_outcome(None, False, '', patterns)[0], 'failed')
        self.assertEqual(checks.classify_outcome(0, True, '', patterns, timed_out=True),
                         ('failed', 'timed out'))

    def test_every_tool_has_refusal_patterns(self):
        self.assertEqual(set(checks.REFUSAL_PATTERNS) | {'lsn_skip'}, set(tools.TOOLS))
        self.assertTrue(checks.refusal_message(
            'INFO: x\nERROR: Destination is on timeline 1, but we are on 2\n',
            checks.REFUSAL_PATTERNS['walg_send']).startswith('ERROR: Destination'))
        self.assertTrue(checks.refusal_message(
            'pg_rewind: error: could not find common ancestor of the source and target '
            "cluster's timelines", checks.REFUSAL_PATTERNS['pg_rewind']))

    def test_verdict(self):
        self.assertEqual(checks.verdict('accepted', True, True, 0, True), 'repaired')
        self.assertEqual(checks.verdict('accepted', True, True, None, True), 'repaired')
        self.assertEqual(checks.verdict('accepted', False, True, 0, True), 'wrong_data')
        self.assertEqual(checks.verdict('accepted', True, False, None, True), 'wrong_data')
        # Stale pages are wrong data even when the copy never came up.
        self.assertEqual(checks.verdict('accepted', None, None, 3, False), 'wrong_data')
        self.assertEqual(checks.verdict('accepted', None, None, 0, False), 'not_verified')
        for outcome in ('refused', 'failed', 'skipped'):
            self.assertEqual(checks.verdict(outcome, None, None, None, None), outcome)


class ParseTest(unittest.TestCase):
    def test_pg_rewind(self):
        rewound = checks.parse_pg_rewind(PG_REWIND_S2)
        self.assertEqual(rewound['diverged_at'], '0/415CDA8')
        self.assertEqual(rewound['diverged_on_timeline'], 1)
        self.assertEqual(rewound['last_common_checkpoint'], '0/415CD30')
        self.assertTrue(rewound['done'])
        self.assertFalse(rewound['no_rewind_required'] or rewound['crash_recovery_run'])
        same = checks.parse_pg_rewind(PG_REWIND_S1)
        self.assertTrue(same['no_rewind_required'] and same['same_timeline'])
        self.assertIsNone(same['diverged_at'])
        self.assertTrue(checks.parse_pg_rewind(PG_REWIND_CRASHED)['crash_recovery_run'])

    def test_pg_probackup(self):
        ok = checks.parse_pg_probackup(PROBACKUP_OK)
        self.assertEqual(ok, {'sync_lsn': '0/415F070', 'start_lsn': '0/7000028',
                              'synchronized': True, 'incremental_ratio_percent': 4,
                              'errors': []})
        refused = checks.parse_pg_probackup(PROBACKUP_REFUSED)
        self.assertFalse(refused['synchronized'])
        self.assertEqual(refused['errors'], ['Destination is not in source timeline history'])

    def test_walg(self):
        stderr = ('INFO: 2026/10/23 Selecting the backup with name base_000000020000000000000007'
                  '_D_000000010000000000000004...\nINFO: Wrote backup with name '
                  'base_000000020000000000000007_D_000000010000000000000004\n')
        self.assertEqual(checks.parse_walg_backup_name(stderr),
                         'base_000000020000000000000007_D_000000010000000000000004')
        self.assertEqual(checks.parse_walg_backup_name('INFO: base_00000002000000000000000A'),
                         'base_00000002000000000000000A')
        self.assertIsNone(checks.parse_walg_backup_name('nothing here'))
        listing = json.dumps([{'backup_name': 'base_000000020000000000000005', 'time': 'x'},
                              {'backup_name': 'base_000000020000000000000007'}])
        self.assertEqual(checks.parse_walg_list_json(listing),
                         ['base_000000020000000000000005', 'base_000000020000000000000007'])
        self.assertEqual(checks.parse_walg_list_json('not json'), [])
        self.assertEqual(checks.parse_walg_list_json('{"a": 1}'), [])
        self.assertIsNone(checks.walg_sysid_flag(WALG_PUSH_HELP))
        self.assertEqual(checks.walg_from_lsn_flag(WALG_PUSH_HELP),
                         '--from-lsn uint   LSN to start incremental backup')
        self.assertIsNone(checks.walg_from_lsn_flag('Usage: wal-g catchup-fetch'))
        self.assertEqual(checks.walg_sysid_flag('      --from-sysid uint   system identifier'),
                         '--from-sysid')

    def test_conf_setting(self):
        conf = ("primary_conninfo = 'host=/tmp/a port=1'\n# primary_conninfo = 'nope'\n"
                "primary_conninfo = 'host=/tmp/b port=2 application_name=''x'''\n")
        self.assertEqual(tools.last_setting(conf, 'primary_conninfo'),
                         "host=/tmp/b port=2 application_name='x'")
        self.assertIsNone(tools.last_setting('port = 5', 'primary_conninfo'))


class DivergedPagesTest(unittest.TestCase):
    def test_compare_listed_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, source, copy = Path(tmp, 'b'), Path(tmp, 's'), Path(tmp, 'c')
            rel = 'base/5/16384'
            shared = heap_page(0x10)
            diverged = [heap_page(0x900, data=f'stale{i}!'.encode()) for i in range(6)]
            fresh = [heap_page(0x80, data=f'fresh{i}!'.encode()) for i in range(4)]
            write(old, rel, shared + b''.join(diverged[1:6]))
            write(source, rel, shared + b''.join(fresh[1:4]))
            # 0: B equals S; 1: stale; 2: repaired; 3: something else; 4: stale and S has
            # no such page; 5: absent from the copy.
            write(copy, rel, shared + diverged[1] + fresh[2] + heap_page(0x77) + diverged[4])
            pages = [{'file': rel, 'block': block} for block in range(6)]
            result = checks.compare_listed_pages(copy, old, source, pages)
        self.assertEqual({k: result[k] for k in ('listed', 'diverged_left', 'same_as_source',
                                                 'other', 'missing_in_copy', 'not_diverged')},
                         {'listed': 6, 'diverged_left': 2, 'same_as_source': 1, 'other': 1,
                          'missing_in_copy': 1, 'not_diverged': 1})
        self.assertEqual(result['diverged_left_by_kind'], {'main': 2})
        self.assertEqual(result['diverged_left_by_cause'], {'content': 1, 'absent_in_source': 1})
        self.assertEqual([e['block'] for e in result['diverged_left_examples']], [1, 4])
        self.assertIsNone(checks.read_page(tmp, rel, 0))

    def test_lsn_skip_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp, 's'), Path(tmp, 't')
            rel = 'base/5/16384'
            write(source, rel, page(0x10, b'n') + page(0x2000, b'n') + page(0x10, b'n'))
            write(source, 'global/pg_control', b'source control')
            write(source, 'pg_wal/000000020000000000000001', b'new wal')
            write(source, 'postgresql.conf', b'source conf')
            write(target, rel, page(0x10, b'o') + page(0x1500, b'o'))
            write(target, 'global/pg_control', b'old control')
            write(target, 'pg_wal/000000010000000000000001', b'old wal')
            write(target, 'base/5/99999', page(0x10))
            write(target, 'postgresql.conf', b'target conf')
            counts = tools.lsn_skip_copy(source, target, 0x1000)
            data = Path(target, rel).read_bytes()
            self.assertEqual(data[:BLOCK], page(0x10, b'o'))  # skipped: B keeps its page
            self.assertEqual(data[BLOCK:], page(0x2000, b'n') + page(0x10, b'n'))
            self.assertEqual(Path(target, 'global/pg_control').read_bytes(), b'source control')
            self.assertEqual(Path(target, 'postgresql.conf').read_bytes(), b'target conf')
            self.assertFalse(Path(target, 'base/5/99999').exists())
            self.assertEqual(sorted(p.name for p in Path(target, 'pg_wal').iterdir()),
                             ['000000020000000000000001'])
        self.assertEqual(counts, {'pages_copied': 2, 'pages_skipped': 1, 'files_copied': 2,
                                  'files_removed': 2})


class FakeContext:
    """The parts of scenarios.BuildContext that the S2/S2b checkers use."""
    check = scenarios.BuildContext.check

    def __init__(self, root):
        self.old_copy, self.source_backup = Path(root, 'b'), Path(root, 's')
        self.facts, self.observations, self.checks, self.notes = {}, {}, {}, []


class S2bCheckTest(unittest.TestCase):
    """The page-LSN check that S2b's builder records in scenario.json."""

    def test_helpers(self):
        self.assertEqual(scenarios.segment_location('base/5/1', 3), ('base/5/1', 3))
        self.assertEqual(scenarios.segment_location('base/5/1', scenarios.SEGMENT_BLOCKS + 3),
                         ('base/5/1.1', 3))
        self.assertEqual(scenarios.history_switchpoint(
            '1\t0/415f140\tno recovery target specified\n'), '0/415F140')
        self.assertEqual(scenarios.history_switchpoint(
            '1\t0/3000000\tno recovery target specified\n\n'
            '2\t0/50000A0\tno recovery target specified\n'), '0/50000A0')
        with self.assertRaises(ValueError):
            scenarios.history_switchpoint('# nothing\n')

    def test_pages_after_lsn(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, 'base/5/16384', page(0x10) + page(0x5000) + page(0x20) + b'partial')
            write(tmp, 'base/5/16384.1', page(0x7000))
            write(tmp, 'base/5/16384_fsm', page(0x6000))
            write(tmp, 'global/pg_control', page(0x9000))  # not a relation file
            write(tmp, 'pg_wal/000000010000000000000001', page(0x9000))
            pages = scenarios.pages_after_lsn(tmp, 0x1000)
        self.assertEqual(pages, [
            {'file': 'base/5/16384', 'block': 1, 'kind': 'main', 'lsn': '0/5000'},
            {'file': 'base/5/16384.1', 'block': 0, 'kind': 'main', 'lsn': '0/7000'},
            {'file': 'base/5/16384_fsm', 'block': 0, 'kind': 'fsm', 'lsn': '0/6000'}])

    def make(self, tmp, *, state='in production', redo='0/700', checkpoint='0/750',
             on_disk_lsn=0x900):
        ctx = FakeContext(tmp)
        rel = 'base/5/16384'
        write(ctx.old_copy, rel, heap_page(0x100) + heap_page(on_disk_lsn, data=b'diverged')
              + heap_page(0x950, data=b'diverged'))
        write(ctx.source_backup, rel, heap_page(0x100) + heap_page(0x80) + heap_page(0x90))
        write(ctx.source_backup, 'pg_wal/00000002.history',
              b'1\t0/800\tno recovery target specified\n')
        ctx.facts = {'diverged': {'file': rel, 'blocks': [1, 2]}, 'split_lsn': '0/800',
                     'old_primary_end_lsn': '0/960', 'flush': {'clean': True}}
        old = {'timeline': 1, 'system_identifier': '7', 'cluster_state': state,
               'redo_location': redo, 'checkpoint_location': checkpoint}
        scenarios.check_s2b(ctx, old, {'timeline': 2, 'system_identifier': '7'},
                            {'lsn': redo})
        return ctx

    def test_check_s2b(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.make(tmp)
        failed = [name for name, check in ctx.checks.items() if not check['ok']]
        self.assertEqual(failed, [])
        self.assertEqual(set(ctx.checks), {'diverged_pages_differ', 'timeline_switched',
                                           'same_system_identifier', 'old_copy_crashed',
                                           'no_checkpoint_after_fork',
                                           'diverged_pages_on_disk_after_fork'})
        self.assertEqual(ctx.observations['fork_lsn'], '0/800')
        self.assertEqual(ctx.observations['old_copy_redo_lsn'], '0/700')
        self.assertEqual(ctx.observations['post_fork_page_count'], 2)
        self.assertEqual([(p['block'], p['lsn']) for p in ctx.observations['post_fork_pages']],
                         [(1, '0/900'), (2, '0/950')])

    def test_check_s2b_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            clean = self.make(Path(tmp, 'a'), state='shut down', redo='0/958',
                              checkpoint='0/960')
            unflushed = self.make(Path(tmp, 'b'), on_disk_lsn=0x400)
        self.assertFalse(clean.checks['old_copy_crashed']['ok'])
        self.assertFalse(clean.checks['no_checkpoint_after_fork']['ok'])
        self.assertTrue(clean.checks['diverged_pages_on_disk_after_fork']['ok'])
        self.assertFalse(unflushed.checks['diverged_pages_on_disk_after_fork']['ok'])
        self.assertEqual(unflushed.checks['diverged_pages_on_disk_after_fork'][
            'pages_with_lsn_after_fork'], 1)

    def test_evict_sql_by_version(self):
        self.assertIn('FILTER (WHERE pg_buffercache_evict(b.bufferid))',
                      scenarios.evict_dirty_user_buffers_sql(17))
        self.assertIn('FILTER (WHERE (pg_buffercache_evict(b.bufferid)).buffer_evicted)',
                      scenarios.evict_dirty_user_buffers_sql(18))

    def test_registry_and_selection(self):
        spec = scenarios.BY_ID['S2b']
        self.assertEqual(spec.dirname, 'S2b-ex-primary-no-checkpoint-after-fork')
        self.assertIs(spec.checker, scenarios.check_s2b)
        self.assertEqual([s.id for s in run_scenarios.select_scenarios(['s2b', 'S1B', 'S2'])],
                         ['S2b', 'S1b', 'S2'])
        conf = {'source': {'wal_keep_size': '1GB'}, 'old_copy': {}, 'source_ran_with': 'source',
                'old_copy_ran_with': 'old_copy'}
        meta = scenarios.scenario_metadata(spec, rows=1, built_ok=True,
                                           source_cluster={'name': 'standby', 'kept': True,
                                                           'datadir': 'pgdwork-x/standby'},
                                           server_conf=conf)
        self.assertEqual(meta['source_cluster']['datadir'], 'pgdwork-x/standby')
        self.assertEqual(meta['server_conf'], conf)
        self.assertEqual(scenarios.scenario_metadata(spec, rows=1, built_ok=False)['server_conf'],
                         {'source': {}, 'old_copy': {}})

    def test_role_settings(self):
        """A standby cloned from a primary of another role gets its own role's settings,
        and the primary's role's settings reset to their defaults."""
        class Recorder:
            def __init__(self, name):
                self.name, self.appended, self.queries = name, [], []

            def append_conf(self, settings):
                self.appended.append(settings)

            def query(self, sql):
                self.queries.append(sql)
                return [['wal_keep_size', '0']]

        ctx = SimpleNamespace(role_conf={'source': {'wal_keep_size': '4GB'}, 'old_copy': {}},
                              step=lambda text: None)
        switch = scenarios.BuildContext.switch_role_conf
        primary, standby = Recorder('primary'), Recorder('standby')
        switch(ctx, standby, primary, 'source', 'old_copy')  # S1: R becomes B
        self.assertEqual(standby.appended, [{'wal_keep_size': '0'}])
        self.assertIn("name IN ('wal_keep_size')", primary.queries[0])
        primary, standby = Recorder('primary'), Recorder('standby')
        switch(ctx, standby, primary, 'old_copy', 'source')  # S2: P becomes B, R the source
        self.assertEqual((standby.appended, primary.queries), ([{'wal_keep_size': '4GB'}], []))
        ctx.role_conf['old_copy'] = {'wal_keep_size': '1GB'}
        primary, standby = Recorder('primary'), Recorder('standby')
        switch(ctx, standby, primary, 'source', 'old_copy')
        self.assertEqual((standby.appended, primary.queries), ([{'wal_keep_size': '1GB'}], []))


class FlushWithoutCheckpointTest(unittest.TestCase):
    """S2b's flush on a real server, for each of PostgreSQL 17 and 18 that is installed:
    pg_buffercache_evict() returns a boolean in 17 and a record in 18."""

    def flush(self, bindir):
        sockdir = pgcluster.short_tempdir()
        cluster = pgcluster.Cluster('flush', pgcluster.PgBin(bindir), sockdir / 'data', sockdir,
                                    pgcluster.free_port(), sockdir / 'server.log')
        try:
            cluster.initdb()
            cluster.start()
            cluster.psql('CREATE EXTENSION pg_buffercache;\n'
                         'CREATE TABLE t AS SELECT g AS id, md5(g::text) AS v '
                         'FROM generate_series(1, 2000) g;\nCHECKPOINT;\n')
            checkpoint = cluster.scalar('SELECT checkpoint_lsn FROM pg_control_checkpoint()')
            cluster.psql('UPDATE t SET v = upper(v) WHERE id = 1;')
            written = cluster.scalar('SELECT pg_current_wal_lsn()')
            flush = scenarios.flush_without_checkpoint(cluster)
            after = cluster.scalar('SELECT checkpoint_lsn FROM pg_control_checkpoint()')
            page = scenarios.read_block(sockdir / 'data' / cluster.scalar(
                "SELECT pg_relation_filepath('t')"), 0)
            cluster.stop('immediate')
        finally:
            cluster.ensure_stopped()
            shutil.rmtree(sockdir, ignore_errors=True)
        self.assertTrue(flush['clean'], flush)
        self.assertGreater(flush['buffers_evicted'], 0)
        self.assertEqual(after, checkpoint)  # no checkpoint ran
        # The updated page reached disk with an LSN past the last checkpoint.
        self.assertGreater(lsnscan.page_lsn(page), lsnscan.parse_lsn(checkpoint))
        self.assertLessEqual(lsnscan.page_lsn(page), lsnscan.parse_lsn(written))

    def test_postgresql_17_and_18(self):
        found = [b for b in (pgcluster.DEFAULT_PG17, pgcluster.DEFAULT_PG18)
                 if (b / 'initdb').is_file()]
        if not found:
            self.skipTest('no PostgreSQL 17 or 18 binaries')
        for bindir in found:
            with self.subTest(bindir=str(bindir)):
                self.flush(bindir)


def executable(path, text):
    Path(path).write_text(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


class ProbeTest(unittest.TestCase):
    def test_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            pgbin = SimpleNamespace(major=17, bindir=Path(tmp))
            disabled = tools.probe('pg_probackup', pgbin=pgbin, pg_probackup='none')
            self.assertFalse(disabled['available'])
            self.assertIn('disabled', disabled['reason'])
            missing = tools.probe('walg', pgbin=pgbin, wal_g=str(Path(tmp, 'nope')))
            self.assertIn('not an executable file', missing['reason'])
            self.assertIn('not found', tools.probe('pg_rewind', pgbin=pgbin)['reason'])
            self.assertTrue(tools.probe('lsn_skip', pgbin=pgbin)['available'])
            old = executable(Path(tmp, 'pg_probackup'),
                             '#!/bin/sh\necho "pg_probackup 2.5.16 (PostgreSQL 16.4)"\n')
            wrong = tools.probe('pg_probackup', pgbin=pgbin, pg_probackup=str(old))
            self.assertFalse(wrong['available'])
            self.assertIn('not built for PostgreSQL 17', wrong['reason'])
            walg = executable(Path(tmp, 'wal-g'), '#!/bin/sh\ncase "$1" in\n'
                              '--version) echo "wal-g version v3.0.9";;\n'
                              '--help) echo "  catchup-push  Creates incremental backup";;\n'
                              'catchup-push) echo "      --from-lsn uint   LSN";;\nesac\n')
            push = tools.probe('walg', pgbin=pgbin, wal_g=str(walg))
            send = tools.probe('walg_send', pgbin=pgbin, wal_g=str(walg))
        self.assertTrue(push['available'])
        self.assertEqual(push['version'], 'wal-g version v3.0.9')
        self.assertIsNone(push['sysid_flag'])
        self.assertEqual(push['from_lsn_flag'], '--from-lsn uint   LSN')
        self.assertFalse(send['available'])
        self.assertIn('no catchup-send', send['reason'])

    def test_skipped_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = tools.Job(scenario_id='S2', scenario_dir=tmp, meta={'rows': 5}, expected={},
                            listed_pages=None, tool='walg',
                            tool_info={'available': False, 'reason': 'WAL-G: disabled'},
                            run_dir=Path(tmp, 'run'), pgbin=None)
            result = tools.run_job(job)
            self.assertFalse(Path(tmp, 'run').exists())
        self.assertEqual((result['outcome'], result['verdict']), ('skipped', 'skipped'))
        self.assertEqual(result['outcome_reason'], 'WAL-G: disabled')

    def test_walg_env(self):
        job = SimpleNamespace(source=SimpleNamespace(sockdir=Path('/tmp/pgdsrc'), port=5555))
        with mock.patch.dict(os.environ, {'WALG_S3_PREFIX': 's3://elsewhere', 'PGHOST': 'x',
                                          'WALE_GPG_KEY_ID': 'k', 'HOME': '/home/me'}):
            env = tools.walg_env(job, Path('/data/t11/storage'))
        self.assertEqual(env['WALG_FILE_PREFIX'], '/data/t11/storage')
        self.assertEqual((env['PGHOST'], env['PGPORT'], env['PGUSER']),
                         ('/tmp/pgdsrc', '5555', 'postgres'))
        self.assertNotIn('WALG_S3_PREFIX', env)
        self.assertNotIn('WALE_GPG_KEY_ID', env)
        self.assertEqual(env['HOME'], '/home/me')

    def test_ensure_standby_config(self):
        source = SimpleNamespace(sockdir=Path('/tmp/pgdsrc'), port=5555)
        with tempfile.TemporaryDirectory() as tmp:
            written = Path(tmp, 'a')
            written.mkdir()
            Path(written, 'standby.signal').touch()
            Path(written, 'postgresql.auto.conf').write_text(
                "primary_conninfo = 'user=postgres host=/tmp/pgdsrc port=5555'\n")
            self.assertEqual(tools.ensure_standby_config(written, source), 'tool')
            stale = Path(tmp, 'b')
            stale.mkdir()
            Path(stale, 'postgresql.auto.conf').write_text(
                "primary_conninfo = 'host=/tmp/gone port=1'\n")
            self.assertEqual(tools.ensure_standby_config(stale, source), 'harness')
            self.assertTrue(Path(stale, 'standby.signal').is_file())
            conninfo = tools.last_setting(Path(stale, 'postgresql.auto.conf').read_text(),
                                          'primary_conninfo')
        self.assertEqual(conninfo, 'host=/tmp/pgdsrc port=5555 user=postgres dbname=postgres')


def walg_job(tmp, fake, redo='0/415F070'):
    """A Job for the WAL-G adapters, with a fake source and copy of B (no PostgreSQL)."""
    job = tools.Job(scenario_id='S2b', scenario_dir=tmp, expected={}, listed_pages=None,
                    meta={'old_copy': {'redo_location': redo, 'system_identifier': '7'},
                          'source_backup': {'system_identifier': '7'}},
                    tool='walg', tool_info={'available': True, 'path': str(fake),
                                            'sysid_flag': None},
                    run_dir=Path(tmp, 'run'), pgbin=None, verbose=False)
    job.logs.mkdir(parents=True)
    job.work.mkdir(parents=True)
    job.source = SimpleNamespace(datadir=Path(tmp, 'source'), sockdir=Path('/tmp/pgdsrc'),
                                 port=5555)
    job.target = Path(tmp, 'target')
    job.target.mkdir()
    return job


class WalgAdapterTest(unittest.TestCase):
    def test_push_command(self):
        cmd = tools.walg_push_command('wal-g', '/data/s', '0/415F070')
        self.assertEqual(cmd, ['wal-g', 'catchup-push', '/data/s', '--from-lsn', '68546672'])
        self.assertEqual(int(cmd[-1]), 0x415F070)
        self.assertEqual(tools.walg_push_command('wal-g', '/s', '1/0', '--sysid', '7')[-3:],
                         [str(1 << 32), '--sysid', '7'])

    def test_catchup_push_takes_the_lsn_as_a_number(self):
        """v3.0.9 rejects --from-lsn 0/415F070 before doing anything; the adapter must
        pass a number, or every WAL-G run looks like a refusal."""
        with tempfile.TemporaryDirectory() as tmp:
            fake = executable(Path(tmp, 'wal-g'), FAKE_WALG)
            job = walg_job(tmp, fake)
            run = tools.run_walg(job)
            fetched = sorted(p.name for p in job.target.iterdir())
        self.assertEqual(run.returncode, 0, run.output)
        self.assertEqual(run.commands[0]['command'][-2:], ['--from-lsn', '68546672'])
        self.assertEqual((run.parsed['from_lsn'], run.parsed['from_lsn_argument']),
                         ('0/415F070', '68546672'))
        self.assertEqual(run.parsed['backup_name'],
                         'base_000000020000000000000007_D_000000010000000000000004')
        self.assertEqual([c['name'] for c in run.commands], ['catchup-push', 'catchup-fetch'])
        self.assertEqual(fetched,
                         ['fetched-base_000000020000000000000007_D_000000010000000000000004'])


def result_doc(scenario, tool, **fields):
    return {'scenario': scenario, 'tool': tool, 'outcome': 'accepted', 'verdict': 'repaired',
            'logical_match': True, 'amcheck_ok': True, 'seconds': 1.5,
            'diverged_pages': None, **fields}


class SummaryTest(unittest.TestCase):
    def test_collect_and_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            documents = [
                result_doc('S2b', 'walg', outcome='accepted', verdict='wrong_data',
                           logical_match=False, diverged_pages={'diverged_left': 7}),
                result_doc('S2', 'pg_probackup', outcome='refused', verdict='refused',
                           logical_match=None, amcheck_ok=None, seconds=0.2),
                result_doc('S1', 'pgdelta'),
                result_doc('S1', 'pg_rewind', bring_up={'caught_up': True, 'error': None}),
            ]
            for document in documents:
                tools.write_result(out / document['scenario'] / document['tool'], document)
            (out / 'scenarios' / 'S1-x').mkdir(parents=True)
            Path(out, 'S9', 'broken').mkdir(parents=True)
            Path(out, 'S9', 'broken', 'result.json').write_text('{')
            results = checks.collect_results(out, [s.id for s in scenarios.SCENARIOS],
                                             tools.TOOLS)
            self.assertEqual([(r['scenario'], r['tool']) for r in results],
                             [('S1', 'pg_rewind'), ('S1', 'pgdelta'), ('S2', 'pg_probackup'),
                              ('S2b', 'walg')])
            json_path, csv_path = checks.write_summary(out, results, {'rows': 5})
            summary = json.loads(json_path.read_text())
            with csv_path.open() as handle:
                rows = list(csv.reader(handle))
        self.assertEqual(rows[0], list(checks.CSV_COLUMNS))
        self.assertEqual(rows[0], ['scenario', 'tool', 'outcome', 'logical_match', 'amcheck_ok',
                                   'diverged_pages_left', 'seconds'])
        self.assertEqual(rows[3], ['S2', 'pg_probackup', 'refused', '', '', '', '0.2'])
        self.assertEqual(rows[4], ['S2b', 'walg', 'accepted', 'False', 'True', '7', '1.5'])
        self.assertEqual(summary['rows'], 5)
        self.assertEqual(summary['runs'][3]['verdict'], 'wrong_data')
        self.assertTrue(summary['runs'][0]['caught_up'])


class DriverTest(unittest.TestCase):
    def test_parse_list(self):
        self.assertEqual(run_t11.parse_list('s1, S2b ,', list(scenarios.BY_ID), 'scenario'),
                         ['S1', 'S2b'])
        self.assertEqual(run_t11.parse_list('PG_REWIND,walg', list(tools.TOOLS), 'tool'),
                         ['pg_rewind', 'walg'])
        with self.assertRaises(SystemExit):
            run_t11.parse_list('S1,S42', list(scenarios.BY_ID), 'scenario')
        with self.assertRaises(SystemExit):
            run_t11.parse_list(' , ', list(tools.TOOLS), 'tool')
        self.assertEqual(run_t11.server_settings('none'), {'source': {}, 'old_copy': {}})
        self.assertEqual(run_t11.server_settings('4GB', 'none'),
                         {'source': {'wal_keep_size': '4GB'}, 'old_copy': {}})
        self.assertEqual(run_t11.server_settings('4GB', '1GB'),
                         {'source': {'wal_keep_size': '4GB'}, 'old_copy': {'wal_keep_size': '1GB'}})

    def test_listed_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            stored = {'observations': {'post_fork_pages': [{'file': 'f', 'block': 1}],
                                       'fork_lsn': '0/800'}}
            pages, info = run_t11.listed_pages(tmp, stored)
            self.assertEqual((pages, info['source']), ([{'file': 'f', 'block': 1}],
                                                       'scenario.json'))
            same = {'observations': {}, 'timelines': {'old_copy': 1, 'source_backup': 1}}
            self.assertEqual(run_t11.listed_pages(tmp, same), (None, None))
            write(tmp, 'old_copy/base/5/1', page(0x100) + page(0x900))
            write(tmp, 'source_backup/pg_wal/00000002.history', b'1\t0/800\tx\n')
            forked = {'observations': {}, 'timelines': {'old_copy': 1, 'source_backup': 2}}
            pages, info = run_t11.listed_pages(tmp, forked)
        self.assertEqual([(p['file'], p['block']) for p in pages], [('base/5/1', 1)])
        self.assertEqual(info['fork_lsn'], '0/800')
        self.assertTrue(info['source'].startswith('computed by run_t11'))

    def test_scenario_ready(self):
        conf = run_t11.server_settings('4GB')
        with tempfile.TemporaryDirectory() as tmp:
            meta = {'built_ok': True, 'rows': 50,
                    'server_conf': {**conf, 'source_ran_with': 'source',
                                    'old_copy_ran_with': 'old_copy'},
                    'source_cluster': {'kept': True, 'datadir': 'pgdwork-x/standby'}}
            Path(tmp, 'scenario.json').write_text(json.dumps(meta))
            self.assertIsNone(run_t11.scenario_ready(tmp, 50, conf))
            write(tmp, 'pgdwork-x/standby/PG_VERSION', b'17\n')
            self.assertEqual(run_t11.scenario_ready(tmp, 50, conf), meta)
            self.assertIsNone(run_t11.scenario_ready(tmp, 51, conf))
            self.assertIsNone(run_t11.scenario_ready(tmp, 50, run_t11.server_settings('none')))
            self.assertIsNone(run_t11.scenario_ready(tmp, 50, run_t11.server_settings('4GB', '4GB')))
            # Built before the roles were told apart: every cluster had the settings.
            del meta['server_conf']
            Path(tmp, 'scenario.json').write_text(json.dumps({**meta, 'extra_conf': {}}))
            self.assertIsNone(run_t11.scenario_ready(tmp, 50, run_t11.server_settings('none')))


@unittest.skipUnless((PG_BIN / 'initdb').is_file() and (PG_BIN / 'pg_rewind').is_file()
                     and (PG_BIN / 'pg_amcheck').is_file(), f'{PG_BIN} has no PostgreSQL')
class PgRewindClusterTest(unittest.TestCase):
    """run_t11 end to end on tiny scenarios: pg_rewind must repair S1 and S2; on S2b it
    must repair too, while the LSN-skip control must be called wrong."""

    def run_t11(self, tmp, scenario_ids, tool_names, *extra):
        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet):
            status = run_t11.main(['--pg-bin', str(PG_BIN), '--scenarios', scenario_ids,
                                   '--tools', tool_names, '--rows', '2000', '--out', str(tmp),
                                   '--wal-keep-size', '1GB', '--quiet', *extra])
        self.assertEqual(status, 0, quiet.getvalue()[-3000:])
        return {(r['scenario'], r['tool']): r
                for r in json.loads(Path(tmp, 'summary.json').read_text())['runs']}

    def result(self, tmp, scenario, tool):
        return json.loads(Path(tmp, scenario, tool, 'result.json').read_text())

    def wal_keep_mb(self, tmp, scenario):
        """wal_keep_size (MB) as B and the kept source cluster would run with it."""
        directory = Path(tmp, 'scenarios', scenarios.BY_ID[scenario].dirname)
        meta = json.loads((directory / 'scenario.json').read_text())
        values = {}
        for name, datadir in (('old_copy', directory / 'old_copy'),
                              ('source', directory / meta['source_cluster']['datadir'])):
            values[name] = pgcluster.run([PG_BIN / 'postgres', '-D', datadir, '-C',
                                          'wal_keep_size']).stdout.strip()
        return values, meta['server_conf']

    def test_pg_rewind_repairs_s1_and_s2(self):
        tmp = Path(tempfile.mkdtemp(prefix='pgd'))
        try:
            runs = self.run_t11(tmp, 'S1,S2', 'pg_rewind')
            for scenario in ('S1', 'S2'):
                run = runs[(scenario, 'pg_rewind')]
                self.assertEqual((run['outcome'], run['verdict']), ('accepted', 'repaired'),
                                 self.result(tmp, scenario, 'pg_rewind'))
                self.assertTrue(run['logical_match'] and run['amcheck_ok'] and run['caught_up'])
            s1, s2 = self.result(tmp, 'S1', 'pg_rewind'), self.result(tmp, 'S2', 'pg_rewind')
            self.assertTrue(s1['parsed']['no_rewind_required'])
            self.assertIsNone(s1['diverged_pages'])
            self.assertEqual(s2['parsed']['diverged_on_timeline'], 1)
            self.assertEqual(s2['diverged_pages']['diverged_left'], 0)
            self.assertGreater(s2['diverged_pages']['listed'], 0)
            self.assertEqual(s2['bring_up']['recovery_conf'], 'tool')
            self.assertFalse(Path(tmp, 'S2', 'pg_rewind', 'work').exists())
            # Only the source keeps its WAL; B runs with EXP-03's settings, whether it was
            # the standby (S1) or the old primary (S2).
            for scenario in ('S1', 'S2'):
                values, conf = self.wal_keep_mb(tmp, scenario)
                self.assertEqual(values, {'old_copy': '0', 'source': '1024'}, scenario)
                self.assertEqual((conf['old_copy_ran_with'], conf['source_ran_with']),
                                 ('old_copy', 'source'))
                self.assertEqual((conf['source'], conf['old_copy']),
                                 ({'wal_keep_size': '1GB'}, {}))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_s2b(self):
        tmp = Path(tempfile.mkdtemp(prefix='pgd'))
        try:
            runs = self.run_t11(tmp, 'S2b', 'pg_rewind,lsn_skip',
                                '--old-copy-wal-keep-size', '512MB')
            meta = json.loads(Path(tmp, 'scenarios', scenarios.BY_ID['S2b'].dirname,
                                   'scenario.json').read_text())
            self.assertTrue(meta['built_ok'], meta.get('failed_checks') or meta.get('error'))
            observations = meta['observations']
            self.assertLess(int(observations['old_copy_redo_lsn'].replace('/', ''), 16),
                            int(observations['fork_lsn'].replace('/', ''), 16))
            self.assertEqual(meta['old_copy']['cluster_state'], 'in production')
            self.assertGreaterEqual(observations['post_fork_page_count'],
                                    meta['checks']['diverged_pages_differ'][
                                        'pages_written_after_split'])
            self.assertEqual(self.wal_keep_mb(tmp, 'S2b')[0], {'old_copy': '512', 'source': '1024'})
            self.assertEqual((meta['server_conf']['source'], meta['server_conf']['old_copy']),
                             ({'wal_keep_size': '1GB'}, {'wal_keep_size': '512MB'}))
            self.assertTrue(meta['checks']['old_copy_conf_applied']['ok'])
            rewind = self.result(tmp, 'S2b', 'pg_rewind')
            self.assertTrue(rewind['parsed']['crash_recovery_run'])
            self.assertEqual(runs[('S2b', 'pg_rewind')]['verdict'], 'repaired', rewind)
            self.assertEqual(rewind['diverged_pages']['diverged_left'], 0)
            control = runs[('S2b', 'lsn_skip')]
            self.assertEqual(control['verdict'], 'wrong_data')
            self.assertGreater(control['diverged_pages_left'], 0)
            self.assertFalse(control['logical_match'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
