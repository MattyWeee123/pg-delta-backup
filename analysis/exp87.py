"""EXP-87's analysis: the second held-out session (rule 15), on the sealed settings H5-H8, for --lsn-single-read (the
candidate for page-lsn's default), --crashed-standby (the default since K15) and the fast path's defaults
(docs/experiments/EXP-87-held-out-h5-h8.md; docs/research/held-out-workloads.md).

    .venv/bin/python analysis/exp87.py [--results results/exp04] [--out docs/experiments/data/exp87-cells.csv]

Each setting's lab run is results/exp04/exp87-h5 ... exp87-h8 (one run per arm per cell). Arms: fast8wf2c (B0, a standby
killed mid-change, --crashed-standby), sb_fast8wf2 (B0s, a standby stopped cleanly: the fast path's defaults),
sb_lsn1 (B0s, page-lsn with --lsn-single-read), sb_lsn (B0s, page-lsn with two reads) and full (pg_basebackup). Per
cell, scores:
1 single read: used (proof "clean shutdown in recovery"), correct, and at most 1.05x pg_basebackup's time;
2 --crashed-standby: used, proven "crashed standby", correct, and at most max(1.10x, +1.0 s) of the clean standby's time;
3 the fast path's defaults against pg_basebackup: faster exactly where the plan sends under 91% of the pages;
4 every copy matched the source;
5 single read at most 1.024x two reads' time (rule 17's spread; EXP-85: 1.000 in hot, 0.879 in uniform).
Single read is eligible to become page-lsn's default if 1 and 5 hold in every cell. A miss narrows the claim to the
cells where it held. Writes the CSV and the figure exp87-held-out.
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
SETTINGS = {'h5': 'H5: skewed non-HOT updates', 'h6': 'H6: 32 GB of memory', 'h7': 'H7: local NVMe',
            'h8': 'H8: checksums off, wal_log_hints'}
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
        crashed, clean, single, lsn, full = (get('fast8wf2c'), get('sb_fast8wf2'), get('sb_lsn1'), get('sb_lsn'),
                                             get('full'))
        for key, r in (('crashed', crashed), ('clean', clean), ('single', single), ('lsn', lsn), ('full', full)):
            c[f'{key}_s'] = r['seconds'] if r else None
            c[f'{key}_match'] = r['match'] if r and key != 'full' else None
            c[f'{key}_status'] = r['status'] if r else None
        c['crashed_proof'] = crashed['proof'] if crashed else None
        c['crashed_used'] = crashed['fast_used'] if crashed else None
        c['single_proof'] = single['proof'] if single else None
        c['single_used'] = single['fast_used'] if single else None
        one = (stats.get(single['run']) or {}).get('fast_path', {}) if single else {}
        two = (stats.get(lsn['run']) or {}).get('fast_path', {}) if lsn else {}
        c['single_read'], c['single_reread_bytes'] = one.get('lsn_single_read'), one.get('reread_bytes')
        c['single_held_bytes_peak'], c['lsn_reread_bytes'] = one.get('held_bytes_peak'), two.get('reread_bytes')
        clean_stats = (stats.get(clean['run']) or {}) if clean else {}
        pages = clean_stats.get('pages') or {}
        fast = clean_stats.get('fast_path') or {}
        filled = (fast.get('fill_gaps') or {}).get('pages_filled') or 0  # the break-even is on the unfilled plan
        c['pages_chosen_share'] = (round(((fast.get('pages_chosen') or 0) - filled) / pages['total'], 4)
                                   if pages.get('total') else None)
        out.append(c)
    return out


def score(cells):
    verdicts = {k: [] for k in ('1', '2', '3', '4', '5')}
    for c in cells:
        name = f"{c['setting']}/{c['pattern']}"
        if c['single_s'] is not None and c['full_s']:
            ok = (c['single_used'] and c['single_proof'] == 'clean shutdown in recovery' and c['single_match'] is True
                  and c['single_read'] is True and c['single_reread_bytes'] == 0 and c['single_s'] <= 1.05 * c['full_s'])
            verdicts['1'].append((name, ok, f"{c['single_s']} s, {c['single_s'] / c['full_s']:.3f}x pg_basebackup; "
                                            f"held at most {c['single_held_bytes_peak']} bytes"))
        if c['crashed_s'] is not None and c['clean_s'] is not None:
            ok = (c['crashed_used'] and c['crashed_proof'] == 'crashed standby' and c['crashed_match'] is True
                  and c['crashed_s'] <= max(1.10 * c['clean_s'], c['clean_s'] + 1.0))
            verdicts['2'].append((name, ok, f"{c['crashed_s']} s against {c['clean_s']} s clean; proof "
                                            f"{c['crashed_proof']}"))
        if c['clean_s'] is not None and c['full_s'] and c['pages_chosen_share'] is not None:
            faster = c['clean_s'] < c['full_s']
            ok = faster == (c['pages_chosen_share'] < BREAK_EVEN)
            verdicts['3'].append((name, ok, f"{c['clean_s'] / c['full_s']:.3f}x pg_basebackup at "
                                            f"{c['pages_chosen_share']:.1%} of the pages"))
        matches = [c[f'{k}_match'] for k in ('crashed', 'clean', 'single', 'lsn') if c[f'{k}_s'] is not None]
        statuses = [c[f'{k}_status'] for k in ('crashed', 'clean', 'single', 'lsn', 'full') if c[f'{k}_s'] is not None]
        verdicts['4'].append((name, all(m is True for m in matches) and all(s == 0 for s in statuses),
                              f"{sum(m is True for m in matches)} of {len(matches)} copies matched"))
        if c['single_s'] is not None and c['lsn_s']:
            verdicts['5'].append((name, c['single_s'] <= 1.024 * c['lsn_s'],
                                  f"{c['single_s'] / c['lsn_s']:.3f}x two reads ({c['lsn_s']} s; re-read "
                                  f"{c['lsn_reread_bytes']} bytes)"))
    labels = {'1': 'single read (--lsn-single-read)', '2': '--crashed-standby',
              '3': 'the fast path against pg_basebackup', '4': 'correctness', '5': 'single read against two reads'}
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
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp87-cells.csv'))
    ap.add_argument('--figure', default='exp87-held-out')
    a = ap.parse_args(argv)
    cells = []
    for setting in SETTINGS:
        d = Path(a.results) / f'exp87-{setting}'
        if d.exists():
            cells += cells_of(setting, d)
    score(cells)
    exp76.write_csv(a.out, cells)
    figure(cells, a.figure)


def figure(cells, name):
    plotting.style()
    fig, ax = plt.subplots(figsize=(11.5, 4.4))
    arms = (('clean', 'fast path (defaults)', COLORS['blue']), ('crashed', '--crashed-standby', COLORS['sky']),
            ('single', 'page-lsn, single read', COLORS['green']), ('lsn', 'page-lsn, two reads', COLORS['purple']))
    labelled = set()
    for i, c in enumerate(cells):
        if not c['full_s']:
            continue
        for j, (key, label, color) in enumerate(arms):
            if c[f'{key}_s'] is None:
                continue
            ax.bar(i * 5 + j, c[f'{key}_s'] / c['full_s'], 0.8, color=color,
                   label=label if label not in labelled else None)
            labelled.add(label)
    ax.axhline(1.0, color=COLORS['orange'], lw=1.2, label='pg_basebackup')
    ax.set_xticks([i * 5 + 1.5 for i in range(len(cells))],
                  [f"{c['setting'].upper()}\n{c['pattern']}" for c in cells], fontsize=8)
    ax.set_ylabel("time over pg_basebackup's, one run each")
    ax.axhline(1.05, color=COLORS['grey'], lw=1, ls='--', label='single read\'s bar (1.05)')
    ax.set_title('EXP-87: the held-out settings H5-H8 (rule 15)', fontsize=10)
    ax.set_ylim(0, max(1.45, max((c[f'{k}_s'] or 0) / c['full_s'] for c in cells if c['full_s'] for k, *_ in arms) * 1.3))
    ax.legend(fontsize=7.5, loc='upper left', ncol=3)
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
