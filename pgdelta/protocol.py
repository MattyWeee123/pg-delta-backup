"""The wire format: framed messages over one byte stream, with bytes counted per message type.

Every frame is a 5-byte header (payload length as a 4-byte big-endian integer, then a 1-byte type)
followed by the payload.
"""
from __future__ import annotations

import json
import socket
import struct
from collections import Counter

VERSION = 1
HEADER = struct.Struct('>IB')
MAX_PAYLOAD = 64 << 20

# Message types, in the order a session uses them.
HELLO = 1        # sender -> receiver: JSON {version, chunk_size, key, entries}
ACCEPT = 2       # receiver -> sender: JSON {basis_sizes: [[file index, size], ...]}
CHUNK_HASH = 3   # sender -> receiver: file index, chunk index, digest of S's chunk
VERDICT = 4      # receiver -> sender: 0 = same as B's chunk, 1 = different
CHUNK_DATA = 5   # sender -> receiver: file index, chunk index, the chunk's bytes
FILE_DONE = 6    # sender -> receiver: file index, size, digest of S's whole file
COMPLETE = 7     # sender -> receiver: every file has been sent
RESULT = 8       # receiver -> sender: JSON {ok, stats}
ERROR = 9        # either way: JSON {message}

NAMES = {HELLO: 'HELLO', ACCEPT: 'ACCEPT', CHUNK_HASH: 'CHUNK_HASH', VERDICT: 'VERDICT',
         CHUNK_DATA: 'CHUNK_DATA', FILE_DONE: 'FILE_DONE', COMPLETE: 'COMPLETE', RESULT: 'RESULT',
         ERROR: 'ERROR'}

SAME, DIFFERENT = 0, 1

CHUNK_HASH_BODY = struct.Struct('>IQ16s')
VERDICT_BODY = struct.Struct('>B')
CHUNK_DATA_HEAD = struct.Struct('>IQ')
FILE_DONE_BODY = struct.Struct('>IQ16s')


class ProtocolError(Exception):
    """The peer sent something this version doesn't expect."""


class PeerError(Exception):
    """The peer reported an error (an ERROR frame)."""


class Closed(Exception):
    """The stream ended where a frame was expected."""


class Channel:
    """One framed connection. Counts the bytes sent and received per message type."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.sent = Counter()
        self.received = Counter()

    def send(self, kind: int, payload: bytes = b'') -> None:
        if len(payload) > MAX_PAYLOAD:
            raise ProtocolError(f'{NAMES.get(kind, kind)} payload of {len(payload)} bytes is too large')
        self.sock.sendall(HEADER.pack(len(payload), kind) + payload)
        self.sent[NAMES.get(kind, str(kind))] += HEADER.size + len(payload)

    def send_json(self, kind: int, value) -> None:
        self.send(kind, json.dumps(value, separators=(',', ':')).encode())

    def recv(self) -> tuple[int, bytes]:
        length, kind = HEADER.unpack(self._exact(HEADER.size))
        if length > MAX_PAYLOAD:
            raise ProtocolError(f'frame of {length} bytes is too large')
        payload = self._exact(length)
        self.received[NAMES.get(kind, str(kind))] += HEADER.size + length
        return kind, payload

    def expect(self, kind: int) -> bytes:
        """The next frame's payload, which must be of type `kind`. An ERROR frame raises PeerError."""
        got, payload = self.recv()
        if got == ERROR:
            raise PeerError(json.loads(payload).get('message', 'unknown error'))
        if got != kind:
            raise ProtocolError(f'expected {NAMES[kind]}, got {NAMES.get(got, got)}')
        return payload

    def _exact(self, n: int) -> bytes:
        parts, left = [], n
        while left:
            part = self.sock.recv(min(left, 1 << 20))
            if not part:
                raise Closed('the peer closed the connection')
            parts.append(part)
            left -= len(part)
        return b''.join(parts)


def tune(sock: socket.socket) -> socket.socket:
    """TCP_NODELAY on: stop and wait sends many small frames, and Nagle's algorithm would hold each one back."""
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock
