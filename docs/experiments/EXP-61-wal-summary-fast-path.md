# EXP-61: Would a WAL-summary fast path ever leave a wrong page, and how much reading would it save?

Status: **pre-registered October 27, 2026, before any run.** Local only (the Mac, PostgreSQL 17.11 and 18.6). No VM, no change to pgdelta.

## Question

pgdelta hashes every page of both copies, so beyond memory its time is set by reading the old copy B (EXP-48). The owner asked on October 27 whether pages could instead carry a label, so the tool already knows what changed without another exchange with B.

PostgreSQL 17 keeps such labels as WAL summaries. With `summarize_wal = on`, a background process records which blocks of which relation forks each stretch of WAL touched. So a refresh could work from the summaries alone:
1. B reports one position: the redo point its recovery would start from (and its timeline).
2. S sends exactly the blocks the summaries list between that point and its own backup start.
3. Neither side reads, or hashes, any other page.

EXP-03 showed the simpler label, the page LSN, leaving stale or wrong pages in 4 of 8 same-cluster scenarios, including real stale data after a failover. Summaries are a different label:
- they come from the WAL itself, so a page whose change is logged can't be missed by a stale `pd_lsn`;
- they still can't see what was never logged: hint bits, `pg_checksums --enable`, an old primary's writes after a split.

Two paths are scored on EXP-03's tricky old copies:
- **Naive:** trust B's redo point and S's own timeline history.
- **Guarded:** use the summaries only when B's lineage is provable, and otherwise fall back to hashing everything, as pgdelta does now.

## Design

EXP-03's scenario builder, with the source cluster running `summarize_wal = on` from its creation (`run_scenarios.py build --wal-summaries`, new). Twelve scenarios:
- EXP-03's S1–S9;
- the two later variants of the failover case, S2b and S2c, whose old copy's redo point lies *before* the split.

Before the servers stop, the builder:
1. waits until the summarizer has passed S's start;
2. dumps every summary's blocks and limit blocks (a fork's truncation or creation), and S's timeline history.

**The guard** (all must hold, else hash everything):
- (a) B and S have the same system identifier;
- (b) the same major version;
- (c) the same data checksum version;
- (d) B's timeline is on S's history, and B's end lies within that timeline's stretch of S's history. B's end is the furthest of its latest checkpoint, its minimum recovery point, and the last WAL record in its own `pg_wal` (read with `pg_waldump`). This is the check `pg_rewind` makes before trusting a fork point. It catches an old copy that kept writing after the split even when its redo point is before it (S2b, S2c).
- (e) The summaries cover B's redo point to S's start without a gap.

**The summary path's pages,** following PostgreSQL 17's own incremental backup (`basebackup_incremental.c`):
- a relation file absent from B is sent whole, and so are free-space-map and init forks, and every non-relation file;
- main and visibility-map forks get:
  - the blocks the summaries list in the range;
  - every block from the lowest limit block in the range on;
  - every block past B's length.

**Ground truth:** every page of S is compared with B's page at the same place. A page that differs and isn't sent is **left wrong**. Each is sorted with EXP-03's classifier (`classify.py`):
- `hint_bits_only` (and `hint_bits_and_lsn`): differ only in bits PostgreSQL itself changes without WAL; harmless;
- anything else (`content`, `all_visible`, `vm`, `fsm`, `checksum_only`, `lsn_only`, `init`, `unclassified`) counts as a **real** wrong page.

**Reading:**
- the summary path reads only the pages it sends, on S;
- the hash path reads every page of S and of B.

This counts pages, not seconds. What reading fewer pages saves in time beyond memory is inferred from EXP-48's disk model, not measured here.

**Code:** `experiments/tricky_copies/summaryscan.py` (new), the dump in `scenarios.py`, and the analysis `analysis/exp61.py` with figure `exp61-summary-path`, all committed before the run.

## Predictions

1. **Deciding: the guarded path leaves no real wrong page in any of the 12 scenarios.** At most `hint_bits_only` or `hint_bits_and_lsn` pages, which PostgreSQL itself treats as equal.
2. **The guard refuses exactly S2, S2b, S2c** (diverged after a failover), **S6** (checksum version), **S8** (another cluster) and **S9** (another major version), and passes S1, S1b, S3, S4, S5 and S7.
3. **Without the guard, the summaries leave real wrong pages in S2, S2b, S2c and S6.** That is the same failure class as LSN skipping in EXP-03.
4. **Where the guard passes, the summary path reads at most half the pages the hash path reads,** and sends at most 1.25 times the pages that actually differ (plus files absent from B).

The counts are deterministic for a given build, so one build per scenario (as EXP-03) and no second session.

## What follows

- **If 1 and 2 hold:** a guarded summary path is safe on every tricky copy tested, and reads only the pages it sends. A fast path then becomes a design option for the owner, for old copies with provable lineage on servers with `summarize_wal` on. It is the one change that could beat the "at best a tie" HA verdict beyond memory. It is not built or turned on by this result: the design rule "LSNs and summaries never skip pages" (DESIGN.md) is the owner's to change.
  - **Caveats that stay:** Omni ships with `summarize_wal` off (EXP-40), so summaries exist only from when it is turned on. PostgreSQL 17 keeps them 10 days by default. Versions before 17.11 and 18.6 omit some visibility-map changes from the summaries (the bug fixed in August 2026), so the guard would also require a fixed server version.
- **If 1 fails:** the write-up names each real wrong page's scenario, relation and cause. A summary path is then unsafe even with the guard, and hashing stays the only path.
- **If 2 fails** (the guard refuses a safe copy, or passes a diverged one): the guard is revised, and the revision is pre-registered and re-run on the same builds before anything is concluded.
- **If 4 fails:** the summary path is safe but saves less than claimed. Its reading cost is reported as measured.
