"""Laptop runs: a real PostgreSQL cluster on this machine, refreshed by each method, recorded live.

The lanes run one after another, so they don't compete for the laptop's disk. pg_basebackup's lanes
run first, because they need the server running; pgdelta's read the stopped data directory. At the end
the last pgdelta copy is started and its rows compared with the source.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from bench.config import RunConfig
from bench.lanes import pump_pg_basebackup
from bench import engine
from bench.steps import no_steps
from pgdelta.events import EventLog

REPO = Path(__file__).resolve().parents[1]
OPTS = "-c listen_addresses=127.0.0.1 -c unix_socket_directories=''"


def pg_bin(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    if shutil.which('pg_config'):
        return Path(subprocess.run(['pg_config', '--bindir'], capture_output=True, text=True, check=True).stdout.strip())
    return Path('/opt/homebrew/opt/postgresql@17/bin')


def where_clause(cfg: RunConfig) -> str:
    rows = cfg.scale * 100_000
    return 'aid % 100 = 0' if cfg.change == 'uniform' else f'aid <= {rows // 100}'


def run_laptop(cfg: RunConfig, run_dir: Path, work: Path, pgbin: Path, log=print, steps=None) -> dict:
    """Run every lane of `cfg`, writing events into run_dir. Returns facts for the report."""
    step = steps.step if steps else no_steps
    env = {**os.environ, 'LC_ALL': os.environ.get('LC_ALL', 'en_US.UTF-8'), 'LANG': os.environ.get('LANG', 'en_US.UTF-8')}

    def run(*argv, **kw):
        return subprocess.run([str(a) for a in argv], check=True, env=env, **kw)

    def ctl(action, data, port=None):
        argv = [pgbin / 'pg_ctl', '-D', data, '-w']
        argv += (['-o', f'-p {port} {OPTS}', '-l', work / f'{Path(data).name}.log', 'start'] if action == 'start'
                 else ['-m', 'fast', 'stop'])
        run(*argv, stdout=subprocess.DEVNULL)

    def sql(port, *statements):
        argv = [pgbin / 'psql', '-h', '127.0.0.1', '-p', str(port), '-U', 'postgres', '-qAt']
        for statement in statements:
            argv += ['-c', statement]
        return run(*argv, 'postgres', capture_output=True, text=True).stdout.strip()

    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    for d in ('S', 'B', 'O', 'O1', 'Bw', 'full'):
        shutil.rmtree(work / d, ignore_errors=True)
    found = re.search(r'\d+(?:\.\d+)+', run(pgbin / 'postgres', '--version', capture_output=True, text=True).stdout)
    facts = {'postgres': found.group(0) if found else 'unknown', 'machine': os.uname().machine,
             'cpus': os.cpu_count(), 'lanes': {}}
    log(f'building the database (scale {cfg.scale}, PostgreSQL {facts["postgres"]}) …')
    try:
        with step('build'):
            run(pgbin / 'initdb', '-D', work / 'S', '-U', 'postgres', '--data-checksums', '-A', 'trust',
                stdout=subprocess.DEVNULL)
            ctl('start', work / 'S', 55432)
            run(pgbin / 'pgbench', '-i', '-s', cfg.scale, '-h', '127.0.0.1', '-p', 55432, '-U', 'postgres', 'postgres',
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ctl('stop', work / 'S')
        with step('copy'):
            shutil.copytree(work / 'S', work / 'B', symlinks=True)
            ctl('start', work / 'S', 55432)
            sql(55432, f'UPDATE pgbench_accounts SET abalance = abalance + 7 WHERE {where_clause(cfg)}', 'CHECKPOINT')
            expect = sql(55432, 'SELECT count(*), sum(abalance) FROM pgbench_accounts')
            facts['database_bytes'] = sum(f.stat().st_size for f in (work / 'S').rglob('*') if f.is_file())
        running = True
        for lane in cfg.lanes():
            path = run_dir / f"events-{lane['id']}.jsonl"
            log(f"lane {lane['id']} …")
            with step('lane:' + lane['id']):
                if lane['method'] == 'pg_basebackup':
                    if not running:
                        ctl('start', work / 'S', 55432)
                        running = True
                    shutil.rmtree(work / 'full', ignore_errors=True)
                    events = EventLog(path, method=lane['id'])
                    events.emit('start')
                    proc = subprocess.Popen([str(pgbin / 'pg_basebackup'), '-h', '127.0.0.1', '-p', '55432', '-U',
                                             'postgres', '-D', str(work / 'full'), '-X', 'stream', '-c', 'fast', '-P'],
                                            stderr=subprocess.PIPE, env=env)
                    total = pump_pg_basebackup(proc.stderr, events)
                    if proc.wait():
                        raise RuntimeError('pg_basebackup failed')
                    elapsed = round(time.perf_counter() - events.started, 4)
                    events.emit('done', ok=True, elapsed=elapsed, bytes_sent=total, total_bytes=total)
                    events.close()
                    facts['lanes'][lane['id']] = {'elapsed': elapsed, 'bytes_sent': total, 'total_bytes': total}
                else:
                    if running:
                        ctl('stop', work / 'S')
                        running = False
                    inplace = lane['method'] == 'pgdelta-inplace'
                    target = work / ('Bw' if inplace else 'O')
                    shutil.rmtree(target, ignore_errors=True)
                    if inplace:  # a fresh copy of B to patch, made before the clock starts
                        shutil.copytree(work / 'B', target, symlinks=True)
                    stats = run_dir / f"stats-{lane['id']}.json"
                    started = time.perf_counter()
                    run(sys.executable, '-m', 'pgdelta',
                        *engine.local_args(lane['method'], work / 'S', target if inplace else work / 'B', target,
                                           str(pgbin / 'pg_controldata')),
                        '--events', path, '--stats-json', stats, cwd=REPO, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
                    result = engine.facts(stats, time.perf_counter() - started, facts['database_bytes'])
                    _retag(path, lane['id'])
                    ctl('start', target, 55434)  # every copy is started and checked
                    result['copy'] = sql(55434, 'SELECT count(*), sum(abalance) FROM pgbench_accounts')
                    ctl('stop', target)
                    result['match'] = result['copy'] == expect
                    facts['lanes'][lane['id']] = result
        if running:
            ctl('stop', work / 'S')
    finally:
        for d in ('S', 'O', 'O1', 'Bw'):
            if (work / d / 'postmaster.pid').exists():
                subprocess.run([str(pgbin / 'pg_ctl'), '-D', str(work / d), '-m', 'fast', '-w', 'stop'],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    checked = [lane for lane in facts['lanes'].values() if 'match' in lane]
    facts['verify'] = {'source': expect, 'copy': checked[0]['copy'] if checked else None,
                       'match': all(lane['match'] for lane in checked), 'checked': bool(checked)}
    log(f"copy check: source {expect}, {len(checked)} pgdelta copies: {'all MATCH' if facts['verify']['match'] else 'MISMATCH'}")
    return facts


def _retag(path: Path, lane_id: str) -> None:
    """pgdelta's events say method "pgdelta"; the viewer wants the lane's id (pgdelta-inplace-r2, ...)."""
    import json
    events = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    Path(path).write_text(''.join(json.dumps({**e, 'method': lane_id}, separators=(',', ':')) + '\n' for e in events))
