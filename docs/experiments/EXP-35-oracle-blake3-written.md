# EXP-35: Were EXP-33's missed controls correct copies, with BLAKE3 and `--verify-scope written`?

Status: **pre-registered October 25, 2026, before any run.** [EXP-27](EXP-27-control-oracle.md)'s oracle on [EXP-33](EXP-33-live-verify-written.md)'s configuration. Local (the Mac), about 25 minutes.

## Question

In EXP-33, the read-during-start control was caught in only 47 of 55 trials, below its 90% bar, so no bound was claimed and the default stayed `--verify-scope all`. EXP-27 had shown, under the default configuration, that this control's misses are copies the WAL fully repaired: the checks caught every wrong copy. Is that still true under BLAKE3 and `written`, or do the checks miss wrong copies here?

## Design

EXP-27's harness (`oracle_campaign.py`), unchanged except `--pgdelta-args "--verify-scope written --hash auto"`, so BLAKE3 is used here, as in EXP-33. Every trial is the during-start control. pgdelta's test-only oracle also reads the same files as soon as `pg_backup_start` returns, which gives B (the variant's copy) and B2 (the correct read's copy) of one backup, recovered side by side and compared page by page. 40 trials, seeds 3000–3039.

## Predictions

1. B2 passes every check in all 40 trials (the oracle's own control).
2. **Deciding: every missed B is physically identical to B2.**
3. Every B the oracle finds wrong is caught by the checks.

## What follows

- **If 2 and 3 hold:** EXP-33's shortfall is the variant making correct copies more often (BLAKE3 changes the refresh's timing), not the checks missing wrong ones, and its 1,000 correct trials read as checked by sensitive checks. With EXP-32's 2,000 trials on a stopped source, `--verify-scope written` then **becomes the default for in-place refreshes**. The full re-read stays as `--verify-scope all`. EXP-33's own registered verdict (no bound) is unchanged.
- **If a missed B is wrong:** the checks have a gap in this configuration. It is reported with the relations it hit, and the default stays `all`.
