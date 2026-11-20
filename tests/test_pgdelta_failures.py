"""Gate G1 shapes: corrupt data, unsafe paths and refused inputs never publish O or touch B."""
import base64
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest

from pgdelta import protocol
from pgdelta.errors import InvalidInput, PeerError, ProtocolError, UnsafePath, VerificationError
from pgdelta.protocol import Channel
from pgdelta.receiver import receipt_path
from pgdelta.sender import Inbox

from pgdelta_support import (PAGE, REPO_ROOT, Outcome, channel_pair, python_command, run_receiver_thread,
                             snapshot, staging_dirs, subprocess_env, transfer, write_tree)


class CorruptingChannel(Channel):
    """Flips one byte in the first PAGE_DATA payload, after the sender has hashed the file."""
    corrupted = 0

    def send(self, code, *parts):
        if code == protocol.PAGE_DATA and not self.corrupted:
            data = bytearray(parts[-1])
            data[len(data) // 2] ^= 0x01
            parts = parts[:-1] + (bytes(data),)
            self.corrupted += 1
        super().send(code, *parts)


class FailureTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.basis, self.output = self.root / 'S', self.root / 'B', self.root / 'O'
        rng = random.Random(40)
        old = rng.randbytes(12 * PAGE)
        new = bytearray(old)
        new[5 * PAGE] ^= 1
        write_tree(self.source, {'base/1/rel': bytes(new), 'base/1/added': rng.randbytes(3 * PAGE)})
        write_tree(self.basis, {'base/1/rel': old, 'stale': b'x'})
        self.basis_before = snapshot(self.basis)

    def assert_nothing_published(self):
        self.assertFalse(os.path.lexists(self.output))
        self.assertFalse(os.path.lexists(receipt_path(self.output)))
        self.assertEqual(staging_dirs(self.output), [])
        self.assertEqual(snapshot(self.basis), self.basis_before)

    def fake_sender(self, hello_changes=None, frames=()):
        """Speak raw protocol to a receiver thread; return (outcome, messages the receiver sent)."""
        send_channel, receive_channel = channel_pair()
        outcome = Outcome()
        thread = run_receiver_thread(receive_channel, outcome, self.basis, self.output,
                                     reuse_any_basis=True)
        hello = {'protocol_version': 1, 'block': PAGE, 'node_blocks': 4,
                 'key': base64.b64encode(bytes(32)).decode(), 'source_identity': None, 'root_mode': 0o700,
                 'directories': [['base', 0o700]], 'files': [['base/f', 10, 0o600]]}
        hello.update(hello_changes or {})
        replies = []
        try:
            send_channel.send_json(protocol.HELLO, hello)
            for code, payload in frames:
                send_channel.send(code, payload)
            send_channel._writer.close()  # end of stream, so a receiver that accepted everything stops
            while True:
                message = send_channel.recv()
                if message is None:
                    break
                replies.append(message)
        except protocol.ChannelClosed:
            pass
        finally:
            send_channel.close()
        thread.join(30)
        return outcome, replies


class CorruptionTests(FailureTestCase):
    def test_page_corrupted_in_transit_fails_verification(self):
        for assume in (True, False):  # reuse mode and full mode
            with self.subTest(reuse=assume):
                outcome = transfer(self.source, self.basis, self.output, sender_class=CorruptingChannel,
                                   reuse_any_basis=assume)
                self.assertIsInstance(outcome.receiver_error, VerificationError)
                self.assertIsInstance(outcome.sender_error, PeerError)
                self.assertEqual(outcome.sender_error.exit_code, 4)
                self.assertEqual(outcome.sender_error.pgdelta_stats['status'], 'failed')
                self.assert_nothing_published()

    def test_wrong_file_digest_fails_verification(self):
        outcome, replies = self.fake_sender(frames=[
            (protocol.PAGE_DATA, protocol.page_data_head(0, 0) + b'0123456789'),
            (protocol.FILE_DONE, protocol.encode_file_done(0, 10, bytes(16)))])
        self.assertIsInstance(outcome.receiver_error, VerificationError)
        self.assertEqual(protocol.decode_json(replies[-1][1], 'ERROR')['exit_code'], 4)
        self.assert_nothing_published()

    def test_unreadable_basis_file_fails_the_transfer(self):
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            self.skipTest('root can read any file')
        os.chmod(self.basis / 'base' / '1' / 'rel', 0)
        self.addCleanup(os.chmod, self.basis / 'base' / '1' / 'rel', 0o600)
        outcome = transfer(self.source, self.basis, self.output)
        self.assertEqual(outcome.sender_error.exit_code, 3)
        self.assertIn('cannot read basis file', str(outcome.sender_error))
        self.assertFalse(os.path.lexists(self.output))
        self.assertEqual(staging_dirs(self.output), [])


class DrainTests(unittest.TestCase):
    def test_sender_keeps_reading_after_a_receiver_error(self):
        # The receiver may still be writing (e.g. its basis thread) while it cleans up;
        # if the sender stopped reading, that writer would block on a full pipe.
        send_channel, receive_channel = channel_pair()
        inbox = Inbox(send_channel)
        inbox.start()
        receive_channel.send_json(protocol.ERROR, {'exit_code': 4, 'message': 'boom'})

        def flood():
            for _ in range(64):
                receive_channel.send(protocol.BASIS_NODES, protocol.encode_basis_nodes(0, 0, bytes(16 << 10)))
            receive_channel.close()
        writer = threading.Thread(target=flood)
        writer.start()
        writer.join(20)
        self.assertFalse(writer.is_alive(), 'receiver-side writer blocked')
        with self.assertRaises(PeerError) as caught:
            inbox.wait(lambda: None, 'anything')
        self.assertEqual(caught.exception.exit_code, 4)
        inbox.join(10)
        send_channel.close()


class PathSafetyTests(FailureTestCase):
    def test_path_traversal_in_hello_is_rejected(self):
        bad_inventories = [
            {'files': [['../evil', 4, 0o600]]},
            {'files': [['base/../../evil', 4, 0o600]]},
            {'files': [[str(self.root / 'evil'), 4, 0o600]]},
            {'files': [['base//f', 4, 0o600]]},
            {'files': [['base/./f', 4, 0o600]]},
            {'directories': [['..', 0o700]], 'files': []},
            {'directories': [['base', 0o700], ['base/..', 0o700]], 'files': []},
        ]
        for changes in bad_inventories:
            with self.subTest(changes=changes):
                outcome, replies = self.fake_sender(changes)
                self.assertIsInstance(outcome.receiver_error, UnsafePath)
                self.assertEqual([code for code, _ in replies], [protocol.ERROR])
                self.assertEqual(protocol.decode_json(replies[0][1], 'ERROR')['exit_code'], 2)
                self.assertFalse(os.path.lexists(self.root / 'evil'))
                self.assert_nothing_published()

    def test_malformed_hello_is_rejected(self):
        for changes in ({'protocol_version': 2}, {'block': 100}, {'node_blocks': 0}, {'key': 'c2hvcnQ='},
                        {'extra_field': 1}, {'files': [['base/f', 4, 0o600], ['base/f', 4, 0o600]]}):
            with self.subTest(changes=changes):
                outcome, replies = self.fake_sender(changes)
                self.assertIsNotNone(outcome.receiver_error)
                self.assertEqual([code for code, _ in replies], [protocol.ERROR])
                self.assert_nothing_published()

    def test_protocol_violations_after_accept_abort_cleanly(self):
        cases = [
            [(protocol.HELLO, b'{}')],                                                      # wrong message
            [(protocol.PAGE_DATA, protocol.page_data_head(0, 5) + b'x')],                    # beyond the file
            [(protocol.PAGE_DATA, protocol.page_data_head(0, 0) + b'x' * 11)],               # longer than file
            [(protocol.PAGE_DATA, protocol.page_data_head(0, 0) + b'x' * 10)] * 2,           # page sent twice
            [(protocol.PAGE_DATA, protocol.page_data_head(1, 0) + b'x')],                    # unknown file
            [(protocol.FILE_DONE, protocol.encode_file_done(0, 10, bytes(16)))],             # page never sent
            [(protocol.LEAF_REQUEST, protocol.encode_leaf_request(0, [0]))],                 # no basis file
            [(protocol.COMPLETE, b'{"files":1,"directories":1}')],                           # too early
        ]
        for frames in cases:
            with self.subTest(frames=frames):
                outcome, replies = self.fake_sender(frames=frames)
                self.assertIsInstance(outcome.receiver_error, ProtocolError)
                self.assertEqual(replies[0][0], protocol.ACCEPT)
                self.assertEqual(protocol.decode_json(replies[-1][1], 'ERROR')['exit_code'], 3)
                self.assert_nothing_published()


class RefusalTests(FailureTestCase):
    def test_existing_output_is_refused(self):
        self.output.mkdir()
        (self.output / 'keep').write_bytes(b'precious')
        outcome = transfer(self.source, self.basis, self.output)
        self.assertIsInstance(outcome.receiver_error, InvalidInput)
        self.assertEqual(outcome.sender_error.exit_code, 2)
        self.assertEqual((self.output / 'keep').read_bytes(), b'precious')
        self.assertEqual(staging_dirs(self.output), [])

    def test_output_inside_basis_is_refused(self):
        self.output = self.basis / 'O'
        outcome = transfer(self.source, self.basis, self.output)
        self.assertIn('overlaps', str(outcome.receiver_error))
        self.assertEqual(outcome.sender_error.exit_code, 2)
        self.assert_nothing_published()

    def test_running_basis_is_refused(self):
        (self.basis / 'postmaster.pid').write_text('12345\n')
        self.basis_before = snapshot(self.basis)
        outcome = transfer(self.source, self.basis, self.output)
        self.assertIn('postmaster.pid', str(outcome.receiver_error))
        self.assertEqual(outcome.sender_error.exit_code, 2)
        self.assert_nothing_published()

    def test_source_with_symlink_or_tablespace_is_refused(self):
        (self.source / 'pg_tblspc').mkdir()
        (self.source / 'pg_tblspc' / '16384').symlink_to(self.root)
        outcome = transfer(self.source, self.basis, self.output)
        self.assertIsInstance(outcome.sender_error, InvalidInput)
        self.assertIn('tablespaces', str(outcome.sender_error))
        self.assertIsInstance(outcome.receiver_error, PeerError)
        self.assertEqual(outcome.receiver_error.exit_code, 2)
        self.assert_nothing_published()

    def test_basis_with_symlink_is_refused(self):
        (self.basis / 'pg_wal').symlink_to(self.root)
        self.basis_before = snapshot(self.basis)
        outcome = transfer(self.source, self.basis, self.output)
        self.assertIn('symlinks', str(outcome.receiver_error))
        self.assert_nothing_published()


@unittest.skipIf(os.name == 'nt', 'needs POSIX signals')
class KilledSenderTests(FailureTestCase):
    def test_killed_sender_leaves_no_output_and_no_staging(self):
        receiver = subprocess.Popen(
            python_command('receive', '--basis', self.basis, '--output', self.output,
                           '--listen', '127.0.0.1:0', '--assume-same-cluster'),
            stderr=subprocess.PIPE, text=True, cwd=REPO_ROOT, env=subprocess_env())
        self.addCleanup(receiver.stderr.close)
        self.addCleanup(lambda: receiver.poll() is None and receiver.kill())
        line = receiver.stderr.readline()
        self.assertIn('listening on 127.0.0.1:', line)
        port = int(line.rsplit(':', 1)[1])
        script = textwrap.dedent(f"""
            import os, signal, sys
            from pgdelta import protocol, sender, transport
            original = protocol.Channel.send
            def send(self, code, *parts):
                original(self, code, *parts)
                if code == protocol.PAGE_DATA:
                    os.kill(os.getpid(), signal.SIGKILL)
            protocol.Channel.send = send
            channel = transport.connect_channel('127.0.0.1', {port})
            sender.run_sender(channel, sys.argv[1], node_blocks=4)
        """)
        killed = subprocess.run([sys.executable, '-c', script, str(self.source)], cwd=REPO_ROOT,
                                env=subprocess_env(), capture_output=True, timeout=60)
        self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stderr)
        self.assertEqual(receiver.wait(timeout=60), 3)
        self.assertIn('connection', receiver.stderr.read())
        self.assert_nothing_published()

    def test_interrupted_local_run_cleans_up(self):
        # SIGTERM to the sender (parent) while the receiver child waits mid-transfer.
        big = random.Random(41).randbytes(64 * PAGE)
        write_tree(self.source, {'big': big})
        script = textwrap.dedent("""
            import os, signal, sys, time
            from pgdelta import cli, protocol
            original = protocol.Channel.send
            def send(self, code, *parts):
                original(self, code, *parts)
                if code == protocol.PAGE_DATA:
                    os.kill(os.getpid(), signal.SIGTERM)
                    time.sleep(5)
            protocol.Channel.send = send
            sys.exit(cli.main(sys.argv[1:]))
        """)
        result = subprocess.run([sys.executable, '-c', script, 'local', '--source', str(self.source),
                                 '--basis', str(self.basis), '--output', str(self.output)],
                                cwd=REPO_ROOT, env=subprocess_env(), capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('interrupted', result.stderr)
        self.assert_nothing_published()


if __name__ == '__main__':
    unittest.main()
