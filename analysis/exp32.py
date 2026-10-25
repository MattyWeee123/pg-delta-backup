"""EXP-32's analysis: EXP-17's campaign with --verify-scope written (docs/experiments/EXP-32-verify-written-correctness.md).

    .venv/bin/python analysis/exp32.py [--trials results/correctness/exp32/trials.jsonl] [--figdir DIR]

Counts the counted trials by fault and outcome (negative controls apart), the kills caught before the process
exited by B's state, and the negative controls (caught: exit 4, B blocked, start refused; then repaired). The
bound: 1 - (1 - c)^(1/N) for zero wrong outputs in N counted trials. Writes docs/experiments/data/exp32-summary.csv
and docs/figures/exp32-correctness.svg.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

import numpy as np

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
FAULTS = ['none', 'diverged', 'kill', 'corrupt', 'truncate', 'foreign']
NAMES = ['no fault', 'B diverged', 'killed, rerun', 'B corrupted', 'B truncated', 'foreign B (refused)']


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--trials', default=str(ROOT / 'results' / 'correctness' / 'exp32' / 'trials.jsonl'))
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    rows = list({r['trial']: r for r in (json.loads(line) for line in open(a.trials) if line.strip())}.values())
    counted = [r for r in rows if 'pass' in r and not r.get('negative_control')]
    controls = [r for r in rows if r.get('negative_control')]
    by = {f: [r for r in counted if r['fault'] == f] for f in FAULTS}
    wrong = [r for r in counted if not r['pass']]
    n = len(counted)
    print(f'{len(rows)} trials: {n} counted, {len(wrong)} wrong, {sum("harness_error" in r for r in rows)} harness errors')
    for f in FAULTS:
        print(f'  {f:9s} {len(by[f]):4d} counted, {sum(r["pass"] for r in by[f]):4d} passed')
    unfinished = [r for r in by['kill'] if r.get('finished_before_kill') is False]
    states = Counter(r.get('b_state_at_kill') for r in unfinished)
    partial_refused = sum(bool(r.get('refused_start_after_kill')) for r in unfinished if r.get('b_state_at_kill') == 'partial')
    print(f'  kills before the exit: {len(unfinished)}, B at the kill {dict(states)}; partial ones refused to start: '
          f'{partial_refused} of {states.get("partial", 0)}')
    print(f'  negative controls: {len(controls)}, caught {sum(r["caught"] for r in controls)}, '
          f'repaired {sum(bool(r.get("pass")) for r in controls)}')
    for c in (0.95, 0.999):
        print(f'  bound at {c:.1%}: ' + ('none (a wrong output)' if wrong else f'{1 - (1 - c) ** (1 / n):.4%} per refresh'))
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp32-summary.csv', 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['row', 'trials', 'correct_or_caught', 'wrong_or_missed'])
        for fl in FAULTS:
            w.writerow([fl, len(by[fl]), sum(r['pass'] for r in by[fl]), sum(not r['pass'] for r in by[fl])])
        w.writerow(['all counted', n, n - len(wrong), len(wrong)])
        w.writerow(['negative control (misplaced write)', len(controls), sum(r['caught'] for r in controls),
                    sum(not r['caught'] for r in controls)])

    fig, ax = plt.subplots(figsize=(8.5, 4.2))
    labels = NAMES + ['control: a write\none page off']
    good = [sum(r['pass'] for r in by[f]) for f in FAULTS] + [sum(r['caught'] for r in controls)]
    bad = [sum(not r['pass'] for r in by[f]) for f in FAULTS] + [sum(not r['caught'] for r in controls)]
    y = np.arange(len(labels))[::-1]
    ax.barh(y, good, color=[COLORS['green']] * len(FAULTS) + [COLORS['blue']])
    ax.barh(y, bad, left=good, color=COLORS['red'])
    for yi, g, b in zip(y, good, bad):
        ax.text(g + b + max(good) * 0.01, yi, f'{g}' + (f' + {b} wrong' if b else ''), va='center', fontsize=9)
    ax.set_yticks(y, labels, fontsize=9)
    ax.set_xlabel('trials')
    ax.set_title(f'{n} randomized in-place refreshes with --verify-scope written (green: correct),\n'
                 'and the misplaced-write control (blue: caught by the verification)', fontsize=10)
    fig.tight_layout()
    print(plotting.save(fig, 'exp32-correctness'))


if __name__ == '__main__':
    main()
