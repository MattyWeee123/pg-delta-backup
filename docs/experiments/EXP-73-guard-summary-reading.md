# EXP-73: Where does the fast path's guard spend its time, and does reading the summaries grouped on the server cut it?

Status: **pre-registered October 28, 2026, before any run.** Local, on the Mac (PostgreSQL 17.11), about 20 minutes. Research loop 1 names the fast path's fixed costs next. At EXP-71's scattered cell the guard took 5.4 s of the session's 46.8 s while reading 1.47 million summary rows. At EXP-70's it took 4.7 s, a tenth of the run.

## Question

**Today, per summary the guard overlaps** (`sender._decide_fast_path`):
- it asks `pg_wal_summary_contents()` through the session's psql;
- the server builds one JSON array per row (tablespace, database, relfilenode, fork, block, limit flag) and aggregates them with `json_agg`;
- psql prints the result as one line, and Python parses it and turns each row into a tuple;
- `fastpath.block_plan` then adds every block to a per-relation set.

A block changed in several checkpoint intervals appears once per summary. At EXP-71's scattered cell, 1.47 million rows named about 600,000 distinct pages.

**The alternative (B):** one query over all the chosen summaries, grouped on the server per relation fork. It returns each fork's distinct blocks as one array and its lowest limit block, so the server sorts and deduplicates, and psql prints and Python parses about one integer per distinct block.

**Where does the time go today, and does B cut it with an identical plan?**

## Design

**Scale models,** each a fresh local cluster (`initdb --data-checksums`, `summarize_wal = on`), loaded with `pgbench -i`, then a `CHECKPOINT`; that LSN is the start. The lag is EXP-64's uniform pattern, scaled: 1% of `pgbench_accounts`' rows updated in random order, in 5 batches with a `CHECKPOINT` after each, so a block appears in several summaries as it does on the VMs.
- **Two sizes:** scale 20 and scale 80 (about 0.3 and 1.2 GB), to see how each part scales with rows.
- **Ranges:** the summaries from the start through the WAL summarizer's latest, after `pg_switch_wal()` and waiting for the summarizer.

**Methods, each run 3 times per size, alternated, after one warm-up run each:**
- **A, today's:** `live.Session.value` per summary, the same SQL as `live.summary_rows`, then `fastpath.block_plan`. It is timed in parts:
  - the server alone: the same query with its result discarded (`SELECT length(...)`);
  - psql's transfer and Python's `json.loads`;
  - the tuples and `block_plan`.
- **B:** one query through the same `Session`, grouping per relation fork:
  - `array_agg(DISTINCT relblocknumber ORDER BY relblocknumber) FILTER (WHERE NOT is_limit_block)`;
  - `min(relblocknumber) FILTER (WHERE is_limit_block)`;
  - then the same plan structure built from those arrays.

**Every run checks that B's plan equals A's:** every relation fork's block set and limit, and the same forks. A plan that differs fails the run.

The script is `experiments/fastpath_fixed_costs/guard_reading.py`, which writes `docs/experiments/data/exp73-guard.csv` and the figure `exp73-guard`.

## Predictions

**The model:** about 60 bytes of JSON per row, and a few microseconds per row in each of json_agg, json.loads and the tuples and sets. That gives several seconds per million rows, as the VMs show (5.4 s at 1.47 M). B moves about 8 bytes per distinct block and does one sort on the server.

1. **Deciding: at scale 80, B takes at most 0.4 of A's time** (median of 3), with identical plans. The model gives about 0.2.
2. **B's plan equals A's in every run,** at both sizes.
3. **A's time grows with rows:** its time per summary row at scale 80 is within 30% of scale 20's. A fixed part much larger than the per-row part would show up as a much higher per-row cost at the small size.
4. **No single part is over 70% of A's time** at scale 80. The cost is spread over the server, the transfer and Python, so only removing rows helps, not tuning one step.

**Reported, not predicted:** rows, distinct blocks and relation forks per size; each part's seconds; and the projection to EXP-71's 1.47 M rows, from the per-row costs.

## What follows

- **If 1 and 2 hold:** B replaces A in the sender (`live.summary_blocks`). Tests check that it builds the same plan as A on the tricky copies and the real failover. The next VM session times it on EXP-71's or EXP-70's scattered cell.
- **If 1 fails:** the parts say what to cut next, for example the guard's other steps.
- **If 2 fails:** B is wrong and is not built. The differing forks are reported.
