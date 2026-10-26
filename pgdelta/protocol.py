"""Framed messages over a byte stream, with per-type byte accounting.

Frame: 4-byte big-endian payload length, 1-byte message type, payload.
Control messages carry UTF-8 JSON; digests and pages travel as raw bytes.
Every frame (header included) is counted per direction and per type, so a
run can report exactly how many bytes crossed the link and why.
"""
from __future__ import annotations

import json
import struct
import threading
import time

from pgdelta.errors import ProtocolError, TransferError
from pgdelta.hashing import DIGEST_SIZE

PROTOCOL_VERSION = 1
HEADER = struct.Struct('>IB')
MAX_FRAME = 64 << 20

HELLO, ACCEPT, BASIS_NODES, LEAF_REQUEST, LEAF_DIGESTS, PAGE_DATA, FILE_DONE, COMPLETE, ERROR = range(1, 10)
# A live source (--source-kind live, pgdelta.live) learns which WAL segments and what backup_label to send only
# after pg_backup_stop, when every listed file has been sent. Its HELLO says so ("appends": true), and APPEND
# then lists those files once, before COMPLETE. No other session sends either, so their bytes are unchanged.
APPEND = 10
# --compress zstd (pgdelta.compress): a run of pages compressed, its head as PAGE_DATA's. Sent only after the
# receiver accepted the HELLO's compress_offer, so no other session's bytes change.
PAGE_DATA_ZSTD = 11
MESSAGES = {
    HELLO: ('HELLO', 'control'),
    ACCEPT: ('ACCEPT', 'control'),
    BASIS_NODES: ('BASIS_NODES', 'node_digests'),
    LEAF_REQUEST: ('LEAF_REQUEST', 'leaf_digests'),
    LEAF_DIGESTS: ('LEAF_DIGESTS', 'leaf_digests'),
    PAGE_DATA: ('PAGE_DATA', 'page_data'),
    FILE_DONE: ('FILE_DONE', 'control'),
    COMPLETE: ('COMPLETE', 'control'),
    ERROR: ('ERROR', 'control'),
    APPEND: ('APPEND', 'control'),
    PAGE_DATA_ZSTD: ('PAGE_DATA_ZSTD', 'page_data'),
}
CATEGORIES = ('control', 'node_digests', 'leaf_digests', 'page_data')

_INDEX = struct.Struct('>I')
_INDEX_U64 = struct.Struct('>IQ')
_NODE_HEAD = struct.Struct('>II')
_FILE_DONE = struct.Struct(f'>IQ{DIGEST_SIZE}s')
PAGE_HEAD_SIZE = _INDEX_U64.size


def name(code):
    return MESSAGES[code][0] if code in MESSAGES else f'type {code}'


class ChannelClosed(TransferError):
    """The stream failed or the peer went away."""


class Counter:
    def __init__(self):
        self.frames = dict.fromkeys(MESSAGES, 0)
        self.bytes = dict.fromkeys(MESSAGES, 0)

    def add(self, code, size):
        self.frames[code] += 1
        self.bytes[code] += size

    @property
    def total(self):
        return sum(self.bytes.values())

    def summary(self):
        result = {'total': self.total, **dict.fromkeys(CATEGORIES, 0)}
        for code, (_, category) in MESSAGES.items():
            result[category] += self.bytes[code]
        result['by_message_type'] = {
            label: {'frames': self.frames[code], 'bytes': self.bytes[code]}
            for code, (label, _) in MESSAGES.items() if self.frames[code]}
        return result


class Channel:
    """One bidirectional session stream. send() is thread-safe; recv() has one caller.

    `sock` is the TCP socket under the stream (None over pipes), whose send
    queue the sender reads while it waits (pgdelta.waits); `connect_seconds`
    is how long connecting it took, when this end connected.

    `sending_since` is the time.monotonic() at which the send() in progress
    began (None between sends), so another thread can tell a send that the
    peer has stopped taking. abort() ends the stream at once from any
    thread through `aborter` (a TCP socket: shut it down; a child process:
    kill it), so a send or receive blocked on the peer fails, and every
    later send fails without writing.
    """
    connect_seconds = None

    def __init__(self, reader, writer, *, closer=None, max_frame=MAX_FRAME, sock=None, aborter=None):
        self._reader, self._writer, self._closer = reader, writer, closer
        self.max_frame = max_frame
        self.sock = sock
        self.sent, self.received = Counter(), Counter()
        self._lock = threading.Lock()
        self._closed = self._broken = False
        self._aborter = aborter
        self.aborted = False
        self.sending_since = None

    def send(self, code, *parts):
        if code not in MESSAGES:
            raise ProtocolError(f'refusing to send unknown message type {code}')
        size = sum(len(part) for part in parts)
        if size > self.max_frame:
            raise ProtocolError(f'{name(code)} payload of {size} bytes exceeds the {self.max_frame} byte frame limit')
        with self._lock:
            if self.aborted:
                raise ChannelClosed(f'cannot send {name(code)}: the connection was aborted')
            if self._broken:
                raise ChannelClosed(f'cannot send {name(code)}: an earlier frame was cut off')
            self.sending_since = time.monotonic()
            try:
                self._writer.write(HEADER.pack(size, code))
                for part in parts:
                    self._writer.write(part)
                self._writer.flush()
            except BaseException as exc:
                # A partly written frame desynchronizes the stream; send nothing more.
                self._broken = True
                if isinstance(exc, (OSError, ValueError)):
                    raise ChannelClosed(f'cannot send {name(code)}: {exc}') from exc
                raise
            finally:
                self.sending_since = None
            self.sent.add(code, HEADER.size + size)

    def abort(self):
        """End the stream now, from any thread: what is blocked on the peer fails, and nothing more is sent."""
        if self.aborted:
            return
        self.aborted = True
        if self._aborter is not None:
            try:
                self._aborter()
            except OSError:
                pass

    def send_json(self, code, value):
        self.send(code, json.dumps(value, separators=(',', ':'), allow_nan=False).encode('utf-8'))

    def recv(self):
        """Return (type, payload), or None on a clean end of stream between frames."""
        header = self._read_exact(HEADER.size, allow_eof=True)
        if header is None:
            return None
        size, code = HEADER.unpack(header)
        if code not in MESSAGES:
            raise ProtocolError(f'unknown message type {code}')
        if size > self.max_frame:
            raise ProtocolError(f'{name(code)} frame of {size} bytes exceeds the {self.max_frame} byte limit')
        payload = self._read_exact(size)
        self.received.add(code, HEADER.size + size)
        return code, payload

    def _read_exact(self, size, allow_eof=False):
        buffer = bytearray(size)
        view = memoryview(buffer)
        filled = 0
        while filled < size:
            try:
                count = self._reader.readinto(view[filled:])
            except (OSError, ValueError) as exc:
                raise ChannelClosed(f'connection failed: {exc}') from exc
            if not count:
                if filled == 0 and allow_eof:
                    return None
                raise ChannelClosed('connection closed in the middle of a frame')
            filled += count
        return buffer

    def discard_input(self):
        """Read and drop everything until the peer closes its side."""
        try:
            while self._reader.read1(1 << 16):
                pass
        except (OSError, ValueError):
            pass

    def close(self):
        """Close the write side first so the peer sees end of stream even if a reader is blocked."""
        if self._closed:
            return
        self._closed = True
        if self._closer is not None:
            self._closer(self._reader, self._writer)
            return
        for stream in (self._writer, self._reader):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def decode_json(payload, what):
    try:
        value = json.loads(bytes(payload).decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f'{what} is not valid UTF-8 JSON') from exc
    if not isinstance(value, dict):
        raise ProtocolError(f'{what} must be a JSON object')
    return value


def encode_basis_nodes(index, size, node_blob):
    return _INDEX_U64.pack(index, size) + node_blob


def decode_basis_nodes(payload):
    if len(payload) < _INDEX_U64.size or (len(payload) - _INDEX_U64.size) % DIGEST_SIZE:
        raise ProtocolError('malformed BASIS_NODES')
    index, size = _INDEX_U64.unpack_from(payload)
    return index, size, bytes(payload[_INDEX_U64.size:])


def encode_leaf_request(index, nodes):
    return _INDEX.pack(index) + struct.pack(f'>{len(nodes)}I', *nodes)


def decode_leaf_request(payload):
    if len(payload) < _INDEX.size + 4 or (len(payload) - _INDEX.size) % 4:
        raise ProtocolError('malformed LEAF_REQUEST')
    count = (len(payload) - _INDEX.size) // 4
    return _INDEX.unpack_from(payload)[0], list(struct.unpack_from(f'>{count}I', payload, _INDEX.size))


def encode_leaf_digests(index, entries):
    """entries: (node index, concatenated basis leaf digests of that node) pairs."""
    parts = [_INDEX.pack(index)]
    for node, blob in entries:
        parts += [_NODE_HEAD.pack(node, len(blob) // DIGEST_SIZE), blob]
    return b''.join(parts)


def decode_leaf_digests(payload):
    if len(payload) < _INDEX.size:
        raise ProtocolError('malformed LEAF_DIGESTS')
    index = _INDEX.unpack_from(payload)[0]
    offset, entries = _INDEX.size, []
    while offset < len(payload):
        if offset + _NODE_HEAD.size > len(payload):
            raise ProtocolError('truncated LEAF_DIGESTS entry')
        node, count = _NODE_HEAD.unpack_from(payload, offset)
        offset += _NODE_HEAD.size
        end = offset + count * DIGEST_SIZE
        if end > len(payload):
            raise ProtocolError('truncated LEAF_DIGESTS entry')
        entries.append((node, bytes(payload[offset:end])))
        offset = end
    return index, entries


def page_data_head(index, first_page):
    return _INDEX_U64.pack(index, first_page)


def decode_page_data(payload):
    if len(payload) <= PAGE_HEAD_SIZE:
        raise ProtocolError('malformed PAGE_DATA')
    index, first_page = _INDEX_U64.unpack_from(payload)
    return index, first_page, memoryview(payload)[PAGE_HEAD_SIZE:]


def encode_file_done(index, size, digest):
    return _FILE_DONE.pack(index, size, digest)


def decode_file_done(payload):
    if len(payload) != _FILE_DONE.size:
        raise ProtocolError('malformed FILE_DONE')
    return _FILE_DONE.unpack(payload)


MIN_BLOCK, MAX_BLOCK = 512, 1 << 20
MAX_NODE_BLOCKS = 1 << 16
MAX_NODE_BYTES = 64 << 20


def check_parameters(block, node_blocks, error=ProtocolError):
    if type(block) is not int or not MIN_BLOCK <= block <= MAX_BLOCK:
        raise error(f'block size {block!r} must be an integer from {MIN_BLOCK} to {MAX_BLOCK}')
    if (type(node_blocks) is not int or not 1 <= node_blocks <= MAX_NODE_BLOCKS
            or block * node_blocks > MAX_NODE_BYTES):
        raise error(f'node_blocks {node_blocks!r} must be 1 to {MAX_NODE_BLOCKS} '
                    f'with nodes of at most {MAX_NODE_BYTES} bytes')


def max_nodes_per_request(node_blocks, max_frame=MAX_FRAME):
    """Nodes per LEAF_REQUEST so that the LEAF_DIGESTS reply fits comfortably in one frame."""
    per_node = _NODE_HEAD.size + node_blocks * DIGEST_SIZE
    return max(1, min(4096, (max_frame // 2) // per_node))
