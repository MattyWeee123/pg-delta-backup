# What the evidence says (October 26, 2026)

A one-page summary of the questions pgdelta's pitch rests on: the answer, how firm it is, and the figure behind it. Every answer links to its experiment, which was pre-registered, analysed by a committed script, and reports its failed predictions as failed. The full ledger is [DECISIONS.md](DECISIONS.md), and the day-by-day record [LOG.md](LOG.md).

pgdelta refreshes an older physical copy of a PostgreSQL cluster (B) from the live server (S). It hashes both, sends only the pages that differ, patches B in place, and verifies the result. v1 is that path reading the running server in backup mode.

## Is it correct?

| Question | Answer | Firmness |
|---|---|---|
| Wrong copies from a stopped source? | **None in 10,000 randomized trials** (EXP-17), plus **2,000** more with one-pass verification (EXP-32) | Below 0.069% per refresh at 99.9% confidence; macOS, 50 MB |
| Wrong copies from a live, busy source? | **None in 1,000 trials** (EXP-25), and **1,000 more** with BLAKE3 and one-pass verification (EXP-33) | EXP-25: below 0.69% at 99.9%. EXP-33 claims no bound: a control fell below its bar |
| Would the checks catch a wrong copy? | **Yes: 76 of 76** wrong copies of the read-during-start kind were caught, and no correct copy was flagged (EXP-27, EXP-35, with an oracle) | Measured for that kind of fault |
| A killed refresh? | B is always left untouched, blocked from starting, or complete (EXP-17, -25, -32, -33) | Several hundred kills |
| On AlloyDB Omni? | **Omni 17.10, yes:** stopped, B came out byte-identical; live, Omni recovered B; both started with S's data (EXP-40). Omni 16 is now refused, which the probe found missing. Omni's container ships with data checksums off (its Kubernetes operator turns them on, EXP-44), and either way the bytes sent are the same (EXP-41: within 0.16 points). **On a busy Omni, 20 of 20 live refreshes were correct**, and Omni's own `_mintxid` fork goes through the WAL like a page, so recovery repairs it (EXP-42). Unlogged tables are copied as Omni's own `pg_basebackup` copies them (EXP-43) | A container on one VM; the live claim rests on the mechanism |
| Is there an old copy to refresh on Omni's Kubernetes operator? | **Not after an HA failover:** the operator deletes the old primary's disks and reseeds a new standby with `pg_basebackup` (EXP-44). A sidecar does run on every instance with PGDATA readable and a CPU quota. So HA on Kubernetes needs an operator change; DR remains pgdelta's case | One cluster, one failover |
| A power loss? | **16 of 16 hard resets** left B blocked (refusing to start) or complete, never wrong (EXP-34) | An existence check |

![1,000 live refreshes](figures/exp25-live.svg)

## Is it fast, and where?

| Question | Answer | Firmness |
|---|---|---|
| **DR (20–120 ms round trips): faster than the full copies?** | **Yes**, with BBR on pgdelta's socket: 0.33–0.75 of the fastest parallel copy (EXP-20, -24), confirmed on a real 31 ms link (EXP-15) | Three emulated sessions and a real link |
| **DR: faster than native incremental, where history exists?** | **As fast or faster:** v1 was faster by 14–74% on ext4, and the two tied on XFS with reflinks (EXP-36) | XFS tie at two sessions |
| HA (fast links): faster? | Only for small data on a fast, idle destination. Beyond memory on a capped disk, at best a tie for local changes (EXP-22, -31), and a design that reads all of S can't do better there | Two sessions |
| Can we project beyond memory? | **Yes, on this disk type:** v1 ≈ \|B\| ÷ read rate + pages written ÷ page-write rate; fixed at 12 GB, it predicted 18 GB within 4.4% (EXP-48) | Two sizes, one disk |
| How old can the copy be? | **For local changes, any age measured**; for evenly scattered OLTP, about **1.75 updated rows per page** behind (58,000 transactions at 345 MB) before a full copy is as fast at DR (EXP-38) | Bytes measured, time projected |
| On a busy primary with a large dirty set? | **The start checkpoint dominates, not pgdelta:** with about 4.9 GB dirty on a balanced persistent disk, a fast checkpoint (pgdelta's default, `pg_basebackup -c fast`) nearly stalled the load for 30 s (27.5 s of work lost); a spread one (`pg_basebackup`'s default) ran it half short for 4.5 minutes (51.8 s lost). `--checkpoint spread` exists; which O4 number governs is open (EXP-57) | One session, one VM |
| On a busy primary? | At 2 source workers on 8 vCPUs, and 4 on 16, v1 costs the load no more than `pg_basebackup`. **With BLAKE3 it refreshes 2.1× faster than `pg_basebackup` on 8 vCPUs, and 2.1× on 16 at 4 workers** (EXP-28, -30, -39) | Three sessions (4 workers on 8 vCPUs missed in one of them) |

![v1 against native incremental at DR](figures/exp36-incremental-b.svg)

![BLAKE3 on a busy primary](figures/exp30-blake3.svg)

## What it costs others

| Question | Answer |
|---|---|
| Is BBR fair to other traffic? | **No: it stays opt-in.** On a shared bottleneck it took 81–96% of the link from cubic flows when the queue held a round trip or less (EXP-26) |
| Slowdown of the source's load? | Within `pg_basebackup`'s, at live mode's default of one source worker per 4 CPUs (EXP-28, -30, -39) |

## What's left on the table

| Question | Answer |
|---|---|
| Reuse after `VACUUM FULL` / `CLUSTER`? | **None, by any strategy:** cross-file page matching and content-defined chunking found nothing (EXP-37). Positional pages lose nothing |
| Fewer bytes on slow links? | **zstd is built** (`--compress zstd`, opt-in): 2.8× fewer bytes. Scattered changes took 0.52 of v1's time at 0.5 Gbit/s (6.8 s against 13.1; `pg_basebackup` 36.9), 0.75 at 1 Gbit/s and, with batched pool tasks, 0.86 at 2. **Break-even about 4 Gbit/s** (EXP-45 to -47, -52, -53). **Sub-page deltas are built but off** (`--subpage`): 3.5× fewer bytes again, but slower as built, since their block hashing outweighed the link time saved (EXP-54 to -56) |

![Bytes by strategy](figures/exp37-bytes.svg)

## The defaults these results set

| Setting | Default | Evidence |
|---|---|---|
| `--hash` | `auto`: BLAKE3 where both ends have the package, else BLAKE2b | EXP-29 (4.8× per core), EXP-30 |
| `--verify-scope` | `written` in place, `all` for a new copy | EXP-31, -32, -33, -35 |
| `--jobs` with `--source-kind live` | at most CPUs ÷ 4, from 1 to 4 | EXP-28, -30, -39 |
| `--congestion-control` | the host's default; `bbr` opt-in | EXP-24 (speed), EXP-26 (fairness) |
| `--compress` | `off`; `zstd` opt-in for DR links below about 4 Gbit/s, chiefly 2 and slower | EXP-37, -45 to -47, -52, -53 |
| `--subpage` | off (slower as built) | EXP-54 to -56 |
| `--checkpoint` (live) | `fast`; `spread` opt-in | EXP-57 |

## Still open

- **HA on Omni's Kubernetes operator:** the operator deletes the old instance's disks on a failover and reseeds with `pg_basebackup` (EXP-44), so pgdelta's HA role there needs the operator to keep and refresh the old disk. DR is unaffected.
- **Larger sizes:** beyond memory, a two-term disk model (read B, then the scattered writes) fixed at 12 GB predicted 18 GB within 4.4% (EXP-48). Other disks' page-write rates still need measuring; sizes past a few times memory remain projection.
- **The rest of the slowdown tests:** a moderate load, a large dirty set, a source near saturation at 16 vCPUs.
- **The bandwidth levers:** zstd's own time floor (about 6.1 s here, which keeps it even at 2 Gbit/s; EXP-47). EXP-49 traced it to the thread pools' cost per task on pgdelta's small runs (about 12–13 µs each, and 12 µs more for each of the receiver's writes), not to the codec. Batching runs into 1 MiB tasks is now the default: in two local sessions it cut zstd's extra time over plain v1 to 0.45 of it, with every copy byte-identical to S (EXP-50, -51). On VMs at 2 Gbit/s zstd went from even (1.05) to 0.86 of v1 (4.88 s), in two sessions (EXP-52). Its break-even is now about 4 Gbit/s at 60 ms (interpolated: 0.95 at 3 Gbit/s, 0.97 at 4, 1.06 at 5; EXP-53), so it is worth it chiefly at 2 Gbit/s and slower. **Sub-page deltas multiply with zstd:** 512-byte blocks plus zstd sent 0.21 of zstd's bytes, 13.6× fewer than whole pages, counting B's block digests (EXP-54). Cheap beyond their bytes on the Mac (EXP-55), but **as built, slower** on VMs: 1.16× v1 + zstd's time at 0.5 Gbit/s and 1.44× at 1, with 3.5× fewer bytes. Its block hashing, about 2.2 s on the sender's main thread plus waits on the receiver's, outweighed the link time saved (EXP-56). `--subpage` stays opt-in and off; moving that hashing onto the workers is the revision to test.
