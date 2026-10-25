"""Keyed digests for pages (leaves), 1 MiB nodes and whole files: BLAKE2b, or BLAKE3 if asked for.

The key is random per session, so page contents chosen by a database user
cannot be crafted in advance to collide. Each level has its own BLAKE2b
personalization, so a leaf digest can never be confused with a node digest.
BLAKE3 (EXP-29: 4.8 times BLAKE2b's pages per core on the x86 VMs) has no
personalization: each level gets its own key instead, derived from the
session key in BLAKE3's key-derivation mode. It needs the blake3 package.
"""
from __future__ import annotations

import hashlib
import os

BLOCK = 8192
NODE_BLOCKS = 128
DIGEST_SIZE = 16
KEY_SIZE = 32
LEAF_PERSON = b'pgdelta-leaf'
NODE_PERSON = b'pgdelta-node'
FILE_PERSON = b'pgdelta-file'
FILES_PERSON = b'pgdelta-files'
ALGORITHMS = ('blake2b', 'blake3')
DEFAULT_ALGORITHM = 'blake2b'
BLAKE3_CONTEXTS = ('pgdelta 2026-10-25 leaf digest key', 'pgdelta 2026-10-25 node digest key',
                   'pgdelta 2026-10-25 file digest key')


def blake3_module():
    """The blake3 package, or ImportError saying how to get it."""
    try:
        import blake3
    except ImportError as exc:
        raise ImportError('the blake3 hash needs the blake3 package (pip install blake3)') from exc
    return blake3


def new_key():
    return os.urandom(KEY_SIZE)


class Hasher:
    """Digests for one session key. Copies pre-keyed states instead of re-keying per page."""

    def __init__(self, key, algorithm=DEFAULT_ALGORITHM):
        if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_SIZE:
            raise ValueError(f'hash key must be {KEY_SIZE} bytes')
        if algorithm not in ALGORITHMS:
            raise ValueError(f'hash algorithm must be one of {", ".join(ALGORITHMS)}, not {algorithm!r}')
        # A process hash pool checks that its workers use this key and algorithm.
        self.key, self.algorithm = bytes(key), algorithm
        self._blake3 = algorithm == 'blake3'
        if self._blake3:
            blake3 = blake3_module()
            self._leaf, self._node, self._file = (
                blake3.blake3(key=blake3.blake3(self.key, derive_key_context=context).digest())
                for context in BLAKE3_CONTEXTS)
        else:
            self._leaf = hashlib.blake2b(key=key, person=LEAF_PERSON, digest_size=DIGEST_SIZE)
            self._node = hashlib.blake2b(key=key, person=NODE_PERSON, digest_size=DIGEST_SIZE)
            self._file = hashlib.blake2b(key=key, person=FILE_PERSON, digest_size=DIGEST_SIZE)

    def _digest(self, state):
        return state.digest(length=DIGEST_SIZE) if self._blake3 else state.digest()

    def leaf(self, page):
        state = self._leaf.copy()
        state.update(page)
        return self._digest(state)

    def node(self, leaf_digests):
        """Digest of the concatenated leaf digests of one node."""
        state = self._node.copy()
        state.update(leaf_digests)
        return self._digest(state)

    def file(self, size, node_digests):
        """Digest of the file size (8 bytes, big-endian) and the concatenated node digests."""
        state = self._file.copy()
        state.update(size.to_bytes(8, 'big'))
        state.update(node_digests)
        return self._digest(state)


def leaf_digest(page, key):
    return Hasher(key).leaf(page)


def node_digest(leaf_digests, key):
    return Hasher(key).node(leaf_digests)


def file_digest(size, node_digests, key):
    return Hasher(key).file(size, node_digests)


class FilesDigest:
    """Combined digest of a whole inventory, for matching a sender's statistics to a receipt.

    Unkeyed BLAKE2b-256 over, for every file in inventory order: the path's
    UTF-8 length (4 bytes, big-endian) and bytes, the size (8 bytes) and the
    16-byte keyed file digest. The session key is not kept, so the value
    names one run's verified content; it cannot be recomputed from O later.
    """

    def __init__(self):
        self._state = hashlib.blake2b(digest_size=32, person=FILES_PERSON)
        self.count = 0

    def add(self, path, size, digest):
        encoded = path.encode('utf-8', 'surrogatepass')
        self._state.update(len(encoded).to_bytes(4, 'big') + encoded + size.to_bytes(8, 'big') + digest)
        self.count += 1

    def hexdigest(self):
        return self._state.hexdigest()
