"""EXP-13's blocks together: the harness (the emulated WAN, the conditions, the arms), pgdelta's switches and
timers, and the analysis.

- the specification's run lines, read from docs/experiments/EXP-13-latency-transport.md exactly as written: a POSIX
  shell expands and splits them as test_exp13_core's reading does; execute takes each as far as the VMs, visiting the
  conditions the "conditions run" table names; A16's variant of the first line, as the README writes it, adds `full`
  at 1 Gbit/s in repeats 2 and 4;
- the network README's "Methods for T12" names every option of run_exp04's EXP-13 group, of the run lines, of the
  analysis and of pgdelta's switches, and its sh lines parse;
- the harness's own pgdelta command lines for v1 and its side arms, run by the merged pgdelta on a real stopped
  cluster over local TCP (PostgreSQL 17's binaries, tests/pgdelta_cluster.PG_BIN): B ends byte-identical to S, and
  the harness's tool_reported(), its summary row and the analysis read every statistic the smoke table names from
  what pgdelta wrote; and the stubbed harness's run records, with those statistics, hold every run Record path of
  the smoke table that the stub can produce.

On macOS the sender's waits have no busy and idle parts (the send queue is read with Linux ioctls): there the parts
are checked to be null and the totals present; on Linux, present. The analysis's parts need the analysis venv's
matplotlib and are skipped without it.
"""

import io
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from experiments.network import measure, remote, run_exp04
from pgdelta import cli
from pgdelta.datadir import select

import test_exp13_core as core
from pgdelta_cluster import CONTROLDATA, SKIP_REASON, Servers, available, initdb
from pgdelta_support import REPO_ROOT, snapshot, subprocess_env
from test_pgdelta_datadir import materialize

try:
    import matplotlib  # noqa: F401
except ImportError:  # the analysis scripts need the analysis venv
    matplotlib = None
if matplotlib is not None:
    sys.path.append(str(Path(__file__).resolve().parents[1] / 'analysis'))
    import exp13

needs_venv = unittest.skipUnless(matplotlib is not None, 'needs the analysis venv (matplotlib)')
ROOT = Path(__file__).resolve().parents[1]
SPEC = core.SPEC
README = ROOT / 'experiments' / 'network' / 'README.md'
V1, TLS = core.V1, core.TLS
W1, W16, HC, _ = measure.T12_V1_ARMS
CLEAR_ARMS = (V1, W1, W16, HC)
LINUX = sys.platform.startswith('linux')
# The conditions each invocation visits ("The conditions run"), in --conditions' order.
VISITED = {
    'exp13-uniform1': ['2:0:default', '2:20:default', '2:60:default', '2:120:default', '2:60:tuned', '2:120:tuned',
                       '2:0:tuned', '1:60:default', '1:120:default'],
    'exp13-hot': ['2:0:default', '2:20:default', '2:60:default', '2:120:default', '2:60:tuned', '2:120:tuned',
                  '2:0:tuned'],
    'exp13-uniform10': ['2:0:default', '2:0:tuned', '2:20:default', '2:60:default', '2:60:tuned', '2:120:tuned'],
}
A16_FULL = '1:60:default,1:120:default'


def run_lines_block() -> str:
    return re.search(r'\*\*Run lines\*\*.*?```sh\n(.*?)```', SPEC.read_text(), re.S).group(1)


def shell_run_lines(block: str) -> dict:
    """{invocation name: run_exp04's arguments}, as /bin/sh expands and splits the block: python3 is a shell function
    that prints its arguments, so nothing runs."""
    script = 'python3() { for word in "$@"; do printf "%s\\000" "$word"; done; printf "\\001"; }\n' + block
    out = subprocess.run(['/bin/sh', '-c', script], capture_output=True, check=True, timeout=30).stdout.decode()
    lines = {}
    for call in filter(None, out.split('\x01')):
        words = call.split('\x00')[:-1]
        if words[:1] != ['experiments/network/remote.py']:
            continue
        args = remote.build_parser().parse_args(words[1:])
        lines[args.name] = args.args[1:] if args.args[:1] == ['--'] else args.args
    return lines


def harness_args(name, argv, tmp='/nonexistent/exp13'):
    return run_exp04.parse_args(['--source-ip', '192.0.2.2', '--out', f'{tmp}/{name}/out', '--work-dir',
                                 f'{tmp}/{name}/work', *argv])


def schedule(args) -> list:
    return measure.t12_schedule(run_exp04.method_list(args.methods), measure.parse_conditions(args.conditions),
                                args.repeats, run_exp04.t12_plan(args))


class Stop(Exception):
    pass


class RunLines(unittest.TestCase):
    """EXP-13's "Run plan and budget": its run lines, exactly as written there."""

    def test_a_posix_shell_splits_them_as_the_core_tests_read_them(self):
        found = shell_run_lines(run_lines_block())
        self.assertEqual(sorted(found), ['exp13-hot', 'exp13-uniform1', 'exp13-uniform10'])
        self.assertEqual(found, core.spec_run_lines())

    def test_as_written_execute_takes_each_as_far_as_the_vms(self):
        for name, argv in core.spec_run_lines().items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                orchestrator = run_exp04.Orchestrator(harness_args(name, argv, tmp))
                self.addCleanup(orchestrator.pool.shutdown)
                with mock.patch.object(run_exp04.subprocess, 'run') as run, \
                        mock.patch.object(run_exp04.subprocess, 'Popen') as popen, \
                        mock.patch.object(orchestrator.source, 'open', side_effect=Stop), \
                        self.assertRaises(Stop):
                    orchestrator.execute()
                self.assertTrue(orchestrator.t12)
                self.assertEqual([condition['label'] for condition in orchestrator.speeds], VISITED[name])
                self.assertFalse(run.called or popen.called)

    def test_a16s_variant_runs_full_at_1_gbit_in_repeats_2_and_4(self):
        """"If A16 says `full` must run at 1 Gbit/s, the first invocation's `full` line gains 1:60:default,
        1:120:default". Its --baseline-every-repeat covers every baseline method, so with that alone `full` would run
        in all 5 repeats there; A16 says repeats 2 and 4, which --method-repeats full=2,4 keeps (the README)."""
        argv = list(core.spec_run_lines()['exp13-uniform1'])
        index = next(i for i, word in enumerate(argv) if word.startswith('full='))
        argv[index] += f',{A16_FULL}'
        as_worded = [(repeat, label) for repeat, label, method in schedule(harness_args('a16', argv))
                     if method == 'full' and label.startswith('1:')]
        self.assertEqual(len(as_worded), 10)
        argv += ['--method-repeats', 'full=2,4']
        planned = schedule(harness_args('a16', argv))
        at_one = sorted((repeat, label) for repeat, label, method in planned
                        if method == 'full' and label.startswith('1:'))
        self.assertEqual(at_one, [(2, '1:120:default'), (2, '1:60:default'), (4, '1:120:default'),
                                  (4, '1:60:default')])
        self.assertEqual(len(planned), 116)
        self.assertEqual(Counter(method for _, _, method in planned)['full'], 18)


# --- the README ---------------------------------------------------------------------------------------------------

def readme_section(title: str) -> str:
    text = README.read_text()
    start = text.index(f'## {title}\n')
    following = text.find('\n## ', start + 1)
    return text[start:following if following != -1 else len(text)]


def exp13_options() -> list[str]:
    """run_exp04's EXP-13 (T12) argument group, from its --help."""
    out = io.StringIO()
    with redirect_stdout(out), mock.patch('sys.stderr', io.StringIO()):
        try:
            run_exp04.parse_args(['--help'])
        except SystemExit:
            pass
    text = out.getvalue()
    group = text[text.index('EXP-13 (T12):'):text.index('EXP-12 (T10a):')]
    return sorted(set(re.findall(r'^\s{2}(--[a-z][a-z0-9-]*)', group, re.M)))


class Readme(unittest.TestCase):
    """The network README's "Methods for T12" describes what the run lines, the harness, pgdelta and the analysis
    take."""

    def test_it_names_every_option_of_the_exp13_group(self):
        section = readme_section('Methods for T12')
        options = exp13_options()
        self.assertIn('--first-visit-link-streams', options)
        self.assertEqual([option for option in options if f'`{option}' not in section], [])

    def test_every_option_of_the_run_lines_is_described(self):
        named = sorted({word.split('=')[0] for argv in core.spec_run_lines().values() for word in argv
                        if word.startswith('--')})
        text = README.read_text()
        self.assertEqual([option for option in named if f'`{option}' not in text], [])

    def test_it_names_pgdeltas_switches(self):
        section = readme_section('Methods for T12')
        for option in ('--window', '--busy-inflight-bytes', '--high-churn-node-fraction'):
            self.assertIn(f'`{option}', section)

    @needs_venv
    def test_it_names_every_option_of_the_analysis(self):
        section = readme_section('Methods for T12')
        options = [action.option_strings[0] for action in exp13.build_parser()._actions
                   if action.option_strings and action.option_strings[0] != '-h']
        self.assertEqual([option for option in options if f'`{option}' not in section], [])

    @needs_venv
    def test_its_analysis_lines_parse(self):
        # The section's sh blocks, split as a POSIX shell would; the smoke run's analysis writes into a scratch
        # directory under results/ (its item 22), never into docs/.
        found = []
        for block in re.findall(r'```sh\n(.*?)```', readme_section('Methods for T12'), re.S):
            for line in block.replace('\\\n', ' ').splitlines():
                words = shlex.split(line, comments=True)
                if words[:2] == ['.venv/bin/python', 'analysis/exp13.py']:
                    found.append(exp13.build_parser().parse_args(words[2:]))
        self.assertEqual(len(found), 2)
        smoke, sweep = found
        self.assertTrue(smoke.smoke)
        self.assertEqual((smoke.sweeps, smoke.tag), (['results/exp04/exp13-smoke'], '-smoke'))
        self.assertTrue(smoke.datadir.startswith('results/') and smoke.figdir.startswith('results/'))
        self.assertEqual(sweep.model_sweeps, ['results/exp04/exp13-smoke'])
        self.assertEqual(sweep.sweeps, [str(path.relative_to(ROOT)) for path in exp13.SWEEPS])


# --- pgdelta's switches and timers through the harness and the analysis -------------------------------------------

def repoint(argv, values: dict) -> list[str]:
    """The harness's command line with the values after some options replaced (the VMs' paths and addresses)."""
    argv = [str(word) for word in argv]
    for option, value in values.items():
        argv[argv.index(option) + 1] = str(value)
    argv[0] = sys.executable
    return argv


class Receiver:
    """pgdelta's receiver as the harness launches it (run_exp04.Child): its launch on the wall clock, its wall until
    it exits, its stderr drained by a thread, and the port it listens on."""

    def __init__(self, argv):
        self.started_utc = time.time()
        self.started = time.perf_counter()
        self.process = subprocess.Popen(argv, stderr=subprocess.PIPE, text=True, cwd=REPO_ROOT, env=subprocess_env())
        self.lines, self.listening = [], threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stderr:
            self.lines.append(line)
            if 'listening on' in line:
                self.listening.set()
        self.listening.set()

    def port(self) -> str:
        self.listening.wait(60)
        line = next((line for line in self.lines if 'listening on' in line), None)
        if line is None:
            raise AssertionError(f'the receiver never listened: {"".join(self.lines)}')
        return line.rsplit(':', 1)[1].strip()

    def wait(self) -> dict:
        code = self.process.wait(timeout=120)
        wall = time.perf_counter() - self.started
        self.reader.join(10)
        self.process.stderr.close()
        return {'host': 'dest', 'exit_code': code, 'wall_seconds': round(wall, 6), 'started_utc': self.started_utc,
                'role': f'receiver, launched in {measure.DIRECT_PHASE}', 'log_tail': ''.join(self.lines)}


def stub(tmp) -> core.T12Stub:
    """The core tests' harness at EXP-13's conditions, at the anchor, pgdelta taking --busy-inflight-bytes."""
    orchestrator = core.T12Stub(tmp, mock.Mock(return_value=1000.0))
    orchestrator.busy_flag = True
    orchestrator.condition = {**measure.parse_condition('2:0:default'), 'busy_inflight_bytes': 24000}
    return orchestrator


def tool_reported(orchestrator, method, stats_text) -> dict:
    """run_exp04's own account of a pgdelta run, reading the sender's --stats-json as the source's agent would."""
    with mock.patch.object(orchestrator.source, 'agent', return_value={'exit_code': 0, 'log_tail': stats_text}):
        return run_exp04.Orchestrator.tool_reported(orchestrator, method, orchestrator.method_paths(method), {})


class RealSessions:
    """A stopped cluster S, its earlier copy B0 with scattered changes since (as uniform's: every page of the table
    changes, so the fallback at 0.5 fires on it), and one session per arm with the harness's own command lines, each
    refreshing its own copy of B0 in place over local TCP. Built once for the module, before any test patches the
    clock (test_exp13_core's Clocked13 patches time.perf_counter)."""

    built = None

    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.servers = Servers(self.root)
        self.source, self.b0 = self.root / 'S', self.root / 'B0'
        initdb(self.source)
        self.servers.run_sql(self.source,
                             'create table t as select g as id, md5(g::text) as v from generate_series(1, 40000) g',
                             'checkpoint')
        shutil.copytree(self.source, self.b0)
        self.servers.run_sql(self.source, "update t set v = v || '!' where id % 23 = 0", 'checkpoint')
        # What the stopped source's selection holds (EXP-09's check: B byte-identical to it).
        self.expected = snapshot(materialize(self.source, select(self.source, CONTROLDATA), self.root / 'selected'))
        self.orchestrator = stub(str(self.root / 'harness'))
        self.sessions = {method: self.session(method) for method in CLEAR_ARMS}

    @classmethod
    def get(cls) -> 'RealSessions':
        if cls.built is None:
            cls.built = cls()
            unittest.addModuleCleanup(cls.built.close)
        return cls.built

    def close(self):
        self.orchestrator.pool.shutdown()
        self.servers.close()
        self.temp.cleanup()
        RealSessions.built = None

    def session(self, method) -> dict:
        orchestrator, paths = self.orchestrator, self.orchestrator.method_paths(method)
        basis, stats = self.root / f'B-{method}', self.root / f'{method}-send.json'
        shutil.copytree(self.b0, basis)
        receive = orchestrator.pgdelta_receive_argv(method, paths)
        send = orchestrator.pgdelta_send_argv(method, paths)
        receiver = Receiver(repoint(receive, {'--basis': basis, '--listen': '127.0.0.1:0',
                                              '--pg-controldata': CONTROLDATA}))
        port = receiver.port()
        sender = subprocess.run(repoint(send, {'--source': self.source, '--connect': f'127.0.0.1:{port}',
                                               '--pg-controldata': CONTROLDATA, '--stats-json': stats}),
                                capture_output=True, text=True, cwd=REPO_ROOT, env=subprocess_env(), timeout=120)
        command = receiver.wait()
        return {'receive': receive, 'send': send, 'sender': sender, 'receiver': command, 'basis': basis,
                'stats_text': stats.read_text() if stats.exists() else ''}


@unittest.skipUnless(available(), SKIP_REASON)
class PgdeltaThroughTheHarness(unittest.TestCase):
    """v1 and its side arms, with the harness's own command lines, on a real cluster (RealSessions)."""

    @classmethod
    def setUpClass(cls):
        built = RealSessions.get()
        cls.expected, cls.orchestrator, cls.sessions = built.expected, built.orchestrator, built.sessions

    def test_the_harnesss_command_lines_refresh_b_byte_identical_to_s(self):
        for method in CLEAR_ARMS:
            with self.subTest(method=method):
                session = self.sessions[method]
                self.assertEqual(session['sender'].returncode, 0, session['sender'].stderr)
                self.assertEqual(session['receiver']['exit_code'], 0, session['receiver']['log_tail'])
                self.assertEqual(snapshot(session['basis']), self.expected)
                send = [str(word) for word in session['send']]
                self.assertEqual(send[send.index('--window') + 1], str(measure.PGDELTA_ENGINES[method]['window']))
                self.assertEqual(send[send.index('--busy-inflight-bytes') + 1], '24000')
                self.assertIn('--in-place', [str(word) for word in session['receive']])

    def test_v1_tls_command_lines_are_pgdeltas_own(self):
        # Its sessions need the stunnel pair; its command lines are v1's, to and from localhost.
        paths = self.orchestrator.method_paths(TLS)
        receive = [str(word) for word in self.orchestrator.pgdelta_receive_argv(TLS, paths)]
        send = [str(word) for word in self.orchestrator.pgdelta_send_argv(TLS, paths)]
        parser = cli.build_parser()
        received, sent = parser.parse_args(receive[3:]), parser.parse_args(send[3:])
        self.assertEqual((received.listen, sent.connect),
                         (f'127.0.0.1:{run_exp04.PGDELTA_PORT}', f'127.0.0.1:{measure.STUNNEL_CLIENT_PORT}'))
        self.assertEqual((sent.window, sent.busy_inflight_bytes, received.in_place), (4, 24000, True))

    def test_the_harness_reads_every_statistic_the_smoke_table_names(self):
        for method in CLEAR_ARMS:
            with self.subTest(method=method):
                engine = measure.PGDELTA_ENGINES[method]
                reported = tool_reported(self.orchestrator, method, self.sessions[method]['stats_text'])
                self.assertEqual((reported['window'], reported['window_ok']), (engine['window'], True))
                self.assertEqual(reported['high_churn_ok'], True if method == HC else None)
                self.assertEqual(reported['high_churn_node_fraction'], engine.get('high_churn'))
                if method == HC:
                    self.assertGreaterEqual(reported['high_churn_files'], 1)
                self.assertTrue(reported['switches_ok'] and reported['prefetch_ok'])
                self.assertGreater(reported['prefetch']['bytes'], 0)
                self.assertGreater(reported['files']['changed'], 0)
                self.assertIn('sent', reported['pages'])
                for name in ('connect_s', 'leaf_requests', 'leaf_waits_over_1ms',
                             *(f'{base}_s' for base in measure.PGDELTA_SENDER_TIMERS)):
                    self.assertIsNotNone(reported[name], name)
                for wait in measure.PGDELTA_SENDER_WAITS:
                    parts = (reported[f'{wait}_busy_s'], reported[f'{wait}_idle_s'])
                    if LINUX:
                        # Each wait's parts add up to it within 2 ms (smoke item 13).
                        self.assertLessEqual(abs(reported['busy_idle_gap_s'][wait]), 0.002, wait)
                    else:
                        self.assertEqual(parts, (None, None), wait)
                receiver = reported['receiver']
                self.assertEqual(sorted(receiver), ['hello_wait_s', 'publish_s', 'published_utc', 'recv_wait_s'])
                self.assertTrue(all(value is not None for value in receiver.values()), receiver)
                self.assertGreater(receiver['publish_s'], receiver['hello_wait_s'])

    def record(self, method) -> dict:
        session = self.sessions[method]
        command = session['receiver']
        return {'method': method, 'speed': {'label': '2:0:default'}, 'condition': self.orchestrator.condition,
                'tool_reported': tool_reported(self.orchestrator, method, session['stats_text']),
                'receiver_wall_s': command['wall_seconds'],
                'phases': {measure.DIRECT_PHASE: {'commands': [command]}}}

    def test_the_summary_row_carries_the_timers_and_the_drain_on_dests_clock(self):
        for method in CLEAR_ARMS:
            with self.subTest(method=method):
                record = self.record(method)
                row = measure.summary_row(record)
                for name in ('window', 'leaf_requests', 'connect_s', 'accept_wait_s', 'basis_wait_s', 'leaf_wait_s',
                             'page_send_s', 'complete_wait_s', 'hello_wait_s', 'recv_wait_s', 'publish_s',
                             'receiver_wall_s', 'drain_s', 'prefetch_bytes'):
                    self.assertIsNotNone(row[name], name)
                self.assertIs(row['busy_split_valid'], True)
                # The receiver exits after the sender closes (its drain, up to 2 s). publish_s and the drain are read
                # on dest's clock, from the receiver's launch: they add up to its wall, and publish_s holds the
                # interpreter's start-up that pgdelta's own publish_s leaves out.
                self.assertLessEqual(0.0, row['drain_s'])
                self.assertLess(row['drain_s'], 2.5)
                self.assertAlmostEqual(row['publish_s'] + row['drain_s'], row['receiver_wall_s'], delta=2e-6)
                own = record['tool_reported']['receiver']['publish_s']
                self.assertGreater(row['publish_s'], own)
                self.assertLess(row['publish_s'] - own, 2.0)

    @needs_venv
    def test_the_analysis_reads_a3s_parts_from_them(self):
        for method in CLEAR_ARMS:
            with self.subTest(method=method):
                record = self.record(method)
                summary = measure.summary_row(record)
                row = {name: summary.get(name) for name in exp13.NUMERIC}
                row.update(method=method, status='pass', dirty_increase_mb=None, disk_write_dest_bytes=None,
                           output_bytes=None)
                row.update(exp13.from_record(record, row))
                self.assertEqual(row['drain_s'], summary['drain_s'])
                self.assertGreater(row['files_changed'], 0)
                parts = exp13.protocol_parts(row)
                if LINUX:
                    self.assertTrue(all(value is not None for value in parts.values()), parts)
                    self.assertGreaterEqual(parts['fixed_rtts_s'], row['connect_s'] + row['drain_s'])
                else:  # no busy and idle parts off Linux: A3's parts are null, never guessed
                    self.assertEqual(parts, {'idle_waits_s': None, 'fixed_rtts_s': None, 'sending_s': None})


@needs_venv
@unittest.skipUnless(available(), SKIP_REASON)
class HarnessRecordsThroughTheAnalysis(core.Clocked13):
    """The stubbed harness's runs of every arm (test_exp13_core's fakes), pgdelta's statistics those of a real
    session: the analysis finds every run Record path of the smoke table in them."""

    stats_text = None

    @classmethod
    def setUpClass(cls):
        cls.stats_text = RealSessions.get().sessions[V1]['stats_text']

    class RealStats(core.T12Stub):
        stats_text = None

        def tool_reported(self, method, paths, phases):
            if method in measure.PGDELTA_METHODS:
                with mock.patch.object(self.source, 'agent',
                                       return_value={'exit_code': 0, 'log_tail': self.stats_text}):
                    return run_exp04.Orchestrator.tool_reported(self, method, paths, phases)
            return super().tool_reported(method, paths, phases)

    def test_every_run_record_path_of_the_smoke_table_is_there(self):
        methods = f'full,parcopy8,probackup8,{V1},{W1},{W16},{HC},{TLS}'
        orchestrator = self.orchestrator(self.RealStats, ['--methods', methods, '--dirty-noise-table',
                                                          '5:4,30:4,120:4'])
        orchestrator.stats_text = self.stats_text
        orchestrator.busy_flag = True
        # As execute's preflight sets it from --dirty-noise-table (nothing measured here).
        orchestrator.noise_table = measure.noise_table(measure.parse_noise_table(orchestrator.args.dirty_noise_table),
                                                       {})
        records = []
        with mock.patch.object(run_exp04, 'PROCESSES_SNAPSHOT_S', 0.0), \
                mock.patch.object(core.T12Stub, 'processes_snapshot',
                                  lambda self: {'source': ['sshd: bench'], 'dest': []}):
            for label, method in (('2:60:default', 'full'), ('2:60:default', 'parcopy8'),
                                  ('2:60:default', 'probackup8'), ('2:60:default', V1), ('2:120:tuned', W1),
                                  ('2:120:tuned', W16), ('2:120:tuned', HC), ('2:120:tuned', TLS),
                                  ('2:0:tuned', V1), ('2:0:default', 'full')):
                record = self.run_at(orchestrator, label, method)
                self.assertNotEqual(record['status'], 'error', (method, record.get('error')))
                records.append(record)
        rows, _, _, found = exp13.load(orchestrator.out)
        self.assertEqual(len(rows), len(records))
        wanted = {(item, kind, path) for item, kind, path, which in exp13.SMOKE_RECORDS
                  if kind == 'run' and any(exp13.smoke_applies(which, record) for record in records)}
        # The stub's stunnel pair records no log paths (its tests in test_exp13_core read the real one's).
        stub_only = {(12, 'run', 'stunnel.logs')}
        self.assertEqual(sorted(wanted - found - stub_only), [])
        v1 = next(row for row in rows if row['method'] == V1 and row['speed'] == '2:60:default')
        self.assertEqual((v1['window'], v1['rtt_ms'], v1['host_tcp']), (4, 60.0, 'default'))
        self.assertIsNotNone(v1['publish_s'])
        tls = next(row for row in rows if row['method'] == TLS)
        self.assertEqual((tls['tls_version'], tls['busy_split_valid']), ('TLSv1.3', False))



@needs_venv
class TimeoutsThroughTheAnalysis(core.Clocked13):
    """A15's timeout leg end to end: a probackup8 copy that stalls is stopped at its bound by the harness, recorded as a
    tool failure ('timeout'), and the analysis excludes the arm from every baseline (S.a15)."""

    def test_a_stalled_copy_lands_in_a15(self):
        orchestrator = self.orchestrator(core.Stalling, ['--methods', f'full,parcopy8,probackup8,{V1},{TLS}'])
        orchestrator.gnu_time = None
        condition, link, _ = self.visit(orchestrator, '2:60:default')
        with mock.patch.object(measure, 'T12_TIMEOUT_PROBACKUP_S', 0.5), \
                mock.patch.object(core.T12Stub, 'processes_snapshot', lambda self: {'source': [], 'dest': []}):
            record = orchestrator.run_method(core.TRIAL, 0, condition, dict(link), 'probackup8', 0, ['probackup8'])
        self.assertEqual(record['failure_reason'], 'timeout')
        rows, trials, _, _ = exp13.load(orchestrator.out)
        S = exp13.Session(rows, trials)
        self.assertEqual([row['failure_reason'] for row in S.a15['probackup8']], ['timeout'])
        self.assertIn('1 timed out at 4 times the expected upper bound', S.excluded['probackup8'])
        self.assertIsNone(S.baseline('B_rel', '2:60:default'))
        self.assertEqual(rows[0]['left_out'], 'status error (tool: timeout)')


if __name__ == '__main__':
    unittest.main()
