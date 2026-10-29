"""EXP-83 and EXP-83b: what summarize_wal costs a busy server (docs/experiments/EXP-83-summarize-wal-cost.md).

    python3 experiments/summarize_cost/bench.py --conn "host=H port=P user=U dbname=postgres" --bin BIN
        [--clients 16] [--threads 4] [--seconds 60] [--pairs 6] [--warmup 30] [--seed 830]
        [--ps "ps -axo time,command"] [--stat "cat /proc/stat"] --out runs.csv

pgbench's TPC-B-like script (prepared statements, no vacuum) runs against a live server in pairs of runs, one with
summarize_wal on and one with it off, the order within each pair random (seed). Before each run:
- the setting is set (ALTER SYSTEM, a reload) and the summarizer waited for: started and caught up to the server's
  current LSN for "on" (so a run measures the summarizer keeping pace, not a backlog), gone for "off";
- a CHECKPOINT, so each run starts from the same place in the checkpoint cycle (and its full-page writes).
Each run records pgbench's throughput, every transaction's latency (pgbench -l: the mean and the 50th, 95th and 99th
percentiles), the WAL it generated, the summarizer's CPU time over the run (--ps: a command listing processes with
their cumulative CPU time; the summarizer's line holds "walsummarizer"), and where --stat gives /proc/stat, the
server's CPU busy fraction. A warm-up run (off) comes first and is not scored. The setting is left off at the end.
"""
import argparse
import csv
import glob
import os
import random
import re
import shlex
import shutil
import statistics
import subprocess
import tempfile
import time

LSN = re.compile(r'^([0-9A-Fa-f]+)/([0-9A-Fa-f]+)$')


def lsn_bytes(text):
    high, low = LSN.match(text.strip()).groups()
    return (int(high, 16) << 32) | int(low, 16)


def cpu_seconds(text):
    """ps's cumulative CPU time: [[dd-]hh:]mm:ss[.ff] (Linux and macOS)."""
    days = 0
    if '-' in text:
        d, text = text.split('-', 1)
        days = int(d)
    parts = [float(p) for p in text.split(':')]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


class Server:
    def __init__(self, conn, bin_dir, ps, stat):
        self.conn, self.bin, self.ps, self.stat = conn, bin_dir, ps, stat

    def sql(self, statement):
        result = subprocess.run([os.path.join(self.bin, 'psql'), self.conn, '-qAtX', '-v', 'ON_ERROR_STOP=1', '-c',
                                 statement], capture_output=True, text=True, timeout=600)
        if result.returncode:
            raise RuntimeError(f'psql failed: {result.stderr.strip()}')
        return result.stdout.strip()

    def summarize(self, on, timeout=1800):
        """summarize_wal on (the summarizer started and caught up to the current LSN) or off (it gone)."""
        self.sql(f"ALTER SYSTEM SET summarize_wal = {'on' if on else 'off'}")
        self.sql('SELECT pg_reload_conf()')
        target = self.sql('SELECT pg_current_wal_lsn()')
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.sql('SELECT summarizer_pid IS NOT NULL, coalesce(pending_lsn >= '
                             f"'{target}', false) FROM pg_get_wal_summarizer_state()").split('|')
            if (on and state == ['t', 't']) or (not on and state[0] == 'f'):
                return
            time.sleep(0.2)
        raise RuntimeError(f"the summarizer did not {'catch up' if on else 'stop'} within {timeout} s")

    def summarizer_cpu(self):
        if not self.ps:
            return None
        out = subprocess.run(self.ps, shell=True, capture_output=True, text=True, timeout=60).stdout
        for line in out.splitlines():
            if 'walsummarizer' in line and 'grep' not in line:
                return cpu_seconds(line.split()[0])
        return 0.0  # no summarizer running

    def cpu_ticks(self):
        """(busy, total) jiffies from /proc/stat's cpu line, or None."""
        if not self.stat:
            return None
        out = subprocess.run(self.stat, shell=True, capture_output=True, text=True, timeout=60).stdout
        fields = [int(x) for x in next(l for l in out.splitlines() if l.startswith('cpu ')).split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return sum(fields) - idle, sum(fields)


def pgbench(server, args, log_dir):
    command = [os.path.join(server.bin, 'pgbench'), '-n', '-M', 'prepared', '-c', str(args.clients), '-j',
               str(args.threads), '-T', str(args.seconds), '-l', '--log-prefix', os.path.join(log_dir, 'pgb')]
    command += shlex.split(args.pgbench_conn)
    result = subprocess.run(command, capture_output=True, text=True, timeout=args.seconds + 600)
    if result.returncode:
        raise RuntimeError(f'pgbench failed: {result.stderr.strip()[-800:]}')
    tps = float(re.search(r'tps = ([0-9.]+) \(without initial connection time\)', result.stdout).group(1))
    latencies = []
    for path in glob.glob(os.path.join(log_dir, 'pgb.*')):
        with open(path) as f:
            latencies += [int(line.split()[2]) for line in f if line.strip()]
    latencies.sort()
    q = lambda p: latencies[min(len(latencies) - 1, int(p * len(latencies)))] / 1000
    return {'tps': round(tps, 1), 'transactions': len(latencies),
            'lat_mean_ms': round(statistics.fmean(latencies) / 1000, 3), 'lat_p50_ms': round(q(0.50), 3),
            'lat_p95_ms': round(q(0.95), 3), 'lat_p99_ms': round(q(0.99), 3)}


def run(server, args, mode, label):
    server.summarize(mode == 'on')
    server.sql('CHECKPOINT')
    log_dir = tempfile.mkdtemp(prefix='exp83-')
    try:
        wal0, cpu0, ticks0 = server.sql('SELECT pg_current_wal_lsn()'), server.summarizer_cpu(), server.cpu_ticks()
        started = time.perf_counter()
        row = pgbench(server, args, log_dir)
        seconds = time.perf_counter() - started
        wal1, cpu1, ticks1 = server.sql('SELECT pg_current_wal_lsn()'), server.summarizer_cpu(), server.cpu_ticks()
    finally:
        shutil.rmtree(log_dir, ignore_errors=True)
    wal = lsn_bytes(wal1) - lsn_bytes(wal0)
    row.update({'mode': mode, 'label': label, 'seconds': round(seconds, 2), 'wal_mb': round(wal / 1e6, 1),
                'summarizer_cpu_s': None if cpu0 is None or cpu1 is None else round(max(0.0, cpu1 - cpu0), 2),
                'server_busy': None if ticks0 is None else round((ticks1[0] - ticks0[0]) / max(1, ticks1[1] - ticks0[1]), 4)})
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--conn', required=True, help='a libpq connection string for psql (a superuser: ALTER SYSTEM)')
    ap.add_argument('--pgbench-conn', default=None, help="pgbench's connection options (default: from --conn)")
    ap.add_argument('--bin', required=True)
    ap.add_argument('--clients', type=int, default=16)
    ap.add_argument('--threads', type=int, default=4)
    ap.add_argument('--seconds', type=int, default=60)
    ap.add_argument('--pairs', type=int, default=6)
    ap.add_argument('--warmup', type=int, default=30)
    ap.add_argument('--seed', type=int, default=830)
    ap.add_argument('--ps', default=None)
    ap.add_argument('--stat', default=None)
    ap.add_argument('--cell', default='cell')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    if a.pgbench_conn is None:
        opts = dict(kv.split('=', 1) for kv in a.conn.split())
        a.pgbench_conn = ' '.join(f'-{flag} {opts[key]}' for flag, key in (('h', 'host'), ('p', 'port'), ('U', 'user'))
                                  if key in opts) + f" {opts.get('dbname', 'postgres')}"
    server = Server(a.conn, a.bin, a.ps, a.stat)
    rng = random.Random(a.seed)
    rows = []
    try:
        if a.warmup:
            warm = argparse.Namespace(**{**vars(a), 'seconds': a.warmup})
            row = run(server, warm, 'off', 'warmup')
            print(f"warm-up: {row['tps']} tps", flush=True)
        for pair in range(a.pairs):
            order = ['on', 'off'] if rng.random() < 0.5 else ['off', 'on']
            for position, mode in enumerate(order):
                row = run(server, a, mode, 'scored')
                row.update({'cell': a.cell, 'pair': pair, 'position': position})
                rows.append(row)
                print(f"pair {pair} {mode:3s}: {row['tps']:9.1f} tps, mean {row['lat_mean_ms']} ms, p99 "
                      f"{row['lat_p99_ms']} ms, WAL {row['wal_mb']} MB, summarizer {row['summarizer_cpu_s']} s, "
                      f"busy {row['server_busy']}", flush=True)
    finally:
        try:
            server.summarize(False, timeout=120)
        except Exception as exc:  # left for the caller to see
            print(f'could not turn summarize_wal off: {exc}', flush=True)
    keys = ['cell', 'pair', 'position', 'mode', 'tps', 'transactions', 'lat_mean_ms', 'lat_p50_ms', 'lat_p95_ms',
            'lat_p99_ms', 'wal_mb', 'summarizer_cpu_s', 'server_busy', 'seconds', 'label']
    write_header = not os.path.exists(a.out)
    with open(a.out, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction='ignore')
        if write_header:
            w.writeheader()
        w.writerows(rows)
    print(a.out)


if __name__ == '__main__':
    main()
