"""Validate live pgdelta completion evidence and packaged WAL, not a manifest.

This trusts the engine's data verification receipt. It does not independently
re-hash data or test restore. Self-contained so it can run with python -I in
the tools container, without importing the host's modules.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


def lsn_value(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9A-Fa-f]{1,8}/[0-9A-Fa-f]{1,8}', value):
        raise ValueError('Missing or malformed live LSN')
    high, low = value.split('/')
    return int(high, 16) << 32 | int(low, 16)


def validate_evidence(output, stats, receipt):
    if (stats.get('schema_version') != 1 or stats.get('status') != 'complete' or type(stats.get('exit_code')) is not int
            or stats['exit_code'] != 0 or stats.get('source_kind') != 'live'
            or stats.get('in_place') is not True):
        raise ValueError('Live pgdelta did not complete an in-place refresh')
    if (receipt.get('schema_version') != 1 or receipt.get('tool') != 'pgdelta' or receipt.get('status') != 'committed'
            or receipt.get('in_place') is not True or not receipt.get('committed_at')
            or not receipt.get('verified_at')):
        raise ValueError('Missing committed, verified in-place receipt')
    if Path(receipt.get('output', '')).absolute() != Path(output).absolute():
        raise ValueError('Receipt belongs to another output directory')
    digest = stats.get('files_digest')
    receiver = stats.get('receiver') or {}
    if (not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
            or receipt.get('files_digest') != digest or receiver.get('files_digest') != digest
            or receiver.get('in_place') is not True):
        raise ValueError('Sender, receiver and receipt digests do not agree')
    if (type(receipt.get('files')) is not int or receipt['files'] < 1
            or receipt['files'] != receiver.get('files')):
        raise ValueError('Receipt and receiver file counts differ')
    live = stats.get('live') or {}
    if (live.get('read_before_start') or live.get('test_variant')
            or live.get('timed_out') or live.get('cleanup') != 'finished: slot dropped, session closed'):
        raise ValueError('Incomplete live cleanup or unsafe test variant')
    start, stop = live.get('start_lsn'), live.get('stop_lsn')
    if lsn_value(start) >= lsn_value(stop):
        raise ValueError('Invalid live WAL range')
    timeline = live.get('timeline')
    identity = (stats.get('source_identity') or {}).get('system_identifier')
    if (type(timeline) is not int or timeline < 1
            or not isinstance(identity, str) or not identity.isdigit()):
        raise ValueError('Missing live timeline or cluster identity')
    return {'system_identifier': identity, 'timeline': timeline, 'lsn': stop,
            'start_lsn': start, 'source': 'pgdelta.live.stop_lsn'}


def verify(output, stats_path, pg_waldump, pg_controldata):
    output = Path(output)
    if output.is_symlink() or not output.is_dir():
        raise ValueError('Live output must be a real directory')
    for name in ('pgdelta-incomplete.json', 'global/pg_control.pgdelta-incomplete',
                 'postmaster.pid', 'standby.signal', 'recovery.signal'):
        if os.path.lexists(output / name):
            raise ValueError(f'Incomplete or running output: {name}')
    # The fixture supports no tablespaces or external symlinks. A successful
    # receipt must not conceal an output borrowing source/basis files.
    for directory, dirs, files in os.walk(output, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError(f'Non-regular live output entry: {path}')
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise ValueError(f'Hard-linked live output entry: {path}')
    stats = json.loads(Path(stats_path).read_text())
    receipt_path = output.with_name(output.name + '.pgdelta-receipt.json')
    receipt = json.loads(receipt_path.read_text())
    endpoint = validate_evidence(output, stats, receipt)
    label = (output / 'backup_label').read_text()
    match = re.search(r'^START WAL LOCATION: ([0-9A-Fa-f/]+) \(file ([0-9A-F]{24})\)', label, re.M)
    if (not match or lsn_value(match[1]) != lsn_value(endpoint['start_lsn'])
            or int(match[2][:8], 16) != endpoint['timeline']):
        raise ValueError('backup_label differs from the live WAL boundary')
    env = dict(os.environ, LC_ALL='C', LANG='C')
    control = subprocess.run([pg_controldata, '-D', str(output)],
                             capture_output=True, text=True, env=env, timeout=60)
    if control.returncode or 'crc checksum does not match' in control.stderr.lower():
        raise ValueError('Output pg_control could not be read reliably')
    identity = re.search(r'^Database system identifier:\s*(\d+)\s*$', control.stdout, re.M)
    if not identity or identity[1] != endpoint['system_identifier']:
        raise ValueError('Output pg_control belongs to another cluster')
    wal = subprocess.run([pg_waldump, '--quiet', '--path', str(output / 'pg_wal'),
                          '--timeline', str(endpoint['timeline']),
                          '--start', endpoint['start_lsn'], '--end', endpoint['lsn']],
                         capture_output=True, text=True, env=env, timeout=120)
    if wal.returncode:
        raise ValueError('Required packaged WAL failed pg_waldump: ' + wal.stderr.strip())
    return {'status': 'pass', 'kind': 'pgdelta_receipt_and_wal',
            'recovery_endpoint': endpoint, 'receipt': str(receipt_path),
            'receipt_record': receipt,
            'wal_parse': 'pass', 'data_verification': 'pgdelta engine receipt; no independent re-hash',
            'fast_path': stats.get('fast_path'), 'restore_tested': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--stats', required=True)
    parser.add_argument('--pg-waldump', required=True)
    parser.add_argument('--pg-controldata', required=True)
    args = parser.parse_args(argv)
    try:
        report = verify(args.output, args.stats, args.pg_waldump, args.pg_controldata)
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        print(json.dumps({'status': 'fail', 'error': str(exc)}))
        return 4
    print(json.dumps(report, allow_nan=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
