"""Tests for EXP-90's analysis (analysis/exp90.py): the disk model that scores D1 and D4, and the sampler's format.

They need the analysis venv's matplotlib and numpy and are skipped without them. Nothing is written to docs/.
"""

import sys
import tempfile
import unittest
from pathlib import Path

try:
    import matplotlib  # noqa: F401
    import numpy as np
except ImportError:
    matplotlib = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'analysis'))


@unittest.skipIf(matplotlib is None, 'needs the analysis venv (matplotlib, numpy)')
class DiskModel(unittest.TestCase):
    CAPS = {'read_bw': 200e6, 'read_ops': 4000, 'write_bw': 200e6, 'write_ops': 4000}

    def caps(self, r_mix, w_mix):
        return dict(self.CAPS, r_mix=r_mix, w_mix=w_mix)

    def test_a_shared_disk_adds_reads_and_writes(self):
        import exp90
        # 100 s of reads and 50 s of writes at their own limits take 150 s where they share one budget...
        self.assertAlmostEqual(exp90.disk_use(self.caps(0.5, 0.5), 200e8, 0, 0, 200000), 150.0)
        # ...and 100 s, the larger, where they don't.
        self.assertAlmostEqual(exp90.disk_use(self.caps(1.0, 1.0), 200e8, 0, 0, 200000), 100.0)

    def test_the_larger_of_bytes_and_operations_counts(self):
        import exp90
        # 1,000 reads of 8 KiB: 8.2 MB is 0.04 s by bytes, but 1,000 operations take 0.25 s.
        self.assertAlmostEqual(exp90.disk_use(self.caps(1.0, 1.0), 8192e3, 1000, 0, 0), 0.25)
        # 400 MB in 400 reads of 1 MiB: 2 s by bytes.
        self.assertAlmostEqual(exp90.disk_use(self.caps(1.0, 1.0), 400e6, 400, 0, 0), 2.0)


@unittest.skipIf(matplotlib is None, 'needs the analysis venv (matplotlib, numpy)')
class SamplerFormat(unittest.TestCase):
    def test_series_reads_samples_and_threads(self):
        import exp90
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'util.txt'
            path.write_text('# util_series interface=ens4 device=sdb interval=0.1 hz=100 cpus=2\n'
                            'S 10.0 0 0 0 0 0 0 0 0 0 0 0 0 0\n'
                            'T 10.0 7 7 100 python3\n'
                            'S 11.0 5 1000 2 2048 3 16 900 50 200 25 100 25 100\n'
                            'T 11.0 7 7 180 python3\n')
            head, times, rows, threads = exp90.series(path)
            self.assertEqual(head['hz'], '100')
            self.assertEqual(list(times), [10.0, 11.0])
            delta = exp90.counters(times, rows, 10.75) - exp90.counters(times, rows, 10.25)
            self.assertAlmostEqual(delta[exp90.TX], 500.0)
            self.assertAlmostEqual(delta[exp90.WSECT] * 512, 4096.0)
            self.assertAlmostEqual(delta[exp90.BUSYJ] / delta[exp90.ALLJ], 0.25)
            name, ts, ticks = threads[('7', '7')]
            self.assertEqual((name, ticks), ('python3', [100, 180]))
            self.assertTrue(np.allclose(exp90.at(ts, ticks, [10.0, 11.0]), [100, 180]))


@unittest.skipIf(matplotlib is None, 'needs the analysis venv (matplotlib, numpy)')
class OperationUnits(unittest.TestCase):
    def test_an_operation_over_256_kib_counts_once_per_256_kib(self):
        import exp90
        import exp90_overlap
        d = np.zeros(14)
        # fio's mixed point: 196 reads of 1 MiB and 3,408 writes of 8 KiB in a second are 784 + 3,408 units.
        d[exp90.READS], d[exp90.RSECT] = 196, 196 * 2048
        d[exp90.WRITES], d[exp90.WSECT] = 3408, 3408 * 16
        self.assertAlmostEqual(float(exp90_overlap.units(d)), 4192.0)

if __name__ == '__main__':
    unittest.main()
