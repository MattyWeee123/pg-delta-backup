# EXP-15: Do EXP-13's emulated DR verdicts hold on a real cross-region link? (T12x)

Status: **pre-registered October 25, 2026, as a proposal; reviewed October 25 and revised (see [After the review](#after-the-review-october-25-2026)); not built, not run.** This is test T12x, proposed by the [rule 11 audit](../FIRMNESS.md#41-the-tests) and allowed by the owner's K3 ([ledger](../DECISIONS.md#decisions-of-october-24-2026-night-after-the-rule-11-audit)). It checks [EXP-13](EXP-13-latency-transport.md)'s emulated WAN on a real path. EXP-13's [revision under rule 11](EXP-13-latency-transport.md#revisions-under-rule-11-october-25-2026-before-running) makes this check a condition for any of its DR rows to pick a route. Like EXP-11 to EXP-13, this design gets an independent review, and a check of the revision, before its harness is built; the review is done, and its changes are made in place, marked "(after the review)". Everything here is written before running. Two things can only be fixed by measuring the path: the region, and its round trip r_R. A probe fixes them before EXP-13's session 1 (below), and they are recorded here, dated, before any arm runs.

## Question

EXP-13 measures DR's round trips only through netem, between two VMs in one zone: a constant delay, one token-bucket shaper with a queue of max(50 ms, r), no jitter, loss or reordering, and CUBIC. Under rule 11 an emulation is a proxy: it fails the test "measured on the relevant platform, not by proxy" ([FIRMNESS.md](../FIRMNESS.md), items X13-wan and X13-rows). So no DR row of EXP-13 can pick a route until a real path agrees.

Here, v1's path and the full copies run across a real link between two Google Cloud regions, and, at the same time, across EXP-13's emulation of that same link between two VMs in one zone. The questions:
1. **Does the emulation reproduce a real path at that path's round trip?** On the same arms, workloads, link rate and host settings, do the two give the same ratios and the same verdict row? The real path keeps whatever loss and jitter it has; the emulation has none.
2. **Which of EXP-13's rows does a real path confirm, and which does it overturn?** Only at the real path's own round trip, or close to it (after the review).
3. **What does a real cross-region path carry, and does v1 beat the fastest copy at that path's own rate, and at 5 Gbit/s?** (DR-rate: no DR link rate is documented, and "DR first" assumes DR links run below v1's crossovers.)
4. **Does pgdelta's request window matter on a real path?** (D29)
5. **Is one TCP stream window-limited on a real path as it is on the emulator?** (D36)

**What would change the plan** (the full rules are under [Decision rules](#decision-rules)). Under rule 11, each "Then" follows only from a firm outcome (see [Rule 11](#rule-11-which-outcomes-may-pick-a-single-route)):

| If | Then |
|---|---|
| The emulation reproduces the real path at r_R, and the real path gives the row EXP-13 gave at r_R (its 2:r_R cells), or at 60 ms when r_R is within 12 ms of 60 (revised after the review) | That EXP-13 row is confirmed on a real path, and may pick its route if EXP-13's two sessions also agree, unflagged. R's own row at r_R, in both sessions, is the real-path DR row |
| The emulation doesn't reproduce the real path, and the real path is worse for v1 | No DR claim for v1 rests on EXP-13's emulated rows. The emulator gets what the real path showed (loss, jitter, queueing), and EXP-13's deciding cells are proposed for a rerun with it |
| At the path's own rate, a range-parallel copier beats v1 **(expected)** | "DR first" holds only on links slower than this path. The positioning states the link rate with it, and names its yardstick: against one `pg_basebackup` stream v1 is still expected to win on default hosts |
| At the path's own rate, v1 beats the fastest copy measured | "DR first" holds on this real path at its own rate, on these VMs |

## Setup

### The region pair, and the check that fixes it before running

- **Source:** `us-central1-a`, EXP-13's zone (pinned in its set file; after the review).
- **Dest:** the region whose round trip from `us-central1` is between 30 and 60 ms and nearest 60 ms. 60 ms is the ledger's DR round trip. K3 allowed one session outside `us-central1`, and no North American region is expected near 120 ms.
- **Candidates:** `us-west2` (Los Angeles), `us-west1` (Oregon), `us-west4` (Las Vegas) and `us-east1` (South Carolina). Their round trips from `us-central1` are not documented in this repository, and none is assumed here. Probing them puts small VMs in four regions outside `us-central1`, which is the owner's call (Q1; after the review).
- **The probe, in wave 0, before EXP-13's session 1** (revised after the review), after the read-first checks below pass for a candidate:
  - one small VM (`e2-small`, labelled `purpose=pg-delta-benchmark`) in the source's zone, and one in a zone of each candidate region;
  - from the `us-central1` VM, as root, 200 pings 0.05 s apart to each candidate's internal address, with the path idle; their minimum, median, 99th percentile and loss;
  - 20 TCP connects to each candidate's port 22, which checks that the internal firewall rule reaches it;
  - then every probe VM is deleted. About 15 minutes; 5 VMs, at most 10 vCPUs at once; about 1.25 instance-hours of `e2-small`.
  - It runs before EXP-13's session 1 so that EXP-13 can run its cells at r_R (its E26). If the owner hasn't answered Q1 by then, the probe runs before EXP-15's session 1 instead, EXP-13 runs without those cells, and X4 confirms an EXP-13 row only within 0.2 r of r_R.
- **The rule that picks the region:** among candidates whose median is in [30, 60] ms and whose loss is at most 1% (2 of 200 pings; revised after the review), the one nearest 60 ms. A tie within 2 ms goes to the smaller p99 less minimum. If none is in range, EXP-15 doesn't run; that is recorded here, and the owner is asked (another region pair would need K3 again). Each candidate's loss is recorded.
- **Recorded here, dated, before EXP-13's session 1 (or before EXP-15's, by the rule above):** the region, the zones, the probe's figures, and the predictions recomputed at its median (see [Predictions](#predictions)).
- **On each session day,** R's set-up measures r_R again: 200 pings on internal addresses. A median outside [30, 60] ms, loss above 1% (3 or more of 200), or a peer that doesn't answer, and the session doesn't run: an infrastructure fault, the VMs deleted, another day. Any lower loss is recorded and the session runs (revised after the review). A median more than 3 ms from the probe's flags the session "path moved"; the session's own r_R is what E emulates.

### Read first (read-only, before the probe)

Every check is a `gcloud ... describe` or `list`. Nothing is raised or changed: under K2 (the owner, October 25) the project works within its quota for now, and the owner can raise it from the console (revised after the review). IAM, firewall, network, billing and org-policy changes stay off limits.
- **The project's CPU quota across all regions:** 32 vCPUs. A session uses all 32 (four n2-standard-8), so nothing else may run during one, until the owner raises the quota.
- **Each region's quotas:** in `us-central1`, CPUs and N2 CPUs for three VMs (24), local SSD for three VMs (4,500 GB), and three of its 8 external addresses. In the candidate, CPUs and N2 CPUs for one VM (8), local SSD for one (1,500 GB), and one external address (the Mac reaches each VM over its external address, as today).
- **The per-project inter-region traffic quota** named on the VPC quotas page ([Omni notes](../research/notes/alloydb-omni.md)): its name and value for the pair, recorded. Below 5 Gbit/s, the `5g` conditions can't run, and EXP-13's A18 stays provisional; below 2 Gbit/s, EXP-15 doesn't run. Below what the path carries, the own-rate cells measure the quota, flagged "quota-limited" (X6).
- **The default network:** a subnet in the candidate region; its internal firewall rule's source ranges cover that subnet; its SSH rule admits the Mac there. Any failure drops the candidate.
- **The price of inter-region egress:** about 20 USD per TiB between North American regions at list price ([Omni notes](../research/notes/alloydb-omni.md), fetched October 23), re-read and recorded with its date.

### Machines

- **Pair R (real):** the source in `us-central1-a`, the dest in the chosen region.
- **Pair E (emulated):** source and dest both in `us-central1-a`.
- Four `n2-standard-8` VMs, each with four local NVMe SSDs in RAID 0 at `/data`, ext4, Debian 12, PostgreSQL 17 from PGDG, as EXP-13. They are labelled `purpose=pg-delta-benchmark`, created fresh for each session, and deleted after their results are fetched. 32 vCPUs: the whole quota.
- **One platform for all four, in both sessions** (after the review). Each pair's set file pins its zones, the exact Debian 12 image EXP-13's sessions used (images are global, so the same name serves every region), and the CPU platform they ran on (EXP-11's Cascade Lake), set as each VM's minimum. Each VM's platform and image are read from the metadata server and recorded. A VM that reports another platform is deleted and created again once, before provisioning. If the four still differ, X8's "pair-sensitive" flag applies to every reading that compares R with E.
- The same deployed commit on all four, with EXP-13's harness and the additions under [What it needs built](#what-it-needs-built-in-the-harness).
- Data crosses internal addresses only. If the chosen region's zones have no `n2-standard-8` with four local SSDs, the next candidate whose probe passed is used, and recorded.

### Conditions

| Condition | On R | On E | Why |
|---|---|---|---|
| `2g:default`, `2g:tuned` | `wan.sh` at 2 Gbit/s on both VMs, with a one-way delay of 0.05 ms and the tbf's queue set to max(50 ms, r_R) | `wan.sh` at 2 Gbit/s, one-way delay (r_R − r0_E)/2 + 0.05 ms, queue max(50 ms, r_R) | EXP-13's rows are judged at 2 Gbit/s. The same shaper and queue on both, so the pairs differ only in the path |
| `5g:default`, `5g:tuned` (added after the review) | as `2g`, at 5 Gbit/s | as `2g`, at 5 Gbit/s | EXP-13's A18 is read at 5 Gbit/s. The path's own rate (6–16 Gbit/s expected) can't test it |
| `own:default`, `own:tuned` | no tree: Debian's qdisc and the path's own rate | `wan.sh` at C_R, one-way delay (r_R − r0_E)/2, queue max(50 ms, r_R) | DR-rate: what a real path carries. E checks the emulation at that rate too |
| E's anchors, `2:0:default` and `2:0:tuned` (added after the review) | – | EXP-13's anchors: `wan.sh` at 2 Gbit/s with r = 0, on each host setting | A3's parts are Δs from an anchor. R has no zero-delay condition; E's anchor lets E's parts at r_R be taken as EXP-13 takes them |

- **The 0.05 ms on R.** netem orphans packets only when a delay is set (EXP-13, the emulated WAN). With a delay set, R's tbf sees the same traffic as E's, and drops at the shaper the same way. It adds 0.1 ms to each round trip at R's `2g` and `5g` cells, and E's trees add the same, so the two still match. r_R itself is measured with no tree.
- **The queue.** `wan.sh` computes the tbf's queue from the delay. It gains an override, so R's shaper has E's queue.
- **r0_E** is E's own ping minimum with no tree, about 0.1 ms. So E's ping minimum matches R's.
- **C_R** is the largest steady rate R's set-up link tests carried (below), rounded down to 0.1 Gbit/s. E's own-rate tree uses it. If E's tree at r = 0 can't pass 0.9 × C_R with 16 tuned streams (its set-up), E's own-rate cells are flagged "emulator-limited", as EXP-13's A12. The same test at 5 Gbit/s must pass 4.5 Gbit/s, or E's `5g` cells are flagged so.
- **Host settings:** EXP-13's `default` (Debian's `tcp_rmem` and `tcp_wmem`) and `tuned` (both maxima at 128 MiB), set, guarded and reset as in EXP-13. If EXP-13's A21 adds a third setting, it runs here too, at 2 Gbit/s only (`2g:cos`), and is in the runs, time and egress below (after the review).
- **Nothing is added on E:** no loss, jitter or reordering. Whether leaving them out matters is the question, so R keeps every run its path's loss or jitter touches (X7; after the review).

### The control path, and what R's clock holds

- On R the harness's control path (port 2222) crosses the real WAN. It stays in the plain band at `2g` and `5g` and is unshaped at `own`, but each control command costs real round trips.
- **The sender's launch is inside the clock** for v1's arms and the `parcopy` arms, as in EXP-13. On E it crosses about 0.1 ms; on R it costs at least one r_R (an SSH channel's open and its command).
- **`launch_s`** (R and E): from the clock's start on dest to the sender's start on the source. The sender's start is stamped on the source's clock, and the offset between the two clocks comes from `agent.py clock-probe` at each visit, with `chronyc`'s offsets recorded beside it.
- **The reading compared:** R's `total_s` less `launch_s`, against E's `total_s`, for arms launched over the control path. `full` is launched on dest and needs no correction. Beside it, R's `total_s` against E's `R_trig` (`total_s` plus one r_R). If the two give different outcomes, the outcome is flagged "launch-sensitive".
- **The visit's control no-op check** compares R with R's own first visit, not with E.

### Data, trials, repeats and order

- pgbench scale 70 (2.1 GB) with EXP-01's `events` table and seed, as EXP-09 to EXP-13. Two trials, uniform 1% first, then hot 1%: EXP-13's deciding trials.
- **R's set-up, once per session before the sweep:** 200 pings; link tests from source to dest of 10 s each (the last 3 s steady) with 1, 8, 16 and 32 streams, on both host settings, unshaped; one stream from dest to source on both; pings during the 8-stream test on each setting. They give r_R and C_R, recorded before E's sweep starts.
- **E's set-up:** the same tests at r = 0, then 16 tuned streams through the tree at r = 0 at C_R and at 5 Gbit/s, then the tests through the `own` tree at r_R and C_R.
- **5 repeats.** Each repeat visits the six paired conditions in a seeded random order, and each visit runs its arms in a seeded random order. The seeds are 151 and 152 (one per trial) in session 1 and **153 and 154 in session 2** (revised after the review), so an order effect doesn't repeat in both sessions. Within a session the same seeds run on both pairs, so the pairs run the same arm at about the same time. E's sweep starts once R's set-up has fixed r_R and C_R, about 15 minutes after R's.
- **E's anchors close each repeat on E** (after the review): after its six paired visits, E visits `2:0:default` and `2:0:tuned`, in an order drawn from the trial's seed, v1 once in each. So E's paired visits keep R's order, and E falls about a minute behind R per repeat, about 5 per trial, recorded.
- **Both sessions start in the same UTC window,** 13:00–15:00 for the VMs' creation, fixed now (after the review): on a real path the backbone's load may follow the hour. Each session's start is recorded. A session that starts outside the window runs and is labelled "time of day" on every reading that compares the two sessions.
- **Every arm runs in every repeat at every condition where it runs.** No baseline is pooled across trials.
- **Each visit,** on both pairs: EXP-13's checks, adapted on R (X7); a one-stream link test (6 s, the last 3 steady). **Each trial's first visit to a condition:** 8 streams (and 16 and 32 at `own:default`, 16 at `5g:default`), one stream from dest to source, and pings during the 8-stream test.
- **Cache state, durability, verification and timeouts:** EXP-13's, unchanged: caches dropped after set-up, v1's B a fresh copy of B0, a stopped source for v1 and the `parcopy` arms, a running idle one for `full`; checks (a) and (b); A13–A15. EXP-11's per-trial probes (dest's durable write floor, the source's cold read floor) run in every trial on both pairs, since `parcopy8` runs, and X8 reads them (after the review).

### Measured

Everything EXP-13 measures in each run (its clock, the `ss` sampler, `nstat`, pgdelta's protocol timers with their busy and idle parts, the qdisc counters where a tree is set, durability and verification), plus:
- **R's path:** the visit's pings (minimum, median, p99 and loss); pings under an 8-stream load; retransmissions in every run. At `own` there is no shaper on R, so every retransmission is the path's or the VM's. Loss, changes of round trip and retransmissions are kept with their runs and reported; they exclude nothing (after the review).
- **`launch_s`** and the clocks' offsets, in every run.
- **The pair speed:** on all four VMs, at the session's start and end, pgdelta's hash (BLAKE2b, as v1) over a fixed 1 GiB in memory at 4 workers, 3 times, timed; and EXP-11's per-trial probes, above (after the review). They show whether R's and E's machines and disks differ in speed.
- **The platform:** each VM's CPU platform, image and kernel (after the review).
- **Egress:** R's source-to-dest and dest-to-source bytes at the interface, for the cost.

## Arms

| Arm | What runs | Conditions | Runs per trial and pair |
|---|---|---|---:|
| **`v1`** (`pgdelta_direct_inplace`) | EXP-13's v1: 4 jobs on each end, window 4, in place, stopped source, in clear | the six paired conditions; on E also its two anchors | 30 (40 on E) |
| `v1_w1`, `v1_w16` | v1 with `--window 1` or `--window 16` | `2g` and `own`, both settings | 40 |
| `full` | one `pg_basebackup` stream, as EXP-13: the copy Omni documents for a DR re-seed | the six paired conditions | 30 |
| `parcopy8` | EXP-11's bound, unchanged | the six paired conditions | 30 |
| `parcopy16` (added) | the bound with 16 streams | `own:default`, `5g:default` | 10 |
| `parcopy32` (added) | the bound with 32 streams | `own:default` | 5 |
| `full_nosync` | negative control, never a baseline | `2g:default`, repeat 1 | 1 |
| **all** | | | **146 (156 on E)** |

So 292 runs on R and 312 on E per sweep: 604 per session (revised after the review; 444 before). With EXP-13's A21 setting, `2g:cos` adds v1, the two window arms, `full` and `parcopy8`, 25 runs per trial and pair: 100 per session.

- **Why `parcopy16` and `parcopy32` at `own:default`.** At 50 ms, 8 default windows carry about 3.3–4.0 Gbit/s and 16 about 6.7–8.0, both possibly below C_R. A window-limited bound would favour v1 (EXP-13's A9). 32 windows carry 13–16 Gbit/s. At `2g`, 8 default windows already carry more than the link up to 60 ms, and at `own:tuned` 8 tuned windows carry far more than C_R.
- **Why `parcopy16` at `5g:default`** (after the review). 8 default windows carry 3.3–4.0 Gbit/s at 50 ms, below 5; 16 carry more. As in EXP-13 at 5:60:default.
- **The window arms run at `2g` and `own`.** On a real path the round trips are real, so the window's effect on them is what D29 needs. The `own` cells are their best case: the faster the send, the more round trips weigh. They don't run at `5g`, which A4 doesn't read.

### Considered and not run

| Option | Why not |
|---|---|
| A round trip near 120 ms (a region outside North America) | K3 allowed one session outside `us-central1`, and a second region pair is another owner decision. It is asked separately (Q2). Cost is not the reason (K1; revised after the review) |
| More region pairs, or more round trips | One pair at r_R checks the emulator. A sweep across paths is a different experiment |
| A zero-delay anchor on R | A real path has no zero-delay condition, so R1, L1 and L2 can't be judged on a real path; they stay EXP-13's. E's anchors run (after the review; they were here "not run"), and give A3's parts on E as Δs |
| Uniform 10%, the fallback (`v1_hc`), TLS, one source worker, 8 jobs, `probackup8`, the SSH probe | EXP-13 measures them at its emulated cells. None depends on the path in a way the agreement on v1 and the copies wouldn't show. TLS adds a handshake round trip, which the emulator delays like any other, and CPU, which the path doesn't change. Without the fallback, EXP-13's A5 can't be firm (EXP-13's revision says so) |
| Loss, jitter or reordering added on E | The question is whether leaving them out matters. R measures what the path has, and keeps it (X7) |
| External IP addresses | Traffic over them is capped at 3 Gbit/s per flow. EXP-13 uses internal addresses, and so does this |
| R and E one after the other | At the same time, the time of day and the network's state are the same for both. The quota allows exactly four VMs |
| A live source, data beyond RAM, persistent disks, GKE | T8, T9 and EXP-14 (T9-PD), OMNI-2 |

## Baselines and ratios

A cell is (pair, trial, condition). Everything below uses runs that pass verification, durability and the emulator's checks (X7), on medians of 5 runs, with the maximum beside them (rule 6). R's runs touched by loss or jitter are kept (after the review).
- **B_fast(c),** rule 3's fastest durable full copy measured at c: the fastest of `full`, `parcopy8`, and `parcopy16` and `parcopy32` where they ran. Ties within 0.05 s go to the lower maximum.
- **B_rel(c):** one `pg_basebackup` stream (`full`). `probackup8` doesn't run here, so B_rel is also F0, what Omni runs today.
- **`R_fast`** = v1's median over B_fast's, and **`R_rel`** over `full`'s, per trial (no pooling). On R, v1's and the `parcopy` arms' medians are of `total_s − launch_s` (the launch-sensitive view beside).
- **Robustness views:** `R_max` (maxima) and the launch view on R.
- **EXP-13's verdict rows,** applied at r_R exactly as EXP-13 registers them (rows 1–5, with "v1 not durable" and "Not judged" first, and its flags), on each pair. R1 isn't judged here: R has no anchor, and E's anchors run v1 alone. **Rows 1–3 are claims for v1, so they carry EXP-13's R1** (after the review): where R1 fired, or wasn't judged, in either EXP-13 session, they read "provisional: R1 fired (EXP-13)", as EXP-13's rows do. The ledger's rule says "before any WAN claim", and a real path doesn't lift it.
- **A3's parts** (after the review): on both pairs at `2g`, absolute (each part's median at r_R); on E also as Δs, each part's median at r_R less its median at E's anchor of the same host setting, as EXP-13's A3 takes them.

## Predictions

**Planning values, at r = 50 ms and C_R = 10 Gbit/s.** Before the probe's figures are recorded, `analysis/exp13_model.py` recomputes every range below at the probe's median r_R, with the model's other inputs as EXP-13 registers them (W = 2.6–3.15 MB per default stream, about 64 MiB tuned; 240 MB/s of payload at 2 Gbit/s, 600 MB/s at 5). Those ranges are recorded here, dated, before any arm runs, and they are the ones judged. The planning values stay beside them. The own-rate ranges are recomputed again at the session's C_R and reported beside, not judged.

| ID | Prediction | Basis |
|---|---|---|
| P1 | **The probe** picks a West Coast region, `us-west2` or `us-west1`, with r_R 40–55 ms | Great-circle distance from Iowa, with a fibre route 1.5–2 times as long; not measured |
| P2 | **The path at rest:** ping median within 0.5 ms of the minimum, p99 within 2 ms, no loss in 200 pings; on each session day within 3 ms of the probe | A provider backbone between two regions |
| P3 | **One stream at 2 Gbit/s:** default 0.42–0.50 Gbit/s on R and on E, within 10% of each other, with `rwnd_limited` + `sndbuf_limited` at least half of its busy time on both (EXP-13's E15); tuned at least 1.8 Gbit/s on both | W / r_R |
| P4 | **The path's own rate C_R:** 6–16 Gbit/s, with 32 default or 8 tuned streams; the lower of the VM's 16 Gbit/s egress cap and the inter-region quota bounds it. One default stream at its own rate: 0.42–0.50 Gbit/s; one tuned stream at least 5 Gbit/s | The per-VM cap ([Omni notes](../research/notes/alloydb-omni.md)); 64 MiB / 50 ms ≈ 10.7 Gbit/s per tuned stream |
| P5 | **Under load.** At `2g`, pings during an 8-stream tuned test rise alike on R and on E (the same tbf queue). At `own:tuned`, E's pings rise by up to max(50 ms, r_R), its tbf's queue, and R's by less than 10 ms; R's tuned bulk runs at `own` retransmit more than E's | A provider's backbone isn't expected to hold a 50 ms queue for one VM's flows; CUBIC then finds the rate by loss there |
| P6 | **Times at `2g`** (R's corrected, and E's): v1 hot 3.2–5.2 s default and 3.1–4.9 s tuned; v1 uniform 1% 12.9–16.0 s default and 5.8–7.4 s tuned; `full` 34–44 s default and 10.8–12.0 s tuned; `parcopy8` 9.5–10.4 s on both | EXP-13's E4–E7, scaled from 60 ms to 50 by its model |
| P7 | **Ratios at `2g`:** `R_fast` hot 0.31–0.55 default and 0.30–0.52 tuned; uniform 1% 1.24–1.68 default and 0.56–0.78 tuned. So **row 2** on R and on E. `R_rel` hot 0.07–0.15 default and 0.26–0.45 tuned; uniform 1% 0.29–0.47 default and 0.48–0.69 tuned | P6. EXP-13 expects row 2 at 60 ms (its E13) and at r_R (its E26). So R is expected to confirm EXP-13's row at r_R; its 60 ms row only if r_R is at least 48 ms; its 20 ms row never (revised after the review) |
| P8 | **The emulation stands at `2g`** (X2) on both host settings, in both sessions | The same shaper and queue on both pairs; at 2 Gbit/s the real path has room to spare |
| P9 | **The path's own rate.** v1 hot 3.1–4.9 s default and 3.0–4.7 s tuned; uniform 1% 12.9–16.0 s default and 3.8–5.5 s tuned; `full` 34–44 s default and 4.2–5.6 s tuned; B_fast 2.5–3.5 s on both. `R_fast`: hot 0.9–2.0 default and 0.9–1.9 tuned; uniform 1% 3.7–6.4 default and 1.1–2.2 tuned. So **Out at the path's own rate** against the fastest copy measured (X6). `R_rel`: hot 0.07–0.15 and uniform 0.29–0.47 on default hosts; hot 0.54–1.12 and uniform 0.68–1.31 tuned | v1's fixed work (EXP-11: about 3.1 s unthrottled for local changes) plus E6's latency; a 32-stream or tuned 8-stream copy at about 1.25 GB/s, above the disk's durable write floor (1.30 s for 2.1 GB, EXP-11); one default stream window-bound |
| P10 | **The window** on R at `2g:tuned` and `own:tuned`: `v1_w16` − v1 −0.3 to +0.1 s; `v1_w1` − v1 0 to +1.5 s (hot) and 0 to +0.8 s (uniform 1%); each within 0.15 s of E's | EXP-13's E9, scaled to 50 ms |
| P11 | **Pair speed and launch:** the hash benchmark, dest's durable write floor and the source's cold read floor each within 5% between R's and E's VMs, all four on one CPU platform (revised after the review); on R, `launch_s` is 1–3 round trips plus about 0.05 s, on E under 0.1 s | The same machine type, image and platform; an SSH session over a multiplexed connection plus the interpreter's start |
| P12 | **Durability and verification:** every durable run passes; `full_nosync` fails check (a) by at least 1.5 GB on both pairs | EXP-11, EXP-13 |
| P13 | **Egress** (revised after the review): 0.67–0.83 TB per session for C_R of 6–16 Gbit/s (0.77 at 10), about 12–15 USD at list price; about 0.06 TB more with EXP-13's A21 setting | The bytes under [Run plan and cost](#run-plan-and-cost) |
| P14 | **5 Gbit/s** (added after the review), R and E alike. Default: v1 hot 3.2–5.2 s, uniform 1% 12.9–16.0 s; `full` 34–44 s; `parcopy8` 5.1–6.4 s (window-limited); `parcopy16` 4.4–5.0 s. Tuned: v1 hot 3.1–4.9 s, uniform 1% 4.2–6.0 s; `full` 5.3–6.4 s; `parcopy8` 4.4–5.2 s. `R_fast`: default hot 0.64–1.18, uniform 1% 2.6–3.6; tuned hot 0.60–1.11, uniform 1% 0.81–1.36. So A18's reading "bounded by a link rate between 2 and 5 Gbit/s" on default hosts, and probably on tuned | EXP-13's E19, scaled from 60 ms to 50 by its model |
| P15 | **E's anchors** (added after the review): v1 hot 2.8–3.4 s and uniform 1% 4.8–5.8 s, the tuned anchor within 0.1 s of the default one (EXP-13's E1). On default hosts sending is the largest of E's Δ parts in uniform 1%; on tuned hosts idle waits are not the largest part (EXP-13's notes before running) | EXP-13's E1 and its notes |

## Decision rules

Every rule is judged per session, then read across the two sessions (X4 and X5 need both).

| ID | Condition | Consequence |
|---|---|---|
| **X1** | **Runs, and Not judged.** (i) At the read-first checks and the probe: no candidate passes the checks, or none has a probe median in [30, 60] ms with loss at most 1%. (ii) On a session day: R's median outside [30, 60] ms, loss above 1% in 200 pings, or a peer that doesn't answer (revised after the review). (iii) A deciding cell (v1 or B_fast's arm, hot or uniform 1%, at `2g` on either host setting, on R or on E) is not measured: more than 1 of its 5 runs excluded by X7, or none counts | (i) EXP-15 doesn't run. It is recorded here; EXP-13's DR rows keep "emulated WAN"; the owner is told. (ii) The session doesn't run: an infrastructure fault, another day. (iii) X2 and the rows at that host setting are Not judged in that session. Stated; nothing rerun |
| **X2** | **The emulation stands at r_R,** per session, at `2g`: (a) at each host setting, for both hot and uniform 1%, R's `R_fast` within ±10% of E's (\|R / E − 1\| ≤ 0.10); (b) EXP-13's rows, read from both host settings as EXP-13 reads them, give the same row on R and on E; (c) on default hosts, R's one-stream steady rate (the visits' median) within ±10% of E's. R's cells hold every run its path's loss or jitter touched, and E has none: that is the comparison (after the review) | All three hold: "stands at r_R". Any fails: "differs", with which test, at which host setting, by how much, and its direction (R's ratio above or below E's). Beside it: how many of R's visits had ping loss, and R's and E's retransmissions per run (after the review). Flags: *near the line* (a ratio within ±0.03 of 0.90 or 1.00 on either pair, or \|R / E − 1\| between 0.08 and 0.12; across sessions, within max(0.03, the ratio's shift between them), after the review); *fragile* (`R_max` gives another outcome); *launch-sensitive*; *pair-sensitive* (X8); *path moved* (X1, X7). A cell with lossy visits is labelled *lossy path*, which is reported and doesn't stop a route: it is what the comparison tests |
| **X3** | **The emulation at 5 Gbit/s and at the path's own rate:** X2's three tests at `5g:default`, `5g:tuned`, `own:default` and `own:tuned` (`5g` added after the review) | Reported. At `5g` it decides whether R's reading confirms EXP-13's A18 (X4). At `own` it decides only how X6 is read. Where it "differs", R's retransmissions, pings under load and `ss` readings beside E's say what the emulator leaves out |
| **X4** | **Which EXP-13 rows it confirms** (revised after the review). Needs X2 "stands" in both EXP-15 sessions, unflagged. Let row_R be the row R gives at r_R, the same in both sessions. **An EXP-13 row is confirmable only at the same round trip:** its row at r_R (its 2:r_R cells, where they ran), or its row at r where \|r − r_R\| ≤ 0.2 r. An emulated row at another r is a proxy for the real path (rule 11) | **EXP-13's row at r_R** is confirmed where it equals row_R in both EXP-13 sessions. **Its 60 ms row** is confirmed the same way only if r_R ≥ 48 ms. **Its 20 and 120 ms rows,** and its 60 ms row when r_R < 48 ms, are never confirmed here: they read "emulated WAN; emulator checked at r_R ms". A confirmed row reads "emulated WAN, confirmed on a real path at r_R ms (EXP-15)". **R's own row at r_R,** the same in both sessions and unflagged, is the real-path DR row at r_R, whatever EXP-13 gives; rows 1–3 carry EXP-13's R1. **A2 (D36):** confirmed where row_R is row 2 and, on R's default hosts, v1's sending socket in uniform 1% is `rwnd_limited` + `sndbuf_limited` for at least half its busy time, in both sessions. Then D36's contradiction is measured on a real path at r_R. **A3's named fix:** confirmed where (1) R's and E's absolute parts at r_R (idle waits, fixed round trips, sending, residual) agree within max(0.1 s, 10%) each, so the emulator reproduces the timers there, and (2) E's Δ parts at r_R, from E's anchor of the same host setting, name the same largest part as EXP-13's A3 at r_R (or at 60 ms, if r_R ≥ 48 ms), in both sessions. With the machines alike (X8), (1) and (2) say R's own Δ parts would name that part. **A4 (D29):** R's own reading decides: A4's thresholds on R at `2g:tuned` and `own:tuned` give the same outcome at both cells, in both sessions, and the same as on E. EXP-13's A4 at 120 ms tuned is reported beside it and confirmed by nothing here. **A18 (5 Gbit/s at 60 ms):** confirmed where X3 stands at `5g` on that host setting, R's reading at `5g` gives A18's outcome on that setting, the same in both sessions and as EXP-13's at 5:60, and r_R ≥ 48 ms. Otherwise A18 stays provisional, and R's `5g` reading at r_R is reported as the real-path result at 5 Gbit/s. **E3's link-test model at r_R:** confirmed where R's 1-, 8- and 16-stream rates at `2g` and at `own:default` are within 10% of E's |
| **X5** | **What it overturns** (revised after the review). (i) X2 "differs" in both EXP-15 sessions: (a) or (c) at a host setting, or (b). (ii) X2 stands, but row_R differs from EXP-13's row at r_R, or from its 60 ms row where r_R ≥ 48 ms. (iii) The two EXP-15 sessions differ on X2 or on row_R | (i) Every EXP-13 DR row that reads that host setting (every row, if (b) failed) stays provisional: "the emulation differed from a real path at r_R ms (EXP-15)". If R's ratios are higher than E's (worse for v1), no DR claim for v1 rests on those rows, at any r; X3's readings say what the emulator leaves out, and a revised emulation of EXP-13's deciding cells is proposed for the queue. If lower, EXP-13's rows are conservative for v1, still provisional. (ii) That EXP-13 row stays provisional: "a real path at r_R ms gave row n". At r_R, EXP-13 and E then differ through the same emulator, a shift between experiments, reported with both. (iii) Every outcome is provisional, with both sessions' readings; a third session is proposed to the owner |
| **X6** | **DR at the path's own rate,** per session, on default hosts first and tuned beside, as EXP-13 reads them. **Against the fastest copy measured** (rule 3): `R_fast` ≤ 0.90 for hot and uniform 1% "holds"; ≥ 1.00 for either "Out"; otherwise "Undecided". **Against one `pg_basebackup` stream:** the same bands on `R_rel` | Each reading names its yardstick. **Out against the fastest copy, in both sessions, unflagged:** "DR first" holds only on DR links slower than this path's own rate. The ledger's positioning states the link rate with it: R's reading at 5 Gbit/s (X4's A18) and EXP-13's crossover between 2 and 5 Gbit/s at 60 ms (its A18, interpolated, labelled), beside C_R here. DR-rate becomes "measured on one real path". The reading against one stream is stated separately. **Holds in both:** "DR first" holds on this path at its own rate, against the fastest copy measured, on these VMs. **Otherwise:** both readings kept, provisional. Flags: *near the line* (across sessions, within max(0.03, the ratio's shift between them); after the review); *window-limited bound* (B_fast's arm's link test below 0.9 × C_R); *quota-limited* (C_R within 10% of the inter-region quota read first); *emulator-limited* applies to E only |
| **X7** | **Validity,** EXP-13's rules adapted to a real path (revised after the review). **Durability, verification, timeouts:** EXP-13's A13, A14 and A15, with A15's bounds four times the upper ends recomputed at r_R (at least 60 s). **On R, each visit:** the peer answers; its 20 pings' minimum in [r_R − 0.5, r_R + 2] ms; at `2g` and `5g`, the tree parses as intended. **On R, each run:** the sending sockets' `minrtt` in [r_R − 0.5, r_R + 3] ms; at `2g` and `5g`, netem's own drops 0. **On E:** EXP-13's checks at r = r_R, and at r = 0 at its anchors | **Infrastructure faults only:** a visit whose peer doesn't answer, or whose 20 pings lose 3 or more, or whose tree doesn't parse, is retried once, before any run; a second failure stops the trial ("unreachable" or "gross loss"). **Kept and labelled:** a visit with lower loss is labelled *lossy path*; a visit or run whose minimum round trip falls outside its range is labelled *path moved* (R's base round trip changed, so E no longer emulates it), and its runs are kept; X2 is then flagged. **Excluded:** a run that fails durability, verification or its timeout, or has netem's own drops above 0 on a tree, is excluded and reported; more than 1 of 5 at a cell leaves it not measured (X1 (iii)). Retransmissions are recorded, not judged |
| **X8** | **Pair speed:** R's and E's VMs (source with source, dest with dest) differ by more than 5% in the hash benchmark's median, dest's durable write floor or the source's cold read floor (each trial's medians), or the four VMs report different CPU platforms (revised after the review) | Any X2 or X3 difference above 10% is flagged "pair-sensitive", and X2 (a) is also read on v1's network parts alone (A3's idle waits, fixed round trips and sending), which the machines' speed doesn't set. A platform difference flags every R-against-E reading "pair-sensitive" |

### Which EXP-13 rows it confirms or overturns

Revised after the review: a row is confirmable only at r_R, or within 0.2 r of it.

| EXP-13 item | Confirmed here when | Stays provisional, with the reason, when |
|---|---|---|
| DR rows at r_R (its 2:r_R cells, run if the probe came before its session 1) | X2 stands in both sessions, and row_R equals EXP-13's row at r_R in both of its sessions | X2 differs (X5 (i)), row_R differs (X5 (ii)), or the sessions differ (X5 (iii)) |
| DR rows at 60 ms | The same, with its 60 ms row, and only if r_R ≥ 48 ms | r_R < 48 ms: "emulated WAN; emulator checked at r_R ms". Otherwise as the row above |
| DR rows at 20 and 120 ms | Never here: r_R is at least 30 ms, so neither lies within 0.2 r of it | Always: "emulated WAN; emulator checked at r_R ms" |
| A2: D36 contradicted for DR on default buffers | row_R is row 2, and R's `ss` shows the window, in both sessions: the contradiction is then measured on a real path at r_R | R gives row 1 on default hosts, or R's `ss` doesn't show the window |
| A3's named fix | R's and E's absolute parts agree, and E's Δ parts from its anchor name EXP-13's part at r_R (or at 60 ms if r_R ≥ 48 ms) | They don't agree, another part is largest, or no EXP-13 reading lies near r_R |
| A4: the window (D29) | R's own A4 outcome at r_R, the same at `2g:tuned` and `own:tuned`, in both sessions, and on E. EXP-13's 120 ms reading is reported beside it | They differ; the default stays 4 |
| E3's link-test model | R's rates within 10% of E's | Otherwise |
| A18: 5 Gbit/s at 60 ms | X3 stands at `5g`, R's `5g` reading gives A18's outcome in both sessions, as EXP-13 does, and r_R ≥ 48 ms | Otherwise; R's `5g` reading at r_R is reported as the real-path result at 5 Gbit/s |
| L1, L2 and R1 | Not checked: a real path has no zero-delay anchor | – |

### Rule 11: which outcomes may pick a single route

- **X4's confirmations** may pick a route only with both EXP-15 sessions and both EXP-13 sessions agreeing, unflagged (EXP-13's rule 11 section). The route is then firm for the conditions measured: n2-standard-8 VMs, local NVMe, Debian's TCP defaults or the tuned maxima, 2.1 GB, v1's stopped-source path at 4 jobs, idle, in clear, one region pair at r_R. Used for Omni's DR (GKE nodes, persistent disks, a live primary, other region pairs), it states those conditions and stays provisional for them until T8, EXP-14 (T9-PD) and OMNI-2.
- **R's own row at r_R** (after the review) is firm for those conditions when both EXP-15 sessions give it, unflagged. Rows 1–3 are claims for v1, so they also need EXP-13's R1 not to have fired, and to have been judged, in both EXP-13 sessions.
- **X6's "Out" or "holds"** may change the positioning only with both sessions agreeing, unflagged, under the same conditions. Otherwise both readings are kept, and "DR first" stays as the ledger has it, provisional.
- **X5's outcomes** keep EXP-13's rows provisional. They withhold claims and close no route, so they apply after one session.
- **X1, X3, X7 and X8** are validity rules and readings. They pick nothing.
- **A D-row grade** that moves on this evidence (D36, D29) moves as EXP-13's rules say, recorded "provisional (rule 11)" until firm.
- **K10.** One session is never firm here. The shift between the two sessions is reported per arm and pair, and per deciding ratio. "Near the line" across sessions is a margin under max(0.03, that ratio's shift) (after the review).
- **K3 and two sessions.** K3 allowed "one session outside `us-central1`". K1 and K12 ask two sessions per test, on different days. This design runs two, and asks the owner to confirm before session 2, and before the probe (Q1). Without the second session, every outcome here stays provisional, and so do EXP-13's DR rows.
- **Expected** (P7, P9, P14; after the review): X6 "Out" at the path's own rate against the fastest copy, firm if both sessions agree; R's row at r_R row 2, which confirms A2's contradiction of D36 on a real path, while its claim for v1 stays provisional by EXP-13's R1, which is expected to fire on tuned hosts; A4 unchanged (P10).

## Limitations, known before running

- **One region pair, one round trip.** Paths differ between region pairs, and a path can change between days. 120 ms isn't checked, and neither is 20 ms.
- **A provider backbone.** Omni's DR between other clouds, or to a data centre, crosses other networks.
- **VMs, not GKE nodes;** Debian's TCP settings or the tuned maxima; local NVMe; 2.1 GB in RAM; v1 and the bound read a stopped source; the source idle; everything in clear.
- **The launch correction** rests on the clocks' offset, good to about a millisecond on a symmetric path, and worse on an asymmetric one.
- **Pair speed** between R and E is measured (X8), not removed.
- **The inter-region quota** may set the own rate, not the path (flagged).
- **Two trials only** (hot and uniform 1%); no uniform 10%, fallback, TLS or one-source-worker arm here.
- **E reproduces R's minimum round trip, not its jitter or loss.** That difference is what X2 and X3 measure, with R's lossy visits kept (after the review).
- **Two sessions, two days,** in the same UTC window, with different seeds. Their shift is measured, not removed.

## Smoke run

Before the sweep, in each session, on all four VMs.

| # | Item | Record | Pass | If it fails |
|---|---|---|---|---|
| 1 | **Reach across regions:** the Mac to R's dest over SSH; R's dest to R's source on port 2222 (the control path) and on the data ports, over internal addresses | `run.json`: `control`; the set-up's connect probes | all connect | the region is unusable: X1 (i) or (ii) |
| 2 | **R's `2g` and `5g` trees:** `wan.sh` with a one-way 0.05 ms and the queue override parses as intended; the band counters show the control path in the plain band; the ping minimum is r_R + 0.1 ms, ±0.5 | trial record: `visits[].tree`, `visits[].pings` | as intended | fix `wan.sh` or the override |
| 3 | **E's trees at r_R, at C_R and at 5 Gbit/s, and its anchors:** EXP-13's smoke items 1–6 at r_R; 16 tuned streams at r = 0 pass 0.9 × C_R, and 4.5 Gbit/s at 5 Gbit/s | trial record: `visits[]`, `first_visit.link_tests[]` | as EXP-13 | E's own-rate or `5g` cells flagged emulator-limited |
| 4 | **Launch timing:** 10 clock probes between each pair's VMs spread under 1 ms; `launch_s` for v1 and `parcopy8` at scale 5 is 1–3 round trips plus up to 0.1 s on R, under 0.1 s on E | run records: `launch_s`, `clock_offset` | yes | fix the stamp or the probe before the sweep |
| 5 | **Pair speed:** the hash benchmark on all four VMs | `run.json`: `pair_speed` | recorded | – |
| 6 | **Every arm at every condition on both pairs,** E's anchors included, scale 5, 1 run | run records: `verification`, `durability`, the network fields | all pass | as EXP-13 |
| 7 | **Scale 70,** uniform 1%, `2g:default`, every arm once on both pairs: the time projection and the egress counters | run records: `totals.total_s`, wire bytes | – | a projection above the plan's upper end (4.5 h, 5.0 h with A21's setting) is searched for a fault, as EXP-13's revision does; the session runs uncut otherwise |
| 8 | **Idle noise** for check (a) on both dests: EXP-13's item 18 table | `run.json`: `dirty_idle_noise` | as EXP-13 | as EXP-13 |
| 9 | **`analysis/exp15.py`** end to end on the smoke records | its output directory | runs | fix |
| 10 | **Clean-up:** `wan.sh off` restores Debian's qdisc on all four VMs | `run.json`: `teardown` | yes | fix |
| 11 | **The platform** (added after the review): every VM's zone, image and CPU platform; the session's UTC start and seeds | `run.json`: `environment.gce`, `environment.image`, `session` | the set files' values on all four; a start in 13:00–15:00 UTC | a VM on another platform is created again once; a difference left is flagged "pair-sensitive" (X8), a start outside the window "time of day" |

## Run plan and cost

**Before EXP-13's session 1, once** (revised after the review): the read-first checks (no VMs); the probe (about 15 minutes, at most 10 vCPUs, about 1.25 instance-hours of `e2-small`, under 1 USD), if the owner allows it (Q1); the region, r_R and the recomputed predictions recorded here, dated.

**Per session,** four n2-standard-8 VMs at once (32 vCPUs, the whole quota):

| Step | Runs | Wall time |
|---|---:|---:|
| Create the four VMs in two regions (zones, image and platform pinned); provision (`startup.sh`, `install_incumbents.sh`); deploy; SSH set-up | – | 25 min |
| R's set-up (pings, link tests at 1, 8, 16 and 32 streams on both settings); E's set-up at r = 0 (C_R and 5 Gbit/s) and at r_R and C_R | – | 15 min |
| Smoke run | about 90 (both pairs) | 50 min (40–60) |
| Sweep: uniform 1% and hot, six conditions, 5 repeats, both pairs at once, E's anchors closing each repeat | 604 (R 292, E 312) | 140 min (125–160) |
| Only if EXP-13's A21 adds a third host setting: `2g:cos` on both pairs | 100 | about 25 min |
| Fetch from all four, then `wan.sh off` and teardown | – | 10 min |
| **Total** | **about 695 (795 with A21's setting)** | **about 4.0 h (3.6–4.5); about 4.5 h with A21's setting** |

The sweep's time, from EXP-13's per-run overheads: the registered four conditions took about 100 minutes (90–115); `5g` adds about 28 minutes on both pairs at once, and E's anchors put E about 11 minutes behind R by the sweep's end.

- **Instance-hours:** about 16 per session (14.4–18; 18 with A21's setting); about 32 for the two sessions (29–36), plus the probe.
- **Inter-region egress, R only, both directions** (recomputed after the review, from each test's length, stream count and rate):

| Per session | C_R 6 Gbit/s | C_R 10 Gbit/s | C_R 16 Gbit/s |
|---|---:|---:|---:|
| Copies: hot 163 GB, uniform 1% 204 GB (2.12 GB per full copy and `parcopy` run, 29 or 609 MB per v1 run) | 367 GB | 367 GB | 367 GB |
| Per-visit one-stream link tests (6 s; 60 visits) | 105 GB | 135 GB | 140 GB |
| First-visit tests (8, 16 and 32 streams where they run, one stream back; 6 s) | 79 GB | 99 GB | 116 GB |
| R's set-up (10 s tests at 1, 8, 16 and 32 streams on both settings, one back) | 58 GB | 90 GB | 120 GB |
| Smoke run (one trial's first visits and one repeat's visits, the scale-5 and scale-70 runs) | 64 GB | 77 GB | 86 GB |
| **Total** | **0.67 TB** | **0.77 TB** | **0.83 TB** |

  A tuned stream is taken at min(C_R, 10.7 Gbit/s), a default one at 0.46 Gbit/s, a shaped test at 0.95 of its tbf rate; pings under load share the 8-stream test. The `5g` condition accounts for about 0.19 TB of each total, and EXP-13's A21 setting would add about 0.06 TB. The design before the review gave about 0.45 TB per session, and the review about 0.45–0.7; without `5g` this model gives 0.48–0.63 TB.
- **Cost:** at about 20 USD per TiB, 12–15 USD of egress per session (16 with A21's setting); at about 0.5 USD per instance-hour, about 8 USD of VMs. About 20–24 USD per session, and 40–50 USD for both, with the probe.
- **When** (revised after the review). [PLAN.md](../PLAN.md#schedule-to-the-presentation-october-25-2026) puts EXP-15 in the week of November 1, alone: it takes the whole 32-vCPU quota, and under K2 the project stays within the quota until the owner raises it. Session 2 runs on another day, also alone, in the same UTC window. The probe runs earlier, in wave 0, before EXP-13's session 1. The sessions come after EXP-13's two, so X4 can read them; if they run first, X4 is read once EXP-13's exist.

## What it needs built in the harness

About 1.75–2.25 days, after EXP-13's revision's additions, whose set files, session record and image and CPU-platform options it shares (revised after the review). The per-pair infrastructure is built on `build/infra-pairs` (428bd6c, not merged): named VM sets with their own host keys, a zone, machine type and data disk per role (`DEST_ZONE` puts R's dest in another region), and `remote.py` describing each VM in its own zone. What remains:

| Block | Files | What |
|---|---|---|
| Set files | `infra/gcp/sets/` | Two sets, R and E: R's source and both of E's VMs in `us-central1-a`, R's dest in the zone the probe picked; the image and CPU platform EXP-13's sessions used, pinned in both |
| Read-first checks | `infra/gcp/` (a script) | The quotas, the default network's subnets and firewall rules, read and printed; nothing changed |
| The probe | `infra/gcp/` (a script) | Create the labelled `e2-small` VMs, ping and connect over internal addresses, print the figures with each candidate's loss, delete the VMs |
| `wan.sh` | `infra/gcp/wan.sh` | An override for the tbf's queue (in ms), so R's shaper can have E's queue at a 0.05 ms delay |
| Real-path conditions | `experiments/network/run_exp04.py`, `measure.py` | `own` conditions with no tree, and R's `2g` and `5g` trees at 0.05 ms; visit and run checks against the measured r_R (X7) that label lossy visits and a moved path and keep their runs, with an unanswering peer or gross loss as an infrastructure fault; conditions at a measured r_R and C_R for E, written into E's run line from R's set-up and recorded |
| E's anchors | `run_exp04.py` | Conditions that close each repeat after the paired ones (`--trailing-conditions`), on E only, in an order drawn from the trial's seed |
| Launch timing | `agent.py`, `run_exp04.py` | The sender's start stamped on the source; a clock probe at each visit; `launch_s` and the offset in every run record |
| Arms | `measure.py` | `parcopy32` in `PARCOPY_VARIANTS`; A15's bounds from the ranges recomputed at r_R, the `5g` cells and E's anchors included |
| Pair speed | `agent.py` | A hash benchmark over a fixed 1 GiB in memory at 4 workers. EXP-11's probes are already taken with `parcopy8` |
| Pings under load | `run_exp04.py`, `agent.py` | Pings during an 8-stream link test, at set-up and first visits |
| Analysis | `analysis/exp15.py` (new) | Per pair and session: ratios, EXP-13's rows at r_R (its code reused) with EXP-13's R1 carried on rows 1–3, X1–X8, A3's parts absolute and from E's anchor, the network table (lossy visits, pings under load, retransmissions, `ss`, link tests against the model); X4 and X5 against EXP-13's two sessions' cells at r_R, and within 0.2 r; the two-session reading, with each deciding ratio's shift |
| Tests | `tests/` | Each block, and the rules on synthetic medians, every outcome of X2, X4, X5 and X6 reachable; a lossy visit kept and labelled; X4 refusing a row outside 0.2 r |

## Questions for the owner

- **Q1. The probe's four regions, and two sessions outside `us-central1`?** (revised after the review) K3 allowed one session outside `us-central1`. The probe puts one `e2-small` VM for about 15 minutes in each of `us-west2`, `us-west1`, `us-west4` and `us-east1`, best before EXP-13's session 1, so that EXP-13 can run cells at the real path's round trip; without the probe by then, EXP-13 runs without them, and a real path confirms an EXP-13 row only within 0.2 r of its round trip. K1 and K12 ask two sessions per test, on different days, with R's dest in the region the probe picks. Without the second, every outcome here, and every DR row of EXP-13, stays provisional (K10).
- **Q2. A second region pair, near 120 ms?** (added after the review) No real path here comes near EXP-13's 120 ms rows, nor its 20 ms row, so they stay provisional whatever EXP-13 shows. A pair from `us-central1` to a European region would be one more set of sessions outside `us-central1` (K3 again), designed separately if the owner wants the 120 ms rows firm.
- Nothing else blocks it. The region is picked by the probe, under the rule above. The quotas are read, not raised (K2, October 25: work within them for now).

## After the review (October 25, 2026)

An independent review read this design with EXP-13's revision under rule 11 and made 20 findings, 7 major. Each was checked against this document, EXP-13, the ledger, FIRMNESS.md, EXP-11 and the code. All held; none is rejected. The ones that touch this design, and what changed (EXP-13's [After the review](EXP-13-latency-transport.md#after-the-review-october-25-2026) lists all 20):
- **A real path confirms an EXP-13 row only at its own round trip** (major). X4 had confirmed EXP-13's 20 or 60 ms row whenever it equalled R's row, while the 120 ms rows were refused for the same factor-of-2 stretch. Now the probe runs in wave 0, before EXP-13's session 1, so EXP-13 adds 2:r_R cells on both host settings; X4 confirms EXP-13's row at r_R, and its row at r only where \|r − r_R\| ≤ 0.2 r (the 60 ms row if r_R ≥ 48 ms; the 20 and 120 ms rows never). R's own row at r_R, in both sessions, is the real-path DR row. P7, X5 and the confirmation table follow. Both of the review's options are taken: the r_R cells, with the tolerance as the fallback when the probe can't come first.
- **A3 is confirmed from an anchor on E** (major). Absolute parts carry the zero-delay work (about 2.5 s of sending and 2.6–3.0 s of hashing in uniform 1% at 2 Gbit/s), so their largest part would say little about latency. E now runs EXP-13's two anchors, v1 only, closing each repeat; X4 confirms A3's fix where R's and E's absolute parts agree and E's Δ parts name EXP-13's part at the same round trip. The "An anchor on E" row under Considered and not run is replaced.
- **A 5 Gbit/s condition** (major). A18 is read at 5 Gbit/s and X6 at C_R (6–16 Gbit/s), so X6 couldn't test it. `5g:default` and `5g:tuned` run on both pairs, with v1, `full`, `parcopy8` and, on default hosts, `parcopy16`, in hot and uniform 1% (P14). A18 is confirmed by R's `5g` reading, within the same round-trip tolerance.
- **Loss is kept, not gated** (major). Any ping loss had ruled out a region, cancelled a session or stopped a trial, so R was measured only while loss-free, against the question. Now only an unanswering peer or gross loss (above 1% of 200 pings, or 3 of a visit's 20) is an infrastructure fault; lossy visits keep their runs, labelled, and X2 and X3 compare R with its impairments against loss-free E, with the count of lossy visits beside them. A change of R's base round trip keeps its runs too, flagged "path moved".
- **K2 and EXP-14** (minor). The branch is rebased on main, where K2 reads "work within the quota for now" (October 25; the review cited an earlier version of that day's answer, "no vCPU cap", which the owner revised the same night). EXP-15 runs alone until the owner raises the quota. T9-PD is named as EXP-14.
- **Platform pinned** (minor). One image and one CPU platform on all four VMs and in both sessions, from set files, with R's source and E in `us-central1-a`.
- **New seeds and one time window** (minor). Seeds 153 and 154 in session 2; both sessions start 13:00–15:00 UTC.
- **X8 sees the disks and the platform** (minor). EXP-11's write and read floors, taken anyway with `parcopy8`, and the CPU platform join the hash benchmark.
- **Egress recomputed** (minor), per test, at C_R of 6, 10 and 16 Gbit/s, with `5g` and A21's setting in the runs, time and cost: 0.67–0.83 TB per session, against 0.45 before.
- **The probe's regions go to the owner, and 120 ms is K3's question alone** (minor): Q1 and Q2. Cost is no longer given as a reason.
- **What is built** (minor). The harness list points at `build/infra-pairs` and keeps only what remains.
- **A4 moves on R's own reading** at r_R, in both sessions, agreeing with E, with EXP-13's 120 ms reading beside it (minor).
- **Near the line across sessions** uses each ratio's measured shift (minor).

Found while checking: EXP-15 had said EXP-13's R1 "applies to EXP-13's rows" only, which would have let R's rows 1–3 make a WAN claim for v1 that the ledger's rule withholds. Rows 1–3 here now carry EXP-13's R1. This doesn't favour v1.

## A lean session instead (October 25, 2026, before running)

Under rule 11 as the owner clarified it (small experiments, no two-session programme), and after [EXP-20](EXP-20-dr-latency-core.md) to [EXP-24](EXP-24-dr-with-bbr.md) found that v1's DR result turns on cubic's early-loss slow mode, which BBR avoids, the design above is on hold and this one session runs instead. Its question: **does the slow mode, and BBR's escape from it, happen on a real link between regions, not only in netem?**

- **VMs** (set file `infra/gcp/sets/xr.sh`): two `n2-standard-8` with local NVMe, the source in `us-central1-a`, dest in `us-west1-b`; the harness's link tests and pings record the real round trip and one stream's rate. BBR is added to the source's allowed list (the default stays cubic), as in EXP-24.
- **Conditions:** no emulated delay; the tree's 2 Gbit/s rate limit on default and on tuned hosts (2:0:default, 2:0:tuned), so the real path supplies the round trip.
- **Arms:** `pgdelta_direct_inplace` (cubic), `pgdelta_direct_inplace_bbr`, `full`, `parcopy8`; hot and uniform 1%; **5 repeats**.

**Predictions**
1. The round trip is 30–50 ms; one default stream reaches 0.9–1.4 Gbit/s, a tuned one at least 1.8.
2. For uniform 1%, BBR v1 is in the slow mode in 0 of 10 runs (both settings).
3. For uniform 1% on tuned hosts, BBR v1 ÷ the fastest copy is at most 0.90 (EXP-24 at 20 and 60 ms tuned: 0.63–0.73).
4. Whether cubic v1 falls into the slow mode here is the open question: reported as a count, not predicted.

**What follows:** if 2 and 3 hold, the emulated results stand on a real path and BBR's role is confirmed; if BBR v1 is slow here, the emulator misled and the DR claim goes back to provisional.
