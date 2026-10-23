# EXP-08: How do the incumbent refresh tools compare where pgdelta claims its edge?

Status: **pre-registered October 23, 2026**, before the tooling for it ran. It was **revised the same day, still before running,** after an independent review; see [Revisions before running](#revisions-before-running-october-23-2026), and the first version is in git (commit 4cda743). This is test T11 of the [decision ledger](../DECISIONS.md#t11-incumbent-tools-where-pgdelta-claims-its-edge), whose registered predictions are marked "ledger" below.

## Question

pgdelta's claimed edge has two halves, and neither has been tested against a real tool:
- **Correctness.** [EXP-03](EXP-03-tricky-copies.md) showed that a bare LSN shortcut keeps stale pages after a failover. But real catch-up tools check the cluster's identity and timeline, so they may refuse that case rather than corrupt it. And `pg_rewind`, which AlloyDB Omni already uses for DR, was never run.
- **Speed and bytes against a repository.** Omni's built-in backups are pgBackRest, and pgBackRest's `restore --delta` reuses an old copy's files whose checksums still match. Where a cluster has a repository, that is the obvious alternative.

## Part A: correctness on tricky old copies

**Setup.** One Debian 12 VM with PostgreSQL 17.11 from PGDG, using EXP-03's scenario builder. The ledger planned this part for the Mac. It moved to a VM because `pg_probackup` and WAL-G aren't packaged for macOS. Each (scenario, tool) pair runs once: the builds are deterministic, and the exact tool versions are recorded.

| ID | Old copy B | Source |
|---|---|---|
| S1 | A standby that stopped following the primary, same timeline | The primary, after more writes |
| S2 | An ex-primary that kept writing after a failover, then stopped cleanly | The promoted standby (timeline 2), after more writes |
| **S2b** (new) | **As S2, but the ex-primary never checkpoints after the fork.** Its diverged pages are forced to disk without a checkpoint, then it is stopped with `-m immediate`. So B's checkpoint is from before the fork, and some of its pages hold writes from after it. | As S2 |
| **S2c** (new, added in revision) | **A standby that followed the ex-primary after the fork, stopped cleanly.** It replays the ex-primary's diverged writes. No checkpoint follows the fork, so its shutdown restartpoint keeps a REDO point from before the fork while flushing the diverged pages. Its state is "shut down in recovery". | As S2 |

**Exposed pages.** S2b and S2c only test LSN-based tools if some of B's post-fork pages are blocks the source hasn't changed since B's REDO point. A tool that sends only pages newer than B's REDO point would skip those blocks and keep B's diverged bytes. The builders record how many such pages exist, and the count must be above zero. To guarantee it, part of the diverged writes go to a table the source doesn't write after the fork.

**Tools**, each refreshing a fresh copy of B from the live source:
- `pg_rewind --write-recovery-conf`. For S2b, it first runs crash recovery itself.
- `pg_probackup catchup --backup-mode=DELTA`.
- **WAL-G `catchup-send` / `catchup-receive`.** This is WAL-G's catch-up path, and it checks the system identifier and timeline. A second WAL-G arm, `catchup-push --from-lsn` then `catchup-fetch`, takes the starting LSN from the user and checks nothing. It is reported, labelled "user-supplied LSN", but doesn't count toward I2 or I3.
- pgdelta, as the reference.

**Classification.**
- **Refused, for identity or history:** the tool declines because of a system-identifier, timeline or history mismatch. Only this kind counts as "refuses" in I2 and I3.
- **Refused, for another reason:** for example, a destination that wasn't shut down cleanly. It's recorded with the tool's message. When a tool refuses S2b only for that reason, its I3 verdict comes from S2c.
- **Failed:** an error after starting work.
- **Accepted.**

**Correct (repaired)** means all three checks pass once the copy has reached consistency the way its tool intends (for `pg_rewind`, replaying the source's WAL as a standby):
1. every user table's row count and md5 equal the source's;
2. `pg_amcheck --heapallindexed` finds nothing;
3. for S2b and S2c, none of B's post-fork pages still holds B's diverged bytes, counted after recovery and a checkpoint.

**WAL for S1.** The runner starts the source with `wal_keep_size` large enough to keep the WAL written since S1's standby stopped, so the copy can catch up by replay.

Runner: `experiments/incumbents/run_t11.py`.

**`pg_rewind` runs in two arms (added October 23, before the registered run).** A development run of the runner on the Mac, not the registered experiment, showed that `pg_rewind` depends on B still holding its own WAL back to the fork. At 200,000 rows, B's shutdown checkpoint had already recycled that WAL:
- on S2, `pg_rewind` refused with "could not find previous WAL record";
- on S2b, its own crash recovery removed the WAL it then needed.

So every scenario runs `pg_rewind` twice:
- **B as EXP-03 builds it,** with PostgreSQL's default WAL retention;
- **B with its WAL retained** (`wal_keep_size` of 4 GB on the old primary). This stands in for production's WAL archive with `--restore-target-wal`, which `pg_rewind`'s documentation names for this case.

I4 is judged on the retained arm, the configuration `pg_rewind` documents. The default arm is reported as what happens without retention. The other tools run on both builds too, so every tool sees the same copies.

### Predictions

| ID | Prediction | Source |
|---|---|---|
| I1 | The catch-up tools accept S1, and the copy is correct. | ledger ("accept S1"); "correct" added here |
| I2 | The catch-up tools refuse S2, for identity or history. | ledger |
| I3 | The catch-up tools accept S2b (or S2c, as above) and keep some stale pages: at least one of the three checks fails. | ledger; S2c added in revision |
| I4 | `pg_rewind` repairs S2 and S2b: the copy is correct as defined above. | ledger |
| I5 | `pg_rewind` on S1 reports that no rewind is needed; the copy then catches up by replay alone. | added here |
| I6 | pgdelta is correct on S2b and S2c, as it was on S1 and S2 in EXP-03. | added here |

**What would change the plan (from the ledger):**
- If the catch-up tools refuse S2b and S2c for identity or history, or repair them correctly, pgdelta has no measured correctness edge over them, only wider reuse.
- If `pg_rewind` repairs S2 and S2b, then for Omni's DR path, where rewind is tried first, pgdelta's role is the fallback when rewind can't run, such as missing WAL. Its job there is not to replace rewind.

## Part B: pgBackRest delta restore over the network

**Setup.** The two `n2-standard-8` VMs of EXP-04b, with the same data, workloads, seed, cache drops, link shaping and wire-byte counting. The repository is on the source VM. The destination restores over SSH, with `process-max=4`. The exact pgBackRest version and configuration are recorded.

| Method | Compression | Block incremental |
|---|---|---|
| `pgbackrest_zst` | zstd (Omni's standalone sample uses it) | no |
| `pgbackrest_none` | none, including on the network (`compress-level-network=0`) | no |
| `pgbackrest_block` | zstd | yes (`repo-block=y`, `repo-bundle=y`) |

**The backup counts as already paid.** One full pgBackRest backup per variant is taken after the workload, untimed. Each run then goes:
1. Copy B0 to a fresh B, untimed, **before** the cache drop.
2. The timed phase: `restore --delta` onto B.
3. Start the restored copy, untimed. It recovers through `archive-get`. The WAL fetched there is counted in pgBackRest's wire bytes, measured at the interface, so both pgBackRest and pgdelta pay for the WAL they need.
4. Compare the copy's per-table row counts and md5s with the trial's ground truth, taken from the source after every method has run, as the harness already does. The source isn't scanned during the trial, because a scan sets hint bits and writes WAL.

**In the same sweep:** the full copy, staged pgdelta (as in EXP-04b), and pgdelta's v1 path (`pgdelta_direct_inplace`, see [EXP-09](EXP-09-direct-read.md)). Speeds 0.5 Gbit/s, 2 Gbit/s and unthrottled; uniform, hot and append at 1%; 3 repeats; random order. Archiving is on for the whole sweep, so source CPU is compared only within this sweep, not with EXP-04b.

### Predictions

Every time and byte comparison uses per-workload medians of 3 runs at each speed. **`pgbackrest_zst` is the arm for I7, I8 and the rules,** because it is Omni's configuration. The other two variants explain the mechanism.

| ID | Prediction | Source |
|---|---|---|
| I7 | `pgbackrest_zst` beats staged pgdelta on time for local changes (hot, append). | ledger |
| I8 | pgBackRest sends more bytes than pgdelta for scattered changes (uniform). | ledger; judged on `pgbackrest_zst` |
| I8a | Without compression (`pgbackrest_none`), I8 holds: it restores whole changed files, about 1.05 GB (EXP-01's `pgbench_accounts` and its index), against pgdelta's 610 MB. A check of the mechanism. | added here |
| I8b | With zstd, I8 fails: `pgbackrest_zst` sends fewer bytes than pgdelta for uniform, because pgbench's padded rows compress unusually well. | added here |
| I9 | `pgbackrest_block` sends at most half of `pgbackrest_zst`'s bytes for local changes. | added here |
| I10 (revised) | For uniform changes, `pgbackrest_zst` is faster than pgdelta's v1 path at 0.5 Gbit/s. | added here |
| I10b (revised) | For local changes, pgdelta's v1 path is faster than `pgbackrest_zst` at 0.5 and 2 Gbit/s. | added here |
| I11 | `pgbackrest_zst` beats the full copy at every speed for local changes. | added here |

**What would change the plan** (revised):
- **I8 (registered), judged on `pgbackrest_zst`:** if it sends fewer bytes than pgdelta for uniform, as I8b expects, the rule fires. On bytes, the pitch to Omni users must be "no repository needed". Whether this depends on pgbench's compressibility is checked offline in T4, which now also measures zstd on the changed files of realistic (TPROC-C) pairs.
- **No edge where a repository exists:** if `pgbackrest_zst`'s median time is at most 1.1 times pgdelta's v1 path at every speed, in all three workloads, and it sends no more bytes, then pgdelta's case for Omni clusters with a repository rests on what pgBackRest can't do. HA and DR seeding bypass the repository, and a refresh straight from the live server needs no recent backup. The pitch becomes "no repository needed".
- **A clear edge:** if pgdelta's v1 path takes at most 0.8 times `pgbackrest_zst`'s time at every speed, in all three workloads, it is worth using even where a repository exists.
- **Mixed (the likely outcome):** otherwise, report for each workload the link speed at which the two cross. The recommendation for clusters with a repository follows that crossover.

## Revisions before running (October 23, 2026)

An independent reviewer checked the first version against the ledger and EXP-04b's data before anything ran. Changes:
- **S2b alone could be refused for the wrong reason.** `pg_probackup`'s catch-up probably requires a cleanly stopped destination in DELTA mode, and S2b is stopped with `-m immediate`. A refusal for that reason says nothing about fork detection. Hence the refusal classes and the cleanly stopped S2c.
- **I3 could fail by construction** if the source rewrote every page that B diverged on. Hence the exposed-page count.
- **The WAL-G arm tested its unchecked mode.** The research names `catchup-send`/`catchup-receive` as WAL-G's catch-up path, the one with identity and timeline checks.
- **"Repairs" and "stale pages" had no fixed definition or time.** They now mean the three checks, counted after recovery and a checkpoint.
- **`pgbackrest_none` would still have compressed on the wire.** pgBackRest compresses network traffic by default when the repository is remote.
- **The predictions and rules named no pgBackRest variant or statistic, and I10 contradicted I8b:** at 0.5 Gbit/s, sending fewer bytes makes zstd the faster one. The rules also missed the likely mixed outcome.
- **Splitting I8 had sidestepped its registered falsifier.** The rule is now judged on Omni's configuration.
- **I8a's 2.1 GB was wrong:** uniform touches one table of about 1.05 GB.
- **Copy order, source scans and WAL bytes are now stated:** B0 is copied before the cache drop; the source isn't scanned during the trial; pgBackRest's WAL is counted.

## Limitations, known before running

- **"Already paid" favours pgBackRest.** A real repository's newest backup can be up to a day old (Omni's default is a daily full), and a restore would then also replay up to a day of WAL. Here the backup is taken right before each restore.
- **pgdelta's v1 arm reads a stopped source:** no backup mode, no WAL streaming, no live workload. That favours pgdelta.
- **The repository sits on the source's local NVMe.** Omni's repositories can be a local disk, Cloud Storage or S3.
- **SSH encrypts pgBackRest's traffic; pgdelta's is unencrypted,** although owner decision O6 requires TLS. The cipher's cost counts against pgBackRest. It could matter for the "clear edge" rule's 20% margin.
- **pgbench's padded rows compress unrealistically,** which helps the zstd variants (I8b).
