"""Cloud runs: two Google Cloud VMs (source and destination), created for the run and always deleted after.

Each VM runs Debian 13 (PostgreSQL 17 from its own packages) with a separate data disk at /data, as in the
first phase's experiments. The source builds the database, the destination takes the old copy with
pg_basebackup, then the source changes 1% of the rows. Each lane runs with both VMs' caches dropped,
the link capped if asked (infra/gcp/throttle.sh on both), and the 0.1-second sampler
(experiments/network/util_series.py) on both VMs; its link and disk rates become the lane's gauges.

Guardrails: an estimate is shown before anything is created; quota is checked; a run refuses to start
while another benchmark's VMs exist; every VM is labelled purpose=pg-delta-benchmark with an expiry time
(`python -m bench cleanup` deletes expired ones); and the VMs are deleted when the run ends, whatever happens.
"""
from __future__ import annotations

import io
import json
import re
import secrets
import subprocess
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bench import cost
from bench.config import RunConfig
from bench.lanes import copy_events, merge_gauges, pump_pg_basebackup
from pgdelta.events import EventLog
from viz import sampler

REPO = Path(__file__).resolve().parents[1]
IMAGE = ('debian-13', 'debian-cloud')
PGBIN = '/usr/lib/postgresql/17/bin'
BASE_LABELS = 'purpose=pg-delta-benchmark,owner=pg-delta-backup'
FAMILY_QUOTA = {'n2d': 'N2D_CPUS', 'n2': 'N2_CPUS', 'e2': 'E2_CPUS', 'c2d': 'C2D_CPUS', 'c2': 'C2_CPUS',
                'c3': 'C3_CPUS', 'n1': 'CPUS'}
CODE = ('pgdelta', 'experiments/network/util_series.py', 'infra/gcp/throttle.sh')


def gcloud(*args, check=True, timeout=None) -> subprocess.CompletedProcess:
    return subprocess.run(['gcloud', *map(str, args)], capture_output=True, text=True, check=check, timeout=timeout)


def _quotas(region: str) -> dict:
    out = gcloud('compute', 'regions', 'describe', region, '--format=json(quotas)')
    quotas = {q['metric']: (q['usage'], q['limit']) for q in json.loads(out.stdout).get('quotas', [])}
    project = gcloud('compute', 'project-info', 'describe', '--format=json(quotas)', check=False)
    if project.returncode == 0:
        for q in json.loads(project.stdout).get('quotas', []):
            if q['metric'] == 'CPUS_ALL_REGIONS':
                quotas['CPUS_ALL_REGIONS'] = (q['usage'], q['limit'])
    return quotas


def active_benchmarks() -> list[dict]:
    out = gcloud('compute', 'instances', 'list', '--filter=labels.purpose=pg-delta-benchmark',
                 '--format=json(name,zone.basename(),status,labels)', check=False)
    return json.loads(out.stdout or '[]') if out.returncode == 0 else []


def preflight(cfg: RunConfig) -> dict:
    """Everything to know before creating VMs: the estimate, problems that block the run, warnings."""
    report = {'estimate': cost.estimate(cfg), 'problems': [], 'warnings': [], 'quota': {}}
    try:
        account = gcloud('config', 'get-value', 'account').stdout.strip()
        project = gcloud('config', 'get-value', 'project').stdout.strip()
        report.update(account=account, project=project)
        if gcloud('auth', 'print-access-token', check=False).returncode:
            report['problems'].append('gcloud is not signed in: run `gcloud auth login`')
            return report
    except FileNotFoundError:
        report['problems'].append('gcloud is not installed: see https://cloud.google.com/sdk/docs/install')
        return report
    family = cfg.machine.split('-')[0]
    need_cpus = report['estimate']['vcpus']
    ssd = cfg.disk_type in ('pd-balanced', 'pd-ssd')
    need_ssd = 2 * cost.BOOT_GB + (2 * cfg.disk_gb if ssd else 0)
    try:
        quotas = _quotas(cfg.region)
    except (subprocess.CalledProcessError, ValueError) as exc:
        report['problems'].append(f'could not read quotas: {exc}')
        return report
    checks = [('CPUS', need_cpus), (FAMILY_QUOTA.get(family, 'CPUS'), need_cpus), ('CPUS_ALL_REGIONS', need_cpus),
              ('SSD_TOTAL_GB', need_ssd)] + ([] if ssd else [('DISKS_TOTAL_GB', 2 * cfg.disk_gb)])
    for metric, need in checks:
        if metric in quotas:
            used, limit = quotas[metric]
            report['quota'][metric] = {'used': used, 'limit': limit, 'need': need}
            if used + need > limit:
                report['problems'].append(f'quota {metric}: {used:.0f} used of {limit:.0f}, this run needs {need}')
    others = active_benchmarks()
    if others:
        names = ', '.join(f"{o['name']} ({o.get('status')})" for o in others)
        report['problems'].append(f'another benchmark is running (one at a time): {names}. '
                                  f'If it is stale, `python -m bench cleanup --all` deletes benchmark VMs.')
    if not report['estimate']['machine_known']:
        report['warnings'].append(f'no price on file for {cfg.machine}: the dollar estimate assumes $0.30/hour')
    return report


def cleanup(all_benchmarks: bool = False, now: float | None = None) -> list[str]:
    """Delete benchmark VMs whose expiry label has passed (or every benchmark VM with all_benchmarks)."""
    now = now or time.time()
    deleted = []
    for vm in active_benchmarks():
        labels = vm.get('labels', {})
        expires = labels.get('pgd-expires', '')
        if all_benchmarks or (expires.isdigit() and int(expires) < now):
            gcloud('compute', 'instances', 'delete', vm['name'], '--zone', vm['zone'], '--quiet', check=False)
            deleted.append(vm['name'])
    return deleted


class VM:
    def __init__(self, name: str, zone: str):
        self.name, self.zone, self.ip = name, zone, None

    def ssh_argv(self, command: str) -> list[str]:
        return ['gcloud', 'compute', 'ssh', self.name, '--zone', self.zone, '--quiet',
                '--ssh-flag=-oServerAliveInterval=30', '--ssh-flag=-oStrictHostKeyChecking=no', '--command', command]

    def ssh(self, command: str, check=True, timeout=None) -> str:
        out = subprocess.run(self.ssh_argv(command), capture_output=True, text=True, timeout=timeout)
        if check and out.returncode:
            raise RuntimeError(f'{self.name}: `{command[:80]}` failed: {out.stderr.strip()[-400:]}')
        return out.stdout

    def popen(self, command: str, **kw) -> subprocess.Popen:
        return subprocess.Popen(self.ssh_argv(command), **kw)

    def scp_to(self, local: Path, remote: str) -> None:
        subprocess.run(['gcloud', 'compute', 'scp', '--zone', self.zone, '--quiet', str(local), f'{self.name}:{remote}'],
                       check=True, capture_output=True)

    def scp_from(self, remote: str, local: Path) -> None:
        subprocess.run(['gcloud', 'compute', 'scp', '--zone', self.zone, '--quiet', f'{self.name}:{remote}', str(local)],
                       check=True, capture_output=True)


def _code_tarball(path: Path) -> None:
    with tarfile.open(path, 'w:gz') as tar:
        for item in CODE:
            tar.add(REPO / item, arcname=item, filter=lambda t: None if '__pycache__' in t.name else t)


PROVISION = f"""set -e
sudo DEBIAN_FRONTEND=noninteractive apt-get -qq update > /dev/null
sudo DEBIAN_FRONTEND=noninteractive apt-get -qq install -y postgresql-17 postgresql-client-17 python3 > /dev/null
sudo systemctl stop postgresql > /dev/null 2>&1 || true
sudo systemctl disable postgresql > /dev/null 2>&1 || true
sudo mkfs.ext4 -q -F /dev/disk/by-id/google-data
sudo mkdir -p /data && sudo mount -o noatime /dev/disk/by-id/google-data /data && sudo chown "$USER" /data
mkdir -p ~/pgd && tar -xzf ~/code.tgz -C ~/pgd
{PGBIN}/postgres --version
"""


def run_gcp(cfg: RunConfig, run_dir: Path, log=print, keep: bool = False) -> dict:
    tag = (re.sub(r'[^a-z0-9]', '', cfg.name.lower())[-10:] or 'run') + secrets.token_hex(2)
    expires = int(time.time() + cfg.ttl_minutes * 60)
    labels = f'{BASE_LABELS},pgd-bench={tag},pgd-expires={expires}'
    src, dst = VM(f'pgd-bench-{tag}-src', cfg.zones[0]), VM(f'pgd-bench-{tag}-dst', cfg.zones[1])
    facts = {'vms': {'source': src.name, 'destination': dst.name, 'zones': cfg.zones, 'machine': cfg.machine,
                     'disk': f'{cfg.disk_gb} GB {cfg.disk_type}', 'image': IMAGE[0]}, 'lanes': {}}
    created = []
    lock = threading.Lock()

    def create(vm: VM):
        out = gcloud('compute', 'instances', 'create', vm.name, '--zone', vm.zone, '--machine-type', cfg.machine,
                     '--image-family', IMAGE[0], '--image-project', IMAGE[1], '--boot-disk-size', f'{cost.BOOT_GB}GB',
                     '--boot-disk-type', 'pd-balanced',
                     f'--create-disk=name={vm.name}-data,size={cfg.disk_gb},type={cfg.disk_type},device-name=data,auto-delete=yes',
                     '--labels', labels, '--format=json')
        with lock:
            created.append(vm)
        vm.ip = json.loads(out.stdout)[0]['networkInterfaces'][0]['networkIP']

    try:
        log(f'creating {src.name} ({src.zone}) and {dst.name} ({dst.zone}), {cfg.machine} …')
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(create, (src, dst)))
        for vm in (src, dst):
            for _ in range(40):
                if subprocess.run(vm.ssh_argv('true'), capture_output=True).returncode == 0:
                    break
                time.sleep(5)
            else:
                raise RuntimeError(f'{vm.name}: ssh never became ready')
        log('installing PostgreSQL 17 and the code on both VMs …')
        tarball = run_dir / 'code.tgz'
        _code_tarball(tarball)
        with ThreadPoolExecutor(2) as pool:
            versions = list(pool.map(lambda vm: (vm.scp_to(tarball, 'code.tgz'), vm.ssh(PROVISION))[1], (src, dst)))
        tarball.unlink()
        found = re.search(r'\d+\.\d+', versions[0])
        facts['postgres'] = found.group(0) if found else 'unknown'
        facts['rtt_ms'] = _rtt(src, dst)

        rows = cfg.scale * 100_000
        where = 'aid % 100 = 0' if cfg.change == 'uniform' else f'aid <= {rows // 100}'
        psql = f'{PGBIN}/psql -h /tmp -U postgres -qAt postgres'
        log(f'building the database (scale {cfg.scale}) and the old copy …')
        src.ssh(f"""set -e
{PGBIN}/initdb -D /data/S -U postgres --data-checksums -A trust -E UTF8 --locale=C.UTF-8 > /dev/null
echo "listen_addresses = '*'" >> /data/S/postgresql.conf
printf 'host all all {dst.ip}/32 trust\\nhost replication all {dst.ip}/32 trust\\n' >> /data/S/pg_hba.conf
{PGBIN}/pg_ctl -D /data/S -l /data/S.log -w start -o '-p 5432 -c unix_socket_directories=/tmp' > /dev/null
{PGBIN}/pgbench -i -s {cfg.scale} -h /tmp -U postgres postgres > /dev/null 2>&1""")
        dst.ssh(f'{PGBIN}/pg_basebackup -h {src.ip} -U postgres -D /data/B -X stream -c fast')
        src.ssh(f'{psql} -c "UPDATE pgbench_accounts SET abalance = abalance + 7 WHERE {where}" -c CHECKPOINT')
        expect = src.ssh(f'{psql} -c "SELECT count(*), sum(abalance) FROM pgbench_accounts"').strip()
        facts['database_bytes'] = int(src.ssh('du -sb /data/S | cut -f1').split()[0])

        running = True
        raw = run_dir / 'raw'
        for lane in cfg.lanes():
            lane_id, path = lane['id'], run_dir / f"events-{lane['id']}.jsonl"
            if lane['method'] == 'pg_basebackup' and not running:
                src.ssh(f"{PGBIN}/pg_ctl -D /data/S -l /data/S.log -w start -o '-p 5432 -c unix_socket_directories=/tmp' > /dev/null")
                running = True
            if lane['method'] == 'pgdelta' and running:
                src.ssh(f'{PGBIN}/pg_ctl -D /data/S -m fast -w stop > /dev/null')
                running = False
            log(f'lane {lane_id} …')
            for vm in (src, dst):
                vm.ssh(f'sync; echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null; '
                       f'nohup python3 ~/pgd/experiments/network/util_series.py > /data/util.txt 2> /dev/null '
                       f'< /dev/null & echo $! > /data/util.pid')
                if cfg.link != 'full':
                    vm.ssh(f'sudo bash ~/pgd/infra/gcp/throttle.sh {cfg.link} > /dev/null')
            time.sleep(0.5)
            if lane['method'] == 'pg_basebackup':
                times, result = _lane_pg_basebackup(src, dst, path, lane_id)
            else:
                times, result = _lane_pgdelta(src, dst, path, lane_id, cfg.chunk_size, run_dir)
            lane_raw = raw / lane_id
            lane_raw.mkdir(parents=True, exist_ok=True)
            for vm, side in ((src, 'source'), (dst, 'dest')):
                if cfg.link != 'full':
                    vm.ssh('sudo bash ~/pgd/infra/gcp/throttle.sh off > /dev/null', check=False)
                vm.ssh('kill $(cat /data/util.pid); sleep 0.4', check=False)
                vm.scp_from('/data/util.txt', lane_raw / f'util-{side}.txt')
            (lane_raw / 'meta.json').write_text(json.dumps({**times, 'status': 0 if result.get('ok') else 1,
                                                            'arm': lane_id}))
            gauges = [e | {'method': lane_id} for e in sampler.lane_events(lane_raw) if e['kind'] == 'gauges']
            merge_gauges(path, gauges)
            facts['lanes'][lane_id] = result
        if running:
            src.ssh(f'{PGBIN}/pg_ctl -D /data/S -m fast -w stop > /dev/null', check=False)
        got = None
        if 'pgdelta' in cfg.methods:
            got = dst.ssh(f"""set -e
{PGBIN}/pg_ctl -D /data/O -l /data/O.log -w start -o '-p 5433 -c listen_addresses= -c unix_socket_directories=/tmp' > /dev/null
{PGBIN}/psql -h /tmp -p 5433 -U postgres -qAt postgres -c "SELECT count(*), sum(abalance) FROM pgbench_accounts"
{PGBIN}/pg_ctl -D /data/O -m fast -w stop > /dev/null""").strip()
        facts['verify'] = {'source': expect, 'copy': got, 'checked': got is not None,
                           'match': got is None or (bool(expect) and expect == got)}
        log(f"copy check: source {expect}, refreshed copy {got}: {'MATCH' if facts['verify']['match'] else 'MISMATCH'}")
        return facts
    finally:
        if created and not keep:
            log('deleting the VMs …')
            with ThreadPoolExecutor(2) as pool:
                list(pool.map(lambda vm: gcloud('compute', 'instances', 'delete', vm.name, '--zone', vm.zone,
                                                '--quiet', check=False), created))
            facts['vms']['deleted'] = True


def _rtt(src: VM, dst: VM) -> float | None:
    out = src.ssh(f'ping -c 5 -q {dst.ip} | tail -1', check=False)
    found = re.search(r'= [\d.]+/([\d.]+)/', out)
    return float(found.group(1)) if found else None


def _times(stderr_text: str) -> dict:
    found = dict(re.findall(r'^(T[01]) ([\d.]+)$', stderr_text, re.M))
    return {'t0': float(found['T0']), 't1': float(found['T1'])} if 'T0' in found and 'T1' in found else {}


def _lane_pg_basebackup(src: VM, dst: VM, path: Path, lane_id: str):
    events = EventLog(path, method=lane_id)
    events.emit('start')
    command = (f'rm -rf /data/full; echo T0 $(date +%s.%N); '
               f'{PGBIN}/pg_basebackup -h {src.ip} -U postgres -D /data/full -X stream -c fast -P; rc=$?; '
               f'echo T1 $(date +%s.%N); exit $rc')
    proc = dst.popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out_text = io.StringIO()
    reader = threading.Thread(target=lambda: out_text.write(proc.stdout.read().decode()), daemon=True)
    reader.start()
    total = pump_pg_basebackup(proc.stderr, events)
    rc = proc.wait()
    reader.join()
    elapsed = round(time.perf_counter() - events.started, 4)
    events.emit('done', ok=rc == 0, elapsed=elapsed, bytes_sent=total, total_bytes=total)
    events.close()
    if rc:
        raise RuntimeError('pg_basebackup failed on the destination')
    return _times(out_text.getvalue()), {'elapsed': elapsed, 'bytes_sent': total, 'total_bytes': total, 'ok': True}


def _lane_pgdelta(src: VM, dst: VM, path: Path, lane_id: str, chunk_size: int, run_dir: Path):
    receiver = dst.popen('rm -rf /data/O; cd ~/pgd && python3 -m pgdelta receive --basis /data/B --output /data/O '
                         '--listen 0.0.0.0:7000 --stats-json /data/recv.json',
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(60):
        if ':7000 ' in dst.ssh("ss -ltn | grep ':7000 ' || true"):
            break
        time.sleep(0.5)
    else:
        receiver.kill()
        raise RuntimeError('the pgdelta receiver never started listening')
    command = (f'cd ~/pgd && echo T0 $(date +%s.%N) >&2; python3 -m pgdelta send --source /data/S '
               f'--connect {dst.ip}:7000 --chunk-size {chunk_size} --events - --stats-json /data/send.json; rc=$?; '
               f'echo T1 $(date +%s.%N) >&2; exit $rc')
    sender = src.popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err = io.StringIO()
    reader = threading.Thread(target=lambda: err.write(sender.stderr.read().decode()), daemon=True)
    reader.start()
    copy_events(sender.stdout, path, lane_id)
    rc = sender.wait()
    reader.join()
    receiver.wait(timeout=120)
    if rc:
        raise RuntimeError(f'pgdelta send failed: {err.getvalue().strip()[-300:]}')
    stats = run_dir / f'stats-{lane_id}.json'
    src.scp_from('/data/send.json', stats)
    report = json.loads(stats.read_text())
    c = report.get('counts', {})
    result = {'elapsed': report['elapsed_seconds'], 'bytes_sent': report['bytes_sent_total'],
              'total_bytes': c.get('bytes_read', 0), 'round_trips': c.get('round_trips', 0),
              'chunks_changed': c.get('chunks_changed', 0), 'chunks_same': c.get('chunks_same', 0),
              'chunks_new': c.get('chunks_new', 0), 'ok': report.get('ok', False)}
    return _times(err.getvalue()), result
