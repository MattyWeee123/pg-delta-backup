"""TLS on the TCP transport (O6; pgdelta.transport): mutual TLS 1.3 with a private CA, opt-in on both ends.

A throwaway CA, a receiver and a sender certificate, and a rogue CA's sender certificate are made with the openssl
command (the tests skip without it). A session over TLS builds the same copy as one in clear. A relay between
the two ends records the stream: a marker from S's changed page crosses in clear without TLS, and never with it.
A sender whose certificate another CA signed, and a sender that speaks no TLS, are refused before anything is
written. Both ends may read and write at once, as a session's threads do (OpenSSL forbids that on one SSL object,
which crashed or hung TLS sessions until the transport kept its TLS state under a lock; October 28).
"""
import random
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from pgdelta import transport
from pgdelta.errors import InvalidInput, PgDeltaError
from pgdelta.receiver import run_receiver
from pgdelta.sender import run_sender

from pgdelta_support import PAGE, REPO_ROOT, Outcome, python_command, snapshot, subprocess_env, write_tree

MARKER = b'pgdelta-tls-marker-7f3a9c41e2b85d06'
LEAF_EXTENSIONS = ('basicConstraints=CA:FALSE\nkeyUsage=digitalSignature\n'
                   'extendedKeyUsage=serverAuth,clientAuth\nsubjectKeyIdentifier=hash\n'
                   'authorityKeyIdentifier=keyid\n')


def openssl(*args, cwd):
    subprocess.run(['openssl', *args], cwd=cwd, check=True, capture_output=True)


def make_ca(directory, name):
    openssl('req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes',
            '-keyout', f'{name}.key', '-out', f'{name}.crt', '-days', '2', '-subj', f'/CN={name}',
            '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign',
            cwd=directory)


def make_leaf(directory, name, ca):
    openssl('req', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes',
            '-keyout', f'{name}.key', '-out', f'{name}.csr', '-subj', f'/CN={name}', cwd=directory)
    openssl('x509', '-req', '-in', f'{name}.csr', '-CA', f'{ca}.crt', '-CAkey', f'{ca}.key', '-CAcreateserial',
            '-out', f'{name}.crt', '-days', '2', '-extfile', 'leaf.ext', cwd=directory)


class Relay:
    """Forwards one connection to `port`, recording what the client sends."""

    def __init__(self, port):
        self.listener = socket.create_server(('127.0.0.1', 0))
        self.port, self.target, self.seen = self.listener.getsockname()[1], port, bytearray()
        self.thread = threading.Thread(target=self._run, name='test-relay', daemon=True)
        self.thread.start()

    def _run(self):
        client, _ = self.listener.accept()
        self.listener.close()
        server = socket.create_connection(('127.0.0.1', self.target))
        pumps = [threading.Thread(target=self._pump, args=(client, server, True), daemon=True),
                 threading.Thread(target=self._pump, args=(server, client, False), daemon=True)]
        for pump in pumps:
            pump.start()
        for pump in pumps:
            pump.join(60)
        for sock in (client, server):
            sock.close()

    def _pump(self, source, sink, record):
        try:
            while True:
                data = source.recv(1 << 16)
                if not data:
                    break
                if record:
                    self.seen += data
                sink.sendall(data)
        except OSError:
            pass
        try:
            sink.shutdown(socket.SHUT_WR)
        except OSError:
            pass


@unittest.skipUnless(shutil.which('openssl'), 'the openssl command is not available')
class TlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certs_dir = tempfile.TemporaryDirectory()
        certs = cls.certs = Path(cls.certs_dir.name)
        (certs / 'leaf.ext').write_text(LEAF_EXTENSIONS)
        make_ca(certs, 'ca')
        make_ca(certs, 'rogue-ca')
        make_leaf(certs, 'receiver', 'ca')
        make_leaf(certs, 'sender', 'ca')
        make_leaf(certs, 'rogue-sender', 'rogue-ca')

    @classmethod
    def tearDownClass(cls):
        cls.certs_dir.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source, self.basis = root / 'S', root / 'B'
        rng = random.Random(58)
        old = rng.randbytes(12 * PAGE)
        new = bytearray(old)
        new[5 * PAGE + 100:5 * PAGE + 100 + len(MARKER)] = MARKER  # one changed page, carrying the marker
        write_tree(self.source, {'base/1/1': bytes(new), 'base/1/2': rng.randbytes(2 * PAGE)})
        write_tree(self.basis, {'base/1/1': old})

    def context(self, name, ca='ca', server=False):
        return transport.tls_context(self.certs / f'{name}.crt', self.certs / f'{name}.key',
                                     self.certs / f'{ca}.crt', server=server)

    def session(self, output, server_tls, client_tls, relay=False):
        """One session over TCP on 127.0.0.1 (through a recording relay if asked), and what each end saw."""
        listener = transport.listen('127.0.0.1', 0)
        outcome = Outcome()
        outcome.receiver_tls = outcome.sender_tls = None

        def serve():
            try:
                channel = transport.accept_channel(listener, tls=server_tls)
            except BaseException as exc:
                outcome.receiver_error = exc
                return
            outcome.receiver_tls = channel.tls
            try:
                outcome.receiver = run_receiver(channel, self.basis, output, reuse_any_basis=True)
            except BaseException as exc:
                outcome.receiver_error = exc
            finally:
                channel.close()

        thread = threading.Thread(target=serve, name='test-receiver')
        thread.start()
        port = listener.getsockname()[1]
        relayed = Relay(port) if relay else None
        try:
            channel = transport.connect_channel('127.0.0.1', relayed.port if relayed else port, tls=client_tls)
            outcome.sender_tls = channel.tls
            try:
                outcome.sender = run_sender(channel, self.source, node_blocks=4)
            finally:
                channel.close()
        except BaseException as exc:
            outcome.sender_error = exc
        thread.join(60)
        self.assertFalse(thread.is_alive(), 'the receiver did not finish')
        if relayed:
            relayed.thread.join(10)
            outcome.seen = bytes(relayed.seen)
        return outcome

    def test_a_session_over_tls_builds_the_same_copy(self):
        output = Path(self.temp.name) / 'O'
        outcome = self.session(output, self.context('receiver', server=True), self.context('sender'))
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assertIsNone(outcome.receiver_error, repr(outcome.receiver_error))
        self.assertEqual(snapshot(output), snapshot(self.source))
        self.assertEqual((outcome.sender_tls['version'], outcome.receiver_tls['version']), ('TLSv1.3', 'TLSv1.3'))
        self.assertEqual((outcome.sender_tls['peer'], outcome.receiver_tls['peer']), ('receiver', 'sender'))
        self.assertGreaterEqual(outcome.sender_tls['handshake_seconds'], 0)

    def test_nothing_crosses_in_clear_with_tls(self):
        # The positive control first: without TLS the marker in S's changed page is on the wire.
        plain = self.session(Path(self.temp.name) / 'O-plain', None, None, relay=True)
        self.assertIsNone(plain.sender_error, repr(plain.sender_error))
        self.assertIn(MARKER, plain.seen)
        self.assertIn(b'base/1/1', plain.seen)  # HELLO's inventory, the hash key beside it
        secure = self.session(Path(self.temp.name) / 'O-tls', self.context('receiver', server=True),
                              self.context('sender'), relay=True)
        self.assertIsNone(secure.sender_error, repr(secure.sender_error))
        self.assertEqual(snapshot(Path(self.temp.name) / 'O-tls'), snapshot(self.source))
        self.assertGreater(len(secure.seen), len(MARKER))
        self.assertNotIn(MARKER, secure.seen)
        self.assertNotIn(b'base/1/1', secure.seen)

    def test_a_sender_another_ca_signed_is_refused(self):
        output = Path(self.temp.name) / 'O'
        outcome = self.session(output, self.context('receiver', server=True),
                               self.context('rogue-sender', ca='ca'))
        self.assertIsInstance(outcome.receiver_error, PgDeltaError)
        self.assertIn('TLS handshake failed', str(outcome.receiver_error))
        self.assertIsNotNone(outcome.sender_error)
        self.assertFalse(output.exists())

    def test_a_receiver_another_ca_signed_is_refused(self):
        output = Path(self.temp.name) / 'O'
        outcome = self.session(output, self.context('receiver', server=True), self.context('sender', ca='rogue-ca'))
        self.assertIsInstance(outcome.sender_error, PgDeltaError)
        self.assertIn('TLS handshake failed', str(outcome.sender_error))
        self.assertIsNotNone(outcome.receiver_error)
        self.assertFalse(output.exists())

    def test_a_sender_without_tls_is_refused(self):
        output = Path(self.temp.name) / 'O'
        outcome = self.session(output, self.context('receiver', server=True), None)
        self.assertIsInstance(outcome.receiver_error, PgDeltaError)
        self.assertIn('TLS handshake failed', str(outcome.receiver_error))
        self.assertIsNotNone(outcome.sender_error)
        self.assertFalse(output.exists())

    def test_both_ends_read_and_write_at_once(self):
        """16 MiB each way, each end writing on one thread while another reads, every byte checked. Through one
        ssl.SSLSocket this crashed the process (SIGBUS in OpenSSL) or hung, though rarely at this size: a stress of
        64 MiB rounds failed within 30 (experiments/tls_duplex_stress.py)."""
        listener = transport.listen('127.0.0.1', 0)
        chunk, chunks = 1 << 16, 256
        stream = {end: [random.Random(f'{end}{i}').randbytes(chunk) for i in range(4)] for end in 'ab'}
        channels, errors, received = {}, [], {}

        def pump(end, channel):
            def write():
                try:
                    for i in range(chunks):
                        channel._writer.write(stream[end][i % 4])
                    channel._writer.flush()
                except BaseException as exc:
                    errors.append((end, 'write', exc))
            writer = threading.Thread(target=write)
            writer.start()
            peer, count = 'b' if end == 'a' else 'a', 0
            try:
                for i in range(chunks):
                    if bytes(channel._read_exact(chunk)) == stream[peer][i % 4]:
                        count += 1
            except BaseException as exc:
                errors.append((end, 'read', exc))
            writer.join(60)
            received[end] = count

        def serve():
            channels['b'] = transport.accept_channel(listener, tls=self.context('receiver', server=True))
            pump('b', channels['b'])

        server = threading.Thread(target=serve)
        server.start()
        channels['a'] = transport.connect_channel('127.0.0.1', listener.getsockname()[1], tls=self.context('sender'))
        client = threading.Thread(target=pump, args=('a', channels['a']))
        client.start()
        for thread in (client, server):
            thread.join(120)
        hung = [thread.name for thread in (client, server) if thread.is_alive()]
        for channel in channels.values():
            channel.abort() if hung else channel.close()
        self.assertEqual(hung, [], 'a TLS stream stopped moving')
        self.assertEqual(errors, [])
        self.assertEqual(received, {'a': chunks, 'b': chunks})

    def test_a_missing_certificate_is_invalid_input(self):
        with self.assertRaises(InvalidInput):
            transport.tls_context(self.certs / 'absent.crt', self.certs / 'sender.key', self.certs / 'ca.crt',
                                  server=False)


class TlsOptionTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(python_command(*args), capture_output=True, text=True, cwd=REPO_ROOT,
                              env=subprocess_env(), timeout=120)

    def test_the_three_options_go_together(self):
        with tempfile.TemporaryDirectory() as temp:
            write_tree(Path(temp) / 'S', {'f': b'x'})
            refused = self.run_cli('send', '--source', Path(temp) / 'S', '--connect', '127.0.0.1:9',
                                   '--tls-cert', Path(temp) / 'c.pem', '--tls-ca', Path(temp) / 'ca.pem')
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn('--tls-cert, --tls-key and --tls-ca go together', refused.stderr)

    def test_stdio_takes_no_tls(self):
        with tempfile.TemporaryDirectory() as temp:
            write_tree(Path(temp) / 'B', {'f': b'x'})
            refused = self.run_cli('receive', '--basis', Path(temp) / 'B', '--output', Path(temp) / 'O', '--stdio',
                                   '--tls-cert', 'c.pem', '--tls-key', 'k.pem', '--tls-ca', 'ca.pem')
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn('--tls-* is for --listen', refused.stderr)


if __name__ == '__main__':
    unittest.main()
