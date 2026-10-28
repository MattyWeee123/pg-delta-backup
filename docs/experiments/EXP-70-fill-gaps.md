# EXP-70: Do gap-filled reads and writes (idea 2) make the fast path faster on scattered changes?

Status: **pre-registered October 28, 2026, before any run.** One VM pair (the `ha` set), about 1.5 hours with the data load, after EXP-69's part B. It follows EXP-68's result: re-applied on the real page set, EXP-66's rule says to prototype idea 2.

## Question

With 8 reader threads and the receiver's syncs on a worker (the fast path's defaults since EXP-68), the fast path refreshed EXP-64's scattered cell (18 GB, 28% of pages changed) in 76.5 s, against 90.9 s for a full copy. What binds now is the disks' operation rate:
- **the source:** 254,547 reads in 61.7 s of busy time, about the cap EXP-66 measured;
- **the destination:** 255,260 writes;
- **the plan behind them:** 249,404 runs, each read once and written once (1.018 writes per run).

**Idea 2** (research loop 1, section 4) fills each clean gap of a few pages between two chosen runs, so the two become one read and one write. EXP-66 found the disk counts a write of up to 32 KiB as one operation. EXP-68's model, run on the real page set, puts the write side's time at:
- 65.4 s without filling;
- 43.8 s filling gaps of 1 page;
- **38.6 s filling gaps of up to 2 pages** (71,647 writes; 238,764 extra pages, +39%);
- 41.3 s filling gaps of up to 4.

The reads should follow the same curve (**inferred**: EXP-66 measured reads' cost by size only at 8 KiB).

**Does filling make the refresh faster as modelled, and where does it stop paying?**

## Design

**The new option:** `--fill-gaps PAGES` (sender, with `--fast-path`). Each clean gap of at most PAGES pages between two chosen pages of a file is sent too: `fastpath.fill_gaps`, applied before the plan is read or written.
- **The filled pages are S's current copies:** read like the chosen ones, written and verified by the receiver.
- **Statistics:** `fast_path.fill_gaps` gives the pages filled, and `pages_chosen` counts them.
- **Tests:**
  - unit tests for `fill_gaps` and its validation;
  - a refresh against a real server with `--fill-gaps 2`. Its copy recovers to the source's state and passes `pg_amcheck`, it filled more than 0 pages, its plan lists the filled pages, and no two runs of a file are separated by a gap of 2 or fewer.

**The set-up** is EXP-68's, unchanged: the `ha` set in us-west1, two `n2d-highcpu-8` VMs with 150 GiB balanced persistent disks, `blake3` on both.
- **The source:** EXP-48's database (scale 600, about 18 GB), PostgreSQL 17.11 or later, `summarize_wal` on.
- **The cells:** hot, then uniform (1% of rows each); per cell, B0 is a base backup with its manifest, taken before the change.
- **Each run** starts from a fresh copy of B0, with both VMs' caches dropped. The pgdelta arms are live, in place, with `--jobs 4 --hash blake3 --fast-path summaries --read-ahead 8`, and the receiver's `--fsync worker`.

**The arms,** with every setting explicit in the lab script:

| Arm | Cells | `--fill-gaps` |
|---|---|---:|
| `fast8w` | uniform, hot | 0 (EXP-68's arm) |
| `fast8wf1` | uniform | 1 |
| `fast8wf2` | uniform, hot | 2 |
| `fast8wf4` | uniform | 4 |
| `full` | uniform | one `pg_basebackup -X stream -c fast` into a new directory |

**The schedule:** 2 runs per arm, in a shuffled order (seed 700).
- **The command:** `PATTERNS="hot uniform" ARMS_hot="fast8w fast8wf2" ARMS_uniform="fast8w fast8wf1 fast8wf2 fast8wf4 full" PLAN=1 lab_exp64.sh SOURCE 2 700`, with results in `exp70`.
- **Per run:** `/proc/diskstats` on both VMs. Every fast run writes its plan, and the first plan per cell and arm is kept (new in the lab script).
- **Correctness:** each fast copy is started, and its `pgbench_accounts` row count and sum of `abalance` must equal the source's.
- **A smoke first,** at scale 20 (seed 701, one run per arm), deciding nothing. It must have kept every arm's plan and recorded four counter lines per run.

**The analysis,** committed before the data: `analysis/exp70.py` (it reuses EXP-68's parsing), figure `exp70-fill`, data `docs/experiments/data/exp70-cells.csv` and `exp70-model.csv`. Medians of 2. It was dry-run on synthetic results shaped like the harness's output.

**The lab script's arms were made explicit before this registration.** EXP-68 changed the fast path's defaults, so the arms now pass every setting rather than relying on them:
- `fast` passes `--read-ahead 0`;
- every arm without a `w` passes `--fsync main`.

EXP-64's, EXP-64b's and EXP-68's arms mean what they did when registered. EXP-67's lab script was pinned the same way.

## Predictions

On the uniform cell unless named.

1. **Deciding: `fast8wf2` takes at most 0.85 of `fast8w`'s time.**
   - **The model gives about 0.69.** Each side costs about 38 s at 71,647 operations of 96 KiB on average, and both sides overlap, as EXP-68 showed. That leaves the fixed costs EXP-68 measured (about 15 s, the guard's 4.7 s among them).
   - **At the bar (65 s),** filling still recovers 11 s of the source's 62 s of reads.
2. **The operations fall as modelled:** `fast8wf2`'s source read requests and destination write requests are each at most 0.40 of `fast8w`'s. The model gives 0.29.
3. **No material gain past 2 pages:** `fast8wf4` takes at least 0.95 of `fast8wf2`'s time. The model gives 1.07: filling 4 pages writes 20% more pages for fewer operations, and bandwidth starts to charge for them.
4. **No loss in the hot cell:** `fast8wf2` takes at most max(`fast8w` + 0.3 s, 1.15 × `fast8w`).
5. **Every run is correct and as configured:**
   - every run and every pgdelta receiver exited 0;
   - every fast run used the fast path on B0's `backup_manifest`, synced on the worker, and its copy matched the source;
   - each cell and arm's plans were identical, and every fast run read as many runs as its plan lists;
   - **each filled plan is exactly `fill_gaps` of the unfilled one,** file by file, runs included.
6. **Controls:** `fast8w` within 10% of EXP-68's 76.54 s, and the full copy within 5% of its 90.90 s.

**Reported, not predicted:**
- `fast8wf1`;
- the pages and bytes sent;
- both disks' busy time and the sender's waits;
- EXP-66's write model for this session's own unfilled plan, beside the measured times.

**Rule 11:** a deciding ratio within 0.05 of its bar gets a second session.

## What follows

- **If 1, 3 and 4 hold:** `--fill-gaps 2` becomes the fast path's default. Research loop 1's crossover against a full copy moves again, and the selector (idea 4) is re-sized.
- **If 1 fails and 2 holds:** the operations fell, but the time didn't. The counters and timers say what binds instead: bandwidth on either disk, the network, or the sender's hashing of the extra pages.
- **If 2 fails:** the filled runs didn't reach the disks as single requests. The block layer's request size limit or the sender's 1 MiB run cap would be checked first.
- **If 3 fails (`fast8wf4` faster):** the best fill lies beyond 2. The model's bandwidth term overstates the cost of larger I/O here, and the default is set from the measured curve instead.
