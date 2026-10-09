# Consolidated benchmark and tool choices

Sam's `benchmark-harness-v0.1` commit `ff8b85e` is preserved as a parent of the consolidation merge. Its full-backup CLI, Docker fixture, HammerDB scripts and tests are integrated with the native recovery suite. There is one shared backup implementation in `benchmarks/backup.py`.

## Two entry points, shared backup phases

| Entry point | Purpose | Uses |
|---|---|---|
| `python -m benchmarks.benchmark` | Run one full or incremental backup against an existing disposable fixture; retain logs and results | Shared capture, input verification, combination, final verification, timers and v2 backup record |
| `python -m benchmarks.native_baseline` | Create small isolated databases, run the randomized matrix, and test actual recovery | The same adapters and record, plus controlled fixtures, pgbench, restore oracles and negative controls |

Both paths default to plain format, streamed WAL, SHA256 manifests, a fast checkpoint and normal PostgreSQL synchronization. The CLI permits an explicit checksum/checkpoint choice, recorded with the run; comparisons must match those choices. `--format tar` is now rejected because this PG17 verification path expects an unpacked plain backup. There is no custom backup or transfer engine in either path.

The shared adapter supports `pg_basebackup_full` and `pg_basebackup_incremental`. Incremental requires a complete immutable B0, verifies it as preparation, captures I1, verifies I1, combines B0+I1 with `--copy`, then verifies the resulting full package. A failing phase stops the pipeline. The native suite passes the actual B0 verification result obtained before workload launch; the standalone CLI performs that prerequisite itself. Chained incremental bases are outside this CLI's initial contract.

The CLI's result is `PASS_MANIFEST_ONLY`, with `restore_tested: false`. That statement remains true even when a surrounding integration test subsequently restores the output: the restore verdict is recorded separately. A native trial's outer `pass` means its documented recovery checks passed; it does not certify unimplemented active-cutoff guarantees.

## Shared result schema v2

Sam's original ten-field v0.1 record is extended deliberately, with `schema_version: 2` and contract tests. Both frontends retain the shared backup record. Native results embed it under `backup_result`; their outer record also contains workload and restore evidence. Existing convenience timing fields are derived from that shared result. Previously committed v1 evidence is historical and is not rewritten.

| Field | Meaning |
|---|---|
| `backup`, `verification` | Capture and final manifest/WAL verification outcomes |
| `basis_verification` | Incremental prerequisite, outside refresh timing; the creation/storage of B0 must also be disclosed |
| `increment_verification`, `combination` | Incremental-only phases; null for full backup |
| Phase `exit_code`, `duration_seconds`, `error` | Execution outcome; timeout/missing executable is a failure, never a successful zero-time run |
| `backup_file_bytes`, `output_file_bytes` | Apparent captured and usable-output file lengths, including WAL/metadata; not network bytes |
| `method_seconds` | Wall time for capture and any input verification/reconstruction |
| `time_to_verified_seconds` | Wall time through final verification; null on failure |
| `integrity_verified`, `restore_tested`, `status` | Explicit distinction between manifest integrity and separately tested recovery |
| `network_bytes`, `unavailable_metrics` | Null plus reason until a transport observer exists |

Timings include command launch and waiting. At the smoke database's short durations, orchestration overhead is material. These measurements are not a performance acceptance threshold. Execution logs and atomically published JSON survive failures; POSIX command timeouts kill the process group. The CLI refuses source/basis/output overlap, including symlink aliases. Auth/TLS environment settings are preserved while implicit libpq routing settings are removed.

## HammerDB and pgbench have different jobs

Our account-transfer fixture already uses PostgreSQL's existing **pgbench**, not a new load generator. pgbench supplies concurrency, fixed offered rates, random scheduling and transaction logs; our small SQL script supplies changes with an easily checked invariant. It is useful for deterministic recovery cases and measuring latency under a chosen load. [pgbench documentation](https://www.postgresql.org/docs/17/pgbench.html)

**HammerDB TPROC-C** supplies a richer order-processing schema, data generator and five transaction types. Reuse Sam's scripts for application-oriented performance experiments. TPROC-C is derived from TPC-C and has important driver differences; it is not an audited TPC-C result. It cannot replace a restore oracle. [HammerDB workload definition](https://www.hammerdb.com/docs/ch03s06.html)

Do not replace one with the other or treat their scores as interchangeable. pgbench TPS is not HammerDB new orders per minute (NOPM). Do not force pgbench's offered-rate percentages onto HammerDB's virtual-user-driven load without a separate calibration. Record warehouses, virtual users, all-warehouse behavior, rampup and measurement windows. Sam's scripts enable `pg_allwarehouse`; default home-warehouse sizing advice must be interpreted with that setting in mind. [HammerDB sizing guidance](https://www.hammerdb.com/docs/ch03s07.html)

The current exact-data restore checker is specific to the account-transfer fixture. Adding HammerDB to the recovery acceptance gate requires an expected-state export/checker covering its schema and a declared recovery point. Importing the scripts alone does not provide that guarantee. The full HammerDB performance matrix remains to be automated and rerun with retained raw evidence.

## Existing tools to reuse

| Tool | Decision and role |
|---|---|
| PostgreSQL `pg_basebackup`, `pg_combinebackup`, `pg_verifybackup` | Already used for capture, native incremental reconstruction and integrity/WAL validation. Our code orchestrates them. |
| pgbench | Already used for inexpensive controlled workloads and per-transaction observations. |
| HammerDB | Sam's schema builder and workload scripts retained for the larger application benchmark. Do not implement a new TPC-C-like generator. |
| Docker / Compose | Shared environment packaging and lifecycle; optional on a machine with matching tools installed. |
| [rsync](https://download.samba.org/pub/rsync/rsync.1) | Next transfer comparison on identical frozen backup inputs. Use remote/delta mode explicitly and include basis/output work. Do not synchronize changing live PGDATA and call it a valid backup. |
| [pgBackRest](https://pgbackrest.org/user-guide.html) | Later production-backup comparison; supports established backup/restore workflows. Keep its repository, compression and recovery semantics explicit. It is a comparison target, not the custom algorithm. |
| [sysstat](https://sysstat.github.io/) (`pidstat`, `iostat`) | Reuse process and device observations when implementing resource instrumentation; account for child/server processes and observer overhead. |
| Linux interface counters / packet capture | Count the designated backup link in both directions. Container-wide traffic may include workload traffic, and summing both endpoints double-counts the same bytes. Validate the observer before claiming savings. |
| [iperf3](https://software.es.net/iperf/) | Calibrate the test link before backup runs. It is not a measurement of bytes sent by the backup itself. |

We do not need Kubernetes, a custom scheduler, a monitoring dashboard, or a new backup manager for the first baseline. The project-specific layer is experiment coordination, consistent timing boundaries, independent recovery checks, and reproducible reports.

## Why Docker is useful, and its limits

A container packages the database/client dependencies and gives each run a separate process/filesystem environment. Compose defines how the fixture database and backup runner start and connect. This reduces teammate setup differences and makes disposable environments easy to recreate. Docker does not implement the benchmark or require GCP. Both Python entrypoints can run directly on Linux with matching PostgreSQL 17 binaries. [Docker container explanation](https://docs.docker.com/get-started/docker-concepts/the-basics/what-is-a-container/)

Both runner images now come from **one Dockerfile**: `--target cli` for the configurable runner and `--target native` for recovery smoke. Sam's separate `fixture/runner.Dockerfile` is retired. Compose's `runner` service uses the CLI target. PostgreSQL base images should be resolved to a digest before a measured experiment; record the built runtime identity and all software versions for the controlled baseline as well.

CPU/memory limits are limits, not exclusive reservations of physical hardware. Separate containers can still compete for the same CPU, cache, network and disk; separate Docker volumes do not imply separate disks. Docker Desktop also adds its VM environment. Final claims require a documented host, storage and topology, whether containers are used or not. [Docker resource controls](https://docs.docker.com/engine/containers/resource_constraints/)

No cloud provisioning is part of this consolidation. GCP can host the future controlled lab once the personal-billing issue and spending allowance are resolved.

## Completion still required

The combined code is a working integration foundation. It is not the completed baseline release: finish the HammerDB recovery oracle and workload coordination, matched no-backup windows, network/resource observers, calibrated scales and repeated controlled runs. See [BASELINE-GATE.md](BASELINE-GATE.md). Candidate optimization follows that release.
