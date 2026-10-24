"""Tests for EXP-11's pg_probackup block (T2's released parallel arm) and the syscall audit's place
in the orchestrator: experiments/network/run_exp04.py and measure.py.

No VM, PostgreSQL, pg_probackup or strace is needed. The orchestrator runs with the stubs of
test_exp04_network and test_exp11_core; pg_probackup's console and help texts are 2.5.16's, as
its source prints them; the outputs are small trees in temporary directories.
"""

import csv
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from experiments.network import durability_audit, measure, run_exp04

import test_exp04_network as base
import test_exp11_core as core

# pg_probackup-17 2.5.16's `catchup --help` (src/help.c, help_catchup), trimmed to the usage and the
# options probackup* pass.
CATCHUP_HELP = """\

pg_probackup-17 catchup  -b catchup-mode
                 --source-pgdata=path_to_pgdata_on_remote_server
                 --destination-pgdata=path_to_local_dir
                 [--stream [-S slot-name]] [--temp-slot | --perm-slot]
                 [-j num-threads]
                 [-T OLDDIR=NEWDIR]
                 [--exclude-path=path_prefix]
                 [-d dbname] [-h host] [-p port] [-U username]
                 [-w --no-password] [-W --password]
                 [--remote-proto] [--remote-host]
                 [--remote-port] [--remote-path] [--remote-user]
                 [--ssh-options]
                 [--dry-run]
                 [--help]

  -b, --backup-mode=catchup-mode   catchup mode=FULL|DELTA|PTRACK
      --stream                     stream the transaction log (only supported mode)
  -S, --slot=SLOTNAME              replication slot to use
      --temp-slot                  use temporary replication slot
  -j, --threads=NUM                number of parallel threads

  Connection options:
  -U, --pguser=USERNAME            user name to connect as (default: current local user)
  -d, --pgdatabase=DBNAME          database to connect (default: username)
  -h, --pghost=HOSTNAME            database server host or socket directory(default: 'local socket')
  -p, --pgport=PORT                database server port (default: 5432)

  Logging options:
      --log-level-console=log-level-console
                                   level for console logging (default: info)

  Remote options:
      --remote-proto=protocol      remote protocol to use
      --remote-host=hostname       remote host address or hostname
      --remote-port=port           remote host port (default: 22)
      --remote-path=path           path to directory with pg_probackup binary on remote host
      --remote-user=username       user name for ssh connection (default: current user)
      --ssh-options=ssh_options    additional ssh options (default: none)
"""
VERSION = 'pg_probackup-17 2.5.16 (PostgreSQL 17)'
LOGICAL = base.LOGICAL
# A stopped source's selected files (what agent.py datadir-digest hashes), with what a clean stop rewrites.
SOURCE_FILES = {'PG_VERSION': b'17\n', 'base/5/16397': b'A' * 8192, 'base/5/1259': b'B' * 8192,
                'global/1262': b'G' * 8192, 'global/pg_control': b'C' * 64,
                'pg_wal/000000010000000000000003': b'W' * 128}


def make_tree(root: Path, files: dict) -> Path:
    for path, data in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)
    return root


def reference_of(root: Path) -> dict:
    """A reference digest of a stopped source tree, as reference_digest() keeps it."""
    return measure.reference_view(measure.files_digest(root, sorted(SOURCE_FILES)))


# --- the helpers in measure ---------------------------------------------------------------

class Helpers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def argv(self, method='probackup8'):
        return measure.probackup_catchup_argv(
            measure.PROBACKUP_EXE, method, source_pgdata='/w/src/data', dest_pgdata=f'/w/out/{method}',
            source_ip='10.0.0.12', port=5432, user='study', database='postgres', ssh_user='bench',
            ssh_config='/w/probackup/ssh_config')

    def test_2_5_16s_catchup_help_lists_everything_probackup_passes(self):
        self.assertEqual(measure.probackup_help_problems(CATCHUP_HELP, self.argv()), [])
        self.assertEqual(measure.probackup_help_problems(CATCHUP_HELP, self.argv('probackup4')), [])
        no_full = CATCHUP_HELP.replace('catchup mode=FULL|DELTA|PTRACK', 'catchup mode=DELTA|PTRACK')
        self.assertEqual(measure.probackup_help_problems(no_full, self.argv()), ['-b FULL is not among its catchup modes'])
        older = CATCHUP_HELP.replace('[--temp-slot | --perm-slot]', '').replace('--temp-slot ', '')
        self.assertEqual(measure.probackup_help_problems(older, self.argv()), ['--temp-slot is not listed'])
        self.assertEqual(len(measure.probackup_help_problems('', self.argv())), 2 + 14)

    def test_versions(self):
        self.assertEqual(measure.probackup_version(VERSION), {'version': '2.5.16', 'postgresql': '17'})
        self.assertEqual(measure.probackup_version('pg_probackup 2.5.16 (PostgreSQL 16.4)'),
                         {'version': '2.5.16', 'postgresql': '16'})
        self.assertIsNone(measure.probackup_version(None))
        self.assertIsNone(measure.probackup_version('pg_probackup-17: command not found'))
        self.assertEqual(measure.PROBACKUP_VERSION, '2.5.16')

    def test_the_reach_test_is_how_catchup_starts_its_agent(self):
        # src/utils/remote.c: ssh -l USER, --ssh-options, its own -o options, the host, "<path>/<name> agent".
        self.assertEqual(measure.probackup_ssh_argv('/usr/local/bin/pg_probackup-17', source_ip='10.0.0.12',
                                                    ssh_user='bench', ssh_config='/w/probackup/ssh_config'),
                         ['ssh', '-l', 'bench', '-F', '/w/probackup/ssh_config', '-o', 'PasswordAuthentication=no',
                          '-o', 'Compression=no', '-o', 'ControlMaster=no', '-o', 'LogLevel=error', '10.0.0.12',
                          '/usr/local/bin/pg_probackup-17 --version'])
        # catchup's own argv names the same directory for its agent and the same configuration.
        argv = self.argv()
        self.assertIn('--remote-path=/usr/local/bin', argv)
        self.assertIn('--ssh-options=-F /w/probackup/ssh_config', argv)

    def test_outcomes(self):
        for started, match, reference, outcome in (
                (True, True, True, 'pass'), (True, True, False, 'pass'), (True, False, True, 'fail'),
                (True, False, False, 'void'), (True, False, None, 'fail'), (False, None, False, 'fail'),
                (False, None, True, 'fail')):
            with self.subTest(started=started, match=match, reference=reference):
                self.assertEqual(measure.probackup_outcome(started, match, reference), outcome)

    def test_the_byte_diagnostic(self):
        source = make_tree(self.root / 'src', SOURCE_FILES)
        reference = reference_of(source)
        copied = {**SOURCE_FILES, 'global/pg_control': b'D' * 64, 'pg_wal/000000010000000000000004': b'X',
                  'base/5/pg_internal.init': b'I', 'backup_label': b'label'}
        output = make_tree(self.root / 'out', copied)
        paths = measure.byte_diagnostic_paths(output)
        self.assertEqual(paths, ['base/5/1259', 'base/5/16397', 'base/5/pg_internal.init', 'global/1262'])
        result = measure.byte_diagnostic(reference, measure.files_digest(output, paths))
        self.assertTrue(result['identical'])  # pg_control, pg_wal and the files outside base/ and global/ left out
        self.assertEqual((result['selected_files'], result['selected_bytes'], result['output_files']),
                         (3, 3 * 8192, 4))
        self.assertEqual((result['extra'], result['extra_count']), (['base/5/pg_internal.init'], 1))
        (output / 'base/5/16397').write_bytes(b'Z' * 8192)
        (output / 'global/1262').unlink()
        result = measure.byte_diagnostic(reference, measure.files_digest(output, measure.byte_diagnostic_paths(output)))
        self.assertFalse(result['identical'])
        self.assertEqual((result['differing'], result['missing']), (['base/5/16397'], ['global/1262']))


# --- the timed phase --------------------------------------------------------------------

class Probackup(core.T2Stub):
    """T2Stub whose pg_probackup prints 2.5.16's console lines and leaves an output tree, with the
    counter reads recorded as events."""

    OUTPUT = {**SOURCE_FILES, 'backup_label': b'START WAL LOCATION: 2/A0000028\n',
              'pg_wal/000000010000000000000003': b'W' * 4096}
    CATCHUP_EXIT = 0

    def snapshot_source(self):
        self.events.append('snapshot_source')
        return super().snapshot_source()

    def snapshot_dest(self):
        self.events.append('snapshot_dest')
        return super().snapshot_dest()

    def local_command(self, argv, log_name, *, timeout=None, cwd=None, env=None, stamp_lines=False):
        result = super().local_command(argv, log_name, timeout=timeout, cwd=cwd, env=env, stamp_lines=stamp_lines)
        if Path(argv[0]).name.startswith('pg_probackup'):
            log = self.dest_log(log_name)
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(core.PROBACKUP_LOG)
            output = Path(next(part.split('=', 1)[1] for part in argv if part.startswith('--destination-pgdata=')))
            make_tree(output, self.OUTPUT)
            result.update(log=str(log), stamped_lines=stamp_lines, env=env, exit_code=self.CATCHUP_EXIT)
        return result


class Runs(core.Clocked):
    def test_the_wal_ticker_runs_around_the_window_not_inside_it(self):
        orchestrator = self.orchestrator(Probackup)
        record = self.run_method(orchestrator, 'probackup8')
        self.assertEqual(record['status'], 'pass', record.get('traceback'))
        phase = record['phases']['catchup_sync']
        self.assertEqual(phase['wal_ticker']['interval_s'], 0.1)
        events = [event for event in orchestrator.events if isinstance(event, tuple)]
        start = events.index(('wal_ticker', 'start', 'probackup8'))
        stop = events.index(('wal_ticker', 'stop'))
        catchup = next(i for i, event in enumerate(events)
                       if event[0] == 'dest' and Path(event[1][0]).name.startswith('pg_probackup'))
        self.assertLess(start, catchup)
        self.assertLess(catchup, stop)
        flags = orchestrator.method_flags('probackup8')['wal_ticker']
        self.assertEqual(flags['interval_s'], 0.1)
        self.assertIn('pg_logical_emit_message(false', flags['sql'])
        self.assertNotIn('-c', flags['argv'])

    def test_the_wal_ticker_can_be_turned_off(self):
        orchestrator = self.orchestrator(Probackup, ['--probackup-wal-ticker', '0'])
        record = self.run_method(orchestrator, 'probackup8')
        self.assertEqual(record['status'], 'pass', record.get('traceback'))
        self.assertIsNone(record['phases']['catchup_sync']['wal_ticker'])
        self.assertNotIn(('wal_ticker', 'stop'), orchestrator.events)
        self.assertIsNone(orchestrator.method_flags('probackup8')['wal_ticker'])

    def test_the_real_ticker_feeds_psql_a_watch_and_stops_it(self):
        orchestrator = self.orchestrator(Probackup)
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / 'psql'
            seen = Path(tmp) / 'stdin'
            fake.write_text(f'#!/bin/sh\ncat > {seen}\n')
            fake.chmod(0o755)
            with mock.patch.object(run_exp04.Orchestrator, 'wal_ticker_argv', return_value=[str(fake)]):
                ticker = run_exp04.Orchestrator.start_wal_ticker(orchestrator, 'probackup8')
                ticker['process'].stdin.close()  # the stand-in's cat ends at EOF, as psql's \watch never does
                ticker['process'].wait(timeout=10)
                stopped = run_exp04.Orchestrator.stop_wal_ticker(orchestrator, ticker)
            self.assertEqual(stopped['interval_s'], 0.1)
            self.assertTrue(stopped['exited_early'])  # here, because the stand-in ended; reported, not fatal
            self.assertIn("SELECT pg_logical_emit_message(false, 'pgd-tick', '', true) \\watch 0.1",
                          seen.read_text())

    def test_catchup_then_its_sync_f_in_one_window(self):
        orchestrator = self.orchestrator(Probackup)
        record = self.run_method(orchestrator, 'probackup8')
        self.assertEqual(record['status'], 'pass', record.get('traceback'))
        self.assertEqual(list(record['phases']), ['catchup_sync'])
        phase = record['phases']['catchup_sync']
        catchup, sync = phase['commands']
        output = str(orchestrator.outputs / 'probackup8')
        self.assertEqual(catchup['argv'], [str(part) for part in orchestrator.probackup_argv(
            'probackup8', orchestrator.method_paths('probackup8'))])
        self.assertEqual(catchup['argv'][1:8], ['catchup', '-b', 'FULL', '--stream', '--temp-slot', '-j', '8'])
        self.assertTrue(catchup['stamped_lines'])
        self.assertEqual(catchup['env']['LC_MESSAGES'], 'C')
        self.assertEqual(sync['argv'], ['sync', '-f', output])
        self.assertEqual(phase['steps'], {'catchup': {'host': 'dest', 'offset_seconds': 0.0, 'wall_seconds': 1.0},
                                          'sync': {'host': 'dest', 'offset_seconds': 1.0, 'wall_seconds': 0.5}})
        # Check (c): the sync -f runs inside the phase's one counter window, after the catchup.
        kinds = [kind for kind in self.kinds(orchestrator)
                 if kind in ('snapshot_source', 'snapshot_dest', 'drop_caches') or kind in (
                     ('dest', 'pg_probackup-17'), ('dest', 'sync'))]
        self.assertEqual(kinds, ['drop_caches', 'snapshot_source', 'snapshot_dest', ('dest', 'pg_probackup-17'),
                                 ('dest', 'sync'), 'snapshot_dest', 'snapshot_source'])
        row = measure.summary_row(record)
        self.assertEqual((row['counter_windows'], row['total_s'], row['probackup_sync_s']), (1, 1.5, 0.5))
        self.assertEqual((row['probackup_copy_s'], row['probackup_stop_s'], row['probackup_files_sync_s']),
                         (2.4, 0.098, 0.695))
        self.assertEqual((row['tier'], row['durable']), ('released', True))
        self.assertTrue(record['tool_reported']['probackup_phases']['complete'])

    def test_a_catchup_that_fails_gets_no_sync(self):
        class Failing(Probackup):
            CATCHUP_EXIT = 1

        orchestrator = self.orchestrator(Failing)
        record = self.run_method(orchestrator, 'probackup4')
        self.assertEqual(record['status'], 'error')
        self.assertIn('pg_probackup-17 exited 1', record['error'])
        phase = record['phases']['catchup_sync']
        self.assertEqual([Path(command['argv'][0]).name for command in phase['commands']], ['pg_probackup-17'])
        self.assertEqual(list(phase['steps']), ['catchup'])
        self.assertNotIn(('dest', 'sync'), self.kinds(orchestrator))
        # A copy the tool itself aborted: A4 counts it, as an output that didn't start.
        self.assertEqual(record['failure_kind'], 'tool')
        a4 = orchestrator.a4_summary([record])
        self.assertEqual(a4['excluded_arms'], ['probackup4'])
        self.assertIn('pg_probackup-17 exited 1', a4['failed_runs'][0]['reason'])


# --- verification ----------------------------------------------------------------------

class Verifying(Probackup):
    verify = run_exp04.Orchestrator.verify
    start = {'ok': True, 'ready_seconds': 2.5}

    server_log = 'LOG:  redo starts at 2/A0000028\nLOG:  consistent recovery state reached\n'

    def check_output(self, method, data, *, recovery, query=run_exp04.OUTPUT_QUERY):
        # What the start sees, and what starting the output changes in it.
        self.events.append(('check_output', recovery, query, run_exp04.tree_bytes(data)))
        (Path(data) / 'postmaster.pid').write_text('4242\n' * 20)
        log = self.dest_log(f'{method}-output-server.log')
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(self.server_log)
        return {**self.start, 'answer': json.dumps(LOGICAL) if self.start['ok'] else None}


class Verification(core.Clocked):
    def orchestrator(self, cls=Verifying, extra_args=()):
        orchestrator = super().orchestrator(cls, extra_args)
        source = make_tree(Path(self.tmp.name) / 'src', SOURCE_FILES)
        orchestrator.reference_digests = {'start': reference_of(source)}
        orchestrator.methods = ['full', 'probackup8', 'probackup4']
        return orchestrator

    def test_bytes_counted_the_byte_diagnostic_then_the_start_and_a_pending_digest(self):
        orchestrator = self.orchestrator()
        with mock.patch.object(run_exp04.measure, 'tree_stat', return_value={}):
            record = self.run_method(orchestrator, 'probackup8', speed=core.UNTHROTTLED)
        self.assertEqual(record['status'], 'pending', record.get('traceback'))
        verification = record['verification']
        size = sum(len(data) for data in Probackup.OUTPUT.values())
        start = [event for event in orchestrator.events if isinstance(event, tuple) and event[0] == 'check_output']
        self.assertEqual(start, [('check_output', True, measure.logical_digest_sql(), size)])
        self.assertEqual(verification['output_bytes'], size)  # counted before the start added postmaster.pid
        self.assertEqual(record['durability']['output_bytes'], size)
        self.assertEqual(verification['wal']['wal_bytes'], 4096)
        diagnostic = verification['byte_diagnostic']
        self.assertTrue(diagnostic['identical'])
        self.assertEqual((diagnostic['selected_files'], diagnostic['extra_count'], diagnostic['reference']),
                         (3, 0, 'start'))
        self.assertEqual(verification['recovery_signals'], [])
        self.assertEqual(verification['wal_files_restored'], 0)
        self.assertEqual(verification['logical'], LOGICAL)
        self.assertIsNone(verification['logical_match'])
        self.assertIsNone(verification['void'])
        self.assertTrue(verification['verified'])
        row = measure.summary_row(record)
        self.assertEqual((row['bytes_identical'], row['void'], row['logical_match'], row['output_start_s'],
                          row['usable_s']), (True, None, None, 2.5, 4.0))
        # The output's view is kept for the end of the trial.
        self.assertEqual(list(orchestrator.byte_views), [(0, 'unthrottled', 'probackup8')])

    def test_an_output_that_does_not_start_fails_at_once(self):
        class NoStart(Verifying):
            start = {'ok': False, 'error': 'CommandError: the output did not start'}

        orchestrator = self.orchestrator(NoStart)
        with mock.patch.object(run_exp04.measure, 'tree_stat', return_value={}):
            record = self.run_method(orchestrator, 'probackup8')
        self.assertEqual(record['status'], 'fail')
        self.assertFalse(record['verification']['verified'])
        self.assertIs(record['verification']['void'], False)
        self.assertNotIn('logical', record['verification'])

    def test_a_copy_that_needed_wal_fetched_after_the_clock_fails(self):
        class Fetched(Verifying):
            server_log = ('LOG:  restored log file "000000010000000200000004" from archive\n'
                          'LOG:  consistent recovery state reached\n')

        orchestrator = self.orchestrator(Fetched)
        with mock.patch.object(run_exp04.measure, 'tree_stat', return_value={}):
            record = self.run_method(orchestrator, 'probackup8')
        self.assertEqual(record['status'], 'fail')
        self.assertEqual(record['verification']['wal_files_restored'], 1)

    def test_bytes_that_differ_are_a_diagnostic_only(self):
        class Changed(Verifying):
            OUTPUT = {**Probackup.OUTPUT, 'base/5/16397': b'Z' * 8192}

        orchestrator = self.orchestrator(Changed)
        with mock.patch.object(run_exp04.measure, 'tree_stat', return_value={}):
            record = self.run_method(orchestrator, 'probackup8')
        self.assertEqual(record['status'], 'pending')
        self.assertFalse(record['verification']['bytes_identical'])
        self.assertEqual(record['verification']['byte_diagnostic']['differing'], ['base/5/16397'])

    def test_the_byte_diagnostic_can_be_cut_to_each_trials_first_repeat(self):
        orchestrator = self.orchestrator(extra_args=['--probackup-byte-diagnostic', 'first_repeat'])
        output = make_tree(Path(self.tmp.name) / 'out-probackup8', Probackup.OUTPUT)
        orchestrator.current_run = (0, 'unthrottled')
        self.assertTrue(orchestrator.probackup_byte_diagnostic('probackup8', output)['identical'])
        orchestrator.current_run = (6, 'unthrottled')
        self.assertEqual(orchestrator.probackup_byte_diagnostic('probackup8', output),
                         {'skipped': '--probackup-byte-diagnostic first_repeat', 'identical': None})
        orchestrator.reference_digests = {}
        self.assertIsNone(orchestrator.probackup_byte_diagnostic('probackup8', output))
        self.assertEqual(run_exp04.parse_args(['--source-ip', '10.0.0.12', '--out', '/x']).probackup_byte_diagnostic,
                         'every')

    def test_a_stopped_source_runs_own_digest_against_the_reference_is_reported(self):
        orchestrator = self.orchestrator()
        source = make_tree(Path(self.tmp.name) / 'src-later', {**SOURCE_FILES, 'global/pg_control': b'E' * 64})
        same = orchestrator.source_against_reference(measure.files_digest(source, sorted(SOURCE_FILES)))
        self.assertTrue(same['start_match'])  # pg_control left out
        (source / 'base/5/1259').write_bytes(b'Q' * 8192)
        changed = orchestrator.source_against_reference(measure.files_digest(source, sorted(SOURCE_FILES)))
        self.assertFalse(changed['start_match'])
        self.assertIn('base/5/1259', changed['start_differences'][0])


# --- the end of the trial: void, failed, reported, A4 ------------------------------------------

def record(method, repeat, *, status='pending', started=True, logical=None, **verification):
    return {'trial_id': '00-uniform-f0.01-s5', 'method': method, 'repeat': repeat,
            'speed': {'label': 'unthrottled'}, 'status': status, 'error': None,
            'verification': {'output_start': {'ok': started}, 'logical': logical,
                             'logical_match': None if status == 'pending' else verification.pop('logical_match', None),
                             'void': None if started else False, **verification}}


class Verdicts(core.Clocked):
    def setUp(self):
        super().setUp()
        self.source = make_tree(Path(self.tmp.name) / 'src', SOURCE_FILES)

    def orchestrator(self, cls=core.T2Stub, extra_args=()):
        orchestrator = super().orchestrator(cls, extra_args)
        orchestrator.methods = ['probackup8', 'probackup4', 'parcopy8', 'parcopy4', 'pgdelta_direct_inplace']
        orchestrator.reference_digests = {'start': reference_of(self.source)}
        return orchestrator

    def finish(self, orchestrator, records, reference_match):
        orchestrator.records = records
        trial = {'reference_match': reference_match,
                 'runs': [{'repeat': item['repeat'], 'speed': 'unthrottled', 'method': item['method'],
                           'status': item['status'], 'block': 'main'} for item in records]}
        with mock.patch.object(orchestrator, 'query', return_value=json.dumps(LOGICAL)) as query:
            check = orchestrator.finish_logical_checks(trial)
        return trial, check, query

    def test_with_the_source_unchanged_a_difference_fails_and_excludes_the_arm(self):
        orchestrator = self.orchestrator()
        lost = {**LOGICAL, 'events': {'rows': 504999, 'md5': 'f' * 32}}
        records = [record('probackup8', 0, logical=LOGICAL), record('probackup8', 1, logical=lost),
                   record('probackup4', 0, status='fail', started=False),
                   record('parcopy8', 0, status='fail', verified=False),
                   {**record('parcopy4', 0, status='error'), 'failure_kind': 'harness',
                    'error': 'CommandError: the source cluster could not be restarted'},
                   record('pgdelta_direct_inplace', 0, status='pass')]
        trial, check, query = self.finish(orchestrator, records, True)
        query.assert_called_once()
        self.assertEqual([item['status'] for item in records], ['pass', 'fail', 'fail', 'fail', 'error', 'pass'])
        self.assertEqual([run['status'] for run in trial['runs']], ['pass', 'fail', 'fail', 'fail', 'error', 'pass'])
        self.assertEqual([(item['verification']['logical_match'], item['verification']['void'])
                          for item in records[:2]], [(True, False), (False, False)])
        self.assertIn("digest differs from the source's", records[1]['error'])
        a4 = check['a4']
        self.assertEqual(a4['excluded_arms'], ['parcopy8', 'probackup4', 'probackup8'])
        self.assertEqual([(run['method'], run['reason']) for run in a4['failed_runs']],
                         [('probackup8', "its digest differs from the source's, which didn't change"),
                          ('probackup4', "the output didn't start"), ('parcopy8', 'verify_direct failed')])
        self.assertEqual([run['method'] for run in a4['errors']], ['parcopy4'])
        self.assertEqual((check['void_runs'], check['reference_match']), ([], True))
        self.assertEqual([run['match'] for run in check['runs']], [True, False])
        saved = json.loads(orchestrator.record_path(records[1]).read_text())
        self.assertEqual((saved['status'], saved['verification']['void']), ('fail', False))
        with open(orchestrator.out / 'summary.csv', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([(row['status'], row['void'], row['logical_match']) for row in rows[:2]],
                         [('pass', 'False', 'True'), ('fail', 'False', 'False')])

    def test_with_the_source_changed_a_difference_is_void(self):
        orchestrator = self.orchestrator()
        changed = make_tree(Path(self.tmp.name) / 'src-end', {**SOURCE_FILES, 'base/5/16397': b'N' * 8192})
        orchestrator.reference_digests['end'] = reference_of(changed)
        # The first copy was taken after the change: its bytes match the end reference, not the start.
        copy = make_tree(Path(self.tmp.name) / 'copy', {**SOURCE_FILES, 'base/5/16397': b'N' * 8192})
        view = measure.byte_view(measure.files_digest(copy, measure.byte_diagnostic_paths(copy)))
        orchestrator.byte_views[(0, 'unthrottled', 'probackup8')] = view
        diagnostic = {**measure.byte_diagnostic(orchestrator.reference_digests['start'], view), 'reference': 'start'}
        lost = {**LOGICAL, 'events': {'rows': 504999, 'md5': 'f' * 32}}
        v1_digest = measure.files_digest(self.source, sorted(SOURCE_FILES))
        records = [record('probackup8', 0, logical=lost, byte_diagnostic=diagnostic, bytes_identical=False),
                   record('probackup8', 1, logical=LOGICAL),
                   record('probackup4', 0, status='fail', started=False),
                   record('pgdelta_direct_inplace', 0, status='pass',
                          reference=orchestrator.source_against_reference(v1_digest))]
        trial, check, _ = self.finish(orchestrator, records, False)
        self.assertEqual([item['status'] for item in records], ['void', 'pass', 'fail', 'pass'])
        self.assertEqual([run['status'] for run in trial['runs']], ['void', 'pass', 'fail', 'pass'])
        self.assertTrue(records[0]['verification']['void'])
        self.assertIsNone(records[0]['error'])
        self.assertIn('the source changed during the trial', records[0]['void_reason'])
        self.assertIs(records[1]['verification']['void'], False)
        self.assertEqual(check['void_runs'], [{'repeat': 0, 'speed': 'unthrottled', 'method': 'probackup8'}])
        # A void run is no failure; a copy that didn't start fails whatever the source did.
        self.assertEqual(check['a4']['excluded_arms'], ['probackup4'])
        # Reported only: the void copy's bytes against the end reference, and v1's own digest of the source.
        self.assertFalse(records[0]['verification']['byte_diagnostic']['identical'])
        self.assertTrue(records[0]['verification']['byte_diagnostic']['end']['identical'])
        self.assertTrue(records[0]['verification']['bytes_identical'])
        reference = records[3]['verification']['reference']
        self.assertEqual((reference['start_match'], reference['end_match']), (True, False))
        self.assertEqual(records[3]['status'], 'pass')
        saved = json.loads(orchestrator.record_path(records[3]).read_text())
        self.assertIs(saved['verification']['reference']['end_match'], False)
        with open(orchestrator.out / 'summary.csv', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([(row['status'], row['void'], row['bytes_identical']) for row in rows[:2]],
                         [('void', 'True', 'True'), ('pass', 'False', '')])

    def test_nothing_to_settle(self):
        orchestrator = self.orchestrator()
        trial, check, query = self.finish(orchestrator, [record('pgdelta_direct_inplace', 0, status='pass')], True)
        query.assert_not_called()
        self.assertEqual(check['a4'], {'failed_runs': [], 'excluded_arms': [], 'errors': [],
                                       'rule': check['a4']['rule']})
        # Outside a T2 run, a trial with nothing pending has nothing to settle, as before.
        orchestrator.methods = ['full', 'pgdelta_direct_inplace']
        self.assertIsNone(self.finish(orchestrator, [record('pgdelta_direct_inplace', 0, status='pass')], None)[1])

    def test_a_trial_with_a_void_run_passes(self):
        class Voided(core.Trials.Ordered):
            outcome = 'void'

            def finish_logical_checks(self, trial):
                trial['runs'][0]['status'] = self.outcome
                return None

        for outcome, status in (('void', 'pass'), ('fail', 'fail')):
            with self.subTest(outcome=outcome):
                Voided.outcome = outcome
                orchestrator = core.Clocked.orchestrator(self, Voided, ['--repeats', '1', '--methods',
                                                                        'full,probackup8'])
                orchestrator.speeds = measure.parse_speeds('unthrottled')
                orchestrator.methods = ['full', 'probackup8']
                with mock.patch.object(run_exp04.measure, 'tree_digest',
                                       return_value={'entries': {}, 'device': 1}):
                    trial = orchestrator.run_trial(0, 'hot', 0.01)
                self.assertEqual(trial['status'], status, trial.get('traceback'))


# --- the preflight ---------------------------------------------------------------------

class Preflight(core.Clocked):
    class Checking(core.T2Stub):
        source_help = CATCHUP_HELP
        reach = (0, VERSION + '\n')

        def _source_agent(self, *args, timeout=600):
            self.events.append(('source_agent', args))
            if args[0] == 'run' and args[-2:] == ('catchup', '--help'):
                return {'exit_code': 0, 'log_tail': self.source_help}
            return {'exit_code': 0, 'log_tail': ''}

    def orchestrator(self, cls=Checking, extra_args=(), versions=(VERSION, VERSION)):
        orchestrator = super().orchestrator(cls, extra_args)
        orchestrator.run = {'environment': {'source': {'pg_probackup': versions[0]},
                                            'dest': {'pg_probackup': versions[1]}}}
        return orchestrator

    def subprocess_run(self, orchestrator, dest_help=CATCHUP_HELP):
        def run(argv, **kwargs):
            orchestrator.events.append(('subprocess', [str(part) for part in argv]))
            if argv[0] == 'ssh':
                code, text = orchestrator.reach
                return subprocess.CompletedProcess(argv, code, text, '' if code == 0 else 'Permission denied')
            if list(argv[1:]) == ['catchup', '--help']:
                return subprocess.CompletedProcess(argv, 0, dest_help, '')
            route = '10.0.0.12 dev ens4 src 10.0.0.13 uid 1000\n'
            return subprocess.CompletedProcess(argv, 0, route, '')
        return mock.patch.object(run_exp04.subprocess, 'run', side_effect=run)

    def test_the_same_release_on_both_able_to_run_catchup_and_reached_over_ssh(self):
        orchestrator = self.orchestrator()
        with self.subprocess_run(orchestrator):
            result = orchestrator.check_probackup()
        self.assertEqual(result['version'], {'version': '2.5.16', 'postgresql': '17'})
        self.assertTrue(result['as_read'])
        self.assertEqual(result['versions'], {'source': VERSION, 'dest': VERSION})
        config = Path(result['ssh_config'])
        self.assertEqual(config, Path(orchestrator.work, 'probackup', 'ssh_config'))
        self.assertEqual(config.read_text(), measure.probackup_ssh_config('10.0.0.12', orchestrator.args.ssh_user,
                                                                          '/nonexistent'))
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertTrue(Path(orchestrator.work, 'probackup', 'log').is_dir())
        calls = [event[1] for event in orchestrator.events if event[0] == 'subprocess']
        self.assertEqual(calls[0], ['/usr/local/bin/pg_probackup-17', 'catchup', '--help'])
        self.assertEqual(calls[1], measure.probackup_ssh_argv('/usr/local/bin/pg_probackup-17', source_ip='10.0.0.12',
                                                              ssh_user=orchestrator.args.ssh_user,
                                                              ssh_config=str(config)))
        source_help = [event[1] for event in orchestrator.events if event[0] == 'source_agent']
        self.assertEqual(source_help[0][-3:], ('/usr/local/bin/pg_probackup-17', 'catchup', '--help'))

    def test_refusals(self):
        older = 'pg_probackup-17 2.5.15 (PostgreSQL 17)'
        cases = (((VERSION, older), {}, 'differs between the VMs'),
                 ((None, VERSION), {}, 'is not installed on source'),
                 ((VERSION, VERSION), {'source_help': CATCHUP_HELP.replace('FULL|', '')}, '-b FULL'),
                 ((VERSION, VERSION), {'reach': (255, '')}, "SSH from dest does not reach"),
                 ((VERSION, VERSION), {'reach': (0, older)}, 'SSH from dest does not reach'))
        for versions, attributes, message in cases:
            with self.subTest(message=message, attributes=attributes):
                cls = type('Refusing', (self.Checking,), attributes)
                orchestrator = self.orchestrator(cls, versions=versions)
                with self.subprocess_run(orchestrator), self.assertRaises(SystemExit) as refused:
                    orchestrator.check_probackup()
                self.assertIn(message, str(refused.exception))

    def test_execute_records_the_check_in_run_json(self):
        class Executing(core.RunJson.Executing):
            check_probackup = run_exp04.Orchestrator.check_probackup
            reach = (0, VERSION)

            def environment(self):
                env = {'deployed': {'tree_sha256': 'x'}, 'strace': 'strace -- version 6.1',
                       'pg_basebackup_sync_method': True, 'pg_probackup': VERSION, 'data_mount': core.EXT4_MOUNT}
                return {'source': dict(env), 'dest': dict(env)}

            def _source_agent(self, *args, timeout=600):
                if args[-2:] == ('catchup', '--help'):
                    return {'exit_code': 0, 'log_tail': CATCHUP_HELP}
                return super()._source_agent(*args, timeout=timeout)

        args = ['--methods', 'full,probackup8,probackup4', '--speeds', 'unthrottled', '--patterns', 'hot',
                '--gnu-time', 'none', '--disk-test-mib', '0', '--idle-seconds', '0', '--phase-floor-repeats', '0']
        orchestrator = super().orchestrator(Executing, args)
        with self.subprocess_run(orchestrator), mock.patch.object(orchestrator.source, 'open'), \
                mock.patch.object(orchestrator.source, 'close'):
            self.assertEqual(orchestrator.execute(), 0)
        run = json.loads((orchestrator.out / 'run.json').read_text())
        probackup = run['t2_preflight']['probackup']
        self.assertEqual(probackup['versions'], {'source': VERSION, 'dest': VERSION})
        self.assertTrue(Path(probackup['ssh_config']).is_file())


class InstallLink(unittest.TestCase):
    """infra/gcp/install_incumbents.sh puts pg_probackup-17 where the harness runs it on both VMs, whichever
    way each got it: Postgres Pro's package (/usr/bin, on PATH) or the source build (/usr/local/bin)."""

    SCRIPT = Path(__file__).resolve().parents[1] / 'infra' / 'gcp' / 'install_incumbents.sh'

    def link(self, binary: Path, link: Path):
        """The script's link_probackup on `binary`, with its link at `link` (bash; nothing else of the script)."""
        text = self.SCRIPT.read_text()
        start = text.index('link_probackup() {')
        function = text[start:text.index('\n}\n', start) + 3]
        script = ('set -euo pipefail\nfail() { echo "install_incumbents: $*" >&2; exit 1; }\n'
                  f'PROBACKUP_LINK={link}\n{function}link_probackup "$1"\n')
        return subprocess.run(['bash', '-c', script, 'bash', str(binary)], capture_output=True, text=True, timeout=60)

    @staticmethod
    def binary(path: Path, body='echo "pg_probackup-17 2.5.16 (PostgreSQL 17)"') -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\n{body}\n')
        path.chmod(0o755)
        return path

    def test_the_harnesss_path_runs_pg_probackup_whichever_way_it_came(self):
        self.assertEqual(measure.PROBACKUP_EXE, '/usr/local/bin/pg_probackup-17')
        text = self.SCRIPT.read_text()
        self.assertIn('PROBACKUP_LINK=/usr/local/bin/pg_probackup-$PG_MAJOR', text)
        # Called whatever PATH finds: before, the package's binary, already on PATH, got no link.
        self.assertIn('\nlink_probackup "$PROBACKUP"\n', text)
        self.assertNotIn('!= "$(command -v', text)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            link = root / 'usr/local/bin/pg_probackup-17'
            link.parent.mkdir(parents=True)
            package = self.binary(root / 'usr/bin/pg_probackup-17')
            result = self.link(package, link)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(os.readlink(link), str(package))
            self.assertEqual(self.link(package, link).returncode, 0)  # again: idempotent
            # The source build installs at the link's path itself: left as it is.
            link.unlink()
            built = self.binary(link)
            self.assertEqual(self.link(built, link).returncode, 0)
            self.assertFalse(link.is_symlink())
            # A binary that doesn't run fails the install.
            link.unlink()
            broken = self.binary(root / 'opt/pg_probackup-17', 'exit 127')
            result = self.link(broken, link)
            self.assertEqual(result.returncode, 1)
            self.assertIn(f'{link} does not run pg_probackup', result.stderr)


# --- the syscall audit in the orchestrator -------------------------------------------------------

# strace stand-in: writes a trace line naming the command to its -o file, then runs the command.
STUB_STRACE = """\
import subprocess, sys
args = sys.argv[1:]
out = args[args.index('-o') + 1]
command = args[args.index('--') + 1:]
with open(out, 'w') as stream:
    stream.write('4242 10:00:00.000001 openat(AT_FDCWD, "/x", O_RDONLY) = 3</x>\\n')
    stream.write('# ' + ' '.join(args[:args.index('-o')]) + ' -- ' + ' '.join(command) + '\\n')
sys.exit(subprocess.call(command))
"""


class Audit(core.Clocked):
    def test_child_runs_under_strace_inside_gnu_time_while_tracing(self):
        bin_dir = Path(self.tmp.name, 'bin')
        bin_dir.mkdir()
        strace = bin_dir / 'strace'
        strace.write_text(f'#!{os.path.realpath(sys.executable)}\n' + STUB_STRACE)
        strace.chmod(0o755)
        gnu_time = base.stub_gnu_time(self.tmp.name)
        log = Path(self.tmp.name, 'logs', 'probackup8.log')
        with mock.patch.dict(os.environ, {'PATH': f'{bin_dir}:{os.environ["PATH"]}'}):
            with mock.patch.object(run_exp04.time, 'perf_counter', __import__('time').perf_counter):
                run_exp04.Child.trace = True
                try:
                    traced = run_exp04.Child(['/bin/echo', 'copied'], log, gnu_time=gnu_time).wait(60)
                finally:
                    run_exp04.Child.trace = False
                plain = run_exp04.Child(['/bin/echo', 'copied'], Path(self.tmp.name, 'logs', 'plain.log'),
                                        gnu_time=gnu_time).wait(60)
        self.assertEqual(traced['exit_code'], 0)
        self.assertEqual(traced['argv'], ['/bin/echo', 'copied'])  # the tool's own, for tool_reported
        self.assertEqual(traced['launcher'][0], gnu_time)  # GNU time outside, so it never times strace's -o
        self.assertEqual(traced['peak_rss_kb'], 4321)
        self.assertEqual(traced['trace'], f'{log}.strace')
        self.assertIn('# -f -tt -y -e trace=%file,%desc -- /bin/echo copied', Path(traced['trace']).read_text())
        self.assertEqual(log.read_text(), 'copied\n')
        self.assertNotIn('trace', plain)

    class Audited(Probackup):
        """--syscall-audit with every dest command's trace written as if strace had run it."""

        def local_command(self, argv, log_name, *, timeout=None, cwd=None, env=None, stamp_lines=False):
            traced = run_exp04.Child.trace
            result = super().local_command(argv, log_name, timeout=timeout, cwd=cwd, env=env, stamp_lines=stamp_lines)
            self.events.append(('traced', Path(argv[0]).name, traced))
            if traced:
                output = str(self.outputs / 'probackup8')
                path = self.dest_log(log_name).with_name(self.dest_log(log_name).name + '.strace')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(self.catchup_trace(output) if Path(argv[0]).name.startswith('pg_probackup')
                                else self.sync_trace(output))
                result['trace'] = str(path)
            return result

        @staticmethod
        def catchup_trace(output):
            data, label = f'{output}/base/5/16397', f'{output}/backup_label'
            return (f'4242 10:00:00.000001 mkdir("{output}", 0700) = 0\n'
                    f'4243 10:00:00.000002 openat(AT_FDCWD, "{data}", O_WRONLY|O_CREAT|O_TRUNC, 0600) = 5<{data}>\n'
                    f'4243 10:00:00.000003 pwrite64(5<{data}>, "A"..., 8192, 0) = 8192\n'
                    f'4243 10:00:00.000004 close(5<{data}>) = 0\n'
                    f'4242 10:00:00.000005 openat(AT_FDCWD, "{label}", O_WRONLY|O_CREAT|O_TRUNC, 0600) = 6<{label}>\n'
                    f'4242 10:00:00.000006 write(6<{label}>, "START"..., 240) = 240\n'
                    f'4242 10:00:00.000007 close(6<{label}>) = 0\n'
                    f'4242 10:00:00.000008 openat(AT_FDCWD, "{data}", O_RDWR) = 5<{data}>\n'
                    f'4242 10:00:00.000009 fsync(5<{data}>) = 0\n')

        @staticmethod
        def sync_trace(output):
            return (f'5000 10:00:01.000001 openat(AT_FDCWD, "{output}", O_RDONLY|O_NONBLOCK) = 3<{output}>\n'
                    f'5000 10:00:01.000002 syncfs(3<{output}>) = 0\n')

    def test_a_timed_phases_commands_are_traced_and_audited(self):
        orchestrator = self.orchestrator(self.Audited, ['--syscall-audit', '--audit-filesystem', self.tmp.name])
        orchestrator.verify = lambda method, paths, phases: (  # its start runs a command too
            orchestrator.local_command(['/usr/lib/postgresql/17/bin/pg_ctl', 'start'], 'start.log')
            and {'verified': True, 'basis_changes': [], 'output_bytes': 8432})
        record = self.run_method(orchestrator, 'probackup8')
        self.assertEqual(record['status'], 'pass', record.get('traceback'))
        traced = [event[1:] for event in orchestrator.events if event[0] == 'traced']
        # Only the timed phase's commands: not the verification's start.
        self.assertEqual(traced, [('pg_probackup-17', True), ('sync', True), ('pg_ctl', False)])
        self.assertFalse(run_exp04.Child.trace)
        audited = record['audit']
        output = str(orchestrator.outputs / 'probackup8')
        self.assertEqual(audited['scope'], [output])
        self.assertTrue(audited['result']['ok'], audited['result'])
        self.assertEqual(len(audited['result']['traces']), 2)
        self.assertFalse(audited['tool_only']['ok'])
        self.assertEqual([entry['path'] for entry in audited['tool_only']['uncovered_files']], [f'{output}/backup_label'])
        self.assertIn('times nothing', audited['untimed'])

    def test_without_the_audit_nothing_is_traced(self):
        orchestrator = self.orchestrator(self.Audited)
        orchestrator.verify = lambda method, paths, phases: {'verified': True, 'basis_changes': [],
                                                              'output_bytes': 8432}
        record = self.run_method(orchestrator, 'probackup8')
        self.assertEqual({event[2] for event in orchestrator.events if event[0] == 'traced'}, {False})
        self.assertNotIn('audit', record)

    def test_the_audit_needs_strace_on_dest_and_is_recorded_in_run_json(self):
        class Executing(core.RunJson.Executing):
            pass

        orchestrator, code, run = core.RunJson.execute(self, ['full', 'full_nosync'], ['--syscall-audit'],
                                                       cls=Executing)
        self.assertEqual(code, 0)
        self.assertEqual(run['syscall_audit']['argv'], durability_audit.strace_argv('<log>.strace'))
        self.assertEqual(run['syscall_audit']['filesystem'], '/data')
        orchestrator.run['environment']['dest']['strace'] = None
        with self.assertRaises(SystemExit) as refused:
            orchestrator.preflight_audit()
        self.assertIn('--syscall-audit needs strace on dest', str(refused.exception))


if __name__ == '__main__':
    unittest.main()
