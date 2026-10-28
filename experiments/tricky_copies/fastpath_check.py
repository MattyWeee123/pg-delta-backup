#!/usr/bin/env python3
"""EXP-63: pgdelta's own --fast-path summaries, run live, on EXP-03's tricky old copies
(docs/experiments/EXP-63-fast-path-tricky-copies.md).

    .venv/bin/python experiments/tricky_copies/fastpath_check.py RESULTS_DIR [SCENARIO ...]

RESULTS_DIR holds scenarios built with `run_scenarios.py build --wal-summaries --keep`. For each, the kept source
cluster is started as a live primary, a copy of the old copy B is refreshed in place with `pgdelta local
--source-kind live --fast-path summaries` (and, where the identity gate refused, again with --reuse-any-basis), and
each published copy is restored and compared with the source's logical state. Writes fastpath_check.json in each
scenario directory.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.tricky_copies.evaluate import restore_and_compare  # noqa: E402
from experiments.tricky_copies.pgcluster import SUPERUSER, Cluster, PgBin, free_port, short_tempdir  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def refresh(scenario, source, sockdir, port, source_bin, basis_bin, work, extra):
    """One in-place refresh of a fresh copy of B: (target, exit code, statistics or None, stderr tail)."""
    target = Path(tempfile.mkdtemp(prefix='fp-', dir=work)) / 'B'
    shutil.copytree(scenario / 'old_copy', target, symlinks=True)
    os.chmod(target, 0o700)
    stats_path = target.parent / 'stats.json'
    env = dict(os.environ, PYTHONPATH=str(REPO))
    argv = [sys.executable, '-m', 'pgdelta', 'local', '--source', str(source), '--source-kind', 'live',
            '--pg-controldata', str(source_bin.tool('pg_controldata')),
            '--basis-pg-controldata', str(basis_bin.tool('pg_controldata')),
            '--pg-conn', f'host={sockdir} port={port} user={SUPERUSER} dbname=postgres',
            '--basis', str(target), '--in-place', '--fast-path', 'summaries', '--stats-json', str(stats_path),
            # EXP-63's settings, pinned since EXP-68, EXP-70 and EXP-71 changed the fast path's defaults.
            '--read-ahead', '0', '--fill-gaps', '0', '--no-diverged-basis', '--fsync', 'main', *extra]
    result = subprocess.run(argv, capture_output=True, text=True, cwd=REPO, env=env, timeout=1800)
    stats = json.loads(stats_path.read_text()) if stats_path.exists() else None
    return target, result.returncode, stats, result.stderr[-1500:]


def summarize(stats):
    if not stats:
        return None
    fast, received = stats.get('fast_path') or {}, (stats.get('receiver') or {}).get('fast_path') or {}
    return {'mode': stats.get('mode'), 'fast_used': fast.get('used'), 'reasons': fast.get('reasons'),
            'receiver_refused': received.get('refused'), 'proof': (received.get('facts') or {}).get('proof'),
            'files_chosen': fast.get('files_chosen'), 'files_whole': fast.get('files_whole'),
            'pages_total': (stats.get('pages') or {}).get('total'), 'pages_sent': (stats.get('pages') or {}).get('sent')}


def check(scenario):
    meta = json.loads((scenario / 'scenario.json').read_text())
    expected = json.loads((scenario / 'expected_state.json').read_text())
    source_bin = PgBin(meta['postgresql']['source_backup']['bindir'])
    basis_bin = PgBin(meta['postgresql']['old_copy']['bindir'])
    source = scenario / meta['source_cluster']['datadir']
    work = Path(tempfile.mkdtemp(prefix='fpcheck-', dir=scenario))
    sockdir = short_tempdir()
    cluster = Cluster('source', source_bin, source, sockdir, free_port(), work / 'source.log')
    attempts = []
    try:
        cluster.start()
        for extra in ([], ['--reuse-any-basis']):
            target, code, stats, stderr = refresh(scenario, source, sockdir, cluster.port, source_bin, basis_bin,
                                                  work, extra)
            attempt = {'args': extra, 'exit_code': code, **(summarize(stats) or {}), 'stderr_tail': stderr}
            if code == 0:
                restored = restore_and_compare(target, expected, source_bin, work)
                attempt.update(restored=restored.get('restore_ok'), logical_match=restored.get('logical_match'),
                               differences=restored.get('differences', [])[:5], restore_error=restored.get('error'))
            attempts.append(attempt)
            shutil.rmtree(target.parent, ignore_errors=True)
            if code == 0 or extra:
                break  # published (or refused even with --reuse-any-basis): no second invocation
    finally:
        cluster.ensure_stopped()
        shutil.rmtree(sockdir, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
    document = {'scenario': meta['id'], 'attempts': attempts}
    (scenario / 'fastpath_check.json').write_text(json.dumps(document, indent=2) + '\n')
    return document


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    root, wanted = Path(args[0]), {name.upper() for name in args[1:]}
    for scenario in sorted(p for p in root.iterdir() if (p / 'scenario.json').exists()):
        if wanted and scenario.name.split('-')[0].upper() not in wanted:
            continue
        document = check(scenario)
        for a in document['attempts']:
            print(f"{document['scenario']:4} {' '.join(a['args']) or 'default':18} exit {a['exit_code']} "
                  f"fast {a.get('fast_used')} match {a.get('logical_match')} "
                  f"sent {a.get('pages_sent')}/{a.get('pages_total')} "
                  f"{(a.get('reasons') or [a.get('receiver_refused') or ''])[:1]}", flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
