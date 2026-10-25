"""Byte-stream transports: a local child process over pipes, the receiver's own stdio, and TCP.

The session code only sees a protocol.Channel, so an SSH transport can be
added later by running `python -m pgdelta receive --stdio` through ssh and
wrapping the ssh process's pipes exactly as spawn_local_receiver does.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import socket
import subprocess
import sys
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
                         in_place=False, fsync=None, verify_scope=None):
    """Start `receive --stdio` as a child process and return (process, channel).

    local_source is S: the child then also refuses to build O inside it.
    jobs, hash_backend, rebuild, verify, fsync and verify_scope, when given, are
    passed on as the child's --jobs, --hash-backend, --rebuild, --verify, --fsync
    and --verify-scope.
    in_place (output None) passes --in-place instead of --output.
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


def socket_channel(sock):
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    reader, writer = sock.makefile('rb'), sock.makefile('wb')

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
    return Channel(reader, writer, closer=close, sock=sock, aborter=lambda: sock.shutdown(socket.SHUT_RDWR))


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


def accept_channel(listener):
    try:
        sock, _ = listener.accept()
    except OSError as exc:
        raise TransferError(f'accept failed: {exc}') from exc
    finally:
        listener.close()
    return socket_channel(sock)


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


def connect_channel(host, port, timeout=30.0, congestion_control=None):
    """Connect to a receiver; the channel's connect_seconds is the connect's own time (EXP-13's connect_s), and
    with congestion_control its congestion_control reports what the socket got (set_congestion_control)."""
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
    sock.settimeout(None)
    channel = socket_channel(sock)
    channel.connect_seconds = connected
    channel.congestion_control = chosen
    return channel
