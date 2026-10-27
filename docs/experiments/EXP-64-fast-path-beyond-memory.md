# EXP-64: Beyond memory, how much faster is the fast path than hashing, and than a full copy?

Status: **pre-registered October 27, 2026, before any run.** One VM pair (the `ha` set), about 2 hours with the data load. The owner asked for the fast path to be built and timed beyond memory (K11).

## Question

Beyond memory, pgdelta's hashing path is bound by reading B. EXP-48's model put v1 in place at |B| ÷ read rate + pages written ÷ 6,750 a second:
- 87.5 s for hot changes at 18 GB on 8 GB VMs, against 90.5 s for one `pg_basebackup`: "at best a tie";
- 184.6 s for uniform changes.

The fast path (`--fast-path summaries`, built after EXP-61 to EXP-63) reads neither copy's unchanged pages. If the model is right, the read term disappears and only the changed pages remain. Measured, how much faster is it?

## Design

EXP-48's set-up, run by a lab script (`experiments/network/lab_exp64.sh`) because the fast path needs a live source:
- **The VMs:** the `ha` set, two `n2d-highcpu-8` (8 vCPUs, 8 GB) in two zones, each with a 150 GiB balanced persistent disk, unthrottled. `blake3` is installed on both.
- **The source:** EXP-48's database (`source_cluster.py setup`, scale 600, about 18 GB, its seed), running, with `summarize_wal = on` set before any old copy is taken. PostgreSQL 17 from PGDG; the script fails if it is older than 17.11.
- **Per pattern** (hot, then uniform; 1% of rows, EXP-48's workload and seed):
  1. B0, a `pg_basebackup` of the source with its manifest, is taken before the change;
  2. the change runs;
  3. the timed runs.
- **Each run** starts from a fresh copy of B0, with both VMs' page caches dropped:
  - **hash:** pgdelta live, in place, `--jobs 4 --hash blake3`, hashing both copies;
  - **fast:** the same with `--fast-path summaries`;
  - **full:** one `pg_basebackup -X stream -c fast` into a new directory, as EXP-48's `full`.

  Each arm runs 2 times per pattern, in a shuffled order (seed 640).
- **Correctness:** each fast copy is started (recovering from its backup_label), and its `pgbench_accounts` row count and sum of `abalance` must equal the source's.

**The analysis,** committed before the data: `analysis/exp64.py`, figure `exp64-beyond-memory`. Medians of 2.

## Predictions

1. **Deciding: hot changes, the fast path takes at most 0.35 of the hashing path's time.** The model: the hashing path pays about 86 s reading B; the fast path writes the roughly 14,000 changed pages (EXP-48: 13,935) in about 2 s, plus fixed costs of a few seconds.
2. **Hot changes, the fast path takes at most 0.35 of the full copy's time.** This is the HA case where EXP-48 found "at best a tie".
3. **Uniform changes, the fast path takes at most 0.75 of the hashing path's time.** Its write term stays (EXP-48: 619,856 pages, about 92 s at 6,750 a second); only the read of B goes.
4. **Every fast run uses the fast path, and every fast copy is correct:**
   - the guard passed, proved by B0's `backup_manifest`;
   - the copy recovered and matched the source;
   - every run exited 0.
5. **Control:** the hashing path is within 20% of EXP-48's v1 times (87.5 s hot, 184.6 s uniform), so this session is comparable with that one.

The uniform comparison with the full copy is reported, not predicted. **Rule 11:** a deciding ratio within 0.05 of its bar gets a second session.

## What follows

- **If 1 and 2 hold:** beyond memory, for local changes, a refresh with provable lineage is several times faster than both hashing and a full copy. RESULTS' HA row changes from "at best a tie", with its conditions:
  - `summarize_wal` on;
  - a cleanly stopped or base-backup old copy;
  - PostgreSQL 17.11 or later.

  The Omni operator's practice (EXP-44: it deletes the old disk) is unchanged, so the HA case on Omni still needs that disk kept.
- **If 1 fails:** the fast path's own costs (the summaries, the guard, B's facts, the backup's start and stop) are read from its statistics, and the model is restated.
- **If 3 fails:** the uniform case is bound by something besides the read; the write term is re-measured.
