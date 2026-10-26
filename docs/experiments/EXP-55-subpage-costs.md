# EXP-55: What would sub-page deltas cost beyond bytes?

Status: **pre-registered October 27, 2026, before any run.** Local (the Mac), over EXP-51's data, as EXP-54. Lean: one script, a few minutes. It prices the three costs EXP-54 left out, so that a build has a concrete design and price before anyone decides on it.

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
