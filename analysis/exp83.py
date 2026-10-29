"""EXP-83 and EXP-83b's analysis: what summarize_wal costs a busy server (docs/experiments/EXP-83-summarize-wal-cost.md,
EXP-83b-summarize-wal-cost-vms.md).

    .venv/bin/python analysis/exp83.py --runs docs/experiments/data/exp83-runs.csv --figure exp83-cost
        [--tps-bar 0.97] [--p99-bar 1.10] [--cpu-bar 0.05]

Per cell, from runs in pairs (one with summarize_wal on, one off, in a random order; experiments/summarize_cost/bench.py):
each pair's ratio on/off of throughput, mean latency and 99th-percentile latency, their mean over the pairs and its
95% t-interval. Scores, per cell: 1 (deciding: the throughput ratio's lower 95% bound at least --tps-bar, so
summarize_wal costs at most 1 - that, at 95%), 2 (the mean p99 latency ratio at most --p99-bar) and 3 (the
summarizer's CPU at most --cpu-bar of one core, at the cell's WAL rate). Reports the WAL rate and the summarizer's
CPU per MB of WAL. Writes the figure.
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
from collections import defaultdict
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
# Student's t, two-sided 95% (0.975 quantile), by degrees of freedom.
T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
        11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
        25: 2.060, 30: 2.042}


def t975(df):
    return T975.get(df) or T975[max(k for k in T975 if k <= df)]


def interval(values):
    """(mean, lower, upper): the mean and its two-sided 95% t-interval."""
    mean = statistics.fmean(values)
    if len(values) < 2:
        return mean, None, None
    half = t975(len(values) - 1) * statistics.stdev(values) / math.sqrt(len(values))
    return mean, mean - half, mean + half


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--runs', required=True)
    ap.add_argument('--figure', required=True)
    ap.add_argument('--tps-bar', type=float, default=0.97)
    ap.add_argument('--p99-bar', type=float, default=1.10)
    ap.add_argument('--cpu-bar', type=float, default=0.05)
    a = ap.parse_args(argv)
    rows = [r for r in csv.DictReader(open(a.runs)) if r['label'] == 'scored']
    cells = defaultdict(dict)
    for r in rows:
        cells[r['cell']].setdefault(int(r['pair']), {})[r['mode']] = r
    results = {}
    for cell, pairs in cells.items():
        complete = [p for p in pairs.values() if 'on' in p and 'off' in p]
        ratio = lambda key: [float(p['on'][key]) / float(p['off'][key]) for p in complete]
        tps, mean_lat, p99 = interval(ratio('tps')), interval(ratio('lat_mean_ms')), interval(ratio('lat_p99_ms'))
        on = [p['on'] for p in complete]
        cpu_share = statistics.fmean(float(r['summarizer_cpu_s']) / float(r['seconds']) for r in on
                                     if r['summarizer_cpu_s'] not in ('', None))
        wal_rate = statistics.fmean(float(r['wal_mb']) / float(r['seconds']) for r in on)
        us_per_mb = statistics.fmean(float(r['summarizer_cpu_s']) * 1e6 / float(r['wal_mb']) for r in on
                                     if r['summarizer_cpu_s'] not in ('', None) and float(r['wal_mb']) > 0)
        busy = [float(p[m]['server_busy']) for p in complete for m in ('on', 'off') if p[m]['server_busy'] not in ('', None)]
        results[cell] = {'pairs': len(complete), 'tps': tps, 'lat': mean_lat, 'p99': p99, 'cpu': cpu_share,
                         'wal_rate': wal_rate, 'us_per_mb': us_per_mb,
                         'tps_on': statistics.fmean(float(p['on']['tps']) for p in complete),
                         'tps_off': statistics.fmean(float(p['off']['tps']) for p in complete),
                         'busy': statistics.fmean(busy) if busy else None, 'ratios': ratio('tps'), 'p99s': ratio('lat_p99_ms')}
        fmt = lambda t: f'{t[0]:.4f} [{t[1]:.4f}, {t[2]:.4f}]' if t[1] is not None else f'{t[0]:.4f}'
        print(f"{cell}: {len(complete)} pairs; throughput on/off {fmt(tps)}; mean latency {fmt(mean_lat)}; p99 {fmt(p99)}")
        print(f"1 (deciding) {cell}. {'HELD' if tps[1] is not None and tps[1] >= a.tps_bar else 'FAILED'}: the "
              f"throughput ratio's lower 95% bound {tps[1] if tps[1] is None else round(tps[1], 4)} (bar {a.tps_bar})")
        print(f"2 {cell}. {'HELD' if p99[0] <= a.p99_bar else 'FAILED'}: mean p99 latency ratio {p99[0]:.4f} "
              f"(bar {a.p99_bar})")
        print(f"3 {cell}. {'HELD' if cpu_share <= a.cpu_bar else 'FAILED'}: the summarizer's CPU {cpu_share:.4f} of "
              f"one core (bar {a.cpu_bar})")
        print(f"reported {cell}: throughput {results[cell]['tps_off']:.0f} tps off, {results[cell]['tps_on']:.0f} on; "
              f"WAL {wal_rate:.1f} MB/s; summarizer {us_per_mb:.0f} us of CPU per MB of WAL; server busy "
              f"{results[cell]['busy']}")
    figure(results, a)


def figure(results, a):
    plotting.style()
    cells = list(results)
    fig, (left, right) = plt.subplots(1, 2, figsize=(10.5, 4.0))
    for i, cell in enumerate(cells):
        r = results[cell]
        for k, (key, color) in enumerate((('ratios', COLORS['blue']), ('p99s', COLORS['orange']))):
            x = i * 3 + k
            values = r[key]
            left.plot([x] * len(values), values, 'o', color=color, alpha=0.5, ms=5,
                      label=('throughput on/off' if key == 'ratios' else 'p99 latency on/off') if i == 0 else None)
            stat = r['tps'] if key == 'ratios' else r['p99']
            if stat[1] is not None:
                left.errorbar(x + 0.25, stat[0], yerr=[[stat[0] - stat[1]], [stat[2] - stat[0]]], fmt='s', color=color,
                              capsize=4, ms=6)
    left.axhline(1.0, color=COLORS['grey'], lw=1, ls='--')
    left.axhline(a.tps_bar, color=COLORS['blue'], lw=1, ls=':', label=f'throughput bar ({a.tps_bar})')
    left.set_xticks([i * 3 + 0.5 for i in range(len(cells))], cells, fontsize=8.5)
    left.set_ylabel('ratio, summarize_wal on over off\n(dots: pairs; squares: mean, 95% interval)')
    left.legend(fontsize=7.5, loc='best')
    left.set_title('What summarize_wal costs: throughput and tail latency', fontsize=9.5)
    xs = range(len(cells))
    right.bar(xs, [results[c]['cpu'] * 100 for c in cells], 0.6, color=COLORS['green'])
    for x, c in zip(xs, cells):
        right.annotate(f"{results[c]['cpu'] * 100:.2f}% of a core\n{results[c]['wal_rate']:.0f} MB/s of WAL\n"
                       f"{results[c]['us_per_mb']:.0f} us per MB", (x, results[c]['cpu'] * 100),
                       textcoords='offset points', xytext=(0, 3), ha='center', fontsize=7.5)
    right.set_xticks(list(xs), cells, fontsize=8.5)
    right.set_ylabel("the summarizer's CPU (% of one core)")
    right.set_ylim(0, max(results[c]['cpu'] * 100 for c in cells) * 1.6 + 0.1)
    right.set_title("The summarizer's own CPU", fontsize=9.5)
    print(plotting.save(fig, a.figure))


if __name__ == '__main__':
    main()
