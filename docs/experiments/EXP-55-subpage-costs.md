# EXP-55: What would sub-page deltas cost beyond bytes?

Status: **done October 27, 2026 (one local run, 3 write repeats; all three predictions held). Sub-page deltas cost little beyond their bytes:** B's block digests can go up front with the leaf digests, with no extra round trip, for 17.2 MB (1.88× the on-request design). With them, blocks plus zstd still send 0.25 of zstd alone's bytes. Hashing the blocks takes 1.46 CPU-seconds on one thread, and writing 512-byte pieces into B took 1.07× the time of whole runs, every patched copy byte-identical to S. Pre-registered before any run. Local (the Mac), over EXP-51's data, as EXP-54. Lean: one script, a few minutes. It prices the three costs EXP-54 left out, so that a build has a concrete design and price before anyone decides on it.

## Question

EXP-54: changed 512-byte blocks plus zstd send 0.21 of zstd's bytes for scattered updates. Three costs weren't measured:
1. **Getting B's block digests to the source.** There are two designs:
   - **on request:** the source asks for 16 block digests for each changed page, which costs an extra round trip per request (60 ms on a DR link);
   - **up front:** the receiver sends block digests for every page of B in each node it descends, alongside the leaf digests it already sends. No extra round trip, but more digest bytes.
2. **Hashing B's pages in 512-byte blocks** at the receiver.
3. **Writing 512-byte pieces** into B's pages in place, instead of whole runs of pages.

## Design

`experiments/bytes/exp55.py`, over EXP-51's scale-70 data (`scratchpad/exp50-work`: S `uniform-s`, B `template`), every relation file:
- **Digest bytes,** 8 per block, 16 blocks per page:
  - **on request:** the changed pages B has (EXP-54's count);
  - **up front:** every page B has in a descended node, a node of 128 pages with at least one changed page that B has.
- **Hashing CPU,** one thread: keyed BLAKE3 and keyed BLAKE2b, 8-byte digests, over every 512-byte block of each design's pages, read from B. pgdelta hashes on 4 worker processes, so the work divides by up to 4 there.
- **Writing,** on fresh APFS clones of B, read once first so its pages are cached, as after pgdelta's hash pass. Each write mode is timed through `fsync`:
  - **pages:** each of pgdelta's runs written whole;
  - **blocks:** each run's changed 512-byte blocks, with adjacent ones joined into one write.

  3 repeats, alternating which mode goes first. After every blocks run, the clone's relation files are compared with S's byte for byte.
- **The analysis,** committed before the data: `analysis/exp55.py`.

## Predictions

1. **Deciding (the design): digests can go up front.**
   - Up-front digests come to at most 2.5× the on-request digests.
   - Blocks plus zstd, with up-front digests, still send at most 0.35 of zstd alone's bytes.
2. **Hashing is cheap next to the saving:** the faster algorithm hashes the up-front design's blocks in at most 3 CPU-seconds on one thread. That is under 0.75 s on 4 workers, against a projected link saving of 2.7 s at 0.5 Gbit/s (EXP-54).
3. **Writing blocks costs little:**
   - block writes take at most 1.5× the time of whole-run writes, cache warm, `fsync` included;
   - every patched clone equals S byte for byte.

## What follows

- **If all hold:** the sub-page design is settled on these choices: up-front digests, 512-byte blocks, adjacent blocks joined, and the faster hash. Building it is put to the owner with this price attached.
- **If 1 fails:** the design needs the extra round trip, which the send window has to hide at 60 ms. That becomes part of the build's own experiment.
- **If 2 or 3 fails:** that cost is weighed against the saving, and EXP-54's verdict, worth building, is restated with it.

## Results (October 27)

One run of `experiments/bytes/exp55.py` over EXP-51's scale-70 data, with the writes repeated 3 times in alternating order. Data: `docs/experiments/data/exp55-costs.csv`; raw `results/bytes/exp55/results.json`; analysis `analysis/exp55.py`.

| Design | pages of B digested | digest bytes | BLAKE3, one thread | BLAKE2b, one thread |
|---|---|---|---|---|
| on request (an extra round trip) | 71,188 | 9.1 MB | 0.86 s | 0.80 s |
| **up front** (with the leaf digests) | 134,072 | **17.2 MB** | 1.65 s | **1.46 s** |

| Writing into B, cache warm, `fsync` included (median of 3) | writes | bytes | seconds |
|---|---|---|---|
| whole runs (v1 today) | 29,106 | 593.5 MB | 0.80 s |
| changed 512-byte blocks, adjacent ones joined | 149,688 | 109.6 MB | **0.86 s** |

All 3 block-patched copies matched S byte for byte, in every relation file.

| # | Prediction | Verdict |
|---|---|---|
| 1 | **Deciding: up-front digests at most 2.5× on-request; blocks + zstd + up-front digests at most 0.35 of zstd alone** | **Held:** 1.88×; 0.246 (51.8 MB against 210.3) |
| 2 | The faster hash covers the up-front blocks in at most 3 CPU-seconds | **Held:** 1.46 s (BLAKE2b) |
| 3 | Block writes at most 1.5× whole-run writes; every copy equal to S | **Held:** 1.07×; 3 of 3 |

## What it means

**The design is settled:**
- **up-front digests:** B's 512-byte block digests go with the leaf digests the receiver already sends, so there's no extra round trip;
- **512-byte blocks,** 8-byte digests;
- **adjacent changed blocks** written as one piece;
- **BLAKE2b for the blocks,** slightly faster than BLAKE3 at this size through Python (1.46 s against 1.65).

**Its price at this data:**
- **bytes:** 51.8 MB in both directions, against zstd's 210.3 MB today and v1's 593.5 MB;
- **receiver CPU:** about 1.5 CPU-seconds more hashing, under 0.4 s on its 4 workers;
- **writes:** about the same.

**Unmeasured:**
- **The sender's own block comparison:** it would hash S's changed pages in blocks too, a similar 1.5 CPU-seconds or less.
- **The protocol work itself.**
- **A real DR run.**

  The build's own experiment would measure the whole: v1 with sub-page deltas against today's zstd at 0.5 and 1 Gbit/s, with every copy byte-identical to S.
