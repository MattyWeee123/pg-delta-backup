# EXP-40: Does pgdelta work on AlloyDB Omni's data directories?

Status: **pre-registered October 26, 2026, before any run.** OMNI-1, lean: the first evidence on Omni itself (G5: "PostgreSQL results alone are insufficient"). The owner accepted the AlloyDB Omni container's license terms for this research use on October 26. One VM, about 30 minutes.

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
