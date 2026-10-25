# EXP-21: Why does v1's page stream slow down at DR round trips: slow start after idle, or losses?

Status: **pre-registered October 25, 2026, before running.** One fresh VM pair, about 1 hour. A diagnostic for [EXP-20](EXP-20-dr-latency-core.md), whose scattered-change cells were limited by TCP's congestion window, not by buffers or round trips.

## Design

- One fresh `n2-standard-8` pair, `us-central1-a`, EXP-20's harness and settings. Workload uniform 1% (scale 70, about 610 MB of pages for v1); arms `pgdelta_direct_inplace` (v1) and `full` (one `pg_basebackup` stream); conditions 2:60:default and 2:60:tuned; 3 repeats.
- **Four TCP settings, one invocation each,** set on both VMs before it and restored after:
  - **A** as shipped: cubic, `tcp_slow_start_after_idle = 1` (EXP-20's setting, repeated: it also measures this session against EXP-20's);
  - **B** `tcp_slow_start_after_idle = 0`, cubic;
  - **C** BBR congestion control, slow start after idle on;
  - **D** BBR and slow start after idle off.

## Predictions

1. **A repeats EXP-20** within 20%: v1 at 60 ms tuned 24.0 s (19.2–28.8).
2. **If slow start after idle is the main cause,** B brings v1 at 60 ms tuned under 12 s, and C much less.
3. **If losses at the emulated bottleneck are the main cause,** C brings it under 12 s, and B much less.
4. `full`, which sends continuously, changes by less than 20% across A–D on tuned hosts.

## What follows

- **If B or D brings v1 at 60 ms tuned to 0.90 of EXP-20's fastest copy or below (about 9.1 s):** EXP-13's row 2 is reachable with a host setting, and pgdelta can avoid needing it by keeping its stream full between files, or by choosing its congestion control per socket (BBR is selectable per connection on Linux). The DR verdict "local changes only" is then a property of today's sending loop, not of v1's design.
- **If only C does:** the losses are at the emulated bottleneck; a real link (EXP-15) decides whether they happen outside the emulator.
- **If none does:** the cause is elsewhere; the run records (pgdelta's timers, `ss`) are read for it before anything is claimed.

## Limitations

One session; the emulated WAN in one zone; uniform 1% only.
