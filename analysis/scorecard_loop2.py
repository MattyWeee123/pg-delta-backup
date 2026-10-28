"""Research loop 2's scorecard as a figure: per situation, pgdelta's best path against the standard tool and the
strongest competitor, from the measured results (no new data). Each cell's text and its source are in
docs/research/2026-10-28-research-loop-2.md.

    .venv/bin/python analysis/scorecard_loop2.py

Writes the figure loop2-scorecard.
"""
from __future__ import annotations

import plotting
from plotting import COLORS, plt

WIN, TIE, LOSE, OPEN, NA = 'win', 'tie', 'lose', 'open', 'n/a'
SHADE = {WIN: COLORS['green'], TIE: COLORS['yellow'], LOSE: COLORS['red'], OPEN: '#d9d9d9', NA: '#f2f2f2'}

# (situation, [(the standard tool, verdict, evidence), (the strongest competitor, verdict, evidence)])
ROWS = [
    ('Old primary after a failover\n(18 GB, beyond memory)',
     [('pg_rewind', WIN, '0.20 hot, 0.11 scattered\n(EXP-71)'), ('re-clone', WIN, '0.08 hot, 0.53 scattered\n(EXP-71)')]),
    ('Standby behind, WAL archive local\n(18 GB)',
     [('archive replay', WIN, '0.16 hot, 0.65 scattered\n(EXP-65, older fast path)'),
      ('re-clone', OPEN, 'won scattered in EXP-65;\nnot re-run since EXP-68/70')]),
    ('Standby behind, no archive,\nlineage proven (fast path)',
     [('re-clone', WIN, 'up to ~91% of pages (EXP-79b);\npast it: same bytes, a limit'),
      ('WAL-G catch-up\n(in place, page LSN)', OPEN, 'never timed; it reads every\nchanged file in full on S')]),
    ('Lineage unprovable\n(crashed copy; summaries off)',
     [('re-clone', LOSE, 'hashing reads all of B: ties\nlocal, 2.1x slower scattered (EXP-31)'),
      ('WAL-G catch-up', OPEN, 'never timed\n(page LSN, no summaries needed)')]),
    ('An earlier backup with its manifest,\nsummaries on (backup chain)',
     [('native incremental\n+ pg_combinebackup', OPEN, 'beat hashing 42 of 42 (EXP-04b);\nnever vs the fast path'),
      ('pgBackRest delta\n(fresh backup)', WIN, 'to a usable copy: 0.21-0.78\n(EXP-08; WAL backlog inflated)')]),
    ('DR re-seed, 20-120 ms\n(2.1 GB)',
     [('pg_basebackup', WIN, '0.33-0.75, hashing path\n(EXP-15, -20, -24)'),
      ('native incremental;\npgBackRest repo', WIN, 'XFS tie, ext4 faster (EXP-36);\n0.02-0.12 (EXP-59)')]),
    ('Cloud HA, same zone\n(disk clone / volume snapshot)',
     [('CNPG volume snapshot\n+ WAL archive', OPEN, 'never measured'),
      ('GCP disk clone', OPEN, '1 TiB in under a minute, full\nspeed at once (documented)')]),
]


def main():
    plotting.style()
    fig, ax = plt.subplots(figsize=(11.5, 6.4))
    ax.set_xlim(0, 3)
    ax.set_ylim(0, len(ROWS))
    ax.axis('off')
    for i, (situation, cells) in enumerate(ROWS):
        y = len(ROWS) - 1 - i
        ax.text(0.02, y + 0.5, situation, va='center', fontsize=8.5)
        for j, (competitor, verdict, evidence) in enumerate(cells):
            x = 1 + j
            ax.add_patch(plt.Rectangle((x + 0.02, y + 0.06), 0.96, 0.88, color=SHADE[verdict], alpha=0.55, lw=0))
            ax.text(x + 0.5, y + 0.66, f'{competitor}: {verdict.upper()}' if verdict != NA else '', ha='center',
                    va='center', fontsize=8, weight='bold')
            ax.text(x + 0.5, y + 0.30, evidence, ha='center', va='center', fontsize=7.2)
    ax.text(1.5, len(ROWS) + 0.15, 'against the standard tool', ha='center', fontsize=9, weight='bold')
    ax.text(2.5, len(ROWS) + 0.15, 'against the strongest competitor', ha='center', fontsize=9, weight='bold')
    ax.set_title("Research loop 2: pgdelta's best path per situation (time ratios: pgdelta / theirs)", fontsize=10,
                 pad=18)
    print(plotting.save(fig, 'loop2-scorecard'))


if __name__ == '__main__':
    main()
