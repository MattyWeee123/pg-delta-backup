"""EXP-64b's analysis: the fast path's source reads overlapped with sending (--read-ahead), on EXP-64's scattered
cell (docs/experiments/EXP-64b-fast-path-read-ahead.md).

    .venv/bin/python analysis/exp64b.py [--results results/exp04/exp64b]

Per arm (hash; fast, fast1 and fast8, the fast path with --read-ahead 0, 1 and 8; full): the median time of the 2
runs (t1 - t0 on dest's clock), with the sender's reading and blocked-sending time, how long its sending thread
waited on the reader threads, and the receiver's waiting, writing and syncing. Scores 1 (deciding: fast8 at most 0.80
of fast), 2 (fast8 at most 0.75 of hash), 3 (fast8's receiver waits for data at most 30 s), 4 (fast1 and fast8 within
15% of each other), 5 (every fast run used the fast path and its copy matched the source; every run exited 0) and 6
(control: fast within 15% of EXP-64's 164.8 s, hash within 20% of its 180.1 s). Writes
docs/experiments/data/exp64b-cells.csv and the figure exp64b-read-ahead.
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
ARMS = ('hash', 'fast', 'fast1', 'fast8', 'full')
LABELS = {'hash': 'hashing both copies', 'fast': 'fast path, reads in the sending thread (EXP-64)',
          'fast1': 'fast path, 1 reader thread', 'fast8': 'fast path, 8 reader threads', 'full': 'pg_basebackup'}
EXP64 = {'fast': 164.8, 'hash': 180.1}
BOUND = 112.0  # EXP-64's overlapped bound: max(about 99 s of reads, about 107 s of writes and syncs) + about 5 s


def runs(results):
    out = []
    for meta_path in sorted(Path(results).glob('run-*/meta.json')):
        meta = json.loads(meta_path.read_text())
        send = meta_path.parent / 'send.json'
        stats = json.loads(send.read_text()) if send.exists() and send.stat().st_size else {}
        fast = stats.get('fast_path') or {}
        ahead = fast.get('read_ahead') or {}
        receiver = stats.get('receiver') or {}
        timings = stats.get('timings_seconds') or {}
        blocked = (stats.get('protocol_timers') or {}).get('page_send_s')
        out.append({**{k: meta[k] for k in ('run', 'pattern', 'arm', 'repeat', 'status')},
                    'receiver_status': meta.get('receiver_status'), 'seconds': round(meta['t1'] - meta['t0'], 3),
                    'match': (meta.get('verify') or {}).get('match'), 'fast_used': fast.get('used'),
                    'proof': ((receiver.get('fast_path') or {}).get('facts') or {}).get('proof'),
                    'threads': ahead.get('threads'), 'read_wait_s': ahead.get('wait_seconds'),
                    'pages_sent': (stats.get('pages') or {}).get('sent'),
                    # The sender's reading of the source: hash_source less the time blocked sending.
                    'source_s': timings.get('hash_source'), 'blocked_s': blocked,
                    'recv_wait_s': (receiver.get('protocol_timers') or {}).get('recv_wait_s'),
                    'rebuild_s': receiver.get('rebuild_seconds'), 'fsync_s': receiver.get('fsync_seconds'),
                    'guard_s': fast.get('guard_seconds')})
    return out


def median(rows, key):
    values = [r[key] for r in rows if r.get(key) is not None]
    return round(statistics.median(values), 2) if values else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp64b'))
    a = ap.parse_args(argv)
    rows = runs(a.results)
    for r in rows:
        print(r)
    cells = {}
    for arm in ARMS:
        mine = [r for r in rows if r['arm'] == arm and r['status'] == 0]
        cell = {'arm': arm, 'runs': len(mine), 'seconds': median(mine, 'seconds')}
        for key in ('pages_sent', 'source_s', 'blocked_s', 'read_wait_s', 'recv_wait_s', 'rebuild_s', 'fsync_s',
                    'guard_s'):
            cell[key] = median(mine, key)
        cells[arm] = cell
        print(cell)
    t = {arm: cells[arm]['seconds'] for arm in ARMS}
    ratio = lambda x, y: round(t[x] / t[y], 3) if t[x] and t[y] else None  # noqa: E731
    held = lambda v, bar: v is not None and v <= bar  # noqa: E731
    one, two = ratio('fast8', 'fast'), ratio('fast8', 'hash')
    near = [(name, v) for name, v, bar in (('fast8/fast', one, 0.80), ('fast8/hash', two, 0.75))
            if v is not None and abs(v - bar) <= 0.05]
    print(f"1 (deciding). {'HELD' if held(one, 0.80) else 'FAILED'}: fast8 / fast {one} (bar 0.80)"
          + (f'; within 0.05 of a bar: {near} (rule 11)' if near else ''))
    print(f"2. {'HELD' if held(two, 0.75) else 'FAILED'}: fast8 / hash {two} (bar 0.75); fast8 / full "
          f"{ratio('fast8', 'full')}, fast1 / full {ratio('fast1', 'full')} (reported)")
    wait = cells['fast8']['recv_wait_s']
    print(f"3. {'HELD' if held(wait, 30.0) else 'FAILED'}: fast8's receiver waited {wait} s for data (bar 30 s; "
          f"fast {cells['fast']['recv_wait_s']} s)")
    pair = ratio('fast1', 'fast8')
    print(f"4. {'HELD' if pair is not None and abs(pair - 1) <= 0.15 else 'FAILED'}: fast1 / fast8 {pair} "
          f"(within 0.15 of 1)")
    fast_rows = [r for r in rows if r['arm'].startswith('fast')]
    five = (rows and all(r['status'] == 0 for r in rows)
            and all(r['fast_used'] and r['match'] and r['proof'] == 'backup_manifest' for r in fast_rows))
    print(f"5. {'HELD' if five else 'FAILED'}: statuses {sorted({r['status'] for r in rows})}; fast runs "
          f"{[(r['arm'], r['fast_used'], r['proof'], r['match']) for r in fast_rows]}")
    six = (t['fast'] is not None and abs(t['fast'] / EXP64['fast'] - 1) <= 0.15
           and t['hash'] is not None and abs(t['hash'] / EXP64['hash'] - 1) <= 0.20)
    print(f"6. {'HELD' if six else 'FAILED'}: fast {t['fast']} against EXP-64's {EXP64['fast']} (15%), hash "
          f"{t['hash']} against {EXP64['hash']} (20%)")
    keys = list(next(iter(cells.values())))
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp64b-cells.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(cells.values())
    figure(cells)


def figure(cells):
    plotting.style()
    fig, (left, right) = plt.subplots(1, 2, figsize=(10.2, 4.3), gridspec_kw={'width_ratios': [1.25, 1]})
    colors = {'hash': COLORS['grey'], 'fast': COLORS['sky'], 'fast1': COLORS['blue'], 'fast8': COLORS['purple'],
              'full': COLORS['orange']}
    for i, arm in enumerate(ARMS):
        value = cells[arm]['seconds']
        if value is None:
            continue
        left.bar(i, value, 0.75, color=colors[arm])
        left.annotate(f'{value:.1f}', (i, value), textcoords='offset points', xytext=(0, 3), ha='center', fontsize=8.5)
    left.axhline(BOUND, color=COLORS['black'], lw=0.8, ls=':')
    left.text(len(ARMS) - 0.5, BOUND + 3, f'overlapped bound, about {BOUND:.0f} s', ha='right', fontsize=8)
    left.set_xticks(range(len(ARMS)), ['hashing', 'fast,\nno threads', 'fast,\n1 thread', 'fast,\n8 threads',
                                       'pg_base-\nbackup'], fontsize=8.5)
    left.set_ylabel('seconds to refresh about 18 GB (median)')
    left.set_title('Scattered changes (uniform 1%, 28% of pages)', fontsize=10)
    fast_arms = [arm for arm in ('fast', 'fast1', 'fast8') if cells[arm]['seconds'] is not None]
    parts = (('sender reading the source', COLORS['blue'], lambda c: (c['source_s'] or 0) - (c['blocked_s'] or 0)),
             ('sender blocked sending', COLORS['sky'], lambda c: c['blocked_s'] or 0),
             ('receiver waiting for data', COLORS['grey'], lambda c: c['recv_wait_s'] or 0))
    for j, (label, color, value) in enumerate(parts):
        right.barh([k * 4 + j for k in range(len(fast_arms))], [value(cells[arm]) for arm in fast_arms], 0.8,
                   color=color, label=label)
    right.set_yticks([k * 4 + 1 for k in range(len(fast_arms))],
                     [{'fast': 'no threads', 'fast1': '1 thread', 'fast8': '8 threads'}[arm] for arm in fast_arms])
    right.invert_yaxis()
    right.set_xlabel('seconds (median); reading and receiving overlap when they add up to more than the total')
    right.legend(fontsize=8, loc='lower right')
    right.set_title('Where the fast path\'s time went', fontsize=10)
    fig.suptitle('EXP-64b: overlapping the source\'s scattered reads with sending (8 GB VMs, balanced disks)',
                 fontsize=10.5)
    fig.tight_layout()
    print(plotting.save(fig, 'exp64b-read-ahead'))


if __name__ == '__main__':
    main()
