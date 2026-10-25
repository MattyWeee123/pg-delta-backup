# Team plan

Proposed October 22, 2026, for the team to confirm at the October 23 sync.
Code freeze is about November 13; the sponsor demo is late November or the first week of December.

*Revised October 25, 2026: the code freeze moves to **November 30**, and the presentation is in the first week of December (the owner's decision K9 in the [ledger](DECISIONS.md#decisions-of-october-24-2026-night-after-the-rule-11-audit)). The [schedule to the presentation](#schedule-to-the-presentation-october-25-2026) replaces the week-by-week schedule below.*
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

## Schedule to the presentation (October 25, 2026)

This order follows the [rule 11 audit's](FIRMNESS.md#43-what-runs-at-once) waves, with the owner's decisions of October 24 and 25: experiments sized to what firm results need, two sessions per test on different days, no vCPU cap, a compiled hash allowed, and a freeze on November 30. It is a ranking by judgment, so under rule 11 it is provisional, and it moves as results come in. Every wave fits the project's 32 vCPUs (the owner chose to work within the quota): EXP-13's pair beside EXP-14's uses 32, and T10b's three VMs beside T5's extra one use 28.

| Dates | Experiments (VMs) | Building (no VMs) |
|---|---|---|
| Oct 25–31 | Wave 1, session 1: EXP-13 revised beside EXP-14 (the HA re-test on persistent disks across zones) | Multi-pair infrastructure; EXP-13's revision, EXP-14 and EXP-15 pre-registered and reviewed; their harness; T8's design and build start; T4 on the Mac |
| Nov 1–7 | Wave 1, session 2, on another day; T13c (the in-place crash test on Linux) beside OMNI-1 (an Omni container probe); EXP-15 (the real cross-region link) | OMNI-2 on GKE; T10b's harness, with its client VM, and T5's arms |
| Nov 8–14 | OMNI-2; T13c's second session; T10b with T5 inside it, two sessions | T8's build |
| Nov 15–23 | T8's two sessions (v1 itself on a live source, the client on its own VM); T10b's v1 arm; v1 against native incremental | Fixes from T8 |
| Nov 24–30 | T9 at scale; T11b; T13's fault classes; any second sessions still owed | Final charts and write-ups; **freeze November 30** |
| Dec 1–5 | – | Presentation; fixes only |

Anything not firm by November 30 is presented as provisional, with the experiment that would make it firm.

## Interfaces to agree at the sync

1. **Backup method contract:** inputs (old copy, source connection, output folder) and the v3 result record, registered in `benchmarks/methods.py`. Every method uses it.
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
