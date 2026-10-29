"""Tests for the method registry, the v3 result record and the staged methods.

No PostgreSQL, rsync or pgdelta is required. Staged methods run end to end
against tests/stub_transfer.py, which stands in for pg_basebackup,
pg_verifybackup, rsync and the pgdelta CLI and does real file work.
"""

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from benchmarks import methods
from benchmarks.backup import (RESULT_KEYS, SELF_REPORTED_BYTES, build_result,
                               prepare_backup, run_backup, run_command)
from benchmarks import benchmark, native_baseline, report_native

STUB = Path(__file__).resolve().parent / 'stub_transfer.py'

# Captured verbatim from rsync 3.5.1 (Homebrew, macOS) running
#   rsync --archive --checksum --no-whole-file --inplace --block-size=8192
#         --delete --stats S/ O/
# against an O holding three changed 8 KiB pages, one new and one stale file.
GNU_STATS = '''
Number of files: 6 (reg: 3, dir: 3)
Number of created files: 1 (reg: 1)
Number of deleted files: 2 (reg: 1, dir: 1)
Number of regular files transferred: 3
Total file size: 557,063 bytes
Total transferred file size: 557,063 bytes
Literal data: 57,351 bytes
Number of 4 KiB logical blocks touched: 15
Matched data: 499,712 bytes
File list size: 0
File list generation time: 0.001 seconds
File list transfer time: 0.000 seconds
Total bytes sent: 57,958
Total bytes received: 499

sent 57,958 bytes  received 499 bytes  116,914.00 bytes/sec
total size is 557,063  speedup is 9.53
'''

# The same rsync 3.5.1 on an 8 MiB file with 28 dirty pages: grouping in millions.
GNU_STATS_LARGE = '''
Number of files: 4 (reg: 1, dir: 3)
Number of created files: 0
Number of deleted files: 0
Number of regular files transferred: 1
Total file size: 8,388,608 bytes
Total transferred file size: 8,388,608 bytes
Literal data: 229,376 bytes
Number of 4 KiB logical blocks touched: 56
Matched data: 8,159,232 bytes
File list size: 0
File list generation time: 0.001 seconds
File list transfer time: 0.000 seconds
Total bytes sent: 233,667
Total bytes received: 6,188

sent 233,667 bytes  received 6,188 bytes  479,710.00 bytes/sec
total size is 8,388,608  speedup is 34.97
'''

# Captured verbatim from macOS openrsync (protocol 29) on the first input.
OPENRSYNC_STATS = '''Number of files: 6
Number of files transferred: 3
Total file size: 557063 B
Total transferred file size: 557063 B
Unmatched data: 57351 B
Matched data: 499712 B
File list size: 268 B
File list generation time: 0.001 seconds
File list transfer time: 0.001 seconds
Total sent: 58029 B
Total received: 476 B

sent 58029 bytes  received 476 bytes  5270720 bytes/sec
total size is 557063  speedup is 9.52
'''

# Captured from openrsync when O already matched S.
OPENRSYNC_UNCHANGED = '''Number of files: 4
Number of files transferred: 0
Total file size: 8388608 B
Total transferred file size: 0 B
Unmatched data: 0 B
Matched data: 0 B
File list size: 162 B
File list generation time: 0.007 seconds
File list transfer time: 0.001 seconds
Total sent: 182 B
Total received: 20 B

sent 182 bytes  received 20 bytes  24938 bytes/sec
total size is 8388608  speedup is 41527.56
'''

GNU_VERSION = '''rsync  version 3.5.1  protocol version 33
Copyright (C) 1996-2026 by Andrew Tridgell, Wayne Davison, and others.
Web site: https://rsync.samba.org/
'''
OPENRSYNC_VERSION = 'openrsync: protocol version 29\nrsync version 2.6.9 compatible\n'


def launcher(directory, tool):
    if os.name == 'nt':
        path = directory / f'{tool}.cmd'
        path.write_text(f'@echo off\r\n"{sys.executable}" "{STUB}" {tool} %*\r\n')
    else:
        path = directory / tool
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STUB}" {tool} "$@"\n')
        path.chmod(0o755)
    return path


def snapshot(root):
    """Every path under root with a digest of its bytes (None for directories)."""
    root = Path(root)
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            if p.is_file() else None for p in sorted(root.rglob('*'))}


def phase(exit_code=0, duration=1.0, error=None):
    return {'exit_code': exit_code, 'duration_seconds': duration, 'error': error}


class RegistryTests(unittest.TestCase):
    def test_built_in_methods_are_registered(self):
        self.assertEqual(methods.names(), ('pg_basebackup_full', 'pg_basebackup_incremental',
                                           'pg_delta_staged', 'rsync_staged'))
        self.assertFalse(methods.get('pg_basebackup_full').needs_basis)
        for name in ('pg_basebackup_incremental', 'rsync_staged', 'pg_delta_staged'):
            self.assertTrue(methods.get(name).needs_basis)
        # Only incremental reuses basis bytes unchecked, so only it verifies B first.
        self.assertEqual([n for n in methods.names() if methods.get(n).trusts_basis],
                         ['pg_basebackup_incremental'])

    def test_unknown_method_is_a_value_error(self):
        for name in ('rsync', None, ['list']):
            with self.subTest(name=name), self.assertRaises(ValueError):
                methods.get(name)

    def test_duplicate_reserved_and_malformed_names_are_refused(self):
        run = methods.get('pg_basebackup_full').run
        with mock.patch.dict(methods._REGISTRY):
            for name in ('pg_basebackup_full', 'no_backup', 'Has Space', '', '-flag'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    methods.register(methods.Method(name, needs_basis=False, run=run))
            with self.assertRaises(ValueError):
                methods.register(methods.Method('trusting', needs_basis=False, run=run,
                                                trusts_basis=True))

    def test_a_new_method_needs_only_a_registration(self):
        """The CLI picks a registered method up without any other edit."""
        def run(ctx):
            capture = ctx.execute('backup', ['capture'])
            transfer = ctx.execute('transfer', ['copy', str(ctx.output)])
            return {'backup': capture, 'transfer': transfer,
                    'transfer_stats': {'bytes_source_to_dest': 10, 'bytes_dest_to_source': 2}}

        with mock.patch.dict(methods._REGISTRY):
            methods.register(methods.Method('demo_copy', needs_basis=False, run=run))
            self.assertIn('demo_copy', methods.names())
            self.assertEqual(benchmark.parse_args(['--method', 'demo_copy']).method, 'demo_copy')
            stages = []
            result, code = run_backup(
                'demo_copy', run_id='r', output='/nowhere/out',
                tools={'pg_basebackup': 'b', 'pg_verifybackup': 'v'},
                execute=lambda stage, argv: stages.append(stage) or phase(), host='h')
        self.assertEqual(code, 0)
        self.assertEqual(stages, ['backup', 'transfer', 'verification'])
        self.assertEqual(result['network_bytes'], 12)
        self.assertNotIn('demo_copy', methods.names())

    def test_inputs_are_validated_before_anything_runs(self):
        execute = mock.Mock()
        tools = {'pg_basebackup': 'b', 'pg_verifybackup': 'v', 'rsync': 'r', 'python': 'p'}
        common = dict(run_id='r', output='/o', execute=execute, host='h',
                      read_log=lambda stage: '')
        cases = [
            dict(method='rsync_staged', tools=tools, capture_path='/s'),
            dict(method='rsync_staged', tools=tools, basis='/b'),
            dict(method='rsync_staged', tools={**tools, 'rsync': None}, basis='/b',
                 capture_path='/s'),
            dict(method='pg_basebackup_full', tools=tools, capture_path='/s'),
            dict(method='pg_basebackup_incremental', tools=tools, basis='/b',
                 increment='/i', capture_path='/s'),
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                run_backup(**common, **case)
        with self.assertRaises(ValueError):
            run_backup('rsync_staged', run_id='r', output='/o', execute=execute, host='h',
                       tools=tools, basis='/b', capture_path='/s')
        with self.assertRaises(ValueError):
            run_backup('pg_basebackup_full', run_id='r', output='/o', execute=execute,
                       host='h', tools=tools, prepared={})
        execute.assert_not_called()


class ResultSchemaTests(unittest.TestCase):
    def test_v3_record_has_the_strict_key_set_and_null_transfer_by_default(self):
        result, code = build_result('r', 'pg_basebackup_full', phase(), phase(), 10)
        self.assertEqual(tuple(result), RESULT_KEYS)
        self.assertEqual(result['schema_version'], 3)
        self.assertIsNone(result['transfer'])
        self.assertIsNone(result['transfer_stats'])
        self.assertIsNone(result['network_bytes'])
        self.assertEqual(result['unavailable_metrics'],
                         {'network_bytes': 'transport observer not implemented'})
        self.assertIn('transfer', RESULT_KEYS)
        self.assertIn('transfer_stats', RESULT_KEYS)

    def test_network_bytes_come_from_both_directions(self):
        result, _ = build_result(
            'r', 'rsync_staged', phase(duration=2), phase(duration=1), 10,
            transfer=phase(duration=3),
            transfer_stats={'bytes_source_to_dest': 57958, 'bytes_dest_to_source': 499})
        self.assertEqual(result['network_bytes'], 58457)
        self.assertEqual(result['unavailable_metrics'], {'interface_bytes': SELF_REPORTED_BYTES})
        # Without an explicit timer, method time sums capture and transfer.
        self.assertEqual(result['method_seconds'], 5)
        self.assertEqual(result['time_to_verified_seconds'], 6)

    def test_partial_or_invalid_counts_leave_network_bytes_null(self):
        for stats in ({'bytes_source_to_dest': 10}, {'bytes_source_to_dest': 10,
                      'bytes_dest_to_source': -1}, {'bytes_source_to_dest': True,
                      'bytes_dest_to_source': 1}, {'bytes_source_to_dest': 1.5,
                      'bytes_dest_to_source': 1}, {'unavailable_reason': 'x'}):
            with self.subTest(stats=stats):
                result, _ = build_result('r', 'm', phase(), phase(), 1, transfer_stats=stats)
                self.assertIsNone(result['network_bytes'])
                self.assertIn('network_bytes', result['unavailable_metrics'])

    def test_record_with_transfer_is_json_serialisable(self):
        result, _ = build_result('r', 'm', phase(), phase(), 1, transfer=phase(),
                                 transfer_stats={'bytes_source_to_dest': 1,
                                                 'bytes_dest_to_source': 1})
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False))['network_bytes'], 2)


class RsyncStatsTests(unittest.TestCase):
    def test_gnu_rsync_3_layout(self):
        stats = methods.parse_rsync_stats(GNU_STATS)
        self.assertEqual(stats['stats_format'], 'gnu')
        self.assertEqual((stats['bytes_sent'], stats['bytes_received']), (57958, 499))
        self.assertEqual((stats['literal_bytes'], stats['matched_bytes']), (57351, 499712))
        self.assertEqual(stats['files'], 6)
        self.assertEqual(stats['files_created'], 1)
        self.assertEqual(stats['files_deleted'], 2)
        self.assertEqual(stats['files_transferred'], 3)
        self.assertEqual(stats['total_file_bytes'], 557063)
        self.assertEqual(stats['file_list_bytes'], 0)

    def test_gnu_grouping_in_millions(self):
        stats = methods.parse_rsync_stats(GNU_STATS_LARGE)
        self.assertEqual(stats['matched_bytes'], 8159232)
        self.assertEqual(stats['literal_bytes'], 229376)
        self.assertEqual(stats['literal_bytes'] % 8192, 0)

    def test_gnu_layout_without_the_logical_block_line(self):
        older = '\n'.join(line for line in GNU_STATS.splitlines()
                          if 'logical blocks' not in line)
        self.assertEqual(methods.parse_rsync_stats(older)['bytes_sent'], 57958)

    def test_openrsync_layout(self):
        stats = methods.parse_rsync_stats(OPENRSYNC_STATS)
        self.assertEqual(stats['stats_format'], 'openrsync')
        self.assertEqual((stats['bytes_sent'], stats['bytes_received']), (58029, 476))
        self.assertEqual((stats['literal_bytes'], stats['matched_bytes']), (57351, 499712))
        self.assertEqual(stats['files_transferred'], 3)
        self.assertEqual(stats['file_list_bytes'], 268)
        self.assertNotIn('files_created', stats)

    def test_unchanged_destination_reports_zero_literal_data(self):
        stats = methods.parse_rsync_stats(OPENRSYNC_UNCHANGED)
        self.assertEqual((stats['literal_bytes'], stats['bytes_sent']), (0, 182))

    def test_output_wrapped_in_the_cli_log_parses(self):
        log = ('$ /usr/bin/rsync --archive --stats /a: weird/ /b/\n\n'
               + GNU_STATS + '\nrsync warning: vanished\n')
        self.assertEqual(methods.parse_rsync_stats(log)['bytes_received'], 499)

    def test_other_locale_grouping_parses(self):
        dotted = GNU_STATS.replace('57,958', '57.958').replace('499,712', "499'712")
        stats = methods.parse_rsync_stats(dotted)
        self.assertEqual((stats['bytes_sent'], stats['matched_bytes']), (57958, 499712))

    def test_human_readable_sizes_are_refused_not_misread(self):
        with self.assertRaises(ValueError):
            methods.parse_rsync_stats(GNU_STATS.replace('57,351 bytes', '57.35K bytes'))

    def test_missing_counters_are_refused(self):
        for text in ('', 'stub exec ok\n', GNU_STATS.replace('Total bytes received', 'Gone')):
            with self.subTest(text=text[:20]), self.assertRaises(ValueError):
                methods.parse_rsync_stats(text)

    def test_conflicting_repeated_counters_are_refused(self):
        with self.assertRaises(ValueError):
            methods.parse_rsync_stats(GNU_STATS + 'Total bytes sent: 1\n')

    def test_directions_are_named_for_the_push(self):
        stats = methods.rsync_transfer_stats(GNU_STATS)
        self.assertEqual(stats['bytes_source_to_dest'], 57958)
        self.assertEqual(stats['bytes_dest_to_source'], 499)

    def test_version_banners(self):
        self.assertEqual(methods.rsync_version(GNU_VERSION),
                         'rsync version 3.5.1 protocol version 33')
        self.assertEqual(methods.rsync_version(OPENRSYNC_VERSION),
                         'openrsync: protocol version 29 / rsync version 2.6.9 compatible')
        self.assertEqual(methods.rsync_version('$ rsync --version\n\n' + GNU_VERSION),
                         'rsync version 3.5.1 protocol version 33')
        self.assertIsNone(methods.rsync_version(''))

    def test_command_uses_the_agreed_options_and_trailing_separators(self):
        argv = methods.rsync_command('/usr/bin/rsync', '/s', Path('/o'))
        self.assertEqual(argv, ['/usr/bin/rsync', '--archive', '--checksum', '--no-whole-file',
                                '--inplace', '--block-size=8192', '--delete', '--stats',
                                '/s' + os.sep, '/o' + os.sep])


class PgdeltaStatsTests(unittest.TestCase):
    def test_contract_keys_are_extracted_and_the_rest_retained(self):
        stats = methods.parse_pgdelta_stats(json.dumps(
            {'bytes_source_to_dest': 100, 'bytes_dest_to_source': 7, 'pages_sent': 3}))
        self.assertEqual(stats['bytes_source_to_dest'], 100)
        self.assertEqual(stats['bytes_dest_to_source'], 7)
        self.assertEqual(stats['reported']['pages_sent'], 3)

    def test_real_pgdelta_byte_summaries_are_read_from_their_totals(self):
        # Shape and numbers from `pgdelta local --stats-json` (schema_version 1) on PG17.11.
        reported = {
            'schema_version': 1, 'status': 'complete', 'exit_code': 0,
            'bytes_source_to_dest': {
                'total': 18176589, 'control': 58421, 'node_digests': 0, 'leaf_digests': 338,
                'page_data': 18117830,
                'by_message_type': {'PAGE_DATA': {'frames': 50, 'bytes': 18117830}}},
            'bytes_dest_to_source': {
                'total': 46041, 'control': 12291, 'node_digests': 29372, 'leaf_digests': 4378,
                'page_data': 0,
                'by_message_type': {'BASIS_NODES': {'frames': 972, 'bytes': 29372}}},
            'pages': {'total': 5059, 'sent': 2213, 'reused': 2846, 'sent_bytes': 18116980}}
        stats = methods.parse_pgdelta_stats(json.dumps(reported))
        self.assertEqual((stats['bytes_source_to_dest'], stats['bytes_dest_to_source']),
                         (18176589, 46041))
        self.assertEqual(stats['reported'], reported)
        result, _ = build_result('r', 'pg_delta_staged', phase(), phase(), 1,
                                 transfer=phase(), transfer_stats=stats)
        self.assertEqual(result['network_bytes'], 18176589 + 46041)
        # Plain integers, and one of each, are accepted too.
        mixed = methods.parse_pgdelta_stats(json.dumps(
            {'bytes_source_to_dest': 100, 'bytes_dest_to_source': {'total': 7}}))
        self.assertEqual((mixed['bytes_source_to_dest'], mixed['bytes_dest_to_source']), (100, 7))

    def test_summaries_without_a_usable_total_are_refused(self):
        for value in ({}, {'total': -1}, {'total': 1.5}, {'total': True}, {'total': None},
                      {'page_data': 10}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                methods.parse_pgdelta_stats(json.dumps(
                    {'bytes_source_to_dest': value, 'bytes_dest_to_source': 0}))

    def test_invalid_statistics_are_refused(self):
        for text in ('{', '[]', '{"bytes_source_to_dest": 1}',
                     '{"bytes_source_to_dest": -1, "bytes_dest_to_source": 0}',
                     '{"bytes_source_to_dest": true, "bytes_dest_to_source": 0}',
                     '{"bytes_source_to_dest": 1.0, "bytes_dest_to_source": 0}',
                     '{"bytes_source_to_dest": 1, "bytes_dest_to_source": 0, "x": NaN}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                methods.parse_pgdelta_stats(text)

    def test_default_command_and_stats_location(self):
        self.assertEqual(methods.pgdelta_command({'python': '/py'}), ['/py', '-m', 'pgdelta'])
        self.assertEqual(methods.pgdelta_command({'python': '/py', 'pgdelta': ['x', 'y']}),
                         ['x', 'y'])
        self.assertEqual(methods.pgdelta_command({'pgdelta': Path('/bin/pgd')}), ['/bin/pgd'])
        # A sibling of O, never inside the data directory.
        self.assertEqual(methods.pgdelta_stats_path('/r/backup'),
                         Path('/r/backup-pgdelta-stats.json'))


class StubCase(unittest.TestCase):
    """A basis B, fresh S and O paths, and stand-in tools that do real file work."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        self.stubs = {tool: launcher(bin_dir, tool) for tool in
                      ('pg_basebackup', 'pg_verifybackup', 'rsync', 'pgdelta')}
        self.version_file = self.root / 'rsync-version.txt'
        self.version_file.write_text(GNU_VERSION)
        self.stats_file = self.root / 'rsync-stats.txt'
        self.stats_file.write_text(GNU_STATS)
        self.log = self.root / 'stub.log'
        self.env = {'STUB_LOG': str(self.log), 'STUB_RSYNC_VERSION': str(self.version_file),
                    'STUB_RSYNC_STATS': str(self.stats_file)}
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.basis = self.root / 'basis'
        (self.basis / 'base' / '1').mkdir(parents=True)
        (self.basis / 'pg_wal').mkdir()
        (self.basis / 'stale').mkdir()
        (self.basis / 'base' / '1' / '1259').write_bytes(b'page' * 2048 + b'old')
        (self.basis / 'pg_wal' / '000000010000000000000001').write_bytes(b'old wal')
        (self.basis / 'stale' / 'only-in-basis').write_bytes(b'must not survive')
        (self.basis / 'backup_manifest').write_text('{"Stamp": "old"}')
        self.before = snapshot(self.basis)
        self.run_dir = self.root / 'run'
        self.run_dir.mkdir()
        self.output = self.run_dir / 'backup'
        self.staging = self.run_dir / 'staging'
        self.logs = self.root / 'logs'
        self.logs.mkdir()
        self.tools = {'pg_basebackup': str(self.stubs['pg_basebackup']),
                      'pg_verifybackup': str(self.stubs['pg_verifybackup']),
                      'rsync': str(self.stubs['rsync']), 'python': sys.executable}

    def execute(self, stage, argv):
        return run_command(argv, self.logs / f'{stage}.log', 60)

    def read_log(self, stage):
        return (self.logs / f'{stage}.log').read_text(encoding='utf-8')

    def adapter(self, **overrides):
        return {'run_id': 'trial', 'output': self.output, 'tools': self.tools,
                'execute': self.execute, 'host': 'fixture', 'basis': self.basis,
                'capture_path': self.staging, 'read_log': self.read_log, **overrides}

    def invocations(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def tools_run(self):
        return [(entry['tool'], '--version' in entry['argv']) for entry in self.invocations()]

    def assert_basis_untouched(self):
        self.assertEqual(snapshot(self.basis), self.before)


class RsyncStagedTests(StubCase):
    def test_refreshes_a_copy_of_the_basis_and_records_its_transfer(self):
        result, code = run_backup('rsync_staged', **self.adapter())
        self.assertEqual(code, 0, result)
        self.assertEqual(tuple(result), RESULT_KEYS)
        self.assertEqual(result['status'], 'PASS_MANIFEST_ONLY')
        self.assertEqual(snapshot(self.output), snapshot(self.staging))
        self.assertNotIn('stale', snapshot(self.output))
        self.assert_basis_untouched()
        stats = result['transfer_stats']
        self.assertEqual(stats['tool'], 'rsync')
        self.assertEqual(stats['version'], 'rsync version 3.5.1 protocol version 33')
        self.assertEqual(stats['stats_format'], 'gnu')
        self.assertEqual((stats['bytes_source_to_dest'], stats['bytes_dest_to_source']),
                         (57958, 499))
        self.assertEqual((stats['literal_bytes'], stats['matched_bytes']), (57351, 499712))
        self.assertEqual(result['network_bytes'], 57958 + 499)
        self.assertNotIn('network_bytes', result['unavailable_metrics'])
        self.assertIsNone(result['basis_verification'])
        self.assertIsNone(result['combination'])

    def test_phases_run_in_order_and_are_timed_separately(self):
        result, _ = run_backup('rsync_staged', **self.adapter())
        self.assertEqual(self.tools_run(), [('rsync', True), ('pg_basebackup', False),
                                            ('rsync', False), ('pg_verifybackup', False)])
        preparation = result['transfer_stats']['preparation']
        self.assertEqual(sorted(preparation), ['output_preparation', 'rsync_version'])
        for record in (result['backup'], result['transfer'], result['verification'],
                       *preparation.values()):
            self.assertEqual(record['exit_code'], 0)
            self.assertGreater(record['duration_seconds'], 0)
        self.assertGreaterEqual(result['method_seconds'], result['backup']['duration_seconds']
                                + result['transfer']['duration_seconds'])
        self.assertGreaterEqual(result['time_to_verified_seconds'],
                                result['method_seconds'] + result['verification']['duration_seconds'])
        self.assertEqual(result['backup_file_bytes'], sum(
            p.stat().st_size for p in self.staging.rglob('*') if p.is_file()))

    def test_commands_follow_the_contract(self):
        run_backup('rsync_staged', **self.adapter())
        rsync, verify = self.invocations()[2], self.invocations()[3]
        self.assertEqual(rsync['argv'], [*methods.RSYNC_OPTIONS, os.path.join(str(self.staging), ''),
                                         os.path.join(str(self.output), '')])
        # S's manifest verifies O, so O must match S rather than keep B's manifest.
        self.assertEqual(verify['argv'], ['--manifest-path',
                                          str(self.staging / 'backup_manifest'), str(self.output)])
        copy = self.read_log('output_preparation').splitlines()[0]
        self.assertIn('-I', copy)
        self.assertIn('copytree', copy)

    def test_openrsync_output_is_parsed(self):
        self.version_file.write_text(OPENRSYNC_VERSION)
        self.stats_file.write_text(OPENRSYNC_STATS)
        result, code = run_backup('rsync_staged', **self.adapter())
        self.assertEqual(code, 0)
        self.assertEqual(result['transfer_stats']['stats_format'], 'openrsync')
        self.assertTrue(result['transfer_stats']['version'].startswith('openrsync'))
        self.assertEqual(result['network_bytes'], 58029 + 476)

    def test_failed_transfer_is_not_verified(self):
        with mock.patch.dict(os.environ, {'STUB_FAIL': 'rsync-transfer'}):
            result, code = run_backup('rsync_staged', **self.adapter())
        self.assertEqual((result['status'], code), ('FAIL_TRANSFER', 3))
        self.assertEqual(result['transfer']['exit_code'], 23)
        self.assertIsNone(result['verification'])
        self.assertIsNone(result['time_to_verified_seconds'])
        self.assertIsNone(result['network_bytes'])
        self.assert_basis_untouched()

    def test_unusable_statistics_fail_the_transfer(self):
        self.stats_file.write_text('stub exec ok\n')
        result, code = run_backup('rsync_staged', **self.adapter())
        self.assertEqual((result['status'], code), ('FAIL_TRANSFER', 3))
        self.assertEqual(result['transfer']['exit_code'], 0)
        self.assertIn('unusable transfer statistics', result['transfer']['error'])
        self.assertIsNone(result['verification'])

    def test_existing_output_fails_preparation_before_capture(self):
        self.output.mkdir()
        result, code = run_backup('rsync_staged', **self.adapter())
        self.assertEqual((result['status'], code), ('FAIL_PREPARE', 3))
        self.assertIsNone(result['backup'])
        self.assertNotIn('pg_basebackup', [tool for tool, _ in self.tools_run()])
        self.assertNotEqual(result['transfer_stats']['preparation']['output_preparation']
                            ['exit_code'], 0)

    def test_missing_rsync_fails_preparation_before_copying(self):
        with mock.patch.dict(os.environ, {'STUB_FAIL': 'rsync-version'}):
            result, _ = run_backup('rsync_staged', **self.adapter())
        self.assertEqual(result['status'], 'FAIL_PREPARE')
        self.assertEqual(list(result['transfer_stats']['preparation']), ['rsync_version'])
        self.assertFalse(self.output.exists())

    def test_failed_capture_skips_the_transfer(self):
        with mock.patch.dict(os.environ, {'STUB_FAIL': 'pg_basebackup'}):
            result, code = run_backup('rsync_staged', **self.adapter())
        self.assertEqual((result['status'], code), ('FAIL_BACKUP', 3))
        self.assertIsNone(result['transfer'])
        self.assertEqual(self.tools_run(), [('rsync', True), ('pg_basebackup', False)])

    def test_output_left_unlike_s_fails_verification(self):
        """A transfer that exits 0 but leaves B's content in O must not pass."""
        lazy = [sys.executable, '-c', 'import sys; print(open(sys.argv[1]).read())',
                str(self.stats_file)]
        with mock.patch('benchmarks.methods.rsync_command', lambda *args: lazy):
            result, code = run_backup('rsync_staged', **self.adapter())
        self.assertEqual(result['transfer']['exit_code'], 0)
        self.assertEqual((result['status'], code), ('FAIL_VERIFY', 4))
        self.assertFalse(result['integrity_verified'])
        self.assertIsNone(result['time_to_verified_seconds'])

    def test_basis_manifest_left_by_an_exclude_fails_verification(self):
        """pg_verifybackup never reads O's backup_manifest, so B's copy got through."""
        with mock.patch.dict(os.environ, {'STUB_RSYNC_EXCLUDE': 'backup_manifest'}):
            result, code = run_backup('rsync_staged', **self.adapter())
        self.assertEqual((self.output / 'backup_manifest').read_text(), '{"Stamp": "old"}')
        # The stand-in, like PostgreSQL 17.11, passes this output.
        self.assertEqual(result['verification']['exit_code'], 0)
        self.assertEqual((result['status'], code), ('FAIL_VERIFY', 4))
        self.assertIn('backup_manifest', result['verification']['error'])
        self.assertFalse(result['integrity_verified'])
        self.assertIsNone(result['time_to_verified_seconds'])
        self.assert_basis_untouched()

    def test_exact_copy_records_a_passing_output_check(self):
        result, code = run_backup('rsync_staged', **self.adapter())
        self.assertEqual(code, 0)
        check = result['verification']['output_check']
        self.assertEqual((check['exit_code'], check['error'], check['differences']),
                         (0, None, []))
        self.assertEqual(check['report']['files_compared'], 3)
        # The check is the harness's oracle, not part of the method's cost.
        self.assertLess(result['time_to_verified_seconds'],
                        result['method_seconds'] + result['verification']['duration_seconds']
                        + check['duration_seconds'])

    def test_early_preparation_is_reused(self):
        prepared = prepare_backup('rsync_staged', **self.adapter())
        self.assertTrue(self.output.is_dir())
        result, code = run_backup('rsync_staged', **self.adapter(), prepared=prepared)
        self.assertEqual(code, 0)
        self.assertEqual([tool for tool, version in self.tools_run() if version], ['rsync'])
        self.assertIs(result['transfer_stats']['preparation'], prepared)

    def test_preparation_is_outside_method_timing(self):
        def execute(stage, argv):
            if stage == 'output_preparation':
                time.sleep(0.3)
            return phase(duration=0.01)

        logs = {'rsync_version': GNU_VERSION, 'transfer': GNU_STATS, 'output_check': ''}
        result, code = run_backup('rsync_staged', **self.adapter(
            execute=execute, read_log=logs.__getitem__))
        self.assertEqual(code, 0)
        self.assertLess(result['method_seconds'], 0.3)
        self.assertLess(result['time_to_verified_seconds'], 0.3)


class PgDeltaStagedTests(StubCase):
    def setUp(self):
        super().setUp()
        package = self.root / 'pkg' / 'pgdelta'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('')
        (package / '__main__.py').write_text(
            'import runpy, sys\n'
            f'sys.argv = [{str(STUB)!r}, "pgdelta", *sys.argv[1:]]\n'
            f'runpy.run_path({str(STUB)!r}, run_name="__main__")\n')
        patcher = mock.patch.dict(os.environ, {'PYTHONPATH': str(self.root / 'pkg')})
        patcher.start()
        self.addCleanup(patcher.stop)
        # The real pgdelta package sits in the repo root, which `python -m` puts ahead of
        # PYTHONPATH when tests run from there; run the stand-in package from its own dir.
        self.cwd = os.getcwd()
        os.chdir(self.root / 'pkg')
        self.addCleanup(os.chdir, self.cwd)

    def test_runs_python_m_pgdelta_with_the_cli_contract(self):
        result, code = run_backup('pg_delta_staged', **self.adapter())
        self.assertEqual(code, 0, result)
        self.assertEqual(result['status'], 'PASS_MANIFEST_ONLY')
        stats_path = self.run_dir / 'backup-pgdelta-stats.json'
        self.assertEqual(self.invocations()[1]['argv'], [
            'local', '--source', str(self.staging), '--basis', str(self.basis),
            '--output', str(self.output), '--stats-json', str(stats_path)])
        log = self.read_log('transfer').splitlines()[0]
        self.assertTrue(log.startswith(f'$ {sys.executable} -m pgdelta local'))
        stats = result['transfer_stats']
        self.assertEqual(stats['command'], [sys.executable, '-m', 'pgdelta'])
        self.assertEqual((stats['bytes_source_to_dest'], stats['bytes_dest_to_source']),
                         (40960, 2048))
        self.assertEqual(stats['reported']['pages_reused'], 1019)
        self.assertEqual(result['network_bytes'], 43008)
        self.assertEqual(snapshot(self.output), snapshot(self.staging))
        self.assert_basis_untouched()
        verify = self.invocations()[2]
        self.assertEqual(verify['argv'][:2], ['--manifest-path',
                                              str(self.staging / 'backup_manifest')])
        self.assertEqual(self.tools_run(), [('pg_basebackup', False), ('pgdelta', False),
                                            ('pg_verifybackup', False)])
        self.assertIsNone(result['basis_verification'])

    def test_explicit_command_and_pg_controldata(self):
        tools = {**self.tools, 'pgdelta': [str(self.stubs['pgdelta'])],
                 'pg_controldata': '/usr/lib/postgresql/17/bin/pg_controldata'}
        result, code = run_backup('pg_delta_staged', **self.adapter(tools=tools))
        self.assertEqual(code, 0)
        argv = self.invocations()[1]['argv']
        self.assertEqual(argv[-2:], ['--pg-controldata',
                                     '/usr/lib/postgresql/17/bin/pg_controldata'])
        self.assertEqual(result['transfer_stats']['command'], [str(self.stubs['pgdelta'])])

    def test_unusable_statistics_fail_the_transfer(self):
        for mode in ('missing', 'malformed', 'negative', 'nan'):
            with self.subTest(mode=mode), mock.patch.dict(os.environ,
                                                          {'STUB_PGDELTA_STATS': mode}):
                self.output = self.run_dir / f'out-{mode}'
                self.staging = self.run_dir / f'staging-{mode}'
                result, code = run_backup('pg_delta_staged', **self.adapter())
                self.assertEqual((result['status'], code), ('FAIL_TRANSFER', 3))
                self.assertIn('unusable transfer statistics', result['transfer']['error'])
                self.assertIsNone(result['network_bytes'])
                self.assertIsNone(result['verification'])
                json.dumps(result, allow_nan=False)

    def run_defective(self, defect):
        self.output = self.run_dir / f'out-{defect}'
        self.staging = self.run_dir / f'staging-{defect}'
        with mock.patch.dict(os.environ, {'STUB_PGDELTA_DEFECT': defect}):
            return run_backup('pg_delta_staged', **self.adapter())

    def test_differences_pg_verifybackup_skips_fail_verification(self):
        """O must match S exactly where pg_verifybackup does not look."""
        cases = {'basis_manifest': 'backup_manifest', 'auto_conf': 'postgresql.auto.conf',
                 'signal': 'standby.signal', 'extra_wal': 'pg_wal/000000010000000000000001',
                 'missing_directory': 'pg_notify'}
        for defect, path in cases.items():
            with self.subTest(defect=defect):
                result, code = self.run_defective(defect)
                # The stand-in, like PostgreSQL 17.11, passes each of these outputs.
                self.assertEqual(result['verification']['exit_code'], 0)
                self.assertEqual((result['status'], code), ('FAIL_VERIFY', 4))
                self.assertIn(path, result['verification']['error'])
                check = result['verification']['output_check']
                self.assertTrue(any(path in line for line in check['differences']), check)
                self.assertFalse(result['integrity_verified'])
                self.assertIsNone(result['time_to_verified_seconds'])
                self.assert_basis_untouched()

    def test_output_published_as_a_symlink_to_s_is_refused(self):
        result, code = self.run_defective('symlink')
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(result['verification']['exit_code'], 0)
        self.assertEqual((result['status'], code), ('FAIL_VERIFY', 4))
        self.assertIn('symbolic link', result['verification']['error'])
        # S's size must not be reported as O's.
        self.assertIsNone(result['output_file_bytes'])
        self.assertIsNone(result['time_to_verified_seconds'])

    def test_output_hard_linked_to_s_is_refused(self):
        result, code = self.run_defective('hardlink')
        self.assertEqual(result['verification']['exit_code'], 0)
        self.assertEqual((result['status'], code), ('FAIL_VERIFY', 4))
        self.assertIn('base/1/1259', result['verification']['error'])
        self.assertIn('hard link', result['verification']['error'])

    def test_output_check_runs_after_the_measured_phases(self):
        executed, seen = [], []

        def execute(stage, argv):
            executed.append(stage)
            return self.execute(stage, argv)

        result, code = run_backup('pg_delta_staged', **self.adapter(
            execute=execute, after_verification=lambda: seen.append(list(executed))))
        self.assertEqual(code, 0, result)
        self.assertEqual(seen, [['backup', 'transfer', 'verification']])
        self.assertEqual(executed[-1], 'output_check')
        self.assertTrue(self.read_log('output_check').startswith(f'$ {sys.executable} -I -c '))

    def test_tool_failure_is_a_transfer_failure(self):
        with mock.patch.dict(os.environ, {'STUB_FAIL': 'pgdelta'}):
            result, code = run_backup('pg_delta_staged', **self.adapter())
        self.assertEqual((result['status'], code), ('FAIL_TRANSFER', 3))
        self.assertEqual(result['transfer']['exit_code'], 3)
        self.assert_basis_untouched()


class CliTests(StubCase):
    def invoke(self, *extra):
        argv = ['--out', str(self.root / 'runs'),
                '--pg-basebackup', str(self.stubs['pg_basebackup']),
                '--pg-verifybackup', str(self.stubs['pg_verifybackup']), *extra]
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return benchmark.main(argv)

    def run_dir_of(self):
        (run,) = (self.root / 'runs').iterdir()
        return run

    def test_rsync_staged_end_to_end(self):
        code = self.invoke('--method', 'rsync_staged', '--basis', str(self.basis),
                           '--rsync', str(self.stubs['rsync']))
        self.assertEqual(code, 0)
        run = self.run_dir_of()
        result = json.loads((run / 'result.json').read_text())
        self.assertEqual(tuple(result), RESULT_KEYS)
        self.assertEqual(result['network_bytes'], 58457)
        self.assertEqual(snapshot(run / 'backup'), snapshot(run / 'staging'))
        self.assert_basis_untouched()
        phases = [c['phase'] for c in json.loads((run / 'commands.json').read_text())]
        self.assertEqual(phases, ['rsync_version', 'output_preparation', 'backup', 'transfer',
                                  'verification', 'output_check'])
        self.assertTrue((run / 'transfer.log').is_file())
        self.assertTrue((run / 'output_check.log').is_file())
        self.assertEqual(result['verification']['output_check']['differences'], [])
        versions = (run / 'versions.txt').read_text()
        self.assertIn('rsync: rsync version 3.5.1 protocol version 33\n', versions)
        self.assertIn('method_python: Python', versions)

    def test_pg_delta_staged_with_an_explicit_command(self):
        code = self.invoke('--method', 'pg_delta_staged', '--basis', str(self.basis),
                           '--pgdelta', str(self.stubs['pgdelta']),
                           '--pg-controldata', str(self.stubs['pg_verifybackup']))
        self.assertEqual(code, 0)
        run = self.run_dir_of()
        result = json.loads((run / 'result.json').read_text())
        self.assertEqual(result['network_bytes'], 43008)
        (call,) = [entry for entry in self.invocations() if entry['tool'] == 'pgdelta']
        self.assertEqual(call['argv'][-2:], ['--pg-controldata',
                                             str(self.stubs['pg_verifybackup'])])
        self.assertTrue((run / 'backup-pgdelta-stats.json').is_file())
        self.assertIn('pgdelta_command:', (run / 'versions.txt').read_text())

    def assert_basis_change_fails_the_run(self, defect):
        with mock.patch.dict(os.environ, {'STUB_PGDELTA_DEFECT': defect}):
            code = self.invoke('--method', 'pg_delta_staged', '--basis', str(self.basis),
                               '--pgdelta', str(self.stubs['pgdelta']))
        self.assertNotEqual(snapshot(self.basis), self.before)
        self.assertEqual(code, 4)
        result = json.loads((self.run_dir_of() / 'result.json').read_text())
        self.assertEqual(tuple(result), RESULT_KEYS)
        self.assertEqual(result['status'], 'FAIL_BASIS')
        self.assertFalse(result['integrity_verified'])
        self.assertIsNone(result['time_to_verified_seconds'])
        error = result['basis_verification']['error']
        self.assertIn('basis changed during the run', error)
        self.assertIn('base/1/1259', error)
        return error

    def test_basis_grown_during_the_run_fails_it(self):
        self.assertIn('size', self.assert_basis_change_fails_the_run('modify_basis'))

    def test_basis_rewritten_with_its_mtime_restored_still_fails_the_run(self):
        error = self.assert_basis_change_fails_the_run('rewrite_basis')
        self.assertNotIn('size', error)
        self.assertNotIn('mtime_ns', error)

    def test_unchanged_basis_leaves_the_record_alone(self):
        code = self.invoke('--method', 'pg_delta_staged', '--basis', str(self.basis),
                           '--pgdelta', str(self.stubs['pgdelta']))
        self.assertEqual(code, 0)
        result = json.loads((self.run_dir_of() / 'result.json').read_text())
        self.assertEqual(result['status'], 'PASS_MANIFEST_ONLY')
        self.assertIsNone(result['basis_verification'])

    def test_full_backup_records_no_extra_tools(self):
        self.assertEqual(self.invoke(), 0)
        versions = (self.run_dir_of() / 'versions.txt').read_text()
        for absent in ('rsync', 'method_python', 'pgdelta', 'pg_controldata',
                       'pg_combinebackup'):
            self.assertNotIn(absent, versions)

    def test_basis_rules_follow_the_registry(self):
        with redirect_stderr(StringIO()):
            for argv in (['--method', 'rsync_staged'], ['--method', 'pg_delta_staged'],
                         ['--method', 'pg_basebackup_full', '--basis', '/b'],
                         ['--method', 'nonexistent']):
                with self.subTest(argv=argv), self.assertRaises(SystemExit):
                    benchmark.parse_args(argv)
        self.assertEqual(benchmark.parse_args(['--method', 'rsync_staged', '--basis', '/b'])
                         .basis, Path('/b'))

    def test_staged_basis_must_be_a_directory(self):
        not_a_directory = self.root / 'file'
        not_a_directory.write_text('x')
        code = self.invoke('--method', 'rsync_staged', '--basis', str(not_a_directory),
                           '--rsync', str(self.stubs['rsync']))
        self.assertEqual(code, 2)
        self.assertFalse((self.root / 'runs').exists())

    def test_missing_rsync_is_refused_before_creating_a_run(self):
        code = self.invoke('--method', 'rsync_staged', '--basis', str(self.basis),
                           '--rsync', str(self.root / 'absent-rsync'))
        self.assertEqual(code, 2)
        self.assertFalse((self.root / 'runs').exists())


class NativeMatrixTests(unittest.TestCase):
    def test_default_matrix_matches_the_committed_evidence_order(self):
        import random
        legacy = []
        rng = random.Random(20261009)
        for repeat in range(3):
            block = [(m, mode) for mode in ('quiescent', 'active') for m in ('full', 'incremental')]
            block.append(('no_backup', 'active'))
            rng.shuffle(block)
            legacy.extend((repeat, m, mode) for m, mode in block)
        renamed = [(r, report_native.method_name(m), mode) for r, m, mode in legacy]
        self.assertEqual(native_baseline.trial_matrix(native_baseline.DEFAULT_METHODS, 3, 20261009),
                         renamed)

    def test_chosen_methods_each_run_in_both_modes_with_one_control(self):
        matrix = native_baseline.trial_matrix(['rsync_staged'], 2, 1)
        self.assertEqual(sorted((m, mode) for r, m, mode in matrix if r == 0),
                         [('no_backup', 'active'), ('rsync_staged', 'active'),
                          ('rsync_staged', 'quiescent')])
        self.assertEqual(len(matrix), 6)

    def test_methods_flag_accepts_only_distinct_registered_names(self):
        self.assertEqual(native_baseline.parse_methods('rsync_staged, pg_basebackup_full'),
                         ['rsync_staged', 'pg_basebackup_full'])
        for text in ('', 'full', 'no_backup', 'rsync_staged,rsync_staged'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                native_baseline.parse_methods(text)

    def test_bad_methods_flag_stops_before_preflight(self):
        with tempfile.TemporaryDirectory() as root, redirect_stderr(StringIO()):
            with mock.patch.object(native_baseline, 'preflight') as preflight:
                with self.assertRaises(SystemExit):
                    native_baseline.main(['--output', str(Path(root) / 'out'),
                                          '--methods', 'nope'])
            preflight.assert_not_called()

    def test_extra_tools_only_for_methods_that_use_them(self):
        args = SimpleNamespace(python='/py', rsync=sys.executable, pgdelta='python3 -m pgdelta',
                               bin_dir=None)
        self.assertEqual(native_baseline.extra_tools(args, native_baseline.DEFAULT_METHODS), {})
        tools = native_baseline.extra_tools(args, ['rsync_staged'])
        self.assertEqual(tools, {'python': '/py', 'rsync': sys.executable})
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'pg_controldata').write_text('')
            args.bin_dir = root
            tools = native_baseline.extra_tools(args, ['pg_delta_staged'])
        self.assertEqual(tools['pgdelta'], ['python3', '-m', 'pgdelta'])
        self.assertEqual(tools['pg_controldata'], str(Path(root) / 'pg_controldata'))
        args.rsync = str(Path(root) / 'absent')
        with self.assertRaises(ValueError):
            native_baseline.extra_tools(args, ['rsync_staged'])

    def test_backup_phase_runs_the_given_executable_and_keeps_its_output(self):
        with tempfile.TemporaryDirectory() as root:
            trial = native_baseline.Trial(Path(root), Path(root) / 'out', {}, None)
            record = trial.backup_phase('transfer', [sys.executable, '-c', 'print("moved")'])
            self.assertEqual(record['exit_code'], 0)
            self.assertEqual(trial.read_log('transfer'), 'moved\n')
            self.assertEqual(trial.commands[-1]['argv'][0], sys.executable)
            with self.assertRaises(OSError):
                trial.read_log('never-ran')


class ReportNativeTests(unittest.TestCase):
    def bundle(self, method):
        row = {'run_id': f'00-{method}-quiescent-r0', 'method': method, 'mode': 'quiescent',
               'status': 'pass', 'capture_seconds': 1.0, 'verified_backup_seconds': 2.0,
               'capture_bytes': 1048576}
        return {'status': 'pass', 'postgres_version': '17', 'trials': [row],
                'trial_order': [[0, method, 'quiescent'], [0, 'no_backup', 'active']]}

    def test_legacy_and_registry_labels_render_as_the_same_method(self):
        legacy = report_native.render(self.bundle('full'))
        current = report_native.render(self.bundle('pg_basebackup_full'))
        self.assertIn('| pg_basebackup_full | quiescent | 1 | 1.000 | 2.000 | 1.000 |', legacy)
        self.assertEqual(legacy.replace('00-full', '00-pg_basebackup_full'), current)
        self.assertNotIn('| no_backup |', current)

    def test_staged_methods_get_rows_and_an_honest_byte_caveat(self):
        report = report_native.render(self.bundle('rsync_staged'))
        self.assertIn('| rsync_staged | quiescent | 1 |', report)
        self.assertIn('not wire bytes', report)

    def test_preparation_inclusive_time_puts_staged_methods_on_equal_terms(self):
        """rsync_staged copies B into O untimed; pg_delta_staged builds O inside its timer."""
        bundle = self.bundle('rsync_staged')
        bundle['trials'][0].update(method_seconds=3.0, preparation_seconds=2.0)
        bundle['trials'].append({**bundle['trials'][0], 'run_id': '01-pg_delta_staged',
                                 'method': 'pg_delta_staged', 'method_seconds': 4.0})
        del bundle['trials'][1]['preparation_seconds']
        report = report_native.render(bundle)
        header = next(line for line in report.splitlines() if line.startswith('| Method |'))
        self.assertTrue(header.endswith('| Median preparation + method seconds |'), header)
        self.assertIn('| rsync_staged | quiescent | 1 | 1.000 | 2.000 | 1.000 | 5.000 |', report)
        self.assertIn('| pg_delta_staged | quiescent | 1 | 1.000 | 2.000 | 1.000 | 4.000 |',
                      report)
        self.assertIn('copies B0 into the output as untimed preparation', report)

    def test_rows_without_method_time_have_no_preparation_inclusive_time(self):
        report = report_native.render(self.bundle('pg_basebackup_full'))
        self.assertIn('| pg_basebackup_full | quiescent | 1 | 1.000 | 2.000 | 1.000 | unavailable |',
                      report)

    def test_historical_active_pass_is_not_upgraded_to_exact_cutoff_pass(self):
        bundle = self.bundle('pg_basebackup_full')
        bundle['trials'][0].update(mode='active', self_contained_restore={
            'logical_match': 'not_checked'}, pitr_restore={'logical_match': 'pass'})
        report = report_native.render(bundle)
        self.assertIn('| pass | not_checked | not checked | pass |', report)
        self.assertIn('Historical active PASS records', report)

    def test_exact_cutoff_verdict_is_reported_separately_from_later_pitr(self):
        bundle = self.bundle('pg_basebackup_full')
        bundle['trials'][0].update(mode='active', self_contained_restore={
            'logical_match': 'pass'}, active_package_exact_cutoff_oracle='pass')
        report = report_native.render(bundle)
        self.assertIn('| pass | pass | pass | not applicable |', report)


if __name__ == '__main__':
    unittest.main()
