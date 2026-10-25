"""A stand-in for psql that answers the statements pgdelta.live sends, as a running server would.

Run as `stub_psql.py [psql options] -d CONNINFO`; its configuration is the JSON file named by STUB_PSQL_CONFIG:

- data_directory, system_identifier, wal_segment_size, version, in_recovery: what the server reports;
- slot_lsn, start_lsn, checkpoint_lsn, stop_lsn (X/Y), timeline, spcmapfile: its backup's answers;
- log: a file to which every statement is appended as one JSON line ({"sql": ...});
- fail_on: a substring; the first statement holding it fails (ERROR on stderr, exit 3, as psql under
  ON_ERROR_STOP);
- on_start: {relative path: hex bytes} written into the data directory when pg_backup_start is answered, so a
  test can tell what was read before it from what was read after;
- start_delay: seconds pg_backup_start takes (as its checkpoint would) before those bytes are written and it
  answers;
- refuse_connect: exit at once with a connection error.

Statements end with ';' at the end of a line; `\\echo X` prints X; `\\q` or the end of input ends it.
"""
import json
import os
from pathlib import Path
import sys
import time

CONFIG = json.loads(Path(os.environ['STUB_PSQL_CONFIG']).read_text())


def log(sql):
    with open(CONFIG['log'], 'a') as stream:
        stream.write(json.dumps({'sql': sql}) + '\n')


def out(value):
    sys.stdout.write(json.dumps(value) + '\n')


def label():
    segment = CONFIG['wal_segment_size']
    hi, lo = (int(part, 16) for part in CONFIG['start_lsn'].split('/'))
    segno = ((hi << 32) | lo) // segment
    per_id = 0x100000000 // segment
    name = f'{CONFIG.get("timeline", 1):08X}{segno // per_id:08X}{segno % per_id:08X}'
    return (f'START WAL LOCATION: {CONFIG["start_lsn"]} (file {name})\n'
            f'CHECKPOINT LOCATION: {CONFIG["checkpoint_lsn"]}\n'
            'BACKUP METHOD: streamed\nBACKUP FROM: primary\nSTART TIME: 2026-10-25 00:00:00 UTC\n'
            f'LABEL: stub\nSTART TIMELINE: {CONFIG.get("timeline", 1)}\n')


def answer(sql):
    log(sql)
    if CONFIG.get('fail_on') and CONFIG['fail_on'] in sql:
        sys.stderr.write('psql:<stdin>:1: ERROR:  injected failure\n')
        sys.stderr.flush()
        sys.exit(3)
    if sql.startswith('SET '):
        return
    if 'pg_is_in_recovery' in sql:
        out({'version': CONFIG.get('version', 170011), 'in_recovery': CONFIG.get('in_recovery', False),
             'data_directory': CONFIG['data_directory'], 'system_identifier': CONFIG['system_identifier'],
             'wal_segment_size': CONFIG['wal_segment_size']})
    elif 'pg_create_physical_replication_slot' in sql:
        out({'lsn': CONFIG['slot_lsn']})
    elif 'pg_backup_start' in sql:
        time.sleep(CONFIG.get('start_delay', 0))
        for path, data in CONFIG.get('on_start', {}).items():
            target = Path(CONFIG['data_directory']) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, 'r+b' if target.exists() else 'wb') as stream:
                stream.write(bytes.fromhex(data))
        out({'lsn': CONFIG['start_lsn']})
    elif 'FROM pg_backup_stop' in sql:
        out({'lsn': CONFIG['stop_lsn'], 'labelfile': label(), 'spcmapfile': CONFIG.get('spcmapfile', '')})
    elif 'pg_backup_stop' in sql:
        out(CONFIG['stop_lsn'])
    elif 'pg_drop_replication_slot' in sql:
        sys.stdout.write('\n')


def main():
    if CONFIG.get('refuse_connect'):
        sys.stderr.write('psql: error: connection to server failed: no such server\n')
        return 2
    statement = []
    for line in sys.stdin:
        line = line.rstrip('\n')
        if line == '\\q':
            return 0
        if line.startswith('\\echo '):
            sys.stdout.write(line[len('\\echo '):] + '\n')
            sys.stdout.flush()
            continue
        statement.append(line)
        if line.endswith(';'):
            answer('\n'.join(statement)[:-1])
            statement = []
            sys.stdout.flush()
    return 0


if __name__ == '__main__':
    sys.exit(main())
