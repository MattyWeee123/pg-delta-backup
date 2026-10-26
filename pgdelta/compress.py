"""zstd for the pages sent (--compress zstd): negotiated in HELLO and ACCEPT, off unless the sender asks.

EXP-37 measured zstd at level 3 on the pages pgdelta sends for scattered updates: 2.83 times fewer bytes, at
548 MiB/s per core. That pays where the link is slower than one core compresses (DR round trips), and costs where
it is faster (a fast LAN), so it is opt-in, like BBR. Either the standard library's compression.zstd (Python 3.14)
or the zstandard package serves; both ends need one, and a receiver without either refuses nothing: it accepts
without compression, and the sender sends plain PAGE_DATA.
"""
from __future__ import annotations

import os
import threading

ALGORITHMS = ('zstd',)
LEVEL = 3
# EXP-49: a pool task costs about 12 µs whatever it holds, so one task per 2.5-page run capped both pools at
# 1.5-1.7 GB/s on the Mac. A task takes runs up to this many bytes of pages instead (the receiver: a quarter of it
# in frames, about as many pages at zstd's 2.8x); each run is still its own frame. 0 means one run per task,
# EXP-47's path (PGDELTA_ZSTD_TASK_BYTES, for EXP-50's comparison).
TASK_BYTES = max(0, int(os.environ.get('PGDELTA_ZSTD_TASK_BYTES', 1 << 20)))


def _backend():
    """('stdlib' | 'zstandard', module), or (None, None) when neither imports."""
    try:
        from compression import zstd  # Python 3.14
        return 'stdlib', zstd
    except ImportError:
        pass
    try:
        import zstandard
        return 'zstandard', zstandard
    except ImportError:
        return None, None


def usable(algorithm='zstd'):
    return algorithm in ALGORITHMS and _backend()[0] is not None


class Codec:
    """One end's zstd: compress() a run of pages, decompress() one back within a size limit."""

    def __init__(self, level=LEVEL):
        self.backend, module = _backend()
        if module is None:
            raise ImportError('zstd needs Python 3.14 (compression.zstd) or the zstandard package')
        self.level, self._module = level, module
        if self.backend == 'zstandard':
            # A zstandard compressor serves one thread at a time, so each compressing thread has its own
            # (the sender compresses on a pool, EXP-46). The frame records its content size, so decompress()
            # can check it before allocating.
            self._local = threading.local()

    def compress(self, data):
        if self.backend == 'stdlib':
            return self._module.compress(bytes(data), level=self.level)
        compressor = getattr(self._local, 'compressor', None)
        if compressor is None:
            compressor = self._local.compressor = self._module.ZstdCompressor(level=self.level,
                                                                              write_content_size=True)
        return compressor.compress(bytes(data))

    def decompress(self, data, limit):
        """The bytes of one compressed frame; ValueError if malformed, trailed by more data, or over `limit`."""
        try:
            if self.backend == 'stdlib':
                decompressor = self._module.ZstdDecompressor()
                out = decompressor.decompress(bytes(data), max_length=limit + 1)
                if not decompressor.eof or decompressor.unused_data or len(out) > limit:
                    raise ValueError('not one complete frame within the limit')
                return out
            # The declared size first, so nothing over the limit is allocated; then one frame, nothing after it.
            size = self._module.frame_content_size(bytes(data))
            if size < 0 or size > limit:
                raise ValueError('no content size, or over the limit')
            decompressor = getattr(self._local, 'decompressor', None)
            if decompressor is None:  # one per thread, as the compressors (the receiver decompresses on a pool)
                decompressor = self._local.decompressor = self._module.ZstdDecompressor()
            stream = decompressor.decompressobj()
            out = stream.decompress(bytes(data))
            if not getattr(stream, 'eof', True) or getattr(stream, 'unused_data', b'') or len(out) != size:
                raise ValueError('not one complete frame of its declared size')
            return out
        except ValueError:
            raise
        except Exception as exc:  # each backend's own error type
            raise ValueError(str(exc)) from exc
