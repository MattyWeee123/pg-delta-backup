# EXP-83b: What does summarize_wal cost a busy server? (the VMs)

Status: **pre-registered October 29, 2026, before any run.** One `ha` VM pair, about an hour. It is EXP-83's VM stage, and it decides (rule 2): on the Mac, throughput varied 2.9x from run to run and couldn't bound the cost.

## Question

**On the VMs, with the client on another machine as a busy server's clients are:** how much throughput and tail latency does `summarize_wal = on` cost, with the database in memory and beyond it? How much CPU does the summarizer take?

## Design

**The harness:** EXP-83's `bench.py`, run on dest by `experiments/summarize_cost/vm.sh`.
- **The source:** built by `source_cluster.py` at each cell's scale. That is EXP-48's database and set-up: PostgreSQL 17.11, data checksums, `checkpoint_timeout` 30 min, `max_wal_size` 16 GB, `shared_buffers` 128 MB.
- **The load:** pgbench's TPC-B-like script (prepared statements, no vacuum) from dest over the internal network, **32 clients on 8 threads**, 60 s a run.
- **Pairs:** **8 pairs** per cell, on and off in random order, after a 60 s warm-up (off).
- **Before each run:** the setting set, the summarizer caught up (on) or gone (off), and a `CHECKPOINT`, as in EXP-83.
- **Measured over ssh:**
  - the summarizer's CPU from `/proc` on the source (`summarizer_cpu.sh`, 10 ms resolution);
  - the source's CPU busy fraction (`/proc/stat`).

**The cells,** in this order on the `ha` set (n2d-highcpu-8, 150 GiB balanced PDs):

| Cell | Scale | Database | Seed |
|---|---|---|---|
| `mem` | 100 | About 1.5 GB, in the 8 GB VM's memory | 831 |
| `disk` | 600 | About 18 GB, beyond memory | 832 |

**The smoke** (scale 10, 1 pair of 10 s, 5 s warm-up, seed 839) decides nothing. It checks that the client connects, that the setting toggles, and that the CPU readings arrive over ssh.

**The command,** on dest:

    bash experiments/summarize_cost/vm.sh SOURCE 100 32 8 60 8 60 831 mem OUT.csv
    bash experiments/summarize_cost/vm.sh SOURCE 600 32 8 60 8 60 832 disk OUT.csv

**The analysis:** EXP-83's `analysis/exp83.py`, unchanged, on `docs/experiments/data/exp83b-runs.csv`, with the figure `exp83b-cost`.

## Predictions

**From EXP-83's measured cost per MB (inferred):**
- **The summarizer's cost:** about 0.6 ms of CPU per MB of WAL on the Mac's cores. On the VMs' slower cores, about 1 ms.
- **The WAL rate:** a TPC-B-like load from a remote client, bound by round trips at a few thousand to ten thousand transactions a second, writes about 10–40 MB/s.
- **So the summarizer takes 1–4% of one core,** under 0.5% of the source's 8 vCPUs, and throughput should move by under 1%.

**Scored, in each cell:**
1. **Deciding: the throughput ratio (on/off, mean over pairs) has a lower 95% bound of at least 0.97.** `summarize_wal` costs at most 3% of throughput, at 95%.
2. **The mean p99 latency ratio is at most 1.10.**
3. **The summarizer takes at most 5% of one core.**

**Reported:**
- throughput and the source's CPU busy fraction, on and off;
- the WAL rate;
- the summarizer's CPU per MB of WAL;
- mean and median latency ratios.

**Rule 11:** a deciding lower bound within 0.01 of 0.97 gets a second session, with more pairs.

## The rules this session runs under

- **Rule 13:** EXP-83 was the local stage. Its throughput couldn't bound the cost, so this session runs on the arithmetic above. Its cost per MB is carried over.
- **Rule 16:** `analysis/integrity.py register EXP-83b` records the analysis, `bench.py`, `vm.sh` and `summarizer_cpu.sh`.
- **Rule 17:** the paired, randomized design and the interval are the noise bar.

## What follows

- **If 1–3 hold in both cells:** `summarize_wal` costs a busy server under 3% of throughput.
  - **The advice:** turn it on, and pgdelta's summaries path applies to every copy taken after that (EXP-79b, -80b, -81). Copies taken before it are covered by summaries on demand where the WAL is kept (EXP-82c), and by page-lsn where it isn't (EXP-82b).
  - **Where the advice goes:** RESULTS and the README.
- **If 1 fails in a cell:** the cost is stated as measured, with its share of the source's CPU. The advice becomes a trade-off, not a default.

## Workflow check

| | Check | For this session |
|---|---|---|
| 1 | Situation, standard, competitor | PostgreSQL's default is off; pgdelta's fastest routes need it on |
| 2 | The bottleneck | The source's cost of the setting |
| 3 | Systems or algorithm | Neither: a deployment cost |
| 4 | Papers and prior art | None measured it (EXP-83's search) |
| 5 | Committee-grade evidence | Pre-registered; paired, randomized; the client remote |
| 13 | The cascade | The VM stage, after a noise-bound local stage |
| 14 | The idea archive | "Advise summarize_wal on", to be archived with the result |
| 15 | Held-out workloads | Not owed |
| 16 | Scorer integrity | Recorded at registration |
| 17 | The noise bar | The paired design and its interval |
