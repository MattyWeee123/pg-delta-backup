"""Shared fixtures for the pgdelta tests: synthetic trees, snapshots and an in-process transfer."""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import sys
import threading

from pgdelta.protocol import Channel
from pgdelta.receiver import run_receiver, staging_pattern
from pgdelta.sender import run_sender

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE = 8192


def write_tree(root, files, directories=(), modes=None):
    """files: {relative path: bytes}; directories: extra (possibly empty) directories."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for path in directories:
        (root / path).mkdir(parents=True, exist_ok=True)
    for path, data in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)
    for path, mode in (modes or {}).items():
        os.chmod(root / path, mode)
    return root


def snapshot(root):
    """Map every path under root to (kind, permission bits, sha256 of content)."""
    root = Path(root)
    result = {'': ('dir', stat.S_IMODE(os.stat(root).st_mode), None)}
    for current, directories, files in os.walk(root):
        for name in directories + files:
            path = Path(current) / name
            info = os.lstat(path)
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(info.st_mode):
                result[relative] = ('dir', stat.S_IMODE(info.st_mode), None)
            elif stat.S_ISLNK(info.st_mode):
                result[relative] = ('link', None, os.readlink(path))
            else:
                result[relative] = ('file', stat.S_IMODE(info.st_mode),
                                    hashlib.sha256(path.read_bytes()).hexdigest())
    return result


def differing_pages(source, basis, block=PAGE):
    """Ground truth: pages of S that are absent from B or differ in bytes or length at the same offset."""
    source, basis, count = Path(source), Path(basis), 0
    for path in sorted(p for p in source.rglob('*') if p.is_file()):
        data = path.read_bytes()
        old_path = basis / path.relative_to(source)
        old = old_path.read_bytes() if old_path.is_file() else None
        for offset in range(0, len(data), block):
            if old is None or old[offset:offset + block] != data[offset:offset + block]:
                count += 1
    return count


def total_pages(source, block=PAGE):
    return sum(-(-p.stat().st_size // block) for p in Path(source).rglob('*') if p.is_file())


def staging_dirs(output):
    output = Path(output)
    return list(output.parent.glob(staging_pattern(output)))


@dataclass
class Outcome:
    sender: object = None
    receiver: object = None
    sender_error: BaseException = None
    receiver_error: BaseException = None


def channel_pair(sender_class=Channel, receiver_class=Channel):
    to_receiver_r, to_receiver_w = os.pipe()
    to_sender_r, to_sender_w = os.pipe()
    sender = sender_class(os.fdopen(to_sender_r, 'rb'), os.fdopen(to_receiver_w, 'wb'))
    receiver = receiver_class(os.fdopen(to_receiver_r, 'rb'), os.fdopen(to_sender_w, 'wb'))
    return sender, receiver


def run_receiver_thread(channel, outcome, basis, output, **kwargs):
    def receive():
        try:
            outcome.receiver = run_receiver(channel, basis, output, **kwargs)
        except BaseException as exc:
            outcome.receiver_error = exc
        finally:
            channel.close()
    thread = threading.Thread(target=receive, name='test-receiver')
    thread.start()
    return thread


def transfer(source, basis, output, *, source_identity=None, basis_identity=None,
             reuse_any_basis=True, sender_class=Channel, node_blocks=4, receiver_jobs=None,
             receiver_options=None, **sender_kwargs):
    """Run sender and receiver in one process over a pair of pipes.

    reuse_any_basis defaults to True so synthetic trees (no identity) are compared page by page.
    receiver_jobs, when given, is the receiver's hashing worker count (sender_kwargs may hold jobs);
    receiver_options holds other run_receiver keywords (hash_backend, rebuild, verify).
    """
    send_channel, receive_channel = channel_pair(sender_class)
    outcome = Outcome()
    receiver_kwargs = dict(receiver_options or {})
    if receiver_jobs is not None:
        receiver_kwargs['jobs'] = receiver_jobs
    thread = run_receiver_thread(receive_channel, outcome, basis, output, identity=basis_identity,
                                 reuse_any_basis=reuse_any_basis, **receiver_kwargs)
    try:
        outcome.sender = run_sender(send_channel, source, identity=source_identity,
                                    node_blocks=node_blocks, **sender_kwargs)
    except BaseException as exc:
        outcome.sender_error = exc
    finally:
        send_channel.close()
    thread.join(60)
    if thread.is_alive():
        raise AssertionError('receiver thread did not finish')
    return outcome


VOLATILE_RECEIVER_KEYS = frozenset((
    'hash_basis_seconds', 'rebuild_seconds', 'copy_seconds', 'fsync_seconds', 'verify_seconds',
    'verify_wait_seconds', 'pool_start_seconds', 'pool_first_ready_seconds', 'pool_ready_seconds',
    'worker_start_method', 'jobs', 'switches', 'output', 'receipt',
    'acknowledged', 'acknowledgement_error',
    # where files are fsynced (pgdelta.flush): the switch, and what it did and cost
    'fsync', 'fsync_effective', 'fsync_mode', 'writeback_calls', 'fsync_wait_seconds', 'writeback_seconds',
    # how O was built, which depends on the rebuild switch, unlike pages_copied and bytes_copied
    'basis_copy_bytes', 'basis_copies',
    # EXP-13's protocol timers (pgdelta.waits), which vary like timings
    'protocol_timers'))
VOLATILE_SENDER_KEYS = frozenset(('timings_seconds', 'jobs', 'hash_backend', 'worker_start_method', 'switches',
                                  'receiver', 'protocol_timers'))


def comparable(outcome):
    """The statistics of one session without what may legitimately differ: timings, switches, paths.

    The receiver's final COMPLETE frame carries its statistics, timings
    included, so its length can differ by a byte or two between any two
    runs; it is taken out of the destination-to-source counts and only
    its frame count is compared.
    """
    def drop(stats, keys):
        return {key: value for key, value in stats.items() if key not in keys}
    sender = drop(outcome.sender, VOLATILE_SENDER_KEYS)
    down = copy.deepcopy(sender['bytes_dest_to_source'])
    final = down['by_message_type']['COMPLETE']
    down['total'] -= final['bytes']
    down['control'] -= final['bytes']
    final['bytes'] = None
    sender['bytes_dest_to_source'] = down
    return (sender, drop(outcome.sender['receiver'], VOLATILE_RECEIVER_KEYS),
            drop(outcome.receiver, VOLATILE_RECEIVER_KEYS))


def pgdelta_threads():
    """Names of this process's live pgdelta threads (pool workers, basis, verifier)."""
    return sorted(thread.name for thread in threading.enumerate() if thread.name.startswith('pgdelta-'))


def python_command(*args):
    return [sys.executable, '-m', 'pgdelta', *map(str, args)]


def subprocess_env():
    env = dict(os.environ)
    env['PYTHONPATH'] = str(REPO_ROOT) + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    return env
