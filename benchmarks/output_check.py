"""Check that a staged method's output O is an independent, exact copy of S.

The staged methods are verified with pg_verifybackup --manifest-path
S/backup_manifest O. That skips backup_manifest, postgresql.auto.conf,
recovery.signal, standby.signal, the pg_wal entries outside the backup's WAL
range and empty directories, follows a symlinked O and cannot see hard links
(all observed with PostgreSQL 17.11). This check runs after pg_verifybackup
passes and closes those gaps:

* independence: O is a real directory, not a symlink, and does not overlap S
  or B. No entry of O is the same file (st_dev, st_ino) as an entry of S or
  B, no regular file of O has a second hard link, and no symlink in O
  resolves into S or B;
* content: O and S hold the same relative paths with the same types, every
  regular file has the same bytes and every symlink the same target. Files
  are compared in bounded chunks, so memory does not grow with file size.

The harness runs this file's source with the method's own interpreter, where
the method's commands run (possibly inside a container):

    python -I -c SOURCE --reference S --independent-of S --independent-of B -- O

So it uses only the standard library and never reads __file__.

Exit status: 0 when O passes, 1 when a difference was found, 2 on a usage or
I/O error. Differences are printed one per line, then a JSON report on one
line that starts with REPORT_PREFIX.
"""
import argparse
import json
import os
import stat
import sys

REPORT_PREFIX = 'OUTPUT_CHECK '
EXIT_PASS, EXIT_DIFFERENT, EXIT_ERROR = 0, 1, 2
CHUNK = 1 << 20
DEFAULT_LIMIT = 20


class _Enough(Exception):
    """The difference limit was reached; stop looking."""


class Findings:
    def __init__(self, limit):
        self.limit = max(1, limit)
        self.lines = []

    def add(self, line):
        self.lines.append(line)
        if len(self.lines) >= self.limit:
            raise _Enough


def kind(mode):
    if stat.S_ISDIR(mode):
        return 'directory'
    if stat.S_ISREG(mode):
        return 'file'
    if stat.S_ISLNK(mode):
        return 'symlink'
    return 'special file'


def walk(root):
    """(relative POSIX path, lstat) for every entry under root, depth first, sorted.

    Symlinks are reported, never followed. Errors propagate: an unreadable
    directory must not shrink the comparison silently.
    """
    pending = ['']
    while pending:
        prefix = pending.pop()
        with os.scandir(os.path.join(root, prefix) if prefix else root) as scan:
            names = sorted(entry.name for entry in scan)
        below = []
        for name in names:
            relative = prefix + name
            # os.lstat, not DirEntry.stat: on Windows the latter leaves st_ino 0.
            info = os.lstat(os.path.join(root, relative))
            yield relative, info
            if stat.S_ISDIR(info.st_mode):
                below.append(relative + '/')
        pending.extend(reversed(below))


def _inside(path, root):
    try:
        return path != root and os.path.commonpath([path, root]) == root
    except ValueError:  # different drives on Windows
        return False


def check_independence(output, others, findings):
    """O must be its own directory, sharing no file with S or B."""
    info = os.lstat(output)
    if stat.S_ISLNK(info.st_mode):
        findings.add(f'{output} is a symbolic link to {os.readlink(output)}, '
                     'not a directory of its own')
        return
    if not stat.S_ISDIR(info.st_mode):
        findings.add(f'{output} is a {kind(info.st_mode)}, not a directory')
        return
    real = os.path.realpath(output)
    roots = [(other, os.path.realpath(other)) for other in others]
    for other, other_real in roots:
        if real == other_real:
            findings.add(f'{output} is the same directory as {other}')
        elif _inside(real, other_real):
            findings.add(f'{output} lies inside {other}')
        elif _inside(other_real, real):
            findings.add(f'{other} lies inside {output}')
    if findings.lines:
        return
    # One entry per file and directory of S and B, not per byte. An inode
    # number of 0 means the filesystem does not report one.
    identities = {}
    for other, _ in roots:
        top = os.stat(other)
        identities.setdefault((top.st_dev, top.st_ino), other)
        for relative, entry in walk(other):
            if not stat.S_ISLNK(entry.st_mode) and entry.st_ino:
                identities.setdefault((entry.st_dev, entry.st_ino),
                                      os.path.join(other, relative))
    same = identities.get((info.st_dev, info.st_ino)) if info.st_ino else None
    if same is not None:
        findings.add(f'{output} is the same directory as {same}')
        return
    for relative, entry in walk(output):
        if stat.S_ISLNK(entry.st_mode):
            target = os.path.realpath(os.path.join(output, relative))
            for other, other_real in roots:
                if target == other_real or _inside(target, other_real):
                    findings.add(f'{relative} is a symbolic link into {other}')
            continue
        same = identities.get((entry.st_dev, entry.st_ino)) if entry.st_ino else None
        if same is not None:
            findings.add(f'{relative} is a hard link to {same}'
                         if stat.S_ISREG(entry.st_mode)
                         else f'{relative} is the same directory as {same}')
        elif stat.S_ISREG(entry.st_mode) and entry.st_nlink > 1:
            findings.add(f'{relative} has {entry.st_nlink} hard links, so another '
                         'path can change it')


def _open(path):
    return os.fdopen(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)), 'rb')


def first_difference(left, right):
    """Offset of the first differing byte of two files, or None if they are equal."""
    with _open(left) as one, _open(right) as other:
        offset = 0
        while True:
            a, b = one.read(CHUNK), other.read(CHUNK)
            if a != b:
                shared = min(len(a), len(b))
                # Narrow to the first differing 4 KiB slice, then to the byte.
                start = next((i for i in range(0, shared, 4096)
                              if a[i:i + 4096] != b[i:i + 4096]), shared)
                return offset + next((i for i in range(start, min(start + 4096, shared))
                                      if a[i] != b[i]), min(start + 4096, shared))
            if not a:
                return None
            offset += len(a)


def compare(output, reference, findings, totals):
    """O must hold exactly S's paths, types and bytes."""
    def inventory(root):
        return {relative: (kind(info.st_mode), info.st_size)
                for relative, info in walk(root)}

    actual, expected = inventory(output), inventory(reference)
    for relative in sorted(actual.keys() | expected.keys()):
        have, want = actual.get(relative), expected.get(relative)
        if have is None:
            findings.add(f'{relative} ({want[0]}) is missing from the output')
        elif want is None:
            findings.add(f'{relative} ({have[0]}) is in the output but not in {reference}')
        elif have[0] != want[0]:
            findings.add(f'{relative} is a {have[0]} in the output but a {want[0]} in {reference}')
        elif have[0] == 'file':
            if have[1] != want[1]:
                findings.add(f'{relative} is {have[1]} bytes in the output, {want[1]} in {reference}')
                continue
            offset = first_difference(os.path.join(output, relative),
                                      os.path.join(reference, relative))
            totals['files_compared'] += 1
            totals['bytes_compared'] += want[1]
            if offset is not None:
                findings.add(f'{relative} differs from {reference} at byte {offset}')
        elif have[0] == 'symlink':
            mine = os.readlink(os.path.join(output, relative))
            theirs = os.readlink(os.path.join(reference, relative))
            if mine != theirs:
                findings.add(f'{relative} links to {mine!r}, not {theirs!r}')
        elif have[0] == 'special file':
            findings.add(f'{relative} is a special file, which cannot be compared')


def check(output, reference=None, independent_of=(), limit=DEFAULT_LIMIT):
    """Return (exit status, report); the report lists the first differences."""
    findings = Findings(limit)
    totals = {'files_compared': 0, 'bytes_compared': 0}
    report = {'independent': None, 'identical': None, 'truncated': False, 'error': None}
    status = EXIT_PASS
    try:
        try:
            check_independence(output, list(independent_of), findings)
            report['independent'] = not findings.lines
            # Comparing through a symlink or hard link would prove nothing.
            if reference is not None and not findings.lines:
                compare(output, reference, findings, totals)
                report['identical'] = not findings.lines
        except _Enough:
            report['truncated'] = True
    except Exception as exc:  # noqa: BLE001 - any failure to check is an error, never a pass
        # Not left to the interpreter: its exit status 1 would read as "differences".
        report['error'] = f'{exc.__class__.__name__}: {exc}'
        status = EXIT_ERROR
    if status == EXIT_PASS and findings.lines:
        status = EXIT_DIFFERENT
    report.update(totals, passed=status == EXIT_PASS, differences=list(findings.lines))
    return status, report


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='output_check', description='Check that O is an independent, exact copy of S.')
    parser.add_argument('output', help='the output directory O')
    parser.add_argument('--reference', help='the staged backup S that O must equal')
    parser.add_argument('--independent-of', action='append', default=[], metavar='PATH',
                        help='a directory (S, B) that O must share no file with; repeatable')
    parser.add_argument('--max-differences', type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args(argv)
    status, report = check(args.output, args.reference, args.independent_of,
                           args.max_differences)
    for line in report['differences']:
        print(line)
    if report['error']:
        print('error: ' + report['error'])
    print(REPORT_PREFIX + json.dumps(report, sort_keys=True))
    return status


if __name__ == '__main__':
    sys.exit(main())
