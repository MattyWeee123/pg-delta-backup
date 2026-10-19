"""Tests for the HammerDB TPROC-C experiment driver.

Fixtures reproduce the real shapes observed on October 17, 2026: a jobs
database written by HammerDB CLI v6.0 and the container log it emitted.
No HammerDB, PostgreSQL or Docker is required.
"""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from benchmarks.hammerdb_experiment import (
    Sample,
    TrialError,
    Window,
    build_pair_record,
    collect_workload,
    main,
    make_executor,
    read_trial,
    run_trial,
    start_workload,
    wait_for_marker,
    latency_change,
    matched_window,
    parse_log_markers,
    parse_test_result,
    read_jobid,
    read_latency,
    read_tpm_series,
    recovery_window,
    samples_in,
    throughput_loss,
    window_stats,
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

    def test_path_containing_a_uri_delimiter_is_handled(self):
        """A '#' in an evidence path must not redirect SQLite elsewhere.

        Interpolating a path into a file: URI leaves '#' acting as a fragment
        delimiter, so SQLite opens a different database and the tables appear
        to be missing.
        """
        awkward = Path(self.temp.name) / 'run#1 and 2'
        awkward.mkdir()
        database = make_jobs_db(awkward / 'hammer.DB')
        self.assertEqual(len(read_tpm_series(database)), len(COUNTER_ROWS))
        self.assertEqual(read_jobid(database), JOBID)

    def test_reader_refuses_to_create_a_missing_database(self):
        missing = Path(self.temp.name) / 'absent.DB'
        with self.assertRaises(sqlite3.OperationalError):
            read_tpm_series(missing)
        self.assertFalse(missing.exists())


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

    def test_nanosecond_precision_is_truncated_not_mangled(self):
        from benchmarks.hammerdb_experiment import _leading_timestamp
        parsed = _leading_timestamp('2026-10-17T21:19:57.756031072Z')
        self.assertEqual(parsed.microsecond, 756031)
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_non_utc_offset_is_preserved_not_silently_relabelled(self):
        """Collecting 'all digits' from the fraction also eats the offset.

        That rebuilt the string without a zone, so +05:30 was silently read
        as UTC: a five and a half hour error in any derived window.
        """
        from benchmarks.hammerdb_experiment import _leading_timestamp
        shifted = _leading_timestamp('2026-10-17T21:19:57.756031072+05:30')
        utc = _leading_timestamp('2026-10-17T21:19:57.756031072Z')
        self.assertNotEqual(shifted, utc)
        self.assertEqual((utc - shifted).total_seconds(), 5.5 * 3600)

    def test_short_and_absent_fractions_parse(self):
        from benchmarks.hammerdb_experiment import _leading_timestamp
        self.assertEqual(
            _leading_timestamp('2026-10-17T21:19:57.5Z').microsecond, 500000)
        self.assertEqual(
            _leading_timestamp('2026-10-17T21:19:57Z').microsecond, 0)

    def test_non_timestamp_text_is_rejected(self):
        from benchmarks.hammerdb_experiment import _leading_timestamp
        for text in ['', 'Vuser', '2026-10-17', 'not-a-time', '12345']:
            self.assertIsNone(_leading_timestamp(text), text)

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


BASE = datetime(2026, 10, 17, 21, 0, 0, tzinfo=timezone.utc)


def at(second, minute=0):
    """A test instant, offset from BASE so seconds may exceed 59."""
    return BASE + timedelta(minutes=minute, seconds=second)


def steady(start_minute, start_second, count, tpm, step=10):
    """A uniform series, for building control runs in tests."""
    base = at(start_second, start_minute)
    return [Sample(base + timedelta(seconds=step * i), tpm) for i in range(count)]


class WindowTests(unittest.TestCase):
    def test_attribution_is_exclusive_of_start_inclusive_of_end(self):
        samples = [Sample(at(10), 1), Sample(at(20), 2), Sample(at(30), 3)]
        selected = samples_in(samples, Window(at(10), at(30)))
        # The sample exactly at start belongs to the preceding window.
        self.assertEqual([s.tpm for s in selected], [2, 3])

    def test_duration_and_containment(self):
        outer = Window(at(0), at(60))
        self.assertEqual(outer.duration_seconds, 60)
        self.assertTrue(outer.contains(Window(at(10), at(20))))
        self.assertFalse(outer.contains(Window(at(10), at(90))))

    def test_mean_over_irregular_cadence_reports_span_and_count(self):
        samples = [Sample(at(10), 100), Sample(at(20), 200), Sample(at(24), 300)]
        stats = window_stats(samples, Window(at(0), at(30)))
        self.assertEqual(stats['samples'], 3)
        self.assertEqual(stats['mean_tpm'], 200.0)
        self.assertEqual(stats['min_tpm'], 100)
        self.assertEqual(stats['max_tpm'], 300)
        self.assertEqual(stats['observed_span_seconds'], 14.0)
        self.assertIsNone(stats['unavailable'])

    def test_single_sample_window_is_unavailable_not_a_number(self):
        stats = window_stats([Sample(at(10), 100)], Window(at(0), at(15)))
        self.assertIsNone(stats['mean_tpm'])
        self.assertIn('fewer than', stats['unavailable'])

    def test_empty_window_is_unavailable(self):
        stats = window_stats([Sample(at(10), 100)], Window(at(30), at(40)))
        self.assertEqual(stats['samples'], 0)
        self.assertIsNone(stats['mean_tpm'])

    def test_matched_window_copies_offset_and_duration(self):
        treatment_whole = Window(at(0, 1), at(0, 3))
        analysis = Window(at(30, 1), at(0, 2))
        control_whole = Window(at(0, 10), at(0, 12))
        matched = matched_window(control_whole, treatment_whole, analysis)
        self.assertEqual(matched.start, at(30, 10))
        self.assertEqual(matched.duration_seconds, analysis.duration_seconds)

    def test_recovery_window_spans_backup_end_to_window_end(self):
        whole = Window(at(0), at(60))
        recovery = recovery_window(Window(at(10), at(40)), whole)
        self.assertEqual((recovery.start, recovery.end), (at(40), at(60)))

    def test_recovery_window_absent_when_backup_runs_to_the_end(self):
        whole = Window(at(0), at(60))
        self.assertIsNone(recovery_window(Window(at(10), at(60)), whole))


class DegradationMathTests(unittest.TestCase):
    def ok(self, mean):
        return {'mean_tpm': mean, 'unavailable': None}

    def test_loss_follows_the_documented_formula(self):
        loss, reason = throughput_loss(self.ok(30000), self.ok(15000))
        self.assertIsNone(reason)
        self.assertAlmostEqual(loss, 0.5)

    def test_improvement_is_reported_as_negative_loss(self):
        loss, _ = throughput_loss(self.ok(10000), self.ok(12000))
        self.assertAlmostEqual(loss, -0.2)

    def test_zero_control_is_unavailable_not_infinite(self):
        loss, reason = throughput_loss(self.ok(0), self.ok(100))
        self.assertIsNone(loss)
        self.assertIn('zero', reason)

    def test_unavailable_input_propagates_its_reason(self):
        missing = {'mean_tpm': None, 'unavailable': 'fewer than 2 counter samples'}
        loss, reason = throughput_loss(missing, self.ok(100))
        self.assertIsNone(loss)
        self.assertTrue(reason.startswith('control:'))
        loss, reason = throughput_loss(self.ok(100), missing)
        self.assertTrue(reason.startswith('backup:'))

    def test_latency_change_and_guards(self):
        value, reason = latency_change({'p95_ms': 20.0}, {'p95_ms': 50.0})
        self.assertIsNone(reason)
        self.assertAlmostEqual(value, 1.5)
        self.assertEqual(latency_change({'p95_ms': None}, {'p95_ms': 5.0})[0], None)
        self.assertEqual(latency_change({'p95_ms': 0}, {'p95_ms': 5.0})[0], None)


class PairRecordTests(unittest.TestCase):
    def pair(self, analysis_start=at(20, 1), analysis_end=at(50, 1),
             control_tpm=30000, backup_tpm=3000):
        control_whole = Window(at(0, 10), at(0, 12))
        treatment_whole = Window(at(0, 1), at(0, 3))
        return dict(
            pair_id='p1', repetition=1, order=('control', 'backup'),
            control={
                'series': steady(10, 5, 13, control_tpm),
                'latency': {'p95_ms': 20.0, 'scope': 'whole_run'},
                'aggregate': {'nopm': 13000, 'tpm': control_tpm},
                'whole': control_whole,
            },
            treatment={
                'series': steady(1, 5, 13, backup_tpm),
                'latency': {'p95_ms': 60.0, 'scope': 'whole_run'},
                'aggregate': {'nopm': 1300, 'tpm': backup_tpm},
                'whole': treatment_whole,
            },
            backup_result={'schema_version': 2, 'status': 'PASS_MANIFEST_ONLY'},
            capture_started_at=analysis_start, verified_at=analysis_end,
        )

    def test_accepted_pair_reports_loss_and_keeps_windows_separate(self):
        record = build_pair_record(**self.pair())
        self.assertTrue(record['accepted'])
        self.assertIsNone(record['rejection_reason'])
        self.assertAlmostEqual(record['throughput_loss_fraction'], 0.9)
        self.assertIn('whole_window', record['backup'])
        self.assertIn('analysis_window', record['backup'])
        self.assertIn('recovery_window', record['backup'])
        self.assertIn('matched_window', record['control'])

    def test_backup_outside_the_window_is_rejected(self):
        # Capture starts before ramp-up completes, as a mistimed run would.
        record = build_pair_record(**self.pair(analysis_start=at(30, 0)))
        self.assertFalse(record['accepted'])
        self.assertIn('entirely inside', record['rejection_reason'])
        self.assertIsNone(record['throughput_loss_fraction'])
        self.assertIn('throughput_loss', record['unavailable_metrics'])

    def test_rejected_pair_reports_no_latency_number_either(self):
        record = build_pair_record(**self.pair(analysis_start=at(30, 0)))
        self.assertIsNone(record['latency']['increase_fraction'])

    def test_latency_is_always_labelled_whole_run(self):
        record = build_pair_record(**self.pair())
        self.assertEqual(record['latency']['scope'], 'whole_run')
        self.assertIn('not', record['latency']['note'])

    def test_network_bytes_is_null_with_a_reason(self):
        record = build_pair_record(**self.pair())
        self.assertIsNone(record['network_bytes'])
        self.assertIn('network_bytes', record['unavailable_metrics'])

    def test_record_is_json_serialisable(self):
        import json
        record = build_pair_record(**self.pair())
        self.assertEqual(json.loads(json.dumps(record))['pair_id'], 'p1')

    def test_embeds_the_shared_backup_record(self):
        record = build_pair_record(**self.pair())
        self.assertEqual(record['backup_result']['schema_version'], 2)


class StubDockerTests(unittest.TestCase):
    """Orchestration against a stand-in docker CLI. No containers are started."""

    def setUp(self):
        import os
        import sys as _sys
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        template = make_jobs_db(self.root / 'template.DB')
        launcher = self.root / ('docker.cmd' if os.name == 'nt' else 'docker')
        script = Path(__file__).resolve().parent / 'stub_docker.py'
        if os.name == 'nt':
            launcher.write_text(
                f'@echo off\r\n"{_sys.executable}" "{script}" %*\r\n',
                encoding='utf-8')
        else:
            launcher.write_text(
                f'#!/bin/sh\nexec "{_sys.executable}" "{script}" "$@"\n',
                encoding='utf-8')
            launcher.chmod(0o755)
        self.docker = str(launcher)
        self.environment = {
            'STUB_STATE': str(self.state),
            'STUB_JOBS_DB': str(template),
            'STUB_WINDOW': '2.0',
            'STUB_CADENCE': '0.02',
            'STUB_EXEC_MS': '120',
        }
        patcher = mock.patch.dict(os.environ, self.environment)
        patcher.start()
        self.addCleanup(patcher.stop)

    def options(self, **overrides):
        from types import SimpleNamespace
        base = dict(
            vu=2, rampup=0, duration=0, backup_offset=0,
            method='pg_basebackup_full', checksum='SHA256', checkpoint='fast',
            pg_host='postgres', pg_port=5432, pg_user='postgres',
            pg_password='secret', network='net', compose_project='fixture',
            hammerdb_image='img', runner_container='runner',
            backup_output='/results/out', scripts_dir=str(self.root),
            timeout=60, marker_margin=30, observer_margin=5,
            docker=self.docker,
            tools={'pg_basebackup': 'pg_basebackup',
                   'pg_verifybackup': 'pg_verifybackup',
                   'pg_combinebackup': 'pg_combinebackup'})
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_start_workload_passes_settings_and_mounts_scripts(self):
        start_workload(name='c1', image='img', network='net', host='postgres',
                       password='secret',
                       settings={'vu': 4, 'rampup': 1, 'duration': 2},
                       scripts_dir=str(self.root), docker_bin=self.docker)
        args = (self.state / 'c1.args').read_text().splitlines()
        self.assertIn('-d', args)
        self.assertNotIn('--rm', args)  # the jobs DB must survive the run
        self.assertIn('TPCC_VU=4', args)
        self.assertIn('TPCC_RAMPUP=1', args)
        self.assertIn('PGBENCH_PASS=secret', args)
        self.assertIn('./hammerdbcli', args)

    def test_wait_for_marker_finds_and_times_out(self):
        start_workload(name='c2', image='i', network='n', host='h',
                       password='p', settings={'vu': 1, 'rampup': 0, 'duration': 0},
                       scripts_dir=str(self.root), docker_bin=self.docker)
        self.assertTrue(wait_for_marker('c2', 'Rampup complete', timeout=5,
                                        docker_bin=self.docker, sleep=lambda _: None))
        with self.assertRaises(TrialError):
            wait_for_marker('c2', 'never appears', timeout=0.05,
                            docker_bin=self.docker, sleep=lambda _: None)

    def test_collect_writes_evidence_and_preserves_carriage_returns(self):
        start_workload(name='c3', image='i', network='n', host='h',
                       password='p', settings={'vu': 1, 'rampup': 0, 'duration': 0},
                       scripts_dir=str(self.root), docker_bin=self.docker)
        directory = self.root / 'trial'
        log_path, database = collect_workload('c3', directory,
                                              docker_bin=self.docker)
        self.assertTrue(log_path.exists() and database.exists())
        self.assertIn('\r', log_path.read_bytes().decode())
        self.assertTrue(read_tpm_series(database))

    def test_executor_prefixes_docker_exec_and_records_wall_clock(self):
        execute, phases = make_executor('runner', self.root / 'phases', 30,
                                        docker_bin=self.docker)
        before = datetime.now(timezone.utc)
        result = execute('backup', ['pg_basebackup', '--pgdata', '/x'])
        self.assertEqual(result['exit_code'], 0)
        self.assertIn('backup', phases)
        self.assertLessEqual(before, phases['backup']['started_at'])
        self.assertLessEqual(phases['backup']['started_at'],
                             phases['backup']['ended_at'])
        logged = (self.state / 'exec.log').read_text()
        self.assertIn('exec runner pg_basebackup', logged)

    def test_read_trial_rejects_an_incomplete_window(self):
        directory = self.root / 'bad'
        directory.mkdir()
        (directory / 'hammerdb.log').write_text('no markers here\n')
        with self.assertRaises(TrialError):
            read_trial(directory / 'hammerdb.log', self.database_template())

    def database_template(self):
        return make_jobs_db(self.root / 'another.DB')

    def test_control_trial_produces_a_usable_window(self):
        trial = run_trial('control', options=self.options(),
                          directory=self.root / 'ctl', sleep=lambda _: None)
        self.assertEqual(trial['treatment'], 'control')
        self.assertIsNone(trial['backup_result'])
        self.assertGreater(len(trial['series']), 2)
        self.assertAlmostEqual(trial['whole'].duration_seconds, 2.0, places=1)
        self.assertEqual(trial['aggregate'], {'nopm': 9000, 'tpm': 21000})

    def test_backup_trial_records_phase_boundaries_inside_the_window(self):
        trial = run_trial('backup', options=self.options(),
                          directory=self.root / 'bkp', sleep=lambda _: None)
        self.assertIsNotNone(trial['backup_result'])
        self.assertEqual(trial['backup_result']['schema_version'], 2)
        self.assertIsNotNone(trial['capture_started_at'])
        self.assertLess(trial['capture_started_at'], trial['verified_at'])
        # The backup must sit inside the measured window, or the pair is void.
        self.assertTrue(trial['whole'].contains(
            Window(trial['capture_started_at'], trial['verified_at'])))
        self.assertTrue((self.root / 'bkp' / 'backup-result.json').exists())

    def test_each_backup_trial_uses_a_fresh_destination(self):
        """pg_basebackup refuses a non-empty directory.

        A shared output path would fail every repetition after the first.
        """
        destinations = []
        for index in range(2):
            run_trial('backup', options=self.options(),
                      directory=self.root / f'rep{index}', sleep=lambda _: None)
        for line in (self.state / 'exec.log').read_text().splitlines():
            if '--pgdata' in line:
                parts = line.split()
                destinations.append(parts[parts.index('--pgdata') + 1])
        self.assertEqual(len(destinations), 2)
        self.assertNotEqual(destinations[0], destinations[1])
        # Separator style is host dependent: backup.py wraps the destination
        # in Path, which rewrites it on Windows. The driver is documented as
        # Linux only for exactly that reason, so compare normalised.
        normalised = [d.replace('\\', '/') for d in destinations]
        self.assertTrue(all(d.startswith('/results/out/') for d in normalised),
                        normalised)
        self.assertEqual(len({Path(d).name for d in normalised}), 2)

    def test_failed_docker_run_raises_rather_than_measuring(self):
        import os
        with mock.patch.dict(os.environ, {'STUB_FAIL': 'run'}):
            with self.assertRaises(TrialError):
                run_trial('control', options=self.options(),
                          directory=self.root / 'fail', sleep=lambda _: None)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_main(self, *extra, env=None):
        import os
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        with mock.patch.dict(os.environ, env or {}, clear=False):
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                return main(['--output', str(self.root / 'run'), *extra])

    def test_missing_password_is_refused(self):
        import os
        environment = {k: v for k, v in os.environ.items()
                       if k != 'POSTGRES_PASSWORD'}
        with mock.patch.dict(os.environ, environment, clear=True):
            self.assertEqual(self.run_main(), 2)
        self.assertFalse((self.root / 'run').exists())

    def test_existing_output_is_refused(self):
        (self.root / 'run').mkdir()
        self.assertEqual(self.run_main(env={'POSTGRES_PASSWORD': 'p'}), 2)

    def test_zero_repeats_is_refused(self):
        self.assertEqual(
            self.run_main('--repeats', '0', env={'POSTGRES_PASSWORD': 'p'}), 2)

    def test_module_runs_as_main(self):
        """Executing as __main__ must reach every definition it uses.

        Importing the module defines everything before anything runs, so a
        helper placed after the __main__ guard still resolves in tests while
        failing with NameError in the only path that matters in production.
        """
        import subprocess
        import sys as _sys
        done = subprocess.run(
            [_sys.executable, '-m', 'benchmarks.hammerdb_experiment', '--help'],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parent.parent), timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn('NameError', done.stderr)
        self.assertIn('--output', done.stdout)

    def test_guards_reject_bad_input_when_run_as_main(self):
        import os
        import subprocess
        import sys as _sys
        environment = {k: v for k, v in os.environ.items()
                       if k != 'POSTGRES_PASSWORD'}
        done = subprocess.run(
            [_sys.executable, '-m', 'benchmarks.hammerdb_experiment',
             '--output', str(self.root / 'never')],
            capture_output=True, text=True, env=environment,
            cwd=str(Path(__file__).resolve().parent.parent), timeout=120)
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertNotIn('Traceback', done.stderr)

    def test_seeded_order_is_reproducible(self):
        import random as _random
        first = _random.Random(7)
        second = _random.Random(7)
        orders = []
        for chooser in (first, second):
            sequence = []
            for _ in range(5):
                order = ['control', 'backup']
                chooser.shuffle(order)
                sequence.append(tuple(order))
            orders.append(sequence)
        self.assertEqual(orders[0], orders[1])
        self.assertGreater(len(set(orders[0])), 1)


if __name__ == '__main__':
    unittest.main()
