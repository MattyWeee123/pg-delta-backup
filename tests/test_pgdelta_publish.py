"""Publication: the completion receipt beside O, and failures before and after the rename."""
import errno
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from pgdelta import protocol
from pgdelta.errors import InvalidInput, PeerError, TransferError
from pgdelta.protocol import Channel, ChannelClosed
from pgdelta.receiver import receipt_path
from pgdelta.sender import run_sender

from pgdelta_support import (PAGE, REPO_ROOT, Outcome, channel_pair, python_command, run_receiver_thread,
                             snapshot, staging_dirs, subprocess_env, transfer, write_tree)


class LostAcknowledgementChannel(Channel):
    """Receiver side: the sender is gone by the time the final COMPLETE is sent."""

    def send(self, code, *parts):
        if code == protocol.COMPLETE:
            raise ChannelClosed('cannot send COMPLETE: [Errno 32] Broken pipe')
        super().send(code, *parts)


class PublishTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.basis, self.output = self.root / 'S', self.root / 'B', self.root / 'O'
        rng = random.Random(80)
        old = rng.randbytes(9 * PAGE)
        new = bytearray(old)
        new[4 * PAGE] ^= 1
        write_tree(self.source, {'base/1/rel': bytes(new), 'base/1/new': rng.randbytes(PAGE), 'empty': b''},
                   ['pg_wal'])
        write_tree(self.basis, {'base/1/rel': old})
        self.basis_before = snapshot(self.basis)
        self.receipt = receipt_path(self.output)

    def read_receipt(self):
        return json.loads(self.receipt.read_text())

    def assert_published(self):
        self.assertEqual(snapshot(self.output), snapshot(self.source))
        self.assertEqual(snapshot(self.basis), self.basis_before)
        self.assertEqual(staging_dirs(self.output), [])

    def assert_nothing_published(self):
        self.assertFalse(os.path.lexists(self.output))
        self.assertFalse(os.path.lexists(self.receipt))
        self.assertEqual(staging_dirs(self.output), [])
        self.assertEqual(snapshot(self.basis), self.basis_before)


class ReceiptTests(PublishTestCase):
    def test_success_leaves_a_published_receipt_beside_o(self):
        outcome = transfer(self.source, self.basis, self.output)
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assert_published()
        receipt = self.read_receipt()
        self.assertEqual(receipt['schema_version'], 1)
        self.assertEqual(receipt['status'], 'published')
        self.assertEqual((receipt['files'], receipt['directories']), (3, 3))
        self.assertEqual(receipt['bytes'], 10 * PAGE)
        self.assertEqual(Path(receipt['output']), self.output.absolute())
        self.assertIsNotNone(receipt['verified_at'])
        self.assertIsNotNone(receipt['published_at'])
        self.assertRegex(receipt['files_digest'], r'^[0-9a-f]{64}$')
        # The sender computes the same combined digest, so its statistics can be matched to the receipt.
        self.assertEqual(outcome.sender['files_digest'], receipt['files_digest'])
        self.assertEqual(outcome.receiver['files_digest'], receipt['files_digest'])
        self.assertEqual(outcome.receiver['receipt'], str(self.receipt))
        self.assertTrue(outcome.receiver['acknowledged'])

    def test_files_digest_depends_on_contents(self):
        first = transfer(self.source, self.basis, self.output)
        (self.source / 'empty').write_bytes(b'x')
        second = transfer(self.source, self.basis, self.root / 'O2')
        self.assertNotEqual(first.sender['files_digest'], second.sender['files_digest'])

    def test_receipt_is_written_as_verified_before_the_rename(self):
        real_rename, seen = os.rename, {}

        def spy(source, target, *args, **kwargs):
            if Path(target) == self.output:
                seen['receipt'] = self.read_receipt()
                seen['output_existed'] = os.path.lexists(self.output)
            return real_rename(source, target, *args, **kwargs)

        with mock.patch('pgdelta.receiver.os.rename', side_effect=spy):
            outcome = transfer(self.source, self.basis, self.output)
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assertEqual(seen['receipt']['status'], 'verified')
        self.assertIsNone(seen['receipt']['published_at'])
        self.assertFalse(seen['output_existed'])
        self.assertEqual(self.read_receipt()['status'], 'published')

    def test_stale_receipt_without_an_output_is_replaced(self):
        self.receipt.write_text('{"status": "published", "stale": true}\n')
        outcome = transfer(self.source, self.basis, self.output)
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        receipt = self.read_receipt()
        self.assertEqual(receipt['status'], 'published')
        self.assertNotIn('stale', receipt)

    def test_receipt_path_that_is_a_directory_is_refused(self):
        self.receipt.mkdir()
        outcome = transfer(self.source, self.basis, self.output)
        self.assertIsInstance(outcome.receiver_error, InvalidInput)
        self.assertIn('receipt', str(outcome.receiver_error))
        self.assertEqual(outcome.sender_error.exit_code, 2)
        self.assertTrue(self.receipt.is_dir())
        self.assertFalse(os.path.lexists(self.output))

    def test_failure_before_the_rename_removes_staging_and_receipt(self):
        real_rename = os.rename

        def failing(source, target, *args, **kwargs):
            if Path(target) == self.output:
                raise OSError(errno.EIO, 'Input/output error')
            return real_rename(source, target, *args, **kwargs)

        with mock.patch('pgdelta.receiver.os.rename', side_effect=failing):
            outcome = transfer(self.source, self.basis, self.output)
        self.assertIsInstance(outcome.receiver_error, TransferError)
        self.assertEqual(outcome.sender_error.exit_code, 3)
        self.assert_nothing_published()


class AfterTheRenameTests(PublishTestCase):
    def test_sender_gone_at_the_acknowledgement_still_counts_as_published(self):
        send_channel, receive_channel = channel_pair(receiver_class=LostAcknowledgementChannel)
        outcome = Outcome()
        thread = run_receiver_thread(receive_channel, outcome, self.basis, self.output, reuse_any_basis=True)
        try:
            outcome.sender = run_sender(send_channel, self.source, node_blocks=4)
        except BaseException as exc:
            outcome.sender_error = exc
        finally:
            send_channel.close()
        thread.join(60)
        self.assertIsNone(outcome.receiver_error, repr(outcome.receiver_error))
        stats = outcome.receiver
        self.assertTrue(stats['published'])
        self.assertFalse(stats['acknowledged'])
        self.assertIn('Broken pipe', stats['acknowledgement_error'])
        self.assert_published()
        self.assertEqual(self.read_receipt()['status'], 'published')
        # The sender cannot know: it fails, and says where to look.
        self.assertIsInstance(outcome.sender_error, ChannelClosed)
        self.assertIn('receipt', str(outcome.sender_error))
        self.assertEqual(outcome.sender_error.pgdelta_stats['files_digest'], self.read_receipt()['files_digest'])

    def test_interrupt_right_after_the_rename_waits_until_the_publication_is_durable(self):
        import pgdelta.receiver as receiver_module
        real_fsync_dir, interrupts = receiver_module.fsync_dir, []

        def interrupted_once(path):
            if Path(path) == self.output.parent and os.path.lexists(self.output) and not interrupts:
                interrupts.append(path)
                raise KeyboardInterrupt
            return real_fsync_dir(path)

        with mock.patch('pgdelta.receiver.fsync_dir', side_effect=interrupted_once):
            outcome = transfer(self.source, self.basis, self.output)
        self.assertEqual(len(interrupts), 1)
        self.assertIsNone(outcome.receiver_error, repr(outcome.receiver_error))
        self.assertFalse(outcome.receiver['acknowledged'])
        self.assertEqual(outcome.receiver['acknowledgement_error'], 'interrupted')
        self.assert_published()
        self.assertEqual(self.read_receipt()['status'], 'published')
        self.assertIsInstance(outcome.sender_error, ChannelClosed)

    def test_publication_that_cannot_be_made_durable_fails_but_keeps_o(self):
        import pgdelta.receiver as receiver_module
        real_fsync_dir = receiver_module.fsync_dir

        def failing(path):
            if Path(path) == self.output.parent and os.path.lexists(self.output):
                raise OSError(errno.EIO, 'Input/output error')
            return real_fsync_dir(path)

        with mock.patch('pgdelta.receiver.fsync_dir', side_effect=failing):
            outcome = transfer(self.source, self.basis, self.output)
        self.assertIsInstance(outcome.receiver_error, TransferError)
        self.assertIn('was published', str(outcome.receiver_error))
        self.assertIn('durable', str(outcome.receiver_error))
        self.assertIsInstance(outcome.sender_error, PeerError)
        self.assertEqual(outcome.sender_error.exit_code, 3)
        self.assert_published()  # a verified O is never deleted
        self.assertEqual(self.read_receipt()['status'], 'verified')


@unittest.skipIf(os.name == 'nt', 'needs POSIX signals')
class SignalAfterTheRenameTests(PublishTestCase):
    def test_sigterm_while_acknowledging_exits_0_with_a_distinct_message(self):
        script = textwrap.dedent("""
            import os, signal, sys
            from pgdelta import cli, protocol
            original = protocol.Channel.send
            def send(self, code, *parts):
                if code == protocol.COMPLETE:
                    os.kill(os.getpid(), signal.SIGTERM)
                original(self, code, *parts)
            protocol.Channel.send = send
            sys.exit(cli.main(sys.argv[1:]))
        """)
        receiver = subprocess.Popen(
            [sys.executable, '-c', script, 'receive', '--basis', str(self.basis), '--output', str(self.output),
             '--listen', '127.0.0.1:0', '--assume-same-cluster'],
            stderr=subprocess.PIPE, text=True, cwd=REPO_ROOT, env=subprocess_env())
        self.addCleanup(receiver.stderr.close)
        self.addCleanup(lambda: receiver.poll() is None and receiver.kill())
        port = receiver.stderr.readline().rsplit(':', 1)[1].strip()
        stats_path = self.root / 'stats.json'
        sent = subprocess.run(python_command('send', '--source', self.source, '--connect', f'127.0.0.1:{port}',
                                             '--node-blocks', '4', '--stats-json', stats_path),
                              capture_output=True, text=True, cwd=REPO_ROOT, env=subprocess_env(), timeout=60)
        self.assertEqual(receiver.wait(timeout=60), 0)
        log = receiver.stderr.read()
        self.assertIn('published but acknowledgement failed', log)
        self.assert_published()
        receipt = self.read_receipt()
        self.assertEqual(receipt['status'], 'published')
        self.assertEqual(sent.returncode, 3, sent.stderr)
        self.assertIn('receipt', sent.stderr)
        stats = json.loads(stats_path.read_text())
        self.assertEqual(stats['status'], 'failed')
        self.assertEqual(stats['files_digest'], receipt['files_digest'])


if __name__ == '__main__':
    unittest.main()
