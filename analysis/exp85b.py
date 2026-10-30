"""EXP-85b's analysis: does page-lsn's single read change how often EXP-82's negative control comes out wrong?
(docs/experiments/EXP-85b-control-retest.md)

    .venv/bin/python analysis/exp85b.py [--controls docs/experiments/data/exp85b-controls.csv]

A control's copy is wrong when its refresh exited 0 and its rows differ from S's or pg_amcheck fails. Scores:
1 (deciding) at least 80% of the controls wrong in each mode, single read and two reads (EXP-82's bar: 24 of 30);
2 every trial ran as its mode says: the fast path used with proof backup_manifest, fast_path.lsn_single_read as the
  mode, and nothing read again with single read.
Reported: each mode's share wrong with its 95% Wilson interval, and Fisher's exact test (two-sided) of the two
shares. A pass lets EXP-85's VM stage run as registered (the owner's decision, October 30).
"""
from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BAR = 0.80


def wilson(k, n, z=1.959964):
    if not n:
        return None, None
    p = k / n
    centre, half = p + z * z / (2 * n), z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - half) / (1 + z * z / n), (centre + half) / (1 + z * z / n)


def fisher(a, b, c, d):
    """Two-sided Fisher's exact test of the table [[a, b], [c, d]]: the probability of tables at most as likely."""
    n1, n2, k = a + b, c + d, a + c

    def p(x):
        return math.comb(n1, x) * math.comb(n2, k - x) / math.comb(n1 + n2, k)

    observed = p(a)
    return min(1.0, sum(p(x) for x in range(max(0, k - n2), min(k, n1) + 1) if p(x) <= observed * (1 + 1e-9)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--controls', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp85b-controls.csv'))
    a = ap.parse_args(argv)
    rows = list(csv.DictReader(open(a.controls)))
    counts, ran = {}, True
    for mode in ('single', 'two'):
        mine = [r for r in rows if r['mode'] == mode]
        wrong = [r for r in mine if r['exit'] == '0' and (r['rows_equal'] != 'True' or r['amcheck_ok'] != 'True')]
        counts[mode] = (len(wrong), len(mine))
        bad = [r['trial'] for r in mine if r['fast_used'] != 'True' or r['proof'] != 'backup_manifest'
               or r['single_read'] != str(mode == 'single') or (mode == 'single' and r['reread_bytes'] != '0')]
        ran = ran and not bad and bool(mine)
        low, high = wilson(len(wrong), len(mine))
        print(f"{mode}: {len(wrong)} of {len(mine)} wrong ({len(wrong) / len(mine):.0%}; 95% "
              f"{low:.2f}-{high:.2f}); refreshes that failed: {sum(r['exit'] != '0' for r in mine)}; "
              f"not run as the mode says: {bad}")
    one = all(n and k / n >= BAR for k, n in counts.values())
    print(f"1 (deciding). {'HELD' if one else 'FAILED'}: at least {BAR:.0%} wrong in each mode: "
          f"{ {m: f'{k} of {n}' for m, (k, n) in counts.items()} }")
    print(f"2. {'HELD' if ran else 'FAILED'}: every trial ran as its mode says")
    (k1, n1), (k2, n2) = counts['single'], counts['two']
    print(f"reported: Fisher's exact test, single against two reads: p = {fisher(k1, n1 - k1, k2, n2 - k2):.3f}")


if __name__ == '__main__':
    main()
