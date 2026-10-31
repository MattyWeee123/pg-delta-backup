"""EXP-91's local stage (rule 13): hashing ahead and verifying as written, on the Mac's scale model
(docs/experiments/EXP-91-hash-ahead-verify-as-written.md).

    .venv/bin/python experiments/network/exp91_local.py [--scale 100] [--repeats 2] [--seed 911]
        [--out docs/experiments/data/exp91-local.csv]

EXP-82's local lab (experiments/summaries_off/timing.py): a live PostgreSQL 17 source S with data checksums, and in
each cell a base backup B0 taken before the cell's change (hot: 1% of the rows, contiguous; scatter: 0.5% of the
rows by a hash of the key, about a quarter of the heap's pages). Each run copies B0 to B and times the refresh of B
in place by hashing (no fast path):
- hash: as today;
- hashA: --hash-ahead 64G --read-ahead 8;
- hashV: --verify-scope as-written;
- hashAV: both.
Runs are shuffled per cell. Every copy is started and compared with S (rows and pg_amcheck). The Mac holds the whole
database in memory, so none of the VMs' disk limits apply: these are a scale model's ratios, logged beside the VMs'
as rule 13's proxy pair, and a check on a real server that neither change breaks a refresh.
"""
import argparse
import csv
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'experiments' / 'summaries_off'))
from timing import CELLS, Lab  # noqa: E402  (it puts experiments/fastpath_crashed on the path)
from crashed_standby_trials import PG_BIN, PORT_S, run  # noqa: E402

FLAGS = {'hash': [], 'hashA': ['--hash-ahead', '64G', '--read-ahead', '8'],
         'hashV': ['--verify-scope', 'as-written'],
         'hashAV': ['--hash-ahead', '64G', '--read-ahead', '8', '--verify-scope', 'as-written']}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--scale', type=int, default=100)
    ap.add_argument('--repeats', type=int, default=2)
    ap.add_argument('--seed', type=int, default=911)
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp91-local.csv'))
    a = ap.parse_args()
    rows = []
    with tempfile.TemporaryDirectory(prefix='exp91l-') as temp:
        root = Path(temp)
        lab = Lab(root, a.scale)
        try:
            size = int(lab.sql("SELECT pg_database_size('postgres')"))
            print(run(PG_BIN / 'postgres', '--version').stdout.strip(), f'database {size / 1e6:.0f} MB', flush=True)
            for cell in CELLS:
                b0 = root / f'B0-{cell}'
                run(PG_BIN / 'pg_basebackup', '-h', lab.sockets, '-p', PORT_S, '-U', 't', '-D', b0, '-X', 'stream',
                    '-c', 'fast')
                lab.change(cell)
                plan = [(arm, k) for arm in FLAGS for k in range(a.repeats)]
                random.Random(a.seed + CELLS.index(cell)).shuffle(plan)
                for arm, k in plan:
                    b = root / 'B'
                    shutil.rmtree(b, ignore_errors=True)
                    shutil.copytree(b0, b, symlinks=True)
                    started = time.perf_counter()
                    result, stats, _ = lab.refresh(b, None, *FLAGS[arm])
                    seconds = time.perf_counter() - started
                    receiver = stats.get('receiver') or {}
                    row = {'cell': cell, 'arm': arm, 'repeat': k, 'seconds': round(seconds, 3),
                           'exit': result.returncode, 'pages_sent': (stats.get('pages') or {}).get('sent'),
                           'pages_total': (stats.get('pages') or {}).get('total'), 'database_bytes': size,
                           'nodes_reread': (receiver.get('verify_written') or {}).get('nodes_reread'),
                           'pages_read_back': (receiver.get('verify_written') or {}).get('pages_read_back'),
                           'most_ahead_bytes': (stats.get('hash_ahead') or {}).get('most_ahead_bytes')}
                    if result.returncode == 0:
                        equal, amcheck, _, _ = lab.verify(b)
                        row.update({'rows_equal': equal, 'amcheck_ok': amcheck})
                    rows.append(row)
                    print(f"{cell:8s} {arm:7s} {k} {row['seconds']:7.2f} s exit {row['exit']} sent "
                          f"{row['pages_sent']} of {row['pages_total']} rows {row.get('rows_equal')} amcheck "
                          f"{row.get('amcheck_ok')}", flush=True)
                shutil.rmtree(b0, ignore_errors=True)
        finally:
            lab.close()
    with open(a.out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), restval='')
        w.writeheader()
        w.writerows(rows)
    print(a.out)


if __name__ == '__main__':
    main()
