# Research loop 4: a primary that crashed, proven by the end of its own WAL

October 30, 2026. The fourth pass of the research loop (rule 12). It takes the one situation where pgdelta still loses.

## 1. Situation, standard and competitor

**The situation:** the old copy B is a primary that crashed. Its `pg_control` says "in production": no shutdown checkpoint, no minimum recovery point. The common case is an unplanned failover: the primary dies, its standby is promoted, and the old primary comes back with its disks.

| | Today |
|---|---|
| The standard | Re-clone with `pg_basebackup`, or `pg_rewind` |
| `pg_rewind` | Runs crash recovery on B first (single-user `postgres --single -F`), then rewinds from the end crash recovery found (`pg_rewind.c:335-339`, `ensureCleanShutdown`) |
| pgdelta | Refuses the fast path ("its end can't be proven") and hashes both copies: at best a tie with a re-clone beyond memory |

## 2. The bottleneck

**The obstacle is the proof, not speed.** The fast path is safe only if it knows where B's history ends:
- **Same timeline:** every page B changed since its redo point must be in the server's change list.
- **After a failover:** B's own changes past the fork must be found and overwritten.

A clean stop records the end in a shutdown checkpoint, and a standby records it as its minimum recovery point. A crashed primary records neither.

EXP-80 showed why crash recovery first is no proof. On an old primary that had lost its WAL past the fork (S2e), crash recovery replayed the WAL that was left, marked B "shut down", and the copy came out wrong in 3 of 3 builds. PostgreSQL itself doesn't tell lost WAL from a natural end: on a primary, recovery simply ends at the first record it can't read (`xlogrecovery.c` around 3647, `XLREAD_FAIL`).

## 3. Systems or algorithm: what a proof needs, and what it costs

**Three ways to bound B's end:**

| Proof | Sound? | Cost |
|---|---|---|
| Crash recovery first (`pg_rewind`) | **No:** trusts whatever WAL is left (EXP-80's S2e) | Replays B's WAL |
| Scan every page of B for its LSN | Yes, without trusting the WAL | Reads all of B: **ties hashing** beyond memory (the lower bound for a proof that trusts nothing) |
| **The end of B's own WAL, checked (this pass)** | Yes, under PostgreSQL's own durability rules, with the checks below | Reads B's WAL since its last checkpoint, plus a few page headers |

**Why the end of B's WAL bounds its pages.** PostgreSQL flushes WAL before it writes any page that WAL changed (`bufmgr.c` `FlushBuffer`, 3910-3927: `XLogFlush(recptr)` before `smgrwrite`). So every page on B's disk carries an LSN at or before the end of B's durable WAL.

**What the end is, exactly.** It is where the last valid record ends, as `XLogInsert` returns it: `pgdelta.fastpath.wal_record_end`. A test checks that, over real WAL, every computed end is exactly where the next record begins, across page and segment boundaries and a segment switch.

**When the end can be trusted (`pgdelta.fastpath.crashed_wal_end`).** Reading from B's redo point must stop the way a crash stops:
- **Inside a segment file that is there and whole.** PostgreSQL never creates a short segment (`XLogFileInitInternal` zero-fills or extends it before a durable rename). It never removes a segment from the redo point's on until a checkpoint past it (`RemoveOldXlogFiles` after `UpdateControlFile`). So a missing or short segment is damage. This catches EXP-62's and EXP-80's lost WAL.
- **With no WAL of the same generation past the stop.** That means no page after the stop, in that file or at the start of any later one, whose header carries its own address and B's magic.
  - After a crash, the space past the end holds zeros or a recycled segment's older pages ("unexpected pageaddr", `xlogreader.c` 1314-1335).
  - `XLogWrite` fsyncs a segment before it writes the next one (`xlog.c` 2469-2482), so current WAL in a later file means damage.
  - Inside the end's segment, valid-looking pages past the stop can also be natural:
    - a multi-page write whose later page reached disk before an earlier one;
    - stale pages left by an earlier crash in the same segment.

    These are refused too. A false refusal falls back to hashing, so it costs speed, never correctness.
- **With fsync on in B's configuration.** With fsync off, nothing orders WAL and pages on disk.

**After a failover,** B's end is compared with the server's fork point, as for a cleanly stopped old primary. If B ran past the fork, `--diverged-basis` (idea 1, EXP-69 to EXP-71) lists every block B's own WAL names from the checkpoint before the fork to that end, and sends the server's copy of each.

**What this can't see (stated, and measured in EXP-89's blind-spot arm):**
- **A pg_wal replaced by an older copy of itself.** It ends the way a crash does, while B's pages hold later writes. Only a scan of B's pages would catch it.
- **Storage that loses WAL it reported flushed.**

PostgreSQL's own crash recovery and `pg_rewind` are exposed to both as well.

**One exception to WAL before data, and why it is harmless here.** PostgreSQL 17's `bulk_write.c` (index builds, table rewrites) writes the pages of a *new* relation file with `log_newpages` and no `XLogFlush` first. So such a file can reach disk with LSNs past the durable WAL.
- **On the same timeline,** the server continued from the same directory, so the file is the same on both sides.
- **After a failover,** a file only B created is one the server lacks, and pgdelta removes it. The server's own new files are sent whole.

## 4. Papers and prior art

- **PostgreSQL's WAL reader** (`xlogreader.c`) checks each page header (magic, address, timeline), each record's length, prev-link and CRC, and each continuation. It treats the first failure as the end, which is right for recovery and is the gap here.
- **`pg_rewind`** requires checksums or `wal_log_hints` on the target (`pg_rewind.c:754-758`). It trusts crash recovery's end, then reads the target's WAL from the last common checkpoint (`extractPageMap`).
- **ARIES and every write-ahead-logging system** rest on the same WAL-before-data rule (pageLSN at or before the flushed log).
- **The source reading** for this pass is at PostgreSQL `REL_17_STABLE`, fetched October 30, 2026.

**What is new:** using the old primary's own WAL tail, checked for loss and damage rather than replayed, as the proof of its end. Combined with the server's WAL summaries and idea 1's diverged-block list, this refreshes a crashed old primary in place.

## 5. Committee-grade evidence: the plan

1. **Tests:**
   - the record-end arithmetic against real WAL;
   - a crashed primary taken, and refused without the option;
   - a crashed old primary after a failover taken, with its own blocks found;
   - a lost tail, a short segment and a damaged record refused;
   - fsync off refused.
2. **EXP-89, local:** a randomized campaign:
   - crashed primaries on the server's timeline and after a failover;
   - controls with lost, damaged and lost-after-failover WAL;
   - a blind-spot arm.
3. **EXP-89b, the VMs:**
   - real power loss (a hard reset, as EXP-34 did), where torn WAL tails happen, which a process kill doesn't produce;
   - the timing against hashing, a re-clone and `pg_rewind` on an 18 GB crashed old primary.
4. **A held-out session** before `--crashed-primary` becomes a default (rule 15).

## Workflow check

| | Check | For this pass |
|---|---|---|
| 1 | Situation, standard, competitor | Crashed primary: re-clone or `pg_rewind`; pgdelta ties at best today |
| 2 | The bottleneck | The proof of B's end (EXP-80's S2e), not speed |
| 3 | Systems or algorithm | Algorithm: a new proof; a scan of B is sound but ties hashing (the lower bound) |
| 4 | Papers and prior art | PostgreSQL's WAL reader and recovery, `pg_rewind`, ARIES's WAL rule, from source |
| 5 | Committee-grade | Tests built; EXP-89 pre-registered with controls and a blind-spot arm |
| 13 | The cascade | Tests, then local, then the VMs (power loss and timing) |
| 14 | The idea archive | New row: the crashed primary by its own WAL; crash recovery first stays rejected |
| 15 | Held-out workloads | Before a default: H9–H12 |
| 16 | Scorer integrity | Registered at EXP-89 |
| 17 | The noise bar | Correctness counts here; timing in EXP-89b |
