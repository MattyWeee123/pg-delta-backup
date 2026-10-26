"""EXP-54's analysis: do sub-page deltas still pay on top of zstd? (docs/experiments/EXP-54-subpage-on-zstd.md)

    .venv/bin/python analysis/exp54.py [--results results/bytes/exp54/results.json]

Scores, counting B's block digests (dest -> source) in both sub-page schemes:
1 (deciding) subpage_zstd + digests <= 0.5 x positional_zstd;
2 positional / (subpage + digests) within 15% of EXP-37's 5.0x;
3 positional / positional_zstd within 10% of EXP-37's 2.83x.
Projects each scheme's time on the link alone at 0.5, 1 and 2 Gbit/s (bytes x 8 / rate: a projection, not a
measurement). Writes docs/experiments/data/exp54-bytes.csv and the figure exp54-subpage-on-zstd.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMES = ('positional', 'positional_zstd', 'subpage', 'subpage_zstd')
LABELS = {'positional': 'pages (v1)', 'positional_zstd': 'pages + zstd (today)', 'subpage': '512-byte blocks',
          'subpage_zstd': '512-byte blocks + zstd'}
EXP37 = {'subpage': 5.0, 'zstd': 2.83}
RATES = (0.5, 1.0, 2.0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'bytes' / 'exp54' / 'results.json'))
    a = ap.parse_args(argv)
    r = json.loads(Path(a.results).read_text())
    digests = r['digest_bytes']
    total = {s: r['schemes'][s]['bytes'] + (digests if s.startswith('subpage') else 0) for s in SCHEMES}
    print(f"{r['changed_pages']} changed pages in {r['runs']} runs; B has {r['pages_b_has']}; digests {digests / 1e6:.1f} MB")
    for s in SCHEMES:
        extra = {k: v for k, v in r['schemes'][s].items() if k != 'bytes'}
        print(f"  {s}: {total[s] / 1e6:.1f} MB ({r['changed_pages'] * 8192 / total[s]:.2f}x fewer than pages) {extra}")
    ratio = total['subpage_zstd'] / total['positional_zstd']
    print(f"1 (deciding). {'HELD' if ratio <= 0.5 else 'FAILED'}: sub-page + zstd / zstd alone = {ratio:.3f}")
    sub = total['positional'] / total['subpage']
    print(f"2. {'HELD' if abs(sub / EXP37['subpage'] - 1) <= 0.15 else 'FAILED'}: sub-page alone {sub:.2f}x (EXP-37: 5.0)")
    z = total['positional'] / total['positional_zstd']
    print(f"3. {'HELD' if abs(z / EXP37['zstd'] - 1) <= 0.10 else 'FAILED'}: zstd alone {z:.2f}x (EXP-37: 2.83)")
    rows = []
    for s in SCHEMES:
        row = {'scheme': s, 'source_to_dest_bytes': r['schemes'][s]['bytes'],
               'dest_to_source_bytes': digests if s.startswith('subpage') else 0, 'total_bytes': total[s],
               'fewer_than_pages': round(total['positional'] / total[s], 3)}
        row.update({f'link_s_at_{g:g}g': round(total[s] * 8 / (g * 1e9), 3) for g in RATES})
        rows.append(row)
        print('  ', row)
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp54-bytes.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    figure(rows, digests)


def figure(rows, digests):
    """Left: megabytes per scheme, split by direction. Right: the projected time on the link alone."""
    import plotting
    from plotting import COLORS, plt
    plotting.style()
    fig, (left, right) = plt.subplots(1, 2, figsize=(10, 4.2))
    names = [LABELS[r['scheme']] for r in rows]
    s2d = [r['source_to_dest_bytes'] / 1e6 for r in rows]
    d2s = [r['dest_to_source_bytes'] / 1e6 for r in rows]
    x = range(len(rows))
    left.bar(x, s2d, 0.6, color=COLORS['blue'], label='source → dest (pages or blocks)')
    left.bar(x, d2s, 0.6, bottom=s2d, color=COLORS['orange'], label="dest → source (B's block digests)")
    for i, r in enumerate(rows):
        left.annotate(f"{r['total_bytes'] / 1e6:.0f} MB\n{r['fewer_than_pages']:.1f}×", (i, r['total_bytes'] / 1e6),
                      textcoords='offset points', xytext=(0, 3), ha='center', fontsize=8)
    left.set_ylim(0, max(r['total_bytes'] for r in rows) / 1e6 * 1.18)
    left.set_xticks(list(x), names, fontsize=8, rotation=12)
    left.set_ylabel('MB per refresh (uniform 1%, scale 70)')
    left.set_title('Bytes on the wire', fontsize=10)
    left.legend(fontsize=8)
    colors = [COLORS['grey'], COLORS['red'], COLORS['sky'], COLORS['green']]
    for r, color in zip(rows, colors):
        right.plot(RATES, [r[f'link_s_at_{g:g}g'] for g in RATES], marker='o', color=color, label=LABELS[r['scheme']])
    right.set_xscale('log', base=2)
    right.set_xticks(list(RATES), [f'{g:g}' for g in RATES])
    right.set_xlabel('link rate (Gbit/s)')
    right.set_ylabel('seconds on the link alone (projected)')
    right.set_title('What the bytes cost on a DR link', fontsize=10)
    right.legend(fontsize=8)
    fig.tight_layout()
    print(plotting.save(fig, 'exp54-subpage-on-zstd'))


if __name__ == '__main__':
    main()
