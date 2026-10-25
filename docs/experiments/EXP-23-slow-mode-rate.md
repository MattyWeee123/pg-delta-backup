# EXP-23: How often does v1's page stream fall into cubic's slow mode, and does BBR prevent it?

Status: **pre-registered October 25, 2026, before running.** On EXP-21's VM pair (still up), about 40 minutes. It turns [EXP-21](EXP-21-tcp-diagnosis.md)'s finding into a rate, with many short repeats instead of sessions on different days.

## Design

- EXP-21's pair and harness. v1 (`pgdelta_direct_inplace`) only, uniform 1% (scale 70, about 610 MB of pages), at **2:60:tuned** and **2:20:default**, the two conditions where cubic's slow mode appeared most; **15 repeats** each.
- Two invocations: **cubic** (as shipped) and **BBR** on both VMs, slow start after idle on in both.
- **The slow mode,** as EXP-21 defined it: the time `send()` blocked over page data is more than twice the time the condition's measured one-stream link rate needs for those bytes.

## Predictions

1. Cubic falls into the slow mode in 3 to 12 of its 15 runs at 2:60:tuned (EXP-20 and EXP-21 together: 4 of 9 with slow start after idle on or off).
2. **BBR falls into it in at most 1 of 15 runs at each condition, and cubic in at least 3 more than BBR at 2:60:tuned.** This is the deciding prediction.
3. Fast-mode runs take the same time under both (within 10%).

## What follows

- **If 2 holds:** pgdelta selects BBR on its own data socket (`TCP_CONGESTION`, falling back to the host's default where BBR isn't allowed), and EXP-20's scattered-change cells are rerun with it: DR's verdict is then judged on the fixed sender.
- **If BBR also falls into a slow mode:** the cause isn't cubic's alone; the next step is to keep the stream full between files, or to send page data on several streams.
- With 15 runs, a 0 of 15 bounds BBR's rate at about 18% (95%); the rate is stated with that bound, not as zero.
