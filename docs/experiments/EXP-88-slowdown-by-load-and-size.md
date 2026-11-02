# EXP-88: On a loaded primary, at three load levels and beyond memory, does pgdelta still cost the load no more than `pg_basebackup` — and what does the WAL-summary fast path cost?

Status: **pre-registered October 30, 2026, before any run and before any VM for it exists.** Three VMs per
session (source, dest, client), three cluster shapes, about 9–12 hours of VM time in all. It serves the three
slowdown gaps [RESULTS](../RESULTS.md) lists as open — a moderate load, a large dirty set, a source near
saturation — and prices two things that have never been measured: the fast path's cost to a live load, and the
cost of the prerequisite it needs.

Where this document and the [decision ledger](../DECISIONS.md) disagree, the ledger wins. Departures from it
are listed under [Changes from the ledger](#changes-from-the-ledger-rule-8).

---

## Question

O4 is the owner's bar: **a refresh must cost the source's load no more than one `pg_basebackup` stream under
the same load.** [EXP-28](EXP-28-live-slowdown.md) met that bar — but at **one** load level, on **one** cluster
of about 1 GB that fits easily in RAM, on local NVMe, with the hashing path only. Its own limitations say so:

> One load (EXP-12's bank load at 0.6 of capacity), one cluster size (about 1 GB, mostly static), one machine
> shape. EXP-12's moderate level and its large-dirty-set cluster are not run.

Four things follow, and none is measured:

1. **Does the verdict hold at other load levels?** A source at a third of capacity has spare CPU; one at four
   fifths does not. O4's answer could differ at both ends.
2. **Does it hold beyond memory?** [EXP-48](EXP-48-disk-term.md) showed that past RAM, v1's verification
   re-read stops coming from the page cache and becomes a disk pass. On a capped disk that is the floor, and it
   is the condition Omni's DR actually has. Every slowdown number so far is from data that fit in RAM.
3. **What does the WAL-summary fast path cost the load?** [EXP-64](EXP-64-fast-path-beyond-memory.md) measured
   its *speed* (18 GB of local changes in 1.98 s, 0.022 of a full copy). Its cost to a workload has never been
   measured at all — the fast path has never been run against a live loaded source.
4. **What does its prerequisite cost *while a backup runs*?** The fast path needs `summarize_wal = on`.
   Its standing cost is **already measured and largely settled** by the EXP-83 series — this experiment does
   **not** claim to measure it first. What EXP-83 did not have is a backup running on top, which is the only
   thing added here. See [What EXP-83 already settled](#what-exp-83-already-settled-do-not-re-claim-it).

---

## What EXP-83 already settled (do not re-claim it)

A four-part series (EXP-83, -83b, -83c, -83d, the last two done October 29, 2026) measured what
`summarize_wal = on` costs a busy server **with no backup running**. Its findings stand, and this experiment
cites them rather than repeating them:

| Condition | Throughput ratio (on ÷ off) | p99 ratio | Sessions |
|---|---|---|---|
| **In memory**, 32 clients | 1.029 [0.9999, 1.058] | 0.999 | two (with EXP-83b's 0.996) |
| **In memory**, 90 clients | 1.010 [0.9991, 1.020] | 0.964 | two |
| **Beyond memory** (17 GB on 8 GB), EXP-83c | 0.948 [0.887, 1.010] | **1.205** [1.030, 1.379] | |
| **Beyond memory**, EXP-83d, no switch in the data | 0.971 [0.925, 1.018] | **1.22** [1.10, 1.35] | two |

So: **in memory it costs nothing measurable; beyond memory p99 rises about 20%.** The summarizer's own CPU is
0.78–0.96 ms per MB of WAL in every cell.

**The mechanism is unresolved, and EXP-83d says so plainly:** its two registered hypotheses were both refuted.
The summarizer read **0.0%** of its WAL from storage — all of it from the page cache — so the cost is not its
own disk reads. Post-hoc, with summaries on the data disk wrote **24% more besides WAL** (33.9 against
27.4 MB/s) and read about 3.5% more, which points at the summarizer **displacing cached data pages**. Those
cells were bound by commit fsyncs on the persistent disk, not by CPU.

**What this experiment adds, and only this:** every EXP-83 cell was idle of backups. Here a backup runs on top
of the summarizer, at three load levels. If the cost is cache displacement, then a concurrent backup — which
reads B in bulk and evicts cache itself — is exactly the condition under which the two could compound. So the
`none` vs `none_nosum` pair here is a **control for the backup arms, and a possible contribution to EXP-83d's
open mechanism question**, not a first measurement of anything.

Because of that, this experiment records disk read and write bytes besides WAL on both ends in every arm, which
is the measurement EXP-83d had to reach for post-hoc. If the 24% write amplification appears again and grows
with a backup running, that is evidence for displacement; if it does not, that is evidence against.

**Prediction 7 is rewritten accordingly** (see Predictions): it no longer asks whether the prerequisite is
cheap — EXP-83 answered that — but whether EXP-83's beyond-memory penalty **survives, vanishes or compounds**
once a backup is running.

## Design

### Staged, so the core question is answered first and cheaply

Revised October 30, 2026, before any run, to put the cheapest decisive stage first. **Each stage gates the
next**: if stage 1 cannot reproduce EXP-28's cell, nothing else runs; if stage 1 shows O4 met at every level,
stage 2 asks whether that survives beyond memory; stage 3 runs only if stage 2 finds a difference to explain.

| Stage | Source and dest | RAM | Data disk | Cluster | Runs | Wall time | Answers |
|---|---|---|---|---|---:|---:|---|
| **1 pilot** | `n2-standard-8` ×2 | 32 GB | local NVMe | ~2 GB (EXP-28's) | 45 | ~1.2 h | **Does O4 hold at 0.3 and 0.8 of capacity?** Plus: what `summarize_wal` costs; is the fast path's window even measurable at 2 GB |
| **2 beyond memory** | `n2-highcpu-8` ×2 | **8 GB** | balanced PD | **18 GB** | 16 | ~2 h | **Does O4 survive beyond memory, and what does the fast path cost once its window is long enough to measure?** |
| **3 page cache** | `n2-standard-8` ×2 | 32 GB | balanced PD, **matched to stage 2** | 18 GB | 16 | ~2 h | *Only if stage 2 differs:* does the effect survive when B fits in cache? |

A `n2-standard-4` client VM runs the load in every stage.

**Why 18 GB on 8 GB of RAM, not 50 GB on 32 GB.** "Beyond memory" is a ratio, not a size: what matters is that
B no longer fits in page cache, so v1's verification re-read becomes a disk pass (EXP-48). 18 GB on an 8 GB
machine is beyond memory by 2.25×, and it is **the exact shape EXP-48 modelled and EXP-64 ran** — so stage 2's
numbers sit directly beside theirs instead of beginning a new size nobody else has measured. It also copies
2.8× less data, and copying B was the dominant cost of the earlier 50 GB plan.

`n2-highcpu-8` keeps 8 vCPUs, so the load still has the CPU EXP-28's load needs, and it is in the **N2** family
— the pool with 200 vCPUs free, not the N2D pool that is full.

### What stage 3 actually isolates (corrected)

An earlier draft said stage 3 "isolates the disk effect". **That was wrong, and the error mattered.** Stages 2
and 3 use the **same** storage (balanced persistent disk) and the **same** data (18 GB); the only thing that
changes is **RAM, 8 GB → 32 GB**. So the contrast is a **page-cache contrast**, not a disk one: it asks whether
the effect survives once B fits in cache, which is a different question from what the disk does.

That distinction is not pedantic here. EXP-83d's unresolved mechanism is **cache displacement** — the
summarizer appearing to evict data pages, with 24% more non-WAL writes to show for it. A page-cache contrast is
exactly the right instrument for that hypothesis, and calling it a disk contrast would have pointed the
analysis at the wrong cause.

**To keep the comparison interpretable, everything but RAM is held matched between stages 2 and 3:**

| Held constant | Value |
|---|---|
| Storage | balanced persistent disk, same size and type |
| Cluster | 18 GB, built by the same script and seed |
| vCPUs | 8 |
| CPU platform | pinned to the same minimum on both (`--min-cpu-platform`), so a different host generation cannot be mistaken for the cache effect |
| Machine family | N2 (`n2-highcpu-8` and `n2-standard-8` are both N2) |
| Zone, image, load, arms, levels, seeds | identical |
| **Varied** | **RAM only: 8 GB → 32 GB** |

Pinning the CPU platform is new in this revision and is the reason the stages are comparable at all: without
it, `n2-highcpu-8` and `n2-standard-8` could land on different host generations and the difference would be
unattributable.

**Known cost of the smaller shape, stated:** with 8 GB of RAM the load's own working set is more disk-bound, so
stage 2's `none` baseline is slower and noisier than stage 1's. Every verdict is a within-session ratio against
`full` under the same load, so this does not bias the comparison — but it widens the intervals, which is why
stage 2 takes two extra runs at any cell landing near its bar.

### The load

EXP-28's, unchanged, so its cell is reproducible: the bank scripts (`commit.sql` at 99, `abort.sql` at 1),
open loop, `pgbench -n -M prepared -c 128 -j 4 -R R`, driven **from the client VM over the network**, every
transaction logged to the client's tmpfs. The client is on its own VM because a client sharing the source's CPU
charges CPU-heavy arms more than an application on another host would feel — the flaw that made EXP-12's
verdict fragile.

**Capacity is measured, not assumed.** C is the median of three 30-second closed-loop runs at 128 clients
after a 30-second warm-up, taken at set-up **in every session**. EXP-12 recorded a trial whose capacity was
10.9% below its own smoke run's, so C is a per-session quantity and the levels are computed from it:

| Level | Rate |
|---|---|
| `L30` | R = 0.30 × C |
| `L60` | R = 0.60 × C (**EXP-28's level**, the reproduction) |
| `L80` | R = 0.80 × C |

Absolute rates will differ between sessions. That is accepted: every number this experiment decides is a
**within-session ratio**, which is what both of the registered formulas are.

### Arms

| Arm | What it is | Why |
|---|---|---|
| `none` | load only, `summarize_wal = on` | the null that W and D are net of |
| `none_nosum` | load only, `summarize_wal = **off**` | **new:** prices the fast path's prerequisite |
| `full` | `pg_basebackup -Fp -X stream -c fast`, one stream, `--sync-method=fsync` | **O4's bar** |
| `v1` | pgdelta live, in place, hashing path, at the shipped default (`--source-kind live`, `--jobs` from the default rule: at most CPUs ÷ 4, so 2 on 8 vCPUs) | the product as it ships |
| `v1_fast` | the same plus `--fast-path summaries` |
| `v1_j4` | **diagnostic:** v1 with `--jobs 4` on the source, at the 0.6 level only — the cell EXP-28's spike came from | **new:** the fast path against a live load |

`v1` and `v1_fast` refresh an old copy B taken at set-up; each run starts from a fresh copy of it. `full`
copies into a new directory, deleted after the run's tail. Both take a fast start checkpoint
(`--checkpoint fast`, pgdelta's default).

The fast path's guard v2 takes B's end only from a clean shutdown's checkpoint, a cleanly stopped standby's
minimum recovery point, or a base backup's manifest. B here is a `pg_basebackup`, so the manifest route
applies and the guard should pass. **An arm whose guard refuses is recorded as refused, not as a failure** —
refusing is the guard's job, and EXP-62 exists because it once failed to.

### What the pilot does and does not replicate (added before running)

EXP-28's headline was a **cliff between 2 and 4 source workers**: at 4 workers p95 went 6.4 → 74.3 ms and p99
9.3 → 114.6 ms, failing its deciding prediction and moving the shipped default to at most 2 workers on
8 vCPUs.

The `v1` arm here runs the **shipped default**, which on an 8-vCPU source is **2 workers**. So on its own this
experiment would reproduce only EXP-28's *two-worker* result and would say nothing about the four-worker spike
that motivated the cap. Claiming otherwise would be the kind of overreach rule 12 asks to be checked for.

Two ways to be honest about that, and this experiment takes the second:

1. narrow the claim to "reproduces EXP-28's two-worker cell"; or
2. **add a `v1_j4` diagnostic cell at the 0.6 level** — EXP-28's own level — so the spike is either reproduced
   or shown absent.

**This is a change to the design, recorded here before any run (rule 8).** It adds 3 runs to stage 1 (48
instead of 45) and nothing to stages 2 or 3. It is a **diagnostic, not a decision**: `v1_j4` is not the
shipped default, so whether it meets O4 changes no product behaviour. Its purpose is to tell a failure of the
replication gate from a change in conditions.

**The gate, stated precisely.** Stage 1 passes, and stages 2 and 3 may run, when both hold at the 0.6 level:

- **`v1` at the shipped default meets O4** — as EXP-28 found for 2 workers; and
- **`v1_j4` misses O4, or meets it only at twice the tolerances** — as EXP-28 found for 4 workers unthrottled
  (0.073 s of work against a 0.05 s floor, 0.047 of a worst drop against 0.03).

If `v1_j4` comes out clean at 0.6, the instrument is not reproducing EXP-28 and nothing downstream is
trustworthy until that is explained. EXP-28 itself flags this risk: the 4-worker miss "did not repeat
identically in the next session", so a second reading here is worth having either way.

### Cells and runs

Each run is a 15 s settle (load only, receiver already listening), the timed arm from t0 to t1 on dest's
clock, then a 10 s tail. Order is shuffled by a recorded seed. **3 repeats per cell.**

| Stage | Arms | Levels | Repeats | Runs |
|---|---|---:|---:|---:|
| 1 | 5 (`none`, `none_nosum`, `full`, `v1`, `v1_fast`) + `v1_j4` at 0.6 only | 3 (0.3, 0.6, 0.8) | 3 | **48** |
| 2 | 4 (`none`, `full`, `v1`, `v1_fast`) | 2 (0.6, 0.8) | 2 | 16 |
| 3 | 4 (as stage 2) | 2 (0.6, 0.8) | 2 | 16 |

*Stage 3 holds storage, cluster, vCPUs, CPU platform, zone, image, load, arms, levels and seeds matched to stage 2, and varies RAM alone.*

**What the later stages drop, and why.** `none_nosum` runs only in stage 1: pricing `summarize_wal` needs a
loaded source, not a large one, and the answer carries. The 0.3 level runs only in stage 1: the load-level
sweep is the pilot's question, while beyond memory the interesting cells are the reference (0.6) and the stress
(0.8). Repeats fall from 3 to 2 in stages 2 and 3, with **two extra runs at any cell whose excess over `full`
lands within 20% of its δ** — EXP-28's own rule, which spends repeats where they change a verdict instead of
spreading them evenly.

80 runs in all, against 135 in the first draft. No link shaping: **no artificial delay is added**. Network
latency is EXP-13's question, not this one.

### What is measured

**Deciding — O4's two numbers,** as EXP-12's change 3 defines them and EXP-28 applied them, from the client's
log over `[t0, t1 + 10 s]` in 1-second buckets, mapped to dest's clock by chrony's offsets:

- the worst drop **D** = max(1 − X_b/S_b);
- the work lost **W** = Σ max(0, S_b − X_b)/R,

where S_b counts the transactions scheduled in a bucket and X_b those completed in it. Net values subtract the
same cell's median over the `none` runs; below 0 counts as 0.

**"Meets O4"** (EXP-12's floors, EXP-28's wording):

> W_net(arm) − W_net(full) ≤ δ_W = max(0.05 s, 0.15 × W_net(full)), **and**
> D_net(arm) − D_net(full) ≤ δ_D = 0.03.

Verdicts are also given at half and twice the floors (K7). An arm whose excess over `full` lies within 20% of
its δ on either number gets two more runs at that cell.

**Reported, not deciding:**

- **p50, p95 and p99** over the window, and the registered increase against the matched `none` cell:

  > p95 increase (%) = (p95 during backup ÷ p95 without backup − 1) × 100

  and the same for p50 and p99.
- **Throughput loss:** TPS loss (%) = (1 − TPS during backup ÷ TPS baseline) × 100.
- **Failed transactions** per arm, from pgbench's own log (a latency field of `failed` or `skipped`). **New:**
  `analysis/exp28.py`'s "failed" counts failed *runs*, not failed transactions.
- backup duration; the source's VM-wide CPU-seconds (`/proc/stat`); disk read and write bytes on both ends.

**Why the percentile ratios do not decide.** A ratio of two p95s compounds the tail noise of both, and at 3
repeats its interval is wide. W and D are an integral and a maximum over 1-second buckets, far more stable, and
they are what O4 is already written in. Inventing a latency pass/fail threshold here would create a second,
weaker bar beside the owner's — so the percentiles are reported beside the verdict and never instead of it.

**Why the null is a matched cell, not a paired window.** A no-backup window of the same duration taken
elsewhere in the run is not matched if the load drifts, and EXP-12 recorded its load's database growing during
the session. So each arm's null is the median of that cell's own `none` runs, with run order shuffled — EXP-28's
method, kept.

### Committed before the data, and recorded under rule 16

`experiments/network/lab_exp88.sh` (the driver, on dest) and `analysis/exp88.py` (the analysis and the figure
`exp88-slowdown-by-load`). Neither may be written after a run.

**Rule 16 (scorer integrity).** Before the first run, `analysis/integrity.py register EXP-88 --commit <sha>`
records the SHA-256 of every file that scores this experiment, runs it, or is under test:

- `analysis/exp88.py` and any helper it imports;
- `experiments/network/lab_exp88.sh`, `source_cluster.py`, `measure.py`, `agent.py`;
- the load scripts `experiments/network/load/commit.sql` and `abort.sql`;
- `infra/gcp/create.sh`, `teardown.sh` and this experiment's set file;
- the pgdelta modules under test: `live.py`, `inplace.py`, `fastpath.py`, `hashing.py`, `cli.py`.

When the results are written, `analysis/integrity.py check EXP-88` must be run and its output quoted in this
document. Any file that changed after registration is listed with the reason it changes no score — the check
exits non-zero on a change, so a silent edit cannot pass unremarked.

Registering `fastpath.py` matters here specifically: `v1_fast` is the first arm to exercise the fast path
against a live loaded source, and a mid-session change to it would invalidate every `v1_fast` cell before it.

---

## Predictions

Written before any run. Each says what would prove it wrong.

1. **`full` costs the load little at `L30` and `L60`** (W_net under 0.1 s, as EXP-28 measured 0.000), **and
   measurably more at `L80`.** *Wrong if `full` costs nothing measurable even at 0.8 of capacity* — in which
   case O4's bar is "nothing measurable" at every level and every arm is judged against that.
2. **`v1` at the shipped two-worker default meets O4 at `L30` and `L60` in shape P**, reproducing EXP-28's
   *two-worker* cell — and only that cell. *Wrong if it misses at `L60`*, which would contradict EXP-28 and
   stop the experiment until the disagreement is explained.
2b. **The diagnostic `v1_j4` at `L60` misses O4, or meets it only at twice the tolerances**, reproducing
   EXP-28's four-worker cell. *Wrong if it meets O4 cleanly* — then the instrument is not reproducing EXP-28,
   and the gate fails even though prediction 2 held. EXP-28 records that this miss "did not repeat identically
   in the next session", so this is the prediction most likely to come out ambiguous; an ambiguous result is
   reported as such and not read as a pass.
3. **Deciding: `v1` meets O4 at `L80` in every shape.** *Wrong if it misses at `L80`* — then the shipped
   default is not safe on a source near saturation, and live mode needs a load-aware worker cap (D50, D81).
4. **With B beyond cache (stage 2), `v1`'s work lost is at least twice its value with B cached (stage 3)** at
   the same level, because the verification re-read must then reach the disk rather than the page cache.
   *Wrong if the two differ by less than 1.5×*, which would mean the re-read is not what costs the load beyond
   memory and EXP-48's model does not carry over to slowdown. Note this is a **page-cache** contrast: the disk
   is the same in both stages, so a difference is attributable to what fits in cache, not to the device.
5. **`v1_fast` costs the load less than `v1` at 50 GB** (both shapes), since it reads only the blocks the
   summaries list instead of all of B. *Wrong if it costs the same or more* — then the fast path buys speed at
   no saving in load, and its 1.98 s win comes with an unchanged bill.
6. **In shape P the fast path's window is under 1 second,** so its W and D are **not judged** there: fewer than
   two 1-second buckets cannot support either number, and a p99 over under a second of transactions is
   meaningless. This is recorded as "too fast to measure", not as a pass. *Wrong if the window exceeds 2 s*,
   in which case P's fast-path cell is judged like the others.
7. **EXP-83's beyond-memory penalty reproduces with no backup, and does not grow when one runs.** Concretely:
   in stage 2's `none` vs `none_nosum` pair, p99(on) ÷ p99(off) falls inside EXP-83d's interval
   [1.10, 1.35]; and the same ratio computed inside the backup window of the `v1`/`v1_fast` arms is **no
   larger** than it is in `none`. *Wrong if the in-window ratio exceeds the no-backup one by more than its own
   interval* — which would mean the summarizer and a concurrent backup compound, supporting EXP-83d's
   displacement reading and making `summarize_wal` a cost that must be reported beside the fast path's speed.
   In stage 1 (in memory) the expectation is EXP-83's: **no measurable difference**, so a difference there
   contradicts two prior sessions and is a reason to stop and explain rather than to publish.
8. **No failed transactions in any arm,** and every copy correct: `pg_verifybackup`, recovery, the load's
   balance invariant, row counts and `pg_amcheck`. *Any wrong output stops the speed work* (rule: correctness
   first).

---

## What follows

- **If 3 holds:** O4 is met across 0.3–0.8 of capacity and at 50 GB, and RESULTS' "within `pg_basebackup`'s"
  row widens from one load level to three and from 1 GB to 50 GB, with its conditions.
- **If 3 fails:** the default worker count becomes load-aware, and the cap is reported with the time it costs.
  That is a product change, so it is the owner's.
- **If 4 holds:** slowdown beyond memory is a disk effect, and the HA/DR guidance must say that v1's cost to a
  loaded source rises once B stops fitting in cache.
- **If 5 fails:** the fast path's pitch changes from "faster and cheaper" to "faster, same cost", which matters
  for whether it should ever be the default.
- **If 7 fails:** every slowdown number in EXP-12, EXP-28, EXP-30 and EXP-39 contains an unpriced `summarize_wal`
  overhead, and each needs a note.
- **Rule 11:** a deciding ratio within 0.05 of its bar gets a second session on a fresh pair, on another day.

---

## Limitations, known before running

- **One load shape.** EXP-12's bank scripts. TPROC-C is a follow-up (below), not this.
- **B is minutes old, not hours.** Each `v1` run refreshes the previous run's output, so it is the
  frequent-refresh case. A refresh after hours of churn reads and sends more (EXP-38's axis).
- **No added latency.** Source and dest are in one region at the zone's own rate. DR's round trips are
  EXP-13's.
- **One zone, one image, one session per shape.** Rule 11 gives a second session only where a deciding ratio
  lands near its bar.
- **50 GB is not TB scale.** No ratio is projected past the sizes measured (rule 10).
- **The fast path needs `summarize_wal` on;** Omni ships with it off (EXP-44). So `v1_fast` measures a
  configuration an operator must opt into, and prediction 7 prices that opt-in.
- **`pg_amcheck` on a 50 GB cluster is slow,** so it runs once per cell rather than once per run, after the
  cell's last repeat.

---

## Amendments after the harness was built, before any run (rule 8)

Writing the driver surfaced three things the registration got wrong or left open. All three are recorded here
before any VM exists, and none is resolved by quietly changing what runs.

### 1. `v1_fast` has two *structural* blockers, not just a timing one

The registration deferred `v1_fast` because `--fast-path` now has two modes and page-lsn's default read strategy
is being settled by EXP-87. Building the driver found two further reasons it cannot simply be switched on here,
both in `pgdelta/fastpath.py`:

- **An in-place refresh destroys the lineage proof.** Guard v2 takes B's end only from a clean shutdown's
  checkpoint, a cleanly stopped standby's minimum recovery point, or a base backup's `backup_manifest`
  (`fastpath.py:10-12`). This experiment, like EXP-28, refreshes **one B in place**, so after the first run B is
  the previous run's output rather than the `pg_basebackup` whose manifest the guard reads.
- **The `none_nosum` blocks create a summary gap.** With `summarize_wal` off, no summaries cover that interval,
  so a fast-path run scheduled after an off-block has no coverage from B's end and the guard refuses. The
  driver records `summary_gap` per run so this is visible rather than mysterious.

**Consequence, stated:** `v1_fast` does not run in stage 1, and enabling it without fixing these would produce
a column of refusals that says nothing about the fast path's cost. When it does run it needs **a freshly taken
base backup as B before each fast-path run**, and must not follow a `none_nosum` block. Whether that belongs in
this experiment or in its own is the owner's call; at 2 GB re-taking B is cheap, at 18 GB it is not.

A refusal remains a *recorded pass*, not a failure — the guard refusing is the guard working (EXP-62).

### 2. Prediction 8's correctness checks are not in this driver

Prediction 8 asks for `pg_verifybackup`, recovery, the load's balance invariant, row counts and `pg_amcheck`.
**The driver does not run them, and EXP-28's did not either.** What it does record is per-transaction
`failed`/`skipped` counts and both ends' exit codes.

So prediction 8 is split, honestly:

- **8a, judged here:** no failed or skipped transactions attributable to an arm, and every run exits 0.
- **8b, NOT judged here:** copy correctness. It is a separate pass, and **no speed or slowdown claim from this
  experiment may be published before 8b has run** — the project's rule is correctness first, and a slowdown
  number from a run that produced a wrong copy is meaningless. EXP-86 established correctness for the hashing
  and page-lsn paths on Linux (320 of 320), which is the standing evidence 8b leans on; this experiment adds
  no new correctness evidence and must not be read as if it did.

### 3. Which hash `v1` runs, and why it is the conservative choice

The registration said `v1` runs "the shipped default". pgdelta's shipped default is `--hash auto`, which picks
BLAKE3 where both ends have the package. The driver instead pins **BLAKE2b**, to match EXP-28's cell so the
replication gate compares like with like.

That choice is deliberate and it cuts **against** pgdelta, which is why it is acceptable: EXP-60 measured BLAKE3
at 0.872 of BLAKE2b's time, with each hashing phase about 0.58 of BLAKE2b's, so BLAKE2b spends **more** source
CPU per page. O4 measures what the refresh costs the load, so a BLAKE2b `v1` is the pessimistic arm. If it meets
O4, the shipped configuration with BLAKE3 meets it too.

**What this does not license:** quoting these absolute latencies as the shipped product's. They are the
BLAKE2b configuration's, and the write-up must say so wherever a number is given.

### 4. The scorer was re-registered after the smoke run, and why

Rule 16 first recorded the scorer at `d132fda`. The smoke run (stage 1, 5 runs at L60, **not counted data**)
then showed `analysis/exp88.py` reporting "the fast path did not activate (the guard refused)" for the `v1` and
`v1_j4` arms — which never pass `--fast-path`, so no guard was ever consulted. Two causes: the note did not
separate "refused" from "never requested", and `fast_path_reasons` was never carried from `run.json` into the
run row, so the distinction could not be made. Both fixed at `c663e68`, and rule 16 re-registered there:
**16 of 16 files unchanged**.

This is legitimate precisely because it happened **before any counted run existed**. A smoke run is not scored
data — the project's convention throughout (EXP-28's smoke is recorded "not counted"), and the whole purpose of
rule 16 is that the scorer is fixed before the results it scores. The same change made after the grid would
have had to be declared as a post-hoc change to the scorer, with every number re-read.

Worth recording for its own sake: **the synthetic fixture could not have caught this.** It never wrote a run
that asked for the fast path and was refused, so the two cases were indistinguishable in the tests while being
distinguishable in life. The test that asserted the old wording was itself encoding the bug.

### 5. A `v1_j1` arm near saturation, added because prediction 3 failed (rule 8)

Stage 1 found the shipped two-worker default **missing O4 at 0.8 of capacity** by 5.2 times the work-lost
tolerance, while meeting it cleanly at 0.3 and 0.6. The registered consequence is a load-aware worker cap. But
stage 1 cannot say what that cap should be, or whether one exists: it ran **one** worker count near saturation.

So the live question is now narrower and sharper than the registration anticipated: **is any worker count safe
at 0.8?** EXP-28 found one worker meeting O4 at 0.6 on this machine shape, in twice the time of two. Whether it
still does at 0.8 is the difference between "cap the workers" and "pgdelta cannot meet O4 near saturation at
all", and those imply very different products.

**Added, before the runs:** a `v1_j1` arm, and `v1_j4` extended to L80 (stage 1 ran it at L60 only). The driver
needs no change — it derives `--jobs N` from any arm named `v1_jN`. Cells: `none`, `full`, `v1`, `v1_j1`,
`v1_j4` at L60 and L80, 3 repeats, on stage 1's machine shape so the comparison is direct. 30 runs, about
1 hour, about 2 USD.

`v1` runs again rather than being carried over from stage 1: W and D are net of **that session's own** `none`
runs, so each session must be self-contained, and it doubles as a replication of stage 1's L80 miss.

**Predictions, before any run:**

1. `v1_j1` meets O4 at L60, as EXP-28 found one worker doing at 0.6.
2. **Deciding: `v1_j1` meets O4 at L80.** *Wrong if it misses* — then no worker count tested meets O4 near
   saturation on this shape, a worker cap is not a sufficient remedy, and the live path needs something else
   (throttling its reads, or backing off on observed latency). That is a materially worse finding than stage 1's
   and would be reported as such.
3. `v1_j4`'s excess over `full` at L80 is larger than at L60: the cost grows with load.
4. `v1` reproduces stage 1's L80 miss (W excess above 0.05 and D excess above 0.03). *Wrong if it meets O4* —
   then stage 1's headline does not replicate within the same shape and nothing downstream stands until that is
   explained.

**What follows:** if 2 holds, the cap's floor is one worker near saturation and the write-up reports the time
that costs (EXP-28: about twice two workers'). If 2 fails, the finding is that pgdelta's live path misses O4
near saturation at every worker count measured, and the remedy is an owner-level design question rather than a
default change.

### 6. Two harness changes after stage 1, and one optimisation rejected (rule 16)

**Declared, and the scorer re-registered:**

- **`lab_exp88.sh`: a `RUNS=` invocation no longer writes into the grid's directory.** The registered
  near-the-line rule's documented mechanism (`RUNS="arm:level" SKIP_SETUP=1`) restarted the run index at 0 and
  wrote into the grid's own output directory, so `run-000` onwards were overwritten. On 2 November that happened:
  the grid's `full`/L30/rep0 and `none`/L30/rep0 were replaced by the two extra `v1_j4` runs. **Nothing was lost
  only because the grid had already been fetched** — luck, not design. A `RUNS=` invocation now gets its own
  session directory, and a run directory that already holds a run makes the driver **stop rather than replace
  it**. The guard is there as well as the fix because the convention had already failed once.
- **`infra/gcp/create.sh`: `MIN_CPU_PLATFORM` and `IMAGE` are read.** Stages 2 and 3 separate a page-cache
  effect by varying RAM alone, so an unpinned host generation would be unattributable. Both are no-ops when
  unset, checked against an unpinned set's dry run.

Neither changes a score: stage 1's data was collected and analysed before both, and neither file scores
anything — one provisions machines, the other orchestrates runs.

**Rejected: a per-level parse cache.** The analysis parses each level's log once per run, so L60's 120 MB log was
inflated and parsed 15 times; stage 1 took over an hour to score. A cache keyed on the path set was written and
then **withdrawn before use**: stage 1's three logs hold **27.3 million transactions**, and holding them as
parsed Python tuples cost about **4.4 GB** — measured at 5.09 GiB resident against the container's 7.6 GiB, on
the edge of being killed. It traded a tolerable time cost for an intolerable memory one. The analysis therefore
stays as registered, and the slow path is accepted: scoring runs detached and blocks nothing.

If it is worth optimising later, the shape is NumPy `int64` arrays (8 bytes a field rather than ~160) and
windowing before materialising — a larger change that would need its own verification that no number moves.

## Rules this experiment is run under

| Rule | What it requires here |
|---|---|
| 8 | Departures from the ledger are listed below, recorded before running |
| 11 | A deciding ratio within 0.05 of its bar gets a second session on a fresh pair, another day |
| 12 | Novelty checked: the `summarize_wal` arm is explicitly **not** novel (EXP-83), and the replication gate is narrowed to what the pilot actually tests |
| 16 | Scorer integrity: the analysis, harness and modules under test are registered before the first run and checked unchanged when results are written |

## Changes from the ledger (rule 8)

- **Three load levels instead of one.** The ledger's T10b asks "how many source workers each load level
  allows"; EXP-28 answered it at one level for lack of time. This widens the level, not the question.
- **A `summarize_wal` control arm,** not in the ledger. It is added because the fast path, built after T10b was
  written, makes the prerequisite a standing cost rather than a detail.
- **Balanced persistent disk for the 50 GB shapes,** where EXP-28 used local NVMe. The reason is realism:
  Omni's Kubernetes operator gives a node persistent disk, and EXP-57 found the checkpoint stall only once the
  disk was capped.
- **A `v1_j4` diagnostic cell at the 0.6 level,** not in the ledger, added so the EXP-28 replication gate
  tests the four-worker cell the cap came from rather than only the two-worker default. Diagnostic, not
  deciding: `v1_j4` is not the shipped default.
- **Stage 3 varies RAM alone,** with storage, cluster, vCPUs and CPU platform held matched to stage 2, and is
  described as a page-cache contrast rather than a disk one.
- **`us-east1` instead of `us-central1`,** where EXP-12 and EXP-28 ran. The reason is quota, not preference:
  `SSD_TOTAL_GB` has 100 GB free in `us-central1` against this experiment's 450 GB peak, and 500 GB free in
  `us-east1`. The consequence is recorded under "Zone, and why it is `us-east1`": verdicts rest on
  within-session ratios and are unaffected; absolute times are not compared with EXP-28's.

## HammerDB, and why pgbench goes first

The sponsor's brief discusses HammerDB TPROC-C, NOPM and latency percentiles, and this experiment does not use
it. The reason is measurement scope, not preference. `benchmarks/hammerdb_experiment.py` records the limitation
in its own words:

> Latency limitation: `JOBTIMING` percentiles cover a virtual user's whole elapsed time, including ramp-up.
> HammerDB records no time dimension for latency, so a window-aligned percentile cannot be derived from its
> instrumentation. Latency comparisons are therefore labelled whole-run and are not matched to the backup
> window the way throughput is.

O4's two numbers and every percentile here are defined **inside** `[t0, t1 + 10 s]`, so a whole-run percentile
cannot answer them. HammerDB's *throughput* is window-matched already, which is why it keeps NOPM below.

pgbench's `-l` log carries a completion timestamp and a latency for every transaction, which is exactly what a
windowed percentile needs. So:

1. **this experiment** — pgbench, windowed percentiles, O4's verdict;
2. **then HammerDB TPROC-C** — NOPM and throughput impact, with percentiles reported only at their correct
   scope, or after the runner learns to window them;
3. **then a real cross-region DR run** — actual RTT and refresh time, with no injected delay.

## Budget

Staged, so each stage can be stopped on its result. **Stage 1 alone answers the core question.**

| Stage | VMs | Wall time | Cost (approximate) |
|---|---|---:|---:|
| 1 pilot, 2 GB | `n2-standard-8` ×2 + `n2-standard-4`, local NVMe | ~1.2 h | **~$2** |
| 2 beyond memory, 18 GB | `n2-highcpu-8` ×2 + `n2-standard-4`, balanced PD | ~2 h | **~$2** |
| 3 page cache, 18 GB in RAM *(conditional)* | `n2-standard-8` ×2 + `n2-standard-4`, balanced PD, platform pinned | ~2 h | **~$2** |
| | | **~5 h** | **~$6** |

Against the first draft's three 50 GB shapes at ~9–12 h and ~$25–35. The saving is almost entirely the data
size: at 18 GB each copy of B takes about a third as long as at 50 GB, and B is re-copied before every `v1`
run.

These are approximate on-demand list prices for `us-east1` (roughly $0.39/h for an `n2-standard-8`, less for an
`n2-highcpu-8`, about $0.19/h for the `n2-standard-4` client, plus disks); they are not queried from the
billing API, and the real posted figure should be read from the console afterwards as the project does
elsewhere. Treat them as the right order of magnitude, not a quotation.

**Quota: nothing to raise.** In `us-east1`, checked October 30, 2026: `N2_CPUS` 200 free (every stage uses 20),
`SSD_TOTAL_GB` 500 free (peak need now about 250 GB at 18 GB clusters, down from 450),
`LOCAL_SSD_TOTAL_GB` unlimited (stage 1's data disk), `INSTANCES` 24 free, `IN_USE_ADDRESSES` 8 free.
