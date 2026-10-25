"""EXP-26's analysis: BBR's fairness to cubic on a shared bottleneck (docs/experiments/EXP-26-bbr-fairness.md).

    .venv/bin/python analysis/exp26.py [--runs results/exp04/sets/fair/exp26] [--figdir DIR]

Per run, from the iperf3 servers' per-second intervals: the competitors' mean received rate over t = 40-100 s and
the newcomer's over the same seconds (its own t = 20-80); the congestion control each sender got; the source's tbf
drops; and the ping round trip meanwhile. A run counts only if both senders got what they asked for and each window
has at least 90% of its seconds. Per cell (round trip, queue depth, competitors): the median of each newcomer's
repeats, and the harm H = 1 - (competitors' rate beside a BBR newcomer) / (beside a cubic newcomer). The verdict
(registered): BBR becomes pgdelta's default only if H <= 0.20 in every cell; also reported at 0.10 and 0.40, and an
H within 20% of 0.20 (0.16-0.24) in a cell that decides it is provisional. Writes docs/experiments/data/exp26-cells.csv
and docs/figures/exp26-fairness.svg.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from pathlib import Path

import numpy as np

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
LINK_MBIT = 2000
BAR, MARGIN = 0.20, 0.20
WINDOWS = {'comp': (40, 100), 'new': (20, 80)}  # the registered timing; a run's meta.json gives its own
MIN_SHARE = 0.9  # of the window's seconds


def window_mbit(path, which, meta):
    """Mean received Mbit/s over the window, the seconds it rests on, and the congestion control the sender got."""
    j = json.loads(Path(path).read_text())
    lo, hi = meta.get(f'{which}_window') or WINDOWS[which]
    rates = [x['sum']['bits_per_second'] for x in j.get('server_output_json', {}).get('intervals', [])
             if lo <= x['sum']['start'] < hi]
    end = j.get('end', {})
    return (statistics.mean(rates) / 1e6 if rates else None, len(rates), end.get('sender_tcp_congestion'),
            end.get('sum_sent', {}).get('retransmits'))


def tbf_drops(path):
    try:
        tree = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return sum(q.get('drops', 0) for q in tree if q.get('kind') == 'tbf')


def ping_ms(path):
    times = [float(t) for t in re.findall(r'time=([\d.]+) ms', Path(path).read_text(errors='replace'))]
    return statistics.median(times) if times else None


def load(root):
    runs = []
    for d in sorted(Path(root).glob('run-*')):
        meta = json.loads((d / 'meta.json').read_text())
        try:
            comp, comp_s, comp_cc, comp_rx = window_mbit(d / 'comp.json', 'comp', meta)
            new, new_s, new_cc, new_rx = window_mbit(d / 'new.json', 'new', meta)
        except (OSError, ValueError, KeyError) as exc:
            runs.append({**meta, 'ok': False, 'why': f'unreadable: {exc}'})
            continue
        why = []
        if comp_cc != 'cubic' or new_cc != meta['newcomer']:
            why.append(f'congestion control {comp_cc}/{new_cc}')
        lo, hi = meta.get('comp_window') or WINDOWS['comp']
        if min(comp_s, new_s) < MIN_SHARE * (hi - lo):
            why.append(f'windows of {comp_s} and {new_s} s')
        before, after = tbf_drops(d / 'qdisc-before.json'), tbf_drops(d / 'qdisc-after.json')
        runs.append({**meta, 'ok': not why, 'why': '; '.join(why), 'comp_mbit': comp, 'new_mbit': new,
                     'retransmits_comp': comp_rx, 'retransmits_new': new_rx, 'ping_ms': ping_ms(d / 'ping.txt'),
                     'tbf_drops': None if before is None or after is None else after - before})
    return runs


def cells(runs):
    out = {}
    for r in runs:
        if r['ok']:
            out.setdefault((r['rtt_ms'], r['depth_rtt'], r['competitors']), {}).setdefault(r['newcomer'], []).append(r)
    rows = []
    for (rtt, depth, n), arms in sorted(out.items()):
        row = {'rtt_ms': rtt, 'depth_rtt': depth, 'competitors': n}
        for cc in ('cubic', 'bbr'):
            rs = arms.get(cc, [])
            row[f'runs_{cc}'] = len(rs)
            for key in ('comp_mbit', 'new_mbit', 'ping_ms'):
                vals = [r[key] for r in rs if r[key] is not None]
                row[f'{key}_{cc}'] = round(statistics.median(vals), 1) if vals else None
        if row['comp_mbit_cubic'] and row['comp_mbit_bbr'] is not None:
            row['harm'] = round(1 - row['comp_mbit_bbr'] / row['comp_mbit_cubic'], 3)
        else:
            row['harm'] = None
        rows.append(row)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--runs', default=str(ROOT / 'results' / 'exp04' / 'sets' / 'fair' / 'exp26'))
    ap.add_argument('--figdir')
    ap.add_argument('--tag', default='')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    runs = load(a.runs)
    bad = [r for r in runs if not r['ok']]
    print(f'{len(runs)} runs, {len(runs) - len(bad)} counted' + (f'; not counted: {[(r["run"], r["why"]) for r in bad]}'
                                                               if bad else ''))
    rows = cells(runs)
    print('rtt depth N | competitors beside cubic / beside BBR (Mbit/s) | newcomer cubic / BBR | ping cubic / BBR | H')
    for r in rows:
        print(f"{r['rtt_ms']:4d} {r['depth_rtt']:5g} {r['competitors']:2d} | {r['comp_mbit_cubic']} / {r['comp_mbit_bbr']}"
              f" | {r['new_mbit_cubic']} / {r['new_mbit_bbr']} | {r['ping_ms_cubic']} / {r['ping_ms_bbr']} | {r['harm']}"
              f" (runs {r['runs_cubic']}/{r['runs_bbr']})")
    harms = [r['harm'] for r in rows]
    for bar in (BAR / 2, BAR, BAR * 2):
        over = [r for r in rows if r['harm'] is None or r['harm'] > bar]
        print(f'  bar {bar:.2f}: ' + ('BBR as the default (H at or below the bar in every cell)' if not over else
              f'BBR stays opt-in; above the bar: {[(r["rtt_ms"], r["depth_rtt"], r["competitors"]) for r in over]}'))
    near = [r for r in rows if r['harm'] is not None and abs(r['harm'] / BAR - 1) < MARGIN]
    print(f'  cells within 20% of the bar (provisional where they decide): '
          f'{[(r["rtt_ms"], r["depth_rtt"], r["competitors"], r["harm"]) for r in near]}')
    data = ROOT / 'docs' / 'experiments' / 'data' / f'exp26-cells{a.tag}.csv'
    with open(data, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(data)

    fig, (left, right) = plt.subplots(1, 2, figsize=(11.5, 4.4), gridspec_kw={'width_ratios': [1, 1.2]})
    left.axhspan(BAR * (1 - MARGIN), BAR * (1 + MARGIN), color=COLORS['grey'], alpha=0.15, lw=0)
    left.axhline(BAR, color=COLORS['black'], lw=0.9, ls='--', label='registered bar (0.20)')
    left.axhline(0, color=COLORS['black'], lw=0.6)
    for n, color in ((1, COLORS['blue']), (4, COLORS['orange'])):
        pts = sorted((r['depth_rtt'], r['harm']) for r in rows if r['rtt_ms'] == 60 and r['competitors'] == n
                     and r['harm'] is not None)
        if pts:
            left.plot(*zip(*pts), marker='o', color=color, label=f'60 ms, {n} cubic competitor{"s" if n > 1 else ""}')
    for rtt, marker in ((20, 's'), (120, '^')):
        pts = [(r['depth_rtt'], r['harm']) for r in rows if r['rtt_ms'] == rtt and r['harm'] is not None]
        if pts:
            left.scatter(*zip(*pts), marker=marker, color=COLORS['green'] if rtt == 20 else COLORS['purple'],
                         zorder=3, label=f'{rtt} ms, 1 competitor')
    left.set_xscale('log', base=2)
    left.set_xticks([0.5, 1, 2, 4], ['0.5', '1', '2', '4'])
    left.set_xlabel('bottleneck queue (round trips of the link)')
    left.set_ylabel('harm H (above 0: BBR takes more than cubic would)')
    left.set_title('What a BBR newcomer takes from cubic traffic, against a cubic newcomer', fontsize=10)
    left.legend(fontsize=7.5)
    labels, x = [], np.arange(len(rows))
    for r in rows:
        labels.append(f"{r['rtt_ms']} ms\n{r['depth_rtt']:g} RTT\n{r['competitors']} comp.")
    share = {cc: [(r[f'new_mbit_{cc}'] or 0) / ((r[f'new_mbit_{cc}'] or 0) + (r[f'comp_mbit_{cc}'] or 0) or 1)
                  for r in rows] for cc in ('cubic', 'bbr')}
    right.bar(x - 0.2, share['cubic'], 0.4, color=COLORS['grey'], label='cubic newcomer')
    right.bar(x + 0.2, share['bbr'], 0.4, color=COLORS['red'], label='BBR newcomer')
    right.scatter(x, [1 / (r['competitors'] + 1) for r in rows], marker='_', s=300, color=COLORS['black'],
                  zorder=3, label='equal share, 1/(N+1)')
    right.set_xticks(x, labels, fontsize=7)
    right.set_ylabel("newcomer's share of what got through")
    right.set_ylim(0, 1)
    right.set_title('The newcomer\'s share, per cell (medians of the repeats)', fontsize=10)
    right.legend(fontsize=7.5, loc='upper right')
    fig.tight_layout()
    print(plotting.save(fig, f'exp26-fairness{a.tag}'))


if __name__ == '__main__':
    main()
