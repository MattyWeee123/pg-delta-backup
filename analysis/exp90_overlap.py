"""EXP-90, post hoc (not registered): which machine was busy in each second of each run, and dest's disk operations
(docs/experiments/EXP-90-where-hashing-time-goes.md, "Where the gap is").

    .venv/bin/python analysis/exp90_overlap.py [--results results/exp04/exp90/lab]
        [--profile results/exp04/exp90/profile] [--figures docs/figures]

D1 found the scattered hashing runs at 1.7-2.0x their slowest resource's time, while fewer than 4% of their seconds
had no resource at 0.8 of its capacity. That is what work done in turn looks like: some resource is always busy, but
not the same one. Two views, neither registered:
1. Each run's seconds (analysis/exp90.py's per-second uses, BUSY = 0.8), split into: the link busy (whatever the
   disks do); both disks busy; only the source's; only dest's; nothing busy. Dest's busy seconds are split again into
   reads and writes at once (reads over 20 MB/s and writes over 10 MB/s), reads only, or writes only.
2. Dest's disk as one budget of operations: if random reads and writes share it (as the 10-s windows of the uniform
   hashing run suggest), the floor on dest is every operation over the budget measured alone (fio's random writes),
   counting an operation over 256 KiB as one per 256 KiB. That unit fits fio's mixed point: 196 reads of 1 MiB a
   second take 784 units, and 784 + 3,408 random writes is the 4,197 alone. The view prints that floor beside the
   registered T_impl and the largest 10-s sum of units.
It changes no score: D1-D4 stand as analysis/exp90.py printed them.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

import exp90
import plotting
from plotting import COLORS, plt

CLASSES = ('link busy', 'both disks', 'source disk only', 'dest disk only', 'nothing busy')
SHADES = {'link busy': COLORS['black'], 'both disks': COLORS['green'], 'source disk only': COLORS['blue'],
          'dest disk only': COLORS['red'], 'nothing busy': '#cccccc'}
UNIT = 256 << 10


def split(r):
    p, w = r['per_second'], r['width']
    s, d, l = (np.array(p[k]) >= exp90.BUSY for k in ('source disk use', 'dest disk use', 'link use'))
    out = {'link busy': l, 'both disks': s & d & ~l, 'source disk only': s & ~d & ~l, 'dest disk only': d & ~s & ~l,
           'nothing busy': ~(s | d | l)}
    secs = {k: round(float(v.sum() * w), 1) for k, v in out.items()}
    rd, wr = np.array(p['dest read MB/s']) > 20, np.array(p['dest write MB/s']) > 10
    secs['dest busy: reads and writes at once'] = round(float((d & rd & wr).sum() * w), 1)
    secs['dest busy: reads only'] = round(float((d & rd & ~wr).sum() * w), 1)
    secs['dest busy: writes only'] = round(float((d & wr & ~rd).sum() * w), 1)
    return secs


def units(d):
    """Operations in UNITs: reads and writes, each the larger of its count and its bytes over 256 KiB."""
    return (np.maximum(d[..., exp90.READS], d[..., exp90.RSECT] * 512 / UNIT)
            + np.maximum(d[..., exp90.WRITES], d[..., exp90.WSECT] * 512 / UNIT))


def ops_view(folder, cap):
    meta = exp90.load(folder / 'meta.json')
    t0, t1 = meta['t0'], meta['t1']
    _, times, rows, _ = exp90.series(folder / 'util-dest.txt')
    edges = np.arange(t0, t1 + 1e-9, 1.0)
    per = np.diff(np.array([exp90.counters(times, rows, e) for e in edges]), axis=0)
    total = exp90.counters(times, rows, t1) - exp90.counters(times, rows, t0)
    starts = np.arange(t0, t1 - 10 + 1e-9, 0.5)
    tens = [units((exp90.counters(times, rows, s + 10) - exp90.counters(times, rows, s)) / 10) for s in starts]
    return {'read_ops': int(total[exp90.READS]), 'read_gb': round(total[exp90.RSECT] * 512 / 1e9, 2),
            'write_ops': int(total[exp90.WRITES]), 'write_gb': round(total[exp90.WSECT] * 512 / 1e9, 2),
            'floor_s': round(float(units(per).sum() / cap), 1), 'max_10s_units': round(float(max(tens, default=0)))}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(exp90.ROOT / 'results' / 'exp04' / 'exp90' / 'lab'))
    ap.add_argument('--profile', default=str(exp90.ROOT / 'results' / 'exp04' / 'exp90' / 'profile'))
    ap.add_argument('--figures', default=None)
    a = ap.parse_args(argv)
    if a.figures:
        plotting.FIGURES = Path(a.figures)
    profile = Path(a.profile)
    caps = {side: exp90.disk_profile(profile / f'fio-{side}') for side in ('source', 'dest')}
    links = {False: exp90.link_capacity(profile, None), True: exp90.link_capacity(profile, '1g')}
    database = exp90.load(Path(a.results) / 'source.json').get('database_bytes')
    folders = {}
    for folder in sorted(Path(a.results).glob('run-*')):
        meta = exp90.load(folder / 'meta.json')
        folders[(meta['pattern'], meta['arm'])] = folder
    order = [(p, arm) for p in ('hot', 'uniform') for arm in ('full', 'hash', 'hashw', 'full_1g', 'hash_1g', 'hashw_1g')]
    shown = []
    for key in order:
        if key not in folders:
            continue
        r = exp90.run_metrics(folders[key], caps, links, database)
        secs = split(r)
        shown.append((r, secs))
        print(f"{r['pattern']:7s} {r['arm']:9s} {r['seconds']:6.1f} s (T_impl {r['t_impl']} s): "
              + ', '.join(f'{k} {v}' for k, v in secs.items()))
        if r['arm'].split('_')[0] in ('hash', 'hashw'):
            v = ops_view(folders[key], caps['dest']['write_ops'])
            print(f"        dest: reads {v['read_gb']} GB in {v['read_ops']:,} ops, writes {v['write_gb']} GB in "
                  f"{v['write_ops']:,} ops; one shared budget of {caps['dest']['write_ops']:,.0f} units/s: floor "
                  f"{v['floor_s']} s (measured over it {r['seconds'] / v['floor_s']:.2f}); largest 10-s sum "
                  f"{v['max_10s_units']:,} units/s")

    plotting.style()
    fig, ax = plt.subplots(figsize=(9, 0.42 * len(shown) + 2.0))
    labels = []
    for i, (r, secs) in enumerate(shown):
        left = 0.0
        for c in CLASSES:
            ax.barh(i, secs[c], left=left, color=SHADES[c], label=c if i == 0 else None, height=0.7)
            left += secs[c]
        ax.plot([r['t_impl']] * 2, [i - 0.42, i + 0.42], color=COLORS['yellow'], lw=3)
        labels.append(f"{r['pattern']}, {exp90.LABELS.get(r['arm'].split('_')[0], r['arm'])}"
                      + (', 1 Gbit/s' if r['arm'].endswith('_1g') else ''))
    ax.plot([], [], color=COLORS['yellow'], lw=3, label='T_impl: the slowest resource\'s own time')
    ax.set_yticks(range(len(shown)), labels)
    ax.invert_yaxis()
    ax.set_xlabel('seconds of the run, grouped by what was busy (at 0.8 of its capacity)')
    ax.set_title('EXP-90: which machine was busy, second by second')
    ax.legend(loc='upper center', bbox_to_anchor=(0.4, -0.1), ncol=3, fontsize=8, frameon=False)
    print(plotting.save(fig, 'exp90-overlap'))


if __name__ == '__main__':
    main()
