# EXP-57: Does a fast start checkpoint on a large dirty set hurt a busy primary?

Status: **pre-registered October 27, 2026, before any run.** One VM (about an hour), no network. The C-ckpt row of `docs/FIRMNESS.md`, sized to its one question.

## Question

pgdelta reads a live primary between `pg_backup_start` and `pg_backup_stop`. It asks for a **fast** checkpoint (`--checkpoint fast`, the default), as `pg_basebackup -c fast` does. A fast checkpoint writes every dirty buffer at once. `pg_basebackup`'s own default is **spread**, which paces the same writes over `checkpoint_completion_target` of `checkpoint_timeout`.

Every slowdown test so far (EXP-12, -28, -30, -39) used a load database of about 30 MB (`pgbench -s 2`), so the start checkpoint never had much to write. On a primary with gigabytes of dirty buffers on a persistent disk, a fast checkpoint may saturate the disk that also holds the WAL, and stall commits. How much, against a spread checkpoint and against none?

## Design

`experiments/network/lab_exp57.sh`, run on one VM of the `ha` set:
- **The machine:** `MACHINE=n2-standard-8` (8 vCPUs, 32 GB) with its 150 GiB balanced persistent disk. That is EXP-48's disk, which wrote about 6,750 scattered pages a second. Data and WAL are both on it, as PostgreSQL's defaults put them.
- **The cluster:** PostgreSQL 17, `pgbench -i -s 400` (about 6 GB, all of it fitting in shared buffers). Settings:
  - `shared_buffers = 8GB`;
  - `max_wal_size = 32GB` and `checkpoint_timeout = 5min` (the default), so no checkpoint but the arm's own falls inside a trial;
  - `checkpoint_completion_target = 0.9` (the default);
  - `log_checkpoints = on`;
  - the database prewarmed into shared buffers (`pg_prewarm`).
- **The load:** pgbench's TPC-B script, 16 clients on 4 threads, open loop at 0.6 × C. C is the median of three 30-second closed-loop runs after a 30-second warm-up. Every transaction is logged (`-l`).
- **A trial:**
  1. `CHECKPOINT`, so every trial starts clean;
  2. the load at 0.6 × C for 180 s, to build the dirty set;
  3. the dirty buffers counted (`pg_buffercache`), then the arm at t0;
  4. 10 s of tail.

  The load runs 500 s in all, covering the slowest arm.
- **Arms**, each in one session:
  - `none`: 60 s of nothing;
  - `fast`: `pg_backup_start('exp57', true)`, then `pg_backup_stop(false)`;
  - `spread`: the same with `false`.

  Its start returns when the checkpoint is done (t1). 2 repeats of each, in a shuffled order (seed 570).
- **Measures** (EXP-12's, from the transaction log in 1-second buckets over [t0, t1 + 10 s]; `none`: [t0, t0 + 70 s]):
  - **D:** the worst drop, max(1 − completed ÷ scheduled);
  - **W:** the work lost, the sum of max(0, scheduled − completed) ÷ the rate, in seconds;
  - the p99 latency of the transactions that completed in the window.

  Net values subtract the `none` arm's medians. Also recorded: the buffers each checkpoint wrote and its write and sync times (from `log_checkpoints`), and how long `pg_backup_start` blocked.
- **The analysis,** committed before the data: `analysis/exp57.py`, figure `exp57-checkpoint-burst`.

## Predictions

1. **The dirty set is large** (the manipulation check): at t0, at least 1 GB of dirty buffers in every trial.
2. **Deciding: a fast checkpoint hurts the load far more than a spread one.** Medians:
   - fast's net worst-second drop D is at least 0.30;
   - fast's net work lost W is at least 2× spread's.
3. **A fast start blocks for a while:** `pg_backup_start(…, true)` takes at least 20 s, where it took well under a second on EXP-28's small dirty set.

## What follows

- **If 1 and 2 hold:**
  - **README and help:** advise `--checkpoint spread` on a busy primary with a large dirty set on persistent disks.
  - **The trade-off, with numbers:** fast starts reading at once but stalls the load; spread is gentle but adds up to 0.9 × `checkpoint_timeout` before reading.
  - **The default:** goes to the owner. O4's bar ("no worse than `pg_basebackup`") points to spread, `pg_basebackup`'s own default.
- **If 2 fails:** fast stays the default, with this measurement behind it.
- **If 1 fails:** the dirty set wasn't large, and nothing is concluded about it.
