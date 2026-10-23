"""Tests for the EXP-04 tooling's pure logic (experiments/network).

No VM, PostgreSQL, tc or rsync is needed: /proc text, rsync output and trees
are fixed samples or temporary directories. One test runs a short bulk TCP
stream over loopback.
"""

import csv
import io
import json
import os
from pathlib import Path
import random
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from unittest import mock

from benchmarks.methods import rsync_transfer_stats
from experiments.network import agent, measure, remote, report, run_exp04, source_cluster

# /proc/net/dev from pgd-source (Debian 12, kernel 6.1, virtio_net), trimmed.
NETDEV = """\
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo:  123456     789    0    0    0     0          0         0   123456     789    0    0    0     0       0          0
  ens5: 9876543210 6543210    0    3    0     0          0         0 1234567890 987654    0    0    0     0       0          0
"""
PROC_STAT = """\
cpu  1000 20 300 50000 40 0 60 7 0 0
cpu0 125 2 37 6250 5 0 7 1 0 0
intr 12345
"""
# rsync 3.2.7 (Debian 12) pushing S to the rsync daemon on pgd-dest in the
# EXP-04 smoke run: rsync --archive --checksum --no-whole-file --inplace
# --block-size=8192 --delete --stats S/ rsync://10.128.0.13:8730/exp04/rsync_8k/
RSYNC_DAEMON_PUSH = """
Number of files: 1,019 (reg: 992, dir: 27)
Number of created files: 1 (reg: 1)
Number of deleted files: 0
Number of regular files transferred: 14
Total file size: 189,244,280 bytes
Total transferred file size: 96,386,972 bytes
Literal data: 58,785,692 bytes
Matched data: 37,601,280 bytes
File list size: 0
File list generation time: 0.002 seconds
File list transfer time: 0.000 seconds
Total bytes sent: 58,858,526
Total bytes received: 69,523

sent 58,858,526 bytes  received 69,523 bytes  39,285,366.00 bytes/sec
total size is 189,244,280  speedup is 3.21
"""


def snap(rx, tx, ticks, *, iface='ens5', monotonic=0.0):
    names = measure.CPU_FIELDS
    return {'iface': iface, 'net': {'rx_bytes': rx, 'tx_bytes': tx, 'rx_packets': rx // 1000,
                                    'tx_packets': tx // 1000},
            'cpu_ticks': dict(zip(names, ticks)), 'clk_tck': 100, 'monotonic': monotonic}


class ProcParsing(unittest.TestCase):
    def test_netdev(self):
        interfaces = measure.parse_netdev(NETDEV)
        self.assertEqual(set(interfaces), {'lo', 'ens5'})
        self.assertEqual(interfaces['ens5']['rx_bytes'], 9876543210)
        self.assertEqual(interfaces['ens5']['rx_drop'], 3)
        self.assertEqual(interfaces['ens5']['tx_bytes'], 1234567890)
        self.assertEqual(interfaces['ens5']['tx_packets'], 987654)

    def test_netdev_rejects_short_or_empty_text(self):
        with self.assertRaises(ValueError):
            measure.parse_netdev('ens5: 1 2 3\n')
        with self.assertRaises(ValueError):
            measure.parse_netdev('Inter-|   Receive |  Transmit\n')

    def test_proc_stat_aggregate_line(self):
        ticks = measure.parse_proc_stat(PROC_STAT)
        self.assertEqual(ticks['user'], 1000)
        self.assertEqual(ticks['idle'], 50000)
        self.assertEqual(ticks['steal'], 7)

    def test_proc_stat_pads_old_kernels_and_needs_the_cpu_line(self):
        ticks = measure.parse_proc_stat('cpu  1 2 3 4\n')
        self.assertEqual(ticks['iowait'], 0)
        self.assertEqual(ticks['guest_nice'], 0)
        with self.assertRaises(ValueError):
            measure.parse_proc_stat('cpu0 1 2 3 4\n')

    def test_snapshot_delta(self):
        before = snap(1000, 5000, (100, 0, 50, 1000, 10, 0, 5, 0, 0, 0), monotonic=1.0)
        after = snap(3000, 905000, (250, 0, 80, 1100, 30, 1, 25, 2, 0, 0), monotonic=3.5)
        delta = measure.snapshot_delta(before, after)
        self.assertEqual(delta['net']['rx_bytes'], 2000)
        self.assertEqual(delta['net']['tx_bytes'], 900000)
        # busy = user + nice + system + irq + softirq + steal = 150 + 30 + 1 + 20 + 2 ticks
        self.assertEqual(delta['cpu']['busy_seconds'], 2.03)
        self.assertEqual(delta['cpu']['iowait_seconds'], 0.2)
        self.assertEqual(delta['interval_seconds'], 2.5)

    def test_snapshot_delta_rejects_backwards_counters_and_other_interfaces(self):
        ticks = (1,) * 10
        with self.assertRaises(ValueError):
            measure.snapshot_delta(snap(10, 10, ticks), snap(5, 10, ticks))
        with self.assertRaises(ValueError):
            measure.snapshot_delta(snap(1, 1, ticks), snap(1, 1, ticks, iface='lo'))

    def test_snapshot_reads_proc_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / 'net').mkdir()
            (proc / 'net' / 'dev').write_text(NETDEV)
            (proc / 'stat').write_text(PROC_STAT)
            result = measure.snapshot('ens5', proc)
            self.assertEqual(result['net']['tx_bytes'], 1234567890)
            self.assertEqual(result['cpu_ticks']['system'], 300)
            with self.assertRaises(ValueError):
                measure.snapshot('ens4', proc)


class Speeds(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(measure.parse_speed('0.5'),
                         {'label': '0.5g', 'gbit': 0.5, 'tc_rate': '500mbit'})
        self.assertEqual(measure.parse_speed('0.25')['tc_rate'], '250mbit')
        self.assertEqual(measure.parse_speed('10')['tc_rate'], '10000mbit')
        self.assertEqual(measure.parse_speed(' Unthrottled ')['tc_rate'], 'off')
        self.assertEqual(measure.parse_speed('2g')['label'], '2g')

    def test_manifest_checksums_default_is_what_exp04_ran(self):
        args = run_exp04.parse_args(['--source-ip', 'x', '--out', 'y'])
        self.assertEqual(args.manifest_checksums, 'SHA256')
        args = run_exp04.parse_args(['--source-ip', 'x', '--out', 'y', '--manifest-checksums', 'CRC32C'])
        self.assertEqual(args.manifest_checksums, 'CRC32C')

    def test_rejects_bad_speeds(self):
        for text in ('0', '-1', 'nan', 'inf', '0.0005', 'fast'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                measure.parse_speed(text)
        with self.assertRaises(ValueError):
            measure.parse_speeds('1,1.0')
        with self.assertRaises(ValueError):
            measure.parse_speeds(' , ')

    def test_default_sweep(self):
        speeds = measure.parse_speeds(run_exp04.DEFAULT_SPEEDS)
        self.assertEqual([s['tc_rate'] for s in speeds],
                         ['100mbit', '250mbit', '500mbit', '1000mbit', '2000mbit', '5000mbit',
                          '10000mbit', 'off'])

    def test_rate(self):
        self.assertEqual(measure.gbit_per_second(125_000_000, 1.0), 1.0)
        self.assertIsNone(measure.gbit_per_second(1, 0))

    def test_method_orders_are_seeded_permutations(self):
        first = measure.method_orders(measure.METHODS, random.Random(4), 5)
        again = measure.method_orders(measure.METHODS, random.Random(4), 5)
        self.assertEqual(first, again)
        for order in first:
            self.assertEqual(sorted(order), sorted(measure.METHODS))
        self.assertGreater(len({tuple(order) for order in first}), 1)


class Trees(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def make(self, name, files):
        top = self.root / name
        for rel, data in files.items():
            path = top / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        top.mkdir(exist_ok=True)
        return top

    def test_identical_trees(self):
        files = {'base/1/16384': b'x' * 9000, 'global/pg_control': b'c', 'pg_wal/000001': b''}
        left = measure.tree_digest(self.make('S', files))
        right = measure.tree_digest(self.make('O', files))
        self.assertEqual(measure.compare_digests(left, right), (True, []))
        self.assertEqual(left['bytes'], 9001)
        self.assertEqual(left['files'], 3)
        self.assertEqual(measure.digest_summary(left)['listing_sha256'],
                         measure.digest_summary(right)['listing_sha256'])

    def test_differences(self):
        left = measure.tree_digest(self.make('S', {'a': b'1', 'b': b'2', 'c': b'3'}))
        right = measure.tree_digest(self.make('O', {'a': b'1', 'b': b'X', 'd': b'4'}))
        same, differences = measure.compare_digests(left, right)
        self.assertFalse(same)
        self.assertEqual(differences, ["differs: b ['file', 1] vs ['file', 1]",
                                       'missing in output: c', 'extra in output: d'])

    def test_symlinks_are_not_followed(self):
        top = self.make('S', {'a': b'1'})
        os.symlink('a', top / 'link')
        digest = measure.tree_digest(top)
        self.assertEqual(digest['entries']['link'][:3], ['link', 0, 'a'])
        self.assertEqual(digest['files'], 1)

    def test_independence(self):
        basis = self.make('B0', {'a': b'1', 'b': b'2'})
        output = self.make('O', {'c': b'3'})
        os.link(basis / 'a', output / 'a')
        os.link(output / 'c', self.root / 'c-second-link')
        independent, problems = measure.independence(measure.tree_digest(output),
                                                     measure.tree_digest(basis))
        self.assertFalse(independent)
        self.assertEqual(problems, ['shares an inode with the basis: a',
                                    'has 2 hard links: c'])
        clean = self.make('O2', {'a': b'1'})
        self.assertEqual(measure.independence(measure.tree_digest(clean),
                                              measure.tree_digest(basis)), (True, []))

    def test_tree_stat_sees_writes(self):
        top = self.make('B0', {'a': b'1', 'sub/b': b'2'})
        before = measure.tree_stat(top)
        self.assertEqual(measure.stat_changes(before, measure.tree_stat(top)), [])
        (top / 'sub' / 'b').write_bytes(b'22')
        (top / 'new').write_bytes(b'')
        changes = measure.stat_changes(before, measure.tree_stat(top))
        self.assertEqual([change.split(':')[0] for change in changes], ['new', 'sub/b'])


def phase(name, wall, *, s_tx, s_rx, d_tx, d_rx, s_cpu, d_cpu, commands=()):
    return {'name': name, 'wall_seconds': wall, 'commands': list(commands), 'failures': [],
            'net': {'source': {'tx_bytes': s_tx, 'rx_bytes': s_rx},
                    'dest': {'tx_bytes': d_tx, 'rx_bytes': d_rx}},
            'cpu': {'source': {'busy_seconds': s_cpu}, 'dest': {'busy_seconds': d_cpu}}}


def command(host, cpu, maxrss, wall=1.0, inner=None):
    # wait4's ru_maxrss is the launcher's high-water mark; the peak is GNU time's.
    return {'host': host, 'exit_code': 0, 'wall_seconds': wall, 'inner_wall_seconds': inner,
            'rusage': {'cpu_seconds': cpu, 'wait4_maxrss_kb': 420644}, 'peak_rss_kb': maxrss}


class Results(unittest.TestCase):
    def staged_phases(self):
        return {
            'capture': phase('capture', 1.25, s_tx=2500, s_rx=1700, d_tx=950, d_rx=1240,
                             s_cpu=1.2, d_cpu=0.01,
                             commands=[command('source', 0.28, 20000, 1.04, 0.96)]),
            'transfer': phase('transfer', 2.5, s_tx=58_670_000, s_rx=354_000, d_tx=355_000,
                              d_rx=58_729_000, s_cpu=0.88, d_cpu=1.88,
                              commands=[command('dest', 1.39, 30000),
                                        command('source', 0.645, 24700, 2.26, 2.18)]),
        }

    def test_phase_totals(self):
        totals = measure.phase_totals(self.staged_phases())
        self.assertEqual(totals['wall_seconds'], 3.75)
        self.assertEqual(totals['wire_bytes_source_to_dest'], 58_672_500)
        self.assertEqual(totals['wire_bytes_dest_to_source'], 355_950)
        self.assertEqual(totals['wire_bytes_source_to_dest_dest_rx'], 58_730_240)
        self.assertEqual(totals['wire_bytes_dest_to_source_source_rx'], 355_700)
        self.assertEqual(totals['cpu_seconds_source'], 2.08)
        self.assertEqual(totals['cpu_seconds_dest'], 1.89)
        self.assertEqual(totals['process_cpu_seconds_source'], 0.925)
        self.assertEqual(totals['process_cpu_seconds_dest'], 1.39)
        self.assertEqual(totals['peak_rss_kb_source'], 24700)
        self.assertEqual(totals['peak_rss_kb_dest'], 30000)
        self.assertEqual(totals['remote_overhead_seconds'], 0.16)
        self.assertEqual(totals['wall_seconds_less_remote_overhead'], 3.59)

    def test_phase_totals_unknown_when_a_phase_lacks_a_counter(self):
        phases = self.staged_phases()
        del phases['transfer']['net']['source']['tx_bytes']
        totals = measure.phase_totals(phases)
        self.assertIsNone(totals['wire_bytes_source_to_dest'])
        self.assertEqual(totals['wire_bytes_dest_to_source'], 355_950)
        self.assertIsNone(measure.phase_totals({})['wall_seconds'])

    def record(self, **changes):
        phases = self.staged_phases()
        record = {'run_name': 'smoke', 'trial_id': '00-uniform-f0.01-s5', 'pattern': 'uniform',
                  'fraction': 0.01, 'scale': 5, 'rows_changed': 5000, 'repeat': 0,
                  'speed': {'label': '0.5g', 'gbit': 0.5, 'tc_rate': '500mbit'},
                  'method': 'pgdelta', 'order_index': 3, 'status': 'pass', 'error': None,
                  'link': {'source_to_dest_gbit': 0.4794, 'dest_to_source_gbit': 0.4793},
                  'phases': phases, 'totals': measure.phase_totals(phases),
                  'preparation': None, 'caches_dropped': True,
                  'tool_reported': {'bytes_source_to_dest': 58_545_664,
                                    'bytes_dest_to_source': 199_514},
                  'verification': {'verified': True, 'identical_to_capture': True},
                  'output_bytes': 189_244_280, 'capture_bytes': 189_244_280}
        record.update(changes)
        return record

    def test_summary_row(self):
        row = measure.summary_row(self.record())
        self.assertEqual(set(row), set(measure.CSV_FIELDS))
        self.assertEqual(row['speed'], '0.5g')
        self.assertEqual(row['target_gbit'], 0.5)
        self.assertEqual(row['measured_s2d_gbit'], 0.4794)
        self.assertEqual(row['capture_s'], 1.25)
        self.assertEqual(row['transfer_s'], 2.5)
        self.assertIsNone(row['combine_s'])
        self.assertEqual(row['total_s'], 3.75)
        self.assertEqual(row['wire_s2d_bytes'], 58_672_500)
        self.assertEqual(row['tool_s2d_bytes'], 58_545_664)
        self.assertEqual(row['ssh_overhead_s'], 0.16)
        self.assertTrue(row['identical_to_s'])

    def test_summary_row_of_a_failed_run(self):
        row = measure.summary_row({'trial_id': 't', 'method': 'full', 'status': 'error',
                                   'speed': {'label': 'unthrottled', 'gbit': None},
                                   'error': 'CommandError: boom'})
        self.assertEqual(row['status'], 'error')
        self.assertIsNone(row['total_s'])
        self.assertEqual(row['error'], 'CommandError: boom')

    def test_write_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'summary.csv'
            measure.write_csv(path, [self.record(), self.record(method='full', order_index=0)])
            with open(path, newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(tuple(rows[0]), measure.CSV_FIELDS)
            self.assertEqual([row['method'] for row in rows], ['pgdelta', 'full'])
            self.assertEqual(rows[0]['wire_s2d_bytes'], '58672500')
            self.assertEqual(rows[0]['combine_s'], '')
            self.assertFalse((Path(tmp) / 'summary.csv.tmp').exists())

    def test_rsync_daemon_push_stats(self):
        stats = rsync_transfer_stats(RSYNC_DAEMON_PUSH)
        self.assertEqual(stats['bytes_source_to_dest'], 58_858_526)
        self.assertEqual(stats['bytes_dest_to_source'], 69_523)
        self.assertEqual(stats['literal_bytes'], 58_785_692)
        self.assertEqual(stats['matched_bytes'], 37_601_280)
        self.assertEqual(stats['files_transferred'], 14)
        self.assertEqual(stats['stats_format'], 'gnu')

    def test_agent_result_line(self):
        out = 'noise\n' + agent.RESULT_PREFIX + '{"a": 1}\nmore noise\n'
        self.assertEqual(run_exp04.agent_result(out), {'a': 1})
        with self.assertRaises(ValueError):
            run_exp04.agent_result('nothing here\n')

    def test_emit_round_trips(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            agent.emit({'x': [1, 2], 'y': None})
        self.assertEqual(run_exp04.agent_result(buffer.getvalue()), {'x': [1, 2], 'y': None})


class Report(unittest.TestCase):
    def rows(self):
        def row(speed, gbit, method, total, status='pass', measured=None):
            return {'trial_id': 't0', 'speed': speed, 'target_gbit': gbit, 'method': method,
                    'total_s': total, 'status': status, 'measured_s2d_gbit': measured,
                    'error': ''}
        return [row('0.5g', 0.5, 'full', 30.0), row('0.5g', 0.5, 'pgdelta', 12.0),
                row('0.5g', 0.5, 'pgdelta', 14.0), row('0.5g', 0.5, 'pgdelta', 100.0, 'error'),
                row('2g', 2.0, 'full', 9.0), row('2g', 2.0, 'pgdelta', 9.5),
                row('unthrottled', None, 'full', 3.0, measured=14.9),
                row('unthrottled', None, 'pgdelta', 8.0, measured=14.9),
                row('10g', 10.0, 'full', 3.2), row('10g', 10.0, 'pgdelta', 7.5)]

    def test_speed_order_puts_unthrottled_at_its_measured_rate(self):
        self.assertEqual(report.speed_order(self.rows()), ['0.5g', '2g', '10g', 'unthrottled'])

    def test_medians_skip_failed_runs(self):
        table = report.medians(self.rows())
        self.assertEqual(table[('t0', '0.5g')]['pgdelta'], 13.0)

    def test_crossover(self):
        rows = self.rows()
        result = report.crossover(report.medians(rows), report.speed_order(rows), 't0',
                                  'pgdelta')
        self.assertAlmostEqual(result['ratios']['0.5g'], 0.433, places=3)
        self.assertEqual(result['not_faster_from'], '2g')
        self.assertFalse(result['faster_everywhere'])

    def test_crossover_compares_the_times_not_rounded_ratios(self):
        # The smoke run's uniform 0.5g cell: pgdelta (serial) 3.127 s against full 3.128 s is
        # faster, though its ratio, 0.99968, rounds to 1.000.
        def row(method, total):
            return {'trial_id': 't0', 'speed': '0.5g', 'target_gbit': 0.5, 'method': method,
                    'total_s': total, 'status': 'pass', 'measured_s2d_gbit': None, 'error': ''}
        rows = [row('full', 3.128), row('pgdelta', 3.127), row('pgdelta_1thread', 3.269)]
        table, speeds = report.medians(rows), report.speed_order(rows)
        self.assertTrue(report.crossover(table, speeds, 't0', 'pgdelta')['faster_everywhere'])
        self.assertEqual(report.crossover(table, speeds, 't0', 'pgdelta_1thread')['not_faster_from'], '0.5g')
        # A tie is not faster.
        tie = report.medians([row('full', 3.128), row('pgdelta', 3.128)])
        self.assertEqual(report.crossover(tie, speeds, 't0', 'pgdelta')['not_faster_from'], '0.5g')
        # Ratios are rounded once, for display: 3.269 / 3.128 = 1.04508 is 1.05, not 1.04 via 1.045.
        text = report.render(rows)
        self.assertIn('| pgdelta | 1.00 | never (faster at every speed) |', text)
        self.assertIn('| pgdelta_1thread | 1.05 | 0.5g |', text)

    def test_render_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'summary.csv'
            with open(path, 'w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=measure.CSV_FIELDS)
                writer.writeheader()
                for row in self.rows():
                    writer.writerow({key: ('' if value is None else value)
                                     for key, value in row.items()})
            text = report.render(report.load(path))
        self.assertIn('| pgdelta | 0.43 | 1.06 | 2.34 | 2.67 | 2g |', text)
        self.assertIn('1 run(s) did not pass', text)


class Agent(unittest.TestCase):
    def test_remove_stays_below_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'work'
            (root / 'out' / 'x').mkdir(parents=True)
            (root / 'file').write_text('1')
            outside = Path(tmp) / 'keep'
            outside.mkdir()
            result = agent.cmd_remove(Namespace(under=str(root), paths=[
                str(root / 'out'), str(root / 'file'), str(root / 'missing')]))
            self.assertEqual(len(result['removed']), 2)
            self.assertFalse((root / 'out').exists())
            for bad in (str(outside), str(root), 'relative/path', str(root / '..' / 'keep')):
                with self.subTest(path=bad), self.assertRaises(SystemExit):
                    agent.cmd_remove(Namespace(under=str(root), paths=[bad]))
            self.assertTrue(outside.exists())

    def test_run_reports_exit_code_and_log_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = agent.cmd_run(Namespace(
                log=str(Path(tmp) / 'logs' / 'x.log'), cwd=tmp, env=['EXP04_TEST=yes'],
                argv=['--', 'sh', '-c', 'echo "$EXP04_TEST $(pwd)"; exit 3']))
        self.assertEqual(result['exit_code'], 3)
        self.assertIn('yes', result['log_tail'])
        self.assertEqual(result['argv'][:2], ['sh', '-c'])
        self.assertGreaterEqual(result['rusage']['cpu_seconds'], 0)

    def test_blast_over_loopback(self):
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        received = {}

        def receive():
            with redirect_stdout(io.StringIO()):
                received.update(agent.cmd_blast_recv(Namespace(
                    listen=f'127.0.0.1:{port}', timeout=20, warmup=0.05)))

        thread = threading.Thread(target=receive)
        thread.start()
        for _ in range(100):
            try:
                sent = agent.cmd_blast_send(Namespace(connect=f'127.0.0.1:{port}', seconds=0.2))
                break
            except ConnectionRefusedError:
                time.sleep(0.02)
        thread.join(30)
        self.assertEqual(received['bytes'], sent['bytes'])
        self.assertGreater(received['gbit'], 0)
        self.assertIn('steady_gbit', received)


class Remote(unittest.TestCase):
    def test_parse_instance(self):
        description = {'name': 'pgd-source', 'status': 'RUNNING', 'networkInterfaces': [
            {'networkIP': '10.128.0.12', 'accessConfigs': [{'natIP': '203.0.113.12'}]}]}
        self.assertEqual(remote.parse_instance(description),
                         {'name': 'pgd-source', 'internal': '10.128.0.12',
                          'external': '203.0.113.12', 'status': 'RUNNING'})
        description['networkInterfaces'][0].pop('accessConfigs')
        self.assertIsNone(remote.parse_instance(description)['external'])

    def test_config_from_config_sh(self):
        with mock.patch.dict(os.environ, {'ZONE': 'us-central1-x', 'SSH_KEY': '/k/id'}):
            config = remote.load_config()
        self.assertEqual(config['ZONE'], 'us-central1-x')
        self.assertEqual(config['SOURCE_VM'], 'pgd-source')
        self.assertEqual(config['DEST_VM'], 'pgd-dest')
        self.assertEqual(config['SSH_KNOWN_HOSTS'], '/k/known_hosts')

    def test_tree_sha256_covers_content_and_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'a').write_text('1')
            (root / 'b').write_text('2')
            first = remote.tree_sha256(root, ['b', 'a'])
            self.assertEqual(first, remote.tree_sha256(root, ['a', 'b']))
            (root / 'a').chmod(0o755)
            second = remote.tree_sha256(root, ['a', 'b'])
            (root / 'a').write_text('3')
            self.assertEqual(len({first, second, remote.tree_sha256(root, ['a', 'b'])}), 3)

    def test_results_dir_rejects_paths(self):
        self.assertEqual(remote.results_dir('smoke'), '/data/exp04-results/smoke')
        for bad in ('', '../x', 'a/b', '.hidden'):
            with self.subTest(name=bad), self.assertRaises(SystemExit):
                remote.results_dir(bad)


class SourceCluster(unittest.TestCase):
    def test_trial_spec_uses_exp01_defaults(self):
        spec = source_cluster.trial_spec(Namespace(
            scale=5, pattern='uniform', fraction=0.01, seed=20261022, hot_percent=1.0,
            events_rows=None, batch_size=100))
        self.assertEqual(spec['accounts_rows'], 500000)
        self.assertEqual(spec['events_rows'], 500000)
        self.assertTrue(spec['data_checksums'])
        self.assertTrue(spec['load_vacuum'])
        self.assertFalse(spec['read_scan'] or spec['vacuum'] or spec['checkpoint'])
        self.assertEqual(spec['seed'], 20261022)
        self.assertEqual(source_cluster.rows_to_change(spec), 5000)

    def test_hba_lines_admit_only_the_destination(self):
        self.assertEqual(source_cluster.hba_lines('10.128.0.13'),
                         ['host all study 10.128.0.13/32 trust',
                          'host replication study 10.128.0.13/32 trust'])


class Arguments(unittest.TestCase):
    def test_defaults(self):
        args = run_exp04.parse_args(['--source-ip', '10.0.0.1', '--out', '/tmp/x'])
        self.assertEqual(args.scale, 70)
        self.assertEqual(args.methods.split(','), list(measure.METHODS))
        self.assertEqual(args.seed, 20261022)

    def test_rejects_bad_speeds(self):
        with redirect_stdout(io.StringIO()), mock.patch('sys.stderr', io.StringIO()):
            with self.assertRaises(SystemExit):
                run_exp04.parse_args(['--source-ip', 'x', '--out', 'y', '--speeds', '1,1'])


# --- the fairness fixes: stubbed orchestrator runs -----------------------------

# GNU time stand-in: parses -f FORMAT -o FILE, runs the command, writes FORMAT
# with fixed numbers (maxrss 4321 KiB) like GNU time does, exits with its code.
STUB_GNU_TIME = """\
import subprocess, sys
args = sys.argv[1:]
fmt = out = None
while args and args[0].startswith('-'):
    flag = args.pop(0)
    if flag == '-f':
        fmt = args.pop(0)
    elif flag == '-o':
        out = args.pop(0)
code = subprocess.call(args)
line = (fmt.replace('%M', '4321').replace('%e', '0.01').replace('%U', '0.02')
        .replace('%S', '0.03').replace('%x', str(code)))
with open(out, 'w') as stream:
    if code:
        stream.write(f'Command exited with non-zero status {code}\\n')
    stream.write(line + '\\n')
sys.exit(code)
"""


def stub_gnu_time(directory) -> str:
    path = Path(directory) / 'gnu-time'
    path.write_text(f'#!{os.path.realpath(__import__("sys").executable)}\n' + STUB_GNU_TIME)
    path.chmod(0o755)
    return str(path)


class FakeClock:
    """Stands in for time.perf_counter: stubbed commands advance it by their wall time."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class StubOrchestrator(run_exp04.Orchestrator):
    """An orchestrator whose VM-touching primitives log events instead.

    Commands take fixed wall times (advancing a fake clock) and the counters
    move by fixed amounts per snapshot, so phase timers and totals are exact.
    """

    COMMAND_WALL = {'cp': 3.0, 'sync': 0.5, 'pg_basebackup': 1.0, 'rsync': 2.0,
                    'python3': 2.0}
    REMOTE_OVERHEAD = 0.06

    def __init__(self, tmp, clock, extra_args=()):
        args = run_exp04.parse_args(['--source-ip', '10.0.0.12', '--out', f'{tmp}/out',
                                     '--work-dir', f'{tmp}/work', '--ssh-key', '/nonexistent',
                                     *extra_args])
        super().__init__(args)
        Path(args.work_dir).mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.events = []
        self.dest_ip, self.dest_iface, self.source_iface = '10.0.0.13', 'ens4', 'ens4'
        self.trial_id = '00-uniform-f0.01-s5'
        self.b0_digest, self.b0_stat = {'entries': {}, 'device': 1}, {}
        self.ticks = {'source': 0, 'dest': 0}
        self.source.agent = self._source_agent
        self.source.script = self._source_script
        self.source.write = lambda path, text: self.events.append(('source_write', path, text))

    def _source_agent(self, *args, timeout=600):
        self.events.append(('source_agent', args))
        return {'exit_code': 0, 'log_tail': ''}

    def _source_script(self, script, *args, timeout=7200):
        self.events.append(('source_script', args))
        return {}

    def concurrently(self, local, remote):
        # A sequential stand-in on the fake clock: both parts start at the same
        # instant and the pair ends when the longer one does. (The real
        # concurrency is tested with a barrier, test_prep_and_capture_overlap.)
        start = self.clock.now
        source = remote()
        source_end = self.clock.now
        self.clock.now = start
        dest = local()
        self.clock.now = max(self.clock.now, source_end)
        return source, dest

    def _snap(self, host):
        # 1 CPU-second (100 ticks) and 1000 tx bytes per snapshot on each VM.
        self.ticks[host] += 1
        n = self.ticks[host]
        return snap(n * 1000, n * 1000, (n * 100, 0, 0, 0, 0, 0, 0, 0, 0, 0),
                    iface='ens4', monotonic=self.clock.now)

    def snapshot_source(self):
        return self._snap('source')

    def snapshot_dest(self):
        return self._snap('dest')

    def drop_caches(self):
        self.events.append('drop_caches')
        return {'source': {'dropped': True}, 'dest': {'dropped': True}}

    def clean_method(self, paths):
        self.events.append('clean')

    def remove_dest(self, *paths):
        pass

    def remove_source(self, *paths):
        pass

    def local_agent(self, *args, timeout=600):
        self.events.append(('local_agent', args))
        return {}

    def _wall(self, argv):
        return self.COMMAND_WALL.get(Path(str(argv[0])).name, 1.0)

    def local_command(self, argv, log_name, *, timeout=None, cwd=None, env=None):
        argv = [str(part) for part in argv]
        self.events.append(('dest', argv, log_name))
        wall = self._wall(argv)
        self.clock.now += wall
        return {'host': 'dest', 'argv': argv, 'exit_code': 0, 'wall_seconds': wall,
                'started_utc': 0, 'rusage': {'cpu_seconds': 0.1, 'wait4_maxrss_kb': 900000},
                'peak_rss_kb': 2000, 'log': log_name, 'timed_out': False}

    def remote_command(self, argv, log_name, *, cwd=None):
        argv = [str(part) for part in argv]
        self.events.append(('source', argv, log_name))
        inner = self._wall(argv)
        self.clock.now += inner + self.REMOTE_OVERHEAD
        return {'host': 'source', 'argv': argv, 'exit_code': 0,
                'wall_seconds': round(inner + self.REMOTE_OVERHEAD, 6),
                'inner_wall_seconds': inner, 'started_utc': 0,
                'rusage': {'cpu_seconds': 0.2, 'wait4_maxrss_kb': 900000},
                'peak_rss_kb': 3000, 'log': log_name, 'log_tail': RSYNC_DAEMON_PUSH,
                'timed_out': False}

    def start_rsyncd(self, *args):
        self.events.append(('start_rsyncd', args))
        return 'daemon'

    def stop_rsyncd(self, daemon):
        self.events.append('stop_rsyncd')
        return {'host': 'dest', 'argv': ['rsync', '--daemon'], 'exit_code': 0,
                'wall_seconds': 9.0, 'rusage': {'cpu_seconds': 0.5}, 'peak_rss_kb': 5000}

    def verify(self, method, paths, phases):
        self.events.append('verify')
        return {'verified': True, 'basis_changes': [], 'identical_to_capture': True,
                'output_bytes': 189_244_280, 'capture_bytes': 189_244_280}

    def tool_reported(self, method, paths, phases):
        return {'bytes_source_to_dest': 1, 'bytes_dest_to_source': 1}


TRIAL = {'pattern': 'uniform', 'fraction': 0.01, 'scale': 5, 'rows_changed': 5000}
SPEED = {'label': '0.5g', 'gbit': 0.5, 'tc_rate': '500mbit'}
LINK = {'source_to_dest_gbit': 0.479, 'dest_to_source_gbit': 0.479}


class StubbedRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        for patcher in (mock.patch.object(run_exp04.time, 'perf_counter', self.clock),
                        mock.patch.object(run_exp04, 'log')):
            patcher.start()
            self.addCleanup(patcher.stop)

    def orchestrator(self, cls=StubOrchestrator, extra_args=()):
        orchestrator = cls(self.tmp.name, self.clock, extra_args)
        self.addCleanup(orchestrator.pool.shutdown)
        return orchestrator

    def run_method(self, orchestrator, method, speed=SPEED):
        return orchestrator.run_method(TRIAL, 0, speed, dict(LINK), method, 0, [method])

    # rsync's copy of B0 is a timed, durable step in the end-to-end total. It runs
    # on dest at the same time as the capture on source (round 2, finding 1),
    # after the one cache drop of the run, which leaves O warm (finding 2).

    def event_kinds(self, orchestrator):
        return [event if isinstance(event, str) else
                (event[0], Path(event[1][0]).name) if event[0] in ('dest', 'source')
                else event[0] for event in orchestrator.events]

    def test_manifest_checksums_reach_every_backup_and_combine(self):
        for value in ('SHA256', 'CRC32C'):
            orchestrator = self.orchestrator(extra_args=['--manifest-checksums', value])
            for method in ('incremental', 'rsync_8k', 'full'):  # rsync_8k's capture is the staged one
                record = self.run_method(orchestrator, method)
                self.assertEqual(record['status'], 'pass', record.get('traceback'))
            seen = []
            for event in orchestrator.events:
                if isinstance(event, tuple) and event[0] in ('dest', 'source'):
                    argv = [str(a) for a in event[1]]
                    tool = Path(argv[0]).name
                    if tool in ('pg_basebackup', 'pg_combinebackup'):
                        joined = ' '.join(argv)
                        self.assertTrue(f'--manifest-checksums {value}' in joined
                                        or f'--manifest-checksums={value}' in joined, joined)
                        seen.append(tool)
            self.assertIn('pg_basebackup', seen)
            self.assertIn('pg_combinebackup', seen)

    def test_rsync_prep_runs_beside_the_capture_after_one_cache_drop(self):
        orchestrator = self.orchestrator()
        record = self.run_method(orchestrator, 'rsync_8k')
        self.assertEqual(record['status'], 'pass', record.get('traceback'))
        self.assertEqual(list(record['phases']), ['prep_capture', 'transfer'])
        pair = record['phases']['prep_capture']
        output = str(orchestrator.outputs / 'rsync_8k')
        prep = [command for command in pair['commands'] if command['part'] == 'prep']
        self.assertEqual([command['argv'] for command in prep],
                         [['cp', '-a', str(orchestrator.b0), output], ['sync', '-f', output]])
        capture = [command for command in pair['commands'] if command['part'] == 'capture']
        self.assertEqual([Path(command['argv'][0]).name for command in capture],
                         ['pg_basebackup'])
        self.assertEqual(pair['parts']['prep'], {**pair['parts']['prep'], 'host': 'dest',
                                                 'wall_seconds': 3.5, 'offset_seconds': 0.0})
        self.assertEqual(pair['parts']['capture'], {**pair['parts']['capture'], 'host': 'source',
                                                    'wall_seconds': 1.06,
                                                    'offset_seconds': 0.0})
        # The pair lasts as long as its longer part.
        self.assertEqual(pair['wall_seconds'], 3.5)
        kinds = self.event_kinds(orchestrator)
        # One drop, after the daemon starts and before both parts; none between
        # the copy and the transfer, so O stays in the page cache as cp left it.
        self.assertEqual(kinds.count('drop_caches'), 1)
        drop = kinds.index('drop_caches')
        self.assertLess(kinds.index('start_rsyncd'), drop)
        self.assertLess(drop, kinds.index(('dest', 'cp')))
        self.assertLess(drop, kinds.index(('source', 'pg_basebackup')))
        self.assertLess(kinds.index(('dest', 'sync')), kinds.index(('source', 'rsync')))
        self.assertNotIn('prep_cache_drop', record)

    def test_prep_and_capture_overlap(self):
        # Each part waits for the other to start: run one after the other, the
        # barrier breaks and the run fails.
        barrier = threading.Barrier(2, timeout=5)

        class Overlapping(StubOrchestrator):
            concurrently = run_exp04.Orchestrator.concurrently

            def local_command(self, argv, log_name, **kwargs):
                if Path(str(argv[0])).name == 'cp':
                    barrier.wait()
                return super().local_command(argv, log_name, **kwargs)

            def remote_command(self, argv, log_name, **kwargs):
                if Path(str(argv[0])).name == 'pg_basebackup':
                    barrier.wait()
                return super().remote_command(argv, log_name, **kwargs)

        record = self.run_method(self.orchestrator(Overlapping), 'rsync_default')
        self.assertEqual(record['status'], 'pass', record.get('traceback'))
        self.assertEqual(set(record['phases']['prep_capture']['parts']), {'prep', 'capture'})

    def test_both_waits_for_the_source_side_when_the_local_side_fails(self):
        orchestrator = self.orchestrator()
        finished = threading.Event()

        def remote():
            time.sleep(0.2)
            finished.set()

        def local():
            raise RuntimeError('cp failed')

        with self.assertRaises(RuntimeError):
            run_exp04.Orchestrator.both(orchestrator, local, remote)
        self.assertTrue(finished.is_set())

    def test_rsync_end_to_end_total_is_max_of_prep_and_capture_plus_transfer(self):
        # The stub's prep (cp 3.0 + sync 0.5) is longer than its capture (1.06).
        orchestrator = self.orchestrator()
        record = self.run_method(orchestrator, 'rsync_default')
        walls = {name: phase['wall_seconds'] for name, phase in record['phases'].items()}
        self.assertEqual(walls, {'prep_capture': 3.5, 'transfer': 2.06})
        self.assertEqual(record['totals']['wall_seconds'], 5.56)
        # The upper bound: prep, capture and transfer one after the other.
        self.assertEqual(record['totals_serial']['wall_seconds'], 6.62)
        # The in-place lower bound: no copy of B0.
        self.assertEqual(record['totals_in_place']['wall_seconds'], 3.12)
        # VM-wide CPU: one counter window per phase, 1 CPU-second per VM in the stub.
        self.assertEqual(record['totals']['cpu_seconds_dest'], 2.0)
        self.assertEqual(record['totals']['cpu_seconds_source'], 2.0)
        # In place: the prep's VM's CPU in the shared window is not separable.
        self.assertIsNone(record['totals_in_place']['cpu_seconds_dest'])
        self.assertEqual(record['totals_in_place']['cpu_seconds_source'], 2.0)
        row = measure.summary_row(record)
        self.assertEqual(row['total_s'], 5.56)
        self.assertEqual(row['serial_total_s'], 6.62)
        self.assertEqual(row['inplace_total_s'], 3.12)
        self.assertEqual(row['prep_s'], 3.5)
        self.assertEqual(row['capture_s'], 1.06)
        self.assertEqual(row['prep_capture_s'], 3.5)
        self.assertEqual(row['prep_capture_cpu_dest_s'], 1.0)
        self.assertEqual(row['cpu_dest_s'], 2.0)
        self.assertEqual(row['counter_windows'], 2)
        # Process CPU of the prep's cp and sync is in the dest figure too.
        self.assertEqual(row['proc_cpu_dest_s'], 0.7)

    def test_a_prep_shorter_than_the_capture_adds_no_time(self):
        class QuickCopy(StubOrchestrator):
            COMMAND_WALL = {**StubOrchestrator.COMMAND_WALL, 'cp': 0.3, 'sync': 0.2}

        record = self.run_method(self.orchestrator(QuickCopy), 'rsync_8k')
        row = measure.summary_row(record)
        self.assertEqual(row['prep_s'], 0.5)
        self.assertEqual(row['capture_s'], 1.06)
        self.assertEqual(row['prep_capture_s'], 1.06)
        self.assertEqual(row['total_s'], 3.12)
        self.assertEqual(row['inplace_total_s'], 3.12)
        self.assertEqual(row['serial_total_s'], 3.62)
        # The capture's SSH launch is on the critical path here; the transfer's always.
        self.assertEqual(row['ssh_overhead_s'], 0.12)
        self.assertEqual(row['total_no_ssh_s'], 3.0)
        self.assertEqual(row['serial_total_no_ssh_s'], 3.5)
        self.assertEqual(row['inplace_total_no_ssh_s'], 3.0)

    # Round 2, finding 3: the dest-only prep adds no source counter window.

    def test_rsync_source_cpu_has_as_many_counter_windows_as_pgdelta(self):
        orchestrator = self.orchestrator()
        rsync = self.run_method(orchestrator, 'rsync_8k')
        with mock.patch.object(run_exp04, 'Child', FakeChild):
            pgdelta = self.run_method(orchestrator, 'pgdelta')
        # Each window adds 1 CPU-second per VM in the stub (the snapshot floor).
        self.assertEqual(rsync['totals']['cpu_seconds_source'], 2.0)
        self.assertEqual(pgdelta['totals']['cpu_seconds_source'], 2.0)
        self.assertEqual(measure.summary_row(rsync)['counter_windows'],
                         measure.summary_row(pgdelta)['counter_windows'])

    def test_phase_floor_is_measured_with_empty_phases(self):
        orchestrator = self.orchestrator()
        floor = orchestrator.phase_floor(3)
        self.assertEqual(floor['repeats'], 3)
        for host in ('source', 'dest'):
            self.assertEqual(floor[host]['busy_seconds'], [1.0, 1.0, 1.0])
            self.assertEqual(floor[host]['median_busy_seconds'], 1.0)
        self.assertEqual(run_exp04.parse_args(['--source-ip', 'x', '--out', 'y'])
                         .phase_floor_repeats, 3)

    # Round 2, finding 4: one capture log per staged method.

    def test_staged_methods_keep_their_own_capture_and_stats_logs(self):
        orchestrator = self.orchestrator(
            extra_args=['--methods', 'pgdelta,pgdelta_1thread,rsync_8k,rsync_default'])
        captures = {}
        with mock.patch.object(run_exp04, 'Child', FakeChild):
            for method in ('pgdelta', 'pgdelta_1thread', 'rsync_8k', 'rsync_default'):
                orchestrator.events.clear()
                self.assertEqual(self.run_method(orchestrator, method)['status'], 'pass')
                captures[method] = [event[2] for event in orchestrator.events
                                    if isinstance(event, tuple) and event[0] == 'source'
                                    and Path(event[1][0]).name == 'pg_basebackup']
                mkdir = [event[1] for event in orchestrator.events
                         if isinstance(event, tuple) and event[0] == 'source_agent'
                         and 'mkdir' in event[1]]
                if method in measure.PGDELTA_METHODS:
                    log_path = mkdir[0][mkdir[0].index('--log') + 1]
                    self.assertTrue(log_path.endswith(f'/r0-0.5g-{method}-mkdir-stats.log'),
                                    log_path)
        self.assertEqual(captures, {method: [f'{method}-capture.log'] for method in captures})

    # Round 2, finding 7: full's byte reference.

    def test_basebackup_methods_name_their_byte_reference(self):
        orchestrator = self.orchestrator()
        for method in ('full', 'incremental'):
            reported = run_exp04.Orchestrator.tool_reported(
                orchestrator, method, orchestrator.method_paths(method), {})
            self.assertIn('output_minus_wal_padding_bytes', reported['scope'])
            self.assertIsNone(reported['bytes_source_to_dest'])

    def test_rsync_transfer_fsyncs_and_keeps_block_size_per_variant(self):
        orchestrator = self.orchestrator()
        argvs = {}
        for method in ('rsync_8k', 'rsync_default'):
            orchestrator.events.clear()
            self.run_method(orchestrator, method)
            argvs[method] = next(event[1] for event in orchestrator.events
                                 if isinstance(event, tuple) and event[0] == 'source'
                                 and Path(event[1][0]).name == 'rsync')
        for method, argv in argvs.items():
            with self.subTest(method=method):
                self.assertIn('--fsync', argv)
                self.assertIn('--inplace', argv)
                self.assertEqual('--block-size=8192' in argv, method == 'rsync_8k')
                self.assertTrue(argv[-1].endswith(f'/{method}/'))

    def test_other_methods_have_no_prep_phase(self):
        orchestrator = self.orchestrator()
        for method in ('full', 'incremental', 'pgdelta'):
            with self.subTest(method=method):
                with mock.patch.object(run_exp04, 'Child', FakeChild):
                    record = self.run_method(orchestrator, method)
                self.assertNotIn('prep_capture', record['phases'])
                self.assertIsNone(record.get('totals_in_place'))
                row = measure.summary_row(record)
                self.assertIsNone(row['inplace_total_s'])
                self.assertIsNone(row['prep_s'])
                self.assertIsNone(row['prep_capture_s'])
                # pgdelta's receiver starts beside its capture, so it has a serial bound too.
                self.assertEqual(record.get('totals_serial') is None, method != 'pgdelta')
                self.assertEqual(row['serial_total_s'] is None, method != 'pgdelta')
                self.assertEqual(row['receiver_capture_s'] is None, method != 'pgdelta')
                self.assertEqual(orchestrator.events.count('drop_caches'), 1)
                orchestrator.events.clear()

    # Finding 3: a second total without the remote-launch overhead.

    def test_total_without_remote_launch_overhead(self):
        orchestrator = self.orchestrator()
        record = self.run_method(orchestrator, 'rsync_8k')
        totals = record['totals']
        # The capture's launch overhead is hidden behind the longer prep; the
        # transfer's is not.
        self.assertEqual(totals['remote_overhead_seconds'], 0.06)
        self.assertEqual(totals['wall_seconds_less_remote_overhead'], 5.5)
        row = measure.summary_row(record)
        self.assertEqual(row['total_no_ssh_s'], 5.5)
        self.assertEqual(row['serial_total_no_ssh_s'], 6.5)
        self.assertEqual(row['inplace_total_no_ssh_s'], 3.0)
        self.assertEqual(row['ssh_overhead_s'], 0.06)

    # Finding 6: one rsync daemon log per method.

    def test_each_rsync_method_has_its_own_daemon_log(self):
        class RealDaemon(StubOrchestrator):
            start_rsyncd = run_exp04.Orchestrator.start_rsyncd
            stop_rsyncd = run_exp04.Orchestrator.stop_rsyncd

        orchestrator = self.orchestrator(RealDaemon)
        FakeChild.started.clear()
        listening = subprocess_result('LISTEN 0 5 10.0.0.13:8730 0.0.0.0:*\n')
        with mock.patch.object(run_exp04, 'Child', FakeChild), \
                mock.patch.object(run_exp04.subprocess, 'run', return_value=listening):
            for method in ('rsync_8k', 'rsync_default'):
                self.assertEqual(self.run_method(orchestrator, method)['status'], 'pass')
        daemons = [child for child in FakeChild.started if '--daemon' in child.argv]
        self.assertEqual(len(daemons), 2)
        logs = [child.log_path.name for child in daemons]
        self.assertEqual(len(set(logs)), 2, logs)
        for method, name in zip(('rsync_8k', 'rsync_default'), logs):
            self.assertIn(method, name)
        confs = [next(part for part in child.argv if part.startswith('--config=')) for child in
                 daemons]
        self.assertEqual(len(set(confs)), 2)

    # pgdelta and pgdelta_1thread each run an engine pinned by its flags (EXP-06 review):
    # EXP-06's choice, and EXP-02's single-threaded engine.

    def pgdelta_argvs(self, method):
        orchestrator = self.orchestrator(extra_args=['--methods', 'full,pgdelta_1thread'])
        FakeChild.started.clear()
        with mock.patch.object(run_exp04, 'Child', FakeChild):
            record = self.run_method(orchestrator, method)
        self.assertEqual(record['status'], 'pass', record.get('traceback'))
        receive = next(child.argv for child in FakeChild.started if 'receive' in child.argv)
        send = next(event[1] for event in orchestrator.events
                    if isinstance(event, tuple) and event[0] == 'source' and 'send' in event[1])
        return receive, send, record

    @staticmethod
    def engine_flags(argv):
        flags = {}
        for flag in ('--jobs', '--rebuild', '--verify', '--hash-backend', '--prefetch-basis'):
            if flag in argv:
                flags[flag] = argv[argv.index(flag) + 1]
            assert argv.count(flag) <= 1, argv
        return flags

    def test_pgdelta_1thread_runs_exp02s_single_threaded_engine(self):
        receive, send, record = self.pgdelta_argvs('pgdelta_1thread')
        self.assertEqual(self.engine_flags(receive),
                         {'--jobs': '1', '--rebuild': 'gap-fill', '--verify': 'sequential',
                          '--prefetch-basis': 'on'})
        self.assertEqual(self.engine_flags(send), {'--jobs': '1'})  # send builds nothing
        self.assertIn('pgdelta_1thread', record['paths']['output'])
        self.assertIn('pgdelta_1thread', record['paths']['stats'])

    def test_pgdelta_pins_exp06s_engine_instead_of_the_defaults(self):
        receive, send, _ = self.pgdelta_argvs('pgdelta')
        self.assertEqual(self.engine_flags(receive),
                         {'--jobs': '4', '--rebuild': 'copy-patch', '--verify': 'pipelined',
                          '--prefetch-basis': 'on'})
        self.assertEqual(self.engine_flags(send), {'--jobs': '4'})

    def test_the_pinned_flags_select_the_engines_they_name(self):
        # Parsed by pgdelta's own command line, so a renamed option or a new default shows here.
        from experiments.change_patterns import compare_methods
        from pgdelta import cli, receiver
        parser = cli.build_parser()
        helps = {command: parser._subparsers._group_actions[0].choices[command].format_help()
                 for command in ('send', 'receive')}
        for method, engine in measure.PGDELTA_ENGINES.items():
            with self.subTest(method=method):
                args = measure.PGDELTA_ARGS[method]
                required = measure.pgdelta_required_options([method])
                if not all(run_exp04.pgdelta_supports(helps[command], options)
                           for command, options in required.items()):
                    # T3's and T8a's --fsync, --in-place and --source-kind arrive with pgdelta's
                    # new modes; until then the orchestrator's check refuses these methods.
                    self.assertIn(method, (*measure.T3_METHODS, *measure.PGDELTA_DIRECT_METHODS))
                    self.skipTest(f'this pgdelta lacks options {method} pins: {required}')
                output = [] if engine.get('in_place') else ['--output', 'O']
                receive = parser.parse_args(['receive', '--basis', 'B', *output, '--stdio',
                                             *args['receive']])
                send = parser.parse_args(['send', '--source', 'S', '--connect', 'h:1', *args['send']])
                self.assertEqual((receive.jobs, receive.verify, receive.hash_backend,
                                  receive.prefetch_basis),
                                 (engine['jobs'], engine['verify'], 'process', engine['prefetch_basis']))
                if engine.get('rebuild'):
                    self.assertEqual(receive.rebuild, engine['rebuild'])
                self.assertEqual((send.jobs, send.hash_backend), (engine['jobs'], 'process'))
                if 'fsync' in engine:
                    self.assertEqual(receive.fsync, engine['fsync'])
                self.assertEqual(bool(getattr(receive, 'in_place', False)), bool(engine.get('in_place')))
                if 'source_kind' in engine:
                    self.assertEqual(send.source_kind, engine['source_kind'])
        # pgdelta_1thread is EXP-02's engine, the one compare_methods pins (its `local` receiver,
        # over stdio, has nothing to prefetch); EXP-04 adds only the listener's prefetch to it.
        engine = measure.PGDELTA_ENGINES['pgdelta_1thread']
        self.assertEqual(measure.pgdelta_engine_args(engine)['receive'], compare_methods.PGDELTA_ENGINE)
        self.assertEqual(measure.PGDELTA_ARGS['pgdelta_1thread']['receive'],
                         (*compare_methods.PGDELTA_ENGINE, '--prefetch-basis', 'on'))
        self.assertEqual({engine['prefetch_basis'] for engine in measure.PGDELTA_ENGINES.values()}, {'on'})
        # pgdelta is the command line's default on EXP-04's 8-vCPU VMs (send and receive on two hosts).
        default = measure.PGDELTA_ENGINES['pgdelta']
        self.assertEqual((cli.default_jobs(8), receiver.DEFAULT_REBUILD, receiver.DEFAULT_VERIFY),
                         (default['jobs'], default['rebuild'], default['verify']))

    def test_pgdelta_records_the_engine_it_ran(self):
        fetched = {'switch': 'on', 'ran': True, 'bytes': 189_000_000, 'basis_bytes': 189_000_000,
                   'seconds': 0.12, 'waited_seconds': 1.1, 'stopped': 'finished'}
        stats = {'bytes_source_to_dest': {'total': 10}, 'bytes_dest_to_source': {'total': 2},
                 'mode': 'reuse', 'jobs': 4, 'worker_start_method': 'spawn',
                 'switches': measure.pgdelta_switches(measure.PGDELTA_ENGINES['pgdelta']),
                 'receiver': {'worker_start_method': 'spawn', 'prefetch': fetched}}
        orchestrator = self.orchestrator()
        for switches, ok in ((stats['switches'], True),
                             (measure.pgdelta_switches(measure.PGDELTA_ENGINES['pgdelta_1thread']), False)):
            with self.subTest(ok=ok), mock.patch.object(
                    orchestrator.source, 'agent',
                    lambda *args, **kwargs: {'log_tail': json.dumps(dict(stats, switches=switches))}):
                reported = run_exp04.Orchestrator.tool_reported(
                    orchestrator, 'pgdelta', orchestrator.method_paths('pgdelta'), {})
            self.assertEqual(reported['switches'], switches)
            self.assertIs(reported['switches_ok'], ok)
            self.assertEqual(reported['flags'], {command: list(argv) for command, argv
                                                 in measure.PGDELTA_ARGS['pgdelta'].items()})
            self.assertEqual(reported['worker_start_method'], {'source': 'spawn', 'dest': 'spawn'})
            self.assertEqual(reported['prefetch'], fetched)
            self.assertIs(reported['prefetch_ok'], True)
            row = measure.summary_row({'method': 'pgdelta', 'tool_reported': reported})
            self.assertEqual((row['prefetch_bytes'], row['prefetch_basis_bytes'], row['prefetch_s'],
                              row['prefetch_wait_s'], row['prefetch_stopped']),
                             (189_000_000, 189_000_000, 0.12, 1.1, 'finished'))

    def test_pgdelta_1thread_is_optional(self):
        self.assertNotIn('pgdelta_1thread', measure.METHODS)
        self.assertIn('pgdelta_1thread', measure.ALL_METHODS)
        self.assertIn('pgdelta_1thread', measure.STAGED_METHODS)
        args = run_exp04.parse_args(['--source-ip', 'x', '--out', 'y'])
        self.assertNotIn('pgdelta_1thread', args.methods.split(','))

    def test_engine_options_are_checked_from_help_text(self):
        self.assertTrue(run_exp04.pgdelta_supports('  --jobs N   hashing threads\n'))
        self.assertFalse(run_exp04.pgdelta_supports('  --block BLOCK  page size\n'))
        receive = ('  --jobs N  workers\n  --rebuild {copy-patch,gap-fill}\n  --verify {pipelined,sequential}\n'
                   '  --prefetch-basis {on,off}\n')
        self.assertTrue(run_exp04.pgdelta_supports(receive, measure.PGDELTA_OPTIONS['receive']))
        self.assertFalse(run_exp04.pgdelta_supports('  --jobs N  workers\n', measure.PGDELTA_OPTIONS['receive']))
        self.assertEqual(measure.PGDELTA_OPTIONS, {'send': ('--jobs',),
                                                   'receive': ('--jobs', '--rebuild', '--verify',
                                                               '--prefetch-basis')})
        # The real pgdelta has them.
        from pgdelta import cli
        for command, options in measure.PGDELTA_OPTIONS.items():
            parser = cli.build_parser()._subparsers._group_actions[0].choices[command]
            self.assertTrue(run_exp04.pgdelta_supports(parser.format_help(), options), command)

    # pgdelta's receiver starts beside its capture, as rsync's prep does, and prefetches B0
    # while it waits (EXP-04 decision note on pgdelta's receiver).

    def pgdelta_run(self, method='pgdelta', child=None, orchestrator=None):
        orchestrator = orchestrator or self.orchestrator(extra_args=['--methods', 'pgdelta,pgdelta_1thread'])
        FakeChild.started.clear()
        with mock.patch.object(run_exp04, 'Child', child or FakeChild):
            record = self.run_method(orchestrator, method)
        return orchestrator, record

    def test_pgdelta_receiver_starts_beside_the_capture_after_one_cache_drop(self):
        clock = self.clock

        class SlowStart(FakeChild):
            # The receiver takes 1.5 s to listen (Python start-up, pg_controldata, scan of B0).
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                clock.now += 1.5

        # Every staged pgdelta method, T3's included (pgdelta_direct* has no capture; see its tests).
        for method in measure.PGDELTA_STAGED_METHODS:
            with self.subTest(method=method):
                orchestrator, record = self.pgdelta_run(method, SlowStart)
                self.assertEqual(record['status'], 'pass', record.get('traceback'))
                self.assertEqual(list(record['phases']), ['receiver_capture', 'transfer'])
                pair = record['phases']['receiver_capture']
                self.assertEqual(pair['parts']['receiver'],
                                 {**pair['parts']['receiver'], 'host': 'dest', 'wall_seconds': 1.5,
                                  'offset_seconds': 0.0})
                self.assertEqual(pair['parts']['capture'],
                                 {**pair['parts']['capture'], 'host': 'source', 'wall_seconds': 1.06,
                                  'offset_seconds': 0.0})
                receiver = FakeChild.started[0]
                self.assertEqual(pair['parts']['receiver']['argv'], receiver.argv)
                self.assertIn('--listen', receiver.argv)
                self.assertEqual(receiver.argv[receiver.argv.index('--prefetch-basis') + 1], 'on')
                # The capture's commands are in the pair; the receiver's record (its whole life) is
                # in the transfer, once it has exited.
                self.assertEqual([command['part'] for command in pair['commands']], ['capture'])
                transfer = record['phases']['transfer']['commands']
                self.assertEqual([command['host'] for command in transfer], ['dest', 'source'])
                self.assertIn('receive', transfer[0]['argv'])
                self.assertIn('receiver_capture', transfer[0]['role'])
                # One drop, before both parts; the sender only after the receiver listened.
                kinds = self.event_kinds(orchestrator)
                self.assertEqual(kinds.count('drop_caches'), 1)
                self.assertLess(kinds.index('drop_caches'), kinds.index(('source', 'pg_basebackup')))
                sends = [event for event in orchestrator.events
                         if isinstance(event, tuple) and event[0] == 'source' and 'send' in event[1]]
                self.assertEqual(len(sends), 1)
                # max(receiver start, capture) + transfer; the serial bound adds the two parts.
                row = measure.summary_row(record)
                self.assertEqual((row['receiver_start_s'], row['capture_s'], row['receiver_capture_s'],
                                  row['transfer_s']), (1.5, 1.06, 1.5, 2.06))
                self.assertEqual(row['total_s'], 3.56)
                self.assertEqual(row['serial_total_s'], 4.62)
                self.assertIsNone(row['inplace_total_s'])
                self.assertIsNone(row['prep_capture_s'])
                self.assertEqual(row['counter_windows'], 2)
                self.assertEqual(row['receiver_capture_cpu_dest_s'], 1.0)
                # The capture's launch overhead is hidden behind the longer receiver start-up.
                self.assertEqual(row['ssh_overhead_s'], 0.06)

    def test_a_receiver_quicker_than_the_capture_adds_no_time(self):
        _, record = self.pgdelta_run()
        row = measure.summary_row(record)
        self.assertEqual((row['receiver_start_s'], row['receiver_capture_s']), (0.0, 1.06))
        self.assertEqual(row['total_s'], 3.12)
        self.assertEqual(row['serial_total_s'], 3.12)
        self.assertEqual(row['ssh_overhead_s'], 0.12)
        self.assertEqual(row['total_no_ssh_s'], 3.0)

    def test_pgdelta_records_the_prefetch_read_between_its_timed_phases(self):
        # The receiver keeps reading B0 after receiver_capture ends, while the counters are read
        # (untimed), until the sender connects in the transfer. That part is measured per run.
        clock = self.clock

        class Spilling(StubOrchestrator):
            def snapshot_source(self):
                clock.now += 0.06  # an SSH call to the source agent
                return super().snapshot_source()

            def tool_reported(self, method, paths, phases):
                pair = phases['receiver_capture']
                start, end = pair['started_utc'] + 0.2, pair['ended_utc'] + 0.05
                return {'bytes_source_to_dest': 1, 'bytes_dest_to_source': 1,
                        'prefetch': {'ran': True, 'bytes': 91_000_000, 'seconds': round(end - start, 6),
                                     'started_utc': start, 'ended_utc': end, 'stopped': 'finished'}}

        with mock.patch.object(run_exp04.time, 'time', lambda: clock.now):
            _, record = self.pgdelta_run(orchestrator=self.orchestrator(Spilling))
        self.assertEqual(record['status'], 'pass', record.get('traceback'))
        pair, transfer = record['phases']['receiver_capture'], record['phases']['transfer']
        self.assertAlmostEqual(pair['ended_utc'], pair['started_utc'] + 1.06)
        # Between them: the pair's "after" snapshots and the transfer's "before" ones, 0.12 s.
        self.assertAlmostEqual(transfer['started_utc'] - pair['ended_utc'], 0.12)
        self.assertEqual(record['prefetch_untimed'],
                         {'gap_seconds': 0.12, 'end_after_phase_seconds': 0.05, 'untimed_seconds': 0.05,
                          'untimed_bytes_estimate': 5_000_000, 'spilled': True})
        row = measure.summary_row(record)
        self.assertEqual((row['untimed_gap_s'], row['prefetch_untimed_s'], row['prefetch_untimed_bytes_est']),
                         (0.12, 0.05, 5_000_000))
        # Other methods have none.
        _, full = self.pgdelta_run('pgdelta_1thread')
        self.assertIsNone(full['prefetch_untimed'])  # the stub reports no prefetch statistics
        self.assertIsNone(measure.summary_row({'method': 'rsync_8k'})['prefetch_untimed_s'])

    def test_receiver_and_capture_overlap(self):
        # Each part waits for the other to start: run one after the other, the barrier breaks.
        barrier = threading.Barrier(2, timeout=5)

        class Overlapping(StubOrchestrator):
            concurrently = run_exp04.Orchestrator.concurrently

            def remote_command(self, argv, log_name, **kwargs):
                if Path(str(argv[0])).name == 'pg_basebackup':
                    barrier.wait()
                return super().remote_command(argv, log_name, **kwargs)

        class Waiting(FakeChild):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                barrier.wait()

        _, record = self.pgdelta_run(child=Waiting, orchestrator=self.orchestrator(Overlapping))
        self.assertEqual(record['status'], 'pass', record.get('traceback'))
        self.assertEqual(set(record['phases']['receiver_capture']['parts']), {'receiver', 'capture'})

    def test_a_failed_capture_stops_the_waiting_receiver(self):
        class FailingCapture(StubOrchestrator):
            def remote_command(self, argv, log_name, **kwargs):
                result = super().remote_command(argv, log_name, **kwargs)
                if Path(str(argv[0])).name == 'pg_basebackup':
                    result['exit_code'] = 1
                return result

        orchestrator, record = self.pgdelta_run(orchestrator=self.orchestrator(FailingCapture))
        self.assertEqual(record['status'], 'error')
        self.assertEqual(list(record['phases']), ['receiver_capture'])
        receiver = FakeChild.started[0]
        self.assertTrue(receiver.stopped)
        stopped = [command for command in record['phases']['receiver_capture']['commands']
                   if command.get('part') == 'receiver']
        self.assertEqual(len(stopped), 1)
        self.assertIn('capture failed', stopped[0]['role'])
        # No sender was started.
        self.assertFalse(any(isinstance(event, tuple) and event[0] == 'source' and 'send' in event[1]
                             for event in orchestrator.events))

    def test_an_exception_during_the_capture_stops_the_receiver(self):
        started = threading.Event()

        class Broken(StubOrchestrator):
            concurrently = run_exp04.Orchestrator.concurrently  # the receiver starts on this thread

            def remote_command(self, argv, log_name, **kwargs):
                if Path(str(argv[0])).name == 'pg_basebackup':
                    started.wait(5)  # fail once the receiver is up
                    raise run_exp04.CommandError('ssh: connection lost')
                return super().remote_command(argv, log_name, **kwargs)

        class Started(FakeChild):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                started.set()

        _, record = self.pgdelta_run(child=Started, orchestrator=self.orchestrator(Broken))
        self.assertEqual(record['status'], 'error')
        self.assertIn('connection lost', record['error'])
        self.assertTrue(FakeChild.started[0].stopped)

    def test_an_exception_during_the_transfer_stops_the_receiver(self):
        class Broken(StubOrchestrator):
            def remote_command(self, argv, log_name, **kwargs):
                if 'send' in [str(part) for part in argv]:
                    raise run_exp04.CommandError('ssh: connection lost')
                return super().remote_command(argv, log_name, **kwargs)

        _, record = self.pgdelta_run(orchestrator=self.orchestrator(Broken))
        self.assertEqual(record['status'], 'error')
        self.assertTrue(FakeChild.started[0].stopped)

    def test_a_receiver_that_exits_before_listening_fails_the_run(self):
        class Refusing(FakeChild):
            LOG = 'pgdelta: error: output O already exists\n'
            EXIT = 2

        orchestrator, record = self.pgdelta_run(child=Refusing)
        self.assertEqual(record['status'], 'error')
        self.assertIn('exited 2', record['error'])
        self.assertEqual(list(record['phases']), ['receiver_capture'])
        self.assertFalse(any(isinstance(event, tuple) and event[0] == 'source' and 'send' in event[1]
                             for event in orchestrator.events))

    # The last review: both() waits for the source-side call whatever interrupts it.
    # The interrupts are real SIGTERMs to the main thread, handled by run_exp04._terminate as
    # in a run. (_thread.interrupt_main only sets Python's pending-signal flag: it does not
    # break the main thread's blocking wait in future.result(), so the KeyboardInterrupt would
    # come only once the source side had finished, whichever both() was in place.)

    def sigterms_to_main_thread(self, *delays):
        """SIGTERM the main thread after each delay, with run_exp04's handler installed."""
        self.assertIs(threading.current_thread(), threading.main_thread())
        previous = signal.signal(signal.SIGTERM, run_exp04._terminate)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        main = threading.main_thread().ident
        timers = [threading.Timer(delay, signal.pthread_kill, (main, signal.SIGTERM)) for delay in delays]
        for timer in timers:
            timer.start()

        def stop():
            for timer in timers:
                timer.cancel()
                timer.join()
        self.addCleanup(stop)

    def test_both_waits_for_the_source_side_when_interrupted_while_awaiting_it(self):
        orchestrator = self.orchestrator()
        finished = threading.Event()

        def remote():
            time.sleep(0.6)
            finished.set()
            return 'source'

        # Twice while it waits for the source side, after the local side has returned.
        self.sigterms_to_main_thread(0.1, 0.3)
        started = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            run_exp04.Orchestrator.both(orchestrator, lambda: 'dest', remote)
        self.assertTrue(finished.is_set(), f'raised after {time.monotonic() - started:.3f} s, '
                                           'before the source side had finished')

    def test_both_waits_for_the_source_side_when_the_local_side_is_interrupted(self):
        orchestrator = self.orchestrator()
        finished = threading.Event()

        def remote():
            time.sleep(0.6)
            finished.set()

        def local():
            raise KeyboardInterrupt('the first signal')

        # The local side is interrupted at once; a second SIGTERM arrives while both() waits.
        self.sigterms_to_main_thread(0.2)
        with self.assertRaises(KeyboardInterrupt) as raised:
            run_exp04.Orchestrator.both(orchestrator, local, remote)
        self.assertTrue(finished.is_set())
        self.assertEqual(str(raised.exception), 'the first signal')

    def test_both_raises_the_source_sides_error_after_the_local_side(self):
        orchestrator = self.orchestrator()
        done = []

        def remote():
            raise run_exp04.CommandError('source failed')

        def local():
            time.sleep(0.1)
            done.append('local')

        with self.assertRaises(run_exp04.CommandError):
            run_exp04.Orchestrator.both(orchestrator, local, remote)
        self.assertEqual(done, ['local'])

    # Finding 5: speed order shuffled per repeat and recorded.

    def test_speed_order_is_shuffled_per_repeat_and_recorded(self):
        class Trials(StubOrchestrator):
            def throttle(self, rate):
                return {}

            def set_speed(self, speed):
                return {'speed': speed, 'source_to_dest_gbit': 1.0, 'dest_to_source_gbit': 1.0}

            def qdisc_stats(self):
                return {}

            def take_b0(self, trial):
                self.b0_digest = {'entries': {}, 'device': 1}

            def cleanup_trial(self, trial):
                pass

            def run_method(self, trial, repeat, speed, link, method, order_index, order):
                record = {'status': 'pass', 'speed_index': link.get('speed_index'),
                          'speed_order': link.get('speed_order')}
                self.records.append(record)
                return record

        speeds = '0.1,0.25,0.5,1,2,5,10,unthrottled'
        orchestrator = self.orchestrator(Trials, ['--repeats', '4', '--speeds', speeds,
                                                  '--methods', 'full'])
        orchestrator.speeds = measure.parse_speeds(speeds)
        orchestrator.methods = ['full']
        orchestrator.source.script = lambda *args, **kwargs: {
            'spec': {'accounts_rows': 500000, 'events_rows': 500000}}
        with mock.patch.object(run_exp04.measure, 'tree_digest',
                               return_value={'entries': {}, 'device': 1}):
            trial = orchestrator.run_trial(0, 'uniform', 0.01)
        orders = trial['speed_orders']
        ascending = [speed['label'] for speed in orchestrator.speeds]
        self.assertEqual(len(orders), 4)
        for order in orders:
            self.assertEqual(sorted(order), sorted(ascending))
        self.assertTrue(any(order != ascending for order in orders), orders)
        self.assertGreater(len({tuple(order) for order in orders}), 1)
        self.assertEqual(orchestrator.run['speed_orders'][trial['trial_id']], orders)
        # Each run record carries its repeat's order and its position in it.
        self.assertEqual(orchestrator.records[0]['speed_order'], orders[0])
        self.assertEqual([r['speed_index'] for r in orchestrator.records[:8]], list(range(8)))
        # Seeded: the same seed gives the same orders.
        again = self.orchestrator(Trials, ['--repeats', '4', '--speeds', speeds,
                                           '--methods', 'full'])
        again.speeds, again.methods = measure.parse_speeds(speeds), ['full']
        again.source.script = orchestrator.source.script
        with mock.patch.object(run_exp04.measure, 'tree_digest',
                               return_value={'entries': {}, 'device': 1}):
            self.assertEqual(again.run_trial(0, 'uniform', 0.01)['speed_orders'], orders)


def subprocess_result(stdout):
    return mock.Mock(stdout=stdout, returncode=0)


class FakeChild:
    """run_exp04.Child stand-in: records how it was started, 'runs' instantly.

    LOG is what its log says at once (a pgdelta receiver that listens). With
    EXIT set, it has already exited with that code when first polled.
    """

    started: list = []
    LOG = 'listening on 10.0.0.13:7000\n'
    EXIT = None

    def __init__(self, argv, log_path, *, cwd=None, env=None, gnu_time=None):
        self.argv = [str(part) for part in argv]
        self.log_path = Path(log_path)
        self.gnu_time = gnu_time
        self.result = None
        self.signals = []
        self.stopped = False
        FakeChild.started.append(self)

    def _record(self, code):
        self.result = {'host': 'dest', 'argv': self.argv, 'exit_code': code, 'wall_seconds': 1.0,
                       'rusage': {'cpu_seconds': 0.1}, 'peak_rss_kb': 1000, 'log': str(self.log_path)}
        return self.result

    def poll(self):
        if self.result is None and self.EXIT is not None:
            self._record(self.EXIT)
        return self.result

    def log_text(self):
        return self.LOG

    def signal(self, signum=None):
        self.signals.append(signum)

    def stop(self):
        self.stopped = True
        if self.result is None:
            self._record(-15)

    def wait(self, timeout):
        return self.result or self._record(0)


# --- finding 2: each tool's own peak memory ------------------------------------

class PeakMemory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.time = stub_gnu_time(self.tmp.name)

    def test_parse_gnu_time_output(self):
        text = ('Command exited with non-zero status 3\n'
                'EXP04-TIME maxrss_kb=1144 wall_seconds=0.00 user_seconds=0.01 '
                'system_seconds=0.00 exit=3\n')
        self.assertEqual(measure.parse_gnu_time(text),
                         {'maxrss_kb': 1144, 'wall_seconds': 0.0, 'user_seconds': 0.01,
                          'system_seconds': 0.0, 'exit_code': 3})
        self.assertIsNone(measure.parse_gnu_time('Command terminated by signal 9\n'))
        self.assertIsNone(measure.parse_gnu_time(''))

    def test_gnu_time_argv(self):
        argv = measure.gnu_time_argv('/usr/bin/time', '/x/log.time', ['pg_basebackup', '-D', 'o'])
        self.assertEqual(argv[:5], ['/usr/bin/time', '-f', measure.GNU_TIME_FORMAT, '-o',
                                    '/x/log.time'])
        self.assertEqual(argv[5:], ['pg_basebackup', '-D', 'o'])
        self.assertIn('%M', measure.GNU_TIME_FORMAT)

    def test_child_reports_the_tools_peak_from_gnu_time(self):
        child = run_exp04.Child(['sh', '-c', 'echo hello; exit 2'],
                                Path(self.tmp.name) / 'logs' / 'x.log', gnu_time=self.time)
        result = child.wait(60)
        self.assertEqual(result['exit_code'], 2)
        self.assertEqual(result['argv'], ['sh', '-c', 'echo hello; exit 2'])
        self.assertEqual(result['peak_rss_kb'], 4321)
        self.assertEqual(result['gnu_time']['exit_code'], 2)
        self.assertIn('hello', child.log_text())
        self.assertNotIn('EXP04-TIME', child.log_text())

    def test_child_without_gnu_time_reports_no_peak(self):
        result = run_exp04.Child(['true'], Path(self.tmp.name) / 'y.log').wait(60)
        self.assertIsNone(result['peak_rss_kb'])
        self.assertNotIn('maxrss_kb', result['rusage'])

    def test_agent_run_reports_the_tools_peak_from_gnu_time(self):
        result = agent.cmd_run(Namespace(
            log=str(Path(self.tmp.name) / 'logs' / 'r.log'), cwd=None, env=None,
            gnu_time=self.time, argv=['--', 'sh', '-c', 'echo out']))
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['peak_rss_kb'], 4321)
        self.assertEqual(result['argv'], ['sh', '-c', 'echo out'])
        self.assertIn('out', result['log_tail'])

    def test_phase_totals_take_peak_from_gnu_time_not_wait4(self):
        commands = [{'host': 'dest', 'exit_code': 0, 'wall_seconds': 1.0,
                     'rusage': {'cpu_seconds': 0.5, 'wait4_maxrss_kb': 420644},
                     'peak_rss_kb': 1144},
                    {'host': 'dest', 'exit_code': 0, 'wall_seconds': 1.0,
                     'rusage': {'cpu_seconds': 0.25, 'wait4_maxrss_kb': 420644},
                     'peak_rss_kb': 8200}]
        phases = {'transfer': phase('transfer', 2.0, s_tx=0, s_rx=0, d_tx=0, d_rx=0, s_cpu=0,
                                    d_cpu=1, commands=commands)}
        totals = measure.phase_totals(phases)
        self.assertEqual(totals['peak_rss_kb_dest'], 8200)
        self.assertEqual(totals['process_cpu_seconds_dest'], 0.75)
        commands[0]['peak_rss_kb'] = None  # unknown for one command: the peak is unknown
        self.assertIsNone(measure.phase_totals(phases)['peak_rss_kb_dest'])

    def test_group_members_from_proc(self):
        proc = Path(self.tmp.name) / 'proc'
        for pid, pgrp in ((10, 10), (11, 10), (12, 99)):
            (proc / str(pid)).mkdir(parents=True)
            (proc / str(pid) / 'stat').write_text(f'{pid} (a b) S 1 {pgrp} {pgrp} 0 -1\n')
        (proc / 'self').mkdir()
        self.assertEqual(sorted(run_exp04.group_members(10, proc)), [10, 11])
        self.assertIsNone(run_exp04.group_members(10, Path(self.tmp.name) / 'no-proc'))

    def test_proc_scope_marks_per_process_columns(self):
        for method in measure.ALL_METHODS:
            with self.subTest(method=method):
                self.assertIn('walsender', measure.PROC_SCOPE[method])
                # %M is the largest single process, not the tree's sum.
                self.assertIn('largest single process', measure.PROC_SCOPE[method])
        # pgdelta at --jobs 4 is not one process: its spawned workers are children it reaps, so
        # wait4's and GNU time's CPU include them, while %M is the largest single process.
        scope = measure.PROC_SCOPE['pgdelta']
        for words in ('not one process', '4 worker processes', 'spawn', 'reaps', 'wait4', "GNU time's user",
                      'largest single process'):
            self.assertIn(words, scope)
        self.assertNotIn('is one process', scope)
        self.assertIn('pgdelta at --jobs 4 is not one process', measure.PEAK_RSS_SCOPE)
        self.assertIn('no worker processes', measure.PROC_SCOPE['pgdelta_1thread'])
        self.assertIn('not summed', measure.PEAK_RSS_SCOPE)
        for tool in ('pg_basebackup', 'generator', 'receiver'):
            self.assertIn(tool, measure.PEAK_RSS_SCOPE)


# --- round 2, finding 6: signals spare GNU time --------------------------------

class ChildSignals(unittest.TestCase):
    """Child.signal: everything but SIGKILL goes to the group minus GNU time itself."""

    LAUNCHER, TOOL, GRANDCHILD = 4242, 4243, 4244

    def child(self, *, gnu_time=True, result=None):
        child = run_exp04.Child.__new__(run_exp04.Child)
        child.process = mock.Mock(pid=self.LAUNCHER)
        child.time_path = Path('/nonexistent/x.log.time') if gnu_time else None
        child.result = result
        return child

    def send(self, child, signum, members=(LAUNCHER, TOOL, GRANDCHILD), kill_effect=None):
        with mock.patch.object(run_exp04, 'group_members',
                               return_value=None if members is None else list(members)) as found, \
                mock.patch.object(run_exp04.os, 'kill', side_effect=kill_effect) as kill, \
                mock.patch.object(run_exp04.os, 'killpg') as killpg:
            child.signal(signum)
        return found, kill, killpg

    def test_sigterm_reaches_the_tool_and_not_gnu_time(self):
        found, kill, killpg = self.send(self.child(), signal.SIGTERM)
        found.assert_called_once_with(self.LAUNCHER)
        self.assertEqual(kill.call_args_list, [mock.call(self.TOOL, signal.SIGTERM),
                                               mock.call(self.GRANDCHILD, signal.SIGTERM)])
        killpg.assert_not_called()

    def test_a_member_that_already_exited_is_skipped(self):
        _, kill, killpg = self.send(self.child(), signal.SIGINT,
                                    kill_effect=[ProcessLookupError, None])
        self.assertEqual([call.args for call in kill.call_args_list],
                         [(self.TOOL, signal.SIGINT), (self.GRANDCHILD, signal.SIGINT)])
        killpg.assert_not_called()

    def test_sigkill_goes_to_the_whole_group(self):
        found, kill, killpg = self.send(self.child(), signal.SIGKILL)
        found.assert_not_called()
        kill.assert_not_called()
        killpg.assert_called_once_with(self.LAUNCHER, signal.SIGKILL)

    def test_without_gnu_time_or_proc_the_group_is_signalled(self):
        for child, members in ((self.child(gnu_time=False), (self.LAUNCHER, self.TOOL)),
                               (self.child(), None)):
            with self.subTest(gnu_time=child.time_path is not None):
                _, kill, killpg = self.send(child, signal.SIGTERM, members=members)
                kill.assert_not_called()
                killpg.assert_called_once_with(self.LAUNCHER, signal.SIGTERM)

    def test_a_finished_child_is_not_signalled(self):
        found, kill, killpg = self.send(self.child(result={'exit_code': 0}), signal.SIGTERM)
        found.assert_not_called()
        kill.assert_not_called()
        killpg.assert_not_called()

    @unittest.skipUnless(Path('/proc/self/stat').exists(), 'needs Linux /proc')
    def test_sigterm_leaves_gnu_time_to_write_its_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            child = run_exp04.Child(['sh', '-c', 'echo started; exec sleep 30'],
                                    Path(tmp) / 'tool.log', gnu_time=stub_gnu_time(tmp))
            deadline = time.monotonic() + 20
            while 'started' not in child.log_text():
                self.assertLess(time.monotonic(), deadline, 'the tool did not start')
                time.sleep(0.01)
            child.signal(signal.SIGTERM)
            result = child.wait(30)
            self.assertTrue(child.time_path.exists())
            self.assertEqual(result['peak_rss_kb'], 4321)
            self.assertFalse(result['timed_out'])


class PrefetchOutsideTimers(unittest.TestCase):
    """measure.prefetch_untimed: pgdelta's prefetch of B0 between its two timed phases.

    receiver_capture runs from 100.0 to 101.0 (UTC seconds) and the transfer starts at 101.115,
    after the counters were read in between, untimed: a gap of 0.115 s.
    """

    PHASES = {'receiver_capture': {'started_utc': 100.0, 'wall_seconds': 1.0, 'ended_utc': 101.0},
              'transfer': {'started_utc': 101.115, 'wall_seconds': 2.0, 'ended_utc': 103.115}}

    @staticmethod
    def prefetch(start, end, read=200_000_000, stopped='finished'):
        return {'ran': True, 'bytes': read, 'seconds': round(end - start, 6), 'started_utc': start,
                'ended_utc': end, 'stopped': stopped}

    def test_a_prefetch_that_ends_inside_the_phase_reads_nothing_untimed(self):
        result = measure.prefetch_untimed(self.PHASES, self.prefetch(100.15, 100.41))
        self.assertEqual(result, {'gap_seconds': 0.115, 'end_after_phase_seconds': -0.59,
                                  'untimed_seconds': 0.0, 'untimed_bytes_estimate': 0, 'spilled': False})

    def test_a_prefetch_that_ends_in_the_gap_spills_into_it(self):
        # 2.0 s of reading at 100 MB/s; 0.05 s of it after receiver_capture ended.
        result = measure.prefetch_untimed(self.PHASES, self.prefetch(99.05, 101.05))
        self.assertEqual(result['untimed_seconds'], 0.05)
        self.assertEqual(result['end_after_phase_seconds'], 0.05)
        self.assertEqual(result['untimed_bytes_estimate'], 5_000_000)
        self.assertIs(result['spilled'], True)

    def test_a_prefetch_still_reading_when_the_sender_connects_spills_the_whole_gap(self):
        result = measure.prefetch_untimed(self.PHASES, self.prefetch(100.2, 101.4, read=120_000_000,
                                                                     stopped='connected'))
        self.assertEqual(result['untimed_seconds'], 0.115)
        self.assertEqual(result['untimed_bytes_estimate'], 11_500_000)
        self.assertEqual(result['gap_seconds'], 0.115)

    def test_records_without_an_end_time_use_the_start_and_wall(self):
        phases = {name: {key: value for key, value in phase.items() if key != 'ended_utc'}
                  for name, phase in self.PHASES.items()}
        self.assertEqual(measure.prefetch_untimed(phases, self.prefetch(99.05, 101.05))['untimed_seconds'],
                         0.05)

    def test_unknown_without_both_phases_or_the_prefetch_times(self):
        self.assertIsNone(measure.prefetch_untimed({'transfer': self.PHASES['transfer']},
                                                   self.prefetch(100.15, 100.41)))
        self.assertIsNone(measure.prefetch_untimed(self.PHASES, None))
        self.assertIsNone(measure.prefetch_untimed(self.PHASES, {'ran': True, 'bytes': 5, 'seconds': 0.1}))
        self.assertIsNone(measure.prefetch_untimed(self.PHASES, prefetch_not_run()))


def prefetch_not_run():
    from pgdelta import prefetch
    return prefetch.not_run('off', 'switched off')


class ConcurrentPhases(unittest.TestCase):
    """measure.phase_overhead and the views of a concurrent phase."""

    def pair(self, prep_wall, capture_wall, capture_inner):
        commands = [{**command('dest', 0.1, 2300, prep_wall), 'part': 'prep'},
                    {**command('source', 0.3, 8700, capture_wall, capture_inner),
                     'part': 'capture'}]
        pair = phase('prep_capture', max(prep_wall, capture_wall), s_tx=2500, s_rx=1700,
                     d_tx=950, d_rx=1240, s_cpu=1.0, d_cpu=0.3, commands=commands)
        pair['parts'] = {'prep': {'host': 'dest', 'wall_seconds': prep_wall},
                         'capture': {'host': 'source', 'wall_seconds': capture_wall}}
        return pair

    def test_only_the_critical_paths_launch_overhead_counts(self):
        # Capture longer: its whole 0.06 s launch is on the critical path.
        self.assertAlmostEqual(measure.phase_overhead(self.pair(0.5, 1.06, 1.0)), 0.06)
        # The prep ends 0.03 s before the capture: only 0.03 s of it is.
        self.assertAlmostEqual(measure.phase_overhead(self.pair(1.03, 1.06, 1.0)), 0.03)
        # Prep longer: the launch is hidden.
        self.assertEqual(measure.phase_overhead(self.pair(3.5, 1.06, 1.0)), 0.0)

    def test_views_of_a_concurrent_phase(self):
        transfer = phase('transfer', 2.0, s_tx=58_000_000, s_rx=300_000, d_tx=310_000,
                         d_rx=58_100_000, s_cpu=0.6, d_cpu=0.25,
                         commands=[command('source', 0.5, 7600, 2.06, 2.0)])
        phases = {'prep_capture': self.pair(0.5, 1.06, 1.0), 'transfer': transfer}
        totals = measure.phase_totals(phases)
        self.assertEqual(totals['wall_seconds'], 3.06)
        self.assertEqual(totals['remote_overhead_seconds'], 0.12)
        self.assertEqual(totals['cpu_seconds_source'], 1.6)
        self.assertEqual(totals['cpu_seconds_dest'], 0.55)
        self.assertEqual(totals['peak_rss_kb_source'], 8700)
        serial = measure.serial_totals(phases)
        self.assertEqual(serial, {'wall_seconds': 3.56, 'remote_overhead_seconds': 0.12,
                                  'wall_seconds_less_remote_overhead': 3.44})
        in_place = measure.phase_totals(measure.without_part(phases, 'prep'))
        self.assertEqual(in_place['wall_seconds'], 3.06)
        self.assertEqual(in_place['wire_bytes_source_to_dest'], 58_002_500)
        self.assertEqual(in_place['cpu_seconds_source'], 1.6)
        self.assertIsNone(in_place['cpu_seconds_dest'])
        self.assertEqual(in_place['process_cpu_seconds_dest'], None)
        # Nothing to leave out: unchanged.
        self.assertEqual(measure.without_part({'transfer': transfer}, 'prep'),
                         {'transfer': transfer})


# --- finding 4: WAL bytes of each capture ----------------------------------------

MANIFEST = json.dumps({'PostgreSQL-Backup-Manifest-Version': 2, 'Files': [],
                       'WAL-Ranges': [{'Timeline': 1, 'Start-LSN': '0/5000028',
                                       'End-LSN': '0/5000158'}]})


class Wal(unittest.TestCase):
    def test_parse_lsn(self):
        self.assertEqual(measure.parse_lsn('0/5000028'), 0x5000028)
        self.assertEqual(measure.parse_lsn('1/0'), 1 << 32)

    def test_summary_of_a_staged_capture(self):
        segment = 16 << 20
        files = {'pg_wal/000000010000000000000005': segment,
                 'pg_wal/archive_status/000000010000000000000005.done': 0}
        wal = measure.wal_summary(files, MANIFEST)
        self.assertEqual(wal['wal_bytes'], segment)
        self.assertEqual(wal['wal_segments'], 1)
        self.assertEqual(wal['wal_stream_bytes'], 0x158)
        self.assertEqual(wal['wal_padding_bytes'], segment - 0x158)
        self.assertEqual(wal['wal_ranges'], [{'timeline': 1, 'start_lsn': '0/5000028',
                                              'end_lsn': '0/5000158'}])
        unknown = measure.wal_summary(files, None)
        self.assertIsNone(unknown['wal_stream_bytes'])
        self.assertIsNone(unknown['wal_padding_bytes'])

    def test_wal_files_from_digest_and_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'pg_wal' / 'archive_status').mkdir(parents=True)
            (root / 'pg_wal' / '000000010000000000000005').write_bytes(b'\0' * 1000)
            (root / 'base').mkdir()
            (root / 'base' / '1').write_bytes(b'x' * 50)
            expected = {'pg_wal/000000010000000000000005': 1000}
            self.assertEqual(measure.wal_files(root), expected)
            self.assertEqual(measure.wal_files_from_digest(measure.tree_digest(root)), expected)

    def test_summary_row_has_wal_columns_and_full_reference(self):
        record = Results().record()
        record['wal'] = measure.wal_summary({'pg_wal/000000010000000000000005': 16 << 20},
                                            MANIFEST)
        record['output_minus_wal_padding_bytes'] = 189_244_280 - record['wal'][
            'wal_padding_bytes']
        row = measure.summary_row(record)
        self.assertEqual(row['wal_bytes'], 16 << 20)
        self.assertEqual(row['wal_stream_bytes'], 0x158)
        self.assertEqual(row['wal_padding_bytes'], (16 << 20) - 0x158)
        self.assertEqual(row['output_minus_wal_padding_bytes'],
                         189_244_280 - (16 << 20) + 0x158)


# --- report: end-to-end vs in place, with and without the SSH launch overhead ---

class ReportTotals(unittest.TestCase):
    def rows(self):
        def row(speed, gbit, method, total, no_ssh, inplace=None, inplace_no_ssh=None,
                serial=None, serial_no_ssh=None):
            return {'trial_id': 't0', 'speed': speed, 'target_gbit': gbit, 'method': method,
                    'total_s': total, 'total_no_ssh_s': no_ssh, 'inplace_total_s': inplace,
                    'inplace_total_no_ssh_s': inplace_no_ssh, 'serial_total_s': serial,
                    'serial_total_no_ssh_s': serial_no_ssh, 'status': 'pass',
                    'measured_s2d_gbit': None, 'error': ''}
        return [row('0.5g', 0.5, 'full', 10.0, 10.0),
                row('0.5g', 0.5, 'rsync_8k', 11.0, 10.88, 8.0, 7.88, 12.0, 11.88),
                row('0.5g', 0.5, 'pgdelta', 9.0, 8.88, serial=9.5, serial_no_ssh=9.38),
                row('2g', 2.0, 'full', 4.0, 4.0),
                row('2g', 2.0, 'rsync_8k', 6.0, 5.88, 3.9, 3.78, 7.0, 6.88),
                row('2g', 2.0, 'pgdelta', 5.0, 4.88, serial=5.5, serial_no_ssh=5.38)]

    def test_serial_rows_are_the_upper_bound(self):
        rows = report.with_in_place(self.rows())
        self.assertEqual(sorted({row['method'] for row in rows}),
                         ['full', 'pgdelta', 'pgdelta (serial)', 'rsync_8k', 'rsync_8k (in place)',
                          'rsync_8k (serial)'])
        result = report.crossover(report.medians(rows), report.speed_order(rows), 't0',
                                  'rsync_8k (serial)')
        self.assertEqual(result['ratios'], {'0.5g': 1.2, '2g': 1.75})
        text = report.render(self.rows())
        self.assertIn('| rsync_8k (serial) | 1.20 | 1.75 | 0.5g |', text)
        self.assertIn('| pgdelta (serial) | 0.95 | 1.38 | 2g |', text)
        self.assertIn('| pgdelta | 0.90 | 1.25 | 2g |', text)
        self.assertIn('max(prep, capture) + transfer', text)
        self.assertIn('max(receiver start, capture) + transfer', text)
        self.assertIn('upper bound', text)
        # The two kinds of serial row differ: rsync's copy reads B0 cold on its critical path,
        # pgdelta's receiver still prefetches B0 while the capture runs.
        header = text.split('\n## ')[0]
        self.assertIn('prefetch', header)
        self.assertIn('cold', header)

    def test_runs_whose_prefetch_read_b0_between_timed_phases_are_flagged(self):
        rows = self.rows()
        for row in rows:
            row.update(prefetch_untimed_s=None, prefetch_untimed_bytes_est=None, repeat='0')
        pgdelta = [row for row in rows if row['method'] == 'pgdelta']
        pgdelta[0].update(prefetch_untimed_s=0.0, prefetch_untimed_bytes_est=0.0)
        pgdelta[1].update(prefetch_untimed_s=0.08, prefetch_untimed_bytes_est=58_000_000.0)
        text = report.render(rows)
        self.assertIn('1 of 2 pgdelta runs', text)
        self.assertIn('- t0 r0 2g pgdelta: 0.080 s, about 58.0 MB', text)
        pgdelta[1].update(prefetch_untimed_s=0.0, prefetch_untimed_bytes_est=0.0)
        self.assertIn('None of the 2 pgdelta runs', report.render(rows))
        # Older summaries have no such column: nothing is said.
        for row in rows:
            row.pop('prefetch_untimed_s')
        self.assertNotIn('pgdelta runs', report.render(rows))

    def test_crossover_on_end_to_end_and_in_place(self):
        rows = report.with_in_place(self.rows())
        table = report.medians(rows)
        speeds = report.speed_order(rows)
        end_to_end = report.crossover(table, speeds, 't0', 'rsync_8k')
        self.assertEqual(end_to_end['ratios'], {'0.5g': 1.1, '2g': 1.5})
        self.assertEqual(end_to_end['not_faster_from'], '0.5g')
        in_place = report.crossover(table, speeds, 't0', 'rsync_8k (in place)')
        self.assertEqual(in_place['ratios'], {'0.5g': 0.8, '2g': 0.975})
        self.assertTrue(in_place['faster_everywhere'])
        no_ssh = report.crossover(report.medians(rows, 'total_no_ssh_s'), speeds, 't0',
                                  'rsync_8k (in place)')
        self.assertEqual(no_ssh['ratios']['2g'], 0.945)

    def test_render_names_the_total_the_crossover_uses(self):
        text = report.render(self.rows())
        self.assertIn('crossover uses `total_s`', text)
        self.assertIn('| rsync_8k | 1.10 | 1.50 | 0.5g |', text)
        self.assertIn('| rsync_8k (in place) | 0.80 | 0.97 | never (faster at every speed) |',
                      text)
        self.assertIn('`total_no_ssh_s`', text)


# --- T3, T8a and T11: the optional methods decided on October 23, 2026 ------------

NEW_METHODS = (*measure.T3_METHODS, *measure.PGDELTA_DIRECT_METHODS, *measure.PGBACKREST_METHODS)
JOBS = ('--jobs', '4')
# A per-table digest as logical_digest_sql prints it.
LOGICAL = {'pgbench_accounts': {'rows': 500000, 'md5': 'a' * 32},
           'pgbench_branches': {'rows': 5, 'md5': 'b' * 32},
           'pgbench_tellers': {'rows': 50, 'md5': 'c' * 32},
           'pgbench_history': {'rows': 0, 'md5': 'd41d8cd98f00b204e9800998ecf8427e'},
           'events': {'rows': 505000, 'md5': 'e' * 32}}
# pgbackrest info --output=json for one stanza after its full backup (pgBackRest 2.5x layout, trimmed).
PGBACKREST_INFO = json.dumps([{
    'name': 'pgbackrest_zst', 'status': {'code': 0, 'message': 'ok'}, 'cipher': 'none',
    'backup': [{'label': '20261023-120000F', 'type': 'full', 'error': False,
                'info': {'size': 2_112_000_000, 'delta': 2_112_000_000,
                         'repository': {'size': 310_000_000, 'delta': 310_000_000}},
                'timestamp': {'start': 1761220800, 'stop': 1761220830}}]}])
# pgBackRest's restore, console (info) then file log (detail), trimmed.
PGBACKREST_RESTORE_LOG = """\
2026-10-23 12:10:00.000 P00   INFO: restore command begin 2.54.0: --config=/data/exp04/pgbackrest/pgbackrest.conf --delta --pg1-path=/data/exp04/out/pgbackrest_zst --process-max=4 --stanza=pgbackrest_zst
2026-10-23 12:10:00.100 P00   INFO: repo1: restore backup set 20261023-120000F, recovery will start at 2026-10-23 12:00:00
2026-10-23 12:10:00.200 P00   INFO: remove invalid files/links/paths from '/data/exp04/out/pgbackrest_zst'
2026-10-23 12:10:03.000 P00   INFO: write updated /data/exp04/out/pgbackrest_zst/postgresql.auto.conf
2026-10-23 12:10:03.100 P00   INFO: restore global/pg_control (performed last to ensure aborted restores cannot be started)
2026-10-23 12:10:03.200 P00   INFO: restore size = 2.1GB, file total = 1012
2026-10-23 12:10:03.300 P00 DETAIL: statistics: {"socket.client":{"total":5}}
2026-10-23 12:10:03.300 P00   INFO: restore command end: completed successfully (3301ms)
2026-10-23 12:10:01.000 P01 DETAIL: restore file /data/exp04/out/pgbackrest_zst/base/5/16384 - exists and matches backup (1GB, 48.00%) checksum 0123
2026-10-23 12:10:02.000 P02 DETAIL: restore file /data/exp04/out/pgbackrest_zst/base/5/16384.1 (136MB, 54.00%) checksum 4567
2026-10-23 12:10:02.500 P03 DETAIL: restore file /data/exp04/out/pgbackrest_zst/base/5/16397 - exists and matches backup (8KB, 54.01%) checksum 89ab
"""


class NewMethodFlags(unittest.TestCase):
    def test_the_new_methods_are_optional(self):
        self.assertEqual(len(NEW_METHODS), 10)
        for method in NEW_METHODS:
            self.assertNotIn(method, measure.METHODS)
            self.assertIn(method, measure.OPTIONAL_METHODS)
            self.assertIn(method, report.DELTA_METHODS)
            self.assertIn(method, measure.PROC_SCOPE)
        self.assertEqual(run_exp04.parse_args(['--source-ip', 'x', '--out', 'y']).methods,
                         'full,incremental,pgdelta,rsync_8k,rsync_default')
        # T3's methods are staged like pgdelta; T8a's and T11's have no capture.
        for method in measure.T3_METHODS:
            self.assertIn(method, measure.STAGED_METHODS)
        for method in (*measure.PGDELTA_DIRECT_METHODS, *measure.PGBACKREST_METHODS):
            self.assertNotIn(method, measure.STAGED_METHODS)
        self.assertEqual(set(measure.PGDELTA_METHODS),
                         {'pgdelta', 'pgdelta_1thread', *measure.T3_METHODS, *measure.PGDELTA_DIRECT_METHODS})
        self.assertEqual(set(measure.BASIS_COPY_METHODS),
                         {'pgdelta_inplace', 'pgdelta_direct_inplace', *measure.PGBACKREST_METHODS})

    def test_each_new_pgdelta_method_pins_its_flags(self):
        def receive(*middle, fsync='main', in_place=False):
            return (*JOBS, *middle, '--prefetch-basis', 'on', '--fsync', fsync,
                    *(('--in-place',) if in_place else ()))
        direct = (*JOBS, '--source-kind', 'datadir')
        expected = {
            'pgdelta_gapfill_seq': (JOBS, receive('--rebuild', 'gap-fill', '--verify', 'sequential')),
            'pgdelta_seq': (JOBS, receive('--rebuild', 'copy-patch', '--verify', 'sequential')),
            'pgdelta_fsync_wb': (JOBS, receive('--rebuild', 'copy-patch', '--verify', 'pipelined',
                                               fsync='writeback')),
            'pgdelta_fsync_worker': (JOBS, receive('--rebuild', 'copy-patch', '--verify', 'pipelined',
                                                   fsync='worker')),
            # In place there is nothing to copy B from: no --rebuild.
            'pgdelta_inplace': (JOBS, receive('--verify', 'pipelined', in_place=True)),
            'pgdelta_direct': (direct, receive('--rebuild', 'copy-patch', '--verify', 'pipelined')),
            'pgdelta_direct_inplace': (direct, receive('--verify', 'pipelined', in_place=True)),
        }
        for method, (send, received) in expected.items():
            with self.subTest(method=method):
                self.assertEqual(measure.PGDELTA_ARGS[method], {'send': send, 'receive': received})
        # pgdelta and pgdelta_1thread are as before: no --fsync, so they still run on a pgdelta
        # without the new modes.
        self.assertEqual(measure.PGDELTA_ARGS['pgdelta'],
                         {'send': JOBS, 'receive': (*JOBS, '--rebuild', 'copy-patch', '--verify',
                                                    'pipelined', '--prefetch-basis', 'on')})

    def test_the_pre_run_check_asks_for_the_selected_methods_options(self):
        self.assertEqual(measure.pgdelta_required_options(['full', 'rsync_8k']), {})
        self.assertEqual(measure.pgdelta_required_options(measure.METHODS), measure.PGDELTA_OPTIONS)
        self.assertEqual(measure.pgdelta_required_options(['pgdelta_inplace']),
                         {'send': ('--jobs',),
                          'receive': ('--jobs', '--verify', '--prefetch-basis', '--fsync', '--in-place')})
        self.assertEqual(measure.pgdelta_required_options(['pgdelta', 'pgdelta_direct']),
                         {'send': ('--jobs', '--source-kind'),
                          'receive': ('--jobs', '--rebuild', '--verify', '--prefetch-basis', '--fsync')})
        today = ('  --jobs N\n  --rebuild {copy-patch,gap-fill}\n  --verify {pipelined,sequential}\n'
                 '  --prefetch-basis {on,off}\n')
        required = measure.pgdelta_required_options(['pgdelta_fsync_wb'])['receive']
        self.assertFalse(run_exp04.pgdelta_supports(today, required))
        self.assertTrue(run_exp04.pgdelta_supports(today + '  --fsync {main,writeback,worker}\n', required))

    def test_switches_are_compared_on_what_the_method_pins(self):
        expected = measure.pgdelta_switches(measure.PGDELTA_ENGINES['pgdelta'])
        reported = json.loads(json.dumps(expected))
        self.assertTrue(measure.pgdelta_switches_match(expected, reported))
        reported['receiver']['fsync'] = 'main'  # a newer pgdelta reports more: the same engine
        self.assertTrue(measure.pgdelta_switches_match(expected, reported))
        reported['receiver']['verify'] = 'sequential'
        self.assertFalse(measure.pgdelta_switches_match(expected, reported))
        self.assertFalse(measure.pgdelta_switches_match(expected, None))
        self.assertFalse(measure.pgdelta_switches_match(expected, {'sender': expected['sender']}))
        self.assertNotIn('rebuild', measure.pgdelta_switches(measure.PGDELTA_ENGINES['pgdelta_inplace'])['receiver'])

    def test_modes_are_found_wherever_pgdelta_reports_them(self):
        engine = measure.PGDELTA_ENGINES['pgdelta_direct_inplace']
        nothing = measure.pgdelta_modes_report({}, engine)
        self.assertIsNone(nothing['modes_ok'])
        self.assertEqual(nothing['modes_expected'],
                         {'in_place': True, 'fsync_mode': 'main', 'source_kind': 'datadir'})
        stats = {'source_kind': 'datadir',
                 'receiver': {'in_place': True, 'timings_seconds': {'fsync_wait': 0.25}},
                 'switches': {'receiver': {'fsync_mode': 'main'}}}
        found = measure.pgdelta_modes_report(stats, engine)
        self.assertEqual(found['modes'], {'in_place': True, 'fsync_mode': 'main', 'source_kind': 'datadir'})
        self.assertIs(found['modes_ok'], True)
        self.assertEqual(found['fsync_wait_seconds'], 0.25)
        stats['receiver']['in_place'] = False
        self.assertIs(measure.pgdelta_modes_report(stats, engine)['modes_ok'], False)
        self.assertEqual(measure.pgdelta_modes_report({'fsync_wait_seconds': 1.5}, engine)['fsync_wait_seconds'], 1.5)
        # pgdelta pins no mode: it expects today's write path and a capture.
        today = measure.PGDELTA_ENGINES['pgdelta']
        self.assertEqual(measure.pgdelta_modes(today),
                         {'in_place': False, 'fsync_mode': 'main', 'source_kind': 'backup'})
        self.assertIs(measure.pgdelta_modes_report({'fsync_mode': 'main', 'in_place': False}, today)['modes_ok'], True)


    def test_the_fsync_mode_is_read_as_the_engine_reports_it(self):
        # build/engine-v1-modes's statistics: the receiver's `fsync` (asked for) and `fsync_effective`
        # (in effect; writeback falls back to main where sync_file_range is refused), and the sender's
        # timings, whose `fsync` is seconds.
        def stats(asked, effective):
            return {'source_kind': 'backup', 'in_place': False, 'timings_seconds': {'fsync': 0.75},
                    'receiver': {'in_place': False, 'fsync': asked, 'fsync_effective': effective,
                                 'fsync_wait_seconds': 0.125, 'timings_seconds': None}}

        worker = measure.pgdelta_modes_report(stats('worker', 'worker'),
                                              measure.PGDELTA_ENGINES['pgdelta_fsync_worker'])
        self.assertEqual(worker['modes'], {'in_place': False, 'fsync_mode': 'worker', 'source_kind': 'backup'})
        self.assertEqual(worker['fsync_requested'], 'worker')
        self.assertIs(worker['modes_ok'], True)
        self.assertEqual(worker['fsync_wait_seconds'], 0.125)
        # writeback asked for, main in effect: the run did not measure what pgdelta_fsync_wb names.
        fallback = measure.pgdelta_modes_report(stats('writeback', 'main'),
                                                measure.PGDELTA_ENGINES['pgdelta_fsync_wb'])
        self.assertEqual((fallback['modes']['fsync_mode'], fallback['fsync_requested']), ('main', 'writeback'))
        self.assertIs(fallback['modes_ok'], False)
        self.assertEqual(measure.summary_row({'tool_reported': fallback})['fsync_mode'], 'main')
        # A receiver without the keys: the timings' seconds are not a mode.
        older = stats(None, None)
        older['receiver'] = {'in_place': False, 'timings_seconds': {'fsync': 0.5}}
        report = measure.pgdelta_modes_report(older, measure.PGDELTA_ENGINES['pgdelta_seq'])
        self.assertIsNone(report['modes']['fsync_mode'])
        self.assertIsNone(report['fsync_requested'])
        # The contract's own key still counts.
        contract = measure.pgdelta_modes_report({'receiver': {'fsync_mode': 'writeback'}},
                                                measure.PGDELTA_ENGINES['pgdelta_fsync_wb'])
        self.assertEqual(contract['modes']['fsync_mode'], 'writeback')
        self.assertIs(contract['modes_ok'], True)


class MethodSpeeds(unittest.TestCase):
    speeds = measure.parse_speeds('2,5,unthrottled')
    methods = ['full', 'pgdelta', 'pgdelta_seq', 'pgdelta_inplace', 'pgdelta_direct']

    def test_methods_limited_to_some_speeds(self):
        limited = measure.parse_method_speeds(['pgdelta_seq+pgdelta_inplace=unthrottled', 'full=unthrottled,2'],
                                              self.methods, self.speeds)
        self.assertEqual(limited, {'pgdelta_seq': ['unthrottled'], 'pgdelta_inplace': ['unthrottled'],
                                   'full': ['2g', 'unthrottled']})
        self.assertEqual(measure.methods_at(self.methods, limited, '5g'), ['pgdelta', 'pgdelta_direct'])
        self.assertEqual(measure.methods_at(self.methods, limited, 'unthrottled'), self.methods)
        self.assertEqual(measure.methods_at(self.methods, {}, '2g'), self.methods)
        self.assertEqual(measure.parse_method_speeds([], self.methods, self.speeds), {})

    def test_bad_specs_are_refused(self):
        for specs in (['pgdelta_seq'], ['=2'], ['pgdelta_seq=10'], ['rsync_8k=2'], ['pgdelta_seq=fast'],
                      ['pgdelta_seq=2', 'pgdelta_seq=5']):
            with self.subTest(specs=specs), self.assertRaises(ValueError):
                measure.parse_method_speeds(specs, self.methods, self.speeds)
        base = ['--source-ip', 'x', '--out', 'y', '--speeds', '2,unthrottled', '--methods', 'full,pgdelta_seq']
        with redirect_stdout(io.StringIO()), mock.patch('sys.stderr', io.StringIO()), \
                self.assertRaises(SystemExit):
            run_exp04.parse_args([*base, '--method-speeds', 'pgdelta_seq=5'])
        args = run_exp04.parse_args([*base, '--method-speeds', 'pgdelta_seq=unthrottled'])
        self.assertEqual(args.method_speeds, ['pgdelta_seq=unthrottled'])


class TrialStub(StubOrchestrator):
    """run_trial with every VM step stubbed: events say what ran, and in what order."""

    def throttle(self, rate):
        return {}

    def set_speed(self, speed):
        return {'speed': speed, 'source_to_dest_gbit': 1.0, 'dest_to_source_gbit': 1.0}

    def qdisc_stats(self):
        return {}

    def take_b0(self, trial):
        self.events.append('take_b0')
        self.b0_digest = {'entries': {}, 'device': 1}

    def cleanup_trial(self, trial):
        self.events.append('cleanup_trial')

    def _source_script(self, script, *args, timeout=7200):
        self.events.append(('source_script', args))
        return {'spec': {'accounts_rows': 500000, 'events_rows': 500000}}

    def setup_pgbackrest(self):
        self.events.append('setup_pgbackrest')
        return {'backups': {}}

    def pgbackrest_backups(self):
        self.events.append('pgbackrest_backups')
        return {stanza: {'repo_size_bytes': 1} for stanza in self.pgbackrest_methods}

    def run_method(self, trial, repeat, speed, link, method, order_index, order):
        self.events.append(('run', speed['label'], method))
        record = {'status': 'pass', 'speed_index': link.get('speed_index'), 'method': method}
        self.records.append(record)
        return record


class NewMethodRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        for patcher in (mock.patch.object(run_exp04.time, 'perf_counter', self.clock),
                        mock.patch.object(run_exp04, 'log')):
            patcher.start()
            self.addCleanup(patcher.stop)
        FakeChild.started.clear()

    def orchestrator(self, cls=StubOrchestrator, extra_args=()):
        orchestrator = cls(self.tmp.name, self.clock, extra_args)
        self.addCleanup(orchestrator.pool.shutdown)
        return orchestrator

    def run_method(self, orchestrator, method, child=FakeChild):
        with mock.patch.object(run_exp04, 'Child', child):
            return orchestrator.run_method(TRIAL, 0, SPEED, dict(LINK), method, 0, [method])

    @staticmethod
    def kinds(orchestrator):
        """Events as short names: drop_caches, verify, ('dest', 'cp'), ('source_script', 'start')."""
        kinds = []
        for event in orchestrator.events:
            if isinstance(event, str):
                kinds.append(event)
            elif event[0] in ('dest', 'source'):
                kinds.append((event[0], Path(event[1][0]).name))
            elif event[0] == 'source_script':
                kinds.append((event[0], event[1][0]))
            else:
                kinds.append(event[0])
        return kinds

    @staticmethod
    def sends(orchestrator):
        return [event[1] for event in orchestrator.events
                if isinstance(event, tuple) and event[0] == 'source' and 'send' in event[1]]

    # T3: staged like pgdelta, one switch changed.

    def test_t3_methods_run_staged_with_their_own_flags(self):
        for method in measure.T3_METHODS:
            with self.subTest(method=method):
                orchestrator = self.orchestrator()
                FakeChild.started.clear()
                record = self.run_method(orchestrator, method)
                self.assertEqual(record['status'], 'pass', record.get('traceback'))
                self.assertEqual(list(record['phases']), ['receiver_capture', 'transfer'])
                receive = FakeChild.started[0].argv
                self.assertEqual(receive[receive.index('--pg-controldata') + 2:],
                                 list(measure.PGDELTA_ARGS[method]['receive']))
                [send] = self.sends(orchestrator)
                self.assertEqual(send[send.index('--source') + 1], orchestrator.source_capture)
                self.assertEqual(send[-len(measure.PGDELTA_ARGS[method]['send']):],
                                 list(measure.PGDELTA_ARGS[method]['send']))
                self.assertIsNotNone(record['totals_serial'])
                self.assertEqual(self.kinds(orchestrator).count('drop_caches'), 1)

    def test_pgdelta_inplace_refreshes_a_copy_of_b0_made_before_the_cache_drop(self):
        orchestrator = self.orchestrator()
        record = self.run_method(orchestrator, 'pgdelta_inplace')
        self.assertEqual(record['status'], 'pass', record.get('traceback'))
        b = str(orchestrator.outputs / 'pgdelta_inplace')
        dest = [event[1] for event in orchestrator.events if isinstance(event, tuple) and event[0] == 'dest']
        # Never a reflink clone: on XFS cp's default would make B share every block with B0.
        self.assertEqual(dest[:2], [['cp', '-a', '--reflink=never', str(orchestrator.b0), b], ['sync', '-f', b]])
        kinds = self.kinds(orchestrator)
        self.assertLess(kinds.index(('dest', 'sync')), kinds.index('drop_caches'))
        self.assertLess(kinds.index('drop_caches'), kinds.index(('source', 'pg_basebackup')))
        receive = FakeChild.started[0].argv
        self.assertEqual(receive[receive.index('--basis') + 1], b)
        self.assertNotIn('--output', receive)
        self.assertNotIn('--rebuild', receive)
        self.assertIn('--in-place', receive)
        self.assertEqual(receive[receive.index('--fsync') + 1], 'main')
        self.assertEqual(record['paths']['receipt'], b + '.pgdelta-receipt.json')
        # The copy is untimed set-up; the timed phases are pgdelta's.
        row = measure.summary_row(record)
        self.assertEqual(row['total_s'], 3.12)
        self.assertEqual(row['untimed_setup_s'], 3.5)  # the stub's cp 3.0 s and sync 0.5 s
        self.assertEqual([step['what'][:7] for step in record['preparation']['steps']], ['copy B0'])

    def test_a_failed_copy_of_b0_fails_the_run_before_the_cache_drop(self):
        class FailingCopy(StubOrchestrator):
            def local_command(self, argv, log_name, **kwargs):
                result = super().local_command(argv, log_name, **kwargs)
                if Path(str(argv[0])).name == 'cp':
                    result['exit_code'] = 1
                return result

        orchestrator = self.orchestrator(FailingCopy)
        record = self.run_method(orchestrator, 'pgbackrest_zst')
        self.assertEqual(record['status'], 'error')
        self.assertIn('copy of B0', record['error'])
        self.assertNotIn('drop_caches', self.kinds(orchestrator))

    def test_a_copy_of_b0_that_shares_blocks_fails_the_run_before_the_cache_drop(self):
        for method in measure.BASIS_COPY_METHODS:
            with self.subTest(method=method):
                orchestrator = self.orchestrator()
                orchestrator.pgbackrest = {'backups': {}}
                shared = {'supported': True, 'error': None, 'files': 990, 'shared_files': 990,
                          'shared_bytes': 2_100_000_000, 'examples': ['base/5/16384']}
                with mock.patch.object(run_exp04.measure, 'shared_extents', return_value=shared) as check:
                    record = self.run_method(orchestrator, method)
                check.assert_called_once_with(orchestrator.outputs / method)
                self.assertEqual(record['status'], 'error')
                self.assertIn('shares disk blocks', record['error'])
                self.assertNotIn('drop_caches', self.kinds(orchestrator))

    def test_the_copy_of_b0_records_that_it_shares_no_blocks(self):
        orchestrator = self.orchestrator()
        unshared = {'supported': True, 'error': None, 'files': 990, 'shared_files': 0, 'shared_bytes': 0,
                    'examples': []}
        with mock.patch.object(run_exp04.measure, 'shared_extents', return_value=unshared):
            record = self.run_method(orchestrator, 'pgdelta_inplace')
        self.assertEqual(record['status'], 'pass', record.get('traceback'))
        [step] = record['preparation']['steps']
        self.assertEqual(step['shared_extents'], unshared)
        self.assertIn('--reflink=never', step['what'])

    # T8a: no capture; the source is stopped cleanly first and restarted whatever happens.

    def test_pgdelta_direct_reads_the_stopped_source_without_a_capture(self):
        clock = self.clock

        class SlowStart(FakeChild):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                clock.now += 1.5

        orchestrator = self.orchestrator()
        record = self.run_method(orchestrator, 'pgdelta_direct', SlowStart)
        self.assertEqual(record['status'], 'pass', record.get('traceback'))
        self.assertEqual(list(record['phases']), ['receiver_start', 'transfer'])
        kinds = self.kinds(orchestrator)
        self.assertNotIn(('source', 'pg_basebackup'), kinds)
        self.assertNotIn(('dest', 'cp'), kinds)  # a new copy O: nothing to copy first
        # Stopped before the cache drop, restarted after the verification.
        self.assertEqual([kind for kind in kinds if isinstance(kind, tuple) and kind[0] == 'source_script'],
                         [('source_script', 'stop-clean'), ('source_script', 'start')])
        self.assertLess(kinds.index(('source_script', 'stop-clean')), kinds.index('drop_caches'))
        self.assertLess(kinds.index('verify'), kinds.index(('source_script', 'start')))
        [send] = self.sends(orchestrator)
        self.assertEqual(send[send.index('--source') + 1], orchestrator.source_data)
        self.assertEqual(send[send.index('--source-kind') + 1], 'datadir')
        receive = FakeChild.started[0].argv
        self.assertEqual(receive[receive.index('--basis') + 1], str(orchestrator.b0))
        self.assertEqual(receive[receive.index('--output') + 1], str(orchestrator.outputs / 'pgdelta_direct'))
        self.assertEqual(receive[receive.index('--fsync') + 1], 'main')
        self.assertNotIn('--in-place', receive)
        # The receiver's record (its whole life) is in the transfer, as for the staged methods.
        transfer = record['phases']['transfer']['commands']
        self.assertEqual([command['host'] for command in transfer], ['dest', 'source'])
        self.assertIn('receiver_start', transfer[0]['role'])
        row = measure.summary_row(record)
        self.assertEqual((row['receiver_start_s'], row['transfer_s'], row['total_s']), (1.5, 2.06, 3.56))
        self.assertIsNone(row['capture_s'])
        self.assertIsNone(row['serial_total_s'])
        self.assertEqual(row['counter_windows'], 2)
        self.assertIn('source_restart', record)
        self.assertIsNone(orchestrator.source_down)

    def test_pgdelta_direct_inplace_copies_b0_and_stops_the_source_first(self):
        orchestrator = self.orchestrator()
        record = self.run_method(orchestrator, 'pgdelta_direct_inplace')
        self.assertEqual(record['status'], 'pass', record.get('traceback'))
        kinds = self.kinds(orchestrator)
        drop = kinds.index('drop_caches')
        self.assertLess(kinds.index(('dest', 'sync')), drop)
        self.assertLess(kinds.index(('source_script', 'stop-clean')), drop)
        receive = FakeChild.started[0].argv
        self.assertEqual(receive[receive.index('--basis') + 1], str(orchestrator.outputs / 'pgdelta_direct_inplace'))
        self.assertIn('--in-place', receive)
        self.assertNotIn('--output', receive)
        [send] = self.sends(orchestrator)
        self.assertEqual(send[send.index('--source-kind') + 1], 'datadir')

    def test_the_source_is_restarted_whatever_happens(self):
        class LostTransfer(StubOrchestrator):
            def remote_command(self, argv, log_name, **kwargs):
                if 'send' in [str(part) for part in argv]:
                    raise run_exp04.CommandError('ssh: connection lost')
                return super().remote_command(argv, log_name, **kwargs)

        class FailedStop(StubOrchestrator):
            def _source_script(self, script, *args, timeout=7200):
                super()._source_script(script, *args)
                if args[0] == 'stop-clean':
                    raise run_exp04.CommandError('the source did not stop cleanly')
                return {}

        for cls in (LostTransfer, FailedStop):
            with self.subTest(failure=cls.__name__):
                orchestrator = self.orchestrator(cls)
                FakeChild.started.clear()
                record = self.run_method(orchestrator, 'pgdelta_direct_inplace')
                self.assertEqual(record['status'], 'error')
                scripts = [kind[1] for kind in self.kinds(orchestrator)
                           if isinstance(kind, tuple) and kind[0] == 'source_script']
                self.assertEqual(scripts, ['stop-clean', 'start'])
                self.assertIsNone(orchestrator.source_down)
                if FakeChild.started:
                    self.assertTrue(FakeChild.started[0].stopped)

    def test_a_source_that_does_not_restart_stops_the_trial(self):
        class NoRestart(TrialStub):
            run_method = run_exp04.Orchestrator.run_method
            verify = StubOrchestrator.verify

            def _source_script(self, script, *args, timeout=7200):
                if args[0] == 'start':
                    raise run_exp04.CommandError('the source did not start')
                return super()._source_script(script, *args)

        orchestrator = self.orchestrator(NoRestart)
        orchestrator.speeds = measure.parse_speeds('2,unthrottled')
        orchestrator.methods = ['pgdelta_direct', 'full']
        with mock.patch.object(run_exp04, 'Child', FakeChild), \
                mock.patch.object(run_exp04.measure, 'tree_digest', return_value={'entries': {}, 'device': 1}):
            trial = orchestrator.run_trial(0, 'uniform', 0.01)
        self.assertEqual(trial['status'], 'error')
        self.assertIn('source cluster is down', trial['error'])
        direct = [record for record in orchestrator.records if record['method'] == 'pgdelta_direct']
        self.assertEqual(len(direct), 1)
        self.assertIn('could not be restarted', direct[0]['error'])
        self.assertIn('cleanup_trial', orchestrator.events)

    def test_a_source_that_did_not_restart_stops_only_its_own_trial(self):
        class RestartsOnlyLater(TrialStub):
            run_method = run_exp04.Orchestrator.run_method
            verify = StubOrchestrator.verify
            starts = 0

            def _source_script(self, script, *args, timeout=7200):
                if args[0] == 'start':
                    self.starts += 1
                    if self.starts == 1:
                        raise run_exp04.CommandError('the source did not start')
                return super()._source_script(script, *args)

        orchestrator = self.orchestrator(RestartsOnlyLater)
        orchestrator.speeds = measure.parse_speeds('unthrottled')
        orchestrator.methods = ['pgdelta_direct', 'full']
        with mock.patch.object(run_exp04, 'Child', FakeChild), \
                mock.patch.object(run_exp04.measure, 'tree_digest', return_value={'entries': {}, 'device': 1}):
            first = orchestrator.run_trial(0, 'uniform', 0.01)
            # The next trial's set-up builds a new source cluster, which restarts fine.
            second = orchestrator.run_trial(1, 'uniform', 0.01)
        self.assertEqual(first['status'], 'error')
        self.assertIn('source cluster is down', first['error'])
        self.assertEqual(second['status'], 'pass', second.get('error'))
        self.assertEqual(sorted(run['method'] for run in second['runs']), ['full', 'pgdelta_direct'])
        self.assertIsNone(orchestrator.source_down)

    # T11: pgBackRest restore --delta of a copy of B0.

    class Restoring(StubOrchestrator):
        verify = run_exp04.Orchestrator.verify
        tool_reported = run_exp04.Orchestrator.tool_reported

        def check_output(self, method, data, *, recovery, query=run_exp04.OUTPUT_QUERY):
            self.events.append(('check_output', recovery, query))
            return {'ok': True, 'answer': json.dumps(LOGICAL), 'ready_seconds': 4.2}

    def test_pgbackrest_restores_a_copy_of_b0_from_the_source_repository(self):
        orchestrator = self.orchestrator(self.Restoring, ['--methods', 'full,pgbackrest_block'])
        orchestrator.methods = ['full', 'pgbackrest_block']
        orchestrator.pgbackrest = {'backups': {'pgbackrest_block': {'repo_size_bytes': 123}}}
        record = self.run_method(orchestrator, 'pgbackrest_block')
        # Restored and started; it passes or fails once the source's digest is taken.
        self.assertEqual(record['status'], 'pending', record.get('traceback'))
        b = str(orchestrator.outputs / 'pgbackrest_block')
        restore = [event[1] for event in orchestrator.events
                   if isinstance(event, tuple) and event[0] == 'dest' and event[1][0] == '/usr/bin/pgbackrest']
        self.assertEqual(restore, [['/usr/bin/pgbackrest', f'--config={orchestrator.work}/pgbackrest/pgbackrest.conf',
                                    '--stanza=pgbackrest_block', '--delta', '--process-max=4',
                                    f'--pg1-path={b}', 'restore']])
        kinds = self.kinds(orchestrator)
        self.assertLess(kinds.index(('dest', 'sync')), kinds.index('drop_caches'))
        self.assertLess(kinds.index('drop_caches'), kinds.index(('dest', 'pgbackrest')))
        self.assertEqual(list(record['phases']), ['restore'])
        [check] = [event for event in orchestrator.events if isinstance(event, tuple) and event[0] == 'check_output']
        self.assertEqual(check, ('check_output', True, measure.logical_digest_sql()))
        self.assertEqual(record['verification']['logical'], LOGICAL)
        self.assertIsNone(record['verification']['logical_match'])
        self.assertEqual(record['tool_reported']['backup'], {'repo_size_bytes': 123})
        self.assertEqual(record['tool_reported']['flags']['stanza'],
                         {'compress-type': 'zst', 'repo1-bundle': 'y', 'repo1-block': 'y'})
        row = measure.summary_row(record)
        self.assertEqual((row['restore_s'], row['total_s'], row['counter_windows']), (1.0, 1.0, 1))
        self.assertEqual((row['status'], row['logical_match'], row['output_start_s']), ('pending', None, 4.2))
        self.assertEqual(row['untimed_setup_s'], 3.5)
        self.assertIsNone(row['tool_s2d_bytes'])

    def test_pending_pgbackrest_runs_pass_or_fail_against_the_source_at_the_end(self):
        orchestrator = self.orchestrator(self.Restoring)
        orchestrator.methods = ['pgbackrest_zst', 'pgbackrest_none']
        orchestrator.pgbackrest = {'backups': {}}
        records = [self.run_method(orchestrator, method) for method in orchestrator.methods]
        records[1]['verification']['logical']['events']['rows'] = 504999  # a lost row
        trial = {'runs': [{'repeat': 0, 'speed': '0.5g', 'method': record['method'], 'status': 'pending'}
                          for record in records]}
        with mock.patch.object(orchestrator, 'query', return_value=json.dumps(LOGICAL)) as query:
            check = orchestrator.finish_logical_checks(trial)
        query.assert_called_once()
        self.assertEqual(query.call_args.args, (measure.logical_digest_sql(),))
        self.assertEqual((query.call_args.kwargs['host'], query.call_args.kwargs['port']), ('10.0.0.12', 5432))
        self.assertEqual([record['status'] for record in records], ['pass', 'fail'])
        self.assertEqual([run['status'] for run in trial['runs']], ['pass', 'fail'])
        self.assertEqual([run['match'] for run in check['runs']], [True, False])
        self.assertIn('events', records[1]['verification']['logical_differences'][0])
        saved = json.loads(orchestrator.record_path(records[1]).read_text())
        self.assertEqual((saved['status'], saved['verification']['logical_match']), ('fail', False))
        with open(orchestrator.out / 'summary.csv', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([(row['status'], row['logical_match']) for row in rows],
                         [('pass', 'True'), ('fail', 'False')])
        # Nothing pending: no query of the source.
        with mock.patch.object(orchestrator, 'query') as query:
            self.assertIsNone(orchestrator.finish_logical_checks(trial))
        query.assert_not_called()

    def test_pgbackrest_is_set_up_only_in_runs_that_select_it(self):
        for methods in (['full', 'pgdelta'], ['full', 'pgbackrest_zst']):
            with self.subTest(methods=methods):
                orchestrator = self.orchestrator(TrialStub)
                orchestrator.speeds = measure.parse_speeds('unthrottled')
                orchestrator.methods = methods
                with mock.patch.object(run_exp04.measure, 'tree_digest', return_value={'entries': {}, 'device': 1}):
                    trial = orchestrator.run_trial(0, 'uniform', 0.01)
                self.assertEqual(trial['status'], 'pass', trial.get('traceback'))
                kinds = self.kinds(orchestrator)
                steps = [kind for kind in kinds if kind in ('setup_pgbackrest', 'take_b0', 'pgbackrest_backups')
                         or kind in (('source_script', 'setup'), ('source_script', 'workload'))]
                if 'pgbackrest_zst' in methods:
                    # Set up before B0; the backups after the workload and its CHECKPOINT.
                    self.assertEqual(steps, [('source_script', 'setup'), 'setup_pgbackrest', 'take_b0',
                                             ('source_script', 'workload'), 'pgbackrest_backups'])
                    self.assertEqual(trial['pgbackrest']['backups'], {'pgbackrest_zst': {'repo_size_bytes': 1}})
                else:
                    self.assertEqual(steps, [('source_script', 'setup'), 'take_b0', ('source_script', 'workload')])
                    self.assertNotIn('pgbackrest', trial)
                    self.assertIsNone(orchestrator.pgbackrest_run_record())

    def test_method_speeds_limit_what_runs_at_each_speed(self):
        orchestrator = self.orchestrator(TrialStub)
        orchestrator.speeds = measure.parse_speeds('2,unthrottled')
        orchestrator.methods = ['full', 'pgdelta_seq', 'pgdelta_direct']
        orchestrator.method_speeds = {'pgdelta_seq': ['unthrottled']}
        with mock.patch.object(run_exp04.measure, 'tree_digest', return_value={'entries': {}, 'device': 1}):
            orchestrator.run_trial(0, 'uniform', 0.01)
        runs = [event[1:] for event in orchestrator.events if isinstance(event, tuple) and event[0] == 'run']
        self.assertEqual(sorted(method for speed, method in runs if speed == '2g'), ['full', 'pgdelta_direct'])
        self.assertEqual(sorted(method for speed, method in runs if speed == 'unthrottled'),
                         ['full', 'pgdelta_direct', 'pgdelta_seq'])

    def test_pgbackrest_set_up_commands(self):
        orchestrator = self.orchestrator()
        orchestrator.methods = ['full', 'pgbackrest_zst', 'pgbackrest_block']
        state = orchestrator.setup_pgbackrest()
        paths = measure.pgbackrest_paths(orchestrator.work)
        texts = orchestrator.pgbackrest_texts()
        # The source: its configuration, then archiving (one restart) through source_cluster.py.
        writes = [event[1:] for event in orchestrator.events if event[0] == 'source_write']
        self.assertEqual(writes, [(paths['config'], texts['source_config'])])
        [archive] = [event[1] for event in orchestrator.events if event[0] == 'source_script']
        self.assertEqual(archive[0], 'archive')
        self.assertEqual(archive[archive.index('--command') + 1],
                         f'/usr/bin/pgbackrest --config={paths["config"]} --stanza=pgbackrest_zst archive-push %p'
                         f' && /usr/bin/pgbackrest --config={paths["config"]} --stanza=pgbackrest_block '
                         'archive-push %p')
        # On the source: every stanza-create before archiving starts, then check per stanza.
        steps = [list(event[1][event[1].index('/usr/bin/pgbackrest') + 1:]) if event[0] == 'source_agent'
                 else event[1][0] for event in orchestrator.events
                 if (event[0] == 'source_agent' and '/usr/bin/pgbackrest' in event[1])
                 or event[0] == 'source_script']
        config = f'--config={paths["config"]}'
        self.assertEqual(steps, [[config, '--stanza=pgbackrest_zst', 'stanza-create'],
                                 [config, '--stanza=pgbackrest_block', 'stanza-create'], 'archive',
                                 [config, '--stanza=pgbackrest_zst', 'check'],
                                 [config, '--stanza=pgbackrest_block', 'check']])
        # Dest: its configuration and its own SSH set-up, then info per stanza through it.
        self.assertEqual(Path(paths['config']).read_text(), texts['dest_config'])
        self.assertEqual(Path(paths['ssh_config']).read_text(), texts['ssh_config'])
        self.assertTrue(os.access(paths['ssh'], os.X_OK))
        info = [event[1] for event in orchestrator.events if event[0] == 'dest']
        self.assertEqual(info, [['/usr/bin/pgbackrest', f'--config={paths["config"]}', f'--stanza={stanza}', 'info']
                                for stanza in ('pgbackrest_zst', 'pgbackrest_block')])
        self.assertEqual(sorted(state['stanzas']), ['pgbackrest_block', 'pgbackrest_zst'])
        self.assertEqual(state['archive_command'], archive[archive.index('--command') + 1])

    def test_pgbackrest_backups_are_full_and_recorded(self):
        orchestrator = self.orchestrator()
        orchestrator.methods = ['pgbackrest_zst']
        calls = []

        def agent(*args, timeout=600):
            calls.append(args)
            return {'exit_code': 0, 'log_tail': PGBACKREST_INFO if 'info' in args else 'backup done'}

        orchestrator.source.agent = agent
        backups = orchestrator.pgbackrest_backups()
        commands = [call[call.index(orchestrator.pgbackrest_exe) + 2:] for call in calls]
        self.assertEqual(commands, [('--stanza=pgbackrest_zst', '--type=full', 'backup'),
                                    ('--stanza=pgbackrest_zst', '--output=json', 'info')])
        self.assertEqual(backups['pgbackrest_zst']['repo_size_bytes'], 310_000_000)
        self.assertEqual(backups['pgbackrest_zst']['label'], '20261023-120000F')

    def test_run_json_records_the_flags_each_method_pins(self):
        orchestrator = self.orchestrator()
        self.assertEqual(orchestrator.method_flags('pgdelta_fsync_wb'),
                         {command: list(args) for command, args in measure.PGDELTA_ARGS['pgdelta_fsync_wb'].items()})
        self.assertEqual(orchestrator.method_flags('pgbackrest_none')['stanza'], {'compress-type': 'none'})
        self.assertEqual(orchestrator.method_flags('pgbackrest_none')['restore'], ['--delta', '--process-max=4'])
        self.assertIn('--fsync', orchestrator.method_flags('rsync_8k')['rsync'])
        self.assertEqual(orchestrator.method_flags('full'), {'pg_basebackup': ['--manifest-checksums', 'SHA256']})
        orchestrator.methods = ['pgbackrest_zst']
        record = orchestrator.pgbackrest_run_record()
        self.assertIn('archive_mode = on', record['archiving'])
        self.assertIn('[pgbackrest_zst]', record['source_config'])

    def test_the_pre_run_check_needs_the_new_options_and_the_datadir_module(self):
        orchestrator = self.orchestrator()
        orchestrator.methods = ['pgdelta', 'pgdelta_direct']
        helps = {'send': '  --jobs N\n  --source-kind {backup,datadir}\n',
                 'receive': ('  --jobs N\n  --rebuild {copy-patch,gap-fill}\n  --verify {pipelined,sequential}\n'
                             '  --prefetch-basis {on,off}\n  --fsync {main,writeback,worker}\n')}

        def local(argv, **kwargs):
            if '-c' in argv:  # import pgdelta.datadir
                return mock.Mock(returncode=0, stdout='')
            return mock.Mock(returncode=0, stdout=helps[argv[3]])

        def remote(*args, timeout=600):
            if '-c' in args:
                return {'exit_code': 1, 'log_tail': 'No module named pgdelta.datadir'}
            return {'exit_code': 0, 'log_tail': helps[args[-2]]}

        orchestrator.source.agent = remote
        with mock.patch.object(run_exp04.subprocess, 'run', side_effect=local), \
                self.assertRaises(SystemExit) as refused:
            orchestrator.check_pgdelta_jobs()
        self.assertIn('pgdelta.datadir', str(refused.exception))
        orchestrator.source.agent = lambda *args, timeout=600: (
            {'exit_code': 0, 'log_tail': ''} if '-c' in args else {'exit_code': 0, 'log_tail': helps[args[-2]]})
        with mock.patch.object(run_exp04.subprocess, 'run', side_effect=local):
            found = orchestrator.check_pgdelta_jobs()
        self.assertEqual(found, {'send': {'dest': True, 'source': True}, 'receive': {'dest': True, 'source': True},
                                 'pgdelta.datadir': {'dest': True, 'source': True}})
        helps['receive'] = helps['receive'].replace('--fsync', '--other')
        with mock.patch.object(run_exp04.subprocess, 'run', side_effect=local), self.assertRaises(SystemExit):
            orchestrator.check_pgdelta_jobs()


# --- the verification of the new methods ------------------------------------------

class OutputVerification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = mock.patch.object(run_exp04, 'log')
        patcher.start()
        self.addCleanup(patcher.stop)

    def orchestrator(self, cls=StubOrchestrator):
        orchestrator = cls(self.tmp.name, FakeClock())
        self.addCleanup(orchestrator.pool.shutdown)
        return orchestrator

    def test_outputs_start_on_a_spare_port_with_no_listener_and_no_archiving(self):
        orchestrator = self.orchestrator()
        argv = [str(part) for part in orchestrator.output_start_argv(Path('/d/B'), Path('/l/server.log'))]
        self.assertEqual(argv[:6], ['/usr/lib/postgresql/17/bin/pg_ctl', '-D', '/d/B', '-l', '/l/server.log', '-o'])
        self.assertEqual(argv[7:], ['-w', '-t', '1800', 'start'])
        self.assertEqual(shlex.split(argv[6]), ['-p', '5433', '-k', str(orchestrator.work / 'vs'),
                                                '-c', 'listen_addresses=', '-c', 'archive_mode=off'])
        self.assertEqual([str(part) for part in orchestrator.output_stop_argv(Path('/d/B'))],
                         ['/usr/lib/postgresql/17/bin/pg_ctl', '-D', '/d/B', '-m', 'fast', '-w', '-t', '300', 'stop'])
        self.assertEqual([str(part) for part in orchestrator.psql_argv('/s', 5433, 'SELECT 1;')],
                         ['/usr/lib/postgresql/17/bin/psql', '-X', '-A', '-t', '-q', '-v', 'ON_ERROR_STOP=1',
                          '-h', '/s', '-p', '5433', '-U', 'study', '-d', 'postgres', '-c', 'SELECT 1;'])

    def server_stub(self, *, start_exit=0, answers=()):
        """An orchestrator whose pg_ctl creates and removes postmaster.pid, and whose psql answers in turn."""
        answers = list(answers)

        class Server(StubOrchestrator):
            def local_command(self, argv, log_name, *, timeout=None, cwd=None, env=None):
                argv = [str(part) for part in argv]
                self.events.append(('dest', argv, log_name))
                data = Path(argv[argv.index('-D') + 1])
                code = 0
                if argv[-1] == 'start':
                    code = start_exit
                    if not start_exit:
                        (data / 'postmaster.pid').write_text('1')
                elif argv[-1] == 'stop':
                    (data / 'postmaster.pid').unlink(missing_ok=True)
                return {'host': 'dest', 'argv': argv, 'exit_code': code, 'wall_seconds': 0.1}

            def query(self, sql, **kwargs):
                self.events.append(('query', sql))
                answer = answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer

        return self.orchestrator(Server)

    def test_check_output_waits_for_recovery_and_stops_the_server(self):
        data = self.root / 'B'
        data.mkdir()
        orchestrator = self.server_stub(answers=[run_exp04.CommandError('starting up'), 't', 'f', '{"x": 1}'])
        with mock.patch.object(run_exp04.time, 'sleep'):
            result = orchestrator.check_output('pgbackrest_zst', data, recovery=True, query='SELECT digest;')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['answer'], '{"x": 1}')
        queries = [event[1] for event in orchestrator.events if event[0] == 'query']
        self.assertEqual(queries, ['SELECT pg_is_in_recovery();'] * 3 + ['SELECT digest;'])
        self.assertFalse((data / 'postmaster.pid').exists())
        self.assertEqual([command['argv'][-1] for command in result['stop']['commands']], ['stop'])

    def test_check_output_stops_the_server_when_the_query_fails(self):
        data = self.root / 'O'
        data.mkdir()
        orchestrator = self.server_stub(answers=[run_exp04.CommandError('relation does not exist')])
        result = orchestrator.check_output('pgdelta_direct', data, recovery=False)
        self.assertFalse(result['ok'])
        self.assertIn('relation does not exist', result['error'])
        self.assertFalse((data / 'postmaster.pid').exists())
        # One that never started is not stopped.
        orchestrator = self.server_stub(start_exit=1)
        result = orchestrator.check_output('pgdelta_direct', data, recovery=False)
        self.assertFalse(result['ok'])
        self.assertIn('did not start', result['error'])
        self.assertEqual(result['stop'], {'commands': []})

    def make_tree(self, name, files):
        root = self.root / name
        for path, data in files.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_bytes(data)
        return root

    SOURCE_FILES = {'PG_VERSION': b'17\n', 'global/pg_control': b'C' * 64, 'base/5/16384': b'P' * 8192,
                    'pg_wal/000000010000000000000003': b'W' * 128, 'postmaster.opts': b'opts\n'}
    SELECTED = ['PG_VERSION', 'base/5/16384', 'global/pg_control', 'pg_wal/000000010000000000000003']

    def direct_run(self, method, output_files, *, selected=None):
        source = self.make_tree('src-data', self.SOURCE_FILES)
        output = self.make_tree(f'out-{method}-{len(list(self.root.iterdir()))}', output_files)
        b0 = self.make_tree('B0', {'PG_VERSION': b'17\n', 'global/pg_control': b'old' * 8})

        class Direct(StubOrchestrator):
            verify = run_exp04.Orchestrator.verify

            def check_output(self, method, data, *, recovery, query=run_exp04.OUTPUT_QUERY):
                self.events.append(('check_output', recovery, query))
                return {'ok': True, 'answer': '5', 'ready_seconds': 0.8}

        orchestrator = self.orchestrator(Direct)
        orchestrator.work = self.root  # B0 and the stats under the temporary tree
        orchestrator.b0_digest, orchestrator.b0_stat = measure.tree_digest(b0), measure.tree_stat(b0)
        listed = self.SELECTED if selected is None else selected
        orchestrator.source.agent = lambda *args, timeout=600: {
            'selected': listed, 'digest': measure.files_digest(source, listed)}
        return orchestrator, orchestrator.verify(method, {'output': output}, {})

    def test_direct_output_matches_the_files_the_datadir_mode_selects(self):
        output_files = {path: self.SOURCE_FILES[path] for path in self.SELECTED}
        orchestrator, result = self.direct_run('pgdelta_direct', output_files)
        self.assertTrue(result['verified'], result)
        self.assertTrue(result['identical_to_source'])
        self.assertEqual(result['selected_files'], 4)
        self.assertEqual(result['capture_bytes'], 3 + 64 + 8192 + 128)
        self.assertEqual(result['wal']['wal_bytes'], 128)
        self.assertEqual(result['output_start']['ready_seconds'], 0.8)
        self.assertIn(('check_output', False, run_exp04.OUTPUT_QUERY), orchestrator.events)
        row = measure.summary_row({'method': 'pgdelta_direct', 'verification': result})
        self.assertEqual((row['identical_to_s'], row['output_start_s']), (True, 0.8))
        # A changed page, an extra file and a missing one are all differences.
        for change in ({'base/5/16384': b'X' * 8192}, {'postmaster.opts': b'opts\n'}):
            with self.subTest(change=sorted(change)):
                _, result = self.direct_run('pgdelta_direct', {**output_files, **change})
                self.assertFalse(result['verified'])
                self.assertFalse(result['identical_to_source'])
        _, result = self.direct_run('pgdelta_direct', output_files, selected=[*self.SELECTED, 'global/missing'])
        self.assertFalse(result['identical_to_source'])
        self.assertIn('selected but not a regular file on the source: global/missing', result['differences'])

    def test_direct_in_place_must_leave_no_marker(self):
        output_files = {path: self.SOURCE_FILES[path] for path in self.SELECTED}
        _, result = self.direct_run('pgdelta_direct_inplace', output_files)
        self.assertTrue(result['verified'], result)
        self.assertEqual(result['in_place_leftovers'], [])
        _, result = self.direct_run('pgdelta_direct_inplace', {**output_files, 'pgdelta-incomplete.json': b'{}'})
        self.assertFalse(result['verified'])
        self.assertEqual(result['in_place_leftovers'], ['pgdelta-incomplete.json'])

    def test_staged_in_place_checks_its_leftovers_too(self):
        class Staged(StubOrchestrator):
            verify = run_exp04.Orchestrator.verify

            def local_command(self, argv, log_name, **kwargs):
                return {'host': 'dest', 'argv': [str(a) for a in argv], 'exit_code': 0, 'wall_seconds': 0.1}

        capture = self.make_tree('S', {'PG_VERSION': b'17\n', 'global/pg_control': b'C' * 8})
        output = self.make_tree('B', {'PG_VERSION': b'17\n', 'global/pg_control': b'C' * 8,
                                      'global/pg_control.pgdelta': b'C' * 8})
        orchestrator = self.orchestrator(Staged)
        orchestrator.b0_digest, orchestrator.b0_stat = {'entries': {}, 'device': -1}, {}
        orchestrator.source.agent = lambda *args, timeout=600: measure.tree_digest(capture)
        result = orchestrator.verify('pgdelta_inplace', {'output': output, 'capture': str(capture)}, {})
        self.assertEqual(result['in_place_leftovers'], ['global/pg_control.pgdelta'])
        self.assertFalse(result['verified'])

    def test_in_place_leftovers(self):
        def digest(*paths):
            return {'entries': {path: ['file', 1, 'x', 0o600, 1, 1] for path in paths}}
        self.assertEqual(measure.in_place_leftovers(digest('PG_VERSION', 'global/pg_control')), [])
        self.assertEqual(measure.in_place_leftovers(digest('global/pg_control', 'pgdelta-incomplete.json',
                                                           'global/pg_control.incomplete', 'base/x.pgdelta-tmp')),
                         ['base/x.pgdelta-tmp', 'global/pg_control.incomplete', 'pgdelta-incomplete.json'])
        self.assertEqual(measure.in_place_leftovers(digest('PG_VERSION', 'global/pgdelta_pg_control')),
                         ['global/pgdelta_pg_control', 'global/pg_control is missing'])

    def test_files_digest_and_the_datadir_list(self):
        root = self.make_tree('d', {'a': b'1', 'b/c': b'22'})
        (root / 'link').symlink_to('a')
        digest = measure.files_digest(root, ['b/c', 'a', 'link', 'gone', 'a'])
        self.assertEqual(sorted(digest['entries']), ['a', 'b/c'])
        self.assertEqual(digest['missing'], ['gone', 'link'])
        self.assertEqual((digest['files'], digest['bytes']), (2, 3))
        self.assertEqual(digest['entries']['b/c'][:3], measure.tree_digest(root)['entries']['b/c'][:3])
        self.assertEqual(measure.files_of(measure.tree_digest(root))['files'], 2)
        self.assertEqual(measure.parse_datadir_list('["a", "b/c"]'), ['a', 'b/c'])
        self.assertEqual(measure.parse_datadir_list('{"files": ["a"], "bytes": 1}'), ['a'])
        self.assertEqual(measure.parse_datadir_list('reading /x\n["a"]\n'), ['a'])
        for bad in ('', '{"x": 1}', '[1, 2]'):
            with self.subTest(text=bad), self.assertRaises(ValueError):
                measure.parse_datadir_list(bad)

    def test_agent_hashes_what_the_datadir_mode_lists(self):
        root = self.make_tree('data', {'PG_VERSION': b'17\n', 'postmaster.pid': b'1\n'})
        listing = mock.Mock(returncode=0, stdout='["PG_VERSION"]\n', stderr='')
        with mock.patch.object(agent.subprocess, 'run', return_value=listing) as run:
            result = agent.cmd_datadir_digest(Namespace(root=str(root), pg_controldata='/b/pg_controldata'))
        argv = run.call_args.args[0]
        self.assertEqual(argv[1:], ['-m', 'pgdelta.datadir', 'list', str(root), '--pg-controldata', '/b/pg_controldata'])
        self.assertEqual(result['selected'], ['PG_VERSION'])
        self.assertEqual(list(result['digest']['entries']), ['PG_VERSION'])
        with mock.patch.object(agent.subprocess, 'run', return_value=mock.Mock(returncode=2, stdout='', stderr='bad')), \
                self.assertRaises(SystemExit):
            agent.cmd_datadir_digest(Namespace(root=str(root), pg_controldata='/b/pg_controldata'))
        self.assertIs(agent.xfs_reflink('meta-data=/dev/md127 isize=512\n= crc=1 finobt=1, sparse=1, rmapbt=0\n'
                                        '= reflink=1 bigtime=1 inobtcount=1'), True)
        self.assertIs(agent.xfs_reflink('= reflink=0'), False)
        self.assertIsNone(agent.xfs_reflink(None))


def fiemap_reply(*extents):
    """An FS_IOC_FIEMAP reply buffer holding (logical, length, flags) extents."""
    buffer = bytearray(measure.FIEMAP_HEADER.size + len(extents) * measure.FIEMAP_EXTENT.size)
    measure.FIEMAP_HEADER.pack_into(buffer, 0, 0, 0, 0, len(extents), len(extents), 0)
    for index, (logical, length, flags) in enumerate(extents):
        measure.FIEMAP_EXTENT.pack_into(buffer, measure.FIEMAP_HEADER.size + index * measure.FIEMAP_EXTENT.size,
                                        logical, 4096 * index, length, 0, 0, flags, 0, 0, 0)
    return buffer


class SharedExtents(unittest.TestCase):
    """FIEMAP's shared flag: whether a copy is a reflink clone (XFS) or real bytes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'base').mkdir()
        (self.root / 'base' / '16384').write_bytes(b'x' * 100)
        (self.root / 'PG_VERSION').write_bytes(b'17\n')

    def test_layout_and_parse(self):
        # The structures' sizes match the ioctl number's size field (0x20) and the kernel's 56-byte extent.
        self.assertEqual(measure.FIEMAP_HEADER.size, 32)
        self.assertEqual(measure.FIEMAP_EXTENT.size, 56)
        self.assertEqual((measure.FS_IOC_FIEMAP >> 16) & 0x3fff, measure.FIEMAP_HEADER.size)
        reply = fiemap_reply((0, 65536, measure.FIEMAP_EXTENT_SHARED), (65536, 4096, measure.FIEMAP_EXTENT_LAST))
        self.assertEqual(measure.parse_fiemap(reply), [(0, 65536, 0x2000), (65536, 4096, 0x1)])

    def test_shared_bytes_follow_the_flag_across_batches(self):
        replies = [fiemap_reply((0, 8192, measure.FIEMAP_EXTENT_SHARED), (8192, 8192, 0)),
                   fiemap_reply((16384, 4096, measure.FIEMAP_EXTENT_SHARED | measure.FIEMAP_EXTENT_LAST))]
        starts = []

        def ioctl(fd, request, buffer):
            self.assertEqual(request, measure.FS_IOC_FIEMAP)
            starts.append(measure.FIEMAP_HEADER.unpack_from(buffer, 0)[0])
            reply = replies.pop(0)
            buffer[:len(reply)] = reply
            return 0

        with mock.patch.object(measure.fcntl, 'ioctl', ioctl):
            self.assertEqual(measure.shared_extent_bytes(self.root / 'PG_VERSION'), 12288)
        self.assertEqual(starts, [0, 16384])  # the second call starts where the first batch ended

    def test_a_tree_that_shares_blocks_and_one_that_does_not(self):
        def clone(fd, request, buffer):
            buffer[:] = fiemap_reply((0, 4096, measure.FIEMAP_EXTENT_SHARED | measure.FIEMAP_EXTENT_LAST)) \
                + bytes(len(buffer) - measure.FIEMAP_HEADER.size - measure.FIEMAP_EXTENT.size)
            return 0

        def copy(fd, request, buffer):
            buffer[:measure.FIEMAP_HEADER.size + measure.FIEMAP_EXTENT.size] = fiemap_reply(
                (0, 4096, measure.FIEMAP_EXTENT_LAST))
            return 0

        with mock.patch.object(measure.fcntl, 'ioctl', clone):
            found = measure.shared_extents(self.root)
        self.assertEqual(found, {'supported': True, 'error': None, 'files': 2, 'shared_files': 2,
                                 'shared_bytes': 8192, 'examples': ['PG_VERSION', 'base/16384']})
        with mock.patch.object(measure.fcntl, 'ioctl', copy):
            self.assertEqual(measure.shared_extents(self.root)['shared_files'], 0)

    def test_without_fiemap_nothing_is_claimed(self):
        with mock.patch.object(measure.fcntl, 'ioctl', side_effect=OSError(95, 'Operation not supported')):
            found = measure.shared_extents(self.root)
        self.assertIs(found['supported'], False)
        self.assertIsNone(found['shared_files'])
        self.assertIn('not supported', found['error'])
        self.assertIsNone(measure.shared_extents(self.root / 'missing')['supported'])

    def test_the_csv_reports_the_outputs_shared_bytes(self):
        record = {'verification': {'shared_extents': {'shared_bytes': 2_000_000_000}}}
        self.assertEqual(measure.summary_row(record)['output_shared_bytes'], 2_000_000_000)
        self.assertIsNone(measure.summary_row({})['output_shared_bytes'])


class LogicalDigest(unittest.TestCase):
    def test_tables_are_exp01s(self):
        from experiments.change_patterns import run_study
        self.assertEqual(tuple(table for table, _ in measure.LOGICAL_TABLES), run_study.USER_TABLES)

    def test_query_shape(self):
        sql = measure.logical_digest_sql()
        self.assertTrue(sql.startswith('SELECT json_build_object('))
        for table, key in measure.LOGICAL_TABLES:
            self.assertIn(f"'{table}', (SELECT json_build_object(", sql)
            if key:
                self.assertIn(f'{key} / 65536 AS b', sql)
                self.assertIn(f"md5(string_agg(md5(t::text), '' ORDER BY {key}))", sql)
        self.assertIn("string_agg(md5(r), '' ORDER BY r)", measure.table_digest_sql('pgbench_history', None))
        self.assertIn('TimeZone=UTC', measure.LOGICAL_SESSION_OPTIONS)

    def test_compare(self):
        self.assertEqual(measure.compare_logical(LOGICAL, json.loads(json.dumps(LOGICAL))), (True, []))
        changed = json.loads(json.dumps(LOGICAL))
        changed['pgbench_accounts']['md5'] = 'f' * 32
        del changed['events']
        same, differences = measure.compare_logical(LOGICAL, changed)
        self.assertFalse(same)
        self.assertEqual([difference.split(':')[0] for difference in differences], ['events', 'pgbench_accounts'])
        # Counts are compared as numbers: sum() comes back as JSON 5 or "5" alike.
        text_counts = {table: {**value, 'rows': str(value['rows'])} for table, value in LOGICAL.items()}
        self.assertTrue(measure.compare_logical(LOGICAL, text_counts)[0])


class PgBackRestConfig(unittest.TestCase):
    work = '/data/exp04'

    def parse(self, text):
        import configparser
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string(text)
        return parser

    def test_source_config_has_a_stanza_and_a_repository_per_variant(self):
        text = measure.pgbackrest_source_config(self.work, measure.PGBACKREST_METHODS,
                                                pg_path='/data/exp04/src/data', socket_path='/data/exp04/src/s',
                                                port=5432, user='study')
        config = self.parse(text)
        self.assertEqual(config.sections(), ['global', *measure.PGBACKREST_METHODS])
        self.assertEqual(dict(config['global']),
                         {'log-path': '/data/exp04/pgbackrest/log', 'lock-path': '/data/exp04/pgbackrest/lock',
                          'spool-path': '/data/exp04/pgbackrest/spool', 'log-level-console': 'info',
                          'log-level-file': 'detail', 'start-fast': 'y', 'process-max': '4',
                          'repo1-retention-full': '1'})
        common = {'pg1-path': '/data/exp04/src/data', 'pg1-socket-path': '/data/exp04/src/s', 'pg1-port': '5432',
                  'pg1-user': 'study'}
        self.assertEqual(dict(config['pgbackrest_zst']),
                         {**common, 'repo1-path': '/data/exp04/pgbackrest/repo-pgbackrest_zst', 'compress-type': 'zst'})
        self.assertEqual(dict(config['pgbackrest_none']),
                         {**common, 'repo1-path': '/data/exp04/pgbackrest/repo-pgbackrest_none', 'compress-type': 'none'})
        self.assertEqual(dict(config['pgbackrest_block']),
                         {**common, 'repo1-path': '/data/exp04/pgbackrest/repo-pgbackrest_block', 'compress-type': 'zst',
                          'repo1-bundle': 'y', 'repo1-block': 'y'})
        # Only the selected variants.
        only = self.parse(measure.pgbackrest_source_config(self.work, ['pgbackrest_none'], pg_path='p', socket_path='s',
                                                           port=5432, user='study'))
        self.assertEqual(only.sections(), ['global', 'pgbackrest_none'])

    def test_dest_config_reaches_the_repository_over_ssh_with_the_harness_key(self):
        config = self.parse(measure.pgbackrest_dest_config(self.work, ['pgbackrest_zst'], source_ip='10.0.0.12',
                                                           ssh_user='bench'))
        self.assertEqual(config['global']['repo1-host'], '10.0.0.12')
        self.assertEqual(config['global']['repo1-host-user'], 'bench')
        self.assertEqual(config['global']['repo1-host-config'], '/data/exp04/pgbackrest/pgbackrest.conf')
        self.assertEqual(config['global']['cmd-ssh'], '/data/exp04/pgbackrest/ssh')
        self.assertEqual(dict(config['pgbackrest_zst']), {'repo1-path': '/data/exp04/pgbackrest/repo-pgbackrest_zst'})
        self.assertNotIn('pg1-path', config['pgbackrest_zst'])  # given on the restore's command line
        ssh = measure.pgbackrest_ssh_config('10.0.0.12', 'bench', '/home/bench/.ssh/exp04_ed25519')
        for line in ('Host 10.0.0.12', '    User bench', '    IdentityFile /home/bench/.ssh/exp04_ed25519',
                     '    BatchMode yes', '    StrictHostKeyChecking accept-new', '    IdentitiesOnly yes'):
            self.assertIn(line + '\n', ssh)
        wrapper = measure.pgbackrest_ssh_wrapper('/data/exp04/pgbackrest/ssh_config')
        self.assertTrue(wrapper.startswith('#!/bin/sh\n'))
        self.assertTrue(wrapper.endswith('exec ssh -F /data/exp04/pgbackrest/ssh_config "$@"\n'))

    def test_archive_and_restore_commands(self):
        self.assertEqual(measure.pgbackrest_archive_command('/usr/bin/pgbackrest', '/c', ['pgbackrest_zst']),
                         '/usr/bin/pgbackrest --config=/c --stanza=pgbackrest_zst archive-push %p')
        self.assertEqual(measure.pgbackrest_restore_argv('/usr/bin/pgbackrest', '/c', 'pgbackrest_none', '/o/B'),
                         ['/usr/bin/pgbackrest', '--config=/c', '--stanza=pgbackrest_none', '--delta',
                          '--process-max=4', '--pg1-path=/o/B', 'restore'])
        lines = source_cluster.archive_lines("a 'b' && c")
        self.assertEqual(lines, ['archive_mode = on', "archive_command = 'a ''b'' && c'"])

    def test_restore_log_and_backup_info(self):
        parsed = measure.parse_pgbackrest_restore(PGBACKREST_RESTORE_LOG)
        self.assertEqual(parsed, {'version': '2.54.0', 'backup_set': '20261023-120000F', 'restore_size': '2.1GB',
                                  'file_total': 1012, 'file_lines': 3, 'files_matched': 2, 'completed': True,
                                  'reported_ms': 3301, 'statistics': {'socket.client': {'total': 5}}})
        empty = measure.parse_pgbackrest_restore('')
        self.assertIsNone(empty['completed'])
        self.assertIsNone(empty['files_matched'])
        aborted = measure.parse_pgbackrest_restore('P00   INFO: restore command end: aborted with exception [075]')
        self.assertIs(aborted['completed'], False)
        summary = measure.pgbackrest_backup_summary(PGBACKREST_INFO)
        self.assertEqual(summary, {'stanza': 'pgbackrest_zst', 'label': '20261023-120000F', 'type': 'full',
                                   'size_bytes': 2_112_000_000, 'delta_bytes': 2_112_000_000,
                                   'repo_size_bytes': 310_000_000, 'repo_delta_bytes': 310_000_000,
                                   'start': 1761220800, 'stop': 1761220830})
        self.assertIsNone(measure.pgbackrest_backup_summary('[{"name": "s", "backup": []}]')['backup'])


class SourceClusterCommands(unittest.TestCase):
    def test_start_and_stop_argv(self):
        self.assertEqual(source_cluster.start_argv('/w/src', '/b', '/w/logs/server.log'),
                         ['/b/pg_ctl', '-D', '/w/src/data', '-l', '/w/logs/server.log', '-o', '-k /w/src/s -p 5432',
                          '-w', '-t', '600', 'start'])
        self.assertEqual(source_cluster.stop_argv('/w/src', '/b'),
                         ['/b/pg_ctl', '-D', '/w/src/data', '-m', 'fast', '-w', '-t', '600', 'stop'])

    def test_a_stop_that_does_not_end_shut_down_fails(self):
        args = Namespace(root='/w/src', bin_dir='/b')
        results = [mock.Mock(returncode=0, stdout='', stderr=''),
                   mock.Mock(returncode=0, stdout='Database cluster state:               in production\n')]
        with mock.patch.object(source_cluster.subprocess, 'run', side_effect=results), \
                self.assertRaises(SystemExit):
            source_cluster.cmd_stop_clean(args)
        results = [mock.Mock(returncode=0, stdout='', stderr=''),
                   mock.Mock(returncode=0, stdout='Database cluster state:               shut down\n')]
        with mock.patch.object(source_cluster.subprocess, 'run', side_effect=results):
            self.assertEqual(source_cluster.cmd_stop_clean(args)['state'], 'shut down')

    def test_start_leaves_a_running_cluster_be(self):
        with mock.patch.object(source_cluster.subprocess, 'run',
                               return_value=mock.Mock(returncode=0, stdout='server is running', stderr='')) as run:
            result = source_cluster.cmd_start(Namespace(root='/w/src', bin_dir='/b', log_dir='/w/logs'))
        self.assertEqual(result['already_running'], True)
        self.assertEqual(run.call_args.args[0], ['/b/pg_ctl', '-D', '/w/src/data', 'status'])


class InfraScripts(unittest.TestCase):
    SCRIPT = Path(__file__).resolve().parents[1] / 'infra' / 'gcp' / 'reformat_dest_xfs.sh'

    def run_script(self, *args, host='pgd-dest', uid='0'):
        """The script with stand-ins for id and hostname, and every destructive command refusing."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp)
            stubs = {'id': f'echo {uid}', 'hostname': f'echo {host}'}
            stubs.update({name: 'echo "$0 must not run" >&2; exit 99'
                          for name in ('umount', 'mkfs.xfs', 'mount', 'chown', 'chmod', 'wipefs')})
            for name, body in stubs.items():
                path = bin_dir / name
                path.write_text(f'#!/bin/sh\n{body}\n')
                path.chmod(0o755)
            env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'}
            return subprocess.run(['bash', str(self.SCRIPT), *args], capture_output=True, text=True,
                                  env=env, timeout=60)

    def test_it_refuses_without_yes_root_or_the_destination(self):
        self.assertTrue(os.access(self.SCRIPT, os.X_OK))
        cases = ((('',), {}, 'usage'), (('--yes',), {'uid': '501'}, 'run as root'),
                 (('--yes',), {'host': 'pgd-source'}, 'refusing to run on pgd-source'),
                 (('--yes',), {'host': 'laptop'}, 'for pgd-dest only'))
        for args, kwargs, message in cases:
            with self.subTest(message=message):
                result = self.run_script(*[arg for arg in args if arg], **kwargs)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(message, result.stderr)
                self.assertNotIn('must not run', result.stderr)

    def test_it_refuses_without_the_array(self):
        if Path('/dev/md/pgdata').exists():
            self.skipTest('this machine has the array')
        result = self.run_script('--yes')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('no md array', result.stderr)
        self.assertNotIn('must not run', result.stderr)

    def test_startup_installs_pgbackrest_and_xfsprogs(self):
        text = (self.SCRIPT.parent / 'startup.sh').read_text()
        self.assertIn('postgresql-17 postgresql-client-17 pgbackrest xfsprogs', text)
        self.assertIn('install --no-install-recommends pgbackrest xfsprogs', text)

    def test_remote_provision_runs_this_trees_startup_script_on_both(self):
        vms = {'source': {'external': '203.0.113.1'}, 'dest': {'external': '203.0.113.2'}}
        calls = []

        def ssh(config, address, command, **kwargs):
            calls.append((address, command, kwargs['input_bytes']))
            return mock.Mock(stdout=b'pgd startup done\n')

        with mock.patch.object(remote, 'instances', return_value=vms), mock.patch.object(remote, 'ssh', ssh), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(remote.cmd_provision({}, Namespace()), 0)
        script = (self.SCRIPT.parent / 'startup.sh').read_bytes()
        self.assertEqual([(address, payload) for address, _, payload in calls],
                         [('203.0.113.1', script), ('203.0.113.2', script)])
        self.assertIn('sudo bash "$f" < /dev/null', calls[0][1])


class NewMethodReport(unittest.TestCase):
    def rows(self):
        def row(speed, gbit, method, total, **extra):
            return {'trial_id': 't0', 'speed': speed, 'target_gbit': gbit, 'method': method, 'total_s': total,
                    'status': 'pass', 'measured_s2d_gbit': None, 'error': '', **extra}
        return [row('5g', 5.0, 'full', 5.0, transfer_s=5.0),
                row('5g', 5.0, 'pgdelta', 6.0, receiver_start_s=0.2, capture_s=2.0, transfer_s=4.0, rebuild_s=3.3,
                    fsync_s=1.7, serial_total_s=6.2),
                row('5g', 5.0, 'pgdelta_direct', 4.8, receiver_start_s=0.3, transfer_s=4.5, rebuild_s=3.4,
                    fsync_s=1.7),
                row('5g', 5.0, 'pgdelta_fsync_worker', 4.9, receiver_start_s=0.2, capture_s=2.0, transfer_s=2.9,
                    fsync_wait_s=0.3, serial_total_s=5.1),
                row('5g', 5.0, 'pgbackrest_zst', 3.0, restore_s=3.0),
                row('5g', 5.0, 'pgbackrest_none', 9.0, restore_s=9.0, status='pending')]

    def test_ratios_and_phases_of_the_new_methods(self):
        text = report.render(self.rows())
        self.assertIn('| pgdelta_direct | 0.96 | never (faster at every speed) |', text)
        self.assertIn('| pgbackrest_zst | 0.60 | never (faster at every speed) |', text)
        self.assertIn('| pgdelta_fsync_worker (serial) | 1.02 | 5g |', text)
        self.assertIn('| pgbackrest_none |  | n/a |', text)  # pending: not in the medians
        self.assertIn('pgbackrest_none: pending', text)
        phases = text.split('Median timed phases')[1]
        self.assertIn('| method | speed | runs | receiver_start_s | capture_s | transfer_s | restore_s | rebuild_s | '
                      'fsync_s | fsync_wait_s | total_s |', phases)
        self.assertIn('| pgdelta_direct | 5g | 1 | 0.30 |  | 4.50 |  | 3.40 | 1.70 |  | 4.80 |', phases)
        self.assertIn('| pgbackrest_zst | 5g | 1 |  |  |  | 3.00 |  |  |  | 3.00 |', phases)
        self.assertIn('restore alone', text)

    def test_new_csv_columns(self):
        record = {'method': 'pgdelta_fsync_wb', 'preparation': {'wall_seconds': 0.4},
                  'phases': {'receiver_capture': {'wall_seconds': 1.1, 'parts': {'receiver': {'wall_seconds': 0.2}}},
                             'transfer': {'wall_seconds': 3.0}},
                  'tool_reported': {'timings_seconds': {'rebuild': 2.0, 'fsync': 0.1, 'verify_wait': 0.05},
                                    'fsync_wait_seconds': 0.6,
                                    'modes': {'in_place': False, 'fsync_mode': 'writeback', 'source_kind': 'backup'}},
                  'verification': {'identical_to_capture': True}}
        row = measure.summary_row(record)
        self.assertEqual(set(row), set(measure.CSV_FIELDS))
        self.assertEqual((row['rebuild_s'], row['fsync_s'], row['fsync_wait_s'], row['verify_wait_s']),
                         (2.0, 0.1, 0.6, 0.05))
        self.assertEqual((row['in_place'], row['fsync_mode'], row['source_kind']), (False, 'writeback', 'backup'))
        self.assertEqual((row['receiver_start_s'], row['untimed_setup_s'], row['restore_s']), (0.2, 0.4, None))
        for column in ('restore_s', 'untimed_setup_s', 'rebuild_s', 'fsync_s', 'fsync_wait_s', 'verify_wait_s',
                       'in_place', 'fsync_mode', 'source_kind', 'logical_match', 'output_start_s'):
            self.assertIn(column, measure.CSV_FIELDS)
        for column in ('restore_s', 'untimed_setup_s', 'rebuild_s', 'fsync_s', 'fsync_wait_s', 'output_start_s'):
            self.assertIn(column, report.NUMERIC)


PG_BIN = Path(os.environ.get('PGDELTA_TEST_PG_BIN', '/opt/homebrew/opt/postgresql@17/bin'))


@unittest.skipUnless(all((PG_BIN / tool).exists() for tool in ('initdb', 'pg_ctl', 'psql', 'pgbench')),
                     f'PostgreSQL binaries not found in {PG_BIN}')
class OutputOnACluster(unittest.TestCase):
    """The start of an output and the logical digest, on a real PostgreSQL."""

    def test_the_output_starts_despite_the_sources_settings_and_its_digest_follows_its_rows(self):
        from experiments.change_patterns import workloads
        tmp = tempfile.mkdtemp(prefix='e4-')
        self.addCleanup(shutil.rmtree, tmp, True)

        class Real(StubOrchestrator):
            local_command = run_exp04.Orchestrator.local_command

        orchestrator = Real(tmp, FakeClock(), ['--bin-dir', str(PG_BIN), '--gnu-time', 'none'])
        self.addCleanup(orchestrator.pool.shutdown)
        data = Path(tmp) / 'out' / 'pgdelta_direct'
        subprocess.run([str(PG_BIN / 'initdb'), '-D', str(data), '-U', 'study', '--auth=trust', '--no-sync',
                        '-E', 'UTF8', '--locale=C'], check=True, capture_output=True)
        # The source's settings, which would stop it starting here or make it archive.
        with open(data / 'postgresql.conf', 'a') as stream:
            stream.write("listen_addresses = '192.0.2.1'\narchive_mode = on\narchive_command = 'false'\n")
        settings = ("SELECT current_setting('archive_mode') || ',' || current_setting('listen_addresses') || ',' "
                    "|| current_setting('port');")
        with mock.patch.object(run_exp04, 'log'):
            result = orchestrator.check_output('pgdelta_direct', data, recovery=False, query=settings)
        self.assertTrue(result['ok'], (result, orchestrator.dest_log('pgdelta_direct-output-server.log').read_text()))
        self.assertEqual(result['answer'], 'off,,5433')
        self.assertFalse((data / 'postmaster.pid').exists())
        # Tables to digest: pgbench at scale 1 and a few events.
        start = orchestrator.output_start_argv(data, Path(tmp) / 'load.log')
        subprocess.run([str(part) for part in start], check=True, capture_output=True, env=run_exp04.server_env())
        try:
            socket_dir = str(orchestrator.output_socket)
            subprocess.run([str(PG_BIN / 'pgbench'), '-i', '-q', '-s', '1', '-h', socket_dir, '-p', '5433',
                            '-U', 'study', 'postgres'], check=True, capture_output=True)
            ddl = workloads.EVENTS_DDL + '\n' + workloads.events_prefill_sql(1000)
            subprocess.run([str(PG_BIN / 'psql'), '-X', '-q', '-v', 'ON_ERROR_STOP=1', '-h', socket_dir, '-p', '5433',
                            '-U', 'study', '-d', 'postgres', '-c', ddl], check=True, capture_output=True)

            def digest():
                return json.loads(orchestrator.query(measure.logical_digest_sql(), host=socket_dir, port=5433,
                                                     log_name='digest.log'))

            first = digest()
            self.assertEqual({table: int(value['rows']) for table, value in first.items()},
                             {'pgbench_accounts': 100000, 'pgbench_branches': 1, 'pgbench_tellers': 10,
                              'pgbench_history': 0, 'events': 1000})
            self.assertEqual(digest(), first)
            orchestrator.query('UPDATE pgbench_accounts SET abalance = 1 WHERE aid = 70000;', host=socket_dir,
                               port=5433, log_name='update.log')
            second = digest()
            same, differences = measure.compare_logical(first, second)
            self.assertFalse(same)
            self.assertEqual([difference.split(':')[0] for difference in differences], ['pgbench_accounts'])
        finally:
            orchestrator.stop_output('pgdelta_direct', data)
        self.assertFalse((data / 'postmaster.pid').exists())


if __name__ == '__main__':
    unittest.main()
