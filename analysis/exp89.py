"""EXP-89's analysis: crashed primaries by the end of their own WAL (--crashed-primary; research loop 4), and lost
or damaged WAL refused (docs/experiments/EXP-89-crashed-primary.md).

    .venv/bin/python analysis/exp89.py [--trials docs/experiments/data/exp89-trials.csv] [--figure exp89-crashed-primary]

A copy is correct when its refresh exited 0, its rows equal S's (every pgbench table and the cold table by md5, the
history's count and sum, each side table's rows) and pg_amcheck is clean. Scores:
1 (deciding) every same and failover trial took the fast path with the proof "crashed primary" and came out correct;
2 every failover trial found blocks of B's own past the fork (diverged_blocks > 0), so that path was exercised;
3 every control (lost, damaged, lost-failover) was refused as unproven ("can't be proven") and its hashed copy
  came out correct.
Reported: the blind-spot arm (stale-wal: B's pg_wal replaced by an older copy), how many it passed and how many of
those came out wrong; each control's refusal reasons; the 95% upper bound on wrong copies per refresh in the deciding
arms (none seen: 1 - 0.05^(1/n)).
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
DECIDING, CONTROLS, BLIND = ('same', 'failover'), ('lost', 'damaged', 'lost-failover'), ('stale-wal',)


def correct(r):
    return r['exit'] == '0' and r.get('rows_equal') == 'True' and r.get('amcheck_ok') == 'True'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--trials', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp89-trials.csv'))
    ap.add_argument('--figure', default='exp89-crashed-primary')
    a = ap.parse_args(argv)
    rows = list(csv.DictReader(open(a.trials)))
    by = {arm: [r for r in rows if r['arm'] == arm] for arm in DECIDING + CONTROLS + BLIND}
    deciding = [r for arm in DECIDING for r in by[arm]]
    good = [r for r in deciding if r['fast_used'] == 'True' and r['proof'] == 'crashed primary' and correct(r)]
    one = bool(deciding) and len(good) == len(deciding)
    bound = 1 - 0.05 ** (1 / len(deciding)) if deciding else None
    print(f"1 (deciding). {'HELD' if one else 'FAILED'}: {len(good)} of {len(deciding)} crashed primaries taken by the "
          f"fast path and correct ({ {arm: sum(r in good for r in by[arm]) for arm in DECIDING} } of "
          f"{ {arm: len(by[arm]) for arm in DECIDING} }); wrong copies below {bound:.2%} per refresh at 95% if none")
    for r in deciding:
        if r not in good:
            print(f"   not taken or not correct: trial {r['trial']} {r['arm']} {r['kill']} used {r['fast_used']} proof "
                  f"{r['proof']} exit {r['exit']} rows {r.get('rows_equal')} amcheck {r.get('amcheck_ok')} "
                  f"{(r.get('refused') or '')[:120]} {(r.get('diff') or '')[:80]}")
    failover = by['failover']
    found = [r for r in failover if r.get('diverged_blocks') not in ('', None, '0')]
    two = bool(failover) and len(found) == len(failover)
    print(f"2. {'HELD' if two else 'FAILED'}: {len(found)} of {len(failover)} failover trials found B's own blocks past "
          f"the fork (median {sorted(int(r['diverged_blocks']) for r in found)[len(found) // 2] if found else None})")
    controls = [r for arm in CONTROLS for r in by[arm]]
    refused = [r for r in controls if r['fast_used'] != 'True' and "can't be proven" in (r.get('refused') or '')
               and correct(r)]
    three = bool(controls) and len(refused) == len(controls)
    print(f"3. {'HELD' if three else 'FAILED'}: {len(refused)} of {len(controls)} controls refused and hashed correctly "
          f"({ {arm: sum(r in refused for r in by[arm]) for arm in CONTROLS} } of { {arm: len(by[arm]) for arm in CONTROLS} })")
    for arm in CONTROLS:
        reasons = Counter((r.get('refused') or 'not refused').split(': ', 1)[-1].split(' (')[0][:70] for r in by[arm])
        print(f"   reported {arm}: {dict(reasons)}")
    stale = by['stale-wal']
    passed = [r for r in stale if r['fast_used'] == 'True']
    wrong = [r for r in passed if not correct(r)]
    print(f"reported blind spot (stale-wal): {len(passed)} of {len(stale)} passed the proof; {len(wrong)} of those came "
          f"out wrong")
    print(f"reported: seconds per refresh, median by arm "
          f"{ {arm: sorted(float(r['seconds']) for r in by[arm])[len(by[arm]) // 2] for arm in by if by[arm]} }")
    figure(by, a.figure)


def figure(by, name):
    plotting.style()
    fig, ax = plt.subplots(figsize=(10.5, 4.2))
    arms = DECIDING + CONTROLS + BLIND
    labels = ('same timeline', 'after a failover', 'control: WAL lost', 'control: WAL damaged',
              'control: WAL lost\nafter a failover', 'blind spot: an older\npg_wal swapped in')
    for i, arm in enumerate(arms):
        rows = by[arm]
        taken = [r for r in rows if r['fast_used'] == 'True']
        taken_ok = sum(correct(r) for r in taken)
        refused_ok = sum(correct(r) for r in rows if r['fast_used'] != 'True')
        wrong = len(rows) - taken_ok - refused_ok
        ax.bar(i, taken_ok, 0.6, color=COLORS['blue'], label='taken by the fast path, correct' if i == 0 else None)
        ax.bar(i, refused_ok, 0.6, bottom=taken_ok, color=COLORS['grey'],
               label='refused, hashed, correct' if i == 0 else None)
        ax.bar(i, wrong, 0.6, bottom=taken_ok + refused_ok, color=COLORS['red'], label='wrong copy' if i == 0 else None)
        ax.annotate(f'{len(rows)}', (i, len(rows)), textcoords='offset points', xytext=(0, 3), ha='center', fontsize=8)
    ax.set_xticks(range(len(arms)), labels, fontsize=8)
    ax.set_ylabel('trials')
    ax.set_title('EXP-89: crashed primaries by the end of their own WAL; lost or damaged WAL refused', fontsize=10)
    ax.set_ylim(0, max(len(by[arm]) for arm in arms) * 1.25)
    ax.legend(fontsize=7.5, loc='upper right')
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
