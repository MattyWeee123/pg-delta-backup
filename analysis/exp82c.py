"""EXP-82c's analysis: summaries on demand on the VMs, on a source that never summarized WAL (EXP-82b's on-demand runs
were void), with page-lsn and hashing beside it (docs/experiments/EXP-82c-summaries-on-demand-vms.md).

    .venv/bin/python analysis/exp82c.py [--results results/exp04/exp82c]

Arms on B0s (a standby stopped cleanly before the change): sb_hash, sb_lsn, sb_od. Cells hot and uniform; medians of
the runs (exp68.runs). Scores 1 (deciding: hot's on demand at most 0.20 of hashing's time), 2 (uniform's on demand at
most 0.90 of hashing's), 3 (page-lsn, a second session: at most 0.70 of hashing's in uniform and 1.05 in hot) and 4
(the source reported no summarizer at set-up; every run exits 0 and every copy matches; on demand used the summaries'
fast path, with summaries, in every run; page-lsn used in every run). Writes docs/experiments/data/exp82c-cells.csv
and the figure exp82c-on-demand.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import exp68
import exp76
import exp82b

ROOT = Path(__file__).resolve().parents[1]
CELLS = ('hot', 'uniform')
ARMS = ('sb_hash', 'sb_lsn', 'sb_od')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp82c'))
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp82c-cells.csv'))
    ap.add_argument('--figure', default='exp82c-on-demand')
    a = ap.parse_args(argv)
    rows, meta, fast = exp68.runs(a.results), exp82b.metas(a.results), exp82b.sends(a.results)
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
                          'source_read_mb': exp68.median(mine, 'source_read_mb'),
                          'dest_write_mb': exp68.median(mine, 'dest_write_mb'),
                          'source_io_s': exp68.median(mine, 'source_io_s'), 'dest_io_s': exp68.median(mine, 'dest_io_s')})
            print(cells[-1])
    by = {(c['pattern'], c['arm']): c for c in cells}

    def ratio(p, ours, theirs):
        return (round(by[(p, ours)]['seconds'] / by[(p, theirs)]['seconds'], 3)
                if (p, ours) in by and (p, theirs) in by else None)

    one = ratio('hot', 'sb_od', 'sb_hash')
    print(f"1 (deciding). {'HELD' if one is not None and one <= 0.20 else 'FAILED'}: hot, on demand / hashing {one} "
          f"(bar 0.20)" + ('; within 0.05 of the bar (rule 11)' if one is not None and abs(one - 0.20) <= 0.05 else ''))
    two = ratio('uniform', 'sb_od', 'sb_hash')
    print(f"2. {'HELD' if two is not None and two <= 0.90 else 'FAILED'}: uniform, on demand / hashing {two} (bar 0.90)")
    lsn = {p: ratio(p, 'sb_lsn', 'sb_hash') for p in CELLS}
    three = lsn['uniform'] is not None and lsn['hot'] is not None and lsn['uniform'] <= 0.70 and lsn['hot'] <= 1.05
    print(f"3. {'HELD' if three else 'FAILED'}: page-lsn / hashing {lsn} (bars 0.70 uniform, 1.05 hot)")
    source = json.loads((Path(a.results) / 'source.json').read_text()) if (Path(a.results) / 'source.json').exists() else {}
    never = source.get('summarized_lsn') in ('0/0', '', None)
    checked = [r for r in rows if r['arm'] != 'full']
    od = [r for r in rows if r['arm'] == 'sb_od']
    page = [r for r in rows if r['arm'] == 'sb_lsn']
    four = (never and bool(od) and bool(page) and all(r['status'] == 0 for r in rows)
            and all(r['match'] is True for r in checked)
            and all(r['fast_used'] and (fast.get(r['run'], {}).get('summaries') or 0) > 0 for r in od)
            and all(r['fast_used'] and r['proof'] == 'clean shutdown in recovery' for r in page))
    print(f"4. {'HELD' if four else 'FAILED'}: summarizer at set-up {source.get('summarized_lsn')!r}; statuses "
          f"{sorted({r['status'] for r in rows})}; copies matched {sum(r['match'] is True for r in checked)} of "
          f"{len(checked)}; on demand used {sum(bool(r['fast_used']) for r in od)} of {len(od)}; page-lsn used "
          f"{sum(bool(r['fast_used']) for r in page)} of {len(page)}")
    for p in CELLS:
        print(f"reported: {p}: on demand / page-lsn {ratio(p, 'sb_od', 'sb_lsn')}")
    for r in od:
        ph = meta[r['run']].get('phases') or {}
        print(f"reported: on demand run {r['run']} {r['pattern']}: summarize {ph.get('summarize_s')} s, refresh "
              f"{ph.get('refresh_s')} s, summaries {fast.get(r['run'], {}).get('summaries')}, summary rows "
              f"{fast.get(r['run'], {}).get('summary_rows')}")
    exp76.write_csv(a.out, cells)
    exp82b.figure(by, ratio, a.figure)


if __name__ == '__main__':
    main()
