"""The python -m pgdelta command line: local mode over pipes, TCP mode, exit codes and statistics."""
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from pgdelta import cli
from pgdelta.protocol import CATEGORIES
from pgdelta.tree import DEFAULT_JOBS

from pgdelta_support import (PAGE, REPO_ROOT, differing_pages, python_command, snapshot, staging_dirs,
                             subprocess_env, write_tree)


def run(*args, timeout=120):
    return subprocess.run(python_command(*args), capture_output=True, text=True, cwd=REPO_ROOT,
                          env=subprocess_env(), timeout=timeout)


def run_patched(patch, *args, timeout=120):
    """Run cli.main(args) in a child Python after executing `patch` there."""
    script = textwrap.dedent(patch) + '\nimport sys\nfrom pgdelta import cli\nsys.exit(cli.main(sys.argv[1:]))\n'
    return subprocess.run([sys.executable, '-c', script, *map(str, args)], capture_output=True, text=True,
                          cwd=REPO_ROOT, env=subprocess_env(), timeout=timeout)


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.basis, self.output = self.root / 'S', self.root / 'B', self.root / 'O'
        rng = random.Random(50)
        old = rng.randbytes(300 * PAGE + 123)
        new = bytearray(old)
        for page in (3, 150, 151, 299):
            new[page * PAGE + 7] ^= 0xFF
        write_tree(self.source, {'base/5/16384': bytes(new), 'base/5/16384_fsm': rng.randbytes(3 * PAGE),
                                 'PG_VERSION': b'17\n'}, ['pg_wal/archive_status'],
                   {'base/5/16384': 0o600})
        write_tree(self.basis, {'base/5/16384': old, 'base/5/dropped': rng.randbytes(PAGE)})

    def check_stats(self, path, expected_mode):
        stats = json.loads(Path(path).read_text())
        self.assertEqual((stats['schema_version'], stats['status'], stats['exit_code']), (1, 'complete', 0))
        self.assertEqual(stats['mode'], expected_mode)
        for direction in ('bytes_source_to_dest', 'bytes_dest_to_source'):
            self.assertEqual(sum(stats[direction][c] for c in CATEGORIES), stats[direction]['total'])
        self.assertEqual(stats['receiver']['bytes_read'], stats['bytes_source_to_dest'])
        timings = stats['timings_seconds']
        self.assertEqual(set(timings), {'hash_source', 'hash_basis', 'protocol', 'rebuild', 'copy_basis', 'fsync',
                                        'verify', 'verify_wait', 'pool_start', 'pool_first_ready', 'pool_ready',
                                        'receiver_pool_first_ready', 'receiver_pool_ready', 'total'})
        # A pool's workers may not all have started before a very short run ends.
        optional = {'pool_first_ready', 'pool_ready', 'receiver_pool_first_ready', 'receiver_pool_ready'}
        self.assertTrue(all(value is not None and value >= 0 for key, value in timings.items()
                            if key not in optional or value is not None))
        self.assertEqual(set(stats['switches']), {'sender', 'receiver'})
        self.assertEqual(set(stats['switches']['receiver']), {'jobs', 'hash_backend', 'rebuild', 'verify'})
        self.assertEqual(set(stats['files']), {'total', 'unchanged', 'changed', 'new', 'size_mismatch'})
        return stats


class CliTests(CliTestCase):
    def test_local_mode_rebuilds_and_writes_stats(self):
        before = snapshot(self.basis)
        stats_path = self.root / 'stats.json'
        result = run('local', '--source', self.source, '--basis', self.basis, '--output', self.output,
                     '--assume-same-cluster', '--stats-json', stats_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot(self.output), snapshot(self.source))
        self.assertEqual(snapshot(self.basis), before)
        stats = self.check_stats(stats_path, 'reuse')
        self.assertEqual(stats['pages']['sent'], differing_pages(self.source, self.basis))
        self.assertEqual(stats['files'], {'total': 3, 'unchanged': 0, 'changed': 1, 'new': 2,
                                          'size_mismatch': 0})
        self.assertIn('complete (reuse mode', result.stderr)

    def test_reuse_any_basis_and_its_alias_record_the_policy(self):
        for flag in ('--reuse-any-basis', '--assume-same-cluster'):
            with self.subTest(flag=flag):
                output, stats_path = self.root / f'O{flag}', self.root / f'stats{flag}.json'
                result = run('local', '--source', self.source, '--basis', self.basis, '--output', output,
                             flag, '--stats-json', stats_path)
                self.assertEqual(result.returncode, 0, result.stderr)
                stats = self.check_stats(stats_path, 'reuse')
                self.assertEqual(stats['reuse_policy'], 'any-basis')
                self.assertEqual(snapshot(output), snapshot(self.source))
        stats_path = self.root / 'stats-default.json'
        result = run('local', '--source', self.source, '--basis', self.basis, '--output', self.root / 'O-default',
                     '--stats-json', stats_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.check_stats(stats_path, 'full')['reuse_policy'], 'same-cluster')

    def test_help_explains_that_reuse_is_verified(self):
        for command in ('local', 'receive'):
            text = ' '.join(run(command, '--help').stdout.split())
            self.assertIn('--reuse-any-basis', text)
            self.assertIn('--assume-same-cluster', text)
            self.assertIn('verified', text)

    def test_local_mode_without_identity_sends_everything(self):
        stats_path = self.root / 'stats.json'
        result = run('local', '--source', self.source, '--basis', self.basis, '--output', self.output,
                     '--stats-json', stats_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        stats = self.check_stats(stats_path, 'full')
        self.assertEqual(stats['pages']['sent'], stats['pages']['total'])
        self.assertEqual(snapshot(self.output), snapshot(self.source))

    def test_tcp_receive_and_send(self):
        receiver = subprocess.Popen(
            python_command('receive', '--basis', self.basis, '--output', self.output,
                           '--listen', '127.0.0.1:0', '--assume-same-cluster', '--jobs', '2'),
            stderr=subprocess.PIPE, text=True, cwd=REPO_ROOT, env=subprocess_env())
        self.addCleanup(receiver.stderr.close)
        self.addCleanup(lambda: receiver.poll() is None and receiver.kill())
        line = receiver.stderr.readline()
        port = line.rsplit(':', 1)[1].strip()
        stats_path = self.root / 'stats.json'
        result = run('send', '--source', self.source, '--connect', f'127.0.0.1:{port}',
                     '--stats-json', stats_path, '--node-blocks', '16', '--jobs', '3')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(receiver.wait(timeout=60), 0)
        self.assertIn('published', receiver.stderr.read())
        stats = self.check_stats(stats_path, 'reuse')
        self.assertEqual(stats['node_blocks'], 16)
        self.assertEqual((stats['jobs'], stats['receiver']['jobs']), (3, 2))
        self.assertEqual(stats['pages']['sent'], differing_pages(self.source, self.basis))
        self.assertEqual(snapshot(self.output), snapshot(self.source))

    def test_engine_switches_reach_both_ends_and_change_nothing_but_the_timings(self):
        # (extra flags, expected sender switches, expected receiver switches)
        jobs = cli.default_jobs(sides=2)  # local: both ends on this host
        parallel = jobs > 1
        cases = {
            'defaults': ((), {'jobs': jobs, 'hash_backend': 'process' if parallel else 'serial'},
                         {'jobs': jobs, 'hash_backend': 'process' if parallel else 'serial',
                          'rebuild': 'copy-patch', 'verify': 'pipelined' if parallel else 'sequential'}),
            'one': (('--jobs', 1, '--verify', 'pipelined'), {'jobs': 1, 'hash_backend': 'serial'},
                    {'jobs': 1, 'hash_backend': 'serial', 'rebuild': 'copy-patch', 'verify': 'sequential'}),
            'processes': (('--jobs', 3), {'jobs': 3, 'hash_backend': 'process'},
                          {'jobs': 3, 'hash_backend': 'process', 'rebuild': 'copy-patch', 'verify': 'pipelined'}),
            'exp05': (('--jobs', 2, '--hash-backend', 'thread', '--rebuild', 'gap-fill', '--verify', 'sequential'),
                      {'jobs': 2, 'hash_backend': 'thread'},
                      {'jobs': 2, 'hash_backend': 'thread', 'rebuild': 'gap-fill', 'verify': 'sequential'}),
        }
        self.assertEqual(DEFAULT_JOBS, 1)  # the library default; the command line's is default_jobs()
        results = {}
        for name, (extra, sender_switches, receiver_switches) in cases.items():
            with self.subTest(case=name):
                output, stats_path = self.root / f'O-{name}', self.root / f'stats-{name}.json'
                result = run('local', '--source', self.source, '--basis', self.basis, '--output', output,
                             '--assume-same-cluster', '--node-blocks', '8', '--stats-json', stats_path, *extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(snapshot(output), snapshot(self.source))
                stats = self.check_stats(stats_path, 'reuse')
                # local passes every engine switch on to its child receiver
                self.assertEqual(stats['switches'], {'sender': sender_switches, 'receiver': receiver_switches})
                self.assertEqual((stats['jobs'], stats['receiver']['jobs']),
                                 (sender_switches['jobs'], receiver_switches['jobs']))
                results[name] = (stats['files'], stats['pages'], stats['nodes'],
                                 stats['bytes_source_to_dest']['total'],
                                 stats['bytes_dest_to_source']['node_digests'],
                                 stats['bytes_dest_to_source']['leaf_digests'],
                                 stats['receiver']['pages_copied'], stats['receiver']['bytes_copied'])
        for name, result in results.items():
            self.assertEqual(result, results['defaults'], name)

    def test_engine_switches_must_be_known_values(self):
        for flag, value in (('--rebuild', 'copy'), ('--verify', 'never'), ('--hash-backend', 'fork')):
            with self.subTest(flag=flag):
                result = run('local', '--source', self.source, '--basis', self.basis, '--output', self.output,
                             flag, value)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(flag, result.stderr)
        for command in ('receive', 'send'):
            result = run(command, '--help')
            self.assertEqual(result.returncode, 0)
            self.assertNotIn('--hash-backend', result.stdout)  # an experiment switch, kept out of --help
        self.assertIn('--rebuild', run('receive', '--help').stdout)
        self.assertNotIn('--rebuild', run('send', '--help').stdout)
        self.assertFalse(self.output.exists())

    def test_default_jobs_is_four_at_most_and_one_on_a_single_cpu(self):
        # EXP-06: --jobs 4 halved the wall time of --jobs 1; one CPU keeps the in-process engine.
        self.assertEqual(cli.MAX_DEFAULT_JOBS, 4)
        self.assertEqual([cli.default_jobs(cpus) for cpus in (1, 2, 3, 4, 8, 15, 64)], [1, 2, 3, 4, 4, 4, 4])
        self.assertEqual(cli.default_jobs(), min(4, cli.available_cpus()))
        self.assertGreaterEqual(cli.available_cpus(), 1)
        for command, sides, share in (('local', 2, 'CPUs / 2'), ('receive', 1, 'CPUs'), ('send', 1, 'CPUs')):
            with self.subTest(command=command):
                args = {'local': ['--source', 'S', '--basis', 'B', '--output', 'O'],
                        'receive': ['--basis', 'B', '--output', 'O', '--stdio'],
                        'send': ['--source', 'S', '--connect', 'h:1']}[command]
                self.assertEqual(cli.build_parser().parse_args([command, *args]).jobs,
                                 cli.default_jobs(sides=sides))
                self.assertEqual(cli.build_parser().parse_args([command, *args, '--jobs', '1']).jobs, 1)
                self.assertIn(f'min(4, {share})', ' '.join(run(command, '--help').stdout.split()))

    def test_a_live_source_defaults_to_two_jobs_at_most(self):
        # EXP-28: on a busy primary 4 source workers slowed the load more than pg_basebackup; 1 and 2 didn't.
        self.assertEqual(cli.LIVE_MAX_DEFAULT_JOBS, 2)
        with mock.patch.object(cli, 'available_cpus', return_value=8):
            parser = cli.build_parser()
            send = ['send', '--source', 'S', '--connect', 'h:1']
            self.assertEqual(cli.resolve_jobs(parser.parse_args([*send, '--source-kind', 'live'])).jobs, 2)
            self.assertEqual(cli.resolve_jobs(parser.parse_args([*send, '--source-kind', 'live', '--jobs', '4'])).jobs, 4)
            self.assertEqual(cli.resolve_jobs(parser.parse_args(send)).jobs, 4)
            local = ['local', '--source', 'S', '--basis', 'B', '--in-place', '--source-kind', 'live']
            self.assertEqual(cli.resolve_jobs(parser.parse_args(local)).jobs, 2)
            receive = parser.parse_args(['receive', '--basis', 'B', '--output', 'O', '--stdio'])
            self.assertEqual(cli.resolve_jobs(receive).jobs, 4)
        with mock.patch.object(cli, 'available_cpus', return_value=1):
            live = cli.build_parser().parse_args(['send', '--source', 'S', '--connect', 'h:1', '--source-kind', 'live'])
            self.assertEqual(cli.resolve_jobs(live).jobs, 1)
        self.assertIn('with --source-kind live at most 2', ' '.join(run('send', '--help').stdout.split()))

    def test_local_splits_the_cpus_between_its_two_ends(self):
        # `local` runs N workers on each side: on 4-7 CPUs min(4, CPUs) per side would start up to 8
        # workers plus the two pgdelta processes (EXP-06 review), so each side gets CPUs // 2.
        self.assertEqual([cli.default_jobs(cpus, sides=2) for cpus in (1, 2, 3, 4, 5, 6, 7, 8, 15, 64)],
                         [1, 1, 1, 2, 2, 3, 3, 4, 4, 4])
        for cpus in range(1, 65):
            self.assertLessEqual(2 * cli.default_jobs(cpus, sides=2), max(2, cpus))
        with mock.patch.object(cli, 'available_cpus', return_value=4):
            local = cli.build_parser().parse_args(['local', '--source', 'S', '--basis', 'B', '--output', 'O'])
            send = cli.build_parser().parse_args(['send', '--source', 'S', '--connect', 'h:1'])
            receive = cli.build_parser().parse_args(['receive', '--basis', 'B', '--output', 'O', '--stdio'])
        self.assertEqual((local.jobs, send.jobs, receive.jobs), (2, 4, 4))

    def test_jobs_must_be_a_positive_whole_number(self):
        for command in (('local', '--source', self.source, '--basis', self.basis, '--output', self.output),
                        ('receive', '--basis', self.basis, '--output', self.output, '--stdio'),
                        ('send', '--source', self.source, '--connect', '127.0.0.1:1')):
            for value in ('0', '-2', 'two', '1.5'):
                with self.subTest(command=command[0], value=value):
                    result = run(*command, '--jobs', value)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn('--jobs', result.stderr)
        self.assertFalse(self.output.exists())

    def test_existing_output_exits_2_and_is_untouched(self):
        self.output.mkdir()
        (self.output / 'keep').write_text('precious')
        result = run('local', '--source', self.source, '--basis', self.basis, '--output', self.output)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('already exists', result.stderr)
        self.assertEqual((self.output / 'keep').read_text(), 'precious')

    def test_refusals_reported_by_the_child_receiver_exit_2(self):
        (self.basis / 'postmaster.pid').write_text('1\n')
        stats_path = self.root / 'stats.json'
        result = run('local', '--source', self.source, '--basis', self.basis, '--output', self.output,
                     '--stats-json', stats_path)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('postmaster.pid', result.stderr)
        stats = json.loads(stats_path.read_text())
        self.assertEqual((stats['status'], stats['exit_code']), ('failed', 2))
        self.assertFalse(self.output.exists())
        self.assertEqual(staging_dirs(self.output), [])

    def test_overlapping_paths_exit_2(self):
        result = run('local', '--source', self.source, '--basis', self.basis, '--output', self.source / 'O')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('overlap', result.stderr)

    def test_missing_pg_controldata_exits_2(self):
        result = run('local', '--source', self.source, '--basis', self.basis, '--output', self.output,
                     '--pg-controldata', self.root / 'no-such-binary')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('pg_controldata not found', result.stderr)

    def test_connection_refused_exits_3(self):
        result = run('send', '--source', self.source, '--connect', '127.0.0.1:1')
        self.assertEqual(result.returncode, 3, result.stderr)

    def test_stdio_receiver_rejects_garbage(self):
        result = subprocess.run(python_command('receive', '--basis', self.basis, '--output', self.output,
                                               '--stdio'),
                                input=b'\x00\x00\x00\x01\x63x', capture_output=True, cwd=REPO_ROOT,
                                env=subprocess_env(), timeout=60)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn(b'unknown message type 99', result.stderr)
        self.assertFalse(self.output.exists())
        self.assertEqual(staging_dirs(self.output), [])

    def test_usage_errors_exit_2(self):
        self.assertEqual(run('receive', '--basis', self.basis, '--output', self.output).returncode, 2)
        self.assertEqual(run('local', '--source', self.source).returncode, 2)
        self.assertEqual(run('--help').returncode, 0)


class StatsAndUnexpectedErrorTests(CliTestCase):
    """Statistics problems never turn a transfer into a traceback, and unexpected errors exit 3."""

    def local_args(self, stats_path):
        return ('local', '--source', self.source, '--basis', self.basis, '--output', self.output,
                '--assume-same-cluster', '--stats-json', stats_path)

    def assert_not_started(self):
        self.assertFalse(self.output.exists())
        self.assertEqual(staging_dirs(self.output), [])

    def test_missing_stats_directory_is_refused_before_the_transfer(self):
        result = run(*self.local_args(self.root / 'no-such-dir' / 'stats.json'))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('statistics', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assert_not_started()

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root can write anywhere')
    def test_read_only_stats_directory_is_refused_before_the_transfer(self):
        locked = self.root / 'locked'
        locked.mkdir(mode=0o500)
        self.addCleanup(os.chmod, locked, 0o700)
        result = run(*self.local_args(locked / 'stats.json'))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('statistics', result.stderr)
        self.assertEqual(list(locked.iterdir()), [])
        self.assert_not_started()

    def test_stats_file_inside_the_basis_is_refused(self):
        before = snapshot(self.basis)
        result = run(*self.local_args(self.basis / 'run.json'))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('overlaps basis', result.stderr)
        self.assertEqual(snapshot(self.basis), before)
        self.assert_not_started()

    def test_stats_write_failure_after_publication_keeps_exit_0(self):
        result = run_patched("""
            from pgdelta import cli
            def broken(path, value):
                raise OSError(28, 'No space left on device')
            cli.write_json = broken
        """, *self.local_args(self.root / 'stats.json'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('could not write statistics', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(snapshot(self.output), snapshot(self.source))

    def test_stats_write_failure_keeps_the_refusal_exit_code(self):
        (self.basis / 'postmaster.pid').write_text('1\n')
        result = run_patched("""
            from pgdelta import cli
            def broken(path, value):
                raise OSError(28, 'No space left on device')
            cli.write_json = broken
        """, *self.local_args(self.root / 'stats.json'))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('postmaster.pid', result.stderr)
        self.assertIn('could not write statistics', result.stderr)
        self.assert_not_started()

    def test_unexpected_exception_exits_3_and_writes_failure_stats(self):
        stats_path = self.root / 'stats.json'
        result = run_patched("""
            from pgdelta import sender
            def boom(*args, **kwargs):
                raise RuntimeError('boom')
            sender.run_sender = boom
        """, *self.local_args(stats_path))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('internal error', result.stderr)
        self.assertIn('boom', result.stderr)
        stats = json.loads(stats_path.read_text())
        self.assertEqual((stats['status'], stats['exit_code']), ('failed', 3))
        self.assertIn('boom', stats['error'])
        self.assert_not_started()

    def test_unexpected_exception_in_receive_exits_3(self):
        result = run_patched("""
            from pgdelta import receiver
            def boom(*args, **kwargs):
                raise RuntimeError('boom')
            receiver.run_receiver = boom
        """, 'receive', '--basis', self.basis, '--output', self.output, '--stdio')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('internal error', result.stderr)


if __name__ == '__main__':
    unittest.main()


class CongestionControlTests(unittest.TestCase):
    """EXP-23's follow-up: --congestion-control asks for a congestion control on the sender's socket."""

    def test_connect_reports_what_the_socket_got(self):
        import socket as _socket
        from pgdelta import transport
        listener = _socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        try:
            channel = transport.connect_channel('127.0.0.1', listener.getsockname()[1], congestion_control='reno')
            accepted, _ = listener.accept()
            report = channel.congestion_control
            self.assertEqual(report['requested'], 'reno')
            if hasattr(_socket, 'TCP_CONGESTION'):
                self.assertEqual((report['effective'], report['error']), ('reno', None))
            else:
                self.assertIsNone(report['effective'])
                self.assertIn('not available', report['error'])
            accepted.close()
            channel.close()
        finally:
            listener.close()

    def test_without_the_option_nothing_is_set(self):
        import socket as _socket
        from pgdelta import transport
        listener = _socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        try:
            channel = transport.connect_channel('127.0.0.1', listener.getsockname()[1])
            accepted, _ = listener.accept()
            self.assertIsNone(channel.congestion_control)
            accepted.close()
            channel.close()
        finally:
            listener.close()

    def test_the_send_command_takes_the_option(self):
        from pgdelta import cli
        parser = cli.build_parser() if hasattr(cli, 'build_parser') else None
        if parser is None:
            self.skipTest('no build_parser')
        args = parser.parse_args(['send', '--source', 'S', '--connect', 'h:1', '--congestion-control', 'bbr'])
        self.assertEqual(args.congestion_control, 'bbr')
