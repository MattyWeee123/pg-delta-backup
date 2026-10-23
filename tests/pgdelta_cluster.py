"""Real PostgreSQL clusters for the pgdelta tests: initdb, start and stop, SQL. Skipped without binaries."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile

PG_BIN = Path(os.environ.get('PGDELTA_TEST_PG_BIN', '/opt/homebrew/opt/postgresql@17/bin'))
TOOLS = ('initdb', 'pg_ctl', 'psql', 'pg_controldata')
ENV = dict(os.environ, LC_ALL='C', LANG='C')
CONTROLDATA = PG_BIN / 'pg_controldata'


def available():
    return all((PG_BIN / tool).exists() for tool in TOOLS)


SKIP_REASON = f'PostgreSQL binaries ({", ".join(TOOLS)}) not found in {PG_BIN}; set PGDELTA_TEST_PG_BIN'


def initdb(datadir):
    subprocess.run([str(PG_BIN / 'initdb'), '-D', str(datadir), '--username=t', '--auth=trust', '--no-sync'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=ENV, timeout=120)


class Servers:
    """Starts and stops clusters one at a time on a private Unix socket directory (no TCP)."""

    def __init__(self, logs):
        self.logs = Path(logs)
        # Unix socket paths are limited to about 100 bytes, so not under the test's temporary directory.
        self.sockets = Path(tempfile.mkdtemp(prefix='pgd'))

    def close(self):
        shutil.rmtree(self.sockets, True)

    def pg_ctl(self, datadir, action, check=True, mode='fast', timeout=60):
        return subprocess.run(
            [str(PG_BIN / 'pg_ctl'), '-D', str(datadir), '-l', str(self.logs / f'{Path(datadir).name}.log'),
             '-w', '-t', '30', '-m', mode, '-o', f"-k {self.sockets} -c listen_addresses='' -c fsync=off",
             action], check=check, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=ENV,
            timeout=timeout)

    def sql(self, statement):
        return subprocess.run([str(PG_BIN / 'psql'), '-h', str(self.sockets), '-U', 't', '-d', 'postgres', '-At',
                               '-v', 'ON_ERROR_STOP=1', '-c', statement], check=True, capture_output=True,
                              text=True, env=ENV, timeout=60).stdout.strip()

    def run_sql(self, datadir, *statements):
        """Start the cluster, run the statements, stop it cleanly; return the last statement's output."""
        self.pg_ctl(datadir, 'start')
        try:
            result = None
            for statement in statements:
                result = self.sql(statement)
            return result
        finally:
            self.pg_ctl(datadir, 'stop')
