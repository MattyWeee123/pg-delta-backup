"""Framing, byte accounting, binary message encodings and positional matching."""
import io
import os
import struct
import unittest

from pgdelta import matching, protocol
from pgdelta.errors import ProtocolError
from pgdelta.hashing import Hasher
from pgdelta.protocol import Channel, ChannelClosed
from pgdelta.tree import FileTree, hash_stream

KEY = bytes(32)


def loopback(max_frame=protocol.MAX_FRAME):
    read_fd, write_fd = os.pipe()
    channel = Channel(os.fdopen(read_fd, 'rb'), os.fdopen(write_fd, 'wb'), max_frame=max_frame)
    return channel


def reader_for(raw):
    return Channel(io.BytesIO(raw), io.BytesIO())


class FramingTests(unittest.TestCase):
    def test_round_trip_and_byte_accounting(self):
        channel = loopback()
        self.addCleanup(channel.close)
        channel.send_json(protocol.HELLO, {'a': 1})
        channel.send(protocol.BASIS_NODES, protocol.encode_basis_nodes(3, 10, b'n' * 16))
        channel.send(protocol.PAGE_DATA, protocol.page_data_head(3, 7), b'p' * 100)
        received = [channel.recv() for _ in range(3)]
        self.assertEqual(protocol.decode_json(received[0][1], 'HELLO'), {'a': 1})
        self.assertEqual(protocol.decode_basis_nodes(received[1][1]), (3, 10, b'n' * 16))
        index, first, data = protocol.decode_page_data(received[2][1])
        self.assertEqual((index, first, bytes(data)), (3, 7, b'p' * 100))
        for counter in (channel.sent, channel.received):
            summary = counter.summary()
            self.assertEqual(summary['total'], 3 * 5 + 7 + 28 + 112)
            self.assertEqual(summary['control'], 12)
            self.assertEqual(summary['node_digests'], 33)
            self.assertEqual(summary['page_data'], 117)
            self.assertEqual(sum(summary[c] for c in protocol.CATEGORIES), summary['total'])
            self.assertEqual(summary['by_message_type']['PAGE_DATA'], {'frames': 1, 'bytes': 117})

    def test_digests_and_pages_are_not_hex_encoded(self):
        payload = protocol.encode_basis_nodes(0, 8192, bytes(range(16)))
        self.assertEqual(len(payload), 4 + 8 + 16)

    def test_clean_end_of_stream_returns_none(self):
        self.assertIsNone(reader_for(b'').recv())

    def test_unknown_message_type_is_rejected(self):
        with self.assertRaisesRegex(ProtocolError, 'unknown message type 99'):
            reader_for(struct.pack('>IB', 0, 99)).recv()
        channel = loopback()
        self.addCleanup(channel.close)
        with self.assertRaises(ProtocolError):
            channel.send(99, b'')

    def test_oversized_frame_is_rejected_before_reading_it(self):
        with self.assertRaisesRegex(ProtocolError, 'exceeds'):
            reader_for(struct.pack('>IB', 1 << 30, protocol.PAGE_DATA)).recv()
        small = loopback(max_frame=10)
        self.addCleanup(small.close)
        with self.assertRaisesRegex(ProtocolError, 'exceeds'):
            small.send(protocol.PAGE_DATA, b'x' * 11)

    def test_truncated_frames_are_transfer_failures(self):
        for raw in (b'\x00\x00', struct.pack('>IB', 10, protocol.PAGE_DATA) + b'abc'):
            with self.subTest(raw=raw), self.assertRaises(ChannelClosed):
                reader_for(raw).recv()

    def test_send_after_peer_closed_raises_channel_closed(self):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        channel = Channel(io.BytesIO(), os.fdopen(write_fd, 'wb'))
        self.addCleanup(channel.close)
        with self.assertRaises(ChannelClosed):
            channel.send(protocol.PAGE_DATA, b'x' * 100000)

    def test_binary_messages_round_trip(self):
        self.assertEqual(protocol.decode_leaf_request(protocol.encode_leaf_request(5, [0, 2, 9])), (5, [0, 2, 9]))
        entries = [(0, b'a' * 32), (4, b'b' * 16)]
        self.assertEqual(protocol.decode_leaf_digests(protocol.encode_leaf_digests(2, entries)), (2, entries))
        self.assertEqual(protocol.decode_file_done(protocol.encode_file_done(1, 99, b'd' * 16)), (1, 99, b'd' * 16))

    def test_malformed_binary_messages_are_rejected(self):
        cases = [
            (protocol.decode_basis_nodes, b'\x00' * 11),
            (protocol.decode_basis_nodes, b'\x00' * 13),
            (protocol.decode_leaf_request, b'\x00' * 4),
            (protocol.decode_leaf_request, b'\x00' * 6),
            (protocol.decode_leaf_digests, b'\x00' * 4 + struct.pack('>II', 0, 2) + b'x' * 16),
            (protocol.decode_page_data, b'\x00' * 12),
            (protocol.decode_file_done, b'\x00' * 27),
            (lambda p: protocol.decode_json(p, 'X'), b'[1]'),
            (lambda p: protocol.decode_json(p, 'X'), b'\xff'),
        ]
        for decode, payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ProtocolError):
                decode(payload)


def tree_of(data, node_blocks=4, block=8192):
    return hash_stream(io.BytesIO(data), Hasher(KEY), block, node_blocks)


class MatchingTests(unittest.TestCase):
    def test_equal_nodes_match_and_changed_nodes_descend(self):
        basis = bytes(12 * 8192)
        source = bytearray(basis)
        source[5 * 8192] = 1
        plan = matching.classify_nodes(tree_of(bytes(source)), len(basis), tree_of(basis).node_blob)
        self.assertEqual((plan.matched, plan.descend, plan.whole), ([0, 2], [1], []))

    def test_append_and_truncate_descend_only_into_the_boundary_node(self):
        basis = tree_of(bytes(6 * 8192))
        appended = matching.classify_nodes(tree_of(bytes(11 * 8192)), basis.size, basis.node_blob)
        self.assertEqual((appended.matched, appended.descend, appended.whole), ([0], [1], [2]))
        truncated = matching.classify_nodes(tree_of(bytes(5 * 8192)), basis.size, basis.node_blob)
        self.assertEqual((truncated.matched, truncated.descend, truncated.whole), ([0], [1], []))

    def test_changed_pages_compare_length_and_digest(self):
        basis = tree_of(bytes(6 * 8192 + 100))
        source = tree_of(bytes(6 * 8192 + 200))
        pages = matching.changed_pages(source, 1, basis.size, basis.leaf_blob[4 * 16:])
        self.assertEqual(pages, [6])

    def test_high_churn_skips_descent(self):
        basis = tree_of(bytes(12 * 8192))
        source = tree_of(b'\x01' * (8 * 8192) + bytes(4 * 8192))
        plan = matching.classify_nodes(source, basis.size, basis.node_blob, high_churn_node_fraction=0.5)
        self.assertTrue(plan.high_churn)
        self.assertEqual((plan.descend, plan.whole), ([], [0, 1]))
        plan = matching.classify_nodes(source, basis.size, basis.node_blob, high_churn_node_fraction=0.7)
        self.assertEqual((plan.descend, plan.whole), ([0, 1], []))

    def test_runs_coalesce_contiguous_pages(self):
        self.assertEqual(matching.runs([0, 1, 2, 5, 6, 9], 2), [(0, 2), (2, 1), (5, 2), (9, 1)])
        self.assertEqual(matching.runs([], 4), [])

    def test_max_nodes_per_request_keeps_replies_inside_one_frame(self):
        for node_blocks in (1, 128, 65536):
            count = protocol.max_nodes_per_request(node_blocks)
            self.assertGreaterEqual(count, 1)
            self.assertLessEqual(count * (8 + node_blocks * 16) + 4, protocol.MAX_FRAME)

    def test_file_tree_accessors(self):
        tree = FileTree(1, 8192, 4, b'd' * 16, b'n' * 16, b'l' * 16)
        self.assertEqual((tree.page_count, tree.node_count, tree.node(0), tree.leaf(0)),
                         (1, 1, b'n' * 16, b'l' * 16))


if __name__ == '__main__':
    unittest.main()
