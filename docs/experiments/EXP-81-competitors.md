# EXP-81: The fast path against native incremental and WAL-G catch-up, each in its best mode, beyond memory

Status: **pre-registered October 28, 2026, before the registered run** (pilots are listed below). One `ha` VM pair, about 1.5 hours. It is research loop 2's next step: the scorecard's open rows against the strongest competitors.

## Question

**Research loop 2's scorecard left two comparisons open:**
- **A backup chain** (an earlier backup with its manifest, WAL summaries on): PostgreSQL 17's own incremental backup, `pg_basebackup --incremental` then `pg_combinebackup`. It beat pgdelta's hashing in all 42 cells of EXP-04b, in memory, and was never timed against the fast path.
- **A standby behind, with no archive:** WAL-G's catch-up updates a stopped standby in place by page LSN. It was never timed at all.

**The question: beyond memory, on the same disks, is the fast path faster than each competitor in its best mode, and by how much?**

## What each competitor does (from its source, PostgreSQL 17 and WAL-G 3.0.9)

**Native incremental:**
- **The capture** (`basebackup.c`, `sendFile`) reads each changed block with its own 8 KiB read, one after another, in one server process. A file with more than 90% of its blocks changed is sent whole.
- **The combine** (`reconstruct.c`) writes a new directory. Every relation file with a changed block is rebuilt block by block. With `--copy-file-range`, each block is copied by `copy_file_range`, which on XFS with reflinks shares the block instead of copying it. Files with no changed block are copied whole the same way.
- **Correction to research loop 2:** it said the copy options apply only to whole unchanged files. That holds for `--clone` and `--link`; `--copy-file-range` shares the rebuilt files' blocks too (EXP-36 saw it as "sharing blocks").

**WAL-G's catch-up** (`catchup_send_recieve_handler.go`, `pagefile.go`):
- **Its start:** the standby sends its control file and its file list. The source takes every page whose LSN is at or after the standby's latest checkpoint location. So the standby must be stopped cleanly. (A base backup's control file can name a checkpoint later than its start.)
- **Its reading:** a file is skipped only if its modification time equals the standby's copy's exactly. A standby made by `pg_basebackup` never matches. So every relation file is read in full on the source, and its new pages sent, compressed with Brotli.
- **Its writing:** the receiver writes pages in place and syncs nothing (`ApplyFileIncrement(..., fsync false)`). Its standby keeps `standby.signal` and its own `postgresql.auto.conf`, and replays from the source when started.

## Pilots, before this registration (scale 20; not scored)

- **Every arm ran and every copy matched** (1 run each, hot and uniform), WAL-G's standby included once it had replayed.
- **Native incremental's best mode was settled by a pilot** on one B0 and a 1% scattered change. From an unsynced capture (`--no-sync`), XFS has to write each changed block out before it can share it, one block at a time:

  | Capture | Combine | Capture (s) | Combine (s) |
  |---|---|---|---|
  | `--no-sync` | `--copy-file-range` | 1.16 | **8.62** |
  | synced (fsync, the default) | `--copy-file-range` | 1.24 | **0.75** |
  | synced (syncfs) | `--copy-file-range` | 1.20, 1.18 | 0.76, 0.77 |
  | synced (fsync) | `--clone` | 1.21 | 3.01 |

  So the capture is synced, as by default, and the combine uses `--copy-file-range`.

## Design

**The lab:** EXP-70's (`experiments/network/lab_exp64.sh`), with new arms and modes committed before this registration.

**The setting:**
- the `ha` set (n2d-highcpu-8, 150 GiB balanced PDs, us-west1-b and -c), EXP-48's 18 GB database (scale 600), PostgreSQL 17.11, WAL-G 3.0.9 (its published SHA-256 checked), pgdelta at this commit, `SERIES=1`;
- **dest's `/data` on XFS with reflinks** (`reformat_dest_xfs.sh`), which native incremental's best mode needs. Every arm runs on it, so the fast path is timed on XFS here; its earlier cells were on ext4.

**Two old copies per cell, both taken before the change:**
- **B0, a base backup with its manifest, never started:** a backup chain's last link.
- **B0s, a standby** (`pg_basebackup -R`), started, caught up and stopped cleanly.

Each run that changes its old copy starts from a real copy of it (`cp --reflink=never`). Native incremental reads B0 itself and writes a new directory.

**The arms:**

| Arm | Old copy | What runs | Time to |
|---|---|---|---|
| `fast8wf2` | B0 | The fast path, today's defaults (8 reader threads, the fsync worker, gaps of up to 2 pages filled) | B refreshed in place and synced |
| `incr` | B0 | `pg_basebackup --incremental=B0/backup_manifest -X stream -c fast`, then `pg_combinebackup B0 inc -o out --copy-file-range --manifest-checksums=NONE --sync-method=syncfs` | A new directory, synced |
| `full` | — | `pg_basebackup -X stream -c fast` | A new copy, synced |
| `sb_fast8wf2` | B0s | The fast path, as above | B refreshed in place and synced |
| `sb_walg` | B0s | `wal-g catchup-receive` on dest, `wal-g catchup-send` on the source, then `sync -f` on dest | B refreshed in place and synced |

**The cells:** `hot` and `uniform` (EXP-64's changes, about 64 s and 460 s of load on the source).

**Runs:** 2 per arm, shuffled per cell (seed 810); 20 runs.

**Every copy is checked:** started, and its `pgbench_accounts` compared with the source's.
- **The fast path's copies and native incremental's output** recover from their `backup_label`.
- **WAL-G's** stays a standby, and is read once it has replayed to the source's current LSN.

**The smoke** at scale 20 (seed 812) decides nothing: every run exits 0, every copy matches, both fast arms used the fast path.

**The command:**

    PATTERNS="hot uniform" ARMS="fast8wf2 incr full sb_fast8wf2 sb_walg" STANDBY_TOO=1 SERIES=1 OUT=/data/exp04-results/exp81 lab_exp64.sh SOURCE 2 810

**The analysis,** committed before the data: `analysis/exp81.py`, dry-run on the pilot's runs. Data `docs/experiments/data/exp81-cells.csv`, figure `exp81-competitors`.
- **The dry run gave** 0.90 (hot) and 0.32 (uniform) for prediction 1, and 0.24 and 0.59 for prediction 2.
- **It predicts little:** at scale 20 the whole database (600 MB) is in memory, and every relation file is small, so the combine rebuilds most of the database. At scale 600 only the changed 1 GB segments are rebuilt, and the reads go to a cold disk.

## Predictions

**What the source code and earlier sessions imply (inferred, not measured at this size):**
- **Native incremental, hot:** a capture of a few seconds, plus a combine that shares about 2–3 GB of rebuilt files block by block (about 20 µs a block in the pilot). About 7–13 s against the fast path's 2 s: a ratio of 0.15–0.3.
- **Native incremental, uniform:** the capture reads about 620,000 blocks one at a time from a cold disk, at roughly 0.3–0.7 ms each; EXP-64's single-reader fast path averaged about 0.67 ms a request. Then it rebuilds about 9 GB of files. About 250–450 s against the fast path's 42 s: 0.1–0.17.
- **WAL-G:** it reads all 18 GB on the source (about 86 s at 210 MB/s). In uniform it then compresses about 5 GB of pages with Brotli on one core and syncs them on dest. About 90 s (hot) and 130–200 s (uniform): ratios of about 0.02 and 0.2–0.33.

**Scored:**
1. **Deciding: the fast path takes at most 0.80 of native incremental's time in both cells** (`fast8wf2` over `incr`, medians).
2. **The fast path takes at most 0.80 of WAL-G catch-up's time in both cells** (`sb_fast8wf2` over `sb_walg`, with WAL-G's sync).
3. **Correctness:**
   - every run exits 0, and every copy matches the source: the fast path's, native incremental's and WAL-G's;
   - `fast8wf2` was proven by B0's backup manifest, and `sb_fast8wf2` by "clean shutdown in recovery".

**Why 0.80:** a clear win, beyond the proxy's error (rule 13) and the noise (rule 17: 2.08% within a session for runs of 10 s or more, 7.54% under). A ratio within 0.05 of 0.80 gets a second session (rule 11).

**Reported, not predicted:**
- each competitor's phases: native incremental's capture and combine and the incremental's size; WAL-G's catch-up and its sync;
- the bytes each arm moved over the network (dest's received bytes), and both disks' reads, writes and busy time;
- every arm over `pg_basebackup`;
- the fast path on XFS against its ext4 cells (EXP-79b: 42.17 s uniform; EXP-80b: 2.03 s hot).

## The rules this session runs under

- **Rule 13:** this measures competitors, not a candidate change, so no local ratio promotes anything. The pilots checked the harness and settled native incremental's best mode. A proxy-fidelity pair is logged where one exists: the fast path's ext4 cells against these.
- **Rule 15:** not a milestone; no held-out cell is used.
- **Rule 16:** `analysis/integrity.py register EXP-81` records the analysis, the lab, the disk sampler and pgdelta's modules as of this commit. The check at the write-up explains any change.
- **Rule 17:** above.

## What follows

- **If 1 and 2 hold:** research loop 2's two open rows become measured wins, against the competitor's best mode. RESULTS and the scorecard say by how much, and where each competitor's time goes.
- **If 1 fails in a cell:** native incremental is the faster refresh of a backup chain there. The claim narrows to copies without one, and the phases show which of its steps won.
- **If 2 fails:** the same for WAL-G and stopped standbys.
- **If 3 fails:** the failing arm's result is not scored until its run is understood. If it is pgdelta's, the fast path's claim is withdrawn.
