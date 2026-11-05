# EXP-94: Does the sender keeping its stream full remove v1's need for BBR at DR round trips?

Status: **pre-registered November 5, 2026; not yet run.** Registered before any VM exists (rule 16). One fresh
`n2-standard-8` pair, EXP-13's set (`infra/gcp/sets/exp13.sh`), about 105 minutes.

This is EXP-20's and EXP-24's cells again, with one arm added on each congestion control: v1 with the sender
hashing ahead of the file it is sending (`--hash-ahead`, EXP-91). It closes gap **G3** of the
[November 5 survey](../research/2026-11-05-codebase-and-latency-survey.md) and re-establishes **G5**.

## Why this experiment

[EXP-21](EXP-21-tcp-diagnosis.md) traced v1's DR slowdown for scattered changes to cubic's congestion
window, not to buffers or round trips: v1's page stream is **short and bursty** (610 MB), so a burst that loses
segments early sets cubic's threshold low and it grows back slowly, while one `pg_basebackup` stream escapes the
same fate by sending 2.1 GB continuously. EXP-21's "what follows" says pgdelta

> can avoid needing [BBR] by keeping its stream full between files.

[EXP-24](EXP-24-dr-with-bbr.md) then fixed it from the transport side instead, and v1 with BBR holds at every DR
round trip. But [EXP-26](EXP-26-bbr-fairness.md) found a BBR newcomer taking 81–96% of a shared link from cubic
traffic, so **BBR stays opt-in** and needs a host setting on the source.

`--hash-ahead` (EXP-91) is the sender-side fix EXP-21 described, built later for an unrelated reason — removing
a sender stall at 1 Gbit/s — and **never measured at any round trip**. It needs no host setting and takes
nothing from other flows. If it does what EXP-21 predicted, it is worth more than BBR.

Three facts make this a measurement rather than a guess:

- **The mechanism matches.** EXP-91's `--hash-ahead` keeps a planner thread hashing ahead so the sending thread
  always has frames to write. That is precisely "keeping its stream full between files".
- **It is already measured, at no delay.** EXP-91: 0.785 and 0.777 of plain hashing in uniform (full link and
  1 Gbit/s), and 0.99–1.00 in hot. At 0.1 ms there is no slow mode to avoid, so that gain is the *floor* this
  session tests against, not the ceiling.
- **The cells where cubic's slow mode bites are known.** [EXP-23](EXP-23-slow-mode-rate.md): cubic fell into it in
  12 of 15 runs at 20 ms and 0 of 15 at 60 ms tuned; BBR in 0 of 30.

## Design

- **VMs:** one fresh `n2-standard-8` pair in `us-central1-a`, local NVMe RAID 0, ext4, Debian 12, minimum
  platform Intel Cascade Lake — **EXP-13's set, unchanged** (`infra/gcp/sets/exp13.sh`), which is what EXP-20
  and EXP-24 both ran on. The hardware is held identical on purpose: rule 17's cross-session comparison has no
  meaning if the pair shape moves. The resolved image name is recorded here at session start.
- **Conditions** (`RATE:RTT_MS:HOST`): **EXP-20's and EXP-24's own set**, unchanged - `2:0:default`,
  `2:20:default`, `2:60:default`, `2:120:default`, `2:60:tuned`, `2:120:tuned` (the `2:0:tuned` anchor, which
  EXP-20 ran for `v1` and `full` only, is dropped).

  An earlier draft of this registration picked four cells by the size of EXP-24's measured cubic-to-BBR gap and
  kept `2:60:default` as EXP-20's headline cell. Writing the test that checks a stated reason against the data
  (`tests/test_exp94_plan.py`) showed the reason did not hold: **`2:60:default` has almost no gap in EXP-24**
  (uniform `R_fast` 1.645 cubic against 1.629 BBR, a gap of 0.016), because cubic did not fall into its slow
  mode there in that session - while in **EXP-20's** session the same cell and the same arm gave **2.578**. Two
  sessions disagreeing by 57% in one cell is itself the thing rule 17's gate exists for.

  So the selection is dropped rather than re-justified. Running every cell those sessions ran removes the
  selection argument a reviewer would otherwise make, makes prediction 2's comparability gate apply in every
  cell, and keeps the cell where the prior sessions disagree most instead of quietly choosing around it.
- **`parcopy16` at `2:120:default` only**, as EXP-20 ran it, because `B_fast` there is `min(parcopy8,
  parcopy16)` and 8 default windows may not fill the link at 120 ms. Without it `R_fast` in that cell would not
  be the same quantity EXP-20 and EXP-24 reported.
- **Arms** (7; `parcopy16` at one condition):

  | Arm | What it is | Why it is here |
  |---|---|---|
  | `pgdelta_direct_inplace` | v1 as shipped, cubic | rule 17's control against EXP-20 and EXP-24's cubic v1 |
  | `pgdelta_direct_inplace_bbr` | v1 asking for BBR on its data socket | rule 17's control against EXP-24's BBR v1, and the fix to beat |
  | **`pgdelta_direct_inplace_ha`** | **v1, cubic, `--hash-ahead 64G --read-ahead 8`** | **the deciding arm** |
  | **`pgdelta_direct_inplace_bbr_ha`** | **the same with BBR** | whether the two fixes address the same stall |
  | `full` | one `pg_basebackup` stream | the `R_F0` denominator, and the between-session control (rule 17) |
  | `parcopy8` | the 8-stream parallel copy | the `R_fast` denominator, rule 3's yardstick |
  | `parcopy16` | the same over 16 streams, at `2:120:default` only | `B_fast` there, as EXP-20 measured it |

- **`--hash-ahead 64G`** is EXP-91's `hashA` value, kept deliberately: **EXP-92 measured a smaller window and it
  was slower** (2 GiB came out at 1.288 and 1.275 of 64 GiB, because the disks took turns while the planner
  waited for room). At 64 GiB over a 2.1 GB dataset the planner is never the limit, so the arm measures hashing
  ahead rather than a particular budget. `--read-ahead` is pinned at 8 — pgdelta's own default *with*
  `--hash-ahead` — because that default is derived from `--hash-ahead` rather than fixed, and a change to that
  rule must not be able to move what this arm measures.
- **EXP-91's one known cost should not appear here.** On its 8 GB VMs the planner ran 11–16 GB ahead, so the
  source read the pages it sent a second time (its disk 98–107 s against 88 s). These VMs have 32 GB and the
  dataset is 2.1 GB, so the whole of S fits in the page cache. Prediction 5 checks that rather than assuming it.
- **Workloads:** hot and uniform 1% (scale 70, about 2.1 GB) - EXP-20's, unchanged. **3 repeats.**
  **Order seed 940.** That is 6 arms x 6 conditions x 3 repeats, plus `parcopy16`'s 3, per workload: **111 a
  workload, 222 in all.** At EXP-24's measured pace (150 runs in 71 minutes, 28.4 s a run) about **105
  minutes**.
- **Durability and verification:** EXP-13's, unchanged, including check (a)'s 4 MiB limit at every window
  length.
- **A short smoke first** (scale 5, hot, 1 repeat, at `2:0:default` and `2:60:tuned`), then the run. It checks
  three things before the session spends time: the emulation (the round trips and the one-stream rate, as
  EXP-20's smoke did), that both new arms' flags are accepted on both ends, and - by running the two BBR arms -
  that the source really allows BBR, which nothing in the harness would otherwise refuse (prediction 4).
- **Analysis:** two scorers, both committed before the data.
  - `analysis/exp20.py`, the committed and already-registered scorer, run four times:
    `--v1 pgdelta_direct_inplace --tag exp94-cubic`, `--v1 pgdelta_direct_inplace_bbr --tag exp94-bbr`,
    `--v1 pgdelta_direct_inplace_ha --tag exp94-ha`, `--v1 pgdelta_direct_inplace_bbr_ha --tag exp94-bbr-ha`.
    It gives each arm its DR verdict rows against the full copies, unchanged from how EXP-20 and EXP-24 were
    scored.
  - `analysis/exp94.py`, new, for the arm-against-arm readings EXP-20's scorer has no notion of: predictions 1,
    3, 4, 5, 6 and 7, each printed beside its registered bar and its noise band, plus the rule-17 gate against
    EXP-24's committed `full` times. It reuses EXP-23's slow-mode rule and its Clopper-Pearson interval rather
    than restating either. `tests/test_exp94_analysis.py` scores synthetic records through it, so what the
    predictions mean is fixed in code before the data exists.

### Prep: the one host setting (EXP-24's, on the source only)

BBR has to be allowed before an unprivileged sender may ask for it. The host's *default* stays cubic, so
`pg_basebackup`, the parallel copies and every other connection are unaffected; only pgdelta's own data socket
asks for BBR, and each run's statistics record what it got.

```sh
# on the source VM, once, before the session
sudo modprobe tcp_bbr
sudo sysctl -qw net.ipv4.tcp_allowed_congestion_control="reno cubic bbr"
sysctl -n net.ipv4.tcp_allowed_congestion_control net.ipv4.tcp_congestion_control   # record both in the write-up
```

`tcp_allowed_congestion_control` is not one of the sysctls the preflight guard compares with Debian's defaults
(those are `tcp_rmem`, `tcp_wmem`, `rmem_max`, `wmem_max`, `tcp_congestion_control`, `default_qdisc`,
`tcp_slow_start_after_idle`, `tcp_adv_win_scale`), and `tcp_congestion_control` is left at cubic, so the guard
still passes - as it did in EXP-24.

**Run lines** (the shape):

```sh
NOISE=5:4,30:4,120:4   # dest's idle Dirty + Writeback noise, MiB, per window length in s (EXP-13's smoke item 18)
AT=2:0:default,2:20:default,2:60:default,2:120:default,2:60:tuned,2:120:tuned
ARMS=full,parcopy8,pgdelta_direct_inplace,pgdelta_direct_inplace_bbr,pgdelta_direct_inplace_ha,pgdelta_direct_inplace_bbr_ha
COMMON="--manifest-checksums CRC32C --dirty-noise-table $NOISE --control-port 2222 --link-test-seconds 6 --link-test-warmup 3 --tuned-tcp-max-bytes 134217728"
# the smoke first (scale 5, hot, 1 repeat, two conditions), read before the scored runs start. parcopy16 is not
# in it: it runs only at 2:120:default, which the smoke does not visit, and the harness refuses a
# --method-conditions naming a condition outside --conditions (which is how this was caught).
python3 experiments/network/remote.py start exp94-smoke -- --patterns hot --hot-percent 1.0 --scale 5     $COMMON --methods $ARMS --repeats 1 --conditions 2:0:default,2:60:tuned --order-seed 939
# the scored runs: both workloads in one invocation, so one summary.csv holds the session, as EXP-20's did.
# 6 arms x 6 conditions x 3 repeats + parcopy16's 3 = 111 a workload, 222 in all
python3 experiments/network/remote.py start exp94 -- --patterns hot,uniform --fractions 0.01 --hot-percent 1.0     $COMMON --methods $ARMS,parcopy16 --method-conditions parcopy16=2:120:default     --repeats 3 --conditions $AT --order-seed 940 --first-visit-link-streams 16@2:120:default
```


Both workloads run in **one** invocation rather than two, so the session lands in one `summary.csv` and
`analysis/exp20.py` reads it with its own `--sweep`, exactly as it read EXP-20's and EXP-24's. Every arm runs at
every condition and in every repeat, baselines included: `full` is this session's
between-session control (rule 17, prediction 2) and `parcopy8` is the `R_fast` denominator, so neither may be
sampled at a subset of repeats the way EXP-13's `--baseline-repeats` sampled them.
`tests/test_exp94_plan.py` parses this block and checks the plan it produces is those 222 runs, so the
registration's run count is verified rather than asserted.

## Predictions

Registered before the VMs exist. Numbered so the write-up answers each one.

1. **(Deciding.)** For **uniform 1%**, `pgdelta_direct_inplace_ha` is **at most 0.80×**
   `pgdelta_direct_inplace`. 0.80 is EXP-91's own measured ratio for the same flag at no delay (0.785, 0.777);
   a round trip gives it the stall it was predicted to remove as well, so it should do at least as well. A
   ratio in 0.80–0.98 is a real but smaller gain than the mechanism implies, and is reported as such rather
   than as a pass.
   - **Where it has to hold, and why that is not named in advance.** The ratio is reported at **every** cell,
     and the prediction is judged at the cells where **cubic is in the slow mode in this session**, by
     prediction 7's rule applied to this session's own runs. The cells are not listed here because *the two
     prior sessions disagree about which they are*: EXP-20 measured uniform `R_fast` 2.578 at `2:60:default`
     and EXP-24 measured 1.645 in the same cell with the same arm, because cubic fell into its slow mode there
     in one session and not the other. Naming a cell list from either session would therefore be naming the
     wrong cells roughly half the time. Prediction 7's rule is mechanical and registered before the data
     (`page_send_s` above 2.0× the measured link floor), so selecting by it is not a post-hoc choice; selecting
     by *outcome* would be, and is not what this does. A cell where cubic never enters the slow mode is a cell
     where the mechanism predicts nothing to remove, and prediction 6 is what says whether that is true of it.
2. **The comparability gate (rule 17).** This session's `full` is **within 1%** of EXP-24's `full` in each
   shared cell. If it is not, no cross-session claim is made: the session is then read only within itself,
   where all seven arms share one pair and one hour, and EXP-20's and EXP-24's numbers are quoted as history
   rather than compared.
3. **Hot is the negative control.** For **hot**, `_ha` is within **1.03×** of `pgdelta_direct_inplace` at every
   condition. Few pages change, so there is little for a planner to run ahead of and nothing to gain; EXP-91's
   own hot arms came out at 0.994–1.001. A gain in hot would mean the arm is measuring something other than the
   stall, and would call the deciding reading into question.
4. **Whether the two fixes are the same fix.** For **uniform 1%**, `_bbr_ha` is within **1.05×** of `_bbr` at
   every condition. If BBR already keeps the window open, hashing ahead has little left to add. `_bbr_ha`
   clearly faster than `_bbr` would mean the two address *different* stalls, and would be reported as a new
   finding rather than folded into prediction 1.
   - **This prediction is gated, because a BBR arm can silently run cubic.** pgdelta asks for BBR with
     `setsockopt(TCP_CONGESTION)` and **does not fail if the host refuses**: an unprivileged process may
     choose only what `net.ipv4.tcp_allowed_congestion_control` lists, and `transport.set_congestion_control`
     records `{'effective': 'cubic', 'error': ...}` while the transfer goes on. `run_exp04.py` checks nothing
     about it. So if the prep step above is forgotten or does not stick, both BBR arms run cubic, **every run
     still passes**, and this prediction becomes prediction 1 again under another name. `analysis/exp94.py`
     therefore reads every run's statistics first and reports prediction 4 as **"not readable"** unless every
     run that asked for BBR got it with no error. Absent statistics count as unknown, not as success. The
     smoke run exercises both BBR arms, so a missing host setting shows before the scored session starts.
5. **EXP-91's cost does not reappear at this size.** In every `_ha` cell the source's disk **read bytes over
   the timed phases** (`measure.phase_disk_bytes(record, 'source', 'read_bytes')`, from each run's `run.json`;
   `run_exp04.py` surfaces only dest's as a CSV column) are within **1.05×** the matching
   `pgdelta_direct_inplace` cell's. EXP-91's cost was the source reading the pages it sends a second time, on
   8 GB VMs with the planner 11-16 GB ahead; here the whole of S is 2.1 GB on a 32 GB VM, so it should stay in
   the page cache and the extra reads should not appear at all.
6. **The mechanism, measured directly, not inferred.** `--hash-ahead` is supposed to keep the stream full
   between files, and the harness already times exactly that: `leaf_wait_idle_s` is the time the sender was
   blocked waiting for leaf digests **while its socket had nothing unsent and nothing in flight**
   (`pgdelta/waits.py` splits each wait into busy and idle parts from `SIOCOUTQNSD` and `SIOCOUTQ`). An idle
   leaf wait *is* a stream that is not full. Prediction: for **uniform 1%**, `_ha`'s median `leaf_wait_idle_s`
   is at most **0.25×** `pgdelta_direct_inplace`'s at every condition.
   - This is independent of prediction 1 on purpose. If 6 holds and 1 fails, the stall was real and removing it
     did not help, which is what EXP-90 and EXP-91 found at 0.1 ms when another resource bound instead. If 6
     fails, the arm is not doing what its flag claims, and prediction 1 says nothing either way.
7. **The slow mode, counted by EXP-23's own rule.** A run is in the slow mode when `page_send_s` exceeds
   **2.0×** the time the condition's measured one-stream link rate needs for the bytes it sent - EXP-23's
   definition, reused unchanged so the counts are comparable with EXP-23's and EXP-24's. Prediction:
   `pgdelta_direct_inplace` is in the slow mode in **at least 2 of its 3** uniform 1% runs at `2:20:default`
   (EXP-23 found 12 of 15 there; EXP-24's prediction 4 was the same and held), and `_ha` in **0 of 3**, as BBR
   was in 0 of 57 runs across three sessions. Reported with an exact (Clopper-Pearson) 95% interval, as EXP-23
   reported it; with 3 runs a cell the interval is wide, and that is stated rather than hidden.

**What follows:**

- **If 1 and 2 hold:** `--hash-ahead` is the DR fix for scattered changes, and it needs no host setting and is
  fair to other flows, which BBR is not (EXP-26). That makes it a **candidate default** — and a default change
  owes a held-out session first (rule 15, H9–H12), so the write-up proposes one rather than changing anything.
  D29 ("hash 4 files ahead") gets its latency reading, and the idea archive's `--hash-ahead` row gains its DR
  conditions.
- **If 1 fails and 4 holds:** keeping the stream full is not what rescues scattered changes at a round trip, and
  EXP-24's transport fix stands alone. EXP-21's "what follows" is then recorded as **not borne out**, which is
  worth as much as a win: it is the project's standing explanation of the DR slowdown.
- **If 2 fails:** the session still decides prediction 1 within itself (all four v1 arms ran in the same
  repeats), but G5 stays open and EXP-20's and EXP-24's verdicts remain attached to the October 25 engine.

## Rules

| Rule | Answer |
|---|---|
| **11** | One session. A deciding ratio within 20% of its threshold is reported as provisional, as in EXP-20 and EXP-24. |
| **13** | **Stage 3 (the VMs).** Stage 1 (tests) is green. **Stage 2 is skipped, and why is stated rather than hidden:** the local scale model has no round trip, and a round trip is the entire subject. So this session logs **no new local/VM pair** in [proxy-fidelity.csv](../research/proxy-fidelity.csv). What stands in for stage 2 is EXP-91's already-measured VM gain for the same flag at no delay (0.785), which prediction 1 uses as its bar. |
| **14** | Idea archive read. The `--hash-ahead` row (opt-in, EXP-91; EXP-92 settled 64 GiB) gains DR conditions; the **"fast path at DR round trips"** row stays **open** — it needs `--source-kind live`, which `run_exp04.py` does not have, and is EXP-95. The structurally different idea beside this tuning is that one: labels instead of hashing, at latency. |
| **15** | **No held-out session is owed by this experiment,** because it changes no default — both new arms are opt-in. A held-out session (H9–H12) *is* owed before `--hash-ahead` becomes a default, which is what prediction 1 holding would propose. |
| **16** | Registered below. **Correctness gate:** EXP-13's, unchanged — every copy compared with the source, every run's `pg_verifybackup`, and pgdelta's reported switches checked against what each arm pins. **Negative control:** hot (prediction 3), a workload where the mechanism predicts no gain. |
| **17** | **Noise, as of EXP-92:** two runs of a cell differ by up to 1.92% at the 95th percentile for runs of 10 s or more and 7.11% under 10 s; between sessions `pg_basebackup` in the same cell moves about 0.3%. Prediction 1's bar (0.80) and prediction 3's (1.03) both sit outside the within-session spread for the uniform cells, which run 16–38 s. **Hot runs are 3–6 s, so they fall in the 7.11% band:** prediction 3's 1.03 is inside it, and a hot difference under 7.11% is therefore reported as a tie, never as a gain. Prediction 2's 1% gate is the cross-session control. Refreshed by `analysis/noise_floor.py` with this session. |

## Limitations

An emulated WAN in one zone (EXP-15 is the real-link check). One session, one VM type, 2.1 GB in page cache —
which is exactly why prediction 5 exists, and also why this says nothing about the larger datasets where
EXP-91 found its cost. `--hash-ahead` is measured only on v1's hashing path with a cleanly stopped source; the
fast path cannot run here at all, since it requires `--source-kind live` (EXP-95).

## Registered files

Rule 16 records the hash of every file that **scores, runs or is under test** at the registration commit.
`analysis/integrity.py register EXP-94 -- <these>`:

**The scorers** — `analysis/exp20.py` (the verdict rows, unchanged from EXP-20 and EXP-24),
`analysis/exp94.py` (the predictions above), `analysis/exp23.py` (its slow-mode rule and exact interval, which
prediction 7 uses), `analysis/noise_floor.py` (rule 17's refresh), `analysis/plotting.py`.

**The harness that runs it** — `experiments/network/run_exp04.py`, `experiments/network/measure.py`
(the two new arms live here), `experiments/network/agent.py`, `experiments/network/remote.py`,
`experiments/network/parcopy.py`, `experiments/network/source_cluster.py`,
`experiments/change_patterns/run_study.py`.

**The infrastructure** — `infra/gcp/config.sh`, `infra/gcp/create.sh`, `infra/gcp/teardown.sh`,
`infra/gcp/wan.sh`, `infra/gcp/throttle.sh`, `infra/gcp/vmset.sh`, `infra/gcp/startup.sh`,
`infra/gcp/sets/dr94.sh` **and** `infra/gcp/sets/exp13.sh`, which it sources - the hardware claim rests on both
files, so both are registered.

**The engine under test** — `pgdelta/cli.py`, `pgdelta/sender.py`, `pgdelta/receiver.py`,
`pgdelta/transport.py`, `pgdelta/waits.py` (prediction 6 reads what it measures), `pgdelta/inplace.py`,
`pgdelta/hashing.py`, `pgdelta/datadir.py`, `pgdelta/fastpath.py`.

**The tests that fix what the plan and the predictions mean** — `tests/test_exp94_plan.py`,
`tests/test_exp94_analysis.py`. They are registered because they are the only thing holding the bars in
`analysis/exp94.py` to the numbers written above: loosening a bar without the document disagreeing should not
be possible, and a changed test hash is how that would show.

## Results

Not yet run.
