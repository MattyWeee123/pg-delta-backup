"""Tests for benchmarks/output_check.py, the staged methods' output oracle."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from benchmarks import output_check
from benchmarks.backup import output_check_command, output_check_report


def can_symlink(directory):
    probe = Path(directory) / 'symlink-probe'
    try:
        probe.symlink_to(directory, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


class Trees(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.s, self.b, self.o = self.root / 'S', self.root / 'B', self.root / 'O'
        for tree in (self.s, self.b):
            (tree / 'base' / '1').mkdir(parents=True)
            (tree / 'pg_wal').mkdir()
            (tree / 'pg_notify').mkdir()
        (self.s / 'base' / '1' / '1259').write_bytes(b'n' * (3 << 20))
        (self.s / 'backup_manifest').write_text('{"new": 1}')
        (self.s / 'pg_wal' / '000000010000000000000002').write_bytes(b'wal')
        (self.b / 'base' / '1' / '1259').write_bytes(b'o' * (3 << 20))
        (self.b / 'backup_manifest').write_text('{"old": 1}')
        (self.b / 'pg_wal' / '000000010000000000000001').write_bytes(b'old')

    def copy(self):
        shutil.copytree(self.s, self.o, symlinks=True)

    def check(self, reference=True, **kwargs):
        return output_check.check(str(self.o), str(self.s) if reference else None,
                                  [str(self.s), str(self.b)], **kwargs)

    def assert_fails_with(self, *fragments, independent=None):
        status, report = self.check()
        self.assertEqual(status, output_check.EXIT_DIFFERENT, report)
        self.assertFalse(report['passed'])
        text = '\n'.join(report['differences'])
        for fragment in fragments:
            self.assertIn(fragment, text)
        if independent is not None:
            self.assertIs(report['independent'], independent)
        return report


class ContentTests(Trees):
    def test_exact_copy_passes(self):
        self.copy()
        status, report = self.check()
        self.assertEqual(status, output_check.EXIT_PASS, report)
        self.assertEqual(report['differences'], [])
        self.assertTrue(report['independent'] and report['identical'] and report['passed'])
        self.assertEqual(report['files_compared'], 3)
        self.assertEqual(report['bytes_compared'], (3 << 20) + len('{"new": 1}') + 3)

    def test_each_kind_of_difference_is_named(self):
        cases = {
            'missing file': (lambda: (self.o / 'backup_manifest').unlink(),
                             'backup_manifest (file) is missing'),
            'extra file': (lambda: (self.o / 'standby.signal').write_text(''),
                           'standby.signal (file) is in the output but not in'),
            'extra WAL': (lambda: shutil.copy(self.b / 'pg_wal' / '000000010000000000000001',
                                              self.o / 'pg_wal'),
                          'pg_wal/000000010000000000000001 (file) is in the output'),
            'missing empty directory': (lambda: (self.o / 'pg_notify').rmdir(),
                                        'pg_notify (directory) is missing'),
            'extra empty directory': (lambda: (self.o / 'pg_stale').mkdir(),
                                      'pg_stale (directory) is in the output'),
            'type': (lambda: ((self.o / 'pg_notify').rmdir(),
                              (self.o / 'pg_notify').write_text('')),
                     'pg_notify is a file in the output but a directory'),
            'size': (lambda: (self.o / 'backup_manifest').write_text('{"old": 12}'),
                     'backup_manifest is 11 bytes in the output, 10 in'),
        }
        for name, (damage, message) in cases.items():
            with self.subTest(name=name):
                if self.o.exists():
                    shutil.rmtree(self.o)
                self.copy()
                damage()
                report = self.assert_fails_with(message, independent=True)
                self.assertIs(report['identical'], False)

    def test_same_size_content_difference_reports_the_first_differing_byte(self):
        self.copy()
        offset = (1 << 20) + 4096 * 3 + 17  # past the first read chunk and slice
        with (self.o / 'base' / '1' / '1259').open('r+b') as stream:
            stream.seek(offset)
            stream.write(b'X')
        self.assert_fails_with(f'base/1/1259 differs from {self.s} at byte {offset}')

    def test_symlink_targets_must_match(self):
        if not can_symlink(self.root):
            self.skipTest('symlinks unavailable')
        (self.s / 'link').symlink_to('base')
        self.copy()
        self.assertEqual(self.check()[0], output_check.EXIT_PASS)
        (self.o / 'link').unlink()
        (self.o / 'link').symlink_to('pg_wal')
        self.assert_fails_with("link links to 'pg_wal', not 'base'")

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'needs FIFOs')
    def test_special_files_are_never_accepted(self):
        os.mkfifo(self.s / 'fifo')
        shutil.copytree(self.s, self.o, ignore=lambda *_: ['fifo'])
        os.mkfifo(self.o / 'fifo')
        self.assert_fails_with('fifo is a special file')

    def test_differences_are_capped(self):
        self.copy()
        for index in range(30):
            (self.o / f'extra-{index:02d}').write_text('')
        status, report = self.check(limit=7)
        self.assertEqual(status, output_check.EXIT_DIFFERENT)
        self.assertEqual(len(report['differences']), 7)
        self.assertTrue(report['truncated'])

    @unittest.skipIf(os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0),
                     'permission bits are not enforced')
    def test_unreadable_directory_is_an_error_not_a_pass(self):
        self.copy()
        locked = self.o / 'base'
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o755)
        status, report = self.check()
        self.assertEqual(status, output_check.EXIT_ERROR)
        self.assertFalse(report['passed'])
        self.assertIn('PermissionError', report['error'])


    def test_unexpected_failure_is_an_error_not_a_difference(self):
        self.copy()
        with mock.patch.object(output_check, 'first_difference', side_effect=RuntimeError('boom')):
            status, report = self.check()
        self.assertEqual(status, output_check.EXIT_ERROR)
        self.assertEqual(report['error'], 'RuntimeError: boom')
        self.assertFalse(report['passed'])

    def test_missing_output_is_an_error(self):
        status, report = self.check()
        self.assertEqual(status, output_check.EXIT_ERROR)
        self.assertIn('FileNotFoundError', report['error'])


class IndependenceTests(Trees):
    def test_symlinked_output_is_refused_before_comparing(self):
        if not can_symlink(self.root):
            self.skipTest('symlinks unavailable')
        self.o.symlink_to(self.s, target_is_directory=True)
        report = self.assert_fails_with('is a symbolic link to', independent=False)
        self.assertIsNone(report['identical'])
        self.assertEqual(report['files_compared'], 0)

    def test_output_overlapping_s_or_b_is_refused(self):
        self.o = self.s
        self.assert_fails_with('is the same directory as', independent=False)
        self.o = self.b / 'pg_notify'
        self.assert_fails_with('lies inside', independent=False)
        self.o = self.root
        self.assert_fails_with('lies inside', independent=False)

    def test_hard_link_to_an_identical_basis_file_is_refused(self):
        """Content alone cannot catch it: S and B hold the same bytes here."""
        shutil.copy(self.s / 'pg_wal' / '000000010000000000000002', self.b / 'pg_wal')
        self.copy()
        wal = self.o / 'pg_wal' / '000000010000000000000002'
        wal.unlink()
        os.link(self.b / 'pg_wal' / '000000010000000000000002', wal)
        self.assert_fails_with('pg_wal/000000010000000000000002 is a hard link to '
                               + os.path.join(str(self.b), 'pg_wal', '000000010000000000000002'),
                               independent=False)

    def test_hard_link_to_anywhere_else_is_refused(self):
        self.copy()
        os.link(self.o / 'backup_manifest', self.root / 'elsewhere')
        self.assert_fails_with('backup_manifest has 2 hard links', independent=False)

    def test_symlink_into_the_basis_is_refused(self):
        if not can_symlink(self.root):
            self.skipTest('symlinks unavailable')
        self.copy()
        (self.o / 'stale').symlink_to(self.b / 'base')
        self.assert_fails_with(f'stale is a symbolic link into {self.b}', independent=False)

    def test_independence_alone_without_a_reference(self):
        self.copy()
        (self.o / 'unrelated').write_text('')
        status, report = self.check(reference=False)
        self.assertEqual(status, output_check.EXIT_PASS, report)
        self.assertIsNone(report['identical'])


class ScriptTests(Trees):
    """The harness passes the module source to python -I -c where commands run."""

    def run_script(self):
        command = output_check_command(sys.executable, self.o, self.s, [self.s, self.b])
        # From an empty directory, so nothing from this repository is importable.
        cwd = self.root / 'elsewhere'
        cwd.mkdir()
        done = subprocess.run(command, capture_output=True, text=True, cwd=cwd, timeout=60,
                              env={key: value for key, value in os.environ.items()
                                   if key != 'PYTHONPATH'})
        return done.returncode, output_check_report(done.stdout), done

    def test_isolated_script_passes_an_exact_copy(self):
        self.copy()
        code, report, done = self.run_script()
        self.assertEqual(code, 0, done.stderr)
        self.assertTrue(report['passed'])
        self.assertEqual(report['files_compared'], 3)

    def test_isolated_script_reports_differences_and_exits_one(self):
        self.copy()
        (self.o / 'backup_manifest').write_text('{"old": 1}')
        code, report, done = self.run_script()
        self.assertEqual(code, 1, done.stderr)
        self.assertIn('backup_manifest differs', report['differences'][0])
        self.assertIn('backup_manifest differs', done.stdout.splitlines()[0])

    def test_output_path_cannot_be_mistaken_for_an_option(self):
        self.copy()
        command = output_check_command('py', '-O', '/s', ['/s', '/b'])
        self.assertEqual(command[:3], ['py', '-I', '-c'])
        self.assertEqual(command[-2:], ['--', '-O'])
        self.assertEqual(command[4:], ['--reference', '/s', '--independent-of', '/s',
                                       '--independent-of', '/b', '--', '-O'])

    def test_report_is_found_after_a_logged_command_line(self):
        source_line = "    print(REPORT_PREFIX + json.dumps(report))"
        log = f'$ python -I -c """doc"""\n{source_line}\n\nok\nOUTPUT_CHECK {{"passed": true}}\n'
        self.assertEqual(output_check_report(log), {'passed': True})
        self.assertIsNone(output_check_report('$ python -I -c ...\nno report\n'))
        with self.assertRaises(ValueError):
            output_check_report('OUTPUT_CHECK [1]\n')


if __name__ == '__main__':
    unittest.main()
