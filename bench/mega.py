"""Three-host live HammerDB backup experiment. Planning is the default; --execute is explicit."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import random
import re
import shlex
import statistics
import subprocess
import tarfile
import tempfile
import time

from bench import gcp
from benchmarks.backup import atomic_json
from benchmarks.hammerdb_experiment import read_trial, Window, matched_window, window_stats

REPO = Path(__file__).resolve().parents[1]
METHODS = ('pg_basebackup', 'pgdelta', 'pgdelta-inplace', 'native-incremental')
PG = '/usr/lib/postgresql/17/bin/'
DEFAULTS = dict(name='small', warehouses=2, build_vu=2, vu=2, rampup=1, duration=6,
                churn_minutes=1, repeats=3, warmups=1, links=['100mbit', '1gbit'],
                methods=list(METHODS), seed=42, jobs=2, hash_ahead='1G', interval=0.1,
                timeout=3600, checkpoint='fast', cache='warm',
                hammerdb_image='', zones=['us-central1-a'] * 3)


def load_config(path):
    raw = json.loads(Path(path).read_text())
    unknown = set(raw) - set(DEFAULTS) - {'source', 'destination', 'driver'}
    if unknown:
        raise ValueError(f'unknown configuration fields: {sorted(unknown)}')
    cfg = DEFAULTS | raw
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,30}', cfg['name']):
        raise ValueError('name must be a short lowercase identifier')
    hosts = [cfg.get(k) for k in ('source', 'destination', 'driver')]
    if any(not isinstance(h, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', h) for h in hosts):
        raise ValueError('source, destination and driver must be existing GCE instance names')
    if len(set(hosts)) != 3:
        raise ValueError('three distinct hosts are required')
    if len(cfg['zones']) != 3 or any(not re.fullmatch(r'[a-z0-9-]+', z) for z in cfg['zones']):
        raise ValueError('zones must list source, destination and driver zones')
    for key in ('warehouses', 'build_vu', 'vu', 'rampup', 'duration', 'churn_minutes',
                'repeats', 'jobs', 'timeout'):
        if type(cfg[key]) is not int or cfg[key] < 1:
            raise ValueError(f'{key} must be a positive integer')
    if type(cfg['warmups']) is not int or cfg['warmups'] < 0:
        raise ValueError('warmups must be a nonnegative integer')
    if not 0.05 <= cfg['interval'] <= 10:
        raise ValueError('interval must be between 0.05 and 10 seconds')
    if not cfg['methods'] or len(set(cfg['methods'])) != len(cfg['methods']) or any(m not in METHODS for m in cfg['methods']):
        raise ValueError(f'methods must be unique choices from {METHODS}')
    if 'pg_basebackup' not in cfg['methods']:
        raise ValueError('pg_basebackup is required as the comparison denominator')
    if not cfg['links'] or any(not re.fullmatch(r'(full|[1-9][0-9]*(mbit|gbit))', r) for r in cfg['links']):
        raise ValueError('links must contain full or positive integer mbit/gbit rates')
    if cfg['checkpoint'] not in ('fast', 'spread') or cfg['cache'] not in ('warm', 'cold'):
        raise ValueError('checkpoint: fast/spread; cache: warm/cold (before rampup)')
    if not re.fullmatch(r'[1-9][0-9]*[MG]', cfg['hash_ahead']):
        raise ValueError('hash_ahead must be a positive M/G size')
    if not re.fullmatch(r'[a-zA-Z0-9./_-]+@sha256:[a-f0-9]{64}', cfg['hammerdb_image']):
        raise ValueError('hammerdb_image must be pinned by sha256 digest')
    return cfg


def source_files():
    for folder in ('bench', 'pgdelta'):
        yield from sorted((REPO / folder).rglob('*.py'))
    yield from sorted((REPO / 'benchmarks').glob('*.py'))
    yield from sorted((REPO / 'benchmarks/workloads/hammerdb').glob('*.tcl'))


def fingerprint():
    digest = hashlib.sha256()
    for path in source_files():
        digest.update(str(path.relative_to(REPO)).encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()


def trial_plan(cfg):
    rng = random.Random(cfg['seed'])
    result = []
    for link in cfg['links']:
        for repeat in range(-cfg['warmups'], cfg['repeats']):
            methods = list(cfg['methods'])
            rng.shuffle(methods)
            for method in methods:
                pair = ['control', method]
                rng.shuffle(pair)
                result.append(dict(link=link, repeat=repeat, warmup=repeat < 0,
                                   method=method, order=pair))
    return result


def require_validation(cfg, path, digest):
    if cfg['warehouses'] <= 10:
        return
    if path is None:
        raise ValueError('warehouses > 10 requires --validated-run from a successful small run')
    record = json.loads(Path(path).read_text())
    old = record['config']
    if record.get('status') != 'PASS_RESTORE_SMOKE' or record.get('code_sha256') != digest:
        raise ValueError('small run must pass and use this exact benchmark/engine source fingerprint')
    if old['warehouses'] > 10 or not set(cfg['methods']) <= set(old['methods']):
        raise ValueError('validation must cover every requested method at <= 10 warehouses')
    if old['hammerdb_image'] != cfg['hammerdb_image']:
        raise ValueError('validation must use the same pinned HammerDB image')
    completed = {r['treatment'] for r in record.get('trials', [])
                 if r.get('status') == 'PASS_RESTORE_SMOKE' and not r.get('warmup')}
    if not set(cfg['methods']) <= completed:
        raise ValueError('validation must retain successful measured trials of every requested method')


class Node:
    def __init__(self, name, zone, root):
        self.vm = gcp.VM(name, zone)
        self.root = root
        self.owned = False
        self.locked = False
        info = json.loads(gcp.gcloud('compute', 'instances', 'describe', name,
                                    '--zone', zone, '--format=json').stdout)
        self.ip = str(ipaddress.IPv4Address(info['networkInterfaces'][0]['networkIP']))
        self.info = info
        self.interface = self.shell('ip -o route show default').split('dev ', 1)[1].split()[0]
        if not re.fullmatch(r'[a-zA-Z0-9_.-]+', self.interface):
            raise ValueError('invalid interface')

    def shell(self, command, check=True):
        return self.vm.ssh(command, check=check, timeout=120)

    def argv(self, argv, check=True):
        return self.shell(shlex.join(map(str, argv)), check=check)

    def put_json(self, path, value):
        with tempfile.TemporaryDirectory() as temp:
            local = Path(temp) / 'spec.json'
            local.write_text(json.dumps(value))
            self.vm.scp_to(local, path)

    def get_json(self, path):
        return json.loads(self.argv(['cat', path]))

    def get_json_network(self, table):
        return json.loads(self.shell(self.agent('network', '--table', table)))

    def agent(self, action, *args):
        return f'cd {shlex.quote(self.root + "/code")} && ' + shlex.join(
            ['python3', '-m', 'bench.mega_agent', action, *map(str, args)])

    def command(self, ident, argv, timeout, background=False):
        prefix = f'{self.root}/evidence/{ident}'
        spec = prefix + '.spec.json'
        self.put_json(spec, dict(prefix=prefix, argv=argv, timeout=timeout))
        proc = self.vm.popen(self.agent('execute', '--spec', spec),
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if background:
            return proc
        try:
            _, err = proc.communicate(timeout=timeout + 120)
        except subprocess.TimeoutExpired:
            self.cancel(ident)
            proc.kill()
            raise RuntimeError(f'{ident}: worker connection timeout') from None
        except BaseException:
            self.cancel(ident)
            proc.kill()
            raise
        record = self.get_json(prefix + '.json')
        if proc.returncode or record['returncode']:
            raise RuntimeError(f'{ident} failed; retained worker evidence at {prefix}; '
                               + err.decode(errors='replace')[-200:])
        return record

    def psql(self, sql, data=None, port=5432):
        socket = data or self.root
        return self.argv([PG + 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-h', socket,
                          '-p', str(port), '-U', gcp.login(), '-d', 'postgres', '-Atc', sql])

    def cancel(self, ident):
        self.shell(self.agent('cancel', '--spec', self.root + '/evidence/' + ident))


def remove_pgdata(node, path):
    # A failed stop must never be followed by deleting a live database.
    script = '''import os,pathlib,shutil,sys
p=pathlib.Path(sys.argv[1]); pid=p/'postmaster.pid'
if pid.exists():
 try: os.kill(int(pid.read_text().splitlines()[0]),0)
 except ProcessLookupError: pass
 else: raise RuntimeError('refusing to delete possibly running PostgreSQL: '+str(p))
if p.exists(): shutil.rmtree(p)
'''
    node.argv(['python3', '-c', script, path])


def pg_start(node, directory, port=5432):
    options = f'-p {port} -c unix_socket_directories={node.root}'
    if port != 5432:
        options += " -c listen_addresses='' -c restore_command='' -c primary_conninfo=''"
    node.argv([PG + 'pg_ctl', '-D', directory, '-l', node.root + '/postgres.log', '-w',
               'start', '-o', options])


def pg_stop(node, directory):
    node.argv([PG + 'pg_ctl', '-D', directory, '-w', 'stop', '-m', 'fast'])


def workload(driver, src, cfg, ident, duration=None):
    folder = f'{driver.root}/evidence/{ident}'
    spec = folder + '.workload.json'
    settings = dict(vu=cfg['vu'], rampup=cfg['rampup'], duration=duration or cfg['duration'])
    driver.put_json(spec, dict(directory=folder, timeout=cfg['timeout'], name='pgd-' + ident,
                              image=cfg['hammerdb_image'], network='host', host=src.ip,
                              password='disposable-benchmark', settings=settings,
                              scripts_dir=driver.root + '/code/benchmarks/workloads/hammerdb'))
    proc = driver.vm.popen(driver.agent('workload', '--spec', spec),
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.monotonic() + cfg['rampup'] * 60 + 180
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError('HammerDB failed before rampup; inspect ' + folder)
        if driver.shell(f'test -f {shlex.quote(folder + "/ready")} && echo ready', check=False).strip() == 'ready':
            return proc, folder
        time.sleep(2)
    driver.cancel(ident)
    proc.kill()
    raise RuntimeError('HammerDB rampup timed out')


def finish_workload(driver, proc, folder, local, timeout):
    try:
        _, err = proc.communicate(timeout=timeout + 120)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError('HammerDB collection timed out') from None
    if proc.returncode:
        raise RuntimeError('HammerDB failed: ' + err.decode(errors='replace')[-300:])
    local.mkdir(parents=True)
    for name in ('hammerdb.log', 'hammer.DB'):
        driver.vm.scp_from(folder + '/' + name, local / name)
    return read_trial(local / 'hammerdb.log', local / 'hammer.DB')


def setup(nodes, cfg, out):
    src, dst, driver = nodes
    with tempfile.TemporaryDirectory() as temp:
        bundle = Path(temp) / 'code.tgz'
        with tarfile.open(bundle, 'w:gz') as tar:
            for path in source_files():
                tar.add(path, arcname=str(path.relative_to(REPO)))
        for node in nodes:
            # Exclusive experiment roots. Never reuse or delete an existing dataset.
            node.argv(['mkdir', '/data/pgd-mega.lock'])
            node.locked = True
            node.argv(['mkdir', node.root])
            node.owned = True
            node.argv(['mkdir', node.root + '/code', node.root + '/evidence'])
            node.vm.scp_to(bundle, node.root + '/code.tgz')
            node.argv(['tar', '-xzf', node.root + '/code.tgz', '-C', node.root + '/code'])
            for argv in ([PG + 'postgres', '--version'], ['python3', '--version'], ['sudo', '-n', 'true']):
                node.argv(argv)
    driver.argv(['docker', 'pull', cfg['hammerdb_image']])
    src.argv([PG + 'initdb', '-D', src.root + '/S', '--data-checksums', '-A', 'trust',
              '-E', 'UTF8', '--locale=C.UTF-8'])
    configure = '''import pathlib,sys
p=pathlib.Path(sys.argv[1]); source,dest,driver,user=sys.argv[2:]
with (p/'postgresql.conf').open('a') as f:
 f.write("\\nlisten_addresses = '"+source+"'\\nsummarize_wal=on\\nwal_summary_keep_time='30d'\\nmax_wal_senders=10\\nmax_replication_slots=10\\n")
with (p/'pg_hba.conf').open('a') as f:
 f.write('\\nhost all all '+driver+'/32 trust\\nhost replication '+user+' '+dest+'/32 trust\\n')
'''
    src.argv(['python3', '-c', configure, src.root + '/S', src.ip, dst.ip, driver.ip, gcp.login()])
    pg_start(src, src.root + '/S')
    # The established scripts use the postgres role. initdb's owner remains the SSH user.
    src.psql('CREATE ROLE postgres SUPERUSER LOGIN') if gcp.login() != 'postgres' else None
    driver.command('build', ['docker', 'run', '--rm', '--name', 'pgd-build-' + cfg['name'], '--network', 'host',
        '-e', f'PGBENCH_HOST={src.ip}', '-e', 'PGBENCH_PASS=disposable-benchmark',
        '-e', f'TPCC_WAREHOUSES={cfg["warehouses"]}', '-e', f'TPCC_BUILD_VU={cfg["build_vu"]}',
        '-v', driver.root + '/code/benchmarks/workloads/hammerdb:/hdb:ro',
        cfg['hammerdb_image'], './hammerdbcli', 'auto', '/hdb/build_schema.tcl'], cfg['timeout'])
    dst.command('basis', [PG + 'pg_basebackup', '-h', src.ip, '-U', gcp.login(), '-D',
                         dst.root + '/B', '-X', 'stream', '-c', cfg['checkpoint'],
                         '--manifest-checksums=SHA256'], cfg['timeout'])
    dst.command('basis-verify', [PG + 'pg_verifybackup', dst.root + '/B'], cfg['timeout'])
    proc, folder = workload(driver, src, cfg, 'churn', cfg['churn_minutes'])
    finish_workload(driver, proc, folder, out / 'churn', cfg['timeout'])
    pg_stop(src, src.root + '/S')
    src.argv(['mv', src.root + '/S', src.root + '/target'])
    facts = {'database_bytes': int(src.argv(['du', '-sb', src.root + '/target']).split()[0]),
             'logical_database_bytes': None, 'nodes': {}}
    for role, node in zip(('source', 'destination', 'driver'), nodes):
        before_clock = time.time()
        remote_clock = float(node.argv(['date', '+%s.%N']))
        after_clock = time.time()
        if not before_clock - 1 <= remote_clock <= after_clock + 1:
            raise RuntimeError(f'{role}: synchronize clocks before comparing workload windows')
        facts['nodes'][role] = dict(instance=node.vm.name, zone=node.vm.zone,
            machine=node.info['machineType'].split('/')[-1],
            kernel=node.argv(['uname', '-a']), memory=node.argv(['cat', '/proc/meminfo']),
            disks=node.argv(['lsblk', '-b', '-J']), postgres=node.argv([PG + 'postgres', '--version']),
            cpu=node.argv(['lscpu', '-J']), clock_offset_bounds_seconds=[remote_clock-after_clock, remote_clock-before_clock])
    facts['ping'] = src.argv(['ping', '-c', '5', dst.ip], check=False)
    return facts


def reset_source(src, cfg, ident):
    # target is immutable, and every treatment/control begins with identical source bytes.
    src.argv(['cp', '-a', '--reflink=never', src.root + '/target', src.root + '/S'])
    if cfg['cache'] == 'cold':
        src.shell('sync && echo 3 | sudo -n tee /proc/sys/vm/drop_caches >/dev/null')
    pg_start(src, src.root + '/S')


def stats_snapshot(src):
    views = ('pg_stat_database', 'pg_stat_wal', 'pg_stat_checkpointer', 'pg_stat_io', 'pg_replication_slots')
    parts = [f"'{view}', (SELECT coalesce(json_agg(t),'[]'::json) FROM {view} t)" for view in views]
    return json.loads(src.psql('SELECT json_build_object(' + ','.join(parts) + ')'))


def verify_output(dst, target, cfg, ident):
    # Manifest checks are independent; pgdelta uses its engine verification, then the same restore smoke.
    pg_start(dst, target, port=5434)
    try:
        # No external WAL or upstream streaming may be needed for this backup package.
        tables = dst.psql("SELECT count(*) FROM pg_database WHERE datname='tpcc'", port=5434).strip()
        if tables != '1':
            raise RuntimeError('restored package is missing tpcc')
        sql = "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name IN ('warehouse','district','customer','history','new_order','orders','order_line','item','stock')"
        names = dst.argv([PG + 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-h', dst.root, '-p', '5434',
                          '-U', gcp.login(), '-d', 'tpcc', '-Atc', sql]).strip()
        if names != '9':
            raise RuntimeError('restored TPROC-C schema incomplete')
        result = dst.command(ident + '-amcheck', [PG + 'pg_amcheck', '--all', '--install-missing', '-h', dst.root,
                             '-p', '5434', '-U', gcp.login()], cfg['timeout'])
        return {'status': 'PASS_RESTORE_SMOKE', 'amcheck_seconds': result['seconds'],
                'scope': 'package startup, nine TPROC-C tables, pg_amcheck; no exact-cutoff logical oracle'}
    finally:
        pg_stop(dst, target)


def backup(src, dst, cfg, method, ident):
    target = dst.root + '/O'
    timeout = cfg['timeout']
    phases = {}
    if method in ('pg_basebackup', 'native-incremental'):
        increment = method == 'native-incremental'
        argv = [PG + 'pg_basebackup', '-h', src.ip, '-U', gcp.login(), '-D',
                dst.root + '/I' if increment else target, '-X', 'stream', '-c', cfg['checkpoint'],
                '--manifest-checksums=SHA256']
        if increment:
            argv += ['--incremental=' + dst.root + '/B/backup_manifest']
        phases['capture'] = dst.command(ident + '-capture', argv, timeout)
        if increment:
            phases['increment_verify'] = dst.command(ident + '-increment-verify',
                [PG + 'pg_verifybackup', dst.root + '/I'], timeout)
            phases['combine'] = dst.command(ident + '-combine', [PG + 'pg_combinebackup', '--copy',
                '--manifest-checksums=SHA256', '-o', target, dst.root + '/B', dst.root + '/I'], timeout)
        phases['verify'] = dst.command(ident + '-verify', [PG + 'pg_verifybackup', target], timeout)
    else:
        inplace = method == 'pgdelta-inplace'
        argv = ['python3', '-m', 'pgdelta', 'receive', '--basis', target if inplace else dst.root + '/B',
                '--listen', '0.0.0.0:7000', '--jobs', str(cfg['jobs']), '--fsync', 'worker',
                '--prefetch-basis', 'off', '--basis-pg-controldata', PG + 'pg_controldata',
                '--stats-json', dst.root + '/evidence/' + ident + '-receive-stats.json']
        argv += ['--in-place', '--verify-scope', 'as-written'] if inplace else ['--output', target]
        receiver = dst.command(ident + '-receiver', argv, timeout, background=True)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if receiver.poll() is not None:
                    raise RuntimeError('receiver exited before listening')
                if ':7000 ' in dst.argv(['ss', '-ltn']):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError('receiver did not listen')
            argv = ['python3', '-m', 'pgdelta', 'send', '--source', src.root + '/S',
                    '--source-kind', 'live', '--pg-conn', f'host={src.root} user={gcp.login()} dbname=postgres',
                    '--pg-controldata', PG + 'pg_controldata', '--psql', PG + 'psql',
                    '--connect', dst.ip + ':7000', '--jobs', str(cfg['jobs']), '--hash', 'blake2b',
                    '--hash-ahead', cfg['hash_ahead'], '--read-ahead', '8',
                    '--checkpoint', cfg['checkpoint'], '--live-timeout', str(timeout),
                    '--stats-json', src.root + '/evidence/' + ident + '-send-stats.json']
            phases['capture'] = src.command(ident + '-sender', argv, timeout)
            _, err = receiver.communicate(timeout=120)
            if receiver.returncode:
                raise RuntimeError('receiver failed: ' + err.decode(errors='replace')[-300:])
        finally:
            if receiver.poll() is None:
                dst.cancel(ident + '-receiver')
                try:
                    receiver.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    receiver.kill()
        phases['engine_stats'] = src.get_json(src.root + '/evidence/' + ident + '-send-stats.json')
        phases['receiver_stats'] = dst.get_json(dst.root + '/evidence/' + ident + '-receive-stats.json')
    return phases


def one_trial(nodes, cfg, lane, treatment, ident, out, logical_bytes):
    src, dst, driver = nodes
    trial = out / ident
    trial.mkdir()
    prepare_start = time.monotonic()
    reset_source(src, cfg, ident)
    if treatment == 'pgdelta-inplace':
        dst.argv(['cp', '-a', '--reflink=never', dst.root + '/B', dst.root + '/O'])
    if cfg['cache'] == 'cold':
        dst.shell('sync && echo 3 | sudo -n tee /proc/sys/vm/drop_caches >/dev/null')
    preparation = time.monotonic() - prepare_start
    table = 'pgdm_' + cfg['name'].replace('-', '_')
    monitors = []
    configured = []
    workload_proc = None
    phases = {}
    try:
        for role, node, peer in [('source', src, dst), ('destination', dst, src)]:
            configured.append(node)
            node.shell(node.agent('configure', '--table', table, '--peer', peer.ip,
                                  '--interface', node.interface, '--rate', lane['link']))
        workload_proc, folder = workload(driver, src, cfg, ident)
        for role, node in [('source', src), ('destination', dst)]:
            prefix = node.root + '/evidence/' + ident
            stop = prefix + '.stop'
            proc = node.vm.popen(node.agent('monitor', '--table', table, '--output', prefix + '.host.jsonl',
                '--stop', stop, '--interval', cfg['interval'], '--pgdata', src.root + '/S' if node is src else '/nonexistent'),
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            monitors.append((node, proc, stop))
        for node, proc, _ in monitors:
            prefix = node.root + '/evidence/' + ident + '.host.jsonl'
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError('resource observer exited before backup')
                if node.shell(f'test -s {shlex.quote(prefix)} && echo ready', check=False).strip() == 'ready':
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError('resource observer did not become ready')
        before = stats_snapshot(src)
        start = time.time()
        if treatment == 'control':
            time.sleep(min(60, cfg['duration'] * 60 / 2))
        else:
            phases = backup(src, dst, cfg, treatment, ident)
        end = time.time()
        after = stats_snapshot(src)
        traffic = {role: node.get_json_network(table) for role, node in [('source', src), ('destination', dst)]}
        # Keep resource observers running through application recovery, before restore checks add I/O.
        workload_data = finish_workload(driver, workload_proc, folder, trial / 'workload', cfg['timeout'])
        for node, proc, stop in monitors:
            node.argv(['touch', stop])
            _, err = proc.communicate(timeout=120)
            if proc.returncode:
                raise RuntimeError('resource observer failed: ' + err.decode(errors='replace')[-200:])
        if treatment == 'control':
            duration = None
            verified = None
            restore = None
            output_sizes = None
        else:
            first = phases['capture']
            last = phases.get('verify', first)
            start, end = first['started_epoch'], last['ended_epoch']
            duration = end - start
            if not (workload_data['whole'].start.timestamp() <= start < end <= workload_data['whole'].end.timestamp()):
                raise RuntimeError('backup must fit inside HammerDB measurement window; increase duration')
            output_sizes = dict(apparent_bytes=int(dst.argv(['du', '-sb', dst.root + '/O']).split()[0]),
                                allocated_bytes=int(dst.argv(['du', '-sB1', dst.root + '/O']).split()[0]))
            restore_start = time.monotonic()
            restore = verify_output(dst, dst.root + '/O', cfg, ident)
            verified = duration + time.monotonic() - restore_start
        record = dict(id=ident, treatment=treatment, link=lane['link'], repeat=lane['repeat'],
            warmup=lane['warmup'], preparation_seconds=preparation, backup_verified_seconds=duration,
            backup_plus_restore_check_seconds=verified, phases=phases, postgres_before=before,
            postgres_after=after, network=traffic, restore=restore, start_epoch=start, end_epoch=end,
            logical_database_bytes=logical_bytes, whole_latency=workload_data['latency'],
            workload_aggregate=workload_data['aggregate'], output_sizes=output_sizes,
            status='CONTROL' if treatment == 'control' else 'PASS_RESTORE_SMOKE')
        atomic_json(trial / 'result.json', record)
        return record, workload_data
    except BaseException as exc:
        atomic_json(trial / 'result.json', dict(id=ident, treatment=treatment, link=lane['link'],
            repeat=lane['repeat'], warmup=lane['warmup'], status='FAILED', phases=phases,
            error=f'{type(exc).__name__}: {exc}'))
        raise
    finally:
        for node, proc, stop in monitors:
            node.argv(['touch', stop], check=False)
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        # Stop only the database and container owned by this trial.
        src.argv([PG + 'pg_ctl', '-D', src.root + '/S', '-w', 'stop', '-m', 'fast'], check=False)
        driver.argv(['docker', 'rm', '-f', 'pgd-' + ident], check=False)
        if workload_proc is not None:
            driver.cancel(ident)
            if workload_proc.poll() is None:
                workload_proc.kill()
        for node in configured:
            node.shell(node.agent('cleanup', '--table', table, '--interface', node.interface), check=False)
        remove_pgdata(src, src.root + '/S')
        for path in ('O', 'I'):
            dst.argv([PG + 'pg_ctl', '-D', dst.root + '/' + path, '-w', 'stop', '-m', 'fast'], check=False)
            remove_pgdata(dst, dst.root + '/' + path)


def compare(control, candidate, record):
    window = Window(datetime.fromtimestamp(record['start_epoch'], timezone.utc),
                    datetime.fromtimestamp(record['end_epoch'], timezone.utc))
    reference = matched_window(control['whole'], candidate['whole'], window)
    if reference.start < control['whole'].start or reference.end > control['whole'].end:
        raise ValueError('matched control interval exceeds its measurement window')
    a, b = window_stats(control['series'], reference), window_stats(candidate['series'], window)
    loss = None if not a['mean_tpm'] or b['mean_tpm'] is None else 1 - b['mean_tpm'] / a['mean_tpm']
    recovery = Window(window.end, candidate['whole'].end)
    return dict(control=a, backup=b, throughput_loss=loss,
                recovery=window_stats(candidate['series'], recovery), latency_scope='whole_run')


def summarize(records):
    groups = {}
    for row in records:
        if row.get('status') != 'PASS_RESTORE_SMOKE' or row['warmup']:
            continue
        net = row['network']
        forward = net['source']['backup']['bytes']
        reverse = net['destination']['backup']['bytes']
        groups.setdefault((row['link'], row['treatment']), []).append(
            dict(seconds=row['backup_verified_seconds'], source_bytes=forward,
                 reverse_bytes=reverse, total_bytes=forward + reverse,
                 source_Mbps=forward * 8 / row['backup_verified_seconds'] / 1e6,
                 refresh_MBps=row['logical_database_bytes'] / row['backup_verified_seconds'] / 1e6))
    output = []
    for (link, method), rows in groups.items():
        summary = dict(link=link, method=method, runs=len(rows))
        for key in rows[0]:
            values = [r[key] for r in rows]
            summary[key] = dict(median=statistics.median(values), min=min(values), max=max(values))
        output.append(summary)
    for row in output:
        baseline = next((b for b in output if b['link'] == row['link'] and b['method'] == 'pg_basebackup'), None)
        if baseline:
            row['time_speedup'] = baseline['seconds']['median'] / row['seconds']['median']
            row['forward_saving'] = 1 - row['source_bytes']['median'] / baseline['source_bytes']['median']
    return output


def report(record, output):
    lines = ['# Live HammerDB network benchmark', '',
             'Status: ' + record['status'], '',
             'IP-layer backup-flow bytes include headers/retransmissions; exclude driver/SSH traffic. '
             'Warmups and failed trials are excluded from medians. Latency is whole-run. '
             'Restore checks are package startup/schema/pg_amcheck, not an exact-cutoff logical oracle.', '',
             '| Link | Method | n | Verified backup s | Source MB | Reverse MB | Total MB | Mean outbound Mbit/s | Refresh MB/s | Speedup | Forward savings |',
             '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for row in summarize(record['trials']):
        lines.append(f"| {row['link']} | {row['method']} | {row['runs']} | {row['seconds']['median']:.3f} | "
            f"{row['source_bytes']['median']/1e6:.3f} | {row['reverse_bytes']['median']/1e6:.3f} | "
            f"{row['total_bytes']['median']/1e6:.3f} | {row['source_Mbps']['median']:.2f} | "
            f"{row['refresh_MBps']['median']:.2f} | {row.get('time_speedup', 0):.2f} | "
            f"{100*row.get('forward_saving', 0):.1f}% |")
    lines += ['', 'Per-trial phase timings, restore-check time, preparation, workload windows, resources, '
              'PostgreSQL snapshots, raw logs and time series are retained alongside results.json. '
              'Small samples are descriptive; no statistical significance claim is made.']
    (output / 'report.md').write_text('\n'.join(lines) + '\n')


def run(cfg, output):
    record = dict(config=cfg, code_sha256=fingerprint(), status='RUNNING', trials=[])
    atomic_json(output / 'results.json', record)
    nodes = []
    try:
        root = '/data/pgd-mega-' + cfg['name']
        nodes = [Node(cfg[role], zone, root) for role, zone in
                 zip(('source', 'destination', 'driver'), cfg['zones'])]
        facts = setup(nodes, cfg, output)
        record['environment'] = facts
        src, dst, driver = nodes
        # Size excluding historical WAL is useful for effective refresh throughput.
        pg_start(src, root + '/target')
        logical_bytes = int(src.psql("SELECT sum(pg_database_size(oid)) FROM pg_database").strip())
        pg_stop(src, root + '/target')
        record['environment']['logical_database_bytes'] = logical_bytes
        for index, lane in enumerate(trial_plan(cfg)):
            print(f"pair {index + 1}: {lane['link']} {lane['method']} repeat {lane['repeat']}", flush=True)
            pair = {}
            for treatment in lane['order']:
                ident = f't{index:04d}-' + treatment
                try:
                    row, work = one_trial(nodes, cfg, lane, treatment, ident, output, logical_bytes)
                except BaseException as exc:
                    path = output / ident / 'result.json'
                    failed = json.loads(path.read_text()) if path.exists() else dict(id=ident)
                    record['trials'].append(failed | {'status': 'FAILED', 'error': f'{type(exc).__name__}: {exc}'})
                    raise
                record['trials'].append(row)
                pair[treatment] = row, work
                atomic_json(output / 'results.json', record)
            candidate, work = pair[lane['method']]
            candidate['application'] = compare(pair['control'][1], work, candidate)
            atomic_json(output / 'results.json', record)
        record['status'] = 'PASS_RESTORE_SMOKE'
    except BaseException as exc:
        record.update(status='FAILED', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        # Fetch worker evidence on both success and failure; keep originals if retrieval fails.
        errors = []
        for role, node in zip(('source', 'destination', 'driver'), nodes):
            if not node.owned:
                if node.locked:
                    node.argv(['rmdir', '/data/pgd-mega.lock'], check=False)
                continue
            try:
                if role == 'source':
                    for directory in ('S', 'target'):
                        node.argv([PG + 'pg_ctl', '-D', node.root + '/' + directory,
                                   '-w', 'stop', '-m', 'fast'], check=False)
                if role == 'driver':
                    node.argv(['docker', 'rm', '-f', 'pgd-build-' + cfg['name'], 'pgd-churn'], check=False)
                archive = node.root + '/evidence.tgz'
                node.argv(['tar', '-czf', archive, '-C', node.root, 'evidence'])
                node.vm.scp_from(archive, output / (role + '-evidence.tgz'))
            except Exception as exc:
                errors.append(f'{role}: {exc}')
            finally:
                if node.locked:
                    node.argv(['rmdir', '/data/pgd-mega.lock'], check=False)
        record['collection_errors'] = errors
        if errors and record['status'] == 'PASS_RESTORE_SMOKE':
            record['status'] = 'FAILED'
        record['summary'] = summarize(record['trials'])
        atomic_json(output / 'results.json', record)
        report(record, output)
    return 0 if record['status'] == 'PASS_RESTORE_SMOKE' else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--execute', action='store_true', help='run on the three existing disposable VMs')
    parser.add_argument('--validated-run', type=Path, help='successful small-run results.json, required above 10 warehouses')
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if not args.execute:
            print(json.dumps(dict(config=cfg, code_sha256=fingerprint(), pairs=trial_plan(cfg),
                                  note='Plan only: no network calls or resources created.'), indent=2))
            return 0
        require_validation(cfg, args.validated_run, fingerprint())
        if args.output is None:
            raise ValueError('--execute requires --output (new directory)')
        args.output.mkdir(parents=True, exist_ok=False)
        return run(cfg, args.output)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'error: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
