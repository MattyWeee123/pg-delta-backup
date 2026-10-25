"""EXP-20: EXP-13's DR verdict on its deciding cells (docs/experiments/EXP-20-dr-latency-core.md).

    .venv/bin/python analysis/exp20.py [--sweep results/exp04/exp20] [--figdir DIR]

Per workload and condition, the median time of every passing run of each arm; B_fast is the faster durable
parallel copy at that condition (parcopy8, or parcopy16 where it ran); R_fast = v1 / B_fast and R_F0 = v1 / one
pg_basebackup stream. EXP-13's DR verdict rows, in order, at r = 20, 60 and 120 ms (at 20 ms default hosts only),
and its qualifier. Rule 11 as clarified, fixed here before the data: a deciding ratio within 20% of the threshold
that decides its row (0.90 or 1.00), the drift EXP-16 measured between sessions for CPU-bound cells, makes the row
provisional for one session. Writes docs/experiments/data/exp20-cells.csv and docs/figures/exp20-dr.svg.
"""
from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
V1, FULL, BOUNDS = 'pgdelta_direct_inplace', 'full', ('parcopy8', 'parcopy16')
DRIFT = 0.20


def cells(sweep):
    out = {}
    for r in csv.DictReader(open(sweep / 'summary.csv')):
        if r['status'] != 'pass':
            continue
        out.setdefault((r['pattern'], r['speed'], r['method']), []).append(float(r['total_s']))
    return {k: (statistics.median(v), len(v)) for k, v in out.items()}


def ratios(c, pattern, cond):
    v1, full = c.get((pattern, cond, V1)), c.get((pattern, cond, FULL))
    bounds = [c[(pattern, cond, b)][0] for b in BOUNDS if (pattern, cond, b) in c]
    b_fast = min(bounds) if bounds else None
    return {'v1': v1 and v1[0], 'full': full and full[0], 'b_fast': b_fast,
            'R_fast': v1[0] / b_fast if v1 and b_fast else None, 'R_F0': v1[0] / full[0] if v1 and full else None}


def near(x, threshold):
    return x is not None and abs(x / threshold - 1) < DRIFT


def row(c, r):
    settings = ['default'] if r == 20 else ['default', 'tuned']
    R = {(p, h): ratios(c, p, f'2:{r}:{h}')['R_fast'] for p in ('hot', 'uniform') for h in settings}
    if any(v is None for v in R.values()):
        return 'Not judged', R, False
    deciding = []
    if R[('hot', 'default')] <= 0.90 and R[('uniform', 'default')] <= 0.90:
        verdict = '1. Holds as shipped'; deciding = [(R[('hot', 'default')], 0.90), (R[('uniform', 'default')], 0.90)]
    elif 'tuned' in settings and R[('hot', 'tuned')] <= 0.90 and R[('uniform', 'tuned')] <= 0.90:
        verdict = '2. Holds with tuned buffers'; deciding = [(R[('hot', 'tuned')], 0.90), (R[('uniform', 'tuned')], 0.90),
                                                             (R[('uniform', 'default')], 0.90)]
    elif any(R[('hot', h)] <= 0.90 for h in settings) and all(R[('uniform', h)] >= 1.00 for h in settings):
        verdict = '3. Local changes only'; deciding = [(min(R[('hot', h)] for h in settings), 0.90)] + \
                                                      [(R[('uniform', h)], 1.00) for h in settings]
    elif all(R[('hot', h)] >= 1.00 for h in settings):
        verdict = '4. Out at DR latency'; deciding = [(R[('hot', h)], 1.00) for h in settings]
    else:
        verdict = '5. Undecided'
    return verdict, R, any(near(x, t) for x, t in deciding)


def main(argv=None):
    global V1
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--sweep', default=str(ROOT / 'results' / 'exp04' / 'exp20'))
    ap.add_argument('--figdir')
    ap.add_argument('--v1', default=V1, help='the v1 arm to judge (EXP-24: pgdelta_direct_inplace_bbr)')
    ap.add_argument('--tag', default='', help='suffix for the data and figure files')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    V1 = a.v1
    c = cells(Path(a.sweep))
    conds = ['2:0:default', '2:20:default', '2:60:default', '2:120:default', '2:0:tuned', '2:60:tuned', '2:120:tuned']
    rows = []
    print('pattern, condition: medians (s) v1 / full / B_fast; R_fast, R_F0')
    for p in ('hot', 'uniform'):
        for cond in conds:
            x = ratios(c, p, cond)
            if x['v1'] is None:
                continue
            rows.append({'pattern': p, 'condition': cond, **{k: (round(v, 4) if v is not None else None) for k, v in x.items()}})
            fmt = lambda v: '–' if v is None else f'{v:.3f}'
            print(f"  {p:8s} {cond:14s} {fmt(x['v1'])} / {fmt(x['full'])} / {fmt(x['b_fast'])}; R_fast {fmt(x['R_fast'])}, R_F0 {fmt(x['R_F0'])}")
    print('\nEXP-13\'s DR verdict, rows in order:')
    for r in (20, 60, 120):
        verdict, R, close = row(c, r)
        qual = all((ratios(c, p, f'2:{r}:default')['R_F0'] or 9) <= 0.90 for p in ('hot', 'uniform'))
        print(f"  {r:3d} ms: {verdict}" + (' (provisional: a deciding ratio within 20% of its threshold)' if close else '')
              + f"; qualifier (v1 faster than one pg_basebackup stream, default hosts): {'yes' if qual else 'no'}")
    out = ROOT / 'docs' / 'experiments' / 'data' / f'exp20-cells{a.tag}.csv'
    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    print(out)

    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.4), sharey=True)
    for ax, p in zip(axes, ('hot', 'uniform')):
        for host, style in (('default', '-'), ('tuned', '--')):
            pts = [(int(r['condition'].split(':')[1]), r) for r in rows if r['pattern'] == p and r['condition'].endswith(host)]
            pts.sort(key=lambda t: t[0])
            for key, color, label in (('R_fast', COLORS['blue'], 'v1 ÷ fastest parallel copy'),
                                      ('R_F0', COLORS['orange'], 'v1 ÷ one pg_basebackup stream')):
                xs = [x for x, r in pts if r[key] is not None]; ys = [r[key] for x, r in pts if r[key] is not None]
                ax.plot(xs, ys, style, marker='o', color=color, label=f'{label} ({host} hosts)')
        for t in (0.9, 1.0):
            ax.axhline(t, color=COLORS['black'], lw=0.8, ls=':' if t == 0.9 else '-')
        ax.set_xticks([0, 20, 60, 120]); ax.set_xlabel('round-trip time (ms), 2 Gbit/s')
        ax.set_title(f'{p} 1% changes'); ax.set_yscale('log')
    axes[0].set_ylabel('time ratio (below 1: v1 faster)')
    axes[1].legend(fontsize=7.5, loc='upper left')
    fig.suptitle(f"{V1} against the full copies at disaster-recovery round trips (dotted 0.90, solid 1.00)", fontsize=11)
    fig.tight_layout()
    print(plotting.save(fig, f'exp20-dr{a.tag}'))


if __name__ == '__main__':
    main()
