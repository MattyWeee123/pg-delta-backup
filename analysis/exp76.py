"""EXP-76's analysis: does early write-back on the sync worker (--fsync worker-writeback) cut the fast path's tail,
the receiver's last syncs after the last page, without slowing the transfer; and where, on a timeline of both disks,
does the fast path spend its time beyond them (docs/experiments/EXP-76-tail-and-timeline.md)?

    .venv/bin/python analysis/exp76.py [--results results/exp04/exp76]

Cells: per pattern and arm, the median of the runs (exp68.runs). The pairs: uniform fast8wf2 (worker) against fast8bf2
(worker-writeback), scatterall12 fast8w against fast8b; scatterall12's full (pg_basebackup) beside them.
Each run's disk series (series-dest.txt, series-source.txt: a timestamp and the /proc/diskstats line of /data, every
0.25 s) gives each disk's busy share and MB/s per interval; a fast run's phases come from its statistics: launch (t0 to
pg_backup_start), the start (to the guard), the guard, the plan (to the first file sent), the sending, and the tail
(the last file sent to t1).
Scores 1 (deciding: worker-writeback's median at least 2.0 s below worker's in each cell), 2 (the receiver's waits
for its worker below 1.0 s in every worker-writeback run), 3 (backup mode, pg_backup_start to pg_backup_stop, within
3% of worker's in each cell), 4 (both disks under 10% busy during the guard in every fast run) and 5 (every run exits
0; every fast run used the fast path and passed the copy's check). Writes docs/experiments/data/exp76-cells.csv,
docs/experiments/data/exp76-phases.csv and the figure exp76-timeline.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

import exp68
import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
PAIRS = {'uniform': ('fast8wf2', 'fast8bf2'), 'scatterall12': ('fast8w', 'fast8b')}
FULL = ('scatterall12', 'full')
PHASES = ('launch', 'start', 'guard', 'plan', 'sending', 'tail')
SAVING_S, WAIT_S, MODE_RATIO, GUARD_BUSY = 2.0, 1.0, 0.03, 0.10


def series(path):
    """[(t, reads, sectors read, writes, sectors written, io_ms)] from a disk_series.sh file."""
    out = []
    for line in Path(path).read_text().splitlines():
        parts = line.split()
        if len(parts) >= 14:
            t, f = float(parts[0]), [int(x) for x in parts[4:14]]
            out.append((t, f[0], f[2], f[4], f[6], f[9]))
    return out


def window(samples, start, end):
    """(seconds, busy share, read MB/s, write MB/s) between the samples nearest start and end."""
    if not samples or end <= start:
        return 0.0, None, None, None
    a = min(samples, key=lambda s: abs(s[0] - start))
    b = min(samples, key=lambda s: abs(s[0] - end))
    span = b[0] - a[0]
    if span <= 0:
        return end - start, None, None, None
    return (end - start, min(1.0, (b[5] - a[5]) / 1e3 / span), (b[2] - a[2]) * 512 / 1e6 / span,
            (b[4] - a[4]) * 512 / 1e6 / span)


def phases(folder, meta):
    """[(phase, start, end)] for a fast run, from its statistics; None for another arm."""
    send = folder / 'send.json'
    if meta['arm'] == 'full' or not send.exists() or not send.stat().st_size:
        return None
    stats = json.loads(send.read_text())
    fast, live = stats.get('fast_path') or {}, stats.get('live') or {}
    guard = fast.get('guard_started_utc')
    if guard is None or fast.get('sending_started_utc') is None:
        return None
    marks = [meta['t0'], live['start_requested_utc'], guard, guard + fast['guard_seconds'],
             fast['sending_started_utc'], fast['sending_ended_utc'], meta['t1']]
    return list(zip(PHASES, marks, marks[1:]))


def run_phases(results):
    """One row per fast run and phase: its seconds and each disk's busy share and MB/s."""
    rows = []
    for meta_path in sorted(Path(results).glob('run-*/meta.json')):
        meta, folder = json.loads(meta_path.read_text()), meta_path.parent
        spans = phases(folder, meta)
        if spans is None or not (folder / 'series-source.txt').exists():
            continue
        disks = {side: series(folder / f'series-{side}.txt') for side in ('source', 'dest')}
        for name, start, end in spans:
            row = {'run': meta['run'], 'pattern': meta['pattern'], 'arm': meta['arm'], 'phase': name}
            for side, samples in disks.items():
                seconds, busy, read, write = window(samples, start, end)
                row.update({'seconds': round(seconds, 3), f'{side}_busy': None if busy is None else round(busy, 3),
                            f'{side}_read_mb_s': None if read is None else round(read, 1),
                            f'{side}_write_mb_s': None if write is None else round(write, 1)})
            rows.append(row)
    return rows


def receiver_numbers(results):
    """{run: (fsync_wait_seconds, backup-mode seconds, fsync_effective)} from send.json."""
    out = {}
    for meta_path in sorted(Path(results).glob('run-*/meta.json')):
        meta = json.loads(meta_path.read_text())
        send = meta_path.parent / 'send.json'
        if meta['arm'] == 'full' or not send.exists() or not send.stat().st_size:
            continue
        stats = json.loads(send.read_text())
        live, receiver = stats.get('live') or {}, stats.get('receiver') or {}
        out[meta['run']] = (receiver.get('fsync_wait_seconds'), live['stop_requested_utc'] - live['started_utc'],
                            receiver.get('fsync_effective'))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp76'))
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp76-cells.csv'))
    ap.add_argument('--phases-out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp76-phases.csv'))
    ap.add_argument('--figure', default='exp76-timeline')
    a = ap.parse_args(argv)
    results = Path(a.results)
    rows = exp68.runs(results)
    numbers = receiver_numbers(results)
    cells = []
    for pattern, arms in PAIRS.items():
        for arm in (*arms, *(['full'] if pattern == FULL[0] else [])):
            mine = [r for r in rows if r['pattern'] == pattern and r['arm'] == arm]
            if not mine:
                continue
            cell = {'pattern': pattern, 'arm': arm, 'runs': len(mine), 'seconds': exp68.median(mine, 'seconds'),
                    'source_io_s': exp68.median(mine, 'source_io_s'), 'dest_io_s': exp68.median(mine, 'dest_io_s')}
            mine_numbers = [numbers[r['run']] for r in mine if r['run'] in numbers]
            if mine_numbers:
                cell.update({'fsync_wait_s': round(statistics.median(n[0] for n in mine_numbers), 2),
                             'backup_mode_s': round(statistics.median(n[1] for n in mine_numbers), 2),
                             'fsync_effective': ' '.join(sorted({str(n[2]) for n in mine_numbers}))})
            cells.append(cell)
            print(cell)
    by = {(c['pattern'], c['arm']): c for c in cells}
    savings = {p: round(by[(p, w)]['seconds'] - by[(p, b)]['seconds'], 2) for p, (w, b) in PAIRS.items()
               if (p, w) in by and (p, b) in by}
    one = len(savings) == len(PAIRS) and all(s >= SAVING_S for s in savings.values())
    near = [p for p, s in savings.items() if SAVING_S - 0.5 <= s < SAVING_S + 0.5]
    print(f"1 (deciding). {'HELD' if one else 'FAILED'}: worker - worker-writeback, seconds {savings} "
          f"(bar {SAVING_S})" + (f'; within 0.5 s of the bar: {near} (rule 11)' if near else ''))
    waits = {r['run']: numbers[r['run']][0] for r in rows if r['run'] in numbers
             and r['arm'] in [b for _, b in PAIRS.values()]}
    effective = {numbers[run][2] for run in waits}
    two = bool(waits) and all(w is not None and w < WAIT_S for w in waits.values()) and effective == {'worker-writeback'}
    print(f"2. {'HELD' if two else 'FAILED'}: the receiver's waits with worker-writeback {sorted(waits.values())} s "
          f"(bar {WAIT_S}; in effect {effective})")
    modes = {p: round(by[(p, b)]['backup_mode_s'] / by[(p, w)]['backup_mode_s'], 3) for p, (w, b) in PAIRS.items()
             if (p, w) in by and (p, b) in by and by[(p, w)].get('backup_mode_s')}
    three = len(modes) == len(PAIRS) and all(abs(m - 1) <= MODE_RATIO for m in modes.values())
    print(f"3. {'HELD' if three else 'FAILED'}: backup mode, worker-writeback / worker {modes} (within {MODE_RATIO})")
    spans = run_phases(results)
    guards = [(s['run'], s['source_busy'], s['dest_busy']) for s in spans if s['phase'] == 'guard']
    four = bool(guards) and all(x is not None and y is not None and x < GUARD_BUSY and y < GUARD_BUSY
                                for _, x, y in guards)
    print(f"4. {'HELD' if four else 'FAILED'}: both disks' busy share during the guard {guards} (bar {GUARD_BUSY})")
    fast = [r for r in rows if r['arm'] != 'full']
    five = (bool(rows) and all(r['status'] == 0 for r in rows) and all(r['receiver_status'] == 0 for r in fast)
            and all(r['fast_used'] and r['match'] is True for r in fast))
    print(f"5. {'HELD' if five else 'FAILED'}: statuses {sorted({r['status'] for r in rows})}; fast used and matched "
          f"{all(r['fast_used'] and r['match'] is True for r in fast)} ({len(fast)} runs)")
    for pattern, arms in PAIRS.items():
        for arm in arms:
            mine = [s for s in spans if s['pattern'] == pattern and s['arm'] == arm]
            print(f'  phases {pattern} {arm}: ' + ', '.join(
                f"{p} {statistics.median(s['seconds'] for s in mine if s['phase'] == p):.2f} s"
                for p in PHASES if any(s['phase'] == p for s in mine)))
    write_csv(a.out, cells)
    write_csv(a.phases_out, spans)
    figure(results, rows, a.figure)


def write_csv(path, rows):
    keys = []
    for row in rows:
        keys += [k for k in row if k not in keys]
    with open(path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, restval='')
        w.writeheader()
        w.writerows(rows)


def figure(results, rows, name):
    """scatterall12's first run of each arm: the source's reads and the destination's writes (MB/s) against the
    seconds since t0, with a fast run's phases shaded."""
    plotting.style()
    arms = [('fast8w', 'fast path, worker'), ('fast8b', 'fast path, worker-writeback'), ('full', 'pg_basebackup')]
    fig, axes = plt.subplots(len(arms), 1, figsize=(10.0, 7.6), sharex=True)
    shades = {'guard': COLORS['red'], 'plan': COLORS['yellow'], 'tail': COLORS['purple']}
    for ax, (arm, label) in zip(axes, arms):
        run = next((r for r in sorted(rows, key=lambda r: r['run']) if r['pattern'] == FULL[0] and r['arm'] == arm),
                   None)
        if run is None:
            continue
        folder = Path(results) / f"run-{run['run']:02d}"
        meta = json.loads((folder / 'meta.json').read_text())
        for side, key, color in (('source', 2, COLORS['blue']), ('dest', 4, COLORS['orange'])):
            samples = series(folder / f'series-{side}.txt') if (folder / f'series-{side}.txt').exists() else []
            xs = [(b[0] + c[0]) / 2 - meta['t0'] for b, c in zip(samples, samples[1:])]
            ys = [(c[key] - b[key]) * 512 / 1e6 / (c[0] - b[0]) for b, c in zip(samples, samples[1:])]
            ax.plot(xs, ys, color=color, lw=0.9,
                    label='source reads' if side == 'source' else 'destination writes')
        for phase, start, end in phases(folder, meta) or []:
            if phase in shades:
                ax.axvspan(start - meta['t0'], end - meta['t0'], color=shades[phase], alpha=0.18,
                           label=phase if arm == 'fast8w' else None)
        ax.axvline(meta['t1'] - meta['t0'], color=COLORS['black'], lw=0.8)
        ax.set_ylabel('MB/s')
        ax.set_ylim(0, max(ax.get_ylim()[1], 1) * 1.3)  # the legend above the lines
        ax.set_title(f"{label}: {meta['t1'] - meta['t0']:.1f} s", fontsize=9.5, loc='left')
        ax.legend(fontsize=7.5, loc='upper right', ncol=5)
    axes[-1].set_xlabel('seconds since the run started (scatterall12, first run of each arm)')
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
