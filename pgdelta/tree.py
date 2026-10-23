"""Per-file hash tree: page leaves under fixed-size nodes under one file digest.

Every node (1 MiB by default) is one unit of work: positional reads of its
byte range, at most READ_BYTES at a time, then its leaf digests and its
node digest. The tree is the same however the nodes are spread out.

A HashPool decides where node work runs:
- jobs 1 ("serial"): in the calling thread, one node after another.
- "process" (the default for jobs > 1): `jobs` worker processes, each with
  its own interpreter and so its own interpreter lock. A task names a file
  by path and a run of nodes; the worker opens the file, checks it is the
  same file the caller opened (device and inode), and returns each node's
  digest and leaf digests. The session key reaches the workers once,
  through the pool initializer. Workers are started with "spawn" on every
  platform, so each one is a direct child of the process that owns the
  pool, which reaps it when the pool closes: its CPU time and peak RSS
  reach wait4(2), getrusage(RUSAGE_CHILDREN) and time(1) of whoever ran
  pgdelta. ("forkserver", for tests only, makes them children of the fork
  server, which nobody waits for, so their CPU is missing from all three;
  "fork" is never used, as it is unsafe once threads run.) They ignore
  SIGINT (Ctrl-C reaches the whole process group; the parent decides) and
  exit on their own if the parent dies. Each worker reports its pid on a
  pipe when it has started, so the pool knows when the first and when the
  last of them was ready.
- "thread": `jobs` worker threads sharing the caller's file descriptor,
  one node per task (EXP-05). Python's interpreter lock makes this slower
  than one thread beyond about 3 threads; it is kept for comparison.

Each caller of a pool keeps at most a bounded number of its tasks queued,
so two callers at once (the receiver hashes B while it verifies O) share
the workers. Every task a caller started has finished when its call
returns or raises, even if the pool is closed from another thread
meanwhile. Read buffers are bounded by READ_BYTES whatever the node size.
Leaves are kept as one concatenated bytes object (16 bytes per page, so at
most 2 MiB for a 1 GiB relation segment); node digests likewise.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import (BrokenExecutor, CancelledError, ProcessPoolExecutor, ThreadPoolExecutor,
                                wait)
from dataclasses import dataclass
import errno
import functools
import multiprocessing
import multiprocessing.connection
import os
import signal
import stat
import struct
import threading
import time

from pgdelta.errors import TransferError
from pgdelta.hashing import BLOCK, DIGEST_SIZE, NODE_BLOCKS, Hasher

READ_BYTES = 1 << 20
# Process backend: one task covers about this many bytes of consecutive nodes,
# so the cost of passing a task and its result is small next to the hashing.
TASK_BYTES = 8 << 20
DEFAULT_JOBS = 1
BACKENDS = ('process', 'thread')
DEFAULT_BACKEND = 'process'
# Testing hook: "forkserver" instead of the default "spawn". With it the workers' CPU is not
# counted by wait4/RUSAGE_CHILDREN of whoever runs pgdelta (see start_method).
START_METHOD_ENV = 'PGDELTA_START_METHOD'
DEFAULT_START_METHOD = 'spawn'
_SAFE_START_METHODS = ('spawn', 'forkserver')
# How often the pool's start-up watcher checks whether the pool was closed meanwhile.
_READY_POLL_SECONDS = 0.05
_READY_MESSAGE = struct.Struct('!q')  # a worker's pid, sent once it has started
_PREADV = hasattr(os, 'preadv')
_local = threading.local()


class _SizeChanged(Exception):
    """The file ended before, or continued past, the length it had when it was opened."""


class HashingStopped(TransferError):
    """A pool call stopped early because the run is ending."""


def start_method():
    """The multiprocessing start method for worker processes: "spawn" everywhere, never "fork".

    spawn makes each worker a child of the pgdelta process, which joins it
    when the pool closes, so the worker's CPU time and peak RSS reach
    wait4(2), getrusage(RUSAGE_CHILDREN) and time(1) of whoever launched
    pgdelta. Linux used to get "forkserver" here (and is Python 3.14's own
    default there): its workers are children of the fork server, which no
    one waits for, so 85-90% of pgdelta's CPU at --jobs 4 was missing from
    those figures (EXP-06 review). PGDELTA_START_METHOD=forkserver still
    selects it, for tests.
    """
    available = multiprocessing.get_all_start_methods()
    override = os.environ.get(START_METHOD_ENV)
    if override:
        if override not in _SAFE_START_METHODS or override not in available:
            raise ValueError(f'{START_METHOD_ENV}={override!r}: use one of '
                             f'{[method for method in _SAFE_START_METHODS if method in available]}')
        return override
    return DEFAULT_START_METHOD


# --- worker processes -------------------------------------------------------------------------------

class _WorkerState:
    hasher = None


_WORKER = _WorkerState()


def _worker_init(key, ready=None):
    # Ctrl-C reaches every process in the foreground group; only the parent acts on it, and it
    # shuts the pool down. SIGTERM keeps its default: a worker told to stop, stops.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _WORKER.hasher = Hasher(key)
    parent = multiprocessing.parent_process()
    if parent is not None:
        threading.Thread(target=_exit_with_parent, args=(parent.sentinel,), name='pgdelta-parent-watch',
                         daemon=True).start()
    if ready is not None:
        # Started: tell the pool. One 12-byte write to a pipe is atomic, so workers can share it.
        try:
            ready.send_bytes(_READY_MESSAGE.pack(os.getpid()))
        except OSError:
            pass  # the pool is already closing
        finally:
            ready.close()


def _exit_with_parent(sentinel):
    """Exit as soon as the parent is gone, even if it was killed without shutting the pool down."""
    multiprocessing.connection.wait([sentinel])
    os._exit(1)


def _worker_ping():
    return os.getpid()


def _worker_hash_nodes(path, identity, size, block, node_blocks, keep_leaves, nodes):
    """[(node digest, leaf digests or b'')] for `nodes` of the file at `path`, in a worker process."""
    fd = open_regular(path)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != identity:
            raise OSError(errno.ESTALE, 'the file was replaced while it was hashed', path)
        return [_hash_node(fd, size, _WORKER.hasher, block, node_blocks, keep_leaves, node) for node in nodes]
    finally:
        os.close(fd)


# --- the pool ---------------------------------------------------------------------------------------

class HashPool:
    """Runs node hashing in the calling thread (jobs 1), or on `jobs` worker processes or threads.

    `key` (the session's 32-byte hash key) is required for the process
    backend: workers get it once, through the pool initializer.
    """

    def __init__(self, jobs=1, *, backend=DEFAULT_BACKEND, key=None):
        if type(jobs) is not int or jobs < 1:
            raise ValueError(f'jobs must be a positive integer, not {jobs!r}')
        if backend not in BACKENDS:
            raise ValueError(f'hash backend must be one of {BACKENDS}, not {backend!r}')
        self.jobs = jobs
        self.backend = 'serial' if jobs == 1 else backend
        self.key = None if key is None else bytes(key)
        self.start_seconds = 0.0
        # Seconds from creating the pool until the first, and until every one, of its `jobs`
        # worker processes had started (None until then); 0 without worker processes.
        self.first_ready_seconds = self.ready_seconds = 0.0
        self.start_method = None
        self._lock = threading.Lock()
        self._executor = None
        self._closed = False
        self._ready_pids = set()
        self._ready_thread = None
        self._ready_ends = ()
        started = time.perf_counter()
        if self.backend == 'thread':
            self._executor = ThreadPoolExecutor(max_workers=jobs, thread_name_prefix='pgdelta-hash')
        elif self.backend == 'process':
            if self.key is None:
                raise ValueError('the process backend needs the session key')
            Hasher(self.key)  # checks the key before any process starts
            self._start_workers(started)
        self.start_seconds = time.perf_counter() - started

    def _start_workers(self, started):
        """Start `jobs` worker processes now, so their start-up overlaps the session's first messages.

        Each worker sends its pid on a pipe at the end of its initializer; a
        watcher thread here notes when the first and when the `jobs`-th
        distinct worker reported (first_ready_seconds, ready_seconds). No
        task waits for that: work queued meanwhile runs on whichever worker
        is up. The pings only make the executor start every worker now.
        """
        self.start_method = start_method()
        self.first_ready_seconds = self.ready_seconds = None
        context = multiprocessing.get_context(self.start_method)
        reader, writer = context.Pipe(duplex=False)
        self._ready_ends = (reader, writer)
        try:
            self._executor = ProcessPoolExecutor(max_workers=self.jobs, mp_context=context,
                                                 initializer=_worker_init, initargs=(self.key, writer))
            self._ready_thread = threading.Thread(target=self._watch_ready, args=(reader, started),
                                                  name='pgdelta-pool-ready', daemon=True)
            self._ready_thread.start()
            for _ in range(self.jobs):
                self._executor.submit(_worker_ping)
        except BaseException:
            self.close()
            raise

    def _watch_ready(self, reader, started):
        try:
            while not self._closed:
                if not reader.poll(_READY_POLL_SECONDS):
                    continue
                pid, = _READY_MESSAGE.unpack(reader.recv_bytes())
                elapsed = time.perf_counter() - started
                with self._lock:
                    self._ready_pids.add(pid)
                    if self.first_ready_seconds is None:
                        self.first_ready_seconds = elapsed
                    if len(self._ready_pids) == self.jobs:
                        self.ready_seconds = elapsed
                        return
        except (EOFError, OSError, ValueError, struct.error):
            pass  # closed under us: the pool is shutting down

    @property
    def ready_pids(self):
        """Pids of the worker processes that have reported that they started."""
        with self._lock:
            return frozenset(self._ready_pids)

    @property
    def parallel(self):
        return self._executor is not None

    def map(self, function, items, *, stop=None, window=None):
        """[function(item) for item in items], in order.

        At most `window` (default 2 x jobs) tasks are queued at a time.
        `stop`, a threading.Event, makes the call raise HashingStopped before
        it queues another task. Every task this call started has finished
        when it returns or raises. For the process backend, `function` and
        the items must be picklable.
        """
        items = list(items)
        if self._executor is None or len(items) < 2:
            results = []
            for item in items:
                _check_stop(stop)
                results.append(function(item))
            return results
        limit = max(1, window or 2 * self.jobs)
        results, pending = [], deque()
        try:
            for item in items:
                _check_stop(stop)
                pending.append(self._submit(function, item))
                if len(pending) >= limit:
                    results.append(self._result(pending.popleft()))
            while pending:
                results.append(self._result(pending.popleft()))
        finally:
            # A thread task reads the caller's file descriptor, so none may still run once the
            # caller closes it. Cancel what has not started and wait for the rest. A future that
            # cancel() succeeds on never runs; and one cancelled by close() in another thread is
            # never notified, so waiting on it would block forever (the EXP-05 deadlock).
            started = [future for future in pending if not future.cancel()]
            if started:
                wait(started)
        return results

    def _submit(self, function, item):
        try:
            return self._executor.submit(function, item)
        except BrokenExecutor as exc:
            raise TransferError(f'a hashing worker stopped unexpectedly: {exc}') from exc
        except RuntimeError as exc:  # shut down, by close() in another thread
            raise HashingStopped(f'hashing stopped: the pool was closed ({exc})') from exc

    @staticmethod
    def _result(future):
        try:
            return future.result()
        except BrokenExecutor as exc:
            raise TransferError(f'a hashing worker stopped unexpectedly: {exc}') from exc
        except CancelledError as exc:
            raise HashingStopped('hashing stopped: the pool was closed') from exc

    def node_digests(self, path, fd, size, hasher, block, node_blocks, nodes, keep_leaves, *, stop=None,
                     window=None):
        """[(node digest, leaf digests or b'') for node in nodes] of the regular file open as `fd`.

        `path` is where `fd` was opened; process workers reopen it and check
        that it is the same file. Raises _SizeChanged if a node ends early.
        """
        nodes = list(nodes)
        if self.backend == 'process' and len(nodes) > 1:
            if hasher.key != self.key:
                raise ValueError('the hasher and the hash pool use different keys')
            per_task = max(1, min(TASK_BYTES // (block * node_blocks), -(-len(nodes) // self.jobs)))
            chunks = [tuple(nodes[start:start + per_task]) for start in range(0, len(nodes), per_task)]
            if len(chunks) > 1:
                info = os.fstat(fd)
                task = functools.partial(_worker_hash_nodes, os.fspath(path), (info.st_dev, info.st_ino), size,
                                         block, node_blocks, keep_leaves)
                return [part for chunk in self.map(task, chunks, stop=stop, window=window) for part in chunk]
        if self.backend == 'process':
            pool = SERIAL  # one task's worth: cheaper here than a round trip to a worker
        else:
            pool = self
        task = functools.partial(_hash_node, fd, size, hasher, block, node_blocks, keep_leaves)
        return pool.map(task, nodes, stop=stop, window=window)

    def close(self):
        """Stop the workers, after the tasks already running finish. Safe while another thread uses the pool."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
        if self._ready_thread is not None:
            # It sees _closed within one poll; the pipe is closed only after, never under its poll().
            self._ready_thread.join(10 * _READY_POLL_SECONDS + 5)
        if self._ready_thread is None or not self._ready_thread.is_alive():
            for end in self._ready_ends:
                end.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def _check_stop(stop):
    if stop is not None and stop.is_set():
        raise HashingStopped('hashing stopped because the run is ending')


SERIAL = HashPool(1)


def page_count(size, block):
    return -(-size // block)


def node_count(size, block, node_blocks):
    return -(-size // (block * node_blocks))


def span_length(index, unit, size):
    """Bytes of span `index` (of width `unit`) that lie inside a file of `size` bytes."""
    return max(0, min(unit, size - index * unit))


def split_digests(blob):
    return [bytes(blob[i:i + DIGEST_SIZE]) for i in range(0, len(blob), DIGEST_SIZE)]


@dataclass(frozen=True)
class FileTree:
    size: int
    block: int
    node_blocks: int
    digest: bytes
    node_blob: bytes
    leaf_blob: bytes = b''

    @property
    def page_count(self):
        return page_count(self.size, self.block)

    @property
    def node_count(self):
        return node_count(self.size, self.block, self.node_blocks)

    @property
    def nodes(self):
        return split_digests(self.node_blob)

    @property
    def leaves(self):
        """Leaf digests; empty when the tree was built with keep_leaves=False."""
        return split_digests(self.leaf_blob)

    def node(self, index):
        return self.node_blob[index * DIGEST_SIZE:(index + 1) * DIGEST_SIZE]

    def leaf(self, page):
        return self.leaf_blob[page * DIGEST_SIZE:(page + 1) * DIGEST_SIZE]


def open_regular(path):
    """Open read-only without following a final symlink or blocking on a FIFO."""
    flags = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
             | getattr(os, 'O_NONBLOCK', 0))
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, 'not a regular file', str(path))
    except BaseException:
        os.close(fd)
        raise
    return fd


def pread_exact(fd, length, offset):
    parts = []
    remaining = length
    while remaining:
        data = os.pread(fd, remaining, offset + length - remaining)
        if not data:
            raise OSError(errno.EIO, f'short read: expected {length} bytes at offset {offset}')
        parts.append(data)
        remaining -= len(data)
    return parts[0] if len(parts) == 1 else b''.join(parts)


def _read_full(stream, view):
    filled = 0
    while filled < len(view):
        count = stream.readinto(view[filled:])
        if not count:
            break
        filled += count
    return filled


def _piece_bytes(block):
    """Bytes read at a time: whole pages, at most READ_BYTES unless one page is larger."""
    return block * max(1, READ_BYTES // block)


def hash_stream(stream, hasher, block=BLOCK, node_blocks=NODE_BLOCKS, *, keep_leaves=True):
    view = memoryview(bytearray(_piece_bytes(block)))
    leaves_per_node = node_blocks * DIGEST_SIZE
    nodes, leaves, pending = bytearray(), bytearray(), bytearray()
    size = 0
    while True:
        filled = _read_full(stream, view)
        size += filled
        for offset in range(0, filled, block):
            pending += hasher.leaf(view[offset:min(offset + block, filled)])
            if len(pending) == leaves_per_node:
                nodes += hasher.node(pending)
                if keep_leaves:
                    leaves += pending
                pending.clear()
        if filled < len(view):
            break
    if pending:
        nodes += hasher.node(pending)
        if keep_leaves:
            leaves += pending
    nodes = bytes(nodes)
    return FileTree(size, block, node_blocks, hasher.file(size, nodes), nodes, bytes(leaves))


def _file_size(fd):
    return os.fstat(fd).st_size


def _read_span(fd, length, offset):
    """The `length` bytes at `offset`, in this thread's reusable buffer (a view valid until its next call).

    Callers ask for at most _piece_bytes(block) bytes, so the buffer stays
    at most READ_BYTES (or one page) per thread, whatever the node size.
    """
    buffer = getattr(_local, 'buffer', None)
    if buffer is None or len(buffer) < length:
        buffer = _local.buffer = bytearray(max(length, READ_BYTES))
    view = memoryview(buffer)[:length]
    filled = 0
    while filled < length:
        if _PREADV:
            count = os.preadv(fd, [view[filled:]], offset + filled)
        else:
            data = os.pread(fd, length - filled, offset + filled)
            count = len(data)
            view[filled:filled + count] = data
        if not count:
            raise _SizeChanged
        filled += count
    return view


def _hash_node(fd, size, hasher, block, node_blocks, keep_leaves, node):
    """(node digest, leaf digests or b'') of one node of a file of `size` bytes."""
    offset = node * block * node_blocks
    end = min(offset + block * node_blocks, size)
    piece = _piece_bytes(block)
    parts = []
    while offset < end:
        length = min(piece, end - offset)
        view = _read_span(fd, length, offset)
        parts += [hasher.leaf(view[start:start + block]) for start in range(0, length, block)]
        offset += length
    leaves = b''.join(parts)
    return hasher.node(leaves), leaves if keep_leaves else b''


def hash_file(path, key, block=BLOCK, node_blocks=NODE_BLOCKS, *, keep_leaves=True, hasher=None,
              pool=None, stop=None, window=None):
    """Hash tree of the regular file at `path`, its nodes hashed on `pool` (default: this thread).

    If the file's length changes while it is read, it is hashed again in one
    sequential pass, so the tree's size is what that pass read and a caller
    that compares it with the expected size reports the change. `stop` and
    `window` are passed on to HashPool.map.
    """
    hasher = hasher or Hasher(key)
    fd = open_regular(path)
    try:
        size = _file_size(fd)
        try:
            parts = (pool or SERIAL).node_digests(path, fd, size, hasher, block, node_blocks,
                                                  range(node_count(size, block, node_blocks)), keep_leaves,
                                                  stop=stop, window=window)
            if os.pread(fd, 1, size):
                raise _SizeChanged
        except _SizeChanged:
            with open(fd, 'rb', buffering=0, closefd=False) as stream:
                return hash_stream(stream, hasher, block, node_blocks, keep_leaves=keep_leaves)
        nodes = b''.join([digest for digest, _ in parts])
        leaves = b''.join([blob for _, blob in parts])
        return FileTree(size, block, node_blocks, hasher.file(size, nodes), nodes, leaves)
    finally:
        os.close(fd)


def leaf_digests(fd, first_page, count, size, hasher, block=BLOCK):
    """Concatenated leaf digests of pages [first_page, first_page + count) that lie inside the file."""
    start = first_page * block
    length = max(0, min(count * block, size - start))
    view = memoryview(pread_exact(fd, length, start))
    return b''.join(hasher.leaf(view[offset:offset + block]) for offset in range(0, length, block))
