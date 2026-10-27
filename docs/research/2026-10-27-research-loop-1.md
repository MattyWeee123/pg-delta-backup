# Research loop 1 (October 27, 2026): where pgdelta stands against the real competition, and what to try next

The owner's rule 12 (decision K13): before choosing the next work,
1. name each situation's industry standard and strongest competitor, from primary sources;
2. say with measurements whether pgdelta beats it;
3. where it doesn't, find the bottleneck by experiment, and bound it to tell a systems problem from an algorithm problem;
4. mine papers for mechanisms to combine.

This is the first pass. Every claim is tagged **measured** (with its experiment), **documented** (a primary source), or **inferred** (reasoning not yet tested).

## 1. Have we been doing anything novel?

Mostly not, until the fast path.
- **The core algorithm is standard:** compare keyed hash trees of fixed pages, send what differs, patch in place, verify. rsync, Cassandra's Merkle repair, SQLite's `sqlite3_rsync` and pgBackRest's delta restore all use the same idea.
- **What pgdelta added was engineering for an unserved niche:** refresh any older copy from a live server, in place, verified, with no backup repository.
- **Most of October 25–27 tuned or re-measured that one algorithm:** zstd batching, sub-page deltas, BLAKE3, TLS cost, the start checkpoint, and second sessions. That made results firm, but it never asked whether hashing could win where it was losing.

The first real change of algorithm is the WAL-summary fast path (EXP-61 to EXP-64): labels instead of hashes, behind a lineage proof.
- **What's new:** the combination. PostgreSQL core applies WAL summaries only to produce a new backup through `pg_combinebackup` into a new directory, and only against an earlier backup's manifest (**documented**, PostgreSQL 18 docs). It has no supported in-place application to an existing copy, with `pg_rewind`'s divergence test, from any provable ancestor.

## 2. The industry standard and the strongest competitor, per situation

Verified from primary sources (operator source code, PostgreSQL docs, cloud docs); the sources are in the agent report kept in the session notes, and the main ones are linked here.

| Situation | Standard practice | Strongest competitor | Does pgdelta beat it? |
|---|---|---|---|
| Standby behind, WAL archive exists | Replay the archive through `restore_command` (CloudNativePG does it automatically) | the same | **Not measured head to head.** EXP-59's recovery from a day's churn (67–121 s for about 196 segments) suggests pgdelta wins when the WAL since B is much larger than its distinct changed blocks (**inferred**) |
| Standby behind, no archive (WAL recycled) | Re-clone with `pg_basebackup` (Patroni `reinit`, CloudNativePG by deleting the PVC, Omni's operator automatically); Crunchy PGO tries `pgbackrest restore --delta` first | pgBackRest delta restore (needs a repository) | **Yes:** against a re-clone at DR rates (0.33–0.75, EXP-15/20/24) and against pgBackRest from a day-old backup (0.024–0.122, EXP-59/59b). In HA beyond memory, hashing only ties (EXP-48); the fast path is EXP-64 |
| Old primary after failover | `pg_rewind` (Patroni, CloudNativePG, PGO, Spilo), else a re-clone | `pg_rewind` | **Not measured head to head.** The fast path refuses (diverged), so pgdelta falls back to hashing. `pg_rewind` probably wins wherever it can run: it needs `wal_log_hints` or checksums, plus the old primary's WAL. Omni ships with both off (EXP-40), and its operator deletes the old primary (EXP-44) |
| Cross-region DR copy | Restore from an object-store backup, or re-seed with `pg_basebackup` | GCP PD Async Replication (storage-level, about 1 min recovery point, same continent); pgBackRest | **Yes,** against re-seeds and restores (above). Async Replication keeps a copy continuously, but restarting it after a failover needs a new secondary disk and a full copy (**documented**): that re-protection is pgdelta's opening |
| Same-zone HA re-clone | Omni's operator: delete, then re-seed | A disk clone from a fresh snapshot: no host I/O, and full speed at once on GCP (**documented**) | **Not measured.** A snapshot needs a consistent point plus WAL catch-up, and a new disk |
| Managed services (AlloyDB, Aurora, Neon) | Shared storage: no copy to refresh | n/a | Out of scope |

## 3. Where pgdelta loses: systems problem or algorithm problem? (measured)

**The ceiling of the hash path.** On the Mac, with a 1 GiB file in the page cache (`scratchpad/ceiling/ceiling.py`):

| Path | Throughput |
|---|---|
| Reading alone | 18.9 GB/s |
| The BLAKE3 function, one core | 2.2 GB/s |
| pgdelta's own hash path, `--jobs 1` | 2.0 GB/s |
| pgdelta's own hash path, `--jobs 4` | **7.5 GB/s** |
| Ideal for this algorithm on 4 cores | 8.9 GB/s |

pgdelta hashes at about 85% of what any implementation of the same algorithm could. Per rule 2 this is scoping, not a decision. On the VMs, EXP-60's timers agree: the hash function is about 0.2 s of each 0.9 s hashing phase at 2.1 GB, and the rest is reading the copy from a cold cache. **So better code can't rescue hashing; reading is the cost.**

**The bound beyond memory** (`analysis/bounds.py`, EXP-48's measured constants: read 209.5 MB/s, sequential write 197.6 MB/s, scattered writes 6,750 pages/s, 18 GB):

![Lower bounds beyond memory](../figures/bounds-beyond-memory.svg)

- **A full copy** pays about N/W_seq: 94.7 s.
- **Hashing** must read all of B before it knows what to write: N/R + C/w, where C is the number of changed pages. It can at best tie a full copy (85.1 s against 94.7 s with nothing changed), and it falls behind once about 3% of pages changed. **This is an algorithm limit:** no systems work removes the N/R term.
- **Page labels (the fast path)** pay only f + C/w, where f is the session's fixed cost. They beat a full copy while fewer than about 28% of pages changed (w × 8 KiB / W_seq), and lose past that, where sequential writing wins. **Past 28% the limit is the disk's random-against-sequential write rate,** for any algorithm that writes in place page by page.

Uniform 1% row updates touched 28.4% of pages in EXP-48, so EXP-64's uniform cell sits at the crossover (**inferred**: a tie with the full copy, about half of hashing's time).

## 4. Mechanisms from papers, and what to combine (with a committee's reading)

Mechanisms taken from the papers themselves (extracts in the session's `scratchpad/papers/`):

1. **Both sides' WAL block sets, combined, for the diverged old primary.** `pg_rewind` scans the old primary's WAL from the last common checkpoint for every block touched, copies those blocks from the new primary, then makes the copy replay the new primary's WAL since that checkpoint ([pg_rewind](https://www.postgresql.org/docs/17/app-pgrewind.html)). PostgreSQL 17's summarizer keeps the same block sets per WAL range, with limit blocks for truncation ([blkreftable.c](https://github.com/postgres/postgres/blob/REL_17_STABLE/src/common/blkreftable.c)).
   - **The combination:** U = the blocks B changed since the fork (from its WAL), plus the blocks S changed since then (from S's summaries). Send S's current copy of each block in U, once, plus the files `pg_rewind` copies whole. S's history since the fork is never replayed. This is dsync's dirty tracking ([LISA '13](https://www.usenix.org/conference/lisa13/technical-sessions/papers/knauth)) on both sides.
   - **Where it could beat `pg_rewind`:** after a long-ago failover, `pg_rewind`'s replay grows with S's WAL volume, while U counts each distinct block once (**inferred**).
   - **Its limits are `pg_rewind`'s own:** hint-bit safety needs `wal_log_hints` or checksums; B's WAL must reach back to the fork; S's summaries must cover the gap.
2. **Coalesced, gap-filled writes planned per region** (ROMIO data sieving, [Thakur et al.](https://arxiv.org/abs/cs/0310029); PandaSync's linear threshold, [ICDCS '19](https://ranger.uta.edu/~jiang/publication/Conferences/2019/2019-ICDCS-PandaSync.pdf)). dsync itself reports a plain copy matching it at 10% random change on disk drives, the same wall as section 3's 28% crossover.
   - **The idea:** fill clean gaps of one or two pages between dirty runs, so writes become larger and fewer. The fill bytes come from B's own buffer, or from S on the fast path.
   - **What would decide it:** if a balanced persistent disk counts a 16–80 KiB write as one operation until bandwidth binds, the write term for EXP-48's uniform case could fall from about 92 s to about 40 s. That would move the crossover well past 28%.
   - **Status:** GCP doesn't document how it counts I/O size. **The premise is untested.** An `fio` block-size sweep on the same disk settles it in minutes.
3. **Node-first native hashing with BLAKE3's own subtree chaining values** (BLAKE3 `hazmat` API; SkySync's "combine, don't recompute", [FAST '26](https://www.usenix.org/conference/fast26/presentation/zhang-zhihao)).
   - **The claim:** one SIMD pass per 1 MiB node yields the node digest, the page digests and the 1 KiB chunk digests (for sub-page deltas) at once.
   - **A committee's reading:** the survey estimated most of the 3.3–4 s fixed cost was per-page Python work. Section 3's ceiling contradicts that for hashing: 7.5 of an ideal 8.9 GB/s. It could still help the verify phase and the sub-page CPU cost (EXP-56). **Not justified until a VM measurement splits the fixed cost into reading, Python and I/O.**
4. **A full-copy-or-delta selector** (PandaSync). Predict T_delta and T_full from link and disk rates measured at the start, and stream a full copy where the delta can't win (hashing beyond memory with unproven lineage). It is cheap and safe, and it turns today's ties into the better of the two.
5. **Rejected for now, with reasons:**
   - **IBLT and Rateless IBLT set reconciliation** ([Eppstein et al.](https://www.ics.uci.edu/~eppstein/pubs/EppGooUye-SIGCOMM-11.pdf), [Yang et al.](https://arxiv.org/abs/2402.02668)) cut the digest bytes, already about 0.4% of page bytes. They still need every page digest on both sides, which means the full read that is the real cost.
   - **SkySync-style reuse of PostgreSQL's own 16-bit page checksum** still needs the page read, and 16 bits can't decide equality.
   - **Block-layer trackers** (dm-era, VMware CBT) add nothing on S over WAL summaries, and need device-mapper under B's data directory.

## 5. What to do next, ranked (each to be pre-registered before it runs)

1. **Measure the competitors we've never timed** (no new algorithm until we know we lose):
   - **(a)** the old primary after failover: `pg_rewind`, then start to consistency, against pgdelta's hashing fallback, as S's WAL since the fork grows;
   - **(b)** the lagging standby: catch-up from a WAL archive against the fast path, as the lag grows.

   A local scale model first, then the VMs (rule 2).
2. **The `fio` sweep** on a 150 GiB balanced persistent disk (8–64 KiB writes, queue depth 32): it decides idea 2's premise. Minutes, on the next VM pair.
3. **If 1(a) shows `pg_rewind` beating hashing:** prototype idea 1 (both sides' block sets) and pre-register it against `pg_rewind`.
4. **If 2 confirms larger writes cost the same operations:** prototype idea 2 in the fast path's write plan, and re-run EXP-64's uniform cell.
5. **The selector (idea 4):** small, after EXP-64 gives the fast path's measured constants.
6. **Idea 3 only if** a VM split of the fixed cost shows that per-page work, not reading, dominates.

**Update, the same night: EXP-64's result.** The fast path refreshed 18 GB of local changes in 1.98 s (0.022 of a full copy), on the label bound. Scattered changes took 164.8 s, about 70 s above the bound. The runs' own timers put those 70 s in the sender's one-at-a-time reads of the source, which didn't overlap the receiver's writes: a systems problem, measured. The ranking above gains a new first item:

0. **The fast path's source read pipeline** (parallel or read-ahead reads of the chosen pages, overlapped with sending), pre-registered as EXP-64b on the uniform cell. Its bound is about 112 s (0.62 of hashing). Beating a full copy there also needs idea 2.
