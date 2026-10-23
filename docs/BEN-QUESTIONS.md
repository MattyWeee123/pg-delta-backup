# Questions for Ben and Sumaithri

Updated October 23, 2026. Earlier versions are archived: [September 30](archive/2026-09-30-ben-questions.md) and [October 22](archive/2026-10-22-ben-questions.md).

**No questions are blocking.** On October 23, Matt settled the open questions himself, from the project brief and Google's public documentation. The decisions and the evidence behind each are in [DECISIONS.md](DECISIONS.md#decisions-of-october-23-2026-t1-closed). What Omni does today is in the [AlloyDB Omni research notes](research/notes/alloydb-omni.md).

## Already answered in the chats

- Benchmark while a database workload is active.
- Compare time, network traffic and performance degradation against `pg_basebackup`.
- Users must see no data loss; merely starting PostgreSQL is not enough.
- Byte identity with a separate `pg_basebackup` capture is not required.
- A working proof of concept on PostgreSQL is enough; any language is fine (C only for an upstream contribution).
- Most real-world backups aren't taken with `pg_basebackup` (Ben, October 2).

## Decided on October 23

| Question | Decision |
|---|---|
| Which situation do we target? | AlloyDB Omni's two full re-copies: rebuilding a same-region HA standby, and re-seeding a cross-region DR secondary. We define small, medium and large sizes ourselves. |
| What if there's no old copy? | Out of scope: use `pg_basebackup`. Whether Omni keeps an old disk is the integrator's choice. |
| May the tool run a program on the source? | Yes. Sending only changed chunks requires it. How Omni packages it is the integrator's choice. |
| How much may it slow the live database? | No more than `pg_basebackup` does under the same load. Experiment T10a measures it. |
| Must the old copy survive a failed refresh? | No. Omni's own rebuild paths don't keep it either. PostgreSQL must refuse to start on a half-refreshed copy, and the fallback is a rerun or a full copy. |
| Encrypt the data stream? | Yes (TLS). |
| May we use rsync or other GPL code? | No. pgdelta is to be Apache-2.0; rsync remains a baseline only. |
| Where does the code go? | Not our concern: we build the tool, and Google integrates it if they want. |

## Our assumptions (tell us if any are wrong)

1. A standalone tool, not a PostgreSQL patch.
2. A proof of concept on PostgreSQL 17 or 18 carries over to Omni, which stores ordinary PostgreSQL files.
3. "No data loss" means the refreshed copy passes `pg_verifybackup`, recovers, and its data matches the source at the backup's end point.
4. Where PostgreSQL's own incremental backup applies, we hand off to it. Our tool targets old copies it can't use.
5. We build our own chunking and transfer engine and benchmark against `pg_basebackup`, native incremental, rsync, `pg_rewind`, `pg_probackup catchup`, WAL-G catchup and pgBackRest delta restore.

## Nice to know (not blocking)

- **AlloyDB team:** when the operator deletes an instance after a failover or an auto-heal, does it keep the instance's disk? That decides how often an old copy exists for the HA case.
- **AlloyDB team:** under transparent data encryption, do standbys share the primary's data keys? An old copy is only reusable if they do.
- **AlloyDB team:** do Omni clusters enable data checksums or `wal_log_hints` by default? `pg_rewind` needs one of them.
- **Sumaithri:** is there a formal mid-semester deliverable and date, or is the late-November / early-December demo the main milestone?
