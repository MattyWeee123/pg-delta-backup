# Third-party PostgreSQL backup/restore tools: change detection and data reuse (as of October 22, 2026)

Scope note: These notes cover pgBackRest, Barman, WAL-G, pg_probackup, Kubernetes operators, Patroni, repmgr, and related tools. Core PostgreSQL features (PG17 `pg_basebackup --incremental`, `pg_combinebackup`, `pg_rewind`) appear only where a third-party tool depends on them. Many findings come straight from current source code on each project's default branch. These are marked "(source)" and should be read as documented-by-code, not as a vendor claim. Versions current at the time of writing: pgBackRest v2.60.0 (2026-10-18), Barman 3.20.1 (2026-09-29), WAL-G v3.0.9 (2026-08-20), pg_probackup 2.5.16 (2025-12-09). Sources: [pgBackRest releases](https://github.com/pgbackrest/pgbackrest/releases), [Barman releases](https://github.com/EnterpriseDB/barman/releases), [WAL-G releases](https://github.com/wal-g/wal-g/releases), [pg_probackup releases](https://github.com/postgrespro/pg_probackup/releases).

## Q1. pgBackRest: full/diff/incr, block incremental, and delta restore

### Takeaway
pgBackRest has three distinct reuse layers:
- **Backup, file level.** Files are compared by timestamp and size against the prior manifest by default. Checksums are used instead when `--delta` is set, and are switched on automatically when anomalies appear.
- **Backup, block level.** Block incremental (`repo-block`, GA in v2.46, requires `repo-bundle`) stores only changed blocks. Block size is 8 KiB to 88 KiB, chosen by file size and age, and each block carries an xxHash checksum in a per-file block map.
- **Restore.** Delta restore (`--delta`) SHA-1-hashes every destination file and keeps matches. For block-incremental files it computes per-block checksums on the destination and fetches only the blocks whose checksum differs from the repository's block map.

Of all the surveyed tools, this is the closest analogue to a content-hash chunk resync. It only works from pgBackRest's own repository, and the file-level backup layer trusts mtime and size.

### Cited Findings
**Backup types and file-level change detection**
- The backup `--type` values are `full` (no dependencies), `incr` (from the last successful backup; this is the default), and `diff` (like incr but always based on the last full) — [pgBackRest command reference](https://pgbackrest.org/command.html).
- The user guide says diff and incr backups copy "only those database cluster files that have changed" since the last full or last backup. Without block incremental, the whole file is copied if any part changed — [User guide](https://pgbackrest.org/user-guide.html); [Crunchy Data blog (David Steele, 2023-06-09)](https://crunchydata.com/blog/pgbackrest-file-bundling-and-block-incremental-backup).
- `--delta` on backup: "During a backup, this option will use checksums instead of the timestamps to determine if files will be copied." Default is `n` — [Configuration reference](https://pgbackrest.org/configuration.html).
- (source) In a non-delta incr or diff, a file is not copied when its size and timestamp both equal the prior manifest's. In delta mode, a checksum is computed only when sizes are equal and non-zero; files of unequal length are always copied — [manifest.c](https://github.com/pgbackrest/pgbackrest/blob/main/src/info/manifest/manifest.c).
- (source) Checksum delta switches on automatically, with a WARN, when any of these occur:
  - A timeline switch since the prior backup ("HINT: this is normal after restoring from backup or promoting a standby").
  - The `online` option changed.
  - A file timestamp is earlier than in the prior backup.
  - A file's size changed while its timestamp did not.
  - A file timestamp is in the future relative to copy start.
  - Source: [manifest.c](https://github.com/pgbackrest/pgbackrest/blob/main/src/info/manifest/manifest.c).
- Version history from the release notes:
  - Checksum delta for incremental backups was added in v2.06 (2018-10-15).
  - Automatic enabling on anomalies such as a timeline switch was added in v2.07 (2018-11-16).
  - v2.47 (2023-07-24) fixed a "spurious automatic delta backup on backup from standby": standby file sizes can differ from the primary's (for example free space maps), which triggered a slow checksum delta. The original size is now kept in the manifest.
  - v2.51 (2024-03-25): "During a non-delta diff or incr backup, a file that fits in a single buffer, or any block incremental file, is compared to the file in the prior backup and referenced instead of stored when it has not changed."
  - Source: [Release notes](https://pgbackrest.org/release.html).
- `--checksum-page` validates PostgreSQL page checksums during backup. It is enabled automatically when data checksums are on, and failures only warn — [Configuration reference](https://pgbackrest.org/configuration.html). v2.60.0 (2026-10-18) added `checksum-page-error`: restore and expire now error by default on invalid page checksums, where previously "the restore command did not report invalid page checksums at all" — [Release notes](https://pgbackrest.org/release.html).
- `repo-hardlink` hardlinks unchanged files between backups in the repository; the default is `n` — [Configuration reference](https://pgbackrest.org/configuration.html). Release notes show `repo-hardlink` is disabled when `repo-bundle` is enabled (v2.39 era commit "Disable repo-hardlink option when repo-bundle option is enabled") — [Release notes](https://pgbackrest.org/release.html).

**Block incremental**
- Requirements and version history:
  - It requires bundling: "The repo-bundle option must be enabled before repo-block can be enabled" — [Configuration reference](https://pgbackrest.org/configuration.html).
  - Bundling arrived in v2.39 (2022-05-16).
  - Block incremental was BETA in v2.45 (2023-03-20), with "Do not use it in production: the on-disk format will change", and GA in v2.46 (2023-05-22) — [Release notes](https://pgbackrest.org/release.html).
  - The current user guide says "The repo-block option was introduced in v2.46 but at least v2.52.1 is recommended" — [User guide](https://pgbackrest.org/user-guide.html).
- Bundle defaults: `repo-bundle-size` 20 MiB and `repo-bundle-limit` 2 MiB (larger files are stored separately). "File bundles cannot be resumed" — [Configuration reference](https://pgbackrest.org/configuration.html); [User guide](https://pgbackrest.org/user-guide.html).
- (source) Block size by file size, from the default size map: a file of at least 16 KiB uses 8 KiB blocks. The block size steps up as the file grows: 16 KiB at ≥11 MiB, 24 KiB at ≥43 MiB, 32 KiB at ≥96 MiB, and so on up to 88 KiB at ≥914 MiB. The code comment says "Block size is increased when the block map would be larger than a single block" — [option.c.inc](https://github.com/pgbackrest/pgbackrest/blob/main/src/command/backup/option.c.inc).
- (source) Age map: files untouched for 7 days get a ×2 block multiplier and 14 days ×4. At 28 days the multiplier is 0, meaning the file is not block-incremented. The per-block checksum size grows with block size, through a separate checksum-size map — [option.c.inc](https://github.com/pgbackrest/pgbackrest/blob/main/src/command/backup/option.c.inc). The internal options `repo-block-size-map`, `repo-block-age-map`, and `repo-block-checksum-size-map` exist. `repo-block-size-super` defaults to 256 KiB, and to 1 MiB for full backups — [build/config.yaml](https://github.com/pgbackrest/pgbackrest/blob/main/build/config.yaml).
- (source) Block map format: each map item records the reference (which backup holds the block), super block size, bundle id, offset, stored size, block number within the super block, and an xxHash checksum. "This map does not store the blocks themselves, just the location where they can be found" — [blockMap.h](https://github.com/pgbackrest/pgbackrest/blob/main/src/command/backup/blockMap.h). The v2.45 beta commits "Use xxHash instead of SHA-1 for block incremental checksums" — [Release notes](https://pgbackrest.org/release.html).
- Blocks are compressed and encrypted into super blocks "that can be retrieved independently to make restore more efficient". Larger and older files get larger block sizes. The same post advises: "avoid long chains of block incremental backups since they can have a negative impact on restore performance" — [Crunchy Data blog](https://crunchydata.com/blog/pgbackrest-file-bundling-and-block-incremental-backup).
- Block-incremental bug history:
  - v2.48: a block map with no blocks, written "when a file timestamp changes but the file does not", was not decrypted in some bundle reads.
  - v2.50: short read in block incremental restore.
  - v2.52.1: with backup from standby, a size mismatch between primary and standby made a later backup fail.
  - v2.54.2: disabling bundling after block incremental made prior backups unreadable.
  - v2.55.0: a restore from a non-default repo read block data from the lowest-numbered repo.
  - v2.59.2 (2026-09-27): a file truncated to zero during backup produced an empty block map, and "Every later backup that referenced the file failed with unexpected eof until the next full backup".
  - v2.59.2 also fixed a crash on retried reads through a repository host (Issue #2845).
  - Source: [Release notes](https://pgbackrest.org/release.html). Segfaults during long parallel block-incremental restores were reported in [#2748](https://github.com/pgbackrest/pgbackrest/issues/2748) (v2.58.0, PG18, crash after about 21 hours) and [#2845](https://github.com/pgbackrest/pgbackrest/issues/2845).

**Delta restore**
- Mechanism: delta restore "is accomplished by calculating a SHA-1 cryptographic hash for each file in the database cluster directory. If the SHA-1 hash does not match the hash stored in the backup then that file will be restored." It "also removes files not present in the backup manifest so it will dispose of divergent changes" — [User guide](https://pgbackrest.org/user-guide.html). The README adds that the manifest holds checksums for every file and that extra files are removed first — [README](https://github.com/pgbackrest/pgbackrest/blob/main/README.md).
- `--force` alone overwrites the data and tablespace paths completely. "In combination with --delta a timestamp/size delta will be performed instead of using checksums" — [Command reference](https://pgbackrest.org/command.html). (source) In force-delta mode a file is kept only if size and mtime equal the manifest and mtime is earlier than the backup's copy start time — [restore/file.c.inc](https://github.com/pgbackrest/pgbackrest/blob/main/src/command/restore/file.c.inc). Bug history: v2.28 fixed "restore --force acting like --force --delta", which "meant some files that should have been updated were left unchanged" — [Release notes](https://pgbackrest.org/release.html).
- (source) Per-file checksum delta logic:
  - The delta proceeds only if the destination file is at least the expected size, or, for block-incremental files, non-zero size.
  - Larger files are first truncated to the manifest size, then hashed.
  - A file whose size and checksum match is preserved, and its mtime is reset to the backup's.
  - For block-incremental files, the restore builds a per-block checksum list of the existing file. It reads the repository's block map, and only blocks "beyond the blocks that exist in the block checksum list or when the checksum stored in the repository is different" are fetched and written in place. A full-file SHA-1 is recomputed afterwards as a safeguard.
  - Source: [restore/file.c.inc](https://github.com/pgbackrest/pgbackrest/blob/main/src/command/restore/file.c.inc), [blockDelta.c](https://github.com/pgbackrest/pgbackrest/blob/main/src/command/restore/blockDelta.c), [blockChecksum.c (xxHash)](https://github.com/pgbackrest/pgbackrest/blob/main/src/command/restore/blockChecksum.c).
- Related release notes:
  - v2.40 (2022-07-18): "Delta restore copied a file from the repository whenever its size differed from the manifest. A larger file is now truncated to the size in the manifest and copied only when its checksum does not match."
  - v2.50: "Preserve partial files during block incremental delta restore", for example when retrying a failed restore.
  - v2.51: skip zero-length files for block incremental delta restore.
  - v2.59.0 (2026-07-20): "Improve seek performance during block incremental delta restore".
  - v2.60.0: object-store prefetch for bundles and block maps.
  - Source: [Release notes](https://pgbackrest.org/release.html).
- Safeguards and pitfalls:
  - (source) Restore refuses to run if `postmaster.pid` exists.
  - With `--delta` or `--force`, pgBackRest requires `PG_VERSION` or `backup.manifest` in the target. Otherwise "--delta and --force have been disabled" and the restore fails if files exist — [validate.c.inc](https://github.com/pgbackrest/pgbackrest/blob/main/src/command/restore/validate.c.inc); first added in v0.90 per [Release notes](https://pgbackrest.org/release.html).
  - "Exclusions are not honored on delta restores. Any files/directories that were excluded by the backup will be removed on delta restore" — [Command reference](https://pgbackrest.org/command.html).
  - v2.37 fixed delta restore leaving an existing path in place when link-map mapped it to a link — [Release notes](https://pgbackrest.org/release.html).
- Delta restore is also used for standby rebuilds. The user guide's standby restore is `pgbackrest --stanza=demo --delta --type=standby restore`, and "any cluster may be restored as a standby later, e.g. a primary that is rebuilt as a standby after failover" — [User guide](https://pgbackrest.org/user-guide.html).
- Parallelism and compression:
  - `process-max` defaults to 1, "which is almost never the appropriate value". For delta restore the docs say "a larger number of processes can be used than might be desirable during a backup" since PostgreSQL is down — [User guide](https://pgbackrest.org/user-guide.html).
  - `compress-type` is one of none, bz2, gz (default), lz4, or zst, and zst is recommended — [Configuration reference](https://pgbackrest.org/configuration.html); [User guide](https://pgbackrest.org/user-guide.html).
  - The README: "Compression is usually the bottleneck during backup operations so pgBackRest solves this problem with parallel processing" — [README](https://github.com/pgbackrest/pgbackrest/blob/main/README.md).
- Project status: on 2026-04-27 the maintainer posted "pgBackRest is no longer being maintained". On 2026-05-18 he posted "pgBackRest will continue!", funded by a sponsor coalition (AWS, Supabase, pgEdge, Tiger Data, Percona, Eon, Xata, Dalibo, Data Egret), after Crunchy Data had been the main past sponsor — [pgBackRest news](https://pgbackrest.org/news.html); [README](https://github.com/pgbackrest/pgbackrest/blob/main/README.md). Commentary written during the gap called it "unmaintained" — [The Build, Christophe Pettus, 2026-04-30](https://thebuild.com/blog/after-pgbackrest/).

### Inferences
- In the backup path, pgBackRest trusts filesystem mtime and size by default; it switches to checksums only under detected anomalies or `--delta`. The restore path, by contrast, is content-verified (SHA-1 per file, xxHash per block), so delta restore is the safe direction.
- (source-based) The restore code never compares the destination cluster's system identifier with the backup. Delta restore will reuse matching content from any directory that has `PG_VERSION`. That is safe because every reused file or block is hash-verified, which is the same trust model as a content-hash resync tool.
- pgBackRest's block-level delta restore only saves transfer when the stale copy is aligned with blocks pgBackRest has already stored. The source of truth is the repository, not a live cluster. Refreshing a stale copy from a live primary therefore needs a new backup into the repository first (block incremental keeps that cheap), then a delta restore. That is two hops, and it needs repository storage.
- Block granularity is coarse for large or old files: up to 88 KiB blocks for 1 GiB segments, up to ×4 with the age multiplier, and none at all after 28 days. One changed 8 KiB page can cost 88 KiB to 352 KiB of transfer. Very old files fall back to whole-file copy.

### Gaps
- No official benchmark of delta restore on a large cluster; see Q7 for the one small-scale vendor demo.
- I did not confirm whether pgBackRest delta restore preserves `postgresql.auto.conf` or recovery settings beyond what the restore command writes; check the restore docs if needed.

## Q2. Barman: rsync vs postgres, reuse_backup, PG17 block incremental, recovery into an existing directory

### Takeaway
Barman has two incompatible incremental mechanisms:
- **File-level.** rsync with `reuse_backup = link|copy` (since 1.4.0, 2015): hardlink or copy unchanged files, with rsync deciding what changed.
- **Block-level.** PostgreSQL 17+ native incremental via `pg_basebackup --incremental` (since Barman 3.11.0, 2024-08-22). It requires `summarize_wal`, and restore reconstructs with `pg_combinebackup`.

Recovery into a non-empty directory ("delta restore") uses rsync. It became an explicit `--delta-restore` flag in 3.16.0 (2025-10-02), was broken until 3.17.0, and is supported only for local restores of plain (non-incremental, uncompressed, unencrypted) backups or remote restores with local staging.

### Cited Findings
- History: "Prior to version 2.0, Barman relied solely on rsync for both standard backups and file-level incremental backups. Streaming backups were introduced in this version. Starting with version 3.11, Barman also supports block-level incremental backups" — [Barman 3.20.1 backup docs](https://docs.pgbarman.org/release/3.20.1/user_guide/backup.html).
- File-level `reuse_backup`:
  - The values are `off` (default), `link` ("reuses the last backup and creates hard links for unchanged files, reducing both backup space and time"), and `copy` ("reducing backup time but not space").
  - rsync file-level incremental backups "are self-contained. If a parent backup is deleted, the integrity of other backups is not affected."
  - Source: [Barman backup docs](https://docs.pgbarman.org/release/3.20.1/user_guide/backup.html). The feature was introduced in 1.4.0 (2015-01-26) — [Barman release notes](https://docs.pgbarman.org/release/3.20.1/releases/index.html).
- (source) Barman's rsync "smart copy" uses a *safe horizon* timestamp:
  - Files older than the safe horizon are copied with rsync's normal mtime and size quick check.
  - Newer files go on a "check list" copied with rsync `--checksum`, unless they already differ in mtime or size, in which case rsync detects the change anyway.
  - Extra destination files are purged with `rsync --delete`.
  - Source: [copy_controller.py](https://github.com/EnterpriseDB/barman/blob/master/src/barman/copy_controller.py).
- (source) For recovery, the safe horizon is `min(backup.begin_time, previously-recovered-backup.begin_time)`, read from a `.barman-recover.info` file in the destination. If that file is missing, the safe horizon is None, which "will effectively disable the time-based part of smart_copy method" and checksums every file — [recovery_executor.py](https://github.com/EnterpriseDB/barman/blob/master/src/barman/recovery_executor.py).
- PG17 block-level incremental: `barman backup --incremental BACKUP_ID SERVER_NAME`. Requirements:
  - "Use Postgres 17 or later".
  - "summarize_wal must be enabled on your database server before taking the initial full backup".
  - `backup_method=postgres`.
  - The docs warn that enabling `data_checksums` between incrementals "can potentially cause issues during recovery", so take a new full backup.
  - Source: [Barman backup docs](https://docs.pgbarman.org/release/3.20.1/user_guide/backup.html).
- "Incremental backups of different types are not compatible with each other", for example no block-level incremental on top of an rsync backup — [Barman backup docs](https://docs.pgbarman.org/release/3.20.1/user_guide/backup.html).
- Restoring block-level incrementals runs `pg_combinebackup` over the chain, either in the destination or in a staging path. `--combine-mode` takes copy, link, clone, or copy-file-range — [Barman 3.20.1 recovery docs](https://docs.pgbarman.org/release/3.20.1/user_guide/recovery.html); [Release notes](https://docs.pgbarman.org/release/3.20.1/releases/index.html).
- Related version history, from the [Barman release notes](https://docs.pgbarman.org/release/3.20.1/releases/index.html):
  - 3.15.0 (2025-08-05) allowed incremental backups with compression and encryption.
  - 3.18.0 (2026-03-12) added block-level incremental to cloud storage, uploading "only changed data".
  - 3.20.1 fixed restoring cloud incrementals on a different Barman instance.
- Delta recovery:
  - `--delta-restore` lets Barman "restore a backup into a pre-existing destination directory along with any custom tablespace by overwriting it". It can also be set as `recovery_options = 'delta-restore'`.
  - It is supported "only" for "Local restore of plain backups - incremental, compressed, encrypted, or multi-format backups are *not* supported" and for remote restore with `staging_location=local`.
  - Source: [Barman recovery docs](https://docs.pgbarman.org/release/3.20.1/user_guide/recovery.html).
- Delta recovery version history, from the [Barman release notes](https://docs.pgbarman.org/release/3.20.1/releases/index.html):
  - 3.15.0 started failing restores when target data or tablespace directories are not empty.
  - 3.16.0 (2025-10-02) added `--delta-restore` ("reusing identical data already present in the destination directory, which drastically reduces restore time and network traffic").
  - 3.17.0 (2026-01-07) fixed "delta restore not working" because Barman "was removing the destination directory before performing a restore".
  - 3.20.0 (2026-08-27) fixed `--delta-restore` failing with snapshot backups.
- Third-party commentary: Barman's incremental and delta features via rsync are "functional but less aggressive" than pgBackRest's manifest-driven approach — [The Build](https://thebuild.com/blog/after-pgbackrest/) (opinion, not measured).

### Inferences
- Barman's delta recovery is rsync's rolling-checksum delta transfer, run in `--checksum` mode for recently modified files. It does not use page LSNs or PostgreSQL metadata beyond backup begin times. Its granularity is rsync's block-level delta, not 8 KiB pages.
- PG17 incremental backups cannot be delta-restored into an existing directory, because combining happens first and delta-restore excludes incremental backups. Barman therefore cannot refresh a stale copy in place from a block-level incremental chain.

### Gaps
- I found no Barman performance numbers for delta-restore or PG17 incremental.
- I did not confirm the exact rsync flags Barman passes for recovery beyond `--delete` and `--checksum`. In particular, whether `--inplace` or `--no-whole-file` is used affects whether rsync's delta algorithm actually runs locally. Note that rsync defaults to whole-file copy for local-to-local transfers.

## Q3. WAL-G: delta backups, page-LSN vs WAL-delta detection, and catchup

### Takeaway
WAL-G delta backups (`WALG_DELTA_MAX_STEPS` > 0) work as follows:
- Files are selected by mtime. Paged files are scanned and pages whose header LSN is at or above the base backup's LSN are kept, or all pages are kept if a page is new.
- An optional WAL-parsing mode (`WALG_USE_WAL_DELTA`, default false) builds the changed-block map from archived WAL instead of scanning.
- Restore (`backup-fetch`) requires an empty directory, so there is no in-place delta restore.

WAL-G does have in-place refresh of a stopped lagging standby. `catchup-push`/`catchup-fetch` go through storage; `catchup-send`/`catchup-receive` go directly over TCP. Both use the standby's pg_control checkpoint LSN as the threshold, skip files whose mtime equals the destination's, send page increments for existing paged files, delete removed files, and require the same system identifier and the same timeline. The send/receive path has known open bugs (tablespaces, missing directories), with fixes proposed in 2026.

### Cited Findings
- Delta settings:
  - `WALG_DELTA_MAX_STEPS` "determines how many delta backups can be between full backups. Defaults to 0 (disabled)." "Delta computation is based on ModTime of file system and LSN number of pages in datafiles."
  - `WALG_DELTA_ORIGIN` is LATEST (chain) or LATEST_FULL.
  - `WALG_FORCE_WAL_DELTA` exists "To prevent WAL-G from falling back to a full scan delta backup when it fails to download delta files".
  - Source: [WAL-G PostgreSQL.md](https://github.com/wal-g/wal-g/blob/master/docs/PostgreSQL.md).
- `WALG_USE_WAL_DELTA` defaults to `"false"` — [internal/config/config.go](https://github.com/wal-g/wal-g/blob/master/internal/config/config.go). It is not described in PostgreSQL.md; only `WALG_FORCE_WAL_DELTA` is.
- (source) In WAL-delta mode, `wal-push` produces `*_delta` files, each covering 16 WAL segments (`WalFileInDelta = 16`). At backup time WAL-G downloads delta files plus the remaining WAL to build a `PagedFileDeltaMap` between the base backup's LSN and the new start LSN — [wal_delta_util.go](https://github.com/wal-g/wal-g/blob/master/internal/databases/postgres/wal_delta_util.go); [delta_map_downloader.go](https://github.com/wal-g/wal-g/blob/master/internal/databases/postgres/delta_map_downloader.go).
- (source) Page selection in full-scan mode keeps a page if its header "isNew" or `lsn() >= pageReader.Lsn`. An invalid page header returns `InvalidBlockError`; the catchup path then falls back to sending the whole file. The increment file format is a 4-byte header, then an 8-byte file size, then the count N, then N×4-byte block numbers, then N pages. Code TODOs note a possible torn page and "possible race condition - page was deleted between blocks extraction and seek" — [incremental_page_reader.go](https://github.com/wal-g/wal-g/blob/master/internal/databases/postgres/incremental_page_reader.go); [pagefile.go](https://github.com/wal-g/wal-g/blob/master/internal/databases/postgres/pagefile.go).
- Restore-side composers (none of them reuse destination data):
  - "Reverse delta unpack" (beta) and "redundant archives skipping" reduce restore work for delta chains.
  - "copy composer" makes a full backup "and copies unchanged tar files from previous full backup".
  - "rating composer" groups files by update frequency.
  - Source: [WAL-G PostgreSQL.md](https://github.com/wal-g/wal-g/blob/master/docs/PostgreSQL.md).
- (source) When unpacking a non-incremental (full/base) backup, `backup-fetch` errors with "Directory %v for delta base must be empty" if the target is non-empty. Delta layers are then applied on top of the freshly unpacked base within the same fetch — [backup_fetch_handler.go](https://github.com/wal-g/wal-g/blob/master/internal/databases/postgres/backup_fetch_handler.go); [backup.go `checkDBDirectoryForUnwrap`](https://github.com/wal-g/wal-g/blob/master/internal/databases/postgres/backup.go). `catchup-fetch` calls the unpack routine directly, bypassing the empty check, which is what allows in-place catchup — [catchup_fetch_handler.go](https://github.com/wal-g/wal-g/blob/master/internal/databases/postgres/catchup_fetch_handler.go).
- `catchup-push`/`catchup-fetch` workflow: "1) Stop replica 2) Get replica LSN (for example using pg_controldata command) 3) Start uploading incremental backup on master", then `wal-g catchup-push /path/to/master/postgres --from-lsn replica_lsn`, then `wal-g catchup-fetch /path/to/replica/postgres backup_name` — [WAL-G PostgreSQL.md](https://github.com/wal-g/wal-g/blob/master/docs/PostgreSQL.md). PostgreSQL `X/X` LSN format for `--from-lsn` was accepted via PR [#2643](https://github.com/wal-g/wal-g/pull/2643), merged 2026-10-06.
- `catchup-send`/`catchup-receive`: "These commands are used in conjunction to catchup lagging replica. On a standby you should run catchup-recieve, then on a primary catchup-send. Standby Postgres must be stopped during this procedure." Usage: `wal-g catchup-receive ${PGDATA_STANDBY} 1337 &` and `wal-g catchup-send ${PGDATA_PRIMARY} hostname:1337` — [WAL-G PostgreSQL.md](https://github.com/wal-g/wal-g/blob/master/docs/PostgreSQL.md).
- (source) The catchup-send algorithm, from [catchup_send_recieve_handler.go](https://github.com/wal-g/wal-g/blob/master/internal/databases/postgres/catchup_send_recieve_handler.go):
  1. The receiver sends its pg_control data and a file list containing each file's mtime.
  2. The sender exits if the system identifiers differ ("System identifiers do not match") or if `control.CurrentTimeline != info.Timeline` ("Destination is on timeline %v, but we are on %v").
  3. The sender calls `StartBackup` and exits if its start LSN ≤ the destination checkpoint ("Catchup destination is already ahead").
  4. It walks PGDATA. A file whose mtime equals the destination's mtime is skipped ("No need to catchup"). Paged files that exist on the destination are sent as page increments with LSN ≥ the destination checkpoint; all other files are sent whole.
  5. Files absent on the source are deleted on the destination.
  6. After `StopBackup`, it sends `backup_label`, `tablespace_map`, and finally the source's `pg_control`.
- Why the direct path exists: catchup "via storage doubles the number of bytes that need to be transferred, making the catchup process twice as slow", which motivated direct catchup — [WAL-G issue #1629 "Fast PG catchup" (closed 2024-01-21)](https://github.com/wal-g/wal-g/issues/1629).
- Known catchup bugs and limitations:
  - "[BUG] catchup-send doesn't work with tablespaces" (v3.0.7, open) — [#2068](https://github.com/wal-g/wal-g/issues/2068).
  - Open PR [#2220](https://github.com/wal-g/wal-g/pull/2220) (2026-03-25) fixes four catchup bugs:
    - Wrong order of the error check.
    - "Missing parent-directory creation on the receiver side" (for example new tablespace directories).
    - Directories in the receiver file list causing failed deletes that abort the transfer.
    - An unclosed listen socket.
  - Open PR [#2221](https://github.com/wal-g/wal-g/pull/2221): catchup-receive "used to fatal immediately when the target directory had no pg_control".
  - Open PR [#2222](https://github.com/wal-g/wal-g/pull/2222) adds parallel send/receive. That implies the current transfer is serial: the code sends 8 KiB gob-encoded chunks.
  - A user report says that after catchup-fetch the standby "was stuck on a single WAL" until `restore_command` was added, and asks about stuck replication slots — [#1536](https://github.com/wal-g/wal-g/issues/1536).
- Delta-backup correctness report: "Corrupted Restore (Indexes) on Delta with rating composer". There were index "unexpected zero page" errors only with `WALG_USE_RATING_COMPOSER` together with delta backups, on WAL-G v2.0.1 and PG15. The issue is still open — [#1723](https://github.com/wal-g/wal-g/issues/1723).
- Catchup history: a WAL-G v0.2.15 announcement described the "backup for catchup" feature, the catchup-push/fetch pair — [postgrespro mailing list thread](https://www.postgrespro.com/list/thread-id/2483538) (this is a search-result summary; I did not read the thread directly).

### Inferences
- (source-based) WAL-G reads the destination threshold from pg_control bytes 32–40 ([pg_control_data.go](https://github.com/wal-g/wal-g/blob/master/internal/databases/postgres/pg_control_data.go)). Under PostgreSQL's ControlFileData layout, that is the `checkPoint` record location, not the checkpoint's REDO pointer. pg_probackup catchup uses the destination's redo LSN instead. A page modified between the destination's REDO and checkpoint-record LSN might be skipped by WAL-G. This needs verification; it may be safe in practice because the destination later replays from the source's backup start LSN.
- The mtime-equality skip is a heuristic. A primary and a standby normally have different mtimes, so it rarely triggers, but it is not content-verified. Non-paged files that pass the mtime check are sent whole, as are paged files new to the destination.
- WAL-G catchup requires the same timeline. A standby left behind on an old timeline after a failover cannot be caught up this way; pg_rewind or a full re-clone is needed.

### Gaps
- I found no version note for when catchup-send/receive was added, and no performance numbers for catchup.
- The merge status of PRs #2220/#2221/#2222 as of 2026-10-22 is unconfirmed; they showed as open in search.

## Q4. pg_probackup: FULL/DELTA/PAGE/PTRACK, catchup, incremental restore

### Takeaway
pg_probackup has the richest page-level toolkit:
- **Backup modes.** DELTA scans all pages and compares page LSN with the parent backup's start LSN. PAGE builds a changed-block map from archived WAL. PTRACK uses an extension's on-the-fly changed-block bitmap.
- **catchup (FULL/DELTA/PTRACK).** Re-syncs a stopped, stale standby directly from a running primary, with no backup catalog.
- **Incremental restore.** CHECKSUM mode compares per-page checksum and LSN with the backup; LSN mode replaces pages with LSN > a "shiftpoint" derived from the destination's pg_control redo LSN and timeline, and requires data checksums.

Safeguards include system-identifier checks, a postmaster-running check, timeline-history checks, a `backup_label` check (LSN mode), and renaming pg_control to `pg_control.pbk.bak` during catchup. The open-source project is low-activity: 2.5.16 (Dec 2025) added PG18 support. A PG17 WAL-summary ("SUMMARIZE") mode exists only as an open PR. Postgres Pro now also ships a separate pg_probackup3.

### Cited Findings
**Backup modes**
- DELTA "reads all data files in the data directory and copies only those pages that have changed since the previous backup", and "can impose read-only I/O pressure equal to a full backup" — [pg_probackup docs](https://postgrespro.github.io/pg_probackup/). (source) A page is skipped when `page_st->lsn < prev_backup_start_lsn` in DELTA and PTRACK modes — [data.c](https://github.com/postgrespro/pg_probackup/blob/master/src/data.c).
- PAGE "scans all WAL files in the archive from the moment the previous full or incremental backup was taken" and "requires all the WAL files since the previous backup to be present in the WAL archive" — [pg_probackup docs](https://postgrespro.github.io/pg_probackup/).
- PTRACK: "PostgreSQL tracks page changes on the fly" in a PTRACK bitmap. It requires Postgres Pro Standard/Enterprise "or patched vanilla PostgreSQL" plus the ptrack extension with `ptrack.map_size` > 0, and "Continuous archiving is not necessary" — [pg_probackup docs](https://postgrespro.github.io/pg_probackup/).
- (source) Non-data files in incremental modes are reused only if size equals the previous file's, mtime ≤ the parent backup time, and CRC32 matches. `pg_filenode.map` is always copied — [data.c `backup_non_data_file`](https://github.com/postgrespro/pg_probackup/blob/master/src/data.c).

**catchup**
- Purpose: "cloning a PostgreSQL instance for a fallen-behind standby server to 'catch up' with master… The catchup command allows you to update an existing replica much faster by bringing differences from master" — [pg_probackup docs](https://postgrespro.github.io/pg_probackup/).
- Properties:
  - "The backup catalog is not required. STREAM WAL delivery mode is only supported. Copying external directories is not supported."
  - CREATE/DROP TABLESPACE "cannot be run simultaneously with catchup".
  - Config files (`postgresql.conf`, `postgresql.auto.conf`, `pg_hba.conf`) are taken from the source and overwrite the target unless `--exclude-path` is used.
  - `--dry-run` "Displays the total size of the files to be transferred".
  - `-j` sets parallel threads.
  - Source: [pg_probackup docs](https://postgrespro.github.io/pg_probackup/).
- Modes:
  - FULL needs an empty destination.
  - DELTA "reads all data files in the data directory and creates an incremental copy for pages that have changed since the destination instance was shut down".
  - PTRACK "only reads and copies pages that have changed since the point of divergence" and requires PTRACK ≥ 2.0, which means PostgreSQL ≥ 11.
  - Source: [pg_probackup docs](https://postgrespro.github.io/pg_probackup/).
- (source) Catchup checks, from [catchup.c](https://github.com/postgrespro/pg_probackup/blob/master/src/catchup.c):
  - Errors if the destination is empty in incremental mode, or non-empty in FULL mode.
  - Errors if a postmaster is running in the destination, or if `backup_label` is present.
  - Requires "Database identifiers mismatch" not to fire, meaning the system identifiers must match.
  - The destination redo TLI/LSN must lie in the source's timeline history, fetched with the replication command TIMELINE_HISTORY; otherwise "Destination is not in source timeline history".
  - For PTRACK, errors if "LSN from ptrack_control in source … is greater than checkpoint LSN in destination".
  - Errors if the source start LSN is below the destination redo LSN ("trying to catchup with PostgreSQL instance from the past").
- (source) Catchup execution, from [catchup.c](https://github.com/postgrespro/pg_probackup/blob/master/src/catchup.c):
  - The sync LSN is the destination redo LSN from pg_control (`get_redo`).
  - Redundant destination files are deleted.
  - The destination `global/pg_control` is renamed to `global/pg_control.pbk.bak` before copying, kept "for restart failed incremental restore".
  - Data files are copied page-wise by `catchup_data_file`; other files use `backup_non_data_file`.
  - At the end it logs "Catchup incremental ratio (less is better)".
  - Code comments acknowledge a gap: "Future improvement to catch partial catchup… so user can't start partial catchup'ed instance".
- Open PR [#672](https://github.com/postgrespro/pg_probackup/pull/672) (2025-12-29) proposes a SUMMARIZE mode using PG17 `summarize_wal`, `pg_wal_summary_contents()`, and `pg_available_wal_summaries()` for both backup and catchup. It was not merged at the time of writing.

**Incremental restore**
- "The speed of restore from backup can be significantly improved by replacing only invalid and changed pages in already existing PostgreSQL data directory" — [pg_probackup docs](https://postgrespro.github.io/pg_probackup/).
- CHECKSUM mode: "read all data files in the data directory, validate header and checksum in every page and replace only invalid pages and those with checksum and LSN not matching with corresponding page in backup. This is the simplest, the most fool-proof incremental mode. Recommended to use by default" — [pg_probackup docs](https://postgrespro.github.io/pg_probackup/).
- LSN mode:
  - It reads pg_control "to obtain redo LSN and redo TLI, which allows you to determine a point in history (shiftpoint)… replace only invalid pages and those with LSN greater than shiftpoint".
  - It needs data checksums "(to avoid corruption due to hint bits)", and aborts if checksums are off.
  - It needs pg_control "synched with state of data directory", which "cannot [be] checked… it is a user responsibility". It is not recommended "after pg_resetxlog execution, after restore from backup without recovery been run, etc."
  - Source: [pg_probackup docs](https://postgrespro.github.io/pg_probackup/).
- (source) LSN-mode details, from [restore.c](https://github.com/postgrespro/pg_probackup/blob/master/src/restore.c):
  - It walks back the backup chain for a backup whose `stop_lsn < redo.lsn` on the same TLI, or on a TLI in the redo timeline's history file from the WAL archive. That backup's stop_lsn becomes the shift LSN.
  - Otherwise it errors: "destination directory redo point … is out of reach".
  - A `backup_label` in the destination fails the LSN-mode compatibility check, with a WARNING advising checksum mode.
- Always checked: the postmaster is not running and the system identifier matches the backup. `--force` lets incremental restore overwrite a directory with a different system ID. Incremental restore requires backups made by program version ≥ 2.4.0 — [pg_probackup docs](https://postgrespro.github.io/pg_probackup/).
- Open TODO issues: "restore and pg_control" (drop pg_control first during incremental restore to prevent starting mid-restore) — [#313](https://github.com/postgrespro/pg_probackup/issues/313); "detect PG major version mismatch for incremental restore" — [#361](https://github.com/postgrespro/pg_probackup/issues/361).
- Releases and status:
  - 2.5.16 (2025-12-09) added PostgreSQL 18 support; 2.5.15 was 2024-05-16 — [GitHub releases](https://github.com/postgrespro/pg_probackup/releases).
  - Postgres Pro documents a separate "pg_probackup3" (3.2) with DELTA and PTRACK modes, catchup, and S3/WebDAV/FTP targets. That page does not mention PAGE mode or WAL summarization, and does not state licensing — [pg_probackup3 key features](https://postgrespro.com/docs/pg_probackup3/3.2/pbk-key-features).

### Inferences
- The hint-bit rationale in LSN mode: with checksums or `wal_log_hints` off, hint-bit-only page writes on the stale destination do not advance the page LSN. Pages "older" than the shiftpoint could then carry hint bits set under the destination's divergent history. CHECKSUM mode avoids this by comparing per-page checksums stored in the backup's page header map, which is why it needs program version ≥ 2.4.0.
- pg_probackup catchup is the closest open-source equivalent to "refresh a stale physical copy directly from the live source". It is page-granular (8 KiB) and LSN-trusting: it assumes destination pages with LSN below the destination redo LSN equal the source's pages. That assumption breaks if the destination was modified independently on the same timeline. Content-hash verification avoids that trust.
- Because catchup DELTA reads every source page, I/O cost on the source is a full scan, but transfer is only changed pages. PTRACK avoids the scan but needs an extension that is not available on vanilla PostgreSQL without a patch.

### Gaps
- No independent benchmark of catchup or incremental restore; only a doc example (see Q7).
- Whether non-data files are re-sent unconditionally in catchup is unclear. The code passes `current.parent_backup` and destination file entries without CRCs to `backup_non_data_file`, which suggests most non-data files are always copied. I did not verify this.

## Q5. Other tools: operators, Patroni/Spilo, repmgr, pgcopydb, Percona

### Takeaway
None of the operators or HA managers implements its own change detection.
- **Crunchy PGO** (and, reportedly, Percona's operator) delegate to pgBackRest.
- **Zalando Spilo** delegates to WAL-G `backup-fetch` (full fetch) or pg_basebackup.
- **CloudNativePG** offers object-store backups with no incremental, or CSI volume snapshots whose incremental/differential behavior depends on the storage class.
- **Patroni**'s `keep_data` flag is the one generic hook that lets an in-place delta restore (for example `pgbackrest --delta restore`) rebuild a replica.
- **repmgr** relies on pg_basebackup, Barman, or pg_rewind (`node rejoin --force-rewind`).
- **pgcopydb** is logical and out of scope.

### Cited Findings
- CloudNativePG object stores "Do not support incremental or differential copies". Volume snapshots "Support incremental and differential copies, depending on the capabilities of the underlying storage class" and "Do not support retention policies" — [CNPG backup.md](https://github.com/cloudnative-pg/cloudnative-pg/blob/main/docs/src/backup.md). "Normally, storage classes that provide volume snapshotting support incremental and differential block level backup in a transparent way" — [CNPG backup_volumesnapshot.md](https://github.com/cloudnative-pg/cloudnative-pg/blob/main/docs/src/appendixes/backup_volumesnapshot.md). CNPG bootstraps new clusters by `pg_basebackup` cloning or by recovery from object stores or VolumeSnapshots — [CNPG bootstrap.md](https://github.com/cloudnative-pg/cloudnative-pg/blob/main/docs/src/bootstrap.md).
- Crunchy PGO: pgBackRest is "the backup management tool that powers the disaster recovery capabilities of PGO". In-place PITR passes pgBackRest restore options via `spec.backups.pgbackrest.restore.options` — [PGO disaster recovery docs](https://access.crunchydata.com/documentation/postgres-operator/latest/tutorials/backups-disaster-recovery/disaster-recovery). Whether PGO adds `--delta` automatically was not confirmed.
- Patroni:
  - `create_replica_methods` example: `command: /usr/bin/pgbackrest --stanza=<scope> --delta restore` with `keep_data: True`.
  - "A special keep_data parameter, if defined, will instruct Patroni to not clean PGDATA folder before calling restore."
  - The default `basebackup` method uses pg_basebackup.
  - Source: [Patroni replica_bootstrap.rst](https://github.com/patroni/patroni/blob/master/docs/replica_bootstrap.rst).
- Zalando Spilo: the replica creation methods are a WAL-G restore (`/scripts/walg_restore.sh`, with `threshold_megabytes` and `threshold_backup_size_percentage`, `no_leader: 1`) followed by `basebackup_fast_xlog`. There is no `keep_data` — [configure_spilo.py](https://github.com/zalando/spilo/blob/master/postgres-appliance/scripts/configure_spilo.py). Combined with WAL-G's empty-directory requirement, a Spilo replica rebuild re-downloads everything.
- repmgr:
  - `repmgr standby clone` clones "from another PostgreSQL node… or from Barman". `--fast-checkpoint` is "not effective when cloning from Barman" — [repmgr standby clone docs](https://www.repmgr.org/docs/current/repmgr-standby-clone.html).
  - `repmgr node rejoin --force-rewind` executes pg_rewind to re-integrate a diverged node — [repmgr node rejoin docs](https://www.repmgr.org/docs/current/repmgr-node-rejoin.html).
  - The repmgr FAQ says pg_rewind needs `wal_log_hints` or data checksums; otherwise "the data directory will need to be re-cloned from scratch" — [repmgr FAQ](https://www.repmgr.org/docs/5.5/faq-repmgr.html) (via search summary).
  - The repmgr 3→4 config converter still lists `rsync_options` — [repmgr upgrade docs](https://www.repmgr.org/docs/current/upgrading-from-repmgr-3.html).
- Percona: Percona is a current pgBackRest sponsor — [pgBackRest README](https://github.com/pgbackrest/pgbackrest/blob/main/README.md). pgBackRest release notes mention page-checksum failures being "most likely with pg_tde, since every encrypted file fails the page checksum" (v2.57.0 fix, 2025-10-18) — [Release notes](https://pgbackrest.org/release.html). One article says Percona would keep recommending pgBackRest after the April 2026 maintenance scare — search summary of [this comparison post](https://lilting.ch/en/articles/barman-pgbackrest-postgres-backup-migration) (unverified).
- WAL-G can also fetch pgBackRest backups: there is a `internal/databases/postgres/pgbackrest/backup_fetch_handler.go` in the source tree — [WAL-G repo](https://github.com/wal-g/wal-g/tree/master/internal/databases/postgres/pgbackrest).

### Inferences
- In operator ecosystems, "replica clone" means a full copy, either pg_basebackup or a restore from backup into an empty volume. The exceptions are CSI snapshot clones, where copy-on-write is delegated to storage, and Patroni with `keep_data` plus pgBackRest `--delta`.
- On TDE-encrypted clusters (pg_tde, AlloyDB-like encryption), page-checksum and page-LSN tools may misbehave because page headers are encrypted. Content hashing over raw bytes is unaffected.

### Gaps
- I could not fetch the Percona Operator for PostgreSQL backup docs (the page rendered no text). Its pgBackRest use is not directly sourced here.
- CNPG's mechanism for rejoining a former primary (pg_rewind) was not found in the docs fetched.
- repmgr's removal of `--rsync-only` from `standby clone` (present in repmgr 3.x) was not confirmed from release notes. Current docs show only pg_basebackup and Barman sources.

## Q6. Which tools can refresh an existing stale copy of the same cluster in place, and what they depend on

### Takeaway
Five mechanisms refresh an existing directory in place:
- pgBackRest `restore --delta`
- Barman `restore --delta-restore` (3.16+)
- pg_probackup `restore -I checksum|lsn`
- pg_probackup `catchup -b DELTA|PTRACK`
- WAL-G `catchup-fetch` / `catchup-receive`

Only pg_probackup catchup and WAL-G catchup pull directly from the live source without a repository. Only pgBackRest and pg_probackup in CHECKSUM mode verify reused data by content. The rest trust LSNs, mtimes, or pg_control.

### Cited Findings
Comparison matrix. All entries are sourced in Q1–Q5; "(src)" marks behavior observed in source code.

| Tool / command | Source of truth | Requires | Change detection on the destination | Granularity | Destination safeguards |
|---|---|---|---|---|---|
| pgBackRest `restore --delta` | Own repository (backup set) | Stanza/repo; `PG_VERSION` or `backup.manifest` in target; PG stopped | SHA-1 per file vs manifest; for block-incremental files, xxHash per block vs block map (src) | File, or 8–88 KiB block (×2/×4 by age) | `postmaster.pid` check; removes files not in manifest; recomputes SHA-1 after a block delta (src). No system-ID check (src) |
| pgBackRest `restore --force --delta` | Repository | Same as above | Size + mtime equal and mtime < backup copy start | File | Weaker; size/time trusted |
| Barman `restore --delta-restore` (3.16.0+; fixed in 3.17.0) | Barman backup | Local restore of a plain backup, or remote with `staging_location=local`; not for incremental, compressed, or encrypted backups | rsync quick check for files older than the safe horizon (`.barman-recover.info`), rsync `--checksum` for newer files (src) | rsync | `rsync --delete` of extra files (src) |
| pg_probackup `restore -I checksum` | Own catalog | Backups taken with ≥2.4.0; same system ID (or `--force`); PG stopped | Per-page checksum + LSN vs backup's page headers | 8 KiB page | System ID; postmaster check |
| pg_probackup `restore -I lsn` | Own catalog | Above, plus data checksums on, a trustworthy pg_control, no `backup_label`, redo TLI in the backup chain's timeline history (needs history files in the archive) | Page LSN > shift LSN (stop_lsn of a chain backup older than the destination redo) | 8 KiB page | Errors if the redo point is "out of reach"; warns on `backup_label` (src) |
| pg_probackup `catchup -b DELTA/PTRACK` | Live primary or standby (STREAM) | No catalog; same system ID; destination redo in source timeline history; PTRACK ≥ 2.0 for PTRACK mode; destination stopped, no `backup_label` | Source page LSN ≥ destination redo LSN (DELTA reads all pages; PTRACK uses the bitmap) | 8 KiB page | Renames destination pg_control to `.pbk.bak` during the run; deletes redundant files (src) |
| WAL-G `catchup-push`/`catchup-fetch` | Live primary → storage → replica | Replica stopped; replica LSN provided manually | Page LSN ≥ `--from-lsn` | 8 KiB page | Doubles transfer through storage (#1629) |
| WAL-G `catchup-send`/`catchup-receive` | Live primary → TCP → replica | Same system ID; same current timeline; source start LSN > destination checkpoint; replica stopped | Skips files with equal mtime; page LSN ≥ destination pg_control checkpoint (src) | 8 KiB page or whole file | Deletes missing files; sends `backup_label` and pg_control at the end (src). Open bugs with tablespaces and new directories |
| WAL-G `backup-fetch` | Storage | Empty directory | N/A | N/A | No in-place reuse |
| Barman PG17 block-level incremental restore | Barman chain + `pg_combinebackup` | `summarize_wal` on before the first full; PG17+ | N/A on the destination (combine, then copy) | N/A | Delta-restore unsupported |
| Patroni `keep_data` + pgBackRest `--delta` | pgBackRest repo | Patroni config | As pgBackRest | As pgBackRest | As pgBackRest |

- Catchup for pg_probackup "is different from other pg_probackup operations: The backup catalog is not required. STREAM WAL delivery mode is only supported" — [pg_probackup docs](https://postgrespro.github.io/pg_probackup/).
- WAL-G catchup requires the same timeline, since a mismatch is fatal — [catchup_send_recieve_handler.go](https://github.com/wal-g/wal-g/blob/master/internal/databases/postgres/catchup_send_recieve_handler.go). pg_probackup allows the destination on an ancestor timeline that is in the source's history — [catchup.c](https://github.com/postgrespro/pg_probackup/blob/master/src/catchup.c).
- pgBackRest automatically falls back to checksum delta backups after a timeline switch ("normal after restoring from backup or promoting a standby") — [manifest.c](https://github.com/pgbackrest/pgbackrest/blob/main/src/info/manifest/manifest.c).

### Inferences
Positioning notes for a standalone content-hash chunk-resync tool:
- **Unique combination.** No surveyed open-source tool combines a live source with no repository, content-verified reuse (hash), and sub-file granularity. pg_probackup catchup and WAL-G catchup are live-source and page-granular but LSN- and mtime-trusting, need the same system ID, and need the timeline in history or identical. pgBackRest delta restore is content-verified and block-granular, but only from its own repository.
- **Advantages of content hashing over LSN trust:**
  - It works when page LSNs are unreliable: after `pg_resetwal`, hint-bit-only changes without checksums, or a destination modified on a divergent timeline.
  - It does not depend on pg_control being in sync, or on WAL, archive, or timeline-history availability.
  - It works on encrypted (TDE) pages.
  - It applies equally to non-relation files.
- **Costs to acknowledge:**
  - Both sides must read everything, like DELTA mode. PTRACK, WAL summaries, or WAL-delta maps avoid the source-side full scan.
  - For an online source, it still needs a backup-start/stop protocol and WAL to make the copy consistent, just as catchup does.
- **Safety baseline.** A credible design should match the existing safeguards:
  - Refuse if a postmaster is running on the destination.
  - Check system identifier and timeline lineage, or explicitly opt out.
  - Neutralize destination pg_control or `backup_label` during the run (pg_probackup's `.pbk.bak` trick).
  - Delete files that are absent on the source.
  - Write `backup_label` and pg_control last (WAL-G, pg_probackup).

### Gaps
- I found no tool that documents re-validating hashes on the destination after writing, beyond pgBackRest's post-block-delta SHA-1 check.

## Q7. Published benchmarks and measured numbers

### Takeaway
There are no independent, controlled head-to-head benchmarks of these tools' incremental or delta mechanisms. The best available figures are:
- A small vendor demo of pgBackRest block incremental: about 1 GB database, more than 50× smaller incremental, delta restore copying 3.5 MB instead of 30.4 MB.
- A pg_probackup doc example: LSN incremental restore moved 1693 MB of 15 GB (11%) in 43 s.
- A WAL-G 2020 reverse-delta-unpack test: 16 min 15 s down to about 6 min 24 s.
- A Postgres.ai full-copy benchmark: pgBackRest parallel restore 9.4× faster than pg_basebackup on 1 TiB, at 101 s vs 950 s.

### Cited Findings
- pgBackRest block incremental demo (David Steele / Crunchy Data, 2023-06-09; small database on local storage):
  - Test database size was 995.7 MB.
  - A full backup was 991 repository files without bundling and 7 with bundling.
  - The incremental backup took 12,525 ms vs 3,589 ms and was 52.8 MB vs 943.3 KB ("more than 50x improvement in backup size").
  - Delta restore took 3,697 ms vs 1,536 ms. "With block incremental enabled, delta restore only had to copy 3.5MB of the main table file", against 30.4 MB compressed without.
  - Source: [Crunchy Data blog](https://crunchydata.com/blog/pgbackrest-file-bundling-and-block-incremental-backup) (vendor demo, not a controlled benchmark).
- Postgres.ai "Ultra-fast replica creation with pgBackRest" (Samokhvalov and Boguk, 2025-11-05):
  - Setup: a 1.023 TiB pgbench dataset on AWS i4i.32xlarge (128 vCPU, 8× NVMe RAID0, 75 Gbps), PostgreSQL 18.0.
  - pg_basebackup took 950 s (1.08 GiB/s).
  - pgBackRest with no compression: process-max 4 took 575 s, 8 took 298 s, 16 took 164 s, 32 took 101 s (9.4×, network saturated at about 8.5 GiB/s on the wire), 64 took 107 s, and 128 took 133 s.
  - Network compression gained only about 5–6%.
  - Restore into an empty directory was used; delta restore was not tested.
  - Source: [Postgres.ai blog](https://postgres.ai/blog/20251105-postgres-marathon-2-012-ultra-fast-replica-creation-pgbackrest).
- pg_probackup LSN-mode incremental restore doc example: "PGDATA size: 15GB … Transfered bytes: 1693MB, time elapsed: 43s … Restore incremental ratio (less is better): 11% (1693MB/15GB)" — [pg_probackup docs commit 496605b](https://github.com/postgrespro/pg_probackup/commit/496605bdb50012788fbab74a0f1625df1a26a789). The current docs example shows a 100% ratio because the destination was empty — [pg_probackup docs](https://postgrespro.github.io/pg_probackup/).
- The pg_probackup 2.4.1 release says incremental restore "greatly increases the speed, reduces the network load and I/O consumption" (vendor claim, no numbers) — [PostgreSQL.org news](https://www.postgresql.org/about/news/pg_probackup-241-has-been-released-2048/) (via search summary).
- WAL-G reverse delta unpack benchmark (2020-03-26):
  - Setup: m4.4xlarge, PostgreSQL 10.12, S3, a 16 GB cluster with a base backup plus 5 delta backups from pgbench.
  - `backup-fetch` with standard unpack took about 16 min 15 s; with reverse delta unpack about 6 min 24 s.
  - Source: [WAL-G benchmarks](https://github.com/wal-g/wal-g/blob/master/benchmarks/reverse-delta-unpack/reverse-delta-unpack-26-03-2020.md).
- Anecdote: one write-up on a ~4.2 GB PG16 database reported a Barman full backup of 12 min 34 s vs about 14 min for pgBackRest, and a restore of 23 min 17 s for Barman vs 18 min for pgBackRest. This was a single run — search summary of [lilting.ch article](https://lilting.ch/en/articles/barman-pgbackrest-postgres-backup-migration) (unverified, low weight).

### Inferences
- The available numbers are not comparable across tools: different data sizes, hardware, storage, and workloads. A fair comparison would need a common harness that measures bytes transferred and wall time for (a) refreshing a stale copy at several change rates and (b) full clones.
- The Postgres.ai numbers show that at 75 Gbps, raw parallel copy of 1 TiB takes under 2 minutes. A delta tool's advantage on fast LANs comes mainly from less I/O and less WAN or cross-region transfer, not from network time.

### Gaps
- I found no published numbers for pg_probackup catchup, WAL-G catchup-send/receive, Barman delta-restore, or Barman PG17 incremental.
- I found no independent benchmark of pgBackRest block incremental at TB scale; only a mailing-list user with a 52 TB data directory using block and bundle, without reported timings, per search summary ([pgsql list](https://postgresql.org/message-id/CAEDsCzj6PFh%3D1JXHwBSZkjrsWzo4qjUWnHwE-V_SeU0pPQYQSQ%40mail.gmail.com)).
