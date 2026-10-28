"""EXP-70: post-hoc check, written after the data (docs/experiments/EXP-70-fill-gaps.md, "Post-hoc check"). It
explains why filling 2 pages gained so little over filling 1; it changes no verdict.

    .venv/bin/python analysis/exp70_posthoc.py

For each uniform arm, both disks' requests and bytes per second of busy time (/proc/diskstats' io_ticks), from the
registered analysis's cells (docs/experiments/data/exp70-cells.csv), set against the disk's two limits: EXP-66's
operation rate (4,195 requests a second at 8 KiB) and the full copy's own bytes per busy second in this session, a
sequential read and write of 18 GB. Writes docs/experiments/data/exp70-posthoc-rates.csv and the figure
exp70-posthoc-rates.
"""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

import exp66
import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'docs' / 'experiments' / 'data'
ARMS = ('fast8w', 'fast8wf1', 'fast8wf2', 'fast8wf4', 'full')
LABELS = {'fast8w': 'no fill', 'fast8wf1': 'fill 1', 'fast8wf2': 'fill 2', 'fast8wf4': 'fill 4', 'full': 'full copy'}
SIDES = (('source', 'source_reads', 'source_read_mb', 'source_io_s'),
         ('dest', 'dest_writes', 'dest_write_mb', 'dest_io_s'))


def rates():
    with open(DATA / 'exp70-cells.csv') as f:
        cells = {r['arm']: r for r in csv.DictReader(f) if r['pattern'] == 'uniform'}
    rows = []
    for arm in ARMS:
        cell = cells[arm]
        row = {'arm': arm, 'seconds': float(cell['seconds'])}
        for side, requests, mb, busy in SIDES:
            busy_s = float(cell[busy])
            row[f'{side}_busy_s'] = busy_s
            row[f'{side}_busy_share'] = round(busy_s / row['seconds'], 3)
            row[f'{side}_requests_per_busy_s'] = round(float(cell[requests]) / busy_s)
            row[f'{side}_mb_per_busy_s'] = round(float(cell[mb]) / busy_s, 1)
            row[f'{side}_kib_per_request'] = round(float(cell[mb]) * 1e6 / float(cell[requests]) / 1024, 1)
        rows.append(row)
    return rows


def main():
    rows = rates()
    ops_cap = exp66.load(ROOT / 'results' / 'exp04' / 'exp66')['randread-8k-qd32']['ops_per_s']
    full = rows[-1]
    for row in rows:
        for side, *_ in SIDES:
            row[f'{side}_of_op_cap'] = round(row[f'{side}_requests_per_busy_s'] / ops_cap, 3)
            row[f'{side}_of_full_bytes'] = round(row[f'{side}_mb_per_busy_s'] / full[f'{side}_mb_per_busy_s'], 3)
        print(row)
    with open(DATA / 'exp70-posthoc-rates.csv', 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    figure(rows)


def figure(rows):
    plotting.style()
    fig, axes = plt.subplots(1, 2, figsize=(10.6, 4.0))
    x = np.arange(len(rows))
    for ax, (key, title, ylabel) in zip(axes, (
            ('of_op_cap', "Requests per busy second, against EXP-66's 4,195", 'share of the operation limit'),
            ('of_full_bytes', "Bytes per busy second, against the full copy's", "share of the full copy's rate"))):
        for j, (side, label, color) in enumerate((('source', 'source reads', COLORS['green']),
                                                  ('dest', 'destination writes', COLORS['red']))):
            values = [row[f'{side}_{key}'] for row in rows]
            ax.bar(x + (j - 0.5) * 0.36, values, 0.36, color=color, label=label)
        ax.axhline(1, color=COLORS['grey'], linestyle='--', linewidth=1)
        ax.set_xticks(x, [LABELS[row['arm']] for row in rows], fontsize=8.5)
        ax.set_ylim(0, 1.3)
        ax.set_ylabel(ylabel)
        ax.set_title(title, fontsize=9.5)
        ax.legend(fontsize=7.5, loc='upper left', ncol=2)
    fig.suptitle('EXP-70, after the data: past one filled page the source reads at the full copy\'s byte rate',
                 fontsize=10.5)
    fig.tight_layout()
    print(plotting.save(fig, 'exp70-posthoc-rates'))


if __name__ == '__main__':
    main()
