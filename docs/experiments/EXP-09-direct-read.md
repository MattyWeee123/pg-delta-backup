# EXP-09: How fast is pgdelta when it reads the source's files directly?

Status: **pre-registered October 23, 2026**, before the code for it ran, and **revised the same day before running** after an independent review (see [Revisions before running](#revisions-before-running-october-23-2026)). This is test T8a of the [decision ledger](../DECISIONS.md#t8a-cold-direct-read-without-a-capture), plus a first look at v1's whole path.

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
| D2 (revised) | `pgdelta_direct_inplace` takes **2.6–3.8 s in total (median) for local changes (hot, append) at every speed**. For uniform it takes 3.0–4.5 s unthrottled and is faster than the full copy at 5 and 2 Gbit/s. *First version: 2.0–3.2 s for local changes and 2.8–4.2 s for uniform; its derivation was wrong (see revisions).* | added here |
| D3 (revised) | Without the capture, the source VM's CPU per run falls to 4.5–7.5 CPU-seconds for the direct methods, against 9.74–10.81 for staged pgdelta (EXP-04b's medians at 2 Gbit/s, 5 Gbit/s and unthrottled). The full copy uses 2.6. *First version: 3.5–6.5, below what hashing alone needs.* | added here |
| D4 | The direct methods send within 2% of staged pgdelta's wire bytes. | added here |

**Clarification of D1, written before running.** T8a's numbers are for the transfer phase, the part that changes when the sender reads the data directory instead of S. Without a capture, the receiver's start-up (about 0.2 s in EXP-04) is no longer hidden. So each run reports both the transfer phase and the total, and D1 is judged on the transfer phase, as registered. Comparisons with the full copy are reported for both.

**How D2 was estimated (revised).** The destination's pool of 4 processes hashes B (2.1 GB) and then re-reads all of B to verify it (another 2.1 GB, mostly from the page cache), 4.2 GB in all. EXP-04b's timers show that pool hashing 2.1 GB in 1.14–1.37 s with warm inputs, so at least 2.3–2.75 s. Add about 0.3 s for the receiver's start, the pool's start and the remote launch: about 2.6–3.1 s warm. Here both sides read cold, adding D1's 0.2–0.6 s: about 2.8–3.7 s. In place, almost nothing is copied or flushed for local changes. Uniform writes about 610 MB of pages and flushes them.

**What would change the plan** (revised; every comparison uses per-workload medians of `total_s` against the same session's full copy, with the maximum reported beside it):
- **T8a's rule (registered):** if `pgdelta_direct` is slower than the full copy at 5 Gbit/s in all three workloads, fix the write path (T3) before building backup-mode reads. The rule is judged on `total_s`: receiver start plus transfer, each including its remote-launch overhead, as the full copy's time does. T3 runs anyway, in the same session ([EXP-10](EXP-10-write-path.md)).
- **HA within reach:** if `pgdelta_direct_inplace`'s median unthrottled is at most 0.9 times the full copy's for both hot and append, v1's design can serve Omni's same-region HA rebuild on these VMs. The next tests are then T8 (backup mode on a live, busy source), T9 (beyond RAM) and a run on persistent disks.
- **HA out on fast links:** if it is not faster than the full copy (ratio 1.0 or more) unthrottled for hot or for append, v1 targets DR's slower links first.
- **Between 0.9 and 1.0:** undecided. Decided after T2 (the fastest durable full copy) and T5 (a faster hash).
- **Every HA conclusion is provisional until T2 runs.** The comparison here is with one `pg_basebackup` stream, which is what Omni uses today. Rule 3 asks for the fastest durable copy, and T2 predicts a parallel one at 2.0–3.0 s.

## Revisions before running (October 23, 2026)

An independent reviewer checked this write-up against the ledger and EXP-04b's data before anything ran. Changes:
- **D2's derivation was wrong.** It called EXP-04b's hash timers "cold reads included", but they were warm: S had just been written by the capture, and B had been prefetched. The corrected estimate and range are above. The first range (2.0–3.2 s for local changes) was infeasible below about 2.6 s.
- **D2's rules left outcomes undecided.** A result between the old range's top and the full copy fitted neither. The rules now use ratios to the same session's full copy, with an explicit undecided band.
- **D3's lower bound** was below what hashing 2.1 GB at 0.54–0.55 GB/s per core needs (about 3.8 CPU-s), before cold-read kernel time and sending.
- **D1's reference** moved from EXP-04b's transfer to the same session's staged transfer at the same speed. Where the relative part (0.2–0.6 s slower) and the absolute numbers disagree, the relative part governs.
- **The direct methods run with `--prefetch-basis on`,** like every other pgdelta method. It has almost no time to work, since the sender connects as soon as the receiver listens.
- **Statistics:** every prediction and rule uses per-workload medians of 5 runs. Maxima are reported beside them.

## Limitations, known before running

- **The source is stopped,** so this measures read and hash speed, not backup mode, WAL streaming or the effect of a live workload. That is T8.
- **The fastest full copy measured is still a single `pg_basebackup` stream.** A parallel full copy (T2) may be faster.
- **2.1 GB fits in RAM.** Re-reading B to verify it comes from the page cache here; beyond RAM it is a second disk pass (T9).
- **Local NVMe is faster than GKE's default balanced persistent disk.** On a persistent disk, both the full copy and pgdelta may be limited by the disk's throughput instead.
