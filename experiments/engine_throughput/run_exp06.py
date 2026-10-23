"""EXP-06: time `pgdelta local` under the pre-registered engine configurations on kept B0/B1 pairs.

Run from the repository root, first building the pairs (EXP-02's inputs, same seeds), then this:

    python experiments/change_patterns/run_study.py --matrix --scale 70 \\
        --patterns uniform,hot,append --hot-percent 1 --fractions 0.01 \\
        --keep --compare-methods --out results/exp06/pairs
    nice -n 0 python experiments/engine_throughput/run_exp06.py --pairs results/exp06/pairs

Configurations (docs/experiments/EXP-06-engine-throughput.md), N = worker processes per side:

    baseline   --jobs 1 --rebuild gap-fill --verify sequential   (EXP-05's one thread)
    A-N        --jobs N --rebuild gap-fill --verify sequential   N = 2, 4, 8
    B          --jobs 1 --rebuild copy-patch                     (verification is sequential at --jobs 1)
    ABC-N      --jobs N                                          N = 1, 2, 4, 8 (process, copy-patch, pipelined)

B and ABC-1 are the same engine (--jobs 1 has no workers, so no pipelined verification): the
nine pre-registered configurations are eight distinct engines. Not in the default set, added
with --configs (after the EXP-06 review, to separate C from B):

    AB-N       --jobs N --rebuild copy-patch --verify sequential N = 2, 4, 8 (A + B; C's share is ABC-N - AB-N)
    AC-N       --jobs N --rebuild gap-fill --verify pipelined    N = 2, 4, 8 (A + C)
    exp05-N    EXP-05's thread engine                            N = 2, 4, 8

`--configs review` runs the nine plus AB-2, AB-4 and AB-8. For every passed trial under --pairs (its `kept_work_dir` holds B0 and B1), every configuration
and every repeat, it runs

    python -m pgdelta local --source B1 --basis B0 --output O <flags> \\
        --stats-json <out>/stats/<run>.json --pg-controldata <bin-dir>/pg_controldata

with all runs in one seeded random order (--seed; give each pass its own seed, so that two
passes do not run in the same order). `--pg-controldata` is passed as in EXP-02 and EXP-05,
so the identity gate allows reuse. PGDELTA_START_METHOD is removed from the environment, so
pgdelta's default start method ("spawn") is measured. After each run it checks
that O is byte-identical to B1 and that the switches pgdelta reports as in effect are the
configuration's, then deletes O and its receipt.

Before the first run it waits until no postgres, run_study or pgdelta process of anyone else
is running (other heavy processes are only recorded, not waited for), and reads every B0 and
B1 file once (as EXP-05 did). Where the page cache cannot hold every pair at once, --warm-each
also reads the run's own B0 and B1, untimed, just before each run. Either way the share of B0
and B1 in the page cache (mmap + mincore, which reads nothing) is recorded before that read,
at the start of the run and after it, and a cell is "cache-controlled" only if every one of
its runs started with at least WARM_SHARE of its inputs cached. Cells that are not must not be
compared with each other: their times depend on how much each run had to read from disk.

Then, unless --no-isolation, it measures S2: one 1 GiB file (the first file of B1 of exactly
--isolation-bytes, else a generated file of that size, or --isolation-file) hashed with
pgdelta.tree.hash_file in this process, on each pool in a seeded order: serial (in-process),
1/2/4/8 worker processes and 2/4/8 threads. Each pool is started and given one untimed pass
first (so start-up is excluded and reported separately); then --isolation-repeats timed
passes, best kept, digests checked against serial. "1 process" is HashPool's process backend
with a single worker (HashPool(1) itself is the in-process path, by design of --jobs 1).
CPU per pass is this process's and its reaped workers' CPU over the pool's whole life (start-up,
untimed pass and timed passes) divided by the passes; where /proc exists (Linux), the CPU of the
timed passes alone (this process plus each live worker's /proc/<pid>/stat) is recorded too.
`--isolation-only` without --pairs hashes a generated file (or --isolation-file) and nothing
else, which is how EXP-06's Linux check (L1-L3) runs on a VM; the table also goes to
<out>/isolation.csv.

Recorded per run: wall time measured here and pgdelta's own per-phase timings; CPU user and
system seconds of pgdelta and everything it waited for (RUSAGE_CHILDREN deltas: the sender,
the child receiver, pg_controldata and both sides' worker processes). The workers are counted
only because pgdelta starts them with "spawn": each is a child of its pgdelta process, which
reaps it when the pool closes, so its rusage is passed up. With "forkserver" they would be
children of the fork server, which no one waits for, and RUSAGE_CHILDREN (like wait4 and
time(1)) would miss nearly all hashing CPU at N > 1; each run records the start method both
sides report, and cpu_counts_workers is false unless both used spawn. Also recorded: bytes
each way (and back without the final COMPLETE message, whose statistics vary in length); pages
sent; how O was built (basis_copy_bytes, basis_copies); worker processes left behind;
page-cache residency of B0 and B1; the load averages and memory just before the run, and the
processes using at least --heavy-threshold percent of a CPU just before and just after it (ps
is not sampled during a run).

Writes <out>/results.csv (one row per run) and <out>/results.json (environment, pairs,
isolation, runs, the per pattern x configuration summary with the modelled crossover link
speed, the cross-configuration consistency check, and ps snapshots), rewritten after every
run, plus <out>/stats/ and <out>/logs/.

The modelled crossover (EXP-02) is the link speed below which pgdelta beats a full copy:
(full_bytes - (bytes_s2d + bytes_d2s)) * 8 / wall, in Gbit/s, with full_bytes the size of
B1 (methods.full in the trial JSON) and the median external wall time of the cell.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import resource
import statistics
import subprocess
import sys
import time

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.change_patterns.compare_methods import identical_trees  # noqa: E402
from experiments.parallel_hashing.run_exp05 import (  # noqa: E402
    DEFAULT_BIN_DIR, _ps, command_output, crossover_gbps, environment as base_environment, git_status_lines,
    load_pairs, remove_output, wait_until_idle, warm)
from pgdelta import tree  # noqa: E402
from pgdelta.tree import HashPool, hash_file  # noqa: E402

_GAP_SEQ = ('--rebuild', 'gap-fill', '--verify', 'sequential')
# A run is warm if at least this share of its B0 and B1 was in the page cache when it started.
WARM_SHARE = 0.95


def _config(jobs, flags, rebuild, verify, backend='process'):
    effective = 'serial' if jobs == 1 else backend
    return {'jobs': jobs, 'flags': tuple(flags),
            'expected_switches': {'sender': {'jobs': jobs, 'hash_backend': effective},
                                  'receiver': {'jobs': jobs, 'hash_backend': effective, 'rebuild': rebuild,
                                               'verify': verify}}}


CONFIGS = {
    'baseline': _config(1, ('--jobs', '1', *_GAP_SEQ), 'gap-fill', 'sequential'),
    **{f'A-{n}': _config(n, ('--jobs', str(n), *_GAP_SEQ), 'gap-fill', 'sequential') for n in (2, 4, 8)},
    'B': _config(1, ('--jobs', '1', '--rebuild', 'copy-patch'), 'copy-patch', 'sequential'),
    **{f'ABC-{n}': _config(n, ('--jobs', str(n)), 'copy-patch', 'pipelined' if n > 1 else 'sequential')
       for n in (1, 2, 4, 8)},
    **{f'AB-{n}': _config(n, ('--jobs', str(n), '--rebuild', 'copy-patch', '--verify', 'sequential'), 'copy-patch',
                          'sequential') for n in (2, 4, 8)},
    **{f'AC-{n}': _config(n, ('--jobs', str(n), '--rebuild', 'gap-fill', '--verify', 'pipelined'), 'gap-fill',
                          'pipelined') for n in (2, 4, 8)},
    **{f'exp05-{n}': _config(n, ('--jobs', str(n), '--hash-backend', 'thread', *_GAP_SEQ), 'gap-fill',
                             'sequential', backend='thread') for n in (2, 4, 8)},
}
DEFAULT_CONFIGS = ('baseline', 'A-2', 'A-4', 'A-8', 'B', 'ABC-1', 'ABC-2', 'ABC-4', 'ABC-8')
# The EXP-06 review's check: every default configuration plus A + B, so that C's share is measured.
REVIEW_CONFIGS = DEFAULT_CONFIGS + ('AB-2', 'AB-4', 'AB-8')
ISOLATION_VARIANTS = (('serial', 1), ('process', 1), ('process', 2), ('process', 4), ('process', 8),
                      ('thread', 2), ('thread', 4), ('thread', 8))
ISOLATION_KEY = bytes(range(32))
# EXP-02 (pgdelta_s2d in exp01-trials.csv) and EXP-05 (exp05-runs.csv) for these inputs at the
# default seed: what every configuration must reproduce exactly.
REFERENCE_BYTES = {
    'uniform': {'bytes_s2d': 608895495, 'pages_sent': 74261, 'page_data_bytes': 608831662,
                'bytes_d2s_without_final_complete': 2225452},
    'hot': {'bytes_s2d': 28347258, 'pages_sent': 3454, 'page_data_bytes': 28287565,
            'bytes_d2s_without_final_complete': 97556},
    'append': {'bytes_s2d': 26700566, 'pages_sent': 3253, 'page_data_bytes': 26640905,
               'bytes_d2s_without_final_complete': 80356},
}
PHASES = ('hash_source', 'hash_basis', 'protocol', 'rebuild', 'copy_basis', 'fsync', 'verify', 'verify_wait',
          'pool_start', 'pool_first_ready', 'pool_ready', 'receiver_pool_start', 'receiver_pool_first_ready',
          'receiver_pool_ready', 'total')
INVARIANT_FIELDS = ('bytes_s2d', 'bytes_d2s_without_final_complete', 'page_data_bytes', 'pages_sent',
                    'pages_total', 'nodes_descended')
CSV_FIELDS = ('order', 'run_id', 'pattern', 'trial_id', 'config', 'jobs', 'repeat', 'exit_code', 'status', 'mode',
              'switches_ok', 'sender_jobs', 'sender_backend', 'receiver_jobs', 'receiver_backend', 'rebuild',
              'verify', 'sender_start_method', 'receiver_start_method', 'cpu_counts_workers',
              'wall_external_s', 'cpu_s', 'cpu_user_s', 'cpu_sys_s',
              *(f'{phase}_s' for phase in PHASES),
              'bytes_s2d', 'bytes_d2s', 'bytes_d2s_without_final_complete', 'page_data_bytes', 'pages_sent',
              'pages_total', 'nodes_descended', 'basis_copy_bytes', 'basis_copies', 'identical',
              'leaked_processes', 'resident_before', 'warm_each_s', 'resident_at_start', 'resident_after',
              'load1_before', 'load5_before', 'free_mb_before', 'file_backed_mb_before', 'heavy_before',
              'heavy_after', 'started_utc')


# --- the plan and the summary ---------------------------------------------------------------------

def plan_runs(pair_count, configs, repeats, seed):
    """(pair index, configuration, repeat) for every cell and repeat, in one seeded random order."""
    runs = [(pair, config, repeat) for pair in range(pair_count) for config in configs
            for repeat in range(repeats)]
    random.Random(seed).shuffle(runs)
    return runs


def _spread(values):
    return {'median': statistics.median(values), 'min': min(values), 'max': max(values)}


def _spread_known(values):
    known = [value for value in values if value is not None]
    return _spread(known) if known else None


def _median(group, field):
    values = [row[field] for row in group if row.get(field) is not None]
    return statistics.median(values) if values else None


def cold_runs(group, warm_share=WARM_SHARE):
    """How many runs of `group` started with less than `warm_share` of B0 and B1 cached, or unknown."""
    return sum(1 for row in group if row.get('resident_at_start') is None or row['resident_at_start'] < warm_share)


def summarize(rows, pairs, configs=None):
    """One entry per (pattern, configuration) cell of the runs that completed, in configuration order."""
    full = {pair['trial_id']: pair['full_bytes'] for pair in pairs}
    order = {name: index for index, name in enumerate(configs or CONFIGS)}
    cells = {}
    for row in rows:
        if row['status'] == 'complete':
            cells.setdefault((row['pattern'], row['trial_id'], row['config']), []).append(row)
    summary = []
    for (pattern, trial_id, config), group in sorted(cells.items(),
                                                     key=lambda item: (item[0][0], order.get(item[0][2], 99))):
        wall = _spread([row['wall_external_s'] for row in group])
        entry = {
            'pattern': pattern, 'trial_id': trial_id, 'config': config, 'jobs': group[0]['jobs'],
            'runs': len(group), 'wall_external_s': wall, 'cpu_s': _spread([row['cpu_s'] for row in group]),
            'cpu_user_s': _median(group, 'cpu_user_s'), 'cpu_sys_s': _median(group, 'cpu_sys_s'),
            'phases_median_s': {phase: _median(group, f'{phase}_s') for phase in PHASES},
            **{field: sorted({row[field] for row in group})
               for field in ('bytes_s2d', 'bytes_d2s', *INVARIANT_FIELDS[1:], 'basis_copy_bytes',
                             'basis_copies')},
            'all_identical': all(row['identical'] for row in group),
            'switches_ok': all(row['switches_ok'] for row in group),
            'leaked_processes': sum(row['leaked_processes'] for row in group),
            'load1_before': _spread([row['load1_before'] for row in group]),
            'resident_at_start': _spread_known([row.get('resident_at_start') for row in group]),
            'resident_after': _spread_known([row.get('resident_after') for row in group]),
            # Only cells whose every run started warm can be compared: a cold run reads B0/B1 from disk.
            'cold_runs': cold_runs(group),
            'cache_controlled': cold_runs(group) == 0,
            'cpu_counts_workers': all(row.get('cpu_counts_workers', True) for row in group),
            'full_bytes': full[trial_id],
        }
        entry['crossover_gbps'] = crossover_gbps(full[trial_id], statistics.median(entry['bytes_s2d']),
                                                 statistics.median(entry['bytes_d2s']), wall['median'])
        summary.append(entry)
    baseline = {entry['trial_id']: entry for entry in summary if entry['config'] == 'baseline'}
    for entry in summary:
        base = baseline.get(entry['trial_id'])
        entry['speedup_vs_baseline'] = (base['wall_external_s']['median'] / entry['wall_external_s']['median']
                                        if base else None)
        entry['cpu_vs_baseline'] = entry['cpu_s']['median'] / base['cpu_s']['median'] if base else None
    return summary


def consistency(rows):
    """Per pattern: the distinct values of every invariant across all configurations, and a verdict.

    Every statistic but timings, how O was built and the final COMPLETE message must be the
    same for every switch combination and N (S1), and equal to EXP-02/EXP-05's for these inputs.
    """
    result = {}
    for pattern in sorted({row['pattern'] for row in rows}):
        group = [row for row in rows if row['pattern'] == pattern and row['status'] == 'complete']
        values = {field: sorted({row[field] for row in group}) for field in INVARIANT_FIELDS}
        reference = REFERENCE_BYTES.get(pattern, {})
        result[pattern] = {
            'runs': len(group), 'values': values,
            'one_value_each': all(len(found) == 1 for found in values.values()),
            'all_identical': all(row['identical'] for row in group),
            'matches_exp02_exp05': ({field: values[field] == [expected] for field, expected in reference.items()}
                                    if reference else None),
        }
    return result


# --- machine state --------------------------------------------------------------------------------

def heavy_processes(threshold):
    """Processes (other than this one) using at least `threshold` percent of a CPU, busiest first."""
    rows = [row for row in _ps() if row['pcpu'] >= threshold and row['pid'] != os.getpid()]
    return [{'pid': row['pid'], 'pcpu': row['pcpu'], 'etime': row['etime'], 'command': row['command'][:160]}
            for row in sorted(rows, key=lambda row: -row['pcpu'])]


def _short(command):
    first = command.split(' -', 1)[0].strip()
    return os.path.basename(first)[:40] or command[:40]


def multiprocessing_pids():
    """{pid: command} of every multiprocessing worker, resource tracker or fork server on the machine."""
    return {row['pid']: row['command'][:200] for row in _ps()
            if 'multiprocessing' in row['command'] and row['pid'] != os.getpid()}


class _Residency:
    """Bytes of a file in the page cache, from mmap + mincore (no page is read or faulted in)."""

    def __init__(self):
        import ctypes
        import ctypes.util
        import mmap
        self.ctypes, self.mmap = ctypes, mmap
        libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
        libc.mmap.restype = ctypes.c_void_p
        libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int64]
        libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p]
        self.libc, self.page = libc, mmap.PAGESIZE
        self.failed = ctypes.c_void_p(-1).value

    def file(self, path):
        """(resident bytes, mapped bytes) of the regular file at `path`."""
        size = os.path.getsize(path)
        if not size:
            return 0, 0
        fd = os.open(path, os.O_RDONLY)
        try:
            address = self.libc.mmap(None, size, self.mmap.PROT_READ, self.mmap.MAP_SHARED, fd, 0)
            if address in (None, self.failed):
                raise OSError(self.ctypes.get_errno(), f'mmap {path}')
            try:
                pages = -(-size // self.page)
                vector = self.ctypes.create_string_buffer(pages)
                if self.libc.mincore(address, size, vector) != 0:
                    raise OSError(self.ctypes.get_errno(), f'mincore {path}')
                resident = sum(1 for flags in vector.raw if flags & 1)
            finally:
                self.libc.munmap(address, size)
        finally:
            os.close(fd)
        return min(resident * self.page, size), size


_RESIDENCY = None


def resident_fraction(directories):
    """Share of the bytes of every regular file under `directories` that is in the page cache, or None."""
    global _RESIDENCY
    try:
        if _RESIDENCY is None:
            _RESIDENCY = _Residency()
        resident = total = 0
        for directory in directories:
            for path in Path(directory).rglob('*'):
                if path.is_file() and not path.is_symlink():
                    found, size = _RESIDENCY.file(path)
                    resident, total = resident + found, total + size
        return round(resident / total, 4) if total else None
    except (OSError, AttributeError, ValueError):
        return None


def make_isolation_file(path, size, seed):
    """A file of `size` pseudo-random bytes (BLAKE2b's speed does not depend on the content)."""
    generator = random.Random(seed)
    with open(path, 'wb') as stream:
        remaining = size
        while remaining:
            piece = min(remaining, 1 << 20)
            stream.write(generator.randbytes(piece))
            remaining -= piece
    return Path(path)


def memory_snapshot():
    """Free and file-backed (page cache) memory in MB, from vm_stat (macOS); {} elsewhere."""
    try:
        text = subprocess.run(['vm_stat'], capture_output=True, text=True, check=False).stdout
    except OSError:
        return {}
    page = 4096
    values = {}
    for line in text.splitlines():
        if 'page size of' in line:
            page = int(line.split('page size of')[1].split()[0])
        elif ':' in line:
            name, _, number = line.partition(':')
            number = number.strip().rstrip('.')
            if number.isdigit():
                values[name.strip()] = int(number)
    mb = page / 1e6
    return {'free_mb': round(values.get('Pages free', 0) * mb),
            'file_backed_mb': round(values.get('File-backed pages', 0) * mb)} if values else {}


def dirty_files():
    """`git status --porcelain` lines of the checkout, so a dirty record says what was dirty.

    None where that is unknown: no git or no repository (a deployed tree on a VM; see environment's `deployed`).
    """
    return git_status_lines()


def linux_cpu():
    """The CPU model and topology from /proc/cpuinfo and lscpu on Linux; {} elsewhere."""
    info = {}
    try:
        text = Path('/proc/cpuinfo').read_text()
    except OSError:
        return info
    for line in text.splitlines():
        name, _, value = line.partition(':')
        if name.strip() == 'model name':
            info['cpu_model'] = value.strip()
            break
    for line in command_output(['lscpu']).splitlines():
        name, _, value = line.partition(':')
        if name.strip() in ('Thread(s) per core', 'Core(s) per socket', 'Socket(s)', 'CPU(s)'):
            info[name.strip().lower().replace('(s)', 's').replace(' ', '_')] = value.strip()
    return info


def environment(bin_dir, python):
    env = base_environment(bin_dir)
    env.update(linux_cpu())
    deployed = REPO_ROOT / 'DEPLOYED.json'  # remote.py deploy: no .git on the VMs
    if deployed.is_file():
        env['deployed'] = json.loads(deployed.read_text())
    version = subprocess.run([str(python), '-c', 'import sys; print(sys.version.split()[0])'],
                             capture_output=True, text=True, check=False).stdout.strip()
    # The runs get no PGDELTA_START_METHOD (run_once removes it), so they use pgdelta's default.
    env.update(pgdelta_python=str(python), pgdelta_python_version=version or None,
               niceness=os.getpriority(os.PRIO_PROCESS, 0), start_method=tree.DEFAULT_START_METHOD,
               start_method_env_ignored=os.environ.get(tree.START_METHOD_ENV), git_dirty_files=dirty_files())
    return env


# --- S2: one file in isolation --------------------------------------------------------------------

def _one_worker_pool(key):
    """HashPool's process backend with a single worker process.

    HashPool(1) is the in-process path by design (--jobs 1 never starts a
    process), so the one-worker pool is built from it by starting the worker
    as HashPool.__init__ does for jobs > 1; node_digests and map then run
    exactly as they do for N workers.
    """
    pool = HashPool(1, key=key)
    started = time.perf_counter()
    pool.backend = 'process'
    pool._start_workers(started)
    pool.start_seconds = time.perf_counter() - started
    return pool


def make_pool(backend, jobs, key=ISOLATION_KEY):
    if backend == 'serial':
        return HashPool(1)
    if backend == 'process' and jobs == 1:
        return _one_worker_pool(key)
    return HashPool(jobs, backend=backend, key=key)


def _cpu_seconds():
    own, children = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime


def _live_cpu_seconds(pids):
    """CPU of this process plus that of the live processes `pids`, from /proc; None without /proc.

    Unlike RUSAGE_CHILDREN, this sees workers that have not been reaped yet,
    so the CPU of the timed passes can be told apart from the pool's start-up.
    """
    proc = Path('/proc')
    if not (proc / 'self' / 'stat').exists():
        return None if pids else _own_cpu_seconds()
    ticks = os.sysconf('SC_CLK_TCK')
    total = 0
    for pid in pids:
        text = (proc / str(pid) / 'stat').read_text()
        fields = text[text.rindex(')') + 2:].split()  # fields 3.. of proc(5); utime and stime are 14, 15
        total += int(fields[11]) + int(fields[12])
    return _own_cpu_seconds() + total / ticks


def _own_cpu_seconds():
    own = resource.getrusage(resource.RUSAGE_SELF)
    return own.ru_utime + own.ru_stime


def find_isolation_file(directory, size):
    """The first regular file of exactly `size` bytes under `directory` (sorted by path), or None."""
    files = sorted((path for path in Path(directory).rglob('*') if path.is_file() and not path.is_symlink()),
                   key=lambda path: path.as_posix())
    return next((path for path in files if path.stat().st_size == size), None)


def isolation(path, variants=ISOLATION_VARIANTS, repeats=3, seed=0, key=ISOLATION_KEY):
    """Best-of-`repeats` hash_file wall time of `path` on each pool, after one untimed pass on it."""
    size = os.path.getsize(path)
    with open(path, 'rb', buffering=0) as stream:
        while stream.read(1 << 20):
            pass
    reference = hash_file(path, key).digest
    order = list(variants)
    random.Random(seed).shuffle(order)
    rows = []
    for backend, jobs in order:
        cpu_before, created = _cpu_seconds(), time.perf_counter()
        pool = make_pool(backend, jobs, key)
        try:
            start_seconds = time.perf_counter() - created
            hash_file(path, key, pool=pool)  # untimed: the workers start and run
            deadline = time.monotonic() + 60
            while pool.ready_seconds is None and time.monotonic() < deadline:
                time.sleep(0.01)
            ready_seconds, first_ready_seconds = pool.ready_seconds, pool.first_ready_seconds
            workers = sorted(pool.ready_pids) if pool.backend == 'process' else []
            timed_before = _live_cpu_seconds(workers)
            walls = []
            for _ in range(repeats):
                started = time.perf_counter()
                digest = hash_file(path, key, pool=pool).digest
                walls.append(time.perf_counter() - started)
                if digest != reference:
                    raise SystemExit(f'isolation: {backend} x {jobs} gave a different digest for {path}')
            timed_after = _live_cpu_seconds(workers)
        finally:
            pool.close()
        cpu = _cpu_seconds() - cpu_before  # process workers are counted once the pool has joined them
        timed = None if timed_before is None or timed_after is None else timed_after - timed_before
        best = min(walls)
        rows.append({'backend': backend, 'jobs': jobs, 'walls_s': [round(wall, 4) for wall in walls],
                     'best_s': round(best, 4), 'gb_per_s': round(size / best / 1e9, 3),
                     'pool_start_s': round(start_seconds, 4),
                     'pool_first_ready_s': None if first_ready_seconds is None else round(first_ready_seconds, 4),
                     'pool_ready_s': None if ready_seconds is None else round(ready_seconds, 4),
                     # Over the pool's life: start-up, the untimed pass and the timed ones.
                     'cpu_s_per_pass': round(cpu / (repeats + 1), 3), 'passes': repeats + 1,
                     # The timed passes alone (Linux: workers' /proc/<pid>/stat), start-up left out.
                     'cpu_s_per_timed_pass': None if timed is None else round(timed / repeats, 3)})
    best = {(row['backend'], row['jobs']): row['best_s'] for row in rows}
    # One pass including the pool's start-up (until every worker had started; 0 without workers).
    with_start = {(row['backend'], row['jobs']): row['best_s'] + (row['pool_ready_s'] or 0.0) for row in rows}
    cpu = {(row['backend'], row['jobs']): row for row in rows}
    for row in rows:
        variant = (row['backend'], row['jobs'])
        for field, base in (('cpu_s_per_pass', ('process', 1)), ('cpu_s_per_timed_pass', ('process', 1)),
                            ('cpu_s_per_pass', ('serial', 1)), ('cpu_s_per_timed_pass', ('serial', 1))):
            reference_cpu = (cpu.get(base) or {}).get(field)
            name = field.replace('cpu_s_per_', 'cpu_') + ('_vs_1_process' if base[0] == 'process' else '_vs_serial')
            row[name] = (round(row[field] / reference_cpu, 3)
                         if reference_cpu and row.get(field) is not None else None)
        one = best.get(('process', 1))
        row['speedup_vs_1_process'] = round(one / row['best_s'], 3) if one else None
        row['speedup_vs_serial'] = round(best[('serial', 1)] / row['best_s'], 3) if ('serial', 1) in best else None
        one = with_start.get(('process', 1))
        row['speedup_vs_1_process_with_start'] = round(one / with_start[variant], 3) if one else None
    order_key = {variant: index for index, variant in enumerate(variants)}
    rows.sort(key=lambda row: order_key[(row['backend'], row['jobs'])])
    return {'file': str(path), 'bytes': size, 'repeats': repeats, 'seed': seed, 'order': order,
            'start_method': tree.start_method(), 'rows': rows}


# --- one run --------------------------------------------------------------------------------------

def run_once(order, pair, config, repeat, out, *, python, pg_controldata, heavy_threshold, warm_each=False):
    spec = CONFIGS[config]
    run_id = f'{order:03d}-{pair["pattern"]}-{config}-r{repeat}'
    output = Path(pair['work_dir']) / 'exp06-O'
    remove_output(output)
    stats_path, log_path = out / 'stats' / f'{run_id}.json', out / 'logs' / f'{run_id}.log'
    command = [str(python), '-m', 'pgdelta', 'local', '--source', pair['b1'], '--basis', pair['b0'],
               '--output', str(output), *spec['flags'], '--stats-json', str(stats_path),
               '--pg-controldata', str(pg_controldata)]
    inputs = (pair['b0'], pair['b1'])
    resident_before = resident_fraction(inputs)
    warm_each_s = None
    if warm_each:
        started = time.perf_counter()
        warm(inputs)
        warm_each_s = round(time.perf_counter() - started, 3)
    resident_at_start = resident_fraction(inputs) if warm_each else resident_before
    load = os.getloadavg()
    heavy = heavy_processes(heavy_threshold)
    memory = memory_snapshot()
    row = {'order': order, 'run_id': run_id, 'pattern': pair['pattern'], 'trial_id': pair['trial_id'],
           'config': config, 'jobs': spec['jobs'], 'repeat': repeat, 'command': command,
           'load1_before': round(load[0], 2), 'load5_before': round(load[1], 2),
           'free_mb_before': memory.get('free_mb'), 'file_backed_mb_before': memory.get('file_backed_mb'),
           'heavy_before': ';'.join(f"{_short(item['command'])}:{item['pcpu']:.0f}" for item in heavy),
           'heavy_before_detail': heavy, 'resident_before': resident_before, 'warm_each_s': warm_each_s,
           'resident_at_start': resident_at_start,
           'started_utc': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    env = {key: value for key, value in os.environ.items() if key != tree.START_METHOD_ENV}
    env['PYTHONPATH'] = str(REPO_ROOT)
    workers_before = multiprocessing_pids()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.perf_counter()
    with log_path.open('w') as log:
        process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, cwd=REPO_ROOT, env=env)
    wall = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    leaked = {pid: cmd for pid, cmd in multiprocessing_pids().items() if pid not in workers_before}
    if leaked:  # a resource tracker may take a moment to exit after its parent: look again
        time.sleep(1.0)
        still = multiprocessing_pids()
        leaked = {pid: cmd for pid, cmd in leaked.items() if pid in still}
    user, system = after.ru_utime - before.ru_utime, after.ru_stime - before.ru_stime
    heavy_after = heavy_processes(heavy_threshold)
    row.update(heavy_after=';'.join(f"{_short(item['command'])}:{item['pcpu']:.0f}" for item in heavy_after),
               heavy_after_detail=heavy_after)
    # With resident_at_start, whether the run's inputs stayed in the page cache while it ran.
    row['resident_after'] = resident_fraction(inputs)
    row.update(exit_code=process.returncode, wall_external_s=round(wall, 4), cpu_s=round(user + system, 4),
               cpu_user_s=round(user, 4), cpu_sys_s=round(system, 4), leaked_processes=len(leaked),
               leaked_detail=leaked)
    stats = json.loads(stats_path.read_text()) if stats_path.is_file() else {}
    timings = stats.get('timings_seconds') or {}
    receiver = stats.get('receiver') or {}
    up, down = stats.get('bytes_source_to_dest') or {}, stats.get('bytes_dest_to_source') or {}
    final = (down.get('by_message_type') or {}).get('COMPLETE', {}).get('bytes', 0)
    switches = stats.get('switches') or {}
    sender_switches, receiver_switches = switches.get('sender') or {}, switches.get('receiver') or {}
    methods = (stats.get('worker_start_method'), receiver.get('worker_start_method'))
    row.update(sender_start_method=methods[0], receiver_start_method=methods[1],
               # RUSAGE_CHILDREN counts the workers only if they were pgdelta's own children (spawn).
               cpu_counts_workers=all(method in (None, 'spawn') for method in methods))
    row.update(status=stats.get('status'), mode=stats.get('mode'), switches=switches,
               switches_ok=switches == spec['expected_switches'],
               sender_jobs=sender_switches.get('jobs'), sender_backend=sender_switches.get('hash_backend'),
               receiver_jobs=receiver_switches.get('jobs'), receiver_backend=receiver_switches.get('hash_backend'),
               rebuild=receiver_switches.get('rebuild'), verify=receiver_switches.get('verify'),
               **{f'{phase}_s': timings.get(phase) for phase in PHASES if not phase.startswith('receiver_pool_start')},
               receiver_pool_start_s=receiver.get('pool_start_seconds'),
               bytes_s2d=up.get('total'), bytes_d2s=down.get('total'),
               bytes_d2s_without_final_complete=None if down.get('total') is None else down['total'] - final,
               page_data_bytes=up.get('page_data'), pages_sent=(stats.get('pages') or {}).get('sent'),
               pages_total=(stats.get('pages') or {}).get('total'),
               nodes_descended=(stats.get('nodes') or {}).get('descended'),
               basis_copy_bytes=receiver.get('basis_copy_bytes'), basis_copies=receiver.get('basis_copies'))
    row['identical'] = identical_trees(pair['b1'], output)[0] if process.returncode == 0 else False
    remove_output(output)
    return row


# --- output ---------------------------------------------------------------------------------------

ISOLATION_CSV_FIELDS = ('backend', 'jobs', 'best_s', 'gb_per_s', 'speedup_vs_1_process', 'speedup_vs_serial',
                        'pool_first_ready_s', 'pool_ready_s', 'speedup_vs_1_process_with_start',
                        'cpu_s_per_pass', 'cpu_pass_vs_1_process', 'cpu_pass_vs_serial',
                        'cpu_s_per_timed_pass', 'cpu_timed_pass_vs_1_process', 'cpu_timed_pass_vs_serial',
                        'walls_s')


def write_isolation_csv(path, result):
    """The isolation table, one row per pool, in the variants' order; walls separated by spaces."""
    with Path(path).open('w', newline='') as stream:
        writer = csv.DictWriter(stream, ISOLATION_CSV_FIELDS, extrasaction='ignore')
        writer.writeheader()
        for row in result['rows']:
            writer.writerow({**row, 'walls_s': ' '.join(str(wall) for wall in row['walls_s'])})


def write_results(out, record):
    with (out / 'results.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, CSV_FIELDS)
        writer.writeheader()
        for row in record['runs']:
            writer.writerow({field: row.get(field) for field in CSV_FIELDS})
    temporary = out / 'results.json.tmp'
    temporary.write_text(json.dumps(record, indent=2) + '\n')
    os.replace(temporary, out / 'results.json')


def _fmt(value, digits=2):
    return '-' if value is None else f'{value:.{digits}f}'


def markdown(summary):
    lines = ['| pattern | config | median wall s (min-max) | vs baseline | CPU s median (user/sys) | CPU vs baseline '
             '| hash S | hash B | rebuild | copy B | fsync | verify | verify wait | pool ready S/D '
             '| S->D bytes | D->S bytes | pages sent | basis copies | identical | crossover Gbit/s '
             '| B0+B1 cached at start, min | runs started cold |',
             '|' + '---|' * 22]
    for entry in summary:
        wall, cpu, phase = entry['wall_external_s'], entry['cpu_s'], entry['phases_median_s']
        lines.append(
            f"| {entry['pattern']} | {entry['config']} | {wall['median']:.3f} ({wall['min']:.3f}-{wall['max']:.3f}) "
            f"| {_fmt(entry['speedup_vs_baseline'])}x | {cpu['median']:.2f} ({_fmt(entry['cpu_user_s'])}/"
            f"{_fmt(entry['cpu_sys_s'])}) | {_fmt(entry['cpu_vs_baseline'])}x | {_fmt(phase['hash_source'])} "
            f"| {_fmt(phase['hash_basis'])} | {_fmt(phase['rebuild'])} | {_fmt(phase['copy_basis'])} "
            f"| {_fmt(phase['fsync'])} | {_fmt(phase['verify'])} | {_fmt(phase['verify_wait'])} "
            f"| {_fmt(phase['pool_ready'])}/{_fmt(phase['receiver_pool_ready'])} "
            f"| {'/'.join(map(str, entry['bytes_s2d']))} | {'/'.join(map(str, entry['bytes_d2s']))} "
            f"| {'/'.join(map(str, entry['pages_sent']))} | {'/'.join(map(str, entry['basis_copies']))} "
            f"| {entry['all_identical']} | {entry['crossover_gbps']:.2f} "
            f"| {_fmt((entry['resident_at_start'] or {}).get('min'), 3)} "
            f"| {entry['cold_runs']}{'' if entry['cache_controlled'] else ' (not comparable)'} |")
    return '\n'.join(lines)


def isolation_markdown(result):
    lines = [f"Isolation: {result['file']} ({result['bytes']} bytes), best of {result['repeats']}, "
             f"start method {result['start_method']}",
             '| pool | best s | GB/s | vs 1 process | vs serial | runs s | CPU s per pass | CPU s per timed pass '
             '| pool ready s (first/all) | vs 1 process, start-up included |',
             '|---|---|---|---|---|---|---|---|---|---|']
    for row in result['rows']:
        name = 'serial (in-process)' if row['backend'] == 'serial' else f"{row['jobs']} {row['backend']}"
        lines.append(f"| {name} | {row['best_s']:.3f} | {row['gb_per_s']:.2f} | "
                     f"{_fmt(row['speedup_vs_1_process'])}x | {_fmt(row['speedup_vs_serial'])}x | "
                     f"{'/'.join(f'{wall:.3f}' for wall in row['walls_s'])} | {row['cpu_s_per_pass']:.2f} | "
                     f"{_fmt(row.get('cpu_s_per_timed_pass'))} | "
                     f"{_fmt(row.get('pool_first_ready_s'), 3)}/{_fmt(row['pool_ready_s'], 3)} | "
                     f"{_fmt(row.get('speedup_vs_1_process_with_start'))}x |")
    return '\n'.join(lines)


def _csv_list(kind):
    return lambda text: [kind(item) for item in text.split(',') if item.strip()]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--pairs', type=Path,
                        help='run_study.py --keep output directory (not needed with --isolation-only)')
    parser.add_argument('--out', type=Path, default=REPO_ROOT / 'results' / 'exp06')
    parser.add_argument('--configs', type=_csv_list(str), default=list(DEFAULT_CONFIGS),
                        help=f'comma list of {", ".join(CONFIGS)}, or "review" for {",".join(REVIEW_CONFIGS)} '
                             f'(default: {",".join(DEFAULT_CONFIGS)})')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--seed', type=int, default=20261022, help='seed of the run order')
    parser.add_argument('--bin-dir', type=Path, default=Path(DEFAULT_BIN_DIR))
    parser.add_argument('--python', default=sys.executable, help='interpreter that runs pgdelta')
    parser.add_argument('--idle-timeout', type=float, default=3600,
                        help='seconds to wait for other postgres/run_study/pgdelta processes to finish')
    parser.add_argument('--no-warm', action='store_true', help='skip reading B0 and B1 before the runs')
    parser.add_argument('--warm-each', action='store_true',
                        help="also read the run's own B0 and B1 (untimed) just before every run, for a page "
                             'cache too small to keep every pair; residency is recorded either way')
    parser.add_argument('--no-isolation', action='store_true', help='skip the 1 GiB isolation test (S2)')
    parser.add_argument('--isolation-only', action='store_true', help='run only the isolation test')
    parser.add_argument('--isolation-bytes', type=int, default=1 << 30,
                        help='size of the isolation file: the first file of B1 of exactly this size, else a '
                             'generated one in --out (deleted afterwards)')
    parser.add_argument('--isolation-file', type=Path, help='hash this file in the isolation test instead')
    parser.add_argument('--isolation-repeats', type=int, default=3)
    parser.add_argument('--heavy-threshold', type=float, default=25.0,
                        help='record processes using at least this percent of a CPU before each run')
    args = parser.parse_args(argv)
    if args.configs == ['review']:
        args.configs = list(REVIEW_CONFIGS)
    unknown = [name for name in args.configs if name not in CONFIGS]
    if unknown or not args.configs:
        parser.error(f'unknown or no configurations: {unknown}; choose from {", ".join(CONFIGS)}')
    if args.repeats < 1 or args.isolation_repeats < 1:
        parser.error('--repeats and --isolation-repeats must be at least 1')
    if args.no_isolation and args.isolation_only:
        parser.error('--no-isolation and --isolation-only exclude each other')
    if args.pairs is None and not args.isolation_only:
        parser.error('--pairs is required unless --isolation-only')
    return args


def main(argv=None):
    args = parse_args(argv)
    pairs = load_pairs(args.pairs) if args.pairs is not None else []
    if not pairs and args.pairs is not None:
        raise SystemExit(f'no passed trial with a kept_work_dir under {args.pairs}')
    for pair in pairs:
        for side in ('b0', 'b1'):
            if not Path(pair[side]).is_dir():
                raise SystemExit(f'{pair["trial_id"]}: {pair[side]} is missing')
    out = args.out
    (out / 'stats').mkdir(parents=True, exist_ok=True)
    (out / 'logs').mkdir(parents=True, exist_ok=True)
    pg_controldata = args.bin_dir / 'pg_controldata'
    plan = [] if args.isolation_only else plan_runs(len(pairs), args.configs, args.repeats, args.seed)
    record = {'schema': 'exp06-engine-throughput/1', 'argv': sys.argv[1:] if argv is None else list(argv),
              'started_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'environment': environment(args.bin_dir, args.python), 'pairs': pairs,
              'configs': {name: {'flags': list(CONFIGS[name]['flags']),
                                 'expected_switches': CONFIGS[name]['expected_switches']} for name in args.configs},
              'order': [f'{pairs[pair]["pattern"]}-{config}-r{repeat}' for pair, config, repeat in plan],
              'runs': [], 'summary': [], 'consistency': {}, 'isolation': None,
              'heavy_processes': {}, 'memory': {}}
    wait_until_idle(args.idle_timeout)
    record['heavy_processes']['before'] = heavy_processes(args.heavy_threshold)
    record['memory']['before'] = memory_snapshot()
    if not args.no_warm and pairs:
        started = time.perf_counter()
        record['warm_bytes'] = warm([path for pair in pairs for path in (pair['b0'], pair['b1'])])
        record['warm_seconds'] = round(time.perf_counter() - started, 3)
        record['memory']['after_warm'] = memory_snapshot()
        record['resident_after_warm'] = {pair['pattern']: resident_fraction((pair['b0'], pair['b1']))
                                         for pair in pairs}
    write_results(out, record)
    if not args.no_isolation:
        record['heavy_processes']['before_isolation'] = heavy_processes(args.heavy_threshold)
        load1 = round(os.getloadavg()[0], 2)
        generated = None
        path = args.isolation_file or (find_isolation_file(pairs[0]['b1'], args.isolation_bytes)
                                       if pairs else None)
        if path is None:
            generated = path = make_isolation_file(out / 'isolation.bin', args.isolation_bytes, args.seed)
        try:
            record['isolation'] = isolation(path, repeats=args.isolation_repeats, seed=args.seed)
        finally:
            if generated is not None:
                generated.unlink()
        record['isolation'].update(load1_before=load1, generated=generated is not None)
        write_results(out, record)
        write_isolation_csv(out / 'isolation.csv', record['isolation'])
        print(isolation_markdown(record['isolation']), flush=True)
    for order, (pair_index, config, repeat) in enumerate(plan):
        pair = pairs[pair_index]
        row = run_once(order, pair, config, repeat, out, python=args.python, pg_controldata=pg_controldata,
                       heavy_threshold=args.heavy_threshold, warm_each=args.warm_each)
        record['runs'].append(row)
        record['summary'] = summarize(record['runs'], pairs, args.configs)
        write_results(out, record)
        print(f"{row['run_id']}: exit {row['exit_code']}, {row['wall_external_s']:.3f} s wall, "
              f"{row['cpu_s']:.2f} s CPU, identical={row['identical']}, switches_ok={row['switches_ok']}, "
              f"cached {row['resident_at_start']}->{row['resident_after']}, load {row['load1_before']}, "
              f"heavy {row['heavy_before'] or '-'}", file=sys.stderr, flush=True)
    record['consistency'] = consistency(record['runs'])
    record['heavy_processes']['after'] = heavy_processes(args.heavy_threshold)
    record['memory']['after'] = memory_snapshot()
    record['finished_utc'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    write_results(out, record)
    if record['summary']:
        print(markdown(record['summary']))
        print(json.dumps(record['consistency'], indent=1))
    failed = [row['run_id'] for row in record['runs']
              if row['exit_code'] != 0 or not row['identical'] or row['mode'] != 'reuse' or not row['switches_ok']
              or row['leaked_processes'] or not row['cpu_counts_workers']]
    if failed:
        print('failed, not identical, not in reuse mode, wrong switches, leaked workers or CPU not counting '
              'the workers: ' + ', '.join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
