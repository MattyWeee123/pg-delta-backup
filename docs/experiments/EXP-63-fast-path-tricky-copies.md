# EXP-63: Does pgdelta's own fast path, run live, refresh the tricky old copies correctly?

Status: **pre-registered October 27, 2026, before any run.** Local only (the Mac, PostgreSQL 17.11 and 18.6).

## Question

EXP-61 and EXP-62 scored a WAL-summary fast path offline: a scorer read the summaries and counted the pages it would leave wrong. The owner then chose to build it (K11), and `pgdelta send/local --fast-path summaries` now exists (8f8d866). Its guard is a port of EXP-62's guard v2, and it runs inside a live session:
- the receiver proves B's lineage, including with `pg_waldump` on B's own WAL;
- the sender asks the server for its summaries after `pg_backup_start`.

Does the real thing make the same calls on the tricky copies, and is every copy it produces correct?

Planning this check found one hole, closed before any run (in this commit): the guard relied on the identity gate to keep other clusters out. With `--reuse-any-basis` that gate lets one through, and its LSNs are unrelated, so the fast path now compares system identifier, catalog version and checksum version itself. A unit test covers it.

## Design

EXP-62's 13 scenarios (EXP-03's S1–S9, S2b, S2c, S2d), rebuilt with `--wal-summaries --keep`, so each source cluster stays on disk, stopped. For each scenario:
1. Start the source cluster as a live primary.
2. Refresh a copy of the old copy B in place with `pgdelta local --source-kind live --fast-path summaries`, as a default invocation would run it, with B's own `pg_controldata` given as `--basis-pg-controldata`.
3. Where the identity gate refuses the in-place refresh (another cluster, or another checksum setting), run again with `--reuse-any-basis`. That exercises the fast path's own identity check.
4. Restore each refreshed copy, let it recover, and compare its logical state with the source's recorded state (`expected_state.json`, EXP-03's comparison).

Script: `experiments/tricky_copies/fastpath_check.py`, which writes `fastpath_check.json` per scenario. Summary: `analysis/exp63.py`, committed before the run.

## Predictions

1. **Deciding: no refresh produces a wrong copy.** Every copy that pgdelta published recovers and matches the source's logical state; every refusal leaves B as it was.
2. **The fast path runs exactly where guard v2 passed in EXP-62** (S1, S1b, S4, S5, S7). Everywhere else it is refused, by the receiver or the sender's guard, or never offered because the identity gate refused first.
3. **Where it runs, it sends at most half the pages** hashing compares.

## What follows

- **If 1 and 2 hold:** the implementation matches the tested guard on every tricky copy, and the timed test beyond memory (EXP-64) goes ahead.
- **If 1 fails:** the fast path is withdrawn from the README's recommendations until the cause is fixed and this check passes again.
- **If 2 fails without 1 failing:** the difference is explained, and the guard is fixed or the difference shown to be harmless, before EXP-64.
