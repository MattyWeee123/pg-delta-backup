"""Tests for T11's incumbent-tool harness (experiments/incumbents) and EXP-03's S2b and
S2c."""

import contextlib
import csv
import io
import json
import os
import re
import shutil
import stat
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from experiments.incumbents import checks, run_t11, tools
from experiments.tricky_copies import lsnscan, pgcluster, run_scenarios, scenarios
from experiments.tricky_copies.lsnscan import BLOCK
from test_tricky_copies import btree_leaf, heap_page

PG_BIN = Path(os.environ.get('PGDELTA_TEST_PG_BIN', '/opt/homebrew/opt/postgresql@17/bin'))

PG_REWIND_S2 = """\
pg_rewind: servers diverged at WAL location 0/415CDA8 on timeline 1
pg_rewind: rewinding from last common checkpoint at 0/415CD30 on timeline 1
pg_rewind: Done!
"""
PG_REWIND_S1 = """\
pg_rewind: source and target cluster are on the same timeline
pg_rewind: no rewind required
"""
PG_REWIND_CRASHED = """\
pg_rewind: executing "/usr/lib/postgresql/17/bin/postgres" for target server to complete crash recovery
LOG:  database system was not properly shut down; automatic recovery in progress
""" + PG_REWIND_S2
PROBACKUP_OK = """\
INFO: Catchup start, pg_probackup version: 2.5.16, PostgreSQL version: 17.11, remote: false, catchup-mode: DELTA
INFO: syncLSN = 0/415F070
INFO: pg_start_backup START LSN 0/7000028
INFO: Databases synchronized. Transfered datafiles size: 1290kB, transfered wal size: 16MB, time elapsed: 1s
INFO: Catchup incremental ratio (less is better): 4% (1290kB/31MB)
"""
PROBACKUP_REFUSED = """\
INFO: Catchup start, pg_probackup version: 2.5.16, PostgreSQL version: 17.11, remote: false, catchup-mode: DELTA
ERROR: Destination is not in source timeline history
"""
# What WAL-G v3.0.9 prints for `wal-g catchup-push --help`: --from-lsn is a pflag Uint64Var.
WALG_PUSH_HELP = """\
Creates incremental backup from lsn

Usage:
  wal-g catchup-push PGDATA --from-lsn LSN [flags]

Flags:
      --from-lsn uint   LSN to start incremental backup
  -h, --help            help for catchup-push
"""
# A wal-g stand-in that parses catchup-push's --from-lsn the way v3.0.9 does (pflag's
# strconv.ParseUint(s, 0, 64)), logs a backup name, and has catchup-fetch leave a mark.
FAKE_WALG = r"""#!/bin/sh
case "$1" in
catchup-push)
  case "$4" in
  ''|*[!0-9]*) echo "invalid argument \"$4\" for \"--from-lsn\" flag: strconv.ParseUint: parsing \"$4\": invalid syntax"; exit 1;;
  esac
  echo "INFO: Wrote backup with name base_000000020000000000000007_D_000000010000000000000004" >&2;;
catchup-fetch) touch "$2/fetched-$3";;
*) exit 1;;
esac
"""


def page(lsn, fill=b'x'):
    """An 8 KiB page whose pd_lsn is ``lsn``."""
    return (lsn >> 32).to_bytes(4, 'little') + (lsn & 0xFFFFFFFF).to_bytes(4, 'little') + (
        fill * BLOCK)[:BLOCK - 8]


def write(root, relpath, data):
    path = Path(root, relpath)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class OutcomeTest(unittest.TestCase):
    def test_classify_outcome(self):
        patterns = checks.REFUSAL_PATTERNS['pg_probackup']
        self.assertEqual(checks.classify_outcome(0, True, '', patterns),
                         ('accepted', 'exit status 0', None))
        outcome, reason, refusal = checks.classify_outcome(1, True, PROBACKUP_REFUSED, patterns)
        self.assertEqual((outcome, refusal), ('refused', 'message'))
        self.assertIn('Destination is not in source timeline history', reason)
        self.assertEqual(checks.classify_outcome(2, True, 'boom', patterns),
                         ('failed', 'exit status 2 after the copy was modified: boom', None))
        self.assertEqual(checks.classify_outcome(None, False, '', patterns)[0], 'failed')
        self.assertEqual(checks.classify_outcome(0, True, '', patterns, timed_out=True),
                         ('failed', 'timed out', None))

    def test_errors_are_not_refusals_for_a_reason(self):
        """A tool that stops with an error before touching the copy is still 'refused', as
        T11 defines it, but the run says it gave no reason about the copy, and quotes the
        error. pg_rewind's missing-WAL error is the case seen on the Mac."""
        stderr = ('pg_rewind: servers diverged at WAL location 0/8D10130 on timeline 1\n'
                  'pg_rewind: error: could not open file "t/pg_wal/000000010000000000000008": '
                  'No such file or directory\n'
                  'pg_rewind: error: could not find previous WAL record at 0/8D10130\n')
        self.assertEqual(checks.classify_outcome(1, False, stderr,
                                                 checks.REFUSAL_PATTERNS['pg_rewind']),
                         ('refused', 'exit status 1 before the copy was modified: pg_rewind: '
                          'error: could not find previous WAL record at 0/8D10130',
                          'error_unmodified'))
        self.assertEqual(checks.classify_outcome(1, False, '', ())[1:],
                         ('exit status 1 before the copy was modified', 'error_unmodified'))
        self.assertEqual(checks.error_line('INFO: a\nERROR: b\nINFO: c\n'), 'ERROR: b')
        self.assertEqual(checks.error_line('INFO: a\nINFO: c\n'), 'INFO: c')
        self.assertIsNone(checks.error_line(''))

    def test_pgdelta_uses_its_exit_codes(self):
        """pgdelta never writes to B, so B being unchanged says nothing: its exit code 2
        (invalid or unsupported input) is a refusal, a crash or a failed check is not."""
        codes = checks.REFUSAL_EXIT_CODES['pgdelta']
        self.assertEqual(checks.classify_outcome(2, False, 'pgdelta: error: unsafe path', (),
                                                 refusal_codes=codes),
                         ('refused', 'exit status 2: pgdelta: error: unsafe path', 'exit_code'))
        for code in (3, 4):
            outcome, reason, refusal = checks.classify_outcome(
                code, False, 'Traceback\npgdelta: error: boom', (), refusal_codes=codes)
            self.assertEqual((outcome, refusal), ('failed', None))
            self.assertEqual(reason, f'exit status {code}: pgdelta: error: boom')
        self.assertEqual(checks.classify_outcome(0, False, '', (), refusal_codes=codes)[0],
                         'accepted')

    def test_every_tool_has_refusal_patterns(self):
        self.assertEqual(set(checks.REFUSAL_PATTERNS) | {'lsn_skip'}, set(tools.TOOLS))
        self.assertTrue(checks.refusal_message(
            'INFO: x\nERROR: Destination is on timeline 1, but we are on 2\n',
            checks.REFUSAL_PATTERNS['walg_send']).startswith('ERROR: Destination'))
        self.assertTrue(checks.refusal_message(
            'pg_rewind: error: could not find common ancestor of the source and target '
            "cluster's timelines", checks.REFUSAL_PATTERNS['pg_rewind']))

    def test_verdict(self):
        """EXP-08's three checks: logical match, pg_amcheck and, where B has post-fork
        pages, none of them left after recovery."""
        self.assertEqual(checks.verdict('accepted', True, True, 0, True, True), 'correct')
        # S1: B has no post-fork pages, so the third check doesn't apply.
        self.assertEqual(checks.verdict('accepted', True, True, None, True), 'correct')
        # Listed pages that were never counted after recovery: not correct.
        self.assertEqual(checks.verdict('accepted', True, True, None, True, True),
                         'not_verified')
        self.assertEqual(checks.verdict('accepted', False, True, 0, True), 'wrong_data')
        self.assertEqual(checks.verdict('accepted', True, False, None, True), 'wrong_data')
        self.assertEqual(checks.verdict('accepted', True, True, 2, True, True), 'wrong_data')
        # Pages left after recovery are wrong data whatever else the copy did.
        self.assertEqual(checks.verdict('accepted', None, None, 3, False, True), 'wrong_data')
        self.assertEqual(checks.verdict('accepted', None, None, 0, False), 'not_verified')
        for outcome in ('refused', 'failed', 'skipped'):
            self.assertEqual(checks.verdict(outcome, None, None, None, None), outcome)
        self.assertEqual(checks.verdict('harness_error', None, None, None, None), 'not_verified')


# Refusal messages as the tools print them: (tool, output, expected class).
REFUSALS = [
    ('pg_rewind', 'pg_rewind: error: source and target clusters are from different systems',
     'identity_or_history'),
    ('pg_rewind', "pg_rewind: error: could not find common ancestor of the source and target "
                  "cluster's timelines", 'identity_or_history'),
    ('pg_probackup', 'ERROR: Database identifiers mismatch: we connected to DB id '
                     '7699400495508172219, but in "/data/b" we found id 7699402729295060974',
     'identity_or_history'),
    ('pg_probackup', 'ERROR: Destination is not in source timeline history',
     'identity_or_history'),
    ('pg_probackup', 'ERROR: Source is behind destination in timeline history',
     'identity_or_history'),
    ('pg_probackup', 'ERROR: Current START LSN 0/5000028 is lower than SYNC LSN 0/6000060, it '
                     'may indicate that we are trying to catchup with PostgreSQL instance from '
                     'the past', 'identity_or_history'),
    ('walg_send', 'ERROR: 2026/10/23 12:00:00.000000 System identifiers do not match',
     'identity_or_history'),
    ('walg_send', 'ERROR: 2026/10/23 12:00:00.000000 Destination is on timeline 1, but we are '
                  'on 2', 'identity_or_history'),
    ('walg_send', 'ERROR: 2026/10/23 12:00:00.000000 Catchup destination is already ahead (our '
                  'LSN 83886080, destination LSN 100663296).', 'identity_or_history'),
    # Refusals for another reason.
    ('pg_rewind', 'pg_rewind: error: target server must be shut down cleanly', 'other'),
    ('pg_rewind', 'pg_rewind: servers diverged at WAL location 0/8D10130 on timeline 1\n'
                  'pg_rewind: error: could not find previous WAL record at 0/8D10130', 'other'),
    ('pg_probackup', 'ERROR: Postmaster with pid 4242 is running in destination directory '
                     '"/data/b"', 'other'),
    ('pg_probackup', 'ERROR: Cannot get source timeline history', 'other'),
    ('pg_probackup', 'ERROR: Destination directory contains "backup_label" file', 'other'),
    ('walg_send', 'ERROR: Our system lacks System Identifier, cannot proceed', 'other'),
    ('walg', 'ERROR: System identifiers do not match', 'other'),  # catchup-push checks none
    ('pgdelta', 'pgdelta: error: unsafe path', 'other'),
]


class RefusalClassTest(unittest.TestCase):
    def test_table(self):
        """Every tool has an entry, and every pattern of the table has a message above."""
        self.assertEqual(set(checks.IDENTITY_OR_HISTORY_PATTERNS), set(tools.TOOLS))
        for tool, entries in checks.IDENTITY_OR_HISTORY_PATTERNS.items():
            for pattern, what in entries:
                self.assertTrue(what)
                self.assertTrue(any(re.search(pattern, output) for t, output, expected
                                    in REFUSALS if t == tool), (tool, pattern))

    def test_classes(self):
        for tool, output, expected in REFUSALS:
            with self.subTest(tool=tool, output=output):
                outcome, _reason, refusal = checks.classify_outcome(
                    2 if tool == 'pgdelta' else 1, False, output,
                    checks.REFUSAL_PATTERNS.get(tool, ()),
                    refusal_codes=checks.REFUSAL_EXIT_CODES.get(tool))
                self.assertEqual(outcome, 'refused')
                if expected == 'identity_or_history':
                    # Also one of the tool's refusal messages: refused for a stated reason.
                    self.assertEqual(refusal, 'message')
                found, evidence = checks.refusal_class(
                    outcome, output, checks.IDENTITY_OR_HISTORY_PATTERNS[tool])
                self.assertEqual(found, expected)
                if expected == 'identity_or_history':
                    self.assertIn(evidence, output)
                else:
                    self.assertIsNone(evidence)

    def test_only_refusals_have_a_class(self):
        patterns = checks.IDENTITY_OR_HISTORY_PATTERNS['pg_probackup']
        output = 'ERROR: Destination is not in source timeline history'
        for outcome in ('accepted', 'failed', 'skipped', 'harness_error'):
            self.assertEqual(checks.refusal_class(outcome, output, patterns), (None, None))
        self.assertEqual(checks.refusal_class('refused', '', patterns), ('other', None))


def flip_hint_bits(page, bits=0x0900):
    """``page`` with ``bits`` of its one tuple's t_infomask set (heap_page's layout); by
    default HEAP_XMIN_COMMITTED and HEAP_XMAX_INVALID, as a read sets them."""
    buf = bytearray(page)
    offset = struct.unpack_from('<I', buf, 24)[0] & 0x7FFF
    infomask = struct.unpack_from('<H', buf, offset + 20)[0]
    struct.pack_into('<H', buf, offset + 20, infomask | bits)
    return bytes(buf)


class TupleDataTest(unittest.TestCase):
    """The comparison diverged_after_recovery uses."""

    def test_same_tuple_data(self):
        base = heap_page(0x900, infomask=0, data=b'diverged')
        # Hint bits, a later page LSN and a checksum don't change the tuple data.
        later = flip_hint_bits(heap_page(0x5000, infomask=0, data=b'diverged', checksum=77))
        self.assertNotEqual(base, later)
        self.assertTrue(checks.same_tuple_data(base, later))
        self.assertEqual(checks.tuple_data(base), checks.tuple_data(later))
        self.assertEqual(checks.tuple_data(later)[:10], bytes(10))
        # Other tuple data, other infomask bits than the hint bits, or no page: different.
        self.assertFalse(checks.same_tuple_data(base, heap_page(0x900, infomask=0,
                                                                data=b'source!!')))
        self.assertFalse(checks.same_tuple_data(base, flip_hint_bits(base, 0x0010)))
        self.assertFalse(checks.same_tuple_data(base, None))
        self.assertFalse(checks.same_tuple_data(None, None))
        # All four bits are masked, so a frozen tuple (xmin committed and invalid) is
        # still the same tuple data: it still holds B's row.
        self.assertTrue(checks.same_tuple_data(base, flip_hint_bits(base, checks.HINT_BITS)))
        # The page-level hints the server's heap_mask ignores are not tuple data either
        # (test_page_level_hints); PD_ALL_VISIBLE, which VACUUM WAL-logs, is.
        flagged = bytearray(base)
        struct.pack_into('<H', flagged, 10, 0x0002)  # PD_PAGE_FULL
        self.assertTrue(checks.same_tuple_data(base, bytes(flagged)))
        struct.pack_into('<H', flagged, 10, 0x0004)  # PD_ALL_VISIBLE
        self.assertFalse(checks.same_tuple_data(base, bytes(flagged)))

    def test_page_level_hints(self):
        """Hints PostgreSQL changes without WAL, as its own heap_mask and btree_mask
        ignore them: on a heap page pd_prune_xid, PD_PAGE_FULL and PD_HAS_FREE_LINES
        (heap_page_prune_opt clears and resets them when it finds nothing to remove); on
        a btree leaf the line pointer flags (LP_DEAD from kill_prior_tuple),
        BTP_HAS_GARBAGE, BTP_SPLIT_END and the vacuum cycle id. Only main-fork pages are
        masked this way."""
        def with_header(page, flags=None, prune_xid=None):
            buf = bytearray(page)
            if flags is not None:
                struct.pack_into('<H', buf, 10, flags)
            if prune_xid is not None:
                struct.pack_into('<I', buf, 20, prune_xid)
            return bytes(buf)

        stale = with_header(heap_page(0x900, infomask=0, data=b'diverged'), flags=0x0002,
                            prune_xid=731)
        # What a prune that found nothing to remove leaves: flags and pd_prune_xid reset.
        for hinted in (with_header(stale, flags=0, prune_xid=0),
                       with_header(stale, flags=0x0001, prune_xid=9999),
                       with_header(flip_hint_bits(stale), flags=0x0003)):
            self.assertTrue(checks.same_tuple_data(stale, hinted))
            self.assertTrue(checks.same_tuple_data(hinted, stale, 'main'))
        self.assertFalse(checks.same_tuple_data(stale, with_header(stale, flags=0x0006)))
        # Not on another fork's page, whose header isn't masked.
        self.assertFalse(checks.same_tuple_data(stale, with_header(stale, prune_xid=0), 'fsm'))
        # A btree leaf: LP_DEAD, BTP_HAS_GARBAGE, BTP_SPLIT_END and the cycle id are hints.
        leaf = btree_leaf(0x900)
        hinted_leaf = bytearray(btree_leaf(0x7000, lp_flags=3, garbage=True))
        opaque = BLOCK - 16
        flags = struct.unpack_from('<H', hinted_leaf, opaque + 12)[0]
        struct.pack_into('<HH', hinted_leaf, opaque + 12, flags | (1 << 5), 77)
        self.assertTrue(checks.same_tuple_data(leaf, bytes(hinted_leaf)))
        self.assertFalse(checks.same_tuple_data(leaf, bytes(hinted_leaf), 'init'))
        other_key = bytearray(leaf)
        other_key[opaque - 16:opaque] = b'K' * 16
        self.assertFalse(checks.same_tuple_data(leaf, bytes(other_key)))

    def test_free_space_leftovers(self):
        """Bytes between pd_lower and pd_upper hold no tuple on a main-fork page; an FSM
        or VM page keeps its data there, so only the main fork ignores them."""
        base = heap_page(0x900, infomask=0, data=b'diverged')
        leftovers = bytearray(base)
        leftovers[100:110] = b'old tuple!'  # heap_page: pd_lower 28, pd_upper near the end
        self.assertTrue(checks.same_tuple_data(base, bytes(leftovers)))
        self.assertTrue(checks.same_tuple_data(base, bytes(leftovers), 'main'))
        for fork in ('fsm', 'vm', 'init'):
            self.assertFalse(checks.same_tuple_data(base, bytes(leftovers), fork), fork)
        # An FSM page: a bare header, its tree stored right after it.
        fsm = bytearray(BLOCK)
        struct.pack_into('<HHHH', fsm, 12, 24, BLOCK, BLOCK, BLOCK | 4)
        other = bytearray(fsm)
        other[40] = 0x7F
        self.assertFalse(checks.same_tuple_data(bytes(fsm), bytes(other), 'fsm'))
        self.assertTrue(checks.same_tuple_data(bytes(fsm), bytes(other), 'main'))
        # A btree page's leftovers are masked too; its line pointers are not.
        leaf = bytearray(btree_leaf(0x900))
        leaf[200:204] = b'junk'
        self.assertTrue(checks.same_tuple_data(btree_leaf(0x900), bytes(leaf)))

    def test_other_pages_ignore_only_lsn_and_checksum(self):
        leaf = btree_leaf(0x900)
        moved = bytearray(btree_leaf(0x5000))
        struct.pack_into('<H', moved, 8, 1234)
        self.assertTrue(checks.same_tuple_data(leaf, bytes(moved)))
        # A leaf's LP_DEAD is a hint on the main fork, not on another fork's page.
        self.assertTrue(checks.same_tuple_data(leaf, btree_leaf(0x900, lp_flags=3)))
        self.assertFalse(checks.same_tuple_data(leaf, btree_leaf(0x900, lp_flags=3), 'init'))
        fsm = page(0x10, b'f')
        self.assertTrue(checks.same_tuple_data(fsm, page(0x99, b'f')))
        self.assertFalse(checks.same_tuple_data(fsm, page(0x10, b'g')))

    def test_compare_after_recovery(self):
        """After recovery a page counts as left when it holds B's tuple data, whatever
        hint bits, LSN and checksum it has since gained, and S's tuple data differ."""
        with tempfile.TemporaryDirectory() as tmp:
            old, source, copy = Path(tmp, 'b'), Path(tmp, 's'), Path(tmp, 'c')
            rel = 'base/5/16384'
            stale = [heap_page(0x900, infomask=0, data=f'stale{i}!'.encode()) for i in range(4)]
            fresh = [heap_page(0x80, infomask=0, data=f'fresh{i}!'.encode()) for i in range(4)]
            # Block 3: B and S differ only in hint bits and LSN, so it isn't diverged.
            write(old, rel, b''.join(stale[:3]) + fresh[3])
            write(source, rel, b''.join(fresh[:3]) + flip_hint_bits(heap_page(0x70, infomask=0,
                                                                               data=b'fresh3!')))
            # 0: B's tuples with hint bits and a new LSN; 1: S's with hint bits; 2: neither.
            write(copy, rel, flip_hint_bits(heap_page(0x7000, infomask=0, data=b'stale0!'))
                  + flip_hint_bits(heap_page(0x7000, infomask=0, data=b'fresh1!'))
                  + heap_page(0x7000, infomask=0, data=b'pruned!') + fresh[3])
            pages = [{'file': rel, 'block': block} for block in range(4)]
            before = checks.compare_listed_pages(copy, old, source, pages)
            after = checks.compare_listed_pages(copy, old, source, pages,
                                                same=checks.same_tuple_data)
        keys = ('diverged_left', 'same_as_source', 'other', 'not_diverged')
        self.assertEqual({k: after[k] for k in keys},
                         {'diverged_left': 1, 'same_as_source': 1, 'other': 1,
                          'not_diverged': 1})
        self.assertEqual(after['diverged_left_examples'], [{'file': rel, 'block': 0,
                                                            'cause': 'content'}])
        # Byte for byte, the copy's first three pages are neither B's nor S's any more, and
        # block 3, where B and S differ only in hint bits and LSN, counts as left.
        self.assertEqual({k: before[k] for k in keys},
                         {'diverged_left': 1, 'same_as_source': 0, 'other': 3,
                          'not_diverged': 0})
        self.assertEqual(before['diverged_left_by_cause'], {'hint_bits_and_lsn': 1})


class ParseTest(unittest.TestCase):
    def test_pg_rewind(self):
        rewound = checks.parse_pg_rewind(PG_REWIND_S2)
        self.assertEqual(rewound['diverged_at'], '0/415CDA8')
        self.assertEqual(rewound['diverged_on_timeline'], 1)
        self.assertEqual(rewound['last_common_checkpoint'], '0/415CD30')
        self.assertTrue(rewound['done'])
        self.assertFalse(rewound['no_rewind_required'] or rewound['crash_recovery_run'])
        same = checks.parse_pg_rewind(PG_REWIND_S1)
        self.assertTrue(same['no_rewind_required'] and same['same_timeline'])
        self.assertIsNone(same['diverged_at'])
        self.assertTrue(checks.parse_pg_rewind(PG_REWIND_CRASHED)['crash_recovery_run'])

    def test_pg_probackup(self):
        ok = checks.parse_pg_probackup(PROBACKUP_OK)
        self.assertEqual(ok, {'sync_lsn': '0/415F070', 'start_lsn': '0/7000028',
                              'synchronized': True, 'incremental_ratio_percent': 4,
                              'errors': []})
        refused = checks.parse_pg_probackup(PROBACKUP_REFUSED)
        self.assertFalse(refused['synchronized'])
        self.assertEqual(refused['errors'], ['Destination is not in source timeline history'])

    def test_walg(self):
        stderr = ('INFO: 2026/10/23 Selecting the backup with name base_000000020000000000000007'
                  '_D_000000010000000000000004...\nINFO: Wrote backup with name '
                  'base_000000020000000000000007_D_000000010000000000000004\n')
        self.assertEqual(checks.parse_walg_backup_name(stderr),
                         'base_000000020000000000000007_D_000000010000000000000004')
        self.assertEqual(checks.parse_walg_backup_name('INFO: base_00000002000000000000000A'),
                         'base_00000002000000000000000A')
        self.assertIsNone(checks.parse_walg_backup_name('nothing here'))
        listing = json.dumps([{'backup_name': 'base_000000020000000000000005', 'time': 'x'},
                              {'backup_name': 'base_000000020000000000000007'}])
        self.assertEqual(checks.parse_walg_list_json(listing),
                         ['base_000000020000000000000005', 'base_000000020000000000000007'])
        self.assertEqual(checks.parse_walg_list_json('not json'), [])
        self.assertEqual(checks.parse_walg_list_json('{"a": 1}'), [])
        self.assertIsNone(checks.walg_sysid_flag(WALG_PUSH_HELP))
        self.assertEqual(checks.walg_from_lsn_flag(WALG_PUSH_HELP),
                         '--from-lsn uint   LSN to start incremental backup')
        self.assertIsNone(checks.walg_from_lsn_flag('Usage: wal-g catchup-fetch'))
        self.assertEqual(checks.walg_sysid_flag('      --from-sysid uint   system identifier'),
                         '--from-sysid')

    def test_conf_setting(self):
        conf = ("primary_conninfo = 'host=/tmp/a port=1'\n# primary_conninfo = 'nope'\n"
                "primary_conninfo = 'host=/tmp/b port=2 application_name=''x'''\n")
        self.assertEqual(tools.last_setting(conf, 'primary_conninfo'),
                         "host=/tmp/b port=2 application_name='x'")
        self.assertIsNone(tools.last_setting('port = 5', 'primary_conninfo'))


class DivergedPagesTest(unittest.TestCase):
    def test_compare_listed_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, source, copy = Path(tmp, 'b'), Path(tmp, 's'), Path(tmp, 'c')
            rel = 'base/5/16384'
            shared = heap_page(0x10)
            diverged = [heap_page(0x900, data=f'stale{i}!'.encode()) for i in range(6)]
            fresh = [heap_page(0x80, data=f'fresh{i}!'.encode()) for i in range(4)]
            write(old, rel, shared + b''.join(diverged[1:6]))
            write(source, rel, shared + b''.join(fresh[1:4]))
            # 0: B equals S; 1: stale; 2: repaired; 3: something else; 4: stale and S has
            # no such page; 5: absent from the copy.
            write(copy, rel, shared + diverged[1] + fresh[2] + heap_page(0x77) + diverged[4])
            pages = [{'file': rel, 'block': block} for block in range(6)]
            result = checks.compare_listed_pages(copy, old, source, pages)
        self.assertEqual({k: result[k] for k in ('listed', 'diverged_left', 'same_as_source',
                                                 'other', 'missing_in_copy', 'not_diverged')},
                         {'listed': 6, 'diverged_left': 2, 'same_as_source': 1, 'other': 1,
                          'missing_in_copy': 1, 'not_diverged': 1})
        self.assertEqual(result['diverged_left_by_kind'], {'main': 2})
        self.assertEqual(result['diverged_left_by_cause'], {'content': 1, 'absent_in_source': 1})
        self.assertEqual([e['block'] for e in result['diverged_left_examples']], [1, 4])
        self.assertIsNone(checks.read_page(tmp, rel, 0))

    def test_lsn_skip_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp, 's'), Path(tmp, 't')
            rel = 'base/5/16384'
            write(source, rel, page(0x10, b'n') + page(0x2000, b'n') + page(0x10, b'n'))
            write(source, 'global/pg_control', b'source control')
            write(source, 'pg_wal/000000020000000000000001', b'new wal')
            write(source, 'postgresql.conf', b'source conf')
            write(target, rel, page(0x10, b'o') + page(0x1500, b'o'))
            write(target, 'global/pg_control', b'old control')
            write(target, 'pg_wal/000000010000000000000001', b'old wal')
            write(target, 'base/5/99999', page(0x10))
            write(target, 'postgresql.conf', b'target conf')
            counts = tools.lsn_skip_copy(source, target, 0x1000)
            data = Path(target, rel).read_bytes()
            self.assertEqual(data[:BLOCK], page(0x10, b'o'))  # skipped: B keeps its page
            self.assertEqual(data[BLOCK:], page(0x2000, b'n') + page(0x10, b'n'))
            self.assertEqual(Path(target, 'global/pg_control').read_bytes(), b'source control')
            self.assertEqual(Path(target, 'postgresql.conf').read_bytes(), b'target conf')
            self.assertFalse(Path(target, 'base/5/99999').exists())
            self.assertEqual(sorted(p.name for p in Path(target, 'pg_wal').iterdir()),
                             ['000000020000000000000001'])
        self.assertEqual(counts, {'pages_copied': 2, 'pages_skipped': 1, 'files_copied': 2,
                                  'files_removed': 2})


class FakeContext:
    """The parts of scenarios.BuildContext that the S2/S2b checkers use."""
    check = scenarios.BuildContext.check

    def __init__(self, root):
        self.old_copy, self.source_backup = Path(root, 'b'), Path(root, 's')
        self.facts, self.observations, self.checks, self.notes = {}, {}, {}, []


class S2bCheckTest(unittest.TestCase):
    """The page-LSN check that S2b's builder records in scenario.json."""

    def test_helpers(self):
        self.assertEqual(scenarios.segment_location('base/5/1', 3), ('base/5/1', 3))
        self.assertEqual(scenarios.segment_location('base/5/1', scenarios.SEGMENT_BLOCKS + 3),
                         ('base/5/1.1', 3))
        self.assertEqual(scenarios.history_switchpoint(
            '1\t0/415f140\tno recovery target specified\n'), '0/415F140')
        self.assertEqual(scenarios.history_switchpoint(
            '1\t0/3000000\tno recovery target specified\n\n'
            '2\t0/50000A0\tno recovery target specified\n'), '0/50000A0')
        with self.assertRaises(ValueError):
            scenarios.history_switchpoint('# nothing\n')

    def test_pages_after_lsn(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, 'base/5/16384', page(0x10) + page(0x5000) + page(0x20) + b'partial')
            write(tmp, 'base/5/16384.1', page(0x7000))
            write(tmp, 'base/5/16384_fsm', page(0x6000))
            write(tmp, 'global/pg_control', page(0x9000))  # not a relation file
            write(tmp, 'pg_wal/000000010000000000000001', page(0x9000))
            pages = scenarios.pages_after_lsn(tmp, 0x1000)
        self.assertEqual(pages, [
            {'file': 'base/5/16384', 'block': 1, 'kind': 'main', 'lsn': '0/5000'},
            {'file': 'base/5/16384.1', 'block': 0, 'kind': 'main', 'lsn': '0/7000'},
            {'file': 'base/5/16384_fsm', 'block': 0, 'kind': 'fsm', 'lsn': '0/6000'}])

    def make(self, tmp, *, state='in production', redo='0/700', checkpoint='0/750',
             on_disk_lsn=0x900, source_lsns=(0x80, 0x90), checker=scenarios.check_s2b,
             untouched_lsn=None, flush_checkpoints=('0/750', '0/750')):
        """B's accounts blocks 1 and 2 diverged after the fork at 0/800; with
        ``untouched_lsn``, so did block 0 of another table (tags), which S left alone."""
        ctx = FakeContext(tmp)
        rel = 'base/5/16384'
        write(ctx.old_copy, rel, heap_page(0x100) + heap_page(on_disk_lsn, data=b'diverged')
              + heap_page(0x950, data=b'diverged'))
        write(ctx.source_backup, rel, heap_page(0x100) + heap_page(source_lsns[0])
              + heap_page(source_lsns[1]))
        write(ctx.source_backup, 'pg_wal/00000002.history',
              b'1\t0/800\tno recovery target specified\n')
        ctx.facts = {'diverged': {'file': rel, 'blocks': [1, 2]}, 'split_lsn': '0/800',
                     'old_primary_end_lsn': '0/960',
                     'flush': {'clean': True, 'checkpoint_lsn_before_flush': flush_checkpoints[0],
                               'checkpoint_lsn_after_flush': flush_checkpoints[1]}}
        if untouched_lsn is not None:
            write(ctx.old_copy, 'base/5/16408', heap_page(untouched_lsn, data=b'TAGS'))
            write(ctx.source_backup, 'base/5/16408', heap_page(0x60, data=b'tags'))
            ctx.facts['untouched'] = {'table': 'tags', 'file': 'base/5/16408', 'blocks': [0]}
        old = {'timeline': 1, 'system_identifier': '7', 'cluster_state': state,
               'redo_location': redo, 'checkpoint_location': checkpoint,
               'min_recovery_location': '0/960'}
        checker(ctx, old, {'timeline': 2, 'system_identifier': '7'}, {'lsn': redo})
        return ctx

    def test_check_s2b(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.make(tmp)
        failed = [name for name, check in ctx.checks.items() if not check['ok']]
        self.assertEqual(failed, [])
        self.assertEqual(set(ctx.checks), {'diverged_pages_differ', 'timeline_switched',
                                           'same_system_identifier', 'exposed_pages',
                                           'old_copy_crashed', 'no_checkpoint_after_fork',
                                           'no_checkpoint_during_flush',
                                           'diverged_pages_on_disk_after_fork'})
        self.assertEqual(ctx.observations['fork_lsn'], '0/800')
        self.assertEqual(ctx.observations['old_copy_redo_lsn'], '0/700')
        self.assertEqual(ctx.observations['old_copy_min_recovery_lsn'], '0/960')
        self.assertEqual(ctx.observations['post_fork_page_count'], 2)
        self.assertEqual([(p['block'], p['lsn'], p['source_lsn'], p['exposed'])
                          for p in ctx.observations['post_fork_pages']],
                         [(1, '0/900', '0/80', True), (2, '0/950', '0/90', True)])
        self.assertEqual(ctx.observations['exposed_pages'], 2)
        self.assertEqual(ctx.observations['exposed_pages_by_kind'], {'main': 2})
        self.assertEqual(ctx.checks['exposed_pages']['exposed_pages'], 2)

    def test_check_s2b_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            clean = self.make(Path(tmp, 'a'), state='shut down', redo='0/958',
                              checkpoint='0/960')
            unflushed = self.make(Path(tmp, 'b'), on_disk_lsn=0x400)
        self.assertFalse(clean.checks['old_copy_crashed']['ok'])
        self.assertFalse(clean.checks['no_checkpoint_after_fork']['ok'])
        self.assertTrue(clean.checks['diverged_pages_on_disk_after_fork']['ok'])
        self.assertFalse(unflushed.checks['diverged_pages_on_disk_after_fork']['ok'])
        self.assertEqual(unflushed.checks['diverged_pages_on_disk_after_fork'][
            'pages_with_lsn_after_fork'], 1)

    def test_checkpoint_during_flush_fails_the_build(self):
        """The flush must write the diverged pages without a checkpoint or restartpoint:
        the cluster's pg_control_checkpoint() location must be the same before and after
        it. A missing record of either fails too."""
        for checker, state in ((scenarios.check_s2b, 'in production'),
                               (scenarios.check_s2c, 'shut down in recovery')):
            with tempfile.TemporaryDirectory() as tmp:
                moved = self.make(Path(tmp, 'a'), state=state, checker=checker,
                                  flush_checkpoints=('0/600', '0/750'))
                unrecorded = self.make(Path(tmp, 'b'), state=state, checker=checker,
                                       flush_checkpoints=(None, None))
            check = moved.checks['no_checkpoint_during_flush']
            self.assertFalse(check['ok'], checker.__name__)
            self.assertEqual((check['checkpoint_lsn_before_flush'],
                              check['checkpoint_lsn_after_flush']), ('0/600', '0/750'))
            self.assertFalse(unrecorded.checks['no_checkpoint_during_flush']['ok'])
            # The other checks don't see it: B's REDO and checkpoint are before the fork.
            self.assertTrue(moved.checks['no_checkpoint_after_fork']['ok'])

    def test_no_exposed_page_fails_the_build(self):
        """When the source rewrote every block B diverged on after B's REDO location, an
        LSN-threshold tool skips none of them: the scenario must not count as built. A
        table the source never writes after the fork restores the exposure."""
        with tempfile.TemporaryDirectory() as tmp:
            covered = self.make(Path(tmp, 'a'), source_lsns=(0x880, 0x890))
            untouched = self.make(Path(tmp, 'b'), source_lsns=(0x880, 0x890),
                                  untouched_lsn=0x940)
        self.assertFalse(covered.checks['exposed_pages']['ok'])
        self.assertEqual(covered.observations['exposed_pages'], 0)
        self.assertTrue(covered.checks['diverged_pages_on_disk_after_fork']['ok'])
        self.assertTrue(untouched.checks['exposed_pages']['ok'])
        self.assertEqual(untouched.observations['exposed_pages_by_file'], {'base/5/16408': 1})
        self.assertEqual(untouched.checks['diverged_pages_on_disk_after_fork'][
            'pages_written_after_split'], 3)

    def test_mark_exposed_pages(self):
        """Exposed: S holds the block with an LSN at or below B's REDO and other bytes."""
        with tempfile.TemporaryDirectory() as tmp:
            old, source = Path(tmp, 'b'), Path(tmp, 's')
            write(old, 'base/5/1', page(0x900, b'b') * 5)
            write(source, 'base/5/1', page(0x700, b's') + page(0x701, b's') + page(0x600, b'b')
                  + page(0x900, b'b'))  # no block 4
            pages = [{'file': 'base/5/1', 'block': b, 'kind': 'main', 'lsn': '0/900'}
                     for b in range(5)]
            exposed = scenarios.mark_exposed_pages(old, source, pages, 0x700)
        self.assertEqual(exposed, 2)
        self.assertEqual([(p['source_lsn'], p['exposed']) for p in pages],
                         [('0/700', True), ('0/701', False), ('0/600', True),
                          ('0/900', False), (None, False)])

    def test_check_s2c(self):
        """S2c: B is a standby stopped cleanly, with REDO before the fork."""
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.make(Path(tmp, 'a'), state='shut down in recovery',
                            checker=scenarios.check_s2c, untouched_lsn=0x940)
            crashed = self.make(Path(tmp, 'b'), checker=scenarios.check_s2c)
        failed = [name for name, check in ctx.checks.items() if not check['ok']]
        self.assertEqual(failed, [])
        self.assertEqual(set(ctx.checks), {'diverged_pages_differ', 'timeline_switched',
                                           'same_system_identifier', 'exposed_pages',
                                           'old_copy_is_stopped_standby',
                                           'no_checkpoint_after_fork',
                                           'no_checkpoint_during_flush',
                                           'diverged_pages_on_disk_after_fork'})
        self.assertEqual(ctx.observations['exposed_pages'], 3)
        self.assertFalse(crashed.checks['old_copy_is_stopped_standby']['ok'])
        self.assertEqual(crashed.checks['old_copy_is_stopped_standby']['cluster_state'],
                         'in production')

    def test_evict_sql_by_version(self):
        self.assertIn('FILTER (WHERE pg_buffercache_evict(b.bufferid))',
                      scenarios.evict_dirty_user_buffers_sql(17))
        self.assertIn('FILTER (WHERE (pg_buffercache_evict(b.bufferid)).buffer_evicted)',
                      scenarios.evict_dirty_user_buffers_sql(18))

    def test_registry_and_selection(self):
        spec = scenarios.BY_ID['S2b']
        self.assertEqual(spec.dirname, 'S2b-ex-primary-no-checkpoint-after-fork')
        self.assertIs(spec.checker, scenarios.check_s2b)
        s2c = scenarios.BY_ID['S2c']
        self.assertEqual(s2c.dirname, 'S2c-standby-of-ex-primary-after-fork')
        self.assertIs(s2c.builder, scenarios.build_s2c)
        self.assertIs(s2c.checker, scenarios.check_s2c)
        self.assertEqual(s2c.lsn_prediction, 'wrong')
        self.assertEqual([s.id for s in run_scenarios.select_scenarios(['s2b', 'S1B', 'S2',
                                                                        's2C'])],
                         ['S2b', 'S1b', 'S2', 'S2c'])
        self.assertEqual(run_t11.T11_SCENARIOS, ('S1', 'S2', 'S2b', 'S2c'))
        # S2b and S2c update a table burst_sql never writes; S2 keeps EXP-03's writes.
        self.assertNotIn(scenarios.UNTOUCHED_TABLE, scenarios.burst_sql(200_000, 2))
        self.assertIn(f'UPDATE {scenarios.UNTOUCHED_TABLE} ', scenarios.UNTOUCHED_DIVERGING_SQL)
        self.assertNotIn(scenarios.UNTOUCHED_TABLE, scenarios.diverging_sql(200_000))
        conf = {'source': {'wal_keep_size': '1GB'}, 'old_copy': {}, 'source_ran_with': 'source',
                'old_copy_ran_with': 'old_copy'}
        meta = scenarios.scenario_metadata(spec, rows=1, built_ok=True,
                                           source_cluster={'name': 'standby', 'kept': True,
                                                           'datadir': 'pgdwork-x/standby'},
                                           server_conf=conf)
        self.assertEqual(meta['source_cluster']['datadir'], 'pgdwork-x/standby')
        self.assertEqual(meta['server_conf'], conf)
        self.assertEqual(scenarios.scenario_metadata(spec, rows=1, built_ok=False)['server_conf'],
                         {'source': {}, 'old_copy': {}})

    def test_role_settings(self):
        """A standby cloned from a primary of another role gets its own role's settings,
        and the primary's role's settings reset to their defaults."""
        class Recorder:
            def __init__(self, name):
                self.name, self.appended, self.queries = name, [], []

            def append_conf(self, settings):
                self.appended.append(settings)

            def query(self, sql):
                self.queries.append(sql)
                return [['wal_keep_size', '0']]

        ctx = SimpleNamespace(role_conf={'source': {'wal_keep_size': '4GB'}, 'old_copy': {}},
                              step=lambda text: None)
        switch = scenarios.BuildContext.switch_role_conf
        primary, standby = Recorder('primary'), Recorder('standby')
        switch(ctx, standby, primary, 'source', 'old_copy')  # S1: R becomes B
        self.assertEqual(standby.appended, [{'wal_keep_size': '0'}])
        self.assertIn("name IN ('wal_keep_size')", primary.queries[0])
        primary, standby = Recorder('primary'), Recorder('standby')
        switch(ctx, standby, primary, 'old_copy', 'source')  # S2: P becomes B, R the source
        self.assertEqual((standby.appended, primary.queries), ([{'wal_keep_size': '4GB'}], []))
        ctx.role_conf['old_copy'] = {'wal_keep_size': '1GB'}
        primary, standby = Recorder('primary'), Recorder('standby')
        switch(ctx, standby, primary, 'source', 'old_copy')
        self.assertEqual((standby.appended, primary.queries), ([{'wal_keep_size': '1GB'}], []))


class FlushWithoutCheckpointTest(unittest.TestCase):
    """S2b's flush on a real server, for each of PostgreSQL 17 and 18 that is installed:
    pg_buffercache_evict() returns a boolean in 17 and a record in 18."""

    def flush(self, bindir):
        sockdir = pgcluster.short_tempdir()
        cluster = pgcluster.Cluster('flush', pgcluster.PgBin(bindir), sockdir / 'data', sockdir,
                                    pgcluster.free_port(), sockdir / 'server.log')
        try:
            cluster.initdb()
            cluster.start()
            cluster.psql('CREATE EXTENSION pg_buffercache;\n'
                         'CREATE TABLE t AS SELECT g AS id, md5(g::text) AS v '
                         'FROM generate_series(1, 2000) g;\nCHECKPOINT;\n')
            checkpoint = cluster.scalar('SELECT checkpoint_lsn FROM pg_control_checkpoint()')
            cluster.psql('UPDATE t SET v = upper(v) WHERE id = 1;')
            written = cluster.scalar('SELECT pg_current_wal_lsn()')
            flush = scenarios.flush_without_checkpoint(cluster)
            after = cluster.scalar('SELECT checkpoint_lsn FROM pg_control_checkpoint()')
            page = scenarios.read_block(sockdir / 'data' / cluster.scalar(
                "SELECT pg_relation_filepath('t')"), 0)
            cluster.stop('immediate')
        finally:
            cluster.ensure_stopped()
            shutil.rmtree(sockdir, ignore_errors=True)
        self.assertTrue(flush['clean'], flush)
        self.assertGreater(flush['buffers_evicted'], 0)
        self.assertEqual(after, checkpoint)  # no checkpoint ran
        # The updated page reached disk with an LSN past the last checkpoint.
        self.assertGreater(lsnscan.page_lsn(page), lsnscan.parse_lsn(checkpoint))
        self.assertLessEqual(lsnscan.page_lsn(page), lsnscan.parse_lsn(written))

    def test_postgresql_17_and_18(self):
        found = [b for b in (pgcluster.DEFAULT_PG17, pgcluster.DEFAULT_PG18)
                 if (b / 'initdb').is_file()]
        if not found:
            self.skipTest('no PostgreSQL 17 or 18 binaries')
        for bindir in found:
            with self.subTest(bindir=str(bindir)):
                self.flush(bindir)


def executable(path, text):
    Path(path).write_text(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


class FakeServer:
    """A server that records what is done to it, in ``events``."""

    def __init__(self, events, datadir='/copy', running=False):
        self.events, self.datadir, self.running = events, Path(datadir), running
        self.logfile, self.sockdir, self.port, self.name = Path('/nonexistent.log'), '/s', 1, 'x'

    def start(self, timeout=300):
        self.events.append('start')
        self.running = True

    def stop(self, mode='fast', timeout=300):
        self.events.append(f'stop {mode}')
        self.running = False

    def promote(self, timeout=300):
        self.events.append('promote')

    def wait_for(self, sql, db='postgres', timeout=120, interval=0.2):
        self.events.append('wait')

    def psql(self, sql, db='postgres', timeout=900):
        self.events.append(sql.strip())
        return ''

    def scalar(self, sql, db='postgres'):
        return 'f'

    def query(self, sql, db='postgres', timeout=900):
        return []

    def server_options(self):
        return ''

    def ensure_stopped(self):
        self.running = False


class BringUpOrderTest(unittest.TestCase):
    """EXP-08's third check counts B's post-fork pages once the copy reached consistency
    and was checkpointed and stopped cleanly, before anything reads its tables on a
    primary: pg_amcheck --heapallindexed's heap scans, and any read of a primary, prune
    pages (heap_page_prune_opt), which would turn B's stale pages into 'other'."""

    def patches(self, events):
        def amcheck(cluster, timeout=3600):
            events.append('amcheck')
            return {'ok': True}

        def logical(query):
            events.append('logical read')
            return {}
        return (mock.patch.object(tools, 'run_amcheck', amcheck),
                mock.patch.object(tools, 'compute_logical_state', logical),
                mock.patch.object(tools, 'compare_logical_states', lambda e, a: []))

    def test_standby_counts_before_amcheck(self):
        events = []
        server = FakeServer(events)
        job = SimpleNamespace(new_server=lambda name, datadir: server,
                              source=SimpleNamespace(scalar=lambda sql: '0/10'),
                              pgbin=SimpleNamespace(tool=lambda name: name), expected={})

        def count(copy):
            events.append('count')
            self.assertFalse(server.running)
            return {'diverged_left': 0, 'other': 0}
        amcheck, logical, compare = self.patches(events)
        with amcheck, logical, compare, \
                mock.patch.object(tools, 'ensure_standby_config', lambda c, s: 'harness'), \
                mock.patch.object(tools, 'run', lambda cmd, **kw: events.append('pg_ctl start')), \
                mock.patch.object(tools, 'wait_caught_up', lambda j, c, t: (True, None, '0/10')):
            info, logical_result, amcheck_result, counted = tools.bring_up_standby(
                job, Path('/copy'), count)
        self.assertEqual(events, ['pg_ctl start', 'logical read', 'promote', 'wait',
                                  'CHECKPOINT;', 'stop fast', 'count', 'start', 'amcheck',
                                  'stop fast'])
        self.assertEqual(counted, {'diverged_left': 0, 'other': 0})
        self.assertTrue(info['promoted'] and info['checkpointed'] and info['stopped_cleanly']
                        and info['restarted_for_amcheck'])
        self.assertIsNone(info['error'])
        self.assertEqual((logical_result['match'], amcheck_result), (True, {'ok': True}))

    def test_standby_count_error(self):
        """A count that fails is an after-recovery error; amcheck still runs."""
        events = []
        job = SimpleNamespace(new_server=lambda name, datadir: FakeServer(events),
                              source=SimpleNamespace(scalar=lambda sql: '0/10'),
                              pgbin=SimpleNamespace(tool=lambda name: name), expected={})

        def count(copy):
            raise OSError('unreadable')
        amcheck, logical, compare = self.patches(events)
        with amcheck, logical, compare, \
                mock.patch.object(tools, 'ensure_standby_config', lambda c, s: 'harness'), \
                mock.patch.object(tools, 'run', lambda cmd, **kw: None), \
                mock.patch.object(tools, 'wait_caught_up', lambda j, c, t: (True, None, '0/10')):
            info, _logical, amcheck_result, counted = tools.bring_up_standby(job, Path('/c'),
                                                                             count)
        self.assertIsNone(counted)
        self.assertIn('OSError: unreadable', info['after_recovery_error'])
        self.assertEqual(amcheck_result, {'ok': True})

    def test_restore_counts_before_reading(self):
        """pgdelta's O: recovery ends, then CHECKPOINT, a clean stop and the count; the
        server is started again for the logical comparison and pg_amcheck."""
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            output = write(tmp, 'output/PG_VERSION', b'17\n').parent
            job = SimpleNamespace(source_backup=Path(tmp, 's'), old_copy=Path(tmp, 'b'),
                                  listed_pages=[], expected={}, pgbin=None, work=Path(tmp))

            def counted(job_, copy, when):
                events.append('count')
                return {'diverged_left': 0, 'other': 0, 'counted_after': when}

            def server(name, pgbin, datadir, sockdir, port, logfile):
                return FakeServer(events, datadir)
            amcheck, logical, compare = self.patches(events)
            from experiments.tricky_copies import evaluate
            with amcheck, \
                    mock.patch.object(evaluate, 'compute_logical_state',
                                      lambda q: events.append('logical read') or {'tables': {}}), \
                    mock.patch.object(evaluate, 'compare_logical_states', lambda e, a: []), \
                    mock.patch.object(evaluate, 'Cluster', server), \
                    mock.patch.object(tools, 'count_after_recovery', counted), \
                    mock.patch.object(tools.evaluation, 'compare_trees',
                                      lambda a, b: {'identical': True}), \
                    mock.patch.object(tools.evaluation, 'verify_backup',
                                      lambda o, p: {'ok': True}):
                info, logical_result, amcheck_result, _extra, after = tools.bring_up_restore(
                    job, output)
        self.assertEqual(events, ['start', 'wait', 'CHECKPOINT;', 'stop fast', 'count', 'start',
                                  'wait', 'logical read', 'amcheck'])
        self.assertTrue(info['checkpointed'] and info['stopped_cleanly']
                        and info['restarted_for_amcheck'])
        self.assertIsNone(info['error'])
        self.assertTrue(logical_result['match'])
        self.assertEqual(amcheck_result, {'ok': True})
        self.assertEqual(after['diverged_left'], 0)
        self.assertIn('before', after['counted_after'])

    def accepted_job(self, tmp, tool):
        return tools.Job(scenario_id='S2c', scenario_dir=tmp, meta={'rows': 5}, expected={},
                         listed_pages=[], tool=tool, tool_info={'available': True},
                         run_dir=Path(tmp, 'run'), pgbin=None, verbose=False)

    def test_pgdelta_never_stopped_cleanly(self):
        """A restored O that never stopped cleanly after recovery has no after-recovery
        count, so its verdict can't be 'correct'."""
        def adapter(job):
            job.target = job.work / 'output'
            return tools.ToolRun([], 0, False, '', 0.1, {'basis_unchanged': True})
        bring_up = ({'caught_up': True, 'checkpointed': True, 'stopped_cleanly': False,
                     'error': None}, {'match': True, 'differences': [], 'read_as': 'restored'},
                    {'ok': True}, {}, None)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(tools.ADAPTERS, {'pgdelta': adapter}), \
                mock.patch.object(tools, 'bring_up_restore', lambda job, output: bring_up):
            result = tools.run_job(self.accepted_job(tmp, 'pgdelta'))
        self.assertEqual((result['outcome'], result['logical_match'], result['amcheck_ok']),
                         ('accepted', True, True))
        self.assertIsNone(result['diverged_after_recovery'])
        self.assertIn('never stopped cleanly', result['bring_up']['after_recovery_error'])
        self.assertEqual(result['verdict'], 'not_verified')

    def test_standby_never_stopped_cleanly(self):
        bring_up = ({'caught_up': True, 'promoted': True, 'checkpointed': True,
                     'stopped_cleanly': False, 'error': 'CommandError: stop failed'},
                    {'match': True, 'differences': [], 'read_as': 'hot standby'}, None, None)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(tools, 'bring_up_standby', lambda job, copy, count: bring_up):
            job = self.accepted_job(tmp, 'pg_rewind')
            job.target = Path(tmp, 'target')
            result = {'outcome': 'accepted', 'logical_match': None, 'amcheck_ok': None,
                      'diverged_before_recovery': None, 'diverged_after_recovery': None}
            tools.check_accepted(job, result)
        self.assertIsNone(result['diverged_after_recovery'])
        self.assertIn('never stopped cleanly', result['bring_up']['after_recovery_error'])
        self.assertEqual(checks.verdict('accepted', result['logical_match'],
                                        result['amcheck_ok'], None, True, True), 'not_verified')


class ProbeTest(unittest.TestCase):
    def test_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            pgbin = SimpleNamespace(major=17, bindir=Path(tmp))
            disabled = tools.probe('pg_probackup', pgbin=pgbin, pg_probackup='none')
            self.assertFalse(disabled['available'])
            self.assertIn('disabled', disabled['reason'])
            missing = tools.probe('walg', pgbin=pgbin, wal_g=str(Path(tmp, 'nope')))
            self.assertIn('not an executable file', missing['reason'])
            self.assertIn('not found', tools.probe('pg_rewind', pgbin=pgbin)['reason'])
            self.assertTrue(tools.probe('lsn_skip', pgbin=pgbin)['available'])
            old = executable(Path(tmp, 'pg_probackup'),
                             '#!/bin/sh\necho "pg_probackup 2.5.16 (PostgreSQL 16.4)"\n')
            wrong = tools.probe('pg_probackup', pgbin=pgbin, pg_probackup=str(old))
            self.assertFalse(wrong['available'])
            self.assertIn('not built for PostgreSQL 17', wrong['reason'])
            walg = executable(Path(tmp, 'wal-g'), '#!/bin/sh\ncase "$1" in\n'
                              '--version) echo "wal-g version v3.0.9";;\n'
                              '--help) echo "  catchup-push  Creates incremental backup";;\n'
                              'catchup-push) echo "      --from-lsn uint   LSN";;\nesac\n')
            push = tools.probe('walg', pgbin=pgbin, wal_g=str(walg))
            send = tools.probe('walg_send', pgbin=pgbin, wal_g=str(walg))
        self.assertTrue(push['available'])
        self.assertEqual(push['version'], 'wal-g version v3.0.9')
        self.assertIsNone(push['sysid_flag'])
        self.assertEqual(push['from_lsn_flag'], '--from-lsn uint   LSN')
        self.assertFalse(send['available'])
        self.assertIn('no catchup-send', send['reason'])

    def test_skipped_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = tools.Job(scenario_id='S2', scenario_dir=tmp, meta={'rows': 5}, expected={},
                            listed_pages=None, tool='walg',
                            tool_info={'available': False, 'reason': 'WAL-G: disabled'},
                            run_dir=Path(tmp, 'run'), pgbin=None)
            result = tools.run_job(job)
            self.assertFalse(Path(tmp, 'run').exists())
        self.assertEqual((result['outcome'], result['verdict']), ('skipped', 'skipped'))
        self.assertEqual(result['outcome_reason'], 'WAL-G: disabled')

    def test_walg_env(self):
        job = SimpleNamespace(source=SimpleNamespace(sockdir=Path('/tmp/pgdsrc'), port=5555))
        with mock.patch.dict(os.environ, {'WALG_S3_PREFIX': 's3://elsewhere', 'PGHOST': 'x',
                                          'WALE_GPG_KEY_ID': 'k', 'HOME': '/home/me'}):
            env = tools.walg_env(job, Path('/data/t11/storage'))
        self.assertEqual(env['WALG_FILE_PREFIX'], '/data/t11/storage')
        self.assertEqual((env['PGHOST'], env['PGPORT'], env['PGUSER']),
                         ('/tmp/pgdsrc', '5555', 'postgres'))
        self.assertNotIn('WALG_S3_PREFIX', env)
        self.assertNotIn('WALE_GPG_KEY_ID', env)
        self.assertEqual(env['HOME'], '/home/me')

    def test_ensure_standby_config(self):
        source = SimpleNamespace(sockdir=Path('/tmp/pgdsrc'), port=5555)
        with tempfile.TemporaryDirectory() as tmp:
            written = Path(tmp, 'a')
            written.mkdir()
            Path(written, 'standby.signal').touch()
            Path(written, 'postgresql.auto.conf').write_text(
                "primary_conninfo = 'user=postgres host=/tmp/pgdsrc port=5555'\n")
            self.assertEqual(tools.ensure_standby_config(written, source), 'tool')
            stale = Path(tmp, 'b')
            stale.mkdir()
            Path(stale, 'postgresql.auto.conf').write_text(
                "primary_conninfo = 'host=/tmp/gone port=1'\n")
            self.assertEqual(tools.ensure_standby_config(stale, source), 'harness')
            self.assertTrue(Path(stale, 'standby.signal').is_file())
            conninfo = tools.last_setting(Path(stale, 'postgresql.auto.conf').read_text(),
                                          'primary_conninfo')
        self.assertEqual(conninfo, 'host=/tmp/pgdsrc port=5555 user=postgres dbname=postgres')


# catchup-push succeeds but names no backup, and catchup-list finds none.
FAKE_WALG_NO_NAME = r"""#!/bin/sh
case "$1" in
catchup-push) echo "INFO: backup pushed" >&2;;
catchup-list) echo "[]";;
*) exit 1;;
esac
"""
# catchup-receive can't bind its port, so catchup-send finds nobody listening.
FAKE_WALG_NO_RECEIVER = r"""#!/bin/sh
case "$1" in
catchup-receive) echo "INFO: Receiving $2 on port $3"; echo "ERROR: listen tcp :$3: bind: address already in use"; exit 1;;
catchup-send) echo "ERROR: dial tcp $3: connect: connection refused" >&2; exit 1;;
*) exit 1;;
esac
"""


def walg_job(tmp, fake, redo='0/415F070', tool='walg'):
    """A Job for the WAL-G adapters, with a fake source and copy of B (no PostgreSQL)."""
    job = tools.Job(scenario_id='S2b', scenario_dir=tmp, expected={}, listed_pages=None,
                    meta={'old_copy': {'redo_location': redo, 'system_identifier': '7'},
                          'source_backup': {'system_identifier': '7'},
                          'source_cluster': {'kept': True, 'datadir': 'pgdwork-x/standby'}},
                    tool=tool, tool_info={'available': True, 'path': str(fake),
                                            'sysid_flag': None},
                    run_dir=Path(tmp, 'run'), pgbin=None, verbose=False)
    job.logs.mkdir(parents=True)
    job.work.mkdir(parents=True)
    job.source = SimpleNamespace(datadir=Path(tmp, 'source'), sockdir=Path('/tmp/pgdsrc'),
                                 port=5555)
    job.target = Path(tmp, 'target')
    job.target.mkdir()
    return job


class WalgAdapterTest(unittest.TestCase):
    def test_push_command(self):
        cmd = tools.walg_push_command('wal-g', '/data/s', '0/415F070')
        self.assertEqual(cmd, ['wal-g', 'catchup-push', '/data/s', '--from-lsn', '68546672'])
        self.assertEqual(int(cmd[-1]), 0x415F070)
        self.assertEqual(tools.walg_push_command('wal-g', '/s', '1/0', '--sysid', '7')[-3:],
                         [str(1 << 32), '--sysid', '7'])

    def test_catchup_push_takes_the_lsn_as_a_number(self):
        """v3.0.9 rejects --from-lsn 0/415F070 before doing anything; the adapter must
        pass a number, or every WAL-G run looks like a refusal."""
        with tempfile.TemporaryDirectory() as tmp:
            fake = executable(Path(tmp, 'wal-g'), FAKE_WALG)
            job = walg_job(tmp, fake)
            run = tools.run_walg(job)
            fetched = sorted(p.name for p in job.target.iterdir())
        self.assertEqual(run.returncode, 0, run.output)
        self.assertEqual(run.commands[0]['command'][-2:], ['--from-lsn', '68546672'])
        self.assertEqual((run.parsed['from_lsn'], run.parsed['from_lsn_argument']),
                         ('0/415F070', '68546672'))
        self.assertEqual(run.parsed['backup_name'],
                         'base_000000020000000000000007_D_000000010000000000000004')
        self.assertEqual([c['name'] for c in run.commands], ['catchup-push', 'catchup-fetch'])
        self.assertEqual(fetched,
                         ['fetched-base_000000020000000000000007_D_000000010000000000000004'])

    def run_job(self, tmp, fake, tool):
        """tools.run_job on a fake source and copy of B, with no PostgreSQL."""
        job = walg_job(tmp, fake, tool=tool)
        source, target = job.source, job.target
        source.scalar = lambda sql: '0/5000000'

        def start_source():
            job.source = source

        def copy_old_copy():
            job.target = target
        with mock.patch.object(job, 'start_source', start_source), \
                mock.patch.object(job, 'copy_old_copy', copy_old_copy):
            return tools.run_job(job)

    def test_no_backup_name_is_a_harness_error(self):
        """The harness, not WAL-G, failed to continue the flow: no refusal."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_job(tmp, executable(Path(tmp, 'wal-g'), FAKE_WALG_NO_NAME), 'walg')
        self.assertEqual((result['outcome'], result['verdict'], result['refusal']),
                         ('harness_error', 'not_verified', None))
        self.assertIn('named a backup', result['harness_error'])
        self.assertEqual(result['returncode'], 0)
        self.assertEqual([c['name'] for c in result['commands']], ['catchup-push', 'catchup-list'])

    def test_unreachable_receiver_is_a_harness_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_job(tmp, executable(Path(tmp, 'wal-g'), FAKE_WALG_NO_RECEIVER),
                                  'walg_send')
        self.assertEqual((result['outcome'], result['verdict']), ('harness_error', 'not_verified'))
        self.assertIn('never reached catchup-receive', result['harness_error'])
        self.assertIn('the receiver exited with status 1', result['harness_error'])
        self.assertEqual(result['parsed']['receiver_returncode'], 1)


def result_doc(scenario, tool, **fields):
    return {'scenario': scenario, 'tool': tool, 'outcome': 'accepted', 'verdict': 'correct',
            'logical_match': True, 'amcheck_ok': True, 'seconds': 1.5,
            'diverged_before_recovery': None, 'diverged_after_recovery': None, **fields}


class SummaryTest(unittest.TestCase):
    def test_collect_and_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            documents = [
                result_doc('S2b', 'walg', outcome='accepted', verdict='wrong_data',
                           logical_match=False, diverged_before_recovery={'diverged_left': 9},
                           diverged_after_recovery={'diverged_left': 7, 'other': 2}),
                result_doc('S2', 'pg_probackup', outcome='refused', verdict='refused',
                           logical_match=None, amcheck_ok=None, seconds=0.2, refusal='message',
                           outcome_reason='exit status 1: ERROR: Destination is not in source '
                                          'timeline history'),
                result_doc('S1', 'pgdelta'),
                result_doc('S1', 'pg_rewind', bring_up={'caught_up': True, 'error': None}),
            ]
            for document in documents:
                tools.write_result(out / document['scenario'] / document['tool'], document)
            (out / 'scenarios' / 'S1-x').mkdir(parents=True)
            Path(out, 'S9', 'broken').mkdir(parents=True)
            Path(out, 'S9', 'broken', 'result.json').write_text('{')
            results = checks.collect_results(out, [s.id for s in scenarios.SCENARIOS],
                                             tools.TOOLS)
            self.assertEqual([(r['scenario'], r['tool']) for r in results],
                             [('S1', 'pg_rewind'), ('S1', 'pgdelta'), ('S2', 'pg_probackup'),
                              ('S2b', 'walg')])
            json_path, csv_path = checks.write_summary(out, results, {'rows': 5})
            summary = json.loads(json_path.read_text())
            with csv_path.open() as handle:
                rows = list(csv.reader(handle))
        self.assertEqual(rows[0], list(checks.CSV_COLUMNS))
        self.assertEqual(rows[0][:7], ['scenario', 'tool', 'outcome', 'logical_match',
                                       'amcheck_ok', 'diverged_pages_left', 'seconds'])
        self.assertEqual(rows[0][7:10], ['verdict', 'refusal', 'outcome_reason'])
        self.assertEqual(rows[3][:10], ['S2', 'pg_probackup', 'refused', '', '', '', '0.2',
                                        'refused', 'message',
                                        'exit status 1: ERROR: Destination is not in source '
                                        'timeline history'])
        self.assertEqual(rows[4][:9], ['S2b', 'walg', 'accepted', 'False', 'True', '7', '1.5',
                                       'wrong_data', ''])
        by_name = dict(zip(rows[0], rows[4]))
        self.assertEqual((by_name['diverged_before_recovery'], by_name['refusal_class'],
                          by_name['arm'], by_name['i3_judged_on']), ('9', '', '', ''))
        # The listed pages that hold neither B's nor S's tuple data after recovery.
        self.assertEqual(by_name['other_after_recovery'], '2')
        self.assertEqual(dict(zip(rows[0], rows[1]))['other_after_recovery'], '')
        self.assertEqual(summary['runs'][3]['other_after_recovery'], 2)
        self.assertEqual(summary['rows'], 5)
        self.assertEqual(summary['runs'][3]['verdict'], 'wrong_data')
        self.assertTrue(summary['runs'][0]['caught_up'])

    def test_result_written_before_the_after_recovery_count(self):
        """Its one count, under 'diverged_pages', was taken before recovery."""
        old = {'scenario': 'S2', 'tool': 'lsn_skip', 'diverged_pages': {'diverged_left': 4}}
        row = checks.summary_row(old)
        self.assertEqual((row['diverged_before_recovery'], row['diverged_pages_left']), (4, None))

    def test_refusal_class_arm_and_i3_pointer(self):
        """S2b refused for another reason than identity or history: the summary points to
        the tool's S2c result in the same arm, which I3 is judged on."""
        default = {**run_t11.server_settings('4GB'), 'source_ran_with': 'source',
                   'old_copy_ran_with': 'old_copy'}
        retained = {**run_t11.server_settings('4GB', '4GB'), 'source_ran_with': 'source',
                    'old_copy_ran_with': 'old_copy'}
        refused = {'outcome': 'refused', 'verdict': 'refused', 'refusal': 'message',
                   'logical_match': None, 'amcheck_ok': None}
        documents = [
            result_doc('S2b', 'pg_probackup', server_conf=default, refusal_class='other',
                       outcome_reason='exit status 1: ERROR: Postmaster with pid 1 is running '
                                      'in destination directory', **refused),
            result_doc('S2c', 'pg_probackup', server_conf=default, verdict='wrong_data',
                       logical_match=False, diverged_after_recovery={'diverged_left': 3}),
            result_doc('S2b', 'walg_send', server_conf=default, refusal_class='identity_or_history',
                       refusal_class_evidence='ERROR: Destination is on timeline 1, but we are '
                                              'on 2', **refused),
            result_doc('S2b', 'pg_rewind', server_conf=retained, refusal_class='other',
                       **refused),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for document in documents:
                tools.write_result(Path(tmp, document['scenario'], document['tool']), document)
            results = checks.collect_results(tmp, ['S2b', 'S2c'], tools.TOOLS)
            json_path, csv_path = checks.write_summary(tmp, results, {})
            runs = {(r['scenario'], r['tool']): r
                    for r in json.loads(json_path.read_text())['runs']}
            with csv_path.open() as handle:
                rows = {(r['scenario'], r['tool']): r for r in csv.DictReader(handle)}
        probackup = runs[('S2b', 'pg_probackup')]
        self.assertEqual(probackup['i3_judged_on'],
                         {'scenario': 'S2c', 'tool': 'pg_probackup', 'arm': 'default',
                          'outcome': 'accepted', 'verdict': 'wrong_data',
                          'why': 'S2b was refused for another reason than identity or history '
                                 '(EXP-08)'})
        self.assertEqual(rows[('S2b', 'pg_probackup')]['i3_judged_on'], 'S2c (wrong_data)')
        self.assertEqual(rows[('S2b', 'pg_probackup')]['refusal_class'], 'other')
        self.assertEqual(rows[('S2c', 'pg_probackup')]['i3_judged_on'], '')
        # An identity-or-history refusal counts as "refuses" itself.
        self.assertIsNone(runs[('S2b', 'walg_send')]['i3_judged_on'])
        self.assertEqual(runs[('S2b', 'walg_send')]['refusal_class'], 'identity_or_history')
        self.assertIn('timeline 1', runs[('S2b', 'walg_send')]['refusal_class_evidence'])
        # No S2c run in the retained arm for pg_rewind yet.
        self.assertEqual(rows[('S2b', 'pg_rewind')]['i3_judged_on'], 'S2c (not run)')
        self.assertEqual({key: row['arm'] for key, row in rows.items()},
                         {('S2b', 'pg_probackup'): 'default', ('S2c', 'pg_probackup'): 'default',
                          ('S2b', 'walg_send'): 'default', ('S2b', 'pg_rewind'): 'retained'})
        self.assertEqual(rows[('S2c', 'pg_probackup')]['diverged_pages_left'], '3')
        self.assertIsNone(checks.arm_of(None))


    def test_each_run_carries_its_build(self):
        """A summary covers every run under OUT; runs from builds with other rows or
        settings than this invocation's must say so, row by row."""
        conf = run_t11.server_settings('4GB')
        recorded = {**conf, 'source_ran_with': 'source', 'old_copy_ran_with': 'old_copy'}
        documents = [
            result_doc('S1', 'pg_rewind', rows=2000, server_conf=recorded),
            result_doc('S2', 'pg_rewind', rows=2000,
                       server_conf=run_t11.server_settings('4GB', '4GB')),
            result_doc('S2b', 'pg_rewind', rows=200000, server_conf=recorded),
            result_doc('S2b', 'lsn_skip', rows=2000),  # written before settings were recorded
            result_doc('S1', 'walg', rows=2000, outcome='skipped', verdict='skipped'),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for document in documents:
                tools.write_result(Path(tmp, document['scenario'], document['tool']), document)
            results = checks.collect_results(tmp, ['S1', 'S2', 'S2b'], tools.TOOLS)
            others = checks.other_builds(results, 2000, conf)
            _, csv_path = checks.write_summary(tmp, results, {'invocation': {'rows': 2000}})
            with csv_path.open() as handle:
                rows = {(r['scenario'], r['tool']): r for r in csv.DictReader(handle)}
        self.assertEqual([(o['scenario'], o['tool'], o['rows']) for o in others],
                         [('S2', 'pg_rewind', 2000), ('S2b', 'pg_rewind', 200000),
                          ('S2b', 'lsn_skip', 2000)])
        self.assertIsNone(others[2]['server_conf'])
        build = {key: tuple(rows[key][c] for c in ('rows', 'source_conf', 'old_copy_conf'))
                 for key in rows}
        self.assertEqual(build[('S1', 'pg_rewind')], ('2000', 'wal_keep_size=4GB', ''))
        self.assertEqual(build[('S2', 'pg_rewind')],
                         ('2000', 'wal_keep_size=4GB', 'wal_keep_size=4GB'))
        self.assertEqual(build[('S2b', 'pg_rewind')], ('200000', 'wal_keep_size=4GB', ''))
        self.assertEqual(build[('S2b', 'lsn_skip')], ('2000', '', ''))


class DriverTest(unittest.TestCase):
    def test_parse_list(self):
        self.assertEqual(run_t11.parse_list('s1, S2b ,', list(scenarios.BY_ID), 'scenario'),
                         ['S1', 'S2b'])
        self.assertEqual(run_t11.parse_list('PG_REWIND,walg', list(tools.TOOLS), 'tool'),
                         ['pg_rewind', 'walg'])
        with self.assertRaises(SystemExit):
            run_t11.parse_list('S1,S42', list(scenarios.BY_ID), 'scenario')
        with self.assertRaises(SystemExit):
            run_t11.parse_list(' , ', list(tools.TOOLS), 'tool')
        self.assertEqual(run_t11.server_settings('none'), {'source': {}, 'old_copy': {}})
        self.assertEqual(run_t11.server_settings('4GB', 'none'),
                         {'source': {'wal_keep_size': '4GB'}, 'old_copy': {}})
        self.assertEqual(run_t11.server_settings('4GB', '1GB'),
                         {'source': {'wal_keep_size': '4GB'}, 'old_copy': {'wal_keep_size': '1GB'}})

    def test_arms(self):
        """EXP-08's two arms of B's WAL retention, each into its own default directory."""
        self.assertEqual(run_t11.resolve_arm(None, None), ('default', None))
        self.assertEqual(run_t11.resolve_arm(None, 'none'), ('default', None))
        self.assertEqual(run_t11.resolve_arm('default', None), ('default', None))
        self.assertEqual(run_t11.resolve_arm('retained', None), ('retained', '4GB'))
        self.assertEqual(run_t11.resolve_arm('retained', '1GB'), ('retained', '1GB'))
        self.assertEqual(run_t11.resolve_arm(None, '512MB'), ('retained', '512MB'))
        with self.assertRaises(SystemExit):
            run_t11.resolve_arm('default', '4GB')
        for arm, size in (('default', None), ('retained', '4GB')):
            conf = run_t11.server_settings('4GB', size)
            self.assertEqual(checks.arm_of(conf), arm)
        self.assertEqual(checks.ARMS, ('default', 'retained'))

    def test_zero_retention_is_the_default_arm(self):
        """wal_keep_size 0 is PostgreSQL's default: B keeps no WAL, so it is the default
        arm, whatever unit it is written in, and --arm retained can't have it."""
        for size in ('0', '0MB', '0 GB', '0.0kB', '0B'):
            self.assertEqual(run_t11.resolve_arm(None, size), ('default', None), size)
            self.assertEqual(run_t11.resolve_arm('default', size), ('default', None), size)
            with self.assertRaises(SystemExit):
                run_t11.resolve_arm('retained', size)
            self.assertEqual(checks.arm_of(run_t11.server_settings('4GB', size)), 'default',
                             size)
        for size in ('1', '1kB', '0.5GB', '4GB', '1TB'):
            self.assertEqual(run_t11.resolve_arm(None, size), ('retained', size), size)
            self.assertEqual(checks.arm_of(run_t11.server_settings('4GB', size)), 'retained',
                             size)
        with self.assertRaises(SystemExit):
            run_t11.resolve_arm(None, 'lots')
        with self.assertRaises(SystemExit):
            run_t11.resolve_arm('retained', '-1')
        self.assertEqual(checks.size_in_bytes('4GB'), 4 << 30)
        self.assertEqual(checks.size_in_bytes('512'), 512 << 20)  # wal_keep_size is in MB
        self.assertEqual(checks.size_in_bytes(' 1 kB '), 1024)
        self.assertIsNone(checks.size_in_bytes('4 gigs'))
        self.assertIsNone(checks.size_in_bytes('4gb'))  # PostgreSQL's units are case-sensitive

    def test_scenario_facts(self):
        meta = {'built_ok': True, 'rows': 7,
                'old_copy': {'cluster_state': 'shut down in recovery', 'redo_location': '0/700',
                             'checkpoint_location': '0/750', 'min_recovery_location': '0/960'},
                'server_conf': run_t11.server_settings('4GB', '4GB'),
                'observations': {'fork_lsn': '0/800', 'exposed_pages': 3,
                                 'post_fork_page_count': 5, 'post_fork_pages': [{}] * 5}}
        facts = run_t11.scenario_facts('/x', meta)
        self.assertEqual((facts['old_copy_state'], facts['old_copy_redo_lsn'],
                          facts['old_copy_min_recovery_lsn'], facts['fork_lsn'],
                          facts['exposed_pages'], facts['arm']),
                         ('shut down in recovery', '0/700', '0/960', '0/800', 3, 'retained'))
        self.assertNotIn('post_fork_pages', facts)

    def test_listed_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            stored = {'observations': {'post_fork_pages': [{'file': 'f', 'block': 1}],
                                       'fork_lsn': '0/800'}}
            pages, info = run_t11.listed_pages(tmp, stored)
            self.assertEqual((pages, info['source']), ([{'file': 'f', 'block': 1}],
                                                       'scenario.json'))
            same = {'observations': {}, 'timelines': {'old_copy': 1, 'source_backup': 1}}
            self.assertEqual(run_t11.listed_pages(tmp, same), (None, None))
            write(tmp, 'old_copy/base/5/1', page(0x100) + page(0x900))
            write(tmp, 'source_backup/pg_wal/00000002.history', b'1\t0/800\tx\n')
            forked = {'observations': {}, 'timelines': {'old_copy': 1, 'source_backup': 2}}
            pages, info = run_t11.listed_pages(tmp, forked)
        self.assertEqual([(p['file'], p['block']) for p in pages], [('base/5/1', 1)])
        self.assertEqual(info['fork_lsn'], '0/800')
        self.assertTrue(info['source'].startswith('computed by run_t11'))

    def test_scenario_ready(self):
        conf = run_t11.server_settings('4GB')
        with tempfile.TemporaryDirectory() as tmp:
            meta = {'built_ok': True, 'rows': 50,
                    'server_conf': {**conf, 'source_ran_with': 'source',
                                    'old_copy_ran_with': 'old_copy'},
                    'source_cluster': {'kept': True, 'datadir': 'pgdwork-x/standby'}}
            Path(tmp, 'scenario.json').write_text(json.dumps(meta))
            self.assertIsNone(run_t11.scenario_ready(tmp, 50, conf))
            write(tmp, 'pgdwork-x/standby/PG_VERSION', b'17\n')
            self.assertEqual(run_t11.scenario_ready(tmp, 50, conf), meta)
            self.assertIsNone(run_t11.scenario_ready(tmp, 51, conf))
            self.assertIsNone(run_t11.scenario_ready(tmp, 50, run_t11.server_settings('none')))
            self.assertIsNone(run_t11.scenario_ready(tmp, 50, run_t11.server_settings('4GB', '4GB')))
            # Built before the roles were told apart: every cluster had the settings.
            del meta['server_conf']
            Path(tmp, 'scenario.json').write_text(json.dumps({**meta, 'extra_conf': {}}))
            self.assertIsNone(run_t11.scenario_ready(tmp, 50, run_t11.server_settings('none')))


@unittest.skipUnless((PG_BIN / 'initdb').is_file() and (PG_BIN / 'pg_rewind').is_file()
                     and (PG_BIN / 'pg_amcheck').is_file(), f'{PG_BIN} has no PostgreSQL')
class PgRewindClusterTest(unittest.TestCase):
    """run_t11 end to end on tiny scenarios: pg_rewind must repair S1 and S2; on S2b and
    S2c it must repair too, while the LSN-skip control must be called wrong."""

    def run_t11(self, tmp, scenario_ids, tool_names, *extra):
        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet):
            status = run_t11.main(['--pg-bin', str(PG_BIN), '--scenarios', scenario_ids,
                                   '--tools', tool_names, '--rows', '2000', '--out', str(tmp),
                                   '--wal-keep-size', '1GB', '--quiet', *extra])
        self.assertEqual(status, 0, quiet.getvalue()[-3000:])
        return {(r['scenario'], r['tool']): r
                for r in json.loads(Path(tmp, 'summary.json').read_text())['runs']}

    def result(self, tmp, scenario, tool):
        return json.loads(Path(tmp, scenario, tool, 'result.json').read_text())

    def meta(self, tmp, scenario):
        return json.loads(Path(tmp, 'scenarios', scenarios.BY_ID[scenario].dirname,
                               'scenario.json').read_text())

    def wal_keep_mb(self, tmp, scenario):
        """wal_keep_size (MB) as B and the kept source cluster would run with it."""
        directory = Path(tmp, 'scenarios', scenarios.BY_ID[scenario].dirname)
        meta = json.loads((directory / 'scenario.json').read_text())
        values = {}
        for name, datadir in (('old_copy', directory / 'old_copy'),
                              ('source', directory / meta['source_cluster']['datadir'])):
            values[name] = pgcluster.run([PG_BIN / 'postgres', '-D', datadir, '-C',
                                          'wal_keep_size']).stdout.strip()
        return values, meta['server_conf']

    def assert_post_fork_facts(self, meta):
        """B's REDO and checkpoint lie before the fork, its post-fork pages are on disk,
        and some of them are exposed to an LSN-threshold tool."""
        self.assertTrue(meta['built_ok'], meta.get('failed_checks') or meta.get('error'))
        observations = meta['observations']
        fork = lsnscan.parse_lsn(observations['fork_lsn'])
        self.assertLess(lsnscan.parse_lsn(observations['old_copy_redo_lsn']), fork)
        self.assertLess(lsnscan.parse_lsn(observations['old_copy_checkpoint_lsn']), fork)
        on_disk = meta['checks']['diverged_pages_on_disk_after_fork']
        self.assertTrue(on_disk['ok'])
        self.assertEqual(on_disk['pages_with_lsn_after_fork'], on_disk['pages_written_after_split'])
        # Both the accounts pages and those of the table the source never writes.
        self.assertEqual(set(on_disk['flush']['by_table']), {'accounts', 'tags'})
        self.assertGreaterEqual(observations['post_fork_page_count'],
                                on_disk['pages_written_after_split'])
        self.assertGreater(observations['exposed_pages'], 0)
        self.assertTrue(meta['checks']['exposed_pages']['ok'])
        self.assertEqual(observations['exposed_pages'],
                         sum(p['exposed'] for p in observations['post_fork_pages']))

    def assert_counted_before_amcheck(self, control):
        """The control's copy holds B's exposed pages, and nothing between the tool and
        the after-recovery count may rewrite them: replay as a hot standby can't prune, and
        the count comes before pg_amcheck's heap scans, which would. So every page left
        before recovery is still B's after it, by tuple data, and none is 'other'."""
        before, after = control['diverged_before_recovery'], control['diverged_after_recovery']
        self.assertEqual(after['other'], 0, after)
        self.assertGreater(after['diverged_left'], 0)
        self.assertEqual(after['diverged_left'] + after['not_diverged'],
                         before['diverged_left'] + before['not_diverged'], (before, after))
        self.assertIn('before pg_amcheck', after['counted_after'])
        self.assertTrue(control['bring_up']['restarted_for_amcheck'])

    def test_pg_rewind_repairs_s1_and_s2(self):
        tmp = Path(tempfile.mkdtemp(prefix='pgd'))
        try:
            runs = self.run_t11(tmp, 'S1,S2', 'pg_rewind')
            for scenario in ('S1', 'S2'):
                run = runs[(scenario, 'pg_rewind')]
                self.assertEqual((run['outcome'], run['verdict']), ('accepted', 'correct'),
                                 self.result(tmp, scenario, 'pg_rewind'))
                self.assertTrue(run['logical_match'] and run['amcheck_ok'] and run['caught_up'])
                self.assertEqual(run['arm'], 'default')
            s1, s2 = self.result(tmp, 'S1', 'pg_rewind'), self.result(tmp, 'S2', 'pg_rewind')
            self.assertTrue(s1['parsed']['no_rewind_required'])
            self.assertIsNone(s1['diverged_before_recovery'])
            self.assertIsNone(s1['diverged_after_recovery'])
            self.assertEqual(s2['parsed']['diverged_on_timeline'], 1)
            self.assertEqual(s2['diverged_before_recovery']['diverged_left'], 0)
            self.assertGreater(s2['diverged_before_recovery']['listed'], 0)
            self.assertEqual(s2['diverged_after_recovery']['diverged_left'], 0)
            self.assertTrue(s2['bring_up']['checkpointed'] and s2['bring_up']['stopped_cleanly'])
            self.assertEqual(s2['bring_up']['recovery_conf'], 'tool')
            self.assertFalse(Path(tmp, 'S2', 'pg_rewind', 'work').exists())
            # S2 now records its post-fork pages: its REDO lies after the fork, so every one
            # whose block S didn't rewrite past that point is exposed.
            s2_meta = self.meta(tmp, 'S2')
            self.assertGreater(s2_meta['observations']['exposed_pages'], 0)
            self.assertGreater(lsnscan.parse_lsn(s2_meta['observations']['old_copy_redo_lsn']),
                               lsnscan.parse_lsn(s2_meta['observations']['fork_lsn']))
            self.assertEqual(s2['post_fork_pages']['source'], 'scenario.json')
            # Only the source keeps its WAL; B runs with EXP-03's settings, whether it was
            # the standby (S1) or the old primary (S2).
            for scenario in ('S1', 'S2'):
                values, conf = self.wal_keep_mb(tmp, scenario)
                self.assertEqual(values, {'old_copy': '0', 'source': '1024'}, scenario)
                self.assertEqual((conf['old_copy_ran_with'], conf['source_ran_with']),
                                 ('old_copy', 'source'))
                self.assertEqual((conf['source'], conf['old_copy']),
                                 ({'wal_keep_size': '1GB'}, {}))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_s2b(self):
        tmp = Path(tempfile.mkdtemp(prefix='pgd'))
        try:
            runs = self.run_t11(tmp, 'S2b', 'pg_rewind,lsn_skip',
                                '--old-copy-wal-keep-size', '512MB')
            meta = self.meta(tmp, 'S2b')
            self.assert_post_fork_facts(meta)
            self.assertEqual(meta['old_copy']['cluster_state'], 'in production')
            self.assertEqual(self.wal_keep_mb(tmp, 'S2b')[0], {'old_copy': '512', 'source': '1024'})
            self.assertEqual((meta['server_conf']['source'], meta['server_conf']['old_copy']),
                             ({'wal_keep_size': '1GB'}, {'wal_keep_size': '512MB'}))
            self.assertTrue(meta['checks']['old_copy_conf_applied']['ok'])
            rewind = self.result(tmp, 'S2b', 'pg_rewind')
            self.assertTrue(rewind['parsed']['crash_recovery_run'])
            self.assertEqual(runs[('S2b', 'pg_rewind')]['verdict'], 'correct', rewind)
            self.assertEqual(rewind['diverged_before_recovery']['diverged_left'], 0)
            self.assertEqual(rewind['diverged_after_recovery']['diverged_left'], 0)
            self.assertEqual((runs[('S2b', 'pg_rewind')]['rows'],
                              runs[('S2b', 'pg_rewind')]['old_copy_conf'],
                              runs[('S2b', 'pg_rewind')]['arm']),
                             (2000, 'wal_keep_size=512MB', 'retained'))
            summary = json.loads(Path(tmp, 'summary.json').read_text())
            self.assertEqual(summary['runs_from_other_builds'], [])
            self.assertEqual(summary['invocation']['arm'], 'retained')
            control = runs[('S2b', 'lsn_skip')]
            self.assertEqual(control['verdict'], 'wrong_data')
            self.assertGreater(control['diverged_pages_left'], 0)
            self.assertFalse(control['logical_match'])
            self.assert_counted_before_amcheck(self.result(tmp, 'S2b', 'lsn_skip'))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_s2c(self):
        """S2c on PostgreSQL 17: B is a standby of the old primary that replayed and wrote
        its diverged pages, stopped cleanly with REDO before the fork."""
        tmp = Path(tempfile.mkdtemp(prefix='pgd'))
        try:
            runs = self.run_t11(tmp, 'S2c', 'pg_rewind,pgdelta,lsn_skip')
            meta = self.meta(tmp, 'S2c')
            self.assertEqual(meta['postgresql']['old_copy']['major'], 17)
            self.assert_post_fork_facts(meta)
            self.assertEqual(meta['old_copy']['cluster_state'], 'shut down in recovery')
            self.assertTrue(meta['checks']['old_copy_is_stopped_standby']['ok'])
            flush = meta['checks']['diverged_pages_on_disk_after_fork']['flush']
            self.assertEqual(flush['flushed_on'], 'follower')
            self.assertTrue(flush['clean'])
            # The flush ran no restartpoint on the standby.
            self.assertEqual(flush['checkpoint_lsn_before_flush'],
                             flush['checkpoint_lsn_after_flush'])
            # Q replayed the diverged writes, so its minimum recovery point lies past them.
            observations = meta['observations']
            self.assertGreaterEqual(lsnscan.parse_lsn(observations['old_copy_min_recovery_lsn']),
                                    lsnscan.parse_lsn(observations['old_primary_end_lsn']))
            self.assertEqual(meta['timelines'], {'old_copy': 1, 'source_backup': 2})
            # P was discarded: only R (the source) is kept.
            work = Path(tmp, 'scenarios', scenarios.BY_ID['S2c'].dirname,
                        meta['source_cluster']['datadir']).parent
            self.assertEqual(sorted(p.name for p in work.iterdir() if p.name != 'logs'),
                             ['standby'])
            self.assertEqual(self.wal_keep_mb(tmp, 'S2c')[0], {'old_copy': '0', 'source': '1024'})
            rewind = runs[('S2c', 'pg_rewind')]
            self.assertEqual((rewind['outcome'], rewind['verdict'], rewind['arm']),
                             ('accepted', 'correct', 'default'),
                             self.result(tmp, 'S2c', 'pg_rewind'))
            self.assertEqual(rewind['diverged_pages_left'], 0)
            control = runs[('S2c', 'lsn_skip')]
            self.assertEqual(control['verdict'], 'wrong_data')
            self.assertGreater(control['diverged_pages_left'], 0)
            self.assertGreaterEqual(control['diverged_before_recovery'],
                                    control['diverged_pages_left'])
            self.assertEqual(control['other_after_recovery'], 0)
            self.assert_counted_before_amcheck(self.result(tmp, 'S2c', 'lsn_skip'))
            # pgdelta's restore path: recovery ends, CHECKPOINT, a clean stop, the count.
            pgdelta = self.result(tmp, 'S2c', 'pgdelta')
            self.assertEqual((pgdelta['outcome'], pgdelta['verdict']), ('accepted', 'correct'),
                             pgdelta)
            self.assertTrue(pgdelta['bring_up']['checkpointed']
                            and pgdelta['bring_up']['stopped_cleanly'])
            self.assertNotIn('after_recovery_error', pgdelta['bring_up'])
            after = pgdelta['diverged_after_recovery']
            self.assertEqual((after['diverged_left'], after['other']), (0, 0))
            self.assertEqual(after['listed'], meta['observations']['post_fork_page_count'])
            self.assertIn('before', after['counted_after'])
            self.assertTrue(pgdelta['logical_match'] and pgdelta['amcheck_ok'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
