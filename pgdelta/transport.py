"""Byte-stream transports: a local child process over pipes, the receiver's own stdio, and TCP.

The session code only sees a protocol.Channel, so an SSH transport can be
added later by running `python -m pgdelta receive --stdio` through ssh and
wrapping the ssh process's pipes exactly as spawn_local_receiver does.

TCP may run inside mutual TLS 1.3 (O6; --tls-cert, --tls-key, --tls-ca on send and receive): each end presents a
certificate the other end's CA signed, so only an authorized sender can write into B, and the pages and the
session's hash key (in HELLO) never cross the network in clear. A session reads on one thread while another writes,
which OpenSSL forbids on one SSL object, so the TLS state is an ssl.SSLObject over memory BIOs, used only under a
lock, with the socket's own reads and writes outside it (_TlsConnection).
"""
from __future__ import annotations

import io
import ipaddress
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import threading
import time

from pgdelta.errors import InvalidInput, TransferError
from pgdelta.protocol import Channel

LINGER_SECONDS = 2.0
CHILD_EXIT_SECONDS = 30.0
_PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)
_CHILD_BOOTSTRAP = ('import sys; sys.path.insert(0, sys.argv.pop(1)); '
                    'from pgdelta.cli import main; sys.exit(main(sys.argv[1:]))')


def pipe_channel(read_fd, write_fd):
    return Channel(os.fdopen(read_fd, 'rb'), os.fdopen(write_fd, 'wb'))


def stdio_channel():
    """Take over fds 0 and 1 for the protocol; stray prints then land on stderr."""
    read_fd, write_fd = os.dup(0), os.dup(1)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    os.dup2(2, 1)
    return pipe_channel(read_fd, write_fd)


def spawn_local_receiver(basis, output, *, pg_controldata=None, reuse_any_basis=False,
                         local_source=None, jobs=None, hash_backend=None, rebuild=None, verify=None,
                         in_place=False, fsync=None, verify_scope=None, crashed_standby=None):
    """Start `receive --stdio` as a child process and return (process, channel).

    local_source is S: the child then also refuses to build O inside it.
    jobs, hash_backend, rebuild, verify, fsync and verify_scope, when given, are
    passed on as the child's --jobs, --hash-backend, --rebuild, --verify, --fsync
    and --verify-scope.
    in_place (output None) passes --in-place instead of --output; crashed_standby True or False passes
    --crashed-standby or --no-crashed-standby, and None leaves the child's default.
    """
    command = [sys.executable, '-c', _CHILD_BOOTSTRAP, _PACKAGE_PARENT, 'receive', '--stdio',
               '--basis', str(Path(basis).absolute())]
    command += ['--in-place'] if in_place else ['--output', str(Path(output).absolute())]
    if local_source is not None:
        command += ['--local-source', str(Path(local_source).absolute())]
    if pg_controldata:  # the binary for B: the child receiver only reads B
        command += ['--basis-pg-controldata', str(Path(pg_controldata).absolute())
                    if os.sep in str(pg_controldata) else str(pg_controldata)]
    if reuse_any_basis:
        command.append('--reuse-any-basis')
    if crashed_standby is not None:
        command.append('--crashed-standby' if crashed_standby else '--no-crashed-standby')
    for flag, value in (('--jobs', jobs), ('--hash-backend', hash_backend), ('--rebuild', rebuild),
                        ('--verify', verify), ('--fsync', fsync), ('--verify-scope', verify_scope)):
        if value is not None:
            command += [flag, str(value)]
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    except OSError as exc:
        raise TransferError(f'cannot start the local receiver: {exc}') from exc

    def close(reader, writer):
        try:
            writer.close()
        except OSError:
            pass
        try:
            process.wait(timeout=CHILD_EXIT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        reader.close()

    # abort(): a receiver that stopped reading is killed, so a send blocked on its pipe fails at once.
    return process, Channel(process.stdout, process.stdin, closer=close, aborter=process.kill)


def parse_hostport(text):
    host, separator, port = text.rpartition(':')
    if not separator or not host or not port.isdigit() or int(port) > 65535:
        raise InvalidInput(f'expected HOST:PORT, got {text!r}')
    if host.startswith('[') and host.endswith(']'):
        host = host[1:-1]
    return host, int(port)


def is_loopback(host):
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def tls_context(cert, key, ca, *, server):
    """Mutual TLS 1.3 (O6): this end presents `cert` with `key` and requires the peer's certificate signed by `ca`.
    A private CA names the parties, so the client checks the certificate, not the host name."""
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        if not server:
            context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_cert_chain(cert, key)
        context.load_verify_locations(cafile=ca)
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise InvalidInput(f'cannot load the TLS certificate, key or CA: {exc}') from exc
    return context


_RECV_BYTES = 1 << 16


class _TlsConnection:
    """One TLS connection over a TCP socket that one thread may read while another writes.

    OpenSSL forbids using one SSL object from two threads at once, and ssl.SSLSocket leaves that to its caller:
    a session's reading thread and sending thread, each in SSL_read and SSL_write on one SSLSocket, crashed the
    process (SIGBUS in OpenSSL's BIO code) within 30 rounds of a full-duplex stress, and hung CI's TLS session test
    (October 28). Here the TLS state is an ssl.SSLObject over two memory BIOs, touched only under `_state`; the
    socket's recv and sendall run outside it. `_sending` keeps the TLS records in the order they were made: whoever
    holds it sends everything pending. A reader whose read made records (a TLS 1.3 key update's answer) sends them
    only if `_sending` is free, and otherwise leaves them to its holder, which looks again after letting go, so a
    reader never waits on a writer blocked by the peer.
    """

    def __init__(self, sock, tls, incoming, outgoing):
        self.sock, self._tls, self._incoming, self._outgoing = sock, tls, incoming, outgoing
        self._state, self._sending = threading.Lock(), threading.Lock()

    def settimeout(self, seconds):
        self.sock.settimeout(seconds)

    def readinto(self, buffer):
        if not len(buffer):
            return 0
        while True:
            with self._state:
                try:
                    count = self._tls.read(len(buffer), buffer)
                except ssl.SSLWantReadError:
                    count = None
                except (ssl.SSLZeroReturnError, ssl.SSLEOFError):
                    count = 0  # the peer closed, with or without close_notify, as SSLSocket's ragged EOFs read
                made = self._outgoing.pending
            if made and self._sending.acquire(blocking=False):
                self._send_pending()
            if count is not None:
                return count
            data = self.sock.recv(_RECV_BYTES)
            with self._state:
                if data:
                    self._incoming.write(data)
                else:
                    self._incoming.write_eof()

    def write(self, data):
        self._sending.acquire()
        try:
            with self._state:
                written = self._tls.write(data)
        except BaseException:
            self._sending.release()  # a refused peer's alert, say: the close's flush comes back here
            raise
        self._send_pending()
        return written

    def _send_pending(self):
        """Send every record pending, holding `_sending`, then let it go and look again (see the class)."""
        try:
            while True:
                with self._state:
                    records = self._outgoing.read()
                if not records:
                    break
                self.sock.sendall(records)
        finally:
            self._sending.release()
        with self._state:
            made = self._outgoing.pending
        if made and self._sending.acquire(blocking=False):
            self._send_pending()


class _TlsStream(io.RawIOBase):
    """One direction of a _TlsConnection as a raw stream, for io.BufferedReader or io.BufferedWriter. Closing it
    leaves the connection and its socket open, as closing one of a socket's makefile() streams does."""

    def __init__(self, connection, mode):
        super().__init__()
        self._connection, self._mode = connection, mode

    def readable(self):
        return self._mode == 'r'

    def writable(self):
        return self._mode == 'w'

    def readinto(self, buffer):
        return self._connection.readinto(buffer)

    def write(self, data):
        return self._connection.write(data)


def _handshake(sock, tls, server_side):
    """The TLS handshake on `sock` through memory BIOs: the _TlsConnection, and the SSLObject for what was agreed.
    A refused handshake's alert is sent before the error is raised, as SSLSocket sends it."""
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    secure = tls.wrap_bio(incoming, outgoing, server_side=server_side)
    while True:
        try:
            secure.do_handshake()
            done = True
        except ssl.SSLWantReadError:
            done = False
        except ssl.SSLError:
            try:
                sock.sendall(outgoing.read())
            except OSError:
                pass
            raise
        records = outgoing.read()
        if records:
            sock.sendall(records)
        if done:
            return _TlsConnection(sock, secure, incoming, outgoing), secure
        data = sock.recv(_RECV_BYTES)
        if not data:
            raise ssl.SSLEOFError('the peer closed the connection during the TLS handshake')
        incoming.write(data)


def _secure(sock, tls, server_side):
    """`sock` with TLS after the handshake, as (a _TlsConnection, what was negotiated); (sock, None) without `tls`."""
    if tls is None:
        return sock, None
    try:
        connection, secure = _handshake(sock, tls, server_side)
    except (OSError, ssl.SSLError) as exc:
        sock.close()
        raise TransferError(f'TLS handshake failed: {exc}') from exc
    peer = dict(item for part in (secure.getpeercert() or {}).get('subject', ()) for item in part)
    return connection, {'version': secure.version(), 'cipher': secure.cipher()[0], 'peer': peer.get('commonName')}


def socket_channel(sock, tls=None):
    """A Channel over a connected TCP socket, or over a _TlsConnection on one (what _secure returned); `tls`, what
    _secure negotiated, is kept as channel.tls."""
    if isinstance(sock, _TlsConnection):
        connection, sock = sock, sock.sock
        reader = io.BufferedReader(_TlsStream(connection, 'r'))
        writer = io.BufferedWriter(_TlsStream(connection, 'w'))
    else:
        reader, writer = sock.makefile('rb'), sock.makefile('wb')
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def close(reader, writer):
        # Half-close, then drain until the peer closes too, so a final ERROR
        # frame is not destroyed by a reset caused by unread data.
        for action in (writer.close, lambda: sock.shutdown(socket.SHUT_WR)):
            try:
                action()
            except OSError:
                pass
        deadline = time.monotonic() + LINGER_SECONDS
        try:
            while time.monotonic() < deadline:
                sock.settimeout(max(0.01, deadline - time.monotonic()))
                if not sock.recv(1 << 16):
                    break
        except OSError:
            pass
        for action in (lambda: sock.shutdown(socket.SHUT_RDWR), reader.close, sock.close):
            try:
                action()
            except OSError:
                pass

    # abort(): a shut-down socket fails a send or receive blocked on it at once, on Linux and macOS alike.
    channel = Channel(reader, writer, closer=close, sock=sock, aborter=lambda: sock.shutdown(socket.SHUT_RDWR))
    channel.tls = tls
    return channel


def keep_alive(sock, seconds):
    """TCP keepalives on `sock`, so that a peer gone silent without a reset (a host down, a network partition)
    ends the connection within about `seconds` even while nothing is sent; on Linux also TCP_USER_TIMEOUT, so
    data left unacknowledged for `seconds` ends it too. The first probe goes after half of `seconds`, then 4
    probes at an eighth of it each. Returns what was set, {option name: value}, with "errors" for what the
    system refused; options the system lacks (TCP_USER_TIMEOUT off Linux) are left out."""
    seconds = max(8, int(seconds))
    wanted = [('SO_KEEPALIVE', socket.SOL_SOCKET, 1),
              # Linux names the idle time TCP_KEEPIDLE; macOS, TCP_KEEPALIVE.
              ('TCP_KEEPIDLE' if hasattr(socket, 'TCP_KEEPIDLE') else 'TCP_KEEPALIVE', socket.IPPROTO_TCP,
               seconds // 2),
              ('TCP_KEEPINTVL', socket.IPPROTO_TCP, seconds // 8),
              ('TCP_KEEPCNT', socket.IPPROTO_TCP, 4),
              ('TCP_USER_TIMEOUT', socket.IPPROTO_TCP, seconds * 1000)]
    out, errors = {}, {}
    for option, level, value in wanted:
        number = getattr(socket, option, None)
        if number is None:
            continue
        try:
            sock.setsockopt(level, number, value)
            out[option] = value
        except OSError as exc:
            errors[option] = str(exc)
    if errors:
        out['errors'] = errors
    return out


def listen(host, port):
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    try:
        listener = socket.socket(family, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(1)
    except OSError as exc:
        raise TransferError(f'cannot listen on {host}:{port}: {exc}') from exc
    return listener


def accept_channel(listener, tls=None):
    """The one connection the receiver takes, inside TLS when `tls` (a tls_context with server=True) is given."""
    try:
        sock, _ = listener.accept()
    except OSError as exc:
        raise TransferError(f'accept failed: {exc}') from exc
    finally:
        listener.close()
    sock, negotiated = _secure(sock, tls, server_side=True)
    return socket_channel(sock, negotiated)


def set_congestion_control(sock, name):
    """Ask for TCP congestion control `name` on this socket (Linux's TCP_CONGESTION) before it connects.

    Returns {'requested', 'effective', 'error'}. An unprivileged process may choose only what the host's
    net.ipv4.tcp_allowed_congestion_control lists (Debian's default: reno and cubic); otherwise, and on systems
    without TCP_CONGESTION (macOS), the host's default stays and 'error' says why (EXP-21, EXP-23)."""
    out = {'requested': name, 'effective': None, 'error': None}
    option = getattr(socket, 'TCP_CONGESTION', None)
    if option is None:
        out['error'] = 'TCP_CONGESTION not available on this system'
        return out
    try:
        sock.setsockopt(socket.IPPROTO_TCP, option, name.encode())
    except OSError as exc:
        out['error'] = str(exc)
    try:
        out['effective'] = sock.getsockopt(socket.IPPROTO_TCP, option, 16).split(b'\0', 1)[0].decode()
    except OSError:
        pass
    return out


def connect_channel(host, port, timeout=30.0, congestion_control=None, tls=None):
    """Connect to a receiver; the channel's connect_seconds is the connect's own time (EXP-13's connect_s), and
    with congestion_control its congestion_control reports what the socket got (set_congestion_control). With
    `tls` (a tls_context with server=False) the connection is wrapped in TLS, its handshake timed apart as
    channel.tls['handshake_seconds']."""
    started = time.perf_counter()
    chosen = None
    try:
        if congestion_control is None:
            sock = socket.create_connection((host, port), timeout=timeout)
        else:
            family, kind, proto, _, address = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
            sock = socket.socket(family, kind, proto)
            chosen = set_congestion_control(sock, congestion_control)
            sock.settimeout(timeout)
            sock.connect(address)
    except OSError as exc:
        raise TransferError(f'cannot connect to {host}:{port}: {exc}') from exc
    connected = time.perf_counter() - started
    shaken = time.perf_counter()
    sock, negotiated = _secure(sock, tls, server_side=False)
    if negotiated is not None:
        negotiated['handshake_seconds'] = round(time.perf_counter() - shaken, 6)
    sock.settimeout(None)
    channel = socket_channel(sock, negotiated)
    channel.connect_seconds = connected
    channel.congestion_control = chosen
    return channel
