"""A stopped data directory read directly as the source (--source-kind datadir), for T8a.

v0's source is a finished backup S. With --source-kind datadir the sender
reads a cluster's own data directory instead, which must be cleanly shut
down: pg_controldata must report "Database cluster state: shut down", and
postmaster.pid must be absent. It then sends what pg_basebackup would
send, by the exclusion rules of PostgreSQL 17's base backup
(src/backend/backup/basebackup.c, sendDir), applied by name at every depth
as sendDir applies them:

- "temporary": files and directories whose names start with pgsql_tmp;
- "macos": .DS_Store files;
- "named_files": postgresql.auto.conf.tmp, current_logfiles.tmp,
  backup_label, tablespace_map, backup_manifest, postmaster.pid,
  postmaster.opts, and every file whose name starts with pg_internal.init;
- "unlogged_relations": in a database directory (base/<oid>), every fork
  of an unlogged relation except its _init fork; a relation is unlogged
  when <relfilenode>_init exists beside it;
- "temporary_relations": in a database directory, temporary relation files
  (t<backend>_<relfilenode>...);
- "directory_contents": the contents of pg_stat_tmp, pg_replslot,
  pg_dynshmem, pg_notify, pg_serial, pg_snapshots and pg_subtrans; the
  directories themselves are kept, empty;
- "wal": pg_wal/ is kept, with pg_wal/archive_status/ and
  pg_wal/summaries/ (empty), but of its files only the segment named by
  pg_controldata's "Latest checkpoint's REDO WAL file". After a clean
  shutdown that segment holds the shutdown checkpoint record, so the copy
  starts without recovery.

Unlogged relations need one more step. pg_basebackup can leave out their
main forks because its output always goes through recovery, and recovery
ends by copying each init fork over its main fork (ResetUnloggedRelations).
This copy starts without recovery, so PostgreSQL would open the main fork
as it is: absent, the relation is unreadable ("could not open file"). So
for each init fork <relfilenode>_init[.segment] the main fork
<relfilenode>[.segment] is sent with the init fork's bytes, which is what
that reset leaves: the relation exists and is empty. Selection.sources maps
those listed paths to the file actually read.

Symlinks (a pg_wal on another disk, tablespaces) are refused, as for a
backup source. select() returns the inventory the sender lists and what was
excluded; `python -m pgdelta.datadir list DIR --pg-controldata PATH` prints
the selected files' relative paths as JSON, so a harness can hash exactly
the files pgdelta sends (`--sources` prints [path, path read] pairs, which
differ for the reset main forks).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import stat
import sys

from pgdelta.errors import InvalidInput, PgDeltaError, VerificationError
from pgdelta.identity import controldata_value, read_controldata
from pgdelta.inventory import MODE_MASK, DirEntry, FileEntry, Inventory, require_plain

SOURCE_KINDS = ('backup', 'datadir')
DEFAULT_SOURCE_KIND = 'backup'
SHUT_DOWN = 'shut down'
REDO_WAL_FILE_LABEL = "Latest checkpoint's REDO WAL file"
EXCLUDED_DIRECTORY_CONTENTS = ('pg_stat_tmp', 'pg_replslot', 'pg_dynshmem', 'pg_notify', 'pg_serial',
                               'pg_snapshots', 'pg_subtrans')
EXCLUDED_FILES = ('postgresql.auto.conf.tmp', 'current_logfiles.tmp', 'backup_label', 'tablespace_map',
                  'backup_manifest', 'postmaster.pid', 'postmaster.opts')
EXCLUDED_PREFIXES = ('pg_internal.init',)
TEMPORARY_PREFIX = 'pgsql_tmp'
MACOS_FILES = ('.DS_Store',)
WAL_DIRECTORY = 'pg_wal'
WAL_KEPT_DIRECTORIES = ('archive_status', 'summaries')
RULES = ('temporary', 'macos', 'named_files', 'unlogged_relations', 'temporary_relations',
         'directory_contents', 'wal')
# PostgreSQL's parse_filename_for_nontemp_relation and looks_like_temp_rel_name.
_RELATION = re.compile(r'([1-9][0-9]*)(?:_(fsm|vm|init))?(?:\.[1-9][0-9]*)?')
_TEMPORARY_RELATION = re.compile(r't[0-9]+_[0-9]+(?:_(?:fsm|vm|init))?(?:\.[0-9]+)?')
_DATABASE_DIRECTORY = re.compile(r'base/[0-9]+')
_WAL_FILE_NAME = re.compile(r'[0-9A-F]{24}')
_MAX_OID = (1 << 32) - 1


@dataclass
class Selection:
    """What --source-kind datadir sends from a stopped data directory, and what it leaves out."""
    inventory: Inventory
    identity: dict
    redo_wal_file: str
    excluded: dict = field(default_factory=dict)
    # {listed path: path read instead}: the main forks of unlogged relations, sent as their init forks.
    sources: dict = field(default_factory=dict)

    def source_path(self, path):
        """The file read for the listed `path`, relative to the data directory."""
        return self.sources.get(path, path)

    def excluded_summary(self):
        """{files, directories, bytes, by_rule, unlogged_main_forks_reset} of what was left out or replaced.

        by_rule: {rule: {files, directories, bytes}}. The main forks reset
        from their init forks are counted as excluded (under
        unlogged_relations) and listed with the init fork's size.
        """
        totals = {key: sum(counts[key] for counts in self.excluded.values())
                  for key in ('files', 'directories', 'bytes')}
        return dict(totals, by_rule={rule: dict(counts) for rule, counts in self.excluded.items()},
                    unlogged_main_forks_reset=len(self.sources))


def _relation(name):
    """(relfilenode, fork or None, segment suffix) of a relation file name, or None."""
    match = _RELATION.fullmatch(name)
    if match is None or int(match.group(1)) > _MAX_OID:
        return None
    return match.group(1), match.group(2), name[match.end(2) if match.group(2) else len(match.group(1)):]


def _named_rule(name, is_directory, database, names):
    """The rule that leaves out the entry `name` of a directory, or None (basebackup.c's sendDir order)."""
    if name.startswith(TEMPORARY_PREFIX):
        return 'temporary'
    if name in MACOS_FILES:
        return 'macos'
    if name in EXCLUDED_FILES or name.startswith(EXCLUDED_PREFIXES):
        return 'named_files'
    if database and not is_directory:
        relation = _relation(name)
        if relation and relation[1] != 'init' and f'{relation[0]}_init' in names:
            return 'unlogged_relations'
        if _TEMPORARY_RELATION.fullmatch(name):
            return 'temporary_relations'
    return None


def _measure(path, info):
    """(files, directories, bytes) of an entry, a directory counted with all it holds (no symlink followed)."""
    if not stat.S_ISDIR(info.st_mode):
        return 1, 0, info.st_size if stat.S_ISREG(info.st_mode) else 0
    files, directories, size = 0, 1, 0
    for child in _children(path):
        counts = _measure(child.path, _stat(child))
        files, directories, size = files + counts[0], directories + counts[1], size + counts[2]
    return files, directories, size


def _children(directory):
    try:
        with os.scandir(directory) as entries:
            return sorted(entries, key=lambda entry: entry.name)
    except OSError as exc:
        raise InvalidInput(f'cannot list {directory}: {exc.strerror}') from exc


def _stat(entry):
    try:
        return entry.stat(follow_symlinks=False)
    except OSError as exc:
        raise InvalidInput(f'cannot stat {entry.path}: {exc.strerror}') from exc


def _refuse_running(root):
    if os.path.lexists(root / 'postmaster.pid'):
        raise InvalidInput(f'{root} contains postmaster.pid: a server may be running on it; '
                           f'--source-kind datadir needs a cleanly stopped cluster')


def _check_stopped(root, identity, output):
    """The cluster is cleanly shut down, by pg_controldata; return the name of its REDO WAL segment."""
    if identity.get('untrustworthy'):
        raise InvalidInput(f'cannot tell whether {root} is shut down: {identity["untrustworthy"]}')
    state = identity.get('cluster_state')
    if state != SHUT_DOWN:
        raise InvalidInput(f'{root}: pg_controldata reports cluster state {state!r}, not {SHUT_DOWN!r}; '
                           f'--source-kind datadir needs a cleanly stopped cluster (pg_ctl stop -m fast or smart)')
    redo = controldata_value(output, REDO_WAL_FILE_LABEL)
    if not redo or not _WAL_FILE_NAME.fullmatch(redo):
        raise InvalidInput(f'pg_controldata output for {root} lacks a valid {REDO_WAL_FILE_LABEL!r}: {redo!r}')
    return redo


def select(datadir, pg_controldata):
    """The files and directories of a cleanly stopped data directory that pg_basebackup would send.

    Raises InvalidInput (exit 2) without pg_controldata, unless the cluster
    is shut down and has no postmaster.pid, when the REDO WAL segment is
    missing, or when what is selected holds symlinks, special files or
    tablespaces.
    """
    if not pg_controldata:
        raise InvalidInput('--source-kind datadir needs --pg-controldata: it reads the cluster state and '
                           'the REDO WAL file from it')
    root = Path(datadir)
    try:
        root_stat = os.stat(root)
    except OSError as exc:
        raise InvalidInput(f'cannot read directory {root}: {exc.strerror}') from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise InvalidInput(f'not a directory: {root}')
    _refuse_running(root)
    identity, output = read_controldata(root, pg_controldata)
    redo = _check_stopped(root, identity, output)
    inventory = Inventory(root_mode=stat.S_IMODE(root_stat.st_mode) & MODE_MASK,
                          directory_ids={(root_stat.st_dev, root_stat.st_ino)})
    selection = Selection(inventory, identity, redo, {rule: {'files': 0, 'directories': 0, 'bytes': 0}
                                                      for rule in RULES})

    def exclude(rule, path, info):
        files, directories, size = _measure(path, info)
        counts = selection.excluded[rule]
        counts['files'] += files
        counts['directories'] += directories
        counts['bytes'] += size

    def keep_directory(path, info):
        inventory.directories.append(DirEntry(path, stat.S_IMODE(info.st_mode) & MODE_MASK))
        inventory.directory_ids.add((info.st_dev, info.st_ino))

    def keep_contents_out(entry, rule):
        for child in _children(entry.path):
            exclude(rule, child.path, _stat(child))

    pending, wal_found = [''], False
    while pending:
        relative = pending.pop()
        children = _children(root / relative if relative else root)
        names = {entry.name for entry in children}
        database = bool(_DATABASE_DIRECTORY.fullmatch(relative))
        for entry in children:
            path = f'{relative}/{entry.name}' if relative else entry.name
            info = _stat(entry)
            is_directory = stat.S_ISDIR(info.st_mode)
            rule = _named_rule(entry.name, is_directory, database, names)
            if rule:
                exclude(rule, entry.path, info)
            elif is_directory and entry.name in EXCLUDED_DIRECTORY_CONTENTS:
                keep_directory(path, info)
                keep_contents_out(entry, 'directory_contents')
            elif is_directory and path == WAL_DIRECTORY:
                keep_directory(path, info)
                for child in _children(entry.path):
                    child_info = _stat(child)
                    if child.name == redo and stat.S_ISREG(child_info.st_mode):
                        inventory.files.append(FileEntry(f'{path}/{child.name}', child_info.st_size,
                                                         stat.S_IMODE(child_info.st_mode) & MODE_MASK))
                        wal_found = True
                    elif child.name in WAL_KEPT_DIRECTORIES and stat.S_ISDIR(child_info.st_mode):
                        keep_directory(f'{path}/{child.name}', child_info)
                        keep_contents_out(child, 'wal')
                    else:
                        exclude('wal', child.path, child_info)
            elif is_directory:
                keep_directory(path, info)
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                mode = stat.S_IMODE(info.st_mode) & MODE_MASK
                inventory.files.append(FileEntry(path, info.st_size, mode))
                relation = _relation(entry.name) if database else None
                if relation and relation[1] == 'init':
                    # What recovery's reset of unlogged relations would leave: the init fork as main fork.
                    main = f'{relative}/{relation[0]}{relation[2]}'
                    inventory.files.append(FileEntry(main, info.st_size, mode))
                    selection.sources[main] = path
            elif stat.S_ISLNK(info.st_mode):
                inventory.symlinks.append(path)
            else:
                inventory.special.append(path)
    if not wal_found:
        raise InvalidInput(f'{root}: the REDO WAL segment {WAL_DIRECTORY}/{redo} named by pg_controldata '
                           f'is missing; without it the copy cannot start')
    for name in ('directories', 'files'):
        getattr(inventory, name).sort(key=lambda entry: entry.path)
    inventory.symlinks.sort()
    inventory.special.sort()
    require_plain(inventory, 'source data directory')
    return selection


def check_unchanged(datadir, pg_controldata, selection):
    """Before COMPLETE: the cluster must still be stopped at the same checkpoint, so what was sent is consistent.

    A server started (and perhaps stopped again) during the run would have
    changed files after they were read; its next checkpoint moves the REDO
    location, and while it runs postmaster.pid exists.
    """
    root = Path(datadir)
    if os.path.lexists(root / 'postmaster.pid'):
        raise VerificationError(f'{root} gained postmaster.pid during the run: a server started on it, so the '
                                f'files sent may be inconsistent')
    identity, output = read_controldata(root, pg_controldata)
    redo = controldata_value(output, REDO_WAL_FILE_LABEL)
    if identity != selection.identity or redo != selection.redo_wal_file:
        changed = sorted(key for key in set(identity) | set(selection.identity)
                         if identity.get(key) != selection.identity.get(key))
        raise VerificationError(f'{root} changed during the run (pg_controldata now differs in '
                                f'{", ".join(changed) or "the REDO WAL file"}): a server ran on it, so the '
                                f'files sent may be inconsistent')


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='python -m pgdelta.datadir',
        description='What --source-kind datadir selects from a cleanly stopped data directory.')
    commands = parser.add_subparsers(dest='command', required=True)
    listing = commands.add_parser('list', help='print the relative paths of the selected files as a JSON list')
    listing.add_argument('datadir', type=Path)
    listing.add_argument('--pg-controldata', required=True, metavar='PATH')
    listing.add_argument('--sources', action='store_true',
                         help='print [path, path read] pairs instead: they differ for the main fork of an '
                              'unlogged relation, which is sent with its init fork\'s bytes')
    args = parser.parse_args(argv)
    try:
        selection = select(args.datadir, args.pg_controldata)
    except PgDeltaError as exc:
        print(f'pgdelta: error: {exc}', file=sys.stderr, flush=True)
        return exc.exit_code
    files = [entry.path for entry in selection.inventory.files]
    json.dump([[path, selection.source_path(path)] for path in files] if args.sources else files,
              sys.stdout, indent=1)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
