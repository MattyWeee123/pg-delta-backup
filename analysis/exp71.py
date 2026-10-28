"""EXP-71's analysis: idea 1 built into pgdelta (--diverged-basis), timed against pg_rewind and a re-clone on EXP-67's
old primary after a failover (docs/experiments/EXP-71-idea-1-timed.md).

    .venv/bin/python analysis/exp71.py [--results results/exp04/exp71]

Per lag (hot10, uniform) and arm (rewind, union, full): the median time to a current copy (t1 - t0), from EXP-67's
parsing (exp67.runs), and for the union arm what B's own WAL added (the sender's fast_path.diverged_basis). Scores
1 (deciding: hot10, union at most 0.5 of rewind), 2 (uniform, union at most 0.75 of the re-clone), 3 (uniform, union
at most 0.5 of rewind), 4 (every run correct; every union run took the fast path with B's WAL, its checkpoint before
the fork; both checked copies pass pg_amcheck), 5 (B's blocks within 15% of EXP-69's 20,199 and 20,094) and 6
(controls: rewind within 15% of EXP-67's 36.44 and 459.0 s, the re-clone within 5% of its 90.92 and 91.10 s). Writes
docs/experiments/data/exp71-cells.csv and the figure exp71-union.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

import exp67
import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
ARMS = ('rewind', 'union', 'full')
LAGS = ('hot10', 'uniform')
LABELS = {'rewind': 'pg_rewind, then replay to current', 'union': "pgdelta, idea 1 (B's WAL + S's summaries)",
          'full': 'pg_basebackup re-clone'}
EXP67 = {'hot10': {'rewind': 36.443, 'full': 90.924, 'fast': 90.227},
         'uniform': {'rewind': 458.995, 'full': 91.097, 'fast': 177.851}}
EXP69_B_BLOCKS = {'hot10': 20199, 'uniform': 20094}


def lsn(text):
    return exp67.lsn(text)


def runs(results):
    out = []
    for row in exp67.runs(results):
        folder = Path(results) / f"run-{row['run']:02d}"
        send = folder / 'send.json'
        stats = json.loads(send.read_text()) if send.exists() and send.stat().st_size else {}
        fast = stats.get('fast_path') or {}
        diverged = fast.get('diverged_basis') or {}
        received = ((stats.get('receiver') or {}).get('fast_path') or {}).get('diverged_basis') or {}
        amcheck = folder / 'amcheck-status'
        row.update({'b_blocks': diverged.get('blocks'), 'b_records': diverged.get('records'),
                    'checkpoint': diverged.get('checkpoint'), 'fork': diverged.get('fork'),
                    'b_wal_s': received.get('seconds'), 'guard_s': fast.get('guard_seconds'),
                    'pages_chosen': fast.get('pages_chosen'),
                    'amcheck': int(amcheck.read_text()) if amcheck.exists() else None})
        out.append(row)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp71'))
    a = ap.parse_args(argv)
    rows = runs(a.results)
    for r in rows:
        print(r)
    cells = {}
    for lag in LAGS:
        fork = json.loads((Path(a.results) / f'fork-{lag}.json').read_text())
        info = json.loads((Path(a.results) / f'lag-{lag}.json').read_text())
        cell = {'lag': lag, 'wal_since_fork_mb': round((lsn(info['source_lsn']) - lsn(fork['promoted_lsn'])) / 1e6, 1),
                'check_wal_mb': round((lsn(info['source_lsn_after_check']) - lsn(info['source_lsn'])) / 1e6, 1)}
        for arm in ARMS:
            mine = [r for r in rows if r['lag'] == lag and r['arm'] == arm and r['status'] == 0]
            cell[f'{arm}_s'] = round(statistics.median(r['seconds'] for r in mine), 3) if mine else None
            cell[f'{arm}_runs'] = len(mine)
        rewinds = [r for r in rows if r['lag'] == lag and r['arm'] == 'rewind' and r['rewind_s'] is not None]
        if rewinds:
            cell['pg_rewind_s'] = round(statistics.median(r['rewind_s'] for r in rewinds), 3)
        unions = [r for r in rows if r['lag'] == lag and r['arm'] == 'union']
        for key in ('b_blocks', 'b_records', 'b_wal_s', 'guard_s', 'pages_sent', 'pages_chosen'):
            values = [r[key] for r in unions if r[key] is not None]
            cell[f'union_{key}'] = statistics.median(values) if values else None
        for x, y in (('union', 'rewind'), ('union', 'full'), ('rewind', 'full')):
            if cell[f'{x}_s'] and cell[f'{y}_s']:
                cell[f'{x}_over_{y}'] = round(cell[f'{x}_s'] / cell[f'{y}_s'], 3)
        cells[lag] = cell
        print(cell)
    hot, uni = cells['hot10'], cells['uniform']
    held = lambda v, bar: v is not None and v <= bar  # noqa: E731
    bars = (('hot10', 'union_over_rewind', 0.5), ('uniform', 'union_over_full', 0.75),
            ('uniform', 'union_over_rewind', 0.5))
    near = [(lag, key) for lag, key, bar in bars if cells[lag].get(key) is not None and abs(cells[lag][key] - bar) <= 0.05]
    print(f"1 (deciding). {'HELD' if held(hot.get('union_over_rewind'), 0.5) else 'FAILED'}: hot10, union / rewind "
          f"{hot.get('union_over_rewind')} (bar 0.5)" + (f'; within 0.05 of a bar: {near} (rule 11)' if near else ''))
    print(f"2. {'HELD' if held(uni.get('union_over_full'), 0.75) else 'FAILED'}: uniform, union / re-clone "
          f"{uni.get('union_over_full')} (bar 0.75)")
    print(f"3. {'HELD' if held(uni.get('union_over_rewind'), 0.5) else 'FAILED'}: uniform, union / rewind "
          f"{uni.get('union_over_rewind')} (bar 0.5)")
    unions = [r for r in rows if r['arm'] == 'union']
    checked = [r for r in unions if r['repeat'] == 0]
    four = (bool(rows) and all(r['status'] == 0 and r['match'] for r in rows)
            and all(r['receiver_status'] == 0 and r['fast_used'] is True and r['b_blocks']
                    and lsn(r['checkpoint']) < lsn(r['fork']) for r in unions)
            and len(checked) == len(LAGS) and all(r['amcheck'] == 0 for r in checked))
    print(f"4. {'HELD' if four else 'FAILED'}: statuses {sorted({r['status'] for r in rows})}, all match "
          f"{all(r['match'] for r in rows)}; union runs "
          f"{[(r['lag'], r['fast_used'], r['reasons'], r['checkpoint'], r['fork'], r['amcheck']) for r in unions]}")
    five = {lag: cells[lag]['union_b_blocks'] for lag in LAGS}
    print(f"5. {'HELD' if all(v and abs(v / EXP69_B_BLOCKS[lag] - 1) <= 0.15 for lag, v in five.items()) else 'FAILED'}"
          f": B's blocks {five} against EXP-69's {EXP69_B_BLOCKS} (15%)")
    six = {(lag, arm): (cells[lag][f'{arm}_s'], EXP67[lag][arm]) for lag in LAGS for arm in ('rewind', 'full')}
    ok6 = all(v is not None and abs(v / ref - 1) <= (0.15 if arm == 'rewind' else 0.05)
              for (lag, arm), (v, ref) in six.items())
    print(f"6. {'HELD' if ok6 else 'FAILED'}: {six} (rewind 15%, re-clone 5%)")
    print(f"Reported: against EXP-67's pgdelta (hashing) {EXP67['hot10']['fast']} and {EXP67['uniform']['fast']} s; "
          f"the state check's WAL {hot.get('check_wal_mb')} and {uni.get('check_wal_mb')} MB")
    keys = sorted({k for c in cells.values() for k in c}, key=lambda k: (k != 'lag', k))
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp71-cells.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, restval='')
        w.writeheader()
        w.writerows(cells.values())
    figure(cells)


def figure(cells):
    plotting.style()
    fig, ax = plt.subplots(figsize=(8.2, 4.4))
    colors = {'rewind': COLORS['purple'], 'union': COLORS['blue'], 'full': COLORS['orange']}
    for i, lag in enumerate(LAGS):
        x0 = i * 5
        for j, arm in enumerate(ARMS):
            value = cells[lag].get(f'{arm}_s')
            if value is None:
                continue
            ax.bar(x0 + j, value, 0.8, color=colors[arm], label=LABELS[arm] if i == 0 else None)
            if arm == 'rewind' and cells[lag].get('pg_rewind_s'):
                ax.bar(x0 + j, cells[lag]['pg_rewind_s'], 0.8, color='none', edgecolor=COLORS['black'], hatch='//',
                       lw=0.6, label='of which pg_rewind itself' if i == 0 else None)
            ax.annotate(f'{value:.1f}', (x0 + j, value), textcoords='offset points', xytext=(0, 3), ha='center',
                        fontsize=8.5)
        ax.bar(x0 + 3, EXP67[lag]['fast'], 0.8, color='none', edgecolor=COLORS['grey'], lw=1, linestyle='--',
               label="EXP-67's pgdelta (refused, so hashing)" if i == 0 else None)
        ax.annotate(f"{EXP67[lag]['fast']:.1f}", (x0 + 3, EXP67[lag]['fast']), textcoords='offset points',
                    xytext=(0, 3), ha='center', fontsize=8.5, color=COLORS['grey'])
    ax.set_xticks([1.5, 6.5], [f"hot rows updated 10x\n({cells['hot10']['wal_since_fork_mb']:,.0f} MB of WAL since the fork)",
                               f"1% of rows, scattered\n({cells['uniform']['wal_since_fork_mb']:,.0f} MB of WAL since the fork)"])
    ax.set_ylabel('seconds to a current copy (median)')
    ax.legend(fontsize=8, loc='upper left')
    ax.set_title('EXP-71: an 18 GB old primary after a failover, with idea 1 in pgdelta (8 GB VMs)', fontsize=10)
    print(plotting.save(fig, 'exp71-union'))


if __name__ == '__main__':
    main()
