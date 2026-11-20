"""O, its staging directory and its receipt never land inside S or B, judged by filesystem identity.

Path strings can disguise one directory as another: a case-insensitive or
normalization-insensitive filesystem (macOS APFS by default), a bind mount,
a hard-linked directory, or a parent directory replaced after the first
check. The tests that need a case- or normalization-insensitive filesystem
skip elsewhere; the swap and simulated-mount tests run everywhere.
"""
import os
from pathlib import Path
import random
import subprocess
import tempfile
import unicodedata
import unittest
from unittest import mock

from pgdelta import placement
from pgdelta.errors import InvalidInput
from pgdelta.receiver import prepare_receiver, receipt_path
from pgdelta.sender import run_sender

from pgdelta_support import (PAGE, REPO_ROOT, Outcome, channel_pair, python_command, run_receiver_thread,
                             snapshot, staging_dirs, subprocess_env, write_tree)


def case_insensitive(directory):
    probe = Path(tempfile.mkdtemp(prefix='CaseProbe', dir=directory))
    try:
        return os.path.lexists(probe.with_name(probe.name.lower()))
    finally:
        probe.rmdir()


def normalization_insensitive(directory):
    composed = Path(directory) / unicodedata.normalize('NFC', 'probe-café')
    composed.mkdir()
    try:
        return os.path.lexists(Path(directory) / unicodedata.normalize('NFD', 'probe-café'))
    finally:
        composed.rmdir()


def run_cli(*args):
    return subprocess.run(python_command(*args), capture_output=True, text=True, cwd=REPO_ROOT,
                          env=subprocess_env(), timeout=120)


class PlacementTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        rng = random.Random(70)
        self.source = write_tree(self.root / 'Source', {'base/1/rel': rng.randbytes(5 * PAGE),
                                                        'PG_VERSION': b'17\n'})
        self.basis = write_tree(self.root / 'Basis', {'base/1/rel': rng.randbytes(5 * PAGE)})
        self.before = {'source': snapshot(self.source), 'basis': snapshot(self.basis)}

    def assert_inputs_untouched(self):
        self.assertEqual(snapshot(self.source), self.before['source'])
        self.assertEqual(snapshot(self.basis), self.before['basis'])

    def transfer_with_setup(self, setup):
        send_channel, receive_channel = channel_pair()
        outcome = Outcome()
        thread = run_receiver_thread(receive_channel, outcome, None, None, setup=setup)
        try:
            outcome.sender = run_sender(send_channel, self.source, node_blocks=4)
        except BaseException as exc:
            outcome.sender_error = exc
        finally:
            send_channel.close()
        thread.join(60)
        self.assertFalse(thread.is_alive())
        return outcome


class AliasTests(PlacementTestCase):
    def setUp(self):
        super().setUp()
        if not case_insensitive(self.root):
            self.skipTest('needs a case-insensitive filesystem')

    def test_output_inside_basis_by_case_alias_is_refused_by_the_receiver(self):
        output = self.root / 'basis' / 'O'
        with self.assertRaisesRegex(InvalidInput, 'overlaps basis'):
            prepare_receiver(self.basis, output)
        self.assertFalse(os.path.lexists(self.basis / 'O'))
        self.assert_inputs_untouched()

    def test_cli_local_output_inside_basis_by_case_alias_exits_2(self):
        # The reproduction from the review: this used to exit 0 and create Basis/O.
        result = run_cli('local', '--source', self.source, '--basis', self.basis,
                         '--output', self.root / 'basis' / 'O', '--assume-same-cluster')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('overlap', result.stderr)
        self.assertEqual(staging_dirs(self.basis / 'O'), [])
        self.assert_inputs_untouched()

    def test_cli_local_output_inside_source_by_case_alias_exits_2(self):
        result = run_cli('local', '--source', self.source, '--basis', self.basis,
                         '--output', self.root / 'source' / 'O')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('overlap', result.stderr)
        self.assert_inputs_untouched()

    def test_cli_local_source_and_basis_that_are_one_directory_exit_2(self):
        result = run_cli('local', '--source', self.source, '--basis', self.root / 'source',
                         '--output', self.root / 'O')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('same directory', result.stderr)
        self.assertFalse(os.path.lexists(self.root / 'O'))

    def test_cli_local_basis_inside_source_by_case_alias_exits_2(self):
        nested = write_tree(self.source / 'old', {'base/1/rel': b'x' * PAGE})
        self.before['source'] = snapshot(self.source)
        result = run_cli('local', '--source', self.source, '--basis', self.root / 'source' / nested.name,
                         '--output', self.root / 'O')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('overlap', result.stderr)
        self.assertFalse(os.path.lexists(self.root / 'O'))


class NormalizationAliasTests(PlacementTestCase):
    def test_output_inside_basis_by_normalization_alias_is_refused(self):
        if not normalization_insensitive(self.root):
            self.skipTest('needs a normalization-insensitive filesystem')
        basis = self.root / unicodedata.normalize('NFC', 'café')
        self.basis.rename(basis)
        self.basis = basis
        output = self.root / unicodedata.normalize('NFD', 'café') / 'O'
        with self.assertRaisesRegex(InvalidInput, 'overlaps basis'):
            prepare_receiver(self.basis, output)
        self.assertEqual(snapshot(self.basis), self.before['basis'])


class LateChangeTests(PlacementTestCase):
    """The check is repeated right before the staging directory is created."""

    def test_output_parent_replaced_by_a_link_into_basis_after_the_first_check(self):
        parent = self.root / 'parent'
        parent.mkdir()
        setup = prepare_receiver(self.basis, parent / 'O')
        parent.rename(self.root / 'parent-moved')
        parent.symlink_to(self.basis, target_is_directory=True)
        outcome = self.transfer_with_setup(setup)
        self.assertIsInstance(outcome.receiver_error, InvalidInput)
        self.assertIn('overlaps basis', str(outcome.receiver_error))
        self.assertEqual(outcome.sender_error.exit_code, 2)
        self.assertFalse(os.path.lexists(self.basis / 'O'))
        self.assertEqual(staging_dirs(self.basis / 'O'), [])
        self.assertFalse(os.path.lexists(receipt_path(self.basis / 'O')))
        self.assert_inputs_untouched()

    def test_output_parent_replaced_by_a_link_into_a_local_source(self):
        parent = self.root / 'parent'
        parent.mkdir()
        setup = prepare_receiver(self.basis, parent / 'O', local_source=self.source)
        parent.rename(self.root / 'parent-moved')
        parent.symlink_to(self.source, target_is_directory=True)
        outcome = self.transfer_with_setup(setup)
        self.assertIsInstance(outcome.receiver_error, InvalidInput)
        self.assertIn('overlaps source', str(outcome.receiver_error))
        self.assertFalse(os.path.lexists(self.source / 'O'))
        self.assert_inputs_untouched()

    def test_staging_that_lands_inside_basis_is_removed_and_refused(self):
        # The parent changes between the last check and mkdtemp: the check of the new staging catches it.
        parent = self.root / 'parent'
        parent.mkdir()
        setup = prepare_receiver(self.basis, parent / 'O')
        real_check = placement.refuse_inside
        calls = []

        def swap_after_check(directory, protected, what):
            real_check(directory, protected, what)
            calls.append(directory)
            if len(calls) == 1:
                parent.rename(self.root / 'parent-moved')
                parent.symlink_to(self.basis, target_is_directory=True)

        with mock.patch('pgdelta.receiver.placement.refuse_inside', side_effect=swap_after_check):
            outcome = self.transfer_with_setup(setup)
        self.assertIsInstance(outcome.receiver_error, InvalidInput)
        self.assertIn('overlaps basis', str(outcome.receiver_error))
        self.assertEqual(staging_dirs(self.basis / 'O'), [])
        self.assert_inputs_untouched()


class SimulatedMountTests(PlacementTestCase):
    def test_parent_reached_through_a_mount_of_a_basis_subdirectory_is_refused(self):
        # A bind mount of B/base at /mnt/x makes /mnt/x/O lie inside B although walking up from
        # /mnt/x never meets B itself. B's scan records every directory's identity, so it is caught.
        output_parent = self.root / 'elsewhere'
        output_parent.mkdir()
        inside = os.stat(self.basis / 'base')
        fake_chain = [(inside.st_dev, inside.st_ino), placement.directory_id(self.root, 'root')]
        with mock.patch('pgdelta.placement.lineage', return_value=fake_chain):
            with self.assertRaisesRegex(InvalidInput, 'overlaps basis'):
                prepare_receiver(self.basis, output_parent / 'O')


class PortableTests(PlacementTestCase):
    def test_output_parent_that_is_a_symlink_into_basis_is_refused(self):
        link = self.root / 'link'
        link.symlink_to(self.basis / 'base', target_is_directory=True)
        with self.assertRaisesRegex(InvalidInput, 'overlaps basis'):
            prepare_receiver(self.basis, link / 'O')

    def test_sibling_output_is_accepted(self):
        setup = prepare_receiver(self.basis, self.root / 'O')
        self.assertEqual(setup.output, self.root / 'O')

    def test_cli_local_same_directory_through_a_symlink_exits_2(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.source, target_is_directory=True)
        result = run_cli('local', '--source', self.source, '--basis', alias, '--output', self.root / 'O')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('same directory', result.stderr)


if __name__ == '__main__':
    unittest.main()
