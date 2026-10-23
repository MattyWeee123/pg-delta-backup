"""Logical state of a cluster: databases, and per user table a row count and an md5.

The md5 is taken over the md5 of every row's text form, in primary-key order
(or ordered by the whole row's text when a table has no primary key), with the
session settings that affect text output pinned. Index scans are off, so every
table is read from its heap: through an index, the count and the md5 would miss
heap tuples that no index entry points to. Unlogged and temporary tables are
listed separately and not hashed, because a restored backup resets them.

``compute_logical_state`` takes a ``query(sql, db) -> rows`` callable so it can
run against any server (or a fake in tests).
"""
from __future__ import annotations

SESSION_SETUP = """SET extra_float_digits = 1;
SET DateStyle = 'ISO, YMD';
SET IntervalStyle = 'postgres';
SET TimeZone = 'UTC';
SET bytea_output = 'hex';
SET statement_timeout = 0;
SET enable_indexscan = off;
SET enable_indexonlyscan = off;
SET enable_bitmapscan = off;
"""

DATABASES_SQL = 'SELECT datname, datallowconn FROM pg_database ORDER BY datname;'

TABLES_SQL = r"""
SELECT format('%I.%I', n.nspname, c.relname),
       c.relpersistence,
       coalesce((SELECT string_agg(format('t.%I', a.attname), ', '
                                   ORDER BY array_position(i.indkey::int2[], a.attnum))
                 FROM pg_index i
                 JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
                 WHERE i.indrelid = c.oid AND i.indisprimary), '')
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'm') AND c.relispopulated
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg\_toast%' AND n.nspname NOT LIKE 'pg\_temp\_%'
ORDER BY 1;
"""


def sql_literal(text):
    return "'" + text.replace("'", "''") + "'"


def table_hash_sql(qualified_name, order_by):
    order = order_by or '(t::text) COLLATE "C"'
    return (f"SELECT {sql_literal(qualified_name)}, count(*), "
            f"coalesce(md5(string_agg(md5(t::text), '' ORDER BY {order})), md5('')) "
            f"FROM {qualified_name} t;")


def compute_logical_state(query):
    """Return {'databases': [...], 'tables': {db: {table: {rows, md5}}}, 'skipped': {...}}."""
    databases = query(DATABASES_SQL, 'postgres')
    state = {'databases': sorted(name for name, _ in databases), 'tables': {},
             'not_hashed': {}}
    for name, allow in sorted(databases):
        if allow != 't':
            state['not_hashed'][name] = 'database does not allow connections'
            continue
        tables = query(TABLES_SQL, name)
        hashed = [(table, order) for table, persistence, order in tables if persistence == 'p']
        skipped = [table for table, persistence, _ in tables if persistence != 'p']
        result = {}
        if hashed:
            script = SESSION_SETUP + '\n'.join(table_hash_sql(t, o) for t, o in hashed)
            for table, rows, digest in query(script, name):
                result[table] = {'rows': int(rows), 'md5': digest}
        state['tables'][name] = result
        if skipped:
            state['not_hashed'][name] = {'unlogged_or_temporary': skipped}
    return state


def compare_logical_states(expected, actual):
    """List of human-readable differences; empty means the states match."""
    diffs = []
    want_dbs, got_dbs = set(expected.get('databases', [])), set(actual.get('databases', []))
    for db in sorted(want_dbs - got_dbs):
        diffs.append(f'database {db} missing')
    for db in sorted(got_dbs - want_dbs):
        diffs.append(f'unexpected database {db}')
    want_tables, got_tables = expected.get('tables', {}), actual.get('tables', {})
    for db in sorted(set(want_tables) | set(got_tables)):
        if db in want_dbs ^ got_dbs:
            continue  # already reported as a missing or unexpected database
        if db not in got_tables:
            diffs.append(f'{db}: tables expected but not read')
            continue
        if db not in want_tables:
            diffs.append(f'{db}: tables read but not in the expected state')
            continue
        want, got = want_tables[db], got_tables[db]
        for table in sorted(set(want) - set(got)):
            diffs.append(f'{db}.{table}: table missing')
        for table in sorted(set(got) - set(want)):
            diffs.append(f'{db}.{table}: unexpected table')
        for table in sorted(set(want) & set(got)):
            if want[table]['rows'] != got[table]['rows']:
                diffs.append(f"{db}.{table}: {got[table]['rows']} rows, expected "
                             f"{want[table]['rows']}")
            elif want[table]['md5'] != got[table]['md5']:
                diffs.append(f"{db}.{table}: same row count but contents differ")
    return diffs
