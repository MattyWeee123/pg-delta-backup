"""Tests for the streaming engine: the receiver volunteers B's digests, the sender copies runs.

Nothing is asked for here. The receiver hashes B on a thread of its own and streams BASIS_DIGESTS
without being asked; the sender drains them on a reader thread while its main thread hashes S,
compares locally, and streams CHUNK_COPY runs plus CHUNK_DATA for what differs.
"""
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

# Both the repo root (for `pgdelta`) and this file's own directory (for `test_pgdelta`). The second
# matters because CI runs `python -m unittest discover -s tests`, so test modules are imported by
# their bare name; without `tests/` on sys.path, `from test_pgdelta import ...` would import a
# second, separate copy of that module rather than resolving to the one already imported.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pgdelta import cli, protocol, receiver as receiver_module, sender  # noqa: E402
from pgdelta.hashing import Hasher, new_key  # noqa: E402
from pgdelta.protocol import Channel  # noqa: E402
from pgdelta.receiver import run_receiver  # noqa: E402
from pgdelta.sender import run_sender  # noqa: E402
from pgdelta.stats import Stats  # noqa: E402
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
    def test_the_sender_asks_for_nothing(self):
        """There is no question frame in the protocol any more, so this asserts the whole set of
        frame types that leave this side rather than the absence of one retired name -- which,
        now that the name is gone from protocol.NAMES, could never appear whatever the code did."""
        self.both('base/1/16384', os.urandom(5 * CHUNK))
        code, report = self.refresh()
        self.assertEqual(code, 0)
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        self.assertEqual(set(report['bytes_sent']), {'HELLO', 'CHUNK_COPY', 'FILE_DONE', 'COMPLETE'})
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


class DigestWindow(RefreshFixture, unittest.TestCase):
    def test_the_window_floors_at_one_whole_frame(self):
        self.assertEqual(sender.MIN_DIGEST_WINDOW, 16384)
        self.assertEqual(sender.effective_digest_window(1), sender.MIN_DIGEST_WINDOW)
        self.assertEqual(sender.effective_digest_window(64 << 20), 64 << 20)

    def test_a_tiny_window_still_completes(self):
        """Review Focus 5: a window below one frame starts the reader already over its own
        limit on the very first frame, collapsing the pipeline into synchronous frame-at-a-time
        handoff rather than deadlocking outright -- see MIN_DIGEST_WINDOW's comment in
        sender.py for the mechanism."""
        data = bytearray(os.urandom(300 * CHUNK))
        write(self.b, 'f', bytes(data))
        for i in range(0, 300, 4):
            data[i * CHUNK] ^= 0xFF
        write(self.s, 'f', bytes(data))
        run_with_timeout(self, self.refresh, 60, '--digest-window', '1')
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))

    def test_a_floored_window_is_said_out_loud_and_recorded(self):
        """The window a run really used has to be visible somewhere. The sweep that chooses this
        knob's default labels each row with the value it asked for, so a floor that nothing reports
        publishes a table whose independent variable is wrong. cli.main is called directly here
        because RefreshFixture.refresh swallows stderr, which is where the flooring is said."""
        self.both('f', os.urandom(2 * CHUNK))
        said = io.StringIO()
        with redirect_stderr(said):
            code = cli.main(['local', '--source', str(self.s), '--basis', str(self.b),
                             '--output', str(self.o), '--stats-json', str(self.stats),
                             '--digest-window', '1'])
        self.assertEqual(code, 0)
        report = json.loads(self.stats.read_text())
        self.assertEqual(report['digest_window'], sender.MIN_DIGEST_WINDOW)
        self.assertIn('--digest-window 1', said.getvalue())
        self.assertIn(str(sender.MIN_DIGEST_WINDOW), said.getvalue())

    def test_a_window_above_the_floor_is_recorded_as_it_was_given(self):
        self.both('f', os.urandom(2 * CHUNK))
        window = 2 * sender.MIN_DIGEST_WINDOW
        code, report = self.refresh('--digest-window', str(window))
        self.assertEqual(code, 0)
        self.assertEqual(report['digest_window'], window)

    def test_a_non_positive_window_is_a_bad_argument(self):
        for value in ('0', '-1'):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                self.refresh('--digest-window', value)
            self.assertEqual(caught.exception.code, 2)


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

        A one-digest window and tiny socket buffers stand in for the 32 GiB of basis it would
        otherwise take to fill the default window, and the failure lands at the first file's
        FILE_DONE, by which time the pipeline is in that steady state.

        The two assertions below the setup are what keep those numbers honest. Sampling the window
        at the instant of failure would be the direct check, but it is inherently racy -- the main
        thread drains a frame between stalls -- so what is asserted instead is the configuration
        that forces the stall: one frame of digests must overflow the window, so the reader has to
        stop after every frame, and one frame must not fit the socket buffer, so the generator has
        to block inside a send. Without both, this test would still pass against the bug."""
        small = protocol.MIN_CHUNK_SIZE   # so one frame of digests covers only 512 KiB of basis
        window = protocol.DIGEST_SIZE     # exactly one digest
        buffer_bytes = 2048
        frame = protocol.DIGESTS_PER_FRAME * protocol.DIGEST_SIZE
        for name, chunks in (('a', 2000), ('b', 4000)):
            write(self.b, name, os.urandom(chunks * small))
            write(self.s, name, os.urandom(chunks * small))   # differs everywhere: both sides stay busy
        real = os.pwrite

        def corrupt(fd, chunk, offset):   # 'a' will not match its digest, so FILE_DONE('a') fails
            return real(fd, bytes([chunk[0] ^ 0x55]) + chunk[1:], offset)

        ours, theirs = socket.socketpair()
        for sock in (ours, theirs):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, buffer_bytes)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, buffer_bytes)
        self.assertGreater(frame, window, 'one frame must overflow the window, or the reader never stalls')
        for sock in (ours, theirs):   # the kernel may clamp the buffer upwards; it must stay under a frame
            self.assertLess(sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF), frame,
                            'a whole frame fits the socket buffer, so the generator never blocks in send')
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
                outcome['sender'] = run_sender(Channel(ours), self.s, small, None, window)
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
        # Small buffers, as macOS has by default (8 KiB), so that a sender writing page data at a peer
        # that has stopped reading blocks in its send on every system -- the case where only hanging
        # up wakes it. Linux's ~208 KiB default let these tests pass while macOS hung.
        for sock in (ours, theirs):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
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

    def drive_hello(self, hello: dict, seconds=30) -> str:
        """HELLO a receiver with raw fields and return the ERROR message it refuses with, asserting
        that it finished, that no O appeared and that nothing was left staged. The refusal is read
        the way a real sender would meet it: as a PeerError where ACCEPT should have been."""
        root = self.temp_root()
        basis, output = root / 'B', root / 'O'
        basis.mkdir()
        write(basis, 'f', os.urandom(4 * CHUNK))   # a basis the generator would hash, if it started
        ours, theirs = socket.socketpair()
        outcome = {}

        def serve():
            try:
                outcome['report'] = run_receiver(Channel(theirs), basis, output)
            except BaseException as exc:
                outcome['error'] = exc
            finally:
                theirs.close()

        thread = threading.Thread(target=serve, name='pgdelta-receiver-test', daemon=True)
        thread.start()
        channel = Channel(ours)
        try:
            channel.send_json(protocol.HELLO, hello)
            with self.assertRaises(protocol.PeerError) as caught:
                channel.expect(protocol.ACCEPT)
            return str(caught.exception)
        finally:
            ours.close()
            thread.join(timeout=seconds)
            self.assertFalse(thread.is_alive(), 'run_receiver never finished')
            self.assertIsInstance(outcome.get('error'), protocol.ProtocolError)
            self.assertFalse(output.exists())
            self.assertFalse(any(p.name.endswith('.pgdelta-partial') for p in root.iterdir()))

    def hello_with(self, **overrides) -> dict:
        entries = [Entry('f', 'file', 0o600, size=4 * CHUNK)]
        return {'version': protocol.VERSION, 'chunk_size': CHUNK, 'key': new_key().hex(),
                'root_mode': 0o700, 'entries': [entry.to_json() for entry in entries], **overrides}

    def test_a_digest_withheld_while_later_ones_arrive_is_refused(self):
        """The one wait in the sender that had no wake-up. A peer that sends chunk 1's digest and
        withholds chunk 0's, holding the connection open, parked the main thread in take() for
        ever: no error, no RESULT, and a window filling with digests it cannot reach yet. Digests
        arriving in the generator's order is the invariant that makes the window safe, so it is
        checked rather than assumed."""
        def peer(channel):
            channel.expect(protocol.HELLO)
            channel.send_json(protocol.ACCEPT, {'basis_sizes': [[0, 4 * CHUNK]]})
            channel.send(protocol.BASIS_DIGESTS,
                         protocol.BASIS_DIGESTS_HEAD.pack(0, 1, 1) + bytes(protocol.DIGEST_SIZE))
            # and then nothing at all, with this end held open: only the ordering check ends it

        error = self.drive_sender(peer)
        self.assertIsInstance(error, protocol.ProtocolError)
        self.assertIn(self.NAME, str(error))
        self.assertIn('later chunks', str(error))

    def test_a_frame_that_would_store_nothing_is_refused(self):
        """A frame that passes validation and stores nothing used to be ignored, and ignoring it is
        a livelock: it notifies the waiting take(), which re-checks, finds the table still empty and
        waits again, while `_held` never moves so the reader thread never stops reading. A peer
        could hold this side in that loop for ever while looking perfectly healthy. Refused now, so
        every admitted frame either stores a digest or is a protocol error."""
        def peer(channel):
            channel.expect(protocol.HELLO)
            # B declares more of this file than S holds, so chunk 4 is past S's end and is not a
            # chunk this side compares. The bound this replaces tested the run's *start* against
            # B's declared size, which a frame for chunk 4 passes.
            channel.send_json(protocol.ACCEPT, {'basis_sizes': [[0, 8 * CHUNK]]})
            channel.send(protocol.BASIS_DIGESTS,
                         protocol.BASIS_DIGESTS_HEAD.pack(0, 4, 1) + bytes(protocol.DIGEST_SIZE))
            # and then nothing at all, with this end held open: only the refusal ends it

        error = self.drive_sender(peer)
        self.assertIsInstance(error, protocol.ProtocolError)
        self.assertIn(self.NAME, str(error))
        self.assertIn('outside', str(error))

    def test_a_repeated_digest_frame_is_refused(self):
        """A frame can be comparable, be admitted, and still add nothing this side can take: a
        repeat overwrites the key it already holds while `_held` rises anyway. Enough of those and
        the reader parks on a full window over a table the main thread has emptied, with nothing
        left to wake the waiter -- the livelock above again, with `_held` rising instead of staying
        still. The generator sends each digest once, so a second one is the peer's error."""
        def peer(channel):
            channel.expect(protocol.HELLO)
            channel.send_json(protocol.ACCEPT, {'basis_sizes': [[0, 4 * CHUNK]]})
            frame = protocol.BASIS_DIGESTS_HEAD.pack(0, 0, 4) + bytes(4 * protocol.DIGEST_SIZE)
            channel.send(protocol.BASIS_DIGESTS, frame)
            channel.send(protocol.BASIS_DIGESTS, frame)   # the very same digests, a second time
            # and then nothing at all, with this end held open: only the refusal ends it

        error = self.drive_sender(peer)
        self.assertIsInstance(error, protocol.ProtocolError)
        self.assertIn(self.NAME, str(error))
        self.assertIn('a second digest', str(error))

    def test_a_replayed_digest_frame_is_refused(self):
        """The sibling of the one above, and the reason both are now closed by one watermark rather
        than case by case. A digest already taken is absent from the table, so a replay of it passes
        both the end-of-run bound and the already-holding check, and lands as an orphan: the walk
        never revisits an entry, so nothing will ever take it. Sent after the sender's last take()
        -- while it is parked in result() -- enough of them fill the window with a table nothing can
        drain, and the reader parks before it can read the RESULT that wait is for."""
        def peer(channel):
            channel.expect(protocol.HELLO)
            channel.send_json(protocol.ACCEPT, {'basis_sizes': [[0, 4 * CHUNK]]})
            frame = protocol.BASIS_DIGESTS_HEAD.pack(0, 0, 4) + bytes(4 * protocol.DIGEST_SIZE)
            channel.send(protocol.BASIS_DIGESTS, frame)
            while channel.recv()[0] != protocol.COMPLETE:
                pass                 # by COMPLETE all four chunks have been compared and popped
            channel.send(protocol.BASIS_DIGESTS, frame)   # the same digests again, now as orphans
            # and no RESULT, with this end held open: only the refusal ends it

        error = self.drive_sender(peer)
        self.assertIsInstance(error, protocol.ProtocolError)
        self.assertIn(self.NAME, str(error))
        self.assertIn('already compared', str(error))

    def test_a_hello_with_a_zero_chunk_size_is_refused(self):
        """chunk_size arrives in HELLO, and at 0 protocol.comparable is true for every chunk of a
        non-empty file at an offset that never advances: an endless digest generator and unbounded
        memory out of one unvalidated field. Refused before ACCEPT, so that thread never starts."""
        message = self.drive_hello(self.hello_with(chunk_size=0))
        self.assertIn('chunk size 0', message)
        self.assertIn(str(protocol.MIN_CHUNK_SIZE), message)

    def test_a_hello_with_a_chunk_size_past_the_maximum_is_refused(self):
        message = self.drive_hello(self.hello_with(chunk_size=protocol.MAX_CHUNK_SIZE + 1))
        self.assertIn(str(protocol.MAX_CHUNK_SIZE + 1), message)
        self.assertIn(str(protocol.MAX_CHUNK_SIZE), message)

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
        self.assertIn('entry 0', str(error))
        self.assertIn('never declared', str(error))

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


class ResultIsGuarded(unittest.TestCase):
    """`result()` is the one wait in _BasisDigests that the emission-order invariant does not cover
    by itself: by the time it is called the walk is over, so a digest still in the table is one
    nothing will take, and it holds window space the reader needs to reach RESULT. The watermark in
    _store is what stops a peer putting one there, so this drives the table directly."""

    def test_a_table_nothing_can_drain_raises_instead_of_waiting(self):
        ours, theirs = socket.socketpair()
        self.addCleanup(theirs.close)
        self.addCleanup(ours.close)
        entries = [Entry('f', 'file', 0o600, size=CHUNK)]
        digests = sender._BasisDigests(Channel(ours), CHUNK, entries, {0: CHUNK}, 1 << 20)
        digests._digests[(0, 0)] = bytes(protocol.DIGEST_SIZE)   # an orphan nothing will take
        outcome = {}

        def ask():
            try:
                digests.result(Stats('sender'))
            except BaseException as exc:
                outcome['error'] = exc

        # On a thread, so losing the guard fails this test rather than wedging the whole run: there
        # is no reader here, and the wait it would fall into has nothing that could ever end it.
        thread = threading.Thread(target=ask, name='pgdelta-result-test', daemon=True)
        thread.start()
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive(), 'result() is waiting for a RESULT that cannot arrive')
        self.assertIsInstance(outcome.get('error'), protocol.ProtocolError)
        self.assertIn('cannot drain', str(outcome['error']))


class ResultWhenTheSenderLeaves(unittest.TestCase):
    """O is renamed and every file in it verified before RESULT goes out, so a sender that leaves
    after COMPLETE must not turn a finished refresh into a failure with no report. Two ways it can
    leave: closing outright, and half-closing its write side while still waiting for RESULT -- which
    must still receive it, because by COMPLETE there is no generator left to unblock."""

    def drive(self, leave: str) -> tuple[dict, bytes, Path, list[int]]:
        """Drive a real receiver through a whole session, leave the way `leave` says, and return its
        report, B's bytes, O and the frame types this peer saw."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        basis, output = root / 'B', root / 'O'
        basis.mkdir()
        data = os.urandom(4 * CHUNK)
        write(basis, 'f', data)
        key = new_key()
        entries = [Entry('f', 'file', 0o600, size=len(data))]
        ours, theirs = socket.socketpair()
        outcome = {}

        def serve():
            try:
                outcome['report'] = run_receiver(Channel(theirs), basis, output)
            except BaseException as exc:
                outcome['error'] = exc
            finally:
                theirs.close()

        thread = threading.Thread(target=serve, name='pgdelta-receiver-test', daemon=True)
        thread.start()
        channel = Channel(ours)
        seen = []
        try:
            channel.send_json(protocol.HELLO, {
                'version': protocol.VERSION, 'chunk_size': CHUNK, 'key': key.hex(),
                'root_mode': 0o700, 'entries': [entry.to_json() for entry in entries]})
            channel.expect(protocol.ACCEPT)
            # Waiting for B's digests before COMPLETE is what makes the half-close case definite:
            # one file, one frame, so a frame in hand means the generator has finished.
            kind, _ = channel.recv()
            self.assertEqual(kind, protocol.BASIS_DIGESTS, protocol.NAMES.get(kind, kind))
            whole = Hasher(key).file()
            for chunk in range(4):
                piece = data[chunk * CHUNK:(chunk + 1) * CHUNK]
                whole.update(piece)
                channel.send(protocol.CHUNK_DATA, protocol.CHUNK_DATA_HEAD.pack(0, chunk) + piece)
            channel.send(protocol.FILE_DONE, protocol.FILE_DONE_BODY.pack(0, len(data), whole.digest()))
            channel.send(protocol.COMPLETE)
            if leave == 'close':
                ours.close()
            else:
                ours.shutdown(socket.SHUT_WR)
                try:
                    while True:
                        seen.append(channel.recv()[0])
                except (protocol.Closed, OSError):
                    pass
        finally:
            thread.join(timeout=30)
            if leave != 'close':
                ours.close()
        self.assertFalse(thread.is_alive(), 'run_receiver never finished')
        self.assertNotIn('error', outcome, f"run_receiver raised: {outcome.get('error')!r}")
        return outcome['report'], data, output, seen

    def test_a_sender_that_closes_after_complete_keeps_o_and_the_report(self):
        report, data, output, _ = self.drive('close')
        self.assertEqual(report['counts']['files'], 1)     # the report, not a broken pipe
        self.assertEqual((output / 'f').read_bytes(), data)

    def test_a_half_closed_sender_still_receives_the_result(self):
        report, data, output, seen = self.drive('half-close')
        self.assertEqual(report['counts']['files'], 1)
        self.assertEqual((output / 'f').read_bytes(), data)
        self.assertIn(protocol.RESULT, seen)


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
