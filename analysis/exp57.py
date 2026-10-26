"""EXP-57's analysis: does a fast start checkpoint on a large dirty set hurt a busy primary?
(docs/experiments/EXP-57-checkpoint-burst.md)

    .venv/bin/python analysis/exp57.py [--runs results/exp04/exp57]

Per run, from pgbench's per-transaction logs (with -R: client, transaction, latency us, script, completion s,
completion us, schedule lag us) in 1-second buckets over [t0, t1 + 10 s] ([t0, t0 + 70 s] for none): S_b the
transactions scheduled in bucket b (completion minus latency), X_b those completed; D = max(1 - X_b / S_b),
W = sum(max(0, S_b - X_b)) / rate, and the p99 latency of the transactions completed in the window. Net values
subtract the none arm's medians. From the server log, each checkpoint's buffers written and write and sync times.
Scores 1 (dirty set >= 1 GB in every trial), 2 (deciding: fast's net D >= 0.30 and fast's net W >= 2x spread's) and
3 (fast's pg_backup_start blocked >= 20 s). Writes docs/experiments/data/exp57-runs.csv and the figure.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUFFER = 8192
ARMS = ('none', 'fast', 'spread')
_CKPT = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+) (\w+) .*checkpoint complete: wrote (\d+) buffers .*'
                   r'write=([\d.]+) s, sync=([\d.]+) s, total=([\d.]+) s')


def transactions(run):
    """[(scheduled, completed, latency s)] of every logged transaction of the run."""
    out = []
    for log in run.glob('load*'):
        for line in log.read_text().splitlines():
            parts = line.split()
            if len(parts) < 6:
                continue
            latency = int(parts[2]) / 1e6
            done = int(parts[4]) + int(parts[5]) / 1e6
            out.append((done - latency, done, latency))
    return out


def window_metrics(txs, start, end, rate):
    buckets = range(int(math.floor(start)), int(math.ceil(end)))
    scheduled = {b: 0 for b in buckets}
    completed = {b: 0 for b in buckets}
    latencies = []
    for s, c, latency in txs:
        if int(s) in scheduled:
            scheduled[int(s)] += 1
        if int(c) in completed:
            completed[int(c)] += 1
            latencies.append(latency)
    drops = [1 - completed[b] / scheduled[b] for b in buckets if scheduled[b]]
    lost = sum(max(0, scheduled[b] - completed[b]) for b in buckets) / rate
    latencies.sort()
    p99 = latencies[int(0.99 * (len(latencies) - 1))] if latencies else None
    return max(drops) if drops else None, lost, p99


def checkpoints(log):
    out = []
    for line in Path(log).read_text().splitlines():
        m = _CKPT.match(line)
        if m:
            when = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S.%f').replace(tzinfo=timezone.utc).timestamp()
            out.append({'at': when, 'buffers': int(m.group(3)), 'write_s': float(m.group(4)),
                        'sync_s': float(m.group(5)), 'total_s': float(m.group(6))})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--runs', default=str(ROOT / 'results' / 'exp04' / 'exp57'))
    a = ap.parse_args(argv)
    root = Path(a.runs)
    ckpts = checkpoints(root / 'postgres-final.log')
    rows = []
    for run in sorted(root.glob('run-*')):
        meta = json.loads((run / 'meta.json').read_text())
        end = meta['t0'] + 70 if meta['arm'] == 'none' else meta['t1'] + 10
        d, w, p99 = window_metrics(transactions(run), meta['t0'], end, meta['rate'])
        mine = [c for c in ckpts if meta['start_called'] - 1 <= c['at'] <= meta['t1'] + 2] if meta['arm'] != 'none' else []
        rows.append({'run': meta['run'], 'arm': meta['arm'], 'repeat': meta['repeat'],
                     'dirty_gb': round(meta['dirty_buffers'] * BUFFER / 1e9, 2),
                     'start_blocked_s': round(meta['start_returned'] - meta['start_called'], 2),
                     'D': round(d, 3) if d is not None else None, 'W_s': round(w, 2),
                     'p99_ms': round(p99 * 1000, 1) if p99 is not None else None,
                     'ckpt_buffers': mine[0]['buffers'] if mine else None,
                     'ckpt_write_s': mine[0]['write_s'] if mine else None, 'ckpt_sync_s': mine[0]['sync_s'] if mine else None})
        print(rows[-1])
    med = {arm: {k: statistics.median(r[k] for r in rows if r['arm'] == arm) for k in ('D', 'W_s', 'p99_ms', 'start_blocked_s')}
           for arm in ARMS}
    net = {arm: {k: max(0.0, med[arm][k] - med['none'][k]) for k in ('D', 'W_s')} for arm in ('fast', 'spread')}
    print('medians', med)
    print('net', net)
    print(f"1. {'HELD' if all(r['dirty_gb'] >= 1.0 for r in rows) else 'FAILED'}: dirty GB at t0 "
          f"{[r['dirty_gb'] for r in rows]}")
    ok = net['fast']['D'] >= 0.30 and net['fast']['W_s'] >= 2 * net['spread']['W_s']
    print(f"2 (deciding). {'HELD' if ok else 'FAILED'}: fast net D {net['fast']['D']:.3f}, W {net['fast']['W_s']:.2f} s; "
          f"spread net D {net['spread']['D']:.3f}, W {net['spread']['W_s']:.2f} s")
    print(f"3. {'HELD' if med['fast']['start_blocked_s'] >= 20 else 'FAILED'}: fast start blocked "
          f"{med['fast']['start_blocked_s']} s (spread {med['spread']['start_blocked_s']} s)")
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp57-runs.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    figure(root, rows)


def figure(root, rows):
    """Completed ÷ scheduled per second around t0, one line per run, by arm."""
    import plotting
    from plotting import COLORS, plt
    plotting.style()
    colors = {'none': COLORS['grey'], 'fast': COLORS['red'], 'spread': COLORS['blue']}
    fig, ax = plt.subplots(figsize=(9, 4))
    for run in sorted(root.glob('run-*')):
        meta = json.loads((run / 'meta.json').read_text())
        txs = transactions(run)
        start = int(meta['t0']) - 20
        span = range(start, start + 320)
        scheduled = {b: 0 for b in span}
        completed = {b: 0 for b in span}
        for s, c, _ in txs:
            if int(s) in scheduled:
                scheduled[int(s)] += 1
            if int(c) in completed:
                completed[int(c)] += 1
        xs = [b - meta['t0'] for b in span if scheduled[b]]
        ys = [completed[b] / scheduled[b] for b in span if scheduled[b]]
        ax.plot(xs, ys, color=colors[meta['arm']], lw=0.9, alpha=0.8,
                label=meta['arm'] if meta['repeat'] == 0 else None)
    ax.axvline(0, color=COLORS['black'], lw=0.8, ls=':')
    ax.set_xlabel('seconds from the arm (t0: pg_backup_start called)')
    ax.set_ylabel('transactions completed ÷ scheduled, per second')
    ax.set_title('A busy primary while pg_backup_start checkpoints a large dirty set (balanced PD)', fontsize=10)
    ax.legend(fontsize=8)
    fig.tight_layout()
    print(plotting.save(fig, 'exp57-checkpoint-burst'))


if __name__ == '__main__':
    main()
