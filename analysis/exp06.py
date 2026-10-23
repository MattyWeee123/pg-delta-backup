"""EXP-06 (worker processes, copy-then-patch, pipelined verification) figures.

    .venv/bin/python analysis/exp06.py
Reads the curated tables in docs/experiments/data/ (exp06-runs.csv: the primary, warm-each pass;
exp06-isolation-primary.csv: hashing one 1 GiB file) and writes docs/figures/exp06-*.svg.
"""
from __future__ import annotations

import csv
import statistics
from pathlib import Path

import plotting
from plotting import COLORS, METHOD_COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'docs' / 'experiments' / 'data'
PATTERNS = [('uniform', 'uniform 1%'), ('hot', 'hot (1% set) 1%'), ('append', 'append 1%')]
CONFIGS = [('baseline', 'baseline (EXP-05 engine, 1 thread)', COLORS['grey']),
           ('B', 'B: copy-then-patch, 1 thread', COLORS['yellow']),
           ('A-2', 'A: 2 worker processes', '#9ecae1'), ('A-4', 'A: 4 worker processes', COLORS['sky']),
           ('A-8', 'A: 8 worker processes', '#2b7bba'),
           ('ABC-2', 'A+B+C: 2 processes', '#a1d99b'), ('ABC-4', 'A+B+C: 4 processes (default)', COLORS['green']),
           ('ABC-8', 'A+B+C: 8 processes', '#00543c')]
# rsync's modelled crossover from EXP-02 (median over its trials), for reference.
RSYNC_CROSSOVER = {'uniform': (1.29, 1.55), 'hot': (13.39, 11.97), 'append': (13.45, 13.01)}


def load(name):
    return list(csv.DictReader((DATA / name).open()))


def cell(rows, pattern, config, key):
    values = [float(r[key]) for r in rows if r['pattern'] == pattern and r['config'] == config]
    return statistics.median(values), min(values), max(values)


def fig_wall(rows):
    fig, axes = plt.subplots(1, 3, figsize=(13.5, 4.6), sharex=True)
    for ax, (pattern, title) in zip(axes, PATTERNS):
        for i, (config, label, color) in enumerate(CONFIGS):
            med, lo, hi = cell(rows, pattern, config, 'wall_external_s')
            ax.barh(i, med, xerr=[[med - lo], [hi - med]], color=color, capsize=2.5,
                    edgecolor='black' if config == 'ABC-4' else 'none', linewidth=1.2)
            ax.text(hi + 0.08, i, f'{med:.2f} s', va='center', fontsize=8)
        ax.set_yticks(range(len(CONFIGS)), [label for _, label, _ in CONFIGS] if ax is axes[0] else [])
        ax.invert_yaxis()
        ax.set_title(title, fontsize=10.5)
        ax.set_xlabel('Wall time, median of 3 (s)')
        ax.grid(axis='y', visible=False)
    fig.suptitle('EXP-06: a 2.1 GB refresh on the Mac, by engine configuration (inputs cached)', fontsize=12)
    return plotting.save(fig, 'exp06-wall')


def fig_scaling(rows, isolation):
    fig, (hash_ax, cross_ax) = plt.subplots(1, 2, figsize=(12.0, 4.4))
    serial = next(float(r['gb_per_s']) for r in isolation if r['backend'] == 'serial')
    for backend, label, color, marker in (('process', 'worker processes (EXP-06)', COLORS['green'], 'o'),
                                          ('thread', 'threads (EXP-05)', COLORS['red'], 's')):
        points = sorted((int(r['jobs']), float(r['gb_per_s'])) for r in isolation if r['backend'] == backend)
        if backend == 'thread':
            points = [(1, serial)] + points
        hash_ax.plot([j for j, _ in points], [g for _, g in points], marker=marker, color=color, label=label)
    hash_ax.plot([1, 2, 4, 8], [n * serial for n in (1, 2, 4, 8)], ls=':', color=COLORS['black'], alpha=0.6,
                 label='perfect scaling')
    hash_ax.set_xscale('log', base=2)
    hash_ax.set_xticks([1, 2, 4, 8], ['1', '2', '4', '8'])
    hash_ax.set_xlabel('Workers hashing one 1 GiB file')
    hash_ax.set_ylabel('Hashing speed (GB/s)')
    hash_ax.set_title('Processes scale; threads stop at about 3', fontsize=10.5)
    hash_ax.legend(fontsize=8.5)

    width = 0.2
    for i, (config, label, color) in enumerate([('baseline', 'pgdelta, EXP-05 engine', COLORS['grey']),
                                                ('ABC-4', 'pgdelta, A+B+C, 4 processes', COLORS['green']),
                                                ('ABC-8', 'pgdelta, A+B+C, 8 processes', '#00543c')]):
        values = []
        for pattern, _ in PATTERNS:
            group = [r for r in rows if r['pattern'] == pattern and r['config'] == config]
            wall = statistics.median(float(r['wall_external_s']) for r in group)
            full = {'uniform': 2122342974, 'hot': 2115691070, 'append': 2125029950}[pattern]
            sent = float(group[0]['bytes_s2d']) + float(group[0]['bytes_d2s'])
            values.append((full - sent) * 8 / wall / 1e9)
        xs = [k + (i - 1.5) * width for k in range(len(PATTERNS))]
        cross_ax.bar(xs, values, width, color=color, label=label)
    rsync = [max(RSYNC_CROSSOVER[p]) for p, _ in PATTERNS]
    cross_ax.bar([k + 1.5 * width for k in range(len(PATTERNS))], rsync, width, color=METHOD_COLORS['rsync'],
                 label='rsync, best block size (EXP-02)')
    cross_ax.set_xticks(range(len(PATTERNS)), [title for _, title in PATTERNS])
    cross_ax.set_ylabel('Link speed below which it beats a full copy (Gbit/s)')
    cross_ax.set_title("Modelled crossover vs a full copy (EXP-02's model)", fontsize=10.5)
    cross_ax.legend(fontsize=8)
    fig.suptitle('EXP-06: worker processes make hashing scale, and lift the crossover to rsync\'s level for local changes',
                 fontsize=11.5)
    return plotting.save(fig, 'exp06-scaling')


def fig_verify4(rows):
    """Pipelined against sequential verification at --jobs 4: every run, per workload."""
    fig, ax = plt.subplots(figsize=(8.5, 4.2))
    for k, (pattern, title) in enumerate(PATTERNS):
        for j, (config, label, color) in enumerate((('AB-4', 'sequential verification (A+B, 4 processes)', COLORS['grey']),
                                                    ('ABC-4', 'pipelined verification (A+B+C, 4 processes, default)', COLORS['green']))):
            walls = sorted(float(r['wall_external_s']) for r in rows if r['pattern'] == pattern and r['config'] == config)
            x = k * 3 + j
            ax.scatter([x + (i - len(walls) / 2) * 0.03 for i in range(len(walls))], walls, color=color, s=18,
                       label=label if k == 0 else None, zorder=3)
            med = statistics.median(walls)
            ax.plot([x - 0.3, x + 0.3], [med, med], color=COLORS['black'], lw=1.5, zorder=4)
            ax.text(x + 0.33, med, f'{med:.2f}', va='center', fontsize=8)
    ax.set_xticks([k * 3 + 0.5 for k in range(len(PATTERNS))], [title for _, title in PATTERNS])
    ax.set_ylabel('Wall time per run (s); bar = median of 10')
    ax.set_title('EXP-06 follow-up: pipelined verification at the default 4 processes\n'
                 '(10 interleaved runs each, inputs cached; pipelined faster by 0.06–0.13 s in every workload)', fontsize=10)
    ax.legend(fontsize=8, loc='upper right')
    ax.grid(axis='x', visible=False)
    return plotting.save(fig, 'exp06-verify4')


def main():
    plotting.style()
    rows = load('exp06-runs.csv')
    assert all(r['identical'] == 'True' and r['exit_code'] == '0' for r in rows)
    print(fig_wall(rows))
    print(fig_scaling(rows, load('exp06-isolation-primary.csv')))
    print(fig_verify4(load('exp06-verify4.csv')))


if __name__ == '__main__':
    main()
