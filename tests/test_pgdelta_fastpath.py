"""The WAL-summary fast path (pgdelta.fastpath, --fast-path summaries): its guard and page choice, and refreshes of
a real PostgreSQL 17 primary's old copy, one cleanly stopped (the fast path runs) and one crashed (it is refused)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pgdelta import fastpath
from pgdelta.errors import InvalidInput, ProtocolError
from pgdelta.hashing import Hasher
from pgdelta_cluster import CONTROLDATA, PG_BIN, SKIP_REASON, available
from pgdelta_support import REPO_ROOT, python_command, subprocess_env

L = fastpath.parse_lsn
FACTS = {'timeline': 1, 'redo': '0/2000000', 'end': '0/4000000', 'proof': 'clean shutdown', 'major': 17}


def source(**changes):
    base = {'server_version': 170011, 'summarize_wal': True, 'timeline': 1, 'start_lsn': L('0/8000000'),
            'history': {}, 'summaries': [(1, L('0/1000000'), L('0/5000000')), (1, L('0/5000000'), L('0/8000000'))]}
    return {**base, **changes}


class Guard(unittest.TestCase):
    def test_a_cleanly_stopped_ancestor_on_the_same_timeline_passes(self):
        self.assertEqual(fastpath.guard(FACTS, source()), (True, []))

    def test_a_server_before_the_visibility_map_fix_or_without_summaries_is_refused(self):
        for changes, words in (({'server_version': 170010}, '17.11'), ({'summarize_wal': False}, 'summarize_wal')):
            ok, reasons = fastpath.guard(FACTS, source(**changes))
            self.assertFalse(ok)
            self.assertTrue(any(words in r for r in reasons), reasons)

    def test_an_old_primary_past_the_split_is_refused_even_with_its_redo_before_it(self):
        history = {'00000002.history': '1\t0/3000000\tno recovery target specified\n'}
        ok, reasons = fastpath.guard(FACTS, source(timeline=2, history=history,
                                                   summaries=[(1, L('0/1000000'), L('0/3000000')),
                                                              (2, L('0/3000000'), L('0/8000000'))]))
        self.assertFalse(ok)
        self.assertIn('past where', reasons[0])
        # The same copy, stopped before the split, is an ancestor of the new timeline: it passes.
        self.assertTrue(fastpath.guard({**FACTS, 'end': '0/2800000'},
                                       source(timeline=2, history=history,
                                              summaries=[(1, L('0/1000000'), L('0/3000000')),
                                                         (2, L('0/3000000'), L('0/8000000'))]))[0])

    def test_a_gap_in_the_summaries_or_another_major_is_refused(self):
        ok, reasons = fastpath.guard(FACTS, source(summaries=[(1, L('0/3000000'), L('0/8000000'))]))
        self.assertFalse(ok)
        self.assertIn('do not cover', reasons[0])
        self.assertFalse(fastpath.guard({**FACTS, 'major': 18}, source())[0])

    def test_malformed_facts_are_a_protocol_error(self):
        with self.assertRaises(ProtocolError):
            fastpath.guard({'timeline': 1}, source())


class Pages(unittest.TestCase):
    PLAN = fastpath.block_plan([(1663, 5, 16384, 0, 3, False), (1663, 5, 16384, 0, 131075, False),
                                (1663, 5, 16384, 2, 0, False), (1663, 5, 16390, 0, 4, True),
                                (1664, 0, 1262, 0, 0, False), (1700, 5, 1, 0, 0, False)])

    def pages(self, path, size, basis_size, **kw):
        return fastpath.pages_to_send(path, size, basis_size, self.PLAN, 8192, 131072, **kw)

    def test_listed_blocks_go_by_segment(self):
        self.assertEqual(self.pages('base/5/16384', 10 * 8192, 10 * 8192), ([3], False))
        self.assertEqual(self.pages('base/5/16384.1', 10 * 8192, 10 * 8192), ([3], False))
        self.assertEqual(self.pages('base/5/16384_vm', 8192, 8192), ([0], False))
        self.assertEqual(self.pages('global/1262', 8192, 8192), ([0], False))

    def test_a_limit_block_and_pages_past_b_go_too(self):
        self.assertEqual(self.pages('base/5/16390', 8 * 8192, 8 * 8192), ([4, 5, 6, 7], False))
        self.assertEqual(self.pages('base/5/16384', 12 * 8192, 10 * 8192), ([3, 10, 11], False))

    def test_other_forks_files_b_lacks_and_unlogged_relations_go_whole(self):
        for path, basis, kw in (('base/5/16384_fsm', 8192, {}), ('base/5/16384_init', 8192, {}),
                                ('pg_xact/0000', 8192, {}), ('base/5/16384', None, {}),
                                ('base/5/16384', 8192, {'whole': True})):
            self.assertEqual(self.pages(path, 8192, basis, **kw), ([0], True), path)

    def test_the_written_digest_depends_on_every_page_and_its_place(self):
        hasher = Hasher(b'k' * 32)
        a = fastpath.written_digest(hasher, 16384, [(0, hasher.leaf(b'a' * 8192))])
        self.assertNotEqual(a, fastpath.written_digest(hasher, 16384, [(1, hasher.leaf(b'a' * 8192))]))
        self.assertNotEqual(a, fastpath.written_digest(hasher, 16384, [(0, hasher.leaf(b'b' * 8192))]))
        self.assertNotEqual(a, fastpath.written_digest(hasher, 24576, [(0, hasher.leaf(b'a' * 8192))]))

    def test_decisions_and_modes_are_checked(self):
        self.assertEqual(fastpath.check_decision({'use': False, 'reasons': ['x']}), (False, ['x']))
        for bad in ({'use': 1}, {'use': True, 'extra': 1}, {'use': True, 'reasons': [1]}):
            with self.assertRaises(ProtocolError):
                fastpath.check_decision(bad)
        fastpath.check_mode(None, 'datadir', False)
        for mode, kind, sub in (('summaries', 'datadir', False), ('summaries', 'live', True), ('other', 'live', False)):
            with self.assertRaises(InvalidInput):
                fastpath.check_mode(mode, kind, sub)


@unittest.skipUnless(available() and (PG_BIN / 'pgbench').exists() and (PG_BIN / 'pg_waldump').exists(), SKIP_REASON)
class RealServer(unittest.TestCase):
    """A primary that summarizes WAL; B, a copy of it stopped cleanly before scattered updates, a truncation and a
    new table; and C, a copy of it crashed at the same point."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.sockets = Path(tempfile.mkdtemp(prefix='pgf'))
        cls.source = cls.root / 'S'
        cls.env = dict(os.environ, LC_ALL='C', LANG='C')
        cls.cmd([PG_BIN / 'initdb', '-D', cls.source, '-U', 't', '-A', 'trust', '--no-sync', '--data-checksums'])
        with open(cls.source / 'postgresql.conf', 'a') as conf:
            conf.write('summarize_wal = on\n')
        cls.ctl(cls.source, 'start')
        cls.cmd([PG_BIN / 'pgbench', '-i', '-q', '-s', 4, '-h', cls.sockets, '-p', 5432, '-U', 't', 'postgres'])
        cls.sql('CREATE TABLE gone AS SELECT g, repeat(md5(g::text), 4) AS v FROM generate_series(1, 20000) g')
        cls.ctl(cls.source, 'stop')
        shutil.copytree(cls.source, cls.root / 'stale')
        cls.ctl(cls.source, 'start')
        cls.ctl(cls.source, 'stop', mode='immediate')
        shutil.copytree(cls.source, cls.root / 'crashed')
        cls.ctl(cls.source, 'start')
        cls.sql('UPDATE pgbench_accounts SET abalance = abalance + aid WHERE aid IN (SELECT g * 997 FROM generate_series(1, 400) g)')
        cls.sql('DELETE FROM gone WHERE g > 1000')
        cls.sql('VACUUM gone')  # truncates the table: a limit block in the summaries
        cls.sql('CREATE TABLE fresh AS SELECT g FROM generate_series(1, 5000) g')
        cls.sql('CHECKPOINT')

    @classmethod
    def tearDownClass(cls):
        cls.ctl(cls.source, 'stop', check=False, mode='immediate')
        shutil.rmtree(cls.sockets, True)
        cls.temp.cleanup()

    @classmethod
    def cmd(cls, command, check=True):
        result = subprocess.run([str(part) for part in command], capture_output=True, text=True, env=cls.env,
                                timeout=300)
        if check and result.returncode:
            raise AssertionError(f'{command[0]} failed: {result.stderr[-2000:]}')
        return result

    @classmethod
    def ctl(cls, data, action, port=5432, check=True, mode='fast'):
        return cls.cmd([PG_BIN / 'pg_ctl', '-D', data, '-l', f'{data}.log', '-w', '-t', '60', '-m', mode, '-o',
                        f"-p {port} -k {cls.sockets} -c listen_addresses='' -c fsync=off", action], check=check)

    @classmethod
    def sql(cls, statement, port=5432):
        return cls.cmd([PG_BIN / 'psql', '-h', cls.sockets, '-p', port, '-U', 't', '-d', 'postgres', '-qAtX',
                        '-v', 'ON_ERROR_STOP=1', '-c', statement]).stdout.strip()

    STATE = ("SELECT md5(string_agg(aid || ':' || abalance, ',' ORDER BY aid)) || '/' || "
             "(SELECT count(*) FROM gone) || '/' || (SELECT sum(g) FROM fresh) FROM pgbench_accounts")

    def refresh(self, name, basis):
        target = self.root / name
        shutil.copytree(self.root / basis, target)
        stats_path = self.root / f'{name}.json'
        result = subprocess.run(python_command(
            'local', '--source', self.source, '--source-kind', 'live', '--pg-controldata', CONTROLDATA,
            '--pg-conn', f'host={self.sockets} port=5432 user=t dbname=postgres', '--basis', target, '--in-place',
            '--fast-path', 'summaries', '--stats-json', stats_path), capture_output=True, text=True, cwd=REPO_ROOT,
            env=subprocess_env(), timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        return target, json.loads(stats_path.read_text())

    def check_copy(self, target):
        """The refreshed copy recovers and holds the source's rows, and amcheck finds nothing wrong."""
        self.ctl(target, 'start', port=5433)
        try:
            self.assertEqual(self.sql(self.STATE, port=5433), self.sql(self.STATE))
            amcheck = self.cmd([PG_BIN / 'pg_amcheck', '-h', self.sockets, '-p', 5433, '-U', 't', '--all',
                                '--heapallindexed', '--install-missing'], check=False)
            self.assertEqual(amcheck.returncode, 0, amcheck.stdout + amcheck.stderr)
        finally:
            self.ctl(target, 'stop', port=5433, check=False)

    def test_a_cleanly_stopped_copy_takes_the_fast_path_and_recovers_to_the_source(self):
        target, stats = self.refresh('fast', 'stale')
        fast, received = stats['fast_path'], stats['receiver']['fast_path']
        self.assertTrue(fast['used'], fast['reasons'])
        self.assertTrue(received['used'])
        self.assertEqual(received['facts']['proof'], 'clean shutdown')
        self.assertGreater(fast['files_chosen'], 0)
        self.assertEqual(received['files_written_checked'], fast['files_chosen'])
        self.assertLess(stats['pages']['sent'], stats['pages']['total'] / 2)
        self.assertNotIn('BASIS_NODES', stats['receiver']['bytes_written']['by_message_type'])  # B never hashed
        self.check_copy(target)

    def test_a_crashed_copy_is_refused_and_refreshed_by_hashing(self):
        target, stats = self.refresh('refused', 'crashed')
        fast, received = stats['fast_path'], stats['receiver']['fast_path']
        self.assertFalse(fast['used'])
        self.assertIn('not stopped cleanly', received['refused'])
        self.assertIn('not stopped cleanly', fast['reasons'][0])
        self.assertGreater(stats['receiver']['bytes_written']['by_message_type']['BASIS_NODES']['frames'], 0)
        self.check_copy(target)


if __name__ == '__main__':
    unittest.main()
