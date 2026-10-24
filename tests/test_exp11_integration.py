"""EXP-11's blocks together: the core, parcopy, pg_probackup with the syscall audit, and the analysis.

- the syscall audit's records, by arm (durability_audit.py summary --json), in the shape
  analysis/exp11.py --audit reads, and read by it from a results directory;
- parcopy's fsync count (A5) kept apart from check (a) in the analysis, as the harness records both;
- parcopy's listening time, so the analysis can place its last byte inside the transfer step.

The analysis's parts need the analysis venv's matplotlib and are skipped without it.
"""

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

from experiments.network import durability_audit, measure

import test_durability_audit as audit_tests

try:
    import matplotlib  # noqa: F401
except ImportError:  # the analysis scripts need the analysis venv
    matplotlib = None
if matplotlib is not None:
    import test_exp11_analysis as analysis_tests
    sys.path.append(str(Path(__file__).resolve().parents[1] / 'analysis'))
    import exp11

needs_venv = unittest.skipUnless(matplotlib is not None, 'needs the analysis venv (matplotlib)')


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
        self.assertIn(f'| parcopy8 | no | {len(left)} ({", ".join(left)}) | 0 |', lines)
        self.assertTrue(any(line.startswith('| probackup8 | yes | 0 |') and 'backup_label' in line for line in lines))


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
