"""EXP-50 (lean, local): do larger pool tasks lower zstd's floor? (docs/experiments/EXP-50-zstd-batched-tasks.md)

    .venv/bin/python experiments/bytes/exp50.py --out results/bytes/exp50 [--scale 70] [--pg-bin DIR] [--work DIR]

S is EXP-37's template (pgbench with hex filler) after EXP-37's uniform update; B is the template. Each run
refreshes a fresh APFS clone of B in place with `pgdelta local --source-kind datadir --jobs 4`:
- plain: no compression;
- zstd_per_run: --compress zstd with PGDELTA_ZSTD_TASK_BYTES=0 (one run per pool task, EXP-47's path);
- zstd_batched: --compress zstd (tasks of compress.TASK_BYTES).
One untimed warm-up run (plain), then 3 repeats of all three arms in a rotated order. One JSON object per run goes
to OUT/runs.jsonl, with pgdelta's own statistics.
"""
from __future__ import annotations

import argparse
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

ARMS = ('plain', 'zstd_per_run', 'zstd_batched')
REPEATS = 3
JOBS = 4


def order(repeat):
    """Repeat 0: plain, per run, batched; each later repeat starts one arm further on."""
    return [ARMS[(repeat + i) % len(ARMS)] for i in range(len(ARMS))]


def one_run(a, work, s, b0, arm, label):
    basis, stats_path = work / 'B-run', work / f'{label}.stats.json'
    shutil.rmtree(basis, ignore_errors=True)
    exp37.run(['cp', '-c', '-R', str(b0), str(basis)])
    env = dict(os.environ)
    env.pop('PGDELTA_ZSTD_TASK_BYTES', None)
    if arm == 'zstd_per_run':
        env['PGDELTA_ZSTD_TASK_BYTES'] = '0'
    cmd = [sys.executable, '-m', 'pgdelta', 'local', '--source', str(s), '--basis', str(basis), '--in-place',
           '--source-kind', 'datadir', '--jobs', str(JOBS), '--pg-controldata', str(Path(a.pg_bin) / 'pg_controldata'),
           '--stats-json', str(stats_path)]
    if arm != 'plain':
        cmd += ['--compress', 'zstd']
    started = time.perf_counter()
    done = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
    wall = time.perf_counter() - started
    stats = json.loads(stats_path.read_text()) if stats_path.exists() else {}
    shutil.rmtree(basis, ignore_errors=True)
    receiver = stats.get('receiver') or {}
    return {'arm': arm, 'label': label, 'exit_code': done.returncode, 'wall_s': round(wall, 4),
            'status': stats.get('status'), 'files_digest': stats.get('files_digest'),
            'timings': stats.get('timings_seconds'), 'pages': stats.get('pages'),
            'bytes_source_to_dest': (stats.get('bytes_source_to_dest') or {}).get('total'),
            'compress': stats.get('compress'), 'receiver_compress': receiver.get('compress'),
            'stderr_tail': done.stderr[-400:] if done.returncode else ''}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--out', required=True)
    ap.add_argument('--scale', type=int, default=70)
    ap.add_argument('--pg-bin', default='/opt/homebrew/opt/postgresql@17/bin')
    ap.add_argument('--work')
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
    with open(out / 'runs.jsonl', 'w') as f:
        plan = [('plain', 'warmup', None)] + [(arm, f'r{rep}-{arm}', rep) for rep in range(REPEATS) for arm in order(rep)]
        for arm, label, repeat in plan:
            row = {'scale': a.scale, 'repeat': repeat, **one_run(a, work, s, b0, arm, label)}
            f.write(json.dumps(row) + '\n')
            f.flush()
            print(label, row['exit_code'], row['status'], (row['timings'] or {}).get('total'), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
