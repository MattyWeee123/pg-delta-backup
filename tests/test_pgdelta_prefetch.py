"""The listener's prefetch of B (pgdelta.prefetch): read-only, capped, and stopped on connect, failure and signals."""
import copy
import hashlib
import json
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

from pgdelta import cli, prefetch
from pgdelta.inventory import scan

from pgdelta_support import (PAGE, REPO_ROOT, python_command, snapshot, staging_dirs, subprocess_env,
                             write_tree)

CHUNK = 64 * 1024
WRITE_FLAGS = (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
               | getattr(os, 'O_EXCL', 0))


def basis_state(root):
    """Content, size, mode and mtime of every entry under root: what "B never changes" means here."""
    root = Path(root)
    state = {}
    for path in sorted(root.rglob('*')):
        info = os.lstat(path)
        content = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        state[path.relative_to(root).as_posix()] = (info.st_mode, info.st_size, info.st_mtime_ns, content)
    return state


class Slow(prefetch.BasisPrefetcher):
    """Reads at most 4 KiB per call and pauses, so a test can stop it while it still reads."""

    @staticmethod
    def _read(fd, view, offset):
        time.sleep(0.01)
        return prefetch.BasisPrefetcher._read(fd, view[:4096], offset)


class PrefetcherTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.basis = self.root / 'B'
        rng = random.Random(7)
        self.sizes = {'PG_VERSION': 3, 'base/1/1259': 3 * CHUNK + 17, 'base/1/empty': 0,
                      'base/1/2619': CHUNK, 'global/pg_control': 8192, 'pg_wal/000000010000000000000001': 2 * CHUNK}
        write_tree(self.basis, {path: rng.randbytes(size) for path, size in self.sizes.items()},
                   ['pg_wal/archive_status'])
        self.files = scan(self.basis).files
        self.total = sum(self.sizes.values())

    def prefetcher(self, cls=prefetch.BasisPrefetcher, **kwargs):
        kwargs.setdefault('chunk_bytes', CHUNK)
        kwargs.setdefault('cap_bytes', 1 << 40)
        return cls(self.basis, self.files, **kwargs)

    def run_to_end(self, prefetcher):
        prefetcher.start()
        prefetcher.thread.join(30)
        self.assertFalse(prefetcher.running)
        return prefetcher.stop(prefetch.CONNECTED)


class PrefetcherTests(PrefetcherTestCase):
    def test_reads_every_file_in_inventory_order_read_only_and_changes_nothing(self):
        before = basis_state(self.basis)
        opened, real_open = [], os.open

        def recording_open(path, flags, *args, **kwargs):
            if Path(path).is_relative_to(self.basis):
                opened.append((Path(path).relative_to(self.basis).as_posix(), flags))
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(prefetch.os, 'open', recording_open):
            stats = self.run_to_end(self.prefetcher())
        self.assertEqual(basis_state(self.basis), before)
        # Only regular files, each opened read-only, without following a symlink, in inventory order.
        self.assertEqual([path for path, _ in opened], [entry.path for entry in self.files if entry.size])
        for path, flags in opened:
            self.assertEqual(flags & WRITE_FLAGS, 0, path)
            self.assertEqual(flags & 3, os.O_RDONLY)  # not O_WRONLY (1) or O_RDWR (2)
            self.assertTrue(flags & getattr(os, 'O_NOFOLLOW', 0) or not hasattr(os, 'O_NOFOLLOW'))
        self.assertEqual(stats, {**stats, 'switch': 'on', 'ran': True, 'why_not': None, 'files': len(self.sizes),
                                 'basis_bytes': self.total, 'files_read': len(self.sizes), 'bytes': self.total,
                                 'stopped': prefetch.FINISHED, 'error': None})
        self.assertGreaterEqual(stats['waited_seconds'], stats['seconds'])

    def test_the_cap_is_respected(self):
        for cap in (0, 1, CHUNK - 1, 2 * CHUNK + 5, self.total - 1):
            with self.subTest(cap=cap):
                stats = self.run_to_end(self.prefetcher(cap_bytes=cap))
                self.assertEqual(stats['bytes'], cap)
                self.assertEqual(stats['cap_bytes'], cap)
                self.assertEqual(stats['stopped'], prefetch.CAPPED)
                self.assertLess(stats['files_read'], len(self.sizes))
        stats = self.run_to_end(self.prefetcher(cap_bytes=self.total))
        self.assertEqual((stats['bytes'], stats['stopped']), (self.total, prefetch.FINISHED))

    def test_the_default_cap_is_half_of_memavailable(self):
        meminfo = self.root / 'meminfo'
        meminfo.write_text('MemTotal:       32864312 kB\nMemFree:        30000000 kB\n'
                           'MemAvailable:   31000000 kB\nBuffers:            2048 kB\n')
        self.assertEqual(prefetch.memory_available(meminfo), 31000000 * 1024)
        self.assertEqual(prefetch.default_cap(meminfo), 31000000 * 1024 // 2)
        # No MemAvailable (macOS, or an old kernel): a quarter of physical memory, else a fixed 1 GiB.
        missing = self.root / 'no-meminfo'
        self.assertIsNone(prefetch.memory_available(missing))
        with mock.patch.object(prefetch, 'physical_memory', return_value=24 << 30):
            self.assertEqual(prefetch.default_cap(missing), 6 << 30)
        with mock.patch.object(prefetch, 'physical_memory', return_value=None):
            self.assertEqual(prefetch.default_cap(missing), prefetch.FALLBACK_CAP_BYTES)
        if prefetch.memory_available() is not None:  # Linux; MemAvailable moves a little between reads
            self.assertAlmostEqual(prefetch.BasisPrefetcher(self.basis, self.files).cap_bytes,
                                   prefetch.memory_available() // 2, delta=256 << 20)

    def test_stop_ends_the_reading_at_once(self):
        prefetcher = self.prefetcher(Slow).start()
        deadline = time.monotonic() + 10
        while prefetcher.bytes_read == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        started = time.monotonic()
        stats = prefetcher.stop(prefetch.CONNECTED)
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(prefetcher.running)
        self.assertEqual(stats['stopped'], prefetch.CONNECTED)
        self.assertGreater(stats['bytes'], 0)
        self.assertLess(stats['bytes'], self.total)
        frozen = stats['bytes']
        time.sleep(0.05)
        self.assertEqual(prefetcher.bytes_read, frozen)
        # A second stop changes nothing.
        self.assertEqual(prefetcher.stop(prefetch.FAILED)['stopped'], prefetch.CONNECTED)

    def test_a_read_error_is_recorded_and_never_raised(self):
        (self.basis / 'base/1/2619').unlink()  # gone after B was scanned
        stats = self.run_to_end(self.prefetcher())
        self.assertEqual(stats['stopped'], prefetch.ERROR)
        self.assertIn('base/1/2619', stats['error'])
        before = [entry.path for entry in self.files].index('base/1/2619')
        self.assertEqual(stats['bytes'], sum(entry.size for entry in self.files[:before]))
        self.assertIn('error', prefetch.describe(stats))

    def test_a_file_shorter_than_scanned_is_read_to_its_end(self):
        os.truncate(self.basis / 'base/1/1259', CHUNK + 5)
        stats = self.run_to_end(self.prefetcher())
        self.assertEqual(stats['stopped'], prefetch.FINISHED)
        self.assertEqual(stats['bytes'], self.total - (3 * CHUNK + 17) + CHUNK + 5)

    @unittest.skipUnless(prefetch._ADVISE, 'posix_fadvise is not available here')
    def test_advises_the_next_piece_where_posix_fadvise_exists(self):
        advised = []
        real = os.posix_fadvise
        with mock.patch.object(prefetch.os, 'posix_fadvise',
                               lambda fd, offset, length, advice: advised.append((offset, length, advice))
                               or real(fd, offset, length, advice)):
            stats = self.run_to_end(self.prefetcher())
        self.assertTrue(stats['fadvise'])
        self.assertIn((CHUNK, CHUNK, os.POSIX_FADV_WILLNEED), advised)
        self.assertIn((3 * CHUNK, 17, os.POSIX_FADV_WILLNEED), advised)
        self.assertFalse(self.run_to_end(self.prefetcher(advise=False))['fadvise'])

    @unittest.skipUnless(getattr(os, 'O_NOATIME', 0), 'O_NOATIME is Linux only')
    def test_reads_without_updating_access_times_where_allowed(self):
        stats = self.run_to_end(self.prefetcher())
        self.assertIs(stats['noatime'], True)  # the test owns B's files

    def test_reports_when_it_read_on_the_wall_clock(self):
        # EXP-04 places the reading against its own timed phases, on the same host's clock.
        before = time.time()
        stats = self.run_to_end(self.prefetcher())
        after = time.time()
        self.assertLessEqual(before, stats['started_utc'])
        self.assertLessEqual(stats['started_utc'], stats['ended_utc'])
        self.assertLessEqual(stats['ended_utc'], after)
        self.assertAlmostEqual(stats['ended_utc'] - stats['started_utc'], stats['seconds'], places=5)
        off = prefetch.not_run('off', 'switched off')
        self.assertEqual((off['started_utc'], off['ended_utc']), (None, None))

    def test_not_run_has_the_same_shape(self):
        stats = self.run_to_end(self.prefetcher())
        off = prefetch.not_run('off', 'switched off')
        self.assertEqual(set(off), set(stats))
        self.assertEqual((off['ran'], off['bytes'], off['switch']), (False, 0, 'off'))
        self.assertIn('off', prefetch.describe(off))


class WhileWaitingTests(PrefetcherTestCase):
    def test_stopped_when_the_sender_connects(self):
        prefetcher = self.prefetcher(Slow)

        def accept():
            deadline = time.monotonic() + 10
            while prefetcher.bytes_read == 0 and time.monotonic() < deadline:
                time.sleep(0.005)
            return 'channel'

        channel, stats = prefetch.while_waiting(accept, prefetcher)
        self.assertEqual(channel, 'channel')
        self.assertEqual(stats['stopped'], prefetch.CONNECTED)
        self.assertLess(stats['bytes'], self.total)
        self.assertFalse(prefetcher.running)

    def test_stopped_when_waiting_fails_or_is_interrupted(self):
        for error, reason in ((OSError(9, 'accept failed'), prefetch.FAILED),
                              (KeyboardInterrupt('signal 15'), prefetch.INTERRUPTED)):
            with self.subTest(reason=reason):
                prefetcher = self.prefetcher(Slow)

                def accept():
                    while prefetcher.bytes_read == 0:
                        time.sleep(0.005)
                    raise error

                with self.assertRaises(type(error)):
                    prefetch.while_waiting(accept, prefetcher)
                self.assertFalse(prefetcher.running)
                self.assertEqual(prefetcher.stats()['stopped'], reason)

    def test_no_prefetch_thread_outlives_the_wait(self):
        prefetch.while_waiting(lambda: None, self.prefetcher(Slow))
        self.assertNotIn('pgdelta-prefetch', [thread.name for thread in threading.enumerate()])


# --- receive --listen ----------------------------------------------------------------------------

SLOW_PREFETCH = """
import signal, time
from pgdelta import prefetch
# A test runner started in the background may hand down SIGINT ignored; this test needs Python's default.
signal.signal(signal.SIGINT, signal.default_int_handler)
_read = prefetch.BasisPrefetcher._read
def slow(fd, view, offset):
    time.sleep(0.05)
    return _read(fd, view[:16384], offset)
prefetch.BasisPrefetcher._read = staticmethod(slow)
"""


def receiver_command(patch, *args):
    script = textwrap.dedent(patch) + '\nimport sys\nfrom pgdelta import cli\nsys.exit(cli.main(sys.argv[1:]))\n'
    return [sys.executable, '-c', script, *map(str, args)]


class ListenerPrefetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.basis = self.root / 'S', self.root / 'B'
        rng = random.Random(51)
        old = rng.randbytes(300 * PAGE + 123)
        new = bytearray(old)
        for page in (3, 150, 151, 299):
            new[page * PAGE + 7] ^= 0xFF
        write_tree(self.source, {'base/5/16384': bytes(new), 'base/5/16384_fsm': rng.randbytes(3 * PAGE),
                                 'PG_VERSION': b'17\n'}, ['pg_wal/archive_status'])
        write_tree(self.basis, {'base/5/16384': old, 'base/5/dropped': rng.randbytes(PAGE),
                                'PG_VERSION': b'17\n'})
        self.basis_bytes = sum(path.stat().st_size for path in self.basis.rglob('*') if path.is_file())

    def start_receiver(self, output, *extra, patch=''):
        command = (receiver_command(patch, 'receive', '--basis', self.basis, '--output', output,
                                    '--listen', '127.0.0.1:0', '--reuse-any-basis', '--jobs', '2', *extra)
                   if patch else
                   python_command('receive', '--basis', self.basis, '--output', output, '--listen', '127.0.0.1:0',
                                  '--reuse-any-basis', '--jobs', '2', *extra))
        receiver = subprocess.Popen(command, stderr=subprocess.PIPE, text=True, cwd=REPO_ROOT,
                                    env=subprocess_env())
        self.addCleanup(receiver.stderr.close)
        self.addCleanup(lambda: receiver.poll() is None and receiver.kill())
        line = receiver.stderr.readline()
        self.assertIn('listening on 127.0.0.1:', line)
        return receiver, int(line.rsplit(':', 1)[1])

    def transfer(self, name, *extra, patch=''):
        output, stats_path = self.root / f'O-{name}', self.root / f'stats-{name}.json'
        receiver, port = self.start_receiver(output, *extra, patch=patch)
        sent = subprocess.run(python_command('send', '--source', self.source, '--connect', f'127.0.0.1:{port}',
                                             '--jobs', '2', '--stats-json', stats_path),
                              capture_output=True, text=True, cwd=REPO_ROOT, env=subprocess_env(), timeout=120)
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertEqual(receiver.wait(timeout=60), 0)
        log = receiver.stderr.read()
        self.assertEqual(snapshot(output), snapshot(self.source))
        return json.loads(stats_path.read_text()), log

    @staticmethod
    def comparable(stats):
        """Everything but timings, paths and the prefetch's own statistics."""
        stats = copy.deepcopy(stats)
        stats.pop('timings_seconds')
        stats.pop('protocol_timers')  # EXP-13's, which vary like timings
        receiver = stats.pop('receiver')
        # The final COMPLETE carries the receiver's statistics, whose length varies.
        down = stats['bytes_dest_to_source']
        final = down['by_message_type'].pop('COMPLETE')
        down['total'] -= final['bytes']
        down['control'] -= final['bytes']
        # files_digest covers the keyed file digests, and the key is new in every run.
        stats.pop('files_digest')
        kept = {key: value for key, value in receiver.items()
                if not key.endswith('_seconds') and key not in ('prefetch', 'output', 'receipt', 'files_digest')}
        return stats, kept

    def test_listen_prefetches_b_and_changes_nothing_but_its_own_statistics(self):
        before = basis_state(self.basis)
        on, on_log = self.transfer('on')
        off, off_log = self.transfer('off', '--prefetch-basis', 'off')
        self.assertEqual(basis_state(self.basis), before)
        self.assertEqual(self.comparable(on), self.comparable(off))
        fetched = on['receiver']['prefetch']
        self.assertEqual(fetched, {**fetched, 'switch': 'on', 'ran': True, 'basis_bytes': self.basis_bytes,
                                   'files': 3, 'error': None})
        # The sender connects well after B's 2.5 MB are read, but the two race.
        self.assertIn(fetched['stopped'], (prefetch.FINISHED, prefetch.CONNECTED))
        if fetched['stopped'] == prefetch.FINISHED:
            self.assertEqual(fetched['bytes'], self.basis_bytes)
        self.assertIn('prefetched', on_log)
        self.assertEqual(off['receiver']['prefetch'], prefetch.not_run('off', 'switched off (--prefetch-basis off)'))
        self.assertIn('basis prefetch off', off_log)

    def test_prefetch_stops_when_the_sender_connects(self):
        before = basis_state(self.basis)
        stats, log = self.transfer('slow', patch=SLOW_PREFETCH)  # 16 KiB per 50 ms: 2.5 MB take 8 s
        fetched = stats['receiver']['prefetch']
        self.assertEqual(fetched['stopped'], prefetch.CONNECTED)
        self.assertGreater(fetched['bytes'], 0)
        self.assertLess(fetched['bytes'], self.basis_bytes)
        self.assertIn('stopped: connected', log)
        self.assertEqual(basis_state(self.basis), before)

    def test_prefetch_cap_from_the_command_line(self):
        stats, _ = self.transfer('cap', '--prefetch-cap', '100000')
        fetched = stats['receiver']['prefetch']
        self.assertEqual(fetched['cap_bytes'], 100000)
        self.assertLessEqual(fetched['bytes'], 100000)
        if fetched['stopped'] != prefetch.CONNECTED:
            self.assertEqual((fetched['bytes'], fetched['stopped']), (100000, prefetch.CAPPED))

    def test_sigterm_and_sigint_while_prefetching_exit_3_and_leave_nothing(self):
        before = basis_state(self.basis)
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                output = self.root / f'O-{signum}'
                receiver, _ = self.start_receiver(output, patch=SLOW_PREFETCH)
                time.sleep(0.3)
                receiver.send_signal(signum)
                started = time.monotonic()
                self.assertEqual(receiver.wait(timeout=30), 3)
                self.assertLess(time.monotonic() - started, 10)
                self.assertIn('interrupted', receiver.stderr.read())
                self.assertFalse(output.exists())
                self.assertEqual(staging_dirs(output), [])
        self.assertEqual(basis_state(self.basis), before)

    def test_options_and_stdio(self):
        help_text = ' '.join(subprocess.run(python_command('receive', '--help'), capture_output=True, text=True,
                                            cwd=REPO_ROOT, env=subprocess_env()).stdout.split())
        for option in ('--prefetch-basis', '--prefetch-cap', 'MemAvailable', 'read-only'):
            self.assertIn(option, help_text)
        parser = cli.build_parser()
        args = parser.parse_args(['receive', '--basis', 'B', '--output', 'O', '--listen', 'h:1'])
        self.assertEqual((args.prefetch_basis, args.prefetch_cap), ('on', None))
        for bad in (('--prefetch-basis', 'maybe'), ('--prefetch-cap', '-1'), ('--prefetch-cap', 'lots')):
            with self.subTest(bad=bad):
                result = subprocess.run(python_command('receive', '--basis', self.basis, '--output',
                                                       self.root / 'O', '--listen', '127.0.0.1:0', *bad),
                                        capture_output=True, text=True, cwd=REPO_ROOT, env=subprocess_env())
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(bad[0], result.stderr)
        # `local` runs its receiver over stdio, connected from the start: nothing to prefetch while waiting.
        stats_path = self.root / 'local.json'
        result = subprocess.run(python_command('local', '--source', self.source, '--basis', self.basis,
                                               '--output', self.root / 'O-local', '--reuse-any-basis',
                                               '--stats-json', stats_path),
                                capture_output=True, text=True, cwd=REPO_ROOT, env=subprocess_env(), timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        fetched = json.loads(stats_path.read_text())['receiver']['prefetch']
        self.assertEqual((fetched['switch'], fetched['ran'], fetched['bytes']), ('on', False, 0))
        self.assertIn('stdio', fetched['why_not'])


if __name__ == '__main__':
    unittest.main()
