"""EXP-13's pgdelta switches: the request window (--window) and the protocol timers (pgdelta.waits).

At the defaults nothing on the wire changes: one fixed session (tests/pgdelta_frames.py) puts exactly
the bytes on the wire that it put before either was built.
"""
import io
import json
import os
from pathlib import Path
import random
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock

from pgdelta import cli, protocol, sender, transport, waits
from pgdelta.errors import InvalidInput
from pgdelta.protocol import Channel
from pgdelta.receiver import run_receiver
from pgdelta.sender import Inbox, run_sender

from pgdelta_frames import KEY, NODE_BLOCKS, build, record, session_fingerprint
from pgdelta_support import REPO_ROOT, channel_pair, python_command, snapshot, subprocess_env

# pgdelta_frames' session as the code before EXP-13 sent it (commit BASE_COMMIT, recorded with
# `python tests/pgdelta_frames.py [WINDOW]`). The window orders the source-to-dest stream; nothing else.
BASE_COMMIT = '3c0b8fa'
BEFORE_SOURCE_TO_DEST = {
    None: '4394016e6275fcbd879e0edc5881ae6a2b287d078cd8e2f8d98512770ddd361e',
    1: '6fc7ec8c68411cb9b80329db20b237802a7dd83fe82e9cd6defacaefd089b62c',
    4: '4394016e6275fcbd879e0edc5881ae6a2b287d078cd8e2f8d98512770ddd361e',
    16: '739062a3ac5e86219135e5b3111cda1a78c40f3bbcd117e858fac8e66bb971eb',
}
BEFORE = {
    'source_to_dest_frames': {'COMPLETE': 1, 'FILE_DONE': 29, 'HELLO': 1, 'LEAF_REQUEST': 20, 'PAGE_DATA': 38},
    'dest_to_source': {'ACCEPT': '42358891ee5839a5faf520ac95d8936e42eb7e3229190f43ffcef097967fd285',
                       'BASIS_NODES': '9fb99aa5329937996a5e09c15542dd5756b9dde637011008832c3be838a7e3a8',
                       'LEAF_DIGESTS': '14d8cbf35d0f9cf66ca7d1536700856f5419b761f4451c50ed9c7e8b08d16e35'},
    'dest_to_source_frames': {'ACCEPT': 1, 'BASIS_NODES': 27, 'LEAF_DIGESTS': 20},
    'complete_published': True,
    'complete_receiver': 'b8d24c5dc0a237bec6e65b8d5984c2d3b485a36aaf5312eec1477edb733447ed',
    'complete_receiver_keys': [
        'basis_copies', 'basis_copy_bytes', 'basis_identity', 'bytes_copied', 'bytes_read', 'bytes_written',
        'copy_seconds', 'directories', 'files', 'files_digest', 'files_verified', 'fsync', 'fsync_effective',
        'fsync_mode', 'fsync_seconds', 'fsync_wait_seconds', 'gate_reason', 'hash_basis_seconds', 'in_place',
        'in_place_tree', 'jobs', 'leaf_nodes_cached', 'leaf_nodes_reread', 'mode', 'output',
        'page_bytes_received', 'pages_copied', 'pages_received', 'pool_first_ready_seconds',
        'pool_ready_seconds', 'pool_start_seconds', 'prefetch', 'published', 'rebuild_seconds', 'receipt',
        'reuse_policy', 'source_identity', 'switches', 'verify_seconds', 'verify_wait_seconds',
        'whole_file_copies', 'worker_start_method', 'writeback_calls', 'writeback_seconds'],
}
SENDER_TIMERS = ['connect_s'] + [f'{kind}_{field}' for kind in waits.KINDS
                                 for field in ('wait_s', 'wait_busy_s', 'wait_idle_s', 'waits', 'waits_over_1ms')]
SENDER_TIMERS += ['page_send_s', 'other_send_s', 'send_queue']


def before(window=None):
    return dict(BEFORE, source_to_dest=BEFORE_SOURCE_TO_DEST[window])


def run(*args, timeout=120):
    return subprocess.run(python_command(*args), capture_output=True, text=True, cwd=REPO_ROOT,
                          env=subprocess_env(), timeout=timeout)


class TreeTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.basis = build(self.root)


class WireTests(TreeTestCase):
    """Byte for byte on the wire: the defaults, and --window 4, send what the code sent before EXP-13."""

    def test_the_defaults_put_the_same_bytes_on_the_wire_as_before(self):
        for window in (None, 4):
            with self.subTest(window=window):
                self.assertEqual(session_fingerprint(**({} if window is None else {'window': window})),
                                 before(window))

    def test_windows_1_and_16_send_what_the_same_window_sent_before(self):
        for window in (1, 16):
            with self.subTest(window=window):
                self.assertEqual(session_fingerprint(window=window), before(window))

    @unittest.skipUnless(shutil.which('git'), 'needs git')
    def test_the_commit_before_puts_the_same_bytes_on_the_wire(self):
        """The same session run by the code of BASE_COMMIT, in a child Python, against this code."""
        archive = subprocess.run(['git', '-C', str(REPO_ROOT), 'archive', '--format=tar', BASE_COMMIT, 'pgdelta'],
                                 capture_output=True, timeout=60)
        if archive.returncode:
            self.skipTest(f'commit {BASE_COMMIT} is not in this checkout (a shallow clone?)')
        old = self.root / 'before'
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            tar.extractall(old, filter='data')
        self.assertFalse((old / 'pgdelta' / 'waits.py').exists())
        env = {key: value for key, value in os.environ.items() if not key.startswith('PYTHON')}
        env['PYTHONPATH'] = str(old)
        for window in (4, 16):
            with self.subTest(window=window):
                result = subprocess.run([sys.executable, str(REPO_ROOT / 'tests' / 'pgdelta_frames.py'), str(window)],
                                        capture_output=True, text=True, env=env, timeout=120)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), session_fingerprint(window=window))
                self.assertEqual(json.loads(result.stdout), before(window))


class WindowSwitchTests(TreeTestCase):
    """--window N on send and local: today's 4 by default; it reorders the frames and changes nothing sent."""

    def test_send_and_local_default_to_todays_window(self):
        parser = cli.build_parser()
        send = parser.parse_args(['send', '--source', 'S', '--connect', 'h:1'])
        local = parser.parse_args(['local', '--source', 'S', '--basis', 'B', '--output', 'O'])
        self.assertEqual((sender.DEFAULT_WINDOW, send.window, local.window), (4, 4, 4))
        self.assertEqual(cli._sender_kwargs(send, None)['window'], 4)
        self.assertEqual(cli._sender_kwargs(parser.parse_args(['send', '--source', 'S', '--connect', 'h:1',
                                                               '--window', '16']), None)['window'], 16)
        for command in ('send', 'local'):
            self.assertIn('--window', run(command, '--help').stdout)

    def test_window_must_be_a_positive_whole_number(self):
        for value in ('0', '-1', 'four', '1.5'):
            with self.subTest(value=value):
                result = run('local', '--source', self.source, '--basis', self.basis, '--output', self.root / 'O',
                             '--window', value)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('--window', result.stderr)
        self.assertFalse((self.root / 'O').exists())

    def test_window_reaches_the_sender_with_identical_output_for_1_4_and_16(self):
        results = {}
        for window in (1, 4, 16):
            with self.subTest(window=window):
                output, stats_path = self.root / f'O{window}', self.root / f'stats{window}.json'
                result = run('local', '--source', self.source, '--basis', self.basis, '--output', output,
                             '--reuse-any-basis', '--node-blocks', NODE_BLOCKS, '--jobs', 1, '--window', window,
                             '--stats-json', stats_path)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(snapshot(output), snapshot(self.source))
                stats = json.loads(stats_path.read_text())
                self.assertEqual(stats['window'], window)
                results[window] = (stats['files'], stats['pages'], stats['nodes'], stats['leaf_requests'],
                                   stats['bytes_source_to_dest'], stats['bytes_dest_to_source']['leaf_digests'],
                                   stats['bytes_dest_to_source']['node_digests'])
        self.assertEqual(results[1], results[4])
        self.assertEqual(results[16], results[4])
        self.assertGreater(results[4][1]['sent'], 0)

    def test_the_window_reorders_the_frames_and_changes_none_of_them(self):
        frames = {}
        for window in (1, 4, 16):
            _, _, channel = record(self.source, self.basis, self.root / f'O{window}', window=window)
            frames[window] = channel.frames_sent
        self.assertNotEqual(frames[1], frames[4])
        self.assertNotEqual(frames[16], frames[4])
        for window in (1, 16):
            self.assertEqual(sorted(frames[window]), sorted(frames[4]))
            # Each type's frames keep their order: only how LEAF_REQUESTs interleave with the rest moves.
            for code in protocol.MESSAGES:
                self.assertEqual([f for f in frames[window] if f[0] == code], [f for f in frames[4] if f[0] == code])


# --- the timers, unit by unit ---------------------------------------------------------------------------


class Clock:
    """A clock the test moves by hand."""

    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class ScriptedQueue:
    """A send queue whose readings, (unacknowledged, unsent), come from a list; the last one repeats."""
    available, why_not = True, None

    def __init__(self, *readings):
        self.readings, self.reads = list(readings), 0

    def read(self):
        reading = self.readings[min(self.reads, len(self.readings) - 1)]
        self.reads += 1
        if isinstance(reading, BaseException):
            raise reading
        return reading


class NoQueue:
    available, why_not = False, 'no socket: the channel is not a TCP connection'

    def read(self):
        raise AssertionError('an unavailable queue is never read')


def one_wait(timers, clock, kind, slices):
    """A blocked wait of `kind` whose wake-ups come after each of `slices` seconds."""
    timers.begin(kind)
    for index, seconds in enumerate(slices):
        clock.now += seconds
        if index < len(slices) - 1:
            timers.tick()
    timers.end()


class WaitTimersTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()

    def timers(self, queue, threshold=None):
        return waits.WaitTimers(queue, threshold, clock=self.clock)

    def test_a_slice_is_busy_while_bytes_are_unsent_or_more_than_the_path_holds_are_in_flight(self):
        # (unacknowledged, unsent): unsent; in flight above 1000; in flight at 1000; nothing queued.
        timers = self.timers(ScriptedQueue((5000, 10), (1001, 0), (1000, 0), (0, 0)), threshold=1000)
        one_wait(timers, self.clock, 'leaf', [0.004, 0.003, 0.002, 0.001])
        summary = timers.summary()
        self.assertAlmostEqual(summary['leaf_wait_busy_s'], 0.007)
        self.assertAlmostEqual(summary['leaf_wait_idle_s'], 0.003)
        self.assertAlmostEqual(summary['leaf_wait_s'], 0.010)
        self.assertEqual(timers.readings, 4)

    def test_without_a_threshold_only_unsent_bytes_make_a_slice_busy(self):
        timers = self.timers(ScriptedQueue((5000, 0), (5000, 1), (0, 0)))
        one_wait(timers, self.clock, 'basis', [0.002, 0.003, 0.005])
        summary = timers.summary()
        self.assertEqual((summary['basis_wait_busy_s'], summary['basis_wait_idle_s']), (0.003, 0.007))
        self.assertIsNone(summary['send_queue']['busy_inflight_bytes'])

    def test_each_slice_takes_the_reading_at_its_start_and_the_parts_add_up_to_the_wait(self):
        timers = self.timers(ScriptedQueue((9, 9), (0, 0)))
        one_wait(timers, self.clock, 'complete', [0.0105, 0.25])
        one_wait(timers, self.clock, 'complete', [0.0004])  # its one reading is (0, 0) again: idle
        summary = timers.summary()
        self.assertEqual(summary['complete_wait_busy_s'], 0.0105)
        self.assertEqual(summary['complete_wait_idle_s'], round(0.25 + 0.0004, 6))
        self.assertEqual(summary['complete_wait_s'], round(0.0105 + 0.25 + 0.0004, 6))
        self.assertEqual((summary['complete_waits'], summary['complete_waits_over_1ms']), (2, 1))

    def test_waits_are_counted_by_kind_with_those_over_1_ms(self):
        timers = self.timers(ScriptedQueue((0, 0)))
        for seconds in (0.0002, 0.0009, 0.0011, 0.5):
            one_wait(timers, self.clock, 'leaf', [seconds])
        one_wait(timers, self.clock, 'accept', [0.12])
        summary = timers.summary(connect_seconds=0.0601234567)
        self.assertEqual((summary['leaf_waits'], summary['leaf_waits_over_1ms']), (4, 2))
        self.assertEqual((summary['accept_waits'], summary['accept_waits_over_1ms']), (1, 1))
        self.assertEqual((summary['basis_waits'], summary['basis_wait_s'], summary['basis_wait_busy_s']), (0, 0, 0))
        self.assertEqual(summary['connect_s'], 0.060123)

    def test_sends_are_timed_with_page_data_apart(self):
        timers = self.timers(NoQueue())
        timers.sent(True, 0.25)
        timers.sent(False, 0.125)
        timers.sent(True, 0.5)
        summary = timers.summary()
        self.assertEqual((summary['page_send_s'], summary['other_send_s']), (0.75, 0.125))

    def test_without_readings_the_totals_stay_and_the_parts_are_null(self):
        timers = self.timers(NoQueue(), threshold=4096)
        self.assertIsNone(timers.poll)  # nothing to read, so a wait blocks until it is woken
        one_wait(timers, self.clock, 'leaf', [0.003, 0.004])
        summary = timers.summary()
        self.assertEqual(summary['leaf_wait_s'], 0.007)
        self.assertEqual((summary['leaf_wait_busy_s'], summary['leaf_wait_idle_s']), (None, None))
        self.assertEqual(summary['send_queue'], {'split': False, 'why_not': NoQueue.why_not,
                                                 'busy_inflight_bytes': 4096, 'poll_s': None, 'readings': 0})

    def test_a_failed_reading_drops_the_parts_and_keeps_the_totals(self):
        timers = self.timers(ScriptedQueue((10, 10), OSError(9, 'Bad file descriptor')))
        self.assertEqual(timers.poll, waits.POLL_SECONDS)
        one_wait(timers, self.clock, 'leaf', [0.002, 0.003, 0.004])
        self.assertIsNone(timers.poll)
        summary = timers.summary()
        self.assertEqual(summary['leaf_wait_s'], 0.009)
        self.assertEqual((summary['leaf_wait_busy_s'], summary['leaf_wait_idle_s']), (None, None))
        self.assertFalse(summary['send_queue']['split'])
        self.assertIn('Bad file descriptor', summary['send_queue']['why_not'])

    def test_the_summary_has_exp13s_names(self):
        summary = self.timers(ScriptedQueue((0, 0))).summary()
        self.assertEqual(list(summary), SENDER_TIMERS)
        self.assertEqual(summary['send_queue'], {'split': True, 'why_not': None, 'busy_inflight_bytes': None,
                                                 'poll_s': 0.001, 'readings': 0})


class SendQueueTests(unittest.TestCase):
    def test_unavailable_without_a_socket_or_off_linux(self):
        self.assertFalse(waits.SendQueue(None).available)
        self.assertIn('not a TCP connection', waits.SendQueue(None).why_not)
        with socket.socket() as sock:
            queue = waits.SendQueue(sock, platform='darwin')
        self.assertFalse(queue.available)
        self.assertIn('Linux', queue.why_not)

    def test_reads_unsent_then_unacknowledged_with_the_linux_ioctls(self):
        calls = []

        def ioctl(fd, request, argument):
            calls.append((fd, request, argument))
            value = {waits.SIOCOUTQNSD: 1200}.get(request, 70000)
            return value.to_bytes(4, sys.byteorder)

        sock = mock.Mock(fileno=mock.Mock(return_value=42))
        with mock.patch('fcntl.ioctl', ioctl):
            queue = waits.SendQueue(sock, platform='linux')
            self.assertTrue(queue.available, queue.why_not)
            self.assertEqual(queue.read(), (70000, 1200))
        self.assertEqual(waits.SIOCOUTQNSD, 0x894B)
        import termios
        siocoutq = getattr(termios, 'TIOCOUTQ', waits.TIOCOUTQ_GENERIC)
        self.assertEqual(calls[-2:], [(42, 0x894B, bytes(4)), (42, siocoutq, bytes(4))])

    def test_a_refused_ioctl_leaves_it_unavailable(self):
        sock = mock.Mock(fileno=mock.Mock(return_value=42))
        with mock.patch('fcntl.ioctl', side_effect=OSError(25, 'Inappropriate ioctl for device')):
            queue = waits.SendQueue(sock, platform='linux')
        self.assertFalse(queue.available)
        self.assertIn('Inappropriate ioctl', queue.why_not)


class BusyInflightSwitchTests(TreeTestCase):
    def test_busy_inflight_bytes_is_a_send_switch_of_whole_bytes(self):
        parser = cli.build_parser()
        self.assertIsNone(parser.parse_args(['send', '--source', 'S', '--connect', 'h:1']).busy_inflight_bytes)
        for value in ('-1', 'lots', '2.5'):
            with self.subTest(value=value):
                result = run('send', '--source', self.source, '--connect', '127.0.0.1:1', '--busy-inflight-bytes',
                             value)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('--busy-inflight-bytes', result.stderr)
        self.assertNotIn('--busy-inflight-bytes', run('local', '--help').stdout)


class InboxWaitTests(unittest.TestCase):
    def setUp(self):
        self.send, self.receive = channel_pair()
        self.addCleanup(self.send.close)
        self.addCleanup(self.receive.close)

    def test_a_reply_already_there_is_no_wait(self):
        timers = waits.WaitTimers(ScriptedQueue((0, 0)))
        inbox = Inbox(self.send, timers)
        inbox.basis[3] = (8192, b'')
        self.assertEqual(inbox.wait(lambda: inbox.basis.pop(3, None), 'basis digests', 'basis'), (8192, b''))
        self.assertEqual((timers.kinds['basis'].count, timers.readings), (0, 0))

    def test_a_blocked_wait_reads_the_queue_every_poll_and_counts_under_its_kind(self):
        queue = ScriptedQueue((100, 100))
        timers = waits.WaitTimers(queue)
        inbox = Inbox(self.send, timers)

        def arrive():
            time.sleep(0.05)
            with inbox.cond:
                inbox.accept = {'mode': 'full'}
                inbox.cond.notify_all()

        thread = threading.Thread(target=arrive)
        thread.start()
        self.assertEqual(inbox.wait(lambda: inbox.accept, 'ACCEPT', 'accept'), {'mode': 'full'})
        thread.join()
        kind = timers.kinds['accept']
        self.assertEqual(kind.count, 1)
        self.assertGreaterEqual(kind.seconds, 0.045)
        self.assertAlmostEqual(kind.busy + kind.idle, kind.seconds, places=9)
        self.assertEqual(kind.idle, 0)
        # Woken every millisecond or so; a loaded machine wakes less often, never more.
        self.assertGreaterEqual(timers.readings, 5)
        self.assertLessEqual(timers.readings, kind.seconds / waits.POLL_SECONDS + 3)

    def test_a_wait_that_fails_is_still_timed(self):
        timers = waits.WaitTimers(ScriptedQueue((0, 0)))
        inbox = Inbox(self.send, timers)

        def fail():
            time.sleep(0.02)
            with inbox.cond:
                inbox.closed = True
                inbox.cond.notify_all()

        thread = threading.Thread(target=fail)
        thread.start()
        with self.assertRaises(protocol.ChannelClosed):
            inbox.wait(lambda: inbox.complete, 'COMPLETE', 'complete')
        thread.join()
        self.assertEqual(timers.kinds['complete'].count, 1)
        self.assertGreaterEqual(timers.kinds['complete'].idle, 0.015)

    def test_without_a_kind_or_timers_a_wait_is_not_timed(self):
        timers = waits.WaitTimers(ScriptedQueue((0, 0)))
        for inbox, kind in ((Inbox(self.send, timers), None), (Inbox(self.send), 'leaf')):
            def arrive(inbox=inbox):
                with inbox.cond:
                    inbox.leaves[(0, 0)] = b'x'
                    inbox.cond.notify_all()

            threading.Timer(0.01, arrive).start()
            self.assertEqual(inbox.wait(lambda inbox=inbox: inbox.leaves.pop((0, 0), None), 'leaf', kind), b'x')
        self.assertEqual((sum(k.count for k in timers.kinds.values()), timers.readings), (0, 0))


# --- the timers in sessions ---------------------------------------------------------------------------


class Slowed(Channel):
    """A Channel that sleeps before sending some message types (the receiver's replies, the sender's HELLO)."""
    delays = {}

    def send(self, code, *parts):
        if code in self.delays:
            time.sleep(self.delays[code])
        super().send(code, *parts)


def session(source, basis, output, *, sender_class=Channel, receiver_class=Channel, receiver_options=None,
            **sender_kwargs):
    """One session over pipes in this process: (sender stats, receiver stats)."""
    send_channel, receive_channel = channel_pair(sender_class, receiver_class)
    result = {}

    def receive():
        try:
            result['stats'] = run_receiver(receive_channel, basis, output, reuse_any_basis=True,
                                           **(receiver_options or {}))
        except BaseException as exc:
            result['error'] = exc
        finally:
            receive_channel.close()

    thread = threading.Thread(target=receive, name='test-receiver')
    thread.start()
    try:
        stats = run_sender(send_channel, source, node_blocks=NODE_BLOCKS, key=KEY, **sender_kwargs)
    finally:
        send_channel.close()
        thread.join(60)
    if 'error' in result:
        raise result['error']
    return stats, result['stats']


def check_parts(test, timers, split=True):
    """Every wait's busy and idle parts add up to it (each rounded to a microsecond), or are null."""
    for kind in waits.KINDS:
        busy, idle, total = (timers[f'{kind}_wait_busy_s'], timers[f'{kind}_wait_idle_s'],
                             timers[f'{kind}_wait_s'])
        if split:
            test.assertAlmostEqual(busy + idle, total, delta=2e-6, msg=kind)
        else:
            test.assertEqual((busy, idle), (None, None), kind)
    test.assertEqual(timers['send_queue']['split'], split)


class SlowedChannelTests(TreeTestCase):
    """The timers are in both ends' statistics and grow with the receiver's delays (over pipes: no split)."""
    DELAY = 0.03

    def test_every_timer_is_reported_and_grows_with_the_receivers_delays(self):
        fast, _ = session(self.source, self.basis, self.root / 'fast', window=1)

        class SlowReplies(Slowed):
            delays = {protocol.ACCEPT: self.DELAY, protocol.LEAF_DIGESTS: self.DELAY, protocol.COMPLETE: self.DELAY}

        slow, _ = session(self.source, self.basis, self.root / 'slow', receiver_class=SlowReplies, window=1)
        self.assertEqual(snapshot(self.root / 'slow'), snapshot(self.source))
        for stats in (fast, slow):
            self.assertEqual(list(stats['protocol_timers']), SENDER_TIMERS)
            self.assertEqual(stats['leaf_requests'], BEFORE['source_to_dest_frames']['LEAF_REQUEST'])
            self.assertIsNone(stats['protocol_timers']['connect_s'])  # pipes: nothing connected
            check_parts(self, stats['protocol_timers'], split=False)
            self.assertIn('not a TCP connection', stats['protocol_timers']['send_queue']['why_not'])
        timers, before_timers = slow['protocol_timers'], fast['protocol_timers']
        self.assertGreaterEqual(timers['accept_wait_s'], self.DELAY * 0.9)
        self.assertGreaterEqual(timers['complete_wait_s'], self.DELAY * 0.9)
        # Window 1: every file's leaf reply is waited for, and the receiver sends each one late.
        self.assertGreaterEqual(timers['leaf_wait_s'] + timers['basis_wait_s'],
                                before_timers['leaf_wait_s'] + before_timers['basis_wait_s'] + 10 * self.DELAY)
        self.assertGreaterEqual(timers['leaf_waits_over_1ms'], 10)
        self.assertGreater(timers['page_send_s'], 0)
        self.assertGreater(timers['other_send_s'], 0)

    def test_a_wait_is_busy_while_the_queue_holds_bytes_and_idle_after(self):
        """Queue readings stubbed: the sender's bytes count as unsent until LAG after its last send."""
        lag = 0.04
        last_send = [time.perf_counter()]

        class Lagging:
            available, why_not = True, None

            def __init__(self, sock):
                pass

            def read(self):
                unsent = int(time.perf_counter() < last_send[0] + lag)
                return unsent, unsent

        class Noting(Channel):
            def send(self, code, *parts):
                super().send(code, *parts)
                last_send[0] = time.perf_counter()

        class SlowComplete(Slowed):
            delays = {protocol.COMPLETE: 0.2}

        with mock.patch.object(waits, 'SendQueue', Lagging):
            stats, _ = session(self.source, self.basis, self.root / 'O', sender_class=Noting,
                               receiver_class=SlowComplete)
        timers = stats['protocol_timers']
        check_parts(self, timers)
        # COMPLETE is the sender's last send: its wait reads busy for about LAG, then idle until the reply.
        self.assertGreaterEqual(timers['complete_wait_busy_s'], lag * 0.5)
        self.assertLessEqual(timers['complete_wait_busy_s'], lag + 0.05)
        self.assertGreaterEqual(timers['complete_wait_idle_s'], 0.1)
        self.assertGreater(timers['send_queue']['readings'], 50)

    def test_failure_statistics_carry_the_timers(self):
        with self.assertRaises(InvalidInput) as caught:
            session(self.source, self.basis, self.root / 'O', busy_inflight_bytes=-1)
        stats = caught.exception.pgdelta_stats
        self.assertEqual(stats['status'], 'failed')
        self.assertEqual(list(stats['protocol_timers']), SENDER_TIMERS)
        self.assertIn('busy_inflight_bytes', stats['error'])


def slow_reader(channel, delay):
    """Make a receiver's channel sleep after each page-data frame it reads."""
    recv = channel.recv

    def slowly():
        message = recv()
        if message is not None and message[0] == protocol.PAGE_DATA:
            time.sleep(delay)
        return message

    channel.recv = slowly


class SlowedTcpTests(TreeTestCase):
    """Over a local TCP connection whose receiver reads slowly: the sender's waits split into busy and idle.

    On Linux the split reads the real send queue (SIOCOUTQNSD, SIOCOUTQ). Elsewhere the readings are
    stubbed with what the sender has written and the receiver hasn't read yet.
    """

    def setUp(self):
        super().setUp()
        # A new 4 MiB file near the end of the inventory, sent whole in 1 MiB frames that the slow reader takes
        # one at a time: COMPLETE goes out with MBs still queued, whatever the buffers on the way hold.
        big = self.source / 'base' / '5' / '30000'
        big.write_bytes(random.Random(1302).randbytes(4 << 20))
        os.chmod(big, 0o600)

    def tcp_session(self, busy_inflight_bytes=None):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        # A small receive buffer, so what the slow reader leaves fills the window and the rest stays unsent.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        result = {}

        def receive():
            try:
                channel = transport.accept_channel(listener)
            except BaseException as exc:
                result['error'] = exc
                return
            result['channel'] = channel
            slow_reader(channel, 0.02)
            channel.send = self.delayed_complete(channel.send)
            try:
                result['stats'] = run_receiver(channel, self.basis, self.root / 'O', reuse_any_basis=True)
            except BaseException as exc:
                result['error'] = exc
            finally:
                channel.close()

        thread = threading.Thread(target=receive, name='test-receiver')
        thread.start()
        channel = transport.connect_channel(*listener.getsockname())
        stub = None
        if not sys.platform.startswith('linux'):
            def stub(sock):
                return Unread(channel, result)
        try:
            with mock.patch.object(waits, 'SendQueue', stub or waits.SendQueue):
                stats = run_sender(channel, self.source, node_blocks=NODE_BLOCKS, key=KEY,
                                   busy_inflight_bytes=busy_inflight_bytes)
        finally:
            channel.close()
            thread.join(60)
        if 'error' in result:
            raise result['error']
        return stats

    @staticmethod
    def delayed_complete(send):
        def send_later(code, *parts):
            if code == protocol.COMPLETE:
                time.sleep(0.15)
            return send(code, *parts)
        return send_later

    def test_a_wait_is_busy_while_bytes_are_unsent_and_the_parts_add_up(self):
        stats = self.tcp_session(busy_inflight_bytes=1 << 30)
        self.assertEqual(snapshot(self.root / 'O'), snapshot(self.source))
        timers = stats['protocol_timers']
        check_parts(self, timers)
        self.assertGreater(timers['connect_s'], 0)
        self.assertEqual(timers['send_queue']['busy_inflight_bytes'], 1 << 30)
        self.assertGreater(timers['send_queue']['readings'], 50)
        # COMPLETE goes out right behind the last pages, which the slow reader hasn't read yet...
        self.assertGreater(timers['complete_wait_busy_s'], 0.01)
        # ...and once it has, the receiver publishes and holds its COMPLETE back for 0.15 s.
        self.assertGreater(timers['complete_wait_idle_s'], 0.1)


class Unread:
    """Stand-in send queue where the ioctls are missing: bytes the sender wrote that the receiver hasn't read."""
    available, why_not = True, None

    def __init__(self, sender_channel, result):
        self.sender_channel, self.result = sender_channel, result

    def read(self):
        receiver = self.result.get('channel')
        unread = max(0, self.sender_channel.sent.total - (receiver.received.total if receiver else 0))
        return unread, unread


@unittest.skipUnless(sys.platform.startswith('linux'), 'SIOCOUTQ and SIOCOUTQNSD are Linux ioctls')
class LinuxSendQueueTests(unittest.TestCase):
    """The real readings, on a TCP connection whose peer reads nothing until told to."""

    def test_bytes_are_unsent_while_the_peer_reads_nothing_and_none_once_it_has(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        client = socket.create_connection(listener.getsockname())
        self.addCleanup(client.close)
        peer, _ = listener.accept()
        self.addCleanup(peer.close)
        queue = waits.SendQueue(client)
        self.assertTrue(queue.available, queue.why_not)
        self.assertEqual(queue.read(), (0, 0))
        client.setblocking(False)
        sent = 0
        try:
            while sent < 64 << 20:
                sent += client.send(bytes(1 << 16))
        except BlockingIOError:
            pass
        unacknowledged, unsent = queue.read()
        self.assertGreater(unsent, 0)
        self.assertLessEqual(unsent, unacknowledged)
        self.assertLessEqual(unacknowledged, sent)

        timers, cond = waits.WaitTimers(queue), threading.Condition()

        def wait(seconds):
            with cond:
                timers.begin('leaf')
                deadline = time.perf_counter() + seconds
                while time.perf_counter() < deadline:
                    cond.wait(timers.poll)
                    timers.tick()
            timers.end()

        wait(0.05)
        busy = timers.kinds['leaf'].busy
        self.assertGreaterEqual(busy, 0.045)
        self.assertEqual(timers.kinds['leaf'].idle, 0)

        peer.settimeout(10)
        received = 0
        while received < sent:
            received += len(peer.recv(1 << 20))
        deadline = time.monotonic() + 10
        while queue.read() != (0, 0) and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(queue.read(), (0, 0))
        wait(0.03)
        self.assertEqual(timers.kinds['leaf'].busy, busy)
        self.assertGreaterEqual(timers.kinds['leaf'].idle, 0.025)
        summary = timers.summary()
        self.assertTrue(summary['send_queue']['split'])
        self.assertAlmostEqual(summary['leaf_wait_busy_s'] + summary['leaf_wait_idle_s'], summary['leaf_wait_s'],
                               delta=2e-6)


if __name__ == '__main__':
    unittest.main()
