"""Directory inventories, plain-backup refusal and validation of peer-supplied paths."""
import os
from pathlib import Path
import tempfile
import unittest

from pgdelta.errors import InvalidInput, UnsafePath
from pgdelta.inventory import DirEntry, FileEntry, parse_entries, require_plain, scan, validate_relpath

from pgdelta_support import write_tree


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'data'

    def test_lists_files_and_directories_with_sizes_and_modes(self):
        write_tree(self.root, {'PG_VERSION': b'17\n', 'base/1/1259': b'x' * 9000, 'global/pg_control': b''},
                   directories=['pg_wal/archive_status', 'pg_tblspc'],
                   modes={'base/1/1259': 0o640, 'pg_wal': 0o750})
        inventory = require_plain(scan(self.root))
        self.assertEqual(inventory.files, [
            FileEntry('PG_VERSION', 3, 0o644 & ~_umask()),
            FileEntry('base/1/1259', 9000, 0o640),
            FileEntry('global/pg_control', 0, 0o644 & ~_umask()),
        ])
        self.assertEqual([entry.path for entry in inventory.directories],
                         ['base', 'base/1', 'global', 'pg_tblspc', 'pg_wal', 'pg_wal/archive_status'])
        self.assertIn(DirEntry('pg_wal', 0o750), inventory.directories)

    def test_symlinks_are_listed_and_refused(self):
        write_tree(self.root, {'a': b'1'})
        (self.root / 'pg_wal').symlink_to(self.root / 'a')
        inventory = scan(self.root)
        self.assertEqual(inventory.symlinks, ['pg_wal'])
        with self.assertRaisesRegex(InvalidInput, 'symlinks'):
            require_plain(inventory)

    def test_tablespaces_are_refused(self):
        write_tree(self.root, {'PG_VERSION': b'17\n'}, directories=['pg_tblspc'])
        (self.root / 'pg_tblspc' / '16384').symlink_to(self.temp.name)
        with self.assertRaisesRegex(InvalidInput, 'tablespaces'):
            require_plain(scan(self.root))
        (self.root / 'pg_tblspc' / '16384').unlink()
        (self.root / 'pg_tblspc' / '16385').mkdir()  # in-place tablespace
        with self.assertRaisesRegex(InvalidInput, 'tablespaces'):
            require_plain(scan(self.root))

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'needs mkfifo')
    def test_special_files_are_refused(self):
        write_tree(self.root, {'a': b'1'})
        os.mkfifo(self.root / 'fifo')
        with self.assertRaisesRegex(InvalidInput, 'special files'):
            require_plain(scan(self.root))

    def test_missing_root_is_invalid_input(self):
        with self.assertRaises(InvalidInput):
            scan(self.root / 'missing')


def _umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


class PathValidationTests(unittest.TestCase):
    def test_accepts_normalized_relative_paths(self):
        for path in ('a', 'base/1/1259', 'pg_wal/000000010000000000000001', 'a.b/..c/c..', '...'):
            self.assertEqual(validate_relpath(path), path)

    def test_rejects_unsafe_or_unnormalized_paths(self):
        for path in ('', '/etc/passwd', '..', '../x', 'a/../../x', 'a/..', 'a//b', 'a/', './a', 'a/./b',
                     '.', 'a\x00b', 'a\\..\\b', 'x' * 256, 'a/' * 2100, None, 5, b'a'):
            with self.subTest(path=path), self.assertRaises(UnsafePath):
                validate_relpath(path)

    def test_parse_entries_checks_structure(self):
        dirs, files = parse_entries([['base', 0o700], ['base/1', 0o700]], [['base/1/1', 10, 0o600]])
        self.assertEqual(files, [FileEntry('base/1/1', 10, 0o600)])
        self.assertEqual(len(dirs), 2)
        bad = [
            ([], [['missing/1', 1, 0o600]]),                      # parent not listed
            ([['a', 0o700]], [['a', 1, 0o600]]),                  # file and directory
            ([['a', 0o700], ['a', 0o700]], []),                   # duplicate
            ([], [['f', -1, 0o600]]),                             # negative size
            ([], [['f', True, 0o600]]),                           # bool is not a size
            ([], [['f', 1, 0o4777]]),                             # setuid bits
            ([], [['f', 1]]),                                     # short entry
            ([['../up', 0o700]], []),                             # traversal in a directory
            ({}, []),
        ]
        for directories, entries in bad:
            with self.subTest(directories=directories, files=entries), self.assertRaises(InvalidInput):
                parse_entries(directories, entries)


if __name__ == '__main__':
    unittest.main()
