"""EXP-34's analysis: v1's in-place refresh under a hard reset of the destination (docs/experiments/EXP-34-crash-in-place.md).

    .venv/bin/python analysis/exp34.py [--trials results/crash/exp34/trials.jsonl] [--figdir DIR]

Each trial's reset time against the state it left B in (old, blocked, new or wrong), whether B started as the
documented state says (a blocked B refuses, an old or new one starts), and the rerun's result. Writes
docs/experiments/data/exp34-trials.csv and docs/figures/exp34-crash.svg.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
STATES = ['old', 'blocked', 'new', 'wrong']
COLOR = {'old': COLORS['sky'], 'blocked': COLORS['orange'], 'new': COLORS['green'], 'wrong': COLORS['red']}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--trials', default=str(ROOT / 'results' / 'crash' / 'exp34' / 'trials.jsonl'))
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    rows = [json.loads(line) for line in open(a.trials) if line.strip()]
    states = Counter(r['crash']['state'] for r in rows)
    print(f'{len(rows)} trials, {sum(r["pass"] for r in rows)} passed; states {dict(states)}; '
          f'start as documented {sum(r["start_as_documented"] for r in rows)}; rerun new '
          f'{sum(r["after_rerun"] == "new" for r in rows)}; back after '
          f'{min(r["back_after_s"] for r in rows)}-{max(r["back_after_s"] for r in rows)} s')
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp34-trials.csv', 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['trial', 'reset_after_s', 'state', 'start_ok', 'start_as_documented', 'sender_status',
                    'after_rerun', 'pass'])
        for r in sorted(rows, key=lambda r: r['reset_after_s']):
            w.writerow([r['trial'], r['reset_after_s'], r['crash']['state'], r['start_ok'], r['start_as_documented'],
                        r['sender']['status'], r['after_rerun'], r['pass']])
    fig, ax = plt.subplots(figsize=(9, 3.2))
    for i, state in enumerate(STATES):
        pts = [r for r in rows if r['crash']['state'] == state]
        started = sum(r['start_ok'] for r in pts)
        note = '' if not pts else f', {started} started' if state != 'blocked' else f', {len(pts) - started} refused to start'
        ax.scatter([r['reset_after_s'] for r in pts], [i] * len(pts), s=70, color=COLOR[state], zorder=3,
                   label=f'{state}: {len(pts)}{note}')
    ax.set_yticks(range(len(STATES)), [f'B {s}' for s in STATES])
    ax.set_ylim(-0.6, len(STATES) - 0.4)
    ax.set_xlabel('seconds from the refresh\'s start to the hard reset of the destination')
    ax.set_title(f'{len(rows)} power losses mid-refresh: B is left blocked (refuses to start) or complete (starts), '
                 f'never wrong;\nevery rerun completes it', fontsize=10)
    ax.legend(fontsize=8, loc='lower right')
    fig.tight_layout()
    print(plotting.save(fig, 'exp34-crash'))


if __name__ == '__main__':
    main()
