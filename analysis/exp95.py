"""EXP-95's scorer: is EXP-94's DR win hashing ahead, or just reading ahead?

Committed before any run (rule 16). Each prediction is printed beside its registered bar:

  1  (deciding) uniform: share = (1 - _ra/v1) / (1 - _ha/v1) below 0.35; wrong above 0.65; 0.35-0.65 a split
  2  hot: _ra/v1 and _ha/v1 agree within 1.05x of each other
  3  _ra's median leaf_wait_idle_s at least 0.80x v1's (reading ahead must not fill the stream)
  4  the replication gate: _ha/v1 in uniform reproduces EXP-94's 0.422 and 0.210 within rule 17's band
  5  rule 17 cross-session: this session's `full` within 1% of EXP-94's, read from its committed CSV

Prediction 3 reads the per-run records, so it uses exp94.runs() - whose glob was corrected after EXP-94's
session read 0 of 222 records. That correction is declared in EXP-94's write-up; this scorer depends on it.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'analysis'))

import exp94  # noqa: E402  (cells, runs, medians_by_cell, reported, noise_band)

DATA = ROOT / 'docs' / 'experiments' / 'data'
EXP94_CELLS = DATA / 'exp20-cellsexp94-cubic.csv'

V1 = 'pgdelta_direct_inplace'
RA = 'pgdelta_direct_inplace_ra'
HA = 'pgdelta_direct_inplace_ha'
FULL = 'full'

CONDITIONS = ('2:20:default', '2:120:tuned')
SHARE_HOLDS, SHARE_FAILS = 0.35, 0.65     # prediction 1
HOT_BAR = 1.05                            # prediction 2
IDLE_BAR = 0.80                           # prediction 3
EXP94_HA_OVER_V1 = {'2:20:default': 0.422, '2:120:tuned': 0.210}   # prediction 4
FULL_BAR = 0.01                           # prediction 5


def ratio(cell, pattern, condition, top, bottom):
    """median(top) / median(bottom) in one cell, or None where either arm has no passing run."""
    a, b = cell.get((pattern, condition, top)), cell.get((pattern, condition, bottom))
    if a is None or b is None or not b[0]:
        return None
    return a[0] / b[0]


def exp94_full() -> dict:
    """EXP-94's `full` medians, read from the CSV it committed rather than retyped."""
    out = {}
    if not EXP94_CELLS.exists():
        return out
    with open(EXP94_CELLS, newline='', encoding='utf-8') as stream:
        for row in csv.DictReader(stream):
            try:
                out[(row['pattern'], row['condition'])] = float(row['full'])
            except (KeyError, ValueError):
                continue
    return out


def prediction_1(cell) -> list:
    rows = []
    for condition in CONDITIONS:
        ra, ha = ratio(cell, 'uniform', condition, RA, V1), ratio(cell, 'uniform', condition, HA, V1)
        if ra is None or ha is None or ha >= 1.0:
            rows.append((condition, None, None, None, 'not readable (an arm is missing, or _ha showed no gain)'))
            continue
        share = (1 - ra) / (1 - ha)
        if share < SHARE_HOLDS:
            verdict = 'held: hashing ahead carries most of the gain'
        elif share > SHARE_FAILS:
            verdict = 'FAILED: reading ahead carries most of the gain'
        else:
            verdict = 'split: neither claim supported'
        rows.append((condition, ra, ha, share, verdict))
    return rows


def prediction_2(cell) -> list:
    rows = []
    for condition in CONDITIONS:
        ra, ha = ratio(cell, 'hot', condition, RA, V1), ratio(cell, 'hot', condition, HA, V1)
        if ra is None or ha is None or not ha:
            rows.append((condition, ra, ha, None, 'not readable'))
            continue
        apart = ra / ha
        held = (1 / HOT_BAR) <= apart <= HOT_BAR
        rows.append((condition, ra, ha, apart,
                     'held: the hot gain is reading ahead' if held
                     else 'FAILED: hashing ahead helps in hot too, which neither mechanism explains'))
    return rows


def prediction_3(records) -> list:
    by_cell = exp94.medians_by_cell(records, 'uniform',
                                    lambda record: exp94.reported(record, 'leaf_wait_idle_s'))
    rows = []
    for condition in CONDITIONS:
        v1, ra, ha = (by_cell.get((condition, arm)) for arm in (V1, RA, HA))
        if not v1 or ra is None:
            rows.append((condition, v1, ra, ha, None, 'not readable'))
            continue
        rel = ra / v1
        rows.append((condition, v1, ra, ha, rel,
                     'held: reading ahead leaves the idle wait alone' if rel >= IDLE_BAR
                     else 'FAILED: reading ahead also fills the stream; the flags are not separable here'))
    return rows


def prediction_4(cell) -> list:
    rows = []
    for condition in CONDITIONS:
        ha = ratio(cell, 'uniform', condition, HA, V1)
        expected = EXP94_HA_OVER_V1[condition]
        if ha is None:
            rows.append((condition, None, expected, None, 'not readable'))
            continue
        seconds = cell[('uniform', condition, HA)][0]
        band = exp94.noise_band(seconds)
        off = abs(ha - expected) / expected
        rows.append((condition, ha, expected, off,
                     f'held (within {band:.2%})' if off <= band
                     else f'FAILED: {off:.2%} from EXP-94, band {band:.2%} - the gate is down'))
    return rows


def prediction_5(cell, prior) -> list:
    rows = []
    for pattern in ('hot', 'uniform'):
        for condition in CONDITIONS:
            here = cell.get((pattern, condition, FULL))
            there = prior.get((pattern, condition))
            if here is None or not there:
                rows.append((pattern, condition, None, there, None, 'not readable'))
                continue
            rel = here[0] / there
            rows.append((pattern, condition, here[0], there, rel,
                         'held' if abs(rel - 1) <= FULL_BAR else 'FAILED: no cross-session claim in this cell'))
    return rows


def fmt(value, places=3):
    return '-' if value is None else f'{value:.{places}f}'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--sweep', default=str(ROOT / 'results' / 'exp04' / 'exp95'))
    ap.add_argument('--out', default=str(DATA / 'exp95-cells.csv'))
    args = ap.parse_args(argv)

    cell = exp94.cells(args.sweep)
    records = exp94.runs(args.sweep)
    print(f'### cells read: {len(cell)}   per-run records read: {len(records)}\n')

    print('P1 (deciding) uniform 1%: share of _ha\'s gain that _ra delivers alone '
          f'(held below {SHARE_HOLDS}, failed above {SHARE_FAILS})')
    p1 = prediction_1(cell)
    for condition, ra, ha, share, verdict in p1:
        print(f'  {condition:16s} _ra/v1 {fmt(ra)}  _ha/v1 {fmt(ha)}  share {fmt(share)}  {verdict}')

    print(f'\nP2 hot: _ra/v1 against _ha/v1, they must agree within {HOT_BAR}x')
    for condition, ra, ha, apart, verdict in prediction_2(cell):
        print(f'  {condition:16s} _ra/v1 {fmt(ra)}  _ha/v1 {fmt(ha)}  apart {fmt(apart)}  {verdict}')

    print(f'\nP3 the mechanism: _ra\'s idle leaf wait over v1\'s, must be at least {IDLE_BAR}x')
    for condition, v1, ra, ha, rel, verdict in prediction_3(records):
        print(f'  {condition:16s} v1 {fmt(v1, 4)}s  _ra {fmt(ra, 4)}s  _ha {fmt(ha, 4)}s  '
              f'_ra/v1 {fmt(rel)}  {verdict}')

    print('\nP4 the replication gate: _ha/v1 in uniform against EXP-94\'s own ratio')
    for condition, ha, expected, off, verdict in prediction_4(cell):
        print(f'  {condition:16s} measured {fmt(ha)}  EXP-94 {fmt(expected)}  off {fmt(off)}  {verdict}')

    print(f'\nP5 rule 17: this session\'s `full` over EXP-94\'s, bar {FULL_BAR:.0%}')
    for pattern, condition, here, there, rel, verdict in prediction_5(cell, exp94_full()):
        print(f'  {pattern:8s} {condition:16s} here {fmt(here, 2)}s  EXP-94 {fmt(there, 2)}s  '
              f'ratio {fmt(rel)}  {verdict}')

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream, lineterminator='\n')
        writer.writerow(['pattern', 'condition', 'arm', 'median_seconds', 'runs'])
        for (pattern, condition, arm), (median, runs) in sorted(cell.items()):
            writer.writerow([pattern, condition, arm, f'{median:.4f}', runs])
    print(f'\n{out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
