# What the first phase found (October 22 – November 13, 2026)

The first phase built a full tool and ran 93 pre-registered experiments. All of it is on branch `archive/v1-research` (tag `v1-research-final`). Each experiment's write-up, with its predictions, data and figures, is in that branch's `docs/experiments/`. This page is the short version, to start the new phase from what is already known.

## The problem and the niche

A destination already holds an old copy of a database. `pg_basebackup` ignores it and sends everything. PostgreSQL's own incremental backup, `pg_rewind`, pg_probackup and WAL-G reuse an old copy only while a shared history holds (a manifest, WAL summaries, the old copy's own WAL, trusted page LSNs). The niche is the old copy whose history is lost: a standby whose WAL was recycled, an old primary after a failover, a restored disk snapshot. Content hashing needs no history, and when it errs it errs on the cautious side, by sending a page it didn't need to. The research summary is in [research/state-of-the-art.md](research/state-of-the-art.md).

## The key findings

- **Correct:** no wrong copy in 14,000 randomized refreshes, from stopped and live sources. In 16 of 16 power losses mid-refresh, the copy was left blocked or complete, never wrong (EXP-17, EXP-25, EXP-32 to EXP-35).
- **Sends exactly the changed pages:** about 1% of the data for clustered changes, matching rsync with 8 KiB blocks (EXP-01, EXP-02).
- **The disks are the limit, not the network** (EXP-90 to EXP-92). Setup: two 8-vCPU, 8 GB VMs in two zones, balanced persistent disks, 18 GB, 28% of pages changed. Hashing must read both full copies: about 87 s per side at 205 MB/s, however little changed.
  - The first version took 1.34x `pg_basebackup`'s time at 1 Gbit/s, because one side kept waiting on the other.
  - Removing three stalls brought that to **0.706x**: hashing ahead of sending, checking pages as they were written, and syncing on a worker thread.
  - On a fast same-region link it still takes 1.18x, since there `pg_basebackup` is limited by the disk too.
- **The hash isn't the bottleneck:** hashing used 3–5% of the CPU.
  - BLAKE3 hashed 4.8x as fast as BLAKE2b per core on x86 VMs (EXP-29), and 2.1x as fast as SHA-256 there.
  - On ARM servers SHA-256 is the faster one: 1.5x to 3x BLAKE3 (EXP-93).
- **Worker processes, not threads,** give parallel hashing in Python: threads gained at most 1.2x, processes 2.3–3.1x (EXP-05, EXP-06).
- **One TCP connection** was enough in the same region: hashing used 40% of a 1 Gbit/s link (EXP-92). Several connections would matter on long round trips with default TCP buffers (EXP-15: one stream reached 0.70–0.79 Gbit/s at 31 ms).

## Archived as well: the WAL-summary fast path

When the old copy's history *can* be proven, PostgreSQL's WAL summaries say which blocks changed, so nothing needs hashing. That path was much faster where it applies: 1.98 s against a full copy's 90.7 s for clustered changes at 18 GB (EXP-64), and 6.9 s against `pg_rewind`'s 35.1 s after a failover (EXP-71). It was archived on November 9 to focus on copies with no usable history. It is on branch `archive/fast-path` and in the main archive.

## What the new phase should keep in mind

- **Measure first.** On the setup above, a faster hash or a second connection changes nothing while the disks bind. Profile each version (`experiments/network/util_series.py` samples link, disk and CPU every 0.1 s) and improve what's actually limiting it.
- **Compare against `pg_basebackup` on the same VMs and link,** and check every copy against the source.

## Finding things in the archive

```bash
git fetch origin archive/v1-research
git show v1-research-final:docs/experiments/README.md     # the index of all 93 experiments
git checkout v1-research-final -- docs/experiments/EXP-92-confirm-syncs-on-worker.md   # one write-up
```
