"""EXP-15 (lean): cubic and BBR v1 against both full copies on a real Iowa-Oregon link (31 ms).

    .venv/bin/python analysis/exp15_lean.py [--root results/exp04/sets/xr/exp15-lean] [--figdir DIR]

Medians per workload, host setting and arm; v1's ratios to the parallel bound (R_fast) and to one pg_basebackup
stream (R_F0). Writes docs/experiments/data/exp15-lean-cells.csv and docs/figures/exp15-lean.svg.
"""
from __future__ import annotations

import argparse
import csv
import statistics
from collections import defaultdict
from pathlib import Path

import numpy as np

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
ARMS = [('pgdelta_direct_inplace', 'v1, cubic (host default)', COLORS['orange']),
        ('pgdelta_direct_inplace_bbr', 'v1, BBR on its socket', COLORS['blue']),
        ('parcopy8', 'parallel bound (8 streams)', COLORS['black']), ('full', 'one pg_basebackup stream', COLORS['grey'])]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--root', default=str(ROOT / 'results' / 'exp04' / 'sets' / 'xr' / 'exp15-lean'))
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    cells = defaultdict(list)
    for r in csv.DictReader(open(Path(a.root) / 'summary.csv')):
        if r['status'] == 'pass':
            cells[(r['pattern'], r['speed'], r['method'])].append(float(r['total_s']))
    rows = []
    for p in ('hot', 'uniform'):
        for cond in ('2:0:default', '2:0:tuned'):
            med = {m: statistics.median(cells[(p, cond, m)]) for m, _, _ in ARMS}
            rows.append({'pattern': p, 'host': cond.split(':')[2], **{f'{m}_s': round(v, 2) for m, v in med.items()},
                         **{f'{m}_R_fast': round(med[m] / med['parcopy8'], 3) for m in ('pgdelta_direct_inplace', 'pgdelta_direct_inplace_bbr')},
                         **{f'{m}_R_F0': round(med[m] / med['full'], 3) for m in ('pgdelta_direct_inplace', 'pgdelta_direct_inplace_bbr')}})
    out = ROOT / 'docs' / 'experiments' / 'data' / 'exp15-lean-cells.csv'
    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    print(out)
    fig, ax = plt.subplots(figsize=(10, 4.4))
    x = np.arange(len(rows))
    for i, (m, name, color) in enumerate(ARMS):
        ax.bar(x + (i - 1.5) * 0.2, [r[f'{m}_s'] for r in rows], 0.19, color=color, label=name)
    ax.set_xticks(x, [f"{r['pattern']} 1%, {r['host']} hosts" for r in rows])
    ax.set_ylabel('median time (s)'); ax.legend(fontsize=8.5)
    ax.set_title('EXP-15 (lean): a real link between Iowa and Oregon, 31 ms round trip, 2 Gbit/s cap', fontsize=11)
    fig.tight_layout()
    print(plotting.save(fig, 'exp15-lean'))


if __name__ == '__main__':
    main()
