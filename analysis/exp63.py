"""EXP-63's analysis: pgdelta's own fast path, live, on the tricky old copies
(docs/experiments/EXP-63-fast-path-tricky-copies.md).

    .venv/bin/python analysis/exp63.py [--results results/exp63]

Reads each scenario's fastpath_check.json (experiments/tricky_copies/fastpath_check.py). Scores 1 (deciding: every
published copy recovers and matches the source), 2 (the fast path ran exactly in S1, S1b, S4, S5 and S7) and 3 (where
it ran, at most half the pages sent). Writes docs/experiments/data/exp63-runs.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORDER = ('S1', 'S1b', 'S2', 'S2b', 'S2c', 'S2d', 'S3', 'S4', 'S5', 'S6', 'S7', 'S8', 'S9')
RUNS = {'S1', 'S1b', 'S4', 'S5', 'S7'}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp63'))
    a = ap.parse_args(argv)
    rows = []
    for path in sorted(Path(a.results).glob('*/fastpath_check.json')):
        doc = json.loads(path.read_text())
        for attempt in doc['attempts']:
            rows.append({'scenario': doc['scenario'], 'args': ' '.join(attempt['args']) or 'default',
                         **{k: attempt.get(k) for k in ('exit_code', 'mode', 'fast_used', 'proof', 'receiver_refused',
                                                        'files_chosen', 'files_whole', 'pages_sent', 'pages_total',
                                                        'restored', 'logical_match')},
                         'reason': '; '.join(attempt.get('reasons') or [])[:200]})
    rows.sort(key=lambda r: (ORDER.index(r['scenario']) if r['scenario'] in ORDER else 99, r['args']))
    for r in rows:
        print(r)
    published = [r for r in rows if r['exit_code'] == 0]
    one = len({r['scenario'] for r in rows}) == len(ORDER) and all(r['logical_match'] for r in published)
    print(f"1 (deciding). {'HELD' if one else 'FAILED'}: {len(published)} copies published, all matching the "
          f"source: {all(r['logical_match'] for r in published)}; scenarios {len({r['scenario'] for r in rows})}")
    ran = {r['scenario'] for r in rows if r['fast_used']}
    print(f"2. {'HELD' if ran == RUNS else 'FAILED'}: the fast path ran in {sorted(ran, key=ORDER.index)} "
          f"(predicted {sorted(RUNS, key=ORDER.index)})")
    fast = [r for r in rows if r['fast_used']]
    three = fast and all(r['pages_sent'] <= r['pages_total'] / 2 for r in fast)
    print(f"3. {'HELD' if three else 'FAILED'}: pages sent / total where it ran "
          f"{[(r['scenario'], r['pages_sent'], r['pages_total']) for r in fast]}")
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp63-runs.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


if __name__ == '__main__':
    main()
