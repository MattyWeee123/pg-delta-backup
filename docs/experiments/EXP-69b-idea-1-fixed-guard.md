# EXP-69b: With its guard fixed, does idea 1's copy come out right on every failover it accepts, and is the checksum condition necessary?

Status: **pre-registered October 28, 2026, before any run.** Local, counted, not timed; minutes.

It follows from EXP-69's part A. As registered there, the deciding prediction failed. Post-hoc checks traced the three failures to the guard and the checks, not to idea 1's send set:
- **the guard's end check:** it compared the start of B's last WAL record with a standby's end pointer, and so refused a valid S2c;
- **its hint check:** it flagged hints from common history;
- **its control:** `pg_rewind` skips blocks past either side's end of file by design.

EXP-69 named three things to fix and re-test before any of it counts: the end check, a hint check that tests each transaction's fate on S, and a scenario that makes the hint danger real. This is that test, with one addition: idea 1's copy is built, started, and its rows compared with S's.

## Question

Idea 1 sends S's current copy of every block either side changed since their last common checkpoint C:
- **B's side:** the blocks B's WAL names from C's redo;
- **S's side:** the blocks S's WAL summaries list from C's redo to S's start.

Its guard refuses unless all of these hold:
- B and S match (cluster, version, checksums), with checksums or `wal_log_hints` on;
- B's end is proven by how B was stopped;
- C is in B's WAL, and that WAL reads through B's end;
- S's summaries cover the range.

**With the end check fixed:**
1. Is the copy right wherever the guard passes, judged by its rows once started, not only by its pages?
2. Does the guard refuse every case it must?
3. Is the checksum condition necessary? Without it, can a hint bit B set after the fork, in no WAL, leave the copy's rows wrong?

## Design

The builds use EXP-03's harness as in EXP-69's part A: PostgreSQL 17.11, S's summaries on, fresh builds in `results/exp69b`. Driver: `union_check.py results/exp69b --exp69b`.

**kept,** with `wal_keep_size = 1GB` on B's cluster:
- **S2:** an old primary stopped cleanly;
- **S2b:** the same, crashed;
- **S2c:** a standby that followed the old primary past the fork;
- **S2d:** S2b with its WAL lost;
- **S2h (new):** a transaction on P inserts 20,000 rows into a table of its own and stays open through a checkpoint and the fork (R stops). P then commits it and reads the table, setting `XMIN_COMMITTED` on every row, and stops cleanly. For S, the transaction never committed, so the rows don't exist. P has data checksums, so the hint bits are WAL-logged.
- **S2i (new):** S2h without data checksums or `wal_log_hints`, so the hint bits are in no WAL.

**default,** as EXP-61 built them: S2 and S2c.

**A copy of kept S2's B with its WAL cut** after the fork's segment.

For each build, with C found:
- **The guard:** B's WAL is checked to read from C's redo through B's end with `pg_waldump --end` (the fix).
- **The send set:** scored page by page against S, as in EXP-69.
- **Contradicted hints:** pages left unsent are searched for any hint bit B carries that S lacks and that S's own `pg_xact` contradicts. `XMIN_COMMITTED` or `XMAX_COMMITTED` on a transaction S didn't commit counts, as does `XMIN_INVALID` or `XMAX_INVALID` on one it did. `pg_xact` is read from the cleanly stopped source's files.
- **Idea 1's copy is built** (`unionscan.materialize`):
  - S's non-relation files, including its `backup_label` and its backup's WAL;
  - S's relation files that B lacks;
  - every other relation file, built from B's pages, with S's page wherever the send set takes it, at S's length.

  The copy is then started, recovering from S's backup, and its rows compared with S's (the harness's `restore_and_compare`).
- **The control:** `pg_rewind --dry-run --debug`. Each block `pg_waldump` names that `pg_rewind` doesn't take is placed against both sides' file sizes.

**The analysis,** committed before the data: `analysis/exp69b.py`, data `docs/experiments/data/exp69b-builds.csv`, figure `exp69b-union`.

**Checked before registering:**
- **Unit tests:** 15 in `tests/test_tricky_unionscan.py`. Among them: the fixed guard, reading `pg_xact`, the fate rule, building the copy, and placing a block.
- **The analysis was dry-run on synthetic results.**
- **A build smoke of S2h and S2i** in the scratchpad checked only their build checks, and no send set was scored. Both built:
  - the transaction committed on P, with 20,000 rows there;
  - 0 rows on S;
  - checksums 1 and 0, `wal_log_hints` off.

## Predictions

1. **Deciding:** wherever the guard passes, idea 1's copy starts and its rows equal S's, with no real wrong page and no contradicted hint left unsent. The guard passes kept S2, kept S2c, default S2c and kept S2h.
2. **The guard refuses each of these, for its reason:**
   - kept S2b and S2d: B wasn't stopped cleanly;
   - default S2: no checkpoint before the fork in B's own WAL;
   - kept S2i: neither data checksums nor `wal_log_hints`;
   - kept S2 with its WAL cut: B's WAL ends before B's end.
3. **The hazard is real:** in kept S2i, the union, scored although the guard refused it, leaves at least one page with a contradicted hint unsent, and its copy's rows differ from S's. This is what makes the checksum condition necessary, not merely inherited from `pg_rewind`.
4. **Control:** wherever the guard passes and `pg_rewind` runs, `pg_rewind` takes no block that `pg_waldump` doesn't name. Every block `pg_waldump` names that `pg_rewind` doesn't take lies beyond one side's end of file, or in a file one side lacks.
5. **Size:** wherever the guard passes, idea 1 sends at most 1.5 times the pages that differ.

**Reported:**
- the copies of the refused builds (S2b with its WAL intact; S2d with it lost);
- what S's summaries alone would leave (EXP-61's path);
- `pg_rewind`'s exit and size on each build.

All five predictions are exact counts or bars, so rule 11 doesn't apply.

## What follows

- **If 1–3 hold:** idea 1's send set and guard are correct on these failovers, the hint hazard included, and the checksum condition is shown to be needed. Its prototype in pgdelta is registered next, timed against `pg_rewind` on EXP-67's cells. That prototype's predictions use EXP-69's part B sizes.
- **If 1 fails:** the wrong pages or rows are traced before anything is built.
- **If 3 fails:** S2i didn't make the hazard real. The condition stays, as `pg_rewind`'s own, but the claim that it is needed is withdrawn.
