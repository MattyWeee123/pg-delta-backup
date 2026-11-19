import unittest

from pgdelta import protocol


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


if __name__ == '__main__':
    unittest.main()
