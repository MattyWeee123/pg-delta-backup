# EXP-32: Is v1 still correct when it verifies only what it wrote?

Status: **pre-registered October 25, 2026, before the counted campaign** (a 25-trial smoke checked the harness; see below). Local (the Mac, PostgreSQL 17.11), about an hour. It is the correctness test that `--verify-scope written` needs before it can be recommended. [EXP-31](EXP-31-ha-one-pass.md) measures its speed.

## Question

With `--verify-scope written`, the receiver checks each file from three things, instead of re-reading all of it:
- B's node digests, taken before anything was written;
- B's leaf digests, from the leaf requests;
- the pages it wrote, re-read.

Does v1 still never produce a wrong copy, and does that verification still catch a write that goes to the wrong place?

## Design

[EXP-17](EXP-17-randomized-correctness.md)'s campaign, unchanged except for two options (`experiments/correctness/campaign.py`):
- **`--pgdelta-args "--verify-scope written"`** on every pgdelta run.
- **`--negative-every 20`:** in each block of 20 trial numbers, the one half-way through (10, 30, …) is a negative control, not a counted trial. Its fault is none, and its first run sets the receiver's test switch `PGDELTA_TEST_MISPLACE_WRITE=1`, which writes the run's first movable page run one page away from where it belongs.
  - It is **caught** if pgdelta exits 4 (verification failed), B is left in its blocked state (`global/pg_control` renamed aside, with the marker), and `pg_ctl` refuses to start B.
  - A normal run then must repair B, checked like any trial.

Everything else is EXP-17's:
- the trials: a template cluster cloned to S and B, a random workload on S, S stopped, one random fault applied to B (none, diverged, killed part-way, corrupted, truncated, foreign), B refreshed in place from the stopped data directory;
- the checks, independent of pgdelta: byte identity with S over S's files, no extra relation files, and S's logical digest once B starts.

**Stop rule:** 1,000 counted trials or 2 hours, whichever comes first. A wrong output, a control that isn't caught, or a failed repair stops the campaign.

## Predictions

1. **Deciding: no wrong output in 1,000 counted trials.** That bounds the rate below 0.69% per refresh at 99.9% confidence (EXP-17's bound, with the default scope and 10,000 trials, was below 0.069%).
2. Every negative control is caught (registered: all of them). This is a deterministic argument: the page meant for one place keeps B's bytes, which differ from S's or the page wouldn't have been sent, so the file's digest can't match.
3. Every part-way kill leaves B old, blocked or new, as in EXP-17.

## What follows

- **If 1 and 2 hold:** `--verify-scope written` is shown correct on this platform's fault mix. EXP-31's measurement then decides whether it becomes v1's default for in-place refreshes.
- **Otherwise:** the trial that failed is kept and its cause found, and the scope stays off.

## Limitations

- EXP-17's: a stopped source, about 50 MB, macOS.
- 1,000 trials bound the rate ten times less tightly than EXP-17's 10,000.
- The negative control tests one way a write can go wrong (an offset off by a page). A write that changes a page nobody meant to write, outside every written run, is the case the scope trusts away: in place, every write goes through one function at the offsets of the runs it records.

## Smoke (October 25, before the campaign; not counted)

25 trials with controls every 10: 25 counted, no wrong output; 3 of 3 controls caught (exit 4, B blocked, start refused), each repaired by the next run.
