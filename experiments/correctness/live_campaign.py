"""EXP-25: a randomized campaign for v1's in-place refresh from a live source (docs/experiments/EXP-25-live-correctness.md).

    .venv/bin/python experiments/correctness/live_campaign.py --out results/correctness/exp25 [--trials 1000]
        [--hours 3] [--first 0] [--negative-every 20] [--pg-bin /opt/homebrew/opt/postgresql@17/bin] [--work DIR]

S, an APFS clone of a template cluster, runs for the whole campaign under EXP-25's bank load (bank.py: pgbench,
4 clients at 2,000 transactions a second, every committed one keeping sum(abalance) = sum(n) and appending its
ledger row). B starts as another clone of the template and is refreshed in place by every trial, so each trial's B is
the previous trial's output. Each trial (seeded by its number):

1. runs 1-4 random DDL steps on S, between refreshes (and trims S's ledger to its last 2 minutes), then takes the
   logical digest of every table the load does not write, which the refresh cannot change;
2. applies one random fault to B: none, diverged (B recovers, runs its own load and DDL, stops), kill (the refresh is
   SIGKILLed part-way, B is judged as it was left, then the refresh is rerun), corrupt (bytes overwritten in relation
   files of B) or truncate (a relation file of B truncated or deleted);
3. refreshes B in place from the live S: `pgdelta local --in-place --source-kind live`;
4. checks B independently of pgdelta, on a disposable APFS clone that it starts: it must recover from backup_label
   to the run's stop LSN, its bank state must keep the invariant and lie between pg_backup_start and pg_backup_stop
   (bank.judge, against S's ledger, with pgdelta's reported times bracketed by the harness's own), the digest of
   step 1 must be equal, and pg_amcheck (heapallindexed, every database) must find nothing; and no pgdelta slot or
   session may be left on S.

In each block of `--negative-every` trial numbers, two are negative controls instead of counted trials (fault none):
the one half-way through runs pgdelta's broken variant that reads the files before pg_backup_start is called
(PGDELTA_TEST_READ_BEFORE_START=1), and the block's last one the variant that reads them while pg_backup_start runs
(=during). The checks must find their outputs wrong. pgdelta exits 4 for either variant even when the receiver
committed B; such a run counts as run to completion.

One JSON line per trial goes to OUT/trials.jsonl, and the tallies to OUT/summary.json (summarize): the counted trials,
the share whose refresh's WAL spans two segments or more, and each negative control's catches. A wrong output, or a
refresh that fails, stops the campaign (exit status 2); a pgdelta slot or session left on S stops it (exit status 4);
ten harness errors in a row stop it (3).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import bank  # noqa: E402
from campaign import HarnessError, digest, pgdelta_own, run  # noqa: E402

os.environ.update(LC_ALL='C', LANG='C')  # PostgreSQL on macOS refuses to start without a valid locale
FAULTS = [('none', 40), ('diverged', 15), ('kill', 25), ('corrupt', 10), ('truncate', 10)]
CLIENTS, SCALE, RATE = 4, 3, 2000
ACCOUNTS = SCALE * bank.ACCOUNTS_PER_SCALE
LOAD_TABLES = ('pgbench_accounts', 'load_progress', 'load_ledger')
LEDGER_KEEP = "interval '2 minutes'"
BASE_PORT = 55480
SOURCE_PORT, COPY_PORT, KILLED_PORT, DIVERGED_PORT = BASE_PORT, BASE_PORT + 1, BASE_PORT + 2, BASE_PORT + 3
# Smoke revision: with the default shared buffers the whole cluster stays in memory and its files change only at
# checkpoints, so pgdelta read them unchanged (0 pages changed between hashing and sending in 15 refreshes). Small
# shared buffers and an eager background writer put the load's pages into the files while they are read.
# Review revision: 8 MB, not 1 MB, so that some of the load's changes are still only in shared buffers when a backup
# starts (with 1 MB a read during the start checkpoint was caught in 2 of 61 trials, with 8 MB in 9 of 9), while the
# files still change as they are read (30-191 pages per refresh in a probe; 0 with 32 MB).
SETTINGS = ("-c listen_addresses='' -c fsync=off -c max_wal_size=256MB -c checkpoint_timeout=30s "
            '-c shared_buffers=8MB -c bgwriter_delay=10ms -c bgwriter_lru_maxpages=1000')
WAL_SEGMENT_MB = 1  # review revision: a refresh's 8-16 MB of WAL then spans many segments, not one 16 MB one
SLOT_WAIT_SECONDS = 10
USER = 'postgres'
# pgdelta's in-place block (pgdelta/inplace.py): what a B killed between the block and the commit must show.
CONTROL_FILE, CONTROL_PENDING, MARKER = 'global/pg_control', 'global/pg_control.pgdelta-incomplete', \
    'pgdelta-incomplete.json'
# The negative controls: pgdelta's broken variants and the value of PGDELTA_TEST_READ_BEFORE_START for each.
VARIANTS = {'before_start': '1', 'during_start': 'during'}
CONTROL_SHARE = 0.9  # registered: each negative control caught in at least 90% of its completed runs
MULTI_SEGMENT_SHARE = 0.9  # registered (review revision): the bound covers the WAL range only at this share or more


class Cluster:
    def __init__(self, bindir, sock, data, port):
        self.bin, self.sock, self.data, self.port = Path(bindir), sock, Path(data), port

    def start(self, check=True):
        return run([str(self.bin / 'pg_ctl'), '-D', str(self.data), '-w', '-t', '120', '-l', f'{self.data}.log',
                    '-o', f'-p {self.port} -k {self.sock} {SETTINGS}', 'start'], check=check)

    def stop(self, mode='fast', check=True):
        result = run([str(self.bin / 'pg_ctl'), '-D', str(self.data), '-w', '-t', '120', '-m', mode, 'stop'],
                     check=False)
        if result.returncode != 0 and mode != 'immediate':
            result = run([str(self.bin / 'pg_ctl'), '-D', str(self.data), '-w', '-m', 'immediate', 'stop'],
                         check=False)
        if check and result.returncode != 0 and (self.data / 'postmaster.pid').exists():
            raise HarnessError(f'cannot stop {self.data}: {result.stderr.strip()[-300:]}')

    def sql(self, text, db=bank.DATABASE):
        return run([str(self.bin / 'psql'), '-h', self.sock, '-p', str(self.port), '-U', USER, '-d', db, '-qAtX',
                    '-v', 'ON_ERROR_STOP=1', '-c', text]).stdout.strip()

    def conninfo(self):
        return f'host={self.sock} port={self.port} user={USER} dbname=postgres'

    def log_tail(self, size=1500):
        try:
            return Path(f'{self.data}.log').read_text(errors='replace')[-size:]
        except OSError:
            return ''


def clone(source, target):
    shutil.rmtree(target, ignore_errors=True)
    for name in ('.log',):
        Path(f'{target}{name}').unlink(missing_ok=True)
    run(['cp', '-c', '-R', str(source), str(target)])


def make_template(bindir, sock, path):
    """The template cluster, with 1 MiB WAL segments and on timeline 2 (review revision: so that every refresh's WAL
    spans several segments, with a timeline history file to send, as a large refresh's would)."""
    run([str(Path(bindir) / 'initdb'), '-D', str(path), '-U', USER, '-A', 'trust', '--no-sync',
         '--wal-segsize', str(WAL_SEGMENT_MB)])
    cluster = Cluster(bindir, sock, path, SOURCE_PORT)
    cluster.start()
    cluster.sql(f'CREATE DATABASE {bank.DATABASE}', db='postgres')
    run([str(Path(bindir) / 'pgbench'), '-i', '-q', '-s', str(SCALE), '-h', sock, '-p', str(SOURCE_PORT), '-U', USER,
         bank.DATABASE])
    for statement in bank.setup_statements(CLIENTS):
        cluster.sql(statement)
    cluster.sql('CREATE TABLE notes (id int PRIMARY KEY, body text); '
                "INSERT INTO notes SELECT g, repeat(md5(g::text), 8) FROM generate_series(1, 20000) g")
    cluster.sql('CHECKPOINT')
    cluster.stop()
    # An archive recovery that finds no archive ends at once and promotes: the cluster moves to timeline 2.
    conf = Path(path) / 'postgresql.conf'
    original = conf.read_text()
    conf.write_text(original + "\nrestore_command = 'false'\n")
    (Path(path) / 'recovery.signal').write_text('')
    cluster.start()
    timeline = cluster.sql('SELECT timeline_id FROM pg_control_checkpoint()', db='postgres')
    cluster.sql('CHECKPOINT', db='postgres')
    cluster.stop()
    conf.write_text(original)
    if timeline != '2' or not (Path(path) / 'pg_wal' / '00000002.history').exists():
        raise HarnessError(f'the template did not move to timeline 2 (timeline {timeline})')


# --- the load and the DDL on S -------------------------------------------------------------------------------

def start_load(cluster, scripts, seed, clients=CLIENTS, seconds=86400, rate=RATE, log=None):
    argv = bank.pgbench_argv(cluster.bin / 'pgbench', scripts, clients=clients, scale=SCALE, seconds=seconds,
                             seed=seed, host=cluster.sock, port=cluster.port, user=USER, rate=rate)
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=log or subprocess.DEVNULL,
                            start_new_session=True)


def stop_load(process):
    if process is not None and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def existing(cluster, pattern):
    return cluster.sql(f"SELECT relname FROM pg_class WHERE relkind = 'r' AND relname LIKE '{pattern}' "
                       f'ORDER BY 1').split()


def ddl(cluster, rng):
    """1-4 random DDL steps between refreshes; the ledger trimmed to its last 2 minutes. Returns their names."""
    steps = []
    for _ in range(rng.randint(1, 4)):
        kind = rng.choice(['ctas', 'ctas', 'unlogged', 'drop', 'truncate', 'vacuum_full', 'vacuum_full', 'index',
                           'reindex', 'update', 'delete', 'insert', 'checkpoint', 'createdb', 'dropdb'])
        n = rng.randint(1, 10**6)
        if kind == 'ctas':
            cluster.sql(f'CREATE TABLE IF NOT EXISTS extra_{n % 7} AS SELECT g, md5((g::bigint * {n})::text) AS v '
                        f'FROM generate_series(1, {rng.randint(100, 50000)}) g')
        elif kind == 'unlogged':
            cluster.sql(f'CREATE UNLOGGED TABLE IF NOT EXISTS ulog_{n % 3} AS SELECT g FROM '
                        f'generate_series(1, {rng.randint(100, 20000)}) g')
        elif kind == 'drop':
            tables = existing(cluster, 'extra_%') + existing(cluster, 'ulog_%')
            if tables:
                cluster.sql(f'DROP TABLE {rng.choice(tables)}')
        elif kind == 'truncate':
            tables = existing(cluster, 'extra_%')
            if tables:
                cluster.sql(f'TRUNCATE {rng.choice(tables)}')
        elif kind == 'vacuum_full':
            cluster.sql(f'VACUUM FULL {rng.choice(list(LOAD_TABLES) + ["notes"] + existing(cluster, "extra_%"))}')
        elif kind == 'index':
            cluster.sql(f'CREATE INDEX IF NOT EXISTS ix_{n % 5} ON notes ((left(body, {1 + n % 20})))')
        elif kind == 'reindex':
            cluster.sql(f'REINDEX TABLE {rng.choice(["pgbench_accounts", "load_ledger", "notes"])}')
        elif kind == 'update':
            low = rng.randint(1, 20000)
            cluster.sql(f"UPDATE notes SET body = md5(id::text || '{n}') WHERE id BETWEEN {low} AND "
                        f'{low + rng.randint(1, 3000)}')
        elif kind == 'delete':
            low = rng.randint(1, 20000)
            cluster.sql(f'DELETE FROM notes WHERE id BETWEEN {low} AND {low + rng.randint(1, 500)}')
        elif kind == 'insert':
            low = rng.randint(20001, 40000)
            cluster.sql(f'INSERT INTO notes SELECT g, md5(g::text) FROM generate_series({low}, '
                        f'{low + rng.randint(1, 2000)}) g ON CONFLICT DO NOTHING')
        elif kind == 'checkpoint':
            cluster.sql('CHECKPOINT')
        elif kind == 'createdb':
            strategy = rng.choice(['wal_log', 'file_copy'])
            if f'scratch_{n % 3}' not in cluster.sql('SELECT datname FROM pg_database', db='postgres').split():
                cluster.sql(f'CREATE DATABASE scratch_{n % 3} STRATEGY {strategy}', db='postgres')
            kind = f'createdb_{strategy}'
        else:
            names = [name for name in cluster.sql('SELECT datname FROM pg_database', db='postgres').split()
                     if name.startswith('scratch_')]
            if names:
                cluster.sql(f'DROP DATABASE {rng.choice(names)} WITH (FORCE)', db='postgres')
        steps.append(kind)
    cluster.sql(f'DELETE FROM load_ledger WHERE ts < now() - {LEDGER_KEEP}')
    return steps


def static_digest(cluster):
    """Every table the load does not write (their rows; an unlogged one is only noted, recovery empties it), the
    schema, and the databases. Nothing changes them during a refresh, so a copy must give exactly this."""
    out = {}
    rows = cluster.sql("SELECT c.oid::regclass, c.relpersistence FROM pg_class c JOIN pg_namespace s ON "
                       "s.oid = c.relnamespace WHERE c.relkind = 'r' AND s.nspname = 'public' AND c.relname NOT IN "
                       f"({', '.join(repr(t) for t in LOAD_TABLES)}) ORDER BY 1").splitlines()
    for row in rows:
        name, persistence = row.split('|')
        if persistence == 'u':
            out[name] = 'unlogged'
        else:
            out[name] = cluster.sql(f"SELECT count(*), md5(coalesce(string_agg(t::text, chr(10) ORDER BY t::text), "
                                    f"'')) FROM {name} t")
    out['_schema'] = hashlib.md5(cluster.sql(
        "SELECT string_agg(c.relname || ':' || c.relkind::text || ':' || c.relpersistence::text || ':' || "
        "coalesce(pg_get_indexdef(i.indexrelid), ''), ';' ORDER BY c.relname) FROM pg_class c LEFT JOIN pg_index i "
        "ON i.indexrelid = c.oid JOIN pg_namespace s ON s.oid = c.relnamespace WHERE s.nspname = 'public'")
        .encode()).hexdigest()
    out['_databases'] = cluster.sql('SELECT string_agg(datname, \',\' ORDER BY datname) FROM pg_database',
                                    db='postgres')
    return out


def copy_static_digest(cluster, expect):
    """The same digest of a recovered copy; an unlogged table must be there and empty."""
    got = static_digest(cluster)
    for name, value in expect.items():
        if value == 'unlogged' and got.get(name) == 'unlogged':
            rows = cluster.sql(f'SELECT count(*) FROM {name}')
            got[name] = 'unlogged' if rows == '0' else f'unlogged with {rows} rows'
    return got


# --- B's faults ------------------------------------------------------------------------------------------------

def tree_digest(root):
    """Every regular file of B, WAL and backup_label included: {relative path: sha256}."""
    out = {}
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.isfile(path) and not os.path.islink(path):
                out[os.path.relpath(path, root)] = digest(path)
    return out


def relation_files(root, rng, k):
    candidates = []
    for dirpath, _, filenames in os.walk(Path(root) / 'base'):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if name[0].isdigit() and os.path.getsize(path) > 0:
                candidates.append(path)
    candidates.sort()
    return rng.sample(candidates, min(k, len(candidates)))


def diverge(bindir, sock, basis, rng, scripts, record):
    """B starts (recovering from its backup_label), runs its own load and DDL, and stops cleanly."""
    cluster = Cluster(bindir, sock, basis, DIVERGED_PORT)
    started = cluster.start(check=False)
    if started.returncode != 0:
        record['diverged_start'] = f'B did not start: {cluster.log_tail(400)}'
        return
    try:
        load = start_load(cluster, scripts, rng.randint(1, 10**6), clients=2, seconds=60, rate=None)
        time.sleep(rng.uniform(0.3, 1.5))
        stop_load(load)
        cluster.sql(f'CREATE TABLE diverged_{rng.randint(1, 9)} AS SELECT g FROM generate_series(1, 1000) g; '
                    f"UPDATE notes SET body = 'diverged' WHERE id % 97 = {rng.randint(0, 96)}")
        record['diverged_start'] = 'ok'
    finally:
        cluster.stop()


def corrupt(basis, rng, record):
    paths = relation_files(basis, rng, rng.randint(1, 3))
    for path in paths:
        size = os.path.getsize(path)
        with open(path, 'r+b') as stream:
            for _ in range(rng.randint(1, 16)):
                stream.seek(rng.randrange(size))
                stream.write(bytes([rng.randrange(256)]))
    record['corrupted'] = [os.path.relpath(path, basis) for path in paths]


def truncate(basis, rng, record):
    path = relation_files(basis, rng, 1)[0]
    if rng.random() < 0.5:
        length = rng.randrange(os.path.getsize(path))
        os.truncate(path, length)
        record['truncated'] = [os.path.relpath(path, basis), length]
    else:
        os.remove(path)
        record['truncated'] = [os.path.relpath(path, basis), None]


# --- the refresh and the checks --------------------------------------------------------------------------------

def pgdelta_argv(source, basis, stats, pg):
    return [sys.executable, '-m', 'pgdelta', 'local', '--source', str(source.data), '--source-kind', 'live',
            '--pg-conn', source.conninfo(), '--pg-controldata', str(pg / 'pg_controldata'), '--psql',
            str(pg / 'psql'), '--basis', str(basis), '--in-place', '--stats-json', str(stats)]


def pgdelta_env(variant):
    """pgdelta's environment: with the broken variant `variant` (a key of VARIANTS) switched on, or none."""
    env = dict(os.environ, PYTHONPATH=str(REPO))
    env.pop('PGDELTA_TEST_READ_BEFORE_START', None)
    if variant:
        env['PGDELTA_TEST_READ_BEFORE_START'] = VARIANTS[variant]
    return env


def negative_variant(n, every):
    """The negative control trial number n is, or None: in each block of `every` numbers, the one half-way
    through reads before pg_backup_start, the block's last one while it runs."""
    if every <= 0:
        return None
    if every < 3:
        raise HarnessError('--negative-every must be at least 3 (two negative controls in each block)')
    return {every // 2: 'before_start', every - 1: 'during_start'}.get(n % every)


def leftovers(source):
    """pgdelta's slots and sessions still on S, after waiting up to SLOT_WAIT_SECONDS for them to go."""
    deadline = time.time() + SLOT_WAIT_SECONDS
    while True:
        found = source.sql("SELECT (SELECT count(*) FROM pg_replication_slots WHERE slot_name LIKE 'pgdelta%') || ',' "
                           "|| (SELECT count(*) FROM pg_stat_activity WHERE application_name = 'pgdelta')", db='postgres')
        if found == '0,0' or time.time() > deadline:
            return found


def check_copy(bindir, sock, source, path, work, port, start_us, stop_us, expect, lsns=None):
    """Start a disposable clone of `path` and check it: (started, problems, facts)."""
    target = Path(work) / f'copy{port}'
    clone(path, target)
    cluster = Cluster(bindir, sock, target, port)
    started = time.perf_counter()
    result = cluster.start(check=False)
    facts = {'start_seconds': round(time.perf_counter() - started, 3)}
    if result.returncode != 0:
        facts['log'] = cluster.log_tail()
        cluster.stop(mode='immediate', check=False)
        shutil.rmtree(target, ignore_errors=True)
        return False, ['the copy did not start'], facts
    problems = []
    try:
        log = Path(f'{target}.log').read_text(errors='replace')
        if lsns is not None:
            expected = f'completed backup recovery with redo LSN {lsns[0]} and end LSN {lsns[1]}'
            if expected not in log:
                problems.append(f'its log lacks "{expected}"')
        state = json.loads(cluster.sql(bank.COPY_STATE_SQL))
        pairs = [(row['client'], row['n']) for row in state['progress']]
        reference = json.loads(source.sql(bank.source_reference_sql(start_us, pairs)))
        problems += bank.judge(state, reference, start_us, stop_us, CLIENTS, ACCOUNTS)
        facts['bank'] = bank.summary(state)
        got = copy_static_digest(cluster, expect)
        differ = sorted(key for key in set(got) | set(expect) if got.get(key) != expect.get(key))
        if differ:
            problems.append(f'tables the load does not write differ from S\'s: {differ[:5]}')
        amcheck = run([str(Path(bindir) / 'pg_amcheck'), '-h', sock, '-p', str(port), '-U', USER, '--all',
                       '--heapallindexed', '--install-missing'], check=False)
        facts['amcheck'] = amcheck.returncode
        if amcheck.returncode != 0:
            problems.append(f'pg_amcheck exit {amcheck.returncode}: {(amcheck.stdout + amcheck.stderr)[-400:]}')
    except (HarnessError, ValueError) as exc:
        problems.append(f'checking the copy failed: {str(exc)[-400:]}')
    finally:
        cluster.stop(check=False)
        shutil.rmtree(target, ignore_errors=True)
    return True, problems, facts


def refresh(source, basis, work, pg, variant):
    """Run pgdelta to completion: (exit status, statistics or None, harness launch and exit times)."""
    stats = Path(work) / 'stats.json'
    stats.unlink(missing_ok=True)
    launched = time.time()
    result = run(pgdelta_argv(source, basis, stats, pg), check=False, cwd=REPO, env=pgdelta_env(variant))
    ended = time.time()
    data = json.loads(stats.read_text()) if stats.exists() else None
    return result, data, launched, ended


def kill_state(basis, started, problems):
    """How a refresh killed part-way left B, once B differs from before the run (the kill rule, revised after the
    review of October 25: 'blocked' only by pgdelta's own block).

    'new' if a clone of B started and passed every check; 'blocked' if it refused to start and B is in pgdelta's
    documented blocked state (global/pg_control renamed to global/pg_control.pgdelta-incomplete, with the marker
    pgdelta-incomplete.json beside it); otherwise 'wrong': a B that starts and fails a check, or one that cannot
    start for any other reason (say, committed before its WAL and backup_label were written), which no rerun is
    documented to repair.
    """
    if started:
        return 'new' if not problems else 'wrong'
    basis = Path(basis)
    blocked = (not os.path.lexists(basis / CONTROL_FILE) and os.path.isfile(basis / CONTROL_PENDING)
               and os.path.isfile(basis / MARKER))
    return 'blocked' if blocked else 'wrong'


def stats_summary(stats):
    info = stats.get('live') or {}
    return {'seconds': (stats.get('timings_seconds') or {}).get('total'), 'pages': stats.get('pages'),
            **{key: info.get(key) for key in ('start_lsn', 'stop_lsn', 'timeline', 'backup_mode_seconds',
                                              'wal_segment_size', 'wal_segments', 'history_files', 'files_vanished',
                                              'files_shrunk', 'files_grew', 'pages_changed_after_hash',
                                              'control_file_reads', 'test_variant', 'pre_read_seconds',
                                              'timed_out', 'cleanup')}}


def summarize(records):
    """The campaign's tallies from its trial records: the counted trials and how many passed; how many of them
    refreshed with WAL spanning two segments or more, and whether that share reaches MULTI_SEGMENT_SHARE (the bound
    covers the WAL range only then); and each negative control's completed runs and catches, and whether every
    control reached CONTROL_SHARE (no bound is claimed otherwise)."""
    counted = [r for r in records if 'pass' in r and not r.get('negative_control')]
    multi = sum(1 for r in counted if ((r.get('pgdelta') or {}).get('wal_segments') or 0) >= 2)
    share = multi / len(counted) if counted else None
    controls = {}
    for r in records:
        if r.get('negative_control') and 'caught' in r:
            tally = controls.setdefault(r.get('negative_variant') or 'before_start', {'ran': 0, 'caught': 0})
            tally['ran'] += 1
            tally['caught'] += bool(r['caught'])
    return {
        'counted': len(counted),
        'passed': sum(1 for r in counted if r['pass']),
        'multi_segment': multi,
        'multi_segment_share': share,
        'wal_range_covered': share is not None and share >= MULTI_SEGMENT_SHARE,
        'negative_controls': controls,
        'controls_met': set(controls) == set(VARIANTS) and all(
            tally['caught'] >= CONTROL_SHARE * tally['ran'] for tally in controls.values()),
        'harness_errors': sum(1 for r in records if 'harness_error' in r),
    }


def completed_variant(result, stats, variant):
    """Whether a negative control's refresh ran to completion: pgdelta exits 4 for a broken variant even when the
    receiver committed B (review revision), so completion is that exit with the receiver's confirmation."""
    return (result.returncode == 4 and bool(stats) and stats.get('receiver') is not None
            and (stats.get('live') or {}).get('test_variant') == variant)


def trial(n, variant, ctx):
    """One trial; `variant` (a key of VARIANTS) makes it that negative control, None a counted trial."""
    negative = variant is not None
    rng = random.Random(n)
    fault = 'none' if negative else rng.choices([f for f, _ in FAULTS], weights=[w for _, w in FAULTS])[0]
    source, basis, work, pg, sock = ctx['source'], ctx['basis'], ctx['work'], ctx['pg'], ctx['sock']
    rec = {'trial': n, 'fault': fault, 'negative_control': negative, 'negative_variant': variant}
    rec['ddl'] = ddl(source, rng)
    expect = static_digest(source)
    if fault == 'diverged':
        diverge(pg, sock, basis, rng, ctx['scripts'], rec)
    elif fault == 'corrupt':
        corrupt(basis, rng, rec)
    elif fault == 'truncate':
        truncate(basis, rng, rec)
    elif fault == 'kill':
        delay = rng.uniform(0.05, 1.0)  # smoke revision: 0.05-3.0 s put nearly every kill after the exit
        rec['kill_delay'] = round(delay, 3)
        before = tree_digest(basis)
        stats = Path(work) / 'stats-killed.json'
        launched = time.time()
        process = subprocess.Popen(pgdelta_argv(source, basis, stats, pg), cwd=REPO, env=pgdelta_env(False),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            process.wait(timeout=delay)
            rec['finished_before_kill'] = True
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            killed = time.time()
            rec['finished_before_kill'] = False
            after = tree_digest(basis)
            diff = {'added': sorted(set(after) - set(before)), 'removed': sorted(set(before) - set(after)),
                    'changed': sorted(k for k in set(before) & set(after) if before[k] != after[k])}
            rec['diff_at_kill'] = {key: value[:20] for key, value in diff.items()}
            if not [path for paths in diff.values() for path in paths if not pgdelta_own(path)]:
                rec['b_state_at_kill'] = 'old'
            else:
                started, problems, facts = check_copy(pg, sock, source, basis, work, KILLED_PORT, bank.us(launched),
                                                      bank.us(killed), expect)
                rec['killed_copy'] = dict(facts, problems=problems)
                rec['b_state_at_kill'] = kill_state(basis, started, problems)
            rec['leftovers_after_kill'] = leftovers(source)
            if rec['leftovers_after_kill'] != '0,0':
                rec.update(why=f'pgdelta slot or session left on S after the kill: {rec["leftovers_after_kill"]}',
                           cleanup_failure=True, **{'pass': False})
                return rec
            if rec['b_state_at_kill'] == 'wrong':
                if started:
                    why = f'B was caught part-way, started, and is wrong: {problems[:3]}'
                else:
                    why = (f'B was caught part-way and cannot start, but is not in pgdelta\'s blocked state '
                           f'(global/pg_control renamed aside, with the marker): {facts.get("log", "")[-300:]}')
                rec.update(why=why, **{'pass': False})
                return rec
    result, stats, launched, ended = refresh(source, basis, work, pg, variant)
    rec.update(exit=result.returncode, seconds=round(ended - launched, 3))
    if stats:
        rec['pgdelta'] = stats_summary(stats)
    rec['leftovers'] = leftovers(source)
    if rec['leftovers'] != '0,0':
        rec.update(why=f'pgdelta slot or session left on S: {rec["leftovers"]}', cleanup_failure=True,
                   **{'pass': False})
        return rec
    if negative and not completed_variant(result, stats, variant):
        rec['negative_failed'] = f'pgdelta exit {result.returncode}: {result.stderr.strip()[-300:]}'
        return rec  # neither caught nor missed: not counted
    if not negative and (result.returncode != 0 or not stats):
        rec.update(why=f'pgdelta exit {result.returncode}: {result.stderr.strip()[-300:]}', **{'pass': False})
        return rec
    info = stats['live']
    # pgdelta's own times must lie within the harness's: then its tight bounds are as sound as the harness's.
    times = [launched, info['start_requested_utc'], info['started_utc'], info['stop_requested_utc'],
             info['stopped_utc'], ended]
    rec['times_bracketed'] = times == sorted(times)
    started, problems, facts = check_copy(pg, sock, source, basis, work, COPY_PORT,
                                          bank.us(info['start_requested_utc']), bank.us(info['stopped_utc']),
                                          expect, (info['start_lsn'], info['stop_lsn']))
    if not rec['times_bracketed']:
        problems.append(f'pgdelta\'s backup times {times[1:5]} are not within the run ({launched}, {ended})')
    rec['copy'] = dict(facts, started=started, problems=problems)
    if negative:
        rec['caught'] = bool(problems)
        return rec
    rec['pass'] = not problems
    rec['why'] = None if rec['pass'] else f'wrong output: {problems[:3]}'
    return rec


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--out', required=True)
    parser.add_argument('--trials', type=int, default=1000, help='counted trials (the negative controls apart)')
    parser.add_argument('--hours', type=float, default=3.0)
    parser.add_argument('--first', type=int, default=0)
    parser.add_argument('--negative-every', type=int, default=20,
                        help='in each block of N trial numbers, the one half-way through (N/2, 3N/2, ...) is the '
                             'read-before-start negative control and the last one (N-1, 2N-1, ...) the '
                             'read-during-start one; 0: none')
    parser.add_argument('--pg-bin', default='/opt/homebrew/opt/postgresql@17/bin')
    parser.add_argument('--work')
    args = parser.parse_args(argv)
    if 0 < args.negative_every < 3:
        parser.error('--negative-every must be 0 or at least 3 (two negative controls in each block)')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(args.work or tempfile.mkdtemp(prefix='exp25.'))
    work.mkdir(parents=True, exist_ok=True)
    sock = tempfile.mkdtemp(prefix='pgs.')
    pg = Path(args.pg_bin)
    template = work / 'template'
    if not template.exists():
        make_template(pg, sock, template)
    source = Cluster(pg, sock, work / 's', SOURCE_PORT)
    clone(template, source.data)
    clone(template, work / 'b')
    scripts = bank.write_scripts(work / 'scripts')
    source.start()
    load_log = open(out / 'load.err', 'a')
    load = start_load(source, scripts, args.first, log=load_log)
    ctx = {'source': source, 'basis': work / 'b', 'work': work, 'pg': pg, 'sock': sock, 'scripts': scripts}
    deadline = time.time() + args.hours * 3600
    counted = errors = streak = negatives = caught = 0
    status = 0
    try:
        time.sleep(2)
        with open(out / 'trials.jsonl', 'a') as log:
            n = args.first
            while counted < args.trials and time.time() < deadline:
                variant = negative_variant(n, args.negative_every)
                negative = variant is not None
                if load.poll() is not None:
                    load = start_load(source, scripts, n, log=load_log)
                    log.write(json.dumps({'trial': n, 'load_restarted': True}) + '\n')
                    time.sleep(1)
                try:
                    rec = trial(n, variant, ctx)
                except HarnessError as exc:
                    rec = {'trial': n, 'negative_control': negative, 'negative_variant': variant,
                           'harness_error': str(exc)[-400:]}
                    errors += 1
                    for port, name in ((COPY_PORT, f'copy{COPY_PORT}'), (KILLED_PORT, f'copy{KILLED_PORT}')):
                        Cluster(pg, sock, work / name, port).stop(mode='immediate', check=False)
                    Cluster(pg, sock, work / 'b', DIVERGED_PORT).stop(mode='immediate', check=False)
                log.write(json.dumps(rec) + '\n')
                log.flush()
                streak = streak + 1 if 'harness_error' in rec else 0
                if streak >= 10:
                    print(f'stopping: 10 harness errors in a row, the last: {rec["harness_error"][-300:]}', flush=True)
                    status = 3
                    break
                if negative and 'caught' in rec:
                    negatives += 1
                    caught += rec['caught']
                elif negative and 'negative_failed' in rec:
                    print(f'negative control {n} did not run to completion: {rec["negative_failed"]}', flush=True)
                elif 'pass' in rec:
                    if rec.get('cleanup_failure') and not rec['pass']:
                        print(f'CLEANUP FAILURE at trial {n}: {rec["why"]}', flush=True)
                        status = 4
                        break
                    counted += 1
                    if not rec['pass']:
                        print(f'WRONG OUTPUT OR FAILED REFRESH at trial {n}: {rec["why"]}', flush=True)
                        status = 2
                        break
                if 'harness_error' in rec:
                    outcome = 'harness error'
                elif negative:
                    outcome = 'did not run' if 'caught' not in rec else 'caught' if rec['caught'] else 'NOT CAUGHT'
                else:
                    outcome = 'pass'
                print(f'{time.strftime("%H:%M:%S")} trial {n} '
                      f'({f"negative control, {variant}" if negative else rec["fault"]}): '
                      f'{outcome}; counted {counted}, negative controls caught {caught} of {negatives}, '
                      f'harness errors {errors}', flush=True)
                n += 1
    finally:
        stop_load(load)
        load_log.close()
        source.stop(check=False)
    print(f'done: counted {counted}, negative controls caught {caught} of {negatives}, harness errors {errors}',
          flush=True)
    with open(out / 'trials.jsonl') as stream:
        summary = summarize([json.loads(line) for line in stream if line.strip()])
    (out / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(f'summary (all of {out / "trials.jsonl"}): {json.dumps(summary)}', flush=True)
    return status


if __name__ == '__main__':
    sys.exit(main())
