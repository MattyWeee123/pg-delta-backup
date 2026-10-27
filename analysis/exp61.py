"""EXP-61's analysis: would a WAL-summary fast path ever leave a wrong page, and how much reading would it save?
(docs/experiments/EXP-61-wal-summary-fast-path.md)

    .venv/bin/python analysis/exp61.py [--results results/exp61]

Reads each scenario's summaryscan.json (experiments/tricky_copies/summaryscan.py) and scenario.json. Scores
1 (deciding: the guarded path leaves no real wrong page in any scenario), 2 (the guard refuses exactly S2, S2b,
S2c, S6, S8 and S9), 3 (without the guard, real wrong pages in S2, S2b, S2c and S6) and 4 (where the guard
passes, at most half the hash path's pages read and at most 1.25 times the pages that differ sent).
Writes docs/experiments/data/exp61-scenarios.csv and the figure exp61-summary-path.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
ORDER = ('S1', 'S1b', 'S2', 'S2b', 'S2c', 'S3', 'S4', 'S5', 'S6', 'S7', 'S8', 'S9')
REFUSE = {'S2', 'S2b', 'S2c', 'S6', 'S8', 'S9'}
NAIVE_WRONG = {'S2', 'S2b', 'S2c', 'S6'}


def rows(results):
    out = []
    for d in sorted(Path(results).iterdir()):
        scored, meta = d / 'summaryscan.json', d / 'scenario.json'
        if not scored.exists():
            continue
        s, m = json.loads(scored.read_text()), json.loads(meta.read_text())
        sid = d.name.split('-')[0]
        naive, guarded = s['naive'], s['guarded']
        out.append({'scenario': sid, 'title': m.get('title'), 'built_ok': m.get('built_ok'),
                    'dump_error': s.get('dump_error'), 'summaries': s['summaries'],
                    'guard_ok': s['guard']['ok'], 'guard_reasons': '; '.join(s['guard']['reasons']),
                    'pages_source': naive['pages_source'], 'pages_basis': naive['pages_basis'],
                    'pages_changed': naive['pages_changed'], 'pages_absent_in_basis': naive['pages_absent_in_basis'],
                    'naive_sent': naive['pages_sent'], 'naive_wrong': naive['wrong'],
                    'naive_real_wrong': naive['real_wrong'],
                    'naive_wrong_by_category': json.dumps(naive['by_category'], sort_keys=True),
                    'guarded_path': guarded['path'], 'guarded_real_wrong': guarded['real_wrong'],
                    'guarded_wrong': guarded['wrong'], 'guarded_read_fraction': guarded['read_fraction'],
                    'naive_read_fraction': naive['read_fraction'], 'naive_sent_over_needed': naive['sent_over_needed']})
    return sorted(out, key=lambda r: ORDER.index(r['scenario']) if r['scenario'] in ORDER else 99)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp61'))
    a = ap.parse_args(argv)
    table = rows(a.results)
    for r in table:
        print({k: r[k] for k in ('scenario', 'built_ok', 'dump_error', 'guard_ok', 'pages_changed', 'naive_wrong',
                                 'naive_real_wrong', 'naive_wrong_by_category', 'guarded_real_wrong',
                                 'guarded_read_fraction', 'naive_sent_over_needed')})
        if r['guard_reasons']:
            print('   guard:', r['guard_reasons'])
    seen = {r['scenario'] for r in table}
    print(f"scenarios scored: {len(table)} of {len(ORDER)}; missing {sorted(set(ORDER) - seen)}; "
          f"built_ok {all(r['built_ok'] for r in table)}; dump errors {[r['scenario'] for r in table if r['dump_error']]}")
    one = len(table) == len(ORDER) and all(r['guarded_real_wrong'] == 0 for r in table)
    print(f"1 (deciding). {'HELD' if one else 'FAILED'}: guarded real wrong pages "
          f"{[(r['scenario'], r['guarded_real_wrong']) for r in table]}")
    refused = {r['scenario'] for r in table if not r['guard_ok']}
    print(f"2. {'HELD' if refused == REFUSE else 'FAILED'}: refused {sorted(refused, key=ORDER.index)} "
          f"(predicted {sorted(REFUSE, key=ORDER.index)})")
    naive = {r['scenario'] for r in table if r['naive_real_wrong'] > 0}
    three = NAIVE_WRONG <= naive
    print(f"3. {'HELD' if three else 'FAILED'}: naive real wrong in {sorted(naive, key=ORDER.index)} "
          f"(predicted at least {sorted(NAIVE_WRONG, key=ORDER.index)})")
    passed = [r for r in table if r['guard_ok']]
    four = passed and all(r['guarded_read_fraction'] <= 0.5 and r['naive_sent_over_needed'] <= 1.25 for r in passed)
    print(f"4. {'HELD' if four else 'FAILED'}: where the guard passes, read fraction and sent / needed "
          f"{[(r['scenario'], r['guarded_read_fraction'], r['naive_sent_over_needed']) for r in passed]}")
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp61-scenarios.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(table[0]))
        w.writeheader()
        w.writerows(table)
    figure(table)


def figure(table):
    plotting.style()
    fig, (left, right) = plt.subplots(1, 2, figsize=(10.5, 4.6), sharey=True)
    ys = range(len(table))[::-1]
    labels = [r['scenario'] for r in table]
    for y, r in zip(ys, table):
        left.barh(y + 0.2, max(r['naive_real_wrong'], 0), 0.4, color=COLORS['red'],
                  label='without the guard' if y == ys[0] else None)
        left.barh(y - 0.2, r['guarded_real_wrong'], 0.4, color=COLORS['blue'],
                  label='with the guard' if y == ys[0] else None)
        left.text(max(r['naive_real_wrong'], 0.8) * 1.15, y + 0.2, f"{r['naive_real_wrong']:,}", va='center', fontsize=8)
        left.text(max(r['guarded_real_wrong'], 0.8) * 1.15, y - 0.2, f"{r['guarded_real_wrong']:,}", va='center',
                  fontsize=8)
        color = COLORS['blue'] if r['guard_ok'] else COLORS['grey']
        right.barh(y, r['guarded_read_fraction'], 0.6, color=color)
        right.text(r['guarded_read_fraction'] + 0.02, y,
                   f"{r['guarded_read_fraction']:.0%}" + ('' if r['guard_ok'] else '  (refused: hash path)'),
                   va='center', fontsize=8)
    left.set_xscale('symlog', linthresh=1)
    left.set_yticks(list(ys), labels)
    left.set_xlabel('real wrong pages left in the copy (symlog)')
    left.legend(fontsize=8.5, loc='lower right')
    left.set_title('Wrong pages', fontsize=10)
    right.set_xlim(0, 1.45)
    right.set_xlabel("pages read, as a share of hashing both copies")
    right.set_title('Reading (guarded path)', fontsize=10)
    fig.suptitle('A WAL-summary fast path on the tricky old copies (EXP-03\'s scenarios, PostgreSQL 17.11)', fontsize=11)
    print(plotting.save(fig, 'exp61-summary-path'))


if __name__ == '__main__':
    main()
