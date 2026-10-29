"""Source side: hash S, compare against the basis digests, send only differing pages.

A reader thread drains everything the receiver sends (all of it small:
digests and acknowledgements), so neither side can block the other while
page data is in flight. Files are processed in inventory order with up to
`window` files hashed ahead (DEFAULT_WINDOW, or `--window`), so leaf-digest
round trips overlap hashing. Each file's nodes are hashed on a pool of
`jobs` worker processes (or threads, for comparison), or in the main thread
when jobs is 1. The waits for the receiver, and the time blocked sending,
are timed for EXP-13 (pgdelta.waits).

The source is a finished backup S (`source_kind` "backup", default), a
cleanly stopped data directory read directly ("datadir", pgdelta.datadir),
of which only what pg_basebackup would send is listed, or a running
cluster's data directory read in backup mode ("live", pgdelta.live): the
listed files are read only after pg_backup_start returns, and the WAL from
the backup's start to its stop and the backup_label are appended to the
inventory (APPEND) after pg_backup_stop.
"""
from __future__ import annotations

import base64
import json
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
import os
import queue
import threading
import time

from pgdelta import __version__, datadir, fastpath, live, matching, protocol, selector, subpage, transport, waits
from pgdelta import compress as compression
from pgdelta.errors import (EXIT_TRANSFER_FAILURE, InvalidInput, PeerError, PgDeltaError,
                            ProtocolError, TransferError, VerificationError)
from pgdelta.hashing import (BLOCK, DEFAULT_ALGORITHM, DIGEST_SIZE, NODE_BLOCKS, SUB_BLOCK, FilesDigest, Hasher,
                             new_key, offer)
from pgdelta.identity import check_major
from pgdelta.inplace import COMMITTED
from pgdelta.inventory import require_plain, scan
from pgdelta.protocol import ChannelClosed
from pgdelta.receiver import RECEIPT_SUFFIX
from pgdelta.tree import (BACKENDS, DEFAULT_BACKEND, DEFAULT_JOBS, HashPool, hash_file, node_count, open_regular,
                          pread_exact, span_length)

STATS_SCHEMA_VERSION = 1
DEFAULT_WINDOW = 4
RUN_BYTES = 1 << 20
# --read-ahead (EXP-64b): how far the fast path's reader threads may read ahead of the sending thread.
READ_AHEAD_BYTES = 64 << 20
JOIN_SECONDS = 10.0


def prepare_source(source):
    inventory = require_plain(scan(source), 'source')
    if any(entry.path == 'postmaster.pid' for entry in inventory.files):
        raise InvalidInput('source contains postmaster.pid: v0 needs a completed backup, '
                           'not a running data directory')
    return inventory


class Inbox:
    """Reader thread for receiver -> sender messages."""

    def __init__(self, channel, timers=None):
        self.channel = channel
        self.cond = threading.Condition()
        self.accept = self.complete = self.error = None
        self.basis, self.leaves, self.blocks = {}, {}, {}
        self.subpage = False  # set once ACCEPT took the subpage_offer: BLOCK_DIGESTS may arrive
        # --diverged-basis: set once FORK_POINT was sent, so one BASIS_WAL may arrive; then its reply.
        self.basis_wal_expected, self.basis_wal = False, None
        self.closed = False
        # The sender's protocol timers (pgdelta.waits.WaitTimers); without them waits aren't timed.
        self.timers = timers
        # For the stall watch (StallWatch): when the last message arrived, and since when and for what a wait has
        # blocked (time.monotonic(); None while no wait blocks).
        self.last_message = 0.0
        self.waiting_since = self.waiting_for = None
        self.thread = threading.Thread(target=self._run, name='pgdelta-inbox', daemon=True)

    def start(self):
        self.thread.start()

    def join(self, timeout):
        if self.thread.is_alive():
            self.thread.join(timeout)

    def settle(self, timeout):
        """Wait until the receiver has reported an error or the stream has ended."""
        with self.cond:
            self.cond.wait_for(lambda: self.error is not None or self.closed, timeout)

    def _run(self):
        try:
            while True:
                message = self.channel.recv()
                if message is None:
                    return
                self.last_message = time.monotonic()
                code, payload = message
                with self.cond:
                    self._dispatch(code, payload)
                    self.cond.notify_all()
                if code == protocol.COMPLETE:
                    return
                if code == protocol.ERROR:
                    break
        except PgDeltaError as exc:
            self._set_error(exc)
        except Exception as exc:
            self._set_error(TransferError(f'reading from the receiver failed: {exc!r}'))
        finally:
            with self.cond:
                self.closed = True
                self.cond.notify_all()
        # After a failure, keep reading so the receiver never blocks writing to us while it cleans up.
        self.channel.discard_input()

    def _set_error(self, exc):
        with self.cond:
            if self.error is None:
                self.error = exc

    def fail(self, exc):
        """Fail the run from another thread (the stall watch): a wait in progress or to come raises `exc`."""
        with self.cond:
            if self.error is None:
                self.error = exc
            self.cond.notify_all()

    def _dispatch(self, code, payload):
        if code == protocol.ERROR:
            info = protocol.decode_json(payload, 'ERROR')
            if self.error is None:
                self.error = PeerError(f'receiver: {info.get("message", "unspecified error")}',
                                       info.get('exit_code'))
        elif self.accept is None:
            if code != protocol.ACCEPT:
                raise ProtocolError(f'expected ACCEPT, received {protocol.name(code)}')
            self.accept = protocol.decode_json(payload, 'ACCEPT')
        elif code == protocol.BASIS_NODES:
            index, size, nodes = protocol.decode_basis_nodes(payload)
            if index in self.basis:
                raise ProtocolError(f'duplicate BASIS_NODES for file {index}')
            self.basis[index] = (size, nodes)
        elif code == protocol.LEAF_DIGESTS:
            index, entries = protocol.decode_leaf_digests(payload)
            for node, blob in entries:
                self.leaves[(index, node)] = blob
        elif code == protocol.BLOCK_DIGESTS and self.subpage:
            index, entries = protocol.decode_block_digests(payload)
            for node, blob in entries:
                self.blocks[(index, node)] = blob
        elif code == protocol.BASIS_WAL and self.basis_wal_expected and self.basis_wal is None:
            self.basis_wal = fastpath.check_basis_wal(protocol.decode_json(payload, 'BASIS_WAL'))
        elif code == protocol.COMPLETE:
            self.complete = protocol.decode_json(payload, 'COMPLETE')
        else:
            raise ProtocolError(f'unexpected {protocol.name(code)} from the receiver')

    def wait(self, ready, what, kind=None):
        """ready()'s first value that isn't None. If it has to block for it, the wait is timed under
        `kind` (pgdelta.waits.KINDS), waking every poll interval to read the send queue."""
        timers = self.timers if kind is not None else None
        blocked = False
        try:
            with self.cond:
                while True:
                    if self.error is not None:
                        raise self.error
                    value = ready()
                    if value is not None:
                        return value
                    if self.closed:
                        raise ChannelClosed(f'receiver closed the connection while the sender waited for {what}')
                    if self.waiting_since is None:
                        self.waiting_since, self.waiting_for = time.monotonic(), what
                    if timers is None:
                        self.cond.wait()
                        continue
                    if not blocked:
                        timers.begin(kind)
                        blocked = True
                    self.cond.wait(timers.poll)
                    timers.tick()
        finally:
            self.waiting_since = self.waiting_for = None
            if blocked:
                timers.end()

    def check(self):
        if self.error is not None:
            raise self.error


class StallWatch:
    """While a live source holds backup mode and its slot: give up on a receiver that has stopped.

    The sender is stalled when it has been blocked on the receiver for
    `seconds`: in one send() the receiver doesn't take (Channel.sending_since),
    or in a wait during which no message arrived (Inbox.waiting_since and
    last_message). Time spent working (hashing, reading S, psql) is no stall.
    A thread checks every `interval`; on a stall it fails the inbox with a
    TransferError naming it and aborts the channel, so the run fails and
    pg_backup_stop is called and the slot dropped. Should the run still not
    have ended `grace` seconds later (a send that even an abort cannot end),
    it kills the psql session itself, which ends backup mode and drops the
    temporary slot server side. Over TCP the connection also gets keepalives
    (pgdelta.transport.keep_alive) for a peer lost while nothing is sent.
    """

    GRACE_SECONDS = 30.0

    def __init__(self, channel, inbox, seconds, *, on_give_up=None, interval=None, grace=GRACE_SECONDS,
                 clock=time.monotonic):
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not seconds > 0:
            raise InvalidInput(f'the live timeout must be a positive number of seconds, not {seconds!r}')
        self.channel, self.inbox, self.seconds = channel, inbox, float(seconds)
        self.on_give_up, self.grace, self.clock = on_give_up, grace, clock
        self.interval = interval if interval is not None else min(1.0, self.seconds / 10)
        self.error = None
        self._stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name='pgdelta-stall-watch', daemon=True)

    def stalled(self, now):
        """What the sender has been blocked on for `seconds` by `now` (time.monotonic()), or None."""
        since = self.channel.sending_since
        if since is not None and now - since >= self.seconds:
            return 'sending'
        since, what = self.inbox.waiting_since, self.inbox.waiting_for
        if since is not None and now - max(since, self.inbox.last_message) >= self.seconds:
            return f'waiting for {what}'
        return None

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        """The slot is gone or the run is over: stop watching. Safe to repeat."""
        self._stopped.set()
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(5)

    def _run(self):
        while not self._stopped.wait(self.interval):
            what = self.stalled(self.clock())
            if what is None:
                continue
            self.error = TransferError(
                f'the receiver made no progress for {self.seconds:g} s while the sender was {what}, with backup '
                f'mode and the replication slot held on the source: giving up, so that backup mode ends and the '
                f'slot is dropped (--live-timeout)')
            self.inbox.fail(self.error)
            self.channel.abort()
            if not self._stopped.wait(self.grace) and self.on_give_up is not None:
                self.on_give_up()
            return


@dataclass
class _Job:
    index: int
    path: str
    tree: object
    basis_size: object = None
    plan: matching.NodePlan = field(default_factory=matching.NodePlan)
    entry: object = None
    # Live: the digest of what the copy is made of, the pages sent as sent (pgdelta.live.composite_digest).
    digest: object = None


class _ReadAhead:
    """--read-ahead (EXP-64b): the fast path's chosen runs, read on `threads` threads in sending order, up to
    READ_AHEAD_BYTES ahead of the sending thread, so that the source's scattered reads overlap the sending and the
    receiver's writes instead of alternating with them (EXP-64: 99 s of reads one at a time while the receiver
    waited 105 s). `runs` lists (file index, entry, first page, page count, bytes) in the order they are sent."""

    def __init__(self, reader, runs, threads, window, block):
        self.reader, self.runs, self.block, self.window = reader, iter(runs), block, window
        self.pool = ThreadPoolExecutor(threads, thread_name_prefix='pgdelta-read')
        self.pending, self.handles, self.inflight, self.exhausted = deque(), {}, 0, False
        self.wait_seconds, self.reads = 0.0, 0
        try:
            self._fill()
        except BaseException:
            self.close()
            raise

    def _fill(self):
        while not self.exhausted and (self.inflight < self.window or not self.pending):
            try:
                index, entry, first, count, length = next(self.runs)
            except StopIteration:
                self.exhausted = True
                return
            handle = self.handles.get(index)
            if handle is None:
                handle = self.handles[index] = self.reader.open(entry)
            future = self.pool.submit(handle.read, first * self.block, length)
            self.pending.append((index, first, count, length, future))
            self.inflight += length

    def next(self, index, first):
        """The next run's bytes; it must be file `index`'s run starting at page `first`."""
        if not self.pending:
            raise TransferError('read-ahead ran out of runs')
        run_index, run_first, _, length, future = self.pending.popleft()
        if (run_index, run_first) != (index, first):
            raise TransferError(f'read-ahead out of order: file {run_index} page {run_first}, expected {index} {first}')
        started = time.perf_counter()
        data = future.result()
        self.wait_seconds += time.perf_counter() - started
        self.inflight -= length
        self.reads += 1
        self._fill()
        return data

    def done(self, index):
        """File `index`'s runs are all taken, so no read of it is in flight: its handle is closed. (After a failure,
        close() closes the rest once the threads have stopped.)"""
        handle = self.handles.pop(index, None)
        if handle is not None:
            handle.close()

    def close(self):
        for *_, future in self.pending:
            future.cancel()
        self.pool.shutdown(wait=True, cancel_futures=True)
        for handle in self.handles.values():
            handle.close()
        self.handles.clear()


def _parse_accept(accept, file_count):
    mode, reason = accept.get('mode'), accept.get('reason')
    if mode not in ('reuse', 'full') or not isinstance(reason, str):
        raise ProtocolError('ACCEPT needs mode "reuse" or "full" and a reason')
    identity = accept.get('basis_identity')
    if identity is not None and not isinstance(identity, dict):
        raise ProtocolError('ACCEPT basis_identity must be an object or null')
    policy = accept.get('reuse_policy')
    if policy is not None and not isinstance(policy, str):
        raise ProtocolError('ACCEPT reuse_policy must be a string or null')
    basis_sizes = {}
    for item in accept.get('basis_files', []):
        if (not isinstance(item, list) or len(item) != 2 or type(item[0]) is not int
                or type(item[1]) is not int or not 0 <= item[0] < file_count or item[1] < 0
                or item[0] in basis_sizes):
            raise ProtocolError(f'invalid ACCEPT basis_files entry {item!r}')
        basis_sizes[item[0]] = item[1]
    return mode == 'reuse', reason, policy, identity, basis_sizes


class _Sender:
    def __init__(self, channel, source, identity, block, node_blocks, high_churn_node_fraction,
                 window, key, jobs, hash_backend, source_kind=datadir.DEFAULT_SOURCE_KIND, pg_controldata=None,
                 busy_inflight_bytes=None, pg_conn=None, psql=None, live_timeout=None,
                 hash_algorithm=DEFAULT_ALGORITHM, compress=None, checkpoint=live.DEFAULT_CHECKPOINT, subpage=False,
                 fast_path=None, read_ahead=0, plan_out=None, fill_gaps=0, diverged_basis=False, selector=None):
        self.started = time.perf_counter()
        # --read-ahead (EXP-64b): the fast path's chosen pages read on this many threads ahead of sending (0: one run
        # at a time in the sending thread, as in EXP-64); and what the reading did.
        self.read_ahead, self.ahead = read_ahead, None
        # --plan-out (EXP-68): where the fast path's plan is written, once the guard let it run.
        self.plan_out = plan_out
        # --fill-gaps (EXP-70): clean gaps of at most this many pages between chosen pages are sent too; how many.
        self.fill_gaps, self.pages_filled = fill_gaps, 0
        # --diverged-basis (idea 1): offered in HELLO; for an old primary past the fork, what B's WAL added.
        self.diverged_basis, self.diverged_counts = diverged_basis, None
        # --selector (EXP-74): the disks' limits (pgdelta.selector.Disks) it predicts with; what it chose.
        self.selector, self.selector_counts = selector, None
        # --fast-path summaries (pgdelta.fastpath): offered in HELLO; `fast` is the summaries' block plan once the
        # guard let it run, and the listed files then go by fast_path's rules, B unread.
        self.fast_path, self.fast = fast_path, None
        self.fast_counts = {'used': False, 'reasons': [], 'summaries': 0, 'summary_rows': 0, 'relations': 0,
                            'files_chosen': 0, 'files_whole': 0, 'pages_chosen': 0, 'guard_seconds': 0.0,
                            'summarizer_wait_seconds': None, 'summarized_lsn': None,
                            # Wall-clock times (time.time()), to place the guard and the sending on a timeline of
                            # the disks (EXP-76): the guard's start, and the first and last listed file sent.
                            'guard_started_utc': None, 'sending_started_utc': None, 'sending_ended_utc': None}
        self.hash_algorithm = hash_algorithm
        # --compress zstd: offered in HELLO; the codec exists once the receiver accepted it (pgdelta.compress).
        self.compress, self.codec = compress, None
        # EXP-46: runs are compressed on a pool of `jobs` threads (zstd releases the GIL) while this thread reads
        # and sends; EXP-50: queued into tasks of compress.TASK_BYTES, up to two tasks per thread wait, in order.
        # `seconds` sums the compressing, `wait_seconds` is the time this thread waited for the oldest task.
        self.compressor, self.compressing, self.queued, self.queued_bytes = None, deque(), [], 0
        self.compress_counts = {'runs': 0, 'runs_compressed': 0, 'raw_bytes': 0, 'wire_bytes': 0, 'seconds': 0.0,
                                'wait_seconds': 0.0, 'threads': 0, 'tasks': 0}
        # --subpage (pgdelta.subpage): offered in HELLO; used once the receiver accepted it.
        self.subpage, self.subpage_used = subpage, False
        self.subpage_counts = {'pages_whole': 0, 'pages_partial': 0, 'pages_same': 0, 'blocks_sent': 0,
                               'block_digest_bytes': 0, 'hash_seconds': 0.0}
        self.channel, self.source, self.identity = channel, Path(source), identity
        self.source_kind, self.pg_controldata, self.selection = source_kind, pg_controldata, None
        # A live source (pgdelta.live): its backup session, and how its files are read; the files appended
        # after pg_backup_stop; the watch that gives up on a stalled receiver while the slot is held.
        self.pg_conn, self.psql = pg_conn, psql
        self.live_timeout = live.DEFAULT_TIMEOUT if live_timeout is None else live_timeout
        self.checkpoint = checkpoint
        self.live = self.reader = self.watch = None
        self.appended = []
        # Whether the receiver refreshes B in place (from ACCEPT; null until it arrives).
        self.in_place = None
        self.block, self.node_blocks = block, node_blocks
        self.high_churn = high_churn_node_fraction
        self.window = max(1, window)
        self.key = key
        self.jobs, self.pool, self.hash_backend = jobs, None, hash_backend
        self.pool_start_seconds = 0.0
        # EXP-13: the waits for the receiver, each split by the socket's send queue against
        # busy_inflight_bytes, and the time blocked sending (pgdelta.waits).
        self.busy_inflight_bytes = busy_inflight_bytes
        self.waits = waits.WaitTimers(waits.SendQueue(getattr(channel, 'sock', None)), busy_inflight_bytes)
        self.inbox = Inbox(channel, self.waits)
        self.leaf_requests = 0
        self.mode = self.reason = self.policy = self.basis_identity = self.receiver_stats = None
        self.directory_count = 0
        self.files = dict.fromkeys(('total', 'unchanged', 'changed', 'new', 'size_mismatch'), 0)
        self.pages = dict.fromkeys(('total', 'sent', 'reused', 'sent_bytes'), 0)
        self.nodes = dict.fromkeys(('total', 'matched', 'descended', 'sent_whole'), 0)
        self.high_churn_files = 0
        self.file_count = None
        self.files_digest = FilesDigest()
        self.hash_source = 0.0
        self.protocol_started = self.protocol_seconds = None

    def run(self):
        try:
            stats = self._run()
        except BaseException as exc:
            failure = exc
            try:
                failure = self._fail(exc)
            finally:
                # A live source: end backup mode and drop the slot before the statistics record how.
                self._close_live()
                failure.pgdelta_stats = self.stats('failed', getattr(failure, 'exit_code',
                                                   EXIT_TRANSFER_FAILURE), str(failure))
            if failure is exc:
                raise
            raise failure from exc
        finally:
            self._close_live()
            if self.pool is not None:
                self.pool.close()
            if self.compressor is not None:
                self.compressor.shutdown(wait=False, cancel_futures=True)
        if self.live is not None and self.live.variant is not None:
            raise self._wrong_by_design(stats)
        return stats

    def _wrong_by_design(self, stats):
        """A broken test variant ran to the end: the run fails (exit 4), however complete it looks."""
        output = f'B, which {COMMITTED},' if self.in_place else 'the output the receiver published'
        error = VerificationError(
            f'the deliberately broken test variant ran ({live.READ_BEFORE_START_ENV}: {self.live.variant}): the '
            f'files were read before pg_backup_start returned, so {output} is wrong by design and must not be '
            f'used; refresh it again without the switch, or replace it with a full copy')
        error.pgdelta_stats = dict(stats, status='failed', exit_code=error.exit_code, error=str(error))
        return error

    def _close_live(self):
        if self.live is not None:
            if self.watch is not None and self.watch.error is not None:
                self.live.timed_out = True
            try:
                self.live.close()
            except Exception:  # never hide the run's own failure; the session's end aborts backup mode anyway
                pass
        if self.watch is not None:
            self.watch.stop()

    def _kill_session(self):
        """The stall watch's last resort, when even aborting the channel did not end the run: end the psql session,
        which ends backup mode and drops the temporary slot server side."""
        session = self.live.session if self.live is not None else None
        if session is not None:
            session.kill()

    def _fail(self, exc):
        if self.watch is not None and self.watch.error is not None and not isinstance(exc, PeerError):
            # The receiver stalled while the slot was held: whatever the run then hit (a wait failed by the watch,
            # a send cut off by its abort) has that cause, and the aborted channel can carry no ERROR.
            return self.watch.error
        if isinstance(exc, PeerError):
            return exc
        if isinstance(exc, ChannelClosed):
            self.inbox.settle(JOIN_SECONDS)
            return self.inbox.error if isinstance(self.inbox.error, PeerError) else exc
        if isinstance(exc, PgDeltaError):
            self._send_error(exc.exit_code, str(exc))
        elif isinstance(exc, KeyboardInterrupt):
            self._send_error(EXIT_TRANSFER_FAILURE, 'sender interrupted')
        else:
            self._send_error(EXIT_TRANSFER_FAILURE, f'sender internal error: {exc!r}')
        return exc

    def _send_error(self, exit_code, message):
        try:
            self.channel.send_json(protocol.ERROR, {'exit_code': exit_code, 'message': message})
        except PgDeltaError:
            pass

    def _resolve_block(self):
        identity = self.identity or {}
        identity_block = None if identity.get('untrustworthy') else identity.get('block_size')
        if self.block is None:
            return identity_block or BLOCK
        if identity_block is not None and self.block != identity_block:
            raise InvalidInput(f'block size {self.block} does not match the source cluster '
                               f'block size {identity_block}')
        return self.block

    def _run(self):
        fastpath.check_mode(self.fast_path, self.source_kind, self.subpage, self.read_ahead, self.plan_out,
                            self.fill_gaps, self.diverged_basis, self.selector)
        if self.source_kind not in datadir.SOURCE_KINDS:
            raise InvalidInput(f'source kind must be one of {", ".join(datadir.SOURCE_KINDS)}, '
                               f'not {self.source_kind!r}')
        check_major(self.source, 'source')
        if self.source_kind == 'datadir':
            # Refuses unless the cluster is cleanly shut down; lists only what pg_basebackup would send.
            self.selection = datadir.select(self.source, self.pg_controldata)
            if self.identity is None:
                self.identity = self.selection.identity
        elif self.source_kind == 'live':
            self._begin_live()
        self.block = self._resolve_block()
        protocol.check_parameters(self.block, self.node_blocks, InvalidInput)
        if self.high_churn is not None and not 0 <= self.high_churn <= 1:
            raise InvalidInput('high_churn_node_fraction must be between 0 and 1')
        if type(self.jobs) is not int or self.jobs < 1:
            raise InvalidInput(f'jobs must be a positive integer, not {self.jobs!r}')
        if self.busy_inflight_bytes is not None and (type(self.busy_inflight_bytes) is not int
                                                     or self.busy_inflight_bytes < 0):
            raise InvalidInput(f'busy_inflight_bytes must be a whole number of bytes, '
                               f'not {self.busy_inflight_bytes!r}')
        if self.hash_backend not in BACKENDS:
            raise InvalidInput(f'hash backend must be one of {", ".join(BACKENDS)}, not {self.hash_backend!r}')
        if self.compress not in (None, *compression.ALGORITHMS):
            raise InvalidInput(f'compress must be one of {", ".join(compression.ALGORITHMS)} or None, '
                               f'not {self.compress!r}')
        if self.compress is not None and not compression.usable(self.compress):
            raise InvalidInput(f'--compress {self.compress} needs Python 3.14 (compression.zstd) or the zstandard '
                               f'package here')
        inventory = prepare_source(self.source) if self.selection is None else self.selection.inventory
        self.directory_count, self.file_count = len(inventory.directories), len(inventory.files)
        key = self.key or new_key()
        # --hash auto: offer BLAKE3 too where it can hash here; the receiver picks in ACCEPT, so the hasher and the
        # pool start after it. With only BLAKE2b to offer, nothing is offered and the session is as before.
        offered = offer() if self.hash_algorithm == 'auto' else None
        if offered == [DEFAULT_ALGORITHM]:
            offered, self.hash_algorithm = None, DEFAULT_ALGORITHM
        if offered is None:
            self._start_hashing(key)
        self.protocol_started = time.perf_counter()
        self.inbox.start()
        hello = {
            'protocol_version': protocol.PROTOCOL_VERSION,
            'block': self.block,
            'node_blocks': self.node_blocks,
            'key': base64.b64encode(key).decode('ascii'),
            'source_identity': self.identity,
            'root_mode': inventory.root_mode,
            'directories': [[entry.path, entry.mode] for entry in inventory.directories],
            'files': [[entry.path, entry.size, entry.mode] for entry in inventory.files],
        }
        if self.live is not None:
            hello['appends'] = True  # the WAL and backup_label follow in APPEND, after pg_backup_stop
        if offered is not None:
            hello['hash_offer'] = offered
        elif self.hash_algorithm != DEFAULT_ALGORITHM:
            hello['hash'] = self.hash_algorithm  # EXP-29: absent means BLAKE2b, so older receivers still match
        if self.compress is not None:
            hello['compress_offer'] = [self.compress]
        if self.subpage:
            hello['subpage_offer'] = SUB_BLOCK
        if self.fast_path:
            hello['summary_offer'] = True
            if self.diverged_basis:
                hello['diverged_offer'] = True  # FORK_POINT may follow ACCEPT
        self._send_json(protocol.HELLO, hello)
        accept = self.inbox.wait(lambda: self.inbox.accept, 'ACCEPT', 'accept')
        if offered is not None:
            chosen = accept.get('hash')
            if chosen not in offered:
                raise ProtocolError(f'ACCEPT hash must be one of the offered {offered}, not {chosen!r}')
            self.hash_algorithm = chosen
            self._start_hashing(key)
        if self.compress is not None:
            # A receiver without zstd accepts without it, and the pages go plain.
            chosen = accept.get('compress')
            if chosen not in (None, self.compress):
                raise ProtocolError(f'ACCEPT compress must be {self.compress!r} or absent, not {chosen!r}')
            self.codec = compression.Codec() if chosen else None
            if self.codec is not None:
                threads = max(1, self.jobs)
                self.compressor = ThreadPoolExecutor(threads, thread_name_prefix='pgdelta-zstd')
                self.compress_counts['threads'] = threads
        elif 'compress' in accept:
            raise ProtocolError('ACCEPT compress without a compress_offer')
        if 'subpage' in accept:
            # A receiver that can't build on B's bytes accepts without it, and runs go as PAGE_DATA.
            if not self.subpage or accept['subpage'] != SUB_BLOCK:
                raise ProtocolError(f'ACCEPT subpage {accept["subpage"]!r} without a matching subpage_offer')
            self.subpage_used = self.inbox.subpage = True
        self.reuse, self.reason, self.policy, self.basis_identity, self.basis_sizes = _parse_accept(
            accept, len(inventory.files))
        self.in_place = accept.get('in_place', False)
        if type(self.in_place) is not bool:
            raise ProtocolError('ACCEPT in_place must be true or false')
        self.mode = 'reuse' if self.reuse else 'full'
        self.max_run = max(1, RUN_BYTES // self.block)
        self.max_request = protocol.max_nodes_per_request(self.node_blocks)
        if 'summary' in accept or 'summary_refused' in accept:
            if not self.fast_path:
                raise ProtocolError('ACCEPT summary without a summary_offer')
            self._decide_fast_path(accept)
        if self.fast is not None:
            self._send_fast(inventory.files)
        else:
            self._send_files(inventory.files, 0)
        if self.live is not None:
            # Every listed file has been sent: end backup mode, then send the WAL it needs and its backup_label.
            self._end_live(len(inventory.files))
        elif self.selection is not None:
            # A server started on the data directory meanwhile: fail before the receiver publishes.
            datadir.check_unchanged(self.source, self.pg_controldata, self.selection)
        self._send_json(protocol.COMPLETE, {'files': self.file_count, 'directories': self.directory_count})
        try:
            final = self.inbox.wait(lambda: self.inbox.complete, 'COMPLETE', 'complete')
        except ChannelClosed as exc:
            raise ChannelClosed(f'{exc}. Every file was sent, so the receiver may have published the output '
                                f'anyway: check its log and the receipt beside the output '
                                f'(<output>{RECEIPT_SUFFIX}), whose files_digest should equal this run\'s '
                                f'statistics') from exc
        self.protocol_seconds = time.perf_counter() - self.protocol_started
        self.inbox.join(JOIN_SECONDS)
        if final.get('published') is not True:
            raise ProtocolError('receiver finished without confirming publication')
        receiver = final.get('receiver')
        self.receiver_stats = receiver if isinstance(receiver, dict) else None
        return self.stats('complete', 0, None)

    def _decide_fast_path(self, accept):
        """--fast-path summaries: the guard (pgdelta.fastpath.guard) on what the receiver proved of B and on the
        server's summaries, then SUMMARY_DECISION. A receiver that proved nothing ("summary_refused") waits for no
        decision, and the session hashes both copies."""
        counts, facts = self.fast_counts, accept.get('summary')
        if facts is None:
            refused = accept.get('summary_refused')
            counts['reasons'] = [refused if isinstance(refused, str) else 'the receiver declined the fast path']
            return
        if not isinstance(facts, dict):
            raise ProtocolError('ACCEPT summary must be an object')
        started = time.perf_counter()
        counts['guard_started_utc'] = time.time()
        source = dict(self.live.summary_source(), identity=self.identity)
        counts['summarizer_wait_seconds'] = source['summarizer_wait_seconds']
        counts['summarized_lsn'] = (None if source['summarized_lsn'] is None
                                    else fastpath.format_lsn(source['summarized_lsn']))
        page = self.fast_path == fastpath.PAGE_LSN
        ok, reasons = fastpath.guard(facts, source, summaries=not page)
        common = None
        fork = fastpath.diverged_fork(facts, source) if not ok and self.diverged_basis and not page else None
        if fork is not None and accept.get('diverged') is True:
            common, ok, reasons = self._basis_wal(facts, source, fork)
        if ok and page:
            # page-lsn: no plan before sending; each main-fork file is scanned on the server as it comes (_send_lsn).
            self.fast, self.lsn_threshold = {}, fastpath.parse_lsn(facts['redo'])
            counts.update(lsn_threshold=facts['redo'], files_scanned=0, pages_scanned=0, scan_seconds=0.0)
        elif ok:
            redo = common['redo'] if common is not None else facts['redo']
            spans = fastpath.ranges(fastpath.history_path(source['history'], source['timeline']),
                                    fastpath.parse_lsn(redo), source['start_lsn'])
            chosen = [(tli, start, end) for tli, start, end in source['summaries']
                      if any(tli == t and start < hi and end > lo for t, lo, hi in spans)]
            # One query, grouped per relation fork on the server (EXP-73b); the same plan as block_plan of every row.
            rows, groups = self.live.summary_blocks(chosen)
            self.fast = fastpath.grouped_plan(groups)
            if common is not None:
                fastpath.merge_basis_blocks(self.fast, common['blocks'])
            counts['summaries'], counts['summary_rows'], counts['relations'] = len(chosen), rows, len(self.fast)
        counts['used'], counts['reasons'] = ok, reasons
        counts['guard_seconds'] = time.perf_counter() - started
        self._send_json(protocol.SUMMARY_DECISION, {'use': ok, 'reasons': reasons})

    def _basis_wal(self, facts, source, fork):
        """--diverged-basis: FORK_POINT, the receiver's BASIS_WAL, and the guard on it: (reply, ok, reasons)."""
        self.inbox.basis_wal_expected = True
        self._send_json(protocol.FORK_POINT, {'timeline': int(facts['timeline']), 'fork': fastpath.format_lsn(fork)})
        reply = self.inbox.wait(lambda: self.inbox.basis_wal, 'BASIS_WAL', 'accept')  # part of the handshake
        counts = self.diverged_counts = {'fork': fastpath.format_lsn(fork)}
        if 'refused' in reply:
            counts['refused'] = reply['refused']
            return None, False, [reply['refused']]
        ok, reasons = fastpath.guard(facts, source, common=reply)
        counts.update({key: reply[key] for key in ('checkpoint', 'redo', 'records', 'wal_log_hints')},
                      relations=len(reply['blocks']), blocks=sum(n for runs in reply['blocks'].values() for _, n in runs),
                      receiver_seconds=reply['seconds'])
        return reply, ok, reasons

    def _send_fast(self, entries):
        """--fast-path summaries: each listed file in order. Where the summaries chose its pages, only those go, read
        now, then FILE_DONE_WRITTEN with the digest of what was sent; any other file goes whole by the usual path.
        With --read-ahead, the chosen runs are read on reader threads ahead of the sending thread (_ReadAhead)."""
        if self.fast_path == fastpath.PAGE_LSN:
            return self._send_lsn(entries)
        blocks_per_segment = (self.identity or {}).get('blocks_per_segment') or (1 << 30) // self.block
        counts = self.fast_counts
        plans = []
        for index, entry in enumerate(entries):
            basis_size = self.basis_sizes.get(index) if self.reuse else None
            unlogged = self.selection is not None and self.selection.source_path(entry.path) != entry.path
            pages, whole = fastpath.pages_to_send(entry.path, entry.size, basis_size, self.fast, self.block,
                                                  blocks_per_segment, whole=unlogged)
            plans.append((index, entry, basis_size, pages, whole))
        if self.selector is not None:
            self._select(plans)
        if self.fill_gaps:
            for i, (index, entry, basis_size, pages, whole) in enumerate(plans):
                if not whole:
                    filled = fastpath.fill_gaps(pages, self.fill_gaps)
                    self.pages_filled += len(filled) - len(pages)
                    plans[i] = (index, entry, basis_size, filled, whole)
        if self.plan_out is not None:
            _write_plan(self.plan_out, self.block, plans, self.max_run)
        if self.read_ahead:
            runs = [(index, entry, first, count, min(count * self.block, entry.size - first * self.block))
                    for index, entry, _, pages, whole in plans if not whole
                    for first, count in matching.runs(pages, self.max_run)]
            self.ahead = _ReadAhead(self.reader, runs, self.read_ahead, READ_AHEAD_BYTES, self.block)
        counts['sending_started_utc'] = time.time()
        try:
            for plan in plans:
                self._send_fast_file(*plan)
        finally:
            if self.ahead is not None:
                self.ahead.close()
        counts['sending_ended_utc'] = time.time()

    def _send_lsn(self, entries):
        """--fast-path page-lsn: each listed file in order, as _send_fast sends it, its pages chosen by a scan of the
        server's copy (pgdelta.fastpath.lsn_pages). A scanner thread runs up to fastpath.LSN_LOOKAHEAD files ahead
        of the sending thread, so the chosen pages are read again from the page cache; --read-ahead is not used."""
        counts, work, stop = self.fast_counts, queue.Queue(maxsize=fastpath.LSN_LOOKAHEAD), threading.Event()

        def scan():
            try:
                for index, entry in enumerate(entries):
                    if stop.is_set():
                        return
                    basis_size = self.basis_sizes.get(index) if self.reuse else None
                    unlogged = self.selection is not None and self.selection.source_path(entry.path) != entry.path

                    def read_pages(entry=entry):
                        started, handle = time.perf_counter(), self.reader.open(entry)
                        try:
                            return fastpath.lsn_scan(handle.read, entry.size, self.block, self.lsn_threshold)
                        finally:
                            handle.close()
                            counts['files_scanned'] += 1
                            counts['pages_scanned'] += entry.size // self.block
                            counts['scan_seconds'] += time.perf_counter() - started

                    pages, whole = fastpath.lsn_pages(entry.path, entry.size, basis_size, read_pages, self.block,
                                                      whole=unlogged)
                    work.put((index, entry, basis_size, pages, whole))
                work.put(None)
            except BaseException as exc:  # handed to the sending thread
                work.put(exc)

        thread = threading.Thread(target=scan, name='pgdelta-lsn-scan', daemon=True)
        counts['sending_started_utc'] = time.time()
        thread.start()
        try:
            while (item := work.get()) is not None:
                if isinstance(item, BaseException):
                    if isinstance(item, OSError):
                        raise TransferError(f'cannot read a source file to choose its pages: {item}') from item
                    raise item
                index, entry, basis_size, pages, whole = item
                if self.fill_gaps and not whole:
                    filled = fastpath.fill_gaps(pages, self.fill_gaps)
                    self.pages_filled += len(filled) - len(pages)
                    pages = filled
                self._send_fast_file(index, entry, basis_size, pages, whole)
        finally:
            stop.set()
            while thread.is_alive():  # let a blocked put return
                try:
                    work.get(timeout=0.1)
                except queue.Empty:
                    pass
            thread.join()
        counts['sending_ended_utc'] = time.time()

    def _select(self, plans):
        """--selector (pgdelta.selector, EXP-74): each fill's time, predicted from the unfilled plan, the guard's time
        and the disks' limits; the fastest sets the fill. pg_basebackup's predicted time is recorded beside them, and
        whether it is faster: a recommendation, since pgdelta's own full copy can't beat its fill 0."""
        started = time.perf_counter()
        options, fill = selector.choose([pages for *_, pages, whole in plans if not whole],
                                        sum(entry.size for _, entry, _, _, whole in plans if whole),
                                        sum(entry.size for _, entry, *_ in plans), self.fast_counts['guard_seconds'],
                                        self.block, self.max_run, self.selector)
        self.fill_gaps = fill
        self.selector_counts = {'choice': f'fill{fill}',
                                'predicted_seconds': {name: round(s, 2) for name, s in options.items()},
                                'full_copy_predicted_faster': options[selector.FULL] < options[f'fill{fill}'],
                                'seconds': round(time.perf_counter() - started, 6),
                                'disks': asdict(self.selector)}

    def _send_fast_file(self, index, entry, basis_size, pages, whole):
        """One listed file in fast mode (_send_fast): whole by the usual path, or its chosen pages and FILE_DONE_WRITTEN."""
        counts = self.fast_counts
        self.inbox.check()
        if whole:
            counts['files_whole'] += 1
            self._finish(self._whole_job(index, entry))
            return
        counts['files_chosen'] += 1
        counts['pages_chosen'] += len(pages)
        sent, leaves = 0, []
        started = time.perf_counter()
        try:
            handle = None if self.ahead is not None else self.reader.open(entry)
        except OSError as exc:
            raise TransferError(f'cannot read source file {entry.path}: {exc}') from exc
        try:
            for first, count in matching.runs(pages, self.max_run):
                offset = first * self.block
                length = min(count * self.block, entry.size - offset)
                try:
                    data = (self.ahead.next(index, first) if self.ahead is not None
                            else handle.read(offset, length))
                except OSError as exc:
                    raise TransferError(f'cannot read source file {entry.path}: {exc}') from exc
                view = memoryview(data)
                leaves += [(page, self.hasher.leaf(view[(page - first) * self.block:(page - first + 1) * self.block]))
                           for page in range(first, first + count)]
                self.inbox.check()
                self._send_run(index, first, data)
                sent += length
            self._drain_runs()
            if self.ahead is not None:
                self.ahead.done(index)
        finally:
            if handle is not None:
                handle.close()
        self.hash_source += time.perf_counter() - started
        digest = fastpath.written_digest(self.hasher, entry.size, leaves)
        self._send(protocol.FILE_DONE_WRITTEN, protocol.encode_file_done(index, entry.size, digest))
        self.files_digest.add(entry.path, entry.size, digest)
        total = -(-entry.size // self.block)
        self.files['total'] += 1
        if basis_size != entry.size:
            self.files['size_mismatch'] += 1
        self.files['unchanged' if not pages and basis_size == entry.size else 'changed'] += 1
        self.pages['total'] += total
        self.pages['sent'] += len(pages)
        self.pages['reused'] += total - len(pages)
        self.pages['sent_bytes'] += sent

    def _whole_job(self, index, entry):
        """A listed file sent whole in fast mode: hashed as read (pg_control pinned), every page sent, its digest
        that of the whole file; B's copy of it is never read."""
        started = time.perf_counter()
        try:
            tree = self.reader.hash(entry)
        except OSError as exc:
            raise TransferError(f'cannot read source file {entry.path}: {exc}') from exc
        self.hash_source += time.perf_counter() - started
        if tree.size != entry.size:
            raise VerificationError(f'source file {entry.path} changed size during the run ({entry.size} -> '
                                    f'{tree.size} bytes)')
        job = _Job(index, entry.path, tree, None, entry=entry)
        job.plan.whole = list(range(tree.node_count))
        return job

    def _start_hashing(self, key):
        """The session's hasher and hash pool, and a live source's reader, for self.hash_algorithm."""
        try:
            self.hasher = Hasher(key, self.hash_algorithm)
        except (ImportError, ValueError) as exc:
            raise InvalidInput(str(exc)) from exc
        started = time.perf_counter()
        try:
            self.pool = HashPool(self.jobs, backend=self.hash_backend, key=key, algorithm=self.hash_algorithm)
        except (OSError, ValueError) as exc:
            raise TransferError(f'cannot start the hashing workers: {exc}') from exc
        self.pool_start_seconds = time.perf_counter() - started
        if self.live is not None:
            # Read before start (test switch): the data files come from the copy made before pg_backup_start.
            self.reader = live.Reader(self.live.snapshot or self.source, self.source, self.hasher, self.block,
                                      self.node_blocks, self.pool)

    def _send_files(self, entries, first_index):
        """Hash, compare and send `entries` (file indexes from first_index on), up to `window` files ahead."""
        pending = deque()
        for index, entry in enumerate(entries, first_index):
            self.inbox.check()
            pending.append(self._plan(index, entry))
            while len(pending) > self.window:
                self._finish(pending.popleft())
        while pending:
            self._finish(pending.popleft())

    def _begin_live(self):
        """Open the backup session and its slot, call pg_backup_start, then list the data directory.

        From the slot's creation until it is dropped, the stall watch gives up on a receiver that stops
        (StallWatch), and a TCP connection to it gets keepalives.

        EXP-25's broken variants (PGDELTA_TEST_READ_BEFORE_START, pgdelta.live): the data directory is listed
        and copied aside before pg_backup_start is called ("before_start"), or once it is sent and before
        its answer is read ("during_start").
        """
        self.live = live.LiveSource(self.source, self.pg_conn, self.pg_controldata, psql=self.psql,
                                    checkpoint=self.checkpoint)
        self.watch = StallWatch(self.channel, self.inbox, self.live_timeout, on_give_up=self._kill_session)
        self.live.timeout = self.watch.seconds
        sock = getattr(self.channel, 'sock', None)
        if sock is not None:
            self.live.keepalive = transport.keep_alive(sock, self.watch.seconds)
        self.watch.start()
        self.live.open()
        if self.live.variant == 'before_start':
            self.selection = self.live.select()
            self.live.pre_read(self.selection)
            self.live.start()
        elif self.live.variant == 'during_start':
            def read_meanwhile():
                self.selection = self.live.select()
                self.live.pre_read(self.selection)

            self.live.start(meanwhile=read_meanwhile)
        else:
            self.live.start()
            self.selection = self.live.select()
        if self.live.oracle_dir:
            self.live.oracle_read(self.selection)
        if self.identity is None:
            self.identity = self.selection.identity

    def _end_live(self, first_index):
        """pg_backup_stop, then the WAL segments, history files and backup_label, appended; then drop the slot."""
        entries = self.live.stop()
        for entry in entries:
            if entry.path == live.BACKUP_LABEL:
                self.reader.pin(entry.path, self.live.backup_label)
            else:
                self.reader.hold(entry.path, self.live.segment_check(entry.path))
        self.appended = entries
        self.file_count += len(entries)
        self._send_json(protocol.APPEND, {'files': [[entry.path, entry.size, entry.mode] for entry in entries]})
        self._send_files(entries, first_index)
        self.live.finish()
        self.watch.stop()  # the slot is dropped: waiting on the receiver from here on holds nothing on the source

    def _source_file(self, path):
        """The file read for the listed `path` (datadir: an unlogged relation's main fork is its init fork)."""
        return self.source / (path if self.selection is None else self.selection.source_path(path))

    def _plan(self, index, entry):
        started = time.perf_counter()
        try:
            if self.reader is not None:  # live: always entry.size bytes (pgdelta.live.Reader)
                tree = self.reader.hash(entry)
            else:
                tree = hash_file(self._source_file(entry.path), None, self.block, self.node_blocks,
                                 hasher=self.hasher, pool=self.pool)
        except OSError as exc:
            raise TransferError(f'cannot read source file {entry.path}: {exc}') from exc
        self.hash_source += time.perf_counter() - started
        if tree.size != entry.size:
            raise VerificationError(f'source file {entry.path} changed size during the run '
                                    f'({entry.size} -> {tree.size} bytes); S must be immutable')
        job = _Job(index, entry.path, tree, self.basis_sizes.get(index), entry=entry)
        if job.basis_size is None or not self.reuse:
            job.plan.whole = list(range(tree.node_count))
            return job
        size, nodes = self.inbox.wait(lambda: self.inbox.basis.pop(index, None),
                                      f'basis digests of {entry.path}', 'basis')
        if size != job.basis_size or len(nodes) != node_count(size, self.block, self.node_blocks) * DIGEST_SIZE:
            raise ProtocolError(f'BASIS_NODES for {entry.path} disagrees with the announced basis size')
        job.plan = matching.classify_nodes(tree, size, nodes, self.high_churn)
        for start in range(0, len(job.plan.descend), self.max_request):
            chunk = job.plan.descend[start:start + self.max_request]
            self._send(protocol.LEAF_REQUEST, protocol.encode_leaf_request(index, chunk))
            self.leaf_requests += 1
        return job

    def _finish(self, job):
        tree, plan = job.tree, job.plan
        pages, blocks = [], {}
        for node in plan.descend:
            blob = self.inbox.wait(lambda: self.inbox.leaves.pop((job.index, node), None),
                                   f'leaf digests of {job.path}', 'leaf')
            expected = matching.basis_leaf_count(node, job.basis_size, self.block, self.node_blocks)
            if len(blob) != expected * DIGEST_SIZE:
                raise ProtocolError(f'LEAF_DIGESTS for {job.path} node {node} has the wrong length')
            pages += matching.changed_pages(tree, node, job.basis_size, blob)
            if self.subpage_used:
                blocks[node] = self._block_digests(job, node, expected)
        for node in plan.whole:
            pages += matching.node_pages(tree, node)
        pages.sort()
        old = self._old_blocks(job, blocks) if self.subpage_used else None
        if self.reader is not None:
            sent_bytes = self._send_live_pages(job, pages, old) if pages else 0
        else:
            sent_bytes = self._send_pages(job, pages, old) if pages else 0
        digest = job.digest or tree.digest
        self._send(protocol.FILE_DONE, protocol.encode_file_done(job.index, tree.size, digest))
        self.files_digest.add(job.path, tree.size, digest)
        self._count(job, len(pages), sent_bytes)

    def _block_digests(self, job, node, basis_pages):
        """B's block digests for the pages of `node` (--subpage), checked against the pages B has there."""
        blob = self.inbox.wait(lambda: self.inbox.blocks.pop((job.index, node), None),
                               f'block digests of {job.path}', 'leaf')
        first = node * self.node_blocks
        expected = sum(subpage.digest_bytes(span_length(page, self.block, job.basis_size))
                       for page in range(first, first + basis_pages))
        if len(blob) != expected:
            raise ProtocolError(f'BLOCK_DIGESTS for {job.path} node {node} has the wrong length')
        self.subpage_counts['block_digest_bytes'] += len(blob)
        return blob

    def _old_blocks(self, job, blocks):
        """page -> B's block digests of that page, or None where B has no full page there (it goes whole)."""
        def old(page):
            node, at = divmod(page, self.node_blocks)
            blob = blocks.get(node)
            if blob is None or self.block != subpage.PAGE or (page + 1) * self.block > job.basis_size:
                return None
            return blob[at * subpage.DIGESTS_PER_PAGE:(at + 1) * subpage.DIGESTS_PER_PAGE]
        return old

    def _send_pages(self, job, pages, old=None):
        sent = 0
        try:
            fd = open_regular(self._source_file(job.path))
        except OSError as exc:
            raise TransferError(f'cannot read source file {job.path}: {exc}') from exc
        try:
            for first, count in matching.runs(pages, self.max_run):
                offset = first * self.block
                length = min(count * self.block, job.tree.size - offset)
                try:
                    data = pread_exact(fd, length, offset)
                except OSError as exc:
                    raise TransferError(f'cannot read source file {job.path}: {exc}') from exc
                self.inbox.check()
                self._send_run(job.index, first, data, old)
                sent += length
            self._drain_runs()
        finally:
            os.close(fd)
        return sent

    def _send_live_pages(self, job, pages, old=None):
        """Live: send the pages as read now, each hashed again, and keep the digest of what was sent in job.digest.

        A page may have changed since the file was hashed; the copy is made of
        these bytes, so FILE_DONE carries the digest of the file with them in
        place (pgdelta.live.composite_digest), and the receiver verifies that.
        A file held strictly (the WAL and history files) must not change at
        all: a page of it that differs fails the run.
        """
        sent, changed, tree, block = 0, {}, job.tree, self.block
        strict = job.path in self.reader.strict
        try:
            handle = self.reader.open(job.entry)
        except OSError as exc:
            raise TransferError(f'cannot read source file {job.path}: {exc}') from exc
        try:
            for first, count in matching.runs(pages, self.max_run):
                offset = first * block
                length = min(count * block, tree.size - offset)
                try:
                    data = handle.read(offset, length)
                except OSError as exc:
                    raise TransferError(f'cannot read source file {job.path}: {exc}') from exc
                view = memoryview(data)
                for page in range(first, first + count):
                    leaf = self.hasher.leaf(view[(page - first) * block:(page - first + 1) * block])
                    if leaf != tree.leaf(page):
                        if strict:
                            raise VerificationError(f'{job.path} changed after it was hashed (page {page}): WAL '
                                                    f'must not change once pg_backup_stop has returned')
                        changed[page] = leaf
                self.inbox.check()
                self._send_run(job.index, first, data, old)
                sent += length
            self._drain_runs()
        finally:
            handle.close()
        if changed:
            self.reader.counts['pages_changed_after_hash'] += len(changed)
            job.digest = live.composite_digest(tree, self.hasher, changed)
        return sent

    def _send_run(self, index, first, data, old=None):
        """One run of pages: PAGE_DATA at once, or, with compression accepted, queued for the compressing pool.
        With --subpage accepted, the run goes as PAGE_DELTA: each page's changed blocks against `old`."""
        head, plain, packed = protocol.page_data_head(index, first), protocol.PAGE_DATA, protocol.PAGE_DATA_ZSTD
        if self.subpage_used:
            data, plain, packed = self._delta(first, data, old), protocol.PAGE_DELTA, protocol.PAGE_DELTA_ZSTD
        if self.codec is None:
            self._send(plain, head, data)
            return
        self.queued.append((head, data, plain, packed))
        self.queued_bytes += len(data)
        if self.queued_bytes >= compression.TASK_BYTES:
            self._submit_queued()

    def _submit_queued(self):
        """The queued runs as one task for the compressing pool (EXP-50); at most two tasks per thread wait."""
        if not self.queued:
            return
        self.compressing.append((self.queued, self.compressor.submit(self._compress_task, self.queued)))
        self.queued, self.queued_bytes = [], 0
        while len(self.compressing) > 2 * self.compress_counts['threads']:
            self._send_compressed()

    def _delta(self, first, data, old):
        """A run's PAGE_DELTA body (pgdelta.subpage): every page against B's block digests where B has it."""
        started, parts, view = time.perf_counter(), [], memoryview(data)
        for at in range(0, len(data), self.block):
            page = first + at // self.block
            parts += subpage.encode_page(view[at:at + self.block], old(page) if old else None, self.hasher,
                                         self.subpage_counts)
        self.subpage_counts['hash_seconds'] += time.perf_counter() - started
        return b''.join(parts)

    def _compress_task(self, runs):
        started = time.perf_counter()
        packed = [self.codec.compress(data) for _, data, _, _ in runs]
        return packed, time.perf_counter() - started

    def _drain_runs(self):
        """Send every queued run, in order: a file's pages all precede its FILE_DONE."""
        self._submit_queued()
        while self.compressing:
            self._send_compressed()

    def _send_compressed(self):
        """The oldest task's runs, in order: each PAGE_DATA_ZSTD if it shrank, else PAGE_DATA."""
        runs, future = self.compressing.popleft()
        counts = self.compress_counts
        started = time.perf_counter()
        packed, seconds = future.result()
        counts['wait_seconds'] += time.perf_counter() - started
        counts['seconds'] += seconds
        counts['tasks'] += 1
        for (head, data, plain, packed_code), frame in zip(runs, packed):
            counts['runs'] += 1
            counts['raw_bytes'] += len(data)
            if len(frame) < len(data):
                counts['runs_compressed'] += 1
                counts['wire_bytes'] += len(frame)
                self._send(packed_code, head, frame)
            else:
                counts['wire_bytes'] += len(data)
                self._send(plain, head, data)

    def _send(self, code, *parts):
        """Send one frame, counting the time send() blocked: page_send_s for page data, other_send_s else."""
        started = time.perf_counter()
        self.channel.send(code, *parts)
        self.waits.sent(code in (protocol.PAGE_DATA, protocol.PAGE_DATA_ZSTD), time.perf_counter() - started)

    def _send_json(self, code, value):
        started = time.perf_counter()
        self.channel.send_json(code, value)
        self.waits.sent(False, time.perf_counter() - started)

    def _count(self, job, pages_sent, sent_bytes):
        tree, plan, files = job.tree, job.plan, self.files
        files['total'] += 1
        if job.basis_size is None:
            files['new'] += 1
        else:
            if job.basis_size != tree.size:
                files['size_mismatch'] += 1
            unchanged = self.reuse and pages_sent == 0 and job.basis_size == tree.size
            files['unchanged' if unchanged else 'changed'] += 1
        self.pages['total'] += tree.page_count
        self.pages['sent'] += pages_sent
        self.pages['reused'] += tree.page_count - pages_sent
        self.pages['sent_bytes'] += sent_bytes
        self.nodes['total'] += tree.node_count
        self.nodes['matched'] += len(plan.matched)
        self.nodes['descended'] += len(plan.descend)
        self.nodes['sent_whole'] += len(plan.whole)
        self.high_churn_files += plan.high_churn

    def _live_summary(self):
        if self.live is None:
            return None
        summary = self.live.summary()
        summary['appended_files'] = len(self.appended)
        summary.update(self.reader.counts if self.reader is not None else {})
        return summary

    def switches(self):
        """The sender's engine settings in effect."""
        return {'jobs': self.jobs,
                'hash_backend': self.pool.backend if self.pool is not None else None}

    def stats(self, status, exit_code, error):
        receiver = self.receiver_stats or {}
        now = time.perf_counter()
        protocol_seconds = self.protocol_seconds
        if protocol_seconds is None and self.protocol_started is not None:
            protocol_seconds = now - self.protocol_started
        timings = {
            'hash_source': self.hash_source,
            'hash_basis': receiver.get('hash_basis_seconds'),
            'protocol': protocol_seconds,
            'rebuild': receiver.get('rebuild_seconds'),  # writing O
            'copy_basis': receiver.get('copy_seconds'),  # of which: copying from B
            # of which: fsync of O's files. With --fsync worker it runs on a background thread alongside
            # rebuild instead (and may exceed it); rebuild then holds the receiver's fsync_wait_seconds.
            'fsync': receiver.get('fsync_seconds'),
            'verify': receiver.get('verify_seconds'),  # re-hashing O (overlaps writing it when pipelined)
            'verify_wait': receiver.get('verify_wait_seconds'),  # of which the receiver's main loop waited
            'pool_start': self.pool_start_seconds,
            # until the first, and until every one, of the workers had started (null until then)
            'pool_first_ready': None if self.pool is None else self.pool.first_ready_seconds,
            'pool_ready': None if self.pool is None else self.pool.ready_seconds,
            'receiver_pool_first_ready': receiver.get('pool_first_ready_seconds'),
            'receiver_pool_ready': receiver.get('pool_ready_seconds'),
            'total': now - self.started,
        }
        return {
            'schema_version': STATS_SCHEMA_VERSION,
            'tool': 'pgdelta',
            'tool_version': __version__,
            'protocol_version': protocol.PROTOCOL_VERSION,
            'status': status,
            'exit_code': exit_code,
            'error': error,
            'mode': self.mode,
            'gate_reason': self.reason,
            'reuse_policy': self.policy,
            'block': self.block,
            'node_blocks': self.node_blocks,
            'high_churn_node_fraction': self.high_churn,
            'window': self.window,
            'jobs': self.jobs,
            'hash_backend': self.switches()['hash_backend'],
            'hash': self.hash_algorithm,  # the keyed digest both ends used (EXP-29: blake2b or blake3)
            # "spawn": the workers are this process's children, so wait4/RUSAGE_CHILDREN count them.
            'worker_start_method': None if self.pool is None else self.pool.start_method,
            # Both ends' engine settings; the receiver's are null until its final statistics arrive.
            'switches': {'sender': self.switches(), 'receiver': receiver.get('switches')},
            'source_identity': self.identity,
            'basis_identity': self.basis_identity,
            # A backup (S), a stopped data directory read directly, or a running one read in backup mode, and
            # what was left out of it.
            'source_kind': self.source_kind,
            'source_excluded': None if self.selection is None else self.selection.excluded_summary(),
            # A running source (pgdelta.live): its backup's LSNs and times, the WAL sent, how its files changed
            # while they were read, and how backup mode ended; null for the other kinds.
            'live': self._live_summary(),
            'in_place': self.in_place,
            'files': dict(self.files),
            'directories': self.directory_count,
            'files_digest': (self.files_digest.hexdigest()
                             if self.file_count is not None and self.files_digest.count == self.file_count
                             else None),
            'pages': dict(self.pages),
            'nodes': dict(self.nodes),
            'high_churn_files': self.high_churn_files,
            # LEAF_REQUEST frames sent: the leaf round trips a window may hide (EXP-13).
            'leaf_requests': self.leaf_requests,
            # EXP-13: the connect, the waits for the receiver (each split into busy and idle by the send
            # queue), and the time blocked sending (pgdelta.waits). Like timings, they vary from run to run.
            'protocol_timers': self.waits.summary(getattr(self.channel, 'connect_seconds', None)),
            'bytes_source_to_dest': self.channel.sent.summary(),
            'bytes_dest_to_source': self.channel.received.summary(),
            'timings_seconds': {key: None if value is None else round(value, 6)
                                for key, value in timings.items()},
            'receiver': self.receiver_stats,
            # --compress: what was asked, what the receiver accepted, and what it saved; absent without it.
            **({'compress': {'requested': self.compress, 'used': None if self.codec is None else self.compress,
                             'backend': None if self.codec is None else self.codec.backend,
                             **self.compress_counts}} if self.compress is not None else {}),
            **({'subpage': {'requested': True, 'used': self.subpage_used,
                            **{k: round(v, 6) if isinstance(v, float) else v for k, v in self.subpage_counts.items()}}}
               if self.subpage else {}),
            # --fast-path: whether the guard let it run and why not, and what the summaries chose; absent without it.
            **({'fast_path': {'requested': self.fast_path,
                              **{k: round(v, 6) if isinstance(v, float) else v for k, v in self.fast_counts.items()},
                              'read_ahead': {'threads': self.read_ahead, 'window_bytes': READ_AHEAD_BYTES,
                                             'reads': None if self.ahead is None else self.ahead.reads,
                                             'wait_seconds': None if self.ahead is None
                                             else round(self.ahead.wait_seconds, 6)},
                              # --fill-gaps: pages_chosen counts the pages filled too; absent without it.
                              **({'fill_gaps': {'gap_pages': self.fill_gaps, 'pages_filled': self.pages_filled}}
                                 if self.fill_gaps else {}),
                              # --diverged-basis: offered; for a B past the fork, what its own WAL added.
                              **({'diverged_basis': self.diverged_counts or {}} if self.diverged_basis else {}),
                              # --selector: what it predicted and chose, where the fast path ran; absent without it.
                              **({'selector': self.selector_counts} if self.selector is not None else {})}}
               if self.fast_path else {}),
        }


def _write_plan(path, block, plans, max_run):
    """--plan-out (EXP-68): the fast path's plan as JSON, for modelling its I/O. Per listed file, in sending order:
    its path and size, whether it goes whole, and otherwise its chosen pages as the [first, count] runs sent."""
    files = [{'path': str(entry.path), 'size': entry.size, 'whole': whole,
              'runs': [] if whole else [[first, count] for first, count in matching.runs(pages, max_run)]}
             for _, entry, _, pages, whole in plans]
    try:
        Path(path).write_text(json.dumps({'block': block, 'files': files}, separators=(',', ':')))
    except OSError as exc:
        raise InvalidInput(f'cannot write the plan to {path}: {exc}') from exc


def run_sender(channel, source, *, identity=None, block=None, node_blocks=NODE_BLOCKS,
               high_churn_node_fraction=None, window=DEFAULT_WINDOW, key=None, jobs=DEFAULT_JOBS,
               hash_backend=DEFAULT_BACKEND, source_kind=datadir.DEFAULT_SOURCE_KIND, pg_controldata=None,
               busy_inflight_bytes=None, pg_conn=None, psql=None, live_timeout=None,
               hash_algorithm=DEFAULT_ALGORITHM, compress=None, checkpoint=live.DEFAULT_CHECKPOINT, subpage=False,
               fast_path=None, read_ahead=0, plan_out=None, fill_gaps=0, diverged_basis=False, selector=None):
    """Run one session as the sender and return the statistics dict.

    On failure the raised PgDeltaError carries partial statistics in
    `pgdelta_stats`. `block=None` takes the block size from the source
    identity, or 8192 when the identity is unknown. `jobs` worker processes
    (threads with hash_backend="thread") hash S; jobs=1 hashes in this
    thread. The digests, and so everything sent, are the same for any value.
    With jobs > 1, a program calling this must guard its main module
    (`if __name__ == '__main__':`), as multiprocessing's spawn requires.
    `source_kind` "datadir" reads a cleanly stopped data directory directly
    (pgdelta.datadir) and needs `pg_controldata`; its identity is then read
    from it unless `identity` is given. `source_kind` "live" reads a running
    cluster's data directory in backup mode (pgdelta.live): it needs
    `pg_controldata` and `pg_conn`, a psql connection string to that
    server, and runs `psql` (default: beside pg_controldata, else on PATH).
    While its replication slot is held, a receiver that makes no progress
    for `live_timeout` seconds (default pgdelta.live.DEFAULT_TIMEOUT) fails
    the run (StallWatch). A run with one of EXP-25's broken test variants
    (PGDELTA_TEST_READ_BEFORE_START) raises VerificationError (exit 4) even
    when the receiver published, its statistics attached as on failure.
    `window` files are hashed ahead of the one being sent (at least 1): it
    changes the order of the frames on the wire, never what they hold.
    `busy_inflight_bytes` (EXP-13): over TCP on Linux, a wait counts as busy
    while bytes are unsent, or while more than this many are in flight;
    without it, only unsent bytes do (pgdelta.waits). Only the statistics
    change. `compress` "zstd" offers zstd for the pages sent (pgdelta.compress);
    a receiver that can't use it accepts without, and the pages go plain.
    `checkpoint` ("fast" or "spread") is pg_backup_start's for a live source.
    `subpage` offers sub-page deltas (pgdelta.subpage): with them accepted, each page sent carries only its
    512-byte blocks that differ from B's.
    `fast_path` "summaries" (pgdelta.fastpath, a live source only) offers to send only the blocks the server's WAL
    summaries list since B's redo point, without reading B; the receiver proves B's lineage, the guard here
    decides, and otherwise the session is as without it. `read_ahead` threads read the fast path's chosen pages ahead
    of the sending thread (0: one run at a time in it). `plan_out`, a path, receives the fast path's plan as JSON
    where it runs (_write_plan). `fill_gaps` pages: clean gaps of at most that many pages between the fast path's
    chosen pages are sent too (fastpath.fill_gaps), so fewer, larger reads and writes carry them.
    `diverged_basis` extends the fast path to a B that kept writing after the server's timeline left its own (an old
    primary after a failover): the blocks B's own WAL names since the checkpoint before the fork are sent too.
    `selector`, a pgdelta.selector.Disks, has the fast path choose its fill once the guard has run: the fastest by
    EXP-74's model of the plan on those disks (the fill given is then replaced). The statistics also say whether
    pg_basebackup is predicted faster.
    """
    return _Sender(channel, source, identity, block, node_blocks, high_churn_node_fraction,
                   window, key, jobs, hash_backend, source_kind, pg_controldata, busy_inflight_bytes,
                   pg_conn, psql, live_timeout, hash_algorithm, compress, checkpoint, subpage, fast_path,
                   read_ahead, plan_out, fill_gaps, diverged_basis, selector).run()
