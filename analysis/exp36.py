"""EXP-36's analysis: native incremental against v1 (BBR, the CLI's defaults) at DR round trips (docs/experiments/EXP-36-dr-vs-incremental.md).

    .venv/bin/python analysis/exp36.py [--sweep results/exp04/exp36] [--figdir DIR]

Per pattern and condition, the median time and bytes (source to dest) of every passing run of each method; the
ratio incremental / v1 (below 1: incremental faster) and each against one pg_basebackup stream. Writes
docs/experiments/data/exp36-cells.csv and docs/figures/exp36-incremental.svg.
"""
from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path

import numpy as np

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
V1, INC, FULL = 'pgdelta_direct_inplace_bbr', 'incremental', 'full'
CONDITIONS = ['2:60:default', '2:60:tuned', '2:120:tuned']


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--sweep', default=str(ROOT / 'results' / 'exp04' / 'exp36'))
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    times, nbytes = {}, {}
    for r in csv.DictReader(open(Path(a.sweep) / 'summary.csv')):
        if r['status'] != 'pass':
            continue
        key = (r['pattern'], r['speed'], r['method'])
        times.setdefault(key, []).append(float(r['total_s']))
        if r.get('wire_s2d_bytes'):
            nbytes.setdefault(key, []).append(float(r['wire_s2d_bytes']))
    med = {k: statistics.median(v) for k, v in times.items()}
    mb = {k: statistics.median(v) / 1e6 for k, v in nbytes.items()}
    rows = []
    for p in ('hot', 'uniform'):
        for c in CONDITIONS:
            row = {'pattern': p, 'condition': c}
            for m, name in ((FULL, 'full'), (INC, 'incremental'), (V1, 'v1')):
                row[f'{name}_s'] = round(med[(p, c, m)], 2) if (p, c, m) in med else None
                row[f'{name}_runs'] = len(times.get((p, c, m), []))
                row[f'{name}_mb'] = round(mb[(p, c, m)], 1) if (p, c, m) in mb else None
            if row['incremental_s'] and row['v1_s']:
                row['inc_over_v1'] = round(row['incremental_s'] / row['v1_s'], 3)
            rows.append(row)
            print(row)
    keys = list(rows[0])
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp36-cells.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2), sharey=True)
    x = np.arange(len(CONDITIONS))
    for ax, p in zip(axes, ('hot', 'uniform')):
        for i, (key, label, color) in enumerate((('full_s', 'one pg_basebackup', COLORS['grey']),
                                                  ('incremental_s', 'native incremental + pg_combinebackup', COLORS['green']),
                                                  ('v1_s', 'v1 (BBR, one-pass verification)', COLORS['blue']))):
            vals = [next((r[key] for r in rows if r['pattern'] == p and r['condition'] == c), None) or 0 for c in CONDITIONS]
            bars = ax.bar(x + (i - 1) * 0.27, vals, 0.27, color=color, label=label)
            for bar, v in zip(bars, vals):
                ax.annotate(f'{v:.1f}', (bar.get_x() + bar.get_width() / 2, v), textcoords='offset points',
                            xytext=(0, 2), ha='center', fontsize=7.5)
        ax.set_xticks(x, [c.replace('2:', '').replace(':', ' ms, ') + ' hosts' for c in CONDITIONS], fontsize=8.5)
        ax.set_title(f'{p} 1% changes, 2.1 GB, 2 Gbit/s', fontsize=10)
    axes[0].set_ylabel('refresh time (s), median')
    axes[0].legend(fontsize=8)
    fig.tight_layout()
    print(plotting.save(fig, 'exp36-incremental'))


if __name__ == '__main__':
    main()
