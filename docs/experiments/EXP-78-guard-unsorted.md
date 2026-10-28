# EXP-78: Does the guard's summary reading get faster without the server's sort?

Status: **pre-registered October 28, 2026, before any run.** Local, minutes. It follows EXP-77, after which the guard is the fast path's largest fixed cost where nearly every page changed: 5.3 s of the 7.2 s left against `pg_basebackup`, for 4.6 M summary rows.

## Question

Since EXP-73b the sender reads the chosen WAL summaries in one query, grouped per relation fork on the server (`live.summary_blocks`). Each fork's blocks come back as `array_agg(DISTINCT relblocknumber ORDER BY relblocknumber)`, and the sender turns them into Python sets (`fastpath.grouped_plan`), which neither need order nor keep duplicates.

**A pilot, not scored** (EXP-73b's matched scale model at scale 320: 22 summaries, 791,869 summary rows, 329,719 distinct blocks; 4 runs each), timed the query in parts:
- **the server's grouping without the arrays:** 0.09–0.12 s;
- **with the sorted, distinct arrays:** 0.39–0.51 s;
- **with the JSON as well:** about the same (0.38–0.45 s);
- **psql's transfer and `json.loads`:** 0.01 s.

So the sort and de-duplication is most of B's time, not the JSON or the transfer.
- **The same query with `array_agg(relblocknumber)`, unsorted and with duplicates (D):** 0.22–0.26 s against B's 0.43–0.46 s, with identical plans.
- **Split over 4 connections by summary and merged (D4):** 0.14–0.21 s.
- **The pilot also found a bug in its own first merge:** the server returns OIDs as JSON strings, which `grouped_plan` converts with `int()`.

**The question: on a fresh scale model, does D take at most 0.65 of B's time, with the same plan in every run?**

## Design

- **The scale model:** EXP-73b's (`guard_reading.build`): pgbench at scale 320, 1% of rows updated in 20 checkpointed batches, the lab's state check, then a checkpoint. A new seed (780), so the rows differ from the pilot's.
- **The cluster:** PostgreSQL 17.11, local (macOS, `LC_ALL=C`), data checksums, `summarize_wal` on.
- **Four readings per repeat, over the summaries from the start on:**
  - B (today's);
  - D (unsorted, duplicates kept, one connection);
  - D4 (D split over 4 connections by summary, merged per fork);
  - the server's grouping alone (a count, for scale).
- **Runs:** one warm-up of each, then 5 repeats, each in a rotated order.
- **The script:** `experiments/fastpath_fixed_costs/guard_unsorted.py`, smoke-run at scale 20 (2 repeats, deciding nothing; D/B 0.49, plans equal; its outputs removed). Data `docs/experiments/data/exp78-guard.csv`, figure `exp78-guard`.

## Predictions

1. **Deciding: D's median takes at most 0.65 of B's,** and D's plan equals B's in every run. The pilot gave 0.52–0.57.
2. **D's plan equals B's in every repeat** (also part of 1; scored on its own so a plan mismatch is visible).

**Reported, not predicted:** D4 against B; B's time per summary row against the VMs' 1.15 µs.

**Rule 11:** a ratio within 0.05 of the bar (0.60–0.70) gets a second session, on another seed.

## What follows

- **If 1 holds:** `live.summary_blocks` returns the blocks unsorted with their duplicates, and `grouped_plan` de-duplicates them as it already does. The real-server test that compares the grouped plan with the plan of every summary row stays as the check. The next VM session times the guard. D4's split is weighed then, against its complexity: more connections to the source during backup mode.
- **If 1 fails:** the server's sort isn't where B's time goes on a fresh model, and the pilot's split is re-measured before anything changes.
