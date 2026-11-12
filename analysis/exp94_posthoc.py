"""Where EXP-94's gain actually comes from, decomposed from that session's own records.

POST HOC. Written after EXP-94 was scored and published, on data that already existed, and it is not part of
that experiment's registered scoring. It changes no verdict: predictions 3 and 6 failed against their
registered bars and still do. What it changes is the *reason*, which EXP-94 left open and EXP-95 failed to
settle with a new arm that turned out not to exist.

The question EXP-94 could not answer about itself: `--hash-ahead` took uniform 1% from 30.0 s to 6.3 s at
120 ms tuned, but its registered mechanism - removing the sender's idle wait for leaf digests - accounts for
only a small part of that, and it also went twice as fast in `hot`, where the registration argued there was
nothing to run ahead of.

Two facts from the records settle it:

  1. `leaf_requests` is **19 in every cell**, hot and uniform alike. The sender's waits for the receiver are
     per **file**, not per changed page, so a workload that changes few pages has exactly as many round-trip
     stalls to remove as one that changes many. Prediction 3's premise confused pages-to-send with
     files-to-wait-on.
  2. The time saved is overwhelmingly in **`page_send_s`** - the time `send()` blocked on page data - and only
     marginally in the protocol waits themselves. The idle wait is the *trigger*; the cost is what the
     congestion window does once the socket has drained, and that lands in the sending.

Which is EXP-21's diagnosis, confirmed on its own terms: keeping the stream full between files stops cubic
losing its window, and prediction 7 corroborates it independently - EXP-23's slow-mode rule is itself a
`page_send_s` test, and `--hash-ahead` took cubic from 2 of 3 runs slow to 0 of 3.

    python3 analysis/exp94_posthoc.py [--sweep results/exp04/sets/dr94/exp94]
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V1 = 'pgdelta_direct_inplace'
HA = 'pgdelta_direct_inplace_ha'
CONDITIONS = ('2:0:default', '2:20:default', '2:60:default', '2:120:default', '2:60:tuned', '2:120:tuned')
# The sender's four waits on the receiver (pgdelta/waits.py): ACCEPT, BASIS_NODES, LEAF_DIGESTS, COMPLETE.
WAITS = ('accept_wait_s', 'basis_wait_s', 'leaf_wait_s', 'complete_wait_s')


def records(sweep):
    """Every passing v1 or _ha record. runs/<sweep>/<name>.json - the layout run_exp04.py writes."""
    out = []
    for path in sorted(glob.glob(str(Path(sweep) / 'runs' / '*' / '*.json'))):
        with open(path, encoding='utf-8') as stream:
            record = json.load(stream)
        if record.get('status') == 'pass' and record.get('method') in (V1, HA):
            out.append(record)
    return out


def gathered(recs) -> dict:
    """{(pattern, condition, method, field): median} over the runs of each cell."""
    bucket: dict = {}
    for record in recs:
        key = (record.get('pattern'), (record.get('speed') or {}).get('label'), record['method'])
        reported = record.get('tool_reported') or {}
        for field in (*WAITS, 'page_send_s', 'leaf_requests'):
            value = reported.get(field)
            if value is not None:
                bucket.setdefault((*key, field), []).append(value)
        wall = (record.get('totals') or {}).get('wall_seconds')
        if wall is not None:
            bucket.setdefault((*key, 'total_s'), []).append(wall)
    return {key: statistics.median(values) for key, values in bucket.items()}


def rows(cells) -> list:
    out = []
    for pattern in ('hot', 'uniform'):
        for condition in CONDITIONS:
            get = lambda method, field: cells.get((pattern, condition, method, field))
            v1_total, ha_total = get(V1, 'total_s'), get(HA, 'total_s')
            if v1_total is None or ha_total is None:
                continue
            v1_send, ha_send = get(V1, 'page_send_s') or 0.0, get(HA, 'page_send_s') or 0.0
            v1_wait = sum(get(V1, field) or 0.0 for field in WAITS)
            ha_wait = sum(get(HA, field) or 0.0 for field in WAITS)
            saved = v1_total - ha_total
            out.append({
                'pattern': pattern, 'condition': condition,
                'v1_total_s': v1_total, 'ha_total_s': ha_total, 'saved_s': saved,
                'v1_page_send_s': v1_send, 'ha_page_send_s': ha_send, 'send_saved_s': v1_send - ha_send,
                'v1_waits_s': v1_wait, 'ha_waits_s': ha_wait, 'wait_saved_s': v1_wait - ha_wait,
                'v1_leaf_wait_s': get(V1, 'leaf_wait_s'), 'ha_leaf_wait_s': get(HA, 'leaf_wait_s'),
                'v1_basis_wait_s': get(V1, 'basis_wait_s'), 'ha_basis_wait_s': get(HA, 'basis_wait_s'),
                'leaf_requests': get(V1, 'leaf_requests'),
                'send_share_of_saved': (v1_send - ha_send) / saved if abs(saved) > 0.05 else None,
                'wait_share_of_saved': (v1_wait - ha_wait) / saved if abs(saved) > 0.05 else None,
            })
    return out


def fmt(value, places=2):
    return '-' if value is None else f'{value:.{places}f}'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--sweep', default=str(ROOT / 'results' / 'exp04' / 'sets' / 'dr94' / 'exp94'))
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp94-posthoc-waits.csv'))
    args = ap.parse_args(argv)

    cells = gathered(records(args.sweep))
    table = rows(cells)
    if not table:
        print(f'no v1/_ha records under {args.sweep}')
        return 1

    requests = {row['leaf_requests'] for row in table if row['leaf_requests'] is not None}
    print(f'### leaf-digest requests per run, over every cell: {sorted(requests)}')
    print('### the sender waits once per FILE, so hot and uniform have the same number of stalls to remove\n')

    for pattern in ('hot', 'uniform'):
        print(f'{pattern}:')
        print(f"  {'condition':15s} {'v1':>7s} {'_ha':>7s} {'saved':>7s} | {'send saved':>10s} {'of saved':>9s}"
              f" | {'wait saved':>10s} {'of saved':>9s}")
        for row in (r for r in table if r['pattern'] == pattern):
            share = (lambda v: '-' if v is None else f'{100 * v:7.1f}%')
            print(f"  {row['condition']:15s} {fmt(row['v1_total_s']):>7s} {fmt(row['ha_total_s']):>7s} "
                  f"{fmt(row['saved_s']):>7s} | {fmt(row['send_saved_s']):>10s} "
                  f"{share(row['send_share_of_saved']):>9s} | {fmt(row['wait_saved_s']):>10s} "
                  f"{share(row['wait_share_of_saved']):>9s}")
        print()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(table[0]), lineterminator='\n')
        writer.writeheader()
        writer.writerows(table)
    print(out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
