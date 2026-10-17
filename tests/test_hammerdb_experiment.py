"""Tests for the HammerDB TPROC-C experiment driver.

Fixtures reproduce the real shapes observed on October 17, 2026: a jobs
database written by HammerDB CLI v6.0 and the container log it emitted.
No HammerDB, PostgreSQL or Docker is required.
"""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.hammerdb_experiment import (
    Sample,
    parse_log_markers,
    parse_test_result,
    read_jobid,
    read_latency,
    read_tpm_series,
)

JOBID = '6AD3E641D42E03E293130373'

# Observed cadence is nominally ten seconds, but the real run ended with a
# four-second interval. The fixture keeps that irregularity deliberately.
COUNTER_ROWS = [
    (0, '2026-10-17 21:18:57'),
    (7824, '2026-10-17 21:19:07'),
    (14724, '2026-10-17 21:19:17'),
    (18588, '2026-10-17 21:19:27'),
    (23094, '2026-10-17 21:19:37'),
    (12354, '2026-10-17 21:19:47'),
    (28950, '2026-10-17 21:19:57'),
    (13032, '2026-10-17 21:21:03'),
    (15414, '2026-10-17 21:21:07'),
]

TIMING_ROWS = [
    # procname, calls, avg, p50, p95, p99, elapsed, summary, vu
    ('NEWORD', 13968, 19.066, 8.772, 42.502, 90.291, 129468.5, 1, 4),
    ('PAYMENT', 14008, 14.934, 5.623, 34.039, 90.718, 129468.5, 1, 4),
    ('DELIVERY', 1452, 19.725, 16.922, 45.371, 69.849, 129468.5, 1, 4),
    ('OSTAT', 1392, 3.655, 2.251, 11.801, 20.976, 129468.5, 1, 4),
    ('SLEV', 1436, 3.010, 2.077, 4.590, 9.843, 129468.5, 1, 4),
    # Per-virtual-user rows must never be mistaken for the summary.
    ('NEWORD', 3525, 20.327, 8.630, 44.471, 102.862, 130795.0, 0, 2),
    ('PAYMENT', 3587, 13.567, 5.618, 33.784, 80.462, 130795.0, 0, 2),
]

# Reproduces the real October 17 log exactly, including the carriage return
# HammerDB emits between the docker timestamp and the message. A fixture
# using plain newlines hides a parser bug, so the \r must stay.
LOG_TEXT = (
    '2026-10-17T21:17:56.1Z HammerDB CLI v6.0\n'
    '2026-10-17T21:18:57.420000000Z 0 PostgreSQL tpm\n'
    '2026-10-17T21:19:07.511111111Z 7824 PostgreSQL tpm\n'
    '2026-10-17T21:19:57.123456789Z \rVuser 1:Rampup complete,'
    ' Taking start Transaction Count.\n'
    '2026-10-17T21:20:07.000000000Z 29610 PostgreSQL tpm\n'
    '2026-10-17T21:21:03.900000000Z \rVuser 1:Test complete,'
    ' Taking end Transaction Count.\n'
    '2026-10-17T21:21:04.000000000Z \rVuser 1:4 Active Virtual Users configured\n'
    '2026-10-17T21:21:04.100000000Z \rVuser 1:TEST RESULT : System achieved'
    ' 4954 NOPM from 11527 PostgreSQL TPM\n'
)


def make_jobs_db(path, counter_rows=COUNTER_ROWS, timing_rows=TIMING_ROWS,
                 jobid=JOBID, jobs=1):
    connection = sqlite3.connect(path)
    with connection:
        connection.execute(
            'CREATE TABLE JOBMAIN (jobid TEXT, db TEXT, bm TEXT, '
            'jobdict TEXT, timestamp TEXT, profile_id TEXT)')
        connection.execute(
            'CREATE TABLE JOBTCOUNT (jobid TEXT, counter INT, metric TEXT, '
            'timestamp TEXT)')
        connection.execute(
            'CREATE TABLE JOBTIMING (jobid TEXT, vu INT, procname TEXT, '
            'calls INT, min_ms REAL, avg_ms REAL, max_ms REAL, total_ms REAL, '
            'p99_ms REAL, p95_ms REAL, p75_ms REAL, p50_ms REAL, p25_ms REAL, '
            'sd REAL, ratio_pct REAL, summary INT, elapsed_ms REAL)')
        for index in range(jobs):
            connection.execute(
                'INSERT INTO JOBMAIN (jobid, db, bm, timestamp) VALUES (?,?,?,?)',
                (f'{jobid}{index or ""}', 'PostgreSQL', 'TPC-C',
                 '2026-10-17 21:18:57'))
        for counter, stamp in counter_rows:
            connection.execute(
                'INSERT INTO JOBTCOUNT (jobid, counter, metric, timestamp) '
                'VALUES (?,?,?,?)', (jobid, counter, 'tpm', stamp))
        for name, calls, avg, p50, p95, p99, elapsed, summary, vu in timing_rows:
            connection.execute(
                'INSERT INTO JOBTIMING (jobid, vu, procname, calls, avg_ms, '
                'p50_ms, p95_ms, p99_ms, elapsed_ms, summary) '
                'VALUES (?,?,?,?,?,?,?,?,?,?)',
                (jobid, vu, name, calls, avg, p50, p95, p99, elapsed, summary))
    connection.close()
    return path


class JobsDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = make_jobs_db(Path(self.temp.name) / 'hammer.DB')

    def test_series_is_ordered_and_typed(self):
        series = read_tpm_series(self.database)
        self.assertEqual(len(series), len(COUNTER_ROWS))
        self.assertTrue(all(isinstance(sample, Sample) for sample in series))
        self.assertEqual(series, sorted(series, key=lambda s: s.at))
        self.assertEqual(series[1].tpm, 7824)

    def test_timestamps_are_timezone_aware_utc(self):
        first = read_tpm_series(self.database)[0]
        self.assertEqual(first.at.tzinfo, timezone.utc)
        self.assertEqual(
            first.at, datetime(2026, 10, 17, 21, 18, 57, tzinfo=timezone.utc))

    def test_irregular_cadence_is_preserved(self):
        series = read_tpm_series(self.database)
        gaps = {round((b.at - a.at).total_seconds())
                for a, b in zip(series, series[1:])}
        # A fixed-cadence assumption would be wrong; the real run proves it.
        self.assertIn(10, gaps)
        self.assertIn(4, gaps)

    def test_leading_zero_sample_is_retained(self):
        self.assertEqual(read_tpm_series(self.database)[0].tpm, 0)

    def test_non_tpm_metrics_are_excluded(self):
        connection = sqlite3.connect(self.database)
        with connection:
            connection.execute(
                'INSERT INTO JOBTCOUNT (jobid, counter, metric, timestamp) '
                'VALUES (?,?,?,?)', (JOBID, 999, 'nopm', '2026-10-17 21:19:08'))
        connection.close()
        self.assertNotIn(999, [s.tpm for s in read_tpm_series(self.database)])

    def test_latency_returns_summary_rows_only(self):
        latency = read_latency(self.database)
        self.assertEqual(set(latency['procedures']),
                         {'NEWORD', 'PAYMENT', 'DELIVERY', 'OSTAT', 'SLEV'})
        # The per-virtual-user NEWORD row has p95 44.471 and must not win.
        self.assertAlmostEqual(latency['procedures']['NEWORD']['p95_ms'], 42.502)
        self.assertAlmostEqual(latency['p95_ms'], 42.502)

    def test_latency_is_labelled_whole_run(self):
        # Guards the documented limitation: these are not window-aligned.
        self.assertEqual(read_latency(self.database)['scope'], 'whole_run')

    def test_absent_latency_is_explicit_not_zero(self):
        empty = make_jobs_db(Path(self.temp.name) / 'empty.DB', timing_rows=[])
        latency = read_latency(empty)
        self.assertIsNone(latency.get('p95_ms'))
        self.assertIn('unavailable', latency)

    def test_jobid_reads_single_job(self):
        self.assertEqual(read_jobid(self.database), JOBID)

    def test_ambiguous_jobid_is_none(self):
        two = make_jobs_db(Path(self.temp.name) / 'two.DB', jobs=2)
        self.assertIsNone(read_jobid(two))

    def test_series_can_filter_by_jobid(self):
        self.assertEqual(read_tpm_series(self.database, jobid='absent'), [])
        self.assertTrue(read_tpm_series(self.database, jobid=JOBID))

    def test_reading_does_not_modify_the_database(self):
        before = Path(self.database).read_bytes()
        read_tpm_series(self.database)
        read_latency(self.database)
        self.assertEqual(Path(self.database).read_bytes(), before)


class LogParsingTests(unittest.TestCase):
    def test_test_result_is_parsed(self):
        self.assertEqual(parse_test_result(LOG_TEXT),
                         {'nopm': 4954, 'tpm': 11527})

    def test_missing_test_result_is_none(self):
        self.assertIsNone(parse_test_result('HammerDB CLI v6.0\nno result\n'))

    def test_markers_are_parsed_as_utc(self):
        markers = parse_log_markers(LOG_TEXT)
        self.assertEqual(
            markers['rampup_complete'],
            datetime(2026, 10, 17, 21, 19, 57, 123456, tzinfo=timezone.utc))
        self.assertEqual(
            markers['test_complete'],
            datetime(2026, 10, 17, 21, 21, 3, 900000, tzinfo=timezone.utc))

    def test_truncated_run_reports_missing_completion(self):
        truncated = LOG_TEXT.split('Test complete')[0]
        markers = parse_log_markers(truncated)
        self.assertIsNotNone(markers['rampup_complete'])
        self.assertIsNone(markers['test_complete'])

    def test_empty_log_yields_no_markers(self):
        self.assertEqual(parse_log_markers(''),
                         {'rampup_complete': None, 'test_complete': None})

    def test_untimestamped_lines_are_ignored(self):
        self.assertIsNone(
            parse_log_markers('Rampup complete\n')['rampup_complete'])

    def test_first_marker_wins(self):
        doubled = LOG_TEXT + (
            '2026-10-17T21:30:00.0Z Vuser 1:Rampup complete, again\n')
        self.assertEqual(parse_log_markers(doubled)['rampup_complete'].minute, 19)

    def test_newline_translated_log_parses_identically(self):
        """A reader that converted lone carriage returns must still work.

        Python's universal-newline handling rewrites HammerDB's \\r to \\n,
        which separates each marker from its timestamp. Both forms must give
        the same answer, or results depend on how the caller opened the file.
        """
        translated = LOG_TEXT.replace('\r', '\n')
        self.assertNotEqual(translated, LOG_TEXT)
        self.assertEqual(parse_log_markers(translated),
                         parse_log_markers(LOG_TEXT))
        self.assertIsNotNone(parse_log_markers(translated)['rampup_complete'])

    def test_read_log_preserves_carriage_returns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'c.log'
            path.write_bytes(LOG_TEXT.encode('utf-8'))
            from benchmarks.hammerdb_experiment import read_log
            self.assertIn('\r', read_log(path))


if __name__ == '__main__':
    unittest.main()
