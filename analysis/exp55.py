"""EXP-55's analysis: what would sub-page deltas cost beyond bytes? (docs/experiments/EXP-55-subpage-costs.md)

    .venv/bin/python analysis/exp55.py [--results results/bytes/exp55/results.json]

Scores:
1 (deciding) up-front digests <= 2.5x on-request digests, and blocks + zstd + up-front digests <= 0.35 x zstd alone;
2 the faster of BLAKE3 and BLAKE2b hashes the up-front design's blocks in <= 3 CPU-seconds on one thread;
3 block writes (median) <= 1.5x whole-run writes, and every patched clone equal to S.
Writes docs/experiments/data/exp55-costs.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'bytes' / 'exp55' / 'results.json'))
    a = ap.parse_args(argv)
    r = json.loads(Path(a.results).read_text())
    req, front = r['designs']['on_request'], r['designs']['up_front']
    grow = front['digest_bytes'] / req['digest_bytes']
    share = (r['subpage_zstd_bytes'] + front['digest_bytes']) / r['positional_zstd_bytes']
    print(f"digests: on request {req['digest_bytes'] / 1e6:.1f} MB ({req['pages']} pages), up front "
          f"{front['digest_bytes'] / 1e6:.1f} MB ({front['pages']} pages); blocks + zstd {r['subpage_zstd_bytes'] / 1e6:.1f} MB, "
          f"zstd alone {r['positional_zstd_bytes'] / 1e6:.1f} MB")
    print(f"1 (deciding). {'HELD' if grow <= 2.5 and share <= 0.35 else 'FAILED'}: up front / on request {grow:.2f}; "
          f"blocks + zstd + up-front digests / zstd alone {share:.3f}")
    best = min(front['blake3_cpu_s'], front['blake2b_cpu_s'])
    print(f"2. {'HELD' if best <= 3.0 else 'FAILED'}: up-front hashing, one thread: BLAKE3 {front['blake3_cpu_s']} s, "
          f"BLAKE2b {front['blake2b_cpu_s']} s (on request: {req['blake3_cpu_s']}, {req['blake2b_cpu_s']})")
    med = {m: statistics.median(w['seconds'] for w in r['writes'] if w['mode'] == m) for m in ('pages', 'blocks')}
    exact = all(w['files_differing_from_s'] == 0 for w in r['writes'] if w['mode'] == 'blocks')
    counts = {m: next((w['writes'], w['bytes']) for w in r['writes'] if w['mode'] == m) for m in ('pages', 'blocks')}
    print(f"3. {'HELD' if med['blocks'] <= 1.5 * med['pages'] and exact else 'FAILED'}: writes {med} "
          f"(blocks / pages {med['blocks'] / med['pages']:.2f}); (writes, bytes) {counts}; every clone equal to S: {exact}")
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp55-costs.csv', 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['measure', 'on_request', 'up_front'])
        for key in ('pages', 'digest_bytes', 'blake3_cpu_s', 'blake2b_cpu_s'):
            w.writerow([key, req[key], front[key]])
        w.writerow(['write_seconds_pages_median', med['pages'], ''])
        w.writerow(['write_seconds_blocks_median', med['blocks'], ''])
        w.writerow(['blocks_zstd_bytes', r['subpage_zstd_bytes'], ''])
        w.writerow(['positional_zstd_bytes', r['positional_zstd_bytes'], ''])


if __name__ == '__main__':
    main()
