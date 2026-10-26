"""EXP-50's analysis: do larger pool tasks lower zstd's floor? (docs/experiments/EXP-50-zstd-batched-tasks.md)

    .venv/bin/python analysis/exp50.py [--runs results/bytes/exp50/runs.jsonl]

Medians over the 3 repeats per arm (the warm-up is left out), from pgdelta's own statistics. Scores:
1 (deciding) zstd_batched - plain <= 0.5 x (zstd_per_run - plain) on pgdelta's total; undecided if zstd_per_run is
  less than 0.3 s slower than plain;
2 rebuild: zstd_per_run >= 1.5x plain's, zstd_batched <= 1.3x plain's;
3 every run complete, and one files digest across all runs.
Writes docs/experiments/data/exp50-runs.csv and the figure exp50-batched-tasks.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARMS = ('plain', 'zstd_per_run', 'zstd_batched')
LABELS = {'plain': 'plain v1', 'zstd_per_run': 'zstd, one run per task', 'zstd_batched': 'zstd, batched tasks'}


def load(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def row(r):
    t, c, rc = r['timings'] or {}, r['compress'] or {}, r['receiver_compress'] or {}
    return {'label': r['label'], 'arm': r['arm'], 'repeat': r['repeat'], 'status': r['status'],
            'total_s': t.get('total'), 'rebuild_s': t.get('rebuild'), 'hash_source_s': t.get('hash_source'),
            'hash_basis_s': t.get('hash_basis'), 'verify_s': t.get('verify'), 'wall_s': r['wall_s'],
            'wire_bytes': r['bytes_source_to_dest'], 'runs': c.get('runs'), 'send_tasks': c.get('tasks'),
            'send_wait_s': c.get('wait_seconds'), 'recv_tasks': rc.get('tasks'), 'recv_wait_s': rc.get('wait_seconds'),
            'files_digest': r['files_digest']}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--runs', default=str(ROOT / 'results' / 'bytes' / 'exp50' / 'runs.jsonl'))
    a = ap.parse_args(argv)
    rows = [row(r) for r in load(a.runs)]
    counted = [r for r in rows if r['repeat'] is not None]
    med = {arm: {k: statistics.median(r[k] for r in counted if r['arm'] == arm)
                 for k in ('total_s', 'rebuild_s', 'wall_s')} for arm in ARMS}
    for arm in ARMS:
        print(arm, med[arm], [r['total_s'] for r in counted if r['arm'] == arm])
    for r in counted:
        if r['arm'] != 'plain':
            print('  ', r['label'], 'runs', r['runs'], 'tasks', r['send_tasks'], r['recv_tasks'],
                  'waits', r['send_wait_s'], r['recv_wait_s'], 'wire', r['wire_bytes'])
    p, z, b = (med[arm]['total_s'] for arm in ARMS)
    excess, left = z - p, b - p
    if excess < 0.3:
        verdict = 'UNDECIDED'
    else:
        verdict = 'HELD' if left <= 0.5 * excess else 'FAILED'
    print(f"1 (deciding). {verdict}: per run +{excess:.2f} s over plain, batched +{left:.2f} s "
          f"({left / excess:.2f} of it)" if excess else f"1. {verdict}")
    rz, rb = med['zstd_per_run']['rebuild_s'] / med['plain']['rebuild_s'], med['zstd_batched']['rebuild_s'] / med['plain']['rebuild_s']
    print(f"2. {'HELD' if rz >= 1.5 and rb <= 1.3 else 'FAILED'}: rebuild over plain's: per run {rz:.2f}, batched {rb:.2f}")
    digests = {r['files_digest'] for r in rows}
    ok = all(r['status'] == 'complete' for r in rows) and len(digests) == 1
    print(f"3. {'HELD' if ok else 'FAILED'}: statuses {sorted({r['status'] for r in rows})}, digests {len(digests)}")
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp50-runs.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=[k for k in rows[0] if k != 'files_digest'])
        w.writeheader()
        w.writerows({k: v for k, v in r.items() if k != 'files_digest'} for r in rows)
    figure(counted, med)


def figure(counted, med):
    """Left: pgdelta's total per arm, medians and each repeat. Right: the receiver's time writing pages."""
    import plotting
    from plotting import COLORS, plt
    plotting.style()
    colors = {'plain': COLORS['grey'], 'zstd_per_run': COLORS['red'], 'zstd_batched': COLORS['blue']}
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 4))
    for ax, key, title in ((axes[0], 'total_s', "pgdelta's total"), (axes[1], 'rebuild_s', "the receiver writing pages")):
        for i, arm in enumerate(ARMS):
            ax.bar(i, med[arm][key], 0.6, color=colors[arm])
            ys = [r[key] for r in counted if r['arm'] == arm]
            ax.scatter([i] * len(ys), ys, s=14, color=COLORS['black'], zorder=3)
            ax.annotate(f"{med[arm][key]:.2f} s", (i, med[arm][key]), textcoords='offset points', xytext=(0, 4),
                        ha='center', fontsize=8)
        ax.set_xticks(range(len(ARMS)), [LABELS[arm] for arm in ARMS], fontsize=8)
        ax.set_ylabel('seconds (bar: median; dots: repeats)')
        ax.set_title(title, fontsize=10)
    fig.suptitle('pgdelta local, scale 70, uniform 1%, 4 jobs (the Mac)', fontsize=10)
    fig.tight_layout()
    print(plotting.save(fig, 'exp50-batched-tasks'))


if __name__ == '__main__':
    main()
