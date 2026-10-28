"""EXP-78: does the guard's grouped summary reading get faster without the server's sort, the blocks of each relation
fork returned unsorted with their duplicates and de-duplicated by the sender's sets
(docs/experiments/EXP-78-guard-unsorted.md)?

    .venv/bin/python experiments/fastpath_fixed_costs/guard_unsorted.py [--scale 320] [--repeats 5] [--seed 780]

EXP-73b's matched scale model (guard_reading.build: pgbench at --scale, 1% of rows updated in 20 checkpointed
batches, the lab's state check, a checkpoint), then over the summaries from the start on, each repeat in a rotated
order:
- B, the sender's reading today (live.summary_blocks: array_agg(DISTINCT ... ORDER BY ...) per fork, one query);
- D, the same query with array_agg(...) unsorted and with duplicates, one connection;
- D4, D's query split over 4 connections by summary, merged per fork (reported, not decided);
- the server's grouping without the arrays (count only), for scale.
Each plan is fastpath.grouped_plan's (or the merge's) and is compared with B's. Writes
docs/experiments/data/exp78-guard.csv and the figure exp78-guard, and scores the registered predictions.
"""
import argparse
import csv
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'analysis'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import guard_reading  # noqa: E402
from pgdelta import fastpath  # noqa: E402
from pgdelta.live import Session  # noqa: E402

SORTED = 'array_agg(DISTINCT c.relblocknumber ORDER BY c.relblocknumber)'
UNSORTED = 'array_agg(c.relblocknumber)'
QUERY = ("SELECT json_build_array(coalesce(sum(n), 0), coalesce(json_agg(json_build_array(ts, db, rel, fork, blocks, "
         "lim)), '[]'::json)) FROM (SELECT c.reltablespace AS ts, c.reldatabase AS db, c.relfilenode AS rel, "
         "c.relforknumber AS fork, {agg} FILTER (WHERE NOT c.is_limit_block) AS blocks, min(c.relblocknumber) FILTER "
         "(WHERE c.is_limit_block) AS lim, count(*) AS n FROM (VALUES {values}) AS s(tli, start_lsn, end_lsn) CROSS JOIN "
         "LATERAL pg_wal_summary_contents(s.tli, s.start_lsn, s.end_lsn) AS c GROUP BY 1, 2, 3, 4) AS g")
COUNT = ("SELECT count(*) FROM (SELECT c.reltablespace, c.reldatabase, c.relfilenode, c.relforknumber FROM (VALUES "
         "{values}) AS s(tli, start_lsn, end_lsn) CROSS JOIN LATERAL pg_wal_summary_contents(s.tli, s.start_lsn, "
         "s.end_lsn) AS c GROUP BY 1, 2, 3, 4) AS g")


def values(summaries):
    return ', '.join(f"({int(tli)}, '{start}'::pg_lsn, '{end}'::pg_lsn)" for tli, start, end in summaries)


def read(session, summaries, agg):
    """(summary rows, groups) of one grouped query over `summaries`."""
    rows, groups = session.value(QUERY.format(agg=agg, values=values(summaries)))
    return int(rows), [tuple(g) for g in groups]


def merged(parts):
    """fastpath.grouped_plan's plan from several queries' groups, a fork's blocks united and its lowest limit kept."""
    plan = {}
    for _, groups in parts:
        for ts, db, rel, fork, blocks, limit in groups:
            path = fastpath.relation_path(int(ts), int(db), int(rel), int(fork))
            if path is None:
                continue
            have, low = plan.get(path, (set(), None))
            have.update(int(b) for b in blocks or ())
            if limit is not None:
                low = int(limit) if low is None else min(low, int(limit))
            plan[path] = (have, low)
    return plan


def timed(name, sessions, summaries):
    started = time.perf_counter()
    if name == 'B':
        plan = fastpath.grouped_plan(read(sessions[0], summaries, SORTED)[1])
    elif name == 'D':
        plan = fastpath.grouped_plan(read(sessions[0], summaries, UNSORTED)[1])
    elif name == 'D4':
        with ThreadPoolExecutor(4) as pool:
            parts = list(pool.map(lambda pair: read(pair[0], pair[1], UNSORTED),
                                  zip(sessions[:4], [summaries[i::4] for i in range(4)])))
        plan = merged(parts)
    else:  # the server's grouping alone
        sessions[0].value(COUNT.format(values=values(summaries)))
        plan = None
    return time.perf_counter() - started, plan


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--scale', type=int, default=320)
    ap.add_argument('--repeats', type=int, default=5)
    ap.add_argument('--seed', type=int, default=780)
    ap.add_argument('--bar', type=float, default=0.65)
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp78-guard.csv'))
    ap.add_argument('--figure', default='exp78-guard')
    a = ap.parse_args()
    print(guard_reading.run(guard_reading.PG_BIN / 'postgres', '--version').stdout.strip(), flush=True)
    methods = ['B', 'D', 'D4', 'count']
    records = []
    with tempfile.TemporaryDirectory(prefix='exp78-') as temp:
        cluster = guard_reading.Cluster(Path(temp))
        try:
            started = time.perf_counter()
            _, summaries = guard_reading.build(cluster, a.scale, seed=a.seed, batches=20, state_check=True)
            print(f'scale {a.scale}: built in {time.perf_counter() - started:.0f} s, {len(summaries)} summaries',
                  flush=True)
            sessions = [Session(cluster.conninfo, psql=guard_reading.PG_BIN / 'psql') for _ in range(4)]
            try:
                rows = read(sessions[0], summaries, SORTED)[0]
                for name in methods:  # warm-up, not recorded
                    timed(name, sessions, summaries)
                for rep in range(a.repeats):
                    order = methods[rep % len(methods):] + methods[:rep % len(methods)]
                    results = {name: timed(name, sessions, summaries) for name in order}
                    base = results['B'][1]
                    for name in methods:
                        seconds, plan = results[name]
                        records.append({'scale': a.scale, 'repeat': rep, 'method': name, 'seconds': round(seconds, 4),
                                        'summaries': len(summaries), 'summary_rows': rows,
                                        'distinct_blocks': sum(len(b) for b, _ in base.values()),
                                        'plan_equal': None if plan is None else plan == base})
                    print(f'rep {rep}: ' + ', '.join(f'{n} {results[n][0]:.3f} s' for n in methods)
                          + f"; plans equal D {results['D'][1] == base}, D4 {results['D4'][1] == base}", flush=True)
            finally:
                for session in sessions:
                    session.close()
        finally:
            cluster.stop()
    with open(a.out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(records[0]))
        w.writeheader()
        w.writerows(records)
    score(records, a.bar)
    figure(records, a.figure)


def score(records, bar):
    med = {m: statistics.median(r['seconds'] for r in records if r['method'] == m) for m in ('B', 'D', 'D4', 'count')}
    ratio = med['D'] / med['B']
    equal = all(r['plan_equal'] for r in records if r['plan_equal'] is not None)
    print(f"1 (deciding). {'HELD' if ratio <= bar and equal else 'FAILED'}: D / B {ratio:.3f} (bar {bar}), plans "
          f"equal in every run {equal}" + ('; within 0.05 of the bar (rule 11)' if abs(ratio - bar) <= 0.05 else ''))
    per_row = med['B'] / records[0]['summary_rows'] * 1e6
    print(f"2. {'HELD' if all(r['plan_equal'] for r in records if r['method'] == 'D') else 'FAILED'}: D's plan equals "
          f"B's in every run")
    print(f"reported: medians {({k: round(v, 3) for k, v in med.items()})}; D4 / B {med['D4'] / med['B']:.3f}; "
          f"B {per_row:.2f} us per summary row")


def figure(records, name):
    import plotting
    from plotting import COLORS, plt
    plotting.style()
    fig, ax = plt.subplots(figsize=(7.0, 3.8))
    labels = {'B': 'B: sorted, distinct\n(the sender today)', 'D': 'D: unsorted,\nduplicates kept',
              'D4': 'D over 4\nconnections', 'count': "the server's grouping\nwithout the arrays"}
    colors = {'B': COLORS['grey'], 'D': COLORS['blue'], 'D4': COLORS['sky'], 'count': COLORS['orange']}
    for i, m in enumerate(labels):
        values = [r['seconds'] for r in records if r['method'] == m]
        ax.bar(i, statistics.median(values), 0.6, color=colors[m])
        ax.plot([i] * len(values), values, 'o', color=COLORS['black'], ms=3)
        ax.annotate(f'{statistics.median(values):.3f} s', (i, max(values)), textcoords='offset points', xytext=(0, 4),
                    ha='center', fontsize=8.5)
    ax.set_xticks(range(len(labels)), list(labels.values()), fontsize=8.5)
    ax.set_ylabel('seconds to read the summaries\n(median and each run)')
    ax.set_title(f"EXP-78: the guard's reading at scale {records[0]['scale']} "
                 f"({records[0]['summary_rows']:,} summary rows)", fontsize=10)
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
