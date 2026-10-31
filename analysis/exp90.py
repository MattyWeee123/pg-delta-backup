"""EXP-90's analysis: where the hashing path's time goes, on both VMs, over time
(docs/experiments/EXP-90-where-hashing-time-goes.md).

    .venv/bin/python analysis/exp90.py [--results results/exp04/exp90/lab] [--profile results/exp04/exp90/profile]
        [--mac docs/experiments/data/exp90-hashbench-mac.json] [--prefix exp90] [--figures docs/figures]

The session is experiments/baseline/run.sh (the team's baseline) with NAME=exp90 RESULTS=results/exp04/exp90.

The lab is experiments/network/lab_exp64.sh with UTIL=1 (util_series.py on both VMs every 0.1 s), arms full
(pg_basebackup), hash (pgdelta hashing both copies), hashw (the same, syncs on the receiver's worker) and fast8wf2
(the WAL-summary fast path with its defaults), each also as "_1g" on a link shaped to 1 Gbit/s, in the hot and uniform
patterns at scale 600. The profile, run before the lab: hashbench.py (blake2b, blake3, sha256) and fio_mixed.sh on
each VM, and iperf3 from the source to dest unshaped and at 1 Gbit/s, with the source's transmitted bytes.

Capacities, all measured in this session:
- each /data disk: read and write limits in bytes (1 MiB sequential, alone) and operations (8 KiB random, alone), and
  the mixed point (sequential reads and random writes at once) as fractions r_mix, w_mix of the reads and writes
  alone. A second's disk use is max(u_r, u_w, (u_r + u_w) / (r_mix + w_mix)), where u_r and u_w are the larger of
  bytes and operations against their limits: the sum of the two when reads and writes share one budget
  (r_mix + w_mix near 1), the larger of the two when they don't (near 2);
- the link: the source's transmitted bytes a second during iperf3, unshaped or at 1 Gbit/s, as the run;
- CPU: the VM's CPUs (busy jiffies), and the busiest single thread of pgdelta, pg_basebackup, walsenders and
  pgdelta's hashing workers (a core).

Per run, over its clock: the work each resource did, from the counters, over its capacity, is a lower bound on the
run's time for this implementation's own work (T_impl, the largest of: each disk, the link either way, each VM's
CPUs, the busiest thread). Measured over T_impl is how far the run is from keeping its slowest resource busy: the room
Ben's pipelining could take. For the hashing arms, T_alg puts one read of the database in place of the disks' reads
(the work the algorithm needs: every page of B and S read once, the changed pages written and sent). Scores:
D1 every hash run (hot, uniform; full link, 1 Gbit/s) has measured / T_impl <= 1.15 ("at its bound"; >= 1.30 is
   "a pipelining gap", between "partial").
D2 at 1 Gbit/s: pg_basebackup's mean link use >= 0.85; hash_1g over full_1g <= 0.70 in hot and >= 1.0 in uniform.
D3 no hashing run's T_impl is bound by CPU (a VM's CPUs or a thread); and on the VMs' CPU, sha256 over blake3 per
   8 KiB page (one process, median) <= 1.0.
D4 (the bound's control) both pg_basebackup runs at each rate and pattern have measured / T_impl <= 1.10.
Reported: hashw over hash; the disk model (r_mix, w_mix); each run's mean use per resource, its binding resource and
its slack (the share of its seconds in which no resource is at 0.8 of its capacity); full against EXP-89b's 91.2 s;
the Mac/VM pair of the sha256/blake3 ratio (rule 13).
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import numpy as np

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
ARMS = ('full', 'hash', 'hashw', 'fast8wf2')
LABELS = {'full': 'pg_basebackup', 'hash': 'pgdelta hashing', 'hashw': 'hashing, syncs on a worker',
          'fast8wf2': 'pgdelta fast path'}
EXP89B_FULL = 91.21  # EXP-89b's pg_basebackup, uniform (91.10 hot), for the cross-session control
BUSY = 0.8


def load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def series(path):
    """util_series.py's file: (header, S times, S counters as an array, {(pid, tid): (name, times, ticks)})."""
    head, times, rows, threads = {}, [], [], {}
    for line in Path(path).read_text().splitlines():
        if line.startswith('#'):
            head = dict(kv.split('=', 1) for kv in line[1:].split() if '=' in kv)
        elif line.startswith('S '):
            parts = line.split()
            times.append(float(parts[1]))
            rows.append([int(x) for x in parts[2:]])
        elif line.startswith('T '):
            _, t, pid, tid, ticks, name = line.split(maxsplit=5)
            entry = threads.setdefault((pid, tid), (name, [], []))
            entry[1].append(float(t))
            entry[2].append(int(ticks))
    width = min(len(r) for r in rows)
    return head, np.array(times), np.array([r[:width] for r in rows], dtype=float), threads


# Columns of an S row after the time: rx, tx, reads, sectors read, writes, sectors written, io_ticks, busy, all, ...
RX, TX, READS, RSECT, WRITES, WSECT, TICKS, BUSYJ, ALLJ = range(9)


def at(times, values, t):
    return np.interp(t, times, values)


def counters(times, rows, t):
    """Each counter column at time t, interpolated between samples."""
    return np.array([np.interp(t, times, rows[:, k]) for k in range(rows.shape[1])])


def disk_profile(folder):
    """A VM's measured disk limits and mixed point, from fio_mixed.sh's JSON files."""
    def side(name, job, kind):
        data = load(Path(folder) / f'{name}.json')
        for j in data.get('jobs', []):
            if j['jobname'] == job and j[kind]['io_bytes']:
                return j[kind]['bw_bytes'], j[kind]['iops']
        return None, None
    read_bw, _ = side('seqread-alone', 'seqread', 'read')
    _, read_ops = side('randread-alone', 'randread', 'read')
    write_bw, _ = side('seqwrite-alone', 'seqwrite', 'write')
    _, write_ops = side('randwrite-alone', 'randwrite', 'write')
    mix_r, _ = side('mixed', 'seqread', 'read')
    _, mix_w = side('mixed', 'randwrite', 'write')
    _, alone_w = side('randwrite-alone', 'randwrite', 'write')
    if None in (read_bw, read_ops, write_bw, write_ops, mix_r, mix_w, alone_w):
        return None
    return {'read_bw': read_bw, 'read_ops': read_ops, 'write_bw': write_bw, 'write_ops': write_ops,
            'r_mix': mix_r / read_bw, 'w_mix': mix_w / alone_w}


def disk_use(caps, rb, rops, wb, wops):
    """Seconds of a disk's capacity that this much work takes (or, per second, its use)."""
    u_r = max(rb / caps['read_bw'], rops / caps['read_ops'])
    u_w = max(wb / caps['write_bw'], wops / caps['write_ops'])
    return max(u_r, u_w, (u_r + u_w) / (caps['r_mix'] + caps['w_mix']))


def link_capacity(profile, rate):
    data = load(Path(profile) / f"iperf-{'1g' if rate else 'full'}.json")
    return data['tx_bytes'] / data['seconds'] if data.get('seconds') else None


def run_metrics(folder, caps, links, database_bytes):
    meta = load(folder / 'meta.json')
    send = load(folder / 'send.json')
    t0, t1 = meta['t0'], meta['t1']
    measured = t1 - t0
    sides = {}
    for side in ('source', 'dest'):
        head, times, rows, threads = series(folder / f'util-{side}.txt')
        hz, ncpu = float(head.get('hz', 100)), int(head.get('cpus', 1))
        delta = counters(times, rows, t1) - counters(times, rows, t0)
        thread_s = {key: (at(ts, tk, t1) - at(ts, tk, t0)) / hz for key, (name, ts, tk) in threads.items()
                    if len(ts) > 1}
        busiest = max(thread_s.items(), key=lambda kv: kv[1], default=(None, 0.0))
        sides[side] = {'times': times, 'rows': rows, 'threads': threads, 'hz': hz, 'ncpu': ncpu, 'delta': delta,
                       'busiest': (threads[busiest[0]][0] if busiest[0] else None, busiest[1])}
    link = links[meta.get('rate') is not None]
    bounds = {}
    for side in ('source', 'dest'):
        d = sides[side]['delta']
        bounds[f'{side} disk'] = disk_use(caps[side], d[RSECT] * 512, d[READS], d[WSECT] * 512, d[WRITES])
        bounds[f'{side} CPUs'] = d[BUSYJ] / sides[side]['hz'] / sides[side]['ncpu']
        bounds[f'{side} thread'] = sides[side]['busiest'][1]
    bounds['link'] = max(sides['source']['delta'][TX], sides['dest']['delta'][TX]) / link
    t_impl = max(bounds.values())
    out = {'run': meta['run'], 'pattern': meta['pattern'], 'arm': meta['arm'], 'rate': meta.get('rate'),
           'seconds': round(measured, 2), 'status': meta['status'],
           'match': (meta.get('verify') or {}).get('match'), 'bounds': {k: round(float(v), 2) for k, v in bounds.items()},
           't_impl': round(float(t_impl), 2), 'binding': max(bounds, key=bounds.get),
           'over_impl': round(measured / t_impl, 3),
           'busiest_thread': {s: sides[s]['busiest'][0] for s in sides}}
    # pgdelta's own timers: the sender's waits on the receiver (its round trips), its time in send(), and both
    # sides' phases. The phases overlap, so they don't add up to the run.
    timers = send.get('protocol_timers') or {}
    if timers:
        out['protocol'] = {'waits_s': round(sum(timers.get(k, 0) for k in ('accept_wait_s', 'basis_wait_s',
                                                                              'leaf_wait_s', 'complete_wait_s')), 3),
                           'page_send_s': timers.get('page_send_s')}
        receiver = send.get('receiver') or {}
        out['phases'] = {'hash_source_s': (send.get('timings_seconds') or {}).get('hash_source'),
                         **{k: receiver.get(k) for k in ('hash_basis_seconds', 'rebuild_seconds', 'fsync_seconds',
                                                         'fsync_wait_seconds', 'verify_seconds')}}
    if meta['arm'].split('_')[0] in ('hash', 'hashw'):
        d = sides['dest']['delta']
        sent = (send.get('pages') or {}).get('sent_bytes') or 0
        alg = {'source disk': disk_use(caps['source'], database_bytes, database_bytes / (1 << 20), 0, 0),
               'dest disk': disk_use(caps['dest'], database_bytes, database_bytes / (1 << 20), d[WSECT] * 512, d[WRITES]),
               'link': sent / link}
        out['t_alg'] = round(float(max(alg.values())), 2)
        out['over_alg'] = round(measured / out['t_alg'], 3)
    # Per second over the clock (per 0.2 s for runs under 20 s): each resource's use, and the slack (no resource at
    # BUSY of its capacity).
    width = 1.0 if measured >= 20 else 0.2
    edges = np.arange(t0, t1 + 1e-9, width)
    if len(edges) > 1:
        per = {}
        for side in ('source', 'dest'):
            s = sides[side]
            dc = np.diff(np.array([at(s['times'], s['rows'][:, k], edges) for k in range(s['rows'].shape[1])]), axis=1) / width
            per[f'{side} read MB/s'] = dc[RSECT] * 512 / 1e6
            per[f'{side} write MB/s'] = dc[WSECT] * 512 / 1e6
            per[f'{side} tx MB/s'] = dc[TX] / 1e6
            per[f'{side} disk use'] = np.array([disk_use(caps[side], dc[RSECT][i] * 512, dc[READS][i],
                                                         dc[WSECT][i] * 512, dc[WRITES][i]) for i in range(dc.shape[1])])
            per[f'{side} CPU use'] = dc[BUSYJ] / np.maximum(dc[ALLJ], 1)
            per[f'{side} thread use'] = np.max([np.diff(at(ts, tk, edges)) / s['hz'] / width for name, ts, tk in s['threads'].values()
                                                if len(ts) > 1] or [np.zeros(len(edges) - 1)], axis=0)
        per['link use'] = np.maximum(per['source tx MB/s'], per['dest tx MB/s']) * 1e6 / link
        uses = np.vstack([per['source disk use'], per['dest disk use'], per['link use'], per['source CPU use'],
                          per['dest CPU use'], per['source thread use'], per['dest thread use']])
        out['slack'] = round(float(np.mean(uses.max(axis=0) < BUSY)), 3)
        out['mean_use'] = {k: round(float(np.mean(per[k])), 3) for k in per if k.endswith('use')}
        out['per_second'] = {k: [round(float(x), 2) for x in v] for k, v in per.items()}
        out['width'] = width
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp90' / 'lab'))
    ap.add_argument('--profile', default=str(ROOT / 'results' / 'exp04' / 'exp90' / 'profile'))
    ap.add_argument('--mac', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp90-hashbench-mac.json'))
    ap.add_argument('--prefix', default='exp90')
    ap.add_argument('--figures', default=None, help='where the figures go (default: docs/figures)')
    a = ap.parse_args(argv)
    if a.figures:
        plotting.FIGURES = Path(a.figures)
    profile = Path(a.profile)
    caps = {side: disk_profile(profile / f'fio-{side}') for side in ('source', 'dest')}
    links = {False: link_capacity(profile, None), True: link_capacity(profile, '1g')}
    for side, c in caps.items():
        print(f"disk {side}: " + ('missing' if c is None else
              f"read {c['read_bw'] / 1e6:.1f} MB/s, {c['read_ops']:,.0f} ops/s; write {c['write_bw'] / 1e6:.1f} MB/s, "
              f"{c['write_ops']:,.0f} ops/s; mixed: reads at {c['r_mix']:.3f}, writes at {c['w_mix']:.3f} of alone "
              f"(sum {c['r_mix'] + c['w_mix']:.3f}: {'shared' if c['r_mix'] + c['w_mix'] <= 1.2 else 'independent' if min(c['r_mix'], c['w_mix']) >= 0.8 else 'partly shared'})"))
    print(f"link: full {links[False] and links[False] / 1e6:.1f} MB/s, shaped {links[True] and links[True] / 1e6:.1f} MB/s")
    database = load(Path(a.results) / 'source.json').get('database_bytes')
    rows = []
    for folder in sorted(Path(a.results).glob('run-*')):
        if (folder / 'util-dest.txt').exists() and None not in caps.values() and None not in links.values():
            rows.append(run_metrics(folder, caps, links, database))
    by = {(r['pattern'], r['arm']): r for r in rows}
    for r in rows:
        extra = f", over T_alg {r['over_alg']} (T_alg {r['t_alg']} s)" if 't_alg' in r else ''
        print(f"run {r['run']:2d} {r['pattern']:7s} {r['arm']:12s} {r['seconds']:7.2f} s, exit {r['status']}, match "
              f"{r['match']}: T_impl {r['t_impl']} s by {r['binding']}, over T_impl {r['over_impl']}{extra}; slack "
              f"{r.get('slack')}; busiest threads {r['busiest_thread']}")
        print(f"        bounds {r['bounds']}")
        print(f"        mean use {r.get('mean_use')}")
        if 'protocol' in r:
            print(f"        pgdelta: waits on the receiver {r['protocol']['waits_s']} s, in send() "
                  f"{r['protocol']['page_send_s']} s; phases {r['phases']}")

    hashing = [r for r in rows if r['arm'] in ('hash', 'hash_1g')]
    d1 = bool(hashing) and len(hashing) == 4 and all(r['over_impl'] <= 1.15 for r in hashing)
    verdict = lambda v: 'at its bound' if v <= 1.15 else 'a pipelining gap' if v >= 1.30 else 'partial'
    print(f"D1. {'HELD' if d1 else 'FAILED'}: " + '; '.join(
        f"{r['pattern']} {r['arm']} {r['over_impl']} ({verdict(r['over_impl'])})" for r in hashing))
    f1, h_hot, h_uni = by.get(('uniform', 'full_1g')), by.get(('hot', 'hash_1g')), by.get(('uniform', 'hash_1g'))
    f1h = by.get(('hot', 'full_1g'))
    link_uses = [r['mean_use']['link use'] for r in (f1h, f1) if r and 'mean_use' in r]
    ratio = lambda x, y: round(x['seconds'] / y['seconds'], 3) if x and y else None
    r_hot, r_uni = ratio(h_hot, f1h), ratio(h_uni, f1)
    d2 = len(link_uses) == 2 and min(link_uses) >= 0.85 and r_hot is not None and r_hot <= 0.70 \
        and r_uni is not None and r_uni >= 1.0
    print(f"D2. {'HELD' if d2 else 'FAILED'}: pg_basebackup's mean link use at 1 Gbit/s {link_uses} (bar 0.85); "
          f"hash_1g over full_1g: hot {r_hot} (bar 0.70), uniform {r_uni} (bar 1.0)")
    vm = load(profile / 'hashbench-dest.json')
    med = ((vm.get('by_procs') or {}).get('1') or {}).get('median') or {}
    sha = round(med['sha256'] / med['blake3'], 3) if {'sha256', 'blake3'} <= med.keys() else None
    cpu_bound = [r for r in hashing + [r for r in rows if r['arm'].startswith('hashw')]
                 if 'CPUs' in r['binding'] or 'thread' in r['binding']]
    d3 = not cpu_bound and sha is not None and sha <= 1.0 and bool(hashing)
    print(f"D3. {'HELD' if d3 else 'FAILED'}: hashing runs bound by CPU: {[(r['pattern'], r['arm']) for r in cpu_bound]}; "
          f"sha256 over blake3 per page on dest's CPU ({(vm.get('cpu') or {}).get('model')}): {sha} (bar 1.0)")
    fulls = [r for r in rows if r['arm'] in ('full', 'full_1g')]
    d4 = len(fulls) == 4 and all(r['over_impl'] <= 1.10 for r in fulls)
    print(f"D4 (control). {'HELD' if d4 else 'FAILED'}: pg_basebackup over T_impl " +
          ', '.join(f"{r['pattern']} {r['arm']} {r['over_impl']}" for r in fulls) + ' (bar 1.10)')
    print(f"correctness: {sum(1 for r in rows if r['match'] is True)} of "
          f"{sum(1 for r in rows if not r['arm'].startswith('full'))} pgdelta copies matched; exits "
          f"{sorted({r['status'] for r in rows})}")
    for pattern in ('hot', 'uniform'):
        for suffix in ('', '_1g'):
            h, w = by.get((pattern, 'hash' + suffix)), by.get((pattern, 'hashw' + suffix))
            if h and w:
                print(f"reported hashw over hash, {pattern}{suffix or ' full link'}: {ratio(w, h)}")
        full = by.get((pattern, 'full'))
        if full:
            print(f"reported cross-session control, {pattern} full: {full['seconds']} s against EXP-89b's "
                  f"{EXP89B_FULL if pattern == 'uniform' else 91.10} s")
    mac = load(a.mac)
    mmed = ((mac.get('by_procs') or {}).get('1') or {}).get('median') or {}
    if sha and {'sha256', 'blake3'} <= mmed.keys():
        print(f"reported proxy pair (rule 13): sha256 over blake3, Mac {mmed['sha256'] / mmed['blake3']:.3f}, VM {sha}")
    for side in ('dest', 'source'):
        hb = load(profile / f'hashbench-{side}.json')
        for procs, v in (hb.get('by_procs') or {}).items():
            print(f"reported hashbench {side} ({(hb.get('cpu') or {}).get('model')}, {(hb.get('cpu') or {}).get('flags')}), "
                  f"{procs} proc: pages/s per proc {v['median']}; in GB/s per proc "
                  f"{ {k: round(x * 8192 / 1e9, 2) for k, x in v['median'].items()} }")
    if rows:
        timelines(rows, f'{a.prefix}-timelines')
        bounds_figure(rows, f'{a.prefix}-bounds')
    out = Path(a.results) / 'exp90-scored.json'
    out.write_text(json.dumps([{k: v for k, v in r.items() if k != 'per_second'} for r in rows], indent=1))


def timelines(rows, name):
    plotting.style()
    cells = [(p, rate) for p in ('hot', 'uniform') for rate in (None, '1gbit')]
    arms = [arm for arm in ARMS if any(r['arm'].split('_')[0] == arm for r in rows)]
    fig, axes = plt.subplots(len(cells), len(arms), figsize=(3.6 * len(arms), 2.5 * len(cells)), squeeze=False)
    lines = (('source tx MB/s', 'link (source sends)', COLORS['black']),
             ('source read MB/s', 'source disk reads', COLORS['blue']),
             ('dest read MB/s', 'dest disk reads', COLORS['sky']),
             ('dest write MB/s', 'dest disk writes', COLORS['red']))
    for i, (pattern, rate) in enumerate(cells):
        for j, arm in enumerate(arms):
            ax = axes[i][j]
            r = next((r for r in rows if r['pattern'] == pattern and r['arm'].split('_')[0] == arm
                      and (r['rate'] == rate)), None)
            if not r or 'per_second' not in r:
                ax.set_visible(False)
                continue
            x = (np.arange(len(r['per_second']['source tx MB/s'])) + 0.5) * r['width']
            for key, label, color in lines:
                ax.plot(x, r['per_second'][key], color=color, lw=1.0, label=label)
            ax.set_ylim(0, 300)
            ax.set_xlim(0, max(x[-1] + r['width'] / 2, 1))
            ax.set_title(f"{LABELS[arm]}: {r['seconds']:.0f} s", fontsize=9)
            if j == 0:
                ax.set_ylabel(f"{pattern}, {'1 Gbit/s' if rate else 'full link'}\nMB/s", fontsize=9)
            if i == len(cells) - 1:
                ax.set_xlabel('seconds from the start', fontsize=9)
    handles, labels = next(ax for row in axes for ax in row if ax.get_visible()).get_legend_handles_labels()
    fig.legend(handles, labels, loc='upper center', ncol=4, fontsize=9, bbox_to_anchor=(0.5, 1.02))
    fig.suptitle('EXP-90: what each side does, second by second (18 GB, 8 GB VMs, balanced disks)', y=1.06, fontsize=11)
    print(plotting.save(fig, name))


def bounds_figure(rows, name):
    plotting.style()
    order = sorted(rows, key=lambda r: (r['pattern'], r['rate'] or '', ARMS.index(r['arm'].split('_')[0])))
    fig, ax = plt.subplots(figsize=(9, 0.42 * len(order) + 1.2))
    marks = {'source disk': ('o', COLORS['blue']), 'dest disk': ('s', COLORS['sky']), 'link': ('D', COLORS['black']),
             'source thread': ('^', COLORS['orange']), 'dest thread': ('v', COLORS['red'])}
    for y, r in enumerate(order):
        ax.barh(y, r['seconds'], 0.6, color='#dddddd')
        for key, (m, c) in marks.items():
            ax.plot(r['bounds'][key], y, m, color=c, ms=6, label=key if y == 0 else None)
        ax.annotate(f"{r['over_impl']:.2f}x", (r['seconds'], y), xytext=(4, -3), textcoords='offset points', fontsize=8)
    ax.set_yticks(range(len(order)), [f"{r['pattern']}, {LABELS[r['arm'].split('_')[0]]}"
                                      f"{', 1 Gbit/s' if r['rate'] else ''}" for r in order], fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel('seconds: the run (grey bar) and the time each resource needed for its work at full speed (marks)')
    ax.legend(fontsize=8, loc='lower right')
    ax.set_title('EXP-90: each run against its slowest resource (the label: run over its bound)', fontsize=10)
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
