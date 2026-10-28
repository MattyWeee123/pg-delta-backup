# EXP-69: For an old primary after a failover, is idea 1's send set correct, and what makes `pg_rewind` copy so much?

Status: **pre-registered October 28, 2026, before any run.** Part A runs locally, in minutes. Part B is one VM pair (the `ha` set), about an hour, after EXP-68. It follows from EXP-67's "what follows": idea 1 is next for the failover case, after a measurement of what it rests on.

## Question

After a failover, the old primary B has written past the fork, so pgdelta's fast path must refuse it (EXP-61, EXP-63) and hash both copies. EXP-67 timed that against `pg_rewind` for an 18 GB database beyond memory:
- for hot rows updated 10 times on the new primary S, `pg_rewind` took 36.4 s against pgdelta's 90.2 s;
- for 1% of rows scattered, it took 459.0 s, 84% of it replay; pgdelta took 177.9 s and a re-clone 91.1 s.

**Idea 1** (research loop 1, section 4) would send S's current copy of every block either side changed since their last common checkpoint C, and replay nothing of S's history:
- **B's blocks:** those its own WAL names from C on, as `pg_rewind` takes them;
- **S's blocks:** those its WAL summaries list from C on, as the fast path takes them.

Its inferred time for hot10 is about 5 s. Three things it rests on are unmeasured:
1. **Correctness:** does the union leave any page wrong? Hint bits matter here. On an ancestor they are harmless (EXP-61), but a diverged B may have set one for a transaction whose fate differs on S. `pg_rewind` requires checksums or `wal_log_hints` for that reason, and so does idea 1.
2. **Its guard:** it must refuse when it can't see all of B's changes, for example when B crashed, or when its WAL doesn't reach back to C or forward to its end. EXP-62 found a blind spot of this kind in the fast path's first guard.
3. **Its size against `pg_rewind`'s copy:** EXP-67's `pg_rewind` copied 353 and 4,077 MB beyond S's WAL since the fork, unexplained. B's own WAL since the fork was 158 MB in uniform. With `full_page_writes` on, B's first change to each page after C carries a full-page image, so that WAL can name at most about 20,000 pages, unless the images are mostly empty space. So most of that copy looks like whole files, not B's blocks (**inferred**). If it is whole files that pgdelta never sends (WAL segments), idea 1 avoids it; if it is relation data, idea 1 pays it too.

## Design

**Part A, local and counted, not timed.** EXP-03's failover scenarios, built by the existing harness with PostgreSQL 17.11, data checksums on, and S's summaries on from its creation (EXP-61's `--wal-summaries`):
- **S2:** B, the old primary, kept writing after the fork, then stopped cleanly.
- **S2b:** the same, but B flushed its pages and crashed (`-m immediate`).
- **S2c:** B is a standby that followed the old primary past the fork, then stopped cleanly.
- **S2d:** S2b, then B's WAL from the fork's segment on deleted (EXP-62).

Each is built twice:
- **kept:** with `wal_keep_size = 1GB` on B's cluster, as an operator who uses `pg_rewind` must keep B's WAL;
- **default:** S2 and S2c only, as EXP-61 built them.

A third case is made from kept S2: **a copy of B with its WAL cut** after the fork's segment.

**Idea 1, scored** by `experiments/tricky_copies/unionscan.py`:
- **C** is the last checkpoint record before S's history leaves B's timeline, found in B's own WAL with `pg_waldump`.
- **The send set** is the blocks B's WAL names from C's redo to its end, plus the blocks S's summaries list from C's redo to S's start, limit blocks included. Pages past B's length, whole free-space-map and init forks, and files B lacks are sent as the fast path sends them.
- **Every page of S is compared** with B's page at the same place, as EXP-61 compared them. Pages that differ and aren't sent are sorted by EXP-03's classifier, and also searched for hint bits B carries that S lacks.
- **The guard** passes only if all of these hold:
  - B and S match (cluster, version, checksums), with checksums or `wal_log_hints` on;
  - B's timeline is on S's history, and B's end is proven by how it was stopped (EXP-62's guard v2);
  - C is in B's WAL, and B's WAL reads on from C's redo to at least B's end;
  - S's summaries cover C's redo to S's start.

**The control** is `pg_rewind --dry-run --debug`, run from each kept source onto a copy of B. Its output lists every block it takes from B's WAL, every file it copies whole, and every chunk it fetches. Its blocks are compared with those `pg_waldump` names from C's record on.

Driver: `experiments/tricky_copies/union_check.py results/exp69`.

**Part B, on the VMs,** reruns EXP-67's two cells with diagnostics, at EXP-67's scale (600, about 18 GB). The lab script is `lab_exp67.sh` with `DIAG=1 ARMS=fast`: one repeat (seed 690), with a scale-20 smoke first (seed 691). After each lag, before its timed run, it records:
- **`pg_rewind --dry-run --debug`** from S onto a fresh copy of B0;
- **S's data directory:** its size per top-level entry, and `pg_wal`'s files;
- **B0's WAL** from C's redo (`pg_waldump`);
- **S's WAL summaries** from C's redo on.

The timed arm is pgdelta's hashing (its fast path refused), which gives the pages that differ.

**The analysis,** committed before the data: `analysis/exp69.py`, figure `exp69-union`, data `docs/experiments/data/exp69-scenarios.csv` and `exp69-rewind-copy.csv`. It splits `pg_rewind`'s fetched bytes into:
- relation blocks;
- free-space-map, visibility-map and init forks;
- WAL summaries;
- other files;
- WAL segments, by where they lie: since the fork, before it (an older timeline), or past S's current LSN (recycled or preallocated).

**Checked before registering:**
- **Unit tests** (`tests/test_tricky_unionscan.py`, 10) cover the parsers, the union, the guard's conditions and the hint check, on lines shaped like `pg_waldump`'s and `pg_rewind`'s own output.
- **The analysis was dry-run on synthetic results.** It caught two bugs before any data: a column collision, and a refusal phrase that didn't match the guard's actual text.
- **The lab script's defaults** still reproduce EXP-67's run plans.

**Disclosed: a format probe before registering, on one S2 build in the scratchpad.** It saw only `pg_rewind`'s side; idea 1's send set was never scored before this registration.
- **Built as EXP-61 built it:** B's WAL began after C (B's shutdown checkpoint had recycled the older segments), and `pg_rewind` failed for want of it.
- **With `wal_keep_size`:**
  - `pg_rewind`'s dry run took 1,681 blocks from B's WAL and needed 97 MB;
  - of that, 80 MiB was whole WAL segments (some past S's LSN), 13 MiB relation blocks, and 4 MiB free-space-map and visibility-map forks.

## Predictions

**Part A:**
1. **Deciding:** wherever idea 1's guard passes, it leaves no real wrong page, and it passes kept S2 and kept S2c. In those two, S's summaries alone left 1,276 and 1,444 (EXP-61).
2. **The guard refuses each of these, for its reason:**
   - kept S2b and S2d: B wasn't stopped cleanly;
   - default S2: no checkpoint before the fork in B's own WAL;
   - kept S2 with its WAL cut: B's WAL ends before B's end.

   Default S2c is reported, not predicted: whether a standby's WAL still reaches back to C depends on its last restartpoint.
3. **No unlogged hints:** wherever the guard passes, no page is left unsent on which B carries a hint bit S lacks.
4. **Control:** wherever the guard passes, `pg_rewind` exits 0 and takes exactly the main-fork blocks `pg_waldump` names from C's record on.
5. **Size:** wherever the guard passes, idea 1 sends at most 1.5 times the pages that differ.

**Part B,** EXP-67's cells at 18 GB:

6. **Control at scale:** in both lags, `pg_rewind`'s blocks equal `pg_waldump`'s.
7. **Idea 1's size:** in both lags, idea 1's blocks number between 0.9 and 1.5 times the pages pgdelta's hashing sends, and S's summaries cover C's redo to S's LSN.
8. **What `pg_rewind` copies beyond S's WAL since the fork:** in uniform, relation blocks are under a quarter of it. What the rest is, by kind, is reported.

**Rule 11** applies to ratios: a deciding ratio within 0.05 of its bar gets a second session. Predictions 1–4 and 6 are counts that must be exact.

## What follows

- **If 1–3 hold:** idea 1's send set is correct on these failovers. Its prototype in pgdelta is registered next and timed against `pg_rewind` on EXP-67's cells:
  - the receiver reads B's WAL from C;
  - the sender adds S's summaries;
  - the guard is as here.
- **If 1 or 3 fails:** the missed pages' source (B's WAL range, S's summaries, or hints in no WAL) is found before anything is built.
- **If 4 or 6 fails:** our reading of B's WAL differs from `pg_rewind`'s, and is reconciled before anything is built.
- **If 7 fails high:** idea 1 would send pages that don't differ. Its model is re-sized before the prototype's predictions are set.
- **8 and the composition:**
  - **If the rest is mostly WAL segments**, idea 1 avoids it (pgdelta sends no `pg_wal`), and `pg_rewind`'s time has a part that doesn't grow with either side's changes.
  - **If it is relation data,** idea 1 pays it too, and its model grows by it.
