# EXP-76: Does early write-back on the sync worker cut the fast path's tail, and where does the rest of its time beyond the disks go?

Status: **pre-registered October 28, 2026, before any run.** One `ha` VM pair, about 1.5 hours with the data load. It follows EXP-75, where the fast path's fixed costs, not the disks, put `pg_basebackup` ahead at 96% of the pages changed.

## Question

In EXP-75's `scatterall12` cell, the fast path took 112.99 s against `pg_basebackup`'s 100.94 s. Both kept the disks busy for the same 95 s. The fast path's runs (EXP-74 and EXP-75 together, 16 runs in three cells) split very evenly:
- **launch:** 0.52–0.58 s from the run's start to `pg_backup_start`;
- **the guard:** 1.9 s (uniform), 2.9 s (scatter12) and 5.3 s (scatterall12), reading the WAL summaries;
- **the tail:** after `pg_backup_stop`, 3.5–4.1 s, nearly all of it the receiver waiting for its sync worker at COMPLETE (`fsync_wait_seconds`, 3.1–3.8 s).

**The tail's mechanism (from the code and the receiver's statistics).** With `--fsync worker` the receiver writes through the page cache, and a thread fsyncs each finished file. At COMPLETE it waits for the last fsyncs, which must flush whatever dirty data the kernel has not yet written: a few hundred MB on these 8 GB VMs, about 3.5 s at 210 MB/s. `--fsync writeback` starts write-back every 8 MiB written (`sync_file_range`), so little is left dirty, but it fsyncs in the main loop, which EXP-68 found slows the fast path.

**Built for this experiment:** `--fsync worker-writeback`, the worker with early write-back (`pgdelta/flush.py`, `receiver.py`). Where `sync_file_range` is missing or refused it is the worker, and the statistics say so. Tests cover every mode's output and statistics, the calls every 8 MiB and the fallback; the real call is tested on Linux in CI.

**The rest is unlocated.** Within backup mode, besides the guard, about 8 s at `scatterall12` passed without the source disk busy. The statistics can't say where. So this session also records a timeline:
- **both disks:** `/proc/diskstats` every 0.25 s on both VMs (`experiments/network/disk_series.sh`);
- **the sender's phases:** three new wall-clock times in its statistics (`fast_path.guard_started_utc`, `sending_started_utc`, `sending_ended_utc`), beside `pg_backup_start` and `pg_backup_stop`.

**The question: does `worker-writeback` cut the tail by at least 2 s without slowing the transfer, and where on the timeline does the rest of the fast path's time beyond the disks go?**

## Design

**EXP-75's lab and set-up** (`experiments/network/lab_exp64.sh`, `SERIES=1`):
- the `ha` set in us-west1: two `n2d-highcpu-8` VMs with 150 GiB balanced persistent disks, `blake3` on both;
- EXP-48's 18 GB database, PostgreSQL 17.11, `summarize_wal` on.

**Two cells,** as EXP-75's, each with B0 a base backup taken just before its change:
- **`uniform`:** 1% of `pgbench_accounts`' rows in random batches, bound by requests. **Arms:** `fast8wf2` (worker, fill 2) and `fast8bf2` (worker-writeback, fill 2).
- **`scatterall12`:** 12% of both tables' rows, 96% of the pages, bound by bytes. **Arms:** `fast8w` (worker, fill 0), `fast8b` (worker-writeback, fill 0), and `full` (`pg_basebackup`, for its timeline).

**The lab's change:**
- an arm letter `b` in place of `w`, for `--fsync worker-writeback`;
- `SERIES=1`: both VMs sample their disk from 0.5 s before t0 to 0.5 s after t1. The samples are outside the timing.

**Every fast arm** pins `--read-ahead 8` and `--no-diverged-basis`, and its fill.

**Runs:** 2 per arm, shuffled per cell (seed 760); 10 runs.

**The command:**

    PATTERNS="uniform scatterall12" ARMS_uniform="fast8wf2 fast8bf2" ARMS_scatterall12="fast8w fast8b full" SERIES=1 OUT=/data/exp04-results/exp76 lab_exp64.sh SOURCE 2 760

**A smoke first,** at scale 20 (one run per arm, seed 761, deciding nothing). Every run must exit 0, every fast copy must match, every fast run must use the fast path, and every run must have both disk series.

**The analysis,** committed before the data: `analysis/exp76.py`. Figure `exp76-timeline` (`scatterall12`'s first run of each arm: the source's reads and the destination's writes against time, with the guard, the plan and the tail shaded). Data: `docs/experiments/data/exp76-cells.csv` and `exp76-phases.csv` (per fast run and phase: seconds, each disk's busy share and MB/s).
- **Dry run:** on stand-in results built from EXP-75's runs: arms relabelled, the new timestamps estimated from backup mode, and a synthetic series busy at 210 MB/s while sending. The parsing, the phases, the scoring and the figure worked.
- **The stand-in arms were all `worker`,** so predictions 1 and 2 failed there, as they must. The dry run's outputs were removed.

## Predictions

1. **Deciding: in each cell, `worker-writeback`'s median is at least 2.0 s below `worker`'s.** The tail now takes 3.5–4.1 s; the model is that early write-back leaves under 1 s of it, and the transfer, bound by the disks, is unchanged.
2. **The receiver's waits for its worker** (`fsync_wait_seconds`) are below 1.0 s in every `worker-writeback` run (EXP-75's `worker` runs: 3.1–3.8 s), with `worker-writeback` in effect.
3. **The transfer isn't slowed:** backup mode (`pg_backup_start` to `pg_backup_stop`) with `worker-writeback` is within 3% of `worker`'s, in each cell.
4. **The guard uses neither disk:** both disks under 10% busy during the guard, in every fast run.
5. **Every copy is correct:** every run exits 0, and every fast run used the fast path and passed the copy's check.

**Reported, not predicted:**
- each phase's seconds and each disk's busy share and MB/s, per arm;
- `pg_basebackup`'s timeline beside the fast path's: when its reads start and end, and its own tail;
- where in the sending phase the source disk is not busy (the about 8 s EXP-75 left unlocated).

**Rule 11:** a cell whose saving lies within 0.5 s of the 2.0 s bar (1.5–2.5 s) gets a second session.

## What follows

- **If 1–3 hold:** `worker-writeback` becomes the fast path's `--fsync auto` choice where `sync_file_range` exists, as the worker became in EXP-68. The README and the defaults say so.
- **If 1 fails with 2 holding:** the tail isn't the final syncs. The timeline says what it is.
- **If 3 fails:** early write-back stalls the receiver's main loop on these disks (`sync_file_range` can wait for the device's queue). It stays opt-in.
- **Either way,** the timeline places the fast path's remaining fixed costs, so the next change targets the largest. The guard, at 5.3 s of `scatterall12`, is the known one.
