"""EXP-80's control on EXP-62's blind spot: S2e (a crashed old primary whose WAL past the failover split was lost,
its last checkpoint record kept), refreshed by the fast path as built (guard v2 must refuse it) and after crash
recovery first, as pg_rewind runs on its target (docs/experiments/EXP-80-crashed-standby.md).

    .venv/bin/python experiments/fastpath_crashed/s2e_control.py [--builds 3] [--out docs/experiments/data/exp80-s2e.json]

Each build is fresh (experiments/tricky_copies/run_scenarios.py build S2e --wal-summaries --keep). Then:
- as built: experiments/tricky_copies/fastpath_check.py on it;
- recovery first: a copy of the scenario whose old copy ran `postgres --single -F` (crash recovery, then a shutdown
  checkpoint), then the same check.
Each check restores the refreshed copy and compares it logically with the source (EXP-63's evaluate).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PG_BIN = Path(os.environ.get('PG_BIN', '/opt/homebrew/opt/postgresql@17/bin'))
ENV = dict(os.environ, LC_ALL='C', LANG='C', PYTHONPATH=str(ROOT))


def state(path):
    text = subprocess.run([str(PG_BIN / 'pg_controldata'), str(path)], capture_output=True, text=True, env=ENV).stdout
    return {key: next((line.split(':', 1)[1].strip() for line in text.splitlines() if line.startswith(key)), None)
            for key in ('Database cluster state', 'Latest checkpoint location')}


def check(results):
    """The fast-path check's attempt on the scenario under `results`, from its fastpath_check.json."""
    subprocess.run([sys.executable, str(ROOT / 'experiments' / 'tricky_copies' / 'fastpath_check.py'), str(results),
                    'S2e'], capture_output=True, text=True, env=ENV, timeout=1800, check=True)
    document = json.loads(next(results.glob('S2e-*/fastpath_check.json')).read_text())
    attempt = document['attempts'][0]
    return {key: attempt.get(key) for key in ('exit_code', 'fast_used', 'logical_match', 'pages_sent', 'pages_total',
                                              'receiver_refused', 'reasons')}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--builds', type=int, default=3)
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'experiments' / 'data' / 'exp80-s2e.json'))
    a = ap.parse_args()
    rows = []
    for build in range(a.builds):
        with tempfile.TemporaryDirectory(prefix='exp80-s2e-') as temp:
            built = Path(temp) / 'built'
            subprocess.run([sys.executable, str(ROOT / 'experiments' / 'tricky_copies' / 'run_scenarios.py'), 'build',
                            'S2e', '--wal-summaries', '--keep', '--out', str(built), '--quiet'], env=ENV, check=True,
                           capture_output=True, text=True, timeout=1800)
            first = Path(temp) / 'first'
            shutil.copytree(built, first, symlinks=True)
            old = next(first.glob('S2e-*/old_copy'))
            (old / 'postmaster.pid').unlink(missing_ok=True)
            before = state(old)
            single = subprocess.run([str(PG_BIN / 'postgres'), '--single', '-F', '-D', str(old), 'template1'],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, env=ENV, timeout=600)
            row = {'build': build, 'state_built': before, 'recovery_exit': single.returncode,
                   'state_after_recovery': state(old), 'as_built': check(built), 'recovery_first': check(first)}
            rows.append(row)
            print(json.dumps(row), flush=True)
    Path(a.out).write_text(json.dumps(rows, indent=1) + '\n')
    print(a.out)


if __name__ == '__main__':
    main()
