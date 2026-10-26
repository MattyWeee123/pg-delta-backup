"""EXP-48's analysis: does a two-term disk model predict v1 and a full copy beyond memory at a size it never saw?
(docs/experiments/EXP-48-disk-term.md)

    .venv/bin/python analysis/exp48.py [--sweep results/exp04/sets/ha/exp48-s600]

The model, fixed from EXP-31's second session (results/exp04/sets/ha/exp31b-s400; fitted, so not evidence there):
- v1 (in place, --verify-scope written): reads B once to hash it, then writes the pages it received, scattered
  writes bound by the disk's operations rather than its throughput:
      T_v1 = |B| / R_read(dest) + P_written / IOPS_EFF      (IOPS_EFF = 6,750 pages/s)
- one pg_basebackup stream: a sequential write of S at the disk's rate, plus a fixed overhead:
      T_full = |S| / R_write(dest) + FULL_OVERHEAD_S         (4.5 s)
R_read and R_write are the session's own 1 GiB disk test on dest (run.json disk_test); |B| = |S| = pages x 8 KiB and
P_written are pgdelta's own counts in each run's statistics. Prints each cell's prediction, measurement and error,
and scores the registered predictions. Writes docs/experiments/data/exp48-model.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IOPS_EFF = 6750.0
FULL_OVERHEAD_S = 4.5
PAGE = 8192
V1, FULL = 'pgdelta_direct_inplace_vw', 'full'


def session(sweep):
    sweep = Path(sweep)
    run = json.loads((sweep / 'run.json').read_text())
    dest = run['disk_test']['dest']
    r_read, r_write = dest['read']['mb_per_second'], dest['write']['mb_per_second']
    times = {}
    for r in csv.DictReader(open(sweep / 'summary.csv')):
        if r['status'] == 'pass' and r['speed'] == 'unthrottled':
            times.setdefault((r['pattern'], r['method']), []).append(float(r['total_s']))
    counts = {}
    for log in sweep.glob('logs/*/source/*pgdelta_direct_inplace_vw-stats.json.log'):
        text = log.read_text()
        stats = json.loads(text[text.find('{'):])
        pattern = 'uniform' if 'uniform' in log.parent.parent.name else 'hot'
        pages = stats.get('pages') or {}
        counts.setdefault(pattern, []).append((pages.get('total', 0) * PAGE / 1e6, pages.get('sent', 0)))
    rows = []
    for pattern in ('hot', 'uniform'):
        mb = statistics.median(c[0] for c in counts[pattern])
        sent = statistics.median(c[1] for c in counts[pattern])
        v1_model = mb / r_read + sent / IOPS_EFF
        full_model = mb / r_write + FULL_OVERHEAD_S
        v1, full = statistics.median(times[(pattern, V1)]), statistics.median(times[(pattern, FULL)])
        rows.append({'pattern': pattern, 'database_mb': round(mb), 'pages_written': int(sent), 'r_read_mb_s': r_read,
                     'r_write_mb_s': r_write, 'v1_model_s': round(v1_model, 1), 'v1_s': round(v1, 1),
                     'v1_error': round(v1 / v1_model - 1, 3), 'full_model_s': round(full_model, 1), 'full_s': round(full, 1),
                     'full_error': round(full / full_model - 1, 3), 'v1_over_full': round(v1 / full, 3)})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--sweep', default=str(ROOT / 'results' / 'exp04' / 'sets' / 'ha' / 'exp48-s600'))
    ap.add_argument('--fit', default=str(ROOT / 'results' / 'exp04' / 'sets' / 'ha' / 'exp31b-s400'))
    a = ap.parse_args(argv)
    print('in sample (EXP-31b, where the constants came from; not evidence):')
    for r in session(a.fit):
        print('  ', r)
    rows = session(a.sweep)
    print('out of sample:')
    for r in rows:
        print('  ', r)
    errors = [abs(r[k]) for r in rows for k in ('v1_error', 'full_error')]
    print(f"1 (deciding). {'HELD' if all(e <= 0.15 for e in errors) else 'FAILED'}: every cell within 15% of the model: "
          f"{[(r['pattern'], r['v1_error'], r['full_error']) for r in rows]}")
    hot, uniform = rows
    print(f"2. {'HELD' if abs(hot['v1_over_full'] - 1) <= 0.10 else 'FAILED'}: hot, v1 ties the full copy within 10%: "
          f"{hot['v1_over_full']}")
    print(f"3. {'HELD' if uniform['v1_over_full'] >= 1.8 else 'FAILED'}: uniform, v1 at least 1.8x the full copy: "
          f"{uniform['v1_over_full']}")
    with open(ROOT / 'docs' / 'experiments' / 'data' / 'exp48-model.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    figure(session(a.fit), rows)


def figure(fit_rows, test_rows):
    """Predicted against measured, both sessions, with the registered 15% band (drawn after the verdict)."""
    import plotting
    from plotting import COLORS, plt
    plotting.style()
    fig, ax = plt.subplots(figsize=(6.4, 5.2))
    top = 200
    ax.plot([0, top], [0, top], color=COLORS['black'], lw=0.8)
    ax.fill_between([0, top], [0, top * 0.85], [0, top * 1.15], color=COLORS['grey'], alpha=0.15, lw=0,
                    label='within 15% (the registered bar)')
    for rows, marker, name in ((fit_rows, 'o', '12 GB (fitted)'), (test_rows, 's', '18 GB (predicted beforehand)')):
        for r in rows:
            for arm, color in (('v1', COLORS['blue']), ('full', COLORS['grey'])):
                ax.scatter(r[f'{arm}_model_s'], r[f'{arm}_s'], marker=marker, s=60, color=color,
                           facecolor='none' if marker == 'o' else color, zorder=3)
                if arm == 'v1':
                    ax.annotate(f"v1, {r['pattern']}", (r['v1_model_s'], r['v1_s']), textcoords='offset points',
                                xytext=(-56, 6), fontsize=7.5)
        # The two full copies of a size take the same time (hot or uniform): one label.
        ax.annotate('pg_basebackup', (rows[0]['full_model_s'], rows[0]['full_s']), textcoords='offset points',
                    xytext=(8, -14), fontsize=7.5)
        ax.scatter([], [], marker=marker, color=COLORS['black'], facecolor='none' if marker == 'o' else COLORS['black'], label=name)
    ax.set_xlim(40, top)
    ax.set_ylim(40, top)
    ax.set_xlabel('model (s): |B| / read rate + pages written / 6,750 per s')
    ax.set_ylabel('measured (s), median')
    ax.set_title('Beyond memory on a 150 GiB balanced disk: model against measured', fontsize=10)
    ax.legend(fontsize=8, loc='upper left')
    fig.tight_layout()
    print(plotting.save(fig, 'exp48-disk-model'))


if __name__ == '__main__':
    main()
