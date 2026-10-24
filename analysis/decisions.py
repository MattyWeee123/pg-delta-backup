"""Figures for docs/DECISIONS.md: where pgdelta's transfer time goes on Linux, and the evidence scorecard.

    .venv/bin/python analysis/decisions.py
Reads docs/experiments/data/exp04-pgdelta-timers.csv (pgdelta's own phase timers from the EXP-04b
sweep) and writes docs/figures/decisions-transfer.svg and decisions-scorecard.svg.
"""
from __future__ import annotations

import csv
import statistics
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
TIMERS = ROOT / 'docs' / 'experiments' / 'data' / 'exp04-pgdelta-timers.csv'
# Performance-decision grades from docs/DECISIONS.md (scorecard of the 33 performance-driven decisions).
SCORECARD = [('A: strong', 4, COLORS['green']), ('B: moderate', 4, COLORS['sky']), ('C: weak / indirect', 10, COLORS['yellow']),
             ('D: none (assumed)', 14, COLORS['grey']), ('X: contradicted', 1, COLORS['red'])]


def fig_transfer():
    rows = [r for r in csv.DictReader(TIMERS.open())
            if r['sweep'].startswith('EXP-04b') and r['method'] == 'pgdelta' and r['speed'] == 'unthrottled']
    fig, ax = plt.subplots(figsize=(9.5, 3.6))
    patterns = ['uniform', 'hot', 'append']
    for i, p in enumerate(patterns):
        g = [r for r in rows if r['pattern'] == p]
        med = lambda k: statistics.median(float(r[k]) for r in g)
        total = med('total_s')
        copy, fsync, rebuild = med('copy_basis_s'), med('fsync_s'), med('rebuild_s')
        other_write = max(rebuild - copy - fsync, 0)
        rest = max(total - rebuild, 0)
        parts = [('copy old copy B into O', copy, COLORS['red']), ('flush O to disk (fsync)', fsync, COLORS['purple']),
                 ('write received pages', other_write, COLORS['orange']),
                 ('hashing, protocol, waiting for verification', rest, COLORS['sky'])]
        left = 0
        for label, w, c in parts:
            ax.barh(i, w, left=left, color=c, label=label if i == 0 else None)
            left += w
        ax.text(left + 0.05, i, f'{total:.1f} s', va='center', fontsize=9)
    ax.set_yticks(range(len(patterns)), [f'{p} 1%' for p in patterns])
    ax.invert_yaxis()
    ax.set_xlabel("pgdelta's own phase timers, median of 3, unthrottled link, 4 worker processes (s)")
    ax.set_title("Where pgdelta's transfer goes on Linux (EXP-04b): writing a complete new copy is the biggest cost", fontsize=10.5)
    ax.legend(fontsize=8, loc='lower center', bbox_to_anchor=(0.5, -0.62), ncol=2)
    ax.grid(axis='y', visible=False)
    return plotting.save(fig, 'decisions-transfer')


LEDGER = ROOT / 'docs' / 'DECISIONS.md'
PERFORMANCE_SECTIONS = ('### Performance: the v0 engine', '### Performance: the v1 proposals')


def ledger_grades():
    """Grade counts of the rows in the ledger's two performance sections as the file stands now.

    A grade of '–' marks a decision made moot by a later one; those are counted apart."""
    counts, moot, section = {}, 0, None
    for line in LEDGER.read_text().splitlines():
        if line.startswith('### '):
            section = line.strip() if line.strip() in PERFORMANCE_SECTIONS else None
        elif section and line.startswith('| D'):
            grade = line.split(' | ')[2].strip().strip('*')
            if grade == '–':
                moot += 1
            else:
                counts[grade] = counts.get(grade, 0) + 1
    return counts, moot


def fig_scorecard():
    now, moot = ledger_grades()
    rows = [('October 23, 2026', {label[0]: n for label, n, _ in SCORECARD}),
            (f'October 24, 2026 (after T3, T8a, T11; {moot} now moot)', now)]
    fig, ax = plt.subplots(figsize=(8.5, 3.0))
    for y, (name, counts) in enumerate(rows):
        left = 0
        for label, _, c in SCORECARD:
            n = counts.get(label[0], 0)
            if n:
                ax.barh(y, n, left=left, color=c)
                ax.text(left + n / 2, y, f'{n}', ha='center', va='center', fontsize=10,
                        color='white' if c != COLORS['yellow'] else 'black')
            left += n
    ax.set_yticks(range(len(rows)), [name for name, _ in rows], fontsize=9)
    ax.invert_yaxis()
    ax.set_xlabel('Number of performance-driven design decisions')
    from matplotlib.patches import Patch
    ax.legend([Patch(color=c) for _, _, c in SCORECARD], [f'{l}' for l, _, _ in SCORECARD], ncol=5, fontsize=8,
              loc='upper center', bbox_to_anchor=(0.5, -0.35))
    fig.subplots_adjust(left=0.33, right=0.97, top=0.85, bottom=0.35)
    fig._layout_done = True
    ax.set_title('Evidence behind the performance-driven decisions (DECISIONS.md)', fontsize=10.5)
    ax.grid(False)
    return plotting.save(fig, 'decisions-scorecard')


def main():
    plotting.style()
    print(fig_transfer())
    print(fig_scorecard())


if __name__ == '__main__':
    main()
