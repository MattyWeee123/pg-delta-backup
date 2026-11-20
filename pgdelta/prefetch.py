"""Read the basis B into the page cache while `receive --listen` waits for the sender.

The session key is random per run and arrives in the sender's HELLO, so the
receiver can't hash B before the sender connects. It can read B, though.
Without this, the receiver's first pass over B (hashing it for the sender)
reads it cold whenever B isn't cached: after a cache drop, after a reboot,
or when B was written long ago. That read is on the transfer's critical
path. While the listener waits, a background thread reads B's files instead:

- in B's inventory order, which is also the order in which the sender lists
  S's files and so the order in which the receiver hashes B's;
- sequentially, in pieces of CHUNK_BYTES into one reused buffer, with
  posix_fadvise(POSIX_FADV_WILLNEED) on the next piece where the platform
  has it, so the disk works on piece k + 1 while piece k is copied;
- read-only: every file is opened with O_RDONLY, O_NOFOLLOW and O_NONBLOCK,
  and with O_NOATIME where the kernel allows it (the file's owner), so the
  prefetch doesn't even update B's access times. Nothing is ever written.

It stops:
- when the sender connects. The receiver's own reads of B start right away,
  in the same order, and a second sequential reader would compete with them
  for the disk;
- when it has read every file, or `cap_bytes` (below);
- on its first read error, which is recorded and never raised. The
  receiver's own reads of B report real problems with B as they always did;
- when the receive command ends in any other way: a failure while waiting,
  SIGINT or SIGTERM. The thread is a daemon and finishes within one piece.

**The cap.** By default the prefetch reads at most half of the memory the
kernel says it could give the page cache now (MemAvailable on Linux; a
quarter of physical memory elsewhere). The page cache drops the least
recently used pages first, and the files read first are the ones the
receiver needs first. Read more of B than can stay cached and the prefetch
evicts its own first files before the receiver gets to them, so B is read
twice instead of once. The other half is left for what the run itself
caches while the prefetched files must stay: O as it is written, fsynced
and re-read for verification, and B again for copy-then-patch. When B is
larger than the cap, its first cap bytes are prefetched and the rest is
read by the receiver as before.

Prefetching changes nothing but timings and its own statistics: no output,
digest, byte count or page count. It only decides whether the receiver's
reads of B later find their pages in the page cache. Verification is
untouched: every output file is still re-read and checked against the
source's digest.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
import threading
import time

CHUNK_BYTES = 8 << 20
SWITCHES = ('on', 'off')
DEFAULT_SWITCH = 'on'
JOIN_SECONDS = 5.0
# Without any memory figure, a cap that suits a small machine.
FALLBACK_CAP_BYTES = 1 << 30
MEMINFO = Path('/proc/meminfo')
_ADVISE = hasattr(os, 'posix_fadvise') and hasattr(os, 'POSIX_FADV_WILLNEED')
_PREADV = hasattr(os, 'preadv')
_NOATIME = getattr(os, 'O_NOATIME', 0)
_READ_FLAGS = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
               | getattr(os, 'O_NONBLOCK', 0))
# Reasons a prefetch that was still reading was stopped from outside.
CONNECTED, INTERRUPTED, FAILED = 'connected', 'interrupted', 'failed'
# ... and the ways it ends on its own.
FINISHED, CAPPED, ERROR = 'finished', 'cap', 'error'


def memory_available(meminfo=MEMINFO):
    """Bytes the kernel could give the page cache now (Linux's MemAvailable), or None."""
    try:
        text = Path(meminfo).read_text()
    except OSError:
        return None
    for line in text.splitlines():
        name, _, rest = line.partition(':')
        if name == 'MemAvailable':
            parts = rest.split()
            try:
                value = int(parts[0])
            except (IndexError, ValueError):
                return None
            unit = parts[1].lower() if len(parts) > 1 else ''
            return value * 1024 if unit == 'kb' else value
    return None


def physical_memory():
    """Bytes of physical memory, or None."""
    try:
        pages, size = os.sysconf('SC_PHYS_PAGES'), os.sysconf('SC_PAGE_SIZE')
    except (AttributeError, ValueError, OSError):
        return None
    return pages * size if pages > 0 and size > 0 else None


def default_cap(meminfo=MEMINFO):
    """Half of MemAvailable; without it, a quarter of physical memory; else FALLBACK_CAP_BYTES."""
    available = memory_available(meminfo)
    if available is not None:
        return available // 2
    total = physical_memory()
    return total // 4 if total else FALLBACK_CAP_BYTES


def not_run(switch, why):
    """The statistics of a receiver that did not prefetch, in the same shape."""
    return {'switch': switch, 'ran': False, 'why_not': why, 'files': 0, 'basis_bytes': None,
            'cap_bytes': None, 'files_read': 0, 'bytes': 0, 'seconds': 0.0, 'waited_seconds': None,
            'started_utc': None, 'ended_utc': None, 'stopped': None, 'fadvise': False, 'noatime': None,
            'error': None}


class BasisPrefetcher:
    """Reads B's regular files, in inventory order, on a daemon thread until stopped.

    files: B's inventory entries (path relative to `basis`, size), in the
    order the receiver will read them. stop() returns the statistics.
    """

    def __init__(self, basis, files, *, cap_bytes=None, chunk_bytes=CHUNK_BYTES, advise=_ADVISE):
        if chunk_bytes < 1:
            raise ValueError('chunk_bytes must be at least 1')
        self.basis = Path(basis)
        self.files = [(entry.path, entry.size) for entry in files]
        self.basis_bytes = sum(size for _, size in self.files)
        self.cap_bytes = default_cap() if cap_bytes is None else max(0, int(cap_bytes))
        self.chunk_bytes = chunk_bytes
        self.advise = advise
        self.thread = None
        self._stop = threading.Event()
        self.started = self.ended = self.stopped_at = None
        self.started_utc = None  # time.time() at start, so others on this host can place the reading
        self.files_read = self.bytes_read = 0
        self.outcome = self.reason = self.error = None
        self.advised = False
        self.noatime = None  # True once O_NOATIME was accepted, False once refused

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        self.started_utc = time.time()
        self.started = time.perf_counter()
        self.thread = threading.Thread(target=self._run, name='pgdelta-prefetch', daemon=True)
        self.thread.start()
        return self

    def stop(self, reason):
        """Stop reading if it still is, wait for the thread (at most one piece), and return the statistics."""
        if self.stopped_at is None:
            self.stopped_at = time.perf_counter()
            self.reason = reason
        self._stop.set()
        if self.thread is not None:
            self.thread.join(JOIN_SECONDS)
        return self.stats()

    def stats(self):
        now = time.perf_counter()
        end = self.ended if self.ended is not None else (self.stopped_at or now)
        started = self.started if self.started is not None else end
        seconds = end - started
        return {
            'switch': 'on',
            'ran': self.started is not None,
            'why_not': None,
            'files': len(self.files),
            'basis_bytes': self.basis_bytes,
            'cap_bytes': self.cap_bytes,
            'files_read': self.files_read,  # read to their end
            'bytes': self.bytes_read,
            'seconds': round(seconds, 6),  # reading, until it ended or was stopped
            # From the start until stop(): how long the listener waited for the sender.
            'waited_seconds': None if self.stopped_at is None else round(self.stopped_at - started, 6),
            # When the reading started and ended on this host's clock (seconds since the epoch), so a
            # caller timing the receiver from outside can tell which of its own intervals it fell in.
            'started_utc': None if self.started_utc is None else round(self.started_utc, 6),
            'ended_utc': None if self.started_utc is None else round(self.started_utc + seconds, 6),
            # finished, cap or error if it ended on its own; else why it was stopped (connected, ...)
            'stopped': self.outcome or self.reason,
            'fadvise': self.advised,
            'noatime': self.noatime,
            'error': self.error,
        }

    # --- the reading thread ----------------------------------------------------------------------

    def _run(self):
        outcome, path = FINISHED, None
        try:
            buffer = memoryview(bytearray(self.chunk_bytes))
            for path, size in self.files:
                if self._stop.is_set():
                    outcome = None
                    break
                budget = self.cap_bytes - self.bytes_read
                if size and budget <= 0:
                    outcome = CAPPED
                    break
                if not size:
                    self.files_read += 1
                    continue
                complete = self._read_file(path, min(size, budget), buffer)
                if complete is None:  # stopped from outside
                    outcome = None
                    break
                if complete and budget >= size:
                    self.files_read += 1
                elif budget < size:
                    outcome = CAPPED
                    break
        except OSError as exc:
            outcome = ERROR
            self.error = f'{path}: {exc.strerror or exc}'
        except BaseException as exc:  # never let the prefetch disturb the receiver
            outcome = ERROR
            self.error = f'{path}: {exc!r}'
        finally:
            self.ended = time.perf_counter()
            self.outcome = outcome

    def _read_file(self, path, limit, buffer):
        """Read [0, limit) of one file. True at its end or the limit, None if stopped meanwhile."""
        fd = self._open(self.basis / path)
        try:
            offset = 0
            while offset < limit:
                if self._stop.is_set():
                    return None
                length = min(len(buffer), limit - offset)
                ahead = offset + length
                if self.advise and ahead < limit:
                    self._advise(fd, ahead, min(self.chunk_bytes, limit - ahead))
                count = self._read(fd, buffer[:length], offset)
                if not count:
                    return True  # shorter than when B was scanned: the receiver will say so
                offset += count
                self.bytes_read += count
            return True
        finally:
            os.close(fd)

    def _advise(self, fd, offset, length):
        try:
            os.posix_fadvise(fd, offset, length, os.POSIX_FADV_WILLNEED)
            self.advised = True
        except OSError:
            self.advise = False  # not on this filesystem: plain reads only

    @staticmethod
    def _read(fd, view, offset):
        if _PREADV:
            return os.preadv(fd, [view], offset)
        data = os.pread(fd, len(view), offset)
        view[:len(data)] = data
        return len(data)

    def _open(self, path):
        fd = None
        if _NOATIME and self.noatime is not False:
            try:
                fd = os.open(path, _READ_FLAGS | _NOATIME)
                self.noatime = True
            except PermissionError as exc:
                if exc.errno != errno.EPERM:  # EACCES: unreadable either way
                    raise
                self.noatime = False  # EPERM: O_NOATIME needs the file's owner
        if fd is None:
            fd = os.open(path, _READ_FLAGS)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(0, 'not a regular file')
        except BaseException:
            os.close(fd)
            raise
        return fd


def while_waiting(wait, prefetcher):
    """Run wait() (the listener's accept) while `prefetcher` reads B; stop it however wait() ends.

    Returns (wait's result, the prefetch statistics). The prefetch is stopped
    with reason "connected" when wait() returns, "interrupted" on
    KeyboardInterrupt (SIGINT, or SIGTERM through the command line's
    handler) and "failed" on any other exception, which is then re-raised.
    """
    prefetcher.start()
    reason = FAILED
    try:
        result = wait()
        reason = CONNECTED
    except KeyboardInterrupt:
        reason = INTERRUPTED
        raise
    finally:
        stats = prefetcher.stop(reason)
    return result, stats


def describe(stats):
    """One line for the receiver's log."""
    if not stats['ran']:
        return f'basis prefetch off ({stats["why_not"]})'
    line = (f'prefetched {stats["bytes"]} of {stats["basis_bytes"]} basis bytes ({stats["files_read"]} of '
            f'{stats["files"]} files) in {stats["seconds"]:.3f} s, while waiting {stats["waited_seconds"]:.3f} s '
            f'for the sender; stopped: {stats["stopped"]}')
    if stats['error']:
        line += f' ({stats["error"]})'
    return line
