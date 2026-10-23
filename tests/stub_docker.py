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
    STUB_MANIFEST  text an exec'd `cat .../backup_manifest` prints instead

An exec'd rsync prints a version banner or GNU rsync 3.x --stats output, so
transfer methods can be driven through the container as well. An exec'd
`cat .../backup_manifest` prints a manifest with a checksum on every file.

Every invocation is appended to docker.log and every exec to exec.log, one
line each; newlines inside an argument (python -c source) are written as \\n.
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
RSYNC_VERSION = 'rsync  version 3.4.1  protocol version 32\n'
# GNU rsync 3.x layout; the numbers are illustrative, not from a measured run.
RSYNC_STATS = '''
Number of files: 1,203 (reg: 1,164, dir: 39)
Number of created files: 0
Number of deleted files: 3 (reg: 3)
Number of regular files transferred: 41
Total file size: 1,231,486,976 bytes
Total transferred file size: 335,642,624 bytes
Literal data: 24,576,000 bytes
Matched data: 311,066,624 bytes
File list size: 0
File list generation time: 0.002 seconds
File list transfer time: 0.000 seconds
Total bytes sent: 24,893,113
Total bytes received: 1,210,662

sent 24,893,113 bytes  received 1,210,662 bytes  4,746,141.82 bytes/sec
total size is 1,231,486,976  speedup is 47.18
'''


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


MANIFEST = ('{"PostgreSQL-Backup-Manifest-Version": 2, "Files": [{"Path": "PG_VERSION", '
            '"Size": 3, "Checksum-Algorithm": "SHA256", "Checksum": "ab"}]}')


def one_line(argv):
    return ' '.join(argv).replace('\n', '\\n') + '\n'


def main(argv):
    if not argv:
        return 1
    command = argv[0]
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'docker.log').open('a', encoding='utf-8') as log:
        log.write(one_line(argv))
    if os.environ.get('STUB_FAIL') == command:
        sys.stderr.write(f'stub failure for {command}\n')
        return 1

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
        with (STATE / 'exec.log').open('a', encoding='utf-8') as log:
            log.write(one_line(argv))
        time.sleep(EXEC_MS / 1000.0)
        program = Path(argv[2]).name if len(argv) > 2 else ''
        if program == 'rsync':
            sys.stdout.write(RSYNC_VERSION if '--version' in argv else RSYNC_STATS)
        elif program == 'cat' and argv[-1].endswith('backup_manifest'):
            sys.stdout.write(os.environ.get('STUB_MANIFEST', MANIFEST))
        else:
            print('stub exec ok')
        return 0
    if command == 'rm':
        return 0
    sys.stderr.write(f'stub does not implement {command}\n')
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
