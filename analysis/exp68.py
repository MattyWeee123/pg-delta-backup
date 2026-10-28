"""EXP-68's analysis: the receiver's syncs moved off its main loop (--fsync worker), with 8 and 16 reader threads, on
EXP-64's cells, with each run's disk counters and the fast path's real page set
(docs/experiments/EXP-68-receiver-syncs-off-the-loop.md).

    .venv/bin/python analysis/exp68.py [--results results/exp04/exp68]

Per pattern (hot, uniform) and arm (fast, fast8, fast16: the fast path with --read-ahead 0, 8 and 16 and the
receiver's syncs in its main loop; fast8w and fast16w: the same with --fsync worker; full): the median time of the 2
runs (t1 - t0 on dest's clock), the sender's and receiver's timers as in EXP-64b, and from /proc/diskstats of the
disk behind /data the requests each VM completed in the timed window. Scores, on the uniform cell unless named:
1 (deciding: fast8w at most 0.85 of fast8), 2 (fast8w at most 1.0 of a full copy), 3 (fast16w within 10% of
fast8w), 4 (fast8's dest completed 0.7 to 1.5 write requests per run of the plan), 5 (hot: fast8w at most
max(fast + 0.3 s, 1.15 x fast)), 6 (every run and receiver exited 0, every fast run used the fast path on B0's
backup_manifest and its copy matched the source, each pattern's plans were identical and the threaded runs read as
many runs as the plan lists) and 7 (controls: fast8 within 10% of EXP-64b's 102.77 s, full within 5% of its
90.96 s). Then EXP-66's gap-fill model on the real uniform page set, and its decision rule re-applied: idea 2 is
prototyped if the model without filling is within 30% of fast8's receiver writing and syncing (rebuild_s, its syncs
included) and the best fill cuts the modelled time by at least 25%. Writes docs/experiments/data/exp68-cells.csv,
exp68-model.csv and the figure exp68-syncs.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import statistics
from pathlib import Path

import numpy as np

import exp66
import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = ('hot', 'uniform')
ARMS = ('fast', 'fast8', 'fast16', 'fast8w', 'fast16w', 'full')
LABELS = {'fast': 'no reader threads', 'fast8': '8 threads,\nsyncs\nin loop', 'fast16': '16 threads,\nsyncs\nin loop',
          'fast8w': '8 threads,\nsync\nworker', 'fast16w': '16 threads,\nsync\nworker', 'full': 'pg_base-\nbackup'}
EXP64B = {'fast8': 102.77, 'full': 90.96}  # EXP-64b's medians on the uniform cell
# /proc/diskstats fields after the device name (Documentation/admin-guide/iostats.rst).
FIELDS = ('reads', 'reads_merged', 'sectors_read', 'read_ms', 'writes', 'writes_merged', 'sectors_written',
          'write_ms', 'in_flight', 'io_ms')


def diskstats(path):
    """{side: {field: after - before}} from a run's diskstats.txt ("before|after dest|source" + a diskstats line)."""
    seen = {}
    for line in path.read_text().splitlines():
        words = line.split()
        if len(words) < 5 + len(FIELDS):
            continue
        when, side = words[0], words[1]
        seen[(when, side)] = [int(x) for x in words[5:5 + len(FIELDS)]]
    out = {}
    for side in ('dest', 'source'):
        if ('before', side) in seen and ('after', side) in seen:
            out[side] = {f: b - a for f, a, b in zip(FIELDS, seen[('before', side)], seen[('after', side)])
                         if f != 'in_flight'}
    return out


def runs(results):
    out = []
    for meta_path in sorted(Path(results).glob('run-*/meta.json')):
        meta = json.loads(meta_path.read_text())
        folder = meta_path.parent
        send = folder / 'send.json'
        stats = json.loads(send.read_text()) if send.exists() and send.stat().st_size else {}
        fast = stats.get('fast_path') or {}
        ahead = fast.get('read_ahead') or {}
        receiver = stats.get('receiver') or {}
        disks = diskstats(folder / 'diskstats.txt') if (folder / 'diskstats.txt').exists() else {}
        dest, source = disks.get('dest') or {}, disks.get('source') or {}
        out.append({**{k: meta[k] for k in ('run', 'pattern', 'arm', 'repeat', 'status')},
                    'receiver_status': meta.get('receiver_status'), 'seconds': round(meta['t1'] - meta['t0'], 3),
                    'match': (meta.get('verify') or {}).get('match'), 'plan': meta.get('plan'),
                    'fast_used': fast.get('used'),
                    'proof': ((receiver.get('fast_path') or {}).get('facts') or {}).get('proof'),
                    'fsync_mode': receiver.get('fsync_mode'), 'threads': ahead.get('threads'),
                    'reads_run': ahead.get('reads'), 'read_wait_s': ahead.get('wait_seconds'),
                    'pages_sent': (stats.get('pages') or {}).get('sent'),
                    'source_s': (stats.get('timings_seconds') or {}).get('hash_source'),
                    'blocked_s': (stats.get('protocol_timers') or {}).get('page_send_s'),
                    'recv_wait_s': (receiver.get('protocol_timers') or {}).get('recv_wait_s'),
                    'rebuild_s': receiver.get('rebuild_seconds'), 'fsync_s': receiver.get('fsync_seconds'),
                    'fsync_wait_s': receiver.get('fsync_wait_seconds'), 'guard_s': fast.get('guard_seconds'),
                    'dest_writes': dest.get('writes'), 'dest_writes_merged': dest.get('writes_merged'),
                    'dest_write_mb': round(dest['sectors_written'] * 512 / 1e6, 1) if dest else None,
                    'dest_reads': dest.get('reads'), 'dest_io_s': round(dest['io_ms'] / 1e3, 2) if dest else None,
                    'source_reads': source.get('reads'), 'source_reads_merged': source.get('reads_merged'),
                    'source_read_mb': round(source['sectors_read'] * 512 / 1e6, 1) if source else None,
                    'source_io_s': round(source['io_ms'] / 1e3, 2) if source else None})
    return out


def median(rows, key):
    values = [r[key] for r in rows if r.get(key) is not None]
    return round(statistics.median(values), 2) if values else None


def plan_runs(results, pattern):
    """The fast path's real plan for a pattern: (starts, lengths) of the runs in the files not sent whole, each file
    numbered as its own segment for EXP-66's fill model, plus the bytes of the files sent whole."""
    path = Path(results) / f'plan-runs-{pattern}.json.gz'
    if not path.exists():
        return None
    plan = json.loads(gzip.decompress(path.read_bytes()))
    starts, lengths, whole = [], [], 0
    for number, entry in enumerate(plan['files']):
        whole += entry['size'] if entry['whole'] else 0
        for first, count in entry['runs']:
            starts.append(number * exp66.SEGMENT_PAGES + first)
            lengths.append(count)
    return np.array(starts, dtype=np.int64), np.array(lengths, dtype=np.int64), whole


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp68'))
    a = ap.parse_args(argv)
    rows = runs(a.results)
    for r in rows:
        print(r)
    cells = {}
    for pattern in PATTERNS:
        for arm in ARMS:
            mine = [r for r in rows if r['pattern'] == pattern and r['arm'] == arm and r['status'] == 0]
            if not mine:
                continue
            cell = {'pattern': pattern, 'arm': arm, 'runs': len(mine), 'seconds': median(mine, 'seconds')}
            for key in ('pages_sent', 'reads_run', 'source_s', 'blocked_s', 'read_wait_s', 'recv_wait_s', 'rebuild_s',
                        'fsync_s', 'fsync_wait_s', 'guard_s', 'dest_writes', 'dest_writes_merged', 'dest_write_mb',
                        'dest_reads', 'dest_io_s', 'source_reads', 'source_reads_merged', 'source_read_mb',
                        'source_io_s'):
                cell[key] = median(mine, key)
            cells[(pattern, arm)] = cell
            print(cell)
    t = lambda pattern, arm: (cells.get((pattern, arm)) or {}).get('seconds')  # noqa: E731
    ratio = lambda x, y, pattern='uniform': (round(t(pattern, x) / t(pattern, y), 3)  # noqa: E731
                                             if t(pattern, x) and t(pattern, y) else None)
    held = lambda v, bar: v is not None and v <= bar  # noqa: E731
    one, two, three = ratio('fast8w', 'fast8'), ratio('fast8w', 'full'), ratio('fast16w', 'fast8w')
    print(f"1 (deciding). {'HELD' if held(one, 0.85) else 'FAILED'}: uniform, fast8w / fast8 {one} (bar 0.85)"
          + ('; within 0.05 of the bar (rule 11)' if one is not None and abs(one - 0.85) <= 0.05 else ''))
    print(f"2. {'HELD' if held(two, 1.0) else 'FAILED'}: uniform, fast8w / full {two} (bar 1.0); fast16w / full "
          f"{ratio('fast16w', 'full')}, fast8 / full {ratio('fast8', 'full')} (reported)")
    print(f"3. {'HELD' if three is not None and abs(three - 1) <= 0.10 else 'FAILED'}: fast16w / fast8w {three} "
          f"(within 0.10 of 1); fast16 / fast8 {ratio('fast16', 'fast8')} (reported)")
    real = plan_runs(a.results, 'uniform')
    run_count = len(real[0]) if real else None
    writes = (cells.get(('uniform', 'fast8')) or {}).get('dest_writes')
    per_run = round(writes / run_count, 3) if writes and run_count else None
    print(f"4. {'HELD' if per_run is not None and 0.7 <= per_run <= 1.5 else 'FAILED'}: fast8's dest completed "
          f"{writes} write requests for {run_count} runs, {per_run} a run (bar 0.7 to 1.5)")
    hot_fast, hot_w = t('hot', 'fast'), t('hot', 'fast8w')
    five = hot_fast is not None and hot_w is not None and hot_w <= max(hot_fast + 0.3, 1.15 * hot_fast)
    print(f"5. {'HELD' if five else 'FAILED'}: hot, fast8w {hot_w} s against fast {hot_fast} s "
          f"(bar max(fast + 0.3 s, 1.15 x fast))")
    tool_rows = [r for r in rows if r['arm'] != 'full']
    plans_ok = all(len({r['plan'] for r in rows if r['pattern'] == p and r['arm'] != 'full'}) == 1
                   for p in PATTERNS if any(r['pattern'] == p for r in tool_rows))
    reads_ok = run_count is not None and all(r['reads_run'] == run_count for r in tool_rows
                                             if r['pattern'] == 'uniform' and r['threads'])
    six = (bool(rows) and all(r['status'] == 0 for r in rows) and all(r['receiver_status'] == 0 for r in tool_rows)
           and all(r['fast_used'] and r['match'] and r['proof'] == 'backup_manifest' for r in tool_rows)
           and all((r['fsync_mode'] == 'worker') == r['arm'].endswith('w') for r in tool_rows)
           and plans_ok and reads_ok)
    print(f"6. {'HELD' if six else 'FAILED'}: statuses {sorted({r['status'] for r in rows})}, receivers "
          f"{sorted({r['receiver_status'] for r in tool_rows}, key=str)}; plans identical per pattern {plans_ok}, "
          f"reads equal the plan's {run_count} runs {reads_ok}; fast runs "
          f"{[(r['pattern'], r['arm'], r['fast_used'], r['proof'], r['match'], r['fsync_mode']) for r in tool_rows]}")
    c8, cf = t('uniform', 'fast8'), t('uniform', 'full')
    seven = (c8 is not None and abs(c8 / EXP64B['fast8'] - 1) <= 0.10 and cf is not None
             and abs(cf / EXP64B['full'] - 1) <= 0.05)
    print(f"7. {'HELD' if seven else 'FAILED'}: fast8 {c8} against EXP-64b's {EXP64B['fast8']} (10%), full {cf} "
          f"against {EXP64B['full']} (5%)")
    data = ROOT / 'docs' / 'experiments' / 'data'
    keys = list(dict.fromkeys(k for c in cells.values() for k in c))
    with open(data / 'exp68-cells.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, restval='')
        w.writeheader()
        w.writerows(cells.values())
    model_rows = None
    if real:
        fio = exp66.load(ROOT / 'results' / 'exp04' / 'exp66')
        model_rows = exp66.fill_rows(exp66.cost_of(fio), real[0], real[1])
        for row in model_rows:
            print(row)
        base, best = model_rows[0]['seconds'], min(model_rows, key=lambda r: r['seconds'])
        measured = (cells.get(('uniform', 'fast8')) or {}).get('rebuild_s')
        close = measured is not None and abs(base / measured - 1) <= 0.30
        cut = round(1 - best['seconds'] / base, 3)
        print(f"EXP-66's rule on the real page set ({run_count} runs in files not sent whole, {real[2] / 1e6:,.0f} MB in files sent whole): "
              f"the model without filling {base} s against fast8's measured writing and syncing {measured} s "
              f"({'within' if close else 'not within'} 30%); best fill {best['gap_pages']} pages, {best['seconds']} s, "
              f"a cut of {cut:.1%}: {'prototype idea 2' if close and cut >= 0.25 else 'idea 2 is not prototyped'}")
        with open(data / 'exp68-model.csv', 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(model_rows[0]))
            w.writeheader()
            w.writerows(model_rows)
    figure(cells, model_rows)


def figure(cells, model_rows):
    plotting.style()
    fig, (left, middle, right) = plt.subplots(1, 3, figsize=(13.2, 4.4), gridspec_kw={'width_ratios': [1.3, 1, 1]})
    arms = [arm for arm in ARMS if ('uniform', arm) in cells and arm != 'fast']
    colors = {'fast8': COLORS['purple'], 'fast16': COLORS['sky'], 'fast8w': COLORS['blue'], 'fast16w': COLORS['green'],
              'full': COLORS['orange']}
    for i, arm in enumerate(arms):
        value = cells[('uniform', arm)]['seconds']
        left.bar(i, value, 0.75, color=colors[arm])
        left.annotate(f'{value:.1f}', (i, value), textcoords='offset points', xytext=(0, 3), ha='center', fontsize=8.5)
    left.axhline(EXP64B['fast8'], color=COLORS['black'], lw=0.8, ls=':')
    left.text(len(arms) - 0.5, EXP64B['fast8'] + 2, f"EXP-64b, 8 threads: {EXP64B['fast8']:.1f} s", ha='right',
              fontsize=7.5, bbox={'facecolor': 'white', 'edgecolor': 'none', 'alpha': 0.85, 'pad': 1})
    left.set_xticks(range(len(arms)), [LABELS[arm] for arm in arms], fontsize=8)
    left.set_ylabel('seconds to refresh about 18 GB (median)')
    left.set_title('Scattered changes (uniform 1%, 28% of pages)', fontsize=10)
    fast_arms = [arm for arm in arms if arm != 'full']
    # Changed after the data: the sender's wait on its reader threads added, the part that binds with the worker.
    parts = (('receiver waiting for data', COLORS['grey'], 'recv_wait_s'),
             ('receiver writing (and syncing, in loop)', COLORS['red'], 'rebuild_s'),
             ('sender blocked sending', COLORS['sky'], 'blocked_s'),
             ('sender waiting on its reader threads', COLORS['green'], 'read_wait_s'))
    for j, (label, color, key) in enumerate(parts):
        middle.barh([k * 5 + j for k in range(len(fast_arms))],
                    [cells[('uniform', arm)][key] or 0 for arm in fast_arms], 0.8, color=color, label=label)
    middle.set_yticks([k * 5 + 1.5 for k in range(len(fast_arms))], [LABELS[arm].replace('\n', ' ') for arm in fast_arms],
                      fontsize=8)
    middle.invert_yaxis()
    middle.set_xlabel('seconds (median)')
    middle.legend(fontsize=7.5, loc='upper center', bbox_to_anchor=(0.4, -0.16), ncol=1, frameon=False)
    middle.set_title('Where the time went', fontsize=10)
    if model_rows:
        gaps = [r['gap_pages'] for r in model_rows]
        right.bar(gaps, [r['seconds'] for r in model_rows], 0.7, color=COLORS['blue'], label='real page set (model)')
        measured = (cells.get(('uniform', 'fast8')) or {}).get('rebuild_s')
        if measured:
            right.axhline(measured, color=COLORS['red'], lw=0.9, ls='--',
                          label=f'measured writing and syncing, {measured:.0f} s')
        right.set_xlabel('clean gaps filled, up to this many pages')
        right.set_ylabel('modelled seconds to write the page set')
        right.legend(fontsize=7.5, loc='upper right')
        right.set_ylim(0, max([r['seconds'] for r in model_rows] + [measured or 0]) * 1.35)
        right.set_title(f"Gap-filled writes on the real page set\n({model_rows[0]['writes']:,} runs unfilled)",
                        fontsize=9.5)
    fig.suptitle('EXP-68: the receiver\'s syncs off its main loop (8 GB VMs, balanced disks)', fontsize=10.5)
    fig.tight_layout()
    print(plotting.save(fig, 'exp68-syncs'))


if __name__ == '__main__':
    main()
