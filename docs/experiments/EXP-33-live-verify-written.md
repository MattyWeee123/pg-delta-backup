# EXP-33: Is v1 from a live source still correct when it verifies only what it wrote?

Status: **pre-registered October 25, 2026, before any run.** The second campaign that `--verify-scope written` needs before it can be the default for in-place refreshes ([EXP-31](EXP-31-ha-one-pass.md)'s "What follows"). The first was [EXP-32](EXP-32-verify-written-correctness.md), on a stopped source. Local (the Mac), about 1.5–2 hours.

## Design

[EXP-25](EXP-25-live-correctness.md)'s second run, unchanged, with `--pgdelta-args "--verify-scope written"` on every pgdelta run:
- `experiments/correctness/live_campaign.py`, the registered template (1 MiB WAL segments, timeline 2), a continuous bank load;
- faults none, diverged, kill, corrupt and truncate; checks on a recovered clone (bank invariant and window, static digest, pg_amcheck, the 120 s query limit);
- its two negative controls (read before `pg_backup_start`, and while it runs);
- 1,000 counted trials or 3 hours.

Each trial's record now carries the receiver's verification scope, so the campaign shows the scope was in effect run by run (EXP-32 could check only its last run).

It runs at the same time as EXP-32's rerun and EXP-31's second session on the VMs, so the Mac is busier than in EXP-25. Correctness doesn't depend on the timing, but kill delays land at different points.

## Predictions

1. **Deciding: no wrong output in 1,000 counted trials** (below 0.69% per refresh at 99.9% confidence), every one with the scope `written` in effect.
2. Both negative controls meet their 90% bar.
3. Every part-way kill leaves B old, blocked or new.

## What follows

- **If 1–3 hold, and EXP-32's rerun holds too:** `--verify-scope written` becomes the default for in-place refreshes. The CLI's default changes, and the full re-read stays as `--verify-scope all`.
- **Otherwise:** the trial is kept, its cause found, and the default stays `all`.
