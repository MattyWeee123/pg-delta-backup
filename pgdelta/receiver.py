"""Destination side: rebuild O from the basis B plus received pages, verify, publish atomically.

The output is built in a fresh staging directory next to O. B is only ever
opened read-only. Every file is re-hashed after it is written and compared
with the source digest; O appears (by one rename) only after all files and
directories are in place and fsynced. Any failure before that rename
removes the staging tree.

B's files, the leaf digests B is asked for again and the verification of
the rebuilt files share one hash pool (pgdelta.tree.HashPool): `jobs`
worker processes, or the main thread when jobs is 1.

How O is built (`rebuild`):
- "copy-patch" (default): for a file B also has, B's bytes are copied into
  the staging file in large pieces (copy_file_range on Linux, else
  pread/pwrite through one buffer), each a few MiB ahead of the pages being
  received, and every received page is then written over them at its
  offset. Each byte of B is copied at most once; pages that are sent in a
  run from the start of a file are never copied first.
- "gap-fill" (EXP-05 and earlier): after the last page of a file arrives,
  every run of pages that was not received is copied from B on its own.

How O is verified (`verify`): every output file is re-read in full and its
digest compared with the source's before O is published, either right
after the file is written, in the main loop ("sequential"), or, when there
is a pool of workers, by a background thread while the main loop writes the
next file ("pipelined", default). Publication waits for every verification.

A receipt beside O (never inside it) records the run: it is written,
durably, with status "verified" just before the rename and rewritten with
status "published" once the rename is durable. The rename is the commit
point: a failure after it never deletes O, and a failure that only costs
the final acknowledgement to the sender is not a failure of the receiver.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import errno
import queue
import secrets
import shutil
import stat
import sys
import tempfile
import threading
import time

from pgdelta import __version__, placement, protocol
from pgdelta.errors import (EXIT_TRANSFER_FAILURE, InvalidInput, PeerError, PgDeltaError,
                            ProtocolError, TransferError, VerificationError)
from pgdelta.hashing import DIGEST_SIZE, KEY_SIZE, FilesDigest, Hasher
from pgdelta.identity import gate, read_identity, reuse_policy
from pgdelta.inventory import MODE_MASK, parse_entries, require_plain, scan
from pgdelta.protocol import ChannelClosed
from pgdelta.tree import (BACKENDS, DEFAULT_BACKEND, DEFAULT_JOBS, HashPool, _SizeChanged, hash_file,
                          node_count, open_regular, page_count, pread_exact)

STAGING_INFIX = '.pgdelta-staging-'
RECEIPT_SUFFIX = '.pgdelta-receipt.json'
RECEIPT_SCHEMA_VERSION = 1
COPY_BYTES = 1 << 20  # gap-fill: bytes per read and write
COPY_CHUNK_BYTES = 4 << 20  # copy-patch: the pread/pwrite buffer
COPY_AHEAD_BYTES = 4 << 20  # copy-patch: B is copied at least this far ahead of a page that needs it
LEAF_CACHE_BYTES = 64 << 20
REBUILDS = ('copy-patch', 'gap-fill')
DEFAULT_REBUILD = 'copy-patch'
VERIFY_MODES = ('pipelined', 'sequential')
DEFAULT_VERIFY = 'pipelined'
JOIN_SECONDS = 10.0
HELLO_KEYS = frozenset(('protocol_version', 'block', 'node_blocks', 'key', 'source_identity',
                        'root_mode', 'directories', 'files'))
_CREATE_FLAGS = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_CLOEXEC', 0)
                 | getattr(os, 'O_NOFOLLOW', 0))
_KERNEL_COPY = sys.platform.startswith('linux') and hasattr(os, 'copy_file_range')
# copy_file_range errors that mean "not here" (old kernel, two filesystems, unsupported): copy by hand.
_KERNEL_COPY_UNSUPPORTED = frozenset(code for code in (errno.EXDEV, errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP,
                                                       getattr(errno, 'ENOTSUP', None), errno.EPERM)
                                     if code is not None)


def staging_pattern(output):
    """Glob pattern (relative to the output's parent) matching staging directories for `output`."""
    return f'.{Path(output).name}{STAGING_INFIX}*'


def receipt_path(output):
    """The completion receipt for `output`: a sibling file, so it is never part of O."""
    output = Path(output)
    return output.with_name(output.name + RECEIPT_SUFFIX)


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _describe(exc):
    if isinstance(exc, PgDeltaError):
        return str(exc)
    if isinstance(exc, KeyboardInterrupt):
        return 'interrupted'
    return repr(exc)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def remove_tree(root):
    """Delete a staging tree even if some of its directories were made read-only."""
    def writable(path):
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    writable(root)
    for current, directories, _ in os.walk(root):
        for name in directories:
            writable(os.path.join(current, name))
    shutil.rmtree(root, ignore_errors=True)


def _write_all(fd, data, offset):
    view = memoryview(data)
    while view:
        written = os.pwrite(fd, view, offset)
        view, offset = view[written:], offset + written


class _BasisShort(Exception):
    """The basis file ended before the range being copied from it."""


class _Copier:
    """Copies a byte range from one file to the same offset in another, in large pieces.

    Linux: copy_file_range, so the kernel copies (or clones, on filesystems
    that share extents) without passing the bytes through Python. Where it is
    missing or refused, and elsewhere: pread/pwrite through one reused buffer.
    """

    def __init__(self, kernel=_KERNEL_COPY):
        self.kernel = kernel
        self._view = None

    def copy(self, source, target, offset, length):
        end = offset + length
        while offset < end:
            count = self._kernel_copy(source, target, offset, end) if self.kernel else 0
            if not count:
                count = self._buffer_copy(source, target, offset, end)
            offset += count

    def _kernel_copy(self, source, target, offset, end):
        try:
            count = os.copy_file_range(source, target, min(end - offset, 16 * COPY_CHUNK_BYTES), offset, offset)
        except OSError as exc:
            if exc.errno not in _KERNEL_COPY_UNSUPPORTED:
                raise
            count = 0
        if not count:
            # Refused, or nothing copied (end of file, or a filesystem that copies nothing):
            # copy by hand from now on, which also tells a real end of file apart.
            self.kernel = False
        return count

    def _buffer_copy(self, source, target, offset, end):
        if self._view is None:
            self._view = memoryview(bytearray(COPY_CHUNK_BYTES))
        view = self._view[:min(len(self._view), end - offset)]
        if hasattr(os, 'preadv'):
            count = os.preadv(source, [view], offset)
        else:
            data = os.pread(source, len(view), offset)
            count = len(data)
            view[:count] = data
        if not count:
            raise _BasisShort
        _write_all(target, view[:count], offset)
        return count


@dataclass
class ReceiverSetup:
    basis: Path
    output: Path
    inventory: object
    identity: object = None
    reuse_any_basis: bool = False
    # {label: (path, directory identities)} that O and its staging directory must stay out of.
    protected: dict = field(default_factory=dict)


def prepare_receiver(basis, output, *, identity=None, pg_controldata=None, reuse_any_basis=False,
                     local_source=None):
    """Local checks that need no peer: O is new, B is a plain, stopped copy, and O lies outside B.

    Pass local_source when S is on this host (local mode) so that O must
    lie outside S as well. Containment is judged by filesystem identity:
    O's parent and every directory above it are compared with B and each
    directory inside it (from B's scan), and with S.
    """
    basis, output = Path(basis), Path(output)
    protected = {}
    if local_source is not None:
        protected['source'] = (Path(local_source), {placement.directory_id(local_source, 'source')})
    protected['basis'] = (basis, {placement.directory_id(basis, 'basis')})
    placement.check_new_output(output, protected)
    receipt = receipt_path(output)
    if os.path.lexists(receipt) and not stat.S_ISREG(os.lstat(receipt).st_mode):
        raise InvalidInput(f'receipt path {receipt} exists and is not a regular file')
    if os.path.lexists(basis / 'postmaster.pid'):
        raise InvalidInput(f'basis {basis} contains postmaster.pid; a server may be running on it')
    inventory = require_plain(scan(basis), 'basis')
    protected['basis'] = (basis, frozenset(inventory.directory_ids))
    placement.refuse_inside(output.parent, protected, f'output {output}')
    if identity is None and pg_controldata:
        identity = read_identity(basis, pg_controldata)
    return ReceiverSetup(basis, output, inventory, identity, reuse_any_basis, protected)


@dataclass
class _Output:
    """File being rebuilt. Pages must arrive in increasing order, so written runs stay sorted.

    Copy-then-patch: basis_fd is B's file, and every byte below `copied`
    already holds B's bytes or a received page; B is copied up to copy_limit,
    the shorter of the two lengths.
    """
    index: int
    fd: int
    pages: int
    runs: list = field(default_factory=list)
    next_page: int = 0
    basis_fd: object = None
    copy_limit: int = 0
    copied: int = 0

    def gaps(self):
        """(first, stop) page ranges that were not received."""
        result, position = [], 0
        for first, count in self.runs + [(self.pages, 0)]:
            if first > position:
                result.append((position, first))
            position = first + count
        return result


@dataclass
class _Counts:
    files_verified: int = 0
    pages_received: int = 0
    page_bytes_received: int = 0
    pages_copied: int = 0
    bytes_copied: int = 0
    whole_file_copies: int = 0
    leaf_nodes_cached: int = 0
    leaf_nodes_reread: int = 0
    # How O was built; unlike the counts above, these depend on `rebuild`.
    basis_copy_bytes: int = 0  # bytes copied from B, including copy-then-patch bytes later overwritten
    basis_copies: int = 0  # copy operations from B (gap-fill: one per 1 MiB of each gap)


class _Verifier:
    """Re-hashes finished output files on the pool, in order, while the main loop writes the next file.

    The first failure is kept in `error` and reported to the sender at
    once; the main loop raises it at its next check. finish() waits for
    every file submitted so far; nothing may be published before it returns
    without an error.
    """

    def __init__(self, receiver):
        self.receiver = receiver
        self.queue = queue.Queue()
        self.error = None
        self.busy_seconds = 0.0
        self.thread = threading.Thread(target=self._run, name='pgdelta-verify', daemon=True)
        self.thread.start()

    def submit(self, entry, size, digest):
        self.queue.put((entry, size, digest))

    def finish(self):
        self.queue.put(None)
        self.thread.join()

    def close(self, timeout):
        self.queue.put(None)
        self.thread.join(timeout)

    def _run(self):
        receiver = self.receiver
        try:
            while True:
                item = self.queue.get()
                if item is None or receiver.stop.is_set():
                    return
                started = time.perf_counter()
                # A window of `jobs` tasks: verification queues behind less than B's hashing does.
                receiver._verify_file(*item, window=receiver.jobs)
                self.busy_seconds += time.perf_counter() - started
        except BaseException as exc:
            if not isinstance(exc, PgDeltaError):
                exc = TransferError(f'verification failed: {exc!r}')
            self.error = exc
            if not receiver.stop.is_set():
                receiver._send_error(exc.exit_code, str(exc))


class _Receiver:
    def __init__(self, channel, setup, jobs, hash_backend=DEFAULT_BACKEND, rebuild=DEFAULT_REBUILD,
                 verify=DEFAULT_VERIFY, prefetch=None):
        self.channel, self.setup = channel, setup
        # What the listener's prefetch of B did before the sender connected (pgdelta.prefetch), or None.
        self.prefetch = prefetch
        self.jobs, self.pool = jobs, None
        self.hash_backend, self.rebuild, self.verify = hash_backend, rebuild, verify
        self.verifier, self.copier = None, _Copier()
        self.staging, self.published = None, False
        # published: the staging directory has been renamed to O (the commit point).
        # durable: that rename and the "published" receipt have been fsynced.
        self.durable, self.receipt_written, self.verified_at = False, False, None
        self.final_stats = None
        self.files_digest, self.total_bytes = FilesDigest(), 0
        self.current, self.next_file = None, 0
        self.mode = self.reason = self.source_identity = None
        self.policy = reuse_policy(setup.reuse_any_basis)
        self.files, self.directories, self.basis_files = [], [], {}
        self.counts = _Counts()
        self.hash_basis_seconds = self.hash_leaf_seconds = 0.0
        self.rebuild_seconds = self.verify_seconds = 0.0
        self.copy_seconds = self.fsync_seconds = self.verify_wait_seconds = 0.0
        self.pool_start_seconds = 0.0
        self.stop = threading.Event()
        self.basis_thread, self.basis_error = None, None
        self.error_lock, self.error_sent = threading.Lock(), False
        # Basis leaf digests kept from the first pass, within a byte budget,
        # so a LEAF_REQUEST rarely has to read the basis file a second time.
        self.cache_lock, self.leaf_cache, self.cache_reserved = threading.Lock(), {}, {}
        self.cache_bytes = 0

    def run(self):
        try:
            try:
                return self._run()
            except OSError as exc:
                raise TransferError(f'receiver I/O error: {exc}') from exc
        except BaseException as exc:
            self.stop.set()
            if self.durable:
                # O is complete, verified, published and recorded: only the acknowledgement was lost.
                return dict(self.final_stats or self.stats(), acknowledged=False,
                            acknowledgement_error=_describe(exc))
            if not isinstance(exc, (PeerError, ChannelClosed)):
                if isinstance(exc, PgDeltaError):
                    self._send_error(exc.exit_code, str(exc))
                elif isinstance(exc, KeyboardInterrupt):
                    self._send_error(EXIT_TRANSFER_FAILURE, 'receiver interrupted')
                else:
                    self._send_error(EXIT_TRANSFER_FAILURE, f'receiver internal error: {exc!r}')
            raise
        finally:
            self.stop.set()
            if self.verifier is not None:
                self.verifier.close(JOIN_SECONDS)
            if self.basis_thread is not None:
                self.basis_thread.join(JOIN_SECONDS)
            if self.pool is not None:
                # Safe even if a thread above is still inside the pool: its calls are cancelled, not hung.
                self.pool.close()
            if self.current is not None:
                os.close(self.current.fd)
                if self.current.basis_fd is not None:
                    os.close(self.current.basis_fd)
                self.current = None
            if self.staging is not None and not self.published:
                remove_tree(self.staging)
                if self.receipt_written:
                    try:
                        os.unlink(receipt_path(self.setup.output))
                    except OSError:
                        pass

    def _send_error(self, exit_code, message):
        with self.error_lock:
            if self.error_sent:
                return
            self.error_sent = True
        try:
            self.channel.send_json(protocol.ERROR, {'exit_code': exit_code, 'message': message})
        except PgDeltaError:
            pass

    def _recv(self):
        try:
            message = self.channel.recv()
        except ChannelClosed:
            self._raise_background_errors()
            raise
        if message is None:
            self._raise_background_errors()
            raise ChannelClosed('sender closed the connection before the transfer completed')
        code, payload = message
        if code == protocol.ERROR:
            info = protocol.decode_json(payload, 'ERROR')
            raise PeerError(f'sender: {info.get("message", "unspecified error")}', info.get('exit_code'))
        return code, payload

    def _raise_background_errors(self):
        if self.basis_error is not None:
            raise self.basis_error
        if self.verifier is not None and self.verifier.error is not None:
            raise self.verifier.error

    def _check_options(self):
        if type(self.jobs) is not int or self.jobs < 1:
            raise InvalidInput(f'jobs must be a positive integer, not {self.jobs!r}')
        for label, value, choices in (('hash backend', self.hash_backend, BACKENDS),
                                      ('rebuild', self.rebuild, REBUILDS), ('verify', self.verify, VERIFY_MODES)):
            if value not in choices:
                raise InvalidInput(f'{label} must be one of {", ".join(choices)}, not {value!r}')

    def _start_pool(self):
        started = time.perf_counter()
        try:
            self.pool = HashPool(self.jobs, backend=self.hash_backend, key=self.hasher.key)
        except (OSError, ValueError) as exc:
            raise TransferError(f'cannot start the hashing workers: {exc}') from exc
        self.pool_start_seconds = time.perf_counter() - started
        if self.verify == 'pipelined' and self.pool.parallel:
            self.verifier = _Verifier(self)

    def switches(self):
        """The engine settings in effect (verification is sequential without a pool of workers)."""
        return {
            'jobs': self.jobs,
            'hash_backend': self.pool.backend if self.pool is not None else None,
            'rebuild': self.rebuild,
            'verify': 'pipelined' if self.verifier is not None else 'sequential',
        }

    def _run(self):
        self._check_options()
        self._read_hello()
        self.reuse, self.reason = gate(self.source_identity, self.setup.identity, self.setup.reuse_any_basis)
        self.mode = 'reuse' if self.reuse else 'full'
        basis_map = self.setup.inventory.file_map()
        self.basis_files = {index: basis_map[entry.path].size
                            for index, entry in enumerate(self.files) if entry.path in basis_map}
        self.channel.send_json(protocol.ACCEPT, {
            'mode': self.mode,
            'reason': self.reason,
            'reuse_policy': self.policy,
            'basis_identity': self.setup.identity,
            'basis_files': [[index, size] for index, size in sorted(self.basis_files.items())],
        })
        self._start_pool()
        self._create_staging()
        if self.reuse and self.basis_files:
            self.basis_thread = threading.Thread(target=self._hash_basis, name='pgdelta-basis',
                                                 daemon=True)
            self.basis_thread.start()
        handlers = {protocol.LEAF_REQUEST: self._leaf_request, protocol.PAGE_DATA: self._page_data,
                    protocol.FILE_DONE: self._file_done}
        while True:
            code, payload = self._recv()
            if code == protocol.COMPLETE:
                return self._complete(payload)
            if code not in handlers:
                raise ProtocolError(f'unexpected {protocol.name(code)} from the sender')
            handlers[code](payload)
            self._raise_background_errors()

    def _read_hello(self):
        code, payload = self._recv()
        if code != protocol.HELLO:
            raise ProtocolError(f'expected HELLO, received {protocol.name(code)}')
        hello = protocol.decode_json(payload, 'HELLO')
        if set(hello) != HELLO_KEYS:
            raise ProtocolError(f'HELLO fields differ from protocol version {protocol.PROTOCOL_VERSION}: '
                                f'missing {sorted(HELLO_KEYS - set(hello))}, '
                                f'unknown {sorted(set(hello) - HELLO_KEYS)}')
        if hello['protocol_version'] != protocol.PROTOCOL_VERSION:
            raise InvalidInput(f'unsupported protocol version {hello["protocol_version"]!r}')
        protocol.check_parameters(hello['block'], hello['node_blocks'])
        self.block, self.node_blocks = hello['block'], hello['node_blocks']
        try:
            key = base64.b64decode(hello['key'], validate=True) if isinstance(hello['key'], str) else b''
        except (binascii.Error, ValueError):
            key = b''
        if len(key) != KEY_SIZE:
            raise ProtocolError(f'HELLO key must be {KEY_SIZE} base64-encoded bytes')
        self.hasher = Hasher(key)
        identity = hello['source_identity']
        if identity is not None and not isinstance(identity, dict):
            raise ProtocolError('HELLO source_identity must be an object or null')
        self.source_identity = identity
        root_mode = hello['root_mode']
        if type(root_mode) is not int or not 0 <= root_mode <= MODE_MASK:
            raise InvalidInput(f'invalid root_mode {root_mode!r}')
        self.root_mode = root_mode
        directories, self.files = parse_entries(hello['directories'], hello['files'])
        self.directories = sorted(directories, key=lambda entry: entry.path)

    def _create_staging(self):
        output = self.setup.output
        # Repeat the placement check: O's parent may have changed since prepare_receiver().
        placement.check_new_output(output, self.setup.protected)
        staging = Path(tempfile.mkdtemp(prefix=f'.{output.name}{STAGING_INFIX}', dir=output.parent))
        try:
            placement.refuse_inside(staging, self.setup.protected, f'staging directory {staging} for {output}')
        except BaseException:
            os.rmdir(staging)
            raise
        self.staging = staging
        for entry in self.directories:
            try:
                os.mkdir(self.staging / entry.path, 0o700)
            except FileExistsError as exc:
                raise InvalidInput(f'cannot create directory {entry.path!r}: it collides with another '
                                   f'path on this filesystem') from exc

    def _hash_basis(self):
        try:
            for index, size in sorted(self.basis_files.items()):
                if self.stop.is_set():
                    return
                path = self.files[index].path
                keep = self._reserve_leaves(index, size)
                started = time.perf_counter()
                try:
                    tree = hash_file(self.setup.basis / path, None, self.block, self.node_blocks,
                                     keep_leaves=keep, hasher=self.hasher, pool=self.pool, stop=self.stop)
                except OSError as exc:
                    raise TransferError(f'cannot read basis file {path}: {exc}') from exc
                self.hash_basis_seconds += time.perf_counter() - started
                if tree.size != size:
                    raise TransferError(f'basis file {path} changed size during the run; B must be immutable')
                if keep:
                    with self.cache_lock:
                        self.leaf_cache[index] = tree.leaf_blob
                self.channel.send(protocol.BASIS_NODES,
                                  protocol.encode_basis_nodes(index, size, tree.node_blob))
        except BaseException as exc:
            if not isinstance(exc, PgDeltaError):
                exc = TransferError(f'basis hashing failed: {exc!r}')
            self.basis_error = exc
            if not isinstance(exc, ChannelClosed) and not self.stop.is_set():
                self._send_error(exc.exit_code, str(exc))

    def _reserve_leaves(self, index, size):
        need = page_count(size, self.block) * DIGEST_SIZE
        with self.cache_lock:
            if self.cache_bytes + need > LEAF_CACHE_BYTES:
                return False
            self.cache_bytes += need
            self.cache_reserved[index] = need
            return True

    def _release_leaves(self, index):
        with self.cache_lock:
            self.leaf_cache.pop(index, None)
            self.cache_bytes -= self.cache_reserved.pop(index, 0)

    def _leaf_request(self, payload):
        index, nodes = protocol.decode_leaf_request(payload)
        if not self.reuse or index not in self.basis_files:
            raise ProtocolError(f'LEAF_REQUEST for file {index}, which has no basis digests')
        size, path = self.basis_files[index], self.files[index].path
        limit = node_count(size, self.block, self.node_blocks)
        if (len(nodes) > protocol.max_nodes_per_request(self.node_blocks) or len(set(nodes)) != len(nodes)
                or any(node >= limit for node in nodes)):
            raise ProtocolError(f'invalid LEAF_REQUEST for {path}')
        with self.cache_lock:
            cached = self.leaf_cache.get(index)
        if cached is not None:
            width = self.node_blocks * DIGEST_SIZE
            entries = [(node, cached[node * width:(node + 1) * width]) for node in nodes]
            self.counts.leaf_nodes_cached += len(nodes)
        else:
            started = time.perf_counter()
            try:
                fd = open_regular(self.setup.basis / path)
                try:
                    parts = self.pool.node_digests(self.setup.basis / path, fd, size, self.hasher, self.block,
                                                   self.node_blocks, nodes, True, stop=self.stop)
                finally:
                    os.close(fd)
                entries = [(node, leaves) for node, (_, leaves) in zip(nodes, parts)]
            except _SizeChanged:
                raise TransferError(f'basis file {path} changed size during the run; '
                                    f'B must be immutable') from None
            except OSError as exc:
                raise TransferError(f'cannot read basis file {path}: {exc}') from exc
            self.hash_leaf_seconds += time.perf_counter() - started
            self.counts.leaf_nodes_reread += len(nodes)
        self.channel.send(protocol.LEAF_DIGESTS, protocol.encode_leaf_digests(index, entries))

    def _open(self, index):
        if index != self.next_file:
            raise ProtocolError(f'data for file {index} arrived while file {self.next_file} is pending')
        if self.current is None:
            entry = self.files[index]
            try:
                fd = os.open(self.staging / entry.path, _CREATE_FLAGS, 0o600)
            except FileExistsError as exc:
                raise InvalidInput(f'cannot create {entry.path!r}: it collides with another path '
                                   f'on this filesystem') from exc
            self.current = _Output(index, fd, page_count(entry.size, self.block))
            basis_size = self.basis_files.get(index) if self.reuse else None
            if self.rebuild == 'copy-patch' and basis_size and entry.size:
                self.current.basis_fd = self._open_basis(entry.path, basis_size)
                self.current.copy_limit = min(basis_size, entry.size)
        return self.current

    def _open_basis(self, path, size):
        try:
            fd = open_regular(self.setup.basis / path)
        except OSError as exc:
            raise TransferError(f'cannot read basis file {path}: {exc}') from exc
        try:
            actual = os.fstat(fd).st_size
        except BaseException:
            os.close(fd)
            raise
        if actual != size:
            os.close(fd)
            raise TransferError(f'basis file {path} changed size during the run; B must be immutable')
        return fd

    def _copy_basis(self, entry, output, stop):
        """Copy-then-patch: copy B's bytes [output.copied, stop) into the output file."""
        start = output.copied
        started = time.perf_counter()
        try:
            self.copier.copy(output.basis_fd, output.fd, start, stop - start)
        except _BasisShort:
            raise TransferError(f'basis file {entry.path} changed size during the run; '
                                f'B must be immutable') from None
        except OSError as exc:
            raise TransferError(f'cannot copy basis file {entry.path} into the output: {exc}') from exc
        self.copy_seconds += time.perf_counter() - started
        output.copied = stop
        self.counts.basis_copy_bytes += stop - start
        self.counts.basis_copies += 1

    def _page_data(self, payload):
        index, first, data = protocol.decode_page_data(payload)
        if index >= len(self.files):
            raise ProtocolError(f'PAGE_DATA for unknown file {index}')
        output, entry = self._open(index), self.files[index]
        offset, length = first * self.block, len(data)
        end = offset + length
        if first >= output.pages or end > entry.size or (length % self.block and end != entry.size):
            raise ProtocolError(f'PAGE_DATA for {entry.path} does not fit the file')
        if first < output.next_page:
            raise ProtocolError(f'PAGE_DATA for {entry.path} overlaps or precedes pages already written')
        count = -(-length // self.block)
        started = time.perf_counter()
        if output.basis_fd is not None and offset > output.copied:
            # B's bytes go in first, in one large piece that reaches at least COPY_AHEAD_BYTES further.
            stop = min(max(offset, output.copied + COPY_AHEAD_BYTES), output.copy_limit)
            if stop > output.copied:
                self._copy_basis(entry, output, stop)
        _write_all(output.fd, data, offset)
        output.copied = max(output.copied, end)
        self.rebuild_seconds += time.perf_counter() - started
        if output.runs and output.runs[-1][0] + output.runs[-1][1] == first:
            output.runs[-1] = (output.runs[-1][0], output.runs[-1][1] + count)
        else:
            output.runs.append((first, count))
        output.next_page = first + count
        self.counts.pages_received += count
        self.counts.page_bytes_received += length

    def _file_done(self, payload):
        index, size, digest = protocol.decode_file_done(payload)
        if index >= len(self.files):
            raise ProtocolError(f'FILE_DONE for unknown file {index}')
        output, entry = self._open(index), self.files[index]
        if size != entry.size:
            raise ProtocolError(f'FILE_DONE size for {entry.path} differs from the inventory')
        self._release_leaves(index)
        started = time.perf_counter()
        basis_size = self.basis_files.get(index) if self.reuse else None
        gaps = output.gaps()
        self._check_gaps(entry, gaps, basis_size)
        if size and basis_size == size and not output.runs:
            self.counts.whole_file_copies += 1
        if output.basis_fd is not None:
            if output.copied < output.copy_limit:  # B after the last page received
                self._copy_basis(entry, output, output.copy_limit)
            os.close(output.basis_fd)
            output.basis_fd = None
        elif gaps:
            self._fill_from_basis(entry, output, gaps)
        for start, stop in gaps:
            self.counts.pages_copied += stop - start
            self.counts.bytes_copied += min(stop * self.block, size) - start * self.block
        if os.fstat(output.fd).st_size != size:
            raise ProtocolError(f'{entry.path} was rebuilt with the wrong length')
        synced = time.perf_counter()
        os.fsync(output.fd)
        self.fsync_seconds += time.perf_counter() - synced
        os.close(output.fd)
        self.current = None
        self.rebuild_seconds += time.perf_counter() - started
        if self.verifier is not None:
            self._accept_file(entry, size, digest)
            self.verifier.submit(entry, size, digest)
        else:
            started = time.perf_counter()
            self._verify_file(entry, size, digest)
            elapsed = time.perf_counter() - started
            self.verify_seconds += elapsed
            self.verify_wait_seconds += elapsed
            self._accept_file(entry, size, digest)

    def _accept_file(self, entry, size, digest):
        self.files_digest.add(entry.path, size, digest)
        self.total_bytes += size
        self.next_file += 1

    def _check_gaps(self, entry, gaps, basis_size):
        """Every page that was not received must exist at the same offset and length in B."""
        if not gaps:
            return
        if basis_size is None:
            raise ProtocolError(f'{entry.path}: page {gaps[0][0]} was neither sent nor available from the basis')
        for start, stop in gaps:
            if (stop > page_count(basis_size, self.block)
                    or min(stop * self.block, entry.size) != min(stop * self.block, basis_size)):
                raise ProtocolError(f'{entry.path}: pages {start}-{stop - 1} were neither sent '
                                    f'nor present at the same length in the basis')

    def _verify_file(self, entry, size, digest, window=None):
        """Re-read the whole output file and compare its digest with the source's; then apply its mode."""
        path = self.staging / entry.path
        try:
            tree = hash_file(path, None, self.block, self.node_blocks, keep_leaves=False, hasher=self.hasher,
                             pool=self.pool, stop=self.stop, window=window)
        except OSError as exc:
            raise TransferError(f'cannot re-read {entry.path} to verify it: {exc}') from exc
        if tree.size != size or tree.digest != digest:
            raise VerificationError(f'{entry.path}: rebuilt file does not match the source digest')
        os.chmod(path, entry.mode)
        self.counts.files_verified += 1

    def _fill_from_basis(self, entry, output, gaps):
        """Gap-fill: copy every run of pages that was not received from the same offset in the basis file."""
        copied = time.perf_counter()
        try:
            basis_fd = open_regular(self.setup.basis / entry.path)
        except OSError as exc:
            raise TransferError(f'cannot read basis file {entry.path}: {exc}') from exc
        try:
            for start, stop in gaps:
                begin, end = start * self.block, min(stop * self.block, entry.size)
                for offset in range(begin, end, COPY_BYTES):
                    try:
                        data = pread_exact(basis_fd, min(COPY_BYTES, end - offset), offset)
                    except OSError as exc:
                        raise TransferError(f'cannot read basis file {entry.path}: {exc}') from exc
                    _write_all(output.fd, data, offset)
                    self.counts.basis_copies += 1
                self.counts.basis_copy_bytes += end - begin
        finally:
            os.close(basis_fd)
            self.copy_seconds += time.perf_counter() - copied

    def _complete(self, payload):
        info = protocol.decode_json(payload, 'COMPLETE')
        if (self.current is not None or self.next_file != len(self.files)
                or info.get('files') != len(self.files) or info.get('directories') != len(self.directories)):
            raise ProtocolError('COMPLETE arrived before every file was transferred and verified')
        if self.basis_thread is not None:
            self.basis_thread.join(JOIN_SECONDS)
        if self.verifier is not None:
            started = time.perf_counter()
            self.verifier.finish()
            self.verify_seconds = self.verifier.busy_seconds
            self.verify_wait_seconds += time.perf_counter() - started
        self._raise_background_errors()
        if self.counts.files_verified != len(self.files):
            raise TransferError(f'internal error: {self.counts.files_verified} of {len(self.files)} files '
                                f'were verified; not publishing')
        started = time.perf_counter()
        for entry in reversed(self.directories):
            path = self.staging / entry.path
            os.chmod(path, entry.mode)
            fsync_dir(path)
        os.chmod(self.staging, self.root_mode)
        fsync_dir(self.staging)
        output = self.setup.output
        if os.path.lexists(output):
            raise InvalidInput(f'output {output} appeared during the run; not replacing it')
        self._write_receipt('verified')
        self._publish(output)
        self.rebuild_seconds += time.perf_counter() - started
        self.final_stats = self.stats()
        self.channel.send_json(protocol.COMPLETE, {'published': True, 'receiver': self.final_stats})
        return dict(self.final_stats, acknowledged=True, acknowledgement_error=None)

    def _publish(self, output):
        """Rename the verified staging directory to O, then make that and the receipt durable.

        An interrupt (SIGINT/SIGTERM) after the rename is held back until
        the publication is durable, which takes milliseconds, and raised
        after. An I/O error then is a failure, but O is kept: it is verified.
        """
        try:
            os.rename(self.staging, output)
            self.published = True
        except KeyboardInterrupt:
            if os.path.lexists(self.staging):
                raise  # interrupted before the rename
            self.published = True
            interrupted = True
        else:
            interrupted = False
        for step in (lambda: fsync_dir(output.parent), lambda: self._write_receipt('published')):
            while True:
                try:
                    step()
                    break
                except KeyboardInterrupt:
                    interrupted = True
                except OSError as exc:
                    raise TransferError(f'{output} was published, but the publication could not be made '
                                        f'durable: {exc}; O is complete and verified, but its receipt may '
                                        f'still say "verified"') from exc
        self.durable = True
        if interrupted:
            raise KeyboardInterrupt('after publishing')

    def _write_receipt(self, status):
        """Atomically replace the receipt beside O, then fsync it and O's parent."""
        path = receipt_path(self.setup.output)
        now = _utc_now()
        if status == 'verified':
            self.verified_at = now
        record = {
            'schema_version': RECEIPT_SCHEMA_VERSION,
            'tool': 'pgdelta',
            'tool_version': __version__,
            'status': status,
            'output': os.path.abspath(self.setup.output),
            'verified_at': self.verified_at,
            'published_at': now if status == 'published' else None,
            'files': len(self.files),
            'directories': len(self.directories),
            'bytes': self.total_bytes,
            'files_digest': self.files_digest.hexdigest(),
            'mode': self.mode,
        }
        data = (json.dumps(record, indent=2) + '\n').encode('utf-8')
        # A short name, so a long O name that fits its staging directory also fits here.
        temporary = path.with_name(f'.pgdelta-receipt-{secrets.token_hex(8)}.tmp')
        fd = os.open(temporary, _CREATE_FLAGS, 0o644)
        try:
            try:
                _write_all(fd, data, 0)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        self.receipt_written = True
        fsync_dir(path.parent)

    def stats(self):
        """Receiver's own view; its byte counts exclude the final COMPLETE frame it is about to send."""
        return {
            'mode': self.mode,
            'gate_reason': self.reason,
            'reuse_policy': self.policy,
            'source_identity': self.source_identity,
            'basis_identity': self.setup.identity,
            'output': str(self.setup.output),
            'published': self.published,
            'receipt': str(receipt_path(self.setup.output)),
            'files_digest': self.files_digest.hexdigest() if self.next_file == len(self.files) else None,
            'files': len(self.files),
            'directories': len(self.directories),
            'jobs': self.jobs,
            'switches': self.switches(),
            **vars(self.counts),
            'hash_basis_seconds': round(self.hash_basis_seconds + self.hash_leaf_seconds, 6),
            # Writing O: page writes, copies from B, fsyncs, and the final directory fsyncs and rename.
            'rebuild_seconds': round(self.rebuild_seconds, 6),
            'copy_seconds': round(self.copy_seconds, 6),
            'fsync_seconds': round(self.fsync_seconds, 6),
            # Time spent re-hashing O; pipelined, it overlaps writing O and verify_wait_seconds is
            # the part the main loop waited for (sequential: the two are equal).
            'verify_seconds': round(self.verify_seconds, 6),
            'verify_wait_seconds': round(self.verify_wait_seconds, 6),
            'pool_start_seconds': round(self.pool_start_seconds, 6),
            # Until the first, and until every one, of the workers had started (null until then).
            'pool_first_ready_seconds': _rounded(None if self.pool is None else self.pool.first_ready_seconds),
            'pool_ready_seconds': _rounded(None if self.pool is None else self.pool.ready_seconds),
            'worker_start_method': None if self.pool is None else self.pool.start_method,
            # Reading B into the page cache while the listener waited (pgdelta.prefetch); null if not asked.
            'prefetch': self.prefetch,
            'bytes_read': self.channel.received.summary(),
            'bytes_written': self.channel.sent.summary(),
        }


def _rounded(seconds):
    return None if seconds is None else round(seconds, 6)


def run_receiver(channel, basis=None, output=None, *, setup=None, identity=None, pg_controldata=None,
                 reuse_any_basis=False, local_source=None, jobs=DEFAULT_JOBS, hash_backend=DEFAULT_BACKEND,
                 rebuild=DEFAULT_REBUILD, verify=DEFAULT_VERIFY, prefetch=None):
    """Run one session as the receiver and return its statistics dict.

    Pass a ReceiverSetup from prepare_receiver(), or basis and output to run
    those checks here; a failed check is reported to the sender as ERROR.
    `jobs` workers (processes, or threads with hash_backend="thread") hash B
    and verify O; jobs=1 does it all in this process, on its main and basis
    threads. `rebuild` ("copy-patch" or "gap-fill") and `verify`
    ("pipelined" or "sequential") choose how O is built and verified. The
    output, and every statistic but timings, is the same for any of them.
    With jobs > 1, a program calling this must guard its main module
    (`if __name__ == '__main__':`), as multiprocessing's spawn requires.
    `prefetch`: the statistics of a prefetch of B made before the channel
    connected (pgdelta.prefetch), reported as they are under "prefetch".
    """
    if setup is None:
        try:
            setup = prepare_receiver(basis, output, identity=identity, pg_controldata=pg_controldata,
                                     reuse_any_basis=reuse_any_basis, local_source=local_source)
        except PgDeltaError as exc:
            try:
                channel.send_json(protocol.ERROR, {'exit_code': exc.exit_code, 'message': str(exc)})
            except PgDeltaError:
                pass
            raise
    return _Receiver(channel, setup, jobs, hash_backend, rebuild, verify, prefetch).run()
