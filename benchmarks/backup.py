"""Shared PostgreSQL backup phases for the CLI and disposable recovery suite.

Schema v3 records manifest verification separately from restore correctness,
and carries a transfer phase and its byte counts for delta/transfer methods.
The caller supplies command execution so every frontend retains its own logs.
Methods themselves live in benchmarks/methods.py.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import time
from typing import NamedTuple

from benchmarks import output_check
from benchmarks.resources import ResourceSampler

EXIT_OK, EXIT_INVALID_INPUT, EXIT_IO_FAILURE, EXIT_VERIFICATION_FAILURE = 0, 2, 3, 4
CHECKSUM_ALGORITHMS = ('CRC32C', 'SHA224', 'SHA256', 'SHA384', 'SHA512')
SCHEMA_VERSION = 3
RESULT_KEYS = (
    'schema_version', 'run_id', 'method', 'backup', 'verification',
    'basis_verification', 'increment_verification', 'combination',
    'transfer', 'transfer_stats',
    'backup_file_bytes', 'output_file_bytes', 'method_seconds',
    'time_to_verified_seconds', 'integrity_verified', 'restore_tested',
    'network_bytes', 'unavailable_metrics', 'status',
)
NO_TRANSPORT_OBSERVER = 'transport observer not implemented'
SELF_REPORTED_BYTES = ("network_bytes is the method's own count of its transfer "
                       'stream in both directions; no independent interface observer')


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


def run_command(command, log_path, timeout, *, stderr_path=None, input_text=None,
                env=None, monitor_resources=False):
    """Capture a bounded command; terminate its process group on POSIX timeout.

Timings include command launch and waiting, not just server execution. Native
database integration runs on Linux. Windows stub tests terminate the child.
"""
    start = time.perf_counter()
    exit_code = error = resources = None
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
            sampler = None
            if monitor_resources:
                sampler = ResourceSampler({'runner': process.pid},
                    Path(log_path).with_suffix('.resources.jsonl'))
                sampler.start()
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
            finally:
                if sampler:
                    resources = sampler.stop()
        except (OSError, subprocess.SubprocessError) as exc:
            error = str(exc) or exc.__class__.__name__
        if error:
            (err if stderr_path else out).write('\nERROR: ' + error + '\n')
    result = {'exit_code': exit_code, 'duration_seconds': time.perf_counter() - start,
              'error': error}
    if monitor_resources:
        result['resources'] = resources or {
            'unavailable_reason': error or 'process did not start'}
    return result


def directory_bytes(path):
    """Apparent size of the regular files under path, without following symlinks.

None when path is missing or is itself a symlink: a symlinked root would
report another directory's size as this one's.
"""
    path = Path(path)
    if path.is_symlink() or not path.exists():
        return None
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not Path(root, name).is_symlink()]
        for name in files:
            entry = Path(root, name)
            if not entry.is_symlink() and entry.is_file():
                total += entry.stat().st_size
    return total


class Entry(NamedTuple):
    """One tree_snapshot entry. Contents are not hashed."""

    kind: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    inode: int
    device: int
    target: str | None


def tree_snapshot(root):
    """Cheap per-entry identity of everything under root, without following symlinks.

Records the type, mode, size, mtime, ctime, inode, device and symlink target
of root and every entry beneath it. ctime moves on any write, chmod or new hard
link and cannot be set back, so a same-size rewrite with its mtime restored is
still seen. Unreadable directories raise rather than shrink the snapshot.
"""
    root = Path(root)

    def entry(path):
        info = os.lstat(path)
        return Entry(stat.S_IFMT(info.st_mode), info.st_mode, info.st_size, info.st_mtime_ns,
                     info.st_ctime_ns, info.st_ino, info.st_dev,
                     os.readlink(path) if stat.S_ISLNK(info.st_mode) else None)

    def fail(error):
        raise error

    snapshot = {'.': entry(root)}
    for directory, dirs, files in os.walk(root, onerror=fail):
        for name in dirs + files:
            path = Path(directory, name)
            snapshot[path.relative_to(root).as_posix()] = entry(path)
    return snapshot


def snapshot_changes(before, after, limit=10):
    """The first differences between two tree_snapshots, one phrase each."""
    changes = []
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path), after.get(path)
        if new is None:
            changes.append(f'{path} was removed')
        elif old is None:
            changes.append(f'{path} was added')
        elif old != new:
            fields = [field for field in Entry._fields if getattr(old, field) != getattr(new, field)]
            changes.append(f'{path} changed ({", ".join(fields)})')
        if len(changes) >= limit:
            break
    return changes


def basis_changed(result, changes, duration_seconds=0.0):
    """Fail a finished run whose basis B was modified while it ran.

B must stay immutable (SPEC section 4), whichever method had it, so this
overrides any other outcome: FAIL_BASIS, exit code 4, with the changes in
basis_verification.
"""
    error = 'basis changed during the run: ' + '; '.join(changes)
    check = result['basis_verification'] or {'duration_seconds': duration_seconds}
    check = {**check, 'exit_code': EXIT_VERIFICATION_FAILURE, 'error': error,
             'changes': list(changes)}
    return {**result, 'basis_verification': check, 'integrity_verified': False,
            'time_to_verified_seconds': None, 'status': 'FAIL_BASIS'}, EXIT_VERIFICATION_FAILURE


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


def verify_command(tool, output, manifest=None):
    if manifest is None:
        return [str(tool), str(output)]
    return [str(tool), '--manifest-path', str(manifest), str(output)]


def combine_command(tool, basis, increment, output, checksum='SHA256', mode='--copy'):
    """pg_combinebackup of basis + increment into output; mode is how it copies (--copy, or
    --copy-file-range, which clones on a filesystem with reflinks)."""
    return [str(tool), mode, '--manifest-checksums=' + checksum,
            '-o', str(output), str(basis), str(increment)]


def output_check_command(python, output, reference, independent_of=()):
    """Run benchmarks/output_check.py's source with python -I where commands run."""
    source = Path(output_check.__file__).read_text(encoding='utf-8')
    command = [str(python), '-I', '-c', source, '--reference', str(reference)]
    for path in independent_of:
        command += ['--independent-of', str(path)]
    return command + ['--', str(output)]


def output_check_report(text):
    """The JSON report from an output_check log, or None if it printed none."""
    for line in reversed(text.splitlines()):
        if line.startswith(output_check.REPORT_PREFIX):
            report = json.loads(line[len(output_check.REPORT_PREFIX):])
            if not isinstance(report, dict):
                raise ValueError('output check report must be a JSON object')
            return report
    return None


def successful(phase):
    return phase is not None and phase['exit_code'] == 0 and phase['error'] is None


def read_text(path):
    return Path(path).read_text(encoding='utf-8')


def is_byte_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def require_file_checksums(basis, read_file=read_text):
    """Reject bases whose verification cannot detect same-size file corruption.

pg_verifybackup remains responsible for manifest validity, checksum values and
WAL. This additional policy requires checksum coverage on every listed file.
read_file lets a frontend read the manifest where the commands run.
"""
    manifest = json.loads(read_file(Path(basis) / 'backup_manifest'))
    files = manifest.get('Files') if isinstance(manifest, dict) else None
    if not isinstance(files, list) or not files or any(
        not isinstance(entry, dict)
        or entry.get('Checksum-Algorithm') not in CHECKSUM_ALGORITHMS
        or not entry.get('Checksum') for entry in files
    ):
        raise ValueError('Basis requires file checksums on every manifest entry')


CHECKSUM_POLICY = 'every manifest entry has a file checksum'


def verify_basis(basis, *, execute, tools, read_file=read_text, verification=None):
    """pg_verifybackup on B, then require_file_checksums: one phase record.

verification, if given, is an earlier pg_verifybackup record for B (the
native suite's own), so only the policy runs. A record this function
returned is marked with checksum_policy and passes through unchanged, so a
frontend can verify B before its workload starts and hand the record to
run_backup(basis_verification=...).
"""
    check = verification
    if check is None:
        check = execute('basis_verification', verify_command(tools['pg_verifybackup'], basis))
    if not successful(check) or check.get('checksum_policy') == CHECKSUM_POLICY:
        return check
    start = time.perf_counter()
    try:
        require_file_checksums(basis, read_file)
    except (OSError, ValueError) as exc:
        check = {**check, 'exit_code': EXIT_VERIFICATION_FAILURE, 'error': str(exc)}
    else:
        check = {**check, 'checksum_policy': CHECKSUM_POLICY}
    return {**check, 'duration_seconds': check['duration_seconds'] + time.perf_counter() - start}


def classify(backup, verification):
    if not successful(backup):
        return 'FAIL_BACKUP', EXIT_IO_FAILURE
    if not successful(verification):
        return 'FAIL_VERIFY', EXIT_VERIFICATION_FAILURE
    return 'PASS_MANIFEST_ONLY', EXIT_OK


def transfer_network_bytes(transfer_stats):
    """Both-direction transfer bytes from a method's own counters, or None."""
    if not isinstance(transfer_stats, dict):
        return None
    forward = transfer_stats.get('bytes_source_to_dest')
    backward = transfer_stats.get('bytes_dest_to_source')
    if is_byte_count(forward) and is_byte_count(backward):
        return forward + backward
    return None


def build_result(run_id, method, backup, verification, backup_file_bytes, *,
                 basis_verification=None, increment_verification=None, combination=None,
                 transfer=None, transfer_stats=None,
                 output_file_bytes=None, method_seconds=None, verified_seconds=None,
                 failure=None):
    status, exit_code = failure or classify(backup, verification)
    verified = status == 'PASS_MANIFEST_ONLY'
    phases = (backup, increment_verification, combination, transfer)
    if method_seconds is None and backup is not None:
        method_seconds = sum(p['duration_seconds'] for p in phases if p is not None)
    if verified_seconds is None and verified:
        verified_seconds = method_seconds + verification['duration_seconds']
    network_bytes = transfer_network_bytes(transfer_stats)
    result = {
        'schema_version': SCHEMA_VERSION, 'run_id': run_id, 'method': method,
        'backup': backup, 'verification': verification,
        'basis_verification': basis_verification,
        'increment_verification': increment_verification, 'combination': combination,
        'transfer': transfer, 'transfer_stats': transfer_stats,
        'backup_file_bytes': backup_file_bytes,
        'output_file_bytes': output_file_bytes,
        'method_seconds': method_seconds,
        'time_to_verified_seconds': verified_seconds if verified else None,
        'integrity_verified': verified, 'restore_tested': False,
        'network_bytes': network_bytes,
        'unavailable_metrics': ({'network_bytes': NO_TRANSPORT_OBSERVER} if network_bytes is None
                                else {'interface_bytes': SELF_REPORTED_BYTES}),
        'status': status,
    }
    assert tuple(result) == RESULT_KEYS
    return result, exit_code


def _context(method, *, run_id, output, tools, execute, host, port, user, basis,
             increment, capture_path, checksum, checkpoint, progress, read_log, read_file):
    # Imported here because methods.py builds on this module's command helpers.
    from benchmarks import methods
    spec = methods.get(method)
    if checksum not in CHECKSUM_ALGORITHMS:
        raise ValueError('File checksums are required for verified benchmark results')
    if increment is not None and capture_path is not None:
        raise ValueError('Pass either increment or capture_path, not both')
    return spec, methods.context(
        spec, run_id=run_id, output=output, tools=tools, execute=execute, host=host,
        port=port, user=user, basis=basis,
        capture=capture_path if capture_path is not None else increment,
        checksum=checksum, checkpoint=checkpoint, progress=progress,
        read_log=read_log, read_file=read_file or read_text)


def prepare_backup(method, *, run_id, output, tools, execute, host, port=5432,
                   user='postgres', basis=None, increment=None, capture_path=None,
                   checksum='SHA256', checkpoint='fast', progress=False,
                   read_log=None, read_file=None):
    """Run a method's untimed preparation early, e.g. before a workload starts.

Returns its phase records, or None for a method without preparation. Pass the
result to run_backup(prepared=...) with the same arguments.
"""
    spec, ctx = _context(
        method, run_id=run_id, output=output, tools=tools, execute=execute, host=host,
        port=port, user=user, basis=basis, increment=increment, capture_path=capture_path,
        checksum=checksum, checkpoint=checkpoint, progress=progress,
        read_log=read_log, read_file=read_file)
    return spec.prepare(ctx) if spec.prepare else None


def check_output(ctx, verification):
    """The harness-only check that O is an independent, exact copy of S.

pg_verifybackup --manifest-path S/backup_manifest O does not look at every
file of O and cannot see a symlinked or hard-linked O; see output_check.py.
A failure marks the verification failed and names the first differences.
"""
    independent_of = [ctx.capture] + ([ctx.basis] if ctx.basis is not None else [])
    check = ctx.execute('output_check', output_check_command(
        ctx.tools['python'], ctx.output, ctx.capture, independent_of))
    report = None
    if ctx.read_log is not None:
        try:
            report = output_check_report(ctx.read_log('output_check'))
        except (OSError, ValueError):
            report = None
    differences = list((report or {}).get('differences') or [])
    record = {**check, 'differences': differences,
              'report': None if report is None else
              {key: value for key, value in report.items() if key != 'differences'}}
    verification = {**verification, 'output_check': record}
    if successful(check):
        return verification
    if differences:
        shown = '; '.join(differences[:5])
        more = ' (more in the output_check log)' if len(differences) > 5 else ''
        error = (f'output is not an independent, exact copy of the staged backup '
                 f'{ctx.capture}: {shown}{more}')
    else:
        error = (f'output check failed ({check["error"] or "exit code " + str(check["exit_code"])}); '
                 'see the output_check log')
    return {**verification, 'error': error}


def run_backup(method, *, run_id, output, tools, execute, host, port=5432,
               user='postgres', basis=None, increment=None, capture_path=None,
               checksum='SHA256', checkpoint='fast', progress=False,
               basis_verification=None, prepared=None, read_log=None, read_file=None,
               after_verification=None):
    """Prepare -> method phases -> verify, with one shared v3 record.

B0 verification and method preparation are prerequisites outside refresh
timing. Initial B0 creation and its storage must be disclosed by the enclosing
experiment. Failed phases short-circuit; their outcome never becomes a
successful performance sample. capture_path (or its older name increment) is
the separate capture directory of methods that declare one.

after_verification, if given, is called once, right after the timed phases
and the final pg_verifybackup (also when a phase failed and verification was
skipped), before any harness-only check such as the staged methods' output
check, so a frontend can close its measurement window there. It is not
called when the run stops at FAIL_BASIS or FAIL_PREPARE, before the timer.
"""
    spec, ctx = _context(
        method, run_id=run_id, output=output, tools=tools, execute=execute, host=host,
        port=port, user=user, basis=basis, increment=increment, capture_path=capture_path,
        checksum=checksum, checkpoint=checkpoint, progress=progress,
        read_log=read_log, read_file=read_file)
    if prepared is not None and spec.prepare is None:
        raise ValueError(f'{spec.name} has no preparation phase')
    basis_check = basis_verification
    if spec.trusts_basis:
        basis_check = verify_basis(basis, execute=execute, tools=tools, read_file=ctx.read_file,
                                   verification=basis_verification)
        if not successful(basis_check):
            return build_result(run_id, method, None, None, None,
                                basis_verification=basis_check,
                                failure=('FAIL_BASIS', EXIT_VERIFICATION_FAILURE))
    if spec.prepare is not None:
        if prepared is None:
            prepared = spec.prepare(ctx)
        ctx = replace(ctx, prepared=prepared)
        if not all(successful(phase) for phase in prepared.values()):
            return build_result(run_id, method, None, None, None,
                                basis_verification=basis_check,
                                transfer_stats={'preparation': prepared},
                                failure=('FAIL_PREPARE', EXIT_IO_FAILURE))
    start = time.perf_counter()
    phases = spec.run(ctx)
    method_seconds = time.perf_counter() - start
    failure = phases.get('failure')
    verification = None
    if failure is None:
        verification = execute('verification', verify_command(
            tools['pg_verifybackup'], output, manifest=phases.get('verify_manifest')))
    verified_seconds = time.perf_counter() - start
    if after_verification is not None:
        after_verification()
    # The oracle needs S beside O, which only the harness has, so it is
    # outside time_to_verified_seconds.
    if spec.output_matches_capture and successful(verification):
        verification = check_output(ctx, verification)
    stats = phases.get('transfer_stats')
    if prepared is not None:
        stats = {**(stats or {}), 'preparation': prepared}
    capture_dir = ctx.capture if ctx.capture is not None else output
    return build_result(
        run_id, method, phases.get('backup'), verification, directory_bytes(capture_dir),
        basis_verification=basis_check,
        increment_verification=phases.get('increment_verification'),
        combination=phases.get('combination'), transfer=phases.get('transfer'),
        transfer_stats=stats, output_file_bytes=directory_bytes(output),
        method_seconds=method_seconds, verified_seconds=verified_seconds, failure=failure)
