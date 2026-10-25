"""The bank load of EXP-25 (after EXP-12's) and the checks of a copy recovered from a live refresh.

The load runs in database `bank` (pgbench -i, 100,000 accounts per scale unit, all balances 0), with:
- load_progress: one row per pgbench client, its count of committed transactions `n` and `ts`, the
  clock_timestamp() of the last one;
- load_ledger: one row per committed transaction, (client, n, ts), the same n and ts.

Each committed transaction (weight 99) moves d between two random accounts and adds 1 to the sum of balances, adds
1 to its client's n and appends its ledger row, taking its locks in one order (lower account, higher account, its
own client row), so no two deadlock. Each rolled-back one (weight 1) adds 10^9 to an account and rolls back. So in
any consistent state:

- sum(abalance) = sum(n), and no balance holds a rolled-back 10^9;
- each client's ledger holds its rows n_min..n with no gap, and its last row's ts is load_progress.ts.

The checks of a copy D recovered from a refresh whose pg_backup_start was asked for at T_start and whose
pg_backup_stop returned at T_stop (wall clock, the host's), against the live S's ledger:

- every transaction committed before T_start is in D. A client's transaction k+1 records its ts after transaction k
  committed (one client runs one transaction at a time), so if S's ledger has (c, k+1) with ts < T_start, D's n for
  c must be at least k;
- no transaction that began after T_stop is in D: its ts would be later, so D's last ts for each client must be
  before T_stop;
- D's (c, n) rows are S's: their ts equals S's ledger row (c, n) (a copy that kept B's own transactions, or
  mixed two histories, has other timestamps).

Both bounds hold for any correct copy whatever its recovery end point; they say nothing about which transactions
between T_start and T_stop it holds. Timestamps are integers of microseconds since the epoch.
"""
from __future__ import annotations

from pathlib import Path

DATABASE = 'bank'
ACCOUNTS_PER_SCALE = 100_000
ROLLED_BACK_DELTA = 1_000_000_000

COMMIT_SQL = """-- EXP-25: the committed transaction. +1 to sum(abalance), +1 to the client's n, one ledger row.
\\set a1 random(1, :naccounts)
\\set a2 random(1, :naccounts)
\\set lo least(:a1, :a2)
\\set hi greatest(:a1, :a2)
\\set d random(2, 5000)
BEGIN;
UPDATE pgbench_accounts SET abalance = abalance + :d WHERE aid = :lo;
UPDATE pgbench_accounts SET abalance = abalance - :d + 1 WHERE aid = :hi;
UPDATE load_progress SET n = n + 1, ts = clock_timestamp() WHERE client = :client_id;
INSERT INTO load_ledger (client, n, ts) SELECT client, n, ts FROM load_progress WHERE client = :client_id;
END;
"""

ABORT_SQL = """-- EXP-25: the rolled-back transaction. Its 10^9 must never reach a copy.
\\set a random(1, :naccounts)
BEGIN;
UPDATE pgbench_accounts SET abalance = abalance + 1000000000 WHERE aid = :a;
ROLLBACK;
"""


def setup_statements(clients):
    """After pgbench -i in `bank`: the progress and ledger tables, and amcheck so every copy holds it."""
    return [
        'CREATE TABLE load_progress (client int PRIMARY KEY, n bigint NOT NULL DEFAULT 0, ts timestamptz)',
        f'INSERT INTO load_progress (client) SELECT generate_series(0, {clients - 1})',
        'CREATE TABLE load_ledger (client int NOT NULL, n bigint NOT NULL, ts timestamptz NOT NULL, '
        'PRIMARY KEY (client, n))',
        'CREATE EXTENSION IF NOT EXISTS amcheck',
    ]


def write_scripts(directory):
    """commit.sql and abort.sql in `directory`; returns pgbench's -f arguments with their weights."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'commit.sql').write_text(COMMIT_SQL)
    (directory / 'abort.sql').write_text(ABORT_SQL)
    return ['-f', f'{directory / "commit.sql"}@99', '-f', f'{directory / "abort.sql"}@1']


def pgbench_argv(pgbench, scripts, *, clients, scale, seconds, seed, host, port, user, rate=None):
    """A closed-loop (or, with rate, open-loop) run of the load for `seconds`."""
    argv = [str(pgbench), '-n', '-c', str(clients), '-j', str(min(clients, 4)), '-T', str(seconds),
            '--random-seed', str(seed), '-D', f'naccounts={scale * ACCOUNTS_PER_SCALE}', *scripts]
    if rate:
        argv += ['-R', str(rate)]
    return argv + ['-h', str(host), '-p', str(port), '-U', user, DATABASE]


def us(seconds):
    """Wall-clock seconds (time.time()) as integer microseconds."""
    return int(round(seconds * 1_000_000))


_TS = '(extract(epoch FROM ts) * 1000000)::bigint'

COPY_STATE_SQL = f"""SELECT json_build_object(
 'accounts', (SELECT count(*) FROM pgbench_accounts),
 'sum_balance', (SELECT coalesce(sum(abalance), 0) FROM pgbench_accounts),
 'max_balance', (SELECT coalesce(max(abalance), 0) FROM pgbench_accounts),
 'sum_n', (SELECT coalesce(sum(n), 0) FROM load_progress),
 'progress', (SELECT coalesce(json_agg(json_build_object('client', client, 'n', n, 'ts', {_TS}) ORDER BY client),
                               '[]') FROM load_progress),
 'ledger', (SELECT coalesce(json_agg(json_build_object('client', client, 'min', lo, 'max', hi, 'count', k)
                                     ORDER BY client), '[]')
            FROM (SELECT client, min(n) AS lo, max(n) AS hi, count(*) AS k FROM load_ledger GROUP BY client) x),
 'last', (SELECT coalesce(json_object_agg(l.client, (extract(epoch FROM l.ts) * 1000000)::bigint), '{{}}')
          FROM load_ledger l JOIN load_progress p ON p.client = l.client AND p.n = l.n))"""


def source_reference_sql(start_us, pairs):
    """What S's ledger says for one copy: each client's n that must be present (from T_start), its first kept n,
    and the ts of the rows (client, n) the copy ended at."""
    values = ', '.join(f'({int(client)}, {int(n)})' for client, n in pairs) or '(-1, -1)'
    return f"""SELECT json_build_object(
 'required', (SELECT coalesce(json_object_agg(client, m - 1), '{{}}')
              FROM (SELECT client, max(n) AS m FROM load_ledger WHERE {_TS} < {int(start_us)} GROUP BY client) x),
 'first', (SELECT coalesce(json_object_agg(client, lo), '{{}}')
           FROM (SELECT client, min(n) AS lo FROM load_ledger GROUP BY client) y),
 'ts', (SELECT coalesce(json_object_agg(l.client, (extract(epoch FROM l.ts) * 1000000)::bigint), '{{}}')
        FROM load_ledger l JOIN (VALUES {values}) AS v(client, n) ON v.client = l.client AND v.n = l.n))"""


def judge(copy, reference, start_us, stop_us, clients, accounts):
    """The problems found in a recovered copy (empty: it passes). `copy` from COPY_STATE_SQL on the copy,
    `reference` from source_reference_sql on S, start_us and stop_us the bounds' wall-clock times."""
    problems = []
    if copy['accounts'] != accounts:
        problems.append(f'{copy["accounts"]} accounts, not {accounts}')
    if copy['sum_balance'] != copy['sum_n']:
        problems.append(f'invariant broken: sum(abalance) {copy["sum_balance"]} != sum(n) {copy["sum_n"]}')
    if copy['max_balance'] >= ROLLED_BACK_DELTA // 10:
        problems.append(f'a rolled-back change is present (max abalance {copy["max_balance"]})')
    progress = {row['client']: row for row in copy['progress']}
    if sorted(progress) != list(range(clients)) or len(copy['progress']) != clients:
        problems.append(f'load_progress has {len(copy["progress"])} rows for clients {sorted(progress)[:8]}, '
                        f'not one for each of {clients}')
    ledger = {row['client']: row for row in copy['ledger']}
    required = {int(key): value for key, value in reference['required'].items()}
    first = {int(key): value for key, value in reference['first'].items()}
    source_ts = {int(key): value for key, value in reference['ts'].items()}
    copy_last = {int(key): value for key, value in copy['last'].items()}
    for client in range(clients):
        row = progress.get(client)
        if row is None:
            continue
        n, ts = row['n'], row['ts']
        if n < required.get(client, 0):
            problems.append(f'client {client}: n {n} < {required[client]}, committed before pg_backup_start')
        if n == 0:
            if client in ledger:
                problems.append(f'client {client}: n 0 but ledger rows')
            continue
        if ts is None or ts >= stop_us:
            problems.append(f'client {client}: its last transaction (n {n}, ts {ts}) began after pg_backup_stop '
                            f'returned ({stop_us})')
        if copy_last.get(client) != ts:
            problems.append(f'client {client}: ledger row {n} has ts {copy_last.get(client)}, progress {ts}')
        if source_ts.get(client) != ts:
            problems.append(f'client {client}: (n {n}, ts {ts}) is not S\'s row (S has ts {source_ts.get(client)})')
        entry = ledger.get(client)
        if entry is None or entry['max'] != n or entry['count'] != entry['max'] - entry['min'] + 1 \
                or entry['min'] != first.get(client, entry['min']):
            problems.append(f'client {client}: ledger {entry} is not rows {first.get(client)}..{n} without a gap')
    return problems


def summary(copy):
    """A short record of a copy's state for the trial log."""
    return {'sum_n': copy['sum_n'], 'sum_balance': copy['sum_balance'], 'max_balance': copy['max_balance'],
            'n': [row['n'] for row in copy['progress']]}
