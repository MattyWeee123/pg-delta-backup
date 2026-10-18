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

This module does not test restore and does not certify TPROC-C recovery. See
benchmarks/CONSOLIDATION.md.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
                      backup_result, capture_started_at, verified_at):
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
    if not treatment_whole.contains(analysis):
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

    unavailable = {'network_bytes': 'transport observer not implemented'}
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
        'network_bytes': None,
        'unavailable_metrics': unavailable,
    }


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
