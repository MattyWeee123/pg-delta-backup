"""EXP-59's analysis: pgBackRest from a day-old backup against pgdelta, timed to a usable copy
(docs/experiments/EXP-59-pgbackrest-old-backup.md)

    .venv/bin/python analysis/exp59.py [--sweep results/exp04/exp59]

Per pattern and speed, each method's medians of: its run's time (total_s), its copy's start (output_start_s: for
pgBackRest the recovery through archive-get), their sum (time to a usable copy) and its bytes (source to dest, with
the recovery's WAL). Scores 1 (deciding: pgdelta's usable time at most 0.8 of pgbackrest_zst's in every cell),
2 (pgBackRest moves more bytes in every cell) and 3 (every run passes). Writes docs/experiments/data/exp59-cells.csv
and the figure exp59-old-backup.
"""
from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
PGDELTA, PGBR, FULL = 'pgdelta_direct_inplace', 'pgbackrest_zst', 'full'
METHODS = (PGDELTA, PGBR, FULL)
LABELS = {PGDELTA: 'pgdelta', PGBR: 'pgBackRest (zstd)', FULL: 'pg_basebackup'}
SPEEDS = ('0.5g', '2g', 'unthrottled')  # the harness's speed labels


def num(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def cells(sweep):
    rows = list(csv.DictReader(open(Path(sweep) / 'summary.csv')))
    out, statuses = [], [r['status'] for r in rows if r['method'] in METHODS]
    for pattern in ('uniform', 'hot'):
        for speed in sorted({r['speed'] for r in rows}, key=lambda s: SPEEDS.index(s) if s in SPEEDS else 9):
            row = {'pattern': pattern, 'speed': speed}
            for m in METHODS:
                mine = [r for r in rows if r['pattern'] == pattern and r['speed'] == speed and r['method'] == m
                        and r['status'] == 'pass']
                if not mine:
                    continue
                run = statistics.median(num(r['total_s']) for r in mine)
                start = statistics.median(num(r['output_start_s']) or 0.0 for r in mine)
                usable = statistics.median(num(r['total_s']) + (num(r['output_start_s']) or 0.0) for r in mine)
                mb = statistics.median(((num(r['wire_s2d_bytes']) or 0) + (num(r['recovery_wire_s2d_bytes']) or 0))
                                       / 1e6 for r in mine)
                row.update({f'{m}_run_s': round(run, 2), f'{m}_start_s': round(start, 2),
                            f'{m}_usable_s': round(usable, 2), f'{m}_mb': round(mb, 1), f'{m}_runs': len(mine)})
            if f'{PGDELTA}_usable_s' in row and f'{PGBR}_usable_s' in row:
                row['pgdelta_over_pgbackrest'] = round(row[f'{PGDELTA}_usable_s'] / row[f'{PGBR}_usable_s'], 3)
            out.append(row)
    return out, statuses


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--sweep', default=str(ROOT / 'results' / 'exp04' / 'exp59'))
    a = ap.parse_args(argv)
    rows, statuses = cells(a.sweep)
    for r in rows:
        print(r)
    ratios = [r.get('pgdelta_over_pgbackrest') for r in rows]
    ok = ratios and all(x is not None and x <= 0.8 for x in ratios)
    near = [r for r in rows if r.get('pgdelta_over_pgbackrest') and abs(r['pgdelta_over_pgbackrest'] / 0.8 - 1) < 0.2]
    print(f"1 (deciding). {'HELD' if ok else 'FAILED'}: pgdelta / pgBackRest, usable time: "
          f"{[(r['pattern'], r['speed'], r.get('pgdelta_over_pgbackrest')) for r in rows]}"
          + (f"; within 20% of the bar: {[(r['pattern'], r['speed']) for r in near]}" if near else ''))
    more = all(r.get(f'{PGBR}_mb', 0) > r.get(f'{PGDELTA}_mb', float('inf')) for r in rows)
    print(f"2. {'HELD' if more else 'FAILED'}: MB moved, pgBackRest against pgdelta: "
          f"{[(r['pattern'], r['speed'], r.get(f'{PGBR}_mb'), r.get(f'{PGDELTA}_mb')) for r in rows]}")
    print(f"3. {'HELD' if statuses and all(s == 'pass' for s in statuses) else 'FAILED'}: statuses {sorted(set(statuses))}")
    keys = sorted({k for r in rows for k in r}, key=lambda k: (k not in ('pattern', 'speed'), k))
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp59-cells.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, restval='')
        w.writeheader()
        w.writerows(rows)
    figure(rows)


def figure(rows):
    """Time to a usable copy per cell: each method's run, with pgBackRest's recovery stacked on top."""
    plotting.style()
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.2), sharey=True)
    colors = {PGDELTA: COLORS['blue'], PGBR: COLORS['orange'], FULL: COLORS['grey']}
    for ax, pattern in zip(axes, ('uniform', 'hot')):
        mine = [r for r in rows if r['pattern'] == pattern]
        for i, r in enumerate(mine):
            for j, m in enumerate(METHODS):
                if f'{m}_usable_s' not in r:
                    continue
                x = i * 4 + j
                ax.bar(x, r[f'{m}_run_s'], 0.8, color=colors[m], label=LABELS[m] if i == 0 else None)
                if r[f'{m}_start_s'] > 0.5:
                    ax.bar(x, r[f'{m}_start_s'], 0.8, bottom=r[f'{m}_run_s'], color=colors[m], alpha=0.45,
                           hatch='//', label='recovery before use' if (i == 0 and m == PGBR) else None)
                ax.annotate(f"{r[f'{m}_usable_s']:.1f}", (x, r[f'{m}_usable_s']), textcoords='offset points',
                            xytext=(0, 3), ha='center', fontsize=7.5)
            ax.text(i * 4 + 1, -0.08, r['speed'].replace('g', ' Gbit/s') if r['speed'] != 'unthrottled' else 'unthrottled', transform=ax.get_xaxis_transform(),
                    ha='center', va='top', fontsize=8.5)
        ax.set_xticks([])
        ax.set_title('Scattered changes' if pattern == 'uniform' else 'Local changes', fontsize=10)
    axes[0].set_ylabel('seconds to a usable copy (median)')
    axes[0].legend(fontsize=8)
    fig.suptitle("pgBackRest from a backup a day's churn old (5% of rows) against pgdelta, 2.1 GB", fontsize=10)
    fig.tight_layout()
    print(plotting.save(fig, 'exp59-old-backup'))


if __name__ == '__main__':
    main()
