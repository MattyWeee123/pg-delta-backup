"""EXP-70's analysis: idea 2, gap-filled reads and writes in the fast path (--fill-gaps), on EXP-64's cells
(docs/experiments/EXP-70-fill-gaps.md).

    .venv/bin/python analysis/exp70.py [--results results/exp04/exp70]

Per pattern and arm (fast8w: 8 reader threads and the sync worker, the fast path's defaults since EXP-68; fast8wfK:
the same with --fill-gaps K; full), the median of 2 runs, the timers and disk counters as in EXP-68 (exp68.runs).
Scores, on the uniform cell unless named: 1 (deciding: fast8wf2 at most 0.85 of fast8w), 2 (fast8wf2's source
read requests and dest write requests each at most 0.40 of fast8w's), 3 (fast8wf4 at least 0.95 of fast8wf2: no
material gain past 2), 4 (hot: fast8wf2 at most max(fast8w + 0.3 s, 1.15 x fast8w)), 5 (every run correct and as
configured; each pattern and arm's plans identical; the threaded runs read the plan's runs; and each filled plan is
exactly fastpath.fill_gaps of the unfilled one) and 6 (controls: fast8w within 10% of EXP-68's 76.54 s, full within
5% of its 90.90 s). Reports EXP-66's model for the session's own unfilled plan beside the measured times. Writes
docs/experiments/data/exp70-cells.csv, exp70-model.csv and the figure exp70-fill.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from pathlib import Path

import numpy as np

import exp66
import exp68
import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pgdelta import fastpath, matching  # noqa: E402

PATTERNS = ('hot', 'uniform')
ARMS = ('fast8w', 'fast8wf1', 'fast8wf2', 'fast8wf4', 'full')
FILL = {'fast8w': 0, 'fast8wf1': 1, 'fast8wf2': 2, 'fast8wf4': 4}
EXP68 = {'fast8w': 76.54, 'full': 90.90}  # EXP-68's uniform medians
MAX_RUN = 128  # the sender's runs: at most 1 MiB of 8 KiB pages


def plan_pages(path):
    """{file path: sorted pages} of a saved plan's files not sent whole, and their runs as listed."""
    plan = json.loads(gzip.decompress(Path(path).read_bytes()))
    pages, runs = {}, {}
    for entry in plan['files']:
        if not entry['whole']:
            pages[entry['path']] = [p for first, count in entry['runs'] for p in range(first, first + count)]
            runs[entry['path']] = [tuple(r) for r in entry['runs']]
    return pages, runs


def fill_check(results, pattern):
    """Whether each filled plan is exactly fill_gaps of the unfilled one, file by file, runs included."""
    base = Path(results) / f'plan-runs-{pattern}-fast8w.json.gz'
    if not base.exists():
        return {}
    unfilled, _ = plan_pages(base)
    out = {}
    for arm, gap in FILL.items():
        path = Path(results) / f'plan-runs-{pattern}-{arm}.json.gz'
        if gap == 0 or not path.exists():
            continue
        pages, runs = plan_pages(path)
        same = set(pages) == set(unfilled) and all(
            pages[f] == fastpath.fill_gaps(unfilled[f], gap) and runs[f] == matching.runs(pages[f], MAX_RUN)
            for f in unfilled)
        out[arm] = {'exact': same, 'pages': sum(map(len, pages.values())),
                    'runs': sum(map(len, runs.values())), 'pages_unfilled': sum(map(len, unfilled.values()))}
    return out


def model(results):
    """EXP-66's write model on this session's unfilled uniform plan (exp68.plan_runs on the per-arm plan)."""
    path = Path(results) / 'plan-runs-uniform-fast8w.json.gz'
    if not path.exists():
        return []
    plan = json.loads(gzip.decompress(path.read_bytes()))
    starts, lengths = [], []
    for number, entry in enumerate(plan['files']):
        for first, count in ([] if entry['whole'] else entry['runs']):
            starts.append(number * exp66.SEGMENT_PAGES + first)
            lengths.append(count)
    fio = exp66.load(ROOT / 'results' / 'exp04' / 'exp66')
    rows = exp66.fill_rows(exp66.cost_of(fio), np.array(starts, dtype=np.int64), np.array(lengths, dtype=np.int64))
    return [r for r in rows if r['gap_pages'] in FILL.values()]


def extra(results):
    """pages_filled per run, from each fast run's statistics."""
    out = {}
    for meta_path in sorted(Path(results).glob('run-*/meta.json')):
        send = meta_path.parent / 'send.json'
        stats = json.loads(send.read_text()) if send.exists() and send.stat().st_size else {}
        out[json.loads(meta_path.read_text())['run']] = ((stats.get('fast_path') or {}).get('fill_gaps') or {}).get(
            'pages_filled', 0 if stats.get('fast_path') else None)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp70'))
    a = ap.parse_args(argv)
    rows = exp68.runs(a.results)
    filled = extra(a.results)
    for r in rows:
        r['pages_filled'] = filled.get(r['run'])
        print(r)
    keys = [k for k in rows[0] if k not in ('run', 'pattern', 'arm', 'repeat', 'status', 'receiver_status', 'match',
                                            'plan', 'fast_used', 'proof', 'fsync_mode')]
    cells = {}
    for pattern in PATTERNS:
        for arm in ARMS:
            mine = [r for r in rows if r['pattern'] == pattern and r['arm'] == arm]
            if mine:
                cells[(pattern, arm)] = {'pattern': pattern, 'arm': arm, 'runs': len(mine),
                                         **{k: exp68.median(mine, k) for k in keys}}
    for cell in cells.values():
        print(cell)
    u = lambda arm, key='seconds': (cells.get(('uniform', arm)) or {}).get(key)
    one = round(u('fast8wf2') / u('fast8w'), 3)
    print(f"1 (deciding). {'HELD' if one <= 0.85 else 'FAILED'}: uniform, fast8wf2 / fast8w {one} (bar 0.85)"
          + ('; within 0.05 of the bar (rule 11)' if abs(one - 0.85) <= 0.05 else ''))
    reads, writes = (round(u('fast8wf2', k) / u('fast8w', k), 3) for k in ('source_reads', 'dest_writes'))
    print(f"2. {'HELD' if reads <= 0.40 and writes <= 0.40 else 'FAILED'}: source reads {reads}, dest writes "
          f"{writes} of fast8w's (bar 0.40 each)")
    three = round(u('fast8wf4') / u('fast8wf2'), 3)
    print(f"3. {'HELD' if three >= 0.95 else 'FAILED'}: fast8wf4 / fast8wf2 {three} (bar at least 0.95)")
    h0, h2 = cells[('hot', 'fast8w')]['seconds'], cells[('hot', 'fast8wf2')]['seconds']
    print(f"4. {'HELD' if h2 <= max(h0 + 0.3, 1.15 * h0) else 'FAILED'}: hot, fast8wf2 {h2} s against fast8w {h0} s")
    fast = [r for r in rows if r['arm'] != 'full']
    plans = {(r['pattern'], r['arm']): set() for r in fast}
    for r in fast:
        plans[(r['pattern'], r['arm'])].add(r['plan'])
    checks = {p: fill_check(a.results, p) for p in PATTERNS}
    saved = {}
    for pattern, arm in plans:
        path = Path(a.results) / f'plan-runs-{pattern}-{arm}.json.gz'
        saved[(pattern, arm)] = sum(map(len, plan_pages(path)[1].values())) if path.exists() else None
    five = (all(r['status'] == 0 for r in rows) and all(r['receiver_status'] == 0 for r in fast)
            and all(r['fast_used'] and r['proof'] == 'backup_manifest' and r['match'] is True
                    and r['fsync_mode'] == 'worker' for r in fast)
            and all(len(v) == 1 for v in plans.values())
            and all(r['reads_run'] == saved[(r['pattern'], r['arm'])] for r in fast)
            and all(c['exact'] for p in checks.values() for c in p.values()) and bool(checks.get('uniform')))
    print(f"5. {'HELD' if five else 'FAILED'}: statuses {sorted({r['status'] for r in rows})}, plans per arm "
          f"{ {k: len(v) for k, v in plans.items()} }, fills {checks}")
    six = abs(u('fast8w') / EXP68['fast8w'] - 1) <= 0.10 and abs(u('full') / EXP68['full'] - 1) <= 0.05
    print(f"6. {'HELD' if six else 'FAILED'}: fast8w {u('fast8w')} against {EXP68['fast8w']} (10%), full {u('full')} "
          f"against {EXP68['full']} (5%)")
    model_rows = model(a.results)
    for r in model_rows:
        print(r)
    data = ROOT / 'docs' / 'experiments' / 'data'
    for name, table in (('exp70-cells.csv', list(cells.values())), ('exp70-model.csv', model_rows)):
        if table:
            with open(data / name, 'w', newline='') as f:
                w = csv.DictWriter(f, fieldnames=list(table[0]))
                w.writeheader()
                w.writerows(table)
    figure(cells, model_rows)


def figure(cells, model_rows):
    plotting.style()
    fig, (left, middle, right) = plt.subplots(1, 3, figsize=(13.4, 4.4), gridspec_kw={'width_ratios': [1.2, 1, 1]})
    arms = [arm for arm in ARMS if ('uniform', arm) in cells]
    labels = {'fast8w': 'no fill', 'fast8wf1': 'fill 1', 'fast8wf2': 'fill 2', 'fast8wf4': 'fill 4',
              'full': 'pg_base-\nbackup'}
    colors = [COLORS['blue'] if arm != 'full' else COLORS['orange'] for arm in arms]
    values = [cells[('uniform', arm)]['seconds'] for arm in arms]
    left.bar(range(len(arms)), values, 0.75, color=colors)
    for i, value in enumerate(values):
        left.annotate(f'{value:.1f}', (i, value), textcoords='offset points', xytext=(0, 3), ha='center', fontsize=8.5)
    left.set_xticks(range(len(arms)), [labels[arm] for arm in arms], fontsize=8.5)
    left.set_ylabel('seconds to refresh about 18 GB (median)')
    left.set_title('Scattered changes (28% of pages), 8 readers + sync worker', fontsize=9.5)
    fast = [arm for arm in arms if arm != 'full']
    x = np.arange(len(fast))
    for j, (key, label, color) in enumerate((('source_reads', 'source read requests', COLORS['green']),
                                             ('dest_writes', 'destination write requests', COLORS['red']),
                                             ('reads_run', "the plan's runs", COLORS['grey']))):
        middle.bar(x + (j - 1) * 0.27, [cells[('uniform', arm)][key] or 0 for arm in fast], 0.27, color=color,
                   label=label)
    middle.set_xticks(x, [labels[arm] for arm in fast], fontsize=8.5)
    middle.set_ylabel('requests (thousands)')
    middle.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f'{v / 1000:.0f}'))
    middle.legend(fontsize=7.5, loc='upper right')
    middle.set_title('I/O requests per refresh', fontsize=9.5)
    if model_rows:
        gaps = [r['gap_pages'] for r in model_rows]
        right.plot(gaps, [r['seconds'] for r in model_rows], 's--', color=COLORS['grey'],
                   label="EXP-66's model: writing the plan")
        measured = [cells[('uniform', arm)]['seconds'] for arm, g in FILL.items() if ('uniform', arm) in cells]
        right.plot([g for arm, g in FILL.items() if ('uniform', arm) in cells], measured, 'o-', color=COLORS['blue'],
                   label='measured refresh')
        right.set_xticks(gaps)
        right.set_xlabel('clean gaps filled, up to this many pages')
        right.set_ylabel('seconds')
        right.set_ylim(0, max(measured + [r['seconds'] for r in model_rows]) * 1.25)
        right.legend(fontsize=7.5, loc='lower left')
        right.set_title('Measured against modelled', fontsize=9.5)
    fig.suptitle('EXP-70: gap-filled reads and writes in the fast path (8 GB VMs, balanced disks)', fontsize=10.5)
    fig.tight_layout()
    print(plotting.save(fig, 'exp70-fill'))


if __name__ == '__main__':
    main()
