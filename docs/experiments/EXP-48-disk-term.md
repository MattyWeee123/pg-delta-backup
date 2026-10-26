# EXP-48: Does a two-term disk model predict v1 beyond memory at a size it never saw?

Status: **pre-registered October 26, 2026, before any run.** The timing model's missing disk term (EXP-19: "the disk term is still missing"). One VM pair, about 1.5 hours with the data load.

## Question

Every claim about databases larger than our scale models is extrapolation. EXP-22 and EXP-31 measured v1 beyond memory on balanced persistent disks at one size (12 GB on 8 GB VMs). Their timers suggest v1's time there has two terms:
- **reading B once** to hash it, at the disk's read rate;
- **writing the received pages**, scattered, at a rate set by the disk's operations, not its throughput.

If that model predicts a size it never saw, projections to production sizes can come from a disk's own numbers instead of from guesses.

## The model, fixed now

From EXP-31's second session (`results/exp04/sets/ha/exp31b-s400`). These are fitted constants, and in-sample errors there (under 1%) are not evidence:
- **v1** (in place, `--verify-scope written`): `T_v1 = |B| ÷ R_read + P_written ÷ 6,750 pages/s`. `R_read` is the session's own disk read test on dest; `|B|` = pages × 8 KiB and `P_written` = the pages pgdelta wrote, both from pgdelta's statistics.
- **one `pg_basebackup` stream:** `T_full = |S| ÷ R_write + 4.5 s`, with `R_write` from the same test.

## Design

EXP-31's second session, at a new size:
- **the set:** `ha`, two `n2-highcpu-8` (8 GB) in two zones, each with a 150 GiB balanced persistent disk (the same disk as EXP-31, so the same operations limit);
- **the size:** scale 600 (about 18 GB, 2.25× memory) instead of scale 400 (12 GB, 1.5×);
- **patterns:** hot and uniform 1%; unthrottled; 2 repeats; order seed 48001; CRC32C manifests;
- **methods:** `full` and `pgdelta_direct_inplace_vw` (v1 as it now runs in place);
- **the analysis,** committed before the data: `analysis/exp48.py`.

## Predictions

1. **Deciding: the model predicts every cell's median within 15%** (v1 and the full copy, hot and uniform), from that session's own disk test and pgdelta's own counts. Roughly: hot v1 about 86 s and uniform about 180 s, against a full copy of about 90 s; the exact predictions follow from the measured inputs.
2. Hot: v1 ties the full copy within 10%, as at 12 GB (0.96).
3. Uniform: v1 takes at least 1.8× the full copy, as at 12 GB (1.92×).

## What follows

- **If 1 holds:** the disk term stands at two sizes and memory ratios. The HA verdict beyond memory projects to any size from a disk's read rate and operations limit. A DR projection takes the larger of the network term (EXP-24) and this disk term. RESULTS.md and the deck say so, with the formula.
- **If 1 fails:** which term missed is reported, and larger sizes stay labelled as extrapolation.

## Revision before any run (October 26, rule 8)

**The machine type changes to `n2d-highcpu-8`.** The first attempt found no `n2-highcpu-8` available in us-central1-a, -b, -c or -f; nothing was created. `n2d-highcpu-8` has the same 8 vCPUs and 8 GB, on AMD, so the data still runs at 2.25× memory, and it fits the project's 16 N2D vCPUs.
- **Unchanged:** the disks (150 GiB balanced persistent disks), the model, its constants and every prediction.
- **Why that's sound:** v1's time beyond memory is set by the disk, not the CPU (EXP-22, EXP-31).
- **If prediction 1 fails,** the CPU family is one more candidate cause, and is reported as such.

## Limitations

One disk type and size (so one operations limit), two data sizes, pgbench-shaped data; the model's operations term is one fitted constant, not derived from the disk's published limits.
