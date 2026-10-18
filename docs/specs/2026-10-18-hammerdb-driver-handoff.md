# HammerDB degradation driver: handoff

Written October 18, 2026, for whoever picks this up next.
Author of the work: Sam (workloads and measurement role).
Companion document: [the plan and milestone log](2026-10-17-hammerdb-experiment-driver.md).

This describes a completed, reviewable piece of work and the exact state it is in.
Read "Traps" before changing anything; several of the bugs fixed here were invisible
to a passing test suite.

---

## 1. Where this sits in the project

`pg-delta-backup` asks whether reusing an older backup at a destination can produce a
correct new backup while sending fewer bytes over a constrained link. The sponsor
wants three things measured under active workload: backup time, network traffic, and
**application performance degradation**.

The delta algorithm does not exist yet. What exists is measurement infrastructure, so
that the eventual custom method is compared against baselines produced by identical
instrumentation rather than separately written scripts.

Two documents named this specific gap:

- `benchmarks/CONSOLIDATION.md`: "The full HammerDB performance matrix remains to be
  automated and rerun with retained raw evidence."
- `benchmarks/BASELINE-GATE.md`: "Sam's HammerDB TPROC-C scripts now provide the
  richer application workload. Add their recovery oracle and matched observation
  windows."

**This work delivers the automation and the matched windows.** The recovery oracle is
explicitly not delivered; see section 8.

---

## 2. Current state

| | |
|---|---|
| Branch | `benchmark-harness-v0.1` |
| PR | [#3](https://github.com/MattyWeee123/pg-delta-backup/pull/3) → `main`, OPEN, MERGEABLE |
| Size | +2,522 / −8 across 14 files |
| Tests | 133 pass (62 in this module's suite), stable across repeated runs |
| CI | All three checks green, including `native-baseline` at 4m28s |

Both earlier PRs (#1 Matt's consolidation, #2 Etash's resource metrics) merged to
`main` on October 17 while this was in progress. This branch merged the new `main`
cleanly with zero conflicts.

### Commits, oldest first

| Commit | What |
|---|---|
| `7193112` | M0: Windows `wal_copy` guard, CI registry fallback |
| `78bc271` | M1: measurement readers |
| `a85ccb4` | M2: windows and degradation arithmetic |
| `e6e05e7` | M2.5: cross-check fixes (timezone, SQLite path) |
| `fb21e12` | M3: orchestration |
| `d593eb3` | M3 follow-up: four defects found by the first live run |
| `d9f6298` | Merge of the new `main` |
| `42e0e35` | M4: pilot run and evidence |
| `dfb0a2f` | M5: PR recorded, CI validation of the fallback |

---

## 3. What was built, milestone by milestone

### M0 — unblock the base branch

Two fixes to files owned by others. **Both are still absent from `main`**, so this PR
fixes them there.

**`benchmarks/wal_copy.py`** opened the destination directory read-only to `fsync` it
after `os.replace`. That is POSIX-only; Windows cannot open a directory that way, so
`test_wal_copy_and_conflicting_archive` raised `PermissionError` on a Windows checkout
while passing on the Linux runner. Guarded with the same `os.name != "nt"` check that
`atomic_json` in `backup.py` already uses. Linux durability is unchanged.

**`.github/workflows/native-baseline.yml`** resolved `postgres:17` from a single
anonymous registry. `public.ecr.aws` began returning `toomanyrequests: Data limit
exceeded`, failing the job in nine seconds before any database work.

This was **not a code regression**, and that matters for reading the history: the same
workflow passed on October 9, and Etash's commit touched no workflow or Dockerfile.
It is a registry quota.

The step now tries three mirrors of the same Docker Official Image with bounded
retries, records the serving registry in `BENCH_IMAGE_SOURCE`, and fails explicitly
when none answer. The sha256 is identical across mirrors; only the repository prefix
differs, so the pinned digest stays reproducible.

### M1 — measurement readers

`benchmarks/hammerdb_experiment.py` lines ~50–240.

The measurement source is **HammerDB's jobs SQLite database**, not scraped console
output. This was a deliberate change from the original plan after inspecting a real
run:

- `JOBTCOUNT` holds the transaction-counter series with timestamps, structured.
- `JOBTIMING` holds latency percentiles; `summary = 1` rows aggregate all virtual users.
- `JOBMAIN` holds the job id.

The log is still read, for the `TEST RESULT` NOPM line that the database does not
store, and for the window markers. It is retained as raw evidence.

Functions: `read_tpm_series`, `read_latency`, `read_jobid`, `parse_test_result`,
`parse_log_markers`, `read_log`, `_leading_timestamp`, `_readonly`.

### M2 — windows and degradation

Lines ~246–450. This is the conceptual core.

`Window`, `samples_in`, `window_stats`, `analysis_window`, `recovery_window`,
`matched_window`, `throughput_loss`, `latency_change`, `build_pair_record`.

### M2.5 — cross-check

A review before building further, which found two real defects. Both are described in
section 6 because they are instructive, not merely historical.

### M3 — orchestration

Lines ~455–810. `TrialError`, `_docker`, `start_workload`, `wait_for_marker`,
`wait_for_exit`, `collect_workload`, `container_bytes`, `make_executor`,
`start_observer`, `read_trial`, `run_trial`, `run_pair`, `parse_args`, `main`.

### M4 — pilot

Three runs against the real fixture. Two rejected, one accepted. Evidence committed.

### M5 — PR #3

---

## 4. How the measurement works

### The problem it solves

Comparing a window that contains a backup against a whole control run makes the answer
depend on how much of the window the backup happened to occupy. The October 9 figures
prove it: **the same backup on the same cluster gave 34% loss when it started late in
the window and 65% when it started early.** Neither number describes the backup.

### The fix

The **analysis window** is the interval the backup actually occupied, from capture
start to final verification end. The control is measured at the **same offset from its
own ramp-up, for the same duration**.

```text
throughput_loss = 1 - mean_tpm(treatment, analysis) / mean_tpm(control, matched)
```

Three windows are reported and never collapsed into one figure:

| Window | Span | Why |
|---|---|---|
| `whole_window` | ramp-up complete → test complete | Continuity with earlier figures |
| `analysis_window` | capture start → verification end | The degradation figure |
| `recovery_window` | backup end → window end | Throughput stays depressed after capture |

### Sampling semantics

A counter sample stamped `T` reports work over the interval preceding `T`, so
attribution uses `(start, end]`. **The cadence is not fixed** — a real run contained 4
and 16 second intervals alongside the nominal 10 — so each window also reports its
sample count, observed span, min and max. Means are plain averages, not time weighted;
that is an approximation and is documented as one.

### Rejection

A pair whose backup did not run entirely inside the measurement window is **rejected**,
mirroring how `native_baseline` rejects a trial whose backup outlives its workload. A
control run shorter than the treatment is rejected too. Rejected pairs carry a reason
and no number.

Unavailable values are null with a reason, never zero: fewer than two samples
(`MIN_SAMPLES`, matching the existing convention), a zero control mean, missing
percentiles.

---

## 5. How to run it

### Requirements

**Linux only.** The shared adapters wrap destinations in `pathlib.Path`, which on
Windows rewrites a container path `/results/x` into `\results\x` and the command
fails. Use WSL or a Linux host. The parsing and arithmetic are platform independent
and their tests run anywhere.

### Environment currently left running on Sam's machine

| | |
|---|---|
| `fixture-postgres-1` | PostgreSQL 17.11, healthy, network `fixture_default` |
| `pg-delta-exec` | Long-lived tools container, `.pgpass` already written |
| `fixture_pgdata` | Volume holding the 10-warehouse TPROC-C schema, ~1.2 GB |
| `bench-runs` | Volume holding backup outputs from the pilots |

Password used for the disposable fixture: `bench-throwaway-pw-20261009`.

### From scratch

```sh
export POSTGRES_PASSWORD='choose-a-throwaway-password'
export PG_IMAGE=postgres:17          # avoids the ECR quota locally
docker compose -f benchmarks/fixture/docker-compose.yml up -d --wait postgres

# Build the TPROC-C schema once. 10 warehouses is about 1 GiB.
docker run --rm --network fixture_default \
  -e PGBENCH_HOST=postgres -e PGBENCH_PASS="$POSTGRES_PASSWORD" \
  -e TPCC_WAREHOUSES=10 -e TPCC_BUILD_VU=8 \
  -v "$PWD/benchmarks/workloads/hammerdb":/hdb:ro \
  tpcorg/hammerdb:latest ./hammerdbcli auto /hdb/build_schema.tcl

# Long-lived container holding the PostgreSQL tools, driven via docker exec.
docker run -d --name pg-delta-exec --network fixture_default \
  -v bench-runs:/results --entrypoint sleep pg-delta-runner infinity
docker exec pg-delta-exec sh -c \
  'printf "postgres:5432:*:postgres:%s\n" "'"$POSTGRES_PASSWORD"'" > /root/.pgpass && chmod 600 /root/.pgpass'
```

### The experiment

```sh
POSTGRES_PASSWORD='...' python3 -m benchmarks.hammerdb_experiment \
  --output results/hammerdb-run --repeats 3 \
  --vu 8 --rampup 1 --duration 11 \
  --network fixture_default --compose-project fixture \
  --runner-container pg-delta-exec --backup-output /results/hdb
```

**Choose `--duration` longer than the backup**, or every pair is rejected. On this
fixture capture plus verification took 328–398 s, so 11 minutes works and 6 did not.
Each repetition is two trials, so `--repeats 3 --duration 11` is roughly 72 minutes.

Exit codes: `0` success, `2` invalid input, `3` a trial failed or no pair was accepted.

---

## 6. Traps

Everything here was found the hard way. Do not assume a passing suite means these are
gone if you restructure the code.

### The module broke only when run as `__main__`

Helper definitions drifted below the `if __name__ == '__main__'` guard, so `main()`
called `_leading_timestamp` before it existed and raised `NameError` on the first log
parsed. **Importing a module defines everything before anything runs**, so 130 tests
passed while the only path that matters in production was broken.

`test_module_runs_as_main` now runs the module as a subprocess. Keep it.

### HammerDB writes a carriage return between timestamp and message

`2026-10-17T21:19:57.756031072Z \rVuser 1:Rampup complete`

`str.splitlines()` treats a lone `\r` as a line break, separating each marker from its
timestamp. Worse, **Python's universal-newline translation does the same thing** if a
caller opens the log in default text mode, so results depended on how the file was
read. `parse_log_markers` carries the most recent timestamp forward so both shapes
agree; `read_log` uses `newline=''` to keep evidence byte-faithful; `collect_workload`
captures `docker logs` as **bytes**, because `text=True` strips the carriage returns
before they ever reach the file.

### The timestamp parser silently discarded the time zone

The original nanosecond-truncation logic collected every digit remaining in the
string, which swept up the offset's digits, rebuilt the text with no zone, and relied
on an "assume UTC" fallback. `+05:30` was read as UTC, a 5.5 hour error. It passed
every test because `docker logs -t` always emits UTC. Replaced with an explicit
`RFC3339` pattern that keeps fraction and offset separate.

### A `#` in a path opened a different database

Interpolating a path into a `file:` URI leaves `#` acting as a fragment delimiter, so
SQLite silently opened something else and the tables appeared missing. Evidence
directories are named from run identifiers, so this was reachable. All readers go
through `_readonly`, which uses `Path.as_uri()`.

### `run_backup` returns a tuple

`(record, exit_code)`, not a record. Assigning it directly embeds a tuple in the
result. Their own `benchmark.py` and `native_baseline.py` unpack it; match that.

### Captured byte counts are null for remote captures

`run_backup` calls `directory_bytes` on the host. A capture executed through
`docker exec` does not exist there, so the shared record honestly reports null. The
driver measures the output inside the container and records it separately as
`output_bytes_observed`. **Do not "fix" the shared adapter**; the null is correct for
what it can see.

### Each trial needs its own backup destination

`pg_basebackup` refuses a non-empty directory, so a shared path fails every repetition
after the first.

### Unrelated host load halves the measurement

An otherwise identical 6 minute run measured a control of **8,708 tpm**, while one
executed with the test suite running concurrently measured **4,318 tpm**. Do not run
anything else on the host during a pilot. This is the strongest single argument for
moving pilots off a laptop.

---

## 7. Measured results

Preserved under `benchmarks/evidence/2026-10-17-hammerdb-pilot/` with `report.md`,
`results.json`, `provenance.json`, `raw-evidence.zip` with a SHA-256 inventory, and
`rejected-runs.zip`.

PostgreSQL 17.11, 10-warehouse TPROC-C at 1,231 MB, 8 virtual users, 1 minute ramp-up,
11 minute measurement, trial order drawn from seed 116458667.

| Window | Control | Backup | Loss |
|---|---|---|---|
| Whole measurement | 20,093.4 tpm (n=67) | 8,528.7 tpm (n=68) | 57.6% |
| **Aligned, 328.4 s each** | **18,979.3 tpm (n=33)** | **5,432.6 tpm (n=32)** | **71.4%** |
| Recovery after capture | 18,979.3 reference | 11,280.8 tpm (n=36) | 40.6% still down |

Whole-run p95 rose 57.9 ms → 154.0 ms (+166%). Backup: `PASS_MANIFEST_ONLY`, capture
232.7 s, verification 95.6 s, 2,015,067,325 output bytes.

**The aligned figure is the one to read.** The 14 point gap exists because 346 s of the
674 s window had no backup running.

Two runs were rejected by the window guard, correctly, and are retained:

| Run | Backup span | Window | Outcome |
|---|---|---|---|
| 2 minute window | 174.0 s | 123.8 s | rejected |
| 6 minute window | 398.5 s | 363.7 s | rejected |

---

## 8. What is NOT done

Stated plainly so none of it reads as an oversight.

| | Status |
|---|---|
| **Repetitions** | One accepted pair. `benchmarks/README.md` requires at least three after a warm-up, with medians and ranges. No significance is claimed. |
| **Resource isolation** | None. Database, 8 virtual users and the backup shared one laptop's CPUs and a single SSD; the backup wrote to the same disk as PGDATA. |
| **Window-aligned latency** | **Impossible** from HammerDB's instrumentation. `JOBTIMING` covers a virtual user's whole elapsed time including ramp-up and the schema has no time dimension. Would require changing HammerDB's Tcl driver. The record labels its scope `whole_run`. |
| **Workload calibration (G6)** | Not started. Needs the sustainable no-backup rate, then 25/50/75% offered load. HammerDB is virtual-user driven, so it needs its own calibration curve; pgbench percentages do not transfer. |
| **TPROC-C recovery oracle (G7)** | Not started, and the largest remaining item. Needs an expected-state export for the TPROC-C schema and a declared recovery point. `CONSOLIDATION.md` is explicit that importing the scripts does not provide this. Until it exists HammerDB can measure performance but cannot certify correctness. |
| **Restore testing** | Not done. Status is `PASS_MANIFEST_ONLY`, gate G3 unmet. |
| **Network bytes** | `network_bytes` null with a reason. No transport observer exists. |
| **rsync / transfer baselines** | Belongs to the transfer owner. |

TPROC-C is not audited TPC-C. Never publish these as `tpmC`.

---

## 9. Suggested next steps, in order

1. **Three repetitions on a quiet host.** One command now. The contamination finding
   suggests the numbers will move materially. This is the cheapest path to a citable
   result and closes gate item 5.
2. **Workload calibration (G6).** Find the sustainable no-backup rate, freeze
   25/50/75%. Needs a quiet host too, so pairs naturally with step 1.
3. **TPROC-C recovery oracle (G7).** The largest item, and the one that lets HammerDB
   join the correctness gate rather than only the performance one.
4. **Transfer observers.** `network_bytes` is the last null in the record that the
   sponsor's question actually needs.

Steps 1 and 2 want a dedicated machine. That is a *resource isolation* need, not a
cloud need; a spare Linux box qualifies. See the earlier analysis in
`BASELINE-GATE.md` on environment and cost, and note that
`docs/BEN-QUESTIONS.md` still lists cloud budget as an open P1 question.

---

## 10. File map

| File | |
|---|---|
| `benchmarks/hammerdb_experiment.py` | The driver, 840 lines |
| `tests/test_hammerdb_experiment.py` | 62 tests, 703 lines |
| `tests/stub_docker.py` | Stand-in docker CLI; implements `run`, `logs`, `inspect`, `cp`, `exec`, `rm` against real wall-clock timestamps |
| `benchmarks/workloads/hammerdb/README.md` | Appended a driver section; existing text untouched |
| `benchmarks/evidence/2026-10-17-hammerdb-pilot/` | Pilot evidence |
| `docs/specs/2026-10-17-hammerdb-experiment-driver.md` | Plan, milestone log, status table |
| `docs/VALIDATION.md` | What ran and what it does not establish |
| `benchmarks/wal_copy.py`, `.github/workflows/native-baseline.yml` | The two M0 fixes |

**No other file is modified.** `backup.py`, `resources.py`, `observe_docker.py`,
`native_baseline.py`, `benchmark.py`, `cli_smoke.py`, `report_native.py` and
`Dockerfile` are untouched, and so are all six pre-existing test files. The driver
consumes `run_backup`, `atomic_json`, `run_command` and `observe_docker` rather than
reimplementing or modifying them.
