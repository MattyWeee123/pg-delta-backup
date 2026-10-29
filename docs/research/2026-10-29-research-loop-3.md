# Research loop 3: copies without WAL summaries (October 29, 2026)

The third pass of the research loop ([WORKFLOW.md](../WORKFLOW.md)), on the scorecard's remaining loss: a server that doesn't summarize WAL (`summarize_wal` off, PostgreSQL's default). Two candidates came out of it, both built and tested; the local scale model and the VM session follow.

## 1. The situations, the standard and the competitors

Without summaries, what the server still holds decides what can be done:

| Situation | Standard tool | Strongest competitors | pgdelta today |
|---|---|---|---|
| **(a) the WAL since B's redo point is still on S** (a replication slot, `wal_keep_size`, or a large `max_wal_size`) | The standby replays it (streaming or an archive) | WAL-G catch-up; `pg_probackup catchup` (DELTA) | Hashes both copies |
| **(b) that WAL is gone** (the usual reason a standby can't catch up) | A re-clone (`pg_basebackup`) | WAL-G catch-up; `pg_probackup catchup` (DELTA, or PTRACK with a patched server) | Hashes both copies |

**Measured so far, across sessions on the ha set (18 GB, beyond memory):**

| Cell | Hashing (EXP-80b) | WAL-G catch-up (EXP-81) | Result |
|---|---|---|---|
| hot | 87.9 s | 87.8 s | A tie |
| uniform | 180.1 s | 109.0 s | WAL-G wins, 0.61 |

## 2. The bottleneck, from the measurements

- **Hashing reads both copies in full,** one on each VM, then writes the changed pages to B.
- **In uniform, dest's disk was busy 157.5 s:** reading all of B and writing 5 GB on the same disk (EXP-80b).
- **WAL-G reads only S** (85 s of disk time, both cells) and writes on dest while it reads.

So the loss is B's read, on the disk that also takes the writes.

## 3. Systems or algorithm: the bound

**With no record of what changed, every page of S must be read at least once** to find which changed. So any such refresh takes at least |S| / R, about 86 s here (17.9 GB at the 210 MB/s the source disk gives one stream).
- **Hashing** reads B as well. In uniform, its writes then queue behind B's read on dest's disk. That is an algorithm cost, so no tuning of the hash path removes it (research loop 1 found the hash path at 85% of its ceiling).
- **A refresh that reads only S** is bounded by max(|S| / R, dest's writes): about 86–90 s in both cells. That is WAL-G's hot time, and 0.80–0.83 of its uniform time.

**So, in situation (b), pgdelta can reach parity with WAL-G, and beat hashing by about 2x for scattered changes, by changing the algorithm, but not beat WAL-G by much.** Nothing that must read all of S can.

**In situation (a), the WAL is a record of what changed.** Reading it costs time in proportion to the WAL since B's redo point, not to S:
- 0.4–0.55 GB in the hot cell;
- about 12 GB in the uniform cell (EXP-81's runs, from B's redo point to the backup's start).

There the bound is the WAL's size over the rate it is read, plus the summaries' fast path (1.7 s hot, 41.9 s uniform). That is far below |S| / R for local changes, and near it for scattered ones.

## 4. What the prior art does (primary sources)

The full notes, with every citation: [the prior-art report](#sources). The mechanisms:

| Tool | How it finds changed pages | What it reads | Its threshold, and conditions |
|---|---|---|---|
| WAL-G catch-up | Page LSN on S, in every file whose mtime differs from the standby's (always, for a `pg_basebackup` copy) | All of S | ≥ the standby's **checkpoint record**, not its redo point: it can miss hint-only changes, which is harmless |
| `pg_probackup catchup` DELTA | Page LSN on S, in every main-fork file | All of S ("read-only I/O pressure equal to a full backup") | ≥ the destination's **redo point** (lowered to its minimum recovery point or backup start if smaller); no checksum check |
| `pg_probackup` PAGE | Parses the archived WAL since the parent into a page map | The WAL | Needs every segment since the parent, in an archive |
| WAL-G delta maps | Built from each WAL segment at `wal-push` | The maps | Main forks only; falls back to a page-LSN scan if a map is missing |
| PTRACK | A shared-memory map of changed blocks, set by hooks in the storage manager | The map | A patched server; a small map gives false positives |
| PostgreSQL 17 incremental backup | WAL summaries | The summaries | `summarize_wal` on since the earlier backup; the free-space map always whole |
| pg_rewind | The target's own WAL since the common checkpoint | That WAL | Main forks only; other forks whole; needs checksums or `wal_log_hints` |
| Elsewhere | Percona's redo-log bitmaps (removed in 8.0.30), Oracle RMAN's block change tracking file, SQL Server's per-extent bitmap (DCM), VMware CBT | | XtraBackup's default incremental is a page-LSN scan of every page |

**Soundness:**
- **ARIES's pageLSN** (Mohan et al., TODS 1992), as PostgreSQL states it in `src/backend/access/transam/README`: a page carries the LSN of the last WAL record that changed it.
- **PostgreSQL's exceptions,** a page changing with its LSN unmoved:
  - hint bits;
  - `PD_ALL_VISIBLE` without checksums or `wal_log_hints` (`visibilitymap_set`);
  - visibility-map clears before 17.11 and 18.6;
  - the free-space map, which isn't WAL-logged;
  - unlogged relations;
  - new files under `wal_level=minimal`;
  - `CREATE DATABASE … STRATEGY FILE_COPY` and `ALTER DATABASE SET TABLESPACE`, which keep the old LSNs;
  - zero pages from relation extension.

**Papers:** no paper was found that compares a page-LSN scan, a WAL scan and hashing for refreshing a physical replica. Mohan & Narang (SIGMOD 1993) and Graefe & Kuno (PVLDB 2012) treat archiving and single-page recovery by pageLSN and the log.

## 5. The candidates (built October 29; tests pass)

**Candidate 1, `--fast-path page-lsn`, for situation (b):**
- **What was combined:**
  - WAL-G's and `pg_probackup`'s page-LSN choice;
  - `pg_probackup`'s threshold, B's **redo point**: B's end would miss pages a base backup or a crashed standby holds torn or not at all;
  - PostgreSQL incremental backup's and pg_rewind's rule that the visibility map, the free-space map and the init fork go whole;
  - pg_rewind's condition: data checksums, or `wal_log_hints` on B and on S;
  - pgdelta's own guard v2 (lineage proven by how B stopped, on S's timeline history; neither WAL-G nor `pg_probackup` proves B's end);
  - pgdelta's pipelined sending and gap filling.
- **How it runs:** a scanner thread reads each main-fork file of S once, a few files ahead of the sending thread, which reads the chosen pages again from the page cache. B is never read.
- **Tests:**
  - unit tests of the page test, the scan and the guard;
  - real-server refreshes of a cleanly stopped copy, a base backup and a killed standby, every copy checked with `pg_amcheck --heapallindexed`;
  - on a server that never summarized WAL, the summaries' fast path is refused and page-lsn runs;
  - in the same state, page-lsn's pages are a subset of the summaries' choice, as the pageLSN invariant implies.

**Candidate 2, summaries on demand, for situation (a):**
- **The mechanism:** when `summarize_wal` is turned on with no summaries yet, PostgreSQL 17's summarizer starts at the **oldest WAL still on disk** (`walsummarizer.c`, `GetOldestUnsummarizedLSN`), not at the present. So switching summaries on at refresh time builds, from the retained WAL, the summaries that the existing fast path and its guard need.
- **No code of pgdelta's changes.** The operator turns the setting on (it takes a reload, not a restart). Where the WAL since B's redo point is gone, the guard finds the gap and refuses, as before.
- **Tested** on a real server that never summarized WAL, keeping its WAL (`wal_keep_size`): after the setting was turned on, the summaries covered the old copy's redo point, the fast path ran, and the copy recovered to the source.
- **Its cost** is reading the WAL since B's redo point: the bound above. This is the mechanism of Percona's bitmaps, WAL-G's delta maps and `pg_probackup` PAGE, done after the fact by PostgreSQL's own summarizer, so no WAL parser of pgdelta's is needed. I found it in the summarizer's source, not in the documentation.

**Which one to use:** the WAL since B's redo point (S's current LSN minus B's redo point, known before any page is read) against |S|. Summaries on demand when the WAL is much smaller; page-lsn otherwise, or when the WAL is gone.

## 6. What follows

1. **The local scale model (rule 13), pre-registered as EXP-82:**
   - randomized correctness trials of both candidates on the Mac: cleanly stopped standbys, base backups and crashed standbys, under a load with DDL, truncations, `VACUUM FULL` and new databases, each copy checked;
   - a negative control: the threshold moved from B's redo point to its end, which must give wrong copies where a base backup was taken under writes;
   - timings against hashing.
2. **The VM session (EXP-82b),** if the local stage passes:
   - page-lsn against hashing and WAL-G catch-up, and summaries on demand against both, in hot and uniform on the ha set beyond memory;
   - the source without summaries, its WAL kept.
3. **Reported beside them:** what `summarize_wal` costs a busy server. If it costs little, the best advice for situation (a) and onward is to turn it on, and pgdelta's summaries path already wins there (EXP-81).

## Workflow check

| | Check | For this pass |
|---|---|---|
| 1 | Situation, standard, competitor | Two situations (WAL kept or gone), each with its standard and competitors (section 1). Hashing loses to WAL-G for scattered changes, across sessions |
| 2 | The bottleneck | B's full read on dest's disk, behind the writes (EXP-80b's disk counters) |
| 3 | Systems or algorithm | Algorithm: read S only (bound \|S\| / R), or read the WAL instead of S where it is kept |
| 4 | Papers and prior art | Section 4: WAL-G, `pg_probackup`, PTRACK, PostgreSQL's incremental backup and pg_rewind, from source; ARIES's pageLSN; other systems. No paper compares the three approaches |
| 5 | Committee-grade evidence | The bound is arithmetic from measured rates; the candidates are tested; EXP-82 and EXP-82b are to be pre-registered |
| 13 | The cascade | Tests done; EXP-82 is the local stage; promotion at a local ratio of 0.80 or less against hashing, and the pair logged |
| 14 | The idea archive | Read. Two structurally different ideas, not tuning: page-LSN selection and summaries on demand. Archived beside them: mtime-based skipping of S's files (pgBackRest's and rsync's shortcut), unsound without trusting the clock |
| 15 | Held-out workloads | Not owed: no default changes |
| 16 | Scorer integrity | Each registration records its files. The negative control: the threshold at B's end |
| 17 | The noise bar | 2.40% (10 s or more) and 7.11% under; the effects bounded above are far larger |

## Sources

- **The prior-art report:** a research pass over primary sources on October 29, read at PostgreSQL `REL_17_STABLE` 85a3871, wal-g 924c478, pg_probackup 8f52525 and ptrack cf5fc9b.
- **The citations:** the WAL-G and `pg_probackup` documentation and source files named above; PostgreSQL's `walsummarizer.c`, `basebackup_incremental.c`, `visibilitymap.c`, `freespace/README`, `transam/README` and pg_rewind's `filemap.c` and `parsexlog.c`; the Percona, Oracle, Microsoft and Broadcom documentation for section 4's last row.
