"""EXP-43's analysis: does v1 handle AlloyDB Omni's unlogged tables? (docs/experiments/EXP-43-omni-unlogged.md)

    .venv/bin/python analysis/exp43.py [--steps results/omni/exp43/steps.jsonl]

Scores the registered predictions from experiments/omni/omni_unlogged.sh's steps and prints which forks of the
unlogged table u each copy holds: S, Omni's pg_basebackup, v1 from the stopped and from the running S. Writes
docs/experiments/data/exp43-copies.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def behaves(copy, want=None):
    s, a, f = copy.get('at_start') or {}, copy.get('after_insert') or {}, copy.get('after_freeze') or {}
    ok = (copy.get('started') is True and s.get('u_rows_at_start') == 0 and a.get('u_rows_after_insert') == 50000
          and a.get('index_checks') is not None and a.get('heap_corruptions') == 0 and copy.get('freeze_rc') == 0
          and f.get('heap_corruptions_after_freeze') == 0 and f.get('u_rows') == 50000)
    if want is not None:
        ok = ok and s.get('l_rows') == want['l_rows'] and s.get('l_sum') == want['l_sum']
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--steps', default=str(ROOT / 'results' / 'omni' / 'exp43' / 'steps.jsonl'))
    a = ap.parse_args(argv)
    steps = {}
    for line in open(a.steps):
        if line.strip():
            row = json.loads(line)
            steps[row.pop('step')] = row
    on_s = steps['forks_on_s']
    copies = {'S': on_s, "Omni's pg_basebackup": steps['forks_in_pg_basebackup']['forks'],
              'v1, stopped S': steps['stopped_forks']['forks'], 'v1, running S': steps['live_forks']['forks']}
    print('1. forks on S:', {rel: sorted(f) for rel, f in on_s.items()})
    print("2. u's forks in each copy:")
    for name, forks in copies.items():
        u = forks.get('u', {})
        print(f"     {name:22s} {', '.join(f'{k} ({v['bytes']} B{', = _init' if v['equals_init'] and k != '_init' else ''})' for k, v in sorted(u.items()))}")
    want = steps['source_state']
    stopped, live, ref = steps['stopped_copy'], steps['live_copy'], steps['reference_pg_basebackup']
    v3, v4, v5 = behaves(stopped, want), behaves(live), behaves(ref)
    print(f"3. {'HELD' if v3 else 'FAILED'} (deciding): v1 from the stopped S: exit {steps['stopped_forks']['exit']}, {stopped}")
    print(f"4. {'HELD' if v4 else 'FAILED'}: v1 from the running S: exit {steps['live_forks']['exit']}, {live}")
    print(f"5. {'HELD' if v5 else 'FAILED'}: the reference: {ref}")
    bb_has = '_mintxid' in steps['forks_in_pg_basebackup']['forks'].get('u', {})
    v1_has = '_mintxid' in steps['stopped_forks']['forks'].get('u', {})
    print(f"rule: pg_basebackup sends u's _mintxid: {bb_has}; v1 (stopped) leaves it in B: {v1_has}"
          + (' -> pgdelta must learn the fork' if v1_has and not bb_has else ''))
    out = ROOT / 'docs' / 'experiments' / 'data' / 'exp43-copies.csv'
    with open(out, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['copy', 'u_forks', 'started', 'u_rows_at_start', 'u_rows_after_insert', 'heap_corruptions',
                    'freeze_rc', 'heap_corruptions_after_freeze', 'behaves'])
        for name, copy, forks, ok in (("Omni's pg_basebackup", ref, copies["Omni's pg_basebackup"], v5),
                                      ('v1, stopped S', stopped, copies['v1, stopped S'], v3),
                                      ('v1, running S', live, copies['v1, running S'], v4)):
            s, a_, fz = copy.get('at_start') or {}, copy.get('after_insert') or {}, copy.get('after_freeze') or {}
            w.writerow([name, ' '.join(sorted(forks.get('u', {}))), copy.get('started'), s.get('u_rows_at_start'),
                        a_.get('u_rows_after_insert'), a_.get('heap_corruptions'), copy.get('freeze_rc'),
                        fz.get('heap_corruptions_after_freeze'), ok])
    print(out)


if __name__ == '__main__':
    main()
