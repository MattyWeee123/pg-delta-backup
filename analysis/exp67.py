"""EXP-67's analysis: an old primary after a failover, brought current by pg_rewind, by pgdelta (its fast path refused,
so hashing) and by a re-clone (docs/experiments/EXP-67-rewind-old-primary.md).

    .venv/bin/python analysis/exp67.py [--results results/exp04/exp67]

Per lag (hot10, uniform) and arm (rewind, fast, full): the median time to a current copy (t1 - t0), pg_rewind's own
share (t_rewound - t0) and the replay after it, and whether every copy matched S. Scores 1 (deciding: hot10, rewind at
most 0.6 of pgdelta), 2 (uniform, pgdelta at most 0.85 of rewind) and 3 (every fast run refused by the guard for
B's history, every copy matched, every run and receiver exited 0). The re-clone is reported. Writes
docs/experiments/data/exp67-cells.csv and the figure exp67-rewind.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
ARMS = ('rewind', 'fast', 'full')
LAGS = ('hot10', 'uniform')
LABELS = {'rewind': 'pg_rewind, then replay to current', 'fast': 'pgdelta (fast path refused, so hashing)',
          'full': 'pg_basebackup re-clone'}
HISTORY = ('history', 'timeline')  # the guard's reasons for a diverged B name one of these


def lsn(text):
    high, low = text.split('/')
    return (int(high, 16) << 32) + int(low, 16)


def runs(results):
    out = []
    for meta_path in sorted(Path(results).glob('run-*/meta.json')):
        meta = json.loads(meta_path.read_text())
        folder = meta_path.parent
        send = folder / 'send.json'
        stats = json.loads(send.read_text()) if send.exists() and send.stat().st_size else {}
        fast = stats.get('fast_path') or {}
        rewind_log = folder / 'rewind.log'
        copied = None
        if rewind_log.exists():
            found = re.findall(r'(\d+)/(\d+) kB', rewind_log.read_text())
            copied = int(found[-1][0]) * 1024 if found else None
        out.append({**{k: meta[k] for k in ('run', 'lag', 'arm', 'repeat', 'status', 'match')},
                    'receiver_status': meta.get('receiver_status'), 'seconds': round(meta['t1'] - meta['t0'], 3),
                    'rewind_s': round(meta['t_rewound'] - meta['t0'], 3) if meta.get('t_rewound') else None,
                    'rewind_copied_bytes': copied, 'fast_used': fast.get('used'),
                    'reasons': '; '.join(fast.get('reasons') or []),
                    'pages_sent': (stats.get('pages') or {}).get('sent')})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp67'))
    a = ap.parse_args(argv)
    rows = runs(a.results)
    for r in rows:
        print(r)
    cells = {}
    for lag in LAGS:
        fork = json.loads((Path(a.results) / f'fork-{lag}.json').read_text())
        info = json.loads((Path(a.results) / f'lag-{lag}.json').read_text())
        cell = {'lag': lag, 'wal_since_fork_mb': round((lsn(info['source_lsn']) - lsn(fork['promoted_lsn'])) / 1e6, 1)}
        for arm in ARMS:
            mine = [r for r in rows if r['lag'] == lag and r['arm'] == arm and r['status'] == 0]
            cell[f'{arm}_s'] = round(statistics.median(r['seconds'] for r in mine), 2) if mine else None
            cell[f'{arm}_runs'] = len(mine)
        rewinds = [r for r in rows if r['lag'] == lag and r['arm'] == 'rewind' and r['rewind_s'] is not None]
        if rewinds:
            cell['pg_rewind_s'] = round(statistics.median(r['rewind_s'] for r in rewinds), 2)
            copied = [r['rewind_copied_bytes'] for r in rewinds if r['rewind_copied_bytes']]
            cell['pg_rewind_copied_mb'] = round(statistics.median(copied) / 1e6, 1) if copied else None
        sent = [r['pages_sent'] for r in rows if r['lag'] == lag and r['arm'] == 'fast' and r['pages_sent']]
        cell['fast_pages_sent'] = statistics.median(sent) if sent else None
        for x, y in (('rewind', 'fast'), ('fast', 'rewind'), ('fast', 'full'), ('rewind', 'full')):
            if cell[f'{x}_s'] and cell[f'{y}_s']:
                cell[f'{x}_over_{y}'] = round(cell[f'{x}_s'] / cell[f'{y}_s'], 3)
        cells[lag] = cell
        print(cell)
    hot, uni = cells['hot10'], cells['uniform']
    held = lambda v, bar: v is not None and v <= bar  # noqa: E731
    near = [(lag, key) for lag, key, bar in (('hot10', 'rewind_over_fast', 0.6), ('uniform', 'fast_over_rewind', 0.85))
            if cells[lag].get(key) is not None and abs(cells[lag][key] - bar) <= 0.05]
    print(f"1 (deciding). {'HELD' if held(hot.get('rewind_over_fast'), 0.6) else 'FAILED'}: hot10, rewind / pgdelta "
          f"{hot.get('rewind_over_fast')} (bar 0.6)" + (f'; within 0.05 of a bar: {near} (rule 11)' if near else ''))
    print(f"2. {'HELD' if held(uni.get('fast_over_rewind'), 0.85) else 'FAILED'}: uniform, pgdelta / rewind "
          f"{uni.get('fast_over_rewind')} (bar 0.85)")
    fast_rows = [r for r in rows if r['arm'] == 'fast']
    three = (rows and all(r['status'] == 0 and r['match'] for r in rows)
             and all(r['receiver_status'] == 0 for r in fast_rows)
             and all(r['fast_used'] is False and any(w in r['reasons'] for w in HISTORY) for r in fast_rows))
    print(f"3. {'HELD' if three else 'FAILED'}: statuses {sorted({r['status'] for r in rows})}, all match "
          f"{all(r['match'] for r in rows)}; fast runs {[(r['lag'], r['fast_used'], r['reasons']) for r in fast_rows]}")
    print(f"Reported: re-clone / pgdelta {hot.get('fast_over_full')} and {uni.get('fast_over_full')} (pgdelta / "
          f"re-clone), rewind / re-clone {hot.get('rewind_over_full')} and {uni.get('rewind_over_full')}")
    keys = sorted({k for c in cells.values() for k in c}, key=lambda k: (k != 'lag', k))
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp67-cells.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, restval='')
        w.writeheader()
        w.writerows(cells.values())
    figure(cells)


def figure(cells):
    plotting.style()
    fig, ax = plt.subplots(figsize=(7.6, 4.2))
    colors = {'rewind': COLORS['purple'], 'fast': COLORS['blue'], 'full': COLORS['orange']}
    for i, lag in enumerate(LAGS):
        for j, arm in enumerate(ARMS):
            value = cells[lag].get(f'{arm}_s')
            if value is None:
                continue
            x = i * 4 + j
            ax.bar(x, value, 0.8, color=colors[arm], label=LABELS[arm] if i == 0 else None)
            if arm == 'rewind' and cells[lag].get('pg_rewind_s'):
                ax.bar(x, cells[lag]['pg_rewind_s'], 0.8, color='none', edgecolor=COLORS['black'], hatch='//',
                       lw=0.6, label='of which pg_rewind itself' if i == 0 else None)
            ax.annotate(f'{value:.1f}', (x, value), textcoords='offset points', xytext=(0, 3), ha='center',
                        fontsize=8.5)
    ax.set_xticks([1, 5], [f"hot rows updated 10x\n({cells['hot10']['wal_since_fork_mb']:,.0f} MB of WAL on S)",
                           f"1% of rows, scattered\n({cells['uniform']['wal_since_fork_mb']:,.0f} MB of WAL on S)"])
    ax.set_ylabel('seconds to a current copy (median)')
    ax.legend(fontsize=8.5, loc='upper left')
    ax.set_title('An 18 GB old primary after a failover: pg_rewind, pgdelta, a re-clone (8 GB VMs)', fontsize=10)
    print(plotting.save(fig, 'exp67-rewind'))


if __name__ == '__main__':
    main()
