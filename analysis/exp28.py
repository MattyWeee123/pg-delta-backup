"""EXP-28's analysis: v1 reading a live, busy primary against one pg_basebackup stream (docs/experiments/EXP-28-live-slowdown.md).

    .venv/bin/python analysis/exp28.py [--runs DIR ...] [--figdir DIR]   (default: the grid and its extra repeats)

From the client's per-transaction log (pgbench -l with -R: client, transaction, latency us, script, completion s,
completion us, schedule lag us), on dest's clock by chrony's offsets: per run, over [t0, t1 + 10 s] in 1-s buckets,
S_b the transactions scheduled in bucket b (completion minus latency) and X_b those completed in it; the worst drop
D = max(1 - X_b / S_b) and the work lost W = sum(max(0, S_b - X_b)) / R (EXP-12's change 3). Per cell (arm, speed):
medians over its runs; net values against the same speed's `none` runs (below 0 counts as 0). "Meets O4": W_net(arm)
- W_net(full) <= delta_W = max(0.05 s, 0.15 x W_net(full)) and D_net(arm) - D_net(full) <= delta_D = 0.03, EXP-12's
floors; also reported at half and twice them. Also the refresh time, the source's CPU-seconds (VM-wide, /proc/stat)
and the p95 and p99 latency over the window. Writes docs/experiments/data/exp28-cells.csv and docs/figures/exp28-slowdown.svg.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

import numpy as np

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
TAIL = 10.0
ARMS = ['none', 'full', 'v1_j1', 'v1_j2', 'v1_j4']
SPEEDS = ['unthrottled', '2gbit']
FLOOR_W, FLOOR_D, SHARE_W = 0.05, 0.03, 0.15
USER_HZ = 100


def shift(root):
    """Seconds to add to a client timestamp to get dest's clock (chrony: each VM's offset from the same reference)."""
    offs = [json.loads((Path(root) / f'offsets-{w}.json').read_text()) for w in ('start', 'end')]
    return statistics.mean(o['dest'] - o['client'] for o in offs if o.get('dest') is not None
                           and o.get('client') is not None)


def transactions(root, offset):
    """(scheduled, completed, latency in ms) on dest's clock, for every logged transaction."""
    out = []
    with gzip.open(Path(root) / 'load-log.gz', 'rt') as stream:
        for line in stream:
            f = line.split()
            if len(f) < 6:
                continue
            completed = int(f[4]) + int(f[5]) / 1e6 + offset
            latency = int(f[2]) / 1e6
            out.append((completed - latency, completed, latency * 1000))
    return out


def cpu_seconds(a, b):
    x, y = [int(v) for v in a.split()[1:]], [int(v) for v in b.split()[1:]]
    busy = lambda v: sum(v) - v[3] - v[4]  # all but idle and iowait
    return (busy(y) - busy(x)) / USER_HZ


def runs_of(root, txs):
    sched = np.array(sorted(t[0] for t in txs))
    done_sorted = sorted((t[1], t[2]) for t in txs)
    done = np.array([t[0] for t in done_sorted])
    lat = np.array([t[1] for t in done_sorted])
    out = []
    for d in sorted(Path(root).glob('run-*')):
        meta = json.loads((d / 'meta.json').read_text())
        t0, t1, rate = meta['t0'], meta['t1'], meta['rate']
        n = int(math.floor(t1 + TAIL - t0))
        edges = t0 + np.arange(n + 1)
        s = np.histogram(sched, edges)[0]
        x = np.histogram(done, edges)[0]
        w = float(np.maximum(0, s - x).sum()) / rate
        dmax = float(max(1 - xi / si for si, xi in zip(s, x) if si)) if s.any() else None
        inside = lat[(done >= t0) & (done < t1 + TAIL)]
        out.append({**meta, 'run_dir': f'{d.parent.name}/{d.name}', 'seconds': t1 - t0, 'W': w, 'D': max(dmax, 0.0),
                    'p95_ms': float(np.percentile(inside, 95)) if inside.size else None,
                    'p99_ms': float(np.percentile(inside, 99)) if inside.size else None,
                    'cpu_s': cpu_seconds(meta['cpu0'], meta['cpu1']),
                    'ok': meta['status'] == 0 and meta.get('receiver_status') in (None, 0)})
    return out


def cells(runs):
    by = defaultdict(list)
    for r in runs:
        if r['ok']:
            by[(r['arm'], r['speed'])].append(r)
    med = lambda rs, k: statistics.median(r[k] for r in rs if r[k] is not None) if rs else None
    rows = []
    for speed in SPEEDS:
        base = by.get(('none', speed), [])
        w0, d0 = med(base, 'W'), med(base, 'D')
        full = by.get(('full', speed), [])
        wf = max(0.0, med(full, 'W') - w0) if full and base else None
        df = max(0.0, med(full, 'D') - d0) if full and base else None
        for arm in ARMS:
            rs = by.get((arm, speed), [])
            if not rs:
                continue
            row = {'arm': arm, 'speed': speed, 'runs': len(rs), 'seconds': med(rs, 'seconds'), 'W': med(rs, 'W'),
                   'D': med(rs, 'D'), 'p95_ms': med(rs, 'p95_ms'), 'p99_ms': med(rs, 'p99_ms'),
                   'cpu_s': med(rs, 'cpu_s')}
            if base:
                row['W_net'] = max(0.0, row['W'] - w0)
                row['D_net'] = max(0.0, row['D'] - d0)
            if wf is not None and arm not in ('none', 'full'):
                row['W_excess'] = row['W_net'] - wf
                row['D_excess'] = row['D_net'] - df
                for k in (0.5, 1, 2):
                    dw, dd = max(FLOOR_W, SHARE_W * wf) * k, FLOOR_D * k
                    row[f'meets_x{k:g}'] = row['W_excess'] <= dw and row['D_excess'] <= dd
                row['delta_W'], row['delta_D'] = max(FLOOR_W, SHARE_W * wf), FLOOR_D
                row['near'] = (abs(row['W_excess'] / row['delta_W'] - 1) < 0.2
                               or abs(row['D_excess'] / row['delta_D'] - 1) < 0.2)
            rows.append(row)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    base = ROOT / 'results' / 'exp04' / 'sets' / 't10b'
    ap.add_argument('--runs', nargs='+', default=[str(base / 'exp28'), str(base / 'exp28repeats')])
    ap.add_argument('--figdir')
    ap.add_argument('--tag', default='')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    runs = []
    for root in [r for r in a.runs if Path(r).exists()]:
        runs += runs_of(root, transactions(root, shift(root)))
    bad = [r for r in runs if not r['ok']]
    print(f'{len(runs)} runs, {len(runs) - len(bad)} counted' + (f'; failed: {[(r["run_dir"], r["arm"]) for r in bad]}'
                                                                if bad else ''))
    rows = cells(runs)
    fmt = lambda v, p=3: '–' if v is None else f'{v:.{p}f}'
    print('arm, speed: runs | time s | W s (net) | D (net) | p95/p99 ms | source CPU-s | excess over full: W, D | meets O4')
    for r in rows:
        print(f"  {r['arm']:6s} {r['speed']:11s} {r['runs']} | {fmt(r['seconds'], 2)} | {fmt(r['W'])} ({fmt(r.get('W_net'))})"
              f" | {fmt(r['D'])} ({fmt(r.get('D_net'))}) | {fmt(r['p95_ms'], 1)}/{fmt(r['p99_ms'], 1)} | {fmt(r['cpu_s'], 1)}"
              + (f" | {r['W_excess']:+.3f}, {r['D_excess']:+.3f} | x0.5 {r['meets_x0.5']}, x1 {r['meets_x1']}, "
                 f"x2 {r['meets_x2']}" + (' (near the line)' if r['near'] else '') if 'W_excess' in r else ''))
    data = ROOT / 'docs' / 'experiments' / 'data' / f'exp28-cells{a.tag}.csv'
    keys = sorted({k for r in rows for k in r}, key=lambda k: (k not in ('arm', 'speed', 'runs'), k))
    with open(data, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    print(data)

    fig, axes = plt.subplots(1, 3, figsize=(13, 4.2))
    arms = [x for x in ARMS if x != 'none']
    xs = np.arange(len(arms))
    for ax, key, title, floor in ((axes[0], 'W_net', 'Work lost beyond load-only time (s)', FLOOR_W),
                                  (axes[1], 'D_net', 'Worst one-second drop beyond load-only time', FLOOR_D)):
        for j, (speed, color) in enumerate((('unthrottled', COLORS['blue']), ('2gbit', COLORS['orange']))):
            vals = [next((r.get(key) for r in rows if r['arm'] == arm and r['speed'] == speed), None) for arm in arms]
            ax.bar(xs + (j - 0.5) * 0.38, [v or 0 for v in vals], 0.38, color=color,
                   label='unthrottled' if speed == 'unthrottled' else '2 Gbit/s')
            full = next((r.get(key) for r in rows if r['arm'] == 'full' and r['speed'] == speed), None)
            if full is not None:
                bar = full + (max(floor, SHARE_W * full) if key == 'W_net' else floor)
                ax.hlines(bar, -0.5, len(arms) - 0.5, color=color, ls='--', lw=0.9)
        ax.set_xticks(xs, ['pg_basebackup' if x == 'full' else x.replace('v1_j', 'v1, ') + (' worker' if x == 'v1_j1'
                           else ' workers') for x in arms], fontsize=8.5)
        ax.set_title(title + '\n(dashed: O4\'s bar, pg_basebackup plus its tolerance)', fontsize=9.5)
    axes[0].legend(fontsize=8)
    for speed, color in (('unthrottled', COLORS['blue']), ('2gbit', COLORS['orange'])):
        pts = [(r['seconds'], r.get('W_net') or 0, r['arm']) for r in rows if r['speed'] == speed and r['arm'] != 'none']
        axes[2].scatter([p[0] for p in pts], [p[1] for p in pts], color=color, zorder=3)
        for p in pts:
            axes[2].annotate(p[2].replace('v1_j', 'v1 j').replace('full', 'pg_basebackup'), (p[0], p[1]),
                             textcoords='offset points', xytext=(4, 3), fontsize=7.5)
    axes[2].set_xlabel('refresh time (s)')
    axes[2].set_ylabel('work lost beyond load-only time (s)')
    axes[2].set_title('Time against the load\'s loss', fontsize=9.5)
    fig.tight_layout()
    print(plotting.save(fig, f'exp28-slowdown{a.tag}'))


if __name__ == '__main__':
    main()
