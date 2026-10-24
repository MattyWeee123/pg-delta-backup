"""Tests for EXP-13's emulated WAN (T12): infra/gcp/wan.sh's tc lines, and the parsers and checks that read the tree.

No root, tc or Linux is needed: wan.sh runs with DRY_RUN=1, which prints its tc commands, and the parsers read fixed
samples of `tc -s -j qdisc show` and `tc filter show`.

The samples are built from iproute2 6.1's print functions (tc/tc_qdisc.c, q_prio.c, q_netem.c, q_tbf.c and
f_u32.c) and kernel 6.1's drop accounting (net/sched/sch_netem.c: a packet its tbf child refuses counts as netem's
own drop too, and as the prio root's). They are NOT captures: smoke items 1 and 5 capture `wan.sh show --json` and
`tc filter show` on the Debian 12 VMs (an 8-stream tuned link test at 120 ms for the drops), and these samples are
replaced by those captures before the sweep (EXP-13's Tests row).
"""

import json
import os
from pathlib import Path
import subprocess
import unittest
from argparse import Namespace
from unittest import mock

from experiments.network import agent, measure

WAN = Path(__file__).resolve().parents[1] / 'infra' / 'gcp' / 'wan.sh'
PEER, PORT = '10.0.0.12', 2222

# `tc -s -j qdisc show dev ens4` with wan.sh's tree at 2 Gbit/s and r = 120 ms (60 ms on this VM), after drops at the
# shaper: netem's drop counter (1234) holds its tbf child's 1200 and 34 of its own; the root counts them too. netem's
# delay is in seconds and the tbf's lat in microseconds (tc's JSON); the prio's default pfifo children are invisible.
# NOT a capture (see the module's docstring).
TREE_JSON = json.dumps([
    {'kind': 'prio', 'handle': '1:', 'root': True, 'refcnt': 9,
     'options': {'bands': 2, 'priomap': [0] * 16, 'multiqueue': False},
     'bytes': 31734902386, 'packets': 20959342, 'drops': 1234, 'overlimits': 0, 'requeues': 19, 'backlog': 0,
     'qlen': 0},
    {'kind': 'netem', 'handle': '20:', 'parent': '1:2',
     'options': {'limit': 1000000, 'delay': {'delay': 0.06, 'jitter': 0, 'correlation': 0}, 'ecn': False, 'gap': 0},
     'bytes': 31734512210, 'packets': 20958710, 'drops': 1234, 'overlimits': 0, 'requeues': 0, 'backlog': 27262976,
     'qlen': 18432},
    {'kind': 'tbf', 'handle': '30:', 'parent': '20:1', 'options': {'rate': 250000000, 'burst': 2500000, 'lat': 120000},
     'bytes': 31704011634, 'packets': 20938605, 'drops': 1200, 'overlimits': 4561238, 'requeues': 0,
     'backlog': 15138816, 'qlen': 10011}])
# `tc filter show dev ens4` with wan.sh's three u32 rules (10.0.0.12 is 0a00000c, port 2222 is 08ae). NOT a capture.
FILTERS = """\
filter parent 1: protocol ip pref 1 u32 chain 0
filter parent 1: protocol ip pref 1 u32 chain 0 fh 800: ht divisor 1
filter parent 1: protocol ip pref 1 u32 chain 0 fh 800::800 order 2048 key ht 800 bkt 0 flowid 1:1 not_in_hw
  match 0a00000c/ffffffff at 16
  match 00060000/00ff0000 at 8
  match 08ae0000/ffff0000 at 20
filter parent 1: protocol ip pref 1 u32 chain 0 fh 800::801 order 2049 key ht 800 bkt 0 flowid 1:1 not_in_hw
  match 0a00000c/ffffffff at 16
  match 00060000/00ff0000 at 8
  match 000008ae/0000ffff at 20
filter parent 1: protocol ip pref 2 u32 chain 0
filter parent 1: protocol ip pref 2 u32 chain 0 fh 801: ht divisor 1
filter parent 1: protocol ip pref 2 u32 chain 0 fh 801::800 order 2048 key ht 801 bkt 0 flowid 1:2 not_in_hw
  match 0a00000c/ffffffff at 16
"""
# After `wan.sh off`: Debian's default, mq with an fq_codel per queue (two of eight shown). NOT a capture.
DEFAULT_JSON = json.dumps([
    {'kind': 'mq', 'handle': '0:', 'root': True, 'options': {}, 'bytes': 1000, 'packets': 10, 'drops': 0,
     'overlimits': 0, 'requeues': 0, 'backlog': 0, 'qlen': 0},
    {'kind': 'fq_codel', 'handle': '0:', 'parent': ':8', 'options': {'limit': 10240, 'flows': 1024},
     'bytes': 500, 'packets': 5, 'drops': 0, 'overlimits': 0, 'requeues': 0, 'backlog': 0, 'qlen': 0},
    {'kind': 'fq_codel', 'handle': '0:', 'parent': ':7', 'options': {'limit': 10240, 'flows': 1024},
     'bytes': 500, 'packets': 5, 'drops': 0, 'overlimits': 0, 'requeues': 0, 'backlog': 0, 'qlen': 0}])


def tree_json(condition: dict, *, drops=(0, 0), qlen=0) -> str:
    """TREE_JSON's shape as wan.sh would leave it at a condition (netem's drops, tbf's drops), for other tests."""
    rate = condition['gbit'] * 1e9 / 8
    netem_options = {'limit': 1000000, 'ecn': False, 'gap': 0}
    if condition['delay_ms']:
        netem_options['delay'] = {'delay': condition['delay_ms'] / 1000, 'jitter': 0, 'correlation': 0}
    return json.dumps([
        {'kind': 'prio', 'handle': '1:', 'root': True, 'options': {'bands': 2, 'priomap': [0] * 16},
         'bytes': 2000, 'packets': 20, 'drops': drops[0], 'overlimits': 0, 'requeues': 0, 'backlog': 0, 'qlen': 0},
        {'kind': 'netem', 'handle': '20:', 'parent': '1:2', 'options': netem_options, 'bytes': 1500,
         'packets': 15, 'drops': drops[0], 'overlimits': 0, 'requeues': 0, 'backlog': 0, 'qlen': qlen},
        {'kind': 'tbf', 'handle': '30:', 'parent': '20:1',
         'options': {'rate': int(rate), 'burst': max(131072, int(rate / 100)),
                     'lat': int(max(50, condition['rtt_ms']) * 1000)},
         'bytes': 1500, 'packets': 15, 'drops': drops[1], 'overlimits': 0, 'requeues': 0, 'backlog': 0, 'qlen': 0}])


def filters_text(peer: str = PEER, port: int = PORT) -> str:
    hexed = ''.join(f'{int(part):02x}' for part in peer.split('.'))
    return FILTERS.replace('0a00000c', hexed).replace('08ae', f'{port:04x}')


class WanScript(unittest.TestCase):
    """wan.sh's tc lines, run with DRY_RUN=1."""

    def dry(self, *args):
        return subprocess.run(['bash', str(WAN), *args], capture_output=True, text=True,
                              env={**os.environ, 'DRY_RUN': '1'}, timeout=60)

    def test_the_tree_at_60_ms(self):
        result = self.dry('2000mbit', '30', PEER, str(PORT), 'ens4')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            'tc qdisc del dev ens4 root',
            'tc qdisc add dev ens4 root handle 1: prio bands 2 priomap 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0',
            'tc qdisc add dev ens4 parent 1:2 handle 20: netem delay 30ms limit 1000000',
            'tc qdisc add dev ens4 parent 20:1 handle 30: tbf rate 2000mbit burst 2500000 latency 60ms',
            'tc filter add dev ens4 parent 1: protocol ip prio 1 u32 match ip dst 10.0.0.12/32 match ip protocol 6 '
            '0xff match ip sport 2222 0xffff flowid 1:1',
            'tc filter add dev ens4 parent 1: protocol ip prio 1 u32 match ip dst 10.0.0.12/32 match ip protocol 6 '
            '0xff match ip dport 2222 0xffff flowid 1:1',
            'tc filter add dev ens4 parent 1: protocol ip prio 2 u32 match ip dst 10.0.0.12/32 flowid 1:2'])

    def test_the_queue_is_max_of_50_ms_and_r_and_the_burst_has_a_floor(self):
        cases = {('2000mbit', '60'): 'latency 120ms', ('2000mbit', '10'): 'latency 50ms',
                 ('2000mbit', '0'): 'latency 50ms', ('1000mbit', '30'): 'latency 60ms'}
        for (rate, delay), queue in cases.items():
            with self.subTest(rate=rate, delay=delay):
                lines = self.dry(rate, delay, PEER, str(PORT), 'ens4').stdout.splitlines()
                tbf = next(line for line in lines if ' tbf ' in line)
                self.assertTrue(tbf.endswith(queue), tbf)
                netem = next(line for line in lines if ' netem ' in line)
                self.assertIn(f'netem delay {delay}ms limit 1000000', netem)
        # 10 ms of tokens at 1 Gbit/s; at 50 Mbit/s that is under 128 KiB, so 128 KiB.
        self.assertIn('burst 1250000 ', self.dry('1gbit', '0', PEER, str(PORT), 'ens4').stdout)
        self.assertIn('burst 131072 ', self.dry('50mbit', '0', PEER, str(PORT), 'ens4').stdout)

    def test_off_and_refusals(self):
        self.assertEqual(self.dry('off', 'ens4').stdout.splitlines(), ['tc qdisc del dev ens4 root'])
        for args, message in ((('2000mbit', 'x', PEER, '2222', 'ens4'), 'bad one-way delay'),
                              (('2000mbit', '30', 'peer', '2222', 'ens4'), 'bad peer address'),
                              (('2000mbit', '30', PEER, '70000', 'ens4'), 'bad control port'),
                              (('2000xbit', '30', PEER, '2222', 'ens4'), 'bad rate unit'),
                              (('2000mbit', '30', PEER), 'usage')):
            with self.subTest(message=message):
                result = self.dry(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_the_agent_records_the_same_lines(self):
        result = agent.cmd_wan(Namespace(action='commands', rate='2000mbit', delay_ms=30.0, peer=PEER,
                                         control_port=PORT, iface='ens4'))
        self.assertEqual(result['commands'], self.dry('2000mbit', '30', PEER, str(PORT), 'ens4').stdout.splitlines())
        with self.assertRaises(SystemExit):
            agent.cmd_wan(Namespace(action='apply', rate=None, delay_ms=None, peer=None, control_port=None,
                                    iface='ens4'))

    def test_the_agent_applies_it_with_sudo_and_shows_the_tree(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout='', stderr='')

        def text_of(argv, **kwargs):
            return TREE_JSON if '-j' in argv else FILTERS

        with mock.patch.object(agent.subprocess, 'run', run), mock.patch.object(agent, 'text_of', text_of):
            result = agent.cmd_wan(Namespace(action='apply', rate='2000mbit', delay_ms=60.0, peer=PEER,
                                             control_port=PORT, iface='ens4'))
        self.assertEqual(calls, [['sudo', '-n', 'bash', str(WAN), '2000mbit', '60', PEER, '2222', 'ens4']])
        self.assertTrue(result['tree']['is_wan'])
        self.assertEqual(len(result['filters']), 3)


class TreeParser(unittest.TestCase):
    def test_netems_own_drops_are_its_counter_less_its_childs(self):
        tree = measure.wan_tree(measure.parse_qdisc_json(TREE_JSON))
        self.assertTrue(tree['is_wan'])
        # tbf's 1200 drops show in netem's counter: netem itself dropped 34.
        self.assertEqual((tree['netem']['drops'], tree['tbf']['drops'], tree['netem_own_drops']), (1234, 1200, 34))
        self.assertEqual(tree['root']['drops'], 1234)
        self.assertAlmostEqual(tree['netem']['delay_ms'], 60.0)
        self.assertEqual(tree['netem']['limit'], 1000000)
        self.assertEqual(tree['tbf']['rate_bytes'], 250000000)
        self.assertEqual(tree['tbf']['latency_ms'], 120.0)
        self.assertEqual(tree['netem']['qlen'], 18432)
        # The plain band's pfifo doesn't show: its counters are the root's less netem's.
        self.assertEqual(tree['plain'], {'kind': None, 'derived': True, 'bytes': 390176, 'packets': 632})

    def test_a_visible_plain_band_and_text_units(self):
        data = json.loads(TREE_JSON)
        data.insert(1, {'kind': 'pfifo', 'handle': '8001:', 'parent': '1:1', 'options': {'limit': 1000},
                        'bytes': 77, 'packets': 7, 'drops': 0, 'overlimits': 0, 'requeues': 0, 'backlog': 0, 'qlen': 0})
        data[2]['options']['delay'] = {'delay': '60.000ms'}
        data[3]['options'].update(rate='2Gbit', burst='2500000b', lat='120ms')
        tree = measure.wan_tree(measure.parse_qdisc_json(data))
        self.assertEqual(tree['plain'], {'kind': 'pfifo', 'derived': False, 'bytes': 77, 'packets': 7, 'drops': 0,
                                         'overlimits': 0, 'requeues': 0, 'backlog': 0, 'qlen': 0})
        self.assertEqual((tree['netem']['delay_ms'], tree['tbf']['rate_bytes'], tree['tbf']['burst_bytes'],
                          tree['tbf']['latency_ms']), (60.0, 250000000.0, 2500000.0, 120.0))

    def test_the_default_qdisc_is_not_the_tree(self):
        tree = measure.wan_tree(measure.parse_qdisc_json(DEFAULT_JSON))
        self.assertFalse(tree['is_wan'])
        self.assertIsNone(tree['netem'])
        self.assertEqual(tree['root']['kind'], 'mq')
        expected = measure.wan_expected(measure.parse_condition('2:0:default'), PEER, PORT)
        self.assertIn("not wan.sh's tree", measure.wan_problems(tree, [], expected)[0])

    def test_the_visits_tree_check(self):
        condition = measure.parse_condition('2:120:tuned')
        expected = measure.wan_expected(condition, PEER, PORT)
        self.assertEqual((expected['delay_ms'], expected['queue_ms'], expected['burst_bytes']), (60.0, 120.0, 2500000))
        tree = measure.wan_tree(measure.parse_qdisc_json(TREE_JSON))
        filters = measure.parse_u32_filters(FILTERS)
        self.assertEqual(measure.wan_problems(tree, filters, expected), [])
        # Another condition's tree fails on every value that differs.
        problems = measure.wan_problems(tree, filters, measure.wan_expected(measure.parse_condition('1:60:default'),
                                                                            PEER, PORT))
        self.assertEqual(len(problems), 4, problems)
        self.assertTrue(any("netem's delay" in problem for problem in problems))
        self.assertTrue(any("tbf's rate" in problem for problem in problems))
        # The anchor: netem shows no delay at all.
        anchor = measure.parse_condition('2:0:default')
        tree = measure.wan_tree(measure.parse_qdisc_json(tree_json(anchor)))
        self.assertEqual(tree['netem']['delay_ms'], 0.0)
        self.assertEqual(measure.wan_problems(tree, filters, measure.wan_expected(anchor, PEER, PORT)), [])
        wrong_limit = json.loads(tree_json(anchor))
        wrong_limit[1]['options']['limit'] = 1000
        self.assertIn("netem's limit is 1000", measure.wan_problems(
            measure.wan_tree(measure.parse_qdisc_json(wrong_limit)), filters,
            measure.wan_expected(anchor, PEER, PORT))[0])


class Filters(unittest.TestCase):
    def test_the_three_rules(self):
        rules = measure.parse_u32_filters(FILTERS)
        self.assertEqual([(rule['pref'], rule['flowid'], rule['dst'], rule['protocol'], rule['sport'], rule['dport'])
                          for rule in rules],
                         [(1, '1:1', PEER, 6, PORT, None), (1, '1:1', PEER, 6, None, PORT), (2, '1:2', PEER, None, None,
                                                                                             None)])
        self.assertEqual(measure.wan_filter_problems(rules, PEER, PORT), [])

    def test_what_the_check_catches(self):
        rules = measure.parse_u32_filters(FILTERS)
        # The control port's exception for this VM's role missing (only sport left).
        problems = measure.wan_filter_problems([rules[0], rules[2]], PEER, PORT)
        self.assertEqual(problems, [f'no filter sends TCP dport {PORT} to {PEER} to the plain band'])
        # The delayed band's filter first: the control path would be delayed.
        early = [dict(rule, pref=3) if rule['flowid'] == '1:1' else rule for rule in rules]
        self.assertIn("the delayed band's filter comes before the control port's",
                      measure.wan_filter_problems(early, PEER, PORT))
        # Another peer or port, or a filter more.
        self.assertTrue(measure.wan_filter_problems(rules, '10.0.0.99', PORT))
        self.assertTrue(measure.wan_filter_problems(rules, PEER, 22))
        extra = rules + [{'pref': 3, 'flowid': '1:2', 'dst': '10.0.0.7', 'protocol': None, 'sport': None,
                          'dport': None, 'other': []}]
        self.assertTrue(any('unexpected filters' in problem for problem in measure.wan_filter_problems(extra, PEER,
                                                                                                         PORT)))
        self.assertEqual(measure.parse_u32_filters(filters_text('10.128.0.13', 2222))[2]['dst'], '10.128.0.13')


class Counters(unittest.TestCase):
    def test_a_windows_own_drops_and_a_replaced_tree(self):
        before = measure.qdisc_counters(measure.wan_tree(measure.parse_qdisc_json(TREE_JSON)))
        data = json.loads(TREE_JSON)
        data[1]['drops'] += 500  # 500 refused by the tbf, which netem counts too
        data[2]['drops'] += 500
        data[1]['bytes'] += 10 ** 9
        after = measure.qdisc_counters(measure.wan_tree(measure.parse_qdisc_json(data)))
        delta = measure.qdisc_delta(before, after)
        self.assertTrue(delta['same_tree'])
        self.assertEqual((delta['netem']['drops'], delta['tbf']['drops'], delta['netem_own_drops']), (500, 500, 0))
        self.assertEqual(delta['netem']['bytes'], 10 ** 9)
        data[1]['drops'] += 3  # netem's own (its limit)
        own = measure.qdisc_delta(before, measure.qdisc_counters(measure.wan_tree(measure.parse_qdisc_json(data))))
        self.assertEqual(own['netem_own_drops'], 3)
        # A tree replaced in between: its counters started again, so nothing is read from it.
        replaced = measure.qdisc_counters(measure.wan_tree(measure.parse_qdisc_json(tree_json(
            measure.parse_condition('2:120:tuned')))))
        gone = measure.qdisc_delta(before, replaced)
        self.assertIsNone(gone['netem']['bytes'])
        self.assertIsNone(gone['netem_own_drops'])
        off = measure.qdisc_counters(measure.wan_tree(measure.parse_qdisc_json(DEFAULT_JSON)))
        self.assertIsNone(measure.qdisc_delta(before, off)['netem'])

    def test_the_samplers_largest_backlog_against_netems_limit(self):
        samples = [{**measure.qdisc_counters(measure.wan_tree(measure.parse_qdisc_json(
            tree_json(measure.parse_condition('2:120:tuned'), qlen=qlen)))), 't': index}
            for index, qlen in enumerate((0, 18432, 50))]
        summary = measure.qdisc_samples_summary(samples)
        self.assertEqual((summary['samples'], summary['netem_qlen_max'], summary['at_limit']), (3, 18432, False))
        samples.append({**samples[0], 'netem': {**samples[0]['netem'], 'qlen': 1000000}})
        self.assertTrue(measure.qdisc_samples_summary(samples)['at_limit'])
        self.assertIsNone(measure.qdisc_samples_summary([{'t': 0, 'netem': None}])['at_limit'])


if __name__ == '__main__':
    unittest.main()
