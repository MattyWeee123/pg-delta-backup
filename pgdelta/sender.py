"""The source side (S): walk S, and for each chunk ask the receiver whether B already has it.

Naive on purpose ("stop and wait", the version the team's mentor sketched on October 30):
- one thread does everything, in order;
- for each chunk of each file, it hashes the chunk, sends the hash, and waits for the receiver's
  verdict before it does anything else;
- if the verdict is "different", it sends the chunk.

While it waits, nothing else happens on this side, and while the receiver reads and hashes B's chunk,
the network is idle. The roadmap issues remove these waits one at a time.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

from pgdelta import protocol
from pgdelta.hashing import Hasher, new_key
from pgdelta.protocol import (CHUNK_DATA, CHUNK_DATA_HEAD, CHUNK_HASH, CHUNK_HASH_BODY, COMPLETE, FILE_DONE,
                              FILE_DONE_BODY, HELLO, RESULT, SAME, VERDICT, VERDICT_BODY, Channel)
from pgdelta.stats import Stats
from pgdelta.tree import scan

DEFAULT_CHUNK_SIZE = 8192  # one PostgreSQL page
MIN_CHUNK_SIZE, MAX_CHUNK_SIZE = 512, 16 << 20


def run_sender(channel: Channel, source: Path, chunk_size: int = DEFAULT_CHUNK_SIZE) -> dict:
    """Send S to the receiver at the other end of `channel`. Returns this side's report, with the receiver's."""
    if not MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE:
        raise ValueError(f'chunk size must be between {MIN_CHUNK_SIZE} and {MAX_CHUNK_SIZE} bytes')
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

        for index, entry in enumerate(entries):
            if entry.kind != 'file':
                continue
            stats.counts['files'] += 1
            basis_size = basis_sizes.get(index)  # None: B has no such file
            whole = hasher.file()
            offset = chunk = 0
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
                        # B has this whole range: ask whether it's the same, and wait for the answer.
                        with stats.timed('hash'):
                            digest = hasher.chunk(data)
                        with stats.timed('send'):
                            channel.send(CHUNK_HASH, CHUNK_HASH_BODY.pack(index, chunk, digest))
                        with stats.timed('wait_for_verdict'):
                            (verdict,) = VERDICT_BODY.unpack(channel.expect(VERDICT))
                        stats.counts['round_trips'] += 1
                        if verdict == SAME:
                            stats.counts['chunks_same'] += 1
                        else:
                            stats.counts['chunks_changed'] += 1
                            with stats.timed('send'):
                                channel.send(CHUNK_DATA, CHUNK_DATA_HEAD.pack(index, chunk) + data)
                    else:
                        # B lacks this range (a new file, or past B's end): nothing to compare, send it.
                        stats.counts['chunks_new'] += 1
                        with stats.timed('send'):
                            channel.send(CHUNK_DATA, CHUNK_DATA_HEAD.pack(index, chunk) + data)
                    offset += want
                    chunk += 1
                if f.read(1):
                    raise RuntimeError(f'{entry.path} grew while it was read; S must not change')
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
    report['receiver'] = result.get('stats')
    return report
