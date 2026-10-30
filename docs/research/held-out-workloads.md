# The held-out workload set (rule 15), sealed October 28, 2026

**Now:** H1–H4 were spent by [EXP-84](../experiments/EXP-84-held-out.md) and H5–H8 by [EXP-87](../experiments/EXP-87-held-out-h5-h8.md) (both October 30); they may be used to diagnose and tune. **H9–H12 are sealed, October 30, 2026,** before any work used H5–H8.

**Why held out.** Every fast-path change since EXP-64 was tuned and judged on one setting:
- EXP-48's 18 GB database at pgbench scale 600;
- PostgreSQL 17.11;
- balanced persistent disks;
- the hot, uniform and scatterN cells.

RRSI and ADRS both found gains tuned that way shrink or vanish on settings the search never saw. So the settings below are **sealed**: no tuning experiment and no deciding prediction uses them. They run only at a milestone, in a pre-registered session.
- **Milestones:** before a default changes, or before a claim goes to the sponsor or into a paper.
- **The predictions** come from the tuned results: the time model's predictions, and the ratios against `pg_basebackup` the tuned cells gave.
- **A miss narrows the claim** to the settings where it held. It never re-tunes on the held-out cell. A held-out cell used once for a diagnosis is spent, and replaced by a new one.

| | Setting | How it differs | Status |
|---|---|---|---|
| **H1** | The workloads the fast path was never tuned on: `clustered`, `tpcb`, `append` and `retention` (source_cluster.py's patterns), at scale 600 on the ha set | The change patterns: clustered ranges, TPC-B's mix of four tables, appends to `events`, deletes of old `events` | **Spent** by EXP-84 (built in the lab's patterns) |
| **H2** | Scale 1,200 (about 36 GB, 4.5x the VMs' memory), uniform and hot | The size | **Spent** by EXP-84 (built with `SCALE=1200`) |
| **H3** | A `pd-ssd` data disk instead of balanced, uniform and hot at scale 600 | The disk's operation and byte limits | **Spent** by EXP-84 (built for EXP-84: `create.sh` takes `DATA_DISK=pd-ssd`; the `hs` set) |
| **H4** | PostgreSQL 18, uniform and hot at scale 600 | The server version (WAL-summary and visibility-map behaviour) | **Spent** by EXP-84 (built for EXP-84: the lab's `PG_MAJOR=18`, installed beside 17) |
| **H5** | Skewed updates with index upkeep: pgbench's update on `pgbench_accounts` with `random_gaussian` (parameter 5) for its row, and a secondary index on `abalance`, so no update is HOT; scale 600 on pd-balanced | The change pattern: a skewed hot set, new tuple versions on other pages, and scattered index pages | **Spent** by EXP-87 (built for EXP-87: the lab's pattern `gaussidx`) |
| **H6** | A source and destination with 32 GB of memory (n2d-standard-8), scale 600, hot and uniform | The database fits in memory: the page cache holds S and B | **Spent** by EXP-87 (built for EXP-87: `infra/gcp/sets/hm.sh`) |
| **H7** | Local NVMe SSD data disks (`vmset.sh`'s local-nvme), scale 600, hot and uniform | The disk: limits far above persistent disks', and no network storage | **Spent** by EXP-87 (built for EXP-87: `infra/gcp/sets/hn.sh` (two local NVMe SSDs, striped)) |
| **H8** | PostgreSQL 17.11 with data checksums off and `wal_log_hints = on`, scale 600, hot and uniform | The cluster's configuration: page-lsn's other requirement, and hint bits WAL-logged without checksums | **Spent** by EXP-87 (built for EXP-87: the lab's `CHECKSUMS=0` and `WAL_LOG_HINTS=1`) |
| **H9** | Many relations: 2,000 small tables beside pgbench at scale 300, the change spread over all of them | The relation count: the guard's work per relation, and the files opened and synced | Sealed; built at first use |
| **H10** | PostgreSQL 18 with asynchronous I/O by `io_method = io_uring`, scale 600, hot and uniform | The server's read path (and `pg_basebackup`'s) | Sealed; built at first use |
| **H11** | Smaller VMs: n2d-highcpu-4 (4 vCPUs, 4 GB of memory), scale 600, hot and uniform | Fewer CPUs, and the database 4.5x memory (18 GB on 4 GB) | Sealed; built at first use (a set's machine type) |
| **H12** | Source and destination in different regions (us-west1 and us-east1), scale 600, hot and uniform | A cross-region link (about 60 ms round trips) on the fast path | Sealed; built at first use (a set's zones) |

**What a held-out session reports for each cell:**
- the fast path (current defaults) against `pg_basebackup`;
- the time model's error;
- correctness: every copy matched.

The first held-out session, EXP-84, ran on H1–H4: `--crashed-standby` held in 10 of 10 cells; `--selector`'s time model missed every refresh of a few seconds; page-lsn missed at scale 1,200 with 28% of the pages changed. The second, EXP-87, ran on H5–H8: single read, `--crashed-standby` and the fast path's defaults held in all 7 cells. The next milestone runs on H9–H12.
