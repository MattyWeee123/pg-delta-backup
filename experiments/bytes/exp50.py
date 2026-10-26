"""EXP-50 (lean, local): do larger pool tasks lower zstd's floor? (docs/experiments/EXP-50-zstd-batched-tasks.md)

    .venv/bin/python experiments/bytes/exp50.py --out results/bytes/exp50 [--scale 70] [--pg-bin DIR] [--work DIR]

S is EXP-37's template (pgbench with hex filler) after EXP-37's uniform update; B is the template. Each run
refreshes a fresh APFS clone of B in place with `pgdelta local --source-kind datadir --jobs 4`:
- plain: no compression;
- zstd_per_run: --compress zstd with PGDELTA_ZSTD_TASK_BYTES=0 (one run per pool task, EXP-47's path);
- zstd_batched: --compress zstd with PGDELTA_ZSTD_TASK_BYTES=1048576 (tasks of up to 1 MiB of pages).
One untimed warm-up run (plain), then 3 repeats of all three arms in a rotated order. One JSON object per run goes
to OUT/runs.jsonl, with pgdelta's own statistics.

EXP-51 (docs/experiments/EXP-51-batched-second-session.md) reruns it as a second session with --rotation 1 (each
repeat starts one arm further on) and --compare-bytes: after every run, each file pgdelta sends (its own selection
of S, pgdelta.datadir.select) is compared by SHA-256 with the refreshed copy's, and once, before any run, with B's
(the negative control: B must differ).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import exp37  # noqa: E402  (EXP-37's template, scenario and helpers)
sys.path.insert(0, str(ROOT))
from pgdelta import datadir  # noqa: E402

ARMS = ('plain', 'zstd_per_run', 'zstd_batched')
REPEATS = 3
JOBS = 4


def order(repeat, rotation=0):
    """Repeat 0: plain, per run, batched; each later repeat starts one arm further on (and --rotation more)."""
    return [ARMS[(repeat + rotation + i) % len(ARMS)] for i in range(len(ARMS))]


def expected_files(a, s):
    """{path: SHA-256} of what pgdelta should leave for each file it sends: the bytes of S it reads."""
    selection = datadir.select(s, str(Path(a.pg_bin) / 'pg_controldata'))
    return {f.path: hashlib.sha256((s / selection.source_path(f.path)).read_bytes()).hexdigest()
            for f in selection.inventory.files}


def compare(root, expected):
    """How many of the expected files are missing from `root` or differ from it, and up to 5 of their paths."""
    bad = [path for path, digest in expected.items()
           if not (root / path).is_file() or hashlib.sha256((root / path).read_bytes()).hexdigest() != digest]
    return {'files_checked': len(expected), 'files_differing': len(bad), 'examples': bad[:5]}


def one_run(a, work, s, b0, arm, label, expected=None):
    basis, stats_path = work / 'B-run', work / f'{label}.stats.json'
    shutil.rmtree(basis, ignore_errors=True)
    exp37.run(['cp', '-c', '-R', str(b0), str(basis)])
    env = dict(os.environ)
    env.pop('PGDELTA_ZSTD_TASK_BYTES', None)
    if arm == 'zstd_per_run':
        env['PGDELTA_ZSTD_TASK_BYTES'] = '0'
    elif arm == 'zstd_batched':
        env['PGDELTA_ZSTD_TASK_BYTES'] = str(1 << 20)
    cmd = [sys.executable, '-m', 'pgdelta', 'local', '--source', str(s), '--basis', str(basis), '--in-place',
           '--source-kind', 'datadir', '--jobs', str(JOBS), '--pg-controldata', str(Path(a.pg_bin) / 'pg_controldata'),
           '--stats-json', str(stats_path)]
    if arm != 'plain':
        cmd += ['--compress', 'zstd']
    started = time.perf_counter()
    done = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
    wall = time.perf_counter() - started
    stats = json.loads(stats_path.read_text()) if stats_path.exists() else {}
    checked = compare(basis, expected) if expected is not None else None
    shutil.rmtree(basis, ignore_errors=True)
    receiver = stats.get('receiver') or {}
    return {'arm': arm, 'label': label, 'exit_code': done.returncode, 'wall_s': round(wall, 4),
            'status': stats.get('status'), 'files_digest': stats.get('files_digest'),
            'timings': stats.get('timings_seconds'), 'pages': stats.get('pages'),
            'bytes_source_to_dest': (stats.get('bytes_source_to_dest') or {}).get('total'),
            'compress': stats.get('compress'), 'receiver_compress': receiver.get('compress'),
            'bytes': checked, 'stderr_tail': done.stderr[-400:] if done.returncode else ''}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--out', required=True)
    ap.add_argument('--scale', type=int, default=70)
    ap.add_argument('--pg-bin', default='/opt/homebrew/opt/postgresql@17/bin')
    ap.add_argument('--work')
    ap.add_argument('--rotation', type=int, default=0)
    ap.add_argument('--compare-bytes', action='store_true')
    a = ap.parse_args(argv)
    out, work = Path(a.out), Path(a.work or tempfile.mkdtemp(prefix='exp50.'))
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    pg = exp37.PG(a.pg_bin, tempfile.mkdtemp(prefix='pgs.'))
    b0, s = work / 'template', work / 'uniform-s'
    if not b0.exists():
        exp37.make_template(pg, b0, a.scale)
    shutil.rmtree(s, ignore_errors=True)
    exp37.run(['cp', '-c', '-R', str(b0), str(s)])
    pg.start(s)
    for statement in exp37.SCENARIOS['uniform']:
        pg.sql(statement)
    pg.sql('CHECKPOINT')
    pg.stop(s)
    expected = expected_files(a, s) if a.compare_bytes else None
    with open(out / 'runs.jsonl', 'w') as f:
        if expected is not None:
            control = {'label': 'control', 'arm': None, 'repeat': None, 'bytes': compare(b0, expected)}
            f.write(json.dumps(control) + '\n')
            print('control: B before any refresh', control['bytes']['files_differing'], 'files differ', flush=True)
        plan = [('plain', 'warmup', None)] + [(arm, f'r{rep}-{arm}', rep) for rep in range(REPEATS)
                                               for arm in order(rep, a.rotation)]
        for arm, label, repeat in plan:
            row = {'scale': a.scale, 'repeat': repeat, **one_run(a, work, s, b0, arm, label, expected)}
            f.write(json.dumps(row) + '\n')
            f.flush()
            print(label, row['exit_code'], row['status'], (row['timings'] or {}).get('total'),
                  (row['bytes'] or {}).get('files_differing'), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
