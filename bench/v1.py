"""The archived engine (tag v1-research-final): the first phase's pipelined pgdelta, raced against today's.

Two arms, both with the first phase's best settings, EXP-92's `hashAVw`
(docs/experiments/EXP-92-confirm-syncs-on-worker.md on that tag), except the hash: BLAKE2b, today's, so
the two engines differ in design only:
- `pgdelta-v1` builds a new copy O, as today's engine does. It hashes ahead of sending
  (`--hash-ahead 64G --read-ahead 8`) and syncs on a worker (`--fsync worker`). It can't verify as it
  writes (that needs `--in-place`), so it re-reads O, as today's engine does.
- `pgdelta-v1-inplace` patches a fresh copy of B in place, as EXP-92 ran, and verifies as it writes
  (`--verify-scope as-written`).
Both use 4 worker processes a side (`--jobs 4`), as EXP-92 did, and read the stopped source directly
(`--source-kind datadir`), leaving out what pg_basebackup leaves out (old WAL among it).
"""
from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REF = 'v1-research-final'
METHODS = ('pgdelta-v1', 'pgdelta-v1-inplace')
LABELS = {'pgdelta-v1': 'pgdelta v1, archived (new copy)', 'pgdelta-v1-inplace': 'pgdelta v1, archived (in place)'}
PORT = 7001


def tarball() -> bytes:
    """The archived engine's package, as a tar of pgdelta/ from git."""
    return subprocess.run(['git', 'archive', REF, 'pgdelta'], cwd=REPO, capture_output=True, check=True).stdout


def extract(dest: Path) -> Path:
    """Unpack the archived engine into dest/pgdelta (once). Run it with dest as the working directory."""
    dest = Path(dest)
    if not (dest / 'pgdelta' / '__main__.py').exists():
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(tarball())) as tar:
            tar.extractall(dest)
    return dest


def sender_args(controldata: str) -> list[str]:
    return ['--source-kind', 'datadir', '--pg-controldata', controldata, '--jobs', '4', '--hash', 'blake2b',
            '--hash-ahead', '64G', '--read-ahead', '8']


def receiver_args(method: str, controldata: str) -> list[str]:
    args = ['--basis-pg-controldata', controldata, '--jobs', '4', '--fsync', 'worker']
    return args + (['--in-place', '--verify-scope', 'as-written'] if method == 'pgdelta-v1-inplace' else [])


def local_args(method: str, source, basis, output, controldata: str) -> list[str]:
    """`python -m pgdelta local` arguments for one arm (basis is patched in place for the in-place arm)."""
    target = [] if method == 'pgdelta-v1-inplace' else ['--output', str(output)]
    receiver = receiver_args(method, controldata)[:2] + receiver_args(method, controldata)[4:]  # --jobs once
    return ['local', '--source', str(source), '--basis', str(basis), *target, *sender_args(controldata), *receiver]


def facts(stats_path: Path, elapsed: float, total_bytes: int) -> dict:
    """A lane's facts from the archived engine's --stats-json, timed by the caller."""
    report = json.loads(Path(stats_path).read_text())
    sent = (report.get('bytes_source_to_dest') or {}).get('total', 0)
    return {'elapsed': round(elapsed, 4), 'bytes_sent': sent, 'total_bytes': total_bytes,
            'ok': report.get('exit_code', 0) == 0 and not report.get('error'), 'engine': REF}
