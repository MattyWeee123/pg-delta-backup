# EXP-29: Is keyed BLAKE3 at least 1.5 times faster per core than pgdelta's keyed BLAKE2b?

Status: **pre-registered October 25, 2026, before any VM run.** Ledger test T5, lean: its registered gate only. Two VMs (`PAIR=hash`: an Intel `n2-standard-8`, Cascade Lake like every VM so far, and an AMD `n2d-standard-8`), about 15 minutes, plus the Mac.

## Question

pgdelta hashes every 8 KiB page with keyed BLAKE2b, and on a busy source that CPU is what made 4 workers miss O4 in [EXP-28](EXP-28-live-slowdown.md). Would keyed BLAKE3 (the owner allowed a compiled hash library, K5) be fast enough per core to matter?

## The registered rule (T5's, unchanged)

"Under 1.5 times faster per core keeps BLAKE2b." It is judged on both CPU families: BLAKE3 passes only if it is at least 1.5 times faster on the Intel VM and on the AMD VM.

## Design

- **`experiments/network/hashbench.py`** hashes 8 KiB pages exactly as `pgdelta.hashing.Hasher.leaf` does: a pre-keyed state copied, updated with the page, a 16-byte digest.
  - BLAKE2b from `hashlib`;
  - BLAKE3 from the `blake3` package (1.0.x, the Python binding of the official Rust implementation), in a venv.
- **Each measurement:** N worker processes for 3 s, 5 repeats, the two hashes alternating. Pages per second per process, medians, and the ratio.
- **At N = 1** (per core, the gate) and **N = 4** (as pgdelta's default workers run, where turbo and shared caches may differ). The CPU model and its SIMD flags are recorded.
- **The Mac** (Apple silicon, NEON) is reported beside the VMs. Its quick check before registration gave 1.59×.
- **Per page, not per byte.** pgdelta's tree needs a digest per page, so per-call overhead counts as it does in pgdelta. BLAKE3's parallelism across 1 KiB chunks reaches only 8 chunks per page.

## Predictions

1. **Deciding: BLAKE3 passes the gate on both families** (≥ 1.5× at N = 1). On x86 its AVX2 and AVX-512 paths hash several chunks of a page at once, while BLAKE2b is one sequential compression stream.
2. The ratio is higher on the Intel VM (AVX-512) than on the Mac (NEON, 1.59×).

## What follows

- **If 1 holds:** pgdelta gets a BLAKE3 option, with digests distinguished by algorithm and the BLAKE2b default kept until it is measured end to end:
  - v1's source CPU per refresh;
  - O4 at 4 workers (EXP-28's miss) against 2.
- **If not:** BLAKE2b stays, and T5 closes on its gate.

## Limitations

- A micro-benchmark: it shows the hash's cost, not pgdelta's whole CPU per page (reading, the tree, the protocol).
- One machine type per family.
- Python 3.11 only. T5's wider plan (Python 3.14, sub-interpreters, the free-threaded build) isn't run.
