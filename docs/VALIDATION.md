# Validation performed

Latest update: October 9, 2026. The controlled performance baseline remains incomplete.

## Native PostgreSQL validation added October 9

The complete [workflow run 37991303022](https://github.com/MattyWeee123/pg-delta-backup/actions/runs/37991303022) passed for PR head `633782b84a17b8cd9d49595a109a11c6c71fed6e`, including tests, all database trials, evidence upload and report generation. Actions checked out synthetic merge commit `285a896075e742c0d8acfcd5fc9677e370d5fa28`, which is the commit recorded in results.json; this does not mean the PR was merged. Its `native-baseline-evidence` artifact contains the raw run records, commands, manifests, transaction logs and restore-state digests. A durable [report and raw evidence export](../benchmarks/evidence/2026-10-09-native-smoke/report.md) are checked into this branch; the raw ZIP contains all 1,455 evidence files and their SHA-256 inventory. This preserves the evidence beyond the workflow artifact's three-day retention period.

The [native smoke runner](../benchmarks/NATIVE.md) executed 15 real PostgreSQL 17.11 trials: full and incremental backups with quiescent and active source workloads, plus a no-backup active control, repeated three times in randomized order. Twenty unit tests passed. All 15 database trials passed in [run 37990014663](https://github.com/MattyWeee123/pg-delta-backup/actions/runs/37990014663); that workflow subsequently failed when uploading container-owned server logs. The upload permissions were corrected in commit `633782b` and a new complete run was requested. The earlier attempt [37989866519](https://github.com/MattyWeee123/pg-delta-backup/actions/runs/37989866519) stopped before database execution because Docker Hub rate-limited the shared runner; the workflow now resolves the public official-image mirror to an immutable digest.

These are real local backup/restore smoke measurements on a hosted Linux runner with a two-CPU, 2 GiB container, 20,000 account rows and a measured database size of 14,677,683 bytes. They are not the proposed 1/10/50 GiB matrix. No new GCP resources were provisioned.

- Six quiescent backups restored using only their own package and matched full ordered fixture data and supported schema against independently exported source state.
- Six active backups passed package-only startup and balance conservation; six additional PITR-assisted restores matched the source at a named recovery target using separately accounted archived WAL.
- Corrupt data, missing required WAL and an outdated basis backup were rejected by the appropriate checker.
- Active trials reconciled successful external pgbench transaction logs against source ledger counts and rejected failed/skipped work.

The active package's exact logical contents at its own cutoff are not yet certified. Full correctness of arbitrary schemas, tablespaces, extensions and failover is outside this fixture's scope. Local durations and file sizes are recorded, but actual network bytes, resource observers, calibrated workload capacity, matched-window application degradation, rsync/custom comparisons and controlled scale runs remain pending. Do not use these smoke timings to approve an optimization. See [the release gate and sequence](../benchmarks/BASELINE-GATE.md).

## Historical starter validation: September 30, 2026

The documentation was subsequently revised using the supplied sponsor-chat screenshots.
CORRECTNESS.md describes a proposed oracle and recovery contract, not a tested implementation.
No new implementation or database validation is claimed by that documentation update.

## Executed locally

- Python 3.12.14 standard-library experiment.
- Eleven unittest cases passed, including 100 seeded randomized edit trials and multiple edit-shape/chunk-size combinations.
- Exact reconstruction checked for all nine reported demo scenarios.
- Corrupt literal data, changed reused basis bytes, truncated transfer, invalid ranges, output overrun and wrong target digest are rejected by the educational reconstruction function.
- Raw demo output is saved in [example-results.json](../experiments/example-results.json).

## What the example shows

An 8 MiB deterministic random byte string is divided into 1,024 synthetic 8 KiB pages.
The experiment changes one byte in each selected page.
This is not a PostgreSQL workload.

| Changed synthetic pages | Fixed chunk | Literal bytes | Estimated signatures + instructions + literals | Estimated reduction versus raw full bytes |
|---|---|---|---|---|
| 0 / 1,024 | 8 KiB | 0 | 99,328 | 98.82% |
| 10 / 1,024 | 8 KiB | 81,920 | 181,168 | 97.84% |
| 10 / 1,024 | 64 KiB | 655,360 | 667,696 | 92.04% |
| 1,024 / 1,024 | 8 KiB | 8,388,608 | 8,479,744 | -1.09% |

The signature/instruction format is an illustrative cost model, not an implemented wire protocol.
Session metadata, file inventories, transport framing, WAL, retries and CPU/disk/capture costs are excluded.
These percentages must not be presented as database backup or network benchmark results.

The one-byte prefix insertion counterexample loses fixed-chunk reuse on this random data.
That demonstrates a reason to investigate rolling matching or CDC for shifted files, not a conclusion about PostgreSQL relation files.

## Benchmark runner v0.1, October 9, 2026

Added `benchmarks/benchmark.py` and `tests/test_benchmark.py`.

Executed locally on Windows 11 with Python 3.12.7:

- `python -m unittest discover -s tests` -- 43 tests pass (11 pre-existing, 32 new).
- `python -m py_compile benchmarks/benchmark.py` -- clean.
- `python benchmarks/benchmark.py --help` -- CLI surface renders.

The runner's tests use stub executables that imitate `pg_basebackup` and `pg_verifybackup`, so
they establish the runner's own control flow only: timing capture, log capture, exit-code and
timeout handling, directory byte counting, status and exit-code mapping, path-overlap refusal,
the exact ten-key record shape, and that no password reaches `argv`. They establish **nothing**
about PostgreSQL.

### Fixture and runner executed against a real cluster

Docker Desktop 28.1.1 was started and `benchmarks/fixture/docker-compose.yml` brought up
successfully. The container reached its healthcheck and reported PostgreSQL 17.11
(Debian 17.11-1.pgdg13+2). Every intended setting took effect:

```text
block_size      = 8192
data_checksums  = on
hba_file        = /etc/postgresql/pg_hba.conf
max_wal_senders = 10
summarize_wal   = on
wal_level       = replica
```

The `hba_file` override and the replication lines work: `pg_basebackup --wal-method=stream`
connected over TCP from a second container using `.pgpass`.

Data: `pgbench -i -s 10` into a `benchdb` database, 1,000,000 `pgbench_accounts` rows, 179 MB
total cluster size. The runner was executed inside a `postgres:17` container on the fixture
network, so the measured path is Linux with the real 17.11 client binaries.

Three runs, all behaving as designed:

| Run | Backup | Verification | Output bytes | Status | Exit |
|---|---|---|---|---|---|
| Baseline | exit 0, 3.687 s | exit 0, 0.117 s | 204,812,187 | `PASS_MANIFEST_ONLY` | 0 |
| Negative control: one byte flipped in a relation file after capture | exit 0, 6.963 s | exit 1, 0.115 s | 204,812,187 | `FAIL_VERIFY` | 4 |
| Negative control: unreachable port 5999 | exit 1, 0.032 s | not attempted | null | `FAIL_BACKUP` | 3 |

The real tools produced the diagnostics, not the runner:

```text
pg_verifybackup: error: checksum mismatch for file "base/16388/16409"
pg_basebackup: error: connection to server at "postgres" (172.19.0.2), port 5999 failed: Connection refused
```

The corrupted run correctly recorded `integrity_verified: false` and
`time_to_verified_seconds: null` rather than a duration, and the failed-backup run skipped
verification entirely and wrote `null` for both `verification` and `backup_file_bytes`.

Output is 204 MB against a 179 MB cluster because `--wal-method=stream` includes the WAL
captured during the backup.

### HammerDB TPROC-C workload executed, October 9 2026

Added `benchmarks/workloads/hammerdb/` and `benchmarks/fixture/runner.Dockerfile`.

HammerDB CLI v6.0 from the official `tpcorg/hammerdb` image was run against the fixture. A
TPROC-C schema built successfully at 2 warehouses and then at 10 warehouses, producing a 1022 MB
cluster: nine tables, 645,031 order lines at 2 warehouses, and the five TPC-C stored procedures
(`neword`, `payment`, `delivery`, `ostat`, `slev`).

Three timed runs at 10 warehouses, 8 virtual users, 1 minute ramp-up, 2 minute measurement:

| Run | NOPM | TPM | Backup during window |
|---|---|---|---|
| A | 13,470 | 31,337 | none |
| B | 8,856 | 20,662 | 32.3 s capture, started 58 s in |
| C | 4,704 | 10,920 | 60.3 s capture, started at ramp-up end |

Both backup runs passed verification (`PASS_MANIFEST_ONLY`). The timestamped transaction-counter
series shows throughput collapsing to between 396 and 1,458 TPM for roughly 40 seconds during
run C's capture, against a run A median near 30,000, with recovery beginning when capture ended.

The runner image fixed the earlier provenance gap: containerized runs now record
`git_commit: 8d67adf5a32ae178baf9bdaefe8f14bd3366d535` instead of `unavailable`.

The parameterized scripts were verified to refuse an empty `PGBENCH_PASS` rather than falling back
to a default password.

These are **single runs on one laptop and are not a baseline.** All three runs shared 12 CPUs and
one SSD between the database, eight virtual users and the backup container; the backup wrote to the
same disk as PGDATA. Run A's own samples ranged from 11,808 to 45,258 TPM, so run-to-run variance
exceeds many effects worth measuring. TPROC-C figures are not audited TPC-C results and must never
be published as `tpmC`. No latency percentiles were extracted. Full caveats are in
`benchmarks/workloads/hammerdb/README.md`.

### Still not established by this change

- **No restore test.** Gate G3 is unmet. `pg_verifybackup` passing is why the status is
  `PASS_MANIFEST_ONLY`; no cluster was booted from any of these backups.
- **No network measurement.** `network_bytes` is null in all three runs. Both containers shared a
  Docker bridge network; no constrained link was measured.
- **No workload during capture.** The cluster was idle. Gate G4 is unmet.
- **Single runs, no repetitions.** `benchmarks/README.md` requires at least three repetitions
  after a warm-up before any result is reported. The durations above are one sample each on one
  host and must not be cited as a baseline.
- **`git_commit` was unavailable** in the containerized runs because `git` is not installed in the
  `postgres:17` image. The field degraded to a reason string as designed, but provenance for a
  container run is currently incomplete.

## Not executed at the September 30 checkpoint

- PostgreSQL full or incremental backup, pg_combinebackup, pg_verifybackup, or database restore.
- AlloyDB Omni compatibility or HA deployment.
- Source-side helper, live-source consistency, real transport, fsync/atomic publication or resource-bound implementation.
- Actual network bytes, CPU overhead, disk I/O or end-to-end performance.

Docker is installed on this Windows host, but the Docker daemon was not reachable during inspection.
No cloud resources were provisioned.
At that checkpoint the baseline runbook remained an unexecuted first team task. The October 9 section above supersedes that status for native smoke integration only.

## Publication status

The starter is published at [MattyWeee123/pg-delta-backup](https://github.com/MattyWeee123/pg-delta-backup) on the main branch.
The repository is public by the project lead's choice.
The local repository tracks origin/main.
GitHub Actions results are available on the [Actions page](https://github.com/MattyWeee123/pg-delta-backup/actions).
The [publication workflow run](https://github.com/MattyWeee123/pg-delta-backup/actions/runs/36779762833) passed the experiment tests and demo on GitHub's Ubuntu runner.
The backlog remains draft issue text; GitHub issues and teammate write-access invitations have not been created.
