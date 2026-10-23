# T11 tooling: incumbent refresh tools on tricky old copies

This folder runs the correctness half of test T11 ([decision ledger](../../docs/DECISIONS.md), experiment queue), pre-registered as [EXP-08](../../docs/experiments/EXP-08-incumbent-tools.md) Part A. It takes EXP-03's tricky old copies S1 and S2 and the new S2b and S2c, and records whether each incumbent refresh tool refuses, repairs the copy correctly, or produces wrong data. pgdelta runs on the same copies as the reference.

Requirements: Python 3.10 or newer, standard library only, Linux or macOS. Every server runs as the current (non-root) user on a Unix socket, as in [EXP-03's tooling](../tricky_copies/README.md). Needed:
- a PostgreSQL 17 bin directory (`--pg-bin`) with `pg_rewind` and `pg_amcheck`, and contrib (`amcheck`, `pg_buffercache`). The scenarios, pg_rewind and pgdelta also run with PostgreSQL 18;
- optionally `pg_probackup` and `wal-g`. A tool that is missing is recorded as skipped, with the reason.

On a benchmark VM, [`infra/gcp/install_incumbents.sh`](../../infra/gcp/install_incumbents.sh) installs both.

**Status:** pg_rewind, pgdelta and the `lsn_skip` control have run on the Mac, on S2, S2b and S2c at 200,000 rows in both arms (see [On the Mac](#on-the-mac)). The pg_probackup and WAL-G adapters are written from their documentation and source, but have not run yet: neither tool is installed on the Mac.

## Quick start

EXP-08 runs the whole suite twice, once per arm of B's WAL retention, each into its own results directory:

```sh
# Arm 1: B as EXP-03 builds it, with PostgreSQL's default WAL retention (results/t11/default)
python3 experiments/incumbents/run_t11.py --arm default

# Arm 2: B keeps its own WAL, wal_keep_size 4GB (results/t11/retained)
python3 experiments/incumbents/run_t11.py --arm retained
```

On a Debian VM after `install_incumbents.sh`, with every tool:

```sh
for arm in default retained; do
  python3 experiments/incumbents/run_t11.py --arm "$arm" --pg-bin /usr/lib/postgresql/17/bin \
      --pg-probackup /usr/bin/pg_probackup-17 --wal-g /usr/local/bin/wal-g --out "/data/t11/$arm"
done
```

Other uses:

```sh
# The Mac: pg_rewind, pgdelta and the control only, small scenarios (about 20 s)
python3 experiments/incumbents/run_t11.py --tools pg_rewind,pgdelta,lsn_skip --rows 20000

# Another tool later, on the same builds of one arm
python3 experiments/incumbents/run_t11.py --arm retained --tools walg --reuse-scenarios
```

| Option | Default | Meaning |
|---|---|---|
| `--pg-bin DIR` | Homebrew's 17, else `/usr/lib/postgresql/17/bin` | Builds the clusters; provides pg_rewind and pg_amcheck |
| `--pg-probackup PATH` | `auto`: `pg_probackup-17` or `pg_probackup` on `PATH` | `none` skips it |
| `--wal-g PATH` | `auto`: `wal-g` on `PATH` | `none` skips both WAL-G tools |
| `--scenarios` | `S1,S2,S2b,S2c` | Any EXP-03 ids, comma-separated, in any case |
| `--tools` | `pg_rewind,pg_probackup,walg,walg_send,pgdelta,lsn_skip` | Comma-separated |
| `--arm` | `default`, or `retained` when a nonzero `--old-copy-wal-keep-size` is given | EXP-08's arm: `default` gives B EXP-03's settings; `retained` gives B `wal_keep_size` 4GB, or `--old-copy-wal-keep-size` |
| `--out DIR` | `results/t11/ARM` (gitignored) | Results directory |
| `--rows N` | 200,000 | Passed to EXP-03's scenario builder |
| `--wal-keep-size` | `4GB` | `wal_keep_size` for the cluster that becomes the source; `none` gives EXP-03's settings (see below) |
| `--old-copy-wal-keep-size` | none; 4GB with `--arm retained` | `wal_keep_size` for the cluster whose data directory becomes B (see below). A size of 0, in any unit, is PostgreSQL's default and means the `default` arm, with no setting; `--arm retained` with 0, `--arm default` with a nonzero size, and a size PostgreSQL wouldn't read are errors |
| `--reuse-scenarios` | off | Reuse builds under `OUT/scenarios/` made at the same `--rows` and settings |
| `--keep-copies` | off | Keep each run's copies of B and of the source |
| `--tool-timeout`, `--catchup-timeout` | 1800 s, 120 s | Per tool command; for a refreshed standby to reach the source |

## The scenarios

[EXP-03's README](../tricky_copies/README.md#the-scenarios) describes how each is built.

| ID | B | Source | B's REDO location |
|---|---|---|---|
| S1 | A standby that stopped following the primary | The primary, after more writes | Same timeline as the source |
| S2 | The ex-primary, stopped cleanly after writing past the failover | The promoted standby R (timeline 2) | After the fork (its shutdown checkpoint) |
| S2b | The ex-primary, whose diverged pages were written out without a checkpoint, stopped with `-m immediate` | As S2 | Before the fork; B crashed |
| S2c | A hot standby Q of the ex-primary that replayed its diverged writes, wrote them out without a restartpoint, and stopped with `-m fast`. The ex-primary is then discarded | As S2 | Before the fork; B is "shut down in recovery" |

S2c exists because a tool may refuse S2b only for its crash, which says nothing about fork detection (EXP-08's revision). Its build fails unless B's `pg_controldata` state is "shut down in recovery" and its REDO and checkpoint locations lie before the fork.

**Exposed pages.** For S2, S2b and S2c, `scenario.json` → `observations` lists B's post-fork pages (`post_fork_pages`: every relation page of B whose on-disk LSN is after the fork in S's timeline history file). Each is marked with S's LSN at that block (`source_lsn`) and whether it is `exposed`: S holds that block with a page LSN at or below B's REDO location, and other bytes than B. A tool that sends only the pages newer than B's REDO location skips such a page, and the copy keeps B's diverged bytes. `exposed_pages` counts them, by kind and by file. The build check `exposed_pages` fails the build when the count is 0, because then the scenario could not show such a tool going wrong.
- In S2, B's REDO location is its shutdown checkpoint, after the fork. Every post-fork page whose block S didn't rewrite past that point is exposed. S2 keeps EXP-03's diverging writes.
- In S2b and S2c, B's REDO location is before the fork, so a page is exposed only if the source hasn't changed its block since then. To guarantee that by construction, as EXP-08 says, the diverging writes also update the `tags` table (`UPDATE tags SET v = upper(v) WHERE k = 3`), which `burst_sql` never writes. Burst 2's updates of `accounts` can then overlap any number of diverged `accounts` pages without removing the exposure.

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

   Every refusal also gets a **`refusal_class`** (EXP-08's classification):
   - `identity_or_history`: the tool's output holds one of the messages in `checks.IDENTITY_OR_HISTORY_PATTERNS`, which say B isn't on the source's system, timeline or history. `refusal_class_evidence` quotes the line. Only this class counts as "refuses" in EXP-08's I2 and I3.
   - `other`: any other refusal, e.g. a destination that wasn't shut down cleanly, or WAL that is gone.

   | Tool | Messages classed `identity_or_history` | What they detect |
   |---|---|---|
   | pg_rewind | `source and target clusters are from different systems` | System identifier |
   | | `could not find common ancestor of the source and target cluster's timelines` | Timeline history |
   | pg_probackup | `Database identifiers mismatch` | System identifier |
   | | `Destination is not in source timeline history`, `Source is behind destination in timeline history` | Timeline history |
   | | `Current START LSN ... is lower than SYNC LSN ...` ("instance from the past") | Divergence: B ahead of the source |
   | WAL-G `catchup-send` | `System identifiers do not match` | System identifier |
   | | `Destination is on timeline N, but we are on M` | Timeline |
   | | `Catchup destination is already ahead` | Divergence: B ahead of the source |

   WAL-G's `catchup-push` checks nothing, and pgdelta doesn't refuse a basis from another history (it stops reusing it), so their refusals are always `other`. Messages that mention an identifier or a history without reporting a mismatch are `other` too: pg_probackup's "Cannot get source timeline history" and WAL-G's "Our system lacks System Identifier".

   When the harness, not the tool, keeps a run from completing, the outcome is **harness_error** and the verdict `not_verified`: catchup-push succeeded but no backup name could be found to fetch, catchup-send never reached a listening catchup-receive, or the harness itself raised an exception before the run was classified. `run_t11.py` then exits 1.
4. **Check an accepted copy:**
   - **Diverged pages before recovery** (S2, S2b and S2c, where B's timeline forked from the source's). Right after the tool, each of B's post-fork pages (the list in `scenario.json`, or for an older S2 build computed the same way from S's timeline history file) is compared byte for byte with the copy, and with S at the same file and block. `diverged_before_recovery` → `diverged_left` counts pages where the copy still holds B's bytes and S holds other bytes, or no page. Each such page is also classified with EXP-03's `classify_pair` (content, hint bits, FSM, VM).
   - **The incumbents** are brought up as a standby of the source:
     1. pg_rewind writes its own `standby.signal` and `primary_conninfo` with `--write-recovery-conf`. For the catch-up tools, the harness writes both.
     2. The harness starts the copy and waits until `pg_last_wal_replay_lsn()` reaches the source's `pg_current_wal_lsn()`. It gives up early when the server log says the WAL is gone, the timeline doesn't fit, or recovery failed.
     3. While the copy is a hot standby, it compares the logical state with `expected_state.json`: every database, and for every permanent user table its row count and md5. A copy that errors on that read counts as a mismatch. A hot standby doesn't prune pages, so this read leaves B's pages as they are.
     4. It promotes the copy, runs `CHECKPOINT` and stops it with `-m fast`. B's post-fork pages are counted now (below).
     5. It starts the copy again and runs `pg_amcheck --all --heapallindexed --install-missing`, then stops it. amcheck runs after the promotion because it has to be installed, which a standby can't do.
   - **pgdelta's O** gets EXP-03's checks: byte comparison with S, `pg_verifybackup` and a restore to the end of recovery. The restored server is then checkpointed and stopped with `-m fast`, and B's post-fork pages are counted. It is started again for EXP-03's logical comparison, then pg_amcheck.
   - **Diverged pages after recovery.** At that clean stop (the incumbents' copy of B, or pgdelta's restored O), B's post-fork pages are compared with the copy again: `diverged_after_recovery`. The count comes before anything reads the copy's tables as a primary. Such a read prunes pages (`heap_page_prune_opt`), and pg_amcheck's heap scans are one: run first, they removed B's dead tuples from its stale `accounts` pages, which then counted as `other` (see [On the Mac](#on-the-mac)).

     Here pages are compared by their tuple data (`checks.tuple_data`): what is left once the page is masked the way the server's own `wal_consistency_checking` masks it, reusing EXP-03's `classify.mask_heap` and `mask_btree`. That zeroes `pd_lsn`, `pd_checksum`, `pd_prune_xid` and the free space, and clears `PD_PAGE_FULL` and `PD_HAS_FREE_LINES`; on heap pages it also clears the four hint bits of every tuple's `t_infomask` (xmin and xmax committed and invalid, which also covers a frozen xmin), and on btree leaves the line pointer flags (LP_DEAD), `BTP_HAS_GARBAGE`, `BTP_SPLIT_END` and the cycle id. `PD_ALL_VISIBLE` stays, since VACUUM WAL-logs it. FSM, VM and init-fork pages are compared without `pd_lsn` and `pd_checksum` only. A page counts as left when its tuple data still equals B's original diverged page and differs from S's. So hints, a checksum or a page LSN written since don't hide a stale page, and a page that later activity rewrote isn't counted as B's: it lands in `other`, which `summary.csv` reports as `other_after_recovery`. Such a page may still hold some of B's rows in another layout; the logical comparison is what catches those.
5. **Verdict**, from EXP-08's three checks: the logical match, pg_amcheck and, where B has post-fork pages, `diverged_after_recovery`.
   - `wrong_data` when any of them found a problem (logical mismatch, amcheck errors, or pages left after recovery).
   - `correct` when the copy caught up, matched, passed amcheck and, if B has post-fork pages, none is left after recovery. This is EXP-08's "Correct (repaired)".
   - `not_verified` otherwise, e.g. a copy that never came up or never stopped cleanly, so its pages weren't counted after recovery (`bring_up` → `after_recovery_error` says so), or one that couldn't be started again for pg_amcheck; and for a harness error.
   - Other runs that weren't accepted keep their outcome as the verdict.

### The tools

| `--tools` name | What runs | Notes |
|---|---|---|
| `pg_rewind` | `pg_rewind --target-pgdata COPY --source-server 'host=SOCKDIR port=P user=postgres dbname=postgres' --write-recovery-conf` | PostgreSQL 13 and later first run single-user crash recovery on a target that wasn't shut down cleanly (S2b), unless `--no-ensure-shutdown` |
| `pg_probackup` | `pg_probackup catchup -b DELTA --source-pgdata=SRC --destination-pgdata=COPY --stream -h SOCKDIR -p P -U postgres -d postgres --exclude-path=postgresql.conf --exclude-path=postgresql.auto.conf --exclude-path=pg_hba.conf --exclude-path=pg_ident.conf` | Its documentation's example for a lagging standby, which keeps the destination's configuration files |
| `walg` | `wal-g catchup-push SRC --from-lsn REDO` into file storage (`WALG_FILE_PREFIX` in the run's work directory), then `wal-g catchup-fetch COPY NAME` | REDO is B's latest checkpoint REDO location, passed as a decimal number: v3.0.9's `--from-lsn` is an unsigned integer and rejects the `X/Y` form, which only later versions accept. The probe records the flag's `--help` line in `tool_info` → `from_lsn_flag`. NAME is read from catchup-push's log, else from `catchup-list --json`. v3.0.9's catchup-push has no system-identifier option. The harness records both identifiers, which are equal in S1, S2, S2b and S2c, and passes one only if `catchup-push --help` lists such an option. EXP-08 reports this arm as "user-supplied LSN"; it doesn't count toward I2 or I3 |
| `walg_send` | `wal-g catchup-receive COPY PORT` in the background, then `wal-g catchup-send SRC 127.0.0.1:PORT` | Retries the send while the receiver isn't listening yet. In v3.0.9, catchup-send itself checks B's system identifier and timeline |
| `pgdelta` | EXP-03's tool step: `python -m pgdelta local --source S --basis B --output O --pg-controldata ...` | The reference |
| `lsn_skip` | EXP-03's LSN-skip shortcut, done in-process from the stopped source onto the copy of B: a source page whose LSN is below B's REDO location is skipped | **Negative control**, not an incumbent: it keeps B's exposed pages, so the checks must call it `wrong_data` on S2, S2b and S2c |

WAL-G runs with `PGHOST`, `PGPORT`, `PGUSER` and `PGDATABASE` pointing at the source copy's socket, and no other `PG*` variables.

## Choices that affect the results

- **The source keeps its WAL (`--wal-keep-size 4GB`).** The setting goes to the cluster that becomes the source, from its creation on: P in S1, R in S2, S2b and S2c. `scenario.json` → `server_conf` records the settings by role, and the cluster tests check them with `postgres -C`.
  - pg_rewind's copy replays the source's WAL from the last common checkpoint, which is before the fork. A copy refreshed by a catch-up tool replays from the catch-up's start. Under EXP-03's own settings, the source's checkpoints after the fork, and the segment switches of `pg_basebackup`, recycle that WAL. At 20,000 rows pg_rewind then repairs every page, but its copy can't start: the log says "requested WAL segment ... has already been removed". T11 gives the tools the source's WAL, as an archive or a replication slot would. `--wal-keep-size none` reproduces that case.
- **B's WAL: two arms (`--arm`).** pg_rewind also reads B's own WAL, from the last common checkpoint up to where B ends (its shutdown checkpoint, or for a standby its minimum recovery point), to find the pages B changed after the fork. EXP-08 runs every tool on both:
  - `default`: B's cluster runs with EXP-03's settings: R in S1 (it gets the default back after cloning P's configuration), P in S2 and S2b, Q in S2c. Whether B still has its WAL is then part of what T11 measures. The development run before EXP-08's registration found it gone at 200,000 rows on S2 and S2b: on S2, P's shutdown checkpoint removed it, and pg_rewind exits before touching the copy with "could not find previous WAL record" (`refused`, class `other`); on S2b, P's crash left it in place, but pg_rewind's own crash recovery ends with a checkpoint that removes it, and pg_rewind exits after that recovery changed the copy (`failed`).
  - `retained`: B's cluster gets `wal_keep_size` 4GB. This stands in for production's WAL archive with `pg_rewind --restore-target-wal`. EXP-08 judges I4 on this arm.
  - `result.json`, `summary.csv` and `summary.json` record the arm of each run (`arm`), derived from the build's settings: `retained` when B's `wal_keep_size` is above zero, `default` when it is absent or zero (PostgreSQL's default, in any unit). Use one `--out` per arm, as `--arm` does by default; a run from another build in the same OUT is listed in `runs_from_other_builds`.
  - Neither setting changes S or the LSN-skip numbers. B's settings change only which of B's WAL segments are still in `pg_wal`.
- **Diverged pages are counted twice.** Before recovery, byte for byte, to show what the tool itself left; after recovery, by tuple data, to show what the copy holds once it reached consistency the way its tool intends. Only the second counts for the verdict. A catch-up tool's copy can hold B's bytes right after the tool and lose them in replay (a full-page image of the source overwrites them), or keep them for good.
- **The after-recovery count comes before pg_amcheck**, so the three checks are independent: amcheck's heap scans can't prune B's stale pages before they are counted. pgdelta's logical comparison also moved after the count, since O is read as a primary. The incumbents' logical comparison stays where it was, on the hot standby, which doesn't prune.
- **amcheck runs after promotion**, on a copy that has already matched the source as a standby, started again after the count. `--heapallindexed` checks that every heap tuple is in its indexes. It doesn't check that every index entry points to a live heap tuple, so a copy can pass amcheck and still fail the logical read.
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
- `arm`, `returncode`, `seconds` (the tool's own commands only), `target_modified`, `outcome`, `refusal`, `refusal_class`, `refusal_class_evidence`, `outcome_reason` and, if any, `harness_error`;
- `parsed`, what the tool's output says, from `checks.parse_*`. For pg_rewind: the divergence point, the last common checkpoint, whether crash recovery ran, and whether no rewind was needed. For pg_probackup: `syncLSN`, the start LSN, the transfer ratio and the errors. For WAL-G: the backup name and the LSN used;
- `diverged_before_recovery` and `diverged_after_recovery` (counts by state, the pages left by kind and by cause, examples, and for the second when it was counted), `bring_up` (recovery configuration, LSNs reached, whether the copy was checkpointed, stopped cleanly and started again for pg_amcheck, errors, the server log's tail), `logical_match` and `logical_differences`, `amcheck` and `amcheck_ok`, `verdict`. pgdelta's runs also hold `pgdelta_checks` (byte comparison, `pg_verifybackup`).

`OUT/summary.csv` has one row per run under OUT, whichever invocation made it:
- `scenario, tool, outcome, logical_match, amcheck_ok, diverged_pages_left, seconds`. `diverged_pages_left` is the count after recovery, the one the verdict uses;
- `verdict, refusal, outcome_reason`, so a refusal for a stated reason, a tool that stopped with an error and a harness error read differently;
- `rows, source_conf, old_copy_conf`: the build that run used, from its own `result.json` (`source_conf` and `old_copy_conf` as `name=value`; empty for none, or for a result written before they were recorded);
- `arm, refusal_class, diverged_before_recovery`;
- `i3_judged_on`: for an S2b run refused with class `other`, the S2c run of the same tool and arm, with its verdict (e.g. `S2c (wrong_data)`, or `S2c (not run)`). EXP-08 judges that tool's I3 on S2c;
- `other_after_recovery`: the listed pages that after recovery hold neither B's nor S's tuple data (`diverged_after_recovery` → `other`).

`OUT/summary.json` adds each run's settings, tool version, refusal evidence, bring-up and notes, and `i3_judged_on` in full. `invocation`, `tools` and `scenarios` describe the invocation that wrote it: its `--rows`, settings and arm, the tool versions, and the scenario facts (B's state and REDO, checkpoint and minimum recovery locations, the timelines, the settings, the fork LSN, the exposed pages, where the post-fork page list came from). `runs_from_other_builds` lists the runs under OUT built at other rows or settings, and `run_t11.py` prints a warning for each. Use a separate `--out` per configuration to keep summaries apart.

## On the Mac

PostgreSQL 17.11 (Homebrew), 200,000 rows, `--wal-keep-size 4GB` for the source, `pg_rewind`, `pgdelta` and `lsn_skip` on S2, S2b and S2c, once per arm. These are development runs of the tooling, not EXP-08's registered run, which is on a Debian VM. They were made after the after-recovery count moved before pg_amcheck.

Each arm builds its own scenarios. The builds do the same things apart from B's `wal_keep_size`, but their LSNs differ by a few bytes between arms, as they do between runs. Where they differ, the table gives the default arm first, then the retained arm:

| | S2 | S2b | S2c |
|---|---|---|---|
| B's state | shut down | in production | shut down in recovery |
| Fork (S's history) | 0/8D10120; 0/8D100A0 | 0/8D12558 | 0/AD104C8; 0/AD104B0 |
| B's REDO | 0/9A1D948; 0/9A1D8C8 (after the fork) | 0/8D12488 | 0/AD103F8; 0/AD103E0 |
| B's checkpoint | as its REDO | 0/8D124E0 | 0/AD10450; 0/AD10438 |
| B's minimum recovery point | 0/0 | 0/0 | 0/BB7D0C8; 0/BB7D0B0 (Q replayed all of P's diverged WAL) |
| B's post-fork pages | 1,698 | 1,871 | 1,871 |
| Exposed | 1,685: 1,131 `accounts` heap pages, 547 `accounts_pkey`, 3 `accounts_bal`, 4 FSM and VM | 1,447: 928 `accounts` heap pages, 346 `accounts_pkey`, 2 `accounts_bal`, 167 `tags` heap pages, 4 FSM and VM | as S2b |

In S2b and S2c, every one of the 1,167 pages the diverging writes updated (1,000 of `accounts`, 167 of `tags`) was on B's disk with an LSN after the fork. P's flush (S2b) evicted 1,871 buffers and Q's (S2c) 3,151, each in two passes, and the build check `no_checkpoint_during_flush` passed: P's `pg_control_checkpoint()` stayed at 0/8D124E0, and Q's at its base backup's 0/9000080. Q made its only restartpoint at shutdown, at the last checkpoint it had replayed, from before the fork.

| Tool | Arm | S2 | S2b | S2c |
|---|---|---|---|---|
| pg_rewind | default | refused, class `other`: "could not find previous WAL record at 0/8D10120", before touching the copy | failed: "could not find previous WAL record at 0/8D12558", after its own crash recovery changed the copy | correct |
| pg_rewind | retained | correct | correct (after its crash recovery) | correct |
| pgdelta | both | correct | correct | correct |
| lsn_skip | both | wrong_data: 1,685 pages left before recovery and 1,685 after; reading `docs` fails on a missing TOAST chunk, and amcheck finds missing TOAST values | wrong_data: 1,447 left before recovery and 1,447 after; `accounts` has 199,072 rows of 200,000 and `tags` 19,833 of 20,000; amcheck finds a broken `accounts_bal` sibling link | as S2b |

- **pg_rewind on S2c works in the default arm.** Q's shutdown restartpoint keeps its WAL from its REDO location on, and that REDO belongs to the last common checkpoint (pg_rewind's "last common checkpoint" is B's checkpoint, 0/AD10450 in the default arm). pg_rewind read Q's WAL up to Q's minimum recovery point, after the diverged writes, and rewound every page.
- **No i3 pointer appears:** neither pg_rewind nor pgdelta refused S2b with class `other`. pg_rewind's default-arm S2b is a failure after it modified the copy, not a refusal. EXP-08's S2b-to-S2c rule applies to the catch-up tools.
- **The control's stale pages are all counted after recovery.** Every page it left before recovery still holds B's tuple data at the clean stop after promotion, and none is `other`. An earlier run counted after pg_amcheck instead, and got 685 on S2 and 519 on S2b and S2c: pg_amcheck's `--heapallindexed` heap scan, on the promoted copy, had pruned 1,000 (S2) and 928 (S2b, S2c) exposed `accounts` heap pages. A pruned page has lost the row B updated on it, and holds no tuple data of B's own, so it counted as `other`. In S2b and S2c, `accounts` is short by exactly those 928 rows, and `tags` by its 167 exposed pages' rows.
- **pgdelta's O** holds S's page at every listed block, before and after recovery. The 13 (S2) and 15 (S2b, S2c) listed pages it lacks are blocks past the end of S's files, which B's diverging writes had extended.
- **pg_rewind's copies** hold S's tuple data at every listed page after recovery but one: block 2 of `accounts`' FSM, which counts as `other` (an FSM page isn't WAL-logged). None holds B's.
- Each build took 3 to 4 s and each tool under 0.5 s; a whole arm took under a minute.

## Checked only on Linux so far

- Everything about pg_probackup and WAL-G: that the commands run as written, their refusal messages and refusal classes, the backup-name parsing, whether pg_probackup accepts a crashed B (S2b) and a standby stopped in recovery (S2c), and how their copies start.
- `install_incumbents.sh`, including its fallback that builds pg_probackup from source.

## Files

| File | Role |
|---|---|
| `run_t11.py` | CLI: builds the scenarios, runs the matrix, writes the summary |
| `tools.py` | Tool probing, the adapters, the standby and restore bring-up, `run_job` |
| `checks.py` | Outcome, refusal-class and verdict rules, output parsing, the diverged-page comparisons, summary files |

Tests: `tests/test_incumbents.py`. The unit tests cover classification, refusal classes (a table of real messages), parsing, both page comparisons on synthetic pages (hint bits and the page-level hints included), exposed pages, the LSN-skip control, S2b's and S2c's checks (a checkpoint during the flush included), the arms (a zero size included), the summary columns, the order of both bring-ups (the count before any read of a primary) and a copy that never stopped cleanly. Three cluster tests run `run_t11.py` at 2,000 rows: pg_rewind on S1 and S2; pg_rewind and the control on S2b with B keeping its WAL; pg_rewind, pgdelta and the control on S2c, whose build they check (state, REDO before the fork, post-fork pages on disk, exposed pages, no restartpoint during the flush). On S2b and S2c, the control's stale pages must all still be B's after recovery, none `other`; pgdelta's O must be checkpointed, stopped cleanly and hold none. They take about 20 s and are skipped without PostgreSQL 17 (`PGDELTA_TEST_PG_BIN`, default the Homebrew path).
