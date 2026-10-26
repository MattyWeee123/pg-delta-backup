# EXP-40: Does pgdelta work on AlloyDB Omni's data directories?

Status: **done October 26, 2026 (one VM, 4 of 5 predictions held): v1 refreshed AlloyDB Omni 17.10 data directories correctly, stopped (byte-identical) and live (Omni recovered B with the source's data). The fifth failed: pgdelta did not refuse Omni 16.14, because no code enforced D08's version scope. It does now (tested, and confirmed on the Omni 16.14 directory).** Pre-registered before any run. OMNI-1, lean: the first evidence on Omni itself (G5: "PostgreSQL results alone are insufficient"). The owner accepted the AlloyDB Omni container's license terms for this research use on October 26. One VM, about 30 minutes.

## Question

Every result so far is on community PostgreSQL 17. Omni is Google's PostgreSQL-compatible database, and its HA and DR rebuilds are what pgdelta targets. Four questions:
- What does Omni 17.10 set by default: `data_checksums`, `wal_log_hints`, `summarize_wal`, `full_page_writes`?
- What does its data directory hold beyond community PostgreSQL's?
- Does v1 refresh an Omni copy correctly, from a stopped and from a running Omni?
- Do pgdelta's version gates (D08: PostgreSQL 17 and 18 only) treat Omni 16.14 correctly?

## Design

`experiments/omni/omni_probe.sh` on one VM, with Docker and the public images `google/alloydbomni:17.10.0` and `:16.14.0`. Each data directory is mounted at its host path (`PGDATA`), and Omni's own `pg_controldata` is run from its image.
1. **Settings and layout** of a fresh Omni 17.10 cluster.
2. **A stopped source:**
   - `pgbench -i -s 20` makes B0, the old copy;
   - 5,000 pgbench transactions follow, and Omni is stopped (S);
   - v1 refreshes a copy of B0 in place (`pgdelta local --source-kind datadir --in-place`);
   - checked by byte identity with S over EXP-17's set of files, and by Omni starting B with S's data (`sum(abalance)` and the history count).
3. **A running source:** the same, with S running in Omni and v1 reading it in backup mode (`--source-kind live`). B must recover from its `backup_label` and start in Omni with S's data.
4. **Version gates:** v1 on an Omni 16.14 data directory.

## Predictions

1. Omni 17.10 reports PostgreSQL 17, `full_page_writes` on, `summarize_wal` off. `data_checksums` and `wal_log_hints` are recorded without a prediction.
2. **Deciding: v1 from a stopped Omni source makes B byte-identical to S, and Omni starts it with S's data.**
3. **v1 from a running Omni makes a B that Omni recovers and starts with S's data.**
4. pgdelta refuses the Omni 16.14 directory with a clear message, as D08 says.
5. Omni's data directory holds directories or files beyond community PostgreSQL 17's. They are listed, and pgdelta copies them like any other file.

## What follows

- **If 2 and 3 hold:** G5 moves from "assumed" to "probed": v1 runs on Omni 17 data directories, stopped and live, with Omni's binaries. D08's scope gains Omni 17. A sidecar must see PGDATA at the same path as the server, since live mode checks that the server's `data_directory` is the directory it reads. That goes into the deployment note.
- **If either fails:** what Omni does differently is found and reported. Omni stays unsupported until it is handled.
- **The settings** inform D02b (`pg_rewind` needs checksums or `wal_log_hints`) and native incremental's prerequisite (`summarize_wal`).

## Revisions

- **October 26, before any run (rule 8).** The script now matches the design:
  - it ran 20,000 transactions (`-t 5000` per client on 4 clients) against the design's 5,000, so it runs 1,250 per client;
  - it calls `psql` and `pgbench` by their PostgreSQL 17 paths;
  - it gives each container 1 GB of `/dev/shm`;
  - it lists a community PostgreSQL 17 `initdb` beside Omni's directory, so prediction 5 compares the two.

  A look at the image before the run found the community Docker image's conventions: the `docker-entrypoint.sh` entrypoint, `PGDATA` taken from the environment, stop on SIGINT (a fast shutdown), postgres as uid 999, and binaries in `/usr/lib/postgresql/17/bin`.

## Limitations

- Omni in a container on one VM, not Omni on Kubernetes (OMNI-2, which needs GKE).
- No columnar engine or memory-agent features in use, about 300 MB of data, one refresh of each kind.
- The license was accepted for research use only; nothing here is redistributed.

## Results

Data: `docs/experiments/data/exp40-verdicts.csv`; raw `results/omni/exp40/` (the probe's steps, both runs' statistics, the logs); analysis `analysis/exp40.py`, committed before the data. One `n2-standard-4` VM with a 50 GB balanced persistent disk, Docker 20.10, the images `google/alloydbomni:17.10.0` and `16.14.0`.

### Omni 17.10's defaults (the container, as shipped)

| Setting | Value | Matters for |
|---|---|---|
| `server_version` | 17.10 | D08: the same major as the tests so far |
| `full_page_writes` | on | live mode's torn-page repair by recovery |
| `data_checksums` | **off** | `pg_rewind` (needs checksums or `wal_log_hints`); every byte count so far had checksums on (Checksums-Omni) |
| `wal_log_hints` | **off** | `pg_rewind`, as above |
| `summarize_wal` | off | native incremental (`pg_basebackup --incremental` needs WAL summaries) |
| `wal_level` | replica | |
| `shared_preload_libraries` | `g_stats`, `google_columnar_engine`, `google_job_scheduler`, `google_ml_integration`, `google_storage` | Omni's own extensions, loaded by default |
| `block_size`, `wal_segment_size` | 8192, 16 MiB | the defaults pgdelta was tested with |

### Omni's data directory against community PostgreSQL 17's

A running Omni 17.10 cluster, just created, beside a fresh community `initdb` (two levels deep; 96 entries in common).

| In Omni only | What it is |
|---|---|
| `global/<oid>_mintxid` for 6 shared catalogs (1213, 1214, 1260, 1261, 1262, 2396) | **a relation fork community PostgreSQL doesn't have** |
| `global/9828`–`9831`, `global/9994`–`9997` | 8 shared relations below OID 10,000: Omni's own catalogs |
| `base/16741`, `base/18388` | 2 databases Omni's start-up created (OIDs above 16,384, so not `initdb`'s) |
| `log/*.internal`, `current_internal_logfiles` | Omni's internal server log |
| `telemetry/telemetry_config.json` | Omni's telemetry settings |
| `postmaster.pid`, `postmaster.opts`, `global/pg_internal.init`, WAL segments 3–5, 7 free-space-map forks of shared catalogs | a running cluster's files, and most likely its activity, not Omni's format (the community copy had never been started; it alone had `pg_stat/pgstat.stat` and WAL segment 1) |

pgdelta's relation pattern (`<relfilenode>[_fsm|_vm|_init][.<segment>]`) doesn't match `_mintxid`. So pgdelta treats those files as ordinary files: hashed and verified whole, and never classed as forks of an unlogged relation. EXP-17's set of files, by which B was compared with S, includes all of the Omni-only entries above except the running cluster's files.

### The refreshes

`pgbench -i -s 20` made B0 (the old copy); 5,000 transactions followed on S.

| | From a stopped Omni (`--source-kind datadir`) | From a running Omni (`--source-kind live`) |
|---|---|---|
| Exit | 0 | 0 |
| Time (pgdelta's own) | 10.56 s, 2 workers per side | 8.16 s, 1 source worker (live mode's default on 4 vCPUs, EXP-39); 7.0 s in backup mode |
| Pages sent | 11,046 of 52,285 (21%), 90.7 MB | 13,066 of 52,620 (25%), 107.3 MB |
| B against S, byte for byte | **identical**, 2,557 files of EXP-17's set | (not comparable: B recovers past S's backup point) |
| Omni starts B with S's data | **yes**: `sum(abalance)` 90,134 and 5,000 history rows, as S | **yes**, after recovery from its `backup_label` (3 recovery lines in its log), the same values |

### The version gate

Omni 16.14's directory, refreshed from itself, **was not refused**: pgdelta exited 0 after sending 1 of 10,917 pages. D08 says pgdelta takes PostgreSQL 17 and 18 and refuses everything else, but nothing checked the major version:
- live mode required PostgreSQL 15 or later, for `pg_backup_start`;
- a data directory or a finished backup wasn't checked at all.

The run was harmless here, since S and B were the same cluster. But D08's scope was a statement in the ledger, not a refusal in code.

| # | Prediction | Verdict |
|---|---|---|
| 1 | Omni 17.10 reports PostgreSQL 17, `full_page_writes` on, `summarize_wal` off | **Held**; recorded: `data_checksums` off, `wal_log_hints` off |
| 2 | **Deciding: v1 from a stopped Omni makes B byte-identical to S, and Omni starts it with S's data** | **Held** |
| 3 | **v1 from a running Omni makes a B that Omni recovers and starts with S's data** | **Held** |
| 4 | pgdelta refuses the Omni 16.14 directory with a clear message (D08) | **Failed:** it refreshed it |
| 5 | Omni's directory holds entries beyond community PostgreSQL 17's, copied like any other file | **Held:** a `_mintxid` fork, 8 shared catalogs, 2 databases, an internal log and telemetry, all in the byte identity |

## What follows

- **G5 moves from "assumed" to "probed" for Omni 17:** v1 refreshed Omni 17.10 data directories with Omni's own binaries, stopped and live, and Omni started each B with the source's data. D08's scope gains Omni 17. This is one refresh of each kind, in a container, not Omni on Kubernetes (OMNI-2).
- **The fix for prediction 4, made the same day:** pgdelta now refuses an S, a live server or an in-place B whose major version isn't 17 or 18 (by `PG_VERSION`, and by `server_version_num` in live mode), exit 2, B untouched (`pgdelta/identity.py`'s `check_major`, with tests). A directory without `PG_VERSION` is still copied as any tree, as v0 always did. Rerun on the VM, the Omni 16.14 directory was refused from both ends (`source /data/omni/s16 is PostgreSQL '16' by its PG_VERSION: pgdelta supports PostgreSQL 17 and 18 only (D08)`), and the Omni 17.10 directory still refreshed (exit 0).
- **The `_mintxid` fork is open in two ways,** since pgdelta doesn't know what it is:
  - **live mode** relies on recovery to repair a page read while it was being written. That works only for files Omni writes through the WAL with full-page images. One live refresh with B starting correctly doesn't show that the fork is among them; a live campaign on Omni (as EXP-25 on PostgreSQL) would.
  - **an unlogged relation's `_mintxid` fork,** if Omni makes one, would be copied from a stopped S rather than left out or reset like the other forks.

  Neither touched these runs: the forks seen belong to shared catalogs, which are logged.
- **Omni's container defaults leave `pg_rewind` out:** with checksums and `wal_log_hints` both off, `pg_rewind` refuses to run, so after a failover the old primary can't be rewound unless the deployment turned one of them on. That widens D02b's case for pgdelta on Omni as shipped. Native incremental needs `summarize_wal` turned on first, and has summaries only from then on.
- **Checksums-Omni's registered follow-up is due:** every byte count so far had checksums on, and Omni's container has them off. FIRMNESS.md registered byte counts with checksums off (T4) for this case.
- **The deployment note gains one line:** a sidecar must see `PGDATA` at the same path as the server. Live mode checks that the server's `data_directory` is the directory it reads (by device and inode), and the probe met that by mounting each data directory at its host path.

## Deviations

None from the revised design. The gate's recheck on the VM was added after the results, to confirm the fix.
