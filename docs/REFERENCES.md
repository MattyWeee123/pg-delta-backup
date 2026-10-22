# References

A short, curated reading list. Every claim and source behind it is in
[the state-of-the-art research](research/state-of-the-art.md) and its
[notes](research/notes/). Papers marked **local copy** are in
[references/](references/); the rest are copyrighted and linked only.

## Start here

1. [State-of-the-art research](research/state-of-the-art.md): what exists, where the gap is, and the design and benchmark it implies.
2. [PostgreSQL: continuous archiving and incremental backup](https://www.postgresql.org/docs/18/continuous-archiving.html): how backups, WAL replay and PG17+ incremental backup work.
3. [pg_basebackup](https://www.postgresql.org/docs/18/app-pgbasebackup.html): the tool we must beat. It always sends every file and needs an empty target.
4. [sqlite3_rsync](https://sqlite.org/rsync.html): the closest existing design, with tiered page hashes sent from replica to origin.
5. [pg_probackup `catchup`](https://postgrespro.github.io/pg_probackup/): the closest PostgreSQL competitor. It refreshes a stale standby from the live primary, but trusts page LSNs.

## PostgreSQL internals we rely on

| Source | Why it matters |
|---|---|
| [Page layout](https://www.postgresql.org/docs/18/storage-page-layout.html) | 8 KiB pages and the `pd_lsn` page stamp |
| [Database file layout](https://www.postgresql.org/docs/18/storage-file-layout.html) | Relation files, forks (FSM, VM, init) and 1 GiB segments |
| [WAL settings](https://www.postgresql.org/docs/18/runtime-config-wal.html) | `full_page_writes`, `wal_log_hints`, `summarize_wal` |
| [Replication protocol: BASE_BACKUP](https://www.postgresql.org/docs/18/protocol-replication.html) | Which files a physical copy includes and excludes |
| [Backup control functions](https://www.postgresql.org/docs/18/functions-admin.html#FUNCTIONS-ADMIN-BACKUP) | `pg_backup_start` / `pg_backup_stop`, which the v1 direct-read design needs |
| [Backup manifest format](https://www.postgresql.org/docs/18/backup-manifest-files.html) | One checksum per file; what our output manifest must look like |
| [pg_verifybackup](https://www.postgresql.org/docs/18/app-pgverifybackup.html) | What verification proves, and why test restores are still needed |
| [pg_combinebackup](https://www.postgresql.org/docs/18/app-pgcombinebackup.html) | Rebuilding a usable copy from incremental backups |
| [pg_rewind](https://www.postgresql.org/docs/18/app-pgrewind.html) | Core's only in-place block resync, for a copy that split off after failover |
| [File system level backup](https://www.postgresql.org/docs/18/backup-file.html) | The docs' rsync recipe and why it needs `--checksum` |
| [PostgreSQL 17.11 release notes](https://www.postgresql.org/docs/release/17.11/) and [commit f581fa7](https://github.com/postgres/postgres/commit/f581fa729d8e108fef853c3156267b1f753d0210) | Visibility-map bug that made incremental backups silently stale; the strongest argument for hashing |
| [walsummarizer.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/postmaster/walsummarizer.c) and [basebackup_incremental.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/backup/basebackup_incremental.c) | How Postgres tracks changed blocks, and the cases it admits it can't detect |

## Tools to benchmark against

| Source | Why it matters |
|---|---|
| [pgBackRest user guide](https://pgbackrest.org/user-guide.html) | Delta restore verifies by hash, but only from its own repository |
| [pgBackRest block incremental (Crunchy Data)](https://crunchydata.com/blog/pgbackrest-file-bundling-and-block-incremental-backup) | Block-level incremental and delta restore, with demo numbers |
| [WAL-G PostgreSQL docs](https://github.com/wal-g/wal-g/blob/master/docs/PostgreSQL.md) | Delta backups by page LSN; `catchup-send` / `catchup-receive` |
| [Barman recovery](https://docs.pgbarman.org/release/3.20.1/user_guide/recovery.html) | rsync-based delta restore into an existing directory |
| [rsync(1)](https://download.samba.org/pub/rsync/rsync.1) | `--checksum`, `--no-whole-file`, `--inplace`, `--block-size` for a fair baseline |
| [Ultra-fast replica creation (Postgres.ai)](https://postgres.ai/blog/20251105-postgres-marathon-2-012-ultra-fast-replica-creation-pgbackrest) | A full 1 TiB copy in 101 s over 75 Gbps: where our tool loses |

## How other systems solve it

| Source | Why it matters |
|---|---|
| [Oracle: refresh a physical standby](https://docs.oracle.com/en/database/oracle/oracle-database/19/spmss/refresh-a-physical-standby-db.html) | The one mainstream database that ships "send changed blocks to a stale standby" |
| [ScyllaDB row-level repair](https://www.scylladb.com/2019/08/13/scylla-open-source-3-1-efficiently-maintaining-consistency-with-row-level-repair/) | Hash-based replica sync: 120 GiB down to 4.3 GiB sent |
| [Cassandra repair](https://cassandra.apache.org/doc/latest/cassandra/managing/operating/repair.html) | Merkle-tree comparison between replicas |
| [Percona XtraBackup page tracking](https://docs.percona.com/percona-xtrabackup/8.0/page-tracking.html) | A change tracker that missed some writes and produced bad incrementals |
| [SQL Server automatic seeding](https://learn.microsoft.com/en-us/sql/database-engine/availability-groups/windows/automatic-seeding-secondary-replicas) | The cost of no refresh path: 5 TB over 1 Gb/s takes about 11 hours |

## Papers

| Paper | Why it matters | Copy |
|---|---|---|
| Tridgell and Mackerras, [The rsync algorithm](https://rsync.samba.org/tech_report/) (1996) | Rolling checksum plus strong hash; the baseline algorithm | Link |
| Muthitacharoen et al., [LBFS](https://pdos.csail.mit.edu/papers/lbfs:sosp01/lbfs.pdf) (SOSP 2001) | Origin of content-defined chunking; smaller chunks found more but cost more metadata | Link |
| Xia et al., [FastCDC](https://www.usenix.org/conference/atc16/technical-sessions/presentation/xia) (USENIX ATC 2016) | Fast content-defined chunking | Link |
| [Vectorized Sequence-Based Chunking for Data Deduplication](https://arxiv.org/abs/2505.21194) (2025) | Fastest CDC; includes a fixed-vs-CDC result on a "TPCC" dataset that a reviewer may cite | [Local copy](references/vectorized-seqcdc-2025.pdf) |
| Knauth and Fetzer, [dsync](https://usenix.org/conference/lisa13/technical-sessions/papers/knauth) (LISA 2013) | Tracking changed blocks beats scanning by up to 100× | Link |
| [SkySync](https://www.usenix.org/conference/fast26/presentation/zhang-zhihao) (FAST 2026) | Reusing stored checksums instead of rescanning | Link |
| [PandaSync](https://ranger.uta.edu/~jiang/publication/Conferences/2019/2019-ICDCS-PandaSync.pdf) (ICDCS 2019) | Choosing between full and delta sync adaptively | Link |
| Eppstein et al., [What's the Difference?](https://research.google/pubs/whats-the-difference-efficient-set-reconciliation-without-prior-context/) (SIGCOMM 2011) | Set reconciliation: messages sized to the difference, not the data | Link |
| Venkataramanan et al., [Interactive synchronization](https://arxiv.org/abs/1310.2026) (2013) | How few bits syncing can need in theory | Link |
| DeCandia et al., [Dynamo](https://www.allthingsdistributed.com/files/amazon-dynamo-sosp2007.pdf) (SOSP 2007) | Merkle-tree anti-entropy, the shape of our hash tree | Link |
| Xu et al., [dbDedup](https://pdl.cmu.edu/PDL-FTP/Database/xu-sigmod17_abs.shtml) (SIGMOD 2017) | CMU work on byte-level deltas for database records | Link |
| Phipps, [zsync](http://zsync.moria.org.uk/paper/) | Publishing per-block checksums once, like caching our hash tree | Link |
| O'Connor et al., [BLAKE3](https://github.com/BLAKE3-team/BLAKE3-specs) (2020) | The proposed keyed hash | [Local copy](references/blake3-spec.pdf) |
| [xxHash benchmarks](https://github.com/Cyan4973/xxHash) | Hash throughput comparison | Link |

## Storage and cloud context

| Source | Why it matters |
|---|---|
| [zfs-send(8)](https://openzfs.github.io/openzfs-docs/man/master/8/zfs-send.8.html) | Snapshot deltas only work with an untouched shared ancestor |
| [EBS direct APIs](https://docs.aws.amazon.com/ebs/latest/userguide/ebsapi-elements.html) | Cloud changed-block lists, in 512 KiB units, within one snapshot lineage |
| [AlloyDB storage (Google Cloud blog)](https://cloud.google.com/blog/products/databases/alloydb-for-postgresql-intelligent-scalable-storage/) | Why managed AlloyDB never needs to reuse an old copy |
| [AlloyDB Omni backups on Kubernetes](https://docs.cloud.google.com/alloydb/omni/kubernetes/16.11.0/docs/backup-kubernetes) | Omni is self-managed, so it is our natural target |
| [AlloyDB Omni container backups](https://docs.cloud.google.com/alloydb/omni/containers/16.3.0/docs/backup-overview) | Google recommends pgBackRest for Omni |
| [Aurora cloning](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Aurora.Managing.Clone.html) | Copy-on-write clones inside one storage system |
| [Impact of full page writes (EDB)](https://www.enterprisedb.com/blog/impact-full-page-writes) | Random keys can multiply changed pages and WAL 20× |
