#!/usr/bin/env python3
"""EXP-69, part A: idea 1 (both sides' block sets) on EXP-03's failover scenarios, counted
(docs/experiments/EXP-69-both-sides-block-sets.md).

    .venv/bin/python experiments/tricky_copies/union_check.py RESULTS_DIR

Builds S2, S2b, S2c and S2d with the source's WAL summaries (EXP-61) twice: under RESULTS_DIR/kept, with
wal_keep_size = 1GB on the cluster that becomes B, as an operator who uses pg_rewind must keep B's WAL, and under
RESULTS_DIR/default (S2 and S2c only), as EXP-61 built them. In each it scores idea 1 (unionscan.score_scenario), and
in kept/S2 also a copy of B with its WAL cut after the fork's segment. Then it starts the kept source and runs
`pg_rewind --dry-run --debug` on a copy of B, as the control on B's side: pg_rewind's blocks against those
pg_waldump names from the common checkpoint's record on. Writes unioncheck.json in each scenario directory.
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

from experiments.tricky_copies import run_scenarios, scenarios, unionscan  # noqa: E402
from experiments.tricky_copies.lsnscan import parse_lsn  # noqa: E402
from experiments.tricky_copies.pgcluster import SUPERUSER, Cluster, PgBin, free_port, short_tempdir  # noqa: E402

BUILDS = {'kept': ('S2', 'S2b', 'S2c', 'S2d'), 'default': ('S2', 'S2c')}
KEEP_WAL = {'wal_keep_size': '1GB'}
SPECS = {s.id: s for s in scenarios.SCENARIOS + scenarios.EXTRA_SCENARIOS}


def build(out):
    args = run_scenarios.build_parser().parse_args(['build', '--out', str(out)])
    bins, default = run_scenarios.make_bins(args)
    for variant, ids in BUILDS.items():
        for sid in ids:
            spec = SPECS[sid]
            done = Path(out) / variant / spec.dirname / 'scenario.json'
            if done.exists() and json.loads(done.read_text()).get('built_ok'):
                continue
            meta = scenarios.build_scenario(spec, Path(out) / variant, bins=bins, default_bin=default, keep=True,
                                            verbose=False, wal_summaries=True,
                                            old_copy_conf=KEEP_WAL if variant == 'kept' else None)
            print(f'built {variant}/{sid}: {meta.get("built_ok")} {meta.get("error") or ""}', flush=True)


def cut_after_fork(old_copy, fork, work):
    """A copy of B without its WAL segments after the one holding the fork: B's end is still proven by its clean
    stop, but its WAL no longer reaches it."""
    target = Path(tempfile.mkdtemp(prefix='cut-', dir=work)) / 'B'
    shutil.copytree(old_copy, target, symlinks=True)
    removed = []
    for path in sorted((target / 'pg_wal').iterdir()):
        found = unionscan.SEGMENT_NAME.match(path.name)
        if found and (int(found.group(2), 16) << 32 | int(found.group(3), 16) << 24) \
                // unionscan.WAL_SEGMENT_BYTES > fork // unionscan.WAL_SEGMENT_BYTES:
            path.unlink()
            removed.append(path.name)
    return target, removed


def rewind(scenario, meta, binp, work):
    """pg_rewind --dry-run --debug from the kept source onto a copy of B: (exit code, parsed output)."""
    sockdir = short_tempdir()
    cluster = Cluster('source', binp, scenario / meta['source_cluster']['datadir'], sockdir, free_port(),
                      work / 'source.log')
    target = Path(tempfile.mkdtemp(prefix='rw-', dir=work)) / 'B'
    shutil.copytree(scenario / 'old_copy', target, symlinks=True)
    os.chmod(target, 0o700)
    try:
        cluster.start()
        proc = subprocess.run([str(binp.tool('pg_rewind')), '--target-pgdata', str(target), '--source-server',
                               f'host={sockdir} port={cluster.port} user={SUPERUSER} dbname=postgres',
                               '--dry-run', '--debug', '--progress'], capture_output=True, text=True, timeout=1800)
    finally:
        cluster.ensure_stopped()
        shutil.rmtree(sockdir, ignore_errors=True)
    text = proc.stdout + proc.stderr
    (scenario / 'rewind-debug.log').write_text(text)
    return proc.returncode, unionscan.parse_rewind_debug(text)


def check(scenario):
    meta = json.loads((scenario / 'scenario.json').read_text())
    binp = PgBin(meta['postgresql']['source_backup']['bindir'])
    waldump = binp.tool('pg_waldump')
    old_info, source_info, threshold = meta['old_copy'], meta['source_backup'], meta['threshold']
    result = unionscan.score_scenario(scenario, old_info, source_info, threshold, waldump)
    ours = result.pop('b_main_blocks_from_record', None)
    work = Path(tempfile.mkdtemp(prefix='union-', dir=scenario))
    try:
        if meta['id'] == 'S2' and scenario.parent.name == 'kept' and result.get('fork'):
            cut, removed = cut_after_fork(scenario / 'old_copy', parse_lsn(result['fork']), work)
            scored = unionscan.score_scenario(scenario, old_info, source_info, threshold, waldump, old=cut)
            scored.pop('b_main_blocks_from_record', None)
            result['wal_cut'] = {'segments_removed': removed, 'guard': scored['guard'], 'b': scored['b']}
        code, parsed = rewind(scenario, meta, binp, work)
        theirs = unionscan.rewind_blocks_by_relation(parsed)
        composition = {}
        for path, size in parsed['bytes'].items():
            kind = unionscan.copy_kind(path)
            composition[kind] = composition.get(kind, 0) + size
        result['pg_rewind'] = {'exit_code': code, 'need_mb': parsed['need_mb'],
                               'blocks': sum(len(b) for b in theirs.values()), 'bytes_by_kind': composition,
                               'actions': {a: list(parsed['actions'].values()).count(a)
                                           for a in sorted(set(parsed['actions'].values()))}}
        if ours is not None and code == 0:
            keys = set(ours) | set(theirs)
            result['pg_rewind']['only_pg_rewind'] = sum(len(theirs.get(k, set()) - ours.get(k, set())) for k in keys)
            result['pg_rewind']['only_waldump'] = sum(len(ours.get(k, set()) - theirs.get(k, set())) for k in keys)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    result.update(scenario=meta['id'], variant=scenario.parent.name)
    (scenario / 'unioncheck.json').write_text(json.dumps(result, indent=2, default=sorted) + '\n')
    return result


def main(argv=None):
    out = Path((sys.argv[1:] if argv is None else argv)[0]).resolve()  # the harness compares absolute paths
    build(out)
    for variant in BUILDS:
        for scenario in sorted(p for p in (out / variant).iterdir() if (p / 'scenario.json').exists()):
            r = check(scenario)
            s = r.get('scored') or {}
            rw = r.get('pg_rewind') or {}
            print(f"{variant:8} {r['scenario']:4} guard {r['guard']['ok']!s:5} real_wrong {s.get('real_wrong')} "
                  f"wrong {s.get('wrong')} b_hints {r.get('left_wrong_with_b_hints')} sent {s.get('pages_sent')} "
                  f"changed {s.get('pages_changed')} | pg_rewind exit {rw.get('exit_code')} blocks {rw.get('blocks')}"
                  f" only_rw {rw.get('only_pg_rewind')} only_wd {rw.get('only_waldump')} "
                  f"| {r['guard']['reasons'][:1]} {('cut: ' + str(r['wal_cut']['guard']['reasons'][:1])) if 'wal_cut' in r else ''}",
                  flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
