# Cross-DBMS prior art: incremental physical backup and refreshing a stale physical copy (non-PostgreSQL), as of October 2026

Scope note: these notes cover "send only what changed to a destination that already holds an older physical copy" in MySQL/InnoDB, Oracle, SQL Server, MongoDB, Cassandra/ScyllaDB, CockroachDB, SQLite (sqlite3_rsync, Litestream/LiteFS). Every factual claim carries a link. Statements under "Inferences" are my reasoning, not sourced fact.

## 1. MySQL / InnoDB: XtraBackup, page tracking, MySQL Enterprise Backup, Clone plugin, Galera IST/SST

### Takeaway
MySQL has all three classic strategies side by side: (a) full scan comparing each page's LSN against the base backup LSN (XtraBackup default, MEB "full-scan"), (b) a server-maintained changed-page log (Percona's old XtraDB bitmaps, removed in 8.0.30, replaced by the upstream `mysqlbackup` page-tracking component, 8.0.17/8.0.18+), and (c) redo-log-only increments. The Clone plugin (8.0.17+) does not reuse data already on the recipient; it wipes and re-copies, though it uses page tracking internally to bring a fuzzy file copy forward. Galera's only "delta" path is IST (replay of cached write-sets); its SST falls back to full copy, except the `rsync_wan` SST method, which uses rsync delta transfer against an old datadir.

### Cited Findings

**XtraBackup incremental (LSN-based page scan)**
- Mechanism: "An incremental backup copies each page which LSN is newer than the previous incremental or full backup's LSN." — [Percona XtraBackup 8.0: Create an incremental backup](https://docs.percona.com/percona-xtrabackup/8.0/create-incremental-backup.html)
- Required metadata: each backup writes `xtrabackup_checkpoints` with `from_lsn`, `to_lsn` (last checkpoint LSN) and `last_lsn` (last copied LSN). Example: full `from_lsn=0, to_lsn=1626007`; inc1 `from_lsn=1626007, to_lsn=4124244`. An incremental's `from_lsn` must equal the base backup's `to_lsn`. A gap between `to_lsn` and `last_lsn` means there was write traffic during the backup. — [Percona XtraBackup 8.0: Create an incremental backup](https://docs.percona.com/percona-xtrabackup/8.0/create-incremental-backup.html)
- Output format: per-tablespace `.delta` files (e.g., `ibdata1.delta`, `test/table1.ibd.delta`) holding changed pages since the base LSN. — [same](https://docs.percona.com/percona-xtrabackup/8.0/create-incremental-backup.html)
- MyRocks gets no incremental benefit: XtraBackup "copies all the MyRocks files each time it takes a backup." — [same](https://docs.percona.com/percona-xtrabackup/8.0/create-incremental-backup.html)

**Percona Server "changed page tracking" (XtraDB bitmaps), now removed**
- Percona Server controlled it with the read-only variable `innodb_track_changed_pages`, and the bitmap files sped up XtraBackup incrementals. It was deprecated in Percona Server 8.0.27 and removed in 8.0.30, with Percona recommending upstream MySQL page tracking instead. — [Percona Server 8.0: XtraDB changed page tracking](https://docs.percona.com/percona-server/8.0/changed-page-tracking.html)
- XtraBackup could use the bitmap up to PXB 8.0.27. `--incremental-force-scan` "forces a read of all pages even if the bitmap file is available". PXB 8.0.30 removed that algorithm. — [Percona XtraBackup 8.0: Create an incremental backup](https://docs.percona.com/percona-xtrabackup/8.0/create-incremental-backup.html)

**Upstream MySQL page tracking (`component_mysqlbackup`) used by XtraBackup 8.0.27+ and MEB 8.0.18+**
- XtraBackup: "Percona XtraBackup 8.0.27 adds support for page tracking functionality for an incremental backup." The component provides a list of pages modified since the last backup, and XtraBackup copies only those, which "removes the need to scan the pages in the database". The gain depends on workload: it helps "if the majority of pages have not been modified." No benchmark numbers are given. — [Percona XtraBackup 8.0: Page tracking](https://docs.percona.com/percona-xtrabackup/8.0/page-tracking.html)
- Setup and control: `INSTALL COMPONENT "file://component_mysqlbackup";`, then `SELECT mysqlbackup_page_track_set(true);` and `mysqlbackup_page_track_get_start_lsn()`. Tracking data is written to a file under the datadir. `--page-tracking` on a backup resets tracking to that backup's start. — [Percona XtraBackup 8.0: Page tracking](https://docs.percona.com/percona-xtrabackup/8.0/page-tracking.html)
- Correctness caveats (important for any changed-block tracker):
  - Oracle MySQL bug 106163: with `LOCK=EXCLUSIVE`/`ALGORITHM=INPLACE` DDL, "Oracle MySQL Server does not write these DDL operations" to the redo log. This can produce an inconsistent backup that is detected only at `--prepare`. Percona Server fixed it in 8.0.27.
  - Known issue PS-8032: "you may generate a bad incremental backup using page tracking" when an index is built in place with an exclusive algorithm after the last checkpoint.
  - Page tracking requires the system tablespace to be a single file (ibdata1).
  - After purging tracking data, "you should create a full backup afterward."
  - A server restart creates a new tracking file but keeps the old one, which grows until tracking is stopped.
  - Tracked pages are processed in memory, and the backup errors out if memory is insufficient.
  — [Percona XtraBackup 8.0: Page tracking](https://docs.percona.com/percona-xtrabackup/8.0/page-tracking.html)

**MySQL Enterprise Backup (MEB) incremental modes**
- Four modes (`--incremental=` values):
  - redo-log-only (`--incremental-with-redo-log-only`): changes come from the InnoDB redo log.
  - page-track: uses the server page-tracking component. MEB 8.0.18+.
  - full-scan: scans all InnoDB data files for pages changed since the last backup. This was the default through 8.0.17.
  - optimistic: scans only data files whose modification time changed since the last backup.
  — [MySQL Enterprise Backup 8.0: Making an incremental backup](https://dev.mysql.com/doc/mysql-enterprise-backup/8.0/en/mysqlbackup.incremental.html)
- Version correction: the MEB docs date page-track to MEB 8.0.18 ("For MySQL Enterprise Backup 8.0.18 and later"). The page-tracking server component itself is part of MySQL 8.0.17+ (see the clone/page-tracking design below). Treat "8.0.17 vs 8.0.18" as server-component vs MEB-tool versions. — [MEB 8.0 incremental](https://dev.mysql.com/doc/mysql-enterprise-backup/8.0/en/mysqlbackup.incremental.html)
- Page-track performance claim: "In general, incremental backups using page tracking are faster than other kinds of incremental backups ... if the majority of the data in the database has not been modified." — [MEB 8.0 incremental](https://dev.mysql.com/doc/mysql-enterprise-backup/8.0/en/mysqlbackup.incremental.html)
- Page-track safeguards:
  - If tracking was not enabled before the base backup, or if changed pages are at least 50% of all pages, `--incremental=page-track` errors out. Plain `--incremental` silently falls back to full-scan.
  - Memory: 8 bytes per changed page, sorted in 16 MB buffers. The default 400 MB `--limit-memory` handles "about 800GB of changed data".
  - Purge API (8.0.28+): `mysqlbackup_page_track_purge_up_to(...)`.
  — [MEB 8.0 incremental](https://dev.mysql.com/doc/mysql-enterprise-backup/8.0/en/mysqlbackup.incremental.html)
- Redo-log-only trade-offs:
  - It "usually requires less I/O to read the changes from the redo log than to scan the InnoDB tablespace files".
  - It is less forgiving of too-low `--start-lsn`, and it does not support compression.
  - In 8.0.30+ you must register a redo-log consumer (`innodb_redo_log_consumer_register()` / `_advance($lsn)`) so the needed redo is retained. Before that, if the redo had wrapped, mysqlbackup errors and scripts must fall back to `--incremental`.
  - With huge redo logs, reading redo "could take as long as reading the InnoDB data files."
  — [MEB 8.0 incremental](https://dev.mysql.com/doc/mysql-enterprise-backup/8.0/en/mysqlbackup.incremental.html)
- Optimistic mode safeguard: it depends on file mtimes, so "the system time on the server, and ... the location of the data directory" must be unchanged, "Otherwise, the backup might fail, or an inconsistent incremental backup might be produced." — [MEB 8.0 incremental](https://dev.mysql.com/doc/mysql-enterprise-backup/8.0/en/mysqlbackup.incremental.html)

**MySQL Clone plugin (remote clone for provisioning replicas)**
- Supported in MySQL 8.0.17+. By default, remote clone "removes user-created data (schemas, tables, tablespaces) and binary logs from the recipient data directory, clones the new data ... and automatically restarts the server." The only way to keep existing data is `DATA DIRECTORY = '/path'`, which requires a new, non-existent directory, so no reuse of an old copy is possible. — [MySQL 8.0 Ref Manual: Cloning Remote Data](https://dev.mysql.com/doc/refman/8.0/en/clone-plugin-remote.html)
- Transfer in 1 MB packets plus metadata (`max_allowed_packet` ≥ 2 MB). One clone at a time. Concurrent DDL on the donor has been permitted by default since 8.0.27. — [MySQL 8.0: Cloning Remote Data](https://dev.mysql.com/doc/refman/8.0/en/clone-plugin-remote.html)
- Internal stages: DROP DATA, FILE COPY, PAGE_COPY, REDO_COPY, FILE_SYNC, RESTART, RECOVERY. PAGE_COPY sends pages modified during the file copy. The count is known once FILE COPY completes. — [MySQL 8.4 Ref Manual: Monitoring cloning (clone_progress)](https://docs.oracle.com/cd/E17952_01/mysql-8.4-en/clone-plugin-monitoring.html)
- Design (WL#9209):
  - Modified pages could be tracked either when added to the flush list or when flushed to disk. The design chose flush-to-disk for consistency with checkpointing and to avoid mini-transaction contention.
  - Before moving from page copy to redo copy, "Redo Archiving" is stopped at the "CLONE LSN".
  - So clone = fuzzy file copy + tracked-dirty-page copy + archived redo. This tracking infrastructure is what the `mysqlbackup` page-tracking component exposes.
  — [MySQL WL#9209: InnoDB: Clone local replica](https://dev.mysql.com/worklog/task/?id=9209); see also [Facebook MySQL wiki: Clone Plugin Background and Internals](https://github.com/facebook/mysql-5.6/wiki/Clone-Plugin-Background-and-Internals) (fork-specific, lower authority)

**Galera (MariaDB Galera / Percona XtraDB Cluster): IST vs SST**
- IST is only possible if the joiner was previously a member (matching state UUID) and the donor still holds every missing write-set in its GCache, looked up by the joiner's last seqno. Otherwise an SST starts. — [MariaDB docs: Rapid node recovery with IST and the GCache](https://mariadb.com/docs/galera-cluster/high-availability/rapid-node-recovery-with-ist-and-the-gcache); [Galera docs: State Transfers](https://galeracluster.com/library/documentation/state-transfer.html)
- "the most important parameter for Incremental State Transfers is gcache.size on the donor node". Restarting a donor historically wiped the gcache, forcing SST. `gcache.recover=yes` (Galera 3.19+) is best-effort recovery after graceful shutdown only. The fallback log message is "IST first seqno N not found from cache, falling back to SST". — [Galera docs: State Transfers](https://galeracluster.com/library/documentation/state-transfer.html); [Percona blog: Galera Cache (gcache) is finally recoverable on restart](https://www.percona.com/blog/galera-cache-gcache-finally-recoverable-restart/)
- SST is "a full data copy from a donor". Methods:
  - mariadb-backup: physical and non-locking.
  - rsync: blocks the donor with a read lock. Described as "the fastest SST method, especially for large datasets". It runs `rsync` in `--whole-file` mode, so no delta transfer.
  - `rsync_wan`: uses rsync's "default delta transfer mode", which "may reduce data transfer time substantially when an older datadir state is already present on the joiner node".
  - mysqldump: logical and slowest.
  — [MariaDB docs: Introduction to SSTs](https://mariadb.com/docs/galera-cluster/high-availability/state-snapshot-transfers-ssts-in-galera-cluster/introduction-to-state-snapshot-transfers-ssts/)

### Inferences
- MySQL is the closest analogue to PostgreSQL's situation: page LSN in every page header (like PG's `pd_lsn`), plus an opt-in server-side changed-page log. The page-tracking caveats (DDL paths that bypass redo, restart/purge discontinuities, requiring a fresh full backup after tracking gaps) are a ready-made checklist of correctness hazards for a PG delta tool that relies on WAL summaries or page LSNs.
- MEB's ">=50% changed => refuse page-track" rule is a pragmatic threshold where a tracked delta stops paying off versus a full copy.
- `rsync_wan` is the only MySQL-ecosystem mechanism I found that reuses an existing stale datadir. It does so generically, through rsync's rolling checksums, with no use of LSNs. Clone and MongoDB-style resync both wipe the destination.

### Gaps
- No published benchmark numbers from Percona or Oracle for page-track vs full-scan incremental speedup. Both sources give only qualitative claims.
- Whether MySQL 8.4/9.x added a "clone into an existing datadir and send only changed pages" mode: not found in the 8.0 remote-clone docs. I did not check 9.x release notes exhaustively.
- Exact server version in which the `component_mysqlbackup` page-tracking functions first shipped (8.0.17 is widely cited alongside clone, but MEB docs date usage to 8.0.18). Not confirmed from a server release note.

## 2. Oracle: RMAN incremental, Block Change Tracking, and rolling forward a stale standby (RECOVER ... FROM SERVICE)

### Takeaway
Oracle has both a scan-based and a bitmap-based way to find changed blocks. Without BCT, RMAN reads every block and filters by SCN. With BCT, a small bitmap file (about 1/30,000 of DB size) retains 8 bitmaps of history. On top of RMAN incremental-from-SCN, Oracle provides exactly the "refresh a stale copy by sending changed blocks" operation: `RECOVER DATABASE FROM SERVICE ... NOREDO` (12c) and the one-command `RECOVER STANDBY DATABASE FROM SERVICE` (18c+). The primary builds an incremental backup starting at the SCN in the standby's datafile headers, ships it over Oracle Net, and applies it.

### Cited Findings
- Incremental definition: "An incremental backup copies only data file blocks that have changed since a specified previous backup". Incrementals are differential by default. A level 0 incremental is the base of an incremental strategy, and a plain full backup is not usable as one. — [Oracle 19c Backup and Recovery User's Guide: Backing Up the Database](https://docs.oracle.com/en/database/oracle/oracle-database/19/bradv/backing-up-database.html)
- BCT mechanism: "If block change tracking is enabled on a primary or standby database, then RMAN uses a block change tracking file to identify changed blocks ... By reading this small bitmap file ... RMAN avoids having to scan every block in the data file". "The database performs a bitmap switch before each backup". "The first level 0 incremental backup scans the entire data file." — [Oracle 19c B&R Guide: Using Block Change Tracking](https://docs.oracle.com/en/database/oracle/oracle-database/19/bradv/backing-up-database.html)
- BCT history limit:
  - Space is managed "to retain block change data that covers the eight most recent backups". After 8 bitmaps, the oldest is overwritten.
  - "An incremental backup can be optimized only when it is based on a parent backup that was made after the start of the oldest bitmap".
  - Example: level 0 followed by seven differential level 1s, then a cumulative level 1. That cumulative backup cannot use BCT.
  — [Oracle 19c B&R Guide](https://docs.oracle.com/en/database/oracle/oracle-database/19/bradv/backing-up-database.html)
- BCT size: "approximately 1/30,000 the size of the data blocks to be tracked" (times redo threads for RAC). It starts at 10 MB and grows in 10 MB increments, with a minimum of 320 KB per datafile. Size is "not related to the frequency of updates". — [Oracle 19c B&R Guide](https://docs.oracle.com/en/database/oracle/oracle-database/19/bradv/backing-up-database.html)
- BCT safeguards:
  - "RMAN does not support backup and recovery of the change tracking file. The database resets the change tracking file when it determines that the change tracking file is invalid."
  - After a restore/recover, BCT resets, and only after a new level 0 can the next incremental use it.
  - Disabling and re-enabling BCT loses its contents.
  — [Oracle 19c B&R Guide](https://docs.oracle.com/en/database/oracle/oracle-database/19/bradv/backing-up-database.html)
- BCT on a standby requires an Active Data Guard license. Enabling it on a standby takes effect when managed recovery restarts. — [Oracle 19c B&R Guide](https://docs.oracle.com/en/database/oracle/oracle-database/19/bradv/backing-up-database.html)
- BCT benefit claim: "the benefits of avoiding full data file scans during backup are considerable, especially if only a small percentage of data blocks are changed between backups". It is disabled by default. — [Oracle 19c B&R Guide](https://docs.oracle.com/en/database/oracle/oracle-database/19/bradv/backing-up-database.html)
- 12c roll-forward command: `recover database from service <primary> noredo using compressed backupset section size 100m;`. It "Creates an incremental backup containing the changes to the primary database", including changes "beginning with the SCN in the standby data file header". It "Transfers the incremental backup over the network to the physical standby" and applies it. — [Redgate Simple Talk: Rolling Forward a Physical Standby Database Using the RECOVER Command](https://www.red-gate.com/simple-talk/databases/oracle-databases/rolling-forward-a-physical-standby-database-using-the-recover-command/)
- 12c granularity in practice: datafiles whose checkpoint SCN already matched the primary were skipped ("skipping datafile 5; already restored to SCN 1910129"). The control file was not updated ("the standby control file still contains old SCN values which are lower than the SCN values in the standby data files"), so a separate `RESTORE STANDBY CONTROLFILE FROM SERVICE` was needed. — [Redgate Simple Talk](https://www.red-gate.com/simple-talk/databases/oracle-databases/rolling-forward-a-physical-standby-database-using-the-recover-command/)
- 18c+: one command, `RECOVER STANDBY DATABASE FROM SERVICE primary_db;`. "The standby database is restarted after the roll forward operation". You then restart managed recovery (or broker `APPLY-ON`). Prerequisites: Oracle Net connectivity to the primary, identical password files, `COMPATIBLE` ≥ 12.0. — [Oracle 19c: Steps to Refresh a Physical Standby Database with Changes Made to the Primary Database](https://docs.oracle.com/en/database/oracle/oracle-database/19/spmss/refresh-a-physical-standby-db.html)
- 18c automates what 12c left manual, including the control-file refresh step. — [DEV Community: Resolving missing log gaps in Data Guard](https://dev.to/vahidusefzadeh/resolving-missing-log-gaps-in-data-guard-230b); [virtual-dba.com](https://virtual-dba.com/?p=37537) (third-party, moderate confidence)
- Pre-12c manual procedure: take an incremental backup on the primary starting from the standby's lowest datafile-header SCN, then catalog and apply it on the standby. — [Rackspace: Roll a standby database forward with RMAN](https://docs.rackspace.com/blog/roll-standby-database-forward-with-rman)
- Use case: closing large redo gaps or unresolvable missing archived logs, documented in MOS Doc 2850185.1 (12.1–21c). Clean up backups from orphaned incarnations first. — [modb.pro mirror of MOS note](https://www.modb.pro/doc/117183) (mirror of a paywalled note, lower confidence)
- Targeted block repair: Data Guard also offers `RECOVER ... NONLOGGED BLOCK` to replace standby blocks invalidated by NOLOGGING operations with blocks from the primary. — [Oracle 19c Data Guard Concepts: Managing Physical Standby Databases](https://docs.oracle.com/en/database/oracle/oracle-database/19/sbydb/managing-oracle-data-guard-physical-standby-databases.html)

### Inferences
- Oracle's design reduces "what does the destination have?" to one number per datafile: the datafile-header checkpoint SCN on the standby. The source then emits every block with block SCN > that value. This is the direct analogue of a PG approach that reads the stale copy's checkpoint/redo LSN (from `pg_control`) and sends every page whose `pd_lsn` exceeds it, or uses WAL summaries for that LSN range. It relies on SCNs being monotonic within one incarnation, hence the warnings about orphaned incarnations and RESETLOGS.
- Whether RECOVER FROM SERVICE uses the primary's BCT file is not documented in what I read. Since the base is the standby's SCN rather than an RMAN backup recorded in BCT, it likely falls back to a full block scan on the primary unless the SCN falls within the BCT bitmap window. This is unverified.

### Gaps
- The exact Oracle doc wording that a non-BCT incremental reads each block and compares the block-header SCN to the parent's checkpoint SCN. This is widely known and implied by "avoids having to scan every block", but I did not fetch the RMAN concepts page that states it.
- No Oracle-published numbers for BCT speedup or CTWR runtime overhead in the docs I fetched.
- Whether RECOVER STANDBY DATABASE FROM SERVICE leverages BCT, and how it handles datafiles added on the primary in 18c+. The official page lists no restrictions or internals.

## 3. Microsoft SQL Server: differential backups (DCM), log shipping, Always On reseeding

### Takeaway
SQL Server tracks changes with an always-on, per-extent (64 KiB) Differential Changed Map bitmap per ~4 GiB GAM interval, reset by full backups. Differential backups read only flagged extents. For replicas, automatic seeding (2016+) streams a full VDI backup over the AG endpoint. I found no documented "partial reseed" that reuses a stale secondary's data files. The only incremental catch-up is log-based: restore the backup/log chain manually, then let seeding or log transport catch up.

### Cited Findings
- Differential granularity and mechanism: a differential captures "the state of any extents (collections of eight physically contiguous pages) that have changed" since the base. It "relies on a bitmap page that contains a bit for every extent". "Generally, the older a base is, the larger a new differential backup will be", and differentials "can eventually approach the differential base in size". — [Microsoft Learn: Differential Backups (SQL Server)](https://learn.microsoft.com/en-us/sql/relational-databases/backup-restore/differential-backups-sql-server)
- Copy-only full backups do not update the differential bitmap, so they don't reset the base. — [Microsoft Learn: Differential Backups](https://learn.microsoft.com/en-us/sql/relational-databases/backup-restore/differential-backups-sql-server)
- DCM layout:
  - "Differential backups read the DCM pages to determine which extents have been modified ... The length of time that a differential backup takes is proportional to the number of extents modified since the last full database backup, and not the total size of the database."
  - DCM/BCM pages sit at each GAM interval of about 64,000 extents (about 4 GiB), with 1 bit per extent.
  - Extents are 8 × 8 KiB pages = 64 KiB.
  — [Microsoft Learn: Page and Extent Architecture Guide](https://learn.microsoft.com/en-us/sql/relational-databases/pages-and-extents-architecture-guide)
- Read-only database edge case (metadata safeguard):
  - For read-only DBs, the differential base LSN is stored in `master` (`backupset.differential_base_lsn`). The bitmap may over-report changes, and the extra data is read but filtered out using `differential_base_lsn`.
  - If a read-only DB is rebuilt, restored, or detached and attached, differential-base info is lost, and "The SQL Server Database Engine can't detect or prevent this problem".
  — [Microsoft Learn: Differential Backups](https://learn.microsoft.com/en-us/sql/relational-databases/backup-restore/differential-backups-sql-server)
- Automatic seeding:
  - Introduced in SQL Server 2016. It "uses the log stream transport to stream the backup using VDI to the secondary replica for each database ... using the configured endpoints".
  - Before 2016, the only way was "backup, copy, and restore".
  — [Microsoft Learn: Automatic seeding for secondary replicas](https://learn.microsoft.com/en-us/sql/database-engine/availability-groups/windows/automatic-seeding-secondary-replicas)
- Seeding performance and trade-offs:
  - Worked example: 5 TB over 1 Gb/s (125 MB/s) takes "just over 11 hours", and is slower in practice.
  - The primary's log "cannot be truncated until automatic seeding of that database is complete".
  - Seeding is "a single-threaded process that can handle up to five databases".
  - Compression is off by default and enabled with trace flag 9567, trading CPU for bandwidth.
  — [Microsoft Learn: Automatic seeding](https://learn.microsoft.com/en-us/sql/database-engine/availability-groups/windows/automatic-seeding-secondary-replicas)
- Hybrid catch-up: "first restore the database on a secondary replica including all available transaction logs. Next, enable automatic seeding ... to 'catch up' the secondary replica's database, as if a tail-log backup were restored". — [Microsoft Learn: Automatic seeding](https://learn.microsoft.com/en-us/sql/database-engine/availability-groups/windows/automatic-seeding-secondary-replicas)

### Inferences
- The DCM is SQL Server's equivalent of Oracle BCT and MySQL page tracking. It differs by being always on, embedded in the data files at fixed intervals, coarse-grained (64 KiB extents), and holding exactly one base (since the last non-copy-only full). By contrast, Oracle keeps 8 bitmaps and PG's WAL summaries cover arbitrary LSN ranges. Single-base means you cannot ask "what changed since an arbitrary older copy".
- SQL Server has no documented equivalent of Oracle's "roll forward stale standby from changed blocks". A diverged or far-behind AG secondary is reseeded in full, or brought forward with a manually restored differential plus log chain. A differential taken against the stale secondary's base could serve as a block-level refresh, but only if that base is still the current differential base.

### Gaps
- Log shipping: not researched in depth. It is a log-backup/restore chain and not a block-delta mechanism.
- SQL Server 2022/2025 features for partial or incremental reseeding: none found. I did not search SQL Server 2025 release notes specifically.
- Published numbers for differential backup speedup vs full: none beyond the qualitative DCM statement.

## 4. MongoDB: initial sync (logical vs file-copy-based), resync of a stale member

### Takeaway
MongoDB has no block- or file-level delta resync. A member that falls off the oplog is "stale" and must be fully resynchronized: its dbPath is wiped and it runs an initial sync, either logical (document copy + index build + oplog replay) or, in Enterprise since 5.2, file-copy-based (FCBIS). The alternative is seeding it from a recent snapshot of another member's files. The only "incremental" catch-up is oplog replay within the oplog window.

### Cited Findings
- Logical initial sync:
  - Clones every database except `local`, scanning every collection on the source and inserting into local copies.
  - Builds indexes in parallel with the copy.
  - Applies buffered oplog, then catches up from the source oplog.
  - If a continuity gap is found during steps 3–4, it restarts initial sync from the beginning.
  — [MongoDB Manual: Replica Set Data Synchronization](https://www.mongodb.com/docs/manual/core/replica-set-sync/)
- File copy based initial sync:
  - "Available in MongoDB Enterprise only". "Starting in MongoDB 5.2, initial syncs can be logical or file copy based".
  - It "runs the initial sync process by copying and moving files on the file system" and "can be faster than logical initial sync". It is enabled with startup-only `initialSyncMethod=fileCopyBased`.
  - It replaces the destination's `local` database with the source's.
  - Limitations: no backups on source or destination during sync, and only one source at a time.
  — [MongoDB Manual: Replica Set Data Synchronization](https://www.mongodb.com/docs/manual/core/replica-set-sync/)
- Resume and retry:
  - Transient errors can be resumed for 24 h by default (`initialSyncTransientErrorRetryPeriodSeconds`).
  - After that, it restarts from the beginning with a new source.
  - Up to 10 restart attempts.
  — [MongoDB Manual: Replica Set Data Synchronization](https://www.mongodb.com/docs/manual/core/replica-set-sync/)
- The current manual references MongoDB 9.0 (phase reporting in `replSetGetStatus.initialSyncStatus.phase`), which confirms the doc is current for 2026. — [MongoDB Manual: Replica Set Data Synchronization](https://www.mongodb.com/docs/manual/core/replica-set-sync/)
- Staleness: a member becomes stale "when its replication process falls so far behind that the primary overwrites oplog entries the member has not yet replicated ... you must completely resynchronize the member by removing its data and performing an initial sync". "During initial sync, mongod removes the contents of the dbPath directory." — [MongoDB Manual: Resync a Member of a Self-Managed Replica Set](https://www.mongodb.com/docs/manual/tutorial/resync-replica-set-member/)
- Alternative seeding: copy a recent data directory (snapshot only, not mongodump, and including `local`) from another member. It "can replace the data more quickly but requires more manual steps". The copy must be recent enough to catch up via the oplog. — [MongoDB Manual: Resync a Member](https://www.mongodb.com/docs/manual/tutorial/resync-replica-set-member/)

### Inferences
- MongoDB is the clearest example of the "no delta resync" baseline. Its correctness model is oplog continuity, and when continuity is lost it discards everything. A PG delta-resync tool would improve on this pattern by using physical page identity (stable block numbers) and LSNs, which WiredTiger's B-tree files and MongoDB's logical-copy model do not offer at the replica-protocol level.

### Gaps
- Internals of FCBIS (backup cursor usage, whether it can reuse unchanged WiredTiger files on the destination): not documented on the manual page. The docs imply it wipes, but this is not explicitly stated for FCBIS.
- No MongoDB-published speedup numbers for FCBIS vs logical sync found.

## 5. Other systems: Cassandra/ScyllaDB anti-entropy, CockroachDB snapshots, SQLite sqlite3_rsync, Litestream/LiteFS

### Takeaway
The systems that sync replicas by content hashing are the ones with no shared, monotonic physical page identity between copies. Cassandra and ScyllaDB replicas hold differently compacted SSTables, so they compare data hashes (Cassandra: Merkle trees over token ranges; ScyllaDB 3.1+: per-row hashes with set reconciliation). sqlite3_rsync (SQLite 3.47.0, Oct 2024) hashes pages, because two SQLite files share page numbering but have no global LSN. CockroachDB Raft snapshots send whole ranges (no delta). Litestream/LiteFS ship page-level change files (LTX), which is change capture rather than diffing a stale copy.

### Cited Findings

**sqlite3_rsync**
- Introduced as "the experimental sqlite3_rsync program" in SQLite 3.47.0, released 2024-10-21. — [SQLite 3.47.0 release notes](https://sqlite.org/releaselog/3_47_0.html)
- Usage: makes REPLICA a copy of ORIGIN, locally or over ssh (`USER@HOST:PATH`). Both DBs may be live during the sync. The replica ends as a consistent snapshot of the origin as of the start of the command, and is read-only (no writes) during the sync. — [sqlite.org: sqlite3_rsync](https://sqlite.org/rsync.html)
- Algorithm: the replica sends cryptographic hashes for pages or groups of pages. The origin returns content for differing pages. When a multi-page hash mismatches, the origin requests finer-grained hashes (multi-level). The protocol version is negotiated at startup, and the docs describe it as "similar to rsync". — [sqlite.org: sqlite3_rsync](https://sqlite.org/rsync.html)
- Bandwidth claims:
  - For similar DBs, total traffic is "often less than 0.01% of the database size". Example: a 500 MB DB syncs with about 20 KB.
  - For very different DBs, hash overhead can exceed the DB size by "a few percent at most".
  - Before 3.50.0, per-page hashing meant traffic was usually at least about 0.5% of DB size even for identical DBs.
  — [sqlite.org: sqlite3_rsync](https://sqlite.org/rsync.html)
- 3.50.0 (2025-05-29):
  - Removed the requirements that both DBs be in WAL mode and have the same page size, and added group-level hashing.
  - Mixing 3.50.0+ with ≤3.49.1 can hang (missing fflush); the workaround is `--protocol 1`.
  — [sqlite.org: sqlite3_rsync](https://sqlite.org/rsync.html)
- Output is "not an exact file copy": the content is identical, but the change counter (bytes 24–27) and version-valid-for (bytes 96–99) header fields may differ. — [sqlite.org: sqlite3_rsync](https://sqlite.org/rsync.html)

**Apache Cassandra repair**
- Repair compares replicas over shared token ranges using Merkle trees ("a hierarchy of hashes") and streams the out-of-sync sections. Full repair covers all data in the range. Incremental repair (the default) covers only data written since the previous incremental repair. Repaired data is not re-checked, so occasional full repairs are still needed for disk corruption and bugs. — [Apache Cassandra docs: Repair](https://cassandra.apache.org/doc/latest/cassandra/managing/operating/repair.html)
- Repair is I/O-heavy and run via nodetool. `-prv` (preview) builds Merkle trees and estimates streaming without transferring data. — [Apache Cassandra docs: Repair](https://cassandra.apache.org/doc/latest/cassandra/managing/operating/repair.html)
- Merkle-tree resolution vs overstreaming:
  - Older docs: fixed depth 15 (32K leaves). Overstreaming happens because trees have finite resolution.
  - Later 3.11+ commits added `repair_session_max_tree_depth` (default 18, about 50 MiB per tree; depth 15 is about 6 MiB; minimum 10). Lower depth means coarser leaves and more redundant streaming.
  - CASSANDRA-14096/15202 address coordinator heap pressure from holding all trees.
  — [DataStax Cassandra 3.x docs: Manual repair: Anti-entropy repair](https://docs.datastax.com/en/cassandra/3.x/cassandra/operations/opsRepairNodesManualRepair.html); [CASSANDRA-15202](https://jira.apache.org/jira/browse/CASSANDRA-15202); commit diff via [gitlink mirror](https://cdn09022024.gitlink.org.cn/natas_hw/cassandra/commit/126853068d6cd4d1f487a32defb25c3c452a42cf) (mirror; moderate confidence on exact numbers)

**ScyllaDB row-level repair (3.1, 2019)**
- Old design: checksum per sub-range of about 100 partitions, so "a mismatch of a single row in any of the 100 partitions causes 100 partitions to be transferred". "A difference of just a single byte can easily cause 10GB of data to be streamed" (illustrative, not a benchmark). — [ScyllaDB blog: Row-level repair (3.1)](https://www.scylladb.com/2019/08/13/scylla-open-source-3-1-efficiently-maintaining-consistency-with-row-level-repair/)
- New design: per-row checksums plus set reconciliation, so only mismatched rows are exchanged. Data is read once into a buffer and used for both hashing and sending. The hash moved from SHA-256 to a 64-bit non-cryptographic hash, with 16 ranges repaired in parallel. — [ScyllaDB blog](https://www.scylladb.com/2019/08/13/scylla-open-source-3-1-efficiently-maintaining-consistency-with-row-level-repair/)
- Benchmark: 3 nodes × i3.4xlarge, 1 B rows/node (1 TB, 1 KB rows).

  | Scenario | 3.0 | 3.1 | Speedup |
  |---|---|---|---|
  | 0% synced | 49.0 min | 37.07 min | 1.32× |
  | 100% synced | 47.6 min | 9.8 min | 4.85× |
  | 99.9% synced | 96.4 min | 14.2 min | 6.78× |

  In the 99.9% case, TX fell from 120.52 GiB to 4.28 GiB and RX from 60.09 GiB to 2.14 GiB (3.6%). — [ScyllaDB blog](https://www.scylladb.com/2019/08/13/scylla-open-source-3-1-efficiently-maintaining-consistency-with-row-level-repair/)
- Repair-based node operations reuse this mechanism for bootstrap and decommission, and a failed operation can resume "without sending data that has already been synced". — [ScyllaDB blog: Faster, safer node operations with repair vs streaming](https://www.scylladb.com/2023/12/07/faster-safer-node-operations-with-repair-vs-streaming/); [ScyllaDB docs: Repair-based node operations](https://opensource.docs.scylladb.com/master/operating-scylla/procedures/cluster-management/repair-based-node-operation.html)

**CockroachDB**
- A Raft replica snapshot "copies all of its data as of a specific timestamp". The recipient then replays Raft log entries after that point. Snapshots are sent over gRPC during rebalancing. — [CockroachDB docs (v2.1): Replication Layer](https://www.cockroachlabs.com/docs/v2.1/architecture/replication-layer)
- Snapshots are needed when a follower falls behind the leader's log truncation point. The default max range size is 512 MiB. — [Netdata guide: CockroachDB Raft snapshot storm](https://www.netdata.cloud/guides/cockroachdb/cockroachdb-raft-snapshot-storm/) (third-party)

**Litestream / LiteFS**
- LTX files "represent ordered ranges of pages". Since v0.5.0 each page is compressed separately, with an index at the end.
- Compaction levels: L1 covers 30 s windows, L2 5 min, L3 1 h.
- Restore replays LTX "newest to oldest, skipping duplicate pages (newer wins)". A monotonically increasing TXID enables PITR with "only a dozen or so files on average".
- If Litestream detects a gap between the database and its shadow-WAL backup, it re-snapshots. There is no documented in-place incremental refresh of a stale copy.
— [Fly.io blog: Litestream v0.5.0 is Here](https://fly.io/blog/litestream-v050-is-here/)

### Inferences
- sqlite3_rsync is the most directly comparable prior art for a "hash-based PG delta resync" variant. It relies on a stable page numbering shared between source and stale copy (true for PG relation-file block numbers too), uses hierarchical hashing to keep bandwidth near zero when copies are similar, and needs no history or metadata from the source. Its price: both sides must read the whole DB to hash it (I/O ∝ DB size), and the pre-3.50 per-page scheme had about a 0.5% traffic floor. PG has per-page LSNs, so an LSN filter is possible on the source side, which sqlite3_rsync does not have.
- The ScyllaDB numbers illustrate the "granularity vs hash overhead" trade-off. Coarse checksums over large units overstream massively (120 GiB sent for 0.1% divergence). Fine-grained hashing plus reconciliation cut that about 28×.

### Gaps
- The sqlite3_rsync hash function and digest size, and exact protocol message types. The SQLite Fossil source page blocked automated fetch and the GitHub raw path returned 404.
- Whether current CockroachDB (v25/v26) snapshots are streamed as SSTs or batches. A 2017 issue proposed SST streaming ([cockroach#16954](https://github.com/cockroachdb/cockroach/issues/16954)), but I did not confirm current behavior. Either way, they are full-range copies, not deltas.
- TiDB/TiKV snapshot transfer: not researched.
- LiteFS-specific details (FUSE-based, LTX checksums): not fetched directly.

## 6. Cross-cutting patterns and published performance numbers

### Takeaway
Three families recur:
- **Header-LSN/SCN scan.** No extra metadata, but full read I/O. Examples: XtraBackup default, MEB full-scan, RMAN without BCT, Oracle RECOVER FROM SERVICE keyed off the standby header SCN.
- **Server-maintained change bitmaps/logs.** Cheap reads, but they need history retention, reset rules and bug-free coverage of every write path. Examples: Oracle BCT (8 bitmaps), SQL Server DCM (1 base), MySQL page tracking, old Percona bitmaps.
- **Content hashing.** History-free and robust to divergence, but both sides must read everything. Examples: sqlite3_rsync, Cassandra Merkle trees, ScyllaDB row hashes, rsync_wan.

The only systems that refresh an existing stale physical copy in place are Oracle (RECOVER FROM SERVICE), Galera IST/rsync_wan, and sqlite3_rsync. MySQL Clone, MongoDB, SQL Server AG seeding and CockroachDB all re-send everything.

### Cited Findings
- Bitmap trackers bound scan cost to the change volume, e.g. differential time "proportional to the number of extents modified ... and not the total size of the database". — [Microsoft Learn: Page and Extent Architecture Guide](https://learn.microsoft.com/en-us/sql/relational-databases/pages-and-extents-architecture-guide)
- Bitmap trackers have bounded history (Oracle: 8 bitmaps) and reset on restore. — [Oracle 19c B&R Guide](https://docs.oracle.com/en/database/oracle/oracle-database/19/bradv/backing-up-database.html)
- Change trackers can miss writes that bypass the logging path: MySQL bug 106163 and PS-8032, with in-place DDL producing bad incrementals. — [Percona XtraBackup 8.0: Page tracking](https://docs.percona.com/percona-xtrabackup/8.0/page-tracking.html)
- Tracker-based modes include fallback thresholds (MEB refuses page-track at ≥50% changed) and mtime-based shortcuts with explicit clock/location preconditions. — [MEB 8.0 incremental](https://dev.mysql.com/doc/mysql-enterprise-backup/8.0/en/mysqlbackup.incremental.html)
- Content hashing bandwidth: under 0.01% of DB size for similar DBs (500 MB synced with about 20 KB). Overhead is at most a few percent for totally different DBs. — [sqlite.org: sqlite3_rsync](https://sqlite.org/rsync.html)
- Hash granularity matters: ScyllaDB 3.0's 100-partition checksums sent 120.52 GiB vs 4.28 GiB with row-level hashing, for 99.9%-synced replicas. — [ScyllaDB blog](https://www.scylladb.com/2019/08/13/scylla-open-source-3-1-efficiently-maintaining-consistency-with-row-level-repair/)
- Merkle-tree depth trades memory against overstreaming: depth 18 is about 50 MiB per tree, depth 15 about 6 MiB, and lower depth means more redundant streaming. — [gitlink mirror of Cassandra commit](https://cdn09022024.gitlink.org.cn/natas_hw/cassandra/commit/126853068d6cd4d1f487a32defb25c3c452a42cf); [DataStax 3.x repair docs](https://docs.datastax.com/en/cassandra/3.x/cassandra/operations/opsRepairNodesManualRepair.html)
- The cost of full reseed is spelled out by Microsoft: 5 TB over 1 Gb/s takes about 11 h or more, and the primary log cannot truncate meanwhile. — [Microsoft Learn: Automatic seeding](https://learn.microsoft.com/en-us/sql/database-engine/availability-groups/windows/automatic-seeding-secondary-replicas)
- Delta-aware generic file sync is available as an explicit option in Galera (`rsync_wan`) precisely for the case "when an older datadir state is already present on the joiner node". The default `rsync` SST uses `--whole-file`, because delta checks cost more than they save on fast LANs. — [MariaDB docs: Introduction to SSTs](https://mariadb.com/docs/galera-cluster/high-availability/state-snapshot-transfers-ssts-in-galera-cluster/introduction-to-state-snapshot-transfers-ssts/)

### Inferences
- Why content hashing is used where it is:
  - Cassandra and ScyllaDB replicas are not physically identical (independent compaction), so no shared page ID or LSN exists, and only logical content hashes can be compared.
  - sqlite3_rsync wants zero dependence on history or metadata, works between any two files, and must tolerate arbitrary divergence (including a replica written independently).
  - In both cases, hashing turns "unknown divergence" into a bounded, verifiable comparison.
- Why LSN/SCN or bitmaps are used elsewhere: Oracle, MySQL and SQL Server copies share physical block identity and a monotonic log position with the source (same incarnation/timeline). So "everything newer than X" is exact and needs no destination-side read.
- Implication for a PG delta tool. A defensible design space mirrors these three families:
  - (1) Read the stale copy's `pg_control` checkpoint LSN, then send source pages with `pd_lsn` greater than it (Oracle RECOVER FROM SERVICE / XtraBackup analogue).
  - (2) Use WAL summaries or a tracker to avoid the source-side full scan (BCT/page-track analogue).
  - (3) Hash-compare pages when timelines diverged or LSN trust is broken (sqlite3_rsync analogue).
  - The correctness checklist from prior art: timeline/incarnation checks (Oracle orphaned incarnations), non-WAL-logged writes (MySQL in-place DDL bug; PG unlogged tables, hint-bit-only changes without `wal_log_hints`), tracker discontinuities after restart/purge/restore (requires a new full base), and a size threshold to fall back to full copy (MEB 50%).
  - These PG-specific mappings are my inferences and are not sourced here.

### Gaps
- No vendor-published benchmark quantifying Oracle BCT or MySQL page-track speedups. Only qualitative claims were found in official docs.
- No independent benchmark of sqlite3_rsync beyond the SQLite project's own figures.
- No academic survey comparing these three families across DBMSs was located in this pass.
