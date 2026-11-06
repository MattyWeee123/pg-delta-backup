"""EXP-94's scorer, over synthetic records, so what each prediction means is fixed before the data exists.

EXP-20's scorer judges an arm against the full copies. EXP-94's predictions are arm-against-arm in the same
cell - hashing ahead against the same engine without it - which EXP-20's scorer has no notion of, so this is
what the session's deciding reading rests on. Written and committed before any VM exists (rule 16).
"""
from __future__ import annotations

import csv
import json
import sys
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'analysis'))
sys.path.insert(0, str(ROOT))

import exp94  # noqa: E402

V1, BBR = 'pgdelta_direct_inplace', 'pgdelta_direct_inplace_bbr'
HA, BBR_HA = 'pgdelta_direct_inplace_ha', 'pgdelta_direct_inplace_bbr_ha'
# The per-sweep directory run_exp04.py groups run records under. Named like the real session's
# (`00-hot-f0.01-s70`) so the fixture's layout is the layout the scorer has to read.
SWEEP = '00-hot-f0.01-s70'


def write_sweep(root: Path, records, link_gbit=2.0):
    """A sweep directory as run_exp04 leaves one: summary.csv, runs/<sweep>/<name>.json, a trial with link tests.

    The run records go one level deeper than this fixture first wrote them. run_exp04.py groups them by sweep
    (`runs/00-hot-f0.01-s70/r0-...json`), and writing them flat here is what let `exp94.runs()` ship globbing
    `runs/*.json`: the fixture agreed with the bug, so 48 passing tests said nothing about whether the scorer
    could read a real session. It could not - it read zero records from EXP-94's own 222.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / 'runs' / SWEEP).mkdir(parents=True, exist_ok=True)
    (root / 'trials').mkdir(exist_ok=True)
    fields = ['run_name', 'pattern', 'speed', 'method', 'repeat', 'status', 'total_s']
    with open(root / 'summary.csv', 'w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow({key: record[key] for key in fields})
    for record in records:
        run = {
            'status': record['status'], 'repeat': record['repeat'], 'method': record['method'],
            'pattern': record['pattern'], 'speed': {'label': record['speed']},
            'totals': {'wall_seconds': record['total_s'],
                       'wire_bytes_source_to_dest': record.get('sent_bytes', 600 << 20)},
            'tool_reported': {'page_send_s': record.get('page_send_s', 1.0),
                              'leaf_wait_idle_s': record.get('leaf_wait_idle_s', 0.0)},
            'phases': {'transfer': {'disk': {'source': {'md0': {'read_bytes': record.get('source_read', 1 << 30)}}}}},
        }
        (root / 'runs' / SWEEP / f"{record['run_name']}.json").write_text(json.dumps(run), encoding='utf-8')
    (root / 'trials' / 't.json').write_text(json.dumps({
        'visits': [{'condition': {'label': label}, 'link_tests': [{'steady_gbit': link_gbit}]}
                   for label in sorted({record['speed'] for record in records})]}), encoding='utf-8')


def record(pattern, speed, method, repeat, total_s, **extra):
    return {'run_name': f'{pattern}-{speed}-{method}-r{repeat}'.replace(':', '_'), 'pattern': pattern,
            'speed': speed, 'method': method, 'repeat': repeat, 'status': 'pass', 'total_s': total_s, **extra}


class RunRecordLayout(unittest.TestCase):
    """runs() has to read the layout run_exp04.py writes, and nothing here may assume a flatter one.

    EXP-94's session is what this is for: the scorer globbed one level too shallow, read 0 of 222 records, and
    reported predictions 5, 6 and 7 empty and prediction 1 undecidable - while 48 tests passed, because the
    fixture wrote the shallow layout too. The assertion is therefore on the path, not only on the count.
    """

    def setUp(self):
        self.tmp = Path(self.enterContext(__import__('tempfile').TemporaryDirectory()))
        self.sweep = self.tmp / 'exp94'

    def test_records_are_read_from_the_per_sweep_directory(self):
        write_sweep(self.sweep, [record('uniform', '2:60:tuned', V1, k, 20.0) for k in range(3)])
        self.assertTrue((self.sweep / 'runs' / SWEEP).is_dir(), 'the fixture must write the nested layout')
        self.assertEqual([], list((self.sweep / 'runs').glob('*.json')), 'nothing may be written flat')
        self.assertEqual(3, len(exp94.runs(self.sweep)))

    def test_a_flat_layout_is_not_silently_read_as_an_empty_session(self):
        """A sweep with records only in the old flat place yields 0, and that is the shape of the bug."""
        write_sweep(self.sweep, [record('uniform', '2:60:tuned', V1, k, 20.0) for k in range(3)])
        for path in (self.sweep / 'runs' / SWEEP).glob('*.json'):
            path.rename(self.sweep / 'runs' / path.name)
        (self.sweep / 'runs' / SWEEP).rmdir()
        self.assertEqual([], exp94.runs(self.sweep))


class Medians(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(__import__('tempfile').TemporaryDirectory()))
        self.sweep = self.tmp / 'exp94'

    def test_only_passing_runs_count_and_the_median_is_taken(self):
        write_sweep(self.sweep, [
            record('uniform', '2:60:tuned', V1, 1, 20.0),
            record('uniform', '2:60:tuned', V1, 2, 30.0),
            record('uniform', '2:60:tuned', V1, 3, 22.0),
            {**record('uniform', '2:60:tuned', V1, 4, 999.0), 'status': 'fail'},
        ])
        cells = exp94.cells(self.sweep)
        self.assertEqual(cells[('uniform', '2:60:tuned', V1)], (22.0, 3))


class Predictions(unittest.TestCase):
    """Each prediction's bar, as the registration states it."""

    def setUp(self):
        self.tmp = Path(self.enterContext(__import__('tempfile').TemporaryDirectory()))
        self.sweep = self.tmp / 'exp94'

    def uniform_pair(self, v1_s, ha_s, speed='2:60:tuned', **extra):
        rows = []
        for repeat in (1, 2, 3):
            rows.append(record('uniform', speed, V1, repeat, v1_s, **extra.get('v1', {})))
            rows.append(record('uniform', speed, HA, repeat, ha_s, **extra.get('ha', {})))
        write_sweep(self.sweep, rows)
        return exp94.cells(self.sweep)

    def test_prediction_1_holds_at_or_below_0_80(self):
        cells = self.uniform_pair(25.0, 20.0)  # 0.80 exactly
        got = exp94.prediction1(cells)
        row = next(r for r in got if r['condition'] == '2:60:tuned')
        self.assertAlmostEqual(row['ratio'], 0.80, places=4)
        self.assertEqual(row['verdict'], 'held')

    def test_prediction_1_between_the_bar_and_1_is_a_smaller_gain_not_a_pass(self):
        cells = self.uniform_pair(25.0, 22.0)  # 0.88
        row = next(r for r in exp94.prediction1(cells) if r['condition'] == '2:60:tuned')
        self.assertEqual(row['verdict'], 'gained less than the bar')

    def test_prediction_1_no_gain_is_a_failure(self):
        cells = self.uniform_pair(25.0, 25.5)  # 1.02
        row = next(r for r in exp94.prediction1(cells) if r['condition'] == '2:60:tuned')
        self.assertEqual(row['verdict'], 'failed')

    def test_prediction_3_treats_a_short_hot_difference_inside_the_noise_band_as_a_tie(self):
        """Rule 17: runs under 10 s differ by up to 7.11% between two runs of a cell, so 1.03 is inside it."""
        rows = []
        for repeat in (1, 2, 3):
            rows.append(record('hot', '2:60:tuned', V1, repeat, 5.0))
            rows.append(record('hot', '2:60:tuned', HA, repeat, 5.1))  # 1.02, and the runs are 5 s
        write_sweep(self.sweep, rows)
        row = next(r for r in exp94.prediction3(exp94.cells(self.sweep)) if r['condition'] == '2:60:tuned')
        self.assertEqual(row['verdict'], 'tie (inside the noise band)')
        self.assertAlmostEqual(row['band'], 0.0711, places=4)

    def test_prediction_3_uses_the_tighter_band_for_runs_of_ten_seconds_or_more(self):
        rows = []
        for repeat in (1, 2, 3):
            rows.append(record('hot', '2:0:default', V1, repeat, 20.0))
            rows.append(record('hot', '2:0:default', HA, repeat, 20.1))  # 1.005, inside 1.92%
        write_sweep(self.sweep, rows)
        row = next(r for r in exp94.prediction3(exp94.cells(self.sweep)) if r['condition'] == '2:0:default')
        self.assertAlmostEqual(row['band'], 0.0192, places=4)
        self.assertEqual(row['verdict'], 'tie (inside the noise band)')

    def test_prediction_3_a_hot_gain_past_the_band_is_reported_as_one(self):
        """The negative control: a gain in hot means the arm is measuring something other than the stall."""
        rows = []
        for repeat in (1, 2, 3):
            rows.append(record('hot', '2:0:default', V1, repeat, 20.0))
            rows.append(record('hot', '2:0:default', HA, repeat, 16.0))  # 0.80, far outside 1.92%
        write_sweep(self.sweep, rows)
        row = next(r for r in exp94.prediction3(exp94.cells(self.sweep)) if r['condition'] == '2:0:default')
        self.assertEqual(row['verdict'], 'gained in hot, which the mechanism does not predict')

    def test_prediction_4_asks_whether_hashing_ahead_adds_anything_once_bbr_is_in_play(self):
        rows = []
        for repeat in (1, 2, 3):
            rows.append(record('uniform', '2:20:default', BBR, repeat, 10.0))
            rows.append(record('uniform', '2:20:default', BBR_HA, repeat, 10.2))  # 1.02, within 1.05
        write_sweep(self.sweep, rows)
        row = next(r for r in exp94.prediction4(exp94.cells(self.sweep)) if r['condition'] == '2:20:default')
        self.assertEqual(row['verdict'], 'held (the two fixes address the same stall)')

    def test_prediction_4_a_clear_further_gain_is_reported_as_a_new_finding(self):
        rows = []
        for repeat in (1, 2, 3):
            rows.append(record('uniform', '2:20:default', BBR, repeat, 10.0))
            rows.append(record('uniform', '2:20:default', BBR_HA, repeat, 7.0))  # 0.70
        write_sweep(self.sweep, rows)
        row = next(r for r in exp94.prediction4(exp94.cells(self.sweep)) if r['condition'] == '2:20:default')
        self.assertEqual(row['verdict'], 'a further gain: the two fixes address different stalls')


class Mechanism(unittest.TestCase):
    """Prediction 6: the sender's idle leaf waits, which is 'the stream is not full' in measurable form."""

    def setUp(self):
        self.tmp = Path(self.enterContext(__import__('tempfile').TemporaryDirectory()))
        self.sweep = self.tmp / 'exp94'

    def test_the_idle_leaf_wait_must_fall_to_a_quarter_or_less(self):
        rows = []
        for repeat in (1, 2, 3):
            rows.append(record('uniform', '2:60:tuned', V1, repeat, 25.0, leaf_wait_idle_s=8.0))
            rows.append(record('uniform', '2:60:tuned', HA, repeat, 20.0, leaf_wait_idle_s=1.0))  # 0.125
        write_sweep(self.sweep, rows)
        row = next(r for r in exp94.prediction6(self.sweep) if r['condition'] == '2:60:tuned')
        self.assertAlmostEqual(row['ratio'], 0.125, places=4)
        self.assertEqual(row['verdict'], 'held')

    def test_an_idle_wait_that_does_not_fall_means_the_arm_is_not_doing_what_its_flag_claims(self):
        rows = []
        for repeat in (1, 2, 3):
            rows.append(record('uniform', '2:60:tuned', V1, repeat, 25.0, leaf_wait_idle_s=8.0))
            rows.append(record('uniform', '2:60:tuned', HA, repeat, 20.0, leaf_wait_idle_s=7.0))
        write_sweep(self.sweep, rows)
        row = next(r for r in exp94.prediction6(self.sweep) if r['condition'] == '2:60:tuned')
        self.assertEqual(row['verdict'], 'failed')

    def test_a_zero_baseline_is_reported_rather_than_divided_by(self):
        """If v1 never waited idle there was no stall to remove, and a ratio would be meaningless."""
        rows = []
        for repeat in (1, 2, 3):
            rows.append(record('uniform', '2:0:default', V1, repeat, 5.0, leaf_wait_idle_s=0.0))
            rows.append(record('uniform', '2:0:default', HA, repeat, 5.0, leaf_wait_idle_s=0.0))
        write_sweep(self.sweep, rows)
        row = next(r for r in exp94.prediction6(self.sweep) if r['condition'] == '2:0:default')
        self.assertIsNone(row['ratio'])
        self.assertEqual(row['verdict'], 'no idle wait to remove')


class SlowMode(unittest.TestCase):
    """Prediction 7, by EXP-23's own rule, so the counts are comparable with EXP-23's and EXP-24's."""

    def setUp(self):
        self.tmp = Path(self.enterContext(__import__('tempfile').TemporaryDirectory()))
        self.sweep = self.tmp / 'exp94'

    def test_a_run_is_slow_when_page_send_exceeds_twice_its_link_floor(self):
        # 600 MiB at 2 Gbit/s is 2.517 s of link time; EXP-23's bar is 2x that.
        floor = (600 << 20) * 8 / 2e9
        rows = [record('uniform', '2:20:default', V1, 1, 30.0, page_send_s=floor * 2.5),
                record('uniform', '2:20:default', V1, 2, 30.0, page_send_s=floor * 3.0),
                record('uniform', '2:20:default', V1, 3, 10.0, page_send_s=floor * 1.1),
                record('uniform', '2:20:default', HA, 1, 10.0, page_send_s=floor * 1.05),
                record('uniform', '2:20:default', HA, 2, 10.0, page_send_s=floor * 1.02),
                record('uniform', '2:20:default', HA, 3, 10.0, page_send_s=floor * 1.10)]
        write_sweep(self.sweep, rows, link_gbit=2.0)
        got = {(r['method'], r['condition']): r for r in exp94.prediction7(self.sweep)}
        v1 = got[(V1, '2:20:default')]
        self.assertEqual((v1['slow'], v1['runs']), (2, 3))
        self.assertEqual(v1['verdict'], 'held')          # at least 2 of 3, as EXP-23 found at 20 ms
        ha = got[(HA, '2:20:default')]
        self.assertEqual((ha['slow'], ha['runs']), (0, 3))
        self.assertEqual(ha['verdict'], 'held')          # 0 of 3, as BBR was
        # the interval is EXP-23's exact one, and is reported because 3 runs cannot pin a rate
        self.assertLess(ha['interval'][0], 0.01)
        self.assertGreater(ha['interval'][1], 0.5)

    def test_only_the_two_cells_the_registration_names_get_a_verdict(self):
        """Every other cell is a reading. Borrowing a verdict for an unpredicted cell is how a session ends up
        claiming more than it registered."""
        floor = (600 << 20) * 8 / 2e9
        rows = []
        for method in (V1, BBR, HA, BBR_HA):
            for cond in ('2:20:default', '2:60:tuned'):
                for repeat in (1, 2, 3):
                    rows.append(record('uniform', cond, method, repeat, 10.0, page_send_s=floor * 1.05))
        write_sweep(self.sweep, rows, link_gbit=2.0)
        got = {(r['method'], r['condition']): r for r in exp94.prediction7(self.sweep)}
        self.assertEqual(got[(V1, '2:20:default')]['expected'], 'at least 2 of 3')
        self.assertEqual(got[(HA, '2:20:default')]['expected'], '0 of 3')
        for key in ((V1, '2:60:tuned'), (HA, '2:60:tuned'), (BBR, '2:20:default'), (BBR_HA, '2:20:default')):
            with self.subTest(key=key):
                self.assertIsNone(got[key]['expected'])
                self.assertEqual(got[key]['verdict'], 'reported, not predicted')

    def test_the_scored_cells_go_where_out_says_not_into_docs(self):
        """A smoke run over synthetic records wrote fake numbers into docs/experiments/data once; --out exists
        so that cannot happen again.

        Asserted as "this call did not create or change the docs file", not as "the docs file is absent": once
        EXP-94 is actually scored the file exists for good, and the absence form would then fail for the one
        reason that is not a defect. The intent is unchanged - synthetic numbers must not reach docs.
        """
        docs = ROOT / 'docs' / 'experiments' / 'data' / 'exp94-cells.csv'
        before = docs.read_bytes() if docs.exists() else None
        rows = [record('uniform', '2:60:tuned', V1, repeat, 25.0) for repeat in (1, 2, 3)]
        rows += [record('uniform', '2:60:tuned', HA, repeat, 20.0) for repeat in (1, 2, 3)]
        write_sweep(self.sweep, rows)
        out, figdir = self.tmp / 'cells.csv', self.tmp / 'fig'
        figdir.mkdir()
        self.assertEqual(exp94.main(['--sweep', str(self.sweep), '--out', str(out), '--figdir', str(figdir)]), 0)
        self.assertTrue(out.exists())
        after = docs.read_bytes() if docs.exists() else None
        self.assertEqual(before, after, 'scoring with --out must not create or rewrite the docs CSV')

    def test_the_link_rates_are_read_the_same_way_exp23_reads_them(self):
        """The file reading is local (exp23.py leaves its trial files open), so it must agree with exp23's."""
        import exp23
        rows = [record('uniform', '2:20:default', V1, 1, 10.0)]
        write_sweep(self.sweep, rows, link_gbit=1.75)
        with warnings.catch_warnings():
            # exp23.link_rates leaves its trial files open; that is the reason exp94 has its own reader, and
            # the warning belongs to exp23, not here. Ignored only around this one deliberate call.
            warnings.simplefilter('ignore', ResourceWarning)
            theirs = exp23.link_rates(self.sweep)
        self.assertEqual(exp94.link_rates(self.sweep), theirs)
        self.assertEqual(exp94.link_rates(self.sweep), {'2:20:default': [1.75]})

    def test_the_bar_is_exp23s_two_times_not_a_new_one(self):
        """EXP-23 defines the slow mode as page_send_s above 2x the link floor; reusing it keeps EXP-94's
        counts comparable with EXP-23's and EXP-24's rather than introducing a second definition."""
        import exp23
        self.assertEqual(exp94.SLOW_MODE_FACTOR, 2.0)
        self.assertIn('above 2x is the slow mode', ' '.join((exp23.__doc__ or '').split()))


class Comparability(unittest.TestCase):
    """Prediction 2: rule 17's cross-session gate, against EXP-24's committed full times."""

    def setUp(self):
        self.tmp = Path(self.enterContext(__import__('tempfile').TemporaryDirectory()))
        self.sweep = self.tmp / 'exp94'

    def test_a_full_within_one_percent_of_exp24s_opens_the_cross_session_claim(self):
        earlier = {('hot', '2:60:tuned'): 11.596}
        rows = [record('hot', '2:60:tuned', 'full', repeat, 11.63) for repeat in (1, 2, 3)]
        write_sweep(self.sweep, rows)
        row = next(r for r in exp94.prediction2(exp94.cells(self.sweep), earlier) if r['condition'] == '2:60:tuned')
        self.assertLess(abs(row['ratio'] - 1.0), 0.01)
        self.assertEqual(row['verdict'], 'held')

    def test_a_full_outside_one_percent_closes_it_and_says_so(self):
        earlier = {('hot', '2:60:tuned'): 11.596}
        rows = [record('hot', '2:60:tuned', 'full', repeat, 13.00) for repeat in (1, 2, 3)]
        write_sweep(self.sweep, rows)
        row = next(r for r in exp94.prediction2(exp94.cells(self.sweep), earlier) if r['condition'] == '2:60:tuned')
        self.assertEqual(row['verdict'], 'failed')

    def test_exp24s_full_times_are_read_from_the_committed_csv_not_retyped(self):
        earlier = exp94.earlier_full()
        self.assertEqual(earlier[('hot', '2:60:tuned')], 11.596)
        self.assertEqual(earlier[('uniform', '2:120:default')], 87.612)


class SourceReads(unittest.TestCase):
    """Prediction 5: EXP-91's cost (the source reading the pages it sends twice) must not reappear."""

    def setUp(self):
        self.tmp = Path(self.enterContext(__import__('tempfile').TemporaryDirectory()))
        self.sweep = self.tmp / 'exp94'

    def test_source_reads_within_1_05_hold(self):
        rows = []
        for repeat in (1, 2, 3):
            rows.append(record('uniform', '2:60:tuned', V1, repeat, 25.0, source_read=2 << 30))
            rows.append(record('uniform', '2:60:tuned', HA, repeat, 20.0, source_read=int(2.02 * (1 << 30))))
        write_sweep(self.sweep, rows)
        row = next(r for r in exp94.prediction5(self.sweep) if r['condition'] == '2:60:tuned')
        self.assertEqual(row['verdict'], 'held')

    def test_a_second_read_of_the_pages_sent_shows_up_and_fails(self):
        rows = []
        for repeat in (1, 2, 3):
            rows.append(record('uniform', '2:60:tuned', V1, repeat, 25.0, source_read=2 << 30))
            rows.append(record('uniform', '2:60:tuned', HA, repeat, 20.0, source_read=4 << 30))  # 2.0x
        write_sweep(self.sweep, rows)
        row = next(r for r in exp94.prediction5(self.sweep) if r['condition'] == '2:60:tuned')
        self.assertEqual(row['verdict'], 'failed')


class Bars(unittest.TestCase):
    def test_every_bar_is_the_one_the_registration_states(self):
        spec = (ROOT / 'docs' / 'experiments' / 'EXP-94-dr-hash-ahead.md').read_text(encoding='utf-8')
        for value, where in ((exp94.P1_BAR, '0.80'), (exp94.P3_BAR, '1.03'), (exp94.P4_BAR, '1.05'),
                             (exp94.P5_BAR, '1.05'), (exp94.P6_BAR, '0.25'), (exp94.SLOW_MODE_FACTOR, '2.0')):
            with self.subTest(bar=where):
                self.assertEqual(f'{value:g}'.rstrip('0').rstrip('.'), where.rstrip('0').rstrip('.'))
                self.assertIn(where, spec)
        self.assertEqual(exp94.COMPARABILITY_BAR, 0.01)
        self.assertEqual((exp94.NOISE_LONG, exp94.NOISE_SHORT, exp94.NOISE_SHORT_SECONDS),
                         (0.0192, 0.0711, 10.0))


class CongestionControlGate(unittest.TestCase):
    """A BBR arm that silently ran cubic must not be read as a BBR arm.

    pgdelta asks for BBR with setsockopt(TCP_CONGESTION) and **does not fail if the host refuses**: an
    unprivileged process may choose only what net.ipv4.tcp_allowed_congestion_control lists, and
    transport.set_congestion_control then records {'effective': 'cubic', 'error': ...} and the transfer goes on
    (pgdelta/transport.py). run_exp04.py checks nothing about this - grep finds no mention of congestion
    control in it - so if EXP-94's one host-setting step is forgotten or does not stick, both BBR arms run
    cubic, every run passes, and prediction 4 compares cubic-with-hashing-ahead against cubic, which is
    prediction 1 again under another name. EXP-24 caught this by reading the statistics afterwards; here the
    scorer refuses instead of trusting.
    """

    def setUp(self):
        self.tmp = Path(self.enterContext(__import__('tempfile').TemporaryDirectory()))
        self.sweep = self.tmp / 'exp94'

    def write_stats(self, trial, name, requested, effective, error=None):
        directory = self.sweep / 'logs' / trial / 'source'
        directory.mkdir(parents=True, exist_ok=True)
        body = {'congestion_control': {'requested': requested, 'effective': effective, 'error': error}}
        # the agent prefixes its logs, as run_exp04 captures them; the reader finds the first brace
        (directory / f'{name}-stats.json.log').write_text('agent: ran' + chr(10) + json.dumps(body), encoding='utf-8')

    def test_every_run_that_asked_for_bbr_getting_it_passes_the_gate(self):
        self.write_stats('00-uniform', 'r1-2g-20ms-default-pgdelta_direct_inplace_bbr', 'bbr', 'bbr')
        self.write_stats('00-uniform', 'r1-2g-20ms-default-pgdelta_direct_inplace_bbr_ha', 'bbr', 'bbr')
        self.write_stats('00-uniform', 'r1-2g-20ms-default-pgdelta_direct_inplace', None, 'cubic')
        gate = exp94.congestion_gate(self.sweep)
        self.assertEqual((gate['requested'], gate['effective']), (2, 2))
        self.assertTrue(gate['ok'])
        self.assertEqual(gate['bad'], [])

    def test_a_run_that_asked_and_got_cubic_fails_the_gate_and_is_named(self):
        self.write_stats('00-uniform', 'r1-2g-20ms-default-pgdelta_direct_inplace_bbr', 'bbr', 'cubic',
                         error='[Errno 1] Operation not permitted')
        self.write_stats('00-uniform', 'r2-2g-20ms-default-pgdelta_direct_inplace_bbr', 'bbr', 'bbr')
        gate = exp94.congestion_gate(self.sweep)
        self.assertEqual((gate['requested'], gate['effective']), (2, 1))
        self.assertFalse(gate['ok'])
        self.assertEqual(len(gate['bad']), 1)
        self.assertIn('r1-2g-20ms-default-pgdelta_direct_inplace_bbr', gate['bad'][0]['run'])
        self.assertEqual(gate['bad'][0]['effective'], 'cubic')
        self.assertIn('not permitted', gate['bad'][0]['error'])

    def test_a_session_with_no_bbr_statistics_at_all_is_unknown_not_ok(self):
        """No statistics is not evidence that BBR worked; it is evidence of nothing."""
        gate = exp94.congestion_gate(self.sweep)
        self.assertEqual(gate['requested'], 0)
        self.assertFalse(gate['ok'])
        self.assertIn('no run asked for BBR', gate['reason'])

    def test_prediction_4_is_refused_when_the_gate_fails(self):
        """Otherwise it silently becomes prediction 1 under another name."""
        rows = []
        for repeat in (1, 2, 3):
            rows.append(record('uniform', '2:20:default', BBR, repeat, 10.0))
            rows.append(record('uniform', '2:20:default', BBR_HA, repeat, 10.2))
        write_sweep(self.sweep, rows)
        self.write_stats('00-uniform', 'r1-2g-20ms-default-pgdelta_direct_inplace_bbr', 'bbr', 'cubic')
        row = next(r for r in exp94.prediction4(exp94.cells(self.sweep), gate=exp94.congestion_gate(self.sweep))
                   if r['condition'] == '2:20:default')
        self.assertEqual(row['verdict'], 'not readable: a BBR arm did not get BBR')

    def test_prediction_4_reads_normally_when_the_gate_passes(self):
        rows = []
        for repeat in (1, 2, 3):
            rows.append(record('uniform', '2:20:default', BBR, repeat, 10.0))
            rows.append(record('uniform', '2:20:default', BBR_HA, repeat, 10.2))
        write_sweep(self.sweep, rows)
        self.write_stats('00-uniform', 'r1-2g-20ms-default-pgdelta_direct_inplace_bbr', 'bbr', 'bbr')
        row = next(r for r in exp94.prediction4(exp94.cells(self.sweep), gate=exp94.congestion_gate(self.sweep))
                   if r['condition'] == '2:20:default')
        self.assertEqual(row['verdict'], 'held (the two fixes address the same stall)')

    def test_prediction_1_does_not_depend_on_the_gate(self):
        """P1 is cubic against cubic-with-hashing-ahead: BBR's permissions have nothing to do with it."""
        rows = []
        for repeat in (1, 2, 3):
            rows.append(record('uniform', '2:60:tuned', V1, repeat, 25.0))
            rows.append(record('uniform', '2:60:tuned', HA, repeat, 20.0))
        write_sweep(self.sweep, rows)
        row = next(r for r in exp94.prediction1(exp94.cells(self.sweep)) if r['condition'] == '2:60:tuned')
        self.assertEqual(row['verdict'], 'held')


class DecidingCells(unittest.TestCase):
    """Prediction 1 is judged where cubic is in the slow mode in THIS session, not where an earlier one was.

    EXP-20 measured uniform R_fast 2.578 at 2:60:default and EXP-24 measured 1.645 in the same cell with the
    same arm, because cubic fell into its slow mode there in one session and not the other. A cell list taken
    from either session would be the wrong list about half the time, so the registration selects by prediction
    7's mechanical rule applied to this session's own runs. Selecting by rule is not post hoc; selecting by
    outcome would be, and this must not do that - which is why the selector is the slow-mode count and never
    the time ratio.
    """

    def setUp(self):
        self.tmp = Path(self.enterContext(__import__('tempfile').TemporaryDirectory()))
        self.sweep = self.tmp / 'exp94'
        self.floor = (600 << 20) * 8 / 2e9

    def build(self, slow_at):
        """v1 slow in 3 of 3 at the conditions in `slow_at`, fast elsewhere; _ha always a 0.70 gain."""
        rows = []
        for cond in ('2:20:default', '2:60:default', '2:60:tuned'):
            for repeat in (1, 2, 3):
                factor = 2.6 if cond in slow_at else 1.05
                rows.append(record('uniform', cond, V1, repeat, 25.0, page_send_s=self.floor * factor))
                rows.append(record('uniform', cond, HA, repeat, 17.5, page_send_s=self.floor * 1.05))
        write_sweep(self.sweep, rows, link_gbit=2.0)

    def test_the_slow_mode_cells_are_read_off_prediction_sevens_rule(self):
        self.build({'2:20:default', '2:60:tuned'})
        self.assertEqual(exp94.slow_mode_cells(self.sweep), {'2:20:default', '2:60:tuned'})

    def test_a_cell_where_cubic_never_slows_is_reported_not_judged(self):
        self.build({'2:20:default'})
        rows = {r['condition']: r for r in exp94.prediction1(exp94.cells(self.sweep),
                                                             slow_cells=exp94.slow_mode_cells(self.sweep))}
        self.assertTrue(rows['2:20:default']['deciding'])
        self.assertEqual(rows['2:20:default']['verdict'], 'held')
        for cond in ('2:60:default', '2:60:tuned'):
            with self.subTest(condition=cond):
                self.assertFalse(rows[cond]['deciding'])
                self.assertEqual(rows[cond]['verdict'], 'reported (cubic not in the slow mode here)')
                self.assertAlmostEqual(rows[cond]['ratio'], 0.70, places=4)  # the ratio is still shown

    def test_a_majority_of_runs_slow_makes_the_cell_deciding_not_a_single_run(self):
        rows = []
        for repeat, factor in ((1, 2.6), (2, 1.05), (3, 1.05)):   # 1 of 3 slow: not a slow-mode cell
            rows.append(record('uniform', '2:60:tuned', V1, repeat, 25.0, page_send_s=self.floor * factor))
            rows.append(record('uniform', '2:60:tuned', HA, repeat, 17.5, page_send_s=self.floor * 1.05))
        write_sweep(self.sweep, rows, link_gbit=2.0)
        self.assertEqual(exp94.slow_mode_cells(self.sweep), set())
        rows2 = []
        for repeat, factor in ((1, 2.6), (2, 2.6), (3, 1.05)):    # 2 of 3 slow: it is
            rows2.append(record('uniform', '2:60:tuned', V1, repeat, 25.0, page_send_s=self.floor * factor))
            rows2.append(record('uniform', '2:60:tuned', HA, repeat, 17.5, page_send_s=self.floor * 1.05))
        other = self.tmp / 'other'
        write_sweep(other, rows2, link_gbit=2.0)
        self.assertEqual(exp94.slow_mode_cells(other), {'2:60:tuned'})

    def test_without_a_cell_set_every_cell_is_judged_as_before(self):
        """The default stays what the earlier tests pin, so the change cannot quietly reinterpret them."""
        self.build({'2:20:default'})
        rows = {r['condition']: r for r in exp94.prediction1(exp94.cells(self.sweep))}
        for cond in ('2:20:default', '2:60:default', '2:60:tuned'):
            self.assertEqual(rows[cond]['verdict'], 'held')

    def test_the_report_passes_this_sessions_slow_cells_into_prediction_one(self):
        self.build({'2:20:default'})
        got = exp94.report(self.sweep)
        rows = {r['condition']: r for r in got['1']}
        self.assertTrue(rows['2:20:default']['deciding'])
        self.assertFalse(rows['2:60:tuned']['deciding'])
        self.assertEqual(got['slow_cells'], {'2:20:default'})


if __name__ == '__main__':
    unittest.main()
