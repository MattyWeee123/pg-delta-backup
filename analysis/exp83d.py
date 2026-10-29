"""EXP-83d's analysis: why summarize_wal cost beyond memory in EXP-83b and EXP-83c, and what it costs a CPU-bound
server (docs/experiments/EXP-83d-summarize-wal-cost-mechanism.md).

    .venv/bin/python analysis/exp83d.py [--runs docs/experiments/data/exp83d-runs.csv] [--figure exp83d-cost]

Cell "disk" (scale 600, beyond memory) runs in long blocks (bench.py --design blocks, ABBA BAAB): each block's scored
runs are averaged, and adjacent blocks (one on, one off) form a pair. Cell "cpu" (scale 100, pgbench -N, 90 clients)
runs in pairs with settle runs, as EXP-83c. Scores:
1 (deciding: the mechanism) in "disk", the summarizer reads at most 10% of the WAL it summarizes from storage
  (/proc/<pid>/io read_bytes over the WAL generated in on blocks);
2 in "disk", with no switch in the data, the throughput ratio's lower 95% bound is at least 0.97 and the mean p99
  ratio at most 1.10;
3 in "cpu", the source is CPU-bound (busy at least 0.80), the throughput ratio's lower 95% bound is at least 0.97 and
  the mean p99 ratio at most 1.10;
4 in both, the summarizer takes at most 1.0 ms of CPU per MB of WAL.
And it states the registered decision on the mechanism: H1 (the earlier cost was the switches': 2 holds), H2 (a
steady-state cost from the summarizer reading WAL from storage: 2 fails and the summarizer read at least 50% of its
WAL from storage) or unresolved (anything else).
"""
from __future__ import annotations

import argparse
import csv
import statistics
from collections import defaultdict
from pathlib import Path

import exp83
import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'docs' / 'experiments' / 'data'


def number(row, key):
    value = row.get(key)
    return None if value in ('', None) else float(value)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--runs', default=str(DATA / 'exp83d-runs.csv'))
    ap.add_argument('--figure', default='exp83d-cost')
    a = ap.parse_args(argv)
    rows = [r for r in csv.DictReader(open(a.runs)) if r['label'] == 'scored']
    out = {}
    # disk: block means, adjacent blocks paired.
    disk = [r for r in rows if r['cell'] == 'disk']
    blocks = defaultdict(list)
    for r in disk:
        blocks[int(r['block'])].append(r)
    means = {b: {'mode': rs[0]['mode'], 'tps': statistics.fmean(float(r['tps']) for r in rs),
                 'p99': statistics.fmean(float(r['lat_p99_ms']) for r in rs)} for b, rs in blocks.items()}
    pairs = defaultdict(dict)
    for b, m in means.items():
        pairs[b // 2][m['mode']] = m
    complete = [p for p in pairs.values() if 'on' in p and 'off' in p]
    tps = exp83.interval([p['on']['tps'] / p['off']['tps'] for p in complete]) if complete else (None, None, None)
    p99 = exp83.interval([p['on']['p99'] / p['off']['p99'] for p in complete]) if complete else (None, None, None)
    on = [r for r in disk if r['mode'] == 'on']
    wal_on = sum(float(r['wal_mb']) for r in on)
    read = [number(r, 'summarizer_read_mb') for r in on]
    rchar = [number(r, 'summarizer_rchar_mb') for r in on]
    storage_share = sum(read) / wal_on if on and None not in read and wal_on else None
    rchar_share = sum(rchar) / wal_on if on and None not in rchar and wal_on else None
    one = storage_share is not None and storage_share <= 0.10
    print(f"disk: {len(complete)} block pairs; throughput on/off {tps}; p99 on/off {p99}")
    print(f"1 (deciding) disk. {'HELD' if one else 'FAILED'}: the summarizer read {storage_share} of its WAL from storage "
          f"(bar 0.10); {rchar_share} of it in all (read calls)")
    two = tps[1] is not None and tps[1] >= 0.97 and p99[0] is not None and p99[0] <= 1.10
    print(f"2 disk. {'HELD' if two else 'FAILED'}: throughput lower bound {tps[1]} (bar 0.97), mean p99 ratio {p99[0]} "
          f"(bar 1.10)")
    if two:
        decision = 'H1: no cost with no switch in the data; the earlier cost was the switches\''
    elif storage_share is not None and storage_share >= 0.50:
        decision = 'H2: a steady-state cost, the summarizer reading its WAL from storage'
    else:
        decision = 'unresolved: a cost without the summarizer reading its WAL from storage (or no data)'
    print(f"decision on the mechanism (registered rule): {decision}")
    disk_busy = {m: statistics.fmean(number(r, 'disk_busy') for r in disk if r['mode'] == m and number(r, 'disk_busy') is not None)
                 for m in ('on', 'off') if any(r['mode'] == m and number(r, 'disk_busy') is not None for r in disk)}
    disk_read = {m: statistics.fmean(number(r, 'disk_read_mb') / float(r['seconds']) for r in disk
                                     if r['mode'] == m and number(r, 'disk_read_mb') is not None)
                 for m in ('on', 'off') if any(r['mode'] == m and number(r, 'disk_read_mb') is not None for r in disk)}
    print(f"reported disk: data disk busy {disk_busy}; data disk reads MB/s {disk_read}; block means "
          f"{ {b: (m['mode'], round(m['tps']), round(m['p99'], 1)) for b, m in sorted(means.items())} }")
    out['disk'] = {'tps': tps, 'p99': p99, 'share': storage_share, 'rchar': rchar_share, 'means': means,
                   'ratios': [p['on']['tps'] / p['off']['tps'] for p in complete],
                   'p99s': [p['on']['p99'] / p['off']['p99'] for p in complete]}
    # cpu: pairs, as EXP-83.
    cpu = defaultdict(dict)
    for r in rows:
        if r['cell'] == 'cpu':
            cpu[int(r['pair'])][r['mode']] = r
    cp = [p for p in cpu.values() if 'on' in p and 'off' in p]
    ctps = exp83.interval([float(p['on']['tps']) / float(p['off']['tps']) for p in cp]) if cp else (None, None, None)
    cp99 = exp83.interval([float(p['on']['lat_p99_ms']) / float(p['off']['lat_p99_ms']) for p in cp]) if cp else (None, None, None)
    busy = [number(p[m], 'server_busy') for p in cp for m in ('on', 'off') if number(p[m], 'server_busy') is not None]
    busy_mean = statistics.fmean(busy) if busy else None
    three = (busy_mean is not None and busy_mean >= 0.80 and ctps[1] is not None and ctps[1] >= 0.97
             and cp99[0] is not None and cp99[0] <= 1.10)
    print(f"cpu: {len(cp)} pairs; throughput on/off {ctps}; p99 on/off {cp99}")
    print(f"3 cpu. {'HELD' if three else 'FAILED'}: source busy {busy_mean} (bar 0.80); throughput lower bound {ctps[1]} "
          f"(bar 0.97); mean p99 ratio {cp99[0]} (bar 1.10)")
    out['cpu'] = {'tps': ctps, 'p99': cp99, 'busy': busy_mean,
                  'ratios': [float(p['on']['tps']) / float(p['off']['tps']) for p in cp],
                  'p99s': [float(p['on']['lat_p99_ms']) / float(p['off']['lat_p99_ms']) for p in cp]}
    us = {}
    for cell in ('disk', 'cpu'):
        mine = [r for r in rows if r['cell'] == cell and r['mode'] == 'on' and number(r, 'summarizer_cpu_s') is not None]
        if mine:
            us[cell] = statistics.fmean(float(r['summarizer_cpu_s']) * 1e6 / float(r['wal_mb']) for r in mine)
    four = len(us) == 2 and all(v <= 1000 for v in us.values())
    print(f"4. {'HELD' if four else 'FAILED'}: the summarizer's CPU per MB of WAL {({k: round(v) for k, v in us.items()})} (bar 1000)")
    for cell in ('disk', 'cpu'):
        mine = [r for r in rows if r['cell'] == cell]
        if mine:
            print(f"reported {cell}: throughput off {statistics.fmean(float(r['tps']) for r in mine if r['mode'] == 'off'):.0f}, "
                  f"on {statistics.fmean(float(r['tps']) for r in mine if r['mode'] == 'on'):.0f} tps; WAL "
                  f"{statistics.fmean(float(r['wal_mb']) / float(r['seconds']) for r in mine):.1f} MB/s")
    figure(out, us, a.figure)


def figure(out, us, name):
    plotting.style()
    fig, (left, right) = plt.subplots(1, 2, figsize=(10.5, 4.2))
    for i, cell in enumerate(('cpu', 'disk')):
        r = out.get(cell)
        if not r or r['tps'][0] is None:
            continue
        for k, (key, stat, color, label) in enumerate((('ratios', 'tps', COLORS['blue'], 'throughput on/off'),
                                                       ('p99s', 'p99', COLORS['orange'], 'p99 latency on/off'))):
            x = i * 3 + k
            left.plot([x] * len(r[key]), r[key], 'o', color=color, alpha=0.5, ms=5, label=label if i == 0 else None)
            s = r[stat]
            if s[1] is not None:
                left.errorbar(x + 0.25, s[0], yerr=[[s[0] - s[1]], [s[2] - s[0]]], fmt='s', color=color, capsize=4, ms=6)
    left.axhline(1.0, color=COLORS['grey'], lw=1, ls='--')
    left.axhline(0.97, color=COLORS['blue'], lw=1, ls=':', label='throughput bar (0.97)')
    left.set_xticks([0.5, 3.5], ['cpu (-N, 90 clients,\nin memory; pairs)', 'disk (17 GB beyond memory;\nlong blocks)'],
                    fontsize=8.5)
    left.set_ylabel('ratio, summarize_wal on over off\n(dots: pairs or block pairs; squares: mean, 95%)')
    left.legend(fontsize=7.5, loc='best')
    left.set_title('What summarize_wal costs, with no switch in the data', fontsize=9.5)
    d = out.get('disk', {})
    bars = [('WAL the summarizer\nread (any source)', d.get('rchar')), ('of it, from storage', d.get('share'))]
    xs = range(len(bars))
    right.bar(xs, [100 * (v or 0) for _, v in bars], 0.6, color=[COLORS['sky'], COLORS['red']])
    for x, (_, v) in zip(xs, bars):
        right.annotate('n/a' if v is None else f'{100 * v:.1f}%', (x, 100 * (v or 0)), textcoords='offset points',
                       xytext=(0, 3), ha='center', fontsize=8)
    right.set_xticks(list(xs), [label for label, _ in bars], fontsize=8.5)
    right.set_ylabel('share of the WAL generated in on blocks (%)')
    right.set_ylim(0, max(110, max(100 * (v or 0) for _, v in bars) * 1.15))
    right.set_title(f"Beyond memory: where the summarizer's reads came from\n(its CPU: "
                    + ', '.join(f'{c} {v:.0f} us/MB' for c, v in us.items()) + ')', fontsize=9)
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
