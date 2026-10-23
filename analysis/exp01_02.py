"""EXP-01 (change patterns) and EXP-02 (method comparison) figures and curated data.

    .venv/bin/python analysis/exp01_02.py [RESULTS_ROOT]     default: results/exp01
Reads every passing trial JSON under RESULTS_ROOT/*/trials/, writes
docs/experiments/data/exp01-trials.csv and docs/figures/exp01-*.svg, exp02-*.svg.
"""
from __future__ import annotations

import csv
import glob
import json
import statistics
import sys
from pathlib import Path

import numpy as np

import plotting
from plotting import COLORS, METHOD_COLORS, PATTERN_COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'docs' / 'experiments' / 'data'
CHUNKS = ['8K', '16K', '32K', '64K', '128K', '256K', '512K', '1M']
CHUNK_BYTES = [8, 16, 32, 64, 128, 256, 512, 1024]
METHODS = [('full', 'Full copy'), ('incremental', 'PG17 incremental (capture only)'),
           ('rsync_default', 'rsync, default blocks'), ('rsync_8k', 'rsync, 8 KiB blocks'),
           ('pgdelta', 'pgdelta')]


MARKERS = {'uniform': 'o', 'tpcb': 's', 'hot': '^', 'clustered': 'D', 'append': 'v', 'retention': 'P'}


def line_style(name):
    base = name.split()[0]
    dashed = base == 'tpcb' or '10%' in name
    return dict(marker=MARKERS[base], color=PATTERN_COLORS[base], ls='--' if dashed else '-',
                mfc='none' if base == 'tpcb' else None, ms=7 if base == 'tpcb' else 5.5)


def pattern_name(params):
    if params['pattern'] == 'hot':
        return f"hot ({params['hot_percent']:g}% set)"
    return params['pattern']


def load(root):
    trials = []
    for path in sorted(glob.glob(str(Path(root) / '*' / 'trials' / '*.json'))):
        d = json.loads(Path(path).read_text())
        if d.get('status') != 'pass':
            print('skipping', path, d.get('status'), d.get('error'))
            continue
        p, g = d['params'], d['ground_truth']
        ppr = g['pages_per_row']
        data = d['content_changes']['totals']['data']['chunks']
        incr = d['agreement']['incremental_backup']['scopes']['all']
        m = d.get('methods') or {}
        full = (m.get('full') or {}).get('bytes_source_to_dest')
        row = {
            'run': Path(path).parts[-3], 'trial_id': d['trial_id'], 'pattern': p['pattern'],
            'pattern_name': pattern_name(p), 'fraction': p['fraction'], 'variant': p['variant'],
            'repeat': p.get('repeat', 0), 'rows_changed': g['rows_changed'],
            'target_table': g['target_table'],
            'heap_existing_changed_pct': ppr.get('heap_existing_changed_pct'),
            'model_heap_existing_pct': ppr.get('uniform_model_heap_existing_pct'),
            'heap_appended_pages': ppr.get('heap_appended_pages'),
            'heap_overall_changed_pct': ppr.get('heap_overall_changed_pct'),
            'index_changed_pct': ppr.get('index_changed_pct'),
            'rows_per_heap_page': ppr.get('rows_per_heap_page_before'),
            **{f'send_pct_{c}': 100 * data[c]['send_fraction'] for c in CHUNKS},
            **{f'changed_pct_{c}': 100 * data[c]['changed_fraction'] for c in CHUNKS},
            'changed_and_tracked': incr['changed_and_tracked'],
            'changed_not_tracked': incr['changed_not_tracked'],
            'tracked_unchanged': incr['tracked_unchanged'],
            'full_bytes': full,
        }
        for key, _ in METHODS:
            entry = m.get(key) or {}
            row[f'{key}_s2d'] = entry.get('bytes_source_to_dest')
            row[f'{key}_d2s'] = entry.get('bytes_dest_to_source')
            row[f'{key}_wall'] = entry.get('wall_seconds')
            row[f'{key}_cpu'] = entry.get('cpu_seconds')
            row[f'{key}_identical'] = entry.get('identical')
        trials.append(row)
    return trials


def base_grid(trials):
    """One row per (pattern, fraction) for base-variant trials, median over repeats."""
    groups = {}
    for t in trials:
        if t['variant'] != 'base':
            continue
        groups.setdefault((t['pattern_name'], t['fraction']), []).append(t)
    return groups


def median(rows, key):
    values = [r[key] for r in rows if r.get(key) is not None]
    return statistics.median(values) if values else None


def write_csv(trials):
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / 'exp01-trials.csv'
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(trials[0]))
        writer.writeheader()
        writer.writerows(trials)
    return path


def crossover_gbps(full_bytes, s2d, d2s, seconds):
    """Link speed above which a full copy (charged for bytes only) finishes first."""
    saved = full_bytes - s2d - (d2s or 0)
    if not seconds or saved <= 0:
        return None
    return saved * 8 / seconds / 1e9


def write_methods_csv(groups):
    """EXP-02: one row per (pattern, fraction), medians over repeats."""
    path = DATA / 'exp02-methods.csv'
    rows = []
    for (name, fraction), group in sorted(groups.items()):
        row = {'pattern': name, 'fraction': fraction, 'trials': len(group),
               'pages_to_send_pct': round(median(group, 'send_pct_8K'), 2)}
        full = median(group, 'full_s2d')
        for key, _ in METHODS:
            s2d, d2s = median(group, f'{key}_s2d'), median(group, f'{key}_d2s')
            row[f'{key}_MB'] = round(s2d / 1e6, 1) if s2d is not None else None
            row[f'{key}_back_KB'] = round(d2s / 1e3) if d2s is not None else None
            if key in ('rsync_default', 'rsync_8k', 'pgdelta'):
                wall = median(group, f'{key}_wall')
                row[f'{key}_wall_s'] = round(wall, 2)
                row[f'{key}_cpu_s'] = round(median(group, f'{key}_cpu'), 2)
                gbps = crossover_gbps(full, s2d, d2s, wall)
                row[f'{key}_crossover_gbps'] = round(gbps, 2) if gbps else None
        rows.append(row)
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def fig_model(groups):
    fig, (heap_ax, index_ax) = plt.subplots(1, 2, figsize=(10.5, 4.3), sharey=True)
    names = sorted({name for name, _ in groups}, key=lambda n: (list(PATTERN_COLORS).index(n.split()[0]), n))
    for name in names:
        if name.split()[0] in ('append', 'retention'):
            continue  # their target table is events; shown in the send-fraction figure
        points = sorted((f, rows) for (n, f), rows in groups.items() if n == name)
        xs = [100 * f for f, _ in points]
        heap_ax.plot(xs, [median(r, 'heap_existing_changed_pct') for _, r in points],
                     label=name, **line_style(name))
        index_ax.plot(xs, [median(r, 'index_changed_pct') for _, r in points],
                      label=name, **line_style(name))
    f = np.logspace(-1.2, 1.1, 60) / 100
    heap_model = 100 * (1 - (1 - f) ** 61)
    index_model = 100 * (1 - (1 - f) ** 360)
    heap_ax.plot(100 * f, heap_model, color=COLORS['black'], lw=1, alpha=0.6, label='model 1-(1-f)^61')
    index_ax.plot(100 * f, index_model, color=COLORS['black'], lw=1, alpha=0.6, label='model 1-(1-f)^360')
    for ax, title in ((heap_ax, 'Existing heap pages of the table that changed'),
                      (index_ax, 'Primary-key index pages that changed')):
        ax.set_xscale('log')
        ax.set_xlabel('Rows changed (% of the table, log scale)')
        ax.set_title(title, fontsize=10.5)
        ax.set_ylim(0, 102)
    heap_ax.set_ylabel('Pages changed (%)')
    heap_ax.legend(fontsize=8, loc='upper left')
    index_ax.legend(fontsize=8, loc='lower right')
    fig.suptitle('EXP-01: a few scattered row changes touch most pages; local changes do not', fontsize=12)
    return plotting.save(fig, 'exp01-pages-vs-rows')


def fig_send(groups):
    fig, ax = plt.subplots(figsize=(7.6, 4.3))
    names = sorted({name for name, _ in groups}, key=lambda n: (list(PATTERN_COLORS).index(n.split()[0]), n))
    for name in names:
        points = sorted((f, rows) for (n, f), rows in groups.items() if n == name)
        ax.plot([100 * f for f, _ in points], [median(r, 'send_pct_8K') for _, r in points],
                label=name, **line_style(name))
    ax.set_xscale('log')
    ax.set_yscale('log')
    ax.set_xlabel('Rows changed (% of the target table, log scale)')
    ax.set_ylabel('Data that must be sent, % of database (log)')
    ax.set_title('EXP-01: share of the database a page-level refresh must send (8 KiB pages)', fontsize=11)
    ax.legend(fontsize=8.5)
    return plotting.save(fig, 'exp01-send-fraction')


def fig_chunks(groups, fraction=0.01):
    fig, ax = plt.subplots(figsize=(7.6, 4.3))
    for (name, f), rows in sorted(groups.items()):
        if f != fraction:
            continue
        ax.plot(CHUNK_BYTES, [median(rows, f'send_pct_{c}') for c in CHUNKS], label=name,
                **line_style(name))
    ax.set_xscale('log', base=2)
    ax.set_yscale('log')
    ax.set_xticks(CHUNK_BYTES, [c.replace('K', ' KiB').replace('1M', '1 MiB') for c in CHUNKS])
    ax.set_xlabel('Chunk size compared')
    ax.set_ylabel('Bytes that must be sent, % of data (log)')
    ax.set_title(f'EXP-01: bigger chunks send more ({fraction:.0%} of rows changed)', fontsize=11)
    ax.legend(fontsize=8.5)
    return plotting.save(fig, 'exp01-chunk-size')


def fig_methods(groups, fraction=0.01):
    cells = [(name, rows) for (name, f), rows in sorted(groups.items()) if f == fraction]
    fig, ax = plt.subplots(figsize=(9.5, 4.4))
    width = 0.16
    x = np.arange(len(cells))
    for i, (key, label) in enumerate(METHODS):
        values = []
        for _, rows in cells:
            sent, full = median(rows, f'{key}_s2d'), median(rows, 'full_bytes')
            values.append(100 * sent / full if sent is not None and full else np.nan)
        color = {'full': METHOD_COLORS['full'], 'incremental': METHOD_COLORS['incremental'],
                 'rsync_default': METHOD_COLORS['rsync'], 'rsync_8k': METHOD_COLORS['rsync_8k'],
                 'pgdelta': METHOD_COLORS['pg_delta']}[key]
        ax.bar(x + (i - 2) * width, values, width, label=label, color=color)
    ax.set_xticks(x, [name for name, _ in cells])
    ax.set_ylabel('Bytes sent to the destination, % of a full copy')
    ax.set_title(f'EXP-02: bytes each method sends ({fraction:.0%} of rows changed, about 2.1 GB database)',
                 fontsize=11)
    ax.legend(fontsize=8, ncol=3, loc='upper center', bbox_to_anchor=(0.5, -0.12))
    return plotting.save(fig, f'exp02-bytes-f{fraction:g}')


def fig_compute(groups, fraction=0.01):
    cells = [(name, rows) for (name, f), rows in sorted(groups.items()) if f == fraction]
    fig, ax = plt.subplots(figsize=(8.6, 4.0))
    x = np.arange(len(cells))
    width = 0.26
    for i, (key, label, color) in enumerate([('pgdelta', 'pgdelta', METHOD_COLORS['pg_delta']),
                                             ('rsync_8k', 'rsync, 8 KiB blocks', METHOD_COLORS['rsync_8k']),
                                             ('rsync_default', 'rsync, default blocks', METHOD_COLORS['rsync'])]):
        ax.bar(x + (i - 1) * width, [median(rows, f'{key}_wall') for _, rows in cells], width,
               label=label, color=color)
    ax.set_xticks(x, [name for name, _ in cells])
    ax.set_ylabel('Wall time to rebuild B1 from B0 (s)')
    ax.set_title(f'EXP-02: compute time on one machine (2.1 GB, {fraction:.0%} of rows changed)', fontsize=11)
    ax.legend(fontsize=8.5)
    return plotting.save(fig, f'exp02-compute-f{fraction:g}')


def fig_time_model(groups, cell):
    """Modelled refresh time vs link speed: local compute plus bytes / bandwidth."""
    rows = groups[cell]
    gbps = np.logspace(-1, 1.6, 80)  # 0.1 .. 40 Gbit/s
    fig, ax = plt.subplots(figsize=(7.6, 4.3))
    for key, label in METHODS:
        if key == 'incremental':
            continue  # its pg_combinebackup step wasn't timed here; EXP-04 measures it
        s2d, d2s = median(rows, f'{key}_s2d'), median(rows, f'{key}_d2s') or 0
        if s2d is None:
            continue
        compute = median(rows, f'{key}_wall') or 0.0
        seconds = compute + (s2d + d2s) * 8 / (gbps * 1e9)
        color = {'full': METHOD_COLORS['full'], 'incremental': METHOD_COLORS['incremental'],
                 'rsync_default': METHOD_COLORS['rsync'], 'rsync_8k': METHOD_COLORS['rsync_8k'],
                 'pgdelta': METHOD_COLORS['pg_delta']}[key]
        ax.plot(gbps, seconds, color=color, label=label, ls='--' if key == 'incremental' else '-')
    ax.set_xscale('log')
    ax.set_yscale('log')
    ax.set_xlabel('Link speed (Gbit/s, log scale)')
    ax.set_ylabel('Modelled refresh time (s, log scale)')
    name, fraction = cell
    ax.set_title(f'EXP-02 model: {name}, {fraction:.0%} of rows changed\n'
                 '(local compute + bytes / link speed; full copy charged for bytes only;\n'
                 'incremental left out until its combine step is timed in EXP-04)', fontsize=10)
    ax.legend(fontsize=8)
    return plotting.save(fig, f'exp02-time-model-{name.split()[0]}-f{fraction:g}')


def main(argv):
    root = Path(argv[1]) if len(argv) > 1 else ROOT / 'results' / 'exp01'
    plotting.style()
    trials = load(root)
    print(write_csv(trials), len(trials), 'trials')
    groups = base_grid(trials)
    print(write_methods_csv(groups))
    print(fig_model(groups))
    print(fig_send(groups))
    print(fig_chunks(groups))
    for fraction in (0.01, 0.1):
        if any(f == fraction for _, f in groups):
            print(fig_methods(groups, fraction))
    print(fig_compute(groups))
    for cell in (('uniform', 0.01), ('append', 0.01)):
        if cell in groups:
            print(fig_time_model(groups, cell))


if __name__ == '__main__':
    main(sys.argv)
