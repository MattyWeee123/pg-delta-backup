"""EXP-27's analysis: were the missed during-start controls correct copies? (docs/experiments/EXP-27-control-oracle.md)

    .venv/bin/python analysis/exp27.py [--trials results/correctness/exp27/trials.jsonl] [--figdir DIR]

Per trial: EXP-25's verdict on B (caught or missed) and the oracle's (B physically identical to B2, the correct
read's copy of the same backup, or not; a B that can't recover where B2 can is wrong). The registered predictions:
B2 passes every check in every trial; every missed B is identical to B2; every wrong B is caught. Also the stale
pages by relation kind. Writes docs/experiments/data/exp27-summary.csv and docs/figures/exp27-oracle.svg.
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
KINDS = {'r': 'table', 'i': 'index', 't': 'TOAST table', 'S': 'sequence', 'm': 'materialized view'}


def classify(r):
    o = r.get('oracle') or {}
    if 'caught' not in r or not o:
        return None
    if o.get('started') is False and not o.get('wrong'):
        return None  # B2 didn't recover: no oracle
    return ('caught' if r['caught'] else 'missed', 'wrong' if o.get('wrong') else 'correct')


def kind(name):
    if name.startswith('global/'):
        return 'shared catalog'  # pg_database and the like: named only by path (the map is the bank database's)
    rel, _, k = name.rpartition(':')
    vm = k.endswith('_vm')
    k = k.removesuffix('_vm')
    label = KINDS.get(k, k) + (' (visibility map)' if vm else '')
    return ('catalog ' if rel.startswith('pg_') else '') + label


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--trials', default=str(ROOT / 'results' / 'correctness' / 'exp27' / 'trials.jsonl'))
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    rows = [json.loads(line) for line in open(a.trials) if line.strip()]
    judged = [(r, classify(r)) for r in rows]
    cells = Counter(c for _, c in judged if c)
    b2_ok = [r for r in rows if (r.get('b2') or {}).get('started') and not (r.get('b2') or {}).get('problems')]
    print(f'{len(rows)} trials; judged by the oracle: {sum(cells.values())}; harness errors: '
          f'{sum("harness_error" in r for r in rows)}; not run to completion: {sum("caught" not in r for r in rows)}')
    print(f'  1. B2 passed every check: {len(b2_ok)} of {sum(1 for r in rows if "b2" in r)}')
    for c in (('caught', 'wrong'), ('caught', 'correct'), ('missed', 'correct'), ('missed', 'wrong')):
        print(f'  {c[0]:6s} and {c[1]:7s}: {cells[c]}')
    missed = [(r['trial'], (r.get('oracle') or {}).get('pages_lsn_differ'), (r.get('oracle') or {}).get('bytes_differ'),
               (r.get('oracle') or {}).get('stale_relations')) for r, c in judged if c and c[0] == 'missed']
    print(f'  2. missed controls (trial, stale pages, differing bytes, relations): {missed}')
    print(f'  3. wrong copies caught: {cells[("caught", "wrong")]} of {cells[("caught", "wrong")] + cells[("missed", "wrong")]}')
    unrecovered = sum(1 for r, c in judged if c and (r.get('oracle') or {}).get('started') is False)
    kinds = Counter()
    for r, c in judged:
        for name, count in ((r.get('oracle') or {}).get('stale_relations') or {}).items():
            kinds[kind(name)] += count
    print(f'  wrong copies that could not recover: {unrecovered}; stale pages by kind: {dict(kinds.most_common())}')
    def logical_outcome(o):
        value = o.get('logical')
        if not isinstance(value, str):
            return 'rows or schema differ'
        return 'a query failed' if value.startswith('a query failed') else value

    logical = Counter(logical_outcome(r.get('oracle') or {}) for r, c in judged
                      if c and c[1] == 'wrong' and (r.get('oracle') or {}).get('started'))
    print(f'  wrong copies compared logically: {dict(logical)}')
    data = ROOT / 'docs' / 'experiments' / 'data' / 'exp27-summary.csv'
    with open(data, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['checks', 'oracle', 'trials'])
        for c in (('caught', 'wrong'), ('caught', 'correct'), ('missed', 'correct'), ('missed', 'wrong')):
            w.writerow([*c, cells[c]])
    print(data)

    fig, (left, right) = plt.subplots(1, 2, figsize=(11.5, 4.2), gridspec_kw={'width_ratios': [1, 1.3]})
    labels = ['caught, wrong', 'caught, correct', 'missed, correct', 'missed, wrong']
    values = [cells[('caught', 'wrong')], cells[('caught', 'correct')], cells[('missed', 'correct')],
              cells[('missed', 'wrong')]]
    colors = [COLORS['blue'], COLORS['sky'], COLORS['green'], COLORS['red']]
    bars = left.barh(range(4)[::-1], values, color=colors)
    for bar, v in zip(bars, values):
        left.text(bar.get_width() + 0.3, bar.get_y() + bar.get_height() / 2, str(v), va='center', fontsize=9)
    left.set_yticks(range(4)[::-1], [f'checks {lab.split(", ")[0]},\noracle: copy {lab.split(", ")[1]}' for lab in labels],
                    fontsize=8.5)
    left.set_xlabel('trials (the during-start broken variant)')
    left.set_title("EXP-25's checks against the oracle", fontsize=10)
    names, counts = zip(*kinds.most_common(8)) if kinds else ((), ())
    right.barh(range(len(names))[::-1], counts, color=COLORS['orange'])
    right.set_yticks(range(len(names))[::-1], names, fontsize=8.5)
    right.set_xlabel('stale pages, summed over the wrong copies that recovered')
    right.set_title('Where the broken read left stale pages', fontsize=10)
    fig.tight_layout()
    print(plotting.save(fig, 'exp27-oracle'))


if __name__ == '__main__':
    main()
