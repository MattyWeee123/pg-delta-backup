"""Linux worker for bench.mega; commands are argv, never interpolated shell text.

Network counters count outgoing IPv4 backup flows at the IP layer, including
headers/retransmissions. The controller never adds RX to TX (double counting).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from benchmarks.resources import ResourceSampler


def call(argv, **kwargs):
    return subprocess.run(argv, check=True, capture_output=True, text=True, **kwargs).stdout


def network(table):
    data = json.loads(call(['sudo', '-n', 'nft', '-j', 'list', 'table', 'ip', table]))
    return {item['counter']['name']: {k: item['counter'][k] for k in ('bytes', 'packets')}
            for item in data['nftables'] if 'counter' in item}


def configure(table, peer, interface, rate):
    # Exclusive disposable hosts only. A unique table avoids touching host firewall rules.
    rules = f'''add table ip {table}
add counter ip {table} backup
add chain ip {table} output {{ type filter hook output priority 0; policy accept; }}
add rule ip {table} output ip daddr {peer} tcp sport {{ 5432, 7000 }} counter name backup return
add rule ip {table} output ip daddr {peer} tcp dport {{ 5432, 7000 }} counter name backup return
'''
    call(['sudo', '-n', 'nft', '-f', '-'], input=rules)
    # Shape only traffic to the other backup host, leaving the driver/control traffic alone.
    # Both directions are configured; no additional RTT is injected.
    if rate != 'full':
        call(['sudo', '-n', 'tc', 'qdisc', 'replace', 'dev', interface, 'root', 'handle', '7:', 'htb',
              'default', '20'])
        for ident, limit in [('10', rate), ('20', '100gbit')]:
            call(['sudo', '-n', 'tc', 'class', 'add', 'dev', interface, 'parent', '7:',
                  'classid', '7:' + ident, 'htb', 'rate', limit, 'ceil', limit])
        call(['sudo', '-n', 'tc', 'filter', 'add', 'dev', interface, 'parent', '7:',
              'protocol', 'ip', 'prio', '1', 'u32', 'match', 'ip', 'dst', peer + '/32',
              'flowid', '7:10'])


def host_sample(table):
    def read(path):
        try:
            return Path(path).read_text()
        except OSError:
            return None
    return {'epoch': time.time(), 'network': network(table),
            'interfaces': read('/proc/net/dev'),
            'cpu': read('/proc/stat'), 'memory': read('/proc/meminfo'),
            'disk': read('/proc/diskstats'), 'vm': read('/proc/vmstat'),
            'pressure': {kind: read('/proc/pressure/' + kind) for kind in ('cpu', 'io', 'memory')},
            'tcp': read('/proc/net/snmp'), 'tcp_extended': read('/proc/net/netstat')}


def monitor(table, output, stop, interval, pgdata=None):
    root = {'worker': os.getpid()}
    if pgdata and (Path(pgdata) / 'postmaster.pid').exists():
        root['postgres'] = int((Path(pgdata) / 'postmaster.pid').read_text().splitlines()[0])
    resources = ResourceSampler(root, output.with_suffix('.process.jsonl'), interval=interval)
    resources.start()
    count = 0
    previous = None
    try:
        with output.open('x') as stream:
            while True:
                row = host_sample(table)
                row['backup_Mbps'] = None
                if previous is not None:
                    seconds = row['epoch'] - previous['epoch']
                    delta = row['network']['backup']['bytes'] - previous['network']['backup']['bytes']
                    if seconds > 0 and delta >= 0:
                        row['backup_Mbps'] = delta * 8 / seconds / 1e6
                previous = row
                if count % max(1, round(1 / interval)) == 0:
                    row['tcp_sockets'] = call(['ss', '-tin'])
                count += 1
                stream.write(json.dumps(row) + '\n')
                stream.flush()
                if stop.exists():
                    break
                time.sleep(interval)
    finally:
        output.with_suffix('.resources.json').write_text(json.dumps(resources.stop(), indent=2))


def execute(spec):
    """Measure on the worker, excluding SSH startup; include final process exit/fsync."""
    config = json.loads(Path(spec).read_text())
    prefix = Path(config['prefix'])
    record = {'argv': config['argv'], 'started_epoch': time.time()}
    timer = time.monotonic()
    sampler = ResourceSampler({'command': os.getpid()}, prefix.with_suffix('.process.jsonl'),
                              interval=config.get('interval', 0.1))
    sampler.start()
    try:
        with prefix.with_suffix('.stdout').open('wb') as out, prefix.with_suffix('.stderr').open('wb') as err:
            proc = subprocess.Popen(config['argv'], stdout=out, stderr=err, start_new_session=True)
            try:
                start = process_start(proc.pid)
            except FileNotFoundError:
                start = None  # A short-lived command may already have exited.
            if start is not None:
                prefix.with_suffix('.pid.json').write_text(json.dumps({'pid': proc.pid, 'start': start}))
            try:
                record['returncode'] = proc.wait(timeout=config['timeout'])
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                record.update(returncode=124, error='command timeout')
    finally:
        record.update(ended_epoch=time.time(), seconds=time.monotonic() - timer)
        record['resources'] = sampler.stop()
        prefix.with_suffix('.json').write_text(json.dumps(record, indent=2))
    return record.get('returncode', 1)


def process_start(pid):
    text = Path(f'/proc/{pid}/stat').read_text()
    return text[text.rfind(')') + 2:].split()[19]


def cancel(prefix):
    path = Path(prefix).with_suffix('.pid.json')
    if not path.exists():
        return
    info = json.loads(path.read_text())
    try:
        if process_start(info['pid']) == info['start']:
            (os.killpg if info.get('group', True) else os.kill)(info['pid'], signal.SIGTERM)
    except ProcessLookupError:
        pass
    except FileNotFoundError:
        pass


def workload(spec):
    from benchmarks.hammerdb_experiment import (start_workload, wait_for_marker, wait_for_exit,
                                               collect_workload, RAMPUP_COMPLETE)
    cfg = json.loads(Path(spec).read_text())
    directory = Path(cfg.pop('directory'))
    directory.mkdir()
    directory.with_suffix('.pid.json').write_text(json.dumps(
        {'pid': os.getpid(), 'start': process_start(os.getpid()), 'group': False}))
    timeout = cfg.pop('timeout')
    name = cfg['name']
    try:
        start_workload(**cfg)
        wait_for_marker(name, RAMPUP_COMPLETE, timeout=timeout)
        (directory / 'ready').write_text(str(time.time()))
        wait_for_exit(name, timeout=timeout)
        # Nonzero workload exit must not become a successful low-throughput run.
        code = call(['docker', 'inspect', '-f', '{{.State.ExitCode}}', name]).strip()
        if code != '0':
            raise RuntimeError('HammerDB exited with ' + code)
        collect_workload(name, directory)
        (directory / 'complete').touch()
    finally:
        # Preserve logs/database even on failure, before removing the owned container.
        if not (directory / 'complete').exists():
            with (directory / 'failed.log').open('w') as log:
                subprocess.run(['docker', 'logs', '-t', name], stdout=log, stderr=subprocess.STDOUT)
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('execute', 'cancel', 'workload', 'monitor', 'configure', 'cleanup', 'network'))
    parser.add_argument('--spec')
    parser.add_argument('--table')
    parser.add_argument('--peer')
    parser.add_argument('--interface')
    parser.add_argument('--rate', default='full')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--stop', type=Path)
    parser.add_argument('--pgdata')
    parser.add_argument('--interval', type=float, default=0.1)
    args = parser.parse_args()
    if args.action == 'execute':
        return execute(args.spec)
    if args.action == 'cancel':
        cancel(args.spec)
    if args.action == 'workload':
        workload(args.spec)
    if args.action == 'monitor':
        monitor(args.table, args.output, args.stop, args.interval, args.pgdata)
    elif args.action == 'configure':
        configure(args.table, args.peer, args.interface, args.rate)
    elif args.action == 'network':
        print(json.dumps(network(args.table)))
    elif args.action == 'cleanup':
        call(['sudo', '-n', 'nft', 'delete', 'table', 'ip', args.table])
        # Only delete the qdisc this worker installed, never an arbitrary root qdisc.
        current = call(['tc', 'qdisc', 'show', 'dev', args.interface])
        if 'htb 7: root' in current:
            call(['sudo', '-n', 'tc', 'qdisc', 'del', 'dev', args.interface, 'root', 'handle', '7:'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
