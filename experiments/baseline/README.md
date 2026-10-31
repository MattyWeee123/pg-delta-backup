# The team's baseline: one command, two VMs, every side measured

`experiments/baseline/run.sh` reproduces the project's main setting on GCP and measures, second by second, what each machine's link, disk and CPU are doing. Use it to see where the time goes, and to test a change against the same baseline. It is also how [EXP-90](../../docs/experiments/EXP-90-where-hashing-time-goes.md) was run.

## What it runs

**The setting:** an HA pair beyond memory.
- Two `n2d-highcpu-8` VMs (8 vCPUs, 8 GB of memory), in two zones of one region.
- Each has a 150 GB balanced persistent disk at `/data`.
- PostgreSQL 17.11 with an 18 GB pgbench database (scale 600), so the database doesn't fit in memory.

**The old copy:** a base backup taken before a change. Two changes, each tried separately:
- `hot`: the same 1% of rows updated again and again (few pages change);
- `uniform`: 1% of rows updated, scattered (about 28% of the pages change).

**The arms, each from a fresh copy of the old copy, with both VMs' caches dropped:**

| Arm | What runs |
|---|---|
| `full` | `pg_basebackup` into a new directory: the standard re-clone |
| `hash` | pgdelta hashing both copies: works on any old copy |
| `hashw` | the same, with the receiver's file syncs on a background thread (`--fsync worker`) |
| `fast8wf2` | pgdelta's WAL-summary fast path, with its defaults |
| `X_1g` | arm X on a link shaped to 1 Gbit/s on both VMs (`infra/gcp/throttle.sh`) |

Every pgdelta copy is started and compared with the source (row count and the sum of balances).

**Before the arms, a profile of the hardware:**
- each VM's hash rates per 8 KiB page (BLAKE2b, BLAKE3, SHA-256);
- each disk's limits for reads and writes alone and both at once (`fio`);
- the link's rate, unshaped and at 1 Gbit/s (`iperf3`).

**During every run:** `experiments/network/util_series.py` samples both VMs every 0.1 s. It records bytes sent and received, disk reads and writes, CPU, and the CPU time of each pgdelta, `pg_basebackup` and walsender thread.

## Before you start

1. **Google Cloud access:**
   - the `gcloud` CLI, signed in with your own account (`gcloud auth login`);
   - access to the project `postgres-delta-backup`.
2. **On your machine:** Python 3.11 or later and `ssh`.
3. **For the figures:**

```bash
python3 -m venv .venv && .venv/bin/pip install -r analysis/requirements.txt
```

**Quota:** the project's N2D quota is 16 vCPUs per region, and one set uses all 16. So only one baseline can run in a region at a time. The script tries several regions in turn (`ZONES`).

**Cost:** two 8-vCPU VMs and two 150 GB disks, for about 2.5 hours. They are labelled `purpose=pg-delta-benchmark` and deleted at the end.

## Running it

From the repository's root:

```bash
SMOKE_ONLY=1 experiments/baseline/run.sh
```

That runs a smoke only: a 0.3 GB database, every arm once, about 40 minutes. Run it first to check your setup.

```bash
experiments/baseline/run.sh
```

That runs the full baseline: 16 runs at 18 GB, about 2.5 hours.

**Settings, by environment variable:**

| Variable | Default | What it sets |
|---|---|---|
| `PAIR` | `bl-` and your user name | The VMs' names: `pgd-PAIR-source`, `pgd-PAIR-dest` |
| `NAME` | `baseline-` and the time | The run's name |
| `RESULTS` | `results/baseline/NAME` | Where results go on your machine |
| `ARMS`, `PATTERNS` | the table above | What runs |
| `REPEATS` | `1` | Runs per arm and change |
| `SCALE` | `600` | pgbench's scale: 600 is about 18 GB |
| `KEEP` | `0` | `1` keeps the VMs for a look |

**If it is interrupted,** delete your VMs:

```bash
PAIR=bl-yourname infra/gcp/teardown.sh
```

## What you get

`RESULTS/summary.txt` has one line per run: its time, its slowest resource, and how close it came to that resource's limit. Below those lines are EXP-90's scored predictions (D1 to D4).

**`RESULTS/figures/baseline-timelines.svg`:** each run, second by second.
- The black line is what the source sends over the link.
- Blue is the source's disk reads, light blue dest's reads, and red dest's writes.
- A run that keeps its slowest resource busy shows a flat line at that resource's limit. Gaps show waiting.

**`RESULTS/figures/baseline-bounds.svg`:** each run (grey bar) against the time each resource needed for its work at full speed (marks).
- The run can't be shorter than its largest mark.
- The label, the run over that mark, is the room left for overlapping work: 1.00 means nothing is left.

## Testing a change

The script deploys your working tree's tracked files (`git ls-files`), so commit your change, or at least `git add` new files.
1. Run the baseline on `main`.
2. Run it again on your branch.
3. Compare the times and the bounds figure.

**Noise:** two runs of the same arm in one session differ by up to about 2.2% (`docs/research/noise-floor.csv`). A smaller difference needs more repeats (`REPEATS=2`) or a second session before it means anything.

## Where things are

| What | File |
|---|---|
| The lab (each run) | [experiments/network/lab_exp64.sh](../network/lab_exp64.sh) |
| The sampler | [experiments/network/util_series.py](../network/util_series.py) |
| The disk test | [experiments/network/fio_mixed.sh](../network/fio_mixed.sh) |
| The hash test | [experiments/network/hashbench.py](../network/hashbench.py) |
| The link test | [experiments/network/link_probe.py](../network/link_probe.py) |
| The analysis | [analysis/exp90.py](../../analysis/exp90.py) |
| The VMs | [infra/gcp/](../../infra/gcp/) |
