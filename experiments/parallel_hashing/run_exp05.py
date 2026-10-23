"""EXP-05: time `pgdelta local` with 1, 2, 4 and 8 hashing threads on kept B0/B1 pairs.

Run from the repository root, first building the pairs (EXP-02's inputs, same seeds), then this:

    python experiments/change_patterns/run_study.py --matrix --scale 70 \\
        --patterns uniform,hot,append --hot-percent 1 --fractions 0.01 \\
        --keep --compare-methods --out results/exp05/pairs
    python experiments/parallel_hashing/run_exp05.py --pairs results/exp05/pairs

For every passed trial under --pairs (its `kept_work_dir` holds B0 and B1) and every
--jobs value, it runs

    python -m pgdelta local --source B1 --basis B0 --output O --jobs N \\
        --hash-backend thread --rebuild gap-fill --verify sequential \\
        --stats-json <out>/stats/<run>.json --pg-controldata <bin-dir>/pg_controldata

(the last three flags pin the engine EXP-05 measured: since EXP-06, --jobs N means
worker processes, O is built by copy-then-patch and verified as files finish),
--repeats times, all runs in one shuffled order (--seed). `--pg-controldata` is passed
as in EXP-02, so the identity gate allows reuse. After each run it checks that O is
byte-identical to B1, then deletes O and its receipt. Before the first run it waits
until no postgres, run_study or pgdelta process of anyone else is running, and reads
every B0 and B1 file once so that no run pays for a cold page cache.

Recorded per run: wall time measured here and pgdelta's own timings; CPU seconds of
pgdelta and everything it waited for (RUSAGE_CHILDREN deltas: the sender, the child
receiver, pg_controldata); bytes each way; pages sent; the 1-minute load average.
EXP-05's engine hashes on threads inside those two processes, so their CPU is all
counted. (For worker processes, see run_exp06.py: RUSAGE_CHILDREN counts them only
because pgdelta starts them with "spawn", as its own children.)
Writes <out>/results.csv (one row per run) and <out>/results.json (environment, pairs,
runs, the per pattern x jobs summary with the modelled crossover link speed, and ps
snapshots), rewritten after every run, plus <out>/stats/ and <out>/logs/.

The modelled crossover is the link speed below which pgdelta beats a full copy:
(full_bytes - (bytes_s2d + bytes_d2s)) * 8 / wall, in Gbit/s, with full_bytes the
size of B1 (methods.full in the trial JSON) and the median wall of the cell.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import random
import re
import resource
import shutil
import statistics
import subprocess
import sys
import time

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.change_patterns.compare_methods import identical_trees  # noqa: E402

DEFAULT_BIN_DIR = '/opt/homebrew/opt/postgresql@17/bin'
DEFAULT_JOBS = (1, 2, 4, 8)
# The engine EXP-05 measured (threads, gap-fill, sequential verification), not today's defaults.
EXP05_ENGINE = ('--hash-backend', 'thread', '--rebuild', 'gap-fill', '--verify', 'sequential')
RECEIPT_SUFFIX = '.pgdelta-receipt.json'
BUSY = re.compile(r'(^|/)(postgres|pg_basebackup|pg_ctl|initdb|pgbench|psql)(\s|$)|run_study|pgdelta')
IDLE_POLL_SECONDS = 15
CSV_FIELDS = ('order', 'run_id', 'pattern', 'trial_id', 'jobs', 'repeat', 'exit_code', 'status', 'mode',
              'sender_jobs', 'receiver_jobs', 'wall_external_s', 'wall_stats_total_s', 'hash_source_s',
              'hash_basis_s', 'protocol_s', 'rebuild_s', 'verify_s', 'cpu_s', 'cpu_user_s', 'cpu_sys_s',
              'bytes_s2d', 'bytes_d2s', 'bytes_d2s_without_final_complete', 'page_data_bytes', 'pages_sent',
              'pages_total', 'nodes_descended', 'identical', 'load1_before', 'started_utc')


def load_pairs(pairs_dir):
    """Every passed trial with a kept work directory: pattern, B0, B1, full-copy bytes, rsync results."""
    pairs = []
    for path in sorted(Path(pairs_dir, 'trials').glob('*.json')):
        trial = json.loads(path.read_text())
        work = trial.get('kept_work_dir')
        if trial.get('status') != 'pass' or not work:
            continue
        work = Path(work)
        methods = trial.get('methods') or {}
        full = (methods.get('full') or {}).get('bytes_source_to_dest') or trial['backups']['B1']['bytes']
        pairs.append({
            'trial_id': trial['trial_id'], 'pattern': trial['params']['pattern'],
            'fraction': trial['params']['fraction'], 'seed': trial['params']['seed'],
            'work_dir': str(work), 'b0': str(work / 'B0'), 'b1': str(work / 'B1'), 'full_bytes': full,
            'build_methods': {name: {key: entry.get(key) for key in
                                     ('wall_seconds', 'cpu_seconds', 'bytes_source_to_dest',
                                      'bytes_dest_to_source', 'identical')}
                              for name, entry in methods.items()
                              if name in ('pgdelta', 'rsync_default', 'rsync_8k')},
        })
    return pairs


def plan_runs(pair_count, jobs_values, repeats, seed):
    """(pair index, jobs, repeat) for every cell and repeat, in one seeded random order."""
    runs = [(pair, jobs, repeat) for pair in range(pair_count) for jobs in jobs_values
            for repeat in range(repeats)]
    random.Random(seed).shuffle(runs)
    return runs


def crossover_gbps(full_bytes, s2d, d2s, wall):
    """Link speed (Gbit/s) below which this delta run beats sending full_bytes."""
    return (full_bytes - (s2d + d2s)) * 8 / wall / 1e9


def _spread(values):
    return {'median': statistics.median(values), 'min': min(values), 'max': max(values)}


def summarize(rows, pairs):
    """One entry per (pattern, jobs) cell of the runs that completed."""
    full = {pair['trial_id']: pair['full_bytes'] for pair in pairs}
    cells = {}
    for row in rows:
        if row['status'] == 'complete':
            cells.setdefault((row['pattern'], row['trial_id'], row['jobs']), []).append(row)
    summary = []
    for (pattern, trial_id, jobs), group in sorted(cells.items()):
        wall = _spread([row['wall_external_s'] for row in group])
        entry = {
            'pattern': pattern, 'trial_id': trial_id, 'jobs': jobs, 'runs': len(group),
            'wall_external_s': wall,
            'wall_stats_total_s': _spread([row['wall_stats_total_s'] for row in group]),
            'cpu_s': _spread([row['cpu_s'] for row in group]),
            'hash_source_s': statistics.median(row['hash_source_s'] for row in group),
            'hash_basis_s': statistics.median(row['hash_basis_s'] for row in group),
            'verify_s': statistics.median(row['verify_s'] for row in group),
            'bytes_s2d': sorted({row['bytes_s2d'] for row in group}),
            'bytes_d2s': sorted({row['bytes_d2s'] for row in group}),
            'bytes_d2s_without_final_complete': sorted({row['bytes_d2s_without_final_complete']
                                                        for row in group}),
            'pages_sent': sorted({row['pages_sent'] for row in group}),
            'all_identical': all(row['identical'] for row in group),
            'full_bytes': full[trial_id],
        }
        entry['crossover_gbps'] = crossover_gbps(full[trial_id], statistics.median(entry['bytes_s2d']),
                                                 statistics.median(entry['bytes_d2s']), wall['median'])
        summary.append(entry)
    serial = {(entry['trial_id']): entry['wall_external_s']['median'] for entry in summary if entry['jobs'] == 1}
    for entry in summary:
        base = serial.get(entry['trial_id'])
        entry['speedup_vs_1'] = base / entry['wall_external_s']['median'] if base else None
    return summary


def _ps():
    result = subprocess.run(['ps', '-Ao', 'pid=,ppid=,pcpu=,etime=,command='], capture_output=True,
                            text=True, check=False)
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5:
            rows.append({'pid': int(parts[0]), 'ppid': int(parts[1]), 'pcpu': float(parts[2]),
                         'etime': parts[3], 'command': parts[4]})
    return rows


def _descendants(rows, root):
    children, found, stack = {}, set(), [root]
    for row in rows:
        children.setdefault(row['ppid'], []).append(row['pid'])
    while stack:
        pid = stack.pop()
        for child in children.get(pid, ()):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def _ancestors(rows, pid):
    parents, found = {row['pid']: row['ppid'] for row in rows}, set()
    while pid in parents and pid not in found and pid > 1:
        found.add(pid)
        pid = parents[pid]
    return found


def busy_processes():
    """postgres, run_study or pgdelta processes other than this script, its children and its ancestors."""
    rows = _ps()
    mine = _descendants(rows, os.getpid()) | _ancestors(rows, os.getpid())
    return [row for row in rows if row['pid'] not in mine and BUSY.search(row['command'])]


def heavy_processes(threshold=25.0):
    """Anything using more than `threshold` percent of a CPU, for the record."""
    return [{key: row[key] for key in ('pid', 'pcpu', 'etime')} | {'command': row['command'][:200]}
            for row in _ps() if row['pcpu'] >= threshold and row['pid'] != os.getpid()]


def wait_until_idle(timeout):
    started = time.monotonic()
    while True:
        busy = busy_processes()
        if not busy:
            return
        if time.monotonic() - started > timeout:
            raise SystemExit('still busy after waiting: ' + '; '.join(row['command'][:120] for row in busy))
        print('waiting for: ' + '; '.join(f"{row['pid']} {row['command'][:100]}" for row in busy),
              file=sys.stderr, flush=True)
        time.sleep(IDLE_POLL_SECONDS)


def warm(directories):
    """Read every file once, so the first timed run does not pay for a cold page cache."""
    total = 0
    for directory in directories:
        for path in sorted(Path(directory).rglob('*')):
            if path.is_file() and not path.is_symlink():
                with path.open('rb', buffering=0) as stream:
                    while chunk := stream.read(1 << 20):
                        total += len(chunk)
    return total


def remove_output(output):
    shutil.rmtree(output, ignore_errors=True)
    receipt = output.with_name(output.name + RECEIPT_SUFFIX)
    if receipt.exists():
        receipt.unlink()


def run_once(order, pair, jobs, repeat, out, *, python, pg_controldata):
    run_id = f'{order:02d}-{pair["pattern"]}-j{jobs}-r{repeat}'
    output = Path(pair['work_dir']) / 'exp05-O'
    remove_output(output)
    stats_path, log_path = out / 'stats' / f'{run_id}.json', out / 'logs' / f'{run_id}.log'
    command = [str(python), '-m', 'pgdelta', 'local', '--source', pair['b1'], '--basis', pair['b0'],
               '--output', str(output), '--jobs', str(jobs), *EXP05_ENGINE, '--stats-json', str(stats_path),
               '--pg-controldata', str(pg_controldata)]
    row = {'order': order, 'run_id': run_id, 'pattern': pair['pattern'], 'trial_id': pair['trial_id'],
           'jobs': jobs, 'repeat': repeat, 'load1_before': round(os.getloadavg()[0], 2),
           'started_utc': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.perf_counter()
    with log_path.open('w') as log:
        process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, cwd=REPO_ROOT,
                                 env={**os.environ, 'PYTHONPATH': str(REPO_ROOT)})
    wall = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    user, system = after.ru_utime - before.ru_utime, after.ru_stime - before.ru_stime
    row.update(exit_code=process.returncode, wall_external_s=round(wall, 4), cpu_s=round(user + system, 4),
               cpu_user_s=round(user, 4), cpu_sys_s=round(system, 4))
    stats = json.loads(stats_path.read_text()) if stats_path.is_file() else {}
    timings = stats.get('timings_seconds') or {}
    up, down = stats.get('bytes_source_to_dest') or {}, stats.get('bytes_dest_to_source') or {}
    final = (down.get('by_message_type') or {}).get('COMPLETE', {}).get('bytes', 0)
    row.update(status=stats.get('status'), mode=stats.get('mode'), sender_jobs=stats.get('jobs'),
               receiver_jobs=(stats.get('receiver') or {}).get('jobs'),
               wall_stats_total_s=timings.get('total'), hash_source_s=timings.get('hash_source'),
               hash_basis_s=timings.get('hash_basis'), protocol_s=timings.get('protocol'),
               rebuild_s=timings.get('rebuild'), verify_s=timings.get('verify'),
               bytes_s2d=up.get('total'), bytes_d2s=down.get('total'),
               bytes_d2s_without_final_complete=None if down.get('total') is None else down['total'] - final,
               page_data_bytes=up.get('page_data'), pages_sent=(stats.get('pages') or {}).get('sent'),
               pages_total=(stats.get('pages') or {}).get('total'),
               nodes_descended=(stats.get('nodes') or {}).get('descended'))
    row['identical'] = identical_trees(pair['b1'], output)[0] if process.returncode == 0 else False
    remove_output(output)
    return row


def command_output(argv, failed=''):
    """A command's stripped stdout, or `failed` if it exits non-zero or is not installed.

    A VM may have no git or sysctl, or git but no repository (a deployed tree).
    """
    try:
        result = subprocess.run(argv, capture_output=True, text=True, cwd=REPO_ROOT, check=False)
    except OSError:
        return failed
    return result.stdout.strip() if result.returncode == 0 else failed


def git_status_lines():
    """`git status --porcelain` lines of the checkout, or None where that is unknown (no git, no repository)."""
    if command_output(['git', 'rev-parse', 'HEAD'], failed=None) is None:
        return None
    status = command_output(['git', 'status', '--porcelain'], failed=None)
    return None if status is None else [line for line in status.splitlines() if line.strip()]


def environment(bin_dir):
    def sysctl(name):
        return command_output(['sysctl', '-n', name]) or None
    git = command_output(['git', 'rev-parse', 'HEAD']) or None
    status = git_status_lines()
    return {'python': sys.version.split()[0], 'executable': sys.executable, 'platform': platform.platform(),
            'cpu_count': os.cpu_count(), 'perflevel0_cpus': sysctl('hw.perflevel0.physicalcpu'),
            'perflevel1_cpus': sysctl('hw.perflevel1.physicalcpu'), 'memory_bytes': sysctl('hw.memsize'),
            'cpu_brand': sysctl('machdep.cpu.brand_string'), 'bin_dir': str(bin_dir), 'git_commit': git,
            'git_dirty': None if status is None else bool(status)}  # None: unknown, not clean


def write_results(out, record):
    with (out / 'results.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, CSV_FIELDS)
        writer.writeheader()
        for row in record['runs']:
            writer.writerow({field: row.get(field) for field in CSV_FIELDS})
    temporary = out / 'results.json.tmp'
    temporary.write_text(json.dumps(record, indent=2) + '\n')
    os.replace(temporary, out / 'results.json')


def markdown(summary):
    lines = ['| pattern | jobs | median wall s (min-max) | speedup | median CPU s | S->D bytes | D->S bytes '
             '| pages sent | crossover Gbit/s | identical |',
             '|---|---|---|---|---|---|---|---|---|---|']
    for entry in summary:
        wall, cpu = entry['wall_external_s'], entry['cpu_s']
        lines.append(f"| {entry['pattern']} | {entry['jobs']} | {wall['median']:.3f} ({wall['min']:.3f}-"
                     f"{wall['max']:.3f}) | {entry['speedup_vs_1']:.2f}x | {cpu['median']:.2f} | "
                     f"{'/'.join(map(str, entry['bytes_s2d']))} | {'/'.join(map(str, entry['bytes_d2s']))} | "
                     f"{'/'.join(map(str, entry['pages_sent']))} | {entry['crossover_gbps']:.2f} | "
                     f"{entry['all_identical']} |")
    return '\n'.join(lines)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--pairs', type=Path, required=True, help='run_study.py --keep output directory')
    parser.add_argument('--out', type=Path, default=REPO_ROOT / 'results' / 'exp05')
    parser.add_argument('--jobs', default=','.join(map(str, DEFAULT_JOBS)),
                        type=lambda text: [int(item) for item in text.split(',') if item.strip()])
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--seed', type=int, default=20261022, help='seed of the run order')
    parser.add_argument('--bin-dir', type=Path, default=Path(DEFAULT_BIN_DIR))
    parser.add_argument('--python', default=sys.executable, help='interpreter that runs pgdelta')
    parser.add_argument('--idle-timeout', type=float, default=3600,
                        help='seconds to wait for other postgres/run_study/pgdelta processes to finish')
    parser.add_argument('--no-warm', action='store_true', help='skip reading B0 and B1 before the runs')
    args = parser.parse_args(argv)
    if not args.jobs or any(jobs < 1 for jobs in args.jobs) or args.repeats < 1:
        parser.error('--jobs needs positive integers and --repeats at least 1')
    return args


def main(argv=None):
    args = parse_args(argv)
    pairs = load_pairs(args.pairs)
    if not pairs:
        raise SystemExit(f'no passed trial with a kept_work_dir under {args.pairs}')
    for pair in pairs:
        for side in ('b0', 'b1'):
            if not Path(pair[side]).is_dir():
                raise SystemExit(f'{pair["trial_id"]}: {pair[side]} is missing')
    out = args.out
    (out / 'stats').mkdir(parents=True, exist_ok=True)
    (out / 'logs').mkdir(parents=True, exist_ok=True)
    pg_controldata = args.bin_dir / 'pg_controldata'
    plan = plan_runs(len(pairs), args.jobs, args.repeats, args.seed)
    record = {'schema': 'exp05-parallel-hashing/1', 'argv': sys.argv[1:] if argv is None else list(argv),
              'started_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'environment': environment(args.bin_dir), 'pairs': pairs,
              'order': [f'{pairs[pair]["pattern"]}-j{jobs}-r{repeat}' for pair, jobs, repeat in plan],
              'runs': [], 'summary': [], 'heavy_processes': {}}
    wait_until_idle(args.idle_timeout)
    record['heavy_processes']['before'] = heavy_processes()
    if not args.no_warm:
        started = time.perf_counter()
        record['warm_bytes'] = warm([path for pair in pairs for path in (pair['b0'], pair['b1'])])
        record['warm_seconds'] = round(time.perf_counter() - started, 3)
    write_results(out, record)
    for order, (pair_index, jobs, repeat) in enumerate(plan):
        pair = pairs[pair_index]
        row = run_once(order, pair, jobs, repeat, out, python=args.python, pg_controldata=pg_controldata)
        record['runs'].append(row)
        record['summary'] = summarize(record['runs'], pairs)
        write_results(out, record)
        print(f"{row['run_id']}: exit {row['exit_code']}, {row['wall_external_s']:.3f} s wall, "
              f"{row['cpu_s']:.2f} s CPU, identical={row['identical']}, load {row['load1_before']}",
              file=sys.stderr, flush=True)
    record['heavy_processes']['after'] = heavy_processes()
    record['finished_utc'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    write_results(out, record)
    print(markdown(record['summary']))
    failed = [row['run_id'] for row in record['runs']
              if row['exit_code'] != 0 or not row['identical'] or row['mode'] != 'reuse']
    if failed:
        print('failed or not identical: ' + ', '.join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
