# EXP-63: Does pgdelta's own fast path, run live, refresh the tricky old copies correctly?

Status: **done October 27, 2026 (13 scenarios, 16 refreshes; the deciding prediction held, and 2 of 3 overall): every copy pgdelta published recovered to exactly the source's data, and the fast path ran exactly where guard v2 passed in EXP-62. Its own identity check refused another cluster, an upgraded one and a changed checksum setting, even under `--reuse-any-basis`. Prediction 3 failed in S5, where most pages belong to databases B lacks.** Local only (the Mac, PostgreSQL 17.11 and 18.6).

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

## Results (October 27)

The 13 scenarios were built with `--wal-summaries --keep` (all checks passed). Each was refreshed by `experiments/tricky_copies/fastpath_check.py`, with S1 run first as a smoke test. Data: `docs/experiments/data/exp63-runs.csv`; raw `results/exp63/` (not in git).

| Scenario | Refresh | Fast path | Why not, where refused | Pages sent / total | Copy matches the source |
|---|---|---|---|---:|---|
| S1 lagging standby | default | **ran** (clean shutdown in recovery) | | 4,118 / 10,594 | yes |
| S1b, no checkpoint replayed | default | **ran** | | 4,185 / 10,594 | yes |
| S2 old primary after failover | default | refused (sender's guard) | B reaches past where the server's history leaves timeline 1 | 5,075 / 10,596 | yes (hashed) |
| S2b, its redo before the split | default | refused (receiver) | B was not stopped cleanly | 5,249 / 10,596 | yes (hashed) |
| S2c, a standby of the old primary | default | refused (sender's guard) | B reaches past the split | 5,227 / 10,596 | yes (hashed) |
| S2d, its WAL past the split lost | default | refused (receiver) | B was not stopped cleanly | 5,249 / 10,596 | yes (hashed) |
| S3 crash-consistent copy | default | refused (receiver) | B was not stopped cleanly | 7,060 / 10,791 | yes (hashed) |
| S4 VACUUM truncation | default | **ran** (backup_manifest) | | 2,953 / 9,067 | yes |
| S5 `FILE_COPY` databases | default | **ran** | | 15,559 / 24,425 | yes |
| S6 `pg_checksums --enable` | default | identity gate refused the in-place refresh; B untouched | | – | – |
| S6 | `--reuse-any-basis` | refused (sender's guard) | B's data checksum version (0) is not the server's (1) | 10,478 / 10,496 | yes (hashed) |
| S7 hint bits, no checksums | default | **ran** | | 2,432 / 10,465 | yes |
| S8 another cluster | default | identity gate refused; B untouched | | – | – |
| S8 | `--reuse-any-basis` | refused (sender's guard) | B's system identifier is not the server's | 3,758 / 10,496 | yes (hashed) |
| S9 `pg_upgrade` 17→18 | default | identity gate refused; B untouched | | – | – |
| S9 | `--reuse-any-basis` | refused (sender's guard) | B's system identifier is not the server's | 4,947 / 10,534 | yes (hashed) |

| # | Prediction | Outcome |
|---|---|---|
| 1 | Deciding: no wrong copy | **Held:** 13 copies published, all recovered and matched; 3 refusals left B as it was |
| 2 | The fast path runs exactly in S1, S1b, S4, S5, S7 | **Held** |
| 3 | Where it runs, at most half the pages sent | **Failed in S5 only** (0.64): 837 of its files, most of its pages, belong to the databases created with `FILE_COPY`, which B lacks. Any path sends those whole. The other four sent 0.23–0.40 |

**What it shows:**
- **The implementation makes the same calls as the guard tested offline,** on every tricky copy, and every copy it made is correct. Every refusal names its reason in the statistics:
  - the sender's guard for divergence and identity;
  - the receiver for an unproven end.
- **The identity hole found while planning was real in principle,** and is closed. With `--reuse-any-basis`, S6, S8 and S9 reach the fast path's guard, and it refuses all three by its own identity check.
- **It sends more pages live than EXP-61 counted offline:** 4,118 against EXP-61's 2,039 for S1. The live range runs on to the new backup's start, so it includes the full-page images logged when the harness read every table after taking S (hint bits, with checksums on). The fast path's saving is the pages it doesn't read, not the pages it sends; timing it beyond memory is EXP-64.
