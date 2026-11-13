"""python -m viz {serve,race,import-sampler}: see viz/README.md."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from viz import runs, server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='viz', description='Watch refresh runs live, or replay recorded ones.')
    parser.add_argument('--port', type=int, default=8765)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('serve', help='serve the page and the runs in runs/ and viz/examples/')

    race = commands.add_parser('race', help='a laptop race: pg_basebackup vs pgdelta, served live while it runs')
    race.add_argument('--name', help='run name (default: race-<time>)')
    race.add_argument('--scale', type=int, default=50, help='pgbench scale (about 15 MB each; default %(default)s)')
    race.add_argument('--change', choices=('hot', 'uniform'), default='hot')
    race.add_argument('--work', type=Path, default=Path('demo-work'), help='scratch directory for the databases')
    race.add_argument('--pgbin', help='PostgreSQL binaries (default: pg_config --bindir)')
    race.add_argument('--no-serve', action='store_true', help="don't start the web server")

    imp = commands.add_parser('import-sampler', help='replay recorded VM runs (util_series.py samples)')
    imp.add_argument('name')
    imp.add_argument('lanes', nargs='+', metavar='RUN_DIR:ID:LABEL',
                     help='a recorded run directory, a lane id and its label, e.g. results/run-13:full:pg_basebackup')
    imp.add_argument('--title', default='')
    imp.add_argument('--limits', default='', help='known limits in MB/s, e.g. link=119.6,disk=205.5')
    imp.add_argument('--setup', default='', help='a one-line description of the setup')
    imp.add_argument('--out', type=Path, default=runs.DEFAULT_ROOTS[0], help='where to write the run (default: runs/)')

    args = parser.parse_args(argv)
    if args.command == 'serve':
        srv = server.start(port=args.port)
        print(f'viz: open http://localhost:{args.port}  (Ctrl-C to stop)')
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            srv.shutdown()
        return 0
    if args.command == 'race':
        from viz.race import pg_bin, race as run_race
        name = args.name or time.strftime('race-%Y%m%d-%H%M%S')
        srv = None if args.no_serve else server.start(port=args.port)
        if srv:
            print(f'viz: watch live at http://localhost:{args.port}/#{name}')
        run_race(name, runs.DEFAULT_ROOTS[0], args.work, args.scale, args.change, pg_bin(args.pgbin))
        if srv:
            print(f'viz: done; replay at http://localhost:{args.port}/#{name}  (Ctrl-C to stop the server)')
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                srv.shutdown()
        return 0
    from viz import sampler
    limits = dict((k, float(v)) for k, v in (item.split('=') for item in args.limits.split(',') if item))
    sources = []
    for spec in args.lanes:
        run_dir, lane_id, label = spec.split(':', 2)
        sources.append((Path(run_dir), lane_id, label))
    run_dir = runs.new_run(args.out, args.name, args.title or args.name, [], limits=limits, setup=args.setup)
    lanes = sampler.write_lanes(run_dir, sources)
    meta = runs.load(run_dir)['meta']
    meta.update(lanes=lanes, finished=True)
    runs.write_meta(run_dir, meta)
    print(f'viz: wrote {run_dir} ({len(lanes)} lanes)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
