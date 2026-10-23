#!/usr/bin/env python3
"""EXP-03 driver: build tricky old copies, evaluate the tool on them, summarize.

    run_scenarios.py list
    run_scenarios.py build [S1 ... S9 | all] [--out results/exp03] [--rows N]
    run_scenarios.py evaluate SCENARIO_DIR [...] [--tool-cmd CMD] [--tool-args=ARGS]
    run_scenarios.py aggregate RESULTS_DIR
    run_scenarios.py run [S1 ... S9 | all]      (build, evaluate, aggregate)

PostgreSQL binaries are passed by directory (--pg17-bin, --pg18-bin, --pg-bin);
nothing is taken from PATH. See README.md next to this file.
"""
from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.tricky_copies import evaluate as evaluation  # noqa: E402
from experiments.tricky_copies import scenarios, summary  # noqa: E402
from experiments.tricky_copies.pgcluster import DEFAULT_PG17, DEFAULT_PG18, PgBin  # noqa: E402

DEFAULT_OUT = evaluation.REPO_ROOT / 'results' / 'exp03'


def make_bins(args):
    """{major: PgBin} for every installation given, plus the one S1-S8 use."""
    bins = {}
    for path in (args.pg17_bin, args.pg18_bin):
        pgbin = PgBin(path)
        if pgbin.available():
            bins[pgbin.major] = pgbin
    default = PgBin(args.pg_bin or args.pg17_bin)
    if not default.available():
        raise SystemExit(f'no PostgreSQL binaries (initdb, postgres) in {default.bindir}')
    bins[default.major] = default
    return bins, default


def select_scenarios(names):
    if not names or [n.lower() for n in names] == ['all']:
        return list(scenarios.SCENARIOS)
    chosen = []
    for name in names:
        key = name.upper().split('-')[0]
        if key not in scenarios.BY_ID:
            raise SystemExit(f'unknown scenario {name!r}; choose from '
                             + ', '.join(scenarios.BY_ID))
        chosen.append(scenarios.BY_ID[key])
    return chosen


def cmd_list(args):
    for spec in scenarios.SCENARIOS:
        print(f'{spec.id}  {spec.dirname:32}  LSN-skip prediction: {spec.lsn_prediction:7}  '
              f'B = {spec.old_copy}')
    return 0


def do_build(args, bins, default):
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for spec in select_scenarios(args.scenarios):
        print(f'== build {spec.id} ({spec.title})', flush=True)
        meta = scenarios.build_scenario(spec, out, rows=args.rows, bins=bins,
                                        default_bin=default, keep=args.keep, force=args.force,
                                        verbose=not args.quiet)
        status = 'built' if meta['built_ok'] else 'NOT BUILT'
        detail = meta.get('error') or ', '.join(meta.get('failed_checks', [])) or ''
        print(f'== {spec.id}: {status} in {meta["build_seconds"]}s {detail}', flush=True)
        results.append((spec, meta, out / spec.dirname))
    return results


def cmd_build(args):
    bins, default = make_bins(args)
    results = do_build(args, bins, default)
    return 0 if all(meta['built_ok'] for _, meta, _ in results) else 1


def evaluate_options(args):
    return dict(python=args.python, repo_root=Path(args.repo_root).resolve(),
                tool_cmd=args.tool_cmd, tool_extra=shlex.split(args.tool_args or ''),
                pg_controldata=args.pg_controldata, skip_tool=args.skip_tool,
                selfcheck={'auto': 'auto', 'always': True, 'never': False}[args.selfcheck],
                keep_output=not args.discard_output, verbose=not args.quiet)


def print_evaluation(result):
    tool = result.get('tool') or {}
    print(f"== {result['scenario']}: tool {tool.get('status')}, byte-identical "
          f"{result['byte_identical']}, verifybackup {result['verifybackup']}, restore "
          f"{result['restore_ok']}, logical {result['logical_match']}", flush=True)
    selfcheck = result.get('source_selfcheck')
    if selfcheck:
        print(f"   self-check on S: verifybackup {selfcheck['verifybackup']}, restore "
              f"{selfcheck['restore_ok']}, logical {selfcheck['logical_match']}", flush=True)


def cmd_evaluate(args):
    bins, _ = make_bins(args)
    ok = True
    for directory in args.scenario_dirs:
        print(f'== evaluate {directory}', flush=True)
        result = evaluation.evaluate_scenario(directory, bins=bins, **evaluate_options(args))
        print_evaluation(result)
        ok &= result.get('built_ok', False) and result.get('tool_correct') is not False
    return 0 if ok else 1


def cmd_aggregate(args):
    rows, csv_path, md_path = summary.write_summary(args.results_dir, args.csv, args.markdown)
    print(summary.markdown_table(rows), end='')
    print(f'wrote {csv_path} and {md_path}')
    return 0


def cmd_run(args):
    bins, default = make_bins(args)
    results = do_build(args, bins, default)
    for _, _, directory in results:
        print(f'== evaluate {directory}', flush=True)
        print_evaluation(evaluation.evaluate_scenario(directory, bins=bins,
                                                      **evaluate_options(args)))
    args.results_dir, args.csv, args.markdown = args.out, None, None
    cmd_aggregate(args)
    return 0 if all(meta['built_ok'] for _, meta, _ in results) else 1


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog='\n'.join(__doc__.splitlines()[2:]))
    sub = parser.add_subparsers(dest='command', required=True)

    pg = argparse.ArgumentParser(add_help=False)
    pg.add_argument('--pg17-bin', default=str(DEFAULT_PG17), help='PostgreSQL 17 bin directory')
    pg.add_argument('--pg18-bin', default=str(DEFAULT_PG18), help='PostgreSQL 18 bin directory')
    pg.add_argument('--pg-bin', help='bin directory S1-S8 use (default: --pg17-bin)')
    pg.add_argument('--quiet', action='store_true', help='print only one line per scenario')

    build = argparse.ArgumentParser(add_help=False)
    build.add_argument('scenarios', nargs='*', help='scenario ids (default: all)')
    build.add_argument('--out', default=str(DEFAULT_OUT), help='results directory')
    build.add_argument('--rows', type=int, default=scenarios.DEFAULT_ROWS,
                       help='rows in the accounts table (default %(default)s, about 45 MB)')
    build.add_argument('--keep', action='store_true',
                       help='keep the working clusters (stopped) under <scenario>/pgdwork-*')
    build.add_argument('--force', action='store_true',
                       help='overwrite a non-empty output directory without scenario.json')

    ev = argparse.ArgumentParser(add_help=False)
    ev.add_argument('--python', default=sys.executable, help='interpreter that runs the tool')
    ev.add_argument('--repo-root', default=str(evaluation.REPO_ROOT),
                    help='directory put on PYTHONPATH (and cwd) for the tool')
    ev.add_argument('--tool-cmd', help='replace "PYTHON -m pgdelta"; {python} is substituted')
    ev.add_argument('--tool-args', help='extra tool flags as one string, e.g. '
                    '--tool-args="--identity-gate=off"')
    ev.add_argument('--pg-controldata', default='auto',
                    help="pg_controldata passed to the tool: 'auto' (matching S), a path, or 'none'")
    ev.add_argument('--skip-tool', action='store_true', help='do not run the tool')
    ev.add_argument('--selfcheck', choices=('auto', 'always', 'never'), default='auto',
                    help='also verify and restore S itself (auto: when the tool did not succeed)')
    ev.add_argument('--discard-output', action='store_true', help='delete O after checking it')

    sub.add_parser('list', help='list the scenarios').set_defaults(func=cmd_list)
    sub.add_parser('build', parents=[pg, build], help='build scenarios').set_defaults(
        func=cmd_build)
    p = sub.add_parser('evaluate', parents=[pg, ev], help='run and check the tool')
    p.add_argument('scenario_dirs', nargs='+')
    p.set_defaults(func=cmd_evaluate)
    p = sub.add_parser('aggregate', help='write summary.csv and summary.md')
    p.add_argument('results_dir')
    p.add_argument('--csv', help='CSV path (default RESULTS_DIR/summary.csv)')
    p.add_argument('--markdown', help='Markdown path (default RESULTS_DIR/summary.md)')
    p.set_defaults(func=cmd_aggregate)
    sub.add_parser('run', parents=[pg, build, ev], help='build, evaluate and aggregate'
                   ).set_defaults(func=cmd_run)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
