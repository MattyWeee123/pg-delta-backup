"""Shared PostgreSQL backup phases for the CLI and disposable recovery suite.

Schema v2 records manifest verification separately from restore correctness.
The caller supplies command execution so both frontends retain their own logs.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import subprocess
import time

EXIT_OK, EXIT_INVALID_INPUT, EXIT_IO_FAILURE, EXIT_VERIFICATION_FAILURE = 0, 2, 3, 4
RESULT_KEYS = (
    'schema_version', 'run_id', 'method', 'backup', 'verification',
    'basis_verification', 'increment_verification', 'combination',
    'backup_file_bytes', 'output_file_bytes', 'method_seconds',
    'time_to_verified_seconds', 'integrity_verified', 'restore_tested',
    'network_bytes', 'unavailable_metrics', 'status',
)


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    if os.name != 'nt':
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def run_command(command, log_path, timeout, *, stderr_path=None, input_text=None, env=None):
    """Capture a bounded command; terminate its process group on POSIX timeout.

Timings include command launch and waiting, not just server execution. Native
database integration runs on Linux. Windows stub tests terminate the child.
"""
    start = time.perf_counter()
    exit_code = error = None
    with ExitStack() as stack:
        out = stack.enter_context(Path(log_path).open('w', encoding='utf-8'))
        err = (stack.enter_context(Path(stderr_path).open('w', encoding='utf-8'))
               if stderr_path else subprocess.STDOUT)
        # SQL exports use split logs and must contain only the command's output.
        if stderr_path is None:
            out.write('$ ' + ' '.join(map(str, command)) + '\n\n')
            out.flush()
        try:
            process = subprocess.Popen(
                list(map(str, command)), stdout=out, stderr=err,
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                start_new_session=os.name != 'nt', env=env)
            try:
                process.communicate(input=input_text.encode() if input_text is not None else None,
                                    timeout=timeout)
                exit_code = process.returncode
            except subprocess.TimeoutExpired:
                if os.name != 'nt':
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.communicate()
                error = f'command timed out after {timeout} seconds'
        except (OSError, subprocess.SubprocessError) as exc:
            error = str(exc) or exc.__class__.__name__
        if error:
            (err if stderr_path else out).write('\nERROR: ' + error + '\n')
    return {'exit_code': exit_code, 'duration_seconds': time.perf_counter() - start,
            'error': error}


def directory_bytes(path):
    path = Path(path)
    if not path.exists():
        return None
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not Path(root, name).is_symlink()]
        for name in files:
            entry = Path(root, name)
            if not entry.is_symlink() and entry.is_file():
                total += entry.stat().st_size
    return total


def basebackup_command(tool, output, *, host, port, user, checksum='SHA256',
                       checkpoint='fast', basis=None, progress=False):
    command = [str(tool), '--pgdata', str(output), '--format', 'plain',
               '--wal-method', 'stream', '--manifest-checksums', checksum,
               '--checkpoint', checkpoint, '--host', str(host), '--port', str(port),
               '--username', str(user), '--no-password']
    if basis is not None:
        command += ['--incremental', str(Path(basis) / 'backup_manifest')]
    if progress:
        command.append('--progress')
    return command


def verify_command(tool, output):
    return [str(tool), str(output)]


def combine_command(tool, basis, increment, output, checksum='SHA256'):
    return [str(tool), '--copy', '--manifest-checksums=' + checksum,
            '-o', str(output), str(basis), str(increment)]


def successful(phase):
    return phase is not None and phase['exit_code'] == 0 and phase['error'] is None


def classify(backup, verification):
    if not successful(backup):
        return 'FAIL_BACKUP', EXIT_IO_FAILURE
    if not successful(verification):
        return 'FAIL_VERIFY', EXIT_VERIFICATION_FAILURE
    return 'PASS_MANIFEST_ONLY', EXIT_OK


def build_result(run_id, method, backup, verification, backup_file_bytes, *,
                 basis_verification=None, increment_verification=None, combination=None,
                 output_file_bytes=None, method_seconds=None, verified_seconds=None,
                 failure=None):
    status, exit_code = failure or classify(backup, verification)
    verified = status == 'PASS_MANIFEST_ONLY'
    phases = (backup, increment_verification, combination)
    if method_seconds is None and backup is not None:
        method_seconds = sum(p['duration_seconds'] for p in phases if p is not None)
    if verified_seconds is None and verified:
        verified_seconds = method_seconds + verification['duration_seconds']
    result = {
        'schema_version': 2, 'run_id': run_id, 'method': method,
        'backup': backup, 'verification': verification,
        'basis_verification': basis_verification,
        'increment_verification': increment_verification, 'combination': combination,
        'backup_file_bytes': backup_file_bytes,
        'output_file_bytes': output_file_bytes,
        'method_seconds': method_seconds,
        'time_to_verified_seconds': verified_seconds if verified else None,
        'integrity_verified': verified, 'restore_tested': False,
        'network_bytes': None,
        'unavailable_metrics': {'network_bytes': 'transport observer not implemented'},
        'status': status,
    }
    assert tuple(result) == RESULT_KEYS
    return result, exit_code


def run_backup(method, *, run_id, output, tools, execute, host, port=5432,
               user='postgres', basis=None, increment=None, checksum='SHA256',
               checkpoint='fast', progress=False, basis_verification=None):
    """Capture -> verify input -> combine -> verify, with one shared v2 record.

B0 verification is a prerequisite outside refresh timing. Initial B0 creation
and its storage must be disclosed by the enclosing experiment. Failed phases
short-circuit; their outcome never becomes a successful performance sample.
"""
    if method not in ('pg_basebackup_full', 'pg_basebackup_incremental'):
        raise ValueError('Unknown backup method')
    if method == 'pg_basebackup_incremental' and (basis is None or increment is None):
        raise ValueError('Incremental requires a verified basis and a fresh increment path')
    if method == 'pg_basebackup_full' and (basis is not None or increment is not None):
        raise ValueError('Full backup must not use incremental inputs')
    basis_check = basis_verification
    capture = input_check = combination = verification = None
    capture_path = Path(increment) if basis is not None else Path(output)
    if basis is not None:
        if basis_check is None:
            basis_check = execute('basis_verification', verify_command(tools['pg_verifybackup'], basis))
        if not successful(basis_check):
            return build_result(run_id, method, None, None, None,
                                basis_verification=basis_check,
                                failure=('FAIL_BASIS', EXIT_VERIFICATION_FAILURE))
    start = time.perf_counter()
    capture = execute('backup', basebackup_command(
        tools['pg_basebackup'], capture_path, host=host, port=port, user=user,
        checksum=checksum, checkpoint=checkpoint, basis=basis, progress=progress))
    failure = None
    if not successful(capture):
        failure = ('FAIL_BACKUP', EXIT_IO_FAILURE)
    if failure is None and basis is not None:
        input_check = execute('increment_verification', verify_command(tools['pg_verifybackup'], increment))
        if not successful(input_check):
            failure = ('FAIL_INCREMENT_VERIFY', EXIT_VERIFICATION_FAILURE)
        else:
            combination = execute('combination', combine_command(
                tools['pg_combinebackup'], basis, increment, output, checksum))
            if not successful(combination):
                failure = ('FAIL_COMBINE', EXIT_IO_FAILURE)
    method_seconds = time.perf_counter() - start
    if failure is None:
        verification = execute('verification', verify_command(tools['pg_verifybackup'], output))
    verified_seconds = time.perf_counter() - start
    return build_result(
        run_id, method, capture, verification, directory_bytes(capture_path),
        basis_verification=basis_check, increment_verification=input_check,
        combination=combination, output_file_bytes=directory_bytes(output),
        method_seconds=method_seconds, verified_seconds=verified_seconds, failure=failure)
