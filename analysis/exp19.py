"""EXP-19: EXP-18's two-part model, its share fixed, at 3 busy processes and under disk contention.

    .venv/bin/python analysis/exp19.py [--root results/exp04] [--figdir DIR]

Predicts v1's median at each level as t0 x [(1 - f) + f x H0 / H], with t0 and H0 from this session's level 0 and
f fixed from EXP-18 (0.64 unthrottled, 0.57 at 2 Gbit/s), and `full` as unchanged from level 0, as
docs/experiments/EXP-19-two-part-model.md registered. Writes docs/experiments/data/exp19-cells.csv and
docs/figures/exp19-two-part.svg.
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
LEVELS = [('exp19-k0', 'idle'), ('exp19-k3', '3 busy processes'), ('exp19-io', 'disk contention'), ('exp19-k0b', 'idle, at the end')]
F = {'unthrottled': 0.64, '2g': 0.57}
ARMS = ('pgdelta_direct_inplace', 'full')


def medians(sweep):
    cells = {}
    for r in csv.DictReader(open(sweep / 'summary.csv')):
        if r['method'] in ARMS and r['status'] == 'pass':
            cells.setdefault((r['method'], r['speed']), []).append(float(r['total_s']))
    return {k: (statistics.median(v), len(v)) for k, v in cells.items()}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--root', default=str(ROOT / 'results' / 'exp04'))
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    root = Path(a.root)
    cal = {name: json.loads((root / 'exp19-calibration' / f'{name}-before.json').read_text()) for name, _ in LEVELS}
    H = {n: next(h['pages_per_s'] for h in c['hash'] if h['procs'] == 4) for n, c in cal.items()}
    disk = {n: c.get('disk', {}) for n, c in cal.items()}
    med = {name: medians(root / name) for name, _ in LEVELS}
    base = med['exp19-k0']
    print('Before each level: dest 4-process hash rate; 1 GiB direct write and read (MiB/s)')
    for name, label in LEVELS:
        print(f"  {label:18s} {H[name]:>9,} ({100 * (H[name] / H['exp19-k0'] - 1):+5.1f}%); "
              f"write {disk[name].get('write_mib_per_s')}, read {disk[name].get('read_mib_per_s')}")
    rows = []
    print('\nlevel, arm, speed, runs, measured median, predicted, error')
    for name, label in LEVELS:
        for (m, sp), (meas, n) in sorted(med[name].items()):
            t0 = base[(m, sp)][0]
            pred = t0 * ((1 - F[sp]) + F[sp] * H['exp19-k0'] / H[name]) if m == 'pgdelta_direct_inplace' else t0
            rows.append({'level': name, 'condition': label, 'arm': m, 'speed': sp, 'runs': n, 'measured_s': round(meas, 3),
                         'predicted_s': round(pred, 3), 'error': round(pred / meas - 1, 4), 'hash_rate': H[name],
                         'disk_write_mib_s': disk[name].get('write_mib_per_s'), 'disk_read_mib_s': disk[name].get('read_mib_per_s')})
            print(f'  {label:18s} {m:23s} {sp:11s} {n} {meas:7.3f} {pred:7.3f} ({100 * (pred / meas - 1):+5.1f}%)')
    out = ROOT / 'docs' / 'experiments' / 'data' / 'exp19-cells.csv'
    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    print(out)

    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.4), sharey=False)
    for ax, sp in zip(axes, ('unthrottled', '2g')):
        x = np.arange(len(LEVELS))
        for i, (m, color) in enumerate((('pgdelta_direct_inplace', COLORS['blue']), ('full', COLORS['grey']))):
            sel = [next(r for r in rows if r['level'] == n and r['arm'] == m and r['speed'] == sp) for n, _ in LEVELS
                   if any(r['level'] == n and r['arm'] == m and r['speed'] == sp for r in rows)]
            xs = x[:len(sel)] + (i - 0.5) * 0.38
            ax.bar(xs, [r['measured_s'] for r in sel], 0.36, color=color,
                   label='v1 measured' if m != 'full' else 'pg_basebackup measured')
            ax.scatter(xs, [r['predicted_s'] for r in sel], marker='_', s=400, color=COLORS['black'], zorder=3,
                       label='predicted (fixed model)' if i == 0 else None)
        ax.set_xticks(x, [l for _, l in LEVELS], fontsize=8.5)
        ax.set_ylabel('median time (s)'); ax.set_title(f'{sp}')
        ax.legend(fontsize=8)
    fig.suptitle("EXP-19: the two-part model, fixed from EXP-18, on new VMs", fontsize=12)
    fig.tight_layout()
    print(plotting.save(fig, 'exp19-two-part'))


if __name__ == '__main__':
    main()
