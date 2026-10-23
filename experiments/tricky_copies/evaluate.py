"""EXP-03 evaluation: run the tool on (S, B), then check its output O.

The tool contract (the pgdelta package is built separately):

    PYTHON -m pgdelta local --source S --basis B --output O --stats-json STATS
                            [--pg-controldata PATH] [EXTRA ...]

STATS is JSON with bytes_source_to_dest, bytes_dest_to_source, mode ("reuse" or
"full") and pages {total, sent}. When pgdelta cannot be imported the tool step
is recorded as "tool not available" and the rest still runs; by default the
harness then checks S itself (pg_verifybackup, restore, logical comparison) so
the expected state and the restore procedure are known to be sound.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from experiments.tricky_copies.logical import compare_logical_states, compute_logical_state
from experiments.tricky_copies.pgcluster import (Cluster, CommandError, free_port, pg_major_of,
                                                 run, short_tempdir)

REPO_ROOT = Path(__file__).resolve().parents[2]
STATS_KEYS = ('bytes_source_to_dest', 'bytes_dest_to_source', 'mode', 'pages')
CHUNK = 1 << 20


# --------------------------------------------------------------------------
# Tree comparison
# --------------------------------------------------------------------------
def tree_entries(root):
    """{relative path: 'file' | 'dir' | 'symlink'} for everything under root."""
    root = Path(root)
    entries = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            full = Path(dirpath, name)
            rel = full.relative_to(root).as_posix()
            if full.is_symlink():
                entries[rel] = 'symlink'
            elif full.is_dir():
                entries[rel] = 'dir'
            else:
                entries[rel] = 'file'
    return entries


def same_bytes(left, right):
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open('rb') as a, right.open('rb') as b:
        while True:
            x, y = a.read(CHUNK), b.read(CHUNK)
            if x != y:
                return False
            if not x:
                return True


def compare_trees(expected, actual, limit=20):
    """Compare every file, directory and symlink of two trees byte for byte."""
    want, got = tree_entries(expected), tree_entries(actual)
    missing = sorted(set(want) - set(got))
    extra = sorted(set(got) - set(want))
    kind_mismatch, differing, compared = [], [], 0
    for rel in sorted(set(want) & set(got)):
        if want[rel] != got[rel]:
            kind_mismatch.append(rel)
        elif want[rel] == 'file':
            compared += 1
            if not same_bytes(Path(expected, rel), Path(actual, rel)):
                differing.append(rel)
        elif want[rel] == 'symlink':
            if os.readlink(Path(expected, rel)) != os.readlink(Path(actual, rel)):
                differing.append(rel)
    identical = not (missing or extra or kind_mismatch or differing)
    return {'identical': identical, 'files_compared': compared,
            'missing_in_output': len(missing), 'extra_in_output': len(extra),
            'type_mismatches': len(kind_mismatch), 'differing_files': len(differing),
            'examples': {'missing': missing[:limit], 'extra': extra[:limit],
                         'type_mismatch': kind_mismatch[:limit], 'differing': differing[:limit]}}


def tree_fingerprint(root):
    """Cheap fingerprint (size and mtime of every file) to show a tree was left alone."""
    result = {}
    for rel, kind in tree_entries(root).items():
        if kind == 'file':
            stat = Path(root, rel).stat()
            result[rel] = (stat.st_size, stat.st_mtime_ns)
    return result


# --------------------------------------------------------------------------
# Tool
# --------------------------------------------------------------------------
def tool_env(repo_root):
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(
        part for part in (str(repo_root), os.environ.get('PYTHONPATH', '')) if part)
    return env


def tool_importable(python, repo_root):
    code = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('pgdelta') else 3)"
    try:
        proc = run([python, '-c', code], cwd=repo_root, env=tool_env(repo_root), check=False,
                   timeout=120)
    except (OSError, ValueError):
        return False
    return proc.returncode == 0


def tool_command(source, basis, output, stats, *, python, tool_cmd=None, pg_controldata=None,
                 extra=()):
    """The tool's command line. ``tool_cmd`` replaces "PYTHON -m pgdelta"; ``{python}`` in
    it is replaced by the configured interpreter."""
    if tool_cmd:
        prefix = [part.replace('{python}', str(python)) for part in shlex.split(tool_cmd)]
    else:
        prefix = [str(python), '-m', 'pgdelta']
    cmd = prefix + ['local', '--source', str(source), '--basis', str(basis),
                    '--output', str(output), '--stats-json', str(stats)]
    if pg_controldata:
        cmd += ['--pg-controldata', str(pg_controldata)]
    return cmd + list(extra)


def summarize_stats(stats):
    if not isinstance(stats, dict):
        return {'missing_keys': list(STATS_KEYS)}
    pages = stats.get('pages') if isinstance(stats.get('pages'), dict) else {}
    return {'bytes_source_to_dest': stats.get('bytes_source_to_dest'),
            'bytes_dest_to_source': stats.get('bytes_dest_to_source'),
            'mode': stats.get('mode'),
            'pages_total': pages.get('total'),
            'pages_sent': pages.get('sent'),
            'missing_keys': [key for key in STATS_KEYS if key not in stats]}


def run_tool(scenario_dir, *, python, repo_root, tool_cmd=None, pg_controldata=None, extra=(),
             timeout=7200):
    scenario_dir = Path(scenario_dir)
    source, basis = scenario_dir / 'source_backup', scenario_dir / 'old_copy'
    output, stats_path = scenario_dir / 'output', scenario_dir / 'tool_stats.json'
    cmd = tool_command(source, basis, output, stats_path, python=python, tool_cmd=tool_cmd,
                       pg_controldata=pg_controldata, extra=extra)
    result = {'command': cmd, 'python': str(python), 'repo_root': str(repo_root)}
    if not tool_cmd and not tool_importable(python, repo_root):
        result['status'] = 'tool not available'
        result['detail'] = f'pgdelta is not importable by {python} with PYTHONPATH={repo_root}'
        return result
    shutil.rmtree(output, ignore_errors=True)
    stats_path.unlink(missing_ok=True)
    before = tree_fingerprint(basis)
    started = time.monotonic()
    try:
        proc = run(cmd, cwd=repo_root, env=tool_env(repo_root), check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        result.update(status='failed', detail=f'{type(exc).__name__}: {exc}')
        return result
    result['seconds'] = round(time.monotonic() - started, 2)
    result['returncode'] = proc.returncode
    (scenario_dir / 'tool.log').write_text(
        '$ ' + shlex.join(cmd) + '\n--- stdout\n' + proc.stdout + '\n--- stderr\n' + proc.stderr)
    result['stderr_tail'] = proc.stderr.strip().splitlines()[-10:]
    result['basis_unchanged'] = tree_fingerprint(basis) == before
    if stats_path.is_file():
        try:
            result['stats'] = json.loads(stats_path.read_text())
        except json.JSONDecodeError as exc:
            result['stats_error'] = str(exc)
    result['stats_summary'] = summarize_stats(result.get('stats'))
    result['status'] = 'ok' if proc.returncode == 0 else 'failed'
    return result


# --------------------------------------------------------------------------
# Checks on a backup directory
# --------------------------------------------------------------------------
def verify_backup(backup_dir, pgbin):
    proc = run([pgbin.tool('pg_verifybackup'), backup_dir], check=False, timeout=3600)
    return {'ok': proc.returncode == 0, 'returncode': proc.returncode,
            'output': (proc.stdout + proc.stderr).strip().splitlines()[-10:]}


def log_tail(path, lines=20):
    try:
        return Path(path).read_text(errors='replace').splitlines()[-lines:]
    except OSError:
        return []


def restore_and_compare(backup_dir, expected_state, pgbin, work_parent, keep=False):
    """Copy a backup to a disposable directory, start it, wait for recovery to finish
    and compare its logical state with the expected one."""
    work = Path(tempfile.mkdtemp(prefix='pgdrestore-', dir=work_parent))
    sockdir = short_tempdir()
    datadir = work / 'data'
    result = {'restore_ok': False, 'logical_match': False}
    cluster = Cluster('restore', pgbin, datadir, sockdir, free_port(), work / 'restore.log')
    try:
        shutil.copytree(backup_dir, datadir, symlinks=True)
        os.chmod(datadir, 0o700)
        started = time.monotonic()
        cluster.start(timeout=900)
        cluster.wait_for("SELECT CASE WHEN pg_is_in_recovery() "
                         "THEN pg_get_wal_replay_pause_state() = 'paused' ELSE true END",
                         timeout=900)
        result['in_recovery'] = cluster.scalar('SELECT pg_is_in_recovery()') == 't'
        result['startup_seconds'] = round(time.monotonic() - started, 2)
        result['restore_ok'] = True
        actual = compute_logical_state(cluster.query)
        differences = compare_logical_states(expected_state, actual)
        result['logical_match'] = not differences
        result['differences'] = differences[:50]
        result['tables_compared'] = sum(len(t) for t in actual['tables'].values())
    except (CommandError, TimeoutError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
        result['log_tail'] = log_tail(cluster.logfile)
    finally:
        cluster.ensure_stopped()
        shutil.rmtree(sockdir, ignore_errors=True)
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
    return result


def lsn_numbers(scan):
    if not scan:
        return None
    totals = scan.get('totals', {})
    by_kind = {kind: {'compared': v.get('pages_compared'), 'skipped': v.get('pages_skipped'),
                      'wrongly_skipped': v.get('pages_wrongly_skipped'),
                      'changed': v.get('pages_changed')}
               for kind, v in scan.get('by_kind', {}).items()}
    return {'threshold': scan.get('redo_lsn'),
            'pages_compared': totals.get('pages_compared'),
            'pages_changed': totals.get('pages_changed'),
            'pages_skipped': totals.get('pages_skipped'),
            'pages_wrongly_skipped': totals.get('pages_wrongly_skipped'),
            'pages_changed_and_sent': totals.get('pages_changed_and_sent'),
            'by_kind': by_kind}


def read_json(path):
    path = Path(path)
    return json.loads(path.read_text()) if path.is_file() else None


def evaluate_scenario(scenario_dir, *, bins, python, repo_root=REPO_ROOT, tool_cmd=None,
                      tool_extra=(), pg_controldata='auto', skip_tool=False, selfcheck='auto',
                      keep_output=True, verbose=True):
    """Run every EXP-03 check for one scenario directory and write evaluation.json."""
    scenario_dir = Path(scenario_dir).resolve()
    meta = read_json(scenario_dir / 'scenario.json') or {}
    expected = read_json(scenario_dir / 'expected_state.json')
    scan = read_json(scenario_dir / 'lsnscan.json')
    source = scenario_dir / 'source_backup'

    def say(text):
        if verbose:
            print(f'  {scenario_dir.name}: {text}', flush=True)

    evaluation = {'scenario': meta.get('id', scenario_dir.name), 'name': meta.get('name'),
                  'built_ok': meta.get('built_ok', False),
                  'threshold_source': (meta.get('threshold') or {}).get('source'),
                  'tool': None, 'byte_identical': None, 'byte_comparison': None,
                  'verifybackup': None, 'restore_ok': None, 'logical_match': None,
                  'lsn_skip': lsn_numbers(scan), 'source_selfcheck': None}
    if not meta.get('built_ok') or expected is None or not source.is_dir():
        evaluation['tool'] = {'status': 'not run: scenario not built'}
        write_evaluation(scenario_dir, evaluation)
        return evaluation

    pgbin = bins.get(pg_major_of(source))
    if pgbin is None:
        raise RuntimeError(f'no PostgreSQL {pg_major_of(source)} binaries for {source}')
    if pg_controldata == 'auto':
        pg_controldata = pgbin.tool('pg_controldata')
    elif pg_controldata in (None, '', 'none'):
        pg_controldata = None

    if skip_tool:
        evaluation['tool'] = {'status': 'skipped'}
    else:
        say('running the tool')
        evaluation['tool'] = run_tool(scenario_dir, python=python, repo_root=repo_root,
                                      tool_cmd=tool_cmd, pg_controldata=pg_controldata,
                                      extra=tool_extra)
        say(f"tool: {evaluation['tool']['status']}")

    output = scenario_dir / 'output'
    if evaluation['tool'].get('status') in ('ok', 'failed') and output.is_dir():
        say('comparing O with S byte for byte')
        evaluation['byte_comparison'] = compare_trees(source, output)
        evaluation['byte_identical'] = evaluation['byte_comparison']['identical']
        say('pg_verifybackup O')
        verify = verify_backup(output, pgbin)
        evaluation['verifybackup'], evaluation['verifybackup_detail'] = verify['ok'], verify
        say('restoring O and comparing its logical state')
        restore = restore_and_compare(output, expected, pgbin, scenario_dir)
        evaluation['restore_ok'] = restore['restore_ok']
        evaluation['logical_match'] = restore['logical_match']
        evaluation['restore_detail'] = restore
        if not keep_output:
            shutil.rmtree(output, ignore_errors=True)
    elif evaluation['tool'].get('status') == 'ok':
        evaluation['tool']['detail'] = 'tool exited 0 but wrote no output directory'
    evaluation['tool_correct'] = tool_correct(evaluation)

    if selfcheck is True or (selfcheck == 'auto' and evaluation['tool'].get('status') != 'ok'):
        say('self-check: pg_verifybackup S, restore S, compare logical state')
        verify = verify_backup(source, pgbin)
        restore = restore_and_compare(source, expected, pgbin, scenario_dir)
        evaluation['source_selfcheck'] = {'verifybackup': verify['ok'],
                                          'restore_ok': restore['restore_ok'],
                                          'logical_match': restore['logical_match'],
                                          'verifybackup_detail': verify, 'restore_detail': restore}
    write_evaluation(scenario_dir, evaluation)
    return evaluation


def tool_correct(evaluation):
    """Overall verdict on the rebuilt copy O, or None when the tool did not run.

The byte comparison and pg_verifybackup are what prove correctness; the restore and
logical comparison are necessary but cannot see stale hint bits, FSM/VM or most index
pages, so all four must pass.
"""
    if (evaluation.get('tool') or {}).get('status') not in ('ok', 'failed'):
        return None
    return all(evaluation.get(key) is True for key in
               ('byte_identical', 'verifybackup', 'restore_ok', 'logical_match'))


def write_evaluation(scenario_dir, evaluation):
    evaluation['evaluated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    (Path(scenario_dir) / 'evaluation.json').write_text(json.dumps(evaluation, indent=2) + '\n')
