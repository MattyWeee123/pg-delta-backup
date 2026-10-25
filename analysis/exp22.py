"""EXP-22: v1 against both full copies on persistent disks across zones, in and beyond memory (scale model).

    .venv/bin/python analysis/exp22.py [--root results/exp04/sets/ha] [--figdir DIR]

Medians per size, workload and arm; v1's ratio to one pg_basebackup stream (the released tools' yardstick) and to
the parallel bound (rule 3's), judged as docs/experiments/EXP-22-ha-scale-model.md registered; v1's phase timers.
Writes docs/experiments/data/exp22-cells.csv and docs/figures/exp22-ha.svg.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import statistics
from pathlib import Path

import numpy as np

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
SIZES = [('exp22-s70', '2.1 GB (fits in memory)'), ('exp22-s400', '12 GB (1.5x memory)')]
ARMS = [('pgdelta_direct_inplace', "v1", COLORS['blue']), ('full', 'one pg_basebackup stream', COLORS['grey']),
        ('parcopy8', 'parallel bound (8 streams)', COLORS['black'])]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--root', default=str(ROOT / 'results' / 'exp04' / 'sets' / 'ha'))
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    rows = []
    for name, label in SIZES:
        cells = {}
        for r in csv.DictReader(open(Path(a.root) / name / 'summary.csv')):
            if r['status'] == 'pass':
                cells.setdefault((r['pattern'], r['method']), []).append(float(r['total_s']))
        for pat in ('hot', 'uniform'):
            med = {m: statistics.median(cells[(pat, m)]) for m, _, _ in ARMS}
            timers = {}
            for f in glob.glob(str(Path(a.root) / name / 'runs' / f'*{pat}*' / '*pgdelta_direct_inplace.json')):
                ts = json.load(open(f))['tool_reported']['timings_seconds']
                for k in ('hash_source', 'hash_basis', 'verify', 'verify_wait', 'rebuild'):
                    timers.setdefault(k, []).append(ts.get(k) or 0)
            rows.append({'size': label, 'pattern': pat, **{f'{m}_s': round(v, 2) for m, v in med.items()},
                         'v1_over_full': round(med['pgdelta_direct_inplace'] / med['full'], 3),
                         'v1_over_bound': round(med['pgdelta_direct_inplace'] / med['parcopy8'], 3),
                         **{f'v1_{k}_s': round(statistics.median(v), 1) for k, v in timers.items()}})
    for r in rows:
        print(r)
    out = ROOT / 'docs' / 'experiments' / 'data' / 'exp22-cells.csv'
    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    print(out)
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.4))
    for ax, (_, label) in zip(axes, SIZES):
        sel = [r for r in rows if r['size'] == label]
        x = np.arange(len(sel))
        for i, (m, name, color) in enumerate(ARMS):
            ax.bar(x + (i - 1) * 0.27, [r[f'{m}_s'] for r in sel], 0.26, color=color, label=name)
        for xi, r in zip(x, sel):
            ax.text(xi - 0.27, r['pgdelta_direct_inplace_s'] * 1.02, f"{r['v1_over_full']:.2f}x", ha='center', fontsize=8.5)
        ax.set_xticks(x, [r['pattern'] + ' 1%' for r in sel]); ax.set_ylabel('median time (s)'); ax.set_title(label)
    axes[0].legend(fontsize=8.5)
    fig.suptitle("EXP-22: balanced persistent disks, two zones (v1's time over one pg_basebackup stream above its bars)", fontsize=11)
    fig.tight_layout()
    print(plotting.save(fig, 'exp22-ha'))


if __name__ == '__main__':
    main()
