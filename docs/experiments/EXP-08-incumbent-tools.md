# EXP-08: How do the incumbent refresh tools compare where pgdelta claims its edge?

Status: **pre-registered October 23, 2026**, before the tooling for it ran. This is test T11 of the [decision ledger](../DECISIONS.md#t11-incumbent-tools-where-pgdelta-claims-its-edge). Its first three predictions were written there on October 23; the rest are added here, also before running.

## Question

pgdelta's claimed edge has two halves, and neither has been tested against a real tool:
- **Correctness.** [EXP-03](EXP-03-tricky-copies.md) showed that a bare LSN shortcut keeps stale pages after a failover. But real catch-up tools check the cluster's identity and timeline, so they may refuse that case rather than corrupt it. And `pg_rewind`, which AlloyDB Omni already uses for DR, was never run.
- **Speed and bytes against a repository.** Omni's built-in backups are pgBackRest, and pgBackRest's `restore --delta` reuses an old copy's files whose checksums still match. Where a cluster has a repository, that is the obvious alternative.

## Part A: correctness on tricky old copies

**Setup.** One Debian 12 VM, PostgreSQL 17.11 from PGDG. EXP-03's scenario builder, with one new scenario:

| ID | Old copy B | Source |
|---|---|---|
| S1 | A standby that stopped following the primary, same timeline | The primary, after more writes |
| S2 | An ex-primary that kept writing after a failover, then stopped cleanly | The promoted standby (timeline 2), after more writes |
| **S2b** (new) | **As S2, but the ex-primary never checkpoints after the fork.** Its diverged pages are forced to disk without a checkpoint, then it is stopped with `-m immediate`. So B's checkpoint is from before the fork, and some of its pages hold writes from after it. | As S2 |

**Tools**, each refreshing a fresh copy of B from the live source:
- `pg_rewind --write-recovery-conf`. For S2b it first runs crash recovery itself.
- `pg_probackup catchup --backup-mode=DELTA`.
- WAL-G catchup (`catchup-push --from-lsn <B's checkpoint REDO LSN>`, then `catchup-fetch`).
- pgdelta, as the reference.

**Each run is classified** as refused, failed, or accepted. An accepted copy is brought up the way its tool intends, then checked three ways:
- every user table's row count and md5 against the source's state;
- `pg_amcheck --heapallindexed`;
- for S2b, how many of B's post-fork pages still hold B's diverged bytes.

Runner: `experiments/incumbents/run_t11.py`.

### Predictions

| ID | Prediction | Source |
|---|---|---|
| I1 | The catch-up tools accept S1 and produce a correct copy. | ledger |
| I2 | The catch-up tools refuse S2. | ledger |
| I3 | The catch-up tools accept S2b and keep some stale pages: diverged pages left, or a logical mismatch. | ledger |
| I4 | `pg_rewind` repairs S2 and S2b: after replaying the source's WAL, the logical state matches. | ledger |
| I5 | `pg_rewind` on S1 reports that no rewind is needed; the copy then catches up by replay alone. | added here |
| I6 | pgdelta is correct on S2b, as it was on S1 and S2 in EXP-03. | added here |

**Written before running about the catch-up tools.** WAL-G's checks are less documented than `pg_probackup`'s. WAL-G takes the starting LSN from the user, so it may not detect a fork at all. I2 and I3 are kept for both tools as registered.

**What would change the plan (from the ledger):**
- If the catch-up tools refuse S2b, or repair it correctly, pgdelta has no measured correctness edge over them, only wider reuse.
- If `pg_rewind` repairs S2 and S2b, then for Omni's DR path, where rewind is tried first, pgdelta's role is the fallback when rewind can't run, such as missing WAL. Its job there is not to replace rewind.

## Part B: pgBackRest delta restore over the network

**Setup.** The two `n2-standard-8` VMs of EXP-04b, with the same data, workloads, seed, cache drops, link shaping and wire-byte counting. The repository is on the source VM. The destination restores over SSH, `process-max=4`. Repository variants:

| Method | Compression | Block incremental |
|---|---|---|
| `pgbackrest_zst` | zstd (Omni's standalone sample uses it) | no |
| `pgbackrest_none` | none | no |
| `pgbackrest_block` | zstd | yes (`repo-block=y`, `repo-bundle=y`) |

**The backup counts as already paid.** One full pgBackRest backup per variant is taken after the workload, untimed. Each timed run restores it with `--delta` onto a fresh, untimed copy of B0, after the cache drop. The restored copy is then started untimed, recovering through `archive-get`. Its per-table row counts and md5s are compared with the source.

**In the same sweep:** the full copy, staged pgdelta (as in EXP-04b), and pgdelta's v1 path (`pgdelta_direct_inplace`, see [EXP-09](EXP-09-direct-read.md)). Speeds 0.5 Gbit/s, 2 Gbit/s and unthrottled; uniform, hot and append at 1%; 3 repeats; random order.

### Predictions

| ID | Prediction | Source |
|---|---|---|
| I7 | pgBackRest beats staged pgdelta on time for local changes (hot, append). | ledger |
| I8 | pgBackRest sends more bytes than pgdelta for scattered changes (uniform). | ledger |
| I8a | **Without compression** (`pgbackrest_none`), I8 holds: it restores whole changed files, about 2.1 GB, against pgdelta's 610 MB. | added here |
| I8b | **With zstd** (`pgbackrest_zst`), I8 fails: it sends fewer bytes than pgdelta for uniform, because pgbench's padded rows compress unusually well. | added here |
| I9 | `pgbackrest_block` sends at most half of `pgbackrest_zst`'s bytes for local changes. | added here |
| I10 | For uniform changes, pgdelta's v1 path is faster than `pgbackrest_zst` at every speed. | added here |
| I11 | `pgbackrest_zst` beats the full copy at every speed for local changes. | added here |

**Written before running about I8.** The ledger's I8 didn't consider compression. Split into I8a and I8b, it can be judged on what it was about (whole-file restores) without compression confounding it. I8b is the honest expectation for pgbench's data. Realistic data (TPROC-C) is left to T2 and T4.

**What would change the plan:**
- **If pgBackRest is within 10% of pgdelta's v1 path on time at every speed and sends no more bytes,** pgdelta's case for Omni clusters that have a repository rests on what pgBackRest can't do. HA and DR seeding bypass the repository, and a refresh straight from the live server needs no recent backup. The pitch becomes "no repository needed".
- **If pgdelta's v1 path is faster by more than 20% at every speed,** it is worth using even where a repository exists.
- **I8 (registered):** if pgBackRest also wins on bytes for scattered changes, the pitch must be "no repository needed". Judged on `pgbackrest_none` (I8a) and `pgbackrest_zst` (I8b) separately.

## Limitations, known before running

- **"Already paid" favours pgBackRest.** A real repository's newest backup can be up to a day old (Omni's default is a daily full). A restore would then also replay up to a day of WAL. Here the backup is taken right before each restore.
- **SSH encrypts pgBackRest's traffic; pgdelta v0's is unencrypted.** pgBackRest's CPU numbers include the cipher.
- **pgbench's padded rows compress unrealistically,** which helps the zstd variants (I8b).
