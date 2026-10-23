# Experiment 1: change patterns

## Question

When a known fraction of rows changes under a given workload shape, what fraction of 8 KiB pages, and of larger aligned chunks, actually differs between two physical backups of the same cluster? And how does that compare with what PostgreSQL's own change tracker (WAL summaries, as used by incremental backup) reports?

The tool's benefit depends on the answer. Uniformly random 1% row updates at about 100 rows per page would dirty about 63% of heap pages on paper, but nobody has measured real PostgreSQL patterns.

## Method

`run_study.py` runs each trial on a fresh, disposable cluster:

1. **Cluster.** `initdb` in a new temporary directory (data checksums on unless `--no-checksums`; locale C, UTF8). The server listens on a Unix socket only. Settings are left at their defaults apart from `listen_addresses = ''`, `wal_level = replica`, `max_wal_senders = 10` and `summarize_wal = on`. The trial JSON records the appended lines and every non-default setting.
2. **Dataset.** `pgbench -i -s SCALE` (100,000 × SCALE `pgbench_accounts` rows) plus `events(id bigserial primary key, ts timestamptz, payload text)`. `events` is pre-filled with `--events-rows` rows, by default as many as `pgbench_accounts`. Then `VACUUM ANALYZE`, row counts, and `CHECKPOINT`.
3. **B0.** `pg_basebackup -Fp -X stream --manifest-checksums=SHA256 -c fast`.
4. **Workload.** Exactly one pattern, deterministic for `--seed`. Python generates a SQL script with one row per statement, 100 statements per `BEGIN`/`COMMIT` (`--batch-size`), and `psql -X -q -v ON_ERROR_STOP=1` runs it. k is `round(fraction × rows of the table the pattern changes)`.

   | Pattern | Rows changed |
   |---|---|
   | `uniform` | k distinct `pgbench_accounts` rows sampled without replacement over the whole table; `abalance = abalance + 1` |
   | `hot` | Same, but sampled from the first `--hot-percent` (default 1%) of `aid` values. Errors if k exceeds the hot set. |
   | `clustered` | k consecutive `aid` values from a random start |
   | `append` | k new `events` rows |
   | `retention` | k new `events` rows and the k oldest deleted (2k rows changed) |
   | `tpcb` | `pgbench -b tpcb-like -t k -c 1 --random-seed=SEED`; rows changed = distinct `aid` in `pgbench_history` |

5. **Post-workload steps** (optional, in this order): `--read-scan` (`SELECT count(*)` on every table, with index scans disabled so the heap is read), `--vacuum` (`VACUUM`, never `FULL`), `--checkpoint`.
6. **B1 and I1.** A full backup B1, then `pg_basebackup --incremental=B0/backup_manifest` (I1) with the same options. B1 goes first so that anything changing between the two shows up as tracked-but-unchanged (harmless), never as changed-but-not-tracked.
7. **Ground truth.** After both backups, SQL checks confirm the exact row-level change (for example `count(*) WHERE abalance <> 0 = k`, or the `events` id range). A mismatch fails the trial. Row counts and relation sizes are recorded before and after.
8. **Measurement**, after the cluster stops:
   - **Content changes:** every file of B0 and B1 is compared in aligned 8 KiB blocks. Counts for 16 KiB to 1 MiB chunks are derived from those: a chunk changed if any of its blocks did. Results are broken down by area (relation, WAL, backup metadata, other), top directory, relation kind (heap, index, toast, sequence, fsm, vm, init, unmapped, non_relation), catalog versus user, and relation name. Relation files are mapped to names and kinds through `pg_relation_filepath()` and `pg_class`, queried in every database before B0 and again after I1. template0 refuses connections, so it reuses template1's mapping (initdb creates it as a file copy) and is marked as such. Only regular files reached without following a symlink are read. Symlinks, files under a symlinked directory and other file types are listed in `content_changes.skipped_non_regular` and count as absent on that side.
   - **Tracker view:** I1's size, its `INCREMENTAL.*` files and their block lists, and its whole files. The block lists are parsed from the PostgreSQL 17 header (see `tracker.py` for the format and source citations). Each `INCREMENTAL` file is kept under `tracker.incremental_files.by_file`, keyed by the original relation path (`base/5/16384.1`), as its on-disk `size`, `truncation_block_length` and `blocks`. The blocks are segment-relative and written as inclusive ranges, for example `["0-12", "40", "77-80"]`; `tracker.expand_ranges()` turns them back into numbers. As a second view, `pg_walsummary` reads the live cluster's `pg_wal/summaries` files covering B0 to I1; backups carry only an empty summaries directory. Entries there that are not regular files with a summary name, such as the summarizer's in-progress `temp.summary`, are skipped and listed in `tracker.wal_summaries.ignored_files`.
   - **Agreement:** for every relation file, the content-changed blocks within B1's length are compared with the blocks I1 carries. For an `INCREMENTAL` file that is its block list; for a file I1 sends whole, all its blocks. The counts are changed-and-tracked, changed-but-not-tracked, tracked-but-unchanged and tracked-beyond-B1's-end, overall, per I1 method (`incremental`, `full`, `absent`) and per kind. Each disagreement category lists up to 20 example (file, block) pairs, taken round-robin across files. The same comparison runs against the WAL-summary blocks.
9. **Cleanup.** `pg_ctl stop -m fast`, then the temporary directory is deleted unless `--keep`.

## Definitions

- **Changed fraction** is changed chunks ÷ chunks covering the longer of the two versions of each file. Files on only one side count as entirely changed. Every small file counts as one chunk at any chunk size, so at large sizes the ~950 tiny catalog files dilute this count. **Send fraction** (`send_bytes` ÷ B1 bytes) is the byte-weighted view to use when comparing chunk sizes.
- **`data`** totals cover everything except `pg_wal/` and the backup metadata (`backup_label`, `backup_manifest`, `tablespace_map`), which any refresh ships fresh. **`all`** includes them.
- **`send_bytes`** is the B1 bytes inside changed chunks: what a page-aligned transfer at that chunk size would ship, before compression and protocol overhead.
- **Target table** is `pgbench_accounts` for the update patterns and `tpcb`, and `events` for `append` and `retention`. `ground_truth.pages_per_row` gives its changed heap and index pages, per changed row too, with these heap figures (main fork, all segments):
  - **`heap_existing_changed_pct`**: changed heap pages within B0's length ÷ B0's heap pages (`heap_pages_existing`, P). Pages cut off by a truncation count here as changed. Compare this one with the model.
  - **`uniform_model_heap_existing_pct`**: `1 − (1 − 1/P)^k`, the expected share of existing pages touched if k rows were hit uniformly at random. It predicts nothing about appended pages, and only the `uniform` (and roughly `tpcb`) pattern fits its assumption.
  - **`heap_appended_pages`**: pages in B1 beyond B0's length, mostly new row versions of non-HOT updates or inserts. Every one counts as changed.
  - **`heap_overall_changed_pct`**: all changed heap pages ÷ heap pages covering the longer version. Appended pages are in both numerator and denominator, so it is not comparable with the model.
  - **`index_changed_pct`**: changed ÷ compared pages over all of the table's indexes, appended pages included.

## Running

Requirements: Python 3.10+ (standard library only), PostgreSQL 17 binaries, and an unprivileged user. The binaries come from `--bin-dir`, by default `/opt/homebrew/opt/postgresql@17/bin`; they need not be on `PATH`. Run from the repository root.

```sh
# one trial
python experiments/change_patterns/run_study.py --scale 10 --pattern uniform --fraction 0.01

# patterns x fractions x variants, one JSON per trial plus summary.csv
python experiments/change_patterns/run_study.py --matrix --scale 10 \
  --patterns uniform,hot,append --fractions 0.001,0.01 \
  --variants base,vacuum,no-load-vacuum+read-scan+no-checksums
```

A variant is `base` or a `+`-joined set of `checkpoint`, `vacuum`, `read-scan`, `no-checksums` and `no-load-vacuum`. The plain flags (`--checkpoint`, `--vacuum`, `--read-scan`, `--no-checksums`, `--no-load-vacuum`) apply to every trial. `no-load-vacuum` skips the post-load `VACUUM ANALYZE` and row counts, so the `events` prefill reaches B0 without hint bits. Without it, a read scan finds almost nothing left to hint. Other options: `--hot-percent`, `--events-rows`, `--seed`, `--repeats` (seed + repeat), `--batch-size`, `--work-dir` (parent of the temporary directories), `--keep`, `--stop-on-error` and `--out`.

`--compare-methods` (EXP-02) also rebuilds B1 from B0 with pgdelta and rsync once the cluster has stopped ([compare_methods.py](compare_methods.py)). pgdelta runs with the engine EXP-02 measured, named by its flags: `--jobs 1 --rebuild gap-fill --verify sequential`. So a change of pgdelta's defaults, such as the default `--jobs` or EXP-06's copy-then-patch, doesn't change what is measured, and times stay comparable with EXP-02's. The bytes sent are the same for every engine setting. The trial JSON records the flags under `methods.pgdelta.flags`.

Trial IDs are `<index>-<pattern>-f<fraction>-<options>[-r<repeat>]`, where `<options>` is what the trial actually runs: the variant combined with the plain flags, sorted and `+`-joined, or `base` when none is on. For example `--variants base,read-scan --checkpoint` gives `00-uniform-f0.01-checkpoint` and `01-uniform-f0.01-checkpoint+read-scan`. `params.variant` holds the same label and `params.variant_requested` the `--variants` entry. A matrix in which two entries would run the same trial (same repeat, pattern, row count k and options, as with `--variants base,vacuum --vacuum` or a repeated pattern or fraction) is refused, as is an empty `--patterns`, `--fractions` or `--variants` list.

Cost, measured on an M-series laptop: a scale-10 trial takes 5 to 9 seconds. A 10% fraction there runs 100,000 single-row statements in about 2 seconds. Each trial's temporary directory holds the cluster plus B0, B1 and I1, about 1.4 GB at scale 10 (the data directory alone is 0.66 GB, including the WAL written during the load). Scale 70 is about 1 GB of `pgbench_accounts`, and about twice that with the default `events` size, so expect several GB per trial. Scale 70 has not been run yet.

## Outputs

Everything goes under `--out` (default `results/change_patterns/<UTC time>/`, which git ignores):

```text
run.json                  argv, environment (versions, platform, git provenance), trial statuses
summary.csv               one row per trial: headline numbers
trials/<trial_id>.json    full result per trial
logs/<trial_id>/          stdout/stderr of every command, server.log, error.txt on failure
```

The environment, which every trial JSON repeats, records the git state of the repository: `git_commit` (HEAD), `git_dirty`, up to 50 lines of `git status --porcelain` in `git_status`, and `git_diff_sha256`. That is a 12-character SHA-256 of the status plus `git diff HEAD --binary`, so runs from different uncommitted edits can be told apart; untracked files count by name only. It is `null` for a clean tree, and every field is `null` when git is unavailable.

Main keys of a trial JSON: `params`, `config`, `databases`, `backups` (start LSN and bytes of B0, B1, I1), `workload` (script statement and transaction counts and SHA-256, or the pgbench summary), `ground_truth`, `autovacuum_activity` and `pg_stat_user_tables` snapshots, `content_changes`, `tracker` (including the per-file block lists), `agreement.incremental_backup` and `agreement.wal_summaries`, `timings` and `commands`. A scale-10 trial JSON is about 0.2 to 0.3 MB, of which the block lists take 60 to 130 KB.

CSV columns, one row per trial including failed ones:
- Parameters: `pattern`, `fraction`, `variant` (the effective options), `scale`, `data_checksums`, `load_vacuum`, the post-steps, `seed`.
- Row counts: `rows_changed`, `tpcb_transactions`.
- Target table (see Definitions): `target_table`, `rows_per_heap_page` (before the workload), `heap_pages_existing`, `heap_existing_changed_pct`, `uniform_model_heap_existing_pct`, `heap_appended_pages`, `heap_overall_changed_pct`, `index_changed_pct`.
- Percent of 8 KiB pages changed over every relation of a kind: `pct_8k_changed_all`, `pct_8k_changed_heap`, `pct_8k_changed_index`.
- Whole backup without WAL and backup metadata (`data`), at every chunk size from 8 KiB to 1 MiB (`8k`, `16k`, `32k`, `64k`, `128k`, `256k`, `512k`, `1m`): chunks changed in `pct_<size>_changed_data` and the byte-weighted `pct_bytes_sent_<size>_data`. Also `send_8k_bytes_data`.
- Backup sizes: `b1_bytes`, `i1_bytes`, `i1_over_b1`, plus I1's incremental and full file counts.
- Agreement counts `changed_and_tracked`, `changed_not_tracked`, `tracked_unchanged` and `tracked_beyond_new_end`, three times: unprefixed for I1 over all relation files, `incremental_*` over I1's `INCREMENTAL` files only (the tracker's own block-level view), and `walsummary_*` for the WAL-summary blocks over all relation files.

## First smoke run

October 22, 2026: PostgreSQL 17.11 (Homebrew), macOS arm64, scale 10, fraction 0.01 (10,000 rows), checksums on, default seed. The three base trials were re-run the same day at commit b076955 (clean tree) with the existing/appended split; every figure the first run had reproduced exactly.

| Pattern | Target heap: existing pages changed | Appended heap pages | Target heap overall | Target index pages changed | Data bytes to send, 8 KiB / 1 MiB chunks | I1 / B1 | Changed but not tracked by I1 |
|---|---|---|---|---|---|---|---|
| uniform | 45.40% of 16,394 | 123 | 45.81% | 96.8% | 26.1% / 49.1% | 0.31 | 0 |
| hot | 1.01% of 16,394 | 11 | 1.07% | 1.02% | 0.54% / 1.06% | 0.065 | 0 |
| append | 0.007% of 14,286 (1 page) | 143 | 1.00% | 1.05% | 0.46% / 0.80% | 0.064 | 0 |

`pgbench_accounts` holds about 61 rows per page (16,394 pages), so for 10,000 uniform updates the model predicts 45.66% of existing pages and 45.40% changed. An earlier version of this table compared the model with 45.8%, which also counts the 123 pages appended for new row versions. The primary key changes almost everywhere because only about a quarter of the updates were HOT: pgbench loads at fillfactor 100, so most new row versions need new index entries. For all three patterns, every changed block was also tracked by I1. The WAL summaries alone missed 3 changed blocks in each trial, all in the `pgbench_accounts` free space map, which I1 covers anyway by sending FSM forks whole. The uniform trial with `no-load-vacuum+read-scan+no-checksums` exposes the known hint-bit gap: 14,287 heap blocks (all 14,286 `events` pages plus one `pg_statistic` page) changed but were not tracked, because without checksums hint bits are not WAL-logged. With checksums on, the same scan is WAL-logged and tracked, and I1 grows from 0.31 to 0.66 of B1. These two hint-bit trials were not re-run.

## Limitations

- One machine, local disk, Unix socket; no network and no concurrent workload during the backups.
- Synthetic workloads on pgbench tables at fillfactor 100. Real schemas, wider rows, TOAST-heavy data and other fillfactors will differ.
- No long-running dynamics. Autovacuum stays at its defaults, but a trial lasts seconds, so it rarely runs. `autovacuum_activity` flags it when it does. Use `--vacuum` to see a vacuum's effect explicitly. Checkpoint timing, bgwriter and long-lived hint-bit behaviour aren't modelled.
- B1 and I1 are taken a moment apart, so tracker-only blocks can include changes made in between, such as the backups' own checkpoints.
- The WAL-summary view uses whole summary files that overlap B0 to I1, so its edges may include a little WAL from outside that range.
- Plain format, default tablespace only, 8 KiB blocks and 1 GiB segments assumed. Tested against PostgreSQL 17.11; a single trial also passed on 18.6.

## Tests

`python -m unittest tests.test_change_patterns -v`. Most tests use synthetic files or stand-ins for PostgreSQL: block and chunk diffs, symlink handling, aggregation, the target-table page split, the SQL generator, the INCREMENTAL parser and block-list ranges, the WAL-summary parser and file selection, agreement, trial IDs and duplicate checks, git provenance and the CSV writer. One end-to-end trial (scale 1, uniform 1%) runs when `/opt/homebrew/opt/postgresql@17/bin/initdb` exists and is skipped otherwise. It takes a few seconds.
