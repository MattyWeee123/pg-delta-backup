"""Tests for the observability stack: the pgbench exporter and the backfill of committed data.

The exporter's parser is a local copy of measure.parse_pgbench_line (see the exporter's docstring for why).
The first test here is what keeps the copy honest: it runs both over the same corpus and requires identical
results. It skips where measure cannot be imported at all, which is any non-POSIX host, since measure pulls in
fcntl at import time.
"""
from __future__ import annotations

import importlib.util
import json
import math
import re
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'infra' / 'observability'))
sys.path.insert(0, str(ROOT / 'experiments' / 'network'))

import backfill  # noqa: E402
import pgbench_exporter as exporter  # noqa: E402

# A corpus covering every shape PostgreSQL 17 writes, plus the malformed lines a truncated log can end with.
CORPUS = (
    '0 1 1234 0 1761600000 123456 500',          # with -R: the schedule-lag column is present
    '3 77 980 1 1761600000 000001',              # without -R: no lag column
    '0 2 failed 0 1761600000 123456 500',        # a failed transaction
    '0 3 skipped 0 1761600000 123456 500',       # skipped under -R when too far behind
    '12 9 0 2 1761600001 999999 0',              # a zero latency and a zero lag
    '0 4 1234 0 1761600000',                     # too few fields: not a line
    '',                                          # blank
    'garbage',
    'x y z w v u',                               # right field count, wrong types
    '0 5 1234 0 1761600000 abc 500',             # a bad microsecond field
)


def write_log(directory: Path, name: str, lines: list[str]) -> Path:
    path = directory / name
    path.write_text(''.join(line + '\n' for line in lines))
    return path


def txn(end_epoch: float, latency_ms: float, *, client: int = 0, number: int = 1,
        lag_us: int | None = 400) -> str:
    """One pgbench -l line completing at end_epoch with the given latency."""
    seconds = int(end_epoch)
    micros = int(round((end_epoch - seconds) * 1e6))
    fields = [client, number, int(round(latency_ms * 1000)), 0, seconds, f'{micros:06d}']
    if lag_us is not None:
        fields.append(lag_us)
    return ' '.join(str(field) for field in fields)


class ParserAgreesWithMeasure(unittest.TestCase):
    """The exporter's copy of the parser must not drift from measure's."""

    def test_the_exporters_parser_returns_what_measures_parser_returns_for_every_corpus_line(self):
        if importlib.util.find_spec('fcntl') is None:
            self.skipTest('measure imports fcntl, so it cannot be imported off POSIX')
        import measure

        for line in CORPUS:
            with self.subTest(line=line):
                self.assertEqual(exporter.parse_pgbench_line(line), measure.parse_pgbench_line(line))


class QuantileTests(unittest.TestCase):
    def test_it_matches_numpys_linear_percentile(self):
        try:
            import numpy
        except ImportError:
            self.skipTest('numpy is not installed')
        for values in ([1.0], [1.0, 2.0], [5.0, 1.0, 3.0, 2.0, 4.0], [float(n) for n in range(100)]):
            ordered = sorted(values)
            for q in exporter.QUANTILES:
                with self.subTest(values=len(values), q=q):
                    self.assertAlmostEqual(exporter.quantile(ordered, q),
                                           float(numpy.percentile(values, q * 100)), places=9)

    def test_an_empty_sample_is_nan_so_a_dashboard_shows_a_gap_not_a_zero(self):
        self.assertTrue(math.isnan(exporter.quantile([], 0.95)))


class TailerTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_it_reads_every_thread_log_not_just_the_first(self):
        now = time.time()
        write_log(self.dir, 'load.100', [txn(now, 5.0, number=1)])
        write_log(self.dir, 'load.100.1', [txn(now, 6.0, number=2)])
        write_log(self.dir, 'load.100.2', [txn(now, 7.0, number=3)])
        tailer = exporter.LogTailer(self.dir, 'load.*')
        self.addCleanup(tailer.close)
        latencies = sorted(parsed[2] for parsed in tailer.read())
        self.assertEqual(latencies, [5000, 6000, 7000])

    def test_a_line_already_returned_is_not_returned_again(self):
        now = time.time()
        path = write_log(self.dir, 'load.100', [txn(now, 5.0)])
        tailer = exporter.LogTailer(self.dir, 'load.*')
        self.addCleanup(tailer.close)
        self.assertEqual(len(list(tailer.read())), 1)
        self.assertEqual(list(tailer.read()), [])
        with open(path, 'a') as stream:
            stream.write(txn(now, 6.0, number=2) + '\n')
        self.assertEqual([parsed[2] for parsed in tailer.read()], [6000])

    def test_a_partial_trailing_line_is_held_until_it_is_complete(self):
        """pgbench's log is read while it is being written, so a half-written line must not be parsed or lost."""
        now = time.time()
        path = self.dir / 'load.100'
        complete = txn(now, 5.0)
        partial = txn(now, 6.0, number=2)
        path.write_text(complete + '\n' + partial)          # no trailing newline
        tailer = exporter.LogTailer(self.dir, 'load.*')
        self.addCleanup(tailer.close)
        self.assertEqual([parsed[2] for parsed in tailer.read()], [5000])
        with open(path, 'a') as stream:
            stream.write('\n')
        self.assertEqual([parsed[2] for parsed in tailer.read()], [6000])

    def test_a_file_replaced_under_the_same_name_is_read_from_its_start(self):
        """Each run makes new logs. Tracking by inode, not name, stops a new file resuming at a stale offset."""
        now = time.time()
        name = 'load.100'
        write_log(self.dir, name, [txn(now, 5.0), txn(now, 5.1, number=2), txn(now, 5.2, number=3)])
        tailer = exporter.LogTailer(self.dir, 'load.*')
        self.addCleanup(tailer.close)
        self.assertEqual(len(list(tailer.read())), 3)
        # Make the replacement before the old file goes, so the new one cannot reuse its inode number.
        replacement = self.dir / 'load.101'
        write_log(self.dir, 'load.101', [txn(now, 9.0, number=1)])
        (self.dir / name).unlink()
        replacement.rename(self.dir / name)
        self.assertEqual([parsed[2] for parsed in tailer.read()], [9000])

    def test_a_missing_directory_is_not_an_error_because_the_load_may_start_later(self):
        tailer = exporter.LogTailer(self.dir / 'not-yet', 'load.*')
        self.addCleanup(tailer.close)
        self.assertEqual(list(tailer.read()), [])


class CollectorTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def collector(self, window_seconds: float = 5.0, window_file: Path | None = None) -> exporter.Collector:
        made = exporter.Collector(self.dir, window_seconds, window_file, 'load.*')
        self.addCleanup(made.tailer.close)
        return made

    def test_failed_and_skipped_transactions_are_counted_and_kept_out_of_the_quantiles(self):
        now = 1_000_000.0
        write_log(self.dir, 'load.100', [txn(now, 5.0), txn(now, 7.0, number=2),
                                         '0 3 failed 0 1000000 000000 400',
                                         '0 4 skipped 0 1000000 000000 400'])
        made = self.collector()
        made.poll(now=now)
        self.assertEqual(made.failed_total, 2)
        self.assertEqual(made.completed_total, 2)
        rendered = made.render(now=now)
        self.assertIn('pgdelta_txn_failed_total 2.0', rendered)
        self.assertIn('pgdelta_txn_window_count 2.0', rendered)

    def test_samples_are_trimmed_by_their_own_completion_time_not_by_when_they_were_read(self):
        """A log read late must not report stale transactions as current."""
        now = 1_000_000.0
        write_log(self.dir, 'load.100', [txn(now - 60, 50.0), txn(now - 1, 6.0, number=2)])
        made = self.collector(window_seconds=5.0)
        made.poll(now=now)
        self.assertEqual(len(made.samples), 1)
        self.assertEqual(made.completed_total, 2, 'both were seen, even though one left the window')
        self.assertAlmostEqual(exporter.quantile(sorted(s[1] for s in made.samples), 0.95), 6.0)

    def test_the_backup_window_is_active_only_between_t0_and_t1(self):
        path = self.dir / 'backup-window.json'
        path.write_text(json.dumps({'t0_utc': 100.0, 't1_utc': 200.0}))
        made = self.collector(window_file=path)
        self.assertIn('pgdelta_backup_active 0.0', made.render(now=99.0))
        self.assertIn('pgdelta_backup_active 1.0', made.render(now=150.0))
        self.assertIn('pgdelta_backup_active 0.0', made.render(now=201.0))

    def test_a_window_with_no_end_yet_counts_as_active_so_a_running_arm_is_shaded(self):
        path = self.dir / 'backup-window.json'
        path.write_text(json.dumps({'t0_utc': 100.0}))
        made = self.collector(window_file=path)
        rendered = made.render(now=150.0)
        self.assertIn('pgdelta_backup_active 1.0', rendered)
        self.assertIn('pgdelta_backup_window_end_seconds NaN', rendered)

    def test_an_unreadable_or_absent_window_file_leaves_the_window_nan_rather_than_failing(self):
        made = self.collector(window_file=self.dir / 'absent.json')
        rendered = made.render(now=150.0)
        self.assertIn('pgdelta_backup_window_start_seconds NaN', rendered)
        self.assertIn('pgdelta_backup_active 0.0', rendered)

        bad = self.dir / 'bad.json'
        bad.write_text('{not json')
        self.assertIn('pgdelta_backup_active 0.0', self.collector(window_file=bad).render(now=150.0))

    def test_every_rendered_metric_is_declared_with_a_help_and_type_line(self):
        """Prometheus accepts undeclared metrics, but promtool check fails on a mismatched or missing TYPE."""
        now = 1_000_000.0
        write_log(self.dir, 'load.100', [txn(now, 5.0)])
        made = self.collector()
        made.poll(now=now)
        rendered = made.render(now=now)
        declared = {line.split()[2] for line in rendered.splitlines() if line.startswith('# TYPE ')}
        helped = {line.split()[2] for line in rendered.splitlines() if line.startswith('# HELP ')}
        emitted = {line.split('{')[0].split()[0]
                   for line in rendered.splitlines() if line and not line.startswith('#')}
        self.assertEqual(emitted, declared)
        self.assertEqual(emitted, helped)

    def test_the_rate_is_the_windows_count_over_the_windows_length(self):
        now = 1_000_000.0
        write_log(self.dir, 'load.100', [txn(now - 0.5, 5.0, number=n) for n in range(10)])
        made = self.collector(window_seconds=2.0)
        made.poll(now=now)
        self.assertIn('pgdelta_txn_rate_per_second 5.0', made.render(now=now))




class DrLatencyBackfill(unittest.TestCase):
    """The DR round-trip evidence (EXP-20, EXP-24, EXP-94) reaches Prometheus.

    Before this, backfill.py carried only the slowdown experiments (EXP-12, -28, -57), so the one thing the
    latency chain actually decided - whether v1 holds at 20, 60 and 120 ms, and that EXP-20's "local changes
    only" was cubic's slow mode rather than v1's design (EXP-21, EXP-23, EXP-24) - could not be seen in Grafana
    at all. These CSVs are wide: v1, full and b_fast are three arms' seconds in one row, so each becomes the
    same metric separated by an `arm` label, as the three latency quantiles already are.
    """

    @classmethod
    def setUpClass(cls):
        cls.backfill = backfill
        cls.rendered = cls.backfill.render(step_seconds=600.0)

    def series(self, metric):
        return [line for line in self.rendered.splitlines() if line.startswith(metric + '{')]

    def test_each_dr_session_is_rendered_with_its_own_experiment_label(self):
        for experiment in ('EXP-20', 'EXP-24-cubic', 'EXP-24-bbr'):
            with self.subTest(experiment=experiment):
                self.assertIn(experiment, self.backfill.SESSIONS)
                self.assertTrue([line for line in self.series('pgdelta_evidence_refresh_seconds')
                                 if f'experiment="{experiment}"' in line],
                                f'no refresh_seconds series for {experiment}')

    def test_the_three_arms_of_a_row_become_one_metric_split_by_arm(self):
        hot60 = [line for line in self.series('pgdelta_evidence_refresh_seconds')
                 if 'experiment="EXP-20"' in line and 'pattern="hot"' in line
                 and 'condition="2:60:default"' in line]
        arms = {line.split('arm="')[1].split('"')[0] for line in hot60}
        self.assertEqual(arms, {'v1', 'full', 'b_fast'})
        # EXP-20's measured hot 60 ms row: v1 5.35 s, one pg_basebackup 44.44 s, fastest parallel copy 9.94 s
        v1 = next(line for line in hot60 if 'arm="v1"' in line)
        self.assertAlmostEqual(float(v1.split('} ')[1].split()[0]), 5.35, places=2)

    def test_both_verdict_ratios_are_rendered_and_named_by_what_they_divide_by(self):
        ratios = [line for line in self.series('pgdelta_evidence_ratio')
                  if 'experiment="EXP-20"' in line and 'pattern="uniform"' in line
                  and 'condition="2:60:default"' in line]
        against = {line.split('against="')[1].split('"')[0] for line in ratios}
        self.assertEqual(against, {'fastest_parallel_copy', 'one_pg_basebackup_stream'})
        r_fast = next(line for line in ratios if 'against="fastest_parallel_copy"' in line)
        self.assertAlmostEqual(float(r_fast.split('} ')[1].split()[0]), 2.58, places=2)

    def test_bbr_is_the_arm_that_rescues_scattered_changes(self):
        """EXP-24's deciding reading: cubic 2.36 and BBR below 1 for uniform 1% at 60 ms tuned."""
        def r_fast(experiment):
            line = next(line for line in self.series('pgdelta_evidence_ratio')
                        if f'experiment="{experiment}"' in line and 'pattern="uniform"' in line
                        and 'condition="2:60:tuned"' in line and 'against="fastest_parallel_copy"' in line)
            return float(line.split('} ')[1].split()[0])
        self.assertGreater(r_fast('EXP-24-cubic'), 1.0)
        self.assertLess(r_fast('EXP-24-bbr'), 1.0)

    def test_every_rendered_metric_still_has_a_help_line(self):
        emitted = {line.split('{')[0] for line in self.rendered.splitlines()
                   if line and not line.startswith('#')}
        helped = {line.split()[2] for line in self.rendered.splitlines() if line.startswith('# HELP ')}
        self.assertEqual(emitted, helped)
        # a HELP line that just repeats the metric name means HELP was never written for it
        for line in self.rendered.splitlines():
            if line.startswith('# HELP '):
                metric, text = line.split()[2], ' '.join(line.split()[3:])
                self.assertNotEqual(metric, text, f'{metric} has no help text of its own')


class BackfillSessionFilter(unittest.TestCase):
    """--only imports one session's blocks without re-importing the rest.

    Prometheus's data volume already holds the sessions imported before, so a new session (EXP-94, say) must be
    addable on its own; rendering everything again would write a second block of samples already stored.
    """

    def test_only_keeps_just_the_named_sessions(self):
        document = backfill.render(step_seconds=600.0, only=('EXP-20',))
        emitted = {line.split('experiment="')[1].split('"')[0]
                   for line in document.splitlines() if 'experiment="' in line}
        self.assertEqual(emitted, {'EXP-20'})
        self.assertIn('pgdelta_evidence_ratio', document)

    def test_several_sessions_can_be_named_at_once(self):
        document = backfill.render(step_seconds=600.0, only=('EXP-24-cubic', 'EXP-24-bbr'))
        emitted = {line.split('experiment="')[1].split('"')[0]
                   for line in document.splitlines() if 'experiment="' in line}
        self.assertEqual(emitted, {'EXP-24-cubic', 'EXP-24-bbr'})

    def test_no_filter_renders_every_session_as_before(self):
        document = backfill.render(step_seconds=600.0)
        emitted = {line.split('experiment="')[1].split('"')[0]
                   for line in document.splitlines() if 'experiment="' in line}
        self.assertEqual(emitted, set(backfill.SESSIONS))

    def test_an_unknown_session_is_refused_rather_than_rendering_nothing(self):
        """A typo must not look like a successful import of an empty document."""
        with self.assertRaises(SystemExit) as raised:
            backfill.main(['--stdout', '--only', 'EXP-99'])
        self.assertIn('EXP-99', str(raised.exception))


class DrDashboard(unittest.TestCase):
    """Every panel of the committed DR dashboard matches series the backfill actually renders.

    This exists because of a bug it would have caught. The panels first used instant queries, as the older
    evidence dashboard's do. A backfilled session is a flat line across its own window and the windows do not
    overlap - EXP-20 ran 03:59-05:11 UTC, EXP-24 06:30-07:41 - so an instant query, evaluated at the time
    range's end, showed at most one session, and none once the range ended more than Prometheus's 5-minute
    staleness lookback past the last sample. A panel whose matchers name a label the backfill never writes
    fails the same way: silently empty.
    """

    DASHBOARD = ROOT / 'infra' / 'observability' / 'grafana' / 'dashboards' / 'pgdelta-dr-latency.json'

    @classmethod
    def setUpClass(cls):
        cls.panels = [panel for panel in json.loads(cls.DASHBOARD.read_text())['panels']
                      if panel['type'] != 'text']
        cls.rendered = backfill.render(step_seconds=600.0)

    @staticmethod
    def matchers(expr):
        """(metric, [(label, operator, value)]) from a simple `metric{a="b",c=~"d|e"}` selector."""
        metric, _, rest = expr.partition('{')
        found = []
        for clause in re.findall(r'(\w+)\s*(=~|=)\s*"([^"]*)"', rest):
            found.append(clause)
        return metric, found

    def matching_lines(self, expr):
        metric, clauses = self.matchers(expr)
        lines = []
        for line in self.rendered.splitlines():
            if not line.startswith(metric + '{'):
                continue
            labels = dict(re.findall(r'(\w+)="([^"]*)"', line.split('}')[0]))
            if all((labels.get(name) in value.split('|')) if op == '=~' else (labels.get(name) == value)
                   for name, op, value in clauses):
                lines.append(line)
        return lines

    def test_every_panel_matches_series_the_backfill_renders(self):
        for panel in self.panels:
            for target in panel['targets']:
                with self.subTest(panel=panel['id'], expr=target['expr']):
                    self.assertTrue(self.matching_lines(target['expr']),
                                    f'panel {panel["id"]} matches nothing the backfill writes')

    def test_no_panel_uses_an_instant_query(self):
        for panel in self.panels:
            for target in panel['targets']:
                with self.subTest(panel=panel['id']):
                    self.assertFalse(target.get('instant'),
                                     'an instant query is evaluated at the range end and would show at most '
                                     'one of the three sessions')
                    self.assertTrue(target.get('range'))

    def test_the_deciding_comparison_has_both_of_exp24s_arms_in_one_panel(self):
        """The dashboard's whole point: cubic beside BBR in the same cells."""
        scattered = next(panel for panel in self.panels if panel['id'] == 2)
        experiments = {re.search(r'experiment="([^"]*)"', line).group(1)
                       for line in self.matching_lines(scattered['targets'][0]['expr'])}
        self.assertEqual(experiments, {'EXP-20', 'EXP-24-cubic', 'EXP-24-bbr'})

    def test_the_deciding_panels_do_not_filter_by_experiment_so_a_new_session_needs_no_dashboard_edit(self):
        """EXP-94's cells should appear beside EXP-20's and EXP-24's by backfilling them, nothing more.

        A panel that names `experiment="EXP-20"` shows only EXP-20 forever, which is how a comparison
        dashboard quietly stops comparing. The two ratio panels and the arm-seconds panel are the comparison,
        so they must stay open; the baselines panel is deliberately one session's and is allowed to pin it.
        """
        open_panels = [panel for panel in self.panels if panel['id'] in (2, 3, 4, 5, 6)]
        self.assertEqual(len(open_panels), 5)
        for panel in open_panels:
            for target in panel['targets']:
                with self.subTest(panel=panel['id']):
                    self.assertNotIn('experiment=', target['expr'],
                                     'this panel would not show a later session')
                    self.assertIn('{{experiment}}', target['legendFormat'],
                                  'a panel comparing sessions has to name which is which')

    def test_the_dashboards_time_range_covers_every_session_it_plots(self):
        dashboard = json.loads(self.DASHBOARD.read_text())
        start = backfill.utc(dashboard['time']['from'].replace('Z', '').replace('.000', ''))
        end = backfill.utc(dashboard['time']['to'].replace('Z', '').replace('.000', ''))
        for name in ('EXP-20', 'EXP-24-cubic', 'EXP-24-bbr'):
            with self.subTest(session=name):
                session = backfill.SESSIONS[name]
                self.assertLessEqual(start, session.start)
                self.assertGreaterEqual(end, session.end)


if __name__ == '__main__':
    unittest.main()
