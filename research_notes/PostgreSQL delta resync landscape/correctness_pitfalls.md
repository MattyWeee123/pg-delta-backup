# Correctness pitfalls of detecting "changed data" between physical copies of a PostgreSQL cluster (as of 2026-10-22)

Scope note: the current stable major version is PostgreSQL 18. The latest minors are 17.11 and 18.6, released 2026-08-13; 18.5 was never released. The docs site banner reads "October 15, 2026: PostgreSQL 19 RC 1 Released!" ([PG docs header](https://www.postgresql.org/docs/18/storage-init.html); [18.6 notes](https://www.postgresql.org/docs/release/18.6/)). Source-code citations point to `REL_18_STABLE` as fetched on 2026-10-22. Each item is labelled **[doc]** (documented behaviour), **[code]** (read from source), **[bug]** (confirmed and fixed or acknowledged bug), **[report]** (unconfirmed user report) or **[inference]** (my reasoning).

---

## 1. Page-LSN (pd_lsn) change detection: when can page bytes change without pd_lsn advancing?

### Takeaway
`pd_lsn` is only a reliable change marker for main-fork pages of permanent (WAL-logged) relations, and only under these conditions:
- Hint-bit writes are WAL-logged (data checksums or `wal_log_hints=on`).
- The comparison threshold is the redo point of the checkpoint the old copy's recovery started from.
- Both copies share one unbroken WAL history.

Several forks and file types break this rule:
- The FSM is not fully WAL-logged.
- VM bit clears did not bump the VM page LSN or register the block until 17.11/18.6 (August 2026).
- Unlogged relations, temporary relations and init forks.
- Non-relation files.

Every serious tool (pg_rewind, PG17 incremental backup, WAL-G) copies those wholesale.

### Cited Findings
**What pd_lsn means**
- **[doc]** pd_lsn is "LSN: next byte after last byte of WAL record for last change to this page". The 24-byte page header has no timeline field: pd_lsn, pd_checksum, pd_flags, pd_lower, pd_upper and so on — [PG docs: Database Page Layout](https://www.postgresql.org/docs/18/storage-page-layout.html)

**Hint bits**
- **[doc]** `wal_log_hints`: "When this parameter is on, the PostgreSQL server writes the entire content of each disk page to WAL during the first modification of that page after a checkpoint, even for non-critical modifications of so-called hint bits. If data checksums are enabled, hint bit updates are always WAL-logged and this setting is ignored." The default is `off` — [PG docs: runtime-config-wal](https://www.postgresql.org/docs/18/runtime-config-wal.html)
- **[code]** `MarkBufferDirtyHint()` WAL-logs a full-page image for a hint-bit change only "if the hint bit update is the first change to the page since the last checkpoint". It sets the page LSN only "if we wrote a backup block".
  - When `XLogHintBitIsNeeded()` is false (no checksums, `wal_log_hints=off`), the buffer is simply marked dirty. The hint-bit change then reaches disk with the old LSN.
  - The function also skips dirtying the page during recovery, so a standby sets hints in memory without writing them.
  - Source: [bufmgr.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/storage/buffer/bufmgr.c)
- **[doc]** pg_rewind "requires that the target server either has the wal_log_hints option enabled in postgresql.conf or data checksums enabled ... full_page_writes must also be set to on" — [pg_rewind docs](https://www.postgresql.org/docs/current/app-pgrewind.html)
- **[doc]** pg_probackup incremental restore in LSN mode requires that "data checksums parameter must be enabled in data directory (to avoid corruption due to hint bits)". It checks this at start and aborts otherwise. Unlike pg_rewind, it does not accept `wal_log_hints` as a substitute — [pg_probackup docs](https://postgrespro.github.io/pg_probackup/)
- **[doc]** PG18 changed the initdb default to enable data checksums. `--no-data-checksums` opts out. "pg_upgrade requires matching cluster checksum settings" — [PG 18.0 release notes](https://www.postgresql.org/docs/release/18.0/) (released 2025-09-25)

**Free space map (FSM)**
- **[code]** PG17+ incremental backup always copies the FSM in full: "The free-space map fork is not properly WAL-logged, so we need to backup the entire file every time" — [basebackup_incremental.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/backup/basebackup_incremental.c)
- **[code]** The WAL summarizer ignores the FSM fork everywhere ("not fully WAL-logged") — [walsummarizer.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/postmaster/walsummarizer.c)
- **[code]** pg_rewind treats only the main fork as block-trackable: "only files belonging to the main fork are considered relation files. Other forks are always copied in toto, because we cannot reliably track changes to them, because WAL only contains block references for the main fork." — [pg_rewind filemap.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/bin/pg_rewind/filemap.c)
- **[bug]** Fixed in 17.10/18.4 (2026-05-14): during WAL replay, FSM changes were never written out if checksums were enabled. "On a standby server, over time this would result in a map wildly at variance with the table's actual contents" — [17.10 notes](https://www.postgresql.org/docs/release/17.10/), [18.4 notes](https://www.postgresql.org/docs/release/18.4/)
- **[bug]** pgBackRest observed the same divergence: "File sizes on the standby can differ from the primary, e.g. for free space maps." This caused spurious switches to checksum delta (Issue #2020) — [pgBackRest release notes](https://pgbackrest.org/release.html)

**Visibility map (VM)**
- **[code]** The VM header comments say:
  - "Clearing visibility map bits is not separately WAL-logged."
  - "When a bit is set, the LSN of the visibility map page is updated ... But when a bit is cleared, we don't have to do that because it's always safe to clear a bit in the map from correctness point of view."
  - Source: [visibilitymap.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/access/heap/visibilitymap.c)
- **[bug]** Commit f581fa729d by Melanie Plageman and Andres Freund, 2026-07-15, back-patched through 17. Its message says:
  - "Heap WAL records that clear bits on the visibility map (like inserts and deletes) did not register the visibility map blocks they modified. Because the WAL summarizer only records registered blocks, an incremental backup taken over such operations would omit the changed VM pages. On restore, the VM would retain stale all-visible/all-frozen bits, which can cause wrong results from index-only scans and incorrect relfrozenxid advancement."
  - It also notes the missing VM FPIs: "with checksums off, it is accepted as-is and can lead to data corruption."
  - "Note that this fix does not repair existing incremental backups."
  - Source: [commit f581fa7](https://github.com/postgres/postgres/commit/f581fa729d8e108fef853c3156267b1f753d0210)
- **[bug]** The fix shipped in 17.11 and 18.6 (2026-08-13) as "Fix WAL logging of operations that clear bits in tables' visibility maps ... Such VM changes were missed by the WAL summarizer, potentially leading to incorrect incremental backups. We also failed to log full-page images of such VM pages when needed, potentially allowing torn page writes to go uncorrected." — [17.11 notes](https://www.postgresql.org/docs/release/17.11/), [18.6 notes](https://www.postgresql.org/docs/release/18.6/)
- **[code]** After the fix, `heap_insert` and similar functions call `PageSetLSN(BufferGetPage(vmbuffer), recptr)` when the VM was modified. Replay keeps a fallback for "WAL generated from before the fix" — [heapam.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/access/heap/heapam.c), [heapam_xlog.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/access/heap/heapam_xlog.c)
- **[doc]** This hazard was publicly known in 2017. In the pgsql-hackers thread "On markers of changed data", Andrey Borodin (WAL-G) reported:
  - VM bits are cleared without a page-LSN bump, which could cause wrong index-only-scan results on restored backups.
  - So WAL-G disabled incremental backup for the FSM, VM and XACT/CLOG.
  - Robert Haas noted in the same thread that the FSM is not WAL-logged, so its LSNs are not meaningful.
  - Source: [pgsql-hackers thread, Oct 2017](https://www.postgresql.org/message-id/flat/F0627DEB-7D0D-429B-97A9-D321450365B4%40yandex-team.ru)

**Unlogged, temporary and init forks**
- **[doc]** "Each unlogged table, and each index on an unlogged table, has an initialization fork ... When an unlogged table must be reset to empty due to a crash, the initialization fork is copied over the main fork, and any other forks are erased" — [PG docs: The Initialization Fork](https://www.postgresql.org/docs/18/storage-init.html)
- **[code]** Base backups exclude these files:
  - All forks of unlogged tables except the init fork ("unlogged relation file ... excluded from backup").
  - Temporary relations (`looks_like_temp_rel_name`).
  - Source: [basebackup.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/backup/basebackup.c)
- **[code]** Hint-bit FPIs are emitted only for `BM_PERMANENT` buffers, so unlogged pages never get WAL-driven LSN updates — [bufmgr.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/storage/buffer/bufmgr.c)
- **[doc]** pg_rewind omits `pgsql_tmp*` and a long list of runtime directories from the copy:
  - Omitted directories: `pg_dynshmem/`, `pg_notify/`, `pg_replslot/`, `pg_serial/`, `pg_snapshots/`, `pg_stat_tmp/`, `pg_subtrans/`.
  - Omitted files: `backup_label`, `tablespace_map`, `pg_internal.init`, `postmaster.pid` and others.
  - It copies "all other files, including new relation files, WAL segments, pg_xact, and configuration files" in full.
  - Source: [pg_rewind docs](https://www.postgresql.org/docs/current/app-pgrewind.html)
- **[doc]** The pg_upgrade standby-rsync recipe "needlessly copies files associated with temporary and unlogged tables because these files don't normally exist on standby servers" — [pg_upgrade docs](https://www.postgresql.org/docs/18/pgupgrade.html)
- **[bug]** Related unlogged-relation bugs:
  - 17.8/18.2: "Do not emit WAL for unlogged BRIN indexes" — [17.8 notes](https://www.postgresql.org/docs/release/17.8/)
  - 17.11: "Fix corruption of unlogged sequences after standby promotion" — [17.11 notes](https://www.postgresql.org/docs/release/17.11/)

**How tools choose the LSN threshold**
- **[code]** WAL-G's delta page selector keeps a page if it is all-zero ("vacuumed page") or `pageHeader.lsn() >= pageReader.Lsn`, where `Lsn` is the base backup's LSN. It drops pages whose header is invalid — [WAL-G incremental_page_reader.go](https://github.com/wal-g/wal-g/blob/master/internal/databases/postgres/incremental_page_reader.go)
- **[doc]** pg_probackup LSN mode reads "redo LSN and redo TLI" from the destination's pg_control to find a "shiftpoint". It replaces "invalid pages and those with LSN greater than shiftpoint", and aborts if the shiftpoint is outside the backup chain's history. It is "not recommended to use LSN mode in any situation, where pg_control cannot be trusted or has been tampered with: after pg_resetxlog execution, after restore from backup without recovery been run, etc." — [pg_probackup docs](https://postgrespro.github.io/pg_probackup/)
- **[doc]** pg_probackup `catchup` is the closest existing analog to the student tool. It refreshes an existing replica from a live source:
  - DELTA mode "reads all data files ... and creates an incremental copy for pages that have changed since the destination instance was shut down".
  - PTRACK mode "only reads and copies pages that have changed since the point of divergence".
  - Source: [pg_probackup docs, Cloning and Synchronizing](https://postgrespro.github.io/pg_probackup/)
- **[code]** PostgreSQL itself uses the backup start LSN as its "is this page possibly in flight?" threshold. In base backups: "Only check pages which have not been modified since the start of the base backup. Otherwise, they might have been written only halfway and the checksum would not be valid. However, replaying WAL would reinstate the correct page in this case" (`PageGetLSN(page) >= start_lsn` means skip the checksum check) — [basebackup.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/backup/basebackup.c)

### Inferences
- **[inference] Wrong threshold.** "Skip pages whose LSN is older than the old copy's position" is unsafe if "position" means the copy's end, consistency or replay LSN.
  - With checksums or `wal_log_hints`, a hint-bit change after the first post-checkpoint FPI does not advance pd_lsn. A page can therefore be modified after LSN X while keeping an LSN older than X, as long as it is newer than the redo pointer of the checkpoint covering X.
  - The safe threshold is the redo LSN of the checkpoint from which the old copy's recovery began, i.e. the backup start LSN or `pg_control`'s "REDO location" for a cleanly stopped copy. Pages with LSN ≥ threshold must be copied. This is what pg_probackup (redo LSN), WAL-G (`>=` base LSN) and basebackup.c (`>= start_lsn`) use.
- **[inference] Required preconditions for any LSN fast path:**
  1. Data checksums are on, or at least `wal_log_hints=on`, and were on for the whole interval.
  2. `full_page_writes=on`.
  3. The file is a main fork of a permanent relation.
  4. The destination's history is a prefix of the source's history (see section 2).
  5. If a VM fork is block-compared at all, all WAL in the interval was generated by 17.11+/18.6+. Clusters that ran older minors, or PG ≤16, still clear VM bits without bumping the VM page LSN. Treat the VM like the FSM: always copy or hash it whole.
- **[inference] Missed hint-bit changes are usually benign for a pure prefix refresh.** Hint bits are recomputable from CLOG. Two exceptions:
  - With divergent histories, stale "committed" hints can make rows visible that should not be. This is the likely reason pg_rewind requires logged hints, though the pg_rewind docs do not spell it out.
  - Byte-level verification (chunk-hash comparison after the copy) will report spurious mismatches.
- **[inference] Unlogged and temp files.** The tool should mirror base-backup exclusions: drop main/FSM/VM forks of unlogged relations, copy `_init` forks, skip `t<backend>_<relfilenode>` temp files and `pgsql_tmp`. It should hash-copy all non-relation files in full: `pg_xact`, `pg_multixact`, `pg_control`, `pg_filenode.map`, `PG_VERSION` and so on.

### Gaps
- I found no explicit upstream document explaining why pg_rewind needs logged hint bits. The divergence and visibility explanation above is my inference.
- I did not verify whether other access methods (GiST/SP-GiST "fake LSNs" for unlogged indexes, BRIN, hash) have further non-LSN-bumping writes on permanent relations.
- I did not verify whether PG19 (RC1 as of 2026-10-15) changes any of this, for example online checksum enabling.

---

## 2. Page-LSN under cluster-level events: CREATE DATABASE FILE_COPY, ALTER DATABASE SET TABLESPACE, truncation, pg_upgrade, pg_checksums, failover/timelines

### Takeaway
Several routine operations make files whose pages carry old LSNs, or keep LSNs while rewriting bytes, or reuse the same LSN space for different data:
- FILE_COPY database creation and `SET TABLESPACE` copy whole directories.
- `pg_checksums --enable` rewrites pd_checksum without touching pd_lsn.
- pg_upgrade restarts WAL on timeline 1 and builds new catalogs.
- Failover creates divergent histories over the same LSN range.

PostgreSQL's own WAL summarizer calls the FILE_COPY case potentially "catastrophic" and marks whole database directories as recreated. An LSN-only fast path needs equivalent detection, or must be disabled after these events.

### Cited Findings
**CREATE DATABASE STRATEGY FILE_COPY and ALTER DATABASE SET TABLESPACE**
- **[doc]** "If the WAL_LOG strategy is used, the database will be copied block by block and each block will be separately written to the write-ahead log ... it is the default. The older FILE_COPY strategy ... writes a small record to the write-ahead log for each tablespace used by the target database. Each such record represents copying an entire directory to a new location at the filesystem level ... it also forces the system to perform a checkpoint both before and after" — [CREATE DATABASE docs](https://www.postgresql.org/docs/18/sql-createdatabase.html)
- **[doc]** WAL_LOG became the default in PG15: "Add new WAL-logged method for database creation ... This is the new default method for copying the template database ... the old method is still available." Before PG15 every CREATE DATABASE was a file-level copy — [PG 15.0 release notes](https://www.postgresql.org/docs/release/15.0/)
- **[doc]** PG18 added `file_copy_method` (`COPY` or `CLONE`, where CLONE uses `copy_file_range()` or macOS `copyfile`). It affects both "CREATE DATABASE ... STRATEGY=FILE_COPY" and "ALTER DATABASE ... SET TABLESPACE" — [runtime-config-resource](https://www.postgresql.org/docs/18/runtime-config-resource.html)
- **[code]** `movedb()` (ALTER DATABASE SET TABLESPACE) calls `copydir()` and logs `XLOG_DBASE_CREATE_FILE_COPY`. `CreateDatabaseUsingFileCopy()` forces a checkpoint and then copies directories. Neither stamps new LSNs on the copied pages — [dbcommands.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/commands/dbcommands.c)
- **[code]** The WAL summarizer comment reads: "XLOG_DBASE_CREATE_FILE_COPY ... can create a whole bunch of relation files in a directory without logging anything specific to each one. If we didn't mark the whole DB OID/TS OID combination in some way, then a tablespace that was dropped after the reference backup and recreated using the FILE_COPY method prior to the incremental backup would look just like one that was never touched at all, which would be catastrophic." Its fix is to set a limit block of 0 for every relfilenode under that DB/TS pair — [walsummarizer.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/postmaster/walsummarizer.c)

**Relation truncation (VACUUM truncation, TRUNCATE, DROP)**
- **[code]** The WAL summarizer records `XLOG_SMGR_TRUNCATE` as a "limit block". It also sets a limit block of 0 for relations dropped on commit or abort. Incremental backup sends the whole file when `limit_block <= segno * RELSEG_SIZE` — [walsummarizer.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/postmaster/walsummarizer.c), [basebackup_incremental.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/backup/basebackup_incremental.c)
- **[bug]** 17.8/18.2 (2026-02-12): "If a table exceeding 1GB ... is truncated by VACUUM between the base backup and the incremental backup, pg_combinebackup could fail with an error about 'truncation block length in excess of segment size'." This caused a restore failure, not silent corruption — [17.8 notes](https://www.postgresql.org/docs/release/17.8/), [18.2 notes](https://www.postgresql.org/docs/release/18.2/)
- **[bug]** 17.3 (2025-02-13):
  - "Fix data corruption when relation truncation fails ... (for example, effectively reviving deleted data)". The fix turns such failures into PANICs.
  - "Prevent checkpoints from starting during relation truncation".
  - Source: [17.3 notes](https://www.postgresql.org/docs/release/17.3/)
- **[bug]** 17.10/18.4: "Prevent bloating relation visibility maps during restore of an incremental backup ... incorrect computation of the expected file length". This wasted space but did not corrupt data — [17.10 notes](https://www.postgresql.org/docs/release/17.10/)

**pg_upgrade**
- **[code]** pg_upgrade restarts the new cluster's WAL just past the old cluster's: `pg_resetwal -l 00000001<old nextxlogfile>`, with the comment "use timeline 1 to match controldata and no WAL history file". It preserves next-XID and multixact counters because "we preserve all files and contents" — [pg_upgrade.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/bin/pg_upgrade/pg_upgrade.c)
- **[doc]** PG15+ pg_upgrade preserves "tablespace and database OIDs, as well as relation relfilenode numbers", so user relation file names survive an upgrade — [PG 15.0 release notes](https://www.postgresql.org/docs/release/15.0/)
- **[doc]** Tablespace subdirectories are version-stamped. The docs' own rsync example maps `/vol1/pg_tblsp/PG_12_201909212` to `/vol1/pg_tblsp/PG_18_202307071` — [pg_upgrade docs](https://www.postgresql.org/docs/18/pgupgrade.html)
- **[doc]** Transfer modes are `--copy` (default), `--link`, `--clone`, `--copy-file-range`, and `--swap` (new in PG18: "swap directories rather than copy, clone, or link files ... potentially the fastest"). Standby upgrade after link mode uses `rsync --archive --delete --hard-links --size-only --no-inc-recursive`, which relies on hard-link identity rather than content comparison — [pg_upgrade docs](https://www.postgresql.org/docs/18/pgupgrade.html), [PG 18.0 notes](https://www.postgresql.org/docs/release/18.0/)
- **[doc]** pg_upgrade requires matching checksum settings on the old and new clusters. PG18 initdb defaults to checksums on — [PG 18.0 notes](https://www.postgresql.org/docs/release/18.0/)

**Enabling or disabling checksums offline (pg_checksums)**
- **[code]** `pg_checksums --enable` sets `header->pd_checksum = csum`, seeks back and rewrites each block. It changes no other header field and writes no WAL (it is an offline tool) — [pg_checksums.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/bin/pg_checksums/pg_checksums.c)
- **[doc]** "When using a replication setup with tools which perform direct copies of relation file blocks (for example pg_rewind), enabling or disabling checksums can lead to page corruptions in the shape of incorrect checksums if the operation is not done consistently across all nodes ... Destroying all standbys, performing the operation on the primary and finally recreating the standbys from scratch is also safe." — [pg_checksums docs](https://www.postgresql.org/docs/18/app-pgchecksums.html)
- **[doc]** "pg_combinebackup does not recompute page checksums ... if any of the backups used for reconstruction were taken with checksums disabled, but the final backup was taken with checksums enabled, the resulting directory may contain pages with invalid checksums ... taking a new full backup after changing the checksum state ... is recommended" — [pg_combinebackup docs](https://www.postgresql.org/docs/current/app-pgcombinebackup.html)

**Failover and timeline divergence**
- **[doc]** pg_rewind "examines the timeline histories of the source and target clusters to determine the point where they diverged". It then scans the target's WAL "starting from the last checkpoint before" the fork, records each touched block, and copies those blocks from the source. Afterwards, WAL replay must run from the last checkpoint before divergence — [pg_rewind docs](https://www.postgresql.org/docs/current/app-pgrewind.html)
- **[code]** The incremental-backup sanity-check comment reads: "one possible explanation is that the user has generated WAL on the same timeline with the same LSNs more than once. For instance, if two standbys running on timeline 1 were both promoted and (due to a broken archiving setup) both selected new timeline ID 2 ... there are lots of ways for the user to do something very bad without tripping any of these checks, and they are not intended to be comprehensive" — [basebackup_incremental.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/backup/basebackup_incremental.c)
- **[doc]** pgBackRest v2.07 (2018-11-16) can "Automatically enable backup checksum delta when anomalies (e.g. timeline switch) are detected". A later release added a hint "when checksum delta is enabled after a timeline switch" — [pgBackRest release notes](https://pgbackrest.org/release.html)
- **[bug]** 17.11: "Prevent WAL summarizer process from getting stuck at a timeline switch" — [17.11 notes](https://www.postgresql.org/docs/release/17.11/)

### Inferences
- **[inference] FILE_COPY / SET TABLESPACE.** A database dropped and recreated (or moved) via FILE_COPY can produce files at paths the old copy also has, with pages bearing old template LSNs below the threshold. An LSN fast path would keep the destination's stale pages.
  - Content hashing catches this. LSN-only logic needs a per-DB "recreated" signal. Options: scan WAL for `XLOG_DBASE_CREATE_FILE_COPY` / `XLOG_DBASE_DROP` as the WAL summarizer does, or fall back to hashing any database directory whose file set changed.
  - Pre-PG15 clusters hit this on every CREATE DATABASE.
- **[inference] Truncation.** LSN or hash comparison over blocks that exist on the source cannot remove destination blocks past the new EOF, or whole segment files (`.1`, `.2`, …) that vanished. The tool must reconcile the file list and sizes first: truncate or delete, then compare blocks. Zero-filled "new" pages must count as changed, as WAL-G does.
- **[inference] pg_upgrade breaks LSN comparability with a pre-upgrade copy:**
  - The timeline resets to 1.
  - The catalogs are rebuilt by initdb and schema restore, so same-path catalog files can hold completely different content.
  - initdb-era pages may carry small LSNs.
  - The system identifier differs. I infer this because pg_upgrade's `controldata.c` never references the system identifier, and the new cluster comes from a fresh initdb.
  - Treat any system-identifier change as a new cluster: full hash comparison, plus path remapping for `PG_<ver>_<catver>` tablespace directories. Hashing can still skip unchanged user relation files, because PG15+ preserves relfilenodes and link, clone or copy mode leave their bytes unchanged.
- **[inference] pg_checksums.** After `pg_checksums --enable` between refreshes, almost every page's bytes change while pd_lsn does not. An LSN fast path would leave checksum-less pages on a destination whose `pg_control` claims checksums are on, producing checksum failures.
  - Detect a change in `data_checksum_version` in `pg_control` and force a full hash compare.
  - A hash-based tool handles this correctly, but transfers nearly the whole cluster.
- **[inference] Failover.** Because pd_lsn carries no timeline, an LSN fast path is valid only if the destination's (redo LSN, TLI) lies on the source's timeline history and the destination did not run past the fork point.
  - If the destination is an ex-primary that wrote WAL after divergence, its locally changed pages have no counterpart LSN signal on the source. That is why pg_rewind scans the target's WAL, not the source's page LSNs.
  - Two promotions picking the same TLI make even timeline-history checks unreliable. Content hashing is the fallback.

### Gaps
- I did not find explicit upstream documentation stating that pg_upgrade assigns a new system identifier. That claim is inferred from source.
- I did not test what LSNs initdb-created catalog pages carry after pg_upgrade.
- I did not research Barman's handling of these events.

---

## 3. Torn pages, full_page_writes, and reading a live data directory

### Takeaway
A file-level read of a running cluster can capture half-written ("torn") 8 KB pages and pages from different points in time. Such a copy is only correct if it is bracketed by `pg_backup_start`/`pg_backup_stop` (which forces full-page writes) and then WAL-replayed from the start checkpoint to the end-of-backup point. Any refresh tool reading a live source inherits this requirement. Page checksums cannot be used to validate pages modified during the copy.

### Cited Findings
- **[doc]** `full_page_writes` writes "the entire content of each disk page to WAL during the first modification of that page after a checkpoint. This is needed because a page write that is in process during an operating system crash might be only partially completed, leading to an on-disk page that contains a mix of old and new data ... Turning this parameter off ... might lead to either unrecoverable data corruption, or silent data corruption" — [runtime-config-wal](https://www.postgresql.org/docs/18/runtime-config-wal.html)
- **[doc]** "full_page_writes is effectively forced on during backup mode". A base backup need not be consistent: "Any internal inconsistency in the backup will be corrected by log replay". You "will need to keep all the WAL segment files generated during and after the file system backup" — [Continuous Archiving docs](https://www.postgresql.org/docs/18/continuous-archiving.html)
- **[doc]** Backups taken from a standby require `full_page_writes` on the primary — [pg_basebackup docs](https://www.postgresql.org/docs/current/app-pgbasebackup.html). pg_probackup adds that you must not use tools "like pg_compresslog as archive_command to remove full-page writes from WAL files" — [pg_probackup docs](https://postgrespro.github.io/pg_probackup/)
- **[code]** pg_basebackup's checksum verification skips pages with `PageGetLSN(page) >= start_lsn`, because they "might have been written only halfway ... replaying WAL would reinstate the correct page" — [basebackup.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/backup/basebackup.c)
- **[bug]** Until 17.11/18.6, VM-clearing operations did not emit FPIs, "potentially allowing torn page writes to go uncorrected" — [17.11 notes](https://www.postgresql.org/docs/release/17.11/). The commit adds: "A torn VM page won't raise an error because the VM is read with ZERO_ON_ERROR" — [commit f581fa7](https://github.com/postgres/postgres/commit/f581fa729d8e108fef853c3156267b1f753d0210)
- **[doc]** After pg_rewind, "WAL replay needs to complete for the data directory to be in a consistent state". pg_rewind writes a `backup_label` and a minimum recovery LSN into `pg_control` — [pg_rewind docs](https://www.postgresql.org/docs/current/app-pgrewind.html)
- **[doc]** pgBackRest delta restore first removes `global/pg_control` "so cluster will not start if restore does not complete" — [pgBackRest user guide](https://pgbackrest.org/user-guide.html)
- **[bug]** 17.8/18.2: "Fail recovery if WAL does not exist back to the redo point indicated by the checkpoint record ... Previously, recovery might crash or corrupt the database in this situation" — [17.8 notes](https://www.postgresql.org/docs/release/17.8/)

### Inferences
- **[inference] Live-source workflow.** The tool should call `pg_backup_start` (or use the replication protocol), record the start LSN (redo), copy and compare, call `pg_backup_stop`, and write `backup_label` (and `tablespace_map`). It must ensure WAL from start LSN to stop LSN is available to the destination. It should also invalidate `pg_control` at the start of the destination update, so a partially refreshed directory cannot start.
- **[inference] Hashing a live source.** A chunk hash computed while pages are in flight is a hash of a possibly torn image. The "matching" decision is only safe because WAL replay later overwrites any page modified after the start LSN with an FPI. Skipping a chunk is safe only if its source content at read time was not torn, or will be fixed by replay. Pages modified before the start LSN are stable, and pages after it are covered by FPIs.

### Gaps
- I found no specific published measurement of torn-read frequency in live file copies.

---

## 4. rsync and mtime pitfalls

### Takeaway
PostgreSQL's own docs require `rsync --checksum` for the final pass because rsync's default size+mtime quick check works at one-second granularity. Committers on pgsql-hackers advise against trusting file-system timestamps. WAL-G and pgBackRest still use mtime as a first-level filter, with mitigations. Each has had an mtime/size-related bug or quirk.

### Cited Findings
- **[doc]** "first running rsync while the database server is running, then shutting down the database server long enough to do an rsync --checksum. (--checksum is necessary because rsync only has file modification-time granularity of one second.)" — [PG docs: File System Level Backup](https://www.postgresql.org/docs/18/backup-file.html). The same advice appears for copying a cluster before link-mode pg_upgrade, along with excluding `postmaster.pid` — [pg_upgrade docs](https://www.postgresql.org/docs/18/pgupgrade.html)
- **[doc]** In the 2017 pgsql-hackers thread "On markers of changed data":
  - Tom Lane gave three objections to mtime: unknown timestamp granularity, a clock that can be set backwards (the worst), and filesystems that may skip or delay mtime updates.
  - Michael Paquier added forward clock jumps (e.g. VM snapshots) and concluded the only sane method is to scan page-header LSNs.
  - Stephen Frost described a safer mtime protocol: treat any *difference* as a change, collect mtimes before starting, wait out the timestamp granularity, re-copy anything newer. He judged the residual risk small and still recommended periodic full backups.
  - Source: [pgsql-hackers thread](https://www.postgresql.org/message-id/flat/F0627DEB-7D0D-429B-97A9-D321450365B4%40yandex-team.ru)
- **[doc]** WAL-G: "Delta computation is based on ModTime of file system and LSN number of pages in datafiles" — [WAL-G PostgreSQL docs](https://github.com/wal-g/wal-g/blob/master/docs/PostgreSQL.md)
- **[code]** WAL-G skips a whole file when `time.Equal(baseFile.MTime)`, i.e. the mtime is equal (not merely older). The comment reads "We do not rely here on monotonic time, instead we backup file if MTime changed somehow" and links the thread above. It takes the mtime before reading the file — [WAL-G bundle.go](https://github.com/wal-g/wal-g/blob/master/internal/databases/postgres/bundle.go)
- **[doc]** pgBackRest `--delta`: "During a backup, this option will use checksums instead of the timestamps to determine if files will be copied". For restore, it "performs a delta restore using checksums". "Exclusions are not honored on delta restores" — [pgBackRest configuration](https://pgbackrest.org/configuration.html)
- **[doc]** pgBackRest history:
  - v2.06 (2018-10-15) introduced checksum-delta backup.
  - v2.07 (2018-11-16) auto-enables it on anomalies such as a timeline switch.
  - A later release added `ClockError` "for unexpected clock skew and timezone changes".
  - Source: [pgBackRest release notes](https://pgbackrest.org/release.html)
- **[bug]** pgBackRest v2.28 (2020-07-20): "Fix restore --force acting like --force --delta. This caused restore to replace files based on timestamp and size rather than overwriting, which meant some files that should have been updated were left unchanged." This is a confirmed silent-staleness bug from size+timestamp comparison — [pgBackRest release notes](https://pgbackrest.org/release.html)
- **[bug]** pgBackRest (2023-09-25): restore failed with block incremental when "A block map with no blocks, which is written when a file timestamp changes but the file does not" was mishandled (Issue #2182) — [pgBackRest release notes](https://pgbackrest.org/release.html)
- **[doc]** The pg_upgrade standby recipe uses `rsync --size-only --hard-links`. This is safe only because link mode makes unchanged files hard links of the old ones, and the old standby is assumed to have matched the old primary — [pg_upgrade docs](https://www.postgresql.org/docs/18/pgupgrade.html)

### Inferences
- **[inference] Never skip on "mtime older than X".** At most skip on "mtime and size exactly equal to what we recorded last time, recorded before the previous copy began, and the granularity window has elapsed". Even then, clock steps, VM snapshots, `touch`-preserving tools (`cp -p`, rsync `-t` on restores) and filesystem quirks can defeat it. Periodically force a full hash pass.
- **[inference] Destination mtimes are meaningless as change evidence.** Any destination file written by restore tools may carry source mtimes.

### Gaps
- I did not find the original pgBackRest issue that motivated checksum delta, beyond the release notes.
- I did not research Barman's rsync "safe horizon" logic.

---

## 5. Known real-world bugs and incidents (confirmed vs reported)

### Takeaway
Block-level incremental and refresh tooling has a steady record of correctness bugs:
- PostgreSQL's own PG17 incremental backup silently omitted VM changes until August 2026.
- pg_probackup had a CRC false-positive file reuse and a catchup mode that destroyed the source.
- pgBackRest had a size/timestamp restore bug.
- pg_rewind has had WAL-deletion bugs.

Several fixes do not repair artifacts already produced.

### Cited Findings
**PostgreSQL core (incremental backup, pg_combinebackup, WAL summarizer)**
- **[bug]** VM clears were not registered, so incremental backups omitted VM pages and torn VM pages went uncorrected. Fixed in 17.11/18.6 (2026-08-13). "This fix does not repair existing incremental backups" — [17.11 notes](https://www.postgresql.org/docs/release/17.11/), [commit](https://github.com/postgres/postgres/commit/f581fa729d8e108fef853c3156267b1f753d0210)
- **[bug]** WAL summarizer could get stuck at a timeline switch. Fixed in 17.11 — [17.11 notes](https://www.postgresql.org/docs/release/17.11/)
- **[bug]** pg_combinebackup infinite loop "if the source file is shorter than expected". Fixed in 17.11 — [17.11 notes](https://www.postgresql.org/docs/release/17.11/)
- **[bug]** VACUUM truncation of a >1 GB table between backups broke pg_combinebackup. Fixed in 17.8/18.2 (2026-02-12) — [17.8 notes](https://www.postgresql.org/docs/release/17.8/)
- **[bug]** VM bloat (appended zero blocks) on incremental restore. Fixed in 17.10/18.4 (2026-05-14) — [17.10 notes](https://www.postgresql.org/docs/release/17.10/)
- **[bug]** pg_combinebackup accepted an incremental file inside a directory that should hold a full backup. Fixed in 17.1 (2024-11-14) — [17.1 notes](https://www.postgresql.org/docs/release/17.1/)
- **[bug]** Per-file memory leak in pg_combinebackup. Fixed in 17.7 (2025-11-13) and 18.1 — [17.7 notes](https://www.postgresql.org/docs/release/17.7/)
- **[bug]** pg_basebackup/pg_verifybackup decompression and tar-parsing code "could corrupt LZ4-compressed data in edge cases" and mishandled tar padding. Fixed in 17.10/18.4. 18.4 also hardened tar parsing against non-PostgreSQL tar files — [17.10 notes](https://www.postgresql.org/docs/release/17.10/), [18.4 notes](https://www.postgresql.org/docs/release/18.4/)
- **[bug]** CVE-2026-6475: path traversal in pg_basebackup and pg_rewind, where "a malicious source could overwrite any file writable by these applications". Fixed in 17.10/18.4 — [17.10 notes](https://www.postgresql.org/docs/release/17.10/)
- **[bug]** FSM changes not persisted during recovery with checksums on, so standby FSMs diverge. Fixed in 17.10/18.4 — [17.10 notes](https://www.postgresql.org/docs/release/17.10/)
- **[bug]** A truncation failure could revive deleted data, and a checkpoint during truncation was a race. Fixed in 17.3 — [17.3 notes](https://www.postgresql.org/docs/release/17.3/)

**pg_rewind**
- **[bug]** "Avoid deleting still-needed WAL files during pg_rewind". Files marked `.ready` but not yet archived could be removed, after which "recovery is not possible anymore". Fixed in 17.2 and 14.15 — [17.2 notes](https://www.postgresql.org/docs/release/17.2/), [14.15 notes](https://www.postgresql.org/docs/release/14.15/)
- **[bug]** Older fixes:
  - 12.1: `--dry-run` updated pg_control.
  - 12.4: concurrent deletions in an online source confused pg_rewind.
  - 12.6: "ensure that all WAL is accounted for when rewinding a standby server".
  - Sources: [12.1](https://www.postgresql.org/docs/release/12.1/), [12.4](https://www.postgresql.org/docs/release/12.4/), [12.6](https://www.postgresql.org/docs/release/12.6/)

**pg_probackup** (all from the [pg_probackup releases](https://github.com/postgrespro/pg_probackup/releases))
- **[bug]** 2.5.4 (2021-12-27): "catchup: fix data corruption in delta and ptrack modes, before that, the source database could be damaged". Issue #459: when `--source-pgdata` and `--destination-pgdata` matched over ssh, `pg_control` and `pg_filenode.map` files were deleted — [issue #459](https://github.com/postgrespro/pg_probackup/issues/459)
- **[bug]** 2.5.4: catchup's timeline-history sanity check gave false alarms ("Destination is not in source timeline history") when the source was not on TLI 1 — [issue #463](https://github.com/postgrespro/pg_probackup/issues/463)
- **[bug]** 2.4.10 (2021-02-13): "Due to false-positive crc match it was possible to erroneously reuse pg_filenode.map file during incremental restore, which could break pg_catalog relations" (relations rewritten by VACUUM FULL or REINDEX).
- **[bug]** 2.5.14 (2024-05-14): "Fixed incremental restore of tables larger than 1GB. Checksum computation for file pages was fixed".
- **[bug]** 2.3.3 (2020-05-15): "silent data loss if more than 32 data files were dropped during remote backup".
- **[bug]** 2.2.3 (2019-10-15): metadata writer lost "information about one file per 4MB of metadata" (versions 2.1.4–2.2.1).
- **[bug]** 2.1.3 (2019-05-30): restore with `--no-validate` could lose data when a file was missing, and merge of a PAGE backup with external directories risked data loss.
- **[report]** 2.4.4 cites a PostgreSQL community report of WAL archive corruption from "double switchover".

**WAL-G**
- **[report]** Issue #1723 (opened 2024-06-01, still open): restoring delta backups with `WALG_USE_RATING_COMPOSER: true` (WAL-G v2.0.1, PG15) gave `index ... contains unexpected zero page` errors. Full backups, and delta backups without the rating composer, were fine, and `bt_index_check` passed on the source. Root cause is unconfirmed — [wal-g #1723](https://github.com/wal-g/wal-g/issues/1723)
- **[report]** Issue #1871 (open): WAL part-file handling bugs cause missing delta files and fallback to full-scan delta. This is an efficiency problem, not corruption per the report — [wal-g #1871](https://github.com/wal-g/wal-g/issues/1871)
- **[report]** Issue #1140 (open): `--verify` page-checksum verification failed to detect a specific crafted corruption — [wal-g #1140](https://github.com/wal-g/wal-g/issues/1140)
- **[doc, unverified: taken from a search-result snippet; page not fetched]** WAL-G 2.0's Postgres delta backups are not backward-compatible with pre-2.0 versions — [WAL-G 2.0 announcement](https://www.postgresql.org/about/news/wal-g-20-released-2456/)

**pgBackRest**
- **[bug]** v2.28: `restore --force` used timestamp+size and left files stale — [release notes](https://pgbackrest.org/release.html)
- **[bug]** Block-incremental empty block map restore failure (Issue #2182, 2023) — [release notes](https://pgbackrest.org/release.html)

### Inferences
- **[inference] Chunk hashing.** The pg_probackup CRC false positive is a direct warning for the student tool. Use a cryptographic or 128-bit+ hash (SHA-256, BLAKE3, xxh128 with care) for skip decisions, not CRC32C, which is what PG manifests use by default.
- **[inference] Stale backups.** Several fixes say they do not repair already-produced artifacts. A refresh tool should record the source and destination minor versions in its metadata, so copies made across known-buggy windows can be flagged and fully rehashed.

### Gaps
- I found no confirmed ptrack-extension corruption bug in pg_probackup's release notes. I did not search the ptrack repository separately.
- I did not examine Barman bug history.
- WAL-G #1723's root cause is unknown.

---

## 6. Verification safeguards and their limits

### Takeaway
Manifests, page checksums and amcheck each catch different classes of damage. None of them proves that a delta copy has the correct *version* of every page. A stale but internally valid page passes manifest-less checksum checks and `pg_checksums`. Upstream explicitly says verification is not a substitute for test restores.

### Cited Findings
- **[doc]** pg_basebackup manifests list every file "with the exception of any WAL files", storing "size, last modification time, and an optional checksum". The default is CRC32C, and the manifest always includes "a SHA256 checksum of its own contents". It is only useful against an adversary if the manifest is "stored securely elsewhere". Page checksums are verified during backup by default — [pg_basebackup docs](https://www.postgresql.org/docs/current/app-pgbasebackup.html)
- **[doc]** pg_verifybackup checks the manifest's system identifier against `pg_control`, file presence and sizes, file checksums, and WAL parseability. It "does not and cannot include every check which will be performed by a running server ... you should still perform test restores". `-s` skips checksums and checks only presence and size — [pg_verifybackup docs](https://www.postgresql.org/docs/18/app-pgverifybackup.html)
- **[doc]** pg_combinebackup "only attempts to verify that the backups have the correct relationship to each other, not that each individual backup is intact" — [pg_combinebackup docs](https://www.postgresql.org/docs/current/app-pgcombinebackup.html)
- **[doc]** amcheck verifies "the logical consistency of the structure of relations". Two caveats: `verify_heapam` may itself error out if the facilities it relies on are corrupted, and `bt_index_parent_check` cannot run on hot standbys — [amcheck docs](https://www.postgresql.org/docs/18/amcheck.html)
- **[doc]** pg_probackup offers `--amcheck` logical index verification after file checks. Its CHECKSUM incremental-restore mode, "the most fool-proof", validates every page header and checksum and compares checksum+LSN with the backup — [pg_probackup docs](https://postgrespro.github.io/pg_probackup/)
- **[doc]** WAL-G `--verify` / `WALG_VERIFY_PAGE_CHECKSUMS` records up to 10 corrupt block numbers in the sentinel — [WAL-G docs](https://github.com/wal-g/wal-g/blob/master/docs/PostgreSQL.md)
- **[doc]** Data checksums are on by default for new PG18 clusters (initdb) — [PG 18.0 notes](https://www.postgresql.org/docs/release/18.0/)
- **[report]** WAL-G #1723 shows `bt_index_check` passing on the source while the restored delta copy had zero pages. Checks on the source say nothing about the copy — [wal-g #1723](https://github.com/wal-g/wal-g/issues/1723)

### Inferences
- **[inference] Stale pages pass checksums.** Page checksums detect torn or bit-rotted pages but not stale ones: an old version of a page has a valid checksum. The PG17 VM bug produced exactly such undetectable stale pages.
- **[inference] Verification ladder for the student tool:**
  1. A manifest of strong per-chunk hashes computed from the source at a known start LSN.
  2. Post-refresh rehash of the destination against that manifest. This proves the copy equals what was read, not that what was read was right.
  3. Start the destination in recovery to the stop LSN.
  4. `pg_amcheck --heapallindexed` plus a sampled logical comparison (row counts or checksums of `pg_dump`-style queries) against the source at the same LSN.
  5. Periodic full (non-delta) hash passes to bound how long a missed change can persist.

### Gaps
- I did not fetch Robert Haas's or other committers' blog posts on "verification ≠ recoverability". The pg_verifybackup docs quote is the primary source used here.
- I did not research PG19 changes to pg_verifybackup or amcheck.

---

## 7. Identity checks: system identifier, timelines, refusing to mix clusters

### Takeaway
All mainstream tools key on `pg_control`'s system identifier, and the stronger ones also check timeline history:
- pg_rewind, pg_combinebackup, pg_verifybackup, PG17 incremental and pg_probackup all refuse mismatched system identifiers.
- PG17 incremental and pg_probackup check timeline-history reachability.
- pg_rewind derives the divergence point from timeline history files.

PostgreSQL itself says these checks are not comprehensive, for example when two standbys are promoted to the same TLI.

### Cited Findings
- **[code]** pg_rewind: `if (ControlFile_target.system_identifier != ControlFile_source.system_identifier) pg_fatal("source and target clusters are from different systems")` — [pg_rewind.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/bin/pg_rewind/pg_rewind.c)
- **[code]** pg_combinebackup checks the manifest system identifier against each backup's control file, and that "System identifiers should all match" across the chain — [pg_combinebackup.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/bin/pg_combinebackup/pg_combinebackup.c)
- **[code]** Incremental backup (server side) runs these checks — [basebackup_incremental.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/backup/basebackup_incremental.c):
  - System identifier: "system identifier in backup manifest is %" PRIu64 ", but database system identifier is %" PRIu64.
  - Timeline membership: "timeline %u found in manifest, but not in this server's history".
  - WAL-range checks against timeline switch points.
  - WAL-summary coverage: "WAL summaries are required on timeline %u from %X/%X to %X/%X, but no summaries ... exist".
- **[doc]** Incremental backup needs summaries covering "all LSNs from the start LSN of the prior backup to the start LSN of the current backup", or it fails — [Continuous Archiving: Making an Incremental Backup](https://www.postgresql.org/docs/18/continuous-archiving.html)
- **[doc]** pg_verifybackup is fatal if the manifest "fails to match the system identifier with pg_control of the backup directory" — [pg_verifybackup docs](https://www.postgresql.org/docs/18/app-pgverifybackup.html)
- **[doc]** pg_probackup checks, in every incremental-restore mode, "that postmaster in given destination directory is not running and system-identifier is the same as in the backup". LSN mode additionally checks that the (redo LSN, redo TLI) shiftpoint is within the backup chain's history — [pg_probackup docs](https://postgrespro.github.io/pg_probackup/)
- **[doc]** pg_rewind requires a cleanly shut-down target. By default it runs single-user crash recovery first if the target was not cleanly stopped. It also needs the target's WAL back to the divergence point, from `pg_wal` or `-c` restore_command — [pg_rewind docs](https://www.postgresql.org/docs/current/app-pgrewind.html)
- **[doc]** The pg_upgrade standby procedure requires verifying that "Latest checkpoint location" values match across the old primary and standbys before rsync — [pg_upgrade docs](https://www.postgresql.org/docs/18/pgupgrade.html)
- **[code]** pg_upgrade resets the new cluster to timeline 1 with no history file — [pg_upgrade.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/bin/pg_upgrade/pg_upgrade.c)

### Inferences
- **[inference] Minimum identity gate before any delta refresh:**
  1. Source and destination have an equal system identifier, otherwise do a full copy or full hash.
  2. Equal major version and catalog version (`PG_VERSION` and the catalog version in `pg_control`). Equal `data_checksum_version`, block size and segment size.
  3. The destination is cleanly stopped, or has a known recovery state. Read its redo LSN and TLI from `pg_control`.
  4. The destination's (redo LSN, TLI) lies on the source's timeline history (`*.history` files) at or before the source's corresponding switch point. If not, refuse the LSN fast path and fall back to full hashing, or run pg_rewind first.
  5. Record the source and destination minor versions, so copies taken across known-buggy windows (e.g. VM clears before 17.11/18.6) can be flagged.
- **[inference] Timeline 1 after pg_upgrade.** Because pg_upgrade restarts on TLI 1, a post-upgrade source can show TLI 1 with LSNs that overlap a pre-upgrade copy's TLI 1. The system-identifier gate, not the TLI check, is what catches this.

### Gaps
- I did not verify pgBackRest's or WAL-G's exact system-identifier and stanza checks from source.
- I did not research AlloyDB- or Neon-specific identity semantics, for example disaggregated storage where page LSNs may follow different rules.
