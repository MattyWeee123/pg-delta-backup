"""The WAL-summary fast path (pgdelta.fastpath, --fast-path summaries): its guard and page choice, and refreshes of
a real PostgreSQL 17 primary's old copy, one cleanly stopped (the fast path runs) and one crashed (it is refused)."""
import json
import os
import shutil
import subprocess
import tempfile
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
        # EXP-68's defaults with --fast-path: 8 reader threads, and the receiver's syncs on its worker thread.
        self.assertEqual(fast['read_ahead']['threads'], 8)
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
                                     '--plan-out', plan_path)
        fast = stats['fast_path']
        self.assertTrue(fast['used'], fast['reasons'])
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

    def test_a_crashed_copy_is_refused_and_refreshed_by_hashing(self):
        target, stats = self.refresh('refused', 'crashed')
        fast, received = stats['fast_path'], stats['receiver']['fast_path']
        self.assertFalse(fast['used'])
        self.assertIn('not stopped cleanly', received['refused'])
        self.assertIn('not stopped cleanly', fast['reasons'][0])
        self.assertEqual(stats['receiver']['fsync_mode'], 'main')  # auto: no lineage facts, so the main loop
        self.assertGreater(stats['receiver']['bytes_written']['by_message_type']['BASIS_NODES']['frames'], 0)
        self.check_copy(target)


if __name__ == '__main__':
    unittest.main()
