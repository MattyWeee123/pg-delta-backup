from pathlib import Path
import tempfile
import unittest

from benchmarks.provenance import source_provenance


class SourceProvenanceTests(unittest.TestCase):
    def test_uncommitted_edits_change_hash_and_exports_keep_same_hash(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            roots = [Path(a), Path(b)]
            for root in roots:
                (root / 'benchmarks').mkdir()
                (root / 'pgdelta').mkdir()
                (root / 'benchmarks' / 'runner.py').write_text('print(1)\n')
                (root / 'pgdelta' / 'engine.py').write_text('pass\n')
            first, exported = map(source_provenance, roots)
            self.assertEqual(first['code_sha256'], exported['code_sha256'])
            self.assertEqual(first['python_source_files'], 2)
            (roots[0] / 'benchmarks' / 'runner.py').write_text('print(2)\n')
            self.assertNotEqual(first['code_sha256'], source_provenance(roots[0])['code_sha256'])

    def test_new_modules_are_hashed_but_results_are_not(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'benchmarks').mkdir()
            before = source_provenance(root)['code_sha256']
            (root / 'benchmarks' / 'new.py').write_text('pass\n')
            changed = source_provenance(root)['code_sha256']
            self.assertNotEqual(before, changed)
            (root / 'results.json').write_text('{}')
            self.assertEqual(changed, source_provenance(root)['code_sha256'])


if __name__ == '__main__':
    unittest.main()
