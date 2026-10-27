# EXP-62: Can an old primary whose own WAL was lost fool EXP-61's guard, and does a stricter guard close it?

Status: **pre-registered October 27, 2026, before any run.** Local only, like EXP-61. No VM, no change to pgdelta.

## Question

EXP-61's guard let a WAL-summary fast path run only when the old copy B provably lies on the source's history. It left no real wrong page on 12 tricky copies. Its write-up named one blind spot:
- the guard finds how far B went from the furthest of three positions: its latest checkpoint, its minimum recovery point, and the last record in its own `pg_wal`;
- so an old primary that crashed after writing past a failover split would look like a clean ancestor if its `pg_wal` past the split were later lost: deleted, recycled or never copied.

Its pages would still hold the diverged rows, and the summaries can't see them. This experiment builds that copy and tests a stricter guard.

## Design

**A new scenario, S2d (old primary, its WAL past the split lost),** built like S2b:
1. the old primary keeps writing after the split, flushes the diverged pages without a checkpoint, and stops with `-m immediate`, so its redo point stays before the split;
2. then every WAL segment of B from the one holding the split on is deleted.

S2d is added beside EXP-03's list (`scenarios.EXTRA_SCENARIOS`), not into it, so EXP-03's and T11's runs are unchanged.

**Guard v2:** EXP-61's guard (v1), but B's end must be *proven by how B was stopped*, not read from whatever WAL B still holds:
- **a data directory shut down cleanly** (`shut down`): its end is its shutdown checkpoint, written after every record it wrote;
- **a standby shut down cleanly** (`shut down in recovery`): the later of its checkpoint and its minimum recovery point, which a standby advances before it writes any page past it;
- **a base backup** (has `backup_label`): the latest end in its `backup_manifest`'s WAL ranges; without a manifest, refused;
- **anything else is refused,** since how far it went can't be proven without all of its WAL. That means a data directory that crashed or was copied while running (`in production`, `in crash recovery`, `in archive recovery`).

The last WAL record in B's `pg_wal` still counts when it lies further (more caution, never less). Everything else is as v1:
- the same system identifier, major version and checksum version;
- B's end on S's history;
- the summaries covering the gap.

**Runs:** EXP-61's 12 scenarios rebuilt, plus S2d, with `--wal-summaries`, into `results/exp62/`. Each `summaryscan.json` now carries both guards' verdicts and both guarded paths' counts. The code in `summaryscan.py` and `analysis/exp62.py` is committed before the run. v1 is unchanged, so EXP-61 still reproduces.

## Predictions

1. **Deciding, the blind spot is real:** guard v1 lets the summary path run on S2d, and it leaves real wrong pages there (stale data from the diverged writes).
2. **Deciding, v2 closes it:** guard v2 refuses S2d, and the v2-guarded path leaves no real wrong page in any of the 13 scenarios.
3. **v2's cost:**
   - it passes exactly S1, S1b, S4, S5 and S7;
   - it also refuses S3, the crash-consistent copy, which v1 let through safely and which then falls back to hashing;
   - it refuses everything v1 refused.

## What follows

- **If 1 and 2 hold:** a fast path, if built, uses v2. The rule is that B's end is proven by a clean stop or a backup manifest, never by the WAL B happens to hold. EXP-61's write-up points here for its blind spot. The cost is that crash-copied old copies (S3) are hashed, as today.
- **If 1 fails** (v1 already refuses S2d): the write-up records what caught it. v2 is then stricter than needed, and its extra cost (S3) is weighed against that.
- **If 2 fails:** the write-up names each real wrong page and why it passed. The fast path stays unsafe until a further guard is pre-registered and tested.
