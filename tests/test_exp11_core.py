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
# pg_probackup 2.5 catchup's INFO lines on its console, stamped as above.
PROBACKUP_LOG = """\
[+0.012000] INFO: Database catchup start, pg_probackup version: 2.5.16, PostgreSQL version: 17.6, remote: true, catchup-source-pgdata: /data/exp04/src/data, catchup-destination-pgdata: /data/exp04/out/probackup8
[+0.402000] INFO: PGDATA size: 2112MB
[+0.410000] INFO: Start LSN (source): 2/A0000028, TLI: 1
[+0.512000] INFO: Start transferring data files
[+2.912000] INFO: Data files are transferred, time elapsed: 2s
[+2.913000] INFO: wait for pg_stop_backup()
[+2.960000] INFO: pg_stop backup() successfully executed
[+3.010000] INFO: Syncing copied files to disk
[+3.705000] INFO: Files are synced, time elapsed: 0
[+3.706000] INFO: Catchup completed
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
                 'fsync_s': 0.21, 'fsync_calls': 12, 'fsync_expected': 12}


class ParcopySummary(unittest.TestCase):
    def test_summary_of_the_receivers_stats(self):
        summary = measure.parcopy_summary(PARCOPY_STATS)
        self.assertEqual(summary, {'streams': 2, 'bytes': 2_108_000_000, 'max_stream_bytes': 1_100_000_000,
                                   'balance': round(1_100_000_000 / 1_054_000_000, 4), 'first_byte_s': 0.07,
                                   'last_byte_s': 1.71, 'write_s': 1.2, 'fsync_s': 0.21, 'fsync_calls': 12,
                                   'fsync_expected': 12, 'fsync_ok': True})
        self.assertFalse(measure.parcopy_summary({**PARCOPY_STATS, 'fsync_calls': 11})['fsync_ok'])
        empty = measure.parcopy_summary(None)
        self.assertEqual((empty['streams'], empty['balance'], empty['fsync_ok']), (None, None, None))

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
