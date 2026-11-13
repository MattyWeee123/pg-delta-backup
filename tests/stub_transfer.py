"""Stand-ins for pg_basebackup, pg_verifybackup, rsync and the pgdelta CLI.

The first argument names the tool. The stand-ins do real file work, so tests
can check that the basis is never modified and that the output matches the
staged backup, but nothing touches PostgreSQL.

The pg_verifybackup stand-in checks no more than PostgreSQL 17's tool does
(observed with 17.11): see checked_files.

Environment:
    STUB_LOG            file receiving one JSON line per invocation (required)
    STUB_FAIL           comma-separated tools that exit non-zero; rsync-version
                        and rsync-transfer fail only that rsync invocation
    STUB_RSYNC_VERSION  file whose text rsync --version prints
    STUB_RSYNC_STATS    file whose text rsync prints after the transfer
    STUB_RSYNC_EXCLUDE  comma-separated top-level names rsync leaves alone in
                        the destination, as --exclude does (also from --delete)
    STUB_PGDELTA_STATS  'missing', 'malformed', 'negative' or 'nan' to break
                        the statistics file pgdelta writes
    STUB_PGDELTA_DEFECT how pgdelta gets the output wrong while exiting 0:
                        basis_manifest, auto_conf, signal, extra_wal,
                        missing_directory (S's copy with that defect),
                        symlink (O is a symlink to S), hardlink (O's files
                        are hard links to S's), or modify_basis or
                        rewrite_basis (O is right, but B's base/1/1259 gains a
                        byte, or has its first byte rewritten with its mtime
                        put back)
"""

import argparse
import json
import os
import shutil
import sys
import uuid
from pathlib import Path


def fails(name):
    return name in os.environ.get('STUB_FAIL', '').split(',')


def pg_basebackup(argv):
    if '--version' in argv:
        print('pg_basebackup (PostgreSQL) 17.11 stub')
        return 0
    if fails('pg_basebackup'):
        print('stub pg_basebackup failed', file=sys.stderr)
        return 1
    target = Path(argv[argv.index('--pgdata') + 1])
    if target.exists() and any(target.iterdir()):
        print(f'directory "{target}" exists but is not empty', file=sys.stderr)
        return 1
    stamp = uuid.uuid4().hex
    (target / 'base' / '1').mkdir(parents=True)
    (target / 'pg_wal').mkdir()
    (target / 'base' / '1' / '1259').write_bytes(b'page' * 2048 + stamp.encode())
    (target / 'pg_wal' / '000000010000000000000002').write_bytes(stamp.encode() * 4)
    (target / 'pg_notify').mkdir()
    (target / 'backup_manifest').write_text(json.dumps(
        {'PostgreSQL-Backup-Manifest-Version': 2, 'Stamp': stamp,
         'Stub-Required-WAL': ['000000010000000000000002']}))
    return 0


# Top-level names real pg_verifybackup skips by default; pg_wal is checked
# separately, by parsing only the WAL the backup needs.
VERIFY_IGNORED = ('backup_manifest', 'pg_wal', 'postgresql.auto.conf',
                  'recovery.signal', 'standby.signal')


def checked_files(root):
    """The regular files real pg_verifybackup compares with the manifest.

    PostgreSQL 17.11 passes an output that keeps a different backup_manifest,
    postgresql.auto.conf, *.signal files or extra pg_wal entries, and one that
    gains or loses an empty directory. It also follows a symlinked output and
    cannot see hard links, so this stand-in reads through both as well.
    """
    root = Path(root)
    files = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        if relative.split('/', 1)[0] in VERIFY_IGNORED or not path.is_file():
            continue
        files[relative] = path.read_bytes()
    return files


def pg_verifybackup(argv):
    """With --manifest-path M, the output's checked files must equal M's tree's."""
    if '--version' in argv:
        print('pg_verifybackup (PostgreSQL) 17.11 stub')
        return 0
    if fails('pg_verifybackup'):
        print('stub pg_verifybackup failed', file=sys.stderr)
        return 1
    if '--manifest-path' in argv:
        manifest = Path(argv[argv.index('--manifest-path') + 1])
        source, output = manifest.parent, Path(argv[-1])
        if checked_files(source) != checked_files(output):
            print(f'{output} does not match {source}', file=sys.stderr)
            return 1
        # The real tool parses the WAL range with pg_waldump; here the
        # required segments must be present and unchanged.
        for name in json.loads(manifest.read_text()).get('Stub-Required-WAL', []):
            wal = output / 'pg_wal' / name
            if not wal.is_file() or wal.read_bytes() != (source / 'pg_wal' / name).read_bytes():
                print(f'WAL segment {name} is missing or damaged', file=sys.stderr)
                return 1
    print('backup successfully verified')
    return 0


def mirror(source, destination, excluded=()):
    """What rsync --archive --delete --inplace leaves behind."""
    def skipped(path, root):
        return path.relative_to(root).parts[0] in excluded

    for path in sorted(destination.rglob('*'), reverse=True):
        if skipped(path, destination) or (source / path.relative_to(destination)).exists():
            continue
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()
    for path in sorted(source.rglob('*')):
        if skipped(path, source):
            continue
        target = destination / path.relative_to(source)
        if path.is_dir():
            target.mkdir(exist_ok=True)
        elif not target.exists() or target.read_bytes() != path.read_bytes():
            target.write_bytes(path.read_bytes())


def rsync(argv):
    if '--version' in argv:
        if fails('rsync-version'):
            return 1
        print(Path(os.environ['STUB_RSYNC_VERSION']).read_text(), end='')
        return 0
    if fails('rsync-transfer'):
        print('rsync error: some files could not be transferred (code 23)', file=sys.stderr)
        return 23
    source, destination = argv[-2], argv[-1]
    if not (source.endswith('/') and destination.endswith('/')):
        print('stub rsync expects trailing slashes', file=sys.stderr)
        return 1
    excluded = [name for name in os.environ.get('STUB_RSYNC_EXCLUDE', '').split(',') if name]
    mirror(Path(source), Path(destination), excluded)
    print(Path(os.environ['STUB_RSYNC_STATS']).read_text(), end='')
    return 0


def build_output(source, basis, output, defect):
    """Copy S to O, then apply the requested defect (see the module docstring)."""
    if defect == 'symlink':
        output.symlink_to(source, target_is_directory=True)
        return
    if defect == 'hardlink':
        shutil.copytree(source, output, copy_function=os.link)
        return
    shutil.copytree(source, output)
    if defect == 'basis_manifest':
        shutil.copyfile(basis / 'backup_manifest', output / 'backup_manifest')
    elif defect == 'auto_conf':
        (output / 'postgresql.auto.conf').write_text("primary_conninfo = 'stale'\n")
    elif defect == 'signal':
        (output / 'standby.signal').write_text('')
    elif defect == 'extra_wal':
        for wal in (basis / 'pg_wal').iterdir():
            shutil.copyfile(wal, output / 'pg_wal' / wal.name)
    elif defect == 'missing_directory':
        (output / 'pg_notify').rmdir()
    elif defect == 'modify_basis':
        with (basis / 'base' / '1' / '1259').open('ab') as stream:
            stream.write(b'+')
    elif defect == 'rewrite_basis':
        victim = basis / 'base' / '1' / '1259'
        stat = victim.stat()
        with victim.open('r+b') as stream:
            stream.write(b'P')
        # Hide the write from a size and mtime comparison.
        os.utime(victim, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    elif defect:
        raise SystemExit(f'unknown STUB_PGDELTA_DEFECT {defect!r}')


def pgdelta(argv):
    parser = argparse.ArgumentParser(prog='pgdelta')
    parser.add_argument('command', choices=('local',))
    for flag in ('--source', '--basis', '--output', '--stats-json'):
        parser.add_argument(flag, required=True)
    parser.add_argument('--pg-controldata')
    args = parser.parse_args(argv)
    if fails('pgdelta'):
        print('stub pgdelta transfer failure', file=sys.stderr)
        return 3
    if Path(args.output).exists():
        print('output already exists', file=sys.stderr)
        return 2
    build_output(Path(args.source), Path(args.basis), Path(args.output),
                 os.environ.get('STUB_PGDELTA_DEFECT', ''))
    mode = os.environ.get('STUB_PGDELTA_STATS', '')
    stats = {'bytes_source_to_dest': 40960, 'bytes_dest_to_source': 2048,
             'pages_sent': 5, 'pages_reused': 1019}
    if mode == 'negative':
        stats['bytes_dest_to_source'] = -1
    text = {'malformed': '{not json', 'nan': '{"bytes_source_to_dest": NaN, '
            '"bytes_dest_to_source": 1}'}.get(mode, json.dumps(stats))
    if mode != 'missing':
        Path(args.stats_json).write_text(text)
    return 0


TOOLS = {'pg_basebackup': pg_basebackup, 'pg_verifybackup': pg_verifybackup,
         'rsync': rsync, 'pgdelta': pgdelta}


def main(argv):
    with open(os.environ['STUB_LOG'], 'a', encoding='utf-8') as log:
        log.write(json.dumps({'tool': argv[0], 'argv': argv[1:]}) + '\n')
    return TOOLS[argv[0]](argv[1:])


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
