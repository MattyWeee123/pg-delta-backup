"""A running source read in backup mode (--source-kind live, pgdelta.live; T8).

Without PostgreSQL: the WAL range from two LSNs, backup_label, the control
file's CRC and the WAL page headers; the live selection (pg_wal left out,
nothing refused for running); how files that change while read are hashed
and sent; the receiver's APPEND; and whole sessions against a stand-in for
psql (tests/stub_psql.py) that answers as a server would, which shows the
data directory is listed and read only after pg_backup_start returns (and
before it with the test switch), that the WAL and backup_label are
appended, and that backup mode is ended and the slot dropped on every
failure.

With PostgreSQL 17 binaries: a live refresh, in place and as a new copy,
while a pgbench bank load writes (EXP-12's invariant, EXP-25's load); a
disposable copy of the output recovers from its backup_label and holds a
consistent state between pg_backup_start and pg_backup_stop, and amcheck
finds nothing. A failed run leaves no slot and no session behind.
"""
import base64
import io
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from pgdelta import datadir, live, protocol, sender, transport
from pgdelta.datadir import select_live
from pgdelta.errors import InvalidInput, PeerError, ProtocolError, TransferError, VerificationError
from pgdelta.hashing import Hasher
from pgdelta.inplace import CONTROL_FILE, CONTROL_PENDING, MARKER
from pgdelta.inventory import FileEntry
from pgdelta.receiver import receipt_path
from pgdelta.tree import HashPool, hash_stream

from pgdelta_cluster import CONTROLDATA, PG_BIN, SKIP_REASON, available
from pgdelta_support import (PAGE, REPO_ROOT, Outcome, channel_pair, python_command, run_receiver_thread,
                             staging_dirs, subprocess_env, transfer, write_tree)
from test_pgdelta_datadir import RESET, TREE, build_datadir, stub_controldata
from test_pgdelta_engine import settle
from test_pgdelta_identity import SAMPLE

sys.path.insert(0, str(REPO_ROOT / 'experiments' / 'correctness'))
import bank  # noqa: E402  (EXP-25's load and checks)
import live_campaign  # noqa: E402  (EXP-25's harness: its kill rule and summary)

KEY = bytes(range(32))
SEGMENT = 1 << 20
SYSID = '7699399647675120679'
START, CHECKPOINT, STOP = '0/1000248', '0/1000F38', '0/1100138'  # segments 16 and 17 of 1 MiB
SEGMENTS = ('000000010000000000000010', '000000010000000000000011')


# --- LSNs, WAL names and backup_label -------------------------------------------------------------------

class WalRangeTests(unittest.TestCase):
    def test_lsns_round_trip(self):
        for text, value in (('0/0', 0), ('0/2000028', 0x2000028), ('1A/FF', (0x1A << 32) | 0xFF),
                            ('FFFFFFFF/FFFFFFFF', (1 << 64) - 1)):
            self.assertEqual(live.parse_lsn(text), value)
            self.assertEqual(live.format_lsn(value), text)
        for bad in ('', '0', '0/', 'x/1', '0/123456789', ' / '):
            with self.assertRaises(TransferError):
                live.parse_lsn(bad)

    def test_file_names_split_the_segment_number_at_4_gib(self):
        sixteen = 16 << 20
        self.assertEqual(live.wal_file_name(1, 6, sixteen), '000000010000000000000006')
        self.assertEqual(live.wal_file_name(1, 0xFF, sixteen), '0000000100000000000000FF')
        self.assertEqual(live.wal_file_name(1, 0x100, sixteen), '000000010000000100000000')
        self.assertEqual(live.wal_file_name(3, 0x1234, sixteen), '000000030000001200000034')
        self.assertEqual(live.wal_file_name(1, 5, 1 << 30), '000000010000000100000001')
        self.assertEqual(live.wal_file_name(2, 0xFFF, 1 << 20), '000000020000000000000FFF')
        self.assertEqual(live.wal_file_name(2, 0x1001, 1 << 20), '000000020000000100000001')
        for size in (0, 1000, 3 << 20, 2 << 30):
            with self.assertRaises(InvalidInput):
                live.wal_file_name(1, 1, size)

    def test_the_range_runs_from_the_start_lsns_segment_to_the_one_holding_the_byte_before_stop(self):
        sixteen = 16 << 20
        lsn = live.parse_lsn
        cases = [
            (('0/6000248', '0/707D138'), [6, 7]),          # what a run under load gave
            (('0/6000248', '0/6000F00'), [6]),             # one segment
            (('0/6000248', '0/7000000'), [6]),             # stop exactly on a boundary: pg_walfile_name's segment
            (('0/6000248', '0/7000001'), [6, 7]),
            (('0/6000248', '0/9000028'), [6, 7, 8, 9]),
            (('0/FF000028', '1/1000028'), [0xFF, 0x100, 0x101]),  # across the 4 GiB name boundary
        ]
        for (start, stop), expected in cases:
            with self.subTest(start=start, stop=stop):
                self.assertEqual(list(live.wal_segment_numbers(lsn(start), lsn(stop), sixteen)), expected)
        names = [name for _, name in live.wal_segments(1, lsn('0/FF000028'), lsn('1/1000028'), sixteen)]
        self.assertEqual(names, ['0000000100000000000000FF', '000000010000000100000000', '000000010000000100000001'])
        self.assertEqual(list(live.wal_segment_numbers(lsn('0/1000248'), lsn('0/1100138'), SEGMENT)), [16, 17])
        with self.assertRaises(TransferError):
            live.wal_segment_numbers(lsn('0/7000000'), lsn('0/6000000'), sixteen)
        with self.assertRaises(InvalidInput):
            live.wal_segment_numbers(1, 2, 12345)


LABEL = ('START WAL LOCATION: 0/6000248 (file 000000010000000000000006)\n'
         'CHECKPOINT LOCATION: 0/6551C38\nBACKUP METHOD: streamed\nBACKUP FROM: primary\n'
         'START TIME: 2026-10-25 05:07:00 EDT\nLABEL: pgdelta live 957c7688\nSTART TIMELINE: 1\n')


class BackupLabelTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(live.parse_backup_label(LABEL), {
            'start_lsn': 0x6000248, 'start_wal_file': '000000010000000000000006', 'checkpoint_lsn': 0x6551C38,
            'backup_method': 'streamed', 'backup_from': 'primary', 'label': 'pgdelta live 957c7688',
            'start_timeline': 1})

    def test_check_accepts_only_this_runs_label(self):
        sixteen = 16 << 20
        self.assertEqual(live.check_backup_label(LABEL, 0x6000248, sixteen)['checkpoint_lsn'], 0x6551C38)
        with self.assertRaisesRegex(VerificationError, 'pg_backup_start returned 0/6000250'):
            live.check_backup_label(LABEL, 0x6000250, sixteen)
        with self.assertRaisesRegex(InvalidInput, 'standby'):
            live.check_backup_label(LABEL.replace('FROM: primary', 'FROM: standby'), 0x6000248, sixteen)
        with self.assertRaisesRegex(VerificationError, 'names WAL file'):
            live.check_backup_label(LABEL.replace('(file 000000010000000000000006)',
                                                  '(file 000000010000000000000007)'), 0x6000248, sixteen)
        with self.assertRaisesRegex(VerificationError, 'checkpoint before'):
            live.check_backup_label(LABEL.replace('0/6551C38', '0/5000000'), 0x6000248, sixteen)
        for broken in (LABEL.replace('START WAL LOCATION', 'START WAL'), LABEL.replace('START TIMELINE: 1', ''),
                       LABEL.replace('CHECKPOINT LOCATION: 0/6551C38', 'CHECKPOINT LOCATION: nowhere'), ''):
            with self.assertRaises(TransferError):
                live.check_backup_label(broken, 0x6000248, sixteen)


# --- the control file and WAL page headers ----------------------------------------------------------------

def control_file(seed=1, offset=288):
    """An 8 KiB control file whose CRC-32C over its first `offset` bytes is stored at `offset`, as PostgreSQL 17's."""
    body = bytes((seed * 31 + index * 7) % 251 for index in range(offset))
    crc = live.crc32c(body).to_bytes(4, sys.byteorder)
    return (body + crc).ljust(PAGE, b'\0')


def wal_segment(segno, size=SEGMENT, sysid=SYSID, page=PAGE, stop=None, timeline=1):
    """A WAL segment: a long header on its first page and a short one on every page up to `stop` (all if None)."""
    data = bytearray(size)
    base = segno * size
    data[:40] = struct.pack('=HHIQI4xQII', 0xD116, 0x0002, timeline, base, 0, int(sysid), size, page)
    end = size if stop is None else min(size, stop - base + page)
    for offset in range(page, end, page):
        data[offset:offset + 16] = struct.pack('=HHIQ', 0xD116, 0, timeline, base + offset)
    return bytes(data)


class ControlFileTests(unittest.TestCase):
    def test_crc32c(self):
        self.assertEqual(live.crc32c(b'123456789'), 0xE3069283)
        self.assertEqual(live.crc32c(b''), 0)

    def test_the_crc_is_found_and_a_torn_read_is_not_accepted(self):
        self.assertEqual(live.control_crc_offset(control_file()), 288)
        self.assertEqual(live.control_crc_offset(control_file(offset=296)), 296)
        torn = bytearray(control_file())
        torn[100] ^= 1
        self.assertIsNone(live.control_crc_offset(bytes(torn)))
        torn = control_file(1)[:150] + control_file(2)[150:]  # half of an old write, half of a new one
        self.assertIsNone(live.control_crc_offset(torn))

    def test_reader_rereads_pg_control_until_two_reads_agree_with_a_valid_crc(self):
        with tempfile.TemporaryDirectory() as temp:
            root = write_tree(Path(temp) / 'D', {CONTROL_FILE: control_file(1)})
            reader = live.Reader(root, root, Hasher(KEY), PAGE, 4)
            reads = iter([control_file(1)[:150] + bytes(PAGE - 150), control_file(2), control_file(2)])
            real = live._pread_upto
            with mock.patch.object(live, '_pread_upto', lambda fd, length, offset: next(reads)):
                tree = reader.hash(FileEntry(CONTROL_FILE, PAGE, 0o600))
            self.assertEqual(reader.pinned[CONTROL_FILE], control_file(2))
            self.assertEqual(reader.counts['control_file_reads'], 3)
            self.assertEqual(tree.digest, hash_stream(io.BytesIO(control_file(2)), Hasher(KEY), PAGE, 4).digest)
            self.assertIs(live._pread_upto, real)
            (root / CONTROL_FILE).write_bytes(bytes(PAGE))  # never valid
            with mock.patch.object(live, 'CONTROL_READS', 3), mock.patch.object(live.time, 'sleep'):
                with self.assertRaisesRegex(VerificationError, 'valid CRC'):
                    live.Reader(root, root, Hasher(KEY), PAGE, 4).hash(FileEntry(CONTROL_FILE, PAGE, 0o600))


class WalHeaderTests(unittest.TestCase):
    def check(self, data, segno=16, stop=None, sysid=SYSID, name=SEGMENTS[0]):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / name
            path.write_bytes(data)
            fd = os.open(path, os.O_RDONLY)
            try:
                live.check_wal_segment(fd, name, segno, SEGMENT, sysid, stop)
            finally:
                os.close(fd)

    def test_a_segment_must_be_the_one_its_name_says(self):
        self.check(wal_segment(16))
        self.check(wal_segment(17, stop=0x1100138), 17, stop=0x1100138, name=SEGMENTS[1])
        good = wal_segment(16)
        cases = {
            'is at 0/1100000, not 0/1000000': (wal_segment(17), {}),  # a recycled file under the wrong name
            'system identifier': (wal_segment(16, sysid='42'), {}),
            'segment size': (good[:32] + struct.pack('=I', 2 * SEGMENT) + good[36:], {}),
            'WAL page size': (good[:36] + struct.pack('=I', 1000) + good[40:], {}),
            'no long header': (good[:2] + b'\0\0' + good[4:], {}),
            'not on disk': (wal_segment(17, stop=0x1100000 + PAGE), {'segno': 17, 'stop': 0x1100138 + 3 * PAGE,
                                                                     'name': SEGMENTS[1]}),
        }
        for message, (data, options) in cases.items():
            with self.subTest(problem=message):
                with self.assertRaisesRegex(VerificationError, message):
                    self.check(data, **options)


# --- what is selected from a running cluster ---------------------------------------------------------------

class LiveSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = build_datadir(Path(self.temp.name) / 'D')
        write_tree(self.root, {'postmaster.pid': b'4242\n', 'pg_wal/00000002.history': b'1\t0/3000000\tx\n'})
        self.identity = {'system_identifier': SYSID}

    def test_as_pg_basebackup_from_a_running_server(self):
        selection = select_live(self.root, self.identity)
        files = [entry.path for entry in selection.inventory.files]
        expected = sorted(path for path, rule in TREE.items() if rule is None and not path.startswith('pg_wal/'))
        self.assertEqual(files, expected)  # no REDO segment, and no main fork reset from its init fork
        self.assertEqual(selection.sources, {})
        for main in RESET:
            self.assertNotIn(main, files)
            self.assertIn(main + '_init', files)
        self.assertIsNone(selection.redo_wal_file)
        directories = [entry.path for entry in selection.inventory.directories]
        for kept in ('pg_wal', 'pg_wal/archive_status', 'pg_wal/summaries'):
            self.assertIn(kept, directories)
        by_rule = selection.excluded_summary()['by_rule']
        self.assertEqual(by_rule['wal']['files'], 6)  # both segments, the history file and 3 below
        self.assertEqual(by_rule['named_files']['files'],
                         sum(1 for rule in TREE.values() if rule == 'named_files') + 1)  # postmaster.pid
        self.assertEqual(selection.excluded_summary()['unlogged_main_forks_reset'], 0)

    def test_entries_that_disappear_while_listed_are_skipped(self):
        vanishing = {'16384', 'pg_xact'}

        def scandir(path):
            with real_scandir(path) as entries:
                return FakeScandir([Vanishing(entry) if entry.name in vanishing else entry for entry in entries])

        real_scandir = os.scandir
        with mock.patch.object(datadir.os, 'scandir', scandir):
            selection = select_live(self.root, self.identity)
        files = [entry.path for entry in selection.inventory.files]
        self.assertNotIn('base/5/16384', files)
        self.assertNotIn('pg_xact/0000', files)
        self.assertIn('base/5/16384_fsm', files)
        os.unlink(self.root / 'postmaster.pid')
        with mock.patch.object(datadir.os, 'scandir', scandir):
            with self.assertRaisesRegex(InvalidInput, 'cannot stat'):  # a stopped data directory must not lose files
                datadir.select(self.root, stub_controldata(Path(self.temp.name) / 'bin'))

    def test_a_directory_removed_while_listed_has_no_entries(self):
        def scandir(path):
            if str(path).endswith('base/5'):
                raise FileNotFoundError(2, 'No such file or directory', str(path))
            return real_scandir(path)

        real_scandir = os.scandir
        with mock.patch.object(datadir.os, 'scandir', scandir):
            selection = select_live(self.root, self.identity)
        self.assertFalse(any(entry.path.startswith('base/5/') for entry in selection.inventory.files))

    def test_tablespaces_and_symlinks_are_still_refused(self):
        os.symlink('/', self.root / 'base/5/link')
        with self.assertRaisesRegex(InvalidInput, 'symlinks'):
            select_live(self.root, self.identity)


class Vanishing:
    """A directory entry whose file is removed between the listing and its stat."""

    def __init__(self, entry):
        self.name, self.path = entry.name, entry.path

    def stat(self, follow_symlinks=True):
        raise FileNotFoundError(2, 'No such file or directory', self.path)


class FakeScandir(list):
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


# --- reading files that change ----------------------------------------------------------------------------

class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hasher = Hasher(KEY)

    def digest_of(self, data):
        return hash_stream(io.BytesIO(data), self.hasher, PAGE, 4).digest

    def reader(self, pool=None):
        return live.Reader(self.root, self.root, self.hasher, PAGE, 4, pool)

    def test_a_data_file_is_read_at_its_listed_length(self):
        data = bytes(range(256)) * (10 * PAGE // 256)
        write_tree(self.root, {'grew': data + b'x' * PAGE, 'shrank': data[:3 * PAGE + 100], 'same': data})
        for jobs in (1, 2):
            with HashPool(jobs, key=KEY) as pool, self.subTest(jobs=jobs):
                reader = self.reader(pool)
                for name in ('grew', 'shrank', 'same', 'vanished'):
                    tree = reader.hash(FileEntry(name, len(data), 0o600))
                    expected = {'shrank': data[:3 * PAGE + 100] + bytes(len(data) - 3 * PAGE - 100),
                                'vanished': bytes(len(data))}.get(name, data)
                    self.assertEqual((tree.size, tree.digest), (len(data), self.digest_of(expected)), name)
                    handle = reader.open(FileEntry(name, len(data), 0o600))
                    try:
                        self.assertEqual(handle.read(0, len(data)), expected, name)
                    finally:
                        handle.close()
                self.assertEqual({key: reader.counts[key] for key in ('files_grew', 'files_shrunk', 'files_vanished')},
                                 {'files_grew': 1, 'files_shrunk': 1, 'files_vanished': 1})

    def test_a_file_that_shrinks_while_hashed_on_workers_is_read_again_with_zeros(self):
        data = os.urandom(40 * PAGE)
        write_tree(self.root, {'f': data})
        real = HashPool.node_digests

        def shrink_first(pool, path, *args, **kwargs):
            os.truncate(path, 5 * PAGE)
            return real(pool, path, *args, **kwargs)

        with HashPool(2, key=KEY) as pool, mock.patch.object(HashPool, 'node_digests', shrink_first):
            tree = self.reader(pool).hash(FileEntry('f', len(data), 0o600))
        self.assertEqual(tree.digest, self.digest_of(data[:5 * PAGE] + bytes(35 * PAGE)))

    def test_the_composite_digest_is_that_of_the_pages_as_sent(self):
        old = os.urandom(13 * PAGE + 5)
        write_tree(self.root, {'f': old})
        tree = self.reader().hash(FileEntry('f', len(old), 0o600))
        new = bytearray(old)
        new[2 * PAGE:3 * PAGE] = os.urandom(PAGE)  # changed after hashing, and sent
        new[12 * PAGE:] = os.urandom(PAGE + 5)  # and the last two, the short last page too
        sent = {page: self.hasher.leaf(bytes(new[page * PAGE:(page + 1) * PAGE])) for page in (2, 12, 13)}
        self.assertEqual(live.composite_digest(tree, self.hasher, sent), self.digest_of(bytes(new)))
        self.assertEqual(live.composite_digest(tree, self.hasher, {}), tree.digest)

    def test_wal_is_read_strictly(self):
        write_tree(self.root, {'pg_wal/seg': wal_segment(16)})
        entry = FileEntry('pg_wal/seg', SEGMENT, 0o600)
        reader = self.reader()
        checked = []
        reader.hold(entry.path, checked.append)
        tree = reader.hash(entry)
        self.assertEqual(len(checked), 1)
        self.assertEqual(tree.digest, self.digest_of(wal_segment(16)))
        # Same bytes, another file: made beside it and renamed over it, so the old inode is still in use when the
        # new one is allocated. After an unlink, Linux may give the new file the freed inode number (a CI flake).
        (self.root / 'pg_wal/seg.new').write_bytes(wal_segment(16))
        os.replace(self.root / 'pg_wal/seg.new', self.root / 'pg_wal/seg')
        with self.assertRaisesRegex(VerificationError, 'replaced'):
            reader.open(entry)
        (self.root / 'pg_wal/seg').write_bytes(wal_segment(16)[:-PAGE])
        with self.assertRaisesRegex(VerificationError, 'changed size'):
            reader.hash(entry)
        os.unlink(self.root / 'pg_wal/seg')
        with self.assertRaisesRegex(VerificationError, 'disappeared'):
            reader.hash(entry)


# --- the receiver's APPEND ------------------------------------------------------------------------------

class AppendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.basis, self.output = self.root / 'B', self.root / 'O'
        write_tree(self.basis, {'PG_VERSION': b'17\n', CONTROL_FILE: bytes(PAGE), 'backup_label': b'old\n'})
        self.hasher = Hasher(KEY)

    def session(self, script, appends=True, in_place=False):
        """A hand-written sender: HELLO (one file, PG_VERSION), that file, then `script(send)`."""
        send_channel, receive_channel = channel_pair()
        outcome = Outcome()
        thread = run_receiver_thread(receive_channel, outcome, self.basis, None if in_place else self.output,
                                     reuse_any_basis=True, in_place=in_place)
        files = [['PG_VERSION', 3, 0o600]] + ([[CONTROL_FILE, PAGE, 0o600]] if in_place else [])
        hello = {'protocol_version': 1, 'block': PAGE, 'node_blocks': 4, 'key': base64.b64encode(KEY).decode(),
                 'source_identity': None, 'root_mode': 0o700,
                 'directories': [['global', 0o700], ['pg_wal', 0o700]], 'files': files}
        if appends is not None:
            hello['appends'] = appends

        def send_file(index, data):
            send_channel.send(protocol.PAGE_DATA, protocol.page_data_head(index, 0), data)
            digest = hash_stream(io.BytesIO(data), self.hasher, PAGE, 4).digest
            send_channel.send(protocol.FILE_DONE, protocol.encode_file_done(index, len(data), digest))

        try:
            send_channel.send_json(protocol.HELLO, hello)
            if in_place:  # B has PG_VERSION: it is written only after its digests were sent
                while send_channel.recv()[0] != protocol.BASIS_NODES:
                    pass
            send_file(0, b'17\n')
            if in_place:
                send_file(1, control_file())
            script(send_channel, send_file, len(files))
            send_channel._writer.close()
            while send_channel.recv() is not None:
                pass
        except protocol.ChannelClosed:
            pass
        finally:
            send_channel.close()
        thread.join(30)
        return outcome

    def test_appended_files_are_written_verified_and_kept(self):
        def script(channel, send_file, first):
            channel.send_json(protocol.APPEND, {'files': [['pg_wal/000000010000000000000010', 2 * PAGE, 0o600],
                                                          ['backup_label', 4, 0o600]]})
            send_file(first, b'w' * 2 * PAGE)
            send_file(first + 1, b'new\n')
            channel.send_json(protocol.COMPLETE, {'files': first + 2, 'directories': 2})

        outcome = self.session(script)
        self.assertIsNone(outcome.receiver_error, repr(outcome.receiver_error))
        self.assertEqual(outcome.receiver['appended_files'], 2)
        self.assertEqual((self.output / 'backup_label').read_bytes(), b'new\n')
        self.assertEqual((self.output / 'pg_wal/000000010000000000000010').read_bytes(), b'w' * 2 * PAGE)
        outcome = self.session(script, in_place=True)
        self.assertIsNone(outcome.receiver_error, repr(outcome.receiver_error))
        self.assertEqual((self.basis / 'backup_label').read_bytes(), b'new\n')  # B's old label overwritten
        self.assertFalse(os.path.lexists(self.basis / MARKER))
        self.assertTrue(os.path.isfile(self.basis / CONTROL_FILE))
        settle(self)

    def test_protocol_violations(self):
        def complete(channel, send_file, first):
            channel.send_json(protocol.COMPLETE, {'files': first, 'directories': 2})

        def twice(channel, send_file, first):
            for _ in range(2):
                channel.send_json(protocol.APPEND, {'files': []})

        def clash(channel, send_file, first):
            channel.send_json(protocol.APPEND, {'files': [['PG_VERSION', 3, 0o600]]})

        def orphan(channel, send_file, first):
            channel.send_json(protocol.APPEND, {'files': [['pg_xact/0000', 3, 0o600]]})

        def reserved(channel, send_file, first):
            channel.send_json(protocol.APPEND, {'files': [[MARKER, 3, 0o600]]})

        def extra_field(channel, send_file, first):
            channel.send_json(protocol.APPEND, {'files': [], 'more': 1})

        cases = [
            (complete, True, False, ProtocolError, 'before the APPEND'),
            (twice, True, False, ProtocolError, 'a second APPEND'),
            (clash, True, False, ProtocolError, 'already listed'),
            (orphan, True, False, InvalidInput, 'no listed parent'),
            (reserved, True, True, InvalidInput, 'reserves'),
            (extra_field, True, False, ProtocolError, 'just "files"'),
            (clash, None, False, ProtocolError, 'unexpected APPEND'),  # not announced
        ]
        for script, appends, in_place, error, message in cases:
            with self.subTest(script=script.__name__, appends=appends):
                shutil.rmtree(self.output, ignore_errors=True)
                outcome = self.session(script, appends=appends, in_place=in_place)
                self.assertIsInstance(outcome.receiver_error, error)
                self.assertIn(message, str(outcome.receiver_error))
                self.assertFalse(os.path.lexists(self.output))
        settle(self)

    def test_hello_appends_must_be_a_boolean(self):
        outcome = self.session(lambda *args: None, appends='yes')
        self.assertIsInstance(outcome.receiver_error, ProtocolError)
        self.assertIn('appends must be true or false', str(outcome.receiver_error))


# --- whole sessions against a stand-in for psql -----------------------------------------------------------

class FakeSession:
    """LiveSource's session, scripted: it records statements and fails on the ones named in `fail`."""

    def __init__(self, conninfo, psql, fail=(), answers=None):
        self.statements, self.fail, self.closed = [], fail, False
        self.answers = answers or {}

    def query(self, sql):
        self.statements.append(sql)
        if any(text in sql for text in self.fail):
            raise TransferError('the psql session ended: ERROR:  injected')
        return []

    def value(self, sql):
        self.query(sql)
        return next(answer for key, answer in self.answers.items() if key in sql)

    def close(self):
        self.closed = True


class CleanupTests(unittest.TestCase):
    """LiveSource.close(): backup mode is ended and the slot dropped, whatever failed."""

    def source(self, fail=()):
        session = FakeSession(None, None, fail)
        source = live.LiveSource('/nonexistent', 'host=x', 'pg_controldata', session_class=lambda *a: session)
        source.session = session
        return source, session

    def test_after_start_backup_mode_is_ended_and_the_slot_dropped(self):
        source, session = self.source()
        source.slot_created, source.in_backup = True, True
        source.close()
        self.assertEqual(session.statements, ['SELECT pg_backup_stop(false)',
                                              f"SELECT pg_drop_replication_slot('{source.slot}')"])
        self.assertTrue(session.closed)
        self.assertEqual(source.cleanup, 'pg_backup_stop called; slot dropped; session closed')
        self.assertIsNone(source.session)
        source.close()  # again: nothing more
        self.assertEqual(len(session.statements), 2)

    def test_a_broken_session_is_closed_which_ends_backup_mode(self):
        source, session = self.source(fail=('pg_backup_stop', 'pg_drop_replication_slot'))
        source.slot_created, source.in_backup = True, True
        source.close()
        self.assertTrue(session.closed)
        self.assertIn('pg_backup_stop failed', source.cleanup)
        self.assertIn('session closed, which ends backup mode and drops the temporary slot', source.cleanup)
        self.assertFalse(source.in_backup)

    def test_before_start_only_the_slot_is_dropped(self):
        source, session = self.source()
        source.slot_created = True
        source.close()
        self.assertEqual(session.statements, [f"SELECT pg_drop_replication_slot('{source.slot}')"])

    def test_the_snapshot_of_the_broken_variant_is_removed(self):
        source, session = self.source()
        source.snapshot = Path(tempfile.mkdtemp(prefix='pgdelta-read-before-start-'))
        source.close()
        self.assertIsNone(source.snapshot)

    def test_the_connection_string_is_recorded_without_a_password(self):
        for conninfo, expected in (("host=h password=secret user=u", 'host=h password=*** user=u'),
                                   ("host=h password = 'a b' user=u", 'host=h password = *** user=u'),
                                   ('postgresql://u:secret@h/db', 'postgresql://u:***@h/db'),
                                   ('host=h user=u', 'host=h user=u')):
            self.assertEqual(live.redact_conninfo(conninfo), expected)


class StubServerTestCase(unittest.TestCase):
    """A synthetic data directory, a stub pg_controldata and a stand-in for psql answering as its server."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.datadir = build_datadir(self.root / 'D')
        write_tree(self.datadir, {'postmaster.pid': b'4242\n', 'pg_wal/00000002.history': b'1\t0/900000\tx\n',
                                  CONTROL_FILE: control_file(), f'pg_wal/{SEGMENTS[0]}': wal_segment(16),
                                  f'pg_wal/{SEGMENTS[1]}': wal_segment(17, stop=live.parse_lsn(STOP)),
                                  'pg_wal/000000010000000000000012': wal_segment(18)})  # after stop: not sent
        self.stub = stub_controldata(self.root / 'bin', SAMPLE.replace('16777216', str(SEGMENT)).replace(
            'shut down', 'in production'))
        self.log = self.root / 'psql.log'
        self.config = {'data_directory': str(self.datadir), 'system_identifier': SYSID,
                       'wal_segment_size': SEGMENT, 'slot_lsn': '0/900000', 'start_lsn': START,
                       'checkpoint_lsn': CHECKPOINT, 'stop_lsn': STOP, 'log': str(self.log),
                       # Written when pg_backup_start is answered: read after it, the copy holds these bytes.
                       'on_start': {'base/5/16384': 'aa' * 64, 'base/5/77777': 'bb' * PAGE}}
        self.psql = self.root / 'bin' / 'psql'
        self.psql.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{REPO_ROOT / "tests" / "stub_psql.py"}" "$@"\n')
        self.psql.chmod(0o755)
        self.old = (self.datadir / 'base/5/16384').read_bytes()

    def env(self, **extra):
        (self.root / 'psql.json').write_text(json.dumps(self.config))
        return mock.patch.dict(os.environ, {'STUB_PSQL_CONFIG': str(self.root / 'psql.json'), **extra})

    def statements(self):
        if not self.log.exists():
            return []
        return [json.loads(line)['sql'] for line in self.log.read_text().splitlines()]

    def kinds(self):
        """The statements, by what they do."""
        names = (('SET ', 'settings'), ('pg_is_in_recovery', 'server'), ('pg_create_physical', 'create slot'),
                 ('pg_backup_start', 'start'), ('FROM pg_backup_stop', 'stop'), ('pg_backup_stop', 'abort stop'),
                 ('pg_drop_replication_slot', 'drop slot'))
        return [next(name for key, name in names if key in sql) for sql in self.statements()]

    def run_live(self, output=None, in_place=False, basis=None, checkpoint=None, **extra_env):
        basis = basis or self.root / 'B'
        if not basis.exists():
            write_tree(basis, {'PG_VERSION': b'17\n', CONTROL_FILE: control_file(9)})
        options = {'checkpoint': checkpoint} if checkpoint else {}
        with self.env(**extra_env):
            return transfer(self.datadir, basis, None if in_place else (output or self.root / 'O'),
                            source_kind='live', pg_controldata=self.stub, pg_conn='host=stub user=u password=pw',
                            psql=self.psql, receiver_options={'in_place': in_place} if in_place else None,
                            **options)


class StubSessionTests(StubServerTestCase):
    def assert_holds_the_live_copy(self, copy):
        files = sorted(path.relative_to(copy).as_posix() for path in copy.rglob('*') if path.is_file())
        expected = sorted([path for path, rule in TREE.items() if rule is None and not path.startswith('pg_wal/')]
                          + ['base/5/77777', 'backup_label', 'pg_wal/00000002.history']
                          + [f'pg_wal/{name}' for name in SEGMENTS])
        self.assertEqual(files, expected)
        # Read after pg_backup_start returned: the bytes written when it was answered.
        self.assertEqual((copy / 'base/5/16384').read_bytes()[:64], bytes.fromhex('aa' * 64))
        self.assertEqual((copy / 'base/5/16384').read_bytes()[64:], self.old[64:])
        self.assertEqual((copy / 'base/5/77777').read_bytes(), bytes.fromhex('bb' * PAGE))
        for name in SEGMENTS:
            self.assertEqual((copy / 'pg_wal' / name).read_bytes(), (self.datadir / 'pg_wal' / name).read_bytes())
        label = live.parse_backup_label((copy / 'backup_label').read_text())
        self.assertEqual((label['start_lsn'], label['checkpoint_lsn'], label['label']),
                         (live.parse_lsn(START), live.parse_lsn(CHECKPOINT), 'stub'))
        self.assertEqual((copy / CONTROL_FILE).read_bytes(), control_file())

    def assert_live_stats(self, stats):
        self.assertEqual(stats['source_kind'], 'live')
        info = stats['live']
        self.assertEqual({key: info[key] for key in ('start_lsn', 'checkpoint_lsn', 'stop_lsn', 'timeline',
                                                     'wal_segments', 'wal_bytes', 'wal_first', 'wal_last',
                                                     'history_files', 'appended_files', 'read_before_start')},
                         {'start_lsn': START, 'checkpoint_lsn': CHECKPOINT, 'stop_lsn': STOP, 'timeline': 1,
                          'wal_segments': 2, 'wal_bytes': 2 * SEGMENT, 'wal_first': SEGMENTS[0],
                          'wal_last': SEGMENTS[1], 'history_files': 1, 'appended_files': 4,
                          'read_before_start': False})
        self.assertEqual(info['pg_conn'], 'host=stub user=u password=***')
        self.assertEqual(info['cleanup'], 'finished: slot dropped, session closed')
        self.assertLessEqual(info['start_requested_utc'], info['started_utc'])
        self.assertLessEqual(info['started_utc'], info['stop_requested_utc'])
        self.assertLessEqual(info['stop_requested_utc'], info['stopped_utc'])
        self.assertGreater(info['backup_mode_seconds'], 0)
        self.assertTrue(info['slot'].startswith(live.SLOT_PREFIX))
        self.assertEqual(stats['files']['total'], len(TREE) - sum(1 for rule in TREE.values() if rule)
                         - 1 + 1 + 4)  # less the REDO segment, plus base/5/77777 and the four appended
        self.assertEqual(self.kinds(), ['settings', 'server', 'create slot', 'start', 'stop', 'drop slot'])

    def test_new_copy(self):
        outcome = self.run_live()
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assert_holds_the_live_copy(self.root / 'O')
        self.assert_live_stats(outcome.sender)
        self.assertEqual(outcome.receiver['appended_files'], 4)
        settle(self)

    def test_in_place_the_commit_comes_after_the_wal_and_backup_label(self):
        basis = self.root / 'B'
        shutil.copytree(self.datadir, basis)
        write_tree(basis, {'backup_label': b'START WAL LOCATION: an older run\n', 'recovery.signal': b'',
                           'pg_wal/000000010000000000000003': b'old wal', 'base/5/16384': os.urandom(PAGE)})
        os.unlink(basis / 'postmaster.pid')
        committed = []
        real = live.LiveSource.finish

        def finish(source):  # the slot is dropped after the WAL was sent, before the receiver commits
            committed.append(os.path.lexists(basis / CONTROL_PENDING))
            return real(source)

        with mock.patch.object(live.LiveSource, 'finish', finish):
            outcome = self.run_live(in_place=True, basis=basis)
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assertEqual(committed, [True])
        self.assert_holds_the_live_copy(basis)
        self.assertFalse(os.path.lexists(basis / MARKER))
        self.assertEqual(json.loads(receipt_path(basis).read_text())['status'], 'committed')
        self.assert_live_stats(outcome.sender)
        settle(self)

    def assert_wrong_by_design(self, outcome, variant):
        """The broken variant ran to the end (the receiver published), and the run still fails, with exit 4."""
        error = outcome.sender_error
        self.assertIsInstance(error, VerificationError)
        self.assertIn('deliberately broken test variant', str(error))
        stats = error.pgdelta_stats
        self.assertEqual((stats['status'], stats['exit_code']), ('failed', 4))
        self.assertIsNotNone(stats['receiver'])  # set only once the receiver confirmed publication
        info = stats['live']
        self.assertEqual((info['read_before_start'], info['test_variant']), (True, variant))
        self.assertIsNotNone(info['pre_read_seconds'])
        self.assertEqual(info['cleanup'], 'finished: slot dropped, session closed')
        copy = self.root / 'O'
        self.assertEqual((copy / 'base/5/16384').read_bytes(), self.old)  # read before pg_backup_start returned
        self.assertFalse(os.path.lexists(copy / 'base/5/77777'))  # listed before it was created
        self.assertFalse(list(Path(tempfile.gettempdir()).glob('pgdelta-read-before-start-*')))
        return info

    def test_read_before_start_variant_reads_the_old_bytes(self):
        info = self.assert_wrong_by_design(self.run_live(**{live.READ_BEFORE_START_ENV: '1'}), 'before_start')
        self.assertLessEqual(info['pre_read_started_utc'], info['start_requested_utc'])  # copied, then started
        settle(self)

    def test_files_are_read_only_after_pg_backup_start_returns_even_when_it_is_slow(self):
        self.config['start_delay'] = 1.0  # the start checkpoint takes a second: nothing may be read meanwhile
        outcome = self.run_live()
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assert_holds_the_live_copy(self.root / 'O')
        settle(self)

    def test_the_start_checkpoint_is_fast_by_default_and_spread_on_request(self):
        # C-ckpt: pg_backup_start's fast argument follows --checkpoint, as pg_basebackup's -c; the copy is the same.
        for checkpoint, fast in ((None, 'true'), ('spread', 'false')):
            with self.subTest(checkpoint=checkpoint):
                self.log.unlink(missing_ok=True)
                output = self.root / f'O-{checkpoint}'
                outcome = self.run_live(output=output, checkpoint=checkpoint)
                self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
                self.assert_holds_the_live_copy(output)
                start = next(sql for sql in self.statements() if 'pg_backup_start' in sql)
                self.assertIn(f", {fast})::text", start)
                self.assertEqual(outcome.sender['live']['checkpoint'], checkpoint or 'fast')
                settle(self)

    def test_an_unknown_checkpoint_mode_is_refused(self):
        with self.assertRaises(InvalidInput):
            live.LiveSource(self.datadir, 'host=stub', self.stub, psql=self.psql, checkpoint='immediate')

    def test_the_during_start_variant_reads_while_pg_backup_start_runs(self):
        self.config['start_delay'] = 1.0
        info = self.assert_wrong_by_design(self.run_live(**{live.READ_BEFORE_START_ENV: 'during'}), 'during_start')
        self.assertLessEqual(info['start_requested_utc'], info['pre_read_started_utc'])  # started, then copied
        self.assertLess(info['pre_read_started_utc'], info['started_utc'] - 0.5)  # long before it returned
        self.assertEqual(self.kinds(), ['settings', 'server', 'create slot', 'start', 'stop', 'drop slot'])
        settle(self)

    def test_the_oracle_reads_the_same_files_correctly_once_pg_backup_start_returns(self):
        # EXP-27: beside the broken read, a correct read of the same files, made as soon as pg_backup_start returns.
        self.config['start_delay'] = 1.0
        oracle = self.root / 'oracle'
        self.assert_wrong_by_design(self.run_live(**{live.READ_BEFORE_START_ENV: 'during',
                                                     live.ORACLE_ENV: str(oracle)}), 'during_start')
        self.assertEqual((oracle / 'base/5/16384').read_bytes()[:64], bytes.fromhex('aa' * 64))
        self.assertEqual((oracle / 'base/5/16384').read_bytes()[64:], self.old[64:])
        self.assertFalse(os.path.lexists(oracle / 'base/5/77777'))  # not in the broken read's listing
        settle(self)

    def test_the_oracle_switch_does_nothing_without_a_broken_variant(self):
        oracle = self.root / 'oracle'
        outcome = self.run_live(**{live.ORACLE_ENV: str(oracle)})
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assertFalse(oracle.exists())
        settle(self)

    def test_an_unknown_test_switch_value_is_refused(self):
        outcome = self.run_live(**{live.READ_BEFORE_START_ENV: 'yes'})
        self.assertIsInstance(outcome.sender_error, InvalidInput)
        self.assertIn(live.READ_BEFORE_START_ENV, str(outcome.sender_error))
        self.assertEqual(self.statements(), [])
        settle(self)

    def test_wal_that_changes_after_it_was_hashed_fails_the_run(self):
        real = live.Reader._hash_strict
        target = f'pg_wal/{SEGMENTS[0]}'

        def hash_then_change(reader, entry):
            tree = real(reader, entry)
            if entry.path == target:  # rewritten in place (same file, same size), as a recycled segment would be
                with open(reader.wal_root / entry.path, 'r+b') as stream:
                    stream.seek(300000)
                    stream.write(b'\xde\xad\xbe\xef' * 1024)
            return tree

        with mock.patch.object(live.Reader, '_hash_strict', hash_then_change):
            outcome = self.run_live()
        self.assertIsInstance(outcome.sender_error, VerificationError)
        self.assertIn(f'{target} changed after it was hashed', str(outcome.sender_error))
        self.assertFalse(os.path.lexists(self.root / 'O'))
        self.assertEqual(self.kinds()[-1], 'drop slot')
        settle(self)

    def test_a_receiver_failure_ends_backup_mode_and_drops_the_slot(self):
        basis = self.root / 'B'
        write_tree(basis, {'PG_VERSION': b'17\n', CONTROL_FILE: control_file(9), 'postmaster.pid': b'1\n'})
        outcome = self.run_live(in_place=True, basis=basis)
        self.assertIsInstance(outcome.sender_error, PeerError)
        self.assertIn('postmaster.pid', str(outcome.sender_error))
        self.assertEqual(self.kinds(), ['settings', 'server', 'create slot', 'start', 'abort stop', 'drop slot'])
        self.assertEqual(outcome.sender_error.pgdelta_stats['live']['cleanup'],
                         'pg_backup_stop called; slot dropped; session closed')
        settle(self)

    def test_a_failure_after_stop_drops_the_slot(self):
        self.config['stop_lsn'] = '0/1300138'  # segment 19 is not there
        outcome = self.run_live()
        self.assertIsInstance(outcome.sender_error, VerificationError)
        self.assertIn('000000010000000000000013 is missing', str(outcome.sender_error))
        self.assertEqual(self.kinds(), ['settings', 'server', 'create slot', 'start', 'stop', 'drop slot'])
        self.assertFalse(os.path.lexists(self.root / 'O'))
        self.assertEqual(staging_dirs(self.root / 'O'), [])
        settle(self)

    def test_a_broken_session_is_closed(self):
        self.config['fail_on'] = 'FROM pg_backup_stop'
        outcome = self.run_live()
        self.assertIsInstance(outcome.sender_error, TransferError)
        self.assertIn('injected failure', str(outcome.sender_error))
        cleanup = outcome.sender_error.pgdelta_stats['live']['cleanup']
        self.assertIn('session closed, which ends backup mode and drops the temporary slot', cleanup)
        self.assertFalse(os.path.lexists(self.root / 'O'))
        settle(self)

    def test_a_wal_segment_that_is_not_this_backups_fails_the_run(self):
        (self.datadir / 'pg_wal' / SEGMENTS[0]).write_bytes(wal_segment(18))  # recycled under this name
        outcome = self.run_live()
        self.assertIsInstance(outcome.sender_error, VerificationError)
        self.assertIn("is not this backup's WAL", str(outcome.sender_error))
        self.assertFalse(os.path.lexists(self.root / 'O'))
        settle(self)

    def test_refusals(self):
        cases = {
            'does not run on': {'data_directory': str(self.root)},
            'in recovery (a standby)': {'in_recovery': True},
            'system identifier 42': {'system_identifier': '42'},
            'supports PostgreSQL 17 and 18 only': {'version': 160014},
            'WAL segment size': {'wal_segment_size': 2 * SEGMENT},
            'tablespace_map': {'spcmapfile': '16400 /somewhere\n'},
            'no such server': {'refuse_connect': True},
        }
        base = dict(self.config)
        for message, change in cases.items():
            with self.subTest(case=message):
                self.config = dict(base, **change)
                self.log.unlink(missing_ok=True)
                shutil.rmtree(self.root / 'O', ignore_errors=True)
                outcome = self.run_live()
                self.assertIsNotNone(outcome.sender_error)
                self.assertIn(message, str(outcome.sender_error))
                self.assertFalse(os.path.lexists(self.root / 'O'))
                kinds = self.kinds()
                if 'create slot' in kinds:
                    self.assertEqual(kinds[-1], 'drop slot')
                if message in ('does not run on', 'in recovery (a standby)', 'system identifier 42'):
                    self.assertNotIn('create slot', kinds)
        settle(self)

    def test_cli(self):
        stats_path = self.root / 'stats.json'
        (self.root / 'psql.json').write_text(json.dumps(self.config))
        write_tree(self.root / 'B0', {})
        result = subprocess.run(python_command(
            'local', '--source', self.datadir, '--source-kind', 'live', '--basis', self.root / 'B0',
            '--output', self.root / 'O', '--pg-controldata', self.stub, '--pg-conn', 'host=stub',
            '--psql', self.psql, '--stats-json', stats_path), capture_output=True, text=True, cwd=REPO_ROOT,
            env=dict(subprocess_env(), STUB_PSQL_CONFIG=str(self.root / 'psql.json')), timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        stats = json.loads(stats_path.read_text())
        self.assertEqual((stats['source_kind'], stats['live']['wal_segments']), ('live', 2))
        self.assertEqual(stats['live']['timeout_seconds'], live.DEFAULT_TIMEOUT)
        self.assertNotIn('WARNING', result.stderr)
        # The broken variant: a loud warning before and after, and exit 4, though the receiver published.
        stats_path.unlink()
        variant = subprocess.run(python_command(
            'local', '--source', self.datadir, '--source-kind', 'live', '--basis', self.root / 'B0',
            '--output', self.root / 'O-variant', '--pg-controldata', self.stub, '--pg-conn', 'host=stub',
            '--psql', self.psql, '--live-timeout', '30', '--stats-json', stats_path), capture_output=True,
            text=True, cwd=REPO_ROOT, timeout=120,
            env=dict(subprocess_env(), STUB_PSQL_CONFIG=str(self.root / 'psql.json'),
                     **{live.READ_BEFORE_START_ENV: '1'}))
        self.assertEqual(variant.returncode, 4, variant.stderr)
        self.assertEqual(variant.stderr.count('WARNING'), 2, variant.stderr)
        self.assertIn('wrong by design', variant.stderr)
        self.assertNotIn('complete (', variant.stderr)
        stats = json.loads(stats_path.read_text())
        self.assertEqual((stats['status'], stats['exit_code'], stats['live']['test_variant'],
                          stats['live']['timeout_seconds']), ('failed', 4, 'before_start', 30.0))
        self.assertTrue(os.path.isdir(self.root / 'O-variant'))
        for args, message in (((), '--source-kind live needs --pg-conn'),
                              (('--source-kind', 'datadir', '--pg-conn', 'x'), '--pg-conn and --psql are for'),
                              (('--source-kind', 'datadir', '--live-timeout', '5'), '--live-timeout is for'),
                              (('--source-kind', 'datadir', '--checkpoint', 'spread'), '--checkpoint is for'),
                              (('--source-kind', 'live', '--pg-conn', 'x', '--live-timeout', '0'),
                               'must be a positive number of seconds')):
            kind = () if args else ('--source-kind', 'live')
            refused = subprocess.run(python_command(
                'local', '--source', self.datadir, *kind, *args, '--basis', self.root / 'B0',
                '--output', self.root / 'O2', '--pg-controldata', self.stub), capture_output=True, text=True,
                cwd=REPO_ROOT, env=subprocess_env(), timeout=120)
            self.assertEqual(refused.returncode, 2, refused.stderr)
            self.assertIn(message, refused.stderr)


# --- a receiver that stops while backup mode is held --------------------------------------------------------

class StallTests(StubServerTestCase):
    """While backup mode and the slot are held, a receiver that stops answering or reading fails the run within
    the live timeout: backup mode is ended and the slot dropped, so the source holds no WAL for it."""

    def setUp(self):
        super().setUp()
        self.release = threading.Event()  # lets the stalled stand-in receivers go at the end
        self.addCleanup(self.release.set)
        # More data than loopback buffers hold, so a receiver that stops reading blocks the sender before the end
        # whatever the stand-in data directory's size (with PG_VERSION as initdb writes it, it shrank by 24 KiB).
        (self.datadir / 'base' / '1' / '16999').write_bytes(bytes(range(256)) * (16 << 10))

    def run_against(self, receive, timeout=1.0):
        """The live sender over TCP to the stand-in `receive(conn)`: (outcome, seconds until it gave up)."""
        listener = socket.create_server(('127.0.0.1', 0))
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)  # a small window: sends block early
        self.addCleanup(listener.close)

        def serve():
            conn, _ = listener.accept()
            try:
                receive(conn)
            finally:
                for action in (lambda: conn.shutdown(socket.SHUT_RDWR), conn.close):
                    try:
                        action()
                    except OSError:
                        pass

        threading.Thread(target=serve, name='test-stalled-receiver', daemon=True).start()
        outcome = Outcome()

        def send():
            channel = transport.connect_channel('127.0.0.1', listener.getsockname()[1])
            channel.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)  # no autotuned buffer to fill
            try:
                outcome.sender = sender.run_sender(channel, self.datadir, source_kind='live',
                                                   pg_controldata=self.stub, pg_conn='host=stub user=u',
                                                   psql=self.psql, live_timeout=timeout)
            except BaseException as exc:
                outcome.sender_error = exc
            finally:
                channel.close()

        with self.env():
            started = time.monotonic()
            thread = threading.Thread(target=send, name='test-sender', daemon=True)
            thread.start()
            thread.join(30)
            elapsed = time.monotonic() - started
        self.assertFalse(thread.is_alive(), 'the sender is still blocked on the receiver after 30 s')
        return outcome, elapsed

    def silent(self, conn):
        """Accepts, then never reads or answers: a receiver host that stopped responding."""
        self.release.wait(60)

    def stops_reading(self, conn, until=None):
        """Answers HELLO with ACCEPT (full mode), reads every frame up to the first of type `until` (none: up to
        none), then reads nothing more."""
        channel = protocol.Channel(conn.makefile('rb'), conn.makefile('wb'))
        self.assertEqual(channel.recv()[0], protocol.HELLO)
        channel.send_json(protocol.ACCEPT, {'mode': 'full', 'reason': 'stand-in', 'basis_files': []})
        while until is not None and channel.recv()[0] != until:
            pass
        self.release.wait(60)

    def stops_reading_at_append(self, conn):
        """Takes every data file, then stops reading at APPEND: pg_backup_stop has returned, the slot is held."""
        self.stops_reading(conn, protocol.APPEND)

    def assert_gave_up(self, outcome, elapsed, doing, after_stop=False):
        error = outcome.sender_error
        self.assertIsInstance(error, TransferError, repr(error))
        self.assertIn(f'the receiver made no progress for 1 s while the sender was {doing}', str(error))
        self.assertLess(elapsed, 15)
        info = error.pgdelta_stats['live']
        if after_stop:  # stalled while it sent the WAL: backup mode had ended, the slot still held it
            self.assertEqual(self.kinds(), ['settings', 'server', 'create slot', 'start', 'stop', 'drop slot'])
            self.assertEqual(info['cleanup'], 'slot dropped; session closed')
        else:
            self.assertEqual(self.kinds(), ['settings', 'server', 'create slot', 'start', 'abort stop', 'drop slot'])
            self.assertEqual(info['cleanup'], 'pg_backup_stop called; slot dropped; session closed')
        self.assertEqual((info['timeout_seconds'], info['timed_out']), (1.0, True))
        self.assertEqual(info['keepalive']['SO_KEEPALIVE'], 1)
        settle(self)

    def test_a_receiver_that_never_answers(self):
        outcome, elapsed = self.run_against(self.silent)
        self.assert_gave_up(outcome, elapsed, 'waiting for ACCEPT')

    def test_a_receiver_that_stops_reading(self):
        outcome, elapsed = self.run_against(self.stops_reading)
        self.assert_gave_up(outcome, elapsed, 'sending')

    def test_a_receiver_that_stops_reading_while_the_wal_is_sent(self):
        outcome, elapsed = self.run_against(self.stops_reading_at_append)
        self.assert_gave_up(outcome, elapsed, 'sending', after_stop=True)


class StallWatchTests(unittest.TestCase):
    """What counts as a stall: one send blocked, or a wait with no message from the receiver, for the timeout."""

    def test_a_stall_is_measured_from_the_last_sign_of_progress(self):
        channel = mock.Mock(sending_since=None)
        inbox = mock.Mock(waiting_since=None, waiting_for=None, last_message=0.0)
        watch = sender.StallWatch(channel, inbox, 10.0)
        self.assertIsNone(watch.stalled(1000.0))  # not blocked on the receiver: working is no stall
        channel.sending_since = 100.0
        self.assertIsNone(watch.stalled(109.9))
        self.assertEqual(watch.stalled(110.0), 'sending')
        channel.sending_since = None
        inbox.waiting_since, inbox.waiting_for, inbox.last_message = 100.0, 'ACCEPT', 105.0
        self.assertIsNone(watch.stalled(114.0))  # a message 9 s ago: the receiver is alive
        self.assertEqual(watch.stalled(115.0), 'waiting for ACCEPT')
        inbox.last_message = 50.0
        self.assertEqual(watch.stalled(110.0), 'waiting for ACCEPT')
        with self.assertRaises(InvalidInput):
            sender.StallWatch(channel, inbox, 0)


class AbortTests(unittest.TestCase):
    def test_an_aborted_channel_sends_nothing_more_and_unblocks_a_blocked_send(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        channel = protocol.Channel(a.makefile('rb'), a.makefile('wb'), sock=a,
                                   aborter=lambda: a.shutdown(socket.SHUT_RDWR))
        self.addCleanup(channel.close)
        failure = []

        def flood():  # b never reads: the send blocks once the buffers are full
            try:
                while True:
                    channel.send(protocol.PAGE_DATA, protocol.page_data_head(0, 0), bytes(1 << 16))
            except protocol.ChannelClosed as exc:
                failure.append(exc)

        thread = threading.Thread(target=flood, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while channel.sending_since is None or time.monotonic() - channel.sending_since < 0.2:
            self.assertLess(time.monotonic(), deadline, 'the send never blocked')
            time.sleep(0.01)
        channel.abort()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failure), 1)
        self.assertIsNone(channel.sending_since)
        with self.assertRaisesRegex(protocol.ChannelClosed, 'aborted'):
            channel.send(protocol.COMPLETE, b'{}')

    def test_aborting_the_local_receiver_s_channel_kills_it(self):
        with tempfile.TemporaryDirectory() as temp:
            write_tree(Path(temp) / 'B', {'PG_VERSION': b'17\n'})
            process, channel = transport.spawn_local_receiver(Path(temp) / 'B', Path(temp) / 'O')
            try:
                channel.abort()
                self.assertEqual(process.wait(10), -signal.SIGKILL)
                with self.assertRaisesRegex(protocol.ChannelClosed, 'aborted'):
                    channel.send(protocol.HELLO, b'{}')
            finally:
                channel.close()

    def test_keepalive_on_a_tcp_socket(self):
        listener = socket.create_server(('127.0.0.1', 0))
        self.addCleanup(listener.close)
        sock = socket.create_connection(listener.getsockname())
        self.addCleanup(sock.close)
        options = transport.keep_alive(sock, 120)
        self.assertEqual(options['SO_KEEPALIVE'], 1)
        self.assertNotEqual(sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), 0)
        if hasattr(socket, 'TCP_KEEPINTVL'):
            self.assertEqual((options['TCP_KEEPINTVL'], options['TCP_KEEPCNT']), (15, 4))
            self.assertEqual(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL), 15)
        idle = 'TCP_KEEPIDLE' if hasattr(socket, 'TCP_KEEPIDLE') else 'TCP_KEEPALIVE'
        if hasattr(socket, idle):
            self.assertEqual(options[idle], 60)
        if hasattr(socket, 'TCP_USER_TIMEOUT'):
            self.assertEqual(options['TCP_USER_TIMEOUT'], 120000)
        self.assertEqual(options.get('errors'), None)


# --- EXP-25's harness: the kill rule and the summary ---------------------------------------------------------

class KillRuleTests(unittest.TestCase):
    """A B left by a kill, different from before the run, is 'blocked' only by pgdelta's own block."""

    def state(self, files, started=False, problems=('the copy did not start',)):
        with tempfile.TemporaryDirectory() as temp:
            basis = write_tree(Path(temp) / 'B', dict.fromkeys(files, b'x'))
            return live_campaign.kill_state(basis, started, list(problems))

    def test_only_pgdelta_s_block_is_a_pass_when_b_cannot_start(self):
        block = [CONTROL_PENDING, MARKER]
        self.assertEqual(self.state(block), 'blocked')
        self.assertEqual(self.state(block + ['base/1/1259']), 'blocked')
        # Committed (pg_control back in place) yet unable to start: an unrecoverable output, so wrong.
        self.assertEqual(self.state([CONTROL_FILE]), 'wrong')
        self.assertEqual(self.state([CONTROL_FILE, MARKER]), 'wrong')
        self.assertEqual(self.state([CONTROL_FILE, CONTROL_PENDING, MARKER]), 'wrong')
        self.assertEqual(self.state([CONTROL_PENDING]), 'wrong')  # renamed without the marker: not pgdelta's
        self.assertEqual(self.state([MARKER]), 'wrong')  # no control file at all

    def test_a_b_that_starts_is_new_or_wrong_by_the_checks(self):
        self.assertEqual(self.state([CONTROL_FILE], started=True, problems=()), 'new')
        self.assertEqual(self.state([CONTROL_FILE], started=True, problems=('invariant',)), 'wrong')


class CampaignSummaryTests(unittest.TestCase):
    def test_the_wal_range_share_and_the_negative_controls(self):
        records = [
            {'trial': 0, 'fault': 'none', 'negative_control': False, 'pass': True, 'pgdelta': {'wal_segments': 3}},
            {'trial': 1, 'fault': 'kill', 'negative_control': False, 'pass': True, 'pgdelta': {'wal_segments': 1}},
            {'trial': 2, 'negative_control': True, 'negative_variant': 'before_start', 'caught': True},
            {'trial': 3, 'negative_control': True, 'negative_variant': 'during_start', 'caught': False},
            {'trial': 4, 'negative_control': True, 'negative_variant': 'during_start', 'negative_failed': 'x'},
            {'trial': 5, 'harness_error': 'x'},
            {'trial': 6, 'fault': 'none', 'negative_control': False, 'pass': True, 'pgdelta': {'wal_segments': 2}},
        ]
        summary = live_campaign.summarize(records)
        self.assertEqual((summary['counted'], summary['multi_segment']), (3, 2))
        self.assertAlmostEqual(summary['multi_segment_share'], 2 / 3)
        self.assertFalse(summary['wal_range_covered'])  # registered: at least 90%
        self.assertEqual(summary['negative_controls'], {'before_start': {'ran': 1, 'caught': 1},
                                                        'during_start': {'ran': 1, 'caught': 0}})
        self.assertFalse(summary['controls_met'])
        records[1]['pgdelta']['wal_segments'] = 2
        records[3]['caught'] = True
        summary = live_campaign.summarize(records)
        self.assertTrue(summary['wal_range_covered'] and summary['controls_met'])


# --- EXP-25's checks of a recovered copy -------------------------------------------------------------------

class BankJudgeTests(unittest.TestCase):
    """bank.judge must pass a consistent copy and catch each way a copy can be wrong."""

    def state(self):
        return {'accounts': 100, 'sum_balance': 25, 'max_balance': 9000, 'sum_n': 25,
                'progress': [{'client': 0, 'n': 10, 'ts': 1_000_010}, {'client': 1, 'n': 15, 'ts': 1_000_015}],
                'ledger': [{'client': 0, 'min': 3, 'max': 10, 'count': 8}, {'client': 1, 'min': 1, 'max': 15,
                                                                             'count': 15}],
                'last': {'0': 1_000_010, '1': 1_000_015}}

    def reference(self):
        return {'required': {'0': 9, '1': 12}, 'first': {'0': 3, '1': 1}, 'ts': {'0': 1_000_010, '1': 1_000_015}}

    def judge(self, state=None, reference=None, stop=1_000_020):
        return bank.judge(state or self.state(), reference or self.reference(), 1_000_000, stop, 2, 100)

    def test_a_consistent_copy_passes(self):
        self.assertEqual(self.judge(), [])

    def test_every_violation_is_caught(self):
        def changed(path, value, which='state'):
            data = self.state() if which == 'state' else self.reference()
            target = data
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            return data

        cases = {
            'invariant broken': {'state': changed(('sum_balance',), 24)},
            'rolled-back change': {'state': changed(('max_balance',), 1_000_000_005)},
            'accounts': {'state': changed(('accounts',), 99)},
            'committed before pg_backup_start': {'reference': changed(('required', '1'), 16, 'reference')},
            'began after pg_backup_stop': {'stop': 1_000_015},
            "is not S's row": {'reference': changed(('ts', '0'), 1_000_009, 'reference')},
            'without a gap': {'state': changed(('ledger', 0, 'count'), 7)},
            'ledger row': {'state': changed(('last', '1'), 1_000_014)},
            'not one for each': {'state': dict(self.state(), progress=self.state()['progress'] * 2)},
        }
        for message, change in cases.items():
            with self.subTest(problem=message):
                problems = self.judge(**change)
                self.assertTrue(any(message in problem for problem in problems), problems)


# --- a real server under load -----------------------------------------------------------------------------

CLIENTS, SCALE = 4, 1


@unittest.skipUnless(available() and (PG_BIN / 'pgbench').exists() and (PG_BIN / 'pg_amcheck').exists(),
                     SKIP_REASON)
class RealServerTests(unittest.TestCase):
    """A primary with EXP-25's bank load; B, a copy of it taken stopped before the load ran."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.sockets = Path(tempfile.mkdtemp(prefix='pgl'))  # Unix socket paths are short
        cls.source, cls.stale = cls.root / 'S', cls.root / 'stale'
        cls.env = dict(os.environ, LC_ALL='C', LANG='C')
        cls.cmd([PG_BIN / 'initdb', '-D', cls.source, '-U', 't', '-A', 'trust', '--no-sync'])
        cls.ctl(cls.source, 'start')
        cls.sql('CREATE DATABASE bank')
        cls.cmd([PG_BIN / 'pgbench', '-i', '-q', '-s', SCALE, '-h', cls.sockets, '-p', 5432, '-U', 't', 'bank'])
        for statement in bank.setup_statements(CLIENTS):
            cls.sql(statement, 'bank')
        cls.sql('CREATE UNLOGGED TABLE scratch_unlogged AS SELECT g FROM generate_series(1, 1000) g', 'bank')
        cls.ctl(cls.source, 'stop')
        shutil.copytree(cls.source, cls.stale)
        cls.ctl(cls.source, 'start')
        cls.scripts = bank.write_scripts(cls.root / 'scripts')

    @classmethod
    def tearDownClass(cls):
        cls.ctl(cls.source, 'stop', check=False, mode='immediate')
        shutil.rmtree(cls.sockets, True)
        cls.temp.cleanup()

    @classmethod
    def cmd(cls, command, check=True):
        result = subprocess.run([str(part) for part in command], capture_output=True, text=True, env=cls.env,
                                timeout=300)
        if check and result.returncode:
            raise AssertionError(f'{command[0]} failed: {result.stderr[-2000:]}')
        return result

    @classmethod
    def ctl(cls, data, action, port=5432, check=True, mode='fast'):
        return cls.cmd([PG_BIN / 'pg_ctl', '-D', data, '-l', f'{data}.log', '-w', '-t', '60', '-m', mode, '-o',
                        f"-p {port} -k {cls.sockets} -c listen_addresses='' -c fsync=off", action], check=check)

    @classmethod
    def sql(cls, statement, database='postgres', port=5432):
        return cls.cmd([PG_BIN / 'psql', '-h', cls.sockets, '-p', port, '-U', 't', '-d', database, '-qAtX',
                        '-v', 'ON_ERROR_STOP=1', '-c', statement]).stdout.strip()

    def refresh(self, target, name, **extra_env):
        """A live refresh while the load writes; its statistics."""
        stats_path = self.root / f'{name}.json'
        load = subprocess.Popen(bank.pgbench_argv(PG_BIN / 'pgbench', self.scripts, clients=CLIENTS, scale=SCALE,
                                                  seconds=300, seed=len(name), host=self.sockets, port=5432,
                                                  user='t'), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=self.env)
        try:
            time.sleep(1.0)
            result = subprocess.run(python_command(
                'local', '--source', self.source, '--source-kind', 'live', '--pg-controldata', CONTROLDATA,
                '--pg-conn', f'host={self.sockets} port=5432 user=t dbname=postgres', *target,
                '--stats-json', stats_path), capture_output=True, text=True, cwd=REPO_ROOT,
                env=dict(subprocess_env(), **extra_env), timeout=300)
            time.sleep(0.5)
        finally:
            load.send_signal(signal.SIGTERM)
            load.wait(30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(stats_path.read_text())

    def check_copy(self, output, stats, name):
        """A disposable copy of the output recovers from its backup_label and holds a state between start and stop."""
        copy = self.root / f'{name}-copy'
        shutil.copytree(output, copy)
        self.ctl(copy, 'start', port=5433)
        try:
            log = Path(f'{copy}.log').read_text()
            self.assertIn(f'starting backup recovery with redo LSN {stats["live"]["start_lsn"]}', log)
            self.assertIn(f'completed backup recovery with redo LSN {stats["live"]["start_lsn"]} and end LSN '
                          f'{stats["live"]["stop_lsn"]}', log)
            state = json.loads(self.sql(bank.COPY_STATE_SQL, 'bank', port=5433))
            start_us, stop_us = bank.us(stats['live']['start_requested_utc']), bank.us(stats['live']['stopped_utc'])
            pairs = [(row['client'], row['n']) for row in state['progress']]
            reference = json.loads(self.sql(bank.source_reference_sql(start_us, pairs), 'bank'))
            self.assertEqual(bank.judge(state, reference, start_us, stop_us, CLIENTS, SCALE * bank.ACCOUNTS_PER_SCALE),
                             [])
            self.assertGreater(state['sum_n'], 0)
            self.assertEqual(self.sql('SELECT count(*) FROM scratch_unlogged', 'bank', port=5433), '0')
            amcheck = self.cmd([PG_BIN / 'pg_amcheck', '-h', self.sockets, '-p', 5433, '-U', 't', '--all',
                                '--heapallindexed', '--install-missing'], check=False)
            self.assertEqual(amcheck.returncode, 0, amcheck.stdout + amcheck.stderr)
        finally:
            self.ctl(copy, 'stop', port=5433, check=False)
        self.assertEqual(self.sql('SELECT count(*) FROM pg_replication_slots'), '0')

    def test_in_place_refresh_under_load_recovers_to_a_consistent_state(self):
        basis = self.root / 'B'
        shutil.copytree(self.stale, basis)
        stats = self.refresh(['--basis', basis, '--in-place'], 'in-place')
        info = stats['live']
        self.assertEqual((stats['mode'], stats['in_place'], info['cleanup']),
                         ('reuse', True, 'finished: slot dropped, session closed'))
        self.assertGreater(stats['pages']['reused'], 0)
        self.assertGreaterEqual(info['wal_segments'], 1)
        self.assertEqual(sorted(name for name in os.listdir(basis / 'pg_wal') if not name.startswith(('archive',
                                                                                                     'summaries'))),
                         [name for _, name in live.wal_segments(1, live.parse_lsn(info['start_lsn']),
                                                                live.parse_lsn(info['stop_lsn']), 16 << 20)])
        self.assertEqual(live.parse_backup_label((basis / 'backup_label').read_text())['start_lsn'],
                         live.parse_lsn(info['start_lsn']))
        self.assertFalse(os.path.lexists(basis / MARKER))
        self.check_copy(basis, stats, 'in-place')

    def test_new_copy_under_load_recovers_to_a_consistent_state(self):
        basis, output = self.root / 'B-new', self.root / 'O-new'
        shutil.copytree(self.stale, basis)
        stats = self.refresh(['--basis', basis, '--output', output], 'new')
        self.assertEqual(stats['receiver']['appended_files'], stats['live']['appended_files'])
        self.check_copy(output, stats, 'new')

    def test_a_failed_run_leaves_no_slot_and_no_backup_session(self):
        basis = self.root / 'B-refused'
        shutil.copytree(self.stale, basis)
        (basis / 'postmaster.pid').write_text('1\n')  # the receiver refuses it, after backup mode began
        stats_path = self.root / 'refused.json'
        result = subprocess.run(python_command(
            'local', '--source', self.source, '--source-kind', 'live', '--pg-controldata', CONTROLDATA,
            '--pg-conn', f'host={self.sockets} port=5432 user=t dbname=postgres', '--basis', basis, '--in-place',
            '--stats-json', stats_path), capture_output=True, text=True, cwd=REPO_ROOT, env=subprocess_env(),
            timeout=120)
        self.assertEqual(result.returncode, 2, result.stderr)
        stats = json.loads(stats_path.read_text())
        self.assertEqual(stats['live']['cleanup'], 'pg_backup_stop called; slot dropped; session closed')
        self.assertEqual(self.sql('SELECT count(*) FROM pg_replication_slots'), '0')
        self.assertEqual(self.eventually("SELECT count(*) FROM pg_stat_activity WHERE application_name = 'pgdelta'",
                                         '0'), '0')

    def eventually(self, query, expected, seconds=30):
        """The query's answer once it is `expected`, or after `seconds` (a backend ends a moment after its client)."""
        deadline = time.monotonic() + seconds
        while True:
            answer = self.sql(query)
            if answer == expected or time.monotonic() > deadline:
                return answer
            time.sleep(0.05)

    def test_a_killed_sender_leaves_no_slot_and_no_backup_session(self):
        basis = self.root / 'B-killed'
        shutil.copytree(self.stale, basis)
        process = subprocess.Popen(python_command(
            'local', '--source', self.source, '--source-kind', 'live', '--pg-controldata', CONTROLDATA,
            '--pg-conn', f'host={self.sockets} port=5432 user=t dbname=postgres', '--basis', basis, '--in-place'),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=REPO_ROOT, env=subprocess_env())
        self.assertEqual(self.eventually('SELECT count(*) FROM pg_replication_slots', '1'), '1')
        os.kill(process.pid, signal.SIGKILL)  # the sender alone: its psql sees its input end and exits
        process.wait()
        self.assertEqual(self.eventually('SELECT count(*) FROM pg_replication_slots', '0'), '0')
        self.assertEqual(self.eventually("SELECT count(*) FROM pg_stat_activity WHERE application_name = 'pgdelta'",
                                         '0'), '0')

    def test_a_receiver_that_stops_answering_does_not_hold_wal(self):
        """`send` to a receiver that accepts and never answers: it gives up within --live-timeout, ending backup
        mode and dropping the slot, instead of holding the source's WAL until pg_wal fills."""
        listener = socket.create_server(('127.0.0.1', 0))
        release = threading.Event()
        self.addCleanup(release.set)
        self.addCleanup(listener.close)

        def silent():
            conn, _ = listener.accept()
            with conn:
                release.wait(120)

        threading.Thread(target=silent, daemon=True).start()
        stats_path = self.root / 'stalled.json'
        started = time.monotonic()
        process = subprocess.Popen(python_command(
            'send', '--source', self.source, '--source-kind', 'live', '--pg-controldata', CONTROLDATA,
            '--pg-conn', f'host={self.sockets} port=5432 user=t dbname=postgres', '--live-timeout', '2',
            '--connect', f'127.0.0.1:{listener.getsockname()[1]}', '--stats-json', stats_path),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, cwd=REPO_ROOT, env=subprocess_env())
        self.assertEqual(self.eventually('SELECT count(*) FROM pg_replication_slots', '1'), '1')
        try:
            _, stderr = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            self.fail('the sender was still waiting for the receiver after 60 s')
        self.assertEqual(process.returncode, 3, stderr)
        self.assertLess(time.monotonic() - started, 30)
        self.assertIn('the receiver made no progress for 2 s', stderr)
        stats = json.loads(stats_path.read_text())
        self.assertEqual(stats['live']['cleanup'], 'pg_backup_stop called; slot dropped; session closed')
        self.assertTrue(stats['live']['timed_out'])
        self.assertEqual(self.sql('SELECT count(*) FROM pg_replication_slots'), '0')
        self.assertEqual(self.eventually("SELECT count(*) FROM pg_stat_activity WHERE application_name = 'pgdelta'",
                                         '0'), '0')


if __name__ == '__main__':
    unittest.main()
