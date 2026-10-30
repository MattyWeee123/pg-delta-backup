"""EXP-86: EXP-80's and EXP-82's randomized correctness campaigns on a Linux VM, on the filesystem under TMPDIR
(ext4 on the source VM, XFS with reflinks on the destination), with the receiver's Linux sync paths and page-lsn's
single read mixed in (docs/experiments/EXP-86-linux-correctness.md).

    TMPDIR=/data/exp86 PG_BIN=/usr/lib/postgresql/17/bin python3 experiments/linux_campaign/campaign.py \\
        --campaign summaries|page-lsn --fs ext4 [--per-arm 60] [--controls 10] [--on-demand 10] [--seed 860]

- summaries: EXP-80's lab (summarize_wal on). Arms: crashed-standby (deciding: a standby killed mid-load by
  pg_ctl -m immediate or SIGKILL, more load, then the summaries' fast path, proof "crashed standby"; with K15's
  default) and crashed-primary (control: S killed and copied; the fast path must be refused, the copy hashed).
- page-lsn: EXP-82's lab (summarize_wal off). Arms: lsn-standby, lsn-backup and lsn-crashed (deciding; page-lsn on a
  stopped standby, a base backup taken under load, and a killed standby), on-demand (deciding; summaries turned on
  at refresh time) and lsn-backup-end (negative control; the backup's start moved to its end). Half of each page-lsn
  arm's trials have --lsn-single-read.
Each trial's receiver syncs with --fsync auto (the sync worker with the fast path, the main loop without) or
--fsync worker-writeback (the worker after sync_file_range's write-back, Linux's own path), half each. Arms and
modes are shuffled together. Every copy is started and compared with S (each pgbench table's rows by md5, the
history's count and sum, each side table's rows) and checked with pg_amcheck --heapallindexed. Writes the CSV.
"""
import argparse
import csv
import random
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'fastpath_crashed'))
sys.path.insert(0, str(HERE.parent / 'summaries_off'))
import crashed_standby_trials as summaries  # noqa: E402  EXP-80's lab and trial
import trials as page_lsn  # noqa: E402  EXP-82's lab and trial

FSYNC = ('auto', 'worker-writeback')
SINGLE_READ_ARMS = ('lsn-standby', 'lsn-backup', 'lsn-crashed', 'lsn-backup-end')


def plan(campaign, per_arm, controls, on_demand, rng):
    """[(arm, single read)], shuffled: half of each page-lsn arm single read."""
    if campaign == 'summaries':
        arms = [('crashed-standby', False)] * per_arm + [('crashed-primary', False)] * controls
    else:
        arms = [(arm, i % 2 == 0) for arm in ('lsn-standby', 'lsn-backup', 'lsn-crashed') for i in range(per_arm)]
        arms += [('on-demand', False)] * on_demand + [('lsn-backup-end', i % 2 == 0) for i in range(controls)]
    rng.shuffle(arms)
    fsync = [FSYNC[i % 2] for i in range(len(arms))]
    rng.shuffle(fsync)
    return [(arm, single, mode) for (arm, single), mode in zip(arms, fsync)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--campaign', choices=('summaries', 'page-lsn'), required=True)
    ap.add_argument('--fs', required=True, help='the filesystem under TMPDIR, recorded in each row')
    ap.add_argument('--per-arm', type=int, default=60)
    ap.add_argument('--controls', type=int, default=10)
    ap.add_argument('--on-demand', type=int, default=10)
    ap.add_argument('--seed', type=int, default=860)
    ap.add_argument('--scale', type=int, default=4)
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    trials = plan(a.campaign, a.per_arm, a.controls, a.on_demand, rng)
    module = summaries if a.campaign == 'summaries' else page_lsn
    print(summaries.run(summaries.PG_BIN / 'postgres', '--version').stdout.strip(), f'{len(trials)} trials,',
          a.campaign, 'on', a.fs, 'under', tempfile.gettempdir(), flush=True)
    rows = []
    with tempfile.TemporaryDirectory(prefix='exp86-') as temp:
        root = Path(temp)
        lab = module.Lab(root, a.scale, rng)
        try:
            for number, (arm, single, mode) in enumerate(trials):
                lab.extra = ('--fsync', mode)
                lab.single_read = ('--lsn-single-read',) if single else ()
                row = {'fs': a.fs, 'campaign': a.campaign, 'fsync_asked': mode, 'single_asked': single,
                       **module.trial(lab, root, number, arm, rng)}
                rows.append(row)
                print(f"{number:3d} {arm:15s} {mode:16s} single {single!s:5s} used {row.get('fast_used')!s:5s} "
                      f"proof {row.get('proof')!s:27s} fsync {row.get('fsync_mode')!s:16s} exit {row['exit']} "
                      f"rows {row.get('rows_equal')} amcheck {row.get('amcheck_ok')} {row.get('diff', '')[:60]}",
                      flush=True)
        finally:
            lab.close()
    keys = []
    for row in rows:
        keys += [k for k in row if k not in keys]
    with open(a.out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, restval='')
        w.writeheader()
        w.writerows(rows)
    print(a.out)


if __name__ == '__main__':
    main()
