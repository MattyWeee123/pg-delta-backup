"""EXP-82's local timings (rule 13's local stage): page-lsn and summaries on demand against hashing, on a server that
never summarized WAL (docs/experiments/EXP-82-summaries-off-local.md).

    .venv/bin/python experiments/summaries_off/timing.py [--scale 50] [--repeats 2] [--seed 821] [--arms ...]

One local source S (PostgreSQL 17, data checksums, summarize_wal off, checkpoint_timeout 30 min, max_wal_size 4 GB,
wal_keep_size 8 GB: the VMs' settings, the WAL since each old copy kept). Two cells, in this order, each with its own
B0, a base backup taken before the cell's change:
- hot: 1% of pgbench_accounts' rows, contiguous (aid at most scale x 1,000), updated in batches of 1,000 rows;
- scatter: 0.5% of the rows, chosen by a hash of the key (about a quarter of the heap's pages), updated in one
  statement.
Each run copies B0 to B and times the refresh of B in place:
- hash: no fast path (today's path where the server doesn't summarize);
- page-lsn: --fast-path page-lsn;
- page-lsn-1 (EXP-85, not in the default arms): page-lsn with --lsn-single-read, the chosen pages sent as scanned;
- on-demand: summarize_wal turned on (a reload), then --fast-path summaries, which waits for the summarizer to pass
  the backup's start: the timed span starts before the setting is turned on. Afterwards the setting is turned off and
  its summaries removed.
Runs are shuffled per cell. Every copy is started and compared with S. The Mac holds the whole database in memory and
its caches can't be dropped, so these are a scale model's ratios, not the VMs' times. Writes
docs/experiments/data/exp82-timing.csv.
"""
import argparse
import csv
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trials  # noqa: E402

from crashed_standby_trials import PG_BIN, PORT_S, run  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CELLS = ('hot', 'scatter')
ARMS = ('hash', 'page-lsn', 'on-demand')
SINGLE_READ = 'page-lsn-1'  # EXP-85


class Lab(trials.Lab):
    def __init__(self, root, scale):
        self.root, self.rng, self.scale = root, None, scale
        self.sockets = Path(tempfile.mkdtemp(prefix='pg82t'))
        self.S = root / 'S'
        run(PG_BIN / 'initdb', '-D', self.S, '-U', 't', '-A', 'trust', '--no-sync', '--data-checksums')
        with open(self.S / 'postgresql.conf', 'a') as conf:
            conf.write('summarize_wal = off\ncheckpoint_timeout = 30min\nmax_wal_size = 4GB\nwal_keep_size = 8GB\n')
        self.ctl(self.S, PORT_S, 'start')
        run(PG_BIN / 'pgbench', '-i', '-q', '-s', scale, '-h', self.sockets, '-p', PORT_S, '-U', 't', 'postgres',
            timeout=1800)
        self.side = self.databases = 0

    def change(self, cell):
        if cell == 'hot':
            for first in range(1, self.scale * 1000 + 1, 1000):
                self.sql(f'UPDATE pgbench_accounts SET abalance = abalance + 1 WHERE aid BETWEEN {first} AND {first + 999}')
        else:
            self.sql('UPDATE pgbench_accounts SET abalance = abalance + 1 WHERE abs(hashint4(aid + 74)) % 10000 < 50')
        self.sql('CHECKPOINT')

    def lsn(self):
        return self.sql('SELECT pg_current_wal_lsn()')


def lsn_bytes(text):
    high, low = text.split('/')
    return (int(high, 16) << 32) | int(low, 16)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--scale', type=int, default=50)
    ap.add_argument('--repeats', type=int, default=2)
    ap.add_argument('--seed', type=int, default=821)
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp82-timing.csv'))
    ap.add_argument('--arms', nargs='+', choices=(*ARMS, SINGLE_READ), default=list(ARMS))
    a = ap.parse_args()
    rows = []
    with tempfile.TemporaryDirectory(prefix='exp82t-') as temp:
        root = Path(temp)
        lab = Lab(root, a.scale)
        try:
            size = int(lab.sql("SELECT pg_database_size('postgres')"))
            print(run(PG_BIN / 'postgres', '--version').stdout.strip(), f'database {size / 1e6:.0f} MB', flush=True)
            for cell in CELLS:
                b0 = root / f'B0-{cell}'
                run(PG_BIN / 'pg_basebackup', '-h', lab.sockets, '-p', PORT_S, '-U', 't', '-D', b0, '-X', 'stream',
                    '-c', 'fast')
                redo = next(line.split(': ')[1].split()[0] for line in (b0 / 'backup_label').read_text().splitlines()
                            if line.startswith('START WAL LOCATION'))
                lab.change(cell)
                wal = lsn_bytes(lab.lsn()) - lsn_bytes(redo)
                plan = [(arm, k) for arm in a.arms for k in range(a.repeats)]
                random.Random(a.seed + CELLS.index(cell)).shuffle(plan)
                for arm, k in plan:
                    b = root / 'B'
                    shutil.rmtree(b, ignore_errors=True)
                    shutil.copytree(b0, b, symlinks=True)
                    started = time.perf_counter()
                    if arm == 'on-demand':
                        lab.summaries(True)
                        result, stats, _ = lab.refresh(b, 'summaries')
                    elif arm == SINGLE_READ:
                        result, stats, _ = lab.refresh(b, 'page-lsn', '--lsn-single-read')
                    elif arm == 'hash':
                        result, stats, _ = lab.refresh(b, None)
                    else:  # two reads, explicit since K16 made single read page-lsn's default
                        result, stats, _ = lab.refresh(b, 'page-lsn', '--no-lsn-single-read')
                    seconds = time.perf_counter() - started
                    if arm == 'on-demand':
                        lab.summaries(False)
                    fast = stats.get('fast_path') or {}
                    row = {'cell': cell, 'arm': arm, 'repeat': k, 'seconds': round(seconds, 3),
                           'exit': result.returncode, 'fast_used': fast.get('used'), 'summaries': fast.get('summaries'),
                           'pages_sent': (stats.get('pages') or {}).get('sent'),
                           'pages_total': (stats.get('pages') or {}).get('total'), 'wal_bytes_since_b0': wal,
                           'database_bytes': size, 'reread_bytes': fast.get('reread_bytes'),
                           'scan_seconds': fast.get('scan_seconds'), 'held_bytes_peak': fast.get('held_bytes_peak')}
                    if result.returncode == 0:
                        equal, amcheck, _, _ = lab.verify(b)
                        row.update({'rows_equal': equal, 'amcheck_ok': amcheck})
                    rows.append(row)
                    print(f"{cell:8s} {arm:10s} {k} {row['seconds']:7.2f} s exit {row['exit']} used {row['fast_used']} "
                          f"sent {row['pages_sent']} of {row['pages_total']} rows {row.get('rows_equal')} "
                          f"amcheck {row.get('amcheck_ok')}", flush=True)
                shutil.rmtree(b0, ignore_errors=True)
        finally:
            lab.close()
    keys = list(rows[0])
    for row in rows:
        keys += [k for k in row if k not in keys]
    with open(a.out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, restval='')
        w.writeheader()
        w.writerows(rows)
    print(a.out)


if __name__ == '__main__':
    main()
