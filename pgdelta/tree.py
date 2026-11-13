"""Directory inventories: what is in a data directory, and validation of paths received from a peer."""
from __future__ import annotations

import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath


@dataclass
class Entry:
    path: str          # relative, '/'-separated
    kind: str          # 'dir', 'file' or 'link'
    mode: int          # permission bits
    size: int = 0      # files only
    target: str = ''   # links only

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, value: dict) -> 'Entry':
        entry = cls(path=str(value['path']), kind=str(value['kind']), mode=int(value['mode']),
                    size=int(value.get('size', 0)), target=str(value.get('target', '')))
        if entry.kind not in ('dir', 'file', 'link'):
            raise ValueError(f'unknown entry kind {entry.kind!r}')
        safe_relpath(entry.path)
        if entry.size < 0:
            raise ValueError(f'negative size for {entry.path}')
        return entry


def scan(root: Path) -> list[Entry]:
    """Every directory, regular file and symbolic link under `root`, parents before children, sorted.

    Anything else (sockets, pipes, devices) is refused: a data directory has none.
    """
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f'{root} is not a directory')
    entries = []
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        rel_dir = Path(current).relative_to(root)
        for name in sorted(dirs + files):
            full = Path(current) / name
            rel = (rel_dir / name).as_posix()
            info = full.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                entries.append(Entry(rel, 'link', mode, target=os.readlink(full)))
            elif stat.S_ISDIR(info.st_mode):
                entries.append(Entry(rel, 'dir', mode))
            elif stat.S_ISREG(info.st_mode):
                entries.append(Entry(rel, 'file', mode, size=info.st_size))
            else:
                raise ValueError(f'{full} is not a directory, regular file or symbolic link')
        # os.walk doesn't descend into symlinked directories (followlinks=False), so a link to pg_wal
        # elsewhere is recorded as a link, as pg_basebackup's plain format would not follow it either.
    return entries


def safe_relpath(path: str) -> PurePosixPath:
    """`path` as a relative path that can't escape the directory it's joined to."""
    if not path or '\0' in path:
        raise ValueError(f'invalid path {path!r}')
    pure = PurePosixPath(path)
    if pure.is_absolute() or pure.as_posix() != path or any(part in ('.', '..') for part in pure.parts):
        raise ValueError(f'unsafe path {path!r}')
    return pure
