"""EXP-42's analysis: is v1 safe on a busy AlloyDB Omni? (docs/experiments/EXP-42-omni-live.md)

    .venv/bin/python analysis/exp42.py [--steps results/omni/exp42/steps.jsonl] [--figdir DIR]

Scores the registered predictions from experiments/omni/omni_live.sh's steps: the _mintxid inventory (recorded), the
mechanism (the workload changes _mintxid files and Omni's pg_waldump shows that fork's block references with
full-page images), the counted trials (exit 0, B started, pgbench's invariant, bt_index_check, verify_heapam before
and after VACUUM (FREEZE)) and the controls (caught when any check failed or B didn't start). Writes
docs/experiments/data/exp42-trials.csv and docs/figures/exp42-omni-live.svg.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]


def checks_pass(t):
    s = t.get('sums') or {}
    sums_ok = bool(s) and s.get('accounts') == s.get('tellers') == s.get('branches') == s.get('history')
    return (t.get('started') is True and sums_ok and t.get('index_check_rc') == 0 and t.get('heap_corruptions') == 0
            and t.get('freeze_rc') == 0 and t.get('heap_corruptions_after_freeze') == 0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--steps', default=str(ROOT / 'results' / 'omni' / 'exp42' / 'steps.jsonl'))
    ap.add_argument('--figdir')
    a = ap.parse_args(argv)
    if a.figdir:
        plotting.FIGURES = Path(a.figdir)
    plotting.style()
    rows = [json.loads(line) for line in open(a.steps) if line.strip()]
    inv = next(r for r in rows if r['step'] == 'mintxid_inventory')
    wal = next(r for r in rows if r['step'] == 'mintxid_wal')
    trials = [r for r in rows if r['step'] == 'trial']
    print(f"1. recorded: {inv['files']} _mintxid files, {inv['bytes']} bytes, by directory {inv['by_directory']}")
    for rel, info in inv['pgbench'].items():
        print(f"     {rel}: has _mintxid {info['has_mintxid']} ({info['mintxid_bytes']} bytes)")
    forks = {k for k in wal['block_refs_by_fork'] if 'mintxid' in k.lower()}
    refs = sum(wal['block_refs_by_fork'][k] for k in forks)
    fpis = sum(wal['full_page_images_by_fork'].get(k, 0) for k in forks)
    held2 = wal['mintxid_files_changed'] > 0 and refs > 0 and fpis > 0
    print(f"2. {'HELD' if held2 else 'FAILED'}: {wal['mintxid_files_changed']} of {wal['mintxid_files_after']} _mintxid files "
          f"changed; WAL block references by fork {wal['block_refs_by_fork']}, full-page images "
          f"{wal['full_page_images_by_fork']} ({wal['wal_records']} records)")
    counted = [t for t in trials if not t['control']]
    controls = [t for t in trials if t['control']]
    correct = [t for t in counted if t['exit'] == 0 and checks_pass(t)]
    caught = [t for t in controls if not checks_pass(t)]
    print(f"3. {'HELD' if len(correct) == len(counted) == 20 else 'FAILED'}: {len(correct)} of {len(counted)} counted trials correct"
          + ('' if len(correct) == len(counted) else f"; wrong: {[t['trial'] for t in counted if t not in correct]}"))
    print(f"4. {'HELD' if len(caught) >= 3 else 'FAILED'}: {len(caught)} of {len(controls)} controls caught")
    out = ROOT / 'docs' / 'experiments' / 'data' / 'exp42-trials.csv'
    with open(out, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['trial', 'control', 'exit', 'seconds', 'started', 'sums_equal', 'index_check_rc', 'heap_corruptions',
                    'heap_corruptions_after_freeze', 'checks_pass'])
        for t in trials:
            s = t.get('sums') or {}
            w.writerow([t['trial'], t['control'], t['exit'], t['seconds'], t['started'],
                        bool(s) and s.get('accounts') == s.get('tellers') == s.get('branches') == s.get('history'),
                        t.get('index_check_rc'), t.get('heap_corruptions'), t.get('heap_corruptions_after_freeze'),
                        checks_pass(t)])
    print(out)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4), gridspec_kw={'width_ratios': [1, 1.6]})
    names = sorted(wal['block_refs_by_fork'], key=lambda k: -wal['block_refs_by_fork'][k])
    xs = range(len(names))
    ax1.bar([x - 0.2 for x in xs], [wal['block_refs_by_fork'][k] for k in names], 0.4, color=COLORS['blue'],
            label='block references')
    ax1.bar([x + 0.2 for x in xs], [wal['full_page_images_by_fork'].get(k, 0) for k in names], 0.4,
            color=COLORS['orange'], label='full-page images')
    ax1.set_yscale('log')
    ax1.set_xticks(list(xs), names)
    ax1.set_title("Omni's WAL over 10 s of pgbench, by relation fork", fontsize=10)
    ax1.legend(fontsize=8)
    for t in trials:
        ok = checks_pass(t) and (t['control'] or t['exit'] == 0)
        color = (COLORS['green'] if ok else COLORS['red']) if not t['control'] else (
            COLORS['grey'] if checks_pass(t) else COLORS['black'])
        ax2.scatter(t['trial'], t['seconds'], color=color, marker='x' if t['control'] else 'o', zorder=3)
    for color, marker, label in ((COLORS['green'], 'o', 'counted: correct'), (COLORS['red'], 'o', 'counted: wrong'),
                                 (COLORS['black'], 'x', 'control: caught'), (COLORS['grey'], 'x', 'control: missed')):
        ax2.scatter([], [], color=color, marker=marker, label=label)
    ax2.set_xlabel('trial')
    ax2.set_ylabel('refresh time (s)')
    ax2.set_ylim(bottom=0)
    ax2.set_title('Live refreshes of a busy Omni 17.10, each B started and checked', fontsize=10)
    ax2.legend(fontsize=8, loc='lower right')
    fig.tight_layout()
    print(plotting.save(fig, 'exp42-omni-live'))


if __name__ == '__main__':
    main()
