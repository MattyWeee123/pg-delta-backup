"""EXP-52's analysis: with batched pool tasks, does zstd beat plain v1 at 2 Gbit/s? (docs/experiments/EXP-52-batched-zstd-dr.md)

    .venv/bin/python analysis/exp52.py [--sweep results/exp04/exp52] ...

Uniform 1% at 2 Gbit/s and 60 ms: the median time of each method, zstd / v1 (1, deciding) and the bytes and
pg_basebackup (4); from pgdelta's statistics, the receiver's rebuild with zstd against v1's (2), and every zstd run
in batched tasks with BBR on every v1 run (3). The figure sets this beside EXP-47's two sessions (one run per task).
Writes docs/experiments/data/exp52-cells.csv and docs/figures/exp52-batched-dr.svg.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

import plotting
from exp47 import FULL, V1, V1Z, medians
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
CONDITION = '2:60:tuned'
BEFORE = [str(ROOT / 'results' / 'exp04' / s) for s in ('exp47', 'exp47b')]


def stats_of(sweeps):
    """(method, stats) for every pgdelta run's statistics."""
    for sweep in sweeps:
        for log in sorted(Path(sweep).glob('logs/*/source/*-stats.json.log')):
            text = log.read_text()
            method = V1Z if log.name.endswith('_bbr_z-stats.json.log') else V1
            yield method, json.loads(text[text.find('{'):])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--sweep', action='append', help='repeat for a second session: the cells use every run')
    a = ap.parse_args(argv)
    sweeps = a.sweep or [str(ROOT / 'results' / 'exp04' / 'exp52')]
    t, mb, n = medians(sweeps)
    row = {f'{name}_s': round(t[(CONDITION, m)], 2) for m, name in ((FULL, 'full'), (V1, 'v1'), (V1Z, 'v1_zstd'))}
    row.update({f'{name}_runs': n.get((CONDITION, m), 0) for m, name in ((FULL, 'full'), (V1, 'v1'), (V1Z, 'v1_zstd'))})
    row['v1_mb'], row['v1_zstd_mb'] = round(mb[(CONDITION, V1)], 1), round(mb[(CONDITION, V1Z)], 1)
    row['zstd_over_v1'] = round(row['v1_zstd_s'] / row['v1_s'], 3)
    row['zstd_over_full'] = round(row['v1_zstd_s'] / row['full_s'], 3)
    print(row)
    ratio = row['zstd_over_v1']
    print(f"1 (deciding). {'HELD' if ratio <= 0.90 else 'FAILED'}: 2 Gbit/s, zstd at most 0.90 of v1: {ratio}"
          + (' (within 20% of the bar: rule 11 asks for a second session)' if abs(ratio / 0.90 - 1) < 0.2 else ''))
    rebuild, tasks, effective = {V1: [], V1Z: []}, [], []
    for method, stats in stats_of(sweeps):
        rebuild[method].append(stats['timings_seconds']['rebuild'])
        if method == V1:
            effective.append((stats.get('congestion_control') or {}).get('effective'))
        else:
            c = stats.get('compress') or {}
            received = (stats.get('receiver') or {}).get('compress') or {}
            tasks.append((c.get('runs'), c.get('tasks'), received.get('tasks')))
    r = statistics.median(rebuild[V1Z]) / statistics.median(rebuild[V1])
    print(f"2. {'HELD' if r <= 1.3 else 'FAILED'}: the receiver's rebuild, zstd over v1: {r:.2f} "
          f"(medians {statistics.median(rebuild[V1Z]):.2f} and {statistics.median(rebuild[V1]):.2f} s; EXP-47: 2.1)")
    batched = bool(tasks) and all(s and rt and s < runs / 10 and rt < runs / 10 for runs, s, rt in tasks)
    bbr = bool(effective) and all(e == 'bbr' for e in effective)
    print(f"3. {'HELD' if batched and bbr else 'FAILED'}: (runs, sender tasks, receiver tasks) {tasks}; "
          f"BBR in effect on {sum(e == 'bbr' for e in effective)} of {len(effective)} v1 runs")
    fold = row['v1_mb'] / row['v1_zstd_mb']
    print(f"4. {'HELD' if fold >= 2.5 and row['zstd_over_full'] < 1 else 'FAILED'}: bytes {fold:.2f}x fewer; "
          f"against pg_basebackup {row['zstd_over_full']}")
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp52-cells.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(row) + ['rebuild_zstd_over_v1'])
        w.writeheader()
        w.writerow({**row, 'rebuild_zstd_over_v1': round(r, 3)})
    figure(t, medians(BEFORE)[0])


def figure(after, before):
    """Median seconds per method at 2 Gbit/s and 60 ms: EXP-47 (one run per task) and EXP-52 (batched)."""
    plotting.style()
    fig, ax = plt.subplots(figsize=(7, 4))
    methods = ((FULL, 'pg_basebackup', COLORS['grey']), (V1, 'v1', COLORS['blue']), (V1Z, 'v1 + zstd', COLORS['red']))
    for g, (label, t) in enumerate((('EXP-47: one run per task\n(two sessions)', before),
                                    ('EXP-52: batched tasks', after))):
        for i, (m, name, color) in enumerate(methods):
            x = g * 4 + i
            value = t.get((CONDITION, m))
            if value is None:
                continue
            ax.bar(x, value, 0.8, color=color, label=name if g == 0 else None)
            ax.annotate(f'{value:.2f}', (x, value), textcoords='offset points', xytext=(0, 3), ha='center', fontsize=8)
        ax.text(g * 4 + 1, -0.12, label, transform=ax.get_xaxis_transform(), ha='center', va='top', fontsize=8.5)
    ax.set_xticks([])
    ax.set_ylabel('median seconds, uniform 1%')
    ax.set_title('2 Gbit/s, 60 ms round trip', fontsize=10)
    ax.legend(fontsize=8)
    fig.tight_layout()
    print(plotting.save(fig, 'exp52-batched-dr'))


if __name__ == '__main__':
    main()
