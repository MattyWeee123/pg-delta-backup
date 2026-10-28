"""EXP-79's analysis: the guard's unsorted summary reading (EXP-78) timed on the VMs, and the break-even against
pg_basebackup re-placed with the fast path as it now is (docs/experiments/EXP-79-guard-on-vms.md).

    .venv/bin/python analysis/exp79.py [--results results/exp04/exp79]

Cells: per pattern and arm, the median of the runs (exp68.runs); each fast run's guard and summary rows from its
statistics. Scores 1 (deciding: scatterall12's guard at most 0.60 of EXP-75 to EXP-77's 5.31 s), 2 (uniform's guard at
most 0.60 of their 1.91 s), 3 (scatterall12's fast path at least 2.0 s below EXP-77's 108.48 s, with
pg_basebackup within 1% of EXP-77's 101.25 s), 4 (the summary rows within 1% of the earlier sessions', so the guards
read the same) and 5 (every run exits 0; every fast run used the fast path and passed the copy's check). Reports
the fast path over pg_basebackup in scatter12 and scatterall12 and their interpolated crossing. Writes
docs/experiments/data/exp79-cells.csv and the figure exp79-guard.
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import exp68
import exp76
import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
ARMS = {'uniform': ('fast8wf2',), 'scatter12': ('fast8w', 'full'), 'scatterall12': ('fast8w', 'full')}
# Earlier sessions' guard medians and summary rows, the same sort of cell (EXP-74 to EXP-77's statistics).
BEFORE = {'uniform': (1.91, 1470162), 'scatter12': (2.91, 2585634), 'scatterall12': (5.31, 4599331)}
EXP77_FAST_S, EXP77_FULL_S = 108.48, 101.25
GUARD_BAR, SAVING_S, FULL_BAND, ROWS_BAND = 0.60, 2.0, 0.01, 0.01


def guards(results):
    """{run: (guard seconds, summary rows, unfilled share of pages: pages chosen over pages listed)}."""
    out = {}
    for meta_path in sorted(Path(results).glob('run-*/meta.json')):
        meta = json.loads(meta_path.read_text())
        send = meta_path.parent / 'send.json'
        if meta['arm'] == 'full' or not send.exists() or not send.stat().st_size:
            continue
        stats = json.loads(send.read_text())
        fast = stats.get('fast_path') or {}
        out[meta['run']] = (fast.get('guard_seconds'), fast.get('summary_rows'),
                            fast.get('pages_chosen') / (stats.get('pages') or {}).get('total', 1))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp79'))
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp79-cells.csv'))
    ap.add_argument('--figure', default='exp79-guard')
    a = ap.parse_args(argv)
    results = Path(a.results)
    rows, guard = exp68.runs(results), guards(results)
    cells = []
    for pattern, arms in ARMS.items():
        for arm in arms:
            mine = [r for r in rows if r['pattern'] == pattern and r['arm'] == arm]
            if not mine:
                continue
            cell = {'pattern': pattern, 'arm': arm, 'runs': len(mine), 'seconds': exp68.median(mine, 'seconds')}
            g = [guard[r['run']] for r in mine if r['run'] in guard]
            if g:
                cell.update({'guard_s': round(statistics.median(x[0] for x in g), 3),
                             'summary_rows': int(statistics.median(x[1] for x in g)),
                             'share_chosen': round(statistics.median(x[2] for x in g), 3)})
                cell['guard_us_per_row'] = round(cell['guard_s'] / cell['summary_rows'] * 1e6, 3)
            cells.append(cell)
            print(cell)
    by = {(c['pattern'], c['arm']): c for c in cells}
    fast = {p: by.get((p, ARMS[p][0])) for p in ARMS}
    ratio = {p: round(fast[p]['guard_s'] / BEFORE[p][0], 3) for p in ARMS if fast[p] and fast[p].get('guard_s')}
    one = ratio.get('scatterall12') is not None and ratio['scatterall12'] <= GUARD_BAR
    print(f"1 (deciding). {'HELD' if one else 'FAILED'}: scatterall12's guard {fast['scatterall12'] and fast['scatterall12'].get('guard_s')} s, "
          f"{ratio.get('scatterall12')} of {BEFORE['scatterall12'][0]} s (bar {GUARD_BAR})"
          + ('; within 0.05 of the bar (rule 11)' if ratio.get('scatterall12') is not None
             and abs(ratio['scatterall12'] - GUARD_BAR) <= 0.05 else ''))
    two = ratio.get('uniform') is not None and ratio['uniform'] <= GUARD_BAR
    print(f"2. {'HELD' if two else 'FAILED'}: uniform's guard {ratio.get('uniform')} of {BEFORE['uniform'][0]} s "
          f"(bar {GUARD_BAR}); scatter12's {ratio.get('scatter12')} (reported)")
    full = by.get(('scatterall12', 'full'))
    three = (fast['scatterall12'] is not None and full is not None
             and fast['scatterall12']['seconds'] <= EXP77_FAST_S - SAVING_S
             and abs(full['seconds'] / EXP77_FULL_S - 1) <= FULL_BAND)
    print(f"3. {'HELD' if three else 'FAILED'}: scatterall12's fast path {fast['scatterall12'] and fast['scatterall12']['seconds']} s "
          f"(bar {EXP77_FAST_S - SAVING_S}); pg_basebackup {full and full['seconds']} s against {EXP77_FULL_S} "
          f"(within {FULL_BAND:.0%})")
    four = all(fast[p] and abs(fast[p]['summary_rows'] / BEFORE[p][1] - 1) <= ROWS_BAND for p in ARMS)
    print(f"4. {'HELD' if four else 'FAILED'}: summary rows {({p: fast[p] and fast[p].get('summary_rows') for p in ARMS})} "
          f"against {({p: BEFORE[p][1] for p in ARMS})} (within {ROWS_BAND:.0%})")
    fast_rows = [r for r in rows if r['arm'] != 'full']
    five = (bool(rows) and all(r['status'] == 0 for r in rows) and all(r['receiver_status'] == 0 for r in fast_rows)
            and all(r['fast_used'] and r['match'] is True for r in fast_rows))
    print(f"5. {'HELD' if five else 'FAILED'}: statuses {sorted({r['status'] for r in rows})}; fast used and matched "
          f"{all(r['fast_used'] and r['match'] is True for r in fast_rows)} ({len(fast_rows)} runs)")
    points = [(fast[p]['share_chosen'], fast[p]['seconds'] / by[(p, 'full')]['seconds'])
              for p in ('scatter12', 'scatterall12') if fast[p] and (p, 'full') in by]
    if len(points) == 2:
        (x0, y0), (x1, y1) = points
        cross = x0 + (1 - y0) * (x1 - x0) / (y1 - y0) if y1 != y0 else None
        print(f"reported: fast / pg_basebackup {[(round(x, 3), round(y, 3)) for x, y in points]}; interpolated "
              f"crossing {cross if cross is None else round(cross, 3)} of the listed pages")
    exp76.write_csv(a.out, cells)
    figure(by, a.figure)


def figure(by, name):
    """The guard's median per cell, before (EXP-74 to EXP-77, sorted reading) and now (unsorted), with its rows."""
    plotting.style()
    fig, ax = plt.subplots(figsize=(7.4, 3.9))
    for i, pattern in enumerate(ARMS):
        before, rows = BEFORE[pattern]
        now = (by.get((pattern, ARMS[pattern][0])) or {}).get('guard_s')
        ax.bar(i - 0.18, before, 0.34, color=COLORS['grey'], label='before (sorted, distinct)' if i == 0 else None)
        ax.annotate(f'{before:.2f} s', (i - 0.18, before), textcoords='offset points', xytext=(0, 3), ha='center',
                    fontsize=8.5)
        if now is not None:
            ax.bar(i + 0.18, now, 0.34, color=COLORS['blue'], label='EXP-79 (unsorted)' if i == 0 else None)
            ax.annotate(f'{now:.2f} s\n({now / before:.2f})', (i + 0.18, now), textcoords='offset points',
                        xytext=(0, 3), ha='center', fontsize=8.5)
    ax.set_xticks(range(len(ARMS)), [f'{p}\n({BEFORE[p][1] / 1e6:.2f} M summary rows)' for p in ARMS], fontsize=9)
    ax.set_ylabel("the guard's seconds (median)")
    ax.set_ylim(0, max(v for v, _ in BEFORE.values()) * 1.25)
    ax.legend(fontsize=8.5, loc='upper left')
    ax.set_title("EXP-79: the fast path's guard on the VMs, before and after EXP-78", fontsize=10)
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
