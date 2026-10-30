# EXP-85: Single-read page-lsn: does sending each chosen page from the scan's own read restore the tie with a re-clone at 4.5x memory?

Status: **pre-registered October 30, 2026, before any scored run.** The local stage runs first; the VM stage runs only if L1 and L2 hold.

## Question

**EXP-84's one page-lsn miss:** in H2's uniform cell (scale 1,200, about 36 GB on 8 GB VMs, 28% of the pages changed), `--fast-path page-lsn` took **1.123x `pg_basebackup`'s time** (199.3 s against 177.6 s). In its other nine held-out cells it took 0.86–0.98x.

**The mechanism found after the scoring** (post-hoc, `analysis/exp84_posthoc.py`):
- In that cell it read **1.108x S** from the source's data disk: 3.9 GB more than the 35.7 GB of S. In every other cell it read S once (0.996–1.000x).
- That disk ran at its cap, about 205 MB/s, so the extra reads cost about 19 s of the 21.8 s gap.
- page-lsn reads each main-fork page twice. Its scanner reads every page to choose it by its LSN. The sending thread then reads each chosen page again, up to two files later, expecting it still in the page cache. At 4.5x memory, some weren't.

**The change, structural rather than tuned** (the idea archive's "single-read page-lsn"): send each chosen page from the bytes the scan read, so each page of S is read exactly once. A page-lsn refresh then never reads more than `pg_basebackup` does.

**Rule 15:** H2's cells were spent by EXP-84, so they may be used to diagnose and tune. Single-read page-lsn can become page-lsn's default only after a held-out session on H5–H8.

## What was built (before this registration)

- **`fastpath.lsn_held_runs`:** one read of a file, in 8 MiB chunks, yielding the runs to send with their bytes:
  - the pages page-lsn chooses;
  - every page past B's length;
  - each clean gap of up to `--fill-gaps` pages between two chosen pages, taken from the previous chunk when a gap crosses a chunk boundary;
  - the runs cut at 1 MiB, as `matching.runs` cuts them.

  A randomized test (400 files, every chunk size, gap and run length) checks that its runs and bytes equal what page-lsn sends after `lsn_pages`, `fill_gaps` and `matching.runs`, and that each page is read once. Two deliberate mutations of its chunk-boundary logic each failed that test.
- **`--lsn-single-read`** (opt-in, with `--fast-path page-lsn` only):
  - the scanner thread hands each chunk's runs to the sending thread through a queue of at most `LSN_HELD_BYTES // LSN_SCAN_BYTES` items (about 256 MiB);
  - the sending thread hashes and sends them, then ends each file with `FILE_DONE_WRITTEN`, as before.
- **New statistics:**
  - `fast_path.reread_bytes`: what the two-read path reads again; 0 with single read;
  - `fast_path.held_bytes_peak`: the most the scanner held ahead of sending;
  - `fast_path.lsn_single_read`.
- **Tests:**
  - the property test, and a test that a run is held only while a later page can join it;
  - the mode check;
  - real-server refreshes with single read of a cleanly stopped copy (against two reads), a base backup and a killed standby: each copy correct, nothing read again.
- **Harnesses:**
  - `trials.py --single-read`;
  - `timing.py --arms ... page-lsn-1`;
  - the lab's arm `lsn1` (`lsn` with `--lsn-single-read`).

## Design

### Stage 1: local (rule 13), PostgreSQL 17.11 on the Mac

- **Correctness:** EXP-82's campaign, with every page-lsn refresh single read.
  - 40 trials each of a stopped standby, a base backup taken under load, and a killed standby, plus 10 negative controls (a base backup whose start was moved to its end): 130 trials, seed 850.
  - `experiments/summaries_off/trials.py --per-arm 40 --on-demand 0 --controls 10 --seed 850 --single-read --out docs/experiments/data/exp85-trials.csv`.
- **Timing scale model:** `experiments/summaries_off/timing.py --scale 100 --repeats 3 --seed 851 --arms page-lsn page-lsn-1 --out docs/experiments/data/exp85-timing.csv`, in the hot and scatter cells.
  - The Mac holds the 1.6 GB database in memory, so the two arms should take about the same time.
  - The ratio is logged as rule 13's proxy pair, to show what the local stage can't see.
- **The smoke** (seed 859; decides nothing) passed: 3 of 3 single-read trials correct, the control wrong, and nothing read again.

### Stage 2: the VMs, H2's spent cells

- **The `ha` set:** n2d-highcpu-8, pd-balanced 150 GB data disks, us-west1 first, as in EXP-84.
- **One lab run:** `experiments/network/lab_exp64.sh` with `SCALE=1200 STANDBY_TOO=1 FULL_CHECK=1 SERIES=1 PATTERNS="hot uniform" ARMS="sb_lsn sb_lsn1 full"`, 2 runs per arm (12 runs), shuffled, seed 850.
  - **B0s** is a standby stopped cleanly, as in EXP-84's H2 cells.
  - **`sb_lsn`:** page-lsn with two reads.
  - **`sb_lsn1`:** page-lsn, single read.
  - **`full`:** `pg_basebackup`.
- **Smoke first:** scale 20, seed 859; every run exits 0 and every copy matches.
- **The analysis,** committed before the data: `analysis/exp85.py`, dry-run on the smoke's local files and on EXP-84's H2 runs. Data `docs/experiments/data/exp85-trials.csv`, `exp85-timing.csv` and `exp85-runs.csv`; figure `exp85-single-read`.

## Predictions

Times are each arm's mean over its runs.

**L1. Local, deciding for stage 2:** all 120 single-read page-lsn trials come out correct:
- exit 0, the fast path used, with its expected proof;
- rows equal to S's and `pg_amcheck` clean;
- no byte read again.

**L2. The negative control** is wrong in all 10 trials, so the check can see a wrong copy.

**1. Deciding:** in uniform, single read takes **at most 1.05x `pg_basebackup`'s time**, EXP-84's bar, which two reads missed there at 1.123x.

**2.** In uniform, if two reads miss again (at least 1.05x `pg_basebackup`), single read takes at most 0.95x of their time. If two reads don't miss again, 2 is not testable, and that is reported.

**3. The mechanism:** in each cell, single read's source-disk reads are at most 1.01x `pg_basebackup`'s.

**4. No cost where nothing changed much:** in hot, single read takes at most 1.024x two reads' time (rule 17's spread), and at most 1.05x `pg_basebackup`'s.

**5.** Every copy matches the source. Single read holds at most `LSN_HELD_BYTES` plus two 8 MiB chunks in every run.

**Reported, not predicted:**
- the scan's seconds;
- the time after the scan;
- `reread_bytes`;
- `held_bytes_peak`;
- a timeline of the source disk's reads per arm;
- the local time ratio beside the VM ratio, for rule 13's proxy log.

## What follows

- **If L1 or L2 fails,** stage 2 doesn't run, and the failing trials show why.
- **If 1 and 3 hold,** single read restores page-lsn's tie with a re-clone at 4.5x memory. It becomes the candidate for page-lsn's default, which needs a held-out session on H5–H8 (rule 15).
- **If 3 holds and 1 fails,** the extra reads were not the whole loss. The disk timelines locate the rest.
- **If 3 fails,** single read does not read each page once as built, and it goes no further.

## The rules this session runs under

- **Rule 13:** local correctness gates the VMs. The local time ratio is logged against the VM ratio.
- **Rule 15:** H2's cells are spent, so this is a tuning cell, not a held-out one.
- **Rule 16:** `analysis/integrity.py register EXP-85` records the analysis and its helpers, the harnesses, the lab and the disk sampler, and pgdelta's modules.
- **Rule 17:** every bar is far outside the 2.40% spread for runs of 10 s or more, except 4, which is the spread itself. EXP-84's gap was 12.3%.

## Workflow check

| | Check | For this experiment |
|---|---|---|
| 1 | Situation, standard, competitor | Summaries off at 4.5x memory: page-lsn lost to a re-clone (`pg_basebackup`), 1.123x (EXP-84) |
| 2 | The bottleneck | 11% extra source-disk reads at the disk's cap, measured from EXP-84's disk counters (post-hoc) |
| 3 | Systems or algorithm | Systems: the algorithm needs one read of each page, and the build read the chosen ones twice |
| 4 | Papers and prior art | WAL-G's catch-up and `pg_probackup`'s DELTA mode decide and send from one read of each page |
| 5 | Committee-grade | Pre-registered; analysis committed before the data; the negative control kept; a randomized property test against the two-read path |
| 13 | The cascade | Tests, then the local campaign and scale model, then the VMs; the local/VM ratio pair logged |
| 14 | The idea archive | Single-read page-lsn, added after EXP-84 |
| 15 | Held-out workloads | H2 is spent; a default needs H5–H8 |
| 16 | Scorer integrity | Recorded at registration; correctness gate on every copy; the control kept |
| 17 | The noise bar | 2.40% spread; deciding bar 1.05x against a 12.3% effect |
