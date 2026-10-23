# EXP-03 tooling: tricky old copies

This folder builds the nine old-copy situations of [EXP-03](../../docs/experiments/EXP-03-tricky-copies.md) on real PostgreSQL clusters, plus three variants: S1b (EXP-03's review), and S2b and S2c (for T11, see [experiments/incumbents](../incumbents/README.md)). For each one it records what an LSN-skipping shortcut would get wrong, and it checks the tool's rebuilt copy once the tool exists.

Requirements: Python 3.10 or newer, standard library only. You need PostgreSQL 17 and 18 bin directories; the defaults are the Homebrew paths `/opt/homebrew/opt/postgresql@17/bin` and `/opt/homebrew/opt/postgresql@18/bin`. Nothing is taken from `PATH`. Every server runs as the current (non-root) user. Each one listens only on a Unix socket in a short private `/tmp/pgd*` directory, on a port picked at run time.

## Quick start

```sh
# Build, evaluate and summarize all nine scenarios into results/exp03/ (about 40 s, 1.8 GB)
python experiments/tricky_copies/run_scenarios.py run all

# Or one step at a time
python experiments/tricky_copies/run_scenarios.py list
python experiments/tricky_copies/run_scenarios.py build S2 S5 --rows 50000
python experiments/tricky_copies/run_scenarios.py evaluate results/exp03/S2-ex-primary-after-failover
python experiments/tricky_copies/run_scenarios.py aggregate results/exp03
```

`results/` is gitignored. Working clusters live in `<scenario>/pgdwork-*` while a scenario builds and are deleted afterwards. With `--keep` they stay, stopped, and `scenario.json` → `source_cluster` names the source's data directory (T11 starts a copy of it). Scenario ids are matched in any case (`s2b`, `S1B`). `--rows` sets the size of the main table. The default of 200,000 rows gives copies of 70 to 190 MB; S2, S3 and S5 are the larger ones, because they hold more WAL or more databases.

## The scenarios

P is the primary, R the standby, B the old copy, and S the fresh backup of the source. S1 to S8 use `--pg-bin`, which defaults to PostgreSQL 17. All clusters use `--locale=C -E UTF8`.

| ID | Builds B as | Source and S | LSN-skip prediction |
|---|---|---|---|
| S1 | P with checksums, standby R from `pg_basebackup -R`, streaming. Burst of writes on P, `CHECKPOINT` on P, wait for R to replay it, stop R with `-m fast`. B is R's data directory. | More writes on P; S from P | correct |
| S2 | As S1 up to stopping R (the split point). Then diverging writes on P: an update touching about a third of the `accounts` pages, plus an insert. Stop P cleanly. B is P's data directory. | Start R alone, promote it (timeline 2), write on R; S from R | wrong |
| S2b | As S2, but P runs with `checkpoint_timeout = 1d` and `max_wal_size = 10GB`, and `pg_buffercache` is created before R is cloned. After the diverging writes, `pg_buffercache_evict()` (PostgreSQL 17 and later; it returns a boolean in 17 and a record in 18) writes every dirty buffer of the user relations (OID 16384 and up, all forks) to disk without a checkpoint; the diverged pages are read back from disk to check their LSNs. Then `pg_ctl stop -m immediate`. B is P's data directory: crashed, with its REDO location before the fork and post-fork pages on disk. The diverging writes also update `tags` (see below). | As S2 | wrong |
| S2c | P as in S2b (no checkpoint after the fork, `pg_buffercache`), with two standbys cloned from it: R and Q. Burst on P, `CHECKPOINT`, wait until R and Q replay it, stop R cleanly (the split point). Diverging writes on P as in S2b; wait until Q replays them. On Q, a hot standby, `pg_buffercache_evict()` writes the dirty buffers of the user relations to disk without a restartpoint (Q's `pg_control_checkpoint()` is read before and after), and the diverged pages are read back from Q's disk. Stop Q with `-m fast`, then stop P with `-m immediate` and delete it. B is Q's data directory: "shut down in recovery", with its REDO location before the fork (its shutdown restartpoint is at the last checkpoint it replayed) and post-fork pages on disk. | Start R alone, promote it (timeline 2), write on R; S from R | wrong |
| S3 | P with checksums; pgbench writes in 2 sessions, a `CHECKPOINT` mid-run, then `pg_ctl stop -m immediate` while pgbench is still writing. B is a `shutil.copytree` of the data directory. | Restart P (crash recovery), more writes; S from P | correct |
| S4 | B is a base backup of P. | Delete the upper half of `accounts`, `VACUUM accounts`; the heap file is checked to be shorter in S than in B; S from P | correct |
| S5 | Template database `tmpl` with data, then `app` created from it and written to. B is a base backup. | `CREATE DATABASE app2 TEMPLATE tmpl STRATEGY FILE_COPY`; drop `app` and recreate it the same way; S from P | wrong |
| S6 | P without checksums. B is a base backup. | Stop P, `pg_checksums --enable`, start, small writes; S from P | wrong |
| S7 | P without checksums and `wal_log_hints=off`. Bulk load in one transaction with indexes created first, so nothing reads the rows back, then `CHECKPOINT`. B is a base backup. | Read-only full scans of every table set hint bits, then `CHECKPOINT`; the xid horizon must not move between B and S; S from P | wrong |
| S8 | Cluster X built with the same procedure. B is a base backup of X. | A second cluster Y; S from Y | n/a |
| S9 | A PG17 cluster with checksums. B is a PG17 base backup. | Stop it, `initdb` a PG18 cluster with matching settings, `pg_upgrade --link`, start the 18 cluster; S with PG18 `pg_basebackup` | n/a |

For S2, S2b and S2c, `scenario.json` → `observations` also records:
- `fork_lsn`: the switchpoint in S's timeline history file;
- `old_copy_redo_lsn`, `old_copy_checkpoint_lsn` and `old_copy_min_recovery_lsn` (B's `pg_controldata`; the last is `0/0` for a primary);
- `post_fork_pages`: every relation page of B whose on-disk LSN is after the fork, as `{file, block, kind, lsn, source_lsn, exposed}`, with `block` counted within its segment file. `source_lsn` is S's page LSN at that block (`null` if S has no such page);
- `exposed_pages`, with `exposed_pages_by_kind` and `exposed_pages_by_file`: how many of those pages are **exposed**. S holds that block with a page LSN at or below B's REDO location, and other bytes than B, so a tool that sends only the pages newer than B's REDO location skips it and keeps B's diverged bytes.

The check `exposed_pages` fails the build when none is. In S2, B's REDO location (its shutdown checkpoint) lies after the fork, so the exposure comes from the diverging writes themselves. In S2b and S2c it lies before the fork. There, a page is exposed only if the source didn't rewrite its block after the fork, so the diverging writes also update a table that `burst_sql` never writes (`UPDATE tags SET v = upper(v) WHERE k = 3`): its pages are exposed by construction, however many `accounts` pages burst 2 rewrites. S2 keeps EXP-03's diverging writes.

S2b's and S2c's checks add:
- `old_copy_crashed` (S2b) or `old_copy_is_stopped_standby` (S2c: the state is "shut down in recovery");
- `no_checkpoint_after_fork`: B's REDO and checkpoint locations are both before the fork;
- `diverged_pages_on_disk_after_fork`: every `accounts` and `tags` page the diverging writes updated is on that list. Its `flush` field holds the eviction's counts by table, the cluster it ran on, and its `pg_control_checkpoint()` location before and after the eviction, which must be equal.

The workload is deterministic SQL (`generate_series`, `md5`) over four tables:
- `accounts` has a primary key and a secondary index.
- `history` has a `bigserial` key.
- `docs` holds TOASTed text.
- `tags` has no primary key.

A "burst" updates about 0.1% of `accounts` rows, spread over many pages. It also inserts and deletes `history` rows, rewrites some `docs`, and ends with `VACUUM`, so the visibility map and free space map change too.

## What a scenario directory holds

| File | Contents |
|---|---|
| `old_copy/` | B, exactly as the scenario defines it: a data directory for S1–S3, a base backup for S4–S9 |
| `source_backup/` | S, from `pg_basebackup -Fp -X stream --manifest-checksums=SHA256 -c fast` |
| `expected_state.json` | Logical state of the source right after S: every database, and for every permanent user table in every connectable database, a row count and an md5 |
| `scenario.json` | Description, predictions, PostgreSQL versions, threshold and its source, the system identifier, timeline, checksum version and `pg_controldata` summary of B and S, the quiesce check, scenario checks, observations and a timed step log |
| `lsnscan.json` | Output of `lsnscan.scan(S, B, threshold)`, unchanged |
| `wrong_skips.json` | Why each wrongly-skipped page differs (see below) |
| `logs/` | Server, pgbench and pg_upgrade logs |
| `evaluation.json`, `output/`, `tool_stats.json`, `tool.log` | Written by `evaluate` |

How the values are defined:

- **Quiesced source.** The builder writes synchronously, so nothing else is running when S starts. To prove it, `scenario.json` records `pg_snapshot_xmax(pg_current_snapshot())` before S and after the logical state was read; the two must be equal. It also records that no other client sessions were connected.
- **Logical state.** The md5 is taken over the md5 of each row's text form, in primary-key order. A table without a primary key is ordered by the whole row's text, with `COLLATE "C"`. The session pins `DateStyle`, `TimeZone`, `extra_float_digits`, `IntervalStyle` and `bytea_output`. It also turns index scans off (`enable_indexscan`, `enable_indexonlyscan`, `enable_bitmapscan`), so each table is read from its heap: by default PostgreSQL 17 reads it through the primary key, and the count and md5 would then miss heap tuples that no index entry points to. Unlogged and temporary tables are listed but not hashed, because a restored backup empties them. `template0` is listed but not read.
- **Threshold.** If B has a `backup_label` (a base backup), the threshold is its `START WAL LOCATION`. Otherwise it is `Latest checkpoint's REDO location` from the matching-version `pg_controldata`.

### Why pages were wrongly skipped

`lsnscan.json` counts wrongly-skipped pages by fork. `classify.py` walks the same pages with the same rule and sorts each one into a cause:

| Cause | Meaning |
|---|---|
| `checksum_only` | Only `pd_checksum` differs |
| `lsn_only` | Only `pd_lsn` and the checksum differ: identical rows written at another WAL position |
| `hint_bits_only` | Equal once the hint bits PostgreSQL may set without WAL are masked, the same fields the server's own `heap_mask` and `btree_mask` mask |
| `hint_bits_and_lsn` | As above, but `pd_lsn` also differs |
| `fsm`, `vm` | Free space map or visibility map page |
| `content` | Still different after masking: real stale data |

## Evaluate

```sh
python experiments/tricky_copies/run_scenarios.py evaluate DIR [DIR ...] \
    [--python PY] [--repo-root DIR] [--tool-cmd "CMD"] [--tool-args="--flag value"] \
    [--pg-controldata auto|PATH|none] [--skip-tool] [--selfcheck auto|always|never] [--discard-output]
```

This runs the tool contract:

```
PY -m pgdelta local --source S --basis B --output O --stats-json tool_stats.json [--pg-controldata PATH] [EXTRA]
```

The command runs with `PYTHONPATH` set to `--repo-root` (default: this checkout) and with the repo root as the working directory. `--pg-controldata auto` passes the binary that matches S's version. `--tool-cmd` replaces `PY -m pgdelta`, and `{python}` inside it is substituted. Use `--tool-args=` with an equals sign for flags that start with a dash, such as an identity-gate override. The stats file must contain `bytes_source_to_dest`, `bytes_dest_to_source`, `mode` (`reuse` or `full`) and `pages` {`total`, `sent`}.

If `pgdelta` cannot be imported, the tool step is recorded as `tool not available` and the other steps still run. Then:

1. O is compared with S: every file, directory and symlink, byte for byte.
2. Matching-version `pg_verifybackup` runs on O.
3. O is copied to a disposable directory and started on a Unix socket and a free port. The run waits until recovery has finished, recomputes the logical state and compares it with `expected_state.json`.

A size-and-mtime fingerprint also shows whether the tool left B untouched.

When the tool did not succeed, `--selfcheck auto` runs steps 2 and 3 on S itself. That proves the expected state and the restore procedure are sound before anyone blames the tool. `evaluation.json` holds the tool result and stats, `byte_identical`, `verifybackup`, `restore_ok`, `logical_match`, the LSN-skip numbers and the self-check.

T11 reuses this path from Python. `run_tool` takes `output`, `stats_path` and `log_path`, so O and its files can go elsewhere than the scenario directory. `restore_and_compare` takes `while_running(cluster)`, which runs more checks on the restored server; T11 runs pg_amcheck there.

`aggregate RESULTS_DIR` writes `summary.csv`, with every column, and `summary.md`, a compact table. Each also says whether the pre-registered LSN-skip prediction held. A "wrong" prediction holds when more than 0 pages were wrongly skipped; a "correct" one holds when 0 were.

## Choices that affect the numbers

- **No background activity.** Autovacuum is off and `checkpoint_timeout` is 30 minutes in every cluster (a day in S2b and S2c). The only checkpoints and vacuums are the ones a scenario runs, so repeated builds give the same counts. S3 is the exception, because it depends on pgbench timing.
- **S1's threshold.** `CHECKPOINT` runs on P before R stops, so B's REDO location is a restartpoint after the first burst, as on any standby that has run through a checkpoint. Without it, the threshold falls back to the start of the base backup R was cloned from. A check during development found 0 wrongly-skipped pages in that variant, against 59 with the checkpoint, all of them hint bits and FSM pages.
- **S5's database OIDs.** The recreated databases get new OIDs, so their files live under different `base/<oid>` paths than anything in B. The path-matched analysis never compares them. `scenario.json` → `observations.databases` records, per database, whether its directory exists in B and how many of its pages carry LSNs below the threshold. Reusing the old OID would make the paths coincide, but the scenario deliberately does not force that.
- **S8.** Both clusters run the same deterministic procedure, so most differing pages differ only in `pd_lsn` (`lsn_only`). Only the pages ANALYZE samples randomly differ in content.
- **S9.** User relfilenodes are preserved by `pg_upgrade` and their files are hard links. The catalogs are rebuilt, and their initdb-era LSNs sit below B's threshold.

## Files

| File | Role |
|---|---|
| `run_scenarios.py` | CLI: `list`, `build`, `evaluate`, `aggregate`, `run` |
| `scenarios.py` | Workload SQL, the builders and their checks, `build_scenario` |
| `pgcluster.py` | initdb/pg_ctl/psql/pg_basebackup wrappers, `pg_controldata` and `backup_label` parsing, threshold selection |
| `logical.py` | Logical state and its comparison |
| `evaluate.py` | Tool run, tree comparison, `pg_verifybackup`, restore check |
| `classify.py` | Causes of wrongly-skipped pages |
| `summary.py` | `summary.csv` and `summary.md` |
| `lsnscan.py` | The LSN-skip analyzer (pre-existing, unchanged) |

`build_scenario(..., source_conf={...}, old_copy_conf={...})` adds server settings to the cluster that becomes the source, and to the one whose data directory becomes B, each from its creation on. A standby cloned from a primary of the other role gets its own role's settings, and the other role's reset to their built-in defaults. EXP-03 uses neither. T11 gives the source `wal_keep_size`, so it keeps its WAL since the fork, and B keeps EXP-03's settings unless asked otherwise.
- `old_copy_conf` only applies where B is a cluster of its own: S1, S1b, S2, S2b and S2c (Q, the standby that becomes B; P, which is discarded, and R run with the source's settings). Given for another scenario, its `old_copy_conf_applied` check fails.
- `scenario.json` → `server_conf` records both, and with which role's settings the source and B's cluster ran: `source_ran_with` and `old_copy_ran_with`. The latter is `null` when B is a base backup, which never ran, and `source` in S3, where B is a copy of the source's own data directory.

Tests: `python -m unittest discover -s tests`. `tests/test_tricky_copies.py` holds unit tests plus one integration test, which builds S1 at 2,000 rows, runs the analysis and evaluates a stand-in tool. It takes about 3 s and is skipped when PostgreSQL 17 is not installed. S2b's and S2c's checks, the exposed pages, and the builds of S2b and S2c are tested in `tests/test_incumbents.py`.
