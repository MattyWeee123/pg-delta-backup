# Validation performed

Latest update: October 29, 2026. The controlled performance baseline remains incomplete.

## Live pgdelta benchmark integration, October 29, 2026

The shared registry now includes `pg_delta_live` and
`pg_delta_live_summaries`. They prepare a separate old copy and refresh it
directly from live PGDATA, without staging a new full backup. Their
`PASS_ENGINE_ONLY` verdict validates engine completion evidence and packaged
WAL, not a PostgreSQL manifest or logical recovery. The native suite adds the
independent exact-stop-LSN recovery comparison. Default native methods remain
unchanged. [LIVE.md](../benchmarks/LIVE.md) contains local, GCP working-tree
export and Docker/HammerDB commands; the
[implementation note](specs/2026-10-29-live-benchmark-adapter.md) maps the files.

Validation on the same local PostgreSQL 17.11/Python 3.14.8 environment:

- The final **nine-trial comparison passed** at 20,000 account rows, one
  repetition, 12 seconds, 100 offered scripts/second and one live worker:
  active/quiescent full, native incremental, live hashing and live summaries,
  plus no-backup control. All four active exact-cutoff verdicts passed;
  summary use was confirmed in both summary trials. Evidence is retained
  locally under `results/live-comparison-2026-10-29/` (git-ignored). These
  timings are descriptive smoke measurements, not an optimization claim.
- The full repository suite passed: **1,680 tests, 256 skipped**, run at nice
  priority 0. The skips cover unavailable optional dependencies/environments.
  The first run inherited this app's nice -8 priority and exposed an existing
  CPU-reference test assumption; priority was normalized for the rerun,
  without changing the experiment or its test expectation.
- All **11 live adapter tests passed with PostgreSQL available**: both live
  methods matched independent references under load, summary use was
  confirmed, a public CLI capture restored correctly, mismatched receipts
  and missing packaged WAL were rejected. Pure tests cover failure statuses,
  unsafe variants, source requirements, output aliases and conninfo escaping.
- All **13 active-cutoff tests passed with PostgreSQL available**, including
  native full/incremental recovery, lost-transfer/ledger fault injection,
  wrong-cutoff reference and missing-package-WAL controls.
- All **76 HammerDB tests** and **70 method/report tests** passed. The Docker
  stub exercises source-local commands, preparation before workload launch
  and bad live statistics. A newly discovered acceptance gap was fixed:
  failed/missing backup verification cannot produce an accepted degradation
  pair, even when its observation windows match.
- A working-tree archive including new untracked modules was extracted
  without `.git`; its benchmark/engine source hash matched the local tree.
  New provenance records distinguish actual code from a branch's last commit.
- All five Bash blocks in LIVE.md passed syntax checks. The workflow and both
  Compose YAML files parsed successfully; the runbook's local links resolve.

Live Docker/HammerDB execution and GCP execution were not performed locally.
The workflow now includes a live/native comparison and real live-adapter
fault tests, to run when this work is committed and submitted. No cloud
resources were created. This is correctness/measurement integration, not a
new controlled performance result or a TPROC-C recovery oracle.

## Native active-cutoff oracle, October 29, 2026

The native runner now checks active packages at the manifest's exact end LSN
against an independent B0 plus archived-WAL reference. Candidate replay has
archive fetching and upstream streaming disabled. Both replay positions and
full supported logical states must match. External pgbench operation IDs,
rollback probes, an in-flight transaction and a post-capture marker supplement
the comparison. New bundles use schema v4; historical results are not upgraded.
See [NATIVE.md](../benchmarks/NATIVE.md) for the endpoint semantics and commands.

Validated locally with PostgreSQL 17.11 (built from the official source in a
temporary directory), Python 3.14.8, Linux, as an unprivileged user:

- Five default-matrix trials at 20,000 account rows, one repetition, 8 seconds,
  100 offered scripts/second: active and quiescent full/incremental backups and
  the active no-backup control all passed. Both active exact-cutoff verdicts
  passed. This is correctness smoke evidence, not a performance baseline.
- The real recovery regression test passed for full and incremental backups.
  It rejected a fault-injected lost transfer and ledger entry that preserved
  total balance, a later-state reference at the wrong cutoff, and missing
  candidate WAL despite the reference archive still being available.
- The public CLI smoke passed both full/incremental restores and still
  rejected an unchecksummed incremental basis before capture. Its success
  contract remains `PASS_MANIFEST_ONLY`.
- Endpoint/log validation, reporting compatibility, resource instrumentation
  and benchmark CLI unit tests passed. The endpoint tests require exact replay
  equality and reject malformed ranges, wrong identities and unsupported
  multi-timeline manifests. Historical reports retain a missing/unchecked
  exact-cutoff verdict.

The active workload now includes rollback probes, so timings must not be pooled
with older workloads. Only the deterministic fixture's logged tables and
supported schema are covered; both restores trust PostgreSQL recovery. No
cloud, HammerDB, arbitrary-schema or production validation is implied.

## HammerDB degradation driver, October 17, 2026

Added `benchmarks/hammerdb_experiment.py`, its tests and `tests/stub_docker.py`, plus two fixes
to existing files: a Windows guard on the directory fsync in `wal_copy.py`, and a registry
fallback in the native baseline workflow after `public.ecr.aws` began returning
`toomanyrequests` on shared runners. That workflow failure was a registry quota, not a code
regression: the same workflow passed on October 9 and the resource metrics commit changed no
workflow or Dockerfile.

Executed locally: 133 unit tests pass, twice in succession without flakiness. They cover the
jobs-database readers, log parsing, window arithmetic, degradation guards, and orchestration
against a stub docker CLI. No PostgreSQL, HammerDB or Docker is required for them.

Executed against the real fixture, PostgreSQL 17.11, 10 warehouse TPROC-C schema of 1,231 MB,
8 virtual users, driven from WSL because the shared adapters rewrite container paths on a
Windows host:

| Run | Window | Backup span | Outcome |
|---|---|---|---|
| 2 minute | 123.8 s | 174.0 s | rejected by the window guard |
| 6 minute | 363.7 s | 398.5 s | rejected by the window guard |
| 11 minute | 674.5 s | 328.4 s | **accepted** |

The accepted pair measured a 71.4% throughput loss over aligned 328.4 s windows, 33 control
samples against 32 backup samples, with the control taken at the same offset from its own
ramp-up. The same data compared whole-window gives 57.6%, because 346 s of that window had no
backup running. Throughput remained 40.6% below the control across the 36 samples after capture
finished. Whole-run p95 rose from 57.9 ms to 154.0 ms. The backup passed verification with
capture 232.7 s, verification 95.6 s and 2,015,067,325 output bytes.

[Preserved pilot evidence](../benchmarks/evidence/2026-10-17-hammerdb-pilot/report.md) contains
the record, provenance, raw bundle with a checksum inventory, and the two rejected runs.

A separate observation: an otherwise identical 6 minute run measured a control of 8,708 tpm, while
one with a test suite running concurrently on the same host measured 4,318 tpm. Unrelated host
load halved the measurement.

These are single runs on a shared laptop and are **not a baseline**. No repetitions, no resource
isolation, no warm-up exclusion, no calibrated offered load. Latency is whole-run because
HammerDB records no time dimension for it. `network_bytes` remains null with a reason. Nothing
was restored: the status is `PASS_MANIFEST_ONLY`, gate G3 is unmet, and no TPROC-C recovery
oracle exists. TPROC-C results are not audited TPC-C and must never be published as `tpmC`.

## Consolidation validation, October 9

Sam’s commit `ff8b85e` is preserved in merge `f2d57d1`. Shared adapters and schema v2
were validated at PR head `09425e0` in [run 37994167624](https://github.com/MattyWeee123/pg-delta-backup/actions/runs/37994167624):
62 unit/configuration tests passed without skips, all 15 native matrix trials passed,
and the public CLI completed full and incremental captures followed by two successful
exact-data restores. Compose configuration validation also passed. The subsequently
added workflow step exercises the packaged Compose CLI and exports its evidence.
The current PR checks report that later step’s status.

[Preserved consolidation evidence](../benchmarks/evidence/2026-10-09-consolidated/report.md)
contains the raw records and checksum inventory. Backup records from both frontends
use schema v2; historical v1 artifacts below remain unchanged. HammerDB Tcl
configuration was checked with stub commands, not a real new HammerDB workload run.
Sam’s historical workload measurements remain separately attributed below.

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

## Sam’s historical v0.1 and HammerDB experiments, October 9, 2026

These are Sam’s reported local results from commit `ff8b85e`. The branch contained
summary tables and selected samples but no complete raw result bundles. They were
not independently reproduced on that laptop during consolidation.

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

### Limits of Sam’s experiments

- **No restore test.** Gate G3 is unmet. `pg_verifybackup` passing is why the status is
  `PASS_MANIFEST_ONLY`; no cluster was booted from any of these backups.
- **No network measurement.** `network_bytes` is null in all three runs. Both containers shared a
  Docker bridge network; no constrained link was measured.
- **Active workload was exercised** in the HammerDB runs above; the earlier pgbench fixture
  capture was idle. A repeated, controlled application-impact baseline remains incomplete.
- **Single runs, no repetitions.** `benchmarks/README.md` requires at least three repetitions
  after a warm-up before any result is reported. The durations above are one sample each on one
  host and must not be cited as a baseline.
- **Provenance remains partial for those historical runs.** Later runs recorded the main-branch
  SHA `8d67adf`, which does not identify the subsequently committed runner/workload changes by
  itself. Retain exact source state and raw evidence for new measurements.

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
