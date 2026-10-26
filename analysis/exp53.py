"""EXP-53's analysis: where is zstd's break-even now? (docs/experiments/EXP-53-zstd-break-even.md)

    .venv/bin/python analysis/exp53.py [--sweep results/exp04/exp53] [--sweep results/exp04/exp53b]

Uniform 1% at 3 and 5 Gbit/s and 60 ms, every session's runs together: the median time of each method and zstd / v1
per rate, beside EXP-52's 2 Gbit/s (both sessions). Scores:
1 (deciding) zstd <= 1.00 of v1 at 3 Gbit/s and >= 1.00 at 5: the break-even lies between them;
2 the emulator's steady one-stream link test reaches 90% of each rate in every trial;
3 BBR on every v1 run, and batched tasks (under a tenth as many as runs, at both ends) on every zstd run;
4 v1 and v1 with zstd both faster than one pg_basebackup at 3 and 5 Gbit/s.
The break-even is interpolated linearly in the link rate between the measured rates where zstd / v1 crosses 1.
Writes docs/experiments/data/exp53-cells.csv and docs/figures/exp53-break-even.svg.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

import plotting
from exp47 import FULL, V1, V1Z, medians
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
RATES = {'3:60:tuned': 3.0, '4:60:tuned': 4.0, '5:60:tuned': 5.0}  # 4: the third session (the rule 8 revision)
EXP52 = [str(ROOT / 'results' / 'exp04' / s) for s in ('exp52', 'exp52b')]
EARLIER = [(0.5, 0.52, 'EXP-46'), (1.0, 0.753, 'EXP-47')]  # one run per task, uniform 1% at 60 ms


def cells(sweeps, conditions):
    t, mb, n = medians(sweeps)
    rows = []
    for condition, rate in conditions.items():
        if (condition, V1) not in t:
            continue
        row = {'condition': condition, 'gbit': rate,
               **{f'{name}_s': round(t[(condition, m)], 3) for m, name in ((FULL, 'full'), (V1, 'v1'), (V1Z, 'v1_zstd'))},
               **{f'{name}_runs': n.get((condition, m), 0) for m, name in ((FULL, 'full'), (V1, 'v1'), (V1Z, 'v1_zstd'))}}
        row['zstd_over_v1'] = round(row['v1_zstd_s'] / row['v1_s'], 3)
        rows.append(row)
    return rows


def break_even(points):
    """The link rate where zstd / v1 crosses 1, linear between the measured rates on either side; None if it doesn't."""
    points = sorted(points)
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if y0 <= 1 <= y1 and y1 != y0:
            return x0 + (1 - y0) * (x1 - x0) / (y1 - y0)
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--sweep', action='append')
    a = ap.parse_args(argv)
    sweeps = a.sweep or [str(ROOT / 'results' / 'exp04' / s) for s in ('exp53', 'exp53b', 'exp53c')]
    sweeps = [s for s in sweeps if (Path(s) / 'summary.csv').exists()]
    rows = cells(sweeps, RATES)
    two = cells(EXP52, {'2:60:tuned': 2.0})
    for r in two + rows:
        print(r)
    by = {r['condition']: r for r in rows}
    r3, r5 = by['3:60:tuned']['zstd_over_v1'], by['5:60:tuned']['zstd_over_v1']
    print(f"1 (deciding). {'HELD' if r3 <= 1.0 <= r5 else 'FAILED'}: zstd / v1 at 3 Gbit/s {r3}, at 5 {r5}")
    for sweep in sweeps:
        each = cells([sweep], RATES)
        print('   session', Path(sweep).name, {r['condition']: r['zstd_over_v1'] for r in each})
    links, effective, tasks = [], [], []
    for sweep in sweeps:
        run = json.loads((Path(sweep) / 'run.json').read_text())
        for link in run.get('links') or []:
            if link['speed'] in RATES:
                links.append((link['speed'], link['source_to_dest_steady_gbit']))
        for log in sorted(Path(sweep).glob('logs/*/source/*-stats.json.log')):
            text = log.read_text()
            stats = json.loads(text[text.find('{'):])
            if log.name.endswith('_bbr_z-stats.json.log'):
                c, received = stats.get('compress') or {}, (stats.get('receiver') or {}).get('compress') or {}
                tasks.append((c.get('runs'), c.get('tasks'), received.get('tasks')))
            else:
                effective.append((stats.get('congestion_control') or {}).get('effective'))
    reach = [(s, g, round(g / RATES[s], 3)) for s, g in links]
    print(f"2. {'HELD' if reach and all(f >= 0.9 for _, _, f in reach) else 'FAILED'}: steady one-stream link "
          f"tests (condition, Gbit/s, of the rate): {reach}")
    batched = bool(tasks) and all(runs and s and rt and s < runs / 10 and rt < runs / 10 for runs, s, rt in tasks)
    print(f"3. {'HELD' if batched and effective and all(e == 'bbr' for e in effective) else 'FAILED'}: BBR on "
          f"{sum(e == 'bbr' for e in effective)} of {len(effective)} v1 runs; tasks {sorted(set(tasks))}")
    faster = all(r['v1_s'] < r['full_s'] and r['v1_zstd_s'] < r['full_s'] for r in rows)
    print(f"4. {'HELD' if faster else 'FAILED'}: against pg_basebackup "
          f"{[(r['condition'], round(r['v1_s'] / r['full_s'], 2), round(r['v1_zstd_s'] / r['full_s'], 2)) for r in rows]}")
    points = [(r['gbit'], r['zstd_over_v1']) for r in two + rows]
    crossing = break_even(points)
    print(f"break-even (interpolated): {crossing:.2f} Gbit/s" if crossing else 'break-even: not between the measured rates')
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp53-cells.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(two + rows)
    sessions = [cells([s], {'2:60:tuned': 2.0}) for s in EXP52] + [cells([s], RATES) for s in sweeps]
    figure(two + rows, crossing, sessions)


def figure(rows, crossing, sessions):
    """Left: each session's medians against the link rate, joined within a session (VM pairs differ by up to
    0.6 s, so times compare within a session). Right: zstd / v1, with the earlier one-run-per-task points."""
    plotting.style()
    fig, (left, right) = plt.subplots(1, 2, figsize=(10, 4.2))
    xs = [r['gbit'] for r in rows]
    for key, name, color in (('full_s', 'pg_basebackup', COLORS['grey']), ('v1_s', 'v1', COLORS['blue']),
                             ('v1_zstd_s', 'v1 + zstd (batched tasks)', COLORS['red'])):
        for i, session in enumerate(sessions):
            left.plot([r['gbit'] for r in session], [r[key] for r in session], marker='o', ms=4, lw=1,
                      color=color, label=name if i == 0 else None)
    left.plot([], [], color=COLORS['black'], lw=1, label='a line joins one session’s rates')
    left.set_xlabel('link rate (Gbit/s), 60 ms round trip')
    left.set_ylabel('median seconds per session, uniform 1%')
    left.set_ylim(0, None)
    left.legend(fontsize=7.5)
    left.set_title('Time against the link rate, by session', fontsize=10)
    right.axhline(1.0, color=COLORS['black'], lw=0.8)
    right.plot([x for x, _, _ in EARLIER], [y for _, y, _ in EARLIER], '--', marker='o', color=COLORS['sky'],
               label='one run per pool task (EXP-46, -47)')
    right.plot(xs, [r['zstd_over_v1'] for r in rows], marker='s', color=COLORS['red'],
               label='batched pool tasks (EXP-52, -53)')
    for r in rows:
        right.annotate(f"{r['zstd_over_v1']:.2f}", (r['gbit'], r['zstd_over_v1']), textcoords='offset points',
                       xytext=(6, -12), fontsize=8)
    if crossing:
        right.axvline(crossing, color=COLORS['red'], lw=0.8, ls=':')
        right.annotate(f'break-even ≈ {crossing:.1f} Gbit/s\n(interpolated)', (crossing, 1.0), textcoords='offset points',
                       xytext=(-8, 18), ha='right', fontsize=8, color=COLORS['red'])
    right.set_xlabel('link rate (Gbit/s), 60 ms round trip')
    right.set_ylabel('v1 + zstd time ÷ v1 time')
    right.legend(fontsize=8, loc='lower right')
    right.set_title('Where zstd pays', fontsize=10)
    fig.tight_layout()
    print(plotting.save(fig, 'exp53-break-even'))


if __name__ == '__main__':
    main()
