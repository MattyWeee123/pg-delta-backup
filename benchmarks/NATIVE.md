# Run native PostgreSQL baselines

This runner creates real PostgreSQL 17 clusters, takes full and incremental backups, combines incrementals, verifies required files/WAL, and boots isolated disposable restores. It establishes a working native comparison before custom optimization. It currently produces **local smoke measurements**, not the complete controlled performance baseline described in README.md.

The first complete successful run is preserved in the [October 9 smoke report](evidence/2026-10-09-native-smoke/report.md), with results.json, provenance and compressed raw evidence alongside it.

The suite now calls the same `benchmarks/backup.py` adapters and v3 backup record as
Sam’s configurable CLI, and runs methods from the same registry (`benchmarks/methods.py`).
Trial methods carry registry names (`pg_basebackup_full`, ...) from bundle schema v3 on; the
committed v1 evidence says `full` and `incremental`, and `report_native` reads both.
CI also invokes that public CLI for full and incremental captures and restores both
outputs. See [the consolidation guide](CONSOLIDATION.md).

## What the treatments mean

- **Full:** copy a new complete physical backup with streamed WAL.
- **Incremental:** use an old backup manifest and WAL summaries, verify the incremental, combine with the old backup, then verify the usable result. The old basis remains intact.
- **No backup:** run the same transaction workload without starting a measured backup; this is the application control.
- **Staged transfers (opt-in):** `--methods pg_basebackup_full,rsync_staged` adds `rsync_staged`; `pg_delta_staged` runs the pgdelta tool. Each captures a full S, builds the output from S and the trial's B0, and verifies it with S's manifest. Copying B0 for rsync happens before the workload starts and is reported as `preparation_seconds`, outside the refresh timings; `transfer_seconds` is the transfer alone. The default matrix stays full plus incremental, so new runs remain comparable with the committed evidence. The first quiescent trial of the first listed method carries the negative controls.

Each treatment starts a fresh cluster with the same synthetic data and its own B0. Preparation is excluded from refresh timing. Trials are randomized within each repetition. The smoke configuration uses 20,000 account rows with 256-byte payloads, 1% deterministic key changes, separate insert/delete fixtures and a rollback fixture. This is not a claimed production-sized database.

Active runs use two pgbench clients making atomic account transfers, with ordered row locks and an audit row in each transaction. Each script also performs an explicit rolled-back marker insert. A separate transaction stays open across capture and is rolled back afterward. The total account balance must remain constant. The default offered rate is 100 TPS for eight seconds. Ten committed transactions establish workload readiness. A backup must finish before the workload ends or its trial is rejected; increase duration for larger fixtures. There is no workload-capacity calibration yet. These rollback probes change the workload relative to historical bundles; new bundles use schema v4 and their timings should not be pooled with old evidence.

## Run on an existing Linux machine

Install matching PostgreSQL 17 server and client binaries, including pg_combinebackup, pg_verifybackup, pgbench and pg_waldump, plus Python 3.10+. Run as a non-root user. The script only starts clusters it creates in a new temporary directory. It never accepts an existing PGDATA directory or source connection.

```sh
python -m unittest discover -s tests -v
python -m benchmarks.native_baseline \
  --bin-dir /usr/lib/postgresql/17/bin \
  --output results/native-first \
  --rows 20000 --repeats 3 --duration 8 --rate 100
python -m benchmarks.report_native results/native-first/results.json \
  --output results/native-first/report.md
```

The output path must not exist. Use a different path for each attempt. Do not run multiple benchmark jobs concurrently on the same machine. All database authentication is confined to private Unix-socket directories; TCP listening is disabled. Caller libpq environment settings are removed. Source and restore connections check the actual data-directory identity.

For Docker, resolve `public.ecr.aws/docker/library/postgres:17` (the Docker Official Image in ECR Public) to a digest, build with `--build-arg PG_IMAGE=<digest>`, and run benchmarks/Dockerfile as its non-root postgres user. The Actions workflow records the resolved digest before building and uses `--network none`, two CPUs, 2 GiB memory, and 256 MiB shared memory. Preserve the digest for reproduction; the mutable tag is only the initial resolver. The exact Python and PostgreSQL versions are recorded too.

## Meaning of correctness

In quiescent trials, an independent source export defines expected data. Ordered CSV encodes NULLs distinctly and hashes all supported table rows; schema columns and constraints are compared separately. The output alone must restore and match that state.

In active trials, the cutoff is the verified output manifest's single WAL range **End-LSN**, together with its timeline and system identifier. It is the [earliest consistent endpoint](https://www.postgresql.org/docs/17/backup-manifest-wal-ranges.html), not the command's wall-clock finish and not necessarily the last record bundled in the final WAL segment. Multi-timeline manifests and mismatched identities are rejected by this fixture's oracle.

The independent reference is the immutable B0 plus the source's separately archived WAL. It recovers to `recovery_target_lsn=End-LSN` with `recovery_target_inclusive=off`: End-LSN points just past the backup-end record, so including the next record could include another transaction. The candidate uses `recovery_target=immediate`, `recovery_target_action=pause`, and `restore_command=false`, with upstream streaming disabled. It can use only its own bundled WAL. Both restores must report `pg_last_wal_replay_lsn()` exactly equal to End-LSN before ordered CSV digests, row counts, supported schema and total balance are compared. The candidate is then resumed to prove recovery completes. This implements the active package exact-cutoff oracle; it does not change the generic CLI's `PASS_MANIFEST_ONLY` contract.

After the workload drains, source ledger operation IDs must equal the externally logged successful pgbench IDs, not merely their count. Each cutoff restore must contain contiguous prefixes of those independently acknowledged per-client streams. The rollback markers and open transaction must be absent. A committed post-capture marker must also be absent at the cutoff. Per-client sequence numbers identify operations; they do not establish a global commit order. The independent archived-WAL replay determines which commits precede the boundary.

An additional independent source export is compared with a later named-target restore, labeled PITR-assisted as before. Archive bytes and preparation time are recorded separately; they are never part of the candidate package's recovery dependencies or measured backup duration. The archive counter includes all retained trial WAL, not just the minimal supplementary range. Oracle restores, snapshotting and the post-capture probes run outside the measured backup interval; the open transaction is established before it.

Successful external pgbench log entries must match the source transaction-ledger IDs and count. This supplements the full source/restored data comparison; it is not a general commit-order oracle. Backups taken independently are never compared byte for byte. Only supported fixture objects are covered; this does not certify arbitrary schemas, extensions, tablespaces, failover or cross-version recovery. Historical bundles without an explicit `active_package_exact_cutoff_oracle: pass` retain their original weaker correctness scope.

Run the endpoint and negative-control regression tests with:

```sh
PGDELTA_TEST_PG_BIN=/usr/lib/postgresql/17/bin \
  python3 -m unittest discover -s tests -p test_native_oracle.py -v
```

With PostgreSQL 17 available, these capture real active full and incremental backups. They also inject a lost transfer plus its ledger entry while preserving the balance total, use a wrong later-state oracle, and remove the candidate's WAL while leaving the reference archive available. Each fault must be rejected. Without PostgreSQL, the pure boundary/log tests run and the live test is skipped.

The first quiescent full trial includes corrupt-file, missing-required-WAL and stale-B0 controls. Each must be rejected. Unit tests also cover a changed digest with unchanged row count, malformed workload logs and missing/conflicting WAL copies.

## Measurement boundaries

`capture_seconds` is pg_basebackup duration, including normal synchronization and streamed WAL. `method_seconds` adds incremental-input verification and combination for the incremental treatment, or the transfer for a staged method. `verified_backup_seconds` also includes final verification. The common verified B0 is a preparation cost, excluded from all refresh timings. Restores and later PITR validation are separate.

`capture_bytes` and `output_bytes` are apparent file lengths, including metadata/WAL, not allocated disk size or wire bytes. Network and device-level bytes remain unavailable. An absent phase is different from an unmeasured one: full-backup combine time is zero because that phase does not apply.

On Linux, each trial now saves `source-resources.jsonl` and a `source_resources` summary for the PostgreSQL postmaster and its descendants during the measured backup or no-backup interval. Each backup command also saves a `.resources.jsonl` series and embeds a `resources` summary in its phase record for the command process tree. These are sampled CPU seconds, process read/write bytes and peak summed RSS, not exact host device usage; short-lived processes can be missed and RSS double-counts shared pages. A command with fewer than two live samples reports CPU and I/O as null. A host without Linux `/proc` reports an explicit unavailable reason. PostgreSQL `pg_stat_checkpointer`, `pg_stat_wal` and `pg_stat_io` snapshots and numeric deltas are saved under `postgres_stats`; they cover the whole measured interval, not individual commands. Counter resets or concurrent activity would invalidate a simple interpretation of those deltas.

Transaction logs use completion timestamps to select the backup interval. One script observation represents one committed transfer followed by a rolled-back probe; its latency includes both. Rate-limited pgbench latency includes schedule lag. Percentiles use nearest rank. The whole-workload and backup-window results are both saved; the report does not average per-trial percentiles into a global percentile. A no-backup control has a longer window than a short backup, so any subsequent degradation calculation must match relative windows or clearly use a separate fixed-window analysis.

The smoke run has no excluded backup warmup, no cold-cache control and three repetitions by default. Treat timings as descriptive and inspect raw spread. Do not use these numbers to approve an optimization. Controlled hardware, representative sizes/churn, calibrated workload rates, matched windows and network/device instrumentation remain release gates.

## Evidence and failure handling

Every trial keeps a JSON verdict, commands, stdout/stderr, PostgreSQL logs, manifests, state digests and transaction logs. Results are written atomically. Any subprocess failure, missing log, bad oracle, ended workload or cleanup failure rejects the trial and exits the matrix early. The failed record stays in the bundle; reruns use a new directory. Database copies are removed after owned servers stop. If shutdown cannot be confirmed, the scratch directory is retained and named in the error record.

The workflow retains a small artifact for three days. It prints machine-readable results in the job log so the inspected numbers can also be retrieved independently. Export the evidence before retention expires.
