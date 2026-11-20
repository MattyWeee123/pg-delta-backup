"""How the benchmark runs pgdelta: its two arms, with the settings phase 1 found best.

- `pgdelta` builds a new copy O. It hashes ahead of sending (`--hash-ahead 64G --read-ahead 8`), syncs on
  a worker (`--fsync worker`), and re-reads O to verify it (verifying as written needs `--in-place`).
- `pgdelta-inplace` patches a fresh copy of B in place and verifies as it writes
  (`--verify-scope as-written`): EXP-92's `hashAVw` (docs/PAST-RESULTS.md).
Both hash with BLAKE2b on 4 worker processes a side (`--jobs 4`), start the receiver cold (`--prefetch-basis off`,
so all of its reading of B is inside the timed window), read the stopped source directly
(`--source-kind datadir`, which leaves out what pg_basebackup leaves out, old WAL among it), and write
progress events for python -m viz (`--events`).
"""
from __future__ import annotations

import json
from pathlib import Path

METHODS = ('pgdelta', 'pgdelta-inplace')
LABELS = {'pgdelta': 'pgdelta (new copy)', 'pgdelta-inplace': 'pgdelta (in place)'}
PORT = 7000


def sender_args(controldata: str) -> list[str]:
    return ['--source-kind', 'datadir', '--pg-controldata', controldata, '--jobs', '4', '--hash', 'blake2b',
            '--hash-ahead', '64G', '--read-ahead', '8']


def receiver_args(method: str, controldata: str) -> list[str]:
    # --prefetch-basis off: by default the receiver reads B into memory while it waits for the sender, which
    # puts that read before the clock starts (the sender's start) and flatters pgdelta against pg_basebackup.
    # Found Nov 20: in every such lane the receiver had read all of B (354 MB) before t0.
    args = ['--basis-pg-controldata', controldata, '--jobs', '4', '--fsync', 'worker', '--prefetch-basis', 'off']
    return args + (['--in-place', '--verify-scope', 'as-written'] if method == 'pgdelta-inplace' else [])


def local_args(method: str, source, basis, output, controldata: str) -> list[str]:
    """`python -m pgdelta local` arguments for one arm (basis is patched in place for the in-place arm)."""
    target = [] if method == 'pgdelta-inplace' else ['--output', str(output)]
    # local starts its receiver with the sender (no wait to read ahead in), and takes neither flag twice
    receiver = [a for a in receiver_args(method, controldata) if a not in ('--jobs', '4', '--prefetch-basis', 'off')]
    return ['local', '--source', str(source), '--basis', str(basis), *target, *sender_args(controldata), *receiver]


def facts(stats_path: Path, elapsed: float, total_bytes: int) -> dict:
    """A lane's facts from pgdelta's --stats-json, timed by the caller."""
    report = json.loads(Path(stats_path).read_text())
    pages = report.get('pages') or {}
    sent = (report.get('bytes_source_to_dest') or {}).get('total', 0)
    return {'elapsed': round(elapsed, 4), 'bytes_sent': sent, 'total_bytes': total_bytes,
            'pages_sent': pages.get('sent'), 'pages_reused': pages.get('reused'),
            'ok': report.get('exit_code', 0) == 0 and not report.get('error')}
