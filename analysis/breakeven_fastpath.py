"""Where pg_basebackup starts to beat the fast path: its fastest arm's time over the session's full copy, against the
share of the listed pages changed, on the 18-20 GB database beyond memory (a summary figure; no new data). Each number
is copied from the write-up named beside it.

    .venv/bin/python analysis/breakeven_fastpath.py

The share is the unfilled plan's pages over the listed bytes. Beside the cells, the time model's line for plans bound
by bytes (analysis/fastpath_model.py: a 3 s guard, 19,000 MB listed). Writes the figure fastpath-breakeven.
"""
from __future__ import annotations

import fastpath_model as model
import plotting
from plotting import COLORS, plt

# (experiment and cell, share of listed pages changed, the fastest fast arm's median, the same session's full copy)
CELLS = [
    ('EXP-70 uniform\n(fill 2)', 0.283, 46.6, 91.0),
    ('EXP-74 scatter4\n(fill 0)', 0.499, 54.33, 92.05),
    ('EXP-74 scatter12\n(fill 0)', 0.527, 61.32, 95.29),
    ('EXP-75 scatterall12\n(fill 0)', 0.959, 112.99, 100.94),
]
LISTED_MB, GUARD_S = 19000, 3.0


def model_ratio(share):
    """The model's fast path over its full copy, for a plan of long runs (bound by bytes) of `share` of the pages."""
    mb = LISTED_MB * share
    fast = GUARD_S + model.OVERHEAD_S + model.OVERHEAD_S_PER_MB * mb + mb * 1e6 / model.BYTES
    return fast / model.predict_full(LISTED_MB * 1e6)


def main():
    plotting.style()
    fig, ax = plt.subplots(figsize=(7.6, 4.4))
    shares = [s / 100 for s in range(20, 101)]
    ax.plot(shares, [model_ratio(s) for s in shares], color=COLORS['grey'], lw=1.5, ls='--',
            label='model, plans bound by bytes (long runs)')
    ax.axhline(1.0, color=COLORS['black'], lw=0.8)
    xs, ys = [c[1] for c in CELLS], [c[2] / c[3] for c in CELLS]
    ax.plot(xs, ys, 'o', color=COLORS['blue'], ms=7, label='measured (median of 2, one session each)')
    for (name, share, fast, full), y in zip(CELLS, ys):
        below = name.startswith('EXP-74 scatter4')  # beside scatter12's: its label goes under the point
        ax.annotate(f'{name}\n{y:.2f}', (share, y), textcoords='offset points', xytext=(0, -40 if below else 9),
                    ha='center', fontsize=8)
    # The measured crossing, interpolated between the two cells either side of it.
    (x0, y0), (x1, y1) = (xs[2], ys[2]), (xs[3], ys[3])
    cross = x0 + (1 - y0) * (x1 - x0) / (y1 - y0)
    ax.axvline(cross, color=COLORS['orange'], lw=1, ls=':')
    ax.text(cross - 0.01, 0.3, f'interpolated\nbreak-even\n{cross:.0%} of pages', ha='right', fontsize=8,
            color=COLORS['orange'])
    ax.set_xlim(0.2, 1.0)
    ax.set_ylim(0.2, 1.4)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f'{v:.0%}'))
    ax.set_xlabel("share of the listed pages changed (the unfilled plan's)")
    ax.set_ylabel('fast path ÷ pg_basebackup (time)')
    ax.set_title('Where pg_basebackup starts to win (18-20 GB on 8 GB VMs, balanced PD)', fontsize=10)
    ax.legend(fontsize=8, loc='upper left')
    print(plotting.save(fig, 'fastpath-breakeven'))
    print(f'measured crossing {cross:.3f}; model crossing for byte-bound plans '
          f'{next(s for s in shares if model_ratio(s) >= 1):.2f}')


if __name__ == '__main__':
    main()
