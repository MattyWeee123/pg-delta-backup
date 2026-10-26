"""EXP-41's analysis: do data checksums change how many bytes pgdelta sends? (docs/experiments/EXP-41-checksums-bytes.md)

    .venv/bin/python analysis/exp41.py [--results results/bytes/exp41] [--figdir DIR]

Reads OFF/results.jsonl and ON/results.jsonl under --results (experiments/bytes/exp38.py with --data-checksums off
and on), pairs them by workload and step, and scores the registered predictions: the share pgdelta sends with
checksums on lies within 2 percentage points of the share with them off, and pg_controldata reports version 0 and 1.
Writes docs/experiments/data/exp41-checksums.csv and docs/figures/exp41-checksums.svg.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
BAND = 0.02


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'bytes' / 'exp41'))
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    arms = {arm: {(r['workload'], r['transactions']): r for r in map(json.loads, open(Path(a.results) / arm / 'results.jsonl'))
                  if r} for arm in ('off', 'on')}
    rows, held, versions = [], True, {arm: {r['data_checksum_version'] for r in arms[arm].values()} for arm in arms}
    for key in sorted(arms['off'], key=lambda k: (k[0] != 'tpcb', k[1])):
        off, on = arms['off'][key], arms['on'].get(key)
        diff = None if on is None else on['changed_fraction'] - off['changed_fraction']
        within = diff is not None and abs(diff) <= BAND
        held &= within
        rows.append({'workload': key[0], 'transactions': key[1], 'share_off': off['changed_fraction'],
                     'share_on': on and on['changed_fraction'], 'difference_points': diff and round(100 * diff, 2),
                     'within_2_points': within})
        print(f"  {key[0]:5s} {key[1]:>7,} | off {off['changed_fraction']:.2%} | on "
              f"{'–' if on is None else format(on['changed_fraction'], '.2%')} | "
              f"{'–' if diff is None else format(100 * diff, '+.2f')} points | {'within' if within else 'OUTSIDE'}")
    print(f"1. {'HELD' if held else 'FAILED'}: every cell within {100 * BAND:.0f} points")
    ok2 = versions == {'off': {0}, 'on': {1}}
    print(f"2. {'HELD' if ok2 else 'FAILED'}: checksum versions {versions}")
    data = ROOT / 'docs' / 'experiments' / 'data' / 'exp41-checksums.csv'
    with open(data, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(data)
    fig, ax = plt.subplots(figsize=(8, 4))
    for workload, color in (('tpcb', COLORS['red']), ('hot', COLORS['green'])):
        for arm, style, marker in (('off', '--', 'o'), ('on', '-', 's')):
            pts = sorted((n, r['changed_fraction']) for (w_, n), r in arms[arm].items() if w_ == workload)
            ax.plot([n for n, _ in pts], [f for _, f in pts], ls=style, marker=marker, color=color, mfc='white' if arm == 'off' else color,
                    label=f'{workload}, checksums {arm}')
    ax.set_xscale('log')
    ax.set_ylim(0, 1)
    ax.set_xlabel('transactions since the old copy was taken')
    ax.set_ylabel('share of the database pgdelta sends')
    ax.set_title('Data checksums on against off: changed pages (pgbench scale 20)', fontsize=10)
    ax.legend(fontsize=8, loc='upper left')
    fig.tight_layout()
    print(plotting.save(fig, 'exp41-checksums'))


if __name__ == '__main__':
    main()
