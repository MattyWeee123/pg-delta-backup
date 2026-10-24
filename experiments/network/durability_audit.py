"""EXP-11's syscall audit: was a run durable, by the definition, when its clock stopped?

A run's commands on dest run under `strace -f -tt -y -e trace=%file,%desc -o TRACE` (strace_argv):
`run_exp04.py --syscall-audit` traces every command of every timed phase, untimed, and
`durability_audit.py run` traces one command line. The two classes hold every call that creates,
writes, removes or flushes a file or directory, and -y prints the path of every file descriptor,
so each write and flush is put to a path; the open, close and dup calls keep a table for any
descriptor printed without one.

EXP-11's definition ("Durability, defined"), as the audit applies it to the paths in scope (the
method's output):
- every file the method wrote there: each byte covered by a successful fsync(2), fdatasync(2) or
  syncfs(2) issued after that byte's last write. Early write-back (sync_file_range) counts for
  nothing by itself;
- every directory the method created there, or whose entries it changed (mkdir, a file created,
  renamed, linked or removed): by the directory rule, covered by a later successful syncfs of the
  filesystem, a later fsync of that directory, or a later fsync of a file on the filesystem that the
  method wrote at or after the change (ext4's ordered journal commits transactions in order).
An open with O_CREAT counts as a change of its directory even if the file was there already, and as
a change of the file itself, so the file's own later fsync covers it.

Each trace ends when its command exits, which is when the clock stops (the commands of a timed phase).
Several traces are read in the order given, one after the other: a phase's commands, as
pg_probackup catchup and then its sync -f.

    durability_audit.py check --scope O [--filesystem /data] TRACE...   the audit of traces, as JSON
    durability_audit.py run --trace FILE --scope O -- ARGV...           ARGV under strace, then check
    durability_audit.py summary [--json] RUN_DIR...                     every audited run of run_exp04 runs
                                                                        (--json: by arm, for analysis/exp11.py)

Limits: writes through a shared memory map (msync) and O_SYNC/O_DSYNC descriptors are not seen as
flushes, and nothing here proves the disks honour cache flushes.

Standard library only.
"""
from __future__ import annotations

import argparse
import json
import posixpath
import re
import subprocess
from pathlib import Path

STRACE_OPTIONS = ('-f', '-tt', '-y', '-e', 'trace=%file,%desc')
DEFAULT_FILESYSTEM = '/data'
LIMIT = 20  # examples listed per kind of problem

# One line of `strace -f -tt -o FILE` (the pid first), or of strace -f on stderr ("[pid N] ").
LINE = re.compile(r'^(?:\[pid\s+(\d+)\]\s+|(\d+)\s+)?(\d{1,2}:\d{2}:\d{2}(?:\.\d+)?)\s+(.*)$')
UNFINISHED = re.compile(r'^(\w+)\((.*?)\s*<unfinished \.\.\.>$')
RESUMED = re.compile(r'^<\.\.\. (\w+) resumed>\s*(.*)$')
COMPLETE = re.compile(r'^(\w+)\((.*)\)\s+=\s+(.*)$')
RETURN = re.compile(r'^(-?\d+)(?:<(.*)>)?')
ANNOTATED = re.compile(r'^(-?\d+|AT_FDCWD)<(.*)>$', re.DOTALL)
DELETED = ' (deleted)'

# Calls that write a file's bytes or change its size, and which argument is the descriptor written.
FD_WRITES = {'write': 0, 'writev': 0, 'pwrite64': 0, 'pwritev': 0, 'pwritev2': 0, 'fallocate': 0,
             'ftruncate': 0, 'copy_file_range': 2, 'sendfile': 0, 'sendfile64': 0, 'splice': 2}
# Of those, the ones that return a byte count (0: nothing was written).
BYTE_COUNTS = {'write', 'writev', 'pwrite64', 'pwritev', 'pwritev2', 'copy_file_range', 'sendfile',
               'sendfile64', 'splice'}
FILE_FLUSHES = ('fsync', 'fdatasync')
# Opening calls: (the directory descriptor's argument, the path's, the flags').
OPENS = {'open': (None, 0, 1), 'openat': (0, 1, 2), 'openat2': (0, 1, 2), 'creat': (None, 0, None)}
# Calls that add or remove one entry of a directory: (the directory descriptor's argument, the path's).
ENTRY_CHANGES = {'mkdir': (None, 0), 'mkdirat': (0, 1), 'mknod': (None, 0), 'mknodat': (0, 1),
                 'symlink': (None, 1), 'symlinkat': (1, 2)}
REMOVALS = {'rmdir': (None, 0), 'unlink': (None, 0), 'unlinkat': (0, 1)}
# Calls naming two paths: (old's directory descriptor, old, new's directory descriptor, new).
TWO_PATHS = {'rename': (None, 0, None, 1), 'renameat': (0, 1, 2, 3), 'renameat2': (0, 1, 2, 3),
             'link': (None, 0, None, 1), 'linkat': (0, 1, 2, 3)}


def strace_argv(trace) -> list[str]:
    """The prefix that runs a command under the audit's strace, its trace written to `trace`."""
    return ['strace', *STRACE_OPTIONS, '-o', str(trace), '--']


def under(path: str | None, root: str) -> bool:
    return path is not None and (path == root or path.startswith(root.rstrip('/') + '/'))


# --- parsing ------------------------------------------------------------------------

def split_args(text: str) -> list[str]:
    """strace's printed arguments, split at their top-level commas (strings, braces, brackets,
    parentheses and -y's <path> annotations kept whole)."""
    args, current, depth, quoted, escaped = [], [], 0, False, False
    for char in text:
        if quoted:
            current.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char in '([{<':
            depth += 1
        elif char in ')]}>' and depth:
            depth -= 1
        elif char == ',' and depth == 0:
            args.append(''.join(current).strip())
            current = []
            continue
        current.append(char)
    tail = ''.join(current).strip()
    if tail or args:
        args.append(tail)
    return args


def unquote(text: str) -> str | None:
    """A path argument as strace prints it ("...", with C escapes); None if it isn't a string."""
    text = text.strip()
    if text.endswith('...'):
        text = text[:-3]
    if len(text) < 2 or text[0] != '"' or text[-1] != '"':
        return None
    raw, out, index = text[1:-1], bytearray(), 0
    while index < len(raw):
        char = raw[index]
        if char != '\\':
            out += char.encode()
            index += 1
            continue
        following = raw[index + 1:index + 2]
        if following == 'x':
            out.append(int(raw[index + 2:index + 4], 16))
            index += 4
        elif following.isdigit():
            digits = re.match(r'[0-7]{1,3}', raw[index + 1:]).group(0)
            out.append(int(digits, 8))
            index += 1 + len(digits)
        else:
            out += {'n': b'\n', 't': b'\t', 'r': b'\r', 'v': b'\v', 'f': b'\f'}.get(following, following.encode())
            index += 2
    return out.decode(errors='surrogateescape')


class Call:
    """One completed system call: who, when, its arguments and result, and where it lies in the
    traces (start: the line it was issued on; end: the line its result came back on)."""

    __slots__ = ('pid', 'time', 'name', 'args', 'result', 'start', 'end')

    def __init__(self, pid, time, name, args, result, start, end):
        self.pid, self.time, self.name, self.args = pid, time, name, args
        self.result, self.start, self.end = result, start, end

    @property
    def value(self) -> int | None:
        """The return value (a descriptor, a byte count, 0 or -1); None for '?'."""
        found = RETURN.match(self.result)
        return int(found.group(1)) if found else None

    @property
    def returned_path(self) -> str | None:
        """-y's path of a returned descriptor ("= 3</data/x>")."""
        found = RETURN.match(self.result)
        return found.group(2) if found else None

    @property
    def ok(self) -> bool:
        value = self.value
        return value is not None and value >= 0

    def __repr__(self):
        return f'Call({self.pid} {self.time} {self.name}({", ".join(self.args)}) = {self.result})'


def _join(head: str, tail: str) -> str:
    """An unfinished call's arguments and the rest its "resumed" line printed."""
    head, tail = head.rstrip(), tail.strip()
    if not tail or not head:
        return head or tail
    if head.endswith(',') or tail.startswith(','):
        return f'{head} {tail}' if head.endswith(',') else head + tail
    return f'{head}, {tail}'


def parse(texts) -> list[Call]:
    """Every completed call in the traces, in the order they completed, each trace after the one before.

    An unfinished call (another task's line came between) is joined with its "resumed" line. Signals,
    exits and calls never resumed are left out.
    """
    if isinstance(texts, str):
        texts = [texts]
    calls, index = [], 0
    for text in texts:
        pending: dict[int, tuple] = {}
        for line in text.splitlines():
            found = LINE.match(line.strip())
            if found is None:
                continue
            index += 1
            pid = int(found.group(1) or found.group(2) or 0)
            stamp, body = found.group(3), found.group(4)
            if body.startswith(('+++', '---')):
                continue
            resumed = RESUMED.match(body)
            if resumed:
                if pid not in pending or pending[pid][0] != resumed.group(1):
                    continue
                name, head, issued, started = pending.pop(pid)
                rest = resumed.group(2)
                cut = rest.rfind(') = ')
                if cut < 0:
                    continue
                calls.append(Call(pid, issued, name, split_args(_join(head, rest[:cut])),
                                  rest[cut + 4:].strip(), started, index))
                continue
            unfinished = UNFINISHED.match(body)
            if unfinished:
                pending[pid] = (unfinished.group(1), unfinished.group(2), stamp, index)
                continue
            complete = COMPLETE.match(body)
            if complete:
                calls.append(Call(pid, stamp, complete.group(1), split_args(complete.group(2)),
                                  complete.group(3).strip(), index, index))
    return calls


# --- the audit ----------------------------------------------------------------------

class Auditor:
    """Replays the calls: which inode each path and descriptor names, and what was written, changed
    and flushed, when. Inodes are numbered as first seen; a rename moves names, a removal drops them."""

    def __init__(self, filesystem: str = DEFAULT_FILESYSTEM, cwd: str | None = None):
        self.filesystem = posixpath.normpath(filesystem)
        self.cwd = cwd
        self.cwds: dict[int, str] = {}  # pid -> working directory, after chdir/fchdir
        self.names: dict[str, int] = {}  # current path -> inode
        self.paths: dict[int, str] = {}  # inode -> current path (none once removed)
        self.next_inode = 1
        self.descriptors: dict[int, dict[int, int]] = {}  # pid -> fd -> inode
        self.latest: dict[int, int] = {}  # fd -> inode from any pid (a process's threads share descriptors)
        self.writes: dict[int, list] = {}  # inode -> [(start, end, bytes, path at the time)]
        self.flushes: dict[int, list] = {}  # inode -> [(start, 'fsync' or 'fdatasync')]
        self.syncfs: list[int] = []  # starts of successful syncfs calls on the filesystem
        self.changes: list[dict] = []
        self.counts = {'fsync': 0, 'fdatasync': 0, 'syncfs': 0, 'sync_file_range': 0}
        self.unmapped: list[str] = []
        self.unresolved: list[str] = []
        self.calls = 0

    # names and descriptors

    def inode(self, path: str) -> int:
        if path not in self.names:
            self.names[path] = self.next_inode
            self.paths[self.next_inode] = path
            self.next_inode += 1
        return self.names[path]

    def remove(self, path: str) -> None:
        """A name removed (unlink, rmdir, or replaced by a rename), with every name below it."""
        for name in [name for name in self.names if under(name, path)]:
            number = self.names.pop(name)
            if self.paths.get(number) == name:
                del self.paths[number]

    def move(self, old: str, new: str) -> None:
        """rename(old, new): new's inode goes, and old's names (with those below it) move to new."""
        moving = {name: self.names[name] for name in self.names if under(name, old)}
        self.remove(new)
        for name, number in moving.items():
            self.names.pop(name, None)
            moved = new + name[len(old):]
            self.names[moved] = number
            self.paths[number] = moved

    def descriptor(self, pid: int, text: str) -> tuple[int | None, str | None, bool]:
        """(inode, path, known) of a descriptor argument, from -y's path or else the tables.

        known is False when nothing names it. Sockets, pipes and other non-files come back as
        (None, their -y text, True).
        """
        text = text.strip()
        found = ANNOTATED.match(text)
        fd = None
        try:
            fd = int((found.group(1) if found else text).split('<', 1)[0])
        except ValueError:
            pass
        tabled = None if fd is None else self.descriptors.get(pid, {}).get(fd, self.latest.get(fd))
        if found:
            path = found.group(2)
            if path.endswith(DELETED):  # a removed file: only the tables know its inode
                return tabled, None if tabled is None else self.paths.get(tabled), True
            if not path.startswith('/'):
                return None, path, True
            path = posixpath.normpath(path)
            return self.inode(path), path, True
        if tabled is None:
            return None, None, False
        return tabled, self.paths.get(tabled), True

    def base(self, call: Call, dirfd: int | None) -> str | None:
        """The directory a relative path of the call starts from: its directory descriptor's path,
        AT_FDCWD's path as -y prints it, or the process's working directory."""
        text = call.args[dirfd].strip() if dirfd is not None and dirfd < len(call.args) else 'AT_FDCWD'
        found = ANNOTATED.match(text)
        if text.startswith('AT_FDCWD'):
            if found and found.group(2).startswith('/'):
                return found.group(2)
            return self.cwds.get(call.pid, self.cwd)
        return self.descriptor(call.pid, text)[1]

    def resolve(self, call: Call, dirfd: int | None, index: int) -> str | None:
        """The absolute path of call.args[index], relative ones from base()."""
        if index >= len(call.args):
            return None
        name = unquote(call.args[index])
        if name is None:
            return None
        if not name.startswith('/'):
            base = self.base(call, dirfd)
            if base is None or not base.startswith('/'):
                self.unresolved.append(f'{call.time} {call.name}: {name}')
                return None
            name = posixpath.join(base, name)
        return posixpath.normpath(name)

    def bind(self, pid: int, fd: int, number: int) -> None:
        self.descriptors.setdefault(pid, {})[fd] = number
        self.latest[fd] = number

    # events

    def write(self, number: int, call: Call, size: int, start: int | None = None) -> None:
        self.writes.setdefault(number, []).append(
            (call.start if start is None else start, call.end, size, self.paths.get(number)))

    def change(self, path: str, call: Call) -> None:
        """An entry of path's directory added or removed by `call`."""
        directory = posixpath.dirname(path)
        self.changes.append({'directory': directory, 'inode': self.inode(directory), 'entry': path,
                             'call': call.name, 'time': call.time, 'end': call.end})

    def apply(self, call: Call) -> None:
        self.calls += 1
        name = call.name
        if not call.ok:
            return
        if name == 'close':
            number, _, _ = self.descriptor(call.pid, call.args[0])
            fd = int(call.args[0].split('<', 1)[0])
            self.descriptors.get(call.pid, {}).pop(fd, None)
            # A descriptor the trace never saw opened (inherited from the traced command's parent) has
            # no number: nothing of ours to forget (found in EXP-11's smoke run on real strace output).
            if number is not None and self.latest.get(fd) == number:
                self.latest.pop(fd)
        elif name in OPENS:
            self.open(call)
        elif name in ('dup', 'dup2', 'dup3') or (name == 'fcntl' and len(call.args) > 1
                                                  and call.args[1].startswith('F_DUPFD')):
            number, _, _ = self.descriptor(call.pid, call.args[0])
            if number is not None:
                self.bind(call.pid, call.value, number)
        elif name in FD_WRITES:
            index = FD_WRITES[name]
            if index >= len(call.args) or (name in BYTE_COUNTS and not call.value):
                return
            number, _, known = self.descriptor(call.pid, call.args[index])
            if not known:
                self.unmapped.append(f'{call.time} {name}({call.args[index]}, ...) = {call.result}')
            elif number is not None:
                self.write(number, call, call.value if name in BYTE_COUNTS else 0)
        elif name == 'truncate':
            path = self.resolve(call, None, 0)
            if path is not None:
                self.write(self.inode(path), call, 0)
        elif name in FILE_FLUSHES:
            self.counts[name] += 1
            number, _, _ = self.descriptor(call.pid, call.args[0])
            if number is not None:
                self.flushes.setdefault(number, []).append((call.start, name))
        elif name == 'syncfs':
            self.counts['syncfs'] += 1
            _, path, _ = self.descriptor(call.pid, call.args[0])
            if under(path, self.filesystem):
                self.syncfs.append(call.start)
        elif name == 'sync_file_range':
            self.counts['sync_file_range'] += 1
        elif name in ENTRY_CHANGES:
            path = self.resolve(call, *ENTRY_CHANGES[name])
            if path is not None:
                self.change(path, call)
                self.inode(path)
        elif name in REMOVALS:
            path = self.resolve(call, *REMOVALS[name])
            if path is not None:
                self.change(path, call)
                self.remove(path)
        elif name in TWO_PATHS:
            old_dirfd, old_index, new_dirfd, new_index = TWO_PATHS[name]
            old, new = self.resolve(call, old_dirfd, old_index), self.resolve(call, new_dirfd, new_index)
            if new is None:
                return
            if name.startswith('rename'):
                if old is None:
                    return
                self.change(old, call)
                self.change(new, call)
                self.move(old, new)
            else:
                self.change(new, call)
                if old is not None:
                    self.remove(new)
                    self.names[new] = self.inode(old)
        elif name == 'chdir':
            path = self.resolve(call, None, 0)
            if path is not None:
                self.cwds[call.pid] = path
        elif name == 'fchdir':
            _, path, _ = self.descriptor(call.pid, call.args[0])
            if path is not None and path.startswith('/'):
                self.cwds[call.pid] = path

    def open(self, call: Call) -> None:
        dirfd, path_index, flags_index = OPENS[call.name]
        path = call.returned_path
        if path is None or not path.startswith('/') or path.endswith(DELETED):
            path = self.resolve(call, dirfd, path_index)
        if path is None:
            return
        path = posixpath.normpath(path)
        if flags_index is None:  # creat
            flags = {'O_CREAT', 'O_TRUNC', 'O_WRONLY'}
        else:
            text = call.args[flags_index] if flags_index < len(call.args) else ''
            found = re.search(r'flags=([A-Z0-9_|]+)', text)
            flags = set((found.group(1) if found else text).split('|'))
        number = self.inode(path)
        self.bind(call.pid, call.value, number)
        if 'O_CREAT' in flags:
            self.change(path, call)
            # The file's own metadata changes with it, in the same journal transaction.
            self.write(number, call, 0, start=call.end)
        elif 'O_TRUNC' in flags:
            self.write(number, call, 0)

    # the verdict

    def on_filesystem(self, path: str | None) -> bool:
        return under(path, self.filesystem)

    def file_covered(self, number: int, after: int) -> str | None:
        """What covers a file's writes up to line `after`: its own 'fsync' or 'fdatasync', or 'syncfs'."""
        for start, how in self.flushes.get(number, ()):
            if start > after:
                return how
        if any(start > after for start in self.syncfs):
            return 'syncfs'
        return None

    def change_covered(self, change: dict) -> str | None:
        """What covers a directory change by the directory rule: 'syncfs', 'directory_fsync' or 'file_fsync'."""
        end = change['end']
        if any(start > end for start in self.syncfs):
            return 'syncfs'
        if any(how == 'fsync' and start > end for start, how in self.flushes.get(change['inode'], ())):
            return 'directory_fsync'
        for number, writes in self.writes.items():
            later = [write[1] for write in writes if write[0] >= end and self.on_filesystem(write[3])]
            if later and any(how == 'fsync' and start > min(later) for start, how in self.flushes.get(number, ())):
                return 'file_fsync'
        return None

    def verdict(self, scope, ignore=()) -> dict:
        scope = [posixpath.normpath(str(root)) for root in scope]
        ignore = [posixpath.normpath(str(root)) for root in ignore]

        def in_scope(path):
            return any(under(path, root) for root in scope)

        files, outside = [], []
        for number, writes in self.writes.items():
            path = self.paths.get(number)
            if path is None:  # removed since: part of nothing
                continue
            entry = {'path': path, 'bytes': sum(write[2] for write in writes), 'writes': len(writes),
                     'covered_by': self.file_covered(number, max(write[1] for write in writes))}
            if in_scope(path):
                files.append(entry)
            elif self.on_filesystem(path) and not any(under(path, root) for root in ignore):
                outside.append(entry)
        changes = []
        for change in self.changes:
            # In scope by the entry's path then, or by where its directory is now (renamed into place).
            if in_scope(change['entry']) or in_scope(self.paths.get(change['inode'])):
                changes.append({key: change[key] for key in ('directory', 'entry', 'call', 'time')}
                               | {'covered_by': self.change_covered(change)})
        uncovered_files = [entry for entry in files if entry['covered_by'] is None]
        uncovered_changes = [change for change in changes if change['covered_by'] is None]
        return {
            'ok': not uncovered_files and not uncovered_changes and not self.unmapped,
            'scope': scope, 'filesystem': self.filesystem, 'calls': self.calls,
            'files_written': len(files), 'bytes_written': sum(entry['bytes'] for entry in files),
            'uncovered_file_count': len(uncovered_files), 'uncovered_files': uncovered_files[:LIMIT],
            'directory_changes': len(changes), 'uncovered_directory_change_count': len(uncovered_changes),
            'uncovered_directory_changes': uncovered_changes[:LIMIT],
            'files_covered_by': _tally(entry['covered_by'] for entry in files),
            'directory_changes_covered_by': _tally(change['covered_by'] for change in changes),
            'flushes': dict(self.counts),
            'unmapped_write_count': len(self.unmapped), 'unmapped_writes': self.unmapped[:LIMIT],
            'unresolved_paths': self.unresolved[:LIMIT],
            'outside_scope': {'files_written': len(outside),
                              'uncovered': [entry for entry in outside if entry['covered_by'] is None][:LIMIT]},
        }


def _tally(values) -> dict:
    tally: dict = {}
    for value in values:
        tally[value or 'uncovered'] = tally.get(value or 'uncovered', 0) + 1
    return tally


def audit(texts, scope, *, filesystem: str = DEFAULT_FILESYSTEM, cwd: str | None = None, ignore=()) -> dict:
    """The audit of traces (their text, read in order) for the paths under `scope`, the method's output.

    ok: every file written there covered, every directory change there covered by the directory rule,
    and no write to a descriptor the trace doesn't name. Files written on the filesystem outside the
    scope (and outside `ignore`, the harness's own logs) are listed apart and decide nothing.
    """
    auditor = Auditor(filesystem, cwd)
    for call in parse(texts):
        auditor.apply(call)
    return auditor.verdict(scope, ignore)


def audit_files(paths, scope, **kwargs) -> dict:
    """audit() of trace files; a trace that can't be read is listed, and the audit is then not ok."""
    texts, missing = [], []
    for path in paths:
        try:
            texts.append(Path(path).read_text(errors='replace'))
        except OSError as exc:
            missing.append(f'{path}: {exc}')
    result = audit(texts, scope, **kwargs)
    result['traces'] = [str(path) for path in paths]
    if missing or not paths:
        result.update(ok=False, missing_traces=missing or ['no trace'])
    return result


# --- command line --------------------------------------------------------------------

def cmd_check(args) -> dict:
    return audit_files(args.traces, args.scope, filesystem=args.filesystem, cwd=args.cwd, ignore=args.ignore)


def cmd_run(args) -> dict:
    argv = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
    if not argv:
        raise SystemExit('run needs a command after --')
    Path(args.trace).unlink(missing_ok=True)
    completed = subprocess.run([*strace_argv(args.trace), *argv], stdin=subprocess.DEVNULL)
    result = audit_files([args.trace], args.scope, filesystem=args.filesystem, cwd=str(Path.cwd()),
                         ignore=args.ignore)
    return {'argv': argv, 'exit_code': completed.returncode, **result}


def summary_rows(run_dir) -> list[dict]:
    """One row per view of each audited record of a run_exp04 run (runs/*/*.json with an 'audit')."""
    rows = []
    for path in sorted(Path(run_dir).glob('runs/*/*.json')):
        record = json.loads(path.read_text())
        audited = record.get('audit') or {}
        for view, label in (('result', 'phase'), ('tool_only', 'tool only')):
            result = audited.get(view)
            if not result:
                continue
            rows.append({'trial_id': record.get('trial_id'), 'method': record.get('method'), 'view': label,
                         'ok': result.get('ok'), 'files': result.get('files_written'),
                         'uncovered_files': result.get('uncovered_file_count'),
                         'directory_changes': result.get('directory_changes'),
                         'uncovered_changes': result.get('uncovered_directory_change_count'),
                         'unmapped': result.get('unmapped_write_count'),
                         'examples': [entry['path'] for entry in result.get('uncovered_files', [])[:3]]
                         + [change['entry'] for change in result.get('uncovered_directory_changes', [])[:3]]})
    return rows


SUMMARY_COLUMNS = ('trial_id', 'method', 'view', 'ok', 'files', 'uncovered_files', 'directory_changes',
                   'uncovered_changes', 'unmapped')


def _uncovered(result: dict, scope) -> list[str]:
    """The paths a view left uncovered (files, then directory entries), relative to the output."""
    def relative(path):
        for root in scope:
            if under(path, root):
                return posixpath.relpath(path, root)
        return path
    paths = [entry['path'] for entry in result.get('uncovered_files', [])]
    paths += [change['entry'] for change in result.get('uncovered_directory_changes', [])]
    return list(dict.fromkeys(relative(path) for path in paths))


# The audit's counts, summed over an arm's runs: (the result's key, the arm's key). The path lists
# above are examples only (each kind is cut at LIMIT, then files and directory entries are merged and
# deduplicated), so a count of what was left uncovered comes from these, never from a list's length.
COUNTS = (('files_written', 'files'), ('uncovered_file_count', 'uncovered_files'),
          ('directory_changes', 'directory_changes'),
          ('uncovered_directory_change_count', 'uncovered_directory_changes'))


def _add_counts(arm: dict, result: dict, prefix: str = '') -> None:
    """Add a view's counts to the arm's (None, for good, once a run lacks one)."""
    for key, name in COUNTS:
        value, name = result.get(key), prefix + name
        arm[name] = None if value is None or arm.get(name, 0) is None else arm.get(name, 0) + value


def arm_results(run_dirs) -> dict:
    """{'arms': {method: {...}}}: the audited records of run_exp04 runs by arm, the shape
    analysis/exp11.py's --audit reads.

    passed: every audited run of the arm ok (None if a run has no result); uncovered: what the runs'
    timed phases left uncovered; tool_uncovered (probackup*): what the tool's own flush left, from the
    catchup's trace alone. Paths are relative to the output; runs counts the audited runs. The path
    lists are examples (LIMIT of each kind per run); the counts are files, uncovered_files,
    directory_changes and uncovered_directory_changes, summed over the runs, and the same prefixed
    tool_ for the catchup's trace alone.
    """
    arms: dict = {}
    for run_dir in run_dirs:
        for path in sorted(Path(run_dir).glob('runs/*/*.json')):
            record = json.loads(path.read_text())
            audited = record.get('audit') or {}
            if not audited.get('result'):
                continue
            arm = arms.setdefault(record.get('method'), {'passed': True, 'runs': 0, 'uncovered': []})
            scope = [posixpath.normpath(str(root)) for root in audited.get('scope') or ()]
            ok = audited['result'].get('ok')
            arm['passed'] = None if arm['passed'] is None or ok is None else arm['passed'] and ok
            arm['runs'] += 1
            arm['uncovered'] = list(dict.fromkeys(arm['uncovered'] + _uncovered(audited['result'], scope)))
            _add_counts(arm, audited['result'])
            if audited.get('tool_only'):
                arm['tool_uncovered'] = list(dict.fromkeys(
                    arm.get('tool_uncovered', []) + _uncovered(audited['tool_only'], scope)))
                _add_counts(arm, audited['tool_only'], 'tool_')
    return {'arms': arms}


def cmd_summary(args) -> dict | None:
    if args.json:
        result = arm_results(args.run_dir)
        if not result['arms']:
            raise SystemExit(f'no audited runs under {", ".join(args.run_dir)}')
        # Printed whole, whether or not every arm passed: the analysis reads it.
        print(json.dumps(result, indent=1))
        return None
    rows = [row for run_dir in args.run_dir for row in summary_rows(run_dir)]
    if not rows:
        raise SystemExit(f'no audited runs under {", ".join(args.run_dir)}')
    print('\t'.join((*SUMMARY_COLUMNS, 'examples')))
    for row in rows:
        print('\t'.join([str(row[column]) for column in SUMMARY_COLUMNS] + [', '.join(row['examples'])]))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    for name, func in (('check', cmd_check), ('run', cmd_run)):
        sub = commands.add_parser(name)
        sub.add_argument('--scope', action='append', required=True, metavar='PATH',
                         help="the method's output (repeatable): what the definition is checked for")
        sub.add_argument('--filesystem', default=DEFAULT_FILESYSTEM,
                         help="the output's mount point, which a syncfs of it covers")
        sub.add_argument('--ignore', action='append', default=[], metavar='PATH',
                         help='leave files below PATH out of the outside-scope list (the harness logs)')
        if name == 'check':
            sub.add_argument('--cwd', help="the traced commands' working directory, for relative paths")
            sub.add_argument('traces', nargs='+')
        else:
            sub.add_argument('--trace', required=True)
            sub.add_argument('argv', nargs=argparse.REMAINDER)
        sub.set_defaults(func=func)
    sub = commands.add_parser('summary')
    sub.add_argument('run_dir', nargs='+')
    sub.add_argument('--json', action='store_true',
                     help="each arm's result as JSON (arm_results), for analysis/exp11.py --audit")
    sub.set_defaults(func=cmd_summary)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    result = args.func(args)
    if result is None:
        return 0
    print(json.dumps(result, indent=1))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
