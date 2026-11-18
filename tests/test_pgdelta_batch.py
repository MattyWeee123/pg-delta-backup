"""Tests for batched digest questions and bitmap answers."""
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pgdelta import protocol  # noqa: E402
from pgdelta.hashing import new_key  # noqa: E402
from pgdelta.protocol import Channel  # noqa: E402
from pgdelta.receiver import run_receiver  # noqa: E402
from pgdelta.tree import Entry  # noqa: E402
from test_pgdelta import CHUNK, RefreshFixture, tree_bytes, write  # noqa: E402


class Verdicts(unittest.TestCase):
    def test_bitmap_round_trips_lsb_first(self):
        for flags in ([True], [False], [False] * 8, [True] * 8,
                      [True, False, False, False, False, False, False, False, True]):
            payload = protocol.pack_verdicts(flags)
            self.assertEqual(protocol.unpack_verdicts(payload, len(flags)), flags)

    def test_one_byte_per_eight_chunks(self):
        self.assertEqual(len(protocol.pack_verdicts([False] * 9)),
                         protocol.VERDICTS_HEAD.size + 2)

    def test_first_chunk_is_the_low_bit(self):
        payload = protocol.pack_verdicts([True] + [False] * 7)
        self.assertEqual(payload[protocol.VERDICTS_HEAD.size], 0x01)

    def test_a_count_mismatch_is_refused(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.unpack_verdicts(protocol.pack_verdicts([True, False]), 3)

    def test_a_short_bitmap_is_refused(self):
        payload = protocol.pack_verdicts([False] * 9)
        with self.assertRaises(protocol.ProtocolError):
            protocol.unpack_verdicts(payload[:-1], 9)

    def test_the_protocol_version_is_two(self):
        self.assertEqual(protocol.VERSION, 2)


class BatchedExchange(RefreshFixture, unittest.TestCase):
    def test_one_batch_answers_many_chunks(self):
        self.both('base/1/16384', os.urandom(5 * CHUNK))
        code, report = self.refresh('--batch', '128')
        self.assertEqual(code, 0)
        self.assertEqual(report['counts']['chunks_asked'], 5)
        self.assertEqual(report['counts']['round_trips'], 1)
        self.assertEqual(report['counts']['chunks_same'], 5)

    def test_batches_do_not_span_two_files(self):
        self.both('a', os.urandom(3 * CHUNK))
        self.both('b', os.urandom(3 * CHUNK))
        code, report = self.refresh('--batch', '128')
        self.assertEqual(report['counts']['chunks_asked'], 6)
        self.assertEqual(report['counts']['round_trips'], 2)

    def test_a_batch_stops_at_the_basis_end(self):
        base = os.urandom(3 * CHUNK)
        write(self.b, 'grew', base)
        write(self.s, 'grew', base + os.urandom(2 * CHUNK))
        code, report = self.refresh('--batch', '128')
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        self.assertEqual(report['counts']['chunks_asked'], 3)
        self.assertEqual(report['counts']['chunks_new'], 2)
        self.assertEqual(report['counts']['round_trips'], 1)

    def test_a_partly_covered_chunk_is_new_not_asked(self):
        """B's size is not a multiple of the chunk size: the straddling chunk is sent, not asked about."""
        base = os.urandom(2 * CHUNK + 100)
        write(self.b, 'f', base)
        write(self.s, 'f', base + os.urandom(CHUNK - 100))
        code, report = self.refresh('--batch', '128')
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        self.assertEqual(report['counts']['chunks_asked'], 2)
        self.assertEqual(report['counts']['chunks_new'], 1)

    def test_a_short_final_batch(self):
        self.both('f', os.urandom(5 * CHUNK))
        code, report = self.refresh('--batch', '2')
        self.assertEqual(report['counts']['chunks_asked'], 5)
        self.assertEqual(report['counts']['round_trips'], 3)

    def test_an_empty_file_asks_nothing(self):
        write(self.s, 'empty', b'')
        write(self.b, 'empty', b'')
        code, report = self.refresh('--batch', '128')
        self.assertEqual(code, 0)
        self.assertEqual(report['counts'].get('round_trips', 0), 0)

    def test_changed_chunks_in_a_batch_are_sent_in_order(self):
        data = bytearray(os.urandom(6 * CHUNK))
        write(self.b, 'f', bytes(data))
        for i in (1, 4):
            data[i * CHUNK] ^= 0xFF
        write(self.s, 'f', bytes(data))
        code, report = self.refresh('--batch', '128')
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        self.assertEqual(report['counts']['chunks_changed'], 2)
        self.assertEqual(report['counts']['chunks_same'], 4)
        self.assertEqual(report['counts']['round_trips'], 1)


class MalformedBatch(unittest.TestCase):
    """A peer-supplied count must never size an allocation before it is validated."""

    def inject(self, head, body, basis_bytes=4 * CHUNK) -> str:
        """HELLO a single 4-chunk file B also has, then send one CHUNK_HASHES; return the ERROR message.

        `basis_bytes` is how much of that file actually exists in B (default: all of it), so a chunk
        that's within S's declared size but past B's actual end can be injected too."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        b = root / 'B'
        self.o = root / 'O'
        b.mkdir()
        write(b, 'f', os.urandom(basis_bytes))
        entries = [Entry('f', 'file', 0o600, size=4 * CHUNK)]
        ours, theirs = socket.socketpair()
        outcome = {}

        def serve():
            try:
                outcome['report'] = run_receiver(Channel(theirs), b, self.o)
            except BaseException as exc:
                outcome['error'] = exc
            finally:
                theirs.close()

        thread = threading.Thread(target=serve, name='pgdelta-receiver-test')
        thread.start()
        channel = Channel(ours)
        try:
            channel.send_json(protocol.HELLO, {
                'version': protocol.VERSION, 'chunk_size': CHUNK, 'key': new_key().hex(),
                'root_mode': 0o700, 'entries': [entry.to_json() for entry in entries]})
            channel.expect(protocol.ACCEPT)
            channel.send(protocol.CHUNK_HASHES, head + body)
            kind, payload = channel.recv()
            self.assertEqual(kind, protocol.ERROR)
            return json.loads(payload)['message']
        finally:
            ours.close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

    def test_a_zero_count_is_refused(self):
        head = protocol.CHUNK_HASHES_HEAD.pack(0, 0, 0)
        self.assertIn('count', self.inject(head, b''))

    def test_a_count_beyond_the_body_is_refused(self):
        head = protocol.CHUNK_HASHES_HEAD.pack(0, 0, 2 ** 32 - 1)
        self.assertIn('count', self.inject(head, b'\x00' * protocol.DIGEST_SIZE))

    def test_a_body_that_is_not_a_whole_number_of_digests_is_refused(self):
        head = protocol.CHUNK_HASHES_HEAD.pack(0, 0, 2)
        self.assertIn('count', self.inject(head, b'\x00' * (protocol.DIGEST_SIZE + 1)))

    def test_a_chunk_past_the_file_end_is_refused(self):
        head = protocol.CHUNK_HASHES_HEAD.pack(0, 99, 1)
        self.assertIn('past the end', self.inject(head, b'\x00' * protocol.DIGEST_SIZE))

    def test_a_chunk_outside_bs_size_is_refused(self):
        """Within S's declared 4-chunk size, but B's real file is only 2 chunks long."""
        head = protocol.CHUNK_HASHES_HEAD.pack(0, 2, 1)
        message = self.inject(head, b'\x00' * protocol.DIGEST_SIZE, basis_bytes=2 * CHUNK)
        self.assertIn("outside B's size", message)

    def test_no_output_is_published_after_a_bad_batch(self):
        """Unlike the validation-order checks above, these two both fail after open_file has already
        created the output fd (and, for the second, B's fd too) — cleanup must still remove everything."""
        head = protocol.CHUNK_HASHES_HEAD.pack(0, 99, 1)
        self.inject(head, b'\x00' * protocol.DIGEST_SIZE)
        self.assertFalse(self.o.exists())
        self.assertFalse(any(p.name.endswith('.pgdelta-partial') for p in self.o.parent.iterdir()))

        head = protocol.CHUNK_HASHES_HEAD.pack(0, 2, 1)
        self.inject(head, b'\x00' * protocol.DIGEST_SIZE, basis_bytes=2 * CHUNK)
        self.assertFalse(self.o.exists())
        self.assertFalse(any(p.name.endswith('.pgdelta-partial') for p in self.o.parent.iterdir()))


if __name__ == '__main__':
    unittest.main()
