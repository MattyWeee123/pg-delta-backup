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
| On AlloyDB Omni? | **Omni 17.10, yes:** stopped, B came out byte-identical; live, Omni recovered B; both started with S's data (EXP-40). Omni 16 is now refused, which the probe found missing. Omni ships with data checksums off, which doesn't change the bytes sent (EXP-41: within 0.16 points). **On a busy Omni, 20 of 20 live refreshes were correct**, and Omni's own `_mintxid` fork goes through the WAL like a page, so recovery repairs it (EXP-42) | A container on one VM; the live claim rests on the mechanism |
| A power loss? | **16 of 16 hard resets** left B blocked (refusing to start) or complete, never wrong (EXP-34) | An existence check |

![1,000 live refreshes](figures/exp25-live.svg)

## Is it fast, and where?

| Question | Answer | Firmness |
|---|---|---|
| **DR (20–120 ms round trips): faster than the full copies?** | **Yes**, with BBR on pgdelta's socket: 0.33–0.75 of the fastest parallel copy (EXP-20, -24), confirmed on a real 31 ms link (EXP-15) | Three emulated sessions and a real link |
| **DR: faster than native incremental, where history exists?** | **As fast or faster:** v1 was faster by 14–74% on ext4, and the two tied on XFS with reflinks (EXP-36) | XFS tie at two sessions |
| HA (fast links): faster? | Only for small data on a fast, idle destination. Beyond memory on a capped disk, at best a tie for local changes (EXP-22, -31), and a design that reads all of S can't do better there | Two sessions |
| How old can the copy be? | **For local changes, any age measured**; for evenly scattered OLTP, about **1.75 updated rows per page** behind (58,000 transactions at 345 MB) before a full copy is as fast at DR (EXP-38) | Bytes measured, time projected |
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
| Fewer bytes on slow links? | Two levers, not built: 512-byte blocks would send **5× fewer bytes** for scattered changes, and **zstd 2.8× fewer** at about 0.5 GB/s per core (EXP-37) |

![Bytes by strategy](figures/exp37-bytes.svg)

## The defaults these results set

| Setting | Default | Evidence |
|---|---|---|
| `--hash` | `auto`: BLAKE3 where both ends have the package, else BLAKE2b | EXP-29 (4.8× per core), EXP-30 |
| `--verify-scope` | `written` in place, `all` for a new copy | EXP-31, -32, -33, -35 |
| `--jobs` with `--source-kind live` | at most CPUs ÷ 4, from 1 to 4 | EXP-28, -30, -39 |
| `--congestion-control` | the host's default; `bbr` opt-in | EXP-24 (speed), EXP-26 (fairness) |

## Still open

- **Omni beyond the container:** Omni on Kubernetes (OMNI-2), and a stopped source's unlogged relations, whose `_mintxid` fork pgdelta copies rather than resets.
- **Larger sizes:** extrapolated from scale models only; tests beyond them may need a disk-quota increase.
- **The rest of the slowdown tests:** a moderate load, a large dirty set, a source near saturation at 16 vCPUs.
- **The timing model's disk term.**
- **The two bandwidth levers** (sub-page deltas, zstd), measured but not built.
