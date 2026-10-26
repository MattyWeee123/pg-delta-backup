# EXP-54: Do sub-page deltas still pay on top of zstd?

Status: **pre-registered October 27, 2026, before any run.** Local (the Mac): only bytes and CPU, no network. Lean: one script over existing data, a few minutes. It decides whether sub-page deltas, the largest open lever, are worth building.

## Question

EXP-37 measured two ways to send fewer bytes for scattered updates, each against plain positional pages:
- **sub-page deltas:** only the 512-byte blocks of each page that differ from B's page at that offset, 5.0× fewer bytes including B's block digests;
- **zstd:** 2.8× fewer.

zstd has since shipped and pays below about 4 Gbit/s (EXP-53). Sub-page deltas would mean a protocol change:
- B's block digests travel to the source;
- the source sends a per-page bitmap and the changed blocks;
- the receiver patches blocks in place.

Is that still worth building, once the blocks are compressed too? If sub-page plus zstd sends little less than zstd alone, the answer is no.

## Design

`experiments/bytes/exp54.py`, over EXP-51's data (`scratchpad/exp50-work`):
- **The data:** S is the scale-70 template after EXP-37's uniform update (1% of rows at random), as EXP-47, -52 and -53 sent; B is the template. Same scale and pattern as the VM experiments.
- **The files:** every relation file (base/, global/), with pgdelta's runs: consecutive changed pages, at most 128 per run.
- **Four ways to send each run,** each as one frame:
  - `positional`: the run's pages, as v1 sends them;
  - `positional_zstd`: the same, zstd level 3 per run, as `--compress zstd` sends them;
  - `subpage`: for each page, a 2-byte bitmap of its 16 blocks of 512 bytes, then the blocks that differ from B's page at that offset. A page B lacks goes whole, all 16 bits set;
  - `subpage_zstd`: the same, zstd level 3 per run.
- **The way back:** sub-page needs B's block digests, 8 bytes per block, 128 bytes per page that B has. They are random, so they count uncompressed in both sub-page schemes. Frame headers and other control bytes are the same in every scheme and aren't counted.
- **CPU:** the seconds zstd takes to compress and decompress each scheme are recorded beside the bytes. Computing the block digests isn't measured.
- **The analysis,** committed before the data: `analysis/exp54.py`, figure `exp54-subpage-on-zstd`.

## Predictions

1. **Deciding: sub-page plus zstd sends at most half the bytes of zstd alone,** counting both directions: `subpage_zstd + digests ≤ 0.5 × positional_zstd`.
2. **Sub-page alone matches EXP-37 at this scale:** `positional ÷ (subpage + digests)` is within 15% of EXP-37's 5.0×.
3. **zstd alone matches EXP-37:** `positional ÷ positional_zstd` is within 10% of 2.83×.

## What follows

- **If 1 holds:** sub-page deltas are worth building for DR links. The analysis projects what they save at 0.5, 1 and 2 Gbit/s from bytes alone, labeled as projection, and a design and its own experiment get pre-registered.
- **If 1 fails:** zstd already captures most of what sub-page deltas would. They are set aside, and RESULTS.md says so with the numbers.
