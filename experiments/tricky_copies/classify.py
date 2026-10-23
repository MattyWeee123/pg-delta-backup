"""Explain why each wrongly-skipped page differs.

lsnscan.scan counts pages the LSN shortcut would skip although their bytes
differ. This walks the same pages with the same rule (reusing lsnscan's
primitives) and sorts each wrongly-skipped page into one category:

- ``checksum_only``: only pd_checksum differs (e.g. pg_checksums --enable).
- ``lsn_only``: only pd_lsn (and the checksum covering it) differs; the rows
  are identical but were written at different WAL positions (e.g. two clusters).
- ``hint_bits_only``: same pd_lsn, and equal once the bits PostgreSQL itself may
  change without WAL are masked, following the server's own
  wal_consistency_checking masks (heap_mask, btree_mask): pd_checksum,
  pd_prune_xid, PD_PAGE_FULL, PD_HAS_FREE_LINES, unused space,
  tuple xmin/xmax hint bits, and for btree leaf pages the line pointer flags
  plus BTP_HAS_GARBAGE, BTP_SPLIT_END and the cycle id. Sequence pages are
  masked like heap pages.
- ``hint_bits_and_lsn``: as hint_bits_only, but pd_lsn differs as well.
- ``all_visible``: equal only once PD_ALL_VISIBLE is masked too. Unlike the bits
  above, VACUUM WAL-logs PD_ALL_VISIBLE, and a stale set bit can make page-mode
  scans skip visibility checks, so it is reported separately rather than as harmless.
- ``fsm``, ``vm``, ``init``: the page belongs to that fork (not masked further).
- ``content``: the masked pages still differ, so the copy would hold stale data.
- ``unclassified``: a main-fork page of an access method these masks don't know.
"""
from __future__ import annotations

import struct
from pathlib import Path

from experiments.tricky_copies.lsnscan import BLOCK, format_lsn, page_lsn, relation_kind

PAGE_HEADER = 24
PD_HAS_FREE_LINES, PD_PAGE_FULL, PD_ALL_VISIBLE = 0x1, 0x2, 0x4
HEAP_XMIN_COMMITTED, HEAP_XMIN_INVALID = 0x0100, 0x0200
HEAP_XMAX_COMMITTED, HEAP_XMAX_INVALID = 0x0400, 0x0800
HEAP_XMIN_FROZEN = HEAP_XMIN_COMMITTED | HEAP_XMIN_INVALID
LP_UNUSED, LP_NORMAL = 0, 1
BTP_LEAF, BTP_SPLIT_END, BTP_HAS_GARBAGE = 1 << 0, 1 << 5, 1 << 6
MAX_BT_CYCLE_ID = 0xFF7F
SEQUENCE_MAGIC = 0x1717
CATEGORIES = ('checksum_only', 'lsn_only', 'hint_bits_only', 'hint_bits_and_lsn', 'all_visible',
              'fsm', 'vm', 'init', 'content', 'unclassified')


def header(page):
    lower, upper, special = struct.unpack_from('<HHH', page, 12)
    return lower, upper, special


def page_kind(page):
    """'heap', 'btree', 'sequence', 'new' or 'other', from the special space."""
    lower, upper, special = header(page)
    if upper == 0:
        return 'new'
    if not (PAGE_HEADER <= lower <= upper <= special <= BLOCK):
        return 'other'
    if special == BLOCK:
        return 'heap'
    if BLOCK - special == 16:
        cycle_id = struct.unpack_from('<H', page, BLOCK - 2)[0]
        if cycle_id <= MAX_BT_CYCLE_ID:  # hash, GiST, etc. put a page id >= 0xFF80 here
            return 'btree'
    if BLOCK - special in (4, 8) and struct.unpack_from('<I', page, special)[0] == SEQUENCE_MAGIC:
        return 'sequence'
    return 'other'


def line_pointers(page):
    lower = header(page)[0]
    for index in range((lower - PAGE_HEADER) // 4):
        position = PAGE_HEADER + 4 * index
        value = struct.unpack_from('<I', page, position)[0]
        yield position, value & 0x7FFF, (value >> 15) & 0x3, value >> 17


def mask_common(buf, all_visible=False):
    struct.pack_into('<IIH', buf, 0, 0, 0, 0)  # pd_lsn, pd_checksum
    flags = struct.unpack_from('<H', buf, 10)[0]
    masked = PD_HAS_FREE_LINES | PD_PAGE_FULL | (PD_ALL_VISIBLE if all_visible else 0)
    struct.pack_into('<H', buf, 10, flags & ~masked)
    struct.pack_into('<I', buf, 20, 0)  # pd_prune_xid
    lower, upper, _ = header(buf)
    buf[lower:upper] = bytes(upper - lower)  # unused space


def mask_heap(page, all_visible=False):
    buf = bytearray(page)
    mask_common(buf, all_visible)
    for _pos, offset, flags, length in line_pointers(page):
        if flags != LP_NORMAL or length < 23 or offset + 22 > BLOCK:
            continue
        infomask = struct.unpack_from('<H', buf, offset + 20)[0]
        if infomask & HEAP_XMIN_FROZEN == HEAP_XMIN_FROZEN:
            infomask &= ~(HEAP_XMAX_COMMITTED | HEAP_XMAX_INVALID)
        else:
            infomask &= ~(HEAP_XMIN_FROZEN | HEAP_XMAX_COMMITTED | HEAP_XMAX_INVALID)
        struct.pack_into('<H', buf, offset + 20, infomask)
    return bytes(buf)


def mask_btree(page, all_visible=False):
    buf = bytearray(page)
    mask_common(buf, all_visible)
    opaque = BLOCK - 16
    flags = struct.unpack_from('<H', buf, opaque + 12)[0]
    if flags & BTP_LEAF:
        for position, offset, lp_flags, length in line_pointers(page):
            if lp_flags != LP_UNUSED:
                struct.pack_into('<I', buf, position, offset | (LP_UNUSED << 15) | (length << 17))
    struct.pack_into('<HH', buf, opaque + 12, flags & ~(BTP_HAS_GARBAGE | BTP_SPLIT_END), 0)
    return bytes(buf)


def classify_pair(kind, old_page, new_page):
    """Category of a page pair that differs (see module docstring)."""
    if kind in ('fsm', 'vm', 'init'):
        return kind
    same_lsn = old_page[:8] == new_page[:8]
    if old_page[10:] == new_page[10:]:  # bytes 0-7 are pd_lsn, 8-9 pd_checksum
        return 'checksum_only' if same_lsn else 'lsn_only'
    old_kind, new_kind = page_kind(old_page), page_kind(new_page)
    if old_kind != new_kind or old_kind == 'new':
        return 'content'
    if old_kind in ('heap', 'sequence'):  # a sequence page holds one heap tuple
        mask = mask_heap
    elif old_kind == 'btree':
        mask = mask_btree
    else:
        return 'unclassified'
    if mask(old_page) == mask(new_page):
        return 'hint_bits_only' if same_lsn else 'hint_bits_and_lsn'
    if mask(old_page, all_visible=True) == mask(new_page, all_visible=True):
        return 'all_visible'
    return 'content'


def classify_wrong_skips(source, old, redo, byteorder='<', examples=5):
    """Walk S and B exactly as lsnscan.scan does and classify each wrongly-skipped page."""
    source, old = Path(source), Path(old)
    totals = dict.fromkeys(CATEGORIES, 0)
    by_kind, samples = {}, {}
    for path in sorted(p for p in source.rglob('*') if p.is_file() and not p.is_symlink()):
        relpath = path.relative_to(source).as_posix()
        kind = relation_kind(relpath)
        other = old / relpath
        if kind is None or not other.is_file():
            continue
        with path.open('rb') as new_file, other.open('rb') as old_file:
            block = 0
            while True:
                new_page = new_file.read(BLOCK)
                if not new_page:
                    break
                old_page = old_file.read(BLOCK)
                lsn = page_lsn(new_page, byteorder)
                skipped = (len(old_page) == len(new_page) == BLOCK and lsn is not None
                           and lsn < redo)
                if skipped and new_page != old_page:
                    category = classify_pair(kind, old_page, new_page)
                    totals[category] += 1
                    bucket = by_kind.setdefault(kind, dict.fromkeys(CATEGORIES, 0))
                    bucket[category] += 1
                    listed = samples.setdefault(category, [])
                    if len(listed) < examples:
                        listed.append({'file': relpath, 'block': block,
                                       'source_lsn': format_lsn(lsn)})
                block += 1
    return {'redo_lsn': format_lsn(redo), 'wrongly_skipped': sum(totals.values()),
            'by_category': totals,
            'by_kind': {kind: {c: n for c, n in counts.items() if n}
                        for kind, counts in by_kind.items()},
            'examples': samples}
