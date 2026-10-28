# EXP-79: The guard's unsorted reading on the VMs, and the break-even with the fast path as it now is

Status: **pre-registered October 28, 2026, before any run.** One `ha` VM pair, about 1.5 hours with the data load. It times EXP-78's change on the VMs, and re-places EXP-75's break-even after EXP-77's and EXP-78's changes.

## Question

**EXP-78 (local)** cut the guard's summary reading to 0.460 of its time, by returning each fork's blocks unsorted with their duplicates; it is built into the sender. On the VMs the guard has been steady across sessions (EXP-74 to EXP-77's statistics):
- **uniform:** 1.89–1.93 s for 1.47 M summary rows;
- **scatter12:** 2.91 s for 2.59 M rows;
- **scatterall12:** 5.28–5.35 s for 4.60 M rows.

That is 1.15–1.30 µs per row. At EXP-78's ratio the guard would take about 0.9, 1.3 and 2.4 s. That is a projection: the VMs' source is slower per row than the Mac (0.51 µs), and its share of sorting may differ.

**The break-even:** EXP-75 placed it near 85% of the pages changed, by interpolating between EXP-74's scatter12 (0.64 of `pg_basebackup` at 53% of the pages) and EXP-75's scatterall12 (1.12 at 96%). EXP-77's change brought the 96% point to 1.07. Both points were measured with older code, in different sessions. This session measures both with the current fast path.

**The question: on the VMs, does the guard take at most 0.6 of its time before, and where does `pg_basebackup` now start to win?**

## Design

**EXP-77's lab and set-up** (`experiments/network/lab_exp64.sh`, `SERIES=1`):
- the `ha` set in us-west1: two `n2d-highcpu-8` VMs with 150 GiB balanced persistent disks, `blake3` on both;
- EXP-48's 18 GB database, PostgreSQL 17.11, `summarize_wal` on;
- pgdelta at this commit: the written pages checked on the verifier, a run at a time (EXP-77), and the guard's reading unsorted (EXP-78).

**Three cells,** each with B0 a base backup taken just before its change, in this order:
- **`uniform`:** 1% of `pgbench_accounts`' rows in random batches. **Arm:** `fast8wf2`, for the guard.
- **`scatter12`:** 12% of `pgbench_accounts`' rows by key hash (EXP-74's 53% of the pages). **Arms:** `fast8w` and `full` (`pg_basebackup`).
- **`scatterall12`:** 12% of both tables' rows by key hash (96% of the pages). **Arms:** `fast8w` and `full`.

**Every fast arm** pins `--read-ahead 8`, `--fsync worker` and `--no-diverged-basis`, and its fill.

**Runs:** 2 per arm, shuffled per cell (seed 790); 10 runs.

**The command:**

    PATTERNS="uniform scatter12 scatterall12" ARMS_uniform=fast8wf2 ARMS_scatter12="fast8w full" ARMS_scatterall12="fast8w full" SERIES=1 OUT=/data/exp04-results/exp79 lab_exp64.sh SOURCE 2 790

**A smoke first,** at scale 20 (one run per arm, seed 791, deciding nothing). Every run must exit 0, every fast copy must match, every fast run must use the fast path, and every run must have both disk series.

**The analysis,** committed before the data: `analysis/exp79.py`. Figure `exp79-guard` (each cell's guard before and now); data `docs/experiments/data/exp79-cells.csv`. The break-even figure (`analysis/breakeven_fastpath.py`) gains this session's two points after the data.
- **Dry run:** on earlier sessions' runs of the same cells (EXP-74's uniform and scatter12, EXP-77's scatterall12). The parsing, scoring and crossing worked: it gave EXP-77's 89% from those points.
- **Those runs predate EXP-78,** so predictions 1–3 failed there, as they must. The dry run's outputs were removed.

## Predictions

1. **Deciding: scatterall12's guard takes at most 0.60 of 5.31 s** (median of 2; the model says about 0.46, 2.4 s).
2. **uniform's guard takes at most 0.60 of 1.91 s.**
3. **scatterall12's fast path takes at least 2.0 s less than EXP-77's 108.48 s,** with `pg_basebackup` within 1% of EXP-77's 101.25 s, so the sessions compare.
4. **The guards read the same:** each cell's summary rows within 1% of the earlier sessions' (1.47 M, 2.59 M, 4.60 M).
5. **Every copy is correct:** every run exits 0, and every fast run used the fast path and passed the copy's check.

**Reported, not predicted:**
- the guard's µs per summary row;
- the fast path over `pg_basebackup` at 53% and 96% of the pages, and their interpolated crossing;
- scatter12's guard against its 2.91 s.

**Rule 11:** a deciding ratio within 0.05 of the bar (0.55–0.65) gets a second session.

## What follows

- **If 1–3 hold:** the guard's cost is halved on the VMs. The break-even is stated with the current fast path, in RESULTS and the READMEs.
  - The selector's model, whose overhead per MB and fixed costs predate EXP-77 and EXP-78, is refitted on these cells and tested on new ones before its constants change.
- **If 1 fails:** the VMs' guard isn't bound where the Mac's was. The guard is timed in parts on a VM (EXP-73's script) before anything else changes.
