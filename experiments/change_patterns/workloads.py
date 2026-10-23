"""Deterministic workload scripts with a known set of changed rows.

Row-level patterns become plain SQL statements, one row per statement, grouped
into BEGIN/COMMIT batches. tpcb is run by pgbench itself and is not generated.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import random
from typing import Iterable, Iterator

PATTERNS = ('uniform', 'hot', 'clustered', 'append', 'retention', 'tpcb')
ACCOUNT_PATTERNS = ('uniform', 'hot', 'clustered', 'tpcb')
EVENT_PATTERNS = ('append', 'retention')
ACCOUNTS_PER_SCALE = 100000  # pgbench_accounts rows per scale unit
EVENT_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)

EVENTS_DDL = """CREATE TABLE events(id bigserial PRIMARY KEY, ts timestamptz NOT NULL,
                    payload text NOT NULL);"""


def events_prefill_sql(rows: int) -> str:
    """Fill ids 1..rows with fixed timestamps and 64-character payloads."""
    return (f"INSERT INTO events(ts, payload) SELECT timestamptz '{EVENT_EPOCH.isoformat()}'"
            f" + i * interval '1 second', md5(i::text) || md5((-i)::text)"
            f" FROM generate_series(1, {int(rows)}) AS i;")


def rows_for_fraction(fraction: float, table_rows: int) -> int:
    if not 0 < fraction <= 1:
        raise ValueError(f'fraction must be in (0, 1], got {fraction}')
    rows = round(fraction * table_rows)
    if rows < 1:
        raise ValueError(f'fraction {fraction} of {table_rows} rows selects no rows')
    return rows


def hot_set_size(table_rows: int, hot_percent: float) -> int:
    if not 0 < hot_percent <= 100:
        raise ValueError(f'hot set percent must be in (0, 100], got {hot_percent}')
    return max(1, round(table_rows * hot_percent / 100))


def target_rows(pattern: str, fraction: float, *, accounts_rows: int, events_rows: int) -> int:
    """k: distinct rows to change (tpcb: transactions to run)."""
    if pattern not in PATTERNS:
        raise ValueError(f'unknown pattern {pattern!r}; choose from {", ".join(PATTERNS)}')
    return rows_for_fraction(fraction, accounts_rows if pattern in ACCOUNT_PATTERNS
                             else events_rows)


def select_aids(pattern: str, k: int, accounts_rows: int, seed: int,
                hot_percent: float = 1.0) -> list[int]:
    """Distinct pgbench_accounts.aid values (1-based) for an update pattern."""
    if not 1 <= k <= accounts_rows:
        raise ValueError(f'cannot change {k} distinct rows of {accounts_rows}')
    rng = random.Random(seed)
    if pattern == 'uniform':
        return rng.sample(range(1, accounts_rows + 1), k)
    if pattern == 'hot':
        hot_rows = hot_set_size(accounts_rows, hot_percent)
        if k > hot_rows:
            raise ValueError(f'hot pattern needs {k} distinct rows but the hot set '
                             f'(first {hot_percent}% of aid values) has only {hot_rows}; '
                             f'lower the fraction or raise --hot-percent')
        return rng.sample(range(1, hot_rows + 1), k)
    if pattern == 'clustered':
        start = rng.randint(1, accounts_rows - k + 1)
        return list(range(start, start + k))
    raise ValueError(f'{pattern!r} is not an update pattern')


def event_insert(event_id: int, rng: random.Random) -> str:
    stamp = (EVENT_EPOCH + timedelta(seconds=event_id)).isoformat()
    return (f"INSERT INTO events(ts, payload) VALUES "
            f"('{stamp}', '{rng.getrandbits(256):064x}');")


def workload_statements(pattern: str, k: int, *, accounts_rows: int, events_rows: int,
                        seed: int, hot_percent: float = 1.0,
                        first_event_id: int = 1) -> Iterator[str]:
    """SQL statements changing exactly k distinct rows (retention: k in, k out).

    New event ids continue the prefill (bigserial, so ids events_rows + 1 ...);
    retention deletes the k oldest ids, first_event_id onwards.
    """
    if pattern in ('uniform', 'hot', 'clustered'):
        for aid in select_aids(pattern, k, accounts_rows, seed, hot_percent):
            yield f'UPDATE pgbench_accounts SET abalance = abalance + 1 WHERE aid = {aid};'
        return
    if pattern not in EVENT_PATTERNS:
        raise ValueError(f'{pattern!r} has no generated SQL script')
    if pattern == 'retention' and k > events_rows:
        raise ValueError(f'retention cannot delete {k} of {events_rows} event rows')
    rng = random.Random(seed)
    for index in range(k):
        yield event_insert(first_event_id + events_rows + index, rng)
        if pattern == 'retention':
            yield f'DELETE FROM events WHERE id = {first_event_id + index};'


def batched(statements: Iterable[str], batch_size: int = 100) -> Iterator[str]:
    """Wrap statements in BEGIN/COMMIT, batch_size statements per transaction."""
    if batch_size < 1:
        raise ValueError('batch size must be at least 1')
    open_batch = 0
    for statement in statements:
        if open_batch == 0:
            yield 'BEGIN;'
        yield statement
        open_batch += 1
        if open_batch == batch_size:
            yield 'COMMIT;'
            open_batch = 0
    if open_batch:
        yield 'COMMIT;'


def write_script(path: Path, statements: Iterable[str], batch_size: int = 100) -> dict:
    """Write a psql script; return its statement, transaction and hash summary."""
    digest = hashlib.sha256()
    counts = {'statements': 0, 'transactions': 0}
    with open(path, 'w', encoding='utf-8') as stream:
        for line in batched(statements, batch_size):
            if line == 'BEGIN;':
                counts['transactions'] += 1
            elif line != 'COMMIT;':
                counts['statements'] += 1
            stream.write(line + '\n')
            digest.update(line.encode() + b'\n')
    return {**counts, 'batch_size': batch_size, 'sha256': digest.hexdigest(),
            'bytes': path.stat().st_size}
