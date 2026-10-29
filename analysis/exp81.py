"""EXP-81's analysis: the fast path against the strongest competitors, each in its best mode, beyond memory
(docs/experiments/EXP-81-competitors.md).

    .venv/bin/python analysis/exp81.py [--results results/exp04/exp81]

Two situations, in the cells hot and uniform, dest's /data on XFS with reflinks:
- a backup chain (B0 an untouched base backup with its manifest): fast8wf2 (the fast path) against incr (native
  incremental: pg_basebackup --incremental, then pg_combinebackup --copy-file-range), with full (pg_basebackup);
- a standby behind (B0s, a standby stopped cleanly before the change): sb_fast8wf2 against sb_walg (WAL-G's
  catchup-send and catchup-receive, then a sync).

Cells: per pattern and arm, the median of the runs (exp68.runs), with each competitor's phases. Scores 1 (deciding:
fast8wf2 at most 0.80 of incr's time in both cells), 2 (sb_fast8wf2 at most 0.80 of sb_walg's in both cells) and 3
(every run exits 0; every copy, incr's and walg's too, matches the source; fast8wf2 proven by backup_manifest and
sb_fast8wf2 by "clean shutdown in recovery"). Writes docs/experiments/data/exp81-cells.csv and the figure
exp81-competitors.
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
CELLS = ('hot', 'uniform')
ARMS = ('fast8wf2', 'incr', 'full', 'sb_fast8wf2', 'sb_walg')
PAIRS = {1: ('fast8wf2', 'incr'), 2: ('sb_fast8wf2', 'sb_walg')}
BAR = 0.80
PROOF = {'fast8wf2': 'backup_manifest', 'sb_fast8wf2': 'clean shutdown in recovery'}
PHASES = ('capture_s', 'combine_s', 'incremental_bytes', 'catchup_s', 'sync_s')


def metas(results):
    """{run: meta.json} for every run."""
    return {m['run']: m for m in (json.loads(p.read_text()) for p in sorted(Path(results).glob('run-*/meta.json')))}


def middle(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 3) if values else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp81'))
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp81-cells.csv'))
    ap.add_argument('--figure', default='exp81-competitors')
    a = ap.parse_args(argv)
    rows, meta = exp68.runs(a.results), metas(a.results)
    cells = []
    for pattern in CELLS:
        for arm in ARMS:
            mine = [r for r in rows if r['pattern'] == pattern and r['arm'] == arm]
            if not mine:
                continue
            phases = [meta[r['run']].get('phases') or {} for r in mine]
            cells.append({'pattern': pattern, 'arm': arm, 'runs': len(mine),
                          'seconds': exp68.median(mine, 'seconds'), 'pages_sent': exp68.median(mine, 'pages_sent'),
                          **{k: middle([p.get(k) for p in phases]) for k in PHASES},
                          'net_rx_mb': middle([meta[r['run']].get('net_rx_bytes', 0) / 1e6 for r in mine]),
                          'source_read_mb': exp68.median(mine, 'source_read_mb'),
                          'dest_write_mb': exp68.median(mine, 'dest_write_mb'),
                          'source_io_s': exp68.median(mine, 'source_io_s'), 'dest_io_s': exp68.median(mine, 'dest_io_s')})
            print(cells[-1])
    by = {(c['pattern'], c['arm']): c for c in cells}
    ratios = {}
    for n, (ours, theirs) in PAIRS.items():
        ratio = {p: round(by[(p, ours)]['seconds'] / by[(p, theirs)]['seconds'], 3) for p in CELLS
                 if (p, ours) in by and (p, theirs) in by}
        ratios[n] = ratio
        held = len(ratio) == len(CELLS) and all(v <= BAR for v in ratio.values())
        near = [p for p, v in ratio.items() if abs(v - BAR) <= 0.05]
        print(f"{n}{' (deciding)' if n == 1 else ''}. {'HELD' if held else 'FAILED'}: {ours} / {theirs} {ratio} "
              f"(bar {BAR})" + (f'; within 0.05 of the bar in {near} (rule 11)' if near else ''))
    proofs = {r['run']: r['proof'] for r in rows}
    checked = [r for r in rows if r['arm'] != 'full']
    three = (bool(rows) and all(r['status'] == 0 for r in rows)
             and all(r['receiver_status'] in (0, None) for r in rows)
             and all(r['match'] is True for r in checked)
             and all(proofs.get(r['run']) == PROOF[r['arm']] and r['fast_used'] for r in rows if r['arm'] in PROOF))
    print(f"3. {'HELD' if three else 'FAILED'}: statuses {sorted({r['status'] for r in rows})}; copies matched "
          f"{sum(r['match'] is True for r in checked)} of {len(checked)}; proofs "
          f"{sorted({str(proofs.get(r['run'])) for r in rows if r['arm'] in PROOF})}")
    for p in CELLS:
        full = by.get((p, 'full'))
        if full:
            print(f"reported: {p}: over pg_basebackup " + ', '.join(
                f"{arm} {round(by[(p, arm)]['seconds'] / full['seconds'], 3)}" for arm in ARMS
                if arm != 'full' and (p, arm) in by))
    exp76.write_csv(a.out, cells)
    figure(by, ratios, a.figure)


def figure(by, ratios, name):
    plotting.style()
    fig, axes = plt.subplots(1, 2, figsize=(11.0, 4.4), sharey=False)
    panels = [('A backup chain (B0: a base backup with its manifest)',
               [('fast8wf2', 'fast path', COLORS['blue'], None),
                ('incr', 'native incremental:\ncapture + combine', COLORS['green'], ('capture_s', 'combine_s')),
                ('full', 'pg_basebackup', COLORS['orange'], None)], 1),
              ('A standby behind (B0: a standby stopped cleanly)',
               [('sb_fast8wf2', 'fast path', COLORS['blue'], None),
                ('sb_walg', 'WAL-G catch-up:\nsend + sync', COLORS['purple'], ('catchup_s', 'sync_s')),
                ('full', 'pg_basebackup', COLORS['orange'], None)], 2)]
    for ax, (title, arms, n) in zip(axes, panels):
        for i, pattern in enumerate(CELLS):
            for j, (arm, label, color, split) in enumerate(arms):
                c = by.get((pattern, arm))
                if not c:
                    continue
                x = i * (len(arms) + 1) + j
                if split and c.get(split[0]) is not None and c.get(split[1]) is not None:
                    first = c['seconds'] * c[split[0]] / (c[split[0]] + c[split[1]])
                    ax.bar(x, first, 0.8, color=color, label=label if i == 0 else None)
                    ax.bar(x, c['seconds'] - first, 0.8, bottom=first, color=color, alpha=0.45)
                else:
                    ax.bar(x, c['seconds'], 0.8, color=color, label=label if i == 0 else None)
                ax.annotate(f"{c['seconds']:.1f} s", (x, c['seconds']), textcoords='offset points', xytext=(0, 3),
                            ha='center', fontsize=8)
        ax.set_xticks([i * (len(arms) + 1) + 1 for i in range(len(CELLS))],
                      [f"{p}\n(fast path / theirs: {ratios.get(n, {}).get(p)})" for p in CELLS], fontsize=9)
        ax.set_title(title, fontsize=9.5)
        ax.set_ylim(0, ax.get_ylim()[1] * 1.3)  # room for the legend above the bars
        ax.legend(fontsize=7.5, loc='upper left')
    axes[0].set_ylabel('seconds to a refreshed, synced 18 GB copy\n(median of 2; lighter: second phase)')
    fig.suptitle('EXP-81: the fast path against the strongest competitors, each in its best mode (dest on XFS)',
                 fontsize=10)
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
