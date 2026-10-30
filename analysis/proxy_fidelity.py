"""Rule 13's proxy-fidelity log as a figure: each change's ratio on a cheap proxy (a local scale model) against the same
ratio on the VMs, from docs/research/proxy-fidelity.csv (kept by hand as each pair is measured). No new data.

    .venv/bin/python analysis/proxy_fidelity.py

The time-model rows (a model as the proxy, measured over predicted) are listed in the CSV but not plotted. Writes
the figure proxy-fidelity, and prints each mechanism's VM-to-proxy ratio.
"""
from __future__ import annotations

import csv
import statistics
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]


def main():
    rows = [r for r in csv.DictReader(open(ROOT / 'docs' / 'research' / 'proxy-fidelity.csv'))
            if r['proxy'].startswith('local')]
    plotting.style()
    fig, ax = plt.subplots(figsize=(6.4, 5.0))
    colors = [COLORS['blue'], COLORS['orange'], COLORS['green'], COLORS['purple'], COLORS['red'], COLORS['sky']]
    mechanisms = list(dict.fromkeys(r['mechanism'] for r in rows))
    assert len(mechanisms) <= len(colors), 'more mechanisms than colors: add one'
    for color, mechanism in zip(colors, mechanisms):
        mine = [r for r in rows if r['mechanism'] == mechanism]
        xs, ys = [float(r['proxy_ratio']) for r in mine], [float(r['vm_ratio']) for r in mine]
        ax.plot(xs, ys, 'o', color=color, ms=7, label=mechanism)
        shift = statistics.median(y / x for x, y in zip(xs, ys))
        print(f'{mechanism}: VM over proxy {shift:.3f} (median of {len(mine)})')
    top = max(1.05, max(float(r['proxy_ratio']) for r in rows) * 1.05, max(float(r['vm_ratio']) for r in rows) * 1.05)
    ax.plot([0, top], [0, top], color=COLORS['grey'], lw=1, ls='--', label='VM = proxy')
    ax.fill_between([0, top], [0, 0.875 * top], [0, 1.185 * top], color=COLORS['grey'], alpha=0.12,
                    label='rule 13\'s band (0.875-1.185)')
    ax.set_xlim(0, top)
    ax.set_ylim(0, top)
    ax.set_xlabel('the change\'s time ratio on the local scale model')
    ax.set_ylabel('the same ratio on the VMs (18-36 GB, balanced PD)')
    ax.set_title('Rule 13: how well the local proxy predicts the VMs', fontsize=10)
    ax.legend(fontsize=7, loc='upper left')
    print(plotting.save(fig, 'proxy-fidelity'))


if __name__ == '__main__':
    main()
