"""EXP-46's analysis: with compression on a pool of threads, pipelined with sending, is v1 faster at DR?
(docs/experiments/EXP-46-zstd-pool.md)

    .venv/bin/python analysis/exp46.py [--sweep results/exp04/exp46 ...] [--figdir DIR]

Session 1 alone (one --sweep, the default): EXP-45's analysis on its runs (files tagged -pool) and pgdelta's own
statistics, as registered. Both sessions (--sweep results/exp04/exp46 --sweep results/exp04/exp46b): the registered
verdict on prediction 1 from the medians of all runs per cell (4), each session's ratio beside it; the exploratory
0.5 Gbit/s cell (session 2's revision: uniform at most 0.70 of v1); and from pgdelta's statistics, the BBR check
and the sending thread's wait (prediction 3). Writes docs/experiments/data/exp46-cells.csv and
docs/figures/exp46-zstd.svg.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

import numpy as np

import exp45
import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
FULL, V1, V1Z = exp45.FULL, exp45.V1, exp45.V1Z
DECIDING = ['2:60:tuned', '2:120:tuned']
EXPLORATORY = '0.5:60:tuned'


def runs_of(sweep):
    rows = {}
    for r in csv.DictReader(open(Path(sweep) / 'summary.csv')):
        if r['status'] == 'pass':
            rows.setdefault((r['pattern'], r['speed'], r['method']), []).append(
                (float(r['total_s']), float(r['wire_s2d_bytes']) / 1e6 if r.get('wire_s2d_bytes') else None))
    return rows


def pgdelta_stats(sweep):
    effective, compress = [], {}
    for log in sorted(Path(sweep).glob('logs/*/source/*-stats.json.log')):
        text = log.read_text()
        stats = json.loads(text[text.find('{'):])
        effective.append((stats.get('congestion_control') or {}).get('effective'))
        if stats.get('compress'):
            pattern = 'uniform' if 'uniform' in log.parent.parent.name else 'hot'
            compress.setdefault(pattern, []).append(stats['compress'])
    return effective, compress


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--sweep', action='append')
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    sweeps = a.sweep or [str(ROOT / 'results' / 'exp04' / 'exp46')]
    if len(sweeps) == 1:
        exp45.main(['--sweep', sweeps[0], '--tag=-pool', *(['--figdir', a.figdir] if a.figdir else [])])
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    sessions = [runs_of(s) for s in sweeps]
    combined = {}
    for runs in sessions:
        for key, values in runs.items():
            combined.setdefault(key, []).extend(values)
    med = lambda values: statistics.median(v[0] for v in values) if values else None
    rows = []
    for pattern in ('hot', 'uniform'):
        for condition in [*DECIDING, EXPLORATORY]:
            if not combined.get((pattern, condition, V1)):
                continue
            row = {'pattern': pattern, 'condition': condition}
            for method, name in ((FULL, 'full'), (V1, 'v1'), (V1Z, 'v1_zstd')):
                values = combined.get((pattern, condition, method), [])
                row[f'{name}_s'] = round(med(values), 2) if values else None
                row[f'{name}_runs'] = len(values)
                mbs = [v[1] for v in values if v[1] is not None]
                row[f'{name}_mb'] = round(statistics.median(mbs), 1) if mbs else None
            row['zstd_over_v1'] = round(row['v1_zstd_s'] / row['v1_s'], 3) if row['v1_zstd_s'] and row['v1_s'] else None
            for i, runs in enumerate(sessions, 1):
                v1, z = med(runs.get((pattern, condition, V1), [])), med(runs.get((pattern, condition, V1Z), []))
                row[f'session{i}_zstd_over_v1'] = round(z / v1, 3) if v1 and z else None
            rows.append(row)
            print(row)
    uniform = [r for r in rows if r['pattern'] == 'uniform' and r['condition'] in DECIDING]
    held = all(r['zstd_over_v1'] is not None and r['zstd_over_v1'] <= 0.90 for r in uniform)
    near = any(r['zstd_over_v1'] and abs(r['zstd_over_v1'] / 0.90 - 1) < 0.2 for r in uniform)
    print(f"1 (deciding, all sessions' runs). {'HELD' if held else 'FAILED'}: uniform at most 0.90 of v1 at both round "
          f"trips: {[r['zstd_over_v1'] for r in uniform]}{' (within 20% of the bar)' if near else ''}")
    slow = next((r for r in rows if r['pattern'] == 'uniform' and r['condition'] == EXPLORATORY), None)
    if slow:
        print(f"exploratory, 0.5 Gbit/s uniform: {'HELD' if slow['zstd_over_v1'] <= 0.70 else 'FAILED'} "
              f"(at most 0.70): {slow['zstd_over_v1']}")
    for sweep in sweeps:
        effective, compress = pgdelta_stats(sweep)
        waits = [round(c['wait_seconds'], 2) for c in compress.get('uniform', [])]
        print(f"{Path(sweep).name}: BBR in effect on {sum(e == 'bbr' for e in effective)} of {len(effective)} v1 runs; "
              f"uniform: the sender waited {waits} s (prediction 3: at most 1.1)")
    if len(sweeps) == 1:
        return
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp46-cells.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    conditions = [c for c in [*DECIDING, EXPLORATORY] if any(r['condition'] == c for r in rows)]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2))
    x = np.arange(len(conditions))
    for ax, pattern in zip(axes, ('hot', 'uniform')):
        arms = [('full_s', 'one pg_basebackup', COLORS['grey']), ('v1_s', 'v1 (BBR)', COLORS['sky']),
                ('v1_zstd_s', 'v1 (BBR) + zstd on a pool', COLORS['blue'])]
        width = 0.8 / len(arms)
        for i, (key, label, color) in enumerate(arms):
            vals = [next((r[key] for r in rows if r['pattern'] == pattern and r['condition'] == c), None) or 0
                    for c in conditions]
            bars = ax.bar(x + (i - 1) * width, vals, width, color=color, label=label)
            for bar, v in zip(bars, vals):
                ax.annotate(f'{v:.1f}', (bar.get_x() + bar.get_width() / 2, v), textcoords='offset points',
                            xytext=(0, 2), ha='center', fontsize=7.5)
        ax.set_xticks(x, [c.split(':')[0] + ' Gbit/s, ' + c.split(':')[1] + ' ms' for c in conditions], fontsize=8.5)
        ax.set_title(f'{pattern} 1% changes, 2.1 GB (hex filler); medians of both sessions', fontsize=10)
    axes[0].set_ylabel('refresh time (s)')
    axes[0].legend(fontsize=8)
    fig.tight_layout()
    print(plotting.save(fig, 'exp46-zstd'))


if __name__ == '__main__':
    main()
