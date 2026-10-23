"""PostgreSQL 17 change-tracker views: INCREMENTAL.* files and WAL summaries.

INCREMENTAL file layout, as written by sendFile() in
src/backend/backup/basebackup.c and read by make_incremental_rfile() in
src/bin/pg_combinebackup/reconstruct.c (REL_17_STABLE):

    uint32 magic                    INCREMENTAL_MAGIC, 0xd3ae1f0d
                                    (src/include/backup/basebackup_incremental.h)
    uint32 num_blocks               at most RELSEG_SIZE
    uint32 truncation_block_length  at most RELSEG_SIZE
    uint32 block[num_blocks]        segment-relative, sorted (GetFileBackupMethod
                                    in src/backend/backup/basebackup_incremental.c)
    zero padding to a multiple of BLCKSZ, only when num_blocks > 0
    num_blocks * BLCKSZ bytes of page images, in block-list order

Integers are in the server's native byte order.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import struct
import sys

from experiments.change_patterns.pagediff import BLOCK_SIZE, RELATION_FILE, classify

INCREMENTAL_MAGIC = 0xD3AE1F0D
INCREMENTAL_PREFIX = 'INCREMENTAL.'
RELSEG_BLOCKS = 131072  # RELSEG_SIZE for the default 1 GiB segments
HEADER_FIELDS = 12
DEFAULT_TABLESPACE, GLOBAL_TABLESPACE = 1663, 1664
FORK_SUFFIXES = {'main': '', 'fsm': '_fsm', 'vm': '_vm', 'init': '_init'}
SUMMARY_NAME = re.compile(r'([0-9A-F]{8})([0-9A-F]{16})([0-9A-F]{16})\.summary')
SUMMARY_LINE = re.compile(r'TS (\d+), DB (\d+), REL (\d+), FORK (\w+): '
                          r'(block|blocks|limit) (\d+)(?:\.\.(\d+))?')


class IncrementalFormatError(ValueError):
    pass


@dataclass(frozen=True)
class IncrementalHeader:
    num_blocks: int
    truncation_block_length: int
    blocks: tuple[int, ...]
    header_length: int  # including alignment padding

    @property
    def expected_size(self) -> int:
        return self.header_length + self.num_blocks * BLOCK_SIZE


def incremental_header_length(num_blocks: int) -> int:
    length = HEADER_FIELDS + 4 * num_blocks
    if num_blocks and length % BLOCK_SIZE:
        length += BLOCK_SIZE - length % BLOCK_SIZE
    return length


def parse_incremental_header(data: bytes, *, file_size: int | None = None,
                             byteorder: str = sys.byteorder, name: str = 'file',
                             relseg_blocks: int = RELSEG_BLOCKS) -> IncrementalHeader:
    """Parse the header at the start of data; check file_size when given."""
    order = {'little': '<', 'big': '>'}[byteorder]
    if len(data) < HEADER_FIELDS:
        raise IncrementalFormatError(
            f'{name}: truncated header ({len(data)} bytes, need {HEADER_FIELDS})')
    magic, count, truncation = struct.unpack_from(order + '3I', data)
    if magic != INCREMENTAL_MAGIC:
        swapped = struct.unpack('<I', struct.pack('>I', magic))[0]
        hint = ' (byte-swapped: written on a different-endian server?)' \
            if swapped == INCREMENTAL_MAGIC else ''
        raise IncrementalFormatError(
            f'{name}: bad magic 0x{magic:08x}, expected 0x{INCREMENTAL_MAGIC:08x}{hint}')
    if count > relseg_blocks:
        raise IncrementalFormatError(
            f'{name}: block count {count} exceeds segment size {relseg_blocks}')
    if truncation > relseg_blocks:
        raise IncrementalFormatError(
            f'{name}: truncation block length {truncation} exceeds segment size {relseg_blocks}')
    needed = HEADER_FIELDS + 4 * count
    if len(data) < needed:
        raise IncrementalFormatError(
            f'{name}: block list truncated ({len(data)} bytes, need {needed})')
    blocks = struct.unpack_from(f'{order}{count}I', data, HEADER_FIELDS)
    for previous, block in zip((-1,) + blocks, blocks):
        if block >= relseg_blocks:
            raise IncrementalFormatError(
                f'{name}: block {block} is outside a {relseg_blocks}-block segment')
        if block <= previous:
            raise IncrementalFormatError(f'{name}: block list is not strictly increasing')
    header = IncrementalHeader(count, truncation, blocks, incremental_header_length(count))
    if file_size is not None and file_size != header.expected_size:
        raise IncrementalFormatError(
            f'{name}: size {file_size} does not match header ({header.expected_size} '
            f'= {header.header_length} header + {count} blocks)')
    return header


def read_incremental_header(path: Path) -> IncrementalHeader:
    with open(path, 'rb') as stream:
        fixed = stream.read(HEADER_FIELDS)
        count = (struct.unpack_from(('<' if sys.byteorder == 'little' else '>') + 'I',
                                    fixed, 4)[0] if len(fixed) == HEADER_FIELDS else 0)
        # Bound the read so a corrupt count cannot ask for gigabytes.
        rest = stream.read(4 * min(count, RELSEG_BLOCKS + 1))
    return parse_incremental_header(fixed + rest, file_size=path.stat().st_size,
                                    name=str(path))


@dataclass(frozen=True)
class TrackedFile:
    """How the incremental backup carried one file of the full layout."""
    method: str  # 'incremental' or 'full'
    size: int  # bytes on disk in the incremental backup
    blocks: tuple[int, ...] | None = None
    truncation_block_length: int | None = None


def original_path(relative: str) -> str | None:
    """base/5/INCREMENTAL.16384.1 -> base/5/16384.1; None for other files."""
    head, _, name = relative.rpartition('/')
    if not name.startswith(INCREMENTAL_PREFIX):
        return None
    return (head + '/' if head else '') + name[len(INCREMENTAL_PREFIX):]


def scan_incremental_backup(root: Path) -> dict[str, TrackedFile]:
    """Map each file path of the equivalent full backup to how I1 carried it."""
    result = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
        for name in names:
            entry = Path(directory) / name
            relative = entry.relative_to(root).as_posix()
            original = original_path(relative)
            if original is None:
                result[relative] = TrackedFile('full', entry.stat().st_size)
                continue
            if RELATION_FILE.fullmatch(original) is None:
                raise IncrementalFormatError(f'{relative}: not a relation file name')
            header = read_incremental_header(entry)
            result[original] = TrackedFile('incremental', entry.stat().st_size,
                                           header.blocks, header.truncation_block_length)
    return result


def summary_file_range(name: str) -> tuple[int, int, int]:
    """(timeline, start LSN, end LSN) from a pg_wal/summaries file name."""
    match = SUMMARY_NAME.fullmatch(name)
    if not match:
        raise ValueError(f'not a WAL summary file name: {name}')
    return tuple(int(part, 16) for part in match.groups())


def parse_lsn(text: str) -> int:
    high, low = text.strip().split('/')
    return (int(high, 16) << 32) | int(low, 16)


def parse_walsummary(text: str) -> tuple[dict[tuple, set[int]], dict[tuple, int]]:
    """Blocks and smallest limit block per (tablespace, db, relfilenode, fork)
    from pg_walsummary output; 'blocks a..b' ranges are inclusive."""
    blocks: dict[tuple, set[int]] = {}
    limits: dict[tuple, int] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = SUMMARY_LINE.fullmatch(line.strip())
        if not match:
            raise ValueError(f'unrecognised pg_walsummary line: {line!r}')
        tablespace, database, relation, fork, word, first, last = match.groups()
        key = (int(tablespace), int(database), int(relation), fork)
        if word == 'limit':
            limits[key] = min(limits.get(key, int(first)), int(first))
            continue
        end = int(last) if last is not None else int(first)
        if end < int(first):
            raise ValueError(f'descending block range: {line!r}')
        blocks.setdefault(key, set()).update(range(int(first), end + 1))
    return blocks, limits


def summary_blocks_by_file(blocks: dict[tuple, set[int]],
                           relseg_blocks: int = RELSEG_BLOCKS
                           ) -> tuple[dict[str, set[int]], int]:
    """Convert relation blocks to segment-relative blocks per backup path.

    Returns the mapping and the number of blocks in unsupported tablespaces.
    """
    files: dict[str, set[int]] = {}
    skipped = 0
    for (tablespace, database, relation, fork), numbers in blocks.items():
        if tablespace == GLOBAL_TABLESPACE:
            base = f'global/{relation}'
        elif tablespace == DEFAULT_TABLESPACE:
            base = f'base/{database}/{relation}'
        else:
            skipped += len(numbers)
            continue
        base += FORK_SUFFIXES[fork]
        for number in numbers:
            segment, block = divmod(number, relseg_blocks)
            path = base + (f'.{segment}' if segment else '')
            files.setdefault(path, set()).add(block)
    return files, skipped


def tracked_sets_from_backup(tracked: dict[str, TrackedFile]) -> dict[str, tuple[str, set[int]]]:
    """Blocks I1 ships per relation file: the listed blocks, or all of a full file."""
    result = {}
    for path, entry in tracked.items():
        if RELATION_FILE.fullmatch(path) is None:
            continue
        if entry.method == 'incremental':
            result[path] = ('incremental', set(entry.blocks))
        else:
            result[path] = ('full', set(range(-(-entry.size // BLOCK_SIZE))))
    return result


def compress_ranges(numbers) -> list[str]:
    """Sorted block numbers as inclusive ranges: 0..12, 40, 77..80 -> ['0-12', '40', '77-80']."""
    ranges: list[str] = []
    ordered = sorted(set(numbers))
    start = 0
    for index in range(1, len(ordered) + 1):
        if index == len(ordered) or ordered[index] != ordered[index - 1] + 1:
            first, last = ordered[start], ordered[index - 1]
            ranges.append(str(first) if first == last else f'{first}-{last}')
            start = index
    return ranges


def expand_ranges(ranges) -> list[int]:
    """Inverse of compress_ranges."""
    numbers: list[int] = []
    for item in ranges:
        match = re.fullmatch(r'([0-9]+)(?:-([0-9]+))?', item)
        if not match:
            raise ValueError(f'not a block range: {item!r}')
        first = int(match[1])
        last = int(match[2]) if match[2] is not None else first
        if last < first:
            raise ValueError(f'descending block range: {item!r}')
        numbers.extend(range(first, last + 1))
    return numbers


def summarize_tracked(tracked: dict[str, TrackedFile], relmap: dict[str, dict]) -> dict:
    """Totals of how I1 carried each file, plus every INCREMENTAL file's own
    entry (keyed by the original relation path, block list range-compressed)."""
    incremental = [e for e in tracked.values() if e.method == 'incremental']
    full = [(path, e) for path, e in tracked.items() if e.method == 'full']
    full_relation: dict[str, dict] = {}
    full_other = {'files': 0, 'bytes': 0}
    for path, entry in full:
        info = classify(path, relmap)
        bucket = (full_relation.setdefault(info['kind'], {'files': 0, 'bytes': 0})
                  if info['area'] == 'relation' else full_other)
        bucket['files'] += 1
        bucket['bytes'] += entry.size
    return {
        'total_bytes': sum(e.size for e in tracked.values()),
        'incremental_files': {
            'files': len(incremental),
            'bytes': sum(e.size for e in incremental),
            'blocks_listed': sum(len(e.blocks) for e in incremental),
            'files_with_zero_blocks': sum(not e.blocks for e in incremental),
            'header_bytes': sum(incremental_header_length(len(e.blocks)) for e in incremental),
            'by_file': {path: {'size': e.size,
                               'truncation_block_length': e.truncation_block_length,
                               'blocks': compress_ranges(e.blocks)}
                        for path, e in sorted(tracked.items()) if e.method == 'incremental'},
        },
        'full_files': {
            'files': len(full),
            'bytes': sum(e.size for _, e in full),
            'relation_files_by_kind': full_relation,
            'non_relation': full_other,
        },
    }


def _counts() -> dict:
    return {'files': 0, 'changed_and_tracked': 0, 'changed_not_tracked': 0,
            'tracked_unchanged': 0, 'tracked_beyond_new_end': 0}


def _full_reason(info: dict, diff, changed: set[int], new_blocks: int, size: int) -> str:
    """Best guess at why GetFileBackupMethod sent a relation file whole."""
    if info['fork'] == 'fsm':
        return 'fsm_fork'
    if diff is None or diff.old_size is None:
        return 'absent_from_basis'
    if size == 0:
        return 'empty'
    if new_blocks and len(changed) > 0.9 * new_blocks:
        return 'over_90_percent_changed'
    return 'other'


def agreement(diffs, tracked_sets: dict[str, tuple[str, set[int]]],
              relmap: dict[str, dict], *, max_examples: int = 20) -> dict:
    """Compare content-changed 8 KiB blocks (B0 vs B1) with a tracker's blocks.

    Only relation files are compared, over the blocks that exist in B1;
    tracked blocks past B1's end are counted separately. Scopes are 'all'
    plus one per tracker method ('incremental', 'full', 'absent', ...).
    """
    by_path = {d.path: d for d in diffs if RELATION_FILE.fullmatch(d.path)}
    scopes: dict[str, dict] = {'all': {**_counts(), 'by_kind': {}}}
    pending: dict[str, dict[str, list]] = {}
    reasons: dict[str, int] = {}
    for path in sorted(set(by_path) | set(tracked_sets)):
        diff = by_path.get(path)
        new_blocks = diff.new_blocks if diff is not None else 0
        changed = {b for b in diff.changed if b < new_blocks} if diff is not None else set()
        method, blocks = tracked_sets.get(path, ('absent', set()))
        beyond = {b for b in blocks if b >= new_blocks}
        inside = blocks - beyond
        info = classify(path, relmap)
        values = {'changed_and_tracked': len(changed & inside),
                  'changed_not_tracked': len(changed - inside),
                  'tracked_unchanged': len(inside - changed),
                  'tracked_beyond_new_end': len(beyond)}
        for scope_name in ('all', method):
            scope = scopes.setdefault(scope_name, {**_counts(), 'by_kind': {}})
            kind = scope['by_kind'].setdefault(info['kind'], _counts())
            for target in (scope, kind):
                target['files'] += 1
                for key, value in values.items():
                    target[key] += value
        if method == 'full':
            reason = _full_reason(info, diff, changed, new_blocks, len(blocks) * BLOCK_SIZE)
            reasons[reason] = reasons.get(reason, 0) + 1
        for category, numbers in (('changed_not_tracked', changed - inside),
                                  ('tracked_unchanged', inside - changed)):
            if numbers:
                pending.setdefault(method, {}).setdefault(category, []).append(
                    (path, info, sorted(numbers)))
    if 'full' in scopes:
        scopes['full']['reasons'] = reasons
    examples = {method: {category: round_robin(files, max_examples)
                         for category, files in categories.items()}
                for method, categories in pending.items()}
    return {'scopes': scopes, 'examples': examples}


def round_robin(files: list, limit: int) -> list[dict]:
    """Up to limit (file, block) examples, taking one block per file in turn."""
    picked, depth = [], 0
    while len(picked) < limit and any(depth < len(numbers) for _, _, numbers in files):
        for path, info, numbers in files:
            if depth < len(numbers) and len(picked) < limit:
                picked.append({'file': path, 'block': numbers[depth], 'kind': info['kind'],
                               'relation': info['relation']})
        depth += 1
    return picked
