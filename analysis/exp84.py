"""EXP-84's analysis: the held-out session (rule 15) for --crashed-standby, --selector and page-lsn on the sealed
settings H1-H4 (docs/experiments/EXP-84-held-out.md; docs/research/held-out-workloads.md).

    .venv/bin/python analysis/exp84.py [--results results/exp04] [--out docs/experiments/data/exp84-cells.csv]

Each setting's lab run is results/exp04/exp84-h1 ... exp84-h4 (one run per arm per cell). Arms: fast8wf2c (B0, a standby
killed mid-change, --crashed-standby), sb_fast8wf2 (B0s, a standby stopped cleanly: the fast path's defaults),
sb_fast8ws (B0s, --selector), sb_lsn (B0s, page-lsn) and full (pg_basebackup). Per cell, scores:
1 --crashed-standby: used, proven "crashed standby", correct, and at most max(1.10x, +1.0 s) of the clean standby's time;
2 --selector: its time model's prediction for the fill it chose within 15% of the measured time (H1, H2, H4), or at least
  1.15x it (H3, on pd-ssd, where its pd-balanced disk limits are too slow: an expected miss of transfer); and its call
  on pg_basebackup (full_copy_predicted_faster) matching the measured order of the two;
3 page-lsn: used (proof "clean shutdown in recovery"), correct, and at most 1.05x pg_basebackup's time;
4 the fast path's defaults against pg_basebackup: faster exactly where the plan sends under 91% of the pages (the
  break-even, EXP-79b);
5 every copy matched the source.
A miss narrows that candidate's claim to the cells where it held. Writes the CSV and the figure exp84-held-out.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import exp68
import exp76
import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = {'h1': 'H1: workloads', 'h2': 'H2: scale 1,200', 'h3': 'H3: pd-ssd', 'h4': 'H4: PostgreSQL 18'}
BREAK_EVEN = 0.91


def sends(results):
    out = {}
    for path in sorted(Path(results).glob('run-*/send.json')):
        if path.stat().st_size:
            meta = json.loads((path.parent / 'meta.json').read_text())
            out[meta['run']] = json.loads(path.read_text())
    return out


def cells_of(setting, results):
    rows, stats = exp68.runs(results), sends(results)
    by = {}
    for r in rows:
        by.setdefault(r['pattern'], {})[r['arm']] = r
    out = []
    for pattern, arms in by.items():
        c = {'setting': setting, 'pattern': pattern}
        get = lambda arm: arms.get(arm)
        crashed, clean, sel, lsn, full = (get('fast8wf2c'), get('sb_fast8wf2'), get('sb_fast8ws'), get('sb_lsn'),
                                          get('full'))
        for key, r in (('crashed', crashed), ('clean', clean), ('selector', sel), ('lsn', lsn), ('full', full)):
            c[f'{key}_s'] = r['seconds'] if r else None
            c[f'{key}_match'] = r['match'] if r and key != 'full' else None
            c[f'{key}_status'] = r['status'] if r else None
        c['crashed_proof'] = crashed['proof'] if crashed else None
        c['crashed_used'] = crashed['fast_used'] if crashed else None
        c['lsn_proof'] = lsn['proof'] if lsn else None
        c['lsn_used'] = lsn['fast_used'] if lsn else None
        s = (stats.get(sel['run']) or {}).get('fast_path', {}).get('selector') if sel else None
        if s:
            c['selector_choice'] = s.get('choice')
            predicted = s.get('predicted_seconds') or {}
            c['selector_predicted_s'] = predicted.get(s.get('choice'))
            c['selector_full_predicted_s'] = predicted.get('full')
            c['selector_full_faster'] = s.get('full_copy_predicted_faster')
        clean_stats = (stats.get(clean['run']) or {}) if clean else {}
        pages = clean_stats.get('pages') or {}
        fast = clean_stats.get('fast_path') or {}
        filled = (fast.get('fill_gaps') or {}).get('pages_filled') or 0  # the break-even is on the unfilled plan
        c['pages_chosen_share'] = (round(((fast.get('pages_chosen') or 0) - filled) / pages['total'], 4)
                                   if pages.get('total') else None)
        out.append(c)
    return out


def score(cells):
    verdicts = {k: [] for k in range(1, 6)}
    for c in cells:
        name = f"{c['setting']}/{c['pattern']}"
        if c['crashed_s'] is not None and c['clean_s'] is not None:
            ok = (c['crashed_used'] and c['crashed_proof'] == 'crashed standby' and c['crashed_match'] is True
                  and c['crashed_s'] <= max(1.10 * c['clean_s'], c['clean_s'] + 1.0))
            verdicts[1].append((name, ok, f"{c['crashed_s']} s against {c['clean_s']} s clean; proof {c['crashed_proof']}"))
        if c.get('selector_predicted_s') and c['selector_s']:
            error = c['selector_predicted_s'] / c['selector_s']
            within = (error >= 1.15) if c['setting'] == 'h3' else (abs(error - 1) <= 0.15)
            call = (c['full_s'] is not None and bool(c['selector_full_faster']) == (c['full_s'] < c['selector_s']))
            verdicts[2].append((name, within and call, f"{c['selector_choice']}: predicted {c['selector_predicted_s']} s, "
                                f"measured {c['selector_s']} s ({error:.3f}x); pg_basebackup predicted faster "
                                f"{c['selector_full_faster']}, measured {c['full_s']} s"))
        if c['lsn_s'] is not None and c['full_s']:
            ok = (c['lsn_used'] and c['lsn_proof'] == 'clean shutdown in recovery' and c['lsn_match'] is True
                  and c['lsn_s'] <= 1.05 * c['full_s'])
            verdicts[3].append((name, ok, f"{c['lsn_s']} s, {c['lsn_s'] / c['full_s']:.3f}x pg_basebackup"))
        if c['clean_s'] is not None and c['full_s'] and c['pages_chosen_share'] is not None:
            faster = c['clean_s'] < c['full_s']
            ok = faster == (c['pages_chosen_share'] < BREAK_EVEN)
            verdicts[4].append((name, ok, f"{c['clean_s'] / c['full_s']:.3f}x pg_basebackup at "
                                f"{c['pages_chosen_share']:.1%} of the pages"))
        matches = [c[f'{k}_match'] for k in ('crashed', 'clean', 'selector', 'lsn') if c[f'{k}_s'] is not None]
        statuses = [c[f'{k}_status'] for k in ('crashed', 'clean', 'selector', 'lsn', 'full') if c[f'{k}_s'] is not None]
        verdicts[5].append((name, all(m is True for m in matches) and all(s == 0 for s in statuses),
                            f"{sum(m is True for m in matches)} of {len(matches)} copies matched"))
    labels = {1: '--crashed-standby', 2: '--selector', 3: 'page-lsn', 4: 'the fast path against pg_basebackup',
              5: 'correctness'}
    for k, items in verdicts.items():
        held = [n for n, ok, _ in items if ok]
        print(f"{k}. {labels[k]}: {'HELD' if items and len(held) == len(items) else 'FAILED'} in {len(held)} of "
              f"{len(items)} cells")
        for n, ok, detail in items:
            print(f"   {'held  ' if ok else 'MISSED'} {n}: {detail}")
    return verdicts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04'))
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp84-cells.csv'))
    ap.add_argument('--figure', default='exp84-held-out')
    a = ap.parse_args(argv)
    cells = []
    for setting in SETTINGS:
        d = Path(a.results) / f'exp84-{setting}'
        if d.exists():
            cells += cells_of(setting, d)
    score(cells)
    exp76.write_csv(a.out, cells)
    figure(cells, a.figure)


def figure(cells, name):
    plotting.style()
    fig, ax = plt.subplots(figsize=(11.5, 4.4))
    arms = (('clean', 'fast path (defaults)', COLORS['blue']), ('crashed', '--crashed-standby', COLORS['sky']),
            ('selector', '--selector', COLORS['green']), ('lsn', 'page-lsn', COLORS['purple']))
    for i, c in enumerate(cells):
        if not c['full_s']:
            continue
        for j, (key, label, color) in enumerate(arms):
            if c[f'{key}_s'] is None:
                continue
            ax.bar(i * 5 + j, c[f'{key}_s'] / c['full_s'], 0.8, color=color, label=label if i == 0 else None)
    ax.axhline(1.0, color=COLORS['orange'], lw=1.2, label='pg_basebackup')
    ax.set_xticks([i * 5 + 1.5 for i in range(len(cells))],
                  [f"{c['setting'].upper()}\n{c['pattern']}" for c in cells], fontsize=8)
    ax.set_ylabel("time over pg_basebackup's, one run each")
    ax.set_title('EXP-84: the held-out settings (rule 15)', fontsize=10)
    ax.legend(fontsize=7.5, loc='upper left', ncol=5)
    ax.set_ylim(0, max(1.3, max((c[f'{k}_s'] or 0) / c['full_s'] for c in cells if c['full_s'] for k, *_ in arms) * 1.2))
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
