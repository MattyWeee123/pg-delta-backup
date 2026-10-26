"""EXP-51's analysis: EXP-50's second session, with a byte-for-byte check (docs/experiments/EXP-51-batched-second-session.md)

    .venv/bin/python analysis/exp51.py [--first results/bytes/exp50/runs.jsonl] [--second results/bytes/exp51/runs.jsonl]

Medians per arm of pgdelta's own total (the warm-ups left out), for the second session alone and for both sessions'
6 repeats per arm. Scores:
1 (deciding) zstd_batched - plain <= 0.5 x (zstd_per_run - plain), in the second session alone and in both together;
2 every run of the second session leaves every file pgdelta sends byte-identical to S, and B before any refresh
  differs from S (the control);
3 rebuild, second session: zstd_per_run >= 1.5x plain's, zstd_batched <= 1.3x plain's.
Writes docs/experiments/data/exp51-runs.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARMS = ('plain', 'zstd_per_run', 'zstd_batched')


def load(path, session):
    rows = []
    for line in Path(path).read_text().splitlines():
        r = json.loads(line)
        t = r.get('timings') or {}
        rows.append({'session': session, 'label': r['label'], 'arm': r['arm'], 'repeat': r['repeat'],
                     'status': r.get('status'), 'total_s': t.get('total'), 'rebuild_s': t.get('rebuild'),
                     'files_checked': (r.get('bytes') or {}).get('files_checked'),
                     'files_differing': (r.get('bytes') or {}).get('files_differing')})
    return rows


def medians(rows, key):
    return {arm: statistics.median(r[key] for r in rows if r['arm'] == arm and r['repeat'] is not None) for arm in ARMS}


def cut(m):
    """(per-run excess over plain, batched excess, batched as a fraction of per-run)."""
    excess, left = m['zstd_per_run'] - m['plain'], m['zstd_batched'] - m['plain']
    return excess, left, left / excess if excess > 0 else float('inf')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--first', default=str(ROOT / 'results' / 'bytes' / 'exp50' / 'runs.jsonl'))
    ap.add_argument('--second', default=str(ROOT / 'results' / 'bytes' / 'exp51' / 'runs.jsonl'))
    a = ap.parse_args(argv)
    first, second = load(a.first, 1), load(a.second, 2)
    m2, both = medians(second, 'total_s'), medians(first + second, 'total_s')
    for name, m in (('second session', m2), ('both sessions', both)):
        e, l, f = cut(m)
        print(f'{name}: ' + ', '.join(f'{arm} {m[arm]:.3f}' for arm in ARMS) + f'; per run +{e:.2f} s, batched +{l:.2f} s ({f:.2f})')
    f2, fb = cut(m2)[2], cut(both)[2]
    print(f"1 (deciding). {'HELD' if f2 <= 0.5 and fb <= 0.5 else 'FAILED'}: batched keeps {f2:.2f} of the per-run "
          f"excess (second session), {fb:.2f} (both)")
    control = [r for r in second if r['label'] == 'control']
    runs = [r for r in second if r['label'] != 'control']
    exact = all(r['status'] == 'complete' and r['files_differing'] == 0 and r['files_checked'] for r in runs)
    differs = bool(control) and control[0]['files_differing'] > 0
    print(f"2. {'HELD' if exact and differs else 'FAILED'}: {sum(r['files_differing'] == 0 for r in runs)} of "
          f"{len(runs)} copies identical to S in all {runs[0]['files_checked']} files; the control (B before): "
          f"{control[0]['files_differing'] if control else None} files differ")
    r2 = medians(second, 'rebuild_s')
    rz, rb = r2['zstd_per_run'] / r2['plain'], r2['zstd_batched'] / r2['plain']
    print(f"3. {'HELD' if rz >= 1.5 and rb <= 1.3 else 'FAILED'}: rebuild over plain's: per run {rz:.2f}, batched {rb:.2f}")
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp51-runs.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(second[0]))
        w.writeheader()
        w.writerows(second)


if __name__ == '__main__':
    main()
