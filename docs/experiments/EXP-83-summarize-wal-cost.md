# EXP-83: What does summarize_wal cost a busy server? (the local stage)

Status: **pre-registered October 29, 2026, before the registered run** (the harness's smoke is listed below). Local, about 25 minutes. EXP-83b repeats it on the VMs, which decide (rule 2).

## Question

**pgdelta's fastest routes all depend on WAL summaries:**
- the summaries' fast path (EXP-79b, -80b, -81);
- summaries switched on at refresh time (EXP-82c).

**PostgreSQL ships `summarize_wal` off.** If it costs a busy server little, the advice is simply to leave it on, and pgdelta's fastest route applies everywhere.

**What has been published:**
- **PostgreSQL's documentation** describes the setting without a word on its cost.
- **No benchmark of its cost was found.** One development-time mailing-list note reports the summarizer's memory after creating a relation (about 372 MB in its context, for a 26 MB summary).
- **A reported bug:** the summarizer could hang when the setting was toggled while WAL was being recycled. Fixes were backpatched to 17. The harness times out visibly if it recurs.

**The question: on a busy server, how much throughput and tail latency does `summarize_wal = on` cost, and how much CPU does the summarizer use?**

## Design

**The harness:** `experiments/summarize_cost/bench.py`, with `local.sh` building the local server.
- **The server:** a fresh PostgreSQL 17.11 on the Mac (15 CPUs), data checksums, the VMs' WAL settings (`checkpoint_timeout` 30 min, `max_wal_size` 16 GB), `shared_buffers` 128 MB, pgbench at **scale 50** (about 750 MB, in memory).
- **The load:** pgbench's TPC-B-like script, prepared statements, no vacuum, **16 clients on 4 threads, 30 s a run**, on the same Mac.
- **Pairs:** 10 pairs of runs, one with `summarize_wal` on and one off, the order within each pair random (seed 830), after a 30 s warm-up run (off, not scored).
- **Before each run:**
  - the setting is set (`ALTER SYSTEM`, a reload);
  - for "on", the summarizer is waited for until it has started and caught up to the current LSN, so a run measures it keeping pace, not a backlog;
  - for "off", until it has gone;
  - then a `CHECKPOINT`, so every run starts at the same point in the checkpoint cycle, with its burst of full-page writes.
- **Each run records:**
  - pgbench's throughput;
  - every transaction's latency (`pgbench -l`: the mean, 50th, 95th and 99th percentiles);
  - the WAL generated;
  - the summarizer's CPU time over the run (`ps`).

**The smoke** (not scored; scale 10, 8 clients, 8 s runs, 1 pair, seed 839) ran and recorded every field. The summarizer used 0.18 s of CPU for 263 MB of WAL, about 0.7 ms per MB.

**The command:**

    bash experiments/summarize_cost/local.sh docs/experiments/data/exp83-runs.csv 50 16 4 30 10 30 830

**The analysis,** committed before the data: `analysis/exp83.py`, dry-run on the smoke.
- **What it computes:** each pair's ratio on/off of throughput, mean latency and p99 latency, and their mean over the pairs with a 95% t-interval.
- **Figure** `exp83-cost`.

## Predictions

**What the smoke and the arithmetic imply (inferred):**
- **The summarizer's CPU:** about 0.7 ms per MB of WAL. At the tens of MB/s a busy TPC-B-like load writes, that is a few percent of one core.
- **On a 15-CPU machine** that is a fraction of a percent of its capacity. So throughput should move within the noise.

**Scored:**
1. **Deciding: the throughput ratio (on/off, mean over pairs) has a lower 95% bound of at least 0.97.** That is, `summarize_wal` costs this server at most 3% of throughput, at 95% confidence.
2. **The mean p99 latency ratio is at most 1.10.**
3. **The summarizer uses at most 5% of one core** at this load's WAL rate.

**Reported:** throughput on and off, the WAL rate, the summarizer's CPU per MB of WAL, and the mean and median latency ratios.

## The rules this session runs under

- **Rule 13:** the local stage. EXP-83b on the VMs decides (rule 2), whatever this finds. Its ratio and the VMs' go into the proxy-fidelity log.
- **Rule 16:** `analysis/integrity.py register EXP-83` records the harness and the analysis.
- **Rule 17:** the deciding bar is a 95% interval over 10 pairs. Rule 17's within-session spread (2.40% for runs of 10 s or more) is the expected noise per pair. The paired, randomized design averages it down.

## What follows

- **If 1–3 hold:** the cost is small here. EXP-83b measures it on the VMs (8 vCPUs, where a core is a larger share), in memory and beyond.
- **If 1 fails:** the cost is larger than the arithmetic says. The summarizer's CPU and the latency percentiles show where, and EXP-83b looks for it on the VMs.

## Workflow check

| | Check | For this experiment |
|---|---|---|
| 1 | Situation, standard, competitor | PostgreSQL's default is off; pgdelta's summary routes need it on; WAL-G needs nothing |
| 2 | The bottleneck | Not a refresh bottleneck: the source's cost of the setting |
| 3 | Systems or algorithm | Neither: a deployment cost |
| 4 | Papers and prior art | The documentation (silent on cost); no published benchmark found; the toggle bug |
| 5 | Committee-grade evidence | Pre-registered; paired, randomized, with intervals |
| 13 | The cascade | The local stage; the VMs decide |
| 14 | The idea archive | "Advise summarize_wal on" to be archived with the result |
| 15 | Held-out workloads | Not owed |
| 16 | Scorer integrity | Recorded at registration |
| 17 | The noise bar | Handled by the pairs and the interval |
