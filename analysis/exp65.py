"""EXP-65's analysis: archive replay against the fast path and a full re-clone, to a current copy
(docs/experiments/EXP-65-archive-catch-up.md).

    .venv/bin/python analysis/exp65.py [--results results/exp04/exp65]

Per lag (hot10, uniform) and arm (replay, fast, full): the median time to a current copy (t1 - t0) and whether every
copy matched the source. Scores 1 (deciding: hot10, fast at most 0.25 of replay), 2 (hot10, fast at most 0.25 of full),
3 (uniform, fast at most 1.0 of replay) and 4 (every copy current and correct; the fast path used in every fast run;
every run exited 0). Writes docs/experiments/data/exp65-cells.csv and the figure exp65-archive-catch-up.
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
ARMS = ('replay', 'fast', 'full')
LABELS = {'replay': 'archive replay (WAL already local)', 'fast': 'pgdelta fast path', 'full': 'pg_basebackup re-clone'}


def lsn(text):
    high, low = text.split('/')
    return (int(high, 16) << 32) + int(low, 16)


def replayed_bytes(results, lag, source_lsn):
    """The WAL a replay run applied: from its copy's 'redo starts at' to the source's LSN (added after the data: the
    archive also held WAL from before B0's redo point, so its size overstates what replay paid for)."""
    for meta_path in sorted(Path(results).glob('run-*/meta.json')):
        meta = json.loads(meta_path.read_text())
        log = meta_path.parent / 'copy.log'
        if meta['lag'] == lag and meta['arm'] == 'replay' and log.exists():
            found = re.search(r'redo starts at ([0-9A-F]+/[0-9A-F]+)', log.read_text())
            if found:
                return lsn(source_lsn) - lsn(found.group(1))
    return None


def runs(results):
    out = []
    for meta_path in sorted(Path(results).glob('run-*/meta.json')):
        meta = json.loads(meta_path.read_text())
        send = meta_path.parent / 'send.json'
        stats = json.loads(send.read_text()) if send.exists() and send.stat().st_size else {}
        fast = stats.get('fast_path') or {}
        out.append({**{k: meta[k] for k in ('run', 'lag', 'arm', 'repeat', 'status', 'match')},
                    'seconds': round(meta['t1'] - meta['t0'], 3), 'fast_used': fast.get('used'),
                    'proof': (((stats.get('receiver') or {}).get('fast_path') or {}).get('facts') or {}).get('proof'),
                    'pages_sent': (stats.get('pages') or {}).get('sent')})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp65'))
    a = ap.parse_args(argv)
    rows = runs(a.results)
    for r in rows:
        print(r)
    cells = {}
    for lag in ('hot10', 'uniform'):
        info_path = Path(a.results) / f'lag-{lag}.json'
        info = json.loads(info_path.read_text()) if info_path.exists() else {}
        cell = {'lag': lag, 'archive_mb': round(info.get('archive_bytes', 0) / 1e6, 1)}
        replayed = replayed_bytes(a.results, lag, info['source_lsn']) if info else None
        cell['replayed_mb'] = round(replayed / 1e6, 1) if replayed else None
        for arm in ARMS:
            mine = [r['seconds'] for r in rows if r['lag'] == lag and r['arm'] == arm and r['status'] == 0]
            cell[f'{arm}_s'] = round(statistics.median(mine), 2) if mine else None
            cell[f'{arm}_runs'] = len(mine)
        for other in ('replay', 'full'):
            if cell['fast_s'] and cell[f'{other}_s']:
                cell[f'fast_over_{other}'] = round(cell['fast_s'] / cell[f'{other}_s'], 3)
        cells[lag] = cell
        print(cell)
    hot, uni = cells['hot10'], cells['uniform']
    near = [(lag, key) for lag, key, bar in (('hot10', 'fast_over_replay', 0.25), ('hot10', 'fast_over_full', 0.25),
                                              ('uniform', 'fast_over_replay', 1.0))
            if cells[lag].get(key) is not None and abs(cells[lag][key] - bar) <= 0.05]
    held = lambda v, bar: v is not None and v <= bar  # noqa: E731
    print(f"1 (deciding). {'HELD' if held(hot.get('fast_over_replay'), 0.25) else 'FAILED'}: hot10, fast / replay "
          f"{hot.get('fast_over_replay')} (bar 0.25)" + (f'; within 0.05 of a bar: {near} (rule 11)' if near else ''))
    print(f"2. {'HELD' if held(hot.get('fast_over_full'), 0.25) else 'FAILED'}: hot10, fast / full {hot.get('fast_over_full')}")
    print(f"3. {'HELD' if held(uni.get('fast_over_replay'), 1.0) else 'FAILED'}: uniform, fast / replay "
          f"{uni.get('fast_over_replay')}; fast / full {uni.get('fast_over_full')} (reported)")
    fast_rows = [r for r in rows if r['arm'] == 'fast']
    four = (rows and all(r['status'] == 0 and r['match'] for r in rows)
            and all(r['fast_used'] and r['proof'] == 'clean shutdown in recovery' for r in fast_rows))
    print(f"4. {'HELD' if four else 'FAILED'}: statuses {sorted({r['status'] for r in rows})}, all match "
          f"{all(r['match'] for r in rows)}; fast runs {[(r['lag'], r['fast_used'], r['proof']) for r in fast_rows]}")
    keys = sorted({k for c in cells.values() for k in c}, key=lambda k: (k != 'lag', k))
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp65-cells.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, restval='')
        w.writeheader()
        w.writerows(cells.values())
    figure(cells)


def figure(cells):
    plotting.style()
    fig, ax = plt.subplots(figsize=(7.6, 4.2))
    colors = {'replay': COLORS['purple'], 'fast': COLORS['blue'], 'full': COLORS['orange']}
    for i, lag in enumerate(('hot10', 'uniform')):
        for j, arm in enumerate(ARMS):
            value = cells[lag].get(f'{arm}_s')
            if value is None:
                continue
            ax.bar(i * 4 + j, value, 0.8, color=colors[arm], label=LABELS[arm] if i == 0 else None)
            ax.annotate(f'{value:.1f}', (i * 4 + j, value), textcoords='offset points', xytext=(0, 3), ha='center',
                        fontsize=8.5)
    wal = {lag: cells[lag].get('replayed_mb') or cells[lag]['archive_mb'] for lag in cells}
    ax.set_xticks([1, 5], [f"hot rows updated 10x\n({wal['hot10']:,.0f} MB of WAL replayed)",
                           f"1% of rows, scattered\n({wal['uniform']:,.0f} MB of WAL replayed)"])
    ax.set_ylabel('seconds to a current copy (median)')
    ax.legend(fontsize=8.5, loc='upper left')
    ax.set_title('A standby 18 GB, behind: archive replay, the fast path, a re-clone (8 GB VMs)', fontsize=10)
    print(plotting.save(fig, 'exp65-archive-catch-up'))


if __name__ == '__main__':
    main()
