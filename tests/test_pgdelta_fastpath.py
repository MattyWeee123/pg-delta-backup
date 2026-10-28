"""The WAL-summary fast path (pgdelta.fastpath, --fast-path summaries): its guard and page choice, and refreshes of
a real PostgreSQL 17 primary's old copy, one cleanly stopped (the fast path runs) and one crashed (it is refused);
and an old primary after a failover, with and without --diverged-basis (idea 1)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from pgdelta import fastpath, sender
from pgdelta.errors import InvalidInput, ProtocolError
from pgdelta.hashing import Hasher
from pgdelta_cluster import CONTROLDATA, PG_BIN, SKIP_REASON, available
from pgdelta_support import REPO_ROOT, python_command, subprocess_env

L = fastpath.parse_lsn
IDENTITY = {'system_identifier': '7', 'catalog_version': 202406281, 'data_checksum_version': 1}
FACTS = {'timeline': 1, 'redo': '0/2000000', 'end': '0/4000000', 'proof': 'clean shutdown', 'major': 17, **IDENTITY}


def source(**changes):
    base = {'identity': IDENTITY, 'server_version': 170011, 'summarize_wal': True, 'timeline': 1,
            'start_lsn': L('0/8000000'),
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

    def test_a_timeline_switch_on_a_segment_boundary_leaves_only_a_page_header_which_is_no_gap(self):
        """PostgreSQL 17.11's summaries at such a switch (EXP-69): the old timeline's ends on the boundary, the new
        timeline's first starts after the segment's 40-byte long header. No record lies in a page header."""
        history = {'00000002.history': '1\t0/3000000\tno recovery target specified\n'}
        stopped_before = {**FACTS, 'end': '0/2800000'}
        for start, ok in (('0/3000028', True), ('0/3000018', True), ('0/3000030', False), ('0/3000050', False)):
            result = fastpath.guard(stopped_before, source(timeline=2, history=history,
                                                           summaries=[(1, L('0/1000000'), L('0/3000000')),
                                                                      (2, L(start), L('0/8000000'))]))
            self.assertEqual(result[0], ok, (start, result))
        # Only at a page boundary: a 40-byte hole anywhere else is a gap.
        self.assertFalse(fastpath.covered([(1, L('0/1000000'), L('0/3000100')), (1, L('0/3000128'), L('0/8000000'))],
                                          1, L('0/2000000'), L('0/8000000')))
        self.assertTrue(fastpath.covered([(1, L('0/1000000'), L('0/3002000')), (1, L('0/3002018'), L('0/8000000'))],
                                         1, L('0/2000000'), L('0/8000000')))

    def test_a_gap_in_the_summaries_or_another_major_is_refused(self):
        ok, reasons = fastpath.guard(FACTS, source(summaries=[(1, L('0/3000000'), L('0/8000000'))]))
        self.assertFalse(ok)
        self.assertIn('do not cover', reasons[0])
        self.assertFalse(fastpath.guard({**FACTS, 'major': 18}, source())[0])

    def test_another_cluster_is_refused_whatever_the_reuse_policy(self):
        # --reuse-any-basis lets another cluster through the identity gate; its LSNs mean nothing here.
        for key, value in (('system_identifier', '8'), ('catalog_version', 1), ('data_checksum_version', 0)):
            ok, reasons = fastpath.guard({**FACTS, key: value}, source())
            self.assertFalse(ok, key)
            self.assertIn(key.replace('_', ' '), reasons[0])

    def test_malformed_facts_are_a_protocol_error(self):
        with self.assertRaises(ProtocolError):
            fastpath.guard({'timeline': 1}, source())


class Diverged(unittest.TestCase):
    """--diverged-basis (idea 1): an old primary that kept writing on timeline 1 after the server left it at 0/3000000."""
    HISTORY = {'00000002.history': '1\t0/3000000\tno recovery target specified\n'}
    SUMMARIES = [(1, L('0/1000000'), L('0/3000000')), (2, L('0/3000028'), L('0/8000000'))]
    COMMON = {'checkpoint': '0/2400000', 'redo': '0/2300000', 'records': 9, 'seconds': 0.1, 'wal_log_hints': False,
              'blocks': {'base/5/16384': [[3, 2]], 'base/5/16384_vm': [[0, 1]]}}

    def server(self, **changes):
        return source(timeline=2, history=self.HISTORY, summaries=self.SUMMARIES, **changes)

    def test_the_fork_is_found_only_for_a_copy_past_it(self):
        self.assertEqual(fastpath.diverged_fork(FACTS, self.server()), L('0/3000000'))
        self.assertIsNone(fastpath.diverged_fork({**FACTS, 'end': '0/2800000'}, self.server()))  # an ancestor
        self.assertIsNone(fastpath.diverged_fork(FACTS, source()))  # the server is still on B's timeline
        self.assertIsNone(fastpath.diverged_fork({**FACTS, 'timeline': 3}, self.server()))  # not on its history
        self.assertIsNone(fastpath.diverged_fork({**FACTS, 'system_identifier': '8'}, self.server()))  # refused anyway
        self.assertIsNone(fastpath.diverged_fork(FACTS, self.server(summarize_wal=False)))

    def test_the_checkpoint_before_the_fork_lets_it_run_from_its_redo_point(self):
        self.assertEqual(fastpath.guard(FACTS, self.server(), common=self.COMMON), (True, []))
        # Summaries from 0/1000000 only: from 0/2300000 they still cover; from a redo point before them they don't.
        ok, reasons = fastpath.guard(FACTS, self.server(), common={**self.COMMON, 'redo': '0/0800000'})
        self.assertFalse(ok)
        self.assertIn('do not cover', reasons[0])

    def test_a_checkpoint_past_the_fork_or_no_hint_protection_is_refused(self):
        ok, reasons = fastpath.guard(FACTS, self.server(), common={**self.COMMON, 'checkpoint': '0/3100000'})
        self.assertFalse(ok)
        self.assertIn('not before the fork', reasons[0])
        unprotected = {**FACTS, 'data_checksum_version': 0}
        server = self.server(identity={**IDENTITY, 'data_checksum_version': 0})
        ok, reasons = fastpath.guard(unprotected, server, common=self.COMMON)
        self.assertFalse(ok)
        self.assertIn('wal_log_hints', reasons[0])
        self.assertTrue(fastpath.guard(unprotected, server, common={**self.COMMON, 'wal_log_hints': True})[0])

    def test_b_s_blocks_join_the_summaries_plan(self):
        plan = fastpath.merge_basis_blocks(fastpath.block_plan([(1663, 5, 16384, 0, 9, False)]),
                                           self.COMMON['blocks'])
        self.assertEqual(plan, {'base/5/16384': ({3, 4, 9}, None), 'base/5/16384_vm': ({0}, None)})
        self.assertEqual(fastpath.block_runs([1, 2, 3, 7, 9, 10]), [[1, 3], [7, 1], [9, 2]])

    def test_the_checkpoint_before_the_fork_is_sought_back_from_it_in_doubling_windows(self):
        """A stand-in pg_waldump: checkpoints at 0/1800000 (redo 0/1700000) and 0/5800000, the fork at 0/6400000."""
        with tempfile.TemporaryDirectory() as temp:
            fake, calls = Path(temp) / 'pg_waldump', Path(temp) / 'calls'
            fake.write_text(f'''#!{sys.executable}
import sys
start = sys.argv[sys.argv.index('--start') + 1]
open({str(calls)!r}, 'a').write(start + '\\n')
high, low = start.split('/')
for at, redo in (('0/1800000', '0/1700000'), ('0/5800000', '0/5700000'), ('0/6800000', '0/6700000')):
    h, l = at.split('/')
    if (int(h, 16) << 32 | int(l, 16)) >= (int(high, 16) << 32 | int(low, 16)):
        print(f'rmgr: XLOG len (rec/tot): 148/148, tx: 0, lsn: {{at}}, prev 0/0, desc: CHECKPOINT_ONLINE redo {{redo}}; tli 1')
''')
            fake.chmod(0o755)
            seg = 16 << 20
            found = fastpath.last_checkpoint_before(fake, temp, 1, 0, L('0/6400000'), seg)
            self.assertEqual(found, ((L('0/5800000'), L('0/5700000')), None))
            self.assertEqual(calls.read_text().split(), ['0/6000000', '0/5000000'])  # 1 segment, then 2
            calls.unlink()
            found = fastpath.last_checkpoint_before(fake, temp, 1, L('0/2000000'), L('0/5000000'), seg)
            self.assertIsNone(found[0])  # none from B's oldest segment on
            self.assertIn('no checkpoint before the fork', found[1])
            self.assertEqual(calls.read_text().split(), ['0/5000000', '0/4000000', '0/2000000'])

    def test_messages_are_checked(self):
        self.assertEqual(fastpath.check_fork_point({'timeline': 1, 'fork': '0/3000000'}), (1, L('0/3000000')))
        self.assertEqual(fastpath.check_basis_wal({'refused': 'why'}), {'refused': 'why'})
        self.assertEqual(fastpath.check_basis_wal(self.COMMON), self.COMMON)
        for bad in ({'timeline': '1', 'fork': '0/1'}, {'timeline': 1, 'fork': 'x'}, {'timeline': 1}):
            with self.assertRaises(ProtocolError):
                fastpath.check_fork_point(bad)
        for blocks in ({'base/5/16384.1': [[0, 1]]}, {'pg_wal/x': [[0, 1]]}, {'base/5/16384': [[0, 0]]},
                       {'base/5/16384': [[-1, 1]]}):
            with self.assertRaises(ProtocolError):
                fastpath.check_basis_wal({**self.COMMON, 'blocks': blocks})
        with self.assertRaises(InvalidInput):
            fastpath.check_mode(None, 'live', False, diverged_basis=True)


class Pages(unittest.TestCase):
    PLAN = fastpath.block_plan([(1663, 5, 16384, 0, 3, False), (1663, 5, 16384, 0, 131075, False),
                                (1663, 5, 16384, 2, 0, False), (1663, 5, 16390, 0, 4, True),
                                (1664, 0, 1262, 0, 0, False), (1700, 5, 1, 0, 0, False)])

    def pages(self, path, size, basis_size, **kw):
        return fastpath.pages_to_send(path, size, basis_size, self.PLAN, 8192, 131072, **kw)

    def test_the_grouped_plan_is_the_plan_of_every_row(self):
        # Rows from two summaries (a block in both, a limit in each, one fork with only a limit) against the same
        # rows grouped per relation fork on the server (live.summary_blocks; EXP-73b).
        rows = [(1663, 5, 16384, 0, 3, False), (1663, 5, 16384, 0, 9, False), (1663, 5, 16384, 0, 3, False),
                (1663, 5, 16390, 0, 7, True), (1663, 5, 16390, 0, 4, True), (1663, 5, 16390, 0, 2, False),
                (1663, 5, 16391, 1, 0, True), (1664, 0, 1262, 0, 0, False), (1700, 5, 1, 0, 0, False)]
        groups = [(1663, 5, 16384, 0, [3, 9], None), (1663, 5, 16390, 0, [2], 4), (1663, 5, 16391, 1, None, 0),
                  (1664, 0, 1262, 0, [0], None), (1700, 5, 1, 0, [0], None)]
        self.assertEqual(fastpath.grouped_plan(groups), fastpath.block_plan(rows))
        self.assertEqual(fastpath.grouped_plan([]), {})

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
        fastpath.check_mode('summaries', 'live', False, 4)
        for mode, kind, sub, ahead in (('summaries', 'datadir', False, 0), ('summaries', 'live', True, 0),
                                       ('other', 'live', False, 0), (None, 'live', False, 4),
                                       ('summaries', 'live', False, -1)):
            with self.assertRaises(InvalidInput):
                fastpath.check_mode(mode, kind, sub, ahead)
        fastpath.check_mode('summaries', 'live', False, 0, 'plan.json')
        with self.assertRaises(InvalidInput):
            fastpath.check_mode(None, 'live', False, 0, 'plan.json')
        fastpath.check_mode('summaries', 'live', False, 8, None, 2)
        for fill in (-1, 1.5):
            with self.assertRaises(InvalidInput):
                fastpath.check_mode('summaries', 'live', False, 8, None, fill)
        with self.assertRaises(InvalidInput):
            fastpath.check_mode(None, 'live', False, 0, None, 2)

    def test_fill_gaps_joins_runs_across_short_clean_gaps_only(self):
        pages = [0, 1, 4, 6, 7, 20, 22]
        self.assertEqual(fastpath.fill_gaps(pages, 0), pages)
        self.assertEqual(fastpath.fill_gaps(pages, 1), [0, 1, 4, 5, 6, 7, 20, 21, 22])
        self.assertEqual(fastpath.fill_gaps(pages, 2), [0, 1, 2, 3, 4, 5, 6, 7, 20, 21, 22])
        self.assertEqual(fastpath.fill_gaps([], 3), [])
        self.assertEqual(fastpath.fill_gaps([5], 3), [5])

    def test_the_cli_fills_two_pages_with_the_fast_path_and_none_without(self):
        from argparse import Namespace

        from pgdelta import cli
        self.assertEqual(cli._fill_gaps(Namespace(fast_path='summaries', fill_gaps=None)), fastpath.DEFAULT_FILL_GAPS)
        self.assertEqual(fastpath.DEFAULT_FILL_GAPS, 2)
        self.assertEqual(cli._fill_gaps(Namespace(fast_path='summaries', fill_gaps=0)), 0)
        self.assertEqual(cli._fill_gaps(Namespace(fast_path=None, fill_gaps=None)), 0)
        self.assertEqual(cli._fill_gaps(Namespace()), 0)

    def test_the_cli_takes_an_old_primary_with_the_fast_path_unless_told_not_to(self):
        from argparse import Namespace

        from pgdelta import cli
        self.assertIs(fastpath.DEFAULT_DIVERGED_BASIS, True)
        self.assertIs(cli._diverged_basis(Namespace(fast_path='summaries', diverged_basis=None)), True)
        self.assertIs(cli._diverged_basis(Namespace(fast_path='summaries', diverged_basis=False)), False)
        self.assertIs(cli._diverged_basis(Namespace(fast_path=None, diverged_basis=None)), False)
        self.assertIs(cli._diverged_basis(Namespace(fast_path=None, diverged_basis=True)), True)  # check_mode refuses
        self.assertIs(cli._diverged_basis(Namespace()), False)


class ReadAhead(unittest.TestCase):
    """EXP-64b's reader threads: runs come back in sending order, at most the window ahead, handles closed."""

    class Reader:
        def __init__(self):
            self.opened, self.closed, self.reads = [], [], []

        def open(self, entry):
            reader = self
            self.opened.append(entry)

            class Handle:
                def read(self, offset, length):
                    reader.reads.append((entry, offset))
                    return bytes([len(entry)]) * length

                def close(self):
                    reader.closed.append(entry)
            return Handle()

    def test_runs_in_order_within_the_window(self):
        reader = self.Reader()
        runs = [(0, 'a', 0, 1, 8192), (0, 'a', 5, 2, 16384), (1, 'bb', 3, 1, 8192)]
        ahead = sender._ReadAhead(reader, runs, threads=3, window=16384, block=8192)
        self.assertLessEqual(len(ahead.pending), 2)  # the window: 16 KiB, then one more run
        self.assertEqual(ahead.next(0, 0), b'\x01' * 8192)
        self.assertEqual(ahead.next(0, 5), b'\x01' * 16384)
        ahead.done(0)
        self.assertEqual(reader.closed, ['a'])
        with self.assertRaises(Exception):
            ahead.next(1, 4)  # out of order
        ahead.close()
        self.assertEqual(sorted(reader.opened), ['a', 'bb'])
        self.assertEqual(sorted(reader.closed), ['a', 'bb'])


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

    def refresh(self, name, basis, *extra):
        target = self.root / name
        shutil.copytree(self.root / basis, target)
        stats_path = self.root / f'{name}.json'
        result = subprocess.run(python_command(
            'local', '--source', self.source, '--source-kind', 'live', '--pg-controldata', CONTROLDATA,
            '--pg-conn', f'host={self.sockets} port=5432 user=t dbname=postgres', '--basis', target, '--in-place',
            '--fast-path', 'summaries', '--stats-json', stats_path, *extra), capture_output=True, text=True, cwd=REPO_ROOT,
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

    def test_one_grouped_query_gives_the_plan_of_every_summary_row(self):
        # The sender's one query grouped per relation fork (EXP-73b) against today's reading, row by row, over every
        # summary here: the scattered updates, the truncation's limit block, the new table.
        from pgdelta import live
        session = live.Session(f'host={self.sockets} port=5432 user=t dbname=postgres', psql=PG_BIN / 'psql')
        try:
            source = live.LiveSource.__new__(live.LiveSource)
            source.session = session
            summaries = [(int(t), fastpath.parse_lsn(a), fastpath.parse_lsn(b)) for t, a, b in (
                line.split('|') for line in self.sql('SELECT tli, start_lsn, end_lsn FROM pg_available_wal_summaries()')
                .splitlines())]
            rows = [row for summary in summaries for row in source.summary_rows(*summary)]
            count, groups = source.summary_blocks(summaries)
        finally:
            session.close()
        self.assertGreater(len(summaries), 1)
        self.assertEqual(count, len(rows))
        plan = fastpath.grouped_plan(groups)
        self.assertEqual(plan, fastpath.block_plan(rows))
        self.assertTrue(any(limit is not None for _, limit in plan.values()))  # the truncation is in there
        self.assertEqual(source.summary_blocks([]), (0, []))

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
        # The defaults with --fast-path: 8 reader threads, the receiver's syncs on its worker thread (EXP-68), and
        # clean gaps of up to 2 pages filled (EXP-70).
        self.assertEqual(fast['read_ahead']['threads'], 8)
        self.assertEqual(fast['fill_gaps']['gap_pages'], 2)
        self.assertEqual(stats['receiver']['fsync_mode'], 'worker')
        self.assertTrue(received['fsync_auto'])
        self.check_copy(target)

    def test_read_ahead_threads_give_the_same_copy(self):
        target, stats = self.refresh('fast-ahead', 'stale', '--read-ahead', '4')
        fast = stats['fast_path']
        self.assertTrue(fast['used'], fast['reasons'])
        self.assertEqual(fast['read_ahead']['threads'], 4)
        self.assertGreater(fast['read_ahead']['reads'], 0)
        self.check_copy(target)

    def test_the_fsync_worker_with_a_written_plan_gives_the_same_copy(self):
        """EXP-68's arm: 8 reader threads, the receiver's syncs on its worker thread, and the plan written."""
        plan_path = self.root / 'fast-worker-plan.json'
        target, stats = self.refresh('fast-worker', 'stale', '--read-ahead', '8', '--fsync', 'worker',
                                     '--fill-gaps', '0', '--plan-out', plan_path)
        fast = stats['fast_path']
        self.assertTrue(fast['used'], fast['reasons'])
        self.assertNotIn('fill_gaps', fast)
        self.assertEqual(stats['receiver']['fsync_mode'], 'worker')
        plan = json.loads(plan_path.read_text())
        chosen = [f for f in plan['files'] if not f['whole']]
        self.assertEqual(len(chosen), fast['files_chosen'])
        self.assertEqual(len(plan['files']) - len(chosen), fast['files_whole'])
        self.assertEqual(sum(n for f in chosen for _, n in f['runs']), fast['pages_chosen'])
        self.assertEqual(sum(len(f['runs']) for f in chosen), fast['read_ahead']['reads'])
        self.check_copy(target)

    def test_filled_gaps_give_the_same_copy_in_fewer_runs(self):
        """EXP-70's arm: clean gaps of up to 2 pages sent too; the plan lists the filled pages, read as fewer runs."""
        plan_path = self.root / 'fast-filled-plan.json'
        target, stats = self.refresh('fast-filled', 'stale', '--fill-gaps', '2', '--plan-out', plan_path)
        fast = stats['fast_path']
        self.assertTrue(fast['used'], fast['reasons'])
        self.assertEqual(fast['fill_gaps']['gap_pages'], 2)
        self.assertGreater(fast['fill_gaps']['pages_filled'], 0)  # the stale copy's changes leave short gaps
        chosen = [f for f in json.loads(plan_path.read_text())['files'] if not f['whole']]
        self.assertEqual(sum(n for f in chosen for _, n in f['runs']), fast['pages_chosen'])
        self.assertEqual(sum(len(f['runs']) for f in chosen), fast['read_ahead']['reads'])
        for f in chosen:  # no two runs of a file are separated by a gap the fill should have closed
            for (first, count), (after, _) in zip(f['runs'], f['runs'][1:]):
                self.assertTrue(after == first + count or after - (first + count) > 2, f)
        self.check_copy(target)

    def test_the_selector_picks_a_fill_and_gives_the_same_copy(self):
        """--selector (EXP-74): small changes on these disks' defaults go by the fast path, at the fill it predicts
        fastest; the fill it chose is the one sent."""
        plan_path = self.root / 'fast-selected-plan.json'
        target, stats = self.refresh('fast-selected', 'stale', '--selector', '--plan-out', plan_path)
        fast = stats['fast_path']
        self.assertTrue(fast['used'], fast['reasons'])
        chosen = fast['selector']
        predicted = chosen['predicted_seconds']
        self.assertEqual(set(predicted), {'fill0', 'fill1', 'fill2', 'fill4', 'full'})
        fills = {k: v for k, v in predicted.items() if k != 'full'}
        self.assertEqual(fills[chosen['choice']], min(fills.values()))  # rounded to 0.01 s: ties possible
        self.assertFalse(chosen['full_copy_predicted_faster'])
        gap = int(chosen['choice'][4:])
        self.assertEqual((fast.get('fill_gaps') or {}).get('gap_pages', 0), gap)
        self.assertGreater(fast['files_chosen'], 0)
        for f in json.loads(plan_path.read_text())['files']:  # no gap the chosen fill should have closed is left
            for (first, count), (after, _) in zip(f['runs'], f['runs'][1:]):
                self.assertTrue(after == first + count or after - (first + count) > gap, f)
        self.check_copy(target)

    def test_the_selector_still_takes_a_fill_where_pg_basebackup_is_predicted_faster(self):
        """Disks that read 1 page a second: pg_basebackup is predicted faster, which the statistics say; the fast path
        still runs, at the fill with the fewest requests, and the copy recovers to the source."""
        target, stats = self.refresh('fast-slow-disks', 'stale', '--selector', '--selector-read-ops', '1')
        fast = stats['fast_path']
        self.assertTrue(fast['used'], fast['reasons'])
        chosen = fast['selector']
        self.assertTrue(chosen['full_copy_predicted_faster'])
        self.assertEqual(chosen['disks']['read_ops'], 1.0)
        fills = {k: v for k, v in chosen['predicted_seconds'].items() if k != 'full'}
        self.assertEqual(fills[chosen['choice']], min(fills.values()))
        self.assertEqual(chosen['choice'], 'fill4')  # the fewest requests
        self.assertGreater(fast['files_chosen'], 0)
        self.check_copy(target)

    def test_a_crashed_copy_is_refused_and_refreshed_by_hashing(self):
        target, stats = self.refresh('refused', 'crashed')
        fast, received = stats['fast_path'], stats['receiver']['fast_path']
        self.assertFalse(fast['used'])
        self.assertIn('not stopped cleanly', received['refused'])
        self.assertIn('not stopped cleanly', fast['reasons'][0])
        self.assertEqual(stats['receiver']['fsync_mode'], 'main')  # auto: no lineage facts, so the main loop
        self.assertGreater(stats['receiver']['bytes_written']['by_message_type']['BASIS_NODES']['frames'], 0)
        self.check_copy(target)


@unittest.skipUnless(available() and all((PG_BIN / tool).exists() for tool in ('pgbench', 'pg_waldump', 'pg_basebackup')),
                     SKIP_REASON)
class FailedOver(unittest.TestCase):
    """--diverged-basis (idea 1): a primary P and its standby; the standby promoted (S, timeline 2) while P kept
    writing on timeline 1, then P stopped cleanly. After the fork each side updated other rows and created its own
    table of the same name (the promoted side's OIDs start past the range P had reserved, so their files differ)."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.sockets = Path(tempfile.mkdtemp(prefix='pgo'))
        cls.env = dict(os.environ, LC_ALL='C', LANG='C')
        primary, cls.source = cls.root / 'P', cls.root / 'S'
        cls.cmd([PG_BIN / 'initdb', '-D', primary, '-U', 't', '-A', 'trust', '--no-sync', '--data-checksums'])
        with open(primary / 'postgresql.conf', 'a') as conf:
            conf.write('summarize_wal = on\nwal_keep_size = 512MB\n')
        cls.ctl(primary, 'start', 5434)
        cls.cmd([PG_BIN / 'pgbench', '-i', '-q', '-s', 2, '-h', cls.sockets, '-p', 5434, '-U', 't', 'postgres'])
        cls.cmd([PG_BIN / 'pg_basebackup', '-D', cls.source, '-h', cls.sockets, '-p', 5434, '-U', 't',
                     '-X', 'stream', '-c', 'fast', '-R'])
        cls.ctl(cls.source, 'start', 5432)
        cls.sql('CHECKPOINT', 5434)
        cls.sql('UPDATE pgbench_accounts SET abalance = abalance + 1 WHERE aid % 97 = 0', 5434)  # common history
        lsn = cls.sql('SELECT pg_current_wal_lsn()', 5434)
        for _ in range(600):
            if cls.sql(f"SELECT pg_last_wal_replay_lsn() >= '{lsn}'", 5432) == 't':
                break
            time.sleep(0.05)
        cls.cmd([PG_BIN / 'pg_ctl', '-D', cls.source, '-w', 'promote'])
        # P, now the old primary, keeps writing on timeline 1: updates, a new table, and reads that set hint bits.
        cls.sql('UPDATE pgbench_accounts SET abalance = -7 WHERE aid % 53 = 0', 5434)
        cls.sql('CREATE TABLE side AS SELECT g, repeat(md5(g::text), 3) AS v FROM generate_series(1, 6000) g', 5434)
        cls.sql('SELECT count(*) FROM pgbench_accounts WHERE abalance = -7', 5434)
        cls.ctl(primary, 'stop', 5434)
        shutil.copytree(primary, cls.root / 'old')
        # S writes its own changes and its own table after the fork.
        cls.sql('UPDATE pgbench_accounts SET abalance = abalance + 3 WHERE aid % 41 = 0')
        cls.sql('CREATE TABLE side AS SELECT g FROM generate_series(1, 700) g')
        cls.sql('CHECKPOINT')

    @classmethod
    def tearDownClass(cls):
        cls.ctl(cls.source, 'stop', 5432, check=False, mode='immediate')
        cls.ctl(cls.root / 'P', 'stop', 5434, check=False, mode='immediate')
        shutil.rmtree(cls.sockets, True)
        cls.temp.cleanup()

    cmd, ctl, sql = (RealServer.__dict__[name] for name in ('cmd', 'ctl', 'sql'))

    STATE = ("SELECT md5(string_agg(aid || ':' || abalance, ',' ORDER BY aid)) || '/' || "
             "(SELECT count(*) || ':' || sum(g) FROM side) FROM pgbench_accounts")

    def refresh(self, name, *extra, prepare=None):
        target = self.root / name
        shutil.copytree(self.root / 'old', target)
        if prepare is not None:
            prepare(target)
        stats_path = self.root / f'{name}.json'
        result = subprocess.run(python_command(
            'local', '--source', self.source, '--source-kind', 'live', '--pg-controldata', CONTROLDATA,
            '--pg-conn', f'host={self.sockets} port=5432 user=t dbname=postgres', '--basis', target, '--in-place',
            '--fast-path', 'summaries', '--stats-json', stats_path, *extra), capture_output=True, text=True,
            cwd=REPO_ROOT, env=subprocess_env(), timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        return target, json.loads(stats_path.read_text())

    def check_copy(self, target):
        self.ctl(target, 'start', 5433)
        try:
            self.assertEqual(self.sql(self.STATE, 5433), self.sql(self.STATE))
            amcheck = self.cmd([PG_BIN / 'pg_amcheck', '-h', self.sockets, '-p', 5433, '-U', 't', '--all',
                                    '--heapallindexed', '--install-missing'], check=False)
            self.assertEqual(amcheck.returncode, 0, amcheck.stdout + amcheck.stderr)
        finally:
            self.ctl(target, 'stop', 5433, check=False)

    def test_the_old_primary_takes_the_fast_path_with_its_own_wal(self):
        target, stats = self.refresh('diverged')  # --diverged-basis is on with --fast-path (EXP-71)
        fast, received = stats['fast_path'], stats['receiver']['fast_path']
        self.assertTrue(fast['used'], fast['reasons'])
        diverged = fast['diverged_basis']
        self.assertLess(L(diverged['checkpoint']), L(diverged['fork']))
        self.assertGreater(diverged['blocks'], 0)
        self.assertEqual(received['diverged_basis']['blocks'], diverged['blocks'])
        self.assertLess(stats['pages']['sent'], stats['pages']['total'])
        self.assertNotIn('BASIS_NODES', stats['receiver']['bytes_written']['by_message_type'])  # B never hashed
        self.check_copy(target)

    def test_without_the_option_the_old_primary_is_refused_and_hashed(self):
        target, stats = self.refresh('refused', '--no-diverged-basis')
        self.assertFalse(stats['fast_path']['used'])
        self.assertIn('past where', stats['fast_path']['reasons'][0])
        self.assertNotIn('diverged_basis', stats['fast_path'])
        self.check_copy(target)

    def test_an_old_primary_without_the_wal_since_the_checkpoint_is_refused_and_hashed(self):
        def drop_old_wal(target):
            segments = sorted(p for p in (target / 'pg_wal').iterdir() if p.name.startswith('00000001'))
            for path in segments[:-1]:  # only the segment holding the shutdown checkpoint stays
                path.unlink()
        target, stats = self.refresh('no-wal', prepare=drop_old_wal)
        fast = stats['fast_path']
        self.assertFalse(fast['used'])
        self.assertIn('refused', fast['diverged_basis'])
        self.assertEqual(fast['reasons'], [fast['diverged_basis']['refused']])
        self.check_copy(target)


if __name__ == '__main__':
    unittest.main()
