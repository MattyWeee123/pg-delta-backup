"""EXP-31's analysis: EXP-22's HA scale model at 12 GB, v1 with and without its second pass over B (docs/experiments/EXP-31-ha-one-pass.md).

    .venv/bin/python analysis/exp31.py [--sweep results/exp04/sets/ha/exp31-s400] [--figdir DIR]

The median time of each arm per workload, v1's ratios to both full copies (K6: each yardstick apart), and v1's own
timers (the sender's statistics: hashing S and B, the write phase and its fsync, the wait for verification).
Writes docs/experiments/data/exp31-cells.csv and docs/figures/exp31-ha.svg.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

import numpy as np

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
ARMS = [('full', 'one pg_basebackup'), ('parcopy8', 'parallel copy (8)'), ('pgdelta_direct_inplace', 'v1'),
        ('pgdelta_direct_inplace_vw', 'v1, --verify-scope\nwritten')]
PATTERNS = [('hot', 'hot 1% (local)'), ('uniform', 'uniform 1% (scattered)')]
TIMERS = ['hash_basis', 'rebuild', 'fsync', 'verify_wait', 'total']


def timers(sweep, pattern, method):
    out = []
    for f in sorted((Path(sweep) / 'runs').glob(f'*-{pattern}-*/*-{method}.json')):
        tool = json.loads(f.read_text()).get('tool_reported') or {}
        stats = tool.get('stats', tool)
        found = stats.get('timings_seconds') if isinstance(stats, dict) else None
        if found is None:
            for value in tool.values():
                if isinstance(value, dict) and 'timings_seconds' in value:
                    found = value['timings_seconds']
        if found:
            out.append(found)
    return {k: statistics.median(t.get(k) or 0 for t in out) for k in TIMERS} if out else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--sweep', default=str(ROOT / 'results' / 'exp04' / 'sets' / 'ha' / 'exp31-s400'))
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    times = {}
    for r in csv.DictReader(open(Path(a.sweep) / 'summary.csv')):
        if r['status'] == 'pass':
            times.setdefault((r['pattern'], r['method']), []).append(float(r['total_s']))
    med = {k: statistics.median(v) for k, v in times.items()}
    rows = []
    for p, _ in PATTERNS:
        for m, _ in ARMS:
            row = {'pattern': p, 'method': m, 'runs': len(times.get((p, m), [])), 'median_s': round(med[(p, m)], 2)}
            if m.startswith('pgdelta'):
                row['vs_full'] = round(med[(p, m)] / med[(p, 'full')], 3)
                row['vs_parcopy8'] = round(med[(p, m)] / med[(p, 'parcopy8')], 3)
                row.update({f't_{k}': round(v, 1) for k, v in (timers(a.sweep, p, m) or {}).items()})
            rows.append(row)
            print(row)
    keys = sorted({k for r in rows for k in r}, key=lambda k: (k not in ('pattern', 'method', 'runs', 'median_s'), k))
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp31-cells.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)

    fig, (left, right) = plt.subplots(1, 2, figsize=(12, 4.4), gridspec_kw={'width_ratios': [1.1, 1]})
    x = np.arange(len(PATTERNS))
    colors = [COLORS['grey'], COLORS['sky'], COLORS['orange'], COLORS['blue']]
    for i, ((m, label), color) in enumerate(zip(ARMS, colors)):
        vals = [med[(p, m)] for p, _ in PATTERNS]
        bars = left.bar(x + (i - 1.5) * 0.2, vals, 0.2, color=color, label=label.replace('\n', ' '))
        for bar, v in zip(bars, vals):
            left.annotate(f'{v:.0f}', (bar.get_x() + bar.get_width() / 2, v), textcoords='offset points',
                          xytext=(0, 2), ha='center', fontsize=7.5)
    left.set_xticks(x, [label for _, label in PATTERNS])
    left.set_ylabel('refresh time (s), median of 2')
    left.set_title('12 GB on 8 GB VMs, balanced persistent disk, two zones', fontsize=10)
    left.legend(fontsize=8)
    # v1's timers: hashing B (overlaps hashing S), the write phase (its fsync inside it), the wait for verification.
    bars = [(p, m) for p, _ in PATTERNS for m in ('pgdelta_direct_inplace', 'pgdelta_direct_inplace_vw')]
    labels = [f"{'hot' if p == 'hot' else 'uniform'}\n{'v1' if m.endswith('inplace') else 'v1 written'}" for p, m in bars]
    t = [timers(a.sweep, p, m) for p, m in bars]
    y = np.arange(len(bars))
    hashed = [d['hash_basis'] for d in t]
    write = [d['rebuild'] for d in t]
    wait = [d['verify_wait'] for d in t]
    right.barh(y, hashed, color=COLORS['purple'], label='hashing B (S at the same time)')
    right.barh(y, write, left=hashed, color=COLORS['red'], label='writing pages in place (incl. fsync)')
    right.barh(y, wait, left=[h + w for h, w in zip(hashed, write)], color=COLORS['yellow'],
               label='waiting for verification')
    for yi, d, h, w, v in zip(y, t, hashed, write, wait):
        right.annotate(f"total {d['total']:.0f} s", (max(d['total'], h + w + v), yi), textcoords='offset points',
                       xytext=(4, -3), fontsize=8)
    right.set_xlim(0, max(h + w + v for h, w, v in zip(hashed, write, wait)) * 1.25)
    right.set_yticks(y, labels, fontsize=8.5)
    right.invert_yaxis()
    right.set_xlabel("seconds, v1's own timers (phases overlap)")
    right.set_title("Where v1's time goes", fontsize=10)
    right.legend(fontsize=7.5, loc='upper center', bbox_to_anchor=(0.5, -0.18), ncol=2, frameon=False)
    fig.tight_layout()
    print(plotting.save(fig, 'exp31-ha'))


if __name__ == '__main__':
    main()
