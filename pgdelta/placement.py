"""Where the tool may write: never inside S or B, judged by filesystem identity.

Paths are compared by (st_dev, st_ino), not by their strings. Strings can
name one directory in many ways: a case-insensitive or normalization-
insensitive filesystem ('Basis' and 'basis', NFC and NFD), a symlink, a
bind mount or a hard-linked directory. A directory that does not exist yet
(O) is judged by its parent, which must exist.

Known limit: two mounts that report different st_dev for one directory
(for example the same NFS export mounted twice) cannot be recognized.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

from pgdelta.errors import InvalidInput


def directory_id(path, label):
    """(st_dev, st_ino) of an existing directory, following symlinks."""
    try:
        info = os.stat(path)
    except OSError as exc:
        raise InvalidInput(f'{label} {path} is not accessible: {exc.strerror}') from exc
    if not stat.S_ISDIR(info.st_mode):
        raise InvalidInput(f'{label} {path} is not a directory')
    return info.st_dev, info.st_ino


def lineage(directory):
    """Identities of an existing directory and of every directory above it, up to the root."""
    current = os.path.realpath(directory)
    chain = []
    while True:
        info = os.stat(current)
        chain.append((info.st_dev, info.st_ino))
        parent = os.path.dirname(current)
        if parent == current:
            return chain
        current = parent


def refuse_inside(directory, protected, what):
    """Raise InvalidInput if `directory` is, or lies inside, a protected directory.

    protected: {label: (path, set of directory identities)}. The set holds
    the protected root and, where it was scanned, every directory below it,
    so a mount of one of its subdirectories elsewhere is recognized too.
    """
    try:
        chain = lineage(directory)
    except OSError as exc:
        raise InvalidInput(f'cannot inspect {directory}: {exc.strerror}') from exc
    for label, (path, identities) in protected.items():
        if any(identity in identities for identity in chain):
            raise InvalidInput(f'{what} overlaps {label} {path}: {directory} is the {label} or lies '
                               f'inside it (compared by filesystem identity, not by path string)')


def check_new_output(output, protected):
    """O must not exist, its parent must, and the parent must lie outside every protected directory."""
    output = Path(output)
    if os.path.lexists(output):
        raise InvalidInput(f'output {output} already exists; refusing to overwrite it')
    if not output.parent.is_dir():
        raise InvalidInput(f'output parent directory {output.parent} does not exist')
    refuse_inside(output.parent, protected, f'output {output}')


def check_local(source, basis, output):
    """Local mode: S and B are distinct, neither lies inside the other, and O lies outside both."""
    source, basis = Path(source), Path(basis)
    source_id, basis_id = directory_id(source, 'source'), directory_id(basis, 'basis')
    if source_id == basis_id:
        raise InvalidInput(f'source {source} and basis {basis} overlap: they are the same directory')
    refuse_inside(basis, {'source': (source, {source_id})}, f'basis {basis}')
    refuse_inside(source, {'basis': (basis, {basis_id})}, f'source {source}')
    check_new_output(output, {'source': (source, {source_id}), 'basis': (basis, {basis_id})})
