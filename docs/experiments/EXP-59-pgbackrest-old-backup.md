# EXP-59: Does pgBackRest still compete when its newest backup is a day old?

Status: **pre-registered October 27, 2026, before any run.** Ledger test T11b, lean: one VM pair, about an hour. The owner asked for it on October 27.

## Question

EXP-08 compared pgdelta with pgBackRest's delta restore and found a mixed result:
- **pgdelta** was faster for local changes;
- **pgBackRest** was faster for scattered changes below about 6–9 Gbit/s.

That comparison flattered pgBackRest in two ways, both named in EXP-08's own caveats:
1. **Its backup was taken just before each restore,** so it already matched the source. A real repository's newest backup is up to a day old (Omni's default is a daily full).
2. **Its time stopped when the restore finished,** before the WAL replay the copy needs to be usable.

The realistic case is the one where pgdelta's old copy is recent (a former standby, minutes behind) and the backup is older:
- pgBackRest has to roll the copy back to the backup's state and replay every change since;
- pgdelta sends only the recent changes.

How do the two compare there, timed to a usable copy?

## Design

EXP-08's part B on the default `n2-standard-8` pair (the repository on the source, the restore over SSH with `process-max=4`), with three changes:
- **An old backup** (`--pgbackrest-aged-by 0.05`, new in `run_exp04.py`), in this order:
  1. the repository's full backup is taken first;
  2. 5% of `pgbench_accounts`' rows are updated at random (its own seed): the day's churn;
  3. B0 is taken;
  4. the trial's workload runs as before.

  So pgdelta's old copy B0 is behind the source by the workload alone, while the backup is behind by the churn and the workload. The WAL of both is archived to the repository.
- **Realistic rows** (`--hex-filler`): EXP-08's pgbench rows were blank padding, which compressed unusually well in pgBackRest's favour.
- **Timed to a usable copy:** each run's time plus its copy's start (`total_s + output_start_s`). pgBackRest's start includes its recovery through `archive-get`; pgdelta's copy starts at once.

| Setting | Value |
|---|---|
| Patterns | `uniform` (scattered) and `hot` (local), 1% |
| Scale | 70 (2.1 GB) |
| Speeds | 0.5 and 2 Gbit/s, and unthrottled (no added delay, as EXP-08) |
| Methods | `full`, `pgdelta_direct_inplace` (v1, as EXP-08), `pgbackrest_zst` (Omni's configuration) |
| Repeats | 2 |
| Order seed | 590 |
| Manifests | CRC32C |

Every output must start and match the source's own per-table digest. The workload's row-level check also counts the churn's rows, so it is reported but decides nothing.

**The analysis,** committed before the data: `analysis/exp59.py`, figure `exp59-old-backup`.

**Why 5%:** about a day's churn on a moderately busy OLTP table. It touches about 96% of `pgbench_accounts`' pages, so the restore must roll back nearly the whole table. A busier day only adds to pgBackRest's WAL replay, so 5% is not an extreme for it. The result is stated for this one churn.

## Predictions

1. **Deciding: pgdelta reaches a usable copy in at most 0.8 of pgBackRest's time, in every cell** (both patterns, all three speeds; medians of 2).
2. **pgBackRest moves more bytes than pgdelta in every cell:** its restore and its recovery's WAL, against pgdelta's pages.
3. **Every copy is correct:** each starts and matches the source's digest.

Rule 11 applies to 1: a cell within 20% of the 0.8 bar gets a second session.

## What follows

- **If 1 holds:** pgdelta is faster than pgBackRest even where a repository exists, once its newest backup is a day's churn old. The pitch widens from "no repository needed" to "faster than restoring from a day-old backup". RESULTS, DECISIONS (D01) and the deck say so, with this churn named.
- **If 1 fails:** the pitch stays "no repository needed". The analysis names the cells where pgBackRest wins, and EXP-08's crossover is restated with time to a usable copy.
- **If 3 fails anywhere:** that run is reported, and no time comparison stands for its cell.

## A code check after the predictions were written (October 27, before any run)

The analysis was run once on EXP-08's own data to check its code (its outputs were deleted). Timed to a usable copy, which EXP-08 never did, that data already favours pgdelta:
- **pgBackRest's recovery** took 10–14 s even from a backup taken just before, against pgdelta's 0.16 s start;
- **five of EXP-08's six cells** come out at 0.18–0.34 of pgBackRest's time;
- **its sixth,** scattered changes at 0.5 Gbit/s, is 0.72, within rule 11's 20% of the 0.8 bar.

EXP-08's rows were blank padding, and its backup was fresh, so this decides nothing here. The predictions above stand as written. It does mean the deciding cell to watch is scattered changes at 0.5 Gbit/s. The check also showed the harness names speeds `0.5g` and `2g`, which the analysis now uses.
