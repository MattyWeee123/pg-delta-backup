# Research and code map

Checked September 30, 2026.
Version-specific PostgreSQL links intentionally use version 17 as a research baseline, not a claim that it is the newest version or the sponsor's chosen version.
Pin actual binaries, container digests and source commits when experiments run.

## What the evidence establishes

| Question | Finding | Consequence |
|---|---|---|
| Does PostgreSQL already support incremental backup? | pg_basebackup supports it starting with PostgreSQL 17 | Benchmark it; do not pitch incremental backup itself as new |
| What history does native incremental need? | A reference backup manifest and WAL summaries covering the interval between backup start LSNs | A content-based approach may serve a different availability model |
| Is incremental output directly bootable? | pg_combinebackup reconstructs a full backup using the required prior chain | Include combination costs in time-to-usable-backup comparisons |
| Can arbitrary live PGDATA be hashed/copied safely? | Live backup requires an established capture protocol and recovery WAL | Reuse standard capture first; hash completed immutable outputs |
| Are fixed chunks reasonable? | PostgreSQL tables/indexes generally use fixed-size pages | Compare page-aligned fixed chunks before deciding on CDC |
| Is hashing equivalent to validity? | pg_verifybackup has limits and test restores remain necessary | Validate files, WAL, actual startup, and data |
| Does another delta solution exist? | rsync uses basis signatures and sends references/literals; pgBackRest supports block incremental storage | Both inform baselines and novelty discussion |
| Does PostgreSQL success prove Omni support? | Google's Omni backup guidance supports community backup approaches, but a custom tool remains unvalidated | Confirm exact deployment and test the actual target |

## Existing approaches and fair comparisons

**Full pg_basebackup:** required correctness and end-to-end baseline.
Use a valid, complete backup including the necessary WAL, with equivalent compression and synchronization settings.

**Native incremental plus pg_combinebackup:** required PostgreSQL 17+ baseline.
Enable WAL summarization, retain the needed summaries and reference manifest, reconstruct the output, then verify and restore it.
Also record an explicit missing-summary case, where failure is a prerequisite limitation rather than a performance result.

**rsync over completed backups:** required practical delta-transfer baseline.
Use the same frozen source and basis as the custom transfer and verify the same final artifact.
Local rsync normally uses whole-file behavior; force delta mode where necessary and record command/version.
Do not use timestamp/size shortcuts as the correctness oracle for database fixtures.
Avoid `--inplace` against the sole basis copy.

**pgBackRest:** relevant mature prior art and, if feasible, an additional end-to-end baseline using repo-block and its required repo-bundle setting.
Its managed repository and backup history differ from the proposed arbitrary-basis experiment.
Explain prerequisite and output differences rather than claiming identical operating conditions.

**pg_rewind:** relevant when the scenario is a diverged copy of the same cluster, often an old primary after failover.
It has timeline, WAL and configuration prerequisites and is not a general-purpose binary diff utility.
Make it a benchmark only if Ben's selected use case matches those prerequisites.

## Architecture-changing observations

If all old-to-new WAL is still available and the goal is refreshing a replica, ordinary WAL catch-up may already be the correct solution.
If native incremental prerequisites are available, change tracking can avoid the full content scan our first design pays for.
If the network is fast and disk/hash work dominates, content matching can lose to a full copy.
If almost all content changed, signatures and scanning can be extra cost with little reuse.
These are hypotheses to measure, not reasons to hide unfavorable results.

Content-defined chunking can recover similarity after byte shifts in general files.
PostgreSQL page organization means that advantage must be tested on actual relation files, not inferred from a text-file example.
The FastCDC paper establishes an algorithmic candidate, not its superiority for this project.

## Focused source reading

Use the [PostgreSQL REL_17_STABLE tree](https://github.com/postgres/postgres/tree/REL_17_STABLE) initially and record a commit before reproducing results.
The paths below were checked in the upstream directory listings; this is a reading map, not a completed line-by-line code audit.

| Path or directory | Read to answer | Required research artifact |
|---|---|---|
| [src/bin/pg_basebackup](https://github.com/postgres/postgres/tree/REL_17_STABLE/src/bin/pg_basebackup) | Where does the client request, receive and write a backup? | Sequence sketch with client/server responsibilities |
| pg_basebackup.c, receivelog.c, walmethods.c in that directory | How are base data and WAL handled? What owns completion? | Note describing success, error cleanup and version assumptions |
| bbstreamer.h, bbstreamer_tar.c, bbstreamer_file.c in that directory | What processing occurs after bytes reach the client? | Identify which side of the constrained link each operation runs on |
| [src/backend/backup](https://github.com/postgres/postgres/tree/REL_17_STABLE/src/backend/backup) | Where are backup files selected and sent, and incremental selection implemented? | Candidate integration points and correctness responsibilities |
| [src/bin/pg_combinebackup](https://github.com/postgres/postgres/tree/REL_17_STABLE/src/bin/pg_combinebackup) | What work is needed to materialize a usable incremental result? | Baseline timing boundaries and reconstruction notes |

Do not begin by reading the entire PostgreSQL repository.
Trace one full backup and one native incremental backup, with the documentation open beside the code.

## Primary sources

1. [pg_basebackup, PostgreSQL 17](https://www.postgresql.org/docs/17/app-pgbasebackup.html): supported modes, permissions, output, WAL options and version compatibility.
2. [Incremental and low-level backup procedures](https://www.postgresql.org/docs/17/continuous-archiving.html): historical summaries, manifests, backup API, WAL and metadata obligations.
3. [pg_combinebackup](https://www.postgresql.org/docs/17/app-pgcombinebackup.html): reconstruction, chain dependencies and limitations.
4. [pg_verifybackup](https://www.postgresql.org/docs/17/app-pgverifybackup.html): manifest/WAL checking and why restores still need testing.
5. [WAL introduction](https://www.postgresql.org/docs/17/wal-intro.html): durability ordering and recovery.
6. [Page layout](https://www.postgresql.org/docs/17/storage-page-layout.html): page organization and usual page size.
7. [Database file layout](https://www.postgresql.org/docs/17/storage-file-layout.html): relation files, forks, segments and tablespaces.
8. [Streaming replication protocol](https://www.postgresql.org/docs/17/protocol-replication.html): BASE_BACKUP and source/client responsibilities.
9. [WAL summarization configuration](https://www.postgresql.org/docs/17/runtime-config-wal.html#RUNTIME-CONFIG-WAL-SUMMARIZATION): summarize_wal and summary retention.
10. [pg_rewind](https://www.postgresql.org/docs/current/app-pgrewind.html): divergence-specific reuse; check the selected major's documentation before testing.
11. [The rsync algorithm](https://rsync.samba.org/tech_report/node2.html) and [implementation overview](https://rsync.samba.org/how-rsync-works.html): signatures, references and literals.
12. [pgBackRest block incremental configuration](https://pgbackrest.org/configuration.html#section-repository/option-repo-block) and [user guide](https://pgbackrest.org/user-guide.html): existing block-level backup and restore features.
13. [FastCDC, USENIX ATC 2016](https://www.usenix.org/conference/atc16/technical-sessions/presentation/xia): content-defined chunking research.
14. [AlloyDB Omni container backup overview](https://docs.cloud.google.com/alloydb/omni/containers/17.9.0/docs/backup-overview): example versioned Omni backup guidance; Ben must select the actual target.

The user-provided meeting notes establish project intent and team dates.
They are not copied into this repository and are not authoritative documentation of PostgreSQL internals.
