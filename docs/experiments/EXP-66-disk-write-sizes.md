# EXP-66: Does a balanced persistent disk count a 32 KiB write as one operation, so that gap-filled writes would pay?

Status: **pre-registered October 27, 2026, before any run.** About 10 minutes on dest of EXP-64b's VM pair, after EXP-64b's runs and before teardown. Research loop 1 ranked this second: it decides idea 2's premise.

## Question

Beyond memory, the fast path's scattered case writes about 620,000 pages spread over 28% of an 18 GB copy. EXP-64 measured about 107 s of the receiver writing and syncing them (rebuild 57.6 s plus fsync 49.4 s). Even with the source's reads overlapped (EXP-64b), that write time binds, and it keeps the fast path above a full copy's 91 s.

**Research loop 1's idea 2:** fill short clean gaps between dirty runs, so that writes become fewer and larger. The idea comes from ROMIO's data sieving (Thakur et al.) and PandaSync's threshold rule. The fill pages would be the source's current pages, which a full copy would write anyway.

**The idea pays only if the disk charges by operation rather than by byte** for writes of a few pages. GCP documents a balanced disk's limits per operation and per byte, but not how it counts an operation's size. Say a 32 KiB write costs the same operation as an 8 KiB one until the byte limit binds. Then filling gaps of up to a few pages turns about 440,000 small writes into far fewer larger ones, at the price of writing the filled pages too. If the disk charges by bytes instead, filling only adds work.

A second, smaller question is for EXP-64b's mechanism. How many random 8 KiB reads does the disk complete one at a time? EXP-64's sender read about 4,450 runs a second at queue depth 1, which EXP-64b attributes to the kernel's read-ahead (**inferred**). A measured single-read rate well below that settles the attribution.

## Design

- **Where:** dest of the `ha` set (an `n2d-highcpu-8` with a 150 GiB balanced persistent disk, us-west1), right after EXP-64b's runs, with nothing else running.
- **How:** `experiments/network/fio_sweep.sh`, fio with direct I/O (`libaio`) on a 20 GiB file laid out first. Each configuration runs 30 s after a 5 s ramp, with caches dropped before it:
  - random writes of 8, 16, 32, 64 and 128 KiB at queue depth 32;
  - random 8 KiB reads at queue depths 1, 8 and 32;
  - 1 MiB sequential writes and reads at queue depth 8 (the byte limit).
- **A smoke first** (2 GiB, 5 s per configuration), deciding nothing.
- **The model, committed before the data** (`analysis/exp66.py`, data `docs/experiments/data/exp66-fio.csv` and `exp66-model.csv`, figure `exp66-write-sizes`):
  1. **The page set:** EXP-48's uniform share of pages (619,856 of 2,175,659), chosen independently (seed 660), with runs cut at 1 GiB segment boundaries.
  2. **Filling:** for each g from 0 to 8, every clean gap of at most g pages is filled.
  3. **The cost:** each write of b bytes costs max(1 ÷ ops(b), b ÷ the sequential write rate). ops(b) is interpolated from the measured random-write rates, and held at the 128 KiB rate beyond that size, where the byte term takes over.
  4. **The result:** the modelled write time at each g.

  Assuming pages are chosen independently is a simplification: real updates cluster slightly by page and by index. Prediction 3 tests whether the model is close enough at g = 0.

## Predictions

1. **Deciding: at queue depth 32, random 32 KiB writes complete at least 0.7 times as many operations a second as 8 KiB ones.** This is idea 2's premise: larger writes cost about one operation each until the byte limit binds.
2. **Random 8 KiB reads at queue depth 1 complete at most 2,500 a second.** EXP-64's 4,450 runs a second then needed the kernel's read-ahead, as EXP-64b assumes.
3. **Control on the model:** at g = 0 (no filling), the modelled write time is within 30% of EXP-64's measured 107 s.

**The decision rule:** idea 2 is prototyped in the fast path's write plan only if 1 and 3 hold and the best g cuts the modelled write time by at least 25%. The prototype is then measured on the uniform cell against EXP-64b's best arm, with its own pre-registration.

**Rule 11:** a deciding ratio within 0.05 of its bar gets a second session.

## What follows

- **If the rule says prototype:** the fast path's write plan fills gaps of up to the best g pages, with the fill pages sent from the source. The source's read-ahead is already reading them, so the cost is network bytes on a 16 Gbit/s link. This could move the crossover with a full copy (research loop 1, section 3) well past 28%.
- **If 1 fails:** the disk charges by bytes, so idea 2 is dropped with its reason recorded. Beyond about 28% of pages changed, a full copy then stays the better choice. That is the case for idea 4, the selector: choose a full copy there.
- **If 3 fails:** the model misses how the receiver's writes reach the disk (the kernel merging dirty pages, or the sync pattern). The write path is instrumented before any prototype.
