"""Benchmark runner v0.1: time and verify one PostgreSQL physical backup.

Runs a backup method, measures its duration and output size, verifies the
output against its manifest, and writes a JSON run record plus logs into a
unique run directory.

This runner does not test restore. A passing pg_verifybackup is not proof that
the cluster can be recovered, so a successful run records the status
PASS_MANIFEST_ONLY and restore_tested stays false. See docs/SPEC.md section 7
and docs/CORRECTNESS.md.

Design: docs/specs/2026-10-09-benchmark-runner-design.md
Methodology: benchmarks/README.md
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Exit codes reuse the CLI contract already defined in docs/SPEC.md section 3.
EXIT_OK = 0
EXIT_INVALID_INPUT = 2
EXIT_IO_FAILURE = 3
EXIT_VERIFICATION_FAILURE = 4

# The v0.1 run record is exactly these keys, in this order. A test asserts it,
# so adding a metric is a deliberate schema change rather than a side effect.
RESULT_KEYS = (
    'run_id',
    'method',
    'backup',
    'verification',
    'backup_file_bytes',
    'time_to_verified_seconds',
    'integrity_verified',
    'restore_tested',
    'network_bytes',
    'status',
)

CHECKSUM_ALGORITHMS = ('NONE', 'CRC32C', 'SHA224', 'SHA256', 'SHA384', 'SHA512')


def run_command(command, log_path, timeout):
    """Execute a command, capture its output to log_path, and time it.

    Returns exit_code None plus an error string when the command could not run
    or exceeded the timeout, so a failure to start is never mistaken for a
    clean exit.
    """
    start = time.perf_counter()
    exit_code = None
    error = None
    with log_path.open('w', encoding='utf-8') as log:
        # Record the command so the log shows which options produced the timing.
        # No password ever reaches argv, so this cannot leak a credential.
        log.write(f"$ {' '.join(str(part) for part in command)}\n\n")
        log.flush()
        try:
            process = subprocess.run(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
            exit_code = process.returncode
        except (OSError, subprocess.SubprocessError) as exc:
            error = str(exc) or exc.__class__.__name__
            log.write(f'\nERROR: {error}\n')
    return {
        'exit_code': exit_code,
        'duration_seconds': round(time.perf_counter() - start, 3),
        'error': error,
    }


def directory_bytes(path):
    """Total size of regular files under path, or None if it does not exist.

    Symlinks are skipped rather than followed: counting a link's target would
    inflate the figure and could leave the approved roots.
    """
    if not path.exists():
        return None
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs
                   if not os.path.islink(os.path.join(root, name))]
        for name in files:
            entry = os.path.join(root, name)
            if not os.path.islink(entry):
                total += os.path.getsize(entry)
    return total


def pg_basebackup_full(args, backup_dir):
    """Full plain-format physical backup with streamed WAL and a manifest."""
    command = [
        args.pg_basebackup,
        '--pgdata', str(backup_dir),
        '--format', args.format,
        '--wal-method', 'stream',
        '--manifest-checksums', args.manifest_checksums,
        '--checkpoint', args.checkpoint,
        '--host', args.host,
        '--port', str(args.port),
        '--username', args.user,
        # Fail instead of blocking on an interactive prompt; credentials come
        # from .pgpass or PGPASSFILE, never from the command line.
        '--no-password',
    ]
    if args.progress:
        command.append('--progress')
    return command


# Future methods register here: native incremental (--incremental against a
# basis manifest), pg_combinebackup, and the custom delta executable. The key
# is both the --method argument and the recorded value of the JSON method field.
METHODS = {
    'pg_basebackup_full': pg_basebackup_full,
}


def verify_command(args, backup_dir):
    """Verify a backup against its manifest.

    pg_verifybackup parses the required WAL range by default, which
    benchmarks/README.md step 5 requires; no flag is passed to disable it.
    """
    return [args.pg_verifybackup, str(backup_dir)]


def classify(backup, verification):
    """Map command outcomes to a run status and a process exit code."""
    if backup['error'] is not None or backup['exit_code'] != 0:
        return 'FAIL_BACKUP', EXIT_IO_FAILURE
    if (verification is None
            or verification['error'] is not None
            or verification['exit_code'] != 0):
        return 'FAIL_VERIFY', EXIT_VERIFICATION_FAILURE
    return 'PASS_MANIFEST_ONLY', EXIT_OK


def build_result(run_id, method, backup, verification, backup_file_bytes):
    """Assemble the v0.1 run record and the process exit code."""
    status, exit_code = classify(backup, verification)
    verified = status == 'PASS_MANIFEST_ONLY'
    result = {
        'run_id': run_id,
        'method': method,
        'backup': backup,
        'verification': verification,
        'backup_file_bytes': backup_file_bytes,
        # Null rather than zero when nothing was verified: a time to a verified
        # backup does not exist for a run that never reached one.
        'time_to_verified_seconds': (
            round(backup['duration_seconds'] + verification['duration_seconds'], 3)
            if verified else None
        ),
        'integrity_verified': verified,
        # v0.1 has no restore gate. See docs/SPEC.md gate G3.
        'restore_tested': False,
        # A single-host run has no measured link. benchmarks/README.md forbids
        # putting zero in an unavailable field.
        'network_bytes': None,
        'status': status,
    }
    return result, exit_code


def new_run_id():
    """A sortable, unique run identifier that is safe as a directory name."""
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    return f'{stamp}-{uuid.uuid4().hex[:6]}'


def resolve_tool(name):
    """Resolve a tool to an executable path, accepting an explicit path."""
    candidate = Path(name)
    if candidate.is_file():
        return str(candidate.resolve())
    found = shutil.which(name)
    if found is None:
        raise FileNotFoundError(f'{name} was not found on PATH')
    return found


def assert_no_overlap(backup_dir, source_pgdata):
    """Refuse a backup destination that overlaps the source data directory."""
    backup = Path(os.path.abspath(backup_dir))
    source = Path(os.path.abspath(source_pgdata))
    if backup == source or source in backup.parents or backup in source.parents:
        raise ValueError(
            f'backup destination {backup} overlaps source data directory {source}')


def tool_version(tool):
    """Best-effort version string; never fatal to a run."""
    try:
        done = subprocess.run([tool, '--version'], capture_output=True,
                              text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f'unavailable: {exc}'
    return (done.stdout or done.stderr).strip() or 'unavailable: no output'


def git_commit():
    """Best-effort repository commit for provenance."""
    try:
        done = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True,
                              text=True, timeout=30, check=False,
                              cwd=Path(__file__).resolve().parent)
    except (OSError, subprocess.SubprocessError) as exc:
        return f'unavailable: {exc}'
    return done.stdout.strip() if done.returncode == 0 else 'unavailable'


def write_versions(path, args):
    """Record provenance the ten-key run record has no field for.

    benchmarks/README.md step 1 requires tool versions and configuration to be
    recorded. They live here rather than in result.json so the v0.1 schema
    stays exactly as specified.
    """
    lines = [
        f'recorded_at: {datetime.now(timezone.utc).isoformat()}',
        f'git_commit: {git_commit()}',
        f'python: {platform.python_version()}',
        f'platform: {platform.platform()}',
        f'method: {args.method}',
        f'backup_format: {args.format}',
        f'manifest_checksums: {args.manifest_checksums}',
        f'checkpoint: {args.checkpoint}',
        f'timeout_seconds: {args.timeout}',
        f'target: {args.user}@{args.host}:{args.port}',
        # Presence only. The value is a credential and is never recorded.
        f'pgpassword_env_set: {"PGPASSWORD" in os.environ}',
        f'pg_basebackup: {tool_version(args.pg_basebackup)}',
        f'pg_verifybackup: {tool_version(args.pg_verifybackup)}',
    ]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Time and verify one PostgreSQL physical backup.',
        epilog='Use a disposable test cluster. Never run against production '
               'or personal data. Supply credentials via .pgpass or PGPASSFILE.')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5432)
    parser.add_argument('--user', default='postgres')
    parser.add_argument('--out', type=Path, default=Path('./runs'),
                        help='parent directory for per-run output')
    parser.add_argument('--method', choices=sorted(METHODS),
                        default='pg_basebackup_full')
    parser.add_argument('--format', choices=('plain', 'tar'), default='plain')
    parser.add_argument('--manifest-checksums', choices=CHECKSUM_ALGORITHMS,
                        default='CRC32C',
                        help='CRC32C is faster; SHA256 costs more CPU (default: CRC32C)')
    parser.add_argument('--checkpoint', choices=('fast', 'spread'), default='fast',
                        help='fast avoids a server-paced delay inside the timing')
    parser.add_argument('--timeout', type=float, default=3600.0,
                        help='per-command timeout in seconds')
    parser.add_argument('--source-pgdata',
                        help='source data directory; checked for overlap with the output')
    parser.add_argument('--progress', action='store_true')
    parser.add_argument('--pg-basebackup', default='pg_basebackup')
    parser.add_argument('--pg-verifybackup', default='pg_verifybackup')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    run_id = new_run_id()
    run_dir = args.out / run_id
    backup_dir = run_dir / 'backup'

    # Validate everything that can be checked before any work is done, so an
    # unsupported input never leaves a half-populated run directory behind.
    try:
        args.pg_basebackup = resolve_tool(args.pg_basebackup)
        args.pg_verifybackup = resolve_tool(args.pg_verifybackup)
        if args.source_pgdata:
            assert_no_overlap(backup_dir, args.source_pgdata)
        if backup_dir.exists() and any(backup_dir.iterdir()):
            raise ValueError(f'backup destination {backup_dir} is not empty')
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return EXIT_INVALID_INPUT

    run_dir.mkdir(parents=True, exist_ok=True)
    write_versions(run_dir / 'versions.txt', args)

    backup = run_command(METHODS[args.method](args, backup_dir),
                         run_dir / 'pg_basebackup.log', args.timeout)

    # Skip verification when the backup failed: verifying an absent or partial
    # output produces a second misleading log and no useful measurement.
    verification = None
    if backup['error'] is None and backup['exit_code'] == 0:
        verification = run_command(verify_command(args, backup_dir),
                                   run_dir / 'pg_verifybackup.log', args.timeout)

    result, exit_code = build_result(run_id, args.method, backup, verification,
                                     directory_bytes(backup_dir))
    rendered = json.dumps(result, indent=2)
    (run_dir / 'result.json').write_text(rendered + '\n', encoding='utf-8')
    print(rendered)
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
