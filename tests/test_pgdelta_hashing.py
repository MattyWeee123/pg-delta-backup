"""Keyed digest definitions and the per-file hash tree, checked against hashlib directly."""
import hashlib
import io
import os
from pathlib import Path
import random
import tempfile
import unittest

from pgdelta.hashing import (ALGORITHMS, BLAKE3_CONTEXTS, BLOCK, DIGEST_SIZE, NODE_BLOCKS, Hasher, file_digest,
                             leaf_digest, new_key, node_digest)
from pgdelta.tree import hash_file, hash_stream, leaf_digests, open_regular, split_digests

KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))
MIB = 1 << 20
try:
    import blake3
except ImportError:  # the BLAKE3 tests need the optional package
    blake3 = None


def reference_tree(data, key, block=BLOCK, node_blocks=NODE_BLOCKS):
    def blake(payload, person):
        return hashlib.blake2b(payload, key=key, person=person, digest_size=16).digest()
    leaves = [blake(data[offset:offset + block], b'pgdelta-leaf') for offset in range(0, len(data), block)]
    nodes = [blake(b''.join(leaves[i:i + node_blocks]), b'pgdelta-node')
             for i in range(0, len(leaves), node_blocks)]
    digest = blake(len(data).to_bytes(8, 'big') + b''.join(nodes), b'pgdelta-file')
    return leaves, nodes, digest


class HashingTests(unittest.TestCase):
    def test_digests_are_deterministic_for_one_key(self):
        page = os.urandom(BLOCK)
        self.assertEqual(leaf_digest(page, KEY), leaf_digest(page, KEY))
        self.assertEqual(len(leaf_digest(page, KEY)), DIGEST_SIZE)
        self.assertEqual(Hasher(KEY).leaf(page), leaf_digest(page, KEY))

    def test_different_keys_give_different_digests(self):
        page = b'\x00' * BLOCK
        self.assertNotEqual(leaf_digest(page, KEY), leaf_digest(page, OTHER_KEY))
        self.assertNotEqual(node_digest(page[:32], KEY), node_digest(page[:32], OTHER_KEY))
        self.assertNotEqual(file_digest(0, b'', KEY), file_digest(0, b'', OTHER_KEY))
        self.assertNotEqual(new_key(), new_key())

    def test_levels_are_domain_separated(self):
        data = os.urandom(32)
        self.assertNotEqual(leaf_digest(data, KEY), node_digest(data, KEY))

    def test_file_digest_binds_size(self):
        self.assertNotEqual(file_digest(0, b'', KEY), file_digest(1, b'', KEY))

    def test_key_must_be_32_bytes(self):
        for key in (b'', b'short', bytes(33), 'x' * 32):
            with self.subTest(key=key), self.assertRaises(ValueError):
                Hasher(key)

    def test_reused_states_do_not_leak_between_calls(self):
        hasher = Hasher(KEY)
        first = hasher.leaf(b'a')
        hasher.leaf(b'something else')
        self.assertEqual(hasher.leaf(b'a'), first)


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def check(self, data, block=BLOCK, node_blocks=NODE_BLOCKS):
        path = self.root / 'f'
        path.write_bytes(data)
        tree = hash_file(path, KEY, block, node_blocks)
        leaves, nodes, digest = reference_tree(data, KEY, block, node_blocks)
        self.assertEqual(tree.size, len(data))
        self.assertEqual(tree.leaves, leaves)
        self.assertEqual(tree.nodes, nodes)
        self.assertEqual(tree.digest, digest)
        self.assertEqual(tree.page_count, len(leaves))
        self.assertEqual(tree.node_count, len(nodes))
        compact = hash_file(path, KEY, block, node_blocks, keep_leaves=False)
        self.assertEqual((compact.nodes, compact.digest, compact.leaves), (nodes, digest, []))
        return tree

    def test_boundary_sizes_with_default_parameters(self):
        rng = random.Random(7)
        for size in (0, 1, 8191, 8192, 8193, MIB, MIB + 1, 3 * MIB + 5000):
            with self.subTest(size=size):
                tree = self.check(rng.randbytes(size))
                self.assertEqual(tree.page_count, -(-size // BLOCK))
                self.assertEqual(tree.node_count, -(-size // MIB))

    def test_empty_file_has_no_pages_or_nodes(self):
        tree = self.check(b'')
        self.assertEqual((tree.leaves, tree.nodes), ([], []))

    def test_small_and_odd_node_sizes(self):
        rng = random.Random(8)
        for block, node_blocks in ((4096, 3), (8192, 1), (512, 7)):
            for pages in (0.5, 1, 2.5, 7, 21, 22.25):
                with self.subTest(block=block, node_blocks=node_blocks, pages=pages):
                    self.check(rng.randbytes(int(block * pages)), block, node_blocks)

    def test_large_reads_are_whole_nodes(self):
        # Several reads per file, with the file ending mid-read and mid-node.
        data = random.Random(9).randbytes(2 * MIB + 3 * BLOCK + 17)
        self.check(data, BLOCK, 4)

    def test_hash_stream_handles_short_reads(self):
        class Trickle(io.RawIOBase):
            def __init__(self, data):
                self.data, self.offset = data, 0

            def readable(self):
                return True

            def readinto(self, buffer):
                count = min(1000, len(buffer), len(self.data) - self.offset)
                buffer[:count] = self.data[self.offset:self.offset + count]
                self.offset += count
                return count

        data = random.Random(10).randbytes(5 * BLOCK + 99)
        tree = hash_stream(Trickle(data), Hasher(KEY), BLOCK, 2)
        self.assertEqual(tree.digest, reference_tree(data, KEY, BLOCK, 2)[2])

    def test_leaf_digests_for_one_node_match_the_tree(self):
        data = random.Random(11).randbytes(9 * BLOCK + 100)
        path = self.root / 'f'
        path.write_bytes(data)
        tree = hash_file(path, KEY, BLOCK, 4)
        fd = open_regular(path)
        try:
            for node in range(tree.node_count):
                blob = leaf_digests(fd, node * 4, 4, len(data), Hasher(KEY), BLOCK)
                self.assertEqual(split_digests(blob), tree.leaves[node * 4:node * 4 + 4])
        finally:
            os.close(fd)

    def test_refuses_symlinks_and_special_files(self):
        (self.root / 'target').write_bytes(b'x')
        (self.root / 'link').symlink_to(self.root / 'target')
        with self.assertRaises(OSError):
            hash_file(self.root / 'link', KEY)
        if hasattr(os, 'mkfifo'):
            os.mkfifo(self.root / 'fifo')
            with self.assertRaises(OSError):
                hash_file(self.root / 'fifo', KEY)


if __name__ == '__main__':
    unittest.main()


@unittest.skipUnless(blake3, 'the blake3 package is not installed')
class Blake3Tests(unittest.TestCase):
    """EXP-29's option: keyed BLAKE3, each level keyed by a key derived from the session key."""

    def reference(self, level, payload, key=KEY):
        derived = blake3.blake3(key, derive_key_context=BLAKE3_CONTEXTS[level]).digest()
        return blake3.blake3(payload, key=derived).digest(length=DIGEST_SIZE)

    def test_digests_match_blake3_itself(self):
        hasher, page = Hasher(KEY, 'blake3'), os.urandom(BLOCK)
        self.assertEqual(hasher.algorithm, 'blake3')
        self.assertEqual(hasher.leaf(page), self.reference(0, page))
        self.assertEqual(hasher.node(page[:64]), self.reference(1, page[:64]))
        self.assertEqual(hasher.file(5, page[:32]), self.reference(2, (5).to_bytes(8, 'big') + page[:32]))
        self.assertEqual(len(hasher.leaf(page)), DIGEST_SIZE)

    def test_levels_keys_and_algorithms_are_separated(self):
        page = b'\x00' * 32
        b3 = Hasher(KEY, 'blake3')
        self.assertEqual(len({b3.leaf(page), b3.node(page), b3.file(0, page)}), 3)
        self.assertNotEqual(b3.leaf(page), Hasher(OTHER_KEY, 'blake3').leaf(page))
        self.assertNotEqual(b3.leaf(page), Hasher(KEY, 'blake2b').leaf(page))

    def test_reused_states_do_not_leak_between_calls(self):
        hasher = Hasher(KEY, 'blake3')
        first = hasher.leaf(b'a' * BLOCK)
        hasher.leaf(b'b' * BLOCK)
        self.assertEqual(hasher.leaf(b'a' * BLOCK), first)

    def test_the_tree_is_the_same_on_one_job_and_on_worker_processes(self):
        from pgdelta.tree import HashPool
        data = os.urandom(3 * MIB + 5 * BLOCK)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'f'
            path.write_bytes(data)
            hasher = Hasher(KEY, 'blake3')
            serial = hash_file(path, None, keep_leaves=True, hasher=hasher)
            pool = HashPool(2, key=KEY, algorithm='blake3')
            try:
                parallel = hash_file(path, None, keep_leaves=True, hasher=hasher, pool=pool)
            finally:
                pool.close()
        self.assertEqual(parallel.digest, serial.digest)
        self.assertNotEqual(serial.digest, hash_file(Path(__file__), KEY).digest)


class AlgorithmTests(unittest.TestCase):
    def test_unknown_algorithm_is_refused(self):
        self.assertEqual(ALGORITHMS, ('blake2b', 'blake3'))
        with self.assertRaises(ValueError):
            Hasher(KEY, 'sha256')

    def test_default_is_blake2b(self):
        self.assertEqual(Hasher(KEY).algorithm, 'blake2b')
        self.assertEqual(Hasher(KEY).leaf(b'x'), Hasher(KEY, 'blake2b').leaf(b'x'))
