"""Parallel hashing: trees, digests, statistics and outputs are identical for any pool of workers."""
from pathlib import Path
import random
import tempfile
import threading
import time
import unittest
from unittest import mock

from pgdelta.errors import InvalidInput
from pgdelta.hashing import BLOCK, NODE_BLOCKS, Hasher
from pgdelta.receiver import remove_tree
from pgdelta.tree import HashPool, hash_file

from pgdelta_support import (PAGE, comparable, differing_pages, snapshot, staging_dirs, transfer,
                             write_tree)
from test_pgdelta_hashing import reference_tree

KEY = bytes(range(32))
MIB = 1 << 20
# (sender jobs, receiver jobs): serial, parallel, more workers than nodes in a small file, and mixed.
JOB_PAIRS = ((1, 1), (2, 2), (3, 3), (8, 8), (1, 8), (8, 1))


class RecordingHasher(Hasher):
    """A Hasher that notes which threads of this process computed leaf digests."""

    def __init__(self, key):
        super().__init__(key)
        self.threads, self.lock = set(), threading.Lock()

    def leaf(self, page):
        with self.lock:
            self.threads.add(threading.current_thread().name)
        return super().leaf(page)


def make_pools():
    """Serial, thread and process pools, for one key."""
    return ([HashPool(1)] + [HashPool(jobs, backend='thread') for jobs in (2, 3, 8)]
            + [HashPool(jobs, backend='process', key=KEY) for jobs in (2, 3)])


class ParallelTreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pools = make_pools()

    @classmethod
    def tearDownClass(cls):
        for pool in cls.pools:
            pool.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'f'

    def test_trees_are_identical_for_any_pool(self):
        rng = random.Random(21)
        cases = [(BLOCK, NODE_BLOCKS, size)
                 for size in (0, 1, BLOCK - 1, BLOCK, BLOCK + 1, MIB - 1, MIB, MIB + 1, 3 * MIB + 5000)]
        cases += [(4096, 3, int(4096 * pages)) for pages in (0.5, 7, 22.25)]
        cases += [(BLOCK, 1, 40 * BLOCK + 3), (512, 7, 50 * 512 + 1), (BLOCK, 4, 37 * BLOCK + 100)]
        # Nodes larger than one read (READ_BYTES): each node is read in several pieces.
        cases += [(BLOCK, 300, 7 * MIB + 3 * BLOCK + 5), (1 << 20, 3, 5 * MIB + 17)]
        for block, node_blocks, size in cases:
            data = rng.randbytes(size)
            self.path.write_bytes(data)
            leaves, nodes, digest = reference_tree(data, KEY, block, node_blocks)
            for pool in self.pools:
                with self.subTest(block=block, node_blocks=node_blocks, size=size, jobs=pool.jobs,
                                  backend=pool.backend):
                    tree = hash_file(self.path, KEY, block, node_blocks, pool=pool)
                    self.assertEqual((tree.size, tree.leaves, tree.nodes, tree.digest),
                                     (size, leaves, nodes, digest))
                    compact = hash_file(self.path, KEY, block, node_blocks, keep_leaves=False, pool=pool)
                    self.assertEqual((compact.size, compact.nodes, compact.digest, compact.leaf_blob),
                                     (size, nodes, digest, b''))

    def test_nodes_are_hashed_on_the_workers(self):
        self.path.write_bytes(random.Random(22).randbytes(64 * BLOCK))
        for pool in self.pools:
            with self.subTest(jobs=pool.jobs, backend=pool.backend):
                hasher = RecordingHasher(KEY)
                tree = hash_file(self.path, None, BLOCK, 4, hasher=hasher, pool=pool)
                self.assertEqual(tree.digest, reference_tree(self.path.read_bytes(), KEY, BLOCK, 4)[2])
                if pool.backend == 'serial':
                    self.assertEqual(hasher.threads, {threading.current_thread().name})
                elif pool.backend == 'thread':
                    self.assertTrue(hasher.threads)
                    self.assertTrue(all(name.startswith('pgdelta-hash') for name in hasher.threads))
                else:  # every page was hashed in a worker process, none in this one
                    self.assertEqual(hasher.threads, set())

    def test_a_single_node_file_is_hashed_in_the_calling_thread(self):
        self.path.write_bytes(random.Random(23).randbytes(3 * BLOCK))
        for pool in self.pools:
            with self.subTest(jobs=pool.jobs, backend=pool.backend):
                hasher = RecordingHasher(KEY)
                hash_file(self.path, None, BLOCK, 4, hasher=hasher, pool=pool)
                self.assertEqual(hasher.threads, {threading.current_thread().name})

    def test_a_file_whose_length_changes_while_hashed_is_hashed_again_in_one_pass(self):
        data = random.Random(24).randbytes(10 * BLOCK + 5)
        self.path.write_bytes(data)
        leaves, nodes, digest = reference_tree(data, KEY, BLOCK, 4)
        # The length seen at open is shorter (the file grew) or longer (it shrank) than what is read.
        for apparent in (len(data) - 3 * BLOCK, len(data) - 1, len(data) + 1, len(data) + 3 * BLOCK):
            for pool in self.pools:
                with self.subTest(apparent=apparent, jobs=pool.jobs, backend=pool.backend), \
                        mock.patch('pgdelta.tree._file_size', return_value=apparent):
                    tree = hash_file(self.path, KEY, BLOCK, 4, pool=pool)
                    self.assertEqual((tree.size, tree.leaves, tree.nodes, tree.digest),
                                     (len(data), leaves, nodes, digest))

    def test_a_process_pool_refuses_a_hasher_with_another_key(self):
        self.path.write_bytes(random.Random(25).randbytes(16 * BLOCK))
        with self.assertRaises(ValueError):
            hash_file(self.path, bytes(32), BLOCK, 4, pool=self.pools[-1])


class HashPoolTests(unittest.TestCase):
    def test_jobs_must_be_a_positive_integer(self):
        for jobs in (0, -1, 1.5, True, '2', None):
            with self.subTest(jobs=jobs), self.assertRaises(ValueError):
                HashPool(jobs)

    def test_backend_and_key_are_checked(self):
        for kwargs in ({'backend': 'fork'}, {'backend': None}, {'backend': 'process'},
                       {'backend': 'process', 'key': b'short'}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                HashPool(2, **kwargs)
        with HashPool(1, backend='process') as pool:  # one job needs no workers and no key
            self.assertEqual((pool.backend, pool.parallel), ('serial', False))

    def test_map_keeps_order(self):
        for jobs in (1, 2, 8):
            with self.subTest(jobs=jobs), HashPool(jobs, backend='thread') as pool:
                self.assertEqual(pool.map(lambda item: item * item, range(100)),
                                 [item * item for item in range(100)])
                self.assertEqual(pool.map(lambda item: item, []), [])
        with HashPool(3, backend='process', key=KEY) as pool:
            self.assertEqual(pool.map(abs, range(-50, 50)), [abs(item) for item in range(-50, 50)])

    def test_a_failure_leaves_no_task_running_and_stops_queueing(self):
        lock, state = threading.Lock(), {'running': 0, 'started': 0, 'most': 0}

        def task(item):
            with lock:
                state['running'] += 1
                state['started'] += 1
                state['most'] = max(state['most'], state['running'])
            try:
                time.sleep(0.002)
                if item == 5:
                    raise ValueError('boom')
                return item
            finally:
                with lock:
                    state['running'] -= 1

        with HashPool(4, backend='thread') as pool:
            with self.assertRaises(ValueError):
                pool.map(task, range(100))
            # Nothing still reads the caller's file once map() has returned.
            self.assertEqual(state['running'], 0)
            # At most 2 x jobs tasks are queued ahead of the one being collected.
            self.assertLessEqual(state['started'], 6 + 2 * 4)
            self.assertLessEqual(state['most'], 4)


class ParallelTransferTests(unittest.TestCase):
    """Whole sessions: everything except timings and switches must match the serial run exactly."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.basis = self.root / 'S', self.root / 'B'

    def build(self):
        build_shapes(self.source, self.basis)

    def run_all(self, pairs=JOB_PAIRS, **kwargs):
        expected_sent = differing_pages(self.source, self.basis)
        results = {}
        for sender_jobs, receiver_jobs in pairs:
            output = self.root / f'O-{sender_jobs}-{receiver_jobs}'
            outcome = transfer(self.source, self.basis, output, key=KEY, jobs=sender_jobs,
                               receiver_jobs=receiver_jobs, **kwargs)
            with self.subTest(jobs=(sender_jobs, receiver_jobs)):
                self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
                self.assertIsNone(outcome.receiver_error, repr(outcome.receiver_error))
                self.assertEqual(snapshot(output), snapshot(self.source))
                self.assertEqual(staging_dirs(output), [])
                self.assertEqual((outcome.sender['jobs'], outcome.sender['receiver']['jobs']),
                                 (sender_jobs, receiver_jobs))
                if outcome.sender['mode'] == 'reuse':
                    self.assertEqual(outcome.sender['pages']['sent'], expected_sent)
                results[(sender_jobs, receiver_jobs)] = comparable(outcome)
            remove_tree(output)
        serial = results[pairs[0]]
        self.assertIsNotNone(serial[0]['files_digest'])
        for pair, result in results.items():
            with self.subTest(compare=pair):
                self.assertEqual(result, serial)
        return serial

    def test_every_shape_gives_the_same_session_for_any_number_of_workers(self):
        self.build()
        sender, _, _ = self.run_all()
        self.assertEqual(sender['files'], {'total': 30, 'unchanged': 12, 'changed': 17, 'new': 1,
                                           'size_mismatch': 4})
        self.assertGreater(sender['nodes']['descended'], 0)
        self.assertGreater(sender['nodes']['sent_whole'], 0)

    def test_one_mib_nodes(self):
        rng = random.Random(606)
        old = rng.randbytes(3 * MIB + 5000)
        new = bytearray(old[:2 * MIB + 123]) + rng.randbytes(2 * MIB)
        for offset in (10, MIB + 7 * PAGE, 2 * MIB + 100):
            new[offset] ^= 0xFF
        write_tree(self.source, {'base/5/16384': bytes(new), 'base/5/16385': old})
        write_tree(self.basis, {'base/5/16384': old, 'base/5/16385': old})
        sender, _, _ = self.run_all(((1, 1), (4, 4), (8, 2)), node_blocks=NODE_BLOCKS)
        self.assertEqual(sender['node_blocks'], NODE_BLOCKS)
        self.assertEqual(sender['files']['unchanged'], 1)

    def test_leaf_digests_reread_from_the_basis_on_the_pool(self):
        self.build()
        with mock.patch('pgdelta.receiver.LEAF_CACHE_BYTES', 0):
            sender, receiver, _ = self.run_all(((1, 1), (3, 3), (8, 8)))
        self.assertGreater(receiver['leaf_nodes_reread'], 0)
        self.assertEqual(receiver['leaf_nodes_cached'], 0)
        self.assertGreater(sender['nodes']['descended'], 0)

    def test_full_mode_gives_the_same_session_for_any_number_of_workers(self):
        self.build()
        sender, _, _ = self.run_all(((1, 1), (8, 8)), reuse_any_basis=False)
        self.assertEqual(sender['mode'], 'full')
        self.assertEqual(sender['pages']['sent'], sender['pages']['total'])

    def test_jobs_must_be_positive(self):
        self.build()
        outcome = transfer(self.source, self.basis, self.root / 'O', jobs=0)
        self.assertIsInstance(outcome.sender_error, InvalidInput)
        outcome = transfer(self.source, self.basis, self.root / 'O2', receiver_jobs=0)
        self.assertIsInstance(outcome.receiver_error, InvalidInput)
        self.assertFalse((self.root / 'O').exists() or (self.root / 'O2').exists())


def build_shapes(source, basis):
    """S and B covering every file shape: unchanged, changed, grown, shrunk, emptied, new, deleted."""
    rng = random.Random(505)

    def data(count):
        return rng.randbytes(int(count * PAGE))

    def flip(blob, *offsets):
        blob = bytearray(blob)
        for offset in offsets:
            blob[offset] ^= 0xFF
        return bytes(blob)

    multi, changed, grows, shrinks = data(37 + 100 / PAGE), data(37 + 100 / PAGE), data(9), data(30)
    old = {
        'base/5/multi': multi,  # 10 four-page nodes, short last page, unchanged
        'base/5/changed': changed,
        'base/5/grows': grows,
        'base/5/shrinks': shrinks,
        'base/5/rewritten': data(12),
        'base/5/empty': b'',
        'base/5/becomes_empty': data(5),
        'base/5/was_empty': b'',
        'base/5/deleted': data(3),
        'global/pg_control': data(1),
    }
    new = {
        'base/5/multi': multi,
        # pages 0, 5, 6, 17 and the short last page change
        'base/5/changed': flip(changed, 3, 5 * PAGE, 6 * PAGE + 9, 17 * PAGE + 100, 37 * PAGE + 50),
        # grows past several node boundaries; page 2 changes too
        'base/5/grows': flip(grows, 2 * PAGE) + data(14 + 5 / PAGE),
        # shrinks to mid-node and mid-page; page 1 changes too
        'base/5/shrinks': flip(shrinks[:13 * PAGE + 77], PAGE + 1),
        'base/5/rewritten': data(12),
        'base/5/empty': b'',
        'base/5/becomes_empty': b'',
        'base/5/was_empty': data(6 + 1 / PAGE),
        'base/5/new': data(7.5),
        'global/pg_control': flip(old['global/pg_control'], 0),
    }
    for number in range(20):
        small = data(rng.randint(1, 3 * PAGE) / PAGE)
        old[f'base/5/small_{number}'] = small
        new[f'base/5/small_{number}'] = flip(small, 0) if number % 2 else small
    write_tree(source, new, ['pg_wal/archive_status'])
    write_tree(basis, old, ['pg_wal/archive_status'])


if __name__ == '__main__':
    unittest.main()
