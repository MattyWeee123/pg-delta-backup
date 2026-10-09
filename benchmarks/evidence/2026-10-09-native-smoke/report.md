# Native PostgreSQL backup smoke measurements

**Scope: real local backups and restores; not a controlled network performance baseline.**

Run status: **pass**. PostgreSQL: postgres (PostgreSQL) 17.11 (Debian 17.11-1.pgdg13+2).

Source commit: `285a896075e742c0d8acfcd5fc9677e370d5fa28`. Base image: `public.ecr.aws/docker/library/postgres@sha256:2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3`.

| Method | Source workload | Accepted trials | Median capture seconds | Median verified backup seconds | Median captured MiB |
|---|---|---:|---:|---:|---:|
| full | quiescent | 3 | 0.465 | 0.546 | 44.976 |
| incremental | quiescent | 3 | 0.415 | 0.947 | 21.208 |
| full | active | 3 | 0.465 | 0.546 | 45.007 |
| incremental | active | 3 | 0.415 | 0.978 | 21.372 |

Capture size includes bundled WAL and metadata. It is **not network traffic**.

Verified time includes capture, incremental-input verification where applicable, combination, normal synchronization, and output verification. Common B0 setup is excluded. Restore testing is additional.

Evidence: [successful GitHub run](https://github.com/MattyWeee123/pg-delta-backup/actions/runs/37991303022). The accompanying ZIP retains the raw artifact with a per-file SHA-256 inventory.

## Correctness

Quiescent trials compare full ordered data exports and supported schema against the source after writes are fenced. Active trials test package-only startup and a conservation invariant, then exact logical equivalence at a named recovery target using additional archived WAL. The latter is explicitly PITR-assisted.

The exact logical oracle at the active package’s own capture cutoff is not implemented. Do not read an active PASS as proof of that stronger guarantee.

| Trial | Status | Package logical comparison | PITR logical comparison |
|---|---|---|---|
| 00-full-active-r0 | pass | not_checked | pass |
| 01-no_backup-active-r0 | pass | not applicable | not applicable |
| 02-full-quiescent-r0 | pass | pass | not applicable |
| 03-incremental-active-r0 | pass | not_checked | pass |
| 04-incremental-quiescent-r0 | pass | pass | not applicable |
| 05-no_backup-active-r1 | pass | not applicable | not applicable |
| 06-incremental-active-r1 | pass | not_checked | pass |
| 07-full-active-r1 | pass | not_checked | pass |
| 08-incremental-quiescent-r1 | pass | pass | not applicable |
| 09-full-quiescent-r1 | pass | pass | not applicable |
| 10-incremental-active-r2 | pass | not_checked | pass |
| 11-full-active-r2 | pass | not_checked | pass |
| 12-no_backup-active-r2 | pass | not applicable | not applicable |
| 13-full-quiescent-r2 | pass | pass | not applicable |
| 14-incremental-quiescent-r2 | pass | pass | not applicable |

## Limits and reproduction

- Tiny synthetic database, fresh cluster per trial, uncontrolled host cache and shared CI hardware when run in Actions.
- Fixed low offered transaction rate checks active capture; it is not a calibrated application-capacity experiment.
- Per-transaction logs preserve scheduled latency, failures and lag. Very short backup windows do not support strong p99 conclusions.
- No source/destination CPU, peak-memory, device-I/O or wire-byte measurements yet.
- No rsync/custom-engine comparison, large-data matrix or AlloyDB Omni validation yet.
- Failed trials are retained and are not included as fast successful backups.

Use the immutable image digest and configuration in results.json to repeat the run. See benchmarks/NATIVE.md for commands and exact scope.
