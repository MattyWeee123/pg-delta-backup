# Current live engine in the shared benchmark harness

Implemented on `codex/live-pgdelta-benchmark`, based on the active-cutoff work
in `codex/native-active-cutoff`. The two changes are reviewed as separate draft PRs.

## Problem and resulting behavior

The shared frontends could benchmark only `pg_delta_staged`, which first took
a full source backup. The engine's current live in-place path and guarded
WAL-summary path were therefore absent from the common comparison, despite
being the paths optimized in the later experiments.

Two registered methods now run them directly: `pg_delta_live` and
`pg_delta_live_summaries`. Both verify immutable B0, prepare a separate copy,
and refresh that copy in place from live source PGDATA. There is no source
staging capture. Preparation is timed separately, retained and disclosed;
the source's backup-mode checkpoint policy and worker count are recorded.
The native suite's default matrix remains unchanged; these arms are opt-in.

## Verification contract

Live outputs have no native manifest. The generic adapter uses
`PASS_ENGINE_ONLY` and `verification.kind=pgdelta_receipt_and_wal`, preserving
`restore_tested=false`. The structural v3 record remains unchanged; the new
status is explicit. Receipt/statistics agreement, completion/cleanup,
independent output ownership, backup-label start, cluster identity and
packaged WAL parsing are required. This trusts the engine's data receipt and
does not claim an independent data re-hash.

The native runner then restores the candidate without external WAL to its
validated `live.stop_lsn`. The independent B0/archive reference reaches the
same LSN exclusively. Exact replay position, supported logical data/schema,
external operation IDs, rollback/in-flight exclusions and the post-capture
marker are checked as in the native active-cutoff work. Later PITR remains
separate. A summary request that falls back to hashing is visible in retained
statistics and in the report, not silently credited as a summary-path result.

## Files and responsibilities

| File | Responsibility |
|---|---|
| `benchmarks/methods.py` | Two live registrations, preparation, source-local arguments and custom verification |
| `benchmarks/backup.py` | Common timers and explicit success status for a custom verifier |
| `benchmarks/live_verify.py` | Standalone engine-evidence/output/WAL verifier, usable inside a tools container |
| `benchmarks/native_baseline.py` | Source-local fixture wiring and exact stop-LSN recovery comparison |
| `benchmarks/benchmark.py` | CLI source/tools/worker flags and immutable-basis checks |
| `benchmarks/hammerdb_experiment.py` | The same methods through Docker with matched workload windows |
| `benchmarks/provenance.py` | Hash actual benchmark/engine sources, including uncommitted exports |
| `benchmarks/report_native.py` | Verification scope, path/guard decisions and source hashes |
| `benchmarks/fixture/docker-compose.live.yml` | Optional read-only source PGDATA mount at its original absolute path |
| `.github/workflows/native-baseline.yml` | Additional live matrix and real adapter fault tests |

## Validation and limits

Real PostgreSQL 17.11 pilot: both live methods passed active and quiescent
trials. Real regression tests cover both under load and a public CLI capture
and restore. Missing WAL and mismatched receipts are rejected. Pure tests
cover failure propagation, source/worker requirements, conninfo escaping,
preparation boundaries, custom verdicts, output aliases and source provenance.
The HammerDB container flow is stub-tested for both method wiring and failed
statistics; live Docker/HammerDB execution needs the target environment.
The integration also closes a discovered acceptance gap: matched timing
windows alone no longer let a failed backup enter a HammerDB performance
pair. Failed/missing verification suppresses throughput and latency loss
figures, and failed backups give the run a failure exit status.

These are correctness smoke measurements, not calibrated throughput, WAN or
large-scale claims. Source-local execution still needs the same visible
PGDATA path and sufficient privileges. The TPROC-C recovery oracle remains
unimplemented. No GCP resources were provisioned and no cloud performance
claim is made by this implementation.

## How to run

[LIVE.md](../../benchmarks/LIVE.md) contains complete local, GCP working-tree
export, result retrieval, CLI and Docker/HammerDB commands. Export working
files rather than `git archive HEAD` while this branch is uncommitted; compare
the source hashes to identify the code actually executed.
