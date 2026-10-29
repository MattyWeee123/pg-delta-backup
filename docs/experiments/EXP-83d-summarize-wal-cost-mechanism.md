# EXP-83d: Why did summarize_wal cost beyond memory, and what does it cost a CPU-bound server?

Status: **pre-registered October 29, 2026, before any run.** One `ha` VM pair, about 2.6 hours (the stated reason: long blocks with no switch in the data, as the owner asked). It follows EXP-83b and EXP-83c.

## Question

**In two sessions, with the database beyond memory** (17 GB on 8 GB VMs), `summarize_wal = on` came with p99 up 20–40% and throughput about 5% lower. Two explanations remain:
- **H1, the switches:**
  - Turning the setting on made the summarizer first read the WAL written while it was off, from disk, pushing cached pages out before the next runs. EXP-83c's settle runs (60 s) may not have been long enough for a 17 GB working set.
  - The cache was also still warming through each session.
  - In steady state there would be no cost.
- **H2, steady-state competition:**
  - Even when it keeps pace, the summarizer reads WAL that the page cache, under a working set twice memory, has already dropped. It does so on the disk the workload's random reads already saturate.

**Neither earlier session recorded the summarizer's own disk reads, and in neither was any source loaded past 44% CPU.**

## Design

**The harness:** EXP-83c's, extended with this registration.
- **The summarizer's reads,** from `/proc/<pid>/io` on the source, over each run: `read_bytes` (from storage) and `rchar` (from any source, the page cache included), against the WAL generated (`summarizer_cpu.sh`).
- **The source's data disk:** its reads, writes and busy fraction, from `/proc/diskstats` (`data_disk.sh`).
- **Long blocks** (`bench.py --design blocks`) for the beyond-memory cell:
  - the setting is held for a whole block;
  - each block is the switch and the summarizer's catch-up, one unscored run of 180 s, then 3 scored runs back to back in that mode, so no scored run follows a switch;
  - 12 blocks in the order ABBA BAAB ABBA, the first mode drawn by the seed, which cancels a linear drift;
  - 15 minutes of warm-up first (EXP-83c's 5 minutes weren't enough).
- **pgbench `-N`** for the CPU-bound cell: it skips the branch and teller updates, so 90 clients no longer wait on 100 branch rows, as they did in EXP-83c.

**The cells:**

| Cell | Scale | Load | Design | Warm-up | Seed |
|---|---|---|---|---|---|
| `cpu` | 100 (in memory) | `-N`, 90 clients, 8 threads | 8 pairs, a 30 s settle run before each scored run | 60 s | 836 |
| `disk` | 600 (17 GB, beyond memory) | TPC-B-like, 32 clients, 8 threads | 12 blocks, 3 scored runs each, 180 s settle | 900 s | 837 |

**Everything else is EXP-83c's:**
- the `ha` set, the client on dest;
- prepared statements, 60 s scored runs;
- a `CHECKPOINT` before each run, and the summarizer caught up before each "on" run.

**The smoke** (scale 10; blocks, `-N`, 2 blocks of 2 runs; seed 849) decides nothing. It checks that the summarizer's I/O, the disk statistics and both designs are recorded.

**The analysis,** committed before the data: `analysis/exp83d.py`, dry-run on a local smoke (no `/proc` there, so it fails, as it must). Data `docs/experiments/data/exp83d-runs.csv`, figure `exp83d-cost`.

## Predictions

**My expectation, and why (inferred):** the summarizer reads WAL within seconds of its being written, while those pages are the newest in the page cache. So it should read almost none of it from storage. Then nothing remains to compete with the workload but its CPU, about 0.85 ms per MB, or 0.25% of the source here. That predicts H1.

1. **Deciding (the mechanism): in `disk`, the summarizer reads at most 10% of the WAL it summarizes from storage.**
2. **In `disk`, with no switch in the data:** the throughput ratio (block pairs, on/off) has a lower 95% bound of at least 0.97, and the mean p99 ratio is at most 1.10.
3. **In `cpu`:**
   - the source is CPU-bound (busy at least 0.80, on and off);
   - the throughput ratio's lower 95% bound is at least 0.97;
   - the mean p99 ratio is at most 1.10.
4. **In both cells, the summarizer takes at most 1.0 ms of CPU per MB of WAL.**

**The decision on the mechanism,** registered here and stated by the analysis:
- **H1 (the cost was the switches'):** 2 holds.
- **H2 (a steady-state cost from the summarizer reading its WAL from storage):** 2 fails, and the summarizer read at least 50% of its WAL from storage.
- **Unresolved:** anything else, such as a cost while the summarizer reads from the cache. Then the data disk's busy fraction and reads, on against off, are the next clues.

**Reported:**
- the block means;
- the data disk's busy fraction and reads, on and off;
- the summarizer's reads from any source (`rchar`), against the WAL;
- throughput and the WAL rate per cell.

**Rule 11:** a deciding share within 0.05 of 0.10, or a cell's throughput bound within 0.01 of 0.97, gets a second session.

## The rules this session runs under

- **Rule 13:** the VM stage, third session. EXP-83's local stage showed the summarizer's CPU per MB, not throughput.
- **Rule 16:** `analysis/integrity.py register EXP-83d` records `analysis/exp83d.py` and the `analysis/exp83.py` it uses, `bench.py`, `vm.sh`, `summarizer_cpu.sh` and `data_disk.sh`.
- **Rule 17:** pairs and block pairs, with intervals.

## What follows

- **H1:** the advice to turn `summarize_wal` on extends to working sets beyond memory, with a note to give the cache time after turning it on.
- **H2:** the advice for disk-bound servers states the cost and its cause. A fix to weigh: a larger page cache, or WAL on a separate disk.
- **3 holding:** extends the in-memory advice to a CPU-bound server.

## Workflow check

| | Check | For this session |
|---|---|---|
| 1 | Situation, standard, competitor | PostgreSQL's default is off; pgdelta's fastest routes need it on |
| 2 | The bottleneck | Here, the bottleneck being found is the cost's: the summarizer's reads, measured directly |
| 3 | Systems or algorithm | Neither: a deployment cost |
| 4 | Papers and prior art | None measured it (EXP-83) |
| 5 | Committee-grade evidence | Two hypotheses and a decision rule registered before the data; the switch confound designed out |
| 13 | The cascade | The VM stage, third session |
| 14 | The idea archive | "Advise summarize_wal on" pending this result |
| 15 | Held-out workloads | Not owed |
| 16 | Scorer integrity | Recorded at registration |
| 17 | The noise bar | Pairs, block pairs and intervals |
