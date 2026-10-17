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
from datetime import datetime, timezone

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
    connection = sqlite3.connect(f'file:{database}?mode=ro', uri=True)
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
    connection = sqlite3.connect(f'file:{database}?mode=ro', uri=True)
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
    connection = sqlite3.connect(f'file:{database}?mode=ro', uri=True)
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


def _leading_timestamp(line):
    """Parse the RFC3339 prefix that ``docker logs -t`` prepends."""
    head = line.split(' ', 1)[0]
    if not head:
        return None
    text = head.replace('Z', '+00:00')
    # Python before 3.11 rejects more than six fractional digits.
    if '.' in text:
        base, _, rest = text.partition('.')
        digits = ''.join(part for part in rest if part.isdigit())[:6]
        offset = rest[len(''.join(part for part in rest if part.isdigit())):]
        text = f'{base}.{digits}{offset}'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
