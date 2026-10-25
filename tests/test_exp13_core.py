"""Tests for EXP-13's harness (T12, latency and transport): the conditions, each visit and run, the TLS stand-in.

No VM, root, tc, ss, ping, stunnel or PostgreSQL is needed. Linux's outputs are fixed samples in their documented
formats (ss -tin, /proc/net/snmp and /proc/net/netstat, iputils ping, stunnel's log at debug = info); each is marked
where smoke item 1, 5, 9, 12 or 14 must check it against a capture on a Debian 12 VM. The orchestrator runs with the
stubs of test_exp04_network and test_exp11_core, the emulated WAN and the TCP settings faked in memory (WanFakes);
the link test, the TLS material and one TLS 1.3 handshake run for real over loopback.
"""

import contextlib
import copy
import csv
import io
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import ssl
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from collections import Counter
from contextlib import redirect_stdout
from unittest import mock

from experiments.network import agent, measure, remote, run_exp04

from test_exp04_network import FakeChild, TRIAL, TrialStub
from test_exp11_core import EXT4_MOUNT, SOURCE_DIGEST, T2Stub, Clocked
from test_wan_tree import DEFAULT_JSON, filters_text, tree_json

SPEC = Path(__file__).resolve().parents[1] / 'docs' / 'experiments' / 'EXP-13-latency-transport.md'
REAL_RUN = subprocess.run
V1, TLS = 'pgdelta_direct_inplace', 'pgdelta_direct_inplace_tls'
# Debian 12's TCP settings as /proc/sys prints them (tabs), from EXP-11's run.json.
DEBIAN_TCP = {'net.ipv4.tcp_rmem': '4096\t131072\t6291456', 'net.ipv4.tcp_wmem': '4096\t16384\t4194304',
              'net.ipv4.tcp_congestion_control': 'cubic', 'net.ipv4.tcp_adv_win_scale': 1,
              'net.ipv4.tcp_slow_start_after_idle': 1, 'net.ipv4.tcp_no_metrics_save': 0,
              'net.core.rmem_max': 212992, 'net.core.wmem_max': 212992, 'net.core.default_qdisc': 'fq_codel'}
CONDITIONS = '2:0:default,2:60:default,2:120:tuned,2:0:tuned'
BASE_ARGS = ['--conditions', CONDITIONS, '--control-port', '2222', '--repeats', '2', '--link-test-seconds', '6',
             '--link-test-warmup', '3', '--methods', f'full,parcopy8,{V1},{TLS}',
             '--method-conditions', f'{TLS}=2:0:default,2:120:tuned', '--baseline-methods', 'full,parcopy8',
             '--baseline-repeats', '2', '--negative-control', 'full_nosync@2:0:default']

NO_WARMUP = BASE_ARGS[:BASE_ARGS.index('--link-test-warmup')] + BASE_ARGS[BASE_ARGS.index('--link-test-warmup') + 2:]

# `ss -tinH dst 10.0.0.13` on the source during a v1 run at 120 ms, in iproute2 6.1's format (ss.c tcp_show_info:
# the info line after a tab; notsent and unacked only when not 0): pgdelta's sender, the control path (port 2222,
# left out) and a closed connection. NOT a capture: smoke item 14 checks it on the VMs.
SS_TEXT = """\
ESTAB 0      2896768          10.0.0.12:52344          10.0.0.13:7000
\t cubic wscale:7,7 rto:324 rtt:120.533/0.124 ato:40 mss:1448 pmtu:1500 rcvmss:536 advmss:1448 cwnd:2210 \
ssthresh:1934 bytes_sent:612889421 bytes_retrans:2896 bytes_acked:609992653 bytes_received:41234 segs_out:423293 \
segs_in:120333 data_segs_out:423280 data_segs_in:30 send 212Mbps lastsnd:4 lastrcv:5020 lastack:4 pacing_rate \
254Mbps delivery_rate 199Mbps delivered:421224 busy:12588ms rwnd_limited:9120ms(72.4%) sndbuf_limited:1200ms(9.5%) \
unacked:2058 retrans:0/2 dsack_dups:2 reordering:4 rcv_space:14600 rcv_ssthresh:64088 notsent:2896768 \
minrtt:120.105 snd_wnd:3145728
ESTAB 0      0                10.0.0.12:2222           10.0.0.13:41022
\t cubic wscale:7,7 rto:204 rtt:0.198/0.05 ato:40 mss:1448 cwnd:10 bytes_acked:52311 bytes_received:9100 \
busy:20ms minrtt:0.101
TIME-WAIT 0      0                10.0.0.12:52300          10.0.0.13:7000
\t cubic
"""
# /proc/net/snmp and /proc/net/netstat (kernel 6.1), trimmed to their Tcp and TcpExt lines.
SNMP = """\
Ip: Forwarding DefaultTTL InReceives
Ip: 2 64 123456
Tcp: RtoAlgorithm RtoMin RtoMax MaxConn ActiveOpens PassiveOpens AttemptFails EstabResets CurrEstab InSegs OutSegs \
RetransSegs InErrs OutRsts InCsumErrors
Tcp: 1 200 120000 -1 120 45 3 7 4 3456789 4567890 321 0 12 0
"""
NETSTAT = """\
TcpExt: SyncookiesSent SyncookiesRecv TCPLostRetransmit TCPTimeouts TCPSackRecovery
TcpExt: 0 0 17 5 3
IpExt: InNoRoutes InTruncatedPkts
IpExt: 0 0
"""
# stunnel 5.68's log at debug = info for one v1_tls connection, dest's end (stunnel's src/client.c and log.c lines).
# NOT a capture: smoke item 12 checks it against the VMs' logs.
STUNNEL_SERVER_LOG = """\
2026.10.25 10:00:00 LOG5[ui]: stunnel 5.68 on x86_64-pc-linux-gnu platform
2026.10.25 10:00:00 LOG5[ui]: Configuration successful
2026.10.25 10:00:01 LOG5[0]: Service [pgdelta] accepted connection from 10.0.0.12:51234
2026.10.25 10:00:01 LOG6[0]: Peer certificate required
2026.10.25 10:00:01 LOG5[0]: Certificate accepted at depth=0: CN=pgd-exp13-client
2026.10.25 10:00:01 LOG6[0]: TLS accepted: new session negotiated
2026.10.25 10:00:01 LOG6[0]: TLSv1.3 ciphersuite: TLS_AES_256_GCM_SHA384 (256-bit encryption)
2026.10.25 10:00:01 LOG6[0]: Peer temporary key: X25519, 253 bits
2026.10.25 10:00:01 LOG5[0]: s_connect: connected 127.0.0.1:7000
2026.10.25 10:00:01 LOG5[0]: Service [pgdelta] connected remote server from 127.0.0.1:41000
2026.10.25 10:00:09 LOG5[0]: Connection closed: 41234 byte(s) sent to TLS, 612889421 byte(s) sent to socket
"""


def spec_run_lines() -> dict:
    """{invocation name: run_exp04's arguments} from the specification's run lines, expanded and split as a POSIX
    shell would (a variable's value expanded where it is assigned, comments left out), then as remote.py start hands
    them to run_exp04.py."""
    block = re.search(r'\*\*Run lines\*\*.*?```sh\n(.*?)```', SPEC.read_text(), re.S).group(1)
    variables, lines = {}, {}
    for line in block.replace('\\\n', ' ').splitlines():
        line = re.sub(r'\$(\w+)', lambda found: variables[found.group(1)], line)
        assignment = re.match(r'^(\w+)=(.*)$', line.strip())
        if assignment:
            variables[assignment.group(1)] = ' '.join(shlex.split(assignment.group(2), comments=True))
            continue
        words = shlex.split(line, comments=True)
        if words[:2] != ['python3', 'experiments/network/remote.py']:
            continue
        args = remote.build_parser().parse_args(words[2:])
        lines[args.name] = args.args[1:] if args.args[:1] == ['--'] else args.args
    return lines


def parse(*argv):
    return run_exp04.parse_args(['--source-ip', '10.0.0.12', '--out', '/tmp/out', '--manifest-checksums', 'CRC32C',
                                 *argv])


class WanFakes:
    """The emulated WAN and both VMs' TCP settings, in memory: wan.sh's tree as tc would show it, the sysctls as
    /proc/sys prints them, the pings and the control no-ops a visit reads, the link tests and the samplers."""

    def fake_network(self):
        self.kernel_tcp = {vm: dict(DEBIAN_TCP) for vm in ('source', 'dest')}
        self.tcp_defaults = copy.deepcopy(self.kernel_tcp)
        self.applied = None  # the condition wan.sh has on both VMs, or None (off)
        self.shown = None  # what tc shows instead, when a test wants another tree
        self.control_reference = [0.010] * 5
        self.noops = [0.011] * 5
        self.ping_offsets = []  # each visit check's ping minimum above r, in turn (then 0.2 ms)
        self.stuck_tcp = False  # set_tcp writes but the settings don't change
        self.net_drops = (0, 0)  # netem's and the tbf's drops per window, on both VMs
        self.net_qlen = 0
        self.minrtt_extra = 0.1
        self.qdisc_reads = {'source': 0, 'dest': 0}
        self.blast_seconds = []  # each link test's sender seconds (None: --link-test-seconds)
        self.blast_whole_seconds = []  # the receiver's whole seconds for each link test, in turn (then 6)

    def wan(self, action, condition=None):
        self.events.append(('wan', action, None if condition is None else condition['label']))
        if action == 'apply':
            self.applied = condition
        elif action == 'off':
            self.applied = None

        def show(peer):
            if self.shown is not None:
                text = self.shown
            elif self.applied is None:
                text = DEFAULT_JSON
            else:
                text = tree_json(self.applied)
            qdiscs = json.loads(text)
            return {'qdiscs': qdiscs, 'tree': measure.wan_tree(measure.parse_qdisc_json(qdiscs)),
                    'filters': measure.parse_u32_filters(filters_text(peer, 2222) if self.applied else '')}

        return {'source': show(self.dest_ip), 'dest': show(self.source.ip)}

    def tcp_settings(self):
        return copy.deepcopy(self.kernel_tcp)

    def set_tcp(self, host_setting):
        self.events.append(('set_tcp', host_setting))
        wanted = self.wanted_tcp(host_setting)
        if not self.stuck_tcp:
            for vm in self.kernel_tcp:
                self.kernel_tcp[vm].update({name: value.replace(' ', '\t') for name, value in wanted[vm].items()})
        return {'host_tcp': host_setting, 'wanted': wanted}

    def tcp_metrics_flush(self):
        self.events.append('tcp_metrics_flush')
        return {'source': {'flushed': True, 'entries': 0, 'show': ''}, 'dest': {'flushed': True, 'entries': 0,
                                                                               'show': ''}}

    def ping_check(self):
        rtt = self.applied['rtt_ms'] if self.applied else 0.0
        extra = self.ping_offsets.pop(0) if self.ping_offsets else 0.2
        lines = [f'64 bytes from 10.0.0.12: icmp_seq={index + 1} ttl=64 time={rtt + extra + 0.01 * index:.3f} ms'
                 for index in range(measure.PING_COUNT)]
        return {**measure.parse_ping('\n'.join(lines)), 'exit_code': 0}

    def control_noops(self, count=measure.CONTROL_NOOPS):
        self.events.append('control_noops')
        return list(self.noops)

    def band_counters(self):
        return {'source': {'plain': 1, 'delayed': 2}, 'dest': {'plain': 1, 'delayed': 2}}

    def blast(self, direction, streams=1, *, listen=None, connect=None, seconds=None):
        self.events.append(('blast', direction, streams, listen, connect))
        self.blast_seconds.append(seconds)
        rate = round(min(1.92, 2.6 * streams / max(self.applied['rtt_ms'], 1)) if self.applied else 1.92, 4)
        whole = self.blast_whole_seconds.pop(0) if self.blast_whole_seconds else 6
        return {'sender': {'connect_s': 0.0002 + (self.applied or {}).get('rtt_ms', 0) / 1000,
                           'per_stream_connect_s': [0.06] * streams},
                'receiver': {'gbit': rate, 'steady_gbit': rate, 'steady_last3_gbit': rate,
                             'steady_last3_seconds': list(range(3, whole))[-3:], 'per_second_gbit': [rate] * whole}}

    def ssh_probe(self):
        self.events.append('ssh_probe')
        return {'last_gbit': 0.25, 'per_second_gbit': [0.25] * 6}

    def start_net_samplers(self, name, ss=True):
        self.events.append(('net_samplers', 'start', name))
        return {'out': {'source': f'/s/{name}', 'dest': f'/d/{name}'}}

    def stop_net_samplers(self, started):
        self.events.append(('net_samplers', 'stop'))
        rtt = (self.condition or {}).get('rtt_ms', 0.0)
        qdisc = {'samples': 10, 'with_tree': 10, 'netem_qlen_max': self.net_qlen, 'netem_backlog_max_bytes': 1000,
                 'limit': measure.WAN_NETEM_LIMIT, 'at_limit': self.net_qlen >= measure.WAN_NETEM_LIMIT}
        ss = {'connections': 1, 'sending': [['10.0.0.12:52344', '10.0.0.13:7000']], 'bytes_acked': 10 ** 8,
              'minrtt_ms_min': rtt + self.minrtt_extra, 'minrtt_ms_max': rtt + self.minrtt_extra, 'busy_ms': 1000,
              'rwnd_limited_ms': 600, 'sndbuf_limited_ms': 100, 'limited_fraction': 0.7, 'retrans_total': 2,
              'main': {'local': '10.0.0.12:52344', 'peer': '10.0.0.13:7000', 'minrtt_ms': rtt + self.minrtt_extra,
                       'cwnd': 2210, 'notsent': 0, 'unacked': 12, 'notsent_max': 4096, 'unacked_max': 2058}}
        return {'source': {'ss': ss, 'ss_connections': [], 'qdisc': qdisc, 'pid': 1, 'final': {}, 'samples': 10},
                'dest': {'qdisc': qdisc, 'pid': 2, 'final': {}, 'samples': 10}}

    def start_stunnels(self, tag, server_only=False):
        self.events.append(('stunnel', 'start', tag))
        return {'configs': self.stunnel_configs('server.log', 'client.log'), 'logs': {}, 'server': {},
                **({} if server_only else {'client': {}})}

    def stop_stunnels(self, started):
        self.events.append(('stunnel', 'stop'))
        log = measure.parse_stunnel_log(STUNNEL_SERVER_LOG)
        return {'server': {'log': log, 'cpu_seconds': 0.4}, 'client': {'log': {}, 'cpu_seconds': 0.3},
                'tls_version': log['tls_version'], 'tls_suite': log['tls_suite'], 'connections': log['connections']}

    def qdisc_snapshot(self, host):
        """Each snapshot's tree counters: netem's bytes keep up with the wire's, and each window's drops."""
        self.qdisc_reads[host] += 1
        count = self.qdisc_reads[host]
        windows = count // 2
        return {'is_wan': True, 'root': {'bytes': count * 1000, 'packets': count, 'drops': 0},
                'netem': {'bytes': count * 1000, 'packets': count, 'drops': windows * sum(self.net_drops),
                          'overlimits': 0, 'requeues': 0},
                'tbf': {'bytes': count * 1000, 'packets': count, 'drops': windows * self.net_drops[1],
                        'overlimits': windows * 7, 'requeues': 0},
                'plain': {'bytes': 0, 'packets': 0}}


class T12Stub(WanFakes, T2Stub):
    """T2Stub at EXP-13's conditions (BASE_ARGS), with the emulated WAN faked (WanFakes) and qdisc and nstat counters
    in every snapshot."""

    def __init__(self, tmp, clock, extra_args=()):
        super().__init__(tmp, clock, [*BASE_ARGS, *extra_args])
        self.fake_network()
        self.methods = run_exp04.method_list(self.args.methods)

    def _snap(self, host):
        value = super()._snap(host)
        value['qdisc'] = self.qdisc_snapshot(host)
        value['nstat'] = {'TcpRetransSegs': self.ticks[host] * 3, 'TcpExtTCPLostRetransmit': 0,
                          'TcpExtTCPTimeouts': self.ticks[host] // 2}
        return value


class Clocked13(Clocked):
    def orchestrator(self, cls=T12Stub, extra_args=()):
        return super().orchestrator(cls, extra_args)

    def visit(self, orchestrator, label, trial=None, repeat=0):
        condition = next(condition for condition in orchestrator.conditions if condition['label'] == label)
        trial = trial if trial is not None else {'runs': []}
        link = orchestrator.set_condition(condition, trial, repeat)
        return condition, link, trial

    def run_at(self, orchestrator, label, method):
        condition, link, _ = self.visit(orchestrator, label)
        orchestrator.events.clear()
        with mock.patch.object(run_exp04, 'Child', FakeChild):
            return orchestrator.run_method(TRIAL, 0, condition, dict(link), method, 0, [method])


# --- conditions and the schedule --------------------------------------------------------------------------------

class Conditions(unittest.TestCase):
    def test_a_condition_is_rate_rtt_and_host(self):
        condition = measure.parse_condition('2:60:tuned')
        self.assertEqual(condition, {'label': '2:60:tuned', 'slug': '2g-60ms-tuned', 'gbit': 2.0,
                                     'tc_rate': '2000mbit', 'rtt_ms': 60.0, 'delay_ms': 30.0, 'host_tcp': 'tuned'})
        self.assertEqual(measure.parse_condition(' 1:120:DEFAULT ')['label'], '1:120:default')
        self.assertEqual(measure.parse_condition('2:0:default')['delay_ms'], 0.0)
        for bad in ('2:60', '2:60:fast', 'unthrottled:0:default', '2:-1:default', '2:x:default', '0:0:default'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                measure.parse_condition(bad)
        with self.assertRaises(ValueError):
            measure.parse_conditions('2:0:default,2:0:default')

    def test_the_plan_of_baselines_repeats_and_the_negative_control(self):
        conditions = measure.parse_conditions('2:0:default,1:60:default,2:120:tuned')
        plan = {'method_conditions': {'parcopy8': ['2:0:default', '1:60:default']},
                'method_repeats': {'probackup8': [1, 3, 5]}, 'baseline_methods': ['full', 'parcopy8'],
                'baseline_repeats': [2, 4], 'baseline_every': ['1:60:default'],
                'negative_control': {'method': 'full_nosync', 'conditions': ['2:0:default']}}
        methods = ['v1', 'full', 'parcopy8', 'probackup8']
        self.assertEqual(measure.t12_methods_at(methods, '2:0:default', 1, plan),
                         ['v1', 'probackup8', 'full_nosync'])
        self.assertEqual(measure.t12_methods_at(methods, '2:0:default', 2, plan), ['v1', 'full', 'parcopy8'])
        # At a --baseline-every-repeat condition the baselines run in every repeat.
        self.assertEqual(measure.t12_methods_at(methods, '1:60:default', 3, plan),
                         ['v1', 'full', 'parcopy8', 'probackup8'])
        self.assertEqual(measure.t12_methods_at(methods, '2:120:tuned', 4, plan), ['v1', 'full'])
        schedule = measure.t12_schedule(methods, conditions, 5, plan)
        self.assertEqual(Counter(method for _, _, method in schedule),
                         {'v1': 15, 'full': 9, 'parcopy8': 7, 'probackup8': 9, 'full_nosync': 1})

    def test_the_options_parse_and_refuse(self):
        args = parse(*BASE_ARGS, '--first-visit-link-streams', '16@2:120:tuned', '--stunnel-link-test', '2:0:default',
                     '--zero-delay-link-rates', '1', '--method-repeats', 'parcopy8=1')
        plan = run_exp04.t12_plan(args)
        self.assertEqual(plan['first_visit_streams'], {'2:120:tuned': [16]})
        self.assertEqual(plan['zero_delay_rates'], [1.0])
        self.assertEqual(plan['negative_control'], {'method': 'full_nosync', 'conditions': ['2:0:default']})
        self.assertEqual(plan['baseline_repeats'], [2])
        refused = {
            'needs --control-port': [arg for arg in BASE_ARGS if arg not in ('--control-port', '2222')],
            'not in --conditions': [*BASE_ARGS, '--method-conditions', 'parcopy8=5:0:default'],
            'need --conditions (EXP-13)': ['--methods', 'full,parcopy16', '--speeds', '2'],
            'use --method-conditions': [*BASE_ARGS, '--method-speeds', 'full=2'],
            "doesn't run with --load": [*BASE_ARGS, '--extension-repeats', '1', '--extension-methods', 'full'],
            '--negative-control': [*BASE_ARGS[:-2], '--negative-control', 'nothing@2:0:default'],
            '--link-test-warmup': [*BASE_ARGS, '--link-test-warmup', '6'],
            # Every probe link test needs 3 whole seconds after the warmup (link_test's need_steady): refused before
            # the VMs' time is spent. The defaults (3 s, blast-recv's 0.5 s warmup) give 2; 6 s after 4 s gives 2.
            '--conditions needs --link-test-warmup': NO_WARMUP,
            'at least ceil(--link-test-warmup) + 3': [*BASE_ARGS, '--link-test-warmup', '4'],
            '--dirty-noise-table': [*BASE_ARGS, '--dirty-noise-table', '5'],
        }
        for message, argv in refused.items():
            with self.subTest(message=message), redirect_stderr() as err, self.assertRaises(SystemExit):
                parse(*argv)
            self.assertIn(message, err.getvalue())
        self.assertEqual(parse(*BASE_ARGS, '--link-test-warmup', '2.5').link_test_warmup, 2.5)  # 6 s >= 3 + 3
        with redirect_stderr() as err, self.assertRaises(SystemExit):
            parse(*NO_WARMUP, '--link-test-seconds', '3')
        self.assertIn('--conditions needs --link-test-warmup', err.getvalue())
        with redirect_stderr() as err, self.assertRaises(SystemExit):
            run_exp04.parse_args(['--source-ip', 'x', '--out', 'y', *BASE_ARGS])
        self.assertIn('need --manifest-checksums CRC32C', err.getvalue())


def redirect_stderr():
    from contextlib import redirect_stderr as redirect
    stream = io.StringIO()
    context = redirect(stream)

    class Wrapped:
        def __enter__(self):
            context.__enter__()
            return stream

        def __exit__(self, *exc):
            return context.__exit__(*exc)

    return Wrapped()


class RunLines(unittest.TestCase):
    """The specification's run lines, exactly as written, parse and plan its "Runs per trial" table."""

    def test_they_parse_and_plan_the_registered_runs(self):
        lines = spec_run_lines()
        self.assertEqual(sorted(lines), ['exp13-hot', 'exp13-uniform1', 'exp13-uniform10'])
        expected = {
            'exp13-uniform1': (112, {V1: 45, 'pgdelta_direct_inplace_w1': 5, 'pgdelta_direct_inplace_w16': 5,
                                     'pgdelta_direct_inplace_hc': 5, TLS: 10, 'parcopy8': 22, 'parcopy16': 2,
                                     'full': 14, 'probackup8': 3, 'full_nosync': 1}),
            'exp13-hot': (84, {V1: 35, 'pgdelta_direct_inplace_w1': 5, 'pgdelta_direct_inplace_w16': 5, TLS: 10,
                               'parcopy8': 12, 'parcopy16': 2, 'full': 14, 'full_nosync': 1}),
            'exp13-uniform10': (58, {V1: 30, 'pgdelta_direct_inplace_hc': 5, 'parcopy8': 10, 'full': 12,
                                     'full_nosync': 1})}
        totals = Counter()
        for name, argv in lines.items():
            with self.subTest(name=name):
                args = run_exp04.parse_args(['--source-ip', '10.0.0.12', '--out', f'/data/exp04-results/{name}',
                                             *argv])
                self.assertEqual((args.control_port, args.link_test_seconds, args.link_test_warmup, args.repeats),
                                 (2222, 6.0, 3.0, 5))
                self.assertEqual(measure.parse_noise_table(args.dirty_noise_table),
                                 {5.0: 4 << 20, 30.0: 4 << 20, 120.0: 4 << 20})
                schedule = measure.t12_schedule(run_exp04.method_list(args.methods),
                                                measure.parse_conditions(args.conditions), args.repeats,
                                                run_exp04.t12_plan(args))
                runs = Counter(method for _, _, method in schedule)
                self.assertEqual((len(schedule), dict(runs)), expected[name])
                totals.update(runs)
                self.assertIsNone(run_exp04.checksum_problem(run_exp04.method_list(args.methods),
                                                             args.manifest_checksums, None, args.conditions))
        # The "all" row: 110 v1, 50 side arms, 44 parcopy8, 4 parcopy16, 40 full, 6 others; 254 in all.
        side = sum(totals[method] for method in measure.T12_V1_ARMS)
        self.assertEqual((totals[V1], side, totals['parcopy8'], totals['parcopy16'], totals['full'],
                          totals['probackup8'] + totals['full_nosync'], sum(totals.values())),
                         (110, 50, 44, 4, 40, 6, 254))
        uniform1 = run_exp04.parse_args(['--source-ip', 'x', '--out', 'y', *lines['exp13-uniform1']])
        plan = run_exp04.t12_plan(uniform1)
        self.assertEqual(plan['first_visit_streams'], {'2:120:default': [16]})
        self.assertEqual(plan['stunnel_link_test'], ['2:0:default', '2:60:default', '2:120:tuned'])
        self.assertEqual((plan['zero_delay_rates'], uniform1.ssh_probe_port), ([1.0], 22))


# --- the arms ---------------------------------------------------------------------------------------------------

class Arms(unittest.TestCase):
    def test_v1_pins_window_4_and_each_side_arm_changes_one_thing(self):
        args = measure.PGDELTA_ARGS
        v1 = args[V1]
        self.assertEqual(v1['send'], ('--jobs', '4', '--source-kind', 'datadir', '--window', '4'))
        for method, extra in (('pgdelta_direct_inplace_w1', ('--window', '1')),
                              ('pgdelta_direct_inplace_w16', ('--window', '16')),
                              ('pgdelta_direct_inplace_hc', ('--window', '4', '--high-churn-node-fraction', '0.5')),
                              (TLS, ('--window', '4'))):
            with self.subTest(method=method):
                self.assertEqual(args[method]['send'], ('--jobs', '4', '--source-kind', 'datadir', *extra))
                self.assertEqual(args[method]['receive'], v1['receive'])
                for group in (measure.PGDELTA_DIRECT_METHODS, measure.IN_PLACE_METHODS, measure.SOURCE_STOP_METHODS,
                              measure.OPTIONAL_METHODS, measure.T12_METHODS):
                    self.assertIn(method, group)
                self.assertIn(method, measure.PROC_SCOPE)
        self.assertEqual(measure.PGDELTA_ENGINES[TLS]['transport'], 'tls')
        self.assertIn('stunnel', measure.PROC_SCOPE[TLS])
        self.assertEqual(measure.pgdelta_required_options(['pgdelta_direct_inplace_hc'])['send'],
                         ('--jobs', '--source-kind', '--window', '--high-churn-node-fraction'))

    def test_parcopy16_is_the_bound_over_16_streams(self):
        self.assertEqual(measure.parcopy_args('parcopy16')['send'][:2], ('--streams', '16'))
        self.assertEqual(measure.TIERS['parcopy16'], 'bound')
        for group in (measure.PARCOPY_METHODS, measure.SOURCE_STOP_METHODS, measure.FULL_COPY_METHODS,
                      measure.MULTI_STREAM_METHODS, measure.OUTPUT_START_METHODS):
            self.assertIn('parcopy16', group)
        self.assertNotIn('parcopy16', measure.T2_METHODS)
        self.assertIn('16 streams', measure.PROC_SCOPE['parcopy16'])


# --- TCP settings, pings, control no-ops ------------------------------------------------------------------------

class HostSettings(unittest.TestCase):
    def test_tuned_raises_only_the_two_maxima(self):
        self.assertEqual(measure.condition_tcp_settings('default', DEBIAN_TCP),
                         {'net.ipv4.tcp_rmem': '4096 131072 6291456', 'net.ipv4.tcp_wmem': '4096 16384 4194304'})
        self.assertEqual(measure.condition_tcp_settings('tuned', DEBIAN_TCP, 134217728),
                         {'net.ipv4.tcp_rmem': '4096 131072 134217728', 'net.ipv4.tcp_wmem': '4096 16384 134217728'})
        wanted = measure.condition_tcp_settings('tuned', DEBIAN_TCP)
        self.assertEqual(measure.tcp_settings_problems({'net.ipv4.tcp_rmem': '4096\t131072\t134217728',
                                                        'net.ipv4.tcp_wmem': '4096\t16384\t134217728'}, wanted), [])
        self.assertEqual(len(measure.tcp_settings_problems(DEBIAN_TCP, wanted)), 2)
        with self.assertRaises(ValueError):
            measure.condition_tcp_settings('tuned', {'net.ipv4.tcp_rmem': 'x'})

    def test_the_visits_pings(self):
        def pings(times):
            return measure.parse_ping('PING 10.0.0.12 (10.0.0.12) 56(84) bytes of data.\n' + '\n'.join(
                f'64 bytes from 10.0.0.12: icmp_seq={index} ttl=64 time={value} ms' for index, value in enumerate(times))
                + '\n\n--- 10.0.0.12 ping statistics ---\n20 packets transmitted, 20 received, 0% packet loss\n'
                  'rtt min/avg/max/mdev = 60.1/60.2/60.4/0.1 ms\n')

        good = pings([60.2] * 19 + [60.4])
        self.assertEqual((good['replies'], good['min_ms'], good['median_ms']), (20, 60.2, 60.2))
        self.assertEqual(measure.ping_problems(good, 60.0), [])
        self.assertEqual(measure.ping_problems(pings([0.21] * 20), 0.0), [])
        self.assertEqual(len(measure.ping_problems(pings([59.9] * 20), 60.0)), 1)  # below r: not delayed
        self.assertEqual(len(measure.ping_problems(pings([61.2] * 20), 60.0)), 1)  # above r + 1 ms
        self.assertIn('median', measure.ping_problems(pings([60.2] * 5 + [63.0] * 15), 60.0)[0])
        self.assertIn('19 of 20', measure.ping_problems(pings([60.2] * 19), 60.0)[0])

    def test_the_control_noops_against_the_anchors(self):
        self.assertEqual(measure.control_noop_problems([0.012, 0.014], [0.010, 0.011, 0.010]), [])
        self.assertTrue(measure.control_noop_problems([0.012, 0.016], [0.010, 0.011, 0.010]))
        self.assertTrue(measure.control_noop_problems([0.012], None))


# --- the samplers' parsers: ss, nstat ---------------------------------------------------------------------------

class SsAndNstat(unittest.TestCase):
    def test_ss_keeps_each_sockets_counters_without_the_control_path(self):
        sockets = measure.parse_ss(SS_TEXT, exclude_ports=(2222,))
        self.assertEqual([item['peer'] for item in sockets], ['10.0.0.13:7000', '10.0.0.13:7000'])
        sender = sockets[0]
        self.assertEqual((sender['state'], sender['local_port'], sender['send_q']), ('ESTAB', 52344, 2896768))
        self.assertEqual((sender['minrtt_ms'], sender['rtt_ms'], sender['cwnd'], sender['busy_ms'],
                          sender['rwnd_limited_ms'], sender['sndbuf_limited_ms'], sender['retrans'],
                          sender['retrans_total'], sender['notsent'], sender['unacked'], sender['bytes_acked']),
                         (120.105, 120.533, 2210, 12588, 9120, 1200, 0, 2, 2896768, 2058, 609992653))
        self.assertEqual(sender['delivery_rate'], '199Mbps')
        closed = sockets[1]
        self.assertEqual((closed['state'], closed['notsent'], closed['unacked']), ('TIME-WAIT', 0, 0))
        self.assertEqual(len(measure.parse_ss(SS_TEXT)), 3)
        # With a state filter ss leaves the state column out, and with -H no header line.
        self.assertEqual(measure.parse_ss('Recv-Q Send-Q Local Address:Port Peer Address:Port\n'
                                          '0 0 [::ffff:10.0.0.12]:5432 [::ffff:10.0.0.13]:40000\n')[0]['local'],
                         '10.0.0.12:5432')

    def test_a_runs_reading_is_each_connections_last_and_its_sending_sockets(self):
        first = measure.parse_ss(SS_TEXT, exclude_ports=(2222,))
        later = copy.deepcopy(first)
        later[0].update(busy_ms=20000, rwnd_limited_ms=15000, sndbuf_limited_ms=1000, notsent=0, unacked=0,
                        bytes_acked=900000000)
        small = {**first[0], 'local': '10.0.0.12:52399', 'bytes_acked': 4096, 'busy_ms': 1, 'minrtt_ms': 0.5}
        connections = measure.ss_connections([{'t': 1.0, 'ss': first}, {'t': 1.5, 'ss': [*later, small]}])
        main = next(item for item in connections if item['local'] == '10.0.0.12:52344')
        self.assertEqual((main['samples'], main['notsent_max'], main['unacked_max'], main['last']['busy_ms']),
                         (2, 2896768, 2058, 20000))
        summary = measure.ss_summary(connections)
        # The small connection carried too little to be a sending socket: its minrtt isn't the run's.
        self.assertEqual(summary['sending'], [['10.0.0.12:52344', '10.0.0.13:7000']])
        self.assertEqual((summary['minrtt_ms_min'], summary['busy_ms'], summary['limited_fraction']),
                         (120.105, 20000, 0.8))
        self.assertEqual((summary['main']['notsent_max'], summary['main']['unacked'], summary['main']['cwnd']),
                         (2896768, 0, 2210))
        self.assertIsNone(measure.ss_summary([])['minrtt_ms_min'])

    def test_nstat_reads_the_three_tcp_counters(self):
        self.assertEqual(measure.nstat_values(SNMP, NETSTAT),
                         {'TcpRetransSegs': 321, 'TcpExtTCPLostRetransmit': 17, 'TcpExtTCPTimeouts': 5})
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            (proc / 'net').mkdir()
            (proc / 'net' / 'dev').write_text('Inter-|\n face |\n  ens4: ' + ' '.join(['1'] * 16) + '\n')
            (proc / 'stat').write_text('cpu  1 2 3 4 5 6 7 8 0 0\n')
            (proc / 'net' / 'snmp').write_text(SNMP)
            (proc / 'net' / 'netstat').write_text(NETSTAT)
            self.assertNotIn('nstat', measure.snapshot('ens4', proc))
            first = measure.snapshot('ens4', proc, tcp=True)
            (proc / 'net' / 'snmp').write_text(SNMP.replace(' 321 ', ' 330 '))
            second = measure.snapshot('ens4', proc, tcp=True)
        self.assertEqual(measure.snapshot_delta(first, second)['nstat'],
                         {'TcpRetransSegs': 9, 'TcpExtTCPLostRetransmit': 0, 'TcpExtTCPTimeouts': 0})

    def test_the_net_samplers_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            sockets = measure.parse_ss(SS_TEXT, exclude_ports=(2222,))
            counters = {'is_wan': True, 'root': None, 'tbf': None, 'plain': None,
                        'netem': {'bytes': 1, 'packets': 1, 'drops': 0, 'backlog': 3000, 'qlen': 2}}
            lines = [json.dumps({'t': 1.0, 'ss': sockets, 'qdisc': counters}),
                     json.dumps({'t': 1.5, 'ss': sockets, 'qdisc': {**counters, 'netem': {**counters['netem'],
                                                                                          'qlen': 9}}}), '{"t": 2']
            (Path(tmp) / 'net-samples.jsonl').write_text('\n'.join(lines))
            summary = agent.net_sampler_summary(tmp)
        self.assertEqual(summary['samples'], 2)  # the line cut short at the stop is left out
        self.assertEqual(summary['ss']['minrtt_ms_min'], 120.105)
        self.assertEqual((summary['qdisc']['netem_qlen_max'], summary['qdisc']['at_limit']), (9, False))


# GNU time 1.9's signal handling (time.c, run_command): the command it forks stays in its process group; while it
# waits, it ignores SIGINT and SIGQUIT and nothing else; once the command has exited it writes its -f report to -o.
GNU_TIME_LIKE = """\
import signal, subprocess, sys
args = sys.argv[1:]
fmt, out = args[args.index('-f') + 1], args[args.index('-o') + 1]
command = subprocess.Popen(args[args.index('-o') + 2:])
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGQUIT, signal.SIG_IGN)
code = command.wait()
with open(out, 'w') as stream:
    stream.write(fmt.replace('%M', '4321').replace('%e', '1.0').replace('%U', '0').replace('%S', '0')
                 .replace('%x', str(code)) + '\\n')
sys.exit(code if code >= 0 else 128 - code)
"""
# A command that writes its PID to argv[1] and sleeps: STUBBORN ignores SIGTERM, POLITE ends at it.
_PID = 'open(sys.argv[1] + ".tmp", "w").write(str(os.getpid())); os.replace(sys.argv[1] + ".tmp", sys.argv[1])\n'
STUBBORN = 'import os, signal, sys, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n' + _PID + 'time.sleep(60)\n'
POLITE = 'import os, sys, time\n' + _PID + 'time.sleep(60)\n'


def gone(pid: int, wait: float = 5.0) -> bool:
    """Whether process pid has ended (and been reaped) within `wait` seconds."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class AgentProcesses(unittest.TestCase):
    """The agent's background and probe commands, run for real where they need no root or Linux tool."""

    def test_the_net_sampler_starts_samples_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            started = agent.cmd_net_sampler(Namespace(action='start', out=tmp, peer=None, exclude_port=[],
                                                      qdisc_iface=None, interval=0.05))
            self.assertEqual(started['argv'][2:4], ['net-sampler', 'run'])
            time.sleep(0.5)
            stopped = agent.cmd_net_sampler(Namespace(action='stop', out=tmp))
        self.assertEqual(stopped['pid'], started['pid'])
        self.assertGreaterEqual(stopped['final']['samples'], 3)
        self.assertEqual(stopped['samples'], stopped['final']['samples'])
        self.assertNotIn('ss', stopped)  # no --peer: no ss readings

    def test_the_ssh_probe_reads_one_channel_for_its_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / 'ssh'
            # Stands in for ssh: its arguments go to a file, then zeros on stdout as fast as the pipe takes them.
            fake.write_text(f'#!{os.path.realpath(__import__("sys").executable)}\n'
                            'import sys\n'
                            f'open({str(Path(tmp) / "argv")!r}, "w").write(" ".join(sys.argv[1:]))\n'
                            'chunk = bytes(1 << 16)\n'
                            'while True:\n    sys.stdout.buffer.write(chunk)\n')
            fake.chmod(0o755)
            with mock.patch.dict(os.environ, {'PATH': f'{tmp}:{os.environ["PATH"]}'}):
                probe = agent.cmd_ssh_probe(Namespace(host='10.0.0.12', user='bench', key='/k', port=22, seconds=2.0,
                                                      warmup=1.0, cipher=agent.SSH_PROBE_CIPHER, timeout=20))
            argv = (Path(tmp) / 'argv').read_text()
        self.assertIn('-p 22', argv)
        self.assertIn('Ciphers=aes128-gcm@openssh.com', argv)
        self.assertIn('ControlPath=none', argv)
        self.assertTrue(argv.endswith('bench@10.0.0.12 exec cat /dev/zero'))
        self.assertEqual(len(probe['per_second_bytes']), 2)
        self.assertEqual(probe['last_seconds'], 1)
        self.assertGreater(probe['last_gbit'], 0)

    @staticmethod
    def fake_stunnel(tmp, log, *, marker=None, exit_code=None):
        """A stand-in for stunnel: its "configuration" is the port it listens on; it writes the log and waits (or,
        with exit_code, exits at once). With marker, it first writes that file, to show it was launched."""
        fake = Path(tmp) / 'stunnel4'
        lines = [f'#!{os.path.realpath(sys.executable)}', 'import socket, sys, time']
        if marker:
            lines.append(f'open({str(marker)!r}, "w").write("launched")')
        if exit_code is not None:
            lines.append(f'sys.exit({exit_code})')
        lines += ['listener = socket.socket(); listener.bind(("127.0.0.1", int(sys.argv[1])))', 'listener.listen()',
                  f'open({str(log)!r}, "w").write({STUNNEL_SERVER_LOG!r})', 'time.sleep(60)']
        fake.write_text('\n'.join(lines) + '\n')
        fake.chmod(0o755)
        return fake

    @staticmethod
    def free_port():
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    @staticmethod
    def listening_here():
        """agent.listening reads `ss`, which only Linux has: elsewhere a connect to the port on localhost stands in
        (the fake stunnel never accepts, so the probe's connection only waits in its backlog)."""
        if Path('/proc/self').exists() and shutil.which('ss'):
            return contextlib.nullcontext()

        def connects(port):
            try:
                socket.create_connection(('127.0.0.1', port), timeout=1).close()
                return True
            except OSError:
                return False
        return mock.patch.object(agent, 'listening', connects)

    def test_stunnel_start_waits_for_its_listener_and_stop_reads_its_log(self):
        # Run in this process, the fake stunnel is this process's child: once stopped it is a zombie until reaped,
        # as /proc shows it. stop reaps it rather than waiting 10 s for it and sending SIGKILL to a zombie.
        with tempfile.TemporaryDirectory() as tmp, self.listening_here():
            log = Path(tmp) / 'server.log'
            fake = self.fake_stunnel(tmp, log)
            port = self.free_port()
            started = agent.cmd_stunnel(Namespace(action='start', name='server', dir=tmp, config=str(port), port=port,
                                                  log=str(log), stunnel=str(fake), strace_log=None))
            began = time.monotonic()
            stopped = agent.cmd_stunnel(Namespace(action='stop', name='server', dir=tmp, log=str(log)))
            self.assertLess(time.monotonic() - began, 5)
            self.assertFalse(stopped['killed'])
            self.assertEqual(stopped['pid'], started['pid'])
            with self.assertRaises(ProcessLookupError):  # reaped: no zombie left (and on Linux no /proc entry)
                os.kill(started['pid'], 0)
            self.assertFalse(Path(f'/proc/{started["pid"]}').exists())
            self.assertEqual(stopped['log']['tls_version'], 'TLSv1.3')
            with self.assertRaises(SystemExit):  # nothing listens on this port: refused after the timeout
                with mock.patch.object(agent, 'STUNNEL_START_TIMEOUT', 1):
                    agent.cmd_stunnel(Namespace(action='start', name='server', dir=tmp, config=str(port), port=1,
                                                log=str(log), stunnel=str(fake), strace_log=None))

    def test_stunnel_start_refuses_a_port_something_already_listens_on(self):
        # A stunnel an interrupted run left keeps its port, with an earlier session's CA: no new one starts beside it.
        with tempfile.TemporaryDirectory() as tmp, self.listening_here():
            log, marker = Path(tmp) / 'server.log', Path(tmp) / 'launched'
            fake = self.fake_stunnel(tmp, log, marker=marker)
            stale = socket.socket()
            stale.bind(('127.0.0.1', 0))
            stale.listen()
            self.addCleanup(stale.close)
            port = stale.getsockname()[1]
            with self.assertRaises(SystemExit) as caught:
                agent.cmd_stunnel(Namespace(action='start', name='server', dir=tmp, config=str(port), port=port,
                                            log=str(log), stunnel=str(fake), strace_log=None))
            self.assertIn('already listens', str(caught.exception))
            self.assertFalse(marker.exists())

    def test_stunnel_start_fails_when_its_own_process_has_exited(self):
        # Something else listens on the port by the time it is checked, but this stunnel has exited (EADDRINUSE):
        # its start fails rather than reporting the dead pid.
        with tempfile.TemporaryDirectory() as tmp:
            log, marker = Path(tmp) / 'server.log', Path(tmp) / 'launched'
            fake = self.fake_stunnel(tmp, log, marker=marker, exit_code=1)
            calls = []

            def listening(port):
                # Free before the launch; then "listening" once the fake has run (and exited).
                calls.append(port)
                if len(calls) == 1:
                    return False
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                time.sleep(0.3)
                return True

            with mock.patch.object(agent, 'listening', listening), \
                    mock.patch.object(agent, 'STUNNEL_START_TIMEOUT', 5), self.assertRaises(SystemExit) as caught:
                agent.cmd_stunnel(Namespace(action='start', name='server', dir=tmp, config='1', port=1,
                                            log=str(log), stunnel=str(fake), strace_log=None))
            self.assertIn('exit 1', str(caught.exception))

    def test_stunnel_under_strace_is_stopped_only_at_setsockopt(self):
        # strace without --seccomp-bpf stops stunnel at every system call, which slows its read and write loop.
        process = mock.Mock(pid=4321)
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(agent.subprocess, 'Popen', return_value=process) as popen, \
                mock.patch.object(agent, 'listening', side_effect=[False, True]):
            started = agent.cmd_stunnel(Namespace(action='start', name='server', dir=tmp, config='s.conf', port=7443,
                                                  log=f'{tmp}/s.log', stunnel='/usr/bin/stunnel4',
                                                  strace_log=f'{tmp}/s.strace'))
        argv = popen.call_args.args[0]
        self.assertEqual(argv, started['argv'])
        self.assertEqual(argv[:6], ['strace', '-f', '--seccomp-bpf', '-tt', '-e', 'trace=setsockopt'])
        self.assertEqual(argv[-2:], ['/usr/bin/stunnel4', 's.conf'])

    def test_a_stopped_sampler_reports_its_own_final_record_not_an_earlier_ones(self):
        # Every arm of a visit used to share one directory: an earlier arm's final record made stop return at once,
        # before this sampler's reading after the clock, with that arm's counts.
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, agent.NET_SAMPLER_FILES['final']).write_text(json.dumps({'samples': 999, 'pid': 1}))
            started = agent.cmd_net_sampler(Namespace(action='start', out=tmp, peer=None, exclude_port=[],
                                                      qdisc_iface=None, interval=0.05))
            self.assertFalse(Path(tmp, agent.NET_SAMPLER_FILES['final']).exists())
            time.sleep(0.3)
            stopped = agent.cmd_net_sampler(Namespace(action='stop', out=tmp))
        self.assertEqual(stopped['final']['pid'], started['pid'])
        self.assertNotEqual(stopped['final']['samples'], 999)
        self.assertEqual(stopped['samples'], stopped['final']['samples'])

    def test_stale_samplers_and_stunnels_are_found_and_stopped(self):
        ps = ('  101 python3 /home/bench/pg-delta-backup/experiments/network/agent.py net-sampler run --out /data/exp04/'
              'logs/00-hot/r1-2g-0ms-default-net-sampler-run-full --interval 0.5\n'
              '  102 /usr/bin/stunnel4 /data/exp04/logs/00-hot/r2-2g-0ms-default-stunnel-client.conf\n'
              '  103 strace -f --seccomp-bpf -tt -e trace=setsockopt -o /r/x-stunnel-server.strace /usr/bin/stunnel4 '
              '/r/logs/00-hot/dest/2g-0ms-default-link-test-stunnel-server.conf\n'
              '  104 python3 /home/bench/pg-delta-backup/experiments/network/agent.py net-sampler stop --out /x\n'
              '  105 vim /r/logs/stunnel-server.conf\n'
              '  106 python3 /home/bench/pg-delta-backup/experiments/network/agent.py stale-processes --kill\n')
        found = agent.stale_processes(ps, own_pid=106)
        self.assertEqual([(item['pid'], item['what']) for item in found],
                         [(101, 'net-sampler'), (102, 'stunnel'), (103, 'stunnel')])
        # --kill stops each one found (here one real sampler, the only process ps is made to show).
        with tempfile.TemporaryDirectory() as tmp:
            sampler = subprocess.Popen([sys.executable, str(Path(agent.__file__)), 'net-sampler', 'run', '--out', tmp,
                                        '--interval', '0.05'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: sampler.poll() is None and sampler.kill())
            line = f'{sampler.pid} {sys.executable} {agent.__file__} net-sampler run --out {tmp} --interval 0.05\n'
            with mock.patch.object(agent, 'text_of', return_value=line):
                result = agent.cmd_stale_processes(Namespace(kill=True))
        self.assertEqual([(item['pid'], item['what'], item['killed']) for item in result['found']],
                         [(sampler.pid, 'net-sampler', False)])
        self.assertIsNotNone(sampler.poll())

    def test_run_stops_a_command_at_its_timeout(self):
        # A15: a run that outlasts its bound is a tool failure; the agent stops it there, on the source too.
        with tempfile.TemporaryDirectory() as tmp:
            began = time.monotonic()
            result = agent.cmd_run(Namespace(log=f'{tmp}/x.log', cwd=None, env=None, gnu_time=None, tee=False,
                                             timeout=0.3, argv=['--', sys.executable, '-c',
                                                                'import time; time.sleep(30)']))
            self.assertLess(time.monotonic() - began, 10)
            self.assertTrue(result['timed_out'])
            self.assertNotEqual(result['exit_code'], 0)
            self.assertEqual(result['timeout_s'], 0.3)
            quick = agent.cmd_run(Namespace(log=f'{tmp}/y.log', cwd=None, env=None, gnu_time=None, tee=False,
                                            timeout=30, argv=['--', 'true']))
            self.assertEqual((quick['exit_code'], quick['timed_out']), (0, False))

    def test_run_kills_a_command_that_outlives_sigterm_under_gnu_time(self):
        # The run lines put GNU time in front of every command (--gnu-time). GNU time ignores only SIGINT and SIGQUIT,
        # so a SIGTERM to the whole group ends it at once, wait4 returns, and the SIGKILL used to be skipped: a command
        # that survived SIGTERM was left running. The agent now escalates on the group, not on its leader, and, with
        # /proc, spares GNU time (as Child.signal does on dest), so GNU time outlives the command.
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp, 'time')
            fake.write_text(f'#!{os.path.realpath(sys.executable)}\n' + GNU_TIME_LIKE)
            fake.chmod(0o755)
            for proc in (False, True):
                with self.subTest(proc=proc):
                    pid_file = Path(tmp, f'pid-{proc}')

                    def members(pgid):
                        return [pgid] + ([int(pid_file.read_text())] if pid_file.exists() else [])

                    began = time.monotonic()
                    with mock.patch.object(agent, 'RUN_KILL_GRACE', 1.0), \
                            mock.patch.object(measure, 'group_members', members if proc else (lambda pgid: None)):
                        result = agent.cmd_run(Namespace(
                            log=f'{tmp}/stubborn-{proc}.log', cwd=None, env=None, gnu_time=str(fake), tee=False,
                            timeout=1.0, argv=['--', sys.executable, '-c', STUBBORN, str(pid_file)]))
                    elapsed = time.monotonic() - began
                    pid = int(pid_file.read_text())
                    self.addCleanup(kill_quietly, pid)
                    self.assertTrue(result['timed_out'])
                    self.assertTrue(gone(pid), 'the command outlived the run')
                    self.assertGreaterEqual(elapsed, 1.9)  # the SIGTERM at 1 s, the SIGKILL RUN_KILL_GRACE later
            # A command that stops at the SIGTERM: with /proc, GNU time is spared and writes its report.
            with mock.patch.object(agent, 'RUN_KILL_GRACE', 5.0), \
                    mock.patch.object(measure, 'group_members',
                                      lambda pgid: [pgid] + [int(p.read_text()) for p in Path(tmp).glob('pid-polite')]):
                began = time.monotonic()
                result = agent.cmd_run(Namespace(
                    log=f'{tmp}/polite.log', cwd=None, env=None, gnu_time=str(fake), tee=False, timeout=1.0,
                    argv=['--', sys.executable, '-c', POLITE, str(Path(tmp, 'pid-polite'))]))
            self.assertTrue(result['timed_out'])
            self.assertLess(time.monotonic() - began, 4)  # no wait for the grace once the group is gone
            self.assertIsNotNone(result['gnu_time'])
            self.assertEqual(result['peak_rss_kb'], 4321)

    def test_tls_material_needs_openssl(self):
        with mock.patch.object(agent.subprocess, 'run', return_value=mock.Mock(returncode=1, stderr='no')), \
                tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit):
            agent.cmd_tls_material(Namespace(dir=f'{tmp}/tls', days=1))


# --- link tests ---------------------------------------------------------------------------------------------------

class LinkTests(unittest.TestCase):
    def test_the_steady_rate_is_the_median_of_the_last_3_seconds_after_the_warmup(self):
        per_second = [10 ** 8, 2 * 10 ** 8, 2.4 * 10 ** 8, 2.4 * 10 ** 8, 2.2 * 10 ** 8, 2.4 * 10 ** 8]
        steady = measure.steady_last(per_second, 3)
        self.assertEqual((steady['gbit'], steady['seconds']), (1.92, [3, 4, 5]))
        self.assertEqual(measure.steady_last(per_second[:5], 3)['seconds'], [3, 4])  # never a warmup second
        self.assertIsNone(measure.steady_last([], 3)['gbit'])
        counts = agent.PerSecond()
        for offset, count in ((0.1, 5), (0.9, 5), (1.0, 7), (2.99, 1), (3.2, 100)):
            counts.add(offset, count)
        result = counts.result(3.5, 1)
        self.assertEqual(result['per_second_bytes'], [10, 7, 1])  # second 3 isn't whole
        self.assertEqual(result['steady_last3_seconds'], [1, 2])

    def test_one_and_several_streams_over_loopback_record_seconds_and_connects(self):
        for streams in (1, 2):
            with self.subTest(streams=streams):
                probe = socket.socket()
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
                probe.close()
                received = {}

                def receive():
                    with redirect_stdout(io.StringIO()):
                        received.update(agent.cmd_blast_recv(Namespace(listen=f'127.0.0.1:{port}', timeout=30,
                                                                       warmup=1.0, streams=streams)))

                thread = threading.Thread(target=receive)
                thread.start()
                for _ in range(200):
                    try:
                        sent = agent.cmd_blast_send(Namespace(connect=f'127.0.0.1:{port}', seconds=2.3,
                                                              streams=streams, after_connect=True))
                        break
                    except ConnectionRefusedError:
                        time.sleep(0.02)
                thread.join(30)
                self.assertEqual(received['bytes'], sent['bytes'])
                self.assertTrue(sent['after_connect'])
                self.assertEqual(len(sent['per_stream_connect_s']), streams)
                self.assertLess(sent['connect_s'], 1.0)
                self.assertGreaterEqual(len(received['per_second_bytes']), 2)
                self.assertEqual(received['steady_last3_seconds'][0], 1)
                self.assertGreater(received['steady_last3_gbit'], 0)


# --- the TLS stand-in ---------------------------------------------------------------------------------------------

class Tls(unittest.TestCase):
    def test_the_stunnel_pairs_configurations(self):
        server = measure.stunnel_config('server', accept='10.0.0.13:7443', connect='127.0.0.1:7000',
                                        cert='/t/server.crt', key='/t/server.key', ca='/t/ca.crt', log='/l/s.log')
        client = measure.stunnel_config('client', accept='127.0.0.1:7001', connect='10.0.0.13:7443',
                                        cert='/t/client.crt', key='/t/client.key', ca='/t/ca.crt', log='/l/c.log')
        for text in (server, client):
            lines = text.splitlines()
            for line in ('foreground = yes', 'pid =', 'debug = info', 'socket = l:TCP_NODELAY=1',
                         'socket = r:TCP_NODELAY=1', 'verifyChain = yes', 'requireCert = yes',
                         'sslVersionMin = TLSv1.3', 'sslVersionMax = TLSv1.3', 'CAfile = /t/ca.crt'):
                self.assertIn(line, lines)
            # stunnel's default close timeout: the receiver's drain ends as in clear (EXP-13's revision).
            self.assertNotIn('TIMEOUTclose', text)
            self.assertNotIn('ciphersuites', text.lower())  # OpenSSL's default suites
        self.assertIn('accept = 10.0.0.13:7443', server)
        self.assertIn('connect = 127.0.0.1:7000', server)
        self.assertNotIn('client = yes', server)
        self.assertIn('client = yes', client)
        self.assertIn(f'checkHost = {measure.STUNNEL_SERVER_NAME}', client)
        self.assertIn('accept = 127.0.0.1:7001', client)

    def test_the_log_gives_the_connection_protocol_and_suite(self):
        log = measure.parse_stunnel_log(STUNNEL_SERVER_LOG)
        self.assertEqual((log['connections'], log['tls_version'], log['tls_suite']),
                         (1, 'TLSv1.3', 'TLS_AES_256_GCM_SHA384'))
        self.assertEqual(log['closed'], [{'to_tls': 41234, 'to_socket': 612889421}])
        self.assertEqual(log['refusals'], [])
        refused = measure.parse_stunnel_log(
            '2026.10.25 10:00:01 LOG5[1]: Service [pgdelta] accepted connection from 10.0.0.12:51300\n'
            '2026.10.25 10:00:01 LOG3[1]: SSL_accept: ssl/statem/statem_srvr.c:3511: error:0A0000C7:SSL routines::'
            'peer did not return a certificate\n')
        self.assertEqual((refused['connections'], refused['tls_version'], len(refused['refusals'])), (1, None, 1))

    @unittest.skipUnless(shutil.which('openssl'), 'needs the openssl command')
    def test_the_session_material_makes_mutual_tls_13_and_refuses_a_wrong_ca(self):
        with tempfile.TemporaryDirectory() as tmp:
            material = agent.cmd_tls_material(Namespace(dir=f'{tmp}/tls', days=2))
            files = material['files']
            self.assertEqual(oct(os.stat(files['client.key']).st_mode & 0o777), '0o600')
            self.assertTrue(all('fingerprint' in text.lower() for text in material['certificates'].values()))

            def handshake(client_cert):
                server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                server.minimum_version = ssl.TLSVersion.TLSv1_3
                server.load_cert_chain(files['server.crt'], files['server.key'])
                server.load_verify_locations(files['ca.crt'])
                server.verify_mode = ssl.CERT_REQUIRED
                client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                client.minimum_version = ssl.TLSVersion.TLSv1_3
                client.load_verify_locations(files['ca.crt'])
                if client_cert:
                    client.load_cert_chain(files[f'{client_cert}.crt'], files[f'{client_cert}.key'])
                left, right = socket.socketpair()
                outcome = {}

                def serve():
                    try:
                        with server.wrap_socket(left, server_side=True) as tls:
                            outcome['version'] = tls.version()
                            outcome['peer'] = tls.getpeercert()['subject']
                            tls.sendall(b'ok')
                    except (ssl.SSLError, OSError) as exc:
                        outcome['server_error'] = str(exc)
                        left.close()

                thread = threading.Thread(target=serve)
                thread.start()
                try:
                    with client.wrap_socket(right, server_hostname=measure.STUNNEL_SERVER_NAME) as tls:
                        outcome['read'] = tls.recv(2)
                except (ssl.SSLError, OSError) as exc:
                    outcome['client_error'] = str(exc)
                thread.join(10)
                return outcome

            good = handshake('client')
            self.assertEqual((good['version'], good['read']), ('TLSv1.3', b'ok'))
            self.assertIn((('commonName', 'pgd-exp13-client'),), good['peer'])
            self.assertIn('server_error', handshake('wrong-client'))
            self.assertIn('server_error', handshake(None))


# --- pgdelta's statistics -----------------------------------------------------------------------------------------

STATS = {'schema_version': 1, 'tool': 'pgdelta', 'status': 'ok', 'window': 16, 'high_churn_node_fraction': None,
         'high_churn_files': 0, 'bytes_source_to_dest': {'total': 1000}, 'bytes_dest_to_source': {'total': 10},
         'pages': {'sent': 3}, 'files': {'changed': 19},
         'timings_seconds': {'hash_source': 1.0, 'protocol': 2.0},
         'protocol': {'connect_s': 0.121, 'accept_wait_s': 0.25, 'accept_wait_busy_s': 0.01, 'accept_wait_idle_s': 0.24,
                      'basis_wait_s': 0.3, 'basis_wait_busy_s': 0.1, 'basis_wait_idle_s': 0.2,
                      'leaf_wait_s': 1.5, 'leaf_wait_busy_s': 0.5, 'leaf_wait_idle_s': 1.0, 'leaf_waits_over_1ms': 12,
                      'page_send_s': 3.25, 'complete_wait_s': 0.13, 'complete_wait_busy_s': 0.0,
                      'complete_wait_idle_s': 0.13, 'leaf_requests': 19},
         'receiver': {'prefetch': {'switch': 'on', 'bytes': 2 << 30, 'seconds': 1.4},
                      'timings_seconds': {'hello_wait_s': 0.19, 'recv_wait_s': 4.0, 'publish_s': 6.5}}}


class Statistics(unittest.TestCase):
    def test_the_protocol_report_reads_the_timers_wherever_they_are(self):
        report = measure.pgdelta_protocol_report(STATS)
        self.assertEqual((report['connect_s'], report['leaf_wait_s'], report['leaf_wait_busy_s'],
                          report['leaf_wait_idle_s'], report['page_send_s'], report['leaf_requests'],
                          report['leaf_waits_over_1ms'], report['window']),
                         (0.121, 1.5, 0.5, 1.0, 3.25, 19, 12, 16))
        self.assertEqual(report['receiver'], {'hello_wait_s': 0.19, 'recv_wait_s': 4.0, 'publish_s': 6.5,
                                              'published_utc': None})
        # pgdelta's own layout: the receiver's timers and its publication's moment under protocol_timers.
        timers = {'hello_wait_s': 0.2, 'recv_wait_s': 3.0, 'publish_s': 6.0, 'published_utc': 1790000006.5}
        self.assertEqual(measure.pgdelta_protocol_report({'receiver': {'protocol_timers': timers}})['receiver'],
                         timers)
        self.assertEqual(report['busy_idle_gap_s'], {'accept_wait': 0.0, 'basis_wait': 0.0, 'leaf_wait': 0.0,
                                                     'complete_wait': 0.0})
        # Nested parts ({'leaf_wait': {'total', 'busy', 'idle'}}) and bare names at the top level count too.
        nested = {'connect': 0.2, 'leaf_wait': {'total': 2.0, 'busy': 0.5, 'idle': 1.4},
                  'receiver': {'hello_wait_seconds': 0.3}}
        report = measure.pgdelta_protocol_report(nested)
        self.assertEqual((report['connect_s'], report['leaf_wait_s'], report['leaf_wait_busy_s'],
                          report['leaf_wait_idle_s'], report['busy_idle_gap_s']['leaf_wait']),
                         (0.2, 2.0, 0.5, 1.4, 0.1))
        self.assertEqual(report['receiver']['hello_wait_s'], 0.3)
        # A pgdelta without the timers: every one None, nothing raised.
        empty = measure.pgdelta_protocol_report({'timings_seconds': {'protocol': 2.0}})
        self.assertTrue(all(empty[f'{base}_s'] is None for base in measure.PGDELTA_SENDER_TIMERS))
        self.assertIsNone(empty['receiver']['publish_s'])
        # The sender's search never takes the receiver's timer of the same name.
        self.assertIsNone(measure.pgdelta_protocol_report({'receiver': {'connect_s': 9}})['connect_s'])

    def test_tool_reported_carries_what_the_smoke_table_names(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        clock = mock.Mock(return_value=1000.0)
        orchestrator = T12Stub(tmp.name, clock)
        self.addCleanup(orchestrator.pool.shutdown)
        orchestrator.source.agent = lambda *args, timeout=600: {'exit_code': 0, 'log_tail': json.dumps(STATS)}
        method = 'pgdelta_direct_inplace_w16'
        reported = run_exp04.Orchestrator.tool_reported(orchestrator, method, orchestrator.method_paths(method), {})
        for key in ('window', 'high_churn_node_fraction', 'high_churn_files', 'leaf_requests', 'pages', 'connect_s',
                    'accept_wait_s', 'basis_wait_busy_s', 'leaf_wait_idle_s', 'page_send_s', 'complete_wait_busy_s'):
            self.assertIn(key, reported)
        self.assertEqual(reported['receiver']['publish_s'], 6.5)
        self.assertEqual(reported['prefetch']['bytes'], 2 << 30)  # it stays where EXP-11 kept it
        self.assertTrue(reported['window_ok'])
        self.assertIsNone(reported['high_churn_ok'])
        reported = run_exp04.Orchestrator.tool_reported(orchestrator, V1, orchestrator.method_paths(V1), {})
        self.assertFalse(reported['window_ok'])  # v1 pins 4; these statistics say 16
        record = {'method': method, 'condition': {'gbit': 2.0, 'rtt_ms': 120.0, 'host_tcp': 'tuned'},
                  'tool_reported': run_exp04.Orchestrator.tool_reported(orchestrator, method,
                                                                        orchestrator.method_paths(method), {}),
                  'receiver_wall_s': 6.62}
        row = measure.summary_row(record)
        self.assertEqual((row['window'], row['leaf_wait_idle_s'], row['publish_s'], row['drain_s'],
                          row['busy_split_valid'], row['prefetch_bytes']), (16, 1.0, 6.5, 0.12, True, 2 << 30))
        self.assertFalse(measure.summary_row({**record, 'method': TLS})['busy_split_valid'])
        # With the receiver's launch on dest's clock and pgdelta's publication on the same clock, publish_s and the
        # drain are read there: from its launch to its publication, then to its exit, adding up to its wall.
        record['tool_reported']['receiver']['published_utc'] = 1790000006.45
        record['phases'] = {measure.DIRECT_PHASE: {'commands': [
            {'host': 'dest', 'role': 'receiver', 'started_utc': 1790000000.0, 'wall_seconds': 6.62}]}}
        self.assertEqual(measure.receiver_exit_utc(record), 1790000006.62)
        row = measure.summary_row(record)
        self.assertEqual((row['publish_s'], row['drain_s'], row['receiver_wall_s']), (6.45, 0.17, 6.62))
        self.assertEqual(record['tool_reported']['receiver']['publish_s'], 6.5)  # pgdelta's own stays in the record


# --- durability: check (a)'s limit by the run's clock -------------------------------------------------------------

class NoiseTable(unittest.TestCase):
    def test_a_runs_limit_is_the_noise_of_the_shortest_length_that_covers_its_clock(self):
        self.assertEqual(measure.parse_noise_plan('5:20,30:6,120:3'), [(5.0, 20), (30.0, 6), (120.0, 3)])
        given = measure.parse_noise_table('5:4,30:4,120:4')
        measured = {5.0: 6 << 20, 30.0: 5 << 20, 120.0: 12 << 20}
        table = measure.noise_table(given, measured)
        # A length's noise is the largest over windows of that length or shorter.
        self.assertEqual(table, {5.0: 6 << 20, 30.0: 6 << 20, 120.0: 12 << 20})
        self.assertEqual(measure.noise_limit(table, 3.1), {'window_s': 5.0, 'noise_bytes': 6 << 20,
                                                           'beyond_table': False})
        self.assertEqual(measure.noise_limit(table, 5.0)['window_s'], 5.0)
        self.assertEqual(measure.noise_limit(table, 41.0)['window_s'], 120.0)
        self.assertEqual(measure.noise_limit(table, 200.0), {'window_s': 120.0, 'noise_bytes': 12 << 20,
                                                             'beyond_table': True})
        for bad in ('5', '5:x', '0:4'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                measure.parse_noise_table(bad)

    def test_the_durability_check_takes_each_runs_own_limit(self):
        def record(seconds, increase_mib):
            phase = {'memory': {'dest': {'dirty_writeback_bytes_before': 0,
                                         'dirty_writeback_bytes_after': increase_mib << 20}}}
            return {'method': V1, 'phases': {'receiver_transfer': phase}, 'totals': {'wall_seconds': seconds}}

        table = {5.0: 5 << 20, 30.0: 9 << 20, 120.0: 20 << 20}
        short = measure.durability_check(record(3.2, 8), None, None, table)
        self.assertEqual((short['a_ok'], short['limit_bytes'], short['noise_window_s']), (False, 5 << 20, 5.0))
        long = measure.durability_check(record(41.0, 8), None, None, table)
        self.assertEqual((long['a_ok'], long['limit_bytes'], long['noise_window_s'], long['noise_beyond_table']),
                         (True, 20 << 20, 120.0, False))
        self.assertEqual(measure.durability_check(record(3.2, 3), None, None, {5.0: 1 << 20})['limit_bytes'],
                         measure.DIRTY_LIMIT_BYTES)
        # Without a table, EXP-11's one limit, as before.
        self.assertNotIn('noise_window_s', measure.durability_check(record(3.2, 3), None, 1 << 20))
        row = measure.summary_row({**record(41.0, 8), 'condition': {'gbit': 2.0}, 'speed': {'label': '2:60:default'},
                                   'durability': long})
        self.assertEqual((row['noise_window_s'], row['dirty_limit_bytes']), (120.0, 20 << 20))


# --- A15: the void rule on user-table files ---------------------------------------------------------------------

class VoidRule(unittest.TestCase):
    def test_differences_by_file_class(self):
        start = {'entries': {'base/5/16397': ['file', 8, 'a' * 64], 'base/5/16397_fsm': ['file', 8, 'b' * 64],
                             'base/5/16397.1': ['file', 8, 'f' * 64], 'base/5/1259': ['file', 8, 'c' * 64],
                             'global/1262': ['file', 8, 'd' * 64], 'PG_VERSION': ['file', 3, 'e' * 64]}}
        end = copy.deepcopy(start)
        end['entries']['base/5/1259'][2] = '0' * 64
        end['entries']['PG_VERSION'][2] = '0' * 64
        user = ['base/5/16397']
        classes = measure.reference_classes(start, end, user)
        self.assertEqual(classes['counts'], {'user_table': 0, 'catalog': 1, 'other': 1})
        self.assertTrue(classes['user_tables_match'])
        end['entries']['base/5/16397_fsm'][2] = '0' * 64
        end['entries']['base/5/16397.1'][1] = 9
        classes = measure.reference_classes(start, end, user)
        self.assertEqual(classes['paths']['user_table'], ['base/5/16397.1', 'base/5/16397_fsm'])
        self.assertFalse(classes['user_tables_match'])
        self.assertIsNone(measure.reference_classes(start, end, None)['user_tables_match'])
        self.assertIn("relname LIKE 'pgbench\\_%' OR relname = 'events'", measure.USER_TABLE_FILES_SQL)
        self.assertIn('reltoastrelid', measure.USER_TABLE_FILES_SQL)
        self.assertIn('pg_index', measure.USER_TABLE_FILES_SQL)

    def test_a_probackup_difference_is_void_only_if_a_user_table_differs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        clock = mock.Mock(return_value=1000.0)
        orchestrator = T12Stub(tmp.name, clock)
        self.addCleanup(orchestrator.pool.shutdown)
        source = {'pgbench_accounts': {'rows': 1, 'md5': 'a'}}
        orchestrator.query = lambda *args, **kwargs: json.dumps(source)
        for user_match, status in ((True, 'fail'), (False, 'void'), (None, 'fail')):
            with self.subTest(user_match=user_match):
                record = {'trial_id': orchestrator.trial_id, 'method': 'probackup8', 'status': 'pending', 'repeat': 0,
                          'speed': {'label': '2:60:default', 'slug': '2g-60ms-default'},
                          'verification': {'logical': {'pgbench_accounts': {'rows': 1, 'md5': 'b'}}}}
                orchestrator.records = [record]
                # The whole digests differ (catalogs changed), as in every EXP-11 trial.
                trial = {'reference_match': False, 'reference_user_tables_match': user_match, 'runs': []}
                check = orchestrator.finish_logical_checks(trial)
                self.assertEqual(record['status'], status)
                self.assertEqual(check['void_rule'], 'A15: user-table files only')


# --- the visit ----------------------------------------------------------------------------------------------------

class Visits(Clocked13):
    def test_a_visit_sets_the_tree_and_settings_checks_them_and_tests_the_link(self):
        orchestrator = self.orchestrator(extra_args=['--first-visit-link-streams', '16@2:120:tuned',
                                                     '--stunnel-link-test', '2:120:tuned', '--ssh-probe-port', '22'])
        condition, link, trial = self.visit(orchestrator, '2:120:tuned')
        self.assertEqual(orchestrator.applied['label'], '2:120:tuned')
        self.assertEqual(orchestrator.kernel_tcp['source']['net.ipv4.tcp_rmem'], '4096\t131072\t134217728')
        visit = trial['visits'][0]
        self.assertTrue(visit['ok'])
        self.assertEqual(len(visit['attempts']), 1)
        self.assertEqual(visit['tree']['source']['problems'], [])
        self.assertEqual(visit['pings']['min_ms'], 120.2)
        self.assertEqual(visit['control_noops']['seconds'], [0.011] * 5)
        self.assertEqual(visit['link_tests'][0]['steady_gbit'], link['source_to_dest_steady_gbit'])
        self.assertIn('per_second_gbit', visit['link_tests'][0])
        first = trial['first_visit']['2:120:tuned']
        self.assertEqual([(test['direction'], test['streams']) for test in first['link_tests']],
                         [('dest_to_source', 1), ('source_to_dest', 8), ('source_to_dest', 16)])
        self.assertIn('qdisc', first['link_tests'][1])  # netem's and the tbf's drops from one snapshot each
        self.assertEqual(first['ssh_probe']['last_gbit'], 0.25)
        stunnel = [event for event in orchestrator.events if isinstance(event, tuple) and event[0] == 'blast'
                   and event[3] is not None]
        self.assertEqual(stunnel, [('blast', 'source_to_dest', 1, '127.0.0.1:7000', '127.0.0.1:7001')])
        self.assertEqual(first['stunnel_link_test']['stunnel']['tls_version'], 'TLSv1.3')
        # Every link test follows a TCP metrics flush of its own.
        last = None
        for event in orchestrator.events:
            kind = event if isinstance(event, str) else event[0]
            if kind == 'blast':
                self.assertEqual(last, 'tcp_metrics_flush')
            if kind in ('blast', 'tcp_metrics_flush'):
                last = kind
        self.assertIn('source_to_dest_16streams_steady_gbit', link)
        self.assertIn('dest_to_source_gbit', link)
        # The busy threshold: the one-stream rate times the ping minimum.
        rate = link['source_to_dest_steady_gbit']
        self.assertEqual(orchestrator.condition['busy_inflight_bytes'], int(rate * 1e9 / 8 * 120.2 / 1000))
        # A later visit to the condition repeats none of the first-visit probes.
        orchestrator.events.clear()
        self.visit(orchestrator, '2:120:tuned', trial, 1)
        self.assertNotIn('ssh_probe', orchestrator.events)
        self.assertEqual(len(trial['visits']), 2)

    def test_the_stunnel_link_test_sends_longer_and_needs_3_steady_seconds(self):
        # The sender's seconds start at its connect to the source's stunnel on localhost; its first byte reaches dest
        # about 2.5 r later. It sends ceil(3 r) more seconds (at least 1), so the receiver keeps 3 whole seconds after
        # its 3 s warmup; a test that still has fewer fails its probe rather than giving a rate on 2.
        self.assertEqual([measure.stunnel_link_test_extra_s(r) for r in (0, 20, 60, 120, 400)], [1, 1, 1, 1, 2])
        orchestrator = self.orchestrator(extra_args=['--stunnel-link-test', '2:0:default,2:120:tuned'])
        _, _, trial = self.visit(orchestrator, '2:120:tuned')
        stunnel = [seconds for event, seconds in zip(
            [event for event in orchestrator.events if isinstance(event, tuple) and event[0] == 'blast'],
            orchestrator.blast_seconds) if event[3] is not None]
        self.assertEqual(stunnel, [7])  # --link-test-seconds 6, plus 1
        first = trial['first_visit']['2:120:tuned']
        self.assertEqual(first['stunnel_link_test']['steady_seconds'], [3, 4, 5])
        self.assertTrue(first['stunnel_link_test']['steady_ok'])
        # The direct tests keep --link-test-seconds.
        self.assertEqual(set(orchestrator.blast_seconds) - {7}, {None})
        orchestrator = self.orchestrator(extra_args=['--stunnel-link-test', '2:0:default'])
        # The visit's own test (6 whole seconds), its 2 first-visit tests, then the stunnel test with 5 whole seconds:
        # 2 after the warmup.
        orchestrator.blast_whole_seconds = [6, 6, 6, 5]
        _, link, trial = self.visit(orchestrator, '2:0:default')
        test = trial['first_visit']['2:0:default']['stunnel_link_test']
        self.assertIn('rests on 2 whole seconds', test['error'])
        self.assertIn(('stunnel', 'stop'), orchestrator.events)  # the pair stopped all the same
        # The visit's own link test is no probe: a short one is recorded, and the visit goes on.
        orchestrator = self.orchestrator()
        orchestrator.blast_whole_seconds = [5]
        _, link, trial = self.visit(orchestrator, '2:60:default')
        self.assertFalse(trial['visits'][0]['link_tests'][0]['steady_ok'])
        self.assertEqual(trial['visits'][0]['link_tests'][0]['steady_seconds'], [3, 4])

    def test_the_samplers_stop_when_the_tree_cannot_be_read_before_a_link_test(self):
        orchestrator = self.orchestrator()
        self.visit(orchestrator, '2:60:default')
        orchestrator.events.clear()
        real = orchestrator.wan

        def wan(action, condition=None):
            if action == 'show':
                raise run_exp04.CommandError('tc failed')
            return real(action, condition)

        orchestrator.wan = wan
        with self.assertRaises(run_exp04.CommandError):
            orchestrator.link_test('source_to_dest', 8, qdisc=True)
        kinds = [event[:2] for event in orchestrator.events if isinstance(event, tuple) and event[0] == 'net_samplers']
        self.assertEqual(kinds, [('net_samplers', 'start'), ('net_samplers', 'stop')])

    def test_a_sampler_that_started_is_stopped_when_the_other_vm_s_fails(self):
        # Both sides start at once; one failing used to leave the other running tc every 0.5 s for the session.
        for failing in ('dest', 'source'):
            with self.subTest(failing=failing):
                orchestrator = self.orchestrator()
                calls = []

                def local_agent(*args, timeout=600):
                    calls.append(('dest', args[:2]))
                    if failing == 'dest' and args[:2] == ('net-sampler', 'start'):
                        raise run_exp04.CommandError('dest agent net-sampler failed')
                    return {'pid': 1}

                def source_agent(*args, timeout=600):
                    calls.append(('source', args[:2]))
                    if failing == 'source' and args[:2] == ('net-sampler', 'start'):
                        raise run_exp04.CommandError('source agent net-sampler failed')
                    return {'pid': 2}

                orchestrator.local_agent, orchestrator.source.agent = local_agent, source_agent
                with self.assertRaises(run_exp04.CommandError):
                    run_exp04.Orchestrator.start_net_samplers(orchestrator, 'run-full')
                other = 'source' if failing == 'dest' else 'dest'
                self.assertIn((other, ('net-sampler', 'stop')), calls)
                self.assertNotIn((failing, ('net-sampler', 'stop')), calls)

    def test_the_default_anchors_rates_are_the_zero_delay_rates(self):
        orchestrator = self.orchestrator()
        trial = {'runs': []}
        _, link, _ = self.visit(orchestrator, '2:0:default', trial)
        self.assertEqual(orchestrator.zero_delay[2.0], {'one_stream': 1.92, 'bound': 1.92})
        _, link, _ = self.visit(orchestrator, '2:60:default', trial)
        self.assertEqual((link['zero_delay_gbit'], link['zero_delay_bound_gbit']), (1.92, 1.92))
        # parcopy8's 8 windows at 60 ms carry 0.35 Gbit/s here: under 0.9 x the zero-delay rate, a window-limited bound.
        self.assertTrue(measure.window_limited_bound('parcopy8', link))
        self.assertIsNone(measure.window_limited_bound(V1, link))

    def test_a_failed_check_is_reset_and_checked_once_more(self):
        orchestrator = self.orchestrator()
        orchestrator.ping_offsets = [-0.5]  # the first check's pings come back under r: not delayed
        condition, link, trial = self.visit(orchestrator, '2:60:default')
        visit = trial['visits'][0]
        self.assertTrue(visit['ok'])
        self.assertEqual(len(visit['attempts']), 2)
        self.assertIn('the ping minimum', visit['attempts'][0]['checks']['problems']['pings'][0])
        self.assertIn('reset', visit['attempts'][1])
        # Between the two: the tree off and the defaults written back, then both set again.
        self.assertEqual([event[:2] for event in orchestrator.events if isinstance(event, tuple)
                          and event[0] in ('wan', 'set_tcp') and event[1] in ('off', 'default', 'apply')],
                         [('wan', 'apply'), ('set_tcp', 'default'), ('wan', 'off'), ('set_tcp', 'default'),
                          ('wan', 'apply'), ('set_tcp', 'default')])

    def test_a_second_failure_stops_the_trial_as_an_infrastructure_fault(self):
        orchestrator = self.orchestrator()
        orchestrator.noops = [0.030] * 5  # the control path is delayed
        trial = {'runs': []}
        with self.assertRaises(run_exp04.InfrastructureFault):
            self.visit(orchestrator, '2:60:default', trial)
        self.assertFalse(trial['visits'][0]['ok'])
        self.assertEqual(len(trial['visits'][0]['attempts']), 2)
        self.assertNotIn(('blast', 'source_to_dest', 1, None, None), orchestrator.events)  # no link test, no run

    def test_a_wrong_tree_or_setting_fails_the_check(self):
        orchestrator = self.orchestrator()
        orchestrator.shown = tree_json(measure.parse_condition('2:20:default'))  # tc shows another tree
        with self.assertRaises(run_exp04.InfrastructureFault) as caught:
            self.visit(orchestrator, '2:60:default')
        self.assertIn("netem's delay", str(caught.exception))
        orchestrator = self.orchestrator()
        orchestrator.stuck_tcp = True
        with self.assertRaises(run_exp04.InfrastructureFault) as caught:
            self.visit(orchestrator, '2:120:tuned')
        self.assertIn('tcp_rmem', str(caught.exception))


# --- the run ------------------------------------------------------------------------------------------------------

class Runs(Clocked13):
    def test_the_guard_flush_and_samplers_come_before_the_clock(self):
        orchestrator = self.orchestrator()
        orchestrator.window_writes = 200_000_000  # check (b): the full copies' output reaches dest's array
        for method in ('full', V1, 'parcopy8'):
            with self.subTest(method=method):
                record = self.run_at(orchestrator, '2:60:default', method)
                self.assertEqual(record['status'], 'pass', record.get('traceback'))
                kinds = self.kinds(orchestrator)
                drop = kinds.index('drop_caches')
                self.assertLess(kinds.index('tcp_metrics_flush'), drop)
                self.assertLess(kinds.index('net_samplers'), drop)
                self.assertEqual(kinds.index('tcp_metrics_flush'), drop - 1)  # the last step before the drop
                self.assertGreater(len(kinds) - 1 - kinds[::-1].index('net_samplers'), drop)  # stopped after
                self.assertEqual(record['sysctl']['tcp']['ok'], True)
                self.assertEqual(record['sysctl']['tcp']['source']['net.ipv4.tcp_rmem'], '4096\t131072\t6291456')
                self.assertEqual(record['condition']['label'], '2:60:default')
                self.assertEqual(record['tcp_metrics']['dest']['entries'], 0)
                self.assertEqual(record['network_checks']['ok'], True, record['network_checks'])
                self.assertEqual(record['ss']['minrtt_ms_min'], 60.1)
                self.assertEqual(record['qdisc']['samples']['source']['at_limit'], False)
                phase = next(iter(record['phases'].values()))
                self.assertEqual(phase['qdisc']['source']['netem_own_drops'], 0)
                self.assertEqual(phase['nstat']['source']['TcpRetransSegs'], 3)
                self.assertTrue(orchestrator.record_path(record).name.startswith('r0-2g-60ms-default-'))
        row = measure.summary_row(record)
        self.assertEqual((row['speed'], row['rate_gbit'], row['rtt_ms'], row['host_tcp'], row['network_ok'],
                          row['excluded_reason'], row['inferred'], row['netem_own_drops'], row['retrans_segs_source'],
                          row['minrtt_ms'], row['tbf_overlimits_source']),
                         ('2:60:default', 2.0, 60.0, 'default', True, None, False, 0, 3, 60.1, 7))

    def test_each_arm_samples_into_its_own_directory(self):
        # One directory per visit let an earlier arm's final record stand for a later arm's.
        orchestrator = self.orchestrator()
        for method in ('full', V1):
            self.run_at(orchestrator, '2:60:default', method)
            self.assertIn(('net_samplers', 'start', f'run-{method}'), orchestrator.events)

    def test_a_drifted_setting_is_reset_by_the_guard_and_one_that_wont_refuses_the_run(self):
        orchestrator = self.orchestrator()
        condition, link, _ = self.visit(orchestrator, '2:120:tuned')
        orchestrator.kernel_tcp['dest']['net.ipv4.tcp_wmem'] = '4096\t16384\t4194304'
        with mock.patch.object(run_exp04, 'Child', FakeChild):
            record = orchestrator.run_method(TRIAL, 0, condition, dict(link), V1, 0, [V1])
        self.assertEqual(record['status'], 'pass', record.get('traceback'))
        self.assertIn('guard_reset', record['sysctl']['tcp'])
        self.assertTrue(record['sysctl']['tcp']['ok'])
        orchestrator.kernel_tcp['dest']['net.ipv4.tcp_wmem'] = '4096\t16384\t4194304'
        orchestrator.stuck_tcp = True
        orchestrator.events.clear()
        with mock.patch.object(run_exp04, 'Child', FakeChild):
            record = orchestrator.run_method(TRIAL, 1, condition, dict(link), V1, 0, [V1])
        self.assertEqual((record['status'], record['failure_kind']), ('error', 'harness'))
        self.assertNotIn('drop_caches', orchestrator.events)
        self.assertFalse(record['network_checks']['ok'])
        self.assertIn('A11: the TCP sysctl guard failed', measure.excluded_reason(record))

    def test_the_emulators_overflow_and_a_wrong_minrtt_exclude_the_run(self):
        orchestrator = self.orchestrator()
        orchestrator.net_drops = (3, 500)  # 500 refused by the tbf (counted by netem too), and 3 of netem's own
        record = self.run_at(orchestrator, '2:60:default', 'full')
        problems = record['network_checks']['problems']
        self.assertEqual(problems, ["netem's own drops on source: 3", "netem's own drops on dest: 3"])
        self.assertEqual(measure.summary_row(record)['netem_own_drops'], 6)
        orchestrator = self.orchestrator()
        orchestrator.net_drops = (0, 500)  # drops at the shaper alone: how CUBIC finds the rate, not excluded
        record = self.run_at(orchestrator, '2:60:default', 'full')
        self.assertTrue(record['network_checks']['ok'])
        self.assertEqual(measure.summary_row(record)['tbf_drops_source'], 500)
        orchestrator = self.orchestrator()
        orchestrator.minrtt_extra = 2.5
        record = self.run_at(orchestrator, '2:60:default', V1)
        self.assertIn("minrtt [62.5, 62.5] ms is outside [60, 62] ms", record['network_checks']['problems'][0])
        orchestrator = self.orchestrator()
        orchestrator.net_qlen = measure.WAN_NETEM_LIMIT
        record = self.run_at(orchestrator, '2:60:default', V1)
        self.assertTrue(any('reached its limit' in problem for problem in record['network_checks']['problems']))

    def test_v1_tls_runs_through_the_stunnel_pair(self):
        orchestrator = self.orchestrator()
        record = self.run_at(orchestrator, '2:120:tuned', TLS)
        self.assertEqual(record['status'], 'pass', record.get('traceback'))
        kinds = self.kinds(orchestrator)
        drop = kinds.index('drop_caches')
        stunnel = [index for index, kind in enumerate(kinds) if kind == 'stunnel']
        self.assertLess(stunnel[0], drop)  # started before the clock
        self.assertGreater(stunnel[1], drop)  # stopped after it
        receive = FakeChild.started[-1].argv
        self.assertEqual(receive[receive.index('--listen') + 1], '127.0.0.1:7000')
        send = next(event[1] for event in orchestrator.events if isinstance(event, tuple) and event[0] == 'source'
                    and 'send' in event[1])
        self.assertEqual(send[send.index('--connect') + 1], '127.0.0.1:7001')
        self.assertEqual(record['verification']['tls'], {'tls_version': 'TLSv1.3', 'tls_suite': 'TLS_AES_256_GCM_SHA384',
                                                         'connections': 1, 'ok': True})
        row = measure.summary_row(record)
        self.assertEqual((row['tls_version'], row['tls_suite'], row['tls_connections']),
                         ('TLSv1.3', 'TLS_AES_256_GCM_SHA384', 1))

    def test_a_tls_run_without_tls_13_fails_its_verification(self):
        class Plain(T12Stub):
            def stop_stunnels(self, started):
                result = super().stop_stunnels(started)
                return {**result, 'tls_version': 'TLSv1.2'}

        orchestrator = self.orchestrator(Plain)
        record = self.run_at(orchestrator, '2:0:default', TLS)
        self.assertEqual(record['status'], 'fail')
        self.assertFalse(record['verification']['tls']['ok'])

    def test_the_busy_threshold_reaches_each_pgdelta_sender_where_it_takes_it(self):
        orchestrator = self.orchestrator()
        self.visit(orchestrator, '2:60:default')
        paths = orchestrator.method_paths(V1)
        self.assertNotIn('--busy-inflight-bytes', orchestrator.pgdelta_send_argv(V1, paths))
        orchestrator.busy_flag = True
        argv = [str(part) for part in orchestrator.pgdelta_send_argv(V1, paths)]
        self.assertEqual(argv[argv.index('--busy-inflight-bytes') + 1],
                         str(orchestrator.condition['busy_inflight_bytes']))
        self.assertEqual(argv[argv.index('--window') + 1], '4')

    def test_method_flags_carry_each_arms_command_line_and_the_stunnel_configurations(self):
        orchestrator = self.orchestrator()
        for method in (V1, *measure.T12_V1_ARMS):
            with self.subTest(method=method):
                flags = orchestrator.method_flags(method)
                self.assertEqual(flags['send_argv'][3], 'send')
                self.assertEqual(flags['receive_argv'][3], 'receive')
                self.assertEqual(flags['send'], list(measure.PGDELTA_ARGS[method]['send']))
        tls = orchestrator.method_flags(TLS)
        self.assertIn('client = yes', tls['stunnel']['client'])
        self.assertIn('connect = 127.0.0.1:7000', tls['stunnel']['server'])
        self.assertEqual(orchestrator.method_flags('parcopy16')['send'][:2], ['--streams', '16'])

    def test_probackup_records_a_ps_mid_run_and_its_ticker_runs_on_the_source(self):
        orchestrator = self.orchestrator()
        argv = orchestrator.wal_ticker_argv()
        self.assertEqual(argv[0], 'ssh')
        self.assertIn('Port=2222', argv)
        remote_command = argv[-1]
        self.assertIn(f'{orchestrator.work}/wal-ticker.sql', remote_command)
        self.assertIn(f'-h {orchestrator.source_root}/s', remote_command)  # its Unix socket, not the network
        self.assertIn('read x; kill $p', remote_command)
        self.assertEqual(orchestrator.method_flags('probackup8')['wal_ticker']['argv'], argv)
        with mock.patch.object(run_exp04, 'PROCESSES_SNAPSHOT_S', 0.0), \
                mock.patch.object(T12Stub, 'processes_snapshot', lambda self: {'source': ['sshd: bench'], 'dest': []}):
            record = self.run_at(orchestrator, '2:60:default', 'probackup8')
        self.assertEqual(record['processes'], {'source': ['sshd: bench'], 'dest': []})


class Stalling(T12Stub):
    """T12Stub whose pg_probackup catchup is a real process that stalls (a hang over SSH), run as run_exp04 runs any
    timed command (Child, waited for with the run's bound)."""

    def local_command(self, argv, log_name, *, timeout=None, cwd=None, env=None, stamp_lines=False):
        if Path(str(argv[0])).name.startswith('pg_probackup'):
            return run_exp04.Orchestrator.local_command(
                self, [sys.executable, '-c', 'import time; time.sleep(60)'], log_name, timeout=timeout, cwd=cwd,
                env=env, stamp_lines=stamp_lines)
        return super().local_command(argv, log_name, timeout=timeout, cwd=cwd, env=env, stamp_lines=stamp_lines)


class NeverListening(T12Stub):
    """T12Stub whose receiver is a real process that never listens (a hang before --listen); it ends at SIGTERM."""

    def receive_argv(self, method, paths):
        return [sys.executable, '-c', 'import time; time.sleep(60)']


class RunLimits(Clocked13):
    """A15's timeout leg: a run that reaches four times its expected upper bound (at least 60 s; probackup8 240 s) is
    stopped there and is a tool failure, failure_reason 'timeout'."""

    def test_a_receiver_not_listening_by_the_bound_is_a_timeout(self):
        # start_receiver gives up at what is left of the run's bound and stops the receiver with SIGTERM, at which it
        # exits: its record used to say timed_out False, so the run had failure_kind 'tool' and no failure_reason.
        orchestrator = self.orchestrator(NeverListening)
        orchestrator.gnu_time = None
        condition, link, _ = self.visit(orchestrator, '2:60:default')
        began = time.monotonic()
        with mock.patch.object(measure, 't12_run_limit',
                               return_value={'limit_s': 0.5, 'upper_s': None, 'from': 'a test'}):
            record = orchestrator.run_method(TRIAL, 0, condition, dict(link), V1, 0, [V1])
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual((record['status'], record['failure_kind'], record.get('failure_reason')),
                         ('error', 'tool', 'timeout'))
        receiver = next(iter(record['phases'].values()))['commands'][0]
        self.assertTrue(receiver['timed_out'])
        self.assertIn('(timed out)', record['error'])
        self.assertEqual(measure.summary_row(record)['failure_reason'], 'timeout')
        # Outside a run's bound, giving up after --command-timeout (at most 600 s) is not A15's timeout.
        orchestrator.args.command_timeout = 0.3
        holder = {}
        paths = orchestrator.method_paths(V1)
        [result] = run_exp04.Orchestrator.start_receiver(orchestrator, V1, paths, holder)
        self.assertFalse(result['timed_out'])
        self.assertNotEqual(result['exit_code'], 0)

    def test_each_arms_bound_is_four_times_its_written_upper_bound(self):
        limit = measure.t12_run_limit
        self.assertEqual(limit('probackup8', 'uniform1', '2:60:default')['limit_s'], 240)
        self.assertEqual(limit('full', 'uniform1', '2:120:default')['limit_s'], 408)  # E4: 102 s
        self.assertEqual(limit('full', 'hot', '2:60:default')['limit_s'], 208)
        self.assertEqual(limit('full', 'uniform1', '2:20:default')['limit_s'], 72)
        self.assertEqual(limit('full', 'hot', '2:0:default')['limit_s'], 60)  # E1's 10.8 s: the 60 s floor
        self.assertEqual(limit('full_nosync', 'hot', '2:0:default')['limit_s'], 60)
        self.assertEqual(limit('parcopy8', 'uniform1', '1:120:default')['limit_s'], 80)  # E5: 20.0 s
        self.assertEqual(limit('parcopy16', 'hot', '2:120:default')['limit_s'], 60)
        self.assertEqual(limit(V1, 'uniform1', '2:120:default')['limit_s'], 136)  # E7: 34 s
        self.assertEqual(limit(V1, 'uniform1', '1:60:default')['limit_s'], 74.4)
        self.assertEqual(limit(V1, 'uniform10', '2:60:default')['limit_s'], 124)  # E8: 31 s
        self.assertEqual(limit(V1, 'hot', '2:120:default')['upper_s'], 7.8)  # E1's 3.4 s plus E6's 4.4 s
        self.assertEqual(limit('pgdelta_direct_inplace_hc', 'uniform10', '2:120:tuned')['upper_s'], 15.3)
        self.assertEqual(limit('pgdelta_direct_inplace_hc', 'uniform10', '2:120:tuned')['limit_s'], 61.2)
        # No written bound: --command-timeout, as outside EXP-13.
        self.assertIsNone(limit(V1, 'append1', '2:60:default')['limit_s'])
        self.assertIsNone(limit('full', 'hot', '5:0:default')['limit_s'])
        # Every arm at every condition the run lines run has one.
        for name, argv in spec_run_lines().items():
            args = parse(*argv)
            plan = run_exp04.t12_plan(args)
            workload = measure.t12_workload(args.patterns, float(args.fractions))
            for _, label, method in measure.t12_schedule(run_exp04.method_list(args.methods),
                                                         measure.parse_conditions(args.conditions), args.repeats, plan):
                with self.subTest(invocation=name, label=label, method=method):
                    self.assertIsNotNone(limit(method, workload, label)['limit_s'])

    def test_the_bounds_are_the_written_ranges_upper_ends(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'analysis'))
        try:
            import exp13_model
        finally:
            sys.path.pop(0)
        written = exp13_model.WRITTEN
        for label, value in measure.T12_UPPER_S['full']['hot'].items():
            if label in written['E4'] or ('full', label) in written['E4']:
                self.assertEqual(value, written['E4'][('full', label)][1])
        self.assertEqual(measure.T12_UPPER_S['full']['hot']['2:0:default'], exp13_model.ANCHOR['full'][1])
        for arm in ('parcopy8', 'parcopy16'):
            for label, value in measure.T12_UPPER_S[arm]['uniform1'].items():
                expected = exp13_model.ANCHOR[arm][1] if label == '2:0:default' else written['E5'][(arm, label)][1]
                self.assertEqual(value, expected, (arm, label))
        v1 = measure.T12_UPPER_S['v1']
        for label, value in v1['uniform1'].items():
            expected = (exp13_model.ANCHOR['uniform1'][1] + (0.1 if label == '2:0:tuned' else 0)
                        if label.startswith('2:0:') else written['E7'][('uniform1', label)][1])
            self.assertAlmostEqual(value, expected, msg=label)
        for label, value in v1['uniform10'].items():
            expected = (written['E8'][('uniform10', '2:0:default')][1] + (0.1 if label == '2:0:tuned' else 0)
                        if label.startswith('2:0:') else written['E8'][('uniform10', label)][1])
            self.assertAlmostEqual(value, expected, msg=label)
        for label, value in v1['hot'].items():
            expected = exp13_model.ANCHOR['hot'][1] + (0.1 if label == '2:0:tuned' else 0
                                                       if label.startswith('2:0:')
                                                       else written['E6'][('hot, added', label)][1])
            self.assertAlmostEqual(value, expected, msg=label)

    def test_a_stalled_copy_is_stopped_at_its_bound_as_a_tool_failure(self):
        orchestrator = self.orchestrator(Stalling, ['--methods', f'full,parcopy8,probackup8,{V1},{TLS}'])
        orchestrator.gnu_time = None
        condition, link, _ = self.visit(orchestrator, '2:60:default')
        began = time.monotonic()
        with mock.patch.object(measure, 'T12_TIMEOUT_PROBACKUP_S', 0.5), \
                mock.patch.object(T12Stub, 'processes_snapshot', lambda self: {'source': [], 'dest': []}):
            record = orchestrator.run_method(TRIAL, 0, condition, dict(link), 'probackup8', 0, ['probackup8'])
        self.assertLess(time.monotonic() - began, 10)  # stopped at its bound (0.5 s, at least RUN_LIMIT_FLOOR_S)
        self.assertEqual((record['status'], record['failure_kind'], record['failure_reason']),
                         ('error', 'tool', 'timeout'))
        self.assertEqual(record['run_limit']['limit_s'], 0.5)
        catchup = next(iter(record['phases'].values()))['commands'][0]
        self.assertTrue(catchup['timed_out'])
        self.assertIn('(timed out)', record['error'])
        row = measure.summary_row(record)
        self.assertEqual((row['run_limit_s'], row['failure_reason'], row['failure_kind']), (0.5, 'timeout', 'tool'))
        self.assertIsNone(orchestrator.run_deadline)  # nothing after the clock gets the run's bound

    def test_the_source_stops_a_sender_at_the_runs_bound(self):
        orchestrator = self.orchestrator()
        calls = []

        def source_agent(*args, timeout=600):
            calls.append((args, timeout))
            return {'exit_code': -15, 'wall_seconds': 1.0, 'rusage': None, 'log': 'x.log', 'log_tail': '',
                    'timed_out': True}

        orchestrator.source.agent = source_agent
        orchestrator.run_deadline = time.monotonic() + 30
        result = run_exp04.Orchestrator.remote_command(orchestrator, ['python3', '-m', 'pgdelta', 'send'], 'send.log')
        args, timeout = calls[0]
        position = args.index('--timeout')
        self.assertLessEqual(float(args[position + 1]), 30)
        self.assertGreater(float(args[position + 1]), 29)
        self.assertGreater(timeout, float(args[position + 1]))  # the SSH call outlasts the agent's own stop
        self.assertTrue(result['timed_out'])
        self.assertEqual(run_exp04.failed([result]), ['source: python3 exited -15 (timed out)'])
        # Outside an EXP-13 run's clock: no --timeout, and --command-timeout bounds the call.
        orchestrator.run_deadline = None
        calls.clear()
        run_exp04.Orchestrator.remote_command(orchestrator, ['true'], 'true.log')
        self.assertNotIn('--timeout', calls[0][0])
        self.assertEqual(calls[0][1], orchestrator.args.command_timeout)


# --- the trial, the invocation and the reset after a failure ------------------------------------------------------

class TrialFlow(WanFakes, TrialStub):
    def __init__(self, tmp, clock, extra_args=()):
        super().__init__(tmp, clock, ['--manifest-checksums', 'CRC32C', *BASE_ARGS, *extra_args])
        self.fake_network()
        self.methods = run_exp04.method_list(self.args.methods)
        self.speeds = list(self.conditions)
        self.fail_at = None

    def run_method(self, trial, repeat, speed, link, method, order_index, order):
        self.events.append(('run', speed['label'], method, repeat))
        if self.fail_at == (repeat, speed['label']):
            raise RuntimeError('a fault in the middle of a visit')
        record = {'status': 'pass', 'method': method}
        self.records.append(record)
        return record

    # The real cleanup (the network reset first), with the steps that reach the VMs' files stubbed.
    cleanup_trial = run_exp04.Orchestrator.cleanup_trial

    def fetch_source_logs(self):
        pass

    def probe_relation(self):
        return {'path': 'base/5/16397'}

    def user_table_files(self):
        self.events.append('user_table_files')
        return {'paths': ['base/5/16397']}

    def reference_digest(self, when):
        self.events.append(('reference_digest', when))
        self.reference_digests[when] = measure.reference_view(SOURCE_DIGEST)
        return {}

    def trial_probes(self):
        return {'durable_write_floor': []}

    def durable_write_floor(self):
        return {}

    def zero_delay_tests(self):
        self.events.append('zero_delay_tests')
        return {}

    def finish_logical_checks(self, trial):
        return None


class Trials(Clocked13):
    def trial(self, extra=(), fail_at=None):
        orchestrator = self.orchestrator(TrialFlow, extra)
        orchestrator.fail_at = fail_at
        with mock.patch.object(run_exp04.measure, 'tree_digest', return_value={'entries': {}, 'device': 1}):
            trial = orchestrator.run_trial(0, 'uniform', 0.01)
        return orchestrator, trial

    def test_each_repeat_visits_its_conditions_with_the_tree_off_outside(self):
        orchestrator, trial = self.trial(['--zero-delay-link-rates', '1'])
        self.assertEqual(trial['status'], 'pass', trial.get('traceback'))
        runs = [event for event in orchestrator.events if isinstance(event, tuple) and event[0] == 'run']
        self.assertEqual(Counter(event[2] for event in runs),
                         {V1: 8, TLS: 4, 'full': 4, 'parcopy8': 4, 'full_nosync': 1})
        self.assertEqual(len(trial['visits']), 8)
        self.assertEqual(sorted(trial['first_visit']), sorted(condition['label']
                                                              for condition in orchestrator.conditions))
        # The cluster and B0 with the network off; the zero-delay tests before the repeats; off again at the end.
        kinds = self.kinds(orchestrator)
        self.assertLess(kinds.index('wan'), kinds.index('take_b0'))
        self.assertLess(kinds.index('user_table_files'), kinds.index(('reference_digest')))
        self.assertLess(kinds.index('zero_delay_tests'), kinds.index('run'))
        last_run = len(kinds) - 1 - kinds[::-1].index('run')
        self.assertIn(('wan', 'off', None), orchestrator.events[last_run:])
        self.assertIsNone(orchestrator.applied)
        self.assertEqual(orchestrator.kernel_tcp, orchestrator.tcp_defaults)
        self.assertTrue(trial['reference_user_tables_match'])
        self.assertEqual(trial['reference_classes']['counts'], {'user_table': 0, 'catalog': 0, 'other': 0})

    def test_the_reset_follows_an_exception_in_a_tuned_visit(self):
        orchestrator, trial = self.trial(fail_at=(0, '2:120:tuned'))
        self.assertEqual(trial['status'], 'error')
        self.assertIn('a fault in the middle of a visit', trial['error'])
        # The cleanup took the tree off and wrote Debian's settings back on both VMs.
        self.assertIsNone(orchestrator.applied)
        self.assertEqual(orchestrator.kernel_tcp, orchestrator.tcp_defaults)
        sets = [event for event in orchestrator.events if isinstance(event, tuple) and event[0] == 'set_tcp']
        self.assertEqual(sets[-1], ('set_tcp', 'default'))
        self.assertIn(('set_tcp', 'tuned'), sets)

    def test_an_infrastructure_fault_is_recorded_as_such(self):
        orchestrator = self.orchestrator(TrialFlow)
        orchestrator.noops = [0.5] * 5
        with mock.patch.object(run_exp04.measure, 'tree_digest', return_value={'entries': {}, 'device': 1}):
            trial = orchestrator.run_trial(0, 'hot', 0.01)
        self.assertEqual(trial['status'], 'error')
        self.assertIn('A11 (i)', trial['infrastructure_fault'])
        self.assertEqual(orchestrator.kernel_tcp, orchestrator.tcp_defaults)


class Invocation(Clocked13):
    class Executing(WanFakes, T2Stub):
        def __init__(self, tmp, clock, extra_args=()):
            super().__init__(tmp, clock, extra_args)
            self.fake_network()
            self.tcp_defaults = None  # recorded by the preflight

        def _source_agent(self, *args, timeout=600):
            self.events.append(('source_agent', args))
            if args[0] == 'iface':
                return {'iface': 'ens4'}
            return {'exit_code': 0, 'log_tail': ''}

        def local_agent(self, *args, timeout=600):
            if args[:2] == ('wan', 'commands'):
                # wan.sh's own dry run, with the real subprocess.run (execute's test replaces run_exp04's).
                with mock.patch.object(agent.subprocess, 'run', REAL_RUN):
                    return agent.cmd_wan(Namespace(action='commands', rate=args[2], delay_ms=float(args[3]),
                                                   peer=args[5], control_port=int(args[7]), iface=args[9]))
            return super().local_agent(*args, timeout=timeout)

        def throttle(self, rate):
            return {}

        def environment(self):
            env = {'deployed': {'tree_sha256': 'x'}, 'strace': 'strace -- version 6.1', 'data_mount': EXT4_MOUNT,
                   'pg_basebackup_sync_method': True, 'pg_probackup': 'pg_probackup-17 2.5.16',
                   'stunnel': 'stunnel 5.68 on x86_64-pc-linux-gnu', 'openssl': 'OpenSSL 3.0.15'}
            return {'source': dict(env), 'dest': dict(env)}

        def check_pgdelta_jobs(self):
            return {}

        def check_parcopy(self):
            return {}

        def setup_tls(self):
            self.events.append('setup_tls')
            return {'dir': self.tls_dir}

        def run_trial(self, index, pattern, fraction):
            self.events.append(('trial', pattern))
            self.set_tcp('tuned')
            self.wan('apply', self.conditions[1])
            return {'trial_id': f'{index:02d}-{pattern}', 'status': 'pass'}

    class Interrupted(Executing):
        def run_trial(self, index, pattern, fraction):
            super().run_trial(index, pattern, fraction)  # the tree on and the tuned settings, as a visit leaves them
            raise KeyboardInterrupt  # remote.py stop's SIGTERM, which run_trial and run_method don't catch

    def execute(self, extra=(), cls=None):
        args = ['--patterns', 'hot', '--gnu-time', 'none', '--disk-test-mib', '0', '--idle-seconds', '0',
                '--phase-floor-repeats', '0', *BASE_ARGS, *extra]
        orchestrator = self.last = self.orchestrator(cls or self.Executing, args)
        route = mock.Mock(stdout='10.0.0.12 dev ens4 src 10.0.0.13 uid 1000\n', returncode=0)
        with mock.patch.object(run_exp04.subprocess, 'run', return_value=route), \
                mock.patch.object(orchestrator.source, 'open'), mock.patch.object(orchestrator.source, 'close'):
            code = orchestrator.execute()
        return orchestrator, code, json.loads((orchestrator.out / 'run.json').read_text())

    def test_run_json_holds_the_conditions_the_control_path_and_the_teardown(self):
        orchestrator, code, run = self.execute(['--dirty-noise-table', '5:4,30:6,120:9'])
        self.assertEqual(code, 0)
        self.assertEqual([condition['label'] for condition in run['speeds']],
                         ['2:0:default', '2:60:default', '2:120:tuned', '2:0:tuned'])
        self.assertEqual(run['control']['port'], 2222)
        self.assertIn('Port=2222', run['control']['ssh_options'])
        self.assertEqual(run['methods_by_speed']['2:0:default']['1'], [V1, TLS, 'full_nosync'])
        self.assertEqual(run['methods_by_speed']['2:0:default']['2'], ['full', 'parcopy8', V1, TLS])
        preflight = run['t12_preflight']
        self.assertEqual(preflight['tcp_defaults']['dest']['net.ipv4.tcp_rmem'], '4096\t131072\t6291456')
        self.assertIn('tc qdisc add dev ens4 parent 1:2 handle 20: netem delay 60ms limit 1000000',
                      preflight['tc_commands']['2:120:tuned'])
        self.assertEqual(preflight['control_reference'], {'condition': '2:0:default', 'seconds': [0.011] * 5})
        self.assertFalse(preflight['busy_inflight_flag'])
        self.assertIn('setup_tls', orchestrator.events)
        self.assertIn('client = yes', preflight['stunnel_configs']['client'])
        self.assertEqual(run['t12']['plan']['negative_control']['method'], 'full_nosync')
        self.assertIn('full_nosync', run['method_flags'])
        self.assertEqual(run['noise_table_bytes'], {'5': 4 << 20, '30': 6 << 20, '120': 9 << 20})
        # The trial left the tree on and tuned settings: the invocation's finally took both back.
        self.assertIsNone(orchestrator.applied)
        self.assertEqual(orchestrator.kernel_tcp, orchestrator.tcp_defaults)
        self.assertFalse(run['teardown']['source']['tree_is_wan'])
        self.assertEqual(run['teardown']['dest']['qdiscs'][0]['kind'], 'mq')
        self.assertTrue(run['teardown']['tcp']['ok'])

    def test_an_interrupted_invocation_writes_its_teardown(self):
        # The teardown runs in execute's finally; the interrupt then goes past the save after it, so run.json kept
        # the last per-trial save, with no teardown, and the analysis couldn't check it.
        with self.assertRaises(KeyboardInterrupt):
            self.execute(cls=self.Interrupted)
        orchestrator = self.last
        run = json.loads((orchestrator.out / 'run.json').read_text())
        self.assertIsNone(orchestrator.applied)
        self.assertEqual(orchestrator.kernel_tcp, orchestrator.tcp_defaults)
        self.assertFalse(run['teardown']['source']['tree_is_wan'])
        self.assertFalse(run['teardown']['dest']['tree_is_wan'])
        self.assertTrue(run['teardown']['tcp']['ok'])
        self.assertTrue(measure.teardown_ok(run['teardown']))

    def test_it_refuses_vms_that_do_not_have_debians_tcp_settings(self):
        # An interrupted run can leave the tuned maxima: taken as the defaults, every default visit would run tuned.
        with self.assertRaises(SystemExit) as caught:
            with mock.patch.object(self.Executing, 'fake_network', lambda stub: (
                    WanFakes.fake_network(stub),
                    stub.kernel_tcp['source'].update({'net.ipv4.tcp_rmem': '4096\t131072\t134217728'}))):
                self.execute()
        message = str(caught.exception)
        self.assertIn("source: net.ipv4.tcp_rmem is '4096\\t131072\\t134217728', not Debian's '4096 131072 6291456' "
                      '(a tuned maximum', message)
        self.assertNotIn('dest:', message)
        self.assertIn("sudo sysctl -w net.ipv4.tcp_rmem='4096 131072 6291456'", message)
        self.assertEqual(measure.tcp_defaults_problems(DEBIAN_TCP), [])
        self.assertEqual(len(measure.tcp_defaults_problems({'net.ipv4.tcp_rmem': '4096 87380 6291456',
                                                            'net.ipv4.tcp_wmem': '4096\t16384\t4194304'})), 1)

    def test_the_preflight_stops_what_an_interrupted_run_left(self):
        orchestrator, code, run = self.execute()
        self.assertEqual(code, 0)
        self.assertIn(('source_agent', ('stale-processes', '--kill')), orchestrator.events)
        self.assertIn(('local_agent', ('stale-processes', '--kill')), orchestrator.events)
        self.assertEqual(run['t12_preflight']['stale_processes'], {'source': [], 'dest': []})
        self.assertTrue(run['t12_preflight']['tcp_defaults_check']['ok'])

    def test_a_network_left_unreset_fails_the_invocation(self):
        # The trials passed, but a VM kept the tree or the tuned settings: the next invocation would start from them.
        def network_off(stub):
            if ('trial', 'hot') in stub.events:  # the teardown's, after the trials (the preflight's works)
                raise run_exp04.CommandError('ssh died')
            return run_exp04.Orchestrator.network_off(stub)

        with mock.patch.object(self.Executing, 'network_off', network_off):
            _, code, run = self.execute()
        self.assertEqual(code, 1)
        self.assertEqual(run['status'], 'fail')
        self.assertIn('error', run['teardown'])
        self.assertFalse(measure.teardown_ok({'source': {'tree_is_wan': False}, 'dest': {'tree_is_wan': False},
                                              'tcp': {'ok': False}}))
        self.assertFalse(measure.teardown_ok({'source': {'tree_is_wan': True}, 'dest': {'tree_is_wan': False},
                                              'tcp': {'ok': True}}))
        self.assertTrue(measure.teardown_ok({'source': {'tree_is_wan': False}, 'dest': {'tree_is_wan': False},
                                             'tcp': {'ok': True}}))

    def test_the_idle_noise_is_measured_by_window_length(self):
        orchestrator = self.orchestrator()
        dirty = iter(range(0, 10000, 100))
        orchestrator.snapshot_dest = lambda: {'meminfo': {'Dirty': next(dirty), 'Writeback': 0}}
        with mock.patch.object(run_exp04.time, 'sleep') as sleep:
            noise = orchestrator.dirty_idle_noise_plan(measure.parse_noise_plan('5:2,30:1'))
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5.0, 5.0, 30.0])
        self.assertEqual(noise['plan'], [[5.0, 2], [30.0, 1]])
        self.assertEqual(noise['increases_bytes'], {'5': [102400, 102400], '30': [102400]})
        self.assertEqual(noise['max_increase_bytes'], {'5': 102400, '30': 102400})
        with redirect_stderr(), self.assertRaises(SystemExit):
            parse(*BASE_ARGS, '--idle-noise-plan', '5:20', '--idle-noise-windows', '20')

    def test_a_run_without_conditions_is_as_before(self):
        orchestrator = T2Stub(tempfile.mkdtemp(), mock.Mock(return_value=1.0), ['--methods', 'full'])
        self.addCleanup(orchestrator.pool.shutdown)
        self.assertFalse(orchestrator.t12)
        self.assertNotIn('Port=2222', orchestrator.source.options)
        self.assertNotIn('--busy-inflight-bytes', orchestrator.pgdelta_send_argv(V1, orchestrator.method_paths(V1)))
        self.assertEqual(orchestrator.wal_ticker_argv()[0].rsplit('/', 1)[-1], 'psql')


class RemoteControlPort(unittest.TestCase):
    def test_setup_ssh_checks_the_control_port_when_asked(self):
        vms = {'source': {'external': '203.0.113.1', 'internal': '10.0.0.12'},
               'dest': {'external': '203.0.113.2', 'internal': '10.0.0.13'}}
        calls = []

        def ssh(config, address, command, **kwargs):
            calls.append(command)
            return mock.Mock(stdout=b'ssh-ed25519 AAAA key\n' if 'cat' in command else b'pgd-source\n',
                             returncode=0, stderr=b'')

        config = {'SSH_USER': 'bench'}
        with mock.patch.object(remote, 'instances', return_value=vms), mock.patch.object(remote, 'ssh', ssh), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(remote.cmd_setup_ssh(config, Namespace(control_port=2222)), 0)
        self.assertEqual(calls[-1], remote.control_probe_command('bench', '10.0.0.12', 2222))
        self.assertIn(' -p 2222 ', calls[-1])
        self.assertEqual(remote.build_parser().parse_args(['setup-ssh', '--control-port', '2222']).control_port, 2222)
        source = run_exp04.Source('10.0.0.12', 'bench', '/k', '/repo', port=2222)
        self.assertEqual(source.argv(['true'])[-1], 'true')
        self.assertIn('Port=2222', source.argv(['true']))

    def test_startup_has_sshd_listen_on_the_control_port_and_installs_stunnel(self):
        text = (Path(__file__).resolve().parents[1] / 'infra' / 'gcp' / 'startup.sh').read_text()
        self.assertIn('CONTROL_PORT=2222', text)
        self.assertIn("Port 22\\nPort %s\\n", text)
        self.assertIn('sshd -t', text)
        self.assertIn('install --no-install-recommends stunnel4 openssl iputils-ping', text)


class Csv(unittest.TestCase):
    def test_the_csv_writes_every_t12_column(self):
        for field in ('rate_gbit', 'rtt_ms', 'host_tcp', 'ping_min_ms', 'visit_one_stream_gbit', 'zero_delay_gbit',
                      'netem_own_drops', 'netem_backlog_max', 'retrans_segs_source', 'minrtt_ms', 'rwnd_limited_ms',
                      'notsent', 'unacked', 'connect_s', 'leaf_wait_busy_s', 'leaf_wait_idle_s', 'prefetch_bytes',
                      'prefetch_s', 'tls_version', 'tls_suite', 'excluded_reason', 'inferred', 'window_limited_bound',
                      'noise_window_s', 'dirty_limit_bytes'):
            self.assertIn(field, measure.CSV_FIELDS)
        self.assertEqual(len(set(measure.CSV_FIELDS)), len(measure.CSV_FIELDS))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'summary.csv'
            measure.write_csv(path, [{'method': 'full', 'speed': {'label': 'unthrottled'}}])
            with open(path) as stream:
                row = next(csv.DictReader(stream))
        self.assertEqual(row['rate_gbit'], '')  # outside EXP-13 its columns stay empty


if __name__ == '__main__':
    unittest.main()


class BaseRttTests(unittest.TestCase):
    """EXP-15 on a real path: the checks expect the emulated r plus the path's own round trip."""

    def test_ping_window_moves_with_the_base(self):
        pings = {'count': 20, 'replies': 20, 'min_ms': 31.3, 'median_ms': 31.5}
        self.assertTrue(measure.ping_problems(pings, 0.0))
        self.assertEqual(measure.ping_problems(pings, 0.0, 31.0), [])
        self.assertTrue(measure.ping_problems(pings, 0.0, 33.0))

    def test_minrtt_check_reads_the_base_from_the_record(self):
        record = {'condition': {'rtt_ms': 0.0, 'base_rtt_ms': 31.0}, 'ss': {'minrtt_ms_min': 31.3, 'minrtt_ms_max': 32.0}}
        out = measure.network_checks(record)
        self.assertFalse(any('minrtt' in p for p in out.get('problems', [])), out)
        record['condition']['base_rtt_ms'] = 0.0
        self.assertTrue(any('minrtt' in p for p in measure.network_checks(record).get('problems', [])))
