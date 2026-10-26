# EXP-45: Does compressing the pages sent make v1 faster at DR round trips?

Status: **pre-registered October 26, 2026, before any run.** D35's lever, built and measured. One VM pair (EXP-36's set-up), 24 timed runs, about an hour with set-up.

## Question

At DR round trips v1 sends the changed pages over a link of a few Gbit/s. EXP-24's timing model puts v1 at about 3.48 s plus one second per 194 MB sent, at 60 ms on a 2 Gbit/s link with tuned buffers: for scattered changes the bytes dominate. EXP-37 measured zstd at level 3 on such pages: 2.83 times fewer bytes, at 548 MiB/s per core. That is faster than the link carries them.

`--compress zstd` (built for this, tested; off by default) compresses each run of pages on the sender and decompresses it on the receiver. Does it make v1 faster at DR round trips, and by how much?

## Design

EXP-36's harness command (`run_exp04.py`) and VMs (the default `n2-standard-8` pair), with:
- **conditions:** `2:60:tuned` and `2:120:tuned` (2 Gbit/s, 60 and 120 ms, TCP buffers up to 128 MiB), where EXP-24 found v1 with BBR ahead;
- **patterns:** hot and uniform 1% at scale 70 (about 2.1 GB);
- **data:** `--hex-filler`, new for this. pgbench's blank filler compresses far better than real rows would, so the accounts' filler is overwritten with hex text after the load, as in EXP-37, whose 2.83× is on this data;
- **methods:**
  - `full`, one `pg_basebackup` stream;
  - `pgdelta_direct_inplace_bbr`, v1 with BBR;
  - `pgdelta_direct_inplace_bbr_z`, the same with `--compress zstd`;
- **the VMs' Python:** Debian 12's 3.11, with `python3-zstandard` installed on both. The hash is BLAKE2b, as in EXP-36 (no `blake3` there).
- **runs:** 2 repeats, order seed 450, CRC32C manifests, EXP-36's link test and noise settings. Every output is verified by the harness.
- **The analysis,** committed before the data: `analysis/exp45.py`.

## Predictions

1. **Deciding:** for uniform 1% changes, v1 with zstd takes at most 0.90 of v1's median time at both round trips. EXP-24's model gives 0.70–0.85, depending on whether compressing overlaps sending: the sender compresses in the thread that sends.
2. For hot changes (about 30 MB sent), v1 with zstd is within 10% of v1: there is little to compress.
3. For uniform 1%, the bytes from source to destination fall at least 2-fold (EXP-37: 2.83× on the pages alone).
4. In every cell, v1 with zstd stays faster than one `pg_basebackup` stream, as v1 is (EXP-24, EXP-36).

## What follows

- **If 1 holds:** `--compress zstd` joins BBR in the DR deployment note: both opt-in, both for links of a few Gbit/s or less at long round trips. Whether it can be a default waits on a fast-link test, where compressing on one core should cost time.
- **If 1 fails while 3 holds:** the bytes fell but the time didn't. The sender's single compressing thread is then the bottleneck. Moving compression onto the hashing workers is the next build, and the option stays off.
- **If 3 fails:** these pages don't compress as EXP-37's did, which is reported, and the option stays as it is.

## Limitations

One session, one data shape (pgbench with hex filler), 2 repeats, two round trips at one link rate. Compression at level 3 only. Real rows compress more or less than these.
