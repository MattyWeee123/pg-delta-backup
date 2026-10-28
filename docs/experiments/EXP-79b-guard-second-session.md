# EXP-79b: The guard on the VMs, rule 11's second session, in EXP-75–77's order

Status: **pre-registered October 28, 2026, before any run.** One `ha` VM pair, about an hour with the data load. It is EXP-79's second session under rule 11, and it re-runs the cell EXP-79 changed.

## Question

**EXP-79's deciding prediction held within rule 11's band:** scatterall12's guard took 0.567 of its 5.31 s (bar 0.60). The guard read 0.64–0.69 µs per summary row in every cell, against 1.15–1.30 µs before.

**Its predictions 3 and 4 failed for reasons of design:**
- scatterall12 ran after scatter12, which had already updated the same rows, so the plan chose 84% of the pages, not EXP-77's 96%;
- background writes on the source (5.86 and 2.01 GB, most likely autovacuum) slowed two of its four runs;
- scatter12's baseline row count came from a session with another history.

**This session runs EXP-79's design in EXP-75–77's order:** uniform, then scatterall12. There the source wrote 0.03–0.14 GB per run in EXP-77. It has no scatter12.

**The question: does scatterall12's guard again take at most 0.60 of 5.31 s, and does the fast path on EXP-77's cell gain what the guard saves?**

## Design

**EXP-79's, but with two cells, in this order:**
- **`uniform`:** arm `fast8wf2`.
- **`scatterall12`:** arms `fast8w` and `full`.

The `ha` set in us-west1, EXP-48's 18 GB database, PostgreSQL 17.11 and pgdelta at this commit, the same as EXP-79's; `SERIES=1`.

**Runs:** 2 per arm, shuffled per cell (seed 792); 6 runs. **Smoke** at scale 20 (seed 793), deciding nothing.

**The command:**

    PATTERNS="uniform scatterall12" ARMS_uniform=fast8wf2 ARMS_scatterall12="fast8w full" SERIES=1 OUT=/data/exp04-results/exp79b lab_exp64.sh SOURCE 2 792

**The analysis:** EXP-79's (`analysis/exp79.py`), with `--patterns uniform,scatterall12` and `--below results/exp04/exp79`. The second option takes scatter12's medians from EXP-79, for the crossing only. It was extended before this run, and EXP-79's own scores are unchanged by it.
- **Dry run:** on EXP-77's runs of these two cells, which predate EXP-78, so predictions 1–3 failed there, as they must.
- Data `docs/experiments/data/exp79b-cells.csv`, figure `exp79b-guard`.

## Predictions

EXP-79's, for these cells:

1. **Deciding: scatterall12's guard takes at most 0.60 of 5.31 s.**
2. **uniform's guard takes at most 0.60 of 1.91 s.**
3. **scatterall12's fast path takes at least 2.0 s less than EXP-77's 108.48 s,** with `pg_basebackup` within 1% of EXP-77's 101.25 s.
4. **The summary rows** are within 1% of the earlier sessions' (1.47 M and 4.60 M).
5. **Every copy is correct.**

**Reported:** the guard's µs per row; the fast path over `pg_basebackup` at 96% of the pages, and the crossing with EXP-79's scatter12 point (0.604 at 53%); the source disk's writes during each run.

**Rule 11's verdict:** the two sessions' deciding ratios together. Both at or under 0.60: held. One over: not firm, and stated so.

## What follows

- **If 1 holds here too:** EXP-78's change is firm on the VMs.
- **If 3 holds:** the break-even is re-placed with the current code, and RESULTS, the READMEs and the break-even figure say so.
- **The selector's model** is refitted after this, on these cells, and tested on new ones before its constants change.
