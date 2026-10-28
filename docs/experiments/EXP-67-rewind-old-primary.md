# EXP-67: For an old primary after a failover, does pg_rewind beat pgdelta, and by how much?

Status: **pre-registered October 28, 2026, before any run.** One VM pair (the `ha` set), about 2 hours, after EXP-64b and EXP-66. Research loop 1 ranked this first among the competitors never timed: item 1(a). EXP-65 was 1(b).

## Question

After a failover, the old primary B has written a little that the new primary S never saw, and S has moved on. The standard way back is `pg_rewind`, which Patroni, CloudNativePG, Crunchy PGO and Spilo all use. It does three things:
1. reads B's own WAL since the last common checkpoint for the blocks B changed;
2. copies those blocks, plus every non-relation file and S's WAL, from S;
3. makes B replay S's WAL from that checkpoint as a standby.

**pgdelta can't use its fast path here.** Its guard refuses a B whose history left S's: EXP-61 and EXP-63 showed this is necessary, since summaries alone would miss B's own changes. So pgdelta hashes both copies. Beyond memory, hashing costs about as much as a full copy (EXP-48, EXP-64).

Research loop 1 inferred that `pg_rewind` "probably wins wherever it can run". This measures it, at 18 GB beyond memory, for two shapes of S's lag:
- the same hot rows updated 10 times, where replay pays for many WAL records per page;
- 1% of rows scattered, where replay waits on reads (EXP-65: 31 MB/s of WAL).

The answer decides research loop 1's item 3. If `pg_rewind` beats hashing, idea 1 is worth prototyping: the union of both sides' block sets (B's from its WAL, S's from its summaries), sent once and never replayed.

## Design

The `ha` set as in EXP-64 and EXP-65: two `n2d-highcpu-8` in us-west1 with 150 GiB balanced persistent disks, and EXP-48's database (scale 600, about 18 GB), PostgreSQL 17.11 or later.
- **Data checksums** are on (the clusters' default), so `pg_rewind` can run.
- **`wal_keep_size = 40GB`** on the source, so S keeps its WAL since the fork. In practice a replication slot or an archive serves the same need.

Script: `experiments/network/lab_exp67.sh`.

**The failover, built afresh for each lag, in the real orientation** (B on the old timeline, S on the new one):
1. On the source VM, a standby Q of the current primary P catches up, then stops cleanly.
2. P updates 10,000 rows (every 6,000th account) that Q never sees, then stops cleanly. This is the fenced old primary.
3. P's data directory goes to dest as **B0**.
4. Q, promoted onto a new timeline, takes P's place as **S**.
5. S runs the lag:
   - **hot10:** EXP-48's 1% hot rows updated 10 times;
   - **uniform:** 1% of rows once.
6. S's LSN and `pgbench_accounts` state are recorded.

**Each run** starts from a fresh copy of B0, with both VMs' caches dropped, and is timed until the copy is current:
- **rewind:** `pg_rewind --source-server` S `--write-recovery-conf` on dest, then B started as S's standby until it has replayed to S's LSN. Its own share (`pg_rewind` alone) is timed too.
- **fast:** pgdelta live, in place, `--fast-path summaries --hash blake3 --jobs 4`, then the copy started until it accepts connections. The guard is expected to refuse, so the session hashes both copies. That is what pgdelta would do here.
- **full:** one `pg_basebackup -X stream -c fast` into a new directory, then started the same way.

**Correctness:** each copy's `pgbench_accounts` row count and sum of `abalance` must equal S's. B's own 10,000 changed rows must be gone.

2 repeats per arm and lag, shuffled (seed 670). **A smoke first** at scale 20 (one run per arm), deciding nothing. **The analysis,** committed before the data: `analysis/exp67.py`, figure `exp67-rewind`, data `docs/experiments/data/exp67-cells.csv`.

## Predictions

The model comes from EXP-64 and EXP-65's measurements.
- **pgdelta hashing:** about 88 s for hot10 and about 180 s for uniform (EXP-64's hashing arm).
- **`pg_rewind`:** copies B's 10,000 changed blocks and S's WAL since the fork (about 1.8 and 8 GB), then replays that WAL at EXP-65's rates (89 and 31 MB/s). That gives about 30 s for hot10, and over 280 s for uniform.

1. **Deciding: for hot10, `pg_rewind` reaches a current copy in at most 0.6 of pgdelta's time.** The model gives about 0.35.
2. **For uniform, pgdelta takes at most 0.85 of `pg_rewind`'s time.** Replaying scattered WAL beyond memory is slower than reading B once. The model gives about 0.65.
3. **Every copy is correct and every fast run was refused by the guard:**
   - every copy is current, with B's own changes gone;
   - every pgdelta run was refused for B's history (its reason naming the timeline or history), so it hashed both copies;
   - every run, and every pgdelta receiver, exited 0.

**Reported, not predicted:** the re-clone against both. EXP-65 suggests a re-clone (about 91 s) beats both for uniform.

**Rule 11:** a deciding ratio within 0.05 of its bar gets a second session.

## What follows

- **If 1 holds:** `pg_rewind` beats pgdelta on its home ground, as research loop 1 inferred. Idea 1 is then prototyped and pre-registered against `pg_rewind`: the union of B's blocks since the fork (from B's WAL) and S's blocks since then (from S's summaries), sent once from S and never replayed. Its model is EXP-65's: about 3 s against `pg_rewind`'s replay for hot10.
- **If 1 fails:** hashing is competitive even here, and idea 1's case rests on scattered lags (prediction 2).
- **If 2 holds:** for scattered lags pgdelta's hashing beats `pg_rewind`. A re-clone may still beat both, which is the case for the selector (idea 4).
- **If 3 fails for the guard:** the guard let a diverged B through, or refused for the wrong reason. That's a correctness finding, investigated before anything else.
