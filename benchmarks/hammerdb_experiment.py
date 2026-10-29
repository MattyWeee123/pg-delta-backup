"""HammerDB TPROC-C degradation experiment driver.

Runs a matched pair of TPROC-C trials, one without a backup and one with a
backup inside the measurement window, and reports throughput degradation over
aligned windows rather than over whole runs.

Measurement sources, in order of preference:

* ``JOBTCOUNT`` in HammerDB's jobs database holds the transaction-counter
  series with timestamps. It is structured, so it is preferred over scraping
  ``docker logs``. The log is still retained as raw evidence.
* ``JOBTIMING`` holds per-transaction latency percentiles. Its ``summary``
  rows aggregate all virtual users.

Latency limitation: ``JOBTIMING`` percentiles cover a virtual user's whole
elapsed time, including ramp-up. HammerDB records no time dimension for
latency, so a window-aligned percentile cannot be derived from its
instrumentation. Latency comparisons are therefore labelled whole-run and are
not matched to the backup window the way throughput is.

Run this on Linux. The driver passes container paths such as
``/results/<trial>`` to the shared backup adapters, which wrap them in
``pathlib.Path``. On Windows that rewrites them with backslashes and the
container command fails. Orchestrating a Linux fixture from a Windows host is
therefore unsupported; use WSL, a Linux host, or a container with access to
the docker socket. The parsing and arithmetic in this module are platform
independent and their tests run anywhere.

This module does not test restore and does not certify TPROC-C recovery. See
benchmarks/CONSOLIDATION.md.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from benchmarks import methods
from benchmarks.provenance import source_provenance
from benchmarks.backup import (NO_TRANSPORT_OBSERVER, SELF_REPORTED_BYTES, SUCCESS_STATUSES, atomic_json,
                               prepare_backup, run_backup, run_command, successful,
                               verify_basis)

# HammerDB writes naive local timestamps; the official image runs UTC, which
# the time profile header states explicitly. Parsing as UTC keeps these
# comparable with `docker logs -t`, which is always UTC.
JOBS_TIMESTAMP = '%Y-%m-%d %H:%M:%S'

TEST_RESULT = re.compile(
    r'System achieved\s+(?P<nopm>\d+)\s+NOPM\s+from\s+(?P<tpm>\d+)\s+'
    r'PostgreSQL\s+TPM')
RAMPUP_COMPLETE = 'Rampup complete'
TEST_COMPLETE = 'Test complete'


@dataclass(frozen=True)
class Sample:
    """One transaction-counter observation."""

    at: datetime
    tpm: int


def _as_utc(raw):
    return datetime.strptime(raw, JOBS_TIMESTAMP).replace(tzinfo=timezone.utc)


def read_tpm_series(database, jobid=None):
    """Transaction-counter samples from a HammerDB jobs database.

    Cadence is nominally ten seconds but is not guaranteed: observed runs
    contain shorter final intervals. Callers must not assume a fixed spacing.
    """
    query = "SELECT counter, timestamp FROM JOBTCOUNT WHERE metric = 'tpm'"
    parameters = ()
    if jobid is not None:
        query += ' AND jobid = ?'
        parameters = (jobid,)
    query += ' ORDER BY timestamp'
    connection = _readonly(database)
    try:
        rows = connection.execute(query, parameters).fetchall()
    finally:
        connection.close()
    return [Sample(_as_utc(stamp), int(counter)) for counter, stamp in rows]


def read_latency(database, jobid=None):
    """Whole-run latency percentiles per stored procedure.

    Returns the ``summary`` rows, which aggregate every virtual user. These
    cover the entire elapsed time including ramp-up; see the module docstring.
    """
    query = (
        'SELECT procname, calls, avg_ms, p50_ms, p95_ms, p99_ms, elapsed_ms '
        'FROM JOBTIMING WHERE summary = 1')
    parameters = ()
    if jobid is not None:
        query += ' AND jobid = ?'
        parameters = (jobid,)
    connection = _readonly(database)
    try:
        rows = connection.execute(query, parameters).fetchall()
    finally:
        connection.close()
    if not rows:
        return {
            'scope': 'whole_run',
            'procedures': {},
            'unavailable': 'no summary rows in JOBTIMING',
        }
    procedures = {
        name: {
            'calls': int(calls),
            'avg_ms': avg,
            'p50_ms': p50,
            'p95_ms': p95,
            'p99_ms': p99,
        }
        for name, calls, avg, p50, p95, p99, _ in rows
    }
    return {
        # Named so a reader cannot mistake this for a window-aligned figure.
        'scope': 'whole_run',
        'elapsed_ms': rows[0][6],
        'procedures': procedures,
        # NEWORD is the transaction NOPM counts, so it is the primary series.
        'primary_procedure': 'NEWORD',
        'p95_ms': procedures.get('NEWORD', {}).get('p95_ms'),
    }


def read_jobid(database):
    """The single job recorded by one run, or None when absent or ambiguous."""
    connection = _readonly(database)
    try:
        rows = connection.execute('SELECT jobid FROM JOBMAIN').fetchall()
    finally:
        connection.close()
    return rows[0][0] if len(rows) == 1 else None


def parse_test_result(log_text):
    """NOPM and TPM from the HammerDB result line.

    The jobs database stores the counter series but not this aggregate, so the
    log remains the source for it. Returns None when the line is absent, which
    happens for a run that did not reach its result.
    """
    match = TEST_RESULT.search(log_text)
    if match is None:
        return None
    return {'nopm': int(match.group('nopm')), 'tpm': int(match.group('tpm'))}


# docker logs -t emits RFC3339 with nanosecond precision, which datetime
# cannot represent. The fraction and the offset must be separated explicitly:
# collecting "all digits" also sweeps up the offset's digits and silently
# discards the zone, which turns +05:30 into UTC.
RFC3339 = re.compile(
    r'(?P<base>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})'
    r'(?:\.(?P<fraction>\d+))?'
    r'(?P<offset>Z|[+-]\d{2}:?\d{2})?$')


def _leading_timestamp(line):
    """Parse the RFC3339 prefix that ``docker logs -t`` prepends."""
    head = line.split(' ', 1)[0]
    match = RFC3339.match(head) if head else None
    if match is None:
        return None
    text = match.group('base')
    if match.group('fraction'):
        # Truncate toward zero; microseconds is the most datetime can hold.
        text += '.' + match.group('fraction')[:6].ljust(6, '0')
    offset = match.group('offset')
    if offset and offset != 'Z':
        text += offset
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # A log without a zone is assumed UTC, which is what docker emits.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _readonly(database):
    """A read-only connection that cannot be fooled by characters in the path.

    Formatting a path into a URI leaves '#' acting as a fragment delimiter, so
    SQLite would silently open a different database. Path.as_uri percent-
    encodes it.
    """
    uri = Path(database).resolve().as_uri() + '?mode=ro'
    return sqlite3.connect(uri, uri=True)


def parse_log_markers(log_text):
    """Ramp-up and completion timestamps from ``docker logs -t`` output.

    These bound the measurement window. A missing marker is reported as None
    so a caller fails the trial rather than silently measuring the wrong span.
    """
    markers: dict[str, datetime | None] = {
        'rampup_complete': None, 'test_complete': None}
    # HammerDB emits a carriage return between the docker timestamp and the
    # message. Whether the marker text still shares a line with its timestamp
    # depends on how the caller read the file: Python's universal-newline
    # translation turns a lone \r into \n and splits them apart. Carry the
    # most recent timestamp forward so both shapes parse identically.
    pending = None
    for line in log_text.replace('\r', '\n').split('\n'):
        stamp = _leading_timestamp(line)
        if stamp is not None:
            pending = stamp
        if pending is None:
            continue
        if markers['rampup_complete'] is None and RAMPUP_COMPLETE in line:
            markers['rampup_complete'] = pending
        elif markers['test_complete'] is None and TEST_COMPLETE in line:
            markers['test_complete'] = pending
    return markers


def read_log(path):
    """Read a container log without newline translation.

    Opening in the default text mode rewrites HammerDB's carriage returns,
    which separates a marker from its timestamp. parse_log_markers tolerates
    either shape, but preserving the bytes keeps the retained evidence faithful.
    """
    with open(path, encoding='utf-8', errors='replace', newline='') as stream:
        return stream.read()


# --- windows and degradation -------------------------------------------------

# Matches the existing convention that a command with fewer than two live
# samples reports its measurements as null rather than as a number.
MIN_SAMPLES = 2


@dataclass(frozen=True)
class Window:
    """A half-open measurement interval, exclusive of start, inclusive of end."""

    start: datetime
    end: datetime

    @property
    def duration_seconds(self):
        return (self.end - self.start).total_seconds()

    def contains(self, other):
        return self.start <= other.start and other.end <= self.end

    def as_dict(self):
        return {
            'start': self.start.isoformat(),
            'end': self.end.isoformat(),
            'duration_seconds': round(self.duration_seconds, 3),
        }


def samples_in(series, window):
    """Samples attributed to a window.

    A counter sample timestamped T reports work over the interval preceding T,
    so attribution uses (start, end]. The cadence is not fixed, so the caller
    is given the sample count and observed span rather than an assumed one.
    """
    return [s for s in series if window.start < s.at <= window.end]


def window_stats(series, window):
    """Mean throughput over a window, or an explicit reason it is unavailable.

    The mean is a plain average of counter samples, not time weighted. With an
    irregular cadence that is an approximation, so the sample count and the
    observed span are reported alongside it.
    """
    selected = samples_in(series, window)
    stats = {
        'window': window.as_dict(),
        'samples': len(selected),
        'mean_tpm': None,
        'min_tpm': None,
        'max_tpm': None,
        'observed_span_seconds': None,
        'unavailable': None,
    }
    if len(selected) < MIN_SAMPLES:
        stats['unavailable'] = (
            f'fewer than {MIN_SAMPLES} counter samples in window')
        return stats
    values = [s.tpm for s in selected]
    stats.update(
        mean_tpm=round(sum(values) / len(values), 1),
        min_tpm=min(values),
        max_tpm=max(values),
        observed_span_seconds=round(
            (selected[-1].at - selected[0].at).total_seconds(), 3),
    )
    return stats


def analysis_window(capture_started_at, verified_at):
    """The interval the backup actually occupied, capture through verification."""
    return Window(capture_started_at, verified_at)


def recovery_window(analysis, whole):
    """From the end of the backup to the end of the measurement window.

    Throughput stayed depressed after capture finished in the October 9 runs,
    which no whole-window or analysis-window figure exposes.
    """
    if analysis.end >= whole.end:
        return None
    return Window(analysis.end, whole.end)


def matched_window(control_whole, treatment_whole, treatment_analysis):
    """The control interval matching the treatment's backup position.

    Degradation must not compare a window containing a backup against a whole
    control run: the aggregate then varies with how much of the window the
    backup happened to occupy. The control is measured at the same offset from
    its own ramp-up, for the same duration.
    """
    offset = (treatment_analysis.start - treatment_whole.start).total_seconds()
    duration = treatment_analysis.duration_seconds
    start = control_whole.start + timedelta(seconds=offset)
    return Window(start, start + timedelta(seconds=duration))


def throughput_loss(control_stats, treatment_stats):
    """Fractional throughput loss, or None with a reason.

    Follows benchmarks/README.md:
        throughput_loss = 1 - tpm_during_backup / tpm_without_backup
    """
    for label, stats in (('control', control_stats), ('backup', treatment_stats)):
        if stats['unavailable'] is not None:
            return None, f'{label}: {stats["unavailable"]}'
    control = control_stats['mean_tpm']
    if not control:
        return None, 'control mean throughput is zero'
    return round(1 - treatment_stats['mean_tpm'] / control, 4), None


def latency_change(control_latency, treatment_latency):
    """Fractional p95 change, or None with a reason.

    Whole-run only. HammerDB records no time dimension for latency, so this
    cannot be matched to the backup window the way throughput is, and it
    includes ramp-up for both runs.
    """
    control = control_latency.get('p95_ms')
    treatment = treatment_latency.get('p95_ms')
    if control is None or treatment is None:
        return None, 'p95 unavailable for at least one trial'
    if not control:
        return None, 'control p95 is zero'
    return round(treatment / control - 1, 4), None


def build_pair_record(*, pair_id, repetition, order, control, treatment,
                      backup_result, capture_started_at, verified_at,
                      backup_output_bytes=None):
    """Assemble one matched-pair result.

    `control` and `treatment` each carry `series`, `latency`, `aggregate` and
    a `whole` Window. A pair whose backup does not fit inside the treatment's
    measurement window is rejected rather than reported, mirroring how the
    native suite rejects a trial whose backup outlives its workload.
    """
    analysis = analysis_window(capture_started_at, verified_at)
    treatment_whole = treatment['whole']
    control_whole = control['whole']
    rejection = None
    if not backup_result or backup_result.get('status') not in SUCCESS_STATUSES:
        rejection = 'backup did not pass verification: ' + str((backup_result or {}).get('status'))
    elif backup_result.get('integrity_verified') is False:
        rejection = 'backup integrity verification failed'
    if rejection is None and not treatment_whole.contains(analysis):
        rejection = (
            'backup did not run entirely inside the measurement window; '
            f'backup {analysis.as_dict()} vs window {treatment_whole.as_dict()}')

    matched = matched_window(control_whole, treatment_whole, analysis)
    if rejection is None and not control_whole.contains(matched):
        rejection = (
            'matched control window falls outside the control measurement '
            'window; the control run is shorter than the treatment')

    treatment_analysis = window_stats(treatment['series'], analysis)
    control_matched = window_stats(control['series'], matched)
    recovery = recovery_window(analysis, treatment_whole)

    loss, loss_reason = (None, rejection) if rejection else throughput_loss(
        control_matched, treatment_analysis)
    increase, increase_reason = (None, rejection) if rejection else latency_change(
        control['latency'], treatment['latency'])

    # A transfer method's own byte counts, when the shared record has them.
    network_bytes = (backup_result or {}).get('network_bytes')
    unavailable = ({'network_bytes': NO_TRANSPORT_OBSERVER} if network_bytes is None
                   else {'interface_bytes': SELF_REPORTED_BYTES})
    if loss_reason:
        unavailable['throughput_loss'] = loss_reason
    if increase_reason:
        unavailable['latency_increase'] = increase_reason

    return {
        'schema_version': 2,
        'pair_id': pair_id,
        'repetition': repetition,
        'trial_order': list(order),
        'accepted': rejection is None,
        'rejection_reason': rejection,
        'control': {
            'whole_window': window_stats(control['series'], control_whole),
            'matched_window': control_matched,
            'aggregate': control['aggregate'],
            'latency': control['latency'],
        },
        'backup': {
            'whole_window': window_stats(treatment['series'], treatment_whole),
            'analysis_window': treatment_analysis,
            'recovery_window': (
                window_stats(treatment['series'], recovery) if recovery else None),
            'aggregate': treatment['aggregate'],
            'latency': treatment['latency'],
            # Measured inside the container; the shared record cannot see a
            # remotely executed capture and reports null for it.
            'output_bytes_observed': backup_output_bytes,
        },
        'throughput_loss_fraction': loss,
        'latency': {
            # Named so no reader mistakes this for a window-aligned figure.
            'scope': 'whole_run',
            'increase_fraction': increase,
            'note': ('HammerDB records no time dimension for latency, so this '
                     'covers each whole run including ramp-up and is not '
                     'matched to the backup window'),
        },
        'backup_result': backup_result,
        'network_bytes': network_bytes,
        'unavailable_metrics': unavailable,
    }


# --- orchestration -----------------------------------------------------------

EXIT_OK = 0
EXIT_INVALID_INPUT = 2
EXIT_TRIAL_FAILED = 3

CONTROL = 'control'
BACKUP = 'backup'


class TrialError(RuntimeError):
    """A trial could not be completed, so it must not become a measurement."""


def _docker(args, *, docker_bin='docker', timeout=120, check=True, text=True):
    """Run one docker command and return its completed process.

    text=False is required when the output must stay byte-faithful: capturing
    as text applies universal-newline translation, which rewrites HammerDB's
    carriage returns before the bytes ever reach a file.
    """
    done = subprocess.run([docker_bin, *[str(a) for a in args]],
                          capture_output=True, text=text, timeout=timeout,
                          check=False)
    if check and done.returncode != 0:
        detail = done.stderr or done.stdout or b''
        if isinstance(detail, bytes):
            detail = detail.decode('utf-8', 'replace')
        raise TrialError(
            f'docker {" ".join(str(a) for a in args[:2])} failed: '
            f'{detail.strip()[:400]}')
    return done


def start_workload(*, name, image, network, host, password, settings,
                   script='/hdb/run_timed.tcl', scripts_dir, docker_bin='docker'):
    """Start a detached TPROC-C run.

    Deliberately not --rm: the jobs database holding the counter series and
    the latency percentiles has to be copied out after the process exits.
    """
    environment = {
        'PGBENCH_HOST': host,
        'PGBENCH_PASS': password,
        'TPCC_VU': settings['vu'],
        'TPCC_RAMPUP': settings['rampup'],
        'TPCC_DURATION': settings['duration'],
    }
    arguments = ['run', '-d', '--name', name, '--network', network]
    for key, value in environment.items():
        arguments += ['-e', f'{key}={value}']
    arguments += ['-v', f'{scripts_dir}:/hdb:ro', image,
                  './hammerdbcli', 'auto', script]
    _docker(arguments, docker_bin=docker_bin)
    return name


def wait_for_marker(name, marker, *, timeout, poll=2.0, docker_bin='docker',
                    sleep=time.sleep):
    """Block until a marker appears in the container log.

    A timeout raises rather than returning, so a mistimed run fails the trial
    instead of silently producing a window that never contained the workload.
    """
    deadline = time.monotonic() + timeout
    while True:
        done = _docker(['logs', name], docker_bin=docker_bin, check=False)
        if marker in (done.stdout or '') or marker in (done.stderr or ''):
            return True
        if time.monotonic() >= deadline:
            raise TrialError(
                f'marker {marker!r} did not appear in {name} within {timeout}s')
        sleep(poll)


def wait_for_exit(name, *, timeout, poll=2.0, docker_bin='docker',
                  sleep=time.sleep):
    """Block until the container stops."""
    deadline = time.monotonic() + timeout
    while True:
        done = _docker(['inspect', '-f', '{{.State.Running}}', name],
                       docker_bin=docker_bin, check=False)
        if (done.stdout or '').strip() == 'false':
            return True
        if time.monotonic() >= deadline:
            raise TrialError(f'{name} did not finish within {timeout}s')
        sleep(poll)


def collect_workload(name, directory, *, docker_bin='docker'):
    """Save the log and jobs database, then remove the container."""
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / 'hammerdb.log'
    # Captured as bytes and written as bytes: text mode would translate
    # HammerDB's carriage returns away before the evidence is stored.
    done = _docker(['logs', '-t', name], docker_bin=docker_bin, check=False,
                   text=False)
    log_path.write_bytes((done.stdout or b'') + (done.stderr or b''))
    database = directory / 'hammer.DB'
    _docker(['cp', f'{name}:/tmp/hammer.DB', str(database)],
            docker_bin=docker_bin)
    _docker(['rm', '-f', name], docker_bin=docker_bin, check=False)
    return log_path, database


def container_bytes(container, path, *, docker_bin='docker'):
    """Apparent size of a captured backup, measured inside the container.

    run_backup calls directory_bytes on the host. When the capture is executed
    remotely through docker exec that path does not exist locally, so the
    shared record honestly reports null. This fills the gap without modifying
    the shared adapter. Apparent file length, not allocated disk space.
    """
    done = _docker(['exec', container, 'du', '-sb', str(path)],
                   docker_bin=docker_bin, check=False)
    first = (done.stdout or '').split()
    if done.returncode != 0 or not first or not first[0].isdigit():
        return None
    return int(first[0])


def make_executor(container, directory, timeout, *, docker_bin='docker'):
    """An `execute` for run_backup that runs phases inside a container.

    Also records wall-clock bounds per phase. run_backup measures durations
    with perf_counter, which cannot be aligned against workload timestamps.
    """
    directory.mkdir(parents=True, exist_ok=True)
    phases = {}

    def execute(phase, command):
        started = datetime.now(timezone.utc)
        result = run_command(
            [docker_bin, 'exec', container, *[str(part) for part in command]],
            directory / f'{phase}.log', timeout)
        phases[phase] = {'started_at': started,
                         'ended_at': datetime.now(timezone.utc)}
        return result

    return execute, phases


def container_reader(container, *, docker_bin='docker'):
    """A `read_file` for run_backup that reads a path inside the container.

    Every failure is an OSError, which is what read_file's callers catch.
    """

    def read_file(path):
        try:
            done = subprocess.run([docker_bin, 'exec', container, 'cat', str(path)],
                                  capture_output=True, text=True, timeout=120, check=False)
        except subprocess.TimeoutExpired as exc:
            raise OSError(f'reading {path} in {container} timed out after '
                          f'{exc.timeout} seconds') from exc
        if done.returncode != 0:
            raise OSError(f'cannot read {path} in {container}: '
                          f'{(done.stderr or "").strip()[:400]}')
        return done.stdout

    return read_file


def start_observer(project, output, duration, *, python=None):
    """Sample container resources for the trial, using Etash's observer."""
    command = [python or sys.executable, '-m', 'benchmarks.observe_docker',
               '--project', project, '--output', str(output),
               '--duration', str(duration)]
    try:
        return subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError:
        # Resource sampling is diagnostic; losing it must not fail a trial.
        return None


def read_trial(log_path, database):
    """Turn one collected trial into the inputs the window maths needs."""
    log_text = read_log(log_path)
    markers = parse_log_markers(log_text)
    if markers['rampup_complete'] is None or markers['test_complete'] is None:
        raise TrialError(
            f'{log_path.name} has no complete measurement window: {markers}')
    return {
        'series': read_tpm_series(database),
        'latency': read_latency(database),
        'aggregate': parse_test_result(log_text),
        'whole': Window(markers['rampup_complete'], markers['test_complete']),
        'jobid': read_jobid(database),
    }


def run_trial(treatment, *, options, directory, sleep=time.sleep):
    """Run one TPROC-C trial, optionally with a backup inside its window."""
    directory.mkdir(parents=True, exist_ok=True)
    name = f'hdb-{treatment}-{uuid.uuid4().hex[:8]}'
    window_seconds = (options.rampup + options.duration) * 60
    observer = start_observer(
        options.compose_project, directory / 'resources-docker.jsonl',
        window_seconds + options.observer_margin)
    backup_result = capture_started_at = verified_at = None
    backup_output_bytes = prepared = basis_check = None
    if treatment == BACKUP:
        spec = methods.get(options.method)
        # A fresh path per trial: pg_basebackup refuses a non-empty
        # destination, so a shared path would fail every repetition
        # after the first.
        destination = f"{str(options.backup_output).rstrip('/')}/{name}"
        phase_logs = directory / 'backup-phases'
        execute, phases = make_executor(
            options.runner_container, phase_logs, options.timeout,
            docker_bin=options.docker)
        adapter = dict(
            run_id=f'{name}-backup', output=destination, tools=options.tools,
            execute=execute, host=options.pg_host, port=options.pg_port,
            user=options.pg_user, checksum=options.checksum,
            checkpoint=options.checkpoint,
            basis=getattr(options, 'basis', None) if spec.needs_basis else None,
            capture_path=f'{destination}-{spec.capture_dir}' if spec.capture_dir else None,
            read_log=lambda phase: (phase_logs / f'{phase}.log').read_text(encoding='utf-8'),
            read_file=container_reader(options.runner_container, docker_bin=options.docker))
        if spec.source_local:
            adapter.update(source_pgdata=options.source_pgdata,
                           method_options={'jobs': options.pgdelta_jobs})
    try:
        if treatment == BACKUP and spec.trusts_basis:
            # pg_verifybackup reads all of B: like preparation, it runs before
            # the workload starts, and run_backup reuses the record.
            basis_check = verify_basis(adapter['basis'], execute=execute, tools=options.tools,
                                       read_file=adapter['read_file'])
            if not successful(basis_check):
                reason = basis_check['error'] or f'exit code {basis_check["exit_code"]}'
                raise TrialError(f'basis verification failed: {reason}; see {phase_logs}')
        if treatment == BACKUP and spec.prepare:
            # Preparation (e.g. copying B for rsync) runs before the workload
            # starts, so it overlaps neither measurement window.
            prepared = prepare_backup(options.method, **adapter)
            if not all(successful(phase) for phase in prepared.values()):
                raise TrialError(f'{options.method} preparation failed; see {phase_logs}')
        start_workload(
            name=name, image=options.hammerdb_image, network=options.network,
            host=options.pg_host, password=options.pg_password,
            settings={'vu': options.vu, 'rampup': options.rampup,
                      'duration': options.duration},
            scripts_dir=options.scripts_dir, docker_bin=options.docker)
        wait_for_marker(name, RAMPUP_COMPLETE,
                        timeout=options.rampup * 60 + options.marker_margin,
                        docker_bin=options.docker, sleep=sleep)
        if treatment == BACKUP:
            if options.backup_offset:
                sleep(options.backup_offset)

            def end_of_workload():
                # Called once verification ends. Anything after it (a staged
                # method's output check reads all of O and S, then the du
                # probe) waits for the workload to exit, so it cannot load
                # the recovery window.
                wait_for_exit(name, timeout=window_seconds + options.marker_margin,
                              docker_bin=options.docker, sleep=sleep)

            # run_backup returns (record, exit_code); the record alone is the
            # shared v3 artifact that the pair record embeds.
            backup_result, backup_exit = run_backup(
                options.method, **adapter, prepared=prepared,
                basis_verification=basis_check, after_verification=end_of_workload)
            backup_output_bytes = container_bytes(
                options.runner_container, destination, docker_bin=options.docker)
            atomic_json(directory / 'backup-result.json',
                        {**backup_result, 'exit_code': backup_exit,
                         'backup_output_bytes_observed': backup_output_bytes})
            if 'backup' not in phases:
                raise TrialError('backup phase never executed')
            capture_started_at = phases['backup']['started_at']
            # Fall back to the last phase that ran when verification was
            # skipped, so a failed backup still bounds a window.
            last = phases.get('verification') or phases[max(
                phases, key=lambda key: phases[key]['ended_at'])]
            verified_at = last['ended_at']
        wait_for_exit(name, timeout=window_seconds + options.marker_margin,
                      docker_bin=options.docker, sleep=sleep)
        log_path, database = collect_workload(
            name, directory, docker_bin=options.docker)
    finally:
        if observer is not None:
            observer.terminate()
            try:
                # Reap it; an unwaited child leaks a process and a warning.
                observer.wait(timeout=10)
            except subprocess.TimeoutExpired:
                observer.kill()
                observer.wait(timeout=10)
        _docker(['rm', '-f', name], docker_bin=options.docker, check=False)
    trial = read_trial(log_path, database)
    trial.update(treatment=treatment, backup_result=backup_result,
                 capture_started_at=capture_started_at, verified_at=verified_at,
                 backup_output_bytes=backup_output_bytes)
    return trial


def run_pair(repetition, *, options, directory, order, sleep=time.sleep):
    """Run a control and a backup trial, then compare aligned windows."""
    trials = {}
    for treatment in order:
        trials[treatment] = run_trial(
            treatment, options=options,
            directory=directory / f'{repetition}-{treatment}', sleep=sleep)
    treatment = trials[BACKUP]
    return build_pair_record(
        pair_id=f'{directory.name}-{repetition}', repetition=repetition,
        order=order, control=trials[CONTROL], treatment=treatment,
        backup_result=treatment['backup_result'],
        capture_started_at=treatment['capture_started_at'],
        verified_at=treatment['verified_at'],
        backup_output_bytes=treatment.get('backup_output_bytes'))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Measure TPROC-C degradation during a PostgreSQL backup.',
        epilog='Produces a measurement instrument, not a citable baseline. '
               'Use a disposable fixture only.')
    parser.add_argument('--output', type=Path, required=True,
                        help='new directory for results; must not exist')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--seed', type=int, default=None,
                        help='trial-order seed; recorded so order is reproducible')
    parser.add_argument('--vu', type=int, default=8)
    parser.add_argument('--rampup', type=int, default=1, help='minutes')
    parser.add_argument('--duration', type=int, default=3, help='minutes')
    parser.add_argument('--warehouses', type=int, default=10,
                        help='recorded for provenance; the schema is prebuilt')
    parser.add_argument('--backup-offset', type=float, default=0,
                        help='seconds after ramp-up before the backup starts')
    parser.add_argument('--method', default='pg_basebackup_full',
                        choices=methods.names())
    parser.add_argument('--basis', default=None,
                        help='old copy inside the runner container, for methods '
                             'that need one')
    parser.add_argument('--source-pgdata', help='Source PGDATA at its original absolute path inside the runner')
    parser.add_argument('--pgdelta-jobs', type=int, help='Explicit live pgdelta worker count')
    parser.add_argument('--rsync', default='rsync',
                        help='rsync inside the runner container')
    parser.add_argument('--python', default='python3',
                        help='python inside the runner container')
    parser.add_argument('--pgdelta', type=shlex.split, default=None,
                        help='pgdelta command prefix; default: PYTHON -m pgdelta')
    parser.add_argument('--pg-controldata', default=None,
                        help='passed to pgdelta as --pg-controldata when given')
    parser.add_argument('--psql', default='psql')
    parser.add_argument('--pg-waldump', default='pg_waldump')
    parser.add_argument('--checksum', default='SHA256')
    parser.add_argument('--checkpoint', default='fast', choices=('fast', 'spread'))
    parser.add_argument('--pg-host', default='postgres')
    parser.add_argument('--pg-port', type=int, default=5432)
    parser.add_argument('--pg-user', default='postgres')
    parser.add_argument('--pg-password', default=None,
                        help='defaults to POSTGRES_PASSWORD in the environment')
    parser.add_argument('--network', default='fixture_default')
    parser.add_argument('--compose-project', default='fixture')
    parser.add_argument('--hammerdb-image', default='tpcorg/hammerdb:latest')
    parser.add_argument('--runner-container', default='pg-delta-runner',
                        help='running container holding the PostgreSQL 17 tools')
    parser.add_argument('--backup-output', default='/results/hammerdb-backup')
    parser.add_argument('--scripts-dir', default=None,
                        help='host path mounted at /hdb')
    parser.add_argument('--timeout', type=float, default=3600)
    parser.add_argument('--marker-margin', type=float, default=180,
                        help='extra seconds to wait for a log marker')
    parser.add_argument('--observer-margin', type=float, default=60)
    parser.add_argument('--docker', default='docker')
    return parser.parse_args(argv)


def main(argv=None):
    import os
    options = parse_args(argv)
    options.pg_password = options.pg_password or os.environ.get('POSTGRES_PASSWORD')
    options.tools = {
        'pg_basebackup': 'pg_basebackup',
        'pg_verifybackup': 'pg_verifybackup',
        'pg_combinebackup': 'pg_combinebackup',
        'rsync': options.rsync,
        'python': options.python,
        'pgdelta': options.pgdelta,
        'pg_controldata': options.pg_controldata,
        'psql': options.psql,
        'pg_waldump': options.pg_waldump,
    }
    if options.scripts_dir is None:
        options.scripts_dir = str(
            (Path(__file__).resolve().parent / 'workloads' / 'hammerdb'))
    try:
        if not options.pg_password:
            raise ValueError(
                'POSTGRES_PASSWORD is not set; refusing to run with an empty '
                'password')
        if options.output.exists():
            raise ValueError(
                f'{options.output} already exists; use a new directory per run')
        if options.repeats < 1:
            raise ValueError('--repeats must be at least 1')
        spec = methods.get(options.method)
        if spec.source_local and (not options.source_pgdata or not options.pg_controldata):
            raise ValueError('Live pgdelta requires --source-pgdata and --pg-controldata inside the runner')
        if options.pgdelta_jobs is not None and options.pgdelta_jobs < 1:
            raise ValueError('--pgdelta-jobs must be positive')
        if spec.needs_basis and not options.basis:
            raise ValueError(
                f'{options.method} refreshes an old copy; pass --basis with its '
                'path inside the runner container')
        if options.basis and not spec.needs_basis:
            raise ValueError(f'--basis is not used by {options.method}')
    except ValueError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return EXIT_INVALID_INPUT

    options.output.mkdir(parents=True)
    seed = options.seed if options.seed is not None else random.randrange(2 ** 32)
    chooser = random.Random(seed)
    atomic_json(options.output / 'provenance.json', {
        'source_provenance': source_provenance(),
        'started_at': datetime.now(timezone.utc).isoformat(),
        'seed': seed,
        'settings': {
            key: getattr(options, key)
            for key in ('repeats', 'vu', 'rampup', 'duration', 'warehouses',
                        'backup_offset', 'method', 'basis', 'checksum',
                        'checkpoint', 'network', 'compose_project',
                        'hammerdb_image', 'source_pgdata', 'pgdelta_jobs')
        },
        'note': 'single-host smoke measurement; not a baseline',
    })

    pairs, failures = [], []
    for repetition in range(1, options.repeats + 1):
        # Randomised so a systematic warm-up effect cannot favour one treatment.
        order = [CONTROL, BACKUP]
        chooser.shuffle(order)
        try:
            pairs.append(run_pair(repetition, options=options,
                                  directory=options.output, order=tuple(order)))
        except (TrialError, OSError, sqlite3.Error) as exc:
            failures.append({'repetition': repetition, 'error': str(exc)})

    accepted = [p for p in pairs if p['accepted']]
    losses = [p['throughput_loss_fraction'] for p in accepted
              if p['throughput_loss_fraction'] is not None]
    results = {
        'schema_version': 2,
        'seed': seed,
        'repeats_requested': options.repeats,
        'pairs': pairs,
        'failures': failures,
        'accepted_pairs': len(accepted),
        # A median of a handful of runs is descriptive only; the spread is
        # reported alongside it and no significance is claimed.
        'median_throughput_loss': (
            sorted(losses)[len(losses) // 2] if losses else None),
        'throughput_loss_range': (
            [min(losses), max(losses)] if losses else None),
        'network_bytes': None,
        'unavailable_metrics': {
            'network_bytes': 'transport observer not implemented',
            'window_aligned_latency':
                'HammerDB records no time dimension for latency',
        },
    }
    atomic_json(options.output / 'results.json', results)
    print(json.dumps({key: results[key] for key in (
        'seed', 'accepted_pairs', 'median_throughput_loss',
        'throughput_loss_range')}, indent=2))
    failed_backups = any((pair.get('backup_result') or {}).get('status') not in SUCCESS_STATUSES
                         for pair in pairs)
    if failures or failed_backups or not accepted:
        return EXIT_TRIAL_FAILED
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
