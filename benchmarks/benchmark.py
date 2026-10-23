"""Run one backup method from the registry using the shared benchmark adapters.

Methods are listed in benchmarks/methods.py: native full or incremental, or a
staged transfer (rsync, pgdelta) that refreshes a copy of an old basis.
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
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

# Preserve Sam's documented direct-script command as well as python -m usage.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.backup import (
    EXIT_OK, EXIT_INVALID_INPUT, EXIT_IO_FAILURE, EXIT_VERIFICATION_FAILURE,
    RESULT_KEYS, CHECKSUM_ALGORITHMS, atomic_json, basebackup_command, basis_changed,
    build_result, classify, directory_bytes, run_backup, run_command, snapshot_changes,
    tree_snapshot, verify_command as verification_argv,
)
from benchmarks import methods

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


# Capture-command builders kept for existing callers. The list of methods
# comes from benchmarks.methods, not from this mapping.
METHODS = {'pg_basebackup_full': pg_basebackup_full,
           'pg_basebackup_incremental': pg_basebackup_incremental}
# Executables a method may need; each maps to a --flag of the same name.
TOOL_FLAGS = ('pg_basebackup', 'pg_verifybackup', 'pg_combinebackup', 'rsync', 'python',
              'pg_controldata')
LOGS = {'backup': 'pg_basebackup.log', 'verification': 'pg_verifybackup.log'}


def verify_command(args, backup_dir):
    return verification_argv(args.pg_verifybackup, backup_dir)


def new_run_id():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:12]


def resolve_tool(name):
    """An absolute path to an executable, with symlinks left as they are.

A venv's bin/python is a symlink to the base interpreter, and the venv is
found from the path it was started by: following the link drops the venv.
"""
    candidate = Path(name)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return os.path.abspath(candidate)
    found = shutil.which(name)
    if found is None:
        raise FileNotFoundError(f'{name} was not found on PATH')
    # which() returns a relative path for a relative PATH entry.
    return os.path.abspath(found)


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
    for name in TOOL_FLAGS:
        tool = getattr(args, name, None)
        if not tool:
            continue
        version = tool_version(tool)
        if name == 'rsync':
            version = methods.rsync_version(version) or version
        # 'python' above is the harness itself; this is the method's interpreter.
        lines.append(f'{"method_python" if name == "python" else name}: {version}')
    if getattr(args, 'pgdelta', None):
        lines.append(f'pgdelta_command: {shlex.join(args.pgdelta)}')
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5432)
    parser.add_argument('--user', default='postgres')
    parser.add_argument('--out', type=Path, default=Path('./runs'))
    parser.add_argument('--method', choices=methods.names(), default='pg_basebackup_full')
    parser.add_argument('--basis', type=Path,
                        help='Immutable old copy B0; required by methods that need one')
    parser.add_argument('--format', choices=('plain',), default='plain')
    parser.add_argument('--manifest-checksums', choices=CHECKSUM_ALGORITHMS, default='SHA256')
    parser.add_argument('--checkpoint', choices=('fast', 'spread'), default='fast')
    parser.add_argument('--timeout', type=float, default=3600)
    parser.add_argument('--source-pgdata', help='Optional source path checked for output overlap')
    parser.add_argument('--progress', action='store_true')
    parser.add_argument('--pg-basebackup', default='pg_basebackup')
    parser.add_argument('--pg-verifybackup', default='pg_verifybackup')
    parser.add_argument('--pg-combinebackup', default='pg_combinebackup')
    parser.add_argument('--rsync', default='rsync',
                        help='rsync executable for rsync_staged (GNU rsync 3.x or openrsync)')
    parser.add_argument('--python', default=sys.executable,
                        help='interpreter for the basis copy and python -m pgdelta')
    parser.add_argument('--pgdelta', type=shlex.split, default=None,
                        help='pgdelta command prefix; default: PYTHON -m pgdelta')
    parser.add_argument('--pg-controldata', default=None,
                        help='passed to pgdelta as --pg-controldata when given')
    args = parser.parse_args(argv)
    if not 0 < args.timeout < float('inf'):
        parser.error('timeout must be finite and positive')
    spec = methods.get(args.method)
    if spec.needs_basis != (args.basis is not None):
        parser.error(f'--basis is required for {args.method}' if spec.needs_basis
                     else f'--basis is not accepted for {args.method}')
    if args.pgdelta is not None and not args.pgdelta:
        parser.error('--pgdelta must name a command')
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
    spec = methods.get(args.method)
    try:
        # Only the selected method's tools are resolved and recorded.
        for name in TOOL_FLAGS:
            value = getattr(args, name)
            wanted = name in spec.tools or (value and name in spec.optional_tools)
            setattr(args, name, resolve_tool(value) if wanted else None)
        if args.pgdelta and 'pgdelta' in spec.optional_tools:
            args.pgdelta = [resolve_tool(args.pgdelta[0]), *args.pgdelta[1:]]
        else:
            args.pgdelta = None
        if args.source_pgdata:
            assert_no_overlap(run_dir, args.source_pgdata)
        if args.basis:
            assert_no_overlap(run_dir, args.basis)
            if spec.trusts_basis and not (args.basis / 'backup_manifest').is_file():
                raise ValueError('basis must contain a backup_manifest')
            if not args.basis.is_dir():
                raise ValueError('basis must be a directory')
        if run_dir.exists():
            raise ValueError('Run directory must be new')
        # Every method that takes B gets it directly; B must not change.
        basis_before = tree_snapshot(args.basis) if args.basis else None
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return EXIT_INVALID_INPUT
    run_dir.mkdir(parents=True, exist_ok=False)
    write_versions(run_dir / 'versions.txt', args)
    commands = []

    def log_path(stage):
        return run_dir / LOGS.get(stage, stage + '.log')

    def execute(stage, command):
        path = log_path(stage)
        phase = run_command(command, path, args.timeout, env=connection_env(),
                            monitor_resources=True)
        commands.append({'phase': stage, 'argv': list(map(str, command)),
                         'log': path.name, **phase})
        atomic_json(run_dir / 'commands.json', commands)
        return phase

    tools = {name: getattr(args, name) for name in TOOL_FLAGS}
    tools['pgdelta'] = args.pgdelta
    result, exit_code = run_backup(
        args.method, run_id=run_id, output=output, tools=tools,
        execute=execute, host=args.host, port=args.port, user=args.user,
        basis=args.basis,
        capture_path=run_dir / spec.capture_dir if spec.capture_dir else None,
        checksum=args.manifest_checksums, checkpoint=args.checkpoint, progress=args.progress,
        read_log=lambda stage: log_path(stage).read_text(encoding='utf-8'))
    if basis_before is not None:
        started = time.perf_counter()
        try:
            changes = snapshot_changes(basis_before, tree_snapshot(args.basis))
        except OSError as exc:
            changes = [f'the basis can no longer be read: {exc}']
        if changes:
            result, exit_code = basis_changed(result, changes, time.perf_counter() - started)
            print(f'error: {result["basis_verification"]["error"]}', file=sys.stderr)
    atomic_json(run_dir / 'result.json', result)
    print((run_dir / 'result.json').read_text(), end='')
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
