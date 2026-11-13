"""Keyed digests: one for each chunk, one for each whole file.

The key is random for every session, so nobody can prepare, in advance, a chunk whose digest equals
another chunk's. The two kinds use different personalizations, so a chunk digest can't pass for a
file digest. BLAKE2b ships with Python (hashlib); trying BLAKE3 or SHA-256 is a roadmap issue.
"""
from __future__ import annotations

import hashlib
import os

DIGEST_SIZE = 16
KEY_SIZE = 32
_CHUNK = b'pgdelta-chunk'
_FILE = b'pgdelta-file'


def new_key() -> bytes:
    return os.urandom(KEY_SIZE)


class Hasher:
    def __init__(self, key: bytes):
        if len(key) != KEY_SIZE:
            raise ValueError('the session key must be 32 bytes')
        self._chunk = hashlib.blake2b(key=key, person=_CHUNK, digest_size=DIGEST_SIZE)
        self._file = hashlib.blake2b(key=key, person=_FILE, digest_size=DIGEST_SIZE)

    def chunk(self, data: bytes) -> bytes:
        state = self._chunk.copy()
        state.update(data)
        return state.digest()

    def file(self):
        """A fresh whole-file digest: feed it the file's bytes in order with update(), then digest()."""
        return self._file.copy()
