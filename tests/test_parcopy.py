"""Tests for parcopy, EXP-11's bound (experiments/network/parcopy.py), and its wiring in run_exp04.

No VM is needed. The plan runs on synthetic sizes shaped like scale 70. The copies run over loopback in
this process, from a synthetic stopped data directory read with a stub pg_controldata
(test_pgdelta_datadir's), with os.fsync counted through a patch; with PostgreSQL 17's binaries
(PGDELTA_TEST_PG_BIN) a real stopped cluster is copied and started, and skipped without them. The
orchestrator's run, its preflight and A5's fsync count run with test_exp04_network's and
test_exp11_core's stubs.
"""
from __future__ import annotations

from argparse import Namespace
from contextlib import contextmanager, redirect_stderr
import io
import json
import os
from pathlib import Path
import queue
import random
import stat
import subprocess
import sys
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from experiments.network import agent, measure, parcopy, run_exp04
from pgdelta import datadir, flush
from pgdelta.errors import InvalidInput, PeerError, ProtocolError, TransferError
from pgdelta.inventory import FileEntry, Inventory

from pgdelta_cluster import CONTROLDATA, SKIP_REASON, Servers, available, initdb
from pgdelta_support import REPO_ROOT, subprocess_env
from test_exp04_network import FakeChild, FakeClock, LINK, SPEED, TRIAL
from test_exp11_core import T2Stub
from test_pgdelta_datadir import REDO, RESET, build_datadir, stub_controldata
from test_pgdelta_identity import SAMPLE

MIB = 1 << 20
KIB = 1 << 10
# Scale 70's shape (EXP-11's Data): two heap files hold most of 2.1 GB.
SCALE_70 = {'base/5/16397': 940_000_000, 'base/5/16410': 800_000_000, 'base/5/16403': 150_000_000,
            'base/5/16397_fsm': 260_000, 'base/5/16397_vm': 40_000, f'pg_wal/{REDO}': 16 * MIB,
            **{f'base/5/{1000 + index}': 8 * KIB * (1 + index % 40) for index in range(900)},
            **{f'base/1/{1000 + index}': 8 * KIB * (1 + index % 7) for index in range(300)},
            'base/5/empty': 0}
DIRECTORY_MODES = {'base/5': 0o750, 'pg_wal': 0o750, 'pg_replslot': 0o750, 'pg_wal/archive_status': 0o700}
ROOT_MODE = 0o750
FILE_MODES = {'PG_VERSION': 0o640, 'base/5/16384': 0o600, 'global/1262': 0o400}
LARGE = 'base/5/16384'  # made 5 MiB and 3 bytes, so it splits into ranges


def selection_of(sizes: dict):
    """A Selection whose inventory lists files of these sizes (none exists)."""
    files = sorted((FileEntry(path, size, 0o600) for path, size in sizes.items()), key=lambda entry: entry.path)
    return datadir.Selection(Inventory(root_mode=0o700, files=files), {}, REDO)


@contextmanager
def counted_fsyncs():
    """os.fsync, counted: each call's (st_dev, st_ino), in order."""
    calls = []
    real = os.fsync

    def fsync(fd):
        info = os.fstat(fd)
        real(fd)
        calls.append((info.st_dev, info.st_ino))

    with mock.patch.object(parcopy.os, 'fsync', fsync):
        yield calls


def round_trip(source, controldata, output, streams, *, chunk_bytes=MIB, writeback_bytes=256 * KIB):
    """parcopy's receiver and sender over loopback, each on its own thread: (receiver, sender, outcome).

    outcome holds each end's stats, or its exception under receiver_error and sender_error.
    """
    receiver = parcopy.Receiver(output, streams, writeback_bytes)
    sender = parcopy.Sender(source, controldata, streams, chunk_bytes)
    address, outcome = queue.Queue(), {}

    def receive():
        try:
            outcome['receiver'] = receiver.run('127.0.0.1:0', address.put)
        except BaseException as exc:
            outcome['receiver_error'] = exc
        finally:
            address.put(None)

    thread = threading.Thread(target=receive, daemon=True)
    thread.start()
    bound = address.get(timeout=60)
    if bound is not None:
        try:
            outcome['sender'] = sender.run(f'{bound[0]}:{bound[1]}')
        except Exception as exc:
            outcome['sender_error'] = exc
    thread.join(120)
    return receiver, sender, outcome


def tree(root) -> dict:
    """Every entry below root (and root itself, as ''): relative path -> os.stat result."""
    root = Path(root)
    entries = {'': os.stat(root)}
    for path in root.rglob('*'):
        entries[path.relative_to(root).as_posix()] = os.stat(path)
    return entries


# --- the plan ----------------------------------------------------------------------------------

class Plan(unittest.TestCase):
    def test_every_byte_is_in_one_range_of_at_most_the_chunk(self):
        selection = selection_of(SCALE_70)
        files = selection.inventory.files
        for streams in (1, 4, 8):
            with self.subTest(streams=streams):
                lists = parcopy.plan(selection, streams, 64 * MIB)
                self.assertEqual(len(lists), streams)
                by_file: dict = {}
                for item in (item for ranges in lists for item in ranges):
                    self.assertTrue(0 < item.length <= 64 * MIB)
                    by_file.setdefault(item.file, []).append(item)
                for index, entry in enumerate(files):
                    ranges = sorted(by_file.get(index, []), key=lambda item: item.offset)
                    covered = 0
                    for item in ranges:
                        self.assertEqual(item.offset, covered, entry.path)
                        covered += item.length
                    self.assertEqual(covered, entry.size, entry.path)
                # An empty file has no range: the receiver creates every file first.
                empty = next(index for index, entry in enumerate(files) if entry.path == 'base/5/empty')
                self.assertNotIn(empty, by_file)

    def test_largest_first_to_the_stream_with_the_fewest_bytes(self):
        selection = selection_of({'a': 10, 'b': 7, 'c': 5, 'd': 5, 'e': 3, 'f': 0})
        paths = [entry.path for entry in selection.inventory.files]
        lists = parcopy.plan(selection, 2, 100)
        # a to 0 (10); b to 1 (7); c to 1 (12); d to 0 (15); e to 1 (15).
        self.assertEqual([[(paths[item.file], item.length) for item in ranges] for ranges in lists],
                         [[('a', 10), ('d', 5)], [('b', 7), ('c', 5), ('e', 3)]])
        # Cut into ranges of 4: each range is placed on its own, largest first, the lowest stream on a tie.
        lists = parcopy.plan(selection, 2, 4)
        self.assertEqual([(paths[item.file], item.offset, item.length) for item in lists[0][:3]],
                         [('a', 0, 4), ('b', 0, 4), ('d', 0, 4)])
        with self.assertRaises(ValueError):
            parcopy.plan(selection, 0, 4)

    def test_ranges_balance_the_streams_where_files_would_not(self):
        selection = selection_of(SCALE_70)
        for streams in (4, 8):
            with self.subTest(streams=streams):
                summary = parcopy.plan_summary(parcopy.plan(selection, streams, measure.PARCOPY_CHUNK_BYTES))
                self.assertEqual(summary['bytes'], sum(SCALE_70.values()))
                self.assertLessEqual(summary['balance'], 1.1)  # A3's gate on the busiest stream
                self.assertEqual(sum(summary['stream_bytes']), summary['bytes'])
                # By file, one stream carries pgbench_accounts alone, nearly half the copy.
                by_file = parcopy.plan_summary(parcopy.plan(selection, streams, 1 << 40))
                self.assertEqual(max(by_file['stream_bytes']), 940_000_000)
                self.assertGreater(by_file['balance'], 1.7)

    def test_the_command_lines_are_run_exp04s(self):
        self.assertEqual(parcopy.DEFAULT_CHUNK_BYTES, measure.PARCOPY_CHUNK_BYTES)
        self.assertEqual(parcopy.DEFAULT_WRITEBACK_BYTES, measure.PARCOPY_WRITEBACK_BYTES)
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = T2Stub(tmp, FakeClock())
            self.addCleanup(orchestrator.pool.shutdown)
            address = f'{orchestrator.dest_ip}:{run_exp04.PARCOPY_PORT}'
            for method in measure.PARCOPY_METHODS:
                with self.subTest(method=method):
                    paths = orchestrator.method_paths(method)
                    receive = [str(part) for part in orchestrator.parcopy_receive_argv(method, paths)]
                    send = [str(part) for part in orchestrator.parcopy_send_argv(method, paths)]
                    self.assertEqual(receive[1:3], ['-m', 'experiments.network.parcopy'])
                    args = parcopy.build_parser().parse_args(receive[3:])
                    streams = measure.PARCOPY_VARIANTS[method]['streams']
                    self.assertEqual((args.func, args.output, args.listen, args.stats_json, args.streams,
                                      args.writeback_bytes),
                                     (parcopy.cmd_receive, orchestrator.outputs / method, address,
                                      orchestrator.work / 'stats' / f'{method}-receive.json', streams, 8 * MIB))
                    args = parcopy.build_parser().parse_args(send[3:])
                    self.assertEqual((args.func, args.source, args.pg_controldata, args.connect, args.stats_json,
                                      args.streams, args.chunk_bytes),
                                     (parcopy.cmd_send, Path(orchestrator.source_data),
                                      str(orchestrator.bin / 'pg_controldata'), address,
                                      Path(paths['stats']), streams, 64 * MIB))


# --- copies over loopback --------------------------------------------------------------------

def synthetic_source(root: Path):
    """A stopped data directory under root, with a file of 5 MiB and 3 bytes that splits into ranges and the
    recorded modes, and a stub pg_controldata: (data directory, stub, its selection)."""
    data = build_datadir(root / 'D')
    rng = random.Random(11)
    (data / LARGE).write_bytes(rng.randbytes(5 * MIB + 3))
    for path, mode in FILE_MODES.items():
        os.chmod(data / path, mode)
    for path, mode in DIRECTORY_MODES.items():
        os.chmod(data / path, mode)
    os.chmod(data, ROOT_MODE)
    stub = stub_controldata(root / 'bin')
    return data, stub, datadir.select(data, stub)


def dropped_range_copy(source, controldata, output, streams):
    """A round trip whose plan loses its first stream's first range: (fsync calls, outcome, the lost range)."""
    real, dropped = parcopy.plan, []

    def dropping(selection, streams, chunk_bytes):
        lists = real(selection, streams, chunk_bytes)
        dropped.append(lists[0].pop(0))
        return lists

    with mock.patch.object(parcopy, 'plan', dropping), counted_fsyncs() as calls:
        _, _, outcome = round_trip(source, controldata, output, streams)
    [item] = dropped
    return calls, outcome, item


def exit_statuses(outcome) -> dict:
    """What each end of a round trip exits with on parcopy's command line (finish): receiver and sender."""
    with redirect_stderr(io.StringIO()):
        return {end: parcopy.finish({}, None, outcome.get(f'{end}_error')) for end in ('receiver', 'sender')}


def aborted_run(tmp, statuses: dict, method='parcopy4'):
    """run_exp04's run_method for a parcopy run whose receiver and sender exit with these statuses, on the
    harness's stubs and a fake clock: (orchestrator, record). Its results directory is <tmp>/out."""
    class Receiver(FakeChild):
        def wait(self, timeout):
            return self.result or self._record(statuses['receiver'])

    class Aborted(Parcopying):
        def remote_command(self, argv, log_name, *, cwd=None):
            return {**super().remote_command(argv, log_name, cwd=cwd), 'exit_code': statuses['sender']}

    clock = FakeClock()
    with mock.patch.object(run_exp04.time, 'perf_counter', clock), mock.patch.object(run_exp04, 'log'), \
            mock.patch.object(run_exp04, 'Child', Receiver):
        orchestrator = Aborted(str(tmp), clock)
        try:
            record = orchestrator.run_method(TRIAL, 0, SPEED, dict(LINK), method, 0, [method])
        finally:
            orchestrator.pool.shutdown()
    return orchestrator, record


class Copies(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.datadir, self.stub, self.selection = synthetic_source(self.root)

    def copy(self, streams, name=None, **kwargs):
        output = self.root / (name or f'O{streams}')
        with counted_fsyncs() as calls:
            receiver, sender, outcome = round_trip(self.datadir, self.stub, output, streams, **kwargs)
        return output, calls, receiver, sender, outcome

    def assert_holds_the_selection(self, output):
        """Byte for byte what the selection reads (an unlogged main fork from its init fork), as verify_direct
        compares it, and nothing else."""
        sources = {entry.path: self.selection.source_path(entry.path) for entry in self.selection.inventory.files}
        identical, differences = measure.compare_digests(measure.files_digest(self.datadir, sources),
                                                         measure.files_of(measure.tree_digest(output)))
        self.assertTrue(identical, differences)
        for main, init in RESET.items():
            self.assertEqual((output / main).read_bytes(), (self.datadir / init).read_bytes())

    def test_a_round_trip_is_byte_identical_and_every_file_and_directory_is_fsynced(self):
        inventory = self.selection.inventory
        expected = len(inventory.files) + len(inventory.directories) + 1
        for streams in (1, 4):
            with self.subTest(streams=streams):
                output, calls, receiver, sender, outcome = self.copy(streams)
                self.assertNotIn('receiver_error', outcome)
                self.assertNotIn('sender_error', outcome)
                self.assert_holds_the_selection(output)
                # One fsync of every file, every directory and the top directory: no more, no fewer.
                entries = tree(output)
                self.assertEqual(len(entries), expected)
                inodes = {(info.st_dev, info.st_ino): path for path, info in entries.items()}
                self.assertEqual(len(calls), expected)
                self.assertEqual(sorted(inodes[call] for call in calls), sorted(entries))
                stats = outcome['receiver']
                self.assertEqual((stats['fsync_calls'], stats['fsync_expected']), (expected, expected))
                # Files first, then directories bottom-up, the top directory last.
                order = [inodes[call] for call in calls]
                files = len(inventory.files)
                self.assertEqual(set(order[:files]), {entry.path for entry in inventory.files})
                depths = [path.count('/') for path in order[files:-1]]
                self.assertEqual(depths, sorted(depths, reverse=True))
                self.assertEqual(order[-1], '')
                # Each stream's timers, in the shape measure.parcopy_summary reads.
                self.assertEqual(len(stats['streams']), streams)
                self.assertEqual(sum(stream['bytes'] for stream in stats['streams']),
                                 sum(entry.size for entry in inventory.files))
                for stream in stats['streams']:
                    self.assertLessEqual(stream['first_byte_s'], stream['last_byte_s'])
                    self.assertGreaterEqual(stream['write_s'], 0)
                summary = measure.parcopy_summary(stats)
                self.assertEqual((summary['streams'], summary['fsync_ok']), (streams, True))
                self.assertEqual(summary['bytes'], outcome['sender']['bytes'])
                self.assertIsNotNone(summary['fsync_s'])
                self.assertEqual(outcome['sender']['receiver']['fsync_calls'], expected)
                self.assertEqual(outcome['sender']['plan']['streams'], streams)

    def test_the_sender_opens_every_connection_at_once(self):
        # Each connect takes a round trip (here 0.2 s): one after another, the control connection and 8 data
        # connections would take 9 of them before any byte moved (about 1 s at 120 ms); at once, about one.
        real = socket.create_connection

        def slow(address, *args, **kwargs):
            time.sleep(0.2)
            return real(address, *args, **kwargs)

        with mock.patch.object(parcopy.socket, 'create_connection', slow):
            output, _, _, _, outcome = self.copy(8)
        self.assertNotIn('sender_error', outcome)
        self.assertNotIn('receiver_error', outcome)
        self.assert_holds_the_selection(output)
        sent = outcome['sender']
        connect = sent['connected_s'] - sent['connect_start_s']
        self.assertGreaterEqual(connect, 0.2)
        self.assertLess(connect, 3 * 0.2)
        self.assertEqual(measure.parcopy_connect_s(sent), round(connect, 6))

    def test_a_connection_that_fails_closes_the_others(self):
        opened, real = [], socket.create_connection

        def flaky(address, *args, **kwargs):
            sock = real(address, *args, **kwargs)
            opened.append(sock)
            if len(opened) == 3:
                sock.close()
                raise ConnectionRefusedError('refused')
            return sock

        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(16)
        self.addCleanup(listener.close)
        with mock.patch.object(parcopy.socket, 'create_connection', flaky), \
                self.assertRaises(ConnectionRefusedError):
            parcopy.connect_all('127.0.0.1', listener.getsockname()[1], 4)
        self.assertEqual(len(opened), 5)
        self.assertTrue(all(sock.fileno() == -1 for sock in opened))  # none left open

    def test_files_are_split_across_streams(self):
        lists = parcopy.plan(self.selection, 4, MIB)
        large = next(index for index, entry in enumerate(self.selection.inventory.files) if entry.path == LARGE)
        carriers = {stream for stream, ranges in enumerate(lists) for item in ranges if item.file == large}
        self.assertGreater(len(carriers), 1)
        output, _, _, _, outcome = self.copy(4)
        self.assertNotIn('receiver_error', outcome)
        self.assertEqual((output / LARGE).read_bytes(), (self.datadir / LARGE).read_bytes())

    def test_directories_and_files_get_their_recorded_modes(self):
        previous = os.umask(0o077)  # one that would strip the group bits of 0o750 and 0o640
        try:
            output, _, _, _, outcome = self.copy(4)
        finally:
            os.umask(previous)
        self.assertNotIn('receiver_error', outcome)
        inventory = self.selection.inventory
        self.assertEqual(stat.S_IMODE(os.stat(output).st_mode), ROOT_MODE)
        found = sorted(path.relative_to(output).as_posix() for path in output.rglob('*') if path.is_dir())
        self.assertEqual(found, [entry.path for entry in inventory.directories])
        for entry in inventory.directories:
            self.assertEqual(stat.S_IMODE(os.stat(output / entry.path).st_mode), entry.mode, entry.path)
        for path, mode in DIRECTORY_MODES.items():
            self.assertEqual(stat.S_IMODE(os.stat(output / path).st_mode), mode, path)
        # The directories select() keeps empty are there, empty.
        for path in ('pg_replslot', 'pg_notify', 'pg_wal/archive_status', 'pg_tblspc'):
            self.assertEqual(list((output / path).iterdir()), [], path)
        for entry in inventory.files:
            self.assertEqual(stat.S_IMODE(os.stat(output / entry.path).st_mode), entry.mode, entry.path)
        self.assertEqual(stat.S_IMODE(os.stat(output / 'global/1262').st_mode), 0o400)  # read-only, set after

    def test_write_back_starts_every_writeback_bytes_of_each_range(self):
        started = []

        def start_writeback(fd, offset, length):
            started.append((os.fstat(fd).st_ino, offset, length))
            return True

        with mock.patch.object(parcopy.flush, 'start_writeback', start_writeback):
            output, _, _, _, outcome = self.copy(2, writeback_bytes=256 * KIB)
        self.assertNotIn('receiver_error', outcome)
        large = os.stat(output / LARGE).st_ino
        # 5 MiB of whole 1 MiB ranges, in 256 KiB steps; the last range (3 bytes) is left to the fsync.
        self.assertEqual(sorted((offset, length) for inode, offset, length in started if inode == large),
                         [(offset, 256 * KIB) for offset in range(0, 5 * MIB, 256 * KIB)])
        stats = outcome['receiver']
        self.assertEqual(sum(stream['writeback_calls'] for stream in stats['streams']), len(started))
        self.assertTrue(stats['writeback_effective'])

    def test_write_back_as_the_kernel_allows_it(self):
        output, _, _, _, outcome = self.copy(2, writeback_bytes=256 * KIB)
        stats = outcome['receiver']
        self.assertEqual(stats['writeback_available'], flush.writeback_available())
        calls = sum(stream['writeback_calls'] for stream in stats['streams'])
        if not flush.writeback_available():  # macOS: no sync_file_range
            self.assertFalse(stats['writeback_effective'])
        # Where it ran, it ran; where it was refused, the fsyncs flush it all, and the copy is whole either way.
        self.assertEqual(stats['writeback_effective'], calls > 0)
        self.assertEqual(stats['fsync_calls'], stats['fsync_expected'])
        self.assert_holds_the_selection(output)

    def test_a_dropped_range_fails_the_copy_and_its_verification(self):
        output = self.root / 'O4'
        calls, outcome, item = dropped_range_copy(self.datadir, self.stub, output, 4)
        self.assertEqual((self.selection.inventory.files[item.file].path, item.offset, item.length), (LARGE, 0, MIB))
        self.assertIsInstance(outcome['receiver_error'], TransferError)
        self.assertIn(f'{LARGE} ({4 * MIB + 3} of {5 * MIB + 3} bytes)', str(outcome['receiver_error']))
        self.assertIsInstance(outcome['sender_error'], TransferError)
        self.assertIn('the receiver failed', str(outcome['sender_error']))
        self.assertEqual(calls, [])  # nothing of a failed copy is flushed
        # verify_direct, against what the stopped source's selection holds, fails it too.
        result = self.verify_direct(output)
        self.assertFalse(result['verified'])
        self.assertFalse(result['identical_to_source'])
        self.assertTrue(any(difference.startswith(f'differs: {LARGE}') for difference in result['differences']),
                        result['differences'])
        # In the harness, each end exits as parcopy's command line does (3: a transfer failure), so run_method
        # ends the run in the tool's own error before any verification, and A4 excludes the arm.
        statuses = exit_statuses(outcome)
        self.assertEqual(statuses, {'receiver': 3, 'sender': 3})
        orchestrator, record = aborted_run(self.root / 'harness-run', statuses)
        self.assertEqual((record['status'], record['failure_kind']), ('error', 'tool'))
        self.assertIn(f'dest: {Path(sys.executable).name} exited 3', record['error'])
        self.assertIn('source: python3 exited 3', record['error'])
        self.assertNotIn('verify', orchestrator.events)
        self.assertNotIn('verification', record)
        a4 = orchestrator.a4_summary([record])
        self.assertEqual(a4['excluded_arms'], ['parcopy4'])
        self.assertEqual([(run['method'], run['reason'].split(':')[0]) for run in a4['failed_runs']],
                         [('parcopy4', 'the copy itself failed')])
        self.assertEqual(a4['errors'], [])
        self.assertEqual(measure.summary_row(record)['failure_kind'], 'tool')

    def verify_direct(self, output) -> dict:
        """run_exp04's verify_direct on `output`, against agent.py datadir-digest of the synthetic source."""
        datadir_path, stub = self.datadir, self.stub

        class Direct(T2Stub):
            verify = run_exp04.Orchestrator.verify

            def check_output(self, method, data, *, recovery, query=run_exp04.OUTPUT_QUERY):
                self.events.append(('check_output', recovery))
                return {'ok': True, 'answer': '1', 'ready_seconds': 0.5}

        orchestrator = Direct(str(self.root / 'harness'), FakeClock())
        self.addCleanup(orchestrator.pool.shutdown)
        orchestrator.source.agent = lambda *args, timeout=600: agent.cmd_datadir_digest(
            Namespace(root=str(datadir_path), pg_controldata=str(stub)))
        with mock.patch.object(run_exp04, 'log'):
            result = orchestrator.verify('parcopy4', {'output': output}, {})
        self.assertIn(('check_output', False), orchestrator.events)  # started without recovery
        return result

    def test_verify_direct_counts_the_fsyncs_the_output_needs(self):
        output, calls, _, _, outcome = self.copy(4)
        result = self.verify_direct(output)
        self.assertTrue(result['verified'], result)
        self.assertEqual(result['fsync_expected_from_output'], len(calls))
        self.assertEqual(result['fsync_expected_from_output'], outcome['receiver']['fsync_expected'])

    def test_a_running_cluster_is_refused(self):
        stubs = {'postmaster.pid': (lambda: (self.datadir / 'postmaster.pid').write_text('1\n'), self.stub),
                 "'in production', not 'shut down'": (lambda: None, stub_controldata(
                     self.root / 'running', SAMPLE.replace('shut down', 'in production')))}
        for message, (change, controldata) in stubs.items():
            with self.subTest(message=message):
                change()
                output = self.root / f'refused-{len(list(self.root.iterdir()))}'
                with counted_fsyncs() as calls:
                    _, _, outcome = round_trip(self.datadir, controldata, output, 2)
                self.assertIsInstance(outcome['sender_error'], InvalidInput)
                self.assertIn(message, str(outcome['sender_error']))
                # The waiting receiver is told, and stops without writing anything.
                self.assertIsInstance(outcome['receiver_error'], PeerError)
                self.assertEqual(outcome['receiver_error'].exit_code, 2)
                self.assertIn('the sender gave up before the copy', str(outcome['receiver_error']))
                self.assertFalse(output.exists())
                self.assertEqual(calls, [])
                (self.datadir / 'postmaster.pid').unlink(missing_ok=True)

    def test_the_receiver_refuses_a_non_empty_output_and_a_stream_count_it_does_not_have(self):
        output = self.root / 'used'
        output.mkdir()
        (output / 'x').write_text('x')
        with self.assertRaises(InvalidInput):
            parcopy.Receiver(output, 2, 0).run('127.0.0.1:0', lambda address: self.fail('it listened'))
        receiver = parcopy.Receiver(self.root / 'O-two', 2, 0)
        address, outcome = queue.Queue(), {}

        def receive():
            try:
                receiver.run('127.0.0.1:0', address.put)
            except BaseException as exc:
                outcome['error'] = exc

        thread = threading.Thread(target=receive, daemon=True)
        thread.start()
        host, port = address.get(timeout=30)
        with self.assertRaises((TransferError, OSError)):
            parcopy.Sender(self.datadir, self.stub, 3, MIB).run(f'{host}:{port}')
        thread.join(60)
        self.assertIsInstance(outcome['error'], ProtocolError)
        # Either check may fire first: the header's stream count, or the third data stream arriving before the
        # control connection (the accept order is the network's).
        self.assertRegex(str(outcome['error']), r'the sender has 3 streams, this receiver 2|unexpected data stream 2 \(of 2\)')

    def test_the_command_line_end_to_end(self):
        output, stats = self.root / 'O-cli', self.root / 'stats'
        env = subprocess_env()
        receiver = subprocess.Popen(
            [sys.executable, '-m', 'experiments.network.parcopy', 'receive', '--output', str(output), '--listen',
             '127.0.0.1:0', '--stats-json', str(stats / 'receive.json'), '--streams', '3', '--writeback-bytes',
             str(MIB)], cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            line = receiver.stdout.readline()
            self.assertIn('listening on 127.0.0.1:', line)
            port = line.strip().rsplit(':', 1)[1]
            sender = subprocess.run(
                [sys.executable, '-m', 'experiments.network.parcopy', 'send', '--source', str(self.datadir),
                 '--pg-controldata', str(self.stub), '--connect', f'127.0.0.1:{port}', '--stats-json',
                 str(stats / 'send.json'), '--streams', '3', '--chunk-bytes', str(MIB)], cwd=REPO_ROOT, env=env,
                capture_output=True, text=True, timeout=120)
            rest, _ = receiver.communicate(timeout=120)
        finally:
            if receiver.poll() is None:
                receiver.kill()
                receiver.wait()
        self.assertEqual(sender.returncode, 0, sender.stderr)
        self.assertEqual(receiver.returncode, 0, rest)
        self.assert_holds_the_selection(output)
        received = json.loads((stats / 'receive.json').read_text())
        sent = json.loads((stats / 'send.json').read_text())
        self.assertEqual(received['fsync_calls'], received['fsync_expected'])
        self.assertEqual((received['error'], sent['error']), (None, None))
        self.assertEqual(sent['receiver']['ok'], True)
        self.assertEqual(len(sent['streams']), 3)
        self.assertEqual(sent['excluded'], self.selection.excluded_summary())
        # A refusal: exit 2, the reason on stderr, the stats written.
        (self.datadir / 'postmaster.pid').write_text('1\n')
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = parcopy.main(['send', '--source', str(self.datadir), '--pg-controldata', str(self.stub),
                                 '--connect', f'127.0.0.1:{port}', '--stats-json', str(stats / 'refused.json'),
                                 '--streams', '2'])
        self.assertEqual(code, 2)
        self.assertIn('postmaster.pid', stderr.getvalue())
        self.assertIn('postmaster.pid', json.loads((stats / 'refused.json').read_text())['error'])


@unittest.skipUnless(available(), SKIP_REASON)
class RealCluster(unittest.TestCase):
    QUERY = 'select count(*), sum(length(v)) from t'

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.servers = Servers(cls.root)
        cls.datadir = cls.root / 'D'
        initdb(cls.datadir)
        cls.expected = cls.servers.run_sql(
            cls.datadir, 'create table t as select g as id, md5(g::text) as v from generate_series(1, 50000) g',
            'create unlogged table u as select g from generate_series(1, 5000) g', 'checkpoint', cls.QUERY)

    @classmethod
    def tearDownClass(cls):
        cls.servers.close()
        cls.temp.cleanup()

    def test_a_running_cluster_is_refused(self):
        self.servers.pg_ctl(self.datadir, 'start')
        try:
            with self.assertRaises(InvalidInput) as refused:
                parcopy.Sender(self.datadir, CONTROLDATA, 2, MIB).run('127.0.0.1:9')
            self.assertIn('postmaster.pid', str(refused.exception))
        finally:
            self.servers.pg_ctl(self.datadir, 'stop')

    def test_the_copy_starts_without_recovery(self):
        output = self.root / 'parcopy-out'
        with counted_fsyncs() as calls:
            _, _, outcome = round_trip(self.datadir, CONTROLDATA, output, 4, chunk_bytes=256 * KIB,
                                       writeback_bytes=64 * KIB)
        self.assertNotIn('receiver_error', outcome)
        self.assertNotIn('sender_error', outcome)
        selection = datadir.select(self.datadir, CONTROLDATA)
        inventory = selection.inventory
        self.assertEqual(len(calls), len(inventory.files) + len(inventory.directories) + 1)
        sources = {entry.path: selection.source_path(entry.path) for entry in inventory.files}
        self.assertTrue(any(path != read for path, read in sources.items()))  # the unlogged table's main fork
        identical, differences = measure.compare_digests(measure.files_digest(self.datadir, sources),
                                                         measure.files_of(measure.tree_digest(output)))
        self.assertTrue(identical, differences)
        self.assertEqual(self.servers.run_sql(output, self.QUERY), self.expected)
        self.assertEqual(self.servers.run_sql(output, 'select count(*) from u'), '0')
        log = (self.servers.logs / f'{output.name}.log').read_text()
        self.assertIn('database system was shut down at', log)
        self.assertNotIn('redo starts', log)


# --- the orchestrator: run_parcopy, check_parcopy and A5 ---------------------------------------

RECEIVE_STATS = {'streams': [{'bytes': 1_100_000_000, 'first_byte_s': 0.11, 'last_byte_s': 1.62, 'write_s': 0.9},
                             {'bytes': 1_090_000_000, 'first_byte_s': 0.12, 'last_byte_s': 1.6, 'write_s': 0.8}],
                 'fsync_s': 0.24, 'fsync_calls': 1310, 'fsync_expected': 1310}


class Parcopying(T2Stub):
    """T2Stub whose parcopy receiver leaves its stats, as the real one does; snapshots, the receiver's
    listening and each remote command's working directory are recorded."""

    receive_stats = RECEIVE_STATS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.remote_cwds = []

    def snapshot_source(self):
        self.events.append('snapshot')
        return super().snapshot_source()

    def snapshot_dest(self):
        self.events.append('snapshot')
        return super().snapshot_dest()

    def start_receiver(self, method, paths, holder):
        commands = super().start_receiver(method, paths, holder)
        self.events.append('listening')
        return commands

    def remote_command(self, argv, log_name, *, cwd=None):
        self.remote_cwds.append(cwd)
        return super().remote_command(argv, log_name, cwd=cwd)

    def run_parcopy(self, method, paths):
        phases = super().run_parcopy(method, paths)
        parcopy.write_json(paths['receive_stats'], self.receive_stats)
        return phases

    def tool_reported(self, method, paths, phases):
        if method in measure.PARCOPY_METHODS:
            return run_exp04.Orchestrator.tool_reported(self, method, paths, phases)
        return super().tool_reported(method, paths, phases)


class OrchestratorRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        for patcher in (mock.patch.object(run_exp04.time, 'perf_counter', self.clock),
                        mock.patch.object(run_exp04, 'log')):
            patcher.start()
            self.addCleanup(patcher.stop)
        FakeChild.started.clear()

    def orchestrator(self, cls=Parcopying):
        orchestrator = cls(self.tmp.name, self.clock)
        self.addCleanup(orchestrator.pool.shutdown)
        orchestrator.window_writes = 2_200_000_000  # check (b): the output on the md array in the window
        return orchestrator

    def run_method(self, orchestrator, method, child=FakeChild):
        with mock.patch.object(run_exp04, 'Child', child):
            return orchestrator.run_method(TRIAL, 0, SPEED, dict(LINK), method, 0, [method])

    @staticmethod
    def kinds(orchestrator):
        kinds = []
        for event in orchestrator.events:
            if isinstance(event, str):
                kinds.append(event)
            elif event[0] in ('dest', 'source'):
                kinds.append((event[0], Path(event[1][0]).name, *event[1][1:4]))
            else:
                kinds.append((event[0], event[1][0] if isinstance(event[1], tuple) else event[1]))
        return kinds

    def test_a_parcopy_run_is_one_window_in_pgdelta_directs_frame(self):
        for method in measure.PARCOPY_METHODS:
            with self.subTest(method=method):
                FakeChild.started.clear()
                orchestrator = self.orchestrator()
                record = self.run_method(orchestrator, method)
                self.assertEqual(record['status'], 'pass', record.get('traceback'))
                self.assertEqual(list(record['phases']), ['receiver_transfer'])
                phase = record['phases']['receiver_transfer']
                self.assertEqual(list(phase['steps']), ['receiver_start', 'transfer'])
                streams = str(measure.PARCOPY_VARIANTS[method]['streams'])
                [receiver] = FakeChild.started
                self.assertEqual(receiver.argv[1:4], ['-m', 'experiments.network.parcopy', 'receive'])
                self.assertEqual(receiver.argv[receiver.argv.index('--streams') + 1], streams)
                self.assertEqual(phase['receiver']['argv'], receiver.argv)
                send = [event[1] for event in orchestrator.events if isinstance(event, tuple) and event[0] == 'source']
                self.assertEqual(len(send), 1)
                self.assertEqual(send[0][1:4], ['-m', 'experiments.network.parcopy', 'send'])
                self.assertEqual(send[0][send[0].index('--source') + 1], orchestrator.source_data)
                self.assertEqual(orchestrator.remote_cwds, [orchestrator.remote_repo])
                kinds = self.kinds(orchestrator)
                # Stopped before the cache drop, restarted after the verification; one counter window, and no
                # counter read between the receiver listening and the sender's start.
                scripts = [kind for kind in kinds if kind[0] == 'source_script']
                self.assertEqual(scripts, [('source_script', 'stop-clean'), ('source_script', 'start')])
                self.assertLess(kinds.index(('source_script', 'stop-clean')), kinds.index('drop_caches'))
                self.assertLess(kinds.index('verify'), kinds.index(('source_script', 'start')))
                listening = kinds.index('listening')
                self.assertEqual(kinds[listening + 1][:4], ('source', Path(send[0][0]).name, '-m',
                                                            'experiments.network.parcopy'))
                self.assertEqual(kinds.count('snapshot'), 4)
                row = measure.summary_row(record)
                self.assertEqual(row['counter_windows'], 1)
                self.assertEqual(row['total_s'], round(row['receiver_start_s'] + row['transfer_s'], 3))
                self.assertEqual((row['parcopy_streams'], row['parcopy_fsync_calls'], row['parcopy_fsync_expected']),
                                 (2, 1310, 1310))
                self.assertEqual(row['tool_s2d_bytes'], 2_190_000_000)
                self.assertEqual(row['tier'], 'bound')
                self.assertEqual(record['durability']['fsync_count'],
                                 {'calls': 1310, 'expected': 1310, 'expected_from_output': None, 'ok': True})
                self.assertEqual((row['durable'], row['durability_ok']), (True, True))

    def test_a_wrong_fsync_count_fails_check_a_but_not_the_run(self):
        class Short(Parcopying):
            receive_stats = {**RECEIVE_STATS, 'fsync_calls': 1309}

        record = self.run_method(self.orchestrator(Short), 'parcopy8')
        self.assertEqual(record['status'], 'pass')  # its status is its verification's (A5 is the analysis's)
        self.assertEqual(record['durability']['dirty_writeback_increase_bytes'], 0)
        self.assertEqual((record['durability']['a_ok'], record['durability']['ok']), (False, False))
        self.assertFalse(measure.summary_row(record)['durability_ok'])

    def test_a_receiver_that_never_listens_fails_without_a_transfer(self):
        class Silent(FakeChild):
            LOG = ''
            EXIT = 2

        orchestrator = self.orchestrator()
        record = self.run_method(orchestrator, 'parcopy4', Silent)
        self.assertEqual((record['status'], record['failure_kind']), ('error', 'tool'))
        self.assertEqual(list(record['phases']['receiver_transfer']['steps']), ['receiver_start'])
        self.assertNotIn('source', [kind[0] for kind in self.kinds(orchestrator) if isinstance(kind, tuple)])
        self.assertIn(('source_script', 'start'), self.kinds(orchestrator))
        self.assertEqual(orchestrator.a4_summary([record])['excluded_arms'], ['parcopy4'])

    def test_a_failed_sender_stops_the_receiver_and_the_source_restarts(self):
        class Refused(Parcopying):
            def remote_command(self, argv, log_name, *, cwd=None):
                result = super().remote_command(argv, log_name, cwd=cwd)
                return {**result, 'exit_code': 2}

        orchestrator = self.orchestrator(Refused)
        record = self.run_method(orchestrator, 'parcopy8')
        self.assertEqual((record['status'], record['failure_kind']), ('error', 'tool'))
        self.assertIn('source: python3 exited 2', record['error'])
        [receiver] = FakeChild.started
        self.assertEqual(len(receiver.signals), 1)
        self.assertIn(('source_script', 'start'), self.kinds(orchestrator))
        self.assertEqual(orchestrator.a4_summary([record])['excluded_arms'], ['parcopy8'])

    def test_an_error_of_the_harness_around_the_copy_is_no_a4_failure(self):
        class Unstoppable(Parcopying):
            def _source_script(self, script, *args, timeout=7200):
                if args[0] == 'stop-clean':
                    raise run_exp04.CommandError('source_cluster.py stop-clean failed (1): pg_ctl: server does '
                                                 'not shut down')
                return super()._source_script(script, *args, timeout=timeout)

        orchestrator = self.orchestrator(Unstoppable)
        record = self.run_method(orchestrator, 'parcopy8')
        self.assertEqual((record['status'], record['failure_kind']), ('error', 'harness'))
        self.assertEqual(record['phases'], {})  # it failed before its timed phase
        a4 = orchestrator.a4_summary([record])
        self.assertEqual((a4['failed_runs'], a4['excluded_arms']), ([], []))
        self.assertEqual([(run['method'], run['failure_kind']) for run in a4['errors']], [('parcopy8', 'harness')])
        # A run that passes has no kind.
        self.assertIsNone(self.run_method(self.orchestrator(), 'parcopy4')['failure_kind'])


class Preflight(unittest.TestCase):
    CAPABLE = {'sendfile': True, 'writeback': True}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def check(self, *, dest=CAPABLE, source=CAPABLE, send_help=None):
        """check_parcopy with each VM's --help from this repository's parcopy, and the capabilities given."""
        orchestrator = T2Stub(self.tmp.name, FakeClock())
        self.addCleanup(orchestrator.pool.shutdown)
        orchestrator.methods = ['full', 'parcopy4', 'parcopy8']
        real = subprocess.run
        calls = []

        def local(argv, **kwargs):
            calls.append(('dest', argv))
            if '--help' in argv:
                return real(argv, **kwargs)
            return mock.Mock(stdout=f'{json.dumps(dest)}\n', returncode=0)

        def source_agent(*args, timeout=600):
            argv = list(args[args.index('--') + 1:])
            calls.append(('source', argv))
            if '--help' in argv:
                text = real([sys.executable, *argv[1:]], cwd=REPO_ROOT, capture_output=True, text=True,
                            env=subprocess_env()).stdout if send_help is None else send_help
                return {'exit_code': 0, 'log_tail': text}
            return {'exit_code': 0, 'log_tail': f'noise\n{json.dumps(source)}\n'}

        orchestrator.source.agent = source_agent
        with mock.patch.object(run_exp04.subprocess, 'run', local):
            return orchestrator.check_parcopy(), calls

    def test_both_ends_have_what_parcopy_needs(self):
        result, calls = self.check()
        self.assertEqual(result['options']['receive'], ['--output', '--listen', '--stats-json', '--streams',
                                                        '--writeback-bytes'])
        self.assertEqual(result['options']['send'], ['--source', '--pg-controldata', '--connect', '--stats-json',
                                                     '--streams', '--chunk-bytes'])
        self.assertEqual((result['dest']['options'], result['source']['options']), (True, True))
        self.assertEqual(result['source']['capabilities'], self.CAPABLE)
        self.assertIn(('dest', [sys.executable, '-m', 'experiments.network.parcopy', 'receive', '--help']), calls)
        self.assertIn(('source', ['python3', '-m', 'experiments.network.parcopy', 'send', '--help']), calls)
        self.assertIn(('source', ['python3', '-c', run_exp04.PARCOPY_CAPABILITIES]), calls)
        # The real capabilities() answers every key the check reads.
        self.assertTrue({'sendfile', 'writeback'} <= set(parcopy.capabilities()))

    def test_it_refuses_without_write_back_sendfile_or_an_option(self):
        cases = {"dest can't start write-back": {'dest': {'sendfile': True, 'writeback': False}},
                 'the source has no os.sendfile': {'source': {'sendfile': False, 'writeback': True}},
                 'parcopy send on source lacks': {'send_help': 'usage: parcopy send --source --connect'}}
        for message, change in cases.items():
            with self.subTest(message=message):
                with self.assertRaises(SystemExit) as refused:
                    self.check(**change)
                self.assertIn(message, str(refused.exception))


class FsyncCount(unittest.TestCase):
    @staticmethod
    def record(method='parcopy8', calls=12, expected=12, held=None, increase=0):
        record = {'method': method, 'phases': {'receiver_transfer': {
            'memory': {'dest': {'dirty_writeback_bytes_before': MIB, 'dirty_writeback_bytes_after': MIB + increase}},
            'disk': {'dest': {'md127': {'write_bytes': 3_000_000_000}}}}},
            'tool_reported': {'parcopy_summary': measure.parcopy_summary(
                {'streams': [{'bytes': 1}], 'fsync_calls': calls, 'fsync_expected': expected})},
            'verification': {} if held is None else {'fsync_expected_from_output': held}}
        return record

    def test_a5_a_parcopy_run_must_fsync_what_its_inventory_holds(self):
        output = 2_100_000_000
        passed = measure.durability_check(self.record(held=12), output)
        self.assertEqual((passed['a_ok'], passed['ok']), (True, True))
        self.assertEqual(passed['fsync_count'], {'calls': 12, 'expected': 12, 'expected_from_output': 12, 'ok': True})
        for label, record in (('one short', self.record(calls=11)),
                              ('against the output', self.record(held=13)),
                              ('one too many', self.record(calls=13))):
            with self.subTest(label=label):
                failed = measure.durability_check(record, output)
                self.assertEqual((failed['a_ok'], failed['ok'], failed['fsync_count']['ok']), (False, False, False))
        # Without the receiver's counts, check (a) can't pass; a dirty increase over the limit still fails it.
        unknown = measure.durability_check(self.record(calls=None), output)
        self.assertEqual((unknown['a_ok'], unknown['ok']), (None, None))
        dirty = measure.durability_check(self.record(calls=None, increase=16 * MIB), output)
        self.assertEqual(dirty['a_ok'], False)
        # Other arms have no fsync count.
        self.assertNotIn('fsync_count', measure.durability_check(self.record(method='full'), output))

    def test_the_fsyncs_a_tree_needs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'a' / 'b').mkdir(parents=True)
            (root / 'empty').mkdir()
            for path in ('PG_VERSION', 'a/1', 'a/b/2'):
                (root / path).write_bytes(b'x')
            self.assertEqual(measure.fsyncs_expected(measure.tree_digest(root)), 3 + 3 + 1)


if __name__ == '__main__':
    unittest.main()
