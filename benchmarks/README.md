# Benchmark and restore plan

Status: experiment design, not measured database results.
Sponsor direction from the supplied chats: active workload, time/network/application-impact comparison, team-defined scales and a custom correctness benchmark.
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

First measure the workload with no backup running to establish application throughput and latency without backup interference.
Compare remote full pg_basebackup, native incremental plus combination, and source-side capture plus custom transfer.
Start the clock at backup initiation and stop at a verified usable backup.
Report restore/recovery time separately and also report time-to-restored-database.
Measure source capture/staging and database workload impact for the custom path.
Use equivalent data, churn, WAL handling, compression, resource limits and durability settings.
Record differences in prerequisites rather than masking them.

Native incremental output is not the same artifact as a usable full backup until it is combined.
The old basis backup's creation is a common prerequisite and normally excluded from recurring refresh cost, but disclose its storage and initial provisioning cost.
If a method needs additional persistent preparation, report it separately.
The recovery comparison follows [CORRECTNESS.md](../docs/CORRECTNESS.md), not byte equality between independent captures.
Record each backup's recovery coverage and coverage age at publication so a stale snapshot is not mistaken for a fresh faster result.

## Proposed sponsor-facing scales and plots

Use small approximately 1 GiB, medium approximately 10 GiB and large approximately 50 GiB actual cluster data sizes as initial proposals.
The large case is conditional on storage and approved budget; account for multiple backup copies and retained WAL before provisioning.
Keep database size and workload intensity as separate dimensions.
Calibrate sustainable transaction rate without backup, then test low/medium/high offered rates at roughly 25%, 50% and 75% of that rate with fixed client/resource settings and a declared mix.
Record achieved rate, queued/dropped/failed requests and latency so offered load is not confused with completed work.
These are proposed experimental levels, not sponsor-defined requirements or established results.

For each chosen size/load case, show backup duration, total link traffic, throughput loss and p95 latency change.
Use the matching no-backup run as the denominator for application degradation:

```text
throughput_loss = 1 - TPS_during_backup / TPS_without_backup
latency_increase = p95_during_backup / p95_without_backup - 1
```

Where a fixed offered rate keeps completed throughput unchanged, latency and queueing may reveal degradation that TPS does not.
Include raw values and failed trials; report speedup only where the recovery guarantees and measurement boundaries match.

## Running the v0.1 runner

`benchmark.py` implements the narrowest useful slice of this methodology: it runs one backup
method, times it, verifies the output against its manifest, and writes a JSON run record.
Design notes are in [the runner design](../docs/specs/2026-10-09-benchmark-runner-design.md).

It does **not** test restore, measure network traffic, generate workload, or produce any of the
comparative numbers described above. A successful run records `PASS_MANIFEST_ONLY`, never `PASS`,
because gate G3 in [SPEC.md](../docs/SPEC.md) is unmet. `network_bytes` is always `null`.

Bring up the disposable fixture, then store the password in `.pgpass` rather than passing it on a
command line:

```sh
export POSTGRES_PASSWORD='choose-a-throwaway-password'
docker compose -f benchmarks/fixture/docker-compose.yml up -d
printf '127.0.0.1:5432:*:postgres:%s\n' "$POSTGRES_PASSWORD" > ~/.pgpass
chmod 600 ~/.pgpass
```

```sh
python benchmarks/benchmark.py \
  --host 127.0.0.1 --port 5432 --user postgres \
  --out benchmarks/runs
```

Output lands in `benchmarks/runs/<run-id>/` as `backup/`, `pg_basebackup.log`,
`pg_verifybackup.log`, `versions.txt` and `result.json`. The directory is gitignored; copy any run
you intend to cite into a tracked location.

| Flag | Default | Why it matters |
|---|---|---|
| `--manifest-checksums` | `CRC32C` | CRC32C is faster; step 4 below specifies SHA256, so the two are not directly comparable until one is changed |
| `--checkpoint` | `fast` | PostgreSQL defaults to `spread`, whose server-paced delay falls inside the measured duration |
| `--source-pgdata` | unset | When given, refuses an output path that overlaps the source data directory |
| `--timeout` | `3600` | A timed-out command records `exit_code: null` plus an error, never a clean exit |

Exit codes follow the CLI contract in [SPEC.md](../docs/SPEC.md) section 3: 0 success, 2
invalid or unsupported input, 3 I/O failure, 4 verification failure. The status field is
`PASS_MANIFEST_ONLY`, `FAIL_BACKUP` or `FAIL_VERIFY`.

Provenance that the ten-key record has no field for (tool versions, git commit, platform,
checksum algorithm, checkpoint mode) is written to `versions.txt` in the same run directory, so
step 1 below is satisfied without expanding the v0.1 schema.

### Reproducing a run without host PostgreSQL binaries

If the host has no PostgreSQL 17 client tools, run the runner inside a container on the fixture
network. This is also the more representative path, since SPEC section 1 targets Linux:

```sh
docker run --rm --network fixture_default \
  -v "$PWD":/repo:ro -v bench-runs:/runs -w /repo \
  -e DEBIAN_FRONTEND=noninteractive postgres:17 bash -c '
    apt-get update -qq && apt-get install -y -qq python3 git
    printf "postgres:5432:*:postgres:$POSTGRES_PASSWORD\n" > /root/.pgpass
    chmod 600 /root/.pgpass
    python3 benchmarks/benchmark.py --host postgres --user postgres --out /runs'
```

Install `git` as shown, otherwise `versions.txt` records `git_commit: unavailable` and the run
loses its provenance. On Git Bash for Windows, prefix the command with `MSYS_NO_PATHCONV=1` so
container paths are not rewritten to host paths.

The fixture and the runner have both been executed against a real PostgreSQL 17.11 cluster; see
[VALIDATION.md](../docs/VALIDATION.md) for the transcript, including the corrupted-backup and
unreachable-server negative controls. Those runs are single samples on one host and are not a
baseline: this document requires at least three repetitions after a warm-up before any number is
reported.

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
| Source activity | Quiescent fixture for initial debugging plus active-workload native baseline | Required integrated active-load comparison with declared recovery endpoint |
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

- For the frozen transfer component only, prove exact reconstructed bytes relative to B1 before booting the output.
- Verify the PostgreSQL manifest and required WAL using the appropriate major-version tools.
- Restore from a disposable copy into an isolated instance with distinct sockets/ports and no unintended upstream replication connection.
- Confirm recovery completion, expected tables and schema, row counts, deterministic ordered data digests, and selected application-level invariants.
- Test missing/corrupt WAL and changed output as negative controls.
- Preserve the immutable basis and target backup fixtures for reproduction.
- Compare committed operation outcomes against independent expected state; ensure rolled-back and in-flight transaction effects are absent.
- Restore the self-contained package without supplementary WAL access before any separately labeled PITR-assisted comparison.

For concurrent writes, use a declared recovery target or a controlled barrier that both expected-state capture and restore honor.
Two independently captured backups need not have identical bytes or identical recovery endpoints even if taken close together.
See CORRECTNESS.md for a common named-target test and its explicit additional WAL requirement.

## Results record

Store one JSON object per run with: run_id, timestamp, git_commit, versions, image_digest, method, configuration, workload_seed, actual_db_bytes, requested_row_change_fraction, observed_chunk_change_fraction, basis_age, timings, traffic_by_direction, cpu, memory, disk_io, storage, verification_result, restore_result, errors and unavailable_metrics.
Also record timeline, capture_end_lsn, guaranteed_recovery_cutoff, publication_time, coverage_age, oracle_kind, supplementary_wal_bytes, no_backup_reference_run, offered_rate, achieved_rate and request failures.
Include a units object and a `measurement_kind` distinguishing synthetic payload estimates, local transfer instrumentation and real wire measurements.

Do not fabricate a baseline when Docker, PostgreSQL or Omni is unavailable.
