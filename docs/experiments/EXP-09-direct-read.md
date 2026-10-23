# EXP-09: How fast is pgdelta when it reads the source's files directly?

Status: **pre-registered October 23, 2026**, before the code for it ran. This is test T8a of the [decision ledger](../DECISIONS.md#t8a-cold-direct-read-without-a-capture), plus a first look at v1's whole path.

## Question

In [EXP-04b](EXP-04b-default-checksums.md), staged pgdelta first took a local backup S on the source. That capture alone took 4.0 s, as long as a whole full copy. v1 is meant to read the source's data files directly instead. Two questions:
1. **T8a, as registered:** with today's destination write path, how fast is pgdelta when its sender reads a stopped source's data directory directly, with caches dropped on both sides?
2. **v1's path:** how fast is it when, in addition, it patches the old copy in place, which owner decision O5 now allows? This is the first measurement of both v1 changes together. It still leaves out backup mode and a busy source; those are T8.

The answer decides whether Omni's same-region HA rebuild, on a fast link, is within reach.

## Setup

The two `n2-standard-8` VMs of EXP-04b (local NVMe, ext4, Debian 12, PostgreSQL 17.11) and the same harness ([experiments/network/](../../experiments/network/README.md)):
- scale 70 (2.1 GB), uniform, hot and append workloads at 1%, EXP-01's seed;
- CRC32C manifests, the page cache emptied on both VMs before every run, link shaping on both VMs, wire bytes counted at the interface, 5 repeats in random order.

| Method | Source side | Destination side | Timed |
|---|---|---|---|
| `full` | `pg_basebackup` (one stream) | new directory | the copy |
| `pgdelta` | captures S, as in EXP-04b | new copy O, today's write path | capture (with the receiver's start beside it) + transfer |
| `pgdelta_direct` | **reads the stopped data directory** (`--source-kind datadir`) | new copy O, today's write path | receiver start + transfer |
| `pgdelta_direct_inplace` | reads the stopped data directory | **patches B in place** (`--in-place`) | receiver start + transfer |

- **Untimed set-up for the direct methods:** stop the source cluster cleanly before the cache drop, and restart it after the run. For the in-place method, also copy B0 to a fresh B before the cache drop.
- **Direct reads** apply PostgreSQL's base-backup exclusions and take only the WAL segment holding the shutdown checkpoint.
- **Every output is verified, untimed:** byte-identical to the source's files, then started and queried.
- **Speeds:** 2 Gbit/s, 5 Gbit/s and unthrottled.
- **Engine:** today's, pinned: 4 worker processes on each end, copy-then-patch, pipelined verification, fsync in the main loop.

## Predictions

| ID | Prediction | Source |
|---|---|---|
| D1 | `pgdelta_direct`'s transfer phase is 0.2–0.6 s slower than the warm staged transfer: in the same session, staged `pgdelta`'s transfer phase at the same speed. At 5 Gbit/s that's 4.6–5.0 s for local changes and 5.2–5.6 s for uniform, against the full copy's 5.07 s in EXP-04b. | ledger (T8a) |
| D2 | `pgdelta_direct_inplace` takes **2.0–3.2 s in total for local changes (hot, append) at every speed**, faster than the full copy at every speed, unthrottled included. For uniform, it takes 2.8–4.2 s unthrottled and is faster than the full copy at 5 and 2 Gbit/s. | added here |
| D3 | Without the capture, the source VM's CPU per run falls to 3.5–6.5 CPU-seconds for the direct methods, against 9.8–10.7 for staged pgdelta in EXP-04b. The full copy uses 2.6. | added here |
| D4 | The direct methods send within 2% of staged pgdelta's wire bytes. | added here |

**Clarification of D1, written before running.** T8a's numbers are for the transfer phase, the part that changes when the sender reads the data directory instead of S. Without a capture, the receiver's start-up (about 0.2 s in EXP-04) is no longer hidden. So each run reports both the transfer phase and the total, and D1 is judged on the transfer phase, as registered. Comparisons with the full copy are reported for both.

**How D2 was estimated.** Hashing 2.1 GB on 4 processes takes about 1.1–1.3 s on each side, cold reads included (EXP-04b's phase timers; EXP-07's 2.87 GB/s raw reads). The destination's pool then re-reads B to verify it, another 2.1 GB, mostly from the page cache. In place, almost nothing is copied or flushed for local changes; uniform writes about 610 MB of pages and flushes them. Add about 0.2 s of receiver start-up.

**What would change the plan:**
- **T8a's rule (registered):** if `pgdelta_direct` is slower than the full copy at 5 Gbit/s in all three workloads, fix the write path (T3) before building backup-mode reads. T3 runs anyway, in the same session ([EXP-10](EXP-10-write-path.md)).
- **If D2 holds for local changes unthrottled,** v1's design can serve Omni's same-region HA rebuild on these VMs. The next tests are then T8 (backup mode on a live, busy source), T9 (beyond RAM) and a run on persistent disks.
- **If `pgdelta_direct_inplace` doesn't beat the full copy unthrottled for local changes,** the HA case is out on fast links, and v1 targets DR's slower links first.

## Limitations, known before running

- **The source is stopped,** so this measures read and hash speed, not backup mode, WAL streaming or the effect of a live workload. That is T8.
- **The fastest full copy measured is still a single `pg_basebackup` stream.** A parallel full copy (T2) may be faster.
- **2.1 GB fits in RAM.** Re-reading B to verify it comes from the page cache here; beyond RAM it is a second disk pass (T9).
- **Local NVMe is faster than GKE's default balanced persistent disk.** On a persistent disk, both the full copy and pgdelta may be limited by the disk's throughput instead.
