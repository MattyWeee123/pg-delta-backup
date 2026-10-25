"""Does a simple physical timing model, fitted on one session, predict another? (exploratory, on existing data)

    .venv/bin/python analysis/model_check.py [--figdir DIR]

Fits two models on the October 23 sessions (EXP-09/10, docs/experiments/data/exp09-10-runs.csv) and predicts the
October 24 session (EXP-11, exp11-runs.csv: another VM pair, and a speed, 10 Gbit/s, the fit never saw), each run
from that run's own measured link rate and wire bytes:
- one pg_basebackup stream (`full`): t = c + bytes / min(L, S), where L is the link's measured one-stream rate and
  S the tool's own ceiling; c and S are fitted;
- v1's path (`pgdelta_direct_inplace`): t = c_pattern + bytes / L; one c per change pattern (the hashing, reading
  and writing it does whatever the link), fitted.
It prints each cell's measured and predicted medians and the error, and per arm whether EXP-11's runs used more CPU
than the fit's (a slower or busier CPU) or the same CPU in more time (waiting). Writes docs/figures/model-check.svg.
Not pre-registered: it asks whether such a model can stand in for repeated sessions before one is designed.
"""
from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path

import numpy as np

import plotting
from plotting import COLORS, plt

DATA = Path(__file__).resolve().parents[1] / 'docs' / 'experiments' / 'data'
ARMS = {'full': 'one pg_basebackup stream', 'pgdelta_direct_inplace': "v1's path (4 jobs, in place)"}
SPEED_ORDER = ['2g', '5g', '10g', 'unthrottled']


def load(name):
    rows = []
    for r in csv.DictReader(open(DATA / name)):
        if r['method'] not in ARMS or r.get('status', 'pass') not in ('pass', ''):
            continue
        try:
            rows.append({'method': r['method'], 'pattern': r['pattern'], 'speed': r['speed'],
                         't': float(r['total_s']), 'L': float(r['measured_s2d_gbit']) * 1e9 / 8,
                         'bytes': float(r['wire_s2d_bytes']), 'cpu_src': float(r['cpu_source_s'] or 'nan'),
                         'cpu_dst': float(r['cpu_dest_s'] or 'nan')})
        except (KeyError, ValueError):
            continue
    return rows


def fit_full(rows):
    """c and S minimising the squared log error over a grid of S (bytes per second)."""
    best = None
    for s in np.geomspace(0.05e9, 3e9, 600):
        x = np.array([r['bytes'] / min(r['L'], s) for r in rows])
        t = np.array([r['t'] for r in rows])
        c = max(0.0, float(np.median(t - x)))
        err = float(np.sum(np.log((c + x) / t) ** 2))
        if best is None or err < best[0]:
            best = (err, c, s)
    return best[1], best[2]


def predict_full(r, c, s):
    return c + r['bytes'] / min(r['L'], s)


def fit_v1(rows):
    return {p: float(np.median([r['t'] - r['bytes'] / r['L'] for r in rows if r['pattern'] == p]))
            for p in sorted({r['pattern'] for r in rows})}


def cells(rows, predict):
    out = {}
    for key in sorted({(r['method'], r['pattern'], r['speed']) for r in rows}):
        sel = [r for r in rows if (r['method'], r['pattern'], r['speed']) == key]
        out[key] = (statistics.median(r['t'] for r in sel), statistics.median(predict(r) for r in sel), len(sel))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--figdir')
    args = parser.parse_args(argv)
    if args.figdir:
        plotting.FIGURES = Path(args.figdir)
    plotting.style()
    fit_rows, test_rows = load('exp09-10-runs.csv'), load('exp11-runs.csv')
    full_fit = [r for r in fit_rows if r['method'] == 'full']
    v1_fit = [r for r in fit_rows if r['method'] == 'pgdelta_direct_inplace']
    c_full, s_full = fit_full(full_fit)
    c_v1 = fit_v1(v1_fit)
    print(f"Fitted on EXP-09/10 ({len(full_fit)} full, {len(v1_fit)} v1 runs): full c = {c_full:.2f} s, "
          f"S = {s_full * 8 / 1e9:.2f} Gbit/s; v1 c by pattern: "
          + ', '.join(f'{p} {c:.2f} s' for p, c in c_v1.items()))

    def predict(r):
        return predict_full(r, c_full, s_full) if r['method'] == 'full' else c_v1[r['pattern']] + r['bytes'] / r['L']

    results = {'fit (EXP-09/10)': cells(fit_rows, predict), 'test (EXP-11)': cells(test_rows, predict)}
    for name, res in results.items():
        print(f'\n{name}: arm, pattern, speed, runs, measured median s, predicted s, error')
        for (m, p, sp), (meas, pred, n) in res.items():
            print(f'  {m:23s} {p:8s} {sp:11s} {n:2d} {meas:7.2f} {pred:7.2f} {100 * (pred / meas - 1):+6.1f}%')
        for m in ARMS:
            errs = [abs(pred / meas - 1) for (mm, _, _), (meas, pred, _) in res.items() if mm == m]
            print(f'  {m}: median |error| {100 * statistics.median(errs):.1f}%, largest {100 * max(errs):.1f}%')
    # The CPU-calibrated view: v1's fixed work scaled by the run's own dest CPU-s over the fit's median for its pattern.
    cpu_ref = {pt: float(np.median([r['cpu_dst'] for r in v1_fit if r['pattern'] == pt])) for pt in c_v1}
    scaled = cells([r for r in test_rows if r['method'] == 'pgdelta_direct_inplace'],
                   lambda r: c_v1[r['pattern']] * r['cpu_dst'] / cpu_ref[r['pattern']] + r['bytes'] / r['L'])
    errs = [abs(pred / meas - 1) for (meas, pred, _) in scaled.values()]
    print(f'\nv1 on EXP-11 with its fixed work scaled by each run\'s own dest CPU-s: median |error| '
          f'{100 * statistics.median(errs):.1f}%, largest {100 * max(errs):.1f}% (uncalibrated above)')
    print('\nCPU per run, median (source, dest CPU-s): does the test session use more CPU for the same work?')
    for m in ARMS:
        for label, rows in (('EXP-09/10', fit_rows), ('EXP-11', test_rows)):
            sel = [r for r in rows if r['method'] == m and r['speed'] == 'unthrottled' and r['pattern'] == 'hot']
            if sel:
                print(f"  {m:23s} {label:9s} unthrottled hot: t {statistics.median(r['t'] for r in sel):.2f} s, "
                      f"CPU {statistics.median(r['cpu_src'] for r in sel):.2f} / {statistics.median(r['cpu_dst'] for r in sel):.2f}")

    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.8))
    for ax, m in zip(axes, ARMS):
        lo, hi = 1e9, 0
        for name, marker, face in (('fit (EXP-09/10)', 'o', 'none'), ('test (EXP-11)', 's', None)):
            for (mm, p, sp), (meas, pred, _) in results[name].items():
                if mm != m:
                    continue
                color = {'2g': COLORS['blue'], '5g': COLORS['green'], '10g': COLORS['purple'],
                         'unthrottled': COLORS['red']}[sp]
                ax.scatter(meas, pred, marker=marker, s=40, edgecolors=color,
                           facecolors=color if face is None else 'none', zorder=3)
                lo, hi = min(lo, meas, pred), max(hi, meas, pred)
        span = np.array([lo * 0.85, hi * 1.15])
        ax.plot(span, span, color=COLORS['black'], lw=0.8)
        ax.fill_between(span, span * 0.9, span * 1.1, color=COLORS['grey'], alpha=0.15, lw=0)
        ax.set_xscale('log'); ax.set_yscale('log')
        for axis in (ax.xaxis, ax.yaxis):
            axis.set_major_formatter(plt.matplotlib.ticker.FormatStrFormatter('%g'))
            axis.set_minor_formatter(plt.matplotlib.ticker.FormatStrFormatter('%g'))
        ax.set_xlabel('measured median time (s)'); ax.set_ylabel('predicted time (s)')
        ax.set_title(ARMS[m])
    handles = [plt.Line2D([], [], marker='o', ls='', mfc='none', mec=COLORS['black'], label='fitted on (EXP-09/10)'),
               plt.Line2D([], [], marker='s', ls='', color=COLORS['black'], label='predicted (EXP-11, another pair)')]
    handles += [plt.Line2D([], [], marker='s', ls='', color=c, label=l) for l, c in
                (('2 Gbit/s', COLORS['blue']), ('5 Gbit/s', COLORS['green']), ('10 Gbit/s (never fitted)', COLORS['purple']),
                 ('unthrottled', COLORS['red']))]
    handles.append(plt.Rectangle((0, 0), 1, 1, color=COLORS['grey'], alpha=0.15, label='within ±10%'))
    fig.legend(handles=handles, loc='lower center', ncol=4, fontsize=8.5, frameon=False)
    fig.suptitle('Fitted on one session, does the timing model predict another?', fontsize=12)
    fig.tight_layout(rect=(0, 0.12, 1, 1))
    print(plotting.save(fig, 'model-check'))


if __name__ == '__main__':
    main()
