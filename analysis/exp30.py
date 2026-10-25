"""EXP-30's figure: BLAKE2b against BLAKE3 on a busy primary, unthrottled (docs/experiments/EXP-30-blake3-live.md).

    .venv/bin/python analysis/exp30.py [--runs results/exp04/sets/t10b/exp30] [--figdir DIR]

EXP-28's analysis (analysis/exp28.py: O4's two numbers, the cells and the bar) on EXP-30's runs, drawn per arm: the
refresh time, the sender's own CPU-seconds (its workers included) and the work the load lost beyond load-only time,
with O4's bar. Writes docs/experiments/data/exp30-cells.csv and docs/figures/exp30-blake3.svg.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import numpy as np

import exp28
import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
ARMS = [('full', 'pg_base-\nbackup'), ('v1_j2', 'v1 BLAKE2b\n2 workers'), ('v1_j4', 'v1 BLAKE2b\n4 workers'),
        ('v1b3_j2', 'v1 BLAKE3\n2 workers'), ('v1b3_j4', 'v1 BLAKE3\n4 workers')]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--runs', default=str(ROOT / 'results' / 'exp04' / 'sets' / 't10b' / 'exp30'))
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    runs = exp28.runs_of(a.runs, exp28.transactions(a.runs, exp28.shift(a.runs)))
    rows = {r['arm']: r for r in exp28.cells(runs)}
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp30-cells.csv', 'w', newline='') as f:
        keys = sorted({k for r in rows.values() for k in r}, key=lambda k: (k not in ('arm', 'speed', 'runs'), k))
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows.values())
    colors = [COLORS['grey'], COLORS['sky'], COLORS['sky'], COLORS['blue'], COLORS['blue']]
    x = np.arange(len(ARMS))
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.2))
    panels = ((axes[0], 'seconds', 'Refresh time (s)'),
              (axes[1], 'sender_cpu_s', "Sender's CPU-seconds, workers included"),
              (axes[2], 'W_net', 'Work the load lost beyond load-only time (s)'))
    for ax, key, title in panels:
        vals = [rows.get(arm, {}).get(key) for arm, _ in ARMS]
        ax.bar(x, [v or 0 for v in vals], color=colors)
        for xi, v in zip(x, vals):
            if v is None:
                ax.annotate('not\nmeasured', (xi, 0), textcoords='offset points', xytext=(0, 3), ha='center',
                            fontsize=7.5, color=COLORS['grey'])
            else:
                ax.annotate(f'{v:.2f}' if key != 'W_net' else f'{v:.3f}', (xi, v), textcoords='offset points',
                            xytext=(0, 3), ha='center', fontsize=8.5)
        ax.set_xticks(x, [label for _, label in ARMS], fontsize=7.5)
        ax.set_title(title, fontsize=10)
    full = rows.get('full', {})
    if full.get('W_net') is not None:
        bar = full['W_net'] + max(exp28.FLOOR_W, exp28.SHARE_W * full['W_net'])
        axes[2].axhline(bar, color=COLORS['black'], ls='--', lw=0.9)
        axes[2].annotate("O4's bar", (len(ARMS) - 0.5, bar), textcoords='offset points', xytext=(-4, 3),
                         ha='right', fontsize=8)
    fig.suptitle('A busy primary (14,700 transactions a second), unthrottled: BLAKE2b against BLAKE3', fontsize=11)
    fig.tight_layout()
    print(plotting.save(fig, 'exp30-blake3'))


if __name__ == '__main__':
    main()
