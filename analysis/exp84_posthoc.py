"""EXP-84, post-hoc (after the registered scoring; decides nothing): why the selector and page-lsn missed
(docs/experiments/EXP-84-held-out.md).

    .venv/bin/python analysis/exp84_posthoc.py [--cells docs/experiments/data/exp84-cells.csv] [--results results/exp04]

Left: the selector's prediction for the fill it chose against the measured time, per held-out cell, with the 15% band.
Right: the source data disk's cumulative reads in H2 (scale 1,200) for page-lsn (hot and uniform) and pg_basebackup,
from the lab's 0.25 s diskstats series, against the size of S. Prints each H2-H4 page-lsn run's reads over S's.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import plotting
from plotting import COLORS, PATTERN_COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
SECTOR = 512
# Label offsets (points, alignment), so the cells that land on one another stay readable.
OFFSETS = {'h1 tpcb': (6, 5, 'left'), 'h4 uniform': (6, -9, 'left'), 'h3 uniform': (-7, 3, 'right'),
           'h1 clustered': (6, 4, 'left'), 'h1 retention': (6, -9, 'left'), 'h3 hot': (-7, -4, 'right'),
           'h4 hot': (-5, 8, 'right'), 'h1 append': (6, -9, 'left')}


def series(path):
    rows = []
    for line in open(path):
        f = line.split()
        if len(f) >= 14:
            rows.append((float(f[0]), int(f[6])))  # time, sectors read
    return [(t - rows[0][0], (s - rows[0][1]) * SECTOR / 1e9) for t, s in rows]


def runs(results, setting):
    out = {}
    for d in sorted(Path(results).glob(f'exp84-{setting}/run-*')):
        meta = json.loads((d / 'meta.json').read_text())
        out[(meta['arm'], meta['pattern'])] = d
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--cells', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp84-cells.csv'))
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04'))
    ap.add_argument('--figure', default='exp84-misses')
    a = ap.parse_args(argv)
    cells = list(csv.DictReader(open(a.cells)))
    plotting.style()
    fig, (left, right) = plt.subplots(1, 2, figsize=(11, 4.4))
    for c in cells:
        if not c['selector_predicted_s']:
            continue
        x, y = float(c['selector_s']), float(c['selector_predicted_s'])
        left.plot(x, y, 'o' if c['setting'] != 'h3' else 's', color=PATTERN_COLORS.get(c['pattern'], COLORS['grey']),
                  ms=7, alpha=0.85)
        dx, dy, ha = OFFSETS.get(f"{c['setting']} {c['pattern']}", (6, -3, 'left'))
        left.annotate(f"{c['setting'].upper()} {c['pattern']}", (x, y), textcoords='offset points', xytext=(dx, dy),
                      ha=ha, fontsize=6.5)
    line = [1, 120]
    left.plot(line, line, color=COLORS['grey'], lw=1)
    left.fill_between(line, [v * 0.85 for v in line], [v * 1.15 for v in line], color=COLORS['grey'], alpha=0.15,
                      label='within 15% (the bar)')
    left.set_xscale('log')
    left.set_yscale('log')
    left.set_xlabel('measured seconds (the fill it chose)')
    left.set_ylabel("selector's predicted seconds")
    left.set_title('--selector: short refreshes under-predicted\n(squares: H3, pd-ssd, where it was to over-predict)',
                   fontsize=9)
    left.legend(fontsize=7.5, loc='upper left')
    h2 = runs(a.results, 'h2')
    s_gb = None
    for (arm, pattern), label, color in ((('full', 'uniform'), 'pg_basebackup (uniform)', COLORS['orange']),
                                         (('sb_lsn', 'hot'), 'page-lsn, hot', COLORS['green']),
                                         (('sb_lsn', 'uniform'), 'page-lsn, uniform (28% of pages)', COLORS['purple'])):
        d = h2.get((arm, pattern))
        if not d:
            continue
        pts = series(d / 'series-source.txt')
        right.plot([t for t, _ in pts], [g for _, g in pts], color=color, lw=1.6, label=label)
        if arm == 'sb_lsn':
            stats = json.loads((d / 'send.json').read_text())
            s_gb = stats['fast_path']['pages_scanned'] * 8192 / 1e9
    if s_gb:
        right.axhline(s_gb, color=COLORS['grey'], lw=1, ls='--', label=f'|S| ({s_gb:.1f} GB)')
    right.set_xlabel('seconds into the refresh')
    right.set_ylabel("GB read from the source's data disk")
    right.set_title('page-lsn at scale 1,200: 11% more read than S\n(H2; the disk at its ~205 MB/s cap throughout)',
                    fontsize=9)
    right.legend(fontsize=7.5, loc='lower right')
    print(plotting.save(fig, a.figure))
    for setting in ('h2', 'h3', 'h4'):
        for (arm, pattern), d in sorted(runs(a.results, setting).items()):
            if arm == 'sb_lsn':
                read = series(d / 'series-source.txt')[-1][1]
                scanned = json.loads((d / 'send.json').read_text())['fast_path']['pages_scanned'] * 8192 / 1e9
                print(f'{setting} {pattern}: page-lsn read {read:.2f} GB from the source disk, S {scanned:.2f} GB '
                      f'({read / scanned:.3f})')


if __name__ == '__main__':
    main()
