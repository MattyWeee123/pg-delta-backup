"""--subpage: send only the 512-byte blocks of each page that differ from B's page there (EXP-54, EXP-55, EXP-56).

After each LEAF_DIGESTS the receiver sends BLOCK_DIGESTS: for every page of B in those nodes, the keyed 8-byte
digest of each of its blocks (pgdelta.hashing, Hasher.blocks). The sender hashes the bytes it is about to send in
the same blocks and sends each run as PAGE_DELTA (or PAGE_DELTA_ZSTD): PAGE_DATA's head, then per page a 2-byte
bitmap and the blocks it marks, in order. WHOLE (every bit) carries the whole page: where B has no full page at
that offset, where every block differs, and for a file's short last page. 0 carries nothing: the page is B's.
The receiver writes the blocks into B's page and counts the page as written, so verification re-hashes all of it
and the file digest must still be S's. Only for 8 KiB pages (16 blocks to a 16-bit bitmap).
"""
from __future__ import annotations

import struct

from pgdelta.errors import ProtocolError
from pgdelta.hashing import SUB_BLOCK, SUB_DIGEST_SIZE

PAGE = 8192
BLOCKS = PAGE // SUB_BLOCK
WHOLE = (1 << BLOCKS) - 1
DIGESTS_PER_PAGE = BLOCKS * SUB_DIGEST_SIZE
_BITMAP = struct.Struct('>H')


def digest_bytes(length):
    """The block-digest bytes of a page of `length` bytes."""
    return -(-length // SUB_BLOCK) * SUB_DIGEST_SIZE


def encode_page(page, old, hasher, counts):
    """`page`'s bitmap and blocks against B's block digests `old` (None: B has no full page here)."""
    if old is None or len(page) != PAGE or len(old) != DIGESTS_PER_PAGE:
        counts['pages_whole'] += 1
        return [_BITMAP.pack(WHOLE), page]
    mine = hasher.blocks(page)
    bits, parts = 0, []
    for i in range(BLOCKS):
        at = i * SUB_DIGEST_SIZE
        if mine[at:at + SUB_DIGEST_SIZE] != old[at:at + SUB_DIGEST_SIZE]:
            bits |= 1 << i
            parts.append(page[i * SUB_BLOCK:(i + 1) * SUB_BLOCK])
    if bits == WHOLE:
        counts['pages_whole'] += 1
        return [_BITMAP.pack(WHOLE), page]
    counts['pages_partial' if bits else 'pages_same'] += 1
    counts['blocks_sent'] += len(parts)
    return [_BITMAP.pack(bits), *parts]


def groups(bits):
    """[(first block, count)] of the runs of set bits, in order."""
    out, i = [], 0
    while i < BLOCKS:
        if bits >> i & 1:
            start = i
            while i < BLOCKS and bits >> i & 1:
                i += 1
            out.append((start, i - start))
        else:
            i += 1
    return out


def decode(body, first, size, path):
    """[(page, bitmap, bytes)] of a PAGE_DELTA body for pages from `first` of a file of `size` bytes."""
    body = memoryview(body)
    out, offset, page = [], 0, first
    while offset < len(body):
        if offset + _BITMAP.size > len(body):
            raise ProtocolError(f'PAGE_DELTA for {path} ends inside a bitmap')
        bits = _BITMAP.unpack_from(body, offset)[0]
        offset += _BITMAP.size
        start = page * PAGE
        if start >= size:
            raise ProtocolError(f'PAGE_DELTA for {path} runs past the file')
        length = min(PAGE, size - start) if bits == WHOLE else bin(bits).count('1') * SUB_BLOCK
        if offset + length > len(body):
            raise ProtocolError(f'PAGE_DELTA for {path} holds fewer bytes than its bitmap marks')
        out.append((page, bits, body[offset:offset + length]))
        offset += length
        page += 1
    if not out:
        raise ProtocolError(f'PAGE_DELTA for {path} is empty')
    return out
