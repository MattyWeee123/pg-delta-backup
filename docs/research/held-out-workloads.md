# The held-out workload set (rule 15), sealed October 28, 2026

**Why held out.** Every fast-path change since EXP-64 was tuned and judged on one setting:
- EXP-48's 18 GB database at pgbench scale 600;
- PostgreSQL 17.11;
- balanced persistent disks;
- the hot, uniform and scatterN cells.

RRSI and ADRS both found gains tuned that way shrink or vanish on settings the search never saw. So the settings below are **sealed**: no tuning experiment and no deciding prediction uses them. They run only at a milestone, in a pre-registered session.
- **Milestones:** before a default changes, or before a claim goes to the sponsor or into a paper.
- **The predictions** come from the tuned results: the time model's predictions, and the ratios against `pg_basebackup` the tuned cells gave.
- **A miss narrows the claim** to the settings where it held. It never re-tunes on the held-out cell. A held-out cell used once for a diagnosis is spent, and replaced by a new one.

| | Setting | How it differs | Built? |
|---|---|---|---|
| **H1** | The workloads the fast path was never tuned on: `clustered`, `tpcb`, `append` and `retention` (source_cluster.py's patterns), at scale 600 on the ha set | The change patterns: clustered ranges, TPC-B's mix of four tables, appends to `events`, deletes of old `events` | Yes (the lab's patterns) |
| **H2** | Scale 1,200 (about 36 GB, 4.5x the VMs' memory), uniform and hot | The size | Yes (`SCALE=1200`) |
| **H3** | A `pd-ssd` data disk instead of balanced, uniform and hot at scale 600 | The disk's operation and byte limits | Yes, since EXP-84: `create.sh` takes `DATA_DISK=pd-ssd`; the `hs` set |
| **H4** | PostgreSQL 18, uniform and hot at scale 600 | The server version (WAL-summary and visibility-map behaviour) | Yes, since EXP-84: the lab's `PG_MAJOR=18`, installed beside 17 |

**What a held-out session reports for each cell:**
- the fast path (current defaults) against `pg_basebackup`;
- the time model's error;
- correctness: every copy matched.

The first held-out session is owed before `--crashed-standby` or `--selector` becomes a default.
