# Questions for Ben and Sumaithri

*Archived October 23, 2026: superseded by the [current version](../BEN-QUESTIONS.md). None of these questions was sent.*

Updated October 22, 2026. The [September 30 version](2026-09-30-ben-questions.md) is archived.

Ben asked the team to define the plan and only ask when needed, so we state our assumptions and ask only questions whose answers would change what we build.

## Already answered in the chats

- Benchmark while a database workload is active.
- Compare time, network traffic and performance degradation against `pg_basebackup`.
- Users must see no data loss; merely starting PostgreSQL is not enough.
- Byte identity with a separate `pg_basebackup` capture is not required.
- A working proof of concept on PostgreSQL is enough; any language is fine (C only for an upstream contribution).
- Most real-world backups aren't taken with `pg_basebackup` (Ben, October 2).

## Our assumptions (tell us if any are wrong)

1. A standalone tool, not a PostgreSQL patch.
2. PostgreSQL 17 or 18 for the proof of concept; AlloyDB Omni if time allows.
3. "No data loss" means the restored copy passes `pg_verifybackup`, starts, and its data matches the source at the backup's end point.
4. Where PostgreSQL's own incremental backup applies, we hand off to it; our tool targets old copies it can't use.
5. We build our own chunking and transfer engine and benchmark rsync, `pg_probackup catchup` and WAL-G catchup as baselines.

## Ask Ben

1. **Which real situation prompted this project?** Replicas that fell behind, old primaries after failover, copies from disk snapshots or other backup tools, or something else? Roughly how large are the databases, and how fast is the link between source and destination?
2. **May the tool run a program on the source machine, and is temporary disk space there equal to the database size acceptable?** v0 takes a normal backup locally on the source, then sends only the differences. If scratch space isn't allowed, we go straight to reading the live data directory in backup mode.

## Ask the AlloyDB team (from the research)

1. Does Omni's data directory contain files or page formats beyond vanilla PostgreSQL?
2. Could the Omni operator keep a failed primary's volume for our tool to refresh, instead of deleting it? That would be the most compelling demo.
3. How does Omni's encryption treat page contents? Hashing raw bytes should still work, but we'd like to confirm.

## Ask Sumaithri

- Is there a formal mid-semester deliverable and date, or is the late-November / early-December demo the main milestone?

## Draft post for the Chat space

Not sent yet. Review with the team first.

> Hi Ben and Sumaithri, here's where the delta backup project stands and our plan.
>
> **Where we are:** the measurement setup is built and merged: backup and restore correctness checks, CPU and disk usage, and live-database slowdown under a HammerDB workload. First result: running a backup cut throughput by roughly 57–71% (a single laptop run; repeats on GCP next). We also surveyed existing tools: none refreshes an arbitrary older copy directly from a live server while verifying reused data by hash, which is the niche we're targeting. The tool itself starts this week.
>
> **Plan to code freeze (around November 13):**
> - Week of October 26: first working version on one machine. Rebuild a new backup from an old copy plus only the changed 8 KiB pages; report bytes saved at different change rates.
> - Week of November 2: run between two GCP machines over a throttled link; compare bytes and time against full `pg_basebackup`, native incremental, rsync and the existing catchup tools.
> - Week of November 9: tricky cases (wrong database, a copy that diverged after failover, shrunken tables), correctness under active load, final charts.
> - Demo: refresh time vs. link speed, and bytes sent, at about 1, 10 and 50 GB.
>
> **Our assumptions (tell us if any are wrong):** standalone tool; PostgreSQL 17/18 for the proof of concept, Omni if time allows; "no data loss" means the restored copy passes `pg_verifybackup`, starts, and matches the source at the backup's end point; when native incremental applies we hand off to it; we build our own chunking and transfer engine and benchmark rsync and the catchup tools as baselines.
>
> **Two questions for Ben:**
> 1. Which real situation prompted this: replicas that fell behind, old primaries after failover, copies from disk snapshots or other backup tools, or something else? Roughly how large are the databases, and how fast is the link between source and destination?
> 2. May our tool run a program on the source machine, and is temporary disk space there equal to the database size acceptable?
>
> **Sumaithri:** is there a formal mid-semester deliverable and date, or is the late-November / early-December demo the main milestone?
