# Benchmark and restore plan

Status: experiment design, not measured database results.
Do not compare an already staged custom source against an end-to-end baseline without also reporting staging cost.

## Two different experiments

### Transfer-only experiment

All methods receive the same frozen B0 and B1 directories.
Compare full directory transfer, rsync delta mode, and the custom transfer.
Each method reconstructs the same B1 bytes.
Include hashing, basis signature exchange, instructions, payload, metadata and output verification within the timed boundary.
Record transport encryption/compression settings.
Use fresh destination outputs and a read-only basis for each run.

### End-to-end database experiment

Compare remote full pg_basebackup, native incremental plus combination, and source-side capture plus custom transfer.
Start the clock at backup initiation and stop at a verified usable backup.
Report restore/recovery time separately and also report time-to-restored-database.
Measure source capture/staging and database workload impact for the custom path.
Use equivalent data, churn, WAL handling, compression, resource limits and durability settings.
Record differences in prerequisites rather than masking them.

Native incremental output is not the same artifact as a usable full backup until it is combined.
The old basis backup's creation is a common prerequisite and normally excluded from recurring refresh cost, but disclose its storage and initial provisioning cost.
If a method needs additional persistent preparation, report it separately.

## First PostgreSQL baseline recipe

Use a fresh disposable Linux/container test environment, standard PostgreSQL 17 binaries and synthetic data only.
No cloud provisioning is needed for the first baseline.
The commands below are a runbook, not a validated script for every host.
Paths refer to the chosen test environment; do not run them against personal or production data.

1. Record `postgres --version`, `pg_basebackup --version`, `pg_combinebackup --version`, `pg_verifybackup --version`, OS, CPU, RAM, storage, image digest and configuration.
2. Initialize a fresh cluster with data checksums and configure permitted replication access, enough WAL senders, and `summarize_wal = on`.
3. Create a deterministic schema and data set; record row counts, stable ordered data digests and schema output.
4. Capture B0 using plain pg_basebackup with streamed WAL and SHA-256 manifest checksums.
5. Verify B0 with matching-major pg_verifybackup, preserving WAL parsing, then restore a disposable copy and check data.
6. Apply a reproducible change workload, quiesce writes for the initial comparison, record expected SQL data, and capture B1 plus a native incremental I1 referencing B0's manifest.
7. Combine B0 and I1 into a fresh directory, verify it, restore a copy, and compare the expected data.
8. Use the immutable B0 and B1 as inputs to transfer-only experiments.
9. Extend to sustained concurrent writes only after defining a common recovery endpoint and oracle; comparing arbitrary "latest" queries on a moving source is invalid.

Example commands once permissions and fresh paths are configured:

```sh
pg_basebackup -h "$PGHOST" -U "$PGUSER" -D "$B0" -Fp -X stream --manifest-checksums=SHA256
pg_verifybackup "$B0"
# Apply the deterministic changes here, then capture new backups.
pg_basebackup -h "$PGHOST" -U "$PGUSER" -D "$B1" -Fp -X stream --manifest-checksums=SHA256
pg_basebackup -h "$PGHOST" -U "$PGUSER" -D "$I1" -Fp -X stream --incremental="$B0/backup_manifest" --manifest-checksums=SHA256
pg_combinebackup -o "$COMBINED" "$B0" "$I1"
pg_verifybackup "$COMBINED"
```

All destination variables must name distinct, fresh directories.
Pass credentials using the environment's approved PostgreSQL authentication mechanism; never put passwords in a committed script or command transcript.
For the staged custom path, B1 capture runs on the source side of the link under test.
The commands alone do not set up a remote topology or measure its traffic.

## Workload matrix

| Dimension | Initial cases | Expansion after first restore |
|---|---|---|
| Data size | Small smoke case, then measured approximately 1 GiB | 10-100 GiB as budget permits; record actual on-disk size |
| Change pattern | No workload, localized updates, distributed updates, insert, delete | Append-heavy, VACUUM, VACUUM FULL, REINDEX, TRUNCATE, DDL |
| Change amount | 0%, 1%, 10%, 100% of chosen row set | Record actual changed file/chunk bytes independently |
| Basis | Valid recent backup | Older backup, corrupted basis chunk, wrong cluster, absent basis |
| Network | Local correctness first; then two hosts/containers across a measured link | Controlled low/high bandwidth and latency |
| Source activity | Quiescent data for a deterministic oracle | Concurrent writes and declared recovery endpoint |
| Algorithm | Full transfer, fixed 8 KiB, fixed 64 KiB, rsync | CDC with recorded min/average/max parameters |

No-workload backup captures may still differ in metadata and WAL.
Expect zero literal data only for an exactly identical synthetic input, not necessarily two database backup runs.

## Metrics and definitions

Record total bytes in both directions on the designated interface or transport.
Application payload counters are diagnostic and must not be labeled wire bytes.
Account separately for basis signatures, COPY instructions, literals, metadata, required WAL, encryption/framing and retransmissions where available.

```text
network_saving = 1 - total_custom_wire_bytes / total_full_wire_bytes
speedup = full_end_to_end_seconds / custom_end_to_end_seconds
```

Count the whole path fairly even when saving is negative or speedup is below 1.
Report source and destination CPU time, peak RSS, bytes read/written, scratch space, capture duration, transfer duration, verification duration, restore duration, workload throughput and p95 latency where measured.
Do not put zero in unavailable fields; use null plus a reason.

Use at least three repetitions after one warm-up for an initial result, randomize method order, and disclose warm/cold cache policy.
Never clear caches or restart shared infrastructure without the environment owner's agreement.
Keep raw per-run records, commands, versions and failed runs.
Prefer medians plus range for the small initial sample; do not claim significance from three runs.

## Restore oracle

- First prove exact reconstructed bytes relative to B1 before booting the output.
- Verify the PostgreSQL manifest and required WAL using the appropriate major-version tools.
- Restore from a disposable copy into an isolated instance with distinct sockets/ports and no unintended upstream replication connection.
- Confirm recovery completion, expected tables and schema, row counts, deterministic ordered data digests, and selected application-level invariants.
- Test missing/corrupt WAL and changed output as negative controls.
- Preserve the immutable basis and target backup fixtures for reproduction.

For concurrent writes, use a declared recovery target or a controlled barrier that both expected-state capture and restore honor.
Two independently captured backups need not have identical bytes or identical recovery endpoints even if taken close together.

## Results record

Store one JSON object per run with: run_id, timestamp, git_commit, versions, image_digest, method, configuration, workload_seed, actual_db_bytes, requested_row_change_fraction, observed_chunk_change_fraction, basis_age, timings, traffic_by_direction, cpu, memory, disk_io, storage, verification_result, restore_result, errors and unavailable_metrics.
Include a units object and a `measurement_kind` distinguishing synthetic payload estimates, local transfer instrumentation and real wire measurements.

Do not fabricate a baseline when Docker, PostgreSQL or Omni is unavailable.
