"""EXP-92's analysis: confirming hashing with EXP-91's two changes and the receiver's syncs on its worker (hashAVw), and
the hashing window held to the source's cache (hashA2GVw), at 18 GB on the VMs
(docs/experiments/EXP-92-confirm-syncs-on-worker.md).

    .venv/bin/python analysis/exp92.py [--results results/exp04/exp92/lab] [--profile results/exp04/exp92/profile]
        [--local docs/experiments/data/exp92-local.csv] [--prefix exp92] [--figures docs/figures]

The session is experiments/baseline/run.sh with NAME=exp92 REPEATS=2: in uniform the arms full, hash, hashAVw
(--hash-ahead 64G --read-ahead 8, --verify-scope as-written, --fsync worker) and hashA2GVw (the same with
--hash-ahead 2G), each also on a link shaped to 1 Gbit/s ("_1g"); in hot (ARMS_hot) the same four at full link. Each
run's bound (T_impl), use and slack are EXP-90's (analysis/exp90.py, with this session's measured capacities); which
machine was busy is EXP-90's post-hoc view (analysis/exp90_overlap.py), as EXP-91 used it. An arm's time in a cell is
the mean of its runs (two), and so is each of its counters.

Scores:
A cell is valid when it has its runs (two), each exited 0 on both sides, every pgdelta copy matched its source, and
each ran with its arm's options as the sender's stats record them (the window, the receiver's sync mode and verify
scope). A prediction that uses a cell that isn't valid is VOID, neither held nor failed.
F1 (deciding) uniform, 1 Gbit/s: hashAVw_1g <= 0.85x full_1g, the mean and each run against full_1g's mean.
F2 uniform, full link: hashAVw <= 0.70x hash.
F3 (the window) uniform, at each rate: hashA2GVw's source-disk bound <= 1.04x hash's, its sending waits on the
   pages' reads <= 10 s in all (EXP-91's hashAVw: 39.8 and 52.1 s), and hashA2GVw no slower than hashAVw beyond the
   noise (<= 1.0223x). The window for a held-out session: 2G if it is faster beyond the noise (<= 0.9777x) at one rate
   and no slower beyond it at the other; otherwise 64G.
F4 (no harm, and the bound's control) hot: hashAVw and hashA2GVw each <= 1.03x hash; every pg_basebackup run <= 1.10x
   its T_impl.
F5 (the mechanism) uniform hashAVw, full link: the receiver's main loop waits on its syncs <= 10 s, the source's disk
   busy alone <= 5 s, and measured / T_impl <= 1.20 (EXP-91: 5.5 s, 0 s, 1.130).
Correctness: every cell valid: 24 runs, 18 pgdelta copies matched.
Reported: each run, its bound and busy split; the two runs' spread in each cell against rule 17's bar; the sender's
planner and read-ahead counters; the cross-session values against EXP-91's (full within 1%); the local scale model's
ratios beside the VMs' (rule 13's proxy pairs).
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
ARMS = ('full', 'hash', 'hashAVw', 'hashA2GVw')
LABELS = {'full': 'pg_basebackup', 'hash': 'hashing (as EXP-90)', 'hashAVw': 'A, V, syncs on a worker (64 GiB ahead)',
          'hashA2GVw': 'the same, 2 GiB ahead'}
NEW = ('hashAVw', 'hashA2GVw')
EXP91 = {('hot', 'full'): 90.44, ('hot', 'hash'): 87.72, ('hot', 'hashAVw'): 87.82, ('uniform', 'full'): 90.76,
         ('uniform', 'hash'): 185.19, ('uniform', 'hashAVw'): 111.23, ('uniform', 'full_1g'): 151.80,
         ('uniform', 'hash_1g'): 220.80, ('uniform', 'hashAVw_1g'): 113.01}
EXP91_SYNC_WAIT, EXP91_SOURCE_ONLY, EXP91_OVER_IMPL = 5.5, 0.0, 1.130
SPREAD_BAR = 0.0223  # rule 17: two runs of 10 s or more in one session differ by up to 2.23% (EXP-85's noise floor)
EXPECTED = ({('uniform', arm + rate) for arm in ARMS for rate in ('', '_1g')} | {('hot', arm) for arm in ARMS})
OPTIONS = {'hash': (None, 'main', 'written'), 'hashAVw': (64 << 30, 'worker', 'as-written'),
           'hashA2GVw': (2 << 30, 'worker', 'as-written')}  # the window in bytes, the sync mode, the verify scope


def load_rows(results, caps, links):
    database = exp90.load(Path(results) / 'source.json').get('database_bytes')
    rows = []
    for folder in sorted(Path(results).glob('run-*')):
        if not (folder / 'util-dest.txt').exists():
            continue
        meta = exp90.load(folder / 'meta.json')
        if meta['arm'].split('_')[0] not in ARMS:  # another session's arms (a dry run)
            continue
        r = exp90.run_metrics(folder, caps, links, database)
        r.update(repeat=meta.get('repeat'), receiver_status=meta.get('receiver_status'), busy=exp90_overlap.split(r),
                 ops=exp90_overlap.ops_view(folder, caps['dest']['write_ops']))
        r['extra_read_gb'] = round(r['ops']['read_gb'] - (database or 0) / 1e9, 2)
        send = exp90.load(folder / 'send.json')
        receiver = send.get('receiver') or {}
        r['hash_ahead'] = send.get('hash_ahead')
        r['verify_written'] = receiver.get('verify_written')
        r['options'] = ((r['hash_ahead'] or {}).get('bytes'), receiver.get('fsync'), receiver.get('verify_scope'))
        rows.append(r)
    return rows


def problems(cell, key, repeats):
    """Why a cell isn't valid (an empty list if it is)."""
    runs, arm = cell.get(key, []), key[1].split('_')[0]
    out = [] if len(runs) == repeats else [f'{len(runs)} of {repeats} runs']
    for r in runs:
        if r['status'] != 0 or (arm != 'full' and r['receiver_status'] != 0):
            out.append(f"run {r['run']} exited {r['status']}/{r['receiver_status']}")
        if arm != 'full' and r['match'] is not True:
            out.append(f"run {r['run']}'s copy matched {r['match']}")
        if arm in OPTIONS and r['options'] != OPTIONS[arm]:
            out.append(f"run {r['run']} ran with {r['options']}, not {OPTIONS[arm]}")
    return out


def mean(values):
    values = [v for v in values if v is not None]
    return round(statistics.fmean(values), 3) if values else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp92' / 'lab'))
    ap.add_argument('--profile', default=str(ROOT / 'results' / 'exp04' / 'exp92' / 'profile'))
    ap.add_argument('--local', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp92-local.csv'))
    ap.add_argument('--prefix', default='exp92')
    ap.add_argument('--figures', default=None, help='where the figures go (default: docs/figures)')
    ap.add_argument('--repeats', type=int, default=2, help='runs registered per arm and cell')
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
    rows = [] if None in caps.values() or None in links.values() else load_rows(a.results, caps, links)
    for r in rows:
        print(f"run {r['run']:2d} {r['pattern']:7s} {r['arm']:13s} repeat {r['repeat']}: {r['seconds']:7.2f} s, exit "
              f"{r['status']}, match {r['match']}: T_impl {r['t_impl']} s by {r['binding']}, over T_impl "
              f"{r['over_impl']}; dest reads beyond the database {r['extra_read_gb']} GB")
        print(f"        bounds {r['bounds']}")
        print(f"        busy {r['busy']}")
        if 'protocol' in r:
            print(f"        pgdelta: waits on the receiver {r['protocol']['waits_s']} s, in send() "
                  f"{r['protocol']['page_send_s']} s; phases {r['phases']}")
        if r['hash_ahead'] or r['verify_written']:
            print(f"        hash_ahead {r['hash_ahead']}; verify_written {r['verify_written']}")

    cell = {}
    for r in rows:
        cell.setdefault((r['pattern'], r['arm']), []).append(r)
    invalid = {key: why for key in sorted(EXPECTED) if (why := problems(cell, key, a.repeats))}
    for key, why in invalid.items():
        print(f"invalid cell {key[0]} {key[1]}: {'; '.join(why)}")
    t = lambda pattern, arm: mean(r['seconds'] for r in cell.get((pattern, arm), []))
    ratio = lambda x, y: round(x / y, 3) if x and y else None
    of = lambda pattern, arm, f: mean(f(r) for r in cell.get((pattern, arm), []))

    def verdict(held, *keys):
        void = [f'{p} {arm}' for p, arm in keys if (p, arm) in invalid]
        return f"VOID ({', '.join(void)} not valid)" if void else ('HELD' if held else 'FAILED')

    f1 = ratio(t('uniform', 'hashAVw_1g'), t('uniform', 'full_1g'))
    each = [ratio(r['seconds'], t('uniform', 'full_1g')) for r in cell.get(('uniform', 'hashAVw_1g'), [])]
    held = f1 is not None and f1 <= 0.85 and bool(each) and all(e <= 0.85 for e in each)
    print(f"F1 (deciding). {verdict(held, ('uniform', 'hashAVw_1g'), ('uniform', 'full_1g'))}: uniform hashAVw_1g "
          f"over full_1g {f1}, each run {each} (bar 0.85 for the mean and each run; EXP-91 0.744)")
    if f1 is not None and abs(f1 - 0.85) <= 0.03:
        print('        within 0.03 of its bar: a second session is owed (rule 17)')
    f2 = ratio(t('uniform', 'hashAVw'), t('uniform', 'hash'))
    print(f"F2. {verdict(f2 is not None and f2 <= 0.70, ('uniform', 'hashAVw'), ('uniform', 'hash'))}: uniform "
          f"hashAVw over hash {f2} (bar 0.70; EXP-91 0.601)")
    f3, parts, thans, keys = True, [], [], []
    source = lambda r: r['bounds']['source disk']
    waits = lambda r: (r['hash_ahead'] or {}).get('read_ahead_wait_seconds')
    for rate in ('', '_1g'):
        bound, base = of('uniform', 'hashA2GVw' + rate, source), of('uniform', 'hash' + rate, source)
        reads = of('uniform', 'hashA2GVw' + rate, waits)
        than = ratio(t('uniform', 'hashA2GVw' + rate), t('uniform', 'hashAVw' + rate))
        thans.append(than)
        keys += [('uniform', arm + rate) for arm in ('hash', 'hashAVw', 'hashA2GVw')]
        f3 = (f3 and None not in (bound, base, reads, than) and bound <= 1.04 * base and reads <= 10
              and than <= 1 + SPREAD_BAR)
        parts.append(f"{rate or 'full link'}: the source disk's bound {bound} s (bar 1.04x hash's {base} s; hashAVw "
                     f"{of('uniform', 'hashAVw' + rate, source)}), the sending's wait on its reads {reads} s (bar 10; "
                     f"hashAVw {of('uniform', 'hashAVw' + rate, waits)}), over hashAVw {than} (bar {1 + SPREAD_BAR})")
    print(f"F3 (the window). {verdict(f3, *keys)}: uniform hashA2GVw, " + '; '.join(parts))
    window = ('2G' if None not in thans and min(thans) <= 1 - SPREAD_BAR and max(thans) <= 1 + SPREAD_BAR
              else '64G')
    print(f"        the window for a held-out session: {window} (2G only if faster beyond the noise at one rate and "
          f"no slower beyond it at the other)")
    hot = {arm: ratio(t('hot', arm), t('hot', 'hash')) for arm in NEW}
    fulls = [r for r in rows if r['arm'].split('_')[0] == 'full']
    f4 = (all(v is not None and v <= 1.03 for v in hot.values()) and len(fulls) == 3 * a.repeats
          and all(r['over_impl'] <= 1.10 for r in fulls))
    keys = [('hot', arm) for arm in ARMS] + [('uniform', 'full'), ('uniform', 'full_1g')]
    print(f"F4 (no harm; the bound's control). {verdict(f4, *keys)}: hot, over hash {hot} (bar 1.03); pg_basebackup "
          f"over T_impl {[(r['pattern'], r['arm'], float(r['over_impl'])) for r in fulls]} (bar 1.10)")
    wait = of('uniform', 'hashAVw', lambda r: (r.get('phases') or {}).get('fsync_wait_seconds'))
    alone = of('uniform', 'hashAVw', lambda r: r['busy']['source disk only'])
    over = of('uniform', 'hashAVw', lambda r: r['over_impl'])
    f5 = None not in (wait, alone, over) and wait <= 10 and alone <= 5 and over <= 1.20
    print(f"F5 (mechanism). {verdict(f5, ('uniform', 'hashAVw'))}: uniform hashAVw: the main loop's wait on syncs "
          f"{wait} s (bar 10; EXP-91 {EXP91_SYNC_WAIT}), the source's disk alone {alone} s (bar 5; EXP-91 "
          f"{EXP91_SOURCE_ONLY}), over T_impl {over} (bar 1.20; EXP-91 {EXP91_OVER_IMPL})")
    copies = [r for r in rows if r['arm'].split('_')[0] != 'full']
    matched = sum(1 for r in copies if r['match'] is True)
    counted = sum(len(cell.get(key, [])) for key in EXPECTED)
    print(f"correctness. {'HELD' if not invalid and counted == len(EXPECTED) * a.repeats else 'FAILED'}: "
          f"{counted} of {len(EXPECTED) * a.repeats} runs, {matched} of {len(copies)} pgdelta copies matched; "
          f"invalid cells: {len(invalid) or 'none'}")

    for (pattern, arm), runs in sorted(cell.items()):
        s = [r['seconds'] for r in runs]
        spread = (max(s) - min(s)) / min(s) if len(s) > 1 else None
        flag = ' (over the bar: check for interference, rule 17)' if spread is not None and spread > SPREAD_BAR else ''
        print(f"reported spread, {pattern} {arm}: {s}, {spread if spread is None else f'{spread:.2%}'}{flag}")
    for (pattern, arm), seconds in EXP91.items():
        now = t(pattern, arm)
        if now:
            note = ''
            if arm.split('_')[0] == 'full':
                note = (' (the control, within 1%)' if abs(now / seconds - 1) <= 0.01 else
                        ' (the control, OUTSIDE 1%: no comparison across sessions in this cell, rule 17)')
            print(f"reported cross-session, {pattern} {arm}: {now} s against EXP-91's {seconds} s "
                  f"({now / seconds - 1:+.1%}){note}")
    for pattern in ('hot', 'uniform'):
        for arm in (n + rate for n in NEW for rate in ('', '_1g')):
            runs = [r['hash_ahead'] for r in cell.get((pattern, arm), []) if r['hash_ahead']]
            if runs:
                print(f"reported planner, {pattern} {arm}: most ahead "
                      f"{[round(h['most_ahead_bytes'] / 1e9, 2) for h in runs]} GB, waited for room "
                      f"{[round(h['room_wait_seconds'], 1) for h in runs]} s, for B's digests "
                      f"{[round(h['plan_wait_seconds'], 1) for h in runs]} s; the sending waited for its reads "
                      f"{[round(h['read_ahead_wait_seconds'], 1) for h in runs]} s")
    local = Path(a.local)
    if local.exists():
        runs = list(csv.DictReader(open(local)))
        for name in sorted({r['cell'] for r in runs}):
            med = {arm: statistics.median(float(r['seconds']) for r in runs if r['cell'] == name and r['arm'] == arm)
                   for arm in sorted({r['arm'] for r in runs if r['cell'] == name})}
            if {'hash', 'hashAVw', 'hashA2GVw'} <= med.keys():
                print(f"reported proxy pairs (rule 13), {name}: local hashAVw over hash "
                      f"{med['hashAVw'] / med['hash']:.3f} (VM uniform {f2}); local hashA2GVw over hashAVw "
                      f"{med['hashA2GVw'] / med['hashAVw']:.3f} (VM uniform "
                      f"{ratio(t('uniform', 'hashA2GVw'), t('uniform', 'hashAVw'))}); medians {med}")
    if rows:
        timelines(rows, f'{a.prefix}-timelines')
        runs_figure(rows, f'{a.prefix}-runs')
    out = Path(a.results) / 'exp92-scored.json'
    out.write_text(json.dumps([{k: v for k, v in r.items() if k != 'per_second'} for r in rows], indent=1))


def timelines(rows, name):
    """Each arm in the uniform pattern, at both rates, second by second: the first run of each."""
    plotting.style()
    rates = (None, '1gbit')
    arms = [arm for arm in ARMS if any(r['arm'].split('_')[0] == arm for r in rows)]
    fig, axes = plt.subplots(len(rates), len(arms), figsize=(3.6 * len(arms), 2.7 * len(rates)), squeeze=False)
    lines = (('source tx MB/s', 'link (source sends)', COLORS['black']),
             ('source read MB/s', 'source disk reads', COLORS['blue']),
             ('dest read MB/s', 'dest disk reads', COLORS['sky']),
             ('dest write MB/s', 'dest disk writes', COLORS['red']))
    longest = max((r['seconds'] for r in rows if r['pattern'] == 'uniform'), default=1)
    for i, rate in enumerate(rates):
        for j, arm in enumerate(arms):
            ax = axes[i][j]
            r = min((r for r in rows if r['pattern'] == 'uniform' and r['arm'].split('_')[0] == arm
                     and r['rate'] == rate), key=lambda r: (r['repeat'] or 0, r['run']), default=None)
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
    fig.suptitle('EXP-92: 28% of pages changed, second by second, each arm\'s first run (18 GB, 8 GB VMs, balanced '
                 'disks)', y=1.1, fontsize=11)
    print(plotting.save(fig, name))


def runs_figure(rows, name):
    """Every run's seconds by what was busy, with its bound (T_impl); an arm's runs side by side."""
    plotting.style()
    order = sorted(rows, key=lambda r: (r['pattern'], r['rate'] or '', ARMS.index(r['arm'].split('_')[0]),
                                        r['repeat'] or 0))
    fig, ax = plt.subplots(figsize=(9, 0.36 * len(order) + 2.0))
    for y, r in enumerate(order):
        left = 0.0
        for c in exp90_overlap.CLASSES:
            ax.barh(y, r['busy'][c], left=left, color=exp90_overlap.SHADES[c], label=c if y == 0 else None, height=0.7)
            left += r['busy'][c]
        ax.plot([r['t_impl']] * 2, [y - 0.42, y + 0.42], color=COLORS['yellow'], lw=3)
    ax.plot([], [], color=COLORS['yellow'], lw=3, label='T_impl: the slowest resource\'s own time')
    ax.set_yticks(range(len(order)), [f"{r['pattern']}, {LABELS[r['arm'].split('_')[0]]}"
                                      f"{', 1 Gbit/s' if r['rate'] else ''}, run {(r['repeat'] or 0) + 1}"
                                      for r in order], fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel('seconds of the run, grouped by what was busy (at 0.8 of its capacity)')
    ax.set_title('EXP-92: every run, by which machine was busy')
    ax.legend(loc='upper center', bbox_to_anchor=(0.4, -0.08), ncol=3, fontsize=8, frameon=False)
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
