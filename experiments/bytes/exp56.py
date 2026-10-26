"""EXP-56, stage 1 (local): sub-page deltas as built (docs/experiments/EXP-56-subpage-build.md).

    .venv/bin/python experiments/bytes/exp56.py --work DIR --out results/bytes/exp56

DIR holds EXP-51's scale-70 data: template (B) and uniform-s (S), as EXP-54 and EXP-55 measured them. Each run
refreshes a fresh APFS clone of B in place with `pgdelta local --source-kind datadir --jobs 4` in one of four arms
(plain; zstd; subpage; zstd_subpage), one warm-up run, then 3 repeats in a rotated order. After every run each file
pgdelta sends is compared with S's (EXP-51's check), and once, before any run, with B's (the control). One JSON
object per run goes to OUT/runs.jsonl, with pgdelta's own statistics.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]
import exp37  # noqa: E402  (run)
import exp50  # noqa: E402  (expected_files, compare: EXP-51's byte check)

ARMS = ('zstd', 'zstd_subpage', 'plain', 'subpage')
REPEATS = 3
JOBS = 4


def one_run(a, work, s, b0, arm, label, expected):
    basis, stats_path = work / 'B-run', work / f'{label}.stats.json'
    shutil.rmtree(basis, ignore_errors=True)
    stats_path.unlink(missing_ok=True)
    exp37.run(['cp', '-c', '-R', str(b0), str(basis)])
    cmd = [sys.executable, '-m', 'pgdelta', 'local', '--source', str(s), '--basis', str(basis), '--in-place',
           '--source-kind', 'datadir', '--jobs', str(JOBS), '--pg-controldata', str(Path(a.pg_bin) / 'pg_controldata'),
           '--stats-json', str(stats_path)]
    if 'zstd' in arm:
        cmd += ['--compress', 'zstd']
    if 'subpage' in arm:
        cmd += ['--subpage']
    env = {k: v for k, v in os.environ.items() if k != 'PGDELTA_ZSTD_TASK_BYTES'}
    started = time.perf_counter()
    done = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
    wall = time.perf_counter() - started
    stats = json.loads(stats_path.read_text()) if stats_path.exists() else {}
    checked = exp50.compare(basis, expected)
    shutil.rmtree(basis, ignore_errors=True)
    return {'arm': arm, 'label': label, 'exit_code': done.returncode, 'wall_s': round(wall, 4),
            'status': stats.get('status'), 'timings': stats.get('timings_seconds'),
            'bytes_source_to_dest': stats.get('bytes_source_to_dest'), 'bytes_dest_to_source': stats.get('bytes_dest_to_source'),
            'compress': stats.get('compress'), 'subpage': stats.get('subpage'),
            'receiver_subpage': (stats.get('receiver') or {}).get('subpage'), 'bytes': checked,
            'stderr_tail': done.stderr[-600:] if done.returncode else ''}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--work', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--pg-bin', default='/opt/homebrew/opt/postgresql@17/bin')
    a = ap.parse_args(argv)
    work, out = Path(a.work), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    b0, s = work / 'template', work / 'uniform-s'
    expected = exp50.expected_files(a, s)
    with open(out / 'runs.jsonl', 'w') as f:
        control = {'label': 'control', 'arm': None, 'repeat': None, 'bytes': exp50.compare(b0, expected)}
        f.write(json.dumps(control) + '\n')
        print('control: B before any refresh', control['bytes']['files_differing'], 'files differ', flush=True)
        plan = [('plain', 'warmup', None)] + [(ARMS[(rep + i) % len(ARMS)], f'r{rep}-{ARMS[(rep + i) % len(ARMS)]}', rep)
                                               for rep in range(REPEATS) for i in range(len(ARMS))]
        for arm, label, repeat in plan:
            row = {'repeat': repeat, **one_run(a, work, s, b0, arm, label, expected)}
            f.write(json.dumps(row) + '\n')
            f.flush()
            print(label, row['exit_code'], row['status'], (row['timings'] or {}).get('total'),
                  row['bytes']['files_differing'], (row['bytes_source_to_dest'] or {}).get('total'), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
