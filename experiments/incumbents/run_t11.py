#!/usr/bin/env python3
"""T11 driver: run the incumbent refresh tools on EXP-03's tricky old copies.

    run_t11.py [--pg-bin DIR] [--pg-probackup PATH|auto|none] [--wal-g PATH|auto|none]
               [--scenarios S1,S2,S2b] [--tools pg_rewind,pg_probackup,walg,walg_send,pgdelta]
               [--out results/t11] [--rows N] [--wal-keep-size SIZE|none]
               [--old-copy-wal-keep-size SIZE|none] [--reuse-scenarios] [--keep-copies]

Each scenario is built with EXP-03's builder and --keep, so its source cluster stays
(stopped), under OUT/scenarios/. Every tool then runs on its own copy of B against its
own copy of that source. Writes OUT/<scenario>/<tool>/result.json, OUT/summary.json and
OUT/summary.csv. See README.md next to this file.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.incumbents import checks, tools  # noqa: E402
from experiments.tricky_copies import lsnscan, scenarios  # noqa: E402
from experiments.tricky_copies.evaluate import REPO_ROOT  # noqa: E402
from experiments.tricky_copies.pgcluster import DEFAULT_PG17, PgBin  # noqa: E402

T11_SCENARIOS = ('S1', 'S2', 'S2b')
# EXP-03's sources recycle the WAL written since the fork at their next checkpoints, and
# pg_basebackup's segment switches bring those quickly. pg_rewind's copy, and a standby
# refreshed by a catch-up tool, replay the source's WAL from before the fork or from the
# catch-up's start, so T11 keeps it on the source, as a WAL archive or replication slot
# would. B keeps EXP-03's settings unless --old-copy-wal-keep-size is given: pg_rewind
# also reads B's own WAL from the last common checkpoint on, and whether B still has it
# is part of what T11 measures.
DEFAULT_WAL_KEEP = '4GB'
DEFAULT_TOOLS = tools.TOOLS
DEFAULT_OUT = REPO_ROOT / 'results' / 't11'
DEBIAN_PG17 = Path('/usr/lib/postgresql/17/bin')


def default_pg_bin():
    """Homebrew's PostgreSQL 17 on a Mac, PGDG's on Debian."""
    return DEFAULT_PG17 if (DEFAULT_PG17 / 'initdb').is_file() else DEBIAN_PG17


def parse_list(text, allowed, what):
    """Comma-separated names, matched case-insensitively against ``allowed``."""
    by_key = {name.lower(): name for name in allowed}
    chosen = []
    for name in (part.strip() for part in text.split(',')):
        if not name:
            continue
        if name.lower() not in by_key:
            raise SystemExit(f'unknown {what} {name!r}; choose from {", ".join(allowed)}')
        chosen.append(by_key[name.lower()])
    if not chosen:
        raise SystemExit(f'no {what} given')
    return chosen


def read_json(path):
    path = Path(path)
    return json.loads(path.read_text()) if path.is_file() else None


def server_settings(wal_keep_size, old_copy_wal_keep_size=None):
    """The scenario clusters' extra settings by role: {'source': {...}, 'old_copy': {...}}."""
    def settings(size):
        return {} if size in (None, '', 'none') else {'wal_keep_size': size}
    return {'source': settings(wal_keep_size), 'old_copy': settings(old_copy_wal_keep_size)}


def scenario_ready(directory, rows, conf):
    """The scenario's metadata if it is built at ``rows`` with the settings ``conf`` (from
    server_settings) and its source kept, else None."""
    meta = read_json(Path(directory) / 'scenario.json')
    if not meta or not meta.get('built_ok') or meta.get('rows') != rows:
        return None
    # A build without server_conf predates the roles and gave every cluster the settings.
    if checks.build_conf(meta) != conf:
        return None
    kept = meta.get('source_cluster') or {}
    if not kept.get('kept') or not (Path(directory) / kept.get('datadir', '') / 'PG_VERSION').is_file():
        return None
    return meta


def build_or_reuse(spec, root, *, rows, pgbin, reuse, verbose, conf):
    directory = Path(root) / spec.dirname
    if reuse:
        meta = scenario_ready(directory, rows, conf)
        if meta:
            print(f'== reuse {spec.id} ({directory})', flush=True)
            return directory, meta, True
    print(f'== build {spec.id} ({spec.title}), {rows} rows, keeping the source cluster',
          flush=True)
    meta = scenarios.build_scenario(spec, root, rows=rows, bins={pgbin.major: pgbin},
                                    default_bin=pgbin, keep=True, force=True, verbose=verbose,
                                    source_conf=conf['source'], old_copy_conf=conf['old_copy'])
    status = 'built' if meta['built_ok'] else 'NOT BUILT'
    detail = meta.get('error') or ', '.join(meta.get('failed_checks', [])) or ''
    print(f'== {spec.id}: {status} in {meta["build_seconds"]}s {detail}', flush=True)
    return directory, meta, False


def listed_pages(directory, meta):
    """(B's pages with an LSN after the fork, where the list came from), or (None, None)
    when B and S share a timeline. S2b's scenario.json holds the list; for S2 it is
    computed here the same way, from S's timeline history file and B's pages."""
    observations = meta.get('observations') or {}
    if 'post_fork_pages' in observations:
        return observations['post_fork_pages'], {
            'source': 'scenario.json', 'fork_lsn': observations.get('fork_lsn'),
            'count': len(observations['post_fork_pages'])}
    timelines = meta.get('timelines') or {}
    if timelines.get('old_copy') == timelines.get('source_backup'):
        return None, None
    history = Path(directory) / 'source_backup' / 'pg_wal' / f"{timelines['source_backup']:08X}.history"
    fork = scenarios.history_switchpoint(history.read_text())
    pages = scenarios.pages_after_lsn(Path(directory) / 'old_copy', lsnscan.parse_lsn(fork))
    return pages, {'source': f'computed by run_t11 from B and pg_wal/{history.name} of S',
                   'fork_lsn': fork, 'count': len(pages)}


def print_table(results):
    print(f"{'scenario':8} {'tool':12} {'outcome':9} {'verdict':12} {'logical':7} "
          f"{'amcheck':7} {'left':>5} {'seconds':>8}")
    for result in results:
        row = checks.summary_row(result)
        print(f"{row['scenario']:8} {row['tool']:12} {row['outcome'] or '':9} "
              f"{result.get('verdict') or '':12} {str(row['logical_match']):7} "
              f"{str(row['amcheck_ok']):7} {str(row['diverged_pages_left']):>5} "
              f"{str(row['seconds']):>8}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog='\n'.join(__doc__.splitlines()[2:]))
    parser.add_argument('--pg-bin', default=str(default_pg_bin()),
                        help='PostgreSQL bin directory for the clusters, pg_rewind and pg_amcheck '
                             '(default: Homebrew 17, else /usr/lib/postgresql/17/bin)')
    parser.add_argument('--pg-probackup', default='auto',
                        help="pg_probackup binary, 'auto' (pg_probackup-MAJOR or pg_probackup on "
                             "PATH) or 'none'")
    parser.add_argument('--wal-g', default='auto',
                        help="wal-g binary, 'auto' (wal-g on PATH) or 'none'")
    parser.add_argument('--scenarios', default=','.join(T11_SCENARIOS),
                        help='comma-separated EXP-03 scenario ids (default %(default)s)')
    parser.add_argument('--tools', default=','.join(DEFAULT_TOOLS),
                        help='comma-separated tools (default %(default)s)')
    parser.add_argument('--out', default=str(DEFAULT_OUT), help='results directory')
    parser.add_argument('--rows', type=int, default=scenarios.DEFAULT_ROWS,
                        help='rows in the accounts table, passed to the scenario builder '
                             '(default %(default)s)')
    parser.add_argument('--wal-keep-size', default=DEFAULT_WAL_KEEP,
                        help="wal_keep_size for the cluster that becomes the source, so it keeps "
                             "its WAL since the fork (default %(default)s; 'none' for EXP-03's "
                             "settings, under which the refreshed standbys cannot catch up)")
    parser.add_argument('--old-copy-wal-keep-size', default='none',
                        help="wal_keep_size for the cluster whose data directory becomes B, so B "
                             "keeps its own WAL since the last common checkpoint, as pg_rewind "
                             "needs (default %(default)s: EXP-03's settings)")
    parser.add_argument('--reuse-scenarios', action='store_true',
                        help='reuse scenarios already built under OUT/scenarios at --rows')
    parser.add_argument('--keep-copies', action='store_true',
                        help="keep each run's copies of B and of the source under its work/")
    parser.add_argument('--tool-timeout', type=int, default=1800, help='seconds per tool command')
    parser.add_argument('--catchup-timeout', type=int, default=120,
                        help='seconds a refreshed standby may take to reach the source')
    parser.add_argument('--quiet', action='store_true', help='print one line per run')
    args = parser.parse_args(argv)

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    pgbin = PgBin(args.pg_bin)
    if not pgbin.available():
        raise SystemExit(f'no PostgreSQL binaries (initdb, postgres) in {pgbin.bindir}')
    chosen = [scenarios.BY_ID[sid] for sid in parse_list(args.scenarios, list(scenarios.BY_ID),
                                                         'scenario')]
    tool_names = parse_list(args.tools, list(tools.TOOLS), 'tool')
    infos = {name: tools.probe(name, pgbin=pgbin, pg_probackup=args.pg_probackup,
                               wal_g=args.wal_g) for name in tool_names}
    for name, info in infos.items():
        state = info['version'] if info['available'] else f"skipped: {info['reason']}"
        print(f'== {name}: {state}', flush=True)

    conf = server_settings(args.wal_keep_size, args.old_copy_wal_keep_size)
    results, scenario_info, ok = [], {}, True
    for spec in chosen:
        directory, meta, reused = build_or_reuse(
            spec, out / 'scenarios', rows=args.rows, pgbin=pgbin, reuse=args.reuse_scenarios,
            verbose=not args.quiet, conf=conf)
        info = {'dir': str(directory), 'built_ok': meta.get('built_ok'), 'rows': meta.get('rows'),
                'error': meta.get('error'), 'failed_checks': meta.get('failed_checks'),
                'old_copy_state': (meta.get('old_copy') or {}).get('cluster_state'),
                'old_copy_redo_lsn': (meta.get('old_copy') or {}).get('redo_location'),
                'timelines': meta.get('timelines'), 'server_conf': meta.get('server_conf')}
        scenario_info[spec.id] = info
        if not reused:  # earlier results for this scenario describe another build
            shutil.rmtree(out / spec.id, ignore_errors=True)
        if not meta.get('built_ok'):
            ok = False
            continue
        expected = read_json(directory / 'expected_state.json')
        pages, info['post_fork_pages'] = listed_pages(directory, meta)
        for tool in tool_names:
            run_dir = out / spec.id / tool
            shutil.rmtree(run_dir, ignore_errors=True)
            run_dir.mkdir(parents=True)
            job = tools.Job(scenario_id=spec.id, scenario_dir=directory, meta=meta,
                            expected=expected, listed_pages=pages, tool=tool,
                            tool_info=infos[tool], run_dir=run_dir, pgbin=pgbin,
                            tool_timeout=args.tool_timeout, catchup_timeout=args.catchup_timeout,
                            keep=args.keep_copies, verbose=not args.quiet)
            result = tools.run_job(job)
            result['post_fork_pages'] = info['post_fork_pages']
            tools.write_result(run_dir, result)
            results.append(result)
            ok &= 'harness_error' not in result
            print(f"== {spec.id} {tool}: {result['outcome']} -> {result['verdict']}", flush=True)

    # The summary covers every result under OUT, so tools run separately share it. Each
    # run carries its own build's rows and settings; 'invocation', 'tools' and 'scenarios'
    # describe this invocation only.
    everything = checks.collect_results(out, [spec.id for spec in scenarios.SCENARIOS],
                                        tools.TOOLS)
    others = checks.other_builds(everything, args.rows, conf)
    context = {'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
               'pg_bin': pgbin.describe(),
               'invocation': {'rows': args.rows, 'server_conf': conf,
                              'scenarios': [spec.id for spec in chosen], 'tools': tool_names},
               'tools': infos, 'scenarios': scenario_info, 'runs_from_other_builds': others}
    json_path, csv_path = checks.write_summary(out, everything, context)
    print_table(everything)
    for run in others:
        print(f"warning: {run['scenario']} {run['tool']} under {out} was built with "
              f"{run['rows']} rows and settings {run['server_conf']}, not this invocation's",
              flush=True)
    print(f'wrote {json_path} and {csv_path}')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
