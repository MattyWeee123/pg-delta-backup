# EXP-47: With decompression pipelined on the receiver too, where does zstd pay?

Status: **pre-registered October 26, 2026, before any run.** EXP-46's next lever. One VM pair, 12 timed runs, about 45 minutes with set-up.

## Question

EXP-46 moved compression onto a pool beside the sender's sending thread. At 2 Gbit/s zstd was then even with v1 for scattered changes (1.05, 0.97), and at 0.5 Gbit/s it took 0.52 of v1's time. Its timers put the 2 Gbit/s limit at the receiver: it decompressed in its main loop, 0.9–1.2 s per run, about what the smaller sending saved (0.5–0.9 s).

Now the receiver decompresses on a pool of `--jobs` threads as runs arrive, and writes them in order: before plain pages, a file's end, an append and the end, every queued run is written first. Two questions:
- Does zstd now pay at 2 Gbit/s?
- Where is its break-even? EXP-46's "about 1 Gbit/s" was a judgment between two measured points.

## Design

EXP-46's harness command and VMs (the default `n2-standard-8` pair, the first us-central1 zone with capacity), with:
- **patterns:** uniform 1% only (hot changes moved by at most 4% in all of EXP-45 and EXP-46);
- **conditions:** `2:60:tuned` (deciding) and `1:60:tuned` (1 Gbit/s, 60 ms, the break-even probe);
- **methods:** `full`, `pgdelta_direct_inplace_bbr`, `pgdelta_direct_inplace_bbr_z`;
- **data:** `--hex-filler`; 2 repeats, order seed 470;
- **set-up:** BBR loaded, allowed and checked on the source, and `python3-zstandard` on both ends. Every v1 run must report `effective: bbr`, or it is set apart;
- **the analysis,** committed before the data: `analysis/exp47.py`.

## Predictions

1. **Deciding: at 2 Gbit/s, v1 with zstd takes at most 0.90 of v1's median time.** This is projected, not certain: taking the receiver's 0.9–1.2 s off its main loop leaves EXP-46's 0.5–0.9 s of sending saved, about 0.85–0.92.
2. The receiver waits at most 0.3 s in all for decompressed runs: decompression is off its critical path. In EXP-46 it spent 0.9–1.2 s on it.
3. At 1 Gbit/s, v1 with zstd takes at most 0.75 of v1. There the network dominates: about 5 s for v1's 610 MB against 1.8 s for 219 MB.
4. Bytes fall at least 2-fold, and v1 with zstd is faster than one `pg_basebackup` in every cell.

## What follows

- **If 1 holds:** `--compress zstd` is recommended beside BBR for DR links of 2 Gbit/s and slower.
- **If 1 fails while 2 holds:** decompression is off the path but zstd still doesn't pay at 2 Gbit/s. The recommendation's ceiling is then set by prediction 3 (1 Gbit/s) and EXP-46 (0.5 Gbit/s), and the remaining limit is elsewhere: the source's CPU, shared with hashing, or per-frame costs.
- **Either way,** the README states the measured ratio at 0.5, 1 and 2 Gbit/s, not a judgment.

## Limitations

One session, uniform changes only, 60 ms only, hex-filler data, level 3. The receiver's pool adds CPU on the destination.
