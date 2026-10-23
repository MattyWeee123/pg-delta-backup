"""Tests for the change-pattern study (experiment 1).

Everything except the final integration test runs without PostgreSQL.
"""
from contextlib import redirect_stderr, redirect_stdout
import csv
import io
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from experiments.change_patterns import pagediff, report, run_study, tracker, workloads
from experiments.change_patterns.pagediff import BLOCK_SIZE, FileDiff

PG17_BIN = Path('/opt/homebrew/opt/postgresql@17/bin')
ORDER = '<' if sys.byteorder == 'little' else '>'


def block(fill: int) -> bytes:
    return bytes([fill]) * BLOCK_SIZE


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, relative: str, data: bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


class ChangedBlockTests(TempDirTest):
    def compare(self, old: bytes | None, new: bytes | None, **kwargs):
        old_path = self.write('old', old) if old is not None else None
        new_path = self.write('new', new) if new is not None else None
        return pagediff.changed_blocks(old_path, new_path, **kwargs)

    def test_identical_files_have_no_changes(self):
        data = b''.join(block(i) for i in range(5))
        self.assertEqual(self.compare(data, data), (len(data), len(data), ()))

    def test_single_byte_change_marks_only_its_block(self):
        old = b''.join(block(i) for i in range(6))
        new = bytearray(old)
        new[3 * BLOCK_SIZE + 17] ^= 0xFF
        self.assertEqual(self.compare(old, bytes(new))[2], (3,))

    def test_growth_counts_partial_and_new_blocks(self):
        old = block(1) * 2
        new = old + block(2) + b'x' * 100
        old_size, new_size, changed = self.compare(old, new)
        self.assertEqual((old_size, new_size, changed), (2 * BLOCK_SIZE, 3 * BLOCK_SIZE + 100, (2, 3)))
        diff = FileDiff('f', old_size, new_size, changed)
        self.assertEqual((diff.blocks, diff.new_blocks), (4, 4))

    def test_truncation_counts_blocks_beyond_new_end(self):
        old = block(1) * 4
        self.assertEqual(self.compare(old, old[:BLOCK_SIZE + 10])[2], (1, 2, 3))

    def test_partial_last_block(self):
        old = block(1) + b'abc'
        self.assertEqual(self.compare(old, old)[2], ())
        self.assertEqual(self.compare(old, block(1) + b'abd')[2], (1,))
        self.assertEqual(self.compare(old, block(1) + b'ab')[2], (1,))

    def test_missing_sides(self):
        data = block(7) * 3
        self.assertEqual(self.compare(None, data), (None, 3 * BLOCK_SIZE, (0, 1, 2)))
        self.assertEqual(self.compare(data, None), (3 * BLOCK_SIZE, None, (0, 1, 2)))
        self.assertEqual(self.compare(b'', b''), (0, 0, ()))
        self.assertEqual(self.compare(None, b''), (None, 0, ()))

    def test_changes_across_read_windows(self):
        old = b''.join(block(i) for i in range(9))
        new = bytearray(old)
        for index in (0, 3, 4, 8):
            new[index * BLOCK_SIZE] ^= 1
        result = self.compare(old, bytes(new) + b'tail', window=2 * BLOCK_SIZE)
        self.assertEqual(result[2], (0, 3, 4, 8, 9))

    def test_window_must_be_block_multiple(self):
        with self.assertRaises(ValueError):
            self.compare(b'a', b'a', window=1000)


class ChunkAggregationTests(unittest.TestCase):
    def test_larger_chunks_derive_from_8k_blocks(self):
        diff = FileDiff('f', 200 * BLOCK_SIZE, 200 * BLOCK_SIZE, (0, 1, 9, 130))
        stats = pagediff.chunk_stats(diff)
        self.assertEqual(stats[8192]['changed'], 4)
        self.assertEqual(stats[16384]['changed'], 3)   # chunks 0, 4, 65
        self.assertEqual(stats[65536]['changed'], 3)   # chunks 0, 1, 16
        self.assertEqual(stats[1048576]['changed'], 2)  # chunks 0, 1
        self.assertEqual(stats[1048576]['chunks'], 2)
        self.assertEqual(stats[8192]['chunks'], 200)
        self.assertEqual(stats[8192]['send_bytes'], 4 * BLOCK_SIZE)
        self.assertEqual(stats[1048576]['send_bytes'], 200 * BLOCK_SIZE)

    def test_send_bytes_count_only_the_new_side(self):
        shrunk = FileDiff('f', 4 * BLOCK_SIZE, BLOCK_SIZE + 10, (1, 2, 3))
        stats = pagediff.chunk_stats(shrunk, (8192, 32768))
        self.assertEqual(stats[8192]['send_bytes'], 10)
        self.assertEqual(stats[8192]['new_chunks'], 2)
        self.assertEqual(stats[32768], {'chunks': 1, 'changed': 1, 'new_chunks': 1,
                                        'send_bytes': BLOCK_SIZE + 10})
        deleted = FileDiff('g', 3 * BLOCK_SIZE, None, (0, 1, 2))
        self.assertEqual(pagediff.chunk_stats(deleted, (8192,))[8192]['send_bytes'], 0)

    def test_rejects_unaligned_chunk_size(self):
        with self.assertRaises(ValueError):
            pagediff.chunk_stats(FileDiff('f', 0, 0, ()), (4096,))

    def test_totals_and_fraction(self):
        totals = pagediff.empty_totals((8192, 1048576))
        for diff in (FileDiff('a', 4 * BLOCK_SIZE, 4 * BLOCK_SIZE, (1,)),
                     FileDiff('b', 4 * BLOCK_SIZE, 4 * BLOCK_SIZE, ())):
            pagediff.add_totals(totals, diff, pagediff.chunk_stats(diff, (8192, 1048576)))
        pagediff.finish_totals(totals)
        self.assertEqual((totals['files'], totals['files_changed']), (2, 1))
        self.assertEqual(totals['chunks']['8K']['changed_fraction'], 1 / 8)
        self.assertEqual(totals['chunks']['1M']['changed_fraction'], 1 / 2)
        self.assertEqual(totals['chunks']['8K']['send_fraction'], 1 / 8)
        self.assertEqual(totals['chunks']['1M']['send_fraction'], 1 / 2)
        self.assertIsNone(pagediff.finish_totals(pagediff.empty_totals())['chunks']['8K']
                          ['changed_fraction'])

    def test_chunk_labels(self):
        self.assertEqual([pagediff.chunk_label(s) for s in pagediff.CHUNK_SIZES],
                         ['8K', '16K', '32K', '64K', '128K', '256K', '512K', '1M'])


RELMAP = {
    'base/5/16384': {'oid': 16384, 'name': 'public.accounts', 'relkind': 'r', 'parent': None},
    'base/5/16390': {'oid': 16390, 'name': 'public.accounts_pkey', 'relkind': 'i',
                     'parent': 'public.accounts'},
    'base/5/16387': {'oid': 16387, 'name': 'pg_toast.pg_toast_16384', 'relkind': 't',
                     'parent': 'public.accounts'},
    'base/5/1259': {'oid': 1259, 'name': 'pg_catalog.pg_class', 'relkind': 'r', 'parent': None},
    'global/1262': {'oid': 1262, 'name': 'pg_catalog.pg_database', 'relkind': 'r',
                    'parent': None},
}


class ClassifyTests(unittest.TestCase):
    def check(self, path, **expected):
        info = pagediff.classify(path, RELMAP)
        self.assertEqual({key: info[key] for key in expected}, expected, path)

    def test_relation_forks_segments_and_owners(self):
        self.check('base/5/16384', area='relation', kind='heap', owner='user',
                   relation='public.accounts', fork='main', segno=0)
        self.check('base/5/16384.3', kind='heap', segno=3)
        self.check('base/5/16384_fsm', kind='fsm', fork='fsm')
        self.check('base/5/16384_vm.1', kind='vm', segno=1)
        self.check('base/5/16384_init', kind='init')
        self.check('base/5/16390', kind='index', owner='user')
        self.check('base/5/16387', kind='toast', owner='user')
        self.check('base/5/1259', kind='heap', owner='catalog')
        self.check('global/1262', kind='heap', owner='catalog', relation='pg_catalog.pg_database')
        self.check('base/5/99999', kind='unmapped', owner='unknown', relation=None)
        self.check('base/5/99999_vm', kind='vm', owner='unknown')

    def test_non_relation_files(self):
        self.check('global/pg_control', area='other', kind='non_relation')
        self.check('base/5/PG_VERSION', area='other')
        self.check('base/5/pg_filenode.map', area='other')
        self.check('pg_wal/000000010000000000000003', area='wal')
        self.check('backup_manifest', area='backup_metadata')
        self.check('backup_label', area='backup_metadata')
        self.check('pg_xact/0000', area='other')
        self.check('base/5/INCREMENTAL.16384', area='other')


class CompareTreeTests(TempDirTest):
    def test_tree_comparison_and_summary(self):
        old, new = self.root / 'B0', self.root / 'B1'
        for side in (old, new):
            self.write(f'{side.name}/base/5/16390', block(1) * 2)  # unchanged index
            self.write(f'{side.name}/global/pg_control', b'control')
        self.write('B0/base/5/16384', block(1) * 3)
        self.write('B1/base/5/16384', block(1) + block(9) + block(1) + block(4))  # 1 changed, 1 new
        self.write('B0/base/5/1259', block(5))
        self.write('B1/base/5/1259', block(6))
        self.write('B0/pg_wal/000000010000000000000003', block(0) * 2)
        self.write('B1/pg_wal/000000010000000000000005', block(0) * 2)
        self.write('B1/backup_manifest', b'{}')
        self.write('B0/backup_manifest', b'[]')
        self.write('B1/base/5/16384_fsm', block(2))
        diffs, skipped = pagediff.compare_trees(old, new)
        self.assertEqual(skipped, [])
        by_path = {d.path: d for d in diffs}
        self.assertEqual(by_path['base/5/16384'].changed, (1, 3))
        self.assertEqual(by_path['base/5/16390'].changed, ())
        self.assertIsNone(by_path['base/5/16384_fsm'].old_size)
        summary = pagediff.summarize(diffs, RELMAP)
        data = summary['totals']['data']['chunks']['8K']
        # data excludes pg_wal and backup_manifest: 4 + 2 + 1 + 1 + 1 blocks
        self.assertEqual((data['changed'], data['chunks']), (4, 9))
        everything = summary['totals']['all']['chunks']['8K']
        self.assertEqual((everything['changed'], everything['chunks']), (9, 14))
        self.assertEqual(summary['by_kind']['heap']['chunks']['8K']['changed'], 3)
        self.assertEqual(summary['by_kind']['index']['chunks']['8K']['changed'], 0)
        self.assertEqual(summary['by_owner']['catalog']['chunks']['8K']['changed'], 1)
        self.assertEqual(summary['by_area']['wal']['files'], 2)
        self.assertEqual(summary['by_directory']['base']['files'], 4)
        self.assertEqual(summary['by_relation']['public.accounts']['chunks']['8K']['changed'], 2)
        self.assertIn('public.accounts[fsm]', summary['by_relation'])
        self.assertIn('public.accounts_pkey', summary['by_relation'])  # user, unchanged
        self.assertEqual(summary['only_in_new'], ['base/5/16384_fsm',
                                                  'pg_wal/000000010000000000000005'])
        self.assertEqual(summary['only_in_old'], ['pg_wal/000000010000000000000003'])
        changed_paths = [f['path'] for f in summary['changed_files']]
        self.assertNotIn('base/5/16390', changed_paths)
        self.assertIn('base/5/1259', changed_paths)
        json.dumps(summary)  # must be serialisable as-is

    def test_unmapped_relations_are_labelled_by_path(self):
        diffs = [FileDiff('base/5/20000_vm.1', None, BLOCK_SIZE, (0,))]
        summary = pagediff.summarize(diffs, RELMAP)
        self.assertEqual(list(summary['by_relation']), ['base/5/20000[vm]'])
        self.assertEqual(list(summary['by_owner']), ['unknown'])

    def test_symlinks_are_skipped_not_followed(self):
        old, new = self.root / 'B0', self.root / 'B1'
        self.write('B0/a', b'1')
        self.write('B1/a', b'1')
        self.write('elsewhere/secret', b'x')
        (new / 'link').symlink_to(self.root / 'elsewhere')
        diffs, skipped = pagediff.compare_trees(old, new)
        self.assertEqual([d.path for d in diffs], ['a'])
        self.assertEqual(skipped, ['link'])

    def test_symlinked_file_on_one_side_counts_as_absent(self):
        # Regression: diff_file used Path.is_file(), which follows the link and
        # read the out-of-tree target while also listing the path as skipped.
        self.write('B0/base/5/16384', block(1) * 2)
        self.write('outside/target', block(9) * 3)
        (self.root / 'B1/base/5').mkdir(parents=True)
        (self.root / 'B1/base/5/16384').symlink_to(self.root / 'outside/target')
        diffs, skipped = pagediff.compare_trees(self.root / 'B0', self.root / 'B1')
        self.assertEqual(diffs, [FileDiff('base/5/16384', 2 * BLOCK_SIZE, None, (0, 1))])
        self.assertEqual(skipped, ['base/5/16384'])

    def test_file_under_a_symlinked_directory_counts_as_absent(self):
        self.write('B0/base/5/16384', block(1))
        self.write('outside/5/16384', block(9))
        (self.root / 'B1/base').mkdir(parents=True)
        (self.root / 'B1/base/5').symlink_to(self.root / 'outside/5')
        diffs, skipped = pagediff.compare_trees(self.root / 'B0', self.root / 'B1')
        self.assertEqual(diffs, [FileDiff('base/5/16384', BLOCK_SIZE, None, (0,))])
        self.assertEqual(skipped, ['base/5'])

    def test_diff_file_sides_must_be_regular_files(self):
        self.write('B0/f', block(1))
        (self.root / 'B1/f').mkdir(parents=True)  # a directory where B0 has a file
        self.assertEqual(pagediff.diff_file('f', self.root / 'B0', self.root / 'B1'),
                         FileDiff('f', BLOCK_SIZE, None, (0,)))
        self.assertIsNone(pagediff.regular_file(self.root / 'B0', 'missing/f'))
        self.assertIsNone(pagediff.regular_file(self.root / 'B0', 'f/below-a-file'))


class WorkloadTests(unittest.TestCase):
    def test_fraction_to_rows(self):
        self.assertEqual(workloads.rows_for_fraction(0.01, 100000), 1000)
        self.assertEqual(workloads.rows_for_fraction(0.001, 1000000), 1000)
        self.assertEqual(workloads.rows_for_fraction(1, 7), 7)
        for bad in (0, -0.1, 1.5):
            with self.assertRaises(ValueError):
                workloads.rows_for_fraction(bad, 100)
        with self.assertRaises(ValueError):
            workloads.rows_for_fraction(0.001, 100)

    def test_target_rows_uses_the_modified_table(self):
        sizes = {'accounts_rows': 100000, 'events_rows': 5000}
        self.assertEqual(workloads.target_rows('uniform', 0.1, **sizes), 10000)
        self.assertEqual(workloads.target_rows('tpcb', 0.1, **sizes), 10000)
        self.assertEqual(workloads.target_rows('append', 0.1, **sizes), 500)
        self.assertEqual(workloads.target_rows('retention', 0.1, **sizes), 500)
        with self.assertRaises(ValueError):
            workloads.target_rows('zipf', 0.1, **sizes)

    def test_uniform_sample_is_deterministic_distinct_and_in_range(self):
        first = workloads.select_aids('uniform', 5000, 100000, seed=7)
        self.assertEqual(first, workloads.select_aids('uniform', 5000, 100000, seed=7))
        self.assertNotEqual(first, workloads.select_aids('uniform', 5000, 100000, seed=8))
        self.assertEqual(len(set(first)), 5000)
        self.assertTrue(all(1 <= aid <= 100000 for aid in first))
        # Spread across the table rather than bunched at one end.
        self.assertGreater(max(first) - min(first), 90000)

    def test_hot_set_bounds(self):
        aids = workloads.select_aids('hot', 1000, 100000, seed=1, hot_percent=1.0)
        self.assertEqual(len(set(aids)), 1000)  # k == hot set size is allowed
        self.assertEqual(set(aids), set(range(1, 1001)))
        some = workloads.select_aids('hot', 50, 100000, seed=1, hot_percent=2.5)
        self.assertTrue(all(1 <= aid <= 2500 for aid in some))
        with self.assertRaisesRegex(ValueError, 'hot set'):
            workloads.select_aids('hot', 1001, 100000, seed=1, hot_percent=1.0)
        with self.assertRaises(ValueError):
            workloads.hot_set_size(100, 0)

    def test_clustered_rows_are_consecutive(self):
        aids = workloads.select_aids('clustered', 300, 1000, seed=3)
        self.assertEqual(aids, list(range(aids[0], aids[0] + 300)))
        self.assertTrue(1 <= aids[0] and aids[-1] <= 1000)
        self.assertEqual(workloads.select_aids('clustered', 1000, 1000, seed=3),
                         list(range(1, 1001)))

    def test_too_many_rows_rejected(self):
        with self.assertRaises(ValueError):
            workloads.select_aids('uniform', 11, 10, seed=1)

    def test_update_statements_touch_exactly_k_distinct_rows(self):
        statements = list(workloads.workload_statements(
            'uniform', 250, accounts_rows=1000, events_rows=1000, seed=5))
        aids = [int(re.fullmatch(r'UPDATE pgbench_accounts SET abalance = abalance \+ 1 '
                                 r'WHERE aid = (\d+);', s).group(1)) for s in statements]
        self.assertEqual(len(aids), 250)
        self.assertEqual(len(set(aids)), 250)
        self.assertEqual(aids, workloads.select_aids('uniform', 250, 1000, seed=5))

    def test_append_and_retention_statements(self):
        appended = list(workloads.workload_statements(
            'append', 3, accounts_rows=10, events_rows=100, seed=1))
        self.assertEqual(len(appended), 3)
        self.assertTrue(all(s.startswith('INSERT INTO events') for s in appended))
        self.assertIn('2026-01-01T00:01:41+00:00', appended[0])  # follows prefill id 100
        retention = list(workloads.workload_statements(
            'retention', 3, accounts_rows=10, events_rows=100, seed=1))
        self.assertEqual(retention[0::2], appended)  # same seed, same inserts
        self.assertEqual(retention[1::2], [f'DELETE FROM events WHERE id = {i};'
                                           for i in (1, 2, 3)])
        with self.assertRaises(ValueError):
            list(workloads.workload_statements('retention', 101, accounts_rows=10,
                                               events_rows=100, seed=1))
        with self.assertRaises(ValueError):
            list(workloads.workload_statements('tpcb', 1, accounts_rows=10,
                                               events_rows=10, seed=1))

    def test_batching(self):
        lines = list(workloads.batched([f's{i};' for i in range(250)], 100))
        self.assertEqual(lines.count('BEGIN;'), 3)
        self.assertEqual(lines.count('COMMIT;'), 3)
        self.assertEqual(lines[0], 'BEGIN;')
        self.assertEqual(lines[101], 'COMMIT;')
        self.assertEqual(lines[-1], 'COMMIT;')
        self.assertEqual(list(workloads.batched([], 100)), [])
        with self.assertRaises(ValueError):
            list(workloads.batched(['x;'], 0))

    def test_script_is_deterministic(self):
        with tempfile.TemporaryDirectory() as root:
            summaries = []
            for name in ('a.sql', 'b.sql'):
                path = Path(root) / name
                summaries.append(workloads.write_script(path, workloads.workload_statements(
                    'hot', 120, accounts_rows=100000, events_rows=10, seed=11), 50))
            self.assertEqual(summaries[0], summaries[1])
            self.assertEqual(summaries[0]['statements'], 120)
            self.assertEqual(summaries[0]['transactions'], 3)
            self.assertEqual((Path(root) / 'a.sql').read_text(),
                             (Path(root) / 'b.sql').read_text())

    def test_prefill_sql(self):
        sql = workloads.events_prefill_sql(42)
        self.assertIn('generate_series(1, 42)', sql)


def incremental_bytes(blocks, truncation=0, *, count=None, magic=tracker.INCREMENTAL_MAGIC,
                      pad=True, data=True):
    count = len(blocks) if count is None else count
    header = struct.pack(f'{ORDER}3I{len(blocks)}I', magic, count, truncation, *blocks)
    if pad and blocks and len(header) % BLOCK_SIZE:
        header += bytes(BLOCK_SIZE - len(header) % BLOCK_SIZE)
    return header + (b''.join(block(n % 256) for n in blocks) if data else b'')


class IncrementalParserTests(TempDirTest):
    def test_header_with_blocks_is_padded_to_block_size(self):
        data = incremental_bytes([0, 5, 131071], truncation=9)
        header = tracker.parse_incremental_header(data, file_size=len(data))
        self.assertEqual(header.blocks, (0, 5, 131071))
        self.assertEqual(header.num_blocks, 3)
        self.assertEqual(header.truncation_block_length, 9)
        self.assertEqual(header.header_length, BLOCK_SIZE)
        self.assertEqual(header.expected_size, 4 * BLOCK_SIZE)

    def test_zero_block_file_has_no_padding(self):
        data = incremental_bytes([], truncation=2)
        self.assertEqual(len(data), 12)
        header = tracker.parse_incremental_header(data, file_size=12)
        self.assertEqual((header.blocks, header.header_length), ((), 12))

    def test_long_block_list_spills_into_second_header_block(self):
        blocks = list(range(0, 4094, 2))  # 2047 entries: 12 + 8188 bytes
        header = tracker.parse_incremental_header(incremental_bytes(blocks, data=False))
        self.assertEqual(header.header_length, 2 * BLOCK_SIZE)
        self.assertEqual(tracker.incremental_header_length(2045), BLOCK_SIZE)

    def test_bad_magic(self):
        with self.assertRaisesRegex(tracker.IncrementalFormatError, 'bad magic'):
            tracker.parse_incremental_header(incremental_bytes([], magic=0x12345678))
        swapped = struct.unpack('<I', struct.pack('>I', tracker.INCREMENTAL_MAGIC))[0]
        with self.assertRaisesRegex(tracker.IncrementalFormatError, 'byte-swapped'):
            tracker.parse_incremental_header(incremental_bytes([], magic=swapped))

    def test_structural_errors(self):
        cases = {
            'truncated header': incremental_bytes([])[:7],
            'block count': incremental_bytes([], count=131073),
            'truncation block length': incremental_bytes([], truncation=131073),
            'block list truncated': incremental_bytes([1, 2], count=3, pad=False, data=False),
            'strictly increasing': incremental_bytes([4, 4]),
            'outside': incremental_bytes([131072]),
        }
        cases['strictly increasing (unsorted)'] = incremental_bytes([9, 3])
        for message, data in cases.items():
            with self.subTest(message), self.assertRaisesRegex(
                    tracker.IncrementalFormatError, message.split(' (')[0]):
                tracker.parse_incremental_header(data)

    def test_size_mismatch(self):
        data = incremental_bytes([1, 2])
        with self.assertRaisesRegex(tracker.IncrementalFormatError, 'does not match'):
            tracker.parse_incremental_header(data, file_size=len(data) - 1)

    def test_read_and_scan_backup_directory(self):
        incremental = incremental_bytes([2, 7], truncation=8)
        self.write('I1/base/5/INCREMENTAL.16384', incremental)
        self.write('I1/base/5/INCREMENTAL.16384_vm.1', incremental_bytes([], truncation=1))
        self.write('I1/base/5/16384_fsm', block(3) * 3)
        self.write('I1/backup_label', b'label')
        header = tracker.read_incremental_header(self.root / 'I1/base/5/INCREMENTAL.16384')
        self.assertEqual(header.blocks, (2, 7))
        scanned = tracker.scan_incremental_backup(self.root / 'I1')
        self.assertEqual(scanned['base/5/16384'],
                         tracker.TrackedFile('incremental', len(incremental), (2, 7), 8))
        self.assertEqual(scanned['base/5/16384_vm.1'].blocks, ())
        self.assertEqual(scanned['base/5/16384_fsm'], tracker.TrackedFile('full', 3 * BLOCK_SIZE))
        self.assertEqual(scanned['backup_label'].method, 'full')
        summary = tracker.summarize_tracked(scanned, RELMAP)
        self.assertEqual(summary['incremental_files']['files'], 2)
        self.assertEqual(summary['incremental_files']['blocks_listed'], 2)
        self.assertEqual(summary['full_files']['relation_files_by_kind'],
                         {'fsm': {'files': 1, 'bytes': 3 * BLOCK_SIZE}})
        self.assertEqual(summary['full_files']['non_relation'], {'files': 1, 'bytes': 5})
        self.assertEqual(summary['total_bytes'], len(incremental) + 12 + 3 * BLOCK_SIZE + 5)
        sets = tracker.tracked_sets_from_backup(scanned)
        self.assertEqual(sets['base/5/16384'], ('incremental', {2, 7}))
        self.assertEqual(sets['base/5/16384_fsm'], ('full', {0, 1, 2}))
        self.assertNotIn('backup_label', sets)

    def test_summary_keeps_each_incremental_files_block_list(self):
        # Regression: the parsed block lists were thrown away; only totals were kept.
        listed = [*range(0, 13), 40, *range(77, 81)]
        incremental = incremental_bytes(listed, truncation=81)
        self.write('I1/base/5/INCREMENTAL.16384', incremental)
        self.write('I1/base/5/INCREMENTAL.16390.1', incremental_bytes([], truncation=0))
        self.write('I1/base/5/16384_fsm', block(3))
        summary = tracker.summarize_tracked(tracker.scan_incremental_backup(self.root / 'I1'),
                                            RELMAP)
        self.assertEqual(summary['incremental_files']['by_file'], {
            'base/5/16384': {'size': len(incremental), 'truncation_block_length': 81,
                             'blocks': ['0-12', '40', '77-80']},
            'base/5/16390.1': {'size': 12, 'truncation_block_length': 0, 'blocks': []},
        })
        self.assertEqual(summary['incremental_files']['blocks_listed'], len(listed))
        entry = json.loads(json.dumps(summary))['incremental_files']['by_file']['base/5/16384']
        self.assertEqual(tracker.expand_ranges(entry['blocks']), listed)

    def test_block_ranges_round_trip(self):
        cases = {(): [], (5,): ['5'], (1, 2): ['1-2'], (0, 2, 3, 4, 9): ['0', '2-4', '9'],
                 (131071,): ['131071']}
        for numbers, ranges in cases.items():
            with self.subTest(numbers):
                self.assertEqual(tracker.compress_ranges(numbers), ranges)
                self.assertEqual(tracker.expand_ranges(ranges), list(numbers))
        self.assertEqual(tracker.compress_ranges([4, 2, 3, 3]), ['2-4'])  # sorted, deduplicated
        for bad in (['3-1'], ['x'], ['1-'], ['-1']):
            with self.subTest(bad), self.assertRaises(ValueError):
                tracker.expand_ranges(bad)

    def test_corrupt_file_in_backup_is_reported(self):
        self.write('I1/base/5/INCREMENTAL.16384', b'\0' * 12)
        with self.assertRaisesRegex(tracker.IncrementalFormatError, 'INCREMENTAL.16384'):
            tracker.scan_incremental_backup(self.root / 'I1')

    def test_original_path(self):
        self.assertEqual(tracker.original_path('base/5/INCREMENTAL.16384.2'), 'base/5/16384.2')
        self.assertEqual(tracker.original_path('global/INCREMENTAL.1262_vm'), 'global/1262_vm')
        self.assertIsNone(tracker.original_path('base/5/16384'))


class WalSummaryTests(unittest.TestCase):
    def test_parse_blocks_ranges_and_limits(self):
        text = ('TS 1663, DB 5, REL 16384, FORK main: limit 0\n'
                'TS 1663, DB 5, REL 16384, FORK main: block 3\n'
                'TS 1663, DB 5, REL 16384, FORK main: blocks 10..12\n'
                'TS 1663, DB 5, REL 16384, FORK vm: block 0\n'
                'TS 1664, DB 0, REL 1262, FORK main: block 131073\n'
                'TS 1663, DB 5, REL 16384, FORK main: limit 4\n\n')
        blocks, limits = tracker.parse_walsummary(text)
        self.assertEqual(blocks[(1663, 5, 16384, 'main')], {3, 10, 11, 12})
        self.assertEqual(limits[(1663, 5, 16384, 'main')], 0)
        by_file, skipped = tracker.summary_blocks_by_file(
            {**blocks, (16500, 5, 1, 'main'): {1, 2}})
        self.assertEqual(by_file['base/5/16384'], {3, 10, 11, 12})
        self.assertEqual(by_file['base/5/16384_vm'], {0})
        self.assertEqual(by_file['global/1262.1'], {1})
        self.assertEqual(skipped, 2)

    def test_malformed_output_is_rejected(self):
        for line in ('TS 1663, DB 5: block 1', 'TS 1663, DB 5, REL 1, FORK main: blocks 5..2'):
            with self.subTest(line), self.assertRaises(ValueError):
                tracker.parse_walsummary(line)

    def test_names_and_lsns(self):
        self.assertEqual(tracker.summary_file_range(
            '0000000100000000017630E800000000017631E8.summary'), (1, 0x17630E8, 0x17631E8))
        with self.assertRaises(ValueError):
            tracker.summary_file_range('junk.summary')
        self.assertEqual(tracker.parse_lsn('1/3000028'), (1 << 32) | 0x3000028)
        self.assertEqual(run_study.format_lsn((1 << 32) | 0x3000028), '1/3000028')


def summary_name(start: int, end: int, timeline: int = 1) -> str:
    return f'{timeline:08X}{start:016X}{end:016X}.summary'


class WalSummaryViewTests(TempDirTest):
    """Trial.walsummary_view with pg_walsummary replaced by a stub."""

    def view(self, b0_start, i1_start):
        trial = object.__new__(run_study.Trial)  # no cluster: only data and run are used
        trial.data = self.root / 'data'
        self.calls = []

        def run(name, args, **kwargs):
            self.calls.append((name, [Path(arg).name for arg in args]))
            return 'TS 1663, DB 5, REL 16384, FORK main: block 3\n'
        trial.run = run
        return trial.walsummary_view(b0_start, i1_start)

    def test_temporary_and_foreign_names_are_ignored_and_recorded(self):
        # Regression: glob('*.summary') matched temp.summary, the summarizer's
        # in-progress file, and summary_file_range raised on its name.
        covering, later = summary_name(0x100, 0x200), summary_name(0x900, 0xA00)
        for name in (covering, later, 'temp.summary', 'notes.txt'):
            self.write(f'data/pg_wal/summaries/{name}', b'')
        (self.root / f'data/pg_wal/summaries/{summary_name(0x150, 0x160)}').mkdir()
        view = self.view(0x150, 0x800)
        self.assertEqual(self.calls, [('pg_walsummary', [covering])])
        self.assertEqual(view['files'], 1)
        self.assertEqual(view['ignored_files'],
                         sorted(['notes.txt', summary_name(0x150, 0x160), 'temp.summary']))
        self.assertEqual(view['by_file'], {'base/5/16384': {3}})

    def test_only_ignored_files(self):
        self.write('data/pg_wal/summaries/temp.summary', b'partial')
        view = self.view(0x150, 0x800)
        self.assertEqual(self.calls, [])
        self.assertEqual((view['files'], view['ignored_files']), (0, ['temp.summary']))


class AgreementTests(unittest.TestCase):
    def test_categories_scopes_and_examples(self):
        diffs = [
            FileDiff('base/5/16384', 10 * BLOCK_SIZE, 10 * BLOCK_SIZE, (1, 2, 3)),
            FileDiff('base/5/16384_fsm', 3 * BLOCK_SIZE, 3 * BLOCK_SIZE, (0,)),
            FileDiff('base/5/16390', 4 * BLOCK_SIZE, 2 * BLOCK_SIZE, (2, 3)),  # truncated
            FileDiff('global/pg_control', 100, 100, (0,)),  # not a relation file
        ]
        sets = {
            'base/5/16384': ('incremental', {2, 3, 4, 12}),
            'base/5/16384_fsm': ('full', {0, 1, 2}),
            'base/5/16390': ('incremental', set()),
        }
        result = tracker.agreement(diffs, sets, RELMAP)
        everything = result['scopes']['all']
        self.assertEqual(everything['files'], 3)
        self.assertEqual(everything['changed_and_tracked'], 3)   # heap 2, 3; fsm 0
        self.assertEqual(everything['changed_not_tracked'], 1)   # heap 1
        self.assertEqual(everything['tracked_unchanged'], 3)     # heap 4; fsm 1, 2
        self.assertEqual(everything['tracked_beyond_new_end'], 1)  # heap 12
        incremental = result['scopes']['incremental']
        self.assertEqual((incremental['files'], incremental['changed_not_tracked'],
                          incremental['tracked_unchanged']), (2, 1, 1))
        self.assertEqual(result['scopes']['full']['reasons'], {'fsm_fork': 1})
        self.assertEqual(everything['by_kind']['fsm']['tracked_unchanged'], 2)
        self.assertEqual(result['examples']['incremental']['changed_not_tracked'],
                         [{'file': 'base/5/16384', 'block': 1, 'kind': 'heap',
                           'relation': 'public.accounts'}])

    def test_untracked_files_and_example_limit(self):
        diffs = [FileDiff(f'base/5/{node}', 50 * BLOCK_SIZE, 50 * BLOCK_SIZE, tuple(range(50)))
                 for node in (20000, 20001, 20002)]
        result = tracker.agreement(diffs, {}, RELMAP, max_examples=7)
        self.assertEqual(result['scopes']['absent']['changed_not_tracked'], 150)
        examples = result['examples']['absent']['changed_not_tracked']
        self.assertEqual(len(examples), 7)
        # Round-robin: one block from each file before a second from any.
        self.assertEqual([e['file'][-5:] for e in examples[:3]], ['20000', '20001', '20002'])
        self.assertEqual(examples[0]['kind'], 'unmapped')


ACCOUNTS_RELMAP = {
    'base/5/16397': {'oid': 16397, 'name': 'public.pgbench_accounts', 'relkind': 'r',
                     'parent': None},
    'base/5/16410': {'oid': 16410, 'name': 'public.pgbench_accounts_pkey', 'relkind': 'i',
                     'parent': 'public.pgbench_accounts'},
}


class PagesPerRowTests(unittest.TestCase):
    def pages_per_row(self, diffs, k, *, b0_pages=100, rows=6100):
        content = pagediff.summarize(diffs, ACCOUNTS_RELMAP)
        before = {'relations': {'pgbench_accounts': {'main_bytes': b0_pages * BLOCK_SIZE}},
                  'row_counts': {'pgbench_accounts': rows}}
        result = run_study.pages_per_row({'pattern': 'uniform'}, {'rows_changed': k}, k, before,
                                         content, diffs, ACCOUNTS_RELMAP)
        json.dumps(result, allow_nan=False)
        return result

    def test_existing_and_appended_heap_pages_are_reported_separately(self):
        # Regression: the headline heap percentage counted pages appended for
        # new row versions in its numerator and denominator, yet was compared
        # with a model that predicts only existing pages touched.
        diffs = [
            # 100 B0 pages, 40 of them changed, plus 10 appended pages.
            FileDiff('base/5/16397', 100 * BLOCK_SIZE, 110 * BLOCK_SIZE,
                     (*range(0, 80, 2), *range(100, 110))),
            FileDiff('base/5/16397_fsm', 3 * BLOCK_SIZE, 3 * BLOCK_SIZE, (0, 1, 2)),  # not main
            FileDiff('base/5/16410', 20 * BLOCK_SIZE, 20 * BLOCK_SIZE, tuple(range(15))),
        ]
        result = self.pages_per_row(diffs, 50)
        self.assertEqual((result['heap_pages_existing'], result['heap_pages_changed_existing'],
                          result['heap_appended_pages']), (100, 40, 10))
        self.assertEqual(result['heap_existing_changed_pct'], 40.0)
        self.assertEqual((result['heap_pages_changed'], result['heap_pages_compared']), (50, 110))
        self.assertAlmostEqual(result['heap_overall_changed_pct'], 100 * 50 / 110)
        # The model is computed over the same existing pages as the figure it is compared with.
        self.assertAlmostEqual(result['uniform_model_heap_existing_pct'],
                               100 * (1 - (1 - 1 / 100) ** 50))
        self.assertEqual(result['index_changed_pct'], 75.0)
        self.assertEqual(result['rows_per_heap_page_before'], 61.0)

    def test_truncated_pages_are_existing_and_new_segments_are_appended(self):
        shrunk = self.pages_per_row([FileDiff('base/5/16397', 100 * BLOCK_SIZE, 90 * BLOCK_SIZE,
                                              (*range(5), *range(90, 100)))], 5)
        self.assertEqual((shrunk['heap_pages_changed_existing'], shrunk['heap_appended_pages'],
                          shrunk['heap_existing_changed_pct'], shrunk['heap_overall_changed_pct']),
                         (15, 0, 15.0, 15.0))
        grown = self.pages_per_row([
            FileDiff('base/5/16397', 100 * BLOCK_SIZE, 100 * BLOCK_SIZE, tuple(range(10))),
            FileDiff('base/5/16397.1', None, 2 * BLOCK_SIZE, (0, 1))], 10)
        self.assertEqual((grown['heap_pages_existing'], grown['heap_pages_changed_existing'],
                          grown['heap_appended_pages'], grown['heap_existing_changed_pct']),
                         (100, 10, 2, 10.0))
        self.assertIsNone(grown['index_changed_pct'])  # no index pages compared


AGREEMENT_KEYS = ('changed_and_tracked', 'changed_not_tracked', 'tracked_unchanged',
                  'tracked_beyond_new_end')


def fake_result(**overrides):
    totals = pagediff.empty_totals()
    totals['chunks']['8K'].update(chunks=1000, changed=125, send_bytes=125 * BLOCK_SIZE)
    totals['chunks']['64K'].update(chunks=125, changed=50, send_bytes=400 * BLOCK_SIZE)
    totals['chunks']['1M'].update(chunks=10, changed=5, send_bytes=500 * BLOCK_SIZE)
    totals['new_bytes'] = 1000 * BLOCK_SIZE
    pagediff.finish_totals(totals)
    heap = pagediff.finish_totals({'chunks': {'8K': {'chunks': 400, 'changed': 100}}})
    result = {
        'trial_id': '00-uniform-f0.01-base', 'status': 'pass',
        'params': {'pattern': 'uniform', 'fraction': 0.01, 'variant': 'base', 'scale': 10,
                   'data_checksums': True, 'load_vacuum': True, 'checkpoint': False,
                   'vacuum': False, 'read_scan': False, 'seed': 1},
        'ground_truth': {'rows_changed': 10000, 'pages_per_row': {
            'table': 'public.pgbench_accounts',
            'heap_pages_existing': 100, 'heap_pages_changed_existing': 20,
            'heap_existing_changed_pct': 20.0, 'heap_appended_pages': 20,
            'uniform_model_heap_existing_pct': 63.39676587267709,
            'heap_pages_changed': 40, 'heap_pages_compared': 120,
            'heap_overall_changed_pct': 100 * 40 / 120,
            'index_pages_changed': 0, 'index_pages_compared': 0, 'index_changed_pct': None,
            'rows_per_heap_page_before': 61.03}},
        'content_changes': {'totals': {'all': totals, 'data': totals},
                            'by_kind': {'heap': heap}},
        'tracker': {'total_bytes': 2 * BLOCK_SIZE * 100, 'incremental_files': {'files': 3},
                    'full_files': {'files': 4}},
        'agreement': {
            'incremental_backup': {'scopes': {
                'all': {'changed_and_tracked': 120, 'changed_not_tracked': 5,
                        'tracked_unchanged': 9, 'tracked_beyond_new_end': 2},
                'incremental': {'changed_and_tracked': 100, 'changed_not_tracked': 5,
                                'tracked_unchanged': 0, 'tracked_beyond_new_end': 1}}},
            'wal_summaries': {'scopes': {
                'all': {'changed_and_tracked': 125, 'changed_not_tracked': 0,
                        'tracked_unchanged': 40, 'tracked_beyond_new_end': 3}}}},
    }
    result.update(overrides)
    return result


class ReportTests(TempDirTest):
    def test_summary_row_headlines(self):
        row = report.summary_row(fake_result())
        self.assertEqual(row['pct_8k_changed_data'], 12.5)
        self.assertEqual(row['pct_8k_changed_heap'], 25.0)
        self.assertIsNone(row['pct_8k_changed_index'])
        self.assertEqual(row['pct_1m_changed_data'], 50.0)
        self.assertEqual(row['pct_bytes_sent_8k_data'], 12.5)
        self.assertEqual(row['pct_bytes_sent_1m_data'], 50.0)
        self.assertEqual(row['i1_over_b1'], 0.2)
        self.assertEqual(row['changed_not_tracked'], 5)
        self.assertEqual(row['incremental_tracked_unchanged'], 0)
        self.assertEqual(row['rows_changed'], 10000)
        self.assertEqual(set(row), set(report.CSV_FIELDS))

    def test_target_heap_columns_separate_existing_from_appended_pages(self):
        row = report.summary_row(fake_result())
        self.assertEqual(row['heap_existing_changed_pct'], 20.0)
        self.assertEqual(row['heap_appended_pages'], 20)
        self.assertEqual(row['heap_pages_existing'], 100)
        self.assertEqual(row['uniform_model_heap_existing_pct'], 63.3968)
        self.assertEqual(row['heap_overall_changed_pct'], 33.3333)
        self.assertIsNone(row['index_changed_pct'])
        self.assertEqual(row['rows_per_heap_page'], 61.03)
        self.assertEqual(row['target_table'], 'public.pgbench_accounts')

    def test_csv_has_every_per_trial_column_the_study_reports(self):
        labels = [pagediff.chunk_label(size).lower() for size in pagediff.CHUNK_SIZES]
        required = ['pattern', 'fraction', 'variant', 'rows_changed', 'rows_per_heap_page',
                    'heap_existing_changed_pct', 'uniform_model_heap_existing_pct',
                    'heap_appended_pages', 'heap_overall_changed_pct', 'index_changed_pct',
                    *(f'pct_{label}_changed_data' for label in labels),
                    *(f'pct_bytes_sent_{label}_data' for label in labels),
                    'b1_bytes', 'i1_bytes', 'i1_over_b1',
                    *(prefix + key for prefix in ('', 'incremental_', 'walsummary_')
                      for key in AGREEMENT_KEYS)]
        self.assertEqual([column for column in required if column not in report.CSV_FIELDS], [])
        self.assertEqual(len(set(report.CSV_FIELDS)), len(report.CSV_FIELDS))
        row = report.summary_row(fake_result())
        self.assertEqual((row['pct_64k_changed_data'], row['pct_bytes_sent_64k_data']),
                         (40.0, 40.0))
        self.assertIsNone(row['pct_16k_changed_data'])  # no chunks recorded at that size
        self.assertEqual([row[key] for key in AGREEMENT_KEYS], [120, 5, 9, 2])
        self.assertEqual([row['incremental_' + key] for key in AGREEMENT_KEYS], [100, 5, 0, 1])
        self.assertEqual([row['walsummary_' + key] for key in AGREEMENT_KEYS], [125, 0, 40, 3])
        self.assertEqual(row['variant'], 'base')

    def test_csv_has_one_row_per_trial_including_errors(self):
        path = self.root / 'summary.csv'
        failed = {'trial_id': '01-hot-f0.5-base', 'status': 'error', 'error': 'boom',
                  'params': {'pattern': 'hot', 'fraction': 0.5}}
        report.write_summary_csv(path, [fake_result(), failed])
        rows = list(csv.DictReader(path.read_text().splitlines()))
        self.assertEqual(list(rows[0]), list(report.CSV_FIELDS))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['pct_8k_changed_data'], '12.5')
        self.assertEqual(rows[1]['status'], 'error')
        self.assertEqual(rows[1]['error'], 'boom')
        self.assertEqual(rows[1]['pct_8k_changed_data'], '')
        self.assertFalse(path.with_suffix('.csv.tmp').exists())


class TrialSpecTests(unittest.TestCase):
    def specs(self, *argv):
        args, _ = run_study.parse_args(list(argv))
        return run_study.trial_specs(args)

    def test_single_trial_defaults(self):
        [spec] = self.specs('--pattern', 'append', '--fraction', '0.05', '--scale', '2')
        self.assertEqual(spec['trial_id'], '00-append-f0.05-base')
        self.assertEqual((spec['accounts_rows'], spec['events_rows']), (200000, 200000))
        self.assertTrue(spec['data_checksums'] and spec['load_vacuum'])
        self.assertFalse(spec['vacuum'] or spec['checkpoint'] or spec['read_scan'])

    def test_matrix_product_and_variants(self):
        specs = self.specs('--matrix', '--patterns', 'uniform,hot', '--fractions', '0.001,0.01',
                           '--variants', 'base,vacuum+no-checksums', '--checkpoint')
        self.assertEqual(len(specs), 8)
        self.assertEqual(len({s['trial_id'] for s in specs}), 8)
        self.assertTrue(all(s['checkpoint'] for s in specs))  # global flag applies to all
        tuned = [s for s in specs if s['variant_requested'] == 'vacuum+no-checksums']
        self.assertEqual(len(tuned), 4)
        self.assertTrue(all(s['vacuum'] and not s['data_checksums'] for s in tuned))
        self.assertTrue(all(s['variant'] == 'checkpoint+no-checksums+vacuum' for s in tuned))
        # The global --checkpoint runs in every trial, so no ID may claim 'base'.
        self.assertEqual(specs[0]['trial_id'], '00-uniform-f0.001-checkpoint')

    def test_trial_ids_name_the_effective_options(self):
        # Regression: IDs came from the variant token only, so '--vacuum' trials said 'base'.
        [default] = self.specs()
        self.assertEqual((default['trial_id'], default['variant']),
                         ('00-uniform-f0.01-base', 'base'))
        [spec] = self.specs('--vacuum', '--no-checksums')
        self.assertEqual(spec['trial_id'], '00-uniform-f0.01-no-checksums+vacuum')
        self.assertEqual((spec['variant'], spec['variant_requested']),
                         ('no-checksums+vacuum', 'base'))
        specs = self.specs('--matrix', '--patterns', 'uniform', '--variants',
                           'base,read-scan+no-load-vacuum', '--checkpoint')
        self.assertEqual([s['trial_id'] for s in specs],
                         ['00-uniform-f0.01-checkpoint',
                          '01-uniform-f0.01-checkpoint+no-load-vacuum+read-scan'])
        self.assertFalse(specs[1]['load_vacuum'])

    def test_duplicate_configurations_are_rejected(self):
        cases = {
            'global flag repeats a variant': ['--variants', 'base,vacuum', '--vacuum'],
            'same options in another order': ['--variants', 'vacuum+checkpoint,checkpoint+vacuum'],
            'repeated pattern': ['--patterns', 'uniform,uniform'],
            'repeated fraction': ['--fractions', '0.01,0.010'],
        }
        for name, extra in cases.items():
            with self.subTest(name), self.assertRaisesRegex(ValueError, 'duplicate'):
                self.specs('--matrix', '--patterns', 'uniform', *extra)
        # Repeats differ by seed, so they are not duplicates.
        repeated = self.specs('--matrix', '--patterns', 'uniform', '--repeats', '2')
        self.assertEqual([s['trial_id'] for s in repeated],
                         ['00-uniform-f0.01-base-r0', '01-uniform-f0.01-base-r1'])

    def test_impossible_hot_trial_fails_before_any_cluster_work(self):
        args, _ = run_study.parse_args(['--pattern', 'hot', '--fraction', '0.05'])
        with self.assertRaisesRegex(ValueError, 'hot set'):
            run_study.trial_specs(args)

    def test_bad_variant_is_rejected(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            run_study.parse_args(['--matrix', '--variants', 'vacuum+fast'])


def no_trials(*args, **kwargs):
    raise AssertionError('no trial should run')


class MainTests(TempDirTest):
    """run_study.main with PostgreSQL (preflight) and trials replaced."""

    def main(self, argv, *, run_trial=no_trials):
        with mock.patch.object(run_study, 'preflight', return_value=({}, '17.11', 17)), \
                mock.patch.object(run_study, 'run_trial', run_trial), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            try:
                return run_study.main(argv)
            finally:
                self.stderr = err.getvalue()

    def test_empty_matrix_lists_are_usage_errors(self):
        # Regression: an empty list ran no trials, then crashed reading summary.csv.
        for flag in ('--patterns', '--fractions', '--variants'):
            for value in ('', ','):
                with self.subTest(flag=flag, value=value):
                    out = self.root / f'out{flag}{len(value)}'
                    with self.assertRaises(SystemExit) as raised:
                        self.main(['--matrix', flag, value, '--out', str(out)])
                    self.assertEqual(raised.exception.code, 2)
                    self.assertIn(flag, self.stderr)
                    self.assertFalse(out.exists())

    @unittest.skipUnless(shutil.which('git'), 'git is not installed')
    def test_run_json_records_whether_the_tree_was_dirty(self):
        # Regression: run.json recorded HEAD only, so results from uncommitted
        # code looked identical to results from the commit.
        repo = self.root / 'repo'
        repo.mkdir()

        def git(*args):
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Study',
                            '-c', 'user.email=study@example.invalid', '-c', 'commit.gpgsign=false',
                            '-c', 'core.hooksPath=/dev/null', *args],
                           check=True, capture_output=True)
        git('init', '-q')
        (repo / 'a.txt').write_text('one\n')
        git('add', 'a.txt')
        git('commit', '-q', '-m', 'init')
        trial_environments = []

        def fake_trial(spec, tools, major, out, **kwargs):
            trial_environments.append(kwargs['environment'])
            return {'trial_id': spec['trial_id'], 'status': 'pass', 'params': spec}

        def environment(name):
            out = self.root / name
            with mock.patch.object(run_study, 'REPO_ROOT', repo):
                self.assertEqual(self.main(['--out', str(out)], run_trial=fake_trial), 0)
            return json.loads((out / 'run.json').read_text())['environment']

        clean = environment('clean')
        self.assertRegex(clean['git_commit'], '^[0-9a-f]{40}$')
        self.assertIs(clean['git_dirty'], False)
        self.assertEqual(clean['git_status'], [])
        self.assertIsNone(clean['git_diff_sha256'])

        (repo / 'a.txt').write_text('two\n')
        dirty = environment('dirty')
        self.assertEqual(dirty['git_commit'], clean['git_commit'])
        self.assertIs(dirty['git_dirty'], True)
        self.assertEqual(dirty['git_status'], [' M a.txt'])
        self.assertRegex(dirty['git_diff_sha256'], '^[0-9a-f]{12}$')
        self.assertIs(trial_environments[-1]['git_dirty'], True)  # trial JSONs carry it too

        (repo / 'a.txt').write_text('three\n')
        self.assertNotEqual(environment('dirtier')['git_diff_sha256'], dirty['git_diff_sha256'])

        (repo / 'a.txt').write_text('one\n')
        (repo / 'new.txt').write_text('x\n')
        untracked = environment('untracked')
        self.assertEqual((untracked['git_dirty'], untracked['git_status']), (True, ['?? new.txt']))

    def test_provenance_outside_a_repository_is_unknown(self):
        with mock.patch.dict(os.environ, {'GIT_CEILING_DIRECTORIES': str(self.root)}):
            self.assertEqual(run_study.git_provenance(self.root),
                             {'git_commit': None, 'git_dirty': None, 'git_status': None,
                              'git_diff_sha256': None})

    def test_duplicate_configuration_is_a_usage_error(self):
        out = self.root / 'out'
        with self.assertRaises(SystemExit) as raised:
            self.main(['--matrix', '--variants', 'base,vacuum', '--vacuum', '--out', str(out)])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn('duplicate', self.stderr)
        self.assertFalse(out.exists())


class RunTrialTests(TempDirTest):
    """run_trial bookkeeping, with the PostgreSQL part replaced."""

    def run_with(self, execute, *, keep=False):
        [spec] = run_study.trial_specs(run_study.parse_args([])[0])
        original = run_study.Trial.execute
        run_study.Trial.execute = execute
        try:
            with redirect_stderr(io.StringIO()):
                return run_study.run_trial(spec, {}, 17, self.root / 'out', keep=keep,
                                           work_dir=self.root / 'work')
        finally:
            run_study.Trial.execute = original

    def setUp(self):
        super().setUp()
        (self.root / 'work').mkdir()

    def test_failed_trial_is_recorded_and_cleaned_up(self):
        def explode(trial):
            (trial.root / 'scratch').write_text('x')
            raise RuntimeError('pg_basebackup failed')
        result = self.run_with(explode)
        self.assertEqual(result['status'], 'error')
        self.assertIn('pg_basebackup failed', result['error'])
        saved = json.loads((self.root / 'out/trials/00-uniform-f0.01-base.json').read_text())
        self.assertEqual(saved['status'], 'error')
        self.assertTrue((self.root / 'out/logs/00-uniform-f0.01-base/error.txt').is_file())
        self.assertEqual(list((self.root / 'work').iterdir()), [])
        self.assertEqual(report.summary_row(saved)['status'], 'error')

    def test_keep_retains_the_work_directory(self):
        result = self.run_with(lambda trial: {'marker': 1}, keep=True)
        self.assertEqual((result['status'], result['marker']), ('pass', 1))
        self.assertTrue(Path(result['kept_work_dir']).is_dir())
        self.assertIn('total', result['timings'])


@unittest.skipUnless((PG17_BIN / 'initdb').is_file(), 'PostgreSQL 17 binaries not installed')
class EndToEndTrialTest(unittest.TestCase):
    def test_tiny_uniform_trial(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / 'out'
            started = time.monotonic()
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = run_study.main(['--bin-dir', str(PG17_BIN), '--scale', '1',
                                       '--pattern', 'uniform', '--fraction', '0.01',
                                       '--out', str(out), '--work-dir', root])
            elapsed = time.monotonic() - started
            [trial_file] = (out / 'trials').glob('*.json')
            result = json.loads(trial_file.read_text())
            self.assertEqual(code, 0, result.get('error'))
            self.assertLess(elapsed, 60)
            self.assertEqual(result['status'], 'pass')
            self.assertEqual(result['ground_truth']['rows_changed'], 1000)
            self.assertEqual(result['ground_truth']['verification']['found']['changed'], 1000)
            heap = result['content_changes']['by_relation']['public.pgbench_accounts']
            self.assertGreater(heap['chunks']['8K']['changed'], 100)
            self.assertEqual(result['content_changes']['by_relation']['public.events']
                             ['chunks']['8K']['changed'], 0)
            incremental = result['tracker']['incremental_files']
            self.assertGreater(incremental['files'], 0)
            self.assertEqual(len(incremental['by_file']), incremental['files'])
            self.assertEqual(sum(len(tracker.expand_ranges(entry['blocks']))
                                 for entry in incremental['by_file'].values()),
                             incremental['blocks_listed'])
            self.assertEqual(sum(entry['size'] for entry in incremental['by_file'].values()),
                             incremental['bytes'])
            agreement = result['agreement']['incremental_backup']['scopes']
            # With checksums on and a vacuumed basis, every changed block is WAL-logged.
            self.assertEqual(agreement['all']['changed_not_tracked'], 0)
            self.assertGreaterEqual(agreement['all']['changed_and_tracked'],
                                    heap['chunks']['8K']['changed'])
            self.assertGreater(result['tracker']['wal_summaries']['files'], 0)
            self.assertIsInstance(result['tracker']['wal_summaries']['ignored_files'], list)
            target = result['ground_truth']['pages_per_row']
            self.assertEqual(target['heap_pages_changed_existing'] + target['heap_appended_pages'],
                             target['heap_pages_changed'])
            self.assertEqual(target['heap_pages_existing'], target['heap_pages_before'])
            self.assertGreater(target['heap_appended_pages'], 0)  # non-HOT row versions
            rows = list(csv.DictReader((out / 'summary.csv').read_text().splitlines()))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['status'], 'pass')
            self.assertEqual(float(rows[0]['heap_existing_changed_pct']),
                             round(target['heap_existing_changed_pct'], 4))
            self.assertEqual(int(rows[0]['heap_appended_pages']), target['heap_appended_pages'])
            environment = json.loads((out / 'run.json').read_text())['environment']
            self.assertIn(environment['git_dirty'], (True, False))
            # Temporary cluster and backups are removed; only out/ remains.
            self.assertEqual(sorted(p.name for p in Path(root).iterdir()), ['out'])


if __name__ == '__main__':
    unittest.main()
