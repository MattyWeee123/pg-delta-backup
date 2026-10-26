# Project log

A dated record of what was done, why, and what came out of it. Newest entries first.
Experiments have their own write-ups in [docs/experiments/](experiments/).






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
