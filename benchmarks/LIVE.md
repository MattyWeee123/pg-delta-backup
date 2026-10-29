# Benchmark the current live pgdelta engine

The shared CLI, native recovery suite and HammerDB driver now accept two
additional methods:

| Method | Requested path | Output |
|---|---|---|
| `pg_delta_live` | Live source, hashing | An old copy refreshed in place |
| `pg_delta_live_summaries` | Live source, guarded WAL summaries | The same; falls back to hashing if the guard refuses |

These methods do not stage a full source backup. They verify the immutable
B0, copy it to a fresh output as untimed fixture preparation, then refresh
that output. B0 stays reusable across trials. The copy cost is retained under
`transfer_stats.preparation` and is included in the report's preparation plus
method column. It also warms the output's cache: these are uncontrolled-cache
smoke comparisons, not cold-cache performance measurements.

## What passing means

Live pgdelta emits no PostgreSQL backup manifest. The adapter returns
`PASS_ENGINE_ONLY`, with `restore_tested: false`, after checking:

- Complete source/receiver statistics and a committed receipt for this output,
  agreeing on their digest and file count, with successful live cleanup.
- No incomplete-refresh marker, running-server marker, recovery signal,
  external symlink or hard-linked output file.
- Agreement between the backup label, reported live WAL start, timeline,
  cluster identity and output `pg_control`.
- Parsing all required packaged WAL from start through stop with `pg_waldump`.

The data-integrity evidence is the engine's own verification receipt. This is
not an independent re-hash or `pg_verifybackup`. The native runner separately
starts the output and checks exact logical recovery. Its active cutoff is
`pgdelta.live.stop_lsn`, validated by the adapter, with the timeline and
system identifier. The candidate uses only packaged WAL; the independent
reference uses B0 plus separately archived WAL to reach the same cutoff.
The [active-cutoff contract](../docs/CORRECTNESS.md) and
[native runbook](NATIVE.md) explain that oracle and its supported fixture.

`pg_delta_live_summaries` requests optimization; it does not guarantee its use.
Inspect `backup_result.transfer_stats.reported.fast_path.used` and `reasons`,
also shown in the Markdown report. PostgreSQL 17.11+ with `summarize_wal=on`
and a provable basis history enables this path. A refused guard remains a
correct hashing refresh and must not be described as a summary-path timing.

## Run locally: disposable correctness comparison

Requirements: Linux/POSIX, Python 3.10+, an unprivileged user, and matching
PostgreSQL 17 server/client tools including `pg_controldata`, `pg_waldump`,
`pg_verifybackup`, `pg_combinebackup` and `pgbench`. The engine needs no third
party Python package; optional `blake3` changes the selected hash and must be
recorded consistently for comparisons. The suite creates its own isolated
clusters and private Unix sockets, not an existing database.

From the repository root, set the actual tools directory. On the current
development machine the temporary build is
`/tmp/pgdelta-native-oracle-build/install/bin`; on the team's Debian lab VMs it
is normally `/usr/lib/postgresql/17/bin`.

```bash
PG_BIN=/usr/lib/postgresql/17/bin

PGDELTA_TEST_PG_BIN="$PG_BIN" \
  python3 -m unittest discover -s tests -p test_live_benchmark.py -v

python3 -m benchmarks.native_baseline \
  --bin-dir "$PG_BIN" --output results/live-comparison \
  --methods pg_basebackup_full,pg_basebackup_incremental,pg_delta_live,pg_delta_live_summaries \
  --rows 20000 --repeats 3 --duration 20 --rate 100 --pgdelta-jobs 1

python3 -m benchmarks.report_native results/live-comparison/results.json \
  --output results/live-comparison/report.md
```

Use a new output path each time. All four methods run with active and
quiescent sources, plus one no-backup control per repetition: **27 trials** at
three repetitions. `--duration` is seconds. Increase it if a backup outlasts
its active workload; such trials are rejected. Use a quiet host and run one
benchmark job at a time. This is synthetic correctness/measurement evidence,
not a capacity-calibrated throughput or WAN speedup claim.

The live regression tests cover both paths under load, a real public CLI
capture and restore, stale/mismatched receipts and missing packaged WAL.
Pure adapter tests still run without PostgreSQL; live tests then skip.

## Run on an existing GCP benchmark VM

This runs the same single-host suite on an idle lab VM. It does not create
resources, reformat disks, stop existing databases, or run a two-VM WAN sweep.
For cross-host experiments use [the network lab](../experiments/network/README.md).
The team's [GCP setup](../infra/gcp/README.md) describes VM sets and credentials.

To include local uncommitted changes in a GCP run, export working files rather
than using only a branch checkout or `git archive HEAD`. Include new untracked
modules from the local repository:

```bash
BUNDLE=/tmp/pgdelta-live-worktree.tar.gz
git ls-files -z --cached --others --exclude-standard -- \
  benchmarks pgdelta tests docs infra .github README.md .dockerignore .gitignore .gitattributes \
  | tar --null -T - -czf "$BUNDLE"
sha256sum "$BUNDLE"

# Replace these with the existing VM, zone and login/key for your lab set.
PROJECT=postgres-delta-backup
VM=pgd-source
ZONE=us-central1-b
VM_USER=bench
SSH_KEY="$HOME/.ssh/pgd-bench/id_ed25519"
REMOTE_DIR="pgdelta-live-$(date -u +%Y%m%dT%H%M%SZ)"

gcloud compute ssh "$VM_USER@$VM" --project "$PROJECT" --zone "$ZONE" \
  --ssh-key-file "$SSH_KEY" --command "mkdir -p ~/$REMOTE_DIR"
gcloud compute scp "$BUNDLE" "$VM_USER@$VM:~/$REMOTE_DIR/source.tar.gz" \
  --project "$PROJECT" --zone "$ZONE" --ssh-key-file "$SSH_KEY"
gcloud compute ssh "$VM_USER@$VM" --project "$PROJECT" --zone "$ZONE" \
  --ssh-key-file "$SSH_KEY" \
  --command "cd ~/$REMOTE_DIR && tar -xzf source.tar.gz"
```

Run on the VM's data disk using a short writable scratch path. The benchmark
uses `tempfile`; setting `TMPDIR` ensures its databases use `/data` rather
than the boot disk. Paths must stay short enough for Unix-domain sockets.

```bash
gcloud compute ssh "$VM_USER@$VM" --project "$PROJECT" --zone "$ZONE" \
  --ssh-key-file "$SSH_KEY" --command "cd ~/$REMOTE_DIR && \
  mkdir -p /data/pgd-tmp && \
  TMPDIR=/data/pgd-tmp python3 -m benchmarks.native_baseline \
    --bin-dir /usr/lib/postgresql/17/bin --output results/gcp-live \
    --methods pg_basebackup_full,pg_basebackup_incremental,pg_delta_live,pg_delta_live_summaries \
    --rows 20000 --repeats 3 --duration 20 --rate 100 --pgdelta-jobs 1 && \
  python3 -m benchmarks.report_native results/gcp-live/results.json \
    --output results/gcp-live/report.md"

mkdir -p results
gcloud compute scp --recurse "$VM_USER@$VM:~/$REMOTE_DIR/results/gcp-live" \
  "results/$REMOTE_DIR" --project "$PROJECT" --zone "$ZONE" --ssh-key-file "$SSH_KEY"
```

If `/data` is not writable by the lab user, use an existing writable data-disk
directory; do not point the runner at production data. An interrupted SSH
session may stop this foreground run: use the lab's terminal/session manager
for longer runs. The runner preserves failure evidence and cleans up only its
own clusters. Keep result bundles before removing the temporary remote source.

All three frontends record `source_provenance.code_sha256` over their actual
benchmark and engine Python files. This survives export without `.git` and
changes with uncommitted code edits. CLI evidence stores it in
`source-provenance.json`; native bundles and HammerDB provenance embed it.
Compare local and remote hashes before interpreting results. Git identity is
additional context and may be unavailable in the exported directory.

Google's command references: [compute ssh](https://cloud.google.com/sdk/gcloud/reference/compute/ssh)
and [compute scp](https://cloud.google.com/sdk/gcloud/reference/compute/scp).

## Existing fixtures: CLI and HammerDB

The live process must run beside readable source PGDATA, with access at the
same absolute path the server reports in `SHOW data_directory`. A client
connection alone does not provide file access. Authentication uses libpq's
environment or `.pgpass`; passwords do not enter the recorded conninfo.

```bash
python3 -m benchmarks.benchmark --method pg_delta_live_summaries \
  --host /path/to/source/socket --user bench \
  --source-pgdata /path/to/disposable/source \
  --basis /path/to/immutable/B0 --out results/live-cli \
  --pg-controldata "$PG_BIN/pg_controldata" --psql "$PG_BIN/psql" \
  --pg-verifybackup "$PG_BIN/pg_verifybackup" --pg-waldump "$PG_BIN/pg_waldump" \
  --pgdelta-jobs 1
```

For Docker, the optional overlay mounts the source volume read-only into
runner at that same path. Build the HammerDB schema using
[its runbook](workloads/hammerdb/README.md), then capture B0 before the measured
changes. Use both Compose files for every fixture command:

```bash
export POSTGRES_PASSWORD='choose-a-throwaway-password'
export PG_IMAGE=postgres:17
docker compose -p fixture -f benchmarks/fixture/docker-compose.yml \
  -f benchmarks/fixture/docker-compose.live.yml up -d --wait postgres
docker compose -p fixture -f benchmarks/fixture/docker-compose.yml \
  -f benchmarks/fixture/docker-compose.live.yml build runner

# After building the HammerDB schema, capture and retain the printed run ID.
docker compose -p fixture -f benchmarks/fixture/docker-compose.yml \
  -f benchmarks/fixture/docker-compose.live.yml run --rm runner \
  --host postgres --user postgres --out /results/basis-runs

docker compose -p fixture -f benchmarks/fixture/docker-compose.yml \
  -f benchmarks/fixture/docker-compose.live.yml run -d --name pg-delta-exec \
  --entrypoint sleep runner infinity

python3 -m benchmarks.hammerdb_experiment --output results/hdb-live \
  --method pg_delta_live_summaries \
  --basis /results/basis-runs/FULL_RUN_ID/backup \
  --source-pgdata /var/lib/postgresql/data \
  --pg-controldata /usr/lib/postgresql/17/bin/pg_controldata --pgdelta-jobs 1 \
  --runner-container pg-delta-exec --backup-output /results/hdb \
  --network fixture_default --compose-project fixture \
  --repeats 3 --vu 8 --warehouses 10 --rampup 1 --duration 11
```

HammerDB duration/ramp-up are **minutes**. Its live methods retain the engine-only
verdict; this integration does not add a TPROC-C logical recovery oracle.
Failed/missing backup verification rejects a performance pair even if its
observation windows match; rejected pairs do not contribute degradation
figures. Failed backup evidence gives the run a failure exit status.
Container orchestration is stub-tested; native/CLI recovery is tested against
real PostgreSQL. Docker/HammerDB live execution and GCP timings must be
validated on their target hosts. Pin image digests before a reportable run.
