"""EXP-11's blocks together: the core, parcopy, pg_probackup with the syscall audit, and the analysis.

- the syscall audit's records, by arm (durability_audit.py summary --json), in the shape
  analysis/exp11.py --audit reads, and read by it from a results directory.

The analysis's parts need the analysis venv's matplotlib and are skipped without it.
"""

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

from experiments.network import durability_audit

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


if __name__ == '__main__':
    unittest.main()
