# Validation performed

Latest update: October 9, 2026. The controlled performance baseline remains incomplete.

## Native PostgreSQL validation added October 9

The complete [workflow run 37991303022](https://github.com/MattyWeee123/pg-delta-backup/actions/runs/37991303022) passed for PR head `633782b84a17b8cd9d49595a109a11c6c71fed6e`, including tests, all database trials, evidence upload and report generation. Actions checked out synthetic merge commit `285a896075e742c0d8acfcd5fc9677e370d5fa28`, which is the commit recorded in results.json; this does not mean the PR was merged. Its `native-baseline-evidence` artifact contains the raw run records, commands, manifests, transaction logs and restore-state digests. The artifact has a three-day retention period; preserve an exported copy for longer-term use.

The [native smoke runner](../benchmarks/NATIVE.md) executed 15 real PostgreSQL 17.11 trials: full and incremental backups with quiescent and active source workloads, plus a no-backup active control, repeated three times in randomized order. Twenty unit tests passed. All 15 database trials passed in [run 37990014663](https://github.com/MattyWeee123/pg-delta-backup/actions/runs/37990014663); that workflow subsequently failed when uploading container-owned server logs. The upload permissions were corrected in commit `633782b` and a new complete run was requested. The earlier attempt [37989866519](https://github.com/MattyWeee123/pg-delta-backup/actions/runs/37989866519) stopped before database execution because Docker Hub rate-limited the shared runner; the workflow now resolves the public official-image mirror to an immutable digest.

These are real local backup/restore smoke measurements on a hosted Linux runner with a two-CPU, 2 GiB container, 20,000 account rows and a measured database size of 14,677,683 bytes. They are not the proposed 1/10/50 GiB matrix. No new GCP resources were provisioned.

- Six quiescent backups restored using only their own package and matched full ordered fixture data and supported schema against independently exported source state.
- Six active backups passed package-only startup and balance conservation; six additional PITR-assisted restores matched the source at a named recovery target using separately accounted archived WAL.
- Corrupt data, missing required WAL and an outdated basis backup were rejected by the appropriate checker.
- Active trials reconciled successful external pgbench transaction logs against source ledger counts and rejected failed/skipped work.

The active package's exact logical contents at its own cutoff are not yet certified. Full correctness of arbitrary schemas, tablespaces, extensions and failover is outside this fixture's scope. Local durations and file sizes are recorded, but actual network bytes, resource observers, calibrated workload capacity, matched-window application degradation, rsync/custom comparisons and controlled scale runs remain pending. Do not use these smoke timings to approve an optimization. See [the release gate and sequence](../benchmarks/BASELINE-GATE.md).

## Historical starter validation: September 30, 2026

The documentation was subsequently revised using the supplied sponsor-chat screenshots.
CORRECTNESS.md describes a proposed oracle and recovery contract, not a tested implementation.
No new implementation or database validation is claimed by that documentation update.

## Executed locally

- Python 3.12.14 standard-library experiment.
- Eleven unittest cases passed, including 100 seeded randomized edit trials and multiple edit-shape/chunk-size combinations.
- Exact reconstruction checked for all nine reported demo scenarios.
- Corrupt literal data, changed reused basis bytes, truncated transfer, invalid ranges, output overrun and wrong target digest are rejected by the educational reconstruction function.
- Raw demo output is saved in [example-results.json](../experiments/example-results.json).

## What the example shows

An 8 MiB deterministic random byte string is divided into 1,024 synthetic 8 KiB pages.
The experiment changes one byte in each selected page.
This is not a PostgreSQL workload.

| Changed synthetic pages | Fixed chunk | Literal bytes | Estimated signatures + instructions + literals | Estimated reduction versus raw full bytes |
|---|---|---|---|---|
| 0 / 1,024 | 8 KiB | 0 | 99,328 | 98.82% |
| 10 / 1,024 | 8 KiB | 81,920 | 181,168 | 97.84% |
| 10 / 1,024 | 64 KiB | 655,360 | 667,696 | 92.04% |
| 1,024 / 1,024 | 8 KiB | 8,388,608 | 8,479,744 | -1.09% |

The signature/instruction format is an illustrative cost model, not an implemented wire protocol.
Session metadata, file inventories, transport framing, WAL, retries and CPU/disk/capture costs are excluded.
These percentages must not be presented as database backup or network benchmark results.

The one-byte prefix insertion counterexample loses fixed-chunk reuse on this random data.
That demonstrates a reason to investigate rolling matching or CDC for shifted files, not a conclusion about PostgreSQL relation files.

## Not executed at the September 30 checkpoint

- PostgreSQL full or incremental backup, pg_combinebackup, pg_verifybackup, or database restore.
- AlloyDB Omni compatibility or HA deployment.
- Source-side helper, live-source consistency, real transport, fsync/atomic publication or resource-bound implementation.
- Actual network bytes, CPU overhead, disk I/O or end-to-end performance.

Docker is installed on this Windows host, but the Docker daemon was not reachable during inspection.
No cloud resources were provisioned.
At that checkpoint the baseline runbook remained an unexecuted first team task. The October 9 section above supersedes that status for native smoke integration only.

## Publication status

The starter is published at [MattyWeee123/pg-delta-backup](https://github.com/MattyWeee123/pg-delta-backup) on the main branch.
The repository is public by the project lead's choice.
The local repository tracks origin/main.
GitHub Actions results are available on the [Actions page](https://github.com/MattyWeee123/pg-delta-backup/actions).
The [publication workflow run](https://github.com/MattyWeee123/pg-delta-backup/actions/runs/36779762833) passed the experiment tests and demo on GitHub's Ubuntu runner.
The backlog remains draft issue text; GitHub issues and teammate write-access invitations have not been created.
