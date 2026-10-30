#!/usr/bin/env python3
"""Write a synthetic pgbench log so the live dashboard can be exercised without a VM.

This is a **stack test, not evidence.** It produces nothing that may be cited: its numbers are made up, and it
exists only so the exporter, Prometheus's scraping, and the live dashboard's panels can be checked end to end
before a session spends VM time. Real numbers come from pgbench on the client VM, driven by a lab script.

It writes the same shape pgbench -l writes, in real time, with a latency step while the "backup" is running and
a schedule lag that builds when the offered rate exceeds what the source keeps up with - the shape EXP-28 and
EXP-57 both show.

    python infra/observability/simulate_load.py --seconds 180 --out /tmp/sim
    python infra/observability/pgbench_exporter.py --log-dir /tmp/sim --pattern 'load.*' \
        --window-file /tmp/sim/backup-window.json
"""
from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', required=True, help='directory to write the synthetic log into')
    parser.add_argument('--seconds', type=float, default=180.0, help='how long to run (default 180)')
    parser.add_argument('--rate', type=float, default=400.0,
                        help='offered transactions per second (default 400; a real session runs ~13,000)')
    parser.add_argument('--backup-after', type=float, default=30.0,
                        help='seconds before the backup window opens (default 30)')
    parser.add_argument('--backup-seconds', type=float, default=45.0,
                        help='how long the backup window lasts (default 45)')
    parser.add_argument('--baseline-ms', type=float, default=5.0, help='median latency when idle (default 5)')
    parser.add_argument('--during-ms', type=float, default=70.0,
                        help='median latency during the backup (default 70, roughly EXP-28 v1_j4)')
    parser.add_argument('--seed', type=int, default=28, help='random seed')
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log = out / 'load.1'
    window_file = out / 'backup-window.json'

    rng = random.Random(args.seed)
    started = time.time()
    t0 = started + args.backup_after
    t1 = t0 + args.backup_seconds
    window_file.write_text(json.dumps({'t0_utc': t0, 't1_utc': t1}))

    print(f'simulating {args.seconds:g}s at {args.rate:g} tx/s into {log}; '
          f'backup window {args.backup_after:g}s..{args.backup_after + args.backup_seconds:g}s')
    number = 0
    interval = 1.0 / args.rate
    with open(log, 'w', buffering=1, newline='') as stream:
        while True:
            now = time.time()
            if now - started >= args.seconds:
                break
            inside = t0 <= now <= t1
            median = args.during_ms if inside else args.baseline_ms
            # Lognormal-ish: a right tail, so p99 sits well above p50 as a real OLTP load's does.
            latency_ms = max(0.1, rng.lognormvariate(0, 0.45) * median)
            lag_ms = max(0.0, rng.lognormvariate(0, 0.8) * (median / 4 if inside else 0.4))
            number += 1
            if inside and rng.random() < 0.002:
                stream.write(f'0 {number} failed 0 {int(now)} {int(now % 1 * 1e6):06d} '
                             f'{int(lag_ms * 1000)}\n')
            else:
                stream.write(f'0 {number} {int(latency_ms * 1000)} 0 {int(now)} '
                             f'{int(now % 1 * 1e6):06d} {int(lag_ms * 1000)}\n')
            time.sleep(interval)
    print(f'wrote {number} transactions')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
