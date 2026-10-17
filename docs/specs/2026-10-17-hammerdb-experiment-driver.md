# HammerDB experiment driver: plan

Date: October 17, 2026.
Owner: workloads and measurement role.
Status: proposed plan, not implemented.

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

Each milestone ends with tests passing and a push to the feature branch.

| | Deliverable | Verification |
|---|---|---|
| **M1** | Branch created. Latency source confirmed. TPM series parser and latency parser, pure functions. | Unit tests over captured sample log text, including the October 9 series |
| **M2** | Window alignment, the three window computations, degradation math, record assembly. | Table-driven tests: aligned windows, rejected pair, missing samples, divide-by-zero guard |
| **M3** | Orchestration: container lifecycle, matched pair, randomization, `run_backup` and `observe_docker` wiring, evidence layout. | Tests against stub `docker`/HammerDB; no real containers |
| **M4** | Validated pilot run against the fixture, evidence committed, README section appended, `VALIDATION.md` updated. | A real matched pair completes and produces a populated `results.json` |
| **M5** | PR opened, stacked on PR #2, with a comment on #2 describing the split. | Reviewer can run the documented command |

M1 to M3 need no PostgreSQL and no Docker; M4 does.

## What this does not establish

The driver measures application degradation during a backup. It does not test restore, does not
measure network bytes, does not calibrate offered load, and does not provide a recovery oracle for
the TPROC-C schema. A completed pilot is a working measurement instrument, not a baseline result:
`BASELINE-GATE.md` still requires pinned hardware, calibrated scales, matched controls and at
least three repetitions on a controlled host before any number is cited.
