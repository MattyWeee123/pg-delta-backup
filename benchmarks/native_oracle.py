"""Recovery boundaries for the native runner's single-timeline PG17 fixture."""
from __future__ import annotations

import json
from pathlib import Path
import re


def lsn_value(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9A-Fa-f]{1,8}/[0-9A-Fa-f]{1,8}', value):
        raise ValueError(f'Invalid WAL LSN: {value!r}')
    high, low = value.split('/')
    return (int(high, 16) << 32) | int(low, 16)


def backup_endpoint(backup: Path, system_identifier: str):
    manifest = json.loads((backup / 'backup_manifest').read_text())
    if str(manifest.get('System-Identifier')) != str(system_identifier):
        raise ValueError('Backup manifest system identifier differs from source')
    ranges = manifest.get('WAL-Ranges')
    if not isinstance(ranges, list) or len(ranges) != 1:
        raise ValueError('Exact-cutoff oracle requires one WAL timeline')
    wal = ranges[0]
    if (type(wal.get('Timeline')) is not int or wal['Timeline'] < 1
            or lsn_value(wal['Start-LSN']) >= lsn_value(wal['End-LSN'])):
        raise ValueError('Invalid backup WAL range')
    return {'system_identifier': str(system_identifier), 'timeline': wal['Timeline'],
            'lsn': wal['End-LSN'], 'source': 'backup_manifest.WAL-Ranges.End-LSN'}


def cutoff_options(endpoint, *, reference=False):
    # End-LSN is an end-of-record pointer. On the reference, stop BEFORE the
    # first record starting at/after it; inclusive recovery could add a commit.
    lsn_value(endpoint['lsn'])
    timeline = endpoint['timeline']
    if type(timeline) is not int or timeline < 1:
        raise ValueError('Invalid recovery timeline')
    options = ['-c', f'recovery_target_timeline={timeline}',
               '-c', 'recovery_target_action=pause', '-c', 'hot_standby=on']
    if reference:
        options += ['-c', f"recovery_target_lsn={endpoint['lsn']}",
                    '-c', 'recovery_target_inclusive=off']
    else:
        options += ['-c', 'recovery_target=immediate']
    return options


def check_replay_endpoint(endpoint, replay_lsn):
    if lsn_value(replay_lsn) != lsn_value(endpoint['lsn']):
        raise RuntimeError(f"Recovery endpoint differs: expected {endpoint['lsn']}, got {replay_lsn}")


def successful_operations(paths):
    """PG17 non-aggregated logs: client id and one-based script number.

    This fixture has one committed transfer per script, seq starts at 1,
    max-tries=1, no sampling, no failed/skipped transactions.
    """
    operations = set()
    for path in paths:
        for line in path.read_text().splitlines():
            fields = line.split()
            if len(fields) < 6 or not all(fields[i].isdigit() for i in (0, 1, 2, 3, 4, 5)):
                raise ValueError('Missing or unsuccessful transaction observation')
            operation = (int(fields[0]), int(fields[1]))
            if operation in operations:
                raise ValueError('Duplicate transaction observation')
            operations.add(operation)
    return operations


def check_operation_prefix(operations, acknowledged, *, complete=False):
    """Sequential clients must each recover a prefix, never a gapped stream."""
    if not operations <= acknowledged or (complete and operations != acknowledged):
        raise RuntimeError('Source/recovered operations differ from external acknowledgements')
    workers = {worker for worker, _ in operations}
    for worker in workers:
        sequence = sorted(seq for owner, seq in operations if owner == worker)
        if sequence != list(range(1, len(sequence) + 1)):
            raise RuntimeError('Recovered worker operation stream has a gap')
