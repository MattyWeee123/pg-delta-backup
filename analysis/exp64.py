"""EXP-64's analysis: the fast path beyond memory against hashing and a full copy
(docs/experiments/EXP-64-fast-path-beyond-memory.md).

    .venv/bin/python analysis/exp64.py [--results results/exp04/exp64]

Per pattern (hot, uniform) and arm (hash, fast, full): the median time of the 2 runs (t1 - t0 on dest's clock), with
pgdelta's pages sent and written and whether the fast path ran. Scores 1 (deciding: hot, fast at most 0.35 of hash),
2 (hot, fast at most 0.35 of full), 3 (uniform, fast at most 0.75 of hash), 4 (every fast run used the fast path and
its copy matched the source; every run exited 0) and 5 (control: hash within 20% of EXP-48's v1 times). Writes
docs/experiments/data/exp64-cells.csv and the figure exp64-beyond-memory.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
ARMS = ('hash', 'fast', 'full')
LABELS = {'hash': 'pgdelta, hashing both copies', 'fast': 'pgdelta, WAL-summary fast path', 'full': 'pg_basebackup'}
EXP48_V1 = {'hot': 87.5, 'uniform': 184.6}


def runs(results):
    out = []
    for meta_path in sorted(Path(results).glob('run-*/meta.json')):
        meta = json.loads(meta_path.read_text())
        send = meta_path.parent / 'send.json'
        stats = json.loads(send.read_text()) if send.exists() and send.stat().st_size else {}
        fast = stats.get('fast_path') or {}
        receiver = (stats.get('receiver') or {}).get('fast_path') or {}
        out.append({**{k: meta[k] for k in ('run', 'pattern', 'arm', 'repeat', 'status')},
                    'receiver_status': meta.get('receiver_status'), 'seconds': round(meta['t1'] - meta['t0'], 3),
                    'copy_seconds': meta.get('copy_seconds'), 'match': (meta.get('verify') or {}).get('match'),
                    'fast_used': fast.get('used'), 'proof': (receiver.get('facts') or {}).get('proof'),
                    'reasons': '; '.join(fast.get('reasons') or []),
                    'pages_total': (stats.get('pages') or {}).get('total'), 'pages_sent': (stats.get('pages') or {}).get('sent'),
                    'guard_s': fast.get('guard_seconds')})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp64'))
    a = ap.parse_args(argv)
    rows = runs(a.results)
    for r in rows:
        print(r)
    cells = {}
    for pattern in ('hot', 'uniform'):
        cell = {'pattern': pattern}
        for arm in ARMS:
            mine = [r['seconds'] for r in rows if r['pattern'] == pattern and r['arm'] == arm and r['status'] == 0]
            cell[f'{arm}_s'] = round(statistics.median(mine), 2) if mine else None
            cell[f'{arm}_runs'] = len(mine)
            sent = [r['pages_sent'] for r in rows if r['pattern'] == pattern and r['arm'] == arm and r['pages_sent']]
            if sent:
                cell[f'{arm}_pages_sent'] = statistics.median(sent)
        if cell['fast_s'] and cell['hash_s']:
            cell['fast_over_hash'] = round(cell['fast_s'] / cell['hash_s'], 3)
        if cell['fast_s'] and cell['full_s']:
            cell['fast_over_full'] = round(cell['fast_s'] / cell['full_s'], 3)
        cells[pattern] = cell
        print(cell)
    hot, uni = cells['hot'], cells['uniform']
    def held(value, bar):
        return value is not None and value <= bar
    near = [(p, k) for p, k, bar in (('hot', 'fast_over_hash', 0.35), ('hot', 'fast_over_full', 0.35),
                                      ('uniform', 'fast_over_hash', 0.75))
            if cells[p].get(k) is not None and abs(cells[p][k] - bar) <= 0.05]
    print(f"1 (deciding). {'HELD' if held(hot.get('fast_over_hash'), 0.35) else 'FAILED'}: hot, fast / hash "
          f"{hot.get('fast_over_hash')} (bar 0.35)" + (f'; within 0.05 of a bar: {near} (rule 11)' if near else ''))
    print(f"2. {'HELD' if held(hot.get('fast_over_full'), 0.35) else 'FAILED'}: hot, fast / full "
          f"{hot.get('fast_over_full')} (bar 0.35)")
    print(f"3. {'HELD' if held(uni.get('fast_over_hash'), 0.75) else 'FAILED'}: uniform, fast / hash "
          f"{uni.get('fast_over_hash')} (bar 0.75); fast / full {uni.get('fast_over_full')} (reported)")
    fast_rows = [r for r in rows if r['arm'] == 'fast']
    four = (rows and all(r['status'] == 0 for r in rows)
            and all(r['fast_used'] and r['match'] and r['proof'] == 'backup_manifest' for r in fast_rows))
    print(f"4. {'HELD' if four else 'FAILED'}: statuses {sorted({r['status'] for r in rows})}; fast runs "
          f"{[(r['pattern'], r['fast_used'], r['proof'], r['match']) for r in fast_rows]}")
    control = {p: (cells[p]['hash_s'], EXP48_V1[p]) for p in EXP48_V1}
    five = all(h is not None and abs(h / e - 1) <= 0.20 for h, e in control.values())
    print(f"5. {'HELD' if five else 'FAILED'}: hash against EXP-48's v1 {control}")
    keys = sorted({k for c in cells.values() for k in c}, key=lambda k: (k != 'pattern', k))
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp64-cells.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, restval='')
        w.writeheader()
        w.writerows(cells.values())
    figure(cells)


def figure(cells):
    plotting.style()
    fig, ax = plt.subplots(figsize=(7.6, 4.2))
    colors = {'hash': COLORS['grey'], 'fast': COLORS['blue'], 'full': COLORS['orange']}
    for i, pattern in enumerate(('hot', 'uniform')):
        for j, arm in enumerate(ARMS):
            value = cells[pattern].get(f'{arm}_s')
            if value is None:
                continue
            ax.bar(i * 4 + j, value, 0.8, color=colors[arm], label=LABELS[arm] if i == 0 else None)
            ax.annotate(f'{value:.1f}', (i * 4 + j, value), textcoords='offset points', xytext=(0, 3), ha='center',
                        fontsize=8.5)
    ax.set_xticks([1, 5], ['local changes (hot 1%)', 'scattered changes (uniform 1%)'])
    ax.set_ylabel('seconds to refresh about 18 GB (median)')
    ax.legend(fontsize=8.5, loc='upper left')
    ax.set_title('Beyond memory (18 GB on 8 GB VMs, balanced persistent disks), unthrottled', fontsize=10)
    print(plotting.save(fig, 'exp64-beyond-memory'))


if __name__ == '__main__':
    main()
