"""A synthetic EXP-13 session, for dry runs of analysis/exp13.py: what `remote.py fetch` brings back.

write_session() writes the three invocations of the specification's run lines (uniform 1%, hot, uniform 10%), each as
run_exp04.py lays it out: runs/<trial>/r<repeat>-<slug>-<method>.json, summary.csv (measure.write_csv over those
records, so every column is the harness's), trials/<trial>.json (each visit, the first-visit probes, the reference
digests by file class) and run.json. The schedule is the run lines' own (measure.t12_schedule on run_exp04's plan),
so every arm runs where and as often as the design says.

The times follow the pre-registration's expectations (E4-E11) with seeded noise of about 1%; the link tests follow its
window model (W about 2.9 MB per default stream); pgdelta's timers split each v1 run's added time into idle waits,
fixed round trips and sending. Nothing here is a measurement. A test changes what it needs through `adjust`, called
on each run record before it is written (the record's network checks are recomputed after it), and `adjust_trial`, on
each trial record.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from experiments.network import measure, run_exp04

from test_exp13_core import DEBIAN_TCP, parse, spec_run_lines

V1 = 'pgdelta_direct_inplace'
W1, W16, HC, TLS = measure.T12_V1_ARMS
INVOCATIONS = ('exp13-uniform1', 'exp13-hot', 'exp13-uniform10')
WORKLOAD = {'exp13-uniform1': ('uniform', 0.01), 'exp13-hot': ('hot', 0.01), 'exp13-uniform10': ('uniform', 0.1)}
NAME = {('uniform', 0.01): 'uniform1', ('hot', 0.01): 'hot', ('uniform', 0.1): 'uniform10'}
LINK = {2: 1.92, 1: 0.96}  # the link's payload, Gbit/s
WINDOW = 2.9e6  # what one default stream holds in flight
TUNED_MAX = 128 << 20
COPY_BYTES = 2_110_000_000
V1_WIRE = {'hot': 29_200_000, 'uniform1': 610_400_000, 'uniform10': 1_131_000_000}
PAGE_BYTES = {'hot': 28_100_000, 'uniform1': 606_000_000, 'uniform10': 1_125_000_000}
PREFETCH = {'hot': 150_000_000, 'uniform1': 120_000_000, 'uniform10': 110_000_000}
# Medians, seconds: the baselines by condition, v1 by workload and condition (E4-E8's middles).
BASELINE_TIMES = {
    'full': {'2:0:default': 10.30, '2:20:default': 16.4, '2:60:default': 45.9, '2:120:default': 90.7,
             '2:0:tuned': 10.35, '2:60:tuned': 11.3, '2:120:tuned': 12.4},
    'parcopy8': {'2:0:default': 9.30, '2:20:default': 9.55, '2:60:default': 9.85, '2:120:default': 12.3,
                 '2:60:tuned': 9.9, '2:120:tuned': 10.3, '1:60:default': 18.6, '1:120:default': 19.1},
    'parcopy16': {'2:120:default': 10.3},
    'probackup8': {'2:60:default': 40.0},
    'full_nosync': {'2:0:default': 9.0},
}
V1_TIMES = {
    'hot': {'2:0:default': 3.10, '2:20:default': 3.45, '2:60:default': 4.30, '2:120:default': 5.60,
            '2:0:tuned': 3.12, '2:60:tuned': 4.10, '2:120:tuned': 5.20},
    'uniform1': {'2:0:default': 5.30, '2:20:default': 7.40, '2:60:default': 16.8, '2:120:default': 30.4,
                 '2:0:tuned': 5.32, '2:60:tuned': 6.80, '2:120:tuned': 8.30, '1:60:default': 16.9,
                 '1:120:default': 30.5},
    'uniform10': {'2:0:default': 8.00, '2:20:default': 11.4, '2:60:default': 28.0, '2:0:tuned': 8.02,
                  '2:60:tuned': 9.30, '2:120:tuned': 10.9},
}
# The side arms against v1 in their cell, seconds (E9-E11).
SIDE = {W1: {'hot': 0.8, 'uniform1': 0.4}, W16: {'hot': -0.2, 'uniform1': -0.1},
        HC: {'uniform1': 2.1, 'uniform10': -0.1}, TLS: {'hot': 0.2, 'uniform1': 0.25}}
TLS_ANCHOR = {'hot': 0.05, 'uniform1': 0.1}
HC_BYTES = {'uniform1': 505_000_000, 'uniform10': 2_000_000}
SENDER, RECEIVER = 'source:50000', 'dest:7000'  # the sending socket's ends, as the ss reading names them


def condition(label):
    return measure.parse_condition(label)


def one_stream(c, host=None) -> float:
    """One stream's steady rate, Gbit/s: the link at r = 0 or on tuned hosts, else W / r up to the link."""
    link = LINK[int(c['gbit'])]
    if c['rtt_ms'] == 0 or (host or c['host_tcp']) == 'tuned':
        return round(link - 0.005, 4)
    return round(min(link, WINDOW / (c['rtt_ms'] / 1000) * 8 / 1e9), 4)


def streams_rate(c, streams) -> float:
    link = LINK[int(c['gbit'])]
    return round(min(link - 0.003, streams * one_stream(c)), 4)


def tcp_values(host_tcp) -> dict:
    values = {name: str(value) for name, value in DEBIAN_TCP.items()}
    if host_tcp == 'tuned':
        values['net.ipv4.tcp_rmem'] = f'4096\t131072\t{TUNED_MAX}'
        values['net.ipv4.tcp_wmem'] = f'4096\t16384\t{TUNED_MAX}'
    return values


def link_test(direction, streams, gbit, qdisc=False) -> dict:
    per_second = [round(gbit * f, 4) for f in (0.6, 0.9, 0.98, 1.0, 1.0, 1.0)]
    test = {'direction': direction, 'streams': streams, 'gbit': round(gbit - 0.004, 4), 'steady_gbit': gbit,
            'per_second_gbit': per_second, 'connect_s': 0.001, 'per_stream_connect_s': [0.001] * streams,
            'tcp_metrics': {'source': {'flushed': True}, 'dest': {'flushed': True}}}
    if qdisc:
        counters = {'bytes': 1_400_000_000, 'packets': 960_000, 'drops': 12, 'overlimits': 4000, 'requeues': 0}
        tbf = dict(counters, drops=12)
        test['qdisc'] = {vm: {'before': {}, 'after': {}, 'delta': {'same_tree': True, 'netem': counters, 'tbf': tbf,
                                                                   'root': counters, 'plain': None,
                                                                   'netem_own_drops': 0},
                              'samples': {'samples': 12, 'with_tree': 12, 'netem_qlen_max': 900,
                                          'netem_backlog_max_bytes': 1_300_000, 'limit': 1_000_000,
                                          'at_limit': False}} for vm in ('source', 'dest')}
    return test


class Builder:
    """One invocation's records."""

    def __init__(self, name, argv, rng, out: Path, smoke=False):
        self.name, self.rng, self.out, self.smoke = name, rng, Path(out), smoke
        self.args = parse(*argv)  # the core tests' parser: their source address, a scratch --out
        self.methods = run_exp04.method_list(self.args.methods)
        self.conditions = measure.parse_conditions(self.args.conditions)
        self.plan = run_exp04.t12_plan(self.args)
        self.schedule = measure.t12_schedule(self.methods, self.conditions, self.args.repeats, self.plan)
        if smoke:  # every arm once at every condition its line runs it at, in one repeat
            self.schedule = [(1, label, method) for label, method in
                             dict.fromkeys((label, method) for _, label, method in self.schedule)]
        self.pattern, self.fraction = WORKLOAD[name]
        self.workload = NAME[(self.pattern, self.fraction)]
        self.trial_id = f'00-{self.pattern}-f{self.fraction:g}-s70'
        self.by_label = {c['label']: c for c in self.conditions}

    def noise(self, value, share=0.01):
        return value * (1 + self.rng.uniform(-share, share))

    def time(self, method, label) -> float:
        if method in BASELINE_TIMES:
            base = BASELINE_TIMES[method][label]
        else:
            base = V1_TIMES[self.workload][label]
            if method in SIDE:
                base += (TLS_ANCHOR if method == TLS and label == '2:0:default' else SIDE[method])[self.workload]
        return round(self.noise(base), 4)

    def link(self, c) -> dict:
        """What the run's record keeps of its visit's link tests (keys ending in gbit)."""
        zero = LINK[int(c['gbit'])] - 0.005
        link = {'source_to_dest_gbit': round(one_stream(c) - 0.004, 4), 'source_to_dest_steady_gbit': one_stream(c),
                'dest_to_source_gbit': one_stream(c), 'dest_to_source_steady_gbit': one_stream(c),
                'source_to_dest_8streams_gbit': round(streams_rate(c, 8) * 0.95, 4),
                'source_to_dest_8streams_steady_gbit': streams_rate(c, 8), 'zero_delay_gbit': zero,
                'zero_delay_bound_gbit': zero}
        if c['label'] == '2:120:default':
            link.update(source_to_dest_16streams_gbit=round(streams_rate(c, 16) * 0.95, 4),
                        source_to_dest_16streams_steady_gbit=streams_rate(c, 16))
        return link

    def timers(self, method, c, total) -> dict:
        """pgdelta's statistics as tool_reported carries them: the sender's waits (busy and idle), the receiver's."""
        r = c['rtt_ms'] / 1000
        anchor = V1_TIMES[self.workload][f'2:0:{c["host_tcp"]}'] if c['gbit'] == 2 else total
        added = max(0.0, total - anchor)
        exposed = {'hot': 4, 'uniform1': 3, 'uniform10': 3}[self.workload] * (3 if method == W1 else 1)
        idle_leaf = exposed * r
        idle_basis = 0.5 * r
        fixed = (r + 0.0003, r + 0.001, r + 0.002, r + 0.012)  # connect, accept idle, complete idle, drain
        busy = 0.01 + max(0.0, added - idle_leaf - idle_basis - sum(fixed) - 0.02) * 0.4
        page_send = 0.2 + max(0.0, added - idle_leaf - idle_basis - sum(fixed) - 0.02) * 0.6
        report = {'connect_s': round(fixed[0], 6),
                  'accept_wait_s': round(fixed[1] + 0.002, 6), 'accept_wait_busy_s': 0.002,
                  'accept_wait_idle_s': round(fixed[1], 6),
                  'basis_wait_s': round(idle_basis + 0.004, 6), 'basis_wait_busy_s': 0.004,
                  'basis_wait_idle_s': round(idle_basis, 6),
                  'leaf_wait_s': round(idle_leaf + busy, 6), 'leaf_wait_busy_s': round(busy, 6),
                  'leaf_wait_idle_s': round(idle_leaf, 6),
                  'complete_wait_s': round(fixed[2] + 0.004, 6), 'complete_wait_busy_s': 0.004,
                  'complete_wait_idle_s': round(fixed[2], 6),
                  'page_send_s': round(page_send, 6), 'leaf_requests': 19, 'leaf_waits_over_1ms': exposed,
                  'window': {W1: 1, W16: 16}.get(method, 4),
                  'high_churn_node_fraction': 0.5 if method == HC else None,
                  'high_churn_files': 1 if method == HC else 0,
                  'busy_idle_gap_s': {wait: 0.0 for wait in measure.PGDELTA_SENDER_WAITS}}
        receiver_wall = round(total - 0.04, 6)
        publish = round(receiver_wall - fixed[3], 6)
        report['receiver'] = {'hello_wait_s': round(1.5 * r + 0.01, 6), 'recv_wait_s': round(total * 0.4, 6),
                              'publish_s': publish}
        prefetch = PREFETCH[self.workload] + (int(0.2e9 * r) if method == TLS else 0)
        report.update(prefetch={'bytes': prefetch, 'seconds': round(prefetch / 0.4e9, 4), 'switch': 'on'},
                      pages={'total': 270_000, 'sent': PAGE_BYTES[self.workload] // 8192,
                             'sent_bytes': PAGE_BYTES[self.workload] + (HC_BYTES.get(self.workload, 0)
                                                                        if method == HC else 0)},
                      files={'total': 1200, 'changed': 19, 'unchanged': 1181}, window_ok=True,
                      bytes_source_to_dest=PAGE_BYTES[self.workload], bytes_dest_to_source=2_000_000)
        return report, receiver_wall

    def record(self, repeat, label, method, order_index, order) -> dict:
        c = self.by_label[label]
        r = c['rtt_ms'] / 1000
        total = self.time(method, label)
        pgdelta = method in measure.PGDELTA_METHODS
        wire = ((V1_WIRE[self.workload] + (HC_BYTES.get(self.workload, 0) if method == HC else 0))
                * (1.003 if method == TLS else 1.0)) if pgdelta else COPY_BYTES
        wire = int(self.noise(wire, 0.0005))
        output = COPY_BYTES - 40_000_000 if method in measure.FULL_COPY_METHODS else None
        ping = c['rtt_ms'] + 0.12
        steady = one_stream(c)
        speed = {key: c[key] for key in ('label', 'slug', 'gbit', 'tc_rate', 'rtt_ms', 'delay_ms', 'host_tcp')}
        busy_ms = int(total * 1000 * 0.9)
        limited = 0.75 if c['host_tcp'] == 'default' and c['rtt_ms'] >= 60 else 0.04
        if method == 'probackup8':
            limited = 0.2
        qdisc = {'same_tree': True,
                 'netem': {'bytes': int(wire * 1.04), 'packets': wire // 1400, 'drops': 3 if r else 0,
                           'overlimits': 0, 'requeues': 0},
                 'tbf': {'bytes': int(wire * 1.04), 'packets': wire // 1400, 'drops': 3 if r else 0,
                         'overlimits': 100, 'requeues': 0},
                 'root': None, 'plain': None, 'netem_own_drops': 0}
        phase_name = (measure.DIRECT_PHASE if pgdelta or method in measure.PARCOPY_METHODS else 'transfer')
        commands = [{'host': 'dest', 'role': 'receiver' if pgdelta else 'tool', 'wall_seconds': total - 0.04,
                     'started_utc': 1_790_000_000.0 + order_index}]
        increase = 1_800_000_000 if method == 'full_nosync' else 2_000_000
        record = {
            'schema': 1, 'run_name': self.name, 'trial_id': self.trial_id, 'pattern': self.pattern,
            'fraction': self.fraction, 'scale': 70, 'rows_changed': 70000, 'repeat': repeat - 1, 'speed': speed,
            'method': method, 'order_index': order_index, 'method_order': order, 'block': 'main',
            'source_cache': 'cold', 'status': 'pass', 'error': None, 'failure_kind': None,
            'started_utc': f'2026-10-26T10:{order_index % 60:02d}:00Z',
            'finished_utc': f'2026-10-26T10:{order_index % 60:02d}:{min(59, int(total + 15)):02d}Z',
            'condition': {'label': label, 'gbit': c['gbit'], 'rtt_ms': c['rtt_ms'], 'delay_ms': c['delay_ms'],
                          'host_tcp': c['host_tcp'], 'visit_index': 0, 'ping_min_ms': ping,
                          'visit_one_stream_gbit': steady, 'zero_delay_gbit': LINK[int(c['gbit'])] - 0.005,
                          'zero_delay_bound_gbit': LINK[int(c['gbit'])] - 0.005,
                          'busy_inflight_bytes': int(steady * 1e9 / 8 * ping / 1000)},
            'link': self.link(c),
            'sysctl': {'tcp': {'source': tcp_values(c['host_tcp']), 'dest': tcp_values(c['host_tcp']),
                               'host_tcp': c['host_tcp'], 'problems': [], 'ok': True}},
            'tcp_metrics': {'source': {'flushed': True, 'entries': 0, 'show': ''},
                            'dest': {'flushed': True, 'entries': 0, 'show': ''}},
            'phases': {phase_name: {'wall_seconds': total, 'failures': [], 'commands': commands,
                                    'qdisc': {'source': qdisc, 'dest': {**qdisc, 'netem': {**qdisc['netem'],
                                                                                           'bytes': 3_000_000}}},
                                    'nstat': {vm: {'TcpRetransSegs': 40 if r else 0, 'TcpExtTCPLostRetransmit': 0,
                                                   'TcpExtTCPTimeouts': 0} for vm in ('source', 'dest')},
                                    'disk': {'dest': {'md0': {'write_bytes': int((output or wire) * 1.01),
                                                              'read_bytes': 0}}}}},
            'qdisc': {'samples': {vm: {'samples': int(total * 2), 'with_tree': int(total * 2), 'netem_qlen_max': 800,
                                       'netem_backlog_max_bytes': 1_100_000, 'limit': 1_000_000, 'at_limit': False}
                                  for vm in ('source', 'dest')}},
            'ss': {'connections': 3, 'sending': [[SENDER, RECEIVER]], 'bytes_acked': wire,
                   'minrtt_ms_min': round(c['rtt_ms'] + 0.11, 3), 'minrtt_ms_max': round(c['rtt_ms'] + 0.4, 3),
                   'busy_ms': busy_ms, 'rwnd_limited_ms': int(busy_ms * limited * 0.9),
                   'sndbuf_limited_ms': int(busy_ms * limited * 0.1), 'limited_fraction': limited,
                   'retrans_total': 2,
                   'main': {'local': SENDER, 'peer': RECEIVER, 'minrtt_ms': round(c['rtt_ms'] + 0.11, 3),
                            'cwnd': 2000, 'busy_ms': busy_ms, 'rwnd_limited_ms': int(busy_ms * limited * 0.9),
                            'sndbuf_limited_ms': int(busy_ms * limited * 0.1), 'retrans': 0, 'retrans_total': 2,
                            'notsent': 0, 'unacked': 0, 'notsent_max': 2_000_000 if method == TLS else 0,
                            'unacked_max': 3_000_000, 'bytes_acked': wire}},
            'totals': {'wall_seconds': total, 'wall_seconds_less_remote_overhead': round(total - 0.06, 4),
                       'wire_bytes_source_to_dest': wire, 'wire_bytes_dest_to_source': 3_000_000,
                       'cpu_seconds_source': round(self.noise(5.5 if pgdelta else 2.6) + (0.4 if method == TLS else 0),
                                                   3),
                       'cpu_seconds_dest': round(self.noise(10.0 if pgdelta else 2.8) + (0.4 if method == TLS else 0),
                                                 3)},
            'output_bytes': output,
            'verification': {'verified': True, 'basis_changes': []},
            'durability': {'durable': method not in measure.NON_DURABLE_METHODS, 'ok': True, 'a_ok': True,
                           'dirty_writeback_increase_bytes': increase, 'limit_bytes': 4 << 20,
                           'noise_window_s': 5.0 if total <= 5 else 30.0 if total <= 30 else 120.0,
                           'noise_beyond_table': False,
                           'noise_table_bytes': {'5': 4 << 20, '30': 4 << 20, '120': 4 << 20}},
        }
        if method == 'full_nosync':
            record['durability'].update(durable=False, a_ok=False, ok=True)
        if method in measure.FULL_COPY_METHODS:
            record['durability']['b_ok'] = True
        if method in measure.PARCOPY_METHODS:
            streams = measure.PARCOPY_VARIANTS[method]['streams']
            record['tool_reported'] = {'parcopy_summary': {'streams': streams, 'balance': 1.02, 'fsync_calls': 1500,
                                                           'fsync_expected': 1500, 'last_byte_s': total - 0.5,
                                                           'fsync_s': 0.3}}
            record['durability']['fsync_count'] = {'ok': True, 'calls': 1500, 'expected': 1500}
        elif method in measure.FULL_VARIANTS:
            stream = total - 1.5 - 0.3
            record['tool_reported'] = {'basebackup_phases': {
                'checkpoint_s': 0.04, 'stream_s': stream, 'wal_end_s': 0.004 + r,
                'sync_s': None if method == 'full_nosync' else (1.33 if stream <= 30 else 0.9), 'manifest_s': 0.05,
                'backup_mode_adder_s': round(0.044 + r, 4)}}
        elif method == 'probackup8':
            record['tool_reported'] = {'probackup_phases': {'copy_s': total - 3, 'stop_s': 0.5, 'files_sync_s': 1.0}}
            record['processes'] = {'utc': 1.0, 'argv': ['ps'], 'source': ['sshd: probackup'] * 8, 'dest': ['ssh'] * 8}
        if pgdelta:
            report, receiver_wall = self.timers(method, c, total)
            record['tool_reported'] = report
            record['receiver_wall_s'] = receiver_wall
        if method == TLS:
            record['stunnel'] = {'tls_version': 'TLSv1.3', 'tls_suite': 'TLS_AES_256_GCM_SHA384', 'connections': 1,
                                 'logs': {'server': 'logs/dest/stunnel-server.log',
                                          'client': 'logs/source/stunnel-client.log'}}
            record['verification']['tls'] = {'tls_version': 'TLSv1.3', 'tls_suite': 'TLS_AES_256_GCM_SHA384',
                                             'connections': 1, 'ok': True}
        return record

    def visit(self, repeat, label, index) -> dict:
        c = self.by_label[label]
        steady = one_stream(c)
        ping = c['rtt_ms'] + 0.12
        checks = {'ok': True, 'problems': {'tree': [], 'pings': [], 'control_noops': [], 'sysctl': []}}
        return {'condition': label, 'repeat': repeat - 1, 'utc': f'2026-10-26T10:00:{index % 60:02d}Z',
                'attempts': [{'applied': {}, 'checks': checks}], 'ok': True,
                'tree': {vm: {'tree': {'is_wan': True}, 'filters': [], 'expected': {}, 'problems': []}
                         for vm in ('source', 'dest')},
                'pings': {'times_ms': [ping] * 20, 'min_ms': ping, 'median_ms': ping + 0.05, 'replies': 20,
                          'count': 20},
                'control_noops': {'seconds': [0.0021] * 5, 'reference': [0.002] * 5},
                'sysctl': {'ok': True, 'problems': {}},
                'bands': {key: {vm: {'plain': 100, 'delayed': 1000} for vm in ('source', 'dest')}
                          for key in ('before_pings', 'after_pings', 'after_noops', 'after_link_test')},
                'link_tests': [link_test('source_to_dest', 1, steady)],
                'busy_inflight_bytes': int(steady * 1e9 / 8 * ping / 1000)}

    def first_visit(self, label) -> dict:
        c = self.by_label[label]
        tests = [link_test('dest_to_source', 1, one_stream(c)), link_test('source_to_dest', 8, streams_rate(c, 8), True)]
        for streams in self.plan['first_visit_streams'].get(label, ()):
            tests.append(link_test('source_to_dest', streams, streams_rate(c, streams), True))
        first = {'condition': label, 'utc': '2026-10-26T10:00:00Z', 'link_tests': tests}
        if self.args.ssh_probe_port and c['rtt_ms'] > 0:
            rate = min(one_stream(c), (2 << 20) * 0.9 / (c['rtt_ms'] / 1000) * 8 / 1e9)
            first['ssh_probe'] = {'per_second_gbit': [rate] * 6, 'last_gbit': round(rate, 4), 'first_byte_s': 0.3,
                                  'cipher': 'aes128-gcm@openssh.com'}
        if label in self.plan['stunnel_link_test']:
            first['stunnel_link_test'] = {**link_test('source_to_dest', 1, round(one_stream(c) * 0.99, 4)),
                                          'stunnel': {'tls_version': 'TLSv1.3', 'connections': 1}}
        return first

    def write(self, adjust=None, adjust_trial=None) -> Path:
        out = self.out / self.name
        records, visits, first = [], [], {}
        index = 0
        for repeat in sorted({repeat for repeat, _, _ in self.schedule}):
            for c in self.conditions:
                label = c['label']
                methods = [method for rep, lab, method in self.schedule if rep == repeat and lab == label]
                if not methods:
                    continue
                visits.append(self.visit(repeat, label, len(visits)))
                if label not in first:
                    first[label] = self.first_visit(label)
                order = list(methods)
                self.rng.shuffle(order)
                for position, method in enumerate(order):
                    record = self.record(repeat, label, method, index, order)
                    index += 1
                    if adjust is not None:
                        adjust(record)
                    record['network_checks'] = measure.network_checks(record)
                    records.append(record)
        for record in records:
            path = out / 'runs' / record['trial_id'] / (f'r{record["repeat"]}-{record["speed"]["slug"]}-'
                                                        f'{record["method"]}.json')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record))
        measure.write_csv(out / 'summary.csv', records)
        user = {'relations': [{'relation': 'pgbench_accounts', 'path': 'base/5/16400'}],
                'paths': ['base/5/16400', 'base/5/16410']}
        trial = {'schema': 1, 'trial_id': self.trial_id, 'pattern': self.pattern, 'fraction': self.fraction,
                 'scale': 70, 'status': 'pass', 'visits': visits, 'first_visit': first, 'network_off': {},
                 'network_off_end': {}, 'user_table_files': user,
                 'reference_digests': {'start': {'entries': {}}, 'end': {'entries': {}}}, 'reference_match': False,
                 'reference_classes': {'counts': {'user_table': 0, 'catalog': 7, 'other': 2},
                                       'paths': {'user_table': [], 'catalog': ['base/5/1259'], 'other': ['pg_xact/0000']},
                                       'user_tables_match': True, 'user_files': 2},
                 'reference_user_tables_match': True,
                 'logical_check': {'void_rule': 'A15: user-table files only', 'reference_user_tables_match': True},
                 'runs': [{'repeat': r['repeat'], 'speed': r['speed']['label'], 'method': r['method'],
                           'status': r['status']} for r in records]}
        if self.plan['zero_delay_rates']:
            trial['zero_delay_link_tests'] = {f'{gbit:g}:0:default': link_test('source_to_dest', 1, LINK[int(gbit)] - 0.005)
                                              for gbit in self.plan['zero_delay_rates']}
        if adjust_trial is not None:
            adjust_trial(trial)
        (out / 'trials').mkdir(parents=True, exist_ok=True)
        (out / 'trials' / f'{self.trial_id}.json').write_text(json.dumps(trial))
        environment = {'tcp_sysctls': tcp_values('default'), 'tcp_adv_win_scale': '1',
                       'tcp_rmem': tcp_values('default')['net.ipv4.tcp_rmem']}
        run = {'schema': 1, 'name': self.name, 'argv': [], 'status': 'pass',
               't12': {'conditions': self.conditions, 'plan': self.plan, 'tuned_tcp_max_bytes': TUNED_MAX,
                       'control_port': 2222},
               'control': {'port': 2222, 'multiplexed': True, 'control_path': '/tmp/cm', 'ssh_options': []},
               't12_preflight': {'tcp_defaults': {vm: tcp_values('default') for vm in ('source', 'dest')},
                                 'tc_commands': {}, 'control_reference': {'condition': '2:0:default',
                                                                          'seconds': [0.002] * 5},
                                 'busy_inflight_flag': True},
               'environment': {'source': environment, 'dest': environment},
               'methods_by_speed': {}, 'noise_table_bytes': {'5': 4 << 20, '30': 4 << 20, '120': 4 << 20},
               'dirty_idle_noise': {'plan': [[5, 20], [30, 6], [120, 3]], 'increases_bytes': {},
                                    'max_increase_bytes': {'5': 1 << 20}},
               'teardown': {vm: {'qdiscs': [], 'tree_is_wan': False} for vm in ('source', 'dest')},
               'trials': {self.trial_id: 'pass'}}
        if self.smoke:
            run['syscall_audit'] = {'arms': {V1: {'passed': True}, TLS: {'passed': True}}}
        (out / 'run.json').write_text(json.dumps(run))
        return out


def write_session(root, *, seed=13, smoke=False, adjust=None, adjust_trial=None, invocations=INVOCATIONS) -> list:
    """The three invocations' results under root (or those named); each one's directory, in the session's order.
    smoke: one repeat each (every arm once at every condition its line runs it at)."""
    lines = spec_run_lines()
    rng = random.Random(seed)
    return [Builder(name, lines[name], rng, Path(root), smoke).write(adjust, adjust_trial) for name in invocations]
