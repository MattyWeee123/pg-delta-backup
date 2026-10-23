"""EXP-06 engine changes: worker processes (A), copy-then-patch (B), pipelined verification (C).

Every switch combination and worker count must give the same output and
the same statistics, apart from timings and the switches themselves.
Failures in the middle of a run, signals included, must leave no thread
or process behind and publish nothing.
"""
import base64
import errno
import multiprocessing
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

from pgdelta import protocol, receiver as receiver_module, sender as sender_module, tree
from pgdelta.errors import PeerError, ProtocolError, TransferError, VerificationError
from pgdelta.hashing import BLOCK, Hasher
from pgdelta.protocol import Channel
from pgdelta.receiver import _BasisShort, _Copier, receipt_path, remove_tree
from pgdelta.tree import HashingStopped, HashPool, hash_file

from pgdelta_support import (PAGE, REPO_ROOT, Outcome, channel_pair, comparable, differing_pages,
                             pgdelta_threads, run_receiver_thread, snapshot, staging_dirs, subprocess_env,
                             transfer, write_tree)
from test_pgdelta_failures import CorruptingChannel
from test_pgdelta_hashing import reference_tree
from test_pgdelta_parallel import build_shapes

KEY = bytes(range(32))
MIB = 1 << 20
SETTLE_SECONDS = 10.0


def engine(jobs=1, backend='process', rebuild='copy-patch', verify='pipelined', sender_jobs=None):
    """transfer() keywords for one engine configuration (the sender uses the same jobs unless told)."""
    return {'jobs': jobs if sender_jobs is None else sender_jobs, 'hash_backend': backend,
            'receiver_jobs': jobs,
            'receiver_options': {'hash_backend': backend, 'rebuild': rebuild, 'verify': verify}}


# label: configuration. The EXP-06 cells (baseline, A only, B only, A + B + C), the EXP-05 thread
# backend, and the remaining mixtures of B and C.
CONFIGS = {
    'baseline': engine(1, rebuild='gap-fill', verify='sequential'),
    'B only': engine(1, rebuild='copy-patch', verify='sequential'),
    'A+B+C N=1': engine(1),
    'A only N=2': engine(2, rebuild='gap-fill', verify='sequential'),
    'A only N=3': engine(3, rebuild='gap-fill', verify='sequential'),
    'A+B+C N=2': engine(2),
    'A+B+C N=3': engine(3),
    'A+B N=3': engine(3, verify='sequential'),
    'A+C N=2': engine(2, rebuild='gap-fill'),
    'A+B+C mixed N': engine(3, sender_jobs=1),
    'threads EXP-05': engine(3, backend='thread', rebuild='gap-fill', verify='sequential'),
    'threads B+C': engine(2, backend='thread'),
}


def expected_switches(config):
    jobs, options = config['receiver_jobs'], config['receiver_options']
    backend = 'serial' if jobs == 1 else options['hash_backend']
    sender_jobs = config['jobs']
    return {'sender': {'jobs': sender_jobs, 'hash_backend': 'serial' if sender_jobs == 1 else options['hash_backend']},
            'receiver': {'jobs': jobs, 'hash_backend': backend, 'rebuild': options['rebuild'],
                         'verify': 'pipelined' if jobs > 1 and options['verify'] == 'pipelined' else 'sequential'}}


def settle(test):
    """Wait until no pgdelta thread or worker process of this process is left; fail if some stay."""
    deadline = time.monotonic() + SETTLE_SECONDS
    while time.monotonic() < deadline and (pgdelta_threads() or multiprocessing.active_children()):
        time.sleep(0.02)
    test.assertEqual(pgdelta_threads(), [])
    test.assertEqual(multiprocessing.active_children(), [])


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.basis, self.output = self.root / 'S', self.root / 'B', self.root / 'O'

    def assert_nothing_published(self, output=None):
        output = output or self.output
        self.assertFalse(os.path.lexists(output))
        self.assertFalse(os.path.lexists(receipt_path(output)))
        self.assertEqual(staging_dirs(output), [])

    def run_configs(self, configs=CONFIGS, **kwargs):
        """Run every configuration; check output and switches; return {label: comparable statistics}."""
        expected_sent = differing_pages(self.source, self.basis)
        results = {}
        for label, config in configs.items():
            output = self.root / f'O-{label.replace(" ", "_")}'
            outcome = transfer(self.source, self.basis, output, key=KEY, **config, **kwargs)
            with self.subTest(config=label):
                self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
                self.assertIsNone(outcome.receiver_error, repr(outcome.receiver_error))
                self.assertEqual(snapshot(output), snapshot(self.source))
                self.assertEqual(staging_dirs(output), [])
                self.assertEqual(outcome.sender['switches'], expected_switches(config))
                self.assertEqual(outcome.receiver['switches'], expected_switches(config)['receiver'])
                if outcome.sender['mode'] == 'reuse':
                    self.assertEqual(outcome.sender['pages']['sent'], expected_sent)
                self.assertEqual(outcome.receiver['files_verified'], outcome.sender['files']['total'])
                results[label] = (comparable(outcome), outcome)
            remove_tree(output)
        first = next(iter(results))
        for label, (result, _) in results.items():
            with self.subTest(compare=label):
                self.assertEqual(result, results[first][0])
        settle(self)
        return results


class SwitchMatrixTests(EngineTestCase):
    def test_every_shape_gives_the_same_session_for_every_switch_and_worker_count(self):
        build_shapes(self.source, self.basis)
        results = self.run_configs()
        sender = results['baseline'][0][0]
        self.assertEqual(sender['files'], {'total': 30, 'unchanged': 12, 'changed': 17, 'new': 1,
                                           'size_mismatch': 4})
        for label, (_, outcome) in results.items():
            copied = outcome.receiver['basis_copy_bytes']
            with self.subTest(config=label):
                if outcome.receiver['switches']['rebuild'] == 'gap-fill':
                    self.assertEqual(copied, outcome.receiver['bytes_copied'])
                else:  # every byte of B is copied at most once, so at most the shorter length of each file
                    self.assertGreaterEqual(copied, outcome.receiver['bytes_copied'])
                    self.assertLessEqual(copied, shorter_lengths(self.source, self.basis))

    def test_small_copy_pieces_and_leaf_rereads(self):
        # Copy-ahead and copy buffers far smaller than a node and not page-aligned, so pieces start
        # and end mid-page and every file needs many of them; and no leaf cache, so B is re-read.
        build_shapes(self.source, self.basis)
        default = self.run_configs({'B only': CONFIGS['B only']})['B only'][1].receiver
        configs = {label: CONFIGS[label] for label in ('baseline', 'B only', 'A+B+C N=3', 'threads B+C')}
        with mock.patch('pgdelta.receiver.COPY_AHEAD_BYTES', 2 * PAGE + 1), \
                mock.patch('pgdelta.receiver.COPY_CHUNK_BYTES', 3 * PAGE + 7), \
                mock.patch('pgdelta.receiver.LEAF_CACHE_BYTES', 0):
            results = self.run_configs(configs)
        receiver = results['B only'][1].receiver
        self.assertGreater(receiver['basis_copies'], default['basis_copies'])
        self.assertGreater(receiver['basis_copy_bytes'], 0)
        self.assertGreater(receiver['leaf_nodes_reread'], 0)

    def test_full_mode_for_every_switch(self):
        build_shapes(self.source, self.basis)
        configs = {label: CONFIGS[label] for label in ('baseline', 'B only', 'A+B+C N=3', 'A only N=2')}
        results = self.run_configs(configs, reuse_any_basis=False)
        for _, outcome in results.values():
            self.assertEqual(outcome.sender['mode'], 'full')
            self.assertEqual(outcome.receiver['basis_copy_bytes'], 0)

    def test_one_mib_nodes_across_a_large_file(self):
        rng = random.Random(606)
        old = rng.randbytes(9 * MIB + 5000)
        new = bytearray(old[:7 * MIB + 123]) + rng.randbytes(3 * MIB)
        for offset in range(10, 7 * MIB, 37 * PAGE + 11):  # scattered changes, like EXP-05's uniform 1%
            new[offset] ^= 0xFF
        write_tree(self.source, {'base/5/16384': bytes(new), 'base/5/16385': old, 'base/5/16386': old[:MIB]})
        write_tree(self.basis, {'base/5/16384': old, 'base/5/16385': old, 'base/5/16386': old})
        configs = {label: CONFIGS[label] for label in ('baseline', 'B only', 'A+B+C N=3', 'A only N=3')}
        results = self.run_configs(configs, node_blocks=128)
        copy_patch, gap_fill = results['B only'][1].receiver, results['baseline'][1].receiver
        self.assertLess(copy_patch['basis_copies'], gap_fill['basis_copies'])

    def test_copy_patch_copies_nothing_for_a_file_sent_whole_and_all_of_an_unchanged_one(self):
        rng = random.Random(607)
        old = rng.randbytes(40 * PAGE)
        write_tree(self.source, {'rewritten': rng.randbytes(40 * PAGE)})
        write_tree(self.basis, {'rewritten': old})
        outcome = transfer(self.source, self.basis, self.output, **CONFIGS['B only'])
        self.assertIsNone(outcome.receiver_error)
        self.assertEqual((outcome.receiver['basis_copy_bytes'], outcome.receiver['basis_copies']), (0, 0))
        remove_tree(self.output)
        write_tree(self.source, {'rewritten': old})
        outcome = transfer(self.source, self.basis, self.output, **CONFIGS['B only'])
        self.assertEqual((outcome.receiver['basis_copy_bytes'], outcome.receiver['basis_copies']),
                         (40 * PAGE, 1))
        self.assertEqual(outcome.receiver['whole_file_copies'], 1)
        self.assertEqual(snapshot(self.output), snapshot(self.source))

    def test_growth_shrinkage_new_and_deleted_files_under_copy_patch(self):
        rng = random.Random(608)
        base = rng.randbytes(int(13.5 * PAGE))
        source = {'grows_from_short_page': base + b'tail', 'grows_by_nodes': base + rng.randbytes(9 * PAGE),
                  'shrinks_mid_page': base[:5 * PAGE + 77], 'shrinks_to_page': base[:6 * PAGE],
                  'shrinks_to_zero': b'', 'from_zero': rng.randbytes(3 * PAGE), 'new': rng.randbytes(PAGE + 1)}
        basis = {'grows_from_short_page': base, 'grows_by_nodes': base, 'shrinks_mid_page': base,
                 'shrinks_to_page': base, 'shrinks_to_zero': base, 'from_zero': b'', 'deleted': base}
        write_tree(self.source, source)
        write_tree(self.basis, basis)
        results = self.run_configs({label: CONFIGS[label] for label in ('baseline', 'B only', 'A+B+C N=2')})
        _, outcome = results['B only']
        # Pages past B's end are always sent; shrinking files send only a new short last page.
        self.assertEqual(outcome.sender['pages']['sent'], differing_pages(self.source, self.basis))
        self.assertLessEqual(outcome.receiver['basis_copy_bytes'], shorter_lengths(self.source, self.basis))


def shorter_lengths(source, basis):
    """The sum, over files in both S and B, of the shorter of the two lengths."""
    total = 0
    for path in Path(source).rglob('*'):
        other = Path(basis) / path.relative_to(source)
        if path.is_file() and other.is_file():
            total += min(path.stat().st_size, other.stat().st_size)
    return total


class SerialMeansInProcessTests(EngineTestCase):
    def test_jobs_1_starts_no_worker_thread_or_process(self):
        build_shapes(self.source, self.basis)
        refuse = mock.Mock(side_effect=AssertionError('jobs 1 must not start workers'))
        with mock.patch('pgdelta.tree.ProcessPoolExecutor', refuse), \
                mock.patch('pgdelta.tree.ThreadPoolExecutor', refuse), \
                mock.patch('pgdelta.receiver._Verifier', refuse):
            for rebuild in ('copy-patch', 'gap-fill'):
                for verify in ('pipelined', 'sequential'):
                    for backend in ('process', 'thread'):
                        output = self.root / f'O-{rebuild}-{verify}-{backend}'
                        with self.subTest(rebuild=rebuild, verify=verify, backend=backend):
                            outcome = transfer(self.source, self.basis, output,
                                               **engine(1, backend, rebuild, verify))
                            self.assertIsNone(outcome.receiver_error, repr(outcome.receiver_error))
                            self.assertEqual(snapshot(output), snapshot(self.source))
                            self.assertEqual(outcome.receiver['switches']['verify'], 'sequential')
                            self.assertEqual(outcome.sender['hash_backend'], 'serial')
        refuse.assert_not_called()

    def test_default_jobs_is_one(self):
        self.assertEqual(tree.DEFAULT_JOBS, 1)
        build_shapes(self.source, self.basis)
        outcome = transfer(self.source, self.basis, self.output)
        self.assertEqual(outcome.sender['switches'], {
            'sender': {'jobs': 1, 'hash_backend': 'serial'},
            'receiver': {'jobs': 1, 'hash_backend': 'serial', 'rebuild': 'copy-patch', 'verify': 'sequential'}})


class HashPoolShutdownTests(unittest.TestCase):
    """EXP-05's deadlock: close() while another thread is inside map() must not hang that thread."""

    def check_close_during_map(self, pool):
        outcome = {}

        def caller():
            try:
                outcome['result'] = pool.map(time.sleep, [0.02] * 300)
            except BaseException as exc:
                outcome['error'] = exc
        thread = threading.Thread(target=caller, name='test-map-caller', daemon=True)
        thread.start()
        time.sleep(0.3)
        pool.close()
        thread.join(15)
        self.assertFalse(thread.is_alive(), 'map() hung after close() from another thread')
        self.assertIsInstance(outcome.get('error'), HashingStopped)
        self.assertNotIn('result', outcome)

    def test_close_from_another_thread_during_map_thread_backend(self):
        self.check_close_during_map(HashPool(2, backend='thread'))

    def test_close_from_another_thread_during_map_process_backend(self):
        self.check_close_during_map(HashPool(2, backend='process', key=KEY))
        self.assertEqual(multiprocessing.active_children(), [])

    def test_stop_event_ends_map_before_the_next_task(self):
        stop = threading.Event()
        calls = []

        def task(item):
            calls.append(item)
            if item == 3:
                stop.set()
            return item
        for pool in (HashPool(1), HashPool(2, backend='thread')):
            with self.subTest(backend=pool.backend), pool:
                calls.clear()
                stop.clear()
                with self.assertRaises(HashingStopped):
                    pool.map(task, range(50), stop=stop, window=1)
                self.assertLessEqual(len(calls), 5)

    def test_a_broken_process_pool_is_a_transfer_error(self):
        with HashPool(2, backend='process', key=KEY) as pool:
            with self.assertRaises(TransferError):
                pool.map(os._exit, [1, 1, 1])

    def test_close_twice_and_after_use(self):
        pool = HashPool(2, backend='process', key=KEY)
        self.assertEqual(pool.map(abs, [-1, -2, -3]), [1, 2, 3])
        pool.close()
        pool.close()
        with self.assertRaises(HashingStopped):
            pool.map(abs, [-1, -2])


class BoundedBufferTests(unittest.TestCase):
    """EXP-05 review m1: read buffers stay at READ_BYTES per thread however large a node is."""

    def test_reads_are_bounded_for_large_nodes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'f'
            data = random.Random(70).randbytes(5 * MIB + 3 * BLOCK + 9)
            path.write_bytes(data)
            node_blocks = 512  # 4 MiB nodes, four times READ_BYTES
            leaves, nodes, digest = reference_tree(data, KEY, BLOCK, node_blocks)
            lengths, real = [], tree._read_span

            def recording(fd, length, offset):
                lengths.append(length)
                return real(fd, length, offset)
            for pool in (HashPool(1), HashPool(3, backend='thread')):
                with self.subTest(backend=pool.backend), pool, \
                        mock.patch('pgdelta.tree._read_span', recording):
                    lengths.clear()
                    result = hash_file(path, KEY, BLOCK, node_blocks, pool=pool)
                    self.assertEqual((result.leaves, result.nodes, result.digest), (leaves, nodes, digest))
                    self.assertTrue(lengths)
                    self.assertLessEqual(max(lengths), tree.READ_BYTES)
            self.assertLessEqual(len(tree._local.buffer), tree.READ_BYTES)


class CopierTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = random.Random(71).randbytes(3 * MIB + 1234)
        source, target = Path(temp.name) / 'src', Path(temp.name) / 'dst'
        source.write_bytes(self.data)
        self.source = os.open(source, os.O_RDONLY)
        self.target = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, self.source)
        self.addCleanup(os.close, self.target)
        self.target_path = target

    def check_copy(self, copier, offset, length):
        os.ftruncate(self.target, 0)
        copier.copy(self.source, self.target, offset, length)
        written = self.target_path.read_bytes()
        self.assertEqual(written[offset:], self.data[offset:offset + length])
        self.assertEqual(written[:offset], bytes(offset))

    def test_buffered_copy_of_any_range(self):
        for offset, length in ((0, len(self.data)), (5, 7), (PAGE, 2 * MIB + 3), (len(self.data) - 1, 1)):
            with self.subTest(offset=offset, length=length):
                self.check_copy(_Copier(kernel=False), offset, length)

    def test_a_basis_shorter_than_the_range_is_reported(self):
        with self.assertRaises(_BasisShort):
            _Copier(kernel=False).copy(self.source, self.target, len(self.data) - 10, 11)

    def fake_kernel_copy(self, behaviour):
        """A stand-in for os.copy_file_range: 'partial' copies at most 1000 bytes per call."""
        calls = []

        def copy_file_range(src, dst, count, offset_src, offset_dst):
            calls.append(count)
            if behaviour == 'exdev':
                raise OSError(errno.EXDEV, 'cross-device')
            if behaviour == 'eio':
                raise OSError(errno.EIO, 'I/O error')
            if behaviour == 'zero':
                return 0
            data = os.pread(src, min(count, 1000), offset_src)
            return os.pwrite(dst, data, offset_dst)
        return calls, mock.patch('pgdelta.receiver.os.copy_file_range', copy_file_range, create=True)

    def test_kernel_copy_and_its_fallbacks(self):
        for behaviour in ('partial', 'exdev', 'zero'):
            with self.subTest(behaviour=behaviour):
                calls, patch = self.fake_kernel_copy(behaviour)
                copier = _Copier(kernel=True)
                with patch:
                    self.check_copy(copier, 77, 5000)
                if behaviour == 'partial':
                    self.assertEqual(len(calls), 5)
                    self.assertTrue(copier.kernel)
                else:  # refused or copied nothing: copy by hand from then on
                    self.assertEqual(len(calls), 1)
                    self.assertFalse(copier.kernel)

    def test_a_real_kernel_copy_error_is_raised(self):
        _, patch = self.fake_kernel_copy('eio')
        with patch, self.assertRaises(OSError):
            _Copier(kernel=True).copy(self.source, self.target, 0, 100)


class BasisChangedTests(EngineTestCase):
    """B must be immutable; if it changes while O is built from it, verification must catch it."""

    def setUp(self):
        super().setUp()
        rng = random.Random(72)
        self.old = rng.randbytes(40 * PAGE)
        new = bytearray(self.old)
        new[30 * PAGE + 5] ^= 0xFF
        write_tree(self.source, {'base/1/rel': bytes(new)})
        write_tree(self.basis, {'base/1/rel': self.old})

    def corrupt_basis_once(self):
        """Flip a byte of B in page 3, which S and B share and so is never sent."""
        if not self.corrupted:
            self.corrupted = True
            with open(self.basis / 'base' / '1' / 'rel', 'r+b') as stream:
                stream.seek(3 * PAGE + 100)
                byte = stream.read(1)
                stream.seek(3 * PAGE + 100)
                stream.write(bytes([byte[0] ^ 0x01]))

    def test_basis_changed_during_the_copy_fails_verification(self):
        real_copy, real_pread = _Copier.copy, receiver_module.pread_exact

        def copy(copier, *args):
            self.corrupt_basis_once()
            return real_copy(copier, *args)

        def pread(*args):
            self.corrupt_basis_once()
            return real_pread(*args)
        for label in ('baseline', 'B only', 'A+B+C N=2', 'A+C N=2', 'threads B+C'):
            with self.subTest(config=label):
                write_tree(self.basis, {'base/1/rel': self.old})
                self.corrupted = False
                with mock.patch.object(_Copier, 'copy', copy), mock.patch('pgdelta.receiver.pread_exact', pread):
                    outcome = transfer(self.source, self.basis, self.output, **CONFIGS[label])
                self.assertTrue(self.corrupted)
                self.assertIsInstance(outcome.receiver_error, VerificationError)
                self.assertIsInstance(outcome.sender_error, PeerError)
                self.assertEqual(outcome.sender_error.exit_code, 4)
                self.assert_nothing_published()
        settle(self)

    def test_basis_that_changed_size_is_refused(self):
        real_open = receiver_module._Receiver._open_basis

        def shrink_first(receiver, path, size):
            os.truncate(self.basis / path, size - 1)
            return real_open(receiver, path, size)
        with mock.patch.object(receiver_module._Receiver, '_open_basis', shrink_first):
            outcome = transfer(self.source, self.basis, self.output, **CONFIGS['A+B+C N=2'])
        self.assertIsInstance(outcome.receiver_error, TransferError)
        self.assertIn('changed size', str(outcome.receiver_error))
        self.assertEqual(outcome.sender_error.exit_code, 3)
        self.assert_nothing_published()
        settle(self)


class GapProtocolTests(EngineTestCase):
    """A page neither sent nor available at the same offset and length in B is a protocol error."""

    def fake_sender(self, size, frames, rebuild):
        send_channel, receive_channel = channel_pair()
        outcome = Outcome()
        thread = run_receiver_thread(receive_channel, outcome, self.basis, self.output, reuse_any_basis=True,
                                     rebuild=rebuild)
        hello = {'protocol_version': 1, 'block': PAGE, 'node_blocks': 4,
                 'key': base64.b64encode(bytes(32)).decode(), 'source_identity': None, 'root_mode': 0o700,
                 'directories': [['base', 0o700]], 'files': [['base/f', size, 0o600]]}
        replies = []
        try:
            send_channel.send_json(protocol.HELLO, hello)
            for code, payload in frames:
                send_channel.send(code, payload)
            send_channel._writer.close()
            while True:
                message = send_channel.recv()
                if message is None:
                    break
                replies.append(message)
        except protocol.ChannelClosed:
            pass
        finally:
            send_channel.close()
        thread.join(30)
        return outcome, replies

    def test_pages_missing_from_the_sender_and_the_basis(self):
        rng = random.Random(73)
        cases = {
            # B has 1 page, S has 3: pages 1-2 lie past B's end and must be sent.
            'past the end of B': (rng.randbytes(PAGE), 3 * PAGE, [0]),
            # B's last page is short; S's is full: page 2 must be sent.
            'last page of B is short': (rng.randbytes(2 * PAGE + 10), 3 * PAGE, [0, 1]),
            # S's last page is short; B's is full: page 2 must be sent.
            'last page of S is short': (rng.randbytes(3 * PAGE), 2 * PAGE + 10, [0, 1]),
            # The file is not in B at all.
            'no basis file': (None, 2 * PAGE, [0]),
        }
        for label, (old, size, sent) in cases.items():
            for rebuild in ('copy-patch', 'gap-fill'):
                with self.subTest(case=label, rebuild=rebuild):
                    remove_tree(self.basis)
                    write_tree(self.basis, {} if old is None else {'base/f': old}, ['base'])
                    data = rng.randbytes(size)
                    frames = [(protocol.PAGE_DATA, protocol.page_data_head(0, page)
                               + data[page * PAGE:(page + 1) * PAGE]) for page in sent]
                    frames.append((protocol.FILE_DONE, protocol.encode_file_done(0, size, bytes(16))))
                    outcome, replies = self.fake_sender(size, frames, rebuild)
                    self.assertIsInstance(outcome.receiver_error, ProtocolError, repr(outcome.receiver_error))
                    self.assertIn('neither sent nor', str(outcome.receiver_error))
                    # The basis thread may still send BASIS_NODES after the main loop's ERROR.
                    errors = [protocol.decode_json(payload, 'ERROR') for code, payload in replies
                              if code == protocol.ERROR]
                    self.assertEqual([error['exit_code'] for error in errors], [3])
                    self.assert_nothing_published()


class MidRunFailureTests(EngineTestCase):
    """Failures in the middle of a run: a clean failure, nothing published, no thread or worker left."""

    def setUp(self):
        super().setUp()
        rng = random.Random(74)
        files = {f'base/5/{16384 + number}': rng.randbytes(2 * MIB + number * PAGE) for number in range(4)}
        changed = {path: bytes(blob[:PAGE]) + bytes(PAGE) + blob[2 * PAGE:] for path, blob in files.items()}
        write_tree(self.source, changed)
        write_tree(self.basis, files)

    def configs(self):
        return {label: CONFIGS[label] for label in ('baseline', 'A+B+C N=1', 'A+B+C N=3', 'A only N=2',
                                                    'threads B+C')}

    def check_failure(self, make_outcome, expected_receiver=Exception):
        for label, config in self.configs().items():
            with self.subTest(config=label):
                outcome = make_outcome(config)
                self.assertIsNotNone(outcome.sender_error)
                self.assertIsInstance(outcome.receiver_error, expected_receiver)
                self.assert_nothing_published()
                settle(self)

    def test_sender_fails_while_the_receiver_hashes_the_basis(self):
        real_plan = sender_module._Sender._plan

        def failing_plan(sender, index, entry):
            if index == 1:
                raise TransferError('injected sender failure')
            return real_plan(sender, index, entry)
        with mock.patch.object(sender_module._Sender, '_plan', failing_plan), \
                mock.patch('pgdelta.receiver.JOIN_SECONDS', 0.0):
            self.check_failure(lambda config: transfer(self.source, self.basis, self.output, node_blocks=8,
                                                       **config), PeerError)

    def test_corruption_in_transit_fails_verification_for_every_switch(self):
        def run(config):
            CorruptingChannel.corrupted = 0
            outcome = transfer(self.source, self.basis, self.output, sender_class=CorruptingChannel, **config)
            self.assertEqual(outcome.sender_error.exit_code, 4)
            return outcome
        self.check_failure(run, VerificationError)

    def test_receiver_interrupted_while_writing(self):
        class InterruptingChannel(Channel):
            def recv(self):
                message = super().recv()
                if message is not None and message[0] == protocol.FILE_DONE and message[1][:4] == b'\0\0\0\2':
                    raise KeyboardInterrupt
                return message
        real_pair = channel_pair

        def pair(sender_class=Channel, receiver_class=Channel):
            return real_pair(sender_class, InterruptingChannel)
        with mock.patch('pgdelta_support.channel_pair', pair):
            self.check_failure(lambda config: transfer(self.source, self.basis, self.output, **config),
                               KeyboardInterrupt)

    def test_hashing_workers_killed_mid_run(self):
        class KillingChannel(Channel):
            def send(self, code, *parts):
                if code == protocol.PAGE_DATA:
                    for child in multiprocessing.active_children():
                        os.kill(child.pid, signal.SIGKILL)
                super().send(code, *parts)
        for label in ('A+B+C N=3', 'A only N=2'):
            with self.subTest(config=label):
                outcome = transfer(self.source, self.basis, self.output, sender_class=KillingChannel,
                                   **CONFIGS[label])
                failures = [outcome.sender_error, outcome.receiver_error]
                self.assertEqual([error.exit_code for error in failures], [3, 3], repr(failures))
                self.assertTrue(any('hashing worker' in str(error) for error in failures), repr(failures))
                self.assert_nothing_published()
                settle(self)


SLOW_WORKER_ENV = 'PGDELTA_TEST_SLOW_WORKER'
SLOW_WORKER_SECONDS = 1.5


def slow_first_worker_init(key, *rest):
    """A worker initializer under which the first worker to start takes SLOW_WORKER_SECONDS longer.

    Patched in for tree._worker_init: spawn pickles the initializer by name,
    so the workers import this module and run it, then the real one.
    """
    try:
        os.close(os.open(os.environ[SLOW_WORKER_ENV], os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        time.sleep(SLOW_WORKER_SECONDS)
    except FileExistsError:
        pass
    tree._worker_init(key, *rest)


# Run in a child process: hash `path` `passes` times on HashPool(jobs) (jobs 1: in-process), with
# sys.platform reading "linux" while the pool starts, so the start method chosen is Linux's.
CPU_CHILD = textwrap.dedent("""
    import sys
    from unittest import mock
    from pgdelta.tree import HashPool, hash_file
    if __name__ == '__main__':
        path, jobs, passes = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
        key = bytes(range(32))
        with mock.patch.object(sys, 'platform', 'linux'):
            pool = HashPool(jobs, key=key)
        try:
            for _ in range(passes):
                hash_file(path, key, pool=pool)
        finally:
            pool.close()
""")


class StartMethodTests(unittest.TestCase):
    def test_spawn_on_every_platform(self):
        # Linux used to get "forkserver": its workers are children of the fork server, which no one
        # waits for, so their CPU never reached wait4/RUSAGE_CHILDREN of whoever ran pgdelta.
        environ = {key: value for key, value in os.environ.items() if key != tree.START_METHOD_ENV}
        for platform in ('linux', 'darwin', 'freebsd14'):
            with self.subTest(platform=platform), mock.patch.object(sys, 'platform', platform), \
                    mock.patch.dict(os.environ, environ, clear=True):
                self.assertEqual(tree.start_method(), 'spawn')

    def test_workers_are_children_of_the_process_that_owns_the_pool(self):
        environ = {key: value for key, value in os.environ.items() if key != tree.START_METHOD_ENV}
        with mock.patch.dict(os.environ, environ, clear=True):
            with mock.patch.object(sys, 'platform', 'linux'):
                pool = HashPool(3, key=KEY)
            try:
                deadline = time.monotonic() + 60
                while len(pool._executor._processes) < 3 and time.monotonic() < deadline:
                    time.sleep(0.01)
                workers = sorted(pool._executor._processes)
                parents = subprocess.run(['ps', '-o', 'ppid=', '-p', ','.join(map(str, workers))],
                                         capture_output=True, text=True, check=True).stdout.split()
            finally:
                pool.close()
        self.assertEqual(len(workers), 3)
        self.assertEqual([int(pid) for pid in parents], [os.getpid()] * 3)

    @unittest.skipUnless(hasattr(os, 'wait4'), 'needs os.wait4')
    def test_the_workers_cpu_reaches_whoever_ran_the_pool(self):
        # wait4 of the pool's process, as EXP-04 (wait4), EXP-06 (RUSAGE_CHILDREN) and time(1)
        # measure pgdelta: with the workers' CPU included, hashing on 2 workers costs at least
        # about what it costs in-process. Without it (forkserver) it was under a third.
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'f'
            with open(path, 'wb') as stream:
                block = random.Random(77).randbytes(MIB)
                for _ in range(48):
                    stream.write(block)
            environ = {key: value for key, value in subprocess_env().items() if key != tree.START_METHOD_ENV}

            def tree_cpu(jobs):
                process = subprocess.Popen([sys.executable, '-c', CPU_CHILD, str(path), str(jobs), '4'],
                                           cwd=REPO_ROOT, env=environ)
                _, status, usage = os.wait4(process.pid, 0)
                process.returncode = os.waitstatus_to_exitcode(status)
                self.assertEqual(process.returncode, 0)
                return usage.ru_utime + usage.ru_stime

            serial, pooled = tree_cpu(1), tree_cpu(2)
        self.assertGreater(pooled, 0.7 * serial, f'2 workers: {pooled:.2f} s CPU, in-process: {serial:.2f} s')

    def test_never_fork(self):
        method = tree.start_method()
        self.assertEqual(method, 'spawn')
        for value in ('fork', 'thread', 'nonsense'):
            with self.subTest(value=value), mock.patch.dict(os.environ, {tree.START_METHOD_ENV: value}), \
                    self.assertRaises(ValueError):
                tree.start_method()

    @unittest.skipUnless('forkserver' in multiprocessing.get_all_start_methods(), 'no forkserver here')
    def test_forkserver_workers_give_the_same_trees(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'f'
            data = random.Random(75).randbytes(3 * MIB + 77)
            path.write_bytes(data)
            with mock.patch.dict(os.environ, {tree.START_METHOD_ENV: 'forkserver'}):
                with HashPool(2, backend='process', key=KEY) as pool:
                    result = hash_file(path, KEY, BLOCK, 4, pool=pool)
            self.assertEqual(result.digest, reference_tree(data, KEY, BLOCK, 4)[2])
        self.assertEqual(multiprocessing.active_children(), [])


class PoolReadyTests(unittest.TestCase):
    def test_ready_waits_for_every_worker_but_work_does_not(self):
        # ready_seconds is the time until all `jobs` workers had started. It used to be set once
        # `jobs` pings were answered, which the first worker up could do alone (EXP-06 review).
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'f'
            data = random.Random(78).randbytes(4 * MIB + 5)
            path.write_bytes(data)
            with mock.patch.dict(os.environ, {SLOW_WORKER_ENV: str(Path(temp) / 'slow')}), \
                    mock.patch.object(tree, '_worker_init', slow_first_worker_init):
                pool = HashPool(3, key=KEY)
            try:
                result = hash_file(path, KEY, BLOCK, 4, pool=pool)  # on the workers already up
                self.assertIsNone(pool.ready_seconds, 'hashing waited for the slow worker, or the slow '
                                                      'worker was not waited for')
                deadline = time.monotonic() + 60
                while pool.ready_seconds is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertGreaterEqual(pool.ready_seconds, SLOW_WORKER_SECONDS)
                first, pids, workers = pool.first_ready_seconds, pool.ready_pids, set(pool._executor._processes)
            finally:
                pool.close()
        self.assertEqual(result.digest, reference_tree(data, KEY, BLOCK, 4)[2])
        self.assertLess(first, SLOW_WORKER_SECONDS)
        self.assertEqual(len(pids), 3)
        self.assertEqual(pids, workers)
        self.assertEqual(multiprocessing.active_children(), [])

    def test_ready_times_without_worker_processes(self):
        for pool in (HashPool(1), HashPool(2, backend='thread')):
            with pool:
                self.assertEqual((pool.first_ready_seconds, pool.ready_seconds, pool.start_method), (0.0, 0.0, None))

    def test_a_pool_closed_before_its_workers_start_leaves_nothing_behind(self):
        for _ in range(3):
            pool = HashPool(4, key=KEY)
            pool.close()
            self.assertFalse(pool._ready_thread.is_alive())
            self.assertTrue(all(end.closed for end in pool._ready_ends))
        settle(self)


@unittest.skipIf(os.name == 'nt', 'needs POSIX signals and process groups')
class WorkerLifetimeTests(unittest.TestCase):
    """Worker processes never outlive the run: not on SIGKILL of their parent, nor on any signal."""

    def wait_gone(self, pids=(), group=None, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            alive = []
            for pid in pids:
                try:
                    os.kill(pid, 0)
                    alive.append(pid)
                except ProcessLookupError:
                    pass
            if group is not None:
                try:
                    os.killpg(group, 0)
                    alive.append(f'group {group}')
                except ProcessLookupError:
                    pass
            if not alive:
                return []
            time.sleep(0.05)
        return alive

    def kill_group_on_failure(self, group):
        def cleanup():
            try:
                os.killpg(group, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self.addCleanup(cleanup)

    def test_workers_exit_when_their_parent_is_killed(self):
        script = textwrap.dedent("""
            import multiprocessing, os, signal, sys, time
            from pgdelta.tree import HashPool
            if __name__ == '__main__':
                pool = HashPool(3, backend='process', key=bytes(32))
                while pool.ready_seconds is None:
                    time.sleep(0.01)
                print(' '.join(str(child.pid) for child in multiprocessing.active_children()), flush=True)
                os.kill(os.getpid(), signal.SIGKILL)
        """)
        for method in ('spawn', 'forkserver'):
            if method not in multiprocessing.get_all_start_methods():
                continue
            with self.subTest(start_method=method):
                env = dict(subprocess_env(), **{tree.START_METHOD_ENV: method})
                result = subprocess.run([sys.executable, '-c', script], cwd=REPO_ROOT, env=env,
                                        capture_output=True, text=True, timeout=60, start_new_session=True)
                self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
                pids = [int(pid) for pid in result.stdout.split()]
                self.assertGreaterEqual(len(pids), 3, result.stdout)
                self.assertEqual(self.wait_gone(pids), [], 'workers outlived their killed parent')

    def run_cli_with(self, action, *extra, env=None):
        """`local --jobs 2` whose sender does `action` right after its first PAGE_DATA frame."""
        rng = random.Random(76)
        files = {f'base/5/{16384 + number}': rng.randbytes(2 * MIB) for number in range(3)}
        changed = {path: bytes(PAGE) + blob[PAGE:] for path, blob in files.items()}
        root = Path(self.temp.name)
        write_tree(root / 'S', changed)
        write_tree(root / 'B', files)
        script = textwrap.dedent("""
            import os, signal, sys, time
            from pgdelta import cli, protocol
            action = sys.argv.pop(1)
            original = protocol.Channel.send
            def send(self, code, *parts):
                original(self, code, *parts)
                if code == protocol.PAGE_DATA and not getattr(send, 'done', False):
                    send.done = True
                    if action == 'term':
                        os.kill(os.getpid(), signal.SIGTERM)
                        time.sleep(5)
                    elif action == 'kill':
                        os.kill(os.getpid(), signal.SIGKILL)
                    elif action == 'interrupt-group':  # Ctrl-C: SIGINT to every process in the group
                        os.killpg(os.getpgid(0), signal.SIGINT)
                        time.sleep(5)
            protocol.Channel.send = send
            if __name__ == '__main__':
                sys.exit(cli.main(sys.argv[1:]))
        """)
        process = subprocess.Popen(
            [sys.executable, '-c', script, action, 'local', '--source', str(root / 'S'), '--basis',
             str(root / 'B'), '--output', str(root / 'O'), '--reuse-any-basis', '--node-blocks', '4', '--jobs', '2',
             *extra], cwd=REPO_ROOT, env=dict(subprocess_env(), **(env or {})), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.kill_group_on_failure(process.pid)
        _, stderr = process.communicate(timeout=120)
        return process.returncode, stderr, process.pid, root / 'O'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def check_clean(self, group, output):
        self.assertEqual(self.wait_gone(group=group), [], 'a process of the run outlived it')
        self.assertFalse(os.path.lexists(output))
        self.assertFalse(os.path.lexists(receipt_path(output)))
        self.assertEqual(staging_dirs(output), [])

    def test_sigterm_mid_run(self):
        code, stderr, group, output = self.run_cli_with('term')
        self.assertEqual(code, 3, stderr)
        self.assertIn('interrupted', stderr)
        self.check_clean(group, output)

    def test_ctrl_c_to_the_whole_process_group_mid_run(self):
        code, stderr, group, output = self.run_cli_with('interrupt-group')
        self.assertEqual(code, 3, stderr)
        self.assertIn('interrupted', stderr)
        self.assertNotIn('Traceback', stderr)  # the workers ignore SIGINT; only the parents act on it
        self.check_clean(group, output)

    def test_sigkill_of_the_sender_mid_run(self):
        for method in ('spawn', 'forkserver'):
            if method not in multiprocessing.get_all_start_methods():
                continue
            with self.subTest(start_method=method):
                code, stderr, group, output = self.run_cli_with('kill', env={tree.START_METHOD_ENV: method})
                self.assertEqual(code, -signal.SIGKILL, stderr)
                self.check_clean(group, output)
                remove_tree(Path(self.temp.name) / 'S')
                remove_tree(Path(self.temp.name) / 'B')


if __name__ == '__main__':
    unittest.main()
