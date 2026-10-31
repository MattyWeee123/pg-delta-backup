"""EXP-91's two changes: the sender hashing ahead on a thread of its own (--hash-ahead), and the receiver reading
each run back as it writes it (--verify-scope as-written). Both change when work is done, never what is sent or
written: every refresh must equal the source, and a misplaced or corrupted write must still fail.
"""

import os
import random
import shutil
import unittest
from unittest import mock

import types

from pgdelta import cli, fastpath, receiver as receiver_module, sender as sender_module
from pgdelta.errors import InvalidInput, TransferError, VerificationError
from pgdelta.inplace import CONTROL_FILE, MARKER

from pgdelta_support import PAGE, snapshot, transfer, write_tree
from test_pgdelta_engine import CONFIGS, settle
from test_pgdelta_inplace import InPlaceTestCase, in_place, pg_shapes
from test_pgdelta_live import StubServerTestCase


def heavy_trees(source, basis, seed=91):
    """Files whose 4-page nodes have most pages changed (heavy: "written" re-reads them whole), a grown and a new
    file, and unchanged ones."""
    rng = random.Random(seed)
    files = {f'base/1/{n:02d}': rng.randbytes((n % 4 + 3) * 4 * PAGE) for n in range(12)}
    write_tree(basis, dict(files, **{'PG_VERSION': b'17\n', CONTROL_FILE: bytes(PAGE)}))
    changed = {}
    for n, (path, data) in enumerate(files.items()):
        data = bytearray(data)
        if n % 3:
            for page in range(len(data) // PAGE):
                if rng.random() < 0.6:
                    data[page * PAGE + rng.randrange(PAGE)] ^= 0xFF
        changed[path] = bytes(data)
    changed['base/1/00'] += rng.randbytes(5 * PAGE)  # grown
    changed['base/1/new'] = rng.randbytes(6 * PAGE)
    write_tree(source, dict(changed, **{'PG_VERSION': b'17\n', CONTROL_FILE: bytes(PAGE)}))


class HashAheadTests(InPlaceTestCase):
    """--hash-ahead: S hashed on the planner's thread, up to a bound of bytes ahead of the file being sent."""

    def options(self, config=None, hash_ahead=1 << 30, read_ahead=3, **receiver):
        options = in_place(config) if config else {'receiver_options': {'in_place': True}}
        options['receiver_options'] = dict(options['receiver_options'], **receiver)
        return dict(options, hash_ahead=hash_ahead, read_ahead=read_ahead)

    def reset(self):
        shutil.rmtree(self.root / 'S', ignore_errors=True)
        shutil.rmtree(self.root / 'B', ignore_errors=True)

    def test_every_engine_and_bound_is_refreshed(self):
        for name, config in CONFIGS.items():
            for hash_ahead, read_ahead in ((1, 0), (8 * PAGE, 2), (1 << 30, 3)):
                with self.subTest(config=name, hash_ahead=hash_ahead, read_ahead=read_ahead):
                    self.reset()
                    pg_shapes(self.source, self.basis)
                    outcome = transfer(self.source, self.basis, None,
                                       **self.options(config, hash_ahead, read_ahead))
                    self.assert_refreshed(outcome)
                    counts = outcome.sender['hash_ahead']
                    self.assertEqual((counts['bytes'], counts['read_ahead_threads']), (hash_ahead, read_ahead))
                    self.assertGreater(counts['most_ahead_bytes'], 0)
                    if read_ahead:
                        self.assertGreater(counts['read_ahead_reads'], 0)
                    settle(self)

    def test_only_the_order_of_the_frames_changes(self):
        heavy_trees(self.source, self.basis)
        shutil.copytree(self.basis, self.root / 'B2', symlinks=True)
        ahead = transfer(self.source, self.basis, None, **self.options())
        plain = transfer(self.source, self.root / 'B2', None, receiver_options={'in_place': True})
        self.assert_refreshed(ahead)
        self.assert_refreshed(plain, self.root / 'B2')
        self.assertEqual(snapshot(self.basis), snapshot(self.root / 'B2'))
        for key in ('pages', 'nodes', 'files', 'leaf_requests'):
            self.assertEqual(ahead.sender[key], plain.sender[key], key)
        self.assertNotIn('hash_ahead', plain.sender)  # off, the statistics keep their keys
        settle(self)

    def test_many_files_planned_ahead_never_trip_the_in_place_guard(self):
        # Leaf requests are queued by the planner and sent by the sending thread before each run, so a request for
        # a file far ahead lands among another file's pages; it must still precede that file's own pages.
        files = {f'base/{number:03d}': random.Random(number).randbytes((number % 5 + 1) * PAGE)
                 for number in range(60)}
        write_tree(self.basis, dict(files, **{'PG_VERSION': b'17\n', CONTROL_FILE: bytes(PAGE)}))
        changed = {path: data[:-1] + bytes([data[-1] ^ 1]) for path, data in files.items()}
        write_tree(self.source, dict(changed, **{'PG_VERSION': b'17\n', CONTROL_FILE: bytes(PAGE)}))
        for jobs in (1, 3):
            with self.subTest(jobs=jobs):
                shutil.rmtree(self.basis)
                write_tree(self.basis, dict(files, **{'PG_VERSION': b'17\n', CONTROL_FILE: bytes(PAGE)}))
                outcome = transfer(self.source, self.basis, None, jobs=jobs, receiver_jobs=jobs,
                                   **self.options())
                self.assert_refreshed(outcome)
                settle(self)

    def test_a_new_copy_from_a_backup_reads_its_pages_ahead(self):
        heavy_trees(self.source, self.basis)
        before = snapshot(self.basis)
        outcome = transfer(self.source, self.basis, self.root / 'O', hash_ahead=4 * PAGE, read_ahead=2)
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assertIsNone(outcome.receiver_error, repr(outcome.receiver_error))
        self.assertEqual(snapshot(self.root / 'O'), snapshot(self.source))
        self.assertEqual(snapshot(self.basis), before)
        self.assertGreater(outcome.sender['hash_ahead']['read_ahead_reads'], 0)
        settle(self)

    def test_a_planning_failure_ends_the_run_and_leaves_b_blocked(self):
        pg_shapes(self.source, self.basis)
        original, calls = sender_module._Sender._plan, []

        def failing(self_, index, entry, requests=None):
            calls.append(index)
            if len(calls) == 3:
                raise TransferError('cannot read source file: planted')
            return original(self_, index, entry, requests)

        with mock.patch.object(sender_module._Sender, '_plan', failing):
            outcome = transfer(self.source, self.basis, None, **self.options())
        self.assertIsInstance(outcome.sender_error, TransferError)
        self.assertIn('planted', str(outcome.sender_error))
        self.assertIsNotNone(outcome.receiver_error)
        self.assertTrue(os.path.lexists(self.basis / MARKER) or snapshot(self.basis) != snapshot(self.source))
        settle(self)

    def test_read_ahead_needs_the_fast_path_or_hash_ahead(self):
        with self.assertRaisesRegex(InvalidInput, 'needs --fast-path or --hash-ahead'):
            fastpath.check_mode(None, 'live', False, 4)
        fastpath.check_mode(None, 'live', False, 4, hash_ahead=1 << 20)
        with self.assertRaisesRegex(InvalidInput, 'hash-ahead must be a whole number of bytes'):
            fastpath.check_mode(None, 'live', False, 0, hash_ahead=-1)

    def test_the_command_line(self):
        self.assertEqual([cli._byte_size(text) for text in ('0', '512', '64K', '256m', '32G', '1T')],
                         [0, 512, 64 << 10, 256 << 20, 32 << 30, 1 << 40])
        for text in ('', 'G', '1.5G', '3X', '-1G'):
            with self.subTest(text=text), self.assertRaises(Exception):
                cli._byte_size(text)
        parser = cli.build_parser()
        args = parser.parse_args(['send', '--source', 'S', '--connect', 'h:1', '--hash-ahead', '32G'])
        self.assertEqual(cli._sender_kwargs(args, None)['hash_ahead'], 32 << 30)
        self.assertEqual(cli._read_ahead(args), fastpath.DEFAULT_READ_AHEAD)
        args = parser.parse_args(['send', '--source', 'S', '--connect', 'h:1'])
        self.assertEqual((cli._sender_kwargs(args, None)['hash_ahead'], cli._read_ahead(args)), (0, 0))


class AsWrittenTests(InPlaceTestCase):
    """--verify-scope as-written: each run read back as it is written, every node composed from those leaves."""

    def options(self, config=None, scope='as-written'):
        options = in_place(config) if config else {'receiver_options': {'in_place': True}}
        options['receiver_options'] = dict(options['receiver_options'], verify_scope=scope)
        return options

    def reset(self):
        shutil.rmtree(self.root / 'S', ignore_errors=True)
        shutil.rmtree(self.root / 'B', ignore_errors=True)

    def test_every_shape_and_engine_is_refreshed_and_verified(self):
        for name, config in CONFIGS.items():
            with self.subTest(config=name):
                self.reset()
                pg_shapes(self.source, self.basis)
                outcome = transfer(self.source, self.basis, None, **self.options(config))
                self.assert_refreshed(outcome)
                self.assertEqual(outcome.receiver['verify_scope'], 'as-written')
                counts = outcome.receiver['verify_written']
                self.assertEqual(counts['pages_read_back'], outcome.receiver['pages_received'])
                # Read again: only the neighbours of runs in nodes sent whole (a new or grown file's).
                self.assertLess(counts['pages_reread'], counts['pages_read_back'] / 4)
                self.assertEqual(outcome.receiver['files_verified'], outcome.receiver['files'])
                settle(self)

    def test_heavy_nodes_are_composed_instead_of_reread(self):
        heavy_trees(self.source, self.basis)
        shutil.copytree(self.basis, self.root / 'B2', symlinks=True)
        written = transfer(self.source, self.root / 'B2', None, **self.options(scope='written'))
        as_written = transfer(self.source, self.basis, None, **self.options())
        self.assert_refreshed(written, self.root / 'B2')
        self.assert_refreshed(as_written)
        self.assertEqual(snapshot(self.basis), snapshot(self.root / 'B2'))
        before, after = written.receiver['verify_written'], as_written.receiver['verify_written']
        self.assertGreater(before['nodes_reread'], 0)
        self.assertLess(after['nodes_reread'], before['nodes_reread'])
        self.assertGreater(after['nodes_composed'], before['nodes_composed'])
        # An unwritten node beside a run in a node sent whole (the grown file's end) is re-read whole, not kept.
        self.assertLessEqual(after['nodes_kept'], before['nodes_kept'])
        self.assertGreaterEqual(after['nodes_kept'], before['nodes_kept'] - 2)
        settle(self)

    def test_a_page_written_at_the_wrong_offset_fails_the_verification(self):
        pg_shapes(self.source, self.basis)
        original, calls = receiver_module._write_all, []

        def misplaced(fd, data, offset):
            calls.append(offset)
            return original(fd, data, offset + PAGE if len(calls) == 1 else offset)

        with mock.patch.object(receiver_module, '_write_all', misplaced):
            outcome = transfer(self.source, self.basis, None, **self.options())
        self.assertIsInstance(outcome.receiver_error, VerificationError)
        self.assertIn('does not match the source digest', str(outcome.receiver_error))
        self.assertTrue(os.path.lexists(self.basis / MARKER))  # still blocked
        settle(self)

    def test_bytes_written_unlike_those_received_fail_the_verification(self):
        # The read-back hashes what the file holds, not what arrived: a write that changes the bytes is caught.
        heavy_trees(self.source, self.basis)
        original, calls = receiver_module._write_all, []

        def corrupting(fd, data, offset):
            calls.append(offset)
            if len(calls) == 2:
                data = bytes(data[:-1]) + bytes([data[-1] ^ 1])
            return original(fd, data, offset)

        with mock.patch.object(receiver_module, '_write_all', corrupting):
            outcome = transfer(self.source, self.basis, None, **self.options())
        self.assertIsInstance(outcome.receiver_error, VerificationError)
        self.assertTrue(os.path.lexists(self.basis / MARKER))
        settle(self)

    def test_the_misplaced_write_switch_is_caught(self):
        # EXP-32's negative control, with this scope and with hashing ahead.
        for hash_ahead in (0, 1 << 30):
            with self.subTest(hash_ahead=hash_ahead):
                self.reset()
                heavy_trees(self.source, self.basis)
                with mock.patch.dict(os.environ, {receiver_module.MISPLACE_ENV: '1'}):
                    outcome = transfer(self.source, self.basis, None, hash_ahead=hash_ahead,
                                       read_ahead=3 if hash_ahead else 0, **self.options())
                self.assertIsInstance(outcome.receiver_error, VerificationError)
                self.assertTrue(os.path.lexists(self.basis / MARKER))
                self.assertFalse(os.path.lexists(self.basis / CONTROL_FILE))  # blocked from starting
                rerun = transfer(self.source, self.basis, None, **self.options())
                self.assert_refreshed(rerun)
                settle(self)

    def test_a_run_of_like_pages_written_a_page_back_is_caught(self):
        # The review's case: nodes sent whole (--high-churn-node-fraction) may hold pages equal to B's. A run of zero
        # pages written one page back reads back unchanged (B's last page is zero too) and overwrites the last page
        # of the run before it, which was read back already. Its neighbours are read again at the file's end.
        rng = random.Random(911)
        old = [rng.randbytes(PAGE) for _ in range(135)] + [bytes(PAGE)]
        new = [rng.randbytes(PAGE) for _ in range(128)] + [bytes(PAGE)] * 8  # 0-127 changed, 128-135 zero
        for scope in ('written', 'as-written'):
            with self.subTest(scope=scope):
                self.reset()
                write_tree(self.basis, {'PG_VERSION': b'17\n', CONTROL_FILE: bytes(PAGE), 'base/1/f': b''.join(old)})
                write_tree(self.source, {'PG_VERSION': b'17\n', CONTROL_FILE: bytes(PAGE), 'base/1/f': b''.join(new)})
                original, shifted = receiver_module._write_all, []

                def back_one(fd, data, offset):
                    if offset == 128 * PAGE and not shifted:  # the second run, all zero pages
                        shifted.append(offset)
                        offset -= PAGE
                    return original(fd, data, offset)

                with mock.patch.object(receiver_module, '_write_all', back_one):
                    outcome = transfer(self.source, self.basis, None, high_churn_node_fraction=0.1,
                                       **self.options(scope=scope))
                self.assertEqual(shifted, [128 * PAGE])
                self.assertIsInstance(outcome.receiver_error, VerificationError)
                self.assertTrue(os.path.lexists(self.basis / MARKER))
                settle(self)

    def test_as_written_needs_in_place(self):
        pg_shapes(self.source, self.basis)
        before = snapshot(self.basis)
        outcome = transfer(self.source, self.basis, self.root / 'O', receiver_options={'verify_scope': 'as-written'})
        self.assertIsInstance(outcome.receiver_error, InvalidInput)
        self.assertIn('--verify-scope as-written needs --in-place', str(outcome.receiver_error))
        self.assertEqual(snapshot(self.basis), before)
        settle(self)

    def test_both_changes_together_on_every_engine(self):
        for name, config in CONFIGS.items():
            with self.subTest(config=name):
                self.reset()
                heavy_trees(self.source, self.basis, seed=len(name))
                options = self.options(config)
                outcome = transfer(self.source, self.basis, None, hash_ahead=6 * PAGE, read_ahead=2, **options)
                self.assert_refreshed(outcome)
                counts = outcome.receiver['verify_written']
                self.assertLess(counts['pages_reread'], counts['pages_read_back'] / 4)
                settle(self)


class StallWatchTests(unittest.TestCase):
    def test_the_planners_wait_counts_while_the_sending_thread_waits_for_it(self):
        channel = types.SimpleNamespace(sending_since=None)
        inbox = sender_module.Inbox(channel)
        watch = sender_module.StallWatch(channel, inbox, 30)
        inbox.quiet_since, inbox.quiet_for = 100.0, 'basis digests of base/1/f'
        self.assertIsNone(watch.stalled(200.0))  # the sending thread is busy (sending, hashing): no stall
        inbox.taking_since = 105.0
        self.assertIsNone(watch.stalled(120.0))
        self.assertEqual(watch.stalled(140.0), 'waiting for basis digests of base/1/f')
        inbox.last_message = 130.0  # the receiver is alive
        self.assertIsNone(watch.stalled(140.0))


class LiveTests(StubServerTestCase):
    """A running server read in backup mode (the VMs' case), with both changes."""

    def test_in_place_with_both_changes(self):
        basis = self.root / 'B'
        shutil.copytree(self.datadir, basis)
        write_tree(basis, {'backup_label': b'START WAL LOCATION: an older run\n',
                           'base/5/16384': os.urandom(PAGE)})
        os.unlink(basis / 'postmaster.pid')
        with self.env():
            outcome = transfer(self.datadir, basis, None, source_kind='live', pg_controldata=self.stub,
                               pg_conn='host=stub user=u password=pw', psql=self.psql, hash_ahead=2 * PAGE,
                               read_ahead=2, receiver_options={'in_place': True, 'verify_scope': 'as-written'})
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assertIsNone(outcome.receiver_error, repr(outcome.receiver_error))
        self.assertEqual((basis / 'base/5/16384').read_bytes()[:64], bytes.fromhex('aa' * 64))
        self.assertEqual((basis / 'base/5/77777').read_bytes(), bytes.fromhex('bb' * PAGE))
        self.assertFalse(os.path.lexists(basis / MARKER))
        self.assertEqual(outcome.sender['hash_ahead']['bytes'], 2 * PAGE)
        self.assertEqual(outcome.receiver['verify_scope'], 'as-written')
        settle(self)


if __name__ == '__main__':
    unittest.main()
