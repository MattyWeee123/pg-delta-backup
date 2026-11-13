"""The destination side: build O from B plus the chunks that differ, verify every file, publish O.

B is only ever read. O is built in a staging directory next to it and appears, by one rename, only
after every file is written, synced and verified against S's whole-file digest. Any failure removes
the staging directory, so O either appears complete and verified, or not at all.

Naive on purpose: it answers one chunk question at a time, reading and hashing B's chunk only when
asked, and verifies each file by reading it back from disk after writing it.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

from pgdelta import protocol
from pgdelta.hashing import Hasher
from pgdelta.protocol import (CHUNK_DATA, CHUNK_DATA_HEAD, CHUNK_HASH, CHUNK_HASH_BODY, COMPLETE, DIFFERENT,
                              FILE_DONE, FILE_DONE_BODY, HELLO, RESULT, SAME, VERDICT, VERDICT_BODY, Channel)
from pgdelta.stats import Stats
from pgdelta.tree import Entry

READ_BYTES = 1 << 20


class VerificationError(Exception):
    """A file written in O doesn't match S's digest."""


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
    if hello.get('version') != protocol.VERSION:
        raise protocol.ProtocolError(f'protocol version {hello.get("version")!r}, expected {protocol.VERSION}')
    chunk_size = int(hello['chunk_size'])
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
    has_basis = {index for index, _ in basis_sizes}
    channel.send_json(protocol.ACCEPT, {'basis_sizes': basis_sizes})

    files = {}  # index -> (output fd, basis fd or None), for the file being written
    done = set()
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
                base = os.open(basis / entries[index].path, os.O_RDONLY) if index in has_basis else None
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
            if kind == CHUNK_HASH:
                index, chunk, digest = CHUNK_HASH_BODY.unpack(payload)
                out, base = open_file(index)
                if base is None:
                    raise protocol.ProtocolError(f'asked about {entries[index].path}, which B lacks')
                offset, length = span(index, chunk)
                with stats.timed('read_basis'):
                    data = os.pread(base, length, offset)
                with stats.timed('hash'):
                    same = len(data) == length and hasher.chunk(data) == digest
                if same:
                    with stats.timed('write'):
                        os.pwrite(out, data, offset)
                    stats.counts['chunks_same'] += 1
                with stats.timed('send'):
                    channel.send(VERDICT, VERDICT_BODY.pack(SAME if same else DIFFERENT))
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
                channel.send_json(RESULT, {'ok': True, 'stats': report})
                return report
            elif kind == protocol.ERROR:
                raise protocol.PeerError(json.loads(payload).get('message', 'unknown error'))
            else:
                raise protocol.ProtocolError(f'unexpected {protocol.NAMES.get(kind, kind)}')
    except BaseException as exc:
        for handles in files.values():
            _close(handles)
        if not isinstance(exc, (protocol.PeerError, protocol.Closed)):
            try:
                channel.send_json(protocol.ERROR, {'message': f'receiver failed: {exc}'})
            except OSError:
                pass
        shutil.rmtree(staging, ignore_errors=True)
        raise


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
