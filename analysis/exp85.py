"""EXP-85's analysis: single-read page-lsn (--lsn-single-read), which sends each chosen page from the scan's own
read, against page-lsn's two reads and pg_basebackup (docs/experiments/EXP-85-single-read-page-lsn.md).

    .venv/bin/python analysis/exp85.py [--trials docs/experiments/data/exp85-trials.csv]
        [--timing docs/experiments/data/exp85-timing.csv] [--results results/exp04/exp85-h2]

The local stage (rule 13): experiments/summaries_off/trials.py --single-read (EXP-82's campaign) and timing.py with
the arms page-lsn and page-lsn-1. The VM stage: one lab run on the ha set at scale 1,200 (EXP-84's spent H2 cell),
hot and uniform, two runs each of sb_lsn (two reads), sb_lsn1 (single read) and full (pg_basebackup). Scores:
L1 every single-read page-lsn trial correct: exit 0, the fast path used with its expected proof, rows equal to S's,
   pg_amcheck clean, and no byte read again (reread_bytes 0);
L2 the negative control (lsn-backup-end) wrong in every trial, so the check can see a wrong copy;
1 (deciding) uniform: single read at most 1.05x pg_basebackup's time (EXP-84's bar, missed there at 1.123x);
2 uniform, if two reads miss again (at least 1.05x pg_basebackup): single read at most 0.95x two reads' time;
3 single read's source-disk reads at most 1.01x pg_basebackup's in each cell (each page read once);
4 hot: single read at most 1.024x two reads' time (rule 17's spread) and at most 1.05x pg_basebackup's;
5 every copy matches the source, and single read held at most LSN_HELD_BYTES plus two chunks in every run.
Times are each arm's mean over its runs. Writes docs/experiments/data/exp85-runs.csv and the figure exp85-single-read.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

import exp68
import exp76
import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pgdelta import fastpath  # noqa: E402

DATA = ROOT / 'docs' / 'experiments' / 'data'
ARMS = ('sb_lsn', 'sb_lsn1', 'full')
LABELS = {'sb_lsn': 'page-lsn (two reads)', 'sb_lsn1': 'page-lsn, single read', 'full': 'pg_basebackup'}
HELD_LIMIT = fastpath.LSN_HELD_BYTES + 2 * fastpath.LSN_SCAN_BYTES


def rows_of(path):
    return list(csv.DictReader(open(path))) if Path(path).exists() else []


def local(trials_path, timing_path):
    trials = rows_of(trials_path)
    expected = {'lsn-standby': 'clean shutdown in recovery', 'lsn-backup': 'backup_manifest',
                'lsn-crashed': 'crashed standby'}
    lsn = [r for r in trials if r['arm'] in expected]
    ok = [r for r in lsn if r['exit'] == '0' and r['fast_used'] == 'True' and r['proof'] == expected[r['arm']]
          and r['rows_equal'] == 'True' and r['amcheck_ok'] == 'True' and r.get('single_read') == 'True'
          and r.get('reread_bytes') == '0']
    l1 = bool(lsn) and len(ok) == len(lsn)
    print(f"L1. {'HELD' if l1 else 'FAILED'}: {len(ok)} of {len(lsn)} single-read page-lsn trials correct, single "
          f"read, nothing read again")
    for r in lsn:
        if r not in ok:
            print(f"   not correct: trial {r.get('trial', '?')} {r['arm']} exit {r['exit']} used "
                  f"{r['fast_used']} proof {r['proof']} rows {r.get('rows_equal')} amcheck {r.get('amcheck_ok')} "
                  f"reread {r.get('reread_bytes')} {r.get('reasons', '')[:80]}")
    controls = [r for r in trials if r['arm'] == 'lsn-backup-end']
    wrong = [r for r in controls if r['exit'] == '0' and (r['rows_equal'] != 'True' or r['amcheck_ok'] != 'True')]
    l2 = bool(controls) and len(wrong) == len(controls)
    print(f"L2. {'HELD' if l2 else 'FAILED'}: {len(wrong)} of {len(controls)} negative-control copies wrong")
    timing = rows_of(timing_path)
    ratios = {}
    for cell in sorted({r['cell'] for r in timing}):
        mean = {arm: statistics.fmean(float(r['seconds']) for r in timing if r['cell'] == cell and r['arm'] == arm)
                for arm in ('page-lsn', 'page-lsn-1') if any(r['cell'] == cell and r['arm'] == arm for r in timing)}
        if len(mean) == 2:
            ratios[cell] = mean['page-lsn-1'] / mean['page-lsn']
            reread = {arm: [r['reread_bytes'] for r in timing if r['cell'] == cell and r['arm'] == arm]
                      for arm in mean}
            print(f"reported local {cell}: single read {mean['page-lsn-1']:.2f} s, two reads {mean['page-lsn']:.2f} s "
                  f"({ratios[cell]:.3f}, the proxy for rule 13's log); bytes read again {reread}")
    return {'l1': l1, 'l2': l2, 'ratios': ratios, 'trials': len(lsn), 'correct': len(ok)}


def vm(results):
    rows = exp68.runs(results) if Path(results).exists() else []
    sends, folders = {}, {}
    for path in Path(results).glob('run-*/meta.json'):
        run = json.loads(path.read_text())['run']
        folders[run] = path.parent
        send = path.parent / 'send.json'
        sends[run] = json.loads(send.read_text()) if send.exists() and send.stat().st_size else {}
    for r in rows:
        fast = sends.get(r['run'], {}).get('fast_path') or {}
        r.update({'scan_s': fast.get('scan_seconds'), 'reread_bytes': fast.get('reread_bytes'),
                  'held_bytes_peak': fast.get('held_bytes_peak'), 'single_read': fast.get('lsn_single_read'),
                  'pages_scanned': fast.get('pages_scanned'), 'folder': str(folders.get(r['run'], ''))})
    cells = {}
    for pattern in ('hot', 'uniform'):
        mine = [r for r in rows if r['pattern'] == pattern]
        if not mine:
            continue
        mean = {arm: statistics.fmean(r['seconds'] for r in mine if r['arm'] == arm)
                for arm in ARMS if any(r['arm'] == arm for r in mine)}
        reads = {arm: statistics.fmean(r['source_read_mb'] for r in mine if r['arm'] == arm)
                 for arm in ARMS if any(r['arm'] == arm and r['source_read_mb'] is not None for r in mine)}
        cells[pattern] = {'mean': mean, 'reads': reads, 'runs': mine}
    verdicts = []

    def ratio(pattern, a, b, table='mean'):
        c = cells.get(pattern, {}).get(table, {})
        return c[a] / c[b] if a in c and b in c else None

    one = ratio('uniform', 'sb_lsn1', 'full')
    verdicts.append(('1 (deciding)', one is not None and one <= 1.05,
                     f"uniform: single read {one if one is None else round(one, 3)}x pg_basebackup (bar 1.05)"))
    two_miss = ratio('uniform', 'sb_lsn', 'full')
    two = ratio('uniform', 'sb_lsn1', 'sb_lsn')
    if two_miss is not None and two_miss < 1.05:
        verdicts.append(('2', None, f"not testable: two reads took {two_miss:.3f}x pg_basebackup, under 1.05, so they "
                                    f"did not miss again (single read {two if two is None else round(two, 3)}x them)"))
    else:
        verdicts.append(('2', two is not None and two <= 0.95,
                         f"uniform: single read {two if two is None else round(two, 3)}x two reads (bar 0.95); two "
                         f"reads {two_miss if two_miss is None else round(two_miss, 3)}x pg_basebackup"))
    three = {p: ratio(p, 'sb_lsn1', 'full', 'reads') for p in cells}
    verdicts.append(('3', bool(three) and all(v is not None and v <= 1.01 for v in three.values()),
                     f"source-disk reads, single read over pg_basebackup's: "
                     f"{ {p: (v if v is None else round(v, 4)) for p, v in three.items()} } (bar 1.01); two reads "
                     f"{ {p: (lambda v: v if v is None else round(v, 4))(ratio(p, 'sb_lsn', 'full', 'reads')) for p in cells} }"))
    hot_two, hot_full = ratio('hot', 'sb_lsn1', 'sb_lsn'), ratio('hot', 'sb_lsn1', 'full')
    verdicts.append(('4', hot_two is not None and hot_two <= 1.024 and hot_full is not None and hot_full <= 1.05,
                     f"hot: single read {hot_two if hot_two is None else round(hot_two, 3)}x two reads (bar 1.024), "
                     f"{hot_full if hot_full is None else round(hot_full, 3)}x pg_basebackup (bar 1.05)"))
    pgdelta = [r for r in rows if r['arm'] != 'full']
    held = [r['held_bytes_peak'] for r in rows if r['arm'] == 'sb_lsn1']
    five = (bool(rows) and all(r['status'] == 0 for r in rows) and all(r['match'] is True for r in pgdelta)
            and all(r['fast_used'] for r in pgdelta) and bool(held)
            and all(h is not None and 0 < h <= HELD_LIMIT for h in held))
    verdicts.append(('5', five, f"{sum(r['match'] is True for r in pgdelta)} of {len(pgdelta)} pgdelta copies "
                                f"matched, every run exit 0: {all(r['status'] == 0 for r in rows)}; single read held "
                                f"at most {max(held) / 2**20 if held else None} MiB (bar {HELD_LIMIT / 2**20:.0f})"))
    for name, ok, detail in verdicts:
        print(f"{name}. {'n/a' if ok is None else 'HELD' if ok else 'FAILED'}: {detail}")
    for pattern, c in cells.items():
        print(f"reported {pattern}: mean seconds {({a: round(v, 2) for a, v in c['mean'].items()})}; source MB read "
              f"{({a: round(v) for a, v in c['reads'].items()})}; per run "
              f"{[(r['arm'], r['seconds'], r['scan_s'], r['reread_bytes']) for r in c['runs']]}")
    return rows, cells


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--trials', default=str(DATA / 'exp85-trials.csv'))
    ap.add_argument('--timing', default=str(DATA / 'exp85-timing.csv'))
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp85-h2'))
    ap.add_argument('--out', default=str(DATA / 'exp85-runs.csv'))
    ap.add_argument('--figure', default='exp85-single-read')
    a = ap.parse_args(argv)
    loc = local(a.trials, a.timing)
    rows, cells = vm(a.results)
    if rows:
        exp76.write_csv(a.out, [{k: v for k, v in r.items() if k not in ('plan', 'folder')} for r in rows])
        figure(cells, a.results, a.figure)
    return loc


def series(path):
    rows = []
    for line in open(path):
        f = line.split()
        if len(f) >= 14:
            rows.append((float(f[0]), int(f[6])))
    return [(t - rows[0][0], (s - rows[0][1]) * 512 / 1e9) for t, s in rows]


def figure(cells, results, name):
    plotting.style()
    fig, (left, right) = plt.subplots(1, 2, figsize=(11, 4.4))
    colors = {'sb_lsn': COLORS['purple'], 'sb_lsn1': COLORS['green'], 'full': COLORS['orange']}
    for i, (pattern, c) in enumerate(cells.items()):
        full = c['mean'].get('full')
        for j, arm in enumerate(ARMS):
            values = [r['seconds'] / full for r in c['runs'] if r['arm'] == arm] if full else []
            if values:
                x = i * 4 + j
                left.bar(x, statistics.fmean(values), 0.8, color=colors[arm], label=LABELS[arm] if i == 0 else None)
                left.plot([x] * len(values), values, 'o', color='black', ms=3)
    left.axhline(1.05, color=COLORS['grey'], lw=1, ls='--', label='the bar (1.05)')
    left.set_xticks([i * 4 + 1 for i in range(len(cells))], list(cells), fontsize=9)
    left.set_ylabel("time over pg_basebackup's (bars: mean; dots: runs)")
    left.set_title('Scale 1,200 (4.5x memory): page-lsn with one read and two', fontsize=9.5)
    left.legend(fontsize=7.5, loc='lower right')
    for arm in ARMS:
        run = next((r for r in cells.get('uniform', {}).get('runs', []) if r['arm'] == arm), None)
        path = Path(run['folder']) / 'series-source.txt' if run and run['folder'] else None
        if path and path.exists():
            pts = series(path)
            right.plot([t for t, _ in pts], [g for _, g in pts], color=colors[arm], lw=1.6, label=LABELS[arm])
    right.set_xlabel('seconds into the refresh (uniform, first run of each arm)')
    right.set_ylabel("GB read from the source's data disk")
    right.set_title('Where the time goes: reads from the source disk', fontsize=9.5)
    right.legend(fontsize=7.5, loc='lower right')
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
