"""EXP-89b's analysis: a crashed old primary after a failover, 18 GB on the VMs, refreshed with --crashed-primary
against pg_rewind (crash recovery first), hashing and a re-clone (docs/experiments/EXP-89b-crashed-primary-vms.md).

    .venv/bin/python analysis/exp89b.py [--results results/exp04/exp89b] [--figure exp89b-crashed-primary]

The lab is experiments/network/lab_exp67.sh with CRASHED_P=1 (the old primary's dirty buffers written out, then
pg_ctl -m immediate at the fork) and ARMS="rewind fast crashed full", two runs each, in the hot10 and uniform lags.
Times run from the start of the refresh to a copy that accepts connections (pg_rewind's: replayed to S's LSN). Scores:
1 (deciding) every crashed run took the fast path with the proof "crashed primary", listed blocks of B's own past
  the fork, matched S's state and passed pg_amcheck --heapallindexed;
2 hot10: crashed at most 0.5x pg_rewind's time and at most 0.5x hashing's (the fast arm, refused); uniform: crashed
  faster than the re-clone (full);
3 every fast run was refused (no --crashed-primary) and every copy of every arm matched S.
Times are each arm's mean over its runs. Reported: each arm over the re-clone; the crashed arm's blocks of B's own,
pages sent and its WAL-end proof's seconds; pg_rewind's time to rewound and to replayed.
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
ARMS = ('rewind', 'fast', 'crashed', 'full')
LABELS = {'rewind': 'pg_rewind (crash recovery first)', 'fast': 'pgdelta today (refused, hashes)',
          'crashed': 'pgdelta --crashed-primary', 'full': 'pg_basebackup (re-clone)'}


def load(path):
    try:
        return json.loads(Path(path).read_text()) if Path(path).stat().st_size else {}
    except (OSError, ValueError):
        return {}


def runs(results):
    out = []
    for meta_path in sorted(Path(results).glob('run-*/meta.json')):
        meta, folder = json.loads(meta_path.read_text()), meta_path.parent
        send, receive = load(folder / 'send.json'), load(folder / 'receive.json')
        fast = send.get('fast_path') or {}
        received = (send.get('receiver') or {}).get('fast_path') or receive.get('fast_path') or {}
        amcheck = (folder / 'amcheck-status').read_text().strip() if (folder / 'amcheck-status').exists() else None
        out.append({'run': meta['run'], 'lag': meta['lag'], 'arm': meta['arm'], 'status': meta['status'],
                    'seconds': round(meta['t1'] - meta['t0'], 3), 'match': meta['match'],
                    'rewound_s': round(meta['t_rewound'] - meta['t0'], 3) if meta.get('t_rewound') else None,
                    'fast_used': fast.get('used'), 'proof': (received.get('facts') or {}).get('proof'),
                    'diverged_blocks': (fast.get('diverged_basis') or {}).get('blocks'),
                    'wal_end_s': ((received.get('facts') or {}).get('crashed_wal') or {}).get('seconds'),
                    'pages_sent': (send.get('pages') or {}).get('sent'), 'amcheck': amcheck,
                    'refused': (received.get('refused') or '')[:120]})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp89b'))
    ap.add_argument('--figure', default='exp89b-crashed-primary')
    a = ap.parse_args(argv)
    rows = runs(a.results)
    mean = {(lag, arm): statistics.fmean(r['seconds'] for r in rows if r['lag'] == lag and r['arm'] == arm)
            for lag in ('hot10', 'uniform') for arm in ARMS if any(r['lag'] == lag and r['arm'] == arm for r in rows)}
    crashed = [r for r in rows if r['arm'] == 'crashed']
    ok = [r for r in crashed if r['status'] == 0 and r['fast_used'] and r['proof'] == 'crashed primary'
          and (r['diverged_blocks'] or 0) > 0 and r['match'] and r['amcheck'] == '0']
    one = bool(crashed) and len(ok) == len(crashed)
    print(f"1 (deciding). {'HELD' if one else 'FAILED'}: {len(ok)} of {len(crashed)} crashed runs took the fast path by "
          f"the proof, found B's own blocks, matched S and passed pg_amcheck")
    for r in crashed:
        if r not in ok:
            print(f"   not: {r}")
    ratio = lambda lag, a_, b_: mean[(lag, a_)] / mean[(lag, b_)] if (lag, a_) in mean and (lag, b_) in mean else None
    hot_rewind, hot_fast, uni_full = ratio('hot10', 'crashed', 'rewind'), ratio('hot10', 'crashed', 'fast'), \
        ratio('uniform', 'crashed', 'full')
    two = all(v is not None for v in (hot_rewind, hot_fast, uni_full)) and hot_rewind <= 0.5 and hot_fast <= 0.5 \
        and uni_full < 1.0
    fmt = lambda v: None if v is None else round(v, 3)
    print(f"2. {'HELD' if two else 'FAILED'}: hot10 crashed over pg_rewind {fmt(hot_rewind)} (bar 0.5), over hashing "
          f"{fmt(hot_fast)} (bar 0.5); uniform crashed over the re-clone {fmt(uni_full)} (bar 1.0)")
    fast = [r for r in rows if r['arm'] == 'fast']
    three = bool(rows) and all(not r['fast_used'] for r in fast) and all(r['match'] and r['status'] == 0 for r in rows)
    print(f"3. {'HELD' if three else 'FAILED'}: fast refused in {sum(not r['fast_used'] for r in fast)} of {len(fast)}; "
          f"{sum(bool(r['match']) and r['status'] == 0 for r in rows)} of {len(rows)} copies matched")
    for (lag, arm), v in sorted(mean.items()):
        full = mean.get((lag, 'full'))
        print(f"reported {lag} {arm}: {v:.2f} s" + (f" ({v / full:.3f}x the re-clone)" if full else ""))
    for r in crashed:
        print(f"reported crashed run {r['run']} ({r['lag']}): {r['seconds']} s, B's own blocks {r['diverged_blocks']}, "
              f"pages sent {r['pages_sent']}, WAL-end proof {r['wal_end_s']} s")
    for r in rows:
        if r['arm'] == 'rewind':
            print(f"reported pg_rewind run {r['run']} ({r['lag']}): rewound at {r['rewound_s']} s, replayed at {r['seconds']} s")
    if mean:
        figure(mean, a.figure)


def figure(mean, name):
    plotting.style()
    fig, ax = plt.subplots(figsize=(10, 4.2))
    colors = {'rewind': COLORS['orange'], 'fast': COLORS['grey'], 'crashed': COLORS['blue'], 'full': COLORS['purple']}
    for i, lag in enumerate(('hot10', 'uniform')):
        for j, arm in enumerate(ARMS):
            if (lag, arm) in mean:
                x = i * 5 + j
                ax.bar(x, mean[(lag, arm)], 0.8, color=colors[arm], label=LABELS[arm] if i == 0 else None)
                ax.annotate(f"{mean[(lag, arm)]:.1f} s", (x, mean[(lag, arm)]), textcoords='offset points',
                            xytext=(0, 3), ha='center', fontsize=8)
    ax.set_xticks([1.5, 6.5], ['hot rows, updated 10 times', '1% of rows, scattered'], fontsize=9)
    ax.set_ylabel('seconds to a usable copy (mean of 2 runs)')
    ax.set_title('EXP-89b: an 18 GB primary that crashed after a failover, brought current', fontsize=10)
    ax.set_ylim(0, max(mean.values()) * 1.3)
    ax.legend(fontsize=7.5, loc='upper left', ncol=2)
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
