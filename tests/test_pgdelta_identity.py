"""pg_controldata parsing, the reuse gate, and (when installed) real PostgreSQL 17 and 18 clusters."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from pgdelta.errors import InvalidInput
from pgdelta.identity import gate, parse_controldata, read_identity, reuse_policy

from pgdelta_support import (REPO_ROOT, differing_pages, python_command, snapshot, staging_dirs,
                             subprocess_env, total_pages, transfer, write_tree)

PG_BIN = Path(os.environ.get('PGDELTA_TEST_PG_BIN', '/opt/homebrew/opt/postgresql@17/bin'))
PG18_BIN = Path(os.environ.get('PGDELTA_TEST_PG18_BIN', '/opt/homebrew/opt/postgresql@18/bin'))
CRC_WARNING = ('pg_controldata: warning: calculated CRC checksum does not match value stored in control file\n'
               'pg_controldata: detail: Either the control file is corrupt, or it has a different layout than '
               'this program is expecting.  The results below are untrustworthy.\n')

SAMPLE = """\
pg_control version number:            1700
Catalog version number:               202406281
Database system identifier:           7699399647675120679
Database cluster state:               shut down
pg_control last modified:             Thu Oct 22 04:02:56 2026
Latest checkpoint location:           0/151A8F0
Latest checkpoint's REDO location:    0/151A8F0
Latest checkpoint's REDO WAL file:    000000010000000000000001
Latest checkpoint's TimeLineID:       1
Latest checkpoint's oldestCommitTsXid:0
Time of latest checkpoint:            Thu Oct 22 04:02:56 2026
Database block size:                  8192
Blocks per segment of large relation: 131072
WAL block size:                       8192
Bytes per WAL segment:                16777216
Data page checksum version:           0
Mock authentication nonce:            e7742ee50db962b041b193ec6a55dc77
"""

IDENTITY = {
    'pg_control_version': 1700, 'catalog_version': 202406281,
    'system_identifier': '7699399647675120679', 'cluster_state': 'shut down',
    'redo_lsn': '0/151A8F0', 'timeline': 1, 'block_size': 8192, 'blocks_per_segment': 131072,
    'wal_segment_size': 16777216, 'data_checksum_version': 0,
}


class ParseTests(unittest.TestCase):
    def test_parses_the_gate_and_recovery_fields(self):
        self.assertEqual(parse_controldata(SAMPLE), IDENTITY)

    def test_missing_field_is_invalid_input(self):
        text = '\n'.join(line for line in SAMPLE.splitlines() if not line.startswith('Database block size'))
        with self.assertRaisesRegex(InvalidInput, 'block_size'):
            parse_controldata(text)

    def test_unparseable_number_is_invalid_input(self):
        with self.assertRaises(InvalidInput):
            parse_controldata(SAMPLE.replace('Database block size:                  8192',
                                             'Database block size:                  eight'))

    @unittest.skipIf(os.name == 'nt', 'POSIX shell stub')
    def test_read_identity_runs_the_given_binary_in_the_c_locale(self):
        with tempfile.TemporaryDirectory() as temp:
            stub = Path(temp) / 'pg_controldata'
            (Path(temp) / 'sample.txt').write_text(SAMPLE)
            stub.write_text('#!/bin/sh\n'
                            'test "$1" = "-D" || exit 9\n'
                            'test "$LC_ALL" = "C" || exit 8\n'
                            f'cat "{temp}/sample.txt"\n')
            stub.chmod(0o755)
            self.assertEqual(read_identity(temp, stub), IDENTITY)
            failing = Path(temp) / 'failing'
            failing.write_text('#!/bin/sh\necho "could not open file" >&2\nexit 1\n')
            failing.chmod(0o755)
            with self.assertRaisesRegex(InvalidInput, 'could not open file'):
                read_identity(temp, failing)
            with self.assertRaisesRegex(InvalidInput, 'not found'):
                read_identity(temp, Path(temp) / 'absent')


class GateTests(unittest.TestCase):
    def test_matching_identities_allow_reuse(self):
        allowed, reason = gate(IDENTITY, dict(IDENTITY, redo_lsn='0/1000000', timeline=2))
        self.assertTrue(allowed, reason)

    def test_each_gate_field_mismatch_means_full_mode_by_default(self):
        for key, other in (('system_identifier', '1'), ('catalog_version', 1), ('pg_control_version', 1),
                           ('block_size', 16384), ('blocks_per_segment', 1), ('data_checksum_version', 1)):
            with self.subTest(field=key):
                allowed, reason = gate(IDENTITY, dict(IDENTITY, **{key: other}))
                self.assertFalse(allowed)
                self.assertIn('differs', reason)

    def test_reuse_any_basis_allows_reuse_whatever_the_identities_say(self):
        cases = [(IDENTITY, dict(IDENTITY, system_identifier='1'), 'system identifier differs'),
                 (IDENTITY, dict(IDENTITY, catalog_version=1), 'catalog version differs'),
                 (None, IDENTITY, 'source identity unknown'),
                 (IDENTITY, None, 'basis identity unknown'),
                 (IDENTITY, dict(IDENTITY, untrustworthy='crc'), 'basis identity untrustworthy')]
        for source, basis, finding in cases:
            with self.subTest(finding=finding):
                allowed, reason = gate(source, basis, reuse_any_basis=True)
                self.assertTrue(allowed)
                self.assertIn(finding, reason)
                self.assertIn('--reuse-any-basis', reason)
        self.assertEqual(gate(IDENTITY, IDENTITY, reuse_any_basis=True), gate(IDENTITY, IDENTITY))

    def test_unknown_identity_is_full_mode_unless_any_basis_is_allowed(self):
        self.assertEqual(gate(None, IDENTITY)[0], False)
        self.assertEqual(gate(IDENTITY, None)[0], False)
        self.assertIn('identity unknown', gate(None, None)[1])
        self.assertEqual(gate(None, None, reuse_any_basis=True)[0], True)

    def test_policy_names(self):
        self.assertEqual(reuse_policy(False), 'same-cluster')
        self.assertEqual(reuse_policy(True), 'any-basis')

    def test_gate_docstring_says_it_is_an_efficiency_policy(self):
        text = ' '.join(gate.__doc__.split())
        self.assertIn('efficiency policy', text)
        self.assertIn('verif', text)

    def test_missing_gate_field_is_full_mode(self):
        partial = {key: value for key, value in IDENTITY.items() if key != 'catalog_version'}
        self.assertEqual(gate(IDENTITY, partial), (False, 'catalog version unknown'))


@unittest.skipUnless((PG_BIN / 'pg_controldata').exists() and (PG_BIN / 'initdb').exists(),
                     f'PostgreSQL binaries not found in {PG_BIN}')
class RealClusterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        for name in ('one', 'two'):
            subprocess.run([str(PG_BIN / 'initdb'), '-D', str(cls.root / name), '--username=t',
                            '--auth=trust', '--no-sync'], check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, env=dict(os.environ, LC_ALL='C', LANG='C'))
        shutil.copytree(cls.root / 'one', cls.root / 'one-copy')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_read_identity_parses_a_fresh_cluster(self):
        identity = read_identity(self.root / 'one', PG_BIN / 'pg_controldata')
        self.assertRegex(identity['system_identifier'], r'^\d+$')
        self.assertEqual(identity['block_size'], 8192)
        self.assertEqual(identity['blocks_per_segment'], 131072)
        self.assertEqual(identity['timeline'], 1)
        self.assertRegex(identity['redo_lsn'], r'^[0-9A-F]+/[0-9A-F]+$')
        major = int((self.root / 'one' / 'PG_VERSION').read_text())
        self.assertEqual(identity['pg_control_version'] // 100, major)
        self.assertGreater(identity['catalog_version'], 200000000)
        self.assertIn(identity['data_checksum_version'], (0, 1))

    def test_copy_of_the_same_cluster_reuses_every_page(self):
        controldata = PG_BIN / 'pg_controldata'
        source_identity = read_identity(self.root / 'one', controldata)
        basis_identity = read_identity(self.root / 'one-copy', controldata)
        before = snapshot(self.root / 'one-copy')
        output = self.root / 'out-reuse'
        outcome = transfer(self.root / 'one', self.root / 'one-copy', output, node_blocks=128,
                           source_identity=source_identity, basis_identity=basis_identity,
                           reuse_any_basis=False)
        self.assertIsNone(outcome.sender_error, outcome.sender_error)
        self.assertEqual(outcome.sender['mode'], 'reuse')
        self.assertEqual(outcome.sender['pages']['sent'], 0)
        self.assertEqual(snapshot(output), snapshot(self.root / 'one'))
        self.assertEqual(snapshot(self.root / 'one-copy'), before)

    def test_different_cluster_falls_back_to_full_mode(self):
        controldata = PG_BIN / 'pg_controldata'
        output = self.root / 'out-full'
        outcome = transfer(self.root / 'one', self.root / 'two', output, node_blocks=128,
                           source_identity=read_identity(self.root / 'one', controldata),
                           basis_identity=read_identity(self.root / 'two', controldata),
                           reuse_any_basis=False)
        self.assertIsNone(outcome.sender_error, outcome.sender_error)
        self.assertEqual(outcome.sender['mode'], 'full')
        self.assertEqual(outcome.sender['reuse_policy'], 'same-cluster')
        self.assertIn('system identifier differs', outcome.sender['gate_reason'])
        self.assertEqual(outcome.sender['pages']['sent'], total_pages(self.root / 'one'))
        self.assertLess(differing_pages(self.root / 'one', self.root / 'two'), total_pages(self.root / 'one'))
        self.assertEqual(snapshot(output), snapshot(self.root / 'one'))

    def test_different_cluster_with_reuse_any_basis_is_still_rebuilt_exactly(self):
        # Reusing pages of an unrelated cluster is safe: every output file is verified against S.
        controldata = PG_BIN / 'pg_controldata'
        output = self.root / 'out-any-basis'
        outcome = transfer(self.root / 'one', self.root / 'two', output, node_blocks=128,
                           source_identity=read_identity(self.root / 'one', controldata),
                           basis_identity=read_identity(self.root / 'two', controldata),
                           reuse_any_basis=True)
        self.assertIsNone(outcome.sender_error, outcome.sender_error)
        self.assertEqual(outcome.sender['mode'], 'reuse')
        self.assertEqual(outcome.sender['reuse_policy'], 'any-basis')
        self.assertIn('system identifier differs', outcome.sender['gate_reason'])
        self.assertEqual(outcome.sender['pages']['sent'], differing_pages(self.root / 'one', self.root / 'two'))
        self.assertGreater(outcome.sender['pages']['reused'], 0)
        self.assertEqual(snapshot(output), snapshot(self.root / 'one'))

    @unittest.skipUnless((PG_BIN / 'pg_ctl').exists() and (PG_BIN / 'psql').exists(), 'needs pg_ctl and psql')
    def test_refreshed_copy_of_a_written_cluster_starts_with_the_new_rows(self):
        # S is a disposable stopped cluster here, not a pg_basebackup; only O and S are ever started.
        source, basis, output = self.root / 'written', self.root / 'written-stale', self.root / 'written-out'
        shutil.copytree(self.root / 'one', source)
        sockets = Path(tempfile.mkdtemp(prefix='pgd'))  # Unix socket paths are limited to ~100 bytes
        self.addCleanup(shutil.rmtree, sockets, True)
        env = dict(os.environ, LC_ALL='C', LANG='C')

        def server(datadir, action):
            subprocess.run([str(PG_BIN / 'pg_ctl'), '-D', str(datadir), '-l', str(self.root / f'{datadir.name}.log'),
                            '-w', '-m', 'fast', '-o', f"-k {sockets} -c listen_addresses='' -c fsync=off", action],
                           check=True, stdout=subprocess.DEVNULL, env=env, timeout=60)

        def sql(statement):
            return subprocess.run([str(PG_BIN / 'psql'), '-h', str(sockets), '-U', 't', '-d', 'postgres', '-At',
                                   '-c', statement], check=True, capture_output=True, text=True, env=env,
                                  timeout=60).stdout.strip()

        server(source, 'start')
        try:
            sql('create table t as select g as id, md5(g::text) as v from generate_series(1, 20000) g')
            sql('checkpoint')
        finally:
            server(source, 'stop')
        shutil.copytree(source, basis)
        server(source, 'start')
        try:
            sql("update t set v = v || '!' where id % 97 = 0")
            sql('insert into t select g, md5(g::text) from generate_series(20001, 21000) g')
            expected = sql('select count(*), sum(length(v)) from t')
        finally:
            server(source, 'stop')
        controldata = PG_BIN / 'pg_controldata'
        outcome = transfer(source, basis, output, node_blocks=128, reuse_any_basis=False,
                           source_identity=read_identity(source, controldata),
                           basis_identity=read_identity(basis, controldata))
        self.assertIsNone(outcome.sender_error, outcome.sender_error)
        stats = outcome.sender
        self.assertEqual(stats['mode'], 'reuse')
        self.assertEqual(stats['pages']['sent'], differing_pages(source, basis))
        self.assertLess(stats['pages']['sent'], stats['pages']['total'])
        self.assertEqual(snapshot(output), snapshot(source))
        server(output, 'start')
        try:
            self.assertEqual(sql('select count(*), sum(length(v)) from t'), expected)
        finally:
            server(output, 'stop')

    def test_cli_local_reads_identity_with_pg_controldata(self):
        output = self.root / 'out-cli'
        result = subprocess.run(
            [sys.executable, '-m', 'pgdelta', 'local', '--source', self.root / 'one',
             '--basis', self.root / 'one-copy', '--output', output,
             '--pg-controldata', PG_BIN / 'pg_controldata'],
            capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('reuse mode', result.stderr)
        self.assertEqual(snapshot(output), snapshot(self.root / 'one'))
        self.assertEqual(staging_dirs(output), [])


def major_of(bindir):
    try:
        text = subprocess.run([str(bindir / 'pg_controldata'), '--version'], capture_output=True, text=True,
                              timeout=30).stdout
    except OSError:
        return None
    match = re.search(r'(\d+)(?:\.\d+)?', text)
    return int(match.group(1)) if match else None


def initdb(bindir, datadir, *extra):
    subprocess.run([str(bindir / 'initdb'), '-D', str(datadir), '--username=t', '--auth=trust', '--no-sync',
                    *extra], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   env=dict(os.environ, LC_ALL='C', LANG='C'))


def cli(*args):
    return subprocess.run(python_command(*args), capture_output=True, text=True, cwd=REPO_ROOT,
                          env=subprocess_env(), timeout=120)


@unittest.skipIf(os.name == 'nt', 'POSIX shell stubs')
class BasisControldataTests(unittest.TestCase):
    """--basis-pg-controldata reads B with its own binary (B may be PostgreSQL 17 while S is 18)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = write_tree(self.root / 'S', {'PG_VERSION': b'18\n', 'base/1/rel': b'n' * 8192})
        self.basis = write_tree(self.root / 'B', {'PG_VERSION': b'17\n', 'base/1/rel': b'o' * 8192})
        self.output = self.root / 'O'
        self.calls = self.root / 'calls.log'
        (self.root / 'warning.txt').write_text(CRC_WARNING)
        new = SAMPLE.replace('1700', '1800').replace('202406281', '202506291')
        self.new_tool = self.stub('pg_controldata18', new, warn_on_major='17')
        self.old_tool = self.stub('pg_controldata17', SAMPLE)

    def stub(self, name, text, warn_on_major=None):
        """A pg_controldata that logs its name and directory; it warns like a binary of another major would."""
        (self.root / f'{name}.txt').write_text(text)
        warning = ''
        if warn_on_major:
            warning = (f'if [ "$(cat "$2/PG_VERSION")" = "{warn_on_major}" ]; then '
                       f'cat "{self.root}/warning.txt" >&2; fi\n')
        path = self.root / name
        path.write_text(f'#!/bin/sh\necho "{name} $2" >> "{self.calls}"\n{warning}cat "{self.root}/{name}.txt"\n')
        path.chmod(0o755)
        return path

    def calls_made(self):
        return sorted(self.calls.read_text().splitlines())

    def run_local(self, *extra):
        stats_path = self.root / 'stats.json'
        result = cli('local', '--source', self.source, '--basis', self.basis, '--output', self.output,
                     '--stats-json', stats_path, *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot(self.output), snapshot(self.source))
        return json.loads(stats_path.read_text())

    def test_local_reads_each_side_with_its_own_binary_and_records_both_identities(self):
        stats = self.run_local('--pg-controldata', self.new_tool, '--basis-pg-controldata', self.old_tool)
        self.assertEqual(self.calls_made(), sorted([f'pg_controldata18 {self.source}',
                                                    f'pg_controldata17 {self.basis.absolute()}']))
        self.assertEqual(stats['source_identity']['pg_control_version'], 1800)
        self.assertEqual(stats['basis_identity']['pg_control_version'], 1700)
        self.assertNotIn('untrustworthy', stats['basis_identity'])
        self.assertEqual(stats['receiver']['source_identity'], stats['source_identity'])
        self.assertEqual(stats['receiver']['basis_identity'], stats['basis_identity'])
        self.assertEqual(stats['mode'], 'full')
        self.assertIn('catalog version differs', stats['gate_reason'])

    def test_basis_binary_defaults_to_pg_controldata(self):
        stats = self.run_local('--pg-controldata', self.old_tool)
        self.assertEqual(self.calls_made(), sorted([f'pg_controldata17 {self.source}',
                                                    f'pg_controldata17 {self.basis.absolute()}']))
        self.assertEqual(stats['basis_identity'], stats['source_identity'])
        self.assertEqual(stats['mode'], 'reuse')

    def test_binary_of_another_major_marks_the_basis_identity_untrustworthy(self):
        # pg_controldata 18 on a 17 control file exits 0 but warns that its output is untrustworthy.
        stats = self.run_local('--pg-controldata', self.new_tool)
        self.assertIn('CRC', stats['basis_identity']['untrustworthy'])
        self.assertNotIn('untrustworthy', stats['source_identity'])
        self.assertEqual(stats['mode'], 'full')
        self.assertIn('basis identity untrustworthy', stats['gate_reason'])
        self.assertIn('--basis-pg-controldata', stats['gate_reason'])

    def test_reuse_any_basis_reuses_across_differing_identities(self):
        write_tree(self.source, {'base/1/same': b's' * 3 * 8192})
        write_tree(self.basis, {'base/1/same': b's' * 3 * 8192})
        stats = self.run_local('--pg-controldata', self.new_tool, '--basis-pg-controldata', self.old_tool,
                               '--reuse-any-basis')
        self.assertEqual(stats['mode'], 'reuse')
        self.assertEqual(stats['reuse_policy'], 'any-basis')
        self.assertIn('catalog version differs', stats['gate_reason'])
        self.assertEqual(stats['pages']['reused'], 3)
        self.assertEqual(stats['pages']['sent'], differing_pages(self.source, self.basis))

    def test_receive_takes_basis_pg_controldata(self):
        receiver = subprocess.Popen(
            python_command('receive', '--basis', self.basis, '--output', self.output, '--listen', '127.0.0.1:0',
                           '--pg-controldata', self.new_tool, '--basis-pg-controldata', self.old_tool),
            stderr=subprocess.PIPE, text=True, cwd=REPO_ROOT, env=subprocess_env())
        self.addCleanup(receiver.stderr.close)
        self.addCleanup(lambda: receiver.poll() is None and receiver.kill())
        port = receiver.stderr.readline().rsplit(':', 1)[1].strip()
        stats_path = self.root / 'stats.json'
        sent = cli('send', '--source', self.source, '--connect', f'127.0.0.1:{port}',
                   '--pg-controldata', self.new_tool, '--stats-json', stats_path)
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertEqual(receiver.wait(timeout=60), 0)
        self.assertIn(f'pg_controldata17 {self.basis}', self.calls_made())
        stats = json.loads(stats_path.read_text())
        self.assertEqual(stats['basis_identity']['pg_control_version'], 1700)
        self.assertEqual(stats['source_identity']['pg_control_version'], 1800)


class UntrustworthyIdentityTests(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'POSIX shell stub')
    def test_read_identity_keeps_the_values_but_marks_them_untrustworthy(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'sample.txt').write_text(SAMPLE)
            (Path(temp) / 'warning.txt').write_text(CRC_WARNING)
            stub = Path(temp) / 'pg_controldata'
            stub.write_text(f'#!/bin/sh\ncat "{temp}/warning.txt" >&2\ncat "{temp}/sample.txt"\n')
            stub.chmod(0o755)
            identity = read_identity(temp, stub)
        self.assertEqual({key: value for key, value in identity.items() if key != 'untrustworthy'}, IDENTITY)
        self.assertIn('calculated CRC checksum does not match', identity['untrustworthy'])

    def test_untrustworthy_identity_means_full_mode(self):
        for source, basis, side in ((dict(IDENTITY, untrustworthy='crc'), IDENTITY, 'source'),
                                    (IDENTITY, dict(IDENTITY, untrustworthy='crc'), 'basis')):
            with self.subTest(side=side):
                allowed, reason = gate(source, basis)
                self.assertFalse(allowed)
                self.assertIn(f'{side} identity untrustworthy', reason)


@unittest.skipUnless(major_of(PG_BIN) and major_of(PG18_BIN) and major_of(PG_BIN) != major_of(PG18_BIN)
                     and (PG_BIN / 'initdb').exists() and (PG18_BIN / 'initdb').exists(),
                     f'needs PostgreSQL binaries of two majors in {PG_BIN} and {PG18_BIN}')
class CrossVersionClusterTests(unittest.TestCase):
    """B from the older major, S from the newer one, each read with its own pg_controldata."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        initdb(PG_BIN, cls.root / 'old')
        initdb(PG18_BIN, cls.root / 'new', '--no-data-checksums')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_local(self, name, *extra):
        stats_path = self.root / f'{name}.json'
        output = self.root / name
        result = cli('local', '--source', self.root / 'new', '--basis', self.root / 'old', '--output', output,
                     '--pg-controldata', PG18_BIN / 'pg_controldata', '--stats-json', stats_path, *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot(output), snapshot(self.root / 'new'))
        return json.loads(stats_path.read_text())

    def test_each_side_is_read_with_the_binary_of_its_major(self):
        stats = self.run_local('out-both', '--basis-pg-controldata', PG_BIN / 'pg_controldata')
        self.assertEqual(int((self.root / 'old' / 'PG_VERSION').read_text()), major_of(PG_BIN))
        for key in ('source_identity', 'basis_identity'):
            self.assertNotIn('untrustworthy', stats[key])
        self.assertNotEqual(stats['source_identity']['catalog_version'], stats['basis_identity']['catalog_version'])
        self.assertEqual(stats['receiver']['source_identity'], stats['source_identity'])
        self.assertEqual(stats['mode'], 'full')
        self.assertIn('differs', stats['gate_reason'])

    def test_reuse_any_basis_across_majors_is_still_rebuilt_exactly(self):
        stats = self.run_local('out-any', '--basis-pg-controldata', PG_BIN / 'pg_controldata', '--reuse-any-basis')
        self.assertEqual(stats['mode'], 'reuse')
        self.assertEqual(stats['reuse_policy'], 'any-basis')
        self.assertEqual(stats['pages']['sent'], differing_pages(self.root / 'new', self.root / 'old'))

    def test_one_binary_for_both_marks_the_older_basis_untrustworthy(self):
        stats = self.run_local('out-one')
        self.assertNotIn('untrustworthy', stats['source_identity'])
        self.assertIn('CRC', stats['basis_identity']['untrustworthy'])
        self.assertIn('basis identity untrustworthy', stats['gate_reason'])


@unittest.skipUnless(major_of(PG_BIN) and major_of(PG18_BIN) and major_of(PG_BIN) < major_of(PG18_BIN)
                     and all((bindir / tool).exists() for bindir in (PG_BIN, PG18_BIN)
                             for tool in ('initdb', 'pg_ctl', 'psql'))
                     and (PG18_BIN / 'pg_upgrade').exists(),
                     f'needs PostgreSQL binaries of two majors and pg_upgrade in {PG_BIN} and {PG18_BIN}')
class PgUpgradeTests(unittest.TestCase):
    """The motivating case for --reuse-any-basis: B is the old major, S the same data after pg_upgrade."""

    def test_reuse_any_basis_reuses_relation_pages_across_pg_upgrade(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        sockets = Path(tempfile.mkdtemp(prefix='pgu'))  # Unix socket paths are limited to ~100 bytes
        self.addCleanup(shutil.rmtree, sockets, True)
        env = dict(os.environ, LC_ALL='C', LANG='C')
        old, basis, new, output = root / 'old', root / 'basis', root / 'new', root / 'out'

        def server(bindir, datadir, action):
            subprocess.run([str(bindir / 'pg_ctl'), '-D', str(datadir), '-l', str(root / f'{datadir.name}.log'),
                            '-w', '-m', 'fast', '-o', f"-k {sockets} -c listen_addresses='' -c fsync=off", action],
                           check=True, stdout=subprocess.DEVNULL, env=env, timeout=120)

        def sql(bindir, statement):
            return subprocess.run([str(bindir / 'psql'), '-h', str(sockets), '-U', 't', '-d', 'postgres', '-At',
                                   '-c', statement], check=True, capture_output=True, text=True, env=env,
                                  timeout=60).stdout.strip()

        initdb(PG_BIN, old)
        server(PG_BIN, old, 'start')
        try:
            sql(PG_BIN, 'create table t as select g as id, md5(g::text) as v from generate_series(1, 30000) g')
            sql(PG_BIN, 'checkpoint')
            expected = sql(PG_BIN, 'select count(*), sum(length(v)) from t')
            table_file = sql(PG_BIN, "select pg_relation_filepath('t')")
        finally:
            server(PG_BIN, old, 'stop')
        shutil.copytree(old, basis)
        initdb(PG18_BIN, new, '--no-data-checksums')
        upgraded = subprocess.run([str(PG18_BIN / 'pg_upgrade'), '-b', str(PG_BIN), '-B', str(PG18_BIN),
                                   '-d', str(old), '-D', str(new), '--copy', '-U', 't', '-s', str(sockets)],
                                  cwd=root, capture_output=True, text=True, env=env, timeout=600)
        self.assertEqual(upgraded.returncode, 0, upgraded.stdout[-2000:] + upgraded.stderr[-2000:])
        shutil.rmtree(new / 'pg_upgrade_output.d', ignore_errors=True)

        stats_path = root / 'stats.json'
        result = cli('local', '--source', new, '--basis', basis, '--output', output, '--reuse-any-basis',
                     '--pg-controldata', PG18_BIN / 'pg_controldata',
                     '--basis-pg-controldata', PG_BIN / 'pg_controldata', '--stats-json', stats_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        stats = json.loads(stats_path.read_text())
        self.assertEqual(stats['mode'], 'reuse')
        self.assertEqual(stats['reuse_policy'], 'any-basis')
        self.assertNotIn('untrustworthy', stats['basis_identity'])
        self.assertEqual(stats['pages']['sent'], differing_pages(new, basis))
        # pg_upgrade --copy keeps user relation files (and, since PostgreSQL 15, their relfilenodes)
        # byte for byte, so at least every page of the table is reused.
        self.assertEqual((new / table_file).read_bytes(), (basis / table_file).read_bytes())
        self.assertGreaterEqual(stats['pages']['reused'], (new / table_file).stat().st_size // 8192)
        self.assertGreater((new / table_file).stat().st_size // 8192, 100)
        self.assertEqual(snapshot(output), snapshot(new))
        server(PG18_BIN, output, 'start')
        try:
            self.assertEqual(sql(PG18_BIN, 'select count(*), sum(length(v)) from t'), expected)
        finally:
            server(PG18_BIN, output, 'stop')


if __name__ == '__main__':
    unittest.main()
