"""Figure for docs/FIRMNESS.md: which decisions steering work are firm under the ledger's rule 11.

    .venv/bin/python analysis/firmness.py
Reads docs/experiments/data/firmness.csv (one row per audited item: id, area, firm, reason, urgency) and writes
docs/figures/firmness.svg and firmness.png.

Columns:
- firm: 'firm', 'provisional', or 'provisional-lifted' (provisional, and shaped by a self-set constraint the owner
  has lifted: the two-VM limit, or a VM-hour budget);
- reason: the rule 11 tests the item fails, joined by '+' ('none' when firm). P: not measured on the relevant
  platform or for the conditions it is applied to. R: neither pre-registered nor replicated. F: flagged fragile.
  L: shaped by a lifted self-set constraint. J: a ranking or value judgment, which only the owner can settle;
- urgency: 'next' (steers the next work) or 'later'.
"""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'docs' / 'experiments' / 'data' / 'firmness.csv'

AREAS = [  # in the order of FIRMNESS.md's section 3
    ('owner', 'Owner decisions (Oct 23)'),
    ('scope', 'Scope and positioning'),
    ('correctness', 'Correctness and safety'),
    ('perf-v0', 'Performance: v0 engine'),
    ('perf-v1', 'Performance: v1 proposals'),
    ('measurement', 'Measurement and conditions'),
    ('queue', 'Queue and test rules'),
    ('exp13', "EXP-13's design"),
]
VERDICTS = [
    ('firm', 'firm', COLORS['green']),
    ('provisional', 'provisional', COLORS['orange']),
    ('provisional-lifted', 'provisional, shaped by a lifted self-set constraint', COLORS['red']),
]


def load(path=DATA):
    with path.open(newline='') as f:
        rows = list(csv.DictReader(f))
    areas = {code for code, _ in AREAS}
    verdicts = {code for code, _, _ in VERDICTS}
    for r in rows:
        if r['area'] not in areas or r['firm'] not in verdicts or r['urgency'] not in ('next', 'later'):
            raise ValueError(f"unknown value in {r}")
    return rows


def counts(rows):
    """{area: Counter(verdict)} for every area, in AREAS order."""
    return {code: Counter(r['firm'] for r in rows if r['area'] == code) for code, _ in AREAS}


def fig_firmness(rows):
    panels = [('Every item that steers work or closes a route', rows),
              ('Only the items that steer the next work', [r for r in rows if r['urgency'] == 'next'])]
    fig, axes = plt.subplots(1, 2, figsize=(11.0, 4.4), sharey=True)
    for ax, (title, subset) in zip(axes, panels):
        per_area = counts(subset)
        for y, (code, _) in enumerate(AREAS):
            left = 0
            for verdict, _, color in VERDICTS:
                n = per_area[code].get(verdict, 0)
                if n:
                    ax.barh(y, n, left=left, color=color, height=0.7)
                    ax.text(left + n / 2, y, str(n), ha='center', va='center', fontsize=8.5,
                            color='black' if verdict == 'provisional' else 'white')
                left += n
            ax.text(left + 0.3, y, f'{left}', va='center', fontsize=8.5, color=COLORS['black'])
        total = Counter(r['firm'] for r in subset)
        ax.set_title(f"{title}\n{len(subset)} items: {total.get('firm', 0)} firm, "
                     f"{total.get('provisional', 0) + total.get('provisional-lifted', 0)} provisional "
                     f"({total.get('provisional-lifted', 0)} by a lifted constraint)", fontsize=9.5)
        ax.set_xlabel('Items')
        ax.grid(axis='y', visible=False)
    axes[0].set_yticks(range(len(AREAS)), [label for _, label in AREAS], fontsize=9)
    axes[0].invert_yaxis()
    from matplotlib.patches import Patch
    fig.legend([Patch(color=c) for _, _, c in VERDICTS], [label for _, label, _ in VERDICTS], ncol=3, fontsize=8.5,
               loc='lower center', bbox_to_anchor=(0.55, -0.01))
    fig.suptitle('Rule 11 audit (October 24, 2026): how many decisions steering work are firm', fontsize=11)
    fig.subplots_adjust(left=0.2, right=0.98, top=0.8, bottom=0.2, wspace=0.08)
    fig._layout_done = True
    return plotting.save(fig, 'firmness')


def main():
    plotting.style()
    rows = load()
    total = Counter(r['firm'] for r in rows)
    print(f"{len(rows)} items: {dict(total)}")
    print(fig_firmness(rows))


if __name__ == '__main__':
    main()
