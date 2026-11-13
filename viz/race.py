"""python -m viz race: a laptop race, pg_basebackup vs pgdelta, served live. A shortcut for
`python -m bench run --where laptop` (bench/laptop.py does the work)."""
from __future__ import annotations

from pathlib import Path

from bench.config import load
from bench.lanes import PROGRESS  # noqa: F401  (kept for callers that parse pg_basebackup's progress)
from bench.laptop import pg_bin  # noqa: F401
from bench.runner import execute


def race(name: str, root: Path, work: Path, scale: int, change: str, pgbin: Path | None = None, log=print) -> Path:
    cfg = load({'where': 'laptop', 'name': name, 'scale': scale, 'change': change})
    return execute(cfg, root, work=work, pgbin=str(pgbin) if pgbin else None, log=log)
