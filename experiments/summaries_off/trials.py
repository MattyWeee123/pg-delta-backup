"""EXP-82's correctness campaign: does a server that never summarized WAL get correct copies from --fast-path page-lsn
and from summaries turned on at refresh time (research loop 3), and does the negative control fail?
(docs/experiments/EXP-82-summaries-off-local.md)

    .venv/bin/python experiments/summaries_off/trials.py [--per-arm 40] [--on-demand 20] [--controls 10]
        [--seed 820] [--scale 4]

One local source S (PostgreSQL 17, data checksums, summarize_wal off, checkpoints every 30 s and on 64 MB of WAL,
wal_keep_size 1 GB so the WAL since each old copy stays on disk) runs for the whole campaign, under EXP-80's load:
pgbench on 4 clients with side-table DDL (CREATE TABLE AS, TRUNCATE and refill, DELETE and VACUUM, VACUUM FULL) and,
now and then, a database created with STRATEGY FILE_COPY or WAL_LOG and dropped. Each trial, in a shuffled order:
- lsn-standby: a standby of S, started, stopped cleanly mid-load (pg_ctl -m fast), more load, then page-lsn;
- lsn-backup: a base backup taken while the load runs (its pages may be torn or stale past its start), more load,
  then page-lsn;
- lsn-crashed: a standby killed mid-load (pg_ctl -m immediate or SIGKILL), more load, then page-lsn with
  --crashed-standby;
- on-demand: a stopped standby or a base backup as above, more load, then summarize_wal turned on (a reload) and
  the summaries' fast path; afterwards summarize_wal is turned off again and its summaries removed, so each on-demand
  trial starts from a server that never summarized;
- lsn-backup-end (negative control): a base backup taken under a heavier load, with no load after, its backup_label
  start moved to the backup's end (the threshold page-lsn would take if it used B's end, not its redo point), then
  page-lsn: pages changed during the backup and stale in B must stay unsent, and the copy is expected wrong.
Each refreshed copy is started without a signal file and compared with S (every pgbench table's rows by md5, the
history's count and sum, each side table's row count) and checked with pg_amcheck --heapallindexed. Writes
docs/experiments/data/exp82-trials.csv.
"""
import argparse
import csv
import json
import random
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'fastpath_crashed'))
import crashed_standby_trials as base  # noqa: E402  EXP-80's lab: S, its load, standbys, kills, the copy's check

from crashed_standby_trials import PG_BIN, PORT_S, run, state_of  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PROOFS = {'lsn-standby': 'clean shutdown in recovery', 'lsn-backup': 'backup_manifest',
          'lsn-crashed': 'crashed standby', 'lsn-backup-end': 'backup_manifest'}


class Lab(base.Lab):
    """EXP-80's lab with summarize_wal off, and databases created and dropped in the load."""

    def __init__(self, root, scale, rng):
        self.root, self.rng = root, rng
        self.sockets = Path(tempfile.mkdtemp(prefix='pg82'))
        self.S = root / 'S'
        run(PG_BIN / 'initdb', '-D', self.S, '-U', 't', '-A', 'trust', '--no-sync', '--data-checksums')
        with open(self.S / 'postgresql.conf', 'a') as conf:
            conf.write('summarize_wal = off\ncheckpoint_timeout = 30s\nmax_wal_size = 64MB\n'
                       'wal_keep_size = 1024MB\n')
        self.ctl(self.S, PORT_S, 'start')
        run(PG_BIN / 'pgbench', '-i', '-q', '-s', scale, '-h', self.sockets, '-p', PORT_S, '-U', 't', 'postgres')
        self.side = self.databases = 0

    def load(self, seconds, clients=4):
        """EXP-80's load, plus now and then a database created (FILE_COPY or WAL_LOG) and dropped."""
        if self.rng.random() < 0.3:
            self.databases += 1
            strategy = self.rng.choice(['FILE_COPY', 'WAL_LOG'])
            self.sql(f'CREATE DATABASE extra_{self.databases} STRATEGY {strategy}')
            if self.databases > 1:
                self.sql(f'DROP DATABASE IF EXISTS extra_{self.databases - 1}')
        if clients == 4:
            return super().load(seconds)
        run(PG_BIN / 'pgbench', '-h', self.sockets, '-p', PORT_S, '-U', 't', '-c', clients, '-T', max(1, round(seconds)),
            'postgres', timeout=600)

    def backup_under_load(self, path, seconds, clients=4):
        """A base backup taken while the load runs on S."""
        thread = threading.Thread(target=self.load, args=(seconds, clients))
        thread.start()
        time.sleep(min(1.0, seconds / 3))
        run(PG_BIN / 'pg_basebackup', '-h', self.sockets, '-p', PORT_S, '-U', 't', '-D', path, '-X', 'stream',
            '-c', 'fast')
        thread.join()

    def refresh(self, basis, mode, *flags):
        stats_path = basis.parent / f'{basis.name}.stats.json'
        started = time.perf_counter()
        fast = ['--fast-path', mode] if mode else []
        result = run(sys.executable, '-m', 'pgdelta', 'local', '--source', self.S, '--source-kind', 'live',
                     '--pg-controldata', PG_BIN / 'pg_controldata', '--pg-conn',
                     f'host={self.sockets} port={PORT_S} user=t dbname=postgres', '--basis', basis, '--in-place',
                     *fast, '--stats-json', stats_path, *flags, check=False, cwd=ROOT, timeout=900)
        seconds = time.perf_counter() - started
        stats = json.loads(stats_path.read_text()) if stats_path.exists() else {}
        return result, stats, seconds

    def summaries(self, on):
        """summarize_wal on (a reload), or off with its summaries removed: a server that never summarized."""
        self.sql(f"ALTER SYSTEM SET summarize_wal = {'on' if on else 'off'}")
        self.sql('SELECT pg_reload_conf()')
        if not on:
            for _ in range(100):
                if self.sql('SELECT summarizer_pid IS NULL FROM pg_get_wal_summarizer_state()') == 't':
                    break
                time.sleep(0.1)
            for summary in (self.S / 'pg_wal' / 'summaries').glob('*.summary'):
                summary.unlink()


def move_start_to_end(path):
    """The negative control: backup_label's start moved to the backup's end, from its manifest."""
    manifest = json.loads((path / 'backup_manifest').read_text())
    end = max(r['End-LSN'] for r in manifest['WAL-Ranges'])
    label = path / 'backup_label'
    text = label.read_text()
    text = re.sub(r'^(START WAL LOCATION: )\S+', lambda m: m.group(1) + end, text, count=1, flags=re.M)
    label.write_text(text)
    return end


def trial(lab, root, number, arm, rng):
    path = root / f'B{number}'
    before, after = rng.uniform(1.0, 8.0), rng.uniform(1.0, 6.0)
    kind = arm if arm != 'on-demand' else rng.choice(['lsn-standby', 'lsn-backup'])
    row = {'trial': number, 'arm': arm, 'basis': kind.replace('lsn-', ''), 'load_before_s': round(before, 2),
           'load_after_s': round(after, 2)}
    if kind in ('lsn-standby', 'lsn-crashed'):
        lab.standby(path)
        thread = threading.Thread(target=lab.load, args=(before + 2.0,))
        thread.start()
        time.sleep(before)
        if kind == 'lsn-standby':
            lab.ctl(path, base.PORT_B, 'stop')
        else:
            row['kill'] = rng.choice(['immediate', 'sigkill'])
            lab.kill(path, row['kill'])
        thread.join()
    elif kind == 'lsn-backup':
        lab.backup_under_load(path, before + 2.0)
    else:  # lsn-backup-end
        lab.backup_under_load(path, before + 4.0, clients=8)
        row['moved_start_to'] = move_start_to_end(path)
        after = 0.0
        row['load_after_s'] = 0.0
    row['state'] = state_of(path)
    if after:
        lab.load(after)
    if arm == 'on-demand':
        lab.summaries(True)
        result, stats, seconds = lab.refresh(path, 'summaries')
        lab.summaries(False)
    else:
        result, stats, seconds = lab.refresh(path, 'page-lsn', *(['--crashed-standby'] if kind == 'lsn-crashed' else []))
    received = (stats.get('receiver') or {}).get('fast_path') or {}
    fast = stats.get('fast_path') or {}
    row.update({'exit': result.returncode, 'seconds': round(seconds, 3), 'fast_used': fast.get('used'),
                'proof': (received.get('facts') or {}).get('proof'), 'reasons': '; '.join(fast.get('reasons') or [])[:160],
                'summaries': fast.get('summaries'), 'threshold': fast.get('lsn_threshold'),
                'pages_sent': (stats.get('pages') or {}).get('sent'), 'pages_total': (stats.get('pages') or {}).get('total')})
    if result.returncode == 0:
        equal, amcheck, source, copied = lab.verify(path)
        row.update({'rows_equal': equal, 'amcheck_ok': amcheck})
        if not equal:
            row['diff'] = json.dumps({k: (json.loads(source)[k], json.loads(copied)[k]) for k in json.loads(source)
                                      if json.loads(source)[k] != json.loads(copied)[k]})[:300]
    else:
        row['stderr'] = result.stderr.strip()[-300:]
    shutil.rmtree(path, ignore_errors=True)
    for leftover in root.glob(f'B{number}.*'):
        leftover.unlink(missing_ok=True)
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--per-arm', type=int, default=40)
    ap.add_argument('--on-demand', type=int, default=20)
    ap.add_argument('--controls', type=int, default=10)
    ap.add_argument('--seed', type=int, default=820)
    ap.add_argument('--scale', type=int, default=4)
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp82-trials.csv'))
    a = ap.parse_args()
    rng = random.Random(a.seed)
    arms = (['lsn-standby', 'lsn-backup', 'lsn-crashed'] * a.per_arm + ['on-demand'] * a.on_demand
            + ['lsn-backup-end'] * a.controls)
    rng.shuffle(arms)
    print(run(PG_BIN / 'postgres', '--version').stdout.strip(), f'{len(arms)} trials', flush=True)
    rows = []
    with tempfile.TemporaryDirectory(prefix='exp82-') as temp:
        root = Path(temp)
        lab = Lab(root, a.scale, rng)
        try:
            for number, arm in enumerate(arms):
                row = trial(lab, root, number, arm, rng)
                rows.append(row)
                print(f"{number:3d} {arm:15s} {row['basis']:9s} {row.get('state')!s:22s} used {row.get('fast_used')!s:5s} "
                      f"proof {row.get('proof')!s:27s} exit {row['exit']} rows {row.get('rows_equal')} "
                      f"amcheck {row.get('amcheck_ok')} {row.get('reasons', '')[:60]} {row.get('diff', '')}", flush=True)
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
