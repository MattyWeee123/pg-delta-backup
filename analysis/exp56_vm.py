"""EXP-56's analysis, stage 2 (one VM session): sub-page deltas on a DR link (docs/experiments/EXP-56-subpage-build.md).

    .venv/bin/python analysis/exp56_vm.py [--sweep results/exp04/exp56] ...

Uniform 1% at 0.5 and 1 Gbit/s and 60 ms: each method's median time and bytes, and v1 + zstd + sub-page against
v1 + zstd within the session. Scores:
3 (deciding) at 0.5 Gbit/s, zstd + sub-page at most 0.80 of zstd's time;
4 at 1 Gbit/s, at most 0.95;
5 every run passes, and BBR is in effect on every v1 run.
Writes docs/experiments/data/exp56-vm-cells.csv and the figure exp56-vm.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import plotting
from exp47 import FULL, V1Z, medians
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
V1ZS = 'pgdelta_direct_inplace_bbr_zs'
CONDITIONS = {'0.5:60:tuned': 0.5, '1:60:tuned': 1.0}
BARS = {'0.5:60:tuned': 0.80, '1:60:tuned': 0.95}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--sweep', action='append')
    a = ap.parse_args(argv)
    sweeps = a.sweep or [str(ROOT / 'results' / 'exp04' / 'exp56')]
    t, mb, n = medians(sweeps)
    rows = []
    for condition, rate in CONDITIONS.items():
        row = {'condition': condition, 'gbit': rate,
               **{f'{name}_s': round(t[(condition, m)], 3) for m, name in ((FULL, 'full'), (V1Z, 'zstd'), (V1ZS, 'zstd_subpage'))},
               **{f'{name}_mb': round(mb.get((condition, m), 0), 1) for m, name in ((FULL, 'full'), (V1Z, 'zstd'), (V1ZS, 'zstd_subpage'))},
               **{f'{name}_runs': n.get((condition, m), 0) for m, name in ((FULL, 'full'), (V1Z, 'zstd'), (V1ZS, 'zstd_subpage'))}}
        row['subpage_over_zstd'] = round(row['zstd_subpage_s'] / row['zstd_s'], 3)
        row['subpage_over_full'] = round(row['zstd_subpage_s'] / row['full_s'], 3)
        rows.append(row)
        print(row)
    half, one = rows
    r = half['subpage_over_zstd']
    print(f"3 (deciding). {'HELD' if r <= 0.80 else 'FAILED'}: 0.5 Gbit/s, zstd + sub-page / zstd = {r}"
          + (' (within 20% of the bar: rule 11 asks for a second session)' if abs(r / 0.80 - 1) < 0.2 else ''))
    print(f"4. {'HELD' if one['subpage_over_zstd'] <= 0.95 else 'FAILED'}: 1 Gbit/s, {one['subpage_over_zstd']}")
    effective, statuses = [], []
    for sweep in sweeps:
        for row in csv.DictReader(open(Path(sweep) / 'summary.csv')):
            statuses.append(row['status'])
        for log in sorted(Path(sweep).glob('logs/*/source/*-stats.json.log')):
            text = log.read_text()
            stats = json.loads(text[text.find('{'):])
            effective.append((stats.get('congestion_control') or {}).get('effective'))
    ok = all(s == 'pass' for s in statuses) and effective and all(e == 'bbr' for e in effective)
    print(f"5. {'HELD' if ok else 'FAILED'}: statuses {sorted(set(statuses))}; BBR on "
          f"{sum(e == 'bbr' for e in effective)} of {len(effective)} v1 runs")
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp56-vm-cells.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    figure(rows)


def figure(rows):
    """Median seconds per method at each link rate, with the bytes each sent."""
    plotting.style()
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    methods = (('full', 'pg_basebackup', COLORS['grey']), ('zstd', 'v1 + zstd', COLORS['red']),
               ('zstd_subpage', 'v1 + zstd + sub-page', COLORS['green']))
    for g, row in enumerate(rows):
        for i, (key, name, color) in enumerate(methods):
            x = g * 4 + i
            ax.bar(x, row[f'{key}_s'], 0.8, color=color, label=name if g == 0 else None)
            ax.annotate(f"{row[f'{key}_s']:.1f} s\n{row[f'{key}_mb']:.0f} MB", (x, row[f'{key}_s']),
                        textcoords='offset points', xytext=(0, 3), ha='center', fontsize=7.5)
        ax.text(g * 4 + 1, -0.1, f"{row['gbit']:g} Gbit/s, 60 ms", transform=ax.get_xaxis_transform(), ha='center',
                va='top', fontsize=9)
    ax.set_xticks([])
    ax.set_ylabel('median seconds, uniform 1%')
    ax.set_title('Sub-page deltas on a DR link (one session)', fontsize=10)
    ax.legend(fontsize=8)
    fig.tight_layout()
    print(plotting.save(fig, 'exp56-vm'))


if __name__ == '__main__':
    main()
