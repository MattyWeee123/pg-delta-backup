"""EXP-94's run lines, exactly as registered, parse and plan the runs the registration claims.

The point of this file is that the pre-registration's "180 runs" is checked rather than asserted. EXP-13 set
the pattern (tests/test_exp13_core.py's RunLines): the invocation lives in the write-up, and a test reads it
from there, so the document and what would actually run cannot drift apart. A session that costs VM time should
not discover a mis-scoped `--methods` after the pair is up.
"""
from __future__ import annotations

import re
import shlex
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'experiments' / 'network'))

from experiments.network import measure, remote, run_exp04  # noqa: E402

SPEC = ROOT / 'docs' / 'experiments' / 'EXP-94-dr-hash-ahead.md'
HA, BBR_HA = 'pgdelta_direct_inplace_ha', 'pgdelta_direct_inplace_bbr_ha'
V1, BBR = 'pgdelta_direct_inplace', 'pgdelta_direct_inplace_bbr'
ARMS = ('full', 'parcopy8', 'parcopy16', V1, BBR, HA, BBR_HA)
# EXP-20's and EXP-24's own set, so every ratio is comparable and no cell was selected (see the registration's
# Design: an earlier draft selected four cells by EXP-24's cubic-to-BBR gap, and the reason did not hold).
CONDITIONS = ('2:0:default', '2:20:default', '2:60:default', '2:120:default', '2:60:tuned', '2:120:tuned')
# parcopy16 is B_fast at 120 ms on default hosts only, where 8 default windows may not fill the link.
AT_EVERY_CONDITION = ('full', 'parcopy8', V1, BBR, HA, BBR_HA)
PARCOPY16_CONDITION = '2:120:default'


def spec_run_lines() -> dict:
    """{invocation name: run_exp04's arguments} from the registration's run lines, expanded and split as a
    POSIX shell would, then as remote.py start hands them to run_exp04.py."""
    block = re.search(r'\*\*Run lines\*\*.*?```sh\n(.*?)```', SPEC.read_text(encoding='utf-8'), re.S).group(1)
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


def parse(argv):
    return run_exp04.parse_args(['--source-ip', '10.0.0.12', '--out', '/data/exp04-results/exp94', *argv])


def condition_labels(args):
    """parse_conditions returns a dict per condition; the label is the RATE:RTT_MS:HOST text."""
    return tuple(condition['label'] for condition in measure.parse_conditions(args.conditions))


def schedule_of(args):
    return measure.t12_schedule(run_exp04.method_list(args.methods),
                                measure.parse_conditions(args.conditions), args.repeats,
                                run_exp04.t12_plan(args))


class RunLines(unittest.TestCase):
    def setUp(self):
        self.lines = spec_run_lines()

    def test_the_block_holds_the_smoke_and_the_scored_session(self):
        self.assertEqual(sorted(self.lines), ['exp94', 'exp94-smoke'])

    def test_the_scored_session_plans_111_runs_a_workload_and_222_in_all(self):
        args = parse(self.lines['exp94'])
        self.assertEqual(args.repeats, 3)
        # Both workloads in one invocation, so one summary.csv holds the session (as EXP-20's did) and
        # analysis/exp20.py reads it with its own --sweep.
        self.assertEqual(args.patterns, 'hot,uniform')
        self.assertEqual(condition_labels(args), CONDITIONS)
        runs = Counter(method for _, _, method in schedule_of(args))
        # t12_schedule plans one workload; the orchestrator runs it once per pattern (its trials).
        expected = {arm: len(CONDITIONS) * 3 for arm in AT_EVERY_CONDITION}
        expected['parcopy16'] = 3  # one condition, three repeats
        self.assertEqual(dict(runs), expected)
        self.assertEqual(sum(runs.values()), 111)
        workloads = len(args.patterns.split(','))
        self.assertEqual(sum(runs.values()) * workloads, 222)

    def test_parcopy16_runs_only_where_exp20_ran_it(self):
        args = parse(self.lines['exp94'])
        where = {condition for _, condition, method in schedule_of(args) if method == 'parcopy16'}
        self.assertEqual(where, {PARCOPY16_CONDITION})
        self.assertEqual(run_exp04.t12_plan(args)['method_conditions'],
                         {'parcopy16': [PARCOPY16_CONDITION]})
        # and the first visit there tests the link over 16 streams, as EXP-13 registered
        self.assertEqual(run_exp04.t12_plan(args)['first_visit_streams'], {PARCOPY16_CONDITION: [16]})

    def test_the_conditions_are_exactly_what_exp20_and_exp24_ran(self):
        """No cell was selected: every condition those sessions ran, so no ratio is cherry-picked."""
        import csv
        data = ROOT / 'docs' / 'experiments' / 'data'
        theirs = set()
        for name in ('exp20-cells.csv', 'exp20-cells-exp24-inplace.csv', 'exp20-cells-exp24-bbr.csv'):
            with open(data / name, newline='', encoding='utf-8') as stream:
                theirs.update(row['condition'] for row in csv.DictReader(stream))
        # the only cell of theirs this session drops is the tuned anchor, which EXP-20 ran for v1 and full only
        self.assertEqual(theirs - set(CONDITIONS), {'2:0:tuned'})
        self.assertEqual(set(CONDITIONS) - theirs, set())

    def test_the_two_new_arms_are_in_the_plan_beside_both_controls(self):
        args = parse(self.lines['exp94'])
        methods = run_exp04.method_list(args.methods)
        for arm in (V1, BBR, HA, BBR_HA):
            self.assertIn(arm, methods)
        # the two denominators EXP-20's verdict rows need, and nothing else
        self.assertEqual(set(methods), set(ARMS))

    def test_the_smoke_is_short_and_covers_both_new_arms_before_the_session_spends_time(self):
        args = parse(self.lines['exp94-smoke'])
        self.assertEqual((args.scale, args.repeats, args.patterns), (5, 1, 'hot'))
        methods = run_exp04.method_list(args.methods)
        for arm in (HA, BBR_HA):
            self.assertIn(arm, methods)
        # parcopy16 is deliberately out of the smoke: it runs only at 2:120:default, which the smoke does not
        # visit, and the harness refuses a --method-conditions naming a condition outside --conditions.
        self.assertNotIn('parcopy16', methods)
        # one cell with no delay and one tuned cell at 60 ms: enough to read the emulation and the flags
        self.assertEqual(condition_labels(args), ('2:0:default', '2:60:tuned'))
        self.assertEqual(len(schedule_of(args)), len(AT_EVERY_CONDITION) * 2)

    def test_crc32c_manifests_are_asked_for_so_the_t12_arms_are_satisfied(self):
        """T12 arms need CRC32C manifests; checksum_problem is what refuses a run without them."""
        for name in ('exp94-smoke', 'exp94'):
            with self.subTest(name=name):
                args = parse(self.lines[name])
                self.assertEqual(args.manifest_checksums, 'CRC32C')
                self.assertIsNone(run_exp04.checksum_problem(run_exp04.method_list(args.methods),
                                                             args.manifest_checksums, None, args.conditions))

    def test_the_order_seeds_are_distinct_and_unused_by_the_sessions_being_compared(self):
        """EXP-20 used 200 and EXP-24 used 240; a repeated seed would repeat an order rather than reshuffle."""
        seeds = {name: parse(argv).order_seed for name, argv in self.lines.items()}
        self.assertEqual(len(set(seeds.values())), len(seeds))
        self.assertFalse({200, 240} & set(seeds.values()))

    def test_the_tuned_cells_raise_the_buffers_to_the_value_exp20_and_exp24_used(self):
        args = parse(self.lines['exp94'])
        self.assertEqual(args.tuned_tcp_max_bytes, 134217728)


class ArmFlagsAgainstTheEngine(unittest.TestCase):
    """Each new arm's flag string, parsed by pgdelta's own parser.

    measure.py builds the flags; pgdelta decides whether they mean anything. Nothing between them checks that,
    and a flag the engine refuses - or silently reads as another value - would fail or quietly mis-measure
    every run of a session that costs an hour and a half of VM time. `--hash-ahead 64G` in particular is a
    byte size parsed by pgdelta, not a number measure.py controls.
    """

    @classmethod
    def setUpClass(cls):
        try:
            from pgdelta import cli
        except ImportError as missing:  # pgdelta.inplace imports fcntl: POSIX only
            raise unittest.SkipTest(f'pgdelta is not importable here: {missing}')
        cls.parser = cli.build_parser()

    def parse_send(self, arm):
        return self.parser.parse_args(['send', '--source', '/tmp/s', '--connect', 'host:9000',
                                       *measure.PGDELTA_ARGS[arm]['send']])

    def test_hash_ahead_is_read_as_64_gibibytes_and_read_ahead_as_eight_threads(self):
        for arm in (HA, BBR_HA):
            with self.subTest(arm=arm):
                parsed = self.parse_send(arm)
                self.assertEqual(parsed.hash_ahead, 64 * 1024 ** 3)
                self.assertEqual(parsed.read_ahead, 8)
                self.assertEqual(parsed.window, 4)          # v1's value, unchanged by this arm
                self.assertEqual(parsed.source_kind, 'datadir')

    def test_only_the_bbr_arm_asks_for_bbr(self):
        self.assertEqual(self.parse_send(BBR_HA).congestion_control, 'bbr')
        self.assertIsNone(self.parse_send(HA).congestion_control)

    def test_neither_arm_asks_for_a_fast_path_which_this_harness_cannot_run(self):
        """--fast-path needs --source-kind live, which run_exp04.py has no plumbing for (EXP-95)."""
        for arm in (HA, BBR_HA):
            with self.subTest(arm=arm):
                self.assertIsNone(self.parse_send(arm).fast_path)

    def test_the_receiver_takes_nothing_of_hashing_ahead(self):
        """--hash-ahead reorders the sender's frames; what is sent is unchanged (EXP-91), so the receiver's
        flags must be v1's exactly, or the arm changes two things at once."""
        for arm in (HA, BBR_HA):
            with self.subTest(arm=arm):
                self.assertEqual(measure.PGDELTA_ARGS[arm]['receive'],
                                 measure.PGDELTA_ARGS[V1]['receive'])

    def test_the_orchestrator_will_require_both_flags_of_the_engine_before_a_run(self):
        """pgdelta_required_options is what the orchestrator checks `pgdelta send --help` against."""
        required = measure.pgdelta_required_options([HA, BBR_HA])['send']
        for flag in ('--hash-ahead', '--read-ahead', '--congestion-control'):
            self.assertIn(flag, required)


if __name__ == '__main__':
    unittest.main()
