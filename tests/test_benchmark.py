"""Tests for the v0.1 benchmark runner.

No PostgreSQL is required. Command execution is exercised with short Python
subprocesses, and main() runs end to end against stubs that imitate
pg_basebackup and pg_verifybackup.
"""

import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from benchmarks.benchmark import (
    EXIT_INVALID_INPUT,
    EXIT_IO_FAILURE,
    EXIT_OK,
    EXIT_VERIFICATION_FAILURE,
    METHODS,
    RESULT_KEYS,
    assert_no_overlap,
    build_result,
    classify,
    directory_bytes,
    main,
    new_run_id,
    parse_args,
    run_command,
    verify_command,
)

STUB_BASEBACKUP = '''
import os, sys
argv = sys.argv[1:]
if "--version" in argv:
    print("pg_basebackup (PostgreSQL) 17.0 stub")
    sys.exit(0)
code = int(os.environ.get("STUB_BACKUP_EXIT", "0"))
if code == 0:
    target = argv[argv.index("--pgdata") + 1]
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(target, "backup_manifest"), "w") as handle:
        handle.write('{"PostgreSQL-Backup-Manifest-Version": 1}')
    with open(os.path.join(target, "base.dat"), "wb") as handle:
        handle.write(b"x" * 2048)
print("stub pg_basebackup finished")
sys.exit(code)
'''

STUB_VERIFYBACKUP = '''
import os, sys
if "--version" in sys.argv[1:]:
    print("pg_verifybackup (PostgreSQL) 17.0 stub")
    sys.exit(0)
print("stub pg_verifybackup finished")
sys.exit(int(os.environ.get("STUB_VERIFY_EXIT", "0")))
'''

MANIFEST_BYTES = len('{"PostgreSQL-Backup-Manifest-Version": 1}')


def make_stub(directory, name, body):
    """Write an executable stub that runs body with the current interpreter."""
    script = directory / f'{name}_impl.py'
    script.write_text(body, encoding='utf-8')
    if os.name == 'nt':
        launcher = directory / f'{name}.cmd'
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding='utf-8')
    else:
        launcher = directory / name
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding='utf-8')
        launcher.chmod(0o755)
    return launcher


def python_command(source):
    return [sys.executable, '-c', source]


def outcome(exit_code: int | None = 0, duration: float = 1.0,
            error: str | None = None):
    return {'exit_code': exit_code, 'duration_seconds': duration, 'error': error}


class RunCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_records_success_and_captures_stdout(self):
        log = self.directory / 'out.log'
        result = run_command(python_command('print("hello benchmark")'), log, 60)
        self.assertEqual(result['exit_code'], 0)
        self.assertIsNone(result['error'])
        self.assertGreaterEqual(result['duration_seconds'], 0.0)
        self.assertIn('hello benchmark', log.read_text(encoding='utf-8'))

    def test_logs_the_command_line(self):
        log = self.directory / 'out.log'
        run_command(python_command('pass'), log, 60)
        self.assertTrue(log.read_text(encoding='utf-8').startswith('$ '))

    def test_records_nonzero_exit_code(self):
        result = run_command(
            python_command('import sys; sys.exit(3)'), self.directory / 'out.log', 60)
        self.assertEqual(result['exit_code'], 3)
        self.assertIsNone(result['error'])

    def test_captures_stderr_into_the_same_log(self):
        log = self.directory / 'out.log'
        run_command(
            python_command('import sys; sys.stderr.write("boom\\n")'), log, 60)
        self.assertIn('boom', log.read_text(encoding='utf-8'))

    def test_reports_missing_binary(self):
        log = self.directory / 'out.log'
        result = run_command([str(self.directory / 'definitely-absent')], log, 60)
        self.assertIsNone(result['exit_code'])
        self.assertIsNotNone(result['error'])
        self.assertIn('ERROR', log.read_text(encoding='utf-8'))

    def test_reports_timeout(self):
        result = run_command(
            python_command('import time; time.sleep(30)'),
            self.directory / 'out.log', 0.25)
        self.assertIsNone(result['exit_code'])
        self.assertIsNotNone(result['error'])


class DirectoryBytesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_sums_nested_regular_files(self):
        (self.directory / 'nested').mkdir()
        (self.directory / 'a.dat').write_bytes(b'x' * 10)
        (self.directory / 'nested' / 'b.dat').write_bytes(b'y' * 25)
        self.assertEqual(directory_bytes(self.directory), 35)

    def test_empty_directory_is_zero(self):
        self.assertEqual(directory_bytes(self.directory), 0)

    def test_missing_directory_is_none(self):
        self.assertIsNone(directory_bytes(self.directory / 'absent'))

    def test_ignores_symlinked_files(self):
        target = self.directory / 'real.dat'
        target.write_bytes(b'z' * 16)
        try:
            (self.directory / 'link.dat').symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest('symlink creation not permitted on this host')
        self.assertEqual(directory_bytes(self.directory), 16)


class CommandBuilderTests(unittest.TestCase):
    def build(self, *extra):
        args = parse_args(['--host', 'db.example', '--port', '6543',
                           '--user', 'bench', *extra])
        args.pg_basebackup = 'pg_basebackup'
        args.pg_verifybackup = 'pg_verifybackup'
        return args, METHODS[args.method](args, Path('/tmp/out/backup'))

    def test_includes_connection_and_manifest_flags(self):
        _, command = self.build()
        self.assertIn('--host', command)
        self.assertIn('db.example', command)
        self.assertIn('6543', command)
        self.assertIn('bench', command)
        self.assertIn('--wal-method', command)
        self.assertIn('stream', command)
        self.assertIn('--manifest-checksums', command)
        self.assertIn('SHA256', command)

    def test_never_prompts_and_carries_no_password(self):
        _, command = self.build()
        self.assertIn('--no-password', command)
        joined = ' '.join(command).lower()
        self.assertNotIn('password=', joined)
        self.assertNotIn('pgpassword', joined)

    def test_manifest_checksums_is_selectable(self):
        _, command = self.build('--manifest-checksums', 'CRC32C')
        self.assertIn('CRC32C', command)
        self.assertNotIn('SHA256', command)

    def test_verify_command_targets_the_backup_directory(self):
        args, _ = self.build()
        command = verify_command(args, Path('/tmp/out/backup'))
        self.assertEqual(command[0], 'pg_verifybackup')
        self.assertIn('backup', command[-1])


class ClassifyTests(unittest.TestCase):
    def test_status_and_exit_code_mapping(self):
        cases = [
            (outcome(), outcome(), 'PASS_MANIFEST_ONLY', EXIT_OK),
            (outcome(exit_code=1), None, 'FAIL_BACKUP', EXIT_IO_FAILURE),
            (outcome(exit_code=None, error='timed out'), None,
             'FAIL_BACKUP', EXIT_IO_FAILURE),
            (outcome(), outcome(exit_code=1), 'FAIL_VERIFY',
             EXIT_VERIFICATION_FAILURE),
            (outcome(), outcome(exit_code=None, error='missing'),
             'FAIL_VERIFY', EXIT_VERIFICATION_FAILURE),
        ]
        for backup, verification, status, code in cases:
            with self.subTest(status=status):
                self.assertEqual(classify(backup, verification), (status, code))


class OverlapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_rejects_destination_inside_source(self):
        with self.assertRaises(ValueError):
            assert_no_overlap(self.directory / 'pgdata' / 'backup',
                              self.directory / 'pgdata')

    def test_rejects_source_inside_destination(self):
        with self.assertRaises(ValueError):
            assert_no_overlap(self.directory / 'out',
                              self.directory / 'out' / 'pgdata')

    def test_rejects_identical_paths(self):
        with self.assertRaises(ValueError):
            assert_no_overlap(self.directory / 'same', self.directory / 'same')

    def test_allows_siblings(self):
        assert_no_overlap(self.directory / 'backup', self.directory / 'pgdata')


class BuildResultTests(unittest.TestCase):
    def test_success_record_has_exactly_the_documented_keys(self):
        result, code = build_result(
            'run-1', 'pg_basebackup_full', outcome(duration=45.21),
            outcome(duration=8.42), 1073741824)
        self.assertEqual(tuple(result), RESULT_KEYS)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(result['status'], 'PASS_MANIFEST_ONLY')
        self.assertTrue(result['integrity_verified'])
        self.assertFalse(result['restore_tested'])
        self.assertIsNone(result['network_bytes'])
        self.assertAlmostEqual(result['time_to_verified_seconds'], 53.63, places=2)

    def test_failed_backup_has_null_verification_and_null_time(self):
        result, code = build_result(
            'run-2', 'pg_basebackup_full', outcome(exit_code=1), None, 0)
        self.assertEqual(tuple(result), RESULT_KEYS)
        self.assertEqual(code, EXIT_IO_FAILURE)
        self.assertEqual(result['status'], 'FAIL_BACKUP')
        self.assertIsNone(result['verification'])
        self.assertIsNone(result['time_to_verified_seconds'])
        self.assertFalse(result['integrity_verified'])

    def test_failed_verification_has_null_time(self):
        result, code = build_result(
            'run-3', 'pg_basebackup_full', outcome(), outcome(exit_code=1), 100)
        self.assertEqual(code, EXIT_VERIFICATION_FAILURE)
        self.assertEqual(result['status'], 'FAIL_VERIFY')
        self.assertIsNone(result['time_to_verified_seconds'])
        self.assertFalse(result['integrity_verified'])

    def test_record_is_json_serialisable(self):
        result, _ = build_result(
            'run-4', 'pg_basebackup_full', outcome(), outcome(), 10)
        self.assertEqual(json.loads(json.dumps(result))['run_id'], 'run-4')


class RunIdTests(unittest.TestCase):
    def test_ids_are_unique(self):
        self.assertEqual(len({new_run_id() for _ in range(5)}), 5)

    def test_id_has_no_path_separators(self):
        run_id = new_run_id()
        self.assertNotIn('/', run_id)
        self.assertNotIn('\\', run_id)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.out = self.directory / 'runs'
        self.backup_stub = make_stub(self.directory, 'fakebase', STUB_BASEBACKUP)
        self.verify_stub = make_stub(self.directory, 'fakeverify', STUB_VERIFYBACKUP)

    def invoke(self, *extra, env=None):
        argv = ['--out', str(self.out),
                '--pg-basebackup', str(self.backup_stub),
                '--pg-verifybackup', str(self.verify_stub), *extra]
        with mock.patch.dict(os.environ, env or {}):
            with redirect_stdout(StringIO()):
                return main(argv)

    def latest_run(self):
        runs = sorted(self.out.iterdir())
        self.assertEqual(len(runs), 1, 'expected exactly one run directory')
        return runs[0]

    def test_successful_run_writes_a_complete_record(self):
        self.assertEqual(self.invoke(), EXIT_OK)
        run = self.latest_run()
        result = json.loads((run / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(tuple(result), RESULT_KEYS)
        self.assertEqual(result['status'], 'PASS_MANIFEST_ONLY')
        self.assertEqual(result['method'], 'pg_basebackup_full')
        self.assertTrue(result['integrity_verified'])
        self.assertFalse(result['restore_tested'])
        self.assertIsNone(result['network_bytes'])
        self.assertEqual(result['backup_file_bytes'], 2048 + MANIFEST_BYTES)
        self.assertIsNotNone(result['time_to_verified_seconds'])
        self.assertEqual(result['run_id'], run.name)

    def test_successful_run_writes_logs_and_versions(self):
        self.invoke()
        run = self.latest_run()
        for name in ('pg_basebackup.log', 'pg_verifybackup.log', 'versions.txt'):
            self.assertTrue((run / name).exists(), f'{name} missing')
        versions = (run / 'versions.txt').read_text(encoding='utf-8')
        self.assertIn('manifest_checksums: SHA256', versions)
        self.assertIn('checkpoint: fast', versions)

    def test_versions_records_pgpassword_presence_without_the_value(self):
        self.invoke(env={'PGPASSWORD': 'hunter2'})
        versions = (self.latest_run() / 'versions.txt').read_text(encoding='utf-8')
        self.assertIn('pgpassword_env_set: True', versions)
        self.assertNotIn('hunter2', versions)

    def test_verification_failure_is_reported(self):
        self.assertEqual(
            self.invoke(env={'STUB_VERIFY_EXIT': '1'}), EXIT_VERIFICATION_FAILURE)
        result = json.loads(
            (self.latest_run() / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'FAIL_VERIFY')
        self.assertFalse(result['integrity_verified'])
        self.assertIsNone(result['time_to_verified_seconds'])

    def test_backup_failure_skips_verification(self):
        self.assertEqual(self.invoke(env={'STUB_BACKUP_EXIT': '1'}), EXIT_IO_FAILURE)
        run = self.latest_run()
        result = json.loads((run / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'FAIL_BACKUP')
        self.assertIsNone(result['verification'])
        self.assertFalse((run / 'pg_verifybackup.log').exists())

    def test_missing_tool_fails_before_creating_a_run(self):
        argv = ['--out', str(self.out),
                '--pg-basebackup', str(self.directory / 'absent-tool'),
                '--pg-verifybackup', str(self.verify_stub)]
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(main(argv), EXIT_INVALID_INPUT)
        self.assertFalse(self.out.exists())

    def test_backup_destination_inside_source_pgdata_is_refused(self):
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(['--out', str(self.out),
                         '--pg-basebackup', str(self.backup_stub),
                         '--pg-verifybackup', str(self.verify_stub),
                         '--source-pgdata', str(self.out)])
        self.assertEqual(code, EXIT_INVALID_INPUT)
        self.assertFalse(self.out.exists())


if __name__ == '__main__':
    unittest.main()
