# EXP-31: Without its second pass over B, does v1 recover the HA case beyond memory?

Status: **pre-registered October 25, 2026, before any run.** [EXP-22](EXP-22-ha-scale-model.md)'s scale model again, at its deciding size: a 12 GB database on 8 GB machines, on balanced persistent disks, across two zones. Now with v1's new `--verify-scope written` (built since: it verifies each file from B's digests and the pages it wrote, without re-reading B) beside v1 as before. One VM pair (`PAIR=ha`), about 1 hour.

## Question

EXP-22 put v1 out of HA beyond memory, at 1.77–2.14 times one `pg_basebackup` stream. Its timers showed why: v1 read B twice at the disk's rate, once to hash it and once to verify the result, where a full copy makes one write pass. With the verification pass gone, where does v1 land?

## Design

EXP-22's harness command at scale 400 (about 12 GB):
- hot and uniform 1%, unthrottled;
- CRC32C manifests, caches dropped before each run;
- set file `infra/gcp/sets/ha.sh` (two `n2-highcpu-8` in `us-central1-a` and `-b`, balanced persistent disks of 150 GiB).

Arms:
- `full` (one `pg_basebackup` stream, the released tools' yardstick);
- `parcopy8` (the parallel bound, rule 3's yardstick);
- `pgdelta_direct_inplace` (v1 as in EXP-22);
- **`pgdelta_direct_inplace_vw`** (the same, with `--verify-scope written`).

2 repeats, order seed 31400. Every output verified by the harness, as in EXP-22.

## Predictions

From EXP-22's timers: v1 hashed B in 64–71 s and its verification took 61–82 s; the full copies took 58–62 s.

1. `pgdelta_direct_inplace_vw` takes at most 0.7 times `pgdelta_direct_inplace`'s time, for both workloads (the verification pass removed).
2. **Deciding: v1 with `--verify-scope written` is still not faster than either full copy at 12 GB.** Its ratio to `full` and to `parcopy8` is at least 0.90 for both workloads: one read pass over B at the disk's rate is about one full copy's write pass, and on balanced persistent disk reading B costs what writing a copy does.
3. Its ratio is within 1.5 of both full copies (EXP-22: 1.77–2.26).

## What follows (K6: both yardsticks, separately)

- **If 2 holds:** HA beyond memory stays out on this disk. The gap shrinks to what one read of B costs, and closing it needs B's digests without reading B: a tree kept from the previous refresh, which is only safe if B hasn't changed since. `--verify-scope written` is still worth its time wherever v1 is disk-bound, subject to EXP-32's correctness campaign.
- **If v1 with `written` comes in under 0.90 of a yardstick:** HA beyond memory is back in against that yardstick, provisional until a second session (K10), and only once EXP-32 has passed.

## Limitations

EXP-22's, plus: 2 repeats, one session, the 12 GB size only.
