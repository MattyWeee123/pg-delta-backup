# EXP-10: Where should pgdelta's destination time go?

Status: **pre-registered October 23, 2026**, before the code for its new arms ran. This is test T3 of the [decision ledger](../DECISIONS.md#t3-destination-write-path-on-linux). Its arms, predictions and decision rules were written there on October 23. Changes since are dated below. The write-up was **revised the same day, still before running,** after an independent review; see [Revisions before running](#revisions-before-running-october-23-2026).

## Question

On the VMs, writing the new copy O is pgdelta's largest cost. For local changes it takes 3.3–3.5 s of a 4.3–4.5 s transfer: about 1.6 s copying B's bytes into O and 1.7 s flushing them to disk ([ledger, "Where pgdelta's time goes"](../DECISIONS.md#where-pgdeltas-time-goes-on-the-vms)). None of the choices behind that was ever compared with an alternative on Linux:
- copy-then-patch against gap-fill;
- where the fsync happens;
- pipelined against sequential verification;
- clones on a filesystem that has them;
- patching B in place instead of building O.

## Setup

The two `n2-standard-8` VMs, the harness and data of EXP-04b:
- scale 70, uniform, hot and append at 1%, CRC32C manifests;
- caches dropped before every run, wire bytes at the interface;
- **unthrottled link** (where the write path matters most);
- **5 repeats** in random order, reporting the median and the maximum.

Every arm uses 4 worker processes on each end. Every arm is staged, as in EXP-04b: S is captured with the receiver started beside it, and the receiver prefetches B while it waits. Only the destination's write path changes between arms. The figure of merit is the **transfer phase** (S warm, B prefetched), with the receiver's write phase (`rebuild_seconds`), copy and fsync timers beside it.

| Arm | Method | Rebuild | Verify | fsync | Filesystem |
|---|---|---|---|---|---|
| baseline | `pgdelta` | copy-patch | pipelined | main loop | ext4 |
| (a), (c) | `pgdelta_seq` | copy-patch | sequential | main loop | ext4 |
| (a) | `pgdelta_gapfill_seq` | gap-fill | sequential | main loop | ext4 |
| (b) | `pgdelta_fsync_wb` | copy-patch | pipelined | early write-back (`sync_file_range`) | ext4 |
| (b) | `pgdelta_fsync_worker` | copy-patch | pipelined | on a background thread | ext4 |
| (e) | `pgdelta_inplace` | in place (B is patched, no copy) | pipelined | main loop | ext4 |
| (d) | `pgdelta`, `pgdelta_inplace`, `rsync_8k`, and `incremental` with `pg_combinebackup --copy-file-range` | as above | | | **XFS** with reflinks, on the destination only |
| reference | `full` | | | | ext4, and XFS in the second sweep |

- **In-place runs** first copy B0 to a fresh B, untimed, before the cache drop. On XFS the copy uses `cp --reflink=never`, so B doesn't share blocks with B0. B is refreshed in place behind a startup block: `global/pg_control` is moved aside until the commit, so PostgreSQL can't start on a half-refreshed copy (owner decision O5). **In place, verification still re-reads all of B,** like the new-copy arms, so every arm verifies the same bytes.
- **Clones on XFS:** on XFS, `copy_file_range`, which copy-then-patch already uses, clones extents instead of copying bytes, and `cp -a` (rsync's preparation) clones too.
- **Sweep order: ext4, then XFS, then ext4 again.** The XFS sweep reformats only the destination's array and rebuilds its trials from scratch, since B0 predates each trial's workload. The source stays on ext4. The last ext4 sweep repeats only `full` and `pgdelta`, so session drift can be told apart from the filesystem. Source-side timings (the capture, the source's hashing, the link rate) are reported for every sweep as drift checks. [EXP-09](EXP-09-direct-read.md) and EXP-08's pgBackRest sweep run before the reformat.

## Predictions and decision rules (registered in the ledger)

| Arm | Prediction | Decision rule |
|---|---|---|
| (a) | copy-patch and gap-fill are within 0.3 s of each other either way (both with sequential verification) | a gap over 0.3 s picks the default |
| (b) | moving the fsync saves 1.0–1.6 s | a saving under 0.5 s leaves the fsync where it is |
| (c) | sequential verification adds at least 1.0 s | keep pipelined unless it is slower by more than 0.1 s anywhere |
| (d) | on XFS, pgdelta's write phase drops to 0.6 s or less for local changes and about 1 s for uniform | a write phase over 1.5 s on XFS rules out reflinks |
| (e) | patching in place gives a local-change transfer of 2.0–2.8 s | a saving under 1 s keeps the new-copy design |
| (f), (g) | dropped (owner decision O9: only meaningful while a capture exists) | |

### Notes written before running

- **(b) has two alternatives.** The prediction and its rule apply to the better of early write-back and the fsync worker, compared with the baseline. Each is reported.
- **(e) changed with O5.** The ledger's arm (e) was "patching B in place with an undo journal, if T1 allows". T1 is closed: B may be patched in place, and a failed refresh leaves B blocked rather than rolled back. So the arm has no undo journal. The registered prediction and rule are unchanged. *(Revised before running: the first version said the result should sit at the low end of 2.0–2.8 s. The ledger's own estimate of the pool's remaining work, 2.7–2.8 s of hashing B and re-reading it, points to the top end instead.)*
- **(d) adds two predictions for the other methods that can clone:** on XFS, rsync's preparation (`cp -a` of B0 plus `sync -f`) takes under 0.5 s, against 3.4–3.5 s on ext4 in EXP-04b. `pg_combinebackup --copy-file-range` takes under 2.5 s, against about 3.8 s with `--copy`. *(Revised before running: the first version predicted `--clone` under 1.0 s. But in PostgreSQL 17, `--clone` applies only to whole-file copies, and files with changed blocks, about 1.05 GB here, are rebuilt block by block.)*
- **The "write phase"** is the receiver's `rebuild_seconds`: copying from B, writing pages, the fsyncs, and the final directory fsyncs and publication or commit. With the fsync worker, the time the commit waits for outstanding fsyncs is reported separately as `fsync_wait_seconds` and counted in the transfer phase.

## Revisions before running (October 23, 2026)

An independent reviewer checked the first version before anything ran. Changes:
- **Statistics.** Every prediction and rule is judged per workload, on medians of 5 runs; maxima are reported beside them. Rule (c)'s "slower by more than 0.1 s anywhere" means in any workload's median, not in a single tail run. Rule (a) applies per workload; if workloads disagree, the default follows local changes, Omni's likelier case. (d)'s "about 1 s" for uniform means 0.7–1.3 s.
- **(d) could mislead on the write phase alone.** With clones, O's unpatched pages aren't in the page cache, so the pipelined verifier reads about 2.1 GB from disk. On ext4 that time was hidden behind a 3.3 s write phase. So (d) reports the transfer phase and `verify_wait_seconds` beside the write phase. Reflinks are adopted for a workload only if both hold: the write phase is at most 1.5 s, and the median transfer phase is at least 0.5 s faster than `pgdelta` on ext4 in the sweeps before and after.
- **In-place copies on XFS** use `--reflink=never` (see Setup), and the sweep order controls for session drift.
- **The incremental arm** uses `--copy-file-range`, with a revised prediction (see the notes above).
- **(e)'s expectation** points to the top of the range, not the bottom.

## Limitations, known before running

- **The staged frame keeps S warm and B prefetched**, as in EXP-04b, so the arms differ only in the destination's write path. [EXP-09](EXP-09-direct-read.md) measures the same in-place path with cold direct reads.
- **One VM pair and 2.1 GB, which fits in RAM.** The fsync costs here reflect local NVMe; persistent disks behave differently.
- **XFS and ext4 runs are separate sweeps** on the same machines, so session drift is confounded with the filesystem. The `full` reference runs in both sweeps to show any drift.
