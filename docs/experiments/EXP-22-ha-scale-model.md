# EXP-22: Does v1's HA lead survive Omni's conditions: persistent disks, two zones, and data larger than memory? (a scale model)

Status: **pre-registered October 25, 2026, before running.** One VM pair for about 2 hours. It replaces [EXP-14](EXP-14-ha-persistent-disk.md)'s 30-hour sessions (on hold) with a scale model: instead of a 48.5 GB database on 32 GB machines, a 12 GB database on 8 GB machines, the same ratio of data to memory (1.5×).

## Why

EXP-11's HA verdict, and EXP-18/19's finding that v1 is bound by the destination's CPU while `pg_basebackup` is bound by its disk, all ran on local NVMe in one zone, with 2.1 GB that fits in memory. Omni's HA rebuild runs on GKE's balanced persistent disk, across zones, often at sizes beyond memory. On a slower disk the full copy slows, but v1 also reads both copies once to hash them and re-reads what it verifies; beyond memory, its second pass over B can't come from page cache.

## Design

- **VMs** (set file `infra/gcp/sets/ha.sh`): two `n2-highcpu-8` (8 vCPUs, 8 GB), the source in `us-central1-a` and the destination in `us-central1-b`, each with a 500 GiB balanced persistent disk (140 + 0.28 MiB/s per GiB, about 280 MiB/s) as `/data`, ext4. The round trip and one-stream rate between the zones are recorded by the harness's link tests.
- **Arms:** `pgdelta_direct_inplace` (v1, 4 jobs, in place), `full` (one `pg_basebackup` stream: the released tools' yardstick), `parcopy8` (the 8-stream parallel bound: rule 3's yardstick), unthrottled. Durability and verification as EXP-11.
- **Sizes:** scale 70 (about 2.1 GB, fits in memory) and **scale 400 (about 12 GB, 1.5× memory), which decides**. Workloads hot and uniform 1%; 3 repeats; caches dropped before each run, as in EXP-11.

## Predictions (on the measured disk rate D, from the harness's records)

1. At 12 GB, `full` and `parcopy8` each take within 30% of 12 GB ÷ D plus their 2.1 GB overhead (disk-bound).
2. At 2.1 GB, v1 takes at most `full`'s time for both workloads (its second pass over B still comes from page cache).
3. **At 12 GB, v1 is out:** v1 ÷ `parcopy8` and v1 ÷ `full` are both at least 1.00 for both workloads, because v1 reads both copies and re-reads what it verifies from a disk that caps every arm. This is the deciding prediction.

## What follows (K6: both yardsticks, separately)

- **v1 ≥ 1.00 against both, both workloads:** HA is out as a speed target on Omni-like disks beyond memory, against the fastest copy and the released tools. The positioning drops HA speed and keeps DR for local changes (EXP-20); v1's remaining HA argument would be source slowdown (EXP-12) or bytes, not time.
- **v1 ≤ 0.90 against `full` but not against `parcopy8`:** v1 beats the released tools only; HA's claim names them.
- **v1 ≤ 0.90 against both:** v1's HA lead survives these conditions; EXP-11's "out on fast links" was a local-NVMe effect.
- A deciding ratio within 20% of its threshold is provisional for one session (rule 11, as EXP-20).

## Limitations

A scale model: 12 GB on 8 GB machines, not 1 TB on 64 GB; the ratio holds, the absolute times don't. One session; one disk size (500 GiB); n2-highcpu-8's CPUs are the same family as EXP-11's n2-standard-8.
