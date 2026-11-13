"""What a benchmark run is: where it runs, on what, and which methods it compares.

A config is a JSON object (or command-line flags); anything left out takes the preset's value.
Presets:
- quick: cheap and fast. Two n2-standard-4 VMs (16 GB) with 50 GB balanced disks, a 750 MB database.
- baseline: the first phase's EXP-90 to EXP-92 setup. Two n2d-highcpu-8 VMs (8 GB) with 150 GB balanced
  disks, an 18 GB database (bigger than memory), so its numbers compare with that phase's.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field, fields

METHODS = ('pg_basebackup', 'pgdelta')
CHANGES = ('hot', 'uniform')
LINK = re.compile(r'^(full|\d+(mbit|gbit))$')

PRESETS = {
    'quick': {'machine': 'n2-standard-4', 'disk_type': 'pd-balanced', 'disk_gb': 50, 'scale': 50},
    'baseline': {'machine': 'n2d-highcpu-8', 'disk_type': 'pd-balanced', 'disk_gb': 150, 'scale': 600},
}


@dataclass
class RunConfig:
    name: str = ''
    where: str = 'laptop'                       # 'laptop' or 'gcp'
    methods: list = field(default_factory=lambda: list(METHODS))
    scale: int = 50                             # pgbench scale: about 15 MB each
    change: str = 'hot'                         # 1% of rows: 'hot' (one range) or 'uniform' (every 100th)
    repeats: int = 1
    chunk_size: int = 8192
    link: str = 'full'                          # gcp: 'full' or a cap like '1gbit', '500mbit'
    machine: str = 'n2-standard-4'              # gcp
    disk_type: str = 'pd-balanced'              # gcp: the data disk at /data
    disk_gb: int = 50                           # gcp
    region: str = 'us-central1'                 # gcp
    zones: list = field(default_factory=lambda: ['us-central1-a', 'us-central1-b'])  # gcp: source, destination
    ttl_minutes: int = 120                      # gcp: VMs are labelled to expire after this
    notes: str = ''

    def validate(self) -> 'RunConfig':
        if not self.name:
            self.name = time.strftime(f'{self.where}-%Y%m%d-%H%M%S')
        if not re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$', self.name):
            raise ValueError('name: letters, digits, dot, dash and underscore, at most 61 characters')
        if self.where not in ('laptop', 'gcp'):
            raise ValueError("where must be 'laptop' or 'gcp'")
        if not self.methods or any(m not in METHODS for m in self.methods):
            raise ValueError(f'methods must be a non-empty list from {METHODS}')
        if self.change not in CHANGES:
            raise ValueError(f'change must be one of {CHANGES}')
        if not 1 <= self.scale <= 20000:
            raise ValueError('scale must be between 1 and 20000')
        if not 1 <= self.repeats <= 10:
            raise ValueError('repeats must be between 1 and 10')
        if not LINK.match(self.link):
            raise ValueError("link must be 'full' or a rate like '1gbit' or '500mbit'")
        if self.where == 'laptop' and self.link != 'full':
            raise ValueError('a laptop run has no network to cap: use link full')
        if len(self.zones) != 2 or not all(z.startswith(self.region + '-') for z in self.zones):
            raise ValueError(f'zones must be two zones of {self.region}')
        if not 10 <= self.disk_gb <= 2000:
            raise ValueError('disk_gb must be between 10 and 2000')
        if self.where == 'gcp' and self.disk_gb * 1e9 < 3 * self.scale * 15e6:
            raise ValueError(f'disk_gb {self.disk_gb} is too small for scale {self.scale}: '
                             f'the source and two copies need about {3 * self.scale * 15e6 / 1e9:.0f} GB')
        return self

    @property
    def database_bytes(self) -> int:
        return self.scale * 15_000_000

    def lanes(self) -> list[dict]:
        """One lane per method and repeat, in run order (pg_basebackup's first: it needs the server running)."""
        labels = {'pg_basebackup': 'pg_basebackup (copies everything)',
                  'pgdelta': 'pgdelta (sends what differs)'}
        out = []
        for method in sorted(self.methods, key=METHODS.index):
            for k in range(1, self.repeats + 1):
                suffix = f' · run {k}' if self.repeats > 1 else ''
                out.append({'id': method if self.repeats == 1 else f'{method}-r{k}', 'method': method,
                            'repeat': k, 'label': labels[method] + suffix})
        return out

    def to_json(self) -> dict:
        return asdict(self)


def load(source: dict | None = None, preset: str | None = None, **overrides) -> RunConfig:
    """A config from a preset, then a JSON object, then keyword overrides (None values are ignored)."""
    values = dict(PRESETS.get(preset or 'quick', {}))
    values.update(source or {})
    values.update({k: v for k, v in overrides.items() if v is not None})
    known = {f.name for f in fields(RunConfig)}
    unknown = set(values) - known
    if unknown:
        raise ValueError(f'unknown settings: {", ".join(sorted(unknown))}')
    return RunConfig(**values).validate()


def load_file(path) -> dict:
    with open(path) as f:
        return json.load(f)
