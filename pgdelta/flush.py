"""Where the receiver's output files are flushed to disk (`fsync`), for T3 arm (b).

- "main" (default): each finished file is fsynced in the receiver's main
  loop before the next file is accepted, as v0 always did.
- "writeback": as "main", but on Linux write-back of the file being written
  is started early, with sync_file_range(SYNC_FILE_RANGE_WRITE), every
  WRITEBACK_BYTES written to it, so the fsync at its end has little left to
  flush. sync_file_range only starts the writes: it makes nothing durable,
  so the fsync stays. Elsewhere, or where the call is refused, it falls back
  to "main" and the statistics say so.
- "worker": each finished file's descriptor goes to one background thread
  (FsyncWorker), which fsyncs and closes it while the main loop goes on.
  Nothing is published or committed until every file handed over has been
  fsynced, and any fsync error fails the run.
"""
from __future__ import annotations

import ctypes
import errno
import os
import queue
import sys
import threading
import time

from pgdelta.errors import TransferError

FSYNC_MODES = ('main', 'writeback', 'worker')
DEFAULT_FSYNC = 'main'
WRITEBACK_BYTES = 8 << 20  # writeback: start write-back of the current file every this many bytes written
FSYNC_QUEUE_FILES = 32  # worker: finished files waiting for their fsync before the main loop has to wait
SYNC_FILE_RANGE_WRITE = 2  # <fcntl.h>: start write-out of dirty pages not already under write-out
# sync_file_range errors that mean "not here" (no such call, or not for this file or filesystem).
_UNSUPPORTED = frozenset(code for code in (errno.ENOSYS, errno.EINVAL, errno.ESPIPE, errno.EOPNOTSUPP,
                                           getattr(errno, 'ENOTSUP', None)) if code is not None)
_sync_file_range = None
_loaded = False


def _load():
    """libc's sync_file_range through ctypes, or None (not Linux, or not found)."""
    global _sync_file_range, _loaded
    if not _loaded:
        _loaded = True
        if sys.platform.startswith('linux'):
            try:
                function = ctypes.CDLL(None, use_errno=True).sync_file_range
            except (OSError, AttributeError):
                function = None
            if function is not None:
                function.argtypes = (ctypes.c_int, ctypes.c_int64, ctypes.c_int64, ctypes.c_uint)
                function.restype = ctypes.c_int
                _sync_file_range = function
    return _sync_file_range


def writeback_available():
    """Whether early write-back can be tried here (Linux with sync_file_range)."""
    return _load() is not None


def start_writeback(fd, offset, length):
    """Start write-back of [offset, offset + length) of fd; False if this platform or file refuses it.

    Any other error (EIO, ENOSPC, ...) is raised as OSError: it would fail
    the fsync that follows anyway.
    """
    function = _load()
    if function is None:
        return False
    if function(fd, offset, length, SYNC_FILE_RANGE_WRITE) == 0:
        return True
    code = ctypes.get_errno()
    if code in _UNSUPPORTED:
        return False
    raise OSError(code, os.strerror(code))


class FsyncWorker:
    """Fsyncs and closes finished output files on one background thread, in the order they arrive.

    submit() hands a descriptor over: from then on the worker owns it and
    closes it, fsynced or not. At most `limit` files wait; submit() blocks
    beyond that and returns how long it waited. The first error is kept in
    `error` and passed to `on_error` at once; files after it are closed
    without fsync. finish() waits for every file handed over; nothing may be
    published or committed before it returns without an error. close()
    stops early: what is left is closed without fsync.
    """

    def __init__(self, *, on_error=None, stop=None, limit=FSYNC_QUEUE_FILES):
        self.queue = queue.Queue(limit)
        self.on_error, self.stop = on_error, stop
        self.error = None
        self.busy_seconds = 0.0  # time spent in fsync, overlapping the main loop
        self.files = 0
        self.abandoned = False
        self.thread = threading.Thread(target=self._run, name='pgdelta-fsync', daemon=True)
        self.thread.start()

    def submit(self, fd, path):
        started = time.perf_counter()
        self.queue.put((fd, path))
        return time.perf_counter() - started

    def finish(self):
        self.queue.put(None)
        self.thread.join()

    def close(self, timeout):
        self.abandoned = True
        try:
            self.queue.put(None, timeout=timeout)
        except queue.Full:
            pass
        self.thread.join(timeout)

    def _skip(self):
        return self.error is not None or self.abandoned or (self.stop is not None and self.stop.is_set())

    def _run(self):
        while True:
            item = self.queue.get()
            if item is None:
                return
            fd, path = item
            try:
                if not self._skip():
                    started = time.perf_counter()
                    os.fsync(fd)
                    self.busy_seconds += time.perf_counter() - started
                    self.files += 1
            except BaseException as exc:
                self._fail(f'cannot fsync {path}: {exc}')
            finally:
                try:
                    os.close(fd)
                except OSError as exc:
                    self._fail(f'cannot close {path} after fsync: {exc}')

    def _fail(self, message):
        if self.error is not None or self.abandoned:
            return
        self.error = TransferError(message)
        if self.on_error is not None:
            try:
                self.on_error(self.error)
            except BaseException:
                pass
