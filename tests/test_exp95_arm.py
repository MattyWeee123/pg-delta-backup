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
