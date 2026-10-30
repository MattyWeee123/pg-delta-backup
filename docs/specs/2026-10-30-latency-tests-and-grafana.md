# The latency tests we are going to run, and how each is visualised

Status: **plan, October 30, 2026.** Revised the same day to put the application-latency test first.

It changes no registered design. Where this document and an experiment's own write-up disagree, the write-up
wins, and a departure needs a rule-8 note there, not here. Its purpose is to say in one place what we are about
to run, in what order, what it costs, and what the dashboards will and will not show.

- **First:** [EXP-66](../experiments/EXP-66-slowdown-by-load-and-size.md) — application latency under load.
  Pre-registered, staged, **~$6 and ~5 h**, nothing unbuilt blocking stage 1.
- **Second:** [EXP-13](../experiments/EXP-13-latency-transport.md) — network latency at DR round trips.
  Pre-registered and reviewed twice, **~$18**, needs seven harness items first (three now built).

---

## 1. Two different things called "latency"

The project measures two unrelated quantities. They need different loads, different arms, and different
dashboards, and conflating them is the easiest way to draw a wrong conclusion.

| | **Application latency — EXP-66** | **Network latency — EXP-13** |
|---|---|---|
| The question | While a backup runs, how much slower does the database get for its users? | Over a WAN, how long does the refresh itself take, and what limits it? |
| The metric | p50/p95/p99 of transaction response time; work the load lost; throughput lost; failed transactions | Wall-clock refresh time; wire bytes; achieved throughput; protocol wait time |
| Who is loaded | a `pgbench` client drives the **source**, from its own VM | **nobody** — v1 reads a *stopped* source, `full` an *idle* one |
| Ledger tests | T10a, T10b | T12, T12r |
| Added delay | **none** | 20, 60, 120 ms, emulated |
| Dashboard | **the live one** (`pgdelta-live-slowdown`) — this is its purpose | run-level panels; the live one sits empty |

That last row is the practical consequence: the live dashboard — p50/p95/p99 with the backup window shaded — is
the **primary instrument for EXP-66** and is **empty during EXP-13**, because EXP-13 runs no load at all. That
is correct behaviour in both cases.

---

## 2. The test we are running first: EXP-66

### Why it goes first

EXP-28 already answered O4 — "a refresh must cost the source's load no more than one `pg_basebackup` stream" —
and found the answer turns on the source worker count: at 4 workers on 8 vCPUs, p95 went from 6.4 ms to
**74.3 ms** and p99 to 114.6 ms, which failed the deciding prediction and moved the shipped default to at most
2 workers. But it answered it **once**: one load level (0.6 of capacity), one cluster of about 1 GB that fits
in RAM, local NVMe, hashing path only.

Three things are therefore unmeasured, and all three are in RESULTS' own list of open items:

1. **Other load levels.** A source at a third of capacity has spare CPU; one at four fifths does not.
2. **Beyond memory.** Past RAM, v1's verification re-read stops coming from the page cache and becomes a disk
   pass (EXP-48). Every slowdown number so far is from data that fit in RAM.
3. **The WAL-summary fast path.** EXP-64 measured its *speed* (18 GB of local changes in 1.98 s). Its cost to a
   live load has never been measured — the fast path has never been run against a loaded source at all.

And one thing nobody has priced: the fast path needs `summarize_wal = on`, which an operator leaves on
continuously. EXP-12 and EXP-28 set it for every arm alike, so it cancels in their comparisons and has never
been isolated. EXP-36 says as much.

### Staged, cheapest decisive stage first

Each stage gates the next. If stage 1 cannot reproduce EXP-28's cell, nothing else runs.

| Stage | Source and dest | RAM | Data disk | Cluster | Runs | Wall | Cost | Answers |
|---|---|---|---|---|---:|---:|---:|---|
| **1 pilot** | `n2-standard-8` ×2 | 32 GB | local NVMe | ~2 GB | 45 | ~1.2 h | ~$2 | **Does O4 hold at 0.3 and 0.8 of capacity?** What `summarize_wal` costs; whether the fast path's window is measurable at 2 GB |
| **2 beyond memory** | `n2-highcpu-8` ×2 | **8 GB** | balanced PD | **18 GB** | 16 | ~2 h | ~$2 | **Does O4 survive beyond memory, and what does the fast path cost the load?** |
| **3 mechanism** *(conditional)* | `n2-standard-8` ×2 | 32 GB | balanced PD | 18 GB | 16 | ~2 h | ~$2 | *Only if stage 2 differs:* was it the disk? |

A `n2-standard-4` client VM drives the load in every stage. **77 runs, ~5 h, ~$6 in total**, in `us-east1`.

**Why 18 GB on 8 GB of RAM rather than 50 GB on 32 GB.** Beyond memory is a *ratio*, not a size: what matters
is that B stops fitting in page cache. 18 GB on an 8 GB machine is beyond memory by 2.25× and is the exact
shape EXP-48 modelled and EXP-64 ran, so stage 2's numbers sit beside theirs instead of opening a new size
nobody has measured. It also copies 2.8× less data, and B is re-copied before every `v1` run — which was the
dominant cost of the first draft (~$25–35 for three 50 GB shapes).

### Arms and levels

| Arm | What it is |
|---|---|
| `none` | load only, `summarize_wal = on` — the null W and D are net of |
| `none_nosum` | load only, `summarize_wal = **off**` — prices the fast path's prerequisite (stage 1 only) |
| `full` | `pg_basebackup -Fp -X stream -c fast`, one stream — **O4's bar** |
| `v1` | pgdelta live, in place, hashing path, at the shipped default (2 source workers on 8 vCPUs) |
| `v1_fast` | the same plus `--fast-path summaries` |

Levels **0.3, 0.6, 0.8 × C**, where C is measured per session (the median of three 30-second closed-loop runs
at 128 clients after a 30-second warm-up). EXP-12 recorded a trial whose capacity was 10.9% below its own
smoke run's, so C is a per-session quantity and never assumed.

### What decides, and what merely reports

**Deciding — O4's two numbers**, from the client's log over `[t0, t1 + 10 s]` in 1-second buckets, mapped onto
dest's clock by chrony's offsets:

- worst drop **D** = max(1 − X_b/S_b)
- work lost **W** = Σ max(0, S_b − X_b)/R

> Meets O4 when W_net(arm) − W_net(full) ≤ δ_W = max(0.05 s, 0.15 × W_net(full)) **and**
> D_net(arm) − D_net(full) ≤ δ_D = 0.03.

**Reported — p50, p95, p99** and their percentage increase against the matched `none` cell, TPS loss, failed
transactions, backup duration, source CPU, disk I/O.

**Why the percentiles do not decide.** A ratio of two p95s compounds the tail noise of both, and at 2–3 repeats
its interval is wide. W and D are an integral and a maximum over 1-second buckets — far more stable, and what
O4 is already written in. A new latency threshold would create a second, weaker bar beside the owner's.

---

## 3. How EXP-66 is visualised

EXP-66 has a load, a client VM and a recorded backup window, which is exactly what the live dashboard was built
for. **This is the figure originally sketched for this work, realised against real data.**

### 3.1 Live, during a run — `pgdelta-live-slowdown`

| Panel | Metric | What it shows |
|---|---|---|
| Transaction latency | `pgdelta_txn_latency_ms{quantile}` | p50/p95/p99 over time, exact quantiles over a 5 s sliding window |
| Backup window | `pgdelta_backup_active` | a state timeline on the same axis, from the driver's recorded t0/t1 — the window is **read, not inferred from a spike** |
| Throughput | `pgdelta_txn_rate_per_second` | the other half of O4: a backup can leave latency alone and still cost work |
| Schedule lag | `pgdelta_txn_schedule_lag_ms{quantile}` | open-loop queueing. Lag rising while latency stays flat means the source stopped keeping up with the offered rate — the shape a stalled checkpoint makes |
| Failed transactions | `pgdelta_txn_failed_total` | the "errors" column; red on any non-zero |

Two properties that matter more than the panels:

- **The exporter runs on the client VM, reading pgbench's own `-l` log from tmpfs.** It costs the source
  nothing. This is not a convenience: O4's bar is 0.05 s of the load's work and a worst drop of 0.03, small
  enough that an agent's own CPU could spend it, and EXP-28 moved the client off the source for exactly that
  reason. **Nothing in this stack may ever scrape the source.**
- **The driver writes the window.** `lab_exp66.sh` writes `{"t0_utc": …}` when the timed arm starts and adds
  `t1_utc` when it ends, so the shading is the real window.

One caveat, and it is the same one the analysis handles: t0/t1 are on **dest's** clock while the log is on the
**client's**. `analysis/exp66.py` maps between them with chrony's offsets; a dashboard showing both without
that mapping is off by the skew. Fine for watching a run, not for a number.

### 3.2 After the runs — the evidence dashboard

`backfill.py` turns the committed per-cell CSV into `pgdelta_evidence_*` series, as it already does for EXP-12,
EXP-28 and EXP-57. For EXP-66 it gains a `load_level` and a `stage` label, so the panels become:

| Panel | What it answers |
|---|---|
| p95 and p99 by arm, **faceted by load level** | does the worker-count cliff reappear at 0.8 of capacity? |
| W and D by arm, with δ_W and δ_D drawn as thresholds | the O4 verdict, with its bar visible rather than asserted |
| `v1` vs `v1_fast` at stage 2 | what the fast path costs the load once its window is long enough to measure |
| `none` vs `none_nosum` | the standing price of `summarize_wal` |
| stage 2 vs stage 3, same size | the disk effect, isolated by RAM alone |
| refresh time beside the two O4 numbers | the trade: speed bought at what cost to the load |

**A dashboard is never where a verdict is read.** It cannot show the flags that qualify one — fragile, near the
line, not judged. `analysis/exp66.py` owns verdicts; the panels are for seeing shape and spotting nonsense.

### 3.3 What has to be built for this

Small, and none of it needs a VM:

1. `experiments/network/lab_exp66.sh` — the driver, including the window file.
2. `analysis/exp66.py` — the analysis and its figure, **committed before any data**.
3. `backfill.py` — `load_level` and `stage` labels, and EXP-66's cell CSV.
4. The evidence dashboard — faceting by load level, and the δ threshold lines.
5. Failed transactions wired into the analysis: `analysis/exp28.py`'s "failed" counts failed *runs*, not failed
   transactions, so this is genuinely new.

---

## 4. The test after it: EXP-13

Everything below is unchanged from the October 30 plan; it moves behind EXP-66, it is not reduced.

### What it asks

Every DR number the project has was measured at **0.1 ms** round-trip time. A real DR path is 20–120 ms, and
two things could break there:

1. **pgdelta's protocol waits on round trips.** The sender hashes a file, asks for the digests of its changed
   nodes, and waits — hashing at most **4 files ahead** (`DEFAULT_WINDOW = 4`). 19 files with a basis changed in
   EXP-11's runs. At 0.1 ms that is free; at 120 ms it may not be.
2. **One TCP stream carries at most its window per round trip** — about 3 MiB on these VMs, so roughly
   0.42 Gbit/s at 60 ms and 0.21 at 120 ms. v1 uses one stream (D36); the bound uses 8.

### What it decides

| Decision | Today | What EXP-13 does |
|---|---|---|
| **D29** the request window | **D** — "never varied; latency never added" | `v1_w1` / `v1_w16` vary it with latency added. Registered consequence: *if a window of 16 saves ≥0.3 s in one workload and costs ≤0.1 s in the other, the default becomes 16* |
| **D36** one TCP stream | **C** | against 8 and 16 streams, default and tuned buffers |
| **D22** high-churn fallback | untested | `v1_hc` at 10% changes |
| **D37, D14** under O6 | — | `v1_tls` through a stunnel pair |
| **D03, D78** crossovers | hold only at 0.1 ms | re-read at 20–120 ms |

### Shape, cost and constraints

Two sessions on **different UTC days**, ≥12 h apart, fresh VMs: ~915 runs and ~7.9 h of pair wall time each,
2 × `n2-standard-8`, about **$18 for both**.

Registered and not negotiable without a rule-8 note: zone pinned `us-central1-a`; Debian 12 with one exact
image resolved once and recorded; **minimum CPU platform Cascade Lake**; **four local NVMe SSDs in RAID 0** (the
runs refuse to start otherwise); VMs created **13:00–15:00 UTC**; order seeds fixed per session.

**A capacity risk to expect.** `infra/gcp/config.sh` records that `us-central1-a`, `-c` and `-f` had no
`n2-standard-8` with local SSDs on 2026-10-22 — a stockout, which is why the project's global default zone is
`us-central1-b`. EXP-13 pins exactly that combination. Capacity cannot be probed in advance. The registered
remedy is a recorded fallback zone that labels cross-session readings "platform changed"; the precedent is
EXP-64, which moved to `us-west1` under rule 8. The cost of taking it: `us-central1-a` is where EXP-09, EXP-11
and EXP-12 ran, so moving zones weakens every comparison with them — which is the reason to try the pinned zone
first rather than pre-emptively switching.

### How EXP-13 is visualised

Run-level, from its ~915 `run.json` records — **not** the live latency dashboard, which has no load to read.

| Panel | What it answers |
|---|---|
| Refresh time by round trip | does v1 still beat the copies at 20/60/120 ms, and where does it stop? |
| **The window sweep** — `v1` vs `v1_w1` vs `v1_w16` | **the panel D29 turns on** |
| Protocol wait, **busy vs idle** | idle rising with RTT *is* the protocol paying for distance |
| Achieved vs offered rate | did the link deliver what it was shaped for? Guards against believing an emulator |
| `ss` `rwnd_limited` / `sndbuf_limited` | a claim that a stream was window-limited must be **shown**, not inferred — an EXP-11 lesson |
| Ratios with the 0.90/1.00 bands as thresholds | how close each verdict is to its bar |

Live during a session, the useful thing is health, not findings: runs completed against planned, the current
condition, **shaping achieved against requested** (the single best early-abort signal, since a mis-shaped link
invalidates every run after it), tbf drops and retransmissions as a trace, and failures at zero.

Two sessions are **two series, never averaged** — their shift is itself a measured quantity (K10).

---

## 5. EXP-13's seven items

Audited against the tree on October 30. Most of its harness was already built: `T12_UPPER_S`,
`T12_SIDE_EXTRA_S`, `--zero-delay-link-rates`, `analysis/exp13.py`, and the per-pair infrastructure (the
write-up's "`build/infra-pairs` isn't merged" is **stale** — it is on main).

| # | Item | Files | Blocks | State |
|---|---|---|---|---|
| 1 | `--wait-poll off` | `pgdelta/{cli,sender,waits}.py` | reading A20 | **built** |
| 2 | `pgdelta_direct_inplace_s1` engine | `measure.py` | the `v1_s1` arm | in progress |
| 3 | `--zero-delay-link-streams` | `run_exp04.py` | A9, A12 at 5 Gbit/s | in progress |
| 4 | `--force-buffers` / `SO_SNDBUFFORCE` | `agent.py` | A22 | in progress |
| 5 | `--method-repeats METHOD@CONDITIONS=REPEATS` | `run_exp04.py`, `measure.py` | the run plan | in progress |
| 6 | `cos` in `HOST_TCP_SETTINGS` | `measure.py`, `run_exp04.py` | a third host setting | **deferred** — conditional on A21's COS read |
| 7 | `exp13` set file + `min-cpu-platform` | `infra/gcp/create.sh`, `sets/exp13.sh` | the pinned platform | **built** |

**Correction to an earlier draft of this list:** it claimed `create.sh --image` was already built. It was not —
`create.sh` only passed `--image-family`, and `HEAD` had zero bare `--image` occurrences. The audit grep was
`--image\b`, and `\b` matches the boundary before the hyphen in `--image-family`, so the pattern reported a
false positive. An exact-image option was built with item 7, as EXP-13's own wording requires; without it the
set file's pinned `IMAGE` would have been dead config.

**The shortcut, still available:** the `v1_w1` / `v1_w16` arms need none of these — `--window` already exists.
A lean window-only subset can close D29 before the rest is finished, under rule 8 with the reason recorded
(precedent: O9, which dropped T3's arms (f) and (g) with a recorded reason).

---

## 6. Sequence

1. **Build EXP-66's harness** — the driver, `analysis/exp66.py`, the backfill labels, the faceted dashboard.
   No VM.
2. **EXP-66 stage 1** (~$2, ~1.2 h, `us-east1`). Smoke first; it gates the grid. **Stop and read the result.**
3. **EXP-66 stage 2** if stage 1 holds; **stage 3** only if stage 2 finds a difference to explain.
4. **Finish EXP-13's items 2–5**; item 6 waits on the COS read.
5. **EXP-13 session 1** — created 13:00–15:00 UTC in `us-central1-a`, with the fallback zone ready.
6. **EXP-13 session 2** — a different UTC day, ≥12 h later, fresh VMs, the second seeds.
7. Read each with its committed analysis script. A route is taken only where **both sessions agree, unflagged**.

Then, in the order originally proposed: **HammerDB TPROC-C** for NOPM and throughput impact — with latency
percentiles only at their correct scope, since `hammerdb_experiment.py` records that `JOBTIMING` percentiles
cover a virtual user's whole elapsed time and cannot be windowed — and then **a real cross-region DR run** with
the actual RTT recorded and no injected delay.

## 7. What neither test settles

- **EXP-66:** one load shape (the bank scripts); B is minutes old, not hours; no added latency; 18 GB is not TB
  scale; the fast path needs `summarize_wal` on, which Omni ships off.
- **EXP-13:** an emulated WAN (EXP-15 checks one real path); a stopped source throughout (a live source is
  T8's); 2.1 GB in RAM on local NVMe in one zone.
- **Both:** no ratio is projected past the sizes measured (rule 10).
