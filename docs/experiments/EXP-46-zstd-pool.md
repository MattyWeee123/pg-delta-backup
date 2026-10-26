# EXP-46: With compression on a pool beside sending, does zstd make v1 faster at DR?

Status: **pre-registered October 26, 2026, before any run.** EXP-45's registered next step: its deciding prediction failed because the sender compressed in the thread that sends. EXP-45's design and VMs, about an hour.

## Question

In EXP-45, zstd sent 2.78× fewer bytes for scattered changes but made v1 1.30–1.48× slower at 2 Gbit/s. One thread compressed about 140 MB/s, serially with sending.

Now the sender compresses each run on a pool of threads, one per source worker (`--jobs`, 4 here), while its main thread reads and sends. Up to two runs per thread wait, and they're sent in order.

A local measurement on the same hex-filler pages showed the pool can deliver (Mac, 3-page runs):
- 500–571 MB/s on one thread;
- 1.44–1.68 GB/s on 4 threads, as fast as 4 processes.

zstd releases Python's lock while it compresses. Does compression now pay at this link?

## Design

EXP-45's counted run, unchanged except for the code: the default `n2-standard-8` pair, `2:60:tuned` and `2:120:tuned`, hot and uniform 1% at scale 70 with `--hex-filler`, methods `full`, `pgdelta_direct_inplace_bbr` and `pgdelta_direct_inplace_bbr_z`, 2 repeats, order seed 460. `python3-zstandard` is installed on both VMs.

**BBR is loaded** (`modprobe tcp_bbr`) and allowed on the source, and an unprivileged socket is checked to get it, before the run. Every v1 run must report `effective: bbr`, or it is set apart.

**The analysis,** committed before the data: `analysis/exp46.py`, which runs EXP-45's on these runs (files tagged `-pool`) and reads pgdelta's own statistics.

## Predictions

1. **Deciding: for uniform 1% changes, v1 with zstd takes at most 0.90 of v1's median time, at both round trips.**
   - **The projection:** 4 threads at EXP-45's per-thread rate, less some contention, about 400 MB/s, compress 608 MB in about 1.5 s. That overlaps sending 219 MB, about 0.9 s at v1's effective rate. Against v1's 2.5 s for 610 MB, that's about 0.82.
2. For hot changes, v1 with zstd is within 10% of v1.
3. For uniform, the sending thread waits at most 1.1 s in all for compressed runs: a quarter of EXP-45's serial 4.1–4.7 s. Compression is off the critical path.
4. For uniform, the bytes fall at least 2-fold, and v1 with zstd stays faster than one `pg_basebackup` in every cell.

## What follows

- **If 1 holds:** `--compress zstd` is recommended beside BBR for DR links of 2 Gbit/s and less. A fast-link test finds where it stops paying. Whether it can be part of a DR default then waits on its CPU cost on a busy source (O4): compressing adds source CPU, at most `--jobs` threads.
- **If 1 fails while 3 holds:** compression is off the critical path but still doesn't pay. The source's CPU, shared with the hashing workers, is then the limit, and the option stays off for links this fast.
- **If 3 fails:** the pool doesn't keep up, and the per-thread rate on these VMs is the question.

## Limitations

One session, one data shape, two round trips at one link rate, level 3. The pool adds CPU on the source, which this test doesn't price against a load (EXP-28's O4 does).
