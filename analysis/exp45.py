"""EXP-45's analysis: does compressing the pages sent make v1 faster at DR round trips?
(docs/experiments/EXP-45-zstd-dr.md)

    .venv/bin/python analysis/exp45.py [--sweep results/exp04/exp45] [--figdir DIR]

Per pattern and condition, the median time and bytes (source to dest) of every passing run of each method, the
ratio v1 with zstd / v1 (the deciding one), the byte ratio, and each against one pg_basebackup stream. Scores the
four registered predictions. Writes docs/experiments/data/exp45-cells.csv and docs/figures/exp45-zstd.svg.
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
FULL, V1, V1Z = 'full', 'pgdelta_direct_inplace_bbr', 'pgdelta_direct_inplace_bbr_z'
CONDITIONS = ['2:60:tuned', '2:120:tuned']


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--sweep', default=str(ROOT / 'results' / 'exp04' / 'exp45'))
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    times, nbytes, failed = {}, {}, []
    for r in csv.DictReader(open(Path(a.sweep) / 'summary.csv')):
        if r['status'] != 'pass':
            failed.append((r['pattern'], r['speed'], r['method'], r['status']))
            continue
        key = (r['pattern'], r['speed'], r['method'])
        times.setdefault(key, []).append(float(r['total_s']))
        if r.get('wire_s2d_bytes'):
            nbytes.setdefault(key, []).append(float(r['wire_s2d_bytes']))
    if failed:
        print('not passing:', failed)
    med = {k: statistics.median(v) for k, v in times.items()}
    mb = {k: statistics.median(v) / 1e6 for k, v in nbytes.items()}
    rows = []
    for p in ('hot', 'uniform'):
        for c in CONDITIONS:
            row = {'pattern': p, 'condition': c}
            for m, name in ((FULL, 'full'), (V1, 'v1'), (V1Z, 'v1_zstd')):
                row[f'{name}_s'] = round(med[(p, c, m)], 2) if (p, c, m) in med else None
                row[f'{name}_runs'] = len(times.get((p, c, m), []))
                row[f'{name}_mb'] = round(mb[(p, c, m)], 1) if (p, c, m) in mb else None
            row['zstd_over_v1'] = round(row['v1_zstd_s'] / row['v1_s'], 3) if row['v1_zstd_s'] and row['v1_s'] else None
            row['bytes_v1_over_zstd'] = (round(row['v1_mb'] / row['v1_zstd_mb'], 2)
                                         if row['v1_mb'] and row['v1_zstd_mb'] else None)
            row['zstd_over_full'] = round(row['v1_zstd_s'] / row['full_s'], 3) if row['v1_zstd_s'] and row['full_s'] else None
            row['v1_over_full'] = round(row['v1_s'] / row['full_s'], 3) if row['v1_s'] and row['full_s'] else None
            rows.append(row)
            print(row)
    uni = [r for r in rows if r['pattern'] == 'uniform']
    hot = [r for r in rows if r['pattern'] == 'hot']
    verdicts = [
        ('1 (deciding)', 'uniform: v1 with zstd at most 0.90 of v1, both round trips',
         all(r['zstd_over_v1'] is not None and r['zstd_over_v1'] <= 0.90 for r in uni)),
        ('2', 'hot: within 10% of v1', all(r['zstd_over_v1'] is not None and abs(r['zstd_over_v1'] - 1) <= 0.10 for r in hot)),
        ('3', 'uniform: bytes fall at least 2-fold', all(r['bytes_v1_over_zstd'] is not None and r['bytes_v1_over_zstd'] >= 2 for r in uni)),
        ('4', 'v1 with zstd faster than one pg_basebackup in every cell',
         all(r['zstd_over_full'] is not None and r['zstd_over_full'] < 1 for r in rows)),
    ]
    for n, text, held in verdicts:
        near = ''
        if n.startswith('1'):
            near = ' (provisional: within 20% of 0.90)' if any(r['zstd_over_v1'] and abs(r['zstd_over_v1'] / 0.90 - 1) < 0.2 for r in uni) else ''
        print(f"{n}. {'HELD' if held else 'FAILED'}: {text}{near}")
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp45-cells.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2), sharey=True)
    x = np.arange(len(CONDITIONS))
    for ax, p in zip(axes, ('hot', 'uniform')):
        arms = [('full_s', 'one pg_basebackup', COLORS['grey']), ('v1_s', 'v1 (BBR)', COLORS['sky']),
                ('v1_zstd_s', 'v1 (BBR) + zstd', COLORS['blue'])]
        width = 0.8 / len(arms)
        for i, (key, label, color) in enumerate(arms):
            vals = [next((r[key] for r in rows if r['pattern'] == p and r['condition'] == c), None) or 0 for c in CONDITIONS]
            bars = ax.bar(x + (i - (len(arms) - 1) / 2) * width, vals, width, color=color, label=label)
            for bar, v in zip(bars, vals):
                ax.annotate(f'{v:.1f}', (bar.get_x() + bar.get_width() / 2, v), textcoords='offset points',
                            xytext=(0, 2), ha='center', fontsize=7.5)
        for xi, c in enumerate(CONDITIONS):
            r = next(r for r in rows if r['pattern'] == p and r['condition'] == c)
            if r['bytes_v1_over_zstd']:
                ax.annotate(f"{r['v1_mb']:.0f} → {r['v1_zstd_mb']:.0f} MB", (xi, 0), textcoords='offset points',
                            xytext=(0, -26), ha='center', fontsize=7.5, color=COLORS['blue'])
        ax.set_xticks(x, [c.replace('2:', '').replace(':', ' ms, ') + ' hosts' for c in CONDITIONS], fontsize=8.5)
        ax.set_title(f'{p} 1% changes, 2.1 GB (hex filler), 2 Gbit/s', fontsize=10)
    axes[0].set_ylabel('refresh time (s), median')
    axes[0].legend(fontsize=8)
    fig.tight_layout()
    print(plotting.save(fig, 'exp45-zstd'))


if __name__ == '__main__':
    main()
