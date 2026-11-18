"""The source side (S): walk S, and ask the receiver about a batch of chunks at a time.

One thread does everything, in order. For each file, chunks that B might already have are accumulated
and asked about together, as one CHUNK_HASHES message; the receiver answers with one VERDICTS bitmap,
and only the chunks that differ get sent. A chunk B cannot have (a new file, or past B's end) is sent
unasked, after the pending batch is flushed so chunk indices stay ascending on the wire.

While this side waits for a batch's verdicts, nothing else happens here, and while the receiver reads
and hashes B's chunks, the network is idle. The roadmap issues remove these waits one at a time.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

from pgdelta import protocol
from pgdelta.hashing import Hasher, new_key
from pgdelta.protocol import (CHUNK_DATA, CHUNK_DATA_HEAD, CHUNK_HASHES, CHUNK_HASHES_HEAD, COMPLETE, FILE_DONE,
                              FILE_DONE_BODY, HELLO, RESULT, VERDICTS, Channel, unpack_verdicts)
from pgdelta.events import EventLog
from pgdelta.stats import Stats
from pgdelta.tree import scan

DEFAULT_CHUNK_SIZE = 8192  # one PostgreSQL page
MIN_CHUNK_SIZE, MAX_CHUNK_SIZE = 512, 16 << 20
DEFAULT_BATCH = 128
DEFAULT_BATCH_BYTES = 32 << 20

# A CHUNK_HASHES frame holds one header plus `count` digests; capping the chunk count at what fits
# in MAX_PAYLOAD keeps an oversized --batch-bytes from building a frame Channel.send will refuse.
_MAX_BATCH_FOR_PAYLOAD = (protocol.MAX_PAYLOAD - CHUNK_HASHES_HEAD.size) // protocol.DIGEST_SIZE


def effective_batch(batch: int, batch_bytes: int, chunk_size: int) -> int:
    """How many chunks to accumulate before asking: the smallest of the count cap, the byte cap
    (the accumulator holds each chunk's data, so `batch_bytes` bounds memory), and the payload cap
    (a CHUNK_HASHES frame of that many digests must still fit under protocol.MAX_PAYLOAD). Never zero."""
    return max(1, min(batch, batch_bytes // chunk_size, _MAX_BATCH_FOR_PAYLOAD))


def run_sender(channel: Channel, source: Path, chunk_size: int = DEFAULT_CHUNK_SIZE, events: EventLog | None = None,
               batch: int = DEFAULT_BATCH, batch_bytes: int = DEFAULT_BATCH_BYTES) -> dict:
    """Send S to the receiver at the other end of `channel`. Returns this side's report, with the receiver's.

    `batch` chunks are asked about per CHUNK_HASHES/VERDICTS round trip, at most (never spanning files),
    further capped by `batch_bytes` (the accumulator's memory bound, since it holds each chunk's data)
    and by what fits in one CHUNK_HASHES frame; see `effective_batch`.
    `events`, if given, gets a start event, progress about every 0.1 s, and a done event (pgdelta.events)."""
    events = events or EventLog(None)
    if not MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE:
        raise ValueError(f'chunk size must be between {MIN_CHUNK_SIZE} and {MAX_CHUNK_SIZE} bytes')
    if batch < 1:
        raise ValueError('batch must be at least 1')
    if batch_bytes < 1:
        raise ValueError('batch_bytes must be at least 1')
    batch = effective_batch(batch, batch_bytes, chunk_size)
    source = Path(source)
    stats = Stats('sender')
    try:
        with stats.timed('scan'):
            entries = scan(source)
        key = new_key()
        hasher = Hasher(key)
        channel.send_json(HELLO, {'version': protocol.VERSION, 'chunk_size': chunk_size, 'key': key.hex(),
                                  'root_mode': stat.S_IMODE(source.stat().st_mode),
                                  'entries': [entry.to_json() for entry in entries]})
        accept = json.loads(channel.expect(protocol.ACCEPT))
        basis_sizes = {int(index): int(size) for index, size in accept['basis_sizes']}
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
            asking = []  # (chunk index, digest, data) for this file, accumulated but not yet asked about

            def flush() -> None:
                if not asking:
                    return
                head = CHUNK_HASHES_HEAD.pack(index, asking[0][0], len(asking))
                with stats.timed('send'):
                    channel.send(CHUNK_HASHES, head + b''.join(digest for _, digest, _ in asking))
                with stats.timed('wait_for_verdict'):
                    differs = unpack_verdicts(channel.expect(VERDICTS), len(asking))
                stats.counts['round_trips'] += 1
                stats.counts['chunks_asked'] += len(asking)
                for (c_index, _, c_data), differed in zip(asking, differs):
                    if differed:
                        stats.counts['chunks_changed'] += 1
                        with stats.timed('send'):
                            channel.send(CHUNK_DATA, CHUNK_DATA_HEAD.pack(index, c_index) + c_data)
                    else:
                        stats.counts['chunks_same'] += 1
                asking.clear()

            with open(source / entry.path, 'rb') as f:
                while offset < entry.size:
                    want = min(chunk_size, entry.size - offset)
                    with stats.timed('read'):
                        data = f.read(want)
                    if len(data) != want:
                        raise RuntimeError(f'{entry.path} shrank while it was read; S must not change')
                    whole.update(data)
                    stats.counts['bytes_read'] += want
                    if basis_size is not None and offset + want <= basis_size:
                        # B has this whole range: accumulate the question, asking once the batch is full.
                        with stats.timed('hash'):
                            digest = hasher.chunk(data)
                        asking.append((chunk, digest, data))
                        if len(asking) >= batch:
                            flush()
                    else:
                        # B lacks this range (a new file, or past B's end): flush first so chunk indices
                        # on the wire stay ascending, then send it; nothing to compare.
                        flush()
                        stats.counts['chunks_new'] += 1
                        with stats.timed('send'):
                            channel.send(CHUNK_DATA, CHUNK_DATA_HEAD.pack(index, chunk) + data)
                    offset += want
                    chunk += 1
                    if events.due():
                        progress(entry.path)
                if f.read(1):
                    raise RuntimeError(f'{entry.path} grew while it was read; S must not change')
            flush()
            with stats.timed('send'):
                channel.send(FILE_DONE, FILE_DONE_BODY.pack(index, entry.size, whole.digest()))

        channel.send(COMPLETE)
        result = json.loads(channel.expect(RESULT))
    except (protocol.PeerError, protocol.Closed):
        raise
    except BaseException as exc:
        try:
            channel.send_json(protocol.ERROR, {'message': f'sender failed: {exc}'})
        except OSError:
            pass
        raise
    report = stats.report(channel)
    report['ok'] = bool(result.get('ok'))
    report['chunk_size'] = chunk_size
    report['batch'] = batch
    report['receiver'] = result.get('stats')
    c = stats.counts
    events.emit('done', ok=report['ok'], elapsed=report['elapsed_seconds'], bytes_read=c['bytes_read'],
                bytes_sent=report['bytes_sent_total'], chunks_same=c['chunks_same'],
                chunks_changed=c['chunks_changed'], chunks_new=c['chunks_new'], round_trips=c['round_trips'])
    return report
