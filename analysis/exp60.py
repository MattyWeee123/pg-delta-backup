"""EXP-60's analysis: how much of v1's fixed cost on fast links is the hash, and does BLAKE3 lower it?
(docs/experiments/EXP-60-blake3-fast-links.md)

    .venv/bin/python analysis/exp60.py [--sweep results/exp04/exp60] [--background-only]

Background, from EXP-52's and EXP-53's runs already on disk: v1's own timers (pgdelta_direct_inplace_bbr, uniform
1% at 60 ms, 2 to 5 Gbit/s), the hash each run used (from its sender's statistics log) and EXP-29's cost per page
per core, which estimates how much of each hashing phase the hash function itself is. Writes
docs/experiments/data/exp60-floor.csv and the figure exp60-floor.

The run: v1 with BBR, its hash pinned to BLAKE2b (pgdelta_direct_inplace_bbr_b2) against BLAKE3
(pgdelta_direct_inplace_bbr_b3), uniform 1% at 0.5 and 5 Gbit/s and 60 ms. Scores 1 (deciding: BLAKE3's median time
at most 0.90 of BLAKE2b's at 5 Gbit/s), 2 (both hashing phases at most 0.6 of BLAKE2b's at 5 Gbit/s), 3 (the saving
in seconds at 0.5 Gbit/s within 0.4 s of the saving at 5) and 4 (every run passes, with the hash its arm runs).
Writes docs/experiments/data/exp60-cells.csv and the figure exp60-blake3.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
V1, B2, B3 = 'pgdelta_direct_inplace_bbr', 'pgdelta_direct_inplace_bbr_b2', 'pgdelta_direct_inplace_bbr_b3'
HASH_OF = {B2: 'blake2b', B3: 'blake3'}
BACKGROUND = ('exp52', 'exp52b', 'exp53', 'exp53b', 'exp53c')
# EXP-29 on the Intel n2-standard-8 (the default pair's type), one process: keyed pages hashed a second per core.
PAGES_PER_CORE_S = {'blake2b': 73472, 'blake3': 349440}
TIMERS = ('hash_source', 'hash_basis', 'rebuild', 'fsync', 'verify', 'total')


def runs(sweep, methods):
    """Every run of `methods` in a sweep: its status, rate, wall time, v1's timers and the hash its sender used."""
    out = []
    for path in sorted(Path(sweep).glob('runs/*/*.json')):
        d = json.load(open(path))
        if d.get('method') not in methods:
            continue
        reported = d.get('tool_reported') or {}
        used = reported.get('hash')
        if used is None:  # runs before EXP-60 kept it only in the sender's statistics log
            log = Path(sweep) / 'logs' / path.parent.name / 'source' / f'{path.stem}-stats.json.log'
            found = re.search(r'"hash": *"([a-z0-9]+)"', log.read_text()) if log.exists() else None
            used = found.group(1) if found else None
        timers = reported.get('timings_seconds') or {}
        out.append({'sweep': Path(sweep).name, 'method': d['method'], 'status': d.get('status'),
                    'gbit': (d.get('speed') or {}).get('gbit'), 'hash': used, 'jobs': reported.get('jobs'),
                    'pages': (reported.get('pages') or {}).get('total'),
                    'wall_s': (d.get('totals') or {}).get('wall_seconds'),
                    **{f'{k}_s': timers.get(k) for k in TIMERS}})
    return out


def med(rows, key):
    values = [r[key] for r in rows if r.get(key) is not None]
    return round(statistics.median(values), 3) if values else None


def hash_estimate(pages, jobs, name):
    """Seconds of the hash function alone for one pass over `pages` on `jobs` workers, from EXP-29's rate."""
    return pages / PAGES_PER_CORE_S[name] / jobs


def background():
    rows = [r for s in BACKGROUND for r in runs(ROOT / 'results' / 'exp04' / s, {V1})
            if r['status'] == 'pass' and r['gbit']]
    print(f"background: {len(rows)} passing v1 runs; hashes used {sorted({r['hash'] for r in rows})}")
    groups = {'2 Gbit/s': [r for r in rows if r['gbit'] == 2.0],
              '3 to 5 Gbit/s': [r for r in rows if r['gbit'] >= 3.0]}
    cells = []
    for label, group in groups.items():
        pages, jobs = med(group, 'pages'), med(group, 'jobs')
        cell = {'rates': label, 'runs': len(group), 'pages': pages, 'jobs': jobs,
                **{f'{k}_s': med(group, f'{k}_s') for k in TIMERS}, 'wall_s': med(group, 'wall_s'),
                'blake2b_function_s': round(hash_estimate(pages, jobs, 'blake2b'), 3),
                'blake3_function_s': round(hash_estimate(pages, jobs, 'blake3'), 3)}
        cells.append(cell)
        print(cell)
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp60-floor.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(cells[0]))
        w.writeheader()
        w.writerows(cells)
    floor_figure(cells[-1])


def floor_figure(cell):
    """v1's own timers at 3 to 5 Gbit/s, with the part of each hashing phase the BLAKE2b function accounts for."""
    plotting.style()
    fig, ax = plt.subplots(figsize=(8.2, 4.2))
    bars = [('Source: read and hash S', cell['hash_source_s'], True),
            ('Destination: read and hash B', cell['hash_basis_s'], True),
            ('Destination: write the changed pages', cell['rebuild_s'], False),
            ('Destination: fsync', cell['fsync_s'], False),
            ('Destination: verify the copy', cell['verify_s'], False)]
    for i, (label, seconds, hashing) in enumerate(bars):
        y = len(bars) - 1 - i
        ax.barh(y, seconds, 0.6, color=COLORS['blue'] if hashing else COLORS['grey'], alpha=0.35 if hashing else 0.8)
        if hashing:
            ax.barh(y, cell['blake2b_function_s'], 0.6, color=COLORS['blue'],
                    label='of which the BLAKE2b function (estimated from EXP-29)' if i == 0 else None)
            ax.plot([cell['blake3_function_s']] * 2, [y - 0.3, y + 0.3], color=COLORS['orange'], lw=2.5,
                    label='BLAKE3 would take (estimated)' if i == 0 else None)
        ax.text(seconds + 0.03, y, f'{seconds:.2f} s', va='center', fontsize=9)
    ax.set_yticks(range(len(bars)), [b[0] for b in reversed(bars)], fontsize=9)
    ax.set_xlabel("seconds (median of v1's own timers; they overlap, so they don't add up)")
    ax.set_xlim(0, 1.9)
    ax.legend(fontsize=8.5, loc='upper center', bbox_to_anchor=(0.4, -0.2), ncol=2)
    ax.set_title(f"v1's fixed cost on fast links: {cell['runs']} runs at 3 to 5 Gbit/s, 2.1 GB, all BLAKE2b; "
                 f"tool total {cell['total_s']:.2f} s", fontsize=10)
    print(plotting.save(fig, 'exp60-floor'))


def scored(sweeps):
    rows = [r for s in sweeps for r in runs(s, {B2, B3})]
    statuses_ok = rows and all(r['status'] == 'pass' and r['hash'] == HASH_OF[r['method']] for r in rows)
    cells = []
    for gbit in sorted({r['gbit'] for r in rows}):
        cell = {'gbit': gbit}
        for m, name in ((B2, 'blake2b'), (B3, 'blake3')):
            mine = [r for r in rows if r['gbit'] == gbit and r['method'] == m and r['status'] == 'pass']
            cell.update({f'{name}_runs': len(mine), f'{name}_wall_s': med(mine, 'wall_s'),
                         **{f'{name}_{k}_s': med(mine, f'{k}_s') for k in ('hash_source', 'hash_basis', 'verify')}})
        if cell['blake2b_wall_s'] and cell['blake3_wall_s']:
            cell['ratio'] = round(cell['blake3_wall_s'] / cell['blake2b_wall_s'], 3)
            cell['saving_s'] = round(cell['blake2b_wall_s'] - cell['blake3_wall_s'], 3)
        cells.append(cell)
        print(cell)
    fast = next((c for c in cells if c['gbit'] == 5.0), {})
    slow = next((c for c in cells if c['gbit'] == 0.5), {})
    one = fast.get('ratio') is not None and fast['ratio'] <= 0.90
    print(f"1 (deciding). {'HELD' if one else 'FAILED'}: BLAKE3 / BLAKE2b at 5 Gbit/s {fast.get('ratio')} (bar 0.90)"
          + (' -- within 0.05 of the bar: a second session (rule 11)' if fast.get('ratio') and
             abs(fast['ratio'] - 0.90) <= 0.05 else ''))
    phases = [(fast.get(f'blake3_{k}_s'), fast.get(f'blake2b_{k}_s')) for k in ('hash_source', 'hash_basis')]
    two = all(a is not None and b and a / b <= 0.6 for a, b in phases)
    print(f"2. {'HELD' if two else 'FAILED'}: hashing phases at 5 Gbit/s, BLAKE3 / BLAKE2b "
          f"{[round(a / b, 3) if a is not None and b else None for a, b in phases]} (bar 0.6 each)")
    three = None not in (fast.get('saving_s'), slow.get('saving_s')) and abs(slow['saving_s'] - fast['saving_s']) <= 0.4
    print(f"3. {'HELD' if three else 'FAILED'}: saving at 0.5 Gbit/s {slow.get('saving_s')} s against "
          f"{fast.get('saving_s')} s at 5 (within 0.4 s)")
    print(f"4. {'HELD' if statuses_ok else 'FAILED'}: {len(rows)} runs, statuses "
          f"{sorted({r['status'] for r in rows})}, hashes {sorted({(r['method'], r['hash']) for r in rows})}")
    keys = sorted({k for c in cells for k in c}, key=lambda k: (k != 'gbit', k))
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp60-cells.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, restval='')
        w.writeheader()
        w.writerows(cells)
    run_figure(cells)


def run_figure(cells):
    plotting.style()
    fig, ax = plt.subplots(figsize=(7.0, 3.8))
    for i, c in enumerate(cells):
        for j, (name, color) in enumerate((('blake2b', COLORS['grey']), ('blake3', COLORS['blue']))):
            value = c.get(f'{name}_wall_s')
            if value is None:
                continue
            ax.bar(i * 3 + j, value, 0.8, color=color, label=('BLAKE2b (every DR run before)' if name == 'blake2b'
                                                              else 'BLAKE3') if i == 0 else None)
            ax.annotate(f'{value:.2f}', (i * 3 + j, value), textcoords='offset points', xytext=(0, 3),
                        ha='center', fontsize=8.5)
    ax.set_xticks([i * 3 + 0.5 for i in range(len(cells))], [f"{c['gbit']:g} Gbit/s, 60 ms" for c in cells])
    ax.set_ylabel('seconds to refresh 2.1 GB (median)')
    ax.legend(fontsize=8.5)
    ax.set_title('v1 with BLAKE2b against BLAKE3, scattered changes (1%)', fontsize=10)
    print(plotting.save(fig, 'exp60-blake3'))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--sweep', action='append')
    ap.add_argument('--background-only', action='store_true')
    a = ap.parse_args(argv)
    background()
    if a.background_only:
        return
    sweeps = a.sweep or [str(ROOT / 'results' / 'exp04' / 'exp60')]
    sweeps = [s for s in sweeps if (Path(s) / 'runs').exists()]
    if sweeps:
        scored(sweeps)
    else:
        print('no EXP-60 runs yet')


if __name__ == '__main__':
    main()
