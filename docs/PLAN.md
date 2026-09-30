# Team plan

Proposed September 30, 2026.
Dates are team targets in America/New_York, subject to sponsor confirmation.
The notes list five student participants; the plan therefore uses Matt plus four contributor roles without inventing assignments or availability.

## Workstreams to assign at the next sync

| Role | Owner | First deliverable | Due | Reviewer |
|---|---|---|---|---|
| Lead / integration | Matt | Decision record, Ben questions, issue owners, agreed definition of the next demo | Oct 1 | One teammate |
| Backup and recovery | Assign in sync | Reproducible native full and incremental capture/combine/restore transcript; required WAL and metadata inventory | Oct 2 | Matt |
| Chunking and matching | Assign in sync | Fixed-size chunk experiment at 8/64 KiB; estimate memory and signature costs; recommendation about CDC experiment | Oct 2 | Transfer owner |
| Transfer and reconstruction | Assign in sync | Versioned COPY/DATA contract, safe output lifecycle, corruption/interruption tests on synthetic files | Oct 2 | Backup owner |
| Workloads and measurement | Assign in sync | Deterministic workload, measurement schema, full/rsync/native incremental baseline matrix | Oct 2 | Matching owner |

Record actual names and available hours before leaving the meeting.
One owner is accountable for each deliverable, but owners may pair.
The lead owns integration and scope, not all unfinished implementation work.

## First 48 hours

1. Matt resolves or explicitly records Ben's P0 questions and approves temporary assumptions.
2. Backup owner proves a normal PostgreSQL backup can be restored on the test machine.
3. Matching and transfer owners review SPEC.md together and agree on data types and fixtures before writing separate modules.
4. Measurement owner starts baseline collection immediately, in parallel with engine work.
5. Everyone posts one artifact, one finding, and one blocker on their issue.
6. Matt reviews the integration path and cuts scope if the first restore is not working.

The first checkpoint is evidence, not "research done."
For example: a command transcript, a passing restore, an executable fixture, or a decision note with sources and a rejected alternative.

## First-week definition of done: October 7

- Shared test environment and recorded PostgreSQL build.
- A completed B0 backup and a completed B1 backup after deterministic changes.
- A first delta reconstruction of B1 using B0, initially local if necessary.
- Manifest/WAL verification and isolated restore with SQL checks.
- Measured literal/reuse/signature quantities; explicit distinction from real wire bytes.
- A failing corruption/interruption test that leaves B0 intact.
- A reviewed decision on source-side deployment and core language.

If transport across hosts is not ready, show the local reconstruction honestly and keep network claims out of the demo.
If restoration is not ready, the milestone is incomplete even if chunk hashes look correct.

## Proposed milestone sequence

| Target | Milestone | Exit evidence |
|---|---|---|
| Oct 2 | Native baselines and scope gate | Successful native restore; P0 answers or explicit blockers |
| Oct 7 | First complete reconstruction | B1 rebuilt from B0; verification and SQL restore checks |
| Oct 14 | Real transfer and bounded memory | Separate sender/receiver, measured link bytes, failure cleanup |
| Oct 21 | Source integration decision | Staged versus streaming/native route chosen with Ben; live capture obligations documented |
| Oct 28 | Comparative experiment set | Repeatable full/native incremental/rsync/custom runs; unfavorable cases included |
| Nov 4 | Omni compatibility checkpoint | Target version installed by approved owner; restore demonstration or documented compatibility blocker |
| Nov 11 | Robustness and feature freeze | Failure matrix, supported-input documentation and limitations |
| Nov 18 | Rehearsal | Reproducible demo from a clean environment, result plots, design rationale |
| Nov 20 | Proposed sponsor-ready demo | Confirm exact date with Ben; retain late-November/early-December buffer |

The original notes describe a roughly nine-week effort and a final demo window spanning late November to early December.
These dates are a proposal, not a commitment from the sponsor.
HA setup from the original notes remains a tracked work item; ask Ben whether it is a prerequisite or a parallel learning exercise.
Do not silently discard it, but do not let an unneeded Kubernetes deployment block the first PostgreSQL restore.

## Weekly operating rhythm

- Before sync: each owner links an artifact and updates their issue.
- During sync: run the integrated demo first, then decide blockers and next owners.
- After sync: Matt records decisions, due dates, and the next demo in one issue or note.
- PRs include validation and limitations; one teammate reviews before merge.
- A blocker that persists for one working day is escalated with a concrete question and fallback.

## Gates and fallback decisions

| Trigger | Response |
|---|---|
| Ben requires no source-side helper and no server changes | Stop assuming a custom content-hash protocol is deployable; assess native incremental or another approved mechanism |
| Ben requires upstream PostgreSQL contribution | Move the core integration to C, create a scoped PostgreSQL fork, and reduce other features |
| CDC has no benefit on representative relation files | Keep fixed-size matching and document the negative result |
| Large signatures exhaust memory | Bound work by file/window or spill the index; measure lost reuse and extra passes |
| Network saving does not improve total time | Report the tradeoff and identify the network/churn regime where it is useful |
| Omni unavailable | Continue PostgreSQL work and track Omni validation as incomplete; do not claim compatibility |

## Decisions record template

```text
Date:
Decision / question:
Evidence:
Alternatives:
Chosen option and reason:
Owner:
Acceptance test:
Revisit trigger:
Sponsor confirmation required / received:
```
