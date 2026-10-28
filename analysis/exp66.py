"""EXP-66's analysis: how a balanced persistent disk counts I/O by size, and what gap-filled writes would save
(docs/experiments/EXP-66-disk-write-sizes.md).

    .venv/bin/python analysis/exp66.py [--results results/exp04/exp66]

Reads fio's JSON per configuration. Scores 1 (deciding: random 32 KiB writes at queue depth 32 reach at least 0.7 of
the operations per second of 8 KiB ones), 2 (random 8 KiB reads at queue depth 1 at most 2,500 a second) and 3 (the
model's write time for EXP-64's uniform page set, without filling, within 30% of EXP-64's measured 107 s of writing
and syncing). The model: pages chosen independently with EXP-48's uniform share (619,856 of 2,175,659), runs cut at
1 GiB segments, then gaps of at most g clean pages filled (g = 0 to 8); each write of b bytes costs
max(1 / ops(b), b / sequential write rate), ops(b) interpolated from the random-write sizes. Prints the decision
rule's verdict (idea 2 is prototyped if 1 and 3 hold and the best g cuts the write time by at least 25%). Writes
docs/experiments/data/exp66-fio.csv, exp66-model.csv and the figure exp66-write-sizes.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np

import plotting
from plotting import COLORS, plt

ROOT = Path(__file__).resolve().parents[1]
PAGE = 8192
SEGMENT_PAGES = (1 << 30) // PAGE
UNIFORM_PAGES, TOTAL_PAGES = 619_856, 2_175_659  # EXP-48's uniform cell
MEASURED_WRITE_S = 107.0  # EXP-64, fast path, uniform: rebuild 57.6 s + fsync 49.4 s
# Found after the data: in fsync mode main the receiver's rebuild timer already includes each file's fsync, so the
# registered 107 s counted the syncs twice; the receiver wrote and synced in 57.6 s (EXP-64) and 59.9 s (EXP-64b's
# fast arm). And the real page set fell in 249,193 runs (EXP-64b's read-ahead count), not the model's 443,128.
# Prediction 3 is scored as registered; the figure shows both numbers, and the write-up the correction.
REBUILD_S, REAL_RUNS = 59.9, 249_193  # EXP-64b, fast arm, median; and its readers' count of runs
SIZES_K = (8, 16, 32, 64, 128)
GAPS = range(0, 9)


def load(results):
    out = {}
    for path in sorted(Path(results).glob('*.json')):
        if path.stem == 'layout':
            continue
        job = json.loads(path.read_text())['jobs'][0]
        side = job['write'] if job['write']['io_bytes'] else job['read']
        out[path.stem] = {'config': path.stem, 'ops_per_s': round(side['iops'], 1),
                          'mb_per_s': round(side['bw_bytes'] / 1e6, 2),
                          'mean_latency_us': round(side['clat_ns']['mean'] / 1e3, 1)}
    return out


def runs_of(chosen):
    """(start, length) of each run of chosen pages, cut at segment boundaries."""
    padded = np.concatenate(([False], chosen, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    starts, ends = edges[0::2], edges[1::2]
    out_starts, out_lengths = [], []
    for s, e in zip(starts, ends):
        while s < e:
            cut = min(e, (s // SEGMENT_PAGES + 1) * SEGMENT_PAGES)
            out_starts.append(s)
            out_lengths.append(cut - s)
            s = cut
    return np.array(out_starts), np.array(out_lengths)


def filled(starts, lengths, gap):
    """Runs after filling every gap of at most `gap` clean pages within a segment."""
    merged_lengths, current_start, current_end = [], starts[0], starts[0] + lengths[0]
    for s, n in zip(starts[1:], lengths[1:]):
        if s - current_end <= gap and s // SEGMENT_PAGES == current_start // SEGMENT_PAGES:
            current_end = s + n
        else:
            merged_lengths.append(current_end - current_start)
            current_start, current_end = s, s + n
    merged_lengths.append(current_end - current_start)
    return np.array(merged_lengths)


def cost_of(fio):
    """The cost model: a write of b bytes takes max(1 / ops(b), b / the sequential write rate) seconds."""
    ops = np.array([fio[f'randwrite-{k}k-qd32']['ops_per_s'] for k in SIZES_K])
    seq = fio['seqwrite-1m-qd8']['mb_per_s'] * 1e6
    sizes = np.array(SIZES_K) * 1024

    def cost(nbytes):
        per_op = 1 / np.interp(nbytes, sizes, ops)  # flat beyond 128 KiB; the bandwidth term takes over
        return np.maximum(per_op, nbytes / seq)
    return cost


def fill_rows(cost, starts, lengths):
    """The modelled write time of runs (start, length) with clean gaps of up to g pages filled, g in GAPS. Starts are
    pages numbered so that each segment holds SEGMENT_PAGES of them, since a fill never crosses a segment (EXP-68
    reuses this for the real page set)."""
    rows, pages = [], int(lengths.sum())
    for g in GAPS:
        merged = filled(starts, lengths, g)
        nbytes = merged * PAGE
        rows.append({'gap_pages': g, 'writes': len(merged), 'pages_written': int(merged.sum()),
                     'extra_pages': int(merged.sum() - pages),
                     'mean_write_kib': round(float(nbytes.mean()) / 1024, 1),
                     'seconds': round(float(cost(nbytes).sum()), 1)})
    return rows


def model(fio, seed=660):
    chosen = np.random.default_rng(seed).random(TOTAL_PAGES) < UNIFORM_PAGES / TOTAL_PAGES
    starts, lengths = runs_of(chosen)
    return fill_rows(cost_of(fio), starts, lengths)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp66'))
    a = ap.parse_args(argv)
    fio = load(a.results)
    for row in fio.values():
        print(row)
    rows = model(fio)
    for row in rows:
        print(row)
    w8, w32 = fio['randwrite-8k-qd32']['ops_per_s'], fio['randwrite-32k-qd32']['ops_per_s']
    one = round(w32 / w8, 3)
    print(f"1 (deciding). {'HELD' if one >= 0.7 else 'FAILED'}: 32 KiB / 8 KiB random-write ops {one} (bar 0.7)"
          + ('; within 0.05 of the bar (rule 11)' if abs(one - 0.7) <= 0.05 else ''))
    r1 = fio['randread-8k-qd1']['ops_per_s']
    print(f"2. {'HELD' if r1 <= 2500 else 'FAILED'}: random 8 KiB reads at depth 1, {r1:,.0f} a second (bar 2,500)")
    base = rows[0]['seconds']
    three = abs(base / MEASURED_WRITE_S - 1) <= 0.30
    print(f"3. {'HELD' if three else 'FAILED'}: the model without filling {base} s against EXP-64's "
          f"{MEASURED_WRITE_S} s")
    best = min(rows, key=lambda r: r['seconds'])
    cut = round(1 - best['seconds'] / base, 3)
    go = one >= 0.7 and three and cut >= 0.25
    print(f"Decision rule: best fill {best['gap_pages']} pages, {best['seconds']} s, a cut of {cut:.1%}: "
          f"{'prototype idea 2' if go else 'idea 2 is not prototyped'}")
    data = ROOT / 'docs' / 'experiments' / 'data'
    for name, table in (('exp66-fio.csv', list(fio.values())), ('exp66-model.csv', rows)):
        with open(data / name, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(table[0]))
            w.writeheader()
            w.writerows(table)
    figure(fio, rows)


def figure(fio, rows):
    plotting.style()
    fig, (left, right) = plt.subplots(1, 2, figsize=(10.2, 4.2))
    ops = [fio[f'randwrite-{k}k-qd32']['ops_per_s'] for k in SIZES_K]
    mbs = [fio[f'randwrite-{k}k-qd32']['mb_per_s'] for k in SIZES_K]
    left.plot(SIZES_K, ops, 'o-', color=COLORS['blue'], label='random writes a second (left axis)')
    left.set_xscale('log', base=2)
    left.set_xticks(SIZES_K, [f'{k} KiB' for k in SIZES_K])
    left.set_ylabel('operations a second, queue depth 32')
    left.set_ylim(0, max(ops) * 1.25)
    twin = left.twinx()
    twin.plot(SIZES_K, mbs, 's--', color=COLORS['orange'], label='MB/s (right axis)')
    twin.axhline(fio['seqwrite-1m-qd8']['mb_per_s'], color=COLORS['orange'], lw=0.8, ls=':')
    twin.set_ylabel('MB/s')
    twin.set_ylim(0, fio['seqwrite-1m-qd8']['mb_per_s'] * 1.3)
    left.legend(fontsize=8, loc='lower left')
    twin.legend(fontsize=8, loc='lower right')
    left.set_title('How the disk counts writes by size', fontsize=10)
    gaps = [r['gap_pages'] for r in rows]
    right.bar(gaps, [r['seconds'] for r in rows], 0.7, color=COLORS['blue'])
    right.axhline(MEASURED_WRITE_S, color=COLORS['black'], lw=0.8, ls=':',
                  label=f'registered comparison, {MEASURED_WRITE_S:.0f} s (syncs counted twice)')
    right.axhline(REBUILD_S, color=COLORS['red'], lw=0.9, ls='--',
                  label=f'measured writing and syncing, {REBUILD_S:.0f} s (EXP-64b)')
    right.set_ylim(0, MEASURED_WRITE_S * 1.35)
    right.legend(fontsize=7.5, loc='upper right')
    right.set_xlabel('clean gaps filled, up to this many pages')
    right.set_ylabel('modelled seconds to write the uniform page set')
    right.set_title(f'Gap-filled writes, pages chosen independently\n({rows[0]["writes"]:,} writes unfilled; the real '
                    f'set had {REAL_RUNS:,} runs)', fontsize=9.5)
    fig.suptitle('EXP-66: a 150 GiB balanced persistent disk, fio with direct I/O', fontsize=10.5)
    fig.tight_layout()
    print(plotting.save(fig, 'exp66-write-sizes'))


if __name__ == '__main__':
    main()
