"""A stand-in for the docker CLI, used by the HammerDB driver tests.

Implements only the subcommands the driver calls. State lives in STUB_STATE so
one invocation can see containers started by another. Timestamps are real so
the window arithmetic is exercised against genuine wall-clock ordering rather
than fabricated values.

Environment:
    STUB_STATE     directory holding per-container state (required)
    STUB_JOBS_DB   jobs database returned by `cp` (required for cp)
    STUB_WINDOW    seconds between the ramp-up and completion markers
    STUB_CADENCE   seconds between counter samples in the emitted log
    STUB_EXEC_MS   milliseconds each `exec` takes, to give a backup real width
    STUB_FAIL      subcommand that should exit non-zero
"""

import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE = Path(os.environ['STUB_STATE'])
WINDOW = float(os.environ.get('STUB_WINDOW', '2.0'))
CADENCE = float(os.environ.get('STUB_CADENCE', '0.02'))
EXEC_MS = float(os.environ.get('STUB_EXEC_MS', '100'))


def rfc3339(moment):
    return moment.strftime('%Y-%m-%dT%H:%M:%S.') + f'{moment.microsecond:06d}000Z'


def started_at(name):
    return datetime.fromisoformat((STATE / f'{name}.start').read_text().strip())


def build_log(name):
    """A log whose ramp-up marker is the container start.

    The marker must not land after the backup begins, or every pair would be
    rejected for a backup outside its window.
    """
    start = started_at(name)
    end = start + timedelta(seconds=WINDOW)
    # HammerDB writes a carriage return between timestamp and message.
    lines = [f'{rfc3339(start)} \rVuser 1:Rampup complete, Taking start Count.']
    moment, value = start, 20000
    while moment <= end:
        lines.append(f'{rfc3339(moment)} {value} PostgreSQL tpm')
        moment += timedelta(seconds=CADENCE)
        value = 20000 if value != 20000 else 19000
    lines.append(f'{rfc3339(end)} \rVuser 1:Test complete, Taking end Count.')
    lines.append(f'{rfc3339(end)} \rVuser 1:TEST RESULT : System achieved '
                 '9000 NOPM from 21000 PostgreSQL TPM')
    return '\n'.join(lines) + '\n'


def write_jobs_db(name, destination):
    """Copy the template jobs database, re-stamping it onto this run's window."""
    source = Path(os.environ['STUB_JOBS_DB'])
    Path(destination).write_bytes(source.read_bytes())
    start = started_at(name)
    connection = sqlite3.connect(destination)
    with connection:
        connection.execute('DELETE FROM JOBTCOUNT')
        moment, value = start, 20000
        while moment <= start + timedelta(seconds=WINDOW):
            connection.execute(
                'INSERT INTO JOBTCOUNT (jobid, counter, metric, timestamp) '
                'VALUES (?,?,?,?)',
                ('stub', value, 'tpm', moment.strftime('%Y-%m-%d %H:%M:%S')))
            moment += timedelta(seconds=CADENCE)
            value = 20000 if value != 20000 else 19000
    connection.close()


def main(argv):
    if not argv:
        return 1
    command = argv[0]
    if os.environ.get('STUB_FAIL') == command:
        sys.stderr.write(f'stub failure for {command}\n')
        return 1
    STATE.mkdir(parents=True, exist_ok=True)

    if command == 'run':
        name = argv[argv.index('--name') + 1]
        (STATE / f'{name}.start').write_text(
            datetime.now(timezone.utc).isoformat())
        (STATE / f'{name}.args').write_text('\n'.join(argv))
        print(name)
        return 0
    if command == 'logs':
        name = argv[-1]
        if not (STATE / f'{name}.start').exists():
            sys.stderr.write('No such container\n')
            return 1
        sys.stdout.write(build_log(name))
        return 0
    if command == 'inspect':
        # The workload is treated as finished as soon as it is asked about,
        # so tests do not wait out a real measurement window.
        print('false')
        return 0
    if command == 'cp':
        name = argv[1].split(':', 1)[0]
        write_jobs_db(name, argv[2])
        return 0
    if command == 'exec':
        (STATE / 'exec.log').open('a', encoding='utf-8').write(
            ' '.join(argv) + '\n')
        time.sleep(EXEC_MS / 1000.0)
        print('stub exec ok')
        return 0
    if command == 'rm':
        return 0
    sys.stderr.write(f'stub does not implement {command}\n')
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
