"""EXP-38's analysis: how old can the copy be? (docs/experiments/EXP-38-copy-age.md)

    .venv/bin/python analysis/exp38.py [--results results/bytes/exp38/results.jsonl] [--figdir DIR]

The changed fraction of the database against the transactions since the old copy, per workload, and where it crosses
the DR crossovers registered from EXP-24 (60 ms, tuned hosts, 2.1 GB): v1 takes c + f * size / R with c = 3.48 s and
R = 194.4 MB/s (fitted to EXP-24's hot and uniform 1% medians), against 11.63 s for one pg_basebackup stream and
10.48 s for the fastest parallel copy. The crossing is interpolated in log(transactions). Writes
docs/experiments/data/exp38-age.csv and docs/figures/exp38-age.svg.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
# From EXP-24 (60 ms, tuned hosts, BBR v1): hot 3.63 s for 29.0 MB, uniform 1% 6.62 s for 610.4 MB.
R_MB_S = (610.4 - 29.0) / (6.62 - 3.63)
C_S = 3.63 - 29.0 / R_MB_S
SIZE_MB = 2105.0
FULL_S, PARALLEL_S = 11.63, 10.48
CROSS = {'one pg_basebackup': (FULL_S - C_S) * R_MB_S / SIZE_MB, 'parallel copy': (PARALLEL_S - C_S) * R_MB_S / SIZE_MB}


def crossing(points, f):
    """Transactions at which the changed fraction first reaches f, interpolated in log(transactions); None if never."""
    for (n0, f0), (n1, f1) in zip(points, points[1:]):
        if f0 < f <= f1:
            t = (f - f0) / (f1 - f0)
            return round(math.exp(math.log(n0) + t * (math.log(n1) - math.log(n0))))
    return 0 if points and points[0][1] >= f else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'bytes' / 'exp38' / 'results.jsonl'))
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    rows = [json.loads(line) for line in open(a.results) if line.strip()]
    by = {}
    for r in rows:
        by.setdefault(r['workload'], []).append((r['transactions'], r['changed_fraction']))
    print(f'model: c = {C_S:.2f} s, R = {R_MB_S:.1f} MB/s; crossovers: '
          + ', '.join(f'{k} at {v:.3f} of the database' for k, v in CROSS.items()))
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp38-age.csv', 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['workload', 'transactions', 'changed_fraction', 'v1_s_projected', 'vs_pg_basebackup'])
        for workload, pts in by.items():
            for n, frac in pts:
                t = C_S + frac * SIZE_MB / R_MB_S
                w.writerow([workload, n, frac, round(t, 2), round(t / FULL_S, 3)])
            for name, f_star in CROSS.items():
                print(f'  {workload}: reaches the crossover against {name} ({f_star:.3f}) at '
                      f'{crossing(pts, f_star)} transactions')
    fig, ax = plt.subplots(figsize=(8.5, 4.2))
    colors = {'tpcb': COLORS['red'], 'hot': COLORS['green']}
    labels = {'tpcb': "TPC-B-like (pgbench's built-in: every account equally likely)",
              'hot': 'hot: the same, accounts from the first 1%'}
    for workload, pts in by.items():
        ax.plot([n for n, _ in pts], [f for _, f in pts], marker='o', color=colors.get(workload), label=labels.get(workload, workload))
    for (name, f_star), style in zip(CROSS.items(), ('--', ':')):
        ax.axhline(f_star, color=COLORS['black'], ls=style, lw=0.9)
        ax.annotate(f'v1 = {name} at DR (60 ms, tuned): {f_star:.0%} changed', (min(n for n, _ in by['tpcb']), f_star),
                    textcoords='offset points', xytext=(2, 3), fontsize=7.5)
    ax.set_xscale('log')
    ax.set_xlabel('transactions since the old copy was taken')
    ax.set_ylabel('share of the database pgdelta sends')
    ax.set_ylim(0, 1)
    ax.set_title('How old can the copy be? Changed pages against transactions (pgbench scale 20)', fontsize=10)
    ax.legend(fontsize=8, loc='upper left')
    fig.tight_layout()
    print(plotting.save(fig, 'exp38-age'))


if __name__ == '__main__':
    main()
