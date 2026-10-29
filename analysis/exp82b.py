"""EXP-82b's analysis: summaries off on the VMs beyond memory: --fast-path page-lsn and summaries on demand against
hashing and WAL-G's catch-up (docs/experiments/EXP-82b-summaries-off-vms.md).

    .venv/bin/python analysis/exp82b.py [--results results/exp04/exp82b]

Arms, all on B0s (a standby stopped cleanly before the change): sb_hash (today: no fast path), sb_lsn (page-lsn),
sb_od (summarize_wal turned on, the summarizer waited for, then the summaries' fast path; timed from before the
setting), sb_walg (WAL-G 3.0.9's catch-up plus a sync) and full (pg_basebackup). Cells hot and uniform; medians of the
runs (exp68.runs). Scores 1 (deciding: uniform's page-lsn at most 0.70 of hashing's time), 2 (hot's page-lsn at most
1.05 of hashing's), 3 (page-lsn at most 1.05 of WAL-G's in hot and 0.95 in uniform), 4 (hot's on demand at most 0.20
of hashing's) and 5 (every run exits 0 and every copy matches; page-lsn proven by "clean shutdown in recovery" and
used in every run; on demand used with summaries in every run). Writes docs/experiments/data/exp82b-cells.csv and the
figure exp82b-summaries-off.
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
CELLS = ('hot', 'uniform')
ARMS = ('sb_hash', 'sb_lsn', 'sb_od', 'sb_walg', 'full')
LABELS = {'sb_hash': 'hashing (today)', 'sb_lsn': 'page-lsn', 'sb_od': 'summaries on demand',
          'sb_walg': 'WAL-G catch-up', 'full': 'pg_basebackup'}


def metas(results):
    return {m['run']: m for m in (json.loads(p.read_text()) for p in sorted(Path(results).glob('run-*/meta.json')))}


def sends(results):
    """{run: the sender's fast_path statistics} where send.json has them."""
    out = {}
    for path in sorted(Path(results).glob('run-*/send.json')):
        if path.stat().st_size:
            meta = json.loads((path.parent / 'meta.json').read_text())
            out[meta['run']] = json.loads(path.read_text()).get('fast_path') or {}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp82b'))
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp82b-cells.csv'))
    ap.add_argument('--figure', default='exp82b-summaries-off')
    a = ap.parse_args(argv)
    rows, meta, fast = exp68.runs(a.results), metas(a.results), sends(a.results)
    cells = []
    for pattern in CELLS:
        for arm in ARMS:
            mine = [r for r in rows if r['pattern'] == pattern and r['arm'] == arm]
            if not mine:
                continue
            phases = [meta[r['run']].get('phases') or {} for r in mine]
            cells.append({'pattern': pattern, 'arm': arm, 'runs': len(mine), 'seconds': exp68.median(mine, 'seconds'),
                          'pages_sent': exp68.median(mine, 'pages_sent'),
                          'summarize_s': exp68.median([{'v': p.get('summarize_s')} for p in phases], 'v'),
                          'refresh_s': exp68.median([{'v': p.get('refresh_s')} for p in phases], 'v'),
                          'scan_s': exp68.median([{'v': fast.get(r['run'], {}).get('scan_seconds')} for r in mine], 'v'),
                          'source_read_mb': exp68.median(mine, 'source_read_mb'),
                          'dest_write_mb': exp68.median(mine, 'dest_write_mb'),
                          'source_io_s': exp68.median(mine, 'source_io_s'), 'dest_io_s': exp68.median(mine, 'dest_io_s')})
            print(cells[-1])
    by = {(c['pattern'], c['arm']): c for c in cells}

    def ratio(p, ours, theirs):
        return (round(by[(p, ours)]['seconds'] / by[(p, theirs)]['seconds'], 3)
                if (p, ours) in by and (p, theirs) in by else None)

    one = ratio('uniform', 'sb_lsn', 'sb_hash')
    print(f"1 (deciding). {'HELD' if one is not None and one <= 0.70 else 'FAILED'}: uniform, page-lsn / hashing {one} "
          f"(bar 0.70)" + ('; within 0.05 of the bar (rule 11)' if one is not None and abs(one - 0.70) <= 0.05 else ''))
    two = ratio('hot', 'sb_lsn', 'sb_hash')
    print(f"2. {'HELD' if two is not None and two <= 1.05 else 'FAILED'}: hot, page-lsn / hashing {two} (bar 1.05)")
    walg = {p: ratio(p, 'sb_lsn', 'sb_walg') for p in CELLS}
    three = walg['hot'] is not None and walg['uniform'] is not None and walg['hot'] <= 1.05 and walg['uniform'] <= 0.95
    print(f"3. {'HELD' if three else 'FAILED'}: page-lsn / WAL-G {walg} (bars 1.05 hot, 0.95 uniform)")
    four = ratio('hot', 'sb_od', 'sb_hash')
    print(f"4. {'HELD' if four is not None and four <= 0.20 else 'FAILED'}: hot, on demand / hashing {four} (bar 0.20)")
    checked = [r for r in rows if r['arm'] != 'full']
    lsn = [r for r in rows if r['arm'] == 'sb_lsn']
    od = [r for r in rows if r['arm'] == 'sb_od']
    five = (bool(rows) and bool(lsn) and bool(od) and all(r['status'] == 0 for r in rows) and all(r['receiver_status'] in (0, None) for r in rows)
            and all(r['match'] is True for r in checked)
            and all(r['fast_used'] and r['proof'] == 'clean shutdown in recovery' for r in lsn)
            and all(r['fast_used'] and (fast.get(r['run'], {}).get('summaries') or 0) > 0 for r in od))
    print(f"5. {'HELD' if five else 'FAILED'}: statuses {sorted({r['status'] for r in rows})}; copies matched "
          f"{sum(r['match'] is True for r in checked)} of {len(checked)}; page-lsn used {sum(bool(r['fast_used']) for r in lsn)} "
          f"of {len(lsn)}; on demand used {sum(bool(r['fast_used']) for r in od)} of {len(od)}")
    for p in CELLS:
        print(f"reported: {p}: " + ', '.join(f"{arm} over hashing {ratio(p, arm, 'sb_hash')}" for arm in ARMS[1:])
              + f"; on demand / page-lsn {ratio(p, 'sb_od', 'sb_lsn')}")
    for r in od:
        ph = meta[r['run']].get('phases') or {}
        print(f"reported: on demand run {r['run']} {r['pattern']}: summarize {ph.get('summarize_s')} s from "
              f"{ph.get('wal_from')}, refresh {ph.get('refresh_s')} s, summaries {fast.get(r['run'], {}).get('summaries')}")
    exp76.write_csv(a.out, cells)
    figure(by, ratio, a.figure)


def figure(by, ratio, name):
    plotting.style()
    fig, ax = plt.subplots(figsize=(10.0, 4.4))
    colors = {'sb_hash': COLORS['grey'], 'sb_lsn': COLORS['blue'], 'sb_od': COLORS['green'],
              'sb_walg': COLORS['purple'], 'full': COLORS['orange']}
    for i, pattern in enumerate(CELLS):
        for j, arm in enumerate(ARMS):
            c = by.get((pattern, arm))
            if not c:
                continue
            x = i * (len(ARMS) + 1) + j
            if arm == 'sb_od' and c.get('summarize_s') is not None and c.get('refresh_s') is not None:
                first = c['seconds'] * c['summarize_s'] / (c['summarize_s'] + c['refresh_s'])
                ax.bar(x, first, 0.8, color=colors[arm], label=LABELS[arm] + ': summarize + refresh' if i == 0 else None)
                ax.bar(x, c['seconds'] - first, 0.8, bottom=first, color=colors[arm], alpha=0.45)
            else:
                ax.bar(x, c['seconds'], 0.8, color=colors[arm], label=LABELS[arm] if i == 0 else None)
            ax.annotate(f"{c['seconds']:.1f} s", (x, c['seconds']), textcoords='offset points', xytext=(0, 3),
                        ha='center', fontsize=8)
    ax.set_xticks([i * (len(ARMS) + 1) + 2 for i in range(len(CELLS))],
                  [f"{p}\n(page-lsn / hashing {ratio(p, 'sb_lsn', 'sb_hash')}; / WAL-G {ratio(p, 'sb_lsn', 'sb_walg')};\n"
                   f"on demand / hashing {ratio(p, 'sb_od', 'sb_hash')})" for p in CELLS], fontsize=8.5)
    ax.set_ylabel('seconds to refresh an 18 GB standby\n(median of 2)')
    ax.set_ylim(0, ax.get_ylim()[1] * 1.3)
    ax.legend(fontsize=7.5, loc='upper left', ncol=2)
    ax.set_title('EXP-82b: a server that never summarized WAL, beyond memory', fontsize=10)
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
