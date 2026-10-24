"""EXP-13's pgdelta switches: the request window (--window) and the protocol timers (pgdelta.waits).

At the defaults nothing on the wire changes: one fixed session (tests/pgdelta_frames.py) puts exactly
the bytes on the wire that it put before either was built.
"""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

from pgdelta_frames import build, session_fingerprint
from pgdelta_support import REPO_ROOT

# pgdelta_frames' session as the code before EXP-13 sent it (commit BASE_COMMIT, recorded with
# `python tests/pgdelta_frames.py [WINDOW]`). The window orders the source-to-dest stream; nothing else.
BASE_COMMIT = '3c0b8fa'
BEFORE_SOURCE_TO_DEST = {
    None: '4394016e6275fcbd879e0edc5881ae6a2b287d078cd8e2f8d98512770ddd361e',
    1: '6fc7ec8c68411cb9b80329db20b237802a7dd83fe82e9cd6defacaefd089b62c',
    4: '4394016e6275fcbd879e0edc5881ae6a2b287d078cd8e2f8d98512770ddd361e',
    16: '739062a3ac5e86219135e5b3111cda1a78c40f3bbcd117e858fac8e66bb971eb',
}
BEFORE = {
    'source_to_dest_frames': {'COMPLETE': 1, 'FILE_DONE': 29, 'HELLO': 1, 'LEAF_REQUEST': 20, 'PAGE_DATA': 38},
    'dest_to_source': {'ACCEPT': '42358891ee5839a5faf520ac95d8936e42eb7e3229190f43ffcef097967fd285',
                       'BASIS_NODES': '9fb99aa5329937996a5e09c15542dd5756b9dde637011008832c3be838a7e3a8',
                       'LEAF_DIGESTS': '14d8cbf35d0f9cf66ca7d1536700856f5419b761f4451c50ed9c7e8b08d16e35'},
    'dest_to_source_frames': {'ACCEPT': 1, 'BASIS_NODES': 27, 'LEAF_DIGESTS': 20},
    'complete_published': True,
    'complete_receiver': 'b8d24c5dc0a237bec6e65b8d5984c2d3b485a36aaf5312eec1477edb733447ed',
    'complete_receiver_keys': [
        'basis_copies', 'basis_copy_bytes', 'basis_identity', 'bytes_copied', 'bytes_read', 'bytes_written',
        'copy_seconds', 'directories', 'files', 'files_digest', 'files_verified', 'fsync', 'fsync_effective',
        'fsync_mode', 'fsync_seconds', 'fsync_wait_seconds', 'gate_reason', 'hash_basis_seconds', 'in_place',
        'in_place_tree', 'jobs', 'leaf_nodes_cached', 'leaf_nodes_reread', 'mode', 'output',
        'page_bytes_received', 'pages_copied', 'pages_received', 'pool_first_ready_seconds',
        'pool_ready_seconds', 'pool_start_seconds', 'prefetch', 'published', 'rebuild_seconds', 'receipt',
        'reuse_policy', 'source_identity', 'switches', 'verify_seconds', 'verify_wait_seconds',
        'whole_file_copies', 'worker_start_method', 'writeback_calls', 'writeback_seconds'],
}


def before(window=None):
    return dict(BEFORE, source_to_dest=BEFORE_SOURCE_TO_DEST[window])


class TreeTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.basis = build(self.root)


class WireTests(TreeTestCase):
    """Byte for byte on the wire: the defaults, and --window 4, send what the code sent before EXP-13."""

    def test_the_defaults_put_the_same_bytes_on_the_wire_as_before(self):
        for window in (None, 4):
            with self.subTest(window=window):
                self.assertEqual(session_fingerprint(**({} if window is None else {'window': window})),
                                 before(window))

    def test_windows_1_and_16_send_what_the_same_window_sent_before(self):
        for window in (1, 16):
            with self.subTest(window=window):
                self.assertEqual(session_fingerprint(window=window), before(window))

    @unittest.skipUnless(shutil.which('git'), 'needs git')
    def test_the_commit_before_puts_the_same_bytes_on_the_wire(self):
        """The same session run by the code of BASE_COMMIT, in a child Python, against this code."""
        archive = subprocess.run(['git', '-C', str(REPO_ROOT), 'archive', '--format=tar', BASE_COMMIT, 'pgdelta'],
                                 capture_output=True, timeout=60)
        if archive.returncode:
            self.skipTest(f'commit {BASE_COMMIT} is not in this checkout (a shallow clone?)')
        old = self.root / 'before'
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            tar.extractall(old, filter='data')
        self.assertFalse((old / 'pgdelta' / 'waits.py').exists())
        env = {key: value for key, value in os.environ.items() if not key.startswith('PYTHON')}
        env['PYTHONPATH'] = str(old)
        for window in (4, 16):
            with self.subTest(window=window):
                result = subprocess.run([sys.executable, str(REPO_ROOT / 'tests' / 'pgdelta_frames.py'), str(window)],
                                        capture_output=True, text=True, env=env, timeout=120)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), session_fingerprint(window=window))
                self.assertEqual(json.loads(result.stdout), before(window))


if __name__ == '__main__':
    unittest.main()
