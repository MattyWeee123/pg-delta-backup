"""EXP-12's drift check, as the write-up's "Drift over the session" fixed it during the sweep, before its data was read.

    .venv/bin/python analysis/exp12_drift.py [--sweep results/exp04/exp12-hot]
        [--runs docs/experiments/data/exp12-runs.csv] [--tag ...] [--figdir DIR]

Smoke item 14's registered action wasn't taken (rule 8). What it guards, a load that behaves the same all session, is
judged here from each loaded run's settle (the load-only span before its window) as analysis/exp12.py writes it to
the runs table:
- the busy load (one load run across the busy and warm blocks): the median settle p50 and p99 over the busy block's
  first repeat (its 14 arms' settles; the controls left out) against those over the warm block's settles;
- the moderate load: its first repeat's settles against its last repeat's (the controls left out).
Drift, in a load, if the later median p50 is more than 10% above the earlier, or the later median p99 more than
twice the earlier.

Reported beside, not judged: the same medians for every repeat of every loaded block, the settle gate's passes, the
sizes of loaddb's tables at the trial's start and end, and a per-minute view of each whole load from its pgbench
logs: per whole minute of completions from the load's start, completions per second, and p50 and p99 from a
histogram of 400 bins per decade (each percentile within 0.6% of nearest-rank), over the load-only transactions (those
completed outside every copy's and control's window, t0 to t1 + 15 s; `none`'s is load-only) and over all. The copy windows and the
warm-up are marked. Writes docs/figures/exp12-drift.svg.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import sys
from pathlib import Path

import numpy as np

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import exp12  # noqa: E402

P50_LIMIT, P99_LIMIT = 1.10, 2.0
LOADS = {'busy': (('busy', 'first'), ('warm', 'all')), 'moderate': (('moderate', 'first'), ('moderate', 'last'))}
TABLES = ('load_progress', 'load_progress_pkey', 'pgbench_accounts', 'pgbench_accounts_pkey')
BINS_PER_DECADE, LOW_US, DECADES = 400, 10, 7  # latency histogram: 10 us to 100 s
READ_BYTES = 64 << 20


def number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def read_runs(path):
    """The loaded blocks' runs from analysis/exp12.py's runs table, as dicts: the arms' (controls left out), and
    every run's, controls' included (their windows are marked and kept out of the load-only view)."""
    with open(path, newline='') as handle:
        rows = list(csv.DictReader(handle))
    arms, every = [], []
    for row in rows:
        if row['block'] not in exp12.LOADED_BLOCKS:
            continue
        run = {**row, 'block_repeat': int(row['block_repeat']) if row['block_repeat'] != '' else None,
               't0': number(row['t0_utc']), 't1': number(row['t1_utc']), 'settle_p50': number(row['settle_p50_ms']),
               'settle_p99': number(row['settle_p99_ms']), 'gate': row['settle_gate_passed'] == 'True'}
        every.append(run)
        if not row['control_slot'] and run['block_repeat'] is not None:
            arms.append(run)
    return arms, every


def group(runs, block, which):
    rows = [r for r in runs if r['block'] == block]
    if not rows or which == 'all':
        return rows
    repeat = min(r['block_repeat'] for r in rows) if which == 'first' else max(r['block_repeat'] for r in rows)
    return [r for r in rows if r['block_repeat'] == repeat]


def summary(rows):
    return {'settles': len(rows), 'p50': median(r['settle_p50'] for r in rows),
            'p99': median(r['settle_p99'] for r in rows), 'gate': sum(r['gate'] for r in rows),
            'missing': sum(r['settle_p50'] is None or r['settle_p99'] is None for r in rows)}


def judge(runs):
    """Per load: the earlier and later groups' medians, their ratios and the verdict."""
    out = {}
    for load, ((block_a, which_a), (block_b, which_b)) in LOADS.items():
        early, late = summary(group(runs, block_a, which_a)), summary(group(runs, block_b, which_b))
        names = (f'{block_a} ({which_a} repeat)' if which_a != 'all' else block_a,
                 f'{block_b} ({which_b} repeat)' if which_b != 'all' else block_b)
        if not early['settles'] or not late['settles'] or None in (early['p50'], early['p99'], late['p50'], late['p99']):
            out[load] = {'early': early, 'late': late, 'p50_ratio': None, 'p99_ratio': None, 'drift': None,
                         'groups': names}
            continue
        p50, p99 = late['p50'] / early['p50'], late['p99'] / early['p99']
        out[load] = {'early': early, 'late': late, 'p50_ratio': p50, 'p99_ratio': p99,
                     'drift': p50 > P50_LIMIT or p99 > P99_LIMIT, 'groups': names}
    return out


def by_repeat(runs):
    rows = []
    for block in exp12.LOADED_BLOCKS:
        for repeat in sorted({r['block_repeat'] for r in runs if r['block'] == block}):
            rows.append((block, repeat, summary([r for r in runs if r['block'] == block and r['block_repeat'] == repeat])))
    return rows


# --- the per-minute view, from the load's pgbench logs ---------------------------------------

def log_stream(path):
    """A log file's bytes: zstd-decompressed if it ends in .zst."""
    if path.suffix == '.zst':
        process = subprocess.Popen(['zstd', '-dc', str(path)], stdout=subprocess.PIPE)
        yield from iter(lambda: process.stdout.read(READ_BYTES), b'')
        if process.wait() != 0:
            raise SystemExit(f'zstd failed on {path}')
    else:
        with open(path, 'rb') as handle:
            yield from iter(lambda: handle.read(READ_BYTES), b'')


def chunks(path):
    """(latency us, end us) arrays from a pgbench -l log, a block of lines at a time. Blocks of whole 7-field lines
    read at once; any other block (a failed or skipped transaction) through exp12.parse_chunk."""
    rest = b''
    for data in log_stream(path):
        data = rest + data
        cut = data.rfind(b'\n') + 1
        block, rest = data[:cut], data[cut:]
        if block:
            yield parse(block)
    if rest.strip():
        yield parse(rest + b'\n')


def parse(block):
    lines = block.count(b'\n')
    try:
        values = np.fromstring(block.decode(), dtype=np.int64, sep=' ') if lines else np.zeros(0, np.int64)
    except ValueError:  # a word in a line (numpy 2 stops with an error there)
        values = np.zeros(0, np.int64)
    if lines and values.size == 7 * lines:
        values = values.reshape(lines, 7)
        return values[:, 2], values[:, 4] * exp12.SECOND_US + values[:, 5]
    parsed = exp12.parse_chunk(block.decode().splitlines())
    return parsed['latency'], parsed['end']


def merged(windows):
    """Sorted, merged (start, stop) intervals in whole microseconds, as two arrays."""
    out = []
    for a, b in sorted(windows):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return (np.array([a for a, _ in out], np.int64), np.array([b for _, b in out], np.int64))


def percentiles(hist, edges):
    total = int(hist.sum())
    if not total:
        return None, None
    cumulative = np.cumsum(hist)
    return tuple(float(edges[np.searchsorted(cumulative, q * total) + 1]) / 1000 for q in (0.5, 0.99))


def minute_view(paths, start_s, stop_s, windows):
    """Per whole completion minute from start_s to stop_s: transactions, and p50 and p99 (ms) from a log-binned
    histogram over all and over the load-only transactions (completed outside every window)."""
    edges = LOW_US * 10 ** (np.arange(BINS_PER_DECADE * DECADES + 1) / BINS_PER_DECADE)
    starts, stops = merged(windows)
    nbins = len(edges) - 1
    counts = {}
    start_us, minute_us = int(start_s * exp12.SECOND_US), 60 * exp12.SECOND_US
    for path in paths:
        for latency, end in chunks(path):
            if not latency.size:
                continue
            minute = (end - start_us) // minute_us
            bins = np.clip(np.searchsorted(edges, latency, side='right') - 1, 0, nbins - 1)
            index = np.searchsorted(starts, end, side='right') - 1
            inside = (index >= 0) & (end < stops[np.maximum(index, 0)]) if starts.size else np.zeros(end.size, bool)
            for m in np.unique(minute):
                if m < 0:
                    continue
                hist = counts.setdefault(int(m), np.zeros((2, nbins), np.int64))
                here = minute == m
                hist[0] += np.bincount(bins[here], minlength=nbins)
                hist[1] += np.bincount(bins[here & ~inside], minlength=nbins)
    whole = int((stop_s - start_s) // 60)
    out = []
    for m in sorted(counts):
        if m >= whole:
            continue
        p50, p99 = percentiles(counts[m][0], edges)
        p50_only, p99_only = percentiles(counts[m][1], edges)
        out.append({'minute': m, 'transactions': int(counts[m][0].sum()), 'p50_ms': p50, 'p99_ms': p99,
                    'load_only': int(counts[m][1].sum()), 'p50_only_ms': p50_only, 'p99_only_ms': p99_only})
    return out


def load_runs(sweep, trial):
    """The trial's rated load runs (busy, moderate) with their log paths."""
    folder = sweep / 'logs' / trial['trial_id'] / 'load'
    out = []
    for run in exp12.get(trial, 'load', 'runs') or []:
        if run.get('kind') not in LOADS or not run.get('rate'):
            continue
        paths = []
        for name in run.get('log_files') or []:
            path = folder / f'{name}.zst'
            paths.append(path if path.exists() else folder / name)
        out.append({**run, 'paths': [p for p in paths if p.exists()], 'missing': [p.name for p in paths if not p.exists()]})
    return out


def windows_of(every, blocks):
    """Every copy's or control's window in these blocks, t0 to t1 + 15 s, in whole microseconds (`none`'s is
    load-only)."""
    return [(int(r['t0'] * exp12.SECOND_US), int((r['t1'] + exp12.TAIL15_S) * exp12.SECOND_US))
            for r in every if r['block'] in blocks and r['method'] != 'none' and r['t0'] is not None
            and r['t1'] is not None]


def figure(views, runs, every, tag):
    loads = [v for v in views if v['minutes']]
    if not loads:
        return 'no per-minute view: no load logs'
    fig, axes = plt.subplots(3, len(loads), figsize=(6.2 * len(loads), 7.8), sharex='col', squeeze=False)
    for column, view in enumerate(loads):
        start = view['run']['started_utc']
        rows = view['minutes']
        minutes = [m['minute'] + 0.5 for m in rows]
        top, middle, bottom = axes[0, column], axes[1, column], axes[2, column]
        top.plot(minutes, [m['transactions'] / 60 for m in rows], color=COLORS['black'], lw=1.2,
                 label='completed, per minute')
        top.axhline(view['run']['rate'], color=COLORS['grey'], lw=0.8, ls='--', label=f"scheduled: {view['run']['rate']:,}/s")
        top.set_ylim(0.9 * view['run']['rate'], 1.05 * view['run']['rate'])
        middle.plot(minutes, [m['p50_only_ms'] for m in rows], color=COLORS['black'], lw=1.2,
                    label='per minute, load-only')
        bottom.plot(minutes, [m['p99_ms'] for m in rows], color=COLORS['grey'], lw=0.8, alpha=0.7,
                    label='per minute, all (copies\' included)')
        bottom.plot(minutes, [m['p99_only_ms'] for m in rows], color=COLORS['black'], lw=1.2,
                    label='per minute, load-only')
        blocks = [block for block, _ in LOADS[view['run']['kind']]]
        for a, b in windows_of(every, blocks):
            for ax in (top, middle, bottom):
                ax.axvspan((a / exp12.SECOND_US - start) / 60, (b / exp12.SECOND_US - start) / 60,
                           color=COLORS['orange'], alpha=0.18, lw=0)
        for warmup in view['warmups']:
            for ax in (top, middle, bottom):
                ax.axvspan((warmup['started_utc'] - start) / 60, (warmup['ended_utc'] - start) / 60,
                           color=COLORS['grey'], alpha=0.12, lw=0)
        early, late = (group(runs, block, which) for block, which in LOADS[view['run']['kind']])
        for rows, color, name in ((early, COLORS['blue'], 'earlier settles (judged)'),
                                  (late, COLORS['red'], 'later settles (judged)')):
            x = [(r['t0'] - start) / 60 for r in rows if r['t0'] is not None]
            middle.scatter(x, [r['settle_p50'] for r in rows if r['t0'] is not None], s=14, color=color, zorder=3, label=name)
            bottom.scatter(x, [r['settle_p99'] for r in rows if r['t0'] is not None], s=14, color=color, zorder=3, label=name)
        top.set_title(f"the {view['run']['kind']} load ({view['run']['rate']:,}/s)")
        top.set_ylabel('completed per second')
        middle.set_ylabel('p50 latency (ms)')
        bottom.set_ylabel('p99 latency (ms)')
        bottom.set_yscale('log')
        bottom.set_xlabel('minutes from the load\'s start (orange: copy windows; grey: the warm-up)')
        top.legend(loc='lower right', fontsize=8.5)
        middle.legend(loc='upper right', fontsize=8.5)
        bottom.legend(loc='upper right', fontsize=8.5)
    fig.suptitle('EXP-12: does the load drift over the session?', fontsize=12)
    fig.tight_layout()
    return str(plotting.save(fig, f'exp12-drift{tag}'))


def fmt(value, digits=3):
    return '–' if value is None else f'{value:.{digits}f}'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--sweep', default=str(ROOT / 'results' / 'exp04' / 'exp12-hot'))
    parser.add_argument('--runs', default=str(exp12.DATA / 'exp12-runs.csv'),
                        help="analysis/exp12.py's runs table for the same sweep")
    parser.add_argument('--tag', default='', help='suffix for the figure file')
    parser.add_argument('--figdir', help='write the figure here instead of docs/figures')
    parser.add_argument('--no-logs', action='store_true', help='skip the per-minute view')
    args = parser.parse_args(argv)
    if args.figdir:
        plotting.FIGURES = Path(args.figdir)
    plotting.style()
    sweep = Path(args.sweep)
    runs, every = read_runs(args.runs)
    if not runs:
        raise SystemExit(f'no loaded runs in {args.runs}')

    print(f'{len(runs)} loaded runs (controls left out) from {args.runs}')
    print(f'Drift: the later median settle p50 over the earlier above {P50_LIMIT:g}, or p99 above {P99_LIMIT:g}')
    for load, j in judge(runs).items():
        names = j.get('groups', ('–', '–'))
        verdict = {True: 'DRIFT', False: 'no drift', None: 'not judged (a group is empty or lacks settles)'}[j['drift']]
        print(f"- the {load} load: {verdict}. p50 {fmt(j['early']['p50'])} -> {fmt(j['late']['p50'])} ms "
              f"(x{fmt(j['p50_ratio'])}); p99 {fmt(j['early']['p99'])} -> {fmt(j['late']['p99'])} ms "
              f"(x{fmt(j['p99_ratio'])}); {names[0]}: {j['early']['settles']} settles, {names[1]}: "
              f"{j['late']['settles']}")
    print('\nBy repeat (reported, not judged): block, repeat, settles, median settle p50 and p99 (ms), gate passes')
    for block, repeat, s in by_repeat(runs):
        print(f"  {block:9s} {repeat + 1}  {s['settles']:2d}  {fmt(s['p50'])}  {fmt(s['p99'])}  {s['gate']}/{s['settles']}"
              + (f"  ({s['missing']} without a settle)" if s['missing'] else ''))

    views = []
    for path in sorted((sweep / 'trials').glob('*.json')):
        trial = json.loads(path.read_text())
        pages = [exp12.get(trial, 'references', side, 'loaddb_pages') or {} for side in ('start', 'end')]
        print(f"\n{trial['trial_id']}: loaddb's pages at the start and the end (growth "
              f"{fmt(100 * exp12.get(trial, 'references', 'end', 'loaddb_growth'), 1) if exp12.get(trial, 'references', 'end', 'loaddb_growth') is not None else '–'}%)")
        for table in TABLES:
            print(f'  {table:22s} {pages[0].get(table, "–")} -> {pages[1].get(table, "–")}')
        if args.no_logs:
            continue
        for run in load_runs(sweep, trial):
            if run['missing']:
                print(f"  the {run['kind']} load: missing logs {', '.join(run['missing'])}")
            stop = run.get('stopped_utc') or run.get('signalled_utc')
            blocks = [block for block, _ in LOADS[run['kind']]]
            minutes = minute_view(run['paths'], run['started_utc'], stop, windows_of(every, blocks))
            warmups = [w for w in exp12.get(trial, 'load', 'warmups') or [] if w.get('run_id') == run['run_id']]
            views.append({'run': run, 'minutes': minutes, 'warmups': warmups})
            after = max([w['ended_utc'] for w in warmups], default=run['started_utc'])
            rows = [m for m in minutes if run['started_utc'] + 60 * m['minute'] >= after and m['p50_only_ms'] is not None]
            if not rows:
                print(f"  the {run['kind']} load: no whole minute after its warm-up")
                continue
            first, last = rows[:10], rows[-10:]
            print(f"  the {run['kind']} load: {len(minutes)} whole minutes, {len(rows)} after the warm-up; completions per "
                  f"second {min(m['transactions'] for m in rows) / 60:,.0f}–{max(m['transactions'] for m in rows) / 60:,.0f};"
                  f" load-only p50 {fmt(min(m['p50_only_ms'] for m in rows), 2)}–{fmt(max(m['p50_only_ms'] for m in rows), 2)}"
                  f" ms, p99 {fmt(min(m['p99_only_ms'] for m in rows), 2)}–{fmt(max(m['p99_only_ms'] for m in rows), 2)} ms;"
                  f" medians over its first and last 10 such minutes: p50 {fmt(median(m['p50_only_ms'] for m in first), 3)}"
                  f" and {fmt(median(m['p50_only_ms'] for m in last), 3)} ms, p99 {fmt(median(m['p99_only_ms'] for m in first), 2)}"
                  f" and {fmt(median(m['p99_only_ms'] for m in last), 2)} ms (reported, not judged)")
    if views:
        print(figure(views, runs, every, args.tag))


if __name__ == '__main__':
    main()
