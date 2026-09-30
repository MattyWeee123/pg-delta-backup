# Validation performed

Date: September 30, 2026.

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

## Not executed or established

- PostgreSQL full or incremental backup, pg_combinebackup, pg_verifybackup, or database restore.
- AlloyDB Omni compatibility or HA deployment.
- Source-side helper, live-source consistency, real transport, fsync/atomic publication or resource-bound implementation.
- Actual network bytes, CPU overhead, disk I/O or end-to-end performance.
- GitHub Actions execution; the workflow is prepared but has not run here.

Docker is installed on this Windows host, but the Docker daemon was not reachable during inspection.
No cloud resources were provisioned.
The baseline runbook therefore remains an unexecuted first team task.

## Publication status

The starter is published at [MattyWeee123/pg-delta-backup](https://github.com/MattyWeee123/pg-delta-backup) on the main branch.
The repository is public by the project lead's choice.
The local repository tracks origin/main.
GitHub Actions results are available on the [Actions page](https://github.com/MattyWeee123/pg-delta-backup/actions).
The backlog remains draft issue text; GitHub issues and teammate write-access invitations have not been created.
