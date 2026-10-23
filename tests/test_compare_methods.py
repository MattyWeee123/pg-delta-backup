"""compare_methods (EXP-02) runs pgdelta with EXP-02's engine, whatever pgdelta's defaults are."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from experiments.change_patterns import compare_methods


class RunPgdeltaTests(unittest.TestCase):
    def run_pgdelta(self, **kwargs):
        commands = []

        def measured(command, log, **_):
            commands.append(command)
            return 1, 0.5, 0.25  # failed: no statistics and no identity check to do

        with tempfile.TemporaryDirectory() as work, mock.patch.object(compare_methods, 'measured', measured):
            result = compare_methods.run_pgdelta(Path(work, 'B0'), Path(work, 'B1'), work, **kwargs)
        self.assertEqual(len(commands), 1)
        return commands[0], result

    def test_engine_flags_are_explicit_by_default(self):
        command, result = self.run_pgdelta(pg_controldata='/bin/pg_controldata')
        self.assertEqual(compare_methods.PGDELTA_ENGINE,
                         ('--jobs', '1', '--rebuild', 'gap-fill', '--verify', 'sequential'))
        start = command.index('--jobs')
        self.assertEqual(tuple(command[start:start + 6]), compare_methods.PGDELTA_ENGINE)
        self.assertEqual(command[-2:], ['--pg-controldata', '/bin/pg_controldata'])
        self.assertEqual(result['flags'], list(compare_methods.PGDELTA_ENGINE))

    def test_another_engine_can_be_measured_and_is_recorded(self):
        command, result = self.run_pgdelta(pgdelta_flags=('--jobs', '4'), extra=('--node-blocks', '64'))
        self.assertEqual(command.count('--jobs'), 1)
        self.assertIn('--rebuild', compare_methods.PGDELTA_ENGINE)
        self.assertNotIn('--rebuild', command)
        self.assertEqual(result['flags'], ['--jobs', '4', '--node-blocks', '64'])


class RealRunTests(unittest.TestCase):
    def test_worker_processes_are_counted_and_their_start_method_recorded(self):
        # With --jobs 2 the CPU includes the workers only because they are spawned children of
        # pgdelta (EXP-06 review); the result says which start method both ends used.
        import random
        with tempfile.TemporaryDirectory() as temp:
            rng = random.Random(9)
            for name, data in (('B0', rng.randbytes(3 << 20)), ('B1', None)):
                Path(temp, name, 'base').mkdir(parents=True)
            old = rng.randbytes(3 << 20)
            Path(temp, 'B0', 'base', 'f').write_bytes(old)
            Path(temp, 'B1', 'base', 'f').write_bytes(bytes(8192) + old[8192:])
            Path(temp, 'work').mkdir()
            result = compare_methods.run_pgdelta(Path(temp, 'B0'), Path(temp, 'B1'), Path(temp, 'work'),
                                                 pgdelta_flags=('--jobs', '2', '--node-blocks', '4'),
                                                 extra=('--reuse-any-basis',))
        self.assertEqual(result['exit_code'], 0)
        self.assertTrue(result['identical'])
        self.assertEqual(result['worker_start_method'], {'sender': 'spawn', 'receiver': 'spawn'})
        self.assertTrue(result['cpu_counts_workers'])
        self.assertEqual(result['switches']['receiver']['jobs'], 2)


if __name__ == '__main__':
    unittest.main()
