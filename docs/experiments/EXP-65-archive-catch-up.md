# EXP-65: Against the standard way a lagging standby catches up (archive replay), does the fast path win?

Status: **pre-registered October 27, 2026, before any run.** One VM pair (the `ha` set), about 2 hours. Research loop 1 (rule 12) found this comparison unmeasured; rule 12 asks for it before more optimisation.

## Question

When a streaming standby falls behind and a WAL archive exists, the standard practice is to let it replay the archive through `restore_command` until it is current. CloudNativePG does that automatically, and pgBackRest's `archive-get` serves it (research loop 1). pgdelta's fast path instead sends the blocks that changed.
- **Replay** pays for every WAL record since the standby stopped: each update, each full-page image after each checkpoint.
- **The fast path** pays once for each distinct changed page.

Which is faster to a current copy, and when?

## Design

EXP-64's set-up and VMs: the `ha` set, two `n2d-highcpu-8` with 150 GiB balanced persistent disks, EXP-48's 18 GB database, PostgreSQL 17.11 or later, with `summarize_wal` on. Script: `experiments/network/lab_exp65.sh`.
- **The source** archives its WAL (`archive_mode = on`, `cp` to a local directory).
- **The standby B0** is a `pg_basebackup -R` of the source, caught up by streaming, then stopped cleanly (`shut down in recovery`), so the fast path's guard can prove its end.
- **Two lags,** each with a fresh B0, then a WAL switch, the archive complete, and the source's LSN and `pgbench_accounts` state recorded:
  - **hot10:** the same 1% hot rows updated 10 times (EXP-48's hot workload, ten passes): many WAL records over few pages;
  - **uniform:** 1% of the rows updated once, spread over about 28% of the pages (EXP-64).
- **Fair to the competitor:** before the runs, the whole archive is copied to the destination, untimed. Replay then fetches with a local `cp`: its floor, with no fetch cost at all.
- **Each run** starts from a fresh copy of B0, with both VMs' caches dropped, and is timed until the copy is current:
  - **replay:** B starts as a standby with `restore_command` and no `primary_conninfo`, and is timed until it has replayed past the source's LSN;
  - **fast:** pgdelta live, in place, `--fast-path summaries --hash blake3 --jobs 4`, then the copy started until it accepts connections;
  - **full:** one `pg_basebackup -X stream -c fast` into a new directory, then started the same way.
- **Correctness:** each copy's `pgbench_accounts` row count and sum of `abalance` must equal the source's.
- 2 repeats per arm and lag, shuffled (seed 650). **The analysis,** committed before the data: `analysis/exp65.py`, figure `exp65-archive-catch-up`.

## Predictions

1. **Deciding: for hot10, the fast path reaches a current copy in at most 0.25 of archive replay's time.**
2. **For hot10, the fast path takes at most 0.25 of a full re-clone's time.**
3. **For uniform, the fast path is at least as fast as archive replay** (at most 1.0 of its time). That is uncertain: replay pays about one full-page image per changed page and reads those pages beyond memory, against the fast path's measured 164.8 s (EXP-64).
4. **Every copy is current and correct,** every fast run used the fast path (its guard proved by the clean stop in recovery), and every run exited 0.

**Rule 11:** a deciding ratio within 0.05 of its bar gets a second session.

## What follows

- **If 1 holds:** for standbys whose lag is many updates to few pages, the fast path beats the standard catch-up, even replay's floor with the archive already local. RESULTS says so, with the conditions.
- **If 3 fails:** for scattered lag, archive replay remains the better choice. Research loop 1's write path (EXP-64b, gap-filled writes) is what would change that, and is measured against replay next.
- **Either way,** the comparison table in research loop 1 replaces "not measured" with these numbers.
