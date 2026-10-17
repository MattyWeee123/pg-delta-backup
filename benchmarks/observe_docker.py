"""Record local Compose source and runner resource samples as JSON lines.

Run on the Docker host while the workload and backup execute. This observer is
independent of the backup CLI and does not change the backup timing boundary.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import time


UNITS = {'B': 1, 'kB': 1000, 'MB': 1000 ** 2, 'GB': 1000 ** 3,
         'TB': 1000 ** 4, 'KiB': 1024, 'MiB': 1024 ** 2, 'GiB': 1024 ** 3,
         'TiB': 1024 ** 4}


def bytes_value(value):
    match = re.fullmatch(r'\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*', value)
    if not match or match.group(2) not in UNITS:
        raise ValueError(f'Unsupported Docker size: {value!r}')
    return round(float(match.group(1)) * UNITS[match.group(2)])


def byte_pair(value):
    parts = value.split('/')
    if len(parts) != 2:
        raise ValueError(f'Expected Docker size pair: {value!r}')
    return [bytes_value(part) for part in parts]


def parse_stats(raw):
    memory = byte_pair(raw['MemUsage'])
    block = byte_pair(raw['BlockIO'])
    return {'container_id': raw['ID'], 'container_name': raw['Name'],
            'cpu_percent': float(raw['CPUPerc'].rstrip('%')),
            'memory_usage_bytes': memory[0], 'memory_limit_bytes': memory[1],
            'block_read_bytes': block[0], 'block_write_bytes': block[1],
            'pids': int(raw['PIDs']) if raw.get('PIDs', '').isdigit() else None,
            'raw': raw}


def docker(*args):
    done = subprocess.run(['docker', *args], capture_output=True, text=True,
                          timeout=15, check=False)
    if done.returncode:
        raise RuntimeError(done.stderr.strip() or f'docker {args[0]} failed')
    return done.stdout


def sample(project):
    services = {}
    for service in ('postgres', 'runner'):
        ids = docker('ps', '-q', '--filter', f'label=com.docker.compose.project={project}',
                     '--filter', f'label=com.docker.compose.service={service}').split()
        for container_id in ids:
            try:
                output = docker('stats', '--no-stream', '--format', '{{json .}}', container_id)
            except RuntimeError:
                # The temporary Compose runner can exit after `docker ps`.
                if service == 'runner':
                    continue
                raise
            lines = output.splitlines()
            if lines:
                services.setdefault(service, []).append(parse_stats(json.loads(lines[0])))
    return {'epoch_seconds': time.time(), 'services': services}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', default='fixture', help='Compose project name')
    parser.add_argument('--output', type=Path, required=True, help='New JSONL evidence file')
    parser.add_argument('--duration', type=float, default=240, help='Observation seconds')
    parser.add_argument('--interval', type=float, default=1, help='Minimum seconds between samples')
    args = parser.parse_args(argv)
    if args.duration <= 0 or args.interval <= 0 or args.output.exists():
        parser.error('duration and interval must be positive; output must be new')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.duration
    try:
        with args.output.open('x', encoding='utf-8') as stream:
            while time.monotonic() < deadline:
                started = time.monotonic()
                stream.write(json.dumps(sample(args.project), allow_nan=False) + '\n')
                stream.flush()
                time.sleep(max(0, args.interval - (time.monotonic() - started)))
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(1, f'Observer stopped: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
