"""EXP-91's analysis: hashing ahead on the sender and verifying as written on the receiver, at 18 GB on the VMs
(docs/experiments/EXP-91-hash-ahead-verify-as-written.md).

    .venv/bin/python analysis/exp91.py [--results results/exp04/exp91/lab] [--profile results/exp04/exp91/profile]
        [--local docs/experiments/data/exp91-local.csv] [--prefix exp91] [--figures docs/figures]

The session is experiments/baseline/run.sh with NAME=exp91, the arms full, hash, hashA (--hash-ahead 64G
--read-ahead 8), hashV (--verify-scope as-written), hashAV (both) and hashAVw (both, syncs on the worker), each also
on a link shaped to 1 Gbit/s ("_1g"), in the hot and uniform patterns, one run each. Each run's bound (T_impl), use
and slack are EXP-90's (analysis/exp90.py, with this session's measured capacities); which machine was busy and
dest's operations are EXP-90's post-hoc view (analysis/exp90_overlap.py), now registered.

Scores:
E1 (deciding) uniform, full link: hashAV <= 0.70x hash.
E2 uniform, 1 Gbit/s: hashAV_1g <= 0.92x full_1g (hashing beats a re-clone where many pages change).
E3 uniform, full link: hashA and hashV each <= 0.92x hash, and hashAV below both.
E4 (no harm, and the bound's control) hot: every new arm within 1.03x hash at its rate; every pg_basebackup run
   <= 1.10x its T_impl.
E5 (the mechanism) uniform hashAV, full link: at most 10 s with only the source's disk busy (EXP-90: 28), at most
   0.5 GB of dest's reads beyond the database (EXP-90: 4.86 GB), and measured / T_impl <= 1.30 (EXP-90: 1.70).
Correctness: every pgdelta copy matched its source.
Reported: hashAVw over hashAV; each run's bound, binding resource, slack and busy-time split; the sender's planner and
read-ahead counters and the receiver's read-back counters; the cross-session controls (full and hash against
EXP-90's); the local scale model's hashAV/hash beside the VMs' (rule 13's proxy pair).
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

import numpy as np

import exp90
import exp90_overlap
import plotting
from plotting import COLORS, plt

ROOT = exp90.ROOT
ARMS = ('full', 'hash', 'hashA', 'hashV', 'hashAV', 'hashAVw')
LABELS = {'full': 'pg_basebackup', 'hash': 'hashing (as EXP-90)', 'hashA': 'A: hashing ahead',
          'hashV': 'V: verified as written', 'hashAV': 'A and V', 'hashAVw': 'A and V, syncs on a worker'}
NEW = ('hashA', 'hashV', 'hashAV', 'hashAVw')
EXP90 = {('hot', 'full'): 90.51, ('uniform', 'full'): 90.80, ('hot', 'hash'): 87.66, ('uniform', 'hash'): 186.93,
         ('uniform', 'hash_1g'): 209.92, ('uniform', 'full_1g'): 153.35}
EXP90_SOURCE_ONLY, EXP90_EXTRA_READ_GB, EXP90_OVER_IMPL = 28.0, 4.86, 1.695


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp91' / 'lab'))
    ap.add_argument('--profile', default=str(ROOT / 'results' / 'exp04' / 'exp91' / 'profile'))
    ap.add_argument('--local', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp91-local.csv'))
    ap.add_argument('--prefix', default='exp91')
    ap.add_argument('--figures', default=None, help='where the figures go (default: docs/figures)')
    a = ap.parse_args(argv)
    if a.figures:
        plotting.FIGURES = Path(a.figures)
    profile = Path(a.profile)
    caps = {side: exp90.disk_profile(profile / f'fio-{side}') for side in ('source', 'dest')}
    links = {False: exp90.link_capacity(profile, None), True: exp90.link_capacity(profile, '1g')}
    for side, c in caps.items():
        print(f"disk {side}: " + ('missing' if c is None else
              f"read {c['read_bw'] / 1e6:.1f} MB/s, {c['read_ops']:,.0f} ops/s; write {c['write_bw'] / 1e6:.1f} MB/s, "
              f"{c['write_ops']:,.0f} ops/s; mixed: reads at {c['r_mix']:.3f}, writes at {c['w_mix']:.3f} of alone"))
    print(f"link: full {links[False] and links[False] / 1e6:.1f} MB/s, shaped {links[True] and links[True] / 1e6:.1f} MB/s")
    database = exp90.load(Path(a.results) / 'source.json').get('database_bytes')
    rows = []
    for folder in sorted(Path(a.results).glob('run-*')):
        if not (folder / 'util-dest.txt').exists() or None in caps.values() or None in links.values():
            continue
        if exp90.load(folder / 'meta.json')['arm'].split('_')[0] not in ARMS:  # another session's arms (a dry run)
            continue
        r = exp90.run_metrics(folder, caps, links, database)
        r.update(busy=exp90_overlap.split(r), ops=exp90_overlap.ops_view(folder, caps['dest']['write_ops']))
        r['extra_read_gb'] = round(r['ops']['read_gb'] - (database or 0) / 1e9, 2)
        send = exp90.load(folder / 'send.json')
        r['hash_ahead'] = send.get('hash_ahead')
        r['verify_written'] = (send.get('receiver') or {}).get('verify_written')
        rows.append(r)
    by = {(r['pattern'], r['arm']): r for r in rows}
    for r in rows:
        print(f"run {r['run']:2d} {r['pattern']:7s} {r['arm']:11s} {r['seconds']:7.2f} s, exit {r['status']}, match "
              f"{r['match']}: T_impl {r['t_impl']} s by {r['binding']}, over T_impl {r['over_impl']}; slack "
              f"{r.get('slack')}; dest reads beyond the database {r['extra_read_gb']} GB")
        print(f"        busy {r['busy']}")
        if 'protocol' in r:
            print(f"        pgdelta: waits on the receiver {r['protocol']['waits_s']} s, in send() "
                  f"{r['protocol']['page_send_s']} s; phases {r['phases']}")
        if r['hash_ahead'] or r['verify_written']:
            print(f"        hash_ahead {r['hash_ahead']}; verify_written {r['verify_written']}")

    ratio = lambda x, y: round(x['seconds'] / y['seconds'], 3) if x and y else None
    u = lambda arm: by.get(('uniform', arm))
    e1 = ratio(u('hashAV'), u('hash'))
    print(f"E1 (deciding). {'HELD' if e1 is not None and e1 <= 0.70 else 'FAILED'}: uniform hashAV over hash {e1} "
          f"(bar 0.70)")
    e2 = ratio(u('hashAV_1g'), u('full_1g'))
    print(f"E2. {'HELD' if e2 is not None and e2 <= 0.92 else 'FAILED'}: uniform hashAV_1g over full_1g {e2} (bar 0.92)")
    a_, v_ = ratio(u('hashA'), u('hash')), ratio(u('hashV'), u('hash'))
    e3 = None not in (a_, v_, e1) and a_ <= 0.92 and v_ <= 0.92 and e1 < min(a_, v_)
    print(f"E3. {'HELD' if e3 else 'FAILED'}: uniform hashA over hash {a_}, hashV over hash {v_} (bars 0.92), "
          f"hashAV {e1} below both")
    hot = {(arm, rate): ratio(by.get(('hot', arm + rate)), by.get(('hot', 'hash' + rate)))
           for arm in NEW for rate in ('', '_1g')}
    fulls = [r for r in rows if r['arm'].split('_')[0] == 'full']
    e4 = (all(v is not None and v <= 1.03 for v in hot.values()) and len(fulls) == 4
          and all(r['over_impl'] <= 1.10 for r in fulls))
    print(f"E4 (no harm; the bound's control). {'HELD' if e4 else 'FAILED'}: hot, over hash at its rate "
          f"{ {f'{k[0]}{k[1]}': v for k, v in hot.items()} } (bar 1.03); pg_basebackup over T_impl "
          f"{[(r['pattern'], r['arm'], float(r['over_impl'])) for r in fulls]} (bar 1.10)")
    av = u('hashAV')
    if av:
        alone, extra, over = av['busy']['source disk only'], av['extra_read_gb'], av['over_impl']
        e5 = alone <= 10 and extra <= 0.5 and over <= 1.30
        print(f"E5 (mechanism). {'HELD' if e5 else 'FAILED'}: uniform hashAV: source disk alone {alone} s (bar 10; "
              f"EXP-90 {EXP90_SOURCE_ONLY}), dest reads beyond the database {extra} GB (bar 0.5; EXP-90 "
              f"{EXP90_EXTRA_READ_GB}), over T_impl {over} (bar 1.30; EXP-90 {EXP90_OVER_IMPL})")
    copies = [r for r in rows if r['arm'].split('_')[0] != 'full']
    print(f"correctness: {sum(1 for r in copies if r['match'] is True)} of {len(copies)} pgdelta copies matched; "
          f"exits {sorted({r['status'] for r in rows})}")
    for pattern in ('hot', 'uniform'):
        for rate in ('', '_1g'):
            w, av_ = by.get((pattern, 'hashAVw' + rate)), by.get((pattern, 'hashAV' + rate))
            print(f"reported hashAVw over hashAV, {pattern}{rate or ' full link'}: {ratio(w, av_)}")
    for (pattern, arm), seconds in EXP90.items():
        r = by.get((pattern, arm))
        if r:
            print(f"reported cross-session, {pattern} {arm}: {r['seconds']} s against EXP-90's {seconds} s "
                  f"({r['seconds'] / seconds - 1:+.1%})")
    local = Path(a.local)
    if local.exists():
        runs = list(csv.DictReader(open(local)))
        for cell in sorted({r['cell'] for r in runs}):
            med = {arm: statistics.median(float(r['seconds']) for r in runs if r['cell'] == cell and r['arm'] == arm)
                   for arm in sorted({r['arm'] for r in runs if r['cell'] == cell})}
            if {'hash', 'hashAV'} <= med.keys():
                print(f"reported proxy pair (rule 13), {cell}: local hashAV over hash {med['hashAV'] / med['hash']:.3f}"
                      f" (medians {med}); VM uniform {e1}")
    if rows:
        timelines(rows, f'{a.prefix}-timelines')
        busy_figure(rows, f'{a.prefix}-busy')
    out = Path(a.results) / 'exp91-scored.json'
    out.write_text(json.dumps([{k: v for k, v in r.items() if k != 'per_second'} for r in rows], indent=1))


def timelines(rows, name):
    """Each arm in the uniform pattern, at both rates, second by second."""
    plotting.style()
    rates = (None, '1gbit')
    arms = [arm for arm in ARMS if any(r['arm'].split('_')[0] == arm for r in rows)]
    fig, axes = plt.subplots(len(rates), len(arms), figsize=(3.4 * len(arms), 2.7 * len(rates)), squeeze=False)
    lines = (('source tx MB/s', 'link (source sends)', COLORS['black']),
             ('source read MB/s', 'source disk reads', COLORS['blue']),
             ('dest read MB/s', 'dest disk reads', COLORS['sky']),
             ('dest write MB/s', 'dest disk writes', COLORS['red']))
    longest = max((r['seconds'] for r in rows if r['pattern'] == 'uniform'), default=1)
    for i, rate in enumerate(rates):
        for j, arm in enumerate(arms):
            ax = axes[i][j]
            r = next((r for r in rows if r['pattern'] == 'uniform' and r['arm'].split('_')[0] == arm
                      and r['rate'] == rate), None)
            if not r or 'per_second' not in r:
                ax.set_visible(False)
                continue
            x = (np.arange(len(r['per_second']['source tx MB/s'])) + 0.5) * r['width']
            for key, label, color in lines:
                ax.plot(x, r['per_second'][key], color=color, lw=1.0, label=label)
            ax.set_ylim(0, 300)
            ax.set_xlim(0, longest)
            ax.set_title(f"{LABELS[arm]}: {r['seconds']:.0f} s", fontsize=9)
            if j == 0:
                ax.set_ylabel(f"{'1 Gbit/s' if rate else 'full link'}\nMB/s", fontsize=9)
            if i == len(rates) - 1:
                ax.set_xlabel('seconds from the start', fontsize=9)
    handles, labels = next(ax for row in axes for ax in row if ax.get_visible()).get_legend_handles_labels()
    fig.legend(handles, labels, loc='upper center', ncol=4, fontsize=9, bbox_to_anchor=(0.5, 1.04))
    fig.suptitle('EXP-91: 28% of pages changed, second by second (18 GB, 8 GB VMs, balanced disks)', y=1.1,
                 fontsize=11)
    print(plotting.save(fig, name))


def busy_figure(rows, name):
    """Each run's seconds by what was busy, with its bound (T_impl)."""
    plotting.style()
    order = sorted(rows, key=lambda r: (r['pattern'], r['rate'] or '', ARMS.index(r['arm'].split('_')[0])))
    fig, ax = plt.subplots(figsize=(9, 0.36 * len(order) + 2.0))
    for y, r in enumerate(order):
        left = 0.0
        for c in exp90_overlap.CLASSES:
            ax.barh(y, r['busy'][c], left=left, color=exp90_overlap.SHADES[c], label=c if y == 0 else None, height=0.7)
            left += r['busy'][c]
        ax.plot([r['t_impl']] * 2, [y - 0.42, y + 0.42], color=COLORS['yellow'], lw=3)
    ax.plot([], [], color=COLORS['yellow'], lw=3, label='T_impl: the slowest resource\'s own time')
    ax.set_yticks(range(len(order)), [f"{r['pattern']}, {LABELS[r['arm'].split('_')[0]]}"
                                      f"{', 1 Gbit/s' if r['rate'] else ''}" for r in order], fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel('seconds of the run, grouped by what was busy (at 0.8 of its capacity)')
    ax.set_title('EXP-91: which machine was busy, second by second')
    ax.legend(loc='upper center', bbox_to_anchor=(0.4, -0.08), ncol=3, fontsize=8, frameon=False)
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
