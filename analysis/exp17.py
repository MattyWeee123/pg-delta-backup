"""EXP-17's analysis: outcomes by fault, and the bound on the failure rate (docs/experiments/EXP-17-randomized-correctness.md).

    .venv/bin/python analysis/exp17.py [--trials results/correctness/exp17/trials.jsonl] [--figdir DIR]

Counts every trial by fault and outcome, checks the registered rules (no wrong output; every killed run that hadn't
finished left B unstartable; every foreign basis refused with B unchanged), and gives the upper bound on the
probability of a wrong output, 1 - (1 - c)^(1/N) for zero wrong outputs in N counted trials, at 95% and 99.9%.
Writes docs/experiments/data/exp17-summary.csv and docs/figures/exp17-correctness.svg.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter
from pathlib import Path

import numpy as np

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
FAULTS = ['none', 'diverged', 'kill', 'corrupt', 'truncate', 'foreign']


def bound(n, wrong, confidence):
    """Upper bound on the per-trial failure probability; exact (Clopper-Pearson) for zero failures only."""
    if n == 0 or wrong:
        return None
    return 1 - (1 - confidence) ** (1 / n)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--trials', default=str(ROOT / 'results' / 'correctness' / 'exp17' / 'trials.jsonl'))
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    rows = [json.loads(line) for line in open(a.trials)]
    counted = [r for r in rows if 'pass' in r]
    errors = [r for r in rows if 'harness_error' in r]
    wrong = [r for r in counted if not r['pass']]
    by = {f: [r for r in counted if r['fault'] == f] for f in FAULTS}
    kills = by['kill']
    unfinished = [r for r in kills if r.get('finished_before_kill') is False]
    print(f'{len(rows)} trials: {len(counted)} counted, {len(errors)} harness errors, {len(wrong)} wrong outputs')
    for f in FAULTS:
        rs = by[f]
        print(f'  {f:9s} {len(rs):5d} counted, {sum(r["pass"] for r in rs):5d} passed'
              + (f', refresh {statistics.median(r["seconds"] for r in rs):.2f} s median' if rs else ''))
    touched = [r for r in unfinished if r.get('b_changed_at_kill')]
    untouched = [r for r in unfinished if r.get('b_changed_at_kill') is False]
    print(f'  kills: {len(unfinished)} before the refresh finished: {len(touched)} had changed B, of which '
          f'{sum(bool(r.get("refused_start_after_kill")) for r in touched)} refused to start; {len(untouched)} had not '
          f'touched B (it started as the old copy: {sum(not r.get("refused_start_after_kill") for r in untouched)}); '
          f'{len(kills) - len(unfinished)} after it finished')
    print(f'  foreign bases refused with B unchanged: {sum(r["pass"] for r in by["foreign"])} of {len(by["foreign"])}')
    n = len(counted)
    for c in (0.95, 0.999):
        b = bound(n, len(wrong), c)
        print(f'  upper bound on a wrong output at {c:.1%} confidence: '
              + ('not claimed (a wrong output)' if b is None else f'{b:.4%} per refresh ({b * n:.2f} / N)'))
    if errors:
        print('  harness errors:', Counter(e['harness_error'][:80] for e in errors).most_common(3))
    data = ROOT / 'docs' / 'experiments' / 'data' / 'exp17-summary.csv'
    with open(data, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['fault', 'counted', 'passed', 'wrong', 'median_refresh_s'])
        for fl in FAULTS:
            rs = by[fl]
            w.writerow([fl, len(rs), sum(r['pass'] for r in rs), sum(not r['pass'] for r in rs),
                        round(statistics.median(r['seconds'] for r in rs), 3) if rs else ''])
        w.writerow(['all', n, n - len(wrong), len(wrong), ''])
    print(data)

    fig, (left, right) = plt.subplots(1, 2, figsize=(11.5, 4.3))
    names = ['no fault', 'B diverged', 'killed, rerun', 'B corrupted', 'B truncated', 'foreign B']
    passed = [sum(r['pass'] for r in by[f]) for f in FAULTS]
    failed = [sum(not r['pass'] for r in by[f]) for f in FAULTS]
    y = np.arange(len(FAULTS))[::-1]
    left.barh(y, passed, color=COLORS['green'], label='correct (or refused safely)')
    left.barh(y, failed, left=passed, color=COLORS['red'], label='wrong output')
    for yi, p, q in zip(y, passed, failed):
        left.text(p + q + max(passed) * 0.01, yi, f'{p}' + (f' + {q} wrong' if q else ''), va='center', fontsize=9)
    left.set_yticks(y, names)
    left.set_xlabel('trials')
    left.set_title(f'{n} randomized refreshes, by injected fault')
    left.legend(loc='lower right', fontsize=8.5)
    ns = np.geomspace(10, 100000, 200)
    for c, color in ((0.95, COLORS['blue']), (0.999, COLORS['black'])):
        right.plot(ns, 100 * (1 - (1 - c) ** (1 / ns)), color=color, lw=1.4, label=f'{c:.1%} confidence')
        b = bound(n, len(wrong), c)
        if b is not None:
            right.scatter([n], [100 * b], color=color, zorder=3)
            right.annotate(f'{100 * b:.2f}%', (n, 100 * b), textcoords='offset points', xytext=(6, 4), fontsize=9)
    right.set_xscale('log'); right.set_yscale('log')
    right.set_xlabel('trials with no wrong output (N)')
    right.set_ylabel('upper bound on wrong outputs per refresh (%)')
    right.set_title('What zero failures in N trials allows us to claim')
    right.legend(fontsize=8.5)
    fig.tight_layout()
    print(plotting.save(fig, 'exp17-correctness'))


if __name__ == '__main__':
    main()
