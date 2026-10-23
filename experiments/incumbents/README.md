# T11 tooling: incumbent refresh tools on tricky old copies

This folder runs the correctness half of test T11 ([decision ledger](../../docs/DECISIONS.md), experiment queue). It takes EXP-03's tricky old copies S1, S2 and the new S2b, and records whether each incumbent refresh tool refuses, repairs the copy correctly, or produces wrong data. pgdelta runs on the same copies as the reference.

Requirements: Python 3.10 or newer, standard library only, Linux or macOS. Every server runs as the current (non-root) user on a Unix socket, as in [EXP-03's tooling](../tricky_copies/README.md). Needed:
- a PostgreSQL 17 bin directory (`--pg-bin`) with `pg_rewind` and `pg_amcheck`, and contrib (`amcheck`, `pg_buffercache`). The scenarios, pg_rewind and pgdelta also run with PostgreSQL 18;
- optionally `pg_probackup` and `wal-g`. A tool that is missing is recorded as skipped, with the reason.

On a benchmark VM, [`infra/gcp/install_incumbents.sh`](../../infra/gcp/install_incumbents.sh) installs both.

**Status:** pg_rewind, pgdelta and the `lsn_skip` control have run on the Mac. The pg_probackup and WAL-G adapters are written from their documentation and source, but have not run yet: neither tool is installed on the Mac.

## Quick start

```sh
# The Mac: pg_rewind and pgdelta only, small scenarios (about 15 s)
python3 experiments/incumbents/run_t11.py --tools pg_rewind,pgdelta,lsn_skip --rows 20000

# A Debian VM after install_incumbents.sh, every tool, EXP-03's default scale
python3 experiments/incumbents/run_t11.py --pg-bin /usr/lib/postgresql/17/bin \
    --pg-probackup /usr/bin/pg_probackup-17 --wal-g /usr/local/bin/wal-g --out /data/t11

# Another tool later, on the same builds
python3 experiments/incumbents/run_t11.py --tools walg --reuse-scenarios --rows 20000

# pg_rewind when B also kept its own WAL, in a separate results directory
python3 experiments/incumbents/run_t11.py --tools pg_rewind --scenarios S2,S2b \
    --old-copy-wal-keep-size 4GB --out results/t11-b-keeps-wal
```

| Option | Default | Meaning |
|---|---|---|
| `--pg-bin DIR` | Homebrew's 17, else `/usr/lib/postgresql/17/bin` | Builds the clusters; provides pg_rewind and pg_amcheck |
| `--pg-probackup PATH` | `auto`: `pg_probackup-17` or `pg_probackup` on `PATH` | `none` skips it |
| `--wal-g PATH` | `auto`: `wal-g` on `PATH` | `none` skips both WAL-G tools |
| `--scenarios` | `S1,S2,S2b` | Any EXP-03 ids, comma-separated, in any case |
| `--tools` | `pg_rewind,pg_probackup,walg,walg_send,pgdelta,lsn_skip` | Comma-separated |
| `--out DIR` | `results/t11` (gitignored) | Results directory |
| `--rows N` | 200,000 | Passed to EXP-03's scenario builder |
| `--wal-keep-size` | `4GB` | `wal_keep_size` for the cluster that becomes the source; `none` gives EXP-03's settings (see below) |
| `--old-copy-wal-keep-size` | `none` | `wal_keep_size` for the cluster whose data directory becomes B; `none` is EXP-03's settings (see below) |
| `--reuse-scenarios` | off | Reuse builds under `OUT/scenarios/` made at the same `--rows` and settings |
| `--keep-copies` | off | Keep each run's copies of B and of the source |
| `--tool-timeout`, `--catchup-timeout` | 1800 s, 120 s | Per tool command; for a refreshed standby to reach the source |

## What a run does

1. **Build.** Each scenario is built with EXP-03's `build_scenario` and `--keep`, under `OUT/scenarios/<id>-<slug>/`. The source cluster stays there, stopped, and `scenario.json` → `source_cluster` names its directory. Rebuilding a scenario deletes its earlier T11 results.
2. **Per (scenario, tool):**
   - The tool gets its own copy of B and its own copy of the kept source cluster, which is started. No run sees another run's WAL, backups or changes.
   - The copies are made with `shutil.copytree`, which keeps file modification times. WAL-G's `catchup-send` uses them to skip files.
   - pgdelta reads the scenario's S and B directly; it never writes to either.
3. **Classify.** Exit status 0 is **accepted**. A nonzero exit is **refused** in three cases, which `refusal` tells apart:
   - `message`: the output holds one of the tool's known refusal messages (`checks.REFUSAL_PATTERNS`), so the tool refused for a stated reason;
   - `exit_code`: the tool exited with one of its documented refusal codes (`checks.REFUSAL_EXIT_CODES`). Only pgdelta has them: 2, invalid or unsupported input. pgdelta never writes to B, so B's state can't tell its refusals from its failures; its exit codes 3 and 4 are **failed**;
   - `error_unmodified`: no such message or code, but the copy of B is unchanged (a size and mtime fingerprint of every file). The tool stopped with an error before touching the copy, without saying it judged the copy unsafe. pg_rewind's "could not find previous WAL record" is one.

   Anything else, including a timeout, is **failed**. `outcome_reason` quotes the refusal message, or else the tool's last error line.

   When the harness, not the tool, keeps a run from completing, the outcome is **harness_error** and the verdict `not_verified`: catchup-push succeeded but no backup name could be found to fetch, catchup-send never reached a listening catchup-receive, or the harness itself raised an exception before the run was classified. `run_t11.py` then exits 1.
4. **Check an accepted copy:**
   - **Diverged pages** (S2 and S2b, where B's timeline forked from the source's). Right after the tool, each of B's pages with an LSN after the fork is compared with the copy, and with S at the same file and block. S2b's `scenario.json` lists those pages; for S2 the same list is computed from S's timeline history file. `diverged_left` counts pages where the copy still holds B's bytes and S holds other bytes, or no page. Each such page is also classified with EXP-03's `classify_pair` (content, hint bits, FSM, VM).
   - **The incumbents** are brought up as a standby of the source:
     1. pg_rewind writes its own `standby.signal` and `primary_conninfo` with `--write-recovery-conf`. For the catch-up tools, the harness writes both.
     2. The harness starts the copy and waits until `pg_last_wal_replay_lsn()` reaches the source's `pg_current_wal_lsn()`. It gives up early when the server log says the WAL is gone, the timeline doesn't fit, or recovery failed.
     3. While the copy is a hot standby, it compares the logical state with `expected_state.json`: every database, and for every permanent user table its row count and md5. A copy that errors on that read counts as a mismatch.
     4. It promotes the copy and runs `pg_amcheck --all --heapallindexed --install-missing`. The promotion is needed because amcheck has to be installed, which a standby can't do.
   - **pgdelta's O** gets EXP-03's checks: byte comparison with S, `pg_verifybackup`, a restore to the end of recovery and the logical comparison. pg_amcheck then runs on the restored server.
5. **Verdict.** `wrong_data` when any check found a problem (logical mismatch, amcheck errors, or `diverged_left` > 0), even if the copy never came up. `repaired` when the copy caught up, matched and passed amcheck. `not_verified` otherwise, and for a harness error. Other runs that weren't accepted keep their outcome as the verdict.

### The tools

| `--tools` name | What runs | Notes |
|---|---|---|
| `pg_rewind` | `pg_rewind --target-pgdata COPY --source-server 'host=SOCKDIR port=P user=postgres dbname=postgres' --write-recovery-conf` | PostgreSQL 13 and later first run single-user crash recovery on a target that wasn't shut down cleanly (S2b), unless `--no-ensure-shutdown` |
| `pg_probackup` | `pg_probackup catchup -b DELTA --source-pgdata=SRC --destination-pgdata=COPY --stream -h SOCKDIR -p P -U postgres -d postgres --exclude-path=postgresql.conf --exclude-path=postgresql.auto.conf --exclude-path=pg_hba.conf --exclude-path=pg_ident.conf` | Its documentation's example for a lagging standby, which keeps the destination's configuration files |
| `walg` | `wal-g catchup-push SRC --from-lsn REDO` into file storage (`WALG_FILE_PREFIX` in the run's work directory), then `wal-g catchup-fetch COPY NAME` | REDO is B's latest checkpoint REDO location, passed as a decimal number: v3.0.9's `--from-lsn` is an unsigned integer and rejects the `X/Y` form, which only later versions accept. The probe records the flag's `--help` line in `tool_info` → `from_lsn_flag`. NAME is read from catchup-push's log, else from `catchup-list --json`. v3.0.9's catchup-push has no system-identifier option. The harness records both identifiers, which are equal in S1, S2 and S2b, and passes one only if `catchup-push --help` lists such an option |
| `walg_send` | `wal-g catchup-receive COPY PORT` in the background, then `wal-g catchup-send SRC 127.0.0.1:PORT` | Retries the send while the receiver isn't listening yet. In v3.0.9, catchup-send itself checks B's system identifier and timeline |
| `pgdelta` | EXP-03's tool step: `python -m pgdelta local --source S --basis B --output O --pg-controldata ...` | The reference |
| `lsn_skip` | EXP-03's LSN-skip shortcut, done in-process from the stopped source onto the copy of B | **Negative control**, not an incumbent: it keeps B's diverged pages, so the checks must call it `wrong_data` on S2 and S2b |

WAL-G runs with `PGHOST`, `PGPORT`, `PGUSER` and `PGDATABASE` pointing at the source copy's socket, and no other `PG*` variables.

## Choices that affect the results

- **The source keeps its WAL (`--wal-keep-size 4GB`); B doesn't, by default.** The setting goes to the cluster that becomes the source, from its creation on: P in S1, R in S2 and S2b. The cluster whose data directory becomes B runs with EXP-03's settings: R in S1 (it gets the default back after cloning P's configuration), P in S2 and S2b. `scenario.json` → `server_conf` records both, and the cluster test checks them with `postgres -C`.
  - *Why the source keeps its WAL.* pg_rewind's copy replays the source's WAL from the last common checkpoint, which is before the fork. A copy refreshed by a catch-up tool replays from the catch-up's start. Under EXP-03's own settings, the source's checkpoints after the fork, and the segment switches of `pg_basebackup`, recycle that WAL. At 20,000 rows pg_rewind then repairs every page, but its copy can't start: the log says "requested WAL segment ... has already been removed". T11 gives the tools the source's WAL, as an archive or a replication slot would. `--wal-keep-size none` reproduces that case.
  - *Why B keeps EXP-03's settings.* pg_rewind also reads B's own WAL, from the last common checkpoint up to where B ends, to find the pages B changed after the fork. Whether B still has it is part of what T11 measures. On the Mac at 200,000 rows it is gone, and pg_rewind stops with "could not find previous WAL record". On S2, P's shutdown checkpoint removed it, and pg_rewind exits before touching the copy (`refused`). On S2b, P's crash left the WAL in place, but pg_rewind's own crash recovery ends with a checkpoint that removes it. pg_rewind then exits after that recovery changed the copy (`failed`). At 20,000 rows all of it fits in one segment, and pg_rewind repairs both.
  - *B with its WAL.* `--old-copy-wal-keep-size 4GB` gives P the same setting. pg_rewind then repairs S2 and S2b at 200,000 rows too. In production, `pg_rewind --restore-target-wal` with a WAL archive plays that role. Use a separate `--out`, so the two runs don't share a summary.
  - Neither setting changes S or the LSN-skip numbers. B's settings change only which of B's WAL segments are still in `pg_wal`.
- **Diverged pages are counted right after the tool, before the copy starts.** This counts what the tool itself left. The logical comparison then shows whether those pages matter once the copy has replayed the source's WAL.
- **amcheck runs after promotion**, on a copy that has already matched the source as a standby. `--heapallindexed` checks that every heap tuple is in its indexes. It doesn't check that every index entry points to a live heap tuple, so a copy can pass amcheck and still fail the logical read.
- **The logical read goes through the heap.** EXP-03's logical state turns index scans off, so `logical_match` counts and hashes every visible heap tuple, including one that no index entry points to. Read through the primary key, as PostgreSQL 17 plans it by default, such a tuple would be invisible to the comparison.

## What a run directory holds

`OUT/<scenario>/<tool>/`:

| File | Contents |
|---|---|
| `result.json` | Everything below |
| `logs/NN-<command>.log` | Each tool command with its full stdout and stderr; `source-server.log` and `copy-server.log` are the servers' logs |
| `tool_stats.json` | pgdelta's statistics (pgdelta only) |
| `work/` | Copies of B and the source, and pgdelta's O; deleted unless `--keep-copies` |

`result.json` holds:
- `commands`: for each command, the exact command line, exit status, seconds, and the last 20 lines of stdout and stderr;
- `returncode`, `seconds` (the tool's own commands only), `target_modified`, `outcome`, `refusal`, `outcome_reason` and, if any, `harness_error`;
- `parsed`, what the tool's output says, from `checks.parse_*`. For pg_rewind: the divergence point, the last common checkpoint, whether crash recovery ran, and whether no rewind was needed. For pg_probackup: `syncLSN`, the start LSN, the transfer ratio and the errors. For WAL-G: the backup name and the LSN used;
- `diverged_pages`, `bring_up` (recovery configuration, LSNs reached, errors, the server log's tail), `logical_match` and `logical_differences`, `amcheck` and `amcheck_ok`, `verdict`. pgdelta's runs also hold `pgdelta_checks` (byte comparison, `pg_verifybackup`).

`OUT/summary.csv` has one row per run under OUT: `scenario, tool, outcome, logical_match, amcheck_ok, diverged_pages_left, seconds`, then `verdict, refusal, outcome_reason`, so a refusal for a stated reason, a tool that stopped with an error and a harness error read differently. `OUT/summary.json` adds each run's bring-up and notes, the tool versions and the scenario facts: B's state and REDO location, the timelines, and where the post-fork page list came from.

## Checked only on Linux so far

- Everything about pg_probackup and WAL-G: that the commands run as written, the refusal messages, the backup-name parsing, whether pg_probackup accepts a crashed B (S2b), and how their copies start.
- `install_incumbents.sh`, including its fallback that builds pg_probackup from source.

## Files

| File | Role |
|---|---|
| `run_t11.py` | CLI: builds the scenarios, runs the matrix, writes the summary |
| `tools.py` | Tool probing, the adapters, the standby and restore bring-up, `run_job` |
| `checks.py` | Outcome and verdict rules, output parsing, the diverged-page comparison, summary files |

Tests: `tests/test_incumbents.py`. The unit tests cover classification, parsing, the diverged-page comparison, the LSN-skip control and S2b's page-LSN check. Two cluster tests run `run_t11.py` on S1, S2 and S2b at 2,000 rows, with pg_rewind and the control. They take about 12 s and are skipped without PostgreSQL 17 (`PGDELTA_TEST_PG_BIN`, default the Homebrew path).
