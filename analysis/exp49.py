"""EXP-49's analysis: what sets zstd's floor? (docs/experiments/EXP-49-zstd-floor.md)

    .venv/bin/python analysis/exp49.py [--results results/bytes/exp49/results.json]

Medians over the repeats of every cell; scores the registered predictions on the zstandard binding:
1 (deciding) the receiver's main thread spends at least 1.5x as long in pwrite with the 4-thread decompressing pool;
2 four threads reach less than 2.5x one thread's throughput on pgdelta's runs, compressing and decompressing;
3 one thread compresses pgdelta's runs at no more than 0.7x its rate on 1 MiB frames.
Writes docs/experiments/data/exp49-cells.csv and the figure exp49-zstd-floor.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKENDS = ('zstandard', 'stdlib')


def medians(result):
    groups = {}
    for c in result['cells']:
        if c['cell'] == 'pool':
            key = ('pool', c['backend'], c['op'], c['frames'], c['threads'])
            groups.setdefault(key, []).append(c['mb_per_s'])
        else:
            key = ('receiver', c['backend'], 'pool' if c['pool'] else 'no pool', 'runs', c['threads'])
            groups.setdefault(key, []).append(c['pwrite_us_per_run'])
    return {k: statistics.median(v) for k, v in groups.items()}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'bytes' / 'exp49' / 'results.json'))
    a = ap.parse_args(argv)
    result = json.loads(Path(a.results).read_text())
    m = medians(result)
    print(f"data: {result['runs']} runs, {result['mean_pages_per_run']} pages per run (EXP-47: 2.56), "
          f"{result['raw_bytes'] / 1e6:.1f} MB; zstandard {result['zstandard']}, Python {result['python']}")
    for k in sorted(m):
        print('  ', k, m[k])
    z = 'zstandard'
    slow = m[('receiver', z, 'pool', 'runs', 4)] / m[('receiver', z, 'no pool', 'runs', 0)]
    print(f"1 (deciding). {'HELD' if slow >= 1.5 else 'FAILED'}: pwrite with the pool / without: {slow:.2f}")
    scale = {op: m[('pool', z, op, 'runs', 4)] / m[('pool', z, op, 'runs', 1)] for op in ('compress', 'decompress')}
    print(f"2. {'HELD' if all(v < 2.5 for v in scale.values()) else 'FAILED'}: 4 threads / 1 on pgdelta's runs: "
          f"{ {k: round(v, 2) for k, v in scale.items()} }")
    frame = m[('pool', z, 'compress', 'runs', 1)] / m[('pool', z, 'compress', '1MiB', 1)]
    print(f"3. {'HELD' if frame <= 0.7 else 'FAILED'}: one thread, pgdelta's runs / 1 MiB frames: {frame:.2f}")
    for b in BACKENDS:
        print(f"   {b}: pwrite slowdown {m[('receiver', b, 'pool', 'runs', 4)] / m[('receiver', b, 'no pool', 'runs', 0)]:.2f}; "
              f"4/1 compress {m[('pool', b, 'compress', 'runs', 4)] / m[('pool', b, 'compress', 'runs', 1)]:.2f}, "
              f"decompress {m[('pool', b, 'decompress', 'runs', 4)] / m[('pool', b, 'decompress', 'runs', 1)]:.2f}")
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp49-cells.csv', 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['cell', 'backend', 'op', 'frames', 'threads', 'median', 'unit'])
        for k in sorted(m):
            w.writerow([*k, m[k], 'MB/s' if k[0] == 'pool' else 'us in pwrite per run'])
    figure(m, result)


def figure(m, result):
    """Left: pool throughput against threads. Right: the receiver's time in pwrite per run, with and without."""
    import plotting
    from plotting import COLORS, plt
    plotting.style()
    fig, (left, right) = plt.subplots(1, 2, figsize=(10, 4.2), gridspec_kw={'width_ratios': [1.6, 1]})
    threads = sorted({k[4] for k in m if k[0] == 'pool'})
    colors = {('compress', 'runs'): COLORS['blue'], ('compress', '1MiB'): COLORS['sky'],
              ('decompress', 'runs'): COLORS['red'], ('decompress', '1MiB'): COLORS['orange']}
    for (op, frames), color in colors.items():
        for b, style in zip(BACKENDS, ('-', '--')):
            ys = [m[('pool', b, op, frames, t)] for t in threads]
            label = f"{op}, {'pgdelta’s runs' if frames == 'runs' else '1 MiB frames'}" if b == 'zstandard' else None
            left.plot(threads, ys, style, marker='o', ms=4, color=color, label=label)
    left.plot([], [], '-', color=COLORS['black'], label='zstandard')
    left.plot([], [], '--', color=COLORS['black'], label='standard library')
    left.set_xscale('log', base=2)
    left.set_xticks(threads, [str(t) for t in threads])
    left.set_xlabel('threads in the pool')
    left.set_ylabel('MB/s of pages, median')
    left.set_title(f"The pools, in pgdelta's pattern ({result['mean_pages_per_run']} pages per run)", fontsize=10)
    left.legend(fontsize=7.5, ncol=2)
    x = range(len(BACKENDS))
    no = [m[('receiver', b, 'no pool', 'runs', 0)] for b in BACKENDS]
    yes = [m[('receiver', b, 'pool', 'runs', 4)] for b in BACKENDS]
    right.bar([i - 0.18 for i in x], no, 0.36, color=COLORS['grey'], label='no pool (already decompressed)')
    right.bar([i + 0.18 for i in x], yes, 0.36, color=COLORS['red'], label='4 threads decompressing')
    right.set_xticks(list(x), ['zstandard', 'standard library'])
    right.set_ylabel('µs in pwrite per run, median')
    right.set_title("The receiver's writes", fontsize=10)
    right.legend(fontsize=7.5)
    fig.tight_layout()
    print(plotting.save(fig, 'exp49-zstd-floor'))


if __name__ == '__main__':
    main()
