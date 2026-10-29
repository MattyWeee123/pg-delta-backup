"""Rule 17's noise floor: how far two runs of one cell differ within a session, and how far an unchanged arm moves
between sessions, on the ha set beyond memory (EXP-64 to EXP-81). No new data.

    .venv/bin/python analysis/noise_floor.py

Within a session: for every cell (session, pattern, arm) with two or more runs, the spread of its runs over their
median, |max - min| / median. Cells are split by size, under and over 10 s, since fixed costs weigh more in short
runs. Between sessions: pg_basebackup's median in the same cell built the same way, across sessions (unchanged code;
its time depends only on the disk and the database). Writes docs/research/noise-floor.csv and the figure noise-floor.
"""
from __future__ import annotations

import csv
import statistics
from pathlib import Path

import exp68
import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
SESSIONS = ('exp64', 'exp64b', 'exp65', 'exp67', 'exp68', 'exp70', 'exp71', 'exp74', 'exp75', 'exp76', 'exp77', 'exp79',
            'exp79b', 'exp80b', 'exp81')
# pg_basebackup cells built the same way in several sessions: (pattern, the sessions that ran it so).
FULL_SAME_CELL = {'scatterall12': ('exp75', 'exp76', 'exp77', 'exp79b'),
                  'uniform': ('exp64', 'exp64b', 'exp68', 'exp70', 'exp80b')}


def quantile(values, q):
    values = sorted(values)
    position = q * (len(values) - 1)
    low, high = int(position), min(int(position) + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def main():
    rows, spreads = [], []
    for session in SESSIONS:
        results = ROOT / 'results' / 'exp04' / session
        if not results.exists():
            continue
        try:
            runs = exp68.runs(results)
        except (KeyError, ValueError):  # another lab's records (EXP-65's and EXP-67's): left out
            print(f'{session}: not lab_exp64.sh records, left out')
            continue
        for pattern, arm in sorted({(r['pattern'], r['arm']) for r in runs}):
            seconds = [r['seconds'] for r in runs if r['pattern'] == pattern and r['arm'] == arm
                       and r['status'] == 0 and r['seconds']]
            if len(seconds) >= 2:
                median = statistics.median(seconds)
                spread = (max(seconds) - min(seconds)) / median
                spreads.append((median, spread))
                rows.append({'kind': 'within', 'session': session, 'pattern': pattern, 'arm': arm, 'runs': len(seconds),
                             'median_s': round(median, 2), 'spread': round(spread, 4)})
    between = {}
    for pattern, sessions in FULL_SAME_CELL.items():
        medians = []
        for session in sessions:
            results = ROOT / 'results' / 'exp04' / session
            if results.exists():
                seconds = [r['seconds'] for r in exp68.runs(results) if r['pattern'] == pattern and r['arm'] == 'full'
                           and r['status'] == 0 and r['seconds']]
                if seconds:
                    medians.append((session, statistics.median(seconds)))
        if len(medians) >= 2:
            values = [m for _, m in medians]
            between[pattern] = (max(values) - min(values)) / statistics.median(values)
            rows += [{'kind': 'between', 'session': s, 'pattern': pattern, 'arm': 'full', 'median_s': round(m, 2),
                      'spread': round(between[pattern], 4)} for s, m in medians]
    short = [s for m, s in spreads if m < 10]
    long = [s for m, s in spreads if m >= 10]
    print(f'within a session, runs of 10 s or more: {len(long)} cells, median spread {statistics.median(long):.2%}, '
          f'95th percentile {quantile(long, 0.95):.2%}, largest {max(long):.2%}')
    print(f'within a session, runs under 10 s: {len(short)} cells, median spread {statistics.median(short):.2%}, '
          f'95th percentile {quantile(short, 0.95):.2%}, largest {max(short):.2%}')
    print('between sessions, pg_basebackup in the same cell: '
          + ', '.join(f'{p} {v:.2%}' for p, v in between.items()))
    out = ROOT / 'docs' / 'research' / 'noise-floor.csv'
    keys = []
    for row in rows:
        keys += [k for k in row if k not in keys]
    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, restval='')
        w.writeheader()
        w.writerows(rows)
    plotting.style()
    fig, ax = plt.subplots(figsize=(7.4, 4.0))
    ax.scatter([m for m, _ in spreads], [s * 100 for _, s in spreads], s=14, color=COLORS['blue'],
               label='a cell\'s runs within a session (spread over median)')
    for value in between.values():
        ax.axhline(value * 100, color=COLORS['orange'], lw=1, ls='--')
    ax.text(150, max(between.values()) * 100 + 0.15, 'pg_basebackup in the same cell, between sessions: '
            + ', '.join(f'{v:.2%}' for v in between.values()), fontsize=7.5, color=COLORS['orange'], ha='right')
    top = 10
    for median, spread in spreads:
        if spread * 100 > top:
            ax.annotate(f'{spread:.0%}: EXP-79\'s cell disturbed by\nbackground writes on the source', (median, top),
                        textcoords='offset points', xytext=(-10, -70), ha='right', fontsize=7.5, color=COLORS['red'],
                        arrowprops={'arrowstyle': '->', 'color': COLORS['red'], 'lw': 0.8})
    ax.axhline(quantile(long, 0.95) * 100, color=COLORS['grey'], lw=1, ls=':')
    ax.text(150, quantile(long, 0.95) * 100 + 0.1, '95th percentile, runs of 10 s or more', fontsize=7.5,
            color=COLORS['grey'], ha='right')
    ax.set_xscale('log')
    ax.set_ylim(0, top)
    ax.set_xlabel('the cell\'s median run (s, log scale)')
    ax.set_ylabel('spread (%)')
    ax.set_title('Rule 17\'s noise floor: the ha set beyond memory, EXP-64 to EXP-79b', fontsize=10)
    ax.legend(fontsize=8, loc='upper center')
    print(plotting.save(fig, 'noise-floor'))
    print(out)


if __name__ == '__main__':
    main()
