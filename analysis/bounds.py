"""Lower bounds beyond memory: why hashing can't beat a full copy there, and where page labels can.

    .venv/bin/python analysis/bounds.py [--exp64 results/exp04/exp64]

The disk model fixed in EXP-48 (18 GB on 8 GB VMs, 150 GiB balanced persistent disks; constants measured there):
- a full copy writes every page sequentially: T_full = N x 8 KiB / W_seq + 4.5 s;
- hashing reads all of B before it knows what to write, then writes the changed pages scattered:
  T_hash = N x 8 KiB / R + C / w (EXP-48's v1 model);
- page labels (the WAL-summary fast path) read and write only the changed pages: T_fast >= f + C / w, with f the
  fixed cost of a live session (backup start and stop, the summaries, the WAL).
N pages, C changed pages, R the read rate, W_seq the sequential write rate, w the scattered page-write rate.

No implementation of hashing escapes the N/R term (it must read B to compare it), so with R close to W_seq it can at
best tie T_full (85.1 s against 94.7 s with no change at all) and falls behind it past about 3% of pages changed:
an algorithm limit, not a systems one. Labels drop that term; the fast path then beats a
full copy while C / w < N / W_seq, i.e. while fewer than w x 8 KiB / W_seq of the pages changed (about 28% on this
disk), and loses past it, where sequential writing wins. Plots the three bounds against the share of pages changed,
with EXP-48's measured points and, given --exp64, EXP-64's. Writes the figure bounds-beyond-memory.
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
PAGE = 8192
# EXP-48 (docs/experiments/data/exp48-model.csv): its database, disk test and scattered-write rate.
N = 17_823e6 / PAGE
R, W_SEQ, W_RAND, FULL_FIXED = 209.5e6, 197.6e6, 6_750, 4.5
EXP48 = {'hot': {'pages': 13_935, 'hash': 87.5, 'full': 90.5}, 'uniform': {'pages': 619_856, 'hash': 184.6, 'full': 90.8}}


def t_full(c):
    return N * PAGE / W_SEQ + FULL_FIXED


def t_hash(c):
    return N * PAGE / R + c / W_RAND


def t_fast(c, fixed=5.0):
    return fixed + c / W_RAND


def exp64_points(results):
    points = {}
    for meta_path in sorted(Path(results).glob('run-*/meta.json')):
        meta = json.loads(meta_path.read_text())
        if meta['arm'] == 'fast' and meta['status'] == 0:
            points.setdefault(meta['pattern'], []).append(meta['t1'] - meta['t0'])
    return {p: statistics.median(v) for p, v in points.items()}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--exp64')
    a = ap.parse_args(argv)
    crossover = (t_full(0) - 5.0) * W_RAND / N
    print(f'N = {N:,.0f} pages; T_full = {t_full(0):.1f} s; T_hash(0) = {t_hash(0):.1f} s; '
          f'fast path beats a full copy below {crossover:.1%} of pages changed (w x 8 KiB / W_seq = '
          f'{W_RAND * PAGE / W_SEQ:.1%})')
    plotting.style()
    fig, ax = plt.subplots(figsize=(7.8, 4.4))
    shares = [i / 200 for i in range(0, 121)]
    ax.plot([s * 100 for s in shares], [t_full(s * N) for s in shares], color=COLORS['orange'], lw=2,
            label='full copy (sequential writes)')
    ax.plot([s * 100 for s in shares], [t_hash(s * N) for s in shares], color=COLORS['grey'], lw=2,
            label='hashing: read all of B, then write the changes')
    ax.plot([s * 100 for s in shares], [t_fast(s * N) for s in shares], color=COLORS['blue'], lw=2,
            label='page labels: write the changes only (fast path)')
    for pattern, cell in EXP48.items():
        share = cell['pages'] / N * 100
        ax.plot(share, cell['hash'], 'o', color=COLORS['grey'], ms=7)
        ax.plot(share, cell['full'], 's', color=COLORS['orange'], ms=7)
        ax.annotate(f'EXP-48 {pattern}', (share, cell['hash']), textcoords='offset points', xytext=(6, 4), fontsize=8)
    if a.exp64:
        for pattern, seconds in exp64_points(a.exp64).items():
            share = EXP48[pattern]['pages'] / N * 100
            ax.plot(share, seconds, 'D', color=COLORS['blue'], ms=8)
            ax.annotate(f'EXP-64 fast, {pattern}', (share, seconds), textcoords='offset points', xytext=(6, -12),
                        fontsize=8)
    ax.axvline(crossover * 100, color=COLORS['blue'], lw=0.8, ls=':')
    ax.text(crossover * 100 + 0.5, 15, f'labels lose to a full copy\npast {crossover:.0%} of pages changed',
            fontsize=8, color=COLORS['blue'])
    ax.set_xlim(0, 60)
    ax.set_ylim(0, 260)
    ax.set_xlabel('pages changed since the old copy (% of 18 GB)')
    ax.set_ylabel('seconds (lower bound, EXP-48 disk constants)')
    ax.legend(fontsize=8, loc='upper left')
    ax.set_title('Beyond memory on a balanced persistent disk: what each algorithm must at least pay', fontsize=10)
    print(plotting.save(fig, 'bounds-beyond-memory'))


if __name__ == '__main__':
    main()
