"""In-place refresh (--in-place): B itself becomes the refreshed copy, blocked from starting until it is.

Owner decision O5 (docs/DECISIONS.md, October 23, 2026): B may be patched
in place. A failed refresh may leave B unusable, provided PostgreSQL
refuses to start on it until a refresh completes.

How B is blocked: before the first change to B, the marker
B/pgdelta-incomplete.json is written atomically, then B's
global/pg_control is renamed to global/pg_control.pgdelta-incomplete.
PostgreSQL cannot start without global/pg_control. The source's
pg_control is always sent whole and written to that renamed file during
the run. Renaming it back is the commit: the only point after which B is
startable. Nothing is ever rolled back, and the marker is only removed
after the commit, so a B that holds the marker is incomplete and must be
refreshed again (a rerun takes B's identity from the marker) or replaced
by a full copy.

One run at a time: a run holds an exclusive lock on B's root directory
from its checks to its end (BasisLock), and commits only while B still
holds its own marker (run_id) and renamed pg_control (check_owned).

This module holds the steps on B's tree; pgdelta.receiver runs them in order.
"""
from __future__ import annotations

from datetime import datetime, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import stat

from pgdelta import __version__
from pgdelta.errors import InvalidInput, TransferError
from pgdelta.identity import check_major
from pgdelta.inventory import scan
from pgdelta.tree import open_regular

MARKER = 'pgdelta-incomplete.json'
MARKER_SCHEMA_VERSION = 1
CONTROL_FILE = 'global/pg_control'
CONTROL_PENDING = 'global/pg_control.pgdelta-incomplete'
RESERVED = (MARKER, CONTROL_PENDING)
# What the receiver's messages say, by which the sender tells whether B was left blocked or was committed.
BLOCKED = 'is incomplete and blocked from starting'
COMMITTED = 'was committed in place'
_UPDATE_FLAGS = (os.O_RDWR | os.O_CREAT | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0))
_CREATE_FLAGS = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_CLOEXEC', 0)
                 | getattr(os, 'O_NOFOLLOW', 0))


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def target(path):
    """Where the source file `path` is written in B during the run: pg_control goes to its renamed file."""
    return CONTROL_PENDING if path == CONTROL_FILE else path


def incomplete_note(basis):
    return (f'{basis} {BLOCKED} (its global/pg_control is renamed and {MARKER} marks it): refresh it again '
            f'with --in-place, or replace it with a full copy')


class BasisBusy(InvalidInput):
    """Another in-place run holds B."""


class BasisLock:
    """An exclusive flock on B's root directory, held from prepare_receiver until the run ends.

    Taken without waiting: a second in-place run on the same B, by any path
    to it, is refused while the first one lives. The kernel drops the lock
    when its process dies, so a rerun after a crash takes it at once. A new
    copy (--output) only reads B and takes none.
    """

    def __init__(self, basis):
        self.fd = None
        try:
            fd = os.open(basis, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_CLOEXEC', 0))
        except OSError as exc:
            raise InvalidInput(f'cannot open basis {basis} to lock it: {exc.strerror}') from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise BasisBusy(f'basis {basis} is being refreshed in place by another run, which holds its lock; '
                            f'not touching it') from None
        except OSError as exc:
            os.close(fd)
            raise InvalidInput(f'cannot lock basis {basis} for an in-place refresh: {exc.strerror}') from exc
        self.fd = fd

    def release(self):
        fd, self.fd = self.fd, None
        if fd is not None:
            os.close(fd)

    def __del__(self):
        self.release()


def is_blocked(basis):
    """Whether an in-place refresh left B blocked: its marker is there and its global/pg_control is not.

    A marker beside global/pg_control is a run that stopped before renaming
    it (B unchanged) or after the commit (B complete): B can start either way.
    """
    basis = Path(basis)
    return os.path.lexists(basis / MARKER) and not os.path.lexists(basis / CONTROL_FILE)


def read_marker(basis):
    """The marker of an earlier in-place refresh that did not commit, or None if B has none."""
    path = Path(basis) / MARKER
    if not os.path.lexists(path):
        return None
    try:
        fd = open_regular(path)
        with open(fd, 'rb') as stream:
            record = json.loads(stream.read().decode('utf-8'))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise InvalidInput(f'{path} is unreadable ({exc}); {incomplete_note(basis)}') from exc
    identity = record.get('basis_identity') if isinstance(record, dict) else None
    if (not isinstance(record, dict) or record.get('schema_version') != MARKER_SCHEMA_VERSION
            or not (identity is None or isinstance(identity, dict))):
        raise InvalidInput(f'{path} is not a pgdelta marker of schema version {MARKER_SCHEMA_VERSION}; '
                           f'{incomplete_note(basis)}')
    return record


def check_basis(basis, marker):
    """Refuse a B whose control files cannot be told apart from the ones this mode creates."""
    basis = Path(basis)
    control, pending = os.path.lexists(basis / CONTROL_FILE), os.path.lexists(basis / CONTROL_PENDING)
    if pending and marker is None:
        raise InvalidInput(f'basis {basis} has {CONTROL_PENDING} but no {MARKER}: it was not left by '
                           f'pgdelta; refusing to replace it')
    if pending and control:
        raise InvalidInput(f'basis {basis} has both {CONTROL_FILE} and {CONTROL_PENDING}, and {MARKER}: '
                           f'refusing to choose between them; {incomplete_note(basis)}')


def check_postgresql(basis, inventory, marker):
    """B must be a copy of a PostgreSQL data directory (PG_VERSION and global/pg_control), or blocked by pgdelta.

    An in-place refresh deletes whatever the source does not list, so a
    mistyped --basis (a home directory, say) must never get that far. A B
    an earlier run blocked has no global/pg_control; its marker vouches for
    it instead.
    """
    if marker is not None:
        return
    paths = {entry.path for entry in inventory.files}
    missing = [name for name in ('PG_VERSION', CONTROL_FILE) if name not in paths]
    if missing:
        raise InvalidInput(f'basis {basis} is not a copy of a PostgreSQL data directory (it has no '
                           f'{" and no ".join(missing)}, and no {MARKER}): an in-place refresh deletes what the '
                           f'source lacks, so it refuses this directory; B is untouched')
    check_major(basis, 'basis')


def check_links(basis, inventory):
    """Refuse a B with a file that has another hard link: writing it in place would change that name too.

    Such a B shares inodes with another copy (cp -al, rsync --link-dest,
    pg_combinebackup --link, pg_upgrade --link) or within itself; a new copy
    (--output) never writes B and is unaffected.
    """
    basis, shared = Path(basis), []
    for entry in inventory.files:
        try:
            links = os.lstat(basis / entry.path).st_nlink
        except OSError as exc:
            raise InvalidInput(f'cannot stat basis file {entry.path}: {exc.strerror}') from exc
        if links > 1:
            shared.append(entry.path)
    if shared:
        sample = ', '.join(shared[:3]) + (f' and {len(shared) - 3} more' if len(shared) > 3 else '')
        raise InvalidInput(f'basis {basis} has files with more than one hard link ({sample}): writing them in '
                           f'place would change every other name for them too, such as a copy made with '
                           f'cp -al, rsync --link-dest or pg_combinebackup --link; build a new copy with '
                           f'--output instead')


def check_source(files, directories):
    """The source must have global/pg_control (it is what blocks startup) and none of the reserved names."""
    paths = {entry.path for entry in files} | {entry.path for entry in directories}
    clashes = sorted(paths & set(RESERVED))
    if clashes:
        raise InvalidInput(f'the source lists {", ".join(clashes)}, which an in-place refresh reserves')
    if not any(entry.path == CONTROL_FILE for entry in files):
        raise InvalidInput(f'an in-place refresh needs a PostgreSQL source with {CONTROL_FILE}: '
                           f'renaming it is what keeps B from starting until the refresh commits')


def check_appended(files):
    """The files a live source appends after the listed ones (its WAL and backup_label) take no reserved name either."""
    clashes = sorted({entry.path for entry in files} & set(RESERVED))
    if clashes:
        raise InvalidInput(f'the source appends {", ".join(clashes)}, which an in-place refresh reserves')


def new_marker(basis, previous, basis_identity, source_identity):
    now = _utc_now()
    previous = previous or {}
    attempt = previous.get('attempt')
    return {
        'schema_version': MARKER_SCHEMA_VERSION,
        'tool': 'pgdelta',
        'tool_version': __version__,
        'status': 'incomplete',
        'basis': os.path.abspath(basis),
        'started_at': now,
        'first_started_at': previous.get('first_started_at') or previous.get('started_at') or now,
        'attempt': attempt + 1 if type(attempt) is int else 1,
        # This run's own: it commits only a B whose marker still has it (check_owned).
        'run_id': secrets.token_hex(16),
        # B's identity as read when its first in-place refresh started; a rerun reuses it, since
        # pg_controldata cannot read B while its pg_control is renamed.
        'basis_identity': basis_identity,
        'source_identity': source_identity,
        'note': incomplete_note(basis),
    }


def check_owned(basis, run_id):
    """B still holds this run's marker and renamed pg_control: checked before deleting anything and before the commit.

    The lock keeps a second pgdelta run away; this catches a writer it
    cannot see (another host, or a marker replaced by hand).
    """
    basis = Path(basis)
    try:
        record = read_marker(basis)
    except InvalidInput as exc:
        record, reason = None, str(exc)
    else:
        reason = f'{MARKER} is missing' if record is None else f'{MARKER} is another run\'s'
    if record is not None and record.get('run_id') == run_id:
        if os.path.lexists(basis / CONTROL_PENDING):
            return
        reason = f'{CONTROL_PENDING} is missing'
    raise TransferError(f'{basis} was changed by another process during this run ({reason}), so B is not this '
                        f'run\'s to finish: not deleting anything and not committing')


def write_atomically(path, data, mode=0o600):
    """Replace `path` with `data`: temporary file beside it, fsync, rename, fsync of the directory."""
    path = Path(path)
    temporary = path.with_name(f'.pgdelta-{secrets.token_hex(8)}.tmp')
    fd = os.open(temporary, _CREATE_FLAGS, mode)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    fsync_dir(path.parent)


def write_marker(basis, record):
    write_atomically(Path(basis) / MARKER, (json.dumps(record, indent=2) + '\n').encode('utf-8'))


def remove_receipt(path):
    """Remove a receipt left beside B by an earlier refresh, so it can never vouch for the B being changed."""
    path = Path(path)
    if not os.path.lexists(path):
        return
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise InvalidInput(f'receipt path {path} exists and is not a regular file')
    os.unlink(path)
    fsync_dir(path.parent)


def block_startup(basis):
    """Rename B's global/pg_control out of the way (if B has one), durably: B cannot start from here on."""
    basis = Path(basis)
    if not os.path.lexists(basis / CONTROL_FILE):
        return False
    os.rename(basis / CONTROL_FILE, basis / CONTROL_PENDING)
    fsync_dir(basis / 'global')
    return True


def _writable_directory(path, info):
    if stat.S_IMODE(info.st_mode) & 0o700 != 0o700:
        os.chmod(path, stat.S_IMODE(info.st_mode) | 0o700)


def _remove_tree(path):
    """Delete a directory tree of B, raising on any failure; return (files, directories) removed.

    Read-only directories are made writable first.
    """
    os.chmod(path, 0o700)
    files, directories = 0, 1
    for current, subdirectories, names in os.walk(path):
        for name in subdirectories:
            os.chmod(os.path.join(current, name), 0o700)
        files, directories = files + len(names), directories + len(subdirectories)
    shutil.rmtree(path)
    return files, directories


def prepare_tree(basis, directories, files):
    """Give B every directory of the source (mode 0700 until the end) and clear paths whose type differs.

    A non-directory of B where the source has a directory is deleted; a
    directory of B where the source has a file is deleted with its
    contents. Neither can be a basis file: the gate pairs files by path.
    Existing directories are made writable by their owner (final modes are
    applied after the run). Returns {directories_created, files_removed,
    directories_removed}.
    """
    basis = Path(basis)
    _writable_directory(basis, os.stat(basis))
    counts = dict.fromkeys(('directories_created', 'files_removed', 'directories_removed'), 0)
    for entry in directories:  # sorted by path, so parents come first
        path = basis / entry.path
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISDIR(info.st_mode):
            os.unlink(path)
            counts['files_removed'] += 1
            info = None
        if info is None:
            os.mkdir(path, 0o700)
            counts['directories_created'] += 1
        else:
            _writable_directory(path, info)
    for entry in files:
        path = basis / target(entry.path)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(info.st_mode):
            removed_files, removed_directories = _remove_tree(path)
            counts['files_removed'] += removed_files
            counts['directories_removed'] += removed_directories
    return counts


def _refuse_shared(path, info):
    if info.st_nlink != 1:
        raise TransferError(f'{path} has {info.st_nlink} hard links now (B changed during the run): writing it '
                            f'in place would change the other names too; not writing it')


def open_for_update(path):
    """B's file for writing in place: read-write, created (mode 0600) if missing, never truncated, no symlink.

    A file B holds read-only is made writable by its owner first; its final
    mode is applied once it is verified. A file with another hard link is
    refused (check_links refused those at the start), before any change.
    """
    try:
        fd = os.open(path, _UPDATE_FLAGS, 0o600)
    except PermissionError:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            raise
        _refuse_shared(path, info)
        os.chmod(path, stat.S_IMODE(info.st_mode) | 0o600)
        fd = os.open(path, _UPDATE_FLAGS, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, 'not a regular file', str(path))
        _refuse_shared(path, info)
    except BaseException:
        os.close(fd)
        raise
    return fd


def remove_extraneous(basis, directories, files):
    """Delete what B holds and the source does not list; return (files removed, directories removed).

    Kept: the source's directories, the files it lists (pg_control at its
    renamed path) and the marker, which goes only after the commit. B is
    scanned again, so whatever this run created or found is covered. Every
    kept path must have the source's type by now; otherwise B changed under
    the run and nothing is deleted.
    """
    basis = Path(basis)
    inventory = scan(basis)
    kept_files = {target(entry.path) for entry in files} | {MARKER}
    kept_directories = {entry.path for entry in directories}
    found_files = {entry.path for entry in inventory.files}
    found_directories = {entry.path for entry in inventory.directories}
    wrong = sorted((kept_files - {MARKER} - found_files) | (kept_directories - found_directories))
    if wrong:
        raise TransferError(f'{basis} changed during the run: {", ".join(wrong[:3])} '
                            f'{"is" if len(wrong) == 1 else "are"} missing or of another type')
    # A directory the source lacks holds nothing kept, since every kept path's parents are listed.
    extraneous = sorted(found_directories - kept_directories, key=lambda path: path.count('/'), reverse=True)
    for path in extraneous:
        _writable_directory(basis / path, os.lstat(basis / path))
    removed_files = 0
    for path in sorted(found_files | set(inventory.symlinks) | set(inventory.special)):
        if path not in kept_files:
            os.unlink(basis / path)
            removed_files += 1
    for path in extraneous:  # deepest first
        os.rmdir(basis / path)
    return removed_files, len(extraneous)


def commit_control(basis):
    """The commit: rename the source's pg_control, written during the run, into place."""
    basis = Path(basis)
    os.rename(basis / CONTROL_PENDING, basis / CONTROL_FILE)


def remove_marker(basis):
    """Remove the marker after the commit (a repeat after an interrupt finds it gone, which is fine)."""
    try:
        os.unlink(Path(basis) / MARKER)
    except FileNotFoundError:
        pass
