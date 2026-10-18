# HammerDB experiment driver: plan

Date: October 17, 2026.
Owner: workloads and measurement role.
Branch: `benchmark-harness-v0.1`, based on Etash's `c1d21b8`.

## Status

Updated as each milestone lands. Keep this table and the milestone sections in
agreement; it is the single source of truth for what remains.

| | Milestone | State | Commit |
|---|---|---|---|
| M0 | Unblock the base branch | **done** | `7193112` |
| M1 | Measurement readers | **done** | `78bc271` |
| M2 | Windows and degradation | **done** | `a85ccb4` |
| M2.5 | Cross-check and fixes | **done** | this commit |
| M3 | Orchestration | not started | |
| M4 | Pilot run | not started | |
| M5 | PR and handoff | not started | |

Tests: 118 passing, 47 of them in `tests/test_hammerdb_experiment.py`.

### Deliberately out of scope

Named here so they are not mistaken for oversights. Each needs its own branch.

| | Why it is deferred |
|---|---|
| G6 workload calibration | Needs many long runs on a quiet host; results from this laptop would be discarded |
| G7 HammerDB recovery oracle | Needs an expected-state export for the TPROC-C schema and a declared recovery point; `CONSOLIDATION.md` states that importing the scripts does not provide this |
| Window-aligned latency | Impossible from HammerDB's instrumentation; see M1 findings |
| Network byte accounting | No transport observer exists; `network_bytes` stays null with a reason |
| rsync and transfer baselines | Belongs to the transfer owner |

### Open risks

| Risk | State |
|---|---|
| PR #2 moves under us | Base `c1d21b8` confirmed current at time of writing; rebase if it changes |
| Pilot numbers are not citable | Expected. M4 produces an instrument, not a baseline |
| `--format tar` rejected upstream | Driver uses plain format only; no action needed |

## Problem

`benchmarks/workloads/hammerdb/README.md` records a degradation measurement that was produced by
hand: containers started manually, the backup launched from a second terminal, throughput read off
a log by eye. Nothing about it is reproducible, and the consolidation review correctly observed
that the two backup runs differed in both start position and duration, so neither isolates any
variable.

Two consolidation documents name this gap:

- `CONSOLIDATION.md`: "The full HammerDB performance matrix remains to be automated and rerun with
  retained raw evidence."
- `BASELINE-GATE.md`: "Sam's HammerDB TPROC-C scripts now provide the richer application workload.
  Add their recovery oracle and matched observation windows."

This plan covers the automation and matched windows. The recovery oracle is explicitly out of
scope and tracked separately.

## Goals

| | Goal | Done when |
|---|---|---|
| G1 | Matched-pair orchestration | One command runs control and backup trials with equal windows, randomized order, N repetitions |
| G2 | Window-aligned degradation | Degradation computed over the interval the backup actually occupied, not the whole run |
| G3 | Latency percentiles | p95 recorded for both treatments and `latency_increase` computed |
| G4 | Shared evidence format | Results written as a schema v2 compatible record embedding `backup_result` |
| G5 | Automatic resource observation | `observe_docker` sampled per trial without a manual second terminal |

Out of scope: workload calibration (G6, separate branch), HammerDB recovery oracle (G7, separate
and larger), rsync and transfer baselines, network byte counting.

## Non-interference constraints

Two teammates have open PRs touching these directories. The work is bounded so that it adds files
rather than editing theirs.

| File | Action |
|---|---|
| `benchmarks/hammerdb_experiment.py` | **new** |
| `tests/test_hammerdb_experiment.py` | **new** |
| `benchmarks/workloads/hammerdb/README.md` | **append a section**, no rewriting of existing text |
| `benchmarks/backup.py`, `resources.py`, `observe_docker.py`, `native_baseline.py`, `benchmark.py` | **not modified** |

Base branch is `codex/benchmark-resource-metrics` (PR #2), because `run_backup` and
`observe_docker` exist only there. If PR #2 changes before this merges, rebase rather than
cherry-pick, so Etash's commit stays a single parent.

Module name is flat (`benchmarks/hammerdb_experiment.py`) to match the existing
`python -m benchmarks.<module>` convention used by `benchmark`, `native_baseline`,
`report_native`, `observe_docker` and `cli_smoke`.

## Reuse rather than reimplement

| Existing | Used for |
|---|---|
| `backup.run_backup(method, *, run_id, output, tools, execute, host, ...)` | Runs the backup inside the measurement window and returns the schema v2 record. No backup logic is duplicated. |
| `backup.atomic_json` | Durable result publication |
| `backup.run_command` | Bounded subprocess execution with logs |
| `observe_docker.sample` / `main` | Per-trial container resource series |

## Design

### One invocation

```sh
python -m benchmarks.hammerdb_experiment \
  --output results/hammerdb-pilot --repeats 3 \
  --warehouses 10 --vu 8 --rampup 1 --duration 4
```

For each repetition it runs two trials, `control` and `backup`, in randomized order. A trial:

1. Start the HammerDB container detached, with the committed `run_timed.tcl`.
2. Start `observe_docker` for the trial.
3. Wait for `Rampup complete` in the log; this marks window start.
4. For the `backup` treatment only, call `run_backup` at a declared offset into the window.
5. Wait for `Test complete`; this marks window end.
6. Collect logs, the TPM series, the latency profile, resources, and the backup record.

### Window alignment, the central idea

The current measurement compares a 120 s control against a 120 s window that contained a 60 s
backup. The aggregate mixes backup and non-backup time, which is why moving the backup changed the
answer from 34% to 65% without changing the backup.

Instead, define the **analysis window** as the interval the backup actually occupied, from capture
start to final verification end. Compute the control's throughput over the *same offset and
duration* inside its own window, then:

```text
throughput_loss = 1 - tpm_backup_analysis_window / tpm_control_matched_window
```

Three windows are reported per trial pair, never collapsed into one number:

- `whole_window`: the full measurement period, for continuity with earlier figures.
- `analysis_window`: offset-and-duration matched, the degradation figure.
- `recovery_window`: from backup end to window end, because the October 9 series showed throughput
  still depressed after capture finished.

A pair whose backup does not fit entirely inside the measurement window is **rejected**, matching
how `native_baseline` rejects a trial whose backup outlives the workload.

### Metrics

| Source | Produces |
|---|---|
| `docker logs -t` TPM samples | Timestamped throughput series, per-window means |
| `TEST RESULT` line | NOPM and TPM aggregate, retained for continuity |
| HammerDB time profile | Latency percentiles, p95 primary |
| `observe_docker` | Container CPU, memory, block I/O series |
| `run_backup` record | Capture, verification, durations, output bytes |

### Result record

```text
results/hammerdb-<name>/
  results.json          outer record, schema_version 2
  provenance.json       images and digests, tool versions, git commit, host
  trials/<rep>-<treatment>/
    hammerdb.log
    tpm-series.jsonl
    latency-profile.log
    resources-docker.jsonl
    backup-result.json        (backup treatment only)
    backup/                   (backup treatment only)
```

`network_bytes` stays null with a reason in `unavailable_metrics`, consistent with both existing
entrypoints. Degradation figures are null plus a reason when a pair is rejected, never zero.

## Risks

| Risk | Handling |
|---|---|
| **Latency source unverified.** `pg_timeprofile true` is expected to write `/tmp/hdbxtprofile.log`, but its format has never been inspected and the Docker daemon was unavailable when this plan was written. | First task of M1 is to inspect it. Fallback is HammerDB's jobs database via `jobs <jobid> timing`. If neither parses, G3 ships as a retained raw artifact with percentiles recorded as unavailable plus a reason, rather than being faked. |
| PR #2 moves under us | Rebase before opening the PR; do not edit files Etash owns |
| Laptop measurements remain non-citable | Driver is validated for correctness here; numbers stay labeled smoke until run on a quiet host |
| Long runtimes | Pilot at `--repeats 1 --duration 2` during development; full pilot only at M4 |

## Milestones

Branch is `benchmark-harness-v0.1`, fast-forwarded onto Etash's `c1d21b8`. Each milestone ends
with the full suite passing and a push.

### M0: unblock the base branch — done

Pushed as `7193112`.

- `wal_copy.py`: guard the directory fsync behind `os.name != "nt"`, matching `atomic_json`.
  Windows went from 70/71 to 71/71.
- `.github/workflows/native-baseline.yml`: try three mirrors of the same Docker Official Image
  with bounded retries, record the serving registry in `BENCH_IMAGE_SOURCE`, fail explicitly when
  none answer. Fixes a `toomanyrequests` quota failure that was not a code regression.

### M1: confirm the latency source, then write the parsers

**M1.0 Retire the latency risk first.** Everything else in M1 is cheap; this is the only unknown.
Bring the fixture up, run a short HammerDB job **without `--rm`** so the container survives,
`docker cp /tmp/hdbxtprofile.log` out and inspect it. If unusable, try the jobs database
(`jobs <jobid> timing`). Capture whichever works verbatim as a test fixture. If neither parses,
p95 is reported as unavailable with a reason and the raw artifact is retained; it is never
invented.

**M1.1 `parse_tpm_series(log_text)`.** Consumes `docker logs -t` output and returns samples,
markers and the aggregate.

- `<rfc3339> NNNN PostgreSQL tpm` to `(timestamp, tpm)`
- `Rampup complete` and `Test complete` markers
- `TEST RESULT : System achieved <N> NOPM from <M> PostgreSQL TPM`
- Unparseable lines are counted, not silently dropped, so format drift is detectable
- A missing marker is an explicit error, never a zero

**Sampling semantics, which the window maths depends on:** the transaction counter reports work
over the preceding interval, so a sample stamped `T` describes roughly `(T-10, T]`. A sample is
attributed to a window when its timestamp falls in `(start, end]`. At a ten-second cadence this
leaves up to one sample of edge uncertainty, which is recorded with the result rather than hidden.

**M1.2 `parse_latency_profile(text)`.** Per-transaction percentiles plus overall p95, or an
explicit unavailable reason.

**M1.3 Tests** in `tests/test_hammerdb_experiment.py`, using the real captured October 9 series:
empty log, ramp-up only, truncated run with no `Test complete`, leading `0 PostgreSQL tpm`,
malformed timestamp, missing `TEST RESULT`.

### M2: window alignment and degradation

**M2.1** `Window(start, end)` with `duration_seconds`, and `samples_in(series, window)` using the
`(start, end]` rule above.

**M2.2** The three windows, never collapsed into one figure:

| Window | Span |
|---|---|
| `whole_window` | `Rampup complete` to `Test complete` |
| `analysis_window` | backup capture start to final verification end, clamped to `whole_window` |
| `recovery_window` | end of `analysis_window` to `Test complete` |

**M2.3** Matched control window: take the treatment's analysis-window offset from its own
`Rampup complete` plus its duration, and apply the same offset and duration to the control's
`Rampup complete`.

**M2.4** Degradation, following `benchmarks/README.md`:

```text
throughput_loss = 1 - mean_tpm(treatment, analysis) / mean_tpm(control, matched)
latency_increase = p95(treatment) / p95(control) - 1
```

Guards, all returning null plus a reason rather than a number: control mean of zero, fewer than
two samples in either window (matching the existing "fewer than two live samples" convention),
missing latency data.

**M2.5** Pair rejection when the analysis window is not wholly inside the whole window, mirroring
how `native_baseline` rejects a trial whose backup outlives the workload.

**M2.6** Record assembly: `schema_version: 2`, embedding the `run_backup` record under
`backup_result`, with `network_bytes` null and a reason in `unavailable_metrics`.

**M2.7** Tests: table-driven over aligned pairs, rejected pairs, zero-denominator, single-sample
windows, and a control shorter than the treatment.

### M2.5: cross-check — done

A review of M0 to M2 before building on them found two real defects, both now
fixed with regression tests.

**Timestamp parsing discarded the zone.** The nanosecond-truncation path
collected every digit in the remainder of the string, which swept up the
offset's digits as well, rebuilt the text without a zone, and relied on the
"assume UTC" fallback. `+05:30` was silently read as UTC, a five and a half
hour error in any derived window. Replaced with an explicit RFC3339 pattern
that separates fraction from offset. It works today only because
`docker logs -t` always emits UTC.

**A '#' in an evidence path opened a different database.** Interpolating a
path into a `file:` URI leaves `#` acting as a fragment delimiter, so SQLite
silently opened something else and the tables appeared missing. All readers
now go through `Path.as_uri()`, which percent-encodes it.

Also confirmed: no file belonging to Matt or Etash is modified apart from the
two M0 fixes; the October 9 replay still reproduces the published 65.1%
whole-window figure exactly; and the `wal_copy` guard is on the branch actually
taken on Windows.

### M3: orchestration

**M3.1** Container lifecycle: start detached without `--rm` so the profile can be copied out;
`wait_for(container, pattern, timeout)` polling the log; `collect` pulling `docker logs -t`, the
profile, then removing the container. A wait that times out fails the trial.

**M3.2** Trial runners. Control: start, wait for ramp-up, wait for completion, collect. Backup:
the same, with `backup.run_backup` invoked at a declared offset into the window.

**M3.3** Matched pair, `--repeats` repetitions, order randomized per repetition by a seeded
`random.Random`; the seed is recorded so an ordering is reproducible.

**M3.4** `observe_docker` started per trial into the trial directory.

**M3.5** Evidence layout written with `backup.atomic_json`.

**M3.6** `argparse` CLI: `--output --repeats --warehouses --vu --rampup --duration
--backup-offset --seed --method --image`. Refuses an existing output directory, as
`native_baseline` does.

**M3.7** Tests against a stub `docker` executable, as `tests/test_benchmark.py` already does. No
real containers.

### M4: pilot run

Needs Docker and the fixture.

1. Bring up the fixture; confirm or rebuild the 10-warehouse schema.
2. Build the shared CLI image (`--target cli`).
3. Smoke at `--repeats 1 --duration 3`, then the pilot at `--repeats 3`.
4. Commit evidence **excluding backup directories**, which are over a gigabyte each; follow the
   existing `tar --exclude=backup --exclude=increment` practice.
5. Append the README section; update `VALIDATION.md` with what ran and what it does not establish.

### M5: integration

Open the PR against `codex/benchmark-resource-metrics`, not `main`, so it does not appear to
re-propose Matt's and Etash's commits. Comment on PR #2 describing the split so nobody duplicates
the workload lane.

M0 to M3 need neither PostgreSQL nor Docker, except M1.0. M4 needs both.

## What this does not establish

The driver measures application degradation during a backup. It does not test restore, does not
measure network bytes, does not calibrate offered load, and does not provide a recovery oracle for
the TPROC-C schema. A completed pilot is a working measurement instrument, not a baseline result:
`BASELINE-GATE.md` still requires pinned hardware, calibrated scales, matched controls and at
least three repetitions on a controlled host before any number is cited.
