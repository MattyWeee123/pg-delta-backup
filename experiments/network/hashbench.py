"""EXP-29 (T5, lean): pgdelta's per-page hash, keyed BLAKE2b, against keyed BLAKE3, per core (docs/experiments/EXP-29-hash-per-core.md).

    python3 experiments/network/hashbench.py [--seconds 3] [--repeats 5] [--procs 1,4]

Hashes 8 KiB pages the way pgdelta.hashing.Hasher.leaf does (a pre-keyed state copied, updated with the page,
16-byte digest), with BLAKE2b (hashlib) and BLAKE3 (the blake3 package, the official Rust implementation's Python
binding), in N worker processes for --seconds each, --repeats times, alternating the two. Prints one JSON object:
pages per second per process for each hash and N, the medians and the ratio, the CPU model and the SIMD flags.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import os
import platform
import statistics
import time

PAGE = 8192


def worker(name, seconds, queue):
    key, page = os.urandom(32), os.urandom(PAGE)
    if name == 'blake2b':
        base = hashlib.blake2b(key=key, person=b'pgdelta-leaf', digest_size=16)

        def leaf():
            state = base.copy()
            state.update(page)
            return state.digest()
    else:
        import blake3
        base = blake3.blake3(key=key)

        def leaf():
            state = base.copy()
            state.update(page)
            return state.digest(length=16)
    n, deadline = 0, time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        for _ in range(256):
            leaf()
        n += 256
    queue.put(n / seconds)


def measure(name, procs, seconds):
    queue = mp.Queue()
    workers = [mp.Process(target=worker, args=(name, seconds, queue)) for _ in range(procs)]
    for w in workers:
        w.start()
    rates = [queue.get() for _ in workers]
    for w in workers:
        w.join()
    return statistics.mean(rates)


def cpu_info():
    try:
        text = open('/proc/cpuinfo').read()
    except OSError:
        return {'model': platform.processor() or platform.machine(), 'flags': []}
    model = next((line.split(':', 1)[1].strip() for line in text.splitlines() if line.startswith('model name')), None)
    flags = next((line.split(':', 1)[1].split() for line in text.splitlines() if line.startswith('flags')), [])
    return {'model': model, 'flags': sorted(f for f in flags if f in ('sse4_1', 'avx2', 'avx512f', 'avx512vl'))}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--seconds', type=float, default=3.0)
    ap.add_argument('--repeats', type=int, default=5)
    ap.add_argument('--procs', default='1,4')
    a = ap.parse_args()
    import blake3
    out = {'cpu': cpu_info(), 'cpus': os.cpu_count(), 'python': platform.python_version(),
           'blake3': blake3.__version__, 'seconds': a.seconds, 'repeats': a.repeats, 'by_procs': {}}
    for procs in (int(p) for p in a.procs.split(',')):
        runs = {'blake2b': [], 'blake3': []}
        for _ in range(a.repeats):
            for name in ('blake2b', 'blake3'):
                runs[name].append(measure(name, procs, a.seconds))
        med = {name: statistics.median(v) for name, v in runs.items()}
        out['by_procs'][procs] = {'pages_per_s_per_proc': {k: [round(x) for x in v] for k, v in runs.items()},
                                  'median': {k: round(v) for k, v in med.items()},
                                  'ratio_blake3': round(med['blake3'] / med['blake2b'], 3)}
    print(json.dumps(out))


if __name__ == '__main__':
    main()
