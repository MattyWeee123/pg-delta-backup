# Lead briefing

Prepared September 30, 2026, from the supplied project notes, Google Chat screenshots and primary technical sources.
Sponsor direction below comes from those screenshots; the implementation choices and detailed correctness contract remain team proposals.

## What the chats settle

Ben expects a benchmark that runs a database workload while measuring backup duration, network traffic and application performance degradation.
He expects the team to define small/medium/large scales and propose feasible mid-semester and final deliverables.
He explicitly accepts outputs that differ physically from pg_basebackup, including when backups finish at the same time.
The desired outcome is recovery with no lost committed data within an explicit recovery boundary, not merely a database that starts.
The exact boundary and validation procedure still need a precise team proposal.

Sam and Uttansh have already raised the key correctness questions.
The next step is to turn that discussion into executable checks and assigned deliverables.
Propose Sam for benchmark/validation design and Uttansh for the recovery contract, subject to their availability.

Read [CORRECTNESS.md](CORRECTNESS.md) for the proposed test approach.
Avoid claiming that the wall-clock time when a command returns is automatically the database recovery cutoff.

## What you are trying to solve

Imagine the source has a large database and another machine already has last week's copy.
A full backup sends the whole new copy.
Your project explores whether we can reuse bytes from last week's copy and send only what is missing, while still producing a database that can recover correctly.

For a hypothetical 1 TiB copy with 10 GiB of reusable-chunk misses, the transfer might approach 10 GiB plus signatures, instructions, metadata, and required WAL.
That is an illustration, not a measured result or a guarantee.
One percent of rows updated does not mean one percent of disk bytes changed.
Indexes, page headers, vacuum activity, and WAL all affect the result.

The project has three separate problems:

1. Obtain a valid physical backup of a running PostgreSQL cluster.
2. Transfer that backup efficiently using an older destination copy.
3. Prove that the result is recoverable, then measure its resource costs fairly.

Chunking and hashing address problem 2.
They do not solve problem 1 or prove problem 3.

## The existing solutions change the question

PostgreSQL already has native incremental backups in version 17 and later.
They identify changed relation blocks from WAL summaries and use a prior backup manifest.
The output requires reconstruction with earlier backups.
See the [incremental backup documentation](https://www.postgresql.org/docs/17/continuous-archiving.html#BACKUP-INCREMENTAL-BACKUP).

One possible distinction to investigate is discovering reusable bytes by examining file contents when historical WAL summaries are unavailable.
We still need the WAL required to recover the newly captured backup.
This is not a "backup without WAL" project.
Do not make that hypothesis the project mission without evidence.
Proceed with the confirmed active-workload comparison and ask only whether a particular customer use case should shape the design.

Do not claim that content-based delta transfer is new: [rsync already does it](https://rsync.samba.org/tech_report/node2.html).
[pgBackRest also provides block incremental backups](https://pgbackrest.org/configuration.html#section-repository/option-repo-block).
The valuable research may be a PostgreSQL-specific design, a better operational fit, or a careful characterization of when it wins and loses.

## Six terms you need to explain

| Term | Meaning in this project |
|---|---|
| Physical backup | Files representing the entire PostgreSQL cluster, with recovery metadata and required WAL |
| Basis | The older, frozen destination copy whose bytes may be reused |
| Chunk | A byte range within a file; our first candidate is a fixed-size block |
| Fingerprint | A digest used to find matching bytes; it is not a substitute for database validation |
| WAL / LSN | Recovery records / an address within those records |
| Reconstruction | Building a new output from references to basis bytes and literal new bytes |

PostgreSQL tables and indexes normally use fixed-size 8 KiB pages, although builds can use another size.
That motivates testing page-aligned fixed-size chunks before assuming content-defined chunking is better.
This is a design hypothesis based on [PostgreSQL's page layout](https://www.postgresql.org/docs/17/storage-page-layout.html), not a measured advantage.

## One correction to the Research tab

The notes describe WAL as a batch of updates that is compressed, applied once it fills, and then cleared.
That is not an accurate PostgreSQL model.
WAL records must be made durable before the corresponding dirty data pages are written.
Data pages can be flushed later, and recovery replays records as needed.
WAL recycling and retention depend on recovery, archiving, and replication requirements, rather than simply a buffer filling up.
Read the short [official WAL introduction](https://www.postgresql.org/docs/17/wal-intro.html).

## Your job at the next meeting

You do not need to arrive knowing every storage detail.
You do need to make the uncertainty explicit and leave with owners, evidence requirements, and a next demo.

Suggested opening:

> Ben has given us a clear deliverable: compare our backup approach with pg_basebackup under active load and demonstrate correctness.
> Sam and Uttansh have already clarified that byte equality is not the product requirement.
> Today let's turn that into a measurable recovery guarantee, a baseline experiment, and implementation tasks.
> Each of us will leave with one small deliverable, one reviewer, and a date.
> Our first shared milestone is a backup we can restore successfully.

Do not imply anyone has completed research they have not done.
Present this packet as a proposed starting point for team review.

## A 45-minute sync

| Time | Discussion | Required outcome |
|---|---|---|
| 0-5 min | Explain the problem using the older-copy example | Shared vocabulary |
| 5-12 min | Confirmed sponsor deliverable and proposed workload scales | Agree which comparisons will be shown |
| 12-22 min | Recovery cutoff, correctness oracle, source access and basis type | Adopt a testable first contract; flag deployment questions |
| 22-35 min | Assign the five workstreams in PLAN.md | Actual names, reviewers, deliverables, due dates |
| 35-42 min | Define the next demo and integration owner | One executable milestone, not five isolated documents |
| 42-45 min | Read back decisions and blockers | Written decision record |

## Use the remaining nine hours selectively

- First 45 minutes: read this briefing, run the synthetic demo, and explain the problem aloud.
- Next 45 minutes: review SPEC.md and mark anything you cannot explain.
- Next 30 minutes: prepare the concrete proposal and remaining deployment questions for Ben.
- Next 45 minutes: review the proposed assignments and repository content.
- Next 30 minutes: rehearse the opening and agenda, then stop expanding the plan.
- Leave the remaining time for replies, setup problems, other obligations, and rest.

You do not need to build a database backup engine before this sync.
The useful outcome is that tomorrow's work is precise and reviewable.
