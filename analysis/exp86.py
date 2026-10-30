"""EXP-86's analysis: EXP-80's and EXP-82's randomized correctness campaigns on Linux VMs, ext4 and XFS
(docs/experiments/EXP-86-linux-correctness.md).

    .venv/bin/python analysis/exp86.py [--data docs/experiments/data] [--figure exp86-linux]

Reads exp86-{ext4,xfs}-{summaries,page-lsn}.csv. A copy is correct when its refresh exited 0, its rows equal S's and
pg_amcheck is clean. Scores, per filesystem:
1 (deciding) every deciding trial took the fast path with its expected proof and came out correct: crashed-standby
  ("crashed standby"), lsn-standby ("clean shutdown in recovery"), lsn-backup ("backup_manifest"), lsn-crashed
  ("crashed standby"), on-demand (the summaries' fast path, any of these proofs);
2 every crashed primary refused the fast path ("in production") and hashed, and came out correct;
3 at least 80% of the negative controls (lsn-backup-end) wrong (EXP-82's bar);
4 every trial ran as asked: the receiver's fsync mode as asked (auto: "worker" with the fast path, "main" without;
  worker-writeback: "worker-writeback", Linux's own path), and single read with --lsn-single-read, re-reading nothing,
  two reads re-reading what they sent.
Reported: each filesystem's and the combined 95% upper bound on wrong copies per refresh (none seen: 1 - 0.05^(1/n)).
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
PROOFS = {'crashed-standby': {'crashed standby'}, 'lsn-standby': {'clean shutdown in recovery'},
          'lsn-backup': {'backup_manifest'}, 'lsn-crashed': {'crashed standby'},
          'on-demand': {'clean shutdown in recovery', 'backup_manifest'}}
FILESYSTEMS = ('ext4', 'xfs')


def correct(r):
    return r['exit'] == '0' and r.get('rows_equal') == 'True' and r.get('amcheck_ok') == 'True'


def as_asked(r):
    fast = r['fast_used'] == 'True'
    want = {'auto': 'worker' if fast else 'main', 'worker-writeback': 'worker-writeback'}[r['fsync_asked']]
    if r.get('fsync_mode') != want:
        return False
    if r['arm'] in ('lsn-standby', 'lsn-backup', 'lsn-crashed', 'lsn-backup-end'):
        single = r['single_asked'] == 'True'
        if r.get('single_read') != str(single):
            return False
        reread = int(r.get('reread_bytes') or 0)
        if single and reread != 0:
            return False
        if not single and int(r.get('pages_sent') or 0) and reread == 0:
            return False
    return True


def bound(n):
    """The 95% upper bound on the wrong-copy rate per refresh when none of n was wrong, as text."""
    return f'{1 - 0.05 ** (1 / n):.2%}' if n else 'n/a'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--data', default=str(ROOT / 'docs' / 'experiments' / 'data'))
    ap.add_argument('--figure', default='exp86-linux')
    a = ap.parse_args(argv)
    rows = []
    for fs in FILESYSTEMS:
        for campaign in ('summaries', 'page-lsn'):
            path = Path(a.data) / f'exp86-{fs}-{campaign}.csv'
            if path.exists():
                rows += list(csv.DictReader(open(path)))
    tally, deciding_all = {}, 0
    for fs in FILESYSTEMS:
        mine = [r for r in rows if r['fs'] == fs]
        deciding = [r for r in mine if r['arm'] in PROOFS]
        good = [r for r in deciding if r['fast_used'] == 'True' and r['proof'] in PROOFS[r['arm']] and correct(r)]
        primaries = [r for r in mine if r['arm'] == 'crashed-primary']
        refused = [r for r in primaries if r['fast_used'] != 'True' and 'in production' in (r.get('refused') or '')
                   and correct(r)]
        controls = [r for r in mine if r['arm'] == 'lsn-backup-end']
        wrong = [r for r in controls if r['exit'] == '0' and not correct(r)]
        asked = [r for r in mine if as_asked(r)]
        deciding_all += len(deciding)
        tally[fs] = {arm: (sum(1 for r in good if r['arm'] == arm), sum(1 for r in deciding if r['arm'] == arm))
                     for arm in PROOFS}
        tally[fs]['crashed-primary'] = (len(refused), len(primaries))
        tally[fs]['lsn-backup-end'] = (len(wrong), len(controls))
        print(f"{fs}: {len(mine)} trials")
        ok1 = bool(deciding) and len(good) == len(deciding)
        print(f"  1 (deciding). {'HELD' if ok1 else 'FAILED'}: {len(good)} of {len(deciding)} deciding trials correct "
              f"by the fast path; by arm {({k: f'{g} of {n}' for k, (g, n) in tally[fs].items() if k in PROOFS})}; "
              f"wrong copies below {bound(len(deciding))} per refresh at 95% if none")
        for r in deciding:
            if r not in good:
                print(f"     not correct: {r['arm']} trial {r['trial']} fsync {r['fsync_asked']} single "
                      f"{r['single_asked']} used {r['fast_used']} proof {r['proof']} exit {r['exit']} rows "
                      f"{r.get('rows_equal')} amcheck {r.get('amcheck_ok')} {r.get('diff', '')[:80]}")
        ok2 = bool(primaries) and len(refused) == len(primaries)
        print(f"  2. {'HELD' if ok2 else 'FAILED'}: {len(refused)} of {len(primaries)} crashed primaries refused and "
              f"hashed correctly")
        ok3 = bool(controls) and len(wrong) / len(controls) >= 0.80
        print(f"  3. {'HELD' if ok3 else 'FAILED'}: {len(wrong)} of {len(controls)} negative controls wrong (bar 80%)")
        ok4 = bool(mine) and len(asked) == len(mine)
        print(f"  4. {'HELD' if ok4 else 'FAILED'}: {len(asked)} of {len(mine)} trials ran as asked; not: "
              f"{[(r['arm'], r['trial'], r['fsync_asked'], r.get('fsync_mode')) for r in mine if r not in asked][:6]}")
        print(f"  reported: fsync modes {sorted({(r['fsync_asked'], r.get('fsync_mode')) for r in mine})}; "
              f"single read in {sum(r['single_asked'] == 'True' for r in deciding)} deciding trials")
    print(f"reported: both filesystems, {deciding_all} deciding trials; wrong copies below "
          f"{bound(deciding_all)} per refresh at 95% if none" if deciding_all else 'no data')
    if rows:
        figure(tally, a.figure)


def figure(tally, name):
    plotting.style()
    fig, ax = plt.subplots(figsize=(11, 4.2))
    arms = ('crashed-standby', 'lsn-standby', 'lsn-backup', 'lsn-crashed', 'on-demand', 'crashed-primary',
            'lsn-backup-end')
    labels = ('crashed\nstandby', 'page-lsn:\nstopped standby', 'page-lsn:\nbase backup', 'page-lsn:\nkilled standby',
              'summaries\non demand', 'control:\ncrashed primary\n(refused, hashed)', 'control:\nstart moved\n(meant wrong)')
    for f, fs in enumerate(FILESYSTEMS):
        for i, arm in enumerate(arms):
            good, n = tally.get(fs, {}).get(arm, (0, 0))
            x = i * 3 + f
            color = COLORS['blue'] if f == 0 else COLORS['sky']
            ax.bar(x, n, 0.85, color=COLORS['grey'], alpha=0.25)
            ax.bar(x, good, 0.85, color=color if arm != 'lsn-backup-end' else COLORS['red'],
                   label=(f'{fs}: as expected' if i == 0 else None))
            ax.annotate(f'{good}/{n}', (x, n), textcoords='offset points', xytext=(0, 2), ha='center', fontsize=7)
    ax.set_xticks([i * 3 + 0.5 for i in range(len(arms))], labels, fontsize=7.5)
    ax.set_ylabel('trials (bar: as expected; grey: all)')
    ax.set_title('EXP-86: randomized correctness on Linux VMs (ext4 and XFS); controls in red are meant to be wrong',
                 fontsize=9.5)
    top = max((n for fs in tally.values() for _, n in fs.values()), default=1)
    ax.set_ylim(0, top * 1.3)
    ax.legend(fontsize=7.5, loc='upper center', ncol=2)
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
