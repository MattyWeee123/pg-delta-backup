"""Tests for EXP-12's drift check (analysis/exp12_drift.py).

- The verdict: the busy load's first busy repeat against the warm block, the moderate load's first repeat against its
  last, the controls left out, each limit just under and just over, and "not judged" when a group is missing.
- The per-minute view on a small log with known answers: whole minutes only, load-only transactions outside the
  copies' windows (`none`'s window kept), percentiles within the histogram's resolution, a failed line read through
  the slow path.

They need the analysis venv's numpy and matplotlib and are skipped without them.
"""

import csv
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

try:
    import matplotlib  # noqa: F401
    import numpy as np
except ImportError:  # the analysis scripts need the analysis venv
    matplotlib = np = None
if matplotlib is not None:
    sys.path.append(str(Path(__file__).resolve().parents[1] / 'analysis'))
    import exp12_drift

FIELDS = ['block', 'block_repeat', 'control_slot', 'method', 't0_utc', 't1_utc', 'settle_p50_ms', 'settle_p99_ms',
          'settle_gate_passed']


def row(block, repeat, p50, p99, method='full', control='', t0=None, t1=None):
    return {'block': block, 'block_repeat': '' if repeat is None else str(repeat), 'control_slot': control,
            'method': method, 't0_utc': '' if t0 is None else str(t0), 't1_utc': '' if t1 is None else str(t1),
            'settle_p50_ms': str(p50), 'settle_p99_ms': str(p99), 'settle_gate_passed': 'True'}


def write_rows(folder, rows):
    path = Path(folder) / 'runs.csv'
    with open(path, 'w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def session(warm_p50, warm_p99, moderate_last=(0.50, 1.0)):
    rows = [row('busy', 0, 0.70, 1.5) for _ in range(14)]
    rows += [row('busy', 4, 0.90, 9.0) for _ in range(14)]  # a later busy repeat: reported, not judged
    rows += [row('busy', None, 5.0, 900.0, method='pause2', control='busy_start')]
    rows += [row('warm', r, warm_p50, warm_p99) for r in range(5) for _ in range(2)]
    rows += [row('moderate', 0, 0.50, 1.0) for _ in range(6)]
    rows += [row('moderate', 2, *moderate_last) for _ in range(6)]
    rows += [row('moderate', None, 5.0, 900.0, method='pause2', control='moderate_start')]
    rows += [row('calibration', 0, '', '', method='full')]
    return rows


@unittest.skipIf(matplotlib is None, 'needs the analysis venv (numpy, matplotlib)')
class VerdictTests(unittest.TestCase):
    def verdicts(self, rows):
        with tempfile.TemporaryDirectory() as folder:
            arms, _ = exp12_drift.read_runs(write_rows(folder, rows))
        return exp12_drift.judge(arms)

    def test_within_both_limits_is_no_drift(self):
        out = self.verdicts(session(0.76, 2.9))
        busy = out['busy']
        self.assertFalse(busy['drift'])
        self.assertEqual((busy['early']['settles'], busy['late']['settles']), (14, 10))
        self.assertAlmostEqual(busy['p50_ratio'], 0.76 / 0.70)
        self.assertAlmostEqual(busy['p99_ratio'], 2.9 / 1.5)
        self.assertFalse(out['moderate']['drift'])
        self.assertEqual((out['moderate']['early']['settles'], out['moderate']['late']['settles']), (6, 6))

    def test_p50_over_its_limit_is_drift(self):
        self.assertTrue(self.verdicts(session(0.78, 1.5))['busy']['drift'])

    def test_p99_over_its_limit_is_drift(self):
        self.assertTrue(self.verdicts(session(0.70, 3.1))['busy']['drift'])

    def test_moderate_judged_on_its_first_and_last_repeats(self):
        out = self.verdicts(session(0.70, 1.5, moderate_last=(0.56, 1.0)))
        self.assertTrue(out['moderate']['drift'])
        self.assertFalse(out['busy']['drift'])

    def test_controls_left_out(self):
        out = self.verdicts(session(0.70, 1.5))
        self.assertEqual(out['busy']['early']['p99'], 1.5)
        self.assertEqual(out['moderate']['early']['p99'], 1.0)

    def test_a_missing_group_is_not_judged(self):
        rows = [r for r in session(0.70, 1.5) if r['block'] != 'warm']
        out = self.verdicts(rows)
        self.assertIsNone(out['busy']['drift'])
        self.assertFalse(out['moderate']['drift'])


@unittest.skipIf(matplotlib is None, 'needs the analysis venv (numpy, matplotlib)')
class MinuteViewTests(unittest.TestCase):
    START = 1_800_000_000

    def log(self, folder, lines):
        path = Path(folder) / 'busy.1'
        path.write_text(''.join(lines))
        return path

    @staticmethod
    def line(client, latency_us, end_s):
        whole, part = divmod(round(end_s * 1_000_000), 1_000_000)
        return f'{client} 0 {latency_us} 0 {whole} {part} 0\n'

    def test_whole_minutes_and_load_only(self):
        lines = []
        for second in range(150):  # 2.5 minutes: the last half minute isn't whole
            end = self.START + second + 0.5
            inside = 70 <= second < 80
            lines += [self.line(c, 50_000 if inside else 1_000 + 10 * c, end) for c in range(10)]
        lines.insert(5, '3 7 failed 0 1800000000 900000 0\n')
        windows = [(int((self.START + 70) * 1e6), int((self.START + 80) * 1e6))]
        with tempfile.TemporaryDirectory() as folder:
            minutes = exp12_drift.minute_view([self.log(folder, lines)], self.START, self.START + 150, windows)
        self.assertEqual([m['minute'] for m in minutes], [0, 1])
        self.assertEqual([m['transactions'] for m in minutes], [600, 600])
        self.assertEqual([m['load_only'] for m in minutes], [600, 500])
        # Load-only latencies are 1,000-1,090 us in every minute: p50 1,040 or 1,050, p99 1,090.
        for m in minutes:
            self.assertTrue(1.040 * 0.994 <= m['p50_only_ms'] <= 1.050 * 1.006, m)
            self.assertTrue(1.090 * 0.994 <= m['p99_only_ms'] <= 1.090 * 1.006, m)
        # With the copy's 100 transactions at 50 ms, minute 1's p99 over all is 50 ms.
        self.assertAlmostEqual(minutes[1]['p99_ms'], 50.0, delta=50.0 * 0.006)

    def test_none_window_is_load_only(self):
        every = [{'block': 'busy', 'method': 'none', 't0': 10.0, 't1': 70.0},
                 {'block': 'busy', 'method': 'full', 't0': 100.0, 't1': 110.0},
                 {'block': 'calibration', 'method': 'full', 't0': 200.0, 't1': 210.0}]
        self.assertEqual(exp12_drift.windows_of(every, ['busy']), [(100_000_000, 125_000_000)])

    def test_merged(self):
        starts, stops = exp12_drift.merged([(5, 9), (1, 3), (2, 4), (9, 12)])
        self.assertEqual(starts.tolist(), [1, 5])
        self.assertEqual(stops.tolist(), [4, 12])


@unittest.skipIf(matplotlib is None, 'needs the analysis venv (numpy, matplotlib)')
class ScriptTests(unittest.TestCase):
    def test_runs_without_logs(self):
        with tempfile.TemporaryDirectory() as folder:
            path = write_rows(folder, session(0.76, 2.9))
            out = io.StringIO()
            with redirect_stdout(out):
                exp12_drift.main(['--sweep', folder, '--runs', str(path), '--no-logs', '--figdir', folder])
        text = out.getvalue()
        self.assertIn('the busy load: no drift', text)
        self.assertIn('the moderate load: no drift', text)


if __name__ == '__main__':
    unittest.main()
