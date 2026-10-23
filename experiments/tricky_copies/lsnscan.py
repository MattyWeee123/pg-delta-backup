"""Evaluate the "skip pages whose LSN is below the old copy's redo point" shortcut.

For every 8 KiB page of every file present in both the fresh source backup S and
the old copy B, read the page's pd_lsn (the first 8 bytes of a PostgreSQL page
header: xlogid then xrecoff, both uint32 in native byte order) and decide
whether the shortcut would skip it. A skipped page is *wrongly skipped* when its
bytes differ between S and B: the shortcut would leave a stale page behind.

Non-relation files and pages beyond the old copy's length are never skippable.
Usage: python3 lsnscan.py SOURCE_BACKUP OLD_COPY REDO_LSN [--json OUT]
REDO_LSN is "X/Y" as printed by pg_controldata ("Latest checkpoint's REDO location").
"""
from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path

BLOCK = 8192
RELATION = re.compile(r'^(?:base/\d+|global|pg_tblspc/\d+/[^/]+/\d+)/(\d+)(?:_(fsm|vm|init))?(?:\.\d+)?$')


def parse_lsn(text):
    high, low = text.strip().split('/')
    return (int(high, 16) << 32) | int(low, 16)


def format_lsn(value):
    return f'{value >> 32:X}/{value & 0xFFFFFFFF:X}'


def page_lsn(page, byteorder='<'):
    """Return pd_lsn of a PostgreSQL page (uint32 xlogid, uint32 xrecoff)."""
    if len(page) < 8:
        return None
    high, low = struct.unpack(byteorder + 'II', page[:8])
    return (high << 32) | low


def relation_kind(relpath):
    match = RELATION.match(relpath)
    if not match:
        return None
    return match.group(2) or 'main'


def scan(source, old, redo, byteorder='<'):
    source, old = Path(source), Path(old)
    totals = {'pages_compared': 0, 'pages_skipped': 0, 'pages_wrongly_skipped': 0,
              'pages_changed': 0, 'pages_changed_and_sent': 0}
    # Relation pages in S whose file has no counterpart in B are always sent, so the
    # shortcut is never exercised on them; reported so "compared" isn't read as coverage.
    without_counterpart = 0
    by_kind = {}
    examples = []
    for path in sorted(p for p in source.rglob('*') if p.is_file() and not p.is_symlink()):
        relpath = path.relative_to(source).as_posix()
        kind = relation_kind(relpath)
        other = old / relpath
        if kind is None:
            continue
        if not other.is_file():
            without_counterpart += -(-path.stat().st_size // BLOCK)
            continue
        stats = by_kind.setdefault(kind, dict.fromkeys(totals, 0))
        with path.open('rb') as new_file, other.open('rb') as old_file:
            block = 0
            while True:
                new_page = new_file.read(BLOCK)
                if not new_page:
                    break
                old_page = old_file.read(BLOCK)
                changed = new_page != old_page
                skippable = len(old_page) == len(new_page) == BLOCK
                lsn = page_lsn(new_page, byteorder)
                skipped = skippable and lsn is not None and lsn < redo
                for bucket in (totals, stats):
                    bucket['pages_compared'] += 1
                    bucket['pages_changed'] += changed
                    bucket['pages_skipped'] += skipped
                    bucket['pages_wrongly_skipped'] += skipped and changed
                    bucket['pages_changed_and_sent'] += changed and not skipped
                if skipped and changed and len(examples) < 20:
                    examples.append({'file': relpath, 'block': block, 'kind': kind,
                                     'source_lsn': format_lsn(lsn),
                                     'old_lsn': format_lsn(page_lsn(old_page, byteorder))})
                block += 1
    totals['pages_without_counterpart'] = without_counterpart
    return {'redo_lsn': format_lsn(redo), 'totals': totals, 'by_kind': by_kind,
            'wrongly_skipped_examples': examples}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('source')
    parser.add_argument('old')
    parser.add_argument('redo_lsn')
    parser.add_argument('--json', type=Path)
    parser.add_argument('--big-endian', action='store_true',
                        help='read pd_lsn as big-endian (only for big-endian clusters)')
    args = parser.parse_args(argv)
    result = scan(args.source, args.old, parse_lsn(args.redo_lsn),
                  '>' if args.big_endian else '<')
    text = json.dumps(result, indent=2)
    if args.json:
        args.json.write_text(text + '\n')
    print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
