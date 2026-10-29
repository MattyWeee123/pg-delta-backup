"""Registry of backup methods the measurement harness can run.

A method runs its phases through the caller's execute(stage, argv), so each
frontend keeps its own logs and timers, and returns the phase records the
shared v3 result needs. benchmarks.backup.run_backup owns basis verification,
the untimed preparation, the timer and final verification. Native/staged
methods use pg_verifybackup; live methods explicitly use engine/WAL evidence.

To add a method, write a run function and register() a Method in this module.
The CLI, the native suite and the HammerDB driver take their choices from
names(). See benchmarks/CONSOLIDATION.md for the timing semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Callable

from benchmarks.backup import (EXIT_IO_FAILURE, EXIT_VERIFICATION_FAILURE,
                               basebackup_command, combine_command, is_byte_count,
                               successful, verify_command)


@dataclass(frozen=True)
class Context:
    """One run's inputs. Paths stay as the caller gave them."""

    run_id: str
    output: object
    capture: object
    basis: object
    tools: dict
    execute: Callable
    read_log: Callable | None
    read_file: Callable
    host: object
    port: int
    user: str
    checksum: str
    checkpoint: str
    progress: bool
    prepared: dict | None = None
    source_pgdata: object = None
    options: dict | None = None


@dataclass(frozen=True)
class Method:
    """A registered backup method.

    run(ctx) returns a dict with any of 'backup', 'increment_verification',
    'combination' and 'transfer' (phase records), 'transfer_stats' (a dict,
    whose bytes_source_to_dest and bytes_dest_to_source fill network_bytes),
    'failure' ((status, exit code) or None) and 'verify_manifest' (the
    manifest that must verify the output; the output's own when absent).

    capture_dir names the separate capture directory (the increment or the
    staged backup S); None means the capture is the output. trusts_basis
    means the method reuses basis bytes unchecked, so the basis must first
    pass pg_verifybackup and the checksum policy. output_matches_capture
    means O must be an independent, byte-identical copy of the capture: once
    the final pg_verifybackup passes, run_backup runs benchmarks/output_check.py
    with tools['python'] against S and B. prepare(ctx), if set, runs
    before the timer and returns phase records keyed by stage. tools must
    all be supplied; optional_tools are passed on only when a frontend has
    them.

    source_local requires source_pgdata beside the executing process. verify,
    when set, replaces final pg_verifybackup and requires the explicit
    PASS_ENGINE_ONLY success status. It never supplies a restore verdict.
    """

    name: str
    needs_basis: bool
    run: Callable[[Context], dict]
    capture_dir: str | None = None
    trusts_basis: bool = False
    output_matches_capture: bool = False
    prepare: Callable[[Context], dict] | None = None
    tools: tuple = ('pg_basebackup', 'pg_verifybackup')
    optional_tools: tuple = ()
    needs_logs: bool = False
    description: str = ''
    source_local: bool = False
    verify: Callable | None = None
    success_status: str = 'PASS_MANIFEST_ONLY'


_REGISTRY: dict[str, Method] = {}
# The native suite's workload-only control is a treatment, not a method.
RESERVED = frozenset({'no_backup'})
NAME = re.compile(r'[a-z][a-z0-9_]*\Z')


def register(method):
    if not NAME.match(method.name) or method.name in RESERVED:
        raise ValueError(f'Invalid method name: {method.name!r}')
    if method.name in _REGISTRY:
        raise ValueError(f'Method already registered: {method.name}')
    if method.trusts_basis and not method.needs_basis:
        raise ValueError(f'{method.name} cannot trust a basis it does not take')
    if method.output_matches_capture and (method.capture_dir is None
                                          or 'python' not in method.tools):
        raise ValueError(f'{method.name} needs a capture directory and the python tool '
                         'for its output check')
    if method.success_status not in ('PASS_MANIFEST_ONLY', 'PASS_ENGINE_ONLY'):
        raise ValueError('Unknown method success status')
    if (method.verify is None) != (method.success_status == 'PASS_MANIFEST_ONLY'):
        raise ValueError('A custom verifier requires its own engine-only success status')
    _REGISTRY[method.name] = method
    return method


def get(name):
    try:
        return _REGISTRY[name]
    except (KeyError, TypeError):
        raise ValueError(f'Unknown backup method: {name!r}') from None


def names():
    return tuple(sorted(_REGISTRY))


def context(spec, *, capture, basis, tools, read_log, **fields):
    """Validate a method's inputs before anything executes."""
    if spec.needs_basis and basis is None:
        raise ValueError(f'{spec.name} requires a basis (the old copy)')
    if not spec.needs_basis and basis is not None:
        raise ValueError(f'{spec.name} must not use a basis')
    if spec.capture_dir is not None and capture is None:
        raise ValueError(f'{spec.name} requires a fresh {spec.capture_dir} path')
    if spec.capture_dir is None and capture is not None:
        raise ValueError(f'{spec.name} captures straight into the output')
    missing = [name for name in spec.tools if not tools.get(name)]
    if missing:
        raise ValueError(f'{spec.name} requires tools: {", ".join(missing)}')
    if spec.needs_logs and read_log is None:
        raise ValueError(f'{spec.name} parses command output; pass read_log')
    if spec.source_local and not fields.get('source_pgdata'):
        raise ValueError(f'{spec.name} requires source_pgdata on the execution host')
    jobs = (fields.get('options') or {}).get('jobs')
    if jobs is not None and (type(jobs) is not int or jobs < 1):
        raise ValueError('pgdelta jobs must be a positive integer')
    return Context(capture=capture, basis=basis, tools=tools, read_log=read_log, **fields)


def _capture(ctx, path, basis=None):
    return ctx.execute('backup', basebackup_command(
        ctx.tools['pg_basebackup'], path, host=ctx.host, port=ctx.port, user=ctx.user,
        checksum=ctx.checksum, checkpoint=ctx.checkpoint, basis=basis,
        progress=ctx.progress))


def _failed(phase, status, code):
    return None if successful(phase) else (status, code)


# --- native pg_basebackup methods ------------------------------------------

def run_full(ctx):
    capture = _capture(ctx, Path(ctx.output))
    return {'backup': capture, 'failure': _failed(capture, 'FAIL_BACKUP', EXIT_IO_FAILURE)}


def run_incremental(ctx):
    capture = _capture(ctx, Path(ctx.capture), ctx.basis)
    if not successful(capture):
        return {'backup': capture, 'failure': ('FAIL_BACKUP', EXIT_IO_FAILURE)}
    check = ctx.execute('increment_verification',
                        verify_command(ctx.tools['pg_verifybackup'], ctx.capture))
    if not successful(check):
        return {'backup': capture, 'increment_verification': check,
                'failure': ('FAIL_INCREMENT_VERIFY', EXIT_VERIFICATION_FAILURE)}
    combination = ctx.execute('combination', combine_command(
        ctx.tools['pg_combinebackup'], ctx.basis, ctx.capture, ctx.output, ctx.checksum))
    return {'backup': capture, 'increment_verification': check, 'combination': combination,
            'failure': _failed(combination, 'FAIL_COMBINE', EXIT_IO_FAILURE)}


# --- staged transfer methods -----------------------------------------------
#
# Both capture a fresh full backup S on the source side and build the output
# O from S and the old copy B without modifying B. S's own manifest verifies
# O, but pg_verifybackup skips backup_manifest, postgresql.auto.conf, *.signal,
# pg_wal entries outside the backup's WAL range and empty directories, and
# reads through a symlinked or hard-linked O. So output_matches_capture makes
# run_backup check afterwards that O is an independent, exact copy of S.

def _staged(ctx):
    capture = _capture(ctx, Path(ctx.capture))
    phases = {'backup': capture, 'verify_manifest': Path(ctx.capture) / 'backup_manifest'}
    if not successful(capture):
        phases['failure'] = ('FAIL_BACKUP', EXIT_IO_FAILURE)
    return phases


def _transferred(phases, transfer, stats, read_stats):
    if successful(transfer):
        try:
            stats.update(read_stats())
        except (OSError, ValueError) as exc:
            transfer = {**transfer, 'error': f'unusable transfer statistics: {exc}'}
    return {**phases, 'transfer': transfer, 'transfer_stats': stats,
            'failure': _failed(transfer, 'FAIL_TRANSFER', EXIT_IO_FAILURE)}


RSYNC_OPTIONS = ('--archive', '--checksum', '--no-whole-file', '--inplace',
                 '--block-size=8192', '--delete', '--stats')
RSYNC_SCOPE = ('rsync --stats protocol-stream counters; sent is source to destination '
               'for this local push; excludes SSH/TCP framing')
# -I stops a module in the working directory from shadowing shutil.
COPY_SCRIPT = 'import shutil, sys; shutil.copytree(sys.argv[1], sys.argv[2], symlinks=True)'


def rsync_command(rsync, source, destination):
    # Trailing separators make rsync update O's contents from S's contents.
    return [str(rsync), *RSYNC_OPTIONS, os.path.join(str(source), ''),
            os.path.join(str(destination), '')]


def copy_command(python, source, destination):
    """Copy B to a new O; copytree refuses an existing destination."""
    return [str(python), '-I', '-c', COPY_SCRIPT, str(source), str(destination)]


def rsync_version(text):
    """The version banner, e.g. 'rsync version 3.4.1 protocol version 32'."""
    lines = [' '.join(line.split()) for line in text.splitlines()
             if line.strip() and not line.startswith('$ ')]
    return ' / '.join(line for line in lines[:3] if 'version' in line.lower()) or None


# GNU rsync 3.x and openrsync label the same counters differently.
RSYNC_FIELDS = {
    'Number of files': 'files',
    'Number of created files': 'files_created',
    'Number of deleted files': 'files_deleted',
    'Number of regular files transferred': 'files_transferred',
    'Number of files transferred': 'files_transferred',
    'Total file size': 'total_file_bytes',
    'Total transferred file size': 'transferred_file_bytes',
    'Literal data': 'literal_bytes',
    'Unmatched data': 'literal_bytes',
    'Matched data': 'matched_bytes',
    'File list size': 'file_list_bytes',
    'Total bytes sent': 'bytes_sent',
    'Total sent': 'bytes_sent',
    'Total bytes received': 'bytes_received',
    'Total received': 'bytes_received',
}
RSYNC_REQUIRED = ('bytes_sent', 'bytes_received', 'literal_bytes', 'matched_bytes')
# A count, digit grouping in any locale, an optional unit, then the end of the
# value or GNU's "(reg: ...)" breakdown. Human-readable sizes such as 57.35K
# do not match and are refused rather than misread.
RSYNC_VALUE = re.compile(r"\s*(\d[\d,.'\s]*?)\s*(?:bytes|B)?\s*(?:\(.*)?\Z")


def parse_rsync_stats(text):
    """Counters from rsync --stats output, in GNU 3.x or openrsync layout."""
    stats, labels = {}, set()
    for line in text.splitlines():
        label, colon, value = line.partition(':')
        key = RSYNC_FIELDS.get(label.strip())
        if not colon or key is None:
            continue
        match = RSYNC_VALUE.match(value)
        if match is None:
            raise ValueError(f'unrecognised rsync value: {line.strip()!r}')
        number = int(re.sub(r'\D', '', match.group(1)))
        if stats.get(key, number) != number:
            raise ValueError(f'conflicting rsync counters for {key}')
        stats[key] = number
        labels.add(label.strip())
    missing = [key for key in RSYNC_REQUIRED if key not in stats]
    if missing:
        raise ValueError('rsync --stats output lacks ' + ', '.join(missing))
    stats['stats_format'] = 'openrsync' if 'Total sent' in labels else 'gnu'
    return stats


def rsync_transfer_stats(text):
    stats = parse_rsync_stats(text)
    return {**stats, 'bytes_source_to_dest': stats['bytes_sent'],
            'bytes_dest_to_source': stats['bytes_received']}


def _read_log(ctx, stage):
    try:
        return ctx.read_log(stage)
    except OSError:
        return ''


def prepare_rsync_staged(ctx):
    """Record the rsync version, then copy B to O; both outside the timer."""
    probe = ctx.execute('rsync_version', [str(ctx.tools['rsync']), '--version'])
    if not successful(probe):
        return {'rsync_version': probe}
    probe = {**probe, 'version': rsync_version(_read_log(ctx, 'rsync_version'))}
    return {'rsync_version': probe, 'output_preparation': ctx.execute(
        'output_preparation', copy_command(ctx.tools['python'], ctx.basis, ctx.output))}


def run_rsync_staged(ctx):
    phases = _staged(ctx)
    if phases.get('failure'):
        return phases
    transfer = ctx.execute('transfer', rsync_command(ctx.tools['rsync'], ctx.capture, ctx.output))
    probe = (ctx.prepared or {}).get('rsync_version') or {}
    stats = {'tool': 'rsync', 'version': probe.get('version'),
             'options': list(RSYNC_OPTIONS), 'byte_count_scope': RSYNC_SCOPE}
    return _transferred(phases, transfer, stats,
                        lambda: rsync_transfer_stats(ctx.read_log('transfer')))


PGDELTA_BYTE_KEYS = ('bytes_source_to_dest', 'bytes_dest_to_source')
PGDELTA_SCOPE = 'as reported in the pgdelta --stats-json file'


def pgdelta_command(tools):
    """The pgdelta CLI prefix: tools['pgdelta'] if given, else python -m pgdelta."""
    prefix = tools.get('pgdelta')
    if prefix is None:
        return [str(tools['python']), '-m', 'pgdelta']
    if isinstance(prefix, (str, os.PathLike)):
        return [str(prefix)]
    return [str(part) for part in prefix]


def pgdelta_stats_path(output):
    # A sibling of O: receipts stay out of the data directory.
    output = Path(output)
    return output.with_name(output.name + '-pgdelta-stats.json')


def _no_constant(name):
    raise ValueError(f'{name} is not a byte count')


def parse_pgdelta_stats(text):
    reported = json.loads(text, parse_constant=_no_constant)
    if not isinstance(reported, dict):
        raise ValueError('pgdelta statistics must be a JSON object')
    counts = {}
    for key in PGDELTA_BYTE_KEYS:
        value = reported.get(key)
        # pgdelta reports {'total': n, <category>: n, ...}; a plain integer is also accepted.
        if isinstance(value, dict):
            value = value.get('total')
        if not is_byte_count(value):
            raise ValueError(f'{key} must be a non-negative integer or {{"total": n}}')
        counts[key] = value
    return {**counts, 'reported': reported}


def run_pg_delta_staged(ctx):
    phases = _staged(ctx)
    if phases.get('failure'):
        return phases
    prefix = pgdelta_command(ctx.tools)
    stats_path = pgdelta_stats_path(ctx.output)
    argv = [*prefix, 'local', '--source', str(ctx.capture), '--basis', str(ctx.basis),
            '--output', str(ctx.output), '--stats-json', str(stats_path)]
    if ctx.tools.get('pg_controldata'):
        argv += ['--pg-controldata', str(ctx.tools['pg_controldata'])]
    transfer = ctx.execute('transfer', argv)
    stats = {'tool': 'pgdelta', 'command': prefix, 'stats_file': str(stats_path),
             'byte_count_scope': PGDELTA_SCOPE}
    return _transferred(phases, transfer, stats,
                        lambda: parse_pgdelta_stats(ctx.read_file(stats_path)))


def libpq_conninfo(*, host, port, user):
    # Conninfo uses its own escaping rules, not shell escaping. Authentication
    # stays in libpq's environment/.pgpass, never in the recorded argv.
    def quote(value):
        return "'" + str(value).replace('\\', '\\\\').replace("'", "\\'") + "'"
    return ' '.join(f'{key}={quote(value)}' for key, value in
                    (('host', host), ('port', port), ('user', user), ('dbname', 'postgres')))


def prepare_pg_delta_live(ctx):
    return {'output_preparation': ctx.execute(
        'output_preparation', copy_command(ctx.tools['python'], ctx.basis, ctx.output))}


def run_pg_delta_live(ctx, *, summaries=False):
    prefix = pgdelta_command(ctx.tools)
    stats_path = pgdelta_stats_path(ctx.output)
    argv = [*prefix, 'local', '--source', str(ctx.source_pgdata), '--source-kind', 'live',
            '--pg-conn', libpq_conninfo(host=ctx.host, port=ctx.port, user=ctx.user),
            '--basis', str(ctx.output), '--in-place', '--checkpoint', ctx.checkpoint,
            '--pg-controldata', str(ctx.tools['pg_controldata']),
            '--psql', str(ctx.tools['psql']), '--stats-json', str(stats_path)]
    jobs = (ctx.options or {}).get('jobs')
    if jobs is not None:
        argv += ['--jobs', str(jobs)]
    if summaries:
        argv += ['--fast-path', 'summaries']
    backup = ctx.execute('backup', argv)
    phases = {'backup': backup, 'failure': _failed(backup, 'FAIL_BACKUP', EXIT_IO_FAILURE)}
    if not successful(backup):
        return phases
    try:
        stats = parse_pgdelta_stats(ctx.read_file(stats_path))
    except (OSError, ValueError) as exc:
        return {**phases, 'failure': ('FAIL_TRANSFER', EXIT_IO_FAILURE),
                'transfer_stats': {'error': f'unusable live statistics: {exc}'}}
    return {**phases, 'transfer_stats': {
        **stats, 'tool': 'pgdelta', 'command': prefix, 'stats_file': str(stats_path),
        'byte_count_scope': PGDELTA_SCOPE, 'fast_path_requested': summaries}}


def run_pg_delta_live_summaries(ctx):
    return run_pg_delta_live(ctx, summaries=True)


def verify_pg_delta_live(ctx, phases):
    from benchmarks import live_verify
    # Run where the method runs, including inside a Docker tools container.
    source = Path(live_verify.__file__).read_text(encoding='utf-8')
    phase = ctx.execute('verification', [str(ctx.tools['python']), '-I', '-c', source,
        '--output', str(ctx.output), '--stats', str(pgdelta_stats_path(ctx.output)),
        '--pg-waldump', str(ctx.tools['pg_waldump']),
        '--pg-controldata', str(ctx.tools['pg_controldata'])])
    if not successful(phase):
        return {**phase, 'kind': 'pgdelta_receipt_and_wal'}
    try:
        # CLI/container logs include the command header (and stderr); the
        # native executor keeps stdout separate. In either case the verifier
        # emits exactly one final JSON line.
        lines = ctx.read_log('verification').splitlines()
        report = json.loads(next(line for line in reversed(lines) if line.startswith('{')))
        endpoint = report.get('recovery_endpoint') or {}
        if (report.get('status') != 'pass' or report.get('kind') != 'pgdelta_receipt_and_wal'
                or endpoint.get('source') != 'pgdelta.live.stop_lsn'
                or not endpoint.get('lsn') or not endpoint.get('system_identifier')
                or type(endpoint.get('timeline')) is not int):
            raise ValueError('Live verifier did not report pass')
        return {**phase, 'kind': 'pgdelta_receipt_and_wal', 'report': report}
    except (OSError, ValueError, AttributeError, StopIteration) as exc:
        return {**phase, 'kind': 'pgdelta_receipt_and_wal',
                'error': str(exc) or 'Live verifier emitted no JSON report'}


register(Method(
    'pg_basebackup_full', needs_basis=False, run=run_full,
    description='plain pg_basebackup with streamed WAL'))
register(Method(
    'pg_basebackup_incremental', needs_basis=True, run=run_incremental,
    capture_dir='increment', trusts_basis=True,
    tools=('pg_basebackup', 'pg_verifybackup', 'pg_combinebackup'),
    description='PG17 incremental against the basis manifest, then pg_combinebackup --copy'))
register(Method(
    'rsync_staged', needs_basis=True, run=run_rsync_staged, capture_dir='staging',
    output_matches_capture=True, prepare=prepare_rsync_staged, needs_logs=True,
    tools=('pg_basebackup', 'pg_verifybackup', 'rsync', 'python'),
    description='full capture S, then rsync delta from S into a copy of the basis'))
register(Method(
    'pg_delta_staged', needs_basis=True, run=run_pg_delta_staged, capture_dir='staging',
    output_matches_capture=True,
    tools=('pg_basebackup', 'pg_verifybackup', 'python'),
    optional_tools=('pgdelta', 'pg_controldata'),
    description='full capture S, then python -m pgdelta local from S and the basis'))

for name, run in (('pg_delta_live', run_pg_delta_live),
                  ('pg_delta_live_summaries', run_pg_delta_live_summaries)):
    register(Method(
        name, needs_basis=True, run=run, trusts_basis=True, source_local=True,
        prepare=prepare_pg_delta_live, verify=verify_pg_delta_live,
        success_status='PASS_ENGINE_ONLY', needs_logs=True,
        tools=('pg_verifybackup', 'python', 'pg_controldata', 'psql', 'pg_waldump'),
        optional_tools=('pgdelta',),
        description='live pgdelta in place on a prepared copy of B; engine receipt and WAL verification'))
