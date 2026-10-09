"""Run one full or incremental backup using the shared benchmark adapters.

This command checks the manifest and required WAL, not logical restore
correctness. PASS_MANIFEST_ONLY must never be promoted to a restore PASS.
Use native_baseline for the disposable recovery suite; HammerDB is a separate
workload generator. See benchmarks/CONSOLIDATION.md.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone

# Preserve Sam's documented direct-script command as well as python -m usage.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.backup import (
    EXIT_OK, EXIT_INVALID_INPUT, EXIT_IO_FAILURE, EXIT_VERIFICATION_FAILURE,
    RESULT_KEYS, CHECKSUM_ALGORITHMS, atomic_json, basebackup_command, build_result, classify,
    directory_bytes, run_backup, run_command, verify_command as verification_argv,
)

def pg_basebackup_full(args, backup_dir):
    return basebackup_command(args.pg_basebackup, backup_dir, host=args.host,
                              port=args.port, user=args.user,
                              checksum=args.manifest_checksums, checkpoint=args.checkpoint,
                              progress=args.progress)


def pg_basebackup_incremental(args, backup_dir):
    return basebackup_command(args.pg_basebackup, backup_dir, host=args.host,
                              port=args.port, user=args.user, basis=args.basis,
                              checksum=args.manifest_checksums, checkpoint=args.checkpoint,
                              progress=args.progress)


METHODS = {'pg_basebackup_full': pg_basebackup_full,
           'pg_basebackup_incremental': pg_basebackup_incremental}


def verify_command(args, backup_dir):
    return verification_argv(args.pg_verifybackup, backup_dir)


def new_run_id():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:12]


def resolve_tool(name):
    candidate = Path(name)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate.resolve())
    found = shutil.which(name)
    if found is None:
        raise FileNotFoundError(f'{name} was not found on PATH')
    return found


def assert_no_overlap(backup_dir, source_pgdata):
    backup, source = Path(backup_dir).resolve(), Path(source_pgdata).resolve()
    if backup == source or source in backup.parents or backup in source.parents:
        raise ValueError(f'backup destination {backup} overlaps source data directory {source}')


def tool_version(tool):
    try:
        done = subprocess.run([tool, '--version'], capture_output=True, text=True,
                              timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f'unavailable: {exc}'
    return (done.stdout or done.stderr).strip() or 'unavailable: no output'


def git_commit():
    if os.environ.get('GITHUB_SHA'):
        return os.environ['GITHUB_SHA']
    try:
        done = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True,
                              timeout=30, check=False, cwd=Path(__file__).resolve().parent)
    except (OSError, subprocess.SubprocessError) as exc:
        return f'unavailable: {exc}'
    return done.stdout.strip() if done.returncode == 0 else 'unavailable'


def write_versions(path, args):
    lines = [f'recorded_at: {datetime.now(timezone.utc).isoformat()}',
             f'git_commit: {git_commit()}', f'python: {platform.python_version()}',
             f'platform: {platform.platform()}', f'method: {args.method}',
             'backup_format: plain', f'manifest_checksums: {args.manifest_checksums}',
             f'checkpoint: {args.checkpoint}', f'timeout_seconds: {args.timeout}',
             f'target: {args.user}@{args.host}:{args.port}',
             f'pgpassword_env_set: {"PGPASSWORD" in os.environ}',
             f'image_digest: {os.environ.get("BENCH_IMAGE_DIGEST", "unavailable")}']
    for name in ('pg_basebackup', 'pg_verifybackup', 'pg_combinebackup'):
        if getattr(args, name, None):
            lines.append(f'{name}: {tool_version(getattr(args, name))}')
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5432)
    parser.add_argument('--user', default='postgres')
    parser.add_argument('--out', type=Path, default=Path('./runs'))
    parser.add_argument('--method', choices=sorted(METHODS), default='pg_basebackup_full')
    parser.add_argument('--basis', type=Path, help='Immutable complete B0 for an incremental run')
    parser.add_argument('--format', choices=('plain',), default='plain')
    parser.add_argument('--manifest-checksums', choices=CHECKSUM_ALGORITHMS, default='SHA256')
    parser.add_argument('--checkpoint', choices=('fast', 'spread'), default='fast')
    parser.add_argument('--timeout', type=float, default=3600)
    parser.add_argument('--source-pgdata', help='Optional source path checked for output overlap')
    parser.add_argument('--progress', action='store_true')
    parser.add_argument('--pg-basebackup', default='pg_basebackup')
    parser.add_argument('--pg-verifybackup', default='pg_verifybackup')
    parser.add_argument('--pg-combinebackup', default='pg_combinebackup')
    args = parser.parse_args(argv)
    if not 0 < args.timeout < float('inf'):
        parser.error('timeout must be finite and positive')
    if (args.method == 'pg_basebackup_incremental') != (args.basis is not None):
        parser.error('--basis is required only for the incremental method')
    return args


def connection_env():
    # Keep authentication/TLS settings, but prevent implicit service/routing
    # options overriding the explicit host/port/user recorded for this run.
    routing = {'PGHOST', 'PGHOSTADDR', 'PGPORT', 'PGUSER', 'PGDATABASE',
               'PGSERVICE', 'PGSERVICEFILE', 'PGOPTIONS', 'PGTARGETSESSIONATTRS'}
    return {key: value for key, value in os.environ.items() if key not in routing}


def main(argv=None):
    args = parse_args(argv)
    run_id = new_run_id()
    run_dir = args.out / run_id
    output = run_dir / 'backup'
    try:
        args.pg_basebackup = resolve_tool(args.pg_basebackup)
        args.pg_verifybackup = resolve_tool(args.pg_verifybackup)
        args.pg_combinebackup = resolve_tool(args.pg_combinebackup) if args.basis else None
        if args.source_pgdata:
            assert_no_overlap(run_dir, args.source_pgdata)
        if args.basis:
            assert_no_overlap(run_dir, args.basis)
            if not (args.basis / 'backup_manifest').is_file():
                raise ValueError('basis must contain a backup_manifest')
        if run_dir.exists():
            raise ValueError('Run directory must be new')
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return EXIT_INVALID_INPUT
    run_dir.mkdir(parents=True, exist_ok=False)
    write_versions(run_dir / 'versions.txt', args)
    commands = []

    def execute(stage, command):
        logs = {'backup': 'pg_basebackup.log', 'verification': 'pg_verifybackup.log'}
        path = run_dir / logs.get(stage, stage + '.log')
        phase = run_command(command, path, args.timeout, env=connection_env())
        commands.append({'phase': stage, 'argv': list(map(str, command)),
                         'log': path.name, **phase})
        atomic_json(run_dir / 'commands.json', commands)
        return phase

    result, exit_code = run_backup(
        args.method, run_id=run_id, output=output,
        tools={name: getattr(args, name) for name in
               ('pg_basebackup', 'pg_verifybackup', 'pg_combinebackup')},
        execute=execute, host=args.host, port=args.port, user=args.user,
        basis=args.basis, increment=run_dir / 'increment' if args.basis else None,
        checksum=args.manifest_checksums, checkpoint=args.checkpoint, progress=args.progress)
    atomic_json(run_dir / 'result.json', result)
    print((run_dir / 'result.json').read_text(), end='')
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
