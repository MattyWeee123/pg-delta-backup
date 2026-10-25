"""EXP-25's analysis: v1's in-place refresh from a live source (docs/experiments/EXP-25-live-correctness.md).

    .venv/bin/python analysis/exp25.py [--trials results/correctness/exp25b/trials.jsonl]
        [--first-run results/correctness/exp25/trials.jsonl] [--figdir DIR]

Counts the counted trials by fault and outcome, the kills by B's state at the kill, and each negative control's
catches (the registered 90% each); the share of counted refreshes whose WAL spans two segments or more (registered:
90%, or the bound is claimed without the WAL range), and the history files sent; how many pages changed between
hashing and sending (whether the files really changed as they were read); and the upper bound on a wrong output,
1 - (1 - c)^(1/N) for zero wrong outputs in N counted trials. The first run (on the stale template, see the
document) is summarized apart, never merged. Writes docs/experiments/data/exp25-summary.csv and
docs/figures/exp25-live.svg.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter
from pathlib import Path

import numpy as np

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
FAULTS = ['none', 'diverged', 'kill', 'corrupt', 'truncate']
VARIANTS = ['before_start', 'during_start']


def bound(n, wrong, confidence):
    """Upper bound on the per-trial failure probability; exact (Clopper-Pearson) for zero failures only."""
    if n == 0 or wrong:
        return None
    return 1 - (1 - confidence) ** (1 / n)


def load(path):
    # One record per trial: a trial rerun after a harness error keeps only its last record.
    return list({r['trial']: r for r in (json.loads(line) for line in open(path) if line.strip())}.values())


def tally(rows):
    counted = [r for r in rows if 'pass' in r and not r.get('negative_control')]
    controls = {v: [r for r in rows if r.get('negative_variant') == v and 'caught' in r] for v in VARIANTS}
    segments = [(r.get('pgdelta') or {}).get('wal_segments') or 0 for r in counted]
    return counted, controls, segments


def describe(name, rows):
    counted, controls, segments = tally(rows)
    wrong = [r for r in counted if not r['pass']]
    multi = sum(s >= 2 for s in segments)
    print(f'{name}: {len(rows)} trials, {len(counted)} counted, {len(wrong)} wrong, '
          f'{sum("harness_error" in r for r in rows)} harness errors')
    for v in VARIANTS:
        print(f'  negative control {v}: caught {sum(r["caught"] for r in controls[v])} of {len(controls[v])}')
    print(f'  WAL spanning 2+ segments: {multi} of {len(counted)} ({multi / max(len(counted), 1):.1%}); '
          f'segments per refresh {min(segments, default=0)}-{max(segments, default=0)}')
    return counted, controls, segments, wrong


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--trials', default=str(ROOT / 'results' / 'correctness' / 'exp25b' / 'trials.jsonl'))
    ap.add_argument('--first-run', default=str(ROOT / 'results' / 'correctness' / 'exp25' / 'trials.jsonl'))
    ap.add_argument('--figdir')
    ap.add_argument('--tag', default='', help='suffix for the data and figure files (EXP-33: -exp33)')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    rows = load(a.trials)
    counted, controls, segments, wrong = describe('second run (registered template)', rows)
    by = {f: [r for r in counted if r['fault'] == f] for f in FAULTS}
    for f in FAULTS:
        rs = by[f]
        print(f'  {f:9s} {len(rs):5d} counted, {sum(r["pass"] for r in rs):5d} passed'
              + (f', refresh {statistics.median(r["seconds"] for r in rs):.2f} s median' if rs else ''))
    unfinished = [r for r in by['kill'] if r.get('finished_before_kill') is False]
    print(f'  kills before the process exited: {len(unfinished)}; B at the kill: '
          f'{dict(Counter(r.get("b_state_at_kill") for r in unfinished))}; '
          f'{len(by["kill"]) - len(unfinished)} after it exited')
    stats = [r.get('pgdelta') or {} for r in counted]
    changed = [s.get('pages_changed_after_hash') or 0 for s in stats]
    print(f'  history files sent: {Counter(s.get("history_files") for s in stats)}; timelines: '
          f'{Counter(s.get("timeline") for s in stats)}')
    print(f'  pages changed between hashing and sending: median {statistics.median(changed) if changed else 0}, '
          f'{sum(c > 0 for c in changed)} of {len(changed)} refreshes above 0')
    print(f'  pgdelta times within the harness\'s: {sum(bool(r.get("times_bracketed")) for r in counted)} of '
          f'{len(counted)}; hung copies: {sum(bool((r.get("copy") or {}).get("hung")) for r in rows)} '
          f'(counted {sum(bool((r.get("copy") or {}).get("hung")) for r in counted)})')
    catchers = Counter()
    for v in VARIANTS:
        for r in controls[v]:
            problems = ' '.join((r.get('copy') or {}).get('problems') or [])
            copy = r.get('copy') or {}
            catchers[(v, 'did not start' if not copy.get('started', True) else
                      'a query hung' if copy.get('hung') else
                      'pg_amcheck only' if problems.startswith('pg_amcheck') else 'bank or digest check')] += 1
    print(f'  how the controls were caught: {dict(catchers)}')
    n = len(counted)
    for c in (0.95, 0.999):
        b = bound(n, len(wrong), c)
        print(f'  upper bound on a wrong output at {c:.1%} confidence: '
              + ('not claimed (a wrong output)' if b is None else f'{b:.4%} per refresh ({b * n:.2f} / N)'))
    first = load(a.first_run) if Path(a.first_run).exists() else []
    first_segments = describe('first run (stale template, reported apart)', first)[2] if first else []

    data = ROOT / 'docs' / 'experiments' / 'data' / f'exp25-summary{a.tag}.csv'
    with open(data, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['row', 'trials', 'correct_or_caught', 'wrong_or_missed', 'median_refresh_s'])
        for fl in FAULTS:
            rs = by[fl]
            w.writerow([fl, len(rs), sum(r['pass'] for r in rs), sum(not r['pass'] for r in rs),
                        round(statistics.median(r['seconds'] for r in rs), 3) if rs else ''])
        w.writerow(['all counted', n, n - len(wrong), len(wrong), ''])
        for v in VARIANTS:
            rs = controls[v]
            w.writerow([f'negative control {v}', len(rs), sum(r['caught'] for r in rs),
                        sum(not r['caught'] for r in rs), ''])
    print(data)

    fig, (left, right) = plt.subplots(1, 2, figsize=(11.5, 4.4), gridspec_kw={'width_ratios': [1.25, 1]})
    names = ['no fault', 'B diverged', 'killed, rerun', 'B corrupted', 'B truncated',
             'control: read before start', 'control: read during start']
    good = [sum(r['pass'] for r in by[f]) for f in FAULTS] + [sum(r['caught'] for r in controls[v]) for v in VARIANTS]
    bad = [sum(not r['pass'] for r in by[f]) for f in FAULTS] + \
          [sum(not r['caught'] for r in controls[v]) for v in VARIANTS]
    y = np.arange(len(names))[::-1]
    colors = [COLORS['green']] * len(FAULTS) + [COLORS['blue']] * len(VARIANTS)
    left.barh(y, good, color=colors)
    left.barh(y, bad, left=good, color=COLORS['red'])
    for yi, p, q, k in zip(y, good, bad, range(len(names))):
        word = ('missed' if k >= len(FAULTS) else 'wrong')
        left.text(p + q + max(good) * 0.01, yi, f'{p}' + (f' + {q} {word}' if q else ''), va='center', fontsize=9)
    left.set_yticks(y, names)
    left.set_xlabel('trials')
    left.set_title(f'{n} counted refreshes from a live source (green: correct)\n'
                   'and the broken variants (blue: caught by the checks)', fontsize=10)
    hi = max(segments + first_segments + [2])
    bins = np.arange(0.5, hi + 1.5, 1)
    if first_segments:
        right.hist(first_segments, bins=bins, color=COLORS['grey'], alpha=0.8,
                   label=f'first run, stale template (16 MiB segments), n={len(first_segments)}')
    right.hist(segments, bins=bins, color=COLORS['orange'], alpha=0.85,
               label=f'second run, registered template (1 MiB), n={len(segments)}')
    right.axvline(1.5, color=COLORS['black'], lw=0.8, ls=':')
    right.set_xlabel('WAL segments a refresh sent')
    right.set_ylabel('counted refreshes')
    right.set_title('WAL segments per refresh (registered: 90% at 2 or more)', fontsize=10)
    right.legend(fontsize=8)
    fig.tight_layout()
    print(plotting.save(fig, f'exp25-live{a.tag}'))


if __name__ == '__main__':
    main()
