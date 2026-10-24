"""The sender's protocol timers (EXP-13): how long it waited for the receiver, and whether its own data was
still queued meanwhile.

The sender waits for the receiver in four places: for ACCEPT after HELLO,
for each file's basis digests (BASIS_NODES), for the leaf digests it asked
for (LEAF_DIGESTS), and for the final COMPLETE. Each kind's waits are timed
and counted. A wait whose reply had already arrived is no wait: it is neither
timed nor counted. The sender also times how long its sends blocked: page
data apart from every other frame.

A wait is not always the protocol's. Leaf requests travel in the same TCP
stream as page data, and send() returns once the kernel has taken a frame,
not once it has left. With large send buffers a request can sit behind tens
of MB of earlier pages, and behind a full queue at the link, while the link
is busy with them. So each wait is split into busy and idle parts by reading
the socket's send queue every POLL_SECONDS while it waits (Linux ioctls on
the TCP socket):

- SIOCOUTQNSD: bytes written but not yet sent (write_seq - snd_nxt);
- SIOCOUTQ: bytes written but not yet acknowledged (write_seq - snd_una).

A slice is busy when some bytes are unsent, or when the bytes in flight (the
second less the first) exceed `busy_inflight_bytes`, what the path holds
without queueing (EXP-13 passes the visit's one-stream steady rate times its
ping minimum). Without it, only unsent bytes make a slice busy. Otherwise the
slice is idle. Each slice is classified by the reading at its start, so busy
plus idle is the wait, and each part is good to about one slice per wait.

Where there are no readings (not Linux, or a pipe as in `local`), or a
reading fails, the totals stay and the busy and idle parts are null. The
readings see only this socket: through a proxy (EXP-13's stunnel arm) the
bytes it holds don't show, so a wait can read idle while the link is busy.

Cost: two perf_counter() calls per blocked wait and per send; while a wait
blocks, one wake-up, two ioctls and a clock read per POLL_SECONDS. A wait
that finds its reply already there costs nothing more than before.
"""
from __future__ import annotations

import struct
import sys
import time

KINDS = ('accept', 'basis', 'leaf', 'complete')
POLL_SECONDS = 0.001  # how often a blocked wait reads the send queue
LONG_WAIT_SECONDS = 0.001  # waits longer than this are counted apart (EXP-13: "waits over 1 ms")
SIOCOUTQNSD = 0x894B  # <linux/sockios.h>: output queue size, not sent only
TIOCOUTQ_GENERIC = 0x5411  # <asm-generic/ioctls.h>; SIOCOUTQ is TIOCOUTQ, whose value a few architectures change
_INT = struct.Struct('i')
_ZERO = bytes(_INT.size)


class SendQueue:
    """The send queue of a connected TCP socket on Linux, read as (unacknowledged, unsent) bytes.

    `available` is False, with `why_not` saying why, for no socket (pipes),
    another platform, or a socket the ioctls refuse (tried once here).
    """

    def __init__(self, sock, platform=None):
        self.fd, self.why_not = None, None
        platform = sys.platform if platform is None else platform
        if sock is None:
            self.why_not = 'no socket: the channel is not a TCP connection'
            return
        if not platform.startswith('linux'):
            self.why_not = f'SIOCOUTQ and SIOCOUTQNSD are Linux ioctls, not available on {platform}'
            return
        try:
            import fcntl
            import termios
        except ImportError as exc:
            self.why_not = f'cannot read the send queue: {exc}'
            return
        self._ioctl = fcntl.ioctl
        self.siocoutq = getattr(termios, 'TIOCOUTQ', TIOCOUTQ_GENERIC)
        try:
            self.fd = sock.fileno()
            self.read()
        except (OSError, ValueError) as exc:
            self.fd, self.why_not = None, f'reading the send queue failed: {exc}'

    @property
    def available(self):
        return self.fd is not None

    def read(self):
        """(unacknowledged, unsent) bytes. Unsent is read first: while the sender waits nothing is
        written, so it only falls, and the in-flight bytes are then those of the second reading."""
        unsent = _INT.unpack(self._ioctl(self.fd, SIOCOUTQNSD, _ZERO))[0]
        unacknowledged = _INT.unpack(self._ioctl(self.fd, self.siocoutq, _ZERO))[0]
        return unacknowledged, unsent


class _Kind:
    __slots__ = ('seconds', 'busy', 'idle', 'count', 'long')

    def __init__(self):
        self.seconds = self.busy = self.idle = 0.0
        self.count = self.long = 0


class WaitTimers:
    """The sender's waits by kind, each split into busy and idle, and its time blocked in send().

    Used from the sender's main thread only, one wait at a time:
    begin(kind) when a wait first blocks, tick() after each wake-up
    (blocking at most `poll` seconds in between; None means no readings,
    so block until woken), end() when it returns or fails.
    """

    def __init__(self, queue, busy_inflight_bytes=None, *, poll=POLL_SECONDS, clock=time.perf_counter):
        self.queue, self.busy_inflight_bytes, self.clock = queue, busy_inflight_bytes, clock
        self.poll = poll if queue.available else None
        self.kinds = {kind: _Kind() for kind in KINDS}
        self.page_send_seconds = self.other_send_seconds = 0.0
        self.readings, self.failure = 0, None
        self._kind = self._started = self._last = self._busy = None

    def begin(self, kind):
        now = self.clock()
        self._kind, self._started, self._last = self.kinds[kind], now, now
        self._busy = self._reading()

    def tick(self):
        self._account(self.clock())
        self._busy = self._reading()

    def end(self):
        now = self.clock()
        self._account(now)
        kind, elapsed = self._kind, now - self._started
        kind.seconds += elapsed
        kind.count += 1
        kind.long += elapsed > LONG_WAIT_SECONDS
        self._kind = self._started = self._last = self._busy = None

    def sent(self, page_data, seconds):
        """Count `seconds` spent in one send() of page data, or of any other frame."""
        if page_data:
            self.page_send_seconds += seconds
        else:
            self.other_send_seconds += seconds

    def _account(self, now):
        if self._busy is not None:
            if self._busy:
                self._kind.busy += now - self._last
            else:
                self._kind.idle += now - self._last
        self._last = now

    def _reading(self):
        """Whether our own data is still queued now (True or False), or None without a reading."""
        if self.poll is None:
            return None
        try:
            unacknowledged, unsent = self.queue.read()
        except OSError as exc:
            # The parts would no longer add up to the waits: drop them, keep the totals.
            self.failure, self.poll = f'reading the send queue failed: {exc}', None
            return None
        self.readings += 1
        if unsent > 0:
            return True
        return self.busy_inflight_bytes is not None and unacknowledged - unsent > self.busy_inflight_bytes

    def summary(self, connect_seconds=None):
        """The `protocol_timers` statistics: EXP-13's names, in seconds, with the queue readings' status."""
        split = self.queue.available and self.failure is None
        result = {'connect_s': _rounded(connect_seconds)}
        for name, kind in self.kinds.items():
            result[f'{name}_wait_s'] = round(kind.seconds, 6)
            result[f'{name}_wait_busy_s'] = round(kind.busy, 6) if split else None
            result[f'{name}_wait_idle_s'] = round(kind.idle, 6) if split else None
            result[f'{name}_waits'] = kind.count
            result[f'{name}_waits_over_1ms'] = kind.long
        result['page_send_s'] = round(self.page_send_seconds, 6)
        result['other_send_s'] = round(self.other_send_seconds, 6)
        result['send_queue'] = {
            'split': split,
            'why_not': None if split else (self.queue.why_not or self.failure),
            'busy_inflight_bytes': self.busy_inflight_bytes,
            'poll_s': POLL_SECONDS if self.queue.available else None,
            'readings': self.readings,
        }
        return result


def _rounded(seconds):
    return None if seconds is None else round(seconds, 6)
