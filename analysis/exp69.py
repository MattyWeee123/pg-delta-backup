"""EXP-69's analysis: idea 1 (both sides' block sets) for an old primary after a failover, counted
(docs/experiments/EXP-69-both-sides-block-sets.md).

    .venv/bin/python analysis/exp69.py [--local results/exp69] [--vm results/exp04/exp69]

Part A (local): each scenario's unioncheck.json (experiments/tricky_copies/union_check.py). Scores 1 (deciding: idea 1
leaves no real wrong page wherever its guard passes, and it passes kept S2 and S2c), 2 (its guard refuses kept S2b
and S2d, default S2, and kept S2 with its WAL cut, each for the registered reason; default S2c is reported), 3 (no
page left unsent where B carries a hint S lacks), 4 (pg_rewind takes exactly the main-fork blocks pg_waldump names
from the common checkpoint's record on) and 5 (idea 1 sends at most 1.5 times the pages that differ); 3 to 5 wherever
the guard passes.

Part B (VMs, EXP-67's cells with DIAG=1): diag-LAG/ and the lag's fast run. Scores 6 (the control of 4 at scale),
7 (idea 1's blocks between 0.9 and 1.5 of the pages pgdelta's hashing sent, and S's summaries covering the range)
and 8 (B's blocks under a quarter of what pg_rewind copied beyond S's WAL since the fork, in uniform), and reports
pg_rewind's copy by kind, its WAL segments split by where they lie.

Writes docs/experiments/data/exp69-scenarios.csv, exp69-rewind-copy.csv and the figure exp69-union.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import plotting  # noqa: E402
from plotting import COLORS, plt  # noqa: E402

from experiments.tricky_copies import summaryscan, unionscan  # noqa: E402
from experiments.tricky_copies.lsnscan import parse_lsn  # noqa: E402

PASS = {('kept', 'S2'), ('kept', 'S2c')}
REFUSE = {('kept', 'S2b'): 'not stopped cleanly', ('kept', 'S2d'): 'not stopped cleanly',
          ('default', 'S2'): 'no checkpoint before the fork'}  # default S2c is reported, not predicted
CUT_REASON = 'before B\'s end'
LAGS = ('hot10', 'uniform')
SEGMENT = unionscan.WAL_SEGMENT_BYTES
KINDS = ('wal_since_fork', 'wal_before_fork', 'wal_ahead', 'relation_blocks', 'fsm_vm_init', 'wal_summaries',
         'other')


def part_a(local):
    rows = []
    for path in sorted(Path(local).glob('*/*/unioncheck.json')):
        r = json.loads(path.read_text())
        naive = json.loads((path.parent / 'summaryscan.json').read_text())['naive']
        s, rw = r.get('scored') or {}, r.get('pg_rewind') or {}
        rows.append({'variant': r['variant'], 'scenario': r['scenario'], 'guard_ok': r['guard']['ok'],
                     'reasons': '; '.join(r['guard']['reasons']), 'fork': r.get('fork'),
                     'common_checkpoint': (r.get('common_checkpoint') or {}).get('record'),
                     'pages_changed': s.get('pages_changed'), 'union_sent': s.get('pages_sent'),
                     'real_wrong': s.get('real_wrong'), 'wrong': s.get('wrong'),
                     'wrong_by_category': json.dumps(s.get('by_category') or {}),
                     'b_hints_left': r.get('left_wrong_with_b_hints'), 'b_blocks': r.get('b_blocks'),
                     's_blocks': r.get('s_blocks'), 'union_blocks': r.get('union_blocks'),
                     'naive_sent': naive.get('pages_sent'), 'naive_real_wrong': naive.get('real_wrong'),
                     'rewind_exit': rw.get('exit_code'), 'rewind_need_mb': rw.get('need_mb'),
                     'rewind_blocks': rw.get('blocks'), 'only_pg_rewind': rw.get('only_pg_rewind'),
                     'only_waldump': rw.get('only_waldump'),
                     'rewind_bytes_by_kind': json.dumps(rw.get('bytes_by_kind') or {}),
                     'cut_guard_ok': (r.get('wal_cut') or {}).get('guard', {}).get('ok'),
                     'cut_reasons': '; '.join((r.get('wal_cut') or {}).get('guard', {}).get('reasons', []))})
    return rows


def score_a(rows):
    by = {(r['variant'], r['scenario']): r for r in rows}
    if not rows or any(k not in by for k in PASS):
        print('Part A: not run')
        return
    passing = [r for r in rows if r['guard_ok']]
    one = all(r['real_wrong'] == 0 for r in passing) and all(by[k]['guard_ok'] for k in PASS)
    print(f"1 (deciding). {'HELD' if one else 'FAILED'}: real wrong pages "
          f"{[(r['scenario'], r['real_wrong']) for r in passing]} (EXP-61's summaries alone: "
          f"{[(r['scenario'], r['naive_real_wrong']) for r in passing]})")
    verdicts = {k: (r['guard_ok'], r['reasons']) for k, r in by.items()}
    two = (all(k in by and not by[k]['guard_ok'] and why in by[k]['reasons'] for k, why in REFUSE.items())
           and by[('kept', 'S2')]['cut_guard_ok'] is False and CUT_REASON in by[('kept', 'S2')]['cut_reasons'])
    print(f"2. {'HELD' if two else 'FAILED'}: guard {verdicts}; WAL cut: {by[('kept', 'S2')]['cut_reasons']}")
    three = all(r['b_hints_left'] == 0 for r in passing)
    print(f"3. {'HELD' if three else 'FAILED'}: pages left unsent with a hint only B carries "
          f"{[(r['scenario'], r['b_hints_left']) for r in passing]}")
    four = all(r['rewind_exit'] == 0 and r['only_pg_rewind'] == 0 and r['only_waldump'] == 0 for r in passing)
    print(f"4. {'HELD' if four else 'FAILED'}: pg_rewind's blocks against pg_waldump's "
          f"{[(r['scenario'], r['rewind_exit'], r['rewind_blocks'], r['only_pg_rewind'], r['only_waldump']) for r in passing]}")
    ratios = [(r['scenario'], round(r['union_sent'] / r['pages_changed'], 3)) for r in passing]
    five = all(x <= 1.5 for _, x in ratios)
    print(f"5. {'HELD' if five else 'FAILED'}: union sent / pages that differ {ratios} (bar 1.5)")


def read_status(folder):
    out = {}
    for line in (folder / 'status.txt').read_text().splitlines():
        parts = line.split()
        if parts[0] == 'checkpoint':
            out.update(checkpoint=parts[1], redo=parts[3], b_timeline=int(parts[5]))
        else:
            out[parts[0]] = parts[1]
    return out


def wal_kind(name, s_tli, s_lsn):
    found = unionscan.SEGMENT_NAME.match(name)
    if not found:
        return 'other'
    tli, start = int(found.group(1), 16), int(found.group(2), 16) << 32 | int(found.group(3), 16) << 24
    if tli < s_tli:
        return 'wal_before_fork'
    return 'wal_ahead' if start > s_lsn else 'wal_since_fork'


def summary_blocks(csv_gz):
    blocks, limits, ranges = {}, {}, set()
    with gzip.open(csv_gz, 'rt') as f:
        for tli, start, end, ts, db, rel, fork, block, is_limit in csv.reader(f):
            ranges.add((int(tli), start, end))
            key = f'{ts}/{db}/{rel}/{fork}'
            if is_limit == 't':
                limits[key] = min(int(block), limits.get(key, int(block)))
            else:
                blocks.setdefault(key, set()).add(int(block))
    return blocks, limits


def part_b(vm):
    cells = []
    for lag in LAGS:
        folder = Path(vm) / f'diag-{lag}'
        if not (folder / 'status.txt').exists():
            continue
        status = read_status(folder)
        fork = json.loads((Path(vm) / f'fork-{lag}.json').read_text())
        s_tli, s_lsn = fork['timeline'], parse_lsn(status['s_lsn'])
        parsed = unionscan.parse_rewind_debug(gzip.open(folder / 'rewind-debug.log.gz', 'rt').read())
        diverged = re.search(r'servers diverged at WAL location ([0-9A-F/]+)',
                             gzip.open(folder / 'rewind-debug.log.gz', 'rt').read())
        fork_lsn = parse_lsn(diverged.group(1)) if diverged else parse_lsn(fork['promoted_lsn'])
        by_kind = dict.fromkeys(KINDS, 0)
        for path, size in parsed['bytes'].items():
            kind = unionscan.copy_kind(path)
            if kind == 'wal_segments':
                kind = wal_kind(path.split('/')[-1], s_tli, s_lsn)
            by_kind[kind] += size
        text = gzip.open(folder / 'b0-waldump.txt.gz', 'rt').read()
        b_blocks = unionscan.block_refs(text, start=parse_lsn(status['redo']))[0]
        from_record = unionscan.block_refs(text, start=parse_lsn(status['checkpoint']))[0]
        ours = {summaryscan.relpath(k): v for k, v in from_record.items() if k.endswith('/0')}
        theirs = unionscan.rewind_blocks_by_relation(parsed)
        keys = set(ours) | set(theirs)
        s_blocks, limits = summary_blocks(folder / 's-summaries.csv.gz')
        ranges = [{'tli': int(t), 'start': a, 'end': b} for t, a, b in
                  csv.reader((folder / 's-summary-ranges.csv').read_text().splitlines())]
        path = [(status['b_timeline'], 0, fork_lsn), (s_tli, fork_lsn, summaryscan.END)]
        covered = all(summaryscan.covered(ranges, tli, lo, hi)
                      for tli, lo, hi in summaryscan.ranges(path, parse_lsn(status['redo']), s_lsn))
        union = {}
        for source in (b_blocks, s_blocks):
            for key, blocks in source.items():
                union.setdefault(key, set()).update(blocks)
        sent = [json.loads((d / 'send.json').read_text())['pages']['sent']
                for d in sorted(Path(vm).glob('run-*'))
                if json.loads((d / 'meta.json').read_text())['lag'] == lag and (d / 'send.json').exists()]
        wal_since_fork = s_lsn - fork_lsn
        total = sum(by_kind.values())
        cells.append({'lag': lag, 'rewind_exit': status.get('rewind_exit'), 'need_mb': parsed['need_mb'],
                      'fetched_mb': round(total / 1e6, 1), 'lsn_since_fork_mb': round(wal_since_fork / 1e6, 1),
                      'beyond_wal_since_fork_mb': round((total - wal_since_fork) / 1e6, 1),
                      **{f'{k}_mb': round(v / 1e6, 1) for k, v in by_kind.items()},
                      'rewind_blocks': sum(len(b) for b in theirs.values()),
                      'only_pg_rewind': sum(len(theirs.get(k, set()) - ours.get(k, set())) for k in keys),
                      'only_waldump': sum(len(ours.get(k, set()) - theirs.get(k, set())) for k in keys),
                      'b_blocks': unionscan.count(b_blocks), 's_blocks': unionscan.count(s_blocks),
                      's_limits': len(limits), 'summaries_cover': covered, 'union_blocks': unionscan.count(union),
                      'hash_pages_sent': statistics.median(sent) if sent else None})
    return cells


def score_b(cells):
    if not cells:
        print('Part B: not run')
        return
    six = all(c['rewind_exit'] == '0' and c['only_pg_rewind'] == 0 and c['only_waldump'] == 0 for c in cells)
    print(f"6. {'HELD' if six else 'FAILED'}: pg_rewind against pg_waldump at scale "
          f"{[(c['lag'], c['rewind_exit'], c['rewind_blocks'], c['only_pg_rewind'], c['only_waldump']) for c in cells]}")
    ratios = [(c['lag'], round(c['union_blocks'] / c['hash_pages_sent'], 3) if c['hash_pages_sent'] else None,
               c['summaries_cover']) for c in cells]
    seven = all(r is not None and 0.9 <= r <= 1.5 and cover for _, r, cover in ratios)
    print(f"7. {'HELD' if seven else 'FAILED'}: idea 1's blocks / hashing's pages sent, summaries cover {ratios}")
    uniform = next((c for c in cells if c['lag'] == 'uniform'), None)
    if uniform:
        share = round(uniform['relation_blocks_mb'] / uniform['beyond_wal_since_fork_mb'], 3) \
            if uniform['beyond_wal_since_fork_mb'] > 0 else None
        eight = share is not None and share < 0.25
        print(f"8. {'HELD' if eight else 'FAILED'}: uniform, relation blocks {uniform['relation_blocks_mb']} MB of "
              f"{uniform['beyond_wal_since_fork_mb']} MB beyond S's WAL since the fork ({share})")
    for c in cells:
        print({k: c[k] for k in ('lag', 'fetched_mb', 'lsn_since_fork_mb', *[f'{k}_mb' for k in KINDS])})


def write(name, table):
    if not table:
        return
    with open(ROOT / 'docs' / 'experiments' / 'data' / name, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(table[0]))
        w.writeheader()
        w.writerows(table)


def figure(rows, cells):
    plotting.style()
    fig, (left, right) = plt.subplots(1, 2, figsize=(10.4, 4.3))
    shown = [r for r in rows if r['guard_ok']]
    for i, r in enumerate(shown):
        bars = (('pages that differ (hashing sends these)', r['pages_changed'], COLORS['black'], None),
                ("S's summaries alone (EXP-61)", r['naive_sent'], COLORS['orange'], r['naive_real_wrong']),
                ('idea 1: both sides\' block sets', r['union_sent'], COLORS['blue'], r['real_wrong']))
        for j, (label, value, color, wrong) in enumerate(bars):
            x = i * 4 + j
            left.bar(x, value, 0.8, color=color, label=label if i == 0 else None)
            if wrong is not None:
                left.annotate(f'{wrong:,}\nreal wrong', (x, value), textcoords='offset points', xytext=(0, 3),
                              ha='center', fontsize=7)
    left.set_xticks([1 + 4 * i for i in range(len(shown))],
                    [f"{r['scenario']}" + (" (B's WAL kept)" if r['variant'] == 'kept' else '') for r in shown])
    left.set_ylabel('pages')
    left.set_ylim(0, max([r['union_sent'] for r in shown] + [r['pages_changed'] for r in shown] + [1]) * 1.25)
    left.legend(fontsize=7.5, loc='lower center', bbox_to_anchor=(0.5, -0.45), ncol=1)
    left.set_title('Local failovers: what each path sends and leaves wrong', fontsize=9.5)
    colors = {'wal_since_fork': COLORS['purple'], 'wal_before_fork': COLORS['red'], 'wal_ahead': COLORS['orange'],
              'relation_blocks': COLORS['blue'], 'fsm_vm_init': COLORS['green'], 'wal_summaries': COLORS['sky'],
              'other': COLORS['grey']}
    labels = {'wal_since_fork': "S's WAL since the fork", 'wal_before_fork': 'WAL segments before the fork',
              'wal_ahead': "WAL segments past S's LSN", 'relation_blocks': 'relation blocks',
              'fsm_vm_init': 'FSM, VM and init forks', 'wal_summaries': 'WAL summaries', 'other': 'other files'}
    for i, c in enumerate(cells):
        bottom = 0
        for kind in KINDS:
            value = c[f'{kind}_mb']
            if value:
                right.barh(i, value, 0.6, left=bottom, color=colors[kind], label=labels[kind] if i == 0 else None)
                bottom += value
        right.barh(i + 0.35, c['union_blocks'] * 8192 / 1e6, 0.25, color=COLORS['blue'], hatch='//',
                   edgecolor=COLORS['black'], lw=0.5, label="idea 1's blocks (8 KiB each)" if i == 0 else None)
    right.set_yticks(range(len(cells)), [c['lag'] for c in cells])
    right.set_xlabel('MB')
    right.set_title("EXP-67's cells: what pg_rewind copies, and idea 1's blocks", fontsize=9.5)
    right.legend(fontsize=7, loc='lower center', bbox_to_anchor=(0.5, -0.5), ncol=2)
    fig.suptitle('EXP-69: both sides\' block sets for an old primary after a failover (counted, not timed)',
                 fontsize=10.5)
    fig.tight_layout()
    print(plotting.save(fig, 'exp69-union'))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--local', default=str(ROOT / 'results' / 'exp69'))
    ap.add_argument('--vm', default=str(ROOT / 'results' / 'exp04' / 'exp69'))
    a = ap.parse_args(argv)
    rows = part_a(a.local) if Path(a.local).exists() else []
    score_a(rows)
    cells = part_b(a.vm) if Path(a.vm).exists() else []
    score_b(cells)
    write('exp69-scenarios.csv', rows)
    write('exp69-rewind-copy.csv', cells)
    if rows or cells:
        figure(rows, cells)


if __name__ == '__main__':
    main()
