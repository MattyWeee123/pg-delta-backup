"""EXP-29's figure: keyed BLAKE2b against keyed BLAKE3, per 8 KiB page and per core (docs/experiments/EXP-29-hash-per-core.md).

    .venv/bin/python analysis/exp29.py [--figdir DIR]

Reads docs/experiments/data/exp29-hash.csv (from results/hash/exp29/*.json) and draws pages per second per process
at 1 process, by host, with the ratio and T5's 1.5x gate. Writes docs/figures/exp29-hash.svg.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import numpy as np

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    rows = [r for r in csv.DictReader(open(ROOT / 'docs' / 'experiments' / 'data' / 'exp29-hash.csv'))
            if r['procs'] == '1']
    x = np.arange(len(rows))
    fig, ax = plt.subplots(figsize=(8, 4.2))
    b2 = [int(r['blake2b_pages_per_s']) / 1000 for r in rows]
    b3 = [int(r['blake3_pages_per_s']) / 1000 for r in rows]
    ax.bar(x - 0.2, b2, 0.4, color=COLORS['grey'], label="keyed BLAKE2b (pgdelta's hash)")
    ax.bar(x + 0.2, b3, 0.4, color=COLORS['blue'], label='keyed BLAKE3')
    for xi, hi, r in zip(x, b3, rows):
        ax.annotate(f"{float(r['ratio']):.2f}×", (xi + 0.2, hi), textcoords='offset points', xytext=(0, 4),
                    ha='center', fontsize=10, fontweight='bold')
    ax.set_xticks(x, [f"{r['host']}\n{r['simd'] or 'NEON'}" for r in rows], fontsize=8.5)
    ax.set_ylabel('thousand 8 KiB pages per second, one core')
    ax.set_title("Per-page hashing, one process (T5's gate: BLAKE3 at least 1.5×)", fontsize=10)
    ax.set_ylim(0, max(b3) * 1.3)
    ax.legend(fontsize=8.5, loc='upper center', ncol=2)
    fig.tight_layout()
    print(plotting.save(fig, 'exp29-hash'))


if __name__ == '__main__':
    main()
