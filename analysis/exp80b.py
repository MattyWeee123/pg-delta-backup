"""EXP-80b's analysis: a crashed standby refreshed by the fast path (--crashed-standby) beyond memory, against today's
path for it (the fast path refused, so hashing) and a full copy (docs/experiments/EXP-80b-crashed-standby-timed.md).

    .venv/bin/python analysis/exp80b.py [--results results/exp04/exp80b]

Cells: per pattern and arm, the median of the runs (exp68.runs). Arms: fast8wf2c (--crashed-standby), fast8wf2
(today: refused, hashed), full (pg_basebackup). Scores 1 (deciding: uniform's fast8wf2c at most 0.6 of fast8wf2's
time), 2 (hot's at most 0.2), 3 (fast8wf2c at most 0.8 of the full copy in both cells) and 4 (every run exits 0;
every fast8wf2c run took the fast path with the proof "crashed standby" and every fast8wf2 run was refused as "in
archive recovery"; every pgdelta copy passed the copy's check). Writes docs/experiments/data/exp80b-cells.csv and the
figure exp80b-crashed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import exp68
import exp76
import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
CELLS = ('hot', 'uniform')
ARMS = ('fast8wf2c', 'fast8wf2', 'full')
BARS = {'uniform': 0.6, 'hot': 0.2}
FULL_BAR = 0.8


def receivers(results):
    """{run: (the receiver's fast-path proof or None, its refusal or None)} from send.json."""
    out = {}
    for meta_path in sorted(Path(results).glob('run-*/meta.json')):
        meta = json.loads(meta_path.read_text())
        send = meta_path.parent / 'send.json'
        if meta['arm'] == 'full' or not send.exists() or not send.stat().st_size:
            continue
        received = (json.loads(send.read_text()).get('receiver') or {}).get('fast_path') or {}
        out[meta['run']] = ((received.get('facts') or {}).get('proof'), received.get('refused'))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp80b'))
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp80b-cells.csv'))
    ap.add_argument('--figure', default='exp80b-crashed')
    a = ap.parse_args(argv)
    rows, proofs = exp68.runs(a.results), receivers(a.results)
    cells = []
    for pattern in CELLS:
        for arm in ARMS:
            mine = [r for r in rows if r['pattern'] == pattern and r['arm'] == arm]
            if mine:
                cells.append({'pattern': pattern, 'arm': arm, 'runs': len(mine),
                              'seconds': exp68.median(mine, 'seconds'), 'pages_sent': exp68.median(mine, 'pages_sent'),
                              'source_io_s': exp68.median(mine, 'source_io_s'),
                              'dest_io_s': exp68.median(mine, 'dest_io_s')})
                print(cells[-1])
    by = {(c['pattern'], c['arm']): c for c in cells}
    ratio = {p: round(by[(p, 'fast8wf2c')]['seconds'] / by[(p, 'fast8wf2')]['seconds'], 3) for p in CELLS
             if (p, 'fast8wf2c') in by and (p, 'fast8wf2') in by}
    one = ratio.get('uniform') is not None and ratio['uniform'] <= BARS['uniform']
    print(f"1 (deciding). {'HELD' if one else 'FAILED'}: uniform, --crashed-standby / hashing {ratio.get('uniform')} "
          f"(bar {BARS['uniform']})" + ('; within 0.05 of the bar (rule 11)' if ratio.get('uniform') is not None
                                        and abs(ratio['uniform'] - BARS['uniform']) <= 0.05 else ''))
    two = ratio.get('hot') is not None and ratio['hot'] <= BARS['hot']
    print(f"2. {'HELD' if two else 'FAILED'}: hot, --crashed-standby / hashing {ratio.get('hot')} (bar {BARS['hot']})")
    full = {p: round(by[(p, 'fast8wf2c')]['seconds'] / by[(p, 'full')]['seconds'], 3) for p in CELLS
            if (p, 'fast8wf2c') in by and (p, 'full') in by}
    three = len(full) == len(CELLS) and all(v <= FULL_BAR for v in full.values())
    print(f"3. {'HELD' if three else 'FAILED'}: --crashed-standby / full copy {full} (bar {FULL_BAR})")
    fast = [r for r in rows if r['arm'] != 'full']
    four = (bool(rows) and all(r['status'] == 0 for r in rows) and all(r['receiver_status'] == 0 for r in fast)
            and all(r['match'] is True for r in fast)
            and all(proofs.get(r['run'], (None, None))[0] == 'crashed standby' and r['fast_used']
                    for r in fast if r['arm'] == 'fast8wf2c')
            and all(not r['fast_used'] and 'in archive recovery' in (proofs.get(r['run'], (None, ''))[1] or '')
                    for r in fast if r['arm'] == 'fast8wf2'))
    print(f"4. {'HELD' if four else 'FAILED'}: statuses {sorted({r['status'] for r in rows})}; copies matched "
          f"{all(r['match'] is True for r in fast)}; proofs {sorted({str(v[0]) for v in proofs.values()})}")
    exp76.write_csv(a.out, cells)
    figure(by, ratio, a.figure)


def figure(by, ratio, name):
    plotting.style()
    fig, ax = plt.subplots(figsize=(7.6, 4.0))
    colors = {'fast8wf2c': COLORS['blue'], 'fast8wf2': COLORS['grey'], 'full': COLORS['orange']}
    labels = {'fast8wf2c': 'fast path, --crashed-standby', 'fast8wf2': 'today: refused, hashed',
              'full': 'pg_basebackup'}
    for i, pattern in enumerate(CELLS):
        for j, arm in enumerate(ARMS):
            c = by.get((pattern, arm))
            if not c:
                continue
            x = i * 4 + j
            ax.bar(x, c['seconds'], 0.8, color=colors[arm], label=labels[arm] if i == 0 else None)
            ax.annotate(f"{c['seconds']:.1f} s", (x, c['seconds']), textcoords='offset points', xytext=(0, 3),
                        ha='center', fontsize=8)
    ax.set_xticks([i * 4 + 1 for i in range(len(CELLS))],
                  [f"{p}\n(--crashed-standby / hashing: {ratio.get(p)})" for p in CELLS], fontsize=9)
    ax.set_ylabel('seconds to refresh 18 GB (median of 2)')
    ax.legend(fontsize=8, loc='upper left')
    ax.set_title('EXP-80b: a crashed standby beyond memory', fontsize=10)
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
