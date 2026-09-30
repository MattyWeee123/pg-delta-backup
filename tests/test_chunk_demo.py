"""Byte-level correctness and failure tests for the educational experiment."""

import random
import unittest
from dataclasses import replace

from experiments.chunk_demo import Copy, Literal, Plan, digest, plan_transfer, reconstruct


class ChunkDemoTests(unittest.TestCase):
    def test_exact_reconstruction_across_edit_shapes(self):
        basis = bytes(range(256)) * 3
        targets = {
            'empty': b'',
            'identical': basis,
            'overwrite': basis[:127] + b'x' + basis[128:],
            'append': basis + b'new bytes',
            'truncate': basis[:137],
            'shift': b'prefix' + basis,
            'reorder': basis[128:256] + basis[:128],
            'rewrite': b'completely unrelated bytes',
        }
        for name, target in targets.items():
            for chunk_size in (1, 17, 128, 4096):
                with self.subTest(edit=name, chunk=chunk_size):
                    plan = plan_transfer(basis, target, chunk_size)
                    self.assertEqual(reconstruct(basis, plan), target)

    def test_empty_basis_and_input(self):
        for target in (b'', b'abc'):
            self.assertEqual(reconstruct(b'', plan_transfer(b'', target, 8)), target)

    def test_seeded_random_edits(self):
        rng = random.Random(49)
        for case in range(100):
            basis = rng.randbytes(rng.randrange(2000))
            left = rng.randrange(len(basis) + 1)
            right = rng.randrange(left, len(basis) + 1)
            target = basis[:left] + rng.randbytes(rng.randrange(200)) + basis[right:]
            with self.subTest(case=case):
                self.assertEqual(
                    reconstruct(basis, plan_transfer(basis, target, rng.randrange(1, 128))),
                    target,
                )

    def test_identical_and_reordered_blocks_are_reused(self):
        basis = b'abcdEFGH'
        for target in (basis, b'EFGHabcd'):
            plan = plan_transfer(basis, target, 4)
            self.assertTrue(all(isinstance(op, Copy) for op in plan.operations))
            self.assertEqual(reconstruct(basis, plan), target)

    def test_mutated_basis_is_rejected(self):
        plan = plan_transfer(b'abcdefgh', b'abcdefgh', 4)
        with self.assertRaises(ValueError):
            reconstruct(b'xbcdefgh', plan)

    def test_corrupt_literal_is_rejected(self):
        plan = plan_transfer(b'', b'new data', 4)
        damaged = replace(plan.operations[0], data=b'evil')
        with self.assertRaises(ValueError):
            reconstruct(b'', replace(plan, operations=(damaged,) + plan.operations[1:]))

    def test_truncated_transfer_is_rejected(self):
        plan = plan_transfer(b'', b'abcdefgh', 4)
        with self.assertRaises(ValueError):
            reconstruct(b'', replace(plan, operations=plan.operations[:-1]))

    def test_wrong_whole_file_digest_is_rejected(self):
        plan = plan_transfer(b'', b'abc', 4)
        with self.assertRaises(ValueError):
            reconstruct(b'', replace(plan, target_digest=digest(b'other')))

    def test_invalid_ranges_are_rejected(self):
        for offset, length in ((-1, 1), (0, -1), (0, 0), (7, 2), (2**64, 1)):
            with self.subTest(offset=offset, length=length):
                invalid = Plan((Copy(offset, length, digest(b'a')),), 1, digest(b'a'), 1)
                with self.assertRaises(ValueError):
                    reconstruct(b'abcdefgh', invalid)

    def test_output_overrun_is_rejected(self):
        operation = Literal(b'abc', digest(b'abc'))
        with self.assertRaises(ValueError):
            reconstruct(b'', Plan((operation,), 2, digest(b'ab'), 0))

    def test_invalid_chunk_size_is_rejected(self):
        for size in (0, -1):
            with self.assertRaises(ValueError):
                plan_transfer(b'', b'', size)


if __name__ == '__main__':
    unittest.main()
