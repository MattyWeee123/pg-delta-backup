"""EXP-59b's analysis: does pgBackRest with asynchronous archive-get still lose to pgdelta from a day-old backup?
(docs/experiments/EXP-59-pgbackrest-old-backup.md, "EXP-59b")

    .venv/bin/python analysis/exp59b.py [--sweep results/exp04/exp59b]

Uniform 1% after a 5% day's churn, at 0.5 Gbit/s and unthrottled: per speed, each method's medians of its run, its
copy's start (for pgBackRest the recovery through archive-get), their sum (time to a usable copy) and the WAL files
the recovery fetched. Scores 1 (deciding: pgdelta at most 0.8 of pgbackrest_zst_async's usable time at both speeds),
2 (asynchronous archive-get's recovery at most 0.7 of the synchronous variant's at both speeds) and 3 (every run
passes). Writes docs/experiments/data/exp59b-cells.csv and the figure exp59b-async.
"""
from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path

import plotting
from exp59 import num
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
PGDELTA, SYNC, ASYNC, FULL = 'pgdelta_direct_inplace', 'pgbackrest_zst', 'pgbackrest_zst_async', 'full'
METHODS = (PGDELTA, SYNC, ASYNC, FULL)
LABELS = {PGDELTA: 'pgdelta', SYNC: 'pgBackRest, archive-get one segment a call',
          ASYNC: 'pgBackRest, asynchronous archive-get', FULL: 'pg_basebackup'}


def cells(sweep):
    rows = [r for r in csv.DictReader(open(Path(sweep) / 'summary.csv')) if r['method'] in METHODS]
    out = []
    for speed in ('0.5g', 'unthrottled'):
        row = {'speed': speed}
        for m in METHODS:
            mine = [r for r in rows if r['speed'] == speed and r['method'] == m and r['status'] == 'pass']
            if not mine:
                continue
            row.update({f'{m}_run_s': round(statistics.median(num(r['total_s']) for r in mine), 2),
                        f'{m}_start_s': round(statistics.median(num(r['output_start_s']) or 0 for r in mine), 2),
                        f'{m}_usable_s': round(statistics.median(num(r['total_s']) + (num(r['output_start_s']) or 0)
                                                                 for r in mine), 2),
                        f'{m}_wal_files': statistics.median(num(r.get('recovery_wal_files')) or 0 for r in mine),
                        f'{m}_runs': len(mine)})
        out.append(row)
    return out, [r['status'] for r in rows]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--sweep', default=str(ROOT / 'results' / 'exp04' / 'exp59b'))
    a = ap.parse_args(argv)
    rows, statuses = cells(a.sweep)
    for r in rows:
        print(r)
    ratio = [(r['speed'], round(r[f'{PGDELTA}_usable_s'] / r[f'{ASYNC}_usable_s'], 3)) for r in rows
             if f'{PGDELTA}_usable_s' in r and f'{ASYNC}_usable_s' in r]
    print(f"1 (deciding). {'HELD' if len(ratio) == 2 and all(x <= 0.8 for _, x in ratio) else 'FAILED'}: "
          f"pgdelta / asynchronous pgBackRest, usable time: {ratio}")
    faster = [(r['speed'], round(r[f'{ASYNC}_start_s'] / r[f'{SYNC}_start_s'], 3)) for r in rows
              if r.get(f'{SYNC}_start_s') and f'{ASYNC}_start_s' in r]
    print(f"2. {'HELD' if len(faster) == 2 and all(x <= 0.7 for _, x in faster) else 'FAILED'}: asynchronous / "
          f"one-segment recovery: {faster}")
    print(f"3. {'HELD' if statuses and all(s == 'pass' for s in statuses) else 'FAILED'}: {len(statuses)} runs, "
          f"statuses {sorted(set(statuses))}")
    keys = sorted({k for r in rows for k in r}, key=lambda k: (k != 'speed', k))
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp59b-cells.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, restval='')
        w.writeheader()
        w.writerows(rows)
    figure(rows)


def figure(rows):
    plotting.style()
    fig, ax = plt.subplots(figsize=(8.4, 4.2))
    colors = {PGDELTA: COLORS['blue'], SYNC: COLORS['orange'], ASYNC: COLORS['red'], FULL: COLORS['grey']}
    for i, r in enumerate(rows):
        for j, m in enumerate(METHODS):
            if f'{m}_usable_s' not in r:
                continue
            x = i * 5 + j
            ax.bar(x, r[f'{m}_run_s'], 0.8, color=colors[m], label=LABELS[m] if i == 0 else None)
            if r[f'{m}_start_s'] > 0.5:
                ax.bar(x, r[f'{m}_start_s'], 0.8, bottom=r[f'{m}_run_s'], color=colors[m], alpha=0.4, hatch='//',
                       label='recovery before use' if (i == 0 and m == SYNC) else None)
            ax.annotate(f"{r[f'{m}_usable_s']:.1f}", (x, r[f'{m}_usable_s']), textcoords='offset points',
                        xytext=(0, 3), ha='center', fontsize=8)
    ax.set_xticks([i * 5 + 1.5 for i in range(len(rows))],
                  ['0.5 Gbit/s' if r['speed'] == '0.5g' else 'unthrottled' for r in rows])
    ax.set_ylabel('seconds to a usable copy (median)')
    ax.legend(fontsize=8)
    ax.set_title("Scattered changes after a day's churn: pgBackRest's WAL fetched one segment a call or asynchronously",
                 fontsize=10)
    print(plotting.save(fig, 'exp59b-async'))


if __name__ == '__main__':
    main()
