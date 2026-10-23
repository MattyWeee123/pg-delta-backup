"""EXP-03 figures and curated data from results/exp03 (see docs/experiments/EXP-03).

    .venv/bin/python analysis/exp03.py [RESULTS_DIR]
Writes docs/experiments/data/exp03-summary.csv and docs/figures/exp03-*.svg/.png.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'docs' / 'experiments' / 'data'
CAUSES = [  # (category keys, label, colour), most serious first
    (('content',), 'Stale data (real content)', COLORS['red']),
    (('all_visible',), 'All-visible flag', COLORS['orange']),
    (('hint_bits_only', 'hint_bits_and_lsn'), 'Hint bits only', COLORS['yellow']),
    (('fsm', 'vm', 'init'), 'Free space / visibility map', COLORS['sky']),
    (('checksum_only',), 'Checksum field only', COLORS['purple']),
    (('lsn_only',), 'Page LSN only', COLORS['grey']),
    (('unclassified',), 'Unclassified', COLORS['black']),
]


def load(results):
    rows = []
    def order(path):
        head = path.name.split('-')[0]
        digits = ''.join(ch for ch in head if ch.isdigit())
        return (int(digits) if digits else 99, head)

    for scenario_dir in sorted(Path(results).iterdir(), key=order):
        meta_path = scenario_dir / 'scenario.json'
        if not meta_path.is_file():
            continue
        meta = json.loads(meta_path.read_text())
        scan = json.loads((scenario_dir / 'lsnscan.json').read_text())
        evaluation_path = scenario_dir / 'evaluation.json'
        evaluation = json.loads(evaluation_path.read_text()) if evaluation_path.is_file() else {}
        stats = (evaluation.get('tool') or {}).get('stats') or {}
        categories = (meta.get('observations') or {}).get('wrong_skip_categories') or {}
        totals = scan['totals']
        pages = stats.get('pages') or {}
        rows.append({
            'scenario': meta['id'], 'title': meta.get('title') or meta.get('name'),
            'lsn_prediction': (meta.get('predictions') or {}).get('lsn_skip'),
            'pages_compared': totals['pages_compared'],
            'pages_without_counterpart': totals.get('pages_without_counterpart', 0),
            'pages_changed': totals['pages_changed'],
            'pages_skipped': totals['pages_skipped'],
            'pages_wrongly_skipped': totals['pages_wrongly_skipped'],
            **{f'wrong_{k}': v for k, v in categories.items()},
            'tool_mode': stats.get('mode'), 'tool_policy': stats.get('identity_policy') or stats.get('gate_reason'),
            'tool_pages_total': pages.get('total'), 'tool_pages_sent': pages.get('sent'),
            'tool_bytes_source_to_dest': (stats.get('bytes_source_to_dest') or {}).get('total'),
            'tool_bytes_dest_to_source': (stats.get('bytes_dest_to_source') or {}).get('total'),
            'byte_identical': evaluation.get('byte_identical'),
            'verifybackup': evaluation.get('verifybackup'),
            'restore_ok': evaluation.get('restore_ok'),
            'logical_match': evaluation.get('logical_match'),
            'tool_correct': evaluation.get('tool_correct'),
            'categories': categories,
        })
    return rows


def write_csv(rows, path):
    keys = [k for k in rows[0] if k != 'categories']
    extra = sorted({k for row in rows for k in row if k.startswith('wrong_') and k not in keys})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=keys + extra, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def figure_wrong_skips(rows):
    fig, (left_ax, right_ax) = plt.subplots(1, 2, figsize=(11, 5.2), sharey=True,
                                            gridspec_kw={'width_ratios': [1.1, 1]})
    labels = [f"{r['scenario']}  {r['title']}" for r in rows]
    y = list(range(len(rows)))
    totals = [r['pages_wrongly_skipped'] for r in rows]
    stale = [r['categories'].get('content', 0) for r in rows]
    colors = [COLORS['red'] if s else (COLORS['orange'] if t else COLORS['grey'])
              for s, t in zip(stale, totals)]
    left_ax.barh(y, [max(t, 0.5) if t else 0 for t in totals], color=colors)
    for i, (t, r) in enumerate(zip(totals, rows)):
        note = 'tool OK' if r['tool_correct'] else ('tool WRONG' if r['tool_correct'] is False else '')
        left_ax.text(max(t, 1) * 1.3, i, f'{t:,}  ({note})', va='center', fontsize=8.5)
    left_ax.set_xscale('symlog', linthresh=1)
    left_ax.set_xlim(0, 5e6)
    left_ax.set_yticks(y, labels)
    left_ax.invert_yaxis()
    left_ax.set_xlabel('Pages wrongly skipped by LSN (log scale)')
    left_ax.set_title('Pages LSN skipping gets wrong\n(red: includes stale data; orange: metadata or hint bits only)', fontsize=10)
    for i, r in enumerate(rows):
        total = r['pages_wrongly_skipped']
        start = 0
        for keys, label, color in CAUSES:
            value = sum(r['categories'].get(k, 0) for k in keys)
            if total and value:
                right_ax.barh(i, 100 * value / total, left=start, color=color,
                              label=label if label not in right_ax.get_legend_handles_labels()[1] else None)
                start += 100 * value / total
    right_ax.set_xlim(0, 100)
    right_ax.set_xlabel('Share of the wrongly skipped pages (%)')
    right_ax.set_title('Why those pages differ', fontsize=11)
    handles, names = right_ax.get_legend_handles_labels()
    fig.legend(handles, names, loc='lower center', ncol=6, fontsize=8.5, bbox_to_anchor=(0.5, 0.0))
    fig.suptitle('EXP-03: LSN skipping vs. the hash-based tool on ten tricky old copies', fontsize=12.5)
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    return plotting.save(fig, 'exp03-wrong-skips')


def figure_reuse(rows):
    fig, ax = plt.subplots(figsize=(8.2, 4.2))
    names = [r['scenario'] for r in rows]
    total = [r['tool_pages_total'] or 0 for r in rows]
    sent = [r['tool_pages_sent'] or 0 for r in rows]
    reused = [t - s for t, s in zip(total, sent)]
    ax.bar(names, reused, color=COLORS['blue'], label='Reused from the old copy')
    ax.bar(names, sent, bottom=reused, color=COLORS['orange'], label='Sent over the link')
    for i, r in enumerate(rows):
        if r['tool_mode'] == 'full':
            ax.text(i, total[i] * 1.01, 'full\nmode', ha='center', va='bottom', fontsize=8)
    ax.set_ylabel('8 KiB pages in the fresh backup')
    ax.set_title('EXP-03: pages the tool reused vs. sent, per scenario')
    ax.legend(fontsize=9)
    return plotting.save(fig, 'exp03-reuse')


def figure_policy(default_rows, reuse_rows):
    """Default identity policy vs --reuse-any-basis for the scenarios that differ."""
    by_id = {r['scenario']: r for r in default_rows}
    rows = [(r, by_id[r['scenario']]) for r in reuse_rows if r['scenario'] in by_id]
    fig, ax = plt.subplots(figsize=(7.5, 3.6))
    names = [r['scenario'] + '  ' + r['title'] for r, _ in rows]
    y = range(len(rows))
    default_sent = [100 * d['tool_pages_sent'] / d['tool_pages_total'] for _, d in rows]
    reuse_sent = [100 * r['tool_pages_sent'] / r['tool_pages_total'] for r, _ in rows]
    ax.barh([i + 0.2 for i in y], default_sent, height=0.38, color=COLORS['grey'],
            label='Default policy (different cluster or checksum state: full copy)')
    ax.barh([i - 0.2 for i in y], reuse_sent, height=0.38, color=COLORS['blue'],
            label='--reuse-any-basis (every reused page still verified)')
    for i, value in enumerate(reuse_sent):
        ax.text(value + 1, i - 0.2, f'{value:.1f}%', va='center', fontsize=8.5)
    ax.set_yticks(list(y), names)
    ax.invert_yaxis()
    ax.set_xlim(0, 115)
    ax.set_xlabel('Pages sent, % of the fresh backup')
    ax.set_title('EXP-03: reuse given up by the identity policy\n(all outputs verified correct either way)', fontsize=11)
    ax.legend(loc='upper center', bbox_to_anchor=(0.4, -0.25), fontsize=8.5)
    return plotting.save(fig, 'exp03-policy')


def main(argv):
    results = Path(argv[1]) if len(argv) > 1 else ROOT / 'results' / 'exp03'
    reuse_results = ROOT / 'results' / 'exp03-reuse-any'
    plotting.style()
    rows = load(results)
    write_csv(rows, DATA / 'exp03-summary.csv')
    print(figure_wrong_skips(rows))
    print(figure_reuse(rows))
    if reuse_results.is_dir():
        reuse_rows = load(reuse_results)
        write_csv(reuse_rows, DATA / 'exp03-reuse-any-basis.csv')
        print(figure_policy(rows, reuse_rows))
    for r in rows:
        print(r['scenario'], r['pages_wrongly_skipped'], r['tool_mode'], r['tool_pages_sent'], '/',
              r['tool_pages_total'], 'correct' if r['tool_correct'] else r['tool_correct'])


if __name__ == '__main__':
    main(sys.argv)
