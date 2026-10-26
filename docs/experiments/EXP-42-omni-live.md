# EXP-42: Is v1 safe on a busy AlloyDB Omni, given Omni's own relation fork?

Status: **pre-registered October 26, 2026, before any run.** OMNI-1b, lean: EXP-40's open point. One VM, about 25 minutes with set-up. The owner accepted the Omni container's license terms for this research use on October 26.

## Question

Live mode reads a running server's files while they change, and relies on recovery: every page changed during the backup is rewritten from the WAL, starting with a full-page image. EXP-40 found that Omni adds a relation fork community PostgreSQL doesn't have, `_mintxid`, which pgdelta copies as a plain file. Two questions:
- **The mechanism:** does Omni write `_mintxid` changes through the WAL, with full-page images, so recovery repairs a torn read of it like any page?
- **The outcome:** under a write load, do live refreshes of Omni give correct copies?

## Design

`experiments/omni/omni_live.sh` on one `n2-standard-4` VM (`PAIR=omni`), with Omni 17.10's container and its own binaries:
1. **Where the forks are:** after `pgbench -i -s 20`, every `_mintxid` file in the data directory, and whether pgbench's tables and indexes have one.
2. **The mechanism:** 10 s of pgbench, starting just after a checkpoint, with a slot holding the WAL.
   - The `_mintxid` files are hashed before and after, to see whether the workload changes them.
   - Omni's own `pg_waldump` counts the WAL's block references and full-page images by fork, over the workload's WAL.
3. **The trials:** 24 live refreshes in place.
   - **Each trial:** a fresh copy of B0 (S stopped after step 2, then started again) is refreshed while pgbench writes to S (4 clients).
   - **Controls:** every 5th trial (4 of 24) is pgdelta's deliberately broken variant (`PGDELTA_TEST_READ_BEFORE_START=1`, EXP-25's control), which reads S before `pg_backup_start` and always exits 4.
   - **The checks on each B,** started in Omni:
     - pgbench's invariant: the sums of account, teller and branch balances and of the history's deltas are all equal;
     - `bt_index_check` with `heapallindexed` on every btree index of the database, catalogs included;
     - `verify_heapam` on every table, then `VACUUM (FREEZE)`, then `verify_heapam` again (in case `_mintxid` steers freezing).
   - **Correct:** exit 0, B started, and every check passed. **Caught (a control):** any check failed or B didn't start; exit 4 alone doesn't count.
4. **The analysis,** committed before the data: `analysis/exp42.py`.

## Predictions

1. Recorded without a prediction: which relations have a `_mintxid` fork.
2. **The workload changes `_mintxid` files, and Omni's `pg_waldump` shows block references to that fork, with full-page images.** A persistent fork has to survive a crash, so Omni most likely logs it.
3. **Deciding: all 20 counted trials are correct.**
4. At least 3 of the 4 controls are caught.

## What follows

- **If 2, 3 and 4 hold:** live mode's argument carries to Omni. The fork is repaired by recovery like any page, and 20 trials found no wrong copy. The count is a weak bound on its own (below 35% at 99.9%); the mechanism carries the argument, with EXP-25's 1,000 trials on PostgreSQL behind it. G5 extends to live refreshes of a busy Omni.
- **If 2 fails** (the fork isn't in the WAL): live mode can't rely on recovery for `_mintxid`. Whether a torn or stale `_mintxid` is harmless is Omni's design, so it is recorded as an open risk and a question for Google (for the owner to forward). Omni live refreshes are then not claimed safe, whatever 3 shows.
- **If 3 fails:** live mode on Omni is unsupported until the cause is found.
- **If 4 fails:** the checks are too weak under this load, and 3 claims nothing.

## Limitations

- One VM, one load (pgbench, 4 clients), 24 trials; a container, not Omni on Kubernetes.
- What `_mintxid` is for is undocumented. The checks catch wrong data, broken indexes and broken heap pages, and freezing is exercised once per trial. A stale hint that only matters much later (at wraparound, say) would not show.
