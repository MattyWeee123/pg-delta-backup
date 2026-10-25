# EXP-30: Does BLAKE3 let v1 use 4 workers on a busy primary?

Status: **pre-registered October 25, 2026, before any run.** The end-to-end step [EXP-29](EXP-29-hash-per-core.md) registered: `pgdelta --hash blake3` (built since, BLAKE2b still the default) on [EXP-28](EXP-28-live-slowdown.md)'s busy primary, unthrottled, where v1 at 4 workers missed O4. Three VMs (`PAIR=t10b` again), about 35 minutes with set-up.

## Question

Does keyed BLAKE3 cut v1's source CPU enough that 4 workers meet O4 on a busy primary, and what does it do to v1's time and its CPU per refresh?

## Design

EXP-28's design and its fixed driver (`lab_exp28.sh`, now with `BLAKE=1`: pgdelta runs on both ends from a venv with the `blake3` package), on fresh VMs:
- the same cluster, the same load (128 clients at 0.6 × C, C measured at set-up), the same windows, numbers and bar;
- unthrottled only, where EXP-28's miss was.

Arms, 2 runs each in an order seed 300 shuffles (12 runs):
- `none` and `full` (the bar);
- `v1_j2` and `v1_j4` (BLAKE2b; EXP-28 again, a same-session control);
- `v1b3_j2` and `v1b3_j4` (`--hash blake3`).

**Added:** the sender's own CPU-seconds, its worker processes included (`cpu_time.py`: the sender run as a child, its `RUSAGE_CHILDREN` read after it exits). An arm whose excess lies within 20% of its δ gets two more runs, as in EXP-28.

## Predictions

1. **Deciding: `v1b3_j4` meets O4 unthrottled.** The hash is most of v1's source CPU per page (13.6 of the µs per 8 KiB page on these VMs), and BLAKE3 cuts it about 4.8 times.
2. BLAKE3 cuts the sender's CPU-seconds per refresh by at least half at both worker counts.
3. `v1_j4` (BLAKE2b) misses O4 again in this session, as in EXP-28.
4. `v1b3_j4` is no slower than `v1_j4`.

## What follows

- **If 1 holds:** with the `blake3` package on both ends, `--hash blake3` makes 4 source workers safe on a busy primary. Live mode then caps the default at 4, not 2, when BLAKE3 is chosen. Whether BLAKE3 becomes the default hash (which needs the package on both ends, or a capability exchange in the protocol) is a separate change, judged with the HA scale model.
- **If not:** live mode stays at 2 workers with either hash, and BLAKE3 is judged on its CPU and time alone.
- **If 3 fails:** EXP-28's 4-worker miss doesn't repeat, and both sessions are reported. K10 (as narrowed) asks for a second session only on a deciding cell near its line.

## Limitations

EXP-28's limitations, plus: one session, unthrottled only. A sender's CPU-seconds include its reading and the protocol, not only the hash.
