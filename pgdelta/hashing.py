"""Keyed BLAKE2b digests for pages (leaves), 1 MiB nodes and whole files.

The key is random per session, so page contents chosen by a database user
cannot be crafted in advance to collide. Each level has its own BLAKE2b
personalization, so a leaf digest can never be confused with a node digest.
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


def new_key():
    return os.urandom(KEY_SIZE)


class Hasher:
    """Digests for one session key. Copies pre-keyed states instead of re-keying per page."""

    def __init__(self, key):
        if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_SIZE:
            raise ValueError(f'hash key must be {KEY_SIZE} bytes')
        self.key = bytes(key)  # a process hash pool checks that its workers use this key
        self._leaf = hashlib.blake2b(key=key, person=LEAF_PERSON, digest_size=DIGEST_SIZE)
        self._node = hashlib.blake2b(key=key, person=NODE_PERSON, digest_size=DIGEST_SIZE)
        self._file = hashlib.blake2b(key=key, person=FILE_PERSON, digest_size=DIGEST_SIZE)

    def leaf(self, page):
        state = self._leaf.copy()
        state.update(page)
        return state.digest()

    def node(self, leaf_digests):
        """Digest of the concatenated leaf digests of one node."""
        state = self._node.copy()
        state.update(leaf_digests)
        return state.digest()

    def file(self, size, node_digests):
        """Digest of the file size (8 bytes, big-endian) and the concatenated node digests."""
        state = self._file.copy()
        state.update(size.to_bytes(8, 'big'))
        state.update(node_digests)
        return state.digest()


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
