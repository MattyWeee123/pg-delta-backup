"""--compress zstd (pgdelta.compress): pages sent compressed where both ends can, plain otherwise.

The offer travels in HELLO and the pick in ACCEPT, so a session without --compress is unchanged on the wire
(tests/pgdelta_frames.py). A receiver without zstd accepts without it; a run that doesn't shrink goes plain; a
compressed run is decompressed only within what its file can still hold.
"""
import random
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from pgdelta import compress as compression, protocol, receiver as receiver_module, sender as sender_module
from pgdelta.errors import ProtocolError

from pgdelta_support import PAGE, snapshot, transfer, write_tree

BACKENDS = [kind for kind, probe in (('stdlib', 'compression.zstd'), ('zstandard', 'zstandard'))
            if __import__('importlib').util.find_spec(probe.split('.')[0]) is not None
            and (probe != 'compression.zstd' or __import__('importlib').util.find_spec(probe) is not None)]


def text_page(rng, n):
    """A page that compresses like EXP-37's rows: repeated structure, some varying digits."""
    row = lambda i: f'{i:010d}|{rng.randrange(10**6):06d}|{"%032x" % rng.getrandbits(128)}|'.encode()
    return b''.join(row(n * 1000 + i) for i in range(200))[:PAGE].ljust(PAGE, b' ')


@unittest.skipUnless(BACKENDS, 'neither compression.zstd nor zstandard is available')
class CodecTests(unittest.TestCase):
    def codecs(self):
        for kind in BACKENDS:
            module = __import__('compression.zstd', fromlist=['zstd']) if kind == 'stdlib' else __import__('zstandard')
            with mock.patch.object(compression, '_backend', lambda kind=kind, module=module: (kind, module)):
                yield kind, compression.Codec()

    def test_round_trip_and_refusals(self):
        data = b''.join(text_page(random.Random(1), n) for n in range(16))
        for kind, codec in self.codecs():
            with self.subTest(backend=kind):
                packed = codec.compress(data)
                self.assertLess(len(packed), len(data) // 2)
                self.assertEqual(codec.decompress(packed, len(data)), data)
                with self.assertRaises(ValueError):
                    codec.decompress(packed, len(data) - 1)  # more than the file can hold
                with self.assertRaises(ValueError):
                    codec.decompress(packed + packed, 2 * len(data))  # a second frame after the first
                with self.assertRaises(ValueError):
                    codec.decompress(b'not zstd at all', len(data))


@unittest.skipUnless(BACKENDS, 'neither compression.zstd nor zstandard is available')
class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source, self.basis, self.output = root / 'S', root / 'B', root / 'O'
        rng = random.Random(7)
        old = b''.join(text_page(rng, n) for n in range(40))
        new = bytearray(old)
        for page in range(0, 40, 3):  # scattered changes
            new[page * PAGE + 100:page * PAGE + 120] = b'X' * 20
        noise = rng.randbytes(8 * PAGE)
        write_tree(self.source, {'base/1/1': bytes(new), 'base/1/2': noise, 'base/1/3': text_page(rng, 99)})
        write_tree(self.basis, {'base/1/1': old, 'base/1/2': rng.randbytes(8 * PAGE)})

    def test_pages_go_compressed_and_the_copy_matches(self):
        outcome = transfer(self.source, self.basis, self.output, compress='zstd')
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assertEqual(snapshot(self.output), snapshot(self.source))
        stats = outcome.sender['compress']
        self.assertEqual((stats['requested'], stats['used']), ('zstd', 'zstd'))
        self.assertGreater(stats['runs_compressed'], 0)
        self.assertLess(stats['runs_compressed'], stats['runs'])  # the random file's runs went plain
        self.assertLess(stats['wire_bytes'], stats['raw_bytes'])
        types = outcome.sender['bytes_source_to_dest']['by_message_type']
        self.assertIn('PAGE_DATA_ZSTD', types)
        self.assertIn('PAGE_DATA', types)  # the random file's runs
        self.assertEqual(outcome.sender['receiver']['compress']['accepted'], True)
        self.assertEqual(outcome.sender['receiver']['compress']['raw_bytes'],
                         stats['raw_bytes'] - (stats['wire_bytes'] - outcome.sender['receiver']['compress']['wire_bytes']))

    def test_runs_compress_on_a_pool_and_arrive_in_order(self):
        # EXP-46: with jobs > 1 runs are compressed on that many threads, sent in order. The receiver refuses a
        # run that overlaps or precedes one already written, so a matching copy shows the order held.
        rng = random.Random(11)
        old = b''.join(text_page(rng, n) for n in range(300))
        new = bytearray(old)
        for page in range(0, 300, 2):  # many short runs
            new[page * PAGE + 7:page * PAGE + 30] = rng.randbytes(23)
        write_tree(self.source, {'base/1/9': bytes(new)})
        write_tree(self.basis, {'base/1/9': old})
        with mock.patch.object(compression, 'TASK_BYTES', 1 << 20):  # EXP-50's batched tasks
            outcome = transfer(self.source, self.basis, self.output, compress='zstd', jobs=3, receiver_jobs=3)
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assertIsNone(outcome.receiver_error, repr(outcome.receiver_error))
        self.assertEqual(snapshot(self.output), snapshot(self.source))
        stats = outcome.sender['compress']
        self.assertEqual(stats['threads'], 3)
        self.assertGreater(stats['runs'], 100)
        # EXP-47: the receiver decompresses on its own pool, and writes in order too.
        received = outcome.sender['receiver']['compress']
        self.assertEqual(received['threads'], 3)
        self.assertEqual(received['runs'], stats['runs_compressed'])
        self.assertGreaterEqual(stats['wait_seconds'], 0)
        # EXP-50: with TASK_BYTES set, the runs travel to the pools in tasks of many, not one each.
        self.assertLess(stats['tasks'], stats['runs'] // 10)
        self.assertLess(received['tasks'], received['runs'] // 10)

    def test_task_sizes_change_no_byte(self):
        # EXP-50: tasks of a few runs each (many tasks, more than two per thread waiting) and one run per task
        # (compress.TASK_BYTES = 0, EXP-47's path) build the same copy, in order.
        rng = random.Random(13)
        old = b''.join(text_page(rng, n) for n in range(200))
        new = bytearray(old)
        for page in range(0, 200, 2):
            new[page * PAGE + 5:page * PAGE + 40] = rng.randbytes(35)
        write_tree(self.source, {'base/1/9': bytes(new)})
        write_tree(self.basis, {'base/1/9': old})
        for task_bytes, label in ((3 * PAGE, 'small'), (0, 'one run each')):
            with self.subTest(tasks=label), mock.patch.object(compression, 'TASK_BYTES', task_bytes):
                output = Path(self.temp.name) / f'O-{task_bytes}'
                outcome = transfer(self.source, self.basis, output, compress='zstd', jobs=2, receiver_jobs=2)
                self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
                self.assertIsNone(outcome.receiver_error, repr(outcome.receiver_error))
                self.assertEqual(snapshot(output), snapshot(self.source))
                stats, received = outcome.sender['compress'], outcome.sender['receiver']['compress']
                self.assertEqual(received['runs'], stats['runs_compressed'])
                if task_bytes:
                    self.assertGreater(stats['tasks'], 2 * stats['threads'])
                    self.assertLess(stats['tasks'], stats['runs'])
                    self.assertLess(received['tasks'], received['runs'])
                else:
                    self.assertEqual(stats['tasks'], stats['runs'])
                    self.assertEqual(received['tasks'], received['runs'])

    def test_a_receiver_without_zstd_takes_plain_pages(self):
        # The receiver's own view of the module (the sender shares the real one, which can compress).
        without = types.SimpleNamespace(ALGORITHMS=compression.ALGORITHMS, Codec=compression.Codec,
                                        usable=lambda algorithm='zstd': False)
        with mock.patch.object(receiver_module, 'compression', without):
            outcome = transfer(self.source, self.basis, self.output, compress='zstd')
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assertEqual(snapshot(self.output), snapshot(self.source))
        self.assertEqual(outcome.sender['compress']['used'], None)
        self.assertNotIn('PAGE_DATA_ZSTD', outcome.sender['bytes_source_to_dest']['by_message_type'])
        self.assertEqual(outcome.sender['compress']['runs'], 0)
        self.assertEqual(outcome.sender['receiver']['compress']['accepted'], False)

    def test_without_compress_nothing_is_offered(self):
        sent = []
        original = protocol.Channel.send_json

        def spy(channel, code, value):
            sent.append((code, value))
            return original(channel, code, value)

        with mock.patch.object(protocol.Channel, 'send_json', spy):
            outcome = transfer(self.source, self.basis, self.output)
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        hello = next(value for code, value in sent if code == protocol.HELLO)
        accept = next(value for code, value in sent if code == protocol.ACCEPT)
        self.assertNotIn('compress_offer', hello)
        self.assertNotIn('compress', accept)
        self.assertNotIn('compress', outcome.sender)
        self.assertNotIn('compress', outcome.sender['receiver'])

    def test_a_compressed_run_without_the_offer_is_refused(self):
        codec = compression.Codec()

        def forced(self, index, first, data, old=None):
            self._send(protocol.PAGE_DATA_ZSTD, protocol.page_data_head(index, first), codec.compress(data))

        with mock.patch.object(sender_module._Sender, '_send_run', forced):
            outcome = transfer(self.source, self.basis, self.output)
        self.assertIsInstance(outcome.receiver_error, ProtocolError)
        self.assertIn('unexpected PAGE_DATA_ZSTD', str(outcome.receiver_error))
        self.assertFalse(self.output.exists())

    def test_a_run_that_decompresses_past_its_file_is_refused(self):
        codec = compression.Codec()

        def oversized(self, index, first, data, old=None):
            self._send(protocol.PAGE_DATA_ZSTD, protocol.page_data_head(index, first),
                       codec.compress(bytes(data) + b'\0' * (64 * PAGE)))

        with mock.patch.object(sender_module._Sender, '_send_run', oversized):
            outcome = transfer(self.source, self.basis, self.output, compress='zstd')
        self.assertIsInstance(outcome.receiver_error, ProtocolError)
        self.assertIn('does not decompress within the file', str(outcome.receiver_error))
        self.assertFalse(self.output.exists())


class UnavailableTests(unittest.TestCase):
    def test_asking_without_zstd_here_is_refused_before_anything_is_sent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_tree(root / 'S', {'f': b'x' * PAGE})
            write_tree(root / 'B', {'f': b'y' * PAGE})
            with mock.patch.object(sender_module.compression, 'usable', lambda algorithm='zstd': False):
                outcome = transfer(root / 'S', root / 'B', root / 'O', compress='zstd')
        self.assertIn('--compress zstd needs', str(outcome.sender_error))


if __name__ == '__main__':
    unittest.main()
