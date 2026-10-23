"""EXP-07 (where a local pg_basebackup's time goes) figure from the curated run table.

    .venv/bin/python analysis/exp07.py
Reads docs/experiments/data/exp07-runs.csv and writes docs/figures/exp07-capture.svg.
"""
from __future__ import annotations

import csv
import statistics
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / 'docs' / 'experiments' / 'data' / 'exp07-runs.csv'
ORDER = [('C1', 'pg_basebackup, SHA-256 manifest\n(as EXP-04 ran it)', COLORS['grey']),
         ('C2', 'C1 without page-checksum checks', COLORS['grey']),
         ('C3', 'C1 to /dev/null, no WAL\n(no client disk writes)', COLORS['grey']),
         ('C6', 'pg_basebackup, CRC32C manifest\n(PostgreSQL default)', COLORS['green']),
         ('C7', 'C6 without page-checksum checks', COLORS['green']),
         ('C4', '4 parallel readers of the data files\n(no pg_basebackup)', COLORS['blue'])]


def main():
    plotting.style()
    rows = list(csv.DictReader(RUNS.open()))
    assert all(r['exit_code'] == '0' for r in rows)
    fig, ax = plt.subplots(figsize=(9.5, 4.6))
    for i, (variant, label, color) in enumerate(ORDER):
        walls = [float(r['wall_s']) for r in rows if r['variant'] == variant]
        med = statistics.median(walls)
        ax.barh(i, med, xerr=[[med - min(walls)], [max(walls) - med]], color=color, capsize=3)
        ax.text(max(walls) + 0.12, i, f'{med:.2f} s', va='center', fontsize=9)
    ax.axvline(8.3, color=COLORS['red'], ls='--', lw=1.2)
    ax.text(8.15, 4.0, 'EXP-04: full copy over the\nunthrottled network, 8.3 s', ha='right',
            va='center', fontsize=8.5, color=COLORS['red'])
    ax.set_yticks(range(len(ORDER)), [label for _, label, _ in ORDER], fontsize=9)
    ax.invert_yaxis()
    ax.set_xlabel('Time to read a 2.1 GB cluster on pgd-source (s, median of 3, page cache emptied)')
    ax.set_title('EXP-07: SHA-256 manifests doubled every backup; reading the files directly is 12× faster',
                 fontsize=11)
    ax.grid(axis='y', visible=False)
    print(plotting.save(fig, 'exp07-capture'))


if __name__ == '__main__':
    main()
