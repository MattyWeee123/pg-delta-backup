# EXP-14: Does EXP-11's HA verdict hold on Omni's disk, across zones, beyond the page cache? (T9-PD)

Status: **pre-registered October 24, 2026 (night); reviewed and revised the same night (see [After the review](#after-the-review-october-24-2026)); the harness isn't built; not run.** This is the persistent-disk arm of test T9 in the [decision ledger](../DECISIONS.md#t9-scale-beyond-ram), moved up and widened as the audit's T9-PD ([FIRMNESS.md](../FIRMNESS.md#41-the-tests), section 4.1), under the owner's decisions of October 24 (night): K1 and K12 (size each experiment to what firm results need, two sessions per test on fresh VMs and different days, no budget cut lines), K6 (every HA verdict judged separately against the fastest copy measured and against the released tools Omni can run) and K10 (a single session whose deciding margin is inside the shift between sessions isn't firm). As for EXP-11 to EXP-13, an independent review and a check of the revision come before the harness is built, and every change they lead to is listed here, dated. The ledger's registered text for this arm is quoted word for word and judged as registered. Everything marked "added here" is new. Where the design departs from the ledger, the reason is under [Changes from the ledger](#changes-from-the-ledger-rule-8), as rule 8 asks.

## Question

[EXP-11](EXP-11-full-copy-baseline.md) found v1's path **Out on fast links**: unthrottled, a range-parallel copier built as a bound (`parcopy8`) made a durable copy in 1.88 s, and v1's path at 4 jobs took 1.64–1.65 times as long for local changes. Beside it stood the qualifier: v1 was faster than the released copiers tested, at 0.77–0.78 of one `pg_basebackup` stream with syncfs and about half of `pg_probackup catchup`. Those numbers hold on one VM pair in one zone (0.1 ms round-trip time), on local NVMe that writes about 1.6 GB/s, for 2.1 GB that fits in RAM, with v1 and the bound reading a stopped, idle source.

Omni's HA rebuild runs on other conditions ([Omni notes](../research/notes/alloydb-omni.md)):
- **GKE's default disk is a balanced persistent disk** (`pd-balanced`, ext4). Its throughput is set by the total size of the VM's disks of that kind, and capped per VM. On an `n2-standard-8` it can't exceed 800 MiB/s each way ([Google's disk performance page](https://docs.cloud.google.com/compute/docs/disks/performance)), half of what EXP-11's local NVMe wrote. A smaller Omni deployment has a smaller disk, and less.
- **The standby sits in another zone** of the region, since in-region standbys replicate synchronously across zones.
- **Real databases don't fit in the page cache.** At 2.1 GB, v1's verification re-read came from memory, and every full copy's flush landed at its end, below Debian's write-back thresholds.

Each of these can move the verdict either way. A capped disk slows every method that moves the whole database through it, which pulls the ratio toward 1.0. A copy beyond the page cache may turn v1's re-read into a second disk pass, which pushes it toward 2.0. And one sequential reader on a network disk may be slow, which hurts one `pg_basebackup` stream most.

On VM pairs in two zones, in each of two sessions:
1. **Against the fastest copy measured** (rule 3's yardstick, the bound): does "Out on fast links" hold, and can v1's design reach EXP-09's line on this disk at all?
2. **Against the released tools Omni can run** (one `pg_basebackup` stream, `pg_probackup catchup`): does "faster than the released copiers" hold?
3. **Does it depend on the disk's size** (150 GiB beside 2,000 GiB), on the database's size (2.1 GB against 48.5 GB), or on v1's worker count (4 against 8 jobs)?
4. **What do the zones give:** their round-trip time, and what one TCP stream and eight carry between them?
5. **Where does v1's verification re-read come from at 48.5 GB,** and what sets it: how far the receiver's first pass over B runs ahead of verification?

It decides D03's HA positioning on Omni's conditions, against each yardstick (K6), and the ledger's registered rule for T9's persistent-disk arm. It moves D72 (the platform), the HA qualifier, W2 (the destination's two passes as v1's bottleneck) and D41p (the re-read beyond RAM). It gives readings for D50 (8 jobs against 4), D33 (B's page digests, held per file until the file is done) and, at 2.1 GB, D86 (the new-copy mode's flush) and D85's price (a new copy against in place).

**What would change the plan** (the full rules are under [The HA verdicts](#the-ha-verdicts-added-here) and [Rule 11](#rule-11-which-outcomes-may-pick-a-route)). Every row needs both sessions to agree, at 48.5 GB on the 2,000 GiB disk, with v1 at 4 jobs, no flag, and the registered zones and CPU platform (A13):

| If | Then |
|---|---|
| A12 holds: 0.9 times the fastest copy is under v1's floor, the time to read S and B once **(expected)** | Within reach of the fastest copy is closed for v1's design on Omni's default disk at this size: no change to v1's CPU or verification (T5, T13) can reach EXP-09's line here. Firm whatever the near-line flag. It reads "Out" if `R_fast` ≥ 1.03 in both sessions, and "a tie at the disk cap" otherwise; a tie gets no third session |
| ...and not faster than the released copiers (`R_rel` ≥ 1.00 for hot or append) | HA is closed as a speed target for v1's design at this size and on this disk. DR stays v1's first target, as T12r decides. D03 is restated against both yardsticks |
| ...and faster than the released copiers (`R_rel` ≤ 0.90 for both) | EXP-11's qualifier holds on the conditions measured, named in full. For Omni it stays provisional until OMNI-2 reads its memory limits and `vm.dirty_*` |
| ...and undecided against the released copiers | HA against the released tools stays open, as a branch. T9 at 100–200 GB and T8 decide it |
| A12 fails, and `R_fast` < 1.00 for both (Undecided against the fastest) | EXP-11's Out doesn't carry to Omni's conditions. HA re-enters beside DR, and T5 and T13 are ranked for it again |
| The two sessions disagree, or a row carries a flag | Provisional: both readings are kept, and a third session is proposed to the owner. Not for a tie at the disk cap |

No row against the released copiers is expected in advance: it turns on how much of v1's re-read reaches the disk, and on what limits `pg_probackup`, and neither is known yet (E11).

## Setup

### Machines, disks and zones

- **Two VM sets,** made with the multi-pair scripts (`build/infra-pairs`, commit 428bd6c: per-role zones, a `pd-balanced` data disk at `/data`, a read-only quota check before anything is made, host keys per set, and teardown in every zone with a check for leftovers). Each set is two `n2-standard-8` VMs (8 vCPUs, 32 GB of memory each) with no local SSD, labelled `purpose=pg-delta-benchmark` and `pgd-set=NAME`, and deleted after its results are fetched. Each set's settings are in a committed set file (see Run lines), so `create.sh`, `remote.py` and `teardown.sh` read the same ones.
  - **`t9pd-2000`:** one 2,000 GiB `pd-balanced` data disk per VM: Google's sizing for 1 TiB of data on Omni, and GKE's default storage class. It runs S and the deciding size.
  - **`t9pd-150` (changed after the review):** one 150 GiB `pd-balanced` data disk per VM, at L only. 150 GiB is the smallest size that holds L's pristine copy beside the running cluster on the source (about 100 GB with WAL), and B0 beside one output on dest (about 97 GB). Omni's 2× sizing for this database would be about 100 GiB. A GKE node's boot disk is `pd-balanced` by default too ([GKE's boot disk page](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/custom-boot-disks)), so it adds to the node's limit; its size is OMNI-2's to read.
- **Why two sets.** Google computes a VM's `pd-balanced` limit from the total size of all its `pd-balanced` disks, the boot disk included, with one baseline per VM (performance page, read October 24). A small disk beside the 2,000 GiB one would get the large disk's limit. So each size has its own VMs.
- Debian 12, PostgreSQL 17 from PGDG, `pg_probackup` 2.5.16 on both, strace, as EXP-11. Every version goes into `run.json`.
- **Zones (added here):** the source in `us-central1-a`, EXP-09's and EXP-11's zone; dest in `us-central1-b`. Both sets and both sessions use the same two zones. If a zone can't supply the shape, the next pair in the order (a, c), (b, c), (a, f) is used, and recorded. That session is not a replicate under rule 11 (A13): its rows are reported, and a session on the registered zones is still owed.
- **CPU platform (added after the review):** EXP-11 ran on Cascade Lake. A minimum CPU platform can't pin it: N2's other platform, Ice Lake, is newer, so a minimum of Cascade Lake admits both ([Google's CPU platforms page](https://docs.cloud.google.com/compute/docs/cpu-platforms), read October 24). So each VM's platform is read before anything runs. A set with a VM on another platform is torn down and created again, up to 3 times, recorded. If it still differs, the session runs, and it is not a replicate of a session on Cascade Lake (A13).
- **Filesystem:** ext4 on the whole disk, `startup.sh`'s options (lazy initialisation off, so no background zeroing runs during the runs; mounted `noatime`; journal in ordered mode). The runs refuse to start unless `/data` is ext4 in ordered mode with barriers, on the set's data disk, of the set's size (EXP-11's preflight, extended). Debian's read-ahead, I/O scheduler and page-reclaim settings stay at their defaults, recorded per disk (`/sys/kernel/mm/lru_gen/enabled` among them).
- **pgdelta at EXP-11's code, plus one statistic** (the lead, under Measured), which changes no behaviour. `DEPLOYED.json` records the diff of `pgdelta/` from EXP-11's deployed commit.
- **Quotas, read before each set is created** (`create.sh`'s check, and smoke item 1): the project's 32 vCPUs across all regions (a set uses 16); 24 instances and 8 external addresses in `us-central1` (a set uses 2 and 2); and the regional quota for SSD persistent disks, which `pd-balanced` counts against: 4,100 GB for `t9pd-2000` (two 2,000 GiB data disks and two 50 GiB boot disks), 400 GB for `t9pd-150`, plus what runs beside. If `t9pd-2000` doesn't fit, nothing runs and the owner is asked: raising a quota is the owner's call.

**The published limits** that apply ([disk performance](https://docs.cloud.google.com/compute/docs/disks/performance), read October 24 and again after the review):

| | `t9pd-2000` | `t9pd-150` |
|---|---|---|
| Throughput: 0.28 MiB/s per GiB of the VM's `pd-balanced` disks, plus 140 MiB/s per VM | 140 + 0.28 × 2,050 = 714 MiB/s | 140 + 0.28 × 200 = 196 MiB/s |
| N2 VM with 8–15 vCPUs: most it can read, and write | 800 and 800 MiB/s | 800 and 800 MiB/s |
| IOPS, reads and writes shared: 6 per GiB plus 3,000, at most 15,000 on this VM | 15,000 | 4,200 |
| **This design's cap, each way** | **714 MiB/s = 0.749 GB/s** | **196 MiB/s = 0.206 GB/s** |

The 50 GiB boot disk is `pd-balanced` too, so it counts. Read and write throughput limits are independent; IOPS are shared. **No burst:** the page gives `pd-balanced` a baseline and no burst (read October 24); the first and last repeats' `parcopy8` times check that nothing ran out during a trial (A10). A VM with 16–31 vCPUs would lift the cap to 1,200 MiB/s; that is a limitation, not an arm. The size each VM reports for its data disk is recorded (smoke item 2), and the cap is computed from it.

### Data, sizes and trials

- pgbench with EXP-01's `events` table (as many rows as `pgbench_accounts`) and EXP-01's seed; **hot and append** at 1%, the two workloads EXP-09's HA rule names. Each workload is its own trial, with its own source cluster and B0, as in EXP-09 to EXP-11.
- **Manifest checksums (rule 7):** CRC32C for every `pg_basebackup`, B0 included, with EXP-11's guard.
- **Two sizes:**

| Size | pgbench scale | Database (a full copy's bytes) | Sets | Role |
|---|---:|---:|---|---|
| **S** | 70 | 2.12 GB | `t9pd-2000` | the ledger's registered size for this arm; the bridge to EXP-11 |
| **L (added here)** | 1,600 | about 48.5 GB (45 GiB) | both | **the deciding size**: Omni's regime |

**Why 48.5 GB.** It is the smallest size, with a margin, at which nothing in a run fits in memory the way it did at 2.1 GB:
- **It exceeds dest's page cache by half.** An `n2-standard-8` has about 31 GiB of `MemTotal`, and after a cache drop the page cache can take about 30 GiB. B (48.5 GB) is about 1.45 times `MemTotal`. So no v1 run can hold both of its passes over B in memory, and no full copy's output stays cached whole.
- **Every full copy crosses Debian's write-back thresholds early.** `vm.dirty_background_ratio` is 10 and `vm.dirty_ratio` 20, of about 31 GiB: about 3 and 6 GB. At 2.1 GB, every full copy stayed below both, so its whole flush landed at its end. At 48.5 GB the kernel writes back during the stream, as it would at TB scale.
- **The tables split into 1 GiB segments.** `pgbench_accounts` and `events` become about 20 segment files each. So per-file parallelism (`pg_probackup`) is no longer capped by two large files, the limitation EXP-11 said favoured v1.
- **Not D33's cap (corrected after the review).** The receiver keeps at most 64 MiB of B's page digests (`LEAF_CACHE_BYTES`): 16 bytes per 8 KiB page, so 32 GiB (34.4 GB) of B at once. It holds a file's digests from the moment its basis thread hashes the file until the file's `FILE_DONE`. So the cap binds when the files hashed but not yet done exceed 34.4 GB, not when B does. At L that needs a leaf lead (Measured) of 34 GB or more, which isn't expected.
- **Why not larger.** On the 2,000 GiB disk every pass over 48.5 GB takes about 65 s, and a v1 run with its checks about 5 minutes; each doubling doubles that. But h at 48.5 GB isn't carried to larger sizes. If the lead grows with the run's length, h falls as the size grows; the lead's trace here shows whether it grows during a run. T9 at 100–200 GB stays later, and no ratio here is projected to TB scale (rule 10).
- **How much of each pass reaches the disk isn't assumed.** It is measured in every run, with the lead and the page cache's lists that explain it (below). B being larger than the cache guarantees only that not all of it can stay there.

**The dataset at L is built once per set and session.** The scale-1,600 cluster is built on the source's data disk in the set's smoke run, stopped cleanly, and kept as a pristine copy beside it. Each L trial restores it with 8 concurrent per-file copies (untimed, changed after the review: one `cp -a` reads with one reader), starts it, takes B0, and runs its workload. So every L trial's source cluster is a fresh file copy of the same data. S trials build their cluster as EXP-11 did.

### Blocks and order

| Invocation | Set | Size | Trials, in order | Main block | Other blocks |
|---|---|---|---|---|---|
| 1 | `t9pd-2000` | S | hot, append | 5 repeats, the new-copy arms in each | – |
| 2 | `t9pd-2000` | L | hot, append | 10 repeats; v1 at 8 jobs in the first 5 | the warm-source block: 3 repeats |
| 3 | `t9pd-150` | L | hot, append | 5 repeats | – |

- Within each (trial, repeat), every arm runs once, in a seeded random order. `full_nosync` runs in each trial's first repeat only.
- The same seeds and order in both sessions, so the replicate differs in time and VMs only (FIRMNESS's revision 5 for EXP-13). The sets run one after the other, `t9pd-2000` first, each beside T12r's pair (16 + 16 vCPUs); or both at once, on a day T12r doesn't run (32 vCPUs). Which is recorded. Every deciding ratio compares arms within one set and one repeat, which neither touches.
- **The warm-source block** (invocation 2 only): 3 repeats of `parcopy8`, `full`, `probackup4`, `probackup8` and v1 at 4 jobs, with the source's data directory (without `pg_wal`) read once, untimed, before each run, instead of the drop. At L that leaves the last 25–31 GB read in the source's cache, what a live primary holding most of its working set might have. `probackup4` was added after the review, so that every arm that can be B_rel's has a warm reading.

**Order within a trial:**
1. The source cluster (built at S; restored from the pristine copy at L), started. B0 by `pg_basebackup` (CRC32C), synced. The workload and its `CHECKPOINT`.
2. The start reference digest: a clean stop, `agent.py datadir-digest`, a restart.
3. Probes: the durable write floor (dest); the 4-reader cold read floors (the source's data directory, and B0 on dest; dest's added after the review); the single reader (source); the bracket; 20 pings and the link tests (one stream both ways, 8 streams from source to dest).
4. The main block. Each repeat starts with 20 pings.
5. At L on `t9pd-2000`: the warm-source block.
6. The write floor and both read floors again; the link tests again.
7. The end reference digest.
8. The source's logical digest: always at S; at L only if a `probackup*` run is pending (see Verification).
9. B0's full digest.
10. EXP-01's row-level ground truth: at S only (it is EXP-01's study, and costs minutes at L).

Between steps 2 and 7 no SQL reads a user table on the source, as in EXP-11.

### Cache state at the start of every run

As EXP-11's table, with these additions:
- **Both VMs:** `sync; echo 3 > drop_caches` after the untimed set-up, except the source in the warm-source block. `MemAvailable`, `Cached`, `Active(file)` and `Inactive(file)` are recorded just after the drop.
- **v1's old copy B:** copied from B0 by 8 concurrent per-file copies (`cp --reflink=never`, each file once, directories and modes as `cp -a` leaves them), then `sync -f B`, before the drop: cold and already durable, as EXP-09 and EXP-11. Changed after the review: one `cp -a` reads with one reader, 2–8 minutes at L at the single reader's rate.
- **Warm source (L):** after the untimed read, `fincore` records how much of the source's data directory is cached when the clock starts.
- **No harness write to the disk under test inside a run's window** except the method's own and the driver's log lines, as in EXP-11. The samplers hold their samples in memory; records are written after the clock stops.

### What the clock includes, and durability

**As EXP-11, unchanged:** one timed phase with one counter window per run; the clock is `perf_counter` on dest. The same components are in and out: backup mode and a fast checkpoint in for the live-source arms, out for v1, the new-copy arms and `parcopy8`, which read a cleanly stopped source; `pg_probackup`'s timed `sync -f` in. A run is durable by EXP-11's definition: every byte the method wrote covered by a successful `fsync`, `fdatasync` or `syncfs` after its last write, and every directory change covered by EXP-11's directory rule on ext4 in ordered mode, before the clock stops.

**Checked as in EXP-11,** with the persistent disk in place of the md array:
- **(a)** `Dirty + Writeback` on dest rises by at most max(4 MiB, the idle noise for a window as long as the run) over the run. The idle noise is measured in each set's smoke run over windows of 5, 30, 120 and 600 s, since L's runs last minutes (EXP-13's noise table).
- **(b)** For every full-copy arm, the bytes written to dest's data disk during the window (`/proc/diskstats`, that device) are at least 0.98 × the output's bytes.
- **The syscall audit** (EXP-11's, `strace` on dest), one untimed run of every arm at scale 5 on `t9pd-2000`, the new-copy arms included, in the smoke run.
- **The negative control:** `full_nosync` must fail (a) in every run. At L, write-back during the stream leaves only about the background threshold dirty, so its margin is smaller: at least 1 GB (E15).

These checks catch a tool that skips its flush. They don't prove durability: no power is cut. A persistent disk acknowledges a write once it is stored durably, so its cache flushes cost a round trip, which every arm pays per `fsync`.

### Verification (untimed, every run)

As EXP-11's table, with one change at L:

| Arm | Contents | Started? |
|---|---|---|
| `full`, `full_syncfs`, `full_nosync` | `pg_verifybackup` with the CRC32C manifest. It reads file by file with one reader, so at L it takes about one reader's pass (priced in the run plan) | yes: recovers from `backup_label`, answers a query, stopped |
| `probackup*` at S | EXP-11's: the per-table logical digest of the started output, against the source's at the trial's end; the byte diagnostic in each trial's first repeat | yes, with no WAL fetched after the clock stops |
| **`probackup*` at L (changed here)** | **Bytes first.** Every file of the benchmark database's user relations (heap, indexes, TOAST, free-space and visibility maps) must be byte-identical (SHA-256) to the trial's start reference digest. If any differs, that run's per-table logical digest is taken and compared with the source's at the trial's end, as EXP-11 did. Other files that differ are reported by class (catalogs, other), as EXP-13 does | as at S |
| `parcopy8`, v1 at 4 and 8 jobs, the new-copy arms (S) | EXP-11's: byte-identical to the stopped source's selection (`verify_direct`), no in-place or staging leftovers, the `fsync` count for `parcopy8` | yes |
| every arm | B0 unchanged: a stat snapshot per run, a full digest per trial | – |

- **Why the change.** A logical digest reads every row: about 28 s per 2.1 GB in EXP-11, so about 11 minutes per `probackup*` run at L, about 7 hours per session on `t9pd-2000` alone. Byte identity of the user tables' files is a stronger check of the same rows. The logical digest stays the standard wherever bytes differ.
- **The void rule is EXP-13's:** a `probackup*` run is void, not failed, only if the trial's start and end reference digests differ in a user-table file. EXP-11's reference digests differed in system catalogs in every trial, which made its A4's logical leg impossible; EXP-13 fixed that, and this keeps the fix.
- At L the digests on both sides hash files in parallel (4 workers), so each takes about one pass at the disk's rate instead of being bound by one core's SHA-256. What they check is unchanged.

### Measured

**In every run:** EXP-11's columns (`total_s`, `total_no_ssh_s`, wire bytes, VM-wide CPU on both VMs, per-process rusage and peak RSS, dest's disk bytes, `Dirty` and `Writeback` before and after, each arm's own phase timers), and, added here:
- **Disk bytes read and written in the window on both VMs' data disks.** For v1, `dest_read_share`: dest's disk reads over twice B's bytes. 0.5 means the verification re-read came wholly from the cache, 1.0 wholly from the disk, so **h = 2 − 2 × `dest_read_share`**. For the full copies, the source's disk reads over the output's bytes.
- **The lead (added after the review).** The receiver's first pass over B runs on its own thread (`_hash_basis` in `pgdelta/receiver.py`), file after file, bounded only by the disk and the hashing pool. The sender's inbox thread takes every `BASIS_NODES` frame as it arrives, and the sender's 4-file window bounds only how far the source's hashing runs ahead of sending. Verification is queued too, and can fall behind `FILE_DONE`. So a new pgdelta statistic (statistics only) records, at every basis file hashed, every `FILE_DONE` and every file verified, the time and B's bytes hashed, done and verified. Two leads come from it:
  - **the leaf lead,** hashed minus done: what D33's digest cache holds;
  - **the cache lead,** hashed minus verified: about the bytes read into the page cache between a file's first read and its re-read.

  Each is resampled every 0.5 s beside the dirty sampler, with its peak and its slope over the run.
- **The page cache's lists on dest,** every 0.5 s: `Active(file)` and `Inactive(file)` from `/proc/meminfo`, and `workingset_refault_file`, `pgactivate` and `pgdeactivate` from `/proc/vmstat`. Pages read once wait on the inactive list. Since Linux 4.10 that list may shrink to about 1/(1 + √(10 × the cache's GB)) of the cache, about 1/18 here, once pages read twice (verified ones) fill the active list. So the share of the cache that can hold a file between its two reads isn't known either; these counters measure it.
- **A dirty-page sampler on dest,** every 0.5 s: the peak of `Dirty + Writeback` during the run, and its value when the stream ends.
- **The share of B cached when the clock stops** (`fincore`, before verification), for v1.
- **pgdelta's own statistics** that bear on size: the prefetch's bytes and its cap, `leaf_nodes_reread` and `leaf_nodes_cached` (D33), the lead, and the receiver's hashing, verification and wait timers.
- **`pg_probackup`'s read rates (added after the review):** in every `probackup*` run at L, the read bytes of each of its processes and threads on the source (`/proc/<pid>/task/<tid>/io`), every 0.5 s: its total rate and each thread's.
- **The pings** before each repeat (20, 0.05 s apart, as root): minimum and median.

**Twice per trial, at its start and end, untimed** (EXP-11's probes, sized for L):

| Probe | What | Expected |
|---|---|---|
| durable write floor (dest) | 4 writers write as many bytes as B0 holds, buffered, write-back every 8 MiB, then `fsync` each | 0.90–1.00 of the disk's cap: S 2.83–3.15 s; L 64.7–72 s on `t9pd-2000`, 236–262 s on `t9pd-150` |
| cold read floors (source and dest; dest's added after the review) | 4 readers: of the source's data directory without `pg_wal`, and of B0 on dest | the same bands |
| one reader (source; start only) | one reader of the first 4 GiB of `pgbench_accounts` (S: its whole file), after a drop. This is the rate one `pg_basebackup` stream, `cp -a` and `pg_verifybackup` can read at | 0.10–0.45 GB/s on `t9pd-2000`; on `t9pd-150` the same, capped at 0.206 (E2) |
| link across the zones | 20 pings; one stream each way, and 8 streams from source to dest, 6 s each with a 3 s warm-up | E1 |
| backup-mode bracket | EXP-11's | median 0.03–0.20 s |

**floor(size)** is max(dest's durable write floor, the source's cold read floor, the full copy's bytes at the 8-stream rate), each the median of its trial measurements over hot and append.

**v1's floor (A12)** is max(the source's cold read floor, dest's cold read floor of B0), per trial, each from the faster of its start and end measurements. It is the time to read S and B once, which v1's design can't avoid.

## Arms

Every arm reads and writes the disk under test. Nothing is throttled: the link is the zones' own.

| Arm | Yardstick | What runs | Source | Durable by | Blocks |
|---|---|---|---|---|---|
| **`pgdelta_direct_inplace`** (v1, 4 jobs) | the arm judged | EXP-11's v1 arm unchanged: 4 jobs on each end, `--source-kind datadir`, `--in-place`, `--fsync main`, `--prefetch-basis on`, in clear | stopped cleanly | pgdelta's in-place commit | every block |
| `pgdelta_direct_inplace_j8` (v1, 8 jobs) | beside the verdict | the same with `--jobs 8` on both ends | stopped cleanly | as v1 | `t9pd-2000`'s main blocks (at L, the first 5 repeats) |
| `parcopy8` | **rule 3's (B_fast)** | EXP-11's bound unchanged: 64 MiB ranges over 8 TCP streams, write-back every 8 MiB, a parallel `fsync` of every file and directory | stopped cleanly | its own | every block |
| `full` | **released (B_rel); Omni today (F0)** | `pg_basebackup -Fp -X stream -c fast --manifest-checksums CRC32C --verbose`, one stream, `--sync-method=fsync` | running, idle | its default flush | every block |
| `full_syncfs` | released (B_rel) | `full` with `--sync-method=syncfs` | running, idle | `syncfs` of `/data` | S only |
| `probackup8`, `probackup4` | **released (B_rel)** | EXP-11's `pg_probackup catchup -b FULL --stream --temp-slot -j 8` (or `-j 4`) over SSH (`aes128-gcm`), with its WAL ticker, then `sync -f` of the output in the same phase | running, idle | tool + `sync -f` | every block |
| `full_nosync` | negative control, never a baseline | `full` with `--no-sync` | running, idle | not durable | each trial's first repeat |
| **`pgdelta_direct`, `pgdelta_direct_wb`, `pgdelta_direct_worker`** (the new-copy mode; added after the review) | beside: D86 and D85's price | v1's path writing a new copy O instead of patching B: 4 jobs, `--source-kind datadir`, copy-then-patch, pipelined verification, `--window 4`, `--prefetch-basis on`, with `--fsync main`, `writeback` or `worker` | stopped cleanly | pgdelta's publication | invocation 1's main block |

**Runs per session:**

| Invocation | Trials | Runs per trial | Runs |
|---|---|---|---:|
| 1: `t9pd-2000`, S | hot, append | 10 arms × 5 repeats, plus `full_nosync` once | 102 |
| 2: `t9pd-2000`, L | hot, append | 5 arms × 10 repeats, v1 at 8 jobs × 5, `full_nosync` once; then 5 arms × 3 warm | 112 + 30 = 142 |
| 3: `t9pd-150`, L | hot, append | 5 arms × 5 repeats, plus `full_nosync` once | 52 |
| **all** | | | **296** |

The 5 arms at L are v1 at 4 jobs, `parcopy8`, `full`, `probackup4` and `probackup8`.

**Why these arms.**
- **v1's arm is fixed in advance:** 4 jobs, EXP-09's and EXP-11's registered arm. 8 jobs is reported beside it, as EXP-11 did, since D50 (4 workers) is provisional and T10b sizes the worker count. On `t9pd-150` it isn't run: there every arm is bound by a disk of 196 MiB/s, far below the pool's CPU at either count (E8).
- **`parcopy8` is rule 3's yardstick,** EXP-11's bound unchanged. It reads with 8 parallel readers and writes back every 8 MiB, so on a capped disk it should sit just above floor(size).
- **The released yardstick** is what Omni could run today with no new code: one `pg_basebackup` stream (what Omni runs to seed a standby) and `pg_probackup catchup` (the only released parallel full copier found, EXP-11). At L, with tables in 1 GiB segments, `pg_probackup`'s per-file threads can finally split the work.
- **`full_syncfs` runs at S only.** The ledger's note asks for it beside `full`, and at S it may differ on a persistent disk: `full`'s fsync method flushes about 1,000 files one by one, each a round trip to the disk. At L both flush most of the copy during the stream (E12), and their difference is the final flush of about the write-back threshold. See Changes, item 6.
- **10 runs at the deciding cell (added after the review).** At L on `t9pd-2000` the five arms that can decide a row run 10 repeats per workload, as EXP-11's verdict did (its A9) and FIRMNESS's T9-PD asked. `R_fast` is expected near 1.00, where the median's noise matters. Elsewhere 5, the ledger's number.
- **The new-copy arms at S (added after the review).** D86's question, the fsync on a thread against early write-back, is about a flush that lands at the end, which it still does at 2.1 GB. No queued test had a new-copy arm: FIRMNESS's T9 at scale lists none. At S each run takes under a minute.
- **Hot and append** are the two workloads EXP-09's HA rule names. Uniform isn't run (Considered and not run).

### Considered and not run

| Option | Why not |
|---|---|
| Local NVMe beside the persistent disks, as the ledger's note says ("beside the local NVMe") | Local-SSD shapes were out of stock in three of four zones on October 22 (`config.sh`), and T12r, as FIRMNESS proposes it (its revision 10 for EXP-13), replicates EXP-11's NVMe cells on fresh pairs. The size effect is measured within a set by S against L on the same disk. Rule 8, item 2 |
| A pair in one zone on persistent disks | It would split the zones' effect from the disk's. But the question is the verdict on Omni's conditions as a whole, and the link tests, pings and the arms' own timers say whether the link or the disk sets each arm's pace |
| `pd-ssd` beside `pd-balanced` (dropped after the review) | On this VM its cap is 12% above `pd-balanced`'s (800 against 714 MiB/s), with the same 15,000 IOPS, and the same rows were expected. It cost about 4 hours, 2 TB of cross-zone traffic and 4,000 GB of SSD quota per session. The smaller `pd-balanced` disk is the contrast likely to move a row, and FIRMNESS's T9-PD asked for it. Which class Omni's standbys use stays an integrator question |
| Both disk sizes on one pair | A VM's limit counts all its `pd-balanced` disks, so a small disk beside a large one isn't small (Machines) |
| Hyperdisk, regional persistent disks | Newer series (N4, C4) take only Hyperdisk, which GKE nodes on them would use. Omni's HA standby has its own zonal disk; a regional disk is a different HA design. Both are later questions |
| Uniform 1% | It decides no row of EXP-09's rule. At L it changes about 46% of `pgbench_accounts`'s pages (EXP-01), about 14 GB on the wire for v1, and costs a trial build. EXP-11 reported it at 2.1 GB |
| `parcopy4`, matched parallelism | EXP-11: within 0.10–0.15 s of `parcopy8`, reported only. At L both are capped by the disk |
| `full_syncfs` at L | See Why these arms, and Changes item 6 |
| `full_wb` (dest's write-back at 64 MiB, host-tuned) | Not a released arm. Its effect is bounded by the view `R_wb`, every released arm read without its final flush. T8 runs dest's write-back at the default and at 64 MiB, and OMNI-2 reads `vm.dirty_*` on a GKE node |
| v1 with source and dest at different worker counts | T10b's (its dest-only arm) |
| The new-copy mode at L | It writes a whole second 48.5 GB copy, and beyond RAM its question is T9's. At S it runs (Arms). T9 at scale's ledger entry should carry the new-copy arms (Changes, item 13) |
| Throttled link speeds, crossovers | HA's link is the zones' own. Crossovers are EXP-11's (one zone) and EXP-13's (DR) |
| A memory limit on v1's receiver (`systemd-run --scope -p MemoryMax=...`), as a pod's or sidecar's limit would set (reconsidered after the review) | The page cache is charged to the memory cgroup of the process that reads, so a pod's or sidecar's limit would shrink the cache v1's re-read can use. Omni's limits aren't known, and a guessed one would add a reading no route could use. The arithmetic already says what a small cache does: with h = 0, `R_rel` is 1.28–1.99 (E10), so "faster than the released copiers" would fail. That row therefore stays provisional for Omni until OMNI-2 reads the limits (Rule 11) |
| Pinning the CPU platform with `--min-cpu-platform` | It can't pin Cascade Lake on N2 (Machines). The platform is checked and recorded instead (A13) |
| 100–200 GB | T9, later (rule 10) |
| A busy source; TLS for pgdelta | T10b and T8; T12 and T8 |
| GKE itself | OMNI-2 (K4). Not part of this test |

## Baselines, ratios and views (added here)

Per set and size, over the arms that pass every verification and durability check, without void runs:
- **B_fast,** rule 3's yardstick: the fastest of `parcopy8`, `full`, `full_syncfs` (S), `probackup4` and `probackup8`.
- **B_rel,** the released tools' yardstick: the same without `parcopy8`.
- **F0:** `full`, what Omni runs today.

**Choosing the arm** for each envelope: the lowest median over the main block's runs pooled over hot and append (20 per arm at L on `t9pd-2000`, 10 elsewhere), since a full copy's work doesn't depend on the workload. Ties within max(0.05 s, 1%) go to the lower pooled maximum. The warm block's runs don't enter the choice.

**Ratios,** per workload, v1 at 4 jobs: v1's median over the chosen arm's median, both over the trial's main-block runs (10 at L on `t9pd-2000`, 5 elsewhere):
- `R_fast`, `R_rel`, `R_F0`.
- `R_adj`: v1 plus `full`'s own backup-mode adder (its checkpoint wait and its wait from the WAL end point to its sync, from its `--verbose` stamps), over a live-source envelope. Against `parcopy8`, `R_adj` equals R. On an idle source the adder was 0.045 s in EXP-11, so at L it can't move a ratio by more than about 0.001.
- **Robustness views,** each read as its own row: `R_max` (maxima), `R_noSSH` (`total_no_ssh_s`), `R_paired` (the median of v1's run over the baseline's in the same repeat), and `R_pool` (against the baseline's median pooled over hot and append).
- **`R_wb`** (released yardstick only; extended after the review): B_rel recomputed with every released arm read without its final flush: `full`'s and `full_syncfs`'s sync phase, and `pg_probackup`'s own sync of its files (`probackup_files_sync_s`) and its `sync -f` (`probackup_sync_s`). It bounds what dest's write-back setting could give the released copiers. The first version removed only `full`'s, though at L B_rel's arm may well be `pg_probackup`.
- **Warm (L, `t9pd-2000`):** v1's warm median over the warm medians of the arms that are B_fast's and B_rel's in the main block; every candidate ran warm (3 runs each).
- **A12's target and floor,** per workload: 0.9 × B_fast against v1's floor.
- **Beside, never the row:** each ratio with v1 at 8 jobs; each ratio on `t9pd-150`; and at S.

## Predictions and decision rules

Every prediction and rule is judged per workload on medians of the trial's main-block runs (10 at L on `t9pd-2000`, 5 elsewhere), with the maximum beside it (rule 6), unless it says otherwise. Each session is judged on its own first; [Rule 11](#rule-11-which-outcomes-may-pick-a-route) says what the two together may decide.

### The ledger's registered text (verbatim)

From T9's section of the ledger:

> *Added October 24: v1's path and the full copy on persistent disks (GKE's default), 5 repeats each, beside the local NVMe. Both move about 2.1 GB through the destination's disk: the full copy writes it, and v1 reads all of B to hash it (and re-reads it to verify once it no longer fits the cache). Prediction: the ratio moves toward 1.0, to 0.9–1.1. Rule, EXP-09's bands on medians of 5: at most 0.9 for both hot and append means the HA result holds on persistent disks; 1.0 or more for either means v1 targets DR first there; in between is undecided until T2 and T5, as in EXP-09.*
>
> *Added October 24 (after T2): T2 found the HA verdict Out on local NVMe against the fastest durable copy, a range-parallel copier ([EXP-11](experiments/EXP-11-full-copy-baseline.md)). By rule 3, the persistent-disk arm should be judged the same way: against the fastest durable copy measured there, so `parcopy8` and `full_syncfs` run beside `full` (EXP-11's harness has both).*

### How it is judged here

The registered text is about 2.1 GB, so it is judged at **S on `t9pd-2000`**, as written:

| ID | Registered | How it is judged | Expected here (added) |
|---|---|---|---|
| P1 | "the ratio moves toward 1.0, to 0.9–1.1" | "The ratio" in the first note is v1 over the full copy, `full` (`R_F0`). Holds if `R_F0` is in [0.90, 1.10] for hot and for append. `R_fast` is reported beside it | **Fails low.** `R_F0` 0.12–0.47: one `pg_basebackup` stream reads with one reader, which is slow on a persistent disk (E2), and its whole 2.1 GB flush still lands at its end, at the disk's write rate (E5). `R_fast` 0.79–1.13 |
| P2 | the rule, EXP-09's bands | Judged twice (K6): against B_fast, as the second note says, and against B_rel. "Undecided until T2 and T5" is read as "until T5 and T13", EXP-11's Undecided row, since T2 is done (Changes, item 5) | Against B_fast: **Undecided or Out, near 1.00** (A12 may go either way at S). Against B_rel: **at most 0.9 for both** (`R_rel` 0.21–0.50) |

**The registered consequence is recorded as written,** but at 2.1 GB it picks no route on its own: 2.1 GB fits in RAM, which Omni's rebuild doesn't (rule 11, test P). It counts toward a route only where the verdict at L agrees.

### The HA verdicts (added here)

**Judged at L on `t9pd-2000`, with v1 at 4 jobs, separately against each yardstick (K6),** by EXP-09's bands as EXP-11 read them. The same rows are computed at S, on `t9pd-150`, and with v1 at 8 jobs, and reported beside. Rows are checked in order.

**Against the fastest copy measured (rule 3; B_fast):**

| Row | Condition | What follows (in this session) |
|---|---|---|
| **Out on fast links** | `R_fast` ≥ 1.00 for hot or append | "Out against the fastest copy measured, on a 2,000 GiB `pd-balanced` disk across zones at 48.5 GB; `R_fast` = x" |
| **Within reach** | `R_fast` ≤ 0.90 for both, `R_adj` ≤ 0.90 against B_fast, the bound passes its gate (A3), and A12 doesn't hold | "Within reach of any copier on Omni's default disk at this size" |
| **Bound missing** | `parcopy8` excluded (A4, A5), or failing its gate while slower than B_rel's arm | Judged against B_rel alone, labelled "bound not measured"; provisional |
| **Tie at the disk cap** (added after the review) | `R_fast` < 1.00 for both, and A12 holds for both | "v1 ties the fastest copy measured at the disk cap (`R_fast` = x, y): within reach is unreachable for v1's design here" |
| **Undecided** | anything else | Decided after T5 and T13, and T9 at scale |

**A12 is expected to hold, so Within reach is out of reach at L.** v1 reads all of S on the source and all of B on dest, so it takes at least its floor, D/C, 64.8 s on `t9pd-2000`. A bound that passes its gate takes at most floor + 5%, about 68.0 s, and 0.9 × 68.0 = 61.2 s, under v1's floor. On `t9pd-150`: a floor of 236 s against a target of at most 223 s. At S, v1's floor is about 2.83 s and the target 0.9 × 3.1–3.9 = 2.8–3.5 s, so A12 may go either way there. So the live rows against the fastest at L are Out and the tie. Written now, before running.

**A tie is reported as a tie (added after the review).** At L on `t9pd-2000` the pool's CPU (about 65 s at 4 jobs) and the disk's time (64.8 s) are nearly equal. So if the re-read comes from the cache, v1 and the bound meet at the disk cap, and `R_fast` is likely within ±0.03 of 1.00. A real tie stays near the line in any number of sessions. So the near-line flag doesn't block A12 or the tie, and a tie in both sessions gets no third session (Rule 11).

**Against the released tools Omni can run (B_rel):**

| Row | Condition | What follows (in this session) |
|---|---|---|
| **Faster than the released copiers** | `R_rel` ≤ 0.90 and `R_adj` against B_rel ≤ 0.90, for both hot and append | "v1 is faster than the released copiers tested (`pg_basebackup` with fsync, `pg_probackup catchup -j 4` and `-j 8`) on a 2,000 GiB `pd-balanced` disk (714 MiB/s) on `n2-standard-8`, across zones, at 48.5 GB, with a VM-sized page cache and Debian's write-back, from a stopped source", EXP-11's qualifier carried to the conditions measured |
| **Not faster than the released copiers** | `R_rel` ≥ 1.00 for hot or append | "Out against the released copiers too" |
| **Undecided against the released copiers** | anything else | Reported with `R_rel` per workload |

`R_F0` is stated beside every row: v1 against one `pg_basebackup` stream, what Omni runs today.

**Flags,** beside each verdict:
- *Fragile:* `R_max`, `R_noSSH`, `R_paired` or `R_pool` would give another row.
- *Near the line:* a deciding ratio within ±0.03 of 0.90 or 1.00, whose crossing alone or with others within that margin would change the row. Against the fastest at L it doesn't block A12 or the tie (above).
- *Write-back-sensitive* (released verdict): `R_wb` gives another row.
- *Source-cache-sensitive* (L, `t9pd-2000`): the warm ratios give another row. A live primary keeps much of its data cached, which one `pg_basebackup` stream would profit from most.
- *Bound suspect:* A3 fires.
- *Disk drifted:* A10 fires in a trial the row uses.
- *With void runs:* a trial has void `probackup*` runs; the verdict is also given with them included.

**Readings beside, never the row:**
- *8 jobs:* the same rows with v1 at 8 jobs. If they differ from 4 jobs', both are stated, and the difference belongs to D50 and T10b.
- *`t9pd-150` (changed after the review):* the same rows on the 150 GiB disk. If they differ from `t9pd-2000`'s, the verdict is "disk-size-sensitive": both are stated, and every positioning names the disk size.
- *S against L:* if the rows differ, the size changes the verdict, and L decides it.
- *The lead and h* (W2, D41p): h per run, the leads' peaks and slopes, and the page cache's lists. They explain v1's time; they don't choose.

### Added predictions

**Inputs, written now.** Each is labelled by where it comes from. The derivation is code (`analysis/exp14_model.py`, to be built, as EXP-13's model was). After the smoke runs measure the inputs, it runs once on them, and its ranges are recorded here, dated, beside these. **Every E-range is judged as written here;** the recomputed one is reported beside it.
- C, each way: 0.749 GB/s on `t9pd-2000`, 0.206 GB/s on `t9pd-150` (published, above).
- r1, one sequential buffered reader on the disk: 0.10–0.45 GB/s on `t9pd-2000`; the same, capped at 0.206, on `t9pd-150` (assumed). Debian's read-ahead (128 KiB by default, recorded) keeps one or two requests in flight, and each request to a persistent disk takes about 0.5–1.5 ms. So r ≥ C needs several readers.
- **`pg_probackup`'s rate, two readings (changed after the review).** EXP-11 didn't show what limits it. **(a) Its threads add:** each moves min(r1, 0.24 GB/s), the rate of the thread that copied the largest file (0.94 GB) if that file was its critical path. **(b) The tool as a whole moves about 0.54 GB/s:** EXP-11's copy phase moved 2.12 GB in 3.8–4.0 s unthrottled, 4 threads were as fast as 8, and at 2 and 5 Gbit/s its copy phase ran 1.75–1.92 s above link time (`exp11-cells.csv`). Smoke item 10 measures both rates at L.
- The link across zones: E1 (assumed).
- v1's destination pool processes both passes over B at 1.5 GB/s with 4 jobs and 1.7 GB/s with 8; its source hashes at 1.33 GB/s with 4 (EXP-11: 2 × 2.12 GB in about 2.8 s of the 3.11 s run, 2.45 s of 2.76 s at 8 jobs; the source's hashing 1.59–1.61 s).
- **h, the share of v1's verification re-read served from the page cache (changed after the review):** 1.0 at S; at L anywhere from 0 to 1. It depends on the cache lead, which nothing in pgdelta bounds, and on how much of the cache holds pages read once (Measured). h is measured in every run, and the model derives it again from the measured lead and lists. The first version's 0.8–1.0 rested on the sender's 4-file window, which bounds the source's hashing, not this lead.
- Write-back: Debian's thresholds, about 3 and 6 GB on dest. At L a stream that arrives no faster than the disk writes keeps `Dirty` near the background threshold, so the final flush is about 3–6 GB: 4–8 s on `t9pd-2000`, 15–30 s on `t9pd-150`.
- Each `fsync` on a persistent disk: 0.5–2 ms (assumed).

| ID | Prediction | Basis |
|---|---|---|
| E1 | **The zones:** ping median 0.15–0.8 ms; one TCP stream 7–15 Gbit/s; 8 streams 14.5–16 Gbit/s. One stream isn't window-limited (3 MiB per round trip is above 30 Gbit/s at 0.8 ms) | EXP-11 in one zone: 10.5–15.1 and 15.2–15.3 Gbit/s; the 16 Gbit/s egress cap |
| E2 | **The disks:** the write floor and both 4-reader read floors at 0.90–1.00 of C (probes table). One reader: 0.10–0.45 GB/s on `t9pd-2000`, 0.10–0.206 on `t9pd-150` | published caps; r1 |
| E3 | **Memory and size:** `MemTotal` 30.5–32 GiB; B0 at L 1.4–1.6 times it. 25–31 GB of the source's data cached after the warm read | the machine type; Linux's reserved memory |
| E4 | **`parcopy8`:** S 3.1–3.9 s; L 65–68 s on `t9pd-2000` and 236–248 s on `t9pd-150`. It passes its gate (A3) everywhere | floor plus EXP-11's 0.3–0.8 s of start and launch; 8 readers reach C |
| E5 | **`full`:** S 7.5–26 s, its sync phase 2.9–4.5 s; L 110–495 s on `t9pd-2000`, its sync phase 3–9 s; 250–520 s on `t9pd-150`, its sync phase 15–30 s. At S, `full_syncfs` 0–1.5 s faster than `full` (one `syncfs` instead of about 1,000 round trips to the disk) | its stream at one reader's rate; at S the whole 2.1 GB flushed at its end at C, plus about 1,000 per-file `fsync`s; at L the final flush of what is dirty when the stream ends |
| E6 | **`pg_probackup`:** S, both arms 7–14.5 s. L on `t9pd-2000`, reading (a): `probackup8` 68–85 s, `probackup4` 68–140 s; reading (b): both 92–102 s (`probackup4` up to 131 s if one reader gets less than 0.135 GB/s). On `t9pd-150`, either reading: 250–275 s, bound by the disk | at S the largest file (0.94 GB) on one thread, then its flush of 2.1 GB at C; at L (a) the copy at min(threads × one thread's rate, C), (b) at min(0.53–0.56 GB/s, C), plus at most one 1 GiB segment's tail, its start and its flush |
| E7 | **v1 at 4 jobs:** S 3.1–3.5 s (up to 5.9 if one reader gets less than 0.19 GB/s, so 4 can't reach C). L on `t9pd-2000`: 66–70 s with h = 1, 79–83 s with h = 0.8, 131–135 s with h = 0, so 66–135 s (up to 248 with every input at its worst). On `t9pd-150`: 237–241 s with h = 1, 284–288 with h = 0.8, 473–477 with h = 0 | the larger of the source's read, dest's reads (2 − h passes) and the pool's CPU, plus 1–5 s of start, the last file's verification and the patched pages' `fsync`s |
| E8 | **v1 at 8 jobs against 4:** S −0.3 to +0.1 s; L on `t9pd-2000` −3 to +1 s | at 8 jobs the pool's CPU (57 s at L) drops below the disk's time (64.8 s); with h below 1, dest's disk binds both |
| E9 | **Where v1's re-read comes from:** `dest_read_share` 0.50 at S (within 0.02), and 0.50–1.00 at L (h 0–1). `leaf_nodes_reread` is 0 at S, and 0 at L unless the leaf lead exceeds about 34 GB, which isn't expected: its peak stays under 34 GB in every run | h; D33's cache holds a file's digests from its hashing to its `FILE_DONE` (64 MiB at 16 bytes per 8 KiB page: 34.4 GB of B) |
| E10 | **The ratios,** v1 at 4 jobs, central values (one reader at least 0.19 GB/s), for h of at least 0.8 and h = 0, under each reading of `pg_probackup`, with every input at its worst in brackets: the table below | E4–E7 |
| E11 | **The rows.** At S: Undecided or Out against the fastest (near 1.00), and faster than the released copiers. **At L on `t9pd-2000`: against the fastest, Out or a tie at the disk cap, with A12 holding in both workloads.** Against the released copiers no single row is expected; h and `pg_probackup`'s rate set it. With h at least 0.8: "faster" under reading (b) (`R_rel` 0.65–0.90), Undecided under (a) (0.78–1.22). With h of 0.5: "not faster" under (a), Undecided or "not faster" under (b) (0.96–1.11). With h = 0: "not faster" under both. Against one `pg_basebackup` stream v1 stays faster unless h is low and one reader is fast (`R_F0` 0.13–1.23). On `t9pd-150`: Out or a tie against the fastest (A12 holds); against the released copiers Undecided with h at least 0.8 (0.86–1.15), "not faster" with h = 0 (1.72–1.91) | E10 |
| E12 | **Write-back during the stream:** at L, dest's `Dirty + Writeback` peaks at 2.5–7 GB during every full copy on both sets; at S it reaches 2.0–2.2 GB and stays until the sync | Debian's thresholds |
| E13 | **The warm source** (L, `t9pd-2000`): `full` 25–60% faster than cold; `probackup4`, `probackup8` and `parcopy8` 0–15%; v1 0–10%. So the warm `R_F0` is 1.2–2.5 times the cold one, and the warm `R_fast` and `R_rel` within −0.1 and +0.2 of the cold ones, unless a warm `full` becomes B_rel's arm | a warm source spares one reader most; dest's disk and B, cold in every run, bind the others |
| E14 | **S against L:** `R_rel` rises by 0.15–1.8 from S to L on `t9pd-2000` | `pg_probackup`'s threads and its flush during the stream at L; v1's re-read may reach the disk |
| E15 | **Durability:** every durable arm passes (a) and (b) in every run on both sets, and the syscall audit at scale 5 on `t9pd-2000`, the new-copy arms included. `full_nosync` fails (a) by at least 1.5 GB at S and at least 1 GB at L | EXP-11; E12 |
| E16 | **CPU per GB:** v1's destination pool processes both passes at 1.3–1.7 GB/s at 4 jobs (EXP-11's 1.5, within its session drift) | EXP-11 |
| E17 | **The new-copy arms at S** (added after the review): `pgdelta_direct` (`--fsync main`) 1.5–3.5 s slower than v1 in place; `pgdelta_direct_wb` and `pgdelta_direct_worker` 0.3–2.0 s slower; those two within ±0.5 s of each other | a new copy writes all 2.12 GB through the disk's write cap (2.83 s at C). With write-back or a thread, those writes overlap the reads of S and B, since reads and writes are capped separately; in the main loop each file's flush waits its turn. EXP-10 on local NVMe: the thread beat write-back by 0.05–0.25 s |

**E10, expected ratios:**

| Cell | `R_fast` | `R_rel`, reading (a) | `R_rel`, reading (b) | `R_F0` |
|---|---|---|---|---|
| S, `t9pd-2000` | 0.79–1.13 (up to 1.9) | 0.21–0.50 (up to 0.84) | the same | 0.12–0.47 (up to 0.79) |
| L, `t9pd-2000`, h at least 0.8 | 0.97–1.28 | 0.78–1.22 | 0.65–0.90 | 0.13–0.75 |
| L, `t9pd-2000`, h = 0 | 1.93–2.08 | 1.54–1.99 | 1.28–1.47 | 0.26–1.23 |
| L, `t9pd-2000`, every input at its worst | up to 3.8 | up to 3.6 | up to 2.7 | up to 2.3 |
| L, `t9pd-150`, h at least 0.8 | 0.96–1.22 | 0.86–1.15 | the same | 0.46–1.15 |
| L, `t9pd-150`, h = 0 | 1.91–2.02 | 1.72–1.91 | the same | 0.91–1.91 |

Each range divides a v1 range by a baseline range end to end, so it is wider than the outcome is likely to be. On `t9pd-150` one reader can't fall below the rate that keeps 4 readers at C, so the worst case is the h = 0 row.

### Added rules

| ID | Condition | Consequence |
|---|---|---|
| A1 | The verdicts, at each set and size | As their tables say |
| A2 | **`full_syncfs`** (S): at least 0.3 s faster than `full` in both workloads | Reported: "at S on `t9pd-2000`, `full_syncfs` is x s faster than `full`", and `R_F0` against it beside. Changed after the review: it describes only. S fits in RAM, so no recommendation to Omni follows from it |
| A3 | **The bound's gate:** `parcopy8`'s pooled median over hot and append (20 runs at L on `t9pd-2000`, 10 elsewhere) is more than floor + max(1.0 s, 0.05 × floor), or its busiest stream carries more than 1.1 × the mean bytes | The bound is suspect, and its timers must explain the gap. Within reach is blocked. If `parcopy8` is still B_fast, it stays there for Out, the tie and A12, since a slow bound only favours v1. If a released arm is faster, the "Bound missing" row applies |
| A4 | **Verification,** as EXP-11's A4, with the void rule above. A run that times out (four times its expected upper bound, at least 120 s at S, 1,200 s at L on `t9pd-2000` and 2,400 s on `t9pd-150`) or whose own tool aborts is a tool failure | The arm is excluded from every baseline at that set and size |
| A5 | **Durability,** as EXP-11's A5: a durable run fails (a) or (b), or `parcopy8`'s `fsync` count is wrong | The run is excluded. More than 1 such run of an arm at one set and size in one trial reclassifies the arm as not durable there (rule 8). If v1 is reclassified, the row reads "v1 not durable". The reclassification binds this test's baselines; it enters the ledger only if both sessions give it |
| A6 | **The negative control:** `full_nosync` passes (a) in any run | Check (a) is void at that set and size; durability rests on (b) |
| A7 | **The size:** the smoke run finds B0 at L below 1.4 × dest's `MemTotal` | The scale is raised until it reaches 1.5 ×, before the sweep, recorded here, dated |
| A8 | **The disk's limits:** a trial's write floor or a 4-reader read floor is below 0.9 × C | Reported as "disk under its published limit", with the measured rate; the ratios are read against what the disk gave. Not a flag: every arm in the trial saw the same disk |
| A9 | **The link:** a trial's median one-stream test across the zones is below C (6.0 Gbit/s on `t9pd-2000`, 1.6 on `t9pd-150`) | The one-stream copies are labelled link-bound there, and their timers say whether the link or the reader set their pace. Reported |
| A10 | **Disk drift (extended after the review):** in a trial, the start and end values of any of the three floors (dest's write floor, the source's and dest's read floors) differ by more than 10%, or `parcopy8`'s first and last main-block repeats do | The trial's rows carry *disk drifted* |
| A11 | **The two sessions' shift:** for each arm and cell, the change of its median from session 1 to session 2, as a share of session 1's | Reported for every arm. A deciding ratio whose margin to its line is smaller than the larger shift of the two arms it compares is labelled "margin inside the shift" (K10). With agreeing rows it stays firm by K10's wording, and the write-up says so beside it |
| A12 | **The floor (added after the review; EXP-11's cut rule with this test's floor):** for each workload, the target 0.9 × B_fast against v1's floor (Probes), at each set and size | Target under the floor for hot and append: "Within reach is unreachable for v1's design at this size, on this disk and VM: no change to v1's CPU or verification (T5, T13) can reach the line, since v1 must read all of S and B once." Also reported: the target against the source's read floor alone. If it is under that too, only a design that doesn't read all of S (D12's summaries) could reach the line; a cached tree of B (D13) couldn't. Where the target is at or above the floor, the cut v1 − target is reported in seconds and as a share of v1; EXP-11's bands in seconds (0.6 and 1.2 s) were set at 2.1 GB and aren't applied |
| A13 | **Replicates (added after the review):** a session ran on another zone pair than `us-central1-a` to `-b`, or any of its VMs on another CPU platform than the other session's | Its rows are reported, and it is not a replicate of the other session under rule 11: no route follows from the pair. A session on the registered conditions is still owed, as an infrastructure fault (K1) |
| A14 | **D86 at S (added after the review):** per workload, `pgdelta_direct_worker` against `pgdelta_direct_wb` | `worker` faster by more than max(0.05 s, 2%) in both workloads: "D86's thread wins on a persistent disk at 2.1 GB". `writeback` faster by that margin in both: "D86 contradicted in part there". Otherwise "a tie: both switches kept". The new copy's price against in place (the fastest new-copy arm over v1, D85's price for O5's safe mode) is reported beside |

**No fix and rerun after the main sweep starts.** Fixes found in a smoke run are made before the sweep and recorded here, dated. Between the two sessions nothing changes in the harness, the design or the seeds, except for an infrastructure fault; any change is recorded under rule 8, and the rows it touches are not replicates.

## Rule 11: which outcomes may pick a route

Rule 11: only a firm decision picks a single route. Here a reading is firm when **both sessions give the same row, at L on `t9pd-2000` with v1 at 4 jobs, with no flag in either** (K10), and the sessions are replicates (A13). Two exceptions are written now: A12's reading is firm whatever the near-line flag, and a tie at the disk cap is reported as a tie, with no third session. A firm reading holds only for the conditions it was measured on: `n2-standard-8` on Cascade Lake, a 2,000 GiB `pd-balanced` disk (714 MiB/s), `us-central1-a` to `-b`, 48.5 GB of pgbench data, hot and append at 1%, a VM-sized page cache, Debian's write-back, an idle source read cold, and v1 and the bound reading a stopped source.

| Reading | May pick a single route when | The route it picks | What stays open whatever happens |
|---|---|---|---|
| The ledger's text at S (P1, P2) | never on its own (2.1 GB fits in RAM) | none; its consequence counts only where L agrees | – |
| **Within reach unreachable (A12)** | both sessions, for hot and append, with the bound measured (not excluded by A4 or A5); whatever the near-line flag | D03's "don't claim to beat a parallel full copy" is firm for v1's design on Omni's default disk, across zones, at this size. T5 and T13 stop being ranked for reaching HA's line against a parallel copier on this disk and VM; they keep their DR, source-CPU and correctness reasons. Both v1 and the bound read a stopped source, and backup mode cost about 0.05 s on an idle source in EXP-11, so an idle live source isn't expected to move it; T8 measures that | a larger VM (1,200 MiB/s, 16 vCPUs); T9 at 100–200 GB (rule 10); a busy source (T10b, T8); a design that reads less than all of S (D12) |
| **Out against the fastest copy** | both sessions, unflagged (`R_fast` ≥ 1.03 for hot or append in both) | A12's route, worded "Out against the fastest copy measured; `R_fast` = x" | as A12's |
| **Tie at the disk cap** | both sessions: A12 holds and `R_fast` < 1.03 for both workloads | A12's route, worded "v1 ties the fastest copy measured at the disk cap (`R_fast` x–y)". Reported as a tie; no third session | as A12's |
| **Undecided against the fastest** (A12 fails) | both sessions, unflagged | EXP-11's Out doesn't carry to Omni's conditions: the route "HA dropped against a parallel copier" closes. HA is a target beside DR, and T5 and T13 are ranked for it again | which of HA and DR comes first: no single target |
| **Not faster than the released copiers** | both sessions, unflagged, write-back- and source-cache-sensitive included | With A12's route: HA is closed as a speed target for v1's design at this size and on this disk. DR stays v1's first target, as T12r decides; this test measures no DR condition. HA's pitch is bytes across zones (about 0.01 USD per GB between zones), not time. Source slowdown joins that pitch only if T10b and T8 show v1 at its capped worker count no worse than `pg_basebackup` (the HA caveat says the opposite today, by proxy). A live v1 only adds work (backup mode, WAL, recovery), so this holds for a live source by dominance | T9 at scale; a larger VM |
| **Faster than the released copiers** | both sessions, unflagged, write-back- and source-cache-sensitive included. For Omni, also OMNI-2's reads: no memory limit on v1's side smaller than the cache v1's re-read used here, and Debian's `vm.dirty_*` on Omni's nodes | HA's positioning names its yardstick and its conditions (K6): "faster than `pg_basebackup` and `pg_probackup catchup` on a 2,000 GiB `pd-balanced` disk (714 MiB/s) on `n2-standard-8`, across zones, at 48.5 GB, with a VM-sized page cache and Debian's write-back, from a stopped source". The route "HA dropped altogether" closes for those conditions. For Omni it stays open, as a branch, until OMNI-2's reads | (1) a live or busy source (T8, T10b), which can only add time to v1; (2) a pod's or sidecar's memory limit, which shrinks the cache v1's re-read uses (OMNI-2); (3) `vm.dirty_*` on Omni's nodes: in EXP-11 one stream with write-back at 64 MiB beat v1 (OMNI-2; T8); (4) a smaller disk, where every method is disk-bound (the `t9pd-150` reading beside; T9); (5) a larger VM; (6) sizes beyond 48.5 GB, where a growing lead may push the re-read to the disk (T9) |
| **Undecided against the released copiers** | never | none | both: HA kept or dropped against the released tools |
| Within reach, Bound missing | never: out of reach by A12, or no bound | none | – |
| 8 jobs differs from 4 jobs | never | none | the worker count (D50, T10b) |
| `t9pd-150` differs from `t9pd-2000` | never: the 2,000 GiB row picks, and the difference is stated | none; every positioning names the disk size | how small Omni's disks are (an integrator question) |
| The sessions disagree, either is flagged, or they aren't replicates (A13) | never | none | both rows; a third session, on another day, is proposed to the owner. Not for a tie at the disk cap, nor where the sessions differ only between Out and the tie with A12 holding in both, since both pick A12's route |
| A2 (`full_syncfs` at S) | never: it describes S, which fits in RAM | none; no recommendation to Omni | – |
| A3, A4, A6, A8–A11 | never: they gate or label rows | none | – |
| A5's reclassification | binds this test's baselines in the session that gives it; enters the ledger only if both sessions give it | an arm "not durable on persistent disks" (rule 8) | – |
| A7 | before the sweep only | changes the design (the scale), recorded here, dated | – |
| A13 | never: it says which sessions are replicates | none | – |
| A14 (D86 at S) | both sessions, the same reading, unflagged | D86's switch on persistent disks at 2.1 GB: the thread kept as the new-copy mode's default, or write-back preferred there | beyond RAM (T9 at scale, with the new-copy arms); clones on XFS |
| What the measurements describe: E1's zones, E9's share of the re-read from disk and the leads (W2, D41p), D33's leaf re-reads, E12's write-back, the probes, D85's price | never: they describe, they don't choose | none | – |

## Notes written before running

- **Why the 2,000 GiB disk decides and 150 GiB sits beside it** (changed after the review). A disk's throughput is set by its size. Google sizes Omni's disk at 2,000 GiB for 1 TiB of data, the scale at which a rebuild takes hours. The database here is smaller than the disk is sized for, so that each run takes minutes; the disk's throughput is what a TB-scale standby would have. 150 GiB is near Omni's sizing for this test's database, and shows whether the rows hold when every method is bound by a slow disk.
- **Why the zones matter little and are still measured.** At 0.15–0.8 ms one default TCP stream isn't window-limited, and both disks cap below the link. So the zones are expected to change nothing but a few round trips. The pings and link tests check that; A9 labels a one-stream copy the link held back.
- **The expected outcome, written now** (revised after the review): P1 fails low; at S, Undecided or Out against the fastest, near 1.00, and faster than the released copiers. **At L on `t9pd-2000`: A12 holds, so within reach of the fastest copy is unreachable, read as Out or as a tie at the disk cap.** Against the released copiers no row is expected in advance: it turns on h, which nothing in pgdelta bounds, and on what limits `pg_probackup`, which EXP-11 didn't show (E11). One `pg_basebackup` stream stays the slowest copy unless the re-read reaches the disk. A warm source speeds that stream most, but is expected to leave both rows as they are. On `t9pd-150`: Out or a tie against the fastest; Undecided or "not faster" against the released copiers. 8 jobs gives the same rows on `t9pd-2000`.
- **Arithmetic that bounds it.** At L every method moves 48.5 GB through a disk capped at 0.749 GB/s, 64.8 s. v1's pool needs about 65 s of CPU for both passes at 4 jobs. So v1 and the bound meet near 1.0 if the re-read comes from the cache, and v1 takes about twice the bound's time if it doesn't.
- **What EXP-11 and EXP-13 taught, applied here:** counts come from the tools' and the kernel's own counters; no cause is stated without a timer; smoke checks read the records, not the log; a rule must be able to fire (the void rule is EXP-13's); every expected range shows its arithmetic, and the ranges judged are the ones written here.
- **"Instance-hours"** are one VM for one hour; "hours of the pair" are wall-clock hours, as FIRMNESS reads them.

## Changes from the ledger (rule 8)

Each becomes a ledger row with its own grade when this is registered.

1. **T9's persistent-disk arm runs on its own, now, as the audit's T9-PD,** under the owner's decisions K1, K6, K10 and K12. T9 at 100–200 GB stays later. Grade B.
2. **No local NVMe beside the persistent disks** ("beside the local NVMe" in the registered note). EXP-11's NVMe cells are the reference, and T12r, as FIRMNESS proposes it, replicates them on fresh pairs; local-SSD shapes have run out of stock before. Grade B.
3. **Source and dest in different zones,** where Omni's standbys sit. Grade B.
4. **A second size, 48.5 GB, and it decides.** The registered text is judged at 2.1 GB as written. The verdict that may pick a route is judged at 48.5 GB, the regime Omni's rebuild has, since 2.1 GB fits in RAM. Grade C: a value judgment about which size is relevant.
5. **"Undecided until T2 and T5" reads "until T5 and T13".** T2 is done, and EXP-11's Undecided row names T5 and T13 (FIRMNESS's T9-rule). Grade B.
6. **`full_syncfs` runs at S only.** The registered note asks for it beside `full`. At L both flush most of the copy during the stream; `R_wb` bounds what any flush method could still give, and A2 reports the difference at S. Grade B.
7. **Every verdict against two yardsticks** (K6), the fastest copy measured and the released tools, each with its own rows. Grade A (an owner decision).
8. **`pg_probackup`'s verification at L checks bytes first,** with EXP-11's logical digest wherever bytes differ. Grade B.
9. **A second disk size, 150 GiB of `pd-balanced`, on its own pair** (changed after the review; it replaces `pd-ssd`). FIRMNESS's T9-PD asked for two sizes, and a VM's limit counts all its disks of a kind. Grade B.
10. **10 runs per workload at the deciding cell** for v1 at 4 jobs, `parcopy8`, `full`, `probackup4` and `probackup8` (changed after the review, from the ledger's 5, as EXP-11's A9 and FIRMNESS's T9-PD); 5 elsewhere, the ledger's number, with `R_pool` and two sessions added. Grade B.
11. **Uniform changes aren't run.** Grade B.
12. **A12, EXP-11's cut rule with this test's floor, and a tie at the disk cap reported as a tie with no third session** (added after the review). Grade B.
13. **The new-copy mode at S** (D86, D85's price; added after the review). FIRMNESS put it in T9-PD. At L it stays open, and T9 at scale's ledger entry should carry the new-copy arms (proposed when this is registered). Grade B.

## Limitations, known before running

- **One VM shape, one CPU platform.** On an `n2-standard-8` the disk is capped at 800 MiB/s, and v1's pool has 8 vCPUs. A larger VM lifts the cap to 1,200 MiB/s and gives v1 more CPU; how the ratios move there isn't measured. The platform is Cascade Lake, checked (A13).
- **Two disk sizes,** 2,000 and 150 GiB, on different VM pairs, so disk size is confounded with the pair. Each is judged within its own set.
- **48.5 GB, not TB scale.** No ratio is projected to TB (rule 10). What changes beyond it (more segments, a longer tail, the prefetch's cap, a lead that may grow) is T9's.
- **A VM-sized page cache.** On GKE a pod's or sidecar's memory limit would bound the cache v1's re-read can use. **This favours v1.**
- **Stopped sources** for v1, the new-copy arms and `parcopy8`; the released arms read a running, idle one and pay backup mode. On an idle source that costs about 0.05 s. WAL streaming, recovery and a busy source are T8's and T10b's.
- **A cold source** in every main-block run. A live primary caches much of its data, which speeds one `pg_basebackup` stream most. **This favours v1 against `full`.** The warm block is five arms, 3 runs each, at L on `t9pd-2000`.
- **Freshly copied files.** Each L trial's source and every B are written by 8 concurrent per-file copies, so they are less fragmented than an aged database's, and laid out differently from files written over time. Every arm reads them.
- **Debian, not Container-Optimized OS.** Read-ahead, the I/O scheduler, page reclaim, `vm.dirty_*` and TCP settings are Debian's defaults; GKE's node image may differ (OMNI-2 reads them).
- **Transport is uneven,** as in EXP-11: `pg_probackup` over SSH, the others in clear. pgdelta will need TLS (O6; EXP-13 prices it).
- **Two zones, one pair of them.** Another zone pair may have another round trip; it is measured and recorded.
- **pgbench data** with local changes at 1%.
- **Fast checkpoints, and no deletion of an old copy on the clock,** favour the full copies, as in EXP-11.
- **Two sessions.** A third is run only if the two disagree, never for a tie at the disk cap, and only if the owner asks for it.

## Smoke run

Before the main sweep of each set, in each session, on its two new VMs. Each item says what it settles, the record it reads, and what happens if it fails. Results are recorded here, dated, in session 1; in session 2 they are recorded beside session 1's. Items 9 and 12 run on `t9pd-2000` only.

| # | Item | Record | Pass | If it fails |
|---|---|---|---|---|
| 1 | **Quotas,** read before the set is created (`create.sh`'s check): vCPUs in all regions, N2 vCPUs, instances, external addresses and SSD persistent disk in `us-central1` | the session's notes (read-only `gcloud` output, kept outside the repository) | room for 16 vCPUs, 2 instances, 2 addresses and 4,100 GB (`t9pd-2000`) or 400 GB (`t9pd-150`) of SSD disk, plus what runs beside | wait for what runs beside to end; or ask the owner |
| 2 | **Zones, disks, filesystems and platform:** each VM's zone and CPU platform (`gcloud`'s and `/proc/cpuinfo`'s); its data disk's kind and size from `gcloud` and from the VM (`blockdev --getsize64`), and the cap computed from it; ext4 in ordered mode with barriers; read-ahead, scheduler and `lru_gen` | `run.json`: `environment.disks`, `environment.zone`, `environment.cpu_platform` | as designed; Cascade Lake on both VMs | recreate the set, up to 3 times (A13); fix the infrastructure |
| 3 | **The disks' limits:** the write floor and the 4-reader read floors on both VMs at 8 GiB, and the single reader at 4 GiB | `run.json`: `disk_probes` | recorded; A8 read | – (A8 reports; the model takes the measured rates) |
| 4 | **The zones:** 20 pings; one stream each way and 8 streams, both host directions | trial record: `link_tests`, `pings` | recorded | – (A9 reports) |
| 5 | **Memory and size:** `MemTotal` and `MemAvailable` after a drop; the dirty thresholds; B0's bytes at L | `run.json`: `environment.memory`; trial record: `b0_bytes` | A7 doesn't fire | A7: raise the scale |
| 6 | **The L dataset:** build time; the pristine copy and its restore by 8 concurrent copies; segment counts of `pgbench_accounts` and `events` | trial record: `setup` | the restored cluster starts and its checksums verify | fix |
| 7 | **The sets:** `remote.py --pair NAME config` resolves the set file to the zones, machine and disk above; the two sets' VMs, host keys and results never mix; one set's teardown leaves the other | `run.json`: `environment.set`; the fetched directory `results/exp04/sets/NAME/` | yes | fix |
| 8 | **Durability:** checks (a) and (b) on the data disk at scale 5 and for `full` at L; the idle noise over 20 windows of 5 s, 6 of 30 s, 3 of 120 s and 2 of 600 s, which becomes the set's `NOISE` table in the run lines; `full_nosync` at L leaves at least 1 GB dirty | run records: `durability`; `run.json`: `dirty_idle_noise` | as EXP-11 | as EXP-11 |
| 9 | **The syscall audit** of every arm at scale 5, the new-copy arms included | `run.json`: `syscall_audit` | every durable arm 0 uncovered; `full_nosync` all | as EXP-11's smoke item 3 |
| 10 | **`pg_probackup` at L:** its threads (`ps`), the WAL ticker, `sync -f` inside the phase; the bytes-first check on one run; the logical fallback exercised on a forced difference (a unit test). **Its total and per-thread read rates** (`/proc/<pid>/task/<tid>/io`) on one run each of `probackup4` and `probackup8`, recorded before the sweep, with the model's E6 and E10 recomputed beside the written ranges (added after the review) | run records: `processes`, `phases`, `verification`, `probackup_io` | all present | fix the wrapper |
| 11 | **The cache-state records** on one v1 run at L: `dest_read_share`, `fincore` of B, the dirty sampler, the page cache's lists, the prefetch's bytes and cap, `leaf_nodes_reread`, **the lead's trace and its peaks** | run record: `cache_state`, `tool_reported` | all present, and the lead's last values equal B's bytes | fix |
| 12 | **The warm read at L:** cached bytes of the source when the clock starts | run record: `cache_state.source_fincore` | recorded (E3) | – |
| 13 | **Parallel digests and copies:** the start reference digest at L in at most 1.3 × floor; B's parallel copy equal to B0 (sizes, modes, a digest) | trial record: `reference_digests`, `setup` | yes | fix, or record the longer session |
| 14 | **The time projection:** one L repeat of every arm (hot), with `full_nosync`, and on `t9pd-2000` one warm repeat | run records: `started_utc`, `finished_utc` | – | the session runs whatever it projects (no cut line, K1); the projection is recorded |
| 15 | **`analysis/exp14.py` and `exp14_model.py`** end to end on the smoke CSV, writing to a scratch directory; the model's ranges on the measured inputs recorded beside the written ones | their output directory | runs | fix |
| 16 | **Reference digests:** differences reported by file class | trial records: `reference_digests` | any user-table difference listed | fix the analysis |
| 17 | **Teardown:** `teardown.sh` for the set; nothing of the set left | its exit status and its listing of the set's instances and disks in every zone | none | delete what is left |

## Run plan and cost

**Per-run times, bottom-up.** From EXP-11's records and the predictions above. At L on `t9pd-2000` (central, with the range):
- v1 (either job count): its 66–135 s, plus the parallel copy of B0 to B (65–72 s), the digests on both sides (65–72 s in parallel), the stop, the restart and the start: about 4.7 minutes (3.8–5.6).
- `parcopy8`: about 3.0 minutes (2.7–3.3). `pg_probackup`: about 3.3 (2.7–3.9).
- `full`: its 110–495 s, plus `pg_verifybackup` at one reader's rate (108–485 s; changed after the review) and the start: about 7.3 minutes with one reader at 0.25 GB/s (4.2–17.3).
- A trial's restore, B0, workload, digests and probes: about 22 minutes (17–32).

On `t9pd-150` every disk-bound step takes 3.6 times as long: v1 about 15 minutes (12–18), each copy 8.5–9.5 minutes (`full` up to 18), a trial's set-up about 47 minutes (42–58). At S a run takes 0.3–0.9 minutes, and a trial's set-up about 8.

| Step | Runs | Hours of the pair |
|---|---:|---:|
| `t9pd-2000`: read the quotas; create both VMs in two zones; provision (format with lazy initialisation off); check the platform; deploy | – | 0.6 (0.5–0.7) |
| Its smoke run (above), the L dataset's build (30–60 min) and the idle-noise windows (31 min) included | about 45 | 2.5 (2.0–3.4) |
| Invocation 1: S | 102 | 1.3 (0.9–1.9) |
| Invocation 2: L, with the warm block | 142 | 11.7 (8.8–18.0) |
| Fetch; teardown; check nothing of the set is left | – | 0.25 |
| `t9pd-150`: create, provision, check, deploy | – | 0.6 (0.5–0.7) |
| Its smoke run: the L dataset's build (45–90 min), probes, the idle-noise windows, one repeat | about 8 | 2.7 (2.3–3.4) |
| Invocation 3: L | 52 | 9.9 (9.4–12.3) |
| Fetch; teardown | – | 0.25 |
| **One session** | **296 in the sweep, about 350 with the smoke runs** | **about 30 (25–41)** |

- **VMs and vCPUs:** each set is 2 `n2-standard-8`, 16 vCPUs, 2 instances and 2 external addresses. One set at a time fits beside one EXP-13 pair (T12r) within the 32-vCPU quota, as FIRMNESS's waves 1 and 3 propose. Both sets at once take all 32 vCPUs and can't run beside T12r; the session then takes about 17 hours of wall time instead of 30. No local SSD is needed, so the local-SSD stockouts don't apply.
- **Instance-hours:** about 60 per session (50–82), **about 120 for both sessions (100–164)**. The first version's 60 for both priced one-reader steps at the disk's cap, ran `pd-ssd` in place of the 150 GiB disk, and ran 5 repeats at the deciding cell.
- **Disks:** per VM of `t9pd-2000` a 2,000 GiB data disk and the 50 GiB boot disk, 4,100 GB of the regional SSD quota for the pair; `t9pd-150` 400 GB.
- **Cross-zone traffic:** every full copy at L sends 48.5 GB from one zone to the other. Per session about 8.8 TB: invocation 2 about 5.2 TB, invocation 3 about 2.1, the smoke runs about 0.8, the link tests about 0.6, S about 0.1.
- **Money, at list prices** (to be checked when the quota is read): instances about 30 USD per session at FIRMNESS's 0.5 USD per instance-hour; disks about 10 (`pd-balanced` at 0.10 USD per GiB-month: 4,100 GiB for about 16 hours and 400 for about 14); cross-zone traffic about 88 (0.01 USD per GB). **About 130 USD per session, about 260 for both.**
- **Wall time:** each session is about 30 hours, run unattended on dest as before, so it spans two calendar days. Session 2 starts on another day, on fresh VMs, after session 1's sets are deleted. T12r's two sessions fit beside either set.
- **No cut line** (K1, K12). A session stops early only for an infrastructure fault; then the invocations that finished stand, and the rest run in a later session on fresh VMs, recorded.

**Set files** (committed with the harness, on top of `build/infra-pairs`; the environment still overrides each value, as in a zone fallback):

```sh
# infra/gcp/sets/t9pd-2000.sh: EXP-14 (T9-PD), the 2,000 GiB pd-balanced pair across two zones
ZONE=${ZONE:-us-central1-a}
SOURCE_ZONE=${SOURCE_ZONE:-us-central1-a}
DEST_ZONE=${DEST_ZONE:-us-central1-b}
MACHINE=${MACHINE:-n2-standard-8}
DATA_DISK=${DATA_DISK:-pd-balanced}
PD_SIZE_GB=${PD_SIZE_GB:-2000}
# infra/gcp/sets/t9pd-150.sh: the same lines, with PD_SIZE_GB=${PD_SIZE_GB:-150}
```

**Run lines** (the shape; options marked "new" are to be built):

```sh
S=s1                              # s2 in session 2
NOISE_2000=5:4,30:4,120:4,600:4   # smoke item 8 on t9pd-2000: dest's idle Dirty + Writeback noise, MiB, by window in s
NOISE_150=5:4,30:4,120:4,600:4    # smoke item 8 on t9pd-150
COMMON="--manifest-checksums CRC32C --speeds unthrottled --link-tests per-trial --pings-each-repeat 20 \
    --method-repeats full_nosync=1"                    # the last three options new (with --speeds)
M5=full,probackup4,probackup8,parcopy8,pgdelta_direct_inplace
NEW=pgdelta_direct,pgdelta_direct_wb,pgdelta_direct_worker                      # the last two new
(cd infra/gcp && PAIR=t9pd-2000 ./create.sh)
R="python3 experiments/network/remote.py --pair t9pd-2000"
$R start exp14-$S-2000-s -- --disk-kind pd-balanced --disk-gib 2000 --scale 70 \
    --patterns hot,append --order-seed 141 $COMMON --dirty-noise-table $NOISE_2000 --repeats 5 \
    --methods $M5,full_nosync,full_syncfs,pgdelta_direct_inplace_j8,$NEW          # --disk-* new
$R start exp14-$S-2000-l -- --disk-kind pd-balanced --disk-gib 2000 --scale 1600 \
    --pristine-dataset /data/pristine-s1600 --probackup-verify bytes-first \
    --patterns hot,append --order-seed 142 $COMMON --dirty-noise-table $NOISE_2000 --repeats 10 \
    --method-repeats pgdelta_direct_inplace_j8=1,2,3,4,5 --methods $M5,full_nosync,pgdelta_direct_inplace_j8 \
    --warm-source-repeats 3 --warm-source-methods $M5
$R fetch exp14-$S-2000-s && $R fetch exp14-$S-2000-l
(cd infra/gcp && PAIR=t9pd-2000 ./teardown.sh && PAIR=t9pd-150 ./create.sh)
R="python3 experiments/network/remote.py --pair t9pd-150"
$R start exp14-$S-150-l -- --disk-kind pd-balanced --disk-gib 150 --scale 1600 \
    --pristine-dataset /data/pristine-s1600 --probackup-verify bytes-first \
    --patterns hot,append --order-seed 143 $COMMON --dirty-noise-table $NOISE_150 --repeats 5 \
    --methods $M5,full_nosync
$R fetch exp14-$S-150-l
(cd infra/gcp && PAIR=t9pd-150 ./teardown.sh)
```

Each `start` runs after the one before has finished (`remote.py wait`), and each smoke run after its set is created, as before. Results land in `results/exp04/sets/t9pd-2000/` and `results/exp04/sets/t9pd-150/`, which the analysis reads. The same lines and seeds run in session 2. `NOISE` is 4 MiB at every length until smoke item 8 sets it, as in EXP-13.

## What the harness must add

**Reused from EXP-11 to EXP-13 and `build/infra-pairs` as they are:** every arm but two new-copy entries (`full`, `full_syncfs`, `full_nosync`, `probackup4` and `probackup8` with the WAL ticker and `sync -f`, `parcopy8`, v1 at 4 and 8 jobs, `pgdelta_direct`); checks (a) and (b), the syscall audit, the negative control; the probes; the warm-source block; the reference digests and EXP-11's verification; the idle-noise plan and the noise table by window length, the void rule by file class and the ping check (EXP-13); `fincore` (EXP-12's packages); VM sets with per-role zones, a `pd-balanced` data disk, the quota check, per-set host keys and SSH config, `remote.py --pair`, and teardown in every zone with its check for leftovers (`build/infra-pairs`). `analysis/exp04.crossover` isn't needed (one speed).

**To add (not built), about 3 days:**

| Block | File | What |
|---|---|---|
| Infrastructure (about 0.25 day; changed after the review) | `infra/gcp/sets/t9pd-2000.sh`, `infra/gcp/sets/t9pd-150.sh`; `tests/test_infra_sets.py` | The two set files above. Nothing else in `infra/gcp/` changes: each VM gets one `pd-balanced` disk at `pgd-data`, which `startup.sh` formats and mounts as `/data`. Tests: each set file through a dry run of `create.sh` (both zones, one 2,000 or 150 GB `pd-balanced` data disk per VM with the set's labels and auto-delete, the quota need of 16 vCPUs and 4,100 or 400 GB) and of `teardown.sh` (both zones) |
| pgdelta (about 0.25 day; added after the review) | `pgdelta/receiver.py`, `tests/` | Statistics only: the lead's trace (at every basis file hashed, every `FILE_DONE` and every file verified: the time and B's bytes hashed, done and verified) and its peaks, in the receiver's statistics. A test that outputs, other statistics and the protocol are unchanged |
| Orchestrator (about 1.5 days) | `experiments/network/run_exp04.py`, `measure.py`, `agent.py` | `--disk-kind` and `--disk-gib`, checked by the preflight against the device behind `/data` and its size; `/proc/diskstats` of that device in place of the md array (check (b), the disk counters on both VMs); `run.json`: zones, the set, the CPU platform, disks (kind, size, device, read-ahead, scheduler, `lru_gen`), the published caps computed, memory after a drop; `--pristine-dataset` (built once, restored by 8 concurrent per-file copies per trial); B copied from B0 the same way; `--link-tests per-trial`, `--pings-each-repeat` and `--method-repeats` with `--speeds`; both read floors at each trial's start and end; the cache-state record per run (`MemAvailable`, `Active(file)` and `Inactive(file)` after the drop; a 0.5 s sampler of `Dirty`, `Writeback`, the page cache's lists, `workingset_refault_file`, `pgactivate` and `pgdeactivate`, held in memory until the clock stops; `fincore` of B after the clock stops; `fincore` of the source before a warm run; `dest_read_share`; the leads resampled at 0.5 s); `pg_probackup`'s per-process and per-thread read bytes every 0.5 s; pgdelta's leaf, prefetch and lead figures into the CSV; the single-reader probe on the first 4 GiB of a relation; parallel per-file SHA-256 in `datadir-digest`, `tree_digest` and `files_digest` (4 workers, the same digests); `--probackup-verify bytes-first` with the logical fallback and the source's logical digest at the trial's end only when a run is pending; `pgdelta_direct_wb` and `pgdelta_direct_worker` in `PGDELTA_ENGINES`, with `pgdelta_direct` pinned to `--window 4`; run timeouts scaled with the size and disk (A4); EXP-01's ground truth skipped at L |
| Analysis (about 0.75 day) | `analysis/exp14.py`, `analysis/exp14_model.py` (new) | envelopes per set and size, chosen on pooled main-block runs; both verdicts (K6) with their rows, the tie and every flag; A12's target and floor; `R_adj`, `R_max`, `R_noSSH`, `R_paired`, `R_pool`, `R_wb` and the warm ratios; P1 and P2 at S; the 8-job, `t9pd-150` and S readings beside; A2–A14; **a two-session reader** that prints each row per session, whether they agree, whether they are replicates (A13), the shift of every arm between sessions (A11), and which routes the rule 11 table allows; the cache-state, lead, write-back, disk and link tables; figures `exp14-times.svg` (each arm's time by set and size) and `exp14-ratios.svg` (both yardsticks, both sessions); `exp14-runs.csv`, `exp14-cells.csv`; every set's results read from `results/exp04/sets/NAME/`. `exp14_model.py`: the derivation of E2–E10 and E17 as code, whose inputs are C, r1, `pg_probackup`'s two readings, the link, the pool's rates and h, and h derived from the measured lead and lists beside h measured |
| Tests (about 0.25 day) | `tests/test_exp14.py` | the preflight refusing the wrong disk or size; the diskstats device chosen by kind; the pristine restore and B's parallel copy equal to `cp -a`'s (sizes, modes, digests); the bytes-first rule and its fallback; the parallel digests equal to the serial ones; the verdict rows on synthetic medians, every row and flag reachable, A12 and the tie, both yardsticks; the two-session reader on synthetic sessions that agree, disagree, are flagged, tie, or differ in zones or platform (A13); the model's ranges, from the inputs written here, inside the ranges written here |

Docs: a "Methods for T9-PD" section in `experiments/network/README.md`, and this write-up with a row in the experiments index.

## After the review (October 24, 2026)

An independent review of the first version found five major and seven minor problems. Each was checked against what it cites: the code (`pgdelta/receiver.py`, `pgdelta/sender.py`), `build/infra-pairs` (428bd6c), EXP-11's write-up and cells, FIRMNESS's T9-PD, and Google's disk-performance and CPU-platform pages. All twelve hold. What changed:

1. **v1 and the bound share the disk cap at L, so a tie is the likeliest outcome against the fastest copy, and Undecided's consequence couldn't follow** (major; accepted). Checked: v1 reads all of S and B, so it takes at least D/C, 64.8 s; a bound that passes its gate takes at most 68.0 s; so `R_fast` is at least 0.95. Added A12, EXP-11's cut rule with this test's measured floor, and the "tie at the disk cap" row. "Within reach is unreachable" is firm when both sessions' floors give it, whatever the near-line flag. A tie in both sessions is reported as a tie, with no third session. Undecided now needs A12 to fail, so its consequence (T5 and T13 ranked for HA again) can follow.
2. **The basis thread's lead isn't bounded, so h isn't known** (major; accepted, with two additions). Checked: `_hash_basis` hashes B file after file with nothing tying it to the main loop; the sender's inbox takes every `BASIS_NODES` frame; the 4-file window bounds the source's hashing. Leaf digests are released at `FILE_DONE`, so D33's cap binds only beyond a lead of 34.4 GB. Changed: a statistics-only pgdelta counter records the lead, resampled at 0.5 s; h's written range at L is 0–1; E7, E9, E10 and E11 are recomputed; "crosses D33's cap" is dropped as a reason for the size; "Why not larger" is rewritten. Added beyond the review: verification is queued and can fall behind `FILE_DONE`, so the cache lead is measured to the verifier's position, not to `FILE_DONE`; and since Linux 4.10 the inactive list, where pages read once wait, may shrink to about 1/18 of the cache here, so the page cache's lists are sampled too.
3. **"Faster than the released copiers" left out conditions that favour v1** (major; accepted in its first form). The six conditions that stay open are listed (the five named, plus a live or busy source); the route text names the disk, VM, cache and write-back; for Omni the row stays provisional until OMNI-2 reads memory limits and `vm.dirty_*`. `R_wb` now also removes `pg_probackup`'s final flushes. The memory-cgroup arm wasn't added: Omni's limit isn't known, a guessed one would add a reading no route could use, and E10 already shows that h = 0 reverses the row (Considered and not run).
4. **"Not faster" picked DR and claimed source slowdown** (major; accepted). The consequence now reads "HA closed as a speed target for v1's design at this size and on this disk; DR stays v1's first target, as T12r decides". Source slowdown joins HA's pitch only if T10b and T8 show v1 at its capped worker count no worse than `pg_basebackup`.
5. **The infrastructure didn't match `build/infra-pairs`** (major; accepted in a simpler form). Checked: one `pd-balanced` disk per role at `pgd-data`; `DATA_DISK` only `local-nvme` or `pd-balanced`; the quota check counts one data disk per role; no `mount-data` or `copy-pristine`; no `--pair` on the run lines. With `pd-ssd` dropped (item 11), no VM needs a second disk or another kind. So two committed set files, `--pair` on every `remote.py` line and the analysis reading `results/exp04/sets/NAME/` are all that remains; the infrastructure block is 0.25 day of about 3.
6. **One-reader steps priced at the disk cap** (minor; accepted). B and the pristine restore use 8 concurrent per-file copies, then `sync -f`, untimed. `pg_verifybackup` is priced at one reader's rate. A session is now about 30 hours of the pair (25–41) and about 60 instance-hours (50–82), with every other change in it; it still fits beside T12r, one set at a time.
7. **5 runs at the deciding cell** (minor; accepted). 10 per workload for the five deciding arms at L on `t9pd-2000`, about 3 hours more per session.
8. **A10 missed read drift; burst and the platform** (minor; accepted, with the platform handled differently). A10 now covers the write floor and both VMs' read floors at each trial's start and end, and `parcopy8`'s first and last repeats. Google's page lists no burst for `pd-balanced` (read again October 24). The platform can't be pinned: `--min-cpu-platform "Intel Cascade Lake"` admits Ice Lake, N2's newer platform. So it is checked, a set on another platform is created again up to 3 times, and a session on another platform is not a replicate (A13).
9. **Zone fallback and the warm block's released arms** (minor; accepted). A session on another zone pair is not a replicate (A13). `probackup4` runs in the warm block.
10. **`pg_probackup`'s threads may not add up** (minor; accepted). E6 and E10 give both readings with their numbers (EXP-11's copy phase 3.8–4.0 s unthrottled, 1.75–1.92 s above link time at 2 and 5 Gbit/s). Under reading (b) "faster" becomes the expected released row if h stays at 0.8 or more. Smoke item 10 measures the total and per-thread read rates at L before the sweep.
11. **`pd-ssd` is a contrast expected to change nothing** (minor; accepted: replaced). Checked: 800 against 714 MiB/s, the same 15,000 IOPS, and a VM's limit is computed over all its disks of a kind. The second reading is a 150 GiB `pd-balanced` disk on its own pair (196 MiB/s). It costs about 9 hours more per session than `pd-ssd` did.
12. **No queued test firmed D86** (minor; accepted). The new-copy mode runs at S on `t9pd-2000` with each `--fsync` switch, 5 runs per workload, in both sessions (A14, E17). T9 at scale's ledger entry should carry the new-copy arms (Changes, item 13).

**Rejected, with reasons:** the parts of item 5 that built `pd-ssd` and two disks per VM (a second data disk in `config.sh`, `vmset.sh`, `create.sh` and `startup.sh`, an attach-later path, `mount-data`, `copy-pristine`), since item 11 removed the need; pinning the platform with `--min-cpu-platform`, since it can't pin Cascade Lake; and the memory-cgroup arm in item 3, for the reasons above.
