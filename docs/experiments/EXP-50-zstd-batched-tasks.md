# EXP-50: Do larger pool tasks lower zstd's floor?

Status: **pre-registered October 27, 2026, before the change was measured.** Local (the Mac), end to end with `pgdelta local`. Lean: 10 runs of a few seconds each, after a template build. The fix EXP-49 pointed to.

## Question

EXP-49 found that zstd's floor comes from pgdelta's thread pools, not the codec. Each pool task costs about 12–13 µs on the Mac. EXP-47's runs average 2.5 pages, so the pools stop gaining at 1.5–1.7 GB/s, and with 4 threads decompressing, each of the receiver's writes took 6.4× as long. If each task takes many runs instead of one, the handoffs fall about fiftyfold. Does that remove zstd's extra time?

## The change

- **The sender** queues runs and hands its compressing pool one task per `TASK_BYTES` of pages (1 MiB), or less at a file's end. The task compresses each run separately, so every run still goes as its own `PAGE_DATA_ZSTD` frame (or `PAGE_DATA` if it didn't shrink).
- **The receiver** gathers arriving compressed frames into one task per `TASK_BYTES ÷ 4` of frames: 256 KiB, about 0.7 MiB of pages at zstd's 2.8×. Its rules stay as they were:
  - every queued run is written, in order, before any other message is handled;
  - a run that decompresses past its file is refused.
- **Nothing on the wire changes,** and a session without `--compress` doesn't touch either path.
- **For this comparison only,** the environment variable `PGDELTA_ZSTD_TASK_BYTES=0` restores one run per task (EXP-47's code path).

## Design

`experiments/bytes/exp50.py`:
- **The data:** EXP-37's template (pgbench with hex filler) at **scale 70**, as EXP-47's data, and EXP-37's uniform update (1% of rows at random), stopped cleanly. This is S; B is the template before the update.
- **The run:** `pgdelta local --in-place --source-kind datadir --jobs 4`, with B cloned fresh before every run (APFS clones), and its statistics written (`--stats-json`).
- **Arms:**
  - `plain`: v1 without compression;
  - `zstd_per_run`: `--compress zstd` with `PGDELTA_ZSTD_TASK_BYTES=0`;
  - `zstd_batched`: `--compress zstd`, the new default.
- **Order:** one untimed warm-up run, then 3 repeats. Each repeat runs all three arms, in a rotated order (repeat 1: plain, per run, batched; repeat 2: per run, batched, plain; repeat 3: batched, plain, per run).
- **Measures,** all from pgdelta's own statistics:
  - its total time;
  - the receiver's `rebuild` (its time writing pages);
  - the sender's and the receiver's waits for their pools;
  - the files digest.
- **The analysis,** committed before the data: `analysis/exp50.py`, figure `exp50-batched-tasks`.
- **A smoke at scale 1** checked the plumbing before this was committed:
  - all 10 runs completed;
  - with `PGDELTA_ZSTD_TASK_BYTES=0`, both ends ran one task per run (436 of 436);
  - batched, they ran 26 and 30 tasks.

  At that scale each run takes about 0.35 s, too short to say anything about time, and the predictions weren't changed after it (`results/bytes/exp50-smoke`).

## Predictions

1. **Deciding: batching cuts zstd's extra time over plain v1 by at least half** (medians): `zstd_batched − plain ≤ 0.5 × (zstd_per_run − plain)`. If `zstd_per_run` is less than 0.3 s slower than `plain`, there is too little to cut locally, and 1 is reported as undecided.
2. **The receiver's writes:** `rebuild` takes at least 1.5× plain's with one run per task, and at most 1.3× plain's when batched.
3. **Correctness:** every run completes verified, and all three arms produce the same files digest.

## What follows

- **If 1 holds:** batching stays as the default; the code, tests and README note go in with this write-up.
  - The VM re-measure at 2 Gbit/s (EXP-47's cells) waits for the cloud sign-in. Its registered prediction will be that zstd at 2 Gbit/s falls below plain v1's 5.9 s.
- **If 1 fails or is undecided:** the batching is reverted, and what remains is reported. Candidates are the per-run work inside each task and each end's main thread.
- **If 3 fails anywhere,** the change is reverted, whatever 1 says.
