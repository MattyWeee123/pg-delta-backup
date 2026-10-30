"""EXP-85b: EXP-82's negative control (lsn-backup-end) with page-lsn's single read and with its two reads, in one
shuffled session, to measure whether single read changes how often the control's copy comes out wrong
(docs/experiments/EXP-85b-control-retest.md).

    .venv/bin/python experiments/summaries_off/controls.py [--per-mode 30] [--seed 852] [--scale 4] [--out CSV]

Each trial is trials.py's lsn-backup-end on the same lab: a base backup taken under a heavier load (8 clients), no load
after, its backup_label start moved to the backup's end, then page-lsn (--crashed-standby off, as in EXP-82 and
EXP-85), with --lsn-single-read in "single" trials and without it in "two" trials. Each copy is started and compared
with S as in trials.py. Writes docs/experiments/data/exp85b-controls.csv.
"""
import argparse
import csv
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trials  # noqa: E402  EXP-82's campaign: its lab, its trial, the copy's check

MODES = {'two': (), 'single': ('--lsn-single-read',)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--per-mode', type=int, default=30)
    ap.add_argument('--seed', type=int, default=852)
    ap.add_argument('--scale', type=int, default=4)
    ap.add_argument('--out', default=str(trials.ROOT / 'docs' / 'experiments' / 'data' / 'exp85b-controls.csv'))
    a = ap.parse_args()
    rng = random.Random(a.seed)
    modes = [mode for mode in MODES for _ in range(a.per_mode)]
    rng.shuffle(modes)
    print(trials.run(trials.PG_BIN / 'postgres', '--version').stdout.strip(), f'{len(modes)} controls', flush=True)
    rows = []
    with tempfile.TemporaryDirectory(prefix='exp85b-') as temp:
        root = Path(temp)
        lab = trials.Lab(root, a.scale, rng)
        try:
            for number, mode in enumerate(modes):
                lab.single_read = MODES[mode]
                row = {'mode': mode, **trials.trial(lab, root, number, 'lsn-backup-end', rng)}
                rows.append(row)
                print(f"{number:3d} {mode:6s} single {row.get('single_read')!s:5s} used {row.get('fast_used')!s:5s} "
                      f"exit {row['exit']} rows {row.get('rows_equal')} amcheck {row.get('amcheck_ok')} "
                      f"sent {row.get('pages_sent')} {row.get('diff', '')[:70]}", flush=True)
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
