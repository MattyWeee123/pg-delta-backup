"""EXP-09 (direct reads, v1's path) and EXP-10 (the destination write path), as pre-registered.

    .venv/bin/python analysis/exp09_10.py [--sweep results/exp04/exp09-10-ext4] [--tag ...]

Reads the harness's summary.csv and per-run records from `remote.py fetch`, writes the curated
data docs/experiments/data/exp09-10-runs.csv and exp09-10-cells.csv, the figures
docs/figures/exp09-direct.svg and exp10-write-path.svg, and
prints every prediction and decision rule of docs/experiments/EXP-09-direct-read.md and
EXP-10-write-path.md with its verdict. Every statistic is a per-workload median over the
repeats, as the revised pre-registrations say; maxima are reported beside them.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'docs' / 'experiments' / 'data'
PATTERNS = ['uniform', 'hot', 'append']
LOCAL = ['hot', 'append']
SPEEDS = ['2g', '5g', 'unthrottled']
SPEED_LABEL = {'2g': '2 Gbit/s', '5g': '5 Gbit/s', 'unthrottled': 'unthrottled'}
EXP09 = [('full', 'Full copy (pg_basebackup)', COLORS['grey']),
         ('pgdelta', 'pgdelta v0 (local backup first)', COLORS['blue']),
         ('pgdelta_direct', 'Direct read, new copy', COLORS['orange']),
         ('pgdelta_direct_inplace', 'Direct read, in place (v1 path)', COLORS['green'])]
EXP10 = [('pgdelta', 'baseline: copy-patch, pipelined, fsync in loop'),
         ('pgdelta_seq', '(a)(c) copy-patch, sequential verify'),
         ('pgdelta_gapfill_seq', '(a) gap-fill, sequential verify'),
         ('pgdelta_fsync_wb', '(b) early write-back'),
         ('pgdelta_fsync_worker', '(b) fsync on a worker'),
         ('pgdelta_inplace', '(e) in place, startup block')]
TIMERS = ('hash_source', 'hash_basis', 'rebuild', 'copy_basis', 'fsync', 'verify', 'verify_wait')
NUMERIC = ('total_s', 'total_no_ssh_s', 'transfer_s', 'receiver_start_s', 'capture_s', 'receiver_capture_s',
           'cpu_source_s', 'cpu_dest_s', 'wire_s2d_bytes', 'wire_d2s_bytes', 'measured_s2d_gbit', 'fsync_wait_s')


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load(sweep: Path):
    """One dict per run: the summary row's numbers plus pgdelta's own phase timers."""
    rows = []
    with (sweep / 'summary.csv').open() as f:
        for row in csv.DictReader(f):
            run = {k: row[k] for k in ('trial_id', 'pattern', 'speed', 'method', 'repeat', 'status')}
            run.update({k: number(row.get(k)) for k in NUMERIC})
            run['verified'] = row.get('verified') == 'True'
            record = sweep / 'runs' / row['trial_id'] / f"r{row['repeat']}-{row['speed']}-{row['method']}.json"
            timers = {}
            if record.exists():
                reported = json.loads(record.read_text()).get('tool_reported') or {}
                timers = reported.get('timings_seconds') or {}
            for key in TIMERS:
                run[f'{key}_s'] = number(timers.get(key))
            rows.append(run)
    return rows


def cells(rows):
    """(pattern, speed, method) -> {n, <col>: (median, max)} over passing, verified runs."""
    out = {}
    for run in rows:
        if run['status'] != 'pass' or not run['verified']:
            continue
        out.setdefault((run['pattern'], run['speed'], run['method']), []).append(run)
    table = {}
    for key, runs in out.items():
        cell = {'n': len(runs), 'runs': runs}
        for col in (*NUMERIC, *(f'{t}_s' for t in TIMERS)):
            values = [r[col] for r in runs if r[col] is not None]
            if values:
                cell[col] = (statistics.median(values), max(values))
        table[key] = cell
    return table


def med(table, pattern, speed, method, col='total_s'):
    cell = table.get((pattern, speed, method))
    return None if not cell or col not in cell else cell[col][0]


def median_run(table, pattern, speed, method, col='transfer_s'):
    """The run whose `col` is the median (the lower middle for an even count), for stacked breakdowns."""
    runs = sorted((r for r in table[(pattern, speed, method)]['runs'] if r[col] is not None), key=lambda r: r[col])
    return runs[(len(runs) - 1) // 2]


def write_data(rows, table, tag):
    DATA.mkdir(parents=True, exist_ok=True)
    keys = ['trial_id', 'pattern', 'speed', 'method', 'repeat', 'status', 'verified', *NUMERIC, *(f'{t}_s' for t in TIMERS)]
    with (DATA / f'exp09-10{tag}-runs.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for run in sorted(rows, key=lambda r: (r['pattern'], r['speed'], r['method'], r['repeat'])):
            writer.writerow({k: run.get(k) for k in keys})
    cols = ['total_s', 'transfer_s', 'receiver_start_s', 'capture_s', 'rebuild_s', 'copy_basis_s', 'fsync_s',
            'fsync_wait_s', 'verify_wait_s', 'hash_source_s', 'hash_basis_s', 'cpu_source_s', 'wire_s2d_bytes']
    with (DATA / f'exp09-10{tag}-cells.csv').open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['pattern', 'speed', 'method', 'n', *(f'{c}_{s}' for c in cols for s in ('median', 'max'))])
        for (pattern, speed, method), cell in sorted(table.items()):
            writer.writerow([pattern, speed, method, cell['n'],
                             *(round(cell[c][i], 4) if c in cell else '' for c in cols for i in (0, 1))])


def verdicts(table):
    """Every registered prediction and rule, in order; returns markdown lines."""
    lines = []
    say = lines.append

    def fmt(v):
        return '—' if v is None else f'{v:.2f}'

    say('## EXP-09')
    say('| Workload | Speed | Full | v0 | Direct | Direct, in place | Direct transfer − v0 transfer |')
    say('|---|---|---:|---:|---:|---:|---:|')
    for pattern in PATTERNS:
        for speed in SPEEDS:
            gap = None
            d, v = med(table, pattern, speed, 'pgdelta_direct', 'transfer_s'), med(table, pattern, speed, 'pgdelta', 'transfer_s')
            if d is not None and v is not None:
                gap = d - v
            say(f'| {pattern} | {SPEED_LABEL[speed]} | ' + ' | '.join(fmt(med(table, pattern, speed, m)) for m, *_ in EXP09)
                + f' | {fmt(gap)} |')
    d1 = [(p, s, med(table, p, s, 'pgdelta_direct', 'transfer_s') - med(table, p, s, 'pgdelta', 'transfer_s'))
          for p in PATTERNS for s in SPEEDS
          if med(table, p, s, 'pgdelta_direct', 'transfer_s') is not None and med(table, p, s, 'pgdelta', 'transfer_s') is not None]
    held = [g for *_, g in d1 if 0.2 <= g <= 0.6]
    say(f'- **D1** (direct transfer 0.2–0.6 s slower than the same session\'s staged transfer): {len(held)} of {len(d1)} '
        f'cells in range; gaps {", ".join(f"{p}/{SPEED_LABEL[s]} {g:+.2f}" for p, s, g in d1)}')
    slower = [p for p in PATTERNS if (med(table, p, '5g', 'pgdelta_direct') or 0) > (med(table, p, '5g', 'full') or 1e9)]
    say(f'- **T8a rule** (direct slower than the full copy at 5 Gbit/s in all three workloads, on total_s): '
        f'{"fires" if len(slower) == 3 else "does not fire"} (slower in: {", ".join(slower) or "none"})')
    d2_local = [(p, s, med(table, p, s, 'pgdelta_direct_inplace')) for p in LOCAL for s in SPEEDS]
    d2_in = all(v is not None and 2.6 <= v <= 3.8 for *_, v in d2_local)
    uni = med(table, 'uniform', 'unthrottled', 'pgdelta_direct_inplace')
    uni_faster = all((med(table, 'uniform', s, 'pgdelta_direct_inplace') or 1e9) < (med(table, 'uniform', s, 'full') or 0)
                     for s in ('2g', '5g'))
    say(f'- **D2** local 2.6–3.8 s at every speed: {"held" if d2_in else "failed"} '
        f'({", ".join(f"{p}/{SPEED_LABEL[s]} {fmt(v)}" for p, s, v in d2_local)}); uniform unthrottled 3.0–4.5 s: '
        f'{"held" if uni is not None and 3.0 <= uni <= 4.5 else "failed"} ({fmt(uni)}); uniform faster than the full copy '
        f'at 5 and 2 Gbit/s: {"held" if uni_faster else "failed"}')
    ratios = {p: med(table, p, 'unthrottled', 'pgdelta_direct_inplace') / med(table, p, 'unthrottled', 'full')
              for p in LOCAL if med(table, p, 'unthrottled', 'pgdelta_direct_inplace') and med(table, p, 'unthrottled', 'full')}
    if len(ratios) == 2:
        worst = max(ratios.values())
        outcome = ('HA within reach (both ≤ 0.9)' if worst <= 0.9 else
                   'HA out on fast links (≥ 1.0)' if worst >= 1.0 else 'undecided (0.9–1.0): decided after T2 and T5')
        say(f'- **HA rule** (v1 path ÷ full copy, unthrottled, medians): {", ".join(f"{p} {r:.2f}" for p, r in ratios.items())} → {outcome}')
    for method in ('pgdelta_direct', 'pgdelta_direct_inplace'):
        cpu = [med(table, p, s, method, 'cpu_source_s') for p in PATTERNS for s in SPEEDS]
        cpu = [c for c in cpu if c is not None]
        if cpu:
            say(f'- **D3** {method} source CPU 4.5–7.5 CPU-s: {min(cpu):.1f}–{max(cpu):.1f} → '
                f'{"held" if all(4.5 <= c <= 7.5 for c in cpu) else "failed"}')
    for method in ('pgdelta_direct', 'pgdelta_direct_inplace'):
        diffs = []
        for p in PATTERNS:
            for s in SPEEDS:
                a, b = med(table, p, s, method, 'wire_s2d_bytes'), med(table, p, s, 'pgdelta', 'wire_s2d_bytes')
                if a and b:
                    diffs.append(a / b - 1)
        if diffs:
            say(f'- **D4** {method} wire bytes within 2% of v0: {min(diffs):+.1%} to {max(diffs):+.1%} → '
                f'{"held" if all(abs(d) <= 0.02 for d in diffs) else "failed"}')

    say('\n## EXP-10 (unthrottled, transfer phase, medians)')
    say('| Arm | ' + ' | '.join(PATTERNS) + ' |')
    say('|---|' + '---:|' * len(PATTERNS))
    for method, label in EXP10:
        say(f'| {label} | ' + ' | '.join(
            f'{fmt(med(table, p, "unthrottled", method, "transfer_s"))} (max {fmt((table.get((p, "unthrottled", method)) or {}).get("transfer_s", (None, None))[1])})'
            for p in PATTERNS) + ' |')

    def t(p, m):
        return med(table, p, 'unthrottled', m, 'transfer_s')
    gaps = {p: t(p, 'pgdelta_gapfill_seq') - t(p, 'pgdelta_seq') for p in PATTERNS if t(p, 'pgdelta_gapfill_seq') and t(p, 'pgdelta_seq')}
    say(f'- **(a)** gap-fill − copy-patch (both sequential), within ±0.3 s: '
        f'{", ".join(f"{p} {g:+.2f}" for p, g in gaps.items())} → '
        f'{"held" if all(abs(g) <= 0.3 for g in gaps.values()) else "failed"}; rule: a gap over 0.3 s picks the default')
    saves = {p: t(p, 'pgdelta') - min(t(p, 'pgdelta_fsync_wb'), t(p, 'pgdelta_fsync_worker')) for p in PATTERNS
             if t(p, 'pgdelta') and t(p, 'pgdelta_fsync_wb') and t(p, 'pgdelta_fsync_worker')}
    say(f'- **(b)** better of write-back and worker saves 1.0–1.6 s: {", ".join(f"{p} {s:.2f}" for p, s in saves.items())} → '
        f'{"held" if all(1.0 <= s <= 1.6 for s in saves.values()) else "failed"}; rule: under 0.5 s leaves the fsync where it is → '
        f'{"move it" if all(s >= 0.5 for s in saves.values()) else "leave it" if all(s < 0.5 for s in saves.values()) else "mixed"}')
    for p in PATTERNS:
        if t(p, 'pgdelta') and t(p, 'pgdelta_fsync_wb') and t(p, 'pgdelta_fsync_worker'):
            say(f'  - {p}: write-back saves {t(p, "pgdelta") - t(p, "pgdelta_fsync_wb"):.2f} s, worker saves {t(p, "pgdelta") - t(p, "pgdelta_fsync_worker"):.2f} s')
    seq = {p: t(p, 'pgdelta_seq') - t(p, 'pgdelta') for p in PATTERNS if t(p, 'pgdelta_seq') and t(p, 'pgdelta')}
    say(f'- **(c)** sequential adds ≥ 1.0 s: {", ".join(f"{p} {s:+.2f}" for p, s in seq.items())} → '
        f'{"held" if all(s >= 1.0 for s in seq.values()) else "failed"}; rule: keep pipelined unless it is slower by more than 0.1 s '
        f'in any workload → {"switch" if any(s < -0.1 for s in seq.values()) else "keep pipelined"}')
    inplace = {p: t(p, 'pgdelta_inplace') for p in LOCAL if t(p, 'pgdelta_inplace')}
    saving = {p: t(p, 'pgdelta') - t(p, 'pgdelta_inplace') for p in PATTERNS if t(p, 'pgdelta') and t(p, 'pgdelta_inplace')}
    say(f'- **(e)** in-place local-change transfer 2.0–2.8 s: {", ".join(f"{p} {v:.2f}" for p, v in inplace.items())} → '
        f'{"held" if all(2.0 <= v <= 2.8 for v in inplace.values()) else "failed"}; saving vs baseline '
        f'{", ".join(f"{p} {s:.2f}" for p, s in saving.items())}; rule: a saving under 1 s keeps the new-copy design → '
        f'{"in place wins" if all(s >= 1.0 for s in saving.values()) else "keep new copy" if all(s < 1.0 for s in saving.values()) else "mixed"}')
    return lines


def fig_direct(table, tag):
    fig, axes = plt.subplots(1, 3, figsize=(14.5, 4.8), sharey=True)
    width = 0.2
    seen = set()
    for ax, pattern in zip(axes, PATTERNS):
        for i, speed in enumerate(SPEEDS):
            for k, (method, label, color) in enumerate(EXP09):
                cell = table.get((pattern, speed, method))
                if not cell:
                    continue
                value, top = cell['total_s']
                x = i + (k - 1.5) * width
                ax.bar(x, value, width, color=color, edgecolor='white', label=label if method not in seen else None)
                seen.add(method)
                ax.plot([x, x], [value, top], color='black', linewidth=0.8)
                ax.text(x, top + 0.15, f'{value:.1f}', ha='center', va='bottom', fontsize=7)
        if pattern in LOCAL:
            ax.axhspan(2.6, 3.8, color=COLORS['green'], alpha=0.08, zorder=0)
            ax.text(-0.45, 2.62, 'D2 predicted 2.6–3.8 s', fontsize=7, color=COLORS['green'], ha='left', va='bottom')
        ax.set_xticks(range(len(SPEEDS)), [SPEED_LABEL[s] for s in SPEEDS])
        ax.set_title(f'{pattern} 1%')
    axes[0].set_ylabel('End-to-end time, s (median; line to maximum)')
    fig.legend(loc='lower center', ncol=4, fontsize=9, bbox_to_anchor=(0.5, -0.06))
    fig.suptitle('EXP-09: reading the source directly and patching in place, against the full copy and v0', fontsize=12)
    return plotting.save(fig, f'exp09-direct{tag}')


def fig_write_path(table, tag):
    """Unthrottled transfer phase per arm, split from the median run's own timers."""
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8), sharex=True)
    seen = set()
    parts = [('copy from B', COLORS['orange']), ('fsync (incl. waiting for the worker)', COLORS['red']),
             ('other writing, publish or commit', COLORS['purple']), ('not writing: hashing, network, waits', COLORS['sky'])]
    for ax, pattern in zip(axes, PATTERNS):
        labels = []
        for y, (method, label) in enumerate(EXP10):
            if (pattern, 'unthrottled', method) not in table:
                continue
            run = median_run(table, pattern, 'unthrottled', method)
            copy = run['copy_basis_s'] or 0.0
            fsync = (run['fsync_s'] or 0.0) if method != 'pgdelta_fsync_worker' else (run['fsync_wait_s'] or 0.0)
            rebuild = run['rebuild_s'] or 0.0
            other = max(0.0, rebuild - copy - fsync)
            rest = max(0.0, run['transfer_s'] - rebuild)
            left = 0.0
            for (name, color), value in zip(parts, (copy, fsync, other, rest)):
                ax.barh(y, value, left=left, color=color, edgecolor='white', label=name if name not in seen else None)
                seen.add(name)
                left += value
            top = table[(pattern, 'unthrottled', method)]['transfer_s'][1]
            ax.plot([left, top], [y, y], color='black', linewidth=0.8)
            ax.text(top + 0.05, y, f'{run["transfer_s"]:.2f}', va='center', fontsize=7)
            labels.append((y, label))
        ax.set_yticks([y for y, _ in labels], [l for _, l in labels] if pattern == 'uniform' else [''] * len(labels), fontsize=8)
        ax.invert_yaxis()
        ax.set_title(f'{pattern} 1%')
        ax.set_xlabel('Transfer phase, s (median run; line to maximum)')
    fig.legend(loc='lower center', ncol=4, fontsize=9, bbox_to_anchor=(0.5, -0.07))
    fig.suptitle('EXP-10: the destination write path, unthrottled link (worker arm: only the time waited for fsync)', fontsize=12)
    return plotting.save(fig, f'exp10-write-path{tag}')


def load_extra(sweep: Path):
    """load() plus the columns EXP-10's later sweeps need: rsync's prep, the combine, shared bytes."""
    rows = load(sweep)
    extra = {}
    with (sweep / 'summary.csv').open() as f:
        for row in csv.DictReader(f):
            extra[(row['trial_id'], row['repeat'], row['speed'], row['method'])] = {
                k: number(row.get(k)) for k in ('prep_s', 'combine_s', 'output_shared_bytes')}
    for run in rows:
        run.update(extra.get((run['trial_id'], run['repeat'], run['speed'], run['method']), {}))
    return rows


def unthrottled(rows):
    """pattern -> method -> passing, verified runs at the unthrottled link."""
    out = {}
    for run in rows:
        if run['speed'] == 'unthrottled' and run['status'] == 'pass' and run['verified']:
            out.setdefault(run['pattern'], {}).setdefault(run['method'], []).append(run)
    return out


def m(runs, col):
    values = [r[col] for r in runs if r.get(col) is not None]
    return statistics.median(values) if values else None


def exp10_later(before, xfs, after):
    """EXP-10 arm (d) on XFS against the ext4 sweeps before and after it, and the write-back rerun."""
    b, x, a = unthrottled(before), unthrottled(xfs), unthrottled(after)
    lines = ['## EXP-10 (d): XFS against ext4 before and after (unthrottled, medians)',
             '| Workload | full: ext4 before / XFS / ext4 after | pgdelta transfer: ext4 before / XFS / ext4 after | '
             'pgdelta write phase on XFS | in place transfer on XFS | rsync prep on XFS | combine (copy-file-range) on XFS |',
             '|---|---|---|---:|---:|---:|---:|']
    adopt = {}
    for p in PATTERNS:
        def g(d, meth, col):
            return m(d.get(p, {}).get(meth, []), col)
        fb, fx, fa = g(b, 'full', 'total_s'), g(x, 'full', 'total_s'), g(a, 'full', 'total_s')
        tb, tx, ta = g(b, 'pgdelta', 'transfer_s'), g(x, 'pgdelta', 'transfer_s'), g(a, 'pgdelta', 'transfer_s')
        write = g(x, 'pgdelta', 'rebuild_s')
        lines.append(f'| {p} | {fb:.2f} / {fx:.2f} / {fa:.2f} | {tb:.2f} / {tx:.2f} / {ta:.2f} | {write:.2f} | '
                     f'{g(x, "pgdelta_inplace", "transfer_s"):.2f} | {g(x, "rsync_8k", "prep_s"):.2f} | '
                     f'{g(x, "incremental_cfr", "combine_s"):.2f} |')
        adopt[p] = write <= 1.5 and (tb - tx) >= 0.5 and (ta - tx) >= 0.5
    local_ok = all(m(x[p]['pgdelta'], 'rebuild_s') <= 0.6 for p in LOCAL)
    uni = m(x['uniform']['pgdelta'], 'rebuild_s')
    lines.append(f'- **(d)** write phase ≤ 0.6 s for local changes: {"held" if local_ok else "failed"}; about 1 s (0.7–1.3) '
                 f'for uniform: {"held" if 0.7 <= uni <= 1.3 else "failed"} ({uni:.2f} s)')
    lines.append('- **(d) rule** (write phase ≤ 1.5 s and transfer ≥ 0.5 s faster than ext4 in the sweeps before and after): ' +
                 ', '.join(f'{p} {"adopt reflinks" if ok else "no"}' for p, ok in adopt.items()))
    prep = [m(x[p]['rsync_8k'], 'prep_s') for p in PATTERNS]
    comb = [m(x[p]['incremental_cfr'], 'combine_s') for p in PATTERNS]
    lines.append(f'- rsync prep on XFS < 0.5 s: {"held" if all(v < 0.5 for v in prep) else "failed"} ({min(prep):.2f}–{max(prep):.2f} s)')
    lines.append(f'- combine with --copy-file-range on XFS < 2.5 s: {"held" if all(v < 2.5 for v in comb) else "failed"} '
                 f'({min(comb):.2f}–{max(comb):.2f} s)')
    if all('pgdelta_fsync_wb' in a.get(p, {}) for p in PATTERNS):
        lines.append('\n## EXP-10 (b) rerun (final ext4 sweep, fixed early write-back)')
        saves = {}
        for p in PATTERNS:
            base = m(a[p]['pgdelta'], 'transfer_s')
            wb, wk = m(a[p]['pgdelta_fsync_wb'], 'transfer_s'), m(a[p]['pgdelta_fsync_worker'], 'transfer_s')
            saves[p] = (base - wb, base - wk)
            lines.append(f'- {p}: baseline {base:.2f} s, write-back {wb:.2f} s (saves {base - wb:.2f}), '
                         f'fsync thread {wk:.2f} s (saves {base - wk:.2f})')
        best = {p: max(v) for p, v in saves.items()}
        lines.append(f'- **(b)** better alternative saves 1.0–1.6 s: {"held" if all(1.0 <= v <= 1.6 for v in best.values()) else "failed"}; '
                     f'rule (≥ 0.5 s moves the fsync): {", ".join(f"{p} {v:.2f}" for p, v in best.items())}')
    return lines


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sweep', default=str(ROOT / 'results/exp04/exp09-10-ext4'))
    parser.add_argument('--xfs', default=str(ROOT / 'results/exp04/exp10-xfs'))
    parser.add_argument('--ext4-after', default=str(ROOT / 'results/exp04/exp10-ext4b'))
    parser.add_argument('--tag', default='', help='suffix for data and figure names (e.g. -smoke)')
    args = parser.parse_args()
    plotting.style()
    if Path(args.xfs, 'summary.csv').exists() and Path(args.ext4_after, 'summary.csv').exists():
        print('\n'.join(exp10_later(load_extra(Path(args.sweep)), load_extra(Path(args.xfs)),
                                     load_extra(Path(args.ext4_after)))))
    rows = load(Path(args.sweep))
    table = cells(rows)
    failed = [r for r in rows if r['status'] != 'pass' or not r['verified']]
    print(f'{len(rows)} runs, {len(failed)} not passing or not verified')
    write_data(rows, table, args.tag)
    print('\n'.join(verdicts(table)))
    print(fig_direct(table, args.tag))
    print(fig_write_path(table, args.tag))


if __name__ == '__main__':
    main()
