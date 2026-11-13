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
    for d in ('S', 'B', 'O', 'full'):
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
                    shutil.rmtree(work / 'O', ignore_errors=True)
                    stats = run_dir / f"stats-{lane['id']}.json"
                    run(sys.executable, '-m', 'pgdelta', 'local', '--source', work / 'S', '--basis', work / 'B',
                        '--output', work / 'O', '--chunk-size', cfg.chunk_size, '--events', path, '--stats-json', stats,
                        cwd=REPO, stderr=subprocess.DEVNULL)
                    facts['lanes'][lane['id']] = _pgdelta_facts(stats)
        if running:
            ctl('stop', work / 'S')
        got = None
        if 'pgdelta' in cfg.methods:
            with step('check'):
                ctl('start', work / 'O', 55433)
                got = sql(55433, 'SELECT count(*), sum(abalance) FROM pgbench_accounts')
                ctl('stop', work / 'O')
    finally:
        for d in ('S', 'O'):
            if (work / d / 'postmaster.pid').exists():
                subprocess.run([str(pgbin / 'pg_ctl'), '-D', str(work / d), '-m', 'fast', '-w', 'stop'],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    facts['verify'] = {'source': expect, 'copy': got, 'match': got is None or (bool(expect) and expect == got),
                       'checked': got is not None}
    log(f"copy check: source {expect}, refreshed copy {got}: {'MATCH' if facts['verify']['match'] else 'MISMATCH'}")
    return facts


def _pgdelta_facts(stats_path: Path) -> dict:
    import json
    report = json.loads(Path(stats_path).read_text())
    c = report.get('counts', {})
    return {'elapsed': report['elapsed_seconds'], 'bytes_sent': report['bytes_sent_total'],
            'total_bytes': c.get('bytes_read', 0), 'round_trips': c.get('round_trips', 0),
            'chunks_changed': c.get('chunks_changed', 0), 'chunks_same': c.get('chunks_same', 0),
            'chunks_new': c.get('chunks_new', 0), 'ok': report.get('ok', False)}
