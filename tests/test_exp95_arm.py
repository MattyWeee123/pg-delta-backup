"""EXP-95's arm, pinned by pgdelta's own parser before any VM exists (rule 16).

The whole experiment is one contrast: `_ra` reads ahead and does **not** hash ahead, `_ha` does both. If the
two arms ever came to differ in anything else - or if `--read-ahead` stopped reaching the sender without
`--hash-ahead`, which is exactly the behaviour `cli.py:_read_ahead` makes conditional - the session would
measure something other than what it registered, and it would do so silently. These tests are what stops that.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'experiments' / 'network'))

import measure  # noqa: E402

V1 = 'pgdelta_direct_inplace'
RA = 'pgdelta_direct_inplace_ra'
HA = 'pgdelta_direct_inplace_ha'


class ArmIsKnownToTheOrchestrator(unittest.TestCase):
    """The arm needs a NAME as well as an engine, and this is what EXP-95's own smoke caught.

    Giving `pgdelta_direct_inplace_ra` an entry in PGDELTA_ENGINES is not enough: `run_exp04.py` validates
    `--methods` against `measure.ALL_METHODS`, which is built from the arms tuples. The first attempt added the
    engine and not the name, so a provisioned pair refused the session with "--methods must be distinct names
    from ..." after the VMs were already up. Six flag tests passed throughout, because none of them asked the
    question the orchestrator asks.
    """

    def test_the_arm_is_in_all_methods(self):
        self.assertIn(RA, measure.ALL_METHODS)

    def test_the_arm_is_treated_as_one_of_v1s_direct_paths(self):
        """It reads the stopped source's data directory like every other v1 arm, so it must be here or the
        harness dispatches it down the wrong path."""
        self.assertIn(RA, measure.PGDELTA_DIRECT_METHODS)
        self.assertIn(RA, measure.PGDELTA_METHODS)

    def test_every_arm_this_session_runs_is_accepted(self):
        for arm in ('full', V1, RA, HA):
            with self.subTest(arm=arm):
                self.assertIn(arm, measure.ALL_METHODS)

    def test_reading_ahead_is_not_folded_into_the_hash_ahead_arms(self):
        """RA_V1_ARMS is separate on purpose: this arm asks for no hashing ahead, so listing it among the
        hash-ahead arms would make the name lie about what runs."""
        self.assertNotIn(RA, measure.HA_V1_ARMS)
        self.assertEqual(measure.RA_V1_ARMS, (RA,))

    def test_every_engine_name_is_also_a_known_method(self):
        """The general form of the defect: an engine nobody can select is dead weight, and an engine whose
        name the orchestrator rejects fails only once a pair is already running."""
        for name in measure.PGDELTA_ENGINES:
            with self.subTest(engine=name):
                self.assertIn(name, measure.ALL_METHODS)

    def test_the_new_arm_sits_in_every_set_its_sibling_sits_in(self):
        """The check that would have caught both of this session's false starts, and the next one.

        An arm's behaviour is decided by *name membership* scattered across many containers in measure.py, not
        by its engine dict. Two separate crashes on an already-provisioned pair came from that: missing from
        ALL_METHODS the orchestrator refused the method outright, and missing from IN_PLACE_METHODS the
        harness passed `--output` while the engine passed `--in-place`, so the receiver exited 2 on
        "not allowed with argument --output". Both are invisible to a flag test.

        `_ra` and `_ha` differ in one flag, so they must differ in exactly one pair of containers - the arms
        tuples that name them - and agree everywhere else. Asserted by walking the module rather than by
        listing containers, so a container added later is covered without anyone remembering to come back.
        """
        allowed = {'HA_V1_ARMS', 'RA_V1_ARMS'}
        mismatched = []
        for name in dir(measure):
            container = getattr(measure, name)
            if not isinstance(container, (tuple, list, set, frozenset, dict)) or name in allowed:
                continue
            try:
                has_ha, has_ra = HA in container, RA in container
            except TypeError:                      # unhashable members, not a name collection
                continue
            if has_ha != has_ra:
                mismatched.append(f'{name}: _ha={has_ha} _ra={has_ra}')
        self.assertEqual([], mismatched,
                         'the _ra arm must appear wherever _ha does, except in the arms tuples: '
                         + '; '.join(mismatched))


class ArmFlags(unittest.TestCase):
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

    def test_the_new_arm_reads_ahead_and_does_not_hash_ahead(self):
        """The contrast the experiment rests on, read off the parser rather than the dict."""
        parsed = self.parse_send(RA)
        self.assertEqual(parsed.read_ahead, 8)
        self.assertFalse(parsed.hash_ahead, 'the _ra arm must not hash ahead; that is the whole contrast')

    def test_read_ahead_survives_without_hash_ahead(self):
        """The resolution rule the experiment depends on, asserted where it actually happens.

        The parser only records what was passed (`None` when nothing was); `cli._read_ahead` is what turns
        that into a thread count, and its rule is "as given, else the fast-path default WITH --fast-path or
        --hash-ahead and 0 without". So `_ra` must resolve to 8 even though it asks for no hashing ahead - if
        that ever stopped being true, `_ra` would silently become `v1` and the contrast would vanish.
        """
        from pgdelta import cli
        self.assertEqual(self.parse_send(RA).read_ahead, 8)
        self.assertIsNone(self.parse_send(V1).read_ahead, 'the parser records absence as None')
        self.assertEqual(cli._read_ahead(self.parse_send(RA)), 8, 'read-ahead must survive with no hash-ahead')
        self.assertEqual(cli._read_ahead(self.parse_send(V1)), 0, 'v1 reads ahead not at all')
        self.assertEqual(cli._read_ahead(self.parse_send(HA)), 8)

    def test_the_two_arms_differ_in_hash_ahead_alone(self):
        """Everything else must match, or the contrast is confounded."""
        ra, ha = dict(measure.PGDELTA_ENGINES[RA]), dict(measure.PGDELTA_ENGINES[HA])
        self.assertEqual(ha.pop('hash_ahead'), '64G')
        self.assertIsNone(ra.pop('hash_ahead', None))
        self.assertEqual(ra, ha, 'the _ra and _ha engines must differ in hash_ahead and nothing else')

    def test_the_new_arm_changes_nothing_on_the_receiver(self):
        """Reading ahead is a sender-side read order; what is sent is unchanged, so the receiver's flags must
        be v1's exactly (the same guard EXP-94 put on its own arms)."""
        self.assertEqual(measure.PGDELTA_ARGS[RA]['receive'], measure.PGDELTA_ARGS[V1]['receive'])

    def test_the_new_arm_keeps_v1s_transport_and_source(self):
        parsed = self.parse_send(RA)
        self.assertIsNone(parsed.congestion_control, 'EXP-95 runs cubic only; BBR is not in this contrast')
        self.assertEqual(parsed.window, 4)
        self.assertEqual(parsed.source_kind, 'datadir')
        self.assertIsNone(parsed.fast_path)

    def test_v1_and_ha_are_unchanged_by_adding_the_arm(self):
        """EXP-94's data is already scored and published; adding a name must not move its arms."""
        self.assertEqual(measure.PGDELTA_ENGINES[HA]['hash_ahead'], '64G')
        self.assertEqual(measure.PGDELTA_ENGINES[HA]['read_ahead'], 8)
        self.assertNotIn('hash_ahead', measure.PGDELTA_ENGINES[V1])
        self.assertNotIn('read_ahead', measure.PGDELTA_ENGINES[V1])


if __name__ == '__main__':
    unittest.main()
