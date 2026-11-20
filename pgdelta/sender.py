"""The source side (S): walk S and decide, locally, what the receiver already has.

Nothing is asked for. The receiver streams B's digests unasked (BASIS_DIGESTS); a reader thread here
drains them into a `(file, chunk) -> digest` table while the main thread walks S, hashes each chunk,
and compares it against B's digest without ever waiting for a reply. Consecutive chunks B already
holds accumulate into one `CHUNK_COPY` run-length instruction; everything else is sent as
`CHUNK_DATA`. This is rsync's generator -> sender -> receiver pipeline: both sides read and hash at
the same time, and the request/reply dependency that put a floor of one round trip per file is gone.

Two threads, and the rule that keeps them from deadlocking: the reader thread sits in `recv` for the
whole session, so this side never stops draining the receiver's digests while the main thread is
blocked writing page data. The main thread never touches the socket for reading -- it waits only on
the digest table's condition, which the reader thread notifies on every arrival and on its own death.
"""
from __future__ import annotations

import json
import socket
import stat
import sys
import threading
from pathlib import Path

from pgdelta import protocol
from pgdelta.hashing import Hasher, new_key
from pgdelta.protocol import (BASIS_DIGESTS, CHUNK_COPY, CHUNK_COPY_BODY, CHUNK_DATA, CHUNK_DATA_HEAD, COMPLETE,
                              FILE_DONE, FILE_DONE_BODY, HELLO, RESULT, Channel, comparable,
                              unpack_digest_frame)
from pgdelta.events import EventLog
from pgdelta.stats import Stats
from pgdelta.tree import Entry, scan

DEFAULT_CHUNK_SIZE = 8192  # one PostgreSQL page

# Bytes of undelivered digests the reader thread will hold before it stops draining the socket, which
# lets TCP backpressure stall the receiver's generator rather than growing this table without bound.
# 64 MiB is 4,194,304 digests, which is about 32 GiB of basis at an 8 KiB chunk.
DEFAULT_DIGEST_WINDOW = 64 << 20

# The reader thread's window-full check only gates the *next* recv(), so one BASIS_DIGESTS frame
# always lands no matter how small the window is -- and a frame can hold up to DIGESTS_PER_FRAME
# digests. A window under that many bytes would, after admitting that one frame, already be over
# its own limit before the main thread has taken anything from it: every digest in the frame would
# then have to be drained before the reader is allowed to ask for the next one, collapsing what is
# meant to be a pipeline into a fully synchronous, frame-at-a-time handoff -- and if that handoff
# ever stalls (the receiver not draining is the one this engine has to survive; see Deadlock in
# tests/test_pgdelta_stream.py), the reader has nothing banked to keep the main thread moving while
# it waits. Flooring the window at one whole frame is the smallest value that never puts the reader
# behind on the very first frame it is handed.
MIN_DIGEST_WINDOW = protocol.DIGESTS_PER_FRAME * protocol.DIGEST_SIZE


def effective_digest_window(digest_window: int) -> int:
    """`digest_window`, floored at `MIN_DIGEST_WINDOW` so it can never start the reader out behind."""
    return max(MIN_DIGEST_WINDOW, digest_window)


class _BasisDigests:
    """B's digests as they stream in: filled by a reader thread, drained by the sender's main thread.

    A thread of this side does nothing but read, so the main thread never has to stop writing in
    order to listen -- which is what keeps streaming in both directions at once from deadlocking.
    That thread stops reading only while the window is full, and a full window means digests are
    sitting here for the main thread to consume, so it is never what the main thread is waiting for.
    What makes a full window safe is that consumption order is emission order: the generator emits
    each chunk `protocol.comparable` is true for exactly once, in ascending (entry, chunk) order, and
    the main thread walks the same list in the same order and takes exactly that sequence -- both
    sides decide which chunks those are with the same call on the same two sizes. A digest therefore
    always arrives above `_taken`, the highest key the main thread has consumed, which is why
    `_store` can refuse everything at or below it, and everything it is already holding, without ever
    refusing a conforming peer. `_held` counts only digests that are still to be taken, so a full
    window always means there is something there for the main thread to take.

    Everything the main thread does wait for -- a digest, the peer's RESULT, or the reader thread's
    death -- wakes it through this one condition, so no wait here can outlive the reader.
    """

    def __init__(self, channel: Channel, chunk_size: int, entries: list[Entry], basis_sizes: dict[int, int],
                 window: int):
        self._channel = channel
        self._chunk_size = chunk_size
        self._entries = entries
        self._basis_sizes = basis_sizes
        self._window = window
        self._ready = threading.Condition()
        self._digests: dict[tuple[int, int], bytes] = {}  # (file index, chunk index) -> B's digest
        self._held = 0          # bytes of digests received but not yet consumed
        self._taken = (-1, -1)  # the highest (entry, chunk) the main thread has consumed; see _store
        self._error: BaseException | None = None   # what the reader died of; every waiter re-raises it
        self._result: bytes | None = None          # the RESULT payload, read by the reader like any frame
        self._stopped = False
        self._thread = threading.Thread(target=self._read, name='pgdelta-basis-digests', daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _read(self) -> None:
        """Drain the receiver's direction until RESULT, an error, or `stop`. The only writer to
        `_error` and `_result`, and the only reader of the socket."""
        try:
            while True:
                with self._ready:
                    while self._held >= self._window and not self._stopped:
                        self._ready.wait()   # woken by take() freeing space, or by stop()
                    if self._stopped:
                        return
                kind, payload = self._channel.recv()
                if kind == BASIS_DIGESTS:
                    self._store(*unpack_digest_frame(payload))
                elif kind == RESULT:
                    with self._ready:
                        self._result = payload
                        self._ready.notify_all()
                    return
                elif kind == protocol.ERROR:
                    raise protocol.PeerError(json.loads(payload).get('message', 'unknown error'))
                else:
                    raise protocol.ProtocolError(
                        f'expected BASIS_DIGESTS, got {protocol.NAMES.get(kind, kind)}')
        except BaseException as exc:
            with self._ready:
                self._error = exc
                self._ready.notify_all()
            if not self._stopped:
                # The notify wakes a main thread waiting in take() or result(), but not one blocked in
                # a send the peer has stopped draining: hang up so that send fails and the main thread
                # reaches this error (the receiver's generator does the same on its side). This costs
                # the ERROR frame that would have told a misbehaving peer why; it cannot be sent from
                # here without interleaving into a frame the main thread may be half-way through.
                protocol.hang_up(self._channel.sock)

    def _store(self, index: int, first_chunk: int, digests: list[bytes]) -> None:
        size = self._basis_sizes.get(index)
        if size is None:
            raise protocol.ProtocolError(f'digests for entry {index}, which B never declared a basis for')
        last = first_chunk + len(digests) - 1
        # The run's *end* is what has to be inside what B declared, and `comparable` is the whole of
        # that test: it is true for exactly the chunks 0..k-1 this side compares, so the last chunk
        # of an ascending run bounds every chunk before it.
        if not comparable(last, self._chunk_size, self._entries[index].size, size):
            raise protocol.ProtocolError(
                f'chunk {last} of {self._entries[index].path} is outside the chunks this side '
                f'compares: S holds {self._entries[index].size} bytes and B declared {size}')
        with self._ready:
            # Then the two checks that together make "a frame was admitted and added nothing the
            # main thread will ever take" impossible -- the thing that would let a peer fill the
            # window with digests nobody can drain, park the reader on it, and leave a waiting
            # take() or result() with nothing to wake it.
            #
            # The watermark refuses every key at or below the highest one the main thread has taken,
            # so a digest already compared cannot come back, as an orphan or otherwise. It cannot
            # refuse a conforming frame, because consumption order is emission order: the generator
            # walks `entries` in HELLO order and, for each file, emits the chunks `comparable` is
            # true for in ascending order exactly once; the main thread walks the same list in the
            # same order and takes exactly those chunks, decided by the same `comparable` call on
            # the same two sizes (S's size from its own scan, B's from ACCEPT, which is where the
            # generator's came from). Both sides therefore handle one strictly ascending sequence of
            # keys, and a key can only be taken after it is stored, so when a key arrives the main
            # thread has taken only keys before it in that sequence: the watermark is below it. The
            # first chunk of a file clears it because every key of the previous file has a smaller
            # entry index; a file B holds only part of simply ends its run at the last comparable
            # chunk; a file B lacks puts nothing in the sequence on either side.
            #
            # The second check refuses a key still held, which the watermark cannot see: a repeat
            # landing before the main thread has popped it would overwrite the entry and add to
            # `_held` without adding anything to take. Between them a digest is stored once, and
            # only while it is still to come. Both are checked for the whole frame before any of it
            # is stored, as the bound above is, so a refused frame leaves nothing behind.
            if (index, first_chunk) <= self._taken:
                raise protocol.ProtocolError(
                    f'a digest for {self._entries[index].path} chunk {first_chunk}, which this side '
                    f'has already compared: the last digest it took was entry {self._taken[0]}, '
                    f'chunk {self._taken[1]}')
            for offset in range(len(digests)):
                if (index, first_chunk + offset) in self._digests:
                    raise protocol.ProtocolError(
                        f'a second digest for {self._entries[index].path} '
                        f'chunk {first_chunk + offset}')
            for offset, digest in enumerate(digests):
                self._digests[(index, first_chunk + offset)] = digest
                self._held += len(digest)
            self._ready.notify_all()

    def take(self, index: int, chunk: int, stats: Stats) -> bytes:
        """B's digest for this chunk, waiting for it only if it has not arrived yet. Counts one
        `digest_waits` and one `round_trips` when it really does block on the peer."""
        key = (index, chunk)
        with self._ready:
            if key not in self._digests:
                stats.counts['digest_waits'] += 1
                stats.counts['round_trips'] += 1
                with stats.timed('wait_for_digests'):
                    while key not in self._digests:
                        self._fail_if_finished(index, chunk)
                        self._ready.wait()
            digest = self._digests.pop(key)
            self._taken = key   # ascending by construction: this is the walk's own order
            was_full = self._held >= self._window
            self._held -= len(digest)
            if was_full:
                self._ready.notify_all()   # the reader can start draining the socket again
            return digest

    def result(self, stats: Stats) -> bytes:
        """The receiver's RESULT payload, which the reader thread hands over like any other frame."""
        with self._ready:
            stats.counts['round_trips'] += 1
            while self._result is None:
                if self._error is not None:
                    raise self._error
                # Every file has been compared by the time this is called, so the sequence _store
                # describes is exhausted and nothing will call take() again. A digest still sitting
                # here is therefore one nothing can drain, and it is holding window space the reader
                # needs to reach the RESULT this is waiting for. This is the one wait the ordering
                # invariant does not cover by itself, so it consults the table the way
                # _fail_if_finished does rather than waiting on a reader that cannot come back.
                if self._digests:
                    raise protocol.ProtocolError(
                        f'{len(self._digests)} basis digests are still here with every file '
                        f'compared: nothing will take them, so the window cannot drain')
                self._ready.wait()
            return self._result

    def error(self) -> BaseException | None:
        """What the reader thread died of, if it has died yet. Read by the main thread's failure
        path, where the peer's own reason has to beat a broken `send` on this side."""
        with self._ready:
            return self._error

    def stop(self) -> None:
        """Finish the reader thread, from any exit path. It is either already done (it returned on
        RESULT or on an error), waiting for window space -- which `_stopped` ends -- or blocked in
        `recv`, which `shutdown(SHUT_RD)` ends by making that `recv` return nothing."""
        with self._ready:
            self._stopped = True
            self._ready.notify_all()
        if self._thread.is_alive():
            try:
                self._channel.sock.shutdown(socket.SHUT_RD)
            except OSError:
                pass
        self._thread.join()

    def _fail_if_finished(self, index: int, chunk: int) -> None:
        """Raise if no digest for this chunk can ever arrive, so a waiter never waits forever.
        Called with the condition held, before every wait."""
        path = self._entries[index].path
        if self._digests:
            # The generator emits the comparable chunks in order and this thread consumes exactly
            # that sequence in order, so whenever a digest is missing the table is provably empty:
            # nothing at or after this chunk has arrived, and everything before it was popped. A
            # digest for a later chunk therefore means the peer is not sending in order, and
            # waiting for this one would let it fill the window with digests this thread cannot
            # reach yet -- the reader stops draining, and both threads wait for ever.
            raise protocol.ProtocolError(
                f'no digest for {path} chunk {chunk}, but digests for later chunks arrived')
        if self._error is not None:
            exc = self._error
            message = f'no basis digest for {path} chunk {chunk}: {exc}'
            if isinstance(exc, (protocol.Closed, protocol.PeerError, protocol.ProtocolError)):
                raise type(exc)(message) from exc
            raise exc
        if self._result is not None:
            raise protocol.ProtocolError(
                f'the receiver finished without sending a basis digest for {path} chunk {chunk}')


def run_sender(channel: Channel, source: Path, chunk_size: int = DEFAULT_CHUNK_SIZE, events: EventLog | None = None,
               digest_window: int = DEFAULT_DIGEST_WINDOW) -> dict:
    """Send S to the receiver at the other end of `channel`. Returns this side's report, with the receiver's.

    `digest_window` bounds the bytes of B's digests held here but not yet compared; see `_BasisDigests`.
    `events`, if given, gets a start event, progress about every 0.1 s, and a done event (pgdelta.events).
    Progress is emitted from this thread only: the event log is single-writer, and an interleaved
    write would corrupt the JSON lines `python -m viz` reads."""
    events = events or EventLog(None)
    if not protocol.MIN_CHUNK_SIZE <= chunk_size <= protocol.MAX_CHUNK_SIZE:
        raise ValueError(
            f'chunk size must be between {protocol.MIN_CHUNK_SIZE} and {protocol.MAX_CHUNK_SIZE} bytes')
    if digest_window < 1:
        raise ValueError('digest_window must be at least 1')
    requested_window, digest_window = digest_window, effective_digest_window(digest_window)
    if digest_window != requested_window:
        # Said once, out loud: every report of this run carries the floored value, and without this
        # line nothing would show that it is not the value the caller asked for.
        print(f'pgdelta: --digest-window {requested_window} is below one BASIS_DIGESTS frame; '
              f'using {digest_window}', file=sys.stderr, flush=True)
    source = Path(source)
    stats = Stats('sender')
    digests = None
    current = None  # the file being sent, to name in an error
    try:
        with stats.timed('scan'):
            entries = scan(source)
        key = new_key()
        hasher = Hasher(key)
        channel.send_json(HELLO, {'version': protocol.VERSION, 'chunk_size': chunk_size, 'key': key.hex(),
                                  'root_mode': stat.S_IMODE(source.stat().st_mode),
                                  'entries': [entry.to_json() for entry in entries]})
        accept = json.loads(channel.expect(protocol.ACCEPT))
        stats.counts['round_trips'] += 1
        basis_sizes = _basis_sizes(accept, entries)
        digests = _BasisDigests(channel, chunk_size, entries, basis_sizes, digest_window)
        digests.start()
        events.emit('start', total_bytes=sum(e.size for e in entries if e.kind == 'file'),
                    files=sum(e.kind == 'file' for e in entries), chunk_size=chunk_size)

        def progress(path: str) -> None:
            c = stats.counts
            events.emit('progress', bytes_read=c['bytes_read'], bytes_sent=sum(channel.sent.values()),
                        chunks_same=c['chunks_same'], chunks_changed=c['chunks_changed'],
                        chunks_new=c['chunks_new'], round_trips=c['round_trips'], file=path)

        for index, entry in enumerate(entries):
            if entry.kind != 'file':
                continue
            current = entry.path
            stats.counts['files'] += 1
            basis_size = basis_sizes.get(index)  # None: B has no such file
            whole = hasher.file()
            offset = chunk = 0
            run_first = run_length = 0  # a pending CHUNK_COPY: chunks B already holds, not yet named

            def flush_run() -> None:
                # Closes over `index`, `run_first` and `run_length`, all bound fresh each time around
                # the outer `for` loop; every call below happens within that same iteration, so
                # there's no late-binding hazard (the closure never outlives the file it was made for).
                nonlocal run_length
                if not run_length:
                    return
                with stats.timed('send'):
                    channel.send(CHUNK_COPY, CHUNK_COPY_BODY.pack(index, run_first, run_length))
                stats.counts['copy_runs'] += 1
                run_length = 0

            with open(source / entry.path, 'rb') as f:
                while offset < entry.size:
                    want = min(chunk_size, entry.size - offset)
                    with stats.timed('read'):
                        data = f.read(want)
                    if len(data) != want:
                        raise RuntimeError(f'{entry.path} shrank while it was read; S must not change')
                    whole.update(data)
                    stats.counts['bytes_read'] += want
                    if basis_size is not None and comparable(chunk, chunk_size, entry.size, basis_size):
                        with stats.timed('hash'):
                            digest = hasher.chunk(data)
                        theirs = digests.take(index, chunk, stats)
                        stats.counts['chunks_asked'] += 1
                        if theirs == digest:
                            # Extend the pending run. Every break below flushes it first, so a
                            # pending run is always the chunks immediately before this one.
                            stats.counts['chunks_same'] += 1
                            if not run_length:
                                run_first = chunk
                            run_length += 1
                        else:
                            flush_run()
                            stats.counts['chunks_changed'] += 1
                            with stats.timed('send'):
                                channel.send(CHUNK_DATA, CHUNK_DATA_HEAD.pack(index, chunk) + data)
                    else:
                        # B lacks this range (a new file, or past B's end): flush first so chunk
                        # indices on the wire stay ascending, then send it; nothing to compare.
                        flush_run()
                        stats.counts['chunks_new'] += 1
                        with stats.timed('send'):
                            channel.send(CHUNK_DATA, CHUNK_DATA_HEAD.pack(index, chunk) + data)
                    offset += want
                    chunk += 1
                    if events.due():
                        progress(entry.path)
                if f.read(1):
                    raise RuntimeError(f'{entry.path} grew while it was read; S must not change')
            flush_run()  # a run never spans two files
            with stats.timed('send'):
                channel.send(FILE_DONE, FILE_DONE_BODY.pack(index, entry.size, whole.digest()))

        channel.send(COMPLETE)
        result = json.loads(digests.result(stats))
    except (protocol.PeerError, protocol.Closed):
        raise
    except BaseException as exc:
        # If the receiver failed, said why, and its caller closed the socket, this thread's next
        # `send` breaks and its next `take()` raises the peer's reason -- a race between the two.
        # The reader thread has that reason either way, so report the cause rather than whichever
        # symptom won, and say nothing back to a peer that has already reported.
        reported = digests.error() if digests is not None else None
        cause = exc
        if (isinstance(reported, (protocol.PeerError, protocol.ProtocolError))
                and not isinstance(exc, (protocol.PeerError, protocol.ProtocolError))):
            cause = reported
        elif isinstance(reported, protocol.Closed) and isinstance(exc, OSError):
            # The peer closed its direction, the reader hung up, and that broke a send here: the
            # closed connection is the cause, the broken pipe its symptom.
            cause = protocol.Closed(f'{reported} while sending {current}' if current else str(reported))
        if not isinstance(reported, protocol.PeerError):
            try:
                channel.send_json(protocol.ERROR, {'message': f'sender failed: {cause}'})
            except OSError:
                pass
        if cause is exc:
            raise
        raise cause from exc
    finally:
        if digests is not None:
            digests.stop()
    report = stats.report(channel)
    report['ok'] = bool(result.get('ok'))
    report['chunk_size'] = chunk_size
    # The *effective* window, after flooring: a sweep that reports the value it asked for would
    # label two runs at MIN_DIGEST_WINDOW with two different windows.
    report['digest_window'] = digest_window
    report['receiver'] = result.get('stats')
    c = stats.counts
    events.emit('done', ok=report['ok'], elapsed=report['elapsed_seconds'], bytes_read=c['bytes_read'],
                bytes_sent=report['bytes_sent_total'], chunks_same=c['chunks_same'],
                chunks_changed=c['chunks_changed'], chunks_new=c['chunks_new'], round_trips=c['round_trips'])
    return report


def _basis_sizes(accept: dict, entries: list[Entry]) -> dict[int, int]:
    """ACCEPT's declaration of what B holds, checked against what this side actually sent: the
    indices are peer-supplied, and everything downstream uses them to index `entries`."""
    sizes = {}
    for pair in accept['basis_sizes']:
        index, size = int(pair[0]), int(pair[1])
        if not 0 <= index < len(entries) or entries[index].kind != 'file':
            raise protocol.ProtocolError(f'ACCEPT declares a basis for entry {index}, which is not a file S sent')
        if size < 0:
            raise protocol.ProtocolError(f'ACCEPT declares a basis of {size} bytes for {entries[index].path}')
        sizes[index] = size
    return sizes
