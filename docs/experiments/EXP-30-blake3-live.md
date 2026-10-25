# EXP-30: Does BLAKE3 let v1 use 4 workers on a busy primary?

Status: **done October 25, 2026 (18:55–19:07 UTC; 12 runs, all counted; VMs deleted 19:10): with `--hash blake3`, v1 at 2 workers refreshed a busy primary in 2.73 s, 2.1 times faster than `pg_basebackup`, at no measurable cost to the load, with 64% less sender CPU than BLAKE2b. EXP-28's 4-worker miss did not repeat: BLAKE2b at 4 workers met O4 in this session.** Pre-registered before any run. The end-to-end step [EXP-29](EXP-29-hash-per-core.md) registered: `pgdelta --hash blake3` (built since, BLAKE2b still the default) on [EXP-28](EXP-28-live-slowdown.md)'s busy primary, unthrottled, where v1 at 4 workers missed O4. Three VMs (`PAIR=t10b` again), about 35 minutes with set-up.

## Question

Does keyed BLAKE3 cut v1's source CPU enough that 4 workers meet O4 on a busy primary, and what does it do to v1's time and its CPU per refresh?

## Design

EXP-28's design and its fixed driver (`lab_exp28.sh`, now with `BLAKE=1`: pgdelta runs on both ends from a venv with the `blake3` package), on fresh VMs:
- the same cluster, the same load (128 clients at 0.6 × C, C measured at set-up), the same windows, numbers and bar;
- unthrottled only, where EXP-28's miss was.

Arms, 2 runs each in an order seed 300 shuffles (12 runs):
- `none` and `full` (the bar);
- `v1_j2` and `v1_j4` (BLAKE2b; EXP-28 again, a same-session control);
- `v1b3_j2` and `v1b3_j4` (`--hash blake3`).

**Added:** the sender's own CPU-seconds, its worker processes included (`cpu_time.py`: the sender run as a child, its `RUSAGE_CHILDREN` read after it exits). An arm whose excess lies within 20% of its δ gets two more runs, as in EXP-28.

## Predictions

1. **Deciding: `v1b3_j4` meets O4 unthrottled.** The hash is most of v1's source CPU per page (13.6 of the µs per 8 KiB page on these VMs), and BLAKE3 cuts it about 4.8 times.
2. BLAKE3 cuts the sender's CPU-seconds per refresh by at least half at both worker counts.
3. `v1_j4` (BLAKE2b) misses O4 again in this session, as in EXP-28.
4. `v1b3_j4` is no slower than `v1_j4`.

## What follows

- **If 1 holds:** with the `blake3` package on both ends, `--hash blake3` makes 4 source workers safe on a busy primary. Live mode then caps the default at 4, not 2, when BLAKE3 is chosen. Whether BLAKE3 becomes the default hash (which needs the package on both ends, or a capability exchange in the protocol) is a separate change, judged with the HA scale model.
- **If not:** live mode stays at 2 workers with either hash, and BLAKE3 is judged on its CPU and time alone.
- **If 3 fails:** EXP-28's 4-worker miss doesn't repeat, and both sessions are reported. K10 (as narrowed) asks for a second session only on a deciding cell near its line.

## Limitations

EXP-28's limitations, plus: one session, unthrottled only. A sender's CPU-seconds include its reading and the protocol, not only the hash.

## Results

12 runs, all counted. Fresh VMs gave C = 24,527 transactions a second (EXP-28's: 21,891), so R = 14,700. Every v1 run's statistics record the hash it asked for, and every BLAKE3 run used BLAKE3 at both ends. Data: `docs/experiments/data/exp30-cells.csv`; raw `results/exp04/sets/t10b/exp30`; figure `analysis/exp30.py` (EXP-28's analysis on these runs).

![Refresh time, the sender's CPU and the load's loss, BLAKE2b against BLAKE3](../figures/exp30-blake3.svg)

| Arm | Refresh | Sender CPU-s (workers included) | Work lost, net (s) | Worst drop, net | p95 / p99 (ms) | Excess over `pg_basebackup`: W, D | Meets O4 (×0.5, ×1, ×2) |
|---|---:|---:|---:|---:|---:|---:|---|
| load only | (8.0) | – | – | – | 1.7 / 2.3 | – | – |
| `pg_basebackup` | 5.83 s | not measured | 0.002 | 0.003 | 2.1 / 4.0 | (the bar) | – |
| v1, BLAKE2b, 2 workers | 6.00 s | 9.57 | 0.000 | 0.001 | 2.7 / 6.1 | −0.002, −0.001 | yes, yes, yes |
| v1, BLAKE2b, 4 workers | 4.57 s | 9.59 | 0.001 | 0.002 | 10.1 / 23.8 | −0.001, −0.001 | yes, yes, yes |
| **v1, BLAKE3, 2 workers** | **2.73 s** | **3.46** | 0.000 | 0.001 | 2.7 / 6.9 | −0.002, −0.002 | **yes, yes, yes** |
| v1, BLAKE3, 4 workers | 3.05 s | 4.01 | 0.025 | 0.017 | 11.2 / 34.9 | +0.023, +0.015 | yes, yes, yes |

No arm's excess lies within 20% of its δ, so no extra runs.

| # | Prediction | Verdict |
|---|---|---|
| 1 | **Deciding: v1 with BLAKE3 at 4 workers meets O4 unthrottled** | **Held,** at all three tolerances. It shows less than registered, because 4 BLAKE2b workers met O4 too in this session (prediction 3) |
| 2 | BLAKE3 cuts the sender's CPU-seconds by at least half at both worker counts | **Held:** 64% less at 2 workers (3.46 against 9.57), 58% at 4 (4.01 against 9.59) |
| 3 | v1 with BLAKE2b at 4 workers misses O4 again | **Failed:** it met O4 (excesses −0.001 and −0.001), where EXP-28 had measured +0.073 and +0.047 |
| 4 | BLAKE3 at 4 workers is no slower than BLAKE2b at 4 | **Held:** 3.05 s against 4.57 |

## What the records show

- **BLAKE3 makes v1 the fastest copy on a busy primary.** At 2 workers it took 2.73 s against `pg_basebackup`'s 5.83 s and BLAKE2b's 6.00 s, and cost the load nothing measurable. The hash had been most of v1's source CPU: 9.6 CPU-seconds per refresh with BLAKE2b, 3.5 with BLAKE3.
- **With BLAKE3, 4 workers are slower than 2** (3.05 against 2.73 s) and cost the load more (0.025 s of work, p99 35 ms). Once hashing is cheap, more workers only add contention: reading and sending, not hashing, bound the refresh.
- **EXP-28's 4-worker miss is session-dependent.** With BLAKE2b, 4 workers lost the load 0.073 s of work in EXP-28 and nothing measurable here. These VMs were faster (C 24,527 against 21,891; the load's p95 1.7 ms against 5.3), and EXP-28's own 4-worker runs ranged from 0.013 to 0.114 s. So the miss is real in some sessions and absent in others.

## What follows

- **Live mode's default stays at most 2 workers, with either hash.** This deviates from the registered consequence, which would have raised the cap to 4 with BLAKE3. It is recorded under rule 8: that consequence assumed 4 workers would be faster, and they are slower with BLAKE3. With BLAKE2b, 2 workers met O4 with margin in both sessions, while 4 missed in one of the two.
- **`--hash blake3` is the recommended setting** wherever the `blake3` package can be installed on both ends. It makes v1 about 2.2 times faster than BLAKE2b on these VMs, and twice as fast as `pg_basebackup` on a busy primary.
- **Making it the default** needs either the package everywhere or a way for the receiver to say it supports BLAKE3 before the sender commits to a hash. Today the sender chooses alone, in HELLO. That protocol change is separate, judged after the HA test below.
- **Next:** the HA scale model (EXP-22) with BLAKE3. The destination's CPU decided HA against v1 there, and BLAKE3 cuts that CPU on both of the destination's passes.
