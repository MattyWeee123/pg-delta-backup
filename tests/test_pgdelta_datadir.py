"""Direct read of a stopped data directory (--source-kind datadir, T8a), with pg_basebackup's exclusions.

Every exclusion rule of PostgreSQL 17's base backup is checked on a
synthetic tree read with a stub pg_controldata, as are the refusals: no
pg_controldata, a cluster that is not shut down, a postmaster.pid. With
PostgreSQL binaries, a real stopped cluster refreshes a stale copy, both as
a new copy and in place, and the result starts without recovery.
"""
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from pgdelta import datadir, protocol
from pgdelta.datadir import select
from pgdelta.errors import InvalidInput, PeerError, VerificationError
from pgdelta.protocol import Channel

from pgdelta_cluster import CONTROLDATA, SKIP_REASON, Servers, available, initdb
from pgdelta_support import PAGE, REPO_ROOT, python_command, snapshot, staging_dirs, subprocess_env, transfer, write_tree
from test_pgdelta_identity import CRC_WARNING, SAMPLE

REDO = '000000010000000000000001'


def run(*args, timeout=180):
    return subprocess.run(python_command(*args), capture_output=True, text=True, cwd=REPO_ROOT,
                          env=subprocess_env(), timeout=timeout)


def list_selection(*args):
    """python -m pgdelta.datadir list ..."""
    return subprocess.run([sys.executable, '-m', 'pgdelta.datadir', 'list', *map(str, args)], capture_output=True,
                          text=True, cwd=REPO_ROOT, env=subprocess_env(), timeout=120)


def stub_controldata(directory, text=SAMPLE, stderr=''):
    """A pg_controldata stand-in printing `text`; rewrite controldata.txt to change what it says."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'controldata.txt').write_text(text)
    (directory / 'stderr.txt').write_text(stderr)
    stub = directory / 'pg_controldata'
    stub.write_text(f'#!/bin/sh\ncat "{directory}/controldata.txt"\ncat "{directory}/stderr.txt" >&2\n')
    stub.chmod(0o755)
    return stub


# path: (rule that leaves it out, or None if it is sent). Sizes are in pages.
TREE = {
    'PG_VERSION': None, 'postgresql.conf': None, 'postgresql.auto.conf': None, 'current_logfiles': None,
    'global/pg_control': None, 'global/1262': None,
    # PostgreSQL 17's sendDir treats global/ as a relation directory too: the unlogged rule applies there,
    # but the temporary-relation rule only in a database directory.
    'global/16385': 'unlogged_relations', 'global/16385_fsm': 'unlogged_relations', 'global/16385_init': None,
    'global/t3_16390': None,
    'global/pg_internal.init': 'named_files', 'global/pg_internal.init.4242': 'named_files',
    'base/1/1259': None, 'base/5/PG_VERSION': None,
    'base/5/16384': None, 'base/5/16384_fsm': None, 'base/5/16384_vm': None,  # logged relation
    'base/5/16385': 'unlogged_relations', 'base/5/16385.1': 'unlogged_relations',
    'base/5/16385_fsm': 'unlogged_relations', 'base/5/16385_vm': 'unlogged_relations',
    'base/5/16385_init': None,  # the init fork of the unlogged relation is sent
    'base/5/16390_init': None,  # an init fork alone
    'base/5/016385': None,  # a leading zero: not a relation file
    'base/5/t3_16390': 'temporary_relations', 'base/5/t3_16390_fsm': 'temporary_relations',
    'base/5/t12_7.1': 'temporary_relations',
    'base/5/pg_internal.init': 'named_files',
    'base/5/pgsql_tmp_leftover': 'temporary', 'base/pgsql_tmp/pgsql_tmp123.0': 'temporary',
    'base/.DS_Store': 'macos',
    'pg_stat_tmp/global.stat': 'directory_contents', 'pg_replslot/slot/state': 'directory_contents',
    'pg_dynshmem/mmap.1': 'directory_contents', 'pg_notify/0000': 'directory_contents',
    'pg_serial/0000': 'directory_contents', 'pg_snapshots/00000003-1': 'directory_contents',
    'pg_subtrans/0000': 'directory_contents',
    'postgresql.auto.conf.tmp': 'named_files', 'current_logfiles.tmp': 'named_files',
    'backup_label': 'named_files', 'tablespace_map': 'named_files', 'backup_manifest': 'named_files',
    'postmaster.opts': 'named_files',
    f'pg_wal/{REDO}': None, 'pg_wal/000000010000000000000002': 'wal',
    f'pg_wal/archive_status/{REDO}.done': 'wal', 'pg_wal/summaries/x.summary': 'wal', 'pg_wal/other/f': 'wal',
    'pg_xact/0000': None, 'pg_stat/pgstat.stat': None, 'pg_logical/replorigin_checkpoint': None,
}
EMPTY_DIRECTORIES = ('pg_tblspc', 'pg_twophase', 'pg_commit_ts')
# Main forks of unlogged relations, sent with their init forks' bytes (what recovery's reset leaves).
RESET = {'base/5/16385': 'base/5/16385_init', 'base/5/16390': 'base/5/16390_init',
         'global/16385': 'global/16385_init'}
# Directories excluded with everything in them: (rule, files inside).
EXCLUDED_DIRECTORIES = {'base/pgsql_tmp': 'temporary', 'pg_replslot/slot': 'directory_contents',
                        'pg_wal/other': 'wal'}


def build_datadir(root):
    rng = random.Random(70)
    files = {path: rng.randbytes(PAGE * (1 + len(path) % 3)) for path in TREE}
    # PG_VERSION as initdb writes it: pgdelta refuses a major version other than 17 or 18 (D08, EXP-40).
    files.update({path: b'17\n' for path in files if path.rsplit('/', 1)[-1] == 'PG_VERSION'})
    write_tree(root, files, EMPTY_DIRECTORIES)
    return root


def expected_files():
    return sorted([path for path, rule in TREE.items() if rule is None] + list(RESET))


def expected_directories(root):
    """Every directory of the tree except those excluded with their contents."""
    found = sorted(path.relative_to(root).as_posix() for path in Path(root).rglob('*') if path.is_dir())
    return [path for path in found if not any(path == excluded or path.startswith(excluded + '/')
                                              for excluded in EXCLUDED_DIRECTORIES)]


def materialize(source, selection, destination):
    """A copy of just what `selection` sends from `source`, to compare a refreshed copy with."""
    destination.mkdir()
    os.chmod(destination, selection.inventory.root_mode)
    for entry in selection.inventory.directories:
        (destination / entry.path).mkdir(mode=entry.mode)
        os.chmod(destination / entry.path, entry.mode)
    for entry in selection.inventory.files:
        shutil.copyfile(source / selection.source_path(entry.path), destination / entry.path)
        os.chmod(destination / entry.path, entry.mode)
    return destination


class DatadirTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.datadir = build_datadir(self.root / 'D')
        self.stub = stub_controldata(self.root / 'bin')
        self.basis, self.output = self.root / 'B', self.root / 'O'


class SelectionTests(DatadirTestCase):
    def test_every_exclusion_rule_of_postgresql_17(self):
        selection = select(self.datadir, self.stub)
        self.assertEqual([entry.path for entry in selection.inventory.files], expected_files())
        self.assertEqual([entry.path for entry in selection.inventory.directories],
                         expected_directories(self.datadir))
        self.assertEqual(selection.redo_wal_file, REDO)
        self.assertEqual(selection.identity['cluster_state'], 'shut down')
        self.assertEqual(selection.sources, RESET)
        sizes = {entry.path: entry.size for entry in selection.inventory.files}
        for main, init in RESET.items():
            self.assertEqual(sizes[main], (self.datadir / init).stat().st_size)
        expected = {rule: {'files': 0, 'directories': 0, 'bytes': 0} for rule in datadir.RULES}
        for path, rule in TREE.items():
            if rule is not None:
                expected[rule]['files'] += 1
                expected[rule]['bytes'] += (self.datadir / path).stat().st_size
        for rule in EXCLUDED_DIRECTORIES.values():
            expected[rule]['directories'] += 1
        summary = selection.excluded_summary()
        self.assertEqual(summary['by_rule'], expected)
        self.assertEqual(summary['files'], sum(1 for rule in TREE.values() if rule))
        self.assertEqual(summary['directories'], len(EXCLUDED_DIRECTORIES))
        self.assertEqual(summary['unlogged_main_forks_reset'], len(RESET))
        for name in datadir.EXCLUDED_DIRECTORY_CONTENTS + ('pg_wal/archive_status', 'pg_wal/summaries'):
            with self.subTest(kept_empty=name):
                self.assertIn(name, [entry.path for entry in selection.inventory.directories])

    def test_refusals(self):
        cases = {
            'no pg_controldata': (None, None, '--pg-controldata'),
            'running': (lambda: (self.datadir / 'postmaster.pid').write_text('1\n'), self.stub, 'postmaster.pid'),
            'in production': (None, stub_controldata(self.root / 'prod', SAMPLE.replace(
                'shut down', 'in production')), "'in production', not 'shut down'"),
            'shut down in recovery': (None, stub_controldata(self.root / 'standby', SAMPLE.replace(
                'shut down', 'shut down in recovery')), 'shut down in recovery'),
            'other major': (None, stub_controldata(self.root / 'crc', SAMPLE, CRC_WARNING), 'cannot tell'),
            'no REDO WAL file': (None, stub_controldata(self.root / 'old', SAMPLE.replace(
                "Latest checkpoint's REDO WAL file", 'Something else')), 'REDO WAL file'),
            'REDO segment missing': (lambda: os.unlink(self.datadir / 'pg_wal' / REDO), self.stub, 'is missing'),
            'symlink': (lambda: os.symlink('/', self.datadir / 'base/5/link'), self.stub, 'symlinks'),
            'tablespace': (lambda: (self.datadir / 'pg_tblspc/16400').mkdir(), self.stub, 'tablespaces'),
        }
        for label, (change, stub, message) in cases.items():
            with self.subTest(case=label):
                shutil.rmtree(self.datadir)
                build_datadir(self.datadir)
                if change:
                    change()
                with self.assertRaises(InvalidInput) as caught:
                    select(self.datadir, stub)
                self.assertIn(message, str(caught.exception))

    def test_symlinks_inside_excluded_contents_do_not_matter(self):
        os.symlink('/', self.datadir / 'pg_replslot' / 'link')
        os.symlink('/', self.datadir / 'pg_wal' / 'link')
        self.assertEqual([entry.path for entry in select(self.datadir, self.stub).inventory.files],
                         expected_files())

    def test_list_command_prints_the_selected_files_as_json(self):
        pairs = list_selection(self.datadir, '--pg-controldata', self.stub, '--sources')
        self.assertEqual(pairs.returncode, 0, pairs.stderr)
        self.assertEqual(json.loads(pairs.stdout), [[path, RESET.get(path, path)] for path in expected_files()])
        (self.datadir / 'postmaster.pid').write_text('1\n')
        for extra in ((), ('--sources',)):
            refused = list_selection(self.datadir, '--pg-controldata', self.stub, *extra)
            self.assertEqual(refused.returncode, 2)
            self.assertIn('postmaster.pid', refused.stderr)

    def test_plain_list_names_files_whose_bytes_the_copy_holds_or_refuses(self):
        # A harness hashes DIR/<path> for each path plain `list` prints and compares that with the copy. The
        # main fork of an unlogged relation is sent with its init fork's bytes, so with one plain `list`
        # refuses (exit 2) and points at --sources rather than print a list that cannot be hashed so.
        refused = list_selection(self.datadir, '--pg-controldata', self.stub)
        self.assertEqual((refused.returncode, refused.stdout), (2, ''))
        self.assertIn('--sources', refused.stderr)
        self.assertIn('base/5/16385', refused.stderr)
        for path in [path for path in TREE if path.endswith('_init')]:
            os.unlink(self.datadir / path)
        listing = list_selection(self.datadir, '--pg-controldata', self.stub)
        self.assertEqual(listing.returncode, 0, listing.stderr)
        listed = json.loads(listing.stdout)
        self.assertEqual(listed, [entry.path for entry in select(self.datadir, self.stub).inventory.files])
        write_tree(self.basis, {})
        outcome = transfer(self.datadir, self.basis, self.output, source_kind='datadir', pg_controldata=self.stub)
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assertEqual(sorted(path.relative_to(self.output).as_posix() for path in self.output.rglob('*')
                                if path.is_file()), sorted(listed))
        for path in listed:
            self.assertEqual((self.output / path).read_bytes(), (self.datadir / path).read_bytes(), path)


class TransferTests(DatadirTestCase):
    def stale_copy(self):
        shutil.copytree(self.datadir, self.basis)
        rng = random.Random(71)
        for path in ('base/5/16384', 'global/1262'):
            (self.basis / path).write_bytes(rng.randbytes(PAGE))

    def test_new_copy_holds_exactly_what_is_selected(self):
        self.stale_copy()
        selection = select(self.datadir, self.stub)
        outcome = transfer(self.datadir, self.basis, self.output, source_kind='datadir', pg_controldata=self.stub)
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        expected = materialize(self.datadir, selection, self.root / 'expected')
        self.assertEqual(snapshot(self.output), snapshot(expected))
        for main, init in RESET.items():
            self.assertEqual((self.output / main).read_bytes(), (self.datadir / init).read_bytes())
        stats = outcome.sender
        self.assertEqual(stats['source_kind'], 'datadir')
        self.assertEqual(stats['source_excluded'], selection.excluded_summary())
        self.assertEqual(stats['source_identity'], selection.identity)
        self.assertEqual(stats['files']['total'], len(expected_files()))

    def test_in_place_refresh_from_a_data_directory(self):
        self.stale_copy()
        selection = select(self.datadir, self.stub)
        outcome = transfer(self.datadir, self.basis, None, source_kind='datadir', pg_controldata=self.stub,
                           receiver_options={'in_place': True})
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        expected = materialize(self.datadir, selection, self.root / 'expected')
        self.assertEqual(snapshot(self.basis), snapshot(expected))
        self.assertTrue(outcome.sender['in_place'])

    def test_backup_kind_is_unchanged(self):
        write_tree(self.basis, {})
        outcome = transfer(self.datadir, self.basis, self.output)
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assertEqual(snapshot(self.output), snapshot(self.datadir))
        self.assertEqual((outcome.sender['source_kind'], outcome.sender['source_excluded']), ('backup', None))

    def test_a_server_started_during_the_run_fails_it_before_publication(self):
        write_tree(self.basis, {})
        real = datadir.read_controldata
        calls = []

        def moved(*args, **kwargs):
            identity, output = real(*args, **kwargs)
            calls.append(1)
            if len(calls) > 1:  # the check before COMPLETE: a new checkpoint
                identity = dict(identity, redo_lsn='0/2000028')
            return identity, output

        with mock.patch.object(datadir, 'read_controldata', moved):
            outcome = transfer(self.datadir, self.basis, self.output, source_kind='datadir',
                               pg_controldata=self.stub)
        self.assertIsInstance(outcome.sender_error, VerificationError)
        self.assertIn('changed during the run', str(outcome.sender_error))
        self.assertIn('redo_lsn', str(outcome.sender_error))
        self.assertIsInstance(outcome.receiver_error, PeerError)
        self.assertFalse(os.path.lexists(self.output))
        self.assertEqual(staging_dirs(self.output), [])

        datadir_path = self.datadir

        class Starting(Channel):
            """Sender side: a server starts on the data directory while its pages are sent."""

            def send(self, code, *parts):
                if code == protocol.FILE_DONE:
                    (datadir_path / 'postmaster.pid').write_text('1\n')
                super().send(code, *parts)

        outcome = transfer(self.datadir, self.basis, self.output, source_kind='datadir', pg_controldata=self.stub,
                           sender_class=Starting)
        self.assertIsInstance(outcome.sender_error, VerificationError)
        self.assertIn('gained postmaster.pid', str(outcome.sender_error))
        self.assertFalse(os.path.lexists(self.output))

    def test_cli_needs_pg_controldata_and_records_the_kind(self):
        write_tree(self.basis, {})
        refused = run('local', '--source', self.datadir, '--source-kind', 'datadir', '--basis', self.basis,
                      '--output', self.output)
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn('--source-kind datadir needs --pg-controldata', refused.stderr)
        stats_path = self.root / 'stats.json'
        result = run('local', '--source', self.datadir, '--source-kind', 'datadir', '--basis', self.basis,
                     '--output', self.output, '--pg-controldata', self.stub, '--stats-json', stats_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        stats = json.loads(stats_path.read_text())
        self.assertEqual(stats['source_kind'], 'datadir')
        self.assertEqual(stats['source_excluded']['files'], sum(1 for rule in TREE.values() if rule))
        self.assertEqual(sorted(str(path.relative_to(self.output)) for path in self.output.rglob('*')
                                if path.is_file()), expected_files())
        self.assertIn('--source-kind', run('send', '--help').stdout)


@unittest.skipUnless(available(), SKIP_REASON)
class RealClusterTests(unittest.TestCase):
    QUERY = 'select count(*), sum(length(v)) from t'

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.servers = Servers(cls.root)
        cls.datadir, cls.stale = cls.root / 'D', cls.root / 'stale'
        initdb(cls.datadir)
        cls.servers.run_sql(cls.datadir,
                            'create table t as select g as id, md5(g::text) as v from generate_series(1, 20000) g',
                            'create unlogged table u as select g from generate_series(1, 5000) g', 'checkpoint')
        shutil.copytree(cls.datadir, cls.stale)
        cls.expected = cls.servers.run_sql(
            cls.datadir, "update t set v = v || '!' where id % 97 = 0",
            'insert into t select g, md5(g::text) from generate_series(20001, 21000) g',
            'insert into u select g from generate_series(1, 100) g', 'select pg_switch_wal()', cls.QUERY)
        # Left behind by a crashed backend: never sent.
        write_tree(cls.datadir, {'base/pgsql_tmp/pgsql_tmp4242.0': b'x' * PAGE})

    @classmethod
    def tearDownClass(cls):
        cls.servers.close()
        cls.temp.cleanup()

    def fresh_basis(self, name):
        basis = self.root / name
        shutil.copytree(self.stale, basis)
        return basis

    def assert_starts_clean(self, copy):
        """Starts without recovery, with the new rows and the unlogged table reset to empty."""
        log = self.servers.logs / f'{copy.name}.log'
        self.assertEqual(self.servers.run_sql(copy, self.QUERY), self.expected)
        self.assertEqual(self.servers.run_sql(copy, 'select count(*) from u'), '0')
        text = log.read_text()
        self.assertIn('database system was shut down at', text)
        self.assertNotIn('redo starts', text)

    def selected(self):
        """[(path, path read)] of what pgdelta.datadir selects from the data directory."""
        listing = list_selection(self.datadir, '--pg-controldata', CONTROLDATA, '--sources')
        self.assertEqual(listing.returncode, 0, listing.stderr)
        return [tuple(pair) for pair in json.loads(listing.stdout)]

    def assert_holds_the_selection(self, copy):
        selected = self.selected()
        self.assertEqual(self.files_of(copy), [path for path, _ in selected])
        self.assertTrue(any(path != read for path, read in selected))  # the unlogged table's main fork
        for path, read in selected:
            self.assertEqual((copy / path).read_bytes(), (self.datadir / read).read_bytes(), path)

    def files_of(self, root):
        return sorted(path.relative_to(root).as_posix() for path in root.rglob('*') if path.is_file())

    def test_new_copy_from_a_stopped_data_directory_starts_without_recovery(self):
        basis, output, stats_path = self.fresh_basis('B-new'), self.root / 'O-new', self.root / 'new.json'
        result = run('local', '--source', self.datadir, '--source-kind', 'datadir', '--basis', basis,
                     '--output', output, '--pg-controldata', CONTROLDATA, '--stats-json', stats_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        stats = json.loads(stats_path.read_text())
        self.assertEqual((stats['mode'], stats['source_kind']), ('reuse', 'datadir'))
        by_rule = stats['source_excluded']['by_rule']
        for rule in ('unlogged_relations', 'named_files', 'temporary', 'wal'):
            with self.subTest(rule=rule):
                self.assertGreater(by_rule[rule]['files'], 0)
        self.assertEqual(stats['source_excluded']['unlogged_main_forks_reset'], 1)
        self.assert_holds_the_selection(output)
        self.assert_starts_clean(output)

    def test_in_place_refresh_from_a_stopped_data_directory_starts_without_recovery(self):
        basis = self.fresh_basis('B-in-place')
        result = run('local', '--source', self.datadir, '--source-kind', 'datadir', '--basis', basis,
                     '--in-place', '--pg-controldata', CONTROLDATA)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_holds_the_selection(basis)
        self.assert_starts_clean(basis)

    def test_plain_list_refuses_a_cluster_with_an_unlogged_table(self):
        refused = list_selection(self.datadir, '--pg-controldata', CONTROLDATA)
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn('--sources', refused.stderr)

    def test_running_or_crashed_cluster_is_refused(self):
        crashed = self.fresh_basis('crashed')
        basis = self.fresh_basis('B-refused')
        self.servers.pg_ctl(crashed, 'start')
        try:
            running = run('local', '--source', crashed, '--source-kind', 'datadir', '--basis', basis,
                          '--output', self.root / 'O-running', '--pg-controldata', CONTROLDATA)
            self.assertEqual(running.returncode, 2, running.stderr)
            self.assertIn('postmaster.pid', running.stderr)
        finally:
            self.servers.pg_ctl(crashed, 'stop', mode='immediate')  # a crash: no shutdown checkpoint
        refused = run('local', '--source', crashed, '--source-kind', 'datadir', '--basis', basis,
                      '--output', self.root / 'O-crashed', '--pg-controldata', CONTROLDATA)
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("'in production', not 'shut down'", refused.stderr)
        self.assertFalse(os.path.lexists(self.root / 'O-crashed'))


if __name__ == '__main__':
    unittest.main()
