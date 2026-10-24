"""parcopy: EXP-11's bound, a range-parallel durable full copy of a cleanly stopped data directory.

Harness code, built to be hard to beat, never a product engine. It copies exactly what v1's path
reads (pgdelta.datadir.select(), called directly on the source) over N TCP streams:

    python -m experiments.network.parcopy receive --output O --listen IP:PORT --stats-json PATH \\
        --streams N --writeback-bytes 8388608
                   on dest: prints "parcopy: listening on IP:PORT", then writes O
    python -m experiments.network.parcopy send --source DATADIR --pg-controldata PATH \\
        --connect IP:PORT --stats-json PATH --streams N --chunk-bytes 67108864
                   on the source, from the repository

- The sender refuses a running cluster before anything is sent (select(): postmaster.pid, or
  pg_controldata's state not "shut down"). Each file is read from its listed source, so an unlogged
  relation's main fork gets its init fork's bytes. It doesn't check afterwards that the cluster
  stayed stopped: the harness stopped it and verifies the copy against it (verify_direct).
- plan() cuts every file into ranges of at most --chunk-bytes and gives them, largest first, each
  to the stream with the fewest bytes so far. By file, one stream would carry pgbench_accounts.
- One control connection carries the inventory (directories and files with their modes and sizes)
  and, at the end, the receiver's verdict; each of the N data connections carries its stream's
  ranges, a header then the bytes, sent with os.sendfile. No TLS, no checksums: the harness checks
  byte identity afterwards. The sender opens all N + 1 connections at once (connect_all), so at a
  round trip r they take about r, not (N + 1) r (EXP-13, October 24, 2026: a rule 8 change to
  EXP-11's bound, which opened them one after another; at 0.1 ms the two differ by about 1 ms).
- Before copying, the receiver recreates every directory of the inventory (the empty ones select()
  keeps among them) and creates every file empty, each with its recorded mode; the output's top
  directory gets the data directory's mode. It writes with os.pwrite and starts write-back every
  --writeback-bytes of each range with pgdelta.flush.start_writeback (EXP-10's fixed write-back).
- After the last range it checks that every file got all its bytes, then fsyncs every file in
  parallel (FSYNC_THREADS), then every directory bottom-up (each depth in parallel), the output's top
  directory last: the inventory's files plus its directories plus one. Not the output's parent: on
  ext4's ordered journal a later fsync on the filesystem covers that entry (EXP-11's directory rule).
  Each file is fsynced through a descriptor opened again, as pg_basebackup's own sync does.

Each end's --stats-json holds its timers, in seconds since that end started (after its imports):
per stream its bytes, first and last byte, and the time in pwrite (receiver) or sendfile (sender);
the receiver's fsync tail, its fsync calls and the count its inventory expects; the sender's
connect_start_s and connected_s (its connection set-up, between them).

Exit status: 0; 2 for refused input (a running cluster, a non-empty output); 3 for a failed transfer.
Standard library only, with the repository's pgdelta (standard library only too).
"""
from __future__ import annotations

import argparse
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
import heapq
import json
import os
from pathlib import Path
import platform
import socket
import stat
import struct
import sys
import threading
import time
import traceback

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pgdelta import datadir, flush  # noqa: E402
from pgdelta.errors import (EXIT_TRANSFER_FAILURE, InvalidInput, PeerError, PgDeltaError,  # noqa: E402
                            ProtocolError, TransferError)
from pgdelta.inventory import MODE_MASK, parse_entries  # noqa: E402

# measure.PARCOPY_CHUNK_BYTES and PARCOPY_WRITEBACK_BYTES, which run_exp04 passes (not imported here,
# to keep each end's start-up, inside the clock, short).
DEFAULT_CHUNK_BYTES = 64 << 20
DEFAULT_WRITEBACK_BYTES = flush.WRITEBACK_BYTES
MAGIC = b'PARCOPY1'
CONTROL, DATA = b'C', b'D'  # after MAGIC, what a connection carries
MESSAGE = struct.Struct('!I')  # a JSON message's length, before it
STREAM = struct.Struct('!I')  # a data connection's stream index, after MAGIC + DATA
RANGE = struct.Struct('!IQQ')  # a range's header: file index, offset, length
END = 0xFFFFFFFF  # the file index of a stream's end marker
MAX_MESSAGE = 256 << 20
PIECE_BYTES = 4 << 20  # the receiver's reads and writes, at most (and at most the write-back step)
FSYNC_THREADS = 16  # the receiver's parallel fsyncs after the last range
CONNECT_TIMEOUT = 30  # seconds
SOCKET_TIMEOUT = 600  # seconds: waiting for a connection, a message or bytes
O_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)
O_DIRECTORY = getattr(os, 'O_DIRECTORY', 0)

Range = namedtuple('Range', 'file offset length')  # file: its index in the inventory's files


def plan(selection, streams: int, chunk_bytes: int) -> list[list[Range]]:
    """The ranges each of `streams` streams sends, in order.

    Every file of the selection's inventory is cut into ranges of at most chunk_bytes; the ranges
    go largest first (then by path and offset) each to the stream with the fewest bytes so far, the
    lowest-numbered on a tie. An empty file has no range: the receiver creates every file first.
    """
    if streams < 1 or chunk_bytes < 1:
        raise ValueError('streams and chunk_bytes must be at least 1')
    files = selection.inventory.files
    ranges = [Range(index, offset, min(chunk_bytes, entry.size - offset))
              for index, entry in enumerate(files) for offset in range(0, entry.size, chunk_bytes)]
    ranges.sort(key=lambda item: (-item.length, files[item.file].path, item.offset))
    loads = [(0, stream) for stream in range(streams)]  # a heap of (bytes so far, stream)
    lists: list[list[Range]] = [[] for _ in range(streams)]
    for item in ranges:
        load, stream = heapq.heappop(loads)
        lists[stream].append(item)
        heapq.heappush(loads, (load + item.length, stream))
    return lists


def plan_summary(lists) -> dict:
    """Ranges and bytes per stream; balance is the busiest stream's bytes over the mean (EXP-11's A3)."""
    sizes = [sum(item.length for item in ranges) for ranges in lists]
    mean = sum(sizes) / len(sizes) if sizes else 0
    return {'streams': len(lists), 'ranges': sum(len(ranges) for ranges in lists), 'bytes': sum(sizes),
            'stream_bytes': sizes, 'stream_ranges': [len(ranges) for ranges in lists],
            'balance': round(max(sizes) / mean, 4) if mean else None}


def capabilities() -> dict:
    """What this host's Python offers: sendfile for the sender, early write-back for the receiver."""
    return {'sendfile': hasattr(os, 'sendfile'), 'pwrite': hasattr(os, 'pwrite'),
            'posix_fadvise': hasattr(os, 'posix_fadvise'), 'writeback': flush.writeback_available(),
            'platform': sys.platform, 'python': platform.python_version()}


# --- the wire -------------------------------------------------------------------------

def hostport(text: str) -> tuple[str, int]:
    host, _, port = str(text).rpartition(':')
    if not host or not port.isdigit():
        raise InvalidInput(f'want IP:PORT, got {text!r}')
    return host, int(port)


def receive_exact(sock, size: int) -> bytes:
    buffer = bytearray(size)
    view, got = memoryview(buffer), 0
    while got < size:
        count = sock.recv_into(view[got:])
        if not count:
            raise ProtocolError(f'the connection closed after {got} of {size} bytes')
        got += count
    return bytes(buffer)


def send_message(sock, value: dict) -> None:
    data = json.dumps(value, separators=(',', ':')).encode()
    sock.sendall(MESSAGE.pack(len(data)) + data)


def receive_message(sock) -> dict:
    size = MESSAGE.unpack(receive_exact(sock, MESSAGE.size))[0]
    if size > MAX_MESSAGE:
        raise ProtocolError(f'a message of {size} bytes is too long')
    value = json.loads(receive_exact(sock, size))
    if not isinstance(value, dict):
        raise ProtocolError('a message must be a JSON object')
    return value


def connect(host: str, port: int, kind: bytes, stream: int | None = None):
    """A connection to the receiver, introduced as the control connection or as data stream `stream`."""
    sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    try:
        sock.sendall(MAGIC + kind + (STREAM.pack(stream) if kind == DATA else b''))
    except BaseException:
        sock.close()
        raise
    return sock


def connect_all(host: str, port: int, streams: int):
    """The control connection and every data stream's, opened at once, one thread each: their handshakes take one
    round trip in all, where one after another they would take streams + 1 (at DR latency, 1 s and more of a copy
    that should pay 3-6). (control, data sockets in stream order), the data sockets blocking, for os.sendfile. If any
    fails, every one opened is closed and the first failure raised."""
    with ThreadPoolExecutor(max_workers=streams + 1, thread_name_prefix='parcopy-connect') as pool:
        futures = [pool.submit(connect, host, port, CONTROL)]
        futures += [pool.submit(connect, host, port, DATA, stream) for stream in range(streams)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except Exception as exc:
                outcomes.append(exc)
    failures = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    if failures:
        for outcome in outcomes:
            if not isinstance(outcome, Exception):
                outcome.close()
        raise failures[0]
    for sock in outcomes[1:]:
        sock.settimeout(None)  # blocking, for os.sendfile
    return outcomes[0], outcomes[1:]


def write_json(path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=1, sort_keys=True) + '\n')
    os.replace(temporary, path)


def problem(exc: BaseException) -> str:
    return f'{type(exc).__name__}: {exc}'


# --- the sender (the source) ----------------------------------------------------------------

class Sender:
    """Sends the stopped data directory's selection over `streams` connections; stats as it goes."""

    def __init__(self, source, pg_controldata, streams: int, chunk_bytes: int):
        self.source, self.pg_controldata = Path(source), pg_controldata
        self.streams, self.chunk_bytes = streams, chunk_bytes
        self.started = time.perf_counter()
        self.stats: dict = {'source': str(self.source), 'chunk_bytes': chunk_bytes, 'error': None,
                            'times': 'seconds since the sender started, after its imports'}

    def clock(self) -> float:
        return round(time.perf_counter() - self.started, 6)

    def run(self, address: str) -> dict:
        host, port = hostport(address)
        stats = self.stats
        try:
            selection = datadir.select(self.source, self.pg_controldata)
        except PgDeltaError as exc:
            self.abort(host, port, exc)
            raise
        stats['select_s'] = self.clock()
        inventory = selection.inventory
        lists = plan(selection, self.streams, self.chunk_bytes)
        stats.update(files=len(inventory.files), directories=len(inventory.directories),
                     bytes=sum(entry.size for entry in inventory.files), plan=plan_summary(lists),
                     excluded=selection.excluded_summary(), redo_wal_file=selection.redo_wal_file)
        paths = [self.source / selection.source_path(entry.path) for entry in inventory.files]
        stats['connect_start_s'] = self.clock()
        control, sockets = connect_all(host, port, self.streams)
        try:
            stats['connected_s'] = self.clock()
            send_message(control, {'version': 1, 'streams': self.streams, 'chunk_bytes': self.chunk_bytes,
                                   'root_mode': inventory.root_mode,
                                   'directories': [[entry.path, entry.mode] for entry in inventory.directories],
                                   'files': [[entry.path, entry.size, entry.mode] for entry in inventory.files],
                                   'plan': stats['plan']})
            results: list = [None] * self.streams
            errors: list = []

            def run_stream(stream):
                try:
                    results[stream] = self.send_stream(sockets[stream], lists[stream], paths)
                except BaseException as exc:
                    errors.append(f'stream {stream}: {problem(exc)}')
                    try:  # the receiver sees the stream end early and stops the others
                        sockets[stream].shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

            threads = [threading.Thread(target=run_stream, args=(stream,), name=f'parcopy-send-{stream}',
                                        daemon=True) for stream in range(self.streams)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            stats['streams'] = results
            stats['sent_s'] = self.clock()
            if errors:
                stats['receiver'] = self.verdict(control, CONNECT_TIMEOUT)  # its reason, if it gave one
                raise TransferError('; '.join(errors) + (f"; the receiver: {stats['receiver'].get('error')}"
                                                         if stats['receiver'] else ''))
            control.settimeout(SOCKET_TIMEOUT)
            verdict = receive_message(control)  # the receiver's, after its last fsync
            stats['receiver'] = verdict
            stats['verdict_s'] = self.clock()
        finally:
            for sock in [control, *sockets]:
                sock.close()
        if not verdict.get('ok'):
            raise TransferError(f"the receiver failed: {verdict.get('error')}")
        return stats

    def send_stream(self, sock, ranges, paths) -> dict:
        """One stream's ranges, each a header then its bytes (os.sendfile), then the end marker."""
        record = {'bytes': 0, 'ranges': 0, 'sendfile_s': 0.0, 'first_send_s': None, 'last_send_s': None}
        out = sock.fileno()
        for item in ranges:
            sock.sendall(RANGE.pack(item.file, item.offset, item.length))
            if record['first_send_s'] is None:
                record['first_send_s'] = self.clock()
            fd = os.open(paths[item.file], os.O_RDONLY | O_CLOEXEC)
            try:
                if hasattr(os, 'posix_fadvise'):
                    os.posix_fadvise(fd, item.offset, item.length, os.POSIX_FADV_SEQUENTIAL)
                offset, end = item.offset, item.offset + item.length
                while offset < end:
                    started = time.perf_counter()
                    count = os.sendfile(out, fd, offset, end - offset)
                    record['sendfile_s'] += time.perf_counter() - started
                    if not count:
                        raise TransferError(f'{paths[item.file]} ended at byte {offset}, before byte {end}: it '
                                            'changed while it was copied')
                    offset += count
            finally:
                os.close(fd)
            record['bytes'] += item.length
            record['ranges'] += 1
        sock.sendall(RANGE.pack(END, 0, 0))
        sock.shutdown(socket.SHUT_WR)
        record['last_send_s'] = self.clock()
        record['sendfile_s'] = round(record['sendfile_s'], 6)
        return record

    @staticmethod
    def verdict(control, timeout: float) -> dict | None:
        """The receiver's verdict after a stream failed, if it sends one in time."""
        try:
            control.settimeout(timeout)
            return receive_message(control)
        except (OSError, ValueError, PgDeltaError):
            return None

    @staticmethod
    def abort(host, port, exc: PgDeltaError) -> None:
        """Best effort: tell a waiting receiver why nothing will come, so it stops at once."""
        try:
            with connect(host, port, CONTROL) as sock:
                send_message(sock, {'error': str(exc), 'exit_code': exc.exit_code})
        except OSError:
            pass


# --- the receiver (dest) ---------------------------------------------------------------------

class Receiver:
    """Writes the sender's streams into a new output directory, then makes all of it durable."""

    def __init__(self, output, streams: int, writeback_bytes: int):
        self.output = Path(output)
        self.streams, self.writeback_bytes = streams, writeback_bytes
        self.piece_bytes = min(PIECE_BYTES, writeback_bytes) if writeback_bytes else PIECE_BYTES
        self.writeback = bool(writeback_bytes)  # until the kernel refuses it
        self.umask = os.umask(0)  # read and put back at once: a file needs a chmod only where it strips bits
        os.umask(self.umask)
        self.started = time.perf_counter()
        self.lock = threading.Lock()
        self.directories: list = []
        self.files: list = []
        self.received: dict[int, int] = {}  # file index -> bytes written
        self.modes_after: list = []  # files created writable whose own mode isn't, set after the copy
        self.control = None  # the control connection, once it said so
        self.sockets: list = []  # the data connections
        self.threads: list = []  # their threads
        self.errors: list[str] = []
        self.fsync_calls = 0
        self.stats: dict = {'output': str(self.output), 'stream_count': streams, 'writeback_bytes': writeback_bytes,
                            'piece_bytes': self.piece_bytes, 'fsync_threads': FSYNC_THREADS, 'error': None,
                            'times': 'seconds since the receiver started, after its imports',
                            'streams': [{'bytes': 0, 'ranges': 0, 'first_byte_s': None, 'last_byte_s': None,
                                         'write_s': 0.0, 'writeback_s': 0.0, 'writeback_calls': 0}
                                        for _ in range(streams)]}

    def clock(self) -> float:
        return round(time.perf_counter() - self.started, 6)

    def run(self, address: str, on_listening=None) -> dict:
        """Listen, receive, make durable; the stats. on_listening gets the bound (host, port)."""
        self.refuse_output()
        host, port = hostport(address)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((host, port))
                listener.listen(self.streams + 1)
                listener.settimeout(SOCKET_TIMEOUT)
                self.stats['listening_s'] = self.clock()
                if on_listening is not None:
                    on_listening(listener.getsockname()[:2])
                self.accept(listener)
            finally:
                listener.close()
            for thread in self.threads:
                thread.join()
            self.stats['streams_done_s'] = self.clock()
            if self.errors:
                raise TransferError('; '.join(self.errors[:4]))
            self.check_complete()
            self.make_durable()
            verdict = {'ok': True, 'files': len(self.files), 'directories': len(self.directories),
                       'bytes': sum(self.received.values()), 'fsync_calls': self.fsync_calls}
        except BaseException as exc:
            self.stats['error'] = problem(exc)
            self.stop_streams()
            for thread in self.threads:
                thread.join(SOCKET_TIMEOUT)
            verdict = {'ok': False, 'error': self.stats['error']}
            raise
        finally:
            if self.control is not None:  # the sender waits for this after its last byte
                try:
                    send_message(self.control, verdict)
                except OSError:
                    pass
                self.control.close()
            for sock in self.sockets:
                sock.close()
            for record in self.stats['streams']:
                record['write_s'], record['writeback_s'] = round(record['write_s'], 6), round(record['writeback_s'], 6)
            self.stats.update(fsync_calls=self.fsync_calls, writeback_effective=self.writeback,
                              writeback_available=flush.writeback_available(), wall_s=self.clock())
        return self.stats

    def refuse_output(self) -> None:
        """The output must be absent, or an empty directory, before anything listens."""
        if os.path.lexists(self.output):
            if not self.output.is_dir() or self.output.is_symlink() or any(self.output.iterdir()):
                raise InvalidInput(f'{self.output} exists and is not an empty directory')
        elif not self.output.parent.is_dir():
            raise InvalidInput(f"{self.output}'s parent directory does not exist")

    def accept(self, listener) -> None:
        """The control connection and every data stream; each stream's thread starts once the output is set up."""
        pending, seen = [], set()
        while self.control is None or len(seen) < self.streams:
            sock, _ = listener.accept()
            sock.settimeout(SOCKET_TIMEOUT)
            self.sockets.append(sock)
            hello = receive_exact(sock, len(MAGIC) + 1)
            if hello[:len(MAGIC)] != MAGIC:
                raise ProtocolError(f'not a parcopy connection: {hello!r}')
            kind = hello[len(MAGIC):]
            if kind == CONTROL and self.control is None:
                self.sockets.remove(sock)
                self.control = sock
                header = receive_message(sock)
                if 'error' in header:
                    raise PeerError(f"the sender gave up before the copy: {header['error']}",
                                    header.get('exit_code', EXIT_TRANSFER_FAILURE))
                self.stats['inventory_s'] = self.clock()
                self.setup(header)
                for index, data in pending:
                    self.start_stream(index, data)
                pending = []
            elif kind == DATA:
                index = STREAM.unpack(receive_exact(sock, STREAM.size))[0]
                if index >= self.streams or index in seen:
                    raise ProtocolError(f'unexpected data stream {index} (of {self.streams})')
                seen.add(index)
                if self.control is None:
                    pending.append((index, sock))
                else:
                    self.start_stream(index, sock)
            else:
                raise ProtocolError(f'unexpected connection kind {kind!r}')

    def setup(self, header: dict) -> None:
        """Every directory and every file of the inventory, before any byte is written.

        The directories are made owner-writable first, so the files can be created in them, and get
        their recorded modes once the files exist; the top directory gets the data directory's mode.
        """
        if header.get('streams') != self.streams:
            raise ProtocolError(f"the sender has {header.get('streams')} streams, this receiver {self.streams}")
        self.directories, self.files = parse_entries(header.get('directories'), header.get('files'))
        root_mode = header.get('root_mode')
        if type(root_mode) is not int or not 0 <= root_mode <= MODE_MASK:
            raise InvalidInput(f'invalid mode {root_mode!r} for the top directory')
        started = time.perf_counter()
        if not self.output.is_dir():
            self.output.mkdir(mode=0o700)
        for entry in sorted(self.directories, key=lambda entry: (entry.path.count('/'), entry.path)):
            (self.output / entry.path).mkdir(mode=0o700)
        for entry in self.files:
            mode = entry.mode | stat.S_IWUSR
            fd = os.open(self.output / entry.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_CLOEXEC, mode)
            try:
                if mode & self.umask:
                    os.fchmod(fd, mode)
            finally:
                os.close(fd)
            if mode != entry.mode:
                self.modes_after.append(entry)
        for entry in self.directories:
            os.chmod(self.output / entry.path, entry.mode)
        os.chmod(self.output, root_mode)
        self.stats.update(files=len(self.files), directories=len(self.directories), root_mode=root_mode,
                          bytes=sum(entry.size for entry in self.files), chunk_bytes=header.get('chunk_bytes'),
                          plan=header.get('plan'), setup_s=round(time.perf_counter() - started, 6),
                          setup_done_s=self.clock(), fsync_expected=len(self.files) + len(self.directories) + 1)

    def start_stream(self, index: int, sock) -> None:
        thread = threading.Thread(target=self.run_stream, args=(index, sock), name=f'parcopy-receive-{index}',
                                  daemon=True)
        self.threads.append(thread)
        thread.start()

    def run_stream(self, index: int, sock) -> None:
        try:
            self.receive_stream(index, sock)
        except BaseException as exc:
            with self.lock:
                self.errors.append(f'stream {index}: {problem(exc)}')
            self.stop_streams()

    def stop_streams(self) -> None:
        """After a failure: end every stream, so their threads and the sender stop."""
        for sock in list(self.sockets):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def receive_stream(self, index: int, sock) -> None:
        """One stream's ranges until its end marker: pwrite, with write-back every writeback_bytes of a range."""
        record = self.stats['streams'][index]
        buffer = memoryview(bytearray(self.piece_bytes))
        received: dict[int, int] = {}
        while True:
            number, offset, length = RANGE.unpack(receive_exact(sock, RANGE.size))
            if number == END:
                break
            if number >= len(self.files) or not length or offset + length > self.files[number].size:
                raise ProtocolError(f'range ({number}, {offset}, {length}) is outside the inventory')
            fd = os.open(self.output / self.files[number].path, os.O_WRONLY | O_CLOEXEC)
            try:
                position, end = offset, offset + length
                dirty_start, dirty = offset, 0
                while position < end:
                    piece = min(self.piece_bytes, end - position)
                    view, got = buffer[:piece], 0
                    while got < piece:
                        count = sock.recv_into(view[got:])
                        if not count:
                            raise ProtocolError(f'the stream ended inside range ({number}, {offset}, {length})')
                        if record['first_byte_s'] is None:
                            record['first_byte_s'] = self.clock()
                        got += count
                    record['last_byte_s'] = self.clock()
                    started = time.perf_counter()
                    written = 0
                    while written < piece:
                        count = os.pwrite(fd, view[written:], position + written)
                        if not count:
                            raise TransferError(f'cannot write {self.files[number].path} at byte {position + written}')
                        written += count
                    record['write_s'] += time.perf_counter() - started
                    position += piece
                    dirty += piece
                    if self.writeback and dirty >= self.writeback_bytes:
                        started = time.perf_counter()
                        if flush.start_writeback(fd, dirty_start, dirty):
                            record['writeback_calls'] += 1
                        else:  # not here (not Linux, or refused): the fsyncs flush it all
                            self.writeback = False
                        record['writeback_s'] += time.perf_counter() - started
                        dirty_start, dirty = position, 0
            finally:
                os.close(fd)
            received[number] = received.get(number, 0) + length
            record['bytes'] += length
            record['ranges'] += 1
        with self.lock:
            for number, size in received.items():
                self.received[number] = self.received.get(number, 0) + size

    def check_complete(self) -> None:
        """Every file got exactly its bytes: a dropped or repeated range fails the copy."""
        wrong = [(entry.path, self.received.get(number, 0), entry.size) for number, entry in enumerate(self.files)
                 if self.received.get(number, 0) != entry.size]
        if wrong:
            raise TransferError(f'{len(wrong)} files did not get their bytes: ' + ', '.join(
                f'{path} ({got} of {size} bytes)' for path, got, size in wrong[:5]))

    def fsync(self, path: Path, directory: bool = False) -> None:
        fd = os.open(path, os.O_RDONLY | O_CLOEXEC | (O_DIRECTORY if directory else 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        with self.lock:
            self.fsync_calls += 1

    def make_durable(self) -> None:
        """The fsync tail: every file in parallel, every directory bottom-up, the top directory last."""
        started = time.perf_counter()
        for entry in self.modes_after:
            os.chmod(self.output / entry.path, entry.mode)
        levels: dict[int, list] = {}
        for entry in self.directories:
            levels.setdefault(entry.path.count('/'), []).append(self.output / entry.path)
        with ThreadPoolExecutor(max_workers=FSYNC_THREADS) as pool:
            list(pool.map(self.fsync, [self.output / entry.path for entry in self.files]))
            files_done = time.perf_counter()
            for depth in sorted(levels, reverse=True):
                list(pool.map(lambda path: self.fsync(path, directory=True), levels[depth]))
        self.fsync(self.output, directory=True)
        ended = time.perf_counter()
        self.stats.update(fsync_s=round(ended - started, 6), file_fsync_s=round(files_done - started, 6),
                          directory_fsync_s=round(ended - files_done, 6))


# --- the command line ------------------------------------------------------------------------

def finish(stats, stats_path, exc: Exception | None) -> int:
    """Write the stats, whatever happened, and return the exit status; an error goes to stderr."""
    if stats_path is not None:
        write_json(stats_path, stats)
    if exc is None:
        return 0
    if not isinstance(exc, (PgDeltaError, OSError)):
        traceback.print_exception(exc)
    print(f'parcopy: error: {exc}', file=sys.stderr, flush=True)
    return exc.exit_code if isinstance(exc, PgDeltaError) else EXIT_TRANSFER_FAILURE


def cmd_receive(args) -> int:
    receiver = Receiver(args.output, args.streams, args.writeback_bytes)

    def listening(address):
        print(f'parcopy: listening on {address[0]}:{address[1]}', flush=True)

    try:
        receiver.run(args.listen, listening)
    except Exception as exc:
        receiver.stats['error'] = receiver.stats['error'] or problem(exc)
        return finish(receiver.stats, args.stats_json, exc)
    return finish(receiver.stats, args.stats_json, None)


def cmd_send(args) -> int:
    sender = Sender(args.source, args.pg_controldata, args.streams, args.chunk_bytes)
    try:
        sender.run(args.connect)
    except Exception as exc:
        sender.stats.update(error=problem(exc), wall_s=sender.clock())
        return finish(sender.stats, args.stats_json, exc)
    sender.stats['wall_s'] = sender.clock()
    return finish(sender.stats, args.stats_json, None)


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f'must be at least 1, got {text}')
    return value


def non_negative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f'must be 0 or more, got {text}')
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='python -m experiments.network.parcopy', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    sub = commands.add_parser('receive', help='on dest: listen, write the output, make it durable')
    sub.add_argument('--output', required=True, type=Path, help='the new copy: absent, or an empty directory')
    sub.add_argument('--listen', required=True, metavar='IP:PORT')
    sub.add_argument('--stats-json', type=Path, metavar='PATH')
    sub.add_argument('--streams', required=True, type=positive_int, help='data connections to accept')
    sub.add_argument('--writeback-bytes', type=non_negative_int, default=DEFAULT_WRITEBACK_BYTES,
                     help='start write-back every this many bytes of a range (0: never; default %(default)s)')
    sub.set_defaults(func=cmd_receive)
    sub = commands.add_parser('send', help='on the source: send a cleanly stopped data directory')
    sub.add_argument('--source', required=True, type=Path, help='the stopped data directory')
    sub.add_argument('--pg-controldata', required=True, metavar='PATH')
    sub.add_argument('--connect', required=True, metavar='IP:PORT')
    sub.add_argument('--stats-json', type=Path, metavar='PATH')
    sub.add_argument('--streams', required=True, type=positive_int, help='data connections to open')
    sub.add_argument('--chunk-bytes', type=positive_int, default=DEFAULT_CHUNK_BYTES,
                     help='the largest range (default %(default)s)')
    sub.set_defaults(func=cmd_send)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
