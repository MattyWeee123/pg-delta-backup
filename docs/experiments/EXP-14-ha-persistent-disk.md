# EXP-14: Does EXP-11's HA verdict hold on Omni's disk, across zones, beyond the page cache? (T9-PD)

Status: **pre-registered October 24, 2026 (night); not yet reviewed; the harness isn't built; not run.** This is the persistent-disk arm of test T9 in the [decision ledger](../DECISIONS.md#t9-scale-beyond-ram), moved up and widened as the audit's T9-PD ([FIRMNESS.md](../FIRMNESS.md#41-the-tests), section 4.1), under the owner's decisions of October 24 (night): K1 and K12 (size each experiment to what firm results need, two sessions per test on fresh VMs and different days, no budget cut lines), K6 (every HA verdict judged separately against the fastest copy measured and against the released tools Omni can run) and K10 (a single session whose deciding margin is inside the shift between sessions isn't firm). As for EXP-11 to EXP-13, an independent review and a check of the revision come before the harness is built, and every change they lead to will be listed here, dated. The ledger's registered text for this arm is quoted word for word and judged as registered. Everything marked "added here" is new. Where the design departs from the ledger, the reason is under [Changes from the ledger](#changes-from-the-ledger-rule-8), as rule 8 asks.

## Question

[EXP-11](EXP-11-full-copy-baseline.md) found v1's path **Out on fast links**: unthrottled, a range-parallel copier built as a bound (`parcopy8`) made a durable copy in 1.88 s, and v1's path at 4 jobs took 1.64–1.65 times as long for local changes. Beside it stood the qualifier: v1 was faster than the released copiers tested, at 0.77–0.78 of one `pg_basebackup` stream with syncfs and about half of `pg_probackup catchup`. Those numbers hold on one VM pair in one zone (0.1 ms round-trip time), on local NVMe that writes about 1.6 GB/s, for 2.1 GB that fits in RAM, with v1 and the bound reading a stopped, idle source.

Omni's HA rebuild runs on other conditions ([Omni notes](../research/notes/alloydb-omni.md)):
- **GKE's default disk is a balanced persistent disk** (`pd-balanced`, ext4). Its throughput is set by its size and capped per VM. On an `n2-standard-8` it can't exceed 800 MiB/s each way ([Google's disk performance page](https://docs.cloud.google.com/compute/docs/disks/performance)), half of what EXP-11's local NVMe wrote.
- **The standby sits in another zone** of the region, since in-region standbys replicate synchronously across zones.
- **Real databases don't fit in the page cache.** At 2.1 GB, v1's verification re-read came from memory, and every full copy's flush landed at its end, below Debian's write-back thresholds.

Each of these can move the verdict either way. A capped disk slows every method that moves the whole database through it, which pulls the ratio toward 1.0. A copy beyond the page cache may turn v1's re-read into a second disk pass, which pushes it toward 2.0. And one sequential reader on a network disk may be slow, which hurts one `pg_basebackup` stream most.

On one pair of VMs in two zones, in each of two sessions:
1. **Against the fastest copy measured** (rule 3's yardstick, the bound): does "Out on fast links" hold?
2. **Against the released tools Omni can run** (one `pg_basebackup` stream, `pg_probackup catchup`): does "faster than the released copiers" hold?
3. **Does it depend on the disk class** (`pd-ssd` beside `pd-balanced`), on the database's size (2.1 GB against 48 GB), or on v1's worker count (4 against 8 jobs)?
4. **What do the zones give:** their round-trip time, and what one TCP stream and eight carry between them?

It decides D03's HA positioning on Omni's conditions, against each yardstick (K6), and the ledger's registered rule for T9's persistent-disk arm. It moves D72 (the platform), the HA qualifier, W2 (the destination's two passes as v1's bottleneck) and D41p (the re-read beyond RAM), and gives readings for D50 (8 jobs against 4) and D33 (B's page digests beyond 64 MiB). D86 and D85's price, which FIRMNESS also put in T9-PD, stay open (see [Considered and not run](#considered-and-not-run)).

**What would change the plan** (the full rules are under [The HA verdicts](#the-ha-verdicts-added-here) and [Rule 11](#rule-11-which-outcomes-may-pick-a-route)). Every row needs both sessions to agree, at 48 GB on `pd-balanced`, with v1 at 4 jobs and no flag:

| If | Then |
|---|---|
| Out against the fastest copy, and not faster than the released copiers (`R_rel` ≥ 1.00 for hot or append) | HA is closed as a speed target for v1's design on Omni's default disk at this size. D03 is restated against both yardsticks. HA's pitch is bytes across zones and source slowdown, not time. DR is the single target |
| Out against the fastest copy, and faster than the released copiers (`R_rel` ≤ 0.90 for both) | EXP-11's verdict holds on Omni's conditions. The HA positioning names the released yardstick, for a stopped source until T8 |
| Out against the fastest copy, and Undecided against the released copiers **(expected)** | HA against the released tools stays open, as a branch. T9 at 100–200 GB and T8 decide it |
| Undecided against the fastest copy | EXP-11's Out doesn't carry to Omni's conditions. HA re-enters beside DR, and T5 and T13 are ranked for it again |
| The two sessions disagree, or a row carries a flag | Provisional: both readings are kept, and a third session is proposed to the owner |

## Setup

### Machines, disks and zones

- **Two `n2-standard-8` VMs** (8 vCPUs, 32 GB of memory each), labelled `purpose=pg-delta-benchmark`, with no local SSD. Debian 12, PostgreSQL 17 from PGDG, `pg_probackup` 2.5.16 on both, strace, as EXP-11. Every version goes into `run.json`.
- **Zones (added here):** the source in `us-central1-a`, EXP-09's and EXP-11's zone; dest in `us-central1-b`. Both sessions use the same two zones. If a zone can't supply the shape, the next pair in the order (a, c), (b, c), (a, f) is used. That is an infrastructure fault, recorded and flagged; the zones are a factor in every row.
- **Two data disks on each VM (added here),** created with the VM, labelled like it, and deleted with it (auto-delete):
  - `pd-balanced`, 2,000 GiB: Google's sizing for 1 TiB of data on Omni, and GKE's default storage class.
  - `pd-ssd`, 2,000 GiB: the same size in GKE's faster class.
- **Filesystem:** ext4 on the whole disk, `startup.sh`'s options (`mkfs.ext4` defaults with lazy initialisation off, so no background zeroing runs during the runs; mounted `noatime`; journal in ordered mode). The runs refuse to start unless `/data` is ext4 in ordered mode with barriers on the expected disk (EXP-11's preflight, extended). Debian's read-ahead and I/O scheduler stay at their defaults, recorded per disk.
- **One disk kind at a time.** The disk under test is mounted at `/data` on both VMs and the other kind is unmounted. Between invocations both VMs swap. The results directory moves to the boot disk, so it survives the swap.
- **pgdelta at EXP-11's code.** `DEPLOYED.json` records the diff of `pgdelta/` from EXP-11's deployed commit.
- **Quotas, read before the VMs are created:** the project's 32 vCPUs across all regions (this design uses 16); 24 instances and 8 external addresses in `us-central1` (it uses 2 and 2); and the regional quota for SSD persistent disks, which `pd-balanced` and `pd-ssd` both count against. This design needs 8,100 GB of it, plus what runs beside it. If it doesn't fit, the `pd-ssd` disks are created, labelled and attached with auto-delete after the `pd-balanced` ones are deleted, between invocations 2 and 3, and the large dataset is built again on them (recorded). If even one pair of 2,000 GiB `pd-balanced` disks doesn't fit, nothing runs and the owner is asked: raising a quota is the owner's call.

**The published limits** that apply ([disk performance](https://docs.cloud.google.com/compute/docs/disks/performance), read October 24):

| | `pd-balanced` | `pd-ssd` |
|---|---|---|
| Throughput per GiB, plus a baseline per VM | 0.28 MiB/s, plus 140 MiB/s | 0.48 MiB/s, plus 240 MiB/s |
| N2 VM with 8–15 vCPUs: most it can read, and write | 800 and 800 MiB/s | 800 and 800 MiB/s |
| N2 VM with 8–15 vCPUs: IOPS, reads and writes shared | 15,000 | 15,000 |
| **This design's cap, each way** | **714 MiB/s = 0.749 GB/s**: 140 + 0.28 × 2,050 GiB, since the 50 GiB boot disk is `pd-balanced` too and the formula adds every disk of a kind on the VM | **800 MiB/s = 0.839 GB/s**: 240 + 0.48 × 2,000 = 1,200, capped by the VM |

Read and write throughput limits are independent for both kinds; IOPS are shared. So on this VM the two classes differ by about 12% in throughput and not at all in IOPS. A larger VM would lift the cap to 1,200 MiB/s; that is a limitation, not an arm.

### Data, sizes and trials

- pgbench with EXP-01's `events` table (as many rows as `pgbench_accounts`) and EXP-01's seed; **hot and append** at 1%, the two workloads EXP-09's HA rule names. Each workload is its own trial, with its own source cluster and B0, as in EXP-09 to EXP-11.
- **Manifest checksums (rule 7):** CRC32C for every `pg_basebackup`, B0 included, with EXP-11's guard.
- **Two sizes:**

| Size | pgbench scale | Database (a full copy's bytes) | Role |
|---|---:|---:|---|
| **S** | 70 | 2.12 GB | the ledger's registered size for this arm; the bridge to EXP-11 |
| **L (added here)** | 1,600 | about 48.5 GB (45 GiB) | **the deciding size**: Omni's regime |

**Why 48.5 GB.** It is the smallest size, with a margin, at which nothing in a run fits in memory the way it did at 2.1 GB:
- **It exceeds dest's page cache by half.** An `n2-standard-8` has about 31 GiB of `MemTotal`, and after a cache drop the page cache can take about 30 GiB. B (48.5 GB) is about 1.45 times `MemTotal`. So no v1 run can hold both of its passes over B in memory, and no full copy's output stays cached whole.
- **Every full copy crosses Debian's write-back thresholds early.** `vm.dirty_background_ratio` is 10 and `vm.dirty_ratio` 20, of about 31 GiB: about 3 and 6 GB. At 2.1 GB, every full copy stayed below both, so its whole flush landed at its end. At 48.5 GB the kernel writes back during the stream, as it would at TB scale.
- **It crosses D33's cap.** The receiver keeps at most 64 MiB of B's page digests (`LEAF_CACHE_BYTES`), which covers about 33 GB of B. Beyond it, a leaf request makes the receiver re-read B's nodes, as at any TB size. EXP-11 never reached it.
- **The tables split into 1 GiB segments.** `pgbench_accounts` and `events` become about 20 segment files each. So per-file parallelism (`pg_probackup`) is no longer capped by two large files, the limitation EXP-11 said favoured v1.
- **Why not larger.** On these disks every pass over 48.5 GB takes about 65 s, and a v1 run with its checks about 4 minutes. Each doubling doubles that, without crossing another threshold on this VM shape. T9 at 100–200 GB stays later, and no ratio here is projected to TB scale (rule 10).
- **How much of each pass reaches the disk isn't assumed.** It is measured in every run (below). B being larger than the cache guarantees only that not all of it can stay there.

**The dataset at L is built once per session.** The scale-1,600 cluster is built on the source's `pd-balanced` disk in the smoke run, stopped cleanly, and kept as a pristine copy beside it. Each L trial restores it with `cp -a` (untimed), starts it, takes B0, and runs its workload. At the disk swap the pristine copy is copied to `pd-ssd`. So every L trial's source cluster is a fresh file copy of the same data. S trials build their cluster as EXP-11 did.

### Blocks and order

| Invocation | Disk | Size | Trials, in order | Main block | Other blocks |
|---|---|---|---|---|---|
| 1 | `pd-balanced` | S | hot, append | 5 repeats | – |
| 2 | `pd-balanced` | L | hot, append | 5 repeats | the warm-source block: 3 repeats |
| 3 | `pd-ssd` | L | hot, append | 5 repeats | – |

- Within each (trial, repeat), every arm runs once, in a seeded random order. `full_nosync` runs in each trial's first repeat only.
- The same seeds and order in both sessions, so the replicate differs in time and VMs only (FIRMNESS's revision 5 for EXP-13). The disk kinds always run in this order, so disk kind is confounded with time in the session. The deciding ratios compare arms within a repeat, which the order doesn't touch.
- **The warm-source block** (invocation 2 only): 3 repeats of `parcopy8`, `full`, `probackup8` and v1 at 4 jobs, with the source's data directory (without `pg_wal`) read once, untimed, before each run, instead of the drop. At L that leaves the last 25–31 GB read in the source's cache, what a live primary holding most of its working set might have.

**Order within a trial:**
1. The source cluster (built at S; restored from the pristine copy at L), started. B0 by `pg_basebackup` (CRC32C), synced. The workload and its `CHECKPOINT`.
2. The start reference digest: a clean stop, `agent.py datadir-digest`, a restart.
3. Probes: the durable write floor (dest), the cold read floor and the single reader (source), the bracket; 20 pings and the link tests (one stream both ways, 8 streams from source to dest).
4. The main block. Each repeat starts with 20 pings.
5. At L on `pd-balanced`: the warm-source block.
6. The durable write floor again; the link tests again.
7. The end reference digest.
8. The source's logical digest: always at S; at L only if a `probackup*` run is pending (see Verification).
9. B0's full digest.
10. EXP-01's row-level ground truth: at S only (it is EXP-01's study, and costs minutes at L).

Between steps 2 and 7 no SQL reads a user table on the source, as in EXP-11.

### Cache state at the start of every run

As EXP-11's table, with these additions:
- **Both VMs:** `sync; echo 3 > drop_caches` after the untimed set-up, except the source in the warm-source block. `MemAvailable` and `Cached` are recorded just after the drop.
- **v1's old copy B:** `cp -a --reflink=never B0 B; sync -f B` before the drop, as EXP-09 and EXP-11: cold and already durable. At L the copy takes about 65 s on the disk under test.
- **Warm source (L):** after the untimed read, `fincore` records how much of the source's data directory is cached when the clock starts.
- **No harness write to the disk under test inside a run's window** except the method's own. Records are written after the clock stops, to the boot disk.

### What the clock includes, and durability

**As EXP-11, unchanged:** one timed phase with one counter window per run; the clock is `perf_counter` on dest. The same components are in and out: backup mode and a fast checkpoint in for the live-source arms, out for v1 and `parcopy8`, which read a cleanly stopped source; `pg_probackup`'s timed `sync -f` in. A run is durable by EXP-11's definition: every byte the method wrote covered by a successful `fsync`, `fdatasync` or `syncfs` after its last write, and every directory change covered by EXP-11's directory rule on ext4 in ordered mode, before the clock stops.

**Checked as in EXP-11,** with the persistent disk in place of the md array:
- **(a)** `Dirty + Writeback` on dest rises by at most max(4 MiB, the idle noise for a window as long as the run) over the run. The idle noise is measured in the smoke run over windows of 5, 30, 120 and 600 s, since L's runs last minutes (EXP-13's noise table).
- **(b)** For every full-copy arm, the bytes written to dest's data disk during the window (`/proc/diskstats`, that device) are at least 0.98 × the output's bytes.
- **The syscall audit** (EXP-11's, `strace` on dest), one untimed run of every arm at scale 5 on `pd-balanced`, in the smoke run.
- **The negative control:** `full_nosync` must fail (a) in every run. At L, write-back during the stream leaves only about the background threshold dirty, so its margin is smaller: at least 1 GB (E15).

These checks catch a tool that skips its flush. They don't prove durability: no power is cut. A persistent disk acknowledges a write once it is stored durably, so its cache flushes cost a round trip, which every arm pays per `fsync`.

### Verification (untimed, every run)

As EXP-11's table, with one change at L:

| Arm | Contents | Started? |
|---|---|---|
| `full`, `full_syncfs`, `full_nosync` | `pg_verifybackup` with the CRC32C manifest | yes: recovers from `backup_label`, answers a query, stopped |
| `probackup*` at S | EXP-11's: the per-table logical digest of the started output, against the source's at the trial's end; the byte diagnostic in each trial's first repeat | yes, with no WAL fetched after the clock stops |
| **`probackup*` at L (changed here)** | **Bytes first.** Every file of the benchmark database's user relations (heap, indexes, TOAST, free-space and visibility maps) must be byte-identical (SHA-256) to the trial's start reference digest. If any differs, that run's per-table logical digest is taken and compared with the source's at the trial's end, as EXP-11 did. Other files that differ are reported by class (catalogs, other), as EXP-13 does | as at S |
| `parcopy8`, v1 at 4 and 8 jobs | EXP-11's: byte-identical to the stopped source's selection (`verify_direct`), no in-place leftovers, the `fsync` count for `parcopy8` | yes |
| every arm | B0 unchanged: a stat snapshot per run, a full digest per trial | – |

- **Why the change.** A logical digest reads every row: about 28 s per 2.1 GB in EXP-11, so about 11 minutes per `probackup*` run at L, about 7 hours per session. Byte identity of the user tables' files is a stronger check of the same rows. The logical digest stays the standard wherever bytes differ.
- **The void rule is EXP-13's:** a `probackup*` run is void, not failed, only if the trial's start and end reference digests differ in a user-table file. EXP-11's reference digests differed in system catalogs in every trial, which made its A4's logical leg impossible; EXP-13 fixed that, and this keeps the fix.
- At L the digests on both sides hash files in parallel (4 workers), so each takes about one pass at the disk's rate instead of being bound by one core's SHA-256. What they check is unchanged.

### Measured

**In every run:** EXP-11's columns (`total_s`, `total_no_ssh_s`, wire bytes, VM-wide CPU on both VMs, per-process rusage and peak RSS, dest's disk bytes, `Dirty` and `Writeback` before and after, each arm's own phase timers), and, added here:
- **Disk bytes read and written in the window on both VMs' data disks.** For v1, `dest_read_share`: dest's disk reads over twice B's bytes. 0.5 means the verification re-read came wholly from the cache, 1.0 wholly from the disk. For the full copies, the source's disk reads over the output's bytes.
- **A dirty-page sampler on dest,** every 0.5 s: the peak of `Dirty + Writeback` during the run, and its value when the stream ends.
- **The share of B cached when the clock stops** (`fincore`, before verification), for v1.
- **pgdelta's own statistics** that bear on size: the prefetch's bytes and its cap, `leaf_nodes_reread` and `leaf_nodes_cached` (D33), and the receiver's hashing, verification and wait timers.
- **The pings** before each repeat (20, 0.05 s apart, as root): minimum and median.

**Once per trial, untimed** (EXP-11's probes, sized for L):

| Probe | What | Expected |
|---|---|---|
| durable write floor (dest) | 4 writers write as many bytes as B0 holds, buffered, write-back every 8 MiB, then `fsync` each; at the trial's start and end | 0.90–1.00 of the disk's cap: S 2.83–3.15 s and L 64.7–72 s on `pd-balanced`; L 57.8–64 s on `pd-ssd` |
| cold read floor (source) | 4 readers of the data directory without `pg_wal` | the same bands as the write floor |
| one reader (source; changed here) | one reader of the first 4 GiB of `pgbench_accounts` (S: its whole file), after a drop. This is the rate one `pg_basebackup` stream can read at | 0.10–0.45 GB/s on `pd-balanced`, 0.12–0.55 on `pd-ssd` (E2) |
| link across the zones | 20 pings; one stream each way, and 8 streams from source to dest, 6 s each with a 3 s warm-up; at the trial's start and end | E1 |
| backup-mode bracket | EXP-11's | median 0.03–0.20 s |

**floor(size)** is max(dest's durable write floor, the source's cold read floor, the full copy's bytes at the 8-stream rate), each the median of its trial measurements over hot and append.

## Arms

Every arm reads and writes the disk under test. Nothing is throttled: the link is the zones' own.

| Arm | Yardstick | What runs | Source | Durable by | Blocks |
|---|---|---|---|---|---|
| **`pgdelta_direct_inplace`** (v1, 4 jobs) | the arm judged | EXP-11's v1 arm unchanged: 4 jobs on each end, `--source-kind datadir`, `--in-place`, `--fsync main`, `--prefetch-basis on`, in clear | stopped cleanly | pgdelta's in-place commit | every block |
| `pgdelta_direct_inplace_j8` (v1, 8 jobs) | beside the verdict | the same with `--jobs 8` on both ends | stopped cleanly | as v1 | main blocks |
| `parcopy8` | **rule 3's (B_fast)** | EXP-11's bound unchanged: 64 MiB ranges over 8 TCP streams, write-back every 8 MiB, a parallel `fsync` of every file and directory | stopped cleanly | its own | every block |
| `full` | **released (B_rel); Omni today (F0)** | `pg_basebackup -Fp -X stream -c fast --manifest-checksums CRC32C --verbose`, one stream, `--sync-method=fsync` | running, idle | its default flush | every block |
| `full_syncfs` | released (B_rel) | `full` with `--sync-method=syncfs` | running, idle | `syncfs` of `/data` | S only |
| `probackup8`, `probackup4` | **released (B_rel)** | EXP-11's `pg_probackup catchup -b FULL --stream --temp-slot -j 8` (or `-j 4`) over SSH (`aes128-gcm`), with its WAL ticker, then `sync -f` of the output in the same phase | running, idle | tool + `sync -f` | main blocks; `probackup8` in the warm block |
| `full_nosync` | negative control, never a baseline | `full` with `--no-sync` | running, idle | not durable | each trial's first repeat |

**Runs per session:**

| Invocation | Trials | Runs per trial | Runs |
|---|---|---|---:|
| 1: `pd-balanced`, S | hot, append | 7 arms × 5 repeats, plus `full_nosync` once | 72 |
| 2: `pd-balanced`, L | hot, append | 6 arms × 5, plus `full_nosync` once; then 4 arms × 3 warm | 62 + 24 = 86 |
| 3: `pd-ssd`, L | hot, append | 6 arms × 5, plus `full_nosync` once | 62 |
| **all** | | | **220** |

**Why these arms.**
- **v1's arm is fixed in advance:** 4 jobs, EXP-09's and EXP-11's registered arm. 8 jobs is reported beside it, as EXP-11 did, since D50 (4 workers) is provisional and T10b sizes the worker count.
- **`parcopy8` is rule 3's yardstick,** EXP-11's bound unchanged. It reads with 8 parallel readers and writes back every 8 MiB, so on a capped disk it should sit just above floor(size).
- **The released yardstick** is what Omni could run today with no new code: one `pg_basebackup` stream (what Omni runs to seed a standby) and `pg_probackup catchup` (the only released parallel full copier found, EXP-11). At L, with tables in 1 GiB segments, `pg_probackup`'s per-file threads can finally split the work.
- **`full_syncfs` runs at S only.** The ledger's note asks for it beside `full`, and at S it may differ on a persistent disk: `full`'s fsync method flushes about 1,000 files one by one, each a round trip to the disk. At L both flush most of the copy during the stream (E12), and their difference is the final flush of about the write-back threshold. See Changes, item 6.
- **Hot and append** are the two workloads EXP-09's HA rule names. Uniform isn't run (Considered and not run).

### Considered and not run

| Option | Why not |
|---|---|
| Local NVMe beside the persistent disks, as the ledger's note says ("beside the local NVMe") | Local-SSD shapes were out of stock in three of four zones on October 22 (`config.sh`), and T12r, as FIRMNESS proposes it (its revision 10 for EXP-13), replicates EXP-11's NVMe cells on fresh pairs. The size effect is measured within the session by S against L on the same disk. Rule 8, item 2 |
| A pair in one zone on persistent disks | It would split the zones' effect from the disk's. But the question is the verdict on Omni's conditions as a whole, and the link tests, pings and the arms' own timers say whether the link or the disk sets each arm's pace |
| A smaller disk, such as 100 GiB (Omni's 2× sizing for 48 GB of data: 168 MiB/s) | It makes every method more disk-bound. Whether v1's CPU matters then fades, which moves the ratios toward the count of disk passes each method makes (about 1 for the copies, 1 to 2 for v1). The verdict here is stated for a 2,000 GiB disk, and a smaller one stays an open branch (Limitations) |
| Two pairs at once, one per disk kind (32 vCPUs) | It halves the wall time but confounds disk kind with VM pair, and fills the quota, so T12r couldn't run beside it (FIRMNESS's waves 1 and 3). One pair compares both kinds on the same VMs |
| `pd-ssd` at S | `pd-ssd` is the second reading of the deciding size; at S, `pd-balanced` already bridges to EXP-11 |
| Hyperdisk, regional persistent disks | Newer series (N4, C4) take only Hyperdisk, which GKE nodes on them would use. Omni's HA standby has its own zonal disk; a regional disk is a different HA design. Both are later questions |
| Uniform 1% | It decides no row of EXP-09's rule. At L it changes about 46% of `pgbench_accounts`'s pages (EXP-01), about 14 GB on the wire for v1, and costs a trial build. EXP-11 reported it at 2.1 GB |
| `parcopy4`, matched parallelism | EXP-11: within 0.10–0.15 s of `parcopy8`, reported only. At L both are capped by the disk |
| `full_syncfs` at L | See Why these arms, and Changes item 6 |
| `full_wb` (dest's write-back at 64 MiB, host-tuned) | Not a released arm. Its effect is bounded by the view `R_wb`, `full` read without its sync phase. T8 runs dest's write-back at the default and at 64 MiB, and OMNI-2 reads `vm.dirty_*` on a GKE node |
| v1 with source and dest at different worker counts | T10b's (its dest-only arm) |
| The new-copy mode (D86) and its price against in place (O5) | It writes a whole second 48.5 GB copy, decides D86, not the HA verdict, and needs room for a second copy that Omni's 2× sizing may not leave. FIRMNESS put it in T9-PD; it stays open for T9 |
| Throttled link speeds, crossovers | HA's link is the zones' own. Crossovers are EXP-11's (one zone) and EXP-13's (DR) |
| A memory limit on the destination's processes, as a pod's limit would set | Page cache is charged to a pod's memory cgroup on GKE, so a pod limit would shrink the cache v1's re-read can use. Omni's pod limits aren't known; OMNI-2 can read them. A VM-sized cache favours v1 (Limitations) |
| 100–200 GB | T9, later (rule 10) |
| A busy source; TLS for pgdelta | T10b and T8; T12 and T8 |
| GKE itself | OMNI-2 (K4). Not part of this test |

## Baselines, ratios and views (added here)

Per disk and size, over the arms that pass every verification and durability check, without void runs:
- **B_fast,** rule 3's yardstick: the fastest of `parcopy8`, `full`, `full_syncfs` (S), `probackup4` and `probackup8`.
- **B_rel,** the released tools' yardstick: the same without `parcopy8`.
- **F0:** `full`, what Omni runs today.

**Choosing the arm** for each envelope: the lowest median over the main block's runs pooled over hot and append (10 per arm), since a full copy's work doesn't depend on the workload. Ties within max(0.05 s, 1%) go to the lower pooled maximum. The warm block's runs don't enter the choice.

**Ratios,** per workload, v1 at 4 jobs: v1's median over the chosen arm's median, both over the trial's 5 main-block runs:
- `R_fast`, `R_rel`, `R_F0`.
- `R_adj`: v1 plus `full`'s own backup-mode adder (its checkpoint wait and its wait from the WAL end point to its sync, from its `--verbose` stamps), over a live-source envelope. Against `parcopy8`, `R_adj` equals R. On an idle source the adder was 0.045 s in EXP-11, so at L it can't move a ratio by more than about 0.001.
- **Robustness views,** each read as its own row: `R_max` (maxima), `R_noSSH` (`total_no_ssh_s`), `R_paired` (the median of v1's run over the baseline's in the same repeat), and `R_pool` (against the baseline's median pooled over hot and append, 10 runs).
- **`R_wb`** (released yardstick only): B_rel recomputed with `full`'s and `full_syncfs`'s runs read without their sync phase. It bounds what dest's write-back setting could give the one-stream copies.
- **Warm (L, `pd-balanced`):** v1's warm median over the warm medians of B_fast's arm and of B_rel's arm, where those arms ran warm (3 runs each).
- **Beside, never the row:** each ratio with v1 at 8 jobs; each ratio on `pd-ssd`; and at S.

## Predictions and decision rules

Every prediction and rule is judged per workload on medians of 5 runs, with the maximum beside it (rule 6), unless it says otherwise. Each session is judged on its own first; [Rule 11](#rule-11-which-outcomes-may-pick-a-route) says what the two together may decide.

### The ledger's registered text (verbatim)

From T9's section of the ledger:

> *Added October 24: v1's path and the full copy on persistent disks (GKE's default), 5 repeats each, beside the local NVMe. Both move about 2.1 GB through the destination's disk: the full copy writes it, and v1 reads all of B to hash it (and re-reads it to verify once it no longer fits the cache). Prediction: the ratio moves toward 1.0, to 0.9–1.1. Rule, EXP-09's bands on medians of 5: at most 0.9 for both hot and append means the HA result holds on persistent disks; 1.0 or more for either means v1 targets DR first there; in between is undecided until T2 and T5, as in EXP-09.*
>
> *Added October 24 (after T2): T2 found the HA verdict Out on local NVMe against the fastest durable copy, a range-parallel copier ([EXP-11](experiments/EXP-11-full-copy-baseline.md)). By rule 3, the persistent-disk arm should be judged the same way: against the fastest durable copy measured there, so `parcopy8` and `full_syncfs` run beside `full` (EXP-11's harness has both).*

### How it is judged here

The registered text is about 2.1 GB, so it is judged at **S on `pd-balanced`**, as written:

| ID | Registered | How it is judged | Expected here (added) |
|---|---|---|---|
| P1 | "the ratio moves toward 1.0, to 0.9–1.1" | "The ratio" in the first note is v1 over the full copy, `full` (`R_F0`). Holds if `R_F0` is in [0.90, 1.10] for hot and for append. `R_fast` is reported beside it | **Fails low.** `R_F0` 0.12–0.47: one `pg_basebackup` stream reads with one reader, which is slow on a persistent disk (E2), and its whole 2.1 GB flush still lands at its end, at the disk's write rate (E5). `R_fast` 0.79–1.13 |
| P2 | the rule, EXP-09's bands | Judged twice (K6): against B_fast, as the second note says, and against B_rel. "Undecided until T2 and T5" is read as "until T5 and T13", EXP-11's Undecided row, since T2 is done (Changes, item 5) | Against B_fast: **Undecided or Out, near 1.00.** Against B_rel: **at most 0.9 for both** (`R_rel` 0.21–0.50) |

**The registered consequence is recorded as written,** but at 2.1 GB it picks no route on its own: 2.1 GB fits in RAM, which Omni's rebuild doesn't (rule 11, test P). It counts toward a route only where the verdict at L agrees.

### The HA verdicts (added here)

**Judged at L on `pd-balanced`, with v1 at 4 jobs, separately against each yardstick (K6),** by EXP-09's bands as EXP-11 read them. The same rows are computed at S, on `pd-ssd`, and with v1 at 8 jobs, and reported beside. Rows are checked in order.

**Against the fastest copy measured (rule 3; B_fast):**

| Row | Condition | What follows (in this session) |
|---|---|---|
| **Out on fast links** | `R_fast` ≥ 1.00 for hot or append | "Out against the fastest copy measured, on `pd-balanced` across zones at 48.5 GB; `R_fast` = x" |
| **Within reach** | `R_fast` ≤ 0.90 for both, `R_adj` ≤ 0.90 against B_fast, and the bound passes its gate (A3) | "Within reach of any copier on Omni's default disk at this size" |
| **Bound missing** | `parcopy8` excluded (A4, A5), or failing its gate while slower than B_rel's arm | Judged against B_rel alone, labelled "bound not measured"; provisional |
| **Undecided** | anything else | Decided after T5 and T13, and T9 at scale |

**By arithmetic, Within reach is out of reach at L unless something surprises.** v1's source reads all of S from its disk, as the bound does, so v1 takes at least D/C, 64.7 s on `pd-balanced`. A bound that passes its gate takes at most floor + 5%, about 67.9 s, and 0.9 × 67.9 = 61.1 s, below v1's least. At S, v1 takes at least about 3.08 s, so Within reach needs the bound at 3.42 s or more, near the top of what its gate allows (3.83 s). So the live rows against the fastest are Out and Undecided. Written now, before running.

**Against the released tools Omni can run (B_rel):**

| Row | Condition | What follows (in this session) |
|---|---|---|
| **Faster than the released copiers** | `R_rel` ≤ 0.90 and `R_adj` against B_rel ≤ 0.90, for both hot and append | "v1 is faster than the released copiers tested (`pg_basebackup` with fsync, `pg_probackup catchup -j 4` and `-j 8`) on `pd-balanced` across zones at 48.5 GB, from a stopped source", EXP-11's qualifier carried to Omni's conditions |
| **Not faster than the released copiers** | `R_rel` ≥ 1.00 for hot or append | "Out against the released copiers too" |
| **Undecided against the released copiers** | anything else | Reported with `R_rel` per workload |

`R_F0` is stated beside every row: v1 against one `pg_basebackup` stream, what Omni runs today.

**Flags,** beside each verdict:
- *Fragile:* `R_max`, `R_noSSH`, `R_paired` or `R_pool` would give another row.
- *Near the line:* a deciding ratio within ±0.03 of 0.90 or 1.00, whose crossing alone or with others within that margin would change the row.
- *Write-back-sensitive* (released verdict): `R_wb` gives another row.
- *Source-cache-sensitive* (L, `pd-balanced`): the warm ratios give another row. A live primary keeps much of its data cached, which one `pg_basebackup` stream would profit from most.
- *Bound suspect:* A3 fires.
- *Disk drifted:* A10 fires in a trial the row uses.
- *With void runs:* a trial has void `probackup*` runs; the verdict is also given with them included.

**Readings beside, never the row:**
- *8 jobs:* the same rows with v1 at 8 jobs. If they differ from 4 jobs', both are stated, and the difference belongs to D50 and T10b.
- *`pd-ssd`:* the same rows on `pd-ssd`. If they differ, the verdict is "disk-class-sensitive": both are stated, and which class Omni's standbys use is an integrator question.
- *S against L:* if the rows differ, the size changes the verdict, and L decides it.

### Added predictions

**Inputs, written now.** Each is labelled by where it comes from. The derivation is code (`analysis/exp14_model.py`, to be built, as EXP-13's model was). After the smoke run measures the inputs, it runs once on them, and its ranges are recorded here, dated, beside these. **Every E-range is judged as written here;** the recomputed one is reported beside it.
- C, each way: 0.749 GB/s on `pd-balanced`, 0.839 GB/s on `pd-ssd` (published, above).
- r1, one sequential buffered reader on the disk: 0.10–0.45 GB/s on `pd-balanced`, 0.12–0.55 on `pd-ssd` (assumed). Debian's read-ahead (128 KiB by default, recorded) keeps one or two requests in flight, and each request to a persistent disk takes about 0.5–1.5 ms. So r ≥ C needs several readers.
- One `pg_probackup` thread: min(r1, 0.24 GB/s) (EXP-11: if the largest file was its critical path, that thread moved about 0.24 GB/s).
- The link across zones: E1 (assumed).
- v1's destination pool processes both passes over B at 1.5 GB/s with 4 jobs and 1.7 GB/s with 8; its source hashes at 1.33 GB/s with 4 (EXP-11: 2 × 2.12 GB in about 2.8 s of the 3.11 s run, 2.45 s of 2.76 s at 8 jobs; the source's hashing 1.59–1.61 s).
- h, the share of v1's verification re-read served from the page cache: 1.0 at S; 0.8–1.0 at L (assumed). Verification re-reads each file right after it is patched, a few files after the first pass read it: the sender's window is 4 files of at most 1 GiB, against about 30 GiB of cache.
- Write-back: Debian's thresholds, about 3 and 6 GB on dest. At L a stream that arrives no faster than the disk writes keeps `Dirty` near the background threshold, so the final flush is about 3–6 GB.
- Each `fsync` on a persistent disk: 0.5–2 ms (assumed).

| ID | Prediction | Basis |
|---|---|---|
| E1 | **The zones:** ping median 0.15–0.8 ms; one TCP stream 7–15 Gbit/s; 8 streams 14.5–16 Gbit/s. One stream isn't window-limited (3 MiB per round trip is above 30 Gbit/s at 0.8 ms) | EXP-11 in one zone: 10.5–15.1 and 15.2–15.3 Gbit/s; the 16 Gbit/s egress cap |
| E2 | **The disks:** the write floor and the 4-reader cold read floor at 0.90–1.00 of C (probes table). One reader: 0.10–0.45 GB/s on `pd-balanced`, 0.12–0.55 on `pd-ssd` | published caps; r1 |
| E3 | **Memory and size:** `MemTotal` 30.5–32 GiB; B0 at L 1.4–1.6 times it. 25–31 GB of the source's data cached after the warm read | the machine type; Linux's reserved memory |
| E4 | **`parcopy8`:** S 3.1–3.9 s; L 65–68 s on `pd-balanced` and 58–61.5 s on `pd-ssd`. It passes its gate (A3) everywhere | floor plus EXP-11's 0.3–0.8 s of start and launch; 8 readers reach C |
| E5 | **`full`:** S 7.5–26 s, its sync phase 2.9–4.5 s; L 110–495 s on `pd-balanced`, 91–410 s on `pd-ssd`, its sync phase 3–9 s. At S, `full_syncfs` 0–1.5 s faster than `full` (one `syncfs` instead of about 1,000 round trips to the disk) | its stream at one reader's rate; at S the whole 2.1 GB flushed at its end at C, plus about 1,000 per-file `fsync`s; at L the final flush of what is dirty when the stream ends |
| E6 | **`pg_probackup`:** S, both arms 7–14.5 s. L on `pd-balanced`: `probackup8` 68–85 s, `probackup4` 68–140 s; on `pd-ssd` 61–76 s and 61–120 s | at S the largest file (0.94 GB) on one thread, then its flush of 2.1 GB at C; at L the copy at min(threads × one thread's rate, C), plus at most one 1 GiB segment's tail, its start and its flush |
| E7 | **v1 at 4 jobs:** S 3.1–3.5 s (up to 5.9 if one reader gets less than 0.19 GB/s, so 4 can't reach C). L on `pd-balanced` 66–83 s (66–245); on `pd-ssd` 66–75 s (66–121) | the larger of the source's read, dest's reads (2 − h passes) and the pool's CPU, plus 1–5 s of start, the last file's verification and the patched pages' `fsync`s. In brackets, every input at its worst |
| E8 | **v1 at 8 jobs against 4:** S −0.3 to +0.1 s; L −3 to +1 s on `pd-balanced`, −10 to 0 s on `pd-ssd` | at 8 jobs the pool's CPU (57 s at L) drops below `pd-balanced`'s disk time (64.7 s) but not `pd-ssd`'s (57.8 s) |
| E9 | **Where v1's re-read comes from:** `dest_read_share` 0.50 at S (within 0.02) and 0.50–0.60 at L. `leaf_nodes_reread` 0 at S and above 0 at L | h; D33's 64 MiB cap covers about 33 GB of B |
| E10 | **The ratios,** v1 at 4 jobs, central values (one reader at least 0.19 GB/s, h at least 0.8), with every input at its worst in brackets: the table below | E4–E7 |
| E11 | **The rows:** at S, Undecided or Out against the fastest (near 1.00) and faster than the released copiers. **At L on `pd-balanced`: Out against the fastest copy, and Undecided against the released copiers.** EXP-11's qualifier (`R_rel` ≤ 0.90 for both) fails at L, because `pg_probackup`'s threads split 1 GiB segments. Against one `pg_basebackup` stream, v1 stays faster (`R_F0` at most 0.75). On `pd-ssd`, the same rows, with `R_fast` up to 0.1 higher | E10 |
| E12 | **Write-back during the stream:** at L, dest's `Dirty + Writeback` peaks at 2.5–7 GB during every full copy; at S it reaches 2.0–2.2 GB and stays until the sync | Debian's thresholds |
| E13 | **The warm source** (L, `pd-balanced`): `full` 25–60% faster than cold; `probackup8` and `parcopy8` 0–15%; v1 0–10%. So the warm `R_F0` is 1.2–2.5 times the cold one, and the warm `R_fast` and `R_rel` within −0.1 and +0.2 of the cold ones, unless a warm `full` becomes B_rel's arm | a warm source spares one reader most; dest's disk and B, cold in every run, bind the others |
| E14 | **S against L:** `R_rel` rises by 0.3–0.9 from S to L | `pg_probackup`'s threads and its flush during the stream at L |
| E15 | **Durability:** every durable arm passes (a) and (b) in every run and the syscall audit on `pd-balanced` at scale 5. `full_nosync` fails (a) by at least 1.5 GB at S and at least 1 GB at L | EXP-11; E12 |
| E16 | **CPU per GB:** v1's destination pool processes both passes at 1.3–1.7 GB/s at 4 jobs (EXP-11's 1.5, within its session drift) | EXP-11 |

**E10, expected ratios:**

| Cell | `R_fast` | `R_rel` | `R_F0` |
|---|---|---|---|
| S, `pd-balanced` | 0.79–1.13 (up to 1.9) | 0.21–0.50 (up to 0.84) | 0.12–0.47 (up to 0.79) |
| L, `pd-balanced` | 0.97–1.28 (up to 3.8) | 0.78–1.22 (up to 3.6) | 0.13–0.75 (up to 2.2) |
| L, `pd-ssd` | 1.08–1.29 (up to 2.1) | 0.87–1.23 (up to 2.0) | 0.16–0.82 (up to 1.3) |

Each range divides a v1 range by a baseline range end to end, so it is wider than the outcome is likely to be.

### Added rules

| ID | Condition | Consequence |
|---|---|---|
| A1 | The verdicts, at each disk and size | As their tables say |
| A2 | **`full_syncfs`** (S): at least 0.3 s faster than `full` in both workloads | As EXP-11's A1: the fastest shipped single stream is `full_syncfs`, a one-flag change for Omni, and `R_F0` is restated against it too |
| A3 | **The bound's gate:** `parcopy8`'s pooled median over hot and append (10 runs) is more than floor + max(1.0 s, 0.05 × floor), or its busiest stream carries more than 1.1 × the mean bytes | The bound is suspect, and its timers must explain the gap. Within reach is blocked. If `parcopy8` is still B_fast, it stays there for Out, since a slow bound only favours v1. If a released arm is faster, the "Bound missing" row applies |
| A4 | **Verification,** as EXP-11's A4, with the void rule above. A run that times out (four times its expected upper bound, at least 120 s at S and 1,200 s at L) or whose own tool aborts is a tool failure | The arm is excluded from every baseline at that disk and size |
| A5 | **Durability,** as EXP-11's A5: a durable run fails (a) or (b), or `parcopy8`'s `fsync` count is wrong | The run is excluded. More than 1 such run of an arm at one disk and size in one trial reclassifies the arm as not durable there (rule 8). If v1 is reclassified, the row reads "v1 not durable" |
| A6 | **The negative control:** `full_nosync` passes (a) in any run | Check (a) is void at that disk and size; durability rests on (b) |
| A7 | **The size:** the smoke run finds B0 at L below 1.4 × dest's `MemTotal` | The scale is raised until it reaches 1.5 ×, before the sweep, recorded here, dated |
| A8 | **The disk's limits:** a trial's write floor or 4-reader read floor is below 0.9 × C | Reported as "disk under its published limit", with the measured rate; the ratios are read against what the disk gave. Not a flag: every arm in the trial saw the same disk |
| A9 | **The link:** a trial's median one-stream test across the zones is below C (6.0 Gbit/s on `pd-balanced`) | The one-stream copies are labelled link-bound there, and their timers say whether the link or the reader set their pace. Reported |
| A10 | **Disk drift:** a trial's start and end write floors differ by more than 10% | The trial's rows carry *disk drifted* |
| A11 | **The two sessions' shift:** for each arm and cell, the change of its median from session 1 to session 2, as a share of session 1's | Reported for every arm. A deciding ratio whose margin to its line is smaller than the larger shift of the two arms it compares is labelled "margin inside the shift" (K10). With agreeing rows it stays firm by K10's wording, and the write-up says so beside it |

**No fix and rerun after the main sweep starts.** Fixes found in the smoke run are made before the sweep and recorded here, dated. Between the two sessions nothing changes in the harness, the design or the seeds, except for an infrastructure fault; any change is recorded under rule 8, and the rows it touches are not replicates.

## Rule 11: which outcomes may pick a route

Rule 11: only a firm decision picks a single route. Here a reading is firm when **both sessions give the same row, at L on `pd-balanced` with v1 at 4 jobs, with no flag in either** (K10). It is firm only for the conditions it was measured on: `n2-standard-8`, 2,000 GiB disks, `us-central1-a` to `-b`, 48.5 GB of pgbench data, hot and append at 1%, an idle source read cold, and v1 and the bound reading a stopped source.

| Reading | May pick a single route when | The route it picks | What stays open whatever happens |
|---|---|---|---|
| The ledger's text at S (P1, P2) | never on its own (2.1 GB fits in RAM) | none; its consequence counts only where L agrees | – |
| **Out against the fastest copy** | both sessions, unflagged | D03's "HA Out on fast links" is firm for v1's design on Omni's default disk, across zones, at this size. T5 and T13 stop being ranked for reaching HA's line against a parallel copier; they keep their DR, source-CPU and correctness reasons. It is firm for a stopped source. Both v1 and the bound read one, and backup mode cost about 0.05 s on an idle source in EXP-11, so an idle live source isn't expected to move it; T8 measures that | T9 at 100–200 GB (rule 10); a larger VM, whose cap is 1,200 MiB/s; a smaller disk; a busy source (T10b, T8) |
| **Undecided against the fastest** | both sessions, unflagged | EXP-11's Out doesn't carry to Omni's conditions: the route "HA dropped against a parallel copier" closes. HA is a target beside DR, and T5 and T13 are ranked for it again | which of HA and DR comes first: no single target |
| **Not faster than the released copiers** | both sessions, unflagged, write-back- and source-cache-sensitive included | With Out against the fastest: HA is closed as a speed target for v1's design at this size. HA's pitch is bytes across zones (about 0.01 USD per GB between zones) and source slowdown, not time. DR is the single target. A live v1 only adds work (backup mode, WAL, recovery), so this holds for a live source by dominance | T9 at scale; a larger VM |
| **Faster than the released copiers** | both sessions, unflagged, write-back- and source-cache-sensitive included | HA's positioning names the released yardstick (K6): "faster than `pg_basebackup` and `pg_probackup` on Omni's default disk, across zones, at 48.5 GB, from a stopped source". The route "HA dropped altogether" closes | the same claim for a live or busy source (T8, T10b), which can only add time to v1 |
| **Undecided against the released copiers** | never | none | both: HA kept or dropped against the released tools |
| Within reach, Bound missing | never: out of reach by arithmetic, or no bound | none | – |
| 8 jobs differs from 4 jobs | never | none | the worker count (D50, T10b) |
| `pd-ssd` differs from `pd-balanced` | never: the `pd-balanced` row picks, and the difference is stated | none | which storage class Omni's standbys use (an integrator question) |
| The sessions disagree, or either is flagged | never | none | both rows; a third session, on another day, is proposed to the owner |
| What the measurements describe: E1's zones, E9's share of the re-read from disk (W2, D41p), D33's leaf re-reads, E12's write-back, the probes | never: they describe, they don't choose | none | – |

## Notes written before running

- **Why `pd-balanced` decides and `pd-ssd` sits beside it.** `pd-balanced` is GKE's default storage class, so it is what Omni's standbys get unless an operator chooses otherwise. On this VM the two classes share the IOPS cap and differ by 12% in throughput, so `pd-ssd` is expected to give the same rows.
- **Why 2,000 GiB.** A disk's throughput is set by its size. Google sizes Omni's disk at 2,000 GiB for 1 TiB of data, the scale at which a rebuild takes hours. The database here is smaller than the disk is sized for, so that each run takes minutes; the disk's throughput is what a TB-scale standby would have.
- **Why the zones matter little and are still measured.** At 0.15–0.8 ms one default TCP stream isn't window-limited, and both disks cap below the link. So the zones are expected to change nothing but a few round trips. The pings and link tests check that; A9 labels a one-stream copy the link held back.
- **The expected outcome, written now:** P1 fails low; at S, Undecided or Out against the fastest, near 1.00, and faster than the released copiers. **At L on `pd-balanced`: Out against the fastest copy (`R_fast` 0.97–1.28, so near the line is possible), Undecided against the released copiers (`R_rel` 0.78–1.22), faster than one `pg_basebackup` stream.** EXP-11's qualifier is expected to fail at L. Most of v1's re-read comes from the page cache even at L. One reader on a persistent disk is slow, which makes one `pg_basebackup` stream the slowest copy. A warm source speeds that stream most, but is expected to leave both rows as they are. `pd-ssd` gives the same rows. 8 jobs gives the same rows on `pd-balanced`.
- **Arithmetic that bounds it.** At L every method moves 48.5 GB through a disk capped at 0.749 GB/s, 64.7 s. v1's pool needs about 65 s of CPU for both passes at 4 jobs. So v1 and the bound meet near 1.0 if the re-read comes from the cache, and v1 takes about twice the bound's time if it doesn't.
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
9. **`pd-ssd` beside `pd-balanced`** at L, as a second reading. Grade B.
10. **The ledger's "5 repeats each" is kept,** at both sizes, with `R_pool` and two sessions added. Grade B.
11. **Uniform changes aren't run.** Grade B.

## Limitations, known before running

- **One VM shape.** On an `n2-standard-8` both disk classes are capped at 800 MiB/s, and v1's pool has 8 vCPUs. A larger VM lifts the cap to 1,200 MiB/s and gives v1 more CPU; how the ratios move there isn't measured.
- **One disk size,** 2,000 GiB. A smaller disk is more disk-bound for every method; a smaller Omni deployment would have one.
- **48.5 GB, not TB scale.** No ratio is projected to TB (rule 10). What changes beyond it (more segments, a longer tail, the prefetch's cap) is T9's.
- **A VM-sized page cache.** On GKE a pod's memory limit would bound the cache v1's re-read can use. **This favours v1.**
- **Stopped sources** for v1 and `parcopy8`; the released arms read a running, idle one and pay backup mode. On an idle source that costs about 0.05 s. WAL streaming, recovery and a busy source are T8's and T10b's.
- **A cold source** in every main-block run. A live primary caches much of its data, which speeds one `pg_basebackup` stream most. **This favours v1 against `full`.** The warm block is three arms, 3 runs each, at L on `pd-balanced`.
- **A freshly copied source.** Each L trial's source files are written sequentially by `cp -a`, so they are less fragmented than an aged database's. Every arm reads them.
- **Debian, not Container-Optimized OS.** Read-ahead, the I/O scheduler, `vm.dirty_*` and TCP settings are Debian's defaults; GKE's node image may differ (OMNI-2 reads them).
- **Transport is uneven,** as in EXP-11: `pg_probackup` over SSH, the others in clear. pgdelta will need TLS (O6; EXP-13 prices it).
- **Two zones, one pair of them.** Another zone pair may have another round trip; it is measured and recorded.
- **pgbench data** with local changes at 1%.
- **Fast checkpoints, and no deletion of an old copy on the clock,** favour the full copies, as in EXP-11.
- **Disk kind is confounded with time** in the session; the ratios compare arms within a repeat.
- **Two sessions.** A third is run only if the two disagree, and only if the owner asks for it.

## Smoke run

Before the main sweep of each session, on the two new VMs. Each item says what it settles, the record it reads, and what happens if it fails. Results are recorded here, dated, in session 1; in session 2 they are recorded beside session 1's.

| # | Item | Record | Pass | If it fails |
|---|---|---|---|---|
| 1 | **Quotas,** read before the VMs are created: vCPUs in all regions, N2 vCPUs, instances, external addresses and SSD persistent disk in `us-central1` | the session's notes (read-only `gcloud` output, kept outside the repository) | room for 16 vCPUs, 2 instances, 2 addresses and 8,100 GB of SSD disk, plus what runs beside | the sequential fallback for `pd-ssd`; or ask the owner |
| 2 | **Zones, disks and filesystems:** each VM's zone and CPU platform; each data disk's kind, size and device name, from `gcloud` and from the VM; `.disk-kind` on each; ext4 in ordered mode with barriers; read-ahead and scheduler | `run.json`: `environment.disks`, `environment.zone` | as designed | fix the infrastructure |
| 3 | **The disks' limits:** the write floor and the 4-reader read floor on both kinds at 8 GiB, and the single reader at 4 GiB | `run.json`: `disk_probes` | recorded; A8 read | – (A8 reports; the model takes the measured rates) |
| 4 | **The zones:** 20 pings; one stream each way and 8 streams, both host directions | trial record: `link_tests`, `pings` | recorded | – (A9 reports) |
| 5 | **Memory and size:** `MemTotal` and `MemAvailable` after a drop; the dirty thresholds; B0's bytes at L | `run.json`: `environment.memory`; trial record: `b0_bytes` | A7 doesn't fire | A7: raise the scale |
| 6 | **The L dataset:** build time; the pristine copy and its restore; segment counts of `pgbench_accounts` and `events` | trial record: `setup` | the restored cluster starts and its checksums verify | fix |
| 7 | **The disk swap:** `mount-data` to `pd-ssd` and back on both VMs; the results directory survives on the boot disk; the preflight refuses the wrong disk | `run.json`: `environment.disks` after each swap | yes | fix |
| 8 | **Durability:** checks (a) and (b) on the data disk at scale 5 and for `full` at L; the idle noise over 20 windows of 5 s, 6 of 30 s, 3 of 120 s and 2 of 600 s, which becomes the run lines' `NOISE` table; `full_nosync` at L leaves at least 1 GB dirty | run records: `durability`; `run.json`: `dirty_idle_noise` | as EXP-11 | as EXP-11 |
| 9 | **The syscall audit** of every arm at scale 5 on `pd-balanced` | `run.json`: `syscall_audit` | every durable arm 0 uncovered; `full_nosync` all | as EXP-11's smoke item 3 |
| 10 | **`pg_probackup` at L:** its threads (`ps`), the WAL ticker, `sync -f` inside the phase; the bytes-first check on one run; the logical fallback exercised on a forced difference (a unit test) | run records: `processes`, `phases`, `verification` | all | fix the wrapper |
| 11 | **The cache-state records** on one v1 run at L: `dest_read_share`, `fincore` of B, the dirty sampler, the prefetch's bytes and cap, `leaf_nodes_reread` | run record: `cache_state`, `tool_reported` | all present | fix |
| 12 | **The warm read at L:** cached bytes of the source when the clock starts | run record: `cache_state.source_fincore` | recorded (E3) | – |
| 13 | **Parallel digests:** the start reference digest at L in at most 1.3 × floor | trial record: `reference_digests` | yes | fix, or record the longer session |
| 14 | **The time projection:** one L repeat of every arm on `pd-balanced` (hot), with `full_nosync` and one warm repeat | run records: `started_utc`, `finished_utc` | – | the session runs whatever it projects (no cut line, K1); the projection is recorded |
| 15 | **`analysis/exp14.py` and `exp14_model.py`** end to end on the smoke CSV, writing to a scratch directory; the model's ranges on the measured inputs recorded beside the written ones | their output directory | runs | fix |
| 16 | **Reference digests:** differences reported by file class | trial records: `reference_digests` | any user-table difference listed | fix the analysis |
| 17 | **Teardown:** no labelled instance or disk left | `gcloud compute instances list` and `disks list` filtered on the label | none | delete what is left |

## Run plan and cost

**Per-run times, bottom-up.** From EXP-11's records and the predictions above. At L on `pd-balanced` (central, with the range):
- v1 (either job count): its 66–135 s, plus copying B0 to B (about 65 s), the stop and restart, the digests on both sides (about 65 s in parallel) and the start: about 4 minutes (3.6–5.3).
- `parcopy8`: about 2.8 minutes (2.6–3.2). `pg_probackup`: about 3 (2.6–3.5).
- `full`: its 110–495 s plus `pg_verifybackup` (about 65 s) and the start: about 4.7 minutes (2.5–9.5).
- A trial's restore, B0, workload, digests and probes: about 25 minutes (18–42).

`pd-ssd` is about 10% faster in every disk-bound step. At S, a run takes 0.3–0.9 minutes, and a trial's set-up about 8.

| Step | Runs | Hours of the pair |
|---|---:|---:|
| Read the quotas; create both VMs in two zones with their four data disks; provision (format with lazy initialisation off); deploy | – | 0.6 (0.5–0.7) |
| Smoke run (above), the L dataset's build (30–60 min) and the idle-noise windows (31 min) included | about 45 | 2.4 (2.0–3.0) |
| Invocation 1: `pd-balanced`, S | 72 | 1.0 (0.8–1.4) |
| Invocation 2: `pd-balanced`, L, with the warm block | 86 | 6.3 (5.0–9.3) |
| Swap the disks; copy the pristine dataset to `pd-ssd` | – | 0.1 |
| Invocation 3: `pd-ssd`, L | 62 | 4.2 (3.3–6.3) |
| Fetch; teardown; check no labelled disk is left | – | 0.25 |
| **One session** | **220 in the sweep, about 265 with the smoke run** | **about 15 (12–21)** |

- **VMs and vCPUs:** 2 `n2-standard-8`, 16 vCPUs, 2 instances and 2 external addresses at any time. That fits beside one EXP-13 pair (T12r) within the 32-vCPU quota, as FIRMNESS's waves 1 and 3 propose. No local SSD is needed, so the local-SSD stockouts don't apply.
- **Instance-hours:** about 30 per session (24–42), **about 60 for both sessions (48–84)**. FIRMNESS estimated about 12 for T9-PD at 2.1 GB alone; the 48.5 GB size multiplies every disk pass by 23.
- **Disks:** per VM, 2,000 GiB of `pd-balanced`, 2,000 GiB of `pd-ssd` and the 50 GiB boot disk: 8,100 GB of the regional SSD quota for the pair.
- **Cross-zone traffic:** every full copy at L sends 48.5 GB from one zone to the other. Per session about 6.2 TB (invocation 2 about 3.0 TB, invocation 3 about 2.1, the smoke run about 0.5, the link tests about 0.5, S about 0.1).
- **Money, at list prices** (to be checked when the quota is read): instances about 15 USD per session at FIRMNESS's 0.5 USD per instance-hour; disks about 22 (`pd-balanced` 0.10 and `pd-ssd` 0.17 USD per GiB-month, for 15 hours); cross-zone traffic about 62 (0.01 USD per GB). **About 100 USD per session, about 200 for both.**
- **Wall time:** two days, about a day apart, each with one session of about 15 hours, run unattended on dest as before. Session 2 starts on fresh VMs, created on another day.
- **No cut line** (K1, K12). A session stops early only for an infrastructure fault; then the invocations that finished stand, and the rest run in a later session on fresh VMs, recorded.

**Run lines** (the shape; options marked "new" are to be built):

```sh
NOISE=5:4,30:4,120:4,600:4   # smoke item 8: dest's idle Dirty + Writeback noise, MiB, by window length in s
COMMON="--manifest-checksums CRC32C --dirty-noise-table $NOISE --speeds unthrottled --repeats 5 \
    --link-tests per-trial --pings-each-repeat 20 --method-repeats full_nosync=1"   # the last two options new
M=full,full_nosync,probackup4,probackup8,parcopy8,pgdelta_direct_inplace,pgdelta_direct_inplace_j8
python3 experiments/network/remote.py mount-data pd-balanced                      # new
python3 experiments/network/remote.py start exp14-bal-s -- --disk-kind pd-balanced --scale 70 \
    --patterns hot,append --order-seed 141 $COMMON --methods $M,full_syncfs           # --disk-kind new
python3 experiments/network/remote.py start exp14-bal-l -- --disk-kind pd-balanced --scale 1600 \
    --pristine-dataset /data/pristine-s1600 --probackup-verify bytes-first \
    --patterns hot,append --order-seed 142 $COMMON --methods $M \
    --warm-source-repeats 3 --warm-source-methods full,probackup8,parcopy8,pgdelta_direct_inplace
python3 experiments/network/remote.py copy-pristine pd-ssd                        # new
python3 experiments/network/remote.py mount-data pd-ssd
python3 experiments/network/remote.py start exp14-ssd-l -- --disk-kind pd-ssd --scale 1600 \
    --pristine-dataset /data/pristine-s1600 --probackup-verify bytes-first \
    --patterns hot,append --order-seed 143 $COMMON --methods $M
```

The same lines and seeds run in session 2. `NOISE` is 4 MiB at every length until smoke item 8 sets it, as in EXP-13.

## What the harness must add

**Reused from EXP-11 to EXP-13 as they are:** every arm (`full`, `full_syncfs`, `full_nosync`, `probackup4` and `probackup8` with the WAL ticker and `sync -f`, `parcopy8`, v1 at 4 and 8 jobs); checks (a) and (b), the syscall audit, the negative control; the probes; the warm-source block; the reference digests and EXP-11's verification; `--method-repeats`, the idle-noise plan and the noise table by window length, the void rule by file class and the ping check (EXP-13); `fincore` (EXP-12's packages); `analysis/exp04.crossover` isn't needed (one speed).

**To add (not built), about 3 days:**

| Block | File | What |
|---|---|---|
| Infrastructure: zones and disks (about 0.75 day) | `infra/gcp/config.sh`, `create.sh`, `teardown.sh` | `SOURCE_ZONE` and `DEST_ZONE` in place of one `ZONE`; `LOCAL_SSDS=0`; data disks created with each VM (`--create-disk` with `type=pd-balanced` and `type=pd-ssd`, 2,000 GB, `auto-delete=yes`, device names `pgd-data-balanced` and `pgd-data-ssd`, the benchmark's labels); a read-only quota check before creating; `known_hosts` kept per pair rather than deleted (FIRMNESS's C-capacity item); teardown per zone, then a check that no labelled disk is left |
| | `infra/gcp/startup.sh` | a persistent-disk mode: no local NVMe required; each data disk formatted ext4 with lazy initialisation off and marked with `.disk-kind`; the disk under test mounted at `/data`, the other unmounted; results under the boot disk |
| | `experiments/network/remote.py` | instances looked up per zone; `mount-data KIND` and `copy-pristine KIND` on both VMs; the results root on the boot disk |
| Orchestrator (about 1.25 days) | `experiments/network/run_exp04.py`, `measure.py`, `agent.py` | `--disk-kind`, checked by the preflight against the device behind `/data`; `/proc/diskstats` of that device in place of the md array (check (b), the disk counters on both VMs); `run.json`: zones, disks (kind, size, device, read-ahead, scheduler), the published caps computed, memory after a drop; `--pristine-dataset` (build once, restore by `cp -a` per trial); `--link-tests per-trial` and `--pings-each-repeat`; the cache-state record per run (`MemAvailable` after the drop, a 0.5 s dirty-page sampler, `fincore` of B after the clock stops, `fincore` of the source before a warm run, `dest_read_share`); pgdelta's `leaf_nodes_reread`, `leaf_nodes_cached` and prefetch figures into the CSV; the single-reader probe on the first 4 GiB of a relation; parallel per-file SHA-256 in `datadir-digest`, `tree_digest` and `files_digest` (4 workers, the same digests); `--probackup-verify bytes-first` with the logical fallback and the source's logical digest at the trial's end only when a run is pending; run timeouts scaled with the size (A4); EXP-01's ground truth skipped at L |
| Analysis (about 0.75 day) | `analysis/exp14.py`, `analysis/exp14_model.py` (new) | envelopes per disk and size, chosen on pooled main-block runs; both verdicts (K6) with their rows and every flag; `R_adj`, `R_max`, `R_noSSH`, `R_paired`, `R_pool`, `R_wb` and the warm ratios; P1 and P2 at S; the 8-job, `pd-ssd` and S readings beside; A2–A11; **a two-session reader** that prints each row per session, whether they agree, the shift of every arm between sessions (A11), and which routes the rule 11 table allows; the cache-state, write-back, disk and link tables; figures `exp14-times.svg` (each arm's time by disk and size) and `exp14-ratios.svg` (both yardsticks, both sessions); `exp14-runs.csv`, `exp14-cells.csv`. `exp14_model.py`: the derivation of E2–E10 as code, whose inputs are C, r1, one thread's rate, the link, the pool's rates and h |
| Tests (about 0.25 day) | `tests/test_exp14.py` | `create.sh`'s commands in a dry run: two zones, the disk flags, labels and auto-delete; the preflight refusing the wrong disk; the diskstats device chosen by kind; the pristine restore; the bytes-first rule and its fallback; the parallel digests equal to the serial ones; the verdict rows on synthetic medians, every row and flag reachable, both yardsticks; the two-session reader on synthetic sessions that agree, disagree and are flagged; the model's ranges, from the inputs written here, inside the ranges written here |

Docs: a "Methods for T9-PD" section in `experiments/network/README.md`, and this write-up with a row in the experiments index.
