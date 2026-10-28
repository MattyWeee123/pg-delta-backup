"""EXP-73: where the fast path's guard spends its time reading WAL summaries, and whether one query grouped per
relation fork on the server cuts it (docs/experiments/EXP-73-guard-summary-reading.md).

    .venv/bin/python experiments/fastpath_fixed_costs/guard_reading.py [--scales 20,80] [--repeats 3]

For each scale: a fresh local cluster (data checksums, summarize_wal on), pgbench -i, a CHECKPOINT (the start), then
1% of pgbench_accounts' rows updated in random order in 5 batches with a CHECKPOINT after each. Over the summaries
from the start on, through one pgdelta.live.Session:
- A, today's (live.summary_rows per summary, then fastpath.block_plan), timed in parts: the server alone (the same
  json_agg, only its length returned), psql's transfer and json.loads, and the tuples and block_plan;
- B: one query over all the summaries, grouped per relation fork on the server (each fork's distinct blocks as one
  sorted array, and its lowest limit block), then the same plan structure.
Each run checks that B's plan equals A's. Writes docs/experiments/data/exp73-guard.csv and the figure exp73-guard, and
scores the registered predictions.

EXP-73b (--batches 20 --state-check --deciding-bar 0.5 --exp 73b): the lag in 20 batches, and after its last
checkpoint the lab's state check (a read of all of pgbench_accounts, whose hint bits log a page image of each changed
page with checksums), then a checkpoint, so a block appears in about as many summaries as on the VMs (EXP-73's
post-hoc check). Its outputs are exp73b-guard.csv and exp73b-guard.
"""
import argparse
import csv
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'analysis'))
from pgdelta import fastpath  # noqa: E402
from pgdelta.live import Session  # noqa: E402

PG_BIN = Path(os.environ.get('PG_BIN', '/opt/homebrew/opt/postgresql@17/bin'))
PORT = 5447
ROW_SQL = ("SELECT coalesce(json_agg(json_build_array(reltablespace, reldatabase, relfilenode, relforknumber, "
           "relblocknumber, is_limit_block)), '[]'::json) FROM pg_wal_summary_contents({tli}, '{start}', '{end}')")
SERVER_SQL = ("SELECT length(coalesce(json_agg(json_build_array(reltablespace, reldatabase, relfilenode, relforknumber, "
              "relblocknumber, is_limit_block)), '[]'::json)::text) FROM pg_wal_summary_contents({tli}, '{start}', "
              "'{end}')")
GROUPED_SQL = ("SELECT coalesce(json_agg(json_build_array(ts, db, rel, fork, blocks, lim)), '[]'::json) FROM ("
               "SELECT c.reltablespace AS ts, c.reldatabase AS db, c.relfilenode AS rel, c.relforknumber AS fork, "
               "array_agg(DISTINCT c.relblocknumber ORDER BY c.relblocknumber) FILTER (WHERE NOT c.is_limit_block) "
               "AS blocks, min(c.relblocknumber) FILTER (WHERE c.is_limit_block) AS lim "
               "FROM (VALUES {values}) AS s(tli, start_lsn, end_lsn) "
               "CROSS JOIN LATERAL pg_wal_summary_contents(s.tli, s.start_lsn, s.end_lsn) AS c "
               "GROUP BY 1, 2, 3, 4) AS g")


ENV = dict(os.environ, LC_ALL='C', LANG='C')  # macOS: the postmaster refuses to start without a valid locale


def run(*args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, capture_output=True, text=True, env=ENV, **kwargs)


class Cluster:
    def __init__(self, root):
        self.data, self.sockets = root / 'data', Path(tempfile.mkdtemp(prefix='pg73'))
        run(PG_BIN / 'initdb', '-D', self.data, '-U', 't', '-A', 'trust', '--no-sync', '--data-checksums')
        with open(self.data / 'postgresql.conf', 'a') as conf:
            conf.write(f"summarize_wal = on\nport = {PORT}\nunix_socket_directories = '{self.sockets}'\n"
                       "listen_addresses = ''\nmax_wal_size = 4GB\n")
        run(PG_BIN / 'pg_ctl', '-D', self.data, '-w', '-l', root / 'server.log', 'start')
        self.conninfo = f'host={self.sockets} port={PORT} user=t dbname=postgres'

    def sql(self, statement):
        return run(PG_BIN / 'psql', '-X', '-At', '-v', 'ON_ERROR_STOP=1', '-d', self.conninfo, '-c', statement).stdout.strip()

    def stop(self):
        subprocess.run([str(PG_BIN / 'pg_ctl'), '-D', str(self.data), '-m', 'fast', '-w', 'stop'], capture_output=True,
                       env=ENV)
        shutil.rmtree(self.sockets, True)


def build(cluster, scale, seed, batches=5, state_check=False):
    run(PG_BIN / 'pgbench', '-i', '-q', '-s', scale, '-d', cluster.conninfo)
    cluster.sql('CHECKPOINT')
    start = cluster.sql('SELECT pg_current_wal_lsn()')
    rows = 100000 * scale
    chosen = random.Random(seed).sample(range(1, rows + 1), rows // 100)
    for batch in range(batches):
        part = chosen[batch::batches]
        cluster.sql('UPDATE pgbench_accounts SET abalance = abalance + 1 WHERE aid = ANY(ARRAY['
                    + ','.join(map(str, part)) + '])')
        cluster.sql('CHECKPOINT')
    if state_check:  # the lab's state check after the lag's checkpoint (EXP-67, EXP-69)
        cluster.sql('SELECT count(*), sum(abalance) FROM pgbench_accounts')
        cluster.sql('CHECKPOINT')
    switched = cluster.sql('SELECT pg_switch_wal()')
    cluster.sql('CHECKPOINT')
    for _ in range(600):
        if cluster.sql(f"SELECT summarized_lsn >= '{switched}' FROM pg_get_wal_summarizer_state()") == 't':
            break
        time.sleep(0.1)
    else:
        raise SystemExit('the WAL summarizer did not catch up')
    summaries = [tuple(line.split('|')) for line in cluster.sql(
        f"SELECT tli, start_lsn, end_lsn FROM pg_available_wal_summaries() WHERE end_lsn > '{start}' "
        "ORDER BY start_lsn").splitlines()]
    return start, summaries


def method_a(session, summaries):
    server = transfer = python = 0.0
    rows_total, plan_rows = 0, []
    for tli, start, end in summaries:
        t0 = time.perf_counter()
        session.value(SERVER_SQL.format(tli=int(tli), start=start, end=end))
        t1 = time.perf_counter()
        rows = session.value(ROW_SQL.format(tli=int(tli), start=start, end=end))
        t2 = time.perf_counter()
        tuples = [tuple(row) for row in rows]
        plan_rows += tuples
        t3 = time.perf_counter()
        server += t1 - t0
        transfer += (t2 - t1) - (t1 - t0)  # the same query's server work, then psql and json.loads
        python += t3 - t2
        rows_total += len(rows)
    t4 = time.perf_counter()
    plan = fastpath.block_plan(plan_rows)
    python += time.perf_counter() - t4
    # A's own time is what the sender spends: the full query (server and transfer) and Python, not the server-only probe.
    return plan, {'server_s': server, 'transfer_s': transfer, 'python_s': python,
                  'seconds': server + transfer + python, 'rows': rows_total}


def method_b(session, summaries):
    t0 = time.perf_counter()
    values = ', '.join(f"({int(tli)}, '{start}'::pg_lsn, '{end}'::pg_lsn)" for tli, start, end in summaries)
    groups = session.value(GROUPED_SQL.format(values=values))
    t1 = time.perf_counter()
    plan = {}
    for ts, db, rel, fork, blocks, lim in groups:
        path = fastpath.relation_path(int(ts), int(db), int(rel), int(fork))
        if path is not None:
            plan[path] = (set(blocks or ()), None if lim is None else int(lim))
    t2 = time.perf_counter()
    return plan, {'server_s': None, 'transfer_s': t1 - t0, 'python_s': t2 - t1, 'seconds': t2 - t0,
                  'rows': len(groups)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--scales', default='20,80')
    ap.add_argument('--repeats', type=int, default=3)
    ap.add_argument('--batches', type=int, default=5)
    ap.add_argument('--state-check', action='store_true')
    ap.add_argument('--deciding-bar', type=float, default=0.4)
    ap.add_argument('--exp', default='73')
    ap.add_argument('--out')
    ap.add_argument('--figure')
    a = ap.parse_args()
    a.out = a.out or str(ROOT / 'docs' / 'experiments' / 'data' / f'exp{a.exp}-guard.csv')
    a.figure = a.figure or f'exp{a.exp}-guard'
    print(run(PG_BIN / 'postgres', '--version').stdout.strip(), flush=True)
    records = []
    for scale in [int(s) for s in a.scales.split(',')]:
        with tempfile.TemporaryDirectory(prefix='exp73-') as temp:
            cluster = Cluster(Path(temp))
            try:
                started = time.perf_counter()
                _, summaries = build(cluster, scale, seed=73 + scale, batches=a.batches, state_check=a.state_check)
                print(f'scale {scale}: built in {time.perf_counter() - started:.0f} s, {len(summaries)} summaries',
                      flush=True)
                session = Session(cluster.conninfo, psql=PG_BIN / 'psql')
                try:
                    method_a(session, summaries), method_b(session, summaries)  # warm-up, not recorded
                    for rep in range(a.repeats):
                        plan_a, ra = method_a(session, summaries)
                        plan_b, rb = method_b(session, summaries)
                        equal = plan_a == plan_b
                        blocks = sum(len(b) for b, _ in plan_a.values())
                        for name, r in (('A', ra), ('B', rb)):
                            records.append({'scale': scale, 'method': name, 'repeat': rep, 'summaries': len(summaries),
                                            'forks': len(plan_a), 'distinct_blocks': blocks, 'plan_equal': equal,
                                            **{k: (None if v is None else round(v, 4) if isinstance(v, float) else v)
                                               for k, v in r.items()}})
                        print(f"scale {scale} rep {rep}: A {ra['seconds']:.3f} s ({ra['rows']} rows; server "
                              f"{ra['server_s']:.3f}, transfer {ra['transfer_s']:.3f}, python {ra['python_s']:.3f}), "
                              f"B {rb['seconds']:.3f} s ({rb['rows']} forks), {blocks} blocks, plans equal {equal}",
                              flush=True)
                finally:
                    session.close()
            finally:
                cluster.stop()
    with open(a.out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(records[0]))
        w.writeheader()
        w.writerows(records)
    score(records, a.deciding_bar)
    figure(records, a.figure, a.exp)


def med(records, scale, method, key):
    return statistics.median(r[key] for r in records if r['scale'] == scale and r['method'] == method)


def score(records, bar=0.4):
    scales = sorted({r['scale'] for r in records})
    big, small = scales[-1], scales[0]
    ratio = med(records, big, 'B', 'seconds') / med(records, big, 'A', 'seconds')
    equal = all(r['plan_equal'] for r in records)
    print(f"1 (deciding). {'HELD' if ratio <= bar and equal else 'FAILED'}: at scale {big}, B / A {ratio:.3f} (bar {bar})"
          + ('; within 0.05 of the bar (rule 11)' if abs(ratio - bar) <= 0.05 else ''))
    print(f"2. {'HELD' if equal else 'FAILED'}: plans equal in every run: {equal}")
    per_row = {s: med(records, s, 'A', 'seconds') / next(r['rows'] for r in records if r['scale'] == s and r['method'] == 'A')
               for s in scales}
    print(f"3. {'HELD' if abs(per_row[small] / per_row[big] - 1) <= 0.30 else 'FAILED'}: A's seconds per row "
          f"{ {s: f'{v * 1e6:.2f} us' for s, v in per_row.items()} } (within 30%)")
    parts = {k: med(records, big, 'A', k) for k in ('server_s', 'transfer_s', 'python_s')}
    total = sum(parts.values())
    print(f"4. {'HELD' if max(parts.values()) / total <= 0.70 else 'FAILED'}: A's parts at scale {big} "
          f"{ {k: round(v / total, 3) for k, v in parts.items()} } (none over 0.70)")
    rows_big = next(r['rows'] for r in records if r['scale'] == big and r['method'] == 'A')
    print(f"Reported: projection to EXP-71's 1.47 M rows: A {per_row[big] * 1.47e6:.1f} s; per scale "
          + '; '.join(f"{s}: {next(r['rows'] for r in records if r['scale'] == s and r['method'] == 'A')} rows, "
                      f"{next(r['distinct_blocks'] for r in records if r['scale'] == s)} blocks, "
                      f"{next(r['forks'] for r in records if r['scale'] == s)} forks, "
                      f"{next(r['summaries'] for r in records if r['scale'] == s)} summaries" for s in scales)
          + f" (rows at {big}: {rows_big})")
    for s in scales:
        rows = next(r['rows'] for r in records if r['scale'] == s and r['method'] == 'A')
        blocks = next(r['distinct_blocks'] for r in records if r['scale'] == s)
        b_block = med(records, s, 'B', 'seconds') / blocks
        print(f"Duplication and the post-hoc model at scale {s}: {rows / blocks:.2f} rows per block; B per block "
              f"{b_block * 1e6:.2f} us; B / A {med(records, s, 'B', 'seconds') / med(records, s, 'A', 'seconds'):.3f}, "
              f"EXP-73's model 0.825 / duplication = {0.825 / (rows / blocks):.3f}")


def figure(records, name, exp='73'):
    import plotting
    from plotting import COLORS, plt
    plotting.style()
    scales = sorted({r['scale'] for r in records})
    fig, ax = plt.subplots(figsize=(7.6, 4.2))
    parts = (('server_s', 'A: the server (json_agg per summary)', COLORS['purple']),
             ('transfer_s', "A: psql's transfer and json.loads", COLORS['orange']),
             ('python_s', 'A: tuples and block_plan', COLORS['red']))
    for i, s in enumerate(scales):
        bottom = 0.0
        for key, label, color in parts:
            value = med(records, s, 'A', key)
            ax.bar(i * 3, value, 0.8, bottom=bottom, color=color, label=label if i == 0 else None)
            bottom += value
        ax.annotate(f'{bottom:.2f} s', (i * 3, bottom), textcoords='offset points', xytext=(0, 3), ha='center',
                    fontsize=8.5)
        b = med(records, s, 'B', 'seconds')
        ax.bar(i * 3 + 1, b, 0.8, color=COLORS['blue'], label='B: one query, grouped per relation fork' if i == 0 else None)
        ax.annotate(f'{b:.2f} s', (i * 3 + 1, b), textcoords='offset points', xytext=(0, 3), ha='center', fontsize=8.5)
    rows = {s: next(r['rows'] for r in records if r['scale'] == s and r['method'] == 'A') for s in scales}
    ax.set_xticks([i * 3 + 0.5 for i in range(len(scales))],
                  [f'pgbench scale {s}\n({rows[s]:,} summary rows)' for s in scales])
    ax.set_ylabel('seconds to read the summaries into a plan (median of 3)')
    ax.legend(fontsize=8, loc='upper left')
    ax.set_title(f"EXP-{exp}: the guard's summary reading, today's (A) against grouped on the server (B)", fontsize=10)
    print(plotting.save(fig, name))


if __name__ == '__main__':
    main()
