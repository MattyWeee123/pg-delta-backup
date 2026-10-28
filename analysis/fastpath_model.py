"""The fast path's time model and a selector (research loop 1's idea 4), from the plan the guard produces.

    .venv/bin/python analysis/fastpath_model.py [--results results/exp04/exp70]

Once the guard has run, the sender knows its plan: every chosen page, so every run it will read and write. This
model predicts the transfer from that plan and the disks' two limits, each measured in an earlier session:
- each side's requests: the sender's runs (at most MAX_RUN pages), each split by the block layer into requests of at
  most MAX_REQUEST_PAGES (168 KiB; EXP-70: the plans' runs reproduce the measured requests within 3% at fills 0-2);
- each disk's time: the larger of its requests at its operation rate (reads READ_OPS, writes WRITE_OPS) and its bytes
  at BYTES (EXP-66, EXP-68, EXP-70's post-hoc check and the full copy);
- the transfer: the busier disk (they overlap: EXP-68's read-ahead and sync worker);
- the rest of the session: the guard (measured, already spent when the selector decides) plus OVERHEAD_S and
  OVERHEAD_S_PER_MB per megabyte sent (fitted on EXP-70's hot and scattered cells, at fill 2; the start, the final
  syncs, the pipeline's start and end).
A full copy: FULL_FIXED_S plus every listed file's bytes at BYTES (EXP-70's full copy).

The selector predicts each fill in FILLS (fastpath.fill_gaps of the unfilled plan, file by file, as the sender
makes them) and a full copy, and picks the fastest. Run as a script it reports the model on EXP-70's cells: an
in-sample fit, not a test (EXP-74 tests it on cells it hasn't seen).
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pgdelta import fastpath  # noqa: E402

PAGE = 8192
MAX_RUN = 128  # the sender's runs: at most 1 MiB of pages
MAX_REQUEST_PAGES = 21  # 168 KiB: the requests a run reaches the disk as (EXP-70, inferred from fill 4)
READ_OPS = 4100  # the source's reads per busy second at its cap (EXP-68: 254,547 in about 62 s; EXP-70: 4,130)
WRITE_OPS = 4790  # the destination's writes per busy second through the page cache (EXP-70's post-hoc check)
BYTES = 210e6  # either balanced persistent disk's bytes per busy second (EXP-70's full copy; EXP-66: 205.5 MB/s)
OVERHEAD_S = 0.6  # the fast path's session beyond the guard and the busier disk: 0.6 s
OVERHEAD_S_PER_MB = 0.00097  # plus 0.97 ms per MB sent (EXP-70: hot 0.72 s at 124 MB, scattered 7.44 s at 7,033 MB)
FULL_FIXED_S = 5.7  # a full copy beyond its bytes at BYTES (EXP-70: 91.02 s, both disks busy 85.3 s)
FILLS = (0, 1, 2, 4)


def load_plan(path):
    """A saved plan (--plan-out): ({file path: sorted pages} for the files not sent whole, bytes of the whole files,
    bytes of every listed file: what a full copy reads; EXP-70's plan listed 17,866 MB, its full copy read 17,865)."""
    plan = json.loads(gzip.decompress(Path(path).read_bytes()))
    pages, whole, total = {}, 0, 0
    for entry in plan['files']:
        total += entry['size']
        if entry['whole']:
            whole += entry['size']
        else:
            pages[entry['path']] = [p for first, count in entry['runs'] for p in range(first, first + count)]
    return pages, whole, total


def runs_of(pages):
    """The sender's runs over sorted pages: consecutive pages, at most MAX_RUN each."""
    runs, start, count = [], None, 0
    for page in pages:
        if start is not None and page == start + count and count < MAX_RUN:
            count += 1
        else:
            if start is not None:
                runs.append(count)
            start, count = page, 1
    if start is not None:
        runs.append(count)
    return runs


def transfer(pages_by_file, whole_bytes):
    """The plan's requests, bytes and each disk's predicted busy seconds."""
    runs = [n for pages in pages_by_file.values() for n in runs_of(pages)]
    requests = sum(math.ceil(n / MAX_REQUEST_PAGES) for n in runs)
    pages = sum(runs)
    data = pages * PAGE + whole_bytes
    source = max(requests / READ_OPS, data / BYTES)
    dest = max(requests / WRITE_OPS, data / BYTES)
    return {'pages': pages, 'runs': len(runs), 'requests': requests, 'mb': round(data / 1e6, 1),
            'source_s': round(source, 2), 'dest_s': round(dest, 2), 'transfer_s': round(max(source, dest), 2)}


def predict_fast(pages_by_file, whole_bytes, guard_s):
    t = transfer(pages_by_file, whole_bytes)
    t['seconds'] = round(guard_s + OVERHEAD_S + OVERHEAD_S_PER_MB * t['mb'] + t['transfer_s'], 2)
    return t


def predict_full(database_bytes):
    return round(FULL_FIXED_S + database_bytes / BYTES, 2)


def filled(pages_by_file, gap):
    return {path: fastpath.fill_gaps(pages, gap) for path, pages in pages_by_file.items()}


def select(pages_by_file, whole_bytes, database_bytes, guard_s, fills=FILLS):
    """{option: predicted seconds} for each fill and 'full', and the option predicted fastest."""
    options = {f'fill{k}': predict_fast(filled(pages_by_file, k), whole_bytes, guard_s)['seconds'] for k in fills}
    options['full'] = predict_full(database_bytes)
    return options, min(options, key=options.get)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--results', default=str(ROOT / 'results' / 'exp04' / 'exp70'))
    a = ap.parse_args(argv)
    results = Path(a.results)
    import csv
    cells = {(r['pattern'], r['arm']): r for r in csv.DictReader(open(ROOT / 'docs' / 'experiments' / 'data' /
                                                                     'exp70-cells.csv'))}
    print('In-sample, on EXP-70 (the constants were fitted on these cells or measured beside them):')
    for pattern, arms in (('hot', ('fast8w', 'fast8wf2')), ('uniform', ('fast8w', 'fast8wf1', 'fast8wf2', 'fast8wf4'))):
        for arm in arms:
            cell = cells[(pattern, arm)]
            pages, whole, _ = load_plan(results / f'plan-runs-{pattern}-{arm}.json.gz')
            p = predict_fast(pages, whole, float(cell['guard_s']))
            print(f"  {pattern} {arm}: predicted {p['seconds']} s, measured {cell['seconds']} s "
                  f"({float(cell['seconds']) / p['seconds']:.3f}); requests {p['requests']} (source measured "
                  f"{cell['source_reads']}, dest {cell['dest_writes']}); busy source {p['source_s']} "
                  f"({cell['source_io_s']}), dest {p['dest_s']} ({cell['dest_io_s']})")
    pages, whole, database = load_plan(results / 'plan-runs-uniform-fast8w.json.gz')
    print(f"  uniform full: predicted {predict_full(database)} s, measured {cells[('uniform', 'full')]['seconds']} s")
    options, choice = select(pages, whole, database, float(cells[('uniform', 'fast8w')]['guard_s']))
    print(f"  the selector on the uniform cell's unfilled plan: {options} -> {choice}")


if __name__ == '__main__':
    main()
