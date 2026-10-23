"""EXP-04b: crossovers and phases with SHA-256 manifests (EXP-04) against CRC32C (EXP-04b).

    .venv/bin/python analysis/exp04b.py
Reads results/exp04/full/summary.csv and results/exp04/crc32c/summary.csv (from `remote.py fetch`)
and writes docs/figures/exp04b-crossover.svg and exp04b-phases.svg. The per-sweep figures come from
analysis/exp04.py (run it with --tag=-crc32c on the second sweep).
"""
from __future__ import annotations

import statistics
from pathlib import Path

import exp04
import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
SWEEPS = [('SHA-256 manifests (EXP-04)', ROOT / 'results/exp04/full/summary.csv', 'none'),
          ('CRC32C, PostgreSQL default (EXP-04b)', ROOT / 'results/exp04/crc32c/summary.csv', None)]
DELTA = [(m, label, color) for m, label, color, *_ in exp04.METHODS if m != 'full']


def fig_crossover(tables):
    fig, axes = plt.subplots(1, 3, figsize=(14.5, 4.6), sharey=True)
    width = 0.38
    for ax, pattern in zip(axes, exp04.PATTERNS):
        for k, ((name, _, hatch), table) in enumerate(zip(SWEEPS, tables)):
            for i, (method, label, color) in enumerate(DELTA):
                value, how = exp04.crossover(table, pattern, method)
                top = how.startswith('faster at every speed')
                ax.bar(i + (k - 0.5) * width, value, width, color=color, alpha=0.45 if k == 0 else 1.0,
                       hatch='//' if k == 0 else None, edgecolor='white')
                ax.text(i + (k - 0.5) * width, value * 1.04, ('≥' if top else '') + f'{value:.1f}',
                        ha='center', va='bottom', fontsize=7)
        short = {'incremental': 'incremental\n+ combine', 'rsync_default': 'rsync\ndefault', 'rsync_8k': 'rsync\n8 KiB',
                 'pgdelta_1thread': 'pgdelta\n1 thread', 'pgdelta': 'pgdelta\n4 processes'}
        ax.set_xticks(range(len(DELTA)), [short[m] for m, _, _ in DELTA], fontsize=8)
        ax.set_yscale('log')
        exp04.plain_log(ax)
        ax.set_title(f'{pattern} 1%', fontsize=11)
    axes[0].set_ylabel('Link speed below which it beats a full copy (Gbit/s)')
    from matplotlib.patches import Patch
    fig.legend([Patch(facecolor=COLORS['grey'], alpha=0.45, hatch='//', edgecolor='white'),
                Patch(facecolor=COLORS['grey'])], [SWEEPS[0][0], SWEEPS[1][0]], loc='lower center', ncol=2,
               fontsize=9, bbox_to_anchor=(0.5, -0.04))
    fig.suptitle('EXP-04b: with PostgreSQL\'s default checksums the staged crossovers move up; incremental\'s lead at full speed narrows',
                 fontsize=12)
    return plotting.save(fig, 'exp04b-crossover')


def fig_phases(rows_by_sweep):
    """Unthrottled link, hot 1%: the phase each sweep spent time in, per method."""
    fig, ax = plt.subplots(figsize=(10.5, 4.8))
    parts = [('capture of S / incremental capture', ('prep_capture_s', 'receiver_capture_s', 'capture_s'), COLORS['grey']),
             ('transfer', ('transfer_s',), COLORS['blue']), ('pg_combinebackup', ('combine_s',), COLORS['green'])]
    labels = []
    y = 0
    for method, label, *_ in exp04.METHODS:
        for k, ((name, _, _), rows) in enumerate(zip(SWEEPS, rows_by_sweep)):
            group = [r for r in rows if r['pattern'] == 'hot' and r['method'] == method and r['speed'] == 'unthrottled']
            left = 0.0
            for _, cols, color in parts:
                vals = []
                for r in group:
                    v = next((exp04.num(r[c]) for c in cols if exp04.num(r.get(c))), 0.0)
                    vals.append(v)
                width = statistics.median(vals)
                ax.barh(y, width, left=left, color=color, alpha=0.45 if k == 0 else 1.0)
                left += width
            ax.text(left + 0.1, y, f'{left:.1f} s', va='center', fontsize=8)
            labels.append(f"{label}  [{'SHA-256' if k == 0 else 'CRC32C'}]")
            y += 1
        y += 0.5
    ticks = [i + (i // 2) * 0.5 for i in range(len(labels))]
    ax.set_yticks(ticks, labels, fontsize=7.5)
    ax.invert_yaxis()
    ax.set_xlabel('Median seconds, hot 1%, unthrottled link (sum of the phases on the critical path)')
    from matplotlib.patches import Patch
    fig.legend([Patch(color=c) for _, _, c in parts], [n for n, _, _ in parts], fontsize=8, loc='lower center',
               ncol=3, bbox_to_anchor=(0.6, -0.07))
    ax.set_title('EXP-04b: CRC32C halves the full copy and the capture of S and cuts the combine by 38%;\n'
                 'pgdelta\'s and rsync\'s own transfers are unchanged (pale bars: SHA-256, EXP-04)', fontsize=10.5)
    ax.grid(axis='y', visible=False)
    return plotting.save(fig, 'exp04b-phases')


def fig_winners(table):
    """Which method is fastest at each workload and link speed (CRC32C sweep): with and without incremental."""
    names = {m: (label, color) for m, label, color, *_ in exp04.METHODS}
    speeds = sorted({k[1] for k in table}, key=lambda sp: next(v['speed_gbit'] for k, v in table.items() if k[1] == sp))
    fig, axes = plt.subplots(2, 1, figsize=(11.5, 5.6), sharex=True)
    for ax, allowed, title in ((axes[0], None, 'Fastest method, any old copy that PostgreSQL incremental can use'),
                               (axes[1], {'full', 'rsync_default', 'rsync_8k', 'pgdelta_1thread', 'pgdelta'},
                                'Fastest method when incremental can\'t be used (no shared backup chain, after failover, restored elsewhere)')):
        for row, pattern in enumerate(exp04.PATTERNS):
            for col, speed in enumerate(speeds):
                cell = table[(pattern, speed)]
                options = {m: e['total_s'] for m, e in cell.items() if m != 'speed_gbit' and (allowed is None or m in allowed)}
                best = min(options, key=options.get)
                runner = sorted(options.values())[1]
                label, color = names[best]
                ax.add_patch(plt.Rectangle((col, row), 1, 1, color=color, alpha=0.9))
                ax.text(col + 0.5, row + 0.5, f"{label.split(' (')[0].replace('PG17 incremental + combine', 'incremental')}\n"
                        f"{options[best]:.1f} s ({runner / options[best]:.2f}× ahead)", ha='center', va='center',
                        fontsize=7.2, color='white')
        ax.set_xlim(0, len(speeds))
        ax.set_ylim(len(exp04.PATTERNS), 0)
        ax.set_yticks([i + 0.5 for i in range(len(exp04.PATTERNS))], [f'{p} 1%' for p in exp04.PATTERNS])
        ax.set_title(title, fontsize=10)
        ax.grid(False)
    gbit = [next(v['speed_gbit'] for k, v in table.items() if k[1] == sp) for sp in speeds]
    axes[1].set_xticks([i + 0.5 for i in range(len(speeds))],
                       [f'{g:.2g} Gbit/s' if sp != 'unthrottled' else f'unthrottled\n({g:.0f} Gbit/s)' for g, sp in zip(gbit, speeds)])
    fig.suptitle('EXP-04b: who wins where (2.1 GB, 1% of rows changed, PostgreSQL\'s default checksums)', fontsize=12)
    return plotting.save(fig, 'exp04b-winners')


def main():
    plotting.style()
    rows_by_sweep, tables = [], []
    for _, path, _ in SWEEPS:
        rows, _total = exp04.load(path)
        rows_by_sweep.append(rows)
        tables.append(exp04.cells(rows))
    print(fig_crossover(tables))
    print(fig_phases(rows_by_sweep))
    print(fig_winners(tables[1]))


if __name__ == '__main__':
    main()
