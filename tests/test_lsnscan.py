import struct
import tempfile
import unittest
from pathlib import Path

from experiments.tricky_copies.lsnscan import (BLOCK, format_lsn, page_lsn, parse_lsn,
                                               relation_kind, scan)


def page(lsn, fill=b'\0'):
    header = struct.pack('<II', lsn >> 32, lsn & 0xFFFFFFFF)
    return header + fill * (BLOCK - len(header))


class LsnScanTest(unittest.TestCase):
    def test_lsn_round_trip(self):
        value = parse_lsn('1/2A000028')
        self.assertEqual(value, (1 << 32) | 0x2A000028)
        self.assertEqual(format_lsn(value), '1/2A000028')
        self.assertEqual(page_lsn(page(value)), value)

    def test_relation_kind(self):
        self.assertEqual(relation_kind('base/5/16384'), 'main')
        self.assertEqual(relation_kind('base/5/16384.1'), 'main')
        self.assertEqual(relation_kind('base/5/16384_fsm'), 'fsm')
        self.assertEqual(relation_kind('global/1262_vm'), 'vm')
        self.assertIsNone(relation_kind('pg_xact/0000'))
        self.assertIsNone(relation_kind('backup_label'))

    def test_counts_wrong_skips(self):
        redo = parse_lsn('0/1000')
        with tempfile.TemporaryDirectory() as tmp:
            source, old = Path(tmp, 's'), Path(tmp, 'o')
            for root in (source, old):
                (root / 'base/5').mkdir(parents=True)
                (root / 'pg_xact').mkdir(parents=True)
            # Block 0: old LSN, identical -> correctly skipped.
            # Block 1: old LSN, but bytes differ (e.g. FILE_COPY or checksum rewrite) -> wrong skip.
            # Block 2: new LSN, changed -> sent.
            # Block 3: new on source only -> sent, not compared as skippable.
            source_pages = [page(0x10, b'a'), page(0x20, b'b'), page(0x2000, b'c'), page(0x2100, b'd')]
            old_pages = [page(0x10, b'a'), page(0x20, b'x'), page(0x30, b'c')]
            (source / 'base/5/16384').write_bytes(b''.join(source_pages))
            (old / 'base/5/16384').write_bytes(b''.join(old_pages))
            (source / 'pg_xact/0000').write_bytes(b'z' * BLOCK)
            (old / 'pg_xact/0000').write_bytes(b'y' * BLOCK)
            result = scan(source, old, redo)
        totals = result['totals']
        self.assertEqual(totals['pages_compared'], 4)
        self.assertEqual(totals['pages_skipped'], 2)
        self.assertEqual(totals['pages_wrongly_skipped'], 1)
        self.assertEqual(totals['pages_changed'], 3)
        self.assertEqual(totals['pages_changed_and_sent'], 2)
        self.assertEqual(result['wrongly_skipped_examples'][0]['block'], 1)


if __name__ == '__main__':
    unittest.main()
