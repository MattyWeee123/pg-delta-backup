"""EXP-80: is a crashed standby refreshed correctly by the fast path, its end proven by its minimum recovery point
(--crashed-standby), and is crash recovery first, as pg_rewind does for its target, unsound for the fast path?
(docs/experiments/EXP-80-crashed-standby.md)

    .venv/bin/python experiments/fastpath_crashed/crashed_standby_trials.py [--trials 100] [--controls 20]
        [--recovery-first 40] [--seed 800] [--scale 4]

One local source S (PostgreSQL 17, data checksums, summarize_wal on, checkpoints every 30 s and on 64 MB of WAL) runs
for the whole campaign. Each trial, in a shuffled order of arms:
- crashed-standby (deciding): a new standby of S (pg_basebackup -R), started; a randomized load on S (pgbench with
  side-table DDL: CREATE TABLE AS, TRUNCATE, VACUUM FULL, DROP); the standby killed at a random moment, by
  `pg_ctl -m immediate` or by SIGKILL of its postmaster (whose stale postmaster.pid is then removed, as an operator
  would after checking no process is left); more load on S; then a fast-path refresh of the standby in place with
  --crashed-standby;
- crashed-primary (control): S killed by `pg_ctl -m immediate`, its data directory copied, S restarted and loaded
  again; the copy refreshed with --crashed-standby, which must refuse the fast path (state "in production") and hash;
- recovery-first (control): a crashed standby as above, then crash recovery first (standby.signal moved aside,
  `postgres --single`, the signal put back), which leaves it "shut down"; then the fast path as for a clean stop.
Each refreshed copy is started without standby.signal and compared with S: every table's rows by md5, and
pg_amcheck --heapallindexed. Writes docs/experiments/data/exp80-trials.csv.
"""
import argparse
import csv
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PG_BIN = Path(os.environ.get('PG_BIN', '/opt/homebrew/opt/postgresql@17/bin'))
ENV = dict(os.environ, LC_ALL='C', LANG='C')
PORT_S, PORT_B, PORT_COPY = 5461, 5462, 5463
STATE_SQL = ("SELECT json_build_object("
             "'accounts', (SELECT md5(string_agg(aid || ':' || abalance, ',' ORDER BY aid)) FROM pgbench_accounts), "
             "'tellers', (SELECT md5(string_agg(tid || ':' || tbalance, ',' ORDER BY tid)) FROM pgbench_tellers), "
             "'branches', (SELECT md5(string_agg(bid || ':' || bbalance, ',' ORDER BY bid)) FROM pgbench_branches), "
             "'history', (SELECT count(*) || ':' || coalesce(sum(delta), 0) FROM pgbench_history), "
             "'side', (SELECT coalesce(string_agg(relname || ':' || n_rows, ',' ORDER BY relname), '') FROM "
             "(SELECT c.relname, (xpath('/row/c/text()', query_to_xml('SELECT count(*) AS c FROM ' || "
             "quote_ident(c.relname), false, true, '')))[1]::text AS n_rows FROM pg_class c WHERE c.relname LIKE "
             "'side_%' AND c.relkind = 'r') s))")


def run(*args, check=True, timeout=600, **kwargs):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, env=ENV, timeout=timeout,
                            **kwargs)
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} failed ({result.returncode}): {result.stderr[-1500:]}')
    return result


class Lab:
    extra = ()  # flags added to every refresh (EXP-86 sets the receiver's --fsync per trial)

    def __init__(self, root, scale, rng):
        self.root, self.rng = root, rng
        self.sockets = Path(tempfile.mkdtemp(prefix='pg80'))
        self.S = root / 'S'
        run(PG_BIN / 'initdb', '-D', self.S, '-U', 't', '-A', 'trust', '--no-sync', '--data-checksums')
        with open(self.S / 'postgresql.conf', 'a') as conf:
            conf.write('summarize_wal = on\ncheckpoint_timeout = 30s\nmax_wal_size = 64MB\n'
                       'wal_keep_size = 1024MB\n')
        self.ctl(self.S, PORT_S, 'start')
        run(PG_BIN / 'pgbench', '-i', '-q', '-s', scale, '-h', self.sockets, '-p', PORT_S, '-U', 't', 'postgres')
        self.side = 0

    def ctl(self, data, port, action, mode='fast', check=True):
        return run(PG_BIN / 'pg_ctl', '-D', data, '-l', f'{data}.log', '-w', '-t', '120', '-m', mode, '-o',
                   f"-p {port} -k {self.sockets} -c listen_addresses=''", action, check=check)

    def sql(self, statement, port=PORT_S):
        return run(PG_BIN / 'psql', '-h', self.sockets, '-p', port, '-U', 't', '-d', 'postgres', '-qAtX', '-v',
                   'ON_ERROR_STOP=1', '-c', statement).stdout.strip()

    def load(self, seconds):
        """pgbench on S for `seconds`, with side-table DDL in between: a relation created, truncated, rewritten and
        dropped, so the summaries carry creations, truncations (limit blocks) and new relfilenodes."""
        bench = subprocess.Popen([str(PG_BIN / 'pgbench'), '-h', str(self.sockets), '-p', str(PORT_S), '-U', 't',
                                  '-c', '4', '-T', str(seconds), 'postgres'], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, env=ENV)
        deadline = time.time() + seconds
        while time.time() < deadline - 0.5:
            self.side += 1
            name = f'side_{self.side % 4}'
            choice = self.rng.random()
            create = f'CREATE TABLE IF NOT EXISTS {name} (g int, v text)'
            if choice < 0.4:
                statements = [f'DROP TABLE IF EXISTS {name}', f'CREATE TABLE {name} AS SELECT g, md5(g::text) AS v '
                              f'FROM generate_series(1, {self.rng.randint(1000, 30000)}) g']
            elif choice < 0.6:
                statements = [create, f'TRUNCATE {name}', f'INSERT INTO {name} SELECT g, md5(g::text) FROM '
                              f'generate_series(1, {self.rng.randint(10, 3000)}) g']
            elif choice < 0.8:
                statements = [create, f'DELETE FROM {name} WHERE g % 3 = 0', f'VACUUM {name}']
            else:
                statements = [create, f'VACUUM FULL {name}']
            for statement in statements:  # one each: VACUUM can't run inside a transaction block
                self.sql(statement)
            time.sleep(self.rng.uniform(0.1, 0.6))
        bench.wait()

    def standby(self, path):
        run(PG_BIN / 'pg_basebackup', '-h', self.sockets, '-p', PORT_S, '-U', 't', '-D', path, '-R', '-X', 'stream',
            '-c', 'fast')
        self.ctl(path, PORT_B, 'start')

    def kill(self, path, method):
        """Stop the server on `path` without a clean shutdown."""
        if method == 'immediate':
            self.ctl(path, PORT_B, 'stop', mode='immediate')
            return
        pid = int((path / 'postmaster.pid').read_text().split()[0])
        os.kill(pid, signal.SIGKILL)
        for _ in range(100):  # its children notice the postmaster's death and exit
            if not run('pgrep', '-f', f'-D {path}', check=False).stdout.strip() and \
                    run('pgrep', '-P', pid, check=False).returncode:
                break
            time.sleep(0.1)
        (path / 'postmaster.pid').unlink()  # stale: no process is left on it

    def refresh(self, basis, *flags):
        stats_path = basis.parent / f'{basis.name}.stats.json'
        started = time.perf_counter()
        result = run(sys.executable, '-m', 'pgdelta', 'local', '--source', self.S, '--source-kind', 'live',
                     '--pg-controldata', PG_BIN / 'pg_controldata', '--pg-conn',
                     f'host={self.sockets} port={PORT_S} user=t dbname=postgres', '--basis', basis, '--in-place',
                     '--fast-path', 'summaries', '--stats-json', stats_path, *flags, *self.extra, check=False,
                     cwd=ROOT, timeout=900)
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

    def recovery_first(self, path):
        """pg_rewind's step for a target not shut down cleanly: crash recovery in single-user mode (standby.signal
        moved aside, since single-user servers refuse standby mode), then the signal put back."""
        signal_file = path / 'standby.signal'
        aside = path / 'standby.signal.aside'
        signal_file.rename(aside)
        try:
            result = run(PG_BIN / 'postgres', '--single', '-F', '-D', path, 'template1', check=False,
                         stdin=subprocess.DEVNULL, timeout=600)
        finally:
            aside.rename(signal_file)
        return result.returncode

    def close(self):
        self.ctl(self.S, PORT_S, 'stop', mode='immediate', check=False)
        shutil.rmtree(self.sockets, True)


def state_of(path):
    text = run(PG_BIN / 'pg_controldata', path).stdout
    return next((line.split(':', 1)[1].strip() for line in text.splitlines()
                 if line.startswith('Database cluster state')), None)


def trial(lab, root, number, arm, rng):
    path = root / f'B{number}'
    kill = rng.choice(['immediate', 'sigkill'])
    before, after = rng.uniform(1.0, 8.0), rng.uniform(1.0, 6.0)
    row = {'trial': number, 'arm': arm, 'kill': kill if arm != 'crashed-primary' else 'immediate',
           'load_before_s': round(before, 2), 'load_after_s': round(after, 2)}
    if arm == 'crashed-primary':
        lab.load(before)
        lab.ctl(lab.S, PORT_S, 'stop', mode='immediate')
        shutil.copytree(lab.S, path, symlinks=True)
        lab.ctl(lab.S, PORT_S, 'start')
        (path / 'postmaster.pid').unlink(missing_ok=True)
    else:
        lab.standby(path)
        # The load runs on S while the standby replays, and the kill lands mid-load.
        lab_thread = threading.Thread(target=lab.load, args=(before + 2.0,))
        lab_thread.start()
        time.sleep(before)
        lab.kill(path, kill)
        lab_thread.join()
    row['state_after_kill'] = state_of(path)
    lab.load(after)
    if arm == 'recovery-first':
        row['recovery_exit'] = lab.recovery_first(path)
        row['state_after_recovery'] = state_of(path)
    # Explicit either way: the other arms ran without it, its default before K15 (October 30) made it on.
    flags = ['--crashed-standby'] if arm in ('crashed-standby', 'crashed-primary') else ['--no-crashed-standby']
    result, stats, seconds = lab.refresh(path, *flags)
    received = (stats.get('receiver') or {}).get('fast_path') or {}
    fast = stats.get('fast_path') or {}
    row.update({'exit': result.returncode, 'seconds': round(seconds, 3), 'fast_used': fast.get('used'),
                'proof': (received.get('facts') or {}).get('proof'), 'refused': (received.get('refused') or '')[:120],
                'pages_sent': (stats.get('pages') or {}).get('sent'), 'pages_total': (stats.get('pages') or {}).get('total'),
                'fsync_mode': (stats.get('receiver') or {}).get('fsync_mode')})
    if result.returncode == 0:
        equal, amcheck, source, copied = lab.verify(path)
        row.update({'rows_equal': equal, 'amcheck_ok': amcheck})
        if not equal:
            row['diff'] = json.dumps({k: (json.loads(source)[k], json.loads(copied)[k]) for k in json.loads(source)
                                      if json.loads(source)[k] != json.loads(copied)[k]})[:400]
    else:
        row['stderr'] = result.stderr.strip()[-300:]
    shutil.rmtree(path, ignore_errors=True)
    for leftover in root.glob(f'B{number}.*'):
        leftover.unlink(missing_ok=True)
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--trials', type=int, default=100)
    ap.add_argument('--controls', type=int, default=20)
    ap.add_argument('--recovery-first', type=int, default=40)
    ap.add_argument('--seed', type=int, default=800)
    ap.add_argument('--scale', type=int, default=4)
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp80-trials.csv'))
    a = ap.parse_args()
    rng = random.Random(a.seed)
    arms = (['crashed-standby'] * a.trials + ['crashed-primary'] * a.controls
            + ['recovery-first'] * a.recovery_first)
    rng.shuffle(arms)
    print(run(PG_BIN / 'postgres', '--version').stdout.strip(), f'{len(arms)} trials', flush=True)
    rows = []
    with tempfile.TemporaryDirectory(prefix='exp80-') as temp:
        root = Path(temp)
        lab = Lab(root, a.scale, rng)
        try:
            for number, arm in enumerate(arms):
                row = trial(lab, root, number, arm, rng)
                rows.append(row)
                print(f"{number:3d} {arm:15s} {row.get('state_after_kill')!s:22s} used {row.get('fast_used')!s:5s} "
                      f"proof {row.get('proof')!s:24s} exit {row['exit']} rows {row.get('rows_equal')} "
                      f"amcheck {row.get('amcheck_ok')} {row.get('diff', '')}", flush=True)
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
