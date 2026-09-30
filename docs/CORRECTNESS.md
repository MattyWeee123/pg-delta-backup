# Proposed correctness and recovery contract

Status: team proposal based on sponsor chat, not an implemented or validated guarantee.

## What the sponsor clarified

The supplied screenshots show Ben selecting valid recovery with appropriate guarantees over byte identity with pg_basebackup.
He also says that simply being valid/restorable is too weak and that users should see no data loss.
Sam proposes backup verification, real startup and logical comparison at the same recovery point.
Uttansh proposes logical equivalence at a known point.
Ben supports that direction, while leaving the detailed measurement to the team.

## Proposed product guarantee

For each successful backup, report a cluster identifier, timeline, capture start/end positions, and a precise supported recovery cutoff R.
After recovery through R, the supported transactional data and schema must match the reference state at R.
Every transaction committed by that boundary is reflected, aborted/uncommitted effects are absent, and multi-row transactions are atomic.
Repeated updates, inserts and deletes must reproduce the final committed state, not merely a set of row counts.

This guarantee is bounded by R.
It does not promise recovery of transactions after R when the source later fails.
The tool must not report success until the required data, metadata and WAL for the claimed cutoff are durable and accessible in the declared recovery package.
The initial package is intended to be self-contained; dependence on an external archive must be explicit if introduced later.

Do not equate wall-clock command return time with R.
Track capture-end, recovery coverage and final publication time separately, especially for a staged transfer that finishes long after source capture.
Report coverage age at publication as a freshness metric.
Ask Ben whether that contract matches his completion-time expectation.

PostgreSQL documents the end of an online backup as the earliest consistent recovery point and provides named/LSN recovery targets.
See [recovery target settings](https://www.postgresql.org/docs/17/runtime-config-wal.html#RUNTIME-CONFIG-WAL-RECOVERY-TARGET).
The implementation must validate the mapping of its metadata to an actual recovery target, including timeline and inclusive/exclusive semantics.

## Three separate validation layers

| Layer | What is compared | What passing establishes |
|---|---|---|
| Frozen transfer | One immutable source backup and its reconstruction | Correct byte/inventory transfer for that fixture |
| Self-contained recovery | The output package alone and independently established state at its cutoff | Required data/WAL are present and the tested committed state is preserved |
| Controlled PITR comparison | Independent backups recovered to one explicit common target | Logical agreement at that target, with supplementary WAL dependencies disclosed |

The last layer must not conceal missing WAL in the self-contained package.
Run the self-contained test with no access to a source server or external WAL archive.
Byte equality is useful at the first layer and is not the final product requirement.

## Workload and independent expected state

Use synthetic transactions with stable operation identifiers and a ledger row written in the same transaction as each application change.
The workload client records outcomes outside the database, and the reference side retains an independent expected-state export.
Do not use only the restored ledger as the oracle: a broken backup could lose both the data change and its ledger row.

Start with insert/update/delete transactions, repeated updates to the same key and multi-row transfers with a conserved total.
Add explicit rollback transactions and a transaction left open across the recovery boundary.
Use deterministic per-worker operation streams and record actual committed outcomes.
The same random seed under concurrency does not guarantee the same commit order.
Sequence allocation and client request order are not commit-order oracles; sequence gaps are not evidence of data loss.
Connection loss can make commit acknowledgement ambiguous, so reconcile ambiguous outcomes on the reference before asserting an expected set.

For a direct comparison at cutoff R, establish the reference using an older valid reference backup plus independently retained WAL, restored to that exact target.
That reference may use its archive; the candidate self-contained restore may not.
Check the reference against external operation records and invariants so shared workload mistakes are not silently accepted.
This is an implementation task, not an existing harness.

## Practical first common-target test

The following makes the expected application state easy to establish without assuming wall-clock timestamp equality:

1. Run the instrumented workload while each candidate backup capture executes.
2. After the captures being compared finish, stop issuing application writes, drain in-flight transactions and reconcile outcomes.
3. Keep application/DDL writers fenced while exporting expected logical data and schema.
4. Create a unique named restore point R after both backups' minimum recovery points.
5. Ensure continuous archiving or an explicit WAL collector has durably retained every required segment through R; verify the restore-point segment is available.
6. Restore disposable copies with the same explicit timeline and named target, pause there, and compare against the expected state.
7. Include supplementary WAL in storage, traffic and timing accounting and label this a PITR-assisted comparison.

Application workload stays active during capture; the short fence is a validation barrier after capture, not a claim that backup requires stopping the database.
Run separate, matched performance trials so simultaneous competing backups do not contaminate individual performance measurements.
If methods use separate trials, each may use its own target and expected-state export under the same workload specification.

A post-capture restore point is not automatically contained in WAL bundled by pg_basebackup.
The collector/archive step is required for this particular test.
Use the documented [restore-point function](https://www.postgresql.org/docs/17/functions-admin.html#FUNCTIONS-ADMIN-BACKUP) and [target settings](https://www.postgresql.org/docs/17/runtime-config-wal.html#RUNTIME-CONFIG-WAL-RECOVERY-TARGET).

## Pass and fail criteria

- Matching-version pg_verifybackup succeeds when the output uses the PostgreSQL manifest format, including required-WAL parsing.
- PostgreSQL reaches the declared target; a target-not-reached error is a failure, not a shorter successful restore.
- Exact canonical ordered table exports match for small fixtures; use documented deterministic partitioned hashes and investigate mismatches for larger fixtures.
- Compare column names/types, constraints and other schema features in the supported test surface separately.
- Every expected operation is represented, rollback effects are absent, multi-row invariants hold, and checks detect an intentionally omitted committed operation.
- Missing/corrupt WAL, corrupt reused data and interrupted transfer are negative controls.
- The previous basis remains intact; incomplete output is never advertised as complete.

Do not claim that a finite set of SQL queries proves equality for every possible query or PostgreSQL object type.
Explicitly define supported schema/features, canonical encodings and limitations.
[pg_verifybackup requires complementary restore testing](https://www.postgresql.org/docs/17/app-pgverifybackup.html).

## Suggested ownership

Propose Uttansh for the recovery contract and Sam for benchmark/oracle design, with the backup and transfer owners implementing checks together.
Confirm roles in the sync; the screenshots are not evidence of accepted assignments.
Matt owns resolving the cutoff definition and making sure the integrated demo exercises it.
