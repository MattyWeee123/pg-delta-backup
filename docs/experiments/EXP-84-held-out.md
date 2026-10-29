# EXP-84: The held-out session (rule 15): --crashed-standby, --selector and page-lsn on the sealed settings

Status: **pre-registered October 29, 2026, before any run.** *Amended before any scored data:* the first launch stopped at set-up on two faults:
- `infra/gcp/vmset.sh` still allowed only local-nvme and pd-balanced, so `hs` couldn't be created. It now takes pd-ssd, counted toward the same SSD quota.
- The pipeline's helper called `timeout` on a shell function, so `ha` was created but never prepared.

Both are fixed, `vmset.sh` is added to the recorded files, and the files are recorded again at the fixed commit. The `ha` VMs were kept and are prepared afresh. Two VM sets at once (32 vCPUs, the quota), about 3.5 hours. It is the first held-out session. [held-out-workloads.md](../research/held-out-workloads.md) says one is owed before `--crashed-standby`, `--selector` or page-lsn becomes a default.

## Question

**Do the three candidates' claims, made on the tuned setting, hold on settings never used to tune or decide?** The tuned setting was EXP-48's 18 GB database, PostgreSQL 17.11, pd-balanced, and the hot, uniform and scatter cells.

| Candidate | Its claim on the tuned setting | Evidence |
|---|---|---|
| `--crashed-standby` | A crashed standby is refreshed correctly by the fast path, at the clean standby's cost | EXP-80, EXP-80b |
| `--selector` | Its time model predicts the chosen fill's time, and says when `pg_basebackup` is faster | EXP-74, EXP-75 |
| page-lsn | Correct, and as fast as a re-clone (both read all of S) | EXP-82, EXP-82b, EXP-82c |
| (the fast path's defaults) | Faster than `pg_basebackup` below about 91% of the pages | EXP-79b |

## The sealed settings, as [held-out-workloads.md](../research/held-out-workloads.md) defined them

| Setting | What differs | Cells | Built at first use (this registration) |
|---|---|---|---|
| **H1** | The workloads: `clustered`, `tpcb`, `append` and `retention` (`source_cluster.py`'s patterns), at scale 600 on the ha set | 4 | The lab's `FULL_CHECK=1`: each copy's check also compares `events` (its count and the sum of its ids) and pgbench's branches, tellers and history, which these patterns change |
| **H2** | Scale 1,200 (about 36 GB, 4.5x the VMs' memory), hot and uniform | 2 | — |
| **H3** | A pd-ssd data disk instead of pd-balanced, hot and uniform at scale 600 | 2 | `create.sh` takes `DATA_DISK=pd-ssd`; the `hs` set |
| **H4** | PostgreSQL 18 (18.6, PGDG's build), hot and uniform at scale 600 | 2 | The lab's `PG_MAJOR=18` (installed beside 17 on the `hc` set) |

## Design

**One lab run per setting** (`experiments/network/lab_exp64.sh`, as EXP-80b to -82c):
- **`CRASHED=1`:** B0 is a standby killed 30 s into the change.
- **`STANDBY_TOO=1`:** B0s is a standby stopped cleanly before the change.
- **`SERIES=1`**, and summaries on, as the tuned setting had them.

**The arms, one run each per cell,** in a shuffled order:

| Arm | Old copy | What runs |
|---|---|---|
| `fast8wf2c` | B0 (crashed) | The fast path's defaults with `--crashed-standby` |
| `sb_fast8wf2` | B0s (clean) | The fast path's defaults |
| `sb_fast8ws` | B0s | `--selector`: it chooses the fill, and predicts each option's time and `pg_basebackup`'s, with its pd-balanced disk limits (no others passed) |
| `sb_lsn` | B0s | `--fast-path page-lsn` |
| `full` | — | `pg_basebackup` |

**One run per arm, not two.** A held-out session asks whether large effects transfer. The bars below sit far outside rule 17's spread (2.40% for runs of 10 s or more), and a miss narrows a claim rather than deciding a close race.

**The schedule:**
- **Set `ha`** (pd-balanced, n2d-highcpu-8, us-west1 first): H1 (seed 840), then H2 (`SCALE=1200`, seed 841).
- **Set `hs`** (pd-ssd), at the same time: H3 (seed 842), then torn down.
- **Set `hc`** (pd-balanced), with PostgreSQL 18 installed: H4 (`PG_MAJOR=18`, seed 843).
- **Each set** first runs a smoke at scale 20 (seed 849), deciding nothing: every run exits 0, every copy matches, and the fast-path arms used it.

**The analysis,** committed before the data: `analysis/exp84.py`, dry-run on EXP-80b's runs, where it scores nothing because none of these arms exists there. Data `docs/experiments/data/exp84-cells.csv`, figure `exp84-held-out`.

## Predictions

Per cell, from the tuned results. **A miss narrows that candidate's claim to the cells where it held, and is never tuned away** (rule 15).

1. **`--crashed-standby`:** in every cell it takes the fast path (proof "crashed standby"), the copy is correct, and it takes at most the larger of 1.10x and +1.0 s of the clean standby's time.
2. **`--selector`:**
   - **H1, H2 and H4:** its prediction for the fill it chose is within 15% of the measured time.
   - **H3 (pd-ssd):** its pd-balanced limits make it overestimate by at least 15%, an expected miss of transfer. It is predicted, so it counts as held when it happens.
   - **In every cell,** its call on `pg_basebackup` (whether a full copy is predicted faster) matches the measured order of the two.
3. **page-lsn:** in every cell it takes the fast path ("clean shutdown in recovery"), the copy is correct, and it takes at most 1.05x `pg_basebackup`'s time.
4. **The fast path's defaults:** in every cell, faster than `pg_basebackup` exactly where its unfilled plan sends under 91% of the listed pages.
5. **Every copy matches the source:** all four pgdelta arms, in every cell.

**Reported:** every arm's time over `pg_basebackup`'s; each cell's share of pages chosen; the selector's choice and predictions.

## What follows

- **A candidate whose predictions hold in every cell** becomes eligible to be a default, and RESULTS says so; the change itself is a separate, owner-visible decision.
- **A miss** narrows its claim and is written into RESULTS and the candidate's README row.
- **H3's selector result** decides whether its default must measure the disk's limits at the session's start (the idea archive's revisit trigger).
- **Each held-out cell used here is spent** (rule 15) and is replaced by a new one for the next milestone.

## The rules this session runs under

- **Rule 15:** this is the milestone session. Nothing here is tuned on these settings.
- **Rule 16:** `analysis/integrity.py register EXP-84` records the analysis and its helpers, the lab and disk sampler, `create.sh`, and pgdelta's modules.
- **Rule 17:** one run per arm, with bars far outside the noise.

## Workflow check

| | Check | For this session |
|---|---|---|
| 1 | Situation, standard, competitor | Each candidate's claim against its standard: a re-clone, hashing, the fast path itself |
| 2 | The bottleneck | Not the question: transfer of measured effects |
| 3 | Systems or algorithm | Not the question |
| 4 | Papers and prior art | RRSI's and ADRS's held-out lesson (the RSI review) |
| 5 | Committee-grade evidence | Pre-registered; settings sealed since October 28 |
| 13 | The cascade | VMs only: the settings are VM settings |
| 14 | The idea archive | Each candidate's row updated with the result |
| 15 | Held-out workloads | This is it |
| 16 | Scorer integrity | Recorded at registration |
| 17 | The noise bar | Bars far outside the 2.40% spread |
