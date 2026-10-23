"""Chunk-level content comparison of two plain PostgreSQL backups.

For every file, find the aligned 8 KiB blocks whose bytes differ, derive the
counts for larger aligned chunks from them, and aggregate by relation kind.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat

BLOCK_SIZE = 8192
CHUNK_SIZES = tuple(BLOCK_SIZE << shift for shift in range(8))  # 8 KiB .. 1 MiB
FIRST_NORMAL_OID = 16384  # src/include/access/transam.h
BACKUP_METADATA = frozenset({'backup_label', 'backup_manifest', 'tablespace_map'})
RELATION_FILE = re.compile(r'(?P<key>(?:base/[0-9]+|global)/(?P<node>[0-9]+))'
                           r'(?:_(?P<fork>fsm|vm|init))?(?:\.(?P<segno>[0-9]+))?')
RELKIND_KINDS = {'r': 'heap', 'm': 'heap', 't': 'toast', 'i': 'index', 'I': 'index',
                 'S': 'sequence'}


def chunk_label(size: int) -> str:
    return f'{size // 1048576}M' if size % 1048576 == 0 else f'{size // 1024}K'


@dataclass(frozen=True)
class FileDiff:
    """Sizes are None when the file is absent on that side."""
    path: str
    old_size: int | None
    new_size: int | None
    changed: tuple[int, ...]  # indexes of differing BLOCK_SIZE blocks

    @property
    def blocks(self) -> int:
        return -(-max(self.old_size or 0, self.new_size or 0) // BLOCK_SIZE)

    @property
    def new_blocks(self) -> int:
        return -(-(self.new_size or 0) // BLOCK_SIZE)


def changed_blocks(old: Path | None, new: Path | None, *, window: int = 1 << 20):
    """Return (old_size, new_size, changed block indexes); None means absent.

    A block differs when its byte ranges differ, so blocks present on only one
    side, or partly beyond the shorter file's end, count as changed.
    """
    if window <= 0 or window % BLOCK_SIZE:
        raise ValueError('window must be a positive multiple of the block size')
    changed: list[int] = []
    sizes = [None if old is None else 0, None if new is None else 0]
    with ExitStack() as stack:
        streams = [stack.enter_context(open(path, 'rb')) if path is not None else None
                   for path in (old, new)]
        offset = 0
        while True:
            parts = [stream.read(window) if stream else b'' for stream in streams]
            if not parts[0] and not parts[1]:
                break
            for side, part in enumerate(parts):
                if streams[side]:
                    sizes[side] += len(part)
            if parts[0] != parts[1]:
                # Bytes slices compare with memcmp; memoryview compares per item.
                a, b = parts
                for start in range(0, max(len(a), len(b)), BLOCK_SIZE):
                    if a[start:start + BLOCK_SIZE] != b[start:start + BLOCK_SIZE]:
                        changed.append((offset + start) // BLOCK_SIZE)
            offset += window
    return sizes[0], sizes[1], tuple(changed)


def regular_file(root: Path, relative: str) -> Path | None:
    """root/relative when every component below root is a real directory and
    the last is a regular file; None (absent) for missing paths, symlinks at
    any level, and other file types. Nothing outside root is ever read."""
    path, parts = root, relative.split('/')
    for depth, part in enumerate(parts, 1):
        path = path / part
        try:
            mode = path.lstat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            return None
        if not (stat.S_ISREG(mode) if depth == len(parts) else stat.S_ISDIR(mode)):
            return None
    return path


def diff_file(path: str, old_root: Path, new_root: Path) -> FileDiff:
    """Compare one relative path; a side that is not a regular file counts as absent."""
    old_size, new_size, changed = changed_blocks(regular_file(old_root, path),
                                                 regular_file(new_root, path))
    return FileDiff(path, old_size, new_size, changed)


def regular_files(root: Path) -> tuple[set[str], list[str]]:
    """Relative POSIX paths of regular files, plus any other entries skipped."""
    files, skipped = set(), []
    for directory, dirs, names in os.walk(root, followlinks=False):
        here = Path(directory)
        for name in list(dirs):
            if (here / name).is_symlink():
                skipped.append((here / name).relative_to(root).as_posix())
                dirs.remove(name)
        for name in names:
            entry = here / name
            relative = entry.relative_to(root).as_posix()
            if entry.is_symlink() or not entry.is_file():
                skipped.append(relative)
            else:
                files.add(relative)
    return files, skipped


def compare_trees(old_root: Path, new_root: Path) -> tuple[list[FileDiff], list[str]]:
    old_files, old_skipped = regular_files(old_root)
    new_files, new_skipped = regular_files(new_root)
    diffs = [diff_file(path, old_root, new_root) for path in sorted(old_files | new_files)]
    return diffs, sorted(set(old_skipped) | set(new_skipped))


def chunk_stats(diff: FileDiff, chunk_sizes=CHUNK_SIZES) -> dict[int, dict]:
    """Per chunk size: union chunk count, changed chunks, new-side chunks, bytes to send.

    send_bytes counts only the new side's bytes inside changed chunks, which is
    what a page-aligned transfer would ship (truncation costs no payload).
    """
    total = max(diff.old_size or 0, diff.new_size or 0)
    new_size = diff.new_size or 0
    result = {}
    for size in chunk_sizes:
        if size % BLOCK_SIZE:
            raise ValueError('chunk sizes must be multiples of the block size')
        per_chunk = size // BLOCK_SIZE
        changed = sorted({block // per_chunk for block in diff.changed})
        result[size] = {
            'chunks': -(-total // size),
            'changed': len(changed),
            'new_chunks': -(-new_size // size),
            'send_bytes': sum(max(0, min(size, new_size - index * size)) for index in changed),
        }
    return result


def empty_totals(chunk_sizes=CHUNK_SIZES) -> dict:
    return {'files': 0, 'files_changed': 0, 'old_bytes': 0, 'new_bytes': 0,
            'chunks': {chunk_label(size): {'chunks': 0, 'changed': 0, 'new_chunks': 0,
                                           'send_bytes': 0} for size in chunk_sizes}}


def add_totals(totals: dict, diff: FileDiff, stats: dict[int, dict]) -> None:
    totals['files'] += 1
    totals['files_changed'] += bool(diff.changed)
    totals['old_bytes'] += diff.old_size or 0
    totals['new_bytes'] += diff.new_size or 0
    for size, values in stats.items():
        bucket = totals['chunks'][chunk_label(size)]
        for key, value in values.items():
            bucket[key] += value


def finish_totals(totals: dict) -> dict:
    """Add changed_fraction (by chunk count) and send_fraction (by new-side bytes).

    Every small file is one chunk at any size, so chunk-count fractions at
    large chunk sizes are diluted by the many tiny catalog files;
    send_fraction is the byte-weighted view.
    """
    for bucket in totals['chunks'].values():
        bucket['changed_fraction'] = (bucket['changed'] / bucket['chunks']
                                      if bucket['chunks'] else None)
        bucket['send_fraction'] = (bucket['send_bytes'] / totals['new_bytes']
                                   if totals.get('new_bytes') else None)
    return totals


def classify(path: str, relmap: dict[str, dict]) -> dict:
    """Area, relation kind, owner and name for one backup-relative path.

    relmap maps a main-fork path such as base/5/16384 to {oid, name, relkind}.
    """
    match = RELATION_FILE.fullmatch(path)
    if not match:
        if path.split('/', 1)[0] == 'pg_wal':
            area = 'wal'
        elif path in BACKUP_METADATA:
            area = 'backup_metadata'
        else:
            area = 'other'
        return {'area': area, 'kind': 'non_relation', 'owner': None, 'relation': None,
                'fork': None, 'segno': None}
    fork = match['fork'] or 'main'
    info = relmap.get(match['key'])
    if info is None:
        kind, owner, name = ('unmapped' if fork == 'main' else fork), 'unknown', None
    else:
        kind = fork if fork != 'main' else RELKIND_KINDS.get(info['relkind'], 'other')
        owner = 'catalog' if info['oid'] < FIRST_NORMAL_OID else 'user'
        name = info['name']
    return {'area': 'relation', 'kind': kind, 'owner': owner, 'relation': name,
            'fork': fork, 'segno': int(match['segno'] or 0)}


def summarize(diffs: list[FileDiff], relmap: dict[str, dict], chunk_sizes=CHUNK_SIZES) -> dict:
    """Aggregate per-file diffs overall ('all'), without WAL and backup
    metadata ('data'), and by area, top directory, relation kind, owner and
    relation (every user relation, plus catalog relations that changed)."""
    groups: dict[str, dict[str, dict]] = {'totals': {}, 'by_area': {}, 'by_directory': {},
                                          'by_kind': {}, 'by_owner': {}, 'by_relation': {}}
    changed_files = []

    def add(group, key, diff, stats):
        add_totals(groups[group].setdefault(key, empty_totals(chunk_sizes)), diff, stats)

    for diff in diffs:
        info = classify(diff.path, relmap)
        stats = chunk_stats(diff, chunk_sizes)
        add('totals', 'all', diff, stats)
        if info['area'] not in ('wal', 'backup_metadata'):
            add('totals', 'data', diff, stats)
        add('by_area', info['area'], diff, stats)
        add('by_directory', diff.path.split('/', 1)[0] if '/' in diff.path else '(top)',
            diff, stats)
        add('by_kind', info['kind'], diff, stats)
        if info['owner']:
            add('by_owner', info['owner'], diff, stats)
            if info['owner'] == 'user' or diff.changed:
                label = info['relation'] or RELATION_FILE.fullmatch(diff.path)['key']
                if info['fork'] != 'main':
                    label += f"[{info['fork']}]"
                add('by_relation', label, diff, stats)
        if diff.changed:
            changed_files.append({'path': diff.path, **info, 'old_size': diff.old_size,
                                  'new_size': diff.new_size, 'blocks': diff.blocks,
                                  'changed_blocks': len(diff.changed)})
    for group in groups.values():
        for totals in group.values():
            finish_totals(totals)
    for name in ('all', 'data'):
        groups['totals'].setdefault(name, finish_totals(empty_totals(chunk_sizes)))
    return {
        'block_size': BLOCK_SIZE,
        'chunk_sizes': [chunk_label(size) for size in chunk_sizes],
        'fraction_denominator': 'chunks covering the longer of the two file versions',
        **groups,
        'only_in_old': [d.path for d in diffs if d.new_size is None],
        'only_in_new': [d.path for d in diffs if d.old_size is None],
        'changed_files': changed_files,
    }
