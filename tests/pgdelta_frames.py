"""Every frame of one fixed pgdelta session, recorded on the sender's channel, and a fingerprint of them.

The wire check for the window switch and the protocol timers (EXP-13): the
same session, with the same key, data and modes, must put the same bytes on
the wire before and after them. This module uses only what pgdelta had
before EXP-13 (run_sender, run_receiver, protocol.Channel), so it can also
fingerprint an older checkout: `python tests/pgdelta_frames.py` prints the
fingerprint of whichever pgdelta is first on sys.path.

What is compared:
- source to dest: the whole byte stream, in order (the sender's main thread
  writes every frame, so the order is fixed);
- dest to source: each message type's frames in order. The receiver's basis
  thread sends BASIS_NODES while its main loop sends LEAF_DIGESTS, so how
  the two interleave was never fixed;
- the receiver's final COMPLETE carries its statistics, timings included,
  so no two runs ever sent the same bytes there. It is compared without
  its timings and paths, and without the statistics EXP-13 added
  (ADDED_RECEIVER_KEYS), and its keys are listed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import random
import sys
import tempfile
import threading

from pgdelta import protocol
from pgdelta.protocol import HEADER, Channel
from pgdelta.receiver import run_receiver
from pgdelta.sender import run_sender

PAGE = 8192
NODE_BLOCKS = 4
KEY = bytes(range(32))
# Statistics the receiver's COMPLETE gained in EXP-13 (pgdelta.waits); left out of its comparison.
ADDED_RECEIVER_KEYS = frozenset(('protocol_timers',))


class Recording(Channel):
    """A Channel that keeps every frame it sends and receives, as (type, payload bytes)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.frames_sent, self.frames_received = [], []

    def send(self, code, *parts):
        super().send(code, *parts)
        self.frames_sent.append((code, b''.join(bytes(part) for part in parts)))

    def recv(self):
        message = super().recv()
        if message is not None:
            self.frames_received.append((message[0], bytes(message[1])))
        return message


def _pages(rng, count):
    return rng.randbytes(count * PAGE)


def _flip(data, *pages):
    data = bytearray(data)
    for page in pages:
        data[page * PAGE + 11] ^= 0x5A
    return bytes(data)


def build(root):
    """S and B under root, with fixed contents and modes: more files with a basis than any window run."""
    rng = random.Random(1301)
    source, basis = {}, {}
    for number in range(24):
        old = _pages(rng, 6 + number % 9)
        changed = [page for page in range(len(old) // PAGE) if (page * 7 + number) % 5 == 0]
        source[f'base/5/{16384 + number}'] = _flip(old, *changed) if number % 4 else old
        basis[f'base/5/{16384 + number}'] = old
    old = _pages(rng, 40)
    source['base/5/20000'] = _flip(old, 3, 17, 18, 39) + rng.randbytes(100)  # longer than B's
    basis['base/5/20000'] = old
    source['base/5/20001'] = _pages(rng, 5)  # new
    source['base/5/20002'] = b''
    source['global/pg_control'] = _flip(_pages(rng, 1), 0)
    basis['global/pg_control'] = _flip(source['global/pg_control'], 0)
    source['PG_VERSION'] = basis['PG_VERSION'] = b'17\n'
    basis['base/5/stale'] = _pages(rng, 2)
    trees = {}
    for name, files, directories in (('S', source, ('pg_wal/archive_status',)), ('B', basis, ())):
        tree = Path(root) / name
        for path in directories:
            (tree / path).mkdir(parents=True, exist_ok=True)
        for path, data in files.items():
            (tree / path).parent.mkdir(parents=True, exist_ok=True)
            (tree / path).write_bytes(data)
        for current, subdirectories, names in os.walk(tree):
            for entry in subdirectories:
                os.chmod(Path(current) / entry, 0o700)
            for entry in names:
                os.chmod(Path(current) / entry, 0o600)
        os.chmod(tree, 0o700)
        trees[name] = tree
    return trees['S'], trees['B']


def record(source, basis, output, **sender_kwargs):
    """Run one session in this process over pipes; return (sender stats, receiver stats, the sender's channel)."""
    to_receiver_r, to_receiver_w = os.pipe()
    to_sender_r, to_sender_w = os.pipe()
    sender = Recording(os.fdopen(to_sender_r, 'rb'), os.fdopen(to_receiver_w, 'wb'))
    receiver = Channel(os.fdopen(to_receiver_r, 'rb'), os.fdopen(to_sender_w, 'wb'))
    result = {}

    def receive():
        try:
            result['receiver'] = run_receiver(receiver, basis, output, reuse_any_basis=True)
        except BaseException as exc:
            result['receiver_error'] = exc
        finally:
            receiver.close()

    thread = threading.Thread(target=receive, name='frames-receiver')
    thread.start()
    try:
        stats = run_sender(sender, source, node_blocks=NODE_BLOCKS, key=KEY, **sender_kwargs)
    finally:
        sender.close()
        thread.join(60)
    if 'receiver_error' in result:
        raise result['receiver_error']
    return stats, result['receiver'], sender


def _wire(frames):
    digest = hashlib.sha256()
    for code, payload in frames:
        digest.update(HEADER.pack(len(payload), code))
        digest.update(payload)
    return digest.hexdigest()


def comparable_receiver(stats):
    """The receiver's statistics without timings, paths and what EXP-13 added."""
    return {key: value for key, value in stats.items()
            if not key.endswith('_seconds') and key not in ('output', 'receipt') and key not in ADDED_RECEIVER_KEYS}


def fingerprint(channel):
    """What a session put on the wire, from the sender's Recording channel (see the module docstring)."""
    up = channel.frames_sent
    down = [(code, payload) for code, payload in channel.frames_received if code != protocol.COMPLETE]
    final = [protocol.decode_json(payload, 'COMPLETE') for code, payload in channel.frames_received
             if code == protocol.COMPLETE]
    if len(final) != 1:
        raise AssertionError(f'expected one COMPLETE from the receiver, got {len(final)}')
    receiver = final[0]['receiver']
    return {
        'source_to_dest': _wire(up),
        'source_to_dest_frames': {protocol.name(code): sum(1 for c, _ in up if c == code)
                                  for code in sorted({code for code, _ in up})},
        'dest_to_source': {protocol.name(code): _wire([frame for frame in down if frame[0] == code])
                           for code in sorted({code for code, _ in down})},
        'dest_to_source_frames': {protocol.name(code): sum(1 for c, _ in down if c == code)
                                  for code in sorted({code for code, _ in down})},
        'complete_published': final[0]['published'],
        'complete_receiver': hashlib.sha256(json.dumps(comparable_receiver(receiver), sort_keys=True)
                                            .encode('utf-8')).hexdigest(),
        'complete_receiver_keys': sorted(set(receiver) - ADDED_RECEIVER_KEYS),
    }


def session_fingerprint(**sender_kwargs):
    with tempfile.TemporaryDirectory() as temp:
        source, basis = build(temp)
        _, _, channel = record(source, basis, Path(temp) / 'O', **sender_kwargs)
        return fingerprint(channel)


if __name__ == '__main__':
    window = int(sys.argv[1]) if len(sys.argv) > 1 else None
    print(json.dumps(session_fingerprint(**({} if window is None else {'window': window})), indent=1,
                     sort_keys=True))
