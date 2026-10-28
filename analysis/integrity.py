"""Rule 16's scorer-integrity check: the files that score an experiment, run it and are under test are recorded at its
registration, and checked unchanged when its results are written (docs/experiments/registrations.json).

    .venv/bin/python analysis/integrity.py register EXP-80 --commit ca04163 FILE ...
    .venv/bin/python analysis/integrity.py check EXP-80

`register` records each file's SHA-256 as of the commit (git show), with the commit. `check` compares each with the
file now: a change is listed with `git diff --stat`, and the exit status is 1, so a write-up that runs the check
names every change after registration instead of leaving it to be noticed. A change the registration allowed (a
figure's layout, say) is still listed; the write-up says why it changes no score.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / 'docs' / 'experiments' / 'registrations.json'


def git(*args):
    return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, check=True).stdout


def load():
    return json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}


def register(experiment, commit, files):
    registry = load()
    full = git('rev-parse', commit).decode().strip()
    registry[experiment] = {'commit': full[:12], 'files': {
        path: hashlib.sha256(git('show', f'{full}:{path}')).hexdigest() for path in sorted(files)}}
    REGISTRY.write_text(json.dumps(registry, indent=1, sort_keys=True) + '\n')
    print(f'{experiment}: {len(files)} files recorded as of {full[:12]}')


def check(experiment):
    entry = load().get(experiment)
    if entry is None:
        print(f'{experiment}: not registered')
        return 1
    changed = []
    for path, digest in entry['files'].items():
        current = ROOT / path
        now = hashlib.sha256(current.read_bytes()).hexdigest() if current.exists() else None
        if now != digest:
            changed.append(path)
            stat = git('diff', '--stat', entry['commit'], '--', path).decode().strip().splitlines()
            print(f'CHANGED since {entry["commit"]}: {path}' + (f' ({stat[-1].strip()})' if stat else ' (missing)'))
    print(f"{experiment}: {len(entry['files']) - len(changed)} of {len(entry['files'])} files unchanged since "
          f"{entry['commit']}")
    return 1 if changed else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='command', required=True)
    reg = sub.add_parser('register')
    reg.add_argument('experiment')
    reg.add_argument('--commit', default='HEAD')
    reg.add_argument('files', nargs='+')
    chk = sub.add_parser('check')
    chk.add_argument('experiment')
    a = ap.parse_args(argv)
    if a.command == 'register':
        register(a.experiment, a.commit, a.files)
        return 0
    return check(a.experiment)


if __name__ == '__main__':
    sys.exit(main())
