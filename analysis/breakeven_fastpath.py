"""Where pg_basebackup starts to beat the fast path: its fastest arm's time over the session's full copy, against the
share of the listed pages changed, on the 18-20 GB database beyond memory (a summary figure; no new data). Each number
is copied from the write-up named beside it.

    .venv/bin/python analysis/breakeven_fastpath.py

The share is the unfilled plan's pages over the listed bytes. Two sets of cells: the fast path before EXP-77's
receiver change and EXP-78's guard change, and as it now is (EXP-79, EXP-79b), each set with the crossing of the line
through its two cells either side of 1. Beside them, the time model's line for plans bound by bytes
(analysis/fastpath_model.py: a 3 s guard, 19,000 MB listed). Writes the figure fastpath-breakeven.
"""
from __future__ import annotations

import fastpath_model as model
import plotting
from plotting import COLORS, plt

# (experiment and cell, share of listed pages changed, the fastest fast arm's median, the same session's full copy)
# The fast path before EXP-77's receiver change and EXP-78's guard change:
CELLS = [
    ('EXP-70 uniform\n(fill 2)', 0.283, 46.6, 91.0),
    ('EXP-74 scatter4\n(fill 0)', 0.499, 54.33, 92.05),
    ('EXP-74 scatter12\n(fill 0)', 0.527, 61.32, 95.29),
    ('EXP-75 scatterall12\n(fill 0)', 0.959, 112.99, 100.94),
]
# The fast path as it now is (EXP-79's scatter12, EXP-79b's scatterall12: clean runs, each with its own session's full
# copy). EXP-79's scatterall12 is left out: it ran after scatter12, changed, and was disturbed by background writes.
CURRENT = [
    ('EXP-79 scatter12\n(fill 0)', 0.533, 58.02, 96.01),
    ('EXP-79b scatterall12\n(fill 0)', 0.958, 106.23, 101.10),
]
LISTED_MB, GUARD_S = 19000, 3.0


def model_ratio(share):
    """The model's fast path over its full copy, for a plan of long runs (bound by bytes) of `share` of the pages."""
    mb = LISTED_MB * share
    fast = GUARD_S + model.OVERHEAD_S + model.OVERHEAD_S_PER_MB * mb + mb * 1e6 / model.BYTES
    return fast / model.predict_full(LISTED_MB * 1e6)


# Label offsets (points) where two cells sit close together.
OFFSETS = {'EXP-74 scatter4': (-10, -40), 'EXP-79 scatter12': (48, -34), 'EXP-79b scatterall12': (-8, -42),
           'EXP-75 scatterall12': (-8, 9)}


def crossing(points):
    """The share where the line through two (share, ratio) points crosses 1."""
    (x0, y0), (x1, y1) = points
    return x0 + (1 - y0) * (x1 - x0) / (y1 - y0)


def main():
    plotting.style()
    fig, ax = plt.subplots(figsize=(7.8, 4.6))
    shares = [s / 100 for s in range(20, 101)]
    ax.plot(shares, [model_ratio(s) for s in shares], color=COLORS['grey'], lw=1.2, ls='--',
            label='model, plans bound by bytes (EXP-74)')
    ax.axhline(1.0, color=COLORS['black'], lw=0.8)
    for cells, color, label in ((CELLS, COLORS['grey'], 'before EXP-77 and EXP-78'),
                                (CURRENT, COLORS['blue'], 'the fast path now (EXP-79, EXP-79b)')):
        xs, ys = [c[1] for c in cells], [c[2] / c[3] for c in cells]
        ax.plot(xs, ys, 'o', color=color, ms=7, label=label + ' (median of 2)')
        for (name, share, fast, full), y in zip(cells, ys):
            offset = OFFSETS.get(name.split('\n')[0], (0, 9))
            ax.annotate(f'{name}\n{y:.2f}', (share, y), textcoords='offset points', xytext=offset,
                        ha='center', fontsize=7.5, color=color if cells is CURRENT else COLORS['black'])
    old = crossing([(c[1], c[2] / c[3]) for c in CELLS[2:4]])
    now = crossing([(c[1], c[2] / c[3]) for c in CURRENT])
    for x, color, text in ((old, COLORS['grey'], f'before\n{old:.0%}'), (now, COLORS['blue'], f'now\n{now:.0%}')):
        ax.axvline(x, color=color, lw=1, ls=':')
        ax.text(x - 0.008, 0.3, text, ha='right', fontsize=8, color=color)
    ax.set_xlim(0.2, 1.0)
    ax.set_ylim(0.2, 1.4)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f'{v:.0%}'))
    ax.set_xlabel("share of the listed pages changed (the unfilled plan's)")
    ax.set_ylabel('fast path ÷ pg_basebackup (time)')
    ax.set_title('Where pg_basebackup starts to win (18-20 GB on 8 GB VMs, balanced PD)', fontsize=10)
    ax.legend(fontsize=8, loc='upper left')
    print(plotting.save(fig, 'fastpath-breakeven'))
    print(f'crossing before {old:.3f}, now {now:.3f}; model for byte-bound plans '
          f'{next(s for s in shares if model_ratio(s) >= 1):.2f}')


if __name__ == '__main__':
    main()
