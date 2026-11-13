"""python -m bench {run,estimate,cleanup,publish}: see bench/README.md."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bench import config
from viz import runs


def _config_flags(p):
    p.add_argument('--config', type=Path, help='a JSON config (see bench/config.py); flags override it')
    p.add_argument('--preset', choices=sorted(config.PRESETS), help='quick (default) or baseline (the EXP-92 setup)')
    p.add_argument('--name')
    p.add_argument('--where', choices=('laptop', 'gcp'))
    p.add_argument('--methods', help='comma-separated: pg_basebackup,pgdelta')
    p.add_argument('--scale', type=int)
    p.add_argument('--change', choices=config.CHANGES)
    p.add_argument('--repeats', type=int)
    p.add_argument('--chunk-size', type=int)
    p.add_argument('--link', help="gcp: full, or a cap like 1gbit or 500mbit")
    p.add_argument('--machine', help='gcp machine type, e.g. n2-standard-4')
    p.add_argument('--disk-type', choices=('pd-standard', 'pd-balanced', 'pd-ssd'))
    p.add_argument('--disk-gb', type=int)
    p.add_argument('--notes')


def _load(args) -> config.RunConfig:
    source = config.load_file(args.config) if args.config else {}
    return config.load(source, preset=args.preset, name=args.name, where=args.where,
                       methods=args.methods.split(',') if args.methods else None, scale=args.scale,
                       change=args.change, repeats=args.repeats, chunk_size=args.chunk_size, link=args.link,
                       machine=args.machine, disk_type=args.disk_type, disk_gb=args.disk_gb, notes=args.notes)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='bench', description='Run, record and publish pgdelta benchmarks.')
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='run a benchmark and record it in runs/<name>/')
    _config_flags(run)
    run.add_argument('--publish', action='store_true', help='commit the run to branch runs/<name> and open a PR')
    run.add_argument('--yes', action='store_true', help='gcp: create the VMs without asking')
    run.add_argument('--keep-vms', action='store_true', help="gcp: don't delete the VMs (they still expire)")
    run.add_argument('--work', type=Path, default=Path('demo-work'), help='laptop: scratch directory')
    run.add_argument('--pgbin', help='laptop: PostgreSQL binaries (default: pg_config --bindir)')
    est = commands.add_parser('estimate', help='gcp: the cost, quota and checks, without creating anything')
    _config_flags(est)
    clean = commands.add_parser('cleanup', help='delete benchmark VMs whose expiry has passed')
    clean.add_argument('--all', action='store_true', help='delete every benchmark VM, expired or not')
    pub = commands.add_parser('publish', help='commit an existing run and open a PR')
    pub.add_argument('name')
    args = parser.parse_args(argv)

    try:
        if args.command == 'cleanup':
            from bench.gcp import cleanup
            deleted = cleanup(all_benchmarks=args.all)
            print(f"bench: deleted {', '.join(deleted) if deleted else 'nothing'}")
            return 0
        if args.command == 'publish':
            from bench.runner import _publish
            run_dir = runs.find(runs.DEFAULT_ROOTS, args.name)
            if run_dir is None:
                raise SystemExit(f'bench: no run named {args.name}')
            cfg = config.load(json.loads((run_dir / 'config.json').read_text()))
            result = _publish(run_dir, cfg, (run_dir / 'report.md').read_text(), print)
            meta = json.loads((run_dir / 'meta.json').read_text())
            meta['publish'] = result
            runs.write_meta(run_dir, meta)
            return 0
        cfg = _load(args)
        estimate = None
        if cfg.where == 'gcp':
            from bench.gcp import preflight
            check = preflight(cfg)
            estimate = check['estimate']
            print(f"bench: {cfg.name}: two {cfg.machine} VMs, about {estimate['minutes']} min and "
                  f"${estimate['dollars']} ({estimate['note']})")
            for warning in check['warnings']:
                print(f'bench: warning: {warning}')
            for problem in check['problems']:
                print(f'bench: cannot run: {problem}')
            if args.command == 'estimate':
                print(json.dumps(check, indent=2, default=str))
                return 1 if check['problems'] else 0
            if check['problems']:
                return 1
            if not args.yes and input('Create the VMs? [y/N] ').strip().lower() != 'y':
                print('bench: cancelled')
                return 1
        elif args.command == 'estimate':
            print('bench: a laptop run costs nothing; it takes about a minute at scale 50')
            return 0
        from bench.runner import execute
        run_dir = execute(cfg, work=args.work, pgbin=args.pgbin, publish=args.publish, estimate=estimate,
                          keep_vms=args.keep_vms)
        print(f'bench: done: {run_dir}/report.md  (watch or replay: python -m viz serve)')
        return 0
    except (ValueError, RuntimeError) as exc:
        print(f'bench: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
