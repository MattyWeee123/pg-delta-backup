"""EXP-03 scenario builders.

Each builder leaves an old copy B in ``old_copy/`` and returns the running,
quiesced source cluster. ``build_scenario`` then takes the fresh backup S,
records the logical state, stops every server, picks the LSN threshold, runs
the LSN-skip analysis and writes ``scenario.json``.

The workload is deterministic SQL (generate_series and md5), so two builds at
the same scale do the same logical work. Autovacuum is off and
checkpoint_timeout is 30 minutes in every cluster, so the only checkpoints and
vacuums are the ones a scenario asks for.
"""
from __future__ import annotations

from functools import partial

import json
import shutil
import subprocess
import tempfile
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from experiments.tricky_copies import lsnscan
from experiments.tricky_copies.classify import classify_wrong_skips
from experiments.tricky_copies.logical import compute_logical_state
from experiments.tricky_copies.pgcluster import (REDO_KEY, Cluster, PgBin, clean_env, free_port,
                                                 parse_backup_label, pg_major_of, run,
                                                 select_threshold, short_tempdir, tree_size)

BASE_CONF = {'autovacuum': 'off', 'checkpoint_timeout': '30min'}
DEFAULT_ROWS = 200_000
SEGMENT_BLOCKS = 131072  # 1 GiB segments of 8 KiB blocks
USER_TABLES = ('accounts', 'history', 'docs', 'tags')


# --------------------------------------------------------------------------
# Workload SQL
# --------------------------------------------------------------------------
def load_sql(rows, vacuum=True):
    """Schema plus a bulk load in one transaction. Indexes exist before the
    load, so nothing reads the new rows back (S7 relies on that)."""
    sql = f"""
CREATE TABLE accounts (id bigint PRIMARY KEY, bal bigint NOT NULL, filler text NOT NULL);
CREATE INDEX accounts_bal ON accounts (bal);
CREATE TABLE history (id bigserial PRIMARY KEY, account_id bigint NOT NULL,
                      delta bigint NOT NULL, note text NOT NULL);
CREATE TABLE docs (id int PRIMARY KEY, body text NOT NULL);
CREATE TABLE tags (k int NOT NULL, v text NOT NULL);
BEGIN;
INSERT INTO accounts SELECT g, g % 1000, repeat(md5(g::text), 2) FROM generate_series(1, {rows}) g;
INSERT INTO history (account_id, delta, note)
  SELECT g, 1, 'load' FROM generate_series(1, {max(rows // 2, 10)}) g;
INSERT INTO docs
  SELECT g, (SELECT string_agg(md5((g * 1000 + i)::text), '') FROM generate_series(1, 128) i)
  FROM generate_series(1, {max(rows // 200, 10)}) g;
INSERT INTO tags SELECT g % 97, md5(g::text) FROM generate_series(1, {max(rows // 10, 10)}) g;
COMMIT;
"""
    if vacuum:
        sql += 'VACUUM (ANALYZE);\n'
    return sql


def burst_sql(rows, step, vacuum=True):
    """A small, scattered batch of writes; ``step`` makes each batch different."""
    sql = f"""
UPDATE accounts SET bal = bal + {step}, filler = repeat(md5((id + {step})::text), 2)
  WHERE id % 997 = {step % 997};
INSERT INTO history (account_id, delta, note)
  SELECT g, {step}, 'burst {step}' FROM generate_series(1, {max(rows // 50, 10)}) g;
DELETE FROM history WHERE note = 'load' AND id % 101 = {step % 101};
UPDATE docs SET body = reverse(body) WHERE id % 37 = {step % 37};
"""
    if vacuum:
        sql += 'VACUUM accounts, history, docs;\n'
    return sql


def diverging_sql(rows):
    """S2: what the old primary writes after the standby stopped following it."""
    return f"""
UPDATE accounts SET bal = -bal, filler = upper(filler) WHERE id % 200 = 3;
INSERT INTO accounts VALUES ({rows + 1}, 0, 'written only on the old primary');
"""


DIVERGED_BLOCKS_SQL = ("SELECT DISTINCT ((ctid::text)::point)[0]::bigint FROM accounts "
                       "WHERE id % 200 = 3 ORDER BY 1;")

CRASH_PGBENCH = r"""\set aid random(1, :rows)
BEGIN;
UPDATE accounts SET bal = bal + 1 WHERE id = :aid;
INSERT INTO history (account_id, delta, note) VALUES (:aid, 1, 'crash');
END;
"""

SEQSCAN_SQL = """SET enable_indexscan = off;
SET enable_indexonlyscan = off;
SET enable_bitmapscan = off;
""" + ''.join(f"SELECT count(*) FROM {t};\nSELECT sum(length(t::text)) FROM {t} t;\n"
              for t in USER_TABLES)


# --------------------------------------------------------------------------
# Build context
# --------------------------------------------------------------------------
class BuildContext:
    def __init__(self, scenario_dir, rows, bins, default_bin, keep=False, verbose=True):
        self.scenario_dir = Path(scenario_dir)
        self.old_copy = self.scenario_dir / 'old_copy'
        self.source_backup = self.scenario_dir / 'source_backup'
        self.rows, self.bins, self.default_bin = rows, bins, default_bin
        self.keep, self.verbose = keep, verbose
        self.work = Path(tempfile.mkdtemp(prefix='pgdwork-', dir=self.scenario_dir))
        self.logs = self.work / 'logs'
        self.logs.mkdir()
        self.sockdir = short_tempdir()
        self.clusters = []
        self.ports = set()
        self.steps = []
        self.checks = {}
        self.observations = {}
        self.notes = []
        self.facts = {}
        self.started = time.monotonic()

    def step(self, text):
        elapsed = round(time.monotonic() - self.started, 2)
        self.steps.append({'t': elapsed, 'step': text})
        if self.verbose:
            print(f'  [{elapsed:7.2f}s] {self.scenario_dir.name}: {text}', flush=True)

    def check(self, name, ok, **details):
        self.checks[name] = {'ok': bool(ok), **details}

    def new_cluster(self, name, pgbin=None):
        port = free_port(self.ports)
        self.ports.add(port)
        cluster = Cluster(name, pgbin or self.default_bin, self.work / name, self.sockdir, port,
                          self.logs / f'{name}.log')
        self.clusters.append(cluster)
        return cluster

    def new_primary(self, name, checksums=True, conf=None, pgbin=None, load=True, vacuum=True,
                    db='postgres'):
        cluster = self.new_cluster(name, pgbin)
        flag = 'with' if checksums else 'without'
        self.step(f'initdb {name} ({cluster.pgbin.version_text}, {flag} data checksums)')
        cluster.initdb(checksums=checksums)
        cluster.append_conf({**BASE_CONF, **(conf or {})})
        cluster.start()
        if load:
            self.step(f'load data on {name} ({self.rows} accounts rows)')
            cluster.psql(load_sql(self.rows, vacuum=vacuum), db)
        return cluster

    def new_standby(self, name, primary):
        standby = self.new_cluster(name, primary.pgbin)
        self.step(f'create standby {name} with pg_basebackup -R from {primary.name}')
        primary.basebackup(standby.datadir, extra=['-R'])
        standby.start()
        primary.wait_for("SELECT count(*) > 0 FROM pg_stat_replication WHERE state = 'streaming'")
        self.step(f'{name} is streaming from {primary.name}')
        return standby

    def wait_replay(self, primary, standby):
        target = primary.scalar('SELECT pg_current_wal_lsn()')
        standby.wait_for(f"SELECT pg_last_wal_replay_lsn() >= '{target}'::pg_lsn")
        self.step(f'{standby.name} replayed up to {target}')
        return target

    def backup_old_copy(self, cluster):
        self.step(f'B = base backup of {cluster.name}')
        cluster.basebackup(self.old_copy)

    def adopt_datadir_as_old_copy(self, cluster, copy=False):
        if copy:
            self.step(f'B = copy of {cluster.name} data directory (shutil.copytree)')
            shutil.copytree(cluster.datadir, self.old_copy, symlinks=True)
        else:
            self.step(f'B = {cluster.name} data directory (moved into old_copy/)')
            shutil.move(str(cluster.datadir), str(self.old_copy))
            cluster.datadir = self.old_copy
            self.clusters.remove(cluster)  # never started again

    def stop_all(self):
        for cluster in self.clusters:
            if cluster.running:
                cluster.stop('fast')

    def close(self):
        for cluster in self.clusters:
            cluster.ensure_stopped()
        logs = self.scenario_dir / 'logs'
        if self.logs.is_dir():
            shutil.copytree(self.logs, logs, dirs_exist_ok=True)
        if not self.keep:
            shutil.rmtree(self.work, ignore_errors=True)
        shutil.rmtree(self.sockdir, ignore_errors=True)


def xid_horizon(cluster):
    """First transaction id not yet assigned; it only moves when something writes."""
    return int(cluster.scalar('SELECT pg_snapshot_xmax(pg_current_snapshot())'))


def other_sessions(cluster):
    return int(cluster.scalar("SELECT count(*) FROM pg_stat_activity WHERE backend_type = "
                              "'client backend' AND pid <> pg_backend_pid()"))


def read_block(path, block):
    """One 8 KiB block of a relation, following 1 GiB segment files; None if absent."""
    segment, offset = divmod(block, SEGMENT_BLOCKS)
    name = Path(path) if segment == 0 else Path(f'{path}.{segment}')
    if not name.is_file():
        return None
    with name.open('rb') as handle:
        handle.seek(offset * lsnscan.BLOCK)
        page = handle.read(lsnscan.BLOCK)
    return page if len(page) == lsnscan.BLOCK else None


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------
def build_s1(ctx, restartpoint=True):
    p = ctx.new_primary('primary')
    r = ctx.new_standby('standby', p)
    ctx.step('writes on P (burst 1), replicated to R')
    p.psql(burst_sql(ctx.rows, 1))
    if restartpoint:
        ctx.step('CHECKPOINT on P, so the restartpoint R makes at shutdown lies after burst 1')
        p.psql('CHECKPOINT;')
    ctx.wait_replay(p, r)
    ctx.step('stop R cleanly (-m fast)')
    r.stop('fast')
    if restartpoint:
        ctx.observations['threshold_note'] = (
            "B's REDO location is the restartpoint R made at shutdown for the primary checkpoint "
            'it replayed after burst 1, as on any standby that has run through a checkpoint. '
            'Without that checkpoint the threshold would be the start of the base backup R was '
            'cloned from (see S1b).')
    else:
        ctx.observations['threshold_note'] = (
            'No primary checkpoint after burst 1, so R has no newer restartpoint and the '
            "threshold is the start of the base backup R was cloned from (compare S1).")
    ctx.adopt_datadir_as_old_copy(r)
    ctx.step('more writes on P (burst 2), never seen by R')
    p.psql(burst_sql(ctx.rows, 2))
    return p


def check_s1(ctx, old_info, source_info, threshold):
    ctx.check('same_system_identifier',
              old_info['system_identifier'] == source_info['system_identifier'])
    ctx.check('same_timeline', old_info['timeline'] == source_info['timeline'],
              old=old_info['timeline'], source=source_info['timeline'])
    ctx.check('old_copy_is_stopped_standby',
              old_info['cluster_state'] == 'shut down in recovery',
              cluster_state=old_info['cluster_state'])


def build_s2(ctx):
    p = ctx.new_primary('primary')
    r = ctx.new_standby('standby', p)
    ctx.step('writes on P (burst 1), replicated to R')
    p.psql(burst_sql(ctx.rows, 1))
    p.psql('CHECKPOINT;')
    split = ctx.wait_replay(p, r)
    ctx.step('stop R cleanly: this is the split point')
    r.stop('fast')
    ctx.facts['split_lsn'] = split
    relpath = p.scalar("SELECT pg_relation_filepath('accounts')")
    blocks = [int(row[0]) for row in p.query(DIVERGED_BLOCKS_SQL)]
    ctx.facts['diverged'] = {'file': relpath, 'blocks': blocks}
    ctx.step(f'DIVERGING writes on P: update rows in {len(blocks)} pages of accounts and insert one')
    p.psql(diverging_sql(ctx.rows))
    ctx.facts['old_primary_end_lsn'] = p.scalar('SELECT pg_current_wal_lsn()')
    ctx.step('stop P cleanly (-m fast)')
    p.stop('fast')
    ctx.adopt_datadir_as_old_copy(p)
    ctx.step('start R alone and promote it (new timeline)')
    r.start()
    r.promote()
    r.wait_for('SELECT NOT pg_is_in_recovery()')
    ctx.step('writes on the new primary R (burst 2)')
    r.psql(burst_sql(ctx.rows, 2))
    return r


def check_s2(ctx, old_info, source_info, threshold):
    facts = ctx.facts['diverged']
    redo = lsnscan.parse_lsn(threshold['lsn'])
    differing, missing, below = 0, 0, 0
    for block in facts['blocks']:
        old_page = read_block(ctx.old_copy / facts['file'], block)
        new_page = read_block(ctx.source_backup / facts['file'], block)
        if old_page is None or new_page is None:
            missing += 1
            continue
        differing += old_page != new_page
        below += lsnscan.page_lsn(new_page) < redo
    total = len(facts['blocks'])
    ctx.check('diverged_pages_differ', total > 0 and differing == total, file=facts['file'],
              pages_written_after_split=total, pages_differing=differing, pages_missing=missing,
              source_pages_below_threshold=below)
    ctx.check('timeline_switched', source_info['timeline'] > old_info['timeline'],
              old=old_info['timeline'], source=source_info['timeline'])
    ctx.check('same_system_identifier',
              old_info['system_identifier'] == source_info['system_identifier'])
    history = sorted((ctx.source_backup / 'pg_wal').glob('*.history'))
    ctx.observations['timeline_history'] = {h.name: h.read_text().strip() for h in history}
    ctx.observations['split_lsn'] = ctx.facts['split_lsn']
    ctx.observations['old_primary_end_lsn'] = ctx.facts['old_primary_end_lsn']


def build_s3(ctx):
    p = ctx.new_primary('primary')
    script = ctx.work / 'crash_writes.sql'
    script.write_text(CRASH_PGBENCH)
    ctx.step('start pgbench writes on P (2 clients)')
    log = (ctx.logs / 'pgbench.log').open('w')
    bench = subprocess.Popen(
        [str(arg) for arg in (p.pgbin.tool('pgbench'), '-n', '-c', '2', '-j', '2', '-T', '600',
                              '-D', f'rows={ctx.rows}', '-f', script, '-h', p.sockdir,
                              '-p', p.port, '-U', 'postgres', 'postgres')],
        stdout=log, stderr=subprocess.STDOUT, env=clean_env())
    try:
        count_sql = "SELECT count(*) FROM history WHERE note = 'crash'"

        def wait_rows(minimum, timeout=60):
            deadline = time.monotonic() + timeout
            while int(p.scalar(count_sql)) < minimum:
                if bench.poll() is not None:
                    raise RuntimeError(f'pgbench exited early with {bench.returncode}')
                if time.monotonic() > deadline:
                    raise TimeoutError('pgbench made no progress')
                time.sleep(0.1)

        wait_rows(300)
        ctx.step('CHECKPOINT on P while the writes run')
        p.psql('CHECKPOINT;')
        before = int(p.scalar(count_sql))
        wait_rows(before + 300)
        time.sleep(0.5)
        ctx.step('stop P with -m immediate while pgbench is still writing')
        ctx.facts['pgbench_running_at_crash'] = bench.poll() is None
        p.stop('immediate')
    finally:
        if p.running and bench.poll() is None:
            bench.terminate()  # failed before the crash; otherwise pgbench exits by itself
        try:
            bench.wait(timeout=30)
        except subprocess.TimeoutExpired:
            bench.kill()
            bench.wait()
        log.close()
    ctx.facts['pgbench_exit_code'] = bench.returncode
    ctx.adopt_datadir_as_old_copy(p, copy=True)
    ctx.step('restart P (crash recovery)')
    p.start()
    ctx.step('more writes on P (burst 1)')
    p.psql(burst_sql(ctx.rows, 1))
    return p


def check_s3(ctx, old_info, source_info, threshold):
    ctx.check('old_copy_is_crash_consistent', old_info['cluster_state'] == 'in production'
              and ctx.facts.get('pgbench_running_at_crash'),
              cluster_state=old_info['cluster_state'],
              pgbench_running_at_crash=ctx.facts.get('pgbench_running_at_crash'),
              pgbench_exit_code=ctx.facts.get('pgbench_exit_code'))
    ctx.check('same_timeline', old_info['timeline'] == source_info['timeline'])


def build_s4(ctx):
    p = ctx.new_primary('primary')
    ctx.backup_old_copy(p)
    relpath = p.scalar("SELECT pg_relation_filepath('accounts')")
    before = int(p.scalar("SELECT pg_relation_size('accounts')"))
    ctx.step(f'delete accounts rows with id > {ctx.rows // 2} and VACUUM accounts')
    p.psql(f'DELETE FROM accounts WHERE id > {ctx.rows // 2};\nVACUUM accounts;\n')
    after = int(p.scalar("SELECT pg_relation_size('accounts')"))
    ctx.facts['truncation'] = {'file': relpath, 'size_before': before, 'size_after': after}
    ctx.step(f'accounts heap went from {before} to {after} bytes')
    return p


def check_s4(ctx, old_info, source_info, threshold):
    facts = ctx.facts['truncation']
    old_size = file_size(ctx.old_copy / facts['file'])
    new_size = file_size(ctx.source_backup / facts['file'])
    ctx.check('heap_file_truncated', new_size is not None and old_size is not None
              and new_size < old_size, file=facts['file'], old_copy_bytes=old_size,
              source_bytes=new_size, live_size_before=facts['size_before'],
              live_size_after=facts['size_after'])


def file_size(path):
    """Size of a relation fork across its 1 GiB segment files; None if it is absent."""
    path = Path(path)
    if not path.is_file():
        return None
    total, segment = path.stat().st_size, 1
    while Path(f'{path}.{segment}').is_file():
        total += Path(f'{path}.{segment}').stat().st_size
        segment += 1
    return total


DB_OIDS_SQL = "SELECT datname, oid FROM pg_database ORDER BY datname;"


def build_s5(ctx):
    p = ctx.new_primary('primary', load=False)
    ctx.step('create template database tmpl and load data into it')
    p.psql('CREATE DATABASE tmpl;')
    p.psql(load_sql(ctx.rows), 'tmpl')
    ctx.step('create database app from tmpl (default strategy) and write to it')
    p.psql('CREATE DATABASE app TEMPLATE tmpl;')
    p.psql(burst_sql(ctx.rows, 1), 'app')
    ctx.facts['oids_at_b'] = dict(p.query(DB_OIDS_SQL))
    ctx.backup_old_copy(p)
    ctx.step('CREATE DATABASE app2 TEMPLATE tmpl STRATEGY FILE_COPY')
    p.psql('CREATE DATABASE app2 TEMPLATE tmpl STRATEGY FILE_COPY;')
    ctx.step('DROP DATABASE app; CREATE DATABASE app TEMPLATE tmpl STRATEGY FILE_COPY')
    p.psql('DROP DATABASE app;\nCREATE DATABASE app TEMPLATE tmpl STRATEGY FILE_COPY;\n')
    ctx.facts['oids_at_s'] = dict(p.query(DB_OIDS_SQL))
    return p


def check_s5(ctx, old_info, source_info, threshold):
    redo = lsnscan.parse_lsn(threshold['lsn'])
    at_b, at_s = ctx.facts['oids_at_b'], ctx.facts['oids_at_s']
    databases = {}
    for name, oid in sorted(at_s.items()):
        source_dir = ctx.source_backup / 'base' / oid
        old_dir = ctx.old_copy / 'base' / oid
        files = pages = below = same_path = 0
        for path in sorted(source_dir.glob('*')):
            if not path.is_file() or lsnscan.relation_kind(f'base/{oid}/{path.name}') is None:
                continue
            files += 1
            same_path += (old_dir / path.name).is_file()
            with path.open('rb') as handle:
                while page := handle.read(lsnscan.BLOCK):
                    pages += 1
                    lsn = lsnscan.page_lsn(page)
                    below += lsn is not None and lsn < redo
        databases[name] = {'oid_at_b': at_b.get(name), 'oid_at_s': oid,
                           'directory_in_old_copy': old_dir.is_dir(),
                           'relation_files': files, 'files_at_same_path_in_old_copy': same_path,
                           'pages': pages, 'pages_with_lsn_below_threshold': below}
    ctx.observations['databases'] = databases
    recreated = databases.get('app', {})
    new_db = databases.get('app2', {})
    ctx.check('app_recreated_with_new_oid', at_b.get('app') != at_s.get('app'),
              oid_at_b=at_b.get('app'), oid_at_s=at_s.get('app'))
    ctx.check('file_copy_databases_exist', bool(recreated) and bool(new_db))
    for name in ('app', 'app2'):
        info = databases.get(name, {})
        if info and not info['directory_in_old_copy']:
            ctx.notes.append(
                f"{name} (OID {info['oid_at_s']}) has no directory in B, so path-matched LSN "
                f"skipping never compares its pages; {info['pages_with_lsn_below_threshold']}"
                f" of its {info['pages']} pages carry LSNs below the threshold and would be "
                "skipped if a matching path existed")


def build_s6(ctx):
    p = ctx.new_primary('primary', checksums=False)
    ctx.backup_old_copy(p)
    ctx.step('stop P, pg_checksums --enable, start P')
    p.stop('fast')
    run([p.pgbin.tool('pg_checksums'), '--enable', '-D', p.datadir], timeout=1800)
    p.start()
    ctx.step('small writes on P')
    p.psql("UPDATE accounts SET bal = bal + 1 WHERE id <= 10;\n"
           "INSERT INTO history (account_id, delta, note) "
           "SELECT g, 1, 'after checksums' FROM generate_series(1, 10) g;\n")
    return p


def check_s6(ctx, old_info, source_info, threshold):
    ctx.check('checksums_enabled_between_copies',
              old_info['data_checksum_version'] == 0 and source_info['data_checksum_version'] == 1,
              old=old_info['data_checksum_version'], source=source_info['data_checksum_version'])


def build_s7(ctx):
    p = ctx.new_primary('primary', checksums=False, conf={'wal_log_hints': 'off'}, vacuum=False)
    ctx.step('CHECKPOINT right after the bulk load, without reading the rows back')
    p.psql('CHECKPOINT;')
    ctx.facts['xid_at_b'] = xid_horizon(p)
    ctx.backup_old_copy(p)
    ctx.step('read-only full scans of every table (sets hint bits), then CHECKPOINT')
    p.psql(SEQSCAN_SQL)
    p.psql('CHECKPOINT;')
    ctx.facts['xid_at_s'] = xid_horizon(p)
    return p


def check_s7(ctx, old_info, source_info, threshold):
    ctx.check('no_row_changes_between_copies', ctx.facts['xid_at_b'] == ctx.facts['xid_at_s'],
              xid_horizon_at_b=ctx.facts['xid_at_b'], xid_horizon_at_s=ctx.facts['xid_at_s'])
    ctx.check('no_checksums_no_wal_log_hints',
              source_info['data_checksum_version'] == 0 and source_info['wal_log_hints'] == 'off',
              checksum_version=source_info['data_checksum_version'],
              wal_log_hints=source_info['wal_log_hints'])


def build_s8(ctx):
    x = ctx.new_primary('cluster_x')
    ctx.backup_old_copy(x)
    x.stop('fast')
    return ctx.new_primary('cluster_y')


def check_s8(ctx, old_info, source_info, threshold):
    ctx.check('different_system_identifier',
              old_info['system_identifier'] != source_info['system_identifier'],
              old=old_info['system_identifier'], source=source_info['system_identifier'])


def build_s9(ctx):
    old_bin, new_bin = ctx.bins.get(17), ctx.bins.get(18)
    if old_bin is None or new_bin is None:
        raise RuntimeError('S9 needs both PostgreSQL 17 and 18 binaries')
    old = ctx.new_primary('pg17', pgbin=old_bin)
    ctx.backup_old_copy(old)
    ctx.step('stop the 17 cluster')
    old.stop('fast')
    new = ctx.new_cluster('pg18', new_bin)
    ctx.step(f'initdb the 18 cluster ({new_bin.version_text}) with matching settings')
    new.initdb(checksums=True)
    new.append_conf(BASE_CONF)
    ctx.step('pg_upgrade --link from 17 to 18')
    proc = run([new_bin.tool('pg_upgrade'), '-b', old_bin.bindir, '-B', new_bin.bindir,
                '-d', old.datadir, '-D', new.datadir, '--link', '-U', 'postgres',
                '-p', old.port, '-P', new.port, '--socketdir', ctx.sockdir],
               cwd=ctx.work, timeout=1800)
    (ctx.logs / 'pg_upgrade.log').write_text(proc.stdout + proc.stderr)
    ctx.clusters.remove(old)  # its pg_control is now pg_control.old; it must not start
    ctx.step('start the 18 cluster')
    new.start()
    return new


def check_s9(ctx, old_info, source_info, threshold):
    ctx.check('versions_17_to_18', old_info['pg_version'] == 17 and source_info['pg_version'] == 18,
              old=old_info['pg_version'], source=source_info['pg_version'])
    ctx.check('different_system_identifier',
              old_info['system_identifier'] != source_info['system_identifier'],
              old=old_info['system_identifier'], source=source_info['system_identifier'])


@dataclass(frozen=True)
class Scenario:
    id: str
    slug: str
    title: str
    old_copy: str
    lsn_prediction: str  # 'correct', 'wrong' or 'n/a', as pre-registered in EXP-03
    tool_prediction: str
    builder: Callable
    checker: Optional[Callable] = None

    @property
    def dirname(self):
        return f'{self.id}-{self.slug}'


SCENARIOS = [
    Scenario('S1', 'lagging-standby', 'Lagging standby, same timeline',
             'data directory of a standby that stopped replaying (stopped cleanly)',
             'correct', 'correct, reuses most pages', build_s1, check_s1),
    Scenario('S1b', 'lagging-standby-no-checkpoint',
             'Lagging standby, same timeline, no checkpoint replayed',
             'data directory of a standby that stopped replaying before any new restartpoint',
             'correct', 'correct, reuses most pages', partial(build_s1, restartpoint=False),
             check_s1),
    Scenario('S2', 'ex-primary-after-failover', 'Ex-primary that kept writing after a failover',
             'data directory of the old primary, stopped cleanly after diverging writes',
             'wrong', 'correct; diverged pages detected and re-sent', build_s2, check_s2),
    Scenario('S3', 'crash-consistent-copy', 'Crash-consistent copy',
             'copy of the data directory of a primary stopped with -m immediate mid-write',
             'correct', 'correct', build_s3, check_s3),
    Scenario('S4', 'vacuum-truncation', 'VACUUM truncated a table between copies',
             'base backup taken before half the largest table was deleted and vacuumed',
             'correct', 'correct; the shorter file is handled', build_s4, check_s4),
    Scenario('S5', 'file-copy-database', 'Databases created with STRATEGY FILE_COPY',
             'base backup taken before FILE_COPY database creation and recreation',
             'wrong', 'correct', build_s5, check_s5),
    Scenario('S6', 'checksums-enabled', 'pg_checksums --enable between copies',
             'base backup taken before checksums were enabled offline',
             'wrong', 'correct; every page re-sent', build_s6, check_s6),
    Scenario('S7', 'hint-bits-no-checksums', 'Hint bits set by reads, no checksums',
             'base backup taken before read-only scans set hint bits',
             'wrong', 'correct', build_s7, check_s7),
    Scenario('S8', 'different-cluster', 'A different cluster with the same data',
             'base backup of another cluster built the same way',
             'n/a', 'correct; reuse is only an efficiency question', build_s8, check_s8),
    Scenario('S9', 'pg-upgrade-17-to-18', 'pg_upgrade --link from 17 to 18',
             'PostgreSQL 17 base backup; source upgraded to 18 with pg_upgrade --link',
             'n/a', 'correct; unchanged user-table pages may be reused', build_s9, check_s9),
]
BY_ID = {s.id: s for s in SCENARIOS}


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
def describe_copy(path, bins):
    path = Path(path)
    major = pg_major_of(path)
    if major not in bins:
        raise RuntimeError(f'{path} is PostgreSQL {major}, but no binaries for it were given')
    fields = bins[major].controldata(path)
    label_path = path / 'backup_label'
    label = parse_backup_label(label_path.read_text()) if label_path.is_file() else None
    return {
        'kind': 'base backup directory' if label else 'data directory',
        'pg_version': major,
        'pg_controldata': str(bins[major].tool('pg_controldata')),
        'system_identifier': fields['Database system identifier'],
        'cluster_state': fields['Database cluster state'],
        'timeline': int(fields["Latest checkpoint's TimeLineID"]),
        'backup_label_timeline': int(label['START TIMELINE']) if label else None,
        'redo_location': fields[REDO_KEY],
        'checkpoint_location': fields['Latest checkpoint location'],
        'data_checksum_version': int(fields['Data page checksum version']),
        'wal_log_hints': fields.get('wal_log_hints setting'),
        'catalog_version': fields.get('Catalog version number'),
        'backup_label': label,
        'bytes': tree_size(path),
    }


def scenario_metadata(spec, *, rows, built_ok, error=None, threshold=None, old_info=None,
                      source_info=None, binaries=None, checks=None, observations=None,
                      notes=None, quiesce=None, steps=None, seconds=None):
    """The scenario.json document. Pure, so its shape is easy to test."""
    same_sysid = None
    if old_info and source_info:
        same_sysid = old_info['system_identifier'] == source_info['system_identifier']
    failed = sorted(name for name, check in (checks or {}).items() if not check['ok'])
    return {
        'id': spec.id,
        'name': spec.slug,
        'title': spec.title,
        'description': spec.old_copy,
        'predictions': {'lsn_skip': spec.lsn_prediction, 'tool': spec.tool_prediction},
        'built_ok': bool(built_ok),
        'error': error,
        'failed_checks': failed,
        'rows': rows,
        'built_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'build_seconds': seconds,
        'postgresql': binaries or {},
        'threshold': threshold,
        'old_copy': old_info,
        'source_backup': source_info,
        'same_system_identifier': same_sysid,
        'timelines': {'old_copy': (old_info or {}).get('timeline'),
                      'source_backup': (source_info or {}).get('timeline')},
        'data_checksum_versions': {'old_copy': (old_info or {}).get('data_checksum_version'),
                                   'source_backup': (source_info or {}).get('data_checksum_version')},
        'quiesce': quiesce,
        'checks': checks or {},
        'observations': observations or {},
        'notes': notes or [],
        'steps': steps or [],
    }


def prepare_scenario_dir(path, force=False):
    path = Path(path)
    if path.exists() and any(path.iterdir()):
        if not (path / 'scenario.json').is_file() and not force:
            raise RuntimeError(f'{path} is not empty and has no scenario.json; pass --force')
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path, document):
    Path(path).write_text(json.dumps(document, indent=2, sort_keys=False) + '\n')


def build_scenario(spec, out_root, *, rows=DEFAULT_ROWS, bins, default_bin, keep=False,
                   force=False, verbose=True):
    """Build one scenario under out_root/<id>-<slug>/ and return its metadata."""
    scenario_dir = prepare_scenario_dir(Path(out_root) / spec.dirname, force)
    ctx = BuildContext(scenario_dir, rows, bins, default_bin, keep=keep, verbose=verbose)
    result = {'built_ok': False}
    try:
        source = spec.builder(ctx)
        ctx.step(f'source is {source.name}; check it is quiesced, then take S')
        xid_before, others = xid_horizon(source), other_sessions(source)
        source.basebackup(ctx.source_backup)
        ctx.step('record the logical state of the source')
        state = compute_logical_state(source.query)
        write_json(scenario_dir / 'expected_state.json', state)
        xid_after = xid_horizon(source)
        result['quiesce'] = {'xid_horizon_before_s': xid_before,
                             'xid_horizon_after_state': xid_after,
                             'other_client_sessions': others,
                             'ok': xid_before == xid_after and others == 0}
        ctx.check('source_quiesced', result['quiesce']['ok'])
        result['binaries'] = {'old_copy': None, 'source_backup': source.pgbin.describe()}
        ctx.step('stop all servers')
        ctx.stop_all()
        old_info = describe_copy(ctx.old_copy, bins)
        source_info = describe_copy(ctx.source_backup, bins)
        result['binaries']['old_copy'] = bins[old_info['pg_version']].describe()
        threshold = select_threshold(
            ctx.old_copy, controldata_fn=bins[old_info['pg_version']].controldata_text)
        result.update(old_info=old_info, source_info=source_info, threshold=threshold)
        if spec.checker:
            spec.checker(ctx, old_info, source_info, threshold)
        ctx.step(f"LSN-skip analysis with threshold {threshold['lsn']} ({threshold['source']})")
        scan = lsnscan.scan(ctx.source_backup, ctx.old_copy, lsnscan.parse_lsn(threshold['lsn']))
        write_json(scenario_dir / 'lsnscan.json', scan)
        totals = scan['totals']
        ctx.step(f"compared {totals['pages_compared']} pages, skipped {totals['pages_skipped']}, "
                 f"wrongly skipped {totals['pages_wrongly_skipped']}")
        why = classify_wrong_skips(ctx.source_backup, ctx.old_copy,
                                   lsnscan.parse_lsn(threshold['lsn']))
        write_json(scenario_dir / 'wrong_skips.json', why)
        ctx.observations['wrong_skip_categories'] = {k: n for k, n in why['by_category'].items()
                                                     if n}
        ctx.check('classification_matches_lsnscan',
                  why['wrongly_skipped'] == totals['pages_wrongly_skipped'])
        result['built_ok'] = all(check['ok'] for check in ctx.checks.values())
    except Exception as exc:  # noqa: BLE001 - recorded in scenario.json
        result['error'] = f'{type(exc).__name__}: {exc}'
        result['traceback'] = traceback.format_exc()
        ctx.step(f'FAILED: {result["error"]}')
    finally:
        ctx.close()
    meta = scenario_metadata(
        spec, rows=rows, built_ok=result['built_ok'], error=result.get('error'),
        threshold=result.get('threshold'), old_info=result.get('old_info'),
        source_info=result.get('source_info'), binaries=result.get('binaries'),
        checks=ctx.checks, observations=ctx.observations, notes=ctx.notes,
        quiesce=result.get('quiesce'), steps=ctx.steps,
        seconds=round(time.monotonic() - ctx.started, 1))
    if result.get('traceback'):
        meta['traceback'] = result['traceback']
    write_json(scenario_dir / 'scenario.json', meta)
    return meta
