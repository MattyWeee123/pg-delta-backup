# EXP-73b: With the VMs' duplication, does reading the summaries grouped on the server cut the guard's time?

Status: **pre-registered October 28, 2026, before any run.** Local, on the Mac (PostgreSQL 17.11), about 25 minutes. It follows EXP-73, whose deciding prediction failed:
- **B** (one query grouped per relation fork) took 0.547 of today's reading (A), against a bar of 0.4;
- every plan was identical.

EXP-73's post-hoc check traced the miss to its scale models. Each block appeared in 1.51 summaries there, against about 2.4 on the VMs. A's cost goes with summary rows, and B's with distinct blocks.

## Question

**Built so that a block appears in about as many summaries as on the VMs, does B take at most half of A's time, with identical plans?**

## Design

**EXP-73's script and methods, unchanged** (`experiments/fastpath_fixed_costs/guard_reading.py`), with three changes to the scale models:
- **The lag in 20 batches,** each followed by a `CHECKPOINT` (EXP-73: 5). EXP-71's scattered cell spanned 22 summaries.
- **The lab's state check** after the lag's last checkpoint: a read of all of `pgbench_accounts`, as EXP-67's lab does, then a `CHECKPOINT`. With checksums, its hint bits log an image of each changed page, which is what made the VMs' duplication (EXP-69).
- **Scale 80 and 320** (about 1.2 and 4.8 GB): 320 brings the distinct blocks nearer the VMs' 600,000, since B's cost per block grew with size in EXP-73.

The command:

    experiments/fastpath_fixed_costs/guard_reading.py --scales 80,320 --batches 20 --state-check --deciding-bar 0.5 --exp 73b

It runs 3 times per method and scale, alternated, after a warm-up. Its outputs are `docs/experiments/data/exp73b-guard.csv` and the figure `exp73b-guard`.

## Predictions

**The model** (EXP-73's post-hoc check): B ÷ A ≈ (B's cost per distinct block) ÷ (A's cost per row × duplication).
- A costs 0.91 µs per row, and B 0.75 µs per block at 82,593 blocks, growing with size (0.61 µs at 20,645).
- At a duplication of about 2.4 that gives roughly 0.35 at scale 80, and about 0.4 at scale 320 with the growth.

1. **Deciding: at scale 320, B takes at most 0.5 of A's time** (median of 3), with identical plans. The model gives about 0.4.
2. **The design reaches the VMs' duplication:** between 2.0 and 3.0 rows per distinct block at both scales (VMs: about 2.4). If it doesn't, 1 is reported but doesn't decide.
3. **B's plan equals A's in every run,** at both scales.
4. **EXP-73's model holds out of sample:** at scale 80, B ÷ A within 0.1 of 0.825 ÷ the duplication measured.

**The bar,** stated before the data: it is 0.5, not EXP-73's 0.4, because the model now gives about 0.4 at this size. As before, it leaves room above the model's figure. EXP-73's verdict stays failed either way.

## What follows

- **If 1–3 hold:** B replaces A in the sender:
  - `live.summary_blocks`: one query over the chosen summaries, grouped per relation fork, which also returns the row count for the statistics;
  - `fastpath.grouped_plan`.

  Tests check that it builds A's plan on a real cluster and through the real-failover test. The next VM session times the guard at EXP-70's or EXP-71's scattered cell, where the model gives about 4.7 s → 2 s.
- **If 1 fails with 2 holding:** A stays. The guard costs what it costs per row, and the next fixed cost is looked at instead.
- **If 2 fails:** the scale model still doesn't match the VMs. That is reported, and B is decided on the next VM session instead, timing both.
