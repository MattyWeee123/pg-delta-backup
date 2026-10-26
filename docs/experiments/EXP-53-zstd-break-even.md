# EXP-53: Where is zstd's break-even now?

Status: **pre-registered October 27, 2026, before any run.** Two sessions on one VM pair each, about 45 minutes each. The open number behind the README's zstd advice.

## Question

With batched pool tasks, zstd took 0.86 of plain v1's time at 2 Gbit/s and 60 ms (EXP-52). Above 2 Gbit/s nothing is measured, so the advice stops there. On faster links v1's own time falls, because 610 MB crosses the link faster. zstd's falls less, since it sends 219 MB. Somewhere the two meet. Where?

**The projection,** from EXP-52's session and EXP-47's fit:
- **Plain v1** is about 3.5 s of fixed work plus 4.4 s ÷ Gbit/s on the link: 5.68 s at 2 Gbit/s.
- **zstd's time** of 4.88 s at 2 Gbit/s lies between two bounds:
  - **flat:** it stays at 4.88 s on faster links;
  - **falling:** it loses its own wire time, 1.75 Gbit ÷ the rate, on top of 4.0 s.

Equating each with v1 puts the break-even between **3.1 Gbit/s (flat)** and **5.1 Gbit/s (falling)**.

## Design

EXP-52's command and VMs, at two new rates:
- **The VMs:** the default `n2-standard-8` pair, the first us-central1 zone with capacity; BBR loaded and checked; `python3-zstandard` on both ends.
- **Conditions:** `3:60:tuned` and `5:60:tuned`. 5 Gbit/s at 60 ms is within these VMs' internal egress, about 16 Gbit/s; EXP-13 ran it.
- **The rest:** uniform 1%, hex-filler data, 2 repeats; methods `full`, `pgdelta_direct_inplace_bbr` and `pgdelta_direct_inplace_bbr_z`.
- **Two sessions on fresh VMs, from the start** (order seeds 530 and 531). Every ratio here is expected near 1.0, within rule 11's 20% of the bar, so a second session would be required anyway. The verdict uses both sessions' runs; each session is also reported.
- **The emulator check:** a condition whose steady one-stream link test falls below 90% of its rate is reported as emulator-limited (prediction 2).
- **The analysis,** committed before the data: `analysis/exp53.py`, figure `exp53-break-even`. EXP-52's 2 Gbit/s cell is drawn beside these.

## Predictions

1. **Deciding: the break-even lies between 3 and 5 Gbit/s:** zstd takes at most 1.00 of v1's time at 3 Gbit/s, and at least 1.00 at 5 Gbit/s.
2. **The emulator delivers:** the steady one-stream link test reaches 90% of 3 and of 5 Gbit/s in every trial.
3. **The setup holds:** BBR is in effect on every v1 run, and every zstd run uses batched tasks.
4. **Both pgdelta arms beat one `pg_basebackup` stream** at both rates.

## What follows

- **The advice uses the measured crossing:** zstd for DR links below the break-even, interpolated linearly between the measured rates on either side and rounded down to 0.5 Gbit/s. It is labeled as interpolated in the README, the CLI help and RESULTS.md.
- **If zstd is still faster at 5 Gbit/s,** the advice becomes "up to 5 Gbit/s, measured", and the break-even stays above that, unmeasured.
- **If it is already slower at 3 Gbit/s,** the break-even lies between 2 and 3 Gbit/s, interpolated.

## Revision before any result was read (October 27, rule 8)

**A third session at 4 Gbit/s.**
- **Why:** the owner asked for the break-even to be placed at 3 and 4 Gbit/s. That request arrived after this experiment was registered and started at 3 and 5, while the first session's results had been fetched but not read.
- **What it adds:** `4:60:tuned` on fresh VMs after the second session, otherwise the same command (2 repeats, order seed 532).
- **Unchanged:** the conditions of the first two sessions and every prediction.
- **How it is used:** the analysis reports the 4 Gbit/s cell beside the others, and the break-even is interpolated across 2, 3, 4 and 5 Gbit/s. With one session there, the 4 Gbit/s cell is reported as one session's.
