"""EXP-47's analysis: with decompression pipelined on the receiver too, where does zstd pay?
(docs/experiments/EXP-47-zstd-receiver-pool.md)

    .venv/bin/python analysis/exp47.py [--sweep results/exp04/exp47] [--figdir DIR]

Uniform 1% at 2 and 1 Gbit/s: the median time of each method, zstd / v1 (prediction 1 at 2 Gbit/s, 3 at 1), the
bytes and pg_basebackup (4); from pgdelta's statistics, BBR on every v1 run and the receiver's wait for decompressed
runs (2). The figure plots zstd / v1 against the link rate, with EXP-46's two sessions (the sender's pool only) at
0.5 and 2 Gbit/s beside this run's. Writes docs/experiments/data/exp47-cells.csv and docs/figures/exp47-zstd-rate.svg.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
FULL, V1, V1Z = 'full', 'pgdelta_direct_inplace_bbr', 'pgdelta_direct_inplace_bbr_z'


def medians(sweeps, pattern='uniform'):
    times, mbs = {}, {}
    for sweep in sweeps:
        for r in csv.DictReader(open(Path(sweep) / 'summary.csv')):
            if r['status'] == 'pass' and r['pattern'] == pattern:
                times.setdefault((r['speed'], r['method']), []).append(float(r['total_s']))
                if r.get('wire_s2d_bytes'):
                    mbs.setdefault((r['speed'], r['method']), []).append(float(r['wire_s2d_bytes']) / 1e6)
    return ({k: statistics.median(v) for k, v in times.items()}, {k: statistics.median(v) for k, v in mbs.items()},
            {k: len(v) for k, v in times.items()})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--sweep', action='append', help='repeat for the K10 session: the cells use every run')
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    sweeps = a.sweep or [str(ROOT / 'results' / 'exp04' / 'exp47')]
    t, mb, n = medians(sweeps)
    rows = []
    for condition in ('2:60:tuned', '1:60:tuned'):
        row = {'condition': condition, **{f'{name}_s': round(t[(condition, m)], 2) if (condition, m) in t else None
                                          for m, name in ((FULL, 'full'), (V1, 'v1'), (V1Z, 'v1_zstd'))}}
        row.update({f'{name}_runs': n.get((condition, m), 0) for m, name in ((FULL, 'full'), (V1, 'v1'), (V1Z, 'v1_zstd'))})
        row['v1_mb'], row['v1_zstd_mb'] = (round(mb.get((condition, V1), 0), 1), round(mb.get((condition, V1Z), 0), 1))
        row['zstd_over_v1'] = round(row['v1_zstd_s'] / row['v1_s'], 3) if row['v1_s'] and row['v1_zstd_s'] else None
        row['zstd_over_full'] = round(row['v1_zstd_s'] / row['full_s'], 3) if row['full_s'] and row['v1_zstd_s'] else None
        rows.append(row)
        print(row)
    two, one = rows
    print(f"1 (deciding). {'HELD' if two['zstd_over_v1'] <= 0.90 else 'FAILED'}: 2 Gbit/s, at most 0.90 of v1: "
          f"{two['zstd_over_v1']}" + (' (within 20% of the bar)' if abs(two['zstd_over_v1'] / 0.90 - 1) < 0.2 else ''))
    print(f"3. {'HELD' if one['zstd_over_v1'] <= 0.75 else 'FAILED'}: 1 Gbit/s, at most 0.75 of v1: {one['zstd_over_v1']}")
    ok4 = all(r['v1_mb'] / r['v1_zstd_mb'] >= 2 and r['zstd_over_full'] < 1 for r in rows)
    print(f"4. {'HELD' if ok4 else 'FAILED'}: bytes {[round(r['v1_mb'] / r['v1_zstd_mb'], 2) for r in rows]}, "
          f"against pg_basebackup {[r['zstd_over_full'] for r in rows]}")
    effective, waits = [], []
    for log in sorted(log for sweep in sweeps for log in Path(sweep).glob('logs/*/source/*-stats.json.log')):
        text = log.read_text()
        stats = json.loads(text[text.find('{'):])
        effective.append((stats.get('congestion_control') or {}).get('effective'))
        received = (stats.get('receiver') or {}).get('compress')
        if received and received.get('accepted'):
            waits.append((round(received['wait_seconds'], 2), round(received['seconds'], 2), received['threads']))
    print(f"BBR in effect on {sum(e == 'bbr' for e in effective)} of {len(effective)} v1 runs")
    print(f"2. {'HELD' if waits and all(w <= 0.3 for w, _, _ in waits) else 'FAILED'}: the receiver waited at most "
          f"0.3 s (wait, decompressing summed, threads): {waits}")
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp47-cells.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    earlier, _, _ = medians([ROOT / 'results' / 'exp04' / 'exp46', ROOT / 'results' / 'exp04' / 'exp46b'])
    fig, ax = plt.subplots(figsize=(7.5, 4))
    pts46 = [(gbit, earlier[(c, V1Z)] / earlier[(c, V1)]) for gbit, c in ((0.5, '0.5:60:tuned'), (2, '2:60:tuned'))
             if (c, V1Z) in earlier and (c, V1) in earlier]
    pts47 = [(gbit, r['zstd_over_v1']) for gbit, r in ((2, two), (1, one))]
    ax.plot([p[0] for p in pts46], [p[1] for p in pts46], 'o--', color=COLORS['sky'],
            label="EXP-46: compressing on the sender's pool")
    ax.plot(sorted(p[0] for p in pts47), [dict(pts47)[g] for g in sorted(dict(pts47))], 's-', color=COLORS['blue'],
            label="EXP-47: and decompressing on the receiver's pool")
    ax.axhline(1, color=COLORS['black'], lw=0.8)
    ax.annotate('even with v1', (0.45, 1.0), textcoords='offset points', xytext=(0, 3), fontsize=8)
    for (g, r), dy in [*((p, 6) for p in pts46), *((p, -12) for p in pts47)]:
        ax.annotate(f'{r:.2f}', (g, r), textcoords='offset points', xytext=(-22 if g == 2 else 6, dy), fontsize=8)
    ax.set_xscale('log')
    ax.set_xticks([0.5, 1, 2], ['0.5', '1', '2'])
    ax.xaxis.set_minor_formatter(plt.NullFormatter())
    ax.set_xlabel('link rate (Gbit/s), 60 ms round trip')
    ax.set_ylabel('v1 + zstd time ÷ v1 time')
    ax.set_title('Where zstd pays: scattered 1% changes, 2.1 GB (hex filler)', fontsize=10)
    ax.legend(fontsize=8, loc='upper left')
    fig.tight_layout()
    print(plotting.save(fig, 'exp47-zstd-rate'))


if __name__ == '__main__':
    main()
