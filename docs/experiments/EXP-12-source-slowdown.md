# EXP-12: How much does each refresh slow a busy source, and is every output correct under load?

Status: **done** ([results below](#results); run October 24, 2026, on two VMs created at 16:51 UTC and deleted at 20:10 UTC: the smoke run 16:57–18:18, the registered sweep 18:23–20:08, nothing rerun; the write-up was corrected after an independent recheck the same day, listed under [Corrections after the recheck](#corrections-after-the-recheck-october-24-2026)). Pre-registered October 24, 2026, and revised after an independent review and a check of the revision, all before running; the harness was built and reviewed, and the smoke run's sixteen items and the drift check were recorded here, dated, before the sweep's data was read (see "Analysis readings"). This is test T10a of the [decision ledger](../DECISIONS.md#t10a-source-slowdown-minimal-version-plus-the-correctness-check-under-load-done-october-24-2026), with gate G4 of the [spec](../SPEC.md#7-correctness-gates). It merges two independent drafts, one written for realism and decision value, one for measurement validity; where they differed, the choice and its reason are under [How the two drafts were merged](#how-the-two-drafts-were-merged). An independent review then checked the merged draft against the ledger, EXP-04b, EXP-09, EXP-11 and the harness code: 16 findings, 5 major. The project lead decided each one. Every change, and the parts not taken, are listed under [Revisions before running](#revisions-before-running-october-24-2026). A second, independent check then tested the revision: 9 findings, 2 major, and 5 items left unaddressed or unrecorded. All were applied; the changes are under [After a check of the revision](#after-a-check-of-the-revision). The ledger's registered question, prediction and rules, and owner decision O4, are kept word for word and judged as registered. Everything marked "added here" is new, and "revised" marks what the review or the check changed. Where the design departs from the ledger, the reason is under [Changes from the ledger](#changes-from-the-ledger-rule-8), as rule 8 asks. As for EXP-11, the harness build gets its own review, and the smoke run's findings are recorded here, dated, before the main sweep. How the harness and the analysis read what the text leaves open, after that review, is under [Analysis readings](#analysis-readings-october-24-2026-after-the-harness-builds-review).

## Question

Owner decision O4 (registered, October 23):

> **Slowdown bar: no worse than `pg_basebackup` under the same load.** T10a measures two numbers, both fixed before it runs: the worst drop in throughput, and the total work lost over the refresh.

The ledger's T10a (registered):

> with a fixed-rate pgbench or HammerDB load on the source, how much do throughput and p95 latency drop? Compare against no backup at all, during a full copy, the capture, pgdelta and rsync, at 0.5 and 2 Gbit/s. Check every output: `pg_verifybackup`, recovery, and a logical match.

The spec's G4 (registered): "Required sponsor milestone: sustained write workload during capture; pass G2/G3 and record capture-end, coverage and publication boundaries separately". G2 is `pg_verifybackup` with WAL parsing. G3 asks that a restored copy "matches expected committed data, schema and transaction invariants; aborted/uncommitted effects are absent".

Why it matters now:
- After EXP-11, v1 targets DR's slower links first. There it beats every copy measured on time. What it costs the operator instead is source work. v1 hashes every page of the source on 4 workers: 5.3–6.0 CPU-s per run in EXP-09 and 6.1–7.2 in EXP-11, against 1.6–2.7 for one `pg_basebackup` stream.
- No method's slowdown has been measured on the VMs. D04, D50, D68 and D81 have no data. The only slowdown number is one laptop pilot with HammerDB: a 71.4% throughput cut over the backup's own window (57.6% over the whole run), one repetition on a shared host ([pilot report](../../benchmarks/evidence/2026-10-17-hammerdb-pilot/report.md)).
- v1 can't read a live source until T8. So T10a measures staged pgdelta, and the ledger's October 24 note says how to carry the result to v1. That note assumes the loss grows with CPU-seconds. But staged pgdelta's largest source cost, writing 2.1 GB of S on the source's disks, is one v1 doesn't have. So this design adds a direct measurement of v1's source side, a proxy, so that the answer doesn't rest on one model.

On two VMs, in one session, with a fixed-rate write load on the source:
1. **What does each method cost the load?** O4's two numbers, with p95 and p99 latency beside them.
2. **Does the cost track source CPU-seconds, as the ledger assumes?** And what do that and the proxy say about v1?
3. **Is every output taken from the live source correct (G4)?**

**What it decides:**
- the T5/T8 order (the registered consequence);
- whether v1 needs a cap on its source workers, which moves T10b (D50) and gives D81's CPU budget a basis;
- D04's gray area ("slowdown never measured"), and the slowdown column rule 3 asks to set next to time;
- whether EXP-11's HA qualifier needs a slowdown caveat.

It doesn't measure v1 reading a live source. That is T8.

## Setup

**Machines.** Two `n2-standard-8` VMs (8 vCPUs, 32 GB) in `us-central1-a`, as EXP-08 to EXP-11, labelled `purpose=pg-delta-benchmark`. At most two exist at any time, about 1 USD per hour together. `config.sh` defaults to `us-central1-b`, so the run sets `ZONE`.
- Four local NVMe SSDs in RAID 0 at `/data`, ext4 as `startup.sh` makes it (ordered mode, barriers on), Debian 12. The data, the WAL and staged pgdelta's S share that array.
- PostgreSQL 17 from PGDG, with `pgbench`, `pg_amcheck`, `pg_dump` and `pg_test_fsync` from the same packages. The minor version is recorded. Both VMs must run the same one, since check (vii) hashes a dump whose header names it (revised). If they differ, the older VM gets the newer minor version from PGDG before the sweep, recorded.
- pgdelta at EXP-11's code. The run records `git diff` of `pgdelta/` against EXP-11's deployed commit.
- The zone, the CPU platform, both VMs' clock offsets (`chronyc tracking`) and `vm.dirty_*` go in `run.json`.

**The source cluster.** EXP-01's, as in EXP-11: pgbench scale 70 plus the `events` table (2.1 GB) in database `postgres`, data checksums, `wal_level = replica`, `summarize_wal = on`, `synchronous_commit = on`, `shared_buffers` 128 MB. Added here, for every arm alike:
- `checkpoint_timeout = 30min` and `max_wal_size = 16GB`, so no checkpoint starts inside a window except the one an arm asks for. The backups' own fast checkpoints keep `pg_wal` small.
- `track_wal_io_timing = on`, so `pg_stat_wal` times WAL writes and syncs. It can explain a stall; it decides nothing.
- `log_checkpoints = on` (the default since PostgreSQL 15) and `log_autovacuum_min_duration = 0`, so checkpoints and autovacuum runs inside a window can be flagged.

**Manifest checksums (rule 7):** CRC32C for every `pg_basebackup`: B0, `full`, and the staged capture.

**The load (added here; change 2).** A separate database, `loaddb`, in the same cluster:
- `pgbench -i -s 2 -F 90 loaddb`: 200,000 accounts, about 30 MB. `abalance` isn't indexed and pages keep 10% free, so the updates are HOT and the size stays constant.
- `load_progress`: one row per client (`client`, `n` = 0, a 900-byte filler), fillfactor 10, so each row sits on its own page.
- Autovacuum is off for loaddb's tables, so no vacuum of them falls in a window.
- Two scripts. `commit.sql`, weight 99:
  ```
  \set a1 random(1, 200000)
  \set a2 random(1, 200000)
  \set lo least(:a1, :a2)
  \set hi greatest(:a1, :a2)
  \set d random(2, 5000)
  BEGIN;
  UPDATE pgbench_accounts SET abalance = abalance + :d WHERE aid = :lo;
  UPDATE pgbench_accounts SET abalance = abalance - :d + 1 WHERE aid = :hi;
  UPDATE load_progress SET n = n + 1 WHERE client = :client_id;
  END;
  ```
  `abort.sql`, weight 1:
  ```
  \set a random(1, 200000)
  BEGIN;
  UPDATE pgbench_accounts SET abalance = abalance + 1000000000 WHERE aid = :a;
  ROLLBACK;
  ```
- **The balance invariant.** Each committed transaction adds exactly 1 to the sum of `abalance` and 1 to the sum of `n`. So in any consistent state, Σabalance = Σn. A torn transaction breaks it by 1 to 5,000. A rolled-back change that leaks breaks it by about 10^9. Locks are taken in one order (lower account, higher account, the client's own row), so there are no deadlocks.
- **The consistent cut (revised).** Each client's `n` says exactly which of its committed transactions a copy holds. But `n` counts across every pgbench run on the cluster, the capacity runs included. So each run's starting values are recorded (next point), and a copy's `n` for client k points at line n − n_k(start) of `commit.sql` in the log of the run whose span holds it.
- **Every pgbench run is logged and offset (revised).** With the load stopped, just before every pgbench start, the harness records each client's `n` and Σabalance: the capacity runs, the busy and moderate loads, a restart after the load dies, and every smoke run. It records them again after every stop. For each run and client, `n` at the stop minus `n` at the start must equal that client's count of committed transactions in the run's log.
- **Open loop at a fixed rate.** `pgbench -n -M prepared -c 32 -j 4 -R R -T 14400 --random-seed SEED -f commit.sql@99 -f abort.sql@1 -l --log-prefix /dev/shm/exp12/BLOCK -P 1 --progress-timestamp loaddb`, over the source's Unix socket, with no `--latency-limit`. With `-R`, pgbench schedules transactions on a Poisson clock and measures each one's latency from its scheduled start. So a stall shows up as late transactions, not as fewer requests, and pgbench never skips one.
- **Every transaction is logged** to tmpfs on the source (about 40 bytes each; about 3.6 GB for the whole trial at the expected rates, the capacity run's included), so logging adds no disk I/O. Each line has the client, its transaction number, the script, the completion time (µs) and the latency and schedule lag (µs). Its scheduled start is the completion time minus the latency; its actual start adds the lag back (smoke item 1 checks both).
- **One pgbench runs at R_busy through the busy and warm blocks, and another at R_mod through the moderate block.** Each is stopped with SIGALRM, the signal its own `-T` timer uses, so it ends as at the end of `-T` and writes out its logs. Smoke item 1 checks this (each client's `n` at the stop minus its `n` at the start must equal its log's count of committed transactions). If it doesn't, SIGINT is used, and every line up to 1 s before the signal must be present.
- **The client runs on the source (revised).** Applications usually run on other hosts. The client sits on the source because `throttle.sh`'s tbf shapes all of a VM's egress and can't exempt the load's traffic: on dest, the load would queue behind the copy (the tbf's queue bound is 50 ms), and the test would measure the link, not the source. A third VM isn't allowed. The cost: an arm that uses CPU also delays pgbench's threads, which an application on another host wouldn't feel. That charges CPU-heavy arms, v1's proxy among them, more than their real cost. The sampler measures it, and the "client wait" flag shows where it matters (A8).

Why this shape:
- **A separate small database keeps the change pattern stable.** pgbench's built-in transaction on the benchmark tables would ruin it: at 1,000 transactions per second it touches about 40% of `pgbench_accounts`' 115,000 pages in a minute, so later runs would refresh a far larger delta than earlier ones. Here the benchmark database stays exactly as the workload left it, and loaddb adds a fixed 30 MB.
- **It needs no reference state.** The invariant and the cut hold in any consistent copy, so a wrong copy fails them, whatever the source did meanwhile. (EXP-11's logical check could only ever be void, since the source's catalogs changed in every trial.)
- **It is synthetic.** A write-heavy load on a small cached table, with commit fsyncs on the same disks as the data. It has no read I/O and no large working set. TPROC-C is T10b's.

**Rates (added here).** C is the load's closed-loop capacity: the same scripts and 32 clients with no `-R`, measured in the smoke run on the scale-70 cluster (item 2). Two load levels, each for a whole block:
- **busy:** R_busy = 0.6 × C, rounded down to 100 per second. A primary that is busy but healthy. This block decides.
- **moderate:** R_mod = 0.3 × C. It tells whether the verdict depends on the load.

Both are written here, dated, before the main sweep, with the busy CPU at R_busy, which selects E4's and E7's ranges. The trial also measures its own C before the load starts, as a drift check (A12). Every capacity run is logged and offset like the load.

**Workload.** EXP-01's **hot** workload at 1%, EXP-01's seed, as in EXP-09 and EXP-11: one trial, one cluster, one B0. Uniform and append are left out (change 9).

**Link speeds:** 0.5 and 2 Gbit/s, as registered, and unthrottled (about 15 Gbit/s), added for the HA caveat (change 1). Shaping is `infra/gcp/throttle.sh`, a tbf on both VMs' egress. The load never crosses the link. The single-stream link test runs at each speed's visit, as before.

**Blocks, repeats and order.** One trial with four blocks, in this order: **busy** (5 repeats), **warm** (5 repeats, at R_busy; reported beside, except that it names the cause of a proxy's miss, row 7; revised), **moderate** (3 repeats, at R_mod), and **calibration** (2 runs per arm, no load). In each repeat the speeds are visited in an order drawn from their own seeded stream. Within each (block, repeat, speed) every selected arm runs once, in a seeded random order. The link-free arms (`none`, `capture`, `bracket`) run in the 2 Gbit/s visit.

**Order within the trial.**
1. The cluster, the benchmark data and loaddb, with `CREATE EXTENSION amcheck` in loaddb (revised), so every copy holds it; `VACUUM ANALYZE`; `CHECKPOINT`.
2. B0 over the unthrottled link (CRC32C), verified, hashed in full.
3. EXP-01's hot workload and its `CHECKPOINT`.
4. Start references, with no load: the benchmark database's per-table logical digest (`measure.logical_digest_sql`), then `CHECKPOINT`; loaddb's invariant (Σabalance = Σn = 0); the schema reference, a SHA-256 of `pg_dump --schema-only --restrict-key=exp12` of loaddb and of `postgres` (revised). The fixed key matters: since PostgreSQL 17.6, a plain-text dump carries a `\restrict` line with a random key unless one is given.
5. **The snapshot:** a clean stop; `cp -a --reflink=never data snap` on the source's `/data`, then `sync -f`; the snapshot's selection digest (`agent.py datadir-digest`); a restart. The snapshot never changes after this.
6. Probes, with no load: the idle background CPU rate (20 s); the clock offset between the VMs; `fincore` of the data directory; a WAL sync probe (`pg_test_fsync -s 2` on `/data`).
7. The trial's own C (30 s warm-up, 60 s closed loop), logged, with each client's `n` recorded before and after (revised); then 30 s of rest.
8. The sampler and the load at R_busy, each client's `n` recorded first; 120 s of warm-up.
9. Controls: `pause2` and `burn4`, in a seeded order.
10. The busy block.
11. Controls again: `pause2` and `burn4`.
12. The warm block.
13. The load stops and each client's `n` is recorded; it restarts at R_mod with a new log; 120 s of warm-up; one `pause2`.
14. The moderate block.
15. The load stops. 20 s later, on the source (revised): Σabalance = Σn = the sum, over every logged run, of its committed transactions; and for each run and client, `n` at its stop minus `n` at its start equals its log's count. The logs and the sampler's data are compressed (zstd) and fetched to dest.
16. 30 s idle, then the calibration block.
17. End references: the benchmark database's logical digest (must equal step 4's), the schema dumps (must equal step 4's; revised), EXP-01's row-level ground truth, the snapshot's digest (must equal step 5's), B0's full digest. Then the checks that wait for the logs: each output's consistent cut and coverage (Verification).

No source restart happens while the load runs. The only stops are the snapshot's (step 5) and v1's own in the calibration block, after the load has stopped.

**Each run (busy, warm and moderate blocks).**
1. **Source, untimed, before the settle:** the previous staged run's S is verified and removed; the benchmark database's files (`base/` of `postgres`, about 2.1 GB) and the snapshot are evicted from the page cache with `POSIX_FADV_DONTNEED`, and `fincore` records what stays resident. loaddb, `pg_xact` and `pg_wal` are left alone. In the warm block the benchmark database's files and the snapshot are read once instead.
2. **Dest, untimed, during the settle:** the rest of the previous run's verification; this run's set-up (B for the proxies: `cp -a --reflink=never B0 B; sync -f B`, as EXP-09); dest's cache drop (`sync; echo 3 > drop_caches`).
3. **The settle (revised):** at least 15 s with nothing on the source but the load, the sampler and the harness's counter reads, until the source's settle gate passes (A8) **and** dest's work is done. The source's gate has a cap: if it hasn't passed after 60 s, the run is flagged "unsettled". Dest's work has none: no window starts before it ends, however long that takes, and the wait is recorded.
4. **The timed phase or phases.** Dest's counters for checks (a) and (b) are read just before the clock starts and first after it stops (revised).
5. **The quiet tail:** 15 s with nothing on the source but the load. Dest's verification starts only after the clock has stopped and checks (a) and (b) have had their last read (revised).

No check or set-up step of the harness reaches the source inside a settle, a window or a tail, and none on dest overlaps a timed phase. The only work on the source there besides the load is the arm's own commands, the counter reads and the sampler, with its one query per second, the same in every run.

**Cache state at the start of every run.**

| | Source | Destination |
|---|---|---|
| Page cache | **never dropped,** since that would evict the live load. The benchmark database's files (about 2.1 GB) and the snapshot are evicted with `POSIX_FADV_DONTNEED`, so every copy reads them cold, as in EXP-09 and EXP-11 and as a copy of a database larger than RAM would. loaddb stays resident. `fincore` is recorded before every window (A8). **Warm block:** the benchmark database's files and the snapshot are read once instead | dropped before every run, during the settle |
| Old copy B (proxies only) | – | `cp -a --reflink=never B0 B; sync -f B` before the drop: cold and already durable |
| Output directory | – | absent |
| `vm.dirty_*` | Debian defaults, recorded | the same |
| Source cluster | running, under load. Calibration block: running and idle; stopped cleanly for v1 itself | – |

### What the clock includes

The clock is `perf_counter` on dest, as before. It times the refresh (`total_s`). The **slowdown window** of a run is separate: it runs from the start of its first timed phase to 10 s after the end of its last, mapped to the source's clock.

| Component | `none` | `full` | `capture` | `pgdelta` (staged) | `bracket` | `v1proxy`, `v1proxy_s1` |
|---|---|---|---|---|---|---|
| Timed phases (counter windows) | one: 60 s with nothing run | one: the copy | one: the capture | two, as EXP-04b: `receiver_capture`, then `transfer` | one | one, in four steps: `backup_start`, `receiver_start`, `transfer`, `backup_stop` |
| Backup mode and fast checkpoint | – | in | in | in (its capture's) | in | in: `pg_backup_start(label, true)` and `pg_backup_stop(false)` in one session held open over SSH |
| Reading the source | – | one walsender, live files, cold | the same, locally | the capture's, then S warm | – | 4 sender workers (or 1), the stopped snapshot, cold |
| Writing on the source | – | none | **S, 2.1 GB, flushed** | S, flushed | – | none |
| Hashing on the source | – | none | none | 4 workers over S | – | 4 workers (or 1) over the snapshot |
| WAL | – | streamed, the load's included | in S | in S, sent by pgdelta | – | none: a live v1 would stream what the load writes during its copy (not modelled) |
| Destination | – | a new directory, fsynced | – | a new O, fsync in the main loop | – | B patched in place |
| Out of the clock | the set-up, eviction and cache drops, verification, deleting outputs and S | | | | | |

Every arm, `none` included, reads the same counters at its phase boundaries, so their small cost is in the null too. `pgdelta`'s window includes the untimed gap of about 0.12 s between its two phases. `v1proxy*`'s window includes backup mode, as v1's will.

### The two O4 numbers, defined (added here; change 3)

From pgbench's per-transaction log, on the source's clock. Each run records the UTC start of its first timed phase and the end of its last, on dest, and maps them to the source's clock with the measured offset (gate: 5 ms, A8). Call them t0 and t1.

- **The window:** [t0, t1 + 10 s], in 1-s buckets from t0. Its length in buckets is T.
- **S_b and X_b:** the transactions scheduled in bucket b, and those completed in it. Using the schedule, not the nominal rate, removes the arrivals' own Poisson noise.
- **Worst drop, D:** the largest 1 − X_b / S_b over the window's buckets. A stall that covers one whole bucket gives 1.0.
- **Total work lost, W:** Σ_b max(0, S_b − X_b) / R, in seconds of the load's work. It is the area of the throughput dip. A stall of s seconds gives about s; the catch-up afterwards doesn't cancel it. An application with timeouts would lose that work; pgbench catches up.
- **The null (revised).** A block bootstrap from the block's load-only time. The pool: every `none` run's span from its settle's start to its tail's end (90 s), every settle whose gate passed, in full, and the last 60 s of the block's warm-up. Settles flagged *unsettled* are left out; the null with them is a robustness view. The seconds from a refresh's or a control's clock stop to the next settle are left out too, since the 15-s view counts them as the copy's. The pool's 1-s buckets are drawn in blocks of 10 consecutive buckets (or T, if shorter), with replacement, each block from inside one span, and joined to length T. W and D are computed as for a run. 10,000 resamples, from a fixed seed. Each statistic gets the null of the same statistic. For a cell's median over n runs, the cell's null is the median of n resampled windows, one at each run's own T (revised). The warm block shares the busy block's pool.
- **Net values:** D_net and W_net are the arm's median over its runs in the cell minus the median of the cell's null. A net value below 0 counts as 0.
- **Measurable (revised):** the arm's median over its n runs exceeds the 95th percentile of the cell's null. The same for D.
- **The bound (revised):** where an arm's W isn't measurable, its W_net is bounded by that 95th percentile minus the median of the cell's null. L3, L4, R2 and A6 use it.
- **Tolerances (revised):** for two cells a and b, take each resample of a's null minus the median of a's null, clipped at 0 as the net values are, and the same for b. The percentile is the 95th of a's centred value minus b's. δ_W = the largest of 0.05 s, that percentile for W, and 0.15 × W_net(`full`). δ_D = the larger of 0.03 and that percentile for D. "Measurable" and the bound use the same centring. The floors are fixed now, and the smoke run no longer raises them: δ follows the sweep's own null.
- **"Meets O4"** for an arm in a cell (block, speed): W_net(arm) − W_net(`full`) ≤ δ_W **and** D_net(arm) − D_net(`full`) ≤ δ_D. Otherwise it misses, on D, on W, or on both.

**Reported beside them, for every run:**
- p50, p95, p99, p99.9 and maximum latency of the transactions scheduled in [t0, t1], from their scheduled start; the counts above 10, 50 and 100 ms; and the same for the run's settle and for the null;
- the p99 of the window's worst 5-s span: the five consecutive 1-s buckets whose scheduled transactions have the highest p99. The latency caveat uses it (A9; revised);
- the late count: transactions scheduled in the window that finished more than L late, divided by R, with L fixed in the smoke run (item 3) as the larger of 10 ms and 10 × the p99 over 120 s of load at R_busy, rounded up to 5 ms;
- the backlog at the window's end: scheduled minus completed since t0;
- the backlog area: the sum of each transaction's latency above the settle's p50, in transaction-seconds.

**Robustness views** (each can make a verdict *fragile*, A9): maxima instead of medians; paired with the same repeat's `full` run at the same speed; D in 0.5-s and 2-s buckets; W as the late count at L and at 2L; a 15-s tail instead of 10 s; flagged runs left out (A8); the null with the unsettled settles put back (revised). Each view's "measurable" and δ use the null of its own statistic: for maxima, the maximum of n resampled windows, each at its own run's T.

### Source CPU

- **The registered rate's denominator is calibrated CPU-s** (change 6): VM-wide busy CPU on the source (`/proc/stat`: user, nice, system, irq, softirq; steal reported apart), summed over the run's timed phases in the idle calibration block, minus the idle background rate times the run's length. The median of the arm's two calibration runs. The calibration runs at 2 Gbit/s. EXP-04b's CPU-s at 0.5 and 2 Gbit/s differed by at most 3%, so the 2 Gbit/s value stands for both.
- **Under load, as a view:** the window's VM-wide busy CPU minus the settle's rate times the window's length.
- **The sampler** (added here) reads, on the source, every 0.1 s: per-process CPU of the walsenders, the checkpointer, the backends (pooled), pgbench and the refresh's own processes; the run queue (`procs_running`); steal; `/proc/diskstats`; and each pgbench thread's run-queue wait (`/proc/<pid>/task/<tid>/schedstat`, its second field; revised), so the wait is known per 1-s bucket. Every 1 s, over one held connection: `pg_stat_wal` (WAL write and sync counts and times), `pg_stat_checkpointer` and the number of autovacuum workers. It shows a mechanism. No verdict uses it; the "client wait" flag (A8) marks rows fragile and changes none. Its own cost is checked in the smoke run (item 11).

### Verification (G4; untimed, every run)

| Arm | Bytes | Started? | The load's oracle | A G4 output? |
|---|---|---|---|---|
| `full` | **(i)** `pg_verifybackup` with its CRC32C manifest, WAL parsed (G2) | **(ii)** yes: port 5433, no TCP, no archive, no `restore_command`; recovers from its `backup_label` to consistency with only the WAL it holds | **(iii)** to **(vii)** below | yes |
| `capture` | `pg_verifybackup` of S, on the source, before the next settle | no (S is an intermediate) | – | no |
| `pgdelta` | S passes `pg_verifybackup`; O byte-identical to S (SHA-256 on both VMs, S's half before the next settle); O passes `pg_verifybackup` with S's manifest | yes, as `full` | (iii) to (vii), against S's capture | yes |
| `v1proxy`, `v1proxy_s1` | EXP-09's check against the snapshot: B byte-identical to the snapshot's selection, against step 5's stored digest, so it never reads the source (revised); no in-place leftovers | yes, without recovery, then queried | – (it copies a stopped snapshot) | no |
| calibration: v1 itself | EXP-09's check against the stopped source | yes | – | no |
| every arm | B0 unchanged (stat per run, digest per trial); the snapshot unchanged (stat per run, digest at the trial's end) | – | – | – |

**The load's oracle, on every started live output** (added here; change 8):
- **(iii) Atomicity:** Σabalance = Σn in loaddb, and no balance carries a rolled-back change (G3: "aborted/uncommitted effects are absent").
- **(iv) A consistent cut**, checked at the trial's end from the logs: no committed transaction the copy lacks completed before a transaction it holds started, within 1 ms. If transaction B completed before transaction A started, B's commit record precedes A's, so a consistent copy can't hold A without B. This uses only the source's clock. It catches a lost committed transaction and one that shouldn't be there.
- **(v) Coverage:** the cut lies between the copy's backup start and its clock stop, within the recorded offset plus 50 ms. For `pgdelta`, between its capture's start and end. A copy of an old state fails it.
- **(vi) The benchmark database:** the row counts of its five tables equal the start reference's in every run; in each (arm, speed) cell's first repeat, also its per-table logical digest (about 28 s), against step 4's. It isn't written during the load, so this is an exact match.
- **(vii) Schema (revised):** the SHA-256 of `pg_dump --schema-only --restrict-key=exp12` of loaddb and of `postgres` equals step 4's reference (G3: "schema"). PostgreSQL 17's dump holds no statistics, so autovacuum's analyses don't change it. The fixed key and one minor version on both VMs make two dumps of one state equal; smoke item 5 checks it.
- **`pg_amcheck --heapallindexed`** on loaddb, every relation. loaddb holds the amcheck extension from step 1, so every copy has it (revised). Without it, `pg_amcheck` skips the database with a warning and checks nothing.

**What a result means.**
- **Wrong:** any check above fails, or the output doesn't start. Any error that the copy's own server or tools return counts as a failure (revised): a failed query, a corruption report or error from `pg_amcheck`, a `pg_verifybackup` failure. A wrong pgdelta output fires the registered "Any wrong output stops speed work" (R3). A wrong `full` output makes the oracle suspect (A1).
- **Unverified:** a check couldn't run for a reason on the harness's side, and only these (revised): SSH failed, a check timed out, or a binary was missing. Also `pg_amcheck` printing "skipping database" or checking no relation; check (vii) still says whether the copy holds the extension. The run leaves every median and is reported. More than 1 unverified run in a cell flags the cell.

**G4's boundaries, recorded per live output:** the capture's end (its "write-ahead log end point" LSN and the time `pg_basebackup` printed it), the coverage (the cut's time interval from the log), publication (the clock's stop) and usable (plus the start's time to consistency). **Staleness** is publication minus the cut. For staged pgdelta it includes the whole transfer.

### Durability on dest

The source's slowdown doesn't depend on dest's flushes, but time under load does. R1's "where it wins on time" at 2 Gbit/s compares `pgdelta` (E6: 8.3–11.5 s) with `full` (10.3–11.3 s). So EXP-11's standard applies in full (revised):
- **(a)** `Dirty + Writeback` in dest's `/proc/meminfo`, read just before the clock starts and first after it stops, before any verification step, may grow by at most 4 MiB, or the smoke run's idle noise, whichever is larger. Every run that writes on dest.
- **(b)** Dest's md array writes (`/proc/diskstats`) between the same two reads are at least 0.98 × the output's bytes, for `full` and for `pgdelta`'s new O. The harness applies (b) only to `FULL_COPY_METHODS`, which lacks `pgdelta`, so (b) gets a set of its own (What it needs built; revised). On ext4, pgdelta copies B0's unchanged blocks into O with `copy_file_range`, which writes them, so (b) holds for a durable O.
- **(c)** A stubbed harness test asserts that each arm's flushing step runs inside its timed phase, and that dest's verification starts only after the last read of (a) and (b). `verify()` maps the output's extents with `FIEMAP_FLAG_SYNC`, which writes the output back, so a check read after it could pass a copy that skipped its flush.
- **Negative control:** `full_nosync`, once at scale 70 at 2 Gbit/s in the smoke run (item 13), must fail (a) by at least 1.5 GB. If it doesn't, check (a) is void for the session, and durability rests on (b) and the audit.
- **The syscall audit** (`--syscall-audit`, EXP-11's script) runs once per arm that writes on dest, at scale 5, in the smoke run: `full`, `pgdelta` (whose new copy EXP-11 didn't audit) and `v1proxy`. An arm that fails is fixed before the main sweep if the fault is harness code. If `pg_basebackup` or pgdelta fails, its arm gets a timed `sync -f` of its output inside its phase, recorded here, dated, under rule 8 (EXP-11's smoke item 3).
- A run that fails (a) or (b) leaves every median and is reported. More than 1 such run of an arm at one speed in one block reclassifies the arm (A13).

## Arms

| Arm | Tier | What runs | Reads | Blocks and speeds |
|---|---|---|---|---|
| `none` | control (registered: "no backup at all") | nothing: an empty timed phase of 60 s, with the same counter reads as every arm | – | busy and moderate: once per repeat (2 Gbit/s visit) |
| `full` | O4's bar; Omni today | EXP-11's `full`: `pg_basebackup -Fp -X stream -c fast --manifest-checksums CRC32C --verbose`, one stream, on dest | the live source, cold | busy: 0.5, 2, U; warm: 2; moderate: 2, U |
| `capture` | registered | the staged capture alone: a local `pg_basebackup` of S on the source (CRC32C, `-c fast`, `--verbose`, stamped), flushed | the live source, cold | busy: once per repeat (link-free) |
| `pgdelta` | registered (staged v0) | EXP-04b's arm: the capture with the receiver started beside it (prefetch on); then the transfer, 4 jobs on each end, into a new O | the live source, cold; then S, warm | busy: 0.5, 2, U |
| `bracket` (added) | backup mode alone | `pg_backup_start('t10a', true)` then `pg_backup_stop(false)` in one session | – | busy: once per repeat (link-free) |
| `v1proxy` (added) | v1's source side, by proxy | backup mode held on the live source around EXP-09's `pgdelta_direct_inplace` (4 jobs on each end) reading the snapshot | the stopped snapshot, on the live source's disks, cold | busy: 0.5, 2, U; warm: 2; moderate: 2, U |
| `v1proxy_s1` (added) | v1 with one source core | as `v1proxy`, with the sender at `--jobs 1` and the receiver at `--jobs 4` | the same | busy: 0.5, 2; moderate: 2 |
| `pause2` (added) | positive control | SIGSTOP to pgbench for 2.0 s, then SIGCONT (`agent.py load-pause 2.0`, which records both signals' times on the source's clock) | – | at the busy block's start and end, and once at the moderate block's start |
| `burn4` (added) | CPU reference | 4 busy processes at nice 0 for 2.0 s (`agent.py burn`) | – | at the busy block's start and end |

**Why `full` is the only full copy.** O4's bar is `pg_basebackup`. It is also the fastest copy that can read a live source today: EXP-11's bound, `parcopy`, reads a stopped source, and `pg_probackup catchup` took 5.8–6.1 s against one stream's 3.8–4.1 s.

**The proxy (added here).** v1's source side does four things: backup mode with a fast checkpoint; a cold read of every selected file; hashing every page on 4 worker processes; sending the changed pages. `v1proxy` does all four on the live source's machine, under the load, in that order and overlapping as v1 would. Only the files differ: it reads the snapshot, a stopped copy of the data directory on the same disks, instead of the live files. It runs v1's own code path, the direct read with PostgreSQL's base-backup exclusions, and needs no change to pgdelta: its datadir mode refuses a directory with a `postmaster.pid` (`pgdelta/datadir.py`, `_refuse_running`), and the snapshot has none. What the proxy leaves out:
- streaming the WAL the load writes during the copy (a few MB per second here, plus about 30 MB of full-page images after the checkpoint);
- reading files while the server writes them;
- loaddb's changes, about 30 MB more for a live v1 to send.

Its output is a correct copy of the snapshot. It says nothing about G4. A gate checks that it did v1's work (A4).

**The one-core source (added here).** If v1 at 4 source workers misses the bar, the plan needs to know whether a cap fixes it, and what the cap costs in time. One core is the extreme point: if it misses too, no worker count will do. It also stands for a sidecar with a one-CPU limit (O3), though not for today's pgdelta under such a limit, whose pool ignores the quota (a note below).

**The controls (added here).** `none` gives most of the null: what a window with no refresh looks like. `pause2` has a known answer (E3), so it checks the logging and the arithmetic end to end, as `full_nosync` checked durability in EXP-11. Its gap in the log is also matched with the agent's signal times and with dest's phase mapped through the offset, which checks the clock mapping (A2; revised). Two seconds, so that a whole 1-s bucket always lies inside the pause. `burn4` asks whether 4 busy processes, the size of pgdelta's pool, cost the load anything when nothing else changes.

**The warm block (added here; revised).** `full` and `v1proxy` at 2 Gbit/s, 5 repeats, with the benchmark database's files and the snapshot read into the cache before each settle. A live primary of 2.1 GB would hold its data in 32 GB of RAM, and EXP-11 found a warm source changed one stream's pace in append. It shares the busy block's load, controls and null. Warm, `v1proxy` hashes without reading the disk, so it separates the hashing from the cold read, its I/O and its kernel CPU: if the proxy misses, the warm block names the cause (decision 1, row 7). `v1proxy`'s own work doesn't depend on the link (E6), so its warm reading at 2 Gbit/s stands for that work at both registered speeds. It doesn't stand for the bar: `full` at 0.5 Gbit/s is not `full` at 2. So row 7b also compares warm `v1proxy` with its own cold runs at 2 Gbit/s (revised). It is never cut.

**The calibration block (added here).** After the load stops: `full`, `capture`, `pgdelta`, `v1proxy` and `v1proxy_s1` at 2 Gbit/s, `burn4`, and **v1 itself** (`pgdelta_direct_inplace`: the source stopped cleanly, the benchmark database's files evicted, restarted after), 2 runs each in a seeded order, with the same cache state as the busy block. It gives each arm's source CPU-s and its time without load. Every output is verified as in the main blocks.

### Considered and not run

| Option | Why not |
|---|---|
| HammerDB TPROC-C | Its throughput counter reports about every 10 s, at an irregular cadence (`benchmarks/workloads/hammerdb/README.md`), against refreshes of 0.2–42 s, and its latency has no time axis. A fixed rate needs keying and thinking times, so thousands of virtual users. It needs its own build and schema. T10b uses it |
| pgbench's built-in transaction on the benchmark tables | At 1,000 transactions per second it touches about 40% of `pgbench_accounts`' pages in a minute, so the 1% pattern would be lost within the first runs; `pgbench_history` also grows |
| rsync, staged (registered) | O7 rules rsync out as the engine; Omni's images don't ship it. Its source work is the capture plus one hashing process, which `capture` and `v1proxy_s1` cover more cleanly. T6 keeps it as a baseline (change 1) |
| v1's hashing without backup mode, from a fresh S | `v1proxy`'s own step timers and `bracket` split backup mode from the hashing |
| `pg_probackup catchup` | Neither O4's bar nor faster than one stream (EXP-11), and not in Omni. No queued decision turns on its slowdown |
| `parcopy` (EXP-11's bound) | It reads a stopped source; its backup-mode version is T8's. At 2 Gbit/s it is rule 3's fastest copy (EXP-11: 9.32 s against `full`'s 10.30), so that copy has no slowdown number until T8. This is stated with every result |
| `full_syncfs`, `full_wb`; `full_nosync` in the sweep | They differ from `full` only on dest. The source does the same work. `full_nosync` runs once in the smoke run, as check (a)'s negative control |
| A paced read-only arm, to split the cold read from hashing | It runs no v1 code; the warm block's `v1proxy` splits them with v1's own code path |
| The client pinned to its own CPUs | It would take CPUs from the backends and the copies and change the load; the run-queue reads measure the client's wait instead |
| `pg_basebackup -c spread` | `pg_basebackup`'s default, but Omni's flags are unknown. Every arm here takes a fast checkpoint, as v1 would by default |
| TLS | O6 requires it for pgdelta; T12 measures it. Encrypting 2.1 GB would cost the full copy more source CPU than v1's 28 MB, so sending in clear, as both do here, favours `full` slightly |
| v1 at 8 jobs; a CPU quota on 4 workers | T10b's question |
| Uniform and append trials | Each arm's source work doesn't depend on the change pattern, except v1's send (610 MB instead of 28 MB for uniform). A second load level decides more (change 9) |
| A load at 0.8 × C | Near saturation the load's own null widens, and A3 would likely fire. T10b varies the load with HammerDB |
| Dropping the whole source cache | It would evict the live load and slow it by itself |
| Recovering every output to one shared target with archived WAL | Each output would replay most of the trial's WAL. The checks (iii) to (v) check each copy exactly at its own end point |
| A reset of the source before each run | Not needed: loaddb's size is constant, and the benchmark database isn't written |
| A third VM for the client | At most two VMs |

## Predictions and decision rules

Every prediction and rule is judged on the busy block's medians of 5 runs per cell, with the maximum beside it (rule 6), unless it says otherwise. The moderate block is reported beside it. So is the warm block, except that it names the cause of a proxy's miss (decision 1, row 7).

**Short cells (revised).** Wrong, unverified and durability-failed runs leave a cell's median. A cell with fewer than 3 runs left isn't judged, by any rule. Every rule that needs it takes its "not judged" path at that speed. A short `pgdelta` or `full` cell leaves R1, R2, L3 and L4 not judged there. A short `v1proxy` or `full` cell leaves the proxy not judged there (decision 1, row 4). A short `v1proxy_s1` cell gives "Cap not judged". A judged miss at the other speed still counts.

### The ledger's prediction and rules (registered)

The ledger's text, word for word:

> **Prediction:** the slowdown tracks source CPU. pgdelta uses about 4 times the full copy's source CPU, so its total throughput loss is 2-4 times the full copy's at 2 Gbit/s, and still higher at 0.5 Gbit/s. Every output is correct.

> **Proves it wrong:** if pgdelta's loss is no higher than the full copy's where it wins on time, source CPU isn't a blocker. If it is higher, report slowdown next to time and move T5 ahead of T8. Any wrong output stops speed work.

> *Added October 24: before T8, T10a measures staged pgdelta only, whose capture v1 removes. So judge it as slowdown per source CPU-second. "Tracks" means that rate is within 1.5 times between the full copy and staged pgdelta. If it is, project v1's loss from its 5.3-6.0 CPU-s (EXP-09): that gives 2.8-3.75 times the full copy's loss, and T5 moves ahead of T8 if the projection exceeds 2 times, as it would. If it isn't, the T5/T8 order waits for v1's own arm. v1's own arm, reading a live source in backup mode, is added once T8 exists.*

**How each part is judged here.** "Loss" is W_net. "pgdelta" is staged `pgdelta`. "The rate" is W_net over calibrated CPU-s. "Throughput" is O4's two numbers; p95 latency is reported. The registered speeds are 0.5 and 2 Gbit/s; unthrottled is reported beside.

| ID | Registered text | How it is judged here | Expected here (added) |
|---|---|---|---|
| L1 | "the slowdown tracks source CPU" | By R2 below | doesn't track (or not judged) |
| L2 | "pgdelta uses about 4 times the full copy's source CPU" | Calibrated CPU-s of `pgdelta` over `full`'s, within [3, 5]. Under-load CPU at 0.5 and 2 Gbit/s beside | holds: about 4 |
| L3 | "so its total throughput loss is 2-4 times the full copy's at 2 Gbit/s" | W_net(`pgdelta`) / W_net(`full`) within [2, 4] at 2 Gbit/s, where both W's are measurable. **Revised:** if only `pgdelta`'s is, the ratio is at least W_net(`pgdelta`) over `full`'s bound (The two O4 numbers): "failed high" if that is above 4, not judged otherwise. If only `full`'s is, the ratio is at most `pgdelta`'s bound over W_net(`full`): "failed low" if that is below 2, not judged otherwise. Neither: not judged | failed high (`full` not measurable; the ratio's lower bound above 4) |
| L4 | "and still higher at 0.5 Gbit/s" | The ratio at 0.5 Gbit/s exceeds the ratio at 2, judged where both are defined. **Revised:** if `full`'s W is measurable at 2 but not at 0.5, and `pgdelta`'s at both, it holds if the ratio's lower bound at 0.5 (as in L3) exceeds the ratio at 2, and is not judged otherwise | not judged |
| L5 | "Every output is correct." | Every run of every arm is verified, none wrong; G4's checks for `full` and `pgdelta` | holds |

| Rule | How it is judged here |
|---|---|
| **R1** "if pgdelta's loss is no higher than the full copy's where it wins on time, source CPU isn't a blocker. If it is higher, report slowdown next to time and move T5 ahead of T8." | "Where it wins on time": the speeds where `pgdelta`'s median `total_s` **under load** is below `full`'s. A speed where `pgdelta` is reclassified as not durable doesn't count; one where `full` is counts as a win (A13). "No higher": W_net(`pgdelta`) − W_net(`full`) ≤ δ_W at each of them. If no speed qualifies: not judged. Slowdown is reported next to time in every case (A7). R1's verdict is recorded as "staged pgdelta: source CPU is (or isn't) a blocker". **Its T5 consequence applies as registered (revised; change 4):** if the loss is higher at a speed where it wins on time, T5 moves ahead of T8, whatever R2 says. Where R1 and the October 24 note conflict, R1 governs. A5 says beside it where staged pgdelta's loss comes from |
| **R2** the October 24 note | **Per cell** (0.5 and 2 Gbit/s): if both W's are measurable, the rate ratio is (W_net(`pgdelta`) / CPU-s(`pgdelta`)) / (W_net(`full`) / CPU-s(`full`)), and the cell tracks if it lies in [1/1.5, 1.5]. If exactly one W is measurable, the other's W_net is bounded (The two O4 numbers): the cell doesn't track if that bound already puts the ratio outside [1/1.5, 1.5], and is not judged otherwise. If neither is measurable: not judged. **Overall:** *tracks* if both cells are judged and track; *doesn't track* if either judged cell doesn't; *not judged* otherwise. **Consequences, as registered:** tracks → v1's loss projected as W_net(`full`) × 5.3–6.0 / 1.6–1.9 (EXP-09), that is 2.8–3.75 times `full`'s, so T5 moves ahead of T8; the same projection with this session's calibrated CPU-s of v1 itself and of `full` is reported beside (change 6). Doesn't track, or not judged → "the T5/T8 order waits for v1's own arm", unless R1 has moved T5 (change 4); where neither has, the added rows below take it up (change 5) |
| **R3** "Any wrong output stops speed work." | One wrong output of `pgdelta`, `v1proxy`, `v1proxy_s1` or v1 itself fires it. The session still runs to its end (A11); the ledger's speed work (T12, T9, T5, T4's timing) waits until the cause is known. Unverified runs don't fire it. A wrong `full` output is A1's |

### The plan (added here)

**Decision 1: the T5/T8 order (revised).** Checked in this order, on the busy block at 0.5 and 2 Gbit/s:

| Row | Condition | What follows |
|---|---|---|
| 1. Stop | R3 fires | Speed work stops until the cause is known (registered) |
| 2. R1 | R1 fires: staged pgdelta's loss is higher where it wins on time | As registered: slowdown is reported next to time, and T5 moves ahead of T8 (change 4). R2 and rows 5–7 are reported beside it, row 7 with its cause. If they disagree, both are stated, and this row stands unless the ledger records an override under rule 8 |
| 3. R2 | R1 doesn't fire, and R2 judges "tracks" | As registered: T5 moves ahead of T8. Rows 5–7 are reported beside it, as in row 2 |
| 4. Proxy not judged | Neither R1 nor R2 moves T5, and the busy block's proxy can't be judged: A2 or A3 fires, A4 fires on `v1proxy`, or the trial's clock gate fires; or a `v1proxy` or `full` cell at 0.5 or 2 Gbit/s is short (fewer than 3 runs) and `v1proxy` misses in no judged cell (revised) | As registered: the order waits for v1's own arm. T5 stays behind T8 |
| 5. No measurable slowdown | Neither `full`'s nor `v1proxy`'s W or D is measurable, at 0.5 Gbit/s or at 2 | T5 stays behind T8. "At this load neither copy's slowdown is measurable." D04's gap closes for this load; D81 gets no basis from it |
| 6. v1 meets | `v1proxy` meets O4 at 0.5 and 2 Gbit/s | "By proxy, v1's source side meets O4's bar at this load." T5 stays behind T8, and T8 confirms with v1's own arm |
| 7. v1 misses | `v1proxy` misses at 0.5 or 2 Gbit/s, in a judged cell. The warm block names the cause, checked in this order (revised): | Decision 2 says what else, in every case |
| 7a. Hashing | warm `v1proxy` also misses against warm `full` | T5 moves ahead of T8: a faster hash per core shortens the hashing that slows the load |
| 7b. The cold read | only on positive evidence (revised): warm `v1proxy` meets O4 against warm `full`, **and** on each number it missed cold (D, W or both), its warm net value is below its own cold net value at 2 Gbit/s by more than δ (Tolerances, for those two cells) | "The cold read slows the load: its I/O and its kernel CPU." T5 keeps its place. The consequence is a read-rate limit on v1's sender (D81), which T10b tests |
| 7c. Cause not shown | every other case: the warm reading can't be judged (A4 on the warm runs, or a warm cell short of 3 runs), or warm `v1proxy` meets without falling below its cold value by more than δ (revised) | T5 moves ahead of T8, as for 7a, with "cause not shown"; a read-rate limit is listed beside it |

**Decision 2: a cap on v1's source workers.** Judged whenever the proxy is, whichever row decides decision 1. Checked in this order, and the first row that holds applies, so a judged miss counts first (revised):

| Row | Condition | What follows |
|---|---|---|
| No cap | `v1proxy` meets at 0.5 and 2 Gbit/s, or row 5's condition holds | No cap is needed at 4 source workers at this load. T10b keeps its place |
| Cap fails | `v1proxy` misses at 0.5 or 2 Gbit/s, and `v1proxy_s1` misses too, in a cell where it can be judged (revised) | One source worker slows a busy primary more than `pg_basebackup` too. v1 needs a rate limit on its source work (D81) before T8's speed claims: on hashing under row 7a, on reading under 7b, on both under 7c. T8's correctness work isn't blocked |
| Cap meets | `v1proxy` misses at 0.5 or 2 Gbit/s; `v1proxy_s1` meets at both | v1 at 4 source workers slows a busy primary more than `pg_basebackup`; one source core doesn't. v1 needs a source worker cap on busy primaries. **T10b moves ahead of T8**, as "source workers by load", and D50 and D81 get a basis. A design note: pgdelta's source pool should size itself from its CPU quota |
| Cap not judged (revised) | `v1proxy` misses at 0.5 or 2 Gbit/s, and `v1proxy_s1` can't be judged in a cell: A4 on its runs, or the cell short of 3 runs | v1 at 4 source workers slows a busy primary more than `pg_basebackup`; whether a cap fixes it isn't known. **T10b moves ahead of T8**, as in "Cap meets", to find out |

**Beside the decisions:**

| Row | Condition | What follows |
|---|---|---|
| O4 for staged pgdelta | R1's verdict | Recorded on D04 and D10p, with A5's reading of where the loss comes from |
| HA caveat | `v1proxy` misses O4 unthrottled, busy block | EXP-11's qualifier gets a caveat: "and it slows a busy primary more than `pg_basebackup` does", with both numbers |
| Load | The moderate block's rows differ from the busy block's | Both are stated; the busy block decides. For example: "at 30% of capacity, v1 at 4 source workers meets the bar; at 60% it doesn't". A cap then depends on the load, which is T10b's question |
| Warm source | The warm block's comparison of `v1proxy` with `full` at 2 Gbit/s gives another verdict than the busy block's | Reported beside; the cold reading decides, and the warm one names a miss's cause (row 7) |

Flags beside every row: *fragile* and *near the line* (A9), *latency caveat* (A9), and the cell flags of A8, *client wait* among them.

**The order of judgment:** G4 and R3; A1; A13; the instrument and the load (A2, A3, A8's clock gate); L1–L5; R1; R2; decision 1, with A4 and the warm reading inside it; decision 2; A5, A6; the rows beside; the flags. Each verdict names its cells, its medians and maxima, and its flags. Every outcome has a row: "not measurable" on either side and "not judged" included.

### Added predictions

| ID | Prediction | Basis |
|---|---|---|
| E1 | C is 10,000–40,000 transactions per second. At R_busy the source's VM-wide busy CPU is 40–75%; at R_mod, 20–45%. Above 50% at R_busy is the likelier side: if C is CPU-bound, 0.6 × C uses about 55–65% | Weak: these scripts never ran on these VMs. Three cached single-row HOT updates and a synchronous commit per transaction, 32 clients, the client on the same 8 vCPUs |
| E2 | The null: `none`'s completed over scheduled is 0.995–1.005 per window. For one window, W's null median is at most 0.02 s and its 95th percentile at most 0.05 s, for T up to 50 s; D's null median is 0.005–0.03 at T = 15. For a median of 5 windows, W's 95th percentile is at most 0.03 s, so δ_W is the 0.05 s floor in every comparison (revised). The settle gate passes within 30 s in at least 95% of runs | With the schedule as reference, only bucket-edge shifts of millisecond latencies remain. A median of 5 narrows the spread |
| E3 | `pause2`: W 1.6–2.0 s at ρ = R/C = 0.6 (1.3–2.0 at 0.3); D at least 0.95; the late count at L about 2 / (1 − ρ): 4–6 s at 0.6. The log's gap matches the agent's signal times within 5 ms plus one latency, and lies inside dest's mapped phase (revised) | Every transaction due in the pause is late, and no completion is logged in it, so one whole bucket has none. After it, the backlog drains at the spare rate; in 1-s buckets that drain can offset up to (1 − ρ) s of the deficit in the bucket where the pause ends |
| E4 | `burn4` (revised: by the busy CPU at R_busy, smoke item 2). **At 50% or less:** neither W nor D measurable; window p99 1.5–10 times the settle's. **Above 50%:** W_net at most 0.2 s and D_net at most 0.15, likely measurable; window p99 2–30 times the settle's | At 50% or less, 4 busy processes fit in the spare CPU. Above, they oversubscribe the VM. Linux's scheduler favours waking threads, and the load's backends sleep between requests, so the busy processes give way first, but each wake-up can wait a few milliseconds behind them |
| E5 | Calibrated CPU-s per run (idle, cold, VM-wide): `full` 2.2–3.0; `capture` 3.5–6.0; `pgdelta` 8.5–11.5; `v1proxy` 5.5–7.5; `v1proxy_s1` 5.0–7.0; v1 itself 5.5–7.5, within 20% of `v1proxy`; `burn4` 7.9–8.5. So v1 over `full` is 1.8–3.4 and `pgdelta` over `full` 2.8–5.2, most likely near 2.5 and 4 | EXP-11: `full` 2.3–2.7, v1 6.1–7.2; EXP-09: v1 5.3–6.0, staged 8.2–8.9; EXP-04b: staged 9.74–10.81, of which its transfer 4.9–6.4 (D04), so the capture is about 3.5–6; D48: 4 processes use 1.06 times the CPU of 1 |
| E6 | Times under load: `full` 38–42 s at 0.5 Gbit/s, 10.3–11.3 at 2, 3.8–5.0 unthrottled; `capture` 4.0–5.5; `pgdelta` 8.3–11.5 at every speed; `v1proxy` 3.2–4.5 at every speed; `v1proxy_s1` 4.5–6.5; `bracket` 0.1–0.6. Against the same arm's idle calibration at 2 Gbit/s: `full` within ±5%, the others 0–35% longer | EXP-04b: `full` 36.5 and 10.3 s, staged 8.3–8.6 s; EXP-11: `full` 3.8 s and v1 3.08–3.11 s unthrottled. Plus the WAL streamed during the copy (a few MB per second, plus about 30 MB of full-page images after its checkpoint), backup mode under load, and CPU shared with the load. One core hashes 2.1 GB in about 3.9 s (0.54–0.55 GB/s, D15) |
| E7 | Busy block, net medians (revised: `v1proxy*` by the busy CPU at R_busy): `full` W_net ≤ 0.05 s and D_net ≤ 0.03 at 0.5 and 2 Gbit/s, most likely not measurable; unthrottled W_net 0.02–0.3 s and D_net 0.02–0.15; `bracket` W_net ≤ 0.1 s, D_net ≤ 0.10; `capture` W_net 0.2–3 s, D_net at least 0.3; `pgdelta` W_net 0.3–3.5 s, D_net at least 0.3, at every speed. **At 50% busy or less:** `v1proxy` W_net 0.01–0.10 s and D_net 0.01–0.06 at every speed, with window p99 3–20 times the null's; `v1proxy_s1` not measurable. **Above 50%:** `v1proxy` W_net 0.02–0.30 s and D_net 0.02–0.15, with window p99 3–30 times the null's; `v1proxy_s1` W_net ≤ 0.05 s and D_net ≤ 0.03, likely not measurable | One walsender paced by a slow link uses a fraction of a vCPU and reads at the link's rate. The capture writes 2.1 GB on the WAL's array and flushes it at its end (about 1.3 s at 1.6 GB/s, EXP-11's write floor), and on ext4 in ordered mode a WAL fsync can wait behind that flush. v1 hashes on 4 processes for about 1.5 s and reads at about 1.5 GB/s from the WAL's array. At 50% busy or less, 4 workers fit in the spare CPU; above, they oversubscribe it, and the scheduler's preference for waking backends limits, but doesn't remove, the load's wait. One worker fits on either side |
| E8 | The registered verdicts: L1 doesn't track; L2 holds; L3 fails high (`full` not measurable; the lower bound above 4); L4 not judged; L5 holds. R1: staged pgdelta's loss is higher where it wins on time (0.5 Gbit/s, and 2 Gbit/s if it wins there), so **T5 moves ahead of T8, as registered** (revised; change 4). R2: doesn't track, since the bound on `full`'s W already puts the ratio above 1.5 | E5–E7: staged pgdelta's loss is mostly its capture's flush, which `full` doesn't have and v1 removes. E6's times at 2 Gbit/s overlap, so whether pgdelta wins there is open |
| E9 | **Expected rows (revised): decision 1, row 2, "R1": T5 moves ahead of T8.** Beside it, the proxy, by the busy CPU at R_busy. **At 50% or less:** "v1 meets", near the line, with a latency caveat; decision 2, "No cap". **Above 50%, the likelier side (E1):** "v1 misses" on D, with the warm reading "hashing" (row 7a); decision 2, "Cap meets". The HA caveat doesn't fire at 50% or less and may fire above. The moderate block meets. The warm block gives the same proxy row | E1, E7, E8, E15 |
| E10 | Every output passes. `full`'s cut lies within 0.3 s before its "write-ahead log end point" stamp; staged pgdelta's staleness at publication is 4–8 s, its transfer | A copy recovers to its end of backup; staged pgdelta's transfer runs after S is final |
| E11 | At least half of `pgdelta`'s W comes from its capture (A5 fires: "most is its capture") | The capture's flush; the transfer's hashing is shorter and writes nothing on the source |
| E12 | Wire bytes: `full` 2.15–2.45 GB at 0.5 Gbit/s; `v1proxy` 27.6–29.2 MB; `pgdelta` 60–130 MB | EXP-11's bytes; the snapshot's delta is the workload's alone. S also holds loaddb's changed pages (about 30 MB) and the WAL segments of its capture (3–4 of 16 MB under load), which differ from B0's in full |
| E13 | The load holds steady: loaddb grows by at most 5% over the trial; no pgbench failures; no checkpoint inside a window except an arm's own; no autovacuum on loaddb | HOT updates with 10% free space; locks in one order; `checkpoint_timeout` 30 min and `max_wal_size` 16 GB; autovacuum off for loaddb |
| E14 | A6 (revised: by the busy CPU at R_busy). **At 50% or less:** not judged (`v1proxy`'s W not measurable at both speeds), or "cores". **Above 50%:** "cores", or "CPU-seconds is an adequate unit" where `v1proxy`'s W_net is near the low end of E7's range, so that `v1proxy_s1`'s bound per CPU-s is more than half of `v1proxy`'s W_net per CPU-s | E2, E4, E7 |
| E15 | Warm `v1proxy` (revised): W_net at most its cold W_net at 2 Gbit/s plus 0.02 s, and D_net within ±0.03 of its cold D_net, so neither falls below the cold value by more than δ. If row 7 applies, the warm reading is "hashing" (7a) | EXP-11's warm-source cells (unthrottled, hot and append): v1 at 4 jobs used 5.28–5.36 source CPU-s warm against 6.22–6.27 cold, about 15% less, in about the same time (3.07–3.21 s against 3.08–3.13 s). So the warm reading removes the cold read's I/O and its kernel CPU, and the hashing stays, over the same span. On local NVMe in RAID 0, a 1.5 GB/s read should delay a WAL fsync little. Above 50% busy, 4 hashing workers still oversubscribe the VM |
| E16 | "Client wait" (revised) flags no cell at 50% busy or less. Above, it may flag `burn4`'s and `v1proxy`'s cells, on W or, through the worst bucket, on D | pgbench's 4 threads wait behind busy processes only when the VM is oversubscribed. The flag is judged only where W or D is measurable |

**Written down now, before running (revised):** the likely registered outcome is that R1 fires and R2 doesn't track. Staged pgdelta's loss is higher than `full`'s where it wins on time, and its capture's flush gives it a much higher loss per CPU-second, for a reason unrelated to CPU. The two registered texts then conflict: R1 moves T5 ahead of T8, and the note leaves the order to v1's own arm. As the lead decided (change 4), R1 governs, so T5 moves ahead of T8, although v1 has no capture. The proxy then decides the cap (decision 2) and the HA caveat, and is reported beside decision 1 with its cause. If `full`'s loss isn't measurable, O4's bar for v1 becomes "no measurable loss, within δ". Where the proxy lands depends on the busy CPU at R_busy, as in E9 (revised). At 50% or less, it is expected near that line. Above 50%, the likelier case (E1), it likely misses on D, with "hashing" as the cause.

### Added rules

| ID | Condition | Consequence |
|---|---|---|
| A1 | **The oracle.** A wrong `full` output; or, in the smoke run, a negative control that passes or a good output that fails any check (item 5; revised) | A wrong `full` output voids G4's checks for the session until it is explained, and the write-up says so; it stops nothing else. A negative control that passes, or a good output that fails, blocks the main sweep until the oracle or the harness is fixed |
| A2 | **The instrument (revised).** In a block, a `pause2` run's W is outside [1.8 − (1 − ρ), 2.3] s, or its D is below 0.9; or its gap in the log (from the last completion before the pause to the first after it) differs from the agent's SIGSTOP and SIGCONT times by more than 5 ms plus one latency (the settle's p99) at either end, or doesn't lie inside dest's phase mapped through the offset, within the same margin | That block's loss verdicts (L1, L3, L4, R1, R2, decisions 1 and 2, A5, A6) are "not judged: instrument". Times, CPU and G4 stand. The cause is found before any later session |
| A3 | **Overload.** In a block, `none`'s median completed over scheduled is below 0.98, or a `none` window ends with a backlog above R × 1 s | The block was overloaded: its rate wasn't sustainable without any copy. Its O4 rows are not judged. Nothing is rerun at another rate in this session |
| A4 | **The proxy's gate (revised).** For `v1proxy` and `v1proxy_s1` alike: (i) its sender didn't hash every byte of the snapshot's selection (pgdelta's own counters); or (ii) backup mode wasn't held across the transfer: no start LSN before the sender's first read and stop LSN after both ends exit, in one session; or (iii) its calibrated CPU-s differs by more than 20% from its reference: v1's own (`pgdelta_direct_inplace`) for `v1proxy`, and `v1proxy`'s divided by 1.06 for `v1proxy_s1` (D48: 4 processes use 1.06 times the CPU of 1) | If `v1proxy` fails it, the proxy is excluded: decision 1 takes row 4 where it would need the proxy, and decision 2 isn't judged. If only `v1proxy_s1` fails it, decision 2 takes "Cap not judged" where it would need it. The warm runs are gated by (i) and (ii) |
| A5 | **Where staged pgdelta's loss comes from.** W_net(`capture`) is at least half of W_net(`pgdelta`) in every cell where `pgdelta`'s W is measurable | "Most of staged pgdelta's slowdown is its capture, which v1 removes", recorded on D10p. Otherwise: "most is its transfer". It decides no queue position |
| A6 | **Cores or CPU-seconds.** `v1proxy_s1`'s W_net per calibrated CPU-s, and `burn4`'s, each at most half of `v1proxy`'s, at 0.5 and 2 Gbit/s. Judged only where `v1proxy`'s W is measurable; an unmeasurable W counts at its bound | "Loss follows how many cores run at once, not CPU-seconds": D81's budget is stated in cores, and T10b's question sharpens. Otherwise: "CPU-seconds is an adequate unit at this load". Not judged: D81 stays D |
| A7 | **Time next to slowdown (rule 3).** Always | Every arm's median `total_s` under load is reported beside its W, D and p95, and beside its idle time from the calibration block. R1's "where it wins on time" uses the times under load |
| A8 | **Flags.** Per run: *unsettled* (the source's settle gate, below, didn't pass within 60 s); *checkpoint* (one the arm didn't ask for started between the settle's start and the tail's end); *steal* (above 0.02 CPU-s per s in the window); *cache* (before a cold window, more than 1% of the benchmark database's files or the snapshot resident, or less than 99% of loaddb; before a warm one, less than 95% of them); *client* (a pgbench thread above 90% of a core in the settle); *client wait* (revised), on W or on D: on W, the pgbench threads' run-queue wait in the window, minus the settle's rate times the window's length, averaged over the threads, is at least 25% of the run's W, both in seconds, judged in cells where the arm's W is measurable; on D, in the window's worst bucket, the threads' wait minus the settle's rate times 1 s, averaged over the threads, is at least 25% of that bucket's lost time, (S_b − X_b) / R, judged in cells where the arm's D is measurable; *autovacuum* (a worker on any database in the window). Per trial: the clock offset above 5 ms at its start or end | Flagged runs stay in the medians; the "clean" view leaves them out (A9). More than 1 flagged run in a cell flags the cell. A decision row that rests on a cell flagged *client wait* is marked fragile (A9); the row stands. The clock flag makes the trial's loss verdicts "not judged: clock". If the load dies, the runs after it are void; the harness restarts it, with each client's `n` recorded and 120 s of warm-up, and goes on, recorded |
| A9 | **Fragile, near the line, latency caveat** | *Fragile:* one of the robustness views (The two O4 numbers) would put a decision in another row, or R2 in another verdict; or the row rests on a cell flagged *client wait* (revised). *Near the line:* a deciding difference within ±0.25 × δ of its δ, or a rate ratio within ±0.15 of 1.5 or 1/1.5. *Latency caveat (revised):* an arm meets O4, but the p99 of its worst 5-s span is more than twice `full`'s at the same speed (medians over the cell's runs). The counts above 50 and 100 ms are reported beside. Each is flagged beside the row; the row stands |
| A10 | **Drift against EXP-11** (reported only). `full`'s idle time at 2 Gbit/s outside ±5% of 10.27–10.32 s; v1's calibrated CPU-s outside ±15% of 6.1–7.2; the single-stream link test at 2 Gbit/s outside ±2% of 1.917 Gbit/s | The session is marked shifted. Every comparison here is within the session anyway. The registered projection uses EXP-09's numbers as registered |
| A11 | **No fix and rerun.** Once the main sweep starts | Nothing is fixed and rerun in this session. An arm that fails is left out and recorded under rule 8 |
| A12 | **Capacity drift.** The trial's own C (step 7) differs from the smoke run's by more than 10% | Flagged. The trial keeps the rates set before the sweep |
| A13 | **Durability (revised).** More than 1 run of an arm at one speed in one block fails check (a) or (b) | `full` is reclassified as not durable for the session, as in EXP-11's A5. Any other arm, `pgdelta` included, is reclassified as not durable at that speed for the session (revised; the reason is under After a check of the revision). Recorded under rule 8. A reclassified arm's times are reported but can't count as a win on time where it is reclassified (R1, A7), since its durable time is unknown and could only be longer. So if `full` is reclassified, R1 counts every speed as one where `pgdelta` wins on time. Nothing is rerun |

**The settle gate** (A8): over the settle's last 10 s, every second's completions within ±5% of R and the mean schedule lag under 1 ms, from pgbench's `-P 1` lines; afterwards, from the log, the settle's p99 at most twice the warm-up's, and at most 0.1% of its transactions later than L. Its 60-s cap is the source's only; dest's work has no cap (revised).

## Notes written before running

- **The proxy measures what the registered rules can't (revised).** The registered projection assumes loss grows with CPU-seconds, from a run whose largest source cost, writing S, v1 doesn't pay. With spare CPU, loss may be closer to a threshold on how many cores a copy uses at once. A link-paced `pg_basebackup` may cost nothing measurable at 0.5 and 2 Gbit/s, so the registered ratios may have nothing to divide by (E8). The proxy measures v1's source side directly, for two arms and no change to pgdelta. It decides the cap (decision 2) and the HA caveat. It decides the T5/T8 order only where R1 and R2 leave it open (changes 4 and 5).
- **Why open loop, against the schedule.** The ledger asks for a fixed rate. A closed-loop load slows down with the server and hides a stall as fewer requests. A fixed-rate load keeps arriving, so throughput drops only when the source falls behind, which is the loss an operator sees. Comparing completions with the schedule, not the nominal rate, takes out the arrivals' own noise.
- **Why aligned windows.** The pilot found 57.6% loss over the whole run against 71.4% over the backup's own window, and throughput still 40.6% down after it. Here every number is taken over the arm's own window plus 10 s, against a null of the same length, and the 15-s view checks the tail.
- **Why evict rather than drop.** Dropping the source's cache would evict the live load and slow it by itself. Evicting only the benchmark database's files and the snapshot keeps EXP-09's and EXP-11's cold reads, treats every arm alike, and stands in for a database larger than RAM. The warm block shows what a cached 2.1 GB changes.
- **Why both numbers must meet.** O4 names two numbers. A copy can lose little work overall yet stall the load for a moment, or the reverse.
- **"No worse" against an unmeasurable bar.** Where `full`'s loss is inside the null, "no worse" means "inside the null too, within δ". The floors (0.05 s, 0.03) are fixed now so that the bar can't drift toward either side.
- **Session drift.** Every comparison is within this session. EXP-11's and EXP-09's numbers appear only as bases for predictions and in the registered projection.
- **Order effects.** Runs that follow a staged arm, which wrote and removed 2.1 GB on the source, are compared with the others; a median difference over 0.05 s in W_net or 0.03 in D_net is reported.
- **A design note, not a test:** today's pgdelta sizes its pool from the host's CPU count. In a sidecar with a CPU limit (O3) it would run 4 workers under a smaller quota, in bursts. If "Cap meets" or "Cap fails" applies, the pool should size itself from the quota; T10b can test it.
- **"VM hours"** means the pair's wall-clock time, as EXP-08 and the queue read it. That is about twice as many instance-hours.
- **Lessons from EXP-11, applied:**
  - Every smoke item that checks something is printed by the analysis from the records, not read by eye. EXP-11's smoke item 12 was recorded as passing when its records showed it had failed.
  - Counts come from every log line, never from examples. EXP-11's analysis first counted the audit's example paths.
  - No cause is claimed that a timer or counter doesn't show. A stall is blamed on WAL syncs only if `pg_stat_wal`'s sync time shows it. A proxy's miss is blamed on hashing only if the warm block shows it too, and on the cold read only if removing the read lowers the proxy's own loss by more than δ (row 7; revised).
  - Verification never touches what is measured. EXP-11's verification flushed outputs before their start, which voided its "time until usable" reading. Here no check reaches the source inside a settle, a window or a tail. Dest's verification starts only after checks (a) and (b) have had their last read, and must finish before the next window, however long the settle then runs (revised).
  - The checks can fire. EXP-11's logical check could only ever be void. Here the oracle needs no reference state, and seven negative controls show it fails a wrong copy (smoke item 5), each through the check it targets, with an analysis-only one for coverage (v). `full_nosync` shows check (a) can fail (revised).
  - The checks pass a good copy. Two dumps of one state hash equal, and a good output that fails any check stops the sweep (smoke item 5; revised).
  - The benchmark database is compared by its logical digest and row counts, not file digests: its catalogs changed in every EXP-11 trial, likely through autovacuum.
  - The run lines are tested as a POSIX shell splits them, comments included (EXP-11's last fix).
  - No smoke item without a consequence (EXP-11's revisions; revised here). Each item below says what follows if it fails.
- **Before the main sweep,** the smoke run settles the items below, and they are recorded here, dated.

**To settle in the smoke run** (each printed by `analysis/exp12.py` from the records):
1. **pgbench's log.** Both scripts run with no failures. On every line the latency is at least the schedule lag, so latency is from the schedule. Completion times rise within each client. SIGALRM leaves a complete log: for each client, `n` at the stop minus `n` at the start equals the log's count of `commit.sql` lines, in a capacity run and in a load run after it on the same cluster (revised). The log's size per hour. A failure is fixed in harness code before the sweep, or SIGINT is used (The load).
2. **Capacity and rates.** One 60-s warm-up, then three 60-s closed-loop runs at 32 clients on the scale-70 cluster, logged and offset like every run (revised). C is their median. R_busy and R_mod, and the source's busy CPU at each, which selects E4's and E7's ranges. If a pgbench thread uses more than 90% of a core, `-j` goes to 8 once and C is measured again.
3. **L and the null (revised).** p99 over 120 s at R_busy; L = max(10 ms, 10 × p99), rounded up to 5 ms. From 5 `none` runs and every settle, the bootstrap null at T = 15 s. If W's 95th percentile for one window at T = 15 s is above 0.1 s, R_busy becomes 0.5 × C, once, and items 3 and 12 repeat. The source's busy CPU is then measured again at the new R_busy, as in item 2, and recorded with it before the sweep; that value selects E4's and E7's ranges (revised). The floors stay as fixed.
4. **Clocks.** chrony's offsets on both VMs and the agent's own timestamp exchange: within 5 ms; check (v)'s margin. If an offset is over 5 ms, `chronyc makestep` runs on both VMs and the item repeats; if it is still over, the VMs are replaced once, recorded (revised).
5. **The oracle.** It runs after a capacity measurement and a load run on the same cluster, as in the trial (revised). Both VMs report the same PostgreSQL minor version, and two schema dumps of the unchanged source, a minute apart, hash equal (revised). It passes on a good `full` output and a good `pgdelta` output. It fails on seven negative controls, each through the check named: a copy made with `-X none` (doesn't start); one client's `n` raised alone on a started copy (atomicity); one client's prefix extended by 100 transactions, its `n` and one balance both raised by 100 so that atomicity holds (the cut); the same cut back by 100 (the cut); a heap page of `pgbench_accounts` in loaddb overwritten on a stopped copy, which is then started (the invariant query (iii) errors on the page's checksum; revised); a leaf page of `pgbench_accounts_pkey` overwritten the same way, which no check but `pg_amcheck` reads (`pg_amcheck`, with (iii) passing; revised); a column added to `load_progress` on a started copy (schema; revised). Analysis only: a good output's cut checked against another repeat's interval from backup start to clock stop must fail (v) (revised). A control that passes, or a good output that fails any check, fires A1: the sweep waits until the oracle or the harness is fixed (revised).
6. **The proxy.** The sender reads the snapshot while the live source runs, and the source is never stopped (revised). Backup mode is held across the transfer in one session, with its start and stop LSNs. The output is byte-identical to the snapshot's selection, by step 5's stored digest, and starts. pgdelta's counters show every selected byte hashed (A4). A failure is fixed in harness code before the sweep; if it can't be, the run goes ahead without the proxy, as under What it needs built.
7. **The one-core sender** works against a 4-job receiver and reports its jobs. A failure is fixed before the sweep, or `v1proxy_s1` is left out and decision 2 reads "Cap not judged" wherever it needs it.
8. **Eviction.** After it, `fincore` shows under 1% of the benchmark database's files and the snapshot resident, and loaddb fully resident. If more than 1% stays resident, the eviction runs after a `sync -f /data` and repeats once. If that still leaves more than 1%, the residual share becomes the cache flag's threshold, recorded here, dated (revised).
9. **The staged capture's `--verbose` lines,** stamped over SSH, parse, with the "write-ahead log end point" line. A failure is fixed in harness code before the sweep.
10. **The settle gate** passes within 30 s after each kind of run at R_busy. If it does in fewer than 95% of the smoke's runs, the minimum settle rises to 30 s and item 15's projection is redone (revised).
11. **The sampler's cost**, with the run-queue reads of pgbench's threads. 60 s of load with it and 60 s without: p99 within 10%, its own CPU under 1% of a core. If either fails, its 0.1-s reads go to 0.5 s, then to 1 s, and the item repeats; the setting is recorded here, dated (revised).
12. **`pause2`** twice at R_busy: W and D inside E3's ranges, and the gap in the log matching the agent's signal times and dest's mapped phase (A2; revised). If not, the window mapping or the arithmetic is fixed (harness code) and the item repeats.
13. **Dest's checks (revised).** Check (a)'s idle noise (the largest increase of `Dirty + Writeback` over 20 idle windows of 5 s), which goes into the run line's `NOISE`. `full_nosync` once at scale 70 at 2 Gbit/s, which must leave at least 1.5 GB dirty, or check (a) is void for the session. The syscall audit of `full`, `pgdelta` and `v1proxy` at scale 5, each durable arm passing. An arm that fails the audit is fixed before the sweep if the fault is harness code; if `pg_basebackup` or pgdelta fails, its arm gets a timed `sync -f` (Durability on dest).
14. **Stability (revised: each with its action).** loaddb's size before and after the smoke run, at most 5% growth: if more, loaddb's fillfactor goes to 70 and the item repeats. No checkpoint started by WAL volume during the smoke run's load: if one does, `max_wal_size` goes to 32 GB; by the timer: `checkpoint_timeout` goes to 60 min. No autovacuum on loaddb: if one runs, its cause is found and recorded before the sweep, and the A8 flag stays. The benchmark database's logical digest taken twice, with load between, equal: if not, the cause is found before the sweep, or (vi) rests on row counts alone and the digest is reported. Each action is recorded here, dated.
15. **The time projection.** One scale-70 busy pass of every arm at every speed, with the controls (16 runs), and one calibration pass (7 runs), projected to the whole session with the warm block at 5 repeats. This decides the cuts below.
16. **`analysis/exp12.py`** runs end to end on the smoke data, writing only to a scratch directory. A failure is fixed before the sweep.

## Changes from the ledger (rule 8)

Each becomes a ledger row with its own grade when this is registered.

1. **Arms.** rsync is dropped: O7 rules it out as the engine, and its source work is the capture plus one hashing process, which `capture` and `v1proxy_s1` cover. The capture stays as its own arm, at one speed, since it uses no link. `none` becomes a randomized arm. Added: `bracket`, `v1proxy`, `v1proxy_s1`, the controls `pause2` and `burn4`, the warm block and the calibration block with v1 itself; and the unthrottled link for `full`, `pgdelta` and the proxy, reported beside, for EXP-11's HA qualifier. Grade B.
2. **The load.** pgbench, not HammerDB: two custom scripts on a separate 30 MB database, open loop, the client on the source, at 0.6 × C (deciding) and 0.3 × C (beside). Grade B.
3. **O4's two numbers are defined** as D and W against the schedule, net of a null resampled from load-only time for the same statistic (revised), with tolerances whose floors are fixed now. "Measurable" means above the null's 95th percentile, and the registered ratios are "not judged" (or bounded) where a loss isn't measurable. Grade C: the definitions and tolerances are judgments.
4. **R1's T5 consequence applies as registered (revised).** R1 and the October 24 note conflict when staged pgdelta's loss is higher where it wins on time but its rate doesn't track: R1 moves T5 ahead of T8, and the note leaves the order to v1's own arm. The first version let the note decide, graded B as a reading. In the expected case (E8) that set R1's consequence aside on a loss v1 doesn't have, in v1's favour. The lead decided that R1 governs where the two conflict. Grade C: a value judgment.
5. **The proxy takes up the T5/T8 order where the registered rules leave it open (revised).** When R1 doesn't fire and R2 doesn't track or isn't judged ("waits for v1's own arm"), `v1proxy` decides the order (decision 1, rows 4–7), and the warm block names a miss's cause. The proxy decides the cap whatever R1 and R2 say (decision 2). Where R1 or R2 decides, the proxy is reported beside it; if they disagree, both are stated, and the registered row stands unless the ledger records an override. Grade C: a value judgment, and the proxy leaves out WAL streaming and the live read.
6. **The rate's denominator is calibrated CPU-s from this session,** taken without load. The registered projection keeps EXP-09's 5.3–6.0 and 1.6–1.9, as registered; this session's ratio for v1 itself over `full` is reported beside it. Grade B.
7. **The source's cache:** the benchmark database's files and the snapshot are evicted before each run, and the load's database stays resident. Every earlier test dropped the whole cache, which would evict the load here. A warm block is reported beside, and names the cause of a proxy's miss (revised). Grade B.
8. **G4's logical match is self-contained.** Every live output is checked at its own end point: the balance invariant, a consistent cut and coverage from the load's own log, with 1% rolled-back transactions for G3's "aborted effects are absent". The benchmark database by row counts in every run and its digest in each cell's first repeat. The schema by `pg_dump --schema-only` (revised). No shared recovery target. Grade B.
9. **Hot only, with a second load level instead of a second workload.** Each arm's source work barely depends on the change pattern; whether a cap is needed depends on the load. Grade B.
10. **Cost (revised).** The ledger budgeted 1 day and about 3 VM hours. This needs about 3.45 days of harness work and about 3.4 hours of the pair (3.05–3.85; below), about 3.4 USD. The cut line is 3.5 hours.

## Revisions before running (October 24, 2026)

An independent review checked the merged draft against the ledger, EXP-04b, EXP-09, EXP-11 (with its revisions and corrections) and the harness code. It made 16 findings, 5 of them major. It also confirmed that the registered texts are quoted word for word, that the numbers cited from EXP-04b, EXP-09 and EXP-11 match their write-ups and CSVs, that the pause arithmetic and the budget's sums hold, that the tool claims hold (`pgdelta send` and `receive` take separate `--jobs`; datadir mode accepts a cleanly stopped copy), and that the decision tables don't overlap.

The project lead decided each finding. A fix was applied where the finding held against this draft, the ledger, EXP-11 and the harness code. Where a fix offered options or needed a choice, the lead took the option that doesn't favour v1, and the reason is given. Every finding held, at least in part. Below, "review n" is the review's finding n. The ledger's registered texts are unchanged.

**The load and the oracle**
- **The load's counters are offset per run** (review 1, major). `load_progress.n` counts across every pgbench run on the cluster, the trial's capacity run included. So the first version's cut pointed at the wrong transactions after the first run, and step 15's check failed by construction. Now each client's `n` and Σabalance are recorded, with the load stopped, before every pgbench start and after every stop. A copy's `n` maps to the log of the run whose span holds it. The capacity runs are logged like the others, and step 15 checks Σabalance = Σn = the sum of every logged run's commits. The other option, a capacity script that leaves `load_progress` alone, would measure the capacity of other transactions. Smoke items 1 and 5 now run a capacity measurement first, and a test maps a cut across two runs.
- **amcheck is installed in loaddb** at step 1, before B0, so every copy holds it (review 9). Without it, `pg_amcheck` skips the database with a warning and checks nothing. A "skipping database" line, or no relation checked, makes the run unverified. A copy with an overwritten page is a new negative control.
- **A schema check** (review 10). G3 asks for schema too. Each started live output's `pg_dump --schema-only` of loaddb and `postgres` is hashed and compared with step 4's reference: check (vii). A column added on a started copy is a new negative control, so the check is shown to fire.
- **Coverage and the clock mapping can fail** (review 13). A2 accepted `pause2`'s W anywhere in [1.4, 2.3] s, which can't see a mapping error. The analysis now finds the pause's gap in the log. It must match the agent's SIGSTOP and SIGCONT times within 5 ms plus one latency, and lie inside dest's phase mapped through the offset; otherwise A2 fires. The agent stamps its signals on the source's clock, so the first comparison checks the log and the second the mapping. An analysis-only control checks a good output's cut against another repeat's interval; it must fail (v).

**Durability on dest**
- **EXP-11's standard in full** (review 2, major; A13's scope changed after the check, below). The first version judged checks (a) and (b) "over the window", which runs 10 s past the clock. Dest's verification started inside those 10 s, and `verify()` maps the output's extents with `FIEMAP_FLAG_SYNC`, which writes it back: a copy that skipped its flush could pass. Now (a) and (b) are read just before the clock starts and first after it stops, and dest's verification starts only after that read. Added: `full_nosync` once at scale 70 at 2 Gbit/s in the smoke run, which must leave at least 1.5 GB dirty or check (a) is void; check (b) on `pgdelta`'s new O; check (c)'s stub test; EXP-11's smoke item 3 rule for an arm that fails the audit; and A13, which reclassifies an arm with more than 1 failed run at one speed. A non-durable arm's time can't count as a win on time, since its durable time is unknown and could only be longer. If `full` is reclassified, R1 counts that speed as a win for `pgdelta`, for the same reason. That choice keeps a flush failure from shielding staged pgdelta from R1.

**The null and the tolerances**
- **The null is resampled from all load-only time** (review 3, major). Above about 15 s, the first version's null came from 5 `none` runs, 3 in the moderate block, whose sliding windows overlap. Its 95th percentile decided "measurable" for `full` at 0.5 Gbit/s on roughly 5–10 independent samples. It is now a block bootstrap of 1-s buckets, in blocks of 10, from every `none` span, every settle in full and the last 60 s of each warm-up.
- **Each rule compares like with like** (follows from review 3). An arm's number is a median of n runs. So "measurable" now compares it with the 95th percentile of the null's median of n windows, and the bound in L3, L4, R2 and A6 is that percentile minus the null's median. The first version compared a median with single windows' 95th percentile, which hides small real losses, v1's proxy's among them.
- **δ combines both arms' own spreads** (review 3). δ is the 95th percentile of the null's difference between two medians, each at its own arm's T and run count. The floors (0.05 s, 0.03) and the 0.15 × W_net(`full`) term stay. The smoke run no longer raises the floors: δ now follows the sweep's own null, so a raise could only loosen it.

**The decision rules**
- **R1's consequence applies as registered** (review 6). R1 and the October 24 note conflict in the expected case: R1 moves T5 ahead of T8 when staged pgdelta's loss is higher where it wins on time, and the note leaves the order to v1's own arm when the rate doesn't track. The first version let the note decide and graded that B, as a reading. The review asked for grade C and a decision before the sweep. **The lead decided that R1 governs where the two conflict, so T5 moves ahead of T8.** The other reading sets a registered consequence aside on the expectation that staged pgdelta's loss is its capture's, which v1 removes: a choice that favours v1. Change 4 is now grade C. A5 still says where the loss comes from, beside R1. E8 and E9 change with it.
- **A proxy's miss names its cause before it moves T5** (review 4, major; 7b changed after the check, below). The first version's row 6 assumed the hashing slows the load, but v1 also reads about 1.5 GB/s cold from the WAL's array. The warm block's `v1proxy` hashes without reading the disk, so it now names the cause (row 7). A miss that warm also shows moves T5 ahead (7a). A miss that warm doesn't show points to the cold read: the consequence is a read-rate limit (D81), and T5 keeps its place (7b). If the warm reading can't be judged, T5 still moves ahead, with "cause not shown" (7c). That is the choice that doesn't let a missing reading excuse a miss. The warm block runs 5 repeats and leaves the cut list. The review's alternative, a paced read-only arm, would add an arm that runs no v1 code.
- **The client's part in the slowdown is measured** (review 5, major). The client shares the source's 8 vCPUs, so an arm that uses CPU also delays pgbench's threads. That overcharges CPU-heavy arms, v1's proxy among them. The sampler now reads each pgbench thread's run-queue wait, and a run whose added wait is at least 25% of its W is flagged "client wait". A decision row resting on a flagged cell is marked fragile, and the row stands, so the flag can't turn a miss into a pass. The rationale is reworded: applications usually run on other hosts, and the client is on the source because the egress tbf can't exempt its traffic.
- **L3 uses R2's bound** (review 7). An unmeasurable `full` can still give a ratio inside [2, 4]. L3 now fails high only if W_net(`pgdelta`) over `full`'s bound is above 4, fails low only if `pgdelta`'s bound over W_net(`full`) is below 2, and is not judged otherwise. L4's clause for an unmeasurable `full` at 0.5 Gbit/s had the same flaw and gets the same bound.
- **Decision 2 covers an unjudged one-core arm** (review 12). A4 now gates `v1proxy_s1` too. For (iii), the review offered to drop it or compare with `v1proxy`'s calibrated CPU-s divided by 1.06 (D48). The lead took the comparison: dropping it would let a one-core arm that did less work than it should make a cap look sufficient. If `v1proxy` misses and `v1proxy_s1` can't be judged, decision 2 reads "Cap not judged", and T10b still moves ahead of T8, to find out.
- **The latency caveat compares spans of one length** (review 14). The first version compared window p99 over about 14 s with p99 over about 50 s, and a long, mild effect dilutes more than a short, intense one. The review offered counts above 50 and 100 ms, or p99 over each arm's worst fixed-length span. The lead took the worst 5-s span. Counts add up over a long window, and a short burst at a few times the settle's latency may not reach 50 ms at all, so they would rarely flag a short refresh. The counts are reported beside.
- **E4 and E7 are conditional on the busy CPU** (review 8). The first version assumed about 3 vCPUs spare, while E1 allows 2 to 4.8. Each now has a range for a busy CPU at R_busy of 50% or less, where 4 workers fit in the spare CPU, and one for above, both written now. The review's other option, final ranges written after the smoke run, would set predictions from data.

**Run and build**
- **No window starts before dest's work is done** (review 11). The 60-s cap applies to the source's settle gate only. Dest's wait is recorded.
- **Every smoke item has a consequence** (review 15; item 5's action for a good output that fails was added after the check, below). Items 4, 8, 10, 11 and 14 now say what follows a failure, and so do items 1, 6, 7, 9, 13 and 16, which had no stated action either. For eviction the review suggested `vmtouch -e` or cold-reading only the snapshot; `vmtouch` uses the same `posix_fadvise` call, so the action is a `sync -f` and a second eviction, then the residual share as the cache flag's threshold, which applies to every arm alike.
- **The build list is complete** (review 16; changed after the check, below). `v1proxy*` stays out of `PGDELTA_DIRECT_METHODS` and so out of `SOURCE_STOP_METHODS`, so the harness never stops the live source for it. Its sender and `verify_direct()` read the snapshot and step 5's stored digest, never the live data directory. `method_flags()`, `method_paths()` and `PROC_SCOPE` get every new arm, with a test that `run.json` pins each one's command line. `load-pause` lives in `agent.py`.
- **The budget** (follows from reviews 2, 4, 9, 10 and 11): 117 runs in the sweep instead of 113, about 3.4 hours of the pair instead of 3.2, about 3.4 USD, and about 3.25 days of harness work instead of 2.75. The cut line stays at 3.5 hours.

**Not taken, and why.** No finding was rejected in full. Three parts of fixes weren't taken:
- **Review 3's root-sum-square δ.** The square root of the sum of the two single-window spreads is never smaller than the larger one, so it would loosen δ beyond the first version's, in v1's favour. The bootstrap of the two medians' difference combines the same two spreads for the statistic actually compared.
- **Review 3's tails in the null.** The seconds just after a run's window are the ones the 15-s view counts as the copy's. Adding them to the null would widen it with the copies' after-effects, also in v1's favour.
- **Review 6's wording,** that under E8's outcome R1's consequence doesn't apply. The lead's decision on the conflict goes the other way.

### After a check of the revision

A second, independent check compared the revision with the draft, the review's 16 findings, the ledger, EXP-11 and the harness code (`experiments/network/measure.py`, `run_exp04.py`). It made 9 findings, 2 of them major. It also listed 5 items the revision had left unaddressed or unrecorded. It confirmed that the registered texts and O4 are word for word, that the run counts and the budget add up, and that every review finding was at least partly addressed.

Each finding and item was checked against this document, the ledger, EXP-11 and the harness code. All 9 findings held, and so did the 5 items. Each was applied. Where a fix needed a choice, the option that doesn't favour v1 was taken, and the reason is given. Below, "check n" is the check's finding n, and "item n" its unaddressed item n. The ledger's registered texts are unchanged.

**The oracle**
- **The schema dump uses a fixed key** (check 1, major; item 3). Verified: since PostgreSQL 17.6, a plain-text `pg_dump` writes `\restrict <key>` and `\unrestrict <key>` lines, with a random key unless `--restrict-key` is given. PGDG installs its newest 17.x, and EXP-11 ran 17.11. So two dumps of one database never hashed equal. Every good `full` and `pgdelta` output would have failed (vii): R3 on every pgdelta output, and A1 on `full`. Now every dump uses `--restrict-key=exp12`. Both VMs must run the same minor version, since the dump's header names it; if they differ, the older one is upgraded first. Smoke item 5 checks that two dumps of the unchanged source hash equal. It now also has an action for a good output that fails any check: the sweep waits until the harness is fixed (A1).
- **An error from the copy counts as wrong** (check 4). Verified: data checksums are on, so a heap page overwritten on a stopped copy fails its checksum on the first read. The invariant query (iii) then errors (SQLSTATE XX001) before `pg_amcheck` runs. And "unverified" read "a check couldn't run", so a pgdelta output with a bad page could have left the medians without firing R3. Now any error the copy's own server or tools return is a failure: a failed query, a corruption report or error from `pg_amcheck`, a `pg_verifybackup` failure. Unverified covers only SSH, a check's timeout, a missing binary, and `pg_amcheck` skipping the database, where (vii) still shows whether the copy holds the extension. The `pg_amcheck` control now overwrites a leaf page of `pgbench_accounts_pkey`. The invariant's sequential scans never read it, and autovacuum is off for loaddb's tables, so only `pg_amcheck` can see it. The heap-page overwrite stays, as a seventh control that must come out wrong through (iii).

**The rules**
- **Row 7b needs positive evidence** (check 2, major). Verified: the warm block runs at 2 Gbit/s only, and row 7 compared warm `v1proxy` with warm `full` there. `v1proxy`'s own work doesn't depend on the link, but the bar does. A miss at 0.5 Gbit/s alone, or a miss near the line, could land on 7b by noise, and T5 would keep its place, in v1's favour. The check's example: with δ_D at 0.03, a cold difference of 0.035 at 0.5 Gbit/s and 0.025 at 2, and 0.025 again warm, gave 7b. Now 7b needs two things. Warm `v1proxy` meets O4 against warm `full`. And on each number it missed cold, its warm net value is below its own cold net value at 2 Gbit/s by more than δ. A warm miss gives 7a. Every other case gives 7c, and T5 moves. In the example, the row is now 7c. The warm block's text and the limitation now say that the warm reading stands for v1's own work at both speeds, not for the bar.
- **Decision 2 has an order** (check 5). `v1proxy_s1` could miss at 0.5 Gbit/s and be unjudged at 2, so "Cap fails" and "Cap not judged" both held. The rows are now checked in order: No cap, Cap fails, Cap meets, Cap not judged. A judged miss counts first. "Cap fails" is the stricter outcome: a rate limit before T8's speed claims.
- **Row 4 names `v1proxy`** (check 5). It read "A4 fires", and A4 now gates `v1proxy_s1` too. It now reads "A4 fires on `v1proxy`", as A4's own consequence says. A failure of `v1proxy_s1` alone gives "Cap not judged".
- **Every cell a rule uses needs 3 runs** (check 5). The minimum applied only to `v1proxy_s1` and the warm runs. It now applies to every cell (Short cells). A short cell is not judged, and every rule takes its "not judged" path at that speed: R1, R2, L3 and L4 for a short `pgdelta` or `full` cell; row 4 for a short `v1proxy` or `full` cell; "Cap not judged" for a short `v1proxy_s1` cell. One choice was made here. Row 4 applies only if `v1proxy` misses in no judged cell. Otherwise a harness failure at one speed could hide a miss at the other, in v1's favour. Row 4 is the registered path for "not judged" ("the T5/T8 order waits for v1's own arm"). Since unverified now means only a harness-side failure (check 4), a short cell says nothing about v1 either way.
- **E14, E15, E16 and "Written down now"** (check 8). Verified against `docs/experiments/data/exp11-cells.csv`: in EXP-11's warm-source cells, v1 at 4 jobs used 5.28–5.36 source CPU-s against 6.22–6.27 cold, in 3.07–3.21 s against 3.08–3.13 s. That is about 15% less CPU in about the same time. The revision's basis for E15, that warm hashing is "as intense or more", was wrong. E15's basis now gives these numbers: the warm reading removes the cold read's I/O and its kernel CPU. Row 7b's cause is named "the cold read: its I/O and its kernel CPU". E15's ranges stand. E14 gets the two busy-CPU ranges: above 50%, `v1proxy`'s W is likely measurable, and A6 can read either way. "Written down now" now follows E9's two cases: near the line at 50% busy or less, likely a miss on D above.
- **"Client wait" has a counterpart for D** (check 9). The flag was judged only on W. E9's likelier proxy outcome is a miss on D, which may come from the client's wait in the worst second, and the flag couldn't mark it. Now, in the window's worst bucket, the pgbench threads' added run-queue wait, averaged over the threads, is taken as a share of that bucket's lost time, (S_b − X_b) / R. It is flagged at the same 25%, wherever D is measurable. The sampler's 0.1-s reads give the wait per bucket. E16 changes with it. As before, the flag marks a row fragile and changes none.
- **Smoke item 3's fallback measures the busy CPU again** (check 6). If item 3 moves R_busy to 0.5 × C, the busy CPU that selects E4's and E7's ranges (and so E9's, E14's and E16's) came from item 2, at 0.6 × C. The fallback now measures it at the new R_busy, and that value is recorded with R_busy before the sweep.

**The null**
- **Unsettled settles leave the pool** (check 7). The pool took "every settle in full", unsettled ones included. A settle whose gate didn't pass within 60 s is disturbed by definition, most likely by the previous run's after-effects. Those seconds would widen the null and δ in v1's favour, as the tails would. They are now left out, and the null with them is a robustness view.
- **Each run at its own T, and δ centred** (check 7). The runs in a cell have different T's, and W's null median grows with T. The cell's null now draws one window per run, at that run's own T. δ's percentile is now taken of the difference between the two cells' resampled medians, each minus its own null's median and clipped at 0, as the net values are. The uncentred difference shifted δ by the gap between the two null medians, which grows with the gap between the arms' T's. "Measurable" and the bound use the same centring.

**The harness**
- **`v1proxy*` joins `PGDELTA_DIRECT_METHODS`** (check 3; item 4). Verified in the harness: `PGDELTA_DIRECT_METHODS` also feeds `PGDELTA_METHODS`, which sets the stats and receipt paths (`run_exp04.py` 946–951) and lets `tool_reported` read pgdelta's `--stats-json` (1945), the counters A4 (i) needs. It also feeds `OUTPUT_START_METHODS` (`measure.py` 116), `verify()`'s route to `verify_direct` (1673), `run_phases` (1658), the `pgdelta.datadir` preflight (2903) and `settle_records` (2367). Kept out of it, `v1proxy*` fell through each: `verify()` would run `pg_verifybackup` on B, which has no manifest, and A4 (i) would find no counters. Of the check's two options, this takes the first: `v1proxy*` joins the set, and `SOURCE_STOP_METHODS` is defined without `V1PROXY_METHODS`. The proxy then takes v1's own path through the harness, which A4 asks of it. Only the source differs: it isn't stopped, and its path is the snapshot, in `method_paths()`, in `pgdelta_send_argv()` (which reads `self.source_data` today, line 1290) and in `verify_direct()` (which digests the live data directory, line 1740). The second option needs a dispatch for `V1PROXY_METHODS` at each of these places and at three more that test `PGDELTA_METHODS` (`run_exp04.py` 1514, 2127, 3143), each a chance to fall through. Neither option favours v1. A test asserts, for every new arm, its `run_phases` and `verify` path, its stats path, whether it stops the source, and whether check (b) applies.
- **Check (b) covers `pgdelta`'s O** (check 3; item 4). Verified: `durability_check` applies (b) only to `FULL_COPY_METHODS` (`measure.py` 110, 1636), which lacks `pgdelta`. (b) now uses a set of its own, `FULL_COPY_METHODS` plus `pgdelta`. `FULL_COPY_METHODS` itself is left alone, since EXP-11's analysis and tests use it for full copies alone. On ext4, pgdelta's `copy_file_range` of B0's unchanged blocks writes them, so (b) holds for a durable O.

**Choices the first revision made without a reason**
- **A13 is narrowed for `pgdelta` only** (item 1). Verified: EXP-11's A5 reclassified an arm "as not durable for the session". The revision narrowed that to "at that speed" and gave no reason. The two arms now get the option that doesn't favour v1, each in turn. For `pgdelta`, A13 holds at that speed. R1 is judged per speed, and a reclassification for the session would take `pgdelta` out of R1 at both speeds after a failure at one, which would shield it from R1. For `full`, A13 holds for the session, as EXP-11's A5 did, and R1 counts every speed as a win for `pgdelta`. Narrowing it would take speeds out of R1's reach, in v1's favour. The rule is asymmetric on purpose: in both cases, the choice keeps R1 in force. No rule uses the other arms' times, so for them A13 holds at that speed.
- **Review 3's options** (item 2). Three choices were made without a reason. The reasons:
  - **A block bootstrap, not longer `none` runs.** Runs of 180 s, three disjoint 60-s windows each, would add about 16 minutes of the pair (8 runs × 120 s) for 24 windows at T = 60. The bootstrap draws from about 30 minutes of load-only time at no cost. Its weakness, a disturbance longer than 10 s split across blocks, narrows the null, which doesn't favour v1.
  - **The warm-up's last 60 s, not all 120.** The first minute holds pgbench's start: its connections and first schedule lags, which the warm-up exists to absorb. Those seconds would widen the null in v1's favour.
  - **Every settled settle in full, not its seconds before the gate's last 10 s.** The review left out the gate's last 10 s, since the gate selects them to be quiet. That would keep each settle's first seconds, which follow the source's untimed work for the run (the previous S verified and removed, the eviction). Those may carry that work's after-effects, which would widen the null and δ in v1's favour, as the tails would. The quiet seconds narrow the null instead, which doesn't favour v1. Their other effect, a lower null median, raises every net value by at most E2's 0.02 s, inside δ_W's 0.05-s floor. Unsettled settles are now left out (check 7).
- **E2, E15 and E16 are listed** (item 5). The first revision changed E2 (for a median of 5 windows, δ_W is the 0.05-s floor; from review 3) and added E15 (warm `v1proxy`; from review 4) and E16 ("client wait"; from review 5) without listing them. They are listed here. E15 and E16 change again above (checks 8 and 9).

**The budget.** About 1 more minute of smoke, for the seventh control and the dumps: still about 3.4 hours of the pair (3.05–3.85), about 3.4 USD, and the same 117 runs. Harness work rises from about 3.25 days to 3.45: the oracle 0.05 (the fixed key, the error rule, the new control), the analysis 0.1 (each run's own T, the centred δ, row 7b's test, the flag on D) and the tests 0.05 (each new arm's paths). The cut line stays at 3.5 hours.

**Rejected.** No finding or item was rejected.

### Analysis readings (October 24, 2026, after the harness build's review)

How the harness and `analysis/exp12.py` read what the text above leaves open, recorded before the smoke run, as EXP-11 recorded its own. Each is marked as a *reading* of the text or as a *rule 8* change, with its reason. Where a reading needed a choice, the one that doesn't favour v1 was taken. None changes a registered text.

**What a result means**
- *Reading.* A copy that its own tool aborted is **wrong**: `pg_basebackup`, pgdelta's receiver or sender, or the proxy's `backup-hold` exits non-zero or times out inside the timed phase. The copy doesn't exist, so "the output doesn't start". R3 and L5 read such a run, and A4 also excludes a proxy aborted this way. This holds for every arm with an output (`full`, `pgdelta`, `capture`, the proxies, v1 itself). The harness records the run as status `fail` with `failure_kind` `tool`. On an arm with no output (`bracket`, the controls), a tool's failure stays an error.
- *Reading.* A failure of the harness around an output counts as **unverified**. Two cases: `ssh` exits 255 under `backup-hold`, or a command stamped over SSH never gets its agent's result line. Such a run leaves every median, is listed with the unverified runs, and counts toward their cell flag. L5 then reads "not shown".
- *Reading.* A window whose log holds less than half of its rate's schedule has no numbers and leaves the medians, since it would read as a window without loss. The same holds for a settle: one its log doesn't cover counts as not settled. Spans the log doesn't cover enter no variant of the null.

**The load**
- *Reading.* A run's `load_run` is the pgbench run going after its step 1. When step 1 finds the load dead and restarts it, the settle and the window belong to the restart, whose log is read.
- *Reading of A8 ("If the load dies, the runs after it are void").* A death is found before a run (step 1) or at a planned stop: the switch to the moderate load, step 15, or a clean-up. Either way, the latest run on that load run is void, since its window may hold the death.
- *Reading of the SIGINT fallback.* PostgreSQL 17's pgbench doesn't catch SIGINT. Each thread's buffered last lines are lost: about 30 per client, on a local cluster. So a run stopped this way is judged by the fallback's rule alone. Each client's transaction numbers must run from 1 without a gap, and its last line must come at or after the signal less 1 s. Step 15's sums then take that run's counters as its committed transactions.
- *Reading.* Whatever ends a trial early (an error, or `remote.py stop`) still compresses and fetches the load's logs before the load's directory is removed, and reads them for the cut and coverage of the outputs taken so far.

**The numbers**
- *Reading.* A window's last bucket ends at t0 + T, so it can end up to 1 s past t1 + 10 s; the 15-s view's last bucket can end up to 1 s past t1 + 15 s. The quiet tail runs on until that bucket has ended, plus 50 ms (about 0.5 s more per run, about 1 minute over the sweep), so the view never holds the harness's next step on the source. The D views in 0.5-s and 2-s buckets, and their nulls, draw blocks of 10 s too. A 2-s bucket at a window's odd end holds its last second alone in the run, and its null draws whole 2-s buckets.
- *Reading.* Latency percentiles are nearest-rank over every logged transaction. A transaction's lateness is its latency from its schedule. The settle gate's log part compares the settle's p99 with its load run's warm-up, over the warm-up's last minute.
- *Reading.* A run's own net values (`work_lost_net_s`, `worst_drop_net`) are its W or D minus the median of one null window at its own T, clipped at 0. A cell's net values are as defined above.
- *Reading.* Source CPU under load is the window's VM-wide busy CPU, over [t0, t1 + 10 s], from the sampler's `/proc/stat` ticks, minus the settle's rate times the window's length. The timed phases' own excess is reported beside it.

**The rules**
- *Rule 8.* The controls' cells (`pause2`, `burn4`) are judged on the 2 runs per block that the design gives them. A control cell with 1 run isn't judged. Short cells reads "fewer than 3 runs left isn't judged, by any rule", but the design gives each control only 2 runs per block. Read literally, A6 could never be judged and E4 and E14 would be empty. The rule's reason is runs leaving a cell, which this keeps for every other cell.
- *Reading.* A4 can't be decided when a counter or a calibration is missing, and it is then "undecided". The proxy's judged misses still count and its meets don't, so a missing counter can't hide a miss or make a meet.
- *Reading.* A6 is judged at a speed only where `v1proxy`'s W is measurable and both `v1proxy_s1`'s and `burn4`'s values exist. Where neither registered speed qualifies, A6 is not judged and D81 stays D.
- *Reading.* In row 7b, the warm cell is compared with the cold cell, and the 0.15 × W_net(`full`) term of δ takes the larger of the warm and the cold `full`'s W_net. The larger δ makes 7b harder to reach.
- *Reading.* The paired view pairs an arm's runs only with the same repeat's `full` run, at the same speed, in the arm's own block. Its net value is the median paired difference minus its null's median, unclipped, against δ from that null. Every other comparison, row 7b's among them, uses the main view.
- *Reading.* Decision 1's rows are checked strictly in order, so row 5 (nothing measurable) comes before row 7 even where a difference exceeds δ.
- *Reading.* A2 fires on a `pause2` run that fails any check it could make. A block without a `pause2` run says so.
- *Reading.* A9's "near the line" covers R2's bound ratios as well. The lower bound is read against 1.5 and the upper bound against 1/1.5, the only line where each decides.
- *Reading.* A1's coverage control pairs only outputs taken under the load. Without the load, a copy lacks no commit, so its cut stays open after the last commit it holds, and any later window would cover it.

**The smoke run and the analysis line**
- *Rule 8.* The registered analysis line now passes the smoke run's C, the busy CPU at R_busy and check (a)'s idle noise (Run lines). Without them, A12 could never be judged on the main sweep, and the sweep's own settles could select another branch of E4, E7, E9, E14 and E16 than the one smoke item 2 registered. On the main sweep, without `--busy-cpu`, the analysis selects no branch. The smoke run's own analysis takes its busy settles' median, which is what item 2 records.
- *Reading.* Smoke item 11 compares the busy warm-up's p99 over its last minute with the sampler and without it. The run without it is a separate sweep given to `--sampler-off`.
- *Reading.* Smoke item 12 judges `pause2` by E3's W and D ranges and by A2's two gap checks. E3's late count isn't part of item 12.
- *Reading.* Smoke item 15 adds the budget's fixed parts to the projected runs: 20 minutes to provision, the smoke's 60, the trial's set-up 8 and end 4.5, and 15 to fetch and tear down. It also adds 28 speed visits of 8 s, then applies the cuts in their order until the pair's time is within 3.5 hours.

**The smoke run (October 24, 16:57–18:18 UTC, before the sweep)**

Five invocations on two new VMs (`us-central1-a`, PostgreSQL 17.11 on both, created 16:51): `exp12-smoke-cap` (scale 70; C from three capacity runs at a provisional 1,000/s; the oracle's seven controls), `exp12-smoke` (scale 70 at R_busy; one busy pass of every arm at every speed, the controls, one calibration pass, `full_nosync`, the idle-noise windows; 26 runs), `exp12-smoke-null` (five `none` runs at R_busy), `exp12-smoke-audit` (scale 5, the syscall audit of `full`, `pgdelta` and `v1proxy` under the load, the sampler at 0.5 s) and `exp12-smoke-nosampler` (scale 5, one `none` run with the sampler off). Every run passed. The items, as `analysis/exp12.py` printed them from the records:

1. **pgbench's log** held: no failures; latency never under its schedule lag; completion times never falling within a client; every run complete (each client's change in `n` equal to its log's commits). At step 15, Σabalance = Σn = the logged commits in every trial (7,496,307; 22,119,039; and so on).
2. **Capacity and rates.** C = 30,316 transactions per second (the median of three 60-s closed-loop runs at 32 clients). So **R_busy = 18,100** and **R_mod = 9,000** (0.6 and 0.3 × C, rounded down to 100). The source's busy CPU at R_busy was 0.628 of its 8 CPUs (0.641 in `exp12-smoke-null`), above 50%, which selects E4's and E7's upper ranges. **Seen, not acted on:** each later trial's own one-run C was lower, 27,148 and 27,508, so R_busy was 0.66–0.67 of those trials' C. The rule fixes R_busy from this item; item 3's check of the null, below, is what guards against too high a rate.
3. **L and the null.** The busy warm-up's p99 was 10.54 ms (10.39 ms in `exp12-smoke-null`), so **L = 110 ms** (the larger). One window's W null at T = 15 s had its 95th percentile at 0.009 s (0.017 s from `exp12-smoke-null`'s five `none` runs and its settles), well under 0.1 s, so R_busy stays.
4. **Clocks (changed before the sweep).** Debian's GCE image keeps time with `systemd-timesyncd`, not chrony, so the item's chrony readings and its `chronyc makestep` remedy weren't available. chrony was installed on both VMs against Google's metadata time server (and added to `startup.sh`, commit 127e9ed). Both then read under 2 µs from NTP time, and the harness's own probe put the two clocks 0.002–0.018 ms apart in every trial: within 5 ms.
5. **The oracle** held. Under the load, after the capacity runs, the good copy passed every check, and each of the seven controls failed through the check it targets (`no_wal` the start; `n_raised` and `heap_page` the invariant; `index_page` amcheck only, with the invariant passing; `cut_extended` and `cut_back` the cut; `schema` the schema). One PostgreSQL minor version on both VMs; two schema dumps with `--restrict-key` hashed equal. No good output failed any check.
6. **The proxy** held in 7 of 7 runs: backup mode held across the transfer, every byte hashed, the output verified against the snapshot's digest and started; the source never stopped for a proxy.
7. **The one-core sender** reported `--jobs 1` against a 4-job receiver.
8. **Eviction** held: the benchmark database's files and the snapshot at most 0.00% resident after it, loaddb at least 98.4% resident; the second eviction after `sync -f` ran once.
9. **The capture's `--verbose` lines** parsed, with the WAL end point, in 6 of 6.
10. **The settle gate** passed within 30 s in 19 of 19 runs at R_busy (and 5 of 5 in `exp12-smoke-null`). At the provisional 1,000/s it failed once: the per-second rate's own scatter there (921 to 1,062 against a ±5% gate) is about ±3%, against about ±0.7% at 18,100/s.
11. **The sampler's cost (changed before the sweep, as registered).** At 0.1-s reads it used 2.6% of a core (0.0263 and 0.0256 in two sweeps), over the 1% limit, so its reads went to 0.5 s and the item repeated: 0.9% of a core, and the busy warm-up's p99 over its last minute 9.67 ms with it against 9.32 ms without it (`exp12-smoke-nosampler`), within 10%. **The sweep runs with `--sampler-interval-s 0.5`.**
12. **`pause2`** held in both runs: W 1.896 and 1.915 s, D 1.000, the gap in the log matching the agent's signals, inside dest's mapped phase.
13. **Dest's checks.** The idle noise was 0.48 MiB (499,712 bytes), so **NOISE = 0.48** and check (a)'s limit stays at 4 MiB. `full_nosync` left 2.29 GB dirty (at least 1.5 GB). The syscall audit passed for `full`, `pgdelta` and `v1proxy` under the load: 1,311–1,320 files and 1,323–1,350 directory changes, none uncovered (read from the records; `analysis/exp12.py`'s item-13 line doesn't read the audit, a reporting gap only).
14. **Stability (the registered action not taken; rule 8).** The checkpoint and autovacuum checks held, and the benchmark database's logical digest was the same at the end as at the start. **loaddb's growth failed:** 278%, 67% and 50% in the three scale-70 and scale-5 trials, against at most 5%. All of it was in `load_progress`, the 32-row table of the clients' counters (32 pages at the start, up to 11,759 at the end); `pgbench_accounts` stayed at 3,637 pages. On a local PostgreSQL 17 the same scripts at 56,000/s for 20 s added 62 pages: nearly every update there was HOT. On the VMs about 0.05–0.13% of the counters' updates were not, with no copy needed for it (`exp12-smoke-null` grew 32 to 2,863 pages with `none` runs alone); the cause wasn't found. **The registered action, loaddb's fillfactor at 70, isn't applied:** it acts on `pgbench_accounts`, which didn't grow, and it would change the load whose C and rates items 2 and 3 measured. **What the item guards was checked directly instead:** over `exp12-smoke`'s 19-minute busy load, the completed rate held at about 1.075 million per minute (R_busy), the median latency at 0.69–0.75 ms, and the p99 in minutes without a copy at 1.5–3.6 ms by the end, against about 10 ms in the warm-up; nothing drifted upward. The sweep's write-up will report the same per-minute view over its whole load, and loaddb's growth.
15. **The time projection:** 96 minutes for the 117 runs at this session's median run times, and with the budget's fixed parts, 3.45 hours against the 3.5-hour cut line, so **no cut** (as the dated readings above define the projection, with the budget's 60 minutes for the smoke run). The smoke run itself took about 81 minutes, with the chrony and sampler repeats, so the session will run about 20 minutes over that projection.
16. **`analysis/exp12.py`** ran end to end on every smoke sweep, writing only to a scratch directory.

**Drift over the session (October 24, 19:46 UTC: written during the sweep, before its logs were fetched or any of its numbers read)**

Smoke item 14's registered action wasn't taken. What it guards, a load that behaves the same all session, is judged on the sweep by this check, fixed before the sweep's data is read. Until now only the harness's progress lines had been seen: run names and times, block boundaries, and the busy load's commit count at its end.

1. **What it reads.** Each loaded run's settle, the load-only span before its window, as `analysis/exp12.py` reports it: its p50 and p99. The busy load is one load run across the busy and warm blocks; the moderate load spans the moderate block.
2. **The comparison.** For the busy load, the median p50 and the median p99 over the busy block's first repeat's 14 settles (its earliest load-only time) against those over the warm block's 10 settles (its latest). For the moderate load, its first repeat's 6 settles against its third repeat's 6.
3. **Drift,** in a load, if the later median p50 is more than 10% above the earlier, or the later median p99 more than twice the earlier. The 10%: the smoke's copy-free minutes held their p50 at 0.69–0.75 ms, within ±4%. The factor 2 is the settle gate's own (a settle's p99 at most twice its warm-up's).
4. **Reported beside, not judged:** the same two medians for every repeat; a per-minute view over each whole load (completions, p50 and p99, the copy windows marked); `load_progress`'s and `pgbench_accounts`' sizes at the trial's start and end; the settle gate's passes per repeat.
5. **What follows.** No drift: the session stands, and smoke item 14's deviation is reported as it is. A load that drifted: comparisons within a block stand, flagged, since each repeat visits the speeds in a shuffled order and runs the arms at each speed in a shuffled order. Comparisons that span the drift (busy against warm if the busy load drifted; busy against moderate if either did) are reported as "not judged: drift". A rerun would then be proposed, with a fix aimed at `load_progress` registered before it, for the project owner to decide. Nothing is rerun in this session (A11).

## How the two drafts were merged

The first version's choices. Where the review changed one, the row says so.

| Choice | Realism draft | Measurement draft | Merged, and why |
|---|---|---|---|
| The load's scripts | +1 to one account and to the client's counter; 100,000 rows | a transfer between two accounts plus a teller's count; 200,000 rows | a transfer that adds a net 1, plus the client's counter, on 200,000 rows, and 1% rolled back: one invariant catches a tear anywhere, the counter gives the consistent cut, and the rollbacks test G3 |
| The source's cache | static files evicted, cold | warm, never dropped | cold by eviction, as EXP-09 and EXP-11 read and as a large database reads; a small warm block beside. *Revised:* the warm block runs 5 repeats, is never cut, and names the cause of a proxy's miss |
| v1's proxy | backup mode around a direct read of a stopped snapshot | backup mode around a staged transfer from a fresh S | the snapshot: it runs v1's own code path and reads cold from the same disks |
| W | the sum of per-second deficits against the schedule | the count of transactions later than L | the deficit: it is the area of the dip O4 names and needs no latency limit; the late count is a robustness view |
| D | completed over scheduled in the worst second | the worst second's completions over the settle's mean | completed over scheduled: it removes the arrivals' noise |
| Null and tolerances | `none`'s median; fixed 0.05 and 0.05 s | sliding load-only windows; tolerances from their spread | sliding windows, with floors fixed now (0.05 s, 0.03). *Revised:* a block bootstrap of the same statistic, δ from two medians' difference, the same floors |
| The second condition | a second load level (0.3 × C) | a second workload (uniform) | the load level: it is what a cap would depend on |
| Arms | adds `v1proxy_s1` | keeps the capture and rsync; adds a hashing-only arm, `bracket` and controls | `v1proxy_s1`, the capture, `bracket` and the controls; rsync (O7) and the hashing-only arm (split by the proxy's own steps and `bracket`) left out |
| Unthrottled | yes | no | yes, for the HA caveat, and as the speed where `full`'s loss is most likely measurable |
| Source CPU | under load, minus a baseline | an idle calibration block with v1 itself | the calibration decides, since the registered projection's inputs were idle numbers; under load is a view; the proxy's gate compares it with v1's own |
| The proxy and the registered rule | the proxy outranks the projection | the proxy decides only where the registered rule defers | the second: the registered rule stands where it decides. *Revised:* R1's consequence stands where it fires, so the proxy decides the order only where R1 and R2 both leave it open |
| Positive control | – | a 1.5-s pause | a 2.0-s pause, so a whole 1-s bucket always lies inside it |
| Settle and tail | a guard of 5 s or more, a 10-s pre-window, a 10-s tail | 20 s each | 15 s each, with a gate, to fit the budget; the window ends 10 s after the copy |

## Limitations, known before running

- **No live v1.** The proxy reads a stopped snapshot on the live source's disks, in backup mode on the live server. It doesn't stream WAL, doesn't read files while they are written, and doesn't send loaddb's changes. G4 here covers `pg_basebackup` and staged pgdelta, which copies a frozen S. v1's live read is T8's.
- **One synthetic load,** at two levels calibrated on this hardware: three-row update transactions on a 30 MB cached database, synchronous commits, no read I/O. Real OLTP reads more and has larger working sets (T10b).
- **The client shares the source's CPU (revised).** An arm that uses CPU also delays pgbench's threads, which an application on another host wouldn't feel. That charges CPU-heavy arms, v1's proxy among them, more than their real cost. The "client wait" flag shows where, and the rows stand.
- **The warm reading at 2 Gbit/s stands for v1's own work at both registered speeds, not for the bar** (revised). `v1proxy`'s work doesn't depend on the link, but `full`'s loss does. So row 7b also asks the warm reading to fall below the proxy's own cold reading at 2 Gbit/s by more than δ, and a miss it can't explain moves T5 (7c).
- **The null is resampled** from about 30 minutes of load-only time in the busy block and about 10 in the moderate one, in blocks of 10 s, settles that didn't pass their gate left out. A disturbance in the null longer than 10 s is split across blocks (revised).
- **2.1 GB fits in RAM.** The copies read cold, as a large database's copy would, but they never evict the load's working set, which a TB-scale copy can (T9). No number here is projected to TB scale (rule 10); W grows with a copy's length, D doesn't.
- **Short refreshes,** 0.2–42 s, in 1-s buckets. A sub-second stall is split across buckets in D; W is not split.
- **Small checkpoints.** Only loaddb is dirty, so every fast checkpoint is cheap. A primary with a large dirty set would pay more at each backup's start, every arm alike.
- **Fast checkpoints for every arm.** `pg_basebackup`'s default is spread; Omni's flags are unknown.
- **Clear text.** Under O6's TLS the full copy would encrypt 2.1 GB and v1 about 28 MB.
- **Staged pgdelta writes S on the array that holds the data and the WAL,** and ext4's ordered mode can make a WAL fsync wait for S's flush. That is a v0 cost only.
- **One VM pair, local NVMe, ext4, 0.1 ms round-trip time.** A separate WAL disk or a persistent disk would change the I/O interference (T9). Larger machines leave more headroom.
- **No CPU limit on the refresh.** Omni can give a sidecar its own limits (O3); T10b.
- **The destination is idle,** as it is in HA and DR.
- **Source CPU is VM-wide.** The calibration runs without load; under load the attribution is approximate.
- **O4's tolerances and L are judgments,** fixed here and in the smoke run.
- **Effects that last more than 15 s after a refresh aren't counted.** The 15-s view shows whether the 10-s window misses any.
- **pgbench queues per thread.** A stalled transaction delays the next ones scheduled on its thread. That counts as latency, as it would for an application's connection pool.
- **The bar is `pg_basebackup`, not rule 3's fastest copy.** At 0.5 and 2 Gbit/s the fastest copy measured is EXP-11's bound, which reads a stopped source and has no slowdown number until T8.
- **The order is fixed:** busy, warm, moderate, then calibration. Load level and time in the session are confounded, as workload and time were in EXP-04b and EXP-11.

## Run plan and budget

**Per-run times, bottom-up.** A run under load takes its settle (15 s), its window and its quiet tail (15 s), plus about 3 s for eviction, `fincore` and the counter reads. Dest's set-up and verification overlap the settle, except each cell's first-repeat logical digest (about 28 s), which adds about 23 s. Staged runs add about 7 s of untimed source work (S hashed and removed), the capture about 4 s. Window lengths come from E6. Each speed's visit, with its link test, takes about 8 s. So a busy repeat (14 runs) takes about 11.4 minutes, a moderate repeat (6 runs) about 5.1 and a warm repeat (2 runs) about 1.6. The trial's set-up, from the cluster to the end of the warm-up, takes about 8 minutes, and its end about 4.5. A calibration run takes 15–30 s. Revised: the warm block's two more repeats add about 3.2 minutes; the schema dumps, and first-repeat settles that wait for dest's work, about 1 more. The check's seventh control and the smoke's two extra dumps add about 1 minute to the smoke run.

| Step | Runs | Pair wall time |
|---|---:|---:|
| Create both VMs in `us-central1-a`; `remote.py provision`; deploy; SSH set-up | – | 20 min |
| Smoke: mechanics, the oracle's controls and the syscall audit at scale 5; capacity, L, the null, clocks, the sampler and `pause2` at scale 70; `full_nosync` once; one busy pass of every arm and one calibration pass; the analysis dry run | about 48 | 60 min (50–76) |
| The trial, `exp12-hot`: set-up and warm-up; 4 controls; the busy block (14 runs × 5 repeats); the warm block (2 × 5); the switch and one `pause2`; the moderate block (6 × 3); the calibration block (7 × 2); the end | 117 | 108 min (98–120) |
| Compress and fetch the logs (about 3.6 GB raw), then `infra/gcp/teardown.sh` | – | 15 min |
| **Total** | **117 in the sweep, about 165 with the smoke** | **about 3.4 h (3.05–3.85), about 3.4 USD** |

Per busy repeat: `full`, `pgdelta` and `v1proxy` at 0.5, 2 Gbit/s and unthrottled; `v1proxy_s1` at 0.5 and 2; `none`, `capture` and `bracket` once. Per moderate repeat: `full` and `v1proxy` at 2 Gbit/s and unthrottled; `none` and `v1proxy_s1` at 2. Per warm repeat: `full` and `v1proxy` at 2 Gbit/s.

**If the smoke run projects more than 3.5 hours,** cut in this order until it doesn't (revised: the warm block is no longer on the list):
1. `pgdelta` unthrottled in the busy block (−5 runs, about 4 min). The registered rules use 0.5 and 2 Gbit/s.
2. `bracket` (−5 runs, about 3 min). The fast checkpoint's share is then read from each arm's own backup-start step.
3. The moderate block at 2 repeats (−6 runs, about 5 min).

If the projection is still over after all three, the session runs anyway and the overrun is recorded; nothing else is cut. **Never cut:** in the busy block, `none`, `capture`, and `full`, `pgdelta`, `v1proxy` and `v1proxy_s1` at 0.5 and 2 Gbit/s at 5 repeats, and `full` and `v1proxy` unthrottled; the warm block's `full` and `v1proxy` at 5 repeats, which name a miss's cause; `pause2`; G4's checks on every live output; the oracle's negative controls; `full_nosync` in the smoke run; the syscall audit; the calibration of `full`, `pgdelta`, `v1proxy` and v1 itself. Every cut is recorded under rule 8.

**Run lines** (the shape; the values come from the smoke run):

```sh
R_BUSY=18100; R_MOD=9000  # smoke item 2: 0.6 and 0.3 x C, rounded down to 100 per second
NOISE=0.48         # check (a)'s idle noise, MiB (smoke item 13); the limit is max(4 MiB, NOISE)
L_MS=110           # the late limit for the late-count view (smoke item 3); the analysis's only
C=30316; BUSY_CPU=0.628  # smoke item 2: C, and the busy CPU at R_BUSY as a share of the source's CPUs (selects E4's, E7's ranges)
NOISE_BYTES=499712 # smoke item 13: check (a)'s idle noise in bytes (run.json's dirty_idle_noise.max_increase_bytes)
python3 experiments/network/remote.py start exp12-hot -- --patterns hot \
    --speeds 0.5,2,unthrottled --repeats 5 --order-seed 121 --manifest-checksums CRC32C \
    --dirty-noise-mib $NOISE \
    --methods none,capture,bracket,full,pgdelta,v1proxy,v1proxy_s1 \
    --method-speeds none+capture+bracket=2 --method-speeds v1proxy_s1=0.5,2 \
    --load bank --load-rate $R_BUSY --load-clients 32 --load-threads 4 --load-warmup-s 120 \
    --settle-s 15 --settle-max-s 60 --tail-s 15 --none-window-s 60 \
    --source-cache evict --snapshot --static-digest first_repeat --controls pause2,burn4 \
    --warm-source-repeats 5 --warm-source-speeds 2 --warm-source-methods full,v1proxy \
    --moderate-rate $R_MOD --moderate-repeats 3 --moderate-speeds 2,unthrottled \
    --moderate-methods none,full,v1proxy,v1proxy_s1 --moderate-method-speeds none+v1proxy_s1=2 \
    --calibration-repeats 2 --calibration-speed 2 \
    --calibration-methods full,capture,pgdelta,v1proxy,v1proxy_s1,pgdelta_direct_inplace,burn4 \
    --sampler-interval-s 0.5  # smoke item 11
.venv/bin/python analysis/exp12.py --sweeps results/exp04/exp12-hot --late-limit-ms $L_MS \
    --null-resamples 10000 --null-seed 121 --client-wait-share 0.25 \
    --capacity-tps $C --busy-cpu $BUSY_CPU --idle-noise-bytes $NOISE_BYTES
```

`--settle-max-s` caps the source's settle gate only; dest's work has no cap (revised). The analysis line's `C`, `BUSY_CPU` and `NOISE_BYTES` (added October 24, after the harness build's review) carry smoke items 2 and 13 into A12, the choice of E4's, E7's, E9's, E14's and E16's ranges, and check (a): on the main sweep, `analysis/exp12.py` selects no busy-CPU branch without `--busy-cpu`, and refuses a placeholder 0 for C or the busy CPU.

## What it needs built in the harness

About 3.45 days (revised), in this order of priority. The registered rules need the load, the orchestration, the oracle and the analysis. If the proxy slips, the run goes ahead without it: decision 1 then follows R1 and R2 alone (row 4 where both leave the order open), decision 2 isn't judged, and the change is recorded.

| Block | File | Function or name | What |
|---|---|---|---|
| the load (about 0.6 day) | `experiments/network/load/commit.sql`, `abort.sql` (new) | – | the scripts above |
| | `experiments/network/source_cluster.py` | `loaddb-setup` (`pgbench -i -s 2 -F 90` into loaddb, `load_progress` with one row per client, `CREATE EXTENSION amcheck`, autovacuum off on loaddb's tables, the added server settings, a restart); `load-start` (each client's `n` and Σabalance recorded with the load stopped, then pgbench under `setsid`, its logs and `-P 1` lines on tmpfs, its PID recorded); `load-stop` (SIGALRM, then the log's completeness, then each client's `n` and Σabalance recorded); `load-capacity` (logged and offset the same way); `load-check` (the source's Σabalance and Σn against the sum of every logged run's commits, and each run's counts per client); `backup-hold` (one session: `pg_backup_start(label, true)`, prints the start LSN, waits on stdin, `pg_backup_stop(false)`, prints the stop LSN); `snapshot` (stop, copy, sync, digest, restart) | the load, the proxy's backup mode, the snapshot (revised: the offsets, amcheck) |
| | `experiments/network/agent.py` | `evict PATH... --exclude ...` (`posix_fadvise` DONTNEED per file); `fincore PATH...`; `burn --procs N --seconds S`; `load-pause SECONDS` (SIGSTOP and SIGCONT to pgbench's recorded PID, both times stamped; its only home, revised); `clock-probe` (a timestamp exchange with the dest agent: offset and round trip); `sampler start|stop`, with each pgbench thread's `schedstat` (revised); `env`: `chronyc tracking` and pgbench's version | eviction, controls, clocks, the sampler |
| | `infra/gcp/startup.sh` | `zstd`, and `fincore` where util-linux lacks it | packages |
| orchestration (about 0.85 day) | `experiments/network/run_exp04.py` | `parse_args()`: the options in the run line; `run_trial()`: the order above, the four blocks, the controls, the load's switch and its restart if it dies; `run_method()`: eviction instead of the source's cache drop, the settle gate with its cap on the source's gate only, the quiet tail, UTC t0 and t1 in every record, checks (a) and (b) read just before the clock and first after it, dest's verification only after that read, dest's work finished before the next window, its wait recorded (revised); `run_none()`, `run_capture()`, `run_bracket()`, `run_control()`; the calibration block (the load stopped; v1 itself stopped and restarted) | the blocks and the run |
| | `experiments/network/measure.py` | `PGDELTA_ENGINES`: `v1proxy` (`pgdelta_direct_inplace`'s engine) and `v1proxy_s1` with per-end jobs (`send_jobs` 1, `jobs` 4), `pgdelta_engine_args()` reading them; a new `V1PROXY_METHODS`, inside `PGDELTA_DIRECT_METHODS` so that every dispatch takes v1's path, with `SOURCE_STOP_METHODS` defined without it, so the harness never stops the live source for them (revised); a set for check (b), `FULL_COPY_METHODS` plus `pgdelta`, used by `durability_check`, with `FULL_COPY_METHODS` itself unchanged for EXP-11's analysis (revised); the new methods in `OPTIONAL_METHODS`, `TIERS`, `IN_PLACE_METHODS`; `PROC_SCOPE` for `none`, `capture`, `bracket`, `pause2`, `burn4`, `v1proxy` and `v1proxy_s1` (revised); `CSV_FIELDS`: `block`, `load_rate`, `t0_utc`, `t1_utc`, `clock_offset_s`, `window_buckets`, `worst_drop`, `work_lost_s`, `worst_drop_net`, `work_lost_net_s`, `late_count_s`, `lat_p50_ms` to `lat_max_ms`, `lat_p99_worst5s_ms`, `backlog_end`, `cpu_source_calibrated_s`, `cpu_source_excess_s`, `steal_s`, `client_wait_s`, `client_wait_worst_bucket_s`, `dirty_increase_bytes`, `md_write_ratio`, the flags, `g4_atomic`, `g4_cut`, `g4_coverage`, `g4_schema`, `amcheck_status`, `staleness_s`, `dest_wait_s` | the arms, pinned; records |
| the proxy (about 0.3 day) | `run_exp04.py` | `run_v1proxy()`: one phase, steps `backup_start`, `receiver_start`, `transfer`, `backup_stop`, dispatched from `run_phases()` ahead of `run_pgdelta_direct()` (revised); `method_paths()` giving `v1proxy*` the snapshot as `source_data`, and `pgdelta_send_argv()` reading it from there instead of `self.source_data` (revised); `verify_direct()` against step 5's stored digest, never reading the source (revised); `settle_records()` taking the snapshot's digest as `v1proxy*`'s reference (revised); A4 from pgdelta's counters and the LSNs, for both arms; `method_flags()` and `method_paths()` for every new arm, so none falls through (revised) | wiring |
| the oracle (about 0.6 day) | `run_exp04.py`, `measure.py` | `verify()` for `full` and `pgdelta`: `check_output(recovery=True)` with no `restore_command`, then the invariant query, `pg_amcheck` with its "skipping" lines parsed, the row counts, the schema dumps' hashes with `--restrict-key=exp12`, the first repeat's digest (revised); `load_oracle_sql()`; `consistent_cut(progress, logs, offsets)`, mapping each `n` to its run's log, and `coverage(cut, start, stop, offset)`, pending until the trial's end as `finish_logical_checks()` does; the staged capture with `--verbose` stamped over SSH; the "wrong" and "unverified" statuses, with any error from the copy's server or tools as wrong and only the listed harness-side failures as unverified (revised) | G4 |
| the analysis (about 0.75 day) | `analysis/exp12.py` (new) | stream the logs; scheduled and actual starts; W, D, latency, the worst 5-s span, the late count and every view per run; the block-bootstrap nulls for each statistic, each run's window at its own T, unsettled settles out, δ from the centred medians' difference (revised); the flags, *client wait* on W and on D (revised); the pause's gap against the agent's and dest's times; the coverage control; L1–L5 with their bounds, R1–R3, decisions 1 and 2 in their orders, with row 7's cause and 7b's test against the cold runs (revised), the 3-run minimum, A1–A13 in the order above; every smoke item printed from the records; `exp12-runs.csv`, `exp12-cells.csv`; figures `exp12-timeseries.svg` (completed over scheduled and p99 per second around t0, the median run of each arm and speed) and `exp12-loss.svg` (W_net and D_net per arm and speed, with the null's band and δ around `full`) | written and dry-run on smoke data before the sweep |
| tests (about 0.35 day) | `tests/test_exp12_load.py`, `tests/test_exp12_analysis.py`, `tests/test_exp04_network.py` | – | the scripts' invariant under concurrent clients, and the seven negative controls, each failing through its own check, on a local PostgreSQL 17; two schema dumps of one state hashing equal (revised); the cut mapped across a capacity run and a load run by their offsets (revised); the log parser on real pgbench lines; W and D on synthetic logs with known answers (a pause of 2 s at ρ gives W in [2 − (1 − ρ), 2] and D = 1); the bootstrap null on synthetic logs with a known spread, and δ centred for two cells of different T (revised); every row of every rule, "not measurable" on either side and "not judged" included; the clock mapping with a known offset; the timeline (nothing on the source in a settle, window or tail, by a stub that records commands); check (c): each arm's flush inside its timed phase and dest's verification after the last read of (a) and (b), by the same stub (revised); `v1proxy*` never stopping the source; for every new arm, its `run_phases()` and `verify()` path, its stats path, whether it stops the source and whether check (b) applies (revised); `run.json`'s `method_flags` pinning each new arm's command line (revised); `backup-hold` closing on stdin; per-end jobs in the argv; the run lines split as a POSIX shell splits them |

Docs: a "Methods for T10a" section in `experiments/network/README.md`, and this write-up as `docs/experiments/EXP-12-source-slowdown.md` with a row in the experiments index.

## Choices a reviewer should test

These stay open to question after the review. The harness build's review should test them too.

- The busy rate, 0.6 × C, and 0.3 × C as the second level, rather than a second workload or 0.8 × C.
- Cold reads by eviction in the deciding block, with the warm block beside it and naming a miss's cause.
- R1 governing where it conflicts with the October 24 note (change 4, the lead's decision), and the proxy deciding the order only where R1 and R2 leave it open (change 5).
- The tolerances' floors, the 0.15 × W(`full`) term, δ from the bootstrap of two medians' difference, and "meets" needing both numbers.
- The 25% share that flags "client wait", and 50% busy as the line between E4's and E7's ranges.
- W as the area of the dip, with the late count only as a view.
- Whether the one-core arm belongs here or in T10b.
- The snapshot proxy's gaps: no WAL streaming, no reading files while they are written, and A4's 20% CPU gate against v1 itself.
- 15-s settles and tails, and a window that ends 10 s after the copy.
- Row 7b's test: a warm meet, plus a fall below the proxy's own cold reading by more than δ (revised).
- A13's two scopes: `full` reclassified for the session, every other arm at that speed (revised).
- The 3-run minimum for every cell, with a judged miss counting before a short cell (revised).

## Results

**Run:** October 24, 2026, on two new `n2-standard-8` VMs in `us-central1-a`, created at 16:51 UTC and deleted at 20:10 UTC. That is about 3.3 hours of the pair, about 3.3 USD at about 1 USD per hour for the two, against the budget of about 3.4 hours (3.05–3.85) and the 3.5-hour cut line. No cut applied (smoke item 15). Nothing was fixed or rerun once the sweep started (A11).

| Step | UTC | Runs | Projected |
|---|---|---:|---|
| Create both VMs, provision, deploy | from 16:51 | – | 20 min |
| The smoke run: `exp12-smoke-cap`, `exp12-smoke`, `exp12-smoke-null`, `exp12-smoke-audit`, `exp12-smoke-nosampler` | 16:57–18:18 | 38 | 60 min (50–76) |
| The registered sweep, `exp12-hot` (one trial) | 18:23–20:08 | 117 | 108 min (98–120) |
| Fetch the results, check them against the VMs', delete the VMs | 20:08–20:10 | – | 15 min |

The smoke run took 81 minutes, with its chrony and sampler repeats (items 4 and 11); the sweep took 105. The results were fetched and checked equal to the VMs' copies (file counts and bytes, all six result sets) before the VMs were deleted.

**The trial, in order** (from its records): the cluster, loaddb and B0 from 18:23; the capacity runs 18:25:50–18:27:23; the busy load from 18:27:53, its warm-up to 18:29:53; the busy block's windows 18:30–19:31; the warm block's 19:32–19:39; the busy load's stop signal at 19:39:07 (its counters read at 19:40:31); the moderate load from 19:40:31, its warm-up to 19:42:31; the moderate block 19:42–19:58; the moderate load's stop signal at 19:57:56 (its counters read at 19:58:06); the calibration block 19:59–20:06; the end references to 20:08.

- **Runs:** 117, as planned: 74 in the busy block (its 4 controls included), 10 in the warm block, 19 in the moderate block (its `pause2` included) and 14 in the calibration block. Every run passed, and none left a median.
- **Software:** PostgreSQL 17.11 on both VMs. pgdelta's code is EXP-11's: the diff of `pgdelta/` from EXP-11's tree, recorded at deploy time, is empty. The harness ran at commit b628c43 on both VMs.
- **The run line** is the one under [Run lines](#run-plan-and-budget), as launched, with the smoke run's values: `R_BUSY=18100`, `R_MOD=9000`, `NOISE=0.48`, `--sampler-interval-s 0.5`.
- **Data:** [exp12-runs.csv](data/exp12-runs.csv) (one row per run: times, O4's numbers and their views, latency, CPU, the flags, G4's checks, durability, bytes) and [exp12-cells.csv](data/exp12-cells.csv) (each block, speed and arm, in the main view and every robustness view).
- **Analysis:** [analysis/exp12.py](../../analysis/exp12.py), written and dry-run on the smoke data before the sweep (smoke item 16), run exactly as registered: `.venv/bin/python analysis/exp12.py --sweeps results/exp04/exp12-hot --late-limit-ms 110 --null-resamples 10000 --null-seed 121 --client-wait-share 0.25 --capacity-tps 30316 --busy-cpu 0.628 --idle-noise-bytes 499712`.
- **The drift check:** [analysis/exp12_drift.py](../../analysis/exp12_drift.py), defined at 19:46 UTC during the sweep (commit 8488bbc), its script and tests committed at 20:07 UTC (59a9f80), both before the sweep's logs were fetched, run as `.venv/bin/python analysis/exp12_drift.py --sweep results/exp04/exp12-hot --runs docs/experiments/data/exp12-runs.csv`. After the fetch, one commit (ea46b31) changed only its reported per-minute view: it now stops at the load's stop signal instead of pgbench's exit, prints per-block medians instead of the medians over the first and last 10 minutes after the warm-up, and marks the warm block's reads. On this sweep the replaced summary would have read, for the busy load, p50 0.680 and 0.663 ms and p99 1.44 and 6.9 ms (the last 10 minutes hold the warm block's reads), with a lowest minute of 4,040 completions per second, the minute that ran past the stop signal (recomputed from the pgbench logs). It was reported, not judged, as is its replacement. The judged part is unchanged.
- **Where the numbers come from:** the two scripts' outputs, unless the text says a number was recomputed, either from exp12-runs.csv or exp12-cells.csv, or from the raw records (git-ignored, under `results/exp04/exp12-hot/`: the run and trial records, pgbench's per-second progress lines and the harness's stamped command logs).
- **The analysis's smoke-item lines are this sweep's, not the smoke run's.** Items 3, 11 and 13 re-read the sweep's records, so they print "no data" or "–" where an item's data exist only in the smoke run. Item 3 prints the L this sweep's warm-up would give: 100 ms, from a p99 of 9.988 ms. L = 110 ms was fixed by the smoke run, as registered, and is the only L used. The smoke run's items are judged in its own dated section above.
- **Two things moved during the session.** This trial's own C was 10.9% below the smoke run's (A12), and loaddb grew from 4,224 to 48,968 pages, against smoke item 14's limit of 5% (see [loaddb's growth and the drift check](#loaddbs-growth-and-the-drift-check)). The registered drift check found no drift in either load. A10 marks the session shifted against EXP-11 on `full`'s idle time; that copy was 20% larger than EXP-11's (see [Drift against EXP-11](#drift-against-exp-11-a10-and-capacity-a12)).

### Corrections after the recheck (October 24, 2026)

An independent recheck read this write-up and the ledger against the raw data from four sides (numbers, verdicts, summaries, and departures from the registered text) and made 46 findings, 11 of them major. Every one was checked against the run records, the pgbench logs and the sampler's records; all held, some in part, and together they make 35 distinct corrections. No registered verdict changes. No code changed, so the analysis wasn't rerun, and the curated CSVs and figures are unchanged. The pre-registered and dated pre-run sections are unchanged. The major ones:
- **The warm block didn't show a larger miss.** "By more than cold" and "the miss grew" are gone. Warm `v1proxy`'s net values, 0.087 s and 0.067, are within δ of the cold 0.069 s and 0.038 and inside E15's band, and the warm cell's median run is the unsettled one. Row 7a needs only that warm `v1proxy` also misses against warm `full`, which it does. The CPU comparison that went with it is restated without the unsettled run.
- **`pg_stat_wal` shows the stall.** The write-up said its sync time couldn't show why the load stalls during S's flush. Its per-second counts do: in every staged run the WAL's syncs completed far more slowly during the flush (2–1,979 in the slowest second, against 3,400–5,800 before), and in no other arm. Why they waited stays a hypothesis.
- **The fragile flag on the rows beside the decisions.** The Load row (the moderate meet) and the Warm source row rest on cells flagged *client wait* in every run, so by A9 they are fragile, as the HA caveat and R1 were already marked. The flag now goes with the moderate result wherever it is cited.
- **The client's wait is read against its controls.** Over a window it is large wherever a copy uses CPU (`full`: 0.27–0.74 s with no measurable loss), so it doesn't measure the client's share of the proxy's loss; the worst second's wait (0.30–0.38 s for `v1proxy`, at most 0.10 for `full` and `v1proxy_s1`) is what separates them.
- **One worker's time against a full copy carries its caveats.** The proxy sent 29.5 MB from the snapshot taken before the load; v1 itself sent 449 MB here and took 1.48 s longer idle. And the bar is `pg_basebackup`, not rule 3's fastest copy, EXP-11's bound, which has no slowdown number until T8: a caveat the pre-registration asked to state with every result, and which was missing.
- **A6.** The cores reading of A6's numbers is a hypothesis for T10b, not a finding, and its ratio is about 1/60 (1/58–1/64), not 1/60 to 1/70. The ledger's "not in CPU-seconds" is gone.
- **In the ledger:** D81 moves from D to C by "contradicted in part", as D50 and D68 did, since decision 2 gives it a basis and A6 was judged; D58's note separates backup mode alone (nothing measurable) from the proxy at 4 workers (fragile).

The smaller corrections: staged pgdelta's worst second as O4's bucket ([t0 + 4 s, t0 + 5 s) in all 20 runs, 189–2,368 completed), not progress-line seconds; the flush step starts 3.5–3.9 s after t0; the loads' ends at their stop signals; the settle gate's log part passed in 102 of 103 runs; the warm block's fixed arm order; the per-minute view's first five minutes, and the warm reads' overlap with their settles; the drift check's commit times and the summary that ea46b31 replaced; E6's WAL, E10's speeds and E15's margin (0.0014 s); the pages v1 itself sent and hashed; the run queue, read as E7's model rather than as a finding; `full`'s unthrottled latency and the figure note; R1's note on the flush; A12 in conclusion 7; and the summaries in the ledger, the README, the experiments index and the log.

### Runs and verification: G4, R3, the oracle and durability

G4's checks on every output taken from the live source, the started copies of `full` and staged `pgdelta` (G4's boundaries as medians):

| Block | Arm | Outputs | Passed every check | WAL end point before publication, s | Usable after publication, s | Staleness, s (max) |
|---|---|---:|---:|---:|---:|---|
| busy | `full` | 15 | 15 | 1.43 | 1.17 | 1.43 (1.59) |
| busy | `pgdelta` | 15 | 15 | 7.71 | 0.58 | 7.71 (13.51) |
| warm | `full` | 5 | 5 | 1.65 | 1.17 | 1.66 (1.67) |
| moderate | `full` | 6 | 6 | 1.59 | 0.51 | 1.60 (1.65) |
| calibration | `full` | 2 | 2 | 1.42 | 0.26 | – (no load) |
| calibration | `pgdelta` | 2 | 2 | 7.83 | 0.28 | – (no load) |

- **Every live output passed every check:** 45 outputs. Each passed `pg_verifybackup` with its CRC32C manifest; recovered from its own `backup_label` with only the WAL it holds, and started; held the balance invariant (iii), a consistent cut (iv) and coverage (v) against the load's logs; matched the benchmark database's row counts (vi), and its per-table logical digest in each busy cell's first repeat (6 outputs); hashed equal to the schema references (vii); and passed `pg_amcheck --heapallindexed` over 283 relations. For staged `pgdelta`, S also passed `pg_verifybackup` and O was byte-identical to S.
- **Staleness without load means nothing.** In the calibration block no load runs, so the cut is the last commit before the load stopped, and staleness (228 and 283 s, medians) is only the time since then.
- **Every other output was verified too:** the proxies' 43 outputs (`v1proxy` 28, `v1proxy_s1` 15) were byte-identical to the snapshot's selection, by step 5's stored digest, and started; v1 itself (2 runs) matched the stopped source; S passed `pg_verifybackup` in all 7 `capture` runs. No run was unverified.
- **R3 doesn't fire:** no output of `pgdelta`, `v1proxy`, `v1proxy_s1` or v1 itself was wrong. L5 holds over all 117 runs.
- **A1 doesn't fire:** no `full` output was wrong. The analysis-only coverage control failed (v) in all 41 pairings it checked, as it must. The seven negative controls ran in the smoke run (item 5).
- **The trial's references held** (trial record): the benchmark database's logical digest at the end equalled the start's; the schema dumps hashed equal; the snapshot's digest equalled step 5's; B0 was unchanged in every run and at the end.
- **`full`'s cut lay 0.001–0.072 s before its "write-ahead log end point" stamp** in all 26 outputs taken under load (E10).

**Durability on dest** (check (a)'s limit: 4 MiB, above the smoke run's idle noise of 0.48 MiB):

| Block | Arm | Runs | (a) pass / fail | (b) pass / fail | Largest increase, MB | Smallest disk writes ÷ output |
|---|---|---:|---|---|---:|---:|
| busy | `full` | 15 | 15 / 0 | 15 / 0 | 1.4 | 1.000 |
| busy | `pgdelta` | 15 | 15 / 0 | 15 / 0 | 1.7 | 1.019 |
| busy | `v1proxy`, `v1proxy_s1` | 25 | 25 / 0 | – | 1.0 | 0.014 |
| warm | `full` | 5 | 5 / 0 | 5 / 0 | 1.2 | 1.001 |
| warm | `v1proxy` | 5 | 5 / 0 | – | 0.5 | 0.015 |
| moderate | `full` | 6 | 6 / 0 | 6 / 0 | 1.4 | 1.000 |
| moderate | `v1proxy`, `v1proxy_s1` | 9 | 9 / 0 | – | 0.6 | 0.014 |
| calibration | every arm that writes on dest | 10 | 10 / 0 | 4 / 0 | 1.3 | 0.014 |

- **A13 doesn't fire:** no run failed (a) or (b). The proxies patch B in place, so their disk writes are about 1.4% of the output and check (b) doesn't apply.
- **The negative control and the syscall audit ran in the smoke run** (item 13): `full_nosync` left 2.29 GB dirty, so check (a) stays in force, and `full`, `pgdelta` and `v1proxy` passed the audit under the load.

### The instrument and the load

**The load** (smoke items 1 and 2, read from this trial's records):

| pgbench run | Rate | When (UTC; the rated loads to their stop signal) | Commits | Rollbacks | Complete | Failed | Log, MB per hour |
|---|---|---|---:|---:|---|---:|---:|
| capacity warm-up | closed loop, 30 s | 18:25:50–18:26:21 | 792,754 | 8,034 | yes | 0 | 900.5 |
| capacity | closed loop, 60 s | 18:26:21–18:27:23 | 1,604,780 | 16,227 | yes | 0 | 916.1 |
| busy | 18,100 per second | 18:27:53–19:39:07 (71.2 minutes; counters read at 19:40:31) | 76,590,214 | 774,224 | yes | 0 | 719.5 |
| moderate | 9,000 per second | 19:40:31–19:57:56 (17.4 minutes; counters read at 19:58:06) | 9,304,235 | 93,720 | yes | 0 | 352.9 |

- **Every run was complete:** for each client, `n` at the stop minus `n` at the start equals its log's commits. No transaction failed. On no line was the latency below the schedule lag, and completion times never fell within a client. The rollbacks were 1.0% of the transactions, as the scripts' weights ask.
- **Step 15 held:** Σabalance = Σn = the logged commits = **88,291,983**, with no incomplete run and no gap.
- **C** (step 7) was 27,019 transactions per second, 10.9% below the smoke run's 30,316 (A12, below). So the busy rate, 18,100 per second, was 0.670 of this trial's C (0.60 of the smoke run's), and the moderate rate 0.333 (0.30). The busy CPU at R_busy, 0.628 of the source's 8 CPUs, is smoke item 2's, passed to the analysis as registered; at R_mod the settles' median was 0.369.
- **The warm-ups:** the busy load's p99 was 9.99 ms over its 120 s (10.21 ms over its last minute, the null's part); the moderate load's 0.87 ms.

**The positive control and the clocks** (A2, A3 and the clock gate):

| `pause2` run | ρ | W, s (A2's bounds) | D | Gap in the log against SIGSTOP / SIGCONT, ms | Margin, ms | Inside dest's mapped phase | Late count at L, s |
|---|---:|---|---:|---|---:|---|---:|
| busy block's start, unthrottled visit | 0.670 | 1.908 ([1.47, 2.3]) | 1.000 | 0.00 / 0.06 | 16.98 | yes | 5.45 |
| busy block's end, 0.5 Gbit/s visit | 0.670 | 1.919 ([1.47, 2.3]) | 1.000 | 0.02 / 0.07 | 6.16 | yes | 5.02 |
| moderate block's start, 2 Gbit/s visit | 0.333 | 1.898 ([1.13, 2.3]) | 1.000 | 0.05 / 0.60 | 5.88 | yes | 2.79 |

- **A2 doesn't fire** in either block: the logging, the arithmetic and the clock mapping return a 2.0-s pause as 1.90–1.92 s of lost work and a whole bucket at zero, with the log's gap within 0.6 ms of the agent's signals.
- **A3 doesn't fire:** `none`'s median completed over scheduled was 1.0000 in both blocks, and its largest window-end backlog was 6 transactions (busy) and 2 (moderate), against R × 1 s.
- **The clock gate holds:** the harness's probe put the source 0.000 ms from dest at the trial's start and 0.006 ms at its end; chrony read both VMs within 1 µs of NTP time.
- **The harness's own checks** (smoke items 6–11, read from this trial's records): 107 evictions left at most 0.01% of the benchmark database's files and the snapshot resident, and at least 99.33% of loaddb; the settle gate's progress-line part passed within 30 s in all 103 loaded runs, and its log part (the settle's p99 at most twice the warm-up's, read afterwards) in 102: the first warm run's failed, and it is flagged *unsettled* (below); the sampler used 0.9% of a core at 0.5-s reads, with none of its 10,866 ticks late; all 24 staged captures' `--verbose` lines parsed with their end point; and the proxy held backup mode across its transfer, hashed every byte and verified in 43 of 43 runs, the source never stopped for it.

### The null

| Block | Pool | One window: W median / 95th percentile, s (T = 15, 30, 50) | One window: D median / 95th percentile (T = 15) | Median of 5 windows at T = 15: W's 95th percentile, s |
|---|---|---|---|---:|
| busy (and warm) | 84 spans, 1,685 s (15 s more with the unsettled settle) | 0.0022 / 0.0117, 0.0045 / 0.0162, 0.0077 / 0.0209 | 0.0006 / 0.0100 | 0.0028 |
| moderate | 20 spans, 573 s | 0.0024 / 0.0037, 0.0050 / 0.0067, 0.0083 / 0.0104 | 0.0008 / 0.0011 | 0.0031 |

The busy pool's own latency over 30.9 million transactions: p50 0.653 ms, p95 0.958, p99 1.322, p99.9 11.07, maximum 37.6. The moderate pool's: p50 0.577 ms, p99 0.856. E2 held but for one clause: D's null median at T = 15 was 0.0006, below the 0.005–0.03 predicted. δ_W was its 0.05-s floor and δ_D its 0.03 floor in every comparison.

### The cells: O4's two numbers, with time beside them

The busy block, medians of 5 runs (maximum in brackets), net of the cell's null; "meas." says whether the median exceeds the null's 95th percentile. ΔW and ΔD are the arm's net value less `full`'s at the same speed, against δ_W = 0.05 s and δ_D = 0.03. Latency is over [t0, t1], from each transaction's scheduled start:

| Speed | Arm | Time under load, s | W, s | W_net, s (meas.) | D | D_net (meas.) | ΔW | ΔD | Meets O4 | p95, ms | Worst 5-s p99, ms |
|---|---|---:|---|---|---|---|---:|---:|---|---:|---:|
| 0.5 Gbit/s | `full` (the bar) | 47.19 | 0.008 (0.010) | 0.000 (no) | 0.001 (0.001) | 0.000 (no) | – | – | – | 1.00 | 10.1 |
| | `pgdelta` | 14.99 | 1.269 (1.341) | 1.265 (yes) | 0.969 (0.990) | 0.968 (yes) | +1.265 | +0.968 | **no** | 1,166.6 | 1,391.0 |
| | `v1proxy` | 3.69 | 0.053 (0.079) | 0.051 (yes) | 0.051 (0.067) | 0.051 (yes) | +0.051 | +0.051 | **no** | 68.9 | 92.0 |
| | `v1proxy_s1` | 7.55 | 0.003 (0.003) | 0.000 (no) | 0.001 (0.001) | 0.000 (no) | +0.000 | +0.000 | yes | 1.63 | 11.6 |
| 2 Gbit/s | `full` (the bar) | 11.92 | 0.004 (0.004) | 0.000 (no) | 0.001 (0.001) | 0.000 (no) | – | – | – | 1.34 | 10.7 |
| | `none` | 60.00 | 0.010 (0.012) | 0.000 (no) | 0.001 (0.001) | 0.000 (no) | −0.000 | −0.000 | yes | 0.94 | 2.6 |
| | `capture` | 5.35 | 1.199 (1.348) | 1.197 (yes) | 0.956 (0.974) | 0.955 (yes) | +1.196 | +0.955 | **no** | 1,332.3 | 1,401.9 |
| | `pgdelta` | 11.18 | 1.350 (1.374) | 1.346 (yes) | 0.962 (0.986) | 0.961 (yes) | +1.346 | +0.961 | **no** | 1,212.0 | 1,383.7 |
| | `bracket` | 0.21 | 0.001 (0.002) | 0.000 (no) | 0.000 (0.001) | 0.000 (no) | −0.000 | −0.000 | yes | 11.8 | 8.0 |
| | `v1proxy` | 3.62 | 0.071 (0.108) | 0.069 (yes) | 0.038 (0.072) | 0.038 (yes) | +0.068 | +0.038 | **no** | 96.8 | 124.6 |
| | `v1proxy_s1` | 7.25 | 0.004 (0.010) | 0.001 (yes) | 0.001 (0.007) | 0.000 (yes) | +0.001 | +0.000 | yes | 1.83 | 14.1 |
| unthrottled | `full` (the bar) | 4.29 | 0.002 (0.003) | 0.000 (no) | 0.001 (0.002) | 0.000 (no) | – | – | – | 6.41 | 17.1 |
| | `pgdelta` | 10.26 | 1.257 (1.396) | 1.254 (yes) | 0.959 (0.970) | 0.958 (yes) | +1.254 | +0.958 | **no** | 1,211.3 | 1,360.9 |
| | `v1proxy` | 3.61 | 0.072 (0.105) | 0.070 (yes) | 0.043 (0.061) | 0.042 (yes) | +0.070 | +0.042 | **no** | 131.8 | 161.3 |
| controls (2 runs) | `pause2` | 2.11 | 1.914 (1.919) | 1.912 (yes) | 1.000 (1.000) | 0.999 (yes) | – | – | – | 1,967.1 | 1,986.0 |
| | `burn4` | 2.12 | 0.235 (0.321) | 0.233 (yes) | 0.126 (0.167) | 0.125 (yes) | – | – | – | 273.9 | 291.9 |

Every cell with a measurable W is flagged *client wait* (A8): `pgdelta`, `capture`, `v1proxy`, `v1proxy_s1` at 2 Gbit/s and both controls (see [Flags](#flags-and-robustness-views)).

What the cells show:
- **One `pg_basebackup` stream costs the busy load nothing measurable on O4's two numbers (W and D),** at 0.5 or 2 Gbit/s or unthrottled. So O4's bar here is "no measurable loss, within δ", as the notes before running said it might be. Its latency isn't untouched: unthrottled, over its 4.3-s copy, the load's p95 was 6.4 ms and its p99 17.6 ms, against the null pool's 0.96 and 1.32 (at 0.5 and 2 Gbit/s, p95 1.00 and 1.34 ms).
- **Staged pgdelta misses O4 by a wide margin at every speed:** 1.25–1.35 s of the load's work lost, and in its worst second 3–4% of the scheduled transactions completed (D 0.96–0.97). The load's p95 latency over its window was 1.17–1.21 s.
- **The capture alone loses almost as much,** 1.20 s and D 0.96, with no link involved.
- **v1's source side by proxy, at 4 workers, misses O4 at every speed, on both numbers, by a small margin:** 0.05–0.07 s of work and a worst second 4–5% short. Its effect on latency is larger: over its 3.6-s copy the load's median latency was 6.8–17 ms and its p95 69–132 ms, against 0.65 ms and 0.96 ms in the null.
- **One source worker meets O4 at both speeds:** its net losses were at most 0.001 s and 0.000 (measurable at 2 Gbit/s, but far inside δ), and its p95 was 1.6–1.8 ms.
- **Backup mode alone (`bracket`) and an empty window (`none`) cost nothing measurable.** `burn4`, 4 busy processes for 2 s, lost 0.23 s and 13% of its worst second.

![EXP-12: the load around each refresh, the median run of each arm and speed](../figures/exp12-timeseries.svg)

How to read it: one column per busy-block speed, and one for the controls. Each line is the median run of an arm's cell, against seconds from t0, the start of its first timed phase. The top row is completed over scheduled transactions per 1-s bucket: 1.0 means no loss, a dip below it is lost work, and the rise above it after a dip is pgbench catching up. The bottom row is the p99 latency per second, on a log scale. Staged pgdelta (orange) falls to near zero for one second about 4–5 s after t0 at every speed, and its p99 stays above 1 s for 3–5 s while the backlog drains; the capture alone (yellow-orange, 2 Gbit/s) does the same. `v1proxy` (green) dips by a few percent at t0 + 1 s, overshoots, and its p99 rises to 100–180 ms for 2–3 s. `full` (grey) and one source worker (light blue) stay at 1.0. At 0.5 and 2 Gbit/s their p99 spikes of 10–15 ms also appear before t0, in load-only time; unthrottled, `full`'s p99 rises to 12–15 ms at t0 and t0 + 2 s, inside its 4.3-s copy. `pause2` (blue) stops the load for 2 s by design; `burn4` (yellow) dips to about 0.92.

![EXP-12: each arm's net loss in the busy block](../figures/exp12-loss.svg)

How to read it: W_net (left) and D_net (right) for each arm and speed in the busy block, medians of 5 runs. The black bar over each arm marks `full`'s net value plus δ at that speed: an arm whose bar rises above it misses O4 on that number. The null's grey band is too narrow to see at this scale, and no cell is short (none is hatched). Staged pgdelta and the capture lose 1.2–1.35 s and nearly a whole second; `v1proxy` sits just above the bar at every speed; one source worker, `bracket` and `none` sit at zero.

**Time next to slowdown (A7)**, medians of `total_s` under load; W_net (s), D_net and p95 (ms) beside each:

| Arm | busy 0.5 Gbit/s | busy 2 Gbit/s | busy unthrottled | warm 2 Gbit/s | moderate 2 Gbit/s | moderate unthrottled | idle, 2 Gbit/s (calibration) |
|---|---|---|---|---|---|---|---:|
| `full` | 47.19 s; 0.000, 0.000, 1.0 | 11.92 s; 0.000, 0.000, 1.3 | 4.29 s; 0.000, 0.000, 6.4 | 12.72 s; 0.000, 0.000, 1.2 | 12.65 s; 0.002, 0.002, 0.9 | 4.44 s; 0.001, 0.000, 1.0 | 12.07 |
| `capture` | | 5.35 s; 1.197, 0.955, 1,332 | | | | | 3.89 |
| `pgdelta` | 14.99 s; 1.265, 0.968, 1,167 | 11.18 s; 1.346, 0.961, 1,212 | 10.26 s; 1.254, 0.958, 1,211 | | | | 9.95 |
| `bracket` | | 0.21 s; 0.000, 0.000, 11.8 | | | | | – |
| `v1proxy` | 3.69 s; 0.051, 0.051, 68.9 | 3.62 s; 0.069, 0.038, 96.8 | 3.61 s; 0.070, 0.042, 131.8 | 3.12 s; 0.087, 0.067, 118.4 | 3.23 s; 0.001, 0.000, 2.6 | 3.19 s; 0.001, 0.000, 2.5 | 2.90 |
| `v1proxy_s1` | 7.55 s; 0.000, 0.000, 1.6 | 7.25 s; 0.001, 0.000, 1.8 | | | 6.78 s; 0.000, 0.000, 0.9 | | 5.40 |
| v1 itself | | | | | | | 4.38 |

- **Staged pgdelta wins on time at both registered speeds** under load (14.99 s against 47.19 at 0.5 Gbit/s, 11.18 against 11.92 at 2), so R1 is judged at both. It was faster than `full` at 2 Gbit/s in each of the 5 repeats (recomputed from exp12-runs.csv). Unthrottled it took 10.26 s against 4.29.
- **One source worker costs time:** `v1proxy_s1` took 7.25–7.55 s under load against `v1proxy`'s 3.61–3.69, under `full`'s 11.92 and 47.19 s at 2 and 0.5 Gbit/s. That comparison favours the proxy. Its send was 29.5 MB from the snapshot taken before the load; `full` copied 2,326–2,530 MB at 2 Gbit/s and 2,594–2,893 MB at 0.5, loaddb's growth and the load's WAL included. v1 itself, reading the stopped source after the load in the calibration block, sent 449 MB and took 4.38 s idle against the proxy's 2.90. So a live v1 at one worker would take longer than the proxy, most at 0.5 Gbit/s, where the ~420 MB of loaddb's changes alone take about 6.7 s on the link; at 2 Gbit/s, adding only the idle difference (+1.48 s) gives about 8.7 s, 0.73 of `full`'s 11.92. These are at 0.1 ms round-trip time, not DR's (T12). And `full` is O4's bar, not rule 3's fastest copy: EXP-11's bound (9.32 s at 2 Gbit/s, idle) reads a stopped source and has no slowdown number until T8. `v1proxy_s1` didn't run unthrottled. Its time didn't depend on the link (7.55 and 7.25 s), so on the fastest link it would most likely take longer than one `pg_basebackup` stream (4.29 s); that is an inference, not a measurement.
- **The load slowed the copies:** `v1proxy` took 25% longer than idle, `v1proxy_s1` 34%, the capture 38% and staged `pgdelta` 12% (E6). `full`'s copies grew over the session: loaddb's growth (see [loaddb's growth](#loaddbs-growth-and-the-drift-check)), and the WAL it streamed, 379–418 MB at 0.5 Gbit/s and 107–114 MB at 2 (recomputed from the run records).

### Where staged pgdelta's loss comes from (A5)

**A5: "Most of staged pgdelta's slowdown is its capture, which v1 removes."** The capture alone lost 1.197 s of work, against staged pgdelta's 1.265, 1.346 and 1.254 s at 0.5, 2 Gbit/s and unthrottled: 89–95% of it.

When it happens (recomputed from the raw records: each capture's stamped `--verbose` lines against pgbench's per-second progress lines): in all 20 busy-block `capture` and `pgdelta` runs, the load's slowest second overlaps the capture's `syncing data to disk` step. That step starts 3.5–3.9 s after t0 (measured from each stamped command's launch) and lasts 1.49–1.68 s. O4's worst 1-s bucket is the same in all 20 runs, [t0 + 4 s, t0 + 5 s), inside that step in each: in it the load completed 189–2,368 of the 17,943–18,425 transactions scheduled (D 0.871–0.990; recomputed from the per-transaction logs). A second, smaller rise in p99 comes later, during the transfer (the time series, 0.5 Gbit/s panel).

**During S's flush, WAL syncs completed far more slowly, in every staged run and in no other arm** (`pg_stat_wal`, read every second by the sampler; recomputed from its records). Before the flush, 3,400–5,800 syncs completed per second, at 0.15–0.25 ms each on average. In the second with the fewest, in each of the 20 runs, only 2–1,979 completed, while sync time went on accruing at about a second per second (0.8–2.4 s over that second and the next). So each sync took far longer, up to about a second where only 2 or 3 completed in a second (`r2-2g-capture`: 2 syncs and 6 ms at +4.1 s, then 2,025 ms of sync time at +5.1 s). In every other arm's window, in every block, at least 3,112 syncs completed per second, at 0.26 ms or less each on average; only `pause2`, which stops the load itself, had fewer. The backends report these counters with up to about a second's delay, so the split between neighbouring seconds is approximate. With synchronous commits a commit returns only once its WAL is synced, so the load's commits waited on these syncs. As registered, a stall is blamed on WAL syncs only if `pg_stat_wal`'s sync time shows it; here it does. Why the syncs waited during S's flush isn't shown: E7's basis, that on ext4's ordered journal a WAL fsync can wait behind S's flush, and the shared array's device queue are both hypotheses.

### v1's source side by proxy, at 4 workers and at 1

The proxy passed its gate (A4) in every run, cold and warm: backup mode held around the transfer with its start and stop LSNs, every byte of the snapshot's selection hashed, and calibrated CPU-s within 20% of the reference: `v1proxy` 5.84 against v1 itself's 6.72 (−13%), `v1proxy_s1` 4.90 against 5.84 / 1.06 = 5.51 (−11%).

| | `v1proxy` (4 source workers) | `v1proxy_s1` (1 source worker) |
|---|---|---|
| Meets O4, busy block | no, at 0.5 and 2 Gbit/s and unthrottled, on W and on D | yes, at 0.5 and 2 Gbit/s |
| W_net, D_net | 0.051–0.070 s, 0.038–0.051 | 0.000–0.001 s, 0.000 |
| Load's p50 / p95 / p99 over [t0, t1], ms | 6.8–17.2 / 68.9–131.8 / 96.4–164.5 | 0.73–0.74 / 1.63–1.83 / 10.6–12.4 |
| Time under load, s | 3.61–3.69 | 7.25–7.55 |
| Calibrated CPU-s; under load, CPU-s | 5.84; 5.51–6.12 | 4.90; 5.46–5.92 |
| pgbench threads' added run-queue wait over the window, s (cell medians) | 0.88–0.91 | 0.63–0.66 |
| The same, in the window's worst second, s | 0.32–0.34 | 0.00–0.08 |

- **What the sampler shows is consistent with E7's model, four workers oversubscribing the source and one fitting better; it doesn't show that one fits.** At R_busy the source's 8 CPUs were 63% busy, about 3 spare. During `v1proxy`'s window its processes added 6.9–7.0 CPU-s, while pgbench got about 1 CPU-s less than at the settle's rate and the load's backends 0.6–1.0 less. The run queue (`procs_running`) averaged about 8–10 in the busy settles, on 8 CPUs; over [t0, t1] it averaged 16–21 with `v1proxy` (cell medians 16.4–19.1) and 10–14 with `v1proxy_s1` (10.9–11.9). One worker raised it too, by less, and with it the load's backends got 0.55 CPU-s less at 2 Gbit/s. The clearest contrast is the threads' added wait in the window's worst second: 0.32–0.34 s with 4 workers against 0.00–0.08 with 1 (cell medians, the table above). The run queue is recomputed from the sampler's records; all of it is a mechanism only.
- **The client's own wait over a window doesn't measure its share of the loss.** In each busy-block `v1proxy` run, pgbench's threads waited 8.5–42 times its W in the run queue over the window, and 4.5–19 times the worst second's lost time in that second; in the warm `v1proxy` runs 5.2–12 and 3.6–6.6 times (recomputed from exp12-runs.csv). But the window's total is large wherever a copy uses CPU, loss or not: the busy `full` runs added 0.27–0.74 s of wait with no measurable loss, and `v1proxy_s1` 0.56–0.69 s with none at 0.5 Gbit/s and one far inside δ at 2; `v1proxy` added 0.84–1.02 s. What separates the proxy at 4 workers is the wait in the worst second: 0.30–0.38 s per run, against −0.01 to 0.08 for `full` and −0.01 to 0.10 for `v1proxy_s1`. The load's backends, which an application on another host would also depend on, got 0.6–1.0 CPU-s less in the proxy's windows. So how much of this small loss an application on another host would see isn't known. That is what the *client wait* flag marks (below).
- **The time it costs:** one worker doubles the proxy's time under load (7.25–7.55 s against 3.61–3.69).
- **What the proxy leaves out grew in this session.** It reads the snapshot taken before the load, so it sent 29.5 MB in every run, and hashed 263,415 pages. v1 itself, reading the stopped source after the load, sent 449 MB in the calibration block, 54,749 pages in all, of which `load_progress`'s are at most 44,775 (367 MB), its size at the end; and it hashed 310,891 pages, 18% more than the proxy (recomputed from the run records). A live v1 here would have had loaddb's changes to send too, about 420 MB by the session's end rather than the 30 MB the design expected, 18% more pages to read and hash, and the WAL the load writes during its copy.

### The warm block and the moderate block

**The warm block** (2 Gbit/s, 5 repeats; the benchmark database's files and the snapshot read into the cache before each settle):

| Arm | Time, s | W_net, s | D_net | ΔW / ΔD against warm `full` | Meets O4 | p95, ms |
|---|---:|---:|---:|---|---|---:|
| `full` | 12.72 | 0.000 | 0.000 | – | – | 1.2 |
| `v1proxy` | 3.12 | 0.087 | 0.067 | +0.087 / +0.067 | **no** | 118.4 |

- **Warm, the proxy still misses against warm `full`,** so row 7's reading is **7a, hashing**, as registered and as E15 expected. Its net values didn't fall against cold: 0.087 s and 0.067 warm against 0.069 s and 0.038 at 2 Gbit/s cold. The differences, +0.019 s and +0.030, are inside δ (0.05 s and 0.03) and inside E15's no-change band (W at most +0.02 s, D within ±0.03), so they show no change, not a rise. The warm cell's median run is the unsettled first one (below); the other four give 0.077 s and 0.056 raw, about 0.075 and 0.055 net. Per run, raw W overlaps: warm 0.052–0.104 s, cold 0.042–0.108 (exp12-runs.csv). Without the cold read the proxy finished sooner (3.12 s against 3.62). Its CPU under load (median 4.74 CPU-s) isn't comparable: it includes the first run's 3.05, which that run's backlog-inflated settle rate distorts; without it the median is 5.04, inside the cold runs' 4.26–7.40 at 2 Gbit/s. So the miss doesn't need the cold read. Row 7a doesn't say the cold read costs the load nothing, and the warm block always ran after the busy block, so warm against cold is also later against earlier in the session.
- **The first warm run is flagged *unsettled*.** It is `v1proxy`'s because the seeded draw put `v1proxy` before `full` in all 5 warm repeats (below). Its read of the benchmark database and the snapshot came from evicted files and took 2.40 s, the whole untimed step 2.77 s. pgbench's progress lines show the load stalling while it ran (a mean latency of 904 ms in one second), and the backlog drained in the settle's first 2 s, which started as the read ended. That settle's p99 was 435 ms, more than twice the warm-up's, so its gate's log part failed, and it left the null. The run's window started 15 s later. Its W and D (0.089 s and 0.068) are the cell's medians; the other four runs' medians are 0.077 s and 0.056 (raw values, computed here from exp12-runs.csv), still above δ, so the miss doesn't rest on it.
- **`full`'s warm time isn't comparable with its cold time:** the warm block came later, when loaddb had grown, and its copies were 2,607–2,656 MB against 2,326–2,530 MB cold at 2 Gbit/s (recomputed from the run records).

**The moderate block** (9,000 per second, 0.333 of this trial's C; 3 repeats):

| Speed | Arm | Time, s | W_net, s (meas.) | D_net (meas.) | ΔW / ΔD | Meets O4 | p95, ms |
|---|---|---:|---|---|---|---|---:|
| 2 Gbit/s | `full` (the bar) | 12.65 | 0.002 (yes) | 0.002 (yes) | – | – | 0.87 |
| | `none` | 60.00 | 0.000 (no) | 0.000 (no) | −0.002 / −0.002 | yes | 0.75 |
| | `v1proxy` | 3.23 | 0.001 (yes) | 0.000 (no) | −0.001 / −0.002 | yes | 2.61 |
| | `v1proxy_s1` | 6.78 | 0.000 (no) | 0.000 (no) | −0.002 / −0.002 | yes | 0.91 |
| unthrottled | `full` (the bar) | 4.44 | 0.001 (yes) | 0.000 (no) | – | – | 1.04 |
| | `v1proxy` | 3.19 | 0.001 (no) | 0.000 (no) | −0.000 / +0.000 | yes | 2.46 |

At the moderate load the source's CPUs were 37% busy, about 5 spare, and **4 source workers meet O4**: the proxy's loss is no larger than `pg_basebackup`'s, both near zero and far inside δ, and the load's p95 rose only to 2.5–2.6 ms. **This is fragile by A9:** at 2 Gbit/s, where the Load row is judged, both `full`'s and `v1proxy`'s cells are flagged *client wait* in all 3 runs, so with flagged runs left out neither has a run and the row isn't judged. The moderate `pause2` (1 run) returned W 1.898 s and D 1.000.

### Source CPU

| Arm | Calibrated CPU-s, idle (2 runs) | Idle time, s | Under load, 0.5 Gbit/s | Under load, 2 Gbit/s | Under load, unthrottled |
|---|---:|---:|---:|---:|---:|
| `full` | 2.26 | 12.07 | 7.19 | 4.00 | 3.03 |
| `capture` | 4.42 | 3.89 | – | 1.97 | – |
| `pgdelta` | 10.43 | 9.95 | 9.12 | 8.54 | 8.73 |
| `v1proxy` | 5.84 | 2.90 | 6.12 | 5.51 | 6.10 |
| `v1proxy_s1` | 4.90 | 5.40 | 5.92 | 5.46 | – |
| v1 itself (`pgdelta_direct_inplace`) | 6.72 | 4.38 | – | – | – |
| `burn4` | 8.21 | 2.08 | 6.23 (any link) | | |

Under load is the window's VM-wide busy CPU, [t0, t1 + 10 s], over the settle's rate: approximate, as registered.
- **Staged pgdelta uses 4.61 times `full`'s calibrated CPU-s** (10.43 over 2.26; L2 held). Under load the ratio is 1.27 at 0.5 Gbit/s and 2.13 at 2: there `full`'s window is longer, and its WAL sender streams the load's WAL through it (the sampler, below).
- **v1 itself over `full` is 2.97** (6.72 over 2.26), against EXP-09's 5.3–6.0 over 1.6–1.9.
- **The registered projection isn't made:** R2 doesn't track. Had it tracked, it would have multiplied `full`'s W_net, 0.000 s, by 2.8–3.75, and projected no measurable loss for v1, where the proxy measured 0.05–0.07 s.
- **The sampler** (a mechanism; no verdict uses it), the cells' added CPU-s in the window over the settle's rate: `full`'s WAL sender 2.5–6.1 and the backends 0.6–2.8 more (it streams the load's WAL: 379–418 MB at 0.5 Gbit/s, recomputed from the run records); staged `pgdelta`'s `pg_basebackup` 2.8 and pgdelta 7.0–7.4, with pgbench 3.1–3.3 and the backends 2.5–2.8 less (the stall); `v1proxy`'s pgdelta 6.9–7.0, with pgbench about 1 less and the backends 0.6–1.0 less; `burn4`'s busy processes 6.3. No steal in any cell. `pg_stat_wal`'s per-second reads show the WAL syncs slowing in staged pgdelta's and the capture's stall, and in no other arm (see [A5](#where-staged-pgdeltas-loss-comes-from-a5)).

### Flags and robustness views

**The flags (A8, A9):**
- ***Client wait* (A8)** fired on 58 runs on W and 28 on D, and flagged 14 cells: in the busy block `pgdelta` at every speed, `capture`, `v1proxy` at every speed, `v1proxy_s1` at 2 Gbit/s and both controls; warm `v1proxy`; in the moderate block `full` at both speeds and `v1proxy` at 2 Gbit/s. It flagged every cell of more than one run where W was measurable: the threads' added wait over a window, 0.1–2.2 s, is large next to the small W's measured here. That total doesn't separate the arms: the busy `full` runs, which lost nothing measurable, added 0.27–0.74 s (see [v1's source side](#v1s-source-side-by-proxy-at-4-workers-and-at-1)).
- ***Fragile* (A9): yes.** Decisions 1 and 2 and R2 rest on cells flagged *client wait*: busy `pgdelta` and `v1proxy` at 0.5 and 2 Gbit/s, busy `v1proxy_s1` at 2 Gbit/s, and warm `v1proxy`. And one robustness view, flagged runs left out, empties those cells: it gives decision 1 "4. Proxy not judged", decision 2 "not judged" and R2 "not judged". The rows stand, as registered. The analysis's fragile line covers decisions 1 and 2 and R2. The rows beside the decisions rest on flagged cells too, so by A9 they are fragile as well: the HA caveat (busy, unthrottled, `v1proxy`), R1 (busy `pgdelta`), the Load row (moderate 2 Gbit/s `full` and `v1proxy`, every run flagged; with flagged runs left out both cells are empty and the row isn't judged) and the Warm source row (warm `v1proxy`, every run flagged). The analysis doesn't print these four.
- **For staged pgdelta the flag comes from elsewhere in the window, not from the stall.** Its threads' added wait over the window was 1.3–1.7 times its W, but in the worst second they waited less than in the settle (−0.10 to −0.14 times that second's lost time), as the capture alone did (recomputed from exp12-runs.csv). The stall's second isn't the client's.
- ***Near the line* (A9): no**, as the analysis prints. The margins (recomputed from exp12-cells.csv): at 0.5 Gbit/s `v1proxy`'s ΔW is 0.0511 against δ_W 0.05, inside ±0.25 δ, but its ΔD, 0.0507 against 0.03, is well outside; at 2 Gbit/s its ΔW, 0.0684, clears the band's edge by 0.006 s, and its ΔD, 0.0377, by 0.0002; unthrottled 0.0701 and 0.0419. A miss at a speed needs one number only, so each speed's miss rests on one outside the band. R2's bound ratios are far from 1.5.
- ***Latency caveat* (A9): none.** It applies to an arm that meets O4. `v1proxy_s1`'s worst 5-s p99 was 1.15 and 1.32 times `full`'s at 0.5 and 2 Gbit/s, under the factor of 2. `v1proxy` misses, so the caveat doesn't apply, but its worst 5-s p99 was 9–12 times `full`'s (92–161 ms against 10–17).
- ***Unsettled***: 1 run, the first warm one (above). No *checkpoint*, *steal*, *cache*, *client* or *autovacuum* flag; the clock flag doesn't fire.

**The robustness views** (each registered view, busy block; the analysis's tables, and the paired view from exp12-cells.csv):

| View | Staged `pgdelta`, ΔW at 0.5 / 2 Gbit/s | `v1proxy`, ΔW / ΔD at 0.5; at 2; unthrottled | `v1proxy_s1` | The rows |
|---|---|---|---|---|
| medians (the main view) | +1.265 / +1.346 | +0.051 / +0.051; +0.068 / +0.038; +0.070 / +0.042 | meets | decision 1: 2. R1; decision 2: Cap meets |
| maxima | +1.335 / +1.370 | +0.076 / +0.066; +0.105 / +0.071; +0.103 / +0.059 | meets | the same |
| paired with the same repeat's `full` | +1.267 / +1.347 | +0.052 / +0.051; +0.069 / +0.038; +0.070 / +0.042 | meets | the same |
| D in 0.5-s buckets | as main | D: +0.079; +0.060; +0.048 | meets | the same |
| D in 2-s buckets | as main | D: +0.005; +0.019; +0.021, under δ_D, so the miss rests on W | meets | the same |
| W as the late count at L | +4.435 / +5.911 | W: +0.007; +0.115; +0.249 | meets | the same |
| W as the late count at 2L | +4.022 / +5.554 | W: 0.000 at every speed, so the miss rests on D | meets | the same |
| a 15-s tail | +1.265 / +1.346 | +0.051 / +0.051; +0.068 / +0.038; +0.071 / +0.042 | meets | the same |
| flagged runs left out | no runs left | no runs left | no runs left at 2 Gbit/s | decision 1: 4. Proxy not judged; decision 2: not judged; R2: not judged |
| the null with the unsettled settle put back | as main | as main | meets | the same |

No view but the one that leaves flagged runs out moves a row. The proxy misses at both registered speeds in every other view, though on different numbers: in 2-s buckets only W misses, at 0.5 Gbit/s by 0.001 s, and as the late count at 2L only D does.

### Drift against EXP-11 (A10) and capacity (A12)

**A10 marks the session shifted,** on one of its three gates:

| Gate | This session | EXP-11's range, with its tolerance | Held |
|---|---:|---|---|
| `full`'s idle time at 2 Gbit/s | 12.07 s | 9.76–10.84 s (±5% of 10.27–10.32) | **no** |
| v1's calibrated CPU-s | 6.72 | 5.19–8.28 (±15% of 6.1–7.2) | yes |
| The single-stream link test at 2 Gbit/s | 1.917 Gbit/s | 1.879–1.955 (±2% of 1.917) | yes |

**The shifted gate is the copy's size, not a slower session.** The calibration's `full` sent 2,534 MB against EXP-11's 2,102 MB (hot, 2 Gbit/s), because loaddb, 401 MB at the trial's end, was in the copy. Per MB sent it took about the same time: 4.76 against 4.89 ms (recomputed from exp12-runs.csv and exp11-runs.csv). As registered, every comparison here is within the session, and the registered projection would have used EXP-09's numbers.

**A12 is flagged:** this trial's C, 27,019 per second, was 10.9% below the smoke run's 30,316. The smoke run's later one-run measurements had already read 27,148 and 27,508 (its item 2). The trial kept the rates set before the sweep, as registered, so the busy load ran at 0.670 of this trial's C. A3 shows it was sustainable: `none` completed 1.0000 of its schedule.

### loaddb's growth and the drift check

**loaddb grew to 11.6 times its size** (E13 and smoke item 14 failed: 1059%, against at most 5%), from 4,224 pages to 48,968 (35 MB to 401 MB). From the trial's start to its end, `load_progress` grew from 32 to 44,775 pages (0.26 to 367 MB), while `pgbench_accounts` went from 3,637 to 3,638 pages and the indexes didn't change. As in the smoke run, the cause wasn't found, and the registered action, fillfactor 70, wasn't applied (smoke item 14, rule 8).

**The drift check, fixed before the sweep's data was read, finds no drift in either load:**

| Load | Earlier settles: median p50 / p99, ms | Later settles: median p50 / p99, ms | Later over earlier | Drift (p50 above 1.1, or p99 above 2) |
|---|---|---|---|---|
| busy | first busy repeat, 14 settles: 0.667 / 1.327 | the warm block, 10 settles: 0.650 / 1.222 | ×0.975 / ×0.921 | no |
| moderate | first repeat, 6 settles: 0.581 / 0.861 | third repeat, 6 settles: 0.573 / 0.845 | ×0.985 / ×0.982 | no |

So, as registered, the session stands, and smoke item 14's deviation is reported as it is. The settles' medians fell slightly. The owner had decided before the fetch (October 24, in chat): "Don't rerun now. Apply the committed drift check after the fetch, and propose a rerun with a load_progress fix only if it shows drift." It shows none, so no rerun is proposed.

![EXP-12: does the load drift over the session?](../figures/exp12-drift.svg)

How to read it: the busy load on the left (18,100 per second, 71 minutes) and the moderate load on the right (9,000 per second, 17 minutes), against minutes from each load's start. The top row is completions per minute against the schedule (dashed). The middle and bottom rows are p50 and p99 per minute: black for load-only minutes, with every copy's and control's window (t0 to t1 + 15 s, the orange bands) taken out, and grey in the bottom row for all minutes. The dots are the settles the check judged: blue the earlier ones, orange the later. Grey marks the warm-up; the dotted pink lines are the warm block's reads of the database. Completions hold flat; the judged settles' p99 ends lower than it starts; the per-minute p99 is about 10 ms in the first five minutes (the warm-up, the controls and repeat 1's first two runs) and rises again in the warm block's minutes.

What the per-minute view shows (reported, not judged):
- **Completions per second held at 18,006–18,161** over the busy load's whole minutes (71, 68 after its warm-up), and at 8,976–9,023 over the moderate load's (17, 14 after its warm-up).
- **The busy load's load-only p99 was about 10 ms in its first five minutes** (9.7–12.5 ms): the warm-up, the two controls and repeat 1's first two runs, unthrottled `pgdelta` and `v1proxy`, whose settles had p99 10.7 and 9.5 ms; the third run's settle (unthrottled `full`) had 3.0 ms. It was already 10 ms before the first control. Those two settles are in the null's pool and in the drift check's earlier group, and `burn4`'s first run fell in this stretch too (its settle's p99 10.2 ms; it lost W 0.321 s, against 0.149 for the later run, and E4's W clause fails on their median, 0.235 s, though E4's p99 clause fails in both runs anyway, 41 and 156 against at most 30). Unthrottled `v1proxy`'s first run lost W 0.100 s, the second highest in its cell; without it the cell's median is 0.067 s, still a miss. No verdict changes. Over the busy block's other 58 minutes the load-only p99 was 1.36 ms (median), the largest 3.1 ms; over all 61, 1.37 ms, the largest 12.5 ms, one of the first five (recomputed from the pgbench logs). The smoke run showed the same early 10 ms.
- **In the warm block's 7 minutes the load-only p99 rose** (median 8.7 ms, largest 939 ms). The run records show why. Before each warm run's settle, the harness read the benchmark database and the snapshot once on the source (`run_exp04.py`'s `source_cache_step` with `warm=True`; the record's `source_untimed.cache`, policy `warm`). Each read took 0.88–2.40 s, the first, from evicted files, 2.40 s; the whole untimed step took 1.25–2.77 s and ended as the settle started. Those reads are outside every window, so the per-minute view counts them as load-only time. pgbench's progress lines put a one-second rise in mean latency, to 4.1–7.9 ms, at each of the nine later reads, and the first read's stall at up to 904 ms (recomputed from the run records and the progress lines). The first read's backlog reached into its settle, which its gate flagged *unsettled* (above). In eight of the other nine, the read's one-second rise overlapped the first 0.06–0.63 s of the settle (the progress second that held it ended that long after the settle's start); in the ninth, `r7`'s `full`, it ended 0.11 s before. No second wholly inside those nine settles passed 1.1 ms mean latency, and their p99 was 1.18–1.27 ms, so little of the rise reached them. The moderate load's p99 stayed at 0.85–1.08 ms.
- **The copies grew with loaddb, and the loss didn't** (recomputed from the run records). `full`'s copy at 2 Gbit/s grew from 2,326 to 2,530 MB across the busy block's five repeats, and its time from 11.30 to 12.26 s; at 0.5 Gbit/s from 2,594 to 2,893 MB and 44.3 to 49.5 s. S grew from 2.24 to 2.51 GB, and staged pgdelta's bytes on the wire from 143 to 414 MB. The proxy's snapshot was fixed, so it sent 29.5 MB throughout. The capture's and staged pgdelta's W show no trend over the repeats (at 2 Gbit/s, by repeat: 1.20, 1.35, 1.24, 1.13, 1.18 s and 1.37, 1.37, 1.22, 1.35, 1.24 s), and `full`'s W stayed unmeasurable.

### Order effects

By the registered test, runs that follow a staged arm against the others: **no difference** over 0.05 s in W or 0.03 in D. The order of the blocks was fixed (busy, warm, moderate, calibration), so load level, loaddb's size and time in the session are confounded across blocks: the warm block ran with loaddb larger than in most of the busy block, the moderate block larger still, and the calibration block last. Within a block, each repeat visited the speeds in a shuffled order and ran the arms at each speed in a shuffled order. In the warm block the seeded draw put `v1proxy` before `full` in all 5 repeats (a 1-in-32 draw), so there arm and position coincide: `v1proxy` always ran first after the previous repeat, and the unsettled first warm run, after the read from evicted files, is `v1proxy`'s for that reason.

### What the results depend on

The limitations listed before running all apply. The ones a result here depends on:
- **No live v1.** The proxy reads a stopped snapshot on the live source's disks, in backup mode on the live server. It doesn't stream the WAL the load writes during its copy (about 8.5 MB per second at R_busy: `full` streamed 379–418 MB over its 44–49-s copies at 0.5 Gbit/s), doesn't read files while they are written, and didn't send loaddb's changes, which grew to about 420 MB here (v1 itself sent 449 MB in the calibration block, the proxy 29.5), or read and hash their pages (v1 itself hashed 18% more pages than the proxy, 310,891 against 263,415). So a live v1 here would most likely take longer than the proxy, and use more CPU. Every v1 result here is by proxy; v1's own arm comes with T8.
- **The client shares the source's CPU.** Every deciding cell is flagged *client wait*, and so are the cells behind the HA caveat, R1, the Load row and the Warm source row. In each busy-block `v1proxy` run pgbench's threads waited in the run queue 8.5–42 times the proxy's W over its window, but that total doesn't measure the client's share: `full`'s busy windows added 0.27–0.74 s of wait with no measurable loss. The worst second's wait does separate them: 0.30–0.38 s for `v1proxy`, at most 0.08 for `full` and 0.10 for `v1proxy_s1`. Leaving flagged runs out gives decision 1 "4. Proxy not judged" and decision 2 "not judged": the analysis's *fragile*. The rows stand, as registered, but how much of the proxy's loss an application on another host would see isn't known.
- **The bar is `pg_basebackup`, not rule 3's fastest copy.** EXP-11's bound (`parcopy`, 9.32 s at 2 Gbit/s against `full`'s 10.30, idle) is rule 3's fastest copy at 2 Gbit/s, but it reads a stopped source and has no slowdown number until T8. Every time comparison here is against `pg_basebackup`.
- **One synthetic load,** three-row update transactions on a database that started at 35 MB, synchronous commits, no read I/O, at two levels set on this hardware. The proxy's verdict differs between them: it misses at 0.67 of this trial's C and meets at 0.33 (fragile: the moderate cells are flagged *client wait*).
- **2.1 GB in RAM, one VM pair, local NVMe in RAID 0, ext4 in ordered mode, 0.1 ms round-trip time.** The data, the WAL and staged pgdelta's S share one array. A separate WAL disk or a persistent disk would change staged pgdelta's stall and the proxy's I/O (T9); larger machines leave more spare CPU for 4 workers.
- **The session shifted against EXP-11** on `full`'s idle time (A10), a larger copy, and **this trial's C was 10.9% below the smoke run's** (A12). Every comparison is within the session.
- **loaddb grew to 11.6 times its size.** The registered drift check found no drift in the load's latency, but the full copies and S grew by up to 0.3 GB over the busy block, so `full`'s and staged pgdelta's times rose across repeats, and the warm and moderate blocks copied more than the busy block's early repeats.
- **The order busy, warm, moderate, calibration is fixed,** so load level and time in the session are confounded: warm against cold, and moderate against busy, are also later against earlier, with loaddb larger. In the warm block `v1proxy` also always ran before `full`.
- **O4's definitions, tolerances and L are judgments** (change 3). With `full`'s loss unmeasurable, the bar is "no measurable loss, within δ", and δ sat at its fixed floors (0.05 s, 0.03).
- **Short refreshes in 1-s buckets.** The proxy's 3.6-s copy is three or four buckets; in 2-s buckets its D_net falls under δ_D, and its miss then rests on W alone.
- **Fast checkpoints, clear text and no CPU limit on the refresh,** for every arm.

## Verdict on each prediction

Each verdict is the registered analysis's, in the order of judgment. Where its reading looks wrong, the note says so beside it; the verdict isn't changed.

### The ledger's prediction and rules (registered)

| ID | Prediction | The analysis's verdict | Numbers and notes |
|---|---|---|---|
| L1 | "the slowdown tracks source CPU" | **Failed** (R2: doesn't track) | – |
| L2 | "pgdelta uses about 4 times the full copy's source CPU" | **Held** | 4.61 (10.43 over 2.26 calibrated CPU-s), inside [3, 5]. Under load, beside: 1.27 at 0.5 Gbit/s, 2.13 at 2 |
| L3 | "so its total throughput loss is 2-4 times the full copy's at 2 Gbit/s" | **Failed, high** | `full`'s W isn't measurable; staged pgdelta's 1.346 s over `full`'s bound puts the ratio at 1,625 or more |
| L4 | "and still higher at 0.5 Gbit/s" | **Not judged** | `full`'s W is measurable at neither speed. The lower bounds: 161 at 0.5 Gbit/s, 1,625 at 2 |
| L5 | "Every output is correct." | **Held** | 117 runs, none wrong; 45 live outputs passed every G4 check |

| Rule | The analysis's verdict | Numbers and notes |
|---|---|---|
| R1 | **Fires:** "staged pgdelta: source CPU is a blocker" | Staged pgdelta wins on time at 0.5 and 2 Gbit/s under load (14.99 against 47.19 s; 11.18 against 11.92) and loses more there: ΔW +1.265 and +1.346 s against δ_W 0.05. Its T5 consequence applies (change 4). Fragile: its cells are flagged *client wait*, though the stall's second isn't the client's (Flags). **The wording is R1's, as registered; R2 shows the loss doesn't follow CPU-seconds, A5 that it is mostly the capture's, and the timing that it falls during the capture's flush of S, when WAL syncs slowed (A5's section)** |
| R2 | **Doesn't track** | Judged at both speeds with `full`'s bound: the rate ratio is at least 35 at 0.5 Gbit/s and 353 at 2, far outside [1/1.5, 1.5]. As registered: "the T5/T8 order waits for v1's own arm", unless R1 has moved T5, which it has |
| R3 | **Doesn't fire** | No wrong output of `pgdelta`, `v1proxy`, `v1proxy_s1` or v1 itself; none unverified |

### Decision 1: the T5/T8 order (busy block, 0.5 and 2 Gbit/s; rows in order)

| Row | Applies | Notes |
|---|---|---|
| 1. Stop | no | R3 doesn't fire |
| 2. R1 | **yes** | Slowdown is reported next to time, and T5 moves ahead of T8 (registered; change 4) |
| 3. R2 | no | – |
| 4. Proxy not judged | no | A2, A3, A4 and the clock gate don't fire; no `v1proxy` or `full` cell is short |
| 5. No measurable slowdown | no | `v1proxy`'s W and D are measurable at both speeds |
| 6. v1 meets | no | – |
| 7. v1 misses | (beside row 2) | `v1proxy` misses at 0.5 and 2 Gbit/s, on W and D; warm reading **7a, hashing**: warm `v1proxy` also misses against warm `full` |

**Decision 1: 2. R1. T5 moves ahead of T8.** The proxy's reading beside it, 7a, moves T5 ahead too, so the two agree. Fragile: with flagged runs left out, row 4 (T5 stays behind T8).

### Decision 2: a cap on v1's source workers (rows in order)

| Row | Applies | Notes |
|---|---|---|
| No cap | no | `v1proxy` misses at both speeds |
| Cap fails | no | `v1proxy_s1` meets at both |
| Cap meets | **yes** | `v1proxy` misses at 0.5 and 2 Gbit/s; `v1proxy_s1` meets at both |
| Cap not judged | no | `v1proxy_s1` passed A4, with 5 runs per cell |

**Decision 2: Cap meets.** "v1 at 4 source workers slows a busy primary more than `pg_basebackup`; one source core doesn't. v1 needs a source worker cap on busy primaries. T10b moves ahead of T8, as 'source workers by load', and D50 and D81 get a basis." Fragile: not judged with flagged runs left out.

### Beside the decisions

| Row | The analysis's verdict | Notes |
|---|---|---|
| O4 for staged pgdelta (D04, D10p) | **Misses O4 where it wins on time**; A5: "Most of staged pgdelta's slowdown is its capture, which v1 removes" | 1.197 of 1.265, 1.346 and 1.254 s |
| HA caveat | **Fires** | Unthrottled, `v1proxy` W_net 0.070 s and D_net 0.042, against `full`'s 0.000 and 0.000 (ΔW +0.070 against δ 0.05; ΔD +0.042 against 0.03). Its cell is flagged *client wait*, so by A9 it is fragile, which the analysis doesn't print |
| Load | **The moderate block differs** | Moderate, at 2 Gbit/s: "6. v1 meets", decision 2 "No cap"; busy: "7. v1 misses", "Cap meets". Both are stated; the busy block decides. Fragile by A9, which the analysis doesn't print: the moderate 2 Gbit/s `full` and `v1proxy` cells are flagged *client wait* in every run, so with flagged runs left out the moderate side isn't judged |
| Warm source | **The same verdict** | Warm and cold `v1proxy` both miss at 2 Gbit/s. Fragile by A9: the warm `v1proxy` cell is flagged *client wait* in every run |

### Added predictions

| ID | Prediction (short) | The analysis's verdict | Numbers and notes |
|---|---|---|---|
| E1 | C 10,000–40,000; busy CPU 40–75% at R_busy, 20–45% at R_mod | **Held** | C 27,019; 0.628 (the smoke run's, as registered) and 0.369. Above 50%, which selects the upper ranges of E4, E7, E9, E14 and E16 |
| E2 | The null | **Failed, in one clause** | `none` completed 1.0000 of its schedule in all 8 windows; W's null median 0.002–0.008 s and 95th percentile 0.012–0.021 s at T = 15–50 (at most 0.02 and 0.05); 0.0028 s for a median of 5; δ_W the floor everywhere; the gate's progress-line part within 30 s in 100% of runs, as the analysis reads the clause, and with its log part 102 of 103 (99%), so the clause holds either way. D's null median at T = 15 was 0.0006, below 0.005–0.03 |
| E3 | `pause2`: W 1.6–2.0 s at 0.6; D at least 0.95; late count 4–6 s; the gap matches | **Held** | W 1.908 and 1.919 s (1.898 at 0.33); D 1.000; late count 5.45 and 5.02 s (2.79 at 0.33) |
| E4 | `burn4` above 50%: W_net at most 0.2 s, D_net at most 0.15, window p99 2–30 times the settle's | **Failed** | W_net 0.233 s; D_net 0.125 (inside); window p99 41 and 156 times the settle's |
| E5 | Calibrated CPU-s per arm | **Failed, for one arm** | `v1proxy_s1` 4.90, below 5.0–7.0. Inside: `full` 2.26, `capture` 4.42, `pgdelta` 10.43, `v1proxy` 5.84, v1 itself 6.72 (within 20% of `v1proxy`), `burn4` 8.21; v1 over `full` 2.97, `pgdelta` over `full` 4.61 |
| E6 | Times under load, and against idle | **Failed** | Outside: `full` 47.19 s at 0.5 Gbit/s (38–42) and 11.92 at 2 (10.3–11.3), larger copies: at 0.5 Gbit/s 379–418 MB of WAL streamed at about 8.5 MB per second, above the "few MB per second" of E6's basis, plus loaddb's growth (at 2 Gbit/s, 107–114 MB of WAL); staged `pgdelta` 14.99 at 0.5 (8.3–11.5); `v1proxy_s1` 7.55 and 7.25 (4.5–6.5); the capture 37.5% over idle (0–35%). Inside: `full` 4.29 unthrottled, the capture 5.35, `pgdelta` 11.18 and 10.26, `v1proxy` 3.61–3.69, `bracket` 0.21; `full` −1.3% against idle, the others +12% to +34% |
| E7 | Net medians, above 50% | **Failed** | `full` unthrottled was not measurable (0.02–0.3 s and 0.02–0.15 predicted), and `v1proxy`'s window p99 was 73–124 times the null's (3–30). Inside: `full` at 0.5 and 2, `bracket`, the capture (1.197 s, 0.955), `pgdelta` (1.254–1.346 s, 0.958–0.968), `v1proxy` (0.051–0.070 s, 0.038–0.051), `v1proxy_s1` |
| E8 | The registered verdicts | **Held** | L1 doesn't track, L2 holds, L3 fails high, L4 not judged, L5 holds, R1 fires, R2 doesn't track |
| E9 | Decision 1 row 2; above 50%: "v1 misses" on D, 7a, "Cap meets"; the moderate block meets; the warm block the same | **Failed** | Every part held but one: the proxy missed on W as well as D. The analysis reads "on D" as "on D alone"; read as "at least on D", E9 would hold. The strict reading doesn't favour the prediction, and it is kept |
| E10 | Every output passes; `full`'s cut within 0.3 s before its end point; staged pgdelta's staleness 4–8 s | **Held, held, failed** | Cut 0.001–0.072 s before the end point. Staleness 6.42–13.51 s in the busy block, 5 of 15 above 8 s, all five at 0.5 Gbit/s, rising with S over the repeats (8.73 to 13.51 s); none at 2 Gbit/s or unthrottled above 7.98 s. The analysis's list also holds the two calibration outputs (234 and 332 s), whose staleness means nothing without load; the clause fails on the busy outputs alone |
| E11 | At least half of pgdelta's W from its capture | **Held** | 1.197 of 1.254–1.346 s |
| E12 | Wire bytes | **Failed** | `full` 2,744 MB at 0.5 Gbit/s (2.15–2.45 GB): loaddb's growth and the 379–418 MB of WAL it streamed; `v1proxy` 29.5 MB (27.6–29.2); staged `pgdelta` 262–307 MB (60–130), rising from 143 to 414 MB with `load_progress` over the busy block |
| E13 | loaddb grows at most 5%; no failures; no unasked checkpoint; no autovacuum on loaddb | **Failed, in one clause** | Growth 1059%. No pgbench failure, no checkpoint in a window but an arm's own, no autovacuum on loaddb |
| E14 | A6 above 50%: "cores", or "CPU-seconds" where `v1proxy`'s W_net is near E7's low end, by `v1proxy_s1`'s bound | **Failed** | A6 read "CPU-seconds is an adequate unit", but by `burn4`'s rate, not by the path E14 named: `v1proxy_s1`'s rate is far below half of `v1proxy`'s, and `v1proxy`'s W_net (0.051 and 0.069 s) is above the analysis's 0.02–0.05 reading of "near the low end" |
| E15 | Warm `v1proxy`: W_net at most cold + 0.02 s, D_net within ±0.03; 7a | **Held** | Warm 0.087 s and 0.067 against cold 0.069 s and 0.038, inside by 0.0014 s and 0.0004 (recomputed from exp12-cells.csv); the cause 7a. The warm values are within E15's band of the cold ones, so they show no change, not a rise |
| E16 | Above 50%, "client wait" flags at most `burn4`'s and `v1proxy`'s cells | **Failed** | It flagged 14 cells, `pgdelta`, the capture, `v1proxy_s1` at 2 Gbit/s, `pause2` and moderate `full` among them |

### Added rules

| ID | The analysis's verdict | Numbers and notes |
|---|---|---|
| A1 | **Doesn't fire** | No wrong `full` output; the coverage control failed (v) in 41 of 41; the negative controls ran in the smoke run |
| A2 | **Doesn't fire**, in either block | `pause2` above |
| A3 | **Doesn't fire**, in either block | `none` 1.0000; largest backlog 6 and 2 |
| A4 | **Passes:** `v1proxy`, `v1proxy_s1` and the warm runs | References: v1 itself 6.72 and `v1proxy` / 1.06 = 5.51 CPU-s; v1 itself sent 449 MB to the proxy's 29.5 and hashed 18% more pages (310,891 against 263,415), so part of the gap may be its larger send and its larger read |
| A5 | **"Most of staged pgdelta's slowdown is its capture, which v1 removes"** | 1.197 of 1.265, 1.346 and 1.254 s |
| A6 | **"CPU-seconds is an adequate unit at this load"** | The rule's "otherwise": `v1proxy_s1`'s loss per CPU-s (0.0001–0.0002 s) is at most half of `v1proxy`'s (0.0088–0.0118), but `burn4`'s (0.0284) isn't. Beside it, a reading that isn't registered: per calibrated CPU-s, one worker lost about 1/60 of what four did (1/58 at 2 Gbit/s, 1/64 at 0.5, where one worker's loss isn't measurable and counts at its bound; computed here from exp12-cells.csv), and `burn4`, four processes busy at once, lost 2.4–3.2 times as much per CPU-s as `v1proxy`. Neither comparison shows loss in proportion to CPU-seconds, and both are consistent with loss following how many cores run at once. But that concurrency wasn't measured per run (`burn4` spins 4 processes at nice 0; `v1proxy` averaged about 1.9 cores over its 3.6-s copy), and both cells are flagged *client wait*, so the cores reading is a hypothesis for T10b, not a finding. The rule's text sends a `burn4` that costs more per CPU-s than the proxy to "CPU-seconds", a case it didn't anticipate. As registered, the verdict stands |
| A7 | **Always** | Time is beside every slowdown number above |
| A8 | **Flags:** *client wait* on 58 runs (W) and 28 (D), *unsettled* on 1; the clock gate doesn't fire | 14 cells flagged |
| A9 | **Fragile: yes; near the line: no; latency caveat: none** | Fragile by the flagged cells and by leaving flagged runs out |
| A10 | **The session is shifted** | On `full`'s idle time (12.07 s), a 20% larger copy; v1's CPU-s and the link test held |
| A11 | **Nothing was rerun** | Every run is read as recorded |
| A12 | **Flagged** | C 10.9% below the smoke run's |
| A13 | **Doesn't fire** | No run failed (a) or (b) |

## What the registered rules say

1. **G4 holds and R3 doesn't fire:** every output taken from the live source under load is correct, and speed work goes on.
2. **Decision 1 takes row 2, R1: T5 moves ahead of T8,** and slowdown is reported next to time (rule 3). Beside it the proxy reads "7. v1 misses", with the warm reading 7a, hashing, which moves T5 ahead too; the two agree, so no override is needed. R2 doesn't track, so the registered projection isn't made, and the note's "waits for v1's own arm" gives way to R1 (change 4). The row is fragile: with flagged runs left out it would be "4. Proxy not judged".
3. **Decision 2 is "Cap meets": T10b moves ahead of T8, as "source workers by load".** v1 at 4 source workers slows a busy primary more than `pg_basebackup`; one source core doesn't. So v1 needs a source worker cap on busy primaries, and D50 and D81 get a basis. The design note applies: pgdelta's source pool should size itself from its CPU quota. The row is fragile: not judged with flagged runs left out.
4. **The HA caveat fires.** EXP-11's qualifier becomes: "v1 is faster than the released copiers tested here (`pg_basebackup` with fsync or syncfs, `pg_probackup catchup -j 4` and `-j 8`) at 2.1 GB on these VMs, and it slows a busy primary more than `pg_basebackup` does": unthrottled, by proxy at 4 source workers and 18,100 transactions per second, 0.070 s of work lost and a worst second 4.2% short, against `pg_basebackup`'s nothing measurable (δ 0.05 s and 0.03). It is fragile by A9.
5. **The moderate block's verdict differs, and the busy block decides.** At 30% of capacity (0.33 of this trial's C), v1 at 4 source workers meets the bar; at 60% (0.67) it doesn't. A cap then depends on the load, which is T10b's question. The moderate meet is fragile by A9: its 2 Gbit/s `full` and `v1proxy` cells are flagged *client wait* in every run, and with flagged runs left out it isn't judged. And the moderate block always ran after the busy one, with loaddb larger.
6. **The warm block gives the same verdict** and names the cause of the proxy's miss as registered: hashing (7a), since warm `v1proxy` also misses against warm `full`. Its net values stayed within δ of its cold ones (E15 held), so the miss doesn't need the cold read. Fragile by A9: the warm cell is flagged *client wait* in every run.
7. **O4 for staged pgdelta, on D04 and D10p: it misses where it wins on time** (0.5 and 2 Gbit/s), recorded in R1's words as "staged pgdelta: source CPU is a blocker", and by A5, "Most of staged pgdelta's slowdown is its capture, which v1 removes". R1's wording names CPU; R2 shows the loss doesn't follow CPU-seconds, A5 that it is mostly the capture's, and the timing that it falls during the capture's flush of S, when the WAL's syncs slowed.
8. **A6: "CPU-seconds is an adequate unit at this load".** Read as registered, D81's budget can be stated in CPU-seconds. Beside it, a hypothesis for T10b: the numbers A6 compares are also consistent with loss following how many cores run at once (above). Decision 2's cap is in workers; T10b, now ahead of T8, measures the worker count directly.
9. **A10 marks the session shifted and A12 flags C;** every comparison here is within the session. A11: nothing was rerun.

## Conclusions

1. **Every copy taken from a busy source was correct.** 41 outputs of `pg_basebackup` and staged pgdelta, taken while 18,100 or 9,000 transactions per second committed, and 4 more from the idle source, passed `pg_verifybackup`, recovered and started, and held the load's balance invariant, a consistent cut, coverage, the row counts, the schema and `pg_amcheck`, with 1% of the load's transactions rolled back and none of their effects present. So G4 holds for these two tools, on this load. It says nothing yet about v1's own live read (T8).
2. **One `pg_basebackup` stream costs a busy primary nothing measurable on O4's two numbers (W and D)** at 0.5 or 2 Gbit/s or unthrottled. So O4's bar, on this load and hardware, is "no measurable loss, within δ". Unthrottled it did raise the load's p95 latency over its 4.3-s copy to 6.4 ms, against the null pool's 0.96.
3. **Staged pgdelta fails O4 by a wide margin, and most of it is its capture.** It loses 1.25–1.35 s of the load's work and stalls it almost completely for one second, at every speed; the capture alone loses 1.20 s. The stall falls in the capture's flush of S on the array that holds the WAL, and there the WAL's syncs slowed, to up to about a second, in every staged run and in no other arm (`pg_stat_wal`), so the load's commits waited on them. Why the syncs waited during S's flush isn't shown. v1 removes the capture. This adds to D10p's grade X.
4. **By proxy, v1's source side at 4 workers slows a busy primary more than `pg_basebackup`, and at 1 worker it doesn't.** At 0.67 of this trial's capacity, 4 workers cost 0.05–0.07 s of work and a worst second 4–5% short, at every speed, and raised the load's p95 latency to 69–132 ms during a 3.6-s copy; one worker's loss was far inside δ, in twice the time. By proxy that time, 7.25–7.55 s, is under `full`'s 11.92 and 47.19 s at 2 and 0.5 Gbit/s, but the proxy's send left out the ~420 MB of loaddb's changes v1 itself sent here (v1 itself took 4.38 s idle against the proxy's 2.90), so a live v1 at one worker would take longer, most at 0.5 Gbit/s (~6.7 s for 420 MB on the link); and `full` is O4's bar, not rule 3's fastest copy, EXP-11's bound, which has no slowdown number until T8. At 0.33 of capacity, 4 workers met the bar (fragile: those cells are flagged too). With a warm cache the proxy still missed against warm `full`, so by row 7a its cause is the hashing; its net values stayed within δ of the cold ones (E15). These rest on two caveats stated with them: no live v1 (the proxy reads a stopped snapshot, streams no WAL, and sent 29.5 MB where v1 itself, reading the source after the load, sent 449 MB and hashed 18% more pages), and the client sharing the source's CPU, so that every deciding cell is flagged and the rows are fragile.
5. **So, by the registered rules, T5 and T10b both move ahead of T8,** EXP-11's HA qualifier gains "and it slows a busy primary more than `pg_basebackup` does", and v1 needs a source worker cap on busy primaries, sized by load (T10b). All three rest on cells flagged *client wait*, so all three are fragile.
6. **What this doesn't settle:** v1's own slowdown on a live source (T8); how much of the proxy's small loss an application on another host would see; other loads, a larger working set and TPROC-C (T10b); data beyond RAM and persistent disks (T9); the slowdown of rule 3's fastest copy (T8); and whether loss follows CPU-seconds or cores at once: A6's registered verdict is "CPU-seconds", and the cores reading is a hypothesis for T10b.
7. **The session held steady in the load's latency, and moved in two ways.** This trial's capacity was 10.9% below the smoke run's (A12), so the busy load ran at 0.67 of it rather than 0.6. And loaddb grew to 11.6 times its size, as it grew in the smoke run, so the full copies and S grew by up to 0.3 GB over the busy block and A10's shift is a larger copy, not a slower session. The registered drift check found no drift in the load's latency, so, as the owner decided, no rerun is proposed.
