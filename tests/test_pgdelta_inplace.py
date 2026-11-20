"""In-place refresh (--in-place, owner decision O5): B itself becomes S, blocked from starting until committed.

B must end byte-identical to S for every file shape and engine setting,
hold the marker and no global/pg_control while it is being changed, stay
blocked after any failure, and a rerun must complete it. With PostgreSQL
binaries, a receiver refreshing a real cluster in place is killed at
several points; pg_ctl must refuse the blocked copy, and a rerun must make
it byte-identical to S and startable.
"""
import argparse
import base64
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest import mock

from pgdelta import cli, inplace as inplace_module, protocol, receiver as receiver_module
from pgdelta.errors import InvalidInput, PeerError, PgDeltaError, ProtocolError, TransferError, VerificationError
from pgdelta.identity import read_identity
from pgdelta.inplace import CONTROL_FILE, CONTROL_PENDING, MARKER
from pgdelta.protocol import Channel
from pgdelta.receiver import prepare_receiver, receipt_path
from pgdelta.sender import run_sender

from pgdelta_cluster import CONTROLDATA, SKIP_REASON, Servers, available, initdb
from pgdelta_support import (PAGE, REPO_ROOT, Outcome, channel_pair, comparable, differing_pages, python_command,
                             run_receiver_thread, snapshot, subprocess_env, total_pages, transfer, write_tree)
from test_pgdelta_engine import CONFIGS, settle
from test_pgdelta_failures import CorruptingChannel
from test_pgdelta_parallel import build_shapes

KEY = bytes(range(32))
IDENTITY = {'system_identifier': '7', 'pg_control_version': 1700, 'catalog_version': 202406281,
            'block_size': PAGE, 'blocks_per_segment': 131072, 'data_checksum_version': 0,
            'redo_lsn': '0/1', 'timeline': 1}


def in_place(config):
    """An engine configuration of test_pgdelta_engine, refreshing B in place."""
    return dict(config, receiver_options=dict(config['receiver_options'], in_place=True))


def run(*args, timeout=180):
    return subprocess.run(python_command(*args), capture_output=True, text=True, cwd=REPO_ROOT,
                          env=subprocess_env(), timeout=timeout)


def flip(blob, *offsets):
    blob = bytearray(blob)
    for offset in offsets:
        blob[offset] ^= 0xFF
    return bytes(blob)


def pg_shapes(source, basis):
    """build_shapes, plus the PG_VERSION that an in-place refresh needs in B (and S keeps for a rerun)."""
    build_shapes(source, basis)
    write_tree(source, {'PG_VERSION': b'17\n'})
    write_tree(basis, {'PG_VERSION': b'17\n'})


def add_type_changes(source, basis):
    """Paths whose type differs: a directory of B becomes a file, a file of B a directory, and B-only trees."""
    rng = random.Random(77)
    write_tree(source, {'base/5/was_dir': rng.randbytes(3 * PAGE), 'base/6/inside': rng.randbytes(PAGE)},
               ['new_empty_dir/sub'])
    write_tree(basis, {'base/5/was_dir/inner/f': rng.randbytes(PAGE), 'base/6': rng.randbytes(2 * PAGE),
                       'stale_dir/deep/f': b'gone', 'stale_file': b'gone'}, ['stale_empty_dir'])


class InPlaceTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.basis = self.root / 'S', self.root / 'B'

    def small_trees(self):
        rng = random.Random(90)
        old = rng.randbytes(12 * PAGE)
        new = bytearray(old)
        new[5 * PAGE] ^= 1
        write_tree(self.source, {'PG_VERSION': b'17\n', 'base/1/rel': bytes(new),
                                 'base/1/added': rng.randbytes(3 * PAGE), CONTROL_FILE: rng.randbytes(PAGE)})
        write_tree(self.basis, {'PG_VERSION': b'17\n', 'base/1/rel': old, 'stale': b'x',
                                CONTROL_FILE: rng.randbytes(PAGE)})

    def assert_refreshed(self, outcome, basis=None):
        basis = basis or self.basis
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assertIsNone(outcome.receiver_error, repr(outcome.receiver_error))
        self.assertEqual(snapshot(basis), snapshot(self.source))
        self.assertFalse(os.path.lexists(basis / MARKER))
        receipt = json.loads(receipt_path(basis).read_text())
        self.assertEqual((receipt['status'], receipt['in_place'], receipt['mode']),
                         ('committed', True, outcome.sender['mode']))
        self.assertEqual(receipt['files_digest'], outcome.sender['files_digest'])
        self.assertEqual(Path(receipt['output']), basis.absolute())
        self.assertIsNotNone(receipt['committed_at'])
        self.assertTrue(outcome.sender['in_place'])
        self.assertTrue(outcome.receiver['in_place'])
        self.assertTrue(outcome.receiver['published'])
        self.assertEqual(outcome.receiver['switches']['rebuild'], 'in-place')
        self.assertEqual((outcome.receiver['basis_copy_bytes'], outcome.receiver['basis_copies']), (0, 0))

    def assert_blocked(self, basis=None):
        basis = basis or self.basis
        self.assertTrue(os.path.isfile(basis / MARKER))
        self.assertFalse(os.path.lexists(basis / CONTROL_FILE))
        self.assertTrue(os.path.isfile(basis / CONTROL_PENDING))
        self.assertFalse(os.path.lexists(receipt_path(basis)))


class ShapeTests(InPlaceTestCase):
    def test_every_shape_and_engine_setting_ends_byte_identical_to_s(self):
        template = self.root / 'B-template'
        pg_shapes(self.source, template)
        add_type_changes(self.source, template)
        expected_sent = differing_pages(self.source, template)
        results = {}
        for label, config in CONFIGS.items():
            basis = self.root / f'B-{label.replace(" ", "_")}'
            shutil.copytree(template, basis)
            outcome = transfer(self.source, basis, None, key=KEY, **in_place(config))
            with self.subTest(config=label):
                self.assert_refreshed(outcome, basis)
                self.assertEqual(outcome.sender['mode'], 'reuse')
                # pg_control differs in these trees, so sending it whole sends no extra page.
                self.assertEqual(outcome.sender['pages']['sent'], expected_sent)
                tree = outcome.receiver['in_place_tree']
                self.assertEqual(tree, {'attempt': 1, 'control_renamed': True, 'directories_created': 3,
                                        'files_removed': 5, 'directories_removed': 5})
                results[label] = comparable(outcome)
        first = next(iter(results))
        for label, result in results.items():
            with self.subTest(compare=label):
                self.assertEqual(result, results[first])
        settle(self)

    def test_a_basis_the_gate_would_not_reuse_is_refused_untouched(self):
        # In place S's pages go into B itself, so a B of another cluster, or of unknown identity, would be
        # overwritten: an in-place refresh runs only in reuse mode.
        pg_shapes(self.source, self.basis)
        before = snapshot(self.basis)
        cases = {
            'unknown identity': {},
            'another cluster': {'source_identity': IDENTITY,
                                'basis_identity': dict(IDENTITY, system_identifier='8')},
        }
        for label, identities in cases.items():
            with self.subTest(case=label):
                outcome = transfer(self.source, self.basis, None, reuse_any_basis=False,
                                   receiver_options={'in_place': True}, **identities)
                self.assertIsInstance(outcome.receiver_error, InvalidInput)
                self.assertIn('--reuse-any-basis', str(outcome.receiver_error))
                self.assertEqual(outcome.sender_error.exit_code, 2)
                self.assertNotIn('blocked from starting', str(outcome.sender_error))
                self.assertEqual(snapshot(self.basis), before)
                self.assertFalse(os.path.lexists(receipt_path(self.basis)))
        outcome = transfer(self.source, self.basis, None, reuse_any_basis=True, receiver_options={'in_place': True})
        self.assert_refreshed(outcome)
        self.assertEqual(outcome.sender['mode'], 'reuse')

    def test_a_second_run_on_a_refreshed_b_sends_only_pg_control(self):
        pg_shapes(self.source, self.basis)
        self.assert_refreshed(transfer(self.source, self.basis, None, receiver_options={'in_place': True}))
        outcome = transfer(self.source, self.basis, None, receiver_options={'in_place': True})
        self.assert_refreshed(outcome)
        self.assertEqual(outcome.sender['pages']['sent'], 1)  # pg_control is always sent whole
        self.assertEqual(outcome.receiver['in_place_tree'],
                         {'attempt': 1, 'control_renamed': True, 'directories_created': 0, 'files_removed': 0,
                          'directories_removed': 0})

    def test_read_only_files_and_directories_of_b_are_patched_and_get_the_source_modes(self):
        rng = random.Random(91)
        old = rng.randbytes(4 * PAGE)
        new = bytearray(old)
        new[PAGE] ^= 1
        write_tree(self.source, {'PG_VERSION': b'17\n', 'base/ro': bytes(new), 'base/keep': old,
                                 CONTROL_FILE: bytes(PAGE)},
                   modes={'base/ro': 0o440, 'base/keep': 0o400, 'base': 0o750})
        write_tree(self.basis, {'PG_VERSION': b'17\n', 'base/ro': old, 'base/keep': old, CONTROL_FILE: bytes(PAGE)},
                   modes={'base/ro': 0o400, 'base/keep': 0o600})
        os.chmod(self.basis / 'base', 0o500)
        self.addCleanup(os.chmod, self.basis / 'base', 0o700)
        outcome = transfer(self.source, self.basis, None, receiver_options={'in_place': True})
        self.assert_refreshed(outcome)

    def test_mid_run_b_holds_the_marker_and_no_pg_control(self):
        pg_shapes(self.source, self.basis)
        basis, seen = self.basis, []

        class Watching(Channel):
            """Receiver side: B's state each time a FILE_DONE arrives."""

            def recv(self):
                message = super().recv()
                if message is not None and message[0] == protocol.FILE_DONE:
                    seen.append(tuple(os.path.lexists(basis / path) for path in (MARKER, CONTROL_FILE,
                                                                                  CONTROL_PENDING)))
                return message

        send_channel, receive_channel = channel_pair(receiver_class=Watching)
        outcome = Outcome()
        thread = run_receiver_thread(receive_channel, outcome, self.basis, None, reuse_any_basis=True,
                                     in_place=True)
        try:
            outcome.sender = run_sender(send_channel, self.source, node_blocks=4)
        finally:
            send_channel.close()
        thread.join(60)
        self.assert_refreshed(outcome)
        self.assertEqual(len(seen), len([path for path in self.source.rglob('*') if path.is_file()]))
        self.assertEqual(set(seen), {(True, False, True)})


class FailureTests(InPlaceTestCase):
    def test_a_failure_leaves_b_blocked_and_a_rerun_completes_it(self):
        pg_shapes(self.source, self.basis)
        receipt_path(self.basis).write_text('{"status": "committed"}\n')  # from an earlier refresh
        outcome = transfer(self.source, self.basis, None, sender_class=CorruptingChannel,
                           receiver_options={'in_place': True})
        self.assertIsInstance(outcome.receiver_error, VerificationError)
        self.assertIsInstance(outcome.sender_error, PeerError)
        self.assertEqual(outcome.sender_error.exit_code, 4)
        self.assertIn('is incomplete and blocked from starting', str(outcome.sender_error))
        self.assertIn('full copy', str(outcome.sender_error))
        self.assert_blocked()
        marker = json.loads((self.basis / MARKER).read_text())
        self.assertEqual((marker['status'], marker['attempt']), ('incomplete', 1))
        self.assertEqual(Path(marker['basis']), self.basis.absolute())

        rerun = transfer(self.source, self.basis, None, receiver_options={'in_place': True})
        self.assert_refreshed(rerun)
        self.assertEqual(rerun.receiver['in_place_tree']['attempt'], 2)
        self.assertFalse(rerun.receiver['in_place_tree']['control_renamed'])  # already renamed

    def test_a_failure_before_pg_control_was_renamed_does_not_say_b_is_blocked(self):
        self.small_trees()

        def broken(basis):
            raise OSError(errno.EIO, 'Input/output error (injected)')

        with mock.patch.object(inplace_module, 'block_startup', broken):
            outcome = transfer(self.source, self.basis, None, receiver_options={'in_place': True})
        self.assertIsInstance(outcome.receiver_error, TransferError)
        self.assertIn('injected', str(outcome.sender_error))
        self.assertNotIn('blocked from starting', str(outcome.sender_error))
        self.assertTrue(os.path.isfile(self.basis / MARKER))
        self.assertTrue(os.path.isfile(self.basis / CONTROL_FILE))
        self.assert_refreshed(transfer(self.source, self.basis, None, receiver_options={'in_place': True}))

    def test_a_rerun_takes_b_identity_from_the_marker(self):
        pg_shapes(self.source, self.basis)
        failed = transfer(self.source, self.basis, None, sender_class=CorruptingChannel, reuse_any_basis=False,
                          source_identity=IDENTITY, basis_identity=IDENTITY, receiver_options={'in_place': True})
        self.assertIsInstance(failed.receiver_error, VerificationError)
        self.assert_blocked()
        self.assertEqual(json.loads((self.basis / MARKER).read_text())['basis_identity'], IDENTITY)
        missing = self.root / 'no-such-pg_controldata'  # B has no pg_control to read: never run
        self.assertEqual(prepare_receiver(self.basis, None, in_place=True, pg_controldata=missing).identity,
                         IDENTITY)
        self.assertEqual(prepare_receiver(self.basis, self.root / 'O', pg_controldata=missing).identity, IDENTITY)
        rerun = transfer(self.source, self.basis, None, reuse_any_basis=False, source_identity=IDENTITY,
                         receiver_options={'in_place': True})
        self.assert_refreshed(rerun)
        self.assertEqual(rerun.sender['mode'], 'reuse')
        self.assertEqual(rerun.sender['basis_identity'], IDENTITY)

    def test_marker_left_before_pg_control_was_renamed_is_accepted(self):
        # Killed between writing the marker and renaming pg_control: B is unchanged apart from the marker.
        self.small_trees()
        (self.basis / MARKER).write_text(json.dumps({'schema_version': 1, 'attempt': 1, 'basis_identity': None}))
        outcome = transfer(self.source, self.basis, None, receiver_options={'in_place': True})
        self.assert_refreshed(outcome)
        self.assertEqual(outcome.receiver['in_place_tree']['attempt'], 2)
        self.assertTrue(outcome.receiver['in_place_tree']['control_renamed'])

    def test_refusals_leave_b_untouched(self):
        self.small_trees()
        cases = {
            'no pg_control in the source': ({CONTROL_FILE: None}, 'needs a PostgreSQL source'),
            'reserved marker name': ({MARKER: b'{}'}, 'reserves'),
            'reserved control name': ({CONTROL_PENDING: b'x'}, 'reserves'),
        }
        before = snapshot(self.basis)
        for label, (changes, message) in cases.items():
            with self.subTest(case=label):
                source = self.root / f'S-{label.replace(" ", "_")}'
                shutil.copytree(self.source, source)
                for path, data in changes.items():
                    if data is None:
                        os.unlink(source / path)
                    else:
                        (source / path).write_bytes(data)
                outcome = transfer(source, self.basis, None, receiver_options={'in_place': True})
                self.assertIsInstance(outcome.receiver_error, InvalidInput)
                self.assertIn(message, str(outcome.receiver_error))
                self.assertEqual(outcome.sender_error.exit_code, 2)
                self.assertNotIn('blocked from starting', str(outcome.sender_error))
                self.assertEqual(snapshot(self.basis), before)
                self.assertFalse(os.path.lexists(receipt_path(self.basis)))

    def test_a_directory_that_is_not_a_postgresql_copy_is_refused_untouched(self):
        self.small_trees()
        cases = {
            'no PostgreSQL file': {'docs/thesis.tex': b'chapter 1', 'photos/a.jpg': b'jpeg'},
            'no pg_control': {'PG_VERSION': b'17\n', 'base/1/rel': b'x'},
            'no PG_VERSION': {CONTROL_FILE: bytes(PAGE), 'base/1/rel': b'x'},
        }
        for label, files in cases.items():
            with self.subTest(case=label):
                other = self.root / label.replace(' ', '_')
                write_tree(other, files)
                before = snapshot(other)
                outcome = transfer(self.source, other, None, receiver_options={'in_place': True})
                self.assertIsInstance(outcome.receiver_error, InvalidInput)
                self.assertIn('not a copy of a PostgreSQL data directory', str(outcome.receiver_error))
                self.assertEqual(outcome.sender_error.exit_code, 2)
                result = run('local', '--source', self.source, '--basis', other, '--in-place', '--reuse-any-basis')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('not a copy of a PostgreSQL data directory', result.stderr)
                self.assertEqual(snapshot(other), before)

    def test_prepare_refuses_what_it_cannot_tell_apart(self):
        self.small_trees()
        with self.assertRaisesRegex(InvalidInput, 'takes no output'):
            prepare_receiver(self.basis, self.root / 'O', in_place=True)
        (self.basis / CONTROL_PENDING).write_bytes(b'x')
        with self.assertRaisesRegex(InvalidInput, 'not left by pgdelta'):
            prepare_receiver(self.basis, None, in_place=True)
        (self.basis / MARKER).write_text(json.dumps({'schema_version': 1, 'basis_identity': None}))
        with self.assertRaisesRegex(InvalidInput, 'both'):
            prepare_receiver(self.basis, None, in_place=True)
        (self.basis / MARKER).write_text('not json')
        with self.assertRaisesRegex(InvalidInput, 'unreadable'):
            prepare_receiver(self.basis, None, in_place=True)
        os.unlink(self.basis / MARKER)
        os.unlink(self.basis / CONTROL_PENDING)
        (self.basis / 'postmaster.pid').write_text('1\n')
        with self.assertRaisesRegex(InvalidInput, 'postmaster.pid'):
            prepare_receiver(self.basis, None, in_place=True)
        os.unlink(self.basis / 'postmaster.pid')
        receipt_path(self.basis).mkdir()
        with self.assertRaisesRegex(InvalidInput, 'not a regular file'):
            prepare_receiver(self.basis, None, in_place=True)
        receipt_path(self.basis).rmdir()
        with self.assertRaisesRegex(InvalidInput, 'overlaps source'):
            prepare_receiver(self.basis / 'base', None, in_place=True, local_source=self.basis)
        self.assertTrue(prepare_receiver(self.basis, None, in_place=True).in_place)


class HardLinkTests(InPlaceTestCase):
    """Patching a file of B in place changes every name of its inode: B must share none."""

    def linked_trees(self):
        rng = random.Random(93)
        old = rng.randbytes(4 * PAGE)
        write_tree(self.source, {'PG_VERSION': b'17\n', 'base/1/100': flip(old, 0),
                                 'base/1/200': flip(old, 0, PAGE), CONTROL_FILE: rng.randbytes(PAGE)})
        write_tree(self.basis, {'PG_VERSION': b'17\n', 'base/1/100': old, CONTROL_FILE: rng.randbytes(PAGE)})

    def assert_refused_untouched(self, outcome, before):
        self.assertIsInstance(outcome.receiver_error, InvalidInput)
        self.assertIn('more than one hard link', str(outcome.receiver_error))
        self.assertEqual(outcome.sender_error.exit_code, 2)
        self.assertEqual(snapshot(self.basis), before)
        self.assertFalse(os.path.lexists(self.basis / MARKER))
        self.assertFalse(os.path.lexists(receipt_path(self.basis)))

    def test_two_paths_of_b_sharing_an_inode_are_refused(self):
        self.linked_trees()
        os.link(self.basis / 'base/1/100', self.basis / 'base/1/200')
        before = snapshot(self.basis)
        outcome = transfer(self.source, self.basis, None, receiver_options={'in_place': True})
        self.assert_refused_untouched(outcome, before)
        self.assertEqual(os.stat(self.basis / 'base/1/100').st_nlink, 2)

    def test_b_sharing_inodes_with_another_copy_is_refused_and_that_copy_is_untouched(self):
        # B made from B0 as cp -al, rsync --link-dest or pg_combinebackup --link would make it.
        self.linked_trees()
        original = self.root / 'B0'
        os.rename(self.basis, original)
        shutil.copytree(original, self.basis, copy_function=os.link)
        before, original_before = snapshot(self.basis), snapshot(original)
        outcome = transfer(self.source, self.basis, None, receiver_options={'in_place': True})
        self.assert_refused_untouched(outcome, before)
        self.assertEqual(snapshot(original), original_before)

    def test_a_link_made_during_the_run_is_never_written_through(self):
        self.linked_trees()
        setup = prepare_receiver(self.basis, None, in_place=True, reuse_any_basis=True)
        outside = self.root / 'outside'
        os.link(self.basis / 'base/1/100', outside)
        old = outside.read_bytes()
        send_channel, receive_channel = channel_pair()
        outcome = Outcome()
        thread = run_receiver_thread(receive_channel, outcome, None, None, setup=setup)
        try:
            run_sender(send_channel, self.source, node_blocks=4)
        except PgDeltaError as exc:
            outcome.sender_error = exc
        finally:
            send_channel.close()
        thread.join(60)
        self.assertIsInstance(outcome.receiver_error, TransferError)
        self.assertIn('hard link', str(outcome.receiver_error))
        self.assertEqual(outside.read_bytes(), old)
        self.assert_blocked()
        settle(self)


class ConcurrencyTests(InPlaceTestCase):
    """One in-place run at a time on a B, and a run commits only a B that is still its own."""

    def two_sources(self):
        rng = random.Random(94)
        old, extra = rng.randbytes(4 * PAGE), rng.randbytes(PAGE)
        write_tree(self.basis, {'PG_VERSION': b'17\n', 'base/1/a': extra, 'base/1/b': old, 'base/1/stale': b'x',
                                CONTROL_FILE: rng.randbytes(PAGE)})
        self.first, self.second = self.root / 'S1', self.root / 'S2'
        write_tree(self.first, {'PG_VERSION': b'17\n', 'base/1/a': extra, 'base/1/b': flip(old, 0),
                                CONTROL_FILE: rng.randbytes(PAGE)})
        write_tree(self.second, {'PG_VERSION': b'17\n', 'base/1/a': extra, 'base/1/b': flip(old, PAGE),
                                 CONTROL_FILE: rng.randbytes(PAGE)})

    def start_paused(self, source):
        """An in-place run from `source`, paused at its first PAGE_DATA (B blocked and hashed); returns resume()."""
        paused, resume = threading.Event(), threading.Event()

        class Pausing(Channel):
            def recv(self):
                message = super().recv()
                if message is not None and message[0] == protocol.PAGE_DATA and not paused.is_set():
                    paused.set()
                    resume.wait(60)
                return message

        send_channel, receive_channel = channel_pair(receiver_class=Pausing)
        outcome = Outcome()
        receiver = run_receiver_thread(receive_channel, outcome, self.basis, None, reuse_any_basis=True,
                                       in_place=True)

        def send():
            try:
                outcome.sender = run_sender(send_channel, source, node_blocks=4)
            except BaseException as exc:
                outcome.sender_error = exc
            finally:
                send_channel.close()

        sender = threading.Thread(target=send)
        sender.start()
        self.assertTrue(paused.wait(30))

        def finish():
            resume.set()
            sender.join(60)
            receiver.join(60)
            return outcome
        self.addCleanup(resume.set)
        return finish

    def test_a_second_run_on_a_b_being_refreshed_is_refused(self):
        self.two_sources()
        finish = self.start_paused(self.first)
        during = snapshot(self.basis)
        second = transfer(self.second, self.basis, None, receiver_options={'in_place': True})
        self.assertIsInstance(second.receiver_error, InvalidInput)
        self.assertIn('another run', str(second.receiver_error))
        self.assertEqual(second.sender_error.exit_code, 2)
        self.assertEqual(snapshot(self.basis), during)
        self.source = self.first
        self.assert_refreshed(finish())
        settle(self)

    def test_prepare_holds_b_until_the_setup_is_released(self):
        self.small_trees()
        first = prepare_receiver(self.basis, None, in_place=True)
        with self.assertRaisesRegex(InvalidInput, 'another run'):
            prepare_receiver(self.basis, None, in_place=True)
        with self.assertRaisesRegex(InvalidInput, 'another run'):  # by another path to the same directory
            prepare_receiver(self.root / '.' / 'B', None, in_place=True)
        self.assertEqual(prepare_receiver(self.basis, self.root / 'O').basis, self.basis)  # a new copy only reads B
        first.release()
        prepare_receiver(self.basis, None, in_place=True).release()

    def test_a_run_whose_marker_was_replaced_does_not_commit(self):
        # Another writer the lock cannot see (another host, say) took B over while this run was paused.
        self.two_sources()
        finish = self.start_paused(self.first)
        record = json.loads((self.basis / MARKER).read_text())
        (self.basis / MARKER).write_text(json.dumps(dict(record, run_id='someone-else')))
        outcome = finish()
        self.assertIsInstance(outcome.receiver_error, TransferError)
        self.assertIn('not this run', str(outcome.receiver_error))
        self.assertFalse(os.path.lexists(self.basis / CONTROL_FILE))
        self.assertTrue(os.path.isfile(self.basis / CONTROL_PENDING))
        self.assertTrue(os.path.isfile(self.basis / 'base/1/stale'))  # nothing deleted
        self.assertFalse(os.path.lexists(receipt_path(self.basis)))
        settle(self)


class OrderGuardTests(InPlaceTestCase):
    """B's file i is written only after its digests were sent and its leaf requests answered."""

    def session(self, script):
        """Run `script(channel, replies)` as a hand-written sender against an in-place receiver."""
        rng = random.Random(92)
        old = rng.randbytes(8 * PAGE)
        write_tree(self.basis, {'PG_VERSION': b'17\n', 'base/f': old, CONTROL_FILE: bytes(PAGE)})
        self.old = old
        send_channel, receive_channel = channel_pair()
        outcome = Outcome()
        thread = run_receiver_thread(receive_channel, outcome, self.basis, None, reuse_any_basis=True,
                                     in_place=True)
        hello = {'protocol_version': 1, 'block': PAGE, 'node_blocks': 4,
                 'key': base64.b64encode(KEY).decode(), 'source_identity': None, 'root_mode': 0o700,
                 'directories': [['base', 0o700], ['global', 0o700]],
                 'files': [['base/f', 8 * PAGE, 0o600], [CONTROL_FILE, PAGE, 0o600]]}
        replies = []

        def receive(code):
            while True:
                message = send_channel.recv()
                if message is None:
                    raise protocol.ChannelClosed('closed')
                replies.append(message[0])
                if message[0] == code:
                    return message[1]

        try:
            send_channel.send_json(protocol.HELLO, hello)
            receive(protocol.ACCEPT)
            script(send_channel, receive)
            send_channel._writer.close()
            while send_channel.recv() is not None:
                pass
        except protocol.ChannelClosed:
            pass
        finally:
            send_channel.close()
        thread.join(30)
        return outcome

    def test_page_data_before_the_basis_digests_is_refused(self):
        release = threading.Event()
        original = receiver_module.hash_file

        def slow_hash_file(*args, **kwargs):
            release.wait(30)
            return original(*args, **kwargs)

        def script(channel, receive):
            channel.send(protocol.PAGE_DATA, protocol.page_data_head(0, 0), bytes(PAGE))
            channel.send(protocol.FILE_DONE, protocol.encode_file_done(0, 8 * PAGE, bytes(16)))

        with mock.patch.object(receiver_module, 'hash_file', slow_hash_file):
            try:
                outcome = self.session(script)
            finally:
                release.set()
        self.assertIsInstance(outcome.receiver_error, ProtocolError)
        self.assertIn('before the basis digests', str(outcome.receiver_error))
        self.assertEqual((self.basis / 'base/f').read_bytes(), self.old)  # never opened for writing
        settle(self)

    def test_leaf_request_after_the_file_was_written_is_refused(self):
        def script(channel, receive):
            receive(protocol.BASIS_NODES)
            channel.send(protocol.PAGE_DATA, protocol.page_data_head(0, 0), bytes(PAGE))
            channel.send(protocol.LEAF_REQUEST, protocol.encode_leaf_request(0, [1]))

        outcome = self.session(script)
        self.assertIsInstance(outcome.receiver_error, ProtocolError)
        self.assertIn('after it was written in place', str(outcome.receiver_error))
        settle(self)

    def test_the_sender_never_trips_the_guard(self):
        # Every in-place run above passes the guard; here many files are planned ahead of the one written.
        files = {f'base/{number:03d}': random.Random(number).randbytes((number % 5 + 1) * PAGE)
                 for number in range(40)}
        write_tree(self.basis, dict(files, **{'PG_VERSION': b'17\n', CONTROL_FILE: bytes(PAGE)}))
        changed = {path: data[:-1] + bytes([data[-1] ^ 1]) for path, data in files.items()}
        write_tree(self.source, dict(changed, **{'PG_VERSION': b'17\n', CONTROL_FILE: bytes(PAGE)}))
        for jobs in (1, 3):
            outcome = transfer(self.source, self.basis, None, jobs=jobs, receiver_jobs=jobs, window=8,
                               receiver_options={'in_place': True})
            with self.subTest(jobs=jobs):
                self.assert_refreshed(outcome)
        settle(self)


class VerifyScopeTests(InPlaceTestCase):
    """--verify-scope written: each file verified from B's digests and the pages this run wrote, not a re-read."""

    def options(self, config=None, **extra):
        options = in_place(config) if config else {'receiver_options': {'in_place': True}}
        options['receiver_options'] = dict(options['receiver_options'], verify_scope='written', **extra)
        return options

    def test_every_shape_and_engine_is_refreshed_and_verified(self):
        for name, config in CONFIGS.items():
            with self.subTest(config=name):
                shutil.rmtree(self.root / 'S', ignore_errors=True)
                shutil.rmtree(self.root / 'B', ignore_errors=True)
                pg_shapes(self.source, self.basis)
                outcome = transfer(self.source, self.basis, None, **self.options(config))
                self.assert_refreshed(outcome)
                self.assertEqual(outcome.receiver['verify_scope'], 'written')
                counts = outcome.receiver['verify_written']
                self.assertGreater(counts['nodes_kept'], 0)  # base/5/multi's nodes are unchanged
                self.assertGreater(counts['nodes_composed'], 0)  # one written page in a 4-page node
                self.assertGreater(counts['nodes_reread'], 0)  # more than one, or a new or grown file's
                self.assertLessEqual(counts['pages_reread'], outcome.sender['pages']['sent'])
                self.assertEqual(outcome.receiver['files_verified'], outcome.receiver['files'])
                settle(self)

    def test_both_scopes_give_the_same_copy(self):
        pg_shapes(self.source, self.basis)
        shutil.copytree(self.basis, self.root / 'B2', symlinks=True)
        written = transfer(self.source, self.basis, None, **self.options())
        full = transfer(self.source, self.root / 'B2', None, receiver_options={'in_place': True})
        self.assert_refreshed(written)
        self.assert_refreshed(full, self.root / 'B2')
        self.assertEqual(snapshot(self.basis), snapshot(self.root / 'B2'))  # files_digest is per session key
        self.assertNotIn('verify_scope', full.receiver)  # the default leaves the statistics as they were
        settle(self)

    def test_a_page_written_at_the_wrong_offset_fails_the_verification(self):
        # The page meant for one place lands one page further on: the page it was meant for keeps B's bytes.
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

    def test_a_file_whose_length_ends_wrong_fails_the_verification(self):
        pg_shapes(self.source, self.basis)
        original = receiver_module._Receiver._verify_written

        def truncate_first(self_, index, entry, size, digest, *rest):
            if size > PAGE:
                os.truncate(self_._output_path(entry.path), size - 1)
            return original(self_, index, entry, size, digest, *rest)

        with mock.patch.object(receiver_module._Receiver, '_verify_written', truncate_first):
            outcome = transfer(self.source, self.basis, None, **self.options())
        self.assertIsInstance(outcome.receiver_error, (VerificationError, TransferError))
        self.assertTrue(os.path.lexists(self.basis / MARKER))
        settle(self)

    def test_the_misplaced_write_switch_is_caught_by_either_scope(self):
        # EXP-32's negative control: PGDELTA_TEST_MISPLACE_WRITE=1 writes one page run a page away.
        for scope in ('written', 'all'):
            with self.subTest(scope=scope):
                shutil.rmtree(self.root / 'S', ignore_errors=True)
                shutil.rmtree(self.root / 'B', ignore_errors=True)
                pg_shapes(self.source, self.basis)
                with mock.patch.dict(os.environ, {receiver_module.MISPLACE_ENV: '1'}):
                    outcome = transfer(self.source, self.basis, None,
                                       receiver_options={'in_place': True, 'verify_scope': scope})
                self.assertIsInstance(outcome.receiver_error, VerificationError)
                self.assertTrue(os.path.lexists(self.basis / MARKER))
                self.assertFalse(os.path.lexists(self.basis / CONTROL_FILE))  # blocked from starting
                rerun = transfer(self.source, self.basis, None, **self.options())
                self.assert_refreshed(rerun)
                settle(self)

    def test_written_scope_needs_in_place(self):
        pg_shapes(self.source, self.basis)
        before = snapshot(self.basis)
        outcome = transfer(self.source, self.basis, self.root / 'O', receiver_options={'verify_scope': 'written'})
        self.assertIsInstance(outcome.receiver_error, InvalidInput)
        self.assertIn('--verify-scope written needs --in-place', str(outcome.receiver_error))
        self.assertEqual(snapshot(self.basis), before)
        settle(self)


class CliTests(InPlaceTestCase):
    def test_local_in_place_refreshes_b_and_says_so(self):
        pg_shapes(self.source, self.basis)
        stats_path = self.root / 'stats.json'
        result = run('local', '--source', self.source, '--basis', self.basis, '--in-place', '--reuse-any-basis',
                     '--fsync', 'worker', '--stats-json', stats_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot(self.basis), snapshot(self.source))
        self.assertIn('complete (reuse mode, in place', result.stderr)
        self.assertIn(f'committed {self.basis} in place', result.stderr)
        stats = json.loads(stats_path.read_text())
        self.assertTrue(stats['in_place'])
        self.assertTrue(stats['receiver']['in_place'])
        self.assertEqual(stats['receiver']['fsync'], 'worker')
        self.assertEqual(stats['switches']['receiver']['rebuild'], 'in-place')
        self.assertEqual(json.loads(receipt_path(self.basis).read_text())['status'], 'committed')
        # EXP-35's registered consequence: in place, the command line verifies only what the run wrote.
        self.assertEqual(stats['receiver']['verify_scope'], 'written')

    def test_the_command_lines_verify_scope_is_written_in_place_and_all_otherwise(self):
        for in_place, expected in ((True, 'written'), (False, 'all')):
            with self.subTest(in_place=in_place):
                args = cli.build_parser().parse_args(
                    ['local', '--source', 'S', '--basis', 'B', *(['--in-place'] if in_place else ['--output', 'O'])])
                self.assertEqual(cli._verify_scope(args), expected)
        args = cli.build_parser().parse_args(['local', '--source', 'S', '--basis', 'B', '--in-place',
                                              '--verify-scope', 'all'])
        self.assertEqual(cli._verify_scope(args), 'all')

    def test_tcp_receive_in_place(self):
        pg_shapes(self.source, self.basis)
        receiver = subprocess.Popen(
            python_command('receive', '--basis', self.basis, '--in-place', '--listen', '127.0.0.1:0',
                           '--reuse-any-basis', '--jobs', '2', '--fsync', 'writeback'),
            stderr=subprocess.PIPE, text=True, cwd=REPO_ROOT, env=subprocess_env())
        self.addCleanup(receiver.stderr.close)
        self.addCleanup(lambda: receiver.poll() is None and receiver.kill())
        port = receiver.stderr.readline().rsplit(':', 1)[1].strip()
        stats_path = self.root / 'stats.json'
        result = run('send', '--source', self.source, '--connect', f'127.0.0.1:{port}', '--stats-json', stats_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(receiver.wait(timeout=60), 0)
        self.assertIn(f'committed {self.basis} in place', receiver.stderr.read())
        self.assertEqual(snapshot(self.basis), snapshot(self.source))
        stats = json.loads(stats_path.read_text())
        self.assertTrue(stats['in_place'])
        self.assertEqual(stats['receiver']['fsync'], 'writeback')
        self.assertEqual(stats['mode'], 'reuse')
        self.assertLess(stats['pages']['sent'], stats['pages']['total'])

    def test_output_and_in_place_exclude_each_other(self):
        self.small_trees()
        for command in (('local', '--source', self.source, '--basis', self.basis),
                        ('receive', '--basis', self.basis, '--stdio')):
            with self.subTest(command=command[0]):
                result = run(*command, '--in-place', '--output', self.root / 'O')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('not allowed with argument', result.stderr)
                self.assertNotEqual(run(*command).returncode, 0)
        self.assertFalse(os.path.lexists(self.basis / MARKER))

    def test_a_failure_after_the_commit_does_not_say_b_is_blocked(self):
        # The commit rename happened, then removing the marker failed: B is complete, verified and startable.
        pg_shapes(self.source, self.basis)
        script = textwrap.dedent("""
            import errno, sys
            from pgdelta import cli, inplace
            def broken(basis):
                raise OSError(errno.EIO, 'Input/output error (injected)')
            inplace.remove_marker = broken
            sys.exit(cli.main(sys.argv[1:]))
        """)
        process = subprocess.Popen([sys.executable, '-c', script, 'receive', '--stdio', '--basis', str(self.basis),
                                    '--in-place', '--reuse-any-basis'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=REPO_ROOT, env=subprocess_env())
        channel = Channel(process.stdout, process.stdin)
        try:
            with self.assertRaises(PeerError) as caught:
                run_sender(channel, self.source, node_blocks=4)
        finally:
            channel.close()
        errors = process.stderr.read().decode()
        process.stderr.close()
        self.assertEqual(process.wait(timeout=60), 3, errors)
        self.assertIn('was committed in place', errors)
        self.assertNotIn('blocked from starting', errors)
        self.assertNotIn('blocked from starting', str(caught.exception))
        self.assertTrue(os.path.isfile(self.basis / CONTROL_FILE))
        self.assertTrue(os.path.isfile(self.basis / MARKER))

    def sender_log(self, exc):
        """What the sender's command logs when its run fails with `exc` after an in-place ACCEPT."""
        exc.pgdelta_stats = {'status': 'failed', 'in_place': True}

        def fail():
            raise exc

        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(type(exc)):
            cli._run_sender_command(argparse.Namespace(stats_json=None), fail, {})
        return errors.getvalue()

    def test_the_sender_warns_that_b_may_be_blocked_only_when_it_may_be(self):
        warning = 'may be incomplete and blocked'
        self.assertIn(warning, self.sender_log(protocol.ChannelClosed('the receiver went away')))
        committed = PeerError('receiver: B was committed in place, but the commit could not be made durable', 3)
        self.assertNotIn(warning, self.sender_log(committed))
        blocked = PeerError(f'receiver: verification failed; {inplace_module.incomplete_note("B")}', 4)
        self.assertNotIn(warning, self.sender_log(blocked))

    def test_failed_local_run_exits_with_its_code_and_says_b_is_incomplete(self):
        pg_shapes(self.source, self.basis)
        script = textwrap.dedent("""
            import sys
            from pgdelta import cli, protocol
            original = protocol.Channel.send
            corrupted = []
            def send(self, code, *parts):
                if code == protocol.PAGE_DATA and not corrupted:
                    data = bytearray(parts[-1])
                    data[0] ^= 1
                    parts = parts[:-1] + (bytes(data),)
                    corrupted.append(1)
                original(self, code, *parts)
            protocol.Channel.send = send
            sys.exit(cli.main(sys.argv[1:]))
        """)
        result = subprocess.run([sys.executable, '-c', script, 'local', '--source', str(self.source),
                                 '--basis', str(self.basis), '--in-place', '--reuse-any-basis'],
                                cwd=REPO_ROOT, env=subprocess_env(), capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn('is incomplete and blocked from starting', result.stderr)
        self.assert_blocked()
        rerun = run('local', '--source', self.source, '--basis', self.basis, '--in-place', '--reuse-any-basis')
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertEqual(snapshot(self.basis), snapshot(self.source))

    def test_basis_inside_the_source_is_refused(self):
        self.small_trees()
        inner = self.source / 'inner'
        write_tree(inner, {'f': b'x'})
        result = run('local', '--source', self.source, '--basis', inner, '--in-place')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('overlap', result.stderr)


# Run in a child receiver: SIGKILL itself at a chosen call. argv: owner, attribute, when, count, then the CLI.
KILL_SCRIPT = textwrap.dedent("""
    import os, signal, sys
    from pgdelta import cli, inplace, receiver
    owner_name, attribute, when, count = sys.argv[1:5]
    del sys.argv[1:5]
    owner = {'receiver': receiver._Receiver, 'inplace': inplace}[owner_name]
    original, calls = getattr(owner, attribute), [0]
    def patched(*args, **kwargs):
        calls[0] += 1
        if when == 'before' and calls[0] == int(count):
            os.kill(os.getpid(), signal.SIGKILL)
        result = original(*args, **kwargs)
        if when == 'after' and calls[0] == int(count):
            os.kill(os.getpid(), signal.SIGKILL)
        return result
    setattr(owner, attribute, patched)
    sys.exit(cli.main(sys.argv[1:]))
""")


@unittest.skipUnless(available(), SKIP_REASON)
class RealClusterTests(unittest.TestCase):
    QUERY = 'select count(*), sum(length(v)) from t'

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.servers = Servers(cls.root)
        cls.source, cls.stale = cls.root / 'S', cls.root / 'stale'
        initdb(cls.source)
        cls.servers.run_sql(cls.source,
                            'create table t as select g as id, md5(g::text) as v from generate_series(1, 20000) g',
                            'create table dropped as select g from generate_series(1, 5000) g', 'checkpoint')
        shutil.copytree(cls.source, cls.stale)
        cls.expected = cls.servers.run_sql(
            cls.source, "update t set v = v || '!' where id % 97 = 0",
            'insert into t select g, md5(g::text) from generate_series(20001, 21000) g',
            'create index on t (id)', 'drop table dropped', cls.QUERY)
        cls.files = sum(1 for path in cls.source.rglob('*') if path.is_file())

    @classmethod
    def tearDownClass(cls):
        cls.servers.close()
        cls.temp.cleanup()

    def fresh_basis(self, name):
        basis = self.root / name
        shutil.copytree(self.stale, basis)
        return basis

    def refresh(self, basis, *extra):
        return run('local', '--source', self.source, '--basis', basis, '--in-place',
                   '--pg-controldata', CONTROLDATA, *extra)

    def assert_identical_and_starts(self, basis):
        self.assertEqual(snapshot(basis), snapshot(self.source))
        self.assertEqual(self.servers.run_sql(basis, self.QUERY), self.expected)

    def test_in_place_refresh_is_byte_identical_to_s_and_starts(self):
        basis = self.fresh_basis('B-plain')
        stats_path = self.root / 'plain.json'
        result = self.refresh(basis, '--stats-json', stats_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        stats = json.loads(stats_path.read_text())
        self.assertEqual((stats['mode'], stats['in_place']), ('reuse', True))
        self.assertLess(stats['pages']['sent'], stats['pages']['total'] // 2)
        self.assertGreater(stats['receiver']['in_place_tree']['files_removed'], 0)  # the dropped table
        self.assert_identical_and_starts(basis)

    def kill_receiver(self, basis, owner, attribute, when, count, *extra):
        """Refresh `basis` in place with a receiver that SIGKILLs itself; return its exit status."""
        with open(self.root / f'{basis.name}.err', 'wb') as errors:
            process = subprocess.Popen(
                [sys.executable, '-c', KILL_SCRIPT, owner, attribute, when, str(count), 'receive', '--stdio',
                 '--basis', str(basis), '--in-place', '--pg-controldata', str(CONTROLDATA), '--jobs', '2', *extra],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, cwd=REPO_ROOT, env=subprocess_env())
        channel = Channel(process.stdout, process.stdin)
        try:
            with self.assertRaises(PgDeltaError):
                run_sender(channel, self.source, identity=read_identity(self.source, CONTROLDATA), node_blocks=128)
        finally:
            channel.close()
        return process.wait(timeout=60)

    def test_receiver_killed_anywhere_leaves_b_unstartable_until_a_rerun_completes_it(self):
        points = {
            'after blocking B': ('receiver', '_begin_in_place', 'after', 1, ()),
            'in the middle of the pages': ('receiver', '_page_data', 'after', 2, ()),
            'halfway through the files': ('receiver', '_file_done', 'after', self.files // 2, ('--fsync', 'worker')),
            'before the commit': ('receiver', '_commit', 'before', 1, ()),
        }
        for label, (owner, attribute, when, count, extra) in points.items():
            with self.subTest(killed=label):
                basis = self.fresh_basis(f'B-{attribute}')
                self.assertEqual(self.kill_receiver(basis, owner, attribute, when, count, *extra), -signal.SIGKILL)
                self.assertTrue(os.path.isfile(basis / MARKER))
                self.assertFalse(os.path.lexists(basis / CONTROL_FILE))
                started = self.servers.pg_ctl(basis, 'start', check=False)
                self.assertNotEqual(started.returncode, 0, 'PostgreSQL started on a blocked copy')
                self.assertFalse(os.path.lexists(basis / 'postmaster.pid'))
                rerun = self.refresh(basis)
                self.assertEqual(rerun.returncode, 0, rerun.stderr)
                self.assertFalse(os.path.lexists(basis / MARKER))
                self.assert_identical_and_starts(basis)

    def test_receiver_killed_right_after_the_commit_rename_is_complete_and_a_rerun_tidies_it(self):
        basis = self.fresh_basis('B-committed')
        self.assertEqual(self.kill_receiver(basis, 'inplace', 'commit_control', 'after', 1), -signal.SIGKILL)
        self.assertTrue(os.path.isfile(basis / MARKER))
        self.assertTrue(os.path.isfile(basis / CONTROL_FILE))
        rest = snapshot(basis)
        del rest[MARKER]
        self.assertEqual(rest, snapshot(self.source))  # already the refreshed copy
        rerun = self.refresh(basis)
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assert_identical_and_starts(basis)


if __name__ == '__main__':
    unittest.main()
