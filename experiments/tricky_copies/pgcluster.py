"""Throwaway PostgreSQL clusters for EXP-03, driven through the stock binaries.

Stdlib only. Every server listens on a Unix socket only (``listen_addresses=''``)
in a short private directory, on a port picked at run time, so several runs can
share a machine. Binaries are always passed by directory; nothing relies on PATH.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

DEFAULT_PG17 = Path('/opt/homebrew/opt/postgresql@17/bin')
DEFAULT_PG18 = Path('/opt/homebrew/opt/postgresql@18/bin')
SUPERUSER = 'postgres'
# sockaddr_un.sun_path is 104 bytes on macOS (108 on Linux), including the NUL.
MAX_SOCKET_PATH = 103

REDO_KEY = "Latest checkpoint's REDO location"
START_WAL_KEY = 'START WAL LOCATION'
LSN_TEXT = re.compile(r'^([0-9A-Fa-f]{1,8}/[0-9A-Fa-f]{1,8})\b')


class CommandError(RuntimeError):
    def __init__(self, cmd, returncode, stdout, stderr):
        self.cmd, self.returncode, self.stdout, self.stderr = cmd, returncode, stdout, stderr
        tail = (stderr or stdout or '').strip().splitlines()[-15:]
        super().__init__(f'{Path(cmd[0]).name} exited with {returncode}: ' + ' | '.join(tail))


def clean_env(extra=None):
    """Environment without PG* variables and with English, untranslated messages."""
    env = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
    env.update(LC_ALL='C', LANG='C', TZ='UTC')
    if extra:
        env.update(extra)
    return env


def run(cmd, *, input=None, cwd=None, env=None, timeout=900, check=True):
    cmd = [str(part) for part in cmd]
    proc = subprocess.run(cmd, input=input, cwd=cwd, env=env or clean_env(), capture_output=True,
                          text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, proc.stdout, proc.stderr)
    return proc


def free_port(exclude=()):
    """A TCP port nobody is bound to right now. The servers only use it to name their socket."""
    for _ in range(100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        if port not in exclude and 1024 < port < 65536:
            return port
    raise RuntimeError('no free port found')


def short_tempdir(prefix='pgd'):
    """A private directory with a path short enough to hold a PostgreSQL socket."""
    base = '/tmp' if os.path.isdir('/tmp') and os.access('/tmp', os.W_OK) else None
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=base))
    if len(str(path / '.s.PGSQL.65535.lock')) > MAX_SOCKET_PATH:
        shutil.rmtree(path, ignore_errors=True)
        raise RuntimeError(f'socket directory {path} is too long for a Unix socket path')
    return path


def tree_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            if not os.path.islink(full):
                total += os.path.getsize(full)
    return total


def parse_lsn_text(value):
    """Return the leading 'X/Y' LSN of a pg_controldata or backup_label value."""
    match = LSN_TEXT.match(value.strip())
    if not match:
        raise ValueError(f'no LSN in {value!r}')
    return match.group(1).upper()


def parse_controldata(text):
    """pg_controldata output as {label: value}. Labels never contain a colon; values may."""
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(':')
        if sep and key.strip():
            fields[key.strip()] = value.strip()
    if 'Database system identifier' not in fields:
        raise ValueError('text does not look like pg_controldata output')
    return fields


def parse_backup_label(text):
    """backup_label as {KEY: value}, e.g. 'START WAL LOCATION': '0/2000028 (file ...)'."""
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(': ')
        if sep:
            fields[key.strip()] = value.strip()
    if START_WAL_KEY not in fields:
        raise ValueError('backup_label has no START WAL LOCATION line')
    return fields


def select_threshold(old_copy, controldata_text=None, controldata_fn=None):
    """Pick the LSN-skip threshold for an old copy, as EXP-03 defines it.

    A base backup directory (it still has backup_label) starts recovery at the
    label's START WAL LOCATION. Any other data directory starts at the REDO
    location of the checkpoint recorded in pg_control.
    """
    old_copy = Path(old_copy)
    label = old_copy / 'backup_label'
    if label.is_file():
        fields = parse_backup_label(label.read_text())
        return {'lsn': parse_lsn_text(fields[START_WAL_KEY]),
                'source': 'backup_label START WAL LOCATION',
                'file': 'backup_label',
                'raw': fields[START_WAL_KEY],
                'timeline': fields.get('START TIMELINE')}
    if controldata_text is None:
        if controldata_fn is None:
            raise ValueError(f'{old_copy} has no backup_label and no pg_controldata was given')
        controldata_text = controldata_fn(old_copy)
    fields = parse_controldata(controldata_text)
    return {'lsn': parse_lsn_text(fields[REDO_KEY]),
            'source': f"pg_controldata {REDO_KEY}",
            'file': 'global/pg_control',
            'raw': fields[REDO_KEY],
            'timeline': fields.get("Latest checkpoint's TimeLineID")}


def pg_major_of(datadir):
    """Major version written in a data directory's PG_VERSION file."""
    return int((Path(datadir) / 'PG_VERSION').read_text().strip().split('.')[0])


class PgBin:
    """One installation's bin directory."""

    def __init__(self, bindir):
        self.bindir = Path(bindir)
        self._version = None

    def tool(self, name):
        path = self.bindir / name
        if not path.is_file():
            raise FileNotFoundError(f'{path} not found')
        return path

    @property
    def version_text(self):
        if self._version is None:
            self._version = run([self.tool('postgres'), '--version']).stdout.strip()
        return self._version

    @property
    def major(self):
        match = re.search(r'\(PostgreSQL\) (\d+)', self.version_text)
        if not match:
            raise RuntimeError(f'cannot read a version from {self.version_text!r}')
        return int(match.group(1))

    def available(self):
        return (self.bindir / 'initdb').is_file() and (self.bindir / 'postgres').is_file()

    def controldata_text(self, datadir):
        return run([self.tool('pg_controldata'), '-D', datadir]).stdout

    def controldata(self, datadir):
        return parse_controldata(self.controldata_text(datadir))

    def describe(self):
        return {'bindir': str(self.bindir), 'version': self.version_text, 'major': self.major}


class Cluster:
    """A data directory plus the socket and port its server uses while running."""

    def __init__(self, name, pgbin, datadir, sockdir, port, logfile):
        self.name, self.pgbin = name, pgbin
        self.datadir, self.sockdir = Path(datadir), Path(sockdir)
        self.port, self.logfile = port, Path(logfile)
        self.running = False

    def __repr__(self):
        return f'Cluster({self.name!r}, port={self.port}, datadir={str(self.datadir)!r})'

    # -- lifecycle ---------------------------------------------------------
    def initdb(self, checksums=True, extra=()):
        args = [self.pgbin.tool('initdb'), '-D', self.datadir, '-U', SUPERUSER, '-A', 'trust',
                '-E', 'UTF8', '--locale=C', '--no-sync', '--no-instructions']
        if checksums:
            args.append('--data-checksums')
        elif self.pgbin.major >= 18:
            args.append('--no-data-checksums')  # PG18 turned checksums on by default
        run([*args, *extra])

    def append_conf(self, settings):
        lines = ['', '# added by experiments/tricky_copies']
        lines += [f"{key} = '{value}'" for key, value in settings.items()]
        with (self.datadir / 'postgresql.conf').open('a') as conf:
            conf.write('\n'.join(lines) + '\n')

    def server_options(self):
        return f"-p {self.port} -k {self.sockdir} -c listen_addresses=''"

    def start(self, timeout=300):
        self.logfile.parent.mkdir(parents=True, exist_ok=True)
        run([self.pgbin.tool('pg_ctl'), '-D', self.datadir, '-l', self.logfile, '-w',
             '-t', timeout, '-o', self.server_options(), 'start'], timeout=timeout + 30)
        self.running = True

    def stop(self, mode='fast', timeout=300):
        run([self.pgbin.tool('pg_ctl'), '-D', self.datadir, '-w', '-t', timeout, '-m', mode,
             'stop'], timeout=timeout + 30)
        self.running = False

    def promote(self, timeout=300):
        run([self.pgbin.tool('pg_ctl'), '-D', self.datadir, '-w', '-t', timeout, 'promote'],
            timeout=timeout + 30)

    def is_running(self):
        proc = run([self.pgbin.tool('pg_ctl'), '-D', self.datadir, 'status'], check=False)
        return proc.returncode == 0

    def ensure_stopped(self):
        """Best-effort stop for cleanup paths; never raises."""
        try:
            if self.datadir.exists() and self.is_running():
                self.stop('immediate', timeout=60)
        except Exception:  # noqa: BLE001 - cleanup must not mask the real error
            pass
        self.running = False

    # -- SQL ---------------------------------------------------------------
    def conn_args(self, db='postgres'):
        return ['-h', self.sockdir, '-p', self.port, '-U', SUPERUSER, '-d', db]

    def psql(self, sql, db='postgres', timeout=900):
        """Run a script (one autocommit transaction per statement) and return raw output."""
        proc = run([self.pgbin.tool('psql'), '-X', '-q', '-A', '-t', '-F', '\t', '-v',
                    'ON_ERROR_STOP=1', '--no-password', *self.conn_args(db), '-f', '-'],
                   input=sql, timeout=timeout)
        return proc.stdout

    def query(self, sql, db='postgres', timeout=900):
        out = self.psql(sql, db, timeout)
        return [line.split('\t') for line in out.splitlines() if line != '']

    def scalar(self, sql, db='postgres'):
        rows = self.query(sql, db)
        if len(rows) != 1 or len(rows[0]) != 1:
            raise RuntimeError(f'expected one value from {sql!r}, got {rows!r}')
        return rows[0][0]

    def wait_for(self, sql, db='postgres', timeout=120, interval=0.2):
        """Poll until a boolean query returns true."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self.scalar(sql, db) == 't':
                    return
            except CommandError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f'{self.name}: timed out waiting for {sql!r}')
            time.sleep(interval)

    def basebackup(self, target, extra=(), timeout=1800):
        """pg_basebackup exactly as EXP-03 specifies, plus connection options."""
        run([self.pgbin.tool('pg_basebackup'), '-D', target, '-Fp', '-X', 'stream',
             '--manifest-checksums=SHA256', '-c', 'fast', '--no-password',
             *self.conn_args()[:-2], *extra], timeout=timeout)
