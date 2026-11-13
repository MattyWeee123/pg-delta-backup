"""A laptop race: build a database, change it, then refresh the old copy two ways, recording both live.

Lane 1: pg_basebackup copies all of S (its --progress output becomes progress events).
Lane 2: pgdelta refreshes B into O (its --events file).
Then O is started and its rows compared with S's. The lanes run one after the other, so they don't
compete for the laptop's disk; the page replays them side by side, both starting at zero.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from pgdelta.events import EventLog
from viz import runs

PROGRESS = re.compile(rb'(\d+)/(\d+) kB')
OPTS = "-c listen_addresses=127.0.0.1 -c unix_socket_directories=''"


def pg_bin(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    if shutil.which('pg_config'):
        return Path(subprocess.run(['pg_config', '--bindir'], capture_output=True, text=True, check=True).stdout.strip())
    return Path('/opt/homebrew/opt/postgresql@17/bin')


def race(name: str, root: Path, work: Path, scale: int, change: str, pgbin: Path, log=print) -> Path:
    env = {**os.environ, 'LC_ALL': os.environ.get('LC_ALL', 'en_US.UTF-8'), 'LANG': os.environ.get('LANG', 'en_US.UTF-8')}

    def run(*argv, **kw):
        return subprocess.run([str(a) for a in argv], check=True, env=env, **kw)

    def ctl(action, data, port=None):
        argv = [pgbin / 'pg_ctl', '-D', data, '-w']
        if action == 'start':
            argv += ['-o', f'-p {port} {OPTS}', '-l', work / f'{Path(data).name}.log', 'start']
        else:
            argv += ['-m', 'fast', 'stop']
        run(*argv, stdout=subprocess.DEVNULL)

    def sql(port, *statements):
        argv = [pgbin / 'psql', '-h', '127.0.0.1', '-p', str(port), '-U', 'postgres', '-qAt']
        for statement in statements:
            argv += ['-c', statement]
        return run(*argv, 'postgres', capture_output=True, text=True).stdout.strip()

    work = Path(work).resolve()
    for d in ('S', 'B', 'O', 'full'):
        shutil.rmtree(work / d, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    rows = scale * 100_000
    where = 'aid % 100 = 0' if change == 'uniform' else f'aid <= {rows // 100}'
    found = re.search(r'\d+(?:\.\d+)+', run(pgbin / 'postgres', '--version', capture_output=True, text=True).stdout)
    version = found.group(0) if found else 'unknown'
    run_dir = runs.new_run(root, name, f'Laptop race: pg_basebackup vs naive pgdelta, {change} change, scale {scale}',
                           [{'id': 'pg_basebackup', 'label': 'pg_basebackup (copies everything)'},
                            {'id': 'pgdelta', 'label': 'pgdelta naive (sends what differs)'}],
                           setup={'where': 'laptop', 'postgres': version, 'scale': scale, 'rows': rows,
                                  'change': f'1% of rows, {change}'})
    log(f'run {name}: building the database (scale {scale}) …')
    run(pgbin / 'initdb', '-D', work / 'S', '-U', 'postgres', '--data-checksums', '-A', 'trust', stdout=subprocess.DEVNULL)
    try:
        ctl('start', work / 'S', 55432)
        run(pgbin / 'pgbench', '-i', '-s', scale, '-h', '127.0.0.1', '-p', 55432, '-U', 'postgres', 'postgres',
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ctl('stop', work / 'S')
        shutil.copytree(work / 'S', work / 'B', symlinks=True)
        ctl('start', work / 'S', 55432)
        sql(55432, f'UPDATE pgbench_accounts SET abalance = abalance + 7 WHERE {where}', 'CHECKPOINT')
        expect = sql(55432, 'SELECT count(*), sum(abalance) FROM pgbench_accounts')

        log('lane 1: pg_basebackup …')
        events = EventLog(run_dir / 'events-pg_basebackup.jsonl', method='pg_basebackup')
        events.emit('start')
        proc = subprocess.Popen([str(pgbin / 'pg_basebackup'), '-h', '127.0.0.1', '-p', '55432', '-U', 'postgres',
                                 '-D', str(work / 'full'), '-X', 'stream', '-c', 'fast', '-P'],
                                stderr=subprocess.PIPE, env=env)
        done_kb = total_kb = 0
        buffer = b''
        while chunk := proc.stderr.read1(4096):
            buffer += chunk
            for match in PROGRESS.finditer(buffer):
                done_kb, total_kb = int(match.group(1)), int(match.group(2))
            buffer = buffer[-64:]
            if events.due():
                events.emit('progress', bytes_sent=done_kb * 1024, total_bytes=total_kb * 1024)
        if proc.wait():
            raise RuntimeError('pg_basebackup failed')
        events.emit('done', ok=True, elapsed=round(time.perf_counter() - events.started, 4),
                    bytes_sent=total_kb * 1024, total_bytes=total_kb * 1024)
        events.close()
        ctl('stop', work / 'S')

        log('lane 2: pgdelta …')
        repo = Path(__file__).resolve().parents[1]
        run(sys.executable, '-m', 'pgdelta', 'local', '--source', work / 'S', '--basis', work / 'B',
            '--output', work / 'O', '--events', run_dir / 'events-pgdelta.jsonl',
            '--stats-json', run_dir / 'pgdelta-stats.json', cwd=repo)

        ctl('start', work / 'O', 55433)
        got = sql(55433, 'SELECT count(*), sum(abalance) FROM pgbench_accounts')
        ctl('stop', work / 'O')
    finally:
        for d in ('S', 'O'):
            if (work / d / 'postmaster.pid').exists():
                subprocess.run([str(pgbin / 'pg_ctl'), '-D', str(work / d), '-m', 'fast', '-w', 'stop'],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    meta = json.loads((run_dir / 'meta.json').read_text())
    meta.update(finished=True, verify={'source': expect, 'copy': got, 'match': bool(expect) and expect == got})
    runs.write_meta(run_dir, meta)
    log(f"copy check: source {expect}, refreshed copy {got}: {'MATCH' if meta['verify']['match'] else 'MISMATCH'}")
    return run_dir
