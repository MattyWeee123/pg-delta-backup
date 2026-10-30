# Project log

A dated record of what was done, why, and what came out of it. Newest entries first.
Experiments have their own write-ups in [docs/experiments/](experiments/).
Dates are UTC, as the VMs log them. The six entries after EXP-70's pre-registration were first dated October 29 by mistake; they carry their commits' UTC times instead.

## 2026-10-30 (13:30 UTC): EXP-86 done: 320 of 320 randomized refreshes correct on Linux, ext4 and XFS

- **1 held:** 160 of 160 deciding trials correct on each filesystem:
  - crashed standbys 60;
  - page-lsn's stopped standbys, base backups under load and killed standbys, 30 each, half single read;
  - on demand 10.

  Wrong copies below 0.93% per refresh at 95% over both.
- **2 to 4 held:** every crashed primary refused and hashed correctly, every negative control wrong, and every trial run as asked. Linux's `worker-writeback` (`sync_file_range`) was in effect where asked.
- **A text correction:** the registration said 130 deciding trials per filesystem; its design gives 160, and 160 ran.
- **Next:** the H5–H8 held-out session (EXP-87).

## 2026-10-30 (12:39 UTC): EXP-86 pre-registered: randomized correctness on Linux VMs, ext4 and XFS

- **Why:** the randomized correctness campaigns ran on the Mac. On the VMs, only the timed copies were checked.
- **Design:** the ha set. The source VM's `/data` stays ext4, and the destination's is reformatted XFS. Each runs, on its own disk:
  - EXP-80's campaign: 60 crashed standbys and 10 crashed primaries;
  - EXP-82's campaign: 90 page-lsn trials (half with single read), 10 on demand and 10 negative controls.
- **Each receiver** syncs by `auto` or `worker-writeback`, Linux's `sync_file_range` path, half each.
- **Deciding:** every deciding trial correct by the fast path on each filesystem: 130 per filesystem, a bound below 2.3% each and 1.2% over both.

## 2026-10-30 (07:28 UTC): EXP-85 done: single-read page-lsn ties a re-clone at 4.5x memory

- **Every VM prediction held** (the ha set, scale 1,200, two runs per arm):
  - uniform: single read **0.984x** `pg_basebackup` (174.7 s against 177.6 s); two reads 1.119x (198.8 s, EXP-84's miss reproduced); single read over two reads 0.879x;
  - the source disk: single read read S exactly once (1.000x `pg_basebackup`'s bytes, against 1.102x);
  - hot: no cost (1.000x two reads, 0.983x `pg_basebackup`);
  - 8 of 8 copies correct; at most 16 MiB held.
- **Rule 13:** the local scale model called single read 1.116x slower in scatter, and the VMs found it 0.879x. All six summaries-off pairs lie outside the band, so for mechanisms beyond memory the local stage gates correctness only.
- **Rule 17:** the noise floor's 95th percentile for runs of 10 s or more is now 2.23%. `pg_basebackup` moved 0.03% from EXP-84.
- **Rule 16:** the analysis's figure legend was moved after the scoring (layout only; the scores are unchanged).
- **Next:** `--lsn-single-read` stays opt-in until a held-out session on H5–H8.

## 2026-10-30 (04:33 UTC): EXP-85b held: single read doesn't change the negative control's rate

- **Result:**
  - single read: 28 of 30 controls wrong (93%);
  - two reads: 25 of 30 (83%);
  - Fisher's exact test, p = 0.42;
  - every trial ran as its mode says.
- **The control's measured rate is about 88% wrong,** so EXP-85's one correct control was the control's own randomness. "All 10 wrong" asked more than the control gives; future registrations use EXP-82's bar or this rate.
- **EXP-85's VM stage now runs as registered,** on the owner's decision; its L2 stays recorded as failed.

## 2026-10-30 (04:20 UTC): EXP-85b pre-registered: the negative control, with one read and with two

- **The owner's decision on EXP-85's L2 miss:** measure whether single read changes the control's rate, then run the VM stage if it doesn't.
- **Design:** 30 controls with single read and 30 with two reads, shuffled in one local session (seed 852).
- **Deciding:** at least 80% wrong in each mode (EXP-82's bar). Reported: Fisher's exact test of the two shares.

## 2026-10-30 (03:42 UTC): EXP-85 stage 1: single read correct in 120 of 120 trials; one control came out correct

- **L1 held:** all 120 single-read page-lsn trials were correct (40 stopped standbys, 40 base backups under load, 40 killed standbys), and nothing was read again.
- **L2 failed as registered:** 9 of 10 negative controls were wrong, against the "all 10" registered.
  - EXP-82 had registered "at least 8 of 10" for the same random control. This registration was stricter.
  - Single read can't change a control's outcome on an idle source (same pages, same bytes), though that is argued from the code, not measured.
- **Scale model, in memory:** single read took 0.863x two reads' time in hot and 1.116x in scatter. The scanner's per-page copying in Python is the likely cost (post-hoc).
- **By the registered rule, the VM stage doesn't run.** Running it anyway is the owner's decision.

## 2026-10-30 (03:10 UTC): EXP-85 pre-registered: single-read page-lsn

- **Why:** EXP-84's one page-lsn miss (1.123x `pg_basebackup` at scale 1,200, uniform) came with 11% extra source-disk reads. Post-hoc, these were the second reads of chosen pages that had left the page cache.
- **Built:**
  - `--lsn-single-read` sends each chosen page from the scan's own read;
  - `fastpath.lsn_held_runs` yields exactly the runs and bytes the two-read path sends, as a randomized property test checks;
  - the scanner holds at most about 256 MiB ahead of sending.
- **Local stage:** 120 single-read trials and 10 negative controls (EXP-82's campaign), and a scale-model timing.
- **VM stage, only if the local stage holds:** H2's spent cells on the ha set, with two runs each of two reads, single read and `pg_basebackup`.
- **Deciding:** single read at most 1.05x `pg_basebackup` in uniform. Also predicted: its source-disk reads at most 1.01x `pg_basebackup`'s, and no cost in hot.

## 2026-10-30 (02:35 UTC): K15: `--crashed-standby` on by default with the fast path

- **The owner's decision**, after EXP-84: `--crashed-standby` is on by default, and `--no-crashed-standby` turns it off. It changes only the fast path's decision. A crashed primary stays refused, and the fast path itself stays opt-in.
- **The evidence:**
  - EXP-80: 100 of 100 randomized crashed standbys correct;
  - EXP-80b: 0.023–0.233 of hashing's time beyond memory;
  - EXP-84: 10 of 10 held-out cells.
- **Code:** `fastpath.DEFAULT_CRASHED_STANDBY`; the receiver option is a `BooleanOptionalAction`; a local session's child receiver is passed the setting either way.
- **Tests:**
  - the real-server test now refreshes a killed standby by default, with the option, and with `--no-crashed-standby` (refused, hashed);
  - a unit test covers the CLI and the child receiver's flags.
- **Harnesses** (EXP-63's check, EXP-64's lab, EXP-80's and EXP-82's trials): every arm that ran without the option now passes `--no-crashed-standby`, so it keeps its registered meaning.

## 2026-10-30 (02:20 UTC): EXP-84 done: the first held-out session narrows two claims

- **`--crashed-standby` held in 10 of 10 cells** on H1–H4 (new workloads, scale 1,200, pd-ssd, PostgreSQL 18.6): proven, correct, within +0.36 s or 0.9% of a clean standby. It is eligible to become a default; that is the owner's decision.
- **`--selector` failed in 6 of 10:**
  - every refresh of 1.5–2.5 s was under-predicted by 18–33% (0.26–0.69 s): its fixed cost was fit only on long refreshes;
  - the long cells held, and pd-ssd was over-predicted 1.52x, as predicted;
  - its call on `pg_basebackup` was right in 10 of 10, and its predictions of `pg_basebackup` were within 1% on pd-balanced.
- **page-lsn failed in 1 of 10:** 1.123x `pg_basebackup` at scale 1,200, uniform; 0.86–0.98x elsewhere. Post-hoc: it read 1.108x S there (3.9 GB more, about 19 s at the disk's cap), re-reading chosen pages that had left the cache. Single-read page-lsn is added to the archive.
- **The fast path's defaults held** (0.015–0.46 of `pg_basebackup`), and 40 of 40 copies matched.
- **The session:** three VM sets, 21:44 to 02:10 UTC. The gcloud sign-in expired while `hc` was being prepared, before its smoke; the owner signed in and it resumed. Every set is torn down.
- **Rule 15:** H1–H4 are spent and may now be used to tune. H5–H8 are sealed: skewed non-HOT updates, 32 GB of memory, local NVMe, and checksums off with `wal_log_hints`.
- **Post-hoc figure:** `exp84-misses` (`analysis/exp84_posthoc.py`).

## 2026-10-29 (21:33 UTC): EXP-84 pre-registered: the held-out session for --crashed-standby, --selector and page-lsn

- **The settings:** sealed since October 28: H1 (clustered, tpcb, append, retention), H2 (scale 1,200), H3 (pd-ssd), H4 (PostgreSQL 18.6). H3 and H4 were built at first use: `create.sh` takes pd-ssd, and the lab takes `PG_MAJOR`.
- **The arms, one run each per cell:** a crashed standby with `--crashed-standby`; a clean standby by the fast path's defaults, by `--selector` and by page-lsn; and `pg_basebackup`.
- **Two VM sets at once** (32 vCPUs): `ha` runs H1 then H2; `hs` runs H3; then `hc` runs H4.
- **Predictions, per cell, from the tuned results:**
  - crashed at most 1.10x the clean standby's time, and correct;
  - the selector within 15% (an expected overestimate on pd-ssd), with its full-copy call right;
  - page-lsn at most 1.05x `pg_basebackup`;
  - the fast path faster than `pg_basebackup` exactly below 91% of the pages;
  - every copy correct.
- **A miss narrows the claim** (rule 15).

## 2026-10-29 (20:30 UTC): EXP-83d done: the summarizer reads no WAL from storage, yet a disk-bound server's p99 rises 22%

- **1 (deciding) held:** beyond memory the summarizer read 0.0% of its WAL from storage (100.1% of it, all from the page cache).
- **2 failed:** with 12 long blocks and no switch in the data, p99 was 1.22x [1.10, 1.35] and throughput 0.971 [0.925, 1.018]. The registered decision is unresolved: H1 (the switches) and H2 (its storage reads) are both refuted.
- **Post-hoc:** with summaries on, the data disk wrote 24% more besides WAL (33.9 against 27.4 MB/s; write rate 1.109 [1.036, 1.183] by block pair) and read about 3.5% more. That points to cached data pages displaced by the summarizer's second read of each WAL page, or by its memory.
- **3 failed on its condition:** `-N` with 90 clients reached only 35% CPU. The in-memory cells are bound by commit fsyncs on the persistent disk.
- **4 held:** 0.78–0.96 ms per MB of WAL.
- **The amendment:** the first launch stopped on the probe printing a counter in scientific notation, before any scored data; fixed and re-recorded.
- **Teardown:** the pipeline's teardown failed on a gcloud error after the fetch; the dest VM was deleted by hand at once.
- **The advice in RESULTS:** on, where the working set fits in memory; beyond it, a tail-latency price of about 20%.

## 2026-10-29 (17:18 UTC): EXP-83d pre-registered: the mechanism of summarize_wal's cost beyond memory

- **Two hypotheses:**
  - **H1:** the earlier cost was the switches' (the catch-up's reads evicting cache, and a cache still warming);
  - **H2:** a steady-state cost from the summarizer reading its WAL from storage.
- **Measured directly:** the summarizer's reads from storage (`/proc/<pid>/io`) and the data disk's busy fraction.
- **Design:** 12 long blocks (ABBA BAAB ABBA) with no switch in the data, after 15 minutes of warm-up; and a CPU-bound cell (`-N`, 90 clients).
- **Predictions:**
  1. Deciding: the summarizer reads at most 10% of its WAL from storage;
  2. no cost beyond memory in long blocks;
  3. no cost on a CPU-bound source (busy at least 80%);
  4. at most 1.0 ms of CPU per MB of WAL.
- **The decision rule** (H1, H2 or unresolved) is in the registration.

## 2026-10-29 (17:02 UTC): EXP-83c done: summarize_wal costs nothing measurable in memory; beyond memory a p99 cost again

- **In memory, every prediction held:**
  - 32 clients: throughput 1.029, lower bound 0.9999;
  - 90 clients: 1.010, lower bound 0.9991;
  - p99 0.999 and 0.964.
  - With EXP-83b, two sessions agree.
- **The summarizer:** 0.84–0.88 ms of CPU per MB of WAL in every cell.
- **The 90-client cell** reached only 44% CPU: TPC-B's 100 branch rows serialize the clients at scale 100. Claims are scoped to that.
- **Beyond memory:**
  - throughput 0.948 [0.887, 1.010] and p99 1.205 [1.030, 1.379] failed again;
  - with settle runs the effect no longer follows a switch (post-hoc), so it may be the summarizer's steady-state WAL reads competing for a saturated disk;
  - the cache was also still warming.
- **The advice in RESULTS:** turn it on where the working set fits in memory; weigh it where it doesn't. EXP-83d (the summarizer's disk reads, long blocks, a CPU-bound cell) would settle the rest.

## 2026-10-29 (15:06 UTC): EXP-83c pre-registered: summarize_wal's cost, settled, with a saturated cell

- **What changes from EXP-83b:**
  - a settle run in the same mode after every switch;
  - a five-minute warm-up beyond memory;
  - a 90-client cell on the in-memory source to load its CPU.
- **Predictions, per cell:**
  1. Deciding: throughput cost at most 3% at 95%;
  2. p99 at most 1.10x;
  3. the summarizer at most 1.0 ms of CPU per MB of WAL.

## 2026-10-29 (15:04 UTC): EXP-83b done: no measurable cost in memory; a confounded p99 rise beyond it

- **In memory (scale 100):**
  - throughput 0.996 [0.964, 1.029] with summaries on; the bar of 0.97 missed by 0.006, a rule 11 case;
  - p99 0.999.
- **Beyond memory (scale 600):**
  - throughput 0.951 [0.871, 1.030] and p99 1.41x, both registered as failed;
  - post-hoc, the p99 rise sits in "on" runs that followed "off" runs (1.20–1.80, n=6, against 1.04 and 1.16, n=2): the summarizer first read the previous run's WAL from disk, likely evicting cached pages;
  - throughput also drifted up through the session as the cache warmed.
- **The summarizer:** 0.78–0.85 ms of CPU per MB of WAL, 2–3% of one core.
- **The source was only 19–34% busy** (32 clients across zones are round-trip bound).
- **Next:** EXP-83c, with a settle run after every switch, a five-minute warm-up beyond memory, and a 90-client cell to load the CPU.

## 2026-10-29 (14:08 UTC): EXP-83 done (the summarizer about 0.6 ms of CPU per MB of WAL; throughput noise-bound on the Mac); EXP-83b pre-registered

- **EXP-83 (local):**
  - the summarizer took 2.1% of one core at 33.5 MB/s of WAL (615 µs per MB);
  - p99 latency was 0.95x.
  - The deciding throughput bound failed on noise: the Mac's throughput swung 2.9x from run to run whatever the setting, and the interval was [0.85, 1.48]. The VMs decide (rule 2).
- **EXP-83b:** the client on dest, 32 clients, scale 100 and 600, 8 pairs of 60 s each. Deciding: throughput cost at most 3% at 95% in both cells.

## 2026-10-29 (13:56 UTC): EXP-83 pre-registered: what summarize_wal costs a busy server (local)

- **Why:** pgdelta's fastest routes need WAL summaries, and PostgreSQL ships them off. If they cost little, the advice is to leave them on.
- **Prior art:** the documentation says nothing on cost, and no published benchmark was found.
- **Design:** pgbench TPC-B-like, 16 clients, scale 50, 10 pairs of 30 s runs, on and off in random order, each after a checkpoint, with the summarizer caught up before every "on" run.
- **Predictions:** throughput cost at most 3% at 95% (deciding); p99 at most 1.10x; the summarizer at most 5% of a core.
- **New harness:** `experiments/summarize_cost/`, with `analysis/exp83.py`.

## 2026-10-29 (09:46 UTC): EXP-82c done: summaries on demand 0.045 of hashing's time for local changes

- **Every prediction held,** on a source that never summarized (checked at set-up: "summarizer at 0/0"), with a slot keeping the WAL since the old copy:

  | Cell | Summaries on demand | page-lsn | Hashing | On demand / hashing |
  |---|---|---|---|---|
  | hot | 3.95 s (2.3 s summarizing 0.3–0.5 GB, 1.7 s refreshing) | 87.7 s | 87.7 s | **0.045** |
  | uniform | 100.6 s (58.9 s summarizing 11.9 GB at 203 MB/s, 41.7 s refreshing) | 88.5 s | 185.1 s | 0.544 |

  - page-lsn repeated EXP-82b (1.00, 0.478).
  - 12 of 12 copies were correct.
  - The scorer was unchanged but for EXP-82c's own figure function (disclosed).
- **The routes:**
  - the faster one reads fewer bytes: the WAL since B plus the fast path's pages, against all of S;
  - on demand wins for local changes (0.045 of page-lsn), page-lsn where the WAL nears the database (1.14).
- **Research loop 3:** the summaries-off row has a route with no loss: on demand where the WAL is kept, page-lsn where it is gone.
- **Next:**
  - a selector between them;
  - the cost of `summarize_wal` on a busy server;
  - the held-out session before any default changes.
- **Proxy fidelity:** on demand's local ratios (0.82, 1.55) against the VMs' (0.045, 0.54) are logged; the in-memory model misses disk-bound effects.

## 2026-10-29 (08:25 UTC): EXP-82c pre-registered: summaries on demand on the VMs, the source as one that never summarized

- **What runs:** the on-demand arm again, with page-lsn and hashing (a second session for page-lsn). The lab's set-up now turns summaries off, restarts the source, clears its summaries and checks the summarizer never ran.
- **Predictions:**
  1. Deciding: on demand at most 0.20 of hashing's time in hot (expected 0.03–0.09);
  2. at most 0.90 in uniform (expected 0.48–0.85);
  3. page-lsn again at most 0.70 (uniform) and 1.05 (hot);
  4. the condition checked and every copy correct.

## 2026-10-29 (08:24 UTC): EXP-82b done: page-lsn beats hashing and WAL-G for scattered changes; the on-demand runs void

- **page-lsn held 1–3** (18 GB beyond memory):
  - 0.470 of hashing's time in uniform (88.6 s against 188.6 s) and 0.999 in hot;
  - 0.81 of WAL-G's catch-up in uniform (109.3 s) and 0.998 in hot;
  - 0.97–0.98 of `pg_basebackup` (both read all of S: the algorithm's limit).
  - Dest's disk was busy 1.2 s and 34 s, against hashing's 87 s and 160 s: B never read.
  - Every copy was correct, and the scorer unchanged.
- **4–5 failed, from a harness defect found after the run:**
  - `run_study.py`'s base configuration sets `summarize_wal = on` for every VM source since EXP-64, so `SUMMARIES=0` didn't turn it off, and the source summarized throughout.
  - The first on-demand run per cell was the ordinary summaries' fast path.
  - The cleanup's RESET fell back to on, and deleting the summaries made the second run refuse and hash.
  - page-lsn, hashing and WAL-G read no summaries, so they stand.
  - Local probes confirm on demand works on a source that never summarized.
- **The lab is fixed:** off by `ALTER SYSTEM`, a restart, the summaries cleared, a check that the summarizer never ran, and SET off, not RESET.
- **EXP-82c** re-runs on demand.
- **The scorecard:** summaries off, WAL gone, is now a win against WAL-G (scattered) and parity (local), and a tie against a re-clone. A crashed primary stays a loss.
- **Proxy fidelity:** page-lsn's local ratios (0.49 hot, 0.96 scatter) against the VMs' (1.00, 0.47) are logged. An in-memory model can't show a disk-bound effect.

## 2026-10-29 (05:55 UTC): EXP-82b pre-registered: summaries off on the VMs

- **What runs:** a source that never summarizes, with a replication slot keeping the WAL since each old copy, and an 18 GB standby stopped before the change. It is refreshed by hashing (today), page-lsn, summaries on demand (timed from turning the setting on), WAL-G's catch-up and `pg_basebackup`, in hot and uniform.
- **Predictions, from research loop 3's bound:**
  1. Deciding: page-lsn at most 0.70 of hashing's time in uniform (expected 0.49–0.56);
  2. at most 1.05 in hot;
  3. parity with WAL-G (at most 1.05 hot, 0.95 uniform);
  4. on demand at most 0.20 of hashing's time in hot;
  5. every copy correct.
- **The lab's new modes:** `SUMMARIES=0`, `SLOT=1`, and the arms `hash` (now checked), `lsn` and `od`.

## 2026-10-29 (05:53 UTC): EXP-82 done: both summaries-off candidates correct; neither faster than hashing locally for scattered changes

- **Correctness held:**
  - 120 of 120 page-lsn copies (stopped standbys, base backups under load, killed standbys; failure rate below 2.47% at 95%);
  - 20 of 20 copies with summaries turned on at refresh time;
  - 10 of 10 negative controls wrong (the threshold at B's end), so the redo point is required and the check sees a wrong copy.
  - The scorer was unchanged (11 of 11 files).
- **Rule 13's local promotion failed for both.** In memory, page-lsn took 0.49 of hashing's time (hot) and 0.96 (scatter), and on demand 0.82 and 1.55. On the Mac hashing costs CPU, not the disk reads of B that the VMs' bound turns on. As registered, both go to the VMs on that bound (EXP-82b), and the local/VM pairs will be logged.
- **EXP-82b's design:** a replication slot reserved just before each old copy is taken keeps exactly the WAL since its redo point. With `wal_keep_size` instead, the summarizer would read every older segment too (it starts at the oldest on disk). Archived: B's own WAL could cover the gap when a slot keeps less.

## 2026-10-29 (05:22 UTC): EXP-82 pre-registered: summaries off, the local stage

- **What runs:**
  - **The correctness campaign:** on a server that never summarized WAL, 120 randomized page-lsn refreshes (40 stopped standbys, 40 base backups taken under load, 40 killed standbys) and 20 refreshes with summaries turned on at refresh time.
  - **The load:** pgbench with DDL, and databases created with FILE_COPY or WAL_LOG.
  - **A negative control** (10): base backups with the threshold moved to their end.
  - **Local timings** at scale 100 against hashing, for rule 13's promotion.
- **Predictions:**
  1. Deciding: every page-lsn copy correct;
  2. every on-demand copy correct, by the summaries' fast path;
  3. at least 8 of 10 control copies wrong;
  4. each candidate at most 0.80 of hashing's local time in both cells.
- **The smoke** (7 trials) was correct, with both control copies wrong.

## 2026-10-29 (05:11 UTC): research loop 3: copies without WAL summaries; two candidates built and tested

- **The pass:** [research loop 3](research/2026-10-29-research-loop-3.md), with its [prior-art report](research/2026-10-29-summaries-off-prior-art.md) (WAL-G, `pg_probackup`, PTRACK, pg_rewind and PostgreSQL's incremental backup from source; ARIES's pageLSN; Percona, Oracle, SQL Server and VMware).
- **The bound:**
  - Without a record of what changed, every page of S must be read once (about 86 s on the ha set).
  - Hashing also reads B, which in uniform queues the writes on dest's disk (180 s against WAL-G's 109 s).
  - Reading only S gives parity with WAL-G, not a large win. Reading the WAL instead of S, where it is kept, costs in proportion to the WAL.
- **Candidate 1, `--fast-path page-lsn`:**
  - pages at or past B's **redo point** (`pg_probackup`'s threshold; WAL-G uses the checkpoint record);
  - the visibility and free-space maps whole;
  - data checksums or `wal_log_hints` required (pg_rewind's condition; without them a page marked all-visible keeps its LSN);
  - guard v2 for lineage;
  - a scanner thread ahead of the sender.
- **Candidate 2, summaries on demand:** turning `summarize_wal` on makes PostgreSQL 17 summarize from the oldest WAL still on disk (`walsummarizer.c`), so where S kept the WAL since B's redo point the existing fast path runs.
- **Tests:**
  - real-server refreshes of a clean copy, a base backup and a killed standby by page-lsn;
  - on a server that never summarized, the summaries refused, page-lsn run, then summaries turned on covering B;
  - page-lsn's pages a subset of the summaries' in the same state.
  - 1,658+ tests pass.
- **Next:** EXP-82, the local scale model (correctness trials, a negative control, timings against hashing), then EXP-82b on the VMs against hashing and WAL-G.

## 2026-10-29 (04:34 UTC): the research loop and the RSI rules in one place (WORKFLOW.md)

- **The owner asked** that the RSI rules and their research workflow always be referenced together when a decision comes back to them, and kept in the same spot.
- **[WORKFLOW.md](WORKFLOW.md) is now that spot:**
  - the research loop (rule 12, K13), with the owner's words;
  - the five RSI rules (13–17, K14), with their current values and the files behind each;
  - the workflow check, one line per item, which every decision brought to the owner ends with.
- **DECISIONS.md keeps rules 1–11** and points there for 12–17. The numbers are unchanged, so earlier references hold.

## 2026-10-29 (03:28 UTC): EXP-81 done: the fast path beats native incremental and WAL-G catch-up, each in its best mode

- **Every prediction held** (the `ha` set, 18 GB on 8 GB VMs, dest on XFS with reflinks):

  | Situation | Cell | Fast path | Competitor | Ratio |
  |---|---|---|---|---|
  | A backup chain | hot | 1.66 s | native incremental 5.73 s | **0.29** (deciding; bar 0.80) |
  | A backup chain | uniform | 41.89 s | native incremental 88.90 s | **0.47** |
  | A stopped standby | hot | 1.63 s | WAL-G catch-up 87.75 s | 0.019 |
  | A stopped standby | uniform | 41.92 s | WAL-G catch-up 109.00 s | 0.385 |

  - All 16 copies were correct.
  - The scorer was unchanged except one figure-layout line, disclosed.
- **Where the competitors' time went:**
  - **Native incremental's capture took 71 of its 89 s in uniform:** one request at a time, with the kernel's readahead merging its block reads, which my inference missed (it said 250–450 s).
  - **Its combine took 18 s;** at 27% of the pages it is barely faster than a full copy (0.98).
  - **WAL-G read all of the source in both cells** (17.8 GB) and was slower than a full copy in uniform (1.20).
- **A loss it shows, across sessions:** with summaries off, WAL-G's catch-up beats pgdelta's hashing for scattered changes (109 s against 180 s). Page-LSN selection on the source joins the idea archive for that row.
- **The scorecard:** research loop 2's two open rows are now wins.
- **Also:** two pipeline launches stopped at its own filesystem check, before any run (`xfs_info` needs root's PATH).

## 2026-10-29 (01:57 UTC): EXP-81 pre-registered: the fast path against native incremental and WAL-G catch-up

- **Each competitor's mechanism, from its source:**
  - **Native incremental's capture** reads each changed block with its own 8 KiB read, one at a time. Its combine rebuilds every changed relation file in a new directory.
  - **`--copy-file-range` shares each block on XFS:** a correction to research loop 2, which said the copy options apply only to whole unchanged files.
  - **WAL-G's catch-up** reads every file in full on the source, because a standby's file times never match the source's. It sends pages at or after the standby's checkpoint, compressed with Brotli, and syncs nothing on the receiver.
- **The lab's new arms:**
  - `incr`: the capture synced, then `pg_combinebackup --copy-file-range --sync-method=syncfs`;
  - `walg`: `catchup-receive` and `catchup-send`, then `sync -f`;
  - `STANDBY_TOO=1`: a cleanly stopped standby, B0s, with its `sb_` arms.
  - dest's `/data` is on XFS with reflinks.
- **The pilots at scale 20:**
  - every arm ran and every copy matched;
  - native incremental's best mode: from an unsynced capture the combine took 8.6 s, from a synced one 0.75 s, since XFS writes each block out before sharing it.
- **Predictions:**
  - 1 (deciding): the fast path at most 0.80 of native incremental's time in hot and uniform;
  - 2: at most 0.80 of WAL-G's;
  - 3: every copy correct.
  - Inferred from the code: about 0.15–0.3 and 0.1–0.17 against native incremental, and about 0.02 and 0.2–0.33 against WAL-G.

## 2026-10-29 (01:17 UTC): EXP-80b done: a crashed standby beyond memory, 0.023–0.233 of hashing's time

- **Every prediction held** (the `ha` set, us-west1-b and -c, 18 GB on 8 GB VMs; each standby killed 30 s into its change):

  | Cell | `--crashed-standby` | Hashing (today's path) | `pg_basebackup` | Over hashing | Over `pg_basebackup` |
  |---|---|---|---|---|---|
  | uniform | 42.0 s | 180.1 s | 91.0 s | 0.233 (deciding; bar 0.60) | 0.46 |
  | hot | 2.0 s | 87.9 s | 90.8 s | 0.023 | 0.02 |

  - Every copy was correct.
  - Every `--crashed-standby` run was proven "crashed standby", and every run without it was refused.
- **Each ratio landed on what the clean cells implied:**
  - the plans were the clean B0's exactly, since no restart point happened before the kill;
  - the guard cost the same.
- **Rules 16 and 17:** the scorer was unchanged (13 of 13 files). Every margin was over 10 times the noise. This session's runs, added to the noise floor, move its 95th percentile to 2.08% (10 s or more) and 7.54% (under 10 s).
- **Research loop 2's scorecard:** the "lineage unprovable" row splits. A crashed standby is now a measured win; a crashed primary and summaries off are still hashed.
- **Next, as research loop 2 planned:** the VM session against native incremental with `pg_combinebackup` and against WAL-G catch-up.

## 2026-10-29 (00:07 UTC): EXP-80b pre-registered: a crashed standby beyond memory, on the VMs

- **The question:** how much faster is a crashed standby refreshed with `--crashed-standby` than today's path for it (refused, so hashed), and than `pg_basebackup`? The case is a standby that stayed down until the source recycled the WAL it needs.
- **The lab's new mode, `CRASHED=1`:** B0 is a standby started before the change and killed with `pg_ctl -m immediate` 30 s into it. Arms `fast8wf2c`, `fast8wf2` and `full`, in hot and uniform; 2 runs each, seed 800.
- **Bars from the same set's clean-B0 cells** (EXP-64, -70, -79b):
  - uniform at most 0.60 of hashing's time (deciding; expected about 0.25);
  - hot at most 0.20 (expected about 0.02);
  - both at most 0.80 of `pg_basebackup`.
  - Correctness and the guard's decisions per run.
- **Rules 13–17:**
  - EXP-80 was the cascade's local stage;
  - `integrity.py register EXP-80b` records the scorer, the lab and pgdelta's modules;
  - the bars sit far outside rule 17's noise;
  - a held-out session (rule 15) comes before the option becomes a default.
- **Also:** the EXP-80 entry's heading carried the wrong UTC date (October 28); corrected to October 29.

## 2026-10-29 (00:03 UTC): EXP-80 done: crashed standbys correct through the fast path; crash recovery first wrong on S2e

- **Every prediction held:**
  - 100 of 100 randomized crashed standbys (45 immediate, 55 SIGKILL, mid-load with DDL and truncations) were refreshed by the fast path with `--crashed-standby` and came out correct: rows equal, `pg_amcheck --heapallindexed` clean. The failure rate is below 2.95% at 95%.
  - 20 of 20 crashed primaries were refused and hashed correctly.
  - In 3 of 3 fresh S2e builds, crash recovery first made a wrong copy, while as built each was refused and correct.
- **The first experiment scored under rules 13–17:**
  - **Rule 16:** its 12 scoring and tested files were unchanged since the registration commit.
  - **Rule 17:** doesn't apply (correctness counts).
  - **Rule 13:** this was the cascade's local stage. **Next: EXP-80b,** its VM stage, timing a crashed standby beyond memory against hashing and a re-clone.
- **Crash recovery first on crashed standbys:** 0 wrong in 40, masked by summary granularity as registered. S2e is the evidence against it.

## 2026-10-28 (23:57 UTC): the five workflow changes adopted (K14, rules 13–17)

- **The owner adopted all five changes** from the RSI review. Each now has a rule in DECISIONS.md and a file or script behind it.
- **Rule 13, a cascade and a proxy-fidelity log** (`research/proxy-fidelity.csv`, `analysis/proxy_fidelity.py`, figure `proxy-fidelity`).
  - Tests, then a local scale model, then the VMs.
  - Over 9 pairs so far, the VM ratio lay within 0.875–1.185 of the local one. So a change goes to the VMs on local evidence only with a local ratio of 0.80 or less.
- **Rule 14, an idea archive** (`research/idea-archive.md`): 30 ideas with status, evidence, conditions, and a trigger to revisit each.
- **Rule 15, a held-out workload set** (`research/held-out-workloads.md`), sealed:
  - H1: the clustered, tpcb, append and retention patterns, which no fast-path experiment has used;
  - H2: scale 1,200;
  - H3: a `pd-ssd` disk;
  - H4: PostgreSQL 18.
  - It runs at milestones only, and first before `--crashed-standby` or `--selector` becomes a default.
- **Rule 16, scorer integrity** (`analysis/integrity.py`, `experiments/registrations.json`). EXP-80 is registered as of its commit ca04163; its 12 scoring and tested files are unchanged since.
- **Rule 17, noise as a bar** (`research/noise-floor.csv`, `analysis/noise_floor.py`, figure `noise-floor`).
  - **Within a session:** across 52 cells of 10 s or more, the 95th-percentile spread is 2.27% (median 0.31%); under 10 s it is 7.7%.
  - **Between sessions:** `pg_basebackup` moved 0.28–0.31%.
  - **Interference:** EXP-79's disturbed cell (29%) is the kind of outlier the rule now checks for before scoring.

## 2026-10-28 (23:42 UTC): a review of recursive self-improvement work, against this project's workflow

- **The owner asked** whether RSI agents and papers offer a better workflow. The review is in [research/2026-10-28-rsi-review.md](research/2026-10-28-rsi-review.md).
- **What exists:** "RSI Agent" is not one framework. The field has three families: agents that edit their own code (DGM, HGM, SICA, Google's RRSI), LLM evolutionary program search (AlphaEvolve, OpenEvolve, ShinkaEvolve), and automated science (AI Scientist, AI co-scientist).
- **Their big loops need hundreds to thousands of evaluations,** about $7,000–22,000 a run. At about an hour of VMs per evaluation they don't fit, but their reported failure modes do apply.
- **Our loop matches what they converge on,** with stricter evaluation than most.
- **The gaps their lessons show:** no formal cascade or proxy-fidelity log; no idea archive with triggers to revisit; no held-out workloads; no automatic check of the scorer's integrity; noise not used as an explicit bar.
- **Five changes are proposed** to the owner. One optional use of RSI tooling: an offline evolutionary search over saved plans, the model only ranking and the VMs deciding.

## 2026-10-28 (23:28 UTC): EXP-80 pre-registered: crashed standbys by their minimum recovery point; crash recovery first found unsound

- **The owner approved crash recovery first.** Three pilots changed it before anything was registered:
  - **It works mechanically** (`postgres --single`), with `standby.signal` moved aside for a standby. But it moves B's redo point past where B's pages stop matching the server's.
  - **It reopens EXP-62's blind spot.** S2e, a new scenario, is an old primary that crashed past a failover split and lost the WAL from there, its last checkpoint record kept. Guard v2 refused it and hashed it correctly. After crash recovery first, the guard took it as a clean shutdown, and **the copy was wrong**.
  - **A standby has a sound proof without it:** its minimum recovery point bounds every page on disk even after a crash (PostgreSQL syncs it before writing past it), and a standby writes no WAL of its own.
- **Built:** `--crashed-standby` (opt-in). A standby left "in archive recovery" gets the proof "crashed standby"; crashed primaries stay refused. A real-server test covers it with and without the option.
- **EXP-80 (local):**
  - **arms:** 100 randomized crashed standbys (immediate or SIGKILL, at random points of a load with DDL and truncations), 20 crashed-primary controls, 40 crash-recovery-first trials, and 3 fresh S2e builds;
  - **deciding:** every crashed standby correct through the fast path.
  - **Then on the VMs:** timing against hashing and a re-clone (EXP-80b).
- **A residual risk, recorded:** a crashed old primary that lost its WAL past the split and was then crash-recovered by anyone fools guard v2, and `pg_rewind`'s same test.

## 2026-10-28 (23:07 UTC): research loop 2: the scorecard redone, and the next attack picked

- **Why:** the owner's correction. The goal is open-ended (keep improving until pgdelta beats its competitors where it can), and recent work had drifted into fixed-cost tuning at a limit where a full copy wins on principle.
- **The scorecard** ([research loop 2](research/2026-10-28-research-loop-2.md), figure `loop2-scorecard`): per situation, the standard tool and the strongest competitor, checked against primary sources (PostgreSQL 18, pgBackRest 2.60.0, WAL-G 3.0.9, Barman 3.20.1, pg_probackup 2.5.16, CloudNativePG 1.30.1, Patroni 4.1.5, Google Cloud's disk docs).
  - **Wins, measured:** an old primary after a failover; a standby behind with an archive; the fast path up to about 91% of pages; DR re-seeds; pgBackRest to a usable copy.
  - **Loss, measured:** copies whose lineage can't be proven (crashed; summaries off), where hashing must read all of B.
  - **Open:** native incremental and WAL-G catch-up against the fast path; cloud disk clones.
- **Novelty, documented:** no tool found applies WAL summaries, or a WAL-scan block list, in place. PostgreSQL's own incremental path always writes a new directory and rebuilds every changed file.
- **The pick:** the crashed copy.
  - **What:** finish its crash recovery first, as `pg_rewind` does (documented), then use the fast path.
  - **Why:** it is the only measured loss that isn't a hard limit, and the bound says it is an algorithm problem.
  - **Next:** EXP-80 (local, then VMs), then one session against native incremental and WAL-G catch-up.

## 2026-10-28 (22:40 UTC): the GCP web console reached

- **On the owner's authorization,** the console was opened in the owner's own Chrome (Claude in Chrome), where they were already signed in. The built-in browser has no session and stops at CMU's sign-in, which only the owner completes.
- **What it shows:** the project's welcome page (Postgres Delta Backup, under andrew.cmu.edu) and Compute Engine's VM list. The list is empty, matching `gcloud`: no VM is left running.
- **Nothing was changed in the console.** The tab was closed after. Project access for the experiments stays through `gcloud`, as before.

## 2026-10-28 (22:25 UTC): EXP-79b done: the guard's change firm, and `pg_basebackup` now wins only past about 91% of pages

- **Every prediction held in rule 11's second session**, run in EXP-75–77's order (uniform, then scatterall12). The source wrote 0.03–0.12 GB per run, as in EXP-77.
  - **The guard:** 0.550 of its time at 96% of the pages (2.92 s) and 0.545 in uniform. With EXP-79's 0.567, EXP-78's change is firm on the VMs.
  - **The fast path on EXP-77's cell** took 106.23 s against 108.48 s (2.25 s below, bar 2.0), with `pg_basebackup` at 101.10 s (−0.15%).
- **The break-even with the current code:** 0.604 at 53% (EXP-79's scatter12, clean runs) and 1.051 at 96% cross at about 91% of the pages, against 85% before. `fastpath-breakeven` now shows both sets.
- **What is left at 96%:** 5.1 s, of which 2.9 s is the guard. The selector's model, fitted before these changes, is refitted next on new cells.
- RESULTS, both READMEs and the research loop updated.

## 2026-10-28 (21:28 UTC): EXP-79 done (deciding held within rule 11's band); EXP-79b pre-registered

- **The guard on the VMs, with EXP-78's unsorted reading:** 0.64–0.69 µs per summary row in every cell, against 1.15–1.30 µs. That is 0.531 (uniform), 0.541 (scatter12) and **0.567 (scatterall12, deciding; bar 0.60)** of its earlier times. uniform's refresh fell from 42.64 to 42.04 s.
- **Predictions 3 and 4 failed, both by design.**
  - **scatterall12 ran after scatter12,** which had updated the same rows, so its plan chose 84% of the pages, not 96%.
  - **The source wrote 5.86 and 2.01 GB** during its first two runs (most likely autovacuum), against 0.03–0.14 GB per run in EXP-77. Those were the slow runs.
  - **scatter12's baseline row count** came from a session with another history.
  - The clean runs are reported only. scatter12 with the current code took 0.604 of `pg_basebackup` at 53% of the pages.
- **EXP-79b, rule 11's second session:** uniform then scatterall12, as in EXP-75–77. `analysis/exp79.py` gained `--patterns` and `--below`, and EXP-79's scores are unchanged.
- **Also:** the owner asked what tools the project uses. Kubernetes once (EXP-44, Omni's operator on a short-lived GKE cluster); Docker for the Omni probes (EXP-40 to EXP-43), the early benchmark harness and one CI job; otherwise plain Compute Engine VMs driven by `gcloud` and shell, `tc netem`, `pgbench`, and the incumbent backup tools.

## 2026-10-28 (20:11 UTC): EXP-79 pre-registered: the guard on the VMs, and the break-even with the current code

- **Why:** EXP-78's change was measured locally only (0.460). On the VMs the guard reads 1.15–1.30 µs per summary row, against the Mac's 0.51.
- **And:** the break-even (about 85%) was interpolated between points from older code in different sessions.
- **EXP-79, one ha pair:**
  - **cells:** uniform (the guard), scatter12 (53% of pages) and scatterall12 (96%), with `pg_basebackup` in the last two;
  - **deciding:** scatterall12's guard at most 0.60 of its 5.31 s;
  - **also:** uniform's guard at most 0.60, scatterall12's fast path at least 2 s below EXP-77's with `pg_basebackup` within 1%, and the same summary rows as before.

## 2026-10-28 (20:05 UTC): EXP-78 done (local): the guard's reading without the server's sort; project access checked

- **Project access, read-only, through `gcloud`** (the owner's credentials):
  - the project is ACTIVE, with the Compute, Logging, Monitoring and Storage APIs enabled;
  - quotas: 32 vCPUs across all regions (the binding limit); `n2d` CPUs 16 per region, exactly one ha pair;
  - usage 0, so no VMs are left.
  - The web console still asks for CMU's sign-in in the built-in browser, which I don't complete. Using the owner's own Chrome needs their agreement, which is asked for.
- **The guard's reading, in parts** (a pilot, then EXP-78, pre-registered before its run):
  - the server's grouping alone took 0.09 s of 0.41 s at scale 320;
  - the sorted, distinct arrays were most of the rest; the JSON and psql almost nothing.
  - **The pilot's first merge was wrong:** the server returns OIDs as JSON strings. It was found by its plans not matching, and fixed before EXP-78.
- **EXP-78 (held):** unsorted with duplicates took 0.460 of today's time (0.187 against 0.407 s), with identical plans in 5 of 5 runs. Over 4 connections: 0.323, not built (3 more connections to the source).
- **Built:** `live.summary_blocks` returns each fork's blocks unsorted; `grouped_plan` de-duplicates them as before. The unit test gained unsorted, repeated blocks; the real-server plan test passes; 1,649 tests pass.
  - **Projected for the VMs** (not measured): the guard at scatterall12 from 5.3 to about 2.4 s. The next VM session times it.

## 2026-10-28 (19:27 UTC): EXP-77 done: its deciding prediction failed, but the stall is gone

- **Deciding (failed):** with both arms reading each written run in one call, the check on the verifier saved only 0.44 s over the main loop in scatterall12 (bar 3.0 s).
- **Held:** uniform no slower (0.13 s faster); the source disk's idle stretches while sending 0.51–0.76 s (EXP-76: 3.8–4.8 s); every page still re-read (3.0–3.2 µs a page, 7.0–7.5 s per run, off the critical path); 10 of 10 runs correct.
- **Most of the gain was the batching, in both arms.** Against EXP-76 in the same cell and set-up, the main-loop arm fell from 113.20 to 108.92 s, and the verifier's to 108.48 s. Unchanged arms agree within 0.3% between sessions.
  - The design didn't compare the old page-by-page loop with the new code in one session, so the measured effect is between sessions. That is recorded post-hoc, with no verdict changed.
- **Against `pg_basebackup` at 96% of the pages:** 1.071 (EXP-75: 1.119). Of the 7.2 s left, the guard is 5.3 s and the plan 0.9 s.
- **The break-even figure** marks EXP-77's point apart: the cells below 96% weren't re-measured with the fix.
- **The check stays on the verifier,** as built. **Next: the guard.**

## 2026-10-28 (18:05 UTC): EXP-76 done (failed); the stall it found fixed; EXP-77 pre-registered

- **EXP-76's deciding prediction failed.**
  - **The tail fell as predicted:** early write-back on the worker cut the receiver's last wait from 3.4 to 0.04 s.
  - **The transfer stretched:** `sync_file_range` stalled the receiver's main loop when the disk's queue was full. The result was 1.81 s saved where requests bind (uniform) and 3.75 s lost where bytes bind (scatterall12). `--fsync worker-writeback` stays opt-in.
  - **`pg_basebackup` has the same last flush (about 3.6 s),** so the tail is no extra cost where the destination disk binds.
  - **Rule 11's second uniform session is not run** (a recorded deviation): it can't change the verdict, and the receiver changes next.
- **The timeline found the rest:** a stall at every 1 GiB file's end, with both disks dipping about 15–18 times per run.
  - **The cause:** the receiver re-read every written page one `pread` at a time and hashed it in its main loop, 2.34 M pages at scatterall12. Its time was hidden, since `verify_seconds` is overwritten by the verification pool's busy time.
  - **Accounted against `pg_basebackup` at scatterall12 (12.25 s):** the guard 5.3 s, the plan 0.9 s, the stalls about 5 s more of sending (about 9 s at `pg_basebackup`'s rate), and start and tail about 1 s.
- **Fixed:** the check now runs on the pipelined verifier, on the pool, reading each written run in one call. COMPLETE still waits for every check before anything is published. Its time is reported as `fast_path.reread_seconds`.
  - **Tests added:** the records' digest equals the page-by-page one on every pool; every page sent is re-read on a real server; and a page written one page away fails the session with exit 4 and B blocked, under both verify modes. No such test existed before.
  - **Locally (scale 80):** 2.09–2.16 s against 2.34–2.56 s.
- **EXP-77** times it on the VMs, with the check in the main loop (`--verify sequential`) against on the verifier, in the same two cells, with `pg_basebackup` and the timeline.
  - **Deciding:** at least 3.0 s saved in scatterall12.
  - **Also:** uniform no slower, the source disk's idle stretches under 1.5 s, every page still checked.

## 2026-10-28 (16:30 UTC): EXP-76 pre-registered: the fast path's tail, and a timeline of both disks

- **Why:** EXP-75 found the fast path's fixed costs, not the disks, put `pg_basebackup` ahead at 96% of the pages. The statistics from EXP-74 and EXP-75 (16 fast runs) split those costs very evenly:
  - launch 0.55 s;
  - the guard 1.9–5.3 s;
  - a tail of 3.5–4.1 s after `pg_backup_stop`, nearly all the receiver waiting for its sync worker's last fsyncs (3.1–3.8 s);
  - about 8 s at `scatterall12` that the statistics can't place.
- **Built:**
  - **`--fsync worker-writeback`:** the sync worker with early write-back every 8 MiB (`sync_file_range`), so the last fsyncs have little left to flush. Tests extended to it; the real call runs on Linux in CI.
  - **The sender's phase times:** guard start and the sending's first and last file, as wall-clock times in its statistics.
  - **The lab:** arm letter `b` for `worker-writeback`; `SERIES=1` samples both VMs' disks every 0.25 s (`disk_series.sh`).
- **EXP-76:** the `uniform` and `scatterall12` cells, worker against worker-writeback, and `pg_basebackup`'s timeline beside them.
  - **Deciding:** at least 2.0 s saved in each cell.
  - **Also:** the receiver's wait under 1 s, backup mode within 3%, and the guard using neither disk.
- **The web console:** still CMU's sign-in in the built-in browser. Moving to the owner's own Chrome needs their agreement, so it is asked for.

## 2026-10-28 (16:18 UTC): EXP-75 done, the selector held, and pg_basebackup's win placed near 85% of pages

- **Every prediction held.**
  - **Deciding:** `--selector` took 1.008 of fill 2's time in EXP-70's uniform cell (bound by requests) and 1.001 of fill 0's in `scatterall12` (96% of the pages). It chose those same fills in every run, and the choice took at most 0.15 s.
  - **Its flag:** it said `pg_basebackup` was faster in both `scatterall12` runs and in neither uniform run. It was right: 100.94 s against 112.99 s (0.893, bar 0.95).
  - **The model:** within 1.5% for fill 2 and 6% for fill 0 (over-predicting at nearly every page), and within 0.2% for `pg_basebackup`. Every copy was correct (10 of 10 runs; the changed `events` rows counted).
- **The break-even:** interpolating with EXP-74's scatter12 (0.64 at 53% of pages), the fast path stops beating `pg_basebackup` near 85% of the pages changed; the model gives 86%. New figure `fastpath-breakeven` (`analysis/breakeven_fastpath.py`, from the write-ups' medians), in EXP-75 and RESULTS.
- **Where the fast path loses at 96%:** both arms kept the disks busy for the same 95 s. The 12 s gap is the guard (5.3 s for 4.6 M summary rows) and about 7 s more beyond the busier disk's time: fixed costs, the next target.
- **Post-hoc (no verdict changes):** the overhead beyond the guard and the busier disk was 12.6 s at 20 GB, against the fit's 20.0 s, so it grows less than linearly with the bytes.

## 2026-10-28 (14:59 UTC): EXP-75 pre-registered (the selector timed); a full copy becomes a recommendation

- **Why EXP-75:** EXP-74 registered timing the selector in a later session, and its cells never reached a full copy's win.
- **A finding while designing it, from the code:** in fast mode, a file sent whole is read and hashed in full, then read again to send, in the sending thread and outside the read-ahead.
  - That path was built for the few files the summaries don't cover.
  - The selector's first build used it to send a whole database when a full copy was predicted faster. That would have read about 21 GB twice on 8 GB VMs, roughly twice `pg_basebackup`'s time.
- **And by the model, pgdelta's own full copy can't beat its fill 0:** it is the same pipeline with every page chosen, and fill 0's pages are a subset of those. A full copy wins only by avoiding pgdelta's overhead of about 1 ms per MB.
- **So the selector chooses among the fills only.** It still predicts `pg_basebackup`'s time, and the statistics (`full_copy_predicted_faster`) and the log say when that is faster. Whether to run it instead is the operator's call. Tests updated; the real-server test with slow disks checks the flag.
- **EXP-75's cells:**
  - EXP-70's `uniform` cell, bound by requests (`--selector` against fill 2);
  - `scatterall12`, new: 12% of the rows of both `pgbench_accounts` and `events`, so nearly every page changes (`--selector` against fill 0 and `pg_basebackup`).
- **Deciding:** the selector within 5% of the fixed fill in each cell.
- **Also predicted:** its flag is right on both sides, and in `scatterall12` `pg_basebackup` takes at most 0.95 of the fast path's time. That would be the first measured cell past the break-even.
- **The web console** still redirects the built-in browser to CMU's sign-in, which I don't complete. The project is reached through `gcloud`, which created and deleted both VM sets today.

## 2026-10-28 (14:35 UTC): the selector built into the sender (`--selector`, off by default)

- **Why:** EXP-74's predictions 1–3 held, and its registration said that if they did, the selector goes into the sender behind an option, off by default.
- **What it does:** after the guard, it predicts the fast path's time at fills of 0, 1, 2 and 4 pages and a full copy's, then takes the fastest. That is either the fill, or every listed file sent whole with B unread.
  - **The model:** EXP-74's, in `pgdelta/selector.py`.
  - **Disk limits:** from `--selector-read-ops`, `--selector-write-ops` and `--selector-mb-per-second` (defaults: EXP-74's pair).
  - **Statistics:** `fast_path.selector` gives each option's prediction, the choice and the choice's own time.
- **Counting each fill cheaply:** each file's plan is reduced to its runs of consecutive pages and the gaps between them, so every fill's pages and requests come from one pass.
  - It matches `fastpath.fill_gaps` and the sender's runs exactly on random plans.
  - Its predictions match the analysis model's within 0.03 s.
  - It took 0.14 s on the Mac for 1.2 M scattered pages.
- **Tested on a real server:**
  - with the default limits it picks a fill, and the copy recovers to the source;
  - on disks set to read 1 page a second it predicts a full copy faster, sends every file whole, and that copy recovers too.
- **Not yet timed.** Its full copy is pgdelta's own (every file whole), not the `pg_basebackup` EXP-74 timed. A later session times both.

## 2026-10-28 (14:30 UTC): EXP-74 done, the model and selector held; the guard's grouped reading confirmed on the VMs

- **The selector (deciding, held):** in both new cells (4% and 12% of rows changed) it picked fill 0 from the unfilled plan and the guard's time. Fill 0 was the fastest arm measured in both.
  - In scatter4, fill 2 took 1.050 of fill 0's time, so the choice mattered. In scatter12 the fills were within 0.7%, so that cell tested only the fast path against a full copy.
- **The model:** every fast arm within 2.6% of its prediction (7 arms, 0.974–1.024), and both full copies within 0.1%.
  - Its byte term was right. Its request count was 33–41% high for long runs, which didn't matter because those plans were bound by bytes.
- **The guard on the VMs:** 1.91 s against EXP-70's 4.70 s on the same summary rows (0.406, bar 0.6). The scattered cell took 43.8 s, against EXP-70's 46.6.
- **Not reached: a cell where a full copy wins.** The fast path took 0.59 and 0.64 of a full copy.
  - The updated table was 92% and 100% changed. The database's untouched 8.4 GB caps the fast path at about half of the listed pages.
  - The model puts the break-even at about 85% of the listed bytes for byte-bound plans (not measured).
- **Next, as registered:** the selector goes into the sender, behind an option and off by default.

## 2026-10-28 (13:43 UTC): EXP-72 done, TLS costs about 2% on the fixed transport

- **Sign-in:** `gcloud`'s stored credentials still failed to refresh at first, in the owner's terminal too. A sign-in at 12:54 UTC took, and both pipelines started at 12:55 (EXP-74's after a fix: its script wasn't executable).
- **Capacity:** every us-central1 zone was out of `n2-standard-8` VMs with local SSDs. The pipeline retried every 15 minutes and got us-central1-a, the registered region, on its third try. EXP-74's ha set got us-west1-b and -c, EXP-70's zones.
- **EXP-72 (every prediction held):** inside pgdelta's own TLS, a 2.1 GB refresh took 1.018 of its in-clear time at 2 Gbit/s and 60 ms (bar 1.10), and 1.024 at 5 Gbit/s (bar 1.20). EXP-58 measured 1.033 and 1.148 on the old transport.
  - TLS added 0.3% to the wire bytes and a 69 ms handshake. All 12 runs passed, and the controls were within 2.3% of EXP-58's.
  - **Reading:** most of EXP-58's cost at 5 Gbit/s was the old transport, not the encryption. The two speeds' costs are within run-to-run spread, so this says "at most a few per cent", not 1.8 against 2.4.
  - **Applied as registered:** pgdelta's README and RESULTS give EXP-72's figures in place of EXP-58's "likely an upper bound", and EXP-58's note points to EXP-72. O6 stays met.

## 2026-10-28 (12:40 UTC): EXP-74 pre-registered, the selector's model (idea 4); VM work waits for gcloud

- **The model** (`analysis/fastpath_model.py`) predicts the fast path's time from its plan, which the sender knows once the guard has run.
  - **Each disk's time:** the plan's requests (runs split at 168 KiB) at its operation rate, or its bytes at 210 MB/s, whichever is larger. Every constant comes from an earlier session.
  - **The rest:** the guard plus an overhead fitted on two EXP-70 cells (0.6 s and 0.97 ms per MB sent).
  - **A full copy:** priced from the plan's listed bytes (EXP-70's plan listed 17,866 MB, and its full copy read 17,865).
  - **In-sample on EXP-70:** every scattered arm within 2.5–6%, and the full copy within 0.3%. It picks fill 2, which was the fastest arm. It misses the hot cell by 47%, where fixed write overhead dominates.
- **EXP-74** tests the model and the selector on cells they haven't seen.
  - **The cells:** 4% and 12% of rows changed (about two-thirds and nearly all of the pages), with fills 0–2 and a full copy, plus EXP-70's uniform cell, which times EXP-73b's grouped reading on the VMs.
  - **Deciding:** the selector's pick within 5% of the fastest arm in each cell.
  - **The lab change:** a `scatterN` pattern updates N% of rows chosen by a hash of each key, in one statement. A first version chose them with a seeded `random()`, which a local check showed depends on the table's physical order. The hash picks the same rows either way.
- **`gcloud` is still signed out,** so EXP-72 and EXP-74 wait. Both pipelines are ready, and together they fit the 32-vCPU quota.

## 2026-10-28 (11:40 UTC): EXP-73 and EXP-73b, the guard's summary reading halved

- **Where the guard's time goes (EXP-73, local scale models):** it is the reading of the WAL summaries, linear in their rows. It costs 0.9 µs per row on the Mac and about 3.2 µs on the VMs (EXP-70: 22,000 to 1.46 M rows). The cost is spread over the server's JSON (44%), psql and `json.loads` (25%), and Python's tuples and sets (31%).
- **EXP-73's deciding prediction failed:** B (one query grouped per relation fork on the server) took 0.547 of today's time, against a bar of 0.4. Every plan was identical.
  - **Post-hoc:** the scale models had 1.51 rows per distinct block, against the VMs' 2.4.
- **EXP-73b** matched the duplication (20 checkpointed batches and the lab's state check: 2.40 rows per block).
  - **Deciding, held in two sessions under rule 11:** 0.456 and 0.472 of today's time at scale 320, with all 9 plans identical.
  - **EXP-73's post-hoc model failed out of sample** (0.530 against 0.344), because the server still reads and sorts every row.
- **Applied as registered:** the sender reads the chosen summaries in one grouped query (`live.summary_blocks`, `fastpath.grouped_plan`). A unit test and a real-server test show it builds today's plan, and every fast-path test passes on it.
  - **Projected for EXP-70's scattered cell:** about 4.7 → 2.2 s of guard, or 5% of the refresh. Not measured yet; the next VM session times it, with EXP-72.

## 2026-10-28 (10:30 UTC): EXP-72 pre-registered (TLS's cost on the fixed transport); the console checked

- **EXP-72** repeats EXP-58's design on the fixed TLS transport, with seed 720. EXP-58's figures were measured on the transport that broke OpenSSL's threading rule, so the docs call them "likely an upper bound", which is only inferred. EXP-72 measures instead.
  - **Deciding:** at 2 Gbit/s, TLS takes at most 1.10 of clear's time (EXP-58's bar).
  - **Expected, reported and not scored:** both ratios at or below EXP-58's, and less CPU added on the receiver.
  - **Design fix before registering:** a bar set at EXP-58's own 5 Gbit/s ratio (1.15) would make an unchanged cost a coin flip, so EXP-58's bars stand. The comparison with EXP-58 is reported beside them.
- **The owner's Cloud console** opens to CMU's web login in the built-in browser. Signing in is the owner's to do. The project itself is reachable through `gcloud`, which every VM experiment uses.

## 2026-10-28 (09:50 UTC): a Ctrl-C while the hashing workers start no longer breaks the pool

- **Found from CI:** the TLS fix's run (f29ceaa) failed in `test_ctrl_c_to_the_whole_process_group_mid_run`. A worker was still importing `hashlib` when the test's SIGINT reached the process group, and printed a `KeyboardInterrupt` traceback. The pool doesn't wait for its workers before work starts, and a worker ignored SIGINT only once its initializer ran, after its imports.
- **A new test** sends SIGINT to the group right after a pool spawns two workers. On the code before the fix the workers died and the pool broke (`BrokenProcessPool`) in 3 of 3 runs.
- **The fix** (`tree._sigint_blocked`): the parent blocks SIGINT in the spawning thread while the pool starts its workers. A child inherits that mask across fork and exec, and its initializer ignores SIGINT before unblocking it, so a Ctrl-C during its imports is dropped. The parent's own Ctrl-C stays pending until it unblocks, so it isn't lost. The new test passed 3 of 3 runs.

## 2026-10-28 (09:34 UTC): TLS transport fixed: one SSL object was used by two threads at once

- **Found from CI:** the Experiment tests on 4918b34 hung for 1 h 45 min in `test_a_session_over_tls_builds_the_same_copy` (earlier runs took about 4 min); the run was cancelled. The same commit's code passed on 9eb2eb0's run, so the hang was intermittent.
- **The cause:** a session reads on one thread while another writes, and pgdelta's TLS did both on one `ssl.SSLSocket`. OpenSSL forbids using one SSL object from two threads at once, and CPython leaves that to the caller.
  - **A full-duplex stress** (`experiments/tls_duplex_stress.py`: each end writing on one thread while reading on another, every byte checked) crashed the process with SIGBUS in OpenSSL's `bio_write_intern`. That happened within 30 rounds of 64 MiB, three times in three tries (rounds 29, 0 and 3). Python 3.14.7, OpenSSL 3.6.4, on the Mac.
  - **The TLS tests,** repeated 150 times on an untouched checkout, hung once.
- **The fix** (`transport._TlsConnection`): the TLS state is an `ssl.SSLObject` over memory BIOs, touched only under a lock, and the socket's `recv` and `sendall` run outside it. A second lock keeps records in order, and a reader never waits on a writer blocked by the peer.
  - **A first version deadlocked** on a refused handshake: a failed write kept the send lock, and the close's flush then waited on it. The TLS tests caught it, and it was fixed before anything was committed.
- **Evidence for the fix:** 100 of 100 stress rounds (6.4 GiB each way, every byte checked), and 150 of 150 test repeats without a hang. A new test sends 16 MiB each way at once.
  - **Bulk throughput,** measured locally on one way: about 1,650 MiB/s against the old transport's 670, in three interleaved trials.
  - **EXP-58** gets a note: its TLS cost was measured on the old transport, so it is likely an upper bound now (inferred). No run there failed.

## 2026-10-28 (09:20 UTC): EXP-71 done, `--diverged-basis` on with the fast path

- **EXP-71** (one session, 12 runs, all correct; both `pg_amcheck --heapallindexed` checks passed): all 6 predictions held.
  - **Deciding, hot rows updated 10 times:** pgdelta with idea 1 took 6.9 s against `pg_rewind`'s 35.1 s (0.197; the model gave 0.2) and a re-clone's 90.8 s.
  - **1% of rows scattered:** 48.2 s against a re-clone's 91.1 s (0.529) and `pg_rewind`'s 457.5 s (0.105).
  - **B's side:** 20,199 and 20,094 blocks from B's WAL, exactly EXP-69's `pg_waldump` count, read in 0.65 s. C, the last checkpoint before the fork, was one segment before it in both lags.
  - **Controls:** `pg_rewind` and the re-clone within 4% of EXP-67's times.
- **Applied as registered:** `--diverged-basis` is on with `--fast-path` (CLI only; `fastpath.DEFAULT_DIVERGED_BASIS`; `--no-diverged-basis` turns it off), with tests. The receiver still accepts it only for a cleanly stopped basis.
  - The lab scripts keep their registered arms: EXP-67's fast arm, EXP-64's and EXP-65's fast arms and EXP-63's `fastpath_check.py` pass `--no-diverged-basis`.
- **Research loop 1's failover case is closed.** Next: the fast path's fixed costs (the guard took 5.4 s of the scattered session's 46.8 s), then the selector (idea 4).
- **The pipeline** ran without a pause (us-west1-a had no capacity, so us-west1-b and -c); the VMs and disks were deleted after the fetch.

## 2026-10-28 (07:30 UTC): idea 1 built into pgdelta (`--diverged-basis`); EXP-71 pre-registered

- **`--diverged-basis`** (sender, with `--fast-path summaries`; offered in HELLO, so other sessions' bytes are unchanged). It is for an old primary that kept writing after the server's timeline left its own, which the guard refused until now.
  - **The exchange:** the sender names the fork (`FORK_POINT`). The receiver finds the last checkpoint before it in B's own WAL and lists every block B's WAL names from its redo point through B's end (`BASIS_WAL`).
  - **What is sent:** those blocks join the summaries' blocks since that checkpoint, each the server's current copy, so none of the server's WAL is replayed.
  - **The guard refuses** a B that wasn't stopped cleanly, whose WAL doesn't read through without a gap, that has neither checksums nor `wal_log_hints`, or whose WAL holds a record of an extension's resource manager or a Database or Tablespace record.
  - **Finding the checkpoint:** windows ending at the fork, each twice as long as the last, so the receiver doesn't read all the WAL B keeps (EXP-67's old primary keeps up to 40 GB).
  - **Tests:** a real failover, a standby promoted while its primary kept writing, both sides changing rows and creating a table of the same name. The old primary took the fast path with 4,019 blocks from its WAL, and the copy matched the server and passed `pg_amcheck`. Without the option, and with its WAL removed, it was refused and hashed, and both copies matched too. Unit tests cover the guard, the window search and the messages.
- **EXP-03's classifier gains PostgreSQL's fuller heap mask, as an opt-in** (`classify.mask_heap_postgres`, moved from EXP-69b's post-hoc script).
  - **What it masks:** the tuple's command id, a speculative insertion's ctid and the padding, as `heap_mask` does.
  - **How it's used:** given a page's block number, `classify_pair` calls a page that differs only there `replay_only`. Registered analyses don't pass one, so their categories and counts are unchanged.
  - **Checked:** EXP-69b's post-hoc check reproduces byte for byte.
- **EXP-71 pre-registered:** idea 1 timed against `pg_rewind` and a re-clone on EXP-67's cells, with a new `union` arm in EXP-67's lab.
  - **The deciding prediction:** hot10 takes at most 0.5 of `pg_rewind`'s time (the model gives about 0.2).
  - **Uniform:** at most 0.75 of a re-clone's (about 0.57).

## 2026-10-28 (07:14 UTC): EXP-70 done, `--fill-gaps 2` is the fast path's default

- **EXP-70** (one session, 14 runs, all correct): all 6 predictions held.
  - **Deciding:** filling clean gaps of up to 2 pages took 0.608 of no filling (46.6 against 76.6 s); the model gave 0.69.
  - **The scattered cell** (28% of pages) now refreshes in 0.512 of a full copy's 91.0 s.
  - **Filling 1 page** gave 47.4 s, and filling 4 pages 55.8 s (1.197 of 2, as predicted).
  - **Requests fell** to 0.346 of no filling on both disks, and each filled plan was exactly `fill_gaps` of the unfilled one.
- **A post-hoc check** (`analysis/exp70_posthoc.py`, no verdict changes): past 1 page, both disks move bytes at a full copy's rate while busy, so fewer operations are paid for in bytes. The model overstated small writes' cost: the destination completed about 4,790 writes per busy second, against `fio`'s 4,195 for random 8 KiB writes.
- **Applied as registered:** `--fill-gaps` defaults to 2 with `--fast-path` (CLI only; `fastpath.DEFAULT_FILL_GAPS`), with tests.
  - The lab scripts keep their registered arms (`--fill-gaps 0` where no fill was registered).
  - `lab_exp65.sh` and EXP-63's `fastpath_check.py`, missed when EXP-68 changed the defaults, now pass their registered settings explicitly.
- **The pipeline paused once** on a transient `gcloud` SSL error at the smoke's records check. The VMs were kept as designed, the records were checked by hand, and the registered run resumed on the same pair. The VMs were then deleted.

## 2026-10-28 (05:54 UTC): EXP-69 part B done, `pg_rewind`'s 4 GB explained; EXP-70 running

- **EXP-69 part B** (EXP-67's cells with diagnostics, 18 GB):
  - **As registered,** 6 held both as registered and as amended; 7 and 8 failed.
  - **Control:** `pg_rewind`'s blocks equal `pg_waldump`'s, about 20,000 per lag.
- **The "4 GB extra" was S's own WAL,** written after the lab recorded S's LSN. The lab's state check read all of `pgbench_accounts` right after the lag's checkpoint, and with checksums each page's first hint bit logged a full-page image: 3.76 GB, about 6 KiB per changed page. S's WAL wasn't kept, so this is inferred.
  - **For EXP-67:** its uniform `pg_rewind` path replayed about 11.8 GB, at 30.5 MB/s, matching EXP-65. Without the check's WAL it would be about 313 s, not 459 s. Its verdicts stand, and a correction note points to EXP-69.
- **Idea 1's block set at scale** is 0.94 and 0.997 of hashing's pages. It is the case for building idea 1 next for the failover case.
- **Found:** `fastpath.covered` treats a page header at a timeline switch on a segment boundary as a gap, a needless refusal. A fix with a test is queued.
- **A figure change after the data:** part B's panel now splits off the WAL written by the state check. The labs now also record S's LSN after the check.
- **EXP-70** (`--fill-gaps`) launched on a fresh pair right after part B's teardown.
- **The guard fix, done:** `fastpath.covered` now treats a hole of exactly one WAL page header (24 or 40 bytes) at a page boundary as no gap, since no record lies in a page header. A unit test covers the shape PostgreSQL 17.11 produced in EXP-69 (one timeline's summary ending at 6/D2000000, the next starting at 6/D2000028), and holes that must still refuse. The experiment harness's own check is unchanged, so registered analyses reproduce.

## 2026-10-28 (late night): idea 2 built, EXP-70 pre-registered; EXP-69 part B running

- **`--fill-gaps PAGES`** (sender, with `--fast-path`): each clean gap of at most PAGES pages between two chosen pages is sent too, so two runs become one read and one write (`fastpath.fill_gaps`).
  - **Tests:** a real-server refresh with `--fill-gaps 2` fills more than 0 pages, and its copy recovers to the source's state and passes `pg_amcheck`.
  - **What EXP-68 found it attacks:** the fast path's scattered case is bound by the disks' operation rate, and the rule re-applied on the real page set says to prototype idea 2.
- **EXP-70 pre-registered:** fills of 0, 1, 2 and 4 pages on EXP-64's uniform cell, and 0 and 2 on the hot cell. The deciding prediction: filling gaps of up to 2 pages takes at most 0.85 of no fill (the model gives 0.69).
- **The lab scripts' arms are now explicit:** EXP-68 changed the fast path's defaults, so `lab_exp64.sh` and `lab_exp67.sh` pass every setting, and old arms keep their registered meaning.

## 2026-10-28 (night): EXP-68 done, the fast path beats a full copy at 28% of pages; EXP-69 part B running

- **EXP-68:** all 7 predictions held, all 14 runs correct, on the `ha` set in us-west1; its VMs were deleted after the fetch.
  - **With the receiver's syncs on its worker thread,** 8 reader threads took 76.5 s, against 101.9 s syncing in the main loop (0.751) and a full copy's 90.9 s (0.842).
  - **16 threads** added nothing (0.998). The hot cell gained: 1.77 against 1.96 s.
  - **The destination** completed 1.018 write requests per run of the plan.
- **What binds now is the source's operation rate:** the sender waits 43 s on its readers, and the source's disk was busy 61.7 s for 254,547 reads, about the cap EXP-66 measured.
- **Idea 2's rule** (EXP-66's, re-applied on the real page set) says to prototype it. The model without filling is within 14% of the measured writing; filling gaps of up to 2 pages cuts it by 41%.
- **Applied, as registered:** `--read-ahead` defaults to 8 with `--fast-path`, and the receiver's `--fsync` defaults to `auto` (the worker when the fast path is offered, else the main loop). Tests cover both.
- **A figure panel added after the data:** "sender waiting on its reader threads". No score changed.
- **EXP-69 part B** launched on a fresh pair right after EXP-68's teardown.

## 2026-10-28 (evening): EXP-69b done, idea 1's copy is right wherever its fixed guard passes

- **As registered, 4 of 5 predictions held.** Over 8 builds and a WAL-cut copy:
  - the fixed guard passed kept S2, kept S2c, default S2c and S2h, and refused the other five for their reasons;
  - every passed copy started with rows equal to S's, and left 0 contradicted hints;
  - idea 1 sent 1.11–1.17 times the pages that differ.
- **The deciding prediction failed by one page.** EXP-03's classifier counted 1 real wrong page in S2h: `pg_depend` block 12.
  - **Post hoc:** three tuples there differ only in their command ID. The old primary kept 2, 2 and 4; the promoted standby has 0, as replay writes it.
  - **PostgreSQL's own `heap_mask`** masks that field ("During replay, we set Command Id to FirstCommandId. Hence, mask it."); EXP-03's classifier doesn't. Under PostgreSQL's mask, no unsent page differs in any passed build.
  - **The gap can only over-count,** so no earlier verdict rests on it.
- **The checksum condition is necessary:** in S2i (no checksums, no `wal_log_hints`), the union left 345 pages of hints naming a transaction S never committed. Its copy showed 20,000 rows S doesn't have. The guard refused it.
- **Next:** EXP-69 part B on EXP-68's VMs once they're free, then idea 1's prototype in pgdelta, registered and timed against `pg_rewind`.

## 2026-10-28 (late afternoon): EXP-69b pre-registered

- **What it re-tests:** EXP-69 part A's verdicts, with the end check fixed (`pg_waldump --end`) and a hint check that reads each transaction's fate from S's own `pg_xact`. It adds two things:
  - **an end-to-end check:** idea 1's copy is built from B plus the sent pages, started, and its rows compared with S's;
  - **two new scenarios, S2h and S2i:** a transaction open at the fork, committed and read on the old primary only, with and without data checksums. They test whether the checksum condition is needed: without it, B's hint bits name a transaction S never committed, in no WAL.
- **Checked before registering:** a build smoke of S2h and S2i (their build checks only), 15 unit tests, and the analysis dry-run on synthetic results.

## 2026-10-28 (afternoon): EXP-69 part A done; part B amended before its run

- **Part A, as registered: 2 of 5 predictions held, the deciding one failed.**
  - Idea 1's guard passed S2 (0 real wrong pages, where S's summaries alone left 1,276), but refused S2c, which it should have passed.
  - Its hint check flagged 19 pages.
  - `pg_rewind` took 13 fewer blocks than `pg_waldump` named.
- **Post-hoc checks (`analysis/exp69_posthoc.py`) trace each failure, none to a missed page:**
  - **S2c:** the guard compared the start of B's last WAL record with a standby's end pointer. B's WAL reads through to its end.
  - **The 19 pages:** their hints (82 tuples, `XMIN_COMMITTED`) name transaction 791, which S committed too. They were set before the common checkpoint (same page LSN on both sides), so they are common history and harmless.
  - **The extra blocks:** all lie beyond S's end of file. `pg_rewind` truncates them by design.
- **Where B's WAL was complete,** the send set left 0 real wrong pages in all 4 builds. **Where it was lost** (S2d), 1,443 were left, and the guard refused.
- **Driver bugs:** two, fixed before any page was scored (a relative path, and the scenario's keys). They are recorded in the write-up.
- **Part B, amended before its run:** prediction 6 is also scored with blocks beyond either side's end of file excluded, and the diagnostics record file sizes and B0's own end.
- **Next:** EXP-69b, registered before any run, re-tests part A with three changes:
  - the end check fixed;
  - a hint check that tests each transaction's fate on S;
  - a scenario that makes the hint danger real (B without checksums or `wal_log_hints`, a transaction open at the fork and committed on B).

## 2026-10-28 (midday): EXP-69 pre-registered, idea 1's premises; EXP-68 running

- **Why now:** EXP-67 said idea 1 (both sides' block sets, for an old primary after a failover) is next, after a measurement of what it rests on: its correctness and guard, and why `pg_rewind` copied 4 GB beyond S's WAL.
- **Found while checking formats** (a probe on one S2 build, before registering):
  - **Retained WAL is a precondition:** EXP-61's S2 can't be rewound at all. B's shutdown checkpoint had recycled the WAL back to the last common checkpoint, and `pg_rewind` failed for want of it. Idea 1 shares that condition, so EXP-69 builds B with its WAL kept, as `pg_rewind`'s users must, and keeps EXP-61's builds as the refused case.
  - **Its debug output gives the whole copy plan:** `pg_rewind --debug` lists every block it takes, every file it copies whole and every chunk it fetches. Even in S2, most of its copy was whole WAL segments, some past S's current LSN.
- **EXP-69's two parts:**
  - **Part A, local:** idea 1's send set scored on EXP-03's failover scenarios, its guard on crashed, cut-WAL and recycled-WAL cases, and `pg_rewind`'s blocks as the control.
  - **Part B, on the VMs after EXP-68:** EXP-67's cells rerun with diagnostics: `pg_rewind`'s copy by kind, B's WAL, S's summaries and S's directory.
- **New code:**
  - `unionscan.py` and `union_check.py`, with 10 unit tests on the tools' own output shapes;
  - `lab_exp67.sh` gains `ARMS` and `DIAG`, its defaults unchanged;
  - `analysis/exp69.py`, dry-run on synthetic results; it caught a column collision and a refusal phrase that didn't match the guard's actual text.

## 2026-10-28 (late morning): EXP-67 done, `pg_rewind` against pgdelta; EXP-68 running

- **EXP-67:** all 3 predictions held, all 12 copies correct, on the `ha` set in us-west1; its VMs were deleted after the fetch.
  - **Hot rows updated 10 times on the new primary:** `pg_rewind` and its replay took 36.4 s. pgdelta took 90.2 s (0.404); its guard refused the diverged copy, naming the fork, so it hashed. A re-clone took 90.9 s.
  - **1% of rows scattered:** `pg_rewind` took 459.0 s, 84% of it replay (8,001 MB at 20.7 MB/s). pgdelta took 177.9 s (0.387), and a re-clone 91.1 s, which beat both.
- **Two things the registration didn't model:**
  - `pg_rewind` copied 353 and 4,077 MB beyond S's WAL since the fork, unexplained, since its file list wasn't recorded.
  - Replay ran faster than EXP-65's for hot10 and slower for uniform.

  Bounds in the write-up show neither changes a verdict.
- **Two presentation changes after the data, scores unchanged:**
  - the figure's "MB of WAL on S" now reads "since the fork";
  - medians are kept to 3 decimals, so a label isn't rounded twice (177.8515 s had shown as 177.8).
- **What follows:**
  - **Idea 1 is next for the failover case:** both sides' block sets, sent once, never replayed. Its inferred time for hot10 is about 5 s against `pg_rewind`'s 36.4 s.
  - **First, a local scale model:** it measures the blocks the old primary's WAL names, against `pg_rewind`'s `--debug` file map. Whatever inflated `pg_rewind`'s copy would inflate idea 1's too.
  - **The selector (idea 4)** gains `pg_rewind` as a third option.
- **EXP-68** launched on a fresh pair right after EXP-67's teardown.

## 2026-10-28 (morning): EXP-64b and EXP-66 done; EXP-67 running

- **EXP-64b, overlapped source reads:** the deciding prediction held, and controls replicated EXP-64 within 2.4%; all 10 runs were correct.
  - **8 reader threads:** 102.8 s against 167.4 s without threads (0.614), 184.4 s for hashing (0.557) and 91.0 s for a full copy (1.13).
  - **1 thread:** 148.7 s. Prediction 4 (overlap alone suffices) failed: parallel reads mattered.
  - **Prediction 3 failed:** the receiver still waited 41 s for data. It syncs each file in its main loop and stops receiving meanwhile.
- **EXP-66, how the disk counts I/O:** random 32 KiB writes ran at the same 4,194 a second as 8 KiB ones, so idea 2's premise holds. One 8 KiB read at a time completes 2,163 times a second.
- **Two errors in the registrations' arithmetic, found in the data and written up openly, scores unchanged:**
  - **The syncs were counted twice:** in fsync mode `main`, the receiver's rebuild timer includes the syncs. So EXP-64's "107 s of writing and syncing" was about 60 s, and EXP-64b's 112 s bound wasn't one.
  - **The run count was modelled:** independent pages give 443,128 runs; the real set had 249,193.
  - **EXP-66's control "held"** only because the two errors cancelled. Its registered branch for a failed control is followed instead: instrument the write path before prototyping idea 2.
  - **Both figures** now label the registered numbers as such, and show the measured ones beside them.
- **EXP-67** (`pg_rewind` for an old primary) launched on a fresh pair right after EXP-64b's teardown.
- **EXP-68 pre-registered** while EXP-67 runs. It tests the receiver's syncs on its worker thread (`--fsync worker`, built for EXP-10 and never measured beyond memory).
  - **The deciding prediction:** 8 threads with the worker take at most 0.85 of EXP-64b's 102.8 s. Prediction 2: they beat a full copy.
  - **Also:** 16 threads, the hot cell, and `/proc/diskstats` on both VMs per run.
  - **EXP-66's rule re-applied** on the real page set, dumped by the new `--plan-out`.
  - **Checked before registering:** the analysis was dry-run on synthetic results; a real-server test covers the exact arm (8 threads, the worker, a written plan); and EXP-66's refactored model reproduces its outputs byte for byte.

## 2026-10-28 (early): EXP-65 done, the fast path against archive replay; EXP-64b and EXP-66 running

- **EXP-65:** all 4 predictions held, on the `ha` set in us-west1; its VMs were deleted after the fetch.
  - **Hot rows updated 10 times:** the fast path took 3.1 s, against 19.8 s for replay with the archive already local (0.156) and 90.9 s for a re-clone.
  - **1% of rows scattered:** 166.2 s, against 256.2 s for replay (0.649) and 91.0 s for a re-clone. That's a replication of EXP-64's 164.8 s from a cleanly stopped standby.
  - **The mechanism:** replay pays per WAL record and, beyond memory, waits on data-file reads (31 MB/s of WAL against 89 MB/s); the fast path pays once per page.
- **A label fixed after the data:** the figure had shown the archive's size. It now shows the WAL replay applied, from each copy's `redo starts at`. Scores unchanged.
- **Next:** EXP-64b, then EXP-66, are running on a fresh pair (launched right after EXP-65's teardown, because of the region's N2D quota).
- **EXP-67 pre-registered,** the last competitor research loop 1 lists as never timed: `pg_rewind` for an old primary after a failover.
  - **The failover is real,** built at 18 GB beyond memory: a standby promoted onto a new timeline, and the old primary's own 10,000 rows past the fork.
  - **Against `pg_rewind`:** pgdelta, whose guard must refuse and hash, and a re-clone.
  - **What it decides:** whether idea 1 (the union of both sides' block sets) is worth building.

## 2026-10-27 (late night): overlapped source reads built; EXP-64b pre-registered

- **Asked by the owner:** build the overlapped source reads for the fast path, and register EXP-64b on EXP-64's scattered cell.
- **Built:** `--read-ahead THREADS` (pgdelta/sender.py, `_ReadAhead`). Reader threads read the fast path's chosen runs in sending order, up to 64 MiB ahead of the sending thread. The statistics record how long the sending thread waited on them.
  - **Tests:** order, window and closing (unit), and a real-server refresh with 4 threads that matches the source. The full suite passes (1,598 tests).
  - **One error path fixed before commit:** a failed file closed its handle while a later read of it could still be in flight. Handles now close only after the threads stop.
- **EXP-64b pre-registered** before any run, on the uniform cell: hashing, the fast path with 0, 1 and 8 reader threads, and a full copy.
  - **The deciding bar:** 0.80 of the unthreaded fast path. EXP-64's own timers give a bound of about 112 s (0.68).
  - **One arm per mechanism:** 1 and 8 threads tell overlap apart from parallel reads.
  - **The harness:** `lab_exp64.sh` gains `PATTERNS` and `ARMS`, with defaults that reproduce EXP-64's plan exactly.
- **Waiting on the region's quota:** us-west1 allows 16 N2D vCPUs and EXP-65's pair uses them, so EXP-64b runs after EXP-65's teardown.
- **EXP-66 pre-registered** (research loop 1's second item: the `fio` sweep for idea 2). It runs on EXP-64b's pair after EXP-64b's runs, so it needs no VMs of its own.
  - **The deciding question:** does the disk count a 32 KiB random write as one operation?
  - **The model:** written before the data, it turns the measured rates into a gap-filled write time for EXP-64's page set.
  - **The rule:** idea 2 is prototyped only if it cuts that time by at least 25%.

## 2026-10-27 (night): the fast path built (K11), checked live (EXP-63) and timed beyond memory (EXP-64); research loop 1

- **Built:** `--fast-path summaries` (pgdelta/fastpath.py), opt-in, behind guard v2. Unit tests and two real-server refreshes pass. Planning EXP-63 found that `--reuse-any-basis` could let another cluster reach the fast path; it now checks identity itself.
- **EXP-63 (local, 13 tricky copies):** every published copy matched the source. The fast path ran exactly where guard v2 passed, and refused the rest.
- **EXP-64 (the `ha` set in us-west1, after us-central1 and us-east1 had no capacity):**
  - local changes: 1.98 s, against 87.8 s by hashing and 90.7 s by a full copy;
  - scattered changes: 164.8 s, 1.8× a full copy, the gap traced by the runs' timers to unoverlapped source reads.

  The first pipeline's poll took "workload done" for the end; a corrected watcher fetched the results and deleted the VMs.
- **Research loop 1** (docs/research/2026-10-27-research-loop-1.md):
  - a competitor audit from primary sources;
  - the hash path's measured ceiling (about 85% of ideal), so hashing's HA tie is an algorithm limit;
  - lower bounds beyond memory;
  - mechanisms from papers, read critically.
- **Next, by evidence:** EXP-64b (the source read pipeline), an `fio` sweep for gap-filled writes, and the unmeasured head-to-heads against `pg_rewind` and archive catch-up.

## 2026-10-27 (night): the owner's research loop (rule 12, K13)

The owner, in their words, asked that this be remembered and applied recursively:

> Have we been doing anything novel? Or are you just rehashing the same stuff over and over and over again? The goal of this project was to either win by isolating some set of constraints, which we have, right? You mentioned that we do beat these other approaches when it comes to certain approaches, but I want you to make sure: what is the industry standard for that situation, and do we have competitors? Are we truly better than those competitors? And in the case where we are currently not beating the competition, we should be trying new things based on novel research and discussion, like figuring out where the bottleneck is and realizing: Is this a system's problem or an algorithm problem? Meaning, given our current algorithm, is it even feasibly possible, given the best systems optimizations in the world, to beat this other algorithm? If so, maybe it's a system's problem. If not, then it could even be an algorithm problem. Maybe the issue is this hashing approach. Maybe it's a page labeling approach we should try. Maybe some other algorithm.

> And even better, don't just look at novel research. Try to synthesize information and conduct our own research as well. Maybe two different ideas from two different papers, or the same ideas from a paper, can be reworked potentially better for our use case. Also, definitely try pulling directly from the paper as well if you find something promising.

> All decisions should, at the end of the day, be evidence-driven. You should not simply guess that something is the bottleneck. You should perform experiments to justify it. All decisions must be justified, as in a scientific research setting. All your decisions should be reviewed as if they're being reviewed by a paper committee reviewing your findings.

It is now rule 12 in DECISIONS.md (decision K13); since October 29 it lives with rules 13–17 in WORKFLOW.md. The first pass of the loop started the same night: a competitor audit from primary sources, a paper survey for mechanisms to combine, and a measured ceiling for the hash path.

## 2026-10-27 (evening): EXP-58, what pgdelta's own TLS costs

- **Why:** O6 requires the data stream encrypted. TLS was built earlier on October 27, but what it costs at DR rates was the goal's open half. The TLS session had held EXP-58 for the owner's yes; the standing autonomy rule covers experiments that serve the plan, so it was designed, pre-registered (d355f86) and run.
- **What ran:** one default pair. v1 with BBR and BLAKE3 in clear, against the same inside pgdelta's mutual TLS 1.3, using the session's certificates from the harness's TLS material. Scattered 1% changes at 2 and 5 Gbit/s and 60 ms, 2 repeats, with `pg_basebackup` for context. New harness arm `pgdelta_direct_inplace_bbr_b3_tls`; each run now records the TLS session it negotiated.
- **Outcome (12 runs, all 4 predictions held):**
  - time: 1.033 of clear at 2 Gbit/s, 1.148 at 5 Gbit/s;
  - bytes: 0.3–0.6% more;
  - CPU: about 0.6–0.7 s more on each end;
  - every TLS run used TLS 1.3 with AES-256-GCM, with a 67 ms handshake.
  
  O6 is met; the scorecard's TLS row is marked met. The pair was deleted.

## 2026-10-27 (evening): EXP-59 and -59b against pgBackRest from a day-old backup; EXP-60's BLAKE3, firm

- **Sign-in:** the earlier "logged in" lines in the Terminal were old scrollback from before the hostname changed; gcloud's own logs showed no login after October 26. A new `gcloud auth login` tab fixed it, and a background watcher started the pipeline within a minute.
- **EXP-59 (first pair, 36 runs, all passing; 3 of 3 predictions held):**
  - timed to a usable copy from a backup a day's churn old, pgdelta took 0.024–0.084 of pgBackRest's time;
  - pgBackRest's restore took 6–10 s, then 90–118 s of recovery fetching 166–205 WAL segments one per call;
  - after the run: the analysis double-counted pgBackRest's recovery bytes (fixed: 1,689 MB, not 2,963). The trial-level row check failed after the runs, because it counts only the trial's own changes and the day's churn was also there; that skipped the B0 check. Aged trials now skip the row check, with a test.
- **EXP-60 (same pair, 8 runs; 4 of 4 held):** BLAKE3 took 0.885 of BLAKE2b's time at 5 Gbit/s. The EXP-29-based estimate of each hashing phase held almost exactly. Within 0.05 of the bar, so rule 11's second session followed.
- **Second pair:**
  - **EXP-59b** (16 runs; 3 of 3 held) ran pgBackRest with asynchronous WAL fetching (`archive-async`, 4 processes; new variant `pgbackrest_zst_async`, destination-only). Recovery fell to 0.59–0.64 of the one-per-call time, still 67–78 s, and pgdelta took 0.053–0.122 of its time. The B0 check ran and passed.
  - **EXP-60's second session** gave 0.847, 0.872 pooled: firm.
- **Ledgers:**
  - RESULTS has two new rows (against a repository; the hash on fast links);
  - D01 and D15 are annotated, with no grade changed;
  - the deck has a new slide after the DR slide, with EXP-59b's chart;
  - pgdelta's README recommends the `blake3` package for production.

  Both pairs were deleted, with no VM or disk left.

## 2026-10-27 (late night): EXP-62, the fast path's guard blind spot, and guard v2

- **Why:** EXP-61's write-up named one way its guard could be fooled. It found an old copy's end partly from the WAL the copy still holds, so an old primary that lost its WAL past a failover split could pass. A safety argument for the owner's decision shouldn't rest on an untested hole.
- **What ran (pre-registered in 3ba32b5 before any run):**
  - a new scenario S2d: S2b's crashed old primary, with its WAL segments from the split's on deleted;
  - a guard v2 that takes B's end only from how B was stopped: a clean shutdown's checkpoint, a cleanly stopped standby's minimum recovery point, or a base backup's manifest. It refuses anything else.
  - EXP-61's 12 scenarios plus S2d, rebuilt. EXP-61's numbers reproduced.
- **Outcome (all 3 predictions held):** v1 let S2d through, with 1,444 real wrong pages (1,443 stale rows). v2 refused it and left no real wrong page in any of the 13. Its cost: the crash-consistent copy S3 is hashed instead of using the summaries.
- **Still the owner's decision** whether to build the fast path at all.

## 2026-10-27 (late night): EXP-61, a WAL-summary fast path on the tricky old copies

- **Why:** the owner asked whether pages could carry a label, so the refresh knows what changed without reading both copies. PostgreSQL 17's WAL summaries are such a label. Reading B is what holds pgdelta to "at best a tie" in HA beyond memory, so a safe label-driven path is the one route past it. gcloud stayed signed out, and this needs no VM.
- **What ran (pre-registered in 027e2fa before any run):**
  - EXP-03's scenario builder, with the source running `summarize_wal = on`;
  - a dump of every summary before the servers stop;
  - a scorer that compares every page of S with B and counts what the summary path would leave wrong, with and without a lineage guard: same cluster, version and checksums, B's own end of WAL on S's timeline history, and summaries covering the gap. 12 scenarios, about a minute in all.
- **Outcome:** with the guard, 0 real wrong pages in all 12, and 2–35% of hashing's reads in the 6 it let through. Without it, 1,276–8,046 real wrong pages in each of the other 6. Predictions 1–3 held. Prediction 4 failed in S4, which sent 1.72× the differing pages, all of them free-space-map pages sent whole: a safe over-send.
- **What it doesn't settle:** time (counted pages only), larger and more varied copies, and an old copy whose own WAL was lost, which the guard can't see past. Building the path is the owner's call.

## 2026-10-27 (late night): what pgdelta's fast-link floor is made of; EXP-60 pre-registered

- **Why:** the owner pointed out two answers that contradicted each other. One called pgdelta's roughly 4 s floor on fast links "hashing both copies"; a later one said hashing isn't what limits pgdelta, reading is. Neither had been measured as stated.
- **What the existing runs show** (`analysis/exp60.py --background-only`, from EXP-52's and EXP-53's own timers, 10 runs at 3 to 5 Gbit/s, 2.1 GB):
  - reading and hashing S took 1.47 s on the source, and B 1.31 s on the destination, at the same time;
  - writing the changed pages took 0.70 s, fsync 0.42 s and verification 0.69 s;
  - pgdelta's own total was 4.06 s.
- **Every one of those runs hashed with BLAKE2b,** as did EXP-48's. Their statistics logs say so. The VMs never installed the optional `blake3` package, so `--hash auto` fell back. From EXP-29's per-page cost, BLAKE2b is about 0.88 s of each hashing phase (inferred); BLAKE3 would be about 0.19 s.
- **So each answer was right in one regime:** at 2.1 GB on a fast link the hash function is likely the largest single part of the floor; beyond memory the disk read of B is (EXP-48).
- **EXP-60 (pre-registered):** v1 with BLAKE2b against BLAKE3 at 0.5 and 5 Gbit/s, on the pair EXP-59 creates, after EXP-59's runs. New harness arms `pgdelta_direct_inplace_bbr_b2` and `_b3` pin their hash, and each run now records the hash it used. It waits, with EXP-59, for the owner's `gcloud auth login`.
- **The scorecard's TLS row** was already current (the TLS session had updated it).

## 2026-10-27 (late night): TLS built (O6)

- **Why:** the goals scorecard showed TLS, the owner's own requirement since October 23 (O6), as the one unmet goal pgdelta could close itself. pgdelta sent its pages and the session's hash key in clear, and EXP-13's TLS measurement never ran.
- **What was built:** `--tls-cert`, `--tls-key` and `--tls-ca` on `send` and `receive`, opt-in, all three on both ends.
  - The connection runs inside mutual TLS 1.3, and each end accepts only a peer whose certificate the given private CA signed.
  - The statistics record the version, cipher, peer and handshake time.
  - A session without the options is unchanged on the wire.
- **Tests:** 8 new, with a throwaway CA made by `openssl`:
  - a session over TLS builds the same copy;
  - a relay sees S's marker bytes and file names without TLS and neither with it;
  - a peer from another CA is refused in both directions, as is a sender without TLS, with no output published;
  - the CLI refuses a partial set of options, and TLS with `--stdio`.

  The full suite (1,566 tests) passes.
- **Not yet measured:** what TLS costs at DR rates. EXP-58 is proposed (one VM pair, about 45 minutes) and waits for the owner.

## 2026-10-27 (night): the start checkpoint on a large dirty set

- **EXP-57 (one VM, pre-registered):** with about 4.9 GB of dirty buffers on a balanced persistent disk and a TPC-B load at 0.6 of capacity:
  - a fast start checkpoint (pgdelta's default) nearly stalled the load for 30 s, losing 27.5 s of work;
  - a spread one (`pg_basebackup`'s default) ran it about half short for 4.5 minutes, losing 51.8 s.

  The deciding prediction failed: I had expected spread to lose far less, not more.
  - **Why this one:** the owner hadn't answered, and this was the plan's C-ckpt row cut down to one VM and one question, with no redesign of O4's load.
  - **What changed:** the default stays fast, by the registered rule. The README and `--checkpoint`'s help give both modes' measured costs; FIRMNESS, RESULTS and DECISIONS are updated.
  - **Teardown:** the VM pair was torn down; the project holds no VM or disk.
- **For the owner:** O4's two numbers now disagree. Against `pg_basebackup`'s default, pgdelta's fast start loses less work (27.5 s against 51.8) but has a deeper worst second (0.94 against 0.45). Which should govern?

## 2026-10-27 (evening): sub-page deltas built, and slower as built

- **Why this one:** the owner hadn't chosen between the two large items after several asks, and the standing rules say not to stall. Sub-page deltas were opt-in, harmless to everyone else, and local until the last stage; the large-dirty-set test stays parked (it redesigns O4's load).
- **The build, `--subpage`:** B's 512-byte block digests travel after each LEAF_DIGESTS (BLOCK_DIGESTS), and pages go as a bitmap and their changed blocks (PAGE_DELTA, PAGE_DELTA_ZSTD).
  - It is accepted only where the receiver builds on B's bytes.
  - A patched page counts as written, so verification re-hashes it.
  - 8 new tests: in place, copy-patch, zstd on worker processes, the fallback, two refusals, and a corrupted block caught. The full suite and CI are green.
- **EXP-56 stage 1 (local) held:** every copy was byte-identical (13 of 13), and the bytes came as EXP-54/55 projected (37.0 MB plus 17.2 MB of digests against zstd's 212.8).
- **Stage 2 (one VM session) failed:** 1.16× v1 + zstd's time at 0.5 Gbit/s and 1.44× at 1, with 3.5× fewer bytes.
  - **Why:** the timers show the link wait gone (0.0 s), replaced by 2.2 s of block hashing on the sender's main thread and 0.6–0.9 s of extra waiting for the receiver's digests.
  - **Status:** `--subpage` stays opt-in and off. The README says so.
  - **The revision to test:** move the hashing onto the workers.
  - **Teardown:** the VMs were torn down; the project holds no VM or disk.

## 2026-10-27 (late afternoon): a checkpoint switch for live sources

- **What the large-dirty-set test needs (C-ckpt), found while sizing it.**
  - **The gap:** the slowdown harness's load (`lab_exp28.sh`, EXP-12's bank scripts) writes to `loaddb`, built by `pgbench -i -s 2`: 200,000 accounts, about 30 MB. So in EXP-12, -28, -30 and -39, pg_backup_start's fast checkpoint never had more than about 30 MB of the load's pages to write. Raising shared_buffers alone can't change that.
  - **A test of the burst needs:**
    - a load database of several GB (`pgbench -s 400` or more);
    - shared_buffers of 8 GB, which needs a restart;
    - max_wal_size raised, so time-based checkpoints set the pace;
    - a fixed dirtying period of load before each arm;
    - the backup checkpoint's buffers written, read from `log_checkpoints` as the measured dirty set;
    - arms `none`, `pg_basebackup -c fast`, and v1 with `--checkpoint fast` and `spread`.
  - **Its cost:** the load's capacity C has to be measured again on the larger database, so O4's numbers there aren't comparable with EXP-28's. The source's local SSD absorbs bursts better than a persistent disk would.
  - **Status:** that's a redesign of the O4 measurement, not a routine run, so it waits for the owner's go-ahead.

- **`--checkpoint fast|spread`** for `--source-kind live`, as `pg_basebackup -c`. The default stays `fast` (no behaviour change), and the statistics record the mode.
  - **Why:** pgdelta asks `pg_backup_start` for a fast checkpoint, which writes every dirty buffer at once, while `pg_basebackup` defaults to spread. Every slowdown test so far had only 128 MB of shared buffers, so that burst on a large dirty set is untested (C-ckpt).
  - **What the switch does:** it is that row's own interim step. Until the large-dirty-set slowdown block decides the default, an operator can choose `spread`.
  - **Tests:** 2 new live tests (`true` by default, `false` with spread, the same copy either way; an unknown mode refused) and a CLI refusal without a live source. Full suite green.

## 2026-10-27 (afternoon): sub-page deltas multiply with zstd

- **EXP-54 (local, pre-registered):** do sub-page deltas still pay now that zstd ships? Yes: 512-byte blocks plus zstd sent 43.7 MB against zstd alone's 210.3 MB (0.21), 13.6× fewer than whole pages, counting B's block digests. All three predictions held.
  - **Why now:** the owner hadn't yet picked among the large open items. This cheap check decides whether the largest of them, a protocol change, is worth building at all.
  - **Next:** building them waits for the owner's go-ahead.
- **EXP-55 (local, pre-registered):** the costs beyond bytes are small, and all three predictions held:
  - B's block digests can travel up front with the leaf digests, with no extra round trip; in all, 0.25 of zstd's bytes;
  - hashing the blocks takes 1.46 CPU-seconds;
  - block writes took 1.07× whole runs, every patched copy byte-identical to S.

  The design is set. Building it is the owner's call.

## 2026-10-27 (midday): zstd's break-even, about 4 Gbit/s

- **EXP-53 (three VM sessions, pre-registered):** with batched tasks, zstd took 0.95 of v1's time at 3 Gbit/s, 0.97 at 4 and 1.06 at 5 (60 ms). Its break-even is about 4 Gbit/s (interpolated, 3.9–4.35). All four predictions held.
  - **The 4 Gbit/s session:** the owner asked for 3 and 4 Gbit/s after I had registered and started 3 and 5. It was added by a dated revision, before any result was read.
  - **Within each session zstd's time is flat from 3 Gbit/s up,** while v1's keeps falling. VM pairs differ by up to 0.6 s, so every ratio is taken within a session.
  - **Teardown:** every pair was torn down; the project holds no VM or disk.
- **The advice is now zstd below about 4 Gbit/s, chiefly at 2 Gbit/s and slower.** The README, the CLI help, the deck and the walkthrough say so.

## 2026-10-27 (late morning): zstd pays at 2 Gbit/s

- **EXP-52 (two VM sessions, pre-registered):** with batched pool tasks, zstd took 0.86 of plain v1's time at 2 Gbit/s and 60 ms. That is 4.88 s against 5.68; one run per task had been even (1.05, 6.17 s).
  - **Why two sessions:** the first gave 0.855, within 20% of the 0.90 bar, so rule 11 called for a second on fresh VMs. It gave 0.865.
  - **The miss:** the receiver's writes still take 1.38× v1's, above that prediction's 1.3 bar.
  - **Teardown:** both pairs were torn down; the project holds no VM or disk.
- **Advice:** `--compress zstd` is now worth it on DR links up to 2 Gbit/s. The README, the deck (a new chart on the bandwidth slide) and the walkthrough (a 16th step) say so.

## 2026-10-27 (morning): what sets zstd's floor

- **EXP-49 (local, pre-registered):** zstd's 6.1 s floor comes from pgdelta's thread pools, not the codec. On its small runs of pages (2.5 on average) each pool task costs about 12–13 µs, so neither pool gains past 4 threads (1.5–1.7 GB/s), while 1 MiB frames scale 3.8×. Beside 4 decompressing threads, each of the receiver's writes took 6.4× as long. The deciding prediction held; the other two failed and say so: compression scales better than decompression, and zstd's own cost on small frames is small (0.87 of its 1 MiB rate).
  - **Why local:** EXP-47's own statistics pointed at the receiver (the sender blocked longer than the link needed, and the receiver's writes doubled), and the cloud sign-in had lapsed. A Mac with 15 cores separates pool costs from the VMs' CPU limits.
  - **Next:** batch consecutive runs per pool task, test locally, then re-measure at 2 Gbit/s once the owner signs in to gcloud again.
- **EXP-50 (local, pre-registered):** pool tasks of up to 1 MiB of pages cut zstd's extra time over plain v1 from 0.81 s to 0.36 s end to end (`pgdelta local`, scale 70), and the receiver's writes from 1.74× plain's to 1.20×.
  - **Prediction 3 failed on its own criterion.** I compared files digests across runs, but they are keyed per session and differ every time, even plain against plain.
  - **The registered rule was applied, not reinterpreted:** the default went back to one run per task, with batching behind a switch.
- **EXP-51,** the second session that rule 11 asks for (0.44 was within 20% of 0.5), held on all three predictions:
  - batching kept 0.45 of zstd's extra time;
  - all 10 copies were byte-identical to S in 1,278 files, and the negative control (B before) differed in 12.

  So batching is now the default. Next: a VM test at 2 Gbit/s of EXP-47's cells, where zstd was even with plain v1.
- **The deck and the walkthrough** now include EXP-48 (a 15th step).
- **EXP-48's VMs are deleted:** once the owner signed in to gcloud again, `teardown.sh` removed both VMs and their four disks. The project holds no VM or disk.

## 2026-10-27 (early): the timing model's disk term; the evidence walkthrough

- **EXP-48:** a two-term disk model for v1 beyond memory (read B, then the scattered writes at the disk's page-write rate), fixed from EXP-31's 12 GB session, predicted every cell at 18 GB (2.25× memory) within 4.4%. All three predictions held. Larger sizes now project from a disk's numbers instead of guesses.
  - **Practicalities:** no `n2-highcpu-8` was available in us-central1, so it ran on `n2d-highcpu-8` (same vCPUs and memory), recorded before the run.
  - gcloud's sign-in expired before the fetch, so the results came over SSH. Both VMs were then powered off over SSH, which stops their CPU and memory charges, and await the owner's sign-in for deletion.
- **A walkthrough page** tells the story as 14 evidence-driven decisions, on an animated decision tree and a changing system schematic; a draft deck exists too (both private artifacts).

## 2026-10-26 (night): zstd on both ends: break-even about 1.8 Gbit/s

- **The receiver now decompresses on a pool too,** writing in order; tested, CI green.
- **EXP-47** (uniform changes, 60 ms, BBR verified on all 8 v1 runs):
  - the receiver waited 0.08 s for decompressed runs, not 0.9–1.2;
  - zstd took 1.04 of v1's time at 2 Gbit/s and 0.75 at 1 Gbit/s;
  - its own time was about 6.1 s at both rates, a floor that plain v1 already beats at 2 Gbit/s. Interpolated, the break-even is about 1.8 Gbit/s.
- **The recommendation is now in numbers:** `--compress zstd` for DR links below about 1.8 Gbit/s (0.52 of v1's time at 0.5 Gbit/s, 0.75 at 1).
- **The near-line 2 Gbit/s cell got its second session (K10),** after I had first argued it couldn't change the verdict. It didn't: 1.05 with all 4 runs per arm, the floor again 6.1 s. VMs deleted after each fetch.
- **A slip, caught by CI:** EXP-47's commit pushed a help string whose apostrophe ended its quotes, so `pgdelta/cli.py` didn't import. My commit chain printed the failing test but didn't stop on it. It was fixed about ten minutes later (full suite and CI green), and commits now run only after the tests pass.

## 2026-10-26 (evening): zstd on a pool: even at 2 Gbit/s, 1.9× faster at 0.5 Gbit/s

- **The sender now compresses on a pool of `--jobs` threads** beside its sending thread, in order. Measured first on real hex-filler pages: 4 threads compress as fast as 4 processes, since zstd releases Python's lock while it compresses.
- **EXP-46**, two sessions on fresh VMs, BBR verified on all 40 v1 runs:
  - **at 2 Gbit/s** the pool removed EXP-45's penalty (the sender waited 0.3–0.5 s, not 4+), but scattered changes took 1.05 and 0.97 of v1's time. The deciding prediction failed in both sessions. The receiver's decompression, in its main loop, costs about what the smaller sending saves;
  - **at 0.5 Gbit/s** (exploratory, registered before the session) scattered changes took 0.52 of v1's time, 6.8 s against 13.1, against `pg_basebackup`'s 36.9.
  - So `--compress zstd` is recommended for DR links of about 1 Gbit/s and slower, opt-in.
- **Practicalities:**
  - us-central1-b was out of `n2-standard-8` with local SSDs, so both sessions ran in us-central1-a;
  - one background job had hung on a stray `python -` reading its input, and was stopped. VMs deleted after each fetch.

## 2026-10-26 (afternoon): zstd on the wire, built and measured; it doesn't pay as built

- **`--compress zstd` built:** negotiated like the hash, opt-in and tested with both zstd bindings. CI now installs `zstandard`, the binding the VMs use.
- **EXP-45** measured it at DR round trips (2 Gbit/s, 60 and 120 ms, hex-filler data):
  - it sent 2.78× fewer bytes for scattered changes, as EXP-37 predicted;
  - but it made v1 1.30–1.48× slower, because the sender compresses in the thread that sends, and one VM core managed only about 140 MB/s. A serial model fits the loss;
  - the deciding prediction failed, so the option stays off, and the next build moves compression onto the workers.
- **A slip, caught and set apart:** the first session ran with cubic, not BBR. The `tcp_bbr` module wasn't loaded, so allowing BBR failed. pgdelta's statistics showed it (`effective: cubic`), and the session was set apart before the counted rerun, with BBR verified on every v1 run.
- **A second slip:** zsh doesn't split a command held in a variable, so the counted run's first start never ran. It was rerun under bash. VMs deleted after the fetch.

## 2026-10-26 (midday): Omni's unlogged tables; Omni on Kubernetes (OMNI-2)

- **EXP-43 (OMNI-1c):** Omni's own `pg_basebackup` leaves out an unlogged table's `_mintxid` fork, and pgdelta, which didn't know the fork's name, had copied it.
  - Every copy still behaved: started, the unlogged table empty and writable, every index and heap clean before and after `VACUUM (FREEZE)`.
  - By the registered rule, pgdelta now treats `_mintxid` as a fork (`pgdelta/datadir.py`, tested). Rerun, its copies hold what Omni's backup holds and pass again.
  - us-central1 was short of small N2 VMs, so the set's idle second VM ran as an `e2-standard-2`.
- **EXP-44 (OMNI-2), on the owner's go-ahead:** Omni's Kubernetes operator (1.9.0, Omni 18.4) on a GKE cluster.
  - **No old copy survives an HA failover there:** deleting the primary's node got the standby promoted, and about 40 s later the operator deleted the old primary's four disks and seeded a new standby with `pg_basebackup`. Every instance replacement seen did the same. So pgdelta's HA role on Kubernetes needs the operator to keep the old disk; DR is unaffected.
  - **A sidecar works:** it runs on every instance as the database's user, reads PGDATA read-only, and its CPU limit is a cgroup quota. It reaches a new standby only by a restart after the seeding, which failed prediction 3 by 6 s.
  - **Under the operator, checksums and `wal_log_hints` are on,** unlike the container.
  - A fourth node was needed once the sidecar's memory was added. The cluster and its disks were deleted after the fetch.

## 2026-10-26 (late morning): v1 on a busy Omni; CI green again and kept up to date

- **EXP-42 (OMNI-1b):** does live mode's recovery repair Omni's own `_mintxid` fork?
  - **Yes:** Omni logs that fork with PostgreSQL's generic WAL records, with full-page images (16,631 references and 56 images in 10 s of pgbench).
  - 20 of 20 live refreshes of a busy Omni 17.10 gave correct copies: balances, every index, and every heap before and after `VACUUM (FREEZE)`. All 4 deliberately broken refreshes were caught.
  - The VM was deleted after the fetch.
- **CI (the "Experiment tests" workflow) had been red since October 25,** and I hadn't been checking it:
  - the hash refusal test patched the wrong name, so it passed only where `blake3` is installed (my Mac), not on GitHub's runner;
  - a `parcopy` test raced on which of two correct refusals fired first.

  Both are fixed and the workflow is green. From now on I check CI after code pushes.
- **CI kept up to date (owner's decision):**
  - Python 3.11, as on the Debian 12 VMs;
  - numpy and matplotlib installed, so the analysis and figure tests run there too (skipped tests fell from 239 to 63);
  - it runs only for changes the tests read: code, `infra/`, and the few documents and data files the tests check.

  No `blake3` there, on purpose: that is where `--hash auto`'s BLAKE2b fallback gets tested. The tests that start real PostgreSQL clusters still run on the Mac and in the VM experiments, not on GitHub.

## 2026-10-26 (morning): live mode's worker cap scales with the source; the first run on Omni; checksums don't change the bytes

- **EXP-39 (T10b's larger source):** on a 16-vCPU busy primary (three VMs, 28 vCPUs, 10 runs in 6 minutes), v1 with BLAKE3 at 4 workers met O4 and refreshed in 2.01 s, 20% faster than at 2 (2.52 s) and 2.1× faster than `pg_basebackup` (4.15 s). All three predictions held, so by the registered rule live mode's default `--jobs` is now at most the source's CPUs ÷ 4, from 1 to 4 (`pgdelta/cli.py`, tested).
  - The test was easy for every arm: the source had about 7 idle vCPUs at 0.6 of its capacity, and not even `pg_basebackup` cost the load measurable work.
  - 8 workers also met O4 but raised the load's p95 latency to 11 ms, so the cap stays at 4.
  - The VMs were deleted after the fetch.
- **EXP-40 (OMNI-1, lean), the first run on AlloyDB Omni,** after the owner accepted the Omni container's license terms for this research use:
  - v1 refreshed Omni 17.10 data directories correctly, with Omni's own binaries. From a stopped Omni B came out byte-identical to S; from a running Omni, Omni recovered B from its `backup_label`. Both started with S's data.
  - **A failed prediction, and a fix:** pgdelta didn't refuse an Omni 16.14 directory, although D08 limits it to PostgreSQL 17 and 18. Nothing in the code checked the major version. It now refuses an S, a live server or an in-place B of another major version (tested; rerun on the VM, Omni 16.14 was refused and 17.10 still refreshed).
  - Omni's container ships with data checksums and `wal_log_hints` off (no `pg_rewind` after a failover) and `summarize_wal` off. Its directory adds a `_mintxid` relation fork, 8 shared catalogs, 2 databases, an internal log and telemetry; pgdelta copies them as plain files.
- **EXP-41 (Checksums-Omni):** since Omni ships with data checksums off, EXP-38's workloads ran with checksums on and off, paired. The shares of pages sent matched within 0.16 points in all 6 cells, so byte results carry to Omni's container.
  - **A correction found on the way:** EXP-37 and EXP-38 had run with checksums off (PostgreSQL 17's `initdb` default) without saying so. FIRMNESS.md's "all with data checksums on" was wrong for them. Both documents now say it.

## 2026-10-26 (early morning): the XFS tie repeats; what pages leave on the table; how old the copy can be

- **EXP-36's second session:** the tie between native incremental (`--copy-file-range`) and v1 on XFS repeated on fresh VMs. Scattered changes were within 4%, and for local changes v1 was level or ahead by up to 16%. D02 now rests on two sessions.
- **EXP-37 (T4):**
  - after `VACUUM FULL` or `CLUSTER`, no strategy (cross-file page matching, content-defined chunking) found a reusable byte, so pgdelta's positional pages lose nothing and D18 closes;
  - for scattered updates, 512-byte blocks would send 5.0× fewer bytes and zstd 2.8× fewer (D17, D35: two levers, not built).
- **EXP-38 (T11b):** for evenly scattered OLTP, the old copy can be about 1.75 updated rows per page behind (58,000 transactions at 345 MB) before a full copy is as fast at DR; for local changes, any age measured.
- **[docs/RESULTS.md](RESULTS.md):** a new one-page summary of the evidence, with its figures; the README's status points to it.

## 2026-10-26 (after midnight): D02 changes; native incremental is not faster than v1 at DR

**EXP-36 (INC, lean)** compared native incremental (`pg_basebackup --incremental` with `pg_combinebackup`), v1 with BBR on the new defaults, and one `pg_basebackup` stream, at 60 and 120 ms. I predicted incremental would win everywhere; it didn't.
- **On ext4,** v1 was 14–74% faster in all six cells: incremental's combine writes a whole new 2.1 GB copy (about 4 s), while v1 patches in place.
- **Part B on XFS,** registered after part A: with `--copy-file-range` sharing blocks (a 1.5 s combine), the two tie (0.96–1.10, provisional).

D02 now reads: where history exists, pgdelta is as fast or faster at DR round trips without needing that history. Native incremental keeps the LAN case and fewer bytes for local changes.

## 2026-10-25 (night): new defaults, each from a registered result

- **`--hash auto` (the command line's default):** the sender offers BLAKE3 where it can use it, and the receiver picks the first it can use. Without the package nothing is offered, and the wire is as before. Tested, including a receiver without the package.
- **`--verify-scope written` on the pipelined verifier:** lightly written nodes are composed, heavily written ones re-read whole on the worker pool.
  - EXP-31's second session: the hot tie repeated, and the wait for verification fell to 0 s.
  - EXP-32's rerun: 1,000 more trials, no wrong output.
  - EXP-33, a live source with BLAKE3 and `written`: 1,000 trials, no wrong output, but its during-start control fell to 47 of 55, so no bound was claimed.
  - EXP-35's oracle on that configuration: the checks caught 39 of 39 wrong copies.
  - By EXP-35's registered rule, `--verify-scope written` is now the default in place (`--verify-scope auto`).
- **EXP-34 (T13c):** 16 hard resets of the destination mid-refresh left B blocked (10, refusing to start) or complete (6, starting), never wrong; every rerun completed it.
- **A slip, caught:** the hash negotiation landed in the working tree while two campaigns ran from it. They were restarted from a frozen checkout, the first starts set aside. Campaigns now always run from one.

## 2026-10-25 (evening): access to the Google Cloud project, checked

The owner's project `postgres-delta-backup` is reached through `gcloud`, signed in as the owner (the browser console goes through the university's sign-in, which only the owner completes). A read-only check:
- the project is ACTIVE, created 2026-09-19;
- no VM is left running;
- the quotas that bound the experiments:
  - **32 vCPUs across all regions** (`CPUS_ALL_REGIONS`; the owner chose to work within it, K2), and 200 per region;
  - 8 in-use external addresses;
  - 500 GB of local SSD and 4,096 GB of persistent disk in `us-central1`;
  - 16 AMD (N2D) vCPUs.

Every VM set this session stayed within them and was deleted after its results were fetched. The owner's sign-in lapsed once and was renewed by the owner.

## 2026-10-25 (evening): BLAKE3 and a one-pass verification, each measured end to end

- **EXP-29 (T5's gate):** keyed BLAKE3 hashes a page 4.8 times as fast per core as BLAKE2b on the x86 VMs. `pgdelta --hash blake3` was built: the algorithm goes in HELLO, and BLAKE2b stays the default.
- **EXP-30:** on a busy primary, v1 with BLAKE3 at 2 workers refreshed 2.1 times faster than `pg_basebackup`, at no measurable cost to the load, with 64% less sender CPU. EXP-28's 4-worker miss didn't repeat; the live cap stays at 2.
- **A correction:** I had written that the destination's CPU decided EXP-22. Its timers show two disk passes over B did.
- **`pgdelta --verify-scope written` was built.** In place, each file is verified from B's digests and the pages the run wrote, without re-reading B.
- **EXP-32:** 1,000 randomized trials with the scope on, no wrong output, and 53 of 53 misplaced-write controls caught.
- **EXP-31:** at 12 GB on balanced persistent disk, v1 without its second pass ties the full copies for local changes (from 1.7×), but stays about 2.1× slower for scattered ones. HA beyond memory stays out as a speed target.

## 2026-10-25 (late afternoon): EXP-27 explained the missed controls; EXP-28 set live mode's workers

**EXP-27.** With an oracle, a correct read of the same backup compared page by page after recovery, the 3 missed `during_start` controls in 40 trials were each identical to the correct copy. The checks caught all 37 wrong copies and flagged no correct one; pg_amcheck alone caught the 14 whose rows were right. So EXP-25's misses were copies the WAL had fully repaired, not a gap in the checks.

**EXP-28 (T10b, lean).** Three VMs, the load's client on its own. The first smoke never ran (its log folder was missing on the new VM); the second showed that 32 clients over the network were bound by round trips, so the load moved to 128 clients, recorded before the grid. In 22 runs, v1 from a live source at 1 and 2 workers cost a busy primary no more than `pg_basebackup` at both speeds, and 2 workers were as fast as `pg_basebackup` unthrottled and 2.1× faster at 2 Gbit/s. 4 workers missed O4 unthrottled. Live mode's default `--jobs` is now at most 2, with a test; the VMs are deleted.

## 2026-10-25 (afternoon): EXP-25 finished on the second try; EXP-26 settled BBR's default

**EXP-25 (live-source correctness).** The first campaign run stopped making progress at trial 790. A negative control's deliberately broken copy had looped the checker's query for 3 h 22 min: a torn btree whose right links form a cycle, in a loop where PostgreSQL never checks for interrupts, so not even a cancel stops it. pg_amcheck named the index in under a second. Queries on a copy now have a 120 s limit, a hung query counts as a problem with the copy, pg_amcheck still runs, and the copy is stopped with SIGQUIT. The run's summary then showed that it had used a stale template, built before the review revision (16 MiB WAL segments, timeline 1), so only 4 of 712 refreshes spanned two segments. The harness now checks the template, and the campaign restarted from trial 0 on the registered one.
- **Result:** no wrong output in 1,000 counted trials, each spanning 8–19 WAL segments with a history file (below 0.69% per refresh at 99.9% confidence).
- **Controls:** read before start 55 of 55, read during start 50 of 55, against a bar of 90%.
- **Still to test:** whether the five misses were correct copies, which full-page writes would explain. *EXP-27 then tested it with an oracle: every miss was physically identical to a correct copy of the same backup, and the checks caught 37 of 37 wrong copies.*

**EXP-26 (BBR's fairness).** gcloud needed the owner to sign in again. The owner then asked why every experiment ran for hours, and the registered grid (60 runs of 100 s) was cut, before it ran, to 20 runs of 50 s with repeats only near the bar: 24 minutes in all.
- **Result:** BBR stays opt-in. A BBR newcomer took far more from cubic traffic than a cubic newcomer would in 8 of 10 cells (harm 0.32–0.95, bar 0.20), 81–96% of the link at a queue of one round trip or less.
- **Changes:** the CLI help and the ledger say when to use BBR. The VMs are deleted.

## 2026-10-25: review fixes for the live source and EXP-25

A code review of `build/t8-live` found six things, each checked with a failing test or a probe before it was fixed (branch `build/t8-live-fixed`). In pgdelta: a receiver that stopped answering or reading left the source's slot holding WAL indefinitely; the sender now gives up after `--live-timeout` (default 120 s), ends backup mode and drops the slot, with TCP keepalives on the connection, and `max_slot_wal_keep_size` is documented as the backstop. A WAL page that changed after hashing now fails the run. The broken test variant now warns and exits 4. In EXP-25, before the campaign: a second negative control that reads while a spread `pg_backup_start` runs (with `fast := true` the start checkpoint takes 3-10 ms, less than listing the files, so that order can't be tested here and rests on the code and a stub test); S's shared buffers at 8 MB so that control has dirty pages to miss; the kill rule's "blocked" only in pgdelta's own blocked state; 1 MiB WAL segments on timeline 2. The third smoke: 10 of 10 counted trials correct, 4 of 4 of each control caught, every refresh 8-16 segments. The campaign has not run.

## 2026-10-25: v1 reads a live source (T8's build), and EXP-25 pre-registered

pgdelta gained `--source-kind live` (branch `build/t8-live`): one psql session for the run, a temporary slot reserving WAL, `pg_backup_start` before the data directory is listed or read, `pg_backup_stop` after every listed file is sent, then the WAL from the start LSN's segment through the stop LSN's and the exact `backup_label`, appended to the inventory with a new APPEND frame (other sessions' bytes unchanged); in place, the commit waits for all of it. Pages that change between hashing and sending are hashed again as sent, and the copy is verified against what was sent. A failed or killed run leaves no slot and no backup session. A test switch reads the files before `pg_backup_start`, as the negative control. Tested against a psql stand-in and a real PostgreSQL 17 under a pgbench bank load, whose recovered copies keep the invariant between start and stop and pass `pg_amcheck`; 1,498 tests pass. [EXP-25](experiments/EXP-25-live-correctness.md), the randomized campaign for it, was pre-registered before any run; its smoke (10 counted trials, 5 negative controls) passed as predicted after two harness settings were revised (shared buffers, so the files change while read; the kill delay). The campaign itself has not run.

## 2026-10-25 (morning, the owner away): the lean round of experiments

With the owner away and routine steps pre-approved, ten short experiments ran back to back, each pre-registered, each written up with its figure and pushed: EXP-20 (DR's deciding cells: "local changes only"), EXP-21 (the cause: cubic's early-loss slow mode, not slow start after idle), EXP-23 (its rate: BBR 0 of 30, cubic 12 of 15 at 20 ms), EXP-24 (with BBR on pgdelta's socket, v1 holds at every DR round trip), EXP-22 (HA on persistent disks beyond memory: v1 1.8–2.3× both full copies) and EXP-15's lean session (a real 31 ms link confirms the DR result; cubic's slow mode absent there). pgdelta gained `--congestion-control`, the harness `--base-rtt-ms` and named VM sets in two regions. EXP-17's extension ran on the Mac alongside. The ledger's [What the lean tests settled](DECISIONS.md#what-the-lean-tests-settled-october-25-2026) has the summary. Two slips are recorded where they happened: a VM pair deleted before its failure was read (EXP-15's first attempt), and an analysis edit pushed with a scoping error, fixed before it was used.

## 2026-10-25 (night): EXP-19 held; the owner set standing autonomy

[EXP-19](experiments/EXP-19-two-part-model.md) fixed EXP-18's two-part model before running and tested it on new VMs at 3 busy processes: within 6.2%. Under disk contention v1 slowed 4–5% and one `pg_basebackup` stream up to 28%, so the fast-link comparison turns on which resource is busy on the destination: CPU (v1 loses its lead) or disk (it grows). VMs 03:35–03:56 UTC, deleted.

The owner said routine steps in the set direction need no permission, that resources are not constrained, and that any git activity is fine; main was pushed (76 commits, scanned first for addresses and emails).

## 2026-10-25 (night): EXP-18, the first lab test, on one VM pair for 23 minutes

[EXP-18](experiments/EXP-18-calibrated-contention.md) loaded the destination on purpose with 0, 2 and 4 busy processes and asked whether a hash benchmark taken before the runs predicts the slowdown. Its deciding prediction failed: scaling all of v1's fixed work by the benchmark overshoots by 13–20% (without it, the model undershoots by 20–35%). Exploratory: if about 60% of that work scales, a fit on one level predicts the next within 4%, a hypothesis to test next. And a finding for the positioning: with 2 busy processes on the destination, v1 lost its unthrottled lead over one `pg_basebackup` stream (4.04 against 3.79 s); at 2 Gbit/s it stayed far ahead. The VMs ran 03:08–03:31 UTC (about 0.40 USD) and are deleted. The console link redirects to the university's single sign-on, which needs the owner's sign-in; gcloud access, signed in by the owner earlier, works.

## 2026-10-25 (night): EXP-17, a randomized correctness campaign, locally

[EXP-17](experiments/EXP-17-randomized-correctness.md) ran 1,000 randomized in-place refreshes on the Mac, each with a random workload and one injected fault (a kill part-way and a rerun, corrupted, truncated, diverged or foreign bases), and checked every output independently of pgdelta: byte identity over S's files and S's logical digest after starting. No wrong output: below 0.69% per refresh at 99.9% confidence, within its scope (a stopped source, 50 MB, macOS). All 30 copies caught part-way refused to start.

The campaign stopped twice before that, both times on its own kill rule, not on a wrong copy: first a kill before pgdelta had touched B (B started as the old copy), then a kill after it had published (B started as the new copy). Each was reproduced with kills across the refresh, the rule was narrowed to the property pgdelta promises (start only as the old copy or the complete new one), recorded, and the campaign rerun from the start. Cost: no VMs; about 2 hours of laptop time.

## 2026-10-25: the plan made lean; a timing model checked on existing data (EXP-16)

The owner judged wave 1's designs far too costly (EXP-13 revised at about 8 h a session, EXP-14 at about 30 h and 260 USD) and clarified rule 11: no guesswork and confidence that matches the evidence, not replicating everything; and the token budget is limited, so no more multi-agent review rounds by default. The check of the wave 1 designs was stopped part-way. EXP-13's rule 11 revision, EXP-14 and EXP-15 are on hold, not run, pending the owner's choice of a lean approach: a single-VM lab that varies disk, CPU, memory and network on purpose, a timing model with a per-session calibration benchmark, and randomized fault tests for correctness.

[EXP-16](experiments/EXP-16-timing-model-check.md) checked the model idea first, with no VMs: fitted on the October 23 sessions, it predicts the October 24 pair within 2% wherever the link sets the pace; where CPU sets it the second pair was 6–25% slower, and scaling v1's fixed work by its own CPU-seconds brings the error to a median 2.0%. Session drift was compute speed, which a short benchmark can measure.

## 2026-10-24 (night, later): VM sets, so several tests can run at once

**Why:** the firming plan runs tests side by side within the 32-vCPU quota ([FIRMNESS.md](FIRMNESS.md#43-what-runs-at-once): T12r beside T9-PD, T13c beside OMNI-1), and T10b and T8 put the load's client on a third VM. The infrastructure made one pair from one configuration, and `create.sh` deleted the shared `known_hosts` whenever it made VMs (the audit's C-capacity item).

**What was built** ([infra/gcp/README.md](../infra/gcp/README.md#vm-sets)):
- `PAIR=NAME` names a set of VMs: `pgd-NAME-ROLE`, with its own `known_hosts` and SSH config, and its settings in `infra/gcp/sets/NAME.sh` if that exists, the environment over them. With no set named, every script and `remote.py` act on `pgd-source` and `pgd-dest` as before.
- Roles beyond source and dest, each with its own zone, machine type and data disk if wanted. The client role defaults to an n2-standard-4 with a 50 GB balanced persistent disk, in the set's zone.
- Two kinds of data disk at `/data`, both ext4 with the same options: local NVMe (RAID 0 when more than one) as before, or one balanced persistent disk of a given size, deleted with its VM.
- `create.sh` refuses, before making anything, a VM of another set, one in another zone than asked, and a set the quota can't hold. `teardown.sh` deletes only its set and its disks, in every zone, and exits 1 naming anything of the set that is left. `DRY_RUN=1` prints every gcloud command and runs none.
- `remote.py --pair NAME` (or `PAIR`) acts on a set, each VM described in its own zone; `fetch` keeps a set's runs under `results/exp04/sets/NAME/`. `agent.py env` records each VM's set, role and disk kind from its metadata.
- `tests/test_infra_sets.py`: 33 tests, none calling the cloud; a fake gcloud keeps a project's VMs and disks in a file.

**Not done:** nothing ran on GCP. The first real `create.sh` of a named set, of a balanced persistent disk and of a client is their smoke test. The harness for T10b's load on the client is T10b's work.

## 2026-10-24 (night): a rule 11 audit: what is firm, and how to firm the rest

**Why:** the owner asked whether anything else would leave decisions unfirm, and added rule 11 to the ledger: only a firm decision picks a single route; anything less keeps its routes open as branches, with an experiment queued to firm it.

**What was done:** five audits read the ledger, EXP-01 to EXP-13, this log, the spec and the code, each from one side (owner rows and scope, performance rows, measurement rows and the queue, self-set constraints, EXP-13's design). Their items were merged and deduplicated, and verifiers sent 27 corrections and 10 missing items. Each was checked against the files before it was applied; 24 held as sent, and 3 were applied in part (D85, EXP-13's trial order, and whether D48 needs a test), with the reasons in [FIRMNESS.md](FIRMNESS.md). Nothing was run.

**What it found** ([FIRMNESS.md](FIRMNESS.md)):
- **Of 107 items that steer work or close a route, 11 are firm.** Of the 55 that steer the next work, the only firm one is the 32-vCPU quota. The firm ones: D09 and D11 (for v0 staged and v1 on a stopped source), D10p, D40p, D17, D18, D54, D86b, the quota, T11b after T8, and T8's stop-on-wrong-output gate.
- **17 are provisional because the lifted two-VM limit or a VM-hour budget shaped them:** EXP-12's deciding rows for v1 and what rests on them (D50, the worker cap, D81, D68's move, the HA caveat, T5's and T10b's moves ahead of T8), and EXP-13's design ("At most two exist at any time", one session, the cut line).
- **The rest are provisional mainly because** nothing has run on AlloyDB Omni; every VM result comes from one zone, local NVMe, 2.1 GB and an idle or stopped source; v1 reading a live source has no evidence; three grades don't hold for v1 or Omni (D02 and D85 were earned against v0, D14 rests on an argument); and the queue's order is judgment.

**What it proposes,** for the owner, none of it applied: twelve owner decisions (K1–K12: VM-hour budgets, a quota raise, one session outside `us-central1`, Kubernetes for an Omni probe, D07's scope, rule 3's yardstick for HA, O4's materiality, T13's fault classes, the code freeze, how to read a single session, D13's key, EXP-13's cost); eleven tests, seven of them extending queued T-items (EXP-13 revised and run on two different days beside a persistent-disk, two-zone HA re-test; T13's crash arm on Linux; T10b with T5 inside it; an extended T8; T4) and four new (a real cross-region check, two Omni probes, a v1-against-incremental session pair), about 107 instance-hours before T8's sessions; a queue order, itself provisional; fifteen dated revisions to EXP-13 before it runs (one optional), taking it from one session of about 3.8 h of the pair to two of about 6 h; and grade changes D02 A to C, D85 A to B and D14 A to B. No pre-registration, registered verdict or grade was changed. A figure ([firmness.svg](figures/firmness.svg)) counts firm and provisional items by ledger area, from [firmness.csv](experiments/data/firmness.csv), and `tests/test_firmness.py` checks that the CSV and the page agree.

## 2026-10-24 (evening, later): EXP-12 rechecked; corrections applied

An independent recheck read EXP-12's write-up and its summaries (the ledger, the README, the experiments index and this log) against the raw data, from four sides: numbers, verdicts, summaries and departures from the registered text. 46 findings, 11 major. Each was checked against the run records, the pgbench logs and the sampler's records, and all held, some in part: 35 distinct corrections, listed in [EXP-12](experiments/EXP-12-source-slowdown.md#corrections-after-the-recheck-october-24-2026). No registered verdict changes, no code changed and nothing was rerun, so the curated CSVs and figures are unchanged; the pre-registered and dated pre-run sections are untouched. The main ones:
- **The warm block didn't show a larger miss:** warm `v1proxy`'s net values were within δ of its cold ones (E15 held). Row 7a, hashing, stands because warm `v1proxy` also missed against warm `full`, not because the loss grew.
- **`pg_stat_wal` shows the WAL's syncs slowing during the stall:** in every staged run, and in no other arm, fewer syncs completed during the capture's flush and each took longer on average (0.33–0.55 ms over the three 1-s reads spanning the flush, against 0.15–0.16 in the settles), while total sync time per second over each window stayed at its usual rate. That the load's commits waited on them is the likely reading, not shown in every run; why they slowed stays a hypothesis.
- **The fragile flag goes with the moderate meet and the warm reading too,** whose cells are flagged *client wait* in every run, as the HA caveat and R1 were already marked; and both registered decisions, T5's move and the worker cap, are fragile, not only the proxy's result.
- **The client's wait is read against its controls:** over a window `v1proxy`'s threads waited 0.84–1.02 s, against 0.27–0.74 s for `full` (0.27–0.35 at the same window length) and 0.56–0.69 for one worker, so the total doesn't by itself measure the client's share, since `full` lost nothing measurable; the worst second's wait (0.30–0.38 s against at most 0.10) was 4.5–19 times that second's lost work.
- **One worker's time against a full copy now carries its caveats:** the proxy sent 29.5 MB where v1 itself sent 449 MB here, and the bar is `pg_basebackup`, not the fastest copy measured, whose slowdown waits for T8, a caveat the pre-registration asked for with every result.
- **A6:** the cores reading is a hypothesis for T10b, and one worker lost about 1/60 as much per CPU-second as four (1/58–1/64), not 1/60 to 1/70.
- **In the ledger:** D81 moves from D to C by "contradicted in part", since decision 2 gives it a basis and A6 was judged; D58's note separates backup mode alone, which cost nothing measurable, from the proxy at 4 workers; T12's cost is now about 3.8 VM hours (3.5–4.3), an overrun that awaits the owner.

**The corrections were rechecked in turn,** from three sides (numbers, verdicts, consistency): 22 findings, 10 major, all checked against the same raw data, all held and applied, listed in EXP-12 beside the first recheck's. No registered verdict changes. The main ones: `pg_stat_wal`'s per-second counts lag, so syncs of "up to about a second" can't be read from them and the registered test for blaming the stall on WAL syncs is met only in part; the client's window wait was about 3 times as large for the proxy as for `full` at the same window length, not equal; one worker's time at 2 Gbit/s became about 8.7–10 s by inference, 0.73–0.84 of the full copy, with the session's end sizes, not 8.7; the warm differences are within δ, no change beyond δ shown either way; `full`'s p99 latency at every speed, with its spike as each copy starts, is now stated; and D50's reasons against X again include the moderate meet. A third recheck of those corrections (12 findings, 3 major, all held and applied, no registered verdict changes) widened one worker's time at 2 Gbit/s to about 8.7–11 s (0.73–0.92 of the full copy) by inference, since it adds a difference measured idle to a time under load, still with the session's end sizes, which the runs' own times would lower; gave the load's near-zero time as 1.1–1.3 s of each flush in all; and dropped the claim that the client's window wait grows with a copy's CPU.

## 2026-10-24 (evening): the two-VM limit lifted; a plan to firm T10a's results

The owner asked whether the plan would make EXP-12's fragile conclusions firm, then lifted the limit of two VMs: the project had set it itself, to bound cost and the reach of a mistake, and the GCP project is funded by Google. That limit is why EXP-12's load client ran on the source, which made its deciding rows fragile. The binding limit is now the project's quota, 32 vCPUs across all regions. The ledger's [Firming these results](DECISIONS.md#what-t10a-settled-october-24-2026) records the plan: T10b runs the load's client on a third VM, with the load levels interleaved and EXP-12's pgbench load beside HammerDB; T8 measures v1 itself on a live source the same way; and both run in two sessions on fresh VMs. The other guardrails stay: VMs labelled `purpose=pg-delta-benchmark` in `us-central1`, deleted after their results are fetched, and no change to IAM, firewall, billing or org policies.

## 2026-10-24 (evening): EXP-13's harness built and reviewed, then merged

T12's harness was built on local build branches, as EXP-11's and EXP-12's were, and reviewed: 12 findings, 3 major, all held, recorded in EXP-13's "Analysis readings". One was deferred, not rejected: the parsers' tests use hand-written `tc`, `ss`, `nstat` and stunnel output, and swapping in output captured on the Debian 12 VMs moves into EXP-13's smoke run (a dated rule 8 change there). After EXP-12's write-up, `build/exp13-fixed` merged into main cleanly (721e045); the whole suite passed there (1,407 tests, 7 skipped). Nothing ran on a VM. Its cost is about 4.3 days of harness work and about 3.8 hours of the pair (3.5–4.3), over the ledger's 2 VM hours; the owner hasn't accepted the overrun yet.

## 2026-10-24 (16:51–20:10 UTC): EXP-12 ran; T10a done; VMs deleted

**Ran** on two new `n2-standard-8` VMs in `us-central1-a`, created at 16:51 UTC and deleted at 20:10 UTC:
- **The smoke run,** 16:57–18:18: five invocations, 38 runs. Its sixteen items were recorded in [EXP-12](experiments/EXP-12-source-slowdown.md), dated, before the sweep. Two settings changed before the sweep: chrony was installed on both VMs (item 4's registered remedy, `chronyc makestep`, wasn't available on Debian's image, which runs `systemd-timesyncd`), and, as registered, the sampler's reads went to 0.5 s instead of 0.1. One registered action wasn't taken: loaddb grew, and its fillfactor wasn't changed, under rule 8.
- **The registered sweep,** `exp12-hot`, 18:23–20:08: one trial, 117 runs (103 under the load, 14 on the idle source), with the smoke run's values in the run line (busy load 18,100 transactions per second, moderate 9,000). This trial's capacity was 10.9% below the smoke run's (A12), so the busy load ran at 0.67 of it rather than 0.6.
- The results were fetched and checked equal to the VMs' copies (file counts and bytes, all six result sets) before the VMs were deleted. Nothing was rerun.

**Result** (the registered analysis, run as registered):
- **Every output was correct under load (G4).** 41 outputs of `pg_basebackup` and staged pgdelta taken under the load, and 4 from the idle source, passed `pg_verifybackup`, recovery, the load's balance invariant and consistent cut, coverage, row counts, schema and `pg_amcheck`. The load's sums held: Σabalance = Σn = 88,291,983 logged commits.
- **One `pg_basebackup` stream cost the busy load nothing measurable on O4's two numbers** at 0.5 or 2 Gbit/s or unthrottled. It is O4's bar; rule 3's fastest copy, EXP-11's bound, reads a stopped source and has no slowdown number until T8.
- **Staged pgdelta missed the owner's bar (O4) at every speed:** 1.25–1.35 s of the load's work, and a near-total stall of one second during its capture's flush, when the WAL's syncs slowed. The registered rule R1 fires, so T5 moves ahead of T8. Most of the loss is the capture, which v1 removes.
- **v1's source side, by proxy, missed the bar at 4 workers and met it at 1** (0.05–0.07 s of work and a p95 of 69–132 ms during the copy, against nothing measurable). So v1 needs a source worker cap on busy primaries, and T10b moves ahead of T8 as "source workers by load". At the moderate load, 4 workers met the bar. The HA caveat fires: v1 "slows a busy primary more than `pg_basebackup` does", by proxy at 4 source workers (fragile: its cell is flagged *client wait*).
- **Fragile:** the load's client ran on the source, and the deciding cells where W was measurable are flagged *client wait*, as are the moderate 2 Gbit/s `full` and `v1proxy` cells the Load row rests on and the warm `v1proxy` cell; leaving flagged runs out leaves nothing to judge. The rows stand, as registered, and so both registered decisions are fragile.
- **A6 reads "CPU-seconds is an adequate unit at this load".** By the rule's test its two comparisons split: one source worker's loss per CPU-second was about 1/60 of four's (1/58–1/64), under half, but 4 busy processes (`burn4`) lost 2.4–3.2 times as much per CPU-second as the proxy, above it, so the rule gives "CPU-seconds". Neither shows loss in proportion to CPU-seconds; both are consistent with loss following how many cores run at once, which is a hypothesis for T10b.

**Drift and the owner's decision.** loaddb grew to 11.6 times its size (`load_progress` from 32 to 44,775 pages), against smoke item 14's 5%, as it had in the smoke run. Before the fetch, the owner decided in chat: "Don't rerun now. Apply the committed drift check after the fetch, and propose a rerun with a load_progress fix only if it shows drift." The drift check, defined at 19:46 UTC during the sweep (8488bbc), its script and tests committed at 20:07 UTC (59a9f80), both before the sweep's logs were fetched, found none: the busy load's settles' median p50 went from 0.667 to 0.650 ms and p99 from 1.327 to 1.222; the moderate load's from 0.581 to 0.573 and 0.861 to 0.845. So no rerun is proposed. The growth did make the full copies larger (up to 0.3 GB over the busy block), which is why A10 marks the session shifted against EXP-11 on `full`'s idle time; per MB, `full` took about the same time as in EXP-11.

**In the ledger** ([What T10a settled](DECISIONS.md#what-t10a-settled-october-24-2026)): D50 (4 workers) moves from B to C, D68 (an idle source) from D to C and, after the recheck, D81 (the 1.3× CPU bound) from D to C, each by "contradicted in part"; O4, D03, D04, D10p and D58 get dated notes. The queue is now T12, T5, T10b, T8, by T10a's registered consequences (the moves of T5 and T10b are fragile).

**Cost:** about 3.3 hours of the pair, about 3.3 USD, against the budget of about 3.4 hours. The VMs are deleted. The write-up was rechecked independently afterwards (the entry above).

## 2026-10-24 (afternoon, later): EXP-12's harness built and reviewed

**Built** on local branches, as EXP-11's was: one agent for the load, the arms, the proxy for v1's source side and the correctness checks; one for `analysis/exp12.py`; then a merge with every test run. New: the fixed-rate load and its counters, loaddb, the source-side sampler and clock probe, the eviction that makes reads cold, the snapshot the proxy reads, and the seven negative controls of the correctness check, each failing through its own check on a local PostgreSQL 17.

**Reviewed** by three adversarial reviewers (fidelity to the pre-registration, measurement validity, robustness on the VMs): 17 findings, 3 major, all fixed, each with a regression test checked to fail without its fix. The major ones: a copy its own tool aborted wasn't counted as wrong; a run's 15-s view could reach into the next run; and stopping the proxy's backup-mode session raised an error on Python 3.11 and later.

**Tests:** 1,217, all passing (6 skipped), up from 1,072. The readings the code makes where the text leaves a choice are in EXP-12's "Analysis readings", dated, before any run.

**Not testable on the Mac,** so first exercised by the smoke run: Linux's page-cache eviction and `fincore`, the source-side sampler, and the load's logs on the VMs.

## 2026-10-24 (afternoon): T10a and T12 pre-registered as EXP-12 and EXP-13

**Why these two:** they head the queue re-ranked after T2. T10a measures the owner's bar (O4: a refresh slows a live source no more than `pg_basebackup` does) and checks every output under load. T12 measures DR's links, now v1's first target: what round-trip time, TCP buffers and TLS do to v1's path and to the full copies.

**How they were designed,** as EXP-11 was, before any code for them exists: for each test, two independent drafts (one for realism and decision value, one for measurement validity), merged into one; an adversarial review; a revision; an independent check of the revision; and a final pass. Where a fix offered options, the option that doesn't favour v1 was taken. The reviews and checks found 16 problems in EXP-12 (5 major) and then 9 more (2 major), and 12 in EXP-13 (4 major) and then 13 more (none major). All are answered in each write-up's "Revisions before running". Two examples of what they caught:
- **EXP-12:** a plain-text `pg_dump` has carried a random restrict key since PostgreSQL 17.6, so a schema check by hash would have failed every good copy.
- **EXP-13:** netem counts its child shaper's drops as its own, so a "zero drops" check would have discarded every run with raised TCP buffers.

**Expected results, written down before running:**
- **EXP-12:** the registered rule fires and moves T5 ahead of T8. The proxy for v1's source side most likely misses the bar on the worst drop if the source is busy above 50%, so a cap on source workers would follow (T10b moves up).
- **EXP-13:** with Debian's default TCP buffers, one pgdelta stream is window-limited at 60 ms, so v1 loses scattered changes there; with raised buffers it wins. One TCP stream (D36) would then be contradicted for DR, unless Omni's nodes raise their buffers: a choice for the owner.

**Cost if run:** EXP-12 about 3.45 days of harness work and about 3.4 hours of the VM pair; EXP-13 about 4.3 days and about 3.8 hours (3.5–4.3), over its 3.5-hour cut line and the ledger's 2 VM hours, an overrun the owner hasn't accepted yet. Nothing is built or run.

## 2026-10-24 (later): EXP-11 rechecked; corrections applied

An independent recheck read EXP-11's write-up and its summaries (the ledger, the README, the experiments index and this log) against the raw data: 33 findings, 12 major. Each was checked against the data, and all held, some in part: 25 distinct corrections, listed in [EXP-11](experiments/EXP-11-full-copy-baseline.md#corrections-after-the-recheck-october-24-2026). No registered verdict changes, and the pre-registered sections are untouched. The main ones:
- **The analysis's audit table counted example paths** (33 for `full_nosync`, 3 for `pg_probackup`'s own flush) instead of the audit's counts (992 of 992 files and 1,021 of 1,021 directory changes; 3 files and 2 directory changes). Fixed in `durability_audit.arm_results` and the analysis, with a test (commit d5a2fe6). The registered analysis was rerun as before: only its audit table changed, and the curated CSVs and figures are byte-identical.
- **`pg_probackup`'s slowness** was given a cause the timers don't support; its copy phase alone outlasts one stream's whole stream, for reasons not timed (SSH or its protocol).
- **T5 and T13:** the registered 4-job cut is now stated with the 8-job reading (in the band where they rank equal) and with the session shift it rests on; the ledger no longer says they can't change the HA verdict, and their place in the queue depends on the worker count (D50, T10b).
- **The released-copier claims** now name Debian's default write-back on dest, since one stream with write-back at 64 MiB beat v1; **the crossovers** name their 0.1 ms round-trip time; **D03** stays at B instead of moving to A.

## 2026-10-24 (06:27–09:34 UTC): EXP-11 ran; T2 done; VMs deleted

**Ran** on two new `n2-standard-8` VMs in `us-central1-a`, created at 06:27 UTC and deleted at 09:34 UTC:

| Invocation | UTC | Runs | For |
|---|---|---:|---|
| smoke: `exp11-smoke`, `exp11-smoke70`, `exp11-audit`, `exp11-audit2` | 06:32–07:03 | 89 | EXP-11's twelve smoke items, recorded in its write-up before the sweep |
| `exp11-local` | 07:04–08:53 | 348 | hot, then append: the main block, the extension and the warm-source block |
| `exp11-uniform` | 08:53–09:32 | 120 | uniform: the main block |

Every run of the sweep was verified, and every durable run passed the durability checks. The two invocations took 108 and 39 minutes, against 108 and 38 projected.

**The smoke run's two fixes,** both made before the sweep and recorded in EXP-11:
- **A WAL ticker for `pg_probackup`** (commit 288e26b). On the idle source, `pg_probackup catchup`'s WAL stream noticed the stop point only at its next timer tick, so each catchup idled 2–8 s after `pg_backup_stop`. During `probackup*` runs only, the source now gets one empty WAL message every 0.1 s, as a live primary's WAL would arrive. It changes no table or data file.
- **The syscall audit** stopped on real `strace` output: a `close()` of a descriptor inherited from the parent raised `KeyError`. Fixed with a regression test from the real trace line (commit 43da18c); the rerun audited every arm.

Also from the smoke run: the idle noise of dirty memory was 0.44 MiB, so check (a)'s limit stayed at 4 MiB; and the projection put the session at the 3.5-hour line, so cut 1 applied (`pg_probackup`'s byte diagnostic only in each trial's first repeat).

**Result** (details in [EXP-11](experiments/EXP-11-full-copy-baseline.md), summary in [What T2 settled](DECISIONS.md#what-t2-settled-october-24-2026)). It holds on one VM pair with local NVMe, for 2.1 GB in RAM, on an idle source, at 0.1 ms round-trip time, with v1 and the bound reading a stopped source:
- **The fastest durable full copy** was the parallel copier built as a bound: 1.88 s unthrottled. One `pg_basebackup` stream took 3.8–4.1 s (fsync or syncfs alike), and `pg_probackup catchup -j 4/8` over SSH, the only released parallel tool found, 5.8–6.1 s.
- **EXP-09's HA verdict reverses: out on fast links.** v1's path (stopped source, 4 jobs) takes 1.64–1.65 times as long as the bound for local changes, with no robustness flag. With dest at Debian's default write-back, it is faster than the released copiers tested (0.77–0.78 of one stream with syncfs, 0.79–0.81 of one with fsync, against EXP-09's 0.86). The cut it would need with v1 at 4 jobs, 1.40–1.42 s, is more than T5 and T13 can give together by the registered rule (at 8 jobs it is 1.05–1.07 s, where the rule ranks them equal), so the queue is re-ranked: T12 (DR's links) up to second, T5 and T13 down while the default stays at 4 jobs.
- **Crossovers, at 0.1 ms round-trip time:** v1's path beats the bound below 6.5 Gbit/s for local changes and 5.0 for scattered ones, and, with dest at Debian's default write-back, every released copier at every speed measured. DR's latency is T12's.
- **The session is shifted against EXP-09:** v1 took 14–18% longer for local changes (8–17% for uniform) and one `pg_basebackup` copy 22–30% longer unthrottled (its single TCP stream measured 4–11% slower), so every ratio is within the session.

**Found while writing it up,** and stated in EXP-11 beside the analysis's verdicts, which are unchanged:
- **The start and end reference digests differed in every trial,** in `template1`'s and `postgres`'s statistics catalogs and the commit log (most likely autovacuum; not confirmed), smoke trials included. Smoke item 12 had recorded them as matching. So a `pg_probackup` logical difference would have counted as void, not failed; none occurred.
- **The time until usable can't show where `--no-sync`'s flush goes:** the harness's verification maps every output file with `FIEMAP_FLAG_SYNC`, which writes it back, before starting it.
- **The analysis's E13 counts the negative control:** its "every arm passes the audit" includes `full_nosync`, which must fail. Every durable arm passed.

**Cost:** about 3.1 hours of the pair, at about 1 USD per hour for the two VMs together, so about 3 USD. Both VMs were deleted at 09:34 UTC.

## 2026-10-24 (night): EXP-11's harness built and reviewed

**Built** on local branches, each part by one agent in its own worktree: the core (the method tables, the `pg_basebackup` variants with timestamped phases, the durability checks (a) and (b), the probes, the trial's order with the extension and warm-source blocks, the write-back sysctl and its guard), the parallel copier `parcopy`, the `pg_probackup` arms with the syscall audit, and `analysis/exp11.py`. Then merged, with every test run.

**Reviewed** by three adversarial reviewers (fidelity to the pre-registration, measurement fairness, robustness on the real VMs): 16 findings, 4 major, all fixed with regression tests. The major ones:
- a copy the tool itself aborted was recorded as an error and escaped A4, so a failing arm could still have set a baseline;
- the "near the line" flag missed ratios that decide only together;
- the `pg_probackup` arms depended on a path that exists only when it is built from source;
- check (a) would have ignored the smoke run's idle noise.

**Tests:** 1,067, all passing (6 skipped: Linux-only or binaries missing), up from 864.

**Recorded in EXP-11 before running:** how the harness and the analysis read every rule where the text left a choice (its section "How the harness and the analysis read the rules").

**Not testable on the Mac,** so first exercised by the smoke run: Linux's write-back and `sendfile`, `strace` on real output, and the exact log lines of `pg_basebackup --verbose` and `pg_probackup` 2.5.16, which the parsers were written against from their source code.

## 2026-10-24 (later): T2 pre-registered as EXP-11

**Why T2 next:** EXP-09's HA verdict (v1 at 0.86 of one `pg_basebackup` stream) rests on a single-stream baseline, and rule 3 asks for the fastest durable full copy. T2 measures it.

**How it was designed:** three independent drafts, each from one angle (deployable tools, measurement, risk and scope), merged into one, then reviewed for feasibility and for fairness before any code exists. The reviews found 6 major problems and 15 minor; a check of the revision found 8 more, 2 major. All are answered in [EXP-11](experiments/EXP-11-full-copy-baseline.md)'s "Revisions before running". The ones that change what it can conclude:
- **`pg_probackup catchup` never flushes its streamed WAL or any directory** (from its source code), so its arm ends with a timed `sync -f`, and a syscall audit checks every arm's flushes.
- **"Out" is judged against the fastest copy measured,** as EXP-09 registered, not against released tools only, which would have favoured v1.
- **The HA verdict stays on v1's registered 4-job arm;** v1 at 8 jobs is reported beside it, since the baselines get their best stream count.
- **The deciding unthrottled cells get 10 runs each.**
- **The zstd part moves to T4,** offline; it answers a bytes question on another dataset.

**Expected verdict, written down before running:** Out on fast links against the parallel copier built as a bound, with v1 near the 0.9 line against the released tools. Confirmed is out of reach by arithmetic unless the hardware floor is far higher than probed.

**Cost if run:** about 2.75 days of harness work, then about 3.35 hours of the VM pair. Nothing is built or run yet.

## 2026-10-24 (later): commit history rewritten so the commits link to Matt's GitHub account

**Why:** GitHub showed "Matthew Wei authored and Matthew Wei committed" on most commits, linked to no account. This repository had no `user.email` set, so git used a made-up address from the laptop's hostname, and GitHub can't link an address it can't verify.

**What changed:** the 65 commits with that address (or a second hostname address) now carry Matt's GitHub noreply address, as author and committer, with the committer date set to the author date. Nothing else changed: every commit's files are identical, and teammates' commits keep their own names and addresses. But a commit's ID covers its parents, so 86 of the 91 commits on `main` have new IDs, back to October 9. This repository's git identity is now set, so new commits link too. At Matt's request, `main` was force-pushed on October 24; GitHub now links every commit on it to an account.

**Where old IDs are quoted:** this log, EXP-08 and EXP-10 now cite the new IDs. Teammates' specs, VALIDATION.md's CI records, the benchmark provenance files and the runs' `run.json` keep the IDs they recorded, since those record what actually ran; [the hash map](archive/2026-10-24-commit-hash-map.csv) gives the new ID for each.

## 2026-10-24 (later): four more rechecks; EXP-10's verdict on (b) corrected

**Why:** the first rechecks ran before EXP-10's XFS arm and the write-back rerun existed, and before the ledger, README and index were updated. So a second independent recheck covered those, against the raw sweeps and the write-ups. Each round of corrections was then checked again, because a correction can itself be wrong, and twice one was.

**Second recheck:** 21 problems, 4 major, corrected in commit cf7a740:
- **(b) was judged against its registration.** I had compared the rerun's fsync thread with the rerun's baseline and called (b) held for local changes. The dated note allowed the rerun to replace only the write-back measurement. Judged that way, (b) failed in all three workloads (the better alternative saves 0.68, 0.99 and 0.996 s). The rule's outcome is unchanged: the fsync moves to a thread. For local changes the saving sits on the 1.0 s line in every way of counting it (0.99–1.04 s).
- **The summaries had dropped caveats** that the write-ups keep: pgdelta ran in staged mode in T11, so its edge over `pg_probackup` is not yet shown from a live server; v1's 2.7 s was on a stopped source, and T2's registered prediction would reverse it.
- **Two grades:** D53 (copy-then-patch) goes from A to B, since it ties gap-fill and its XFS advantage is inferred; D87 duplicated D58 and is folded into it. The October 24 scorecard is now 6 A, 6 B, 5 C, 12 D, 3 X and 3 moot.
- **EXP-10 now reports the verifier's wait,** as its revision required.
- **One queue,** not two that disagreed, and a section for T11b.

**Third check, of those corrections** (2 major, 15 minor; commit 1349218):
- **My first explanation of the clone result was wrong.** I had written that about half of the write phase's saving on XFS comes back as the verifier reading cloned pages from disk. For local changes the write phase falls 2.8 s and the transfer only 1.4 s; the final wait for the verifier is only about half of the 1.4 s that doesn't reach the transfer. I replaced it with an explanation the fourth check then rejected (below).
- **The README listed two risks to v1's HA result** where EXP-09 names four (T2, T8, beyond RAM, persistent disks).
- **D01 goes back to C:** its positioning against the incumbents was never measured from a live server, which is T8's. D02's A was scoped to the incremental hand-off.
- Also: the figure compares the ext4 new-copy arms within one sweep; the summaries give pgBackRest's registered time-rule outcome; stale passages in the ledger carry dated notes.

**Fourth check, of the third round's fixes** (6 major, 11 minor; commit 1e3076a):
- **My second explanation still claimed more than the timers show.** It said the extra waiting was mostly hashing B, and that in place's similar wait showed reading cloned pages adds only 0.13–0.18 s. No timer counts disk reads, and the untimed part has no breakdown. EXP-10 now reports only what the timers show: the final wait for the verifier and the untimed time both grow, the background hashing and verifying take longer, and in place on ext4, with no clones, shows most of the same growth.
- **Ledger:** D02's pg_rewind half becomes its own row, D02b; the T9 persistent-disk note predicted the wrong direction (both methods move 2.1 GB through the disk, so the ratio should rise toward 1.0); T10a judges slowdown per source CPU-second; three dates corrected (arm (e) ran October 23).

**Fifth check, of the fourth round's fixes** (2 major, 12 minor; corrected in the next commit):
- **T9's new rule had dropped EXP-09's undecided band** (0.9–1.0) and its "both hot and append" condition; it now uses EXP-09's bands.
- **This log entry had again presented the 0.12–0.18 s difference in the final wait as a bound** on verifying uncached clones. The final wait only measures the verifier's backlog at the end; its busy time rose 0.20–0.27 s against in place. EXP-10 now says the comparison doesn't measure that cost and suggests a few tenths of a second.
- Also: D02b graded C for correctness and D for speed; T11b's ages, WAL rate and every outcome made explicit; T10a's "tracks" given a tolerance and its rule a threshold; two ranges re-rounded from raw values (0.12–0.18 s, 0.24–0.25 s); the README's block-incremental tie now notes its recovery time; pgdelta's README separates skipping fsyncs from skipping re-reads.

**Added:** the figure [exp10-xfs-and-rerun](figures/exp10-xfs-and-rerun.svg); the later sweeps' runs as [exp10-later-runs.csv](experiments/data/exp10-later-runs.csv); `exp10_later` now prints the tables and every derived figure EXP-10's later sections cite, with maxima and ranges.

## 2026-10-24: EXP-08, EXP-09 and EXP-10 done; VMs deleted

**Ran** on two `n2-standard-8` VMs in `us-central1-a`, created about 21:00 UTC on October 23 and deleted at 00:45 UTC on October 24 (about 3.75 hours each):

| Sweep | Runs | For |
|---|---:|---|
| `exp09-10-ext4` | 255 | EXP-09, and EXP-10's ext4 arms |
| `exp08-pgbackrest` | 162 | EXP-08 part B |
| T11 correctness, two WAL arms | 48 | EXP-08 part A |
| `exp10-xfs` | 75 | EXP-10 (d) |
| `exp10-ext4b` | 60 | EXP-10's drift check and the write-back rerun |

Plus two smoke runs. Every output was verified.

**Results:** summarised in [What T3, T8a and T11 settled](DECISIONS.md#what-t3-t8a-and-t11-settled-october-24-2026); details in each write-up.

**Independent rechecks** found 19 problems in EXP-09/10 (9 major) and 18 in EXP-08 (11 major). All were corrected before this entry. They included:
- my wrong explanation of a flagged `pg_statistic` page;
- pgBackRest's recovery time, missing from its totals;
- a bug in the early write-back arm, fixed with a test and rerun as registered.

**Infrastructure:**
- `pg_probackup` had to be built from source: Postgres Pro's repository didn't answer from Google Cloud, and PGDG ships PostgreSQL 18's libpq headers.
- `reformat_dest_ext4.sh` returns the destination to ext4 without recreating the VM.

**Cost** (Cloud Billing report for the project, last 30 days, read at 00:50 UTC on October 24): $18.20 of usage, $16.33 covered by credits, and a $1.87 subtotal not covered by credits. The sweeps above may not have finished posting. About $1 of the usage is E2 cores from two GKE clusters created in the project on September 25–26 for Omni tests, not from these experiments. No VMs, disks or clusters remain in the project.

**Commit history:** at Matt's request, `main`'s history was rewritten on October 23 to drop an attribution line, and force-pushed (see the entry below).

## 2026-10-23 (late night): the code for EXP-08/09/10 built, reviewed and merged; first sweep running

**Built** (each by one agent in its own worktree, then two adversarial reviewers, then fixes with regression tests):
- **Engine** (`pgdelta/inplace.py`, `flush.py`, `datadir.py`):
  - `--in-place` refreshes B itself behind a startup block. `global/pg_control` is moved aside until the commit and a marker file records the run, so PostgreSQL can't start on a half-refreshed copy, and a rerun finishes the job.
  - `--fsync main|writeback|worker` chooses where files are flushed.
  - `--source-kind datadir` reads a cleanly stopped data directory directly, with PostgreSQL 17's base-backup exclusions.
  - The reviews found 14 problems, 3 of them major: hard-linked files could be patched through a shared inode; two in-place runs could race on one B; `--in-place` would empty any directory. All are fixed.
- **Harness:** methods for T3 (five write-path arms), T8a (direct reads, with a new copy and in place), pgBackRest delta restore (three repository variants) and `pg_combinebackup --copy-file-range`, plus an XFS reformat script for the destination. The reviews found 14, 5 major: a reflink clone of B0 on XFS, pgBackRest still compressing on the network, and its recovery WAL going uncounted. All are fixed.
- **T11 runner** (`experiments/incumbents/`) and the scenarios S2b and S2c, with refusal classes, exposed-page counts and stale pages counted after recovery. Two rounds of reviews found 8 and 9 problems; all are fixed.
- **Tests:** 678 before, 863 after, all passing (5 skipped: Linux-only or binaries missing).

**A development run on the Mac changed EXP-08 before its registered run.** `pg_rewind` needs B's own WAL back to the fork, so it now runs in two arms (registered in EXP-08 first).

**Cloud:** `us-central1-b` had no stock, so the two VMs run in `us-central1-a`, with the same machine type and four local NVMe SSDs each. A small smoke run of every new method passed (20 of 20 verified). The main ext4 sweep for EXP-09 and EXP-10 (255 runs) started at 21:07 UTC.

**Analysis:** `analysis/exp09_10.py` was written and dry-run on the smoke data before the registered results existed. It computes every prediction and rule exactly as pre-registered.

## 2026-10-23 (late night): commit history rewritten to drop an attribution line

**Why:** at Matt's request, every commit message on `main` lost its "Co-Authored-By" line naming the AI assistant, and `main` was force-pushed. Nothing else changed: each rewritten commit has the same files, author, date and subject as before.

**What it changes:** the 43 commits from `cc1fb1a` onward have new IDs. Earlier commits keep theirs. Commit IDs recorded before the rewrite map as follows:

| Before | After | Where the old ID appears |
|---|---|---|
| `6936143` | `c5e2296` | the decision ledger's commit, cited in this log |
| `625285e` | `f9348df` | the owner-decisions commit |
| `80f01f5` | `6ec413f` | EXP-08/09/10's first pre-registration (EXP-08 now cites the new ID) |
| `42aa35f` | `fa29cbd` | the revised pre-registrations |
| `c48e853` | `b77f33d` | the code deployed to the VMs for EXP-08/09/10: recorded in each run's `DEPLOYED.json` and `run.json`. The tree hash recorded there is unchanged. |

## 2026-10-23 (night): sponsor questions closed by decision; next, T11, T8a and T3

**Why:** the decision ledger's first test, T1, was a list of questions for the sponsor. Matt went through them and settled each from the project brief and Google's public docs, so nothing waits on the sponsor any more.

**What Omni does today** (public docs, rechecked by an independent agent; [notes](research/notes/alloydb-omni.md)):
- After an unplanned failover, the operator deletes the old primary and seeds a new standby with `pg_basebackup`, which can take hours on a large database.
- After a DR failover, Google's procedure tries a rewind of the former primary and falls back to a full copy.
- No rebuild path keeps a usable copy when it fails: `pg_basebackup` deletes its partial output, a failed `pg_rewind` leaves its target unrecoverable, and Omni's documented remedies are delete-and-recopy.

**Decisions** ([ledger, O1-O9](DECISIONS.md#decisions-of-october-23-2026-t1-closed)):
- Target Omni's HA standby rebuild and DR re-seed; we define the sizes.
- Run only when an old copy exists; otherwise `pg_basebackup`.
- A program on the source is allowed. Its slowdown must be no worse than `pg_basebackup`'s.
- The old copy may be patched in place; a failed refresh leaves it blocked from starting.
- TLS on the wire. No GPL code; pgdelta is to be Apache-2.0 (no LICENSE file yet).
- T3's two staged-only arms are dropped, since v1 has no capture.

**T1's prediction:** "a source program is allowed" held; "B must survive a failed refresh" failed, so in-place patching becomes a candidate default for T3 to decide.

**Also:** [BEN-QUESTIONS.md](BEN-QUESTIONS.md) now lists the decisions and a few non-blocking questions. The October 22 version, whose draft post was never sent, is archived.

**Next, in order:** T11 (the incumbent tools: `pg_rewind`, the catch-up tools, pgBackRest), T8a (pgdelta reading a stopped source directly, no capture), T3 (the destination write path, including in-place patching). Each gets its predictions written down before it runs.

## 2026-10-23 (evening): decision ledger, every design choice graded by its evidence

**Why:** Matt asked whether v0 is the right direction, whether v1 builds on or scraps it, and whether design choices are backed by evidence or added blindly.

**How:** a multi-agent audit, read-only, with nothing re-run.
- It listed every design decision (89) and graded each by its evidence.
- It compared the direction against the research.
- Two adversarial reviewers challenged the draft, and it was revised.
- Its main new numbers were rechecked against the raw data before committing.

**The ledger:** [DECISIONS.md](DECISIONS.md).
- **Direction:** v0's way of deciding what to reuse (keyed fingerprints of every page, every output byte verified) is well supported. The staged capture is contradicted as a performance path. Writing a complete new copy is the largest pgdelta-specific cost, and it was never compared with an alternative.
- **v1:** builds on v0's correctness core. Replace the source path with direct reads in backup mode, and replace the write path. Whether to keep a custom engine or wrap rsync with pgdelta's verification is still open.
- **Evidence coverage:** of the 33 performance-driven decisions, 4 have strong evidence, 4 moderate, 10 weak, 14 none, and 1 is contradicted.
- **New measured fact:** on the VMs, writing O takes 3.3–3.5 s of pgdelta's 4.2–4.8 s transfer, split into copying B (1.6 s) and fsync (1.7 s). Hashing isn't the bottleneck there.
- **Committed timers:** pgdelta's per-phase timers from all 252 pgdelta runs are now in [exp04-pgdelta-timers.csv](experiments/data/exp04-pgdelta-timers.csv).
- **Next:** a ranked queue of experiments, T1 to T14, each with a prediction and a falsifier. It starts with questions for the sponsor and a measurement of how much a refresh slows the source.

## 2026-10-23 (later): closing EXP-06's open question

**Does pipelined verification help at the default of 4 processes?** EXP-06 had left this mixed: one busy re-run with 3 repeats had it costing 0.18 s for uniform.
- **Method:** predictions and a decision rule were recorded first. Then 10 interleaved runs per configuration on the kept EXP-06 pairs ([results](experiments/EXP-06-engine-throughput.md#results-of-the-check-run-october-23-2026)).
- **Result:** pipelined was 0.06–0.13 s (3–7%) faster in all three workloads, with one-sided permutation p = 0.05–0.08 each. Both numeric predictions failed: the effect was smaller than predicted for hot, and present, not absent, for uniform. The decision rule keeps `--verify pipelined` as the default.
- **Other loose ends from the reviews, and how they stand:**
  - EXP-07's C7-against-C6 comparison is closed as "no measurable difference". It changes no decision.
  - EXP-06's thin margins on the busy Mac were superseded by the Linux check and EXP-04/04b, which measured the engine on the VMs.

## 2026-10-23: EXP-04b done; VMs deleted; where the project stands

**EXP-04b** ([write-up](experiments/EXP-04b-default-checksums.md)): the same sweep with PostgreSQL's default CRC32C manifests. 324 runs, all verified.
- **What CRC32C changed:**
  - the full copy and the staged capture halved (8.3 → 4.0 s);
  - `pg_combinebackup` fell 34–38%;
  - pgdelta's and rsync's own transfers were unchanged.
- **Crossovers** (beats a full copy below):

| | Scattered changes | Local changes |
|---|---|---|
| pgdelta | 2.0 Gbit/s | 2.5 Gbit/s |
| rsync | 0.8–1.0 Gbit/s | 3.9 Gbit/s |
| incremental | 3.75 Gbit/s | every speed for hot; append lost narrowly at the unthrottled link |

- **Predictions:** R1–R4 and R7 held. R5 failed for local changes: rsync's crossover came out above the predicted range, where the arithmetic check written before the results put it. R6 held only in part: append lost narrowly at the unthrottled link.
- **A second independent recheck** of the EXP-04b write-up found 3 major and 9 minor overstatements, all fixed. The append result is a loss, not a tie. The hot/append difference is noise in the full copy between trials. Incremental's lead at full speed shrank.
- **Independent recheck:** two reviewers recomputed the EXP-04 and EXP-07 write-ups from the raw data and found 9 major and 22 minor overstatements. All are fixed.
  - N2 failed for every method but pgdelta.
  - N5 held in full under the serial reading.
  - N7 held only in part.
  - EXP-07 measured only the local capture, not every backup.
  - pgdelta's own transfer is the other half of its cost.

**The two VMs were deleted** at 00:05 UTC, after the results were fetched. No instances or disks remain. **GCP access and spend, checked through `gcloud` as Matt (read-only):**
- Project `postgres-delta-backup` is ACTIVE, with billing enabled on the sponsored billing account.
- No instances or disks remain.
- The audit log shows both VMs created at 10:08 UTC on October 22 and deleted at 00:07 UTC on October 23, so they ran 14.0 hours.
- At list prices that's about 15–16 USD: two `n2-standard-8` at about 0.39 USD/hour each, plus 4 local SSDs each at about 0.16 USD/hour.
- **Read from the console's billing reports** (Matt signed in; checked October 23 at about 00:45 UTC):
  - Usage processed so far for October 8–22 is $2.72: N2 core $1.16, N2 RAM $0.76, local SSD $0.72, disks $0.04, networking $0.03.
  - All of it was covered by the sponsored credits, so **$0.00 has been charged.**
  - That is only about 2.5 of the 28 VM-hours. Google posts usage with a delay of up to a day or so, so the rest of October 22 hasn't appeared yet.
  - The list-price estimate above (about 15–16 USD of usage) stands until it posts, and the credits are expected to cover it as well.

**Where the project stands:**
- **pgdelta v0 is correct and sends the minimum page data.** That's evidence from 756 runs between two machines plus the tricky-copy experiment.
- **It isn't yet fast enough to matter on fast links.** Where PostgreSQL's incremental backup can be used, it wins.
- **Where incremental can't be used,** pgdelta wins only for scattered changes on links below about 2 Gbit/s. rsync wins for local changes below about 3.9 Gbit/s. Both lose to a plain full copy above that.
- **Two design directions follow from the measurements:**
  - start the session before the capture, so the destination can hash and copy B while S is taken;
  - read the source's files directly instead of taking S.

## 2026-10-22 (night, cont.): EXP-04 done; the cause found (EXP-07); EXP-04b running

**EXP-04** ([write-up](experiments/EXP-04-network-sweep.md)): 432 runs between the two VMs, all passed and verified.

| | Beats a full copy below |
|---|---|
| pgdelta | 1.1 Gbit/s (scattered changes), 1.5 (local) |
| rsync | 0.6 (scattered), 1.9 (local) |
| PostgreSQL incremental | 3.0 (scattered), every speed (local) |

- **The surprise:** the local backup S that pgdelta and rsync start with took 8.5 s, as long as a whole full copy over the network. A staged method can't win on fast links while that holds.
- **Predictions:** N6 and N7 held. N2 held for pgdelta (within 7% in all 18 cells) but failed for the full copy. N3 and N5 held in part. N1 held in part (overhead 0.1–0.4%, not 2–6%). **N4 failed** (crossovers lower than predicted).
- **Correction to EXP-02:** on the wire, incremental streams only the WAL actually written. pgdelta and rsync must send a 16 MiB segment padded with zeros. So for local changes incremental sends the fewest bytes (17 MB against 29 MB), not pgdelta. A note was added to EXP-02.

**EXP-07** ([write-up](experiments/EXP-07-capture-speed.md)): why the capture was so slow.
- **The harness asked `pg_basebackup` for SHA-256 manifest checksums.** With PostgreSQL's default, CRC32C, the local capture takes 4.1 s instead of 8.6 s. The same setting applied to every full copy, capture and combine in EXP-04; EXP-04b measures how much each changes.
- **Reading the files directly with 4 parallel readers takes 0.73 s** (2.9 GB/s), 5.5× faster than even the CRC32C backup.
- **One mistake, caught and fixed:** the first attempt's direct-read variant listed `pg_wal`, whose old segments PostgreSQL recycled mid-run. It was fixed and re-run, and both attempts are kept.
- **Next design step:** read the source's data files directly, in parallel and in backup mode, instead of writing S. It needs its own correctness experiment (torn pages, WAL replay).

**EXP-04b** ([pre-registration](experiments/EXP-04b-default-checksums.md)): the same sweep with CRC32C manifests.
- Speeds 0.5–5 Gbit/s and unthrottled, 3 repeats.
- Predictions computed from EXP-04's phase times and EXP-07, recorded before the sweep.
- Started 22:24 UTC. The VMs are deleted when it ends.

## 2026-10-22 (overnight): Linux check done; EXP-04 sweep running

**pgdelta gets the same head start as rsync** (recorded in the [EXP-04 write-up](experiments/EXP-04-network-sweep.md) before the sweep):
- rsync copies the old copy at the destination while the source captures S. So now pgdelta's receiver starts during the capture and reads B into memory while it waits.
- It can't fingerprint B early: the key arrives with the sender's first message, by design.
- Any reading that happens between the two timed phases is measured and added to the checks.
- Incremental is now the only method that reads B cold on its critical path. Comparisons with it say so.

**EXP-06's Linux check** (on `pgd-dest`; predictions recorded first): all three held.
- 4 worker processes hash 3.9× faster than 1.
- 8 processes are only 4.5× faster, at 1.8× the CPU, because the 8 vCPUs are 4 cores with two hyperthreads each. So the default of 4 holds on Linux too.
- One VM core hashes at 0.55 GB/s, 2.4× slower than the Mac.

All 676 tests pass on the VM's Python 3.11.

**Full EXP-04 sweep started at 18:13 UTC** at commit `d559b1b`:
- 3 workloads × 8 link speeds × 6 methods × 3 repeats;
- link speeds and method orders shuffled;
- estimated 4–5 hours.

The VMs are deleted once the results are fetched. `analysis/exp04.py` was written and tested on the smoke data, so the figures and verdict numbers come straight from the sweep.

## 2026-10-22 (late night): EXP-06 halves pgdelta's fixed cost; EXP-04 tooling ready

**EXP-06** ([write-up](experiments/EXP-06-engine-throughput.md)), on the Mac, inputs cached:
- **What changed in the engine:**
  - hashing runs in 4 worker processes;
  - each output file is built by copying the old file in bulk and then patching the changed pages;
  - finished files are verified in parallel.
- **Result:** a 2.1 GB refresh with 1% of rows changed went from 4.37 s to 1.40 s for scattered changes, and from about 3.0 s to 1.3 s for local ones. Outputs were byte-identical in all 270 benchmark runs (two passes plus the re-run).
- **Modelled crossover against a full copy:** from 2.8 to 8.6 Gbit/s for scattered changes, and to about 13 Gbit/s for local ones, level with rsync.
- **Five of seven predictions held. Two held only in part:**
  - copy-then-patch helps only scattered changes;
  - 8 processes went over the CPU bound on the Mac's mixed cores, so the default is 4, not the best-measured 8.
- **Still open:** parallel verification's benefit at the default of 4 processes is unproven.
- **The reviews caught two major problems,** both fixed:
  - on Linux, worker CPU would have gone uncounted, because the workers would have been started by a separate server process;
  - an argument for the default rested on a page-cache artefact.
- **Gap found in the method:** reading the inputs once before the runs didn't keep them cached, so every run now re-reads its own inputs first. EXP-05's numbers may include some cold reads.

**EXP-04 fairness fixes are merged** (two review rounds, re-validated on the VMs):
- rsync now pays for copying the old copy and flushing it to disk;
- peak memory is measured per tool;
- link speeds run in random order;
- pgdelta's two engines (the new default and EXP-02's single-threaded one) are pinned by their flags.

**Privacy:** the benchmark records listed other programs running on the Mac. The repository is public, so committed data and write-ups now use generic labels ("desktop application", "macOS system process").

**Next:** give pgdelta's destination the same head start rsync gets: start the receiver, and fingerprint the old copy, while the source takes its backup. Then check the Linux engine on a VM, and run the full EXP-04 sweep.

## 2026-10-22 (night): EXP-05 failed; EXP-06 designed

**EXP-05, parallel hashing with threads** ([write-up](experiments/EXP-05-parallel-hashing.md)): three of four predictions failed.
- Results stay identical for any thread count, but 2–4 threads gain at most 1.2×. **8 threads is 1.5× slower than 1 and uses 3× the CPU.**
- **Cause:** Python's interpreter lock is taken back after every 8 KiB page, and the hand-off costs more than the hash.
- **Second finding:** writing the output is as slow as hashing. With scattered changes, the destination copies tens of thousands of small runs of unchanged pages one by one, and then re-reads everything to verify it. This also explains why EXP-02's uniform 1% runs were slower than 5–10%.
- **The review found a deadlock** in the thread pool's shutdown, and the default of 8 threads is the slowest setting. The branch isn't merged as is.

**Next ([EXP-06](experiments/EXP-06-engine-throughput.md), pre-registered):**
- hash in worker processes;
- build each output file by copying the old file in bulk and then patching the changed pages;
- verify finished files in parallel while the next one is written.

Each change can be switched off, so its share is measured. Target: uniform 1% from 4.55 s to at most 2 s.

**EXP-04 waits for this,** so the sweep on real links measures the engine that would actually ship, alongside one-thread pgdelta. The VMs keep running meanwhile (about 1 USD/hour).

## 2026-10-22 (night): EXP-04 tooling built; fairness fixes before the sweep

**Built and smoke-tested** ([experiments/network/](../experiments/network/README.md)):
- Two `n2-standard-8` VMs in `us-central1-b`, each with 4 striped local NVMe SSDs. Other zones had none in stock with 2 SSDs.
- **Running now, about 1 USD/hour together,** until the sweep is done. They'll then be deleted with `infra/gcp/teardown.sh`.
- The smoke run (scale 5, 30 method runs) passed: every output was verified, staged outputs were byte-identical to S, and B0 was untouched. At that size fixed costs dominate, so its timings only check the tooling.

**The fairness review found 2 major and 4 minor problems**, all being fixed before any real data is collected:
- **rsync's timer was narrower than the others'.** Copying B0 and flushing the copy to disk happened outside it, and rsync didn't flush its own writes. pgdelta, `pg_basebackup` and `pg_combinebackup` all build and flush a complete copy inside their timers. Left as is, rsync would have looked faster than it is.
- **Peak memory was wrong for every non-Python tool:** a child process reports its parent's peak memory from before it started.
- **Minor:**
  - SSH launch overhead counted against the staged methods only;
  - the 16 MiB WAL segment each staged copy must send wasn't recorded separately;
  - link speeds always ran in the same order;
  - one rsync log overwrote the other.

[EXP-04's predictions](experiments/EXP-04-network-sweep.md) were recorded before the smoke run's numbers were looked at.

## 2026-10-22 (evening): EXP-01 and EXP-02 done

**EXP-01, how many pages change** ([write-up](experiments/EXP-01-change-patterns.md)), 37 trials at about 2.1 GB:
- The simple model `1 − (1 − f)^(rows per page)` predicted changed heap pages within 0.4 percentage points at every fraction.
- **Local changes** (hot rows, appends, retention, ranges) left 99% of pages untouched: a refresh sends about 1% of the database.
- **Scattered updates** dirty most of a table once more than about 2% of its rows change. Then no page-level method can avoid resending the table.
- 8 KiB comparison halves the bytes against 64 KiB chunks for scattered changes.
- PostgreSQL's change tracker missed no changed block with checksums on. With checksums off, a read scan changed 100,001 pages through hint bits alone, which the tracker (correctly) ignores.
- Every prediction held, except that index pages changed at about 2f, not f, for clustered updates.

**EXP-02, comparing methods on the same inputs** ([write-up](experiments/EXP-02-local-comparison.md)):
- **Bytes:** pgdelta's page data equalled rsync's (8 KiB blocks) to the byte in all 37 trials, and was 0.5–20% below PostgreSQL incremental, whose extra is exactly its "tracked but unchanged" blocks. Default rsync sent 1.7–2.5× more for scattered changes.
- **Compute is the real difference.** pgdelta spends a nearly fixed 3–5 s per 2.1 GB (hashing every page on one core, three passes, plus writing a full new copy). rsync takes 1.2 s for local changes but up to 10 s for scattered ones.
- **Model of time vs. link speed:** pgdelta beats a full copy below about 2.5 Gbit/s for scattered changes and 5.5 Gbit/s for local ones; rsync below 1–1.6 and about 13 Gbit/s respectively.
- **Wrong predictions:** rsync's checksum traffic back to the source was six times lower than predicted (0.04%), and default rsync's penalty was 1.7×, not ≥2×, at 1% of rows. One open question: pgdelta was slower at 1% of rows than at 5–10%.

**Decision:** pgdelta's fixed hashing cost is the bottleneck, so the next change is parallel hashing ([EXP-05](experiments/EXP-05-parallel-hashing.md), pre-registered before building it). Leaf-fingerprint truncation would cut back-traffic for scattered changes, but at 0.1% of the database it can wait.

**Housekeeping:** figures are now byte-stable across reruns (fixed SVG ids, no timestamps, deterministic line order), so regenerating them only shows in git when the data changes.

## 2026-10-22 (later): Engine built and reviewed; EXP-03 done

**Built, each in its own worktree and each reviewed by a separate agent that tried to break it:**
- **`pgdelta/`, the v0 engine:**
  - keyed BLAKE2b hash tree (8 KiB pages, 1 MiB nodes);
  - framed sender/receiver protocol over pipes or TCP;
  - verified rebuild into a staging folder, renamed into place only after every file checks out.
- **`experiments/change_patterns/`, the EXP-01 study tooling,** plus `compare_methods.py` for EXP-02.
- **`benchmarks/methods.py`, pluggable backup methods,** with two new ones: `rsync_staged` and `pg_delta_staged`.
- **`experiments/tricky_copies/`, the EXP-03 scenarios.**

**Reviews found 4 major and 22 minor problems.** Every one was reproduced before being fixed, then fixed with a regression test that failed first, and the fix was re-reviewed. The majors:
- **Engine:** on macOS's case-insensitive disk, `--output basis/O` could create O *inside* the old copy `Basis`. Placement is now checked by filesystem identity, not path text.
- **Harness:** `pg_verifybackup` doesn't check every file (it skips `postgresql.auto.conf`, `*.signal`, extra WAL), so a wrong rebuild could pass. The harness now also compares the output with the fresh backup file by file.
- **Harness:** an output published as a symlink to the source passed every check. Now refused.
- **Study:** the tracker's per-file block lists were parsed and then thrown away. Now kept in the trial JSON.

The test suite went from 133 to 478 tests, all passing.

**EXP-03 results** ([write-up](experiments/EXP-03-tricky-copies.md)):
- **The tool was correct on all ten tricky old copies:** byte-identical, `pg_verifybackup`, restore and data comparison.
- **LSN skipping went wrong in four of the eight same-cluster scenarios.** After a failover it would have kept 2,371 pages of stale data. Two of my seven pre-registered LSN predictions were wrong; the write-up says so and explains why.

**Smaller decisions:**
- **The identity check is a policy, not a safety requirement.** Every output file is verified against the source, so reuse from a "foreign" old copy can't make the result wrong. `--reuse-any-basis` turns the policy off; across a 17→18 upgrade it reused 53% of pages.
- **Known residual issues, accepted for now (all minor):**
  - In `local` mode, a signal arriving right after publication reports failure even though O is complete; the receipt beside O shows the truth.
  - The harness doesn't re-check that the staged source backup S is unchanged before comparing.
  - The HammerDB driver now waits for the workload before saving the backup record.

**Then ran:** the EXP-01/02 matrix at scale 70 ([run plan](../experiments/change_patterns/run_exp01.sh)); results in the entry above.

**GCP access confirmed** (Matt logged in to `gcloud` with his own account):
- Project `postgres-delta-backup` is active, with billing on the sponsored account and Compute Engine enabled.
- Quotas in us-central1: 200 N2 vCPUs, 24 instances, 8 IP addresses.
- The default VPC allows all internal traffic between VMs.

**Started:** building and validating the EXP-04 two-machine driver on two `n2-standard-8` VMs, plus a fairness review. Every method gets:
- the same timing boundaries;
- shaping in both directions, with the achieved rate verified;
- OS page caches dropped before each timed run;
- randomized method order;
- wire bytes counted from the network interface, not taken from the tools' own reports.

## 2026-10-22: Starting the build

**Goal.** Build the tool end to end and back every claim with an experiment.

**Environment found.**
- **Local machine:** Apple M5 Pro, 15 cores, 24 GB RAM, 764 GB free disk. No Docker, PostgreSQL or `gcloud`. Python 3.14 and Homebrew are present. PostgreSQL 17.11 and 18.6 are being installed with Homebrew; both include the August 2026 visibility-map fix.
- **GCP:** the project `postgres-delta-backup` isn't reachable yet. The browser pane isn't signed in to Google, and there's no `gcloud`. Matt needs to sign in before the two-machine benchmark; everything until then runs locally.

**Plan, in order of what de-risks the project most.**
1. **Change-pattern study.** How many 8 KiB pages, and how many larger chunks, actually change when a given fraction of rows changes, under different workload shapes? If most pages change, the tool can't win, so this goes first.
2. **v0 engine.** Hash tree over 8 KiB pages, a transfer protocol between a sender and receiver process, and a verified rebuild into a new folder. Run locally first, with exact byte accounting.
3. **Local comparison** against a full copy, PostgreSQL incremental backup and rsync, on the same inputs.
4. **Correctness experiments** on tricky old copies: a server that diverged after failover, truncation, `FILE_COPY` databases, enabling checksums, unlogged tables. Each one also checks whether skipping pages by LSN would have been wrong.
5. **Two-machine benchmark on GCP** over throttled links, to find the link speed where the tool starts to win.

**How the work is organised.** The engine, the study tooling and the harness refactor are built in parallel, each in its own git worktree. Each is then reviewed by an independent reviewer that tries to break it before anything is merged.
