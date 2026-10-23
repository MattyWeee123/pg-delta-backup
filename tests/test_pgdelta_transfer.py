"""End-to-end transfers on synthetic directory pairs (gate G0 shapes), sender and receiver in one process."""
from pathlib import Path
import random
import tempfile
import unittest
from unittest import mock

from pgdelta.protocol import CATEGORIES
from pgdelta.receiver import remove_tree

from pgdelta_support import PAGE, differing_pages, snapshot, staging_dirs, total_pages, transfer, write_tree

NODE = 4 * PAGE  # tests use 4-page nodes so multi-node files stay small


def pages(count, seed):
    return random.Random(seed).randbytes(int(count * PAGE))


def flip(data, *offsets):
    data = bytearray(data)
    for offset in offsets:
        data[offset] ^= 0xFF
    return bytes(data)


IDENTITY = {'system_identifier': '1', 'pg_control_version': 1700, 'catalog_version': 202406281,
            'block_size': 8192, 'blocks_per_segment': 131072, 'data_checksum_version': 0,
            'redo_lsn': '0/0', 'timeline': 1}


class TransferTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.basis, self.output = self.root / 'S', self.root / 'B', self.root / 'O'

    def build(self, source_files, basis_files, source_dirs=(), basis_dirs=(), source_modes=None):
        write_tree(self.source, source_files, source_dirs, source_modes)
        write_tree(self.basis, basis_files, basis_dirs)

    def reset_output(self):
        if self.output.exists():
            remove_tree(self.output)

    def run_transfer(self, **kwargs):
        basis_before = snapshot(self.basis)
        expected_sent = differing_pages(self.source, self.basis)
        outcome = transfer(self.source, self.basis, self.output, **kwargs)
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assertIsNone(outcome.receiver_error, repr(outcome.receiver_error))
        stats = outcome.sender
        self.assertEqual(snapshot(self.output), snapshot(self.source))
        self.assertEqual(snapshot(self.basis), basis_before)
        self.assertEqual(staging_dirs(self.output), [])
        self.assertEqual(stats['status'], 'complete')
        self.assertEqual(stats['pages']['total'], total_pages(self.source))
        if stats['mode'] == 'reuse' and kwargs.get('high_churn_node_fraction') is None:
            self.assertEqual(stats['pages']['sent'], expected_sent)
        self.check_accounting(stats, outcome.receiver)
        return stats

    def check_accounting(self, stats, receiver):
        up, down = stats['bytes_source_to_dest'], stats['bytes_dest_to_source']
        for direction in (up, down, receiver['bytes_read'], receiver['bytes_written']):
            self.assertEqual(sum(direction[c] for c in CATEGORIES), direction['total'])
            self.assertEqual(sum(t['bytes'] for t in direction['by_message_type'].values()), direction['total'])
        self.assertEqual(receiver['bytes_read'], up)
        final = down['by_message_type']['COMPLETE']['bytes']
        self.assertEqual(receiver['bytes_written']['total'] + final, down['total'])
        self.assertEqual(up['page_data'] - 17 * up['by_message_type'].get('PAGE_DATA', {}).get('frames', 0),
                         stats['pages']['sent_bytes'])
        self.assertEqual(stats['pages']['sent'] + stats['pages']['reused'], stats['pages']['total'])
        self.assertEqual(receiver['pages_received'], stats['pages']['sent'])
        self.assertEqual(receiver['pages_copied'], stats['pages']['reused'])
        files = stats['files']
        self.assertEqual(files['unchanged'] + files['changed'] + files['new'], files['total'])
        self.assertEqual(receiver['files_verified'], files['total'])
        self.assertIsNotNone(stats['timings_seconds']['verify'])


class ShapeTests(TransferTestCase):
    def test_identical(self):
        files = {'base/1/1259': pages(10.5, 1), 'base/1/1259_fsm': pages(3, 2), 'empty': b'',
                 'global/pg_control': pages(1, 3)}
        self.build(files, files, ['pg_wal/archive_status'], ['pg_wal/archive_status'])
        stats = self.run_transfer()
        self.assertEqual(stats['pages']['sent'], 0)
        self.assertEqual(stats['files'], {'total': 4, 'unchanged': 4, 'changed': 0, 'new': 0, 'size_mismatch': 0})
        self.assertNotIn('LEAF_REQUEST', stats['bytes_source_to_dest']['by_message_type'])
        self.assertEqual(stats['bytes_source_to_dest']['page_data'], 0)
        self.assertEqual(stats['receiver']['whole_file_copies'], 3)

    def test_one_page_changed_in_a_multi_node_file(self):
        old = pages(12, 4)
        self.build({'rel': flip(old, 6 * PAGE + 9)}, {'rel': old})
        stats = self.run_transfer()
        self.assertEqual(stats['pages']['sent'], 1)
        self.assertEqual(stats['nodes'], {'total': 3, 'matched': 2, 'descended': 1, 'sent_whole': 0})
        self.assertEqual(stats['bytes_dest_to_source']['by_message_type']['LEAF_DIGESTS']['frames'], 1)
        self.assertEqual(stats['files']['changed'], 1)

    def test_leaf_digests_come_from_the_cache_or_a_second_read(self):
        old = pages(20, 25)
        self.build({'a': flip(old, 0, 9 * PAGE), 'b': flip(old, 17 * PAGE)}, {'a': old, 'b': old})
        stats = self.run_transfer()
        self.assertEqual((stats['receiver']['leaf_nodes_cached'], stats['receiver']['leaf_nodes_reread']), (3, 0))
        self.reset_output()
        with mock.patch('pgdelta.receiver.LEAF_CACHE_BYTES', 0):
            stats = self.run_transfer()
        self.assertEqual((stats['receiver']['leaf_nodes_cached'], stats['receiver']['leaf_nodes_reread']), (0, 3))

    def test_pages_changed_in_several_nodes_use_one_leaf_round_trip(self):
        old = pages(20, 5)
        self.build({'rel': flip(old, 0, 5 * PAGE, 5 * PAGE + 1, 13 * PAGE + 8000, 19 * PAGE)}, {'rel': old})
        stats = self.run_transfer()
        self.assertEqual(stats['pages']['sent'], 4)
        self.assertEqual(stats['nodes']['descended'], 4)
        self.assertEqual(stats['bytes_source_to_dest']['by_message_type']['LEAF_REQUEST']['frames'], 1)
        self.assertEqual(stats['bytes_dest_to_source']['by_message_type']['LEAF_DIGESTS']['frames'], 1)

    def test_append(self):
        base = pages(6.5, 6)
        self.build({'short_page_grows': base + b'tail',
                    'within_node': base + pages(1.25, 7),
                    'new_nodes': base + pages(9, 8),
                    'from_empty': pages(2, 9)},
                   {'short_page_grows': base, 'within_node': base, 'new_nodes': base, 'from_empty': b''})
        stats = self.run_transfer()
        self.assertEqual(stats['files']['size_mismatch'], 4)
        self.assertEqual(stats['pages']['sent'], 1 + 2 + 10 + 2)

    def test_truncate(self):
        base = pages(13.5, 10)
        self.build({'mid_page': base[:5 * PAGE + 77], 'page_boundary': base[:6 * PAGE],
                    'node_boundary': base[:2 * NODE], 'to_zero': b''},
                   {'mid_page': base, 'page_boundary': base, 'node_boundary': base, 'to_zero': base})
        stats = self.run_transfer()
        self.assertEqual(stats['pages']['sent'], 1)  # only the new short last page of mid_page
        self.assertEqual(stats['files']['changed'], 4)

    def test_deleted_added_and_empty_files(self):
        self.build({'kept': pages(3, 11), 'added': pages(2.5, 12), 'empty_new': b'', 'emptied': b'',
                    'empty_kept': b''},
                   {'kept': pages(3, 11), 'deleted': pages(4, 13), 'emptied': pages(1, 14), 'empty_kept': b''})
        stats = self.run_transfer()
        self.assertFalse((self.output / 'deleted').exists())
        self.assertEqual(stats['files'], {'total': 5, 'unchanged': 2, 'changed': 1, 'new': 2, 'size_mismatch': 1})
        self.assertEqual(stats['pages']['sent'], 3)

    def test_nested_directories_and_permissions(self):
        self.build({'base/16384/2619': pages(5, 15), 'base/16384/2619_vm': pages(1, 16),
                    'pg_xact/0000': pages(2, 17), 'deep/a/b/c/d/file': b'x'},
                   {'base/16384/2619': pages(5, 15), 'only_in_basis/dir/file': b'y'},
                   source_dirs=['pg_wal/archive_status', 'pg_notify', 'deep/empty'],
                   basis_dirs=['stale_empty'],
                   source_modes={'base/16384/2619': 0o600, 'pg_xact/0000': 0o640, 'base': 0o700,
                                 'deep/a': 0o750, 'pg_notify': 0o700})
        self.run_transfer()
        self.assertFalse((self.output / 'only_in_basis').exists())
        self.assertFalse((self.output / 'stale_empty').exists())
        self.assertTrue((self.output / 'pg_wal' / 'archive_status').is_dir())

    def test_rename_is_delete_plus_add(self):
        data = pages(9, 18)
        self.build({'base/1/new_name': data}, {'base/1/old_name': data})
        stats = self.run_transfer()
        self.assertEqual(stats['pages']['sent'], 9)
        self.assertEqual(stats['files']['new'], 1)

    def test_complete_rewrite_and_reordering(self):
        first, second = pages(4, 19), pages(4, 20)
        self.build({'rewrite': pages(8, 21), 'reordered': second + first}, {'rewrite': pages(8, 22),
                                                                           'reordered': first + second})
        stats = self.run_transfer()
        self.assertEqual(stats['pages']['sent'], 16)

    def test_default_one_mib_nodes(self):
        old = pages(300.5, 23)
        self.build({'segment': flip(old, 200 * PAGE) + b'x'}, {'segment': old})
        stats = self.run_transfer(node_blocks=128)
        self.assertEqual(stats['pages']['sent'], 2)
        self.assertEqual(stats['nodes'], {'total': 3, 'matched': 1, 'descended': 2, 'sent_whole': 0})

    def test_many_files_overlap_leaf_requests_with_page_data(self):
        source, basis = {}, {}
        for number in range(40):
            old = pages(6 + number % 7, 100 + number)
            basis[f'base/1/{number}'] = old
            source[f'base/1/{number}'] = flip(old, (number % 6) * PAGE) if number % 3 else old
        self.build(source, basis)
        for window in (1, 4, 64):
            with self.subTest(window=window):
                self.reset_output()
                stats = self.run_transfer(window=window)
                self.assertEqual(stats['window'], window)
                self.assertEqual(stats['pages']['sent'], 26)
                self.assertEqual(stats['bytes_source_to_dest']['by_message_type']['LEAF_REQUEST']['frames'], 26)

    def test_seeded_random_edits(self):
        rng = random.Random(24)
        for case in range(8):
            with self.subTest(case=case):
                self.setUp()
                source_files, basis_files = {}, {}
                for number in range(rng.randrange(1, 6)):
                    old = rng.randbytes(rng.randrange(0, 15 * PAGE))
                    new = bytearray(old)
                    for _ in range(rng.randrange(0, 5)):
                        if new:
                            new[rng.randrange(len(new))] ^= 1 + rng.randrange(255)
                    if rng.random() < 0.3:
                        new = new[:rng.randrange(len(new) + 1)]
                    if rng.random() < 0.3:
                        new += rng.randbytes(rng.randrange(3 * PAGE))
                    path = f'd{number % 2}/f{number}'
                    if rng.random() < 0.85:
                        source_files[path] = bytes(new)
                    if rng.random() < 0.85:
                        basis_files[path] = old
                self.build(source_files, basis_files)
                self.run_transfer()


class ModeTests(TransferTestCase):
    def setUp(self):
        super().setUp()
        old = pages(12, 30)
        self.build({'rel': flip(old, 3), 'new': pages(1, 31)}, {'rel': old})

    def test_unknown_identity_means_full_mode_and_every_page_is_sent(self):
        stats = self.run_transfer(reuse_any_basis=False)
        self.assertEqual(stats['mode'], 'full')
        self.assertEqual(stats['reuse_policy'], 'same-cluster')
        self.assertEqual(stats['receiver']['reuse_policy'], 'same-cluster')
        self.assertIn('identity unknown', stats['gate_reason'])
        self.assertEqual(stats['pages']['sent'], stats['pages']['total'])
        for name in ('BASIS_NODES', 'LEAF_DIGESTS'):
            self.assertNotIn(name, stats['bytes_dest_to_source']['by_message_type'])
        self.assertEqual(stats['files']['changed'] + stats['files']['new'], 2)

    def test_matching_identities_allow_reuse(self):
        stats = self.run_transfer(source_identity=IDENTITY, basis_identity=dict(IDENTITY, redo_lsn='0/1'),
                                  reuse_any_basis=False)
        self.assertEqual(stats['mode'], 'reuse')
        self.assertEqual(stats['reuse_policy'], 'same-cluster')
        self.assertEqual(stats['pages']['sent'], 2)
        self.assertEqual(stats['source_identity'], IDENTITY)

    def test_identity_mismatch_means_full_mode(self):
        stats = self.run_transfer(source_identity=IDENTITY,
                                  basis_identity=dict(IDENTITY, system_identifier='2'), reuse_any_basis=False)
        self.assertEqual(stats['mode'], 'full')
        self.assertIn('system identifier differs', stats['gate_reason'])
        self.assertEqual(stats['pages']['sent'], stats['pages']['total'])

    def test_reuse_any_basis_reuses_a_mismatched_basis_and_still_verifies_every_file(self):
        # The gate is an efficiency policy: the output is exact because every file is verified.
        stats = self.run_transfer(source_identity=IDENTITY, basis_identity=dict(IDENTITY, system_identifier='2'),
                                  reuse_any_basis=True)
        self.assertEqual(stats['mode'], 'reuse')
        self.assertEqual(stats['reuse_policy'], 'any-basis')
        self.assertEqual(stats['receiver']['reuse_policy'], 'any-basis')
        self.assertIn('system identifier differs', stats['gate_reason'])
        self.assertIn('--reuse-any-basis', stats['gate_reason'])
        self.assertEqual(stats['pages']['sent'], 2)
        self.assertEqual(stats['receiver']['files_verified'], 2)

    def test_high_churn_fraction_sends_mismatched_nodes_without_leaf_digests(self):
        stats = self.run_transfer(high_churn_node_fraction=0.0)
        self.assertNotIn('LEAF_REQUEST', stats['bytes_source_to_dest']['by_message_type'])
        self.assertEqual(stats['pages']['sent'], 4 + 1)
        self.assertEqual(stats['high_churn_files'], 1)
        self.reset_output()
        stats = self.run_transfer(high_churn_node_fraction=1.0)
        self.assertEqual(stats['pages']['sent'], 2)

    def test_block_size_follows_the_source_identity(self):
        stats = self.run_transfer(source_identity=dict(IDENTITY, block_size=8192), basis_identity=IDENTITY,
                                  block=None)
        self.assertEqual(stats['block'], 8192)
        outcome = transfer(self.source, self.basis, self.root / 'O2', source_identity=IDENTITY, block=4096)
        self.assertEqual(outcome.sender_error.exit_code, 2)
        self.assertFalse((self.root / 'O2').exists())


if __name__ == '__main__':
    unittest.main()
