"""Commit a run to its own branch and open a pull request, without touching your working copy.

A temporary git worktree starts from origin/main; the run directory is copied into runs/<name>/ there,
committed on branch runs/<name>, pushed, and a pull request is opened with gh. The worktree is then
removed, so whatever you were working on stays as it was.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MAX_BYTES = 20 << 20


def git(*args, cwd=None) -> str:
    cwd = cwd or REPO
    return subprocess.run(['git', *map(str, args)], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def publish(run_dir: Path, title: str, body: str, repo: Path | None = None) -> dict:
    run_dir = Path(run_dir)
    repo = Path(repo or REPO)
    name = run_dir.name
    size = sum(p.stat().st_size for p in run_dir.rglob('*') if p.is_file())
    if size > MAX_BYTES:
        raise RuntimeError(f'run directory is {size / 1e6:.0f} MB; the limit for a commit is {MAX_BYTES >> 20} MB')
    branch = f'runs/{name}'
    git('fetch', '-q', 'origin', 'main', cwd=repo)
    tmp = Path(tempfile.mkdtemp(prefix='pgdelta-publish-'))
    worktree = tmp / 'wt'
    git('worktree', 'add', '-q', '-b', branch, worktree, 'origin/main', cwd=repo)
    try:
        target = worktree / 'runs' / name
        shutil.copytree(run_dir, target, ignore=shutil.ignore_patterns('code.tgz', '*.tmp'))
        git('add', '-f', f'runs/{name}', cwd=worktree)
        git('commit', '-q', '-m', f'Run {name}: {title}', cwd=worktree)
        git('push', '-q', '-u', 'origin', branch, cwd=worktree)
        url = ''
        if shutil.which('gh'):
            out = subprocess.run(['gh', 'pr', 'create', '--base', 'main', '--head', branch, '--title', f'Run {name}: {title}',
                                  '--body', body], cwd=worktree, capture_output=True, text=True)
            url = out.stdout.strip().splitlines()[-1] if out.returncode == 0 and out.stdout.strip() else ''
        if not url:
            remote = git('remote', 'get-url', 'origin', cwd=repo).removesuffix('.git').replace('git@github.com:', 'https://github.com/')
            url = f'{remote}/compare/main...{branch}?expand=1'
        return {'branch': branch, 'url': url}
    finally:
        subprocess.run(['git', 'worktree', 'remove', '--force', str(worktree)], cwd=repo, capture_output=True)
        subprocess.run(['git', 'branch', '-D', branch], cwd=repo, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
