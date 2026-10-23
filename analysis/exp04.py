"""EXP-04 (two VMs, throttled link) figures, curated tables and the numbers behind each verdict.

    .venv/bin/python analysis/exp04.py [SUMMARY_CSV] [--figdir DIR] [--datadir DIR] [--tag NAME]
SUMMARY_CSV defaults to results/exp04/full/summary.csv (from `remote.py fetch full`).
Writes docs/experiments/data/exp04-runs.csv (every run, selected columns) and exp04-cells.csv
(medians per workload, link speed and method), figures docs/figures/exp04-*.svg, and prints
the crossover table and the N1/N2/N3/N5/N6/N7 checks.

Headline time is `total_s` (end to end, as pre-registered: rsync's copy of B0 and pgdelta's
receiver start-up overlap the capture of S; see the decision notes in the EXP-04 write-up).
Link speed on the x axis is the measured source->destination rate, not the setting.
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import plotting
from plotting import COLORS, METHOD_COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = ['uniform', 'hot', 'append']
METHODS = [('full', 'Full copy (pg_basebackup)', METHOD_COLORS['full'], '-', 'o'),
           ('incremental', 'PG17 incremental + combine', METHOD_COLORS['incremental'], '-', 's'),
           ('rsync_default', 'rsync, default blocks', METHOD_COLORS['rsync'], '-', 'v'),
           ('rsync_8k', 'rsync, 8 KiB blocks', METHOD_COLORS['rsync_8k'], '-', '^'),
           ('pgdelta_1thread', 'pgdelta, 1 thread (EXP-02 engine)', COLORS['sky'], '--', 'D'),
           ('pgdelta', 'pgdelta, 4 processes (EXP-06 engine)', METHOD_COLORS['pg_delta'], '-', 'o')]
DELTA = [m for m, *_ in METHODS if m != 'full']
KEEP = ['trial_id', 'pattern', 'fraction', 'scale', 'repeat', 'speed', 'target_gbit', 'measured_s2d_gbit',
        'measured_d2s_gbit', 'method', 'order_index', 'status', 'total_s', 'total_no_ssh_s', 'inplace_total_s',
        'serial_total_s', 'prep_s', 'capture_s', 'prep_capture_s', 'receiver_start_s', 'receiver_capture_s',
        'transfer_s', 'combine_s', 'ssh_overhead_s', 'prefetch_bytes', 'prefetch_s', 'prefetch_untimed_s',
        'hash_basis_s', 'wire_s2d_bytes', 'wire_d2s_bytes', 'tool_s2d_bytes', 'tool_d2s_bytes', 'output_bytes',
        'wal_bytes', 'cpu_source_s', 'cpu_dest_s', 'peak_rss_source_kb', 'peak_rss_dest_kb', 'verified',
        'identical_to_s']


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load(path):
    rows = list(csv.DictReader(open(path)))
    bad = [r for r in rows if r['status'] != 'pass' or r['verified'] != 'True']
    if bad:
        print(f'WARNING: {len(bad)} runs not passing/verified; they are left out', file=sys.stderr)
    return [r for r in rows if r['status'] == 'pass' and r['verified'] == 'True'], len(rows)


def cells(rows):
    """{(pattern, speed): {'speed_gbit': measured, method: {col: median}}}"""
    groups = defaultdict(list)
    for r in rows:
        groups[(r['pattern'], r['speed'], r['method'])].append(r)
    out = defaultdict(dict)
    for (pattern, speed, method), group in groups.items():
        entry = {'n': len(group)}
        for col in ('total_s', 'serial_total_s', 'inplace_total_s', 'transfer_s', 'capture_s', 'prep_capture_s',
                    'receiver_capture_s', 'combine_s', 'wire_s2d_bytes', 'wire_d2s_bytes', 'tool_s2d_bytes',
                    'cpu_source_s', 'cpu_dest_s', 'measured_s2d_gbit', 'measured_d2s_gbit', 'prefetch_untimed_s'):
            values = [num(r[col]) for r in group if num(r.get(col)) is not None]
            entry[col] = statistics.median(values) if values else None
            if col == 'total_s' and values:
                entry['total_min'], entry['total_max'] = min(values), max(values)
        out[(pattern, speed)][method] = entry
    for key, by_method in out.items():
        speeds = [e['measured_s2d_gbit'] for e in by_method.values() if e['measured_s2d_gbit']]
        by_method['speed_gbit'] = statistics.median(speeds)
    return out


def series(table, pattern, method, col='total_s'):
    points = []
    for (p, speed), by_method in table.items():
        if p == pattern and method in by_method and by_method[method].get(col) is not None:
            points.append((by_method['speed_gbit'], by_method[method][col], by_method[method], speed))
    return sorted(points)


def crossover(table, pattern, method, col='total_s'):
    """Measured link speed (Gbit/s) where method/full crosses 1, interpolated in log-log; or a bound."""
    pts = []
    for speed_gbit, value, _, speed in series(table, pattern, method, col):
        full = table[(pattern, speed)].get('full', {}).get('total_s')
        if full:
            pts.append((speed_gbit, value / full))
    if not pts:
        return None, 'no data'
    if all(r < 1 for _, r in pts):
        return pts[-1][0], f'faster at every speed (up to {pts[-1][0]:.1f})'
    if all(r >= 1 for _, r in pts):
        return pts[0][0], f'slower at every speed (from {pts[0][0]:.2f})'
    for (s0, r0), (s1, r1) in zip(pts, pts[1:]):
        if r0 < 1 <= r1:
            t = (0 - math.log(r0)) / (math.log(r1) - math.log(r0))
            return math.exp(math.log(s0) + t * (math.log(s1) - math.log(s0))), 'crosses'
    return None, 'not monotonic'


def plain_log(ax):
    """Log axes labelled 0.1, 1, 10 rather than 10^-1, 10^0, 10^1."""
    from matplotlib.ticker import FuncFormatter, LogLocator
    for axis in (ax.xaxis, ax.yaxis):
        if axis.get_scale() == 'log':
            axis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5)))
            axis.set_major_formatter(FuncFormatter(lambda v, _: f'{v:g}'))
            axis.set_minor_formatter(FuncFormatter(lambda v, _: ''))


def fig_time(table, tag):
    fig, axes = plt.subplots(1, 3, figsize=(14.5, 4.8), sharey=True)
    for ax, pattern in zip(axes, PATTERNS):
        for method, label, color, ls, marker in METHODS:
            pts = series(table, pattern, method)
            if not pts:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            err = [[p[1] - p[2]['total_min'] for p in pts], [p[2]['total_max'] - p[1] for p in pts]]
            ax.errorbar(xs, ys, yerr=err, color=color, ls=ls, marker=marker, ms=4.5, capsize=2, label=label)
        ax.set_xscale('log')
        ax.set_yscale('log')
        ax.set_xlabel('Measured link speed, source to destination (Gbit/s)')
        ax.set_title(f'{pattern} 1%', fontsize=11)
        plain_log(ax)
    axes[0].set_ylabel('End-to-end time, median of repeats (s)')
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc='lower center', ncol=6, fontsize=8, bbox_to_anchor=(0.5, -0.07))
    fig.suptitle('EXP-04: time to refresh a 2.1 GB copy between two VMs, by link speed', fontsize=12)
    return plotting.save(fig, f'exp04-time{tag}')


def fig_ratio(table, tag):
    fig, axes = plt.subplots(1, 3, figsize=(14.5, 4.6), sharey=True)
    for ax, pattern in zip(axes, PATTERNS):
        ax.axhline(1, color=COLORS['black'], lw=1)
        for method, label, color, ls, marker in METHODS:
            if method == 'full':
                continue
            pts = [(s, v / table[(pattern, sp)]['full']['total_s'])
                   for s, v, _, sp in series(table, pattern, method) if table[(pattern, sp)].get('full')]
            if pts:
                ax.plot([p[0] for p in pts], [p[1] for p in pts], color=color, ls=ls, marker=marker, ms=4.5,
                        label=label)
        ax.set_xscale('log')
        ax.set_yscale('log')
        ax.set_xlabel('Measured link speed (Gbit/s)')
        ax.set_title(f'{pattern} 1%', fontsize=11)
        ax.text(0.02, 0.97, 'above 1: slower than a full copy', transform=ax.transAxes, ha='left', va='top',
                fontsize=8, color=COLORS['grey'])
        plain_log(ax)
    top = max(ax.get_ylim()[1] for ax in axes)
    for ax in axes:
        ax.axhspan(1, top, color=COLORS['grey'], alpha=0.08)
        ax.set_ylim(top=top)
    axes[0].set_ylabel('Time relative to a full copy (below 1 = faster)')
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc='lower center', ncol=5, fontsize=8, bbox_to_anchor=(0.5, -0.07))
    fig.suptitle('EXP-04: where each method stops beating a full copy', fontsize=12)
    return plotting.save(fig, f'exp04-ratio{tag}')


def fig_phases(rows, tag):
    """Median phase times at the unthrottled link, per workload: where the fixed cost goes."""
    fig, axes = plt.subplots(1, 3, figsize=(14.5, 4.4), sharex=True)
    parts = [('overlap', 'capture of S (beside rsync\'s copy / pgdelta\'s receiver start)', COLORS['grey']),
             ('capture', 'capture (incremental: pg_basebackup --incremental)', COLORS['sky']),
             ('transfer', 'transfer', COLORS['blue']), ('combine', 'pg_combinebackup', COLORS['green'])]
    for ax, pattern in zip(axes, PATTERNS):
        for i, (method, label, *_rest) in enumerate(METHODS):
            group = [r for r in rows if r['pattern'] == pattern and r['method'] == method and r['speed'] == 'unthrottled']
            if not group:
                continue
            med = lambda col: statistics.median([num(r[col]) or 0.0 for r in group])
            overlap = med('prep_capture_s') or med('receiver_capture_s')
            values = {'overlap': overlap, 'capture': med('capture_s') if method == 'incremental' else 0.0,
                      'transfer': med('transfer_s'), 'combine': med('combine_s')}
            left = 0.0
            for key, plabel, color in parts:
                ax.barh(i, values[key], left=left, color=color)
                left += values[key]
            ax.text(left + 0.1, i, f'{left:.1f} s', va='center', fontsize=8)
        ax.set_yticks(range(len(METHODS)), [m[1] for m in METHODS] if ax is axes[0] else [])
        ax.invert_yaxis()
        ax.set_title(f'{pattern} 1%, unthrottled link', fontsize=10.5)
        ax.set_xlabel('Median seconds')
        ax.grid(axis='y', visible=False)
    from matplotlib.patches import Patch
    fig.legend([Patch(color=c) for _, _, c in parts], [l for _, l, _ in parts], loc='lower center', ncol=4,
               fontsize=8, bbox_to_anchor=(0.5, -0.06))
    fig.suptitle('EXP-04: fixed costs at full link speed, by phase', fontsize=12)
    return plotting.save(fig, f'exp04-phases{tag}')


def fig_cpu(table, tag):
    fig, ax = plt.subplots(figsize=(9.5, 4.2))
    width = 0.13
    for i, (method, label, color, *_r) in enumerate(METHODS):
        values = []
        for pattern in PATTERNS:
            entry = table.get((pattern, 'unthrottled'), {}).get(method)
            values.append(entry['cpu_source_s'] if entry else 0)
        ax.bar([k + (i - 2.5) * width for k in range(len(PATTERNS))], values, width, color=color, label=label)
    ax.set_xticks(range(len(PATTERNS)), [f'{p} 1%' for p in PATTERNS])
    ax.set_ylabel('CPU seconds on the source VM (whole VM, median)')
    ax.set_title('EXP-04: what each method costs the source server (unthrottled link)', fontsize=11)
    ax.legend(fontsize=7.8, ncol=2)
    return plotting.save(fig, f'exp04-cpu-source{tag}')


def model_check(table):
    """N2: time ~= fixed local time (unthrottled) + wire bytes / measured speed, within 15% up to 5 Gbit/s."""
    out = []
    for pattern in PATTERNS:
        for method, *_ in METHODS:
            base = table.get((pattern, 'unthrottled'), {}).get(method)
            if not base:
                continue
            fixed = base['total_s'] - (base['wire_s2d_bytes'] * 8 / (base['measured_s2d_gbit'] * 1e9)
                                       + (base['wire_d2s_bytes'] or 0) * 8 / (base['measured_d2s_gbit'] * 1e9))
            for speed_gbit, measured, entry, speed in series(table, pattern, method):
                if speed == 'unthrottled' or speed_gbit > 5.5:
                    continue
                predicted = fixed + entry['wire_s2d_bytes'] * 8 / (entry['measured_s2d_gbit'] * 1e9) \
                    + (entry['wire_d2s_bytes'] or 0) * 8 / (entry['measured_d2s_gbit'] * 1e9)
                out.append(dict(pattern=pattern, method=method, speed=speed, speed_gbit=speed_gbit,
                                fixed_s=fixed, predicted_s=predicted, measured_s=measured,
                                error=(predicted - measured) / measured))
    return out


def fig_model(checks, tag):
    fig, ax = plt.subplots(figsize=(6.2, 5.6))
    lo = min(min(c['predicted_s'], c['measured_s']) for c in checks) * 0.8
    hi = max(max(c['predicted_s'], c['measured_s']) for c in checks) * 1.2
    xs = [lo, hi]
    ax.fill_between(xs, [x / 1.15 for x in xs], [x * 1.15 for x in xs], color=COLORS['grey'], alpha=0.15,
                    label='within ±15%')
    ax.plot(xs, xs, color=COLORS['black'], lw=1)
    for method, label, color, ls, marker in METHODS:
        pts = [c for c in checks if c['method'] == method]
        ax.scatter([c['measured_s'] for c in pts], [c['predicted_s'] for c in pts], color=color, marker=marker,
                   s=22, label=label)
    ax.set_xscale('log')
    ax.set_yscale('log')
    plain_log(ax)
    ax.set_xlabel('Measured end-to-end time (s)')
    ax.set_ylabel('Model: fixed time + wire bytes / link speed (s)')
    ax.set_title('EXP-04 (N2): does the simple model predict the time?\nspeeds up to 5 Gbit/s, all workloads',
                 fontsize=10.5)
    ax.legend(fontsize=7.5)
    return plotting.save(fig, f'exp04-model{tag}')


def write_tables(rows, table, datadir):
    datadir.mkdir(parents=True, exist_ok=True)
    with (datadir / 'exp04-runs.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=KEEP, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    with (datadir / 'exp04-cells.csv').open('w', newline='') as f:
        cols = ['n', 'total_s', 'total_min', 'total_max', 'serial_total_s', 'inplace_total_s', 'wire_s2d_bytes',
                'wire_d2s_bytes', 'cpu_source_s', 'cpu_dest_s']
        w = csv.writer(f)
        w.writerow(['pattern', 'speed', 'speed_gbit', 'method', *cols, 'ratio_to_full'])
        for (pattern, speed), by_method in sorted(table.items(), key=lambda kv: (kv[0][0], kv[1]['speed_gbit'])):
            full = by_method.get('full', {}).get('total_s')
            for method, *_ in METHODS:
                e = by_method.get(method)
                if e:
                    w.writerow([pattern, speed, round(by_method['speed_gbit'], 3), method,
                                *[round(e[c], 4) if isinstance(e.get(c), float) else e.get(c) for c in cols],
                                round(e['total_s'] / full, 3) if full else None])
    return datadir / 'exp04-runs.csv', datadir / 'exp04-cells.csv'


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('summary', nargs='?', default=str(ROOT / 'results' / 'exp04' / 'full' / 'summary.csv'))
    parser.add_argument('--figdir', help='write figures here instead of docs/figures')
    parser.add_argument('--datadir', default=str(ROOT / 'docs' / 'experiments' / 'data'))
    parser.add_argument('--tag', default='', help='suffix for figure names')
    args = parser.parse_args(argv)
    if args.figdir:
        plotting.FIGURES = Path(args.figdir)
    plotting.style()
    rows, total = load(args.summary)
    print(f'{len(rows)} of {total} runs passed and verified')
    table = cells(rows)
    print(*write_tables(rows, table, Path(args.datadir)), sep='\n')
    for fig in (fig_time, fig_ratio):
        print(fig(table, args.tag))
    print(fig_phases(rows, args.tag))
    print(fig_cpu(table, args.tag))
    checks = model_check(table)
    if checks:
        print(fig_model(checks, args.tag))
        within = sum(abs(c['error']) <= 0.15 for c in checks)
        print(f'\nN2: {within} of {len(checks)} (method, workload, speed) cells within 15% '
              f'(worst {max(abs(c["error"]) for c in checks):.0%})')
        for c in sorted(checks, key=lambda c: -abs(c['error']))[:8]:
            print(f"  {c['pattern']:8s} {c['method']:16s} {c['speed']:>6s} measured {c['measured_s']:.2f} "
                  f"predicted {c['predicted_s']:.2f} ({c['error']:+.0%})")
    print('\nCrossover vs full copy (measured Gbit/s, log-log interpolation of median ratios):')
    for pattern in PATTERNS:
        for method in DELTA:
            for col in ('total_s', 'serial_total_s'):
                value, how = crossover(table, pattern, method, col)
                if value is None and how == 'no data':
                    continue
                print(f'  {pattern:8s} {method:16s} {col:15s} {how:30s} {value if value else float("nan"):.2f}')
    print('\nN1 bytes on the wire (median s2d MB) and N3 speedup vs full at the slowest speed:')
    for pattern in PATTERNS:
        slow = min((k for k in table if k[0] == pattern), key=lambda k: table[k]['speed_gbit'], default=None)
        for method, *_ in METHODS:
            ws = [num(r['wire_s2d_bytes']) for r in rows if r['pattern'] == pattern and r['method'] == method]
            if not ws:
                continue
            spread = (max(ws) - min(ws)) / statistics.median(ws)
            speedup = None
            if slow and method in table[slow] and 'full' in table[slow]:
                speedup = table[slow]['full']['total_s'] / table[slow][method]['total_s']
            print(f'  {pattern:8s} {method:16s} wire {statistics.median(ws) / 1e6:9.1f} MB  spread {spread:6.1%}  '
                  f'x faster than full at {slow[1] if slow else "?"}: {speedup if speedup else float("nan"):.1f}')


if __name__ == '__main__':
    main()
