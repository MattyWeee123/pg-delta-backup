"""EXP-69b's analysis: idea 1 with its guard fixed, its copy started, and the hint hazard made real
(docs/experiments/EXP-69b-idea-1-fixed-guard.md).

    .venv/bin/python analysis/exp69b.py [--results results/exp69b]

Reads each build's unioncheck-69b.json (experiments/tricky_copies/union_check.py --exp69b). Scores 1 (deciding:
wherever the guard passes, idea 1's copy starts and its rows equal S's, with no real wrong page and no hint S's
pg_xact contradicts left unsent; and the guard passes kept S2, kept S2c, default S2c and kept S2h), 2 (the guard
refuses kept S2b and S2d, default S2, kept S2i and kept S2 with its WAL cut, each for its reason), 3 (the hazard is
real: in kept S2i the union, scored although refused, leaves a contradicted hint and its copy's rows differ from S's),
4 (wherever the guard passes and pg_rewind runs, it takes no block pg_waldump doesn't name, and every block pg_waldump
names that it doesn't take lies outside one side's file) and 5 (idea 1 sends at most 1.5 times the pages that
differ). Writes docs/experiments/data/exp69b-builds.csv and the figure exp69b-union.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
PASS = [('kept', 'S2'), ('kept', 'S2c'), ('default', 'S2c'), ('kept', 'S2h')]
REFUSE = {('kept', 'S2b'): 'not stopped cleanly', ('kept', 'S2d'): 'not stopped cleanly',
          ('default', 'S2'): 'no checkpoint before the fork', ('kept', 'S2i'): 'neither data checksums nor wal_log_hints'}
CUT_REASON = "before B's end"
ORDER = PASS + list(REFUSE)


def rows(results):
    out = []
    for path in sorted(Path(results).glob('*/*/unioncheck-69b.json')):
        r = json.loads(path.read_text())
        s, rw, copy = r.get('scored') or {}, r.get('pg_rewind') or {}, r.get('copy') or {}
        danger = r.get('dangerous_hints') or {}
        out.append({'variant': r['variant'], 'scenario': r['scenario'], 'guard_ok': r['guard']['ok'],
                    'reasons': '; '.join(r['guard']['reasons']),
                    'pages_changed': s.get('pages_changed'), 'union_sent': s.get('pages_sent'),
                    'real_wrong': s.get('real_wrong'), 'wrong': s.get('wrong'),
                    'wrong_by_category': json.dumps(s.get('by_category') or {}),
                    'dangerous_pages': danger.get('pages'), 'dangerous_tuples': danger.get('tuples'),
                    'copy_restored': copy.get('restored'), 'copy_rows_match': copy.get('logical_match'),
                    'copy_differences': json.dumps(copy.get('differences') or []),
                    'b_blocks': r.get('b_blocks'), 's_blocks': r.get('s_blocks'),
                    'rewind_exit': rw.get('exit_code'), 'rewind_blocks': rw.get('blocks'),
                    'only_pg_rewind': rw.get('only_pg_rewind'), 'only_waldump': rw.get('only_waldump'),
                    'only_waldump_by_place': json.dumps(rw.get('only_waldump_by_place') or {}),
                    'cut_guard_ok': (r.get('wal_cut') or {}).get('guard', {}).get('ok'),
                    'cut_reasons': '; '.join((r.get('wal_cut') or {}).get('guard', {}).get('reasons', []))})
    return out


def score(table):
    by = {(r['variant'], r['scenario']): r for r in table}
    missing = [k for k in ORDER if k not in by]
    if missing:
        print(f'missing builds: {missing}')
        return
    passing = [r for r in table if r['guard_ok']]
    one = (all(by[k]['guard_ok'] for k in PASS)
           and all(r['copy_rows_match'] is True and r['real_wrong'] == 0 and r['dangerous_pages'] == 0
                   for r in passing))
    print(f"1 (deciding). {'HELD' if one else 'FAILED'}: passed "
          f"{[(r['variant'], r['scenario'], r['copy_rows_match'], r['real_wrong'], r['dangerous_pages']) for r in passing]}")
    two = (all(not by[k]['guard_ok'] and why in by[k]['reasons'] for k, why in REFUSE.items())
           and by[('kept', 'S2')]['cut_guard_ok'] is False and CUT_REASON in by[('kept', 'S2')]['cut_reasons'])
    print(f"2. {'HELD' if two else 'FAILED'}: refused {[(k, by[k]['guard_ok'], by[k]['reasons'][:70]) for k in REFUSE]}; "
          f"cut: {by[('kept', 'S2')]['cut_reasons'][:70]}")
    s2i = by[('kept', 'S2i')]
    three = (s2i['dangerous_pages'] or 0) >= 1 and s2i['copy_rows_match'] is False
    print(f"3. {'HELD' if three else 'FAILED'}: kept S2i, contradicted hints on {s2i['dangerous_pages']} pages "
          f"({s2i['dangerous_tuples']} tuples), copy rows match {s2i['copy_rows_match']} {s2i['copy_differences'][:160]}")
    ran = [r for r in passing if r['rewind_exit'] == 0]
    four = bool(ran) and all(r['only_pg_rewind'] == 0 and 'inside both files' not in json.loads(r['only_waldump_by_place'])
                             for r in ran)
    print(f"4. {'HELD' if four else 'FAILED'}: "
          f"{[(r['variant'], r['scenario'], r['rewind_blocks'], r['only_pg_rewind'], r['only_waldump_by_place']) for r in ran]}"
          f"; pg_rewind didn't run on {[(r['variant'], r['scenario']) for r in passing if r['rewind_exit'] != 0]}")
    ratios = [(r['variant'], r['scenario'], round(r['union_sent'] / r['pages_changed'], 3)) for r in passing]
    five = all(x <= 1.5 for *_, x in ratios)
    print(f"5. {'HELD' if five else 'FAILED'}: sent / pages that differ {ratios} (bar 1.5)")


def figure(table):
    by = {(r['variant'], r['scenario']): r for r in table}
    shown = [by[k] for k in ORDER if k in by and by[k]['union_sent'] is not None]
    plotting.style()
    fig, ax = plt.subplots(figsize=(10.4, 4.6))
    for i, r in enumerate(shown):
        x = i * 3
        ax.bar(x, r['pages_changed'], 0.9, color=COLORS['black'], label='pages that differ' if i == 0 else None)
        passed = r['guard_ok']
        ax.bar(x + 1, r['union_sent'], 0.9, color=COLORS['blue'] if passed else COLORS['grey'],
               label=("idea 1 sends (guard passed)" if passed else "idea 1 would send (guard refused)")
               if i in (0, next((j for j, q in enumerate(shown) if not q['guard_ok']), -1)) else None)
        rows_ok = r['copy_rows_match']
        text = 'rows match' if rows_ok else 'rows differ' if rows_ok is False else 'not started'
        ax.annotate(f"{text}\n{r['real_wrong']} real wrong\n{r['dangerous_pages']} contradicted hints", (x + 1, r['union_sent']),
                    textcoords='offset points', xytext=(0, 3), ha='center', fontsize=6.8,
                    color=COLORS['black'] if rows_ok in (True, None) else COLORS['red'])
    ax.set_xticks([i * 3 + 0.5 for i in range(len(shown))],
                  [f"{r['scenario']}, {'WAL kept' if r['variant'] == 'kept' else 'as EXP-61'}" for r in shown],
                  fontsize=7.5, rotation=15)
    top = max([r['union_sent'] for r in shown] + [r['pages_changed'] for r in shown] + [1])
    ax.set_ylim(0, top * 1.35)
    ax.set_ylabel('pages')
    ax.legend(fontsize=8, loc='upper left', ncol=3)
    ax.set_title("EXP-69b: idea 1's copy, built and started, on each failover (counted, not timed)", fontsize=10)
    fig.tight_layout()
    print(plotting.save(fig, 'exp69b-union'))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp69b'))
    a = ap.parse_args(argv)
    table = rows(a.results)
    for r in table:
        print({k: r[k] for k in ('variant', 'scenario', 'guard_ok', 'pages_changed', 'union_sent', 'real_wrong',
                                 'dangerous_pages', 'copy_rows_match', 'rewind_exit', 'only_waldump_by_place')})
    score(table)
    if table:
        with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp69b-builds.csv', 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(table[0]))
            w.writeheader()
            w.writerows(table)
        figure(table)


if __name__ == '__main__':
    main()
