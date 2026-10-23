"""Where output files are fsynced (--fsync main|writeback|worker, T3 arm b).

Every mode must give the same output and the same statistics, apart from
timings and the fsync statistics themselves, for a new copy and in place.
The worker's fsyncs must all finish before publication or commit, and an
fsync error must fail the run.
"""
import contextlib
import errno
import os
from pathlib import Path
import random
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from pgdelta import flush, receiver as receiver_module
from pgdelta.errors import PeerError, TransferError
from pgdelta.flush import FsyncWorker
from pgdelta.inplace import CONTROL_FILE, CONTROL_PENDING, MARKER
from pgdelta.receiver import receipt_path

from pgdelta_support import PAGE, comparable, snapshot, staging_dirs, transfer, write_tree
from test_pgdelta_engine import settle
from test_pgdelta_inplace import pg_shapes
from test_pgdelta_parallel import build_shapes

KEY = bytes(range(32))
MIB = 1 << 20
EXPECTED_WRITEBACK = 'writeback' if flush.writeback_available() else 'main'


def options(fsync, jobs=1, rebuild='copy-patch', verify='pipelined', in_place=False):
    return {'jobs': jobs, 'receiver_jobs': jobs,
            'receiver_options': {'fsync': fsync, 'rebuild': rebuild, 'verify': verify, 'in_place': in_place}}


def flip_every(blob, step):
    """blob with one byte changed every `step` bytes."""
    changed = bytearray(blob)
    for offset in range(0, len(changed), step):
        changed[offset] ^= 1
    return bytes(changed)


class FsyncTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.basis, self.output = self.root / 'S', self.root / 'B', self.root / 'O'


class ModeTests(FsyncTestCase):
    def test_every_mode_gives_the_same_output_and_statistics(self):
        template = self.root / 'B-template'
        pg_shapes(self.source, template)
        results = {}
        for in_place in (False, True):
            for fsync in flush.FSYNC_MODES:
                for jobs, rebuild, verify in ((1, 'gap-fill', 'sequential'), (2, 'copy-patch', 'pipelined')):
                    label = f'{"in place" if in_place else "new copy"} {fsync} jobs={jobs}'
                    basis = self.root / f'B-{label.replace(" ", "_")}'
                    shutil.copytree(template, basis)
                    output = None if in_place else self.root / f'O-{label.replace(" ", "_")}'
                    outcome = transfer(self.source, basis, output, key=KEY,
                                       **options(fsync, jobs, rebuild, verify, in_place))
                    with self.subTest(config=label):
                        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
                        self.assertIsNone(outcome.receiver_error, repr(outcome.receiver_error))
                        self.assertEqual(snapshot(basis if in_place else output), snapshot(self.source))
                        stats = outcome.receiver
                        self.assertEqual(stats['fsync'], fsync)
                        self.assertEqual(stats['fsync_effective'],
                                         EXPECTED_WRITEBACK if fsync == 'writeback' else fsync)
                        self.assertGreaterEqual(stats['fsync_seconds'], 0)
                        if fsync != 'worker':
                            self.assertEqual(stats['fsync_wait_seconds'], 0)
                        results.setdefault(in_place, {})[label] = comparable(outcome)
        for in_place, runs in results.items():
            first = next(iter(runs))
            for label, result in runs.items():
                with self.subTest(compare=label):
                    self.assertEqual(result, runs[first])
        settle(self)

    def test_statistics_name_the_mode_in_effect_and_the_wait(self):
        # fsync_mode is the key a harness checks (EXP-04's measure.pgdelta_modes): the mode in effect, so
        # that a writeback that fell back to main shows.
        pg_shapes(self.source, self.basis)
        rng = random.Random(32)
        write_tree(self.source, {'a/big': rng.randbytes(9 * MIB)})
        cases = {fsync: ({}, EXPECTED_WRITEBACK if fsync == 'writeback' else fsync) for fsync in flush.FSYNC_MODES}
        cases['writeback refused'] = ({'writeback_available': True, 'start_writeback': False}, 'main')
        for label, (patches, effective) in cases.items():
            fsync = label.split()[0]
            output = self.root / f'O-{label.replace(" ", "_")}'
            with contextlib.ExitStack() as stack:
                for name, value in patches.items():
                    stack.enter_context(mock.patch.object(flush, name, return_value=value))
                outcome = transfer(self.source, self.basis, output, **options(fsync))
            with self.subTest(case=label):
                self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
                self.assertEqual(outcome.receiver['fsync_mode'], effective)
                self.assertEqual(outcome.sender['receiver']['fsync_mode'], effective)
                self.assertEqual(outcome.sender['timings_seconds']['fsync'], outcome.receiver['fsync_seconds'])

    def test_writeback_starts_write_back_every_8_mib_of_each_file(self):
        rng = random.Random(30)
        big, small = rng.randbytes(20 * MIB + 5), rng.randbytes(3 * MIB)
        changed = bytearray(big)
        for offset in range(0, len(changed), 3 * MIB):
            changed[offset] ^= 1
        write_tree(self.source, {'a/big': bytes(changed), 'a/new': big, 'a/small': small})
        write_tree(self.basis, {'a/big': big, 'a/small': small})
        calls = []

        def record(fd, offset, length):
            calls.append((os.fstat(fd).st_ino, offset, length))
            return True

        for rebuild in ('copy-patch', 'gap-fill'):
            calls.clear()
            output = self.root / f'O-{rebuild}'
            with mock.patch.object(flush, 'writeback_available', return_value=True), \
                    mock.patch.object(flush, 'start_writeback', record):
                outcome = transfer(self.source, self.basis, output, node_blocks=128,
                                   **options('writeback', rebuild=rebuild))
            with self.subTest(rebuild=rebuild):
                self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
                self.assertEqual(snapshot(output), snapshot(self.source))
                self.assertEqual(outcome.receiver['fsync_effective'], 'writeback')
                self.assertEqual(outcome.receiver['writeback_calls'], len(calls))
                by_file = {}
                for inode, offset, length in calls:
                    by_file.setdefault(inode, []).append((offset, length))
                # Two calls per 20 MiB file (at 8 and 16 MiB written), none for a 3 MiB one. Each covers
                # the span written since the last; spans may overlap, as a page can land in copied bytes.
                self.assertEqual(sorted(len(ranges) for ranges in by_file.values()), [2, 2])
                for ranges in by_file.values():
                    self.assertTrue(all(0 <= offset and 0 < length and offset + length <= len(big)
                                        for offset, length in ranges))
                    self.assertEqual(ranges[0][0], 0)

    @unittest.skipUnless(flush.writeback_available(), 'sync_file_range is Linux only')
    def test_writeback_calls_sync_file_range_for_a_new_copy_and_in_place(self):
        # No mocks: the real call through ctypes, on a file of over 16 MiB. A new copy writes B's bytes and
        # the received pages; in place only received pages are written, so there every page differs.
        rng = random.Random(33)
        old = rng.randbytes(20 * MIB + 5)
        cases = {'new copy': flip_every(old, 3 * MIB), 'in place': rng.randbytes(len(old))}
        for label, new in cases.items():
            in_place = label == 'in place'
            source, basis = self.root / f'S-{label[:3]}', self.root / f'B-{label[:3]}'
            control = {'PG_VERSION': b'17\n', CONTROL_FILE: rng.randbytes(PAGE)}
            write_tree(source, dict(control, **{'base/1/big': new}))
            write_tree(basis, dict(control, **{'base/1/big': old}))
            output = None if in_place else self.root / f'O-{label[:3]}'
            outcome = transfer(source, basis, output, node_blocks=128, **options('writeback', in_place=in_place))
            with self.subTest(case=label):
                self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
                self.assertIsNone(outcome.receiver_error, repr(outcome.receiver_error))
                self.assertEqual(snapshot(basis if in_place else output), snapshot(source))
                stats = outcome.receiver
                self.assertEqual((stats['fsync_effective'], stats['fsync_mode']), ('writeback', 'writeback'))
                self.assertGreaterEqual(stats['writeback_calls'], 2)

    def test_writeback_refused_by_the_filesystem_falls_back_to_main(self):
        rng = random.Random(31)
        write_tree(self.source, {'a/big': rng.randbytes(9 * MIB)})
        write_tree(self.basis, {})
        with mock.patch.object(flush, 'writeback_available', return_value=True), \
                mock.patch.object(flush, 'start_writeback', return_value=False):
            outcome = transfer(self.source, self.basis, self.output, **options('writeback'))
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assertEqual(snapshot(self.output), snapshot(self.source))
        self.assertEqual((outcome.receiver['fsync_effective'], outcome.receiver['writeback_calls']), ('main', 0))

    @unittest.skipUnless(flush.writeback_available(), 'sync_file_range is Linux only')
    def test_start_writeback_accepts_a_regular_file(self):
        path = self.root / 'f'
        path.write_bytes(os.urandom(MIB))
        fd = os.open(path, os.O_RDWR)
        try:
            self.assertTrue(flush.start_writeback(fd, 0, MIB))
        finally:
            os.close(fd)

    @unittest.skipIf(sys.platform.startswith('linux'), 'tests the fallback elsewhere')
    def test_without_sync_file_range_writeback_is_main(self):
        self.assertFalse(flush.writeback_available())
        self.assertFalse(flush.start_writeback(0, 0, 1))


class WorkerTests(FsyncTestCase):
    def failing_fsync(self):
        """os.fsync that fails with EIO on the worker thread and works elsewhere (directories, the marker)."""
        real = os.fsync

        def fsync(fd):
            if threading.current_thread().name == 'pgdelta-fsync':
                raise OSError(errno.EIO, 'Input/output error')
            return real(fd)
        return fsync

    def test_fsync_error_fails_the_run_before_publication(self):
        build_shapes(self.source, self.basis)
        before = snapshot(self.basis)
        with mock.patch.object(flush.os, 'fsync', self.failing_fsync()):
            outcome = transfer(self.source, self.basis, self.output, **options('worker', jobs=2))
        self.assertIsInstance(outcome.receiver_error, TransferError)
        self.assertIn('cannot fsync', str(outcome.receiver_error))
        self.assertIsInstance(outcome.sender_error, PeerError)
        self.assertEqual(outcome.sender_error.exit_code, 3)
        self.assertFalse(os.path.lexists(self.output))
        self.assertFalse(os.path.lexists(receipt_path(self.output)))
        self.assertEqual(staging_dirs(self.output), [])
        self.assertEqual(snapshot(self.basis), before)
        settle(self)

    def test_fsync_error_in_place_leaves_b_blocked(self):
        pg_shapes(self.source, self.basis)
        with mock.patch.object(flush.os, 'fsync', self.failing_fsync()):
            outcome = transfer(self.source, self.basis, None, **options('worker', in_place=True))
        self.assertIsInstance(outcome.receiver_error, TransferError)
        self.assertIn('blocked from starting', str(outcome.sender_error))
        self.assertTrue(os.path.isfile(self.basis / MARKER))
        self.assertFalse(os.path.lexists(self.basis / CONTROL_FILE))
        self.assertTrue(os.path.isfile(self.basis / CONTROL_PENDING))
        settle(self)

    def test_publication_waits_for_every_fsync(self):
        build_shapes(self.source, self.basis)
        real, done = os.fsync, []

        def slow(fd):
            if threading.current_thread().name == 'pgdelta-fsync':
                time.sleep(0.02)
                done.append(fd)
            return real(fd)

        renamed = []
        original_publish = receiver_module._Receiver._publish

        def publish(receiver, output):
            renamed.append(len(done))
            return original_publish(receiver, output)

        with mock.patch.object(flush.os, 'fsync', slow), \
                mock.patch.object(receiver_module._Receiver, '_publish', publish):
            outcome = transfer(self.source, self.basis, self.output, **options('worker'))
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        files = sum(1 for path in self.source.rglob('*') if path.is_file())
        self.assertEqual(renamed, [files])
        self.assertGreater(outcome.receiver['fsync_wait_seconds'], 0)
        self.assertGreaterEqual(outcome.receiver['fsync_seconds'], 0.02 * files)


class FsyncWorkerTests(unittest.TestCase):
    def open_files(self, count):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return [os.open(Path(temp.name) / f'f{number}', os.O_RDWR | os.O_CREAT, 0o600) for number in range(count)]

    def assert_closed(self, fds):
        for fd in fds:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_finish_fsyncs_and_closes_every_file(self):
        fds = self.open_files(5)
        worker = FsyncWorker()
        for fd in fds:
            worker.submit(fd, 'f')
        worker.finish()
        self.assertIsNone(worker.error)
        self.assertEqual(worker.files, 5)
        self.assert_closed(fds)
        self.assertFalse(worker.thread.is_alive())

    def test_close_abandons_what_is_left_but_still_closes_it(self):
        fds = self.open_files(4)
        gate = threading.Event()
        real = os.fsync

        def blocked(fd):
            gate.wait(10)
            return real(fd)

        with mock.patch.object(flush.os, 'fsync', blocked):
            worker = FsyncWorker()
            for fd in fds:
                worker.submit(fd, 'f')
            closer = threading.Thread(target=worker.close, args=(10,))
            closer.start()
            time.sleep(0.05)
            gate.set()
            closer.join(10)
        self.assertLessEqual(worker.files, 1)
        self.assert_closed(fds)

    def test_first_error_is_kept_and_reported_once(self):
        fds = self.open_files(3)
        reported = []
        with mock.patch.object(flush.os, 'fsync', side_effect=OSError(errno.ENOSPC, 'No space left on device')):
            worker = FsyncWorker(on_error=reported.append)
            for fd in fds:
                worker.submit(fd, f'base/{fd}')
            worker.finish()
        self.assertIsInstance(worker.error, TransferError)
        self.assertIn(f'cannot fsync base/{fds[0]}', str(worker.error))
        self.assertEqual(reported, [worker.error])
        self.assert_closed(fds)

    def test_a_full_queue_makes_submit_wait_and_says_how_long(self):
        fds = self.open_files(3)
        gate = threading.Event()
        real = os.fsync

        def blocked(fd):
            gate.wait(10)
            return real(fd)

        with mock.patch.object(flush.os, 'fsync', blocked):
            worker = FsyncWorker(limit=1)
            worker.submit(fds[0], 'f')  # taken by the worker, which then blocks
            time.sleep(0.05)
            worker.submit(fds[1], 'f')  # fills the queue
            threading.Timer(0.2, gate.set).start()
            waited = worker.submit(fds[2], 'f')
            worker.finish()
        self.assertGreater(waited, 0.1)
        self.assert_closed(fds)


if __name__ == '__main__':
    unittest.main()
