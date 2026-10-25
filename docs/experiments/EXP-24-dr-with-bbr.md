# EXP-24: EXP-20's DR cells again, with v1 asking for BBR on its own connection

Status: **pre-registered October 25, 2026, before running.** On EXP-21/23's VM pair (still up), about 75 minutes. The follow-up EXP-23 named: [EXP-20](EXP-20-dr-latency-core.md)'s verdict for scattered changes rested on cubic's slow mode, which BBR avoided in 42 of 42 runs.

## Design

- EXP-20's design on EXP-21/23's pair: hot and uniform 1%; 2:0, 2:20, 2:60, 2:120 default and 2:60, 2:120 tuned; `full`, `parcopy8` (and `parcopy16` at 2:120:default); 3 repeats; order seed 240.
- **Both v1s in every repeat:** `pgdelta_direct_inplace` (cubic, the host default, as in EXP-20) and `pgdelta_direct_inplace_bbr` (the same engine, `pgdelta send --congestion-control bbr`: BBR on pgdelta's data socket only).
- **The one host setting:** the source adds BBR to `net.ipv4.tcp_allowed_congestion_control` ("reno cubic bbr"), so an unprivileged sender may choose it. The default stays cubic, for `pg_basebackup`, the parallel copies and every other connection. Each run's statistics record what pgdelta's socket got.

## Predictions

1. Every `pgdelta_direct_inplace_bbr` run gets BBR (pgdelta's statistics) and none is in the slow mode.
2. **EXP-13's DR verdict for v1 with BBR** (EXP-20's analysis, `--v1 pgdelta_direct_inplace_bbr`): row 1 ("holds as shipped") at 20 ms, and row 2 ("holds with tuned buffers") at 60 and 120 ms. At 60 and 120 ms default hosts one stream's 6 MiB window still caps v1, which the parallel copies escape with 8 or 16 streams, so row 1 isn't expected there. This is the deciding prediction.
3. For hot 1%, BBR changes v1 by less than 10% at every condition.
4. Cubic v1 falls into its slow mode in at least 2 of its 3 uniform 1% runs at 2:20:default, as in EXP-23 (12 of 15 there).

## What follows

- **If 2 holds:** pgdelta asks for BBR by default on its data socket, falling back to the host's default (and saying so) where BBR isn't allowed. The DR claim for scattered changes becomes "holds with tuned buffers and BBR allowed on the source", and the deployment note names both host settings. EXP-20's "local changes only" is recorded as a property of cubic's slow mode, not of v1's design.
- **If v1 with BBR still fails row 2 for uniform 1% at 60 ms:** the cause is elsewhere, and EXP-20's verdict stands for scattered changes.
- One session; a ratio within 20% of its threshold is provisional (rule 11, as EXP-20).
