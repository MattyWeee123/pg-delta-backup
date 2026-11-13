"""A run's steps and how long each should take: what the viewer's progress bar and ETA are built from.

Before anything starts, a run writes <run>/steps.json: every step in order, with an expected duration and
where that number came from. Each step is stamped when it starts and when it finishes. The viewer counts
down from those stamps and corrects itself as the run goes (see viz/static/index.html, eta()).

Where the expected durations come from, best first:
1. the latest finished run of the same kind, scaled to this database's size. A laptop run only learns from
   runs on the same computer. A cloud run learns from runs on the same machine type, and a lane also needs
   the same disk, link cap, change and chunk size;
2. with no such run yet, the rules of thumb below, measured on one Apple-silicon laptop (scale 50) and one
   cloud run (two n2-standard-4 VMs in two zones of us-central1, scale 20). They can be 2x off elsewhere.
"""
from __future__ import annotations

import hashlib
import json
import platform
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

from bench import cost
from bench.config import RunConfig

# Seconds, as (fixed, per GB of database). pgdelta's lanes add ROUND_TRIP seconds per chunk.
RULES = {
    'laptop': {'build': (1, 5), 'copy': (1, 2), 'lane:pg_basebackup': (0.5, 1.5), 'lane:pgdelta': (0.5, 1),
               'check': (2, 0), 'publish': (5, 0)},
    'gcp': {'vms': (70, 0), 'install': (110, 0), 'build': (5, 40), 'copy': (5, 25), 'lane:pg_basebackup': (8, 0),
            'lane:pgdelta': (8, 10), 'check': (5, 0), 'delete': (45, 0), 'publish': (5, 0)},
}
ROUND_TRIP = {'laptop': 0.0001, 'gcp': 0.00105}  # one stop-and-wait chunk: same machine; two zones
# Settings that must match for an earlier run's step to predict this one (cloud runs; a laptop run matches
# on the computer instead of machine and disk).
MATCH = {'vms': ('machine',), 'install': ('machine',), 'delete': ('machine',), 'check': (), 'publish': (),
         'build': ('machine', 'disk_type'), 'copy': ('machine', 'disk_type'),
         'lane': ('machine', 'disk_type', 'link', 'change', 'chunk_size')}
GROWS = {'build', 'copy', 'lane'}  # take longer with a bigger database


def host_id() -> str:
    """This computer, without naming it (steps.json may be published)."""
    return hashlib.sha256(platform.node().encode()).hexdigest()[:10]


def rule(cfg: RunConfig, kind: str) -> float:
    fixed, per_gb = RULES[cfg.where][kind]
    gb = cfg.database_bytes / 1e9
    seconds = fixed + per_gb * gb
    if kind == 'lane:pgdelta':
        seconds += ROUND_TRIP[cfg.where] * cfg.database_bytes / cfg.chunk_size
    if kind == 'lane:pg_basebackup' and cfg.where == 'gcp':
        seconds += gb * 1000 / min(cost.DISK_MBPS.get(cfg.disk_type, 200), cost.link_mbps(cfg.link))
    return seconds


def history(root: Path | None) -> list[dict]:
    """Earlier runs' steps.json, newest first."""
    out = []
    for path in Path(root).glob('*/steps.json') if root else ():
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data.get('config'), dict):
            out.append(data)
    return sorted(out, key=lambda d: d.get('created', 0), reverse=True)


def expect(cfg: RunConfig, kind: str, past: list[dict]) -> tuple[float, str]:
    """Expected seconds for a step of `kind`, and where the number came from."""
    base = kind.split(':')[0]
    fields = MATCH[base] if cfg.where == 'gcp' else tuple(f for f in MATCH[base] if f in ('change', 'chunk_size'))
    for run in past:
        old = run['config']
        if old.get('where') != cfg.where or (cfg.where == 'laptop' and run.get('host') != host_id()):
            continue
        if any(old.get(f) != getattr(cfg, f) for f in fields):
            continue
        took = [s['finished'] - s['started'] for s in run.get('steps', [])
                if s.get('kind') == kind and s.get('started') and s.get('finished') and not s.get('failed')]
        if took and old.get('scale'):
            seconds = sum(took) / len(took) * (cfg.scale / old['scale'] if base in GROWS else 1)
            return round(seconds, 1), f"measured in {old.get('name', 'an earlier run')}"
    return round(rule(cfg, kind), 1), 'rule of thumb'


def plan(cfg: RunConfig, publish: bool = False, root: Path | None = None) -> list[dict]:
    """Every step of a run of `cfg`, in order, each with its expected seconds."""
    steps = []

    def add(step_id, label, kind=None, lane=None):
        steps.append({'id': step_id, 'kind': kind or step_id, 'label': label, 'lane': lane})

    if cfg.where == 'gcp':
        add('vms', 'Create the two VMs')
        add('install', 'Install PostgreSQL and the code')
    add('build', 'Build the database')
    add('copy', 'Take the old copy, then change 1% of rows')
    for lane in cfg.lanes():
        add('lane:' + lane['id'], lane['label'], kind='lane:' + lane['method'], lane=lane['id'])
    if 'pgdelta' in cfg.methods:
        add('check', 'Check the refreshed copy matches')
    if cfg.where == 'gcp':
        add('delete', 'Delete the VMs')
    if publish:
        add('publish', 'Commit the run and open a pull request')
    past = history(root)
    for step in steps:
        step['expect'], step['basis'] = expect(cfg, step['kind'], past)
    return steps


class Steps:
    """<run>/steps.json: the plan, stamped as each step starts and finishes."""

    def __init__(self, run_dir: Path, cfg: RunConfig, publish: bool = False, root: Path | None = None):
        self.path = Path(run_dir) / 'steps.json'
        self.data = {'created': time.time(), 'host': host_id(), 'config': cfg.to_json(),
                     'dollars_per_hour': cost.hourly(cfg) if cfg.where == 'gcp' else 0,
                     'steps': plan(cfg, publish, root)}
        self._write()

    @contextmanager
    def step(self, step_id: str):
        found = next((s for s in self.data['steps'] if s['id'] == step_id), None)
        if found is None:
            yield
            return
        found['started'] = time.time()
        self._write()
        try:
            yield
        except BaseException:
            found['failed'] = True
            raise
        finally:
            found['finished'] = time.time()
            self._write()

    def skip(self, step_id: str) -> None:
        for s in self.data['steps']:
            if s['id'] == step_id:
                s['skipped'] = True
        self._write()

    def _write(self) -> None:
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.data, indent=1) + '\n')
        tmp.replace(self.path)


def no_steps(_step_id: str):
    """A stand-in for Steps.step when a backend runs without a plan (tests, old callers)."""
    return nullcontext()
