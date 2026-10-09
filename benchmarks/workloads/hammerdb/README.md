# HammerDB TPROC-C workload

Status: Sam’s workload and documented exploratory runs are retained in the consolidated benchmark.
The figures below predate consolidation; they are single runs on one laptop, not a baseline.
The current backup runner and Docker build are documented in [the shared runbook](../../README.md#running-the-consolidated-cli).

TPROC-C is HammerDB's TPC-C derived OLTP workload: nine tables and the five TPC-C transactions
(`neword`, `payment`, `delivery`, `ostat`, `slev`) as PostgreSQL stored procedures. It reports
**NOPM** (new orders per minute) and **TPM**.

TPROC-C output is not an audited TPC-C result. Never publish it as `tpmC`, and never compare it
against a published TPC-C number. HammerDB is explicit about this.

## Why this workload

`benchmarks/README.md` requires application degradation measured against a matching no-backup run.
That needs a workload with a stable transaction mix and a throughput figure. pgbench supports both a built-in transaction mix and custom SQL scripts; we use it for controlled
recovery tests. TPROC-C provides a richer order-processing mix and a per-10-second throughput
counter for the application-performance experiments. Neither workload is mandated by the sponsor.

## Prerequisites

The fixture from `benchmarks/fixture/docker-compose.yml`, running and healthy, and the official
image `tpcorg/hammerdb` (CLI v6.0 at time of writing).

```sh
export POSTGRES_PASSWORD='choose-a-throwaway-password'
docker compose -f benchmarks/fixture/docker-compose.yml up -d
docker pull tpcorg/hammerdb:latest
export HAMMERDB_IMAGE=$(docker image inspect tpcorg/hammerdb:latest --format '{{index .RepoDigests 0}}')
```

Credentials come from the environment. The scripts refuse to run with an empty `PGBENCH_PASS`
rather than silently falling back to a default password.

## Build the schema

10 warehouses produces roughly a 1 GiB cluster, the "small" scale in `benchmarks/README.md`. Each
further warehouse adds roughly 100 MB. On Git Bash for Windows, prefix with `MSYS_NO_PATHCONV=1`.

```sh
docker run --rm --network fixture_default \
  -e PGBENCH_HOST=postgres -e PGBENCH_PASS="$POSTGRES_PASSWORD" \
  -e TPCC_WAREHOUSES=10 -e TPCC_BUILD_VU=8 \
  -v "$PWD/benchmarks/workloads/hammerdb":/hdb:ro \
  "$HAMMERDB_IMAGE" ./hammerdbcli auto /hdb/build_schema.tcl
```

Build only against the disposable fixture. These scripts call HammerDB’s `buildschema`; they do
not implement database reset/recreation. Use a fresh fixture for repeated comparable schema builds.
Keep the resolved HammerDB image digest and actual CLI version with each run; `latest` is only
the initial image resolver. The default Compose network is `fixture_default`; an explicit project
name changes it, so use that project’s network in the commands.

## Run a timed test

```sh
docker run -d --name hdb-run --network fixture_default \
  -e PGBENCH_HOST=postgres -e PGBENCH_PASS="$POSTGRES_PASSWORD" \
  -e TPCC_VU=8 -e TPCC_RAMPUP=1 -e TPCC_DURATION=2 \
  -v "$PWD/benchmarks/workloads/hammerdb":/hdb:ro \
  "$HAMMERDB_IMAGE" ./hammerdbcli auto /hdb/run_timed.tcl

docker logs -f hdb-run | grep -E "TEST RESULT|PostgreSQL tpm"
```

## Measuring degradation during a backup

Run the workload twice under identical settings: once alone for the denominator, once with
`benchmarks/benchmark.py` executing inside the measurement window. Start the backup only after
`Rampup complete` appears in the log, or it lands in the ramp-up and is excluded from the result.

**Read the timestamped series, not just the aggregate.** `docker logs -t` prefixes each TPM
sample with a timestamp; correlate those against the backup's start and end. A 30-second backup
inside a 120-second window dilutes roughly fourfold in the aggregate NOPM, which can mask a short disturbance. The examples below report aggregate loss and much deeper
10-second sample troughs; those samples are not instantaneous measurements or controlled causal estimates.

Resolve `PG_IMAGE` using the shared runbook, then build the shared CLI target before measurement:

```sh
docker build --target cli --build-arg PG_IMAGE="$PG_IMAGE" -f benchmarks/Dockerfile -t pg-delta-runner .
```

## Measured example, October 9 2026

10 warehouses, 1022 MB cluster, 8 virtual users, 1 minute ramp-up, 2 minute measurement.

| Run | NOPM | TPM | Aggregate NOPM loss | Capture + verification share of window |
|---|---|---|---|---|
| A: no backup | 13,470 | 31,337 | denominator | — |
| B: backup starting 58 s into the window | 8,856 | 20,662 | 34.3% | ~31% |
| C: backup starting at ramp-up completion | 4,704 | 10,920 | 65.1% | ~61% |

Backup measurements for the same cluster, both `PASS_MANIFEST_ONLY`:

| Run | Capture | Verification | Output bytes |
|---|---|---|---|
| B | 32.3 s | 4.7 s | 1,439,481,725 |
| C | 60.3 s | 12.5 s | 1,535,328,122 |

Runs B and C used different start positions, and their backup durations also differed. These
single runs do not isolate the effect of start position, caching, CPU, I/O or WAL. **Aggregate
NOPM alone does not identify degradation during backup**: retain raw timestamped observations
and compare matched windows, including recovery after backup completion.

Run C's TPM series, against a run A median of roughly 30,000:

```text
20:19:22  17,628
20:19:32  11,514   <- pg_basebackup starts ~20:19:30
20:19:42  10,176
20:19:52     396
20:20:02     966
20:20:12   1,458
20:20:22     606   <- sustained ~40 s at 1-5% of baseline
20:20:32   9,042   <- capture ends ~20:20:30
20:20:42  19,746   <- verification ends ~20:20:43
20:20:52  25,908
20:21:02  38,526   <- recovered
```

Run B showed the same shape more briefly, bottoming at 588 TPM. In both runs the collapse tracks
the backup window and recovery begins when capture ends, which is consistent with backup I/O
contention being the dominant effect at this scale and topology. That is a hypothesis these runs
support, not a conclusion they establish: nothing here isolates I/O from CPU or WAL volume.

Capture time also grew with concurrent write load (32.3 s to 60.3 s for the same cluster), and
output exceeded the 1.02 GB cluster size in both runs because `--wal-method=stream` bundles WAL
generated during the capture.

## Caveats that make these numbers unusable as a baseline

- **Single run each.** `benchmarks/README.md` requires at least three repetitions after a warm-up,
  with medians and ranges. Run A's own samples ranged from 11,808 to 45,258 TPM, so the variance
  is larger than many effects worth measuring.
- **No resource isolation.** The database, the 8 HammerDB virtual users, and the backup container
  shared one laptop's 12 CPUs and a single SSD. The backup also wrote to the same disk as PGDATA.
  This measures contention on a shared host, not a representative deployment.
- **`--checkpoint fast`** forces an immediate checkpoint at backup start, concentrating I/O.
  `--checkpoint spread` would smear it and likely show a shallower, longer dip.
- **Workload calibration.** Record warehouse count and virtual users separately. The scripts
  enable `pg_allwarehouse`, which changes the default home-warehouse behavior. Apply
  [HammerDB’s sizing guidance](https://www.hammerdb.com/docs/ch03s07.html) with the actual driver
  settings; test for lock contention rather than assuming a universal warehouse/user ratio.
- **No latency figures.** `benchmarks/README.md` wants p95 change as well as throughput. HammerDB
  writes a time profile to `/tmp/hdbxtprofile.log` inside the container, which these runs did not
  extract.
- **Nothing was restored.** Gate G3 remains unmet; the backup was verified against its manifest
  only.

## Preserve observations before removing the workload container

```sh
mkdir -p results/hammerdb
docker logs --timestamps hdb-run > results/hammerdb/workload.log 2>&1
docker cp hdb-run:/tmp/hdbxtprofile.log results/hammerdb/hdbxtprofile.log
```

Retain the profile file if produced by the selected HammerDB version; a missing profile means
latency is unavailable, not zero. Do not call an arbitrary time-profile statistic p95 without
checking that version’s format and retaining enough raw observations. Keep run settings,
image digests, backup result JSON and observation boundaries with these files. Automated matched
window analysis and an exact recovery checker for the HammerDB schema remain to be implemented.
