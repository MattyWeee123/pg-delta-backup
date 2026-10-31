"""EXP-92's arm: EXP-91's two changes (--hash-ahead, --verify-scope as-written) with the receiver's syncs on its worker
(--fsync worker), as the VM session runs them. The worker changes when files are synced, never what is written: every
refresh must equal the source, and a misplaced write must still fail and leave B blocked.
"""

import os
import shutil
from unittest import mock

from pgdelta import receiver as receiver_module
from pgdelta.errors import VerificationError
from pgdelta.inplace import CONTROL_FILE, MARKER

from pgdelta_support import PAGE, transfer
from test_pgdelta_engine import CONFIGS, settle
from test_pgdelta_exp91 import heavy_trees
from test_pgdelta_inplace import InPlaceTestCase, in_place


class SyncsOnWorkerTests(InPlaceTestCase):
    def options(self, config=None, hash_ahead=1 << 30):
        options = in_place(config) if config else {'receiver_options': {'in_place': True}}
        options['receiver_options'] = dict(options['receiver_options'], verify_scope='as-written', fsync='worker')
        return dict(options, hash_ahead=hash_ahead, read_ahead=2)

    def reset(self):
        shutil.rmtree(self.root / 'S', ignore_errors=True)
        shutil.rmtree(self.root / 'B', ignore_errors=True)

    def test_all_three_on_every_engine_and_window(self):
        # A window of 6 pages holds less than one file (the planner waits for room); 1 GiB holds them all.
        for name, config in CONFIGS.items():
            for hash_ahead in (6 * PAGE, 1 << 30):
                with self.subTest(config=name, hash_ahead=hash_ahead):
                    self.reset()
                    heavy_trees(self.source, self.basis, seed=len(name))
                    outcome = transfer(self.source, self.basis, None, **self.options(config, hash_ahead))
                    self.assert_refreshed(outcome)
                    self.assertEqual(outcome.receiver['fsync'], 'worker')
                    self.assertEqual(outcome.receiver['verify_scope'], 'as-written')
                    self.assertEqual(outcome.sender['hash_ahead']['bytes'], hash_ahead)
                    counts = outcome.receiver['verify_written']
                    self.assertEqual(counts['pages_read_back'], outcome.receiver['pages_received'])
                    settle(self)

    def test_the_misplaced_write_switch_is_caught(self):
        # EXP-32's negative control, with the syncs on the worker: caught, B left blocked, then repaired.
        for hash_ahead in (6 * PAGE, 1 << 30):
            with self.subTest(hash_ahead=hash_ahead):
                self.reset()
                heavy_trees(self.source, self.basis)
                with mock.patch.dict(os.environ, {receiver_module.MISPLACE_ENV: '1'}):
                    outcome = transfer(self.source, self.basis, None, **self.options(hash_ahead=hash_ahead))
                self.assertIsInstance(outcome.receiver_error, VerificationError)
                self.assertTrue(os.path.lexists(self.basis / MARKER))
                self.assertFalse(os.path.lexists(self.basis / CONTROL_FILE))  # blocked from starting
                rerun = transfer(self.source, self.basis, None, **self.options(hash_ahead=hash_ahead))
                self.assert_refreshed(rerun)
                settle(self)
