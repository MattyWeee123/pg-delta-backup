# EXP-83c: What does summarize_wal cost a busy server? (the VMs, settled, with a saturated cell)

Status: **pre-registered October 29, 2026, before any run.** One `ha` VM pair, about 1.9 hours. It is EXP-83b's second session (rule 11: its in-memory bound missed by 0.006), with EXP-83b's two design faults fixed, and a cell that loads the source's CPU.

## Question

**EXP-83b found no measurable cost in memory, but two faults in its design:**
- **The catch-up confound:** an "on" run that followed an "off" run paid for the summarizer catching up on the previous run's WAL, and beyond memory for the cached pages that read pushed out. Post-hoc, the p99 rise sat in those runs.
- **The cache drift:** the beyond-memory cell's throughput rose through the session as its cache warmed.
- **And it never loaded the server:** 32 clients across zones left the source 19–34% busy.

**The question, as before: what do throughput and tail latency lose with `summarize_wal` on?** This time it is measured in steady state, and on a source whose CPU is loaded too.

## Design

EXP-83b's, with these changes (committed with this registration):
- **`--settle`:** before every scored run, an unscored run in the same mode (after the switch and the summarizer's catch-up). So no scored run pays for a catch-up or the cache it disturbs. Settle runs are written with the label "settle", and the analysis scores only "scored" runs.
- **Warm-up:** five minutes before the beyond-memory cell (one minute before the others).
- **A third cell, `sat`:** 90 clients, to load the source's CPU. It runs on the in-memory cell's source (`SKIP_SETUP=1`). The source's `max_connections` is 100.

| Cell | Scale | Clients (threads) | Warm-up | Settle | Seed |
|---|---|---|---|---|---|
| `mem` | 100 (about 2.8 GB on disk, in memory) | 32 (8) | 60 s | 30 s | 833 |
| `sat` | 100, the same source | 90 (8) | 60 s | 30 s | 834 |
| `disk` | 600 (about 17 GB, beyond memory) | 32 (8) | 300 s | 60 s | 835 |

**Everything else is EXP-83b's:**
- the `ha` set, the client on dest;
- pgbench's TPC-B-like script, prepared, 60 s scored runs, 8 pairs per cell in random order;
- a `CHECKPOINT` before each run;
- the summarizer's CPU from `/proc`, the source's from `/proc/stat`.

**The smoke** (scale 10, 1 pair of 10 s, a 3 s settle, seed 839) decides nothing. It checks the settle runs and the readings.

**The analysis:** `analysis/exp83.py` with `--us-bar 1000`, which scores the summarizer per MB of WAL, since its share of a core scales with the WAL rate. This option was added with this registration; EXP-83 and EXP-83b's scoring is unchanged without it. Data `docs/experiments/data/exp83c-runs.csv`, figure `exp83c-cost`.

## Predictions

**What EXP-83b implies:**
- **In steady state:** no measurable cost in memory or beyond it.
- **In `sat`:** the WAL rate roughly doubles or triples, so the summarizer takes 5–10% of one core. That is about 1% of the source's 8 vCPUs, so throughput should move by about 1% or less.

**Scored, in each cell:**
1. **Deciding: the throughput ratio (on/off) has a lower 95% bound of at least 0.97.**
2. **The mean p99 latency ratio is at most 1.10.**
3. **The summarizer takes at most 1.0 ms of CPU per MB of WAL** (EXP-83b: 0.78–0.85).

**Reported:**
- each cell's source CPU busy fraction, on and off (the claim for a busy server is scoped by it: `sat` is expected to be well above EXP-83b's 34%);
- throughput and the WAL rate;
- mean and median latency ratios.

**Rule 11:** a lower bound within 0.01 of 0.97 is reported as not firm, with the pairs needed to settle it.

## The rules this session runs under

- **Rule 13:** the VM stage's second session.
- **Rule 16:** `analysis/integrity.py register EXP-83c` records the analysis, `bench.py`, `vm.sh` and `summarizer_cpu.sh`.
- **Rule 17:** the paired, randomized design, now with settle runs.

## What follows

- **If 1–3 hold in all three cells:** `summarize_wal` costs a busy server at most 3% of throughput at 95%, and nothing in p99, at a CPU cost of about 1 ms per MB of WAL. RESULTS and the README advise turning it on.
- **If 1 or 2 fails in a cell:** the cost is stated for that cell, with the source's busy fraction, and the advice becomes a trade-off for that kind of load.

## Workflow check

| | Check | For this session |
|---|---|---|
| 1 | Situation, standard, competitor | PostgreSQL's default is off; pgdelta's fastest routes need it on |
| 2 | The bottleneck | The setting's cost to the source |
| 3 | Systems or algorithm | Neither: a deployment cost |
| 4 | Papers and prior art | None measured it (EXP-83) |
| 5 | Committee-grade evidence | Pre-registered; EXP-83b's confound found post-hoc and designed out, verdicts unchanged |
| 13 | The cascade | The VM stage, second session |
| 14 | The idea archive | "Advise summarize_wal on" pending this result |
| 15 | Held-out workloads | Not owed |
| 16 | Scorer integrity | Recorded at registration |
| 17 | The noise bar | Pairs, intervals and settle runs |
