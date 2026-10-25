"""EXP-23: the slow mode's rate under cubic and BBR (docs/experiments/EXP-23-slow-mode-rate.md).

    .venv/bin/python analysis/exp23.py [--root results/exp04] [--figdir DIR]

For each run of v1, the time send() blocked over page data (pgdelta's page_send_s) against the time the
condition's measured one-stream link rate needs for those bytes; above 2x is the slow mode. Counts per congestion
control and condition, with an exact (Clopper-Pearson) 95% interval. Writes docs/experiments/data/exp23-runs.csv
and docs/figures/exp23-slow-mode.svg.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
from collections import defaultdict
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
SWEEPS = {'cubic': 'exp23-cubic', 'BBR': 'exp23-bbr'}


def cdf(k, n, p):
    """P(X <= k) for X ~ Binomial(n, p)."""
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))


def interval(k, n, conf=0.95):
    """Exact (Clopper-Pearson) interval for k successes in n, by bisection on the binomial CDF."""
    a = (1 - conf) / 2

    def solve(f):
        lo, hi = 0.0, 1.0
        for _ in range(60):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if f(mid) else (lo, mid)
        return (lo + hi) / 2
    lower = 0.0 if k == 0 else solve(lambda p: 1 - cdf(k - 1, n, p) < a)
    upper = 1.0 if k == n else solve(lambda p: cdf(k, n, p) > a)
    return lower, upper


def link_rates(sweep):
    out = defaultdict(list)
    for tf in glob.glob(str(sweep / 'trials' / '*.json')):
        for v in json.load(open(tf)).get('visits', []):
            c = v.get('condition'); label = c.get('label') if isinstance(c, dict) else c
            out[label] += [lt['steady_gbit'] for lt in v.get('link_tests') or [] if isinstance(lt, dict) and lt.get('steady_gbit')]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--root', default=str(ROOT / 'results' / 'exp04'))
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    rows = []
    for cc, name in SWEEPS.items():
        sweep = Path(a.root) / name
        links = link_rates(sweep)
        for f in sorted(glob.glob(str(sweep / 'runs' / '*' / '*pgdelta_direct_inplace.json'))):
            r = json.load(open(f))
            if r.get('status') != 'pass':
                continue
            cond = r['speed']['label']
            sent = r['totals']['wire_bytes_source_to_dest']
            floor = sent * 8 / (min(links[cond]) * 1e9)
            ps = r['tool_reported']['page_send_s']
            rows.append({'cc': cc, 'condition': cond, 'repeat': r['repeat'], 'total_s': r['totals']['wall_seconds'],
                         'page_send_s': round(ps, 3), 'link_floor_s': round(floor, 3), 'ratio': round(ps / floor, 3),
                         'slow': ps / floor > 2.0})
    print('congestion control, condition: slow-mode runs of all, 95% interval; median total s (fast runs, slow runs)')
    stats = {}
    for cc in SWEEPS:
        for cond in sorted({r['condition'] for r in rows}):
            sel = [r for r in rows if r['cc'] == cc and r['condition'] == cond]
            if not sel:
                continue
            k, n = sum(r['slow'] for r in sel), len(sel)
            lo, hi = interval(k, n)
            fast = sorted(r['total_s'] for r in sel if not r['slow']); slow = sorted(r['total_s'] for r in sel if r['slow'])
            med = lambda v: f'{v[len(v) // 2]:.2f}' if v else '–'
            stats[(cc, cond)] = (k, n, lo, hi)
            print(f'  {cc:5s} {cond:14s} {k:2d} of {n:2d} ({100 * lo:.0f}–{100 * hi:.0f}%); fast {med(fast)} s, slow {med(slow)} s')
    out = ROOT / 'docs' / 'experiments' / 'data' / 'exp23-runs.csv'
    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    print(out)

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    for ax, cond in zip(axes, sorted({r['condition'] for r in rows})):
        for i, (cc, color) in enumerate((('cubic', COLORS['orange']), ('BBR', COLORS['blue']))):
            ys = [r['total_s'] for r in rows if r['cc'] == cc and r['condition'] == cond]
            ax.scatter([i + (j % 5 - 2) * 0.04 for j in range(len(ys))], ys, color=color, s=22)
            k, n, lo, hi = stats[(cc, cond)]
            ax.text(i, max(ys) * 1.05, f'{k} of {n} slow', ha='center', fontsize=9)
        ax.set_xticks([0, 1], ['cubic (default)', 'BBR']); ax.set_xlim(-0.6, 1.6)
        ax.set_ylabel("v1's refresh time (s)"); ax.set_title(f'uniform 1%, {cond}')
        ax.set_ylim(0, max(r['total_s'] for r in rows) * 1.18)
    fig.suptitle('EXP-23: each dot is one refresh; the slow mode is the upper cluster', fontsize=11)
    fig.tight_layout()
    print(plotting.save(fig, 'exp23-slow-mode'))


if __name__ == '__main__':
    main()
