"""Source side: hash S, compare against the basis digests, send only differing pages.

A reader thread drains everything the receiver sends (all of it small:
digests and acknowledgements), so neither side can block the other while
page data is in flight. Files are processed in inventory order with up to
`window` files hashed ahead, so leaf-digest round trips overlap hashing.
Each file's nodes are hashed on a pool of `jobs` worker processes (or
threads, for comparison), or in the main thread when jobs is 1.
"""
from __future__ import annotations

import base64
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
import os
import threading
import time

from pgdelta import __version__, matching, protocol
from pgdelta.errors import (EXIT_TRANSFER_FAILURE, InvalidInput, PeerError, PgDeltaError,
                            ProtocolError, TransferError, VerificationError)
from pgdelta.hashing import BLOCK, DIGEST_SIZE, NODE_BLOCKS, FilesDigest, Hasher, new_key
from pgdelta.inventory import require_plain, scan
from pgdelta.protocol import ChannelClosed
from pgdelta.receiver import RECEIPT_SUFFIX
from pgdelta.tree import (BACKENDS, DEFAULT_BACKEND, DEFAULT_JOBS, HashPool, hash_file, node_count, open_regular,
                          pread_exact)

STATS_SCHEMA_VERSION = 1
DEFAULT_WINDOW = 4
RUN_BYTES = 1 << 20
JOIN_SECONDS = 10.0


def prepare_source(source):
    inventory = require_plain(scan(source), 'source')
    if any(entry.path == 'postmaster.pid' for entry in inventory.files):
        raise InvalidInput('source contains postmaster.pid: v0 needs a completed backup, '
                           'not a running data directory')
    return inventory


class Inbox:
    """Reader thread for receiver -> sender messages."""

    def __init__(self, channel):
        self.channel = channel
        self.cond = threading.Condition()
        self.accept = self.complete = self.error = None
        self.basis, self.leaves = {}, {}
        self.closed = False
        self.thread = threading.Thread(target=self._run, name='pgdelta-inbox', daemon=True)

    def start(self):
        self.thread.start()

    def join(self, timeout):
        if self.thread.is_alive():
            self.thread.join(timeout)

    def settle(self, timeout):
        """Wait until the receiver has reported an error or the stream has ended."""
        with self.cond:
            self.cond.wait_for(lambda: self.error is not None or self.closed, timeout)

    def _run(self):
        try:
            while True:
                message = self.channel.recv()
                if message is None:
                    return
                code, payload = message
                with self.cond:
                    self._dispatch(code, payload)
                    self.cond.notify_all()
                if code == protocol.COMPLETE:
                    return
                if code == protocol.ERROR:
                    break
        except PgDeltaError as exc:
            self._set_error(exc)
        except Exception as exc:
            self._set_error(TransferError(f'reading from the receiver failed: {exc!r}'))
        finally:
            with self.cond:
                self.closed = True
                self.cond.notify_all()
        # After a failure, keep reading so the receiver never blocks writing to us while it cleans up.
        self.channel.discard_input()

    def _set_error(self, exc):
        with self.cond:
            if self.error is None:
                self.error = exc

    def _dispatch(self, code, payload):
        if code == protocol.ERROR:
            info = protocol.decode_json(payload, 'ERROR')
            if self.error is None:
                self.error = PeerError(f'receiver: {info.get("message", "unspecified error")}',
                                       info.get('exit_code'))
        elif self.accept is None:
            if code != protocol.ACCEPT:
                raise ProtocolError(f'expected ACCEPT, received {protocol.name(code)}')
            self.accept = protocol.decode_json(payload, 'ACCEPT')
        elif code == protocol.BASIS_NODES:
            index, size, nodes = protocol.decode_basis_nodes(payload)
            if index in self.basis:
                raise ProtocolError(f'duplicate BASIS_NODES for file {index}')
            self.basis[index] = (size, nodes)
        elif code == protocol.LEAF_DIGESTS:
            index, entries = protocol.decode_leaf_digests(payload)
            for node, blob in entries:
                self.leaves[(index, node)] = blob
        elif code == protocol.COMPLETE:
            self.complete = protocol.decode_json(payload, 'COMPLETE')
        else:
            raise ProtocolError(f'unexpected {protocol.name(code)} from the receiver')

    def wait(self, ready, what):
        with self.cond:
            while True:
                if self.error is not None:
                    raise self.error
                value = ready()
                if value is not None:
                    return value
                if self.closed:
                    raise ChannelClosed(f'receiver closed the connection while the sender waited for {what}')
                self.cond.wait()

    def check(self):
        if self.error is not None:
            raise self.error


@dataclass
class _Job:
    index: int
    path: str
    tree: object
    basis_size: object = None
    plan: matching.NodePlan = field(default_factory=matching.NodePlan)


def _parse_accept(accept, file_count):
    mode, reason = accept.get('mode'), accept.get('reason')
    if mode not in ('reuse', 'full') or not isinstance(reason, str):
        raise ProtocolError('ACCEPT needs mode "reuse" or "full" and a reason')
    identity = accept.get('basis_identity')
    if identity is not None and not isinstance(identity, dict):
        raise ProtocolError('ACCEPT basis_identity must be an object or null')
    policy = accept.get('reuse_policy')
    if policy is not None and not isinstance(policy, str):
        raise ProtocolError('ACCEPT reuse_policy must be a string or null')
    basis_sizes = {}
    for item in accept.get('basis_files', []):
        if (not isinstance(item, list) or len(item) != 2 or type(item[0]) is not int
                or type(item[1]) is not int or not 0 <= item[0] < file_count or item[1] < 0
                or item[0] in basis_sizes):
            raise ProtocolError(f'invalid ACCEPT basis_files entry {item!r}')
        basis_sizes[item[0]] = item[1]
    return mode == 'reuse', reason, policy, identity, basis_sizes


class _Sender:
    def __init__(self, channel, source, identity, block, node_blocks, high_churn_node_fraction,
                 window, key, jobs, hash_backend):
        self.started = time.perf_counter()
        self.channel, self.source, self.identity = channel, Path(source), identity
        self.block, self.node_blocks = block, node_blocks
        self.high_churn = high_churn_node_fraction
        self.window = max(1, window)
        self.key = key
        self.jobs, self.pool, self.hash_backend = jobs, None, hash_backend
        self.pool_start_seconds = 0.0
        self.inbox = Inbox(channel)
        self.mode = self.reason = self.policy = self.basis_identity = self.receiver_stats = None
        self.directory_count = 0
        self.files = dict.fromkeys(('total', 'unchanged', 'changed', 'new', 'size_mismatch'), 0)
        self.pages = dict.fromkeys(('total', 'sent', 'reused', 'sent_bytes'), 0)
        self.nodes = dict.fromkeys(('total', 'matched', 'descended', 'sent_whole'), 0)
        self.high_churn_files = 0
        self.file_count = None
        self.files_digest = FilesDigest()
        self.hash_source = 0.0
        self.protocol_started = self.protocol_seconds = None

    def run(self):
        try:
            return self._run()
        except BaseException as exc:
            failure = exc
            try:
                failure = self._fail(exc)
            finally:
                failure.pgdelta_stats = self.stats('failed', getattr(failure, 'exit_code',
                                                   EXIT_TRANSFER_FAILURE), str(failure))
            if failure is exc:
                raise
            raise failure from exc
        finally:
            if self.pool is not None:
                self.pool.close()

    def _fail(self, exc):
        if isinstance(exc, PeerError):
            return exc
        if isinstance(exc, ChannelClosed):
            self.inbox.settle(JOIN_SECONDS)
            return self.inbox.error if isinstance(self.inbox.error, PeerError) else exc
        if isinstance(exc, PgDeltaError):
            self._send_error(exc.exit_code, str(exc))
        elif isinstance(exc, KeyboardInterrupt):
            self._send_error(EXIT_TRANSFER_FAILURE, 'sender interrupted')
        else:
            self._send_error(EXIT_TRANSFER_FAILURE, f'sender internal error: {exc!r}')
        return exc

    def _send_error(self, exit_code, message):
        try:
            self.channel.send_json(protocol.ERROR, {'exit_code': exit_code, 'message': message})
        except PgDeltaError:
            pass

    def _resolve_block(self):
        identity = self.identity or {}
        identity_block = None if identity.get('untrustworthy') else identity.get('block_size')
        if self.block is None:
            return identity_block or BLOCK
        if identity_block is not None and self.block != identity_block:
            raise InvalidInput(f'block size {self.block} does not match the source cluster '
                               f'block size {identity_block}')
        return self.block

    def _run(self):
        self.block = self._resolve_block()
        protocol.check_parameters(self.block, self.node_blocks, InvalidInput)
        if self.high_churn is not None and not 0 <= self.high_churn <= 1:
            raise InvalidInput('high_churn_node_fraction must be between 0 and 1')
        if type(self.jobs) is not int or self.jobs < 1:
            raise InvalidInput(f'jobs must be a positive integer, not {self.jobs!r}')
        if self.hash_backend not in BACKENDS:
            raise InvalidInput(f'hash backend must be one of {", ".join(BACKENDS)}, not {self.hash_backend!r}')
        inventory = prepare_source(self.source)
        self.directory_count, self.file_count = len(inventory.directories), len(inventory.files)
        key = self.key or new_key()
        self.hasher = Hasher(key)
        started = time.perf_counter()
        try:
            self.pool = HashPool(self.jobs, backend=self.hash_backend, key=key)
        except (OSError, ValueError) as exc:
            raise TransferError(f'cannot start the hashing workers: {exc}') from exc
        self.pool_start_seconds = time.perf_counter() - started
        self.protocol_started = time.perf_counter()
        self.inbox.start()
        self.channel.send_json(protocol.HELLO, {
            'protocol_version': protocol.PROTOCOL_VERSION,
            'block': self.block,
            'node_blocks': self.node_blocks,
            'key': base64.b64encode(key).decode('ascii'),
            'source_identity': self.identity,
            'root_mode': inventory.root_mode,
            'directories': [[entry.path, entry.mode] for entry in inventory.directories],
            'files': [[entry.path, entry.size, entry.mode] for entry in inventory.files],
        })
        accept = self.inbox.wait(lambda: self.inbox.accept, 'ACCEPT')
        self.reuse, self.reason, self.policy, self.basis_identity, self.basis_sizes = _parse_accept(
            accept, len(inventory.files))
        self.mode = 'reuse' if self.reuse else 'full'
        self.max_run = max(1, RUN_BYTES // self.block)
        self.max_request = protocol.max_nodes_per_request(self.node_blocks)
        pending = deque()
        for index, entry in enumerate(inventory.files):
            self.inbox.check()
            pending.append(self._plan(index, entry))
            while len(pending) > self.window:
                self._finish(pending.popleft())
        while pending:
            self._finish(pending.popleft())
        self.channel.send_json(protocol.COMPLETE, {'files': len(inventory.files),
                                                   'directories': self.directory_count})
        try:
            final = self.inbox.wait(lambda: self.inbox.complete, 'COMPLETE')
        except ChannelClosed as exc:
            raise ChannelClosed(f'{exc}. Every file was sent, so the receiver may have published the output '
                                f'anyway: check its log and the receipt beside the output '
                                f'(<output>{RECEIPT_SUFFIX}), whose files_digest should equal this run\'s '
                                f'statistics') from exc
        self.protocol_seconds = time.perf_counter() - self.protocol_started
        self.inbox.join(JOIN_SECONDS)
        if final.get('published') is not True:
            raise ProtocolError('receiver finished without confirming publication')
        receiver = final.get('receiver')
        self.receiver_stats = receiver if isinstance(receiver, dict) else None
        return self.stats('complete', 0, None)

    def _plan(self, index, entry):
        started = time.perf_counter()
        try:
            tree = hash_file(self.source / entry.path, None, self.block, self.node_blocks,
                             hasher=self.hasher, pool=self.pool)
        except OSError as exc:
            raise TransferError(f'cannot read source file {entry.path}: {exc}') from exc
        self.hash_source += time.perf_counter() - started
        if tree.size != entry.size:
            raise VerificationError(f'source file {entry.path} changed size during the run '
                                    f'({entry.size} -> {tree.size} bytes); S must be immutable')
        job = _Job(index, entry.path, tree, self.basis_sizes.get(index))
        if job.basis_size is None or not self.reuse:
            job.plan.whole = list(range(tree.node_count))
            return job
        size, nodes = self.inbox.wait(lambda: self.inbox.basis.pop(index, None),
                                      f'basis digests of {entry.path}')
        if size != job.basis_size or len(nodes) != node_count(size, self.block, self.node_blocks) * DIGEST_SIZE:
            raise ProtocolError(f'BASIS_NODES for {entry.path} disagrees with the announced basis size')
        job.plan = matching.classify_nodes(tree, size, nodes, self.high_churn)
        for start in range(0, len(job.plan.descend), self.max_request):
            chunk = job.plan.descend[start:start + self.max_request]
            self.channel.send(protocol.LEAF_REQUEST, protocol.encode_leaf_request(index, chunk))
        return job

    def _finish(self, job):
        tree, plan = job.tree, job.plan
        pages = []
        for node in plan.descend:
            blob = self.inbox.wait(lambda: self.inbox.leaves.pop((job.index, node), None),
                                   f'leaf digests of {job.path}')
            expected = matching.basis_leaf_count(node, job.basis_size, self.block, self.node_blocks)
            if len(blob) != expected * DIGEST_SIZE:
                raise ProtocolError(f'LEAF_DIGESTS for {job.path} node {node} has the wrong length')
            pages += matching.changed_pages(tree, node, job.basis_size, blob)
        for node in plan.whole:
            pages += matching.node_pages(tree, node)
        pages.sort()
        sent_bytes = self._send_pages(job, pages) if pages else 0
        self.channel.send(protocol.FILE_DONE, protocol.encode_file_done(job.index, tree.size, tree.digest))
        self.files_digest.add(job.path, tree.size, tree.digest)
        self._count(job, len(pages), sent_bytes)

    def _send_pages(self, job, pages):
        sent = 0
        try:
            fd = open_regular(self.source / job.path)
        except OSError as exc:
            raise TransferError(f'cannot read source file {job.path}: {exc}') from exc
        try:
            for first, count in matching.runs(pages, self.max_run):
                offset = first * self.block
                length = min(count * self.block, job.tree.size - offset)
                try:
                    data = pread_exact(fd, length, offset)
                except OSError as exc:
                    raise TransferError(f'cannot read source file {job.path}: {exc}') from exc
                self.inbox.check()
                self.channel.send(protocol.PAGE_DATA, protocol.page_data_head(job.index, first), data)
                sent += length
        finally:
            os.close(fd)
        return sent

    def _count(self, job, pages_sent, sent_bytes):
        tree, plan, files = job.tree, job.plan, self.files
        files['total'] += 1
        if job.basis_size is None:
            files['new'] += 1
        else:
            if job.basis_size != tree.size:
                files['size_mismatch'] += 1
            unchanged = self.reuse and pages_sent == 0 and job.basis_size == tree.size
            files['unchanged' if unchanged else 'changed'] += 1
        self.pages['total'] += tree.page_count
        self.pages['sent'] += pages_sent
        self.pages['reused'] += tree.page_count - pages_sent
        self.pages['sent_bytes'] += sent_bytes
        self.nodes['total'] += tree.node_count
        self.nodes['matched'] += len(plan.matched)
        self.nodes['descended'] += len(plan.descend)
        self.nodes['sent_whole'] += len(plan.whole)
        self.high_churn_files += plan.high_churn

    def switches(self):
        """The sender's engine settings in effect."""
        return {'jobs': self.jobs,
                'hash_backend': self.pool.backend if self.pool is not None else None}

    def stats(self, status, exit_code, error):
        receiver = self.receiver_stats or {}
        now = time.perf_counter()
        protocol_seconds = self.protocol_seconds
        if protocol_seconds is None and self.protocol_started is not None:
            protocol_seconds = now - self.protocol_started
        timings = {
            'hash_source': self.hash_source,
            'hash_basis': receiver.get('hash_basis_seconds'),
            'protocol': protocol_seconds,
            'rebuild': receiver.get('rebuild_seconds'),  # writing O
            'copy_basis': receiver.get('copy_seconds'),  # of which: copying from B
            'fsync': receiver.get('fsync_seconds'),  # of which: fsync of O's files
            'verify': receiver.get('verify_seconds'),  # re-hashing O (overlaps writing it when pipelined)
            'verify_wait': receiver.get('verify_wait_seconds'),  # of which the receiver's main loop waited
            'pool_start': self.pool_start_seconds,
            # until the first, and until every one, of the workers had started (null until then)
            'pool_first_ready': None if self.pool is None else self.pool.first_ready_seconds,
            'pool_ready': None if self.pool is None else self.pool.ready_seconds,
            'receiver_pool_first_ready': receiver.get('pool_first_ready_seconds'),
            'receiver_pool_ready': receiver.get('pool_ready_seconds'),
            'total': now - self.started,
        }
        return {
            'schema_version': STATS_SCHEMA_VERSION,
            'tool': 'pgdelta',
            'tool_version': __version__,
            'protocol_version': protocol.PROTOCOL_VERSION,
            'status': status,
            'exit_code': exit_code,
            'error': error,
            'mode': self.mode,
            'gate_reason': self.reason,
            'reuse_policy': self.policy,
            'block': self.block,
            'node_blocks': self.node_blocks,
            'high_churn_node_fraction': self.high_churn,
            'window': self.window,
            'jobs': self.jobs,
            'hash_backend': self.switches()['hash_backend'],
            # "spawn": the workers are this process's children, so wait4/RUSAGE_CHILDREN count them.
            'worker_start_method': None if self.pool is None else self.pool.start_method,
            # Both ends' engine settings; the receiver's are null until its final statistics arrive.
            'switches': {'sender': self.switches(), 'receiver': receiver.get('switches')},
            'source_identity': self.identity,
            'basis_identity': self.basis_identity,
            'files': dict(self.files),
            'directories': self.directory_count,
            'files_digest': (self.files_digest.hexdigest()
                             if self.file_count is not None and self.files_digest.count == self.file_count
                             else None),
            'pages': dict(self.pages),
            'nodes': dict(self.nodes),
            'high_churn_files': self.high_churn_files,
            'bytes_source_to_dest': self.channel.sent.summary(),
            'bytes_dest_to_source': self.channel.received.summary(),
            'timings_seconds': {key: None if value is None else round(value, 6)
                                for key, value in timings.items()},
            'receiver': self.receiver_stats,
        }


def run_sender(channel, source, *, identity=None, block=None, node_blocks=NODE_BLOCKS,
               high_churn_node_fraction=None, window=DEFAULT_WINDOW, key=None, jobs=DEFAULT_JOBS,
               hash_backend=DEFAULT_BACKEND):
    """Run one session as the sender and return the statistics dict.

    On failure the raised PgDeltaError carries partial statistics in
    `pgdelta_stats`. `block=None` takes the block size from the source
    identity, or 8192 when the identity is unknown. `jobs` worker processes
    (threads with hash_backend="thread") hash S; jobs=1 hashes in this
    thread. The digests, and so everything sent, are the same for any value.
    With jobs > 1, a program calling this must guard its main module
    (`if __name__ == '__main__':`), as multiprocessing's spawn requires.
    """
    return _Sender(channel, source, identity, block, node_blocks, high_churn_node_fraction,
                   window, key, jobs, hash_backend).run()
