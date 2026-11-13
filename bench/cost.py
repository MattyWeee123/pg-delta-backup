"""A rough cost for a cloud run: what its VMs cost per hour, and in all for its expected duration.

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


def hourly(cfg: RunConfig) -> float:
    """Dollars per hour while the run's two VMs exist: the machines, their data disks and boot disks."""
    machine = MACHINE_HOURLY.get(cfg.machine, 0.30)
    disks = (cfg.disk_gb * DISK_MONTHLY.get(cfg.disk_type, 0.10) + BOOT_GB * 0.10) / HOURS_PER_MONTH
    return round(2 * (machine + disks), 4)


def estimate(cfg: RunConfig, root=None) -> dict:
    """Minutes and dollars for a cloud run of `cfg`. The minutes are the sum of the run's planned steps
    (bench/steps.py: measured in an earlier run under `root` where there is one, else rules of thumb)."""
    from bench import steps
    seconds = sum(step['expect'] for step in steps.plan(cfg, publish=False, root=root))
    minutes = seconds / 60
    return {'minutes': round(minutes, 1), 'dollars': round(hourly(cfg) * minutes / 60, 2),
            'vcpus': 2 * VCPUS.get(cfg.machine, 8), 'machine_known': cfg.machine in MACHINE_HOURLY,
            'note': 'approximate list prices for us-central1; a guide, not billing'}
