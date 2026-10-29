"""EXP-82's analysis: correctness of --fast-path page-lsn and of summaries on demand on a server that never
summarized WAL, the negative control, and the local timings against hashing (docs/experiments/EXP-82-summaries-off-local.md).

    .venv/bin/python analysis/exp82.py [--trials docs/experiments/data/exp82-trials.csv]
        [--timing docs/experiments/data/exp82-timing.csv]

Scores 1 (deciding: every page-lsn trial, of each basis, refreshed by page-lsn with the expected proof and correct),
2 (every on-demand trial took the summaries' fast path and is correct), 3 (the negative control: at least 8 of its
10 copies wrong) and 4 (rule 13: page-lsn and on demand each at most 0.80 of hashing's time in both cells, medians).
Writes the figure exp82-local.
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'docs' / 'experiments' / 'data'
PROOFS = {'lsn-standby': 'clean shutdown in recovery', 'lsn-backup': 'backup_manifest', 'lsn-crashed': 'crashed standby'}
LSN_ARMS = tuple(PROOFS)
CELLS, ARMS = ('hot', 'scatter'), ('hash', 'page-lsn', 'on-demand')
BAR, CONTROL_WRONG = 0.80, 8


def truth(value):
    return str(value).strip() == 'True'


def correct(row):
    return row['exit'] == '0' and truth(row.get('rows_equal')) and truth(row.get('amcheck_ok'))


def upper95(failures, n):
    """The one-sided 95% upper bound on a failure rate after `failures` of `n` (exact; with none, 1 - 0.05^(1/n))."""
    if n == 0:
        return None
    if failures == 0:
        return 1 - 0.05 ** (1 / n)
    lo, hi = failures / n, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        p_le = sum(math.comb(n, k) * mid ** k * (1 - mid) ** (n - k) for k in range(failures + 1))
        lo, hi = (mid, hi) if p_le > 0.05 else (lo, mid)
    return hi


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--trials', default=str(DATA / 'exp82-trials.csv'))
    ap.add_argument('--timing', default=str(DATA / 'exp82-timing.csv'))
    ap.add_argument('--figure', default='exp82-local')
    a = ap.parse_args(argv)
    trials = list(csv.DictReader(open(a.trials)))
    timing = list(csv.DictReader(open(a.timing))) if Path(a.timing).exists() else []
    counts = {}
    lsn = [r for r in trials if r['arm'] in LSN_ARMS]
    for arm in LSN_ARMS:
        mine = [r for r in trials if r['arm'] == arm]
        good = [r for r in mine if correct(r) and truth(r['fast_used']) and r['proof'] == PROOFS[arm]]
        counts[arm] = (len(good), len(mine))
    one = bool(lsn) and all(g == n and n > 0 for g, n in counts.values())
    failures = sum(n - g for g, n in counts.values())
    bound = upper95(failures, len(lsn))
    print(f"1 (deciding). {'HELD' if one else 'FAILED'}: page-lsn trials refreshed by page-lsn with the expected proof "
          f"and correct: {counts}; failure rate below {bound:.3%} at 95%" if bound is not None else '1. no trials')
    demand = [r for r in trials if r['arm'] == 'on-demand']
    good = [r for r in demand if correct(r) and truth(r['fast_used']) and int(r['summaries'] or 0) > 0]
    counts['on-demand'] = (len(good), len(demand))
    two = bool(demand) and len(good) == len(demand)
    print(f"2. {'HELD' if two else 'FAILED'}: on-demand trials with the summaries' fast path and correct: "
          f"{len(good)} of {len(demand)}; bases {sorted({r['basis'] for r in demand})}")
    control = [r for r in trials if r['arm'] == 'lsn-backup-end']
    wrong = [r for r in control if r['exit'] == '0' and not correct(r)]
    counts['lsn-backup-end'] = (len(control) - len(wrong), len(control))
    three = len(wrong) >= CONTROL_WRONG
    print(f"3. {'HELD' if three else 'FAILED'}: the negative control (threshold at B's end): wrong {len(wrong)} of "
          f"{len(control)} (rows unequal {sum(not truth(r.get('rows_equal')) for r in wrong)}, amcheck failing "
          f"{sum(not truth(r.get('amcheck_ok')) for r in wrong)})")
    medians = {(c, arm): statistics.median(float(r['seconds']) for r in timing if r['cell'] == c and r['arm'] == arm)
               for c in CELLS for arm in ARMS if any(r['cell'] == c and r['arm'] == arm for r in timing)}
    ratios = {arm: {c: round(medians[(c, arm)] / medians[(c, 'hash')], 3) for c in CELLS
                    if (c, arm) in medians and (c, 'hash') in medians} for arm in ARMS[1:]}
    timed_ok = all(r['exit'] == '0' and correct(r) for r in timing)
    for arm, ratio in ratios.items():
        held = len(ratio) == len(CELLS) and all(v <= BAR for v in ratio.values())
        print(f"4. {'HELD' if held else 'FAILED'}: {arm} / hash {ratio} (bar {BAR}): "
              f"{'promoted to the VMs' if held else 'not promoted on local evidence'}")
    print(f"reported: the timed copies all correct: {timed_ok}; medians {({f'{c}/{arm}': round(s, 3) for (c, arm), s in medians.items()})}")
    if timing:
        print(f"reported: WAL since B0 (MB) {({r['cell']: round(int(r['wal_bytes_since_b0']) / 1e6) for r in timing})}, "
              f"database {int(timing[0]['database_bytes']) / 1e6:.0f} MB")
    figure(counts, medians, ratios, a.figure)


def figure(counts, medians, ratios, name):
    plotting.style()
    fig, (left, right) = plt.subplots(1, 2, figsize=(11.0, 4.2), gridspec_kw={'width_ratios': [1.2, 1]})
    labels = {'lsn-standby': 'page-lsn,\nstopped standby', 'lsn-backup': 'page-lsn,\nbackup under load',
              'lsn-crashed': 'page-lsn,\nkilled standby', 'on-demand': 'summaries\non demand',
              'lsn-backup-end': 'control:\nthreshold at\nB\'s end'}
    keys = [k for k in labels if k in counts]
    for i, key in enumerate(keys):
        good, n = counts[key]
        left.bar(i, good, 0.7, color=COLORS['blue'], label='correct' if i == 0 else None)
        left.bar(i, n - good, 0.7, bottom=good, color=COLORS['red'], label='wrong or refused' if i == 0 else None)
        left.annotate(f'{good} of {n}', (i, n), textcoords='offset points', xytext=(0, 3), ha='center', fontsize=8)
    left.set_xticks(range(len(keys)), [labels[k] for k in keys], fontsize=7.5)
    left.set_ylabel('trials')
    left.set_ylim(0, max(n for _, n in counts.values()) * 1.2)
    left.legend(fontsize=7.5, loc='upper right')
    left.set_title('Correctness on a server that never summarized WAL', fontsize=9.5)
    colors = {'hash': COLORS['grey'], 'page-lsn': COLORS['blue'], 'on-demand': COLORS['green']}
    for i, cell in enumerate(CELLS):
        for j, arm in enumerate(ARMS):
            if (cell, arm) not in medians:
                continue
            x = i * 4 + j
            right.bar(x, medians[(cell, arm)], 0.8, color=colors[arm], label=arm if i == 0 else None)
            right.annotate(f'{medians[(cell, arm)]:.2f} s', (x, medians[(cell, arm)]), textcoords='offset points',
                           xytext=(0, 3), ha='center', fontsize=7.5)
    right.set_xticks([1, 5], [f"{c}\n(page-lsn / hash {ratios.get('page-lsn', {}).get(c)};\non demand / hash "
                              f"{ratios.get('on-demand', {}).get(c)})" for c in CELLS], fontsize=8)
    right.set_ylabel('seconds (median), local, in memory')
    right.set_ylim(0, right.get_ylim()[1] * 1.25)
    right.legend(fontsize=7.5, loc='upper left')
    right.set_title('Local timings against hashing (rule 13)', fontsize=9.5)
    fig.suptitle('EXP-82: summaries off, the local stage', fontsize=10)
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
