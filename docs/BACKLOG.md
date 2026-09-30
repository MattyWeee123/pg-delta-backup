# Initial backlog

These are proposed issue bodies, not evidence that GitHub issues have been created.
Assign actual people in the sync.
P0 means the first iteration depends on the answer or result.

## 01 - Confirm sponsor use case and deployment constraints

Priority: P0.
Owner: Matt.
Dependencies: none.
Due: Oct 1.

- Review BEN-QUESTIONS.md with Ben.
- Record basis type, target versions, source-helper permission, staging acceptance and the primary metric.
- Record repo ownership/license rules and final demo date.

Done when the decision log contains answers or named blockers, with no assumptions presented as sponsor approval.

## 02 - Reproduce native full and incremental backup/restore

Priority: P0.
Owner: backup/recovery role.
Dependencies: a disposable PostgreSQL 17 test environment.
Due: Oct 2.

- Capture B0, apply deterministic changes, capture B1 and I1.
- Combine B0 + I1, verify the outputs and restore disposable copies.
- Preserve commands, versions, summaries configuration and expected SQL checks.
- Demonstrate the missing-WAL-summary prerequisite failure on disposable test state.

Done when another teammate reproduces a successful restore from the runbook and understands the prerequisite failure.

## 03 - Freeze fixtures and chunk comparison

Priority: P0.
Owner: chunking/matching role.
Dependencies: synthetic fixtures immediately; database fixtures from 02 later.
Due: Oct 2.

- Run the educational experiment, then replace its toy inputs with immutable file fixtures through a separate research tool.
- Compare 8 KiB and 64 KiB fixed chunking.
- Report signature bytes, reuse, scan cost and memory estimates.
- Add a byte-shift fixture and explain why it does not itself represent PostgreSQL updates.

Done when the recommendation links raw results and names a specific CDC experiment rather than assuming CDC wins.

## 04 - Approve module and protocol contracts

Priority: P0.
Owner: transfer/reconstruction role.
Reviewer: matching owner.
Dependencies: 01 for deployment; SPEC.md for provisional implementation.
Due: Oct 2.

- Specify session/file/COPY/DATA/completion encodings, versioning, bounds and errors.
- Define immutable-input ownership, memory bounds and output publication.
- Define fixtures usable by both sender and receiver implementers.

Done when both module owners implement against the same contract and one reviewer walks through interruption and corruption cases.

## 05 - Implement bounded fixed-chunk sender and receiver

Priority: P1.
Owner: matching and transfer roles; name one integration owner.
Dependencies: 03, 04 and language decision.
Due: first local path Oct 7; transport Oct 14.

- Reuse basis chunks and send literals for misses.
- Bound memory per file/window and validate reused bytes.
- Reconstruct into a new staging directory and verify every completed file.
- Handle zero-length, append, truncate, delete, rename, rewrite and unchanged fixtures.

Done when output matches the source inventory and contents and basis bytes remain unchanged.

## 06 - Integrate backup verification and real restore

Priority: P0.
Owner: backup/recovery role with Matt integrating.
Dependencies: 02 and first path from 05.
Due: Oct 7.

- Rebuild B1 from B0 using the new path.
- Preserve backup metadata and required WAL.
- Run pg_verifybackup and restore a disposable copy.
- Check deterministic data and schema.

Done when the integrated demo passes G2/G3 from SPEC.md; a hash-only demo is insufficient.

## 07 - Build fair measurement harness

Priority: P0.
Owner: workloads/measurement role.
Dependencies: none for design; 02/05 for integrated runs.
Due: plan Oct 2; first records Oct 7.

- Follow benchmarks/README.md and version the run-record format.
- Implement full, rsync and native incremental baselines before custom comparisons.
- Capture both-direction traffic, end-to-end time and component costs.
- Label unavailable values and synthetic versus actual measurements.

Done when a second teammate reproduces one result and checks the timing/traffic boundary.

## 08 - Add failure and resource-limit tests

Priority: P1.
Owner: transfer/reconstruction role.
Dependencies: 05.
Due: Oct 14, expanded through Nov 11.

- Corrupt a reference and a literal, truncate a transfer and simulate a write failure.
- Reject wrong cluster, changing inputs, unsafe paths, unsupported links/tablespaces and oversized frames.
- Verify no partial result is published and the old basis remains valid.

Done when failure injection is automated and reviewers can reproduce each failure without real database data.

## 09 - Evaluate CDC and source integration options

Priority: P1.
Owner: matching role with Matt and Ben reviewing.
Dependencies: stable fixed-chunk baseline and 01.
Due: Oct 21.

- Compare fixed chunks with a specified CDC candidate on representative relation files.
- Evaluate staged, streamed-helper and backend integration obligations.
- Document positive and negative results, including CPU and source I/O costs.

Done when a decision record selects the next architecture with evidence and sponsor agreement.

## 10 - Validate Omni and prepare final demonstration

Priority: P1.
Owner: backup/recovery role; Matt coordinates sponsor access.
Dependencies: exact target from 01, 06 and approved budget.
Due: compatibility checkpoint Nov 4; rehearsal Nov 18.

- Confirm the HA installation action from the original notes and its role in testing.
- Repeat agreed restore tests on the specified Omni deployment.
- Prepare a clean-environment demo, limitations, results and final design explanation.

Done when the target environment is tested or its blocker is explicitly documented without claiming compatibility.
