# PostgreSQL core mechanisms for creating or refreshing a physical copy when an older copy may exist (as of October 2026)

Version status used throughout. On 2026-10-22 the "current" docs on postgresql.org are PostgreSQL 18 (18.6). PostgreSQL 19 is at release candidate: the site banner announces "PostgreSQL 19 RC 1 Released!" dated 2026-10-15, and the v19 release notes still carry a placeholder date ("2026-??-??, AS OF 2026-10-09") — [PG19 release notes (devel)](https://www.postgresql.org/docs/devel/release-19.html); [PG18 docs footer](https://www.postgresql.org/docs/18/app-pgbasebackup.html). PG17.0 was released 2024-09-26 — [PG17 release notes](https://www.postgresql.org/docs/17/release-17.html). PG18.0 was released 2025-09-25 — [PG18 release notes](https://www.postgresql.org/docs/18/release-18.html). Most source-code citations below point at the `master` branch on GitHub, fetched in October 2026.

## 1. pg_basebackup full backups: what it transfers, and whether it can skip data already at the destination

### Takeaway
A full pg_basebackup always streams every included file in full: one tar stream per tablespace from the `BASE_BACKUP` replication command, plus WAL and a JSON manifest with whole-file checksums. It has no way to reuse data at the destination. The `-D` target (and every `-T` and `--waldir` target) must be empty or must not exist yet. Core developers have been rejecting "rsync-like" differential copy in pg_basebackup since 2011, saying to use rsync instead.

### Cited Findings
**Target directory rules**
- `-D`: pg_basebackup creates the directory and any missing parents. If the directory already exists, it must be empty. `-T olddir=newdir` targets and `--waldir` must also be empty if they exist. Tablespace relocation (`-T`) only works with plain format. — [pg_basebackup (PG18)](https://www.postgresql.org/docs/18/app-pgbasebackup.html)
- On error, pg_basebackup removes the directories it created unless `-n/--no-clean` is given. Tablespace directories are never cleaned up. — [pg_basebackup (PG18)](https://www.postgresql.org/docs/18/app-pgbasebackup.html)

**The BASE_BACKUP protocol**
- `BASE_BACKUP` puts the server into backup mode and streams the backup. It accepts these options: LABEL, TARGET (client/server/blackhole), TARGET_DETAIL, PROGRESS, CHECKPOINT (fast/spread, default spread), WAL, WAIT, COMPRESSION (gzip/lz4/zstd), COMPRESSION_DETAIL, MAX_RATE (32 kB/s to 1 GB/s), TABLESPACE_MAP, VERIFY_CHECKSUMS (default true), MANIFEST (protocol default `'no'`), MANIFEST_CHECKSUMS (default CRC32C) and INCREMENTAL. — [Streaming replication protocol (PG18)](https://www.postgresql.org/docs/18/protocol-replication.html)
- The server first sends the start LSN and TLI, then one row per tablespace (oid, location, approximate size). Next comes a CopyOut stream of messages: `'n'` for a new archive (ustar tar, one for the main data directory and one per tablespace), `'m'` for manifest start, `'d'` for data, and `'p'` for progress. A final result set carries the end LSN. — [Streaming replication protocol (PG18)](https://www.postgresql.org/docs/18/protocol-replication.html)

**Files excluded from the stream**
- Excluded: postmaster.pid and postmaster.opts; every pg_internal.init; pgsql_tmp* and temporary relations; unlogged relations except their init fork; and pg_wal (a synthesized pg_wal holding only the needed segments is added if WAL is included).
- pg_dynshmem, pg_notify, pg_replslot, pg_serial, pg_snapshots, pg_stat_tmp and pg_subtrans are sent as empty directories. Symlinks are skipped except those in pg_tblspc. Everything else, including third-party files in PGDATA, is sent. — [Streaming replication protocol (PG18)](https://www.postgresql.org/docs/18/protocol-replication.html); [pg_basebackup Notes (PG18)](https://www.postgresql.org/docs/18/app-pgbasebackup.html)
- The backup starts with a checkpoint on the source. With the default `spread` checkpoint, this can leave pg_basebackup looking idle for some time. — [pg_basebackup (PG18)](https://www.postgresql.org/docs/18/app-pgbasebackup.html)

**WAL methods**
- `-X stream` is the default. It opens a second replication connection, so it needs two connections. If the client keeps up, the source does not have to retain extra WAL.
- On servers v10 and later, a temporary replication slot is used automatically.
- `-X fetch` collects WAL at the end of the backup. It needs `wal_keep_size` high enough, or the backup "will fail and be unusable."
- Unless `-X none` is used, the result is a standalone backup that can start without the archive. — [pg_basebackup (PG18)](https://www.postgresql.org/docs/18/app-pgbasebackup.html)

**Backup manifest**
- The manifest records size, last-modified time and an optional checksum for every file except included WAL. Algorithms are NONE, CRC32C (default), SHA224, SHA256, SHA384 and SHA512. The manifest always carries a SHA256 checksum of itself. — [pg_basebackup (PG18)](https://www.postgresql.org/docs/18/app-pgbasebackup.html); [protocol (PG18)](https://www.postgresql.org/docs/18/protocol-replication.html)
- The docs say CRC32C catches accidental changes but not malicious ones, and that the SHA variants are significantly more CPU-intensive. — [pg_basebackup (PG18)](https://www.postgresql.org/docs/18/app-pgbasebackup.html)
- Per-file manifest fields are: `Path` or `Encoded-Path` (hex, used for non-UTF8 names), `Size`, `Last-Modified` ("informational", not used by pg_verifybackup), and, when enabled, `Checksum-Algorithm` and `Checksum`. The checksum is described as "computed for this file", meaning one value per file. The docs describe no per-block or per-chunk checksums. — [Backup manifest file object (PG18)](https://www.postgresql.org/docs/18/backup-manifest-files.html)
- PG17 added the system identifier to backup manifests (Amul Sul) "to detect invalid WAL usage." — [PG17 release notes](https://www.postgresql.org/docs/17/release-17.html)

**pg_verifybackup**
- It checks four things: the manifest's own checksum and its system identifier against pg_control; missing or extra files and their sizes; per-file checksums; and that the required WAL can be read and parsed via pg_waldump.
- It ignores postgresql.auto.conf, standby.signal, recovery.signal and pg_wal contents. Its docs say it "does not and cannot include every check" that a running server performs. — [pg_verifybackup (PG18)](https://www.postgresql.org/docs/18/app-pgverifybackup.html)
- PG18 can verify tar-format backups (Amul Sul), but WAL parsing works only for plain format. — [PG18 release notes](https://www.postgresql.org/docs/18/release-18.html); [pg_verifybackup (PG18)](https://www.postgresql.org/docs/18/app-pgverifybackup.html)
- PG19 (RC) can read WAL stored in tar archives and adds `--wal-path`. — [PG19 release notes](https://www.postgresql.org/docs/devel/release-19.html)

**Page-checksum verification during backup**
- By default pg_basebackup verifies data page checksums when they are enabled on the source. Failures give a non-zero exit (the backup is kept) and are counted in pg_stat_database. — [pg_basebackup (PG18)](https://www.postgresql.org/docs/18/app-pgbasebackup.html)
- PG18 changed initdb to enable data checksums by default (Greg Sabino Mullane) and added `--no-data-checksums`. pg_upgrade requires matching checksum settings. — [PG18 release notes](https://www.postgresql.org/docs/18/release-18.html)

**Version limits**
- pg_basebackup works with servers of the same or older major version, down to 9.1. `-X stream` needs 9.3 or later. `--incremental` needs 17 or later. — [pg_basebackup (PG18)](https://www.postgresql.org/docs/18/app-pgbasebackup.html)
- PG19 (RC) adds a `backup_type` column (`full`/`incremental`) to `pg_stat_progress_basebackup`. No other pg_basebackup transfer changes appear in the v19 notes. — [PG19 release notes](https://www.postgresql.org/docs/devel/release-19.html)

**History of "delta" requests on the mailing lists**
- 2011, thread "Feature request: pg_basebackup --force": Joshua Berkus asked for a `--force` option to clear target directories. Robert Haas suggested "differential copy, a la rsync" would be more useful. Tom Lane replied that this was reinventing the wheel and asked why not just use rsync. — [Tom Lane, 2011-04-10](https://www.postgresql.org/message-id/9784.1302453336%40sss.pgh.pa.us)
- In the same thread, Kevin Grittner argued for rsync-like behavior so a standby over a slow WAN would not need the whole directory resent. I saw this only in a search snippet and did not fetch it. — [Kevin Grittner msg](https://www.postgresql.org/message-id/4DA2D205020000250003C62C%40gw.wicourts.gov)
- 2020: Stephen Frost listed "delta restore" among the features pg_basebackup lacks. He warned that rsync can't safely be used this way unless done with checksums, "and even then it's complicated." He pointed to pgBackRest's checksum-based delta restore over an existing cluster. — [Stephen Frost, 2020-05-21](https://www.postgresql.org/message-id/20200521215306.GF3418%40tamriel.snowman.net)

### Inferences
- Core pg_basebackup cannot refresh an existing copy at all. The project's "reuse an older copy and send only the delta" behavior lies entirely outside what full pg_basebackup offers.
- The manifest's per-file checksum (CRC32C by default) is the only content fingerprint core produces. It is whole-file, so it can tell a file changed but not which chunk. Per-chunk hashing would be new.
- Because `Last-Modified` is advisory, a tool comparing an old copy to the source cannot rely on mtimes. This matches the rsync `--checksum` warnings in section 5.
- The exclusion list (unlogged relations except init forks, pg_replslot, temp files, and so on) is the de facto contract for "what belongs in a physical copy." A compatible tool should mirror it.

### Gaps
- I did not fetch the backup-manifest top-level and WAL-Ranges subpages. So I have not verbatim-confirmed the manifest version numbering (v1 vs v2), the `WAL-Ranges` field names (`Timeline`, `Start-LSN`, `End-LSN`) or `System-Identifier`. Source code does show "backup manifest version 1 does not support incremental backup" (section 2).
- I found no 2024–2026 pgsql-hackers proposal for an rsync-style or chunk-hash base backup, or for block-level checksums in manifests. My searches returned only the 2011 and 2020 threads. This is absence of evidence, not proof.

## 2. PostgreSQL 17+ incremental backup: WAL summarization, pg_basebackup --incremental, pg_combinebackup, prerequisites, failure modes, PG18/PG19 changes

### Takeaway
PG17 added block-level incremental backup driven by WAL summaries, and the PG18 docs describe it the same way. A WAL summarizer process records which blocks of which relation forks changed between checkpoint-aligned LSN boundaries. pg_basebackup `--incremental=<old backup_manifest>` uploads the prior manifest. The server then sends `INCREMENTAL.*` files holding only changed 8 kB blocks for relation files and sends everything else whole. Restoring needs pg_combinebackup and every backup in the chain. This only works if the prior backup has a manifest, `summarize_wal=on` (default off), and summaries covering the whole LSN gap that have not been deleted (default retention 10 days). It cannot use an arbitrary older data directory as the reference.

### Cited Findings
**WAL summarization (server side)**
- `summarize_wal` (boolean, default off) enables the WAL summarizer on a primary or a standby. It is set in postgresql.conf or on the command line. The server won't start with it on if `wal_level=minimal`, and it skips WAL generated at minimal. — [runtime-config-wal (PG18)](https://www.postgresql.org/docs/18/runtime-config-wal.html)
- `wal_summary_keep_time` (default 10 days; minutes if no unit) deletes summaries by file timestamp. Setting it to 0 disables automatic removal. If `summarize_wal=off`, existing summaries are never removed. The docs advise keeping it comfortably longer than the gap between a backup and an incremental that depends on it. — [runtime-config-wal (PG18)](https://www.postgresql.org/docs/18/runtime-config-wal.html)
- Summaries live in `pg_wal/summaries`. They must cover every LSN from the prior backup's start LSN to the new backup's start LSN, or the incremental fails. The server waits for missing summaries and fails if they were removed or the summarizer can't catch up. — [Continuous archiving §25.3.3 (PG18)](https://www.postgresql.org/docs/18/continuous-archiving.html)
- PG17 introspection tools: `pg_available_wal_summaries()`, `pg_wal_summary_contents()`, `pg_get_wal_summarizer_state()` and the `pg_walsummary` app (Robert Haas, Nathan Bossart, Hubert Depesz Lubaczewski). — [PG17 release notes](https://www.postgresql.org/docs/17/release-17.html)
- PG18 added `pg_ls_summariesdir()` (Yushi Ogiwara). — [PG18 release notes](https://www.postgresql.org/docs/18/release-18.html)

**What a summary file contains**
- Summaries record which blocks in which relation forks were modified. A file usually ends when the summarizer sees a checkpoint, and also on a timeline switch. Redo points and shutdown checkpoints start new files, because redo can only begin at XLOG_CHECKPOINT_REDO or XLOG_CHECKPOINT_SHUTDOWN. — [walsummarizer.c (master)](https://github.com/postgres/postgres/blob/master/src/backend/postmaster/walsummarizer.c)
- File name: `pg_wal/summaries/` + TLI + start LSN + end LSN in hex + `.summary`, written to a temp file and then durably renamed. — [walsummarizer.c (master)](https://github.com/postgres/postgres/blob/master/src/backend/postmaster/walsummarizer.c)
- How changes are tracked:
  - Creating a fork resets tracking and truncating a fork drops tracking beyond the truncation point.
  - Database create/drop is recorded with relfilenode 0 for that database/tablespace OID pair, meaning all relations in it were recreated.
  - Relations removed at commit stop being tracked.
  - The FSM fork is ignored because it is "not fully WAL-logged".
  - On a standby, it summarizes up to whichever is further ahead of the receiver flush or replay position, never past flushed WAL. — [walsummarizer.c (master)](https://github.com/postgres/postgres/blob/master/src/backend/postmaster/walsummarizer.c)
- Each summary is a serialized block reference table keyed per relation fork (RelFileLocator plus fork).
  - Each fork has a "limit block": the smallest relation length known to have occurred. It is set to 0 when the fork is created or destroyed, and modified blocks beyond it are forgotten. This is how truncation is tracked.
  - Block numbers are split into chunks of 2^16 blocks. Each chunk is stored as an array of 2-byte offsets or, once it reaches MAX_ENTRIES_PER_CHUNK, as a bitmap.
  - The file has a magic number and ends with a sentinel and CRC. — [blkreftable.c (master)](https://github.com/postgres/postgres/blob/master/src/common/blkreftable.c)

**Waiting and timeouts**
- WaitForWalSummarization warns "still waiting for WAL summarization through %X/%08X after %ld second(s)". It errors with "WAL summarization is not progressing" if no WAL record is absorbed for a full minute. — [walsummarizer.c (master)](https://github.com/postgres/postgres/blob/master/src/backend/postmaster/walsummarizer.c)

**How the server builds an incremental backup**
- The client runs `UPLOAD_MANIFEST` then `BASE_BACKUP ... INCREMENTAL`. — [protocol (PG18)](https://www.postgresql.org/docs/18/protocol-replication.html)
- Non-relation files are always sent whole. Some relation files become smaller incremental files holding changed blocks plus enough metadata to rebuild the file. — [Continuous archiving §25.3.3 (PG18)](https://www.postgresql.org/docs/18/continuous-archiving.html)
- Fields of the prior manifest that are used:
  - WAL-Ranges: TLI, start LSN and end LSN.
  - System identifier, compared against the server's.
  - File paths and sizes, kept "for sanity-checking purposes".
  - Checksums and last-modified times are not stored or used.
  - A version-1 manifest is rejected: "backup manifest version 1 does not support incremental backup". — [basebackup_incremental.c (master)](https://github.com/postgres/postgres/blob/master/src/backend/backup/basebackup_incremental.c)
- A relation file is sent whole (BACK_UP_FILE_FULLY) when:
  - its size is not a multiple of BLCKSZ or is too large;
  - it is an FSM fork;
  - it was not in the prior manifest;
  - its database/tablespace pair was created since the prior backup;
  - it is zero length;
  - the limit block is at or before the segment start;
  - more than 90% of its blocks changed (`nblocks * BLCKSZ > size * 0.9`).
  
  A file with no summary entry is still sent as an incremental file with zero blocks. — [basebackup_incremental.c (master)](https://github.com/postgres/postgres/blob/master/src/backend/backup/basebackup_incremental.c)
- Incremental files are named `INCREMENTAL.<relfile>[.<segno>]`. The header is three 4-byte values (magic, truncation block length, block count) followed by sorted segment-relative block numbers. When there are blocks, the header is padded to a multiple of BLCKSZ and the block images follow. The truncation block length is the minimum length of the rebuilt file, capped at RELSEG_SIZE and by the limit block. — [basebackup_incremental.c (master)](https://github.com/postgres/postgres/blob/master/src/backend/backup/basebackup_incremental.c)
- The BLCKSZ padding came from a 2024 finding by Tomas Vondra: misaligned block data in incremental files defeated copy-on-write sharing in pg_combinebackup. Aligning it cut combined-output disk use on XFS from 689M/3172M/13797M to 19M/22M/27M for the 1%/10%/20% cases. — [Tomas Vondra, 2024-04-01](https://www.postgresql.org/message-id/5ac425a4-6201-4f24-912e-8eed6905790a%40enterprisedb.com)

**Failure messages, timelines and standbys**
- Server-side failure messages include:
  - "WAL summaries are required on timeline %u from %X/%08X to %X/%08X, but no summaries for that timeline and LSN range exist"
  - the same with "...are incomplete", plus the detail "The first unsummarized LSN in this range is %X/%08X."
  - "system identifier in backup manifest is …, but database system identifier is …"
  
  — [basebackup_incremental.c (master)](https://github.com/postgres/postgres/blob/master/src/backend/backup/basebackup_incremental.c)
- Timeline handling uses `readTimeLineHistory` for the new backup's start TLI and validates the manifest's WAL ranges against it. Errors include:
  - "timeline %u found in manifest, but not in this server's history"
  - "manifest requires WAL from initial/continuation timeline … but that timeline begins at …"
  - "manifest requires WAL from final timeline %u ending at …, but this backup starts at …", with the hint that this can happen on a standby with little activity
  - "...non-final timeline … but this server switched timelines at …"
  
  — [basebackup_incremental.c (master)](https://github.com/postgres/postgres/blob/master/src/backend/backup/basebackup_incremental.c)
- On a standby, replay starts at the latest restartpoint. An incremental backup of a standby can therefore fail if there was little activity since the prior backup. — [Continuous archiving §25.3.3 (PG18)](https://www.postgresql.org/docs/18/continuous-archiving.html)
- PostgreSQL does not track which backups later incrementals depend on. Users must keep chain members themselves. — [Continuous archiving §25.3.3 (PG18)](https://www.postgresql.org/docs/18/continuous-archiving.html)

**pg_combinebackup**
- Backups are listed oldest (full) to newest. The output is a synthetic full backup in `-o` and can itself be used as the base for a later combine.
- It checks that the chain is legal but not that each backup is intact; the docs say to use pg_verifybackup for that. — [pg_combinebackup (PG18)](https://www.postgresql.org/docs/18/app-pgcombinebackup.html)
- Copy modes:
  - `--copy` (default).
  - `--clone` (reflinks: Linux 4.5+ on Btrfs/XFS with reflink, macOS APFS; errors if unsupported).
  - `--copy-file-range` (Linux and FreeBSD).
  - `-k/--link` (hard links; same filesystem; modifying the output, for example by starting the server, can modify the inputs).
  
  If the manifest has no checksum of the right type, files are still cloned or linked but also read to compute checksums. Other options: `--dry-run`, `--no-sync`, `--sync-method`, `--manifest-checksums`, `--no-manifest`, `-T`. — [pg_combinebackup (PG18)](https://www.postgresql.org/docs/18/app-pgcombinebackup.html)
- Version history of the copy modes:
  - `-k/--link` is new in PG18 (Israel Barth Rubio, Robert Haas). The release note says only some files can be hard linked and the option should not be used if the backups will be used independently. — [PG18 release notes](https://www.postgresql.org/docs/18/release-18.html)
  - `--copy-file-range` for pg_upgrade appeared in PG17 (Thomas Munro). — [PG17 release notes](https://www.postgresql.org/docs/17/release-17.html)
  - The commitfest entry "pg_upgrade, pg_combinebackup --copy-file-range" covered both tools. — [Commitfest 4338](https://commitfest.postgresql.org/48/4338)
- Chain checks in source:
  - pg_control CRC and version checks.
  - All backups must share one system identifier, and each manifest's identifier must match.
  - Only the first backup may be full, and the first must be full.
  - Each backup's backup_label must start on the expected TLI and at the expected LSN. The code comments that an exact LSN match is stricter than necessary.
  - It is an error if the final backup has checksums enabled but an earlier one did not: "only some backups have checksums enabled".
  
  The final backup's backup_label is written to the output. — [pg_combinebackup.c (master)](https://github.com/postgres/postgres/blob/master/src/bin/pg_combinebackup/pg_combinebackup.c)
- Reconstruction works per file from newest to oldest:
  - Blocks present in the newest INCREMENTAL file win. Older sources fill blocks that are still missing and below truncation_block_length, stopping at the first full file.
  - Blocks with no source are zero-filled.
  - If an older full file already has exactly the right length and no newer blocks are needed, it is copied whole with the selected method.
  - Manifest checksums are reused when the algorithm matches.
  - A full backup that contains an INCREMENTAL file raises "full backup contains unexpected incremental file". — [reconstruct.c (master)](https://github.com/postgres/postgres/blob/master/src/bin/pg_combinebackup/reconstruct.c)
- Documented limitation: pg_combinebackup does not recompute page checksums. If earlier backups had checksums off and the final one has them on, the output may have invalid page checksums. The docs recommend a new full backup after any pg_checksums change. — [pg_combinebackup (PG18)](https://www.postgresql.org/docs/18/app-pgcombinebackup.html)
- **Conflict to flag:** the PG18 docs imply pg_combinebackup proceeds and produces a directory with bad checksums. My fetch of `master` pg_combinebackup.c reported a hard error ("only some backups have checksums enabled") for the same case. Either master changed after PG18 or the automated summary misread the code. Check `check_control_files()` in the REL_17/REL_18 branches before relying on either. — [pg_combinebackup (PG18)](https://www.postgresql.org/docs/18/app-pgcombinebackup.html); [pg_combinebackup.c (master)](https://github.com/postgres/postgres/blob/master/src/bin/pg_combinebackup/pg_combinebackup.c)

**PG17 and PG18 release-note entries**
- PG17: "Add support for incremental file system backup" (Robert Haas, Jakub Wartak, Tomas Vondra); WAL summarization files; pg_walsummary; system identifier in manifests. — [PG17 release notes](https://www.postgresql.org/docs/17/release-17.html)
- PG18: pg_combinebackup `--link`, pg_verifybackup tar support, pg_ls_summariesdir(), and initdb checksums on by default. The PG18 notes do not mention pg_basebackup or any change to the incremental algorithm. — [PG18 release notes](https://www.postgresql.org/docs/18/release-18.html)

**PG19 (RC, not GA as of 2026-10-22)**
- The only backup items are the `backup_type` column in pg_stat_progress_basebackup and pg_verifybackup reading WAL from tar (`--wal-path`). There are no items about pg_combinebackup, WAL summaries, pg_rewind or manifest format.
- PG19 also uses AVX2 for page checksum computation (Matthew Sterrett, Andrew Kim). — [PG19 release notes](https://www.postgresql.org/docs/devel/release-19.html)

**Design notes from development**
- In an Oct 2023 hackers message, Robert Haas wrote that the summary format was chosen to be space-efficient and easy to move between disk and memory. He argued summaries should ideally be removed once no more incrementals will use them, rather than by age, and that backup software may want to manage them itself.
- He also said the 1 GB JSON limit caps manifests at about 7.16M files (about 150 bytes per entry).
- That message describes a pre-release design (`wal_summarize_mb`, 7-day default retention) that differs from the shipped GUCs (`summarize_wal` boolean, 10-day default). — [Robert Haas, 2023-10-24](https://www.postgresql.org/message-id/CA%2BTgmoYdPS7a4eiqAFCZ8dr4r3-O0zq1LvTO5drwWr%2B7wHQaSQ%40mail.gmail.com)

### Inferences
- Core incremental backup differs from the project's design in a basic way. It is source-side change tracking (WAL-derived block lists) against a *known reference backup identified by its manifest's LSN range*. It is not destination-side content comparison.
- It cannot help when the destination copy:
  - has no backup_manifest (for example, a standby built by an older tool, or a copy made with pg_backup_start and rsync);
  - is older than the summary retention window or predates `summarize_wal=on`;
  - has been modified since (for example, a standby that replayed WAL, or a server that was started);
  - is on a different timeline in a way the history file doesn't explain.
  
  In all these cases a content-hash delta tool is the only built-in-free option short of a full copy.
- Unchanged relation segments still yield an INCREMENTAL file (header only). The server also reads the changed blocks of every relation file it sends. Backup cost is therefore O(changed blocks + number of files), not O(database size). A chunk-hash tool must instead read and hash the whole source and destination, which is O(database size) I/O on both sides. That is the key tradeoff to quantify against PG17+ incremental.
- The incremental output is a separate directory that pg_combinebackup must merge into yet another output directory. Refreshing in place is not supported. With `--link` or `--clone` the merge can avoid most data copying, but the old copy still has to be a chain member with its own manifest.
- The 90% rule means heavily updated segments are resent whole. Hash-based chunking would behave similarly for such files.

### Gaps
- I did not confirm from a primary source whether Tomas Vondra's batched copy_file_range and prefetch patches for pg_combinebackup were ever committed. My fetch of master `reconstruct.c` reported one block per copy_file_range call and no prefetch, but that came from an automated summary and should be checked by reading the file directly.
- I did not find whether the INCREMENTAL header alignment shipped in 17.0 or a later minor release. The thread is dated before 17.0, so 17.0 is likely but unconfirmed.
- I did not verify whether PG17 adopted incremental JSON parsing for manifests, which would lift the about 7.16M-file limit Haas described. I believe it did, from memory, but have no source here.
- I could not extract text from Robert Haas's PGConf.EU 2024 slides; the PDF text was compressed and no pdftotext was available. Search snippets say the slides describe 8 kB block granularity, non-relation files always copied in full, and the goal of smaller and faster backups at possible cost to recovery time. — [Slides PDF](https://postgresql.eu/events/pgconfeu2024/sessions/session/5718/slides/612/Incremental%20Backup%20-%20PGCONF.EU%202024.pdf) (contents unverified)
- I could not locate Robert Haas's rhaas.blogspot.com posts on incremental backup through search. A dbi-services post reportedly links a January 2024 post on use cases, which I did not verify.

## 3. Published benchmarks and measurements of incremental backup size/time and pg_combinebackup cost

### Takeaway
Published numbers are sparse and come mostly from development-era tests (2023–2024) on databases of 0.35–75 GB. They show incrementals at roughly 1/10 of full size under continuous pgbench load and pg_combinebackup taking about 150–200 s for about 75 GB on ext4/XFS/Btrfs. That rose to 900–1000 s on ZFS without prefetch. I found no TB-scale or production benchmarks, and no published timing for the incremental backup step itself.

### Cited Findings
- **Jakub Wartak (EDB) test, reported by Lukas Fittl, 2023-12-21**:
  - Workload: pgbench for 24 h; the database grew from 3.3 GB to 4.3 GB and produced 77 GB of WAL.
  - Backups: one full backup of 3.4 GB, then 11 incrementals every 2 h totaling about 3.5 GB, roughly 1/10 of a full backup each.
  - Restore: about 4 minutes using the incrementals versus about 78 minutes of WAL replay. The article does not label the two numbers explicitly; this mapping is implied by context.
  - Fittl's own test: a 350 MB full backup followed by a 48 MB first incremental.
  - No pg_combinebackup durations or hardware specs are given. — [pganalyze "5mins of Postgres" ep. 96](https://pganalyze.com/blog/5mins-postgres-17-incremental-backups)
- **Tomas Vondra (EDB), 2024-04-01, development benchmark**:
  - Setup: pgbench scale 5000 (about 75 GB). A full backup was taken, then about 1%, 10% and 20% of blocks were updated, with an incremental taken after each step. Chains of 2, 3 and 4 backups were combined.
  - Timing: pg_combinebackup took 150–200 s on EXT4/XFS/BTRFS and 900–1000 s on ZFS. Adding prefetch brought ZFS down to about 250–300 s (3–4x faster); prefetch made no difference on XFS and caused a regression on BTRFS.
  - Batched copy_file_range on XFS: 150 s → 4 s (1%), 150–200 s → 46 s (10%), and 150–200 s → 65 s (20%), with manifest checksums off. With manifest checksums on, the gains were much smaller.
  - Vondra called the patches WIP, "not commit-ready." — [Tomas Vondra, pgsql-hackers](https://www.postgresql.org/message-id/5ac425a4-6201-4f24-912e-8eed6905790a%40enterprisedb.com)
- **Postgres Pro walkthrough** (Pavel Luzanov): a 2.7 GB full backup, a 411 MB first incremental, a 25 MB second incremental, and a 2.7 GB combined output. I saw this only in a search-result summary and did not fetch the page. — [Postgres Pro blog](https://postgrespro.com/blog/pgsql/5970866)

### Inferences
- When manifest checksums are on (CRC32C is the default), pg_combinebackup must read every block it writes to compute checksums. That makes it O(full database size) in reads even with clone/copy_file_range, unless the checksum can be reused from the manifest (only for files copied whole from a chain member).
- The best combine numbers (4 s for 1% changed) appear only with `--no-manifest` and copy-on-write filesystems.
- For the project's benchmark plan, the natural core baselines are:
  - (a) full pg_basebackup time and bytes;
  - (b) PG17/18 `--incremental` bytes sent, plus pg_combinebackup time with `--copy` and with `--clone`/`--link`, with and without manifest checksums;
  - (c) pg_rewind where it applies (divergence scenarios);
  - (d) rsync `--checksum` after pg_backup_start.

### Gaps
- No published TB-scale measurements of incremental backup duration, WAL summarizer CPU or I/O overhead, or summary file sizes.
- No vendor benchmarks (Crunchy Data, Cybertec, Percona, Data Egret) with concrete numbers turned up in my searches. Vendor blogs I saw (pgdash, abcloudz, dbi-services, dzone) appear to be tutorials; I did not fetch them.
- No published pg_rewind timing benchmarks were found.

## 4. pg_rewind: algorithm, prerequisites, limitations, and missing-WAL behavior

### Takeaway
pg_rewind is core's only tool that resynchronizes an existing data directory in place at block granularity. It applies only when the target diverged from the source on a timeline fork, as with an old primary after failover. It finds changed blocks by reading the *target's* WAL from the last common checkpoint before the fork, so the target's WAL must reach back that far (or be fetchable with `-c`). It copies those blocks and every non-relation file whole from the source, then sets up WAL replay via backup_label. It requires `wal_log_hints=on` or data checksums, which are on by default since PG18 initdb.

### Cited Findings
**Purpose and claims**
- pg_rewind synchronizes a cluster with another copy of the same cluster after their timelines diverged, for example to bring an old primary back as a standby. Afterwards the target is "analogous to a base backup" of the source. — [pg_rewind (PG18)](https://www.postgresql.org/docs/18/app-pgrewind.html)
- Unlike a new base backup or rsync, it does not compare or copy unchanged relation blocks. Only changed blocks of existing relation files are copied; everything else (new relation files, config, WAL) is copied in full. The docs say it is "significantly faster" when the database is large and few blocks differ. — [pg_rewind (PG18)](https://www.postgresql.org/docs/18/app-pgrewind.html)

**Algorithm (docs, "How It Works")**
1. Scan the target's WAL from the last checkpoint before the source's timeline forked off, recording every touched data block. If WAL is missing, retry with `-c`.
2. Copy those blocks from the source, via the filesystem (`--source-pgdata`) or SQL (`--source-server`).
3. Copy all other files whole: new relation files, WAL segments, pg_xact, config.
   - The contents of pg_dynshmem, pg_notify, pg_replslot, pg_serial, pg_snapshots, pg_stat_tmp and pg_subtrans are omitted.
   - backup_label, tablespace_map, pg_internal.init, postmaster.opts, postmaster.pid, .DS_Store and pgsql_tmp* are omitted.
4. Write a backup_label so replay begins at the checkpoint created at failover. Set the minimum consistency LSN in pg_control: `pg_current_wal_insert_lsn()` for a live source, or the last checkpoint LSN for a stopped source.
5. On startup, the server replays WAL until it is consistent.

— [pg_rewind (PG18)](https://www.postgresql.org/docs/18/app-pgrewind.html)

**Prerequisites**
- The target needs `wal_log_hints=on` or data checksums, which the doc notes are the initdb default. `full_page_writes` must be on (also the default). — [pg_rewind (PG18)](https://www.postgresql.org/docs/18/app-pgrewind.html)
- `wal_log_hints` is ignored when checksums are on, because hint-bit updates are then always WAL-logged. — [runtime-config-wal (PG18)](https://www.postgresql.org/docs/18/runtime-config-wal.html)
- PG18 made initdb enable checksums by default. — [PG18 release notes](https://www.postgresql.org/docs/18/release-18.html)
- Target state: the target must be shut down cleanly. By default pg_rewind runs single-user crash recovery first if it wasn't; `--no-ensure-shutdown` makes it error out instead.
- Source state: `--source-pgdata` requires a cleanly shut-down source. `--source-server` requires a running source and a normal (non-replication) connection with superuser rights or EXECUTE on pg_ls_dir, pg_stat_file and pg_read_binary_file. — [pg_rewind (PG18)](https://www.postgresql.org/docs/18/app-pgrewind.html)

**Missing WAL**
- The target's pg_wal must reach back to the divergence point. If the target ran a long time after divergence, copy the old WAL from the archive manually or use `-c/--restore-target-wal`, which runs the target's restore_command. — [pg_rewind (PG18)](https://www.postgresql.org/docs/18/app-pgrewind.html)
- WAL the source no longer has must be supplied at startup via recovery.signal and a restore_command. — [pg_rewind (PG18)](https://www.postgresql.org/docs/18/app-pgrewind.html)

**Limitations**
- If pg_rewind fails midway, the target is likely unrecoverable and the docs recommend a fresh backup.
- It copies configuration files from the source, so recovery settings must be fixed before restart, or the target may diverge again.
- It fails immediately on files it can't write, such as read-only SSL keys shared via file mappings.
- Options include `-R`, `--dry-run`, `--no-sync`, `--sync-method` and `--progress`. — [pg_rewind (PG18)](https://www.postgresql.org/docs/18/app-pgrewind.html)

**Release notes**
- PG18 changed only how `--write-recovery-conf` uses the dbname (Masahiko Sawada). — [PG18 release notes](https://www.postgresql.org/docs/18/release-18.html)
- PG17 added `--sync-method` to pg_rewind and other tools. — [PG17 release notes](https://www.postgresql.org/docs/17/release-17.html)
- The PG19 RC notes list no pg_rewind items. — [PG19 release notes](https://www.postgresql.org/docs/devel/release-19.html)
- Stephen Frost (2020) cautioned to use pg_rewind only when you know why the primary failed. — [Stephen Frost, 2020](https://www.postgresql.org/message-id/20200521215306.GF3418%40tamriel.snowman.net)

### Inferences
- pg_rewind's change detection is WAL-based on the *target* side, which is the opposite of incremental backup's source-side summaries. Neither scans data contents.
- It cannot help when the target's WAL since the fork is gone and not archived. It also does not address a copy that is simply stale on the same timeline (a lagging standby whose needed WAL was recycled on the primary). It can't catch up a destination copy of unknown provenance either.
- Non-relation files and new relation files are copied whole. Large new relations created after divergence (or after the copy was taken) are sent in full, and pg_rewind does not hash existing destination data.
- PG18's checksum-on-by-default removes a common blocker (wal_log_hints off) for newly initialized clusters, but not for upgraded clusters.

### Gaps
- I did not fetch pg_rewind's source (filemap.c, pg_rewind.c). I have not confirmed:
  - how it handles size differences, such as truncating a target file that is larger or copying the tail of one that is smaller;
  - its exact "no rewind required" condition;
  - its behavior when the target simply lags on the same timeline. My recollection is that it reports "no rewind required" in that case, but this is unverified.
- No published performance numbers for pg_rewind were found.

## 5. Low-level backup API with rsync or other copy tools: what the docs say

### Takeaway
Core documents three relevant rsync uses. (1) The file-system-level backup recipe: rsync while the server runs, then shut the server down and run `rsync --checksum`, because rsync's mtime granularity is one second. (2) A note that rsync's "vanished source files" exit code is normal during online base backups with pg_backup_start/pg_backup_stop. (3) pg_upgrade's standby procedure, which uses `rsync --archive --delete --hard-links --size-only --no-inc-recursive`, safe only with link mode and caught-up standbys. Core gives no supported recipe for using rsync to refresh a stale running replica.

### Cited Findings
**Low-level API**
- Steps: call `pg_backup_start(label, fast)` on a connection that must stay open for the whole backup, copy the files, then call `pg_backup_stop(wait_for_archive)`. It returns a row whose second field must be written byte-for-byte to `backup_label` and whose third field goes to `tablespace_map` if non-empty.
- Multiple concurrent backups are allowed. `wait_for_archive=false` is risky unless WAL archiving is monitored. — [Continuous archiving §25.3.4 (PG18)](https://www.postgresql.org/docs/18/continuous-archiving.html)
- Omittable files:
  - the contents of pg_wal (recommended) and pg_replslot (recommended);
  - postmaster.pid and postmaster.opts;
  - the contents of pg_dynshmem, pg_notify, pg_serial, pg_snapshots, pg_stat_tmp and pg_subtrans;
  - pgsql_tmp* and pg_internal.init.
  
  The docs page did not mention unlogged relations. — [Continuous archiving §25.3.4.1 (PG18)](https://www.postgresql.org/docs/18/continuous-archiving.html)

**Copy tools and changing files**
- Files changing during the copy is normal during an online backup. Some rsync versions return a separate exit code for "vanished source files", which a driver script should accept.
- GNU tar 1.16+ exits 1 for changed files and 2 for other errors. In 1.23+, `--warning=no-file-changed --warning=no-file-removed` hides the warnings. — [Continuous archiving §25.3.4.1 (PG18)](https://www.postgresql.org/docs/18/continuous-archiving.html)

**rsync file-system backup (§25.2)**
- Run rsync while the server is up, then shut the server down "long enough to do an `rsync --checksum`". The docs say `--checksum` is needed because rsync's modification-time granularity is one second. The second pass is quicker and consistent because the server is down. — [File System Level Backup (PG18)](https://www.postgresql.org/docs/18/backup-file.html)
- The same section says a file-system backup requires the server to be shut down unless an atomic snapshot is used. Individual tables or databases cannot be restored from files without pg_xact. — [File System Level Backup (PG18)](https://www.postgresql.org/docs/18/backup-file.html)
- pg_upgrade's notes also describe a manual copy using `rsync --checksum`, giving the same one-second-granularity reason. — [pg_upgrade (PG18)](https://www.postgresql.org/docs/18/pgupgrade.html)

**pg_upgrade standby procedure (step 11)**
- Applies only when pg_upgrade used link mode. The standbys must be stopped and must have been caught up: matching "Latest checkpoint location" in pg_controldata.
- Run `rsync --archive --delete --hard-links --size-only --no-inc-recursive old_cluster new_cluster remote_dir` on the primary for each standby, plus each tablespace directory and any relocated pg_wal.
- rsync records the hard links pg_upgrade created and recreates them against the standby's matching old files. Unlinked files, usually small, are copied. Temporary and unlogged table files get copied needlessly.
- Users who don't want this are told to recreate the standbys. — [pg_upgrade (PG18)](https://www.postgresql.org/docs/18/pgupgrade.html)
- PG18 added pg_upgrade `--swap` (Nathan Bossart), "potentially the fastest" mode. — [PG18 release notes](https://www.postgresql.org/docs/18/release-18.html)

**Community warnings**
- Stephen Frost (2020): rsync must not be used to rebuild a replica this way except with checksums, and even then it is complicated. Risks include unlogged tables, temp files, WAL management and backup_label contents. — [Stephen Frost, 2020](https://www.postgresql.org/message-id/20200521215306.GF3418%40tamriel.snowman.net)

### Inferences
- The `--size-only` trick in pg_upgrade is only safe because link-mode pg_upgrade guarantees that relation files on a caught-up standby are byte-identical to the primary's. It is not a general delta method.
- For a live source, the documented safe pattern is: pg_backup_start, then rsync with `--checksum` (not the mtime/size quick check), then pg_backup_stop, then write backup_label and tablespace_map, then replay WAL. This is functionally what a chunk-hash tool does, but rsync works per file with a rolling checksum, and `--checksum` reads every byte on both ends. This is the closest core-blessed "reuse older copy" baseline for the project to beat. The docs don't give this exact recipe for online use, so it is an inference that combines §25.2 and §25.3.4.
- Correctness of any such copy rests on backup_label plus WAL replay from the backup's start checkpoint, with full_page_writes repairing torn pages. Byte-identical files are not required at copy time.

### Gaps
- Apart from the PG18 docs, I didn't check older manual versions for wording changes in the rsync notes (for example, whether the `--checksum` note predates PG10).
- The docs page I fetched said nothing about unlogged relations in the low-level API context, though the protocol docs exclude them for BASE_BACKUP.

## 6. Replica re-seeding in core: WAL retention, slots, archive catch-up, and what happens when WAL is gone

### Takeaway
Core prevents re-seeding by retaining WAL, through `wal_keep_size`, replication slots (capped by `max_slot_wal_keep_size`, and in PG18 `idle_replication_slot_timeout`) or a WAL archive reachable via `restore_command`. If a standby needs WAL that is no longer available anywhere, the docs' only remedy is to reinitialize it from a new base backup. Core has no "catch up a stale data directory" tool.

### Cited Findings
**WAL retention settings**
- `wal_keep_size` (default 0, in MB): if a standby falls further behind than this, the sender may remove a segment it needs and the connection is terminated. The standby can recover from an archive if one is in use. — [runtime-config-replication (PG18)](https://www.postgresql.org/docs/18/runtime-config-replication.html)
- `max_slot_wal_keep_size` (default -1, unlimited): if a slot's restart_lsn falls further behind than this, the standby "may no longer be able to continue replication." WAL availability is shown in pg_replication_slots. — [runtime-config-replication (PG18)](https://www.postgresql.org/docs/18/runtime-config-replication.html)
- `idle_replication_slot_timeout` (PG18; default 0, disabled; seconds) invalidates slots that are inactive longer than the timeout, at checkpoint time. — [runtime-config-replication (PG18)](https://www.postgresql.org/docs/18/runtime-config-replication.html); [PG18 release notes](https://www.postgresql.org/docs/18/release-18.html)
- PG17 added `pg_replication_slots.invalidation_reason` and `inactive_since`. — [PG17 release notes](https://www.postgresql.org/docs/17/release-17.html)

**Standby falls behind**
- With streaming replication and no archive, the primary may recycle WAL before the standby receives it. The standby "will need to be reinitialized from a new base backup."
- Avoid this with `wal_keep_size` or a replication slot. With an archive the standby can read, neither is required.
- Slots can fill pg_wal, and `max_slot_wal_keep_size` limits that. — [High availability §26.2.5–26.2.6 (PG18)](https://www.postgresql.org/docs/18/warm-standby.html)

**Standby setup**
- Bootstrap from a base backup (§25.3.2), create `standby.signal`, and set `restore_command` (and `primary_conninfo`). The high-availability chapter does not mention rsync, pg_rewind or reusing an old data directory. — [High availability §26.2 (PG18)](https://www.postgresql.org/docs/18/warm-standby.html)

**pg_upgrade and slots**
- Slots are not carried over for rsync-upgraded standbys before 17. From 17 on, only logical slots are copied. — [pg_upgrade (PG18)](https://www.postgresql.org/docs/18/pgupgrade.html)

### Inferences
- In core, the decision is effectively binary: if the WAL is available, replay it; if not, do a full pg_basebackup. The re-seed path makes no use of the multi-terabyte data already on the stale standby.
- This is precisely the gap a chunk-hash delta tool fills, together with PG17+ incremental backup, which still needs a manifest-backed reference backup and live summaries (section 2).
- A stale standby could in principle use PG17+ incremental backup only if (a) its directory started as a pg_basebackup with a manifest that was kept, (b) `summarize_wal` has been on since that backup, and (c) the summaries are still within `wal_summary_keep_time`. Even then, the standby's own changes from replaying WAL since the backup would not be reflected in the reference manifest. This conclusion is mine, not from the docs.

### Gaps
- I did not fetch the pg_replication_slots view docs for exact invalidation semantics (wal_status values such as "lost"/"unreserved", and the invalidation_reason values).

## 7. Core-community discussions and patches about rsync-like or delta base backups, block-level manifest checksums, or reusing an existing data directory

### Takeaway
I found no core patch or commitfest entry, from 2011 to 2026, that adds destination-side delta transfer, per-block or per-chunk manifest checksums, or reuse of an existing directory to pg_basebackup. The community's answer was to use rsync (2011). Since then it has been to use external tools with delta restore, notably pgBackRest (2020), and since 2024 the WAL-summary-based incremental backup. Committed work in PG17–PG19 focuses on incremental backup, pg_combinebackup copy modes and pg_verifybackup.

### Cited Findings
- 2011: Robert Haas called "differential copy, a la rsync" more useful than a `--force` option. Tom Lane said this was reinventing the wheel and asked why not use rsync. — [Tom Lane, 2011-04-10](https://www.postgresql.org/message-id/9784.1302453336%40sss.pgh.pa.us)
- 2020: Stephen Frost described pgBackRest's delta restore. It restores over an existing cluster, updating only files whose checksums differ from the backup, works in parallel, and is used by Patroni-style HA setups. He was "not aware of anything" that does an rsync-style standby rebuild directly from the live primary. — [Stephen Frost, 2020-05-21](https://www.postgresql.org/message-id/20200521215306.GF3418%40tamriel.snowman.net)
- 2023–2024 incremental backup development ran in the hackers thread "trying again to get incremental backup". Robert Haas was author and committer, and the feature was committed for PG17. The commitfest status comes from a search snippet. — [Commitfest 4648](https://commitfest.postgresql.org/patch/4648); [Haas design msg](https://www.postgresql.org/message-id/CA%2BTgmoYdPS7a4eiqAFCZ8dr4r3-O0zq1LvTO5drwWr%2B7wHQaSQ%40mail.gmail.com)
- 2024: the "pg_combinebackup --copy-file-range" thread covered copy_file_range, CoW alignment and prefetch for combining. — [Commitfest 4338](https://commitfest.postgresql.org/48/4338); [Vondra benchmark](https://www.postgresql.org/message-id/5ac425a4-6201-4f24-912e-8eed6905790a%40enterprisedb.com)
- PG18 committed pg_combinebackup `--link`. — [PG18 release notes](https://www.postgresql.org/docs/18/release-18.html)
- PG19 RC lists no new delta, rsync or manifest-format features. — [PG19 release notes](https://www.postgresql.org/docs/devel/release-19.html)

### Inferences
- The project would not duplicate any core feature. Core has source-side change tracking (WAL summaries and pg_rewind's WAL scan) but no destination-content-aware transfer.
- The most relevant external precedent is pgBackRest's checksum-based delta restore, which works per file rather than per chunk.
- The tool could still reuse core building blocks:
  - pg_backup_start/pg_backup_stop, or BASE_BACKUP's backup mode, for consistency;
  - the backup_manifest format (so pg_verifybackup can check the output);
  - the BASE_BACKUP exclusion list;
  - optionally, WAL summaries as a hint for which blocks to skip hashing.

### Gaps
- I did not systematically search pgsql-hackers archives for 2025–2026 threads (for example "delta restore", "block checksums manifest", "pg_basebackup existing directory"). An archive search on postgresql.org/list could reveal recent proposals I missed.
- I saw a blog post titled "After pgBackRest" on thebuild.com that may discuss the status of pgBackRest. I did not fetch it, so I make no claim about pgBackRest's maintenance status. — [thebuild.com](https://thebuild.com/blog/after-pgbackrest/) (unverified)
