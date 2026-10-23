"""Shared figure style for experiment write-ups.

Figures are saved as SVG (for the docs) and PNG (for quick previews) under
docs/figures/. Run analysis scripts with the analysis venv:
    python3 -m venv .venv && .venv/bin/pip install -r analysis/requirements.txt
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402

FIGURES = Path(__file__).resolve().parents[1] / 'docs' / 'figures'

# Okabe-Ito palette: distinguishable with common colour-vision deficiencies.
COLORS = {
    'blue': '#0072B2', 'orange': '#E69F00', 'green': '#009E73', 'red': '#D55E00',
    'purple': '#CC79A7', 'sky': '#56B4E9', 'yellow': '#F0E442', 'black': '#000000',
    'grey': '#888888',
}
METHOD_COLORS = {
    'pg_delta': COLORS['blue'], 'full': COLORS['grey'], 'incremental': COLORS['green'],
    'rsync': COLORS['orange'], 'rsync_8k': COLORS['red'], 'lsn_skip': COLORS['purple'],
}
PATTERN_COLORS = {
    'uniform': COLORS['red'], 'hot': COLORS['green'], 'clustered': COLORS['blue'],
    'append': COLORS['sky'], 'retention': COLORS['purple'], 'tpcb': COLORS['orange'],
}


def style():
    plt.rcParams.update({
        'figure.figsize': (7.0, 4.2), 'figure.dpi': 110, 'savefig.dpi': 160,
        'font.size': 10.5, 'axes.titlesize': 12, 'axes.labelsize': 10.5,
        'axes.spines.top': False, 'axes.spines.right': False,
        'axes.grid': True, 'grid.alpha': 0.25, 'legend.frameon': False,
        'svg.fonttype': 'none',
        'svg.hashsalt': 'pgdelta',  # stable element ids, so unchanged figures don't show up in git diffs
    })


def save(fig, name):
    """Save fig as docs/figures/<name>.svg and .png; return the SVG path."""
    FIGURES.mkdir(parents=True, exist_ok=True)
    if not getattr(fig, '_layout_done', False) and not fig.get_layout_engine():
        fig.tight_layout()
    svg = FIGURES / f'{name}.svg'
    fig.savefig(svg, bbox_inches='tight', metadata={'Date': None})
    fig.savefig(FIGURES / f'{name}.png', bbox_inches='tight', metadata={'Software': None})
    plt.close(fig)
    return svg
