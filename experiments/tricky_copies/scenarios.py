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
import re
import shutil
import subprocess
import tempfile
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from experiments.tricky_copies import lsnscan, summaryscan
from experiments.tricky_copies.classify import classify_wrong_skips
from experiments.tricky_copies.logical import compute_logical_state
from experiments.tricky_copies.pgcluster import (REDO_KEY, START_WAL_KEY, Cluster, PgBin, clean_env, free_port,
                                                 parse_backup_label, parse_lsn_text, pg_major_of, run,
                                                 select_threshold, short_tempdir, tree_size)

BASE_CONF = {'autovacuum': 'off', 'checkpoint_timeout': '30min'}
# S2b and S2c: no time- or size-triggered checkpoint may run on P after the fork.
NO_CHECKPOINT_CONF = {'checkpoint_timeout': '1d', 'max_wal_size': '10GB'}
DEFAULT_ROWS = 200_000
SEGMENT_BLOCKS = 131072  # 1 GiB segments of 8 KiB blocks
FIRST_NORMAL_OID = 16384  # relations created by users, with their indexes and TOAST
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
# S2b and S2c (T11) also update a table that burst_sql never writes, so the source leaves
# those pages as they were before the fork. Their blocks in S keep LSNs below B's REDO
# location, so an LSN-threshold tool skips them and keeps B's diverged bytes: the
# scenarios expose such pages by construction, whatever burst 2 rewrites in accounts.
UNTOUCHED_TABLE = 'tags'
UNTOUCHED_DIVERGING_SQL = "UPDATE tags SET v = upper(v) WHERE k = 3;\n"
UNTOUCHED_BLOCKS_SQL = ("SELECT DISTINCT ((ctid::text)::point)[0]::bigint FROM tags "
                        "WHERE k = 3 ORDER BY 1;")


def evict_dirty_user_buffers_sql(major):
    """(dirty buffers, buffers evicted) for the user relations of the current database.

    pg_buffercache_evict() writes a dirty buffer out before evicting it, so it puts pages
    on disk without a checkpoint. It returns a boolean in PostgreSQL 17 and a record
    (buffer_evicted, buffer_flushed) from 18 on."""
    evicted = ('pg_buffercache_evict(b.bufferid)' if major < 18
               else '(pg_buffercache_evict(b.bufferid)).buffer_evicted')
    return f"""SELECT count(*), count(*) FILTER (WHERE {evicted})
FROM pg_buffercache b
JOIN pg_class c ON b.relfilenode = pg_relation_filenode(c.oid)
WHERE b.isdirty AND c.oid >= {FIRST_NORMAL_OID}
  AND b.reldatabase = (SELECT oid FROM pg_database WHERE datname = current_database());"""


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
    def __init__(self, scenario_dir, rows, bins, default_bin, keep=False, verbose=True,
                 source_conf=None, old_copy_conf=None):
        self.scenario_dir = Path(scenario_dir)
        # Extra server settings by role (T11; EXP-03 uses none): 'source' for the cluster
        # that becomes the source, 'old_copy' for the one whose data directory becomes B.
        # Each cluster runs with its role's settings from its creation on.
        self.role_conf = {'source': dict(source_conf or {}),
                          'old_copy': dict(old_copy_conf or {})}
        self.roles = {}  # cluster name -> the role whose settings it runs with
        self.old_copy_role = None  # the role of the cluster B was taken from, if B ran
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
                    db='postgres', role='source'):
        cluster = self.new_cluster(name, pgbin)
        flag = 'with' if checksums else 'without'
        self.step(f'initdb {name} ({cluster.pgbin.version_text}, {flag} data checksums)')
        cluster.initdb(checksums=checksums)
        cluster.append_conf({**BASE_CONF, **self.role_conf[role], **(conf or {})})
        self.roles[name] = role
        cluster.start()
        if load:
            self.step(f'load data on {name} ({self.rows} accounts rows)')
            cluster.psql(load_sql(self.rows, vacuum=vacuum), db)
        return cluster

    def new_standby(self, name, primary, role=None):
        """A streaming standby of ``primary``. With a ``role`` other than the primary's, it
        runs with that role's settings instead of those it copied from the primary."""
        standby = self.new_cluster(name, primary.pgbin)
        self.step(f'create standby {name} with pg_basebackup -R from {primary.name}')
        primary.basebackup(standby.datadir, extra=['-R'])
        inherited = self.roles.get(primary.name)
        self.roles[name] = role = role or inherited
        if role != inherited:
            self.switch_role_conf(standby, primary, inherited, role)
        standby.start()
        primary.wait_for("SELECT count(*) > 0 FROM pg_stat_replication WHERE state = 'streaming'")
        self.step(f'{name} is streaming from {primary.name}')
        return standby

    def switch_role_conf(self, standby, primary, inherited, role):
        """Append ``role``'s settings to a fresh clone's postgresql.conf, after resetting
        those of the primary's role to their built-in defaults (read from the primary)."""
        wanted = self.role_conf[role]
        stale = [name for name in self.role_conf.get(inherited, {}) if name not in wanted]
        defaults = {}
        if stale:
            names = ', '.join("'" + name.replace("'", "''") + "'" for name in stale)
            defaults = dict(primary.query(
                f'SELECT name, boot_val FROM pg_settings WHERE name IN ({names}) ORDER BY 1'))
        if defaults or wanted:
            self.step(f'{standby.name} gets the {role} settings {wanted}'
                      + (f' and the defaults {defaults}' if defaults else ''))
            standby.append_conf({**defaults, **wanted})

    def wait_replay(self, primary, standby):
        target = primary.scalar('SELECT pg_current_wal_lsn()')
        standby.wait_for(f"SELECT pg_last_wal_replay_lsn() >= '{target}'::pg_lsn")
        self.step(f'{standby.name} replayed up to {target}')
        return target

    def backup_old_copy(self, cluster):
        self.step(f'B = base backup of {cluster.name}')
        cluster.basebackup(self.old_copy)

    def adopt_datadir_as_old_copy(self, cluster, copy=False):
        self.old_copy_role = self.roles.get(cluster.name)
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


def segment_location(relpath, block):
    """(segment file, block within it) of a relation block, e.g. ('base/5/16384.1', 3)."""
    segment, offset = divmod(block, SEGMENT_BLOCKS)
    return (relpath if segment == 0 else f'{relpath}.{segment}'), offset


def pages_after_lsn(root, lsn):
    """Every relation page under root whose pd_lsn is beyond ``lsn`` (an integer), as
    [{'file', 'block', 'kind', 'lsn'}]; ``block`` counts within that segment file."""
    root = Path(root)
    pages = []
    for path in sorted(p for p in root.rglob('*') if p.is_file() and not p.is_symlink()):
        relpath = path.relative_to(root).as_posix()
        kind = lsnscan.relation_kind(relpath)
        if kind is None:
            continue
        with path.open('rb') as handle:
            block = 0
            while len(page := handle.read(lsnscan.BLOCK)) == lsnscan.BLOCK:
                page_lsn = lsnscan.page_lsn(page)
                if page_lsn > lsn:
                    pages.append({'file': relpath, 'block': block, 'kind': kind,
                                  'lsn': lsnscan.format_lsn(page_lsn)})
                block += 1
    return pages


def read_segment_page(root, relpath, block):
    """One 8 KiB page of one segment file, or None if the file or the block is absent."""
    path = Path(root, relpath)
    if not path.is_file():
        return None
    with path.open('rb') as handle:
        handle.seek(block * lsnscan.BLOCK)
        page = handle.read(lsnscan.BLOCK)
    return page if len(page) == lsnscan.BLOCK else None


def mark_exposed_pages(old, source, pages, redo):
    """Mark each of B's post-fork pages (from pages_after_lsn) with what S holds at the same
    file and block, and return how many are exposed.

    A page is exposed when S has that block with a page LSN at or below ``redo`` (B's REDO
    location, an integer) and other bytes than B: a tool that sends only the pages newer
    than B's REDO location skips it, and the copy keeps B's diverged bytes. Each entry
    gains 'source_lsn' (None when S has no such page) and 'exposed'."""
    exposed = 0
    for page in pages:
        source_page = read_segment_page(source, page['file'], page['block'])
        source_lsn = None if source_page is None else lsnscan.page_lsn(source_page)
        page['source_lsn'] = None if source_lsn is None else lsnscan.format_lsn(source_lsn)
        page['exposed'] = bool(
            source_lsn is not None and source_lsn <= redo
            and read_segment_page(old, page['file'], page['block']) != source_page)
        exposed += page['exposed']
    return exposed


def history_switchpoint(text):
    """LSN where the newest timeline of a timeline history file forked from its parent.

    Each line is "parentTLI<tab>switchpoint<tab>reason"; the last one is the newest fork."""
    entries = [line.split() for line in text.splitlines()
               if line.strip() and not line.lstrip().startswith('#')]
    if not entries or len(entries[-1]) < 2:
        raise ValueError('no switchpoint in timeline history')
    return lsnscan.format_lsn(lsnscan.parse_lsn(entries[-1][1]))


def flush_without_checkpoint(cluster, passes=5):
    """Write every dirty buffer of a user relation (all forks, indexes and TOAST included)
    to disk with pg_buffercache_evict(), until a pass finds none; no checkpoint runs."""
    sql = evict_dirty_user_buffers_sql(cluster.pgbin.major)
    evicted = 0
    for done_passes in range(1, passes + 1):
        dirty, written = (int(v) for v in cluster.query(sql)[0])
        evicted += written
        if dirty == 0:
            return {'passes': done_passes, 'buffers_evicted': evicted, 'clean': True}
    return {'passes': passes, 'buffers_evicted': evicted, 'clean': False}


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------
def build_s1(ctx, restartpoint=True):
    p = ctx.new_primary('primary')
    r = ctx.new_standby('standby', p, role='old_copy')
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


def record_diverged_blocks(ctx, primary, untouched=False):
    """Before the diverging writes: the accounts blocks they will update, and with
    ``untouched`` those of UNTOUCHED_TABLE too, in ctx.facts."""
    relpath = primary.scalar("SELECT pg_relation_filepath('accounts')")
    blocks = [int(row[0]) for row in primary.query(DIVERGED_BLOCKS_SQL)]
    ctx.facts['diverged'] = {'file': relpath, 'blocks': blocks}
    if untouched:
        ctx.facts['untouched'] = {
            'table': UNTOUCHED_TABLE,
            'file': primary.scalar(f"SELECT pg_relation_filepath('{UNTOUCHED_TABLE}')"),
            'blocks': [int(row[0]) for row in primary.query(UNTOUCHED_BLOCKS_SQL)]}


def diverging_writes(ctx, primary, untouched=False):
    """S2's diverging writes on the old primary, plus, with ``untouched``, an update of
    UNTOUCHED_TABLE, which the source never writes after the fork. Returns the LSN just
    before them: the standby that becomes the source has stopped, so everything written
    from there on lies after the fork."""
    before = primary.scalar('SELECT pg_current_wal_lsn()')
    accounts = len(ctx.facts['diverged']['blocks'])
    extra = ctx.facts.get('untouched') if untouched else None
    ctx.step(f'DIVERGING writes on {primary.name}: update rows in {accounts} pages of accounts '
             'and insert one' + (f", update rows in {len(extra['blocks'])} pages of "
                                  f"{extra['table']}" if extra else ''))
    primary.psql(diverging_sql(ctx.rows) + (UNTOUCHED_DIVERGING_SQL if untouched else ''))
    ctx.facts['old_primary_end_lsn'] = primary.scalar('SELECT pg_current_wal_lsn()')
    return before


def diverged_locations(ctx):
    """[(table, relation file, block)] of every page the diverging writes updated."""
    found = []
    for name, table in (('diverged', 'accounts'), ('untouched', None)):
        facts = ctx.facts.get(name)
        if facts:
            found += [(table or facts['table'], facts['file'], block) for block in facts['blocks']]
    return found


def flush_diverged_pages(ctx, cluster, before):
    """Write the dirty buffers of the user relations of ``cluster`` (the old primary in
    S2b, its hot standby in S2c) to disk without a checkpoint or restartpoint, then read
    the diverged pages back from disk: each must carry an LSN after ``before``."""
    ctx.step(f'write every dirty buffer of the user relations of {cluster.name} to disk with '
             'pg_buffercache_evict(), without a checkpoint or restartpoint')
    checkpoint_sql = 'SELECT checkpoint_lsn FROM pg_control_checkpoint()'
    checkpoint_before = cluster.scalar(checkpoint_sql)
    flush = flush_without_checkpoint(cluster)
    checkpoint_after = cluster.scalar(checkpoint_sql)
    threshold = lsnscan.parse_lsn(before)
    by_table = {}
    for table, relpath, block in diverged_locations(ctx):
        lsn = lsnscan.page_lsn(read_block(cluster.datadir / relpath, block) or b'')
        counts = by_table.setdefault(table, {'pages': 0, 'on_disk_after_that_lsn': 0})
        counts['pages'] += 1
        counts['on_disk_after_that_lsn'] += lsn is not None and lsn > threshold
    total = sum(c['pages'] for c in by_table.values())
    flushed = sum(c['on_disk_after_that_lsn'] for c in by_table.values())
    ctx.facts['flush'] = {**flush, 'flushed_on': cluster.name,
                          'lsn_before_diverging_writes': before,
                          'checkpoint_lsn_before_flush': checkpoint_before,
                          'checkpoint_lsn_after_flush': checkpoint_after,
                          'diverged_pages': total,
                          'diverged_pages_on_disk_after_that_lsn': flushed, 'by_table': by_table}
    ctx.step(f'{flushed} of {total} diverged pages carry a later LSN on disk of {cluster.name}; '
             f'its pg_control checkpoint went from {checkpoint_before} to {checkpoint_after}')
    return ctx.facts['flush']


def promote_and_write(ctx, r):
    """Start R alone, promote it to a new timeline and write burst 2 on it."""
    ctx.step('start R alone and promote it (new timeline)')
    r.start()
    r.promote()
    r.wait_for('SELECT NOT pg_is_in_recovery()')
    ctx.step('writes on the new primary R (burst 2)')
    r.psql(burst_sql(ctx.rows, 2))
    return r


def build_s2(ctx, crash=False, switch_before_split=False):
    """S2, or with ``crash`` S2b: P flushes its diverged pages without a checkpoint and
    stops with -m immediate, so B's REDO location stays before the fork. S2b's diverging
    writes also update UNTOUCHED_TABLE (EXP-08's exposed pages); S2's are EXP-03's.
    ``switch_before_split`` (S2e, EXP-80): a WAL segment switch after P's last checkpoint, so its
    checkpoint record lies in an earlier segment than the split."""
    p = ctx.new_primary('primary', conf=NO_CHECKPOINT_CONF if crash else None, role='old_copy')
    if crash:
        ctx.step('CREATE EXTENSION pg_buffercache on P, before R is cloned, so R has it too')
        p.psql('CREATE EXTENSION pg_buffercache;')
    r = ctx.new_standby('standby', p, role='source')
    ctx.step('writes on P (burst 1), replicated to R')
    p.psql(burst_sql(ctx.rows, 1))
    p.psql('CHECKPOINT;')
    if switch_before_split:
        ctx.step('switch P to a new WAL segment and write again, so the split lies past the checkpoint record\'s '
                 'segment')
        p.psql('SELECT pg_switch_wal();')
        p.psql(burst_sql(ctx.rows, 2, vacuum=False))
    split = ctx.wait_replay(p, r)
    ctx.step('stop R cleanly: this is the split point')
    r.stop('fast')
    ctx.facts['split_lsn'] = split
    record_diverged_blocks(ctx, p, untouched=crash)
    before = diverging_writes(ctx, p, untouched=crash)
    if crash:
        flush_diverged_pages(ctx, p, before)
        ctx.step('stop P with -m immediate (no shutdown checkpoint)')
        p.stop('immediate')
    else:
        ctx.step('stop P cleanly (-m fast)')
        p.stop('fast')
    ctx.adopt_datadir_as_old_copy(p)
    return promote_and_write(ctx, r)


WAL_SEGMENT = re.compile(r'^([0-9A-F]{8})([0-9A-F]{8})([0-9A-F]{8})$')
WAL_SEGMENT_BYTES = 16 * 1024 * 1024  # initdb's default


def build_s2d(ctx):
    """S2d (EXP-62): S2b, then B loses its WAL from the segment holding the split on, as if deleted or never
    copied. Its pages keep the diverged writes; nothing left in B shows it went past the split."""
    source = build_s2(ctx, crash=True)
    split = lsnscan.parse_lsn(ctx.facts['split_lsn'])
    first = split // WAL_SEGMENT_BYTES
    removed = []
    for path in sorted((ctx.old_copy / 'pg_wal').iterdir()):
        match = WAL_SEGMENT.match(path.name)
        if match and (int(match.group(2), 16) << 32 | int(match.group(3), 16) << 24) // WAL_SEGMENT_BYTES >= first:
            path.unlink()
            removed.append(path.name)
    ctx.step(f"delete B's WAL from the split's segment on: {len(removed)} segments")
    ctx.facts['wal_removed'] = removed
    ctx.check('s2d_wal_removed', bool(removed), segments=removed)
    return source


def build_s2e(ctx):
    """S2e (EXP-80): S2d, with P's last checkpoint record in an earlier WAL segment than the split, so B's
    deleted WAL (the split's segment on) leaves that record. Crash recovery can then run on B: it replays to the
    end of the WAL left, before the split, and marks B shut down, while B's pages keep the diverged writes."""
    source = build_s2(ctx, crash=True, switch_before_split=True)
    split = lsnscan.parse_lsn(ctx.facts['split_lsn'])
    first = split // WAL_SEGMENT_BYTES
    removed = []
    for path in sorted((ctx.old_copy / 'pg_wal').iterdir()):
        match = WAL_SEGMENT.match(path.name)
        if match and (int(match.group(2), 16) << 32 | int(match.group(3), 16) << 24) // WAL_SEGMENT_BYTES >= first:
            path.unlink()
            removed.append(path.name)
    ctx.step(f"delete B's WAL from the split's segment on: {len(removed)} segments")
    ctx.facts['wal_removed'] = removed
    ctx.check('s2e_wal_removed', bool(removed), segments=removed)
    return source


def build_s2h(ctx, checksums=True):
    """S2h, or without ``checksums`` S2i (EXP-69b): the hint-bit hazard of a diverged old primary. A transaction on P
    inserts rows into a table of its own and stays open; a checkpoint follows, then the standby R stops: the fork. P
    then commits the transaction and reads the table, setting XMIN_COMMITTED on every row, and stops cleanly. S (R
    promoted) never saw the commit, so for S the rows don't exist. The hints are in P's WAL only with data checksums
    or wal_log_hints (S2h has checksums; S2i has neither)."""
    p = ctx.new_primary('primary', checksums=checksums, conf={'wal_log_hints': 'off'}, role='old_copy')
    r = ctx.new_standby('standby', p, role='source')
    p.psql('CREATE TABLE hazard (id int, pad text);')
    ctx.step('open a transaction on P that inserts 20,000 rows into hazard and stays open')
    session = subprocess.Popen([str(p.pgbin.tool('psql')), '-X', '-q', '-v', 'ON_ERROR_STOP=1', '--no-password',
                                *[str(a) for a in p.conn_args()]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, env=clean_env())
    session.stdin.write("BEGIN;\nINSERT INTO hazard SELECT g, repeat('x', 100) FROM generate_series(1, 20000) g;\n")
    session.stdin.flush()
    p.wait_for("SELECT count(*) = 1 FROM pg_stat_activity WHERE state = 'idle in transaction' "
               "AND backend_xid IS NOT NULL")
    ctx.facts['hazard_xid'] = p.scalar("SELECT backend_xid FROM pg_stat_activity WHERE state = 'idle in transaction'")
    p.psql('CHECKPOINT;')
    split = ctx.wait_replay(p, r)
    ctx.step('stop R cleanly: this is the split point, with the transaction still open on P')
    r.stop('fast')
    ctx.facts['split_lsn'] = split
    ctx.step('commit the transaction on P after the fork, then read hazard there (setting its hint bits)')
    out, err = session.communicate('COMMIT;\n', timeout=300)
    ctx.check('hazard_committed_on_p', session.returncode == 0, stderr=err[-500:])
    ctx.check('hazard_rows_on_p', p.scalar('SELECT count(*) FROM hazard') == '20000')
    ctx.facts['old_primary_end_lsn'] = p.scalar('SELECT pg_current_wal_lsn()')
    ctx.step('stop P cleanly (-m fast)')
    p.stop('fast')
    ctx.adopt_datadir_as_old_copy(p)
    source = promote_and_write(ctx, r)
    ctx.check('hazard_absent_on_s', source.scalar('SELECT count(*) FROM hazard') == '0')
    return source


def build_s2c(ctx):
    """S2c: B is a hot standby Q of the old primary P. Q replays P's diverging writes,
    writes them to disk without a restartpoint and stops cleanly. P never checkpoints after
    the fork, so Q's shutdown restartpoint keeps the REDO location of the last checkpoint
    it replayed, from before the fork: B is "shut down in recovery" with REDO before the
    fork and post-fork pages on disk. P is then discarded, and R becomes the source."""
    p = ctx.new_primary('primary', conf=NO_CHECKPOINT_CONF, role='source')
    ctx.step('CREATE EXTENSION pg_buffercache on P, before R and Q are cloned, so they have it')
    p.psql('CREATE EXTENSION pg_buffercache;')
    r = ctx.new_standby('standby', p, role='source')
    q = ctx.new_standby('follower', p, role='old_copy')
    ctx.step('writes on P (burst 1), replicated to R and Q')
    p.psql(burst_sql(ctx.rows, 1))
    p.psql('CHECKPOINT;')
    split = ctx.wait_replay(p, r)
    ctx.wait_replay(p, q)
    ctx.step('stop R cleanly: this is the split point')
    r.stop('fast')
    ctx.facts['split_lsn'] = split
    record_diverged_blocks(ctx, p, untouched=True)
    before = diverging_writes(ctx, p, untouched=True)
    ctx.facts['follower_replay_lsn'] = ctx.wait_replay(p, q)
    flush_diverged_pages(ctx, q, before)
    ctx.step('stop Q cleanly (-m fast), before P can write a checkpoint record Q would replay')
    q.stop('fast')
    ctx.step('stop P with -m immediate and discard it')
    p.stop('immediate')
    ctx.clusters.remove(p)
    shutil.rmtree(p.datadir, ignore_errors=True)
    ctx.adopt_datadir_as_old_copy(q)
    return promote_and_write(ctx, r)


def check_s2(ctx, old_info, source_info, threshold):
    """EXP-03's S2 checks, plus B's post-fork pages and how many of them are exposed (see
    record_post_fork_pages). Returns (fork LSN as an integer, post-fork pages)."""
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
    return record_post_fork_pages(ctx, old_info, source_info)


def record_post_fork_pages(ctx, old_info, source_info):
    """Record the fork LSN (from S's timeline history file), B's REDO, checkpoint and
    minimum recovery locations, and every page of B whose on-disk LSN is after the fork,
    each marked with S's LSN at that block and whether it is exposed (mark_exposed_pages).
    The 'exposed_pages' check fails the build when none is: the scenario could then not
    show an LSN-threshold tool keeping B's diverged bytes."""
    history = ctx.source_backup / 'pg_wal' / f"{source_info['timeline']:08X}.history"
    fork = history_switchpoint(history.read_text())
    fork_value = lsnscan.parse_lsn(fork)
    redo = old_info['redo_location']
    post_fork = pages_after_lsn(ctx.old_copy, fork_value)
    exposed = mark_exposed_pages(ctx.old_copy, ctx.source_backup, post_fork,
                                 lsnscan.parse_lsn(redo))
    by_kind, by_file = {}, {}
    for page in post_fork:
        if page['exposed']:
            by_kind[page['kind']] = by_kind.get(page['kind'], 0) + 1
            by_file[page['file']] = by_file.get(page['file'], 0) + 1
    ctx.observations.update(
        fork_lsn=fork, fork_lsn_source=f'pg_wal/{history.name} of S', old_copy_redo_lsn=redo,
        old_copy_checkpoint_lsn=old_info['checkpoint_location'],
        old_copy_min_recovery_lsn=old_info.get('min_recovery_location'),
        post_fork_page_count=len(post_fork), exposed_pages=exposed,
        exposed_pages_by_kind=by_kind, exposed_pages_by_file=by_file,
        exposed_rule="S holds the block with a page LSN at or below B's REDO location, "
                     'and other bytes than B', post_fork_pages=post_fork)
    ctx.check('exposed_pages', exposed > 0, exposed_pages=exposed,
              post_fork_pages=len(post_fork), redo_lsn=redo, fork_lsn=fork)
    return fork_value, post_fork


def check_after_fork(ctx, old_info, fork_value, post_fork):
    """S2b's and S2c's checks: B's REDO and checkpoint locations lie before the fork, the
    flush ran no checkpoint or restartpoint (the flushing cluster's pg_control_checkpoint()
    location is the same before and after it), and every page the diverging writes updated
    is on B's disk with an LSN after the fork."""
    redo, checkpoint = old_info['redo_location'], old_info['checkpoint_location']
    ctx.check('no_checkpoint_after_fork', lsnscan.parse_lsn(redo) < fork_value
              and lsnscan.parse_lsn(checkpoint) < fork_value,
              fork_lsn=lsnscan.format_lsn(fork_value), redo_lsn=redo, checkpoint_lsn=checkpoint)
    flush = ctx.facts.get('flush') or {}
    before_flush = flush.get('checkpoint_lsn_before_flush')
    after_flush = flush.get('checkpoint_lsn_after_flush')
    ctx.check('no_checkpoint_during_flush', before_flush is not None
              and before_flush == after_flush, flushed_on=flush.get('flushed_on'),
              checkpoint_lsn_before_flush=before_flush, checkpoint_lsn_after_flush=after_flush)
    listed = {(page['file'], page['block']) for page in post_fork}
    written = diverged_locations(ctx)
    found = sum(segment_location(relpath, block) in listed for _table, relpath, block in written)
    ctx.check('diverged_pages_on_disk_after_fork', written and found == len(written),
              pages_written_after_split=len(written), pages_with_lsn_after_fork=found,
              flush=ctx.facts.get('flush'))


def check_s2b(ctx, old_info, source_info, threshold):
    """S2's checks, plus: B crashed, its REDO and checkpoint lie before the fork, and the
    pages P wrote after the fork are on disk."""
    fork_value, post_fork = check_s2(ctx, old_info, source_info, threshold)
    ctx.check('old_copy_crashed', old_info['cluster_state'] == 'in production',
              cluster_state=old_info['cluster_state'])
    check_after_fork(ctx, old_info, fork_value, post_fork)


def check_s2c(ctx, old_info, source_info, threshold):
    """S2's checks, plus: B is a standby stopped cleanly ("shut down in recovery"), its
    REDO and checkpoint lie before the fork, and the pages it replayed after the fork are
    on disk."""
    fork_value, post_fork = check_s2(ctx, old_info, source_info, threshold)
    ctx.check('old_copy_is_stopped_standby', old_info['cluster_state'] == 'shut down in recovery',
              cluster_state=old_info['cluster_state'])
    ctx.observations['follower_replay_lsn'] = ctx.facts.get('follower_replay_lsn')
    check_after_fork(ctx, old_info, fork_value, post_fork)


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
    new.append_conf({**BASE_CONF, **ctx.role_conf['source']})
    ctx.roles[new.name] = 'source'
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
    Scenario('S2b', 'ex-primary-no-checkpoint-after-fork',
             'Ex-primary that flushed diverged pages after a failover without a checkpoint',
             'data directory of the old primary, stopped with -m immediate after its diverged '
             'pages were written out without a checkpoint (REDO before the fork)',
             'wrong', 'correct; diverged pages detected and re-sent',
             partial(build_s2, crash=True), check_s2b),
    Scenario('S2c', 'standby-of-ex-primary-after-fork',
             'Standby that followed the ex-primary past a failover, stopped cleanly',
             'data directory of a hot standby of the old primary that replayed its diverged '
             'writes, wrote them to disk without a restartpoint and stopped with -m fast '
             '(shut down in recovery, REDO before the fork)',
             'wrong', 'correct; diverged pages detected and re-sent', build_s2c, check_s2c),
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
# Built only when named: EXP-62's, beside EXP-03's list so its runs and T11's stay as they were.
EXTRA_SCENARIOS = [
    Scenario('S2d', 'ex-primary-wal-lost', 'Ex-primary after a failover, its WAL past the split lost',
             "data directory of the old primary as S2b's, with its WAL segments from the split's on deleted",
             'wrong', 'correct; diverged pages detected and re-sent', build_s2d, None),
    # EXP-80's: S2d with the old primary's last checkpoint record left, so crash recovery can run on it.
    Scenario('S2e', 'ex-primary-wal-lost-recoverable', 'Ex-primary after a failover, its WAL past the split lost, '
             'its last checkpoint record kept',
             "data directory of the old primary as S2b's, after a WAL segment switch past its checkpoint, with its "
             "WAL segments from the split's on deleted", 'wrong', 'correct; refused by guard v2', build_s2e, None),
    # EXP-69b's: a transaction open at the fork, committed and read on the old primary only.
    Scenario('S2h', 'hint-hazard-checksums', 'Ex-primary that committed and read a transaction open at the fork',
             'data directory of the old primary, with data checksums, whose hint bits name a transaction S never '
             'committed', 'wrong', 'correct; diverged pages detected and re-sent', build_s2h, None),
    Scenario('S2i', 'hint-hazard-no-checksums', 'As S2h, without data checksums or wal_log_hints',
             "data directory of the old primary as S2h's, its hint bits in no WAL", 'wrong',
             'correct; diverged pages detected and re-sent', partial(build_s2h, checksums=False), None),
]


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
        'min_recovery_location': fields.get('Minimum recovery ending location'),
        'data_checksum_version': int(fields['Data page checksum version']),
        'wal_log_hints': fields.get('wal_log_hints setting'),
        'catalog_version': fields.get('Catalog version number'),
        'backup_label': label,
        'bytes': tree_size(path),
    }


def scenario_metadata(spec, *, rows, built_ok, error=None, threshold=None, old_info=None,
                      source_info=None, binaries=None, checks=None, observations=None,
                      notes=None, quiesce=None, steps=None, seconds=None, source_cluster=None,
                      server_conf=None):
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
        'source_cluster': source_cluster,
        'server_conf': server_conf or {'source': {}, 'old_copy': {}},
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


# EXP-61: the source's WAL summaries.
WAL_SUMMARIES_CONF = {'summarize_wal': 'on'}


def dump_wal_summaries(cluster, source_backup, timeout=120):
    """Every WAL summary the source's summarizer wrote, once it has passed S's start, with S's timeline history.

    PostgreSQL 17 ends a summary at each checkpoint's redo record, so once the summarizer has passed S's START
    WAL LOCATION the summaries up to it are complete. Each summary keeps its blocks and its limit blocks (a
    fork's truncation or creation) by "tablespace/database/relfilenode/fork"."""
    start = parse_lsn_text(parse_backup_label((Path(source_backup) / 'backup_label').read_text())[START_WAL_KEY])
    result = {'s_start': start, 'server_version': cluster.scalar('SHOW server_version'), 'error': None}
    try:
        cluster.wait_for(f"SELECT summarized_lsn >= '{start}'::pg_lsn FROM pg_get_wal_summarizer_state()",
                         timeout=timeout)
    except TimeoutError as exc:
        result['error'] = str(exc)
    result['summarizer'] = cluster.query('SELECT summarized_tli, summarized_lsn FROM pg_get_wal_summarizer_state()')[0]
    summaries = []
    for tli, begin, end in cluster.query('SELECT tli, start_lsn, end_lsn FROM pg_available_wal_summaries() '
                                         'ORDER BY tli, start_lsn'):
        blocks, limits = {}, {}
        for ts, db, rel, fork, block, is_limit in cluster.query(
                'SELECT reltablespace, reldatabase, relfilenode, relforknumber, relblocknumber, is_limit_block '
                f"FROM pg_wal_summary_contents({tli}, '{begin}', '{end}')"):
            key = f'{ts}/{db}/{rel}/{fork}'
            if is_limit == 't':
                limits[key] = min(int(block), limits.get(key, int(block)))
            else:
                blocks.setdefault(key, []).append(int(block))
        summaries.append({'tli': int(tli), 'start': begin, 'end': end, 'blocks': blocks, 'limits': limits})
    result['summaries'] = summaries
    result['history'] = {p.name: p.read_text() for p in sorted((Path(cluster.datadir) / 'pg_wal').glob('*.history'))}
    return result


def build_scenario(spec, out_root, *, rows=DEFAULT_ROWS, bins, default_bin, keep=False,
                   force=False, verbose=True, source_conf=None, old_copy_conf=None, wal_summaries=False):
    """Build one scenario under out_root/<id>-<slug>/ and return its metadata.

    ``source_conf`` adds server settings to the cluster that becomes the source, from its
    creation on; ``old_copy_conf`` to the one whose data directory becomes B (S1, S1b, S2,
    S2b and S2c, where that is a cluster of its own). EXP-03 uses neither; T11 gives the
    source wal_keep_size. scenario.json records both, and with which role's settings
    the source and B's cluster ran.

    ``wal_summaries`` (EXP-61) runs the source with summarize_wal on, dumps its summaries before the servers
    stop, and scores the summary path on the scenario (summaryscan.json)."""
    if wal_summaries:
        source_conf = {**(source_conf or {}), **WAL_SUMMARIES_CONF}
    scenario_dir = prepare_scenario_dir(Path(out_root) / spec.dirname, force)
    ctx = BuildContext(scenario_dir, rows, bins, default_bin, keep=keep, verbose=verbose,
                       source_conf=source_conf, old_copy_conf=old_copy_conf)
    result = {'built_ok': False}
    try:
        source = spec.builder(ctx)
        result['source_role'] = ctx.roles.get(source.name)
        if ctx.role_conf['source']:
            ctx.check('source_conf_applied', result['source_role'] == 'source',
                      cluster=source.name, ran_with=result['source_role'])
        if ctx.role_conf['old_copy']:
            ctx.check('old_copy_conf_applied', ctx.old_copy_role == 'old_copy',
                      ran_with=ctx.old_copy_role)
        # With --keep the source stays, stopped, for tools that need a live server (T11).
        result['source_cluster'] = {'name': source.name, 'kept': keep,
                                    'datadir': source.datadir.relative_to(scenario_dir).as_posix()}
        ctx.step(f'source is {source.name}; check it is quiesced, then take S')
        xid_before, others = xid_horizon(source), other_sessions(source)
        source.basebackup(ctx.source_backup)
        ctx.step('record the logical state of the source')
        state = compute_logical_state(source.query)
        write_json(scenario_dir / 'expected_state.json', state)
        if wal_summaries:
            ctx.step("wait for the WAL summarizer to pass S's start, then dump the summaries")
            write_json(scenario_dir / 'wal_summaries.json', dump_wal_summaries(source, ctx.source_backup))
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
        if wal_summaries:
            ctx.step('summary-path analysis (EXP-61)')
            scored = summaryscan.score_scenario(scenario_dir, old_info, source_info, threshold,
                                                bins[old_info['pg_version']].tool('pg_waldump'))
            write_json(scenario_dir / 'summaryscan.json', scored)
            ctx.step(f"guard {'passes' if scored['guard']['ok'] else 'refuses'}; naive path leaves "
                     f"{scored['naive']['wrong']} pages wrong ({scored['naive']['real_wrong']} real)")
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
        seconds=round(time.monotonic() - ctx.started, 1),
        source_cluster=result.get('source_cluster'),
        server_conf={**ctx.role_conf, 'source_ran_with': result.get('source_role'),
                     'old_copy_ran_with': ctx.old_copy_role})
    if result.get('traceback'):
        meta['traceback'] = result['traceback']
    write_json(scenario_dir / 'scenario.json', meta)
    return meta
