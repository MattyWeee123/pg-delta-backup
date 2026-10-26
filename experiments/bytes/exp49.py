"""EXP-49 (lean, local): what sets zstd's floor (docs/experiments/EXP-49-zstd-floor.md).

    .venv/bin/python experiments/bytes/exp49.py --out results/bytes/exp49 [--scale 10] [--pg-bin DIR] [--work DIR]

The data: EXP-37's uniform scenario (its template and UPDATE), the pages positional matching sends, grouped into
pgdelta's runs (matching.runs, at most RUN_BYTES). Then, with pgdelta's Codec on each binding:
- pool: compress and decompress through a ThreadPoolExecutor of 1, 2, 4 and 8 threads, in pgdelta's pattern (at
  most two runs per thread in flight, results in order on the main thread), on pgdelta's runs and on 1 MiB frames;
- receiver: the main thread pwrite()s each run at its offset while 4 threads decompress, and again with the bytes
  already decompressed; the time inside pwrite is what pgdelta's rebuild timer counts.
Each cell passes over the data until it has processed MIN_BYTES; 3 repeats in a fixed order. One JSON object goes
to OUT/results.json.
"""
from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]
import exp37  # noqa: E402  (EXP-37's template, scenario and file helpers)
from pgdelta import compress, matching  # noqa: E402
from pgdelta.sender import RUN_BYTES  # noqa: E402

PAGE = exp37.PAGE
MIN_BYTES = 300 * 10**6
THREADS = (1, 2, 4, 8)
RECEIVER_THREADS = 4
REPEATS = 3
FRAME = 1 << 20


def codec_for(backend):
    """pgdelta's Codec on the named binding (it picks the standard library's when both import)."""
    codec = compress.Codec()
    if backend == 'zstandard':
        import zstandard
        codec.backend, codec._module, codec._local = 'zstandard', zstandard, threading.local()
    else:
        from compression import zstd
        codec.backend, codec._module = 'stdlib', zstd
    return codec


def positional_runs(s_root, b_root):
    """The runs pgdelta would send: per relation file, S's pages that differ from B's page at that offset."""
    runs = []
    max_pages = max(1, RUN_BYTES // PAGE)
    b_files = exp37.relation_files(b_root)
    for rel, path in sorted(exp37.relation_files(s_root).items()):
        data = path.read_bytes()
        old = b_files[rel].read_bytes() if rel in b_files else b''
        changed = [i for i in range(-(-len(data) // PAGE))
                   if old[i * PAGE:(i + 1) * PAGE] != data[i * PAGE:(i + 1) * PAGE]]
        for first, count in matching.runs(changed, max_pages):
            runs.append(data[first * PAGE:(first + count) * PAGE])
    return runs


def pool_cell(fn, items, threads, rounds):
    """pgdelta's pattern: submit, keep at most 2 per thread in flight, take results in order."""
    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(fn, items[:threads]))  # start the threads outside the timing
        pending = deque()
        started = time.perf_counter()
        for _ in range(rounds):
            for item in items:
                pending.append(pool.submit(fn, item))
                while len(pending) > 2 * threads:
                    pending.popleft().result()
        while pending:
            pending.popleft().result()
        seconds = time.perf_counter() - started
    return seconds


def receiver_cell(codec, packed, raw, offsets, fd, with_pool, rounds):
    """The receiver's loop: each run written at its offset; decompressed on the pool first, or already."""
    in_pwrite = 0.0
    pool = ThreadPoolExecutor(RECEIVER_THREADS) if with_pool else None
    try:
        if pool:
            list(pool.map(lambda p: codec.decompress(p, FRAME * 64), packed[:RECEIVER_THREADS]))
        pending = deque()
        started = time.perf_counter()
        for _ in range(rounds):
            for i, offset in enumerate(offsets):
                if pool is None:
                    t = time.perf_counter()
                    os.pwrite(fd, raw[i], offset)
                    in_pwrite += time.perf_counter() - t
                    continue
                pending.append((offset, pool.submit(codec.decompress, packed[i], len(raw[i]))))
                while len(pending) > 2 * RECEIVER_THREADS or (pending and i == len(offsets) - 1):
                    at, future = pending.popleft()
                    data = memoryview(future.result())
                    t = time.perf_counter()
                    os.pwrite(fd, data, at)
                    in_pwrite += time.perf_counter() - t
        seconds = time.perf_counter() - started
    finally:
        if pool:
            pool.shutdown()
    return seconds, in_pwrite


def build(a, work):
    pg = exp37.PG(a.pg_bin, tempfile.mkdtemp(prefix='pgs.'))
    template = work / 'template'
    if not template.exists():
        exp37.make_template(pg, template, a.scale)
    b0, s = work / 'uniform-b0', work / 'uniform-s'
    for d in (b0, s):
        shutil.rmtree(d, ignore_errors=True)
        exp37.run(['cp', '-c', '-R', str(template), str(d)])
    pg.start(s)
    for statement in exp37.SCENARIOS['uniform']:
        pg.sql(statement)
    pg.sql('CHECKPOINT')
    pg.stop(s)
    return positional_runs(s, b0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--out', required=True)
    ap.add_argument('--scale', type=int, default=10)
    ap.add_argument('--pg-bin', default='/opt/homebrew/opt/postgresql@17/bin')
    ap.add_argument('--work')
    a = ap.parse_args(argv)
    out, work = Path(a.out), Path(a.work or tempfile.mkdtemp(prefix='exp49.'))
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    runs = build(a, work)
    blob = b''.join(runs)
    frames = {'runs': runs, '1MiB': [blob[i:i + FRAME] for i in range(0, len(blob), FRAME)]}
    offsets, at = [], 0
    for r in runs:
        offsets.append(at)
        at += len(r)
    rounds = -(-MIN_BYTES // len(blob))
    import zstandard
    result = {'scale': a.scale, 'python': platform.python_version(), 'machine': platform.machine(),
              'cpus': os.cpu_count(), 'zstandard': zstandard.__version__, 'raw_bytes': len(blob), 'runs': len(runs),
              'mean_pages_per_run': round(len(blob) / PAGE / len(runs), 3), 'rounds': rounds, 'cells': []}
    codecs = {name: codec_for(name) for name in ('zstandard', 'stdlib')}
    packed = {(name, kind): [c.compress(x) for x in items] for name, c in codecs.items() for kind, items in frames.items()}
    result['wire_bytes'] = {f'{n}/{k}': sum(len(p) for p in v) for (n, k), v in packed.items()}
    print(json.dumps({k: v for k, v in result.items() if k != 'cells'}), flush=True)
    target = work / 'receiver.out'
    fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        for repeat in range(REPEATS):
            for name, codec in codecs.items():
                for op in ('compress', 'decompress'):
                    for kind, items in frames.items():
                        if op == 'compress':
                            work_items, fn = items, codec.compress
                        else:
                            work_items = list(zip(packed[(name, kind)], (len(x) for x in items)))
                            fn = (lambda c: lambda pl: c.decompress(pl[0], pl[1]))(codec)
                        for threads in THREADS:
                            seconds = pool_cell(fn, work_items, threads, rounds)
                            cell = {'cell': 'pool', 'repeat': repeat, 'backend': name, 'op': op, 'frames': kind,
                                    'threads': threads, 'seconds': round(seconds, 4),
                                    'mb_per_s': round(rounds * len(blob) / seconds / 1e6, 1)}
                            result['cells'].append(cell)
                            print(json.dumps(cell), flush=True)
                for with_pool in (False, True):
                    seconds, in_pwrite = receiver_cell(codec, packed[(name, 'runs')], runs, offsets, fd,
                                                       with_pool, rounds)
                    cell = {'cell': 'receiver', 'repeat': repeat, 'backend': name, 'pool': with_pool,
                            'threads': RECEIVER_THREADS if with_pool else 0, 'seconds': round(seconds, 4),
                            'pwrite_s': round(in_pwrite, 4),
                            'pwrite_us_per_run': round(in_pwrite / (rounds * len(runs)) * 1e6, 2)}
                    result['cells'].append(cell)
                    print(json.dumps(cell), flush=True)
    finally:
        os.close(fd)
        target.unlink(missing_ok=True)
    (out / 'results.json').write_text(json.dumps(result, indent=1) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
