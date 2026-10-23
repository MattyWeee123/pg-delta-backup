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

Each prints one RESULT_PREFIX JSON line (see agent.py). The trial options
(--scale, --pattern, --fraction, --seed, --hot-percent, --events-rows,
--batch-size) are EXP-01's, parsed by run_study's own parser.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.change_patterns import run_study, workloads  # noqa: E402
from experiments.network.agent import emit  # noqa: E402

DEFAULT_BIN_DIR = '/usr/lib/postgresql/17/bin'
TRIAL_OPTIONS = ('scale', 'pattern', 'fraction', 'seed', 'hot_percent', 'events_rows',
                 'batch_size')


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
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    emit(json.loads(json.dumps(args.func(args), default=str)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
