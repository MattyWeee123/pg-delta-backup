"""EXP-38 (T11b, lean): how old can the copy be? (docs/experiments/EXP-38-copy-age.md)

    .venv/bin/python experiments/bytes/exp38.py --out results/bytes/exp38 [--scale 20] [--pg-bin DIR] [--work DIR]
        [--data-checksums off|on]

For each workload a fresh clone of the template (pgbench -i -s SCALE) is B0, the old copy; the workload then runs on
S in steps of a cumulative transaction count, and after each step S is checkpointed, stopped, measured against B0
and started again. The measure is pgdelta's positional one (exp37.py): the bytes of S's relation files whose page
differs from B0's page at the same offset. Workloads: tpcb (pgbench's built-in TPC-B-like script: every account is
equally likely), hot (the same script with aid drawn from the first 1% of accounts). One JSON object per workload and
step goes to OUT/results.jsonl, with the template's data checksum version as pg_controldata reports it.
--data-checksums (EXP-41): off is PostgreSQL 17 initdb's default, which EXP-38 ran with; on adds initdb -k.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from exp37 import PG, PAGE, PORT, pages_of, relation_files, run  # noqa: E402

STEPS = [int(x) for x in __import__("os").environ.get("EXP38_STEPS", "1000,3000,10000,30000,100000,300000").split(",")]
HOT_SCRIPT = """\\set aid random(1, 100000 * :scale / 100)
\\set bid random(1, 1 * :scale)
\\set tid random(1, 10 * :scale)
\\set delta random(-5000, 5000)
BEGIN;
UPDATE pgbench_accounts SET abalance = abalance + :delta WHERE aid = :aid;
SELECT abalance FROM pgbench_accounts WHERE aid = :aid;
UPDATE pgbench_tellers SET tbalance = tbalance + :delta WHERE tid = :tid;
UPDATE pgbench_branches SET bbalance = bbalance + :delta WHERE bid = :bid;
INSERT INTO pgbench_history (tid, bid, aid, delta, mtime) VALUES (:tid, :bid, :aid, :delta, CURRENT_TIMESTAMP);
END;
"""


def changed_bytes(s_root, b_root):
    s_files, b_files = relation_files(s_root), relation_files(b_root)
    total = changed = 0
    for rel, path in s_files.items():
        data = path.read_bytes()
        old = b_files[rel].read_bytes() if rel in b_files else b''
        total += len(data)
        for i, page in enumerate(pages_of(data)):
            if old[i * PAGE:i * PAGE + len(page)] != page:
                changed += len(page)
    return total, changed


def checksum_version(pg_bin, datadir):
    """'Data page checksum version' from pg_controldata: 0 off, 1 on."""
    out = subprocess.run([str(Path(pg_bin) / 'pg_controldata'), '-D', str(datadir)], capture_output=True, text=True,
                         check=True, env={'LC_ALL': 'C', 'PATH': '/usr/bin:/bin'}).stdout
    return int(re.search(r'^Data page checksum version:\s*(\d+)', out, re.M).group(1))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--out', required=True)
    ap.add_argument('--scale', type=int, default=20)
    ap.add_argument('--pg-bin', default='/opt/homebrew/opt/postgresql@17/bin')
    ap.add_argument('--work')
    ap.add_argument('--data-checksums', choices=('off', 'on'), default='off')
    a = ap.parse_args(argv)
    out, work = Path(a.out), Path(a.work or tempfile.mkdtemp(prefix='exp38.'))
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    pg = PG(a.pg_bin, tempfile.mkdtemp(prefix='pgs.'))
    template = work / 'template'
    if not template.exists():
        run([str(pg.bin / 'initdb'), '-D', str(template), '-U', 'postgres', '-A', 'trust', '--no-sync',
             *(['--data-checksums'] if a.data_checksums == 'on' else [])])
        pg.start(template)
        pg.sql('CREATE DATABASE bench', db='postgres')
        run([str(pg.bin / 'pgbench'), '-i', '-q', '-s', str(a.scale), '-h', pg.sock, '-p', str(PORT), '-U', 'postgres',
             'bench'])
        pg.sql('CHECKPOINT')
        pg.stop(template)
    checksums = checksum_version(pg.bin, template)
    (work / 'hot.sql').write_text(HOT_SCRIPT)
    with open(out / 'results.jsonl', 'w') as log:
        for workload in ('tpcb', 'hot'):
            b0, s = work / f'{workload}-b0', work / f'{workload}-s'
            for d in (b0, s):
                shutil.rmtree(d, ignore_errors=True)
                run(['cp', '-c', '-R', str(template), str(d)])
            done = 0
            for target in STEPS:
                pg.start(s)
                script = ['-b', 'tpcb-like'] if workload == 'tpcb' else ['-f', str(work / 'hot.sql')]
                run([str(pg.bin / 'pgbench'), '-n', '-c', '8', '-j', '4', '-t', str((target - done) // 8), *script,
                     '-h', pg.sock, '-p', str(PORT), '-U', 'postgres', 'bench'])
                done = target
                pg.sql('CHECKPOINT')
                pg.stop(s)
                total, changed = changed_bytes(s, b0)
                rec = {'workload': workload, 'scale': a.scale, 'transactions': done, 's_relation_bytes': total,
                       'changed_bytes': changed, 'changed_fraction': round(changed / total, 4),
                       'data_checksum_version': checksums}
                log.write(json.dumps(rec) + '\n')
                log.flush()
                print(json.dumps(rec), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
