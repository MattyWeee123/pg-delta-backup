# Large live HammerDB backup benchmark

`python -m bench.mega` coordinates three **existing, disposable GCE Linux VMs**:
source PostgreSQL, destination backups, and a separate HammerDB driver. It reuses
`bench.gcp.VM`, the repository's HammerDB Tcl scripts/jobs-database analysis, and
`benchmarks.resources.ResourceSampler`. The current engine runs directly against
the live source; there is no full source capture before pgdelta transfers.

This runner does not provision or delete VMs. No real benchmark has been run for
this change. Run the small acceptance configuration before the large matrix.

## Prepare hosts

Use the existing [GCP infrastructure](../infra/gcp/README.md) or your usual GCE
provisioning. Use dedicated hosts, not a shared database or an existing dataset.
The controller needs Python 3.11+, `gcloud`, an authenticated project, and SSH/SCP
access as an **unprivileged** account with passwordless sudo on all three hosts.
The same account owns the disposable PostgreSQL clusters. Install on all hosts:

```sh
sudo apt-get update
sudo apt-get install -y postgresql-17 postgresql-client-17 python3 nftables iproute2 iputils-ping
```

Debian 13 supplies PostgreSQL 17, as used by the existing cloud runner. On the
driver also install Docker from your approved package source and grant the SSH
account access to it. Stop/disable the distribution's default PostgreSQL cluster
on these dedicated hosts so it does not compete with the experiment.

Mount a dedicated data disk at `/data` and make it writable by the SSH account.
The runner **does not format disks**. Give each database host at least **4 times
the actual cluster size plus WAL/scratch headroom**. The source retains an immutable
changed snapshot plus a fresh running copy; the destination retains B plus the
output and, for native incremental, its increment. WAL grows under load; provision
for the longest backup and the churn phase. The driver needs space for the image
and evidence. Warehouses are a sizing input, not a byte guarantee: 1,000 is intended
as a roughly 100 GB starting point; record actual size and size/RAM ratio.

Allow private **driver → source TCP 5432**, **destination → source TCP 5432**, and
**source → destination TCP 7000**, plus SSH/control access. Do not expose the
benchmark ports publicly. The runner's synthetic source uses address-restricted
trust authentication for those hosts; no production credentials are used. Source
and destination must have distinct internal IPv4 addresses. Synchronize clocks
(e.g. the normal GCE time service). Cross-host workload alignment is bounded by
clock synchronization; the run retains a coarse clock-offset check.

The runner replaces the root traffic scheduler on the two database hosts when a
link cap is requested, and restores the default afterward. The cap matches only
the backup peer's address; HammerDB and SSH traffic use the uncapped class. Do not
use a host with a traffic scheduler you need to preserve. This is a bandwidth cap,
not added latency; RTT is observed with ping. Calibrate achievable link/disk rates
separately with the existing network lab tools before interpreting utilization.

Pin HammerDB on the driver:

```sh
docker pull tpcorg/hammerdb:latest
docker image inspect tpcorg/hammerdb:latest --format '{{index .RepoDigests 0}}'
```

Copy that complete digest into `hammerdb_image` in both configuration files. Also
edit VM names/zones. The placeholder deliberately fails validation. Keep personal
infrastructure configuration and raw evidence out of public commits.

## Small acceptance run, then scale

From the checkout containing this change:

```sh
# Configuration validation and randomized trial plan only. No cloud calls.
python3 -m bench.mega --config bench/configs/mega-small.json

# Explicitly run the small functional matrix on the existing hosts.
python3 -m bench.mega --config bench/configs/mega-small.json \
  --output runs/mega-small --execute
```

The small example runs all four backup methods once, each paired with a no-backup
control, at 100 Mbit/s. It has eight two-minute measurement windows plus ramp-up,
fixture build/churn, copy preparation, restore checks and collection. It is a
functional acceptance run, not a statistically significant performance result.
A very short backup may yield insufficient 10-second TPM samples; the application
metric then explicitly remains unavailable. A failed backup, missing workload
window, failed restore, failed observer or failed evidence retrieval rejects the run.

Inspect `runs/mega-small/report.md` and `results.json`. Acceptance is
`PASS_RESTORE_SMOKE`, successful trials for every requested method, and no collection
errors. Above ten warehouses the runner refuses execution unless a successful small
run covers every requested method and has the **same exact benchmark/engine source
fingerprint**:

```sh
# Review the plan first; edit size/load/duration/links for your budget.
python3 -m bench.mega --config bench/configs/mega-large.json
python3 -m bench.mega --config bench/configs/mega-large.json \
  --validated-run runs/mega-small/results.json \
  --output runs/mega-large --execute
```

The large example is deliberately explicit: 1,000 warehouses, 16 VUs, one
1-Gbit/s condition, one excluded warm-up and three measured repetitions per method.
It runs **32 workload trials with hour-long measurement windows**, plus preparation,
so budget more than 32 hours of host lifetime. Reduce the initial matrix before
expanding it. A backup must finish within its HammerDB measurement window; increase
`duration` (minutes) and `timeout` (seconds) for larger/slower cases. `timeout` also
bounds schema build, individual commands and workload waits. At 100 Mbit/s a 100 GB
full backup alone needs over two hours, so the example's one-hour window is unsuitable.

Use a new `name` and output directory for every run. Each host's experiment root is
`/data/pgd-mega-<name>`; existing roots are refused. A host lock prevents concurrent
mega runs. For additional sizes, link rates, basis churn or VU levels, copy the
configuration and give it a new name. Record negative results as well as wins;
large size alone does not establish a speedup. Keep source/destination hardware,
PostgreSQL/HammerDB versions and checkpoint policy comparable across runs.

## What happens and what is measured

1. Build TPROC-C, take and verify immutable basis B, apply a fixed-duration HammerDB
   churn workload, then freeze a clean source snapshot. These are fixture costs.
2. For **every** control and backup trial, copy that same frozen source into a fresh
   running cluster. In-place pgdelta receives a fresh physical copy of B. Preparation
   is recorded separately; no reflinks or hard links are used for these copies.
3. Optionally drop database-host caches **before** workload ramp-up (`cache=cold`).
   The active workload then warms its own working set. No cache drop occurs during
   the measured workload. Both pgdelta modes start their receiver with basis
   prefetch disabled, so its read of B is charged inside backup time.
4. Run randomized backup/control pairs. Full and incremental use streamed WAL and
   SHA-256 manifests; incremental includes input verification, `pg_combinebackup
   --copy`, and final verification. Pgdelta uses live backup mode, BLAKE2b, configured
   hash workers/hash-ahead, and worker fsync; in-place verifies as written. No WAL
   summary fast path is enabled for pgdelta; native incremental requires summaries.
5. Restore every output in isolation, without upstream streaming or external WAL,
   check all nine TPROC-C tables exist, and run `pg_amcheck --all`.

| Data | Scope / interpretation |
|---|---|
| Source/destination backup bytes and packets | Named kernel nftables output counters, restricted to backup peer and TCP 5432/7000. Includes both basebackup/WAL connections. Counts IP-layer traffic, not Ethernet wire bytes; kernel offloads can affect accounting. Add source TX + destination TX once; never add RX again. |
| Network throughput series | Observed counter deltas at a requested 100 ms cadence; actual timestamps retained. Raw whole-interface counters and TCP socket/SNMP counters retained for retransmission/congestion diagnosis. |
| Verified-backup total time | First capture start through final package verification. Pgdelta sender completion includes receiver digest verification/sync; native includes combine/verification. Measured worker commands exclude SSH launch; gaps between separate phases remain in the total. Receiver readiness/setup is separate. |
| Restore check / preparation | Backup plus restore-check time reported separately; excludes waiting for the workload to finish. Fixture source reset/basis copy time is separate. Initial schema build, basis capture and churn logs retained. |
| Source, reverse and total traffic savings; speedup | Per-link medians relative to full backup, with sample count/min/max. Effective refresh MB/s uses measured database size/time. No significance assertion. |
| Application impact | Matched-offset/duration TPM samples against a paired control, throughput loss, post-backup recovery window, aggregate NOPM/TPM. Per-procedure latency percentiles are **whole-run**, including ramp-up, not backup-window p95. |
| CPU/memory/I/O | Source PostgreSQL process tree and backup command trees: CPU-seconds, sampled peak summed RSS and process I/O. Both database hosts: raw CPU/memory/disk/vm/pressure samples. Shared RSS pages can be counted more than once; short-lived processes may be missed. |
| PostgreSQL | Before/after `pg_stat_database`, `pg_stat_wal`, `pg_stat_checkpointer`, `pg_stat_io`, replication-slot snapshots. These include concurrent workload activity. |
| Provenance/storage | Source-code SHA-256, complete config/order/seed, image digest, PG version, machine/disks/CPU/RAM/kernel, RTT, database bytes, output apparent/allocated size, commands/logs and verdicts. |

`results.json` and `report.md` summarize the run. Each trial retains workload logs
and HammerDB's jobs database. Three `*-evidence.tgz` archives retain worker raw
samples, phase timings/process resources, engine sender/receiver statistics and
logs. Unavailable metrics are null with scope/reason, not zero. Failed trials stop
the matrix and retain failure evidence; warmups/failures do not enter performance
medians. Complete raw records are needed to explain where the largest benefit occurs.

**Correctness scope:** `PASS_RESTORE_SMOKE` proves package startup, schema presence
and structural checks. It does not prove exact logical state at the active backup's
recovery cutoff. The existing exact-cutoff oracle is pgbench-specific; a TPROC-C
logical recovery oracle remains additional work. TPROC-C scores are not audited
TPC-C/tpmC results.

## Cleanup and development checks

Trial source/output copies and owned workload containers are removed after stopping
them; immutable B/target and worker evidence remain for diagnosis. VMs and disks
remain billable until **you delete them** with your usual infrastructure teardown.
On interruption, inspect retained logs/processes before deleting an experiment
root or recovering a stale `/data/pgd-mega.lock`. The existing `bench cleanup` only
handles VMs created/labeled by that provisioning workflow; it is not a teardown for
arbitrary existing hosts passed here.

Developer checks use mocked transports and never launch PostgreSQL/Docker/GCP:

```sh
python3 -m unittest discover -s tests -p test_mega.py -v
```
