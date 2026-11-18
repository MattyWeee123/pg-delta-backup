"""Tests for batched digest questions and bitmap answers."""
import unittest

from pgdelta import protocol


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
