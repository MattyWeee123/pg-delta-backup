"""EXP-12's blocks together: the harness (the load, the arms, the oracle) and the analysis.

- the specification's run lines, read from docs/experiments/EXP-12-source-slowdown.md exactly as written there
  (R_BUSY=0, R_MOD=0, NOISE=4, L_MS=10), split as a POSIX shell splits them: the harness's through remote.py start
  and run_exp04.parse_args, the analysis's through its own parser; they plan the registered 117 runs, and execute
  refuses the zero rates before it touches anything;
- the network README's "Methods for T10a" names every option of run_exp04's EXP-12 group, of the run lines and of
  the analysis;
- the stubbed harness's whole trial (test_exp12_load's LoadStub) with its load at the rate it was asked for and
  pause2's stop in its logs, read by the analysis: every loaded run has numbers, pause2's stall lands in its own
  window and nowhere else, and the script writes its data and figures from the harness's records.

The analysis's parts need the analysis venv's numpy and matplotlib and are skipped without them.
"""

import csv
import io
import re
import shlex
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from experiments.network import measure, remote, run_exp04

import test_exp12_load
from test_exp12_load import COMMIT_RATE, EPOCH, LoadStub

try:
    import matplotlib  # noqa: F401
    import numpy  # noqa: F401
except ImportError:  # the analysis scripts need the analysis venv
    matplotlib = None
if matplotlib is not None:
    sys.path.append(str(Path(__file__).resolve().parents[1] / 'analysis'))
    import exp12
    import plotting

needs_venv = unittest.skipUnless(matplotlib is not None, 'needs the analysis venv (numpy, matplotlib)')
ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / 'docs' / 'experiments' / 'EXP-12-source-slowdown.md'
README = ROOT / 'experiments' / 'network' / 'README.md'


def spec_run_lines() -> tuple[dict, dict, dict]:
    """(variables, {invocation name: run_exp04's arguments}, {script: its arguments}) from EXP-12's "Run lines" block,
    exactly as written: its assignments kept, its $NAMEs expanded, each line split as a POSIX shell would (comments
    included), then the harness's as remote.py start hands them to run_exp04.py."""
    block = re.search(r'\*\*Run lines\*\*.*?```sh\n(.*?)```', SPEC.read_text(), re.S).group(1)
    variables, harness, scripts = {}, {}, {}
    for line in block.replace('\\\n', ' ').splitlines():
        words = shlex.split(line, comments=True)  # '#' starts a comment, as in a POSIX shell
        if words and all(re.match(r'^\w+=', word) for word in words):
            for word in words:  # R_BUSY=0; R_MOD=0: assignments, ';' ending each
                name, value = word.rstrip(';').split('=', 1)
                variables[name] = value
            continue
        words = shlex.split(re.sub(r'\$(\w+)', lambda m: variables[m.group(1)], line), comments=True)
        if words[:2] == ['python3', 'experiments/network/remote.py']:
            args = remote.build_parser().parse_args(words[2:])
            harness[args.name] = args.args[1:] if args.args[:1] == ['--'] else args.args
        elif words[:2] == ['.venv/bin/python', 'analysis/exp12.py']:
            scripts['analysis/exp12.py'] = words[2:]
    return variables, harness, scripts


def harness_args(tmp='/nonexistent/exp12'):
    _, harness, _ = spec_run_lines()
    return run_exp04.parse_args(['--source-ip', '192.0.2.2', '--out', f'{tmp}/out', '--work-dir', f'{tmp}/work',
                                 *harness['exp12-hot']])


class RunLines(unittest.TestCase):
    """EXP-12's "Run plan and budget": its run lines, exactly as written there (the placeholders' values included)."""

    def test_the_block_has_one_harness_line_and_the_analysiss(self):
        variables, harness, scripts = spec_run_lines()
        self.assertEqual(variables, {'R_BUSY': '0', 'R_MOD': '0', 'NOISE': '4', 'L_MS': '10'})
        self.assertEqual(sorted(harness), ['exp12-hot'])
        self.assertEqual(sorted(scripts), ['analysis/exp12.py'])

    def test_the_harness_line_parses_as_written(self):
        args = harness_args()
        methods = run_exp04.method_list(args.methods)
        self.assertEqual(methods, ['none', 'capture', 'bracket', 'full', 'pgdelta', 'v1proxy', 'v1proxy_s1'])
        self.assertEqual([m for m in methods if m not in measure.ALL_METHODS], [])
        self.assertEqual((args.patterns, args.speeds, args.repeats, args.order_seed), ('hot', '0.5,2,unthrottled',
                                                                                       5, 121))
        self.assertEqual((args.load, args.load_rate, args.moderate_rate), ('bank', 0, 0))
        self.assertEqual((args.load_clients, args.load_threads, args.load_warmup_s), (32, 4, 120))
        self.assertEqual((args.settle_s, args.settle_max_s, args.tail_s, args.none_window_s), (15, 60, 15, 60))
        self.assertEqual((args.source_cache, args.snapshot, args.static_digest), ('evict', True, 'first_repeat'))
        self.assertEqual(run_exp04.method_list(args.controls), ['pause2', 'burn4'])
        self.assertEqual((args.manifest_checksums, args.dirty_noise_mib), ('CRC32C', 4.0))
        self.assertEqual((args.warm_source_repeats, args.warm_source_speeds, args.warm_source_methods),
                         (5, '2', 'full,v1proxy'))
        self.assertEqual((args.moderate_repeats, args.moderate_speeds, args.moderate_methods,
                          args.moderate_method_speeds), (3, '2,unthrottled', 'none,full,v1proxy,v1proxy_s1',
                                                         ['none+v1proxy_s1=2']))
        self.assertEqual((args.calibration_repeats, args.calibration_speed),  (2, '2'))
        self.assertEqual(run_exp04.method_list(args.calibration_methods),
                         ['full', 'capture', 'pgdelta', 'v1proxy', 'v1proxy_s1', 'pgdelta_direct_inplace', 'burn4'])
        # Every check parse_args makes, and execute makes again, passes the line's shape.
        self.assertEqual(run_exp04.load_problems(args, methods), [])
        self.assertEqual(run_exp04.block_problems(args, methods), [])
        self.assertIsNone(run_exp04.checksum_problem(methods, args.manifest_checksums, args.load))

    def test_as_written_it_plans_the_registered_117_runs(self):
        args = harness_args()
        speeds = measure.parse_speeds(args.speeds)
        methods = run_exp04.method_list(args.methods)
        limited = measure.parse_method_speeds(args.method_speeds, methods, speeds)
        busy = {speed['label']: measure.methods_at(methods, limited, speed['label']) for speed in speeds}
        self.assertEqual({label: len(arms) for label, arms in busy.items()}, {'0.5g': 4, '2g': 7, 'unthrottled': 3})
        moderate_methods = run_exp04.method_list(args.moderate_methods)
        moderate_speeds = measure.parse_speeds(args.moderate_speeds)
        moderate_limited = measure.parse_method_speeds(args.moderate_method_speeds, moderate_methods, moderate_speeds)
        counts = {
            'busy': args.repeats * sum(len(arms) for arms in busy.values()),
            'warm': args.warm_source_repeats * len(run_exp04.method_list(args.warm_source_methods))
            * len(measure.parse_speeds(args.warm_source_speeds)),
            'moderate': args.moderate_repeats * sum(len(measure.methods_at(moderate_methods, moderate_limited,
                                                                           speed['label']))
                                                    for speed in moderate_speeds),
            'calibration': args.calibration_repeats * len(run_exp04.method_list(args.calibration_methods)),
            # pause2 and burn4 at the busy block's start and end, and pause2 at the moderate block's start.
            'controls': 2 * len(run_exp04.method_list(args.controls)) + 1}
        self.assertEqual(counts, {'busy': 70, 'warm': 10, 'moderate': 18, 'calibration': 14, 'controls': 5})
        self.assertEqual(sum(counts.values()), 117)

    def test_as_written_execute_refuses_the_zero_rates_before_it_touches_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = run_exp04.Orchestrator(harness_args(tmp))
            self.addCleanup(orchestrator.pool.shutdown)
            with mock.patch.object(run_exp04.subprocess, 'run') as run, \
                    mock.patch.object(run_exp04.subprocess, 'Popen') as popen, \
                    self.assertRaises(SystemExit) as refused:
                orchestrator.execute()
            self.assertIn("the smoke run's R_busy and R_mod", str(refused.exception))
            self.assertFalse(run.called or popen.called)
            self.assertFalse(Path(tmp, 'out').exists() or Path(tmp, 'work').exists())

    @needs_venv
    def test_the_analysis_line_parses_as_written(self):
        _, _, scripts = spec_run_lines()
        found = exp12.build_parser().parse_args(scripts['analysis/exp12.py'])
        self.assertEqual((found.sweeps, found.late_limit_ms, found.null_resamples, found.null_seed,
                          found.client_wait_share), (['results/exp04/exp12-hot'], 10.0, 10_000, 121, 0.25))


def readme_section(title: str) -> str:
    text = README.read_text()
    start = text.index(f'## {title}\n')
    following = text.find('\n## ', start + 1)
    return text[start:following if following != -1 else len(text)]


def exp12_options() -> list[str]:
    """run_exp04's EXP-12 (T10a) argument group, from its --help."""
    out = io.StringIO()
    with redirect_stdout(out), mock.patch('sys.stderr', io.StringIO()):
        try:
            run_exp04.parse_args(['--help'])
        except SystemExit:
            pass
    text = out.getvalue()
    group = text[text.index('EXP-12 (T10a):'):]
    return sorted(set(re.findall(r'^\s{2}(--[a-z][a-z0-9-]*)', group, re.M)))


class Readme(unittest.TestCase):
    """The network README's "Methods for T10a" describes what the run lines and the parsers take."""

    def test_it_names_every_option_of_the_exp12_group(self):
        section = readme_section('Methods for T10a')
        options = exp12_options()
        self.assertIn('--oracle-controls', options)
        self.assertEqual([option for option in options if f'`{option}' not in section], [])

    def test_every_option_of_the_harness_line_is_described(self):
        _, harness, _ = spec_run_lines()
        named = sorted({word for word in harness['exp12-hot'] if word.startswith('--')})
        text = README.read_text()
        self.assertEqual([option for option in named if f'`{option}' not in text], [])

    @needs_venv
    def test_it_names_every_option_of_the_analysis(self):
        section = readme_section('Methods for T10a')
        options = [action.option_strings[0] for action in exp12.build_parser()._actions
                   if action.option_strings and action.option_strings[0] != '-h']
        self.assertEqual([option for option in options if f'`{option}' not in section], [])

    @needs_venv
    def test_its_analysis_lines_parse(self):
        # The section's sh blocks, expanded and split as a POSIX shell would; the smoke run's analysis writes into a
        # scratch directory under results/ (its item 16), never into docs/.
        variables, found = {}, []
        for block in re.findall(r'```sh\n(.*?)```', readme_section('Methods for T10a'), re.S):
            for line in block.replace('\\\n', ' ').splitlines():
                words = shlex.split(line, comments=True)
                if words and all(re.match(r'^\w+=', word) for word in words):
                    variables.update(word.rstrip(';').split('=', 1) for word in words)
                    continue
                words = shlex.split(re.sub(r'\$(\w+)', lambda m: variables[m.group(1)], line), comments=True)
                if words[:2] == ['.venv/bin/python', 'analysis/exp12.py']:
                    found.append(exp12.build_parser().parse_args(words[2:]))
        self.assertEqual([(args.sweeps, args.tag) for args in found], [(['results/exp04/exp12-hot'], ''),
                                                                      (['results/exp04/exp12-smoke'], '-smoke')])
        self.assertEqual((found[0].late_limit_ms, found[0].null_resamples, found[0].null_seed,
                          found[0].client_wait_share), (10.0, 10_000, 121, 0.25))
        self.assertTrue(found[1].datadir.startswith('results/') and found[1].figdir.startswith('results/'))


class PausingLoad(LoadStub):
    """LoadStub whose load logs hold pause2's SIGSTOP: every transaction scheduled while pgbench is stopped completes
    just after the SIGCONT, as pgbench's open loop catches up (burn4 runs 2 s too, but stops nothing)."""

    def __init__(self, tmp, clock, extra_args=()):
        super().__init__(tmp, clock, extra_args)
        self.pauses: list[tuple[float, float]] = []

    def _source_agent(self, *args, timeout=600):
        result = super()._source_agent(*args, timeout=timeout)
        if args[0] == 'load-pause':
            self.pauses.append((EPOCH + self.clock.now - 2.0, EPOCH + self.clock.now))
        return result

    def fetch_load_logs(self):
        result = super().fetch_load_logs()
        pauses = [(int(round(stop * 1e6)), int(round(go * 1e6))) for stop, go in self.pauses]
        for path in self.load_logs.glob('*.4000'):
            lines = []
            for line in path.read_text().splitlines():
                client, number, latency, script, end, lag = measure.parse_pgbench_line(line)
                scheduled = end - latency
                for stop, go in pauses:
                    if stop <= scheduled < go:
                        end = go + 200
                lines.append((end, client, number, f'{client} {number} {end - scheduled} {script} '
                                                   f'{end // 1_000_000} {end % 1_000_000} {lag}\n'))
            path.write_text(''.join(text for *_, text in sorted(lines)))
        return result


@needs_venv
class StubbedSession(unittest.TestCase):
    """The stubbed harness's whole trial, its load at the rate it was asked for (the stub's 2 clients at COMMIT_RATE
    each), read by the analysis without loosening its log coverage."""

    RATE = int(2 * COMMIT_RATE)

    def setUp(self):
        stub = test_exp12_load.WholeTrial('test_the_trials_order')
        stub.setUp()
        self.addCleanup(stub.doCleanups)
        self.orchestrator, self.trial = stub.trial(['--load-rate', str(self.RATE), '--moderate-rate', str(self.RATE)],
                                                   cls=PausingLoad)
        self.out = Path(self.orchestrator.out)

    def session(self):
        runs, trials, meta = exp12.load(self.out)
        return exp12.Session(runs, trials, [meta], sweeps=[self.out], resamples=200)

    def test_pause2s_stall_lands_in_its_own_window_and_nowhere_else(self):
        self.assertEqual(self.trial['status'], 'pass', self.trial.get('error'))
        self.assertEqual(len(self.orchestrator.pauses), 3)  # the busy block's start and end, the moderate's start
        S = self.session()
        loaded = [r for r in S.runs if r['block'] in exp12.LOADED_BLOCKS]
        self.assertEqual(len(loaded), len([r for r in self.orchestrator.records if r['block'] != 'calibration']))
        self.assertEqual([(r['method'], r.get('log_gap')) for r in loaded if r['stats'] is None], [])
        pauses = [r for r in loaded if r['method'] == 'pause2']
        self.assertEqual(len(pauses), 3)
        for r in pauses:
            # Two whole 1-s buckets without a completion, the backlog all done in the next: W just under 2 s, D = 1.
            self.assertTrue(1.8 <= r['stats']['W'] <= 2.0, r['stats']['W'])
            self.assertEqual(r['stats']['D'], 1.0)
        for r in loaded:
            if r['method'] != 'pause2':
                self.assertLessEqual(r['stats']['W'], 0.05, (r['block'], r['speed'], r['method']))
                self.assertLessEqual(r['stats']['D'], 0.05, (r['block'], r['speed'], r['method']))

    def test_the_script_writes_its_data_and_figures_from_the_harnesss_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, figures = io.StringIO(), Path(tmp) / 'figures'
            with mock.patch.object(plotting, 'FIGURES', figures), redirect_stdout(out):
                code = exp12.main(['--sweeps', str(self.out), '--datadir', str(Path(tmp) / 'data'), '--figdir',
                                   str(figures), '--null-resamples', '200'])
            self.assertEqual(code, 0)
            with open(Path(tmp) / 'data' / 'exp12-runs.csv', newline='') as f:
                rows = list(csv.DictReader(f))
            for name in ('exp12-timeseries.svg', 'exp12-loss.svg'):
                self.assertTrue((figures / name).exists())
        text = out.getvalue()
        self.assertEqual(len(rows), len(self.orchestrator.records))
        loaded = [row for row in rows if row['block'] != 'calibration']
        # The columns the harness leaves empty are the analysis's, from each record's own window.
        for column in ('window_buckets', 'worst_drop', 'work_lost_s', 'lat_p99_ms', 'lat_p99_worst5s_ms'):
            self.assertTrue(all(row[column] != '' for row in loaded), column)
        self.assertEqual(sorted(round(float(row['worst_drop']), 2) for row in loaded if row['method'] == 'pause2'),
                         [1.0, 1.0, 1.0])
        self.assertIn('**Decision 1:', text)
        self.assertIn('**A2** (busy block)', text)


if __name__ == '__main__':
    unittest.main()
