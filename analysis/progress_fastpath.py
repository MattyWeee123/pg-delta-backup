"""The fast path's progress beyond memory, step by step, from the experiments' own medians (a summary figure; no new
data). Each number is copied from the write-up named beside it.

    .venv/bin/python analysis/progress_fastpath.py

Left: EXP-64's scattered cell (18 GB on 8 GB VMs, 1% of rows, 28% of pages changed), each step against the full copy
measured in the same session. Right: EXP-67's old primary after a failover, before and after idea 1 (EXP-71), against
pg_rewind and a re-clone. Writes the figure fastpath-progress.
"""
from __future__ import annotations

import plotting
from plotting import COLORS, plt

# (experiment, what changed, the fast path's median, the session's full copy, its hashing if measured)
SCATTERED = [
    ('EXP-64', 'WAL summaries,\none read at a time', 164.8, 91.2, 180.1),
    ('EXP-64b', '+ 8 reader\nthreads', 102.8, 91.0, None),
    ('EXP-68', "+ receiver's syncs\non a worker", 76.5, 90.9, None),
    ('EXP-70', '+ gaps of up to\n2 pages filled', 46.6, 91.0, None),
]
# (lag, EXP-67's pgdelta (refused, hashing), EXP-71's idea 1, pg_rewind and replay (EXP-71), a re-clone (EXP-71))
FAILOVER = [('hot rows\nupdated 10 times', 90.2, 6.9, 35.1, 90.8), ('1% of rows,\nscattered', 177.9, 48.2, 457.5, 91.1)]


def main():
    plotting.style()
    fig, (ax, bx) = plt.subplots(1, 2, figsize=(12.0, 4.6), gridspec_kw={'width_ratios': [4, 3]})
    for i, (exp, what, fast, full, hashing) in enumerate(SCATTERED):
        ax.bar(i, fast, 0.6, color=COLORS['blue'], label='pgdelta fast path' if i == 0 else None)
        ax.text(i, max(fast * 0.3, 14), f'{fast:.1f} s\n{fast / full:.2f} of full', ha='center', va='center', fontsize=8.5,
                color='white')
        ax.hlines(full, i - 0.38, i + 0.38, colors=COLORS['orange'], lw=2,
                  label="a full copy, the same session" if i == 0 else None)
        if hashing:
            ax.hlines(hashing, i - 0.38, i + 0.38, colors=COLORS['grey'], lw=1.5, linestyles='--',
                      label='pgdelta hashing both copies' if i == 0 else None)
    ax.set_xticks(range(len(SCATTERED)), [f'{exp}\n{what}' for exp, what, *_ in SCATTERED], fontsize=8.5)
    ax.set_ylabel('seconds to refresh 18 GB (median of 2)')
    ax.set_ylim(0, 205)
    ax.legend(fontsize=8, loc='upper right')
    ax.set_title('Scattered changes (28% of pages): from 1.81 to 0.51 of a full copy', fontsize=10)
    width = 0.2
    arms = (('pgdelta before idea 1 (hashing), EXP-67', COLORS['grey'], 1), ('pgdelta with idea 1, EXP-71', COLORS['blue'], 2),
            ('pg_rewind, then replay', COLORS['purple'], 3), ('a re-clone', COLORS['orange'], 4))
    for i, row in enumerate(FAILOVER):
        for j, (label, color, k) in enumerate(arms):
            x = i + (j - 1.5) * width
            bx.bar(x, row[k], width * 0.9, color=color, label=label if i == 0 else None)
            bx.annotate(f'{row[k]:.0f}' if row[k] >= 10 else f'{row[k]:.1f}', (x, row[k]), textcoords='offset points',
                        xytext=(0, 2), ha='center', fontsize=7.5)
    bx.set_yscale('log')
    bx.set_ylim(3, 1500)
    bx.set_xticks(range(len(FAILOVER)), [r[0] for r in FAILOVER], fontsize=8.5)
    bx.set_ylabel('seconds to a current copy (median, log scale)')
    bx.legend(fontsize=7.5, loc='upper left')
    bx.set_title('An old primary after a failover (18 GB)', fontsize=10)
    print(plotting.save(fig, 'fastpath-progress'))


if __name__ == '__main__':
    main()
