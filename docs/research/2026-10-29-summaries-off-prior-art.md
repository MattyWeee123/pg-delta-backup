# Finding changed pages without WAL summaries: prior art

Sources were read on 2026-10-29 at these commits: PostgreSQL `REL_17_STABLE` 85a3871 (stamped 17.11), wal-g 924c478, pg_probackup 8f52525, ptrack cf5fc9b. Paths are relative to each repo.

## 1. pg_probackup

- **DELTA** reads every page of every main-fork file. It skips a page when the file existed in the parent and `lsn > 0 && lsn < prev_backup_start_lsn`. "Nullified pages must be copied" (`src/data.c` `prepare_page`). The docs say it "can impose read-only I/O pressure equal to a full backup" ([#pbk-overview](https://postgrespro.github.io/pg_probackup/#pbk-overview)).
- **PAGE** parses archived WAL from the parent's start LSN into a per-file pagemap (`src/parsexlog.c` `extractPageMap`/`extractPageInfo`). It keeps main-fork block references only, and an unrecognised `XLR_SPECIAL_REL_UPDATE` record is an ERROR.
  - Needs every segment since the parent in the archive, `archive_mode` on/always, and `wal_level` above minimal ([#pbk-setting-up-continuous-wal-archiving](https://postgrespro.github.io/pg_probackup/#pbk-setting-up-continuous-wal-archiving)).
  - The speedup shrinks when the WAL is comparable in size to the cluster ([#pbk-overview](https://postgrespro.github.io/pg_probackup/#pbk-overview)).
- **PTRACK** relies on a core patch plus the extension. Hooks on `mdwrite`, `mdextend` and `copydir` call `ptrack_mark_block` (`engine.c`), which stores the current LSN into two hashed slots of a fixed shared map. `ptrack_get_pagemapset(lsn)` then walks PGDATA.
  - Needs patched PostgreSQL or Postgres Pro, `CREATE EXTENSION ptrack`, and `ptrack.map_size` > 0 set before a restart ([#pbk-setting-up-ptrack-backups](https://postgrespro.github.io/pg_probackup/#pbk-setting-up-ptrack-backups)).
  - Caveats from the [ptrack README](https://github.com/postgrespro/ptrack) (Limitations, Architecture): a small map gives false positives; resizing loses tracking; it needs `wal_level >= replica`; it uses up to 2x the map size on disk; it claims no false negatives "excepting hint-bits".
- **Files that are not data files** (`_vm`, `_fsm`, `_init`; see `src/dir.c` `set_forkname`, where `is_datafile = forkName == none`) are handled the same way in every mode. A file is skipped only if its size is the same, its mtime is no later than the parent backup, and its CRC32 is equal. Otherwise it is copied whole (`src/data.c` `backup_non_data_file`).
- **Closest prior art:** `pg_probackup catchup -b DELTA|PTRACK` refreshes a stopped destination from a live source ([#pbk-creating-backup-to-catchup](https://postgrespro.github.io/pg_probackup/#pbk-creating-backup-to-catchup)).
  - The threshold is the destination's `checkPointCopy.redo`, lowered to `minRecoveryPoint` or `backupStartPoint` if either is smaller (`src/util.c` `get_redo`).
  - Preflight checks: the destination is stopped, the system ids match, and the timeline is checked (`src/catchup.c`). There is no checksum check.
  - By contrast, incremental restore in LSN mode requires data checksums "to avoid corruption due to hint bits" ([#pbk-incremental-restore](https://postgrespro.github.io/pg_probackup/#pbk-incremental-restore)).

## 2. WAL-G delta maps (`WALG_USE_WAL_DELTA`)

Paths below are under `internal/databases/postgres/` unless shown otherwise.

- **Build:** `wal-push` parses each segment and records the (RelFileNode, block) of every block reference (`internal/walparser/block_locations_util.go` `ExtractBlockLocations`).
  - It writes one `*_delta` file per 16 segments (`wal_delta_util.go`, `WalFileInDelta=16`). `_part` files stitch records that cross segment boundaries (`wal_part_file.go`, `delta_file_manager.go`).
  - Files are staged in `pg_wal/walg_data` (`internal/configure.go` `GetDataFolderPath`).
- **Use:** a delta `backup-push` (`WALG_DELTA_MAX_STEPS` > 0) downloads the deltas for [parent's `IncrementFromLsn`, backup start). It parses the last, partial group's WAL itself (`delta_map_downloader.go` `getDeltaMap`), then selects blocks per file from a roaring bitmap (`paged_file_delta_map.go`).
- **Limits:**
  - The map ignores the fork number, so it is applied only to main-fork segment files (`pagefile.go`, regex `^(\d+)([.]\d+)?$`).
  - `_vm`, `_fsm` and `_init` files are copied whole when their mtime changed. Files with an unchanged mtime are skipped outright (`bundle.go` around line 276).
  - If deltas are missing, WAL-G warns and falls back to a full page-LSN scan, unless `WALG_FORCE_WAL_DELTA` is set (`backup_push_handler.go` lines 311–325).
  - Delta backups are "not implemented" for remote (BASE_BACKUP) backups, and catchup does not use delta maps. The mechanism itself is undocumented: [docs/PostgreSQL.md](https://github.com/wal-g/wal-g/blob/master/docs/PostgreSQL.md) only names the settings.
- **Catchup threshold:** WAL-G reads pg_control bytes 32–40, which is `checkPoint` (the checkpoint record), **not** `checkPointCopy.redo` (`pg_control_data.go` `extractPgControlData`). It sends pages with LSN ≥ that value, plus new pages (`incremental_page_reader.go` `SelectNewValidPage`).

## 3. pg_rewind (PostgreSQL 17)

- `findLastCheckpoint` → `extractPageMap` (`src/bin/pg_rewind/parsexlog.c`) scans the target's WAL from the last checkpoint before divergence. `extractPageInfo` → `process_target_wal_block_change` (`filemap.c`) marks blocks that lie within both file sizes, and those blocks are fetched from the source.
- Blocks changed only on the source are not fetched. Replaying the source's WAL from that checkpoint fixes them ([docs "How It Works"](https://www.postgresql.org/docs/17/app-pgrewind.html); `filemap.c` `decide_file_action`).
- **Forks that are not main forks are copied whole.** `isRelDataFile` matches the main fork only: "Other forks are always copied in toto, because we cannot reliably track changes to them, because WAL only contains block references for the main fork." `extractPageInfo` also skips any block whose `forknum != MAIN_FORKNUM`.
- Files that are not relation files get `FILE_ACTION_COPY` (whole). Relation files whose sizes differ get `COPY_TAIL` or `TRUNCATE`.
- Requires `wal_log_hints` or data checksums ([docs, Description](https://www.postgresql.org/docs/17/app-pgrewind.html)).

## 4. Other systems

- **Percona Server 5.6/5.7 changed page tracking:** a worker thread "reads and parses log records between checkpoints" and writes `ib_modified_log_<seq>_<startlsn>.xdb` bitmaps. `innodb_track_changed_pages` is not dynamic, and the cost is not quantified ([docs](https://docs.percona.com/percona-server/5.7/management/changed_page_tracking.html)).
  - Removed in 8.0.30; XtraBackup 8.0.27+ uses the `mysqlbackup` component instead ([page tracking](https://docs.percona.com/percona-xtrabackup/8.0/page-tracking.html)).
  - XtraBackup's default incremental is a page-LSN scan that reads all pages ([create incremental](https://docs.percona.com/percona-xtrabackup/8.0/create-incremental-backup.html)).
- **Oracle RMAN block change tracking:** one file per database holds bitmaps of changed blocks, keeping the 8 most recent bitmaps.
  - Size is about 1/30,000 of the data, with at least 320 KB per datafile, growing in 10 MB steps. The file is reset after a restore.
  - Runtime overhead and the writer process are not stated ([19c guide, "Using Block Change Tracking to Improve Incremental Backup Performance"](https://docs.oracle.com/en/database/oracle/oracle-database/19/bradv/backing-up-database.html)).
- **SQL Server DCM:** one bit per 64 KiB extent changed since the last full backup, with one DCM page per GAM interval (about 64,000 extents, or 4 GiB). Differential backups read the DCM, so their time is proportional to the changed extents ([Page and Extent Architecture Guide, "DCM and BCM pages"](https://learn.microsoft.com/en-us/sql/relational-databases/pages-and-extents-architecture-guide)).
- **VMware CBT:** the VMkernel tracks changed sectors per disk in `<vm>-ctk.vmdk`, queried with `QueryChangedDiskAreas(changeId)`.
  - It can reset after a hard shutdown, and returns wrong data silently if enabled while snapshots exist; overhead not stated ([Broadcom KB 320557](https://knowledge.broadcom.com/external/article/320557/changed-block-tracking-cbt-on-virtual-machines.html)).

## 5. Is selecting pages by LSN sound?

The invariant: ARIES gives every page a pageLSN (Mohan et al., [TODS 17(1) 1992](https://doi.org/10.1145/128765.128770); I could not retrieve the paper, so its section number is **unverified**). PostgreSQL states the same rule: "each data page … is marked with the LSN … of the latest XLOG record affecting the page" (`src/backend/access/transam/README`, "Write-Ahead Log Coding").

Exceptions in PostgreSQL 17 (a page changes, the LSN does not):

- **Hint bits.**
  - Without checksums or `wal_log_hints`, hint bits never advance the LSN.
  - With them on (`XLogHintBitIsNeeded`), only the first hint after a checkpoint logs `XLOG_FPI_FOR_HINT` and sets the LSN. The test is `lsn <= RedoRecPtr` (`bufmgr.c` `MarkBufferDirtyHint`; `xloginsert.c` `XLogSaveBufferForHint`; `storage/page/README`).
  - **So the safe threshold is a checkpoint *redo* at or before B's state, not B's end LSN.** pg_probackup uses redo; WAL-G uses the checkpoint record LSN, so it can miss hint-only changes on pages whose LSN lies in [redo, checkPoint). Hints are benign to miss.
- **`PD_ALL_VISIBLE`.** `visibilitymap_set` and `heap_xlog_visible` bump the heap page's LSN only when `XLogHintBitIsNeeded` (`visibilitymap.c`, `heapam.c`).
  - The heap block is still registered with `REGBUF_NO_IMAGE` (`heapam.c` `log_heap_visible`), so WAL scans see it and LSN scans do not.
  - *My inference:* with checksums and hints off, an LSN-based refresh can leave a VM bit set while the heap page lacks `PD_ALL_VISIBLE`. The `visibilitymap.c` header comment warns this state lets index-only scans return wrong answers after later local writes, for example after promotion.
- **VM clears.** Before 17.11 and 18.6, a VM clear did not register the VM block, so the VM page LSN did not move **and WAL summaries missed it.** Fixed by [c0d9864f5c](https://github.com/postgres/postgres/commit/c0d9864f5c) (in tag `REL_17_11`; f581fa729d in `REL_18_6`). This affects pgdelta's summaries-on path on 17.0–17.10 unless VM forks are copied whole.
- **FSM.** The FSM "is not explicitly WAL-logged" and uses `MarkBufferDirtyHint` (`storage/freespace/README`, `freespace.c`). PostgreSQL 17 incremental backup copies the FSM whole (`backup/basebackup_incremental.c` `GetFileBackupMethod`).
- **Unlogged relations.** No WAL is written for the main fork. BASE_BACKUP omits them except the init fork (`protocol.sgml`; `CREATE TABLE` UNLOGGED).
- **`wal_level=minimal`.** New relfilenumbers write no WAL. At commit, files under `wal_skip_threshold` (2 MB) are logged as full-page images, and larger ones are only fsynced (`catalog/storage.c` `smgrDoPendingSyncs`; transam README "Skipping WAL for New RelFileLocator").
- **`CREATE DATABASE … STRATEGY FILE_COPY` and `ALTER DATABASE SET TABLESPACE`.** `copydir()` keeps the old page LSNs (`commands/dbcommands.c` `CreateDatabaseUsingFileCopy`). This is safe only when the path is new to B; OID reuse would leave stale pages.
- **Others.**
  - Relation extension writes unlogged zero pages with LSN 0; every tool always copies these.
  - Temp relations write no WAL.
  - A timeline divergence or `pg_resetwal` makes LSNs incomparable.

## 6. Papers

| Paper | Venue, year | Mechanism | Status |
|---|---|---|---|
| Mohan et al., ARIES | TODS 1992 | pageLSN; redo skips a record when pageLSN ≥ its LSN | bibliography verified |
| Mohan & Narang, "An Efficient and Flexible Method for Archiving a Data Base" | SIGMOD 1993 | incremental and full archive copies without locking, read directly from disk | abstract paraphrase only; space-map change bits **unverified** |
| Graefe & Kuno, "Definition, Detection, and Recovery of Single-Page Failures…" | PVLDB 5(7) 2012 | a page recovery index rebuilds one page from its backup copy plus that page's log | abstract verified |
| Talius et al., "Transaction Log Based Application Error Recovery and Point In-Time Query" | PVLDB 5(12) 2012 | pageLSN plus the log reconstructs earlier versions of a page | **unverified** (search snippet only) |
| Graefe, Guy & Sauer, *Instant Recovery with Write-Ahead Logging* | Synthesis Lectures 2014/2016 | single-pass and instant restore from a log archive organised by page | **unverified** (bibliography only) |
| Kemme, Bartoli & Babaoglu, "Online Reconfiguration in Replicated Databases…" | DSN 2001 | strategies for transferring data to bring a rejoining replica current | **unverified** (PDF 403) |

**Not found:** any paper comparing a page-LSN scan, a WAL scan and hashing for refreshing a physical replica.
