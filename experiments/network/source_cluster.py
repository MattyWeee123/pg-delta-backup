"""EXP-04 source side: the disposable PostgreSQL cluster on pgd-source.

The cluster, the dataset and the workload come from EXP-01's own code
(experiments/change_patterns/run_study.py and workloads.py), so a trial here
starts from the same data and applies the same SQL, seed and batching. Two
things differ, both needed for a second machine:

* the server also listens on the source's internal address, and pg_hba.conf
  lets the destination's address in (trust, as EXP-01 trusts its local
  socket) for normal and replication connections;
* the steps run as separate commands, because the orchestrator on pgd-dest
  takes B0 over the network between loading and the workload:

    source_cluster.py setup    --root R --listen-ip SRC --client-ip DST [trial options]
    source_cluster.py workload --root R [trial options]     (then CHECKPOINT)
    source_cluster.py verify   --root R [trial options]     (row-level ground truth)
    source_cluster.py stop     --root R [--remove]

and, between them, three that stop and start the running cluster:

    source_cluster.py stop-clean --root R                   pg_ctl stop -m fast; must end
                                                            'shut down' (pgdelta_direct*)
    source_cluster.py start      --root R --log-dir L       start it again, as setup did
    source_cluster.py archive    --root R --log-dir L --command CMD
                                                            archive_mode = on with CMD, then
                                                            restart (pgbackrest_* trials only)

and one that times backup mode on the running, idle cluster (EXP-11's bracket probe):

    source_cluster.py backup-mode-probe --root R --repeats N
                                                            N x (drop the page cache;
                                                            pg_backup_start('probe', true);
                                                            pg_backup_stop(false))

EXP-12 (T10a) adds the load, a fixed-rate pgbench on its own database, loaddb, and the
proxy's backup mode and snapshot:

    source_cluster.py loaddb-setup --root R --log-dir L --load-dir D --clients 32
                                       loaddb (pgbench -i -s 2 -F 90, load_progress, amcheck),
                                       autovacuum off on its tables, the added server settings,
                                       a restart, VACUUM ANALYZE, CHECKPOINT; the registry emptied
    source_cluster.py load-start --root R --load-dir D --run-id ID --rate R ...
                                       each client's n and the sums recorded with the load
                                       stopped, then pgbench under setsid; its PID recorded
    source_cluster.py load-stop --root R --load-dir D
                                       SIGALRM (as its own -T timer), the log counted per client,
                                       then the counters again; the run complete if they agree
    source_cluster.py load-capacity --root R --load-dir D --run-id ID [--warmup-s 30 --seconds 60 --runs 1]
                                       closed-loop runs, each logged and offset the same way: C
    source_cluster.py load-status --load-dir D
                                       whether the current run's pgbench still runs
    source_cluster.py load-check --root R --load-dir D
                                       the sums against every logged run's commits, each run's
                                       counts per client, and the runs chained without a gap
    source_cluster.py backup-hold --root R --label L
                                       pg_backup_start(L, true) in one psql session, the start LSN
                                       printed; on a line or the end of stdin, pg_backup_stop(false)
    source_cluster.py snapshot --root R --snapshot DIR --log-dir L
                                       a clean stop, cp -a --reflink=never of the data directory,
                                       sync -f, its selection's digest, a restart

Each prints one RESULT_PREFIX JSON line (see agent.py). The trial options
(--scale, --pattern, --fraction, --seed, --hot-percent, --events-rows,
--batch-size) are EXP-01's, parsed by run_study's own parser.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import time

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.change_patterns import run_study, workloads  # noqa: E402
from experiments.network import agent, measure  # noqa: E402
from experiments.network.agent import emit  # noqa: E402

DEFAULT_BIN_DIR = '/usr/lib/postgresql/17/bin'
TRIAL_OPTIONS = ('scale', 'pattern', 'fraction', 'seed', 'hot_percent', 'events_rows',
                 'batch_size')
# EXP-12's load scripts (experiments/network/load/), read by pgbench on the source.
LOAD_SCRIPT_DIR = REPO_ROOT / 'experiments' / 'network' / 'load'
# How long load-stop waits for pgbench to end after its signal, and how often it looks.
LOAD_STOP_TIMEOUT = 120
LOAD_POLL = 0.05
# The proxy's snapshot: a byte copy that shares no block with the live files.
SNAPSHOT_COPY = ('cp', '-a', '--reflink=never')


def hba_lines(client_ip: str) -> list[str]:
    user = run_study.USER
    return [f'host all {user} {client_ip}/32 trust',
            f'host replication {user} {client_ip}/32 trust']


class NetworkTrial(run_study.Trial):
    """run_study's Trial, also reachable from the destination VM."""

    listen_ip: str | None = None
    client_ip: str | None = None

    def init_cluster(self) -> list[str]:
        lines = super().init_cluster()
        extra = [f"listen_addresses = '{self.listen_ip}'"]
        with open(self.data / 'postgresql.conf', 'a') as stream:
            stream.write('# EXP-04: reachable from the destination VM (overrides the above)\n'
                         + '\n'.join(extra) + '\n')
        # pg_hba.conf is first-match; initdb's --auth-host=reject lines must come after ours.
        hba = self.data / 'pg_hba.conf'
        added = hba_lines(self.client_ip)
        hba.write_text('# EXP-04: the destination VM\n' + '\n'.join(added) + '\n\n'
                       + hba.read_text())
        return lines + extra + [f'pg_hba.conf: {line}' for line in added]


def trial_spec(args) -> dict:
    """EXP-01's spec for one trial, built by run_study's own parser and defaults."""
    argv = ['--scale', str(args.scale), '--pattern', args.pattern,
            '--fraction', repr(args.fraction), '--seed', str(args.seed),
            '--hot-percent', repr(args.hot_percent), '--batch-size', str(args.batch_size)]
    if args.events_rows is not None:
        argv += ['--events-rows', str(args.events_rows)]
    study_args, _ = run_study.parse_args(argv)
    [spec] = run_study.trial_specs(study_args)
    return spec


def make_trial(args, step: str) -> NetworkTrial:
    tools, version, major = run_study.preflight(Path(args.bin_dir))
    spec = trial_spec(args)
    root = Path(args.root)
    log_dir = Path(args.log_dir) / f'{step}-{time.strftime("%Y%m%dT%H%M%S")}'
    trial = NetworkTrial(spec, tools, major, root, log_dir)
    trial.version = version
    return trial


def stop_existing(root: Path, bin_dir: Path) -> bool:
    """Stop a leftover server on root/data (from an interrupted run)."""
    data = root / 'data'
    if not (data / 'postmaster.pid').exists():
        return False
    subprocess.run([str(bin_dir / 'pg_ctl'), '-D', str(data), '-m', 'immediate', '-w', '-t',
                    '60', 'stop'], capture_output=True)
    return True


def cmd_setup(args) -> dict:
    root = Path(args.root)
    stopped = stop_existing(root, Path(args.bin_dir))
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    trial = make_trial(args, 'setup')
    trial.listen_ip, trial.client_ip = args.listen_ip, args.client_ip
    started = time.perf_counter()
    config = trial.init_cluster()
    trial.start()
    trial.phase('initdb_start', started)
    started = time.perf_counter()
    before = trial.load()
    trial.phase('load', started)
    return {
        'spec': trial.spec, 'postgres_version': trial.version, 'stopped_leftover': stopped,
        'appended_postgresql_conf': config,
        'settings': trial.sql_json(run_study.SETTINGS_SQL, label='settings'),
        'system_identifier': trial.sql('SELECT system_identifier FROM pg_control_system();',
                                       label='system-identifier'),
        'databases': trial.databases(),
        'tables_before': before,
        'stats_after_load': trial.sql_json(run_study.STATS_SQL, label='stats'),
        'timings': trial.timings, 'commands': trial.commands,
    }


def rows_to_change(spec: dict) -> int:
    return workloads.target_rows(spec['pattern'], spec['fraction'],
                                 accounts_rows=spec['accounts_rows'],
                                 events_rows=spec['events_rows'])


def cmd_workload(args) -> dict:
    trial = make_trial(args, 'workload')
    k = rows_to_change(trial.spec)
    stats = {'before_workload': trial.sql_json(run_study.STATS_SQL, label='stats')}
    started = time.perf_counter()
    workload = trial.apply_workload(k)
    workload['post_steps'] = trial.post_steps()
    trial.phase('workload', started)
    started = time.perf_counter()
    trial.sql('CHECKPOINT;', label='checkpoint-after-workload')
    trial.phase('checkpoint', started)
    # No row counts or other scans here: they would set hint bits and change
    # pages after the CHECKPOINT. The ground truth is checked by `verify` once
    # every method has run, as EXP-01 checks it after its backups.
    stats['after_workload'] = trial.sql_json(run_study.STATS_SQL, label='stats')
    return {'k': k, 'workload': workload, 'pg_stat_user_tables': stats,
            'timings': trial.timings, 'commands': trial.commands}


def cmd_verify(args) -> dict:
    trial = make_trial(args, 'verify')
    k = rows_to_change(trial.spec)
    stats = trial.sql_json(run_study.STATS_SQL, label='stats')
    truth = trial.verify_rows(k)
    return {'k': k, 'ground_truth': truth, 'pg_stat_user_tables': stats,
            'tables_after': trial.table_state('after'), 'commands': trial.commands}


def start_argv(root, bin_dir, log) -> list[str]:
    """pg_ctl start with the socket directory and port run_study.Trial.start gives the server."""
    root = Path(root)
    return [str(Path(bin_dir) / 'pg_ctl'), '-D', str(root / 'data'), '-l', str(log), '-o',
            shlex.join(['-k', str(root / 's'), '-p', str(run_study.PORT)]), '-w', '-t', '600', 'start']


def stop_argv(root, bin_dir, mode: str = 'fast') -> list[str]:
    return [str(Path(bin_dir) / 'pg_ctl'), '-D', str(Path(root) / 'data'), '-m', mode, '-w', '-t',
            '600', 'stop']


def cluster_state(root, bin_dir) -> str | None:
    """'Database cluster state' from pg_controldata: 'shut down' after a clean stop."""
    result = subprocess.run([str(Path(bin_dir) / 'pg_controldata'), str(Path(root) / 'data')],
                            capture_output=True, text=True, env=run_study.controlled_env())
    found = re.search(r'^Database cluster state:\s*(.+?)\s*$', result.stdout, re.MULTILINE)
    return found.group(1) if found else None


def archive_lines(command: str) -> list[str]:
    """postgresql.conf lines that archive every WAL file with `command`."""
    return ['archive_mode = on', "archive_command = '" + command.replace("'", "''") + "'"]


def cmd_stop_clean(args) -> dict:
    """Stop the running cluster with a shutdown checkpoint, and check that it says so."""
    root, bin_dir = Path(args.root), Path(args.bin_dir)
    argv = stop_argv(root, bin_dir)
    started = time.perf_counter()
    result = subprocess.run(argv, capture_output=True, text=True, env=run_study.controlled_env())
    seconds = round(time.perf_counter() - started, 3)
    state = cluster_state(root, bin_dir)
    if result.returncode != 0 or state != 'shut down':
        raise SystemExit(f'the source did not stop cleanly (pg_ctl exit {result.returncode}, '
                         f'cluster state {state!r}): {(result.stdout + result.stderr).strip()[-2000:]}')
    return {'stopped': True, 'state': state, 'seconds': seconds, 'argv': argv}


def cmd_start(args) -> dict:
    """Start the cluster again, with the options `setup` started it with (a running one is left be)."""
    root, bin_dir = Path(args.root), Path(args.bin_dir)
    status = subprocess.run([str(bin_dir / 'pg_ctl'), '-D', str(root / 'data'), 'status'],
                            capture_output=True, text=True, env=run_study.controlled_env())
    if status.returncode == 0:
        return {'started': False, 'already_running': True, 'status': status.stdout.strip()[-1000:]}
    log_dir = Path(args.log_dir) / f'start-{time.strftime("%Y%m%dT%H%M%S")}'
    log_dir.mkdir(parents=True, exist_ok=True)
    (root / 's').mkdir(exist_ok=True)
    argv = start_argv(root, bin_dir, log_dir / 'server.log')
    started = time.perf_counter()
    result = subprocess.run(argv, capture_output=True, text=True, env=run_study.controlled_env())
    seconds = round(time.perf_counter() - started, 3)
    if result.returncode != 0:
        raise SystemExit(f'the source did not start (pg_ctl exit {result.returncode}): '
                         f'{(result.stdout + result.stderr).strip()[-2000:]}')
    return {'started': True, 'seconds': seconds, 'argv': argv, 'log': str(log_dir / 'server.log')}


def cmd_archive(args) -> dict:
    """archive_mode = on with archive_command, which takes a restart: stop cleanly, then start."""
    root, bin_dir = Path(args.root), Path(args.bin_dir)
    lines = archive_lines(args.command)
    with open(root / 'data' / 'postgresql.conf', 'a') as stream:
        stream.write('# EXP-04 (T11): archive to the pgBackRest repositories (trials with a '
                     'pgbackrest method only)\n' + '\n'.join(lines) + '\n')
    stop = cmd_stop_clean(args)
    start = cmd_start(args)
    show = subprocess.run([str(bin_dir / 'psql'), '-X', '-A', '-t', '-h', str(root / 's'), '-p',
                           str(run_study.PORT), '-U', run_study.USER, '-d', run_study.DATABASE, '-c',
                           "SELECT current_setting('archive_mode') || ' ' || current_setting('archive_command')"],
                          capture_output=True, text=True, env=run_study.controlled_env())
    in_effect = show.stdout.strip()
    if show.returncode != 0 or in_effect != f'on {args.command}':
        raise SystemExit(f'archiving is not in effect after the restart: {in_effect!r} '
                         f'{show.stderr.strip()[-1000:]}')
    return {'appended_postgresql_conf': lines, 'in_effect': in_effect, 'stop': stop, 'start': start}


# One backup-mode pair in one session (a non-exclusive backup must start and stop in the same one),
# timed by psql itself; the label and fast checkpoint are EXP-11's.
BACKUP_MODE_PAIR = ("SELECT pg_backup_start('probe', true);", 'SELECT lsn FROM pg_backup_stop(false);')


def backup_mode_argv(root, bin_dir, label: str = 'probe') -> list[str]:
    """psql on the source's socket: \\timing on, then pg_backup_start and pg_backup_stop in one session.

    EXP-12's bracket arm is the same pair with its own label ('t10a').
    """
    root = Path(root)
    pair = BACKUP_MODE_PAIR if label == 'probe' else backup_hold_statements(label)
    return [str(Path(bin_dir) / 'psql'), '-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-h', str(root / 's'),
            '-p', str(run_study.PORT), '-U', run_study.USER, '-d', run_study.DATABASE, '-c', '\\timing on',
            *(part for statement in pair for part in ('-c', statement))]


def cmd_backup_mode_probe(args) -> dict:
    """EXP-11's bracket cross-check: --repeats x (drop the cache; backup start; backup stop), timed.

    The source runs, idle. Before each pair the page cache is dropped (agent.py drop-caches: sync,
    then echo 3 > drop_caches, through sudo -n). psql's \\timing gives each statement's time;
    pair_s is their sum, wall_s the whole psql run, connection included. The first pair is
    reported apart: its checkpoint may have more to write.
    """
    root, bin_dir = Path(args.root), Path(args.bin_dir)
    argv = backup_mode_argv(root, bin_dir)
    pairs = []
    for _ in range(args.repeats):
        drop = agent.cmd_drop_caches(args)
        started = time.perf_counter()
        result = subprocess.run(argv, capture_output=True, text=True, env=run_study.controlled_env())
        wall = round(time.perf_counter() - started, 6)
        timings = measure.psql_timings(result.stdout)
        if result.returncode != 0 or len(timings) != len(BACKUP_MODE_PAIR):
            raise SystemExit(f'backup mode on the source failed (psql exit {result.returncode}, timings '
                             f'{timings}): {(result.stdout + result.stderr).strip()[-2000:]}')
        pairs.append({'start_s': timings[0], 'stop_s': timings[1], 'pair_s': round(sum(timings), 6),
                      'wall_s': wall, 'drop_seconds': drop['seconds']})
    times = [pair['pair_s'] for pair in pairs]
    return {'repeats': args.repeats, 'argv': argv, 'pairs': pairs, 'first_pair_s': times[0],
            'median_pair_s': statistics.median(times),
            'median_after_first_s': statistics.median(times[1:]) if len(times) > 1 else None}


# --- EXP-12 (T10a): the load, the proxy's backup mode and the snapshot -----------------------

def psql_argv(root, bin_dir, database: str, *statements) -> list[str]:
    """psql on the cluster's socket, unaligned and quiet, stopping at the first error; each statement a -c."""
    root = Path(root)
    return [str(Path(bin_dir) / 'psql'), '-X', '-A', '-t', '-q', '-v', 'ON_ERROR_STOP=1', '-h', str(root / 's'),
            '-p', str(run_study.PORT), '-U', run_study.USER, '-d', database,
            *(part for statement in statements for part in ('-c', statement))]


def sql(root, bin_dir, statement: str, database: str = measure.LOAD_DATABASE, timeout: float = 3600) -> str:
    """One statement through psql; its output, or SystemExit with psql's error."""
    result = subprocess.run(psql_argv(root, bin_dir, database, statement), capture_output=True, text=True,
                            timeout=timeout, env=run_study.controlled_env())
    if result.returncode != 0:
        raise SystemExit(f'psql on {database} failed ({result.returncode}): {result.stderr.strip()[-2000:]}')
    return result.stdout.strip()


def load_counters(root, bin_dir) -> dict:
    """Each client's n and the sums of n and abalance, read now (with the load stopped), and when."""
    value = json.loads(sql(root, bin_dir, measure.LOAD_COUNTERS_SQL))
    return {'utc': time.time(), 'n': {key: int(n) for key, n in (value.get('n') or {}).items()},
            'sum_n': int(value.get('sum_n') or 0), 'sum_abalance': int(value.get('sum_abalance') or 0)}


def loaddb_statements(clients: int) -> list[str]:
    """load_progress with one row per client, each on its own page; amcheck, so every copy holds it; autovacuum
    off for loaddb's tables and their TOAST tables, so no vacuum of them falls in a window."""
    statements = [
        f'CREATE TABLE load_progress (client int PRIMARY KEY, n bigint NOT NULL DEFAULT 0, '
        f"filler char({measure.LOAD_FILLER_BYTES}) NOT NULL DEFAULT '') "
        f'WITH (fillfactor = {measure.LOAD_PROGRESS_FILLFACTOR});',
        f'INSERT INTO load_progress (client) SELECT generate_series(0, {clients - 1});',
        'CREATE EXTENSION amcheck;']
    statements += [f'ALTER TABLE {table} SET (autovacuum_enabled = false, toast.autovacuum_enabled = false);'
                   for table in measure.LOAD_TABLES]
    return statements


LOAD_SETTINGS_SQL = ('SELECT json_object_agg(name, current_setting(name)) FROM unnest(ARRAY['
                     + ', '.join(f"'{name}'" for name in measure.LOAD_SERVER_CONFIG) + ']) AS name;')
DATABASES_SQL = 'SELECT json_object_agg(datname, oid::bigint) FROM pg_database;'
LOAD_RELATIONS_SQL = ("SELECT json_object_agg(relname, json_build_object('pages', pg_relation_size(oid) / 8192, "
                      "'path', pg_relation_filepath(oid), 'options', reloptions)) FROM pg_class WHERE relname IN ("
                      + ', '.join(f"'{table}'" for table in measure.LOAD_TABLES)
                      + ", 'pgbench_accounts_pkey', 'load_progress_pkey');")


def cmd_loaddb_setup(args) -> dict:
    """EXP-12's step 1 for the load: loaddb in the trial's cluster, the added settings, a restart.

    pgbench -i -s 2 -F 90 loaddb (200,000 accounts, 10% free per page), load_progress (one row per client, each
    on its own page), CREATE EXTENSION amcheck, autovacuum off on loaddb's tables; then the settings
    (measure.LOAD_SERVER_CONFIG) appended to postgresql.conf, a clean restart, and each checked in effect;
    then VACUUM ANALYZE of loaddb and a CHECKPOINT. The load's directory on the source is emptied, with its
    registry, so the first run's counters are the trial's zero.
    """
    root, bin_dir = Path(args.root), Path(args.bin_dir)
    started = time.perf_counter()
    sql(root, bin_dir, f'CREATE DATABASE {measure.LOAD_DATABASE};', database=run_study.DATABASE)
    init = subprocess.run([str(bin_dir / 'pgbench'), '-i', '-q', '-s', str(measure.LOAD_SCALE), '-F',
                           str(measure.LOAD_FILLFACTOR), '-h', str(root / 's'), '-p', str(run_study.PORT), '-U',
                           run_study.USER, measure.LOAD_DATABASE], capture_output=True, text=True,
                          env=run_study.controlled_env(), timeout=3600)
    if init.returncode != 0:
        raise SystemExit(f'pgbench -i failed ({init.returncode}): {init.stderr.strip()[-2000:]}')
    for statement in loaddb_statements(args.clients):
        sql(root, bin_dir, statement)
    lines = [f'{key} = {value}' for key, value in measure.LOAD_SERVER_CONFIG.items()]
    with open(root / 'data' / 'postgresql.conf', 'a') as stream:
        stream.write('# EXP-12 (T10a): every arm alike; no checkpoint inside a window but an arm\'s own\n'
                     + '\n'.join(lines) + '\n')
    stop = cmd_stop_clean(args)
    start = cmd_start(args)
    settings = json.loads(sql(root, bin_dir, LOAD_SETTINGS_SQL, database=run_study.DATABASE))
    wrong = {name: value for name, value in settings.items() if value != measure.LOAD_SERVER_CONFIG[name]}
    if wrong:
        raise SystemExit(f'the load\'s settings are not in effect after the restart: {wrong}')
    sql(root, bin_dir, 'VACUUM ANALYZE;')
    sql(root, bin_dir, 'CHECKPOINT;', database=run_study.DATABASE)
    load_dir = Path(args.load_dir)
    if load_dir.exists():
        shutil.rmtree(load_dir)
    measure.write_load_state(load_dir, {'runs': [], 'current': None})
    return {'clients': args.clients, 'appended_postgresql_conf': lines, 'settings': settings,
            'pgbench_init': init.stderr.strip()[-2000:], 'statements': loaddb_statements(args.clients),
            'databases': json.loads(sql(root, bin_dir, DATABASES_SQL, database=run_study.DATABASE)),
            'relations': json.loads(sql(root, bin_dir, LOAD_RELATIONS_SQL)),
            'counters': load_counters(root, bin_dir), 'stop': stop, 'start': start, 'load_dir': str(load_dir),
            'seconds': round(time.perf_counter() - started, 3)}


def process_alive(pid) -> bool:
    """Whether the process runs (a zombie, which only waits to be reaped, doesn't).

    pgbench is normally no child of the command that looks (load-start exits at once, so init reaps it); when it
    is one (a capacity run, or the commands called in one process), it is reaped here.
    """
    try:
        if os.waitpid(int(pid), os.WNOHANG)[0] == int(pid):
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = Path(f'/proc/{int(pid)}/stat').read_text()
        return stat[stat.rindex(')') + 2:].split()[0] != 'Z'
    except (OSError, ValueError, IndexError):
        return True


def wait_gone(pid, timeout: float) -> bool:
    """Until the process has ended, at most `timeout` seconds; whether it did."""
    deadline = time.monotonic() + timeout
    while process_alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(LOAD_POLL)
    return True


def start_load_run(args, run_id: str, rate, duration_s: int, kind: str):
    """A pgbench run of the load under setsid, its counters recorded first; (its registry entry, Popen)."""
    root, bin_dir, load_dir = Path(args.root), Path(args.bin_dir), Path(args.load_dir)
    state = measure.read_load_state(load_dir)
    current = measure.load_run(state)
    if current is not None and process_alive(current['pid']):
        raise SystemExit(f'the load run {current["run_id"]} still runs (PID {current["pid"]}); load-stop it first')
    if any(run['run_id'] == run_id for run in state['runs']):
        raise SystemExit(f'the load already had a run {run_id}')
    load_dir.mkdir(parents=True, exist_ok=True)
    counters = load_counters(root, bin_dir)
    argv = measure.pgbench_load_argv(bin_dir / 'pgbench', rate=rate, clients=args.clients, threads=args.threads,
                                     duration_s=duration_s, seed=args.seed, prefix=load_dir / run_id,
                                     scripts=args.scripts, host=root / 's', port=run_study.PORT,
                                     user=run_study.USER)
    with open(measure.summary_path(load_dir, run_id), 'w') as out, \
            open(measure.progress_path(load_dir, run_id), 'w') as err:
        process = subprocess.Popen(argv, stdout=out, stderr=err, stdin=subprocess.DEVNULL, cwd=load_dir,
                                   env=run_study.controlled_env(), start_new_session=True)
    run = {'run_id': run_id, 'kind': kind, 'rate': rate or None, 'clients': args.clients, 'threads': args.threads,
           'duration_s': duration_s, 'seed': args.seed, 'argv': argv, 'pid': process.pid,
           'prefix': str(load_dir / run_id), 'started_utc': time.time(), 'start': counters, 'stop': None}
    state['runs'].append(run)
    state['current'] = run_id
    measure.write_load_state(load_dir, state)
    return run, process


PGBENCH_TPS = re.compile(r'^tps = (\d+(?:\.\d+)?) \(without initial connection time\)', re.MULTILINE)
PGBENCH_PROCESSED = re.compile(r'^number of transactions actually processed: (\d+)', re.MULTILINE)
PGBENCH_FAILED = re.compile(r'^number of failed transactions: (\d+)', re.MULTILINE)


def finish_load_run(args, run_id: str, how: str, signalled_utc=None) -> dict:
    """A run that has ended: its log counted per client, the counters again, and whether they agree.

    complete: for every client, n at the stop minus n at the start equals its log's committed transactions
    (EXP-12's smoke item 1). pgbench's summary and the end of its progress output are kept.
    """
    root, bin_dir, load_dir = Path(args.root), Path(args.bin_dir), Path(args.load_dir)
    state = measure.read_load_state(load_dir)
    run = measure.load_run(state, run_id)
    names = measure.pgbench_log_names(run)
    counts = measure.count_commits([load_dir / name for name in names if (load_dir / name).exists()])
    stop = load_counters(root, bin_dir)
    deltas = {key: stop['n'].get(key, 0) - n for key, n in run['start']['n'].items()}
    mismatched = {key: {'n_delta': delta, 'logged': counts.get(int(key), 0)} for key, delta in deltas.items()
                  if delta != counts.get(int(key), 0)}
    try:
        summary = measure.summary_path(load_dir, run_id).read_text(errors='replace')
        progress = measure.progress_path(load_dir, run_id).read_text(errors='replace')
    except OSError:
        summary, progress = '', ''
    tps, processed, failed_count = (PGBENCH_TPS.search(summary), PGBENCH_PROCESSED.search(summary),
                                    PGBENCH_FAILED.search(summary))
    run.update(stop=stop, stopped_utc=time.time(), signalled_utc=signalled_utc, how=how,
               log_files=[name for name in names if (load_dir / name).exists()],
               commits={str(client): count for client, count in sorted(counts.items())},
               commits_total=sum(counts.values()), complete=not mismatched, mismatched=mismatched,
               tps=float(tps.group(1)) if tps else None, processed=int(processed.group(1)) if processed else None,
               failed=int(failed_count.group(1)) if failed_count else None,
               aborted='aborted' in (summary + progress).lower(),
               errors=[line for line in progress.splitlines() if 'error' in line.lower()][-20:])
    if state.get('current') == run_id:
        state['current'] = None
    measure.write_load_state(load_dir, state)
    return run


def cmd_load_start(args) -> dict:
    """EXP-12's load: pgbench at --rate (open loop) for --duration-s, under setsid, its counters recorded first."""
    run, _ = start_load_run(args, args.run_id, args.rate, args.duration_s, args.kind)
    return run


def cmd_load_stop(args) -> dict:
    """The current run stopped with SIGALRM, the signal its own -T timer uses, so it ends as at -T's end and
    writes out its logs (or --signal INT, EXP-12's fallback); then counted and its counters read.

    If pgbench has already ended (-T, or it died), it is only counted: `how` says which. A pgbench that doesn't
    end within LOAD_STOP_TIMEOUT is killed, and the run is incomplete.
    """
    state = measure.read_load_state(args.load_dir)
    run = measure.load_run(state, args.run_id)
    if run is None or run.get('stop') is not None:
        raise SystemExit(f'no load run to stop ({args.run_id or "current"})')
    signalled, how = None, 'ended'
    if process_alive(run['pid']):
        os.kill(run['pid'], getattr(signal, f'SIG{args.signal}'))
        signalled, how = time.time(), f'SIG{args.signal}'
        if not wait_gone(run['pid'], LOAD_STOP_TIMEOUT):
            os.kill(run['pid'], signal.SIGKILL)
            wait_gone(run['pid'], 30)
            how = f'SIG{args.signal}, then SIGKILL after {LOAD_STOP_TIMEOUT} s'
    return finish_load_run(args, run['run_id'], how, signalled)


def cmd_load_capacity(args) -> dict:
    """EXP-12's C: the same scripts and clients with no -R, closed loop, each run logged and offset as the load.

    A --warmup-s run first (logged, left out of C), then --runs runs of --seconds, each ended by its own -T.
    C is the median of their tps (pgbench's, without the initial connection time).
    """
    runs = []
    plan = ([(f'{args.run_id}-warmup', args.warmup_s, 'capacity_warmup')] if args.warmup_s else []) + [
        (f'{args.run_id}-{index + 1}', args.seconds, 'capacity') for index in range(args.runs)]
    for run_id, seconds, kind in plan:
        _, process = start_load_run(args, run_id, None, seconds, kind)
        process.wait()
        runs.append(finish_load_run(args, run_id, '-T'))
    measured = [run['tps'] for run in runs if run['kind'] == 'capacity' and run['tps'] is not None]
    return {'runs': runs, 'capacity_tps': statistics.median(measured) if measured else None,
            'all_complete': all(run['complete'] for run in runs)}


def cmd_load_status(args) -> dict:
    """Whether the current run's pgbench still runs, with its last progress line."""
    state = measure.read_load_state(args.load_dir)
    run = measure.load_run(state)
    if run is None:
        return {'current': None, 'running': False}
    lines = measure.parse_progress(measure.progress_path(args.load_dir, run['run_id']).read_text(errors='replace'))
    return {'current': run['run_id'], 'pid': run['pid'], 'running': process_alive(run['pid']),
            'last_progress': lines[-1] if lines else None}


def cmd_load_check(args) -> dict:
    """EXP-12's step 15, with the load stopped: the source against every logged run.

    The sums of abalance and of n must both equal the sum of every logged run's committed transactions; each run
    must be complete (its counters against its log, per client); and each run must start where the one before it
    stopped, client by client, so no commit happened outside a logged run.
    """
    state = measure.read_load_state(args.load_dir)
    runs = state.get('runs', [])
    counters = load_counters(args.root, args.bin_dir)
    expected = sum(run.get('commits_total') or 0 for run in runs)
    unfinished = [run['run_id'] for run in runs if run.get('stop') is None]
    incomplete = [run['run_id'] for run in runs if run.get('stop') is not None and not run.get('complete')]
    gaps = [f'{before["run_id"]} -> {after["run_id"]}' for before, after in zip(runs, runs[1:])
            if before.get('stop') is None or before['stop']['n'] != after['start']['n']]
    sums = counters['sum_abalance'] == counters['sum_n'] == expected
    return {'ok': sums and not unfinished and not incomplete and not gaps, 'sums_ok': sums,
            'sum_abalance': counters['sum_abalance'], 'sum_n': counters['sum_n'], 'expected_commits': expected,
            'unfinished': unfinished, 'incomplete': incomplete, 'gaps': gaps, 'counters': counters,
            'runs': [{key: run.get(key) for key in ('run_id', 'kind', 'complete', 'commits_total', 'how')}
                     for run in runs]}


def backup_hold_statements(label: str) -> tuple[str, str]:
    """The proxy's backup mode, in one session: a fast checkpoint at its start, no wait for archiving at its stop."""
    return f"SELECT pg_backup_start('{label}', true);", 'SELECT lsn FROM pg_backup_stop(false);'


BACKUP_STARTED = 'BACKUP-STARTED '


def cmd_backup_hold(args) -> dict:
    """pg_backup_start(label, true) in one psql session, held open: its start LSN printed (BACKUP_STARTED and
    JSON, on a line of its own), then, on a line of stdin or its end, pg_backup_stop(false) in the same session.

    A non-exclusive backup must start and stop in one session, so psql reads its statements from a pipe. The
    end of stdin stops the backup too: the orchestrator's SSH session closing never leaves backup mode held.
    """
    start_sql, stop_sql = backup_hold_statements(args.label)
    psql = subprocess.Popen(psql_argv(args.root, args.bin_dir, args.database), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=run_study.controlled_env())
    try:
        started = time.perf_counter()
        psql.stdin.write(start_sql + '\n')
        psql.stdin.flush()
        start_lsn = psql.stdout.readline().strip()
        start = {'lsn': start_lsn, 'utc': time.time(), 'seconds': round(time.perf_counter() - started, 6)}
        if not start_lsn:
            raise SystemExit(f'pg_backup_start failed: {psql.stderr.read().strip()[-2000:]}')
        print(BACKUP_STARTED + json.dumps(start), flush=True)
        request = sys.stdin.readline()
        requested = time.time()
        started = time.perf_counter()
        psql.stdin.write(stop_sql + '\n')
        psql.stdin.flush()
        stop_lsn = psql.stdout.readline().strip()
        stop = {'lsn': stop_lsn, 'utc': time.time(), 'seconds': round(time.perf_counter() - started, 6),
                'requested_utc': requested, 'by': 'line' if request else 'end of stdin'}
        psql.stdin.close()
        code = psql.wait(60)
        if not stop_lsn or code != 0:
            raise SystemExit(f'pg_backup_stop failed (psql exit {code}): {psql.stderr.read().strip()[-2000:]}')
        return {'label': args.label, 'start': start, 'stop': stop, 'psql_exit_code': code, 'one_session': True}
    finally:
        if psql.poll() is None:
            psql.kill()
            psql.wait()


def cmd_snapshot(args) -> dict:
    """EXP-12's step 5, the proxy's source: a clean stop, a byte copy of the data directory, sync -f, its digest.

    `cp -a --reflink=never data SNAP` on the source's /data, then `sync -f SNAP`; then what pgdelta's datadir mode
    selects in it, hashed (agent.py datadir-digest's), which the proxies' verification uses instead of reading the
    source; the source restarted whatever happened. The snapshot is cleanly stopped and has no postmaster.pid.
    """
    root, snapshot = Path(args.root), Path(args.snapshot)
    result: dict = {'snapshot': str(snapshot)}
    try:
        result['stop'] = cmd_stop_clean(args)
        if snapshot.exists():
            shutil.rmtree(snapshot)
        started = time.perf_counter()
        copy = subprocess.run([*SNAPSHOT_COPY, str(root / 'data'), str(snapshot)], capture_output=True, text=True)
        result['copy'] = {'argv': [*SNAPSHOT_COPY, str(root / 'data'), str(snapshot)], 'exit_code': copy.returncode,
                          'seconds': round(time.perf_counter() - started, 3)}
        if copy.returncode != 0:
            raise SystemExit(f'the snapshot copy failed ({copy.returncode}): {copy.stderr.strip()[-2000:]}')
        started = time.perf_counter()
        synced = subprocess.run(['sync', '-f', str(snapshot)], capture_output=True, text=True)
        result['sync'] = {'exit_code': synced.returncode, 'seconds': round(time.perf_counter() - started, 3)}
        if synced.returncode != 0:
            raise SystemExit(f'sync -f {snapshot} failed: {synced.stderr.strip()[-2000:]}')
        result['digest'] = agent.cmd_datadir_digest(argparse.Namespace(
            root=str(snapshot), pg_controldata=str(Path(args.bin_dir) / 'pg_controldata')))
    finally:
        result['restart'] = cmd_start(args)
    return result


def cmd_stop(args) -> dict:
    root, bin_dir = Path(args.root), Path(args.bin_dir)
    stopped = False
    if (root / 'data' / 'postmaster.pid').exists():
        result = subprocess.run([str(bin_dir / 'pg_ctl'), '-D', str(root / 'data'), '-m', 'fast',
                                 '-w', '-t', '120', 'stop'], capture_output=True, text=True)
        stopped = result.returncode == 0 or stop_existing(root, bin_dir)
    if args.remove and root.exists():
        shutil.rmtree(root)
    return {'stopped': stopped, 'removed': bool(args.remove)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)

    def trial_options(sub):
        sub.add_argument('--root', required=True)
        sub.add_argument('--bin-dir', default=DEFAULT_BIN_DIR)
        sub.add_argument('--log-dir', required=True)
        sub.add_argument('--scale', type=int, required=True)
        sub.add_argument('--pattern', required=True, choices=workloads.PATTERNS)
        sub.add_argument('--fraction', type=float, required=True)
        sub.add_argument('--seed', type=int, default=20261022)
        sub.add_argument('--hot-percent', type=float, default=1.0)
        sub.add_argument('--events-rows', type=int)
        sub.add_argument('--batch-size', type=int, default=100)

    sub = commands.add_parser('setup')
    trial_options(sub)
    sub.add_argument('--listen-ip', required=True)
    sub.add_argument('--client-ip', required=True)
    sub.set_defaults(func=cmd_setup)
    sub = commands.add_parser('workload')
    trial_options(sub)
    sub.set_defaults(func=cmd_workload)
    sub = commands.add_parser('verify')
    trial_options(sub)
    sub.set_defaults(func=cmd_verify)
    sub = commands.add_parser('stop')
    sub.add_argument('--root', required=True)
    sub.add_argument('--bin-dir', default=DEFAULT_BIN_DIR)
    sub.add_argument('--remove', action='store_true', help='then delete the cluster directory')
    sub.set_defaults(func=cmd_stop)
    sub = commands.add_parser('stop-clean')
    sub.add_argument('--root', required=True)
    sub.add_argument('--bin-dir', default=DEFAULT_BIN_DIR)
    sub.set_defaults(func=cmd_stop_clean)
    sub = commands.add_parser('start')
    sub.add_argument('--root', required=True)
    sub.add_argument('--bin-dir', default=DEFAULT_BIN_DIR)
    sub.add_argument('--log-dir', required=True)
    sub.set_defaults(func=cmd_start)
    sub = commands.add_parser('archive')
    sub.add_argument('--root', required=True)
    sub.add_argument('--bin-dir', default=DEFAULT_BIN_DIR)
    sub.add_argument('--log-dir', required=True)
    sub.add_argument('--command', required=True, help='archive_command (pgBackRest archive-push)')
    sub.set_defaults(func=cmd_archive)
    sub = commands.add_parser('backup-mode-probe')
    sub.add_argument('--root', required=True)
    sub.add_argument('--bin-dir', default=DEFAULT_BIN_DIR)
    sub.add_argument('--repeats', type=agent.positive_int, default=10)
    sub.set_defaults(func=cmd_backup_mode_probe)

    # EXP-12 (T10a).
    def load_options(sub, *, pgbench=False):
        sub.add_argument('--root', required=True)
        sub.add_argument('--bin-dir', default=DEFAULT_BIN_DIR)
        sub.add_argument('--load-dir', default=measure.LOAD_LOG_DIR,
                         help="the load's logs and registry on the source (tmpfs)")
        if pgbench:
            sub.add_argument('--clients', type=agent.positive_int, default=32)
            sub.add_argument('--threads', type=agent.positive_int, default=4)
            sub.add_argument('--seed', type=int, required=True, help="pgbench's --random-seed")
            sub.add_argument('--scripts', default=str(LOAD_SCRIPT_DIR), help='commit.sql and abort.sql')

    sub = commands.add_parser('loaddb-setup')
    load_options(sub)
    sub.add_argument('--log-dir', required=True)
    sub.add_argument('--clients', type=agent.positive_int, default=32, help="load_progress's rows: one per client")
    sub.set_defaults(func=cmd_loaddb_setup)
    sub = commands.add_parser('load-start')
    load_options(sub, pgbench=True)
    sub.add_argument('--run-id', required=True)
    sub.add_argument('--rate', type=int, required=True, help='transactions per second (-R); 0 for a closed loop')
    sub.add_argument('--duration-s', type=agent.positive_int, default=14400, help="pgbench's -T")
    sub.add_argument('--kind', default='load', help='what the run is for, recorded (load, restart, smoke)')
    sub.set_defaults(func=cmd_load_start)
    sub = commands.add_parser('load-stop')
    load_options(sub)
    sub.add_argument('--run-id', help='the run to stop (default: the current one)')
    sub.add_argument('--signal', choices=('ALRM', 'INT'), default='ALRM')
    sub.set_defaults(func=cmd_load_stop)
    sub = commands.add_parser('load-capacity')
    load_options(sub, pgbench=True)
    sub.add_argument('--run-id', required=True, help="the runs' ids: ID-warmup, ID-1, ID-2, ...")
    sub.add_argument('--warmup-s', type=int, default=30)
    sub.add_argument('--seconds', type=agent.positive_int, default=60)
    sub.add_argument('--runs', type=agent.positive_int, default=1)
    sub.set_defaults(func=cmd_load_capacity)
    sub = commands.add_parser('load-status')
    sub.add_argument('--load-dir', default=measure.LOAD_LOG_DIR)
    sub.set_defaults(func=cmd_load_status)
    sub = commands.add_parser('load-check')
    load_options(sub)
    sub.set_defaults(func=cmd_load_check)
    sub = commands.add_parser('backup-hold')
    sub.add_argument('--root', required=True)
    sub.add_argument('--bin-dir', default=DEFAULT_BIN_DIR)
    sub.add_argument('--label', default='t10a')
    sub.add_argument('--database', default=run_study.DATABASE)
    sub.set_defaults(func=cmd_backup_hold)
    sub = commands.add_parser('snapshot')
    sub.add_argument('--root', required=True)
    sub.add_argument('--bin-dir', default=DEFAULT_BIN_DIR)
    sub.add_argument('--log-dir', required=True)
    sub.add_argument('--snapshot', required=True, help='the copy of the data directory, on the same disks')
    sub.set_defaults(func=cmd_snapshot)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    emit(json.loads(json.dumps(args.func(args), default=str)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
