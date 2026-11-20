"""The wire format: framed messages over one byte stream, with bytes counted per message type.

Every frame is a 5-byte header (payload length as a 4-byte big-endian integer, then a 1-byte type)
followed by the payload.
"""
from __future__ import annotations

import json
import socket
import struct
from collections import Counter

from pgdelta.hashing import DIGEST_SIZE

VERSION = 3
HEADER = struct.Struct('>IB')
MAX_PAYLOAD = 64 << 20

# Message types, in the order a session uses them. 3, 4, 10 and 11 are retired: the per-chunk
# CHUNK_HASH/VERDICT, then the batched CHUNK_HASHES/VERDICTS. Nothing is asked for any more --
# the receiver volunteers B's digests and the sender decides on its own side.
HELLO = 1        # sender -> receiver: JSON {version, chunk_size, key, entries}
ACCEPT = 2       # receiver -> sender: JSON {basis_sizes: [[file index, size], ...]}
CHUNK_DATA = 5   # sender -> receiver: file index, chunk index, the chunk's bytes
FILE_DONE = 6    # sender -> receiver: file index, size, digest of S's whole file
COMPLETE = 7     # sender -> receiver: every file has been sent
RESULT = 8       # receiver -> sender: JSON {ok, stats}
ERROR = 9        # either way: JSON {message}
BASIS_DIGESTS = 12  # receiver -> sender: unasked, file index, first chunk index, count, B's digests
CHUNK_COPY = 13      # sender -> receiver: file index, first chunk index, count, a run-length copy instruction

NAMES = {HELLO: 'HELLO', ACCEPT: 'ACCEPT',
         CHUNK_DATA: 'CHUNK_DATA', FILE_DONE: 'FILE_DONE', COMPLETE: 'COMPLETE', RESULT: 'RESULT',
         ERROR: 'ERROR', BASIS_DIGESTS: 'BASIS_DIGESTS', CHUNK_COPY: 'CHUNK_COPY'}

CHUNK_DATA_HEAD = struct.Struct('>IQ')
FILE_DONE_BODY = struct.Struct('>IQ16s')
BASIS_DIGESTS_HEAD = struct.Struct('>IQI')
CHUNK_COPY_BODY = struct.Struct('>IQI')

# The chunk size both sides agree on, which arrives in HELLO and so is peer-supplied. It lives here
# rather than in sender.py because the receiver has to check it before it starts hashing B, and must
# not import the sender to do so.
MIN_CHUNK_SIZE, MAX_CHUNK_SIZE = 512, 16 << 20

# How many digests the receiver's generator puts in one BASIS_DIGESTS frame.
DIGESTS_PER_FRAME = 1024
# The most digests a BASIS_DIGESTS payload can hold without exceeding MAX_PAYLOAD.
MAX_DIGESTS_PER_FRAME = (MAX_PAYLOAD - BASIS_DIGESTS_HEAD.size) // DIGEST_SIZE


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


def comparable(chunk: int, chunk_size: int, source_size: int, basis_size: int) -> bool:
    """Whether this chunk of S can be compared against B: B has to hold the whole of the range S's
    chunk covers, since two digests are only comparable over identical ranges. Both sides decide it
    with this one function -- the receiver's generator hashes exactly these chunks of B, and the
    sender waits for exactly these digests.

    `offset + length` is `min(offset + chunk_size, source_size)`, which only increases as a file is
    walked, so within one file this can go true -> false at most once and never back: the chunks a
    file compares are always the contiguous run 0..k-1. The wire format relies on that -- a digest
    frame carries only its first chunk index, not one index per digest.
    """
    offset = chunk * chunk_size
    if offset >= source_size:
        return False
    return offset + min(chunk_size, source_size - offset) <= basis_size


def unpack_digest_frame(payload: bytes) -> tuple[int, int, list[bytes]]:
    """Unpack a BASIS_DIGESTS frame: (file index, first chunk, count) + digests.

    Validates against the payload's actual length before `count` sizes anything, so a peer-supplied
    count can never drive an allocation before it is checked.
    """
    if len(payload) < BASIS_DIGESTS_HEAD.size:
        raise ProtocolError('digest frame payload too short for header')
    file_index, first_chunk, count = BASIS_DIGESTS_HEAD.unpack(payload[:BASIS_DIGESTS_HEAD.size])
    if count == 0:
        raise ProtocolError('digest frame count must be nonzero')
    if count > MAX_DIGESTS_PER_FRAME:
        raise ProtocolError(f'digest frame count {count} exceeds the cap of {MAX_DIGESTS_PER_FRAME}')
    body = payload[BASIS_DIGESTS_HEAD.size:]
    if len(body) != count * DIGEST_SIZE:
        raise ProtocolError(
            f'digest frame body is {len(body)} bytes, expected {count * DIGEST_SIZE} for count {count}')
    digests = [body[i * DIGEST_SIZE:(i + 1) * DIGEST_SIZE] for i in range(count)]
    return file_index, first_chunk, digests


def hang_up(sock: socket.socket) -> None:
    """Shut the connection down so that a thread blocked on `sock` -- in a send the peer will never
    drain, or in a recv -- wakes with an error or end of stream.

    Both directions where the system allows it. macOS refuses SHUT_RDWR (and SHUT_RD) with ENOTCONN
    once the peer has shut its own write side down; reads already end there, so shutting the write
    side is all that is left to do, and it is what fails a blocked send."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def tune(sock: socket.socket) -> socket.socket:
    """TCP_NODELAY on: both directions send small frames (a CHUNK_COPY is 21 bytes), and Nagle's
    algorithm would hold each one back."""
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock
