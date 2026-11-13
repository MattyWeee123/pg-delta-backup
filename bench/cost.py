"""A rough cost and duration estimate for a cloud run, shown before anything is created.

Prices are approximate on-demand list prices for us-central1 in US dollars, kept here by hand. They are
for deciding "is this a few cents or a few dollars?", not billing. Check the console for exact numbers.
"""
from __future__ import annotations

from bench.config import RunConfig

# $/hour for the whole VM (vCPUs + memory)
MACHINE_HOURLY = {
    'e2-standard-2': 0.067, 'e2-standard-4': 0.134, 'e2-standard-8': 0.268,
    'n2-standard-2': 0.097, 'n2-standard-4': 0.194, 'n2-standard-8': 0.389,
    'n2-highcpu-8': 0.287, 'n2d-standard-4': 0.169, 'n2d-standard-8': 0.338,
    'n2d-highcpu-4': 0.125, 'n2d-highcpu-8': 0.250,
}
VCPUS = {'e2-standard-2': 2, 'e2-standard-4': 4, 'e2-standard-8': 8, 'n2-standard-2': 2, 'n2-standard-4': 4,
         'n2-standard-8': 8, 'n2-highcpu-8': 8, 'n2d-standard-4': 4, 'n2d-standard-8': 8, 'n2d-highcpu-4': 4,
         'n2d-highcpu-8': 8}
# $/GB-month for the data disk; a 20 GB pd-balanced boot disk comes on top
DISK_MONTHLY = {'pd-standard': 0.04, 'pd-balanced': 0.10, 'pd-ssd': 0.17}
BOOT_GB = 20
HOURS_PER_MONTH = 730
DISK_MBPS = {'pd-standard': 120, 'pd-balanced': 200, 'pd-ssd': 400}  # rough sequential rates at these sizes


def link_mbps(link: str) -> float:
    if link == 'full':
        return 1500.0
    number = int(''.join(c for c in link if c.isdigit()))
    return number * (125.0 if link.endswith('gbit') else 0.125)


def estimate(cfg: RunConfig) -> dict:
    """Minutes and dollars, with the reasoning, for a cloud run of `cfg`."""
    size_mb = cfg.database_bytes / 1e6
    disk = DISK_MBPS.get(cfg.disk_type, 200)
    setup_min = 8 + size_mb / disk / 60 * 3  # VMs, packages, build the database, take the old copy, change it
    per_lane = {}
    if 'pg_basebackup' in cfg.methods:
        per_lane['pg_basebackup'] = size_mb / min(disk, link_mbps(cfg.link)) / 60 + 0.3
    if 'pgdelta' in cfg.methods:
        round_trips = cfg.database_bytes / cfg.chunk_size
        per_lane['pgdelta'] = (round_trips * 0.0004 + 2 * size_mb / disk) / 60 + 0.3  # ~0.4 ms per round trip in a region
    lanes_min = sum(per_lane.values()) * cfg.repeats * 1.3  # caches dropped, samplers, throttling between lanes
    minutes = setup_min + lanes_min + 3                    # and deletion
    hourly = MACHINE_HOURLY.get(cfg.machine)
    vms = 2 * (hourly if hourly is not None else 0.30) * minutes / 60
    disks = 2 * ((cfg.disk_gb * DISK_MONTHLY.get(cfg.disk_type, 0.10)) + BOOT_GB * 0.10) / HOURS_PER_MONTH * minutes / 60
    return {'minutes': round(minutes, 1), 'dollars': round(vms + disks, 2),
            'vcpus': 2 * VCPUS.get(cfg.machine, 8),
            'lane_minutes': {k: round(v, 1) for k, v in per_lane.items()},
            'machine_known': hourly is not None,
            'note': 'approximate list prices for us-central1; a guide, not billing'}
