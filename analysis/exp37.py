"""EXP-37's figure: the bytes each strategy would send, per scenario (docs/experiments/EXP-37-units-and-rewrites.md).

    .venv/bin/python analysis/exp37.py [--results results/bytes/exp37/results.json] [--figdir DIR]

Writes docs/experiments/data/exp37-bytes.csv and docs/figures/exp37-bytes.svg.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
STRATEGIES = [('positional', "positional pages (pgdelta's)", COLORS['blue']),
              ('content_pages', 'content-addressed pages, any file', COLORS['sky']),
              ('cdc', 'content-defined chunks (avg 8 KiB)', COLORS['purple']),
              ('subpage512_total', '512-byte blocks + B\'s block digests', COLORS['green']),
              ('zstd_of_positional', 'positional pages, zstd -3', COLORS['orange'])]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'bytes' / 'exp37' / 'results.json'))
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    results = json.loads(Path(a.results).read_text())
    for r in results.values():
        r['subpage512_total'] = r['subpage512'] + r['subpage512_digest_bytes']
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp37-bytes.csv', 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['scenario', 's_relation_bytes', *[k for k, _, _ in STRATEGIES], 'subpage64', 'zstd_mib_per_cpu_s'])
        for name, r in results.items():
            w.writerow([name, r['s_relation_bytes'], *[r[k] for k, _, _ in STRATEGIES], r['subpage'],
                        r['zstd_mib_per_cpu_s']])
    names = list(results)
    x = np.arange(len(names))
    fig, ax = plt.subplots(figsize=(10, 4.3))
    width = 0.8 / len(STRATEGIES)
    for i, (key, label, color) in enumerate(STRATEGIES):
        vals = [results[n][key] / 1e6 for n in names]
        bars = ax.bar(x + (i - (len(STRATEGIES) - 1) / 2) * width, vals, width, color=color, label=label)
        for bar, v in zip(bars, vals):
            ax.annotate(f'{v:.0f}', (bar.get_x() + bar.get_width() / 2, v), textcoords='offset points',
                        xytext=(0, 2), ha='center', fontsize=7)
    ax.set_xticks(x, ['UPDATE 1% at random', '... then VACUUM FULL', '... then CLUSTER (another order)'])
    ax.set_ylabel('MB S would send (of %.0f MB of relation files)' % (results[names[0]]['s_relation_bytes'] / 1e6))
    ax.set_title('What whole, positional 8 KiB pages leave on the table (pgbench scale 10, hex filler)', fontsize=10)
    ax.legend(fontsize=7.5, loc='upper left')
    fig.tight_layout()
    print(plotting.save(fig, 'exp37-bytes'))


if __name__ == '__main__':
    main()
