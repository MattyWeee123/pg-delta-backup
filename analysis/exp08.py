"""EXP-08 part B (pgBackRest delta restore over the network) and part A (correctness), as pre-registered.

    .venv/bin/python analysis/exp08.py [--sweep results/exp04/exp08-pgbackrest] [--t11 results/t11] [--tag ...]

Part B reads the harness's summary.csv from `remote.py fetch`; part A reads run_t11.py's
summary.json for each WAL arm (results/t11/default, results/t11/retained), if present. Writes
docs/experiments/data/exp08-runs.csv, exp08-cells.csv and exp08-t11.csv, the figures
docs/figures/exp08-time.svg and exp08-bytes.svg, and prints every prediction and rule of
docs/experiments/EXP-08-incumbent-tools.md with its verdict. Statistics are per-workload
medians of the repeats; `pgbackrest_zst` (Omni's configuration) is the arm for I7, I8 and the rules.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'docs' / 'experiments' / 'data'
PATTERNS = ['uniform', 'hot', 'append']
LOCAL = ['hot', 'append']
SPEEDS = ['0.5g', '2g', 'unthrottled']
SPEED_LABEL = {'0.5g': '0.5 Gbit/s', '2g': '2 Gbit/s', 'unthrottled': 'unthrottled'}
METHODS = [('full', 'Full copy (pg_basebackup)', COLORS['grey'], None),
           ('pgdelta', 'pgdelta v0 (local backup first)', COLORS['blue'], None),
           ('pgdelta_direct_inplace', 'pgdelta v1 path (direct, in place)', COLORS['green'], None),
           ('pgbackrest_zst', 'pgBackRest, zstd', COLORS['orange'], None),
           ('pgbackrest_none', 'pgBackRest, no compression', COLORS['red'], '//'),
           ('pgbackrest_block', 'pgBackRest, zstd + block incremental', COLORS['purple'], '..')]
NUMERIC = ('total_s', 'restore_s', 'transfer_s', 'capture_s', 'cpu_source_s', 'cpu_dest_s', 'wire_s2d_bytes',
           'wire_d2s_bytes', 'recovery_wire_s2d_bytes', 'recovery_wal_files', 'output_bytes', 'measured_s2d_gbit')


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load(sweep: Path):
    rows = []
    with (sweep / 'summary.csv').open() as f:
        for row in csv.DictReader(f):
            run = {k: row[k] for k in ('trial_id', 'pattern', 'speed', 'method', 'repeat', 'status')}
            run.update({k: number(row.get(k)) for k in NUMERIC})
            run['verified'] = row.get('verified') == 'True'
            run['logical_match'] = row.get('logical_match')
            rows.append(run)
    return rows


def ok(run):
    """Passed and verified; a pgBackRest restore must also have matched the source logically."""
    if run['status'] != 'pass' or not run['verified']:
        return False
    return run['logical_match'] == 'True' if run['method'].startswith('pgbackrest') else True


def cells(rows):
    grouped = {}
    for run in rows:
        if ok(run):
            grouped.setdefault((run['pattern'], run['speed'], run['method']), []).append(run)
    table = {}
    for key, runs in grouped.items():
        cell = {'n': len(runs)}
        for col in NUMERIC:
            values = [r[col] for r in runs if r[col] is not None]
            if values:
                cell[col] = (statistics.median(values), max(values))
        table[key] = cell
    return table


def med(table, pattern, speed, method, col='total_s'):
    cell = table.get((pattern, speed, method))
    return None if not cell or col not in cell else cell[col][0]


def crossing(table, pattern, a, b):
    """Measured link speed (Gbit/s) where a/b crosses 1, interpolated in log-log between visited speeds."""
    points = []
    for speed in SPEEDS:
        ta, tb = med(table, pattern, speed, a), med(table, pattern, speed, b)
        gbit = med(table, pattern, speed, 'full', 'measured_s2d_gbit') or med(table, pattern, speed, a, 'measured_s2d_gbit')
        if ta and tb and gbit:
            points.append((gbit, ta / tb))
    for (x0, r0), (x1, r1) in zip(points, points[1:]):
        if (r0 - 1) * (r1 - 1) < 0:
            f = math.log(r0) / (math.log(r0) - math.log(r1))
            return math.exp(math.log(x0) + f * (math.log(x1) - math.log(x0)))
    return None


def write_data(rows, table, tag):
    DATA.mkdir(parents=True, exist_ok=True)
    keys = ['trial_id', 'pattern', 'speed', 'method', 'repeat', 'status', 'verified', 'logical_match', *NUMERIC]
    with (DATA / f'exp08{tag}-runs.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for run in sorted(rows, key=lambda r: (r['pattern'], r['speed'], r['method'], r['repeat'])):
            writer.writerow({k: run.get(k) for k in keys})
    cols = ['total_s', 'wire_s2d_bytes', 'recovery_wire_s2d_bytes', 'cpu_source_s', 'cpu_dest_s']
    with (DATA / f'exp08{tag}-cells.csv').open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['pattern', 'speed', 'method', 'n', *(f'{c}_{s}' for c in cols for s in ('median', 'max'))])
        for (pattern, speed, method), cell in sorted(table.items()):
            writer.writerow([pattern, speed, method, cell['n'],
                             *(round(cell[c][i], 4) if c in cell else '' for c in cols for i in (0, 1))])


def part_b(table):
    lines = []
    say = lines.append

    def fmt(v, unit=''):
        return '—' if v is None else f'{v:.2f}{unit}'

    def mb(v):
        return '—' if v is None else f'{v / 1e6:,.1f}'

    say('## Part B: time (s, median)')
    say('| Workload | Link | ' + ' | '.join(label for _, label, *_ in METHODS) + ' |')
    say('|---|---|' + '---:|' * len(METHODS))
    for p in PATTERNS:
        for s in SPEEDS:
            say(f'| {p} | {SPEED_LABEL[s]} | ' + ' | '.join(fmt(med(table, p, s, m)) for m, *_ in METHODS) + ' |')
    say('\n## Part B: bytes source→dest, MB (median, recovery WAL included for pgBackRest)')
    say('| Workload | ' + ' | '.join(label for _, label, *_ in METHODS) + ' |')
    say('|---|' + '---:|' * len(METHODS))
    for p in PATTERNS:
        say(f'| {p} | ' + ' | '.join(mb(med(table, p, 'unthrottled', m, 'wire_s2d_bytes')) for m, *_ in METHODS) + ' |')

    zst, v0, v1 = 'pgbackrest_zst', 'pgdelta', 'pgdelta_direct_inplace'
    i7 = [(p, s, med(table, p, s, zst), med(table, p, s, v0)) for p in LOCAL for s in SPEEDS]
    say(f'- **I7** (pgbackrest_zst beats staged pgdelta on time for local changes): '
        f'{"held" if all(a is not None and b is not None and a < b for *_, a, b in i7) else "failed"} '
        f'({", ".join(f"{p}/{SPEED_LABEL[s]} {fmt(a)} vs {fmt(b)}" for p, s, a, b in i7)})')
    bz, bd = med(table, 'uniform', 'unthrottled', zst, 'wire_s2d_bytes'), med(table, 'uniform', 'unthrottled', v0, 'wire_s2d_bytes')
    bn = med(table, 'uniform', 'unthrottled', 'pgbackrest_none', 'wire_s2d_bytes')
    if bz and bd:
        say(f'- **I8** (registered, judged on pgbackrest_zst: pgBackRest sends more bytes than pgdelta for uniform): '
            f'{"held" if bz > bd else "failed — the rule fires: on bytes the pitch must be \"no repository needed\""} '
            f'({mb(bz)} vs {mb(bd)} MB)')
    if bn and bd:
        say(f'- **I8a** (pgbackrest_none sends more than pgdelta for uniform, about 1.05 GB): '
            f'{"held" if bn > bd else "failed"} ({mb(bn)} MB, recovery WAL included)')
    if bz and bd:
        say(f'- **I8b** (pgbackrest_zst sends fewer bytes than pgdelta for uniform): {"held" if bz < bd else "failed"}')
    i9 = [(p, med(table, p, 'unthrottled', 'pgbackrest_block', 'wire_s2d_bytes'), med(table, p, 'unthrottled', zst, 'wire_s2d_bytes'))
          for p in LOCAL]
    say(f'- **I9** (pgbackrest_block ≤ half of pgbackrest_zst\'s bytes for local changes): '
        f'{"held" if all(b and z and b <= z / 2 for _, b, z in i9) else "failed"} '
        f'({", ".join(f"{p} {mb(b)} vs {mb(z)} MB" for p, b, z in i9)})')
    a, b = med(table, 'uniform', '0.5g', zst), med(table, 'uniform', '0.5g', v1)
    say(f'- **I10** (uniform: pgbackrest_zst faster than the v1 path at 0.5 Gbit/s): '
        f'{"held" if a and b and a < b else "failed"} ({fmt(a)} vs {fmt(b)})')
    i10b = [(p, s, med(table, p, s, v1), med(table, p, s, zst)) for p in LOCAL for s in ('0.5g', '2g')]
    say(f'- **I10b** (local: v1 path faster than pgbackrest_zst at 0.5 and 2 Gbit/s): '
        f'{"held" if all(x and y and x < y for *_, x, y in i10b) else "failed"} '
        f'({", ".join(f"{p}/{SPEED_LABEL[s]} {fmt(x)} vs {fmt(y)}" for p, s, x, y in i10b)})')
    i11 = [(p, s, med(table, p, s, zst), med(table, p, s, 'full')) for p in LOCAL for s in SPEEDS]
    say(f'- **I11** (pgbackrest_zst beats the full copy at every speed for local changes): '
        f'{"held" if all(x and y and x < y for *_, x, y in i11) else "failed"} '
        f'({", ".join(f"{p}/{SPEED_LABEL[s]} {fmt(x)} vs {fmt(y)}" for p, s, x, y in i11)})')
    pairs = [(p, s, med(table, p, s, zst), med(table, p, s, v1)) for p in PATTERNS for s in SPEEDS]
    pairs = [(p, s, x, y) for p, s, x, y in pairs if x and y]
    no_edge = all(x <= 1.1 * y for *_, x, y in pairs) and all(
        (med(table, p, 'unthrottled', zst, 'wire_s2d_bytes') or 0) <= (med(table, p, 'unthrottled', v1, 'wire_s2d_bytes') or 0)
        for p in PATTERNS)
    clear_edge = all(y <= 0.8 * x for *_, x, y in pairs)
    say('- **Rules** (v1 path against pgbackrest_zst, every speed and workload): ' +
        ('no edge where a repository exists → pitch "no repository needed"' if no_edge else
         'clear edge → worth using even where a repository exists' if clear_edge else 'mixed'))
    say('  - ratios v1 ÷ zst: ' + ', '.join(f'{p}/{SPEED_LABEL[s]} {y / x:.2f}' for p, s, x, y in pairs))
    if not no_edge and not clear_edge:
        for p in PATTERNS:
            x = crossing(table, p, v1, zst)
            say(f'  - {p}: the two cross at {"%.2f Gbit/s" % x if x else "no visited crossing"}')
    return lines


def part_a(t11: Path):
    lines, rows = [], []
    for arm in ('default', 'retained'):
        path = t11 / arm / 'summary.json'
        if not path.exists():
            lines.append(f'(part A, arm {arm}: no {path})')
            continue
        summary = json.loads(path.read_text())
        for result in summary.get('runs') or summary.get('results', []):
            rows.append({'arm': arm, **{k: result.get(k) for k in (
                'scenario', 'tool', 'outcome', 'refusal_class', 'verdict', 'logical_match', 'amcheck_ok',
                'diverged_pages_left', 'diverged_before_recovery', 'seconds', 'outcome_reason')}})
    if rows:
        lines.append('## Part A')
        lines.append('| Arm | Scenario | Tool | Outcome | Refusal class | Verdict | Logical | amcheck | Stale pages left |')
        lines.append('|---|---|---|---|---|---|---|---|---:|')
        for r in sorted(rows, key=lambda r: (r['arm'], r['scenario'] or '', r['tool'] or '')):
            lines.append(f"| {r['arm']} | {r['scenario']} | {r['tool']} | {r['outcome']} | {r['refusal_class'] or ''} | "
                         f"{r['verdict']} | {r['logical_match']} | {r['amcheck_ok']} | {r['diverged_pages_left']} |")
    return lines, rows


def fig_time(table, tag):
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8), sharey=True)
    width = 0.13
    seen = set()
    for ax, pattern in zip(axes, PATTERNS):
        for i, speed in enumerate(SPEEDS):
            for k, (method, label, color, hatch) in enumerate(METHODS):
                cell = table.get((pattern, speed, method))
                if not cell:
                    continue
                value, top = cell['total_s']
                x = i + (k - 2.5) * width
                ax.bar(x, value, width, color=color, hatch=hatch, edgecolor='white', label=label if method not in seen else None)
                seen.add(method)
                ax.plot([x, x], [value, top], color='black', linewidth=0.8)
        ax.set_xticks(range(len(SPEEDS)), [SPEED_LABEL[s] for s in SPEEDS])
        ax.set_yscale('log')
        from exp04 import plain_log
        plain_log(ax)
        ax.set_title(f'{pattern} 1%')
    axes[0].set_ylabel('End-to-end time, s (median; line to maximum; log scale)')
    fig.legend(loc='lower center', ncol=3, fontsize=9, bbox_to_anchor=(0.5, -0.1))
    fig.suptitle('EXP-08: pgBackRest delta restore (backup already in the repository) against pgdelta and the full copy',
                 fontsize=12)
    return plotting.save(fig, f'exp08-time{tag}')


def fig_bytes(table, tag):
    fig, ax = plt.subplots(figsize=(11, 4.4))
    width = 0.13
    for i, pattern in enumerate(PATTERNS):
        for k, (method, label, color, hatch) in enumerate(METHODS):
            value = med(table, pattern, 'unthrottled', method, 'wire_s2d_bytes')
            if value is None:
                continue
            x = i + (k - 2.5) * width
            ax.bar(x, value / 1e6, width, color=color, hatch=hatch, edgecolor='white', label=label if i == 0 else None)
            ax.text(x, value / 1e6 * 1.08, f'{value / 1e6:,.0f}', ha='center', va='bottom', fontsize=7)
    ax.set_xticks(range(len(PATTERNS)), [f'{p} 1%' for p in PATTERNS])
    ax.set_yscale('log')
    from exp04 import plain_log
    plain_log(ax)
    ax.set_ylabel('MB sent source → destination (median)')
    ax.legend(fontsize=8, ncol=2)
    ax.set_title('EXP-08: bytes on the wire, recovery WAL included for pgBackRest')
    return plotting.save(fig, f'exp08-bytes{tag}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sweep', default=str(ROOT / 'results/exp04/exp08-pgbackrest'))
    parser.add_argument('--t11', default=str(ROOT / 'results/t11'))
    parser.add_argument('--tag', default='')
    args = parser.parse_args()
    plotting.style()
    if Path(args.sweep, 'summary.csv').exists():
        rows = load(Path(args.sweep))
        table = cells(rows)
        bad = [r for r in rows if not ok(r)]
        print(f'{len(rows)} runs, {len(bad)} not passing, not verified or not matching logically')
        for r in bad:
            print('  ', r['pattern'], r['speed'], r['method'], r['repeat'], r['status'], r['logical_match'])
        write_data(rows, table, args.tag)
        print('\n'.join(part_b(table)))
        print(fig_time(table, args.tag))
        print(fig_bytes(table, args.tag))
    lines, t11_rows = part_a(Path(args.t11))
    print('\n'.join(lines))
    if t11_rows:
        with (DATA / f'exp08{args.tag}-t11.csv').open('w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(t11_rows[0]))
            writer.writeheader()
            writer.writerows(t11_rows)


if __name__ == '__main__':
    main()
