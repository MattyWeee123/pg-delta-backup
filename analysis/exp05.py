"""EXP-05 (parallel hashing with threads) figure from the curated run table.

    .venv/bin/python analysis/exp05.py
Reads docs/experiments/data/exp05-runs.csv (copied from results/exp05/results.csv of the
benchmark run) and writes docs/figures/exp05-threads.svg.
"""
from __future__ import annotations

import csv
import statistics
from pathlib import Path

import plotting
from plotting import COLORS, PATTERN_COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / 'docs' / 'experiments' / 'data' / 'exp05-runs.csv'
PATTERNS = ['uniform', 'hot', 'append']
JOBS = [1, 2, 4, 8]


def load():
    rows = list(csv.DictReader(RUNS.open()))
    assert all(r['identical'] == 'True' and r['exit_code'] == '0' for r in rows)
    return rows


def cells(rows, pattern, key):
    out = []
    for jobs in JOBS:
        values = [float(r[key]) for r in rows if r['pattern'] == pattern and int(r['jobs']) == jobs]
        out.append((statistics.median(values), min(values), max(values)))
    return out


def fig_threads(rows):
    fig, (wall_ax, cpu_ax, phase_ax) = plt.subplots(1, 3, figsize=(13.5, 4.2))
    for pattern in PATTERNS:
        color = PATTERN_COLORS[pattern]
        for ax, key in ((wall_ax, 'wall_external_s'), (cpu_ax, 'cpu_s')):
            stats = cells(rows, pattern, key)
            med = [s[0] for s in stats]
            err = [[s[0] - s[1] for s in stats], [s[2] - s[0] for s in stats]]
            ax.errorbar(JOBS, med, yerr=err, color=color, marker='o', capsize=3, label=pattern)
    phases = [('hash_source_s', 'hash S (source)'), ('hash_basis_s', 'hash B (destination)'),
              ('rebuild_s', 'write O (destination)'), ('verify_s', 're-hash O (destination)')]
    styles = [('-', 'o', COLORS['blue']), ('--', 's', COLORS['sky']), (':', 'D', COLORS['red']),
              ('-.', '^', COLORS['purple'])]
    for (key, label), (ls, marker, color) in zip(phases, styles):
        phase_ax.plot(JOBS, [s[0] for s in cells(rows, 'uniform', key)], ls=ls, marker=marker, color=color,
                      label=label)
    for ax in (wall_ax, cpu_ax, phase_ax):
        ax.set_xscale('log', base=2)
        ax.set_xticks(JOBS, [str(j) for j in JOBS])
        ax.set_xlabel('Hashing threads per process (--jobs)')
        ax.set_ylim(bottom=0)
    wall_ax.set_ylabel('Seconds')
    wall_ax.set_title('Wall time (median, min–max of 3)', fontsize=10.5)
    cpu_ax.set_title('CPU seconds (user + system)', fontsize=10.5)
    phase_ax.set_title('Uniform 1%: time in each phase (median)', fontsize=10.5)
    wall_ax.legend(fontsize=8.5)
    phase_ax.legend(fontsize=8)
    fig.suptitle('EXP-05: more hashing threads made pgdelta slower past 2–4, and burned CPU (2.1 GB, 1% of rows changed)',
                 fontsize=11.5)
    return plotting.save(fig, 'exp05-threads')


def main():
    plotting.style()
    print(fig_threads(load()))


if __name__ == '__main__':
    main()
