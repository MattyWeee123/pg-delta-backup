"""Experiment 1: how many pages change when a known fraction of rows changes.

Each trial builds a disposable PostgreSQL cluster in a new temporary
directory, loads pgbench plus an events table, takes a full backup B0, applies
one deterministic workload, takes a full backup B1 and an incremental backup
I1 against B0, then compares B0 with B1 chunk by chunk and with what
PostgreSQL's own change tracker (WAL summaries, via I1) says changed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import itertools
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmarks.backup import atomic_json, basebackup_command
from experiments.change_patterns import compare_methods, pagediff, report, tracker, workloads

DEFAULT_BIN_DIR = '/opt/homebrew/opt/postgresql@17/bin'
TOOLS = ('postgres', 'initdb', 'pg_ctl', 'psql', 'pgbench', 'pg_basebackup', 'pg_walsummary',
         'pg_controldata')
USER, PORT, DATABASE = 'study', 5432, 'postgres'
REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = 'change-pattern-trial/1'
GIT_STATUS_LINES = 50
SERVER_CONFIG = {
    'listen_addresses': "''",
    'wal_level': 'replica',
    'max_wal_senders': '10',
    'summarize_wal': 'on',
}
VARIANT_FLAGS = {'checkpoint': 'checkpoint', 'vacuum': 'vacuum', 'read-scan': 'read_scan',
                 'no-checksums': 'no_checksums', 'no-load-vacuum': 'no_load_vacuum', 'hex-filler': 'hex_filler'}
# EXP-45: pgbench's blank filler compresses far better than real rows, so for a compression test the accounts'
# filler is overwritten with hex text after the load, as EXP-37 did (its 2.83x for zstd is on this data).
HEX_FILLER_SQL = ("UPDATE pgbench_accounts SET filler = md5(aid::text) || md5((aid::bigint * 7919)::text) || "
                  "substr(md5((aid::bigint * 31)::text), 1, 20);\nVACUUM FULL pgbench_accounts;")
USER_TABLES = ('pgbench_accounts', 'pgbench_branches', 'pgbench_tellers', 'pgbench_history',
               'events')

RELMAP_SQL = """SELECT coalesce(json_agg(json_build_object(
  'path', pg_relation_filepath(c.oid), 'oid', c.oid::bigint, 'schema', n.nspname,
  'relname', c.relname, 'relkind', c.relkind, 'persistence', c.relpersistence,
  'filenode', pg_relation_filenode(c.oid)::bigint,
  'parent', pn.nspname || '.' || p.relname)), '[]')::text
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_index i ON i.indexrelid = c.oid
LEFT JOIN pg_class p ON p.oid = coalesce(i.indrelid,
  (SELECT t.oid FROM pg_class t WHERE t.reltoastrelid = c.oid))
LEFT JOIN pg_namespace pn ON pn.oid = p.relnamespace
WHERE pg_relation_filepath(c.oid) IS NOT NULL;"""

SIZES_SQL = """SELECT coalesce(json_object_agg(c.relname, json_build_object(
  'kind', CASE WHEN c.relkind = 'i' THEN 'index' ELSE 'table' END,
  'main_bytes', pg_relation_size(c.oid), 'fsm_bytes', pg_relation_size(c.oid, 'fsm'),
  'vm_bytes', pg_relation_size(c.oid, 'vm'),
  'index_bytes', CASE WHEN c.relkind = 'r' THEN pg_indexes_size(c.oid) END,
  'toast_bytes', CASE WHEN c.relkind = 'r'
                      THEN coalesce(pg_total_relation_size(nullif(c.reltoastrelid, 0)), 0) END,
  'relpages', c.relpages, 'reltuples', c.reltuples)), '{}')::text
FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'i');"""

STATS_SQL = """SELECT coalesce(json_object_agg(relname, json_build_object(
  'n_tup_ins', n_tup_ins, 'n_tup_upd', n_tup_upd, 'n_tup_hot_upd', n_tup_hot_upd,
  'n_tup_del', n_tup_del, 'n_live_tup', n_live_tup, 'n_dead_tup', n_dead_tup,
  'vacuum_count', vacuum_count, 'autovacuum_count', autovacuum_count,
  'analyze_count', analyze_count, 'autoanalyze_count', autoanalyze_count,
  'last_autovacuum', last_autovacuum, 'last_autoanalyze', last_autoanalyze)), '{}')::text
FROM pg_stat_user_tables;"""

SETTINGS_SQL = """SELECT json_object_agg(name, json_build_object(
  'setting', setting, 'unit', unit, 'source', source))::text
FROM pg_settings WHERE source NOT IN ('default', 'override') OR name IN (
  'block_size', 'segment_size', 'wal_segment_size', 'data_checksums', 'server_version',
  'shared_buffers', 'autovacuum', 'checkpoint_timeout', 'max_wal_size',
  'wal_summary_keep_time', 'full_page_writes', 'wal_log_hints');"""


def controlled_env() -> dict:
    # Drop the caller's libpq settings; macOS postmasters need a valid LC_ALL.
    env = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
    env['LC_ALL'] = 'C'
    return env


def preflight(bin_dir: Path) -> tuple[dict[str, str], str, int]:
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        raise SystemExit('Run as an unprivileged user; initdb refuses root')
    tools, versions = {}, {}
    for name in TOOLS:
        path = bin_dir / name
        if not path.is_file():
            raise SystemExit(f'Missing PostgreSQL binary: {path}')
        tools[name] = str(path)
        output = subprocess.check_output([str(path), '--version'], text=True,
                                         env=controlled_env())
        match = re.search(r'\(PostgreSQL\) ([0-9]+)(\.[0-9]+)?', output)
        if not match:
            raise SystemExit(f'Cannot read the version of {path}: {output.strip()}')
        versions[name] = match.group(1) + (match.group(2) or '')
    if len(set(versions.values())) != 1:
        raise SystemExit(f'PostgreSQL binaries disagree on version: {versions}')
    version = versions['postgres']
    major = int(version.split('.')[0])
    if major < 17:
        raise SystemExit(f'Incremental backup needs PostgreSQL 17 or later, found {version}')
    return tools, version, major


def git_provenance(root: Path | None = None) -> dict:
    """HEAD and whether the working tree differs from it (None if unknown).

    git_status holds up to GIT_STATUS_LINES lines of `git status --porcelain`.
    git_diff_sha256 is a short SHA-256 of that status plus `git diff HEAD
    --binary`, so two dirty runs with different edits get different hashes;
    untracked files count by name only. It is None for a clean tree.
    """
    root = REPO_ROOT if root is None else root
    result = {'git_commit': None, 'git_dirty': None, 'git_status': None,
              'git_diff_sha256': None}

    def git(*args) -> bytes:
        # --no-optional-locks: do not rewrite the index of the tree we describe.
        return subprocess.run(['git', '--no-optional-locks', '-C', str(root), *args],
                              capture_output=True, timeout=30, check=True).stdout
    try:
        result['git_commit'] = git('rev-parse', 'HEAD').decode().strip()
        status = git('status', '--porcelain=v1', '--untracked-files=all')
        lines = status.decode('utf-8', errors='replace').splitlines()
        result['git_dirty'] = bool(lines)
        result['git_status'] = lines[:GIT_STATUS_LINES] + (
            [f'... {len(lines) - GIT_STATUS_LINES} more'] if len(lines) > GIT_STATUS_LINES else [])
        if lines:
            digest = hashlib.sha256(status + b'\0')
            digest.update(git('diff', 'HEAD', '--binary'))
            result['git_diff_sha256'] = digest.hexdigest()[:12]
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def backup_label_lsn(backup: Path) -> int:
    text = (backup / 'backup_label').read_text()
    match = re.search(r'^START WAL LOCATION: ([0-9A-F]+/[0-9A-F]+)', text, re.MULTILINE)
    if not match:
        raise RuntimeError(f'No START WAL LOCATION in {backup / "backup_label"}')
    return tracker.parse_lsn(match.group(1))


def format_lsn(lsn: int) -> str:
    return f'{lsn >> 32:X}/{lsn & 0xFFFFFFFF:X}'


class Trial:
    def __init__(self, spec: dict, tools: dict, major: int, root: Path, log_dir: Path):
        self.spec, self.tools, self.major = spec, tools, major
        self.root, self.log_dir = root, log_dir
        self.data, self.socket = root / 'data', root / 's'  # short: socket path limit
        self.b0, self.b1, self.i1 = root / 'B0', root / 'B1', root / 'I1'
        self.env = controlled_env()
        self.commands: list[dict] = []
        self.timings: dict[str, float] = {}
        self.running = False
        log_dir.mkdir(parents=True)

    def log(self, message: str) -> None:
        print(f"[{self.spec['trial_id']}] {message}", file=sys.stderr, flush=True)

    def run(self, name: str, args, *, label: str | None = None, stdin: str | None = None,
            timeout: float = 3600, check: bool = True) -> str:
        stem = self.log_dir / f'{len(self.commands) + 1:02d}-{label or name}'
        argv = [self.tools.get(name, name), *map(str, args)]
        started = time.perf_counter()
        returncode = error = None
        with open(stem.with_suffix('.out'), 'w') as out, open(stem.with_suffix('.err'), 'w') as err:
            try:
                returncode = subprocess.run(argv, input=stdin, stdout=out, stderr=err, text=True,
                                            timeout=timeout, env=self.env,
                                            start_new_session=True).returncode
            except subprocess.TimeoutExpired:
                error = f'timed out after {timeout} seconds'
        self.commands.append({'label': label or name, 'argv': argv, 'returncode': returncode,
                              'error': error, 'seconds': round(time.perf_counter() - started, 3)})
        if check and (returncode != 0 or error):
            detail = stem.with_suffix('.err').read_text()[-2000:]
            raise RuntimeError(f'{label or name} failed ({error or returncode}): {detail}')
        return stem.with_suffix('.out').read_text()

    def sql(self, statement: str, *, label: str = 'psql', db: str = DATABASE) -> str:
        return self.run('psql', ['-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1',
                                 '-h', self.socket, '-p', PORT, '-U', USER, '-d', db],
                        label=label, stdin=statement).strip()

    def sql_json(self, statement: str, *, label: str, db: str = DATABASE):
        return json.loads(self.sql(statement, label=label, db=db))

    def phase(self, name: str, started: float) -> None:
        self.timings[name] = round(time.perf_counter() - started, 3)

    # Cluster lifecycle ---------------------------------------------------

    def init_cluster(self) -> list[str]:
        args = ['-D', self.data, '-U', USER, '--auth-local=trust', '--auth-host=reject',
                '-E', 'UTF8', '--locale=C']
        if self.spec['data_checksums']:
            args.append('--data-checksums')
        elif self.major >= 18:
            args.append('--no-data-checksums')  # on by default from PostgreSQL 18
        self.run('initdb', args, timeout=300)
        lines = [f'{key} = {value}' for key, value in SERVER_CONFIG.items()]
        with open(self.data / 'postgresql.conf', 'a') as stream:
            stream.write('\n# change-pattern study\n' + '\n'.join(lines) + '\n')
        return lines

    def start(self) -> None:
        if len(str(self.socket / f'.s.PGSQL.{PORT}')) > 100:  # sun_path is 104 bytes on macOS
            raise RuntimeError(f'Socket path too long; use a shorter --work-dir: {self.socket}')
        self.socket.mkdir()
        self.running = True  # also attempt a stop if startup half-succeeds
        self.run('pg_ctl', ['-D', self.data, '-l', self.log_dir / 'server.log', '-o',
                            shlex.join(['-k', str(self.socket), '-p', str(PORT)]),
                            '-w', '-t', '120', 'start'], label='pg_ctl-start')
        actual = self.sql('SHOW data_directory;', label='check-cluster')
        if Path(actual).resolve() != self.data.resolve():
            raise RuntimeError(f'Connected to the wrong cluster: {actual}')

    def stop(self) -> None:
        if not self.running:
            return
        self.run('pg_ctl', ['-D', self.data, '-m', 'fast', '-w', '-t', '120', 'stop'],
                 label='pg_ctl-stop', check=False)
        status = self.run('pg_ctl', ['-D', self.data, 'status'], label='pg_ctl-status',
                          check=False)
        if self.commands[-1]['returncode'] != 3:
            self.run('pg_ctl', ['-D', self.data, '-m', 'immediate', '-w', '-t', '30', 'stop'],
                     label='pg_ctl-stop-immediate', check=False)
            self.run('pg_ctl', ['-D', self.data, 'status'], label='pg_ctl-status', check=False)
            if self.commands[-1]['returncode'] != 3:
                raise RuntimeError(f'Could not stop the trial cluster: {status.strip()}')
        self.running = False

    def backup(self, output: Path, basis: Path | None = None) -> None:
        argv = basebackup_command(self.tools['pg_basebackup'], output, host=self.socket,
                                  port=PORT, user=USER, basis=basis)
        self.run('pg_basebackup', argv[1:], label='pg_basebackup-' + output.name)

    # Measurements --------------------------------------------------------

    def databases(self) -> list[dict]:
        return self.sql_json("""SELECT json_agg(json_build_object('oid', oid::bigint,
  'name', datname, 'allow_connections', datallowconn) ORDER BY oid)::text FROM pg_database;""",
                             label='databases')

    def relation_map(self, databases: list[dict]) -> dict[str, dict]:
        """Main-fork path -> relation identity, for every connectable database.

        template0 refuses connections; initdb creates it as a file copy of
        template1, so template1's mapping is reused for it and marked as such.
        """
        relmap: dict[str, dict] = {}
        study_oid = next(d['oid'] for d in databases if d['name'] == DATABASE)
        template1 = next((d for d in databases if d['name'] == 'template1'), None)
        per_db: dict[int, list[dict]] = {}
        for database in databases:
            if database['allow_connections']:
                per_db[database['oid']] = self.sql_json(RELMAP_SQL, label='relmap',
                                                        db=database['name'])
        for database in databases:
            rows, source = per_db.get(database['oid']), 'catalog query'
            if rows is None and template1 and template1['oid'] in per_db:
                prefix = f"base/{template1['oid']}/"
                rows = [{**row, 'path': f"base/{database['oid']}/" + row['path'][len(prefix):]}
                        for row in per_db[template1['oid']] if row['path'].startswith(prefix)]
                source = 'copied from template1 (database refuses connections)'
            for row in rows or ():
                name = f"{row['schema']}.{row['relname']}"
                if row['path'].startswith('base/') and database['oid'] != study_oid:
                    name = f"{database['name']}:{name}"
                relmap.setdefault(row['path'], {
                    'oid': row['oid'], 'name': name, 'relkind': row['relkind'],
                    'persistence': row['persistence'], 'filenode': row['filenode'],
                    'parent': row['parent'], 'database_oid': database['oid'],
                    'mapping_source': source})
        return relmap

    def table_state(self, label: str, *, scan: bool = True) -> dict:
        if scan:
            counts = self.sql_json(
                'SELECT json_build_object(' + ', '.join(
                    f"'{table}', (SELECT count(*) FROM {table})" for table in USER_TABLES)
                + ')::text;', label=label + '-counts')
            source = 'count(*)'
        else:
            scale = self.spec['scale']
            counts = {'pgbench_accounts': self.spec['accounts_rows'], 'pgbench_branches': scale,
                      'pgbench_tellers': 10 * scale, 'pgbench_history': 0,
                      'events': self.spec['events_rows']}
            source = 'expected from the load (not scanned, to keep hint bits unset)'
        return {'row_counts': counts, 'row_counts_source': source,
                'relations': self.sql_json(SIZES_SQL, label=label + '-sizes')}

    def load(self) -> dict:
        scale = self.spec['scale']
        self.run('pgbench', ['-i', '-q', '-s', scale, '-h', self.socket, '-p', PORT, '-U', USER,
                             DATABASE], label='pgbench-init', timeout=3600)
        if self.spec.get('hex_filler'):
            self.sql(HEX_FILLER_SQL, label='hex-filler')
        self.sql(workloads.EVENTS_DDL + '\n' + workloads.events_prefill_sql(self.spec['events_rows']),
                 label='events-prefill')
        if self.spec['load_vacuum']:
            self.sql('VACUUM ANALYZE;', label='vacuum-analyze')
            # Counting after VACUUM sets no new hint bits; the CHECKPOINT then
            # flushes everything so B0 starts from a quiet, clean cluster.
            state = self.table_state('before')
        else:
            # No VACUUM, ANALYZE or scan: events tuples keep unset hint bits in B0.
            state = self.table_state('before', scan=False)
        self.sql('CHECKPOINT;', label='checkpoint-after-load')
        return state

    def apply_workload(self, k: int) -> dict:
        spec = self.spec
        if spec['pattern'] == 'tpcb':
            output = self.run('pgbench', ['-n', '-b', 'tpcb-like', '-t', k, '-c', '1', '-j', '1',
                                          f"--random-seed={spec['seed']}", '-h', self.socket,
                                          '-p', PORT, '-U', USER, DATABASE], label='pgbench-tpcb')
            return {'driver': 'pgbench -b tpcb-like', 'transactions': k,
                    'pgbench_summary': [line for line in output.splitlines()
                                        if line.startswith(('number of transactions', 'tps',
                                                            'latency', 'initial connection'))]}
        script = self.root / 'workload.sql'
        statements = workloads.workload_statements(
            spec['pattern'], k, accounts_rows=spec['accounts_rows'],
            events_rows=spec['events_rows'], seed=spec['seed'], hot_percent=spec['hot_percent'])
        summary = workloads.write_script(script, statements, spec['batch_size'])
        self.run('psql', ['-X', '-q', '-v', 'ON_ERROR_STOP=1', '-h', self.socket, '-p', PORT,
                          '-U', USER, '-d', DATABASE, '-f', script], label='workload')
        return {'driver': 'psql script', 'script': summary}

    def post_steps(self) -> list[str]:
        done = []
        if self.spec['read_scan']:
            # Force heap reads so hint bits can be set; index-only scans skip the heap.
            self.sql('SET enable_indexonlyscan = off; SET enable_indexscan = off;\n'
                     'SET enable_bitmapscan = off;\n'
                     + '\n'.join(f'SELECT count(*) FROM {t};' for t in USER_TABLES),
                     label='read-scan')
            done.append('read_scan')
        if self.spec['vacuum']:
            self.sql('VACUUM;', label='vacuum')
            done.append('vacuum')
        if self.spec['checkpoint']:
            self.sql('CHECKPOINT;', label='checkpoint')
            done.append('checkpoint')
        return done

    def verify_rows(self, k: int) -> dict:
        """Check the row-level ground truth against the database after both backups."""
        spec = self.spec
        pattern, n_events = spec['pattern'], spec['events_rows']
        if pattern in ('uniform', 'hot', 'clustered'):
            aids = workloads.select_aids(pattern, k, spec['accounts_rows'], spec['seed'],
                                         spec['hot_percent'])
            found = self.sql_json("""SELECT json_build_object('changed', count(*),
  'min_aid', min(aid), 'max_aid', max(aid), 'balance_sum', sum(abalance))::text
FROM pgbench_accounts WHERE abalance <> 0;""", label='verify')
            expected = {'changed': k, 'min_aid': min(aids), 'max_aid': max(aids),
                        'balance_sum': k}
            truth = {'rows_changed': k, 'rows_updated': k, 'rows_inserted': 0, 'rows_deleted': 0}
        elif pattern == 'append':
            found = self.sql_json("""SELECT json_build_object('rows', count(*),
  'min_id', min(id), 'max_id', max(id))::text FROM events;""", label='verify')
            expected = {'rows': n_events + k, 'min_id': 1, 'max_id': n_events + k}
            truth = {'rows_changed': k, 'rows_updated': 0, 'rows_inserted': k, 'rows_deleted': 0}
        elif pattern == 'retention':
            found = self.sql_json("""SELECT json_build_object('rows', count(*),
  'min_id', min(id), 'max_id', max(id))::text FROM events;""", label='verify')
            expected = {'rows': n_events, 'min_id': k + 1, 'max_id': n_events + k}
            truth = {'rows_changed': 2 * k, 'rows_updated': 0, 'rows_inserted': k,
                     'rows_deleted': k}
        else:
            found = self.sql_json("""SELECT json_build_object('history_rows', count(*),
  'distinct_aids', count(DISTINCT aid), 'distinct_tids', count(DISTINCT tid),
  'distinct_bids', count(DISTINCT bid))::text FROM pgbench_history;""", label='verify')
            expected = {'history_rows': k}
            truth = {'rows_changed': found['distinct_aids'], 'tpcb_transactions': k,
                     'rows_updated_accounts_distinct': found['distinct_aids'],
                     'tellers_touched': found['distinct_tids'],
                     'branches_touched': found['distinct_bids'], 'rows_inserted_history': k,
                     'note': 'rows_changed counts distinct pgbench_accounts rows; '
                             'tellers, branches and history also change'}
        mismatched = {key: (value, found.get(key)) for key, value in expected.items()
                      if found.get(key) != value}
        if mismatched:
            raise RuntimeError(f'Row-level ground truth check failed (expected, found): {mismatched}')
        return {**truth, 'verification': {'expected': expected, 'found': found}}

    def walsummary_view(self, b0_start: int, i1_start: int) -> dict:
        summaries = self.data / 'pg_wal' / 'summaries'
        selected, ignored = [], []
        for path in sorted(summaries.iterdir()) if summaries.is_dir() else ():
            # The summarizer writes temp.summary before renaming it; skip that
            # and anything else that is not a regular, properly named summary.
            if not tracker.SUMMARY_NAME.fullmatch(path.name) or path.is_symlink() \
                    or not path.is_file():
                ignored.append(path.name)
                continue
            tli, start, end = tracker.summary_file_range(path.name)
            if start < i1_start and end > b0_start:
                selected.append(path)
        if not selected:
            return {'files': 0, 'ignored_files': ignored,
                    'note': 'no live WAL summary files cover B0..I1'}
        output = self.run('pg_walsummary', selected, label='pg_walsummary')
        blocks, limits = tracker.parse_walsummary(output)
        by_file, skipped = tracker.summary_blocks_by_file(blocks)
        return {
            'files': len(selected),
            'ignored_files': ignored,
            'source': 'live cluster pg_wal/summaries (backups carry an empty directory)',
            'lsn_range': [format_lsn(b0_start), format_lsn(i1_start)],
            'range_note': 'whole summary files overlapping the range, so edges may include '
                          'a little WAL from just before B0 or after I1 started',
            'relation_forks': len(blocks),
            'blocks': sum(len(v) for v in blocks.values()),
            'limit_entries': len(limits),
            'blocks_in_other_tablespaces': skipped,
            'by_file': by_file,
        }

    def execute(self) -> dict:
        spec = self.spec
        result: dict = {}
        started = time.perf_counter()
        result['config'] = {'appended_postgresql_conf': self.init_cluster(),
                            'initdb': {'locale': 'C', 'encoding': 'UTF8',
                                       'data_checksums': spec['data_checksums']}}
        self.start()
        self.phase('initdb_start', started)

        started = time.perf_counter()
        self.log('loading dataset')
        before = self.load()
        databases = self.databases()
        relmap = self.relation_map(databases)
        self.phase('load', started)

        started = time.perf_counter()
        self.backup(self.b0)
        self.phase('backup_b0', started)

        k = workloads.target_rows(spec['pattern'], spec['fraction'],
                                  accounts_rows=spec['accounts_rows'],
                                  events_rows=spec['events_rows'])
        stats = {'before_workload': self.sql_json(STATS_SQL, label='stats')}
        started = time.perf_counter()
        self.log(f"workload {spec['pattern']}: k={k}")
        workload = self.apply_workload(k)
        workload['post_steps'] = self.post_steps()
        self.phase('workload', started)

        stats['before_b1'] = self.sql_json(STATS_SQL, label='stats')
        started = time.perf_counter()
        # B1 before I1: anything that changes in between shows up as
        # tracked-but-unchanged (harmless) rather than changed-but-not-tracked.
        self.backup(self.b1)
        self.phase('backup_b1', started)
        started = time.perf_counter()
        self.backup(self.i1, basis=self.b0)
        self.phase('backup_i1', started)
        stats['after_i1'] = self.sql_json(STATS_SQL, label='stats')

        started = time.perf_counter()
        after = self.table_state('after')
        truth = self.verify_rows(k)
        relmap_after = self.relation_map(databases)
        for path, info in relmap_after.items():
            relmap.setdefault(path, info)
        result['config']['settings'] = self.sql_json(SETTINGS_SQL, label='settings')
        system_id = self.sql('SELECT system_identifier FROM pg_control_system();',
                             label='system-identifier')
        self.stop()
        self.phase('verify_and_stop', started)

        started = time.perf_counter()
        self.log('comparing B0 with B1')
        diffs, skipped = pagediff.compare_trees(self.b0, self.b1)
        content = pagediff.summarize(diffs, relmap)
        content['skipped_non_regular'] = skipped
        self.phase('compare_content', started)

        started = time.perf_counter()
        tracked = tracker.scan_incremental_backup(self.i1)
        tracker_view = tracker.summarize_tracked(tracked, relmap)
        b0_start, i1_start = backup_label_lsn(self.b0), backup_label_lsn(self.i1)
        summary = self.walsummary_view(b0_start, i1_start)
        agreement = {'incremental_backup': tracker.agreement(
            diffs, tracker.tracked_sets_from_backup(tracked), relmap)}
        if summary.get('files'):
            by_file = summary.pop('by_file')
            agreement['wal_summaries'] = tracker.agreement(
                diffs, {path: ('summary', blocks) for path, blocks in by_file.items()}, relmap)
        tracker_view['wal_summaries'] = summary
        self.phase('tracker_view', started)

        result.update({
            'system_identifier': system_id,
            'databases': databases,
            'backups': {name: {'start_lsn': format_lsn(backup_label_lsn(path)),
                               'bytes': sum(f.stat().st_size for f in path.rglob('*')
                                            if f.is_file())}
                        for name, path in (('B0', self.b0), ('B1', self.b1), ('I1', self.i1))},
            'workload': workload,
            'ground_truth': {**truth, 'k': k, 'target_table': target_table(spec['pattern']),
                             'tables_before': before, 'tables_after': after,
                             'pages_per_row': pages_per_row(spec, truth, k, before, content,
                                                            diffs, relmap)},
            'autovacuum_activity': autovacuum_activity(stats),
            'pg_stat_user_tables': stats,
            'content_changes': content,
            'tracker': tracker_view,
            'agreement': agreement,
            'relation_map_size': len(relmap),
        })
        return result


def target_table(pattern: str) -> str:
    return 'public.events' if pattern in workloads.EVENT_PATTERNS else 'public.pgbench_accounts'


def _pct(part, whole):
    return 100 * part / whole if part is not None and whole else None


def pages_per_row(spec, truth, k, before, content, diffs, relmap) -> dict:
    """Changed 8 KiB pages of the target table's heap and indexes.

    Existing heap pages are the main-fork blocks within B0's length;
    heap_existing_changed_pct (changed existing / B0 pages) is the figure to
    compare with the uniform model, which predicts only existing pages
    touched. Appended pages (B1 blocks beyond B0's length, all changed) are
    counted separately. heap_overall_changed_pct mixes both: appended pages
    are in its numerator and denominator.
    """
    table = target_table(spec['pattern'])
    by_relation = content['by_relation']

    def pages(name):
        return by_relation.get(name, {}).get('chunks', {}).get('8K', {})

    heap = pages(table)
    indexes = sorted(info['name'] for info in relmap.values()
                     if info['parent'] == table and info['relkind'] in ('i', 'I'))
    index_changed = sum(pages(name).get('changed', 0) for name in indexes)
    index_compared = sum(pages(name).get('chunks', 0) for name in indexes)
    b0_pages = existing = appended = 0
    for diff in diffs:
        info = pagediff.classify(diff.path, relmap)
        if info['relation'] == table and info['fork'] == 'main':
            old_blocks = -(-(diff.old_size or 0) // pagediff.BLOCK_SIZE)
            b0_pages += old_blocks
            # Blocks cut off by a truncation lie below old_blocks: existing pages that changed.
            existing += sum(block < old_blocks for block in diff.changed)
            appended += sum(block >= old_blocks for block in diff.changed)
    rows = truth['rows_changed']
    relname = table.split('.', 1)[1]
    catalog_pages = before['relations'].get(relname, {}).get('main_bytes', 0) \
        // pagediff.BLOCK_SIZE
    tuples = before['row_counts'].get(relname) or 0
    # Expected share of the P existing pages touched if k rows were hit uniformly.
    model = 1 - (1 - 1 / b0_pages) ** k if b0_pages else None
    return {
        'table': table, 'indexes': indexes,
        'heap_pages_existing': b0_pages,
        'heap_pages_changed_existing': existing,
        'heap_existing_changed_pct': _pct(existing, b0_pages),
        'uniform_model_heap_existing_pct': None if model is None else 100 * model,
        'heap_appended_pages': appended,
        'heap_pages_compared': heap.get('chunks'),
        'heap_pages_changed': heap.get('changed'),
        'heap_overall_changed_pct': _pct(heap.get('changed'), heap.get('chunks')),
        'index_pages_compared': index_compared,
        'index_pages_changed': index_changed,
        'index_changed_pct': _pct(index_changed, index_compared),
        'heap_pages_per_changed_row': heap.get('changed', 0) / rows if rows else None,
        'index_pages_per_changed_row': index_changed / rows if rows else None,
        'heap_pages_before': catalog_pages,
        'rows_per_heap_page_before': tuples / catalog_pages if catalog_pages else None,
        'definitions': {
            'heap_existing_changed_pct': 'changed heap pages within B0 length / B0 heap pages; '
                                         'compare with uniform_model_heap_existing_pct',
            'uniform_model_heap_existing_pct': '1 - (1 - 1/P)^k with P = heap_pages_existing',
            'heap_appended_pages': 'heap pages in B1 beyond B0 length (new row versions)',
            'heap_overall_changed_pct': 'changed / compared heap pages, where both include '
                                        'appended pages; not comparable with the model',
            'heap_pages_before': 'pg_relation_size before B0, from the catalog',
        },
    }


def autovacuum_activity(stats: dict) -> dict:
    """Autovacuum or autoanalyze runs seen between the stats snapshots."""
    events = {}
    for first, second in (('before_workload', 'before_b1'), ('before_b1', 'after_i1')):
        for table, row in stats[second].items():
            old = stats[first].get(table, {})
            for key in ('autovacuum_count', 'autoanalyze_count'):
                if row.get(key, 0) != old.get(key, 0):
                    events.setdefault(f'{first}..{second}', []).append(f'{table}.{key}')
    return {'detected': bool(events), 'events': events,
            'note': 'counters are cumulative statistics and may lag by up to a second'}


def variant_label(flags: dict[str, bool]) -> str:
    """'base', or the enabled options sorted and '+'-joined: what the trial runs.

    The order of the post-workload steps is fixed (read scan, vacuum,
    checkpoint), so sorting loses nothing.
    """
    return '+'.join(sorted(token for token, name in VARIANT_FLAGS.items() if flags[name])) \
        or 'base'


def trial_specs(args) -> list[dict]:
    """One spec per trial. IDs and 'variant' name the effective options, the
    --variants token combined with the global flags; 'variant_requested' keeps
    the token. Two trials that would run the same thing are rejected."""
    if args.matrix:
        patterns, fractions, variants = args.patterns, args.fractions, args.variants
    else:
        patterns, fractions, variants = [args.pattern], [args.fraction], ['base']
    specs = []
    seen: dict[tuple, str] = {}
    combos = itertools.product(range(args.repeats), variants, patterns, fractions)
    for index, (repeat, variant, pattern, fraction) in enumerate(combos):
        flags = {name: getattr(args, name) for name in VARIANT_FLAGS.values()}
        for token in variant.split('+'):
            if token != 'base':
                flags[VARIANT_FLAGS[token]] = True
        label = variant_label(flags)
        accounts_rows = workloads.ACCOUNTS_PER_SCALE * args.scale
        events_rows = args.events_rows if args.events_rows is not None else accounts_rows
        # Fail fast on impossible parameters before any cluster work.
        k = workloads.target_rows(pattern, fraction, accounts_rows=accounts_rows,
                                  events_rows=events_rows)
        suffix = '' if args.repeats == 1 else f'-r{repeat}'
        trial_id = f'{index:02d}-{pattern}-f{fraction:g}-{label}{suffix}'
        described = f'fraction {fraction:g} with --variants entry {variant!r}'
        key = (repeat, pattern, k, label)
        if key in seen:
            raise ValueError(
                f'duplicate trial configuration: {pattern} {described} runs the same trial '
                f'(k={k}, options {label}) as {seen[key]}; remove one of them')
        seen[key] = described
        spec = {
            'trial_id': trial_id,
            'pattern': pattern, 'fraction': fraction, 'variant': label,
            'variant_requested': variant, 'repeat': repeat,
            'scale': args.scale, 'accounts_rows': accounts_rows, 'events_rows': events_rows,
            'hot_percent': args.hot_percent, 'seed': args.seed + repeat,
            'batch_size': args.batch_size, 'data_checksums': not flags['no_checksums'],
            'load_vacuum': not flags['no_load_vacuum'],
            'checkpoint': flags['checkpoint'], 'vacuum': flags['vacuum'],
            'read_scan': flags['read_scan'],
            'compare_methods': args.compare_methods, 'rsync': args.rsync,
            # Only when set, so every other trial's spec (and its record) is unchanged.
            **({'hex_filler': True} if flags['hex_filler'] else {}),
        }
        if pattern == 'hot':
            workloads.select_aids('hot', k, accounts_rows, spec['seed'], args.hot_percent)
        if pattern == 'retention' and k > events_rows:
            raise ValueError(f'{trial_id}: retention k={k} exceeds the events rows')
        specs.append(spec)
    return specs


def run_trial(spec: dict, tools: dict, major: int, out_dir: Path, *, keep: bool,
              work_dir: Path | None, environment: dict | None = None) -> dict:
    root = Path(tempfile.mkdtemp(prefix='pdbcp-', dir=work_dir))
    params = {key: value for key, value in spec.items() if key != 'trial_id'}
    result = {'schema': SCHEMA, 'trial_id': spec['trial_id'], 'status': 'running',
              'params': params, 'environment': environment,
              'started_utc': datetime.now(timezone.utc).isoformat()}
    trial = Trial(spec, tools, major, root, out_dir / 'logs' / spec['trial_id'])
    started = time.perf_counter()
    try:
        result.update(trial.execute())
        if spec.get('compare_methods'):
            # EXP-02: every method rebuilds B1 from B0; run with the cluster stopped so
            # timings aren't disturbed by it.
            trial.stop()
            compare_started = time.perf_counter()
            result['methods'] = compare_methods.compare(
                trial.b0, trial.b1, trial.i1, root / 'compare', rsync=spec['rsync'],
                pg_controldata=tools.get('pg_controldata'))
            trial.timings['compare_methods'] = round(time.perf_counter() - compare_started, 3)
            bad = [name for name, entry in result['methods'].items()
                   if isinstance(entry, dict) and entry.get('exit_code') not in (None, 0)
                   or isinstance(entry, dict) and entry.get('identical') is False]
            if bad:
                raise RuntimeError('method comparison failed for ' + ', '.join(bad))
        result['status'] = 'pass'
    except Exception as exc:  # record and continue with the next trial
        result.update(status='error', error=f'{type(exc).__name__}: {exc}')
        (trial.log_dir / 'error.txt').write_text(traceback.format_exc())
        trial.log(f'ERROR {exc}')
    finally:
        try:
            trial.stop()
        except Exception as exc:
            result.update(status='error', cleanup_error=str(exc))
        if keep or result.get('cleanup_error'):
            result['kept_work_dir'] = str(root)
        else:
            shutil.rmtree(root, ignore_errors=True)
    result['timings'] = {**trial.timings, 'total': round(time.perf_counter() - started, 3)}
    result['commands'] = trial.commands
    (out_dir / 'trials').mkdir(parents=True, exist_ok=True)
    atomic_json(out_dir / 'trials' / f"{spec['trial_id']}.json", result)
    return result


def csv_list(kind):
    def parse(text: str):
        return [kind(item) for item in text.split(',') if item.strip()]
    return parse


def variant_list(text: str) -> list[str]:
    variants = [item.strip() for item in text.split(',') if item.strip()]
    for variant in variants:
        bad = [t for t in variant.split('+') if t != 'base' and t not in VARIANT_FLAGS]
        if bad:
            raise argparse.ArgumentTypeError(
                f"unknown variant option(s) {bad}; use base or +-joined "
                f"{', '.join(VARIANT_FLAGS)}")
    return variants


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--bin-dir', type=Path, default=Path(DEFAULT_BIN_DIR))
    parser.add_argument('--out', type=Path, help='new or empty output directory '
                        '(default results/change_patterns/<UTC time>)')
    parser.add_argument('--work-dir', type=Path,
                        help='parent for temporary cluster/backups (default system temp dir)')
    parser.add_argument('--keep', action='store_true', help='keep temporary directories')
    parser.add_argument('--pattern', choices=workloads.PATTERNS, default='uniform')
    parser.add_argument('--fraction', type=float, default=0.01,
                        help='fraction of the target table rows to change (tpcb: transactions)')
    parser.add_argument('--matrix', action='store_true',
                        help='run --patterns x --fractions x --variants instead')
    parser.add_argument('--patterns', type=csv_list(str), default=['uniform', 'hot', 'append'])
    parser.add_argument('--fractions', type=csv_list(float), default=[0.01])
    parser.add_argument('--variants', type=variant_list, default=['base'],
                        help='comma list; each is base or +-joined checkpoint, vacuum, '
                             'read-scan, no-checksums, no-load-vacuum')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--scale', type=int, default=1, help='pgbench scale (70 is about 1 GB)')
    parser.add_argument('--events-rows', type=int,
                        help='events prefill rows (default: same as pgbench_accounts)')
    parser.add_argument('--hot-percent', type=float, default=1.0)
    parser.add_argument('--seed', type=int, default=20261022)
    parser.add_argument('--batch-size', type=int, default=100, help='statements per transaction')
    parser.add_argument('--checkpoint', action='store_true', help='CHECKPOINT after the workload')
    parser.add_argument('--vacuum', action='store_true', help='VACUUM after the workload')
    parser.add_argument('--read-scan', action='store_true',
                        help='SELECT count(*) on each table after the workload')
    parser.add_argument('--no-checksums', action='store_true', help='initdb without checksums')
    parser.add_argument('--hex-filler', action='store_true',
                        help="overwrite pgbench_accounts' blank filler with hex text after the load (EXP-45)")
    parser.add_argument('--no-load-vacuum', action='store_true',
                        help='skip VACUUM ANALYZE and row counts before B0, leaving the events '
                             'prefill without hint bits (makes --read-scan effects visible)')
    parser.add_argument('--stop-on-error', action='store_true')
    parser.add_argument('--compare-methods', action='store_true',
                        help='EXP-02: also rebuild B1 from B0 with pgdelta and rsync and measure them')
    parser.add_argument('--rsync', default='/opt/homebrew/bin/rsync' if Path(
                        '/opt/homebrew/bin/rsync').is_file() else 'rsync',
                        help='rsync for --compare-methods (GNU rsync 3.x recommended)')
    args = parser.parse_args(argv)
    if args.matrix:
        for name in ('patterns', 'fractions', 'variants'):
            if not getattr(args, name):
                parser.error(f'--{name} is empty; a --matrix run needs at least one value')
    for pattern in args.patterns:
        if pattern not in workloads.PATTERNS:
            parser.error(f'unknown pattern {pattern!r}')
    if args.scale < 1 or args.repeats < 1 or args.batch_size < 1:
        parser.error('--scale, --repeats and --batch-size must be at least 1')
    if args.events_rows is not None and args.events_rows < 1:
        parser.error('--events-rows must be at least 1')
    return args, parser


def main(argv=None) -> int:
    args, parser = parse_args(argv)
    try:
        specs = trial_specs(args)
    except ValueError as exc:
        parser.error(str(exc))
    if not specs:
        parser.error('no trials to run')
    tools, version, major = preflight(args.bin_dir)
    out = args.out or REPO_ROOT / 'results' / 'change_patterns' / \
        datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    if out.exists() and any(out.iterdir()):
        parser.error(f'output directory is not empty: {out}')
    (out / 'trials').mkdir(parents=True, exist_ok=True)
    run = {
        'schema': 'change-pattern-run/1', 'argv': sys.argv[1:] if argv is None else list(argv),
        'started_utc': datetime.now(timezone.utc).isoformat(),
        'environment': {'postgres_version': version, 'bin_dir': str(args.bin_dir),
                        'python': platform.python_version(), 'platform': platform.platform(),
                        'cpu_count': os.cpu_count(), **git_provenance()},
        'trials': [spec['trial_id'] for spec in specs], 'status': {},
    }
    atomic_json(out / 'run.json', run)
    results = []
    for number, spec in enumerate(specs, 1):
        print(f"Trial {number}/{len(specs)}: {spec['trial_id']}", file=sys.stderr, flush=True)
        result = run_trial(spec, tools, major, out, keep=args.keep, work_dir=args.work_dir,
                           environment=run['environment'])
        results.append(result)
        run['status'][spec['trial_id']] = result['status']
        atomic_json(out / 'run.json', run)
        report.write_summary_csv(out / 'summary.csv', results)
        if result['status'] != 'pass' and args.stop_on_error:
            break
    run['finished_utc'] = datetime.now(timezone.utc).isoformat()
    atomic_json(out / 'run.json', run)
    print(f'Results: {out}', file=sys.stderr)
    print((out / 'summary.csv').read_text(), end='')
    return 0 if all(r['status'] == 'pass' for r in results) and len(results) == len(specs) else 1


if __name__ == '__main__':
    raise SystemExit(main())
