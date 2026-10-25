# EXP-27: Were EXP-25's missed controls correct copies, or a gap in the checks?

Status: **pre-registered October 25, 2026, before any counted run** (a 3-trial smoke checked the harness; see below). Local (the Mac, PostgreSQL 17.11), about 25 minutes. The question [EXP-25](EXP-25-live-correctness.md) left: its `during_start` negative control was missed in 5 of 55 trials, and nothing in their records told a correct copy from a wrong one the checks failed to see.

## Question

When pgdelta's broken `during_start` variant (it reads the files while `pg_backup_start`'s spread checkpoint is still writing) makes a copy that EXP-25's checks pass, is that copy actually correct? And does every copy it makes wrong get caught?

## Why a copy from the broken variant can be correct

A page read stale is one the load changed before the backup's redo point and the checkpoint hadn't written yet. Recovery replays the WAL only from the redo point on. With `full_page_writes` on, the first change to a page after the redo point puts the whole page into the WAL, and replay restores it whatever was read. A stale page stays wrong only if nothing changes it again before the stop LSN. Under EXP-25's load most pages change again, so some of the variant's copies may contain no unrepaired stale page, and be correct.

## The oracle

Built for this, test-only and honoured only with a broken variant on: `PGDELTA_TEST_ORACLE_DIR`. pgdelta also reads the same files again as soon as `pg_backup_start` returns, a correct read, into that directory. Each trial then yields two copies of one backup, with the same WAL and the same `backup_label`:

- **B**, what the variant sent;
- **B2**, B with those files replaced by the correct read: the copy a correct pgdelta would have made from this backup.

Both are recovered side by side (autovacuum off) and shut down cleanly. Then:

- **Physical comparison.** Every relation page's LSN (main and visibility-map forks; the free-space map is advisory and not WAL-logged), and every byte of `pg_xact`, `pg_multixact` and `pg_filenode.map`. A page whose LSN differs holds a WAL-logged change one copy lacks. Hint bits never cause that, since they aren't WAL-logged here (data checksums off, `wal_log_hints` off).
- **When they differ,** the stale pages are named by relation (from B2's catalog), and both copies are compared logically: every table's rows in the bank database, the schema and the databases.
- **B is correct if it is physically identical to B2.** It is wrong if anything differs, or if it can't recover where B2 can.

## Design

`experiments/correctness/oracle_campaign.py`: EXP-25's harness and load, every trial its `during_start` control, with seeds 2000–2039 (the trial number seeds the DDL), plus a normal refresh after each trial, as in the campaign. Each trial records:
- EXP-25's verdict on B (caught or missed);
- EXP-25's checks on B2;
- the oracle's comparison.

40 trials, about 35 s each.

## Predictions

1. **The oracle's own control:** B2 passes every EXP-25 check in all 40 trials.
2. **Deciding:** every missed control's B is physically identical to B2. The misses are correct copies.
3. Every B the oracle finds wrong (stale, or unable to recover) is caught by EXP-25's checks.

## What follows

- **If 2 and 3 hold:** the checks caught every wrong copy the variant made. EXP-25's shortfall from 100% is the variant sometimes making correct copies, and the bound's premise (checks that catch a wrong copy) is supported for this kind of fault. The share of the variant's copies that are wrong is reported.
- **If a missed B is stale:** the checks have a gap. Which relations were stale (table, index, catalog, visibility map) says what check would close it, and EXP-25's claim is qualified until that check exists and is shown to catch it.
- **If 1 fails:** the oracle isn't a correct copy, and nothing is concluded.

## Limitations

- **One kind of fault:** stale pages from reading during the start checkpoint. It says how sensitive the checks are to that kind, not to others.
- **A stale page that changes no row** (say, a missing page prune) makes B wrong here. That is a stricter standard than the bank checks, which judge content.
- **About 3–4 misses expected** at EXP-25's rate (9%). Prediction 2 rests on those.

## Smoke (October 25, before the run; not counted)

Three trials (seeds 9000–9002), after four harness fixes:
- copying the oracle's files gave B2's root folder mode 0755, which PostgreSQL refuses;
- the relation-mapping query needed a `::text` cast;
- a B that can't recover now counts as wrong;
- the stale pages are named by relation.

All three were caught, and B2 passed every check each time. The oracle found B stale in two (2 and 21 pages, in tables, indexes and catalogs; queries on it then failed) and unable to recover in one ("WAL contains references to invalid pages").
