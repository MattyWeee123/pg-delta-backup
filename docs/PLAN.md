# Team plan

Proposed October 22, 2026, for the team to confirm at the October 23 sync.
Code freeze is about November 13; the sponsor demo is late November or the first week of December.
The [September 30 plan](archive/2026-09-30-plan.md) is archived.

## Where we are

- **Measurement harness: built and merged.** Backup and restore checks, CPU and disk sampling, and the HammerDB slowdown driver. First pilot: a running backup cut throughput 57–71% (one laptop run, not a baseline).
- **The tool: not started.** No sender, receiver or rebuild yet, and `network_bytes` is null in every record.
- **Research: done.** No existing tool refreshes *any* older copy straight from a live server while verifying reused data by hash. See [the research summary](research/state-of-the-art.md).

## What we're building

Our niche: refresh an older physical copy whose history is lost (a standby whose WAL was recycled, an ex-primary after failover, a restored snapshot), verifying every reused page by hash.

1. **v0 (staged):** take a normal `pg_basebackup` on the source, compare it with the old copy page by page using a hash tree, send only differing pages, rebuild into a new folder, verify with the fresh backup's manifest and a test restore.
2. **v1 (direct read):** read the live data directory inside `pg_backup_start` / `pg_backup_stop` instead of staging a full copy, streaming WAL alongside, as described in [SPEC.md](SPEC.md).
3. **Benchmark:** two GCP machines with a throttled link, against full `pg_basebackup`, native incremental, rsync, `pg_probackup catchup` and WAL-G catchup.

## Workstreams

Owners are suggestions to confirm at the sync.

| Workstream | Suggested owner | Deliverable | Issues |
|---|---|---|---|
| The tool | Matt | v0 local rebuild added as a backup method; v1 over the network | #7, #8, #9 |
| Benchmark environment and baselines | Sam | Two GCP machines, throttled link, `network_bytes` for every method, full and incremental baselines at about 1, 10 and 50 GB with 3 repeats, HammerDB runs on those machines | #10 |
| Competitors and correctness | Etash | rsync baseline between two finished backups, `pg_probackup catchup` and WAL-G catchup baselines, and the missing active-load correctness check | #10 |
| Change patterns and tricky copies | Uttansh and Badri | Change-pattern study (below), plus a library of tricky old copies the tool must handle or refuse | #6, #11 |

## Dependencies

- **First, make backup methods pluggable (1–2 days).** The method list is hard-coded in four places (`benchmarks/backup.py`, `benchmark.py`, `native_baseline.py`, `hammerdb_experiment.py`). Until it isn't, the tool and the rsync method collide.
- **Sam's two-machine setup is on the critical path** for v1's network numbers and for the final runs. Until then, test between two Docker containers.
- Everything else starts now and joins at the final runs.

## Experiments that gate the plan

- **Change-pattern study (this week).** Updating 1% of rows at random can change about 63% of pages, so the tool's benefit depends on *where* changes land. Take a backup, run a workload for a set interval, then take a PG17 incremental backup: its size is the changed-page count. Repeat for random updates, appends and hot-row workloads. If most pages change daily, the tool's benefit shrinks, and we need to know that now.
- **rsync gate (end of week 1).** If rsync between two finished backups already sends about as little as we'd hope, ask Ben whether an rsync-based engine is acceptable before building our own.

## Schedule

| Week | Tool | Environment | Competitors and correctness | Change patterns and copies |
|---|---|---|---|---|
| Oct 21–27 | Pluggable methods; v0 local rebuild with bytes-saved numbers | Two machines, throttled link, network counting | rsync baseline | Change-pattern study, first results |
| Oct 28–Nov 3 | v1 over the network | Baselines at 1 and 10 GB | Catchup baselines; active-load correctness check | Tricky-copies library v1 |
| Nov 4–10 | Run against tricky copies; fix | Full comparison runs, 3 repeats | Correctness on all methods | Edge-case tests; answer to Ben's LSN question |
| Nov 11–13 | Freeze, docs | Final chart: refresh time vs. link speed | Final checks | Write-up |

## Interfaces to agree at the sync

1. **Backup method contract:** inputs (old copy, source connection, output folder) and the v2 result record. Every method uses it.
2. **Network bytes:** Sam's measurement fills `network_bytes` for every method, so no method reports its own traffic unchecked.
3. **Tricky-copy format:** a folder plus the expected outcome ("must reuse", "must refuse", "must fall back to a full copy").

## If an assumption breaks

| Trigger | Response |
|---|---|
| Ben disallows scratch disk on the source | Skip v0 staging; go straight to v1 direct read |
| Ben disallows running anything on the source | A client-only design can't save network bytes; reassess with Ben |
| Most pages change between refreshes | Report it as a finding; focus the demo on the workloads where the tool wins |
| The network is fast enough that a full copy wins | Show the crossover point honestly; that is still a useful result |
| rsync is close to our target | Ask Ben whether an rsync-based engine is acceptable |
| Omni is unavailable | Continue on PostgreSQL and mark Omni untested |

## Weekly rhythm

- Before the sync, each owner links an artifact on their issue.
- At the sync, run the integrated demo first, then decide blockers.
- PRs include validation and limitations; one teammate reviews before merge.
- A blocker that lasts a working day gets escalated with a concrete question.
