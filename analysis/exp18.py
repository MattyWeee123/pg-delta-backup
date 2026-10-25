"""EXP-18: does a calibration taken before the runs predict how CPU contention on dest slows each tool?

    .venv/bin/python analysis/exp18.py [--root results/exp04] [--figdir DIR]

Reads the four sweeps (exp18-k0, -k2, -k4, -k0b: 0, 2, 4 and again 0 busy processes on dest) and the calibration
files (exp18-calibration/<sweep>-before.json, the 4-process hash rate on dest under that contention), fits the
registered model on level 0 alone and predicts the other levels, calibrated (v1's fixed work scaled by H0 / H) and
uncalibrated (H = H0), as docs/experiments/EXP-18-calibrated-contention.md registered. Writes
docs/experiments/data/exp18-cells.csv and docs/figures/exp18-calibration.svg.
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
LEVELS = [('exp18-k0', 0), ('exp18-k2', 2), ('exp18-k4', 4), ('exp18-k0b', 0)]
ARMS = {'full': 'one pg_basebackup stream', 'pgdelta_direct_inplace': "v1's path (4 jobs, in place)"}


def runs(sweep):
    out = []
    for r in csv.DictReader(open(sweep / 'summary.csv')):
        if r['method'] in ARMS and r['status'] == 'pass':
            out.append({'method': r['method'], 'speed': r['speed'], 't': float(r['total_s']),
                        'L': float(r['measured_s2d_gbit']) * 1e9 / 8, 'bytes': float(r['wire_s2d_bytes']),
                        'cpu_dst': float(r['cpu_dest_s'] or 'nan')})
    return out


def hash_rate(path):
    data = json.loads(path.read_text())
    return next(h['pages_per_s'] for h in data['hash'] if h['procs'] == 4)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--root', default=str(ROOT / 'results' / 'exp04'))
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    root = Path(a.root)
    cal = root / 'exp18-calibration'
    data = {name: runs(root / name) for name, _ in LEVELS}
    H = {name: hash_rate(cal / f'{name}-before.json') for name, _ in LEVELS}
    H_after = {name: hash_rate(cal / f'{name}-after.json') for name, _ in LEVELS}
    base = data['exp18-k0']
    print('Dest hash rate, 4 processes (pages/s), before each level and after its busy processes stopped:')
    for name, k in LEVELS:
        print(f'  {name:10s} {k} busy: {H[name]:>9,} ({100 * (H[name] / H["exp18-k0"] - 1):+.1f}%), after {H_after[name]:,}')
    # Level 0's fit: v1's fixed work per speed-independent run, full's c and S as in EXP-16.
    v1_0 = [r for r in base if r['method'] == 'pgdelta_direct_inplace']
    c_v1 = statistics.median(r['t'] - r['bytes'] / r['L'] for r in v1_0)
    full_0 = [r for r in base if r['method'] == 'full']
    best = None
    for s in np.geomspace(0.05e9, 3e9, 600):
        x = np.array([r['bytes'] / min(r['L'], s) for r in full_0]); t = np.array([r['t'] for r in full_0])
        c = max(0.0, float(np.median(t - x)))
        err = float(np.sum(np.log((c + x) / t) ** 2))
        if best is None or err < best[0]:
            best = (err, c, s)
    _, c_full, s_full = best
    print(f'Level 0 fit: v1 fixed work {c_v1:.3f} s; full c {c_full:.3f} s, S {s_full * 8 / 1e9:.2f} Gbit/s')

    rows = []
    print('\nlevel, arm, speed, runs, measured median, calibrated prediction (error), uncalibrated (error)')
    for name, k in LEVELS:
        for m in ARMS:
            for sp in sorted({r['speed'] for r in data[name] if r['method'] == m}):
                sel = [r for r in data[name] if r['method'] == m and r['speed'] == sp]
                meas = statistics.median(r['t'] for r in sel)
                if m == 'full':
                    cal_p = unc_p = statistics.median(c_full + r['bytes'] / min(r['L'], s_full) for r in sel)
                else:
                    cal_p = statistics.median(c_v1 * H['exp18-k0'] / H[name] + r['bytes'] / r['L'] for r in sel)
                    unc_p = statistics.median(c_v1 + r['bytes'] / r['L'] for r in sel)
                rows.append({'level': name, 'busy': k, 'arm': m, 'speed': sp, 'runs': len(sel), 'measured_s': round(meas, 3),
                             'calibrated_s': round(cal_p, 3), 'calibrated_error': round(cal_p / meas - 1, 4),
                             'uncalibrated_s': round(unc_p, 3), 'uncalibrated_error': round(unc_p / meas - 1, 4),
                             'hash_rate': H[name]})
                print(f'  {name:10s} {m:23s} {sp:11s} {len(sel)} {meas:7.3f} {cal_p:7.3f} ({100 * (cal_p / meas - 1):+5.1f}%) '
                      f'{unc_p:7.3f} ({100 * (unc_p / meas - 1):+5.1f}%)')
    out = ROOT / 'docs' / 'experiments' / 'data' / 'exp18-cells.csv'
    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    print(out)

    fig, (left, right) = plt.subplots(1, 2, figsize=(11.5, 4.4))
    for m, color in (('pgdelta_direct_inplace', COLORS['blue']), ('full', COLORS['grey'])):
        for sp, ls in (('unthrottled', '-'), ('2g', '--')):
            pts = [(r['busy'], r['measured_s']) for r in rows if r['arm'] == m and r['speed'] == sp and r['level'] != 'exp18-k0b']
            if pts:
                xs, ys = zip(*sorted(pts))
                left.plot(xs, ys, ls, marker='o', color=color, label=f'{ARMS[m]}, {sp}')
    left.set_xlabel('busy processes on dest'); left.set_ylabel('median time (s)'); left.set_xticks([0, 2, 4])
    left.set_title('Deliberate CPU contention on dest'); left.legend(fontsize=8)
    v1 = [r for r in rows if r['arm'] == 'pgdelta_direct_inplace' and r['level'] != 'exp18-k0']
    labels = [f"{r['busy']} busy\n{r['speed']}" + ('\n(end)' if r['level'] == 'exp18-k0b' else '') for r in v1]
    x = np.arange(len(v1))
    right.bar(x - 0.2, [100 * r['uncalibrated_error'] for r in v1], 0.4, color=COLORS['orange'], label='uncalibrated')
    right.bar(x + 0.2, [100 * r['calibrated_error'] for r in v1], 0.4, color=COLORS['blue'], label='calibrated by the hash benchmark')
    right.axhspan(-10, 10, color=COLORS['grey'], alpha=0.15, lw=0)
    right.axhline(0, color=COLORS['black'], lw=0.8)
    right.set_xticks(x, labels, fontsize=8); right.set_ylabel('prediction error, v1 (%)')
    right.set_title('Predicting v1 from level 0 (grey: within ±10%)'); right.legend(fontsize=8)
    fig.tight_layout()
    print(plotting.save(fig, 'exp18-calibration'))


if __name__ == '__main__':
    main()
