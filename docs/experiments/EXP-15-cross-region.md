# EXP-15: Do EXP-13's emulated DR verdicts hold on a real cross-region link? (T12x)

Status: **pre-registered October 25, 2026, as a proposal; not reviewed, not built, not run.** This is test T12x, proposed by the [rule 11 audit](../FIRMNESS.md#41-the-tests) and allowed by the owner's K3 ([ledger](../DECISIONS.md#decisions-of-october-24-2026-night-after-the-rule-11-audit)). It checks [EXP-13](EXP-13-latency-transport.md)'s emulated WAN on a real path. EXP-13's [revision under rule 11](EXP-13-latency-transport.md#revisions-under-rule-11-october-25-2026-before-running) makes this check a condition for any of its DR rows to pick a route. Like EXP-11 to EXP-13, this design should get an independent review, and a check of the revision, before its harness is built. Everything here is written before running. Two things can only be fixed by measuring the path: the region, and its round trip r_R. A probe fixes them before session 1 (below), and they are recorded here, dated, before any arm runs.

## Question

EXP-13 measures DR's round trips only through netem, between two VMs in one zone: a constant delay, one token-bucket shaper with a queue of max(50 ms, r), no jitter, loss or reordering, and CUBIC. Under rule 11 an emulation is a proxy: it fails the test "measured on the relevant platform, not by proxy" ([FIRMNESS.md](../FIRMNESS.md), items X13-wan and X13-rows). So no DR row of EXP-13 can pick a route until a real path agrees.

Here, v1's path and the full copies run across a real link between two Google Cloud regions, and, at the same time, across EXP-13's emulation of that same link between two VMs in one zone. The questions:
1. **Does the emulation reproduce a real path at that path's round trip?** On the same arms, workloads, link rate and host settings, do the two give the same ratios and the same verdict row?
2. **Which of EXP-13's rows does a real path confirm, and which does it overturn?**
3. **What does a real cross-region path carry, and does v1 beat the fastest copy at that path's own rate?** (DR-rate: no DR link rate is documented, and "DR first" assumes DR links run below v1's crossovers.)
4. **Does pgdelta's request window matter on a real path?** (D29)
5. **Is one TCP stream window-limited on a real path as it is on the emulator?** (D36)

**What would change the plan** (the full rules are under [Decision rules](#decision-rules)). Under rule 11, each "Then" follows only from a firm outcome (see [Rule 11](#rule-11-which-outcomes-may-pick-a-single-route)):

| If | Then |
|---|---|
| The emulation reproduces the real path at r_R, and the real path gives the row EXP-13 gave at 20 or 60 ms **(expected: its 60 ms row)** | That EXP-13 row is confirmed on a real path, and may pick its route if EXP-13's two sessions also agree, unflagged |
| The emulation doesn't reproduce the real path, and the real path is worse for v1 | No DR claim for v1 rests on EXP-13's emulated rows. The emulator gets what the real path showed (loss, jitter, queueing), and EXP-13's deciding cells are proposed for a rerun with it |
| At the path's own rate, a range-parallel copier beats v1 **(expected)** | "DR first" holds only on links slower than this path. The positioning states the link rate with it, and names its yardstick: against one `pg_basebackup` stream v1 is still expected to win on default hosts |
| At the path's own rate, v1 beats the fastest copy measured | "DR first" holds on this real path at its own rate, on these VMs |

## Setup

### The region pair, and the check that fixes it before running

- **Source:** `us-central1`, in the zone EXP-13 ran in (`us-central1-a` unless it is out of stock; the zone is recorded).
- **Dest:** the region whose round trip from `us-central1` is between 30 and 60 ms and nearest 60 ms. 60 ms is the ledger's DR round trip. No North American region is expected near 120 ms, K3 allowed one session outside `us-central1`, and North America keeps the egress price at its lowest.
- **Candidates:** `us-west2` (Los Angeles), `us-west1` (Oregon), `us-west4` (Las Vegas) and `us-east1` (South Carolina). Their round trips from `us-central1` are not documented in this repository, and none is assumed here.
- **The probe, before session 1** (after the read-first checks below pass for a candidate):
  - one small VM (`e2-small`, labelled `purpose=pg-delta-benchmark`) in the source's zone, and one in a zone of each candidate region;
  - from the `us-central1` VM, as root, 200 pings 0.05 s apart to each candidate's internal address, with the path idle; their minimum, median, 99th percentile and loss;
  - 20 TCP connects to each candidate's port 22, which checks that the internal firewall rule reaches it;
  - then every probe VM is deleted. About 15 minutes; 5 VMs, at most 10 vCPUs at once; about 1.25 instance-hours of `e2-small`.
- **The rule that picks the region:** among candidates whose median is in [30, 60] ms with no loss, the one nearest 60 ms. A tie within 2 ms goes to the smaller p99 less minimum. If none is in range, EXP-15 doesn't run; that is recorded here, and the owner is asked (another region pair would need K3 again).
- **Recorded here, dated, before session 1:** the region, the zones, the probe's figures, and the predictions recomputed at its median (see [Predictions](#predictions)).
- **On each session day,** R's set-up measures r_R again: 200 pings on internal addresses. A median outside [30, 60] ms, or any loss, and the session doesn't run: an infrastructure fault, the VMs deleted, another day. A median more than 3 ms from the probe's flags the session "path moved"; the session's own r_R is what E emulates.

### Read first (read-only, before the probe)

Every check is a `gcloud ... describe` or `list`. Nothing is raised or changed: K2 (a quota raise) is still open, and IAM, firewall, network, billing and org-policy changes stay off limits.
- **The project's CPU quota across all regions:** 32 vCPUs. A session uses all 32 (four n2-standard-8), so nothing else may run during one.
- **Each region's quotas:** in `us-central1`, CPUs and N2 CPUs for three VMs (24), local SSD for three VMs (4,500 GB), and three of its 8 external addresses. In the candidate, CPUs and N2 CPUs for one VM (8), local SSD for one (1,500 GB), and one external address (the Mac reaches each VM over its external address, as today).
- **The per-project inter-region traffic quota** named on the VPC quotas page ([Omni notes](../research/notes/alloydb-omni.md)): its name and value for the pair, recorded. Below 2 Gbit/s, the 2 Gbit/s conditions can't run, and EXP-15 doesn't run. Below what the path carries, the own-rate cells measure the quota, flagged "quota-limited" (X6).
- **The default network:** a subnet in the candidate region; its internal firewall rule's source ranges cover that subnet; its SSH rule admits the Mac there. Any failure drops the candidate.
- **The price of inter-region egress:** about 20 USD per TiB between North American regions at list price ([Omni notes](../research/notes/alloydb-omni.md), fetched October 23), re-read and recorded with its date.

### Machines

- **Pair R (real):** the source in `us-central1`, the dest in the chosen region.
- **Pair E (emulated):** source and dest both in R's source zone.
- Four `n2-standard-8` VMs, each with four local NVMe SSDs in RAID 0 at `/data`, ext4, Debian 12, PostgreSQL 17 from PGDG, as EXP-13. They are labelled `purpose=pg-delta-benchmark`, created fresh for each session, and deleted after their results are fetched. 32 vCPUs: the whole quota.
- The same deployed commit on all four, with EXP-13's harness and the additions under [What it needs built](#what-it-needs-built-in-the-harness).
- Data crosses internal addresses only. If the chosen region's zones have no `n2-standard-8` with four local SSDs, the next candidate whose probe passed is used, and recorded.

### Conditions

| Condition | On R | On E | Why |
|---|---|---|---|
| `2g:default`, `2g:tuned` | `wan.sh` at 2 Gbit/s on both VMs, with a one-way delay of 0.05 ms and the tbf's queue set to max(50 ms, r_R) | `wan.sh` at 2 Gbit/s, one-way delay (r_R − r0_E)/2 + 0.05 ms, queue max(50 ms, r_R) | EXP-13's rows are judged at 2 Gbit/s. The same shaper and queue on both, so the pairs differ only in the path |
| `own:default`, `own:tuned` | no tree: Debian's qdisc and the path's own rate | `wan.sh` at C_R, one-way delay (r_R − r0_E)/2, queue max(50 ms, r_R) | DR-rate: what a real path carries. E checks the emulation at that rate too |

- **The 0.05 ms on R.** netem orphans packets only when a delay is set (EXP-13, the emulated WAN). With a delay set, R's tbf sees the same traffic as E's, and drops at the shaper the same way. It adds 0.1 ms to each round trip at R's `2g` cells, and E's `2g` tree adds the same, so the two still match. r_R itself is measured with no tree.
- **The queue.** `wan.sh` computes the tbf's queue from the delay. It gains an override, so R's shaper has E's queue.
- **r0_E** is E's own ping minimum with no tree, about 0.1 ms. So E's ping minimum matches R's.
- **C_R** is the largest steady rate R's set-up link tests carried (below), rounded down to 0.1 Gbit/s. E's own-rate tree uses it. If E's tree at r = 0 can't pass 0.9 × C_R with 16 tuned streams (its set-up), E's own-rate cells are flagged "emulator-limited", as EXP-13's A12.
- **Host settings:** EXP-13's `default` (Debian's `tcp_rmem` and `tcp_wmem`) and `tuned` (both maxima at 128 MiB), set, guarded and reset as in EXP-13. If EXP-13's A21 adds a third setting, it runs here too, at 2 Gbit/s only.
- **Nothing is added on E:** no loss, jitter or reordering. Whether leaving them out matters is the question.

### The control path, and what R's clock holds

- On R the harness's control path (port 2222) crosses the real WAN. It stays in the plain band at `2g` and is unshaped at `own`, but each control command costs real round trips.
- **The sender's launch is inside the clock** for v1's arms and the `parcopy` arms, as in EXP-13. On E it crosses about 0.1 ms; on R it costs at least one r_R (an SSH channel's open and its command).
- **`launch_s`** (R and E): from the clock's start on dest to the sender's start on the source. The sender's start is stamped on the source's clock, and the offset between the two clocks comes from `agent.py clock-probe` at each visit, with `chronyc`'s offsets recorded beside it.
- **The reading compared:** R's `total_s` less `launch_s`, against E's `total_s`, for arms launched over the control path. `full` is launched on dest and needs no correction. Beside it, R's `total_s` against E's `R_trig` (`total_s` plus one r_R). If the two give different outcomes, the outcome is flagged "launch-sensitive".
- **The visit's control no-op check** compares R with R's own first visit, not with E.

### Data, trials, repeats and order

- pgbench scale 70 (2.1 GB) with EXP-01's `events` table and seed, as EXP-09 to EXP-13. Two trials, uniform 1% first, then hot 1%: EXP-13's deciding trials.
- **R's set-up, once per session before the sweep:** 200 pings; link tests from source to dest of 10 s each (the last 3 s steady) with 1, 8, 16 and 32 streams, on both host settings, unshaped; one stream from dest to source on both; pings during an 8-stream test on each setting. They give r_R and C_R, recorded before E's sweep starts.
- **E's set-up:** the same tests at r = 0 and then through the `own` tree at r_R and C_R.
- **5 repeats.** Each repeat visits the four conditions in a seeded random order, and each visit runs its arms in a seeded random order (seeds 151 and 152, one per trial). The same seeds run on both pairs, so the pairs run the same arm at about the same time. E's sweep starts once R's set-up has fixed r_R and C_R, about 15 minutes after R's.
- **Every arm runs in every repeat at every condition where it runs.** No baseline is pooled across trials.
- **Each visit,** on both pairs: EXP-13's checks, adapted on R (X7); a one-stream link test (6 s, the last 3 steady). **Each trial's first visit to a condition:** 8 streams (and 16 and 32 at `own:default`), one stream from dest to source, and pings during an 8-stream test.
- **Cache state, durability, verification and timeouts:** EXP-13's, unchanged: caches dropped after set-up, v1's B a fresh copy of B0, a stopped source for v1 and the `parcopy` arms, a running idle one for `full`; checks (a) and (b); A13–A15.

### Measured

Everything EXP-13 measures in each run (its clock, the `ss` sampler, `nstat`, pgdelta's protocol timers with their busy and idle parts, the qdisc counters where a tree is set, durability and verification), plus:
- **R's path:** the visit's pings; pings under an 8-stream load; retransmissions in every run. At `own` there is no shaper on R, so every retransmission is the path's or the VM's.
- **`launch_s`** and the clocks' offsets, in every run.
- **The pair speed:** on all four VMs, at the session's start and end, pgdelta's hash (BLAKE2b, as v1) over a fixed 1 GiB in memory at 4 workers, 3 times, timed. It shows whether R's and E's machines differ in speed.
- **Egress:** R's source-to-dest bytes at the interface, for the cost.

## Arms

| Arm | What runs | Conditions | Runs per trial and pair |
|---|---|---|---:|
| **`v1`** (`pgdelta_direct_inplace`) | EXP-13's v1: 4 jobs on each end, window 4, in place, stopped source, in clear | all four | 20 |
| `v1_w1`, `v1_w16` | v1 with `--window 1` or `--window 16` | all four | 40 |
| `full` | one `pg_basebackup` stream, as EXP-13: the copy Omni documents for a DR re-seed | all four | 20 |
| `parcopy8` | EXP-11's bound, unchanged | all four | 20 |
| `parcopy16`, `parcopy32` (added) | the bound with 16 or 32 streams | `own:default` | 10 |
| `full_nosync` | negative control, never a baseline | `2g:default`, repeat 1 | 1 |
| **all** | | | **111** |

So 222 runs per pair and 444 per session in the sweep.

- **Why `parcopy16` and `parcopy32` at `own:default`.** At 50 ms, 8 default windows carry about 3.3–4.0 Gbit/s and 16 about 6.7–8.0, both possibly below C_R. A window-limited bound would favour v1 (EXP-13's A9). 32 windows carry 13–16 Gbit/s. At `2g`, 8 default windows already carry more than the link up to 60 ms, and at `own:tuned` 8 tuned windows carry far more than C_R.
- **The window arms run everywhere.** On a real path the round trips are real, so the window's effect on them is what D29 needs. The `own` cells are their best case: the faster the send, the more round trips weigh.

### Considered and not run

| Option | Why not |
|---|---|
| A round trip near 120 ms (a region outside North America) | K3 allowed one session outside `us-central1`; another region is another owner decision. Egress from North America to Europe costs about 2.5 times as much (about 51 USD per TiB). Proposed later if EXP-15's emulation stands and EXP-13's 120 ms rows come to decide something |
| More region pairs, or more round trips | One pair at r_R checks the emulator. A sweep across paths is a different experiment |
| An anchor on E (r = 0) | It would give Δs and R1 on E. But R has no zero-delay condition, so R1, L1 and L2 can't be judged on a real path. They stay EXP-13's |
| Uniform 10%, the fallback (`v1_hc`), TLS, one source worker, 8 jobs, `probackup8`, the SSH probe | EXP-13 measures them at its emulated cells. None depends on the path in a way the agreement on v1 and the copies wouldn't show. TLS adds a handshake round trip, which the emulator delays like any other, and CPU, which the path doesn't change. Without the fallback, EXP-13's A5 can't be firm (EXP-13's revision says so) |
| Loss, jitter or reordering added on E | The question is whether leaving them out matters. R measures what the path has |
| External IP addresses | Traffic over them is capped at 3 Gbit/s per flow. EXP-13 uses internal addresses, and so does this |
| R and E one after the other | At the same time, the time of day and the network's state are the same for both. The quota allows exactly four VMs |
| A live source, data beyond RAM, persistent disks, GKE | T8, T9 and T9-PD, OMNI-2 |

## Baselines and ratios

A cell is (pair, trial, condition). Everything below uses runs that pass verification, durability and the network checks (X7), on medians of 5 runs, with the maximum beside them (rule 6).
- **B_fast(c),** rule 3's fastest durable full copy measured at c: the fastest of `full`, `parcopy8`, and `parcopy16` and `parcopy32` where they ran. Ties within 0.05 s go to the lower maximum.
- **B_rel(c):** one `pg_basebackup` stream (`full`). `probackup8` doesn't run here, so B_rel is also F0, what Omni runs today.
- **`R_fast`** = v1's median over B_fast's, and **`R_rel`** over `full`'s, per trial (no pooling). On R, v1's and the `parcopy` arms' medians are of `total_s − launch_s` (the launch-sensitive view beside).
- **Robustness views:** `R_max` (maxima) and the launch view on R.
- **EXP-13's verdict rows,** applied at r_R exactly as EXP-13 registers them (rows 1–5, with "v1 not durable" and "Not judged" first, and its flags), on each pair. R1 isn't judged (no anchor), so no row carries "provisional: R1 fired" here; EXP-13's own R1 applies to EXP-13's rows.

## Predictions

**Planning values, at r = 50 ms and C_R = 10 Gbit/s.** Before session 1, `analysis/exp13_model.py` recomputes every range below at the probe's median r_R, with the model's other inputs as EXP-13 registers them (W = 2.6–3.15 MB per default stream, about 64 MiB tuned; 240 MB/s of payload at 2 Gbit/s). Those ranges are recorded here, dated, before any arm runs, and they are the ones judged. The planning values stay beside them. The own-rate ranges are recomputed again at the session's C_R and reported beside, not judged.

| ID | Prediction | Basis |
|---|---|---|
| P1 | **The probe** picks a West Coast region, `us-west2` or `us-west1`, with r_R 40–55 ms | Great-circle distance from Iowa, with a fibre route 1.5–2 times as long; not measured |
| P2 | **The path at rest:** ping median within 0.5 ms of the minimum, p99 within 2 ms, no loss in 200 pings; on each session day within 3 ms of the probe | A provider backbone between two regions |
| P3 | **One stream at 2 Gbit/s:** default 0.42–0.50 Gbit/s on R and on E, within 10% of each other, with `rwnd_limited` + `sndbuf_limited` at least half of its busy time on both (EXP-13's E15); tuned at least 1.8 Gbit/s on both | W / r_R |
| P4 | **The path's own rate C_R:** 6–16 Gbit/s, with 32 default or 8 tuned streams; the lower of the VM's 16 Gbit/s egress cap and the inter-region quota bounds it. One default stream at its own rate: 0.42–0.50 Gbit/s; one tuned stream at least 5 Gbit/s | The per-VM cap ([Omni notes](../research/notes/alloydb-omni.md)); 64 MiB / 50 ms ≈ 10.7 Gbit/s per tuned stream |
| P5 | **Under load.** At `2g`, pings during an 8-stream tuned test rise alike on R and on E (the same tbf queue). At `own:tuned`, E's pings rise by up to max(50 ms, r_R), its tbf's queue, and R's by less than 10 ms; R's tuned bulk runs at `own` retransmit more than E's | A provider's backbone isn't expected to hold a 50 ms queue for one VM's flows; CUBIC then finds the rate by loss there |
| P6 | **Times at `2g`** (R's corrected, and E's): v1 hot 3.2–5.2 s default and 3.1–4.9 s tuned; v1 uniform 1% 12.9–16.0 s default and 5.8–7.4 s tuned; `full` 34–44 s default and 10.8–12.0 s tuned; `parcopy8` 9.5–10.4 s on both | EXP-13's E4–E7, scaled from 60 ms to 50 by its model |
| P7 | **Ratios at `2g`:** `R_fast` hot 0.31–0.55 default and 0.30–0.52 tuned; uniform 1% 1.24–1.68 default and 0.56–0.78 tuned. So **row 2** on R and on E. `R_rel` hot 0.07–0.15 default and 0.26–0.45 tuned; uniform 1% 0.29–0.47 default and 0.48–0.69 tuned | P6. EXP-13 expects row 1 at 20 ms and row 2 at 60 ms (its E13), so R is expected to confirm EXP-13's 60 ms row and not its 20 ms row |
| P8 | **The emulation stands at `2g`** (X2) on both host settings, in both sessions | The same shaper and queue on both pairs; at 2 Gbit/s the real path has room to spare |
| P9 | **The path's own rate.** v1 hot 3.1–4.9 s default and 3.0–4.7 s tuned; uniform 1% 12.9–16.0 s default and 3.8–5.5 s tuned; `full` 34–44 s default and 4.2–5.6 s tuned; B_fast 2.5–3.5 s on both. `R_fast`: hot 0.9–2.0 default and 0.9–1.9 tuned; uniform 1% 3.7–6.4 default and 1.1–2.2 tuned. So **Out at the path's own rate** against the fastest copy measured (X6). `R_rel`: hot 0.07–0.15 and uniform 0.29–0.47 on default hosts; hot 0.54–1.12 and uniform 0.68–1.31 tuned | v1's fixed work (EXP-11: about 3.1 s unthrottled for local changes) plus E6's latency; a 32-stream or tuned 8-stream copy at about 1.25 GB/s, above the disk's durable write floor (1.30 s for 2.1 GB, EXP-11); one default stream window-bound |
| P10 | **The window** on R at `2g:tuned` and `own:tuned`: `v1_w16` − v1 −0.3 to +0.1 s; `v1_w1` − v1 0 to +1.5 s (hot) and 0 to +0.8 s (uniform 1%); each within 0.15 s of E's | EXP-13's E9, scaled to 50 ms |
| P11 | **Pair speed and launch:** the hash benchmark within 5% between R's and E's VMs; on R, `launch_s` is 1–3 round trips plus about 0.05 s, on E under 0.1 s | The same machine type in one family; an SSH session over a multiplexed connection plus the interpreter's start |
| P12 | **Durability and verification:** every durable run passes; `full_nosync` fails check (a) by at least 1.5 GB on both pairs | EXP-11, EXP-13 |
| P13 | **Egress:** 0.4–0.6 TB from R's source to R's dest per session, about 8–12 USD at list price | The bytes under [Run plan and cost](#run-plan-and-cost) |

## Decision rules

Every rule is judged per session, then read across the two sessions (X4 and X5 need both).

| ID | Condition | Consequence |
|---|---|---|
| **X1** | **Runs, and Not judged.** (i) Before session 1: no candidate passes the read-first checks, or none has a probe median in [30, 60] ms with no loss. (ii) On a session day: R's median outside [30, 60] ms, or any loss in 200 pings. (iii) A deciding cell (v1 or B_fast's arm, hot or uniform 1%, at `2g` on either host setting, on R or on E) is not measured: more than 1 of its 5 runs excluded by X7, or none counts | (i) EXP-15 doesn't run. It is recorded here; EXP-13's DR rows keep "emulated WAN"; the owner is told. (ii) The session doesn't run: an infrastructure fault, another day. (iii) X2 and the rows at that host setting are Not judged in that session. Stated; nothing rerun |
| **X2** | **The emulation stands at r_R,** per session, at `2g`: (a) at each host setting, for both hot and uniform 1%, R's `R_fast` within ±10% of E's (\|R / E − 1\| ≤ 0.10); (b) EXP-13's rows, read from both host settings as EXP-13 reads them, give the same row on R and on E; (c) on default hosts, R's one-stream steady rate (the visits' median) within ±10% of E's | All three hold: "stands at r_R". Any fails: "differs", with which test, at which host setting, by how much, and its direction (R's ratio above or below E's). Flags: *near the line* (a ratio within ±0.03 of 0.90 or 1.00 on either pair, or \|R / E − 1\| between 0.08 and 0.12); *fragile* (`R_max` gives another outcome); *launch-sensitive*; *pair-sensitive* (X8); *path moved* (X1) |
| **X3** | **The emulation at the path's own rate:** X2's three tests at `own:default` and `own:tuned` | Reported. It decides only how EXP-13's 5 Gbit/s reading (its A18) and X6 are read. Where it "differs", R's retransmissions, pings under load and `ss` readings beside E's say what the emulator leaves out |
| **X4** | **Which EXP-13 rows it confirms.** Needs X2 "stands" in both EXP-15 sessions, unflagged. Let row_R be the row R gives at r_R, the same in both sessions | **The 20 and 60 ms rows:** EXP-13's row at 20 ms, and at 60 ms, is confirmed where it equals row_R in both EXP-13 sessions. If EXP-13's 20 and 60 ms rows differ, only the one equal to row_R is confirmed, and the change of row is placed between the other round trip and r_R. A confirmed row reads "emulated WAN, confirmed on a real path at r_R ms (EXP-15)". **The 120 ms rows** are never confirmed here. **A2 (D36):** confirmed where row_R is row 2 and, on R's default hosts, v1's sending socket in uniform 1% is `rwnd_limited` + `sndbuf_limited` for at least half its busy time, in both sessions. **A3's named fix:** confirmed where R's and E's parts at r_R (idle waits, fixed round trips, sending, residual, taken absolute, since neither pair has an anchor) agree within max(0.1 s, 10%) each, and their largest part is the one EXP-13's A3 named at 60 ms. **A4 (D29):** confirmed where A4's thresholds, read on R at `2g:tuned` and `own:tuned`, give the same outcome as on E and as EXP-13's at 120 ms tuned. **E3's link-test model at r_R:** confirmed where R's 1-, 8- and 16-stream rates at `2g` and at `own:default` are within 10% of E's |
| **X5** | **What it overturns.** (i) X2 "differs" in both EXP-15 sessions: (a) or (c) at a host setting, or (b). (ii) X2 stands, but row_R equals neither EXP-13's 20 nor its 60 ms row. (iii) The two EXP-15 sessions differ on X2 or on row_R | (i) Every EXP-13 DR row that reads that host setting (every row, if (b) failed) stays provisional: "the emulation differed from a real path at r_R ms (EXP-15)". If R's ratios are higher than E's (worse for v1), no DR claim for v1 rests on those rows, at any r; X3's readings say what the emulator leaves out, and a revised emulation of EXP-13's deciding cells is proposed for the queue. If lower, EXP-13's rows are conservative for v1, still provisional. (ii) EXP-13's 20 and 60 ms rows stay provisional: "a real path between them gave row n". row_R is reported as a real-path row at r_R. (iii) Every outcome is provisional, with both sessions' readings; a third session is proposed to the owner |
| **X6** | **DR at the path's own rate,** per session, on default hosts first and tuned beside, as EXP-13 reads them. **Against the fastest copy measured** (rule 3): `R_fast` ≤ 0.90 for hot and uniform 1% "holds"; ≥ 1.00 for either "Out"; otherwise "Undecided". **Against one `pg_basebackup` stream:** the same bands on `R_rel` | Each reading names its yardstick. **Out against the fastest copy, in both sessions, unflagged:** "DR first" holds only on DR links slower than this path's own rate. The ledger's positioning states the link rate with it: the crossover between 2 and 5 Gbit/s at 60 ms from EXP-13 (its A18, interpolated, labelled), beside C_R here. DR-rate becomes "measured on one real path". The reading against one stream is stated separately. **Holds in both:** "DR first" holds on this path at its own rate, against the fastest copy measured, on these VMs. **Otherwise:** both readings kept, provisional. Flags: *near the line*; *window-limited bound* (B_fast's arm's link test below 0.9 × C_R); *quota-limited* (C_R within 10% of the inter-region quota read first); *emulator-limited* applies to E only |
| **X7** | **Validity,** EXP-13's rules adapted to a real path. **Durability, verification, timeouts:** EXP-13's A13, A14 and A15, with A15's bounds four times the upper ends recomputed at r_R (at least 60 s). **On R, each visit:** 20 pings with minimum in [r_R − 0.5, r_R + 2] ms and no loss; at `2g`, the tree parses as intended. **On R, each run:** the sending sockets' `minrtt` in [r_R − 0.5, r_R + 3] ms; at `2g`, netem's own drops 0. **On E:** EXP-13's checks at r = r_R | A visit that fails is retried once, before any run; a second failure stops the trial as an infrastructure fault ("path changed"). A run that fails is excluded and reported; more than 1 of 5 at a cell leaves it not measured (X1 (iii)). Retransmissions are recorded, not judged |
| **X8** | **Pair speed:** the hash benchmark's medians differ by more than 5% between R's and E's VMs (source with source, dest with dest) | Any X2 or X3 difference above 10% is flagged "pair-sensitive", and X2 (a) is also read on v1's network parts alone (A3's idle waits, fixed round trips and sending), which the machines' speed doesn't set |

### Which EXP-13 rows it confirms or overturns

| EXP-13 item | Confirmed here when | Stays provisional, with the reason, when |
|---|---|---|
| DR rows at 20 ms (default; tuned since its revision's item 15) | X2 stands in both sessions, and row_R equals EXP-13's 20 ms row in both of its sessions | X2 differs (X5 (i)), row_R differs (X5 (ii)), or the sessions differ (X5 (iii)) |
| DR rows at 60 ms | The same, with its 60 ms row **(expected)** | The same |
| DR rows at 120 ms | Never here | Always: "emulated WAN; emulator checked at r_R ms" |
| A2: D36 contradicted for DR on default buffers | row_R is row 2, and R's `ss` shows the window | R gives row 1 on default hosts, or R's `ss` doesn't show the window |
| A3's named fix | R's parts agree with E's, and name EXP-13's part | Another part is largest on R |
| A4: the window (D29) | R, E and EXP-13 give the same A4 outcome | They differ; the default stays 4 |
| E3's link-test model | R's rates within 10% of E's | Otherwise |
| A18: 5 Gbit/s at 60 ms | X6 points the same way | X6 points the other way |
| L1, L2 and R1 | Not checked: a real path has no zero-delay anchor | – |

### Rule 11: which outcomes may pick a single route

- **X4's confirmations** may pick a route only with both EXP-15 sessions and both EXP-13 sessions agreeing, unflagged (EXP-13's rule 11 section). The route is then firm for the conditions measured: n2-standard-8 VMs, local NVMe, Debian's TCP defaults or the tuned maxima, 2.1 GB, v1's stopped-source path at 4 jobs, idle, in clear, one region pair at r_R. Used for Omni's DR (GKE nodes, persistent disks, a live primary, other region pairs), it states those conditions and stays provisional for them until T8, T9-PD and OMNI-2.
- **X6's "Out" or "holds"** may change the positioning only with both sessions agreeing, unflagged, under the same conditions. Otherwise both readings are kept, and "DR first" stays as the ledger has it, provisional.
- **X5's outcomes** keep EXP-13's rows provisional. They withhold claims and close no route, so they apply after one session.
- **X1, X3, X7 and X8** are validity rules and readings. They pick nothing.
- **A D-row grade** that moves on this evidence (D36, D29) moves as EXP-13's rules say, recorded "provisional (rule 11)" until firm.
- **K10.** One session is never firm here. The shift between the two sessions is reported per arm and pair.
- **K3 and two sessions.** K3 allowed "one session outside `us-central1`". K1 and K12 ask two sessions per test, on different days. This design runs two, and asks the owner to confirm before session 2 (Q1). Without the second session, every outcome here stays provisional, and so do EXP-13's DR rows.

## Limitations, known before running

- **One region pair, one round trip.** Paths differ between region pairs, and a path can change between days. 120 ms isn't checked.
- **A provider backbone.** Omni's DR between other clouds, or to a data centre, crosses other networks.
- **VMs, not GKE nodes;** Debian's TCP settings or the tuned maxima; local NVMe; 2.1 GB in RAM; v1 and the bound read a stopped source; the source idle; everything in clear.
- **The launch correction** rests on the clocks' offset, good to about a millisecond on a symmetric path, and worse on an asymmetric one.
- **Pair speed** between R and E is measured (X8), not removed.
- **The inter-region quota** may set the own rate, not the path (flagged).
- **Two trials only** (hot and uniform 1%); no uniform 10%, fallback, TLS or one-source-worker arm here.
- **E reproduces R's minimum round trip, not its jitter or loss.** That difference is what X2 and X3 measure.
- **Two sessions, two days.** Their shift is measured, not removed.

## Smoke run

Before the sweep, in each session, on all four VMs.

| # | Item | Record | Pass | If it fails |
|---|---|---|---|---|
| 1 | **Reach across regions:** the Mac to R's dest over SSH; R's dest to R's source on port 2222 (the control path) and on the data ports, over internal addresses | `run.json`: `control`; the set-up's connect probes | all connect | the region is unusable: X1 (i) or (ii) |
| 2 | **R's `2g` tree:** `wan.sh` with a one-way 0.05 ms and the queue override parses as intended; the band counters show the control path in the plain band; the ping minimum is r_R + 0.1 ms, ±0.5 | trial record: `visits[].tree`, `visits[].pings` | as intended | fix `wan.sh` or the override |
| 3 | **E's trees at r_R and at C_R:** EXP-13's smoke items 1–6 at r_R; 16 tuned streams at r = 0 through the `own` tree pass 0.9 × C_R | trial record: `visits[]`, `first_visit.link_tests[]` | as EXP-13 | E's own-rate cells flagged emulator-limited |
| 4 | **Launch timing:** 10 clock probes between each pair's VMs spread under 1 ms; `launch_s` for v1 and `parcopy8` at scale 5 is 1–3 round trips plus up to 0.1 s on R, under 0.1 s on E | run records: `launch_s`, `clock_offset` | yes | fix the stamp or the probe before the sweep |
| 5 | **Pair speed:** the hash benchmark on all four VMs | `run.json`: `pair_speed` | recorded | – |
| 6 | **Every arm at every condition on both pairs,** scale 5, 1 run | run records: `verification`, `durability`, the network fields | all pass | as EXP-13 |
| 7 | **Scale 70,** uniform 1%, `2g:default`, every arm once on both pairs: the time projection and the egress counters | run records: `totals.total_s`, wire bytes | – | a projection above 5 h of the four VMs is searched for a fault, as EXP-13's revision does; the session runs uncut otherwise |
| 8 | **Idle noise** for check (a) on both dests: EXP-13's item 18 table | `run.json`: `dirty_idle_noise` | as EXP-13 | as EXP-13 |
| 9 | **`analysis/exp15.py`** end to end on the smoke records | its output directory | runs | fix |
| 10 | **Clean-up:** `wan.sh off` restores Debian's qdisc on all four VMs | `run.json`: `teardown` | yes | fix |

## Run plan and cost

**Before session 1, once:** the read-first checks (no VMs); the probe (about 15 minutes, at most 10 vCPUs, about 1.25 instance-hours of `e2-small`, under 1 USD); the region, r_R and the recomputed predictions recorded here, dated.

**Per session,** four n2-standard-8 VMs at once (32 vCPUs, the whole quota):

| Step | Runs | Wall time |
|---|---:|---:|
| Create the four VMs in two regions; provision (`startup.sh`, `install_incumbents.sh`); deploy; SSH set-up | – | 25 min |
| R's set-up (pings, link tests at 1, 8, 16 and 32 streams on both settings); E's set-up at r_R and C_R | – | 15 min |
| Smoke run | about 70 (both pairs) | 40 min (30–50) |
| Sweep: uniform 1% and hot, four conditions, 5 repeats, both pairs at once; R sets the pace | 444 (222 per pair) | 100 min (90–115) |
| Fetch from all four, then `wan.sh off` and teardown | – | 10 min |
| **Total** | **about 515** | **about 3.2 h (2.9–3.6)** |

- **Instance-hours:** about 12.8 per session (11.3–14.3); about 26 for the two sessions (23–29), plus the probe.
- **Inter-region egress, R only:** per session about 110 GB in hot and 145 GB in uniform 1% (the copies' 2.12 GB each, v1's 29 MB or 609 MB), about 80 GB of per-visit link tests, about 85 GB of first-visit and set-up tests, and about 30 GB in the smoke run: about 0.45 TB, 0.4–0.6 TB with the own rate's spread. At about 20 USD per TiB, 8–12 USD per session.
- **Cost:** at about 0.5 USD per instance-hour, about 6.5 USD of VMs per session. With egress, about 14–19 USD per session, and 30–40 USD for both, with the probe.
- **When.** In FIRMNESS.md's schedule, wave 5, alone, since it takes the whole quota. Session 2 on another day, also alone. After EXP-13's two sessions, so X4 can read them; if it runs first, X4 is read once they exist.

## What it needs built in the harness

About 1.5–2 days, after EXP-13's revision's additions (it shares their per-pair configuration).

| Block | Files | What |
|---|---|---|
| Zones per VM | `infra/gcp/config.sh`, `create.sh`, `teardown.sh`, `experiments/network/remote.py` | A zone for each VM of a pair, so R's dest can sit in another region. `remote.py` describes each VM in its own zone (today one `ZONE` serves both) |
| Read-first checks | `infra/gcp/` (a script) | The quotas, the default network's subnets and firewall rules, read and printed; nothing changed |
| The probe | `infra/gcp/` (a script) | Create the labelled `e2-small` VMs, ping and connect over internal addresses, print the figures, delete the VMs |
| `wan.sh` | `infra/gcp/wan.sh` | An override for the tbf's queue (in ms), so R's shaper can have E's queue at a 0.05 ms delay |
| Real-path conditions | `experiments/network/run_exp04.py`, `measure.py` | `own` conditions with no tree; visit and run checks against the measured r_R (X7); conditions at a measured r_R and C_R for E, written into E's run line from R's set-up and recorded |
| Launch timing | `agent.py`, `run_exp04.py` | The sender's start stamped on the source; a clock probe at each visit; `launch_s` and the offset in every run record |
| Arms | `measure.py` | `parcopy32` in `PARCOPY_VARIANTS`; A15's bounds from the ranges recomputed at r_R |
| Pair speed | `agent.py` | A hash benchmark over a fixed 1 GiB in memory at 4 workers |
| Pings under load | `run_exp04.py`, `agent.py` | Pings during an 8-stream link test, at set-up and first visits |
| Analysis | `analysis/exp15.py` (new) | Per pair and session: ratios, EXP-13's rows at r_R (its code reused), X1–X8, the network table (pings under load, retransmissions, `ss`, link tests against the model); X4 and X5 against EXP-13's two sessions' cells; the two-session reading |
| Tests | `tests/` | Each block, and the rules on synthetic medians, every outcome of X2, X4, X5 and X6 reachable |

## Questions for the owner

- **Q1. Two sessions outside `us-central1`?** K3 allowed one session outside `us-central1`; K1 and K12 ask two per test, on different days. This design asks for two. Without the second, every outcome here, and every DR row of EXP-13, stays provisional (K10).
- Nothing else blocks it. The region is picked by the probe, under the rule above. The quotas are read, not raised (K2 is open).
