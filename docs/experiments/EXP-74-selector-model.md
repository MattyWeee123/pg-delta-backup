# EXP-74: Does the fast path's time model predict cells it hasn't seen, and does its selector pick a fast option?

Status: **pre-registered October 28, 2026, before any run.** One `ha` VM pair, about 2.5 hours with the data load. It waits for `gcloud` to be signed in again. It is research loop 1's idea 4 (the selector), and it also times EXP-73b's grouped summary reading on the VMs.

## Question

Once the guard has run, the sender knows its plan: every page it will read and write. **The model** (`analysis/fastpath_model.py`) predicts the fast path's time from that plan and the disks' limits, each measured in an earlier session:
- **requests:** the sender's runs, split by the block layer into requests of at most 168 KiB (EXP-70);
- **each disk's time:** the larger of its requests at its operation rate and its bytes at 210 MB/s:
  - source reads at 4,100 a second (EXP-68, EXP-70);
  - destination writes at 4,790 a second (EXP-70's post-hoc check);
  - the byte rate from EXP-70's full copy, and EXP-66's 205.5 MB/s;
- **the transfer:** the busier disk;
- **the rest of the session:** the guard (measured, already spent when the selector decides), plus 0.6 s and 0.97 ms per MB sent. This term was fitted on two EXP-70 cells.

**A full copy:** 5.7 s plus every listed file's bytes at 210 MB/s.

**The selector** predicts fills 0, 1, 2 and 4 of the unfilled plan, and a full copy, and picks the fastest.

**In-sample on EXP-70,** where its constants come from (a fit, not a test):
- every scattered arm within 2.5–6%;
- the full copy 90.8 s against 91.0;
- it picks fill 2, the fastest arm measured.

It misses the hot cell by 47% (1.26 s against 1.85 s): at 15,000 pages the destination's fixed write overhead dominates, which the model leaves out. So it is a model of the transfer-dominated regime, which is where a choice between options matters.

**The question:** on cells with more changed pages, where filling and a full copy compete, does the model predict each option within 15%, and does the selector's pick come within 5% of the fastest?

## Design

**EXP-70's lab** (`experiments/network/lab_exp64.sh`) and **EXP-70's set-up:**
- the `ha` set in us-west1: two `n2d-highcpu-8` VMs with 150 GiB balanced persistent disks, `blake3` on both;
- EXP-48's 18 GB database, PostgreSQL 17.11, `summarize_wal` on;
- `PLAN=1`, so every fast arm's plan is kept.

**Three cells,** each with B0 a base backup taken just before its change:
- **`uniform`:** EXP-70's scattered cell (1% of rows in random batches, the same seed), arm `fast8wf2` only. Its guard times EXP-73b's grouped reading against EXP-70's 4.70 s on the same summary rows.
- **`scatter4` (new):** 4% of `pgbench_accounts`' rows, chosen by a hash of each row's key in one statement (`abs(hashint4(aid + 74)) % 10000 < 400`). Locally the same rows came out whatever the table's physical order. With about 27 rows a page, about two-thirds of the pages change. The model expects the fast path to win here (roughly 78 s against a full copy's 94 s, from estimated page counts).
- **`scatter12` (new):** 12% of rows the same way, nearly every page. The model expects a full copy to win (roughly 101 s against 115 s).

**Arms in the scatter cells:**
- `fast8w`: fill 0, explicit;
- `fast8wf1`, `fast8wf2`;
- `full`.

The fast arms pin `--read-ahead 8`, the sync worker and `--no-diverged-basis`, with the current pgdelta (grouped summary reading).

**Runs:** 2 per arm, shuffled per cell (seed 740); 18 runs.

**The command:**

    PATTERNS="uniform scatter4 scatter12" ARMS_uniform=fast8wf2 ARMS_scatter4="fast8w fast8wf1 fast8wf2 full" ARMS_scatter12="fast8w fast8wf1 fast8wf2 full" PLAN=1 OUT=/data/exp04-results/exp74 lab_exp64.sh SOURCE 2 740

**A smoke first,** at scale 20 (one run per arm, deciding nothing). Every run must exit 0, and every fast copy must match.

**The analysis,** committed before the data: `analysis/exp74.py` with `analysis/fastpath_model.py`. Figure `exp74-model`, data `docs/experiments/data/exp74-cells.csv`.
- **What the selector sees:** each scatter cell's unfilled plan, its guard's measured median, and the listed files' sizes.
- **Scoring:** it is scored among the options run (fills 0–2 and full). Whether fill 4 would have been its pick is reported.
- **Dry run:** on EXP-70's results, with EXP-70's uniform cell standing in for a scatter cell. The parsing, the model, the fill check and the figure worked. Prediction 4 failed there, as it must on EXP-70's old reading. The dry run's outputs were removed.

## Predictions

1. **Deciding: in each scatter cell, the selector's pick takes at most 1.05 of the fastest measured arm's time** (medians of 2).
2. **The model predicts every fast arm within 15%:** each arm's time from its own saved plan and its guard, against its measured median. That is 7 cells: the uniform `fast8wf2` and 3 arms in each scatter cell.
3. **Each scatter cell's full copy within 5% of the model** (5.7 s plus the listed bytes at 210 MB/s).
4. **EXP-73b on the VMs:** the uniform cell's guard takes at most 0.6 of EXP-70's 4.70 s. The model gives about 0.46, or 2.2 s. Its summary rows are within 1% of EXP-70's 1,464,232.
5. **Every copy is correct:** every run exits 0, every fast run used the fast path and its copy matched the source, and each filled plan is exactly `fastpath.fill_gaps` of the unfilled one.

**Reported, not predicted:**
- each cell's share of pages changed;
- each disk's requests and busy time against the model's;
- the guard's time per summary row in each cell;
- where a full copy beats the fast path.

**Rule 11:** if a cell's pick isn't the fastest arm and its ratio lies between 1.00 and 1.10, that cell gets a second session.

## What follows

- **If 1–3 hold:** the selector is built into the sender, behind an option and off by default. After the guard, it chooses the fill, or a full copy instead of the fast path. It reads its disk constants from options, and the defaults are this pair's. A later session times it on its own.
- **If 2 or 3 fails:** the cells where it misses say which term is wrong (the requests, a disk's limit, or the overhead per MB). The model is revised and tested on new cells before any selector is built.
- **If 4 holds:** EXP-73b's projection is confirmed on the VMs. **If it fails:** the guard's VM time is reported against the Mac's ratio, and the grouped reading's value is re-weighed.
- **Either way,** the cells place the fast path's break-even against a full copy on this disk, which research loop 1 has left open since EXP-70.
