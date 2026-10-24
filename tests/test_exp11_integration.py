"""EXP-11's blocks together: the core, parcopy, pg_probackup with the syscall audit, and the analysis.

- the specification's run lines, read from docs/experiments/EXP-11-full-copy-baseline.md, parse through
  remote.py start and run_exp04.parse_args, and plan the registered number of runs;
- the syscall audit's records, by arm (durability_audit.py summary --json), in the shape
  analysis/exp11.py --audit reads, and read by it from a results directory;
- parcopy's fsync count (A5) kept apart from check (a) in the analysis, as the harness records both;
- a copy parcopy itself aborted (a dropped range) through run_method and the analysis, which A4 excludes;
- parcopy's listening time, so the analysis can place its last byte inside the transfer step.

The analysis's parts need the analysis venv's matplotlib and are skipped without it.
"""

import io
import json
from pathlib import Path
import re
import shlex
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

from experiments.network import durability_audit, measure, remote, run_exp04

import test_durability_audit as audit_tests
import test_parcopy as parcopy_tests

try:
    import matplotlib  # noqa: F401
except ImportError:  # the analysis scripts need the analysis venv
    matplotlib = None
if matplotlib is not None:
    import test_exp11_analysis as analysis_tests
    sys.path.append(str(Path(__file__).resolve().parents[1] / 'analysis'))
    import exp11

needs_venv = unittest.skipUnless(matplotlib is not None, 'needs the analysis venv (matplotlib)')
SPEC = Path(__file__).resolve().parents[1] / 'docs' / 'experiments' / 'EXP-11-full-copy-baseline.md'


def spec_run_lines() -> dict:
    """{invocation name: run_exp04's arguments} from the specification's "Run lines" block, expanded
    and split as a POSIX shell would, then as remote.py start hands them to run_exp04.py."""
    text = SPEC.read_text()
    block = re.search(r'\*\*Run lines\*\*.*?```sh\n(.*?)```', text, re.S).group(1)
    variables, lines = {}, {}
    for line in block.replace('\\\n', ' ').splitlines():
        assignment = re.match(r'^(\w+)=(.*)$', line.strip())
        if assignment:
            variables[assignment.group(1)] = ' '.join(shlex.split(assignment.group(2), comments=True))
            continue
        # Unquoted $M and $S, as the block writes them: expanded, then split on whitespace.
        line = re.sub(r'\$(\w+)', lambda m: variables[m.group(1)], line)
        words = shlex.split(line, comments=True)  # '#' starts a comment, as in a POSIX shell
        if words[:2] != ['python3', 'experiments/network/remote.py']:
            continue
        args = remote.build_parser().parse_args(words[2:])
        lines[args.name] = args.args[1:] if args.args[:1] == ['--'] else args.args
    return lines


class RunLines(unittest.TestCase):
    """EXP-11's "Run plan and budget": its run lines, exactly as written there."""

    def parsed(self):
        lines = spec_run_lines()
        self.assertEqual(sorted(lines), ['exp11-local', 'exp11-uniform'])
        return {name: run_exp04.parse_args(['--source-ip', '192.0.2.2', '--out', '/nonexistent/exp11', *argv])
                for name, argv in lines.items()}

    def test_they_parse_and_name_only_known_methods(self):
        for name, args in self.parsed().items():
            with self.subTest(name=name):
                self.assertEqual(args.manifest_checksums, 'CRC32C')
                methods = run_exp04.method_list(args.methods)
                self.assertEqual([m for m in methods if m not in measure.ALL_METHODS], [])
                self.assertIsNone(run_exp04.checksum_problem(methods, args.manifest_checksums))
                self.assertEqual(run_exp04.block_problems(args, methods), [])

    def test_they_carry_the_idle_noise_into_check_a(self):
        # Check (a)'s limit is 4 MiB or the smoke run's idle noise, whichever is larger: each sweep must be
        # given that noise (or measure its own), or it judges check (a) against 4 MiB alone.
        for name, args in self.parsed().items():
            with self.subTest(name=name):
                self.assertTrue(args.dirty_noise_mib > 0 or args.idle_noise_windows > 0,
                                (args.dirty_noise_mib, args.idle_noise_windows))

    def test_they_plan_the_registered_runs(self):
        totals = {}
        for name, args in self.parsed().items():
            speeds = measure.parse_speeds(args.speeds)
            methods = run_exp04.method_list(args.methods)
            limited = measure.parse_method_speeds(args.method_speeds, methods, speeds)
            pairs = sum(len(measure.methods_at(methods, limited, speed['label'])) for speed in speeds)
            patterns = args.patterns.split(',')
            blocks = {'main': args.repeats * pairs,
                      'extension': args.extension_repeats * len(run_exp04.method_list(args.extension_methods)),
                      'warm_source': args.warm_source_repeats * len(run_exp04.method_list(args.warm_source_methods))}
            totals[name] = {'pairs': pairs, 'patterns': patterns,
                            **{block: count * len(patterns) for block, count in blocks.items()}}
        # 25 arm-speed pairs per (trial, repeat) in hot and append, 24 in uniform; 250 + 80 + 18, then 120.
        self.assertEqual(totals['exp11-local'], {'pairs': 25, 'patterns': ['hot', 'append'], 'main': 250,
                                                 'extension': 80, 'warm_source': 18})
        self.assertEqual(totals['exp11-uniform'], {'pairs': 24, 'patterns': ['uniform'], 'main': 120,
                                                   'extension': 0, 'warm_source': 0})
        self.assertEqual(sum(total['main'] + total['extension'] + total['warm_source'] for total in totals.values()),
                         468)

    def test_every_arm_is_run_and_verified_by_its_block(self):
        class Dispatch:
            """Each block's run_* and verify_* replaced by its own name."""
            def __getattr__(self, name):
                if name.startswith(('run_', 'verify_')):
                    return lambda *args: name
                raise AttributeError(name)

        arms = {'full': ('run_full', None), 'full_nosync': ('run_full', None), 'full_syncfs': ('run_full', None),
                'full_wb': ('run_full', None), 'probackup4': ('run_probackup', 'verify_probackup'),
                'probackup8': ('run_probackup', 'verify_probackup'), 'parcopy4': ('run_parcopy', 'verify_direct'),
                'parcopy8': ('run_parcopy', 'verify_direct'),
                'pgdelta_direct_inplace': ('run_pgdelta_direct', 'verify_direct'),
                'pgdelta_direct_inplace_j8': ('run_pgdelta_direct', 'verify_direct')}
        local = self.parsed()['exp11-local']
        self.assertEqual(sorted(run_exp04.method_list(local.methods)), sorted(arms))
        for arm, (runner, verifier) in arms.items():
            with self.subTest(arm=arm):
                self.assertIn(arm, measure.TIERS)
                self.assertEqual(run_exp04.Orchestrator.run_phases(Dispatch(), arm, {}, {}), runner)
                if verifier:  # the pg_basebackup arms' pg_verifybackup and start are the core's tests
                    self.assertEqual(run_exp04.Orchestrator.verify(Dispatch(), arm, {}, {}), verifier)


def audited_record(trial_id, method, scope, result, tool_only=None) -> dict:
    record = {'trial_id': trial_id, 'method': method,
              'audit': {'untimed': 'traced', 'scope': [scope], 'result': result}}
    if tool_only is not None:
        record['audit']['tool_only'] = tool_only
    return record


def write_audited_run(root: Path) -> Path:
    """A run_exp04 results directory with audited records: probackup8 (its catchup alone leaves the WAL,
    backup_label and their directory entries, its sync -f covers them), full (passes), parcopy8 (a file
    never fsynced), and a record without an audit."""
    pb, out = audit_tests.PB, audit_tests.OUT
    catchup, sync_f = audit_tests.ProbackupLike().catchup(), audit_tests.ProbackupLike().sync_f()
    full = audit_tests.trace(audit_tests.opened(f'{out}/base/1/1', 3), audit_tests.wrote(f'{out}/base/1/1', 3),
                             audit_tests.synced(f'{out}/base/1/1', 3), audit_tests.closed(f'{out}/base/1/1', 3),
                             f'syncfs(4</data>) = 0')
    parcopy = '/data/exp04/out/parcopy8'
    unsynced = audit_tests.trace(f'mkdir("{parcopy}", 0700) = 0', audit_tests.opened(f'{parcopy}/f', 3),
                                 audit_tests.wrote(f'{parcopy}/f', 3), audit_tests.closed(f'{parcopy}/f', 3))
    runs = root / 'runs' / '00-hot-f0.01-s5'
    runs.mkdir(parents=True)
    records = {
        'r0-unthrottled-probackup8': audited_record('00-hot-f0.01-s5', 'probackup8', pb,
                                                    durability_audit.audit([catchup, sync_f], [pb]),
                                                    durability_audit.audit([catchup], [pb])),
        'r0-unthrottled-full': audited_record('00-hot-f0.01-s5', 'full', out, durability_audit.audit([full], [out])),
        'r0-unthrottled-parcopy8': audited_record('00-hot-f0.01-s5', 'parcopy8', parcopy,
                                                  durability_audit.audit([unsynced], [parcopy])),
        'r0-unthrottled-full_nosync': {'trial_id': '00-hot-f0.01-s5', 'method': 'full_nosync'},
    }
    for name, record in records.items():
        (runs / f'{name}.json').write_text(json.dumps(record))
    return root


class AuditByArm(unittest.TestCase):
    """durability_audit's records by arm: what analysis/exp11.py --audit reads."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = write_audited_run(Path(self.tmp.name, 'smoke'))

    def test_each_arm_passed_and_what_it_left(self):
        arms = durability_audit.arm_results([self.run_dir])['arms']
        self.assertEqual(sorted(arms), ['full', 'parcopy8', 'probackup8'])
        self.assertEqual((arms['full']['passed'], arms['full']['uncovered'], arms['full']['runs']), (True, [], 1))
        self.assertNotIn('tool_uncovered', arms['full'])
        probackup = arms['probackup8']
        self.assertEqual((probackup['passed'], probackup['uncovered']), (True, []))
        # Relative to the output, as the analysis's E13 clause reads them.
        self.assertTrue({*audit_tests.ProbackupLike.WAL, 'backup_label'} <= set(probackup['tool_uncovered']),
                        probackup['tool_uncovered'])
        self.assertFalse(any(path.startswith('/') for path in probackup['tool_uncovered']))
        self.assertEqual((arms['parcopy8']['passed'], arms['parcopy8']['uncovered'][0]), (False, 'f'))

    @needs_venv
    def test_counts_are_the_audits_not_the_number_of_example_paths(self):
        # EXP-11's recheck: full_nosync left all 992 files and 1,021 directory changes uncovered, but the
        # analysis printed 33, the merged and deduplicated first 20 examples of each kind.
        nosync = '/data/exp04/out/full_nosync'
        names = [f'{nosync}/base/1/{n}' for n in range(30)]
        calls = [f'mkdir("{nosync}", 0700) = 0']
        for fd, path in enumerate(names, start=3):
            calls += [audit_tests.opened(path, fd), audit_tests.wrote(path, fd), audit_tests.closed(path, fd)]
        result = durability_audit.audit([audit_tests.trace(*calls)], [nosync])
        self.assertEqual((result['uncovered_file_count'], len(result['uncovered_files'])),
                         (30, durability_audit.LIMIT))
        runs = self.run_dir / 'runs' / '00-hot-f0.01-s5'
        (runs / 'r0-unthrottled-full_nosync.json').write_text(
            json.dumps(audited_record('00-hot-f0.01-s5', 'full_nosync', nosync, result)))
        arms = durability_audit.arm_results([self.run_dir])['arms']
        arm = arms['full_nosync']
        self.assertFalse(arm['passed'])
        self.assertEqual((arm['files'], arm['uncovered_files']), (30, 30))
        self.assertEqual(arm['uncovered_directory_changes'], result['uncovered_directory_change_count'])
        self.assertLess(len(arm['uncovered']), arm['uncovered_files'])  # the examples are no count
        cell = exp11.audit_cell(arm)
        self.assertTrue(cell.startswith(f'30 of 30 files, {arm["uncovered_directory_changes"]} of '
                                        f'{arm["directory_changes"]} directory changes ('), cell)
        # pg_probackup's own flush: its counts carry the tool_ prefix, files and directory changes apart.
        probackup = arms['probackup8']
        self.assertGreater(probackup['tool_uncovered_files'], 0)
        self.assertGreater(probackup['tool_uncovered_directory_changes'], 0)
        self.assertEqual((probackup['uncovered_files'], probackup['uncovered_directory_changes']), (0, 0))
        self.assertTrue(exp11.audit_cell(probackup, 'tool_').startswith(
            f'{probackup["tool_uncovered_files"]} of {probackup["tool_files"]} files, '
            f'{probackup["tool_uncovered_directory_changes"]} of '))
        # A summary without the counts (an older --audit JSON) can't give one: it says its paths are examples.
        self.assertEqual(exp11.audit_cell({'uncovered': ['a', 'b']}), '2 paths, examples only (a, b)')
        self.assertEqual(exp11.audit_cell({'uncovered': []}), '0')
        self.assertEqual(exp11.audit_cell(arms['full'], 'tool_'), '0')

    def test_runs_of_several_directories_pool_by_arm(self):
        one = durability_audit.arm_results([self.run_dir])['arms']
        other = write_audited_run(Path(self.tmp.name, 'again'))
        arms = durability_audit.arm_results([self.run_dir, other])['arms']
        self.assertEqual(arms['full']['runs'], 2)
        self.assertEqual(arms['parcopy8']['uncovered'], one['parcopy8']['uncovered'])  # each path once

    def test_an_unknown_result_leaves_passed_unknown(self):
        record = json.loads((self.run_dir / 'runs' / '00-hot-f0.01-s5' / 'r0-unthrottled-full.json').read_text())
        record['audit']['result']['ok'] = None
        (self.run_dir / 'runs' / '00-hot-f0.01-s5' / 'r1-unthrottled-full.json').write_text(json.dumps(record))
        self.assertIsNone(durability_audit.arm_results([self.run_dir])['arms']['full']['passed'])

    def test_summary_json_on_the_command_line(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = durability_audit.main(['summary', '--json', str(self.run_dir)])
        self.assertEqual(code, 0)  # printed whole, though parcopy8 failed
        self.assertEqual(json.loads(out.getvalue()), durability_audit.arm_results([self.run_dir]))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(durability_audit.main(['summary', str(self.run_dir)]), 0)
        self.assertIn('parcopy8', out.getvalue())
        empty = Path(self.tmp.name, 'empty')
        empty.mkdir()
        for flags in ((), ('--json',)):
            with self.subTest(flags=flags), self.assertRaises(SystemExit):
                durability_audit.main(['summary', *flags, str(empty)])


@needs_venv
class AuditInTheAnalysis(unittest.TestCase):
    def test_the_analysis_reads_the_audit_from_a_results_directory_or_its_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = write_audited_run(Path(tmp, 'smoke'))
            arms = exp11.load_audit(str(run_dir))
            self.assertEqual(arms, durability_audit.arm_results([run_dir])['arms'])
            saved = Path(tmp, 'audit.json')
            saved.write_text(json.dumps(durability_audit.arm_results([run_dir])))
            self.assertEqual(exp11.load_audit(str(saved)), arms)
        # E13's clause: not every arm passes (parcopy8), and pg_probackup's own flush leaves the WAL and
        # backup_label to its sync -f.
        self.assertEqual(exp11.audit_word(arms), "every arm passes failed; pg_probackup's own flush leaves WAL and "
                                                 'backup_label to its sync -f held')
        lines = exp11.durability_lines(analysis_tests.session(analysis_tests.runs('full', [3.0])), arms)
        left = arms['parcopy8']['uncovered']
        self.assertIn(f'| parcopy8 | no | 1 of 1 files, 2 of 2 directory changes ({", ".join(left)}) | 0 |', lines)
        self.assertTrue(any(line.startswith('| probackup8 | yes | 0 of ') and 'backup_label' in line for line in lines))


@needs_venv
class FsyncCountApartFromCheckA(unittest.TestCase):
    """measure.durability_check folds parcopy's fsync count into a_ok; the analysis keeps them apart."""

    def record(self, calls=12, expected=12, held=12, increase=0):
        record = {'method': 'parcopy8',
                  'phases': {measure.DIRECT_PHASE: {'memory': {'dest': {'dirty_writeback_bytes_before': 1000,
                                                                        'dirty_writeback_bytes_after': 1000 + increase}},
                                                    'disk': {'dest': {'md127': {'write_bytes': 10 << 20,
                                                                                'read_bytes': 0}}}}},
                  'tool_reported': {'parcopy_summary': measure.parcopy_summary(
                      {'streams': [{'bytes': 1 << 20}], 'fsync_calls': calls, 'fsync_expected': expected})},
                  'verification': {'fsync_expected_from_output': held}}
        record['durability'] = measure.durability_check(record, 10 << 20)
        return record

    def loaded(self, record):
        row = {'method': 'parcopy8', 'dirty_increase_mb': None, 'disk_write_dest_bytes': None, 'output_bytes': None}
        return exp11.from_record(record, row, {})

    def test_a_count_the_output_disagrees_with_fails_a5_not_check_a(self):
        record = self.record(held=13)  # the receiver's counts agree; the output holds one more entry
        self.assertEqual((record['durability']['a_ok'], record['durability']['fsync_count']['ok']), (False, False))
        self.assertEqual((self.loaded(record)['a_ok'], self.loaded(record)['fsync_ok']), (True, False))
        dirty = self.record(increase=20 << 20)
        self.assertEqual((self.loaded(dirty)['a_ok'], self.loaded(dirty)['fsync_ok']), (False, True))
        unknown = self.record(calls=None)
        self.assertEqual((self.loaded(unknown)['a_ok'], self.loaded(unknown)['fsync_ok']), (True, None))
        full = {'method': 'full', 'durability': {'a_ok': False, 'b_ok': True}}
        self.assertEqual(self.loaded(full)['a_ok'], False)  # other arms: the record's own check (a)

    def test_a5_holds_when_check_a_is_void(self):
        rows = analysis_tests.runs('parcopy8', [1.0], fsync_ok=False) + analysis_tests.runs(
            'parcopy8', [1.1], first=1) + analysis_tests.runs('full_nosync', [2.0], a_ok=True, b_ok=False)
        S = analysis_tests.session(rows)
        self.assertTrue(S.a_void)
        # The summary's counts agree (10 and 10); the record's count, which also checks the output, doesn't.
        self.assertEqual(S.rows[0]['left_out'], 'durability failed: fsync count')
        self.assertTrue(S.rows[1]['usable'])  # without the record's count, the summary's decides


@needs_venv
class AbortedCopyInTheAnalysis(unittest.TestCase):
    """A4 across the blocks: parcopy's receiver catches a dropped range and exits 3, run_method ends the run in
    the tool's own error before any verification, and the analysis excludes the arm from every baseline."""

    def test_a_dropped_range_through_run_method_and_the_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, stub, _ = parcopy_tests.synthetic_source(root)
            _, outcome, _ = parcopy_tests.dropped_range_copy(source, stub, root / 'O', 4)
            orchestrator, record = parcopy_tests.aborted_run(root / 'harness', parcopy_tests.exit_statuses(outcome))
            self.assertEqual((record['status'], record['failure_kind']), ('error', 'tool'))
            rows, trials, _ = exp11.load(orchestrator.out)
        [row] = rows
        self.assertEqual((row['method'], row['status'], row['failure_kind']), ('parcopy4', 'error', 'tool'))
        S = exp11.Session(rows, trials)
        self.assertEqual(list(S.a4), ['parcopy4'])
        self.assertTrue(S.excluded['parcopy4'].startswith('A4: 1 run(s) failed'))
        self.assertIsNone(S.choice('B_fast', row['speed']))
        lines = exp11.a_lines(S, [], {'row': 'Undecided'}, {'row': 'Undecided'})
        self.assertTrue(any(line.startswith('- **A4**') and 'fires for parcopy4' in line for line in lines), lines)


class ParcopyListening(unittest.TestCase):
    def test_the_receivers_listening_time_reaches_the_csv(self):
        summary = measure.parcopy_summary({'listening_s': 0.21, 'streams': [{'bytes': 4, 'last_byte_s': 1.9}]})
        self.assertEqual((summary['listening_s'], summary['last_byte_s']), (0.21, 1.9))
        self.assertIn('parcopy_listening_s', measure.CSV_FIELDS)
        row = measure.summary_row({'method': 'parcopy8', 'tool_reported': {'parcopy_summary': summary}})
        self.assertEqual(row['parcopy_listening_s'], 0.21)

    @needs_venv
    def test_the_phases_figure_places_the_last_byte_inside_the_transfer_step(self):
        run = {key: None for key in exp11.NUMERIC}
        run.update(method='parcopy8', total_s=2.0, receiver_start_s=0.3, transfer_s=1.7, parcopy_listening_s=0.25,
                   parcopy_last_byte_s=1.25, parcopy_fsync_s=0.5)
        launch, mode, copy, flush, other = exp11.phase_parts(run)
        self.assertEqual((launch, mode, flush), (0.3, 0.0, 0.5))
        self.assertAlmostEqual(copy, 1.0)
        self.assertAlmostEqual(other, 0.2)


if __name__ == '__main__':
    unittest.main()
