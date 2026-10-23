"""Directory inventories and validation of relative paths received from a peer.

v0 handles plain-format backups only: regular files and directories. Symlinks,
tablespaces and special files are listed by scan() and refused by
require_plain(), so nothing outside the given root is ever followed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat

from pgdelta.errors import InvalidInput, UnsafePath

MAX_PATH_BYTES = 4096
MAX_NAME_BYTES = 255
MAX_FILE_SIZE = (1 << 63) - 1
MODE_MASK = 0o777


@dataclass(frozen=True)
class FileEntry:
    path: str
    size: int
    mode: int


@dataclass(frozen=True)
class DirEntry:
    path: str
    mode: int


@dataclass
class Inventory:
    root_mode: int
    directories: list = field(default_factory=list)
    files: list = field(default_factory=list)
    symlinks: list = field(default_factory=list)
    special: list = field(default_factory=list)
    # (st_dev, st_ino) of the root and every directory below it, for placement checks.
    directory_ids: set = field(default_factory=set)

    def file_map(self):
        return {entry.path: entry for entry in self.files}


def scan(root):
    """List everything under root without following symlinks; paths are relative POSIX strings."""
    root = Path(root)
    try:
        root_stat = os.stat(root)
    except OSError as exc:
        raise InvalidInput(f'cannot read directory {root}: {exc.strerror}') from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise InvalidInput(f'not a directory: {root}')
    inventory = Inventory(root_mode=stat.S_IMODE(root_stat.st_mode) & MODE_MASK,
                          directory_ids={(root_stat.st_dev, root_stat.st_ino)})
    pending = ['']
    while pending:
        relative = pending.pop()
        try:
            with os.scandir(root / relative if relative else root) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
        except OSError as exc:
            raise InvalidInput(f'cannot list {root / relative}: {exc.strerror}') from exc
        for entry in children:
            path = f'{relative}/{entry.name}' if relative else entry.name
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise InvalidInput(f'cannot stat {root / path}: {exc.strerror}') from exc
            mode = stat.S_IMODE(info.st_mode) & MODE_MASK
            if stat.S_ISDIR(info.st_mode):
                inventory.directories.append(DirEntry(path, mode))
                inventory.directory_ids.add((info.st_dev, info.st_ino))
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                inventory.files.append(FileEntry(path, info.st_size, mode))
            elif stat.S_ISLNK(info.st_mode):
                inventory.symlinks.append(path)
            else:
                inventory.special.append(path)
    for name in ('directories', 'files'):
        getattr(inventory, name).sort(key=lambda entry: entry.path)
    inventory.symlinks.sort()
    inventory.special.sort()
    return inventory


def _sample(paths):
    shown = ', '.join(paths[:3])
    return shown + (f' and {len(paths) - 3} more' if len(paths) > 3 else '')


def require_plain(inventory, label='directory'):
    tablespaces = [path for path in (
        [entry.path for entry in inventory.directories + inventory.files]
        + inventory.symlinks + inventory.special) if path.startswith('pg_tblspc/')]
    if tablespaces:
        raise InvalidInput(f'{label} has tablespaces, which v0 does not support '
                           f'(pg_tblspc contains {_sample(sorted(tablespaces))})')
    if inventory.symlinks:
        raise InvalidInput(f'{label} contains symlinks, which v0 does not support: '
                           f'{_sample(inventory.symlinks)}')
    if inventory.special:
        raise InvalidInput(f'{label} contains special files (not regular files or directories): '
                           f'{_sample(inventory.special)}')
    return inventory


def validate_relpath(path):
    """Accept only normalized, relative POSIX paths that stay below the root."""
    if not isinstance(path, str) or not path:
        raise UnsafePath(f'invalid path {path!r}: must be a non-empty string')
    if '\x00' in path or '\\' in path:
        raise UnsafePath(f'invalid path {path!r}: contains NUL or backslash')
    if path.startswith('/'):
        raise UnsafePath(f'invalid path {path!r}: absolute paths are not allowed')
    try:
        encoded = os.fsencode(path)
    except UnicodeError as exc:
        raise UnsafePath(f'invalid path {path!r}: not encodable') from exc
    if len(encoded) > MAX_PATH_BYTES:
        raise UnsafePath(f'invalid path {path[:64]!r}...: longer than {MAX_PATH_BYTES} bytes')
    for part in path.split('/'):
        if part in ('', '.', '..'):
            raise UnsafePath(f'invalid path {path!r}: empty, "." or ".." component')
        if len(os.fsencode(part)) > MAX_NAME_BYTES:
            raise UnsafePath(f'invalid path {path!r}: component longer than {MAX_NAME_BYTES} bytes')
    return path


def _mode(value, path):
    if type(value) is not int or not 0 <= value <= MODE_MASK:
        raise InvalidInput(f'invalid permission bits {value!r} for {path!r}')
    return value


def parse_entries(directories, files):
    """Validate a peer's directory and file lists; return (DirEntry list, FileEntry list).

    Paths must be unique, every parent must be a listed directory (or the
    root), and nothing may be both a file and a directory.
    """
    if not isinstance(directories, list) or not isinstance(files, list):
        raise InvalidInput('inventory must contain directory and file lists')
    dirs, seen = [], set()
    for item in directories:
        if not isinstance(item, list) or len(item) != 2:
            raise InvalidInput(f'invalid directory entry {item!r}')
        path = validate_relpath(item[0])
        if path in seen:
            raise InvalidInput(f'duplicate path {path!r}')
        seen.add(path)
        dirs.append(DirEntry(path, _mode(item[1], path)))
    directory_set = set(seen)
    entries = []
    for item in files:
        if not isinstance(item, list) or len(item) != 3:
            raise InvalidInput(f'invalid file entry {item!r}')
        path = validate_relpath(item[0])
        size = item[1]
        if type(size) is not int or not 0 <= size <= MAX_FILE_SIZE:
            raise InvalidInput(f'invalid size {size!r} for {path!r}')
        if path in seen:
            raise InvalidInput(f'duplicate path {path!r}')
        seen.add(path)
        entries.append(FileEntry(path, size, _mode(item[2], path)))
    for entry in dirs + entries:
        parent = entry.path.rpartition('/')[0]
        if parent and parent not in directory_set:
            raise InvalidInput(f'{entry.path!r} has no listed parent directory')
    return dirs, entries
