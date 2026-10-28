# EXP-64b: Do overlapped source reads close the fast path's gap on scattered changes?

Status: **pre-registered October 27, 2026, before any run.** One VM pair (the `ha` set), about 1.5 hours with the data load. The owner asked for the overlapped reads to be built and this experiment registered on EXP-64's uniform cell.

## Question

Beyond memory, EXP-64's fast path refreshed 18 GB with scattered changes in 164.8 s: 0.915 of hashing's time, and 1.81 times a full copy. The runs' own timers (median of 2, uniform cell) put the cause in the sender:

| EXP-64, fast path, uniform | Seconds |
|---|---:|
| Sender, reading the source and sending (`hash_source`) | 157.1 |
| of which blocked sending (`page_send_s`) | 57.8 |
| so reading the source's 619,251 chosen pages, one run at a time | **about 99** |
| Receiver writing the pages (`rebuild`) | 57.6 |
| Receiver syncing them, before the next file (`fsync`, main loop) | 49.4 |
| Receiver waiting for data (`recv_wait`) | **105.2** |

The two sides' I/O alternated. The sender read a run, then waited on the socket while the receiver wrote and synced; the receiver then waited while the sender read the next run. Overlapped, the time is bounded by the slower side: max(about 99 s of reads, about 107 s of writes and syncs) plus about 5 s of fixed cost (the guard alone took 4.7 s), so **about 112 s**, or 0.68 of 164.8 s.

`--read-ahead THREADS` (built for this) reads the chosen runs on reader threads, in sending order, up to 64 MiB ahead of the sending thread. The source's reads then go on while the sending thread is blocked on the socket. **Does it reach the bound?**

A second question is about mechanism. At 28% of pages, chosen independently, the gaps between chosen pages are short: (1 − 0.28) ÷ 0.28, about 2.5 pages on average, so the 619,251 pages fall in about 440,000 runs.
- **One reader at queue depth 1** read those runs in about 99 s: about 4,450 a second, or 225 µs each. That's faster than a network disk usually answers a single read, and more than the disk's documented 3,900 IOPS (balanced, 150 GiB: 3,000 plus 6 per GiB).
- **The likely reason** is the kernel's read-ahead on a forward-moving reader: it fetched most of each file at the disk's sequential rate (EXP-48: 209.5 MB/s), and 16 GB at that rate is about 80 s (**inferred**).
- **If so,** one reader thread is enough: the gain comes from overlap, not from parallel reads.
- **Eight threads** could do better if reads were latency-bound, or worse if interleaved reads defeat the kernel's read-ahead.

Arms with 1 and 8 threads tell these apart.

## Design

EXP-64's set-up, unchanged: `experiments/network/lab_exp64.sh` now takes `PATTERNS` and `ARMS`, and its defaults reproduce EXP-64's plan exactly (checked).
- **The VMs:** the `ha` set in us-west1 (EXP-64's revision), two `n2d-highcpu-8` (8 vCPUs, 8 GB), each with a 150 GiB balanced persistent disk, unthrottled; `blake3` on both.
- **The source:** EXP-48's database (scale 600, about 18 GB, its seed), PostgreSQL 17.11 or later, `summarize_wal = on` before B0 is taken.
- **The cell:** uniform only (1% of rows, EXP-48's workload and seed, about 28% of pages). B0 is a `pg_basebackup` with its manifest, taken before the change.
- **Each run** starts from a fresh copy of B0, with both VMs' caches dropped. All pgdelta arms are live, in place, with `--jobs 4 --hash blake3`. The arms:
  - **hash:** hashing both copies (EXP-64's `hash`);
  - **fast:** `--fast-path summaries`, reads in the sending thread (EXP-64's `fast`; `--read-ahead 0`);
  - **fast1:** the same with `--read-ahead 1`;
  - **fast8:** the same with `--read-ahead 8`;
  - **full:** one `pg_basebackup -X stream -c fast` into a new directory.
- **The schedule:** 2 runs per arm, in a shuffled order (seed 641): `PATTERNS=uniform ARMS="hash fast fast1 fast8 full"`, `lab_exp64.sh SOURCE 2 641`, results in `exp64b`.
- **Correctness:** each fast copy is started (recovering from its backup_label), and its `pgbench_accounts` row count and sum of `abalance` must equal the source's.
- **Recorded per run:**
  - the sender's `hash_source` and blocked sending (`page_send_s`);
  - the time its sending thread waited on the reader threads (`fast_path.read_ahead.wait_seconds`);
  - the receiver's `recv_wait`, `rebuild` and `fsync`.
- **A smoke first** at scale 20 (one run per arm), which decides nothing.

**The analysis,** committed before the data: `analysis/exp64b.py`, figure `exp64b-read-ahead`, data `docs/experiments/data/exp64b-cells.csv`. Medians of 2.

## Predictions

1. **Deciding: `fast8` takes at most 0.80 of `fast`'s time,** in this session. The bound gives 0.68. A bar at 0.80 still requires the overlap to recover more than half of the roughly 53 s between EXP-64's time and the bound.
2. **`fast8` takes at most 0.75 of hashing's time.** This is EXP-64's failed prediction 3, re-tested; the bound gives 0.62.
3. **The receiver becomes the bottleneck:** in `fast8` it waits for data for at most 30 s in all (EXP-64: 105.2 s).
4. **Mechanism: overlap, not parallel reads.** `fast1` and `fast8` are within 15% of each other.
5. **Every fast run uses the fast path, and every fast copy is correct:**
   - the guard passed on B0's `backup_manifest`;
   - the copy recovered and matched the source;
   - every run exited 0.
6. **Control:** `fast` is within 15% of EXP-64's 164.8 s, and `hash` within 20% of its 180.1 s.

**Reported, not predicted:** each fast arm against the full copy. Even at the bound, the fast path stays about 1.2 times a full copy here, at the 28% crossover the bounds put it on (research loop 1, section 3).

**Rule 11:** a deciding ratio within 0.05 of its bar gets a second session.

## What follows

- **If 1 holds:** the read side is solved, and the fast path's remaining gap to a full copy on scattered changes is the receiver's scattered writes.
  - Read-ahead becomes the fast path's default, at the smaller thread count if 4 holds. Before that, the smoke's in-memory runs and the hot cell (EXP-64: 1.98 s) are re-checked for any loss.
  - The `fio` sweep for gap-filled writes (research loop 1, idea 2) is next, because the writes then bind.
- **If 1 fails while 3 holds:** reads overlapped but something else took their place; the timers say what.
- **If 1 and 3 both fail:** the source's reads are slower than one-at-a-time reads with threads, or are still the bottleneck. The comparison of `fast1` and `fast8` says which, and whether the kernel's read-ahead is the cause.
- **If 4 fails with `fast8` faster:** parallel reads matter beyond overlap, and the thread count is worth a sweep.
- **If 4 fails with `fast8` slower:** interleaved reads defeat the kernel's read-ahead, and one reader thread (or reading whole extents, ROMIO-style data sieving on the read side) is the design.
