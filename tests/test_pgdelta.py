"""The naive pgdelta: every refreshed copy must equal S byte for byte, and say what it sent."""
from __future__ import annotations

import io
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pgdelta import cli, protocol, receiver as receiver_module  # noqa: E402
from pgdelta.protocol import Channel  # noqa: E402
from pgdelta.receiver import run_receiver  # noqa: E402
from pgdelta.sender import run_sender  # noqa: E402
from pgdelta.tree import Entry, scan  # noqa: E402

CHUNK = 8192


def write(root: Path, rel: str, data: bytes, mode: int = 0o600) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def tree_bytes(root: Path) -> dict:
    """Every entry's kind, mode and content (or link target), for comparing two trees."""
    out = {}
    for entry in scan(root):
        if entry.kind == 'file':
            out[entry.path] = ('file', entry.mode, (root / entry.path).read_bytes())
        elif entry.kind == 'link':
            out[entry.path] = ('link', entry.target)
        else:
            out[entry.path] = ('dir', entry.mode)
    return out


class Refresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.s, self.b, self.o = root / 'S', root / 'B', root / 'O'
        self.s.mkdir()
        self.b.mkdir()
        self.stats = root / 'stats.json'

    def refresh(self, *extra) -> tuple[int, dict]:
        with redirect_stderr(io.StringIO()):
            code = cli.main(['local', '--source', str(self.s), '--basis', str(self.b), '--output', str(self.o),
                             '--stats-json', str(self.stats), *extra])
        report = json.loads(self.stats.read_text()) if code == 0 else {}
        return code, report

    def both(self, rel: str, data: bytes) -> None:
        write(self.s, rel, data)
        write(self.b, rel, data)

    def test_identical_copies_send_no_chunk(self):
        self.both('base/1/16384', os.urandom(5 * CHUNK))
        code, report = self.refresh()
        self.assertEqual(code, 0)
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        self.assertEqual(report['counts']['chunks_same'], 5)
        self.assertNotIn('chunks_changed', report['counts'])
        self.assertNotIn('CHUNK_DATA', report['bytes_sent'])
        self.assertEqual(report['counts']['round_trips'], 5)
        self.assertEqual(report['bytes_source_to_dest']['total'], report['bytes_sent_total'])  # read by benchmarks/

    def test_one_changed_page_is_the_only_chunk_sent(self):
        data = bytearray(os.urandom(6 * CHUNK))
        write(self.b, 'base/1/16384', bytes(data))
        data[3 * CHUNK + 100] ^= 0xFF
        write(self.s, 'base/1/16384', bytes(data))
        code, report = self.refresh()
        self.assertEqual(code, 0)
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        self.assertEqual(report['counts']['chunks_changed'], 1)
        self.assertEqual(report['counts']['chunks_same'], 5)
        self.assertEqual(report['bytes_sent']['CHUNK_DATA'], protocol.HEADER.size + 12 + CHUNK)

    def test_new_files_grown_and_shrunk_files(self):
        write(self.s, 'new', os.urandom(2 * CHUNK + 7))
        base = os.urandom(3 * CHUNK)
        write(self.b, 'grew', base)
        write(self.s, 'grew', base + os.urandom(CHUNK + 1))
        write(self.b, 'shrank', base)
        write(self.s, 'shrank', base[:CHUNK + 5])
        code, report = self.refresh()
        self.assertEqual(code, 0)
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        # Sent without asking: all 3 chunks of the new file, and the 2 chunks past B's end in 'grew'.
        # 'shrank' is compared chunk by chunk: B holds every range of it.
        self.assertEqual(report['counts']['chunks_new'], 3 + 2)

    def test_files_only_in_b_are_not_in_o(self):
        self.both('kept', b'x' * 100)
        write(self.b, 'gone', b'old')
        self.assertEqual(self.refresh()[0], 0)
        self.assertFalse((self.o / 'gone').exists())
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))

    def test_empty_files_dirs_modes_and_links(self):
        write(self.s, 'global/pg_control', os.urandom(8192), mode=0o600)
        write(self.s, 'empty', b'', mode=0o644)
        (self.s / 'pg_tblspc').mkdir(mode=0o700)
        (self.s / 'pg_tblspc' / 'empty_dir').mkdir(mode=0o750)
        os.symlink('/somewhere/else', self.s / 'pg_tblspc' / '16400')
        code, _ = self.refresh()
        self.assertEqual(code, 0)
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        self.assertEqual(stat.S_IMODE((self.o / 'pg_tblspc' / 'empty_dir').stat().st_mode), 0o750)

    def test_the_top_directory_keeps_the_sources_mode(self):
        """PostgreSQL refuses to start a data directory wider than 0750."""
        self.s.chmod(0o700)
        self.both('PG_VERSION', b'17\n')
        self.assertEqual(self.refresh()[0], 0)
        self.assertEqual(stat.S_IMODE(self.o.stat().st_mode), 0o700)

    def test_larger_chunks(self):
        data = bytearray(os.urandom(10 * CHUNK))
        write(self.b, 'f', bytes(data))
        data[0] ^= 1
        write(self.s, 'f', bytes(data))
        code, report = self.refresh('--chunk-size', str(4 * CHUNK))
        self.assertEqual(code, 0)
        self.assertEqual(tree_bytes(self.o), tree_bytes(self.s))
        self.assertEqual(report['counts']['chunks_changed'], 1)
        self.assertEqual(report['chunk_size'], 4 * CHUNK)

    def test_a_wrong_write_is_caught_and_no_output_appears(self):
        """Negative control: a receiver that corrupts the pages it writes must fail verification."""
        data = bytearray(os.urandom(4 * CHUNK))
        write(self.b, 'f', bytes(data))
        data[CHUNK] ^= 1
        write(self.s, 'f', bytes(data))
        real = os.pwrite

        def corrupt(fd, chunk, offset):
            return real(fd, bytes([chunk[0] ^ 0x55]) + chunk[1:], offset)

        with mock.patch.object(receiver_module.os, 'pwrite', corrupt):
            code, _ = self.refresh()
        self.assertEqual(code, 1)
        self.assertFalse(self.o.exists())
        self.assertFalse(any(p.name.endswith('.pgdelta-partial') for p in self.o.parent.iterdir()))

    def test_an_existing_output_is_refused_and_left_alone(self):
        self.both('f', b'abc')
        self.o.mkdir()
        (self.o / 'keep').write_text('mine')
        self.assertEqual(self.refresh()[0], 1)
        self.assertEqual((self.o / 'keep').read_text(), 'mine')


class OverTcp(unittest.TestCase):
    def test_send_and_receive_on_localhost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            s, b, o = root / 'S', root / 'B', root / 'O'
            data = bytearray(os.urandom(8 * CHUNK))
            write(b, 'f', bytes(data))
            data[5 * CHUNK] ^= 1
            write(s, 'f', bytes(data))
            server = socket.create_server(('127.0.0.1', 0))
            port = server.getsockname()[1]
            result = {}

            def receive():
                conn, _ = server.accept()
                with conn:
                    result['receiver'] = run_receiver(Channel(protocol.tune(conn)), b, o)

            thread = threading.Thread(target=receive)
            thread.start()
            with socket.create_connection(('127.0.0.1', port)) as sock:
                report = run_sender(Channel(protocol.tune(sock)), s, CHUNK)
            thread.join()
            server.close()
            self.assertTrue(report['ok'])
            self.assertEqual(report['counts']['chunks_changed'], 1)
            self.assertEqual((o / 'f').read_bytes(), bytes(data))
            self.assertEqual(result['receiver']['counts']['chunks_received'], 1)


class Paths(unittest.TestCase):
    def test_unsafe_paths_are_refused(self):
        for path in ('../x', '/etc/passwd', 'a/../../b', 'a//b', ''):
            with self.assertRaises(ValueError, msg=path):
                Entry.from_json({'path': path, 'kind': 'file', 'mode': 0o600, 'size': 0})

    def test_nothing_is_written_through_a_link(self):
        entries = [Entry('pg_wal', 'link', 0o777, target='/etc'), Entry('pg_wal/passwd', 'file', 0o600, size=1)]
        with self.assertRaises(protocol.ProtocolError):
            receiver_module._check_parents(entries)


class Framing(unittest.TestCase):
    def test_frames_round_trip_and_are_counted(self):
        a, b = socket.socketpair()
        with a, b:
            left, right = Channel(a), Channel(b)
            left.send(protocol.VERDICT, b'\x01')
            left.send_json(protocol.HELLO, {'k': 1})
            self.assertEqual(right.recv(), (protocol.VERDICT, b'\x01'))
            self.assertEqual(json.loads(right.expect(protocol.HELLO)), {'k': 1})
            self.assertEqual(left.sent['VERDICT'], protocol.HEADER.size + 1)
            self.assertEqual(right.received['VERDICT'], protocol.HEADER.size + 1)

    def test_an_error_frame_raises(self):
        a, b = socket.socketpair()
        with a, b:
            Channel(a).send_json(protocol.ERROR, {'message': 'boom'})
            with self.assertRaisesRegex(protocol.PeerError, 'boom'):
                Channel(b).expect(protocol.ACCEPT)


if __name__ == '__main__':
    unittest.main()
