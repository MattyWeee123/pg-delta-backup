"""Tests for the streaming engine: the receiver volunteers B's digests, the sender copies runs.

Nothing is asked for here. The receiver hashes B on a thread of its own and streams BASIS_DIGESTS
without being asked; the sender drains them on a reader thread while its main thread hashes S,
compares locally, and streams CHUNK_COPY runs plus CHUNK_DATA for what differs.
"""
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

# Both the repo root (for `pgdelta`) and this file's own directory (for `test_pgdelta`). The second
# matters because CI runs `python -m unittest discover -s tests`, so test modules are imported by
# their bare name; without `tests/` on sys.path, `from test_pgdelta import ...` would import a
# second, separate copy of that module rather than resolving to the one already imported.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pgdelta import protocol, receiver as receiver_module  # noqa: E402
from pgdelta.hashing import new_key  # noqa: E402
from pgdelta.protocol import Channel  # noqa: E402
from pgdelta.receiver import run_receiver  # noqa: E402
from pgdelta.sender import run_sender  # noqa: E402
from pgdelta.tree import Entry  # noqa: E402
from test_pgdelta import CHUNK, RefreshFixture, tree_bytes, write  # noqa: E402


def run_with_timeout(case, refresh, seconds, *extra):
    """Run `refresh(*extra)` on a thread and fail if it does not finish. A deadlocked engine shows up
    as a thread that never returns, so the assertion is on the thread, not on an exception."""
    outcome = {}

    def go():
        try:
            outcome['code'] = refresh(*extra)[0]
        except BaseException as exc:
            outcome['error'] = exc

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    thread.join(timeout=seconds)
    case.assertFalse(thread.is_alive(), 'the refresh never finished: both sides are deadlocked')
    case.assertNotIn('error', outcome, f"refresh raised: {outcome.get('error')!r}")
    case.assertEqual(outcome['code'], 0)


class DigestFrames(unittest.TestCase):
    def test_round_trips_index_first_chunk_and_digests(self):
        digests = [bytes([i]) * protocol.DIGEST_SIZE for i in range(3)]
        payload = protocol.BASIS_DIGESTS_HEAD.pack(7, 100, 3) + b''.join(digests)
        self.assertEqual(protocol.unpack_digest_frame(payload), (7, 100, digests))

    def test_a_payload_shorter_than_the_head_is_refused(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.unpack_digest_frame(b'\x00' * (protocol.BASIS_DIGESTS_HEAD.size - 1))

    def test_a_zero_count_is_refused(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.unpack_digest_frame(protocol.BASIS_DIGESTS_HEAD.pack(0, 0, 0))

    def test_a_body_that_is_not_a_whole_number_of_digests_is_refused(self):
        payload = protocol.BASIS_DIGESTS_HEAD.pack(0, 0, 2) + b'\x00' * (protocol.DIGEST_SIZE + 1)
        with self.assertRaises(protocol.ProtocolError):
            protocol.unpack_digest_frame(payload)

    def test_a_count_beyond_the_body_never_sizes_an_allocation(self):
        payload = protocol.BASIS_DIGESTS_HEAD.pack(0, 0, 2 ** 32 - 1) + b'\x00' * protocol.DIGEST_SIZE
        with self.assertRaises(protocol.ProtocolError):
            protocol.unpack_digest_frame(payload)

    def test_the_frame_cap_matches_max_payload(self):
        self.assertEqual(protocol.MAX_DIGESTS_PER_FRAME, 4194303)
        biggest = protocol.BASIS_DIGESTS_HEAD.size + protocol.MAX_DIGESTS_PER_FRAME * protocol.DIGEST_SIZE
        self.assertLessEqual(biggest, protocol.MAX_PAYLOAD)


class Streaming(RefreshFixture, unittest.TestCase):
    def test_no_chunk_is_asked_about(self):
        self.both('base/1/16384', os.urandom(5 * CHUNK))
        code, report = self.refresh()
        self.assertEqual(code, 0)
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        self.assertNotIn('CHUNK_HASHES', report['bytes_sent'])
        self.assertEqual(report['counts']['chunks_same'], 5)

    def test_an_unchanged_file_costs_one_copy_run(self):
        self.both('f', os.urandom(40 * CHUNK))
        code, report = self.refresh()
        self.assertEqual(report['counts']['copy_runs'], 1)
        self.assertEqual(report['counts']['chunks_same'], 40)
        self.assertNotIn('CHUNK_DATA', report['bytes_sent'])

    def test_copy_runs_break_around_changed_chunks(self):
        data = bytearray(os.urandom(10 * CHUNK))
        write(self.b, 'f', bytes(data))
        for i in (3, 7):
            data[i * CHUNK] ^= 0xFF
        write(self.s, 'f', bytes(data))
        code, report = self.refresh()
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        self.assertEqual(report['counts']['chunks_changed'], 2)
        self.assertEqual(report['counts']['copy_runs'], 3)   # 0-2, 4-6, 8-9

    def test_a_file_b_lacks_waits_for_nothing(self):
        write(self.s, 'new', os.urandom(3 * CHUNK))
        self.both('kept', os.urandom(CHUNK))
        code, report = self.refresh()
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        self.assertEqual(report['counts']['chunks_new'], 3)

    def test_a_shorter_basis_file_waits_only_for_the_range_b_has(self):
        base = os.urandom(2 * CHUNK)
        write(self.b, 'grew', base)
        write(self.s, 'grew', base + os.urandom(2 * CHUNK))
        code, report = self.refresh()
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        self.assertEqual(report['counts']['chunks_same'], 2)
        self.assertEqual(report['counts']['chunks_new'], 2)

    def test_round_trips_no_longer_scale_with_chunks(self):
        """The headline: 200 chunks, and the sender blocks a handful of times, not 200."""
        self.both('f', os.urandom(200 * CHUNK))
        code, report = self.refresh()
        self.assertLess(report['counts']['round_trips'], 10)

    def test_a_straddling_partial_chunk_is_new_not_compared(self):
        """B's size is not a multiple of the chunk size: the straddling chunk is sent, not compared."""
        base = os.urandom(2 * CHUNK + 100)
        write(self.b, 'f', base)
        write(self.s, 'f', base + os.urandom(CHUNK - 100))
        code, report = self.refresh()
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        self.assertEqual(report['counts']['chunks_asked'], 2)
        self.assertEqual(report['counts']['chunks_new'], 1)

    def test_a_shrunk_files_last_partial_chunk_still_copies(self):
        """S's last chunk is 5 bytes where B's whole chunk is 8 KiB. Two digests are only comparable
        over identical ranges, so B's digest covers the range S's chunk covers (protocol.comparable),
        not B's own chunking -- otherwise every shrunk file would resend its tail."""
        base = os.urandom(3 * CHUNK)
        write(self.b, 'shrank', base)
        write(self.s, 'shrank', base[:CHUNK + 5])
        code, report = self.refresh()
        self.assertEqual(code, 0)
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        self.assertEqual(report['counts']['chunks_same'], 2)
        self.assertEqual(report['counts']['copy_runs'], 1)
        self.assertNotIn('CHUNK_DATA', report['bytes_sent'])

    def test_an_empty_file_waits_for_no_digest(self):
        write(self.s, 'empty', b'')
        write(self.b, 'empty', b'')
        code, report = self.refresh()
        self.assertEqual(code, 0)
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        self.assertEqual(report['counts'].get('digest_waits', 0), 0)
        self.assertEqual(report['counts'].get('copy_runs', 0), 0)


class Deadlock(RefreshFixture, unittest.TestCase):
    """A basis big enough that the digest stream exceeds the socket buffer while the sender is still
    writing page data. A deadlocked engine fails these by timeout, not by assertion, so each runs on
    a thread and the assertion is that the thread finished."""

    def big_pair(self, chunks=3000, every=4):
        data = bytearray(os.urandom(chunks * CHUNK))
        write(self.b, 'f', bytes(data))
        for c in range(0, chunks, every):
            data[c * CHUNK] ^= 0xFF
        write(self.s, 'f', bytes(data))

    def over_tcp(self) -> tuple[int, dict]:
        """run_sender/run_receiver over a 127.0.0.1 socket, as OverTcp does, returning
        (exit-code-shaped status, the sender's report) so run_with_timeout can read it like
        cli.main's return value."""
        outcome = {}
        server = socket.create_server(('127.0.0.1', 0))
        port = server.getsockname()[1]

        def receive():
            try:
                conn, _ = server.accept()
                with conn:
                    outcome['receiver'] = run_receiver(Channel(protocol.tune(conn)), self.b, self.o)
            except BaseException as exc:
                outcome['error'] = exc

        thread = threading.Thread(target=receive, name='pgdelta-receiver-test', daemon=True)
        thread.start()
        try:
            with socket.create_connection(('127.0.0.1', port)) as sock:
                report = run_sender(Channel(protocol.tune(sock)), self.s, CHUNK)
        finally:
            thread.join(timeout=60)
            server.close()
        if 'error' in outcome:
            raise outcome['error']
        self.assertFalse(thread.is_alive(), 'the receiver never finished')
        return 0 if report['ok'] else 1, report

    # run_with_timeout is a MODULE-LEVEL helper in this file, not a method: Task 3's tiny-window
    # test needs the same thing, and a deadlock can only ever be detected as a timeout.

    def test_local_socketpair_completes_with_both_directions_busy(self):
        """`local` is the harsher case: socketpair buffers are tens of KB, not megabytes."""
        self.big_pair()
        run_with_timeout(self, self.refresh, 120)
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))

    def test_a_real_socket_completes_with_both_directions_busy(self):
        self.big_pair()
        run_with_timeout(self, self.over_tcp, 120)
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))

    def test_a_receiver_that_fails_mid_stream_hangs_nothing(self):
        """The four-way cycle this engine has to avoid. When the receiver fails it stops draining;
        the sender is then blocked writing page data at it; the sender's reader thread is stalled on
        a full digest window, which only the sender's main thread can free; and the receiver's
        generator is blocked writing digests that only that reader thread can take. All four wait on
        each other unless the receiver keeps draining while it shuts its generator down.

        A one-digest window and 2 KiB socket buffers stand in for the 32 GiB of basis it would
        otherwise take to fill the default window, and the failure lands at the first file's
        FILE_DONE, by which time the pipeline is in that steady state."""
        small = 512   # the minimum chunk size, so 1024 digests cover only 512 KiB of basis
        for name, chunks in (('a', 2000), ('b', 4000)):
            write(self.b, name, os.urandom(chunks * small))
            write(self.s, name, os.urandom(chunks * small))   # differs everywhere: both sides stay busy
        real = os.pwrite

        def corrupt(fd, chunk, offset):   # 'a' will not match its digest, so FILE_DONE('a') fails
            return real(fd, bytes([chunk[0] ^ 0x55]) + chunk[1:], offset)

        ours, theirs = socket.socketpair()
        for sock in (ours, theirs):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2048)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
        outcome = {}

        def receive():
            try:
                outcome['receiver'] = run_receiver(Channel(theirs), self.b, self.o)
            except BaseException as exc:
                outcome['receiver_error'] = exc
            finally:
                theirs.close()

        def send():
            try:
                outcome['sender'] = run_sender(Channel(ours), self.s, small, None, 16)
            except BaseException as exc:
                outcome['sender_error'] = exc
            finally:
                ours.close()

        threads = [threading.Thread(target=send, name='sender', daemon=True),
                   threading.Thread(target=receive, name='receiver', daemon=True)]
        with mock.patch.object(receiver_module.os, 'pwrite', corrupt):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=120)
                self.assertFalse(thread.is_alive(), f'the {thread.name} never finished: both sides are deadlocked')
        self.assertIsInstance(outcome.get('receiver_error'), receiver_module.VerificationError)
        # Which error the sender reports depends on which side noticed first, but it must fail.
        self.assertIn('sender_error', outcome)
        self.assertFalse(self.o.exists())
        self.assertFalse(any(p.name.endswith('.pgdelta-partial') for p in self.o.parent.iterdir()))


class StreamFailures(unittest.TestCase):
    """Each drives one real side over a socketpair and feeds it a hostile peer. The assertion is
    always: a legible error, within a timeout, and no O."""

    NAME = 'pgdata_16384'   # a top-level file, so it is entry 0, and distinctive enough to assert on

    def temp_root(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def drive_sender(self, peer, seconds=30) -> BaseException:
        """Run run_sender against `peer`, which stands in for the receiver on the other end of a
        socketpair. Asserts that run_sender finished and failed; returns what it raised.

        `peer` owns when its end goes away: this side is not closed until run_sender has finished,
        so a sender that keeps writing page data gets a buffer to write into rather than an EPIPE
        that would mask the digest failure under test. A peer that wants the sender to see the end
        of the digest stream closes or half-closes (`SHUT_WR`) its own socket."""
        root = self.temp_root()
        source = root / 'S'
        source.mkdir()
        write(source, self.NAME, os.urandom(4 * CHUNK))   # four chunks, as every peer below declares
        ours, theirs = socket.socketpair()
        outcome = {}

        def send():
            try:
                outcome['report'] = run_sender(Channel(ours), source, CHUNK)
            except BaseException as exc:
                outcome['error'] = exc
            finally:
                ours.close()

        thread = threading.Thread(target=send, name='pgdelta-sender-test', daemon=True)
        thread.start()
        try:
            peer(Channel(theirs))
        finally:
            thread.join(timeout=seconds)
            theirs.close()
        self.assertFalse(thread.is_alive(), 'run_sender never finished: it is waiting for a digest forever')
        self.assertIn('error', outcome, f'run_sender returned instead of failing: {outcome.get("report")}')
        return outcome['error']

    def drive_receiver(self, entries, basis: dict, peer, seconds=30) -> str:
        """HELLO a receiver with `entries`, having written `basis` into B, then let `peer` send its
        frames. Returns the ERROR message the receiver sent back, skipping the digests it volunteers."""
        root = self.temp_root()
        b, self.output = root / 'B', root / 'O'
        b.mkdir()
        for rel, data in basis.items():
            write(b, rel, data)
        ours, theirs = socket.socketpair()
        outcome = {}

        def serve():
            try:
                outcome['report'] = run_receiver(Channel(theirs), b, self.output)
            except BaseException as exc:
                outcome['error'] = exc
            finally:
                theirs.close()

        thread = threading.Thread(target=serve, name='pgdelta-receiver-test', daemon=True)
        thread.start()
        channel = Channel(ours)
        try:
            channel.send_json(protocol.HELLO, {
                'version': protocol.VERSION, 'chunk_size': CHUNK, 'key': new_key().hex(),
                'root_mode': 0o700, 'entries': [entry.to_json() for entry in entries]})
            channel.expect(protocol.ACCEPT)
            peer(channel)
            while True:
                kind, payload = channel.recv()
                if kind == protocol.ERROR:
                    return json.loads(payload)['message']
                self.assertEqual(kind, protocol.BASIS_DIGESTS, protocol.NAMES.get(kind, kind))
        finally:
            ours.close()
            thread.join(timeout=seconds)
            self.assertFalse(thread.is_alive(), 'run_receiver never finished')

    def test_a_dead_peer_raises_instead_of_hanging(self):
        """Review Focus 1 - the failure that would otherwise hang a user's refresh forever."""
        def peer(channel):
            channel.expect(protocol.HELLO)
            channel.send_json(protocol.ACCEPT, {'basis_sizes': [[0, 4 * CHUNK]]})
            channel.sock.close()   # and nothing at all behind it: no BASIS_DIGESTS, ever

        error = self.drive_sender(peer)
        self.assertIsInstance(error, (protocol.Closed, protocol.PeerError))

    def test_a_digest_for_an_undeclared_file_is_refused(self):
        def peer(channel):
            channel.expect(protocol.HELLO)
            channel.send_json(protocol.ACCEPT, {'basis_sizes': []})
            channel.send(protocol.BASIS_DIGESTS,
                         protocol.BASIS_DIGESTS_HEAD.pack(0, 0, 1) + b'\x00' * protocol.DIGEST_SIZE)

        error = self.drive_sender(peer)
        self.assertIsInstance(error, protocol.ProtocolError)
        self.assertIn('0', str(error))

    def test_a_digest_past_the_declared_basis_size_is_refused(self):
        def peer(channel):
            channel.expect(protocol.HELLO)
            channel.send_json(protocol.ACCEPT, {'basis_sizes': [[0, 2 * CHUNK]]})
            channel.send(protocol.BASIS_DIGESTS,
                         protocol.BASIS_DIGESTS_HEAD.pack(0, 0, 6) + b'\x00' * (6 * protocol.DIGEST_SIZE))

        error = self.drive_sender(peer)
        self.assertIsInstance(error, protocol.ProtocolError)
        self.assertIn('outside', str(error))

    def test_missing_digests_for_a_file_b_has_fail_legibly(self):
        """Review Focus 4 - digests for only half the range B declared."""
        def peer(channel):
            channel.expect(protocol.HELLO)
            channel.send_json(protocol.ACCEPT, {'basis_sizes': [[0, 4 * CHUNK]]})
            channel.send(protocol.BASIS_DIGESTS,
                         protocol.BASIS_DIGESTS_HEAD.pack(0, 0, 2) + b'\x00' * (2 * protocol.DIGEST_SIZE))
            # Half-close: the digest stream ends where chunk 2's digest should have been, while the
            # sender can still write the pages it decided to send for chunks 0 and 1.
            channel.sock.shutdown(socket.SHUT_WR)

        error = self.drive_sender(peer)
        self.assertIsInstance(error, protocol.Closed)
        self.assertIn(self.NAME, str(error))

    def test_a_copy_run_spanning_two_files_is_refused(self):
        data = os.urandom(2 * CHUNK)
        entries = [Entry('a', 'file', 0o600, size=2 * CHUNK), Entry('b', 'file', 0o600, size=2 * CHUNK)]

        def peer(channel):
            channel.send(protocol.CHUNK_COPY, protocol.CHUNK_COPY_BODY.pack(0, 0, 3))

        message = self.drive_receiver(entries, {'a': data, 'b': data}, peer)
        self.assertIn('past the end', message)
        self.assertFalse(self.output.exists())
        self.assertFalse(any(p.name.endswith('.pgdelta-partial') for p in self.output.parent.iterdir()))

    def test_a_copy_run_outside_bs_size_is_refused(self):
        entries = [Entry('f', 'file', 0o600, size=4 * CHUNK)]

        def peer(channel):
            channel.send(protocol.CHUNK_COPY, protocol.CHUNK_COPY_BODY.pack(0, 0, 4))

        message = self.drive_receiver(entries, {'f': os.urandom(2 * CHUNK)}, peer)
        self.assertIn("outside B's size", message)
        self.assertFalse(self.output.exists())
        self.assertFalse(any(p.name.endswith('.pgdelta-partial') for p in self.output.parent.iterdir()))


class VersionMismatch(unittest.TestCase):
    """A real sender/receiver version mismatch is reachable, and the peer must be told why, not left
    to see only a closed connection."""

    def test_the_protocol_version_is_three(self):
        self.assertEqual(protocol.VERSION, 3)

    def test_sender_side_sees_a_legible_version_mismatch(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        basis, output = root / 'B', root / 'O'
        basis.mkdir()
        ours, theirs = socket.socketpair()
        outcome = {}

        def serve():
            try:
                outcome['report'] = run_receiver(Channel(theirs), basis, output)
            except BaseException as exc:
                outcome['error'] = exc
            finally:
                theirs.close()

        thread = threading.Thread(target=serve, name='pgdelta-receiver-test')
        thread.start()
        channel = Channel(ours)
        try:
            # A v2 sender's HELLO, against this v3 receiver.
            channel.send_json(protocol.HELLO, {'version': 2, 'chunk_size': CHUNK,
                                               'key': new_key().hex(), 'entries': []})
            # Real sender code does exactly this next: send HELLO, then expect ACCEPT. If the
            # receiver only raised locally, this would see the connection close (Closed), not
            # a message naming both versions.
            with self.assertRaises(protocol.PeerError) as caught:
                channel.expect(protocol.ACCEPT)
            message = str(caught.exception)
            self.assertIn('2', message)
            self.assertIn(str(protocol.VERSION), message)
        finally:
            ours.close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertIsInstance(outcome.get('error'), protocol.ProtocolError)


if __name__ == '__main__':
    unittest.main()
