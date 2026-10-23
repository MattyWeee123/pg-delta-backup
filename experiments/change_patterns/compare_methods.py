"""EXP-02: run each refresh method on one (B0, B1) pair and measure it.

Every method turns the old copy B0 into an exact copy of the new backup B1:
- pgdelta: python -m pgdelta local (sender and receiver over a pipe), with
  the engine EXP-02 measured pinned by its flags (PGDELTA_ENGINE: one job,
  in-process; gap-fill; sequential verification), so neither pgdelta's
  default --jobs nor its default rebuild changes what this measures. The
  bytes sent are the same for every engine setting; only times differ.
  Pass pgdelta_flags to measure another engine; the result records them.
- rsync with its default block size and with 8 KiB blocks, forced into delta
  mode (--no-whole-file) and content comparison (--checksum), updating a
  copy-on-write clone of B0 in place.
Full copy and native incremental need no run here: their bytes are B1's and
I1's sizes, which the change-pattern trial already records.

For each method we record bytes in each direction as the method reports them,
wall time, and CPU seconds of the method's processes (from RUSAGE_CHILDREN),
and check that the result is byte-identical to B1. With pgdelta_flags that
ask for worker processes (--jobs N > 1), their CPU is included only because
pgdelta starts them with "spawn", as children it reaps; the result records
the start method each side reports (worker_start_method), and
cpu_counts_workers is false if either side used anything else (with
PGDELTA_START_METHOD=forkserver the workers' CPU would be missing).
"""
from __future__ import annotations

import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.methods import rsync_transfer_stats  # noqa: E402

CHUNK = 1 << 20
# EXP-02's pgdelta engine (EXP-06's "baseline"): single-threaded in the pgdelta process, each gap
# copied from B on its own, each file verified right after it is written. Explicit, so that a
# change of pgdelta's defaults cannot silently change EXP-02's measurement.
PGDELTA_ENGINE = ('--jobs', '1', '--rebuild', 'gap-fill', '--verify', 'sequential')


def files(root):
    root = Path(root)
    return {p.relative_to(root).as_posix(): p for p in root.rglob('*')
            if p.is_file() and not p.is_symlink()}


def identical_trees(expected, actual):
    """True when both trees hold the same regular files with the same bytes."""
    left, right = files(expected), files(actual)
    if left.keys() != right.keys():
        return False, sorted(left.keys() ^ right.keys())[:10]
    for name, path in left.items():
        other = right[name]
        if path.stat().st_size != other.stat().st_size:
            return False, [name]
        with path.open('rb') as a, other.open('rb') as b:
            while True:
                x, y = a.read(CHUNK), b.read(CHUNK)
                if x != y:
                    return False, [name]
                if not x:
                    break
    return True, []


def tree_bytes(root):
    return sum(p.stat().st_size for p in files(root).values())


def clone_tree(source, target):
    """Copy-on-write clone where the filesystem supports it, else a plain copy."""
    if platform.system() == 'Darwin':
        command = ['cp', '-cR', str(source), str(target)]
    else:
        command = ['cp', '-a', '--reflink=auto', str(source), str(target)]
    subprocess.run(command, check=True)


def measured(command, log, **kwargs):
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.perf_counter()
    with open(log, 'w') as stream:
        proc = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, **kwargs)
    wall = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    return proc.returncode, round(wall, 3), round(cpu, 3)


def run_pgdelta(b0, b1, work, *, python=sys.executable, pg_controldata=None, extra=(),
                pgdelta_flags=PGDELTA_ENGINE):
    output, stats_path = Path(work) / 'pgdelta-out', Path(work) / 'pgdelta-stats.json'
    command = [str(python), '-m', 'pgdelta', 'local', '--source', str(b1), '--basis', str(b0),
               '--output', str(output), '--stats-json', str(stats_path), *pgdelta_flags, *extra]
    if pg_controldata:
        command += ['--pg-controldata', str(pg_controldata)]
    code, wall, cpu = measured(command, Path(work) / 'pgdelta.log', cwd=REPO_ROOT,
                               env={**os.environ, 'PYTHONPATH': str(REPO_ROOT)})
    result = {'method': 'pgdelta', 'exit_code': code, 'wall_seconds': wall, 'cpu_seconds': cpu,
              'flags': [*pgdelta_flags, *extra]}
    if stats_path.is_file():
        stats = json.loads(stats_path.read_text())
        result.update(bytes_source_to_dest=stats['bytes_source_to_dest']['total'],
                      bytes_dest_to_source=stats['bytes_dest_to_source']['total'],
                      bytes_by_category={'source_to_dest': stats['bytes_source_to_dest'],
                                         'dest_to_source': stats['bytes_dest_to_source']},
                      mode=stats.get('mode'), pages=stats.get('pages'),
                      timings=stats.get('timings_seconds'), switches=stats.get('switches'))
        methods = {'sender': stats.get('worker_start_method'),
                   'receiver': (stats.get('receiver') or {}).get('worker_start_method')}
        result.update(worker_start_method=methods,
                      cpu_counts_workers=all(method in (None, 'spawn') for method in methods.values()))
    if code == 0:
        result['identical'], result['differences'] = identical_trees(b1, output)
    shutil.rmtree(output, ignore_errors=True)
    return result


def run_rsync(b0, b1, work, *, rsync='rsync', block_size=None):
    name = 'rsync_default' if block_size is None else f'rsync_{block_size // 1024}k'
    target = Path(work) / f'{name}-out'
    clone_tree(b0, target)
    command = [str(rsync), '-a', '--no-whole-file', '--checksum', '--inplace', '--delete',
               '--stats', f'{b1}/', f'{target}/']
    if block_size:
        command.insert(1, f'--block-size={block_size}')
    log = Path(work) / f'{name}.log'
    code, wall, cpu = measured(command, log)
    result = {'method': name, 'exit_code': code, 'wall_seconds': wall, 'cpu_seconds': cpu,
              'block_size': block_size}
    if code == 0:
        stats = rsync_transfer_stats(log.read_text())
        result.update(bytes_source_to_dest=stats['bytes_source_to_dest'],
                      bytes_dest_to_source=stats['bytes_dest_to_source'],
                      literal_bytes=stats['literal_bytes'], matched_bytes=stats['matched_bytes'])
        result['identical'], result['differences'] = identical_trees(b1, target)
    shutil.rmtree(target, ignore_errors=True)
    return result


def compare(b0, b1, i1, work, *, python=sys.executable, rsync='rsync', pg_controldata=None,
            rsync_block_sizes=(None, 8192), pgdelta_flags=PGDELTA_ENGINE):
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    results = {'full': {'method': 'full', 'bytes_source_to_dest': tree_bytes(b1),
                        'bytes_dest_to_source': 0},
               'incremental': {'method': 'incremental', 'bytes_source_to_dest': tree_bytes(i1),
                               'bytes_dest_to_source': None,
                               'note': 'I1 size; restoring it also needs pg_combinebackup '
                                       'to rewrite a full copy at the destination'}}
    results['pgdelta'] = run_pgdelta(b0, b1, work, python=python, pg_controldata=pg_controldata,
                                     pgdelta_flags=pgdelta_flags)
    for block_size in rsync_block_sizes:
        result = run_rsync(b0, b1, work, rsync=rsync, block_size=block_size)
        results[result['method']] = result
    version = subprocess.run([str(rsync), '--version'], capture_output=True, text=True)
    results['environment'] = {'rsync': version.stdout.splitlines()[0] if version.stdout else None,
                              'python': sys.version.split()[0]}
    return results
