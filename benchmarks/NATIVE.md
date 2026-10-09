# Run native PostgreSQL baselines

This runner creates real PostgreSQL 17 clusters, takes full and incremental backups, combines incrementals, verifies required files/WAL, and boots isolated disposable restores. It establishes a working native comparison before custom optimization. It currently produces **local smoke measurements**, not the complete controlled performance baseline described in README.md.

The first complete successful run is preserved in the [October 9 smoke report](evidence/2026-10-09-native-smoke/report.md), with results.json, provenance and compressed raw evidence alongside it.

The suite now calls the same `benchmarks/backup.py` adapters and v2 backup record as
Sam’s configurable CLI. CI also invokes that public CLI for full and incremental
captures and restores both outputs. See [the consolidation guide](CONSOLIDATION.md).

## What the treatments mean

- **Full:** copy a new complete physical backup with streamed WAL.
- **Incremental:** use an old backup manifest and WAL summaries, verify the incremental, combine with the old backup, then verify the usable result. The old basis remains intact.
- **No backup:** run the same transaction workload without starting a measured backup; this is the application control.

Each treatment starts a fresh cluster with the same synthetic data and its own B0. Preparation is excluded from refresh timing. Trials are randomized within each repetition. The smoke configuration uses 20,000 account rows with 256-byte payloads, 1% deterministic key changes, separate insert/delete fixtures and a rollback fixture. This is not a claimed production-sized database.

Active runs use two pgbench clients making atomic account transfers, with ordered row locks and an audit row in each transaction. The total account balance must remain constant. The default offered rate is 100 TPS for eight seconds. Ten committed transactions establish workload readiness. A backup must finish before the workload ends or its trial is rejected; increase duration for larger fixtures. There is no workload-capacity calibration yet.

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

In active trials, package-only startup and balance conservation are checked without external WAL. After the workload drains, an independent source export is compared with an isolated restore at a named restore point. That restore can access the separate WAL archive and is labeled PITR-assisted. Required archive bytes and preparation time are recorded separately; they are not silently part of the native backup's package. The archive counter includes all retained trial WAL, not just the minimal supplementary range. Exact logical equivalence at the native package's own active-capture cutoff remains unimplemented.

Successful external pgbench log entries must match the source transaction-ledger count. This supplements the full source/restored data comparison; it is not a general commit-order oracle. Backups taken independently are never compared byte for byte. Only supported fixture objects are covered; this does not certify arbitrary schemas, extensions, tablespaces, failover or cross-version recovery.

The first quiescent full trial includes corrupt-file, missing-required-WAL and stale-B0 controls. Each must be rejected. Unit tests also cover a changed digest with unchanged row count, malformed workload logs and missing/conflicting WAL copies.

## Measurement boundaries

`capture_seconds` is pg_basebackup duration, including normal synchronization and streamed WAL. `method_seconds` adds incremental-input verification and combination for the incremental treatment. `verified_backup_seconds` also includes final verification. The common verified B0 is a preparation cost, excluded from all refresh timings. Restores and later PITR validation are separate.

`capture_bytes` and `output_bytes` are apparent file lengths, including metadata/WAL, not allocated disk size or wire bytes. Network bytes, server CPU, device I/O and peak RSS are null/unavailable until observers are implemented. An absent phase is different from an unmeasured one: full-backup combine time is zero because that phase does not apply.

Transaction logs use completion timestamps to select the backup interval. Rate-limited pgbench latency includes schedule lag. Percentiles use nearest rank. The whole-workload and backup-window results are both saved; the report does not average per-trial percentiles into a global percentile. A no-backup control has a longer window than a short backup, so any subsequent degradation calculation must match relative windows or clearly use a separate fixed-window analysis.

The smoke run has no excluded backup warmup, no cold-cache control and three repetitions by default. Treat timings as descriptive and inspect raw spread. Do not use these numbers to approve an optimization. Controlled hardware, representative sizes/churn, calibrated workload rates, matched windows, network/resource instrumentation, rsync/staged controls and the stronger active-cutoff oracle remain release gates.

## Evidence and failure handling

Every trial keeps a JSON verdict, commands, stdout/stderr, PostgreSQL logs, manifests, state digests and transaction logs. Results are written atomically. Any subprocess failure, missing log, bad oracle, ended workload or cleanup failure rejects the trial and exits the matrix early. The failed record stays in the bundle; reruns use a new directory. Database copies are removed after owned servers stop. If shutdown cannot be confirmed, the scratch directory is retained and named in the error record.

The workflow retains a small artifact for three days. It prints machine-readable results in the job log so the inspected numbers can also be retrieved independently. Export the evidence before retention expires.
