"""EXP-62's analysis: can an old primary whose own WAL was lost fool EXP-61's guard, and does guard v2 close it?
(docs/experiments/EXP-62-guard-blind-spot.md)

    .venv/bin/python analysis/exp62.py [--results results/exp62]

Reads each scenario's summaryscan.json (both guards' verdicts and guarded paths). Scores 1 (deciding: guard v1
passes S2d and leaves real wrong pages there), 2 (deciding: guard v2 refuses S2d and leaves no real wrong page in
any of the 13 scenarios) and 3 (v2 passes exactly S1, S1b, S4, S5 and S7). Writes
docs/experiments/data/exp62-guards.csv and the figure exp62-guards.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
ORDER = ('S1', 'S1b', 'S2', 'S2b', 'S2c', 'S2d', 'S3', 'S4', 'S5', 'S6', 'S7', 'S8', 'S9')
V2_PASSES = {'S1', 'S1b', 'S4', 'S5', 'S7'}


def rows(results):
    out = []
    for d in sorted(Path(results).iterdir()):
        scored = d / 'summaryscan.json'
        if not scored.exists():
            continue
        s, meta = json.loads(scored.read_text()), json.loads((d / 'scenario.json').read_text())
        out.append({'scenario': d.name.split('-')[0], 'built_ok': meta.get('built_ok'),
                    'dump_error': s.get('dump_error'), 'b_state': (meta.get('old_copy') or {}).get('cluster_state'),
                    'naive_real_wrong': s['naive']['real_wrong'],
                    'naive_wrong_by_category': json.dumps(s['naive']['by_category'], sort_keys=True),
                    'v1_ok': s['guard']['ok'], 'v1_real_wrong': s['guarded']['real_wrong'],
                    'v1_reasons': '; '.join(s['guard']['reasons']),
                    'v2_ok': s['guard_v2']['ok'], 'v2_real_wrong': s['guarded_v2']['real_wrong'],
                    'v2_reasons': '; '.join(s['guard_v2']['reasons']),
                    'v2_read_fraction': s['guarded_v2']['read_fraction']})
    return sorted(out, key=lambda r: ORDER.index(r['scenario']) if r['scenario'] in ORDER else 99)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp62'))
    a = ap.parse_args(argv)
    table = rows(a.results)
    for r in table:
        print({k: r[k] for k in ('scenario', 'built_ok', 'b_state', 'naive_real_wrong', 'v1_ok', 'v1_real_wrong',
                                 'v2_ok', 'v2_real_wrong', 'v2_read_fraction')})
        for k in ('v1_reasons', 'v2_reasons'):
            if r[k]:
                print(f'   {k[:2]}: {r[k]}')
    by = {r['scenario']: r for r in table}
    print(f"scenarios: {len(table)} of {len(ORDER)}, missing {sorted(set(ORDER) - set(by))}; "
          f"built_ok {all(r['built_ok'] for r in table)}; dump errors {[r['scenario'] for r in table if r['dump_error']]}")
    s2d = by.get('S2d', {})
    one = bool(s2d.get('v1_ok')) and s2d.get('v1_real_wrong', 0) > 0
    print(f"1 (deciding). {'HELD' if one else 'FAILED'}: v1 on S2d passes={s2d.get('v1_ok')}, "
          f"real wrong {s2d.get('v1_real_wrong')} ({s2d.get('naive_wrong_by_category')})")
    two = (len(table) == len(ORDER) and s2d.get('v2_ok') is False
           and all(r['v2_real_wrong'] == 0 for r in table))
    print(f"2 (deciding). {'HELD' if two else 'FAILED'}: v2 refuses S2d={s2d.get('v2_ok') is False}; v2 real wrong "
          f"{[(r['scenario'], r['v2_real_wrong']) for r in table]}")
    passed = {r['scenario'] for r in table if r['v2_ok']}
    v1_refused = {r['scenario'] for r in table if not r['v1_ok']}
    three = passed == V2_PASSES and not (passed & v1_refused)
    print(f"3. {'HELD' if three else 'FAILED'}: v2 passes {sorted(passed, key=ORDER.index)} "
          f"(predicted {sorted(V2_PASSES, key=ORDER.index)})")
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp62-guards.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(table[0]))
        w.writeheader()
        w.writerows(table)
    figure(table)


def figure(table):
    """A grid: one row per scenario, one column per guard. Red: real wrong pages left; blue: the summaries ran
    and left none; grey: refused, so every page is hashed as pgdelta does now."""
    plotting.style()
    fig, ax = plt.subplots(figsize=(7.4, 6.0))
    columns = (('no guard', 'naive_real_wrong', None), ("guard v1\n(EXP-61)", 'v1_real_wrong', 'v1_ok'),
               ('guard v2', 'v2_real_wrong', 'v2_ok'))
    for x, (_, key, ok) in enumerate(columns):
        for y, r in enumerate(table):
            refused = ok is not None and not r[ok]
            wrong = r[key]
            color = COLORS['grey'] if refused else COLORS['red'] if wrong else COLORS['blue']
            ax.add_patch(plt.Rectangle((x + 0.04, y + 0.06), 0.92, 0.88, color=color, alpha=0.25 if refused else 0.8))
            text = 'refused:\nhashed' if refused else (f'{wrong:,} wrong' if wrong else 'summaries,\n0 wrong')
            ax.text(x + 0.5, y + 0.5, text, ha='center', va='center', fontsize=7.5,
                    color='black' if refused else 'white', fontweight='bold' if wrong and not refused else None)
    ax.set_xlim(0, len(columns))
    ax.set_ylim(len(table), 0)
    ax.set_xticks([x + 0.5 for x in range(len(columns))], [c[0] for c in columns])
    ax.xaxis.tick_top()
    ax.set_yticks([y + 0.5 for y in range(len(table))], [f"{r['scenario']}  ({r['b_state']})" for r in table],
                  fontsize=8.5)
    ax.grid(False)
    for side in ('left', 'bottom', 'top', 'right'):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0)
    ax.set_title('Real wrong pages a WAL-summary fast path leaves (S2d: the old primary lost its WAL past the split)',
                 fontsize=9.5, pad=34)
    print(plotting.save(fig, 'exp62-guards'))


if __name__ == '__main__':
    main()
