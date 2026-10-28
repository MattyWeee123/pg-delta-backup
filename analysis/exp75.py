"""EXP-75's analysis: does --selector, built into the sender, come within 5% of the fastest fixed fill in a cell bound
by requests (EXP-70's uniform cell) and in one where nearly every page changed (scatterall12), and does it say rightly
where pg_basebackup is faster (docs/experiments/EXP-75-selector-timed.md)?

    .venv/bin/python analysis/exp75.py [--results results/exp04/exp75]

Cells: per pattern and arm, the median of the runs (exp68.runs); the selector's arm is fast8ws. Each selector run's
choice, predictions and own time come from its statistics (fast_path.selector).
Scores 1 (deciding: in each cell, the selector's median at most 1.05 of the fastest fixed fill's), 2 (its choice in
every run: fill 1 or 2 in uniform, fill 0, 1 or 2 in scatterall12), 3 (pg_basebackup predicted faster in every
scatterall12 run and in no uniform run, and in scatterall12 measured at most 0.95 of the fixed fill's time), 4 (the
fixed fills within 15% of the model from their own plans and guards, pg_basebackup within 5% of the model from the
listed bytes), 5 (the choice's own time at most 1.0 s in every run) and 6 (every run exits 0; every fast run used the
fast path and its copy matched the source's check). Writes
docs/experiments/data/exp75-cells.csv and the figure exp75-selector.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import exp68
import fastpath_model as model
import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
SELECTOR = 'fast8ws'
FIXED = {'uniform': ('fast8wf2',), 'scatterall12': ('fast8w',)}  # the fixed fills it is decided against
ARMS = {'uniform': ('fast8wf2', SELECTOR), 'scatterall12': ('fast8w', 'full', SELECTOR)}
EXPECTED = {'uniform': ('fill1', 'fill2'), 'scatterall12': ('fill0', 'fill1', 'fill2')}
FULL_FASTER = {'uniform': False, 'scatterall12': True}
FILL = {'fast8w': 0, 'fast8wf1': 1, 'fast8wf2': 2}


def selector_runs(results):
    """{run number: fast_path.selector} for the selector's runs (from send.json)."""
    out = {}
    for meta_path in sorted(Path(results).glob('run-*/meta.json')):
        meta = json.loads(meta_path.read_text())
        send = meta_path.parent / 'send.json'
        if meta['arm'] == SELECTOR and send.exists() and send.stat().st_size:
            out[meta['run']] = (json.loads(send.read_text()).get('fast_path') or {}).get('selector') or {}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp75'))
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp75-cells.csv'))
    ap.add_argument('--figure', default='exp75-selector')
    a = ap.parse_args(argv)
    results = Path(a.results)
    rows = exp68.runs(results)
    chosen = selector_runs(results)
    cells = []
    for pattern in FIXED:
        for arm in ARMS[pattern]:
            mine = [r for r in rows if r['pattern'] == pattern and r['arm'] == arm]
            if not mine:
                continue
            cell = {'pattern': pattern, 'arm': arm, 'runs': len(mine),
                    **{k: exp68.median(mine, k) for k in ('seconds', 'guard_s', 'pages_sent', 'source_reads',
                                                         'dest_writes', 'source_io_s', 'dest_io_s', 'source_s')}}
            plan = results / f'plan-runs-{pattern}-{arm}.json.gz'
            if arm in FILL and plan.exists():
                pages, whole, total = model.load_plan(plan)
                p = model.predict_fast(pages, whole, cell['guard_s'] or 0.0)
                cell.update({'predicted_s': p['seconds'], 'predicted_requests': p['requests'],
                             'listed_mb': round(total / 1e6, 1),
                             'page_fraction': round(p['pages'] * model.PAGE / total, 3)})
            elif arm == 'full':
                base = next((results / f'plan-runs-{pattern}-{x}.json.gz' for x in (*FILL, SELECTOR)
                             if (results / f'plan-runs-{pattern}-{x}.json.gz').exists()), None)
                if base is not None:
                    cell['predicted_s'] = model.predict_full(model.load_plan(base)[2])
            if cell.get('predicted_s') and cell['seconds']:
                cell['measured_over_predicted'] = round(cell['seconds'] / cell['predicted_s'], 3)
            if arm == SELECTOR:
                picks = [chosen.get(r['run'], {}) for r in mine]
                cell['choices'] = ' '.join(sorted(p.get('choice') or '?' for p in picks))
                cell['choice_s_max'] = max((p.get('seconds') or 0.0) for p in picks)
                first = picks[0].get('predicted_seconds') or {}
                cell.update({f'predicted_{k}_s': v for k, v in first.items()})
            cells.append(cell)
            print(cell)
    by = {(c['pattern'], c['arm']): c for c in cells}
    picks = {p: [chosen[r['run']].get('choice') for r in rows if r['pattern'] == p and r['arm'] == SELECTOR
                 and r['run'] in chosen] for p in FIXED}
    ratios, fastest = {}, {}
    for pattern, arms in FIXED.items():
        sel = by.get((pattern, SELECTOR))
        fixed = {arm: by[(pattern, arm)]['seconds'] for arm in arms if (pattern, arm) in by}
        if sel and fixed:
            fastest[pattern] = min(fixed, key=fixed.get)
            ratios[pattern] = round(sel['seconds'] / fixed[fastest[pattern]], 3)
    one = len(ratios) == len(FIXED) and all(r <= 1.05 for r in ratios.values())
    # Rule 11 as registered: 1.05-1.10, or 1.00-1.05 with a choice other than the fastest fixed arm's option (the same
    # choice makes the ratio run-to-run spread around 1.00).
    near = [k for k, r in ratios.items()
            if 1.05 < r <= 1.10 or (1.0 < r <= 1.05 and set(picks[k]) != {f'fill{FILL[fastest[k]]}'})]
    print(f"1 (deciding). {'HELD' if one else 'FAILED'}: the selector against the fastest fixed fill {ratios} "
          f"(fastest {fastest}; bar 1.05)" + (f'; a second session for {near} (rule 11)' if near else ''))
    two = all(picks[p] and all(c in EXPECTED[p] for c in picks[p]) for p in FIXED)
    print(f"2. {'HELD' if two else 'FAILED'}: its choices {picks} (expected {EXPECTED})")
    flags = {p: [chosen[r['run']].get('full_copy_predicted_faster') for r in rows if r['pattern'] == p
                 and r['arm'] == SELECTOR and r['run'] in chosen] for p in FIXED}
    fixed, full = by.get(('scatterall12', 'fast8w')), by.get(('scatterall12', 'full'))
    full_ratio = round(full['seconds'] / fixed['seconds'], 3) if fixed and full else None
    three = (all(flags[p] and all(f is FULL_FASTER[p] for f in flags[p]) for p in FIXED)
             and full_ratio is not None and full_ratio <= 0.95)
    print(f"3. {'HELD' if three else 'FAILED'}: pg_basebackup predicted faster {flags} (expected {FULL_FASTER}); "
          f"in scatterall12 pg_basebackup / the fixed fill {full_ratio} (bar 0.95)")
    modelled = [c for c in cells if c.get('measured_over_predicted') is not None and c['arm'] != SELECTOR]
    four = (any(c['arm'] == 'full' for c in modelled) and any(c['arm'] in FILL for c in modelled)
            and all(abs(c['measured_over_predicted'] - 1) <= (0.05 if c['arm'] == 'full' else 0.15)
                    for c in modelled))
    print(f"4. {'HELD' if four else 'FAILED'}: measured / predicted "
          f"{[(c['pattern'], c['arm'], c['measured_over_predicted']) for c in modelled]} (fast 15%, full 5%)")
    times = [s.get('seconds') for s in chosen.values()]
    five = bool(times) and all(t is not None and t <= 1.0 for t in times)
    print(f"5. {'HELD' if five else 'FAILED'}: the choice's own time {sorted(times, key=lambda t: t or 0)} s (bar 1.0)")
    fast = [r for r in rows if r['arm'] != 'full']
    six = (bool(rows) and all(r['status'] == 0 for r in rows) and all(r['receiver_status'] == 0 for r in fast)
           and all(r['fast_used'] and r['match'] is True for r in fast))
    print(f"6. {'HELD' if six else 'FAILED'}: statuses {sorted({r['status'] for r in rows})}; fast used and matched "
          f"{all(r['fast_used'] and r['match'] is True for r in fast)} ({len(fast)} runs)")
    keys = []
    for c in cells:
        keys += [k for k in c if k not in keys]
    with open(a.out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, restval='')
        w.writeheader()
        w.writerows(cells)
    figure(by, a.figure)


def figure(by, name):
    """Left: each cell's arms (median of 2), the model's prediction as a line on each fixed arm, the selector's
    prediction for its choice on its bar. Right: the selector's predictions for every option, per cell."""
    plotting.style()
    fig, (ax, bx) = plt.subplots(1, 2, figsize=(11.0, 4.4), gridspec_kw={'width_ratios': [3, 2]})
    colors = {'fast8w': COLORS['sky'], 'fast8wf2': COLORS['purple'], 'full': COLORS['orange'],
              SELECTOR: COLORS['blue']}
    labels = {'fast8w': 'fast path, no fill', 'fast8wf2': 'fast path, fill 2 (the default)',
              'full': 'pg_basebackup', SELECTOR: '--selector'}
    seen = set()
    for i, pattern in enumerate(FIXED):
        for j, arm in enumerate(ARMS[pattern]):
            c = by.get((pattern, arm))
            if not c or c['seconds'] is None:
                continue
            x = i * 4 + j
            ax.bar(x, c['seconds'], 0.8, color=colors[arm], label=None if arm in seen else labels[arm])
            seen.add(arm)
            predicted = c.get('predicted_s')
            if arm == SELECTOR and c.get('choices'):
                predicted = c.get(f"predicted_{c['choices'].split()[0]}_s")
            if predicted:
                ax.plot([x - 0.4, x + 0.4], [predicted] * 2, color=COLORS['black'], lw=1.2,
                        label=None if 'model' in seen else 'model')
                seen.add('model')
            text = f"{c['seconds']:.1f}" + (f"\n({c['choices'].split()[0]})" if arm == SELECTOR else '')
            ax.annotate(text, (x, max(c['seconds'], predicted or 0)), textcoords='offset points', xytext=(0, 3),
                        ha='center', fontsize=8)
    ax.set_xticks([i * 4 + (len(ARMS[p]) - 1) / 2 for i, p in enumerate(FIXED)],
                  [f"{p}\n({(by.get((p, FIXED[p][0])) or {}).get('page_fraction') or 0:.0%} of pages changed)"
                   for p in FIXED])
    ax.set_ylabel('seconds (median of 2)')
    ax.set_ylim(0, max(c['seconds'] or 0 for c in by.values()) * 1.25)  # room for the labels under the title
    ax.legend(fontsize=8, loc='upper left')
    ax.set_title('EXP-75: --selector against the fixed fills and pg_basebackup', fontsize=10)
    options = ('fill0', 'fill1', 'fill2', 'fill4', 'full')
    width = 0.8 / len(FIXED)
    for i, pattern in enumerate(FIXED):
        c = by.get((pattern, SELECTOR)) or {}
        values = [c.get(f'predicted_{o}_s') for o in options]
        pick = (c.get('choices') or '').split()[:1]
        xs = [k + (i - 0.5) * width for k in range(len(options))]
        bx.bar(xs, [v or 0 for v in values], width * 0.9, color=COLORS['blue'] if i == 0 else COLORS['orange'],
               edgecolor=['black' if [o] == pick else 'none' for o in options])
        bx.bar([0], [0], 0, color=COLORS['blue'] if i == 0 else COLORS['orange'], label=pattern)  # a plain legend key
    bx.set_xticks(range(len(options)), ['fill 0', 'fill 1', 'fill 2', 'fill 4', 'full copy'])
    bx.set_ylabel('predicted seconds (outlined: its choice;\nfull copy: pg_basebackup, reported only)')
    bx.set_ylim(0, bx.get_ylim()[1] * 1.2)  # the legend above the bars
    bx.legend(fontsize=8, loc='upper left', ncol=2)
    bx.set_title("the selector's predictions", fontsize=10)
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
