"""EXP-91's local stage (rule 13): hashing ahead and verifying as written, on the Mac's scale model
(docs/experiments/EXP-91-hash-ahead-verify-as-written.md).

    .venv/bin/python experiments/network/exp91_local.py [--scale 100] [--repeats 2] [--seed 911]
        [--out docs/experiments/data/exp91-local.csv]

EXP-82's local lab (Lab below, moved here from experiments/summaries_off/timing.py when the fast path was archived;
its settings unchanged): a live PostgreSQL 17 source S with data checksums, and in each cell a base backup B0 taken
before the cell's change (hot: 1% of the rows, contiguous; scatter: 0.5% of the rows by a hash of the key, about a
quarter of the heap's pages). Each run copies B0 to B and times the refresh of B in place by hashing:
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
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PG_BIN = Path(os.environ.get('PG_BIN', '/opt/homebrew/opt/postgresql@17/bin'))
ENV = dict(os.environ, LC_ALL='C', LANG='C')
PORT_S, PORT_COPY = 5461, 5463
CELLS = ('hot', 'scatter')
STATE_SQL = ("SELECT json_build_object("
             "'accounts', (SELECT md5(string_agg(aid || ':' || abalance, ',' ORDER BY aid)) FROM pgbench_accounts), "
             "'tellers', (SELECT md5(string_agg(tid || ':' || tbalance, ',' ORDER BY tid)) FROM pgbench_tellers), "
             "'branches', (SELECT md5(string_agg(bid || ':' || bbalance, ',' ORDER BY bid)) FROM pgbench_branches), "
             "'history', (SELECT count(*) || ':' || coalesce(sum(delta), 0) FROM pgbench_history), "
             "'side', (SELECT coalesce(string_agg(relname || ':' || n_rows, ',' ORDER BY relname), '') FROM "
             "(SELECT c.relname, (xpath('/row/c/text()', query_to_xml('SELECT count(*) AS c FROM ' || "
             "quote_ident(c.relname), false, true, '')))[1]::text AS n_rows FROM pg_class c WHERE c.relname LIKE "
             "'side_%' AND c.relkind = 'r') s))")

FLAGS = {'hash': [], 'hashA': ['--hash-ahead', '64G', '--read-ahead', '8'],
         'hashV': ['--verify-scope', 'as-written'],
         'hashAV': ['--hash-ahead', '64G', '--read-ahead', '8', '--verify-scope', 'as-written']}


def run(*args, check=True, timeout=600, **kwargs):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, env=ENV, timeout=timeout,
                            **kwargs)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} failed ({result.returncode}): {result.stderr[-1500:]}')
    return result


class Lab:
    """One local source S (PostgreSQL 17, data checksums, summarize_wal off, checkpoint_timeout 30 min, max_wal_size
    4 GB, wal_keep_size 8 GB: the VMs' settings), pgbench -i at `scale`."""

    def __init__(self, root, scale):
        self.root, self.scale = root, scale
        self.sockets = Path(tempfile.mkdtemp(prefix='pg91'))
        self.S = root / 'S'
        run(PG_BIN / 'initdb', '-D', self.S, '-U', 't', '-A', 'trust', '--no-sync', '--data-checksums')
        with open(self.S / 'postgresql.conf', 'a') as conf:
            conf.write('summarize_wal = off\ncheckpoint_timeout = 30min\nmax_wal_size = 4GB\nwal_keep_size = 8GB\n')
        self.ctl(self.S, PORT_S, 'start')
        run(PG_BIN / 'pgbench', '-i', '-q', '-s', scale, '-h', self.sockets, '-p', PORT_S, '-U', 't', 'postgres',
            timeout=1800)

    def ctl(self, data, port, action, mode='fast', check=True):
        return run(PG_BIN / 'pg_ctl', '-D', data, '-l', f'{data}.log', '-w', '-t', '120', '-m', mode, '-o',
                   f"-p {port} -k {self.sockets} -c listen_addresses=''", action, check=check)

    def sql(self, statement, port=PORT_S):
        return run(PG_BIN / 'psql', '-h', self.sockets, '-p', port, '-U', 't', '-d', 'postgres', '-qAtX', '-v',
                   'ON_ERROR_STOP=1', '-c', statement).stdout.strip()

    def change(self, cell):
        if cell == 'hot':
            for first in range(1, self.scale * 1000 + 1, 1000):
                self.sql(f'UPDATE pgbench_accounts SET abalance = abalance + 1 WHERE aid BETWEEN {first} AND {first + 999}')
        else:
            self.sql('UPDATE pgbench_accounts SET abalance = abalance + 1 WHERE abs(hashint4(aid + 74)) % 10000 < 50')
        self.sql('CHECKPOINT')

    def refresh(self, basis, *flags):
        """B refreshed in place from the live S by pgdelta local: (result, its statistics, seconds)."""
        stats_path = basis.parent / f'{basis.name}.stats.json'
        started = time.perf_counter()
        result = run(sys.executable, '-m', 'pgdelta', 'local', '--source', self.S, '--source-kind', 'live',
                     '--pg-controldata', PG_BIN / 'pg_controldata', '--pg-conn',
                     f'host={self.sockets} port={PORT_S} user=t dbname=postgres', '--basis', basis, '--in-place',
                     '--stats-json', stats_path, *flags, check=False, cwd=ROOT, timeout=900)
        seconds = time.perf_counter() - started
        stats = json.loads(stats_path.read_text()) if stats_path.exists() else {}
        return result, stats, seconds

    def verify(self, copy):
        """(rows equal to S's, pg_amcheck clean, the two states) for the refreshed copy, started without a signal."""
        for name in ('standby.signal', 'recovery.signal'):
            (copy / name).unlink(missing_ok=True)
        self.sql('CHECKPOINT')
        source = self.sql(STATE_SQL)
        self.ctl(copy, PORT_COPY, 'start')
        try:
            copied = self.sql(STATE_SQL, port=PORT_COPY)
            amcheck = run(PG_BIN / 'pg_amcheck', '-h', self.sockets, '-p', PORT_COPY, '-U', 't', '--all',
                          '--heapallindexed', '--install-missing', check=False)
        finally:
            self.ctl(copy, PORT_COPY, 'stop', check=False)
        return copied == source, amcheck.returncode == 0, source, copied

    def close(self):
        self.ctl(self.S, PORT_S, 'stop', mode='immediate', check=False)
        shutil.rmtree(self.sockets, True)


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
                    result, stats, _ = lab.refresh(b, *FLAGS[arm])
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
