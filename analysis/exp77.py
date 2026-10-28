"""EXP-77's analysis: does re-reading the fast path's written pages on the receiver's pipelined verifier, instead of
in its main loop, remove the stall at every file's end (docs/experiments/EXP-77-reread-off-the-loop.md)?

    .venv/bin/python analysis/exp77.py [--results results/exp04/exp77]

Cells: per pattern and arm, the median of the runs (exp68.runs). The pairs: uniform fast8wf2q (the check in the main
loop, --verify sequential) against fast8wf2 (on the verifier), scatterall12 fast8wq against fast8w; scatterall12's full
(pg_basebackup) beside them. The timeline helpers are EXP-76's (exp76.series, exp76.phases).
Scores 1 (deciding: in scatterall12, the verifier's median at least 3.0 s below the main loop's), 2 (in uniform, the
verifier's median at most 0.3 s above the main loop's), 3 (in scatterall12 with the verifier, the source disk under
50 MB/s for at most 1.5 s of the sending in every run), 4 (in every fast run, every page sent re-read, and the
re-reading's time reported) and 5 (every run exits 0; every fast run used the fast path and passed the copy's check).
Writes docs/experiments/data/exp77-cells.csv and the figure exp77-timeline.
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import exp68
import exp76
import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
PAIRS = {'uniform': ('fast8wf2q', 'fast8wf2'), 'scatterall12': ('fast8wq', 'fast8w')}
SAVING_S, UNIFORM_SLACK_S, IDLE_S, IDLE_MB_S = 3.0, 0.3, 1.5, 50


def source_idle(folder, meta):
    """Seconds of the fast run's sending with the source disk under IDLE_MB_S, and in how many stretches."""
    spans = exp76.phases(folder, meta)
    samples = exp76.series(folder / 'series-source.txt')
    if not spans or not samples:
        return None, None
    _, lo, hi = spans[4]
    total, gaps, inside = 0.0, 0, False
    for a, b in zip(samples, samples[1:]):
        if lo <= a[0] and b[0] <= hi:
            idle = (b[2] - a[2]) * 512 / 1e6 / (b[0] - a[0]) < IDLE_MB_S
            total += (b[0] - a[0]) if idle else 0.0
            gaps += idle and not inside
            inside = idle
    return round(total, 2), gaps


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp77'))
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp77-cells.csv'))
    ap.add_argument('--figure', default='exp77-timeline')
    a = ap.parse_args(argv)
    results = Path(a.results)
    rows = exp68.runs(results)
    extra = {}
    for meta_path in sorted(results.glob('run-*/meta.json')):
        meta, folder = json.loads(meta_path.read_text()), meta_path.parent
        send = folder / 'send.json'
        if meta['arm'] == 'full' or not send.exists() or not send.stat().st_size:
            continue
        stats = json.loads(send.read_text())
        received = (stats.get('receiver') or {}).get('fast_path') or {}
        spans = dict((name, end - start) for name, start, end in exp76.phases(folder, meta) or [])
        idle, gaps = source_idle(folder, meta)
        extra[meta['run']] = {'pages_sent': (stats.get('fast_path') or {}).get('pages_chosen'),
                              'pages_reread': received.get('pages_reread'),
                              'reread_s': received.get('reread_seconds'),
                              'verify': ((stats.get('receiver') or {}).get('switches') or {}).get('verify'),
                              'sending_s': spans.get('sending'), 'source_idle_s': idle, 'source_gaps': gaps}
    cells = []
    for pattern, arms in PAIRS.items():
        for arm in (*arms, *(['full'] if pattern == 'scatterall12' else [])):
            mine = [r for r in rows if r['pattern'] == pattern and r['arm'] == arm]
            if not mine:
                continue
            cell = {'pattern': pattern, 'arm': arm, 'runs': len(mine), 'seconds': exp68.median(mine, 'seconds'),
                    'source_io_s': exp68.median(mine, 'source_io_s'), 'dest_io_s': exp68.median(mine, 'dest_io_s')}
            more = [extra[r['run']] for r in mine if r['run'] in extra]
            for key in ('reread_s', 'sending_s', 'source_idle_s'):
                values = [m[key] for m in more if m.get(key) is not None]
                if values:
                    cell[key] = round(statistics.median(values), 2)
            if more:
                cell['verify'] = ' '.join(sorted({str(m['verify']) for m in more}))
            cells.append(cell)
            print(cell)
    by = {(c['pattern'], c['arm']): c for c in cells}
    saving = {p: round(by[(p, q)]['seconds'] - by[(p, v)]['seconds'], 2) for p, (q, v) in PAIRS.items()
              if (p, q) in by and (p, v) in by}
    one = saving.get('scatterall12') is not None and saving['scatterall12'] >= SAVING_S
    near = abs(saving.get('scatterall12', 0) - SAVING_S) <= 0.75
    print(f"1 (deciding). {'HELD' if one else 'FAILED'}: in scatterall12, main loop - verifier "
          f"{saving.get('scatterall12')} s (bar {SAVING_S})" + ('; within 0.75 s of the bar (rule 11)' if near else ''))
    two = saving.get('uniform') is not None and saving['uniform'] >= -UNIFORM_SLACK_S
    print(f"2. {'HELD' if two else 'FAILED'}: in uniform, main loop - verifier {saving.get('uniform')} s "
          f"(at least -{UNIFORM_SLACK_S})")
    idle = [(run, e['source_idle_s'], e['source_gaps']) for run, e in extra.items()
            if any(r['run'] == run and r['pattern'] == 'scatterall12' and r['arm'] == 'fast8w' for r in rows)]
    three = bool(idle) and all(s is not None and s <= IDLE_S for _, s, _ in idle)
    print(f"3. {'HELD' if three else 'FAILED'}: scatterall12 with the verifier, the source disk under {IDLE_MB_S} MB/s "
          f"while sending {idle} s (run, seconds, stretches; bar {IDLE_S})")
    four = bool(extra) and all(e['pages_reread'] == e['pages_sent'] and (e['reread_s'] or 0) > 0 for e in extra.values())
    print(f"4. {'HELD' if four else 'FAILED'}: every page sent re-read and timed "
          f"{[(run, e['pages_sent'], e['pages_reread'], e['reread_s']) for run, e in extra.items()]}")
    fast = [r for r in rows if r['arm'] != 'full']
    five = (bool(rows) and all(r['status'] == 0 for r in rows) and all(r['receiver_status'] == 0 for r in fast)
            and all(r['fast_used'] and r['match'] is True for r in fast))
    print(f"5. {'HELD' if five else 'FAILED'}: statuses {sorted({r['status'] for r in rows})}; fast used and matched "
          f"{all(r['fast_used'] and r['match'] is True for r in fast)} ({len(fast)} runs)")
    full, fill0 = by.get(('scatterall12', 'full')), by.get(('scatterall12', 'fast8w'))
    if full and fill0:
        print(f"  reported: scatterall12 fast path (verifier) / pg_basebackup {fill0['seconds'] / full['seconds']:.3f}")
    exp76.write_csv(a.out, cells)
    figure(results, rows, a.figure)


def figure(results, rows, name):
    """scatterall12's first run of each fast arm: the source's reads and the destination's writes (MB/s) against the
    seconds since t0, with the guard, the plan and the tail shaded."""
    plotting.style()
    arms = [('fast8wq', 'written pages checked in the main loop (--verify sequential)'),
            ('fast8w', 'written pages checked on the pipelined verifier (default)')]
    fig, axes = plt.subplots(len(arms), 1, figsize=(10.0, 5.4), sharex=True)
    shades = {'guard': COLORS['red'], 'plan': COLORS['yellow'], 'tail': COLORS['purple']}
    for ax, (arm, label) in zip(axes, arms):
        run = next((r for r in sorted(rows, key=lambda r: r['run']) if r['pattern'] == 'scatterall12'
                    and r['arm'] == arm), None)
        if run is None:
            continue
        folder = Path(results) / f"run-{run['run']:02d}"
        meta = json.loads((folder / 'meta.json').read_text())
        for side, key, color in (('source', 2, COLORS['blue']), ('dest', 4, COLORS['orange'])):
            samples = exp76.series(folder / f'series-{side}.txt')
            ax.plot([(b[0] + c[0]) / 2 - meta['t0'] for b, c in zip(samples, samples[1:])],
                    [(c[key] - b[key]) * 512 / 1e6 / (c[0] - b[0]) for b, c in zip(samples, samples[1:])],
                    color=color, lw=0.9, label='source reads' if side == 'source' else 'destination writes')
        for phase, start, end in exp76.phases(folder, meta) or []:
            if phase in shades:
                ax.axvspan(start - meta['t0'], end - meta['t0'], color=shades[phase], alpha=0.18,
                           label=phase if arm == arms[0][0] else None)
        ax.axvline(meta['t1'] - meta['t0'], color=COLORS['black'], lw=0.8)
        idle, gaps = source_idle(folder, meta)
        ax.set_title(f"{label}: {meta['t1'] - meta['t0']:.1f} s; source under {IDLE_MB_S} MB/s while sending "
                     f"{idle} s in {gaps} stretches", fontsize=9.5, loc='left')
        ax.set_ylabel('MB/s')
        ax.set_ylim(0, max(ax.get_ylim()[1], 1) * 1.3)
        ax.legend(fontsize=7.5, loc='upper right', ncol=5)
    axes[-1].set_xlabel('seconds since the run started (scatterall12, first run of each arm)')
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
