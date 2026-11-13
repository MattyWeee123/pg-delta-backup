"""Progress events for watching a run live: one JSON object per line, flushed as it's written.

    {"t": 0.0, "kind": "start", "method": "pgdelta", "total_bytes": ..., "files": ..., "chunk_size": ...}
    {"t": 0.1, "kind": "progress", "bytes_read": ..., "bytes_sent": ..., "chunks_same": ..., ...}
    {"t": 3.9, "kind": "done", "ok": true, "elapsed": 3.9, ...}

`t` is seconds since the run started. The visual tool (python -m viz) reads these files as they grow.
Progress is written at most every `interval` seconds, so the cost is one clock read per chunk.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path


class EventLog:
    def __init__(self, path: Path | None, method: str = 'pgdelta', interval: float = 0.1):
        # '-' writes to standard output (the cloud runner streams it back over ssh)
        self._owned = bool(path) and str(path) != '-'
        self.file = (sys.stdout if str(path) == '-' else open(path, 'w')) if path else None
        self.method = method
        self.interval = interval
        self.started = time.perf_counter()
        self._next = 0.0

    def emit(self, kind: str, **fields) -> None:
        if self.file is None:
            return
        record = {'t': round(time.perf_counter() - self.started, 4), 'kind': kind, 'method': self.method, **fields}
        self.file.write(json.dumps(record, separators=(',', ':')) + '\n')
        self.file.flush()

    def due(self) -> bool:
        """True at most once per interval: whether a progress event should be written now."""
        if self.file is None:
            return False
        now = time.perf_counter()
        if now < self._next:
            return False
        self._next = now + self.interval
        return True

    def close(self) -> None:
        if self.file is not None and self._owned:
            self.file.close()
        self.file = None
