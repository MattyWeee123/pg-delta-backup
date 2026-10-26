# EXP-51: Does batching hold in a second session, with every copy byte-identical to S?

Status: **pre-registered October 27, 2026, before any run of this session.** Local (the Mac), end to end with `pgdelta local`, as EXP-50. Lean: 10 runs of a few seconds each. Rule 11 (K10): EXP-50's deciding ratio, 0.44, was within 20% of its 0.5 bar.

## Question

EXP-50 found that pool tasks of up to 1 MiB of pages cut zstd's extra time over plain v1 from 0.81 s to 0.36 s, locally. Its correctness prediction failed on a flawed criterion: files digests are keyed per session, so they differ on every run. By its rule, the default went back to one run per task.

This session asks two things:
- Does the cut hold a second time?
- Is every refreshed copy byte-identical to S?

## Design

EXP-50's harness (`experiments/bytes/exp50.py`), its scale-70 template and S, unchanged, plus:
- **`--rotation 1`:** each repeat starts one arm further on than in EXP-50, so no arm keeps its position.
- **`--compare-bytes`:** after every run, each file pgdelta sends is compared by SHA-256 with the refreshed copy's file. The list of files is pgdelta's own selection of S (`pgdelta.datadir.select`, which reads an unlogged relation's init fork in place of its main fork).
- **A negative control:** once, before any run, the same check on B as it was. It must find files that differ.
- **The batched arm** sets `PGDELTA_ZSTD_TASK_BYTES=1048576` itself, since the default is now 0. The per-run arm sets 0, as before.

A smoke at scale 1 (`results/bytes/exp51-smoke`) checked the new check before this was committed:
- the control found 12 of 1,278 files different in B;
- every one of the 10 refreshed copies matched S in all 1,278 files.

The analysis, committed before the data: `analysis/exp51.py`.

## Predictions

1. **Deciding: batching keeps at most half of zstd's extra time over plain:** `zstd_batched − plain ≤ 0.5 × (zstd_per_run − plain)`. It must hold on medians in this session alone, and with both sessions' 6 repeats per arm together.
2. **Every copy is byte-identical to S:** every run of this session completes, and leaves every file pgdelta sends identical to S's. The control finds at least one file of B that differs.
3. **The receiver's writes,** this session: `rebuild` takes at least 1.5× plain's with one run per task, and at most 1.3× plain's when batched.

## What follows

- **If 1 and 2 hold:** batching becomes the default (`TASK_BYTES` 1 MiB). The README notes it, and a VM test at 2 Gbit/s of EXP-47's cells is pre-registered next. Its prediction: zstd falls below plain v1's time there.
- **If 2 fails:** batching stays off and the difference is investigated, whatever 1 says.
- **If 1 fails:** batching stays off. The two sessions are reported side by side.
