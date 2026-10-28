"""EXP-74's analysis: does the fast path's time model (analysis/fastpath_model.py) predict cells it hasn't seen, and
does its selector pick a fast option? Also the guard's grouped summary reading (EXP-73b) timed on the VMs
(docs/experiments/EXP-74-selector-model.md).

    .venv/bin/python analysis/exp74.py [--results results/exp04/exp74] [--scatter scatter4,scatter12]

Cells: per pattern and arm, the median of the runs (exp68.runs). The selector sees what the sender has once the guard
has run: the cell's unfilled plan (plan-runs-PATTERN-fast8w.json.gz), its guard's measured seconds, and the listed
files' sizes; it predicts fills 0, 1 and 2 (the arms run) and a full copy, and picks the fastest.
Scores 1 (deciding: in each scatter cell, the selector's pick within 5% of the fastest measured arm), 2 (every fast
arm's predicted time, from its own saved plan and guard, within 15% of its measured median), 3 (each scatter cell's
full copy within 5% of its prediction), 4 (the uniform cell's guard at most 0.6 of EXP-70's 4.70 s, its summary rows
within 1% of EXP-70's 1,464,232) and 5 (every run exits 0; every fast run used the fast path and its copy matched the
source; each filled plan exactly fastpath.fill_gaps of the unfilled one). Writes docs/experiments/data/exp74-cells.csv
and the figure exp74-model.
"""
from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path

import exp68
import exp70
import fastpath_model as model
import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
FILL = {'fast8w': 0, 'fast8wf1': 1, 'fast8wf2': 2}
EXP70_GUARD_S, EXP70_ROWS = 4.70, 1464232  # EXP-70's uniform cell, today's reading (A)


def guard_rows(results, pattern):
    """The fast runs' summary rows for a pattern (from send.json)."""
    import json
    rows = []
    for meta_path in sorted(Path(results).glob('run-*/meta.json')):
        meta = json.loads(meta_path.read_text())
        send = meta_path.parent / 'send.json'
        if meta['pattern'] == pattern and meta['arm'] != 'full' and send.exists() and send.stat().st_size:
            rows.append((json.loads(send.read_text()).get('fast_path') or {}).get('summary_rows'))
    return [r for r in rows if r is not None]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp74'))
    ap.add_argument('--scatter', default='scatter4,scatter12')
    ap.add_argument('--uniform', default='uniform')
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp74-cells.csv'))
    ap.add_argument('--figure', default='exp74-model')
    a = ap.parse_args(argv)
    results = Path(a.results)
    rows = exp68.runs(results)
    scatter = [p for p in a.scatter.split(',') if p]
    cells = []
    for pattern in sorted({r['pattern'] for r in rows}):
        for arm in sorted({r['arm'] for r in rows if r['pattern'] == pattern}):
            mine = [r for r in rows if r['pattern'] == pattern and r['arm'] == arm]
            cell = {'pattern': pattern, 'arm': arm, 'runs': len(mine),
                    **{k: exp68.median(mine, k) for k in ('seconds', 'guard_s', 'pages_sent', 'source_reads',
                                                         'dest_writes', 'source_io_s', 'dest_io_s')}}
            plan = results / f'plan-runs-{pattern}-{arm}.json.gz'
            if arm in FILL and plan.exists():
                pages, whole, total = model.load_plan(plan)
                p = model.predict_fast(pages, whole, cell['guard_s'] or 0.0)
                cell.update({'predicted_s': p['seconds'], 'predicted_requests': p['requests'],
                             'predicted_source_s': p['source_s'], 'predicted_dest_s': p['dest_s'],
                             'listed_mb': round(total / 1e6, 1), 'page_fraction': round(p['pages'] * model.PAGE / total, 3)})
            elif arm == 'full':
                base = results / f'plan-runs-{pattern}-fast8w.json.gz'
                if base.exists():
                    cell['predicted_s'] = model.predict_full(model.load_plan(base)[2])
            if cell.get('predicted_s') and cell['seconds']:
                cell['measured_over_predicted'] = round(cell['seconds'] / cell['predicted_s'], 3)
            cells.append(cell)
            print(cell)
    by = {(c['pattern'], c['arm']): c for c in cells}
    picks = {}
    for pattern in scatter:
        base = results / f'plan-runs-{pattern}-fast8w.json.gz'
        guard = (by.get((pattern, 'fast8w')) or {}).get('guard_s')
        if not base.exists() or guard is None:
            continue
        pages, whole, total = model.load_plan(base)
        options, _ = model.select(pages, whole, total, guard, fills=(0, 1, 2, 4))
        measured = {f'fill{FILL[arm]}' if arm in FILL else 'full': by[(pattern, arm)]['seconds']
                    for arm in list(FILL) + ['full'] if (pattern, arm) in by}
        runnable = {k: v for k, v in options.items() if k in measured}
        pick = min(runnable, key=runnable.get)
        best = min(measured, key=measured.get)
        picks[pattern] = {'predicted': options, 'measured': measured, 'pick': pick, 'fastest': best,
                          'pick_over_fastest': round(measured[pick] / measured[best], 3),
                          'fill4_predicted_fastest': min(options, key=options.get) == 'fill4'}
        print(pattern, picks[pattern])
    one = bool(picks) and len(picks) == len(scatter) and all(p['pick_over_fastest'] <= 1.05 for p in picks.values())
    near = [k for k, p in picks.items() if p['pick'] != p['fastest'] and 1.0 < p['pick_over_fastest'] <= 1.10]
    print(f"1 (deciding). {'HELD' if one else 'FAILED'}: the selector's pick against the fastest measured arm "
          f"{ {k: (p['pick'], p['fastest'], p['pick_over_fastest']) for k, p in picks.items()} } (bar 1.05)"
          + (f'; a second session for {near} (rule 11)' if near else ''))
    fast_cells = [c for c in cells if c['arm'] in FILL and c.get('measured_over_predicted') is not None]
    two = bool(fast_cells) and all(abs(c['measured_over_predicted'] - 1) <= 0.15 for c in fast_cells)
    print(f"2. {'HELD' if two else 'FAILED'}: measured / predicted for the fast arms "
          f"{[(c['pattern'], c['arm'], c['measured_over_predicted']) for c in fast_cells]} (within 15%)")
    fulls = [by.get((p, 'full')) for p in scatter]
    three = all(c and c.get('measured_over_predicted') is not None and abs(c['measured_over_predicted'] - 1) <= 0.05
                for c in fulls)
    print(f"3. {'HELD' if three else 'FAILED'}: full copies, measured / predicted "
          f"{[(c['pattern'], c.get('measured_over_predicted')) for c in fulls if c]} (within 5%)")
    uni = [c for c in cells if c['pattern'] == a.uniform and c['arm'] in FILL]
    guard = statistics.median([c['guard_s'] for c in uni]) if uni else None
    summary_rows = guard_rows(results, a.uniform)
    four = (guard is not None and guard <= 0.6 * EXP70_GUARD_S and bool(summary_rows)
            and all(abs(r / EXP70_ROWS - 1) <= 0.01 for r in summary_rows))
    print(f"4. {'HELD' if four else 'FAILED'}: the uniform cell's guard {guard} s against EXP-70's {EXP70_GUARD_S} s "
          f"(bar 0.6, {round(guard / EXP70_GUARD_S, 3) if guard else None}); summary rows {sorted(set(summary_rows))} "
          f"against {EXP70_ROWS}")
    fast = [r for r in rows if r['arm'] != 'full']
    checks = {p: exp70.fill_check(results, p) for p in scatter}
    five = (bool(rows) and all(r['status'] == 0 for r in rows) and all(r['receiver_status'] == 0 for r in fast)
            and all(r['fast_used'] and r['match'] is True for r in fast)
            and all(c['exact'] for p in checks.values() for c in p.values()))
    print(f"5. {'HELD' if five else 'FAILED'}: statuses {sorted({r['status'] for r in rows})}; fast used and matched "
          f"{all(r['fast_used'] and r['match'] is True for r in fast)}; fills {checks}")
    keys = []
    for c in cells:
        keys += [k for k in c if k not in keys]
    with open(a.out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, restval='')
        w.writeheader()
        w.writerows(cells)
    figure(cells, picks, scatter, a.figure)


def figure(cells, picks, scatter, name):
    plotting.style()
    fig, (ax, bx) = plt.subplots(1, 2, figsize=(11.0, 4.4), gridspec_kw={'width_ratios': [3, 2]})
    arms = ('fast8w', 'fast8wf1', 'fast8wf2', 'full')
    labels = {'fast8w': 'fast path, no fill', 'fast8wf1': 'fill 1', 'fast8wf2': 'fill 2', 'full': 'full copy'}
    colors = {'fast8w': COLORS['sky'], 'fast8wf1': COLORS['blue'], 'fast8wf2': COLORS['purple'], 'full': COLORS['orange']}
    by = {(c['pattern'], c['arm']): c for c in cells}
    for i, pattern in enumerate(scatter):
        for j, arm in enumerate(arms):
            c = by.get((pattern, arm))
            if not c or c['seconds'] is None:
                continue
            x = i * 5 + j
            ax.bar(x, c['seconds'], 0.8, color=colors[arm], label=labels[arm] if i == 0 else None)
            if c.get('predicted_s'):
                ax.plot([x - 0.4, x + 0.4], [c['predicted_s']] * 2, color=COLORS['black'], lw=1.2,
                        label='model' if i == 0 and j == 0 else None)
            mark = ' *' if (picks.get(pattern) or {}).get('pick') == (f'fill{FILL[arm]}' if arm in FILL else 'full') else ''
            top = max(c['seconds'], c.get('predicted_s') or 0)  # above the model's line too
            ax.annotate(f"{c['seconds']:.1f}{mark}", (x, top), textcoords='offset points', xytext=(0, 3),
                        ha='center', fontsize=8)
    fractions = {p: (by.get((p, 'fast8w')) or {}).get('page_fraction') for p in scatter}
    ax.set_xticks([i * 5 + 1.5 for i in range(len(scatter))],
                  [f"{p}\n({fractions[p]:.0%} of pages changed)" if fractions[p] else p for p in scatter])
    ax.set_ylabel('seconds (median of 2); * = the selector\'s pick')
    ax.set_ylim(0, max([c['seconds'] for c in cells if c['seconds']] + [1]) * 1.3)
    ax.legend(fontsize=8, loc='upper center', ncol=5)
    ax.set_title('EXP-74: measured against the model, per cell and arm', fontsize=10)
    fast = [c for c in cells if c.get('predicted_s') and c['seconds']]
    bx.scatter([c['predicted_s'] for c in fast], [c['seconds'] for c in fast], color=COLORS['blue'], s=22)
    top = max([c['seconds'] for c in fast] + [c['predicted_s'] for c in fast] + [1]) * 1.1
    bx.plot([0, top], [0, top], color=COLORS['grey'], lw=0.8)
    bx.fill_between([0, top], [0, top * 0.85], [0, top * 1.15], color=COLORS['grey'], alpha=0.12, label='within 15%')
    bx.set_xlabel('predicted seconds')
    bx.set_ylabel('measured seconds')
    bx.legend(fontsize=8, loc='upper left')
    bx.set_title('every cell and arm', fontsize=10)
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
