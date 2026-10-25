# EXP-18: Does a calibration taken before the runs predict how deliberate CPU contention slows each tool?

Status: **pre-registered October 25, 2026, before running.** One VM pair, about 1 hour. It is the first test of the lean plan's lab (rule 11 as clarified October 25): vary a condition on purpose instead of waiting for it between sessions, and see whether a short benchmark predicts its effect.

## Why

[EXP-16](EXP-16-timing-model-check.md) found that the drift between sessions lived in compute speed: scaling v1's fixed work by its own CPU-seconds cut the prediction error on another VM pair from 13.4% to 2.0%. But those CPU-seconds came from the runs themselves. If a two-minute benchmark taken *before* the runs predicts as well, then compute speed can be measured at the start of any session, on any machine, and timing claims can be stated per unit of measured speed instead of repeated across sessions.

## Design

- **VMs:** one fresh pair of `n2-standard-8` (local NVMe RAID 0, ext4, Debian 12), `us-central1-a`, EXP-11's harness and settings.
- **Contention, set on purpose:** 0, 2 and 4 busy processes on dest (`agent.py burn`, nice 0) for the whole of each level. Dest is where v1's pool works hardest (EXP-09). The levels run in that order.
- **Calibration at each level, before its runs and under its contention:** `calibrate.py` on dest, keyed BLAKE2b over 8 KiB pages for 3 s with 1 and with 4 processes (pages per second).
- **Runs at each level:** `full` (one `pg_basebackup` stream) and `pgdelta_direct_inplace` (v1, 4 jobs, in place), hot 1% changes, at 2 Gbit/s and unthrottled, 3 repeats, order seed 18; 12 runs per level, 36 in all.

## The model, fixed now

From level 0 only:
- v1: time = c × (H0 ÷ H) + bytes ÷ L, where c is level 0's median time less bytes ÷ L (its fixed work), H is the calibration's 4-process hash rate at the level and H0 at level 0, L the run's measured link rate.
- `full`: time = c_f + bytes ÷ min(L, S), with c_f and S from level 0 as in EXP-16 (contention on dest is predicted not to matter to it, so c_f is not scaled).
- The **uncalibrated** prediction is the same with H = H0.

## Predictions

1. With 4 busy processes the 4-process hash rate on dest falls by 25–50% (8 vCPUs are 4 cores with two hyperthreads each).
2. v1's median time grows with contention, by 15–60% at 4 busy processes, and by less than 10% at 2.
3. **The calibrated model predicts v1's median time at levels 2 and 4 within 10% in every cell; the uncalibrated one misses by more than 10% at level 4.** This is the deciding prediction.
4. `full`'s time changes by less than 10% at every level (its dest work is light).

## What follows

- **If 3 holds:** a calibration benchmark is a valid way to carry timing results across machines and load. Later lab experiments state their results per measured hash rate and disk speed, and a second session is run only where a decision's margin is inside the calibrated model's error.
- **If it fails:** the residual is reported per cell, the model's missing term is named from the run records (phase timers, CPU-seconds), and timing claims stay per session.

## Limitations, known before running

One pair, one session, one VM type. Contention is synthetic (busy loops), not a real neighbour's mix of memory and cache traffic; disk and memory contention aren't varied here. Levels run in a fixed order, so a slow drift within the hour would mix with contention; level 0 is repeated at the end (3 runs of each arm, unthrottled) to check for it.
