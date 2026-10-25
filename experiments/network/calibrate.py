"""A short calibration of this host's compute speed, before runs (EXP-18).

    python3 experiments/network/calibrate.py [--seconds 3] [--procs 1,4]

Hashes 8 KiB pages with keyed BLAKE2b (pgdelta's hash) in N worker processes for --seconds and prints one JSON
object: pages per second in total and per process, for each N. It reads no disk and sends nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import os
import time

PAGE = 8192


def worker(seconds, queue):
    key, page = os.urandom(32), os.urandom(PAGE)
    n, deadline = 0, time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        for _ in range(256):
            hashlib.blake2b(page, key=key, digest_size=32).digest()
        n += 256
    queue.put(n)


def measure(procs, seconds):
    queue = mp.Queue()
    workers = [mp.Process(target=worker, args=(seconds, queue)) for _ in range(procs)]
    started = time.perf_counter()
    for w in workers:
        w.start()
    pages = sum(queue.get() for _ in workers)
    for w in workers:
        w.join()
    elapsed = time.perf_counter() - started
    return {'procs': procs, 'pages_per_s': round(pages / elapsed), 'pages_per_s_per_proc': round(pages / elapsed / procs),
            'mib_per_s': round(pages * PAGE / elapsed / 2**20, 1)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--seconds', type=float, default=3.0)
    ap.add_argument('--procs', default='1,4')
    a = ap.parse_args()
    print(json.dumps({'utc': time.time(), 'cpus': os.cpu_count(), 'hash': [measure(int(p), a.seconds)
                                                                           for p in a.procs.split(',')]}))


if __name__ == '__main__':
    main()
