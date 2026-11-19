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
MIN_CHUNK_SIZE, MAX_CHUNK_SIZE = 512, 16 << 20

# Bytes of undelivered digests the reader thread will hold before it stops draining the socket, which
# lets TCP backpressure stall the receiver's generator rather than growing this table without bound.
# 64 MiB is 4,194,304 digests, which is about 32 GiB of basis at an 8 KiB chunk.
DEFAULT_DIGEST_WINDOW = 64 << 20


class _BasisDigests:
    """B's digests as they stream in: filled by a reader thread, drained by the sender's main thread.

    A thread of this side does nothing but read, so the main thread never has to stop writing in
    order to listen -- which is what keeps streaming in both directions at once from deadlocking.
    That thread stops reading only while the window is full, and a full window means digests are
    sitting here for the main thread to consume, so it is never what the main thread is waiting for.

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

    def _store(self, index: int, first_chunk: int, digests: list[bytes]) -> None:
        size = self._basis_sizes.get(index)
        if size is None:
            raise protocol.ProtocolError(f'digests for entry {index}, which B never declared a basis for')
        last = first_chunk + len(digests) - 1
        if last * self._chunk_size >= size:
            raise protocol.ProtocolError(
                f'chunk {last} of {self._entries[index].path} is outside the {size} bytes B declared')
        with self._ready:
            for offset, digest in enumerate(digests):
                chunk = first_chunk + offset
                # Only digests the main thread will consume are kept, so the window always drains:
                # a conforming peer sends no others, and a peer that does cannot stall this side by
                # filling the window with digests nobody asks for.
                if comparable(chunk, self._chunk_size, self._entries[index].size, size):
                    self._digests[(index, chunk)] = digest
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
                self._ready.wait()
            return self._result

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
    if not MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE:
        raise ValueError(f'chunk size must be between {MIN_CHUNK_SIZE} and {MAX_CHUNK_SIZE} bytes')
    if digest_window < 1:
        raise ValueError('digest_window must be at least 1')
    source = Path(source)
    stats = Stats('sender')
    digests = None
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
        try:
            channel.send_json(protocol.ERROR, {'message': f'sender failed: {exc}'})
        except OSError:
            pass
        raise
    finally:
        if digests is not None:
            digests.stop()
    report = stats.report(channel)
    report['ok'] = bool(result.get('ok'))
    report['chunk_size'] = chunk_size
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
