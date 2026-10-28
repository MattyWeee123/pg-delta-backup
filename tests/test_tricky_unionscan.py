"""EXP-69's scorer for idea 1 (experiments/tricky_copies/unionscan.py) on hand-made inputs, shaped like
pg_waldump's and pg_rewind's own output."""
import struct
import tempfile
import unittest
from pathlib import Path

from experiments.tricky_copies import summaryscan, unionscan
from experiments.tricky_copies.lsnscan import BLOCK, parse_lsn

INFO = {'system_identifier': '7', 'pg_version': 17, 'data_checksum_version': 1, 'wal_log_hints': 'off'}
WALDUMP = '\n'.join([
    'rmgr: XLOG        len (rec/tot):     30/    30, tx:          0, lsn: 0/08D10038, prev 0/08D0FF90, desc: '
    'CHECKPOINT_REDO wal_level replica',
    'rmgr: Heap        len (rec/tot):     54/    54, tx:        791, lsn: 0/08D10058, prev 0/08D10038, desc: '
    'INSERT off: 9, flags: 0x00, blkref #0: rel 1663/5/16384 blk 7',
    'rmgr: XLOG        len (rec/tot):    114/   114, tx:          0, lsn: 0/08D10090, prev 0/08D10058, desc: '
    'CHECKPOINT_ONLINE redo 0/8D10038; tli 1; prev tli 1; fpw true; wal_level replica; online',
    'rmgr: Heap        len (rec/tot):     78/ 16414, tx:        792, lsn: 0/08D10108, prev 0/08D10090, desc: '
    'UPDATE old_xmax: 792, old_off: 3, new_off: 1, blkref #0: rel 1663/5/16384 blk 0 FPW, '
    'blkref #3: rel 1663/5/16384 fork vm blk 0 FPW',
    'rmgr: Btree       len (rec/tot):     53/  7453, tx:        792, lsn: 0/08D14158, prev 0/08D10108, desc: '
    'INSERT_LEAF off: 4, blkref #0: rel 1663/5/16389 blk 131073 FPW',
    'rmgr: XLOG        len (rec/tot):    114/   114, tx:          0, lsn: 0/09A1D930, prev 0/09A1D908, desc: '
    'CHECKPOINT_SHUTDOWN redo 0/9A1D930; tli 1; prev tli 1; fpw true; wal_level replica; shutdown',
])


class WalDumpParsing(unittest.TestCase):
    def test_checkpoints_give_record_and_redo(self):
        self.assertEqual(unionscan.checkpoints(WALDUMP), [(parse_lsn('0/8D10090'), parse_lsn('0/8D10038')),
                                                          (parse_lsn('0/9A1D930'), parse_lsn('0/9A1D930'))])

    def test_block_references_by_fork_from_a_start(self):
        blocks, records, last = unionscan.block_refs(WALDUMP, start=parse_lsn('0/8D10038'))
        self.assertEqual(blocks, {'1663/5/16384/0': {7, 0}, '1663/5/16384/2': {0}, '1663/5/16389/0': {131073}})
        self.assertEqual((records, last), (6, parse_lsn('0/9A1D930')))
        later = unionscan.block_refs(WALDUMP, start=parse_lsn('0/8D10090'))[0]
        self.assertEqual(later['1663/5/16384/0'], {0})  # the insert before the checkpoint record is left out

    def test_fork_point_is_where_the_history_leaves_b(self):
        path = [(1, 0, parse_lsn('0/8D100E8')), (2, parse_lsn('0/8D100E8'), summaryscan.END)]
        self.assertEqual(unionscan.fork_point(path, 1), parse_lsn('0/8D100E8'))
        self.assertIsNone(unionscan.fork_point(path, 2))
        self.assertIsNone(unionscan.fork_point(path, 3))


class Union(unittest.TestCase):
    def test_b_blocks_join_the_summaries_choice_with_its_limits(self):
        chosen = {'base/5/16384': [{1, 2}, None], 'base/5/16390': [set(), 4]}
        merged = unionscan.union(chosen, {'1663/5/16384/0': {2, 9}, '1663/5/16384/2': {0}})
        self.assertEqual(merged, {'base/5/16384': [{1, 2, 9}, None], 'base/5/16390': [set(), 4],
                                  'base/5/16384_vm': [{0}, None]})
        self.assertEqual(chosen['base/5/16384'][0], {1, 2})  # not changed in place


class Guard(unittest.TestCase):
    path = [(1, 0, parse_lsn('0/5000000')), (2, parse_lsn('0/5000000'), summaryscan.END)]
    summaries = [{'tli': 1, 'start': '0/1000000', 'end': '0/5000000', 'blocks': {}, 'limits': {}},
                 {'tli': 2, 'start': '0/5000000', 'end': '0/9000000', 'blocks': {}, 'limits': {}}]
    c = (parse_lsn('0/4800000'), parse_lsn('0/4700000'))

    def check(self, b_end='0/6000000', wal_last='0/6000000', c=c, old=INFO):
        return unionscan.guard(old, INFO, 1, b_end and parse_lsn(b_end), self.path, self.summaries,
                               parse_lsn('0/8000000'), c, wal_last and parse_lsn(wal_last))

    def test_a_diverged_old_primary_with_its_wal_passes(self):
        self.assertEqual(self.check(), (True, []))

    def test_each_condition_refuses(self):
        cases = {
            'end': self.check(b_end=None),
            'checkpoint': self.check(c=None),
            'short wal': self.check(wal_last='0/5800000'),
            'no wal': self.check(wal_last=None),
            'hints': self.check(old={**INFO, 'data_checksum_version': 0}),
            'gap': unionscan.guard(INFO, INFO, 1, parse_lsn('0/6000000'), self.path, self.summaries,
                                   parse_lsn('0/8000000'), (parse_lsn('0/900000'), parse_lsn('0/800000')),
                                   parse_lsn('0/6000000')),
        }
        for name, (ok, reasons) in cases.items():
            self.assertFalse(ok, name)
            self.assertTrue(reasons, name)
        self.assertIn('wal_log_hints', cases['hints'][1][-1])
        self.assertIn('do not cover', cases['gap'][1][0])

    def test_wal_log_hints_stands_in_for_checksums(self):
        no_sums = {**INFO, 'data_checksum_version': 0, 'wal_log_hints': 'on'}
        ok, reasons = unionscan.guard(no_sums, no_sums, 1, parse_lsn('0/6000000'), self.path, self.summaries,
                                      parse_lsn('0/8000000'), self.c, parse_lsn('0/6000000'))
        self.assertEqual((ok, reasons), (True, []))


def heap_page(infomasks):
    """A heap page with one tuple per infomask, each 24 bytes from the end of the page down."""
    page = bytearray(BLOCK)
    lower = 24 + 4 * len(infomasks)
    upper = BLOCK - 24 * len(infomasks)
    struct.pack_into('<HHH', page, 12, lower, upper, BLOCK)
    for i, mask in enumerate(infomasks):
        offset = BLOCK - 24 * (i + 1)
        struct.pack_into('<I', page, 24 + 4 * i, offset | (1 << 15) | (24 << 17))
        struct.pack_into('<H', page, offset + 20, mask)
    return bytes(page)


class BSideHints(unittest.TestCase):
    def test_a_hint_only_b_carries_is_found_and_one_only_s_carries_is_not(self):
        plain, committed = heap_page([0, 0]), heap_page([0, unionscan.HEAP_XMIN_COMMITTED])
        self.assertTrue(unionscan.b_side_hints(committed, plain, 'main'))
        self.assertFalse(unionscan.b_side_hints(plain, committed, 'main'))
        self.assertFalse(unionscan.b_side_hints(committed, committed, 'main'))
        self.assertFalse(unionscan.b_side_hints(committed, plain, 'vm'))


class RewindDebug(unittest.TestCase):
    TEXT = '\n'.join([
        'pg_rewind: servers diverged at WAL location 0/8D10108 on timeline 1',
        'pg_rewind: base/5/16389 (NONE)', 'pg_rewind: block 1', 'pg_rewind: block 4',
        'pg_rewind: base/5/16389.1 (NONE)', 'pg_rewind: block 2',
        'pg_rewind: base/5/16384_vm (COPY)', 'pg_rewind: pg_wal/00000002000000000000000A (COPY)',
        'pg_rewind: base/5/16399 (COPY_TAIL)',
        'pg_rewind: need to copy 97 MB (total source directory size is 147 MB)',
        'pg_rewind: received chunk for file "base/5/16389", offset 8192, size 8192',
        'pg_rewind: received chunk for file "base/5/16389", offset 32768, size 8192',
        'pg_rewind: received chunk for file "pg_wal/00000002000000000000000A", offset 0, size 1000000',
        'pg_rewind: received chunk for file "base/5/16384_vm", offset 0, size 8192',
    ])

    def test_actions_blocks_bytes_and_size(self):
        parsed = unionscan.parse_rewind_debug(self.TEXT)
        self.assertEqual(parsed['need_mb'], 97)
        self.assertEqual(parsed['actions']['base/5/16399'], 'COPY_TAIL')
        self.assertEqual(parsed['bytes']['base/5/16389'], 16384)
        self.assertEqual(unionscan.rewind_blocks_by_relation(parsed),
                         {'base/5/16389': {1, 4, summaryscan.SEGMENT_BLOCKS + 2}})

    def test_copy_kinds(self):
        kinds = [unionscan.copy_kind(p) for p in ('base/5/16389.1', 'base/5/16384_vm', 'pg_wal/00000002000000000000000A',
                                                   'pg_wal/summaries/x.summary', 'global/pg_control')]
        self.assertEqual(kinds, ['relation_blocks', 'fsm_vm_init', 'wal_segments', 'wal_summaries', 'other'])



def heap_page_with(tuples):
    """A heap page with one tuple per (xmin, infomask)."""
    page = bytearray(heap_page([mask for _, mask in tuples]))
    for i, (xmin, _) in enumerate(tuples):
        struct.pack_into('<I', page, BLOCK - 24 * (i + 1), xmin)
    return bytes(page)


def xact_file(root, statuses):
    """pg_xact/0000 with {xid: status bits}."""
    (root / 'pg_xact').mkdir(parents=True)
    data = bytearray(unionscan.XACT_SEGMENT_BYTES)
    for xid, bits in statuses.items():
        data[xid // 4] |= bits << (2 * (xid % 4))
    (root / 'pg_xact' / '0000').write_bytes(bytes(data))
    return root / 'pg_xact'


class FatesAndCopy(unittest.TestCase):
    """EXP-69b: a hint is dangerous only if S's pg_xact denies it; idea 1's copy is B's pages plus the sent ones."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.xact = xact_file(self.root / 's', {791: 1, 900: 2})  # 791 committed, 900 aborted, 950 never ended

    def test_status_and_contradiction(self):
        self.assertEqual([unionscan.xact_status(self.xact, x) for x in (791, 900, 950)],
                         ['committed', 'aborted', 'in progress'])
        self.assertFalse(unionscan.contradicts(unionscan.HEAP_XMIN_COMMITTED, 791, self.xact))
        self.assertTrue(unionscan.contradicts(unionscan.HEAP_XMIN_COMMITTED, 950, self.xact))
        self.assertTrue(unionscan.contradicts(unionscan.HEAP_XMIN_INVALID, 791, self.xact))
        self.assertFalse(unionscan.contradicts(unionscan.HEAP_XMIN_COMMITTED, 2, self.xact))

    def layout(self, b_tuples, s_tuples):
        for side, tuples in (('b', b_tuples), ('src', s_tuples)):
            rel = self.root / side / 'base' / '5'
            rel.mkdir(parents=True, exist_ok=True)
            (rel / '16384').write_bytes(heap_page_with(tuples) + heap_page_with(tuples))
        return self.root / 'src', self.root / 'b'

    def test_only_a_contradicted_hint_on_an_unsent_page_is_dangerous(self):
        committed = unionscan.HEAP_XMIN_COMMITTED
        source, old = self.layout([(791, committed), (950, committed)], [(791, 0), (950, 0)])
        found = unionscan.dangerous_hints(source, old, {}, self.xact)
        self.assertEqual((found['tuples'], found['pages']), (2, 2))  # 950's hint, on both pages
        self.assertEqual(found['examples'][0]['xmin'], 950)
        sent = unionscan.dangerous_hints(source, old, {'base/5/16384': [{0, 1}, None]}, self.xact)
        self.assertEqual(sent['tuples'], 0)

    def test_materialize_takes_s_pages_only_where_sent_and_s_length(self):
        source, old = self.root / 'src', self.root / 'b'
        for root, pages in ((source, [b'S0', b'S1', b'S2']), (old, [b'B0', b'B1'])):
            (root / 'base' / '5').mkdir(parents=True)
            (root / 'base' / '5' / '16384').write_bytes(b''.join(x.ljust(BLOCK, b'.') for x in pages))
            (root / 'PG_VERSION').write_text('17\n' if root == source else 'old\n')
        target = unionscan.materialize(source, old, {'base/5/16384': [{1}, None]}, self.root / 'copy' / 'B')
        data = (target / 'base' / '5' / '16384').read_bytes()
        self.assertEqual([data[i * BLOCK:i * BLOCK + 2] for i in range(3)], [b'B0', b'S1', b'S2'])
        self.assertEqual((target / 'PG_VERSION').read_text(), '17\n')

    def test_where_block(self):
        source, old = self.root / 'src', self.root / 'b'
        for root, n in ((source, 2), (old, 3)):
            (root / 'base' / '5').mkdir(parents=True)
            (root / 'base' / '5' / '16384').write_bytes(bytes(BLOCK * n))
        self.assertEqual(unionscan.where_block('base/5/16384', 1, source, old), 'inside both files')
        self.assertEqual(unionscan.where_block('base/5/16384', 2, source, old), "beyond S's end of file")
        self.assertEqual(unionscan.where_block('base/5/16999', 0, source, old), 'file missing on a side')


class GuardWithFixedEnd(unittest.TestCase):
    def test_reaching_the_end_is_what_counts_when_known(self):
        path = [(1, 0, parse_lsn('0/5000000')), (2, parse_lsn('0/5000000'), summaryscan.END)]
        summaries = Guard.summaries
        c = Guard.c
        args = (INFO, INFO, 1, parse_lsn('0/6000108'), path, summaries, parse_lsn('0/8000000'), c,
                parse_lsn('0/60000E0'))
        self.assertFalse(unionscan.guard(*args)[0])  # EXP-69's comparison: the last record's start is before the end
        self.assertTrue(unionscan.guard(*args, reaches=True)[0])
        self.assertFalse(unionscan.guard(*args, reaches=False)[0])


if __name__ == '__main__':
    unittest.main()
