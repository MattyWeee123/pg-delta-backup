# EXP-21: Why does v1's page stream slow down at DR round trips: slow start after idle, or losses?

Status: **done October 25, 2026 (05:17–05:51 UTC; 48 runs, all verified): its first prediction failed, and the records showed the cause another way.** Pre-registered before running. One fresh VM pair, about 1 hour. A diagnostic for [EXP-20](EXP-20-dr-latency-core.md), whose scattered-change cells were limited by TCP's congestion window, not by buffers or round trips.

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

## Results

| v1, uniform 1%, 60 ms (median of 3) | A: as shipped | B: no slow start after idle | C: BBR | D: BBR, no slow start after idle | EXP-20 (A) |
|---|---:|---:|---:|---:|---:|
| tuned hosts | 6.72 s | 6.65 | 6.48 | 6.43 | 23.97 |
| default hosts | 16.82 s | 17.28 | 16.34 | 16.04 | 25.49 |
| one `pg_basebackup` stream, tuned | 11.77 s | 11.64 | 13.31 | 11.61 | 11.63 |

| # | Prediction | Verdict |
|---|---|---|
| 1 | A repeats EXP-20 within 20% (v1 at 60 ms tuned 19.2–28.8 s) | **Failed:** 6.72 s |
| 2, 3 | B or C brings v1 at 60 ms tuned under 12 s | Not decidable: A was already under 12 s |
| 4 | `full` within 20% across A–D on tuned hosts | **Held:** 11.61–13.31 s |

## What the records show

- **v1's page stream at long round trips is bimodal.** On the same link, with the same round trip, the time `send()` blocked for the same 610 MB was either about 2.3 s (fast mode, the link's rate) or about 20 s (slow mode): all 3 of EXP-20's tuned runs at 60 ms were slow, none of EXP-21's A runs.
- **The mechanism, from the sampled congestion window** (the source's `ss`, every 0.5 s): in a slow run, a burst lost 32 segments about 1.5 s in, while the window was still 466 segments; cubic set its threshold there and grew back slowly (2,672 segments 18 s later, against about 10,000 to fill 2 Gbit/s at 60 ms). In a fast run, the first loss came after the window had passed 20,000 segments. Retransmission counts don't separate the modes (3–42 fast, 17–32 slow); when the first loss falls does.
- **Across EXP-20 and EXP-21, at 20–120 ms:** with cubic, 9 of 24 runs were in the slow mode (page sending more than twice the link-limited time); with BBR, 0 of 12. Turning off slow start after idle didn't prevent it (1 of 3 slow).
- **Why one `pg_basebackup` stream escapes it:** it sends 2.1 GB continuously, so cubic has time to grow back; v1's 610 MB transfer is short and starts with bursts.

## What follows

- EXP-20's "local changes only" for scattered changes rests on a mode that cubic falls into in some sessions and not others, so by rule 11 it is **provisional**, not a property of v1's design.
- BBR avoided the slow mode in 12 of 12 runs, too few to be firm (the 95% upper bound on its slow-mode rate is about 22%). EXP-23 measures the rate for each with many short repeats. If BBR holds, pgdelta can select it on its own sockets (Linux allows a congestion control per connection), so no host setting is needed.
- Whether the early losses happen on a real path, not only at the emulator's queue, is EXP-15's question.
