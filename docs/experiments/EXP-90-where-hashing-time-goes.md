# EXP-90: Where the hashing path's time goes, on both VMs, second by second

Status: **pre-registered October 31, 2026, before any scored run.**

## Why

At the October 30 weekly meeting, the project's industry mentor reviewed the hashing approach. He asked for four things:
1. **Measure each side's link use over time before optimizing.** He expected one side busy and the other in peaks and troughs, from blocking.
2. **Batch and pipeline the hash exchange,** so both sides hash at once and something is always on the wire.
3. **Try a faster hash:** SHA-256 with the CPU's instructions, or a native (Rust or Go) path, in place of BLAKE3 from Python.
4. **A baseline the team can run.**

**What the project already knew:**
- **Item 2 is how the protocol works.** The receiver hashes B and sends each file's node digests in one frame. The sender, hashing S at the same time on its workers, asks once per file for the leaf digests of the nodes that differ, and it keeps 4 files ahead (`--window`), so those round trips overlap hashing.
- **In EXP-89b's hashing run with scattered changes** (186 s), the sender waited on the receiver for 0.15 s in all. It spent 93 s blocked in `send()`. The receiver's phases took:
  - 101 s hashing B;
  - 121 s syncing files in its main loop (`--fsync main`, hashing's default);
  - 81 s verifying.
- **Research loop 1:** the hash path runs at 85% of its ceiling in memory. Beyond memory, reading B is the floor: hashing can at best tie a re-clone.
- **What was never measured:** a picture of each resource over time. Also unmeasured: whether the disk's reads and writes share one budget, which decides if hashing's scattered case is at its bound (an algorithm problem) or leaves room for overlapping work (a systems problem).

## Question

1. **Where does each run's time go,** second by second, on both VMs? This covers the link, each disk's reads and writes, the CPUs, and the busiest thread.
2. **Is pgdelta's hashing at the bound of its slowest resource,** or is there room for the pipelining the mentor describes?
3. **Is the hash function ever the bottleneck,** and is SHA-256 faster than BLAKE3 on these VMs' CPU?
4. **At 1 Gbit/s,** where the mentor expects the network to bind, which side wins and why?

## Design

**The session is the team's baseline,** `experiments/baseline/run.sh`, with `NAME=exp90 RESULTS=results/exp04/exp90 PAIR=ha` and its defaults:
- **The VMs:** two `n2d-highcpu-8` VMs in two zones of one region, each with a 150 GB balanced persistent disk.
- **The data:** PostgreSQL 17.11, scale 600 (18 GB on 8 GB VMs).
- **The lab:** `experiments/network/lab_exp64.sh` (EXP-64's), with `UTIL=1`.

**The profile, before the lab:**
- **Hash rates:** `hashbench.py` on each VM: BLAKE2b, BLAKE3 and SHA-256, keyed as pgdelta keys a leaf, per 8 KiB page, in 1 and 4 processes, 5 repeats of 3 s.
- **Disk limits:** `fio_mixed.sh` on each VM at once, with direct I/O on a 20 GiB file, 30 s per configuration:
  - 1 MiB sequential reads (queue depth 8) and 8 KiB random writes (queue depth 32), each alone and then both at once;
  - 1 MiB sequential writes and 8 KiB random reads, alone.
- **The link:** `link_probe.py` runs one `iperf3` stream from the source for 10 s, unshaped and at 1 Gbit/s, and counts the bytes the source sent.

**The arms,** in the `hot` and `uniform` patterns (EXP-64's), one run each, shuffled (seed 900):

| Arm | What runs |
|---|---|
| `full` | `pg_basebackup` into a new directory |
| `hash` | pgdelta hashing both copies (no fast path; `--fsync main`) |
| `hashw` | `hash` with the receiver's syncs on its worker (`--fsync worker`) |
| `fast8wf2` | the WAL-summary fast path with its defaults (8 readers, 2-page fills, the sync worker) |
| `X_1g` | arm X on a link shaped to 1 Gbit/s: `throttle.sh` on both VMs, from just before the clock to just after it |

Each run starts from a fresh copy of B0 with both VMs' caches dropped. Every pgdelta copy is started and compared with the source.

**The sampler:** `util_series.py` on both VMs, every 0.1 s, from 1 s before the clock to 1 s after it. It records:
- bytes received and sent;
- the `/data` disk's reads, writes, sectors and busy time;
- CPU busy and total jiffies, per CPU;
- every 0.5 s, the CPU time of each thread of pgdelta, its hashing workers, `pg_basebackup` and walsenders.

**The smoke** runs first, at scale 20 with the same arms (seed 909); it decides nothing.

**The bound arithmetic** (`analysis/exp90.py`, committed before the data):
- **Capacities, measured in this session:**
  - each disk's read and write limits in bytes and in operations;
  - its mixed point: reads at a fraction r_mix of their rate alone and writes at w_mix, when both run at once;
  - the link's rate in the sampler's bytes;
  - each VM's CPUs, and one core per thread.
- **A disk's use,** in a second or over a run: max(u_r, u_w, (u_r + u_w) / (r_mix + w_mix)). Here u_r and u_w are the larger of bytes and operations against their limits. That is the sum where reads and writes share one budget (r_mix + w_mix near 1), and the larger of the two where they don't (near 2).
- **T_impl** is the largest time any one resource needed for the run's own measured work: either disk, the link either way, either VM's CPUs, or the busiest thread. A run can't be shorter. Its time over T_impl is the room left for overlapping work.
- **T_alg,** for the hashing arms, is the same with one read of the database in place of the disks' measured reads: the work hashing can't avoid.
- **Slack** is the share of a run's seconds in which no resource is at 0.8 of its capacity.

## Predictions

**From earlier sessions (inferred):**
- **Hashing, hot:** one read of each copy, 17.8 GB at about 205 MB/s, is about 87 s, against about 88–91 s measured (EXP-80b, EXP-89b).
- **Hashing, uniform:** the 624,000 pages written at EXP-48's 6,750 a second take about 92 s. With B's read, about 179 s, against 180–190 s measured. That fits a disk whose reads and writes share one budget.
- **At 1 Gbit/s,** a re-clone needs about 150 s for 18 GB.

| # | Prediction | Bar |
|---|---|---|
| D1 (deciding) | **Hashing is at its bound:** every `hash` run (hot and uniform, full link and 1 Gbit/s) has measured / T_impl ≤ 1.15 | ≥ 1.30 in any run is a pipelining gap, a systems problem the mentor's direction could take; between is partial |
| D2 | **At 1 Gbit/s:** `pg_basebackup` keeps the link busy (mean use ≥ 0.85 in both patterns). Hashing wins where few pages change (`hash_1g` ≤ 0.70x `full_1g`, hot) and loses where many do (≥ 1.0x, uniform), since it is bound by the disk, not the link | As stated |
| D3 | **CPU is never the bottleneck of hashing:** no `hash` or `hashw` run's T_impl is set by a VM's CPUs or a thread. **On these VMs' CPU, SHA-256 is not faster than BLAKE3** per 8 KiB page (one process, median ratio ≤ 1.0), though on the Mac it was 1.33x | As stated |
| D4 (the bound's control) | **`pg_basebackup` is at its bound** in all four cells: measured / T_impl ≤ 1.10. It streams one way and is the mentor's "most optimized path". If it misses, the bound arithmetic or the capacities are wrong, not pgdelta | 1.10 |

**Reported:**
- **`hashw` over `hash`** (exploratory; predicted 0.90–1.05 where the disk's budget is shared);
- **the disk model:** r_mix and w_mix (predicted shared, r_mix + w_mix ≤ 1.2);
- **per run:**
  - the mean use of each resource, the binding resource and the slack;
  - pgdelta's waits on the receiver and its time in `send()`;
  - the phases' timers;
- **the hash rates on each VM;**
- **the cross-session control:** `full` against EXP-89b's 91.2 s;
- **the figures:**
  - `exp90-timelines`: each run second by second;
  - `exp90-bounds`: each run against each resource's time.

## What follows

- **If D1 holds:** hashing beyond memory is at its bound on this disk, and its loss in the scattered case is the algorithm's (reading all of B), not blocking. No pipelining can recover it. RESULTS and the mentor's answer say so with the timelines.
  - **A structurally different idea is the next step:** keep B's digests from the refresh that wrote B, so B needn't be read again where it is proven unchanged since. It goes into the idea archive.
- **If D1 fails,** the timelines show where each side waits. The candidates are the main-loop syncs (`hashw` measures that one), the verification reads and the order of B's reads and writes. Each would go through the cascade (tests, then a pre-registered VM session).
- **If D3's SHA-256 part fails** (SHA-256 is faster on the VMs' CPU), SHA-256 becomes a hash option only if some run is bound by CPU, since the hash function's share of every run's time decides its effect.

## The rules this session runs under

- **Rule 13:**
  - the tests (`tests/test_exp90_analysis.py`) and a dry run of the analysis on synthetic data come first;
  - the Mac's hash rates are the local stage (`docs/experiments/data/exp90-hashbench-mac.json`): SHA-256 at 1.33x BLAKE3 per page;
  - the VM ratio makes the proxy pair.
  - No local scale model stands in for the disks: rule 13's proxy log puts local timing outside its band beyond memory.
- **Rule 15:** no default changes and no claim leaves the project from this session, so no held-out session is owed. A default change from `hashw` would need one.
- **Rule 16:**
  - `analysis/integrity.py register EXP-90` records the analysis, the baseline script, the lab, the sampler, the probes, the throttle, the Mac's rates and pgdelta's modules;
  - every pgdelta copy is compared with the source;
  - D4 is the bound's control.
- **Rule 17:** one run per arm. Within a session, two runs differ by up to 2.23% (10 s or more) and 7.11% (under 10 s). D1's and D4's bars are about 15 points from the predictions. A deciding ratio within 0.03 of its bar gets a second session.

## Workflow check

| | Check | For this experiment |
|---|---|---|
| 1 | Situation, standard, competitor | Hashing against a re-clone (`pg_basebackup`) beyond memory and at 1 Gbit/s; we tie or lose beyond memory (EXP-89b, measured) and win at DR rates (EXP-15, -20, -24) |
| 2 | The bottleneck | This experiment: each resource over time, and each run against its slowest resource |
| 3 | Systems or algorithm | D1: measured against T_impl and T_alg, with the disk's mixed capacity measured |
| 4 | Papers and prior art | The mentor's review (batching, pipelining, SHA-256 or native code); research loop 1's ceiling and bound; EXP-48's and EXP-66's disk limits |
| 5 | Committee-grade | Pre-registered; capacities measured in the session; a control (D4) for the bound itself; inferred predictions from EXP-48, EXP-66, EXP-80b, EXP-89b |
| 13 | The cascade | Tests, then the Mac's hash rates, then the VMs; the SHA-256/BLAKE3 pair is logged |
| 14 | The idea archive | Read. Structurally different: B's digests kept from the refresh that wrote it (new row); `hashw` is tuning |
| 15 | Held-out workloads | None owed (no default, no claim leaving) |
| 16 | Scorer integrity | Registered at the registration commit; correctness gate on every copy; D4 is the control |
| 17 | The noise bar | 2.23% (runs of 10 s or more), 7.11% (under 10 s); bars about 15 points away |
