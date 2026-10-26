"""EXP-56's analysis, stage 1 (local): sub-page deltas as built (docs/experiments/EXP-56-subpage-build.md).

    .venv/bin/python analysis/exp56.py [--runs results/bytes/exp56/runs.jsonl]

Scores stage 1:
1 (gates everything) every run complete and every copy byte-identical to S, the control differing;
2 zstd_subpage's bytes within 10% of EXP-54/55: source -> dest page data against 34.6 MB plus the frame headers,
  dest -> source block digests against 17.2 MB.
Reports each arm's medians (pgdelta's total, bytes each way) and writes docs/experiments/data/exp56-local.csv and the
figure exp56-local.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARMS = ('plain', 'zstd', 'subpage', 'zstd_subpage')
LABELS = {'plain': 'v1', 'zstd': 'v1 + zstd', 'subpage': 'v1 + sub-page', 'zstd_subpage': 'v1 + zstd + sub-page'}
EXP54_BLOCKS_ZSTD = 34_604_545  # EXP-54: blocks + zstd, source -> dest, without frame headers
EXP55_DIGESTS = 17_161_216  # EXP-55: up-front block digests, dest -> source
FRAME_HEADER = 5 + 12  # protocol HEADER (4 + 1) and the page head (index, first page)


def load(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--runs', default=str(ROOT / 'results' / 'bytes' / 'exp56' / 'runs.jsonl'))
    a = ap.parse_args(argv)
    rows = load(a.runs)
    control = next(r for r in rows if r['label'] == 'control')
    runs = [r for r in rows if r['label'] != 'control']
    counted = [r for r in runs if r['repeat'] is not None]
    exact = all(r['status'] == 'complete' and r['bytes']['files_differing'] == 0 for r in runs)
    print(f"1 (gate). {'HELD' if exact and control['bytes']['files_differing'] > 0 else 'FAILED'}: "
          f"{sum(r['bytes']['files_differing'] == 0 for r in runs)} of {len(runs)} copies identical to S in all "
          f"{runs[0]['bytes']['files_checked']} files; statuses {sorted({str(r['status']) for r in runs})}; control "
          f"{control['bytes']['files_differing']} files differ")
    table = []
    for arm in ARMS:
        mine = [r for r in counted if r['arm'] == arm]
        med = lambda f: statistics.median(f(r) for r in mine)
        row = {'arm': arm, 'runs': len(mine), 'total_s': round(med(lambda r: r['timings']['total']), 3),
               'page_data_mb': round(med(lambda r: r['bytes_source_to_dest']['page_data']) / 1e6, 2),
               'source_to_dest_mb': round(med(lambda r: r['bytes_source_to_dest']['total']) / 1e6, 2),
               'dest_to_source_mb': round(med(lambda r: r['bytes_dest_to_source']['total']) / 1e6, 2)}
        if 'subpage' in arm:
            row['block_digest_mb'] = round(med(lambda r: r['subpage']['block_digest_bytes']) / 1e6, 2)
            row['sender_hash_s'] = round(med(lambda r: r['subpage']['hash_seconds']), 3)
            row['receiver_hash_s'] = round(med(lambda r: r['receiver_subpage']['hash_seconds']), 3)
            row['pages_partial'] = int(med(lambda r: r['subpage']['pages_partial']))
        table.append(row)
        print('  ', row)
    zs = next(r for r in counted if r['arm'] == 'zstd_subpage')
    frames = zs['bytes_source_to_dest']['by_message_type']
    delta_frames = sum(v['frames'] for k, v in frames.items() if k.startswith('PAGE_D'))
    expected_s2d = EXP54_BLOCKS_ZSTD + delta_frames * FRAME_HEADER
    s2d = statistics.median(r['bytes_source_to_dest']['page_data'] for r in counted if r['arm'] == 'zstd_subpage')
    digests = statistics.median(r['subpage']['block_digest_bytes'] for r in counted if r['arm'] == 'zstd_subpage')
    ok = abs(s2d / expected_s2d - 1) <= 0.10 and abs(digests / EXP55_DIGESTS - 1) <= 0.10
    print(f"2. {'HELD' if ok else 'FAILED'}: page data {s2d / 1e6:.2f} MB against {expected_s2d / 1e6:.2f} "
          f"({s2d / expected_s2d:.3f}); block digests {digests / 1e6:.2f} MB against {EXP55_DIGESTS / 1e6:.2f} "
          f"({digests / EXP55_DIGESTS:.3f})")
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp56-local.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(table[-1]), restval='')  # the sub-page arm has every column
        w.writeheader()
        w.writerows(table)
    figure(table)


def figure(table):
    """Left: bytes each way per arm. Right: pgdelta's total per arm (local, so the link costs almost nothing)."""
    import plotting
    from plotting import COLORS, plt
    plotting.style()
    fig, (left, right) = plt.subplots(1, 2, figsize=(10, 4))
    x = range(len(table))
    s2d = [r['source_to_dest_mb'] for r in table]
    d2s = [r['dest_to_source_mb'] for r in table]
    left.bar(x, s2d, 0.6, color=COLORS['blue'], label='source → dest')
    left.bar(x, d2s, 0.6, bottom=s2d, color=COLORS['orange'], label='dest → source')
    for i, r in enumerate(table):
        left.annotate(f"{r['source_to_dest_mb'] + r['dest_to_source_mb']:.0f} MB", (i, s2d[i] + d2s[i]),
                      textcoords='offset points', xytext=(0, 3), ha='center', fontsize=8)
    left.set_xticks(list(x), [LABELS[r['arm']] for r in table], fontsize=8, rotation=10)
    left.set_ylabel('MB per refresh (median)')
    left.set_title('Bytes on the wire, as built', fontsize=10)
    left.legend(fontsize=8)
    right.bar(x, [r['total_s'] for r in table], 0.6, color=[COLORS['grey'], COLORS['red'], COLORS['sky'], COLORS['green']])
    for i, r in enumerate(table):
        right.annotate(f"{r['total_s']:.2f} s", (i, r['total_s']), textcoords='offset points', xytext=(0, 3),
                       ha='center', fontsize=8)
    right.set_xticks(list(x), [LABELS[r['arm']] for r in table], fontsize=8, rotation=10)
    right.set_ylabel("pgdelta's total, seconds (median)")
    right.set_title('Time over loopback (no link to save)', fontsize=10)
    fig.suptitle('pgdelta local, in place, scale 70, uniform 1% (the Mac)', fontsize=10)
    fig.tight_layout()
    print(plotting.save(fig, 'exp56-local'))


if __name__ == '__main__':
    main()
