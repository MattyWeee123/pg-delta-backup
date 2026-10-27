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
    """One row per scenario: real wrong pages left without a guard, with v1 and with v2; refusals marked."""
    plotting.style()
    fig, ax = plt.subplots(figsize=(8.6, 5.2))
    ys = list(range(len(table)))[::-1]
    arms = (('naive_real_wrong', None, 'no guard', COLORS['red']), ('v1_real_wrong', 'v1_ok', 'guard v1 (EXP-61)',
            COLORS['orange']), ('v2_real_wrong', 'v2_ok', 'guard v2', COLORS['blue']))
    for j, (key, ok, label, color) in enumerate(arms):
        for y, r in zip(ys, table):
            offset = 0.27 * (1 - j)
            ax.barh(y + offset, r[key], 0.25, color=color, label=label if y == ys[0] else None)
            text = f'{r[key]:,}' + ('' if ok is None or r[ok] else ' (refused: hashed)')
            ax.text(r[key] * 1.15 + 0.15, y + offset, text, va='center', fontsize=7)
    ax.set_xscale('symlog', linthresh=1)
    ax.set_xlim(0, 2e5)
    ax.set_yticks(ys, [r['scenario'] for r in table])
    ax.set_xlabel('real wrong pages left in the copy (symlog)')
    ax.legend(fontsize=8.5, loc='lower right')
    ax.set_title("A WAL-summary fast path: no guard, EXP-61's guard and guard v2 (S2d: the old primary's WAL lost)",
                 fontsize=10)
    print(plotting.save(fig, 'exp62-guards'))


if __name__ == '__main__':
    main()
