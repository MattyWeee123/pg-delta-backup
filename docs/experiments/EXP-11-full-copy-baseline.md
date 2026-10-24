# EXP-11: How fast can a durable full copy go, and does v1's path still beat it?

Status: **pre-registered October 24, 2026, revised after two independent reviews and a check of the revision; the harness is built and reviewed (see the end of the revisions); not yet run.** This is test T2 of the [decision ledger](../DECISIONS.md#t2-fastest-durable-full-copy-as-v1s-baseline). It merges three independent drafts. The ledger's registered predictions and rules are kept word for word and judged as registered, except the zstd part, which moves to T4 (change 3). Everything marked "added here" is new. Where the arms depart from the ledger's, the reason is under [Changes from the ledger](#changes-from-the-ledger-rule-8), as rule 8 asks. A feasibility review and a fairness review checked the first version before anything ran. Every change they led to, and the changes from the check of the revision, are listed under [Revisions before running](#revisions-before-running-october-24-2026).

## Question

[EXP-09](EXP-09-direct-read.md) measured v1's path (`pgdelta_direct_inplace`: a direct read of a stopped source, B patched in place) at 2.69–2.71 s for local changes at every speed. One `pg_basebackup` stream took 3.13–3.15 s unthrottled. The ratio, 0.86, met EXP-09's HA rule (at most 0.9) by about 0.1 s. But rule 3 asks for the fastest durable full copy, and the ledger registered that a durable parallel copy takes 2.0–3.0 s. Against that, the ratio would be 0.90–1.35, and the verdict would reverse.

On the same two VMs, in one session with v1's path:
1. **How fast can a durable full copy go?** Two answers are needed: with released tools an operator could deploy today, and with a copier built to approach the hardware's floor.
2. **Where does one stream's time above link time go?** In EXP-09 it was 1.47 s at 2 Gbit/s, 1.51 s at 5 Gbit/s and 2.02 s unthrottled (EXP-04b: 1.39–1.55 s). The working hypothesis is the final flush of 2.1 GB, which `pg_basebackup` doesn't overlap with the copy.
3. **Does EXP-09's HA verdict survive, and where do v1's crossovers move?**

The ledger's fourth part, server-side zstd on HammerDB TPROC-C, moved to T4 (change 3).

It decides EXP-09's HA verdict and D03's crossover statement. It also moves D78 (crossovers against the fastest copy, not one stream) and D36 (whether one TCP stream limits a full copy at 0.1 ms). It also measures v1's path (D58, D85) in a second session, at 4 and 8 jobs. D35 (zstd on realistic data) is left to T4.

## Setup

**Machines.** Two `n2-standard-8` VMs, as in EXP-08 to EXP-10, labelled `purpose=pg-delta-benchmark`. At most two exist at any time.
- Zone `us-central1-a`, as EXP-09. `config.sh` defaults to `us-central1-b`, so the run sets `ZONE`. The zone and CPU platform go in `run.json`.
- Four local NVMe SSDs in RAID 0 at `/data`, ext4 with `startup.sh`'s options on both VMs (`mkfs.ext4` defaults, mounted `noatime`, so the journal runs in `data=ordered` mode). Debian 12.
- PostgreSQL 17 from PGDG. PGDG installs its newest minor version, which may not be EXP-09's 17.11; the version is recorded, and the drift gates below cover it.
- pgdelta at EXP-09's code. The only code change since (commit 371434c) touches the write-back mode, which v1's arms (`--fsync main`) don't use. The run records `git diff` of `pgdelta/` against EXP-09's deployed commit.

**Data.** pgbench scale 70 (2.1 GB) with EXP-01's `events` table, and uniform, hot and append workloads at 1%, EXP-01's seed. Each workload is its own trial, with its own cluster and B0, as in EXP-09. Two heap files hold most of the bytes: `pgbench_accounts` (about 0.94 GB) and `events` (about 0.8 GB, estimated from its row width). The smoke run records the real sizes.

**Manifest checksums (rule 7):** CRC32C for every `pg_basebackup`, B0 included. `run_exp04.py`'s `--manifest-checksums` still defaults to SHA256 (the setting behind D64), so the run passes CRC32C, and a new guard refuses this experiment's methods without it.

**Link speeds:** 2, 5 and 10 Gbit/s, and unthrottled (about 15 Gbit/s). EXP-09 had 2, 5 and unthrottled.
- 10 Gbit/s is added because v1's crossover against a parallel copy should lie between 5 Gbit/s and unthrottled, and log-log interpolation (D78) needs a point inside that gap.
- 2 Gbit/s keeps the uniform crossover bracketed if this session's v1 is slower than EXP-09's.
- Shaping is `infra/gcp/throttle.sh`, a tbf on both VMs' egress. One tbf caps the sum of all streams, so parallel arms share the cap.
- At every (repeat, speed) the harness runs its single-stream link test in both directions, as before. In each trial's first repeat it also runs an 8-stream test from source to dest at each speed.

**Repeats and order.** 5 repeats in each trial's main block. Within each (trial, repeat, speed) every selected method runs once, in a seeded random order. Speeds are shuffled per repeat from their own seeded stream. Trials run hot, then append, then uniform, so the two workloads the HA rule uses come first. In hot and append, two blocks follow the main block in the same trial, both unthrottled, each repeat in its own seeded random order:
- **the extension** (A9): 5 more repeats of every arm that can be a baseline there (`full`, `full_syncfs`, `probackup8`, `probackup4`, `parcopy8`, `parcopy4`) and of v1 at 4 and 8 jobs, so each has 10 unthrottled runs per trial;
- **the warm-source block** (added here): 3 repeats of `full`, `probackup8` and v1 at 4 jobs with the source's cache left warm (below).

**Order within a trial (revised).**
1. Cluster and dataset, B0, the workload and its `CHECKPOINT`, as in EXP-09.
2. The start reference digest: a clean stop, `agent.py datadir-digest`, a restart (untimed).
3. Probes: the durable write floor (dest); the cold read floor and the single-reader read (source; they read files only); the bracket probe.
4. The main block.
5. In hot and append: the extension, then the warm-source block.
6. The durable write floor again.
7. The end reference digest, before any SQL that reads a user table on the source.
8. The source's logical digest, which settles the pending `probackup*` checks.
9. EXP-01's row-level ground truth.
10. B0's full digest.

Between steps 2 and 7 no SQL reads a user table on the source. The copies read files or use the replication protocol, and the bracket probe only starts and stops backup mode.

**Cache state at the start of every run.**

| | Source | Destination |
|---|---|---|
| Page cache | `sync; echo 3 > drop_caches` once, after the untimed set-up. **Not in the warm-source block:** there the data directory without `pg_wal` is read once, untimed, instead | dropped at the same time, in every run, the warm-source block included |
| Dirty pages | about 0 after the drop's `sync`; `Dirty` and `Writeback` recorded | the same, recorded just before the clock starts and right after it stops |
| Old copy B (v1 only) | – | `cp -a --reflink=never B0 B; sync -f B` before the drop, as EXP-09: cold and already durable |
| Output directory (full copies) | – | absent; removed before the set-up, and the drop's `sync` flushes the removal |
| `vm.dirty_*` | Debian defaults, recorded | the same, except `full_wb` (see Arms) |
| Source cluster | running and idle for the live-source arms; stopped cleanly before the drop (untimed) for v1 and `parcopy*`, restarted after the run | – |

### What the clock includes

The clock is `perf_counter` on dest. Each run is **one timed phase with one counter window**, so no write-back can happen off the clock between two phases.

| Component | `pg_basebackup` arms | `probackup*` | `parcopy*` | v1 (`pgdelta_direct_inplace*`) |
|---|---|---|---|---|
| Launch | local on dest | local on dest; it opens its own SSH connections to the source, inside the clock | receiver local; sender over the harness's SSH (about 0.06 s, reported) | as `parcopy*` |
| Backup mode and fast checkpoint | in | in | **out**: the source is stopped | **out**: the source is stopped |
| Reading the source | one walsender, cold | N threads over SSH, cold | N readers, cold | 4 or 8 workers, cold |
| WAL | streamed, real bytes only | streamed (`--stream`) | the shutdown checkpoint's segment | the same |
| Manifest | CRC32C, in | none | none | none |
| Flushing | the tool's own (below) | its own per-file fsyncs, then a timed `sync -f` of the output, in the same phase | its own (below) | pgdelta's, for the pages it writes |
| Deleting an old copy first | out | out | out | n/a (in place) |
| Starting the copy | out (verification) | out | out | out |

`R_adj` (below) adds `full`'s own backup-mode time to v1 to price what the stopped-source arms leave out.

### Durability, defined

**A run is durable when, at the moment the clock stops:**
- every byte of every file the method wrote in this run has been covered by a successful `fsync(2)`, `fdatasync(2)` or `syncfs(2)` issued after that byte's last write;
- every directory the method created, or whose entries it changed, is durable by the directory rule below;
- every file the method did not write was already durable when the clock started. This applies only to v1, whose B was synced in the untimed set-up.

**The directory rule, the same for every arm (revised).** A directory change counts as durable once a successful `fsync` or `syncfs` on the same ext4 filesystem has committed a journal transaction at or after the change. With `data=ordered`, jbd2 commits transactions in order, so committing a later one commits the change too. The audit applies the rule this way: the change is covered by a later successful `syncfs` of `/data`, a later `fsync` of that directory, or a later `fsync` of a file on `/data` that the method wrote at or after the change. So no arm has to fsync the output's parent: `pg_basebackup` doesn't, and `parcopy` isn't asked to.

Early write-back (`sync_file_range`) counts only once the `fsync` after it returns. A tool's own flush counts where it is the tool's documented default: `pg_basebackup` without `--no-sync` (`--sync-method` `fsync` or `syncfs`). **`pg_probackup catchup` 2.5.16 flushes only part of what it writes.** The fairness review read its source: `src/catchup.c` fsyncs the source's file list and `pg_control`, one file at a time, and no directory. The streamed WAL is written with `do_sync = false` (`src/stream.c`), and `backup_label`, written after `pg_stop_backup`, isn't in that list. So `probackup*` end with a timed `sync -f` of the output inside the same phase, registered now. The step is reported as `probackup_sync_s`, and the arm is labelled "tool + `sync -f`".

**Checked in every run, not assumed (added here):**
- **(a) Nothing left dirty (revised).** `Dirty + Writeback` in dest's `/proc/meminfo` is read just before the clock starts and first after it stops (a local read, well under 1 ms). The increase must be at most 4 MiB, or at most the idle noise the smoke run measures, whichever is larger. One unflushed 16 MiB WAL segment fails it, and so would a v1 that skipped its fsyncs (it writes about 30 MB).
- **(b) The bytes reached the disk inside the window.** For every full-copy arm, the bytes written to dest's md array during the window (`/proc/diskstats`) must be at least 0.98 × the output's bytes.
- **(c) The flush is inside the window.** A stubbed harness test asserts that each arm's flushing step runs inside its timed phase, `probackup*`'s `sync -f` included.
- **Negative control:** `full_nosync` must fail (a) in every run, by at least 1.5 GB. If it passes, check (a) can't tell the two apart: it is void for the session, and durability rests on (b).

**Checked once per arm, in the smoke run (added here): the syscall audit.** One untimed run of every arm at scale 5 runs on dest under `strace -f -tt -y -e trace=%file,%desc`. Those two classes include every call that creates, writes, removes or flushes a file or directory (among them `rmdir`, `pwritev`, `fallocate`, `ftruncate`, `copy_file_range`, `fsync`, `fdatasync`, `syncfs` and `sync_file_range`), and `-y` with the `close` and `dup` calls lets the script map each file descriptor to its path. `strace` is added to `startup.sh`'s packages and to the preflight. A script checks each trace against the definition: every path written must be covered by a later successful `fsync`, `fdatasync` or `syncfs`, and every directory changed must be covered by the directory rule. What happens to an arm that fails is in smoke item 3.

These checks catch a tool that skips its flush. They don't prove durability: no power is cut, and whether the local SSDs honour cache flushes isn't tested. Every arm is equally exposed to that.

### Verification (untimed, every run)

| Arm | Contents | Started? |
|---|---|---|
| `pg_basebackup` arms | `pg_verifybackup` with the CRC32C manifest (WAL parsed too) | **yes, new for `full`:** started on dest (port 5433, no TCP, no archiving), recovers from `backup_label` to consistency, answers `SELECT count(*) FROM pgbench_branches`, stopped |
| `probackup*` | **Pass or fail (revised): the per-table logical digest** (`measure.logical_digest_sql`, as EXP-08 used for pgBackRest) of the started output, against the source's digest at the trial's end (step 8). **A diagnostic only:** every file the trial's reference digest selects under `base/` and `global/`, except `global/pg_control`, compared by SHA-256; extra files are listed | yes, by the tool's documented procedure, fixed in the smoke run: it must reach consistency from what the tool wrote, with no WAL fetched after the clock stops. The digest is taken then, and stays pending until step 8 |
| `parcopy*` | `verify_direct`, as for v1: every file `pgdelta.datadir` selects from the stopped source is byte-identical (SHA-256), no inode shared with B0 | yes, without recovery, then queried |
| v1, both arms | EXP-09's check: byte-identical to the selection, no in-place leftovers | yes, as EXP-09 |
| every arm | B0 unchanged (stat snapshot per run, full digest per trial) | – |

- **Reference digest (added here).** Steps 2 and 7 above: the source stopped cleanly, its selected files hashed, the source restarted, all untimed. Every comparison leaves out `global/pg_control`, `pg_wal/` and `pg_stat/`, which every clean stop rewrites (since PostgreSQL 15, each clean stop writes the cumulative statistics to `pg_stat/`). The smoke run checks that nothing else differs (item 12).
- **What a difference means (revised).**
  - **Void.** If the start and end reference digests differ, the source changed during the trial. A `probackup*` run whose logical digest then differs from the source's can't be blamed on the tool, so it is void, not failed. Void runs are excluded from B_rel and B_fast, and the HA verdict is also reported with them included, flagged. Nothing is rerun.
  - **Failed.** If the two reference digests match, a `probackup*` logical difference, or an output that doesn't start, is a failure (A4).
  - **Reported only.** A v1 or `parcopy` run's own stopped-source digest that differs from the reference is reported; that run's own verification, against the source at the time, stands. A `probackup*` output whose bytes differ from the reference is reported as a diagnostic; it voids nothing.
- **Time until usable** is recorded for every started output: `total_s` plus the start's time to consistency. A copy taken from a running server makes PostgreSQL sync the whole data directory when it first starts (from PostgreSQL's startup code; not measured). So `--no-sync` moves the flush to the standby's start rather than removing it, and this column shows by how much.
- EXP-01's row-level ground truth still runs on the source at the end of every trial, now after the end reference digest and the logical digest (step 9).

### Measured

**In every run:** `total_s`; `total_no_ssh_s`; wire bytes in each direction at the interface; VM-wide busy CPU on both VMs; per-process rusage and peak RSS; dest's disk bytes read and written, and `Dirty`/`Writeback` before and after; the block (main, extension, warm source) and the source's cache state; and phase timers (rule 6):
- `pg_basebackup` arms run with `--verbose` (never `--progress`, which adds a sizing pass). The orchestrator timestamps each stderr line as it arrives, through a pipe read by a thread, with no extra process inside the clock. The lines split each run into checkpoint, stream, WAL end, sync and manifest rename. **The backup-mode adder** (added here; used by `R_adj`) is read from them: from "initiating base backup, waiting for checkpoint" to "checkpoint completed", plus from "write-ahead log end point" to "syncing data to disk".
- `pg_probackup`: its own timestamped log lines (copy, WAL stream stop, sync), and `probackup_sync_s` for the timed `sync -f`.
- `parcopy`: its own JSON timers per stream: bytes, first and last byte, write time, fsync tail, and the number of `fsync` calls. That number must equal the inventory's files plus its directories plus one for the output's top directory.
- v1: pgdelta's own timers, as before.

**Once per trial, untimed (added here):**

| Probe | What | Expected |
|---|---|---|
| durable write floor (dest) | 4 writers write as many bytes as B0 holds, buffered, write-back every 8 MiB, then `fsync` each; at the trial's start and end | 1.25–1.6 s |
| cold read floor (source) | 4 readers of the data directory without `pg_wal`, as EXP-07's C4 | 0.65–0.9 s |
| single-reader cold read (source; added) | one reader of `pgbench_accounts`'s file, the largest relation, after a drop | 1.2–2.3 GB/s, compared with `full`'s stream-phase rate (about 1.3 GB/s) |
| multi-stream link | 8 streams, source to dest, at each speed, first repeat | 14.5–16 Gbit/s unthrottled (egress cap 16 Gbps) |
| backup-mode bracket (a cross-check; revised) | 10 × (drop the source's cache; `pg_backup_start('probe', true)`; `pg_backup_stop(false)`) on the idle source; the first pair reported separately | median 0.03–0.20 s; not used in any ratio |

**floor(U) (revised)** is the median of the four durable-write measurements in the hot and append trials (each trial's start and end). The cold read floor and the 8-stream link time are reported beside it; unthrottled, both should be well below it. At a throttled speed s, floor(s) = max(link time at the 8-stream rate, floor(U), cold read floor).

## Arms

"U" is unthrottled. Every arm runs 5 repeats in each trial's main block. In hot and append, "+5" marks the extension's 5 more unthrottled repeats in the same trial, and "warm 3" the warm-source block.

| Arm | Tier | What runs | Source | Durable by | Speeds |
|---|---|---|---|---|---|
| `full` | Omni today | `pg_basebackup -Fp -X stream -c fast --manifest-checksums CRC32C --verbose`, one stream, on dest | running, idle | its default `--sync-method=fsync`: starts write-back of every file, then fsyncs every file and directory | 2, 5, 10, U; +5; warm 3 |
| `full_nosync` | diagnostic | `full` with `--no-sync` | running, idle | **not durable**; never a baseline. The ledger's arm, and the audit's negative control | 2, U |
| `full_syncfs` | released | `full` with `--sync-method=syncfs` (PostgreSQL 17) | running, idle | `syncfs` of dest's `/data` | 10, U; +5 |
| `full_wb` | host-tuned | `full` with dest's `vm.dirty_background_bytes` = 64 MiB, so the kernel writes back while the stream arrives. Set before the cache drop. Reset in `run_method`'s `finally` by writing `vm.dirty_background_ratio` back to its recorded default (10 on Debian 12), which returns the bytes setting to 0; Linux refuses 0 written to the bytes setting directly. Before every run a guard requires both the ratio at its default and the bytes setting at 0. Both values recorded per run | running, idle | as `full` | 10, U |
| `probackup8`, `probackup4` | released | `pg_probackup catchup -b FULL --stream --temp-slot -j 8` (or `-j 4`), on dest, reading the source over SSH (below), then `sync -f` of the output in the same phase | running, idle | **tool + `sync -f`:** its own per-file fsyncs, then `syncfs` of `/data` (`probackup_sync_s`) | `-j 8`: 2, 5, 10, U; +5; warm 3. `-j 4`: U; +5 |
| `parcopy8`, `parcopy4` | bound | a parallel copier built in the harness (below), 8 or 4 TCP streams | stopped cleanly, as v1 | write-back every 8 MiB, then a parallel `fsync` of every file, then every directory bottom-up, the output's top directory included | 8: 2, 5, 10, U; +5. 4: U; +5 |
| `pgdelta_direct_inplace` | reference (v1) | EXP-09's arm unchanged: 4 jobs on each end, `--source-kind datadir`, `--in-place`, `--fsync main`, `--prefetch-basis on` | stopped cleanly | pgdelta's in-place commit; B's untouched files durable before the drop | 2, 5, 10, U; +5; warm 3 |
| `pgdelta_direct_inplace_j8` | v1 at 8 jobs (added) | the same with `--jobs 8` on both ends | stopped cleanly | as v1 | U, hot and append only; +5 |

**The tiers.** *Released* means an operator could adopt it today through a released tool's documented options, with no new code. *Host-tuned* is a released arm with one kernel setting changed on dest. *Bound* means built here from standard primitives; no operator can run it as it stands.

**`pg_probackup catchup -b FULL`** is the only released tool found that makes a parallel full copy from a live server into an empty directory in one hop, with WAL streaming and no repository. `install_incumbents.sh` installs it (2.5.16 on October 23) and must run on both VMs, since its remote mode needs the binary on the source; `remote.py provision` is extended to run it. Its SSH configuration is the harness's key with `IdentitiesOnly`, `BatchMode`, no connection sharing, and `Ciphers aes128-gcm@openssh.com`, the fastest cipher with AES-NI and a documented setting an operator would pick. It parallelises by file, so the largest file, about 0.94 GB, sits on one thread. Its own flush is incomplete (see Durability), so its time includes a `sync -f`.

**`parcopy`** is built to be hard to beat, so that it shows what parallelism can do on this hardware:
- It calls `pgdelta.datadir.select()` directly on the stopped source and copies exactly that selection, v1's. Before copying, it recreates every directory of the inventory with its recorded mode, the output's top directory with the data directory's mode, and the empty directories `select()` keeps (`pg_replslot`, `pg_notify`, `pg_wal/archive_status` and the others). Each file is read from its listed source, so an unlogged relation's main fork gets its init fork's bytes.
- It cuts files into ranges of at most 64 MiB and assigns them largest first to the stream with the fewest bytes. Range splitting matters here: by file, one stream would carry about 0.94 GB of 2.1 GB.
- The source sends with `os.sendfile`. Dest writes with `os.pwrite` and starts write-back every 8 MiB with `pgdelta.flush.start_writeback`, EXP-10's fixed write-back.
- After the last range: a parallel `fsync` of every file, then every directory bottom-up, the output's top directory last. The expected `fsync` count comes from the inventory: its files, plus its directories, plus one.
- It is harness code, standard library only, never a product engine, so owner decision O7 doesn't apply.

It reads a stopped source for two reasons. v1's reference arm does too, so the two compare like for like and share `verify_direct`. And a backup-mode version is T8's to build, as v1's live baseline. `R_adj` prices the difference.

**v1 at 8 jobs (added here).** EXP-09 found v1's destination pool of 4 workers nearly saturated. The baselines get the better of 4 and 8 streams, so v1 gets 8 jobs too, unthrottled, in hot and append. **The HA verdict stays on the 4-job arm,** the one EXP-09's rule names, so v1's arm isn't chosen on the data it is judged on. The same verdict with v1 at 8 jobs is reported beside it, and if the two differ, both are stated. The 4-job arm is also EXP-09's drift-gated reference and the one every crossover uses.

### Considered and not run

| Option | Why not |
|---|---|
| pgBackRest full backup then restore (`process-max` 4 or 8) | Two hops, so two full writes of 2.1 GB. It needs archiving, which touches other methods' numbers (EXP-08), so it would need its own invocation and a bridge. Parallel by file, like `pg_probackup`. T11b measures pgBackRest from an existing repository, which is Omni's realistic case |
| Barman `rsync` with `parallel_jobs`, WAL-G `backup-push`/`backup-fetch` | Two hops through a catalog or object storage; not in Omni's toolset; parallel by file |
| Several concurrent `pg_basebackup`s | Each copies everything; there is no option to copy a subset of files |
| `pg_basebackup -Ft`, or tar to `/dev/null` | A standby needs plain files. The `--verbose` timers and `full_nosync` already split the stream from the flush |
| `--no-verify-checksums`, `--manifest-checksums NONE` | EXP-07: 2% or less. Operators keep manifests |
| `parcopy` in backup mode on the live source | T8, as v1's live baseline |
| The low-level backup API plus 8 `rsync` streams | Per file, like `pg_probackup`, so it adds nothing to it; `parcopy` is the bound |
| `pg_probackup` and v1 on the tuned host | Budget. v1 writes about 30 MB for local changes, so it has nothing to gain |
| v1 settings other than its job count | v1 runs EXP-09's engine; T5 and T13 change it |
| Server-side zstd and lz4 on TPROC-C | Moved to T4, with L5 and its rule (change 3) |
| TLS, added latency | T12 |
| Persistent disks, snapshot clones, beyond RAM | T9 |

## Baselines and ratios (added here)

At each speed s, over the arms run there on the default host that pass every verification and durability check, without void runs:
- **B_fast(s)**, rule 3's "fastest durable full copy measured": `full`, `full_syncfs`, `probackup*` and `parcopy*`.
- **B_rel(s)**, the released envelope: the same without `parcopy*`.
- **B_tuned(s):** B_fast with `full_wb` admitted.
- **F0(s):** `full`, what Omni runs today.

**Choosing the baseline arm.** The arm is chosen by the lowest median over the main block's runs at that speed, pooled over the three workloads (15 per arm), since a full copy's work doesn't depend on the workload and pooling reduces the bias of picking the fastest of several noisy medians. Ties within 0.05 s go to the lower pooled maximum. The extension's and the warm-source block's runs don't enter the choice, so every arm is chosen on the same count. An arm run only at some speeds defines an envelope only there; a crossover that depends on such an arm's absence is marked provisional.

**Comparing.** Comparisons are per workload, against the chosen arm's median over its runs in that trial at that speed, outside the warm-source block. Unthrottled in hot and append, that is 10 runs for every arm that can be in B_fast or B_rel and for v1, and 5 for `full_nosync` and `full_wb`. The HA verdict, its ratios and A3 use these. Every other prediction and rule uses the main block's 5 runs per cell.

**v1's arm (revised).** The HA verdict and the ratios below use v1 at 4 jobs, EXP-09's arm. Each is also given with v1 at 8 jobs, unthrottled in hot and append, beside it. Crossovers use the 4-job arm, the only one run at every speed.

**Ratios**, unthrottled, per workload, v1's median over the baseline's median:
- `R_fast`, `R_rel`, `R_tuned`, `R_F0`.
- `R_adj` (revised): v1 plus the backup-mode adder, over a live-source baseline. The adder is `full`'s own checkpoint wait plus its wait from the WAL end point to the start of its sync, from its `--verbose` timestamps. Per workload it is the median over `full`'s unthrottled runs in that trial (10); for `R_paired`, the same block's `full` run. Against a stopped-source baseline (`parcopy`), `R_adj` equals `R`. The adder still leaves out what a live v1 would also pay: streaming WAL, writing `backup_label`, and recovery when the copy starts. The bracket probe is reported beside it as a cross-check.
- Robustness views: `R_max` (maxima), `R_noSSH` (`total_no_ssh_s`), and `R_paired` (the median over the 10 repeats of v1's run over the baseline's run in the same (repeat, speed) block, minutes apart).
- **Matched parallelism** (added; not used for the verdict): every ratio above (`R_fast`, `R_rel`, `R_adj`, `R_tuned`, `R_max`, `R_noSSH`, `R_paired`) is also given with v1 at 4 jobs over the fastest of `full`, `full_syncfs`, `probackup4` and `parcopy4` (and over the same without `parcopy4` for the released readings), chosen as the envelopes are.
- **v1 at 8 jobs** (added; not used for the verdict): the same ratios with v1 at 8 jobs against the same envelopes.
- **Warm source** (added; not used for the verdict): v1 at 4 jobs over `full` and over `probackup8`, each on its warm-source runs (3), beside the same ratios cold.

**Crossovers:** EXP-04b's method (D78), `analysis/exp04.crossover` generalised to take a baseline. Log of the ratio against log of the speed, interpolated between the two speeds that bracket 1. The x-coordinate is this session's median single-stream measured rate at each speed. Reported for v1 at 4 jobs per workload against B_fast, B_rel, B_tuned and F0, and against the pure link time of a full copy's bytes, labelled as a model. v0's crossovers (L3, L4) use only EXP-09's speeds (2, 5 Gbit/s and unthrottled).

**Above link time:** `total_s` minus the source-to-dest wire bytes divided by the measured rate: the single-stream rate for single-stream arms, the 8-stream rate for parallel ones.

## Predictions and decision rules

Every prediction and rule is judged per workload on the main block's medians of 5 runs, with the maximum reported beside it (rule 6), unless it says otherwise. The HA verdict, its ratios and A3 use 10 runs per extended arm (A9).

### The ledger's predictions (registered)

| ID | Ledger prediction | How it is judged here | Expected here (added) |
|---|---|---|---|
| L1 | `--no-sync` removes most of the 1.39-1.55 s the copy spends above link time. | At 2 Gbit/s: `full` − `full_nosync` is more than half of `full`'s time above link time, in every workload. Reported unthrottled too. **Whenever L1 fails** (revised), the phase timers (checkpoint, stream, WAL end, sync) must say where the time goes before any parallel gain is credited to overlapping the flush, whether or not the 0.5 s rule below fires | holds: saves 1.1–1.5 s at both speeds |
| L2 | A durable parallel copy takes 2.0-3.0 s unthrottled. | The faster parallel arm's median, in every workload. Judged twice: **(i)** released (`probackup*`), **(ii)** any (`parcopy*` included) | (i) 2.6–3.8 s; (ii) 1.6–2.3 s, so (ii) may fail low |
| L3 | v0's crossovers fall by at most about 20%. | v0 isn't re-run (change 4). EXP-09's staged `pgdelta` medians against this session's B_fast, and against this session's F0, **interpolated only over EXP-09's speeds (2, 5 Gbit/s and unthrottled)**, with this session's baselines at those speeds and this session's measured single-stream rate as x (revised). Holds if the first crossover is at least 0.8 × the second, in every workload. A crossover below 2 Gbit/s is reported as "below 2" and judged only where 0.8 × the F0 crossover is 2 Gbit/s or more. Judged only if the drift gates hold | holds, near the edge: local about 3.0 → 2.4–2.6 Gbit/s |
| L4 | v1's transfer-only crossovers fall to about 3-4.5 Gbit/s. | **As registered,** "transfer-only" is staged v0's transfer phase, the proxy D03's gray area used before v1's path existed: EXP-09's `transfer_s` medians (local 3.94–3.98 s at every speed; uniform 5.79, 4.46, 4.44 s) against this session's B_fast at 2, 5 Gbit/s and unthrottled, as for L3, within [3, 4.5]. Drift-gated. **Reported beside it:** the measured v1 path's crossover (4 jobs), which now supersedes the proxy (E10) | holds for uniform (3.5–4.5); fails high for local (4.5–6.5) |
| L5 | zstd shrinks TPROC-C by at most 1.6 times. | **Not judged here.** Moved to T4 with its rule (change 3) | – |

### The ledger's rules (registered)

| Rule | How it is judged here |
|---|---|
| `--no-sync` saves under 0.5 s: the overhead lies elsewhere. | At 2 Gbit/s, and separately unthrottled. If it fires, the phase timers (checkpoint, stream, WAL end) must say where the time goes before any parallel gain is credited to overlapping the flush. The same requirement now applies whenever L1 fails |
| A parallel copy takes 3.5 s or more: the single stream was representative. | For (i) and (ii) of L2 separately. For (ii) it may fire only if `parcopy` passes its gate (A3); with the expected floor, a `parcopy` at 3.5 s fails the gate by construction. Otherwise `parcopy` is under-built: (ii) is "not judged", `parcopy` stays in B_fast as A3 says, and a fixed version waits for a later session; nothing is fixed and rerun in this one (A9). If only (i) fires, D03 says: "the single stream represents the released parallel tools at this data shape, not a range-parallel copier". **If (i) lands at 3.0–3.5 s** (L2 (i) fails, but the rule doesn't fire; added), D03 says: "the released parallel copy tested here took x s against one stream's y s at 2.1 GB on these VMs; per-file parallelism gains little at this data shape" |
| zstd gives 2 times or more: compression must be added to both sides before claiming slow-link wins. | **Not judged here.** Moved to T4 with L5 (change 3) |

### EXP-09's HA rule, re-judged (thresholds registered in EXP-09; the reading against B_fast is rule 3's)

EXP-09 registered: within reach at most 0.9 times the full copy, unthrottled, for both hot and append; out at 1.0 or more for hot or append; undecided between, "decided after T2 and T5". Every HA conclusion was provisional until T2. Rule 3 makes the full copy here the fastest durable full copy measured, B_fast. The verdict is judged on unthrottled medians of 10 runs per arm (A9), with v1 at 4 jobs; the verdict with v1 at 8 jobs is reported beside it. The rows are checked in this order:

| Verdict | Condition | What follows |
|---|---|---|
| **Out on fast links** (revised) | `R_fast` ≥ 1.00 for hot or append | Reported as "Out against the fastest copy measured; R_rel = x", for each of hot and append. v1 targets DR's slower links first (EXP-09's rule). D03 stands: no claim to beat a parallel full copy on a fast LAN, now with a measured crossover. **Qualifier:** if `R_rel` ≤ 0.90 and `R_adj` ≤ 0.90 against B_rel, for both hot and append, the verdict adds: "v1 is faster than the released copiers tested here (`pg_basebackup` with fsync or syncfs, `pg_probackup catchup -j 4` and `-j 8`) at 2.1 GB on these VMs". D03 then records that a parallel copier built with the effort v1 takes (O3 allows a program on the source) matches or beats v1 on fast LANs. T5 and T13 get the target below |
| **Confirmed** | `R_fast` ≤ 0.90 for both hot and append, `R_adj` ≤ 0.90 against both B_fast and B_rel, and `parcopy` passes its gate (A3) | v1 is within reach for Omni's same-region HA rebuild against any copier on these VMs. T8, T9 and persistent disks next, as EXP-09 said |
| **Bound missing** (added) | `parcopy` wasn't built, was excluded (A4, A5), or fails its gate (A3) while slower than B_rel, so the bound isn't measured; neither row above applies; and `R_rel` ≤ 0.90 and `R_adj` ≤ 0.90 against B_rel, for both hot and append | "Within reach of the released copiers tested here (`pg_basebackup` with fsync or syncfs, `pg_probackup catchup -j 4` and `-j 8`) at 2.1 GB on these VMs; bound not measured." The HA verdict stays provisional until a session reruns it with the bound |
| **Undecided** | anything else | Decided after T5 and T13. EXP-09 named T2 and T5; T2 is this test, and EXP-09's results put T13 beside T5 |
| *Fragile* (a flag) | `R_max`, `R_noSSH` or `R_paired` would put hot or append in another row | Reported as fragile beside the verdict |
| *Near the line* (a flag) | a deciding ratio within ±0.03 of 0.90 or 1.00 for hot or append | Reported beside the verdict; the row stands (A9) |
| *With void runs* (a flag) | a trial has void `probackup*` runs | The verdict is also given with them included, flagged |

**By arithmetic, Confirmed is out of reach unless something surprises.** With v1 at 2.70 s it needs B_fast of 3.00 s or more. The bound's gate allows that only if floor(U) is 2.0 s or more, and the probes expect 1.25–1.6 s. The reading with v1 at 8 jobs would need, at E14's best of 2.3 s, B_fast of 2.56 s or more and floor(U) of 1.56 s or more, the top of that range. So the live outcomes are Out, with or without the qualifier, and Undecided. It is written down now, before running.

**What a verdict short of Confirmed asks of T5 and T13:** the cut needed is v1 − 0.9 × B_fast in hot and append, with the cut against B_rel beside it.
- Up to 0.6 s: T13 first. Verifying while writing removes up to the 0.62 s EXP-09's main loop waited for the re-read.
- 0.6–1.2 s: T13 and T5, ranked equal.
- More than 1.2 s, or a target under 1.25 s: v1's design can't reach the line on these VMs even with both, since reading B cold once takes about 0.95 s plus about 0.3 s of start-up. The ledger says so.

### Added predictions

| ID | Prediction | Basis |
|---|---|---|
| E1 | `full` reproduces EXP-09 within the drift gates: 9.76–10.79 s at 2 Gbit/s, 4.78–5.28 s at 5, 2.82–3.46 s unthrottled. At 10 Gbit/s: 3.1–3.6 s | EXP-09; at 10 Gbit/s about 0.2 s start + 1.8 s stream + 1.3 s flush |
| E2 | v1 at 4 jobs reproduces EXP-09 within 10%: hot and append 2.43–2.98 s at every speed; uniform 4.44–5.43, 3.09–3.77 and 2.85–3.48 s at 2, 5 and unthrottled; 2.9–3.6 s at 10 | EXP-09 |
| E3 | In `full`, the sync phase takes 1.0–1.6 s at every speed. `full_nosync` saves 1.1–1.5 s at 2 Gbit/s and unthrottled. Its time until usable is within 0.3 s of `full`'s | 2.1 GB stays under dest's background write-back threshold (10% of about 30 GB), so nothing is written back until the final flush, about 1.3 s at 1.6 GB/s; the start then flushes what `--no-sync` left |
| E4 | `full_syncfs` is within ±0.2 s of `full` | both flush the same 2.1 GB; `pg_basebackup`'s fsync method already starts write-back of every file first |
| E5 | `full_wb` is 0.7–1.2 s faster than `full`: 1.9–2.5 s unthrottled, 2.2–2.8 s at 10 Gbit/s | the kernel writes back while the stream arrives, so little is left for the end |
| E6 (revised) | The faster `parcopy` arm: 1.6–2.3 s unthrottled, 2.1–2.6 s at 10 Gbit/s, 3.8–4.2 at 5, 9.1–9.5 at 2. `parcopy8` within ±0.15 s of `parcopy4` unthrottled. At most 1.0 s above floor(U), so A3 passes | floor about 1.3 s plus 0.3–0.8 s of process start, SSH launch and fsync tail |
| E7 | `probackup8` takes 2.6–3.8 s unthrottled and 9.6–10.6 s at 2 Gbit/s; `probackup4` within 0.2 s of it. Its `probackup_sync_s` is under 0.2 s (added) | the largest file, on one thread at about 0.5–1 GB/s through SSH, plus an unoverlapped flush; the `sync -f` finds only the streamed WAL, `backup_label` and directory changes left |
| E8 (revised) | Probes: durable write floor 1.25–1.6 s; cold read (4 readers) 0.65–0.9 s; one reader of `pgbench_accounts` 1.2–2.3 GB/s; 8-stream link 14.5–16 Gbit/s unthrottled. `full`'s backup-mode adder 0.05–0.30 s; the bracket's median 0.03–0.20 s, its first pair slower | `dd` writes 1.6 GB/s and reads 2.17–2.23 GB/s on these VMs (EXP-09); EXP-07's C4 2.87 GB/s; `full`'s walsender is itself one cold reader at about 1.3 GB/s, a lower bound for the single reader; the 16 Gbps egress cap; an idle source's fast checkpoint has almost nothing to write |
| E9 (revised) | For hot and append, with v1 at 4 jobs: `R_fast` 1.15–1.65; `R_rel` 0.85–1.05; `R_F0` 0.78–0.95; `R_tuned` at least `R_fast`, and `full_wb` alone gives 1.1–1.4. With v1 at 8 jobs (reported beside, E14), each ratio falls by up to 15%. **Expected verdict: Out on fast links,** against `parcopy`, with `R_rel` near 0.90 beside it, so the qualifier may go either way. Without the bound: Out, the "bound missing" row or Undecided, with `R_adj` close to the 0.90 line (EXP-09's 0.86 plus the adder) | E2, E5–E7, E14 |
| E10 | v1's crossovers (4 jobs) against B_fast: 6–9.5 Gbit/s for local changes, 4.5–7.5 for uniform. Against B_rel: above 10 Gbit/s or none for local, 7 Gbit/s or more or none for uniform. Against F0: none for local; uniform ties unthrottled (0.95–1.05). The link-only bound for local changes is about 6.3 Gbit/s | E2, E6, E7; v1 local is flat at 2.70 s |
| E11 | VM-wide CPU per run, source / dest: `full` arms 1.6–2.0 / 1.7–2.1; `parcopy` 0.8–2.5 / 2–5; `probackup` 3–8 / 3–8; v1 at 4 jobs 5.3–6.0 / 9–11; v1 at 8 jobs more on both, 10–15 on dest (added) | EXP-09; `sendfile` is kernel time; SSH encryption costs both ends; E14 |
| E12 | *Removed:* it was TPROC-C's zstd timing, which moved to T4 (change 3) | – |
| E13 (revised) | Every durable arm passes checks (a) and (b) in every run, and the syscall audit; `full_nosync`'s increase is at least 1.5 GB in every run. The audit finds that `pg_probackup`'s own flush leaves the streamed WAL, `backup_label` and its directories uncovered, and that its `sync -f` covers them | the definition above; `pg_probackup`'s source, as the fairness review read it |
| E14 (added) | v1 at 8 jobs: 2.3–2.8 s unthrottled for hot and append, from 0.4 s faster to 0.1 s slower than at 4 jobs | EXP-09: the dest pool of 4 is v1's bottleneck (about 9 CPU-s in a 2.55 s transfer). EXP-06's Linux check (recorded in `pgdelta/cli.py`): 8 processes hashed 16% faster than 4, for 1.65× the CPU per pass. 16% of the pool's roughly 2.4 s is about 0.4 s at best; the main loop and the kernel share the 8 vCPUs and may take the gain back |
| E15 (added) | With a warm source: `full` and `probackup8` 0–0.5 s faster than their cold medians, v1 0–0.15 s. So the warm-source ratios are up to about 0.17 above the cold ones against `full` and up to about 0.25 against `probackup8` (v1 / (B − Δ) − v1 / B) | If one stream is bound by its walsender's cold read (about 1.3 GB/s), a warm read leaves it bound by the link (about 1.9 GB/s), saving up to 0.5 s of its 1.6 s stream. v1 is bound by dest's pool (EXP-09); the source's cold read is off its critical path |

**How E5–E10 and E14 were derived** (all to be checked):
- One stream unthrottled ≈ 0.2 s start and checkpoint + about 1.6 s streaming (about 1.3 GB/s) + about 1.3 s flush that doesn't overlap = 3.1 s.
- A parallel copy can shorten the streaming, but not that flush, unless write-back starts early. So a per-file tool without early write-back stays near 2.6 s or more, while `parcopy` and `full_wb` approach the floor.
- A link-bound copy takes about 17.7 / L seconds of link time at a nominal L Gbit/s (2.12 GB, tbf at about 96%). `parcopy(L)` ≈ 17.7 / L + 0.3–0.6 s. v1 local is flat at 2.70 s; v1 uniform fits about 5.08 / L + 2.40 s. Setting them equal gives the crossovers in E10.
- v1 at 8 jobs gains only what its pool gains, at most about 16% of the pool's time (E14).

### Added rules

| ID | Condition | Consequence |
|---|---|---|
| A1 | `full_syncfs` is 0.3 s or more faster than `full` unthrottled, in every workload | The fastest shipped copy is `full_syncfs`, a one-flag change for Omni. "Against what Omni runs today" is restated against it too |
| A2 | `full_wb` is within 0.15 s of B_fast unthrottled, or the HA verdict differs between B_tuned and B_fast | The HA claim must name the destination's write-back setting. The default host's verdict stands. D03 gets a gray area and the integrator a question: does Omni's node configuration set `vm.dirty_*`? |
| A3 (revised twice) | **The bound's gate.** The faster `parcopy` arm's median over its pooled unthrottled runs in hot and append (20) is more than floor(U) + 1.0 s, or its busiest stream carries more than 1.1 × the mean bytes | The bound fails its gate: it is suspect, and its timers must explain the gap in the write-up. **Confirmed is blocked.** If `parcopy` is still B_fast, it stays there for Out and for the crossovers, since a slow bound only favours v1, and crossovers against it are labelled upper bounds: a faster copy would cross lower. If a released arm is faster, the "bound missing" row applies |
| A4 (revised) | Any `parcopy` run fails `verify_direct` or doesn't start; or any `probackup` run fails the logical standard: its output doesn't start, or its logical digest differs from the source's while the trial's two reference digests match | That arm is excluded from every baseline, not just the cell. Confirmed can't be declared without a bound that passes A3. A void run isn't a failure, and byte differences fail nothing |
| A5 (revised) | A durable arm fails check (a) or (b), or a `parcopy` run's `fsync` count differs from the inventory's files plus directories plus one | The run is excluded and reported. If more than 1 of an arm's runs at one speed in one trial fail, the arm is reclassified as not durable for the session and left out of every baseline, recorded under rule 8. Nothing is rerun in this session |
| A6 | `full_nosync` passes check (a) in any run | Check (a) is void for the session; durability rests on (b) |
| A7 | **Drift gates** against EXP-09 (`docs/experiments/data/exp09-10-cells.csv`): `full` within ±5% at 2 and 5 Gbit/s and ±10% unthrottled; v1 at 4 jobs within ±10%; measured link within ±2% at 2 and 5 Gbit/s and ±5% unthrottled; wire bytes within ±1%. Any gate fails | The session is marked shifted. In-session verdicts, the HA verdict among them, stand. L3 and L4, which use EXP-09's v0 medians, are "not judged" |
| A8 | *Removed* with the TPROC-C invocation (change 3) | – |
| A9 (revised) | **The extension, unconditional.** In hot and append, after the main block and before the trial ends, every arm that can be in B_fast or B_rel (`full`, `full_syncfs`, `probackup8`, `probackup4`, `parcopy8`, `parcopy4`) and v1 at 4 and 8 jobs run 5 more unthrottled repeats | The HA verdict, its ratios and A3 are judged on all 10 runs. A deciding ratio within ±0.03 of 0.90 or 1.00 is flagged "near the line"; the row stands. Once the main sweep starts, no arm is fixed and rerun in this session; an arm that fails is left out and recorded under rule 8 |

## Notes written before running

- **Why two baselines.** Rule 3's baseline is B_fast, and the verdict is judged against it. B_rel is always reported beside it, so a reader can see how much of a verdict against v1 rests on a copier nobody ships. A verdict for v1 (Confirmed) needs a bound that passes its gate (A3, A4), so it can't be dismissed as a comparison with a weak copy.
- **Why the parallel arms are `pg_probackup` and `parcopy`.** No other released tool was found that copies in parallel from a live server into an empty directory without a repository. Real tools split by file, and at 2.1 GB two heap files hold most of the bytes, so `pg_probackup` shows what Omni could deploy at this shape, and `parcopy` shows what the hardware allows.
- **`full_nosync` is never a baseline.** It isn't durable, and PostgreSQL flushes what it left when the copy first starts (E3, time until usable).
- **`--verbose` changes `full`'s command line from EXP-09's,** and its stderr now goes through a pipe. Both are recorded; the drift gate covers them.
- **Session drift.** The full copy moved by 22% between EXP-04b's and EXP-09's sessions on the same machine type. So no ratio here mixes sessions, except L3 and L4, which A7 gates.
- **Order effects.** Full copies that follow a v1 or `parcopy` run, which restarted the source, are compared with the others. The extension's runs are compared with the same arms' main-block runs. A difference over 0.1 s in the median is reported.
- **No fix and rerun in this session (added).** Fixes found in the smoke run are made before the main sweep and recorded here, dated. Once the main sweep starts, nothing is fixed and rerun after its results are seen.
- **"VM hours"** means the pair's wall-clock time, as EXP-08 and the queue read it. That is about twice as many instance-hours.
- **Before the main sweep,** the smoke run settles the open items below, and they are recorded here, dated.

**To settle in the smoke run:**
1. `pg_probackup catchup -b FULL`: the flags are accepted; how many SSH connections it opens; how its output starts; its `sync -f` runs inside the phase.
2. `pg_basebackup`: `--sync-method` is listed.
3. The syscall audit: one untimed run of every arm (10) at scale 5, every arm passing. An arm that fails is fixed before the main sweep if it is harness code (`parcopy`, the `pg_probackup` wrapper). A released tool or pgdelta that fails gets a timed `sync -f`, like `probackup*`, recorded here, dated, under rule 8.
4. The 8-stream link rate unthrottled and at 10 Gbit/s.
5. Checks (a) and (b): the idle noise of `Dirty + Writeback` on dest (the largest increase over 20 idle windows of 5 s), which sets check (a)'s limit with the 4 MiB and goes into the run lines' `NOISE`, in MiB; `full` within that limit at scale 5; `full_nosync` leaving most of its copy dirty; check (b) passing.
6. `parcopy`: `verify_direct` passes, its output starts, streams are balanced, and the `fsync` count equals the inventory's files plus directories plus one.
7. The timestamped `pg_basebackup` phases parse, the adder's two intervals included.
8. `full_wb`'s reset after a forced exception: the guard then sees the ratio at its default and the bytes setting at 0.
9. The extension and the warm-source block run, at 1 repeat each.
10. The time projection: one scale-70 unthrottled pass of the hot trial with every arm (10 runs), with the fixed per-run overheads from the scale-5 runs, projected to the full session. This decides the cuts below.
11. `analysis/exp11.py` runs end to end on the smoke CSV.
12. The reference digests: two taken with three stop and start cycles and one `full` run between them must match after the exclusions. Any other file that differs is added to the exclusions here, dated, before the main sweep.

## Changes from the ledger (rule 8)

Each becomes a ledger row with its own grade when this is registered.

1. **"4 and 8 parallel flushed streams" become two kinds of arm.** Released: `pg_probackup catchup -b FULL -j 4` and `-j 8` (live source, one hop, by file, SSH, ending with a timed `sync -f`). Bound: `parcopy4` and `parcopy8` (harness-built, stopped source like v1, 64 MiB ranges, early write-back). Why: the ledger's arm names no tool. A released tool answers "what can Omni deploy"; a range-parallel copier answers "how fast can a copy go", which per-file tools can't show at this data shape. Grade B.
2. **Rule 3 is read as written, with the released envelope reported beside it.** B_fast includes the bound, which reads a stopped source as v1's arm does. EXP-09's HA rule keeps its thresholds and is judged against B_fast: Out fires on `R_fast` ≥ 1.00, as EXP-09 registered against the fastest durable copy. `R_rel` is a qualifier beside the verdict, never a verdict of its own; it decides a row only when the bound is missing. The alternative, judging Out against released tools only, would move the comparison in v1's favour after EXP-09 registered the expectation that T2 reverses it. Grade C: a value judgment.
3. **The zstd part moves to T4.** The ledger's T2 includes server-side zstd on HammerDB TPROC-C, with L5 ("zstd shrinks TPROC-C by at most 1.6 times") and the rule "zstd gives 2 times or more". EXP-11 drops them: no `full_zstd` arm, no TPROC-C trial, no HammerDB build. L5 and its rule are judged in T4, as registered, from offline ratios. Why: this part answers a bytes question with a different dataset and needs a HammerDB build, while EXP-11's core question is the HA verdict. T4 already carries the TPROC-C zstd measurement (ledger note of October 23). A timed compressed copy waits for a session that runs v1 on TPROC-C pairs too, since a time ratio across datasets isn't like for like. Grade B.
4. **v0 isn't re-run.** L3 and L4 are judged from EXP-09's v0 medians against this session's baselines, only if the drift gates hold. v0 is interpolated only over EXP-09's speeds (2, 5 Gbit/s and unthrottled), with this session's baselines at those speeds and this session's measured single-stream rate as x. v0's staged path is graded X (D10p), O9 dropped staged-only work, and its runs would cost about 15 minutes. L4's "transfer-only" is kept as registered, and v1's measured crossover is reported beside it. Grade B.
5. **Added:** 10 Gbit/s; `full_syncfs`; the host-tuned `full_wb`; v1 in the same session, at 4 jobs (EXP-09's arm, which the HA verdict keeps) and at 8 (reported beside it); the probes; the durability checks and the syscall audit; `pg_probackup`'s timed `sync -f`; starting every full copy (EXP-09 only ran `pg_verifybackup` on them); the in-trial extension to 10 unthrottled runs; the warm-source block.
6. **Cost.** The ledger budgeted 1 day and about 2 VM hours. This needs about 2.75 days of harness work and about 3.35 hours of the pair (2.95–3.65; below). The lead accepted that cost; the cut line is 3.5 hours.

## Revisions before running (October 24, 2026)

Two independent reviewers checked the first version before anything ran: one for feasibility, one for fairness and validity. The project lead decided each finding. Below, "feasibility n" and "fairness n" are the reviews' findings in their order. Changes, with the finding each answers:

**The verdict and the baselines**
- **Out is judged against the fastest copy measured** (feasibility 1). Out now fires on `R_fast` ≥ 1.00 for hot or append, as EXP-09 registered against the fastest durable copy (rule 3). The first version required `R_rel` ≥ 1.00, which judged Out against released tools only, in v1's favour. "Within reach against released tools only" is no longer a verdict; the released-envelope result is a qualifier beside Out. Change 2's wording now matches the table.
- **The floor gate fits E6 and says what follows** (feasibility 3). A3 is now floor(U) + 1.0 s; at 0.8 s, E6's own range would have fired it. floor(U) is the median of the four durable-write measurements in hot and append, and A3 is judged on pooled unthrottled runs. When it fires, `parcopy` stays in B_fast for Out and for the crossovers, crossovers against it are labelled upper bounds, and only Confirmed is blocked. A "bound missing" row covers a `parcopy` that slipped or was excluded.
- **The extension is unconditional and in-trial** (feasibility 2). In hot and append, `full`, `full_syncfs`, `probackup8`, `parcopy8` and v1 at 4 and 8 jobs run 5 more unthrottled repeats before the trial ends, and the HA verdict uses all 10. A9's conditional path is gone. No arm is fixed and rerun in this session once the main sweep starts; an arm that fails is left out and recorded under rule 8. This adds 60 runs and about 16 minutes.
- **v1 gets 8 jobs too** (fairness 3). `pgdelta_direct_inplace_j8` runs unthrottled in hot and append, 10 runs per trial. (This revision chose v1's arm by pooled median; the check below reverted that.) Every unthrottled ratio is also reported at matched parallelism. The 4-job arm stays the drift-gated reference and the crossover arm. New E14; E9 and E11 extended.
- **The backup-mode adder comes from `full`'s own runs** (fairness 2). `R_adj` adds `full`'s checkpoint wait plus its wait from the WAL end point to its sync, per workload, in the same runs and cache state. The bracket probe stays as a cross-check, now with the source's cache dropped before each pair and the first pair reported separately. The write-up states what the adder still leaves out: a live v1's WAL stream, `backup_label` write and recovery.
- **Void, failed and reported are defined** (feasibility 10, fairness 5). A void `probackup*` run is excluded from B_rel and B_fast, and the HA verdict is also reported with it, flagged. A v1 or `parcopy` digest that differs from the reference is reported, and its own verification stands. A5 counts "more than 1 of an arm's runs at one speed in one trial", and nothing is rerun.

**Durability**
- **`pg_probackup` ends with a timed `sync -f`** (fairness 1). Its own flush skips the streamed WAL, `backup_label` and every directory, which the audit's tolerances couldn't see. The step is registered now, reported as `probackup_sync_s`, and the arm labelled "tool + `sync -f`". The smoke run no longer decides whether it flushes.
- **A syscall audit replaces that smoke question** (fairness 1). One untimed run of every arm under `strace`, with the reviewer's syscall list, checked by a script against the definition.
- **Check (a) is tightened** (fairness 1). The increase of `Dirty + Writeback` over the run must be at most 4 MiB, or the idle noise, whichever is larger; it was 64 MiB after the run. One WAL segment, or a v1 without fsyncs, now fails it.
- **One directory rule for every arm** (feasibility 7, fairness 4). A directory change is durable once a successful `fsync` or `syncfs` on the same ext4 filesystem has committed a journal transaction at or after it. The audit checks it. `parcopy` no longer fsyncs the output's parent, and its required count drops it.
- **`full_wb`'s reset works** (fairness 6). It writes `vm.dirty_background_ratio` back to the recorded default, since Linux refuses 0 for the bytes setting. The guard and its test require both the ratio at its default and the bytes setting at 0.

**Verification**
- **`pg_probackup`'s pass or fail is the logical digest** (fairness 5). Its output's per-table digest is compared with the source's at the trial's end, plus a start. Byte identity is a diagnostic that voids nothing, and A4 applies only to failures of the logical standard. The end reference digest now comes before EXP-01's ground truth and before any SQL that reads a user table on the source.

**Realism**
- **A warm-source check** (fairness 8). A single-reader cold read of `pgbench_accounts` joins the per-trial probes. An unconditional warm-source block runs `full`, `probackup8` and v1 at 4 jobs with the source's cache warm, 3 repeats in hot and append (18 runs). Its ratios are reported beside the cold ones and not used for the verdict. New E15.

**Scope**
- **TPROC-C and zstd move to T4** (feasibility 4 and 12, fairness 7). Invocation 3, `full_zstd`, E12, A8, `install_hammerdb.sh` and the TPROC-C build block are removed. L5 and the rule "zstd gives 2 times or more" are judged in T4. The reason is under change 3. This also removes the first version's cross-dataset comparison (fairness 7), the missing `--patterns` (feasibility 4) and E12's mismatch with L5 (feasibility 12).

**Rules and wording**
- **L3 and L4 use EXP-09's speeds only** (feasibility 8): 2, 5 Gbit/s and unthrottled, against this session's baselines at those speeds, with this session's measured single-stream rate as x.
- **No band without a consequence** (feasibility 9). The phase-timer requirement applies whenever L1 fails, not only when `--no-sync` saves under 0.5 s. A released parallel copy at 3.0–3.5 s gets its own D03 wording.
- **The released-tools claim names the tools** (feasibility 13): "faster than the released copiers tested here (`pg_basebackup` with fsync or syncfs, `pg_probackup catchup -j 4` and `-j 8`) at 2.1 GB on these VMs".

**Plan and build**
- **The smoke run can project the session** (feasibility 5). It adds a one-repeat scale-70 unthrottled pass of hot with every arm (10 runs), and 10 Gbit/s to its speeds.
- **`parcopy` copies directories too** (feasibility 6). It calls `pgdelta.datadir.select()` directly and recreates the inventory's directories with their recorded modes before copying. Its `fsync` count comes from that inventory, and a localhost test checks that its copy starts.
- **The build list is complete** (feasibility 11). It adds `run_phases()`, `method_paths()`, `method_flags()`, `tool_reported()`, `set_speed()`, `execute()`, `remote.py cmd_provision()` and `agent.py cmd_env()`, and a test that `run.json`'s `method_flags` carries each new arm's full command line.
- **The budget is recomputed** (follows from feasibility 2, fairness 3, 5 and 8). 448 runs in the sweep instead of 378, about 3.2 hours of the pair instead of 2.6. Per-run times now come bottom-up from EXP-09's run log and EXP-08's records; `probackup*`'s logical digest, about 28 s per run, is the largest new cost. The cut line moves from 3.0 to 3.5 hours, and the first cut is now `probackup*`'s byte diagnostic, which decides nothing.

**Found while checking numbers**
- pgdelta's only code change since EXP-09 is commit 371434c. The first version gave e569d55, its ID before the history rewrite.
- Reference-digest comparisons leave out `global/pg_control` and `pg_wal/`, which every clean stop rewrites. (The check of this revision added `pg_stat/`; see below.)
- The audit's syscall list was incomplete; the check of this revision replaced it with strace's `%file` and `%desc` classes (below).
- Unchanged and rechecked against `exp09-10-cells.csv` and `exp09-10-runs.csv`: E1's and E2's ranges and A7's gates; the time above link (1.47, 1.51, 2.02 s); the measured link (1.92, 4.79, 15.06 Gbit/s); v0's transfer medians; v1's dest CPU (about 10 CPU-s at the median, inside E11).

**After a check of this revision (October 24, before running)**

An independent check of the revised version found 8 more problems, 2 major, and four decisions not fully applied. Changes:
- **`pg_stat/` is left out of the reference digests** (major). Each clean stop rewrites the cumulative statistics there, so the start and end digests would have differed in every trial, and no `probackup*` logical failure could ever count as failed. A smoke item checks that nothing else differs.
- **The 4-stream arms join the extension** (major). `probackup4` and `parcopy4` can be chosen as baselines unthrottled, so they get 10 runs like the others (20 more runs, about 8 minutes).
- **The HA verdict stays on v1's 4-job arm.** The revision had chosen v1's arm by pooled median, which would pick it on the data it is judged on, in v1's favour, against EXP-09's registered arm. The 8-job reading is reported beside it.
- **The bound has its own gate.** A3 now gates `parcopy` itself. Confirmed needs a bound that passes it, as the notes say, and a bound that fails it while slower than a released arm counts as missing.
- **The syscall audit traces strace's `%file` and `%desc` classes** with `-y`, which include `rmdir`, `pwritev` and `fallocate`, missing before. `strace` is installed by `startup.sh`.
- **The cut order puts the warm-source block before uniform's repeats,** says what happens if the projection is still over the line after every cut, and records the cut line, 3.5 hours, as the lead's decision.
- **Smaller:** a `parcopy` run with the wrong `fsync` count fails A5; every ratio is also given at matched parallelism; E15's and E9's wording corrected.

**After a review of the harness build (October 24, before running)**
- **The run lines carry check (a)'s idle noise** (`--dirty-noise-mib $NOISE`, from smoke item 5). Without it, both sweeps would have judged check (a) against 4 MiB alone, not the registered limit. No rule changes.

**How the harness and the analysis read the rules (October 24, before running)**

The harness was built after the revisions above, then reviewed adversarially (16 findings, 4 major, all fixed with regression tests). Where the text above left a choice, the code makes the one below. Each is recorded here before anything runs, because each could change how a result is judged.

*Verification and A4*
- **A copy the tool itself aborts fails A4.** A run counts as a tool failure when any command of the arm's own timed phase exits non-zero or times out, `probackup*`'s `sync -f` and a `parcopy` refusal included. A failure of the harness around the copy doesn't count. Where the analysis can't tell which it was, A4 is reported as undecided beside the verdict.
- **A void `probackup*` run gets its own status, "void",** so no baseline counts it as a pass.
- **A `probackup*` output that doesn't start fails A4 whether or not the source changed.** If the trial's reference digests couldn't be compared, a logical difference counts as a failure, not as void.
- **A `probackup*` output whose start had to fetch WAL from an archive fails verification:** "no WAL fetched after the clock stops".
- **Runs whose durability couldn't be checked** (check (a) or (b) missing) are left out of the baselines and of v1's medians, as "durability unchecked". Only real failures count toward A5's reclassification.
- **Check (a) and A5 are judged apart.** A6 voids check (a) only; a `parcopy` run's `fsync` count still decides A5.

*The verdict*
- **"Near the line" flags a ratio within ±0.03 of 0.90 or 1.00** whose crossing, alone or together with other ratios within that margin, would change the row or its qualifier.
- **"Fragile" compares the whole row** that `R_max`, `R_noSSH` and `R_paired` would give with the row the medians give.
- **"Fails its gate while slower than B_rel"** means B_fast's chosen arm unthrottled is a released arm. A gate that can't be judged is not a pass, so it blocks Confirmed.
- **A "Not judged" row is added** for when hot's or append's `R_fast` is missing and the other workload doesn't already decide Out.
- **A crossover is provisional** when an arm of its envelope, missing at a speed the crossover rests on, beat or tied (within 0.05 s) the chosen arm wherever both ran.
- **Crossovers use each workload's median single-stream rate** in the main block as x. "None" in E10 means v1 was faster than that baseline at every measured speed.

*Rules A2 and A7*
- **A2's first clause** is `full_wb` minus B_fast at most 0.15 s, on main-block medians in hot and append.
- **A7's measured link for EXP-09** is the median over EXP-09's runs in that cell; wire bytes are gated for `full` and v1. A session where no gate could be checked counts as shifted.

*Arms and checks*
- **The runs refuse to start unless `/data` on both VMs is ext4 in ordered mode with barriers,** since the directory rule depends on it.
- **`parcopy`** fsyncs with 16 threads in both arms, so `parcopy4` and `parcopy8` differ only in their streams. Its receiver also fails a copy in which any file didn't get exactly its bytes. Its preflight requires early write-back on dest and `os.sendfile` on the source, without which it wouldn't be the arm registered.
- **The syscall audit** runs inside the harness (`--syscall-audit`) and traces every command of every timed phase. Only `fsync`, not `fdatasync`, covers a directory. An `open` with `O_CREAT` counts as a change of its directory. A write the trace can't map to a path fails the audit.
- **The first cut** (`probackup*`'s byte diagnostic after each trial's first repeat) is a flag, `--probackup-byte-diagnostic first_repeat`, so applying it needs no code change.
- **pgdelta's code since EXP-09** is recorded at deploy time: the diff of `pgdelta/` from EXP-09's deployed commit, `b77f33d`, goes into each VM's `DEPLOYED.json` and from there into `run.json`.

**The smoke run (October 24, 06:32–07:03 UTC, before the main sweep)**

Four short invocations on the two new VMs (PostgreSQL 17.11, `pg_probackup` 2.5.16, strace 6.1; both on an Intel Xeon at 2.80 GHz), every run verified: `exp11-smoke` (64 runs: scale 5, hot and append, 2 and 10 Gbit/s and unthrottled, every arm, the extension and the warm-source block at 1 repeat), `exp11-smoke70` (10 runs: scale 70, hot, unthrottled, every arm once), and the syscall audit, `exp11-audit` and its rerun `exp11-audit2` (10 runs each, scale 5). The items:

1. **`pg_probackup` (fixed before the sweep).** Its flags were accepted, its outputs started, and its `sync -f` ran inside the phase. But on the idle source its WAL stream noticed the stop LSN only at its next timer tick, about 10 s after the stream started: the catchups waited 2–8 s after `pg_backup_stop`, doing nothing, and took 5–11 s at scale 5 against 0.6 s for `full`. A live primary keeps WAL arriving, so this was an artifact of the idle source that would have kept `pg_probackup` out of every baseline. **The fix, under rule 8:** during `probackup*` runs only, the source emits one empty, non-transactional WAL message every 0.1 s (`pg_logical_emit_message(false, 'pgd-tick', '', true)`, from a `psql` on dest; `--probackup-wal-ticker`, commit 288e26b). It changes no table, data file or transaction status, only WAL, which every digest leaves out. It starts before the clock and stops after it; its small source CPU falls in `probackup*`'s counters. With it, the stream saw the stop LSN 0.09 s after `pg_backup_stop`. At scale 70 both `probackup4` and `probackup8` then took 6.16 s: 4.0 s copying over SSH (the largest file, about 0.94 GB, on one thread, so 8 threads gain nothing) and 1.6 s in its own sync, before the 0.02 s `sync -f`.
2. **`pg_basebackup --sync-method`** is listed (PostgreSQL 17.11).
3. **The syscall audit (fixed before the sweep).** Its first run stopped on real strace output: a `close()` of a descriptor the trace never saw opened (inherited from the parent) raised `KeyError`. Fixed with a regression test from the real trace line (commit 43da18c), and the rerun audited every arm: each durable arm had 0 uncovered files and 0 uncovered directory changes; `full_nosync` had all 992 files uncovered, as the negative control should. `pg_probackup`'s own flush, without its `sync -f`, left 2 streamed WAL segments, `backup_label` and 2 directory changes uncovered; with it, nothing (E13).
4. **The 8-stream link:** 15.1 Gbit/s unthrottled and 9.5 Gbit/s at 10 Gbit/s. One stream measured 11.0 Gbit/s unthrottled at scale 70, against EXP-09's 15.06, so this session's single stream is slower than EXP-09's, and the drift gates (A7) will likely mark the session shifted. As registered, every verdict stays within the session.
5. **Checks (a) and (b):** the idle noise of `Dirty + Writeback` on dest over 20 windows of 5 s was at most 0.44 MiB, so `NOISE` is 0.44 and check (a)'s limit stays at 4 MiB. Every durable arm stayed within it (at most 2.0 MiB at scale 70); `full_nosync` left 2,115 MB dirty at scale 70 (at least 1.5 GB). Check (b) passed in every durable run.
6. **`parcopy`:** `verify_direct` passed, every output started, the `fsync` count matched the inventory in every run, and its streams were balanced to 1.015 × the mean bytes (A3 allows 1.1).
7. **The `pg_basebackup --verbose` phases parse,** the adder's two intervals included (0.04 s and under 0.01 s at scale 70).
8. **`full_wb`:** `vm.dirty_background_bytes` was 64 MiB during every `full_wb` run and back to 0 (the ratio at 10) before the next run. The reset after a forced exception is covered by the unit tests only.
9. **The extension and the warm-source block** ran at 1 repeat each.
10. **The time projection.** From `exp11-smoke70`'s per-run times, verification included: about 7.7 minutes per main-block (trial, repeat), 2.5 per extension repeat and 1.1 per warm-source repeat, so the two sweeps take about 157 minutes and the session, with the smoke run and its two fixes, about 3.5 hours. That is at the line, so **cut 1 applies:** `pg_probackup`'s byte diagnostic only in each trial's first repeat (`--probackup-byte-diagnostic first_repeat`). It decides no rule. No other cut.
11. **`analysis/exp11.py`** ran end to end on `exp11-smoke`'s real CSV, writing only to a scratch directory.
12. **The reference digests** matched in both trials, start and end, with `pg_wal/`, `global/pg_control` and `pg_stat/` left out; no run was void.

**Seen, not acted on:** one `full_syncfs` run at scale 70 spent 3.6 s in its sync, against `full`'s 1.3 s for the same 2.1 GB. The sweep's 10 runs per trial will say whether that holds (E4).

## Limitations, known before running

- **The data shape caps per-file parallelism.** Two heap files hold most of the 2.1 GB, and released tools split by file. At TB scale, tables split into 1 GiB segments that balance well, so a released parallel copy would gain more there. **This favours v1.**
- **Stopped sources.** v1 and `parcopy` read a cleanly stopped source; the released arms read a running, idle one and pay backup mode. `R_adj` adds `full`'s own backup-mode time to v1, measured in the same runs. It still leaves out what a live v1 would also pay: streaming WAL, writing `backup_label`, and recovery at start. Backup mode under load and `pg_backup_stop`'s wait on a busy server are T8's.
- **An idle source.** No slowdown is measured; source CPU is reported as a proxy only. O4's bar is T10a's, and parallel readers may cost a live primary more than one stream.
- **Mostly cold sources.** Every run outside the warm-source block reads the source cold, though a live primary would hold much of 2.1 GB in its cache. The warm-source block is small (three arms, 3 repeats, hot and append) and reported only.
- **2.1 GB fits in RAM.** The whole flush lands at the end, which overstates one stream's overhead relative to scale, and v1's verification re-read comes from the page cache. T9; no ratio is projected to TB scale (rule 10).
- **Kernel write-back at Debian's defaults** leaves every full copy's flush until the end. `full_wb` varies it on dest, for `pg_basebackup` only. Whether Omni's nodes can set it is unknown.
- **One VM pair, local NVMe, ext4.** The link tops out near 15 Gbit/s and dest writes at about 1.6 GB/s, which sets the unthrottled floor. Larger machine shapes speed a parallel copy more than v1, whose bottleneck is dest CPU. GKE's default persistent disk may cap every method (T9). The directory rule relies on ext4's ordered journal; on another filesystem it would need explicit directory fsyncs.
- **Transport is uneven.** `pg_probackup` goes through SSH; `pg_basebackup`, `parcopy` and pgdelta send in clear. O6 requires TLS for pgdelta (T12). At 0.1 ms round-trip time, one TCP stream isn't window-limited, which hides parallel streams' advantage on DR links (T12).
- **The bound is idealised and home-built.** No TLS, manifest or backup mode, and ranges real tools don't split. A bug or a Python limit could also make it slow; its gate (A3) catches that, and only that.
- **v1 is tuned only in its job count** (4 or 8 on each end). Its other settings are EXP-09's.
- **Picking the fastest arm on the data it is judged on** (winner's curse), for the baselines. Pooling reduces the bias but doesn't remove it. v1's arm is fixed in advance (4 jobs).
- **Fast checkpoints, and no deletion of the old copy on the clock,** both favour the full copies. `pg_basebackup` defaults to a spread checkpoint; on an idle source the difference should be small. Omni's real flags are unknown.
- **No compression is measured.** zstd moved to T4, offline; a timed compressed copy waits for v1 on TPROC-C pairs.
- **Verification differs by tool:** a manifest (`pg_basebackup`), byte identity against the stopped source (`parcopy`, v1), or a per-table logical digest (`pg_probackup`), and every output is started. The logical digest checks rows, not pages.
- **Durability is checked indirectly,** not by crash tests. The syscall audit runs once per arm, at scale 5; checks (a) and (b) run in every run.
- **The order is fixed:** workloads run hot, append, uniform, and in hot and append the extension and the warm-source block follow the main block. So workload, block and time in the session are confounded, as in EXP-04b.

## Run plan and budget

**Per-run times, bottom-up.** From EXP-09's run log: a full copy's run took its timed seconds plus 1–2 s (`pg_verifybackup` about 0.5 s); a v1 run its timed seconds plus 10.3 s (B0 copied to B in 2.7 s, the stop and restart, SHA-256 of both sides, the start); each link test about 7 s; a trial's build about 1 minute. From EXP-08's records: a logical digest of 2.1 GB took 28–30 s on the source. Added here: about 1.5 s to start each full copy; about 36 s per `probackup*` run (the byte diagnostic, the start and the logical digest); about 7.5 s per `parcopy` run (the stop and restart, SHA-256, the start). So a (trial, repeat) of the main block takes about 7.3 minutes, an extension repeat about 2.5, a warm-source repeat about 1.2, and each trial's set-up, probes and end about 3.

| Step | Runs | Pair wall time |
|---|---:|---:|
| Create both VMs in `us-central1-a`; `remote.py provision` (`startup.sh`, then `install_incumbents.sh` with `pg_probackup` on both); deploy; SSH set-up | – | 20 min |
| Smoke: scale 5, hot and append, 2, 10 Gbit/s and unthrottled, every arm, 1 repeat, with the extension and the warm-source block at 1 repeat (60); the syscall audit (10, untimed); the idle-noise windows; one scale-70 unthrottled pass of hot with every arm (10); the analysis dry run | about 80 | 20 min (15–25) |
| Invocation 1: hot and append, 4 speeds, 5 repeats (250), then in each trial the extension (80) and the warm-source block (18) | 348 | 108 min (99–119) |
| Invocation 2: uniform, the same main block | 120 | 38 min (34–42) |
| Fetch, then `infra/gcp/teardown.sh` | – | 10 min |
| **Total** | **468 in the sweep, about 550 with the smoke** | **about 3.35 h (2.95–3.65)** |

Per (trial, repeat), the main block runs 25 arm-speed pairs in hot and append and 24 in uniform: `full`, `probackup8`, `parcopy8` and v1 at 4 jobs at four speeds each; `full_nosync`, `full_syncfs` and `full_wb` at two each; `probackup4`, `parcopy4` and, in hot and append, v1 at 8 jobs at one each. Each extension repeat runs 8 arms, and each warm-source repeat 3.

**If the smoke run projects more than 3.5 h,** cut in this order until it doesn't:
1. `probackup*`'s byte diagnostic after each trial's first repeat (no runs; about 6 min). It voids nothing and decides no rule.
2. The warm-source block (−18 runs, about 6 min). It is reported only.
3. `full_syncfs` and `full_wb` at 10 Gbit/s (−30 runs, about 3 min).
4. Invocation 2 (uniform) at 3 repeats (−48 runs, about 14 min).

If the projection is still over 3.5 h after all four, the session runs anyway and the overrun is recorded; nothing else is cut. Never cut: hot and append at 10 Gbit/s and unthrottled for `full`, `probackup8`, `parcopy8` and both v1 arms; `full_nosync` at 2 Gbit/s; the extension; the syscall audit. Every cut is recorded under rule 8.

**Run lines** (the shape):

```sh
M=full,full_nosync,full_syncfs,full_wb,probackup4,probackup8,parcopy4,parcopy8,pgdelta_direct_inplace
S="--method-speeds full_nosync=2,unthrottled --method-speeds full_syncfs+full_wb=10,unthrottled"
NOISE=0.44  # the smoke run's idle noise, MiB (smoke item 5); check (a)'s limit stays max(4 MiB, NOISE)
python3 experiments/network/remote.py start exp11-local -- --patterns hot,append \
    --speeds 2,5,10,unthrottled --repeats 5 --order-seed 11 --manifest-checksums CRC32C \
    --dirty-noise-mib $NOISE --probackup-byte-diagnostic first_repeat --methods $M,pgdelta_direct_inplace_j8 $S \
    --method-speeds probackup4+parcopy4+pgdelta_direct_inplace_j8=unthrottled \
    --extension-repeats 5 \
    --extension-methods full,full_syncfs,probackup8,probackup4,parcopy8,parcopy4,pgdelta_direct_inplace,pgdelta_direct_inplace_j8 \
    --warm-source-repeats 3 --warm-source-methods full,probackup8,pgdelta_direct_inplace
python3 experiments/network/remote.py start exp11-uniform -- --patterns uniform \
    --speeds 2,5,10,unthrottled --repeats 5 --order-seed 12 --manifest-checksums CRC32C \
    --dirty-noise-mib $NOISE --probackup-byte-diagnostic first_repeat --methods $M $S --method-speeds probackup4+parcopy4=unthrottled
```

The extension and the warm-source block always run unthrottled. `NOISE` carries check (a)'s idle noise into both sweeps: the smoke run's largest increase of `Dirty + Writeback` over its 20 idle windows, in MiB (smoke item 5), written here, dated, before the main sweep. Until then it is 4, which leaves the limit at 4 MiB. Without it, each sweep would judge check (a) against 4 MiB whatever the smoke run measured.

## What it needs built in the harness

About 2.75 days, in this order of priority. The HA verdict needs the first three blocks. If `parcopy` slips, the run goes ahead without it: the "bound missing" row applies, and L2 (ii) is "not judged". If `pg_probackup` fails the smoke run, B_rel is the `pg_basebackup` arms alone, L2 (i) is "not judged", and the change is recorded.

| Block | File | Function or name | What |
|---|---|---|---|
| core (about 1.25 days) | `experiments/network/measure.py` | `FULL_VARIANTS` (method → extra options), `HOST_TUNED_METHODS`, `NON_DURABLE_METHODS`, `TIERS`, `SOURCE_STOP_METHODS` (the direct methods plus `parcopy*`); `pgdelta_direct_inplace_j8` in `PGDELTA_ENGINES` (`jobs: 8`), `PGDELTA_DIRECT_METHODS` and `IN_PLACE_METHODS`; entries in `OPTIONAL_METHODS`, `OUTPUT_START_METHODS`, `PROC_SCOPE` | the new arms, pinned |
| | | `parse_meminfo`, `parse_diskstats`, `snapshot()` and `snapshot_delta()` extended; `durability_check(record, output_bytes, idle_noise)` | checks (a) and (b) |
| | | `parse_stamped_lines`, `basebackup_phases` (with the adder), `probackup_phases` | phase timers |
| | | `CSV_FIELDS`, `summary_row()`: `tier`, `block`, `source_cache`, `durable`, `durability_ok`, `dirty_increase_mb`, `disk_write_dest_bytes`, `pbb_checkpoint_s`, `pbb_stream_s`, `pbb_wal_end_s`, `pbb_sync_s`, `backup_mode_adder_s`, `probackup_sync_s`, `parcopy_*` timers, `output_start_s`, `usable_s`, `logical_match`, `void`, `bytes_identical`, `measured_s2d_gbit_8streams`, `link_time_s`, `above_link_s`, `sysctl_dirty_background_bytes`, `sysctl_dirty_background_ratio` | the CSV |
| | `benchmarks/backup.py` | `basebackup_command(..., extra=())` | extra options, backward compatible |
| | `experiments/network/run_exp04.py` | `Child(..., stamp_lines=True)` | stderr timestamped by a reader thread |
| | | `run_full(method, paths)` over `FULL_VARIANTS`; a guard refusing these methods without `--manifest-checksums CRC32C`; `run_phases()` dispatching the full variants, `probackup*` and `parcopy*` | the `pg_basebackup` arms |
| | | `method_paths()`: `parcopy`'s `source_data` and stats path, `probackup*`'s SSH configuration and log; `method_flags()`: each new arm's full command line (the full variants' `--no-sync`, `--sync-method` and `--verbose`; `probackup*`'s argv, SSH options and `sync -f`; `parcopy`'s argv; v1's `--jobs 8`), so no new method falls through to `pg_basebackup`'s flags | `run.json`'s pins (rules 7 and 8) |
| | | `tool_reported()`: `basebackup_phases`, `probackup_phases` and `parcopy`'s JSON | timers in each record |
| | | `set_dirty_writeback()` / `reset_dirty_writeback()` in `prepare()` and `run_method()`'s `finally`, the reset writing `vm.dirty_background_ratio`; the guard requiring the ratio at its default and the bytes setting at 0 | `full_wb` |
| | | `verify()`: `check_output(recovery=True)` for every `pg_basebackup` arm; `reference_digest()` at steps 2 and 7, leaving out `global/pg_control`, `pg_wal/` and `pg_stat/` | verification |
| | | `blast(direction, streams=1)`; `set_speed()`: the 8-stream test in each trial's first repeat, under a key ending in `gbit` so `run_method` copies it; `durable_write_floor()`, `read_floor()`, `single_read_probe()`, `bracket_probe()` | probes |
| | | `run_trial()`: the trial order above, with the extension, the warm-source block and the end-of-trial steps; `drop_caches(source=False)` and the source's untimed read for warm-source runs; `parse_args()`: `--extension-repeats`, `--extension-methods`, `--warm-source-repeats`, `--warm-source-methods` | the blocks |
| | | `execute()`: refuse to start unless `pg_probackup`, the same version, is on both VMs | preflight |
| | `experiments/network/agent.py` | `blast-send` / `blast-recv --streams N`, `durable-write-test DIR --writers N`, `read-test PATH --readers N --exclude pg_wal` (one reader for a file), `sysctl get` and `sysctl set` (via `sudo -n`), meminfo and diskstats in `snapshot`; `cmd_env()`: `vm.dirty_*` and the `pg_probackup` version | counters, probes, environment |
| | `experiments/network/source_cluster.py` | `backup-mode-probe --repeats 10`, the source's cache dropped before each pair | the bracket cross-check |
| | `experiments/network/remote.py` | `cmd_provision()`: run `install_incumbents.sh` on both VMs after `startup.sh` (today it runs only `startup.sh`) | provisioning |
| | `experiments/network/durability_audit.py` (new); `infra/gcp/startup.sh` | run one arm under `strace -f -tt -y -e trace=%file,%desc` on dest; map descriptors to paths; check the trace against the definition and the directory rule; `strace` in `startup.sh`'s packages and in `execute()`'s preflight | the syscall audit |
| | `experiments/network/report.py`, `analysis/exp11.py` (new) | envelopes and the pooled choice on main-block runs; `R_*` on all 10 runs with v1 at 4 jobs, with the 8-job, matched-parallelism, warm-source and void-flagged views; `R_adj` from `full`'s adder, the bracket beside it; crossovers for v1 at 4 jobs against a chosen baseline (reusing `analysis/exp04.crossover` with a `baseline` argument), v0 over 2, 5 Gbit/s and unthrottled only; drift table against EXP-09's CSVs; durability and audit tables; verdicts with their qualifier and flags; figures `exp11-baselines.svg` (time against link speed, log-log) and `exp11-phases.svg`; `exp11-runs.csv`, `exp11-cells.csv` | written and dry-run on smoke data before the sweep |
| `pg_probackup` (about 0.5 day) | `infra/gcp/install_incumbents.sh` via `remote.py provision` | run on both VMs | the binary on the source too |
| | `run_exp04.py` | `probackup_argv()`, `run_probackup()` (`catchup`, then `sync -f` of the output: one phase, steps `catchup` and `sync`), `setup_probackup_ssh()`, `check_probackup()` (same version on both VMs; `catchup --help` lists `-b FULL`), `verify_probackup()` (the byte diagnostic, then the start, then the output's logical digest, pending until step 8, as for pgBackRest); the void and A4 logic in `finish_logical_checks()` | the released parallel arm |
| `parcopy` (about 1 day) | `experiments/network/parcopy.py` (new) | `plan(selection, streams, chunk_bytes)` over `pgdelta.datadir.select()`, called directly; the inventory's directories recreated with their recorded modes before the copy; `cmd_send`, `cmd_receive`, `--stats-json` with the `fsync` count | the bound |
| | `run_exp04.py` | `run_parcopy()` in `run_pgdelta_direct`'s frame (one phase, steps `receiver_start` and `transfer`); stop and restart for `SOURCE_STOP_METHODS`; `verify_direct` routing; `check_parcopy()` | wiring |
| tests | `tests/test_exp04_network.py`, `tests/test_parcopy.py`, `tests/test_durability_audit.py` | – | the method tables and command lines; `run.json`'s `method_flags` carrying each new arm's full command line; the phase parsers and the adder on real log samples; the durability check (the 4 MiB increase, the idle noise); the audit script on synthetic traces (a file never fsynced, a directory covered only through a later file's `fsync`, a `pg_probackup`-like trace without its `sync -f`); the sysctl reset after an exception, asserting the ratio at its default and the bytes setting at 0; one counter window per arm, with `probackup*`'s `sync -f` inside it; `parcopy`'s plan balance, a localhost round trip with 1 and 4 streams that is byte-identical with every file and directory fsynced (counted through a patched `os.fsync` against the inventory's files plus directories plus one), its directories recreated with their recorded modes, its copy starting under a local PostgreSQL 17, refusal of a running cluster, and a dropped range failing verification; the pooled baseline choice; the void and A4 logic; the end-of-trial order; the CRC32C guard |

Docs: a "Methods for T2" section in `experiments/network/README.md`, and this write-up as `docs/experiments/EXP-11-full-copy-baseline.md` with a row in the experiments index.
