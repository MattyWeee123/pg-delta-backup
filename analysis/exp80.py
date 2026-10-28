"""EXP-80's analysis: crashed standbys refreshed by the fast path with --crashed-standby, the crashed-primary and
crash-recovery-first controls, and EXP-62's blind spot through crash recovery first (S2e)
(docs/experiments/EXP-80-crashed-standby.md).

    .venv/bin/python analysis/exp80.py [--trials docs/experiments/data/exp80-trials.csv]
        [--s2e docs/experiments/data/exp80-s2e.json]

Scores 1 (deciding: every crashed-standby trial exits 0, takes the fast path with the proof "crashed standby", and
its copy's rows equal the source's with pg_amcheck clean), 2 (every crashed-primary control is refused the fast path
as "in production" and its hashed copy is correct), 3 (every S2e build: refused and correct as built; after crash
recovery first, the fast path runs and the copy is wrong). Reports the crash-recovery-first trials' wrong copies, the
share of pages sent, and the one-sided 95% upper bound on the deciding arm's failure rate. Writes the figure
exp80-crashed.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]


def truthy(value):
    return str(value).lower() in ('true', '1')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--trials', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp80-trials.csv'))
    ap.add_argument('--s2e', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp80-s2e.json'))
    ap.add_argument('--figure', default='exp80-crashed')
    a = ap.parse_args(argv)
    rows = list(csv.DictReader(open(a.trials)))
    arms = {arm: [r for r in rows if r['arm'] == arm] for arm in ('crashed-standby', 'crashed-primary', 'recovery-first')}
    standby = arms['crashed-standby']
    good = [r for r in standby if r['exit'] == '0' and truthy(r['fast_used']) and r['proof'] == 'crashed standby'
            and truthy(r['rows_equal']) and truthy(r['amcheck_ok'])]
    one = bool(standby) and len(good) == len(standby)
    bound = 1 - 0.05 ** (1 / len(standby)) if standby else None
    print(f"1 (deciding). {'HELD' if one else 'FAILED'}: crashed standbys refreshed by the fast path and correct "
          f"{len(good)} of {len(standby)}; states {sorted({r['state_after_kill'] for r in standby})}; kills "
          f"{({k: sum(r['kill'] == k for r in standby) for k in ('immediate', 'sigkill')})}; failure rate below "
          f"{bound:.3%} at 95% if none failed")
    for r in standby:
        if r not in good:
            print('   not good:', {k: r.get(k) for k in ('trial', 'exit', 'fast_used', 'proof', 'refused', 'rows_equal',
                                                       'amcheck_ok', 'diff', 'stderr')})
    primary = arms['crashed-primary']
    two = bool(primary) and all(r['exit'] == '0' and not truthy(r['fast_used']) and 'in production' in r['refused']
                                and truthy(r['rows_equal']) and truthy(r['amcheck_ok']) for r in primary)
    print(f"2. {'HELD' if two else 'FAILED'}: crashed-primary controls refused and hashed correctly "
          f"{sum(not truthy(r['fast_used']) and truthy(r['rows_equal']) for r in primary)} of {len(primary)}")
    builds = json.loads(Path(a.s2e).read_text()) if Path(a.s2e).exists() else []
    three = bool(builds) and all(
        b['as_built']['exit_code'] == 0 and b['as_built']['fast_used'] is False and b['as_built']['logical_match'] is True
        and b['recovery_exit'] == 0 and b['state_after_recovery']['Database cluster state'] == 'shut down'
        and b['recovery_first']['fast_used'] is True and b['recovery_first']['logical_match'] is False for b in builds)
    print(f"3. {'HELD' if three else 'FAILED'}: S2e builds, as built refused and correct, after crash recovery first "
          f"taken and wrong: {[(b['as_built']['fast_used'], b['as_built']['logical_match'], b['recovery_first']['fast_used'], b['recovery_first']['logical_match']) for b in builds]}")
    first = arms['recovery-first']
    wrong = [r for r in first if r['exit'] == '0' and not (truthy(r['rows_equal']) and truthy(r['amcheck_ok']))]
    share = [int(r['pages_sent']) / int(r['pages_total']) for r in standby if r.get('pages_sent') and r.get('pages_total')]
    print(f"reported: crash recovery first on crashed standbys, wrong copies {len(wrong)} of {len(first)} "
          f"(fast path {sum(truthy(r['fast_used']) for r in first)}); crashed standbys' pages sent, median share "
          f"{statistics.median(share):.3f} of the pages; median seconds "
          f"{statistics.median(float(r['seconds']) for r in standby):.2f}")
    figure(arms, builds, a.figure)


def figure(arms, builds, name):
    plotting.style()
    fig, ax = plt.subplots(figsize=(8.6, 3.9))
    labels = [('crashed-standby', 'crashed standbys,\n--crashed-standby'),
              ('crashed-primary', 'crashed primaries\n(control: refused)'),
              ('recovery-first', 'crashed standbys,\ncrash recovery first')]
    for i, (arm, label) in enumerate(labels):
        rows = arms[arm]
        fast = sum(truthy(r['fast_used']) for r in rows)
        wrong = sum(r['exit'] == '0' and not (truthy(r['rows_equal']) and truthy(r['amcheck_ok'])) for r in rows)
        ax.bar(i - 0.27, len(rows), 0.26, color=COLORS['grey'], label='trials' if i == 0 else None)
        ax.bar(i, fast, 0.26, color=COLORS['blue'], label='fast path ran' if i == 0 else None)
        ax.bar(i + 0.27, wrong, 0.26, color=COLORS['red'], label='wrong copies' if i == 0 else None)
        for x, v in ((i - 0.27, len(rows)), (i, fast), (i + 0.27, wrong)):
            ax.annotate(str(v), (x, v), textcoords='offset points', xytext=(0, 2), ha='center', fontsize=8)
    if builds:
        i = len(labels)
        wrong_first = sum(b['recovery_first']['logical_match'] is False for b in builds)
        wrong_built = sum(b['as_built']['logical_match'] is False for b in builds)
        ax.bar(i - 0.14, wrong_built, 0.26, color=COLORS['green'])
        ax.bar(i + 0.14, wrong_first, 0.26, color=COLORS['red'])
        ax.annotate(f'{wrong_built} of {len(builds)}\nas built', (i - 0.14, wrong_built), textcoords='offset points',
                    xytext=(0, 2), ha='center', fontsize=7.5)
        ax.annotate(f'{wrong_first} of {len(builds)}\nrecovery first', (i + 0.14, wrong_first),
                    textcoords='offset points', xytext=(0, 2), ha='center', fontsize=7.5)
        labels.append(('s2e', "EXP-62's lost-WAL old\nprimary (S2e): wrong"))
    ax.set_xticks(range(len(labels)), [l for _, l in labels], fontsize=8.5)
    ax.set_ylabel('trials')
    ax.legend(fontsize=8, loc='upper right')
    ax.set_title('EXP-80: crashed copies and the fast path', fontsize=10)
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
