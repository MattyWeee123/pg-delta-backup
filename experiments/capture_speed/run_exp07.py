"""EXP-07: where the time of a local pg_basebackup goes (run on pgd-source).

    python3 experiments/capture_speed/run_exp07.py --root /data/exp07 --out /data/exp07-results [--repeats 3]

Builds a scale-70 cluster with EXP-01's code (through EXP-04's source_cluster.py, so it is the
same cluster EXP-04 loaded, before its 1% workload), then times each variant of docs/experiments/EXP-07-capture-speed.md
with the OS page cache emptied before every run, in one seeded shuffled order. For each run it
records wall time, the client's CPU (rusage), and the server's backup CPU: the growth of the
postmaster's reaped-children CPU (cutime + cstime), which is where a walsender's CPU lands when it
exits (with any other child that exits in the window; the checkpointer's work is not counted). Writes results.json and results.csv under --out, then stops and removes the cluster.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import random
import resource
import shutil
import subprocess
import sys
import time

REPO_ROOT = Path(__file__).resolve().parents[2]
BIN = '/usr/lib/postgresql/17/bin'
USER, PORT = 'study', 5432
CLK = os.sysconf('SC_CLK_TCK')
COMMON = ['--wal-method', 'stream', '--checkpoint', 'fast', '--no-password']
VARIANTS = {
    'C1': ('pg_basebackup to disk, SHA-256 manifest (as EXP-04)', ['--format', 'plain', '--manifest-checksums', 'SHA256']),
    'C2': ('C1 with --no-verify-checksums', ['--format', 'plain', '--manifest-checksums', 'SHA256', '--no-verify-checksums']),
    'C3': ('tar to /dev/null, no WAL, SHA-256 manifest', None),
    'C4': ('4 parallel cat of the data directory', None),
    'C6': ('C1 with CRC32C manifest (PostgreSQL default)', ['--format', 'plain', '--manifest-checksums', 'CRC32C']),
    'C7': ('C6 with --no-verify-checksums', ['--format', 'plain', '--manifest-checksums', 'CRC32C', '--no-verify-checksums']),
}


def drop_caches():
    subprocess.run(['sudo', '-n', 'sh', '-c', 'sync; echo 3 > /proc/sys/vm/drop_caches'], check=True)


def children_cpu(pid):
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return (int(fields[13]) + int(fields[14])) / CLK  # cutime, cstime (fields 16, 17 of stat)


def run(argv, **kwargs):
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.perf_counter()
    result = subprocess.run(argv, **kwargs)
    wall = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    return result, wall, cpu


def data_files(data):
    """Files a base backup copies from the data directory: everything except pg_wal, whose old
    segments PostgreSQL recycles while it runs (a backup sends only the WAL it needs)."""
    files = []
    for path in Path(data).rglob('*'):
        if 'pg_wal' in path.relative_to(data).parts:
            continue
        if path.is_file() and not path.is_symlink():
            files.append((path.stat().st_size, str(path)))
    return sorted(files, reverse=True)


def split(files, parts):
    """Lists of about equal bytes: largest files first, each to the lightest list."""
    lists, loads = [[] for _ in range(parts)], [0] * parts
    for size, path in files:
        k = loads.index(min(loads))
        lists[k].append(path)
        loads[k] += size
    return lists


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--seed', type=int, default=20261022)
    parser.add_argument('--scale', type=int, default=70)
    args = parser.parse_args()
    root, out = Path(args.root), Path(args.out)
    if root.exists():
        shutil.rmtree(root)
    out.mkdir(parents=True, exist_ok=True)
    (root / 'logs').mkdir(parents=True)
    cluster = [sys.executable, str(REPO_ROOT / 'experiments/network/source_cluster.py')]
    trial = ['--root', str(root), '--log-dir', str(root / 'logs'), '--scale', str(args.scale),
             '--pattern', 'uniform', '--fraction', '0.01']
    started = time.time()
    subprocess.run(cluster + ['setup', *trial, '--listen-ip', '127.0.0.1', '--client-ip', '127.0.0.1'],
                   check=True, stdout=subprocess.DEVNULL)
    print(f'cluster built in {time.time() - started:.0f} s', flush=True)
    pidfile = next(root.rglob('postmaster.pid'))
    data = pidfile.parent
    postmaster = int(pidfile.read_text().split()[0])
    host = str(root / 's')
    files = data_files(data)
    data_bytes = sum(size for size, _ in files)
    order = [v for _ in range(args.repeats) for v in VARIANTS]
    random.Random(args.seed).shuffle(order)
    rows, counts = [], {v: 0 for v in VARIANTS}
    for index, variant in enumerate(order):
        target = root / 'S'
        if target.exists():
            shutil.rmtree(target)
        drop_caches()
        server_before = children_cpu(postmaster)
        tool = f'{BIN}/pg_basebackup'
        conn = ['--host', host, '--port', str(PORT), '--username', USER]
        if variant == 'C3':
            argv = [tool, '--pgdata', '-', '--format', 'tar', '--wal-method', 'none', '--checkpoint', 'fast',
                    '--manifest-checksums', 'SHA256', '--no-password', *conn]
            with open(os.devnull, 'wb') as sink:
                result, wall, cpu = run(argv, stdout=sink, stderr=subprocess.PIPE)
            out_bytes = None
        elif variant == 'C4':
            files = data_files(data)  # listed afresh, before the timer
            lists = split(files, 4)
            procs, started = [], time.perf_counter()
            before = resource.getrusage(resource.RUSAGE_CHILDREN)
            for part in lists:
                with open(os.devnull, 'wb') as sink:
                    procs.append(subprocess.Popen(['xargs', '-0', 'cat'], stdin=subprocess.PIPE, stdout=sink))
                procs[-1].stdin.write(b'\0'.join(p.encode() for p in part))
                procs[-1].stdin.close()
            codes = [p.wait() for p in procs]
            wall = time.perf_counter() - started
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
            result = subprocess.CompletedProcess(['cat'], max(codes), b'', b'')
            out_bytes = sum(size for size, _ in files)
        else:
            argv = [tool, '--pgdata', str(target), *VARIANTS[variant][1], *COMMON, *conn]
            result, wall, cpu = run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            out_bytes = sum(p.stat().st_size for p in target.rglob('*') if p.is_file()) if target.exists() else None
        server_cpu = children_cpu(postmaster) - server_before
        counts[variant] += 1
        row = dict(order=index, variant=variant, repeat=counts[variant], what=VARIANTS[variant][0],
                   exit_code=result.returncode, wall_s=round(wall, 3), client_cpu_s=round(cpu, 3),
                   server_cpu_s=round(server_cpu, 3) if variant != 'C4' else None,
                   bytes=out_bytes, data_dir_bytes=data_bytes,
                   mb_per_s=round(out_bytes / wall / 1e6, 1) if out_bytes else None,
                   error=(result.stderr or b'').decode(errors='replace')[-300:] if result.returncode else '')
        rows.append(row)
        print(json.dumps(row), flush=True)
    subprocess.run(cluster + ['stop', '--root', str(root), '--remove'], check=False, stdout=subprocess.DEVNULL)
    lscpu = subprocess.run(['lscpu'], capture_output=True, text=True).stdout
    record = dict(schema='exp07/1', seed=args.seed, repeats=args.repeats, scale=args.scale,
                  data_dir_bytes=data_bytes, files=len(files), cpu=lscpu, rows=rows,
                  pg_basebackup=subprocess.run([f'{BIN}/pg_basebackup', '--version'], capture_output=True,
                                               text=True).stdout.strip())
    (out / 'results.json').write_text(json.dumps(record, indent=1))
    with (out / 'results.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print('done', out)


if __name__ == '__main__':
    main()
