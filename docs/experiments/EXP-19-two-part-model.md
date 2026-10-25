# EXP-19: Does EXP-18's two-part model predict a contention level it never saw, and where does it stop?

Status: **pre-registered October 25, 2026, before running.** One fresh VM pair, about 25 minutes. It tests the hypothesis [EXP-18](EXP-18-calibrated-contention.md) found after the fact, on new VMs and a new level, with the model's numbers fixed here.

## The model, fixed now from EXP-18

v1's time at a contention level = t₀ × [(1 − f) + f × H₀ ÷ H] (+ the link term, which is nearly constant here), where t₀ is v1's median time at level 0 in this session, H₀ and H the destination's 4-process hash rates (`calibrate.py`) at level 0 and at the level, and **f = 0.64 unthrottled and 0.57 at 2 Gbit/s**, the share of v1's time that scaled with the hash rate in EXP-18. Nothing is fitted in this session except t₀ and H₀. `full` is predicted not to change.

## Design

- One fresh `n2-standard-8` pair in `us-central1-a`, EXP-18's harness and runs: `full` and `pgdelta_direct_inplace`, hot 1%, 2 Gbit/s and unthrottled, 3 repeats per cell.
- Levels in order: **0**; **3 busy processes** on dest (a CPU level EXP-18 didn't run); **disk contention** (a background loop of 4 GiB direct writes to dest's data disk); **0** again.
- Before each level's runs, under its condition: the hash benchmark and a 1 GiB direct write and read on dest's data disk.

## Predictions

1. **At 3 busy processes, the fixed model predicts v1's median within 10% in both cells.** This is the deciding prediction.
2. Under disk contention the hash rate stays within 5% of level 0, so the model predicts no change in v1; v1 slows anyway by more than 10%, because its reads and writes share the disk. That is, the model's scope ends at CPU, and the disk benchmark shows how far the disk slowed.
3. `full` changes by less than 10% at 3 busy processes; under disk contention it slows, since it writes 2.1 GB to that disk.
4. Level 0 at the end is within 5% of the first.

## What follows

- **If 1 holds:** CPU contention's effect on v1 is predictable across sessions from a 3-second benchmark, and v1's fast-link verdicts can be stated per measured destination hash rate (for example, "v1 beats one `pg_basebackup` stream unthrottled while dest's 4-process hash rate stays above X").
- **If it fails:** the share f isn't stable across sessions, and the exploratory model is dropped.
- **Prediction 2 decides the next term:** if disk contention slows v1, the next lab test adds the disk benchmark to the model.

## Limitations

One session, one VM type, synthetic contention. Levels run in a fixed order; level 0 is repeated at the end.
