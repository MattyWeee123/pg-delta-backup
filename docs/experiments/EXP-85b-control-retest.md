# EXP-85b: Does single read change how often EXP-82's negative control comes out wrong?

Status: **done October 30, 2026: held.** 28 of 30 single-read controls wrong (93%), 25 of 30 two-read ones (83%); Fisher's exact test p = 0.42. EXP-85's VM stage runs as registered. Pre-registered October 30, before any scored run.

## Why

**EXP-85's stage 1** found single-read page-lsn correct in 120 of 120 trials. But **L2 failed as registered:** 9 of its 10 negative controls came out wrong, against "all 10".
- The control is random by design, and EXP-82 had registered it as "at least 8 of 10 wrong".
- On an idle source, single read sends the same pages and bytes as two reads, so it shouldn't change the control's outcome. That is an argument from the code, not a measurement.

**The owner's decision (October 30):** measure it. Run 30 controls with single read and 30 with two reads at EXP-82's bar (at least 80% wrong in each). If that holds, run EXP-85's VM stage as registered, with L2 still recorded as failed.

## Design

- **The harness:** `experiments/summaries_off/controls.py --per-mode 30 --seed 852`, PostgreSQL 17.11 on the Mac. It builds on EXP-82's campaign (`trials.py`'s lab and trial, unchanged).
- **The 60 controls,** shuffled in one session: each is EXP-82's `lsn-backup-end`, refreshed by page-lsn.
  - Each control: a base backup taken under 8 clients' load, no load after, and its `backup_label` start moved to its end.
  - **Single** trials pass `--lsn-single-read`; **two** trials don't.
- **The check,** as in EXP-82 and EXP-85: each copy is started and compared with S (every pgbench table by md5, the history's count and sum, each side table's rows), and checked with `pg_amcheck --heapallindexed`.
  - A copy is wrong when its refresh exited 0 and the rows differ or `pg_amcheck` fails.
- **The smoke** (seed 859, 1 per mode; decides nothing): both copies wrong, each run as its mode says.
- **The analysis,** committed before the data: `analysis/exp85b.py`. Data `docs/experiments/data/exp85b-controls.csv`.

## Predictions

1. **Deciding: at least 80% of the controls wrong in each mode** (24 of 30 or more), single read and two reads.
2. **Every trial ran as its mode says:**
   - the fast path used, with proof "backup_manifest";
   - `fast_path.lsn_single_read` as the mode;
   - with single read, nothing read again.

**Reported, not predicted:**
- each mode's share wrong, with its 95% Wilson interval;
- Fisher's exact test of the two shares, two-sided.

## What follows

- **If 1 and 2 hold,** EXP-85's VM stage runs as registered (the owner's decision). EXP-85's L2 stays recorded as failed, and its write-up cites this result.
- **If 1 fails for single read only,** single read may be sending something two reads don't on the control's copies. The VM stage doesn't run, and the failing copies are compared with the two-read ones.
- **If 1 fails for both,** the control is weaker than EXP-82's 10 of 10 suggested. EXP-82's and EXP-85's correctness claims are then re-read against its measured rate, and the VM stage waits on the owner.

## The rules this session runs under

- **Rule 13:** the local stage, gating EXP-85's VM stage.
- **Rule 16:** `analysis/integrity.py register EXP-85b` records the harness, `trials.py` and EXP-80's lab under it, the analysis, and pgdelta's modules.
- **Rule 17:** not a timing; the bar is a count.

## Workflow check

| | Check | For this experiment |
|---|---|---|
| 1 | Situation, standard, competitor | Not the question here: the soundness of EXP-85's correctness evidence |
| 2 | The bottleneck | Not the question |
| 3 | Systems or algorithm | Not the question |
| 4 | Papers and prior art | Not the question |
| 5 | Committee-grade | Pre-registered; measures what EXP-85's write-up argued from the code; both modes in one shuffled session |
| 13 | The cascade | The local stage, before the VMs |
| 14 | The idea archive | No change |
| 15 | Held-out workloads | Not used |
| 16 | Scorer integrity | Recorded at registration |
| 17 | The noise bar | A count against a registered bar, not a timing |

## Results

**Scorer integrity (rule 16):** `analysis/integrity.py check EXP-85b` reports 10 of 10 files unchanged since the registration commit (a218464).

**The session:** PostgreSQL 17.11 on the Mac, 60 controls in a shuffled order (seed 852). Every refresh exited 0. Data: `docs/experiments/data/exp85b-controls.csv`.

| # | Prediction | Result |
|---|---|---|
| 1 | At least 80% of the controls wrong in each mode (24 of 30) | **Held:** single read **28 of 30** (93%; 95% 0.79–0.98), two reads **25 of 30** (83%; 0.66–0.93) |
| 2 | Every trial ran as its mode says | **Held:** all 60 took the fast path with proof "backup_manifest" and the mode's `lsn_single_read`; single read re-read nothing |

**Reported:** Fisher's exact test of the two shares gives p = 0.42. Two reads came out right more often than single read, not less.

**What it means:**
- **Single read doesn't make the control's copies right.** EXP-85's correct control (trial 6) was the control's own randomness: two reads came out right 5 times in 30 here.
- **The control's measured rate is about 88% wrong** (53 of 60). EXP-82's 10 of 10 was the likelier outcome at that rate (about 0.28), not a sign of a certain one. EXP-85's registration of "all 10" asked for more than the control gives.
  - A correct control copy is a copy the change happened not to break: the check compares every pgbench table and runs `pg_amcheck`. It is not a wrong copy the check missed.
  - Future registrations use EXP-82's bar for this control, or this measured rate.
- **EXP-85's L2 stays recorded as failed,** as registered. Its VM stage runs on the owner's decision of October 30, with this result as the reason.
