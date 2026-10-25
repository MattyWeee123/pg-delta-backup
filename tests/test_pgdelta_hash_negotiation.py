"""--hash auto (pgdelta.cli's default): the sender offers BLAKE3 where it can hash with it, the receiver picks.

The library's default stays BLAKE2b and sends no offer, so a session that doesn't ask for auto is unchanged on the
wire (tests/pgdelta_frames.py). With an offer, the receiver names its pick in ACCEPT and both ends hash with it.
"""
import importlib.util
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pgdelta import hashing, protocol, receiver as receiver_module
from pgdelta.errors import InvalidInput, ProtocolError

from pgdelta_support import PAGE, snapshot, transfer, write_tree

HAVE_BLAKE3 = importlib.util.find_spec('blake3') is not None


class NegotiationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source, self.basis, self.output = root / 'S', root / 'B', root / 'O'
        rng = random.Random(7)
        old = rng.randbytes(9 * PAGE)
        new = bytearray(old)
        new[3 * PAGE] ^= 1
        write_tree(self.source, {'base/1': bytes(new), 'base/2': rng.randbytes(PAGE)})
        write_tree(self.basis, {'base/1': old})

    def run_auto(self):
        return transfer(self.source, self.basis, self.output, hash_algorithm='auto')

    @unittest.skipUnless(HAVE_BLAKE3, 'the blake3 package is not installed')
    def test_both_ends_with_blake3_use_it(self):
        self.assertEqual(hashing.offer(), ['blake3', 'blake2b'])
        outcome = self.run_auto()
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assertEqual(outcome.sender['hash'], 'blake3')
        self.assertEqual(snapshot(self.output), snapshot(self.source))

    @unittest.skipUnless(HAVE_BLAKE3, 'the blake3 package is not installed')
    def test_a_receiver_without_blake3_picks_blake2b_from_the_offer(self):
        with mock.patch.object(receiver_module, 'usable', lambda algorithm: algorithm == 'blake2b'):
            outcome = self.run_auto()
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assertEqual(outcome.sender['hash'], 'blake2b')
        self.assertEqual(snapshot(self.output), snapshot(self.source))

    def test_a_sender_without_blake3_offers_nothing_and_hashes_blake2b(self):
        with mock.patch.object(hashing, 'usable', lambda algorithm: algorithm == 'blake2b'):
            outcome = self.run_auto()
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assertEqual(outcome.sender['hash'], 'blake2b')
        self.assertEqual(snapshot(self.output), snapshot(self.source))

    def test_the_library_default_offers_nothing(self):
        outcome = transfer(self.source, self.basis, self.output)
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assertEqual(outcome.sender['hash'], 'blake2b')

    def test_a_receiver_that_can_use_nothing_offered_refuses(self):
        with mock.patch.object(hashing, 'offer', lambda: ['blake3']), \
                mock.patch.object(receiver_module, 'usable', lambda algorithm: False):
            outcome = self.run_auto()
        self.assertIsInstance(outcome.receiver_error, InvalidInput)
        self.assertIn('none of the offered hashes', str(outcome.receiver_error))

    def test_an_offer_beside_a_fixed_hash_is_a_protocol_error(self):
        original = protocol.Channel.send_json

        def both(channel, code, value):
            if code == protocol.HELLO:
                value = dict(value, hash='blake2b', hash_offer=['blake2b'])
            return original(channel, code, value)

        with mock.patch.object(protocol.Channel, 'send_json', both):
            outcome = transfer(self.source, self.basis, self.output)
        self.assertIsInstance(outcome.receiver_error, ProtocolError)


if __name__ == '__main__':
    unittest.main()
