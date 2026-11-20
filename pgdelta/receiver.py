"""The destination side: build O from B plus the chunks that differ, verify every file, publish O.

B is only ever read. O is built in a staging directory next to it and appears, by one rename, only
after every file is written, synced and verified against S's whole-file digest. Any failure removes
the staging directory, so O either appears complete and verified, or not at all.

Two threads. A generator thread walks the same file list in the same order as the sender, reads and
hashes B, and streams `BASIS_DIGESTS` without being asked -- it is the only writer in this direction
until it is finished. The main thread stays in `recv` for the whole session, which is what lets the
sender stream page data at us while we stream digests back, and applies what arrives: `CHUNK_COPY`
copies a run of chunks out of B, `CHUNK_DATA` writes the bytes the sender sent instead.

The two threads share no mutable state: the generator keeps its own basis file handles and never
touches `files`, and every write to O is positional (`os.pwrite`), so no file offset is ever shared.
"""
from __future__ import annotations

import json
import os
import select
import shutil
import socket
import stat
import threading
from pathlib import Path

from pgdelta import protocol
from pgdelta.hashing import Hasher
from pgdelta.protocol import (BASIS_DIGESTS, BASIS_DIGESTS_HEAD, CHUNK_COPY, CHUNK_COPY_BODY, CHUNK_DATA,
                              CHUNK_DATA_HEAD, COMPLETE, FILE_DONE, FILE_DONE_BODY, HELLO, RESULT, Channel,
                              comparable)
from pgdelta.stats import Stats
from pgdelta.tree import Entry

READ_BYTES = 1 << 20


class VerificationError(Exception):
    """A file written in O doesn't match S's digest."""


class _StreamBroken(Exception):
    """A digest frame could not be written because the sender is gone. Its own failure is the one
    that matters, and the main loop sees the same break in its own `recv`, so nothing is reported."""


def run_receiver(channel: Channel, basis: Path, output: Path) -> dict:
    """Receive S from the sender at the other end of `channel`, building `output` from `basis`."""
    basis, output = Path(basis), Path(output)
    stats = Stats('receiver')
    if output.exists() or output.is_symlink():
        raise FileExistsError(f'{output} already exists; pgdelta never overwrites an output')
    if not basis.is_dir():
        raise FileNotFoundError(f'{basis} is not a directory')
    staging = output.with_name(f'.{output.name}.pgdelta-partial')

    hello = json.loads(channel.expect(HELLO))

    def refuse(message: str) -> protocol.ProtocolError:
        """The error for a HELLO this side will not act on, with the peer told why first.

        These checks run before the try/except below -- no staging directory exists yet, so there
        is nothing there to clean up. The ERROR frame goes out here, before the raise, or the peer
        sees only the connection close with the real reason stuck on this side's stderr: no help
        when the two ends are on different machines.
        """
        try:
            channel.send_json(protocol.ERROR, {'message': message})
        except OSError:
            pass
        return protocol.ProtocolError(message)

    if hello.get('version') != protocol.VERSION:
        raise refuse(f'protocol version {hello.get("version")!r}, expected {protocol.VERSION}')
    chunk_size = int(hello['chunk_size'])
    # Peer-supplied, and the generator below loops on it: at chunk_size 0, protocol.comparable is
    # true for every chunk of a non-empty file because the offset it computes never advances, so an
    # unchecked 0 is a thread spinning out digests for ever on one HELLO field. Checked here, before
    # ACCEPT and before that thread starts.
    if not protocol.MIN_CHUNK_SIZE <= chunk_size <= protocol.MAX_CHUNK_SIZE:
        raise refuse(f'chunk size {chunk_size}, expected between {protocol.MIN_CHUNK_SIZE} '
                     f'and {protocol.MAX_CHUNK_SIZE} bytes')
    root_mode = int(hello.get('root_mode', 0o700)) & 0o7777  # PostgreSQL refuses a data directory wider than 0750
    hasher = Hasher(bytes.fromhex(hello['key']))
    entries = [Entry.from_json(value) for value in hello['entries']]
    _check_parents(entries)

    basis_sizes = []
    for index, entry in enumerate(entries):
        if entry.kind == 'file':
            try:
                info = (basis / entry.path).lstat()
            except (FileNotFoundError, NotADirectoryError):
                continue
            if stat.S_ISREG(info.st_mode):
                basis_sizes.append([index, info.st_size])
    basis_size = {index: size for index, size in basis_sizes}  # also tells has_basis: its keys
    channel.send_json(protocol.ACCEPT, {'basis_sizes': basis_sizes})

    stop = threading.Event()
    generator_error: list[BaseException] = []

    def emit(index: int, first_chunk: int, digests: list[bytes]) -> None:
        payload = BASIS_DIGESTS_HEAD.pack(index, first_chunk, len(digests)) + b''.join(digests)
        try:
            with stats.timed('send_digests'):
                channel.send(BASIS_DIGESTS, payload)
        except OSError as exc:
            raise _StreamBroken from exc
        stats.counts['basis_chunks_hashed'] += len(digests)

    def generate() -> None:
        """Walk `entries` in HELLO order and stream B's digests, unasked, for exactly the chunks the
        sender compares (see protocol.comparable -- a contiguous run per file, so a frame needs only its
        first chunk index). Opens its own basis handles and never touches `files`: `open_file`'s "the
        previous file is finished" rule belongs to the main loop alone."""
        try:
            for index, entry in enumerate(entries):
                size = basis_size.get(index)
                if size is None or not comparable(0, chunk_size, entry.size, size):
                    continue   # B lacks this file, or holds no whole chunk of S's range of it
                fd = os.open(basis / entry.path, os.O_RDONLY)
                try:
                    first_chunk, chunk, pending = 0, 0, []
                    while comparable(chunk, chunk_size, entry.size, size):
                        if stop.is_set():
                            return
                        offset = chunk * chunk_size
                        length = min(chunk_size, entry.size - offset)
                        with stats.timed('read_digests'):
                            data = os.pread(fd, length, offset)
                        if len(data) != length:
                            raise RuntimeError(f'{entry.path} shrank in B while it was hashed; B must not change')
                        with stats.timed('hash_digests'):
                            pending.append(hasher.chunk(data))
                        chunk += 1
                        if len(pending) == protocol.DIGESTS_PER_FRAME:
                            emit(index, first_chunk, pending)
                            first_chunk, pending = chunk, []
                    if pending:
                        emit(index, first_chunk, pending)
                finally:
                    os.close(fd)
        except _StreamBroken:
            pass
        except BaseException as exc:
            generator_error.append(exc)
            # The sender may be waiting for a digest that will now never come, and the main loop
            # for a frame the sender will never send: break the connection so both raise rather
            # than wait forever.
            try:
                channel.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    files = {}  # index -> (output fd, basis fd or None), for the file being written
    done = set()
    generator = threading.Thread(target=generate, name='pgdelta-basis-generator', daemon=True)
    generator.start()
    try:
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        for entry in entries:
            target = staging / entry.path
            if entry.kind == 'dir':
                target.mkdir(mode=0o700)
            elif entry.kind == 'link':
                os.symlink(entry.target, target)

        def open_file(index: int):
            if index not in files:
                if index in done or not 0 <= index < len(entries) or entries[index].kind != 'file':
                    raise protocol.ProtocolError(f'unexpected data for entry {index}')
                for other in list(files):  # files arrive in order: the previous one is finished
                    _close(files.pop(other))
                out = os.open(staging / entries[index].path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
                base = os.open(basis / entries[index].path, os.O_RDONLY) if index in basis_size else None
                files[index] = (out, base)
            return files[index]

        def span(index: int, chunk: int) -> tuple[int, int]:
            offset = chunk * chunk_size
            size = entries[index].size
            if offset >= size:
                raise protocol.ProtocolError(f'chunk {chunk} is past the end of {entries[index].path}')
            return offset, min(chunk_size, size - offset)

        while True:
            kind, payload = channel.recv()
            if kind == CHUNK_COPY:
                # A run-length instruction, and nothing else: CHUNK_COPY carries no digest body, so
                # its payload is exactly the head.
                if len(payload) != CHUNK_COPY_BODY.size:
                    raise protocol.ProtocolError(
                        f'CHUNK_COPY payload is {len(payload)} bytes, expected {CHUNK_COPY_BODY.size}')
                index, first_chunk, count = CHUNK_COPY_BODY.unpack(payload)
                if count == 0:
                    raise protocol.ProtocolError('CHUNK_COPY count must be nonzero')
                out, base = open_file(index)
                if base is None:
                    raise protocol.ProtocolError(f'asked to copy {entries[index].path}, which B lacks')
                # The whole run is checked before a byte is written: its last chunk bounds the rest,
                # since offsets only ascend. A run that reaches past the file is a run spanning two.
                last = first_chunk + count - 1
                end_offset, end_length = span(index, last)
                if end_offset + end_length > basis_size[index]:
                    raise protocol.ProtocolError(f"chunk {last} of {entries[index].path} is outside B's size")
                for i in range(count):
                    offset, length = span(index, first_chunk + i)
                    with stats.timed('read_basis'):
                        data = os.pread(base, length, offset)
                    if len(data) != length:
                        raise protocol.ProtocolError(f'{entries[index].path} is shorter in B than B declared')
                    with stats.timed('write'):
                        os.pwrite(out, data, offset)
                    stats.counts['chunks_same'] += 1
                stats.counts['copy_runs'] += 1
            elif kind == CHUNK_DATA:
                index, chunk = CHUNK_DATA_HEAD.unpack_from(payload)
                data = payload[CHUNK_DATA_HEAD.size:]
                out, _ = open_file(index)
                offset, length = span(index, chunk)
                if len(data) != length:
                    raise protocol.ProtocolError(f'chunk {chunk} of {entries[index].path} has the wrong length')
                with stats.timed('write'):
                    os.pwrite(out, data, offset)
                stats.counts['chunks_received'] += 1
                stats.counts['bytes_written'] += length
            elif kind == FILE_DONE:
                index, size, digest = FILE_DONE_BODY.unpack(payload)
                out, _ = open_file(index)
                entry = entries[index]
                if size != entry.size:
                    raise protocol.ProtocolError(f'{entry.path}: size {size}, but HELLO said {entry.size}')
                os.ftruncate(out, size)
                with stats.timed('fsync'):
                    os.fsync(out)
                _close(files.pop(index))
                with stats.timed('verify'):
                    _verify(staging / entry.path, hasher, digest)
                os.chmod(staging / entry.path, entry.mode)
                done.add(index)
                stats.counts['files'] += 1
            elif kind == COMPLETE:
                missing = [entry.path for index, entry in enumerate(entries)
                           if entry.kind == 'file' and index not in done]
                if missing:
                    raise protocol.ProtocolError(f'{len(missing)} files were never finished, e.g. {missing[0]}')
                # The generator must be finished before RESULT goes out: one writer per direction,
                # and half a BASIS_DIGESTS frame ahead of RESULT would corrupt the stream.
                stop.set()
                _join_while_draining(channel.sock, generator)
                if generator_error:
                    raise generator_error[0]
                with stats.timed('fsync'):
                    for entry in reversed(entries):  # children before parents
                        if entry.kind == 'dir':
                            os.chmod(staging / entry.path, entry.mode)
                            _fsync_dir(staging / entry.path)
                    os.chmod(staging, root_mode)
                    _fsync_dir(staging)
                os.rename(staging, output)
                _fsync_dir(output.parent)
                report = stats.report(channel)
                try:
                    channel.send_json(RESULT, {'ok': True, 'stats': report})
                except OSError:
                    # O is renamed into place and every file in it is verified, so the refresh is
                    # finished whether or not this last frame lands. A sender that went away after
                    # COMPLETE -- or a half-close that had the drainer below shut this direction
                    # down -- must not turn that into a failed run with no report. The sender sees
                    # its own end of the connection break and reports that itself.
                    pass
                return report
            elif kind == protocol.ERROR:
                raise protocol.PeerError(json.loads(payload).get('message', 'unknown error'))
            else:
                raise protocol.ProtocolError(f'unexpected {protocol.NAMES.get(kind, kind)}')
    except BaseException as exc:
        # Stop the generator before writing anything: it owns this direction while it runs, and an
        # ERROR frame interleaved with a digest frame would be unreadable.
        stop.set()
        quiet = True
        try:
            _join_while_draining(channel.sock, generator)
        except BaseException:
            # Tearing the generator down must not cost the staging directory: socketpair() and
            # thread creation both fail under exhaustion, which is exactly when the rmtree below
            # matters most. Leaving the generator unjoined costs nothing -- `stop` is set, it is a
            # daemon, and its next send fails once the caller closes the socket -- but it may still
            # be writing, so the ERROR frame is skipped rather than interleaved into a digest frame.
            quiet = False
        for handles in files.values():
            _close(handles)
        # A generator failure breaks the connection deliberately, so the main loop usually surfaces
        # it as Closed: report the cause rather than the symptom, with the symptom chained to it.
        closed = None
        if generator_error and isinstance(exc, protocol.Closed):
            closed, exc = exc, generator_error[0]
        if quiet and not isinstance(exc, (protocol.PeerError, protocol.Closed)):
            try:
                channel.send_json(protocol.ERROR, {'message': f'receiver failed: {exc}'})
            except OSError:
                pass
        shutil.rmtree(staging, ignore_errors=True)
        if closed is not None:
            raise exc from closed
        raise exc


def _join_while_draining(sock: socket.socket, thread: threading.Thread) -> None:
    """Wait for `thread` to finish, reading and discarding anything the sender sends meanwhile.

    The draining is what makes the wait bounded, and it is not optional. The generator can be
    blocked in a send that only the sender's reader thread can take; that thread stops reading once
    its digest window is full, and the window only frees when the sender's main thread consumes a
    digest -- which it cannot do while it is blocked writing page data at us. Stop reading here and
    all four wait on each other for ever. Nothing read here is parsed: this runs only where the
    session is over, either at COMPLETE or on the way out with an error.

    The drainer ends on a byte through `waker`, so there is no timeout and no poll anywhere in it.
    It shuts this side's write direction down in one case only: end of stream while `thread` is
    still running, where that shutdown is the only thing that can unblock a generator parked in
    `send`. Once `thread` has finished there is nothing to unblock, so the direction is left open
    and a RESULT can still go out to a peer that half-closed and is waiting for one.
    """
    waker_read, waker_write = socket.socketpair()

    def discard() -> None:
        try:
            while True:
                ready, _, _ = select.select([sock, waker_read], [], [])
                if waker_read in ready:
                    return
                if not sock.recv(READ_BYTES):
                    # End of stream. A peer that closed both directions already fails the
                    # generator's send, but one that shut down only its write side does not: no FIN
                    # ever reaches the generator's direction, so it would stay parked in `send` and
                    # this join would never return. Shutting this side's write direction down makes
                    # that send fail -- but it also costs the RESULT this side may still owe, so it
                    # is done only while there is a generator left to unblock. `is_alive` is false
                    # only once the thread has finished, so a parked generator is never missed.
                    if thread.is_alive():
                        sock.shutdown(socket.SHUT_WR)
                    return
        except (OSError, ValueError):
            return   # ValueError: select on a socket a caller closed under us, which it should not

    drainer = threading.Thread(target=discard, name='pgdelta-drain', daemon=True)
    drainer.start()
    try:
        thread.join()
    finally:
        try:
            waker_write.send(b'\x00')
        except OSError:
            pass
        drainer.join()
        waker_read.close()
        waker_write.close()


def _check_parents(entries: list[Entry]) -> None:
    """Every entry's parent must be a directory listed before it, never a symlink: nothing is written
    through a link, so a crafted HELLO can't make the receiver write outside the staging directory."""
    dirs = {''}
    for entry in entries:
        parent = entry.path.rpartition('/')[0]
        if parent not in dirs:
            raise protocol.ProtocolError(f'{entry.path} is listed before its parent directory, or under a link')
        if entry.kind == 'dir':
            dirs.add(entry.path)


def _verify(path: Path, hasher: Hasher, expected: bytes) -> None:
    whole = hasher.file()
    with open(path, 'rb') as f:
        while block := f.read(READ_BYTES):
            whole.update(block)
    if whole.digest() != expected:
        raise VerificationError(f'{path.name} does not match the source after writing')


def _close(handles) -> None:
    for fd in handles:
        if fd is not None:
            os.close(fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
