"""Tests for EXP-11's core harness (T2, the fastest durable full copy): experiments/network.

No VM, PostgreSQL, strace or root is needed. The orchestrator runs with the stubs of
test_exp04_network; /proc text, pg_basebackup's and pg_probackup's lines are fixed samples;
the host probes run on temporary files, and the link test over loopback.
"""

import csv
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from unittest import mock

from benchmarks.backup import basebackup_command
from experiments.network import agent, measure, run_exp04, source_cluster

from test_exp04_network import FakeChild, FakeClock, LINK, SPEED, StubOrchestrator, TRIAL, TrialStub, snap

T2_ARMS = ('full', 'full_nosync', 'full_syncfs', 'full_wb', 'probackup8', 'probackup4', 'parcopy8', 'parcopy4',
           'pgdelta_direct_inplace', 'pgdelta_direct_inplace_j8')
CRC32C = ['--manifest-checksums', 'CRC32C']
UNTHROTTLED = {'label': 'unthrottled', 'gbit': None, 'tc_rate': 'off'}
MIB = 1 << 20

# pg_basebackup 17 --verbose to a plain directory, streaming WAL, as Child(stamp_lines=True) logs it
# (stdout and stderr through one pipe, each line stamped with the seconds since the launch).
BASEBACKUP_LOG = """\
[+0.004512] pg_basebackup: initiating base backup, waiting for checkpoint to complete
[+0.094870] pg_basebackup: checkpoint completed
[+0.095311] pg_basebackup: write-ahead log start point: 2/9A000028 on timeline 1
[+0.095402] pg_basebackup: starting background WAL receiver
[+0.098120] pg_basebackup: created temporary replication slot "pg_basebackup_41233"
[+1.701233] pg_basebackup: write-ahead log end point: 2/9A000158
[+1.701301] pg_basebackup: waiting for background process to finish streaming ...
[+1.752011] pg_basebackup: syncing data to disk ...
[+3.051222] pg_basebackup: renaming backup_manifest.tmp to backup_manifest
[+3.052870] pg_basebackup: base backup completed
"""
# The same with --no-sync: no "syncing data to disk" line.
BASEBACKUP_NOSYNC_LOG = """\
[+0.004100] pg_basebackup: initiating base backup, waiting for checkpoint to complete
[+0.081200] pg_basebackup: checkpoint completed
[+0.081600] pg_basebackup: write-ahead log start point: 2/9C000028 on timeline 1
[+0.081700] pg_basebackup: starting background WAL receiver
[+0.084300] pg_basebackup: created temporary replication slot "pg_basebackup_41301"
[+1.655400] pg_basebackup: write-ahead log end point: 2/9C000158
[+1.655500] pg_basebackup: waiting for background process to finish streaming ...
[+1.702300] pg_basebackup: renaming backup_manifest.tmp to backup_manifest
[+1.703100] pg_basebackup: base backup completed
"""
# pg_probackup 2.5.16 catchup -b FULL's INFO lines on its console, stamped as above, in the order its
# source prints them (src/catchup.c, src/backup.c): nothing marks the end of the copy threads.
PROBACKUP_LOG = """\
[+0.012000] INFO: Catchup start, pg_probackup version: 2.5.16, PostgreSQL version: 17.6, remote: true, catchup-mode: FULL
[+0.013000] INFO: Database catchup start
[+0.398000] INFO: pg_start_backup START LSN 2/A0000028
[+0.402000] INFO: Source PGDATA size: 2112MB (excluded 0kB)
[+0.410000] INFO: Start LSN (source): 2/A0000028, TLI: 1
[+0.512000] INFO: Start transferring data files
[+2.912000] INFO: wait for pg_stop_backup()
[+2.960000] INFO: pg_stop backup() successfully executed
[+2.961000] INFO: stop_lsn: 2/A0000158
[+3.008000] INFO: Databases synchronized. Transfered datafiles size: 2112MB, transfered wal size: 16MB, time elapsed: 2s
[+3.010000] INFO: Syncing copied files to disk
[+3.705000] INFO: Files are synced, time elapsed: 0
"""
# /proc/meminfo on pgd-dest (Debian 12, kernel 6.1, 32 GB), trimmed.
MEMINFO = """\
MemTotal:       32864388 kB
MemFree:        28012340 kB
MemAvailable:   31654312 kB
Buffers:           53412 kB
Cached:          3401236 kB
Dirty:              1228 kB
Writeback:            16 kB
AnonPages:        176512 kB
HugePages_Total:       0
Hugepagesize:       2048 kB
"""
# /proc/diskstats (kernel 6.1: 17 counters after the name), trimmed.
DISKSTATS = """\
   7       0 loop0 52 0 2214 13 0 0 0 0 0 36 13 0 0 0 0 0 0
   8       0 sda 12345 2345 987654 4321 6789 3456 456789 9876 0 12000 15000 0 0 0 0 512 210
   8       1 sda1 12000 2300 980000 4300 6700 3400 456000 9800 0 11900 14900 0 0 0 0 0 0
 259       0 nvme0n1 4000 0 1024000 900 8000 0 4096000 2000 0 3000 2900 0 0 0 0 0 0
   9     127 md127 16000 0 4096000 0 32000 0 16384000 0 0 0 0 0 0 0 0 0 0
"""
# What `agent.py datadir-digest` reports of a stopped source (files_digest's shape).
SOURCE_DIGEST = {'root': '/data/exp04/src/data', 'device': 1, 'files': 5, 'bytes': 40,
                 'entries': {'base/5/16397': ['file', 8, 'a' * 64, 0o600, 1, 1],
                             'global/1262': ['file', 8, 'b' * 64, 0o600, 2, 1],
                             'global/pg_control': ['file', 8, 'c' * 64, 0o600, 3, 1],
                             'pg_wal/000000010000000000000003': ['file', 8, 'd' * 64, 0o600, 4, 1],
                             'pg_stat/pgstat.stat': ['file', 8, 'e' * 64, 0o600, 5, 1]},
                 'missing': [], 'read_from': {}}


class Kernel:
    """vm.dirty_background_ratio and _bytes as Linux keeps them: setting one zeroes the other, and
    0 written to the bytes setting is refused."""

    def __init__(self, ratio=10, size=0):
        self.values = {measure.DIRTY_BACKGROUND_RATIO: ratio, measure.DIRTY_BACKGROUND_BYTES: size}
        self.writes = []

    def set(self, name, value):
        value = int(value)
        self.writes.append((name, value))
        if name == measure.DIRTY_BACKGROUND_BYTES:
            if value < 1:
                raise run_exp04.CommandError('sysctl: setting key "vm.dirty_background_bytes": Invalid argument')
            self.values.update({name: value, measure.DIRTY_BACKGROUND_RATIO: 0})
        else:
            self.values.update({name: value, measure.DIRTY_BACKGROUND_BYTES: 0})


class T2Stub(StubOrchestrator):
    """StubOrchestrator with dest's kernel settings, Dirty/Writeback and disk counters in its snapshots,
    and pg_basebackup outputs whose stamped logs say what PostgreSQL 17 says."""

    def __init__(self, tmp, clock, extra_args=()):
        super().__init__(tmp, clock, ['--manifest-checksums', 'CRC32C', *extra_args])
        self.kernel = Kernel()
        self.writeback_defaults = {measure.DIRTY_BACKGROUND_RATIO: 10, measure.DIRTY_BACKGROUND_BYTES: 0}
        self.dirty_kb = 1000  # dest's Dirty after the next window, in kB
        self.window_writes = 0  # dest's md array writes per window, bytes
        self.snapshots = {'source': 0, 'dest': 0}

    def _snap(self, host):
        value = super()._snap(host)
        self.snapshots[host] += 1
        before = self.snapshots[host] % 2 == 1  # snapshots come in pairs around each window
        value['meminfo'] = {'Dirty': 1000 if before or host == 'source' else self.dirty_kb, 'Writeback': 0,
                            'Cached': 0}
        sectors = (self.snapshots[host] // 2) * (self.window_writes // 512) if host == 'dest' else 0
        value['disks'] = {'md127': {'reads': 0, 'sectors_read': 0, 'writes': 0, 'sectors_written': sectors}}
        return value

    def local_agent(self, *args, timeout=600):
        self.events.append(('local_agent', args))
        if args[:2] == ('sysctl', 'get'):
            return {'values': {name: self.kernel.values[name] for name in args[2:]}}
        if args[:2] == ('sysctl', 'set'):
            for item in args[2:]:
                self.kernel.set(*item.split('='))
            return {'set': dict(item.split('=') for item in args[2:]), 'values': dict(self.kernel.values)}
        return {}

    def tool_reported(self, method, paths, phases):
        # The real account for T2's own arms; the stub's for pgdelta, whose statistics it doesn't write.
        if method in measure.FULL_VARIANTS or method in measure.PROBACKUP_METHODS:
            return run_exp04.Orchestrator.tool_reported(self, method, paths, phases)
        return super().tool_reported(method, paths, phases)

    def local_command(self, argv, log_name, *, timeout=None, cwd=None, env=None, stamp_lines=False):
        result = super().local_command(argv, log_name, timeout=timeout, cwd=cwd, env=env, stamp_lines=stamp_lines)
        if Path(argv[0]).name == 'pg_basebackup':
            log = self.dest_log(log_name)
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(BASEBACKUP_NOSYNC_LOG if '--no-sync' in argv else BASEBACKUP_LOG)
            result.update(log=str(log), stamped_lines=stamp_lines, env=env)
        return result


class Clocked(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        for patcher in (mock.patch.object(run_exp04.time, 'perf_counter', self.clock),
                        mock.patch.object(run_exp04, 'log')):
            patcher.start()
            self.addCleanup(patcher.stop)
        FakeChild.started.clear()

    def orchestrator(self, cls=T2Stub, extra_args=()):
        orchestrator = cls(self.tmp.name, self.clock, extra_args)
        self.addCleanup(orchestrator.pool.shutdown)
        return orchestrator

    def run_method(self, orchestrator, method, link=None, speed=SPEED):
        with mock.patch.object(run_exp04, 'Child', FakeChild):
            return orchestrator.run_method(TRIAL, 0, speed, dict(link or LINK), method, 0, [method])

    @staticmethod
    def kinds(orchestrator):
        kinds = []
        for event in orchestrator.events:
            if isinstance(event, str):
                kinds.append(event)
            elif event[0] in ('dest', 'source'):
                kinds.append((event[0], Path(event[1][0]).name))
            elif event[0] in ('source_script', 'local_agent', 'source_agent'):
                kinds.append((event[0], event[1][0] if isinstance(event[1], tuple) else event[1]))
            else:
                kinds.append(event[0])
        return kinds


# --- the method tables and command lines ---------------------------------------------

class MethodTables(unittest.TestCase):
    def test_the_arms_are_pinned(self):
        self.assertEqual(measure.FULL_VARIANTS, {
            'full': ('--verbose',), 'full_nosync': ('--verbose', '--no-sync'),
            'full_syncfs': ('--verbose', '--sync-method=syncfs'), 'full_wb': ('--verbose',)})
        self.assertEqual(measure.HOST_TUNED_METHODS, {'full_wb': {'vm.dirty_background_bytes': 64 * MIB}})
        self.assertEqual(measure.NON_DURABLE_METHODS, ('full_nosync',))
        self.assertEqual(measure.PROBACKUP_VARIANTS, {'probackup8': {'jobs': 8}, 'probackup4': {'jobs': 4}})
        self.assertEqual(measure.PARCOPY_VARIANTS, {'parcopy8': {'streams': 8}, 'parcopy4': {'streams': 4}})
        self.assertEqual(set(measure.TIERS), set(T2_ARMS))
        self.assertEqual({method for method, tier in measure.TIERS.items() if tier in ('omni_today', 'released')},
                         {'full', 'full_syncfs', 'probackup8', 'probackup4'})
        self.assertEqual({method for method, tier in measure.TIERS.items() if tier == 'bound'},
                         {'parcopy8', 'parcopy4'})
        self.assertEqual(measure.TIERS['full_wb'], 'host_tuned')
        self.assertEqual(measure.TIERS['full_nosync'], 'diagnostic')
        # Every new arm is optional; the default sweep is as before.
        for method in measure.T2_METHODS:
            self.assertIn(method, measure.OPTIONAL_METHODS)
            self.assertNotIn(method, measure.METHODS)
            self.assertIn(method, measure.PROC_SCOPE)
        self.assertEqual(len(set(measure.ALL_METHODS)), len(measure.ALL_METHODS))
        self.assertEqual(set(measure.T2_METHODS), set(T2_ARMS) - {'full', 'pgdelta_direct_inplace'})

    def test_the_arms_fall_into_their_groups(self):
        self.assertEqual(set(measure.SOURCE_STOP_METHODS),
                         {'pgdelta_direct', 'pgdelta_direct_inplace', 'pgdelta_direct_inplace_j8', 'parcopy8',
                          'parcopy4'})
        for method in ('full', 'full_nosync', 'full_syncfs', 'full_wb', 'probackup8', 'probackup4', 'parcopy8',
                       'parcopy4', 'pgdelta_direct_inplace_j8'):
            self.assertIn(method, measure.OUTPUT_START_METHODS)
        self.assertNotIn('incremental', measure.OUTPUT_START_METHODS)
        self.assertEqual(set(measure.FULL_COPY_METHODS),
                         {'full', 'full_nosync', 'full_syncfs', 'full_wb', 'probackup8', 'probackup4', 'parcopy8',
                          'parcopy4'})
        self.assertEqual(set(measure.MULTI_STREAM_METHODS), {'probackup8', 'probackup4', 'parcopy8', 'parcopy4'})
        for method in measure.FULL_VARIANTS:
            self.assertEqual(measure.PROC_SCOPE[method], measure.PROC_SCOPE['full'])
        for method in (*measure.PROBACKUP_METHODS, *measure.PARCOPY_METHODS):
            self.assertIn('walsender', measure.PROC_SCOPE[method])
            self.assertIn('largest single process', measure.PROC_SCOPE[method])

    def test_v1_at_8_jobs(self):
        self.assertIn('pgdelta_direct_inplace_j8', measure.PGDELTA_DIRECT_METHODS)
        self.assertIn('pgdelta_direct_inplace_j8', measure.IN_PLACE_METHODS)
        self.assertIn('pgdelta_direct_inplace_j8', measure.BASIS_COPY_METHODS)
        engines = measure.PGDELTA_ENGINES
        four, eight = engines['pgdelta_direct_inplace'], engines['pgdelta_direct_inplace_j8']
        self.assertEqual(eight, {**four, 'jobs': 8})
        self.assertEqual(measure.PGDELTA_ARGS['pgdelta_direct_inplace_j8'],
                         {'send': ('--jobs', '8', '--source-kind', 'datadir'),
                          'receive': ('--jobs', '8', '--verify', 'pipelined', '--prefetch-basis', 'on', '--fsync',
                                      'main', '--in-place')})
        self.assertEqual(measure.pgdelta_switches(eight)['receiver']['jobs'], 8)
        self.assertIn('At --jobs 8 each starts 8 worker processes', measure.PROC_SCOPE['pgdelta_direct_inplace_j8'])
        self.assertIn('At --jobs 4 each starts 4 worker processes', measure.PROC_SCOPE['pgdelta_direct_inplace'])

    def test_basebackup_command_takes_extra_options_last_and_is_unchanged_without(self):
        common = ['pg_basebackup', '--pgdata', 'O', '--format', 'plain', '--wal-method', 'stream',
                  '--manifest-checksums', 'CRC32C', '--checkpoint', 'fast', '--host', 'h', '--port', '5432',
                  '--username', 'study', '--no-password']
        self.assertEqual(basebackup_command('pg_basebackup', 'O', host='h', port=5432, user='study',
                                            checksum='CRC32C'), common)
        self.assertEqual(basebackup_command('pg_basebackup', 'O', host='h', port=5432, user='study',
                                            checksum='CRC32C', extra=('--verbose', '--no-sync')),
                         common + ['--verbose', '--no-sync'])

    def test_the_probackup_and_parcopy_command_lines(self):
        argv = measure.probackup_catchup_argv(
            '/usr/local/bin/pg_probackup-17', 'probackup8', source_pgdata='/w/src/data',
            dest_pgdata='/w/out/probackup8', source_ip='10.0.0.12', port=5432, user='study', database='postgres', ssh_user='bench',
            ssh_config='/w/probackup/ssh_config')
        self.assertEqual(argv, [
            '/usr/local/bin/pg_probackup-17', 'catchup', '-b', 'FULL', '--stream', '--temp-slot', '-j', '8',
            '--source-pgdata=/w/src/data', '--destination-pgdata=/w/out/probackup8', '--pghost=10.0.0.12',
            '--pgport=5432', '--pguser=study', '--pgdatabase=postgres', '--remote-proto=ssh',
            '--remote-host=10.0.0.12', '--remote-user=bench', '--remote-path=/usr/local/bin',
            '--ssh-options=-F /w/probackup/ssh_config', '--log-level-console=INFO'])
        self.assertEqual(measure.probackup_args('probackup4')[-2:], ('-j', '4'))
        config = measure.probackup_ssh_config('10.0.0.12', 'bench', '/k/key')
        for line in ('Host 10.0.0.12', 'IdentityFile /k/key', 'IdentitiesOnly yes', 'BatchMode yes',
                     'ControlMaster no', 'Ciphers aes128-gcm@openssh.com'):
            self.assertIn(line, config)
        self.assertEqual(measure.parcopy_args('parcopy4'),
                         {'send': ('--streams', '4', '--chunk-bytes', str(64 * MIB)),
                          'receive': ('--streams', '4', '--writeback-bytes', str(8 * MIB))})


class CommandLines(Clocked):
    def test_each_full_variant_is_full_with_its_options(self):
        orchestrator = self.orchestrator()
        output = orchestrator.outputs
        for method, extra in measure.FULL_VARIANTS.items():
            with self.subTest(method=method):
                argv = orchestrator.full_argv(method, orchestrator.method_paths(method))
                self.assertEqual([str(part) for part in argv],
                                 [str(part) for part in orchestrator.basebackup(output / method, host='10.0.0.12')]
                                 + list(extra))
                self.assertIn('--verbose', argv)
                self.assertNotIn('--progress', argv)
                self.assertEqual(argv[argv.index('--manifest-checksums') + 1], 'CRC32C')

    def test_run_json_method_flags_carry_each_new_arms_full_command_line(self):
        orchestrator = self.orchestrator()
        flags = {method: orchestrator.method_flags(method) for method in T2_ARMS}
        for method in measure.FULL_VARIANTS:
            self.assertEqual(flags[method]['argv'],
                             [str(part) for part in orchestrator.full_argv(method, orchestrator.method_paths(method))])
            self.assertEqual(flags[method]['pg_basebackup'], [*CRC32C, *measure.FULL_VARIANTS[method]])
        self.assertIn('--no-sync', flags['full_nosync']['argv'])
        self.assertIn('--sync-method=syncfs', flags['full_syncfs']['argv'])
        self.assertEqual(flags['full_wb']['sysctl'], {'vm.dirty_background_bytes': 64 * MIB})
        self.assertIn('vm.dirty_background_ratio', flags['full_wb']['sysctl_reset'])
        # No new arm falls through to pg_basebackup's flags.
        for method in ('probackup8', 'probackup4', 'parcopy8', 'parcopy4', 'pgdelta_direct_inplace_j8'):
            self.assertNotIn('pg_basebackup', flags[method])
        for method in measure.PROBACKUP_METHODS:
            paths = orchestrator.method_paths(method)
            self.assertEqual(flags[method]['pg_probackup'], orchestrator.probackup_argv(method, paths))
            self.assertEqual(flags[method]['pg_probackup'][:2], ['/usr/local/bin/pg_probackup-17', 'catchup'])
            self.assertIn(f'--destination-pgdata={orchestrator.outputs / method}', flags[method]['pg_probackup'])
            self.assertEqual(flags[method]['sync'], ['sync', '-f', str(orchestrator.outputs / method)])
            self.assertIn('Ciphers=aes128-gcm@openssh.com', flags[method]['ssh_options'])
            self.assertIn('IdentityFile /nonexistent', flags[method]['ssh_config'])
        for method in measure.PARCOPY_METHODS:
            streams = str(measure.PARCOPY_VARIANTS[method]['streams'])
            receive, send = flags[method]['receive_argv'], flags[method]['send_argv']
            self.assertEqual(receive[1:4], ['-m', 'experiments.network.parcopy', 'receive'])
            self.assertEqual(receive[receive.index('--output') + 1], str(orchestrator.outputs / method))
            self.assertEqual(receive[receive.index('--listen') + 1], '10.0.0.13:7200')
            self.assertEqual(send[1:4], ['-m', 'experiments.network.parcopy', 'send'])
            self.assertEqual(send[send.index('--source') + 1], orchestrator.source_data)
            self.assertEqual(send[send.index('--connect') + 1], '10.0.0.13:7200')
            self.assertEqual(receive[receive.index('--streams') + 1], streams)
            self.assertEqual(send[send.index('--streams') + 1], streams)
            self.assertEqual(flags[method]['send'], list(measure.parcopy_args(method)['send']))
        self.assertEqual(flags['pgdelta_direct_inplace_j8'],
                         {command: list(args) for command, args in
                          measure.PGDELTA_ARGS['pgdelta_direct_inplace_j8'].items()})

    def test_method_paths_of_the_new_arms(self):
        orchestrator = self.orchestrator()
        paths = orchestrator.method_paths('parcopy8')
        self.assertEqual(paths['source_data'], orchestrator.source_data)
        self.assertEqual(paths['stats'], f'{orchestrator.work}/stats/parcopy8-send.json')
        self.assertEqual(paths['receive_stats'], orchestrator.work / 'stats' / 'parcopy8-receive.json')
        paths = orchestrator.method_paths('probackup4')
        self.assertEqual((paths['source_data'], paths['ssh_config'], paths['probackup_log_dir']),
                         (orchestrator.source_data, f'{orchestrator.work}/probackup/ssh_config',
                          f'{orchestrator.work}/probackup/log'))
        self.assertEqual(orchestrator.method_paths('full_syncfs'), {'output': orchestrator.outputs / 'full_syncfs'})


class RunJson(Clocked):
    """execute() with its VM steps stubbed: the guard, the preflight and what run.json holds."""

    class Executing(T2Stub):
        def _source_agent(self, *args, timeout=600):
            self.events.append(('source_agent', args))
            if args[0] == 'iface':
                return {'iface': 'ens4'}
            return {'exit_code': 0, 'log_tail': ''}

        def throttle(self, rate):
            return {}

        def environment(self):
            env = {'deployed': {'tree_sha256': 'x'}, 'strace': 'strace -- version 6.1',
                   'pg_basebackup_sync_method': True, 'pg_probackup': 'pg_probackup-17 2.5.16'}
            return {'source': dict(env), 'dest': dict(self.dest_env, **{'deployed': {'tree_sha256': 'x'}})}

        dest_env = {'strace': 'strace -- version 6.1', 'pg_basebackup_sync_method': True}

        def check_pgdelta_jobs(self):
            return {}

        def check_probackup(self):
            self.events.append('check_probackup')
            return {'version': '2.5.16'}

        def check_parcopy(self):
            self.events.append('check_parcopy')
            return {}

        def run_trial(self, index, pattern, fraction):
            self.events.append(('trial', pattern))
            return {'trial_id': f'{index:02d}-{pattern}', 'status': 'pass'}

    def execute(self, methods, extra=(), cls=None):
        args = ['--methods', ','.join(methods), '--speeds', '2,unthrottled', '--patterns', 'hot',
                '--gnu-time', 'none', '--disk-test-mib', '0', '--idle-seconds', '0', '--phase-floor-repeats', '0',
                *extra]
        orchestrator = self.orchestrator(cls or self.Executing, args)
        route = mock.Mock(stdout='10.0.0.12 dev ens4 src 10.0.0.13 uid 1000\n', returncode=0)
        with mock.patch.object(run_exp04.subprocess, 'run', return_value=route), \
                mock.patch.object(orchestrator.source, 'open'), mock.patch.object(orchestrator.source, 'close'):
            code = orchestrator.execute()
        return orchestrator, code, json.loads((orchestrator.out / 'run.json').read_text())

    def test_run_json_holds_every_new_arms_command_line_and_the_blocks(self):
        methods = list(T2_ARMS)
        orchestrator, code, run = self.execute(methods, [
            '--extension-repeats', '5', '--extension-methods', 'full,probackup8,pgdelta_direct_inplace',
            '--warm-source-repeats', '3', '--warm-source-methods', 'full,probackup8,pgdelta_direct_inplace'])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(run['method_flags']), sorted(methods))
        self.assertEqual(run['method_flags']['full_nosync']['argv'][-2:], ['--verbose', '--no-sync'])
        self.assertEqual(run['method_flags']['probackup8']['pg_probackup'][1:8],
                         ['catchup', '-b', 'FULL', '--stream', '--temp-slot', '-j', '8'])
        self.assertIn('--streams', run['method_flags']['parcopy4']['send_argv'])
        self.assertEqual(run['method_flags']['pgdelta_direct_inplace_j8']['send'], ['--jobs', '8', '--source-kind',
                                                                                     'datadir'])
        self.assertTrue(run['t2'])
        self.assertEqual(run['blocks']['extension'], {'repeats': 5, 'methods': ['full', 'probackup8',
                                                                               'pgdelta_direct_inplace'],
                                                      'speed': 'unthrottled', 'source_cache': 'cold'})
        self.assertEqual(run['blocks']['warm_source']['source_cache'], 'warm')
        # The preflight: strace, the two blocks' hooks and dest's write-back defaults.
        self.assertEqual(run['t2_preflight']['writeback_defaults'],
                         {'vm.dirty_background_ratio': 10, 'vm.dirty_background_bytes': 0})
        self.assertEqual(run['t2_preflight']['strace'], 'strace -- version 6.1')
        self.assertIn('check_probackup', orchestrator.events)
        self.assertIn('check_parcopy', orchestrator.events)
        self.assertEqual(orchestrator.writeback_defaults, run['t2_preflight']['writeback_defaults'])
        self.assertEqual(run['idle_noise_bytes'], 0)
        self.assertIn('4 MiB', run['durability'])

    def test_a_t2_run_refuses_without_strace_or_with_write_back_changed(self):
        class NoStrace(self.Executing):
            dest_env = {'strace': None, 'pg_basebackup_sync_method': True}

        with self.assertRaises(SystemExit) as refused:
            self.execute(['full', 'full_nosync'], cls=NoStrace)
        self.assertIn('strace', str(refused.exception))

        class Tuned(self.Executing):
            def __init__(self, *args):
                super().__init__(*args)
                self.kernel = Kernel(ratio=0, size=64 * MIB)

        with self.assertRaises(SystemExit) as refused:
            self.execute(['full', 'full_wb'], cls=Tuned)
        self.assertIn('vm.dirty_background_bytes is not 0', str(refused.exception))

    def test_the_probackup_preflight_hook_is_the_blocks_check(self):
        class Unprovisioned(self.Executing):
            check_probackup = run_exp04.Orchestrator.check_probackup  # dest reports no pg_probackup

        with self.assertRaises(SystemExit) as refused:
            self.execute(['full', 'probackup8'], cls=Unprovisioned)
        self.assertIn('pg_probackup (/usr/local/bin/pg_probackup-17) is not installed on dest', str(refused.exception))

    def test_a_run_without_t2_arms_does_none_of_it(self):
        orchestrator, code, run = self.execute(['full', 'pgdelta_direct_inplace'], ['--manifest-checksums', 'SHA256'])
        self.assertEqual(code, 0)
        self.assertFalse(run['t2'])
        self.assertNotIn('t2_preflight', run)
        self.assertNotIn(('sysctl', 'get'), [event[1][:2] for event in orchestrator.events
                                             if isinstance(event, tuple) and event[0] == 'local_agent'])
        self.assertNotIn('idle_noise_bytes', run)


# --- the CRC32C guard and the new options -----------------------------------------------

class Guards(unittest.TestCase):
    base = ['--source-ip', 'x', '--out', 'y', '--speeds', '2,unthrottled']

    def parse(self, *args):
        return run_exp04.parse_args([*self.base, *args])

    def refused(self, *args) -> str:
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), mock.patch('sys.stderr', stderr), self.assertRaises(SystemExit):
            self.parse(*args)
        return stderr.getvalue()

    def test_t2_arms_need_crc32c_manifests(self):
        for method in measure.T2_METHODS:
            with self.subTest(method=method):
                self.assertIn('need --manifest-checksums CRC32C', self.refused('--methods', f'full,{method}'))
                self.parse('--methods', f'full,{method}', '--manifest-checksums', 'CRC32C')
        # EXP-04's methods keep their default.
        self.assertEqual(self.parse('--methods', 'full,pgdelta_direct_inplace').manifest_checksums, 'SHA256')
        self.assertIsNone(run_exp04.checksum_problem(['full', 'pgdelta'], 'SHA256'))
        self.assertIn('full_wb', run_exp04.checksum_problem(['full', 'full_wb'], 'SHA256'))

    def test_execute_applies_the_guard_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = run_exp04.parse_args(['--source-ip', 'x', '--out', f'{tmp}/out', '--work-dir', f'{tmp}/work',
                                         '--methods', 'full,full_syncfs', '--manifest-checksums', 'CRC32C'])
            args.manifest_checksums = 'SHA256'
            orchestrator = run_exp04.Orchestrator(args)
            self.addCleanup(orchestrator.pool.shutdown)
            with self.assertRaises(SystemExit) as refused:
                orchestrator.execute()
            self.assertIn('CRC32C', str(refused.exception))
            self.assertFalse(Path(f'{tmp}/out').exists())

    def test_the_block_options(self):
        crc = ('--manifest-checksums', 'CRC32C')
        args = self.parse('--methods', 'full,full_nosync,probackup8', *crc, '--extension-repeats', '5',
                          '--extension-methods', 'full,probackup8', '--warm-source-repeats', '3',
                          '--warm-source-methods', 'full')
        self.assertEqual((args.extension_repeats, args.extension_methods), (5, 'full,probackup8'))
        self.assertEqual((args.warm_source_repeats, args.warm_source_methods), (3, 'full'))
        defaults = self.parse()
        self.assertEqual((defaults.extension_repeats, defaults.warm_source_repeats), (0, 0))
        self.assertEqual(defaults.pg_probackup, '/usr/local/bin/pg_probackup-17')
        for bad, message in (
                (('--extension-repeats', '5'), 'go together'),
                (('--extension-methods', 'full'), 'go together'),
                (('--extension-repeats', '5', '--extension-methods', 'full,pgdelta'), 'from --methods'),
                (('--warm-source-repeats', '2', '--warm-source-methods', 'full,full'), 'twice'),
                (('--warm-source-repeats', '-1', '--warm-source-methods', 'full'), '0 or more')):
            with self.subTest(bad=bad):
                self.assertIn(message, self.refused('--methods', 'full,full_nosync', *crc, *bad))


# --- timestamped lines and the phase parsers ------------------------------------------------

class StampedLines(unittest.TestCase):
    def test_stamp_and_parse(self):
        self.assertEqual(measure.stamp_line(1.5, b'pg_basebackup: checkpoint completed\n'),
                         b'[+1.500000] pg_basebackup: checkpoint completed\n')
        self.assertEqual(measure.stamp_line(0.25, b'no newline'), b'[+0.250000] no newline\n')
        text = BASEBACKUP_LOG + 'an unstamped line\n'
        lines = measure.parse_stamped_lines(text)
        self.assertEqual(len(lines), 10)
        self.assertEqual(lines[1], (0.09487, 'pg_basebackup: checkpoint completed'))

    def test_child_stamps_each_line_as_it_arrives(self):
        script = ("import sys, time\nprint('first', flush=True)\ntime.sleep(0.3)\n"
                  "print('second', file=sys.stderr, flush=True)\n")
        with tempfile.TemporaryDirectory() as tmp:
            from test_exp04_network import stub_gnu_time
            child = run_exp04.Child([sys.executable, '-c', script], Path(tmp) / 'logs' / 'x.log',
                                    gnu_time=stub_gnu_time(tmp), stamp_lines=True)
            result = child.wait(60)
            lines = measure.parse_stamped_lines(child.log_text())
            self.assertEqual(result['exit_code'], 0)
            self.assertTrue(result['stamped_lines'])
            self.assertEqual(result['peak_rss_kb'], 4321)  # GNU time's report goes to its own file
            self.assertNotIn('EXP04-TIME', child.log_text())
        self.assertEqual([text for _, text in lines], ['first', 'second'])
        self.assertGreaterEqual(lines[1][0] - lines[0][0], 0.25)
        self.assertLessEqual(lines[1][0], result['wall_seconds'])
        # Without stamp_lines the log is the output as it was.
        with tempfile.TemporaryDirectory() as tmp:
            child = run_exp04.Child([sys.executable, '-c', "print('plain')"], Path(tmp) / 'y.log')
            self.assertFalse(child.wait(60)['stamped_lines'])
            self.assertEqual(child.log_text(), 'plain\n')

    def test_basebackup_phases_and_the_adder(self):
        lines = measure.parse_stamped_lines(BASEBACKUP_LOG)
        phases = measure.basebackup_phases(lines, wall_seconds=3.06)
        self.assertEqual((phases['start_s'], phases['checkpoint_s'], phases['stream_s'], phases['wal_end_s'],
                          phases['sync_s'], phases['manifest_s'], phases['exit_s']),
                         (0.004512, 0.090358, 1.606363, 0.050778, 1.299211, 0.001648, 0.00713))
        # The adder: the checkpoint wait, plus from the WAL end point to the start of the sync.
        self.assertEqual(phases['backup_mode_adder_s'], round(0.090358 + 0.050778, 6))
        self.assertTrue(phases['synced'] and phases['complete'])
        self.assertAlmostEqual(sum(phases[key] for key in ('start_s', 'checkpoint_s', 'stream_s', 'wal_end_s',
                                                           'sync_s', 'manifest_s', 'exit_s')), 3.06, places=6)

    def test_basebackup_phases_without_a_sync(self):
        phases = measure.basebackup_phases(measure.parse_stamped_lines(BASEBACKUP_NOSYNC_LOG))
        self.assertIsNone(phases['sync_s'])
        self.assertFalse(phases['synced'])
        self.assertTrue(phases['complete'])
        # The WAL end runs to the manifest's rename when there is no sync.
        self.assertEqual(phases['wal_end_s'], round(1.7023 - 1.6554, 6))
        self.assertEqual(phases['backup_mode_adder_s'], round(0.0771 + 0.0469, 6))
        self.assertIsNone(phases['exit_s'])

    def test_missing_lines_leave_their_intervals_empty(self):
        cut = '\n'.join(BASEBACKUP_LOG.splitlines()[:3]) + '\n'  # it failed after the start point
        phases = measure.basebackup_phases(measure.parse_stamped_lines(cut), wall_seconds=0.5)
        self.assertEqual(phases['checkpoint_s'], 0.090358)
        for key in ('stream_s', 'wal_end_s', 'sync_s', 'manifest_s', 'exit_s', 'backup_mode_adder_s'):
            self.assertIsNone(phases[key], key)
        self.assertFalse(phases['complete'])
        self.assertIsNone(measure.basebackup_phases([])['start_s'])

    def test_probackup_phases(self):
        phases = measure.probackup_phases(measure.parse_stamped_lines(PROBACKUP_LOG), wall_seconds=3.72)
        self.assertEqual((phases['start_s'], phases['copy_s'], phases['stop_s'], phases['files_sync_s'],
                          phases['exit_s']), (0.512, 2.4, 0.098, 0.695, 0.015))
        self.assertTrue(phases['complete'])
        self.assertIsNone(measure.probackup_phases(measure.parse_stamped_lines(PROBACKUP_LOG[:200]))['copy_s'])


# --- counters, durability and the CSV -------------------------------------------------------

class Durability(unittest.TestCase):
    def test_meminfo_and_diskstats(self):
        values = measure.parse_meminfo(MEMINFO)
        self.assertEqual((values['Dirty'], values['Writeback'], values['Cached']), (1228, 16, 3401236))
        self.assertEqual(values['HugePages_Total'], 0)
        disks = measure.parse_diskstats(DISKSTATS)
        self.assertEqual(disks['md127']['sectors_written'], 16384000)
        self.assertEqual(disks['sda']['reads'], 12345)
        with self.assertRaises(ValueError):
            measure.parse_meminfo('')
        with self.assertRaises(ValueError):
            measure.parse_diskstats('8 0 sda 1 2\n')

    def test_snapshots_carry_dirty_writeback_and_whole_disks(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / 'net').mkdir()
            (proc / 'net' / 'dev').write_text('Inter-|\n face |\n  ens4: ' + ' '.join(['1'] * 16) + '\n')
            (proc / 'stat').write_text('cpu  1 2 3 4 5 6 7 8 0 0\n')
            older = measure.snapshot('ens4', proc)
            self.assertNotIn('meminfo', older)
            (proc / 'meminfo').write_text(MEMINFO)
            (proc / 'diskstats').write_text(DISKSTATS)
            value = measure.snapshot('ens4', proc)
        self.assertEqual(value['meminfo'], {'Dirty': 1228, 'Writeback': 16, 'Cached': 3401236})
        self.assertEqual(sorted(value['disks']), ['md127', 'nvme0n1', 'sda'])
        self.assertEqual(value['disks']['md127'], {'reads': 16000, 'sectors_read': 4096000, 'writes': 32000,
                                                   'sectors_written': 16384000})

    def test_snapshot_delta_of_memory_and_disks(self):
        before = snap(0, 0, (0,) * 10)
        after = snap(0, 0, (0,) * 10, monotonic=1.0)
        self.assertNotIn('memory', measure.snapshot_delta(before, after))
        before.update(meminfo={'Dirty': 1000, 'Writeback': 24}, disks={'md127': {'reads': 1, 'sectors_read': 8,
                                                                                 'writes': 2, 'sectors_written': 16}})
        after.update(meminfo={'Dirty': 3000, 'Writeback': 48}, disks={'md127': {'reads': 3, 'sectors_read': 8,
                                                                                'writes': 10, 'sectors_written': 4112}})
        delta = measure.snapshot_delta(before, after)
        self.assertEqual(delta['memory']['dirty_writeback_bytes_before'], 1024 * 1024)
        self.assertEqual(delta['memory']['dirty_writeback_increase_bytes'], 2024 * 1024)
        self.assertEqual(delta['disk'], {'md127': {'read_bytes': 0, 'write_bytes': 4096 * 512, 'reads': 2,
                                                   'writes': 8}})

    @staticmethod
    def record(method, increase, written, before=1 << 20):
        return {'method': method, 'phases': {'transfer': {
            'memory': {'dest': {'dirty_writeback_bytes_before': before,
                                'dirty_writeback_bytes_after': before + increase}},
            'disk': {'dest': {'md127': {'write_bytes': written}, 'nvme0n1': {'write_bytes': written // 4}}}}}}

    def test_check_a_allows_4_mib_or_the_idle_noise(self):
        output = 2_100_000_000
        passed = measure.durability_check(self.record('full', 4 * MIB, output), output)
        self.assertEqual((passed['a_ok'], passed['b_ok'], passed['ok'], passed['durable']), (True, True, True, True))
        self.assertEqual(passed['limit_bytes'], 4 * MIB)
        # One unflushed 16 MiB WAL segment fails it; a byte over the limit fails it.
        self.assertFalse(measure.durability_check(self.record('full', 16 * MIB, output), output)['ok'])
        self.assertFalse(measure.durability_check(self.record('full', 4 * MIB + 1, output), output)['a_ok'])
        # A larger idle noise raises the limit; a smaller one leaves 4 MiB.
        noisy = measure.durability_check(self.record('full', 6 * MIB, output), output, idle_noise=8 * MIB)
        self.assertEqual((noisy['a_ok'], noisy['limit_bytes'], noisy['idle_noise_bytes']), (True, 8 * MIB, 8 * MIB))
        quiet = measure.durability_check(self.record('full', 0, output), output, idle_noise=MIB)
        self.assertEqual(quiet['limit_bytes'], 4 * MIB)
        # The negative control: full_nosync leaves most of its copy dirty.
        control = measure.durability_check(self.record('full_nosync', 1_900_000_000, 0), output)
        self.assertEqual((control['durable'], control['a_ok'], control['b_ok'], control['ok']), (False, False, False,
                                                                                                  False))

    def test_check_b_needs_the_output_on_the_array_inside_the_window(self):
        output = 2_100_000_000
        enough = measure.durability_check(self.record('parcopy8', 0, int(0.98 * output) + 1), output)
        self.assertEqual((enough['b_required'], enough['b_ok'], enough['b_min_bytes']), (True, True, 2_058_000_000))
        short = measure.durability_check(self.record('probackup8', 0, int(0.97 * output)), output)
        self.assertEqual((short['b_ok'], short['ok']), (False, False))
        # v1 patches B in place (about 30 MB): only check (a) applies.
        v1 = measure.durability_check(self.record('pgdelta_direct_inplace', MIB, 30_000_000), output)
        self.assertEqual((v1['b_required'], v1['b_ok'], v1['ok']), (False, None, True))
        # Without the counters (an older agent) nothing is claimed.
        unknown = measure.durability_check({'method': 'full', 'phases': {'transfer': {}}}, output)
        self.assertEqual((unknown['a_ok'], unknown['b_ok'], unknown['ok']), (None, None, None))
        self.assertIsNone(measure.durability_check({'method': 'full'}, None)['ok'])
        # Only the md array counts as dest's data disk.
        self.assertEqual(measure.phase_disk_bytes(self.record('full', 0, 1000), 'dest', 'write_bytes'), 1000)

    def test_link_time_and_the_csv_columns(self):
        record = {'method': 'parcopy8', 'speed': UNTHROTTLED, 'block': 'extension', 'source_cache': 'cold',
                  'link': {'source_to_dest_gbit': 15.0, 'source_to_dest_8streams_gbit': 15.6},
                  'totals': {'wall_seconds': 2.0, 'wire_bytes_source_to_dest': 2_145_000_000},
                  'phases': {'receiver_transfer': {'steps': {'receiver_start': {'wall_seconds': 0.3},
                                                             'transfer': {'wall_seconds': 1.7}}}},
                  'durability': {'durable': True, 'ok': True, 'dirty_writeback_increase_bytes': 1_500_000},
                  'tool_reported': {'parcopy_summary': measure.parcopy_summary(PARCOPY_STATS)},
                  'sysctl': {'before': {'vm.dirty_background_ratio': 10, 'vm.dirty_background_bytes': 0}},
                  'verification': {'output_start': {'ready_seconds': 1.25}, 'void': None}}
        row = measure.summary_row(record)
        self.assertEqual(set(row), set(measure.CSV_FIELDS))
        self.assertEqual((row['tier'], row['block'], row['source_cache']), ('bound', 'extension', 'cold'))
        self.assertEqual((row['durable'], row['durability_ok'], row['dirty_increase_mb']), (True, True, 1.5))
        self.assertEqual(row['measured_s2d_gbit_8streams'], 15.6)
        self.assertEqual(row['link_time_s'], round(2_145_000_000 * 8 / 15.6e9, 3))  # the 8-stream rate
        self.assertEqual(row['above_link_s'], round(2.0 - row['link_time_s'], 3))
        self.assertEqual(row['usable_s'], 3.25)
        self.assertEqual((row['sysctl_dirty_background_ratio'], row['sysctl_dirty_background_bytes']), (10, 0))
        self.assertEqual((row['parcopy_streams'], row['parcopy_balance'], row['parcopy_fsync_calls']), (2, 1.0436, 12))
        single = measure.summary_row({**record, 'method': 'full_syncfs'})
        self.assertEqual(single['link_time_s'], round(2_145_000_000 * 8 / 15e9, 3))  # the single stream's
        self.assertIsNone(single['probackup_sync_s'])
        self.assertIsNone(measure.summary_row({'method': 'full'})['link_time_s'])


PARCOPY_STATS = {'streams': [{'bytes': 1_100_000_000, 'first_byte_s': 0.08, 'last_byte_s': 1.71, 'write_s': 1.2},
                             {'bytes': 1_008_000_000, 'first_byte_s': 0.07, 'last_byte_s': 1.69, 'write_s': 1.1}],
                 'fsync_s': 0.21, 'fsync_calls': 12, 'fsync_expected': 12, 'listening_s': 0.05}


class ParcopySummary(unittest.TestCase):
    def test_summary_of_the_receivers_stats(self):
        summary = measure.parcopy_summary(PARCOPY_STATS)
        self.assertEqual(summary, {'streams': 2, 'bytes': 2_108_000_000, 'max_stream_bytes': 1_100_000_000,
                                   'balance': round(1_100_000_000 / 1_054_000_000, 4), 'listening_s': 0.05,
                                   'first_byte_s': 0.07,
                                   'last_byte_s': 1.71, 'write_s': 1.2, 'fsync_s': 0.21, 'fsync_calls': 12,
                                   'fsync_expected': 12, 'fsync_ok': True})
        self.assertFalse(measure.parcopy_summary({**PARCOPY_STATS, 'fsync_calls': 11})['fsync_ok'])
        empty = measure.parcopy_summary(None)
        self.assertEqual((empty['streams'], empty['balance'], empty['fsync_ok']), (None, None, None))

    def test_tool_reported_reads_both_ends(self):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = T2Stub(tmp, FakeClock())
            self.addCleanup(orchestrator.pool.shutdown)
            paths = orchestrator.method_paths('parcopy8')
            Path(paths['receive_stats']).parent.mkdir(parents=True)
            Path(paths['receive_stats']).write_text(json.dumps(PARCOPY_STATS))
            sent = {'streams': [{'bytes': 1_100_000_000, 'sendfile_s': 1.5}]}
            orchestrator.source.agent = lambda *args, **kwargs: {'exit_code': 0, 'log_tail': json.dumps(sent)}
            reported = run_exp04.Orchestrator.tool_reported(orchestrator, 'parcopy8', paths, {})
        self.assertEqual(reported['parcopy_summary']['fsync_calls'], 12)
        self.assertEqual(reported['bytes_source_to_dest'], 2_108_000_000)
        self.assertEqual(reported['send_stats'], sent)
        self.assertEqual(reported['flags']['send'], list(measure.parcopy_args('parcopy8')['send']))


# --- the runs: one counter window, the flush inside it, the sysctl reset ----------------------

class Runs(Clocked):
    def test_each_pg_basebackup_arm_is_one_window_with_its_flush_inside(self):
        for method in measure.FULL_VARIANTS:
            with self.subTest(method=method):
                orchestrator = self.orchestrator()
                orchestrator.window_writes = 2_200_000_000
                record = self.run_method(orchestrator, method)
                self.assertEqual(record['status'], 'pass', record.get('traceback'))
                self.assertEqual(list(record['phases']), ['transfer'])
                row = measure.summary_row(record)
                self.assertEqual(row['counter_windows'], 1)
                [command] = record['phases']['transfer']['commands']
                self.assertEqual(Path(command['argv'][0]).name, 'pg_basebackup')
                self.assertEqual(command['argv'][-len(measure.FULL_VARIANTS[method]):],
                                 list(measure.FULL_VARIANTS[method]))
                # Its lines were stamped, untranslated, and split into its phases.
                self.assertTrue(command['stamped_lines'])
                self.assertEqual(command['env']['LC_MESSAGES'], 'C')
                self.assertEqual(row['pbb_checkpoint_s'], 0.090358 if method != 'full_nosync' else 0.0771)
                self.assertEqual(row['pbb_sync_s'], None if method == 'full_nosync' else 1.299211)
                self.assertIsNotNone(row['backup_mode_adder_s'])
                self.assertEqual((row['tier'], row['block'], row['source_cache']),
                                 (measure.TIERS[method], 'main', 'cold'))
                kinds = self.kinds(orchestrator)
                self.assertEqual(kinds.count('drop_caches'), 1)
                self.assertLess(kinds.index('drop_caches'), kinds.index(('dest', 'pg_basebackup')))

    def test_v1_at_8_jobs_runs_in_one_window_after_a_clean_stop(self):
        orchestrator = self.orchestrator()
        record = self.run_method(orchestrator, 'pgdelta_direct_inplace_j8')
        self.assertEqual(record['status'], 'pass', record.get('traceback'))
        self.assertEqual(list(record['phases']), ['receiver_transfer'])
        receive = FakeChild.started[0].argv
        self.assertEqual(receive[receive.index('--jobs') + 1], '8')
        self.assertIn('--in-place', receive)
        kinds = self.kinds(orchestrator)
        self.assertLess(kinds.index(('source_script', 'stop-clean')), kinds.index('drop_caches'))
        self.assertIn(('source_script', 'start'), kinds)
        self.assertEqual(measure.summary_row(record)['tier'], 'v1_8_jobs')

    def test_the_durability_checks_land_in_each_record(self):
        orchestrator = self.orchestrator()
        orchestrator.window_writes = 2_200_000_000  # each window's writes to the md array
        record = self.run_method(orchestrator, 'full')
        self.assertEqual(record['durability']['ok'], True, record['durability'])
        self.assertEqual(record['durability']['disk_write_bytes'], 2_200_000_000 // 512 * 512)
        self.assertEqual(record['durability']['output_bytes'], 189_244_280)
        self.assertEqual(record['phases']['transfer']['memory']['dest']['dirty_writeback_increase_bytes'], 0)
        orchestrator.dirty_kb = 1000 + 1_700_000  # about 1.7 GB left dirty, as --no-sync leaves it
        control = self.run_method(orchestrator, 'full_nosync')
        self.assertEqual(control['status'], 'pass')  # its status is its verification's
        row = measure.summary_row(control)
        self.assertEqual((row['durable'], row['durability_ok']), (False, False))
        self.assertEqual(row['dirty_increase_mb'], round(1_700_000 * 1024 / 1e6, 3))
        self.assertEqual(row['disk_write_dest_bytes'], 2_200_000_000 // 512 * 512)

    def test_probackup_is_dispatched_to_its_block(self):
        # parcopy's runs are in tests/test_parcopy.py.
        orchestrator = self.orchestrator()
        record = self.run_method(orchestrator, 'probackup8')
        self.assertEqual(list(record['phases']), [measure.PROBACKUP_PHASE])
        # A live-source arm: the source is neither stopped nor restarted.
        self.assertNotIn('source_script', [kind[0] for kind in self.kinds(orchestrator)])

    def test_a_probackup_run_is_one_window_with_its_sync_f_inside(self):
        class Probackup(T2Stub):
            # The pg_probackup block's run_probackup, its catchup's log what 2.5.16 prints.
            def local_command(self, argv, log_name, *, timeout=None, cwd=None, env=None, stamp_lines=False):
                result = super().local_command(argv, log_name, timeout=timeout, cwd=cwd, env=env,
                                               stamp_lines=stamp_lines)
                if Path(argv[0]).name.startswith('pg_probackup'):
                    log = self.dest_log(log_name)
                    log.parent.mkdir(parents=True, exist_ok=True)
                    log.write_text(PROBACKUP_LOG)
                    result['log'] = str(log)
                return result

            def verify_probackup(self, method, paths):
                return {'verified': True, 'basis_changes': [], 'output_bytes': 2_100_000_000}

        orchestrator = self.orchestrator(Probackup)
        record = self.run_method(orchestrator, 'probackup8')
        self.assertEqual(record['status'], 'pass', record.get('traceback'))
        row = measure.summary_row(record)
        self.assertEqual(row['counter_windows'], 1)
        self.assertEqual((row['probackup_sync_s'], row['total_s']), (0.5, 1.5))
        self.assertEqual((row['probackup_copy_s'], row['probackup_files_sync_s']), (2.4, 0.695))
        commands = record['phases']['catchup_sync']['commands']
        self.assertEqual([command['argv'][:2] for command in commands],
                         [['/usr/local/bin/pg_probackup-17', 'catchup'], ['sync', '-f']])

    def test_full_wb_sets_and_resets_dests_write_back(self):
        orchestrator = self.orchestrator()
        record = self.run_method(orchestrator, 'full_wb')
        self.assertEqual(record['status'], 'pass', record.get('traceback'))
        kinds = self.kinds(orchestrator)
        sysctl = [event[1] for event in orchestrator.events if isinstance(event, tuple) and event[0] == 'local_agent'
                  and event[1][0] == 'sysctl']
        self.assertEqual([call[:2] for call in sysctl],
                         [('sysctl', 'get'), ('sysctl', 'set'), ('sysctl', 'set'), ('sysctl', 'get')])
        # Set before the cache drop; reset by writing the ratio, never 0 to the bytes setting.
        self.assertEqual(sysctl[1][2:], ('vm.dirty_background_bytes=67108864',))
        self.assertEqual(sysctl[2][2:], ('vm.dirty_background_ratio=10',))
        self.assertLess([index for index, kind in enumerate(kinds) if kind == ('local_agent', 'sysctl')][1],
                        kinds.index('drop_caches'))
        self.assertEqual(orchestrator.kernel.values, {'vm.dirty_background_ratio': 10, 'vm.dirty_background_bytes': 0})
        self.assertNotIn(('vm.dirty_background_bytes', 0), orchestrator.kernel.writes)
        row = measure.summary_row(record)
        self.assertEqual((row['sysctl_dirty_background_bytes'], row['sysctl_dirty_background_ratio']), (64 * MIB, 0))
        self.assertEqual(record['sysctl']['reset']['values'],
                         {'vm.dirty_background_ratio': 10, 'vm.dirty_background_bytes': 0})
        # Every other run records the defaults it found.
        other = measure.summary_row(self.run_method(orchestrator, 'full'))
        self.assertEqual((other['sysctl_dirty_background_bytes'], other['sysctl_dirty_background_ratio']), (0, 10))

    def test_the_reset_follows_an_exception_and_the_guard_then_passes(self):
        class Failing(T2Stub):
            def run_full(self, method, paths):
                if method == 'full_wb':
                    raise run_exp04.CommandError('the copy failed midway')
                return super().run_full(method, paths)

        orchestrator = self.orchestrator(Failing)
        record = self.run_method(orchestrator, 'full_wb')
        self.assertEqual(record['status'], 'error')
        self.assertIn('the copy failed midway', record['error'])
        self.assertEqual(orchestrator.kernel.values[measure.DIRTY_BACKGROUND_RATIO], 10)
        self.assertEqual(orchestrator.kernel.values[measure.DIRTY_BACKGROUND_BYTES], 0)
        self.assertEqual(orchestrator.check_dirty_writeback(),
                         {'vm.dirty_background_ratio': 10, 'vm.dirty_background_bytes': 0})
        # The next run's guard passes.
        self.assertEqual(self.run_method(orchestrator, 'full')['status'], 'pass')

    def test_a_reset_that_does_not_take_fails_the_run_and_the_guard_stops_the_next(self):
        class Stuck(T2Stub):
            def local_agent(self, *args, timeout=600):
                if args[:2] == ('sysctl', 'set') and args[2].startswith(measure.DIRTY_BACKGROUND_RATIO):
                    self.events.append(('local_agent', args))
                    return {'set': {}, 'values': dict(self.kernel.values)}  # it did not take
                return super().local_agent(*args, timeout=timeout)

        orchestrator = self.orchestrator(Stuck)
        record = self.run_method(orchestrator, 'full_wb')
        self.assertEqual(record['status'], 'error')
        self.assertIn('could not be reset', record['error'])
        later = self.run_method(orchestrator, 'full')
        self.assertEqual(later['status'], 'error')
        self.assertIn('not at their defaults', later['error'])
        self.assertNotIn('drop_caches', self.kinds(orchestrator)[-3:])

    def test_full_outputs_are_started_after_their_bytes_are_counted(self):
        class Verifying(T2Stub):
            verify = run_exp04.Orchestrator.verify

            def check_output(self, method, data, *, recovery, query=run_exp04.OUTPUT_QUERY):
                self.events.append(('check_output', recovery, query))
                return {'ok': self.start_ok, 'ready_seconds': 1.4}

            start_ok = True

        orchestrator = self.orchestrator(Verifying)
        orchestrator.b0_stat = {}
        output = orchestrator.outputs / 'full_syncfs'
        (output / 'pg_wal').mkdir(parents=True)
        (output / 'base').mkdir()
        (output / 'base' / '1').write_bytes(b'x' * 100)
        with mock.patch.object(run_exp04.measure, 'tree_stat', return_value={}):
            result = orchestrator.verify('full_syncfs', orchestrator.method_paths('full_syncfs'), {})
            self.assertEqual(result['output_bytes'], 100)
            self.assertTrue(result['verified'])
            self.assertIn(('check_output', True, run_exp04.OUTPUT_QUERY), orchestrator.events)
            kinds = self.kinds(orchestrator)
            self.assertLess(kinds.index(('dest', 'pg_verifybackup')), kinds.index('check_output'))
            # An output that doesn't start fails its verification.
            (orchestrator.outputs / 'full').mkdir()
            orchestrator.start_ok = False
            self.assertFalse(orchestrator.verify('full', orchestrator.method_paths('full'), {})['verified'])
        # incremental's output is not started.
        events = len(orchestrator.events)
        (orchestrator.outputs / 'incremental').mkdir()
        with mock.patch.object(run_exp04.measure, 'tree_stat', return_value={}):
            orchestrator.verify('incremental', orchestrator.method_paths('incremental'), {})
        self.assertNotIn('check_output', [kind for kind in self.kinds(orchestrator)[events:]])

    def test_parcopy_is_verified_as_v1_and_probackup_by_its_block(self):
        orchestrator = self.orchestrator()
        orchestrator.verify = run_exp04.Orchestrator.verify.__get__(orchestrator)
        with mock.patch.object(orchestrator, 'verify_direct', return_value={'verified': True}) as direct:
            orchestrator.verify('parcopy8', orchestrator.method_paths('parcopy8'), {})
        direct.assert_called_once()
        with mock.patch.object(orchestrator, 'verify_probackup', return_value={'verified': True}) as probackup:
            orchestrator.verify('probackup4', orchestrator.method_paths('probackup4'), {})
        probackup.assert_called_once()

    def test_the_warm_source_block_reads_the_source_instead_of_dropping_it(self):
        class Warm(T2Stub):
            def drop_caches(self, source=True):
                self.events.append('drop_caches' if source else 'drop_dest_caches')
                return {'source': None if not source else {}, 'dest': {}}

        for method in ('full', 'pgdelta_direct_inplace'):
            with self.subTest(method=method):
                orchestrator = self.orchestrator(Warm)
                record = self.run_method(orchestrator, method, link={**LINK, 'block': 'warm_source',
                                                                     'source_cache': 'warm'}, speed=UNTHROTTLED)
                self.assertEqual(record['status'], 'pass', record.get('traceback'))
                kinds = self.kinds(orchestrator)
                self.assertNotIn('drop_caches', kinds)
                warm = kinds.index(('source_agent', 'read-test'))
                self.assertLess(warm, kinds.index('drop_dest_caches'))
                read = [event[1] for event in orchestrator.events if isinstance(event, tuple)
                        and event[0] == 'source_agent' and event[1][0] == 'read-test'][0]
                self.assertEqual(read, ('read-test', orchestrator.source_data, '--readers', '4', '--exclude', 'pg_wal',
                                        '--sync'))
                if method == 'pgdelta_direct_inplace':  # stopped first, then read
                    self.assertLess(kinds.index(('source_script', 'stop-clean')), warm)
                row = measure.summary_row(record)
                self.assertEqual((row['block'], row['source_cache'], row['caches_dropped']),
                                 ('warm_source', 'warm', 'dest'))


# --- the trial: its order, the blocks, reference digests and probes ---------------------------

class Trials(Clocked):
    class Ordered(TrialStub):
        """run_trial with the T2 steps recorded as events."""

        def __init__(self, tmp, clock, extra_args=()):
            super().__init__(tmp, clock, ['--manifest-checksums', 'CRC32C', *extra_args])
            self.end_digest = SOURCE_DIGEST
            self.digests = 0

        def _source_agent(self, *args, timeout=600):
            self.events.append(('source_agent', args))
            if args[0] == 'datadir-digest':
                self.digests += 1
                return {'digest': SOURCE_DIGEST if self.digests == 1 else self.end_digest, 'read_from': {}}
            return {'exit_code': 0, 'log_tail': ''}

        def set_speed(self, speed):
            self.events.append(('link', speed['label']))
            return {'speed': speed, 'source_to_dest_gbit': 1.0, 'dest_to_source_gbit': 1.0}

        def probe_relation(self):
            self.events.append('probe_relation')
            return {'path': 'base/5/16397'}

        def durable_write_floor(self):
            self.events.append('durable_write_floor')
            return {'seconds': 1.4}

        def read_floor(self):
            self.events.append('read_floor')
            return {'seconds': 0.7}

        def single_read_probe(self):
            self.events.append('single_read')
            return {'seconds': 0.6}

        def bracket_probe(self):
            self.events.append('bracket')
            return {'median_pair_s': 0.08}

        def finish_logical_checks(self, trial):
            self.events.append('finish_logical_checks')
            return None

        def run_method(self, trial, repeat, speed, link, method, order_index, order):
            self.events.append(('run', link['block'], repeat, speed['label'], method, link['source_cache']))
            record = {'status': 'pass', 'method': method}
            self.records.append(record)
            return record

    def trial(self, methods, speeds='2,unthrottled', extra=(), cls=None):
        orchestrator = self.orchestrator(cls or self.Ordered, ['--repeats', '2', '--methods', ','.join(methods),
                                                               *extra])
        orchestrator.speeds = measure.parse_speeds(speeds)
        orchestrator.methods = methods
        with mock.patch.object(run_exp04.measure, 'tree_digest',
                               side_effect=lambda root: orchestrator.events.append('b0_digest')
                               or {'entries': {}, 'device': 1}):
            trial = orchestrator.run_trial(0, 'hot', 0.01)
        return orchestrator, trial

    def steps(self, orchestrator):
        steps = []
        for kind in self.kinds(orchestrator):
            if isinstance(kind, tuple) and kind[0] == 'run':
                continue
            if kind in ('take_b0', 'probe_relation', 'durable_write_floor', 'read_floor', 'single_read', 'bracket',
                        'finish_logical_checks', 'b0_digest', 'cleanup_trial'):
                steps.append(kind)
            elif isinstance(kind, tuple) and kind[0] in ('source_script', 'source_agent') \
                    and kind[1] in ('setup', 'workload', 'stop-clean', 'datadir-digest', 'start', 'verify'):
                steps.append(kind[1])
        return steps

    def test_the_end_of_trial_order(self):
        methods = ['full', 'full_nosync', 'probackup8', 'pgdelta_direct_inplace']
        orchestrator, trial = self.trial(methods, extra=[
            '--extension-repeats', '2', '--extension-methods', 'full,probackup8,pgdelta_direct_inplace',
            '--warm-source-repeats', '1', '--warm-source-methods', 'full,pgdelta_direct_inplace'])
        self.assertEqual(trial['status'], 'pass', trial.get('traceback'))
        reference = ['stop-clean', 'datadir-digest', 'start']
        self.assertEqual(self.steps(orchestrator),
                         ['setup', 'take_b0', 'workload', 'probe_relation', *reference,
                          'durable_write_floor', 'read_floor', 'single_read', 'bracket',
                          'durable_write_floor', *reference, 'finish_logical_checks', 'verify', 'b0_digest',
                          'cleanup_trial'])
        events = orchestrator.events
        runs = [event for event in events if isinstance(event, tuple) and event[0] == 'run']
        # The main block, then the extension, then the warm-source block, all before the end steps.
        blocks = [run[1] for run in runs]
        self.assertEqual(blocks, ['main'] * 16 + ['extension'] * 6 + ['warm_source'] * 2)
        last_run = max(index for index, event in enumerate(events) if isinstance(event, tuple) and event[0] == 'run')
        bracket = events.index('bracket')
        first_run = min(index for index, event in enumerate(events) if isinstance(event, tuple) and event[0] == 'run')
        self.assertLess(bracket, first_run)
        self.assertLess(last_run, len(events) - events[::-1].index('durable_write_floor') - 1)
        # The extension and the warm-source block run unthrottled, their repeats numbered on.
        self.assertEqual({(run[2], run[3]) for run in runs if run[1] == 'extension'},
                         {(2, 'unthrottled'), (3, 'unthrottled')})
        self.assertEqual({(run[2], run[3], run[5]) for run in runs if run[1] == 'warm_source'},
                         {(4, 'unthrottled', 'warm')})
        self.assertEqual({run[5] for run in runs if run[1] != 'warm_source'}, {'cold'})
        for repeat in (2, 3):
            self.assertEqual(sorted(run[4] for run in runs if run[2] == repeat),
                             ['full', 'pgdelta_direct_inplace', 'probackup8'])
        self.assertEqual(sum(1 for run in trial['runs'] if run['block'] == 'extension'), 6)
        # The reference digests matched, leaving out what a clean stop rewrites.
        self.assertTrue(trial['reference_match'])
        self.assertEqual(trial['reference_digests']['start']['excluded'],
                         ['global/pg_control', 'pg_stat/pgstat.stat', 'pg_wal/000000010000000000000003'])
        self.assertEqual(trial['reference_digests']['start']['summary']['files'], 2)
        self.assertEqual(sorted(trial['probes']), ['bracket', 'durable_write_floor', 'read_floor', 'single_read'])
        self.assertEqual(len(trial['probes']['durable_write_floor']), 2)

    def test_a_source_that_changed_is_recorded(self):
        class Changed(self.Ordered):
            def __init__(self, *args):
                super().__init__(*args)
                entries = dict(SOURCE_DIGEST['entries'])
                entries['base/5/16397'] = ['file', 8, 'f' * 64, 0o600, 1, 1]
                entries['pg_wal/000000010000000000000004'] = ['file', 8, '0' * 64, 0o600, 6, 1]  # left out
                self.end_digest = {**SOURCE_DIGEST, 'entries': entries}

        orchestrator, trial = self.trial(['full', 'full_syncfs'], cls=Changed)
        self.assertEqual(trial['status'], 'pass', trial.get('traceback'))
        self.assertFalse(trial['reference_match'])
        self.assertEqual(len(trial['reference_differences']), 1)
        self.assertIn('base/5/16397', trial['reference_differences'][0])
        self.assertEqual(sorted(orchestrator.reference_digests), ['end', 'start'])

    def test_a_run_without_t2_arms_keeps_exp04s_trial(self):
        orchestrator, trial = self.trial(['full', 'pgdelta_direct_inplace'])
        self.assertEqual(self.steps(orchestrator),
                         ['setup', 'take_b0', 'workload', 'finish_logical_checks', 'verify', 'b0_digest',
                          'cleanup_trial'])
        self.assertNotIn('reference_digests', trial)
        self.assertNotIn('probes', trial)

    def test_a_failed_probe_is_recorded_and_the_trial_goes_on(self):
        class Failing(self.Ordered):
            def read_floor(self):
                raise run_exp04.CommandError('read-test failed')

        orchestrator, trial = self.trial(['full', 'full_nosync'], cls=Failing)
        self.assertEqual(trial['status'], 'pass', trial.get('traceback'))
        self.assertEqual(trial['probes']['read_floor'], {'error': 'CommandError: read-test failed'})

    def test_the_reference_digest_restarts_the_source_whatever_happens(self):
        orchestrator = self.orchestrator(self.Ordered)

        def failing(*args, timeout=600):
            orchestrator.events.append(('source_agent', args))
            raise run_exp04.CommandError('datadir-digest failed')

        orchestrator.source.agent = failing
        with self.assertRaises(run_exp04.CommandError):
            orchestrator.reference_digest('start')
        self.assertEqual([kind[1] for kind in self.kinds(orchestrator) if kind[0] in ('source_script', 'source_agent')],
                         ['stop-clean', 'datadir-digest', 'start'])
        self.assertIsNone(orchestrator.source_down)

        class NoRestart(self.Ordered):
            def _source_script(self, script, *args, timeout=7200):
                if args[0] == 'start':
                    raise run_exp04.CommandError('the source did not start')
                return super()._source_script(script, *args)

        orchestrator = self.orchestrator(NoRestart)
        with self.assertRaises(run_exp04.CommandError) as failed:
            orchestrator.reference_digest('end')
        self.assertIn('could not be restarted', str(failed.exception))
        self.assertIn('did not start', orchestrator.source_down)


class LinkTests(Clocked):
    def test_the_8_stream_test_runs_at_each_speeds_first_visit(self):
        class Linked(T2Stub):
            def throttle(self, rate):
                return {}

            def blast(self, direction, streams=1):
                self.events.append(('blast', direction, streams))
                gbit = 15.5 if streams > 1 else 14.9
                return {'sender': {}, 'receiver': {'gbit': gbit, 'steady_gbit': gbit}}

        orchestrator = self.orchestrator(Linked)
        orchestrator.methods = ['full', 'parcopy8']
        first = orchestrator.set_speed(UNTHROTTLED)
        again = orchestrator.set_speed(UNTHROTTLED)
        blasts = [event[1:] for event in orchestrator.events if event[0] == 'blast']
        self.assertEqual(blasts, [('source_to_dest', 1), ('dest_to_source', 1), ('source_to_dest', 8),
                                  ('source_to_dest', 1), ('dest_to_source', 1)])
        for link in (first, again):
            self.assertEqual(link['source_to_dest_8streams_gbit'], 15.5)
        self.assertTrue(first['multi_stream']['measured_here'])
        self.assertFalse(again['multi_stream']['measured_here'])
        # run_method copies it into the record (a key ending in gbit), so each row has it.
        record = self.run_method(orchestrator, 'full', link=again, speed=UNTHROTTLED)
        self.assertEqual(measure.summary_row(record)['measured_s2d_gbit_8streams'], 15.5)
        # Outside T2 runs there is no 8-stream test.
        orchestrator = self.orchestrator(Linked)
        orchestrator.methods = ['full']
        orchestrator.set_speed(UNTHROTTLED)
        self.assertEqual([event[2] for event in orchestrator.events if event[0] == 'blast'], [1, 1])

    def test_blast_passes_the_streams_to_both_ends(self):
        orchestrator = self.orchestrator()
        receiver = mock.Mock()
        receiver.stdout.readline.return_value = 'LISTENING\n'
        receiver.communicate.return_value = (agent.RESULT_PREFIX + json.dumps({'gbit': 15.5}), '')
        receiver.returncode = 0
        receiver.poll.return_value = 0
        calls = []
        orchestrator.source.agent = lambda *args, timeout=600: calls.append(args) or {'gbit': 15.4}
        with mock.patch.object(run_exp04.subprocess, 'Popen', return_value=receiver) as popen:
            result = orchestrator.blast('source_to_dest', streams=8)
        self.assertEqual(popen.call_args.args[0][-2:], ['--streams', '8'])
        self.assertEqual(calls[0][-2:], ('--streams', '8'))
        self.assertEqual(result['receiver'], {'gbit': 15.5})
        with mock.patch.object(run_exp04.subprocess, 'Popen', return_value=receiver) as popen:
            orchestrator.blast('source_to_dest')
        self.assertNotIn('--streams', popen.call_args.args[0])


# --- the agent's and the source's probes --------------------------------------------------------

class HostProbes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_multi_stream_blast_over_loopback(self):
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        results = {}

        def receive():
            with redirect_stdout(io.StringIO()):
                results['recv'] = agent.cmd_blast_recv(Namespace(listen=f'127.0.0.1:{port}', timeout=30, warmup=0.1,
                                                                 streams=3))

        thread = threading.Thread(target=receive)
        thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                socket.create_connection(('127.0.0.1', port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.02)
        # That probe connection was one of the three: the sender opens the other two.
        sent = agent.cmd_blast_send(Namespace(connect=f'127.0.0.1:{port}', seconds=0.4, streams=2))
        thread.join(30)
        received = results['recv']
        self.assertEqual(received['streams'], 3)
        self.assertEqual(len(received['per_stream']), 3)
        self.assertEqual(received['bytes'], sent['bytes'])
        self.assertEqual(sum(sent['per_stream_bytes']), sent['bytes'])
        self.assertGreater(received['gbit'], 0)

    def test_the_durable_write_test(self):
        target = self.root / 'floor'
        synced = []
        real = os.fsync
        with mock.patch.object(agent.os, 'fsync', side_effect=lambda fd: synced.append(fd) or real(fd)):
            result = agent.cmd_durable_write_test(Namespace(dir=str(target), writers=3, bytes=10 * MIB + 7,
                                                            writeback_bytes=4 * MIB))
        self.assertEqual(result['bytes'], 10 * MIB + 7)
        self.assertEqual([writer['bytes'] for writer in result['per_writer']], [3495256, 3495256, 3495255])
        self.assertEqual(len(synced), 4)  # each file, then the directory
        self.assertEqual(list(target.iterdir()), [])  # removed afterwards
        self.assertGreater(result['seconds'], 0)

    def test_the_read_test_splits_files_and_leaves_out_pg_wal(self):
        data = self.root / 'data'
        for rel, size in (('base/5/16397', 3000), ('base/5/16397.1', 100), ('base/5/2600', 700),
                          ('global/1262', 200), ('pg_wal/000000010000000000000001', 5000), ('PG_VERSION', 3)):
            (data / rel).parent.mkdir(parents=True, exist_ok=True)
            (data / rel).write_bytes(b'x' * size)
        result = agent.cmd_read_test(Namespace(path=str(data), readers=2, exclude=['pg_wal'], sync=False))
        self.assertEqual((result['files'], result['bytes'], result['readers']), (5, 4003, 2))
        self.assertEqual(sorted(reader['bytes'] for reader in result['per_reader']), [1003, 3000])
        self.assertEqual(result['largest_file']['path'], str(data / 'base/5/16397'))
        # One reader for a file, with its further segments.
        single = agent.cmd_read_test(Namespace(path=str(data / 'base/5/16397'), readers=4, exclude=[], sync=False))
        self.assertEqual((single['readers'], single['files'], single['bytes']), (1, 2, 3100))
        self.assertEqual(measure.split_files([(5, 'a'), (3, 'b'), (3, 'c'), (1, 'd')], 2),
                         [[(5, 'a'), (1, 'd')], [(3, 'b'), (3, 'c')]])

    def test_sysctl_get_and_set(self):
        proc = self.root / 'sys'
        (proc / 'vm').mkdir(parents=True)
        (proc / 'vm' / 'dirty_background_ratio').write_text('10\n')
        (proc / 'vm' / 'dirty_background_bytes').write_text('0\n')

        def run(argv, **kwargs):
            name, value = argv[-1].split('=')
            (proc / name.replace('.', '/')).write_text(value + '\n')
            if name.endswith('_bytes'):
                (proc / 'vm' / 'dirty_background_ratio').write_text('0\n')
            return mock.Mock(returncode=0, stderr='')

        with mock.patch.object(agent, 'sysctl_path', lambda name: proc / name.replace('.', '/')), \
                mock.patch.object(agent.subprocess, 'run', side_effect=run) as called:
            got = agent.cmd_sysctl(Namespace(action='get', settings=['vm.dirty_background_ratio',
                                                                     'vm.dirty_background_bytes']))
            self.assertEqual(got['values'], {'vm.dirty_background_ratio': 10, 'vm.dirty_background_bytes': 0})
            done = agent.cmd_sysctl(Namespace(action='set', settings=['vm.dirty_background_bytes=67108864']))
        self.assertEqual(called.call_args.args[0],
                         ['sudo', '-n', 'sysctl', '-q', '-w', 'vm.dirty_background_bytes=67108864'])
        self.assertEqual(done['values'], {'vm.dirty_background_bytes': 67108864})
        with self.assertRaises(SystemExit):
            agent.cmd_sysctl(Namespace(action='set', settings=['vm.dirty_background_bytes']))
        parser = agent.build_parser()
        self.assertEqual(parser.parse_args(['sysctl', 'get', 'vm.dirty_ratio']).settings, ['vm.dirty_ratio'])

    def test_env_records_write_back_and_tool_versions(self):
        texts = {'strace': 'strace -- version 6.1\nCopyright', '/p/pg_probackup-17': 'pg_probackup 2.5.16',
                 '/b/pg_basebackup': 'Usage:\n  --sync-method=METHOD\n'}

        def text_of(argv, **kwargs):
            return texts.get(argv[0])

        with mock.patch.object(agent, 'text_of', side_effect=text_of), \
                mock.patch.object(agent, 'metadata', return_value=None), \
                mock.patch.object(agent, 'read', side_effect=lambda path: '10' if 'dirty' in str(path) else None):
            env = agent.cmd_env(Namespace(iface='ens4', bin_dir='/b', pg_probackup='/p/pg_probackup-17'))
        self.assertEqual(env['vm_dirty']['dirty_background_ratio'], '10')
        self.assertEqual(set(env['vm_dirty']), set(agent.VM_DIRTY))
        self.assertEqual((env['strace'], env['pg_probackup']), ('strace -- version 6.1', 'pg_probackup 2.5.16'))
        self.assertTrue(env['pg_basebackup_sync_method'])
        self.assertEqual(agent.build_parser().parse_args(['env', '--iface', 'x']).pg_probackup, measure.PROBACKUP_EXE)


class BracketProbe(unittest.TestCase):
    OUTPUT = 'Timing is on.\n2/A0000028\nTime: {start} ms\n2/A0000100\nTime: {stop} ms\n'

    def test_the_pair_in_one_session(self):
        argv = source_cluster.backup_mode_argv('/w/src', '/b')
        self.assertEqual(argv[:12], ['/b/psql', '-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-h', '/w/src/s', '-p',
                                     '5432', '-U', 'study'])
        self.assertEqual(argv[-6:], ['-c', '\\timing on', '-c', "SELECT pg_backup_start('probe', true);", '-c',
                                     'SELECT lsn FROM pg_backup_stop(false);'])
        self.assertEqual(measure.psql_timings('Time: 12.5 ms\nTime: 1500.250 ms (00:01.500)\n'), [0.0125, 1.50025])

    def test_each_pair_follows_a_cache_drop(self):
        times = iter([(180.0, 20.0), (40.0, 10.0), (50.0, 12.0)])
        events = []

        def run(argv, **kwargs):
            events.append('psql')
            start, stop = next(times)
            return mock.Mock(returncode=0, stdout=self.OUTPUT.format(start=start, stop=stop), stderr='')

        def drop(args):
            events.append('drop')
            return {'seconds': 0.5}

        with mock.patch.object(source_cluster.subprocess, 'run', side_effect=run), \
                mock.patch.object(source_cluster.agent, 'cmd_drop_caches', side_effect=drop):
            result = source_cluster.cmd_backup_mode_probe(Namespace(root='/w/src', bin_dir='/b', repeats=3))
        self.assertEqual(events, ['drop', 'psql'] * 3)
        self.assertEqual([pair['pair_s'] for pair in result['pairs']], [0.2, 0.05, 0.062])
        self.assertEqual((result['first_pair_s'], result['median_pair_s'], result['median_after_first_s']),
                         (0.2, 0.062, 0.056))
        self.assertEqual(source_cluster.build_parser().parse_args(['backup-mode-probe', '--root', 'r']).repeats, 10)

    def test_a_failed_pair_fails_the_probe(self):
        with mock.patch.object(source_cluster.subprocess, 'run',
                               return_value=mock.Mock(returncode=3, stdout='', stderr='ERROR: permission denied')), \
                mock.patch.object(source_cluster.agent, 'cmd_drop_caches', return_value={'seconds': 0.1}), \
                self.assertRaises(SystemExit) as failed:
            source_cluster.cmd_backup_mode_probe(Namespace(root='/w/src', bin_dir='/b', repeats=2))
        self.assertIn('permission denied', str(failed.exception))


class ReferenceView(unittest.TestCase):
    def test_what_a_clean_stop_rewrites_is_left_out(self):
        view = measure.reference_view(SOURCE_DIGEST)
        self.assertEqual(sorted(view['entries']), ['base/5/16397', 'global/1262'])
        self.assertEqual((view['files'], view['bytes']), (2, 16))
        self.assertEqual(view['excluded'], ['global/pg_control', 'pg_stat/pgstat.stat',
                                            'pg_wal/000000010000000000000003'])
        for path, excluded in (('pg_wal/archive_status/x.done', True), ('pg_stat/pgstat.stat', True),
                               ('global/pg_control', True), ('global/pg_controlx', False),
                               ('base/5/pg_stat', False), ('pg_stat_tmp/x', False)):
            self.assertEqual(measure.reference_excluded(path), excluded, path)

    def test_the_csv_writes_every_new_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'summary.csv'
            measure.write_csv(path, [{'method': 'full_wb', 'block': 'main', 'durability': {'ok': True}}])
            with open(path, newline='') as stream:
                [row] = list(csv.DictReader(stream))
        self.assertEqual((row['tier'], row['block'], row['durability_ok']), ('host_tuned', 'main', 'True'))
        for column in ('pbb_checkpoint_s', 'backup_mode_adder_s', 'probackup_sync_s', 'parcopy_balance', 'usable_s',
                       'void', 'bytes_identical', 'measured_s2d_gbit_8streams', 'link_time_s', 'above_link_s',
                       'sysctl_dirty_background_bytes', 'sysctl_dirty_background_ratio', 'disk_write_dest_bytes',
                       'dirty_increase_mb', 'source_cache', 'durable'):
            self.assertIn(column, row)


if __name__ == '__main__':
    unittest.main()
