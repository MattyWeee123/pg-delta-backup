"""EXP-46's analysis: with compression on a pool of threads, pipelined with sending, is v1 faster at DR?
(docs/experiments/EXP-46-zstd-pool.md)

    .venv/bin/python analysis/exp46.py [--sweep results/exp04/exp46] [--figdir DIR]

EXP-45's analysis on EXP-46's runs (the cells, predictions 1, 2 and 4, and the bytes; files tagged -pool), then from
pgdelta's own statistics: every v1 run's effective congestion control (BBR required), and for v1 with zstd the
compressing threads, the time compressing summed over them, and the time the sending thread waited for compressed
runs (prediction 3: at most 1.1 s for uniform, a quarter of EXP-45's serial 4.1-4.7 s).
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import exp45

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--sweep', default=str(ROOT / 'results' / 'exp04' / 'exp46'))
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    exp45.main(['--sweep', a.sweep, '--tag', '-pool', *(['--figdir', a.figdir] if a.figdir else [])])
    effective, waits = [], {}
    for log in sorted(Path(a.sweep).glob('logs/*/source/*-stats.json.log')):
        text = log.read_text()
        stats = json.loads(text[text.find('{'):])
        effective.append((stats.get('congestion_control') or {}).get('effective'))
        compress = stats.get('compress')
        if compress:
            pattern = 'uniform' if 'uniform' in str(log.parent.parent.name) else 'hot'
            waits.setdefault(pattern, []).append(compress)
    print(f"BBR in effect: {sum(e == 'bbr' for e in effective)} of {len(effective)} v1 runs")
    for pattern, runs in sorted(waits.items()):
        print(f"  {pattern}: threads {runs[0]['threads']}, compressing {statistics.median(r['seconds'] for r in runs):.2f} s "
              f"(summed), sender waited {statistics.median(r['wait_seconds'] for r in runs):.2f} s, ratio "
              f"{statistics.median(r['raw_bytes'] / r['wire_bytes'] for r in runs):.2f}")
    uniform = waits.get('uniform', [])
    held = bool(uniform) and all(r['wait_seconds'] <= 1.1 for r in uniform)
    print(f"3. {'HELD' if held else 'FAILED'}: uniform, the sender waited at most 1.1 s for compressed runs "
          f"({[round(r['wait_seconds'], 2) for r in uniform]})")


if __name__ == '__main__':
    main()
