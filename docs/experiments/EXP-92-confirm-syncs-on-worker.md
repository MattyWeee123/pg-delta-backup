# EXP-92: Confirming hashing with its syncs on a worker, and holding the hashing window to the source's cache

Status: **pre-registered October 31, 2026 (UTC), before any scored run.**

## Why

[EXP-91](EXP-91-hash-ahead-verify-as-written.md) tried two changes to hashing where many pages change (`uniform`, 28% of the pages, 18 GB beyond memory):
- **Each change removed the stall it targeted,** but together they took 0.818x today's hashing (the bar was 0.70). The receiver syncing each file in its main loop then bound: 78.5 s of 151.5 s.
- **A reported arm also put those syncs on the receiver's worker (`hashAVw`).** It took 0.601x today's hashing at full link, 1.13x its slowest resource's own time. At 1 Gbit/s it took 113 s against `pg_basebackup`'s 152 s (0.744): the first hashing run to beat a re-clone where many pages change.
- **That was one run of an arm not registered as deciding,** so it is a hypothesis, not a claim (rule 11). Single hashing runs with a pipelining gap moved up to 5.2% between sessions.
- **Hashing ahead ran too far ahead.** With 64 GiB allowed, the planner ran 11–16 GB ahead of the sending on VMs with 8 GB of memory. The pages sent had left the source's cache, so they were read from disk a second time:
  - the source's disk needed 98–99 s with `hashAVw` (104–107 s with hashing ahead alone or with only the verification), against 88 s;
  - in `hashAVw` the sending waited 40 s (full link) and 52 s (1 Gbit/s) for those reads.

## Question

1. **Does `hashAVw` hold with two runs per arm, in a session of its own?** Deciding: does hashing beat a re-clone at 1 Gbit/s where many pages change?
2. **Does holding the window to what the source's cache keeps remove the second reads,** without bringing back the source's idle?

## What runs (no pgdelta code changes; no default changes)

**The window: `--hash-ahead 2G`.**
- The source VM has 8 GB of memory, and PostgreSQL's `shared_buffers` is 128 MB, so most of it is page cache. That is inferred from the settings, not measured.
- The window counts the file being sent (`pgdelta/sender.py`, `_Planner`). So 2 GiB is that file and about one more 1 GiB file ahead, and the pages a file sends were read for hashing at most about 2 GiB earlier: well under the cache.
- **The risk:** one file of lookahead is about 5 s of the source's disk. When the planner waits for room, the source's disk may idle (EXP-90's stall). F3 tests whether it does. A larger window keeps fewer of the pages cached.

**The lab and the baseline** (opt-in, decides nothing):
- `experiments/network/lab_exp64.sh`: an arm's window may follow its `A`. So `hashA2GVw` is `--hash-ahead 2G`, and `hashAVw` keeps 64 GiB.
- `experiments/baseline/run.sh`: a pattern's own arms (`ARMS_hot`) are passed to the lab, so the hot cell can run fewer arms.

**The arms:**

| Arm | Sender | Receiver |
|---|---|---|
| `full` | `pg_basebackup` | |
| `hash` | hashing, as EXP-90 and EXP-91 | `--fsync main`, `--verify-scope written` (the defaults) |
| `hashAVw` | `--hash-ahead 64G --read-ahead 8` | `--verify-scope as-written --fsync worker` |
| `hashA2GVw` | `--hash-ahead 2G --read-ahead 8` | as `hashAVw` |

**An independent review before registration** found one blocker and seven smaller issues, all fixed before any data:
- **The blocker:** a failed or missing run could pass a prediction, since the analysis averaged whatever runs it found. Cells are now checked for validity (below), and a prediction on an invalid cell is void.
- **F1's mean could hide a bad run:** each run must now pass too.
- **Nothing checked that each arm ran with its options:** the sender's stats are now compared with the arm's.
- **F3's time bar sat inside the noise,** though it chooses the window for a held-out session: it is now "no slower beyond the noise", with a tie rule. Its source-disk bar is now relative to the same session's `hash`.
- **The window counts the file being sent,** so 2 GiB is one file of lookahead, not two. The text and the local window (192 MiB, which held less than one file) are corrected.
- **The campaign's gating half** is now the deciding arm's regime (everything ahead); a half cut short by its time cap has not passed.
- **The VM command and rule 16's file list** are now given in full.

## Design

**Stage 1: the tests.**
- The suite: 1,693 tests, plus 5 new ones.
- **`tests/test_pgdelta_exp92.py` (2):** all three options together, on every engine configuration, with a window smaller than one file (6 pages) and one holding everything (1 GiB); and EXP-32's misplaced-write switch, caught and then repaired.
- **`tests/test_exp92_analysis.py` (3):** a failed, unmatched, missing or misconfigured run makes its cell invalid.
- EXP-91's 17 tests cover each change alone and both together.

**Stage 2: local** (the Mac, PostgreSQL 17.11).
- **The correctness campaign:** EXP-32's randomized fault campaign (`experiments/correctness/campaign.py`), as EXP-91 ran it, with the syncs on the worker added.
  - The flags: `--pgdelta-args "--verify-scope as-written --hash-ahead SIZE --read-ahead 3 --fsync worker"`.
  - **First half:** 500 counted trials with SIZE 2G. The campaign's databases are smaller than that, so everything is planned ahead: the regime of the deciding arm, `hashAVw`.
  - **Second half:** 500 with SIZE 64K, a few files ahead.
  - A misplaced-write control every 20 trials.
  - **Stop rule:** a wrong output, a control not caught, or a failed repair stops it. Each half is 500 counted trials, with at most 2 hours; a half that the time cap stops short of 500 has not passed.
- **The scale model:** `experiments/network/exp92_local.py --scale 100 --repeats 2 --seed 921`.
  - EXP-91's local lab, in the hot and scatter cells, with the arms `hash`, `hashAVw` and `hashA2GVw`.
  - Locally `hashA2GVw` holds 2 GiB too: the same in files as on the VMs (the file being sent and about one more). The 1.6 GB local database fits in it almost whole, so its local ratio says little.
  - Every copy is started and checked.
  - Its ratios are rule 13's proxy pairs. In memory nothing is evicted, so the window's purpose can't show.

**The gate to the VMs (rule 13):**
- Promotion rests on EXP-91's VM measurements: the local model's ratio for these changes had the opposite sign to the VMs' (1.354 against 0.818).
- The VM session starts once the tests pass and the campaign's first half (500 trials) passes.
- The second half may run while the VMs do. A wrong output, a missed control or a failed repair in either half voids the VM session's verdicts.

**Stage 3: the VMs.**
- **The script:** the team's baseline (`experiments/baseline/run.sh`), as in EXP-90 and EXP-91:

```bash
PAIR=ha NAME=exp92 RESULTS=results/exp04/exp92 SEED=920 REPEATS=2 PATTERNS="hot uniform" ARMS="full hash hashAVw hashA2GVw full_1g hash_1g hashAVw_1g hashA2GVw_1g" ARMS_hot="full hash hashAVw hashA2GVw" bash experiments/baseline/run.sh
```

- **The setting:** the same two `n2d-highcpu-8` VMs, 150 GB balanced disks, and PostgreSQL 17.11 at scale 600 (18 GB).
- **The arms:**
  - `uniform`: `full`, `hash`, `hashAVw` and `hashA2GVw`, each also on a link shaped to 1 Gbit/s (`_1g`). Two runs each: 16 runs.
  - `hot`: the same four at full link (`ARMS_hot`). Two runs each: 8 runs. EXP-91 showed hot hashing doesn't use the link (under 1 s of it).
- **Each run:** from a fresh copy of B0, with both VMs' caches dropped, in an order shuffled within each pattern. Every pgdelta copy is started and compared with the source.
- **The sampler:** both VMs every 0.1 s, as in EXP-90 and EXP-91.

**The analysis**, committed before the data: `analysis/exp92.py`.
- It reuses EXP-90's bound arithmetic (`analysis/exp90.py`) and the busy-time view (`analysis/exp90_overlap.py`).
- An arm's time in a cell is the mean of its two runs, and so is each counter. Every run is also reported.
- **A cell is valid when:**
  - it has both its runs, each exited 0 on both sides, and every pgdelta copy matched its source;
  - each run ran with its arm's options, as the sender's stats record them (the window, the receiver's sync mode and its verify scope). A misspelled arm would otherwise run as plain hashing.
- **A prediction that uses a cell that isn't valid is void,** neither held nor failed (`tests/test_exp92_analysis.py`).
- Dry-run on EXP-91's data, it reproduces EXP-91's values.

## Predictions

| # | Prediction | Bar (predicted) |
|---|---|---|
| F1 (deciding) | **Hashing beats a re-clone at 1 Gbit/s where many pages change:** uniform, the mean of `hashAVw_1g`'s runs, and each of them, ≤ 0.85x the mean of `full_1g`'s | 0.85 (0.70–0.79: EXP-91's 0.744, within the 5.2% single hashing runs moved between sessions) |
| F2 | **The gap closes at full link:** uniform `hashAVw` ≤ 0.70x `hash` | 0.70 (0.56–0.65) |
| F3 (the window) | Uniform, at each rate: **`hashA2GVw`'s source-disk bound ≤ 1.04x `hash`'s** (the source reading S once), the sending's wait on the pages' reads **≤ 10 s in all,** and **`hashA2GVw` no slower than `hashAVw` beyond the noise** (≤ 1.0223x) | 1.04 (0.99–1.02); 10 s (under 5); 1.0223 (0.90–0.99) |
| F4 (no harm, and the bound's control) | **Where few pages change, nothing moves:** hot, `hashAVw` and `hashA2GVw` each ≤ 1.03x `hash`. **`pg_basebackup` stays at its bound:** all six runs ≤ 1.10x T_impl | As stated |
| F5 (the mechanism) | Uniform `hashAVw`, full link: the receiver's main loop **waits on its syncs ≤ 10 s,** the source's disk is **busy alone ≤ 5 s,** and **measured / T_impl ≤ 1.20** | EXP-91: 5.5 s, 0 s, 1.130 |
| Correctness | Every cell valid: 24 runs, and every pgdelta copy matches its source (18). No wrong output in the 1,000 local trials, and every misplaced-write control caught | Any miss stops and is reported |

**Reported:**
- each run's time, bound, binding resource and busy-time split;
- **the two runs' spread in each cell,** against rule 17's 2.23%. A cell over it is checked for interference (the samplers' other processes, the disks' counters) before it is scored;
- the sender's planner counters (how far ahead, the waits for room and for B's digests) and the sending's waits on its reads;
- **the cross-session values** against EXP-91's: `full` within 1% for any comparison across sessions, then `hash` and `hashAVw` at both rates;
- the local ratios beside the VMs' (rule 13's proxy pairs);
- **the figures:** `exp92-timelines` (the uniform runs, second by second, each arm's first run) and `exp92-runs` (every run's seconds by which machine was busy, with its bound).

## What follows

- **If F1 and F2 hold:** hashing with these three options beats a re-clone at 1 Gbit/s where many pages change, in a second session with two runs per arm. It still isn't a default or a claim outside the project:
  - before either, a held-out session is owed (rule 15: H9–H12), predicted from this session's results;
  - **the window that goes to it:** 2 GiB only if `hashA2GVw` is faster than `hashAVw` beyond the noise (≤ 0.9777x) at one rate and no slower beyond it at the other. Otherwise 64 GiB, the arm F1 decides on.
- **If F1 fails:** EXP-91's 0.744 didn't reproduce. No claim is made, and the timelines show why.
- **If F3 fails because the source idles** (the planner waiting for room while the source's disk is quiet): try a larger window (4 GiB), or count the window in pages to send rather than files.
- **At full link, at best a tie either way:** all of B is read. B's digests kept from the refresh that wrote B stays the structurally different idea, next in the idea archive.

## The rules this session runs under

- **Rule 13:** the tests, then the local campaign and scale model, then the VMs, as gated above. Both proxy pairs are logged.
- **Rule 14:** the idea archive was read. Its "window held to the cache" row is F3. Structurally different, beside it: B's stored digests.
- **Rule 15:** no default changes and no claim leaves the project from this session, so no held-out session is owed now. One would be before either.
- **Rule 16:**
  - `analysis/integrity.py register EXP-92` records, as of the registration commit:
    - the analysis: `analysis/exp92.py`, and what it reuses (`analysis/exp90.py`, `analysis/exp90_overlap.py`, `analysis/plotting.py`);
    - the VM session: `experiments/baseline/run.sh`, `experiments/network/lab_exp64.sh`, `remote.py`, `util_series.py`, `fio_mixed.sh`, `link_probe.py`, `hashbench.py`, `source_cluster.py`, `experiments/change_patterns/run_study.py`, and `infra/gcp/` (`config.sh`, `create.sh`, `sets/ha.sh`, `startup.sh`, `throttle.sh`);
    - the local stage: `experiments/correctness/campaign.py`, `experiments/network/exp92_local.py` and `exp91_local.py`, `experiments/summaries_off/timing.py` and `trials.py`, `experiments/fastpath_crashed/crashed_standby_trials.py`;
    - the tests: `tests/test_pgdelta_exp92.py`, `tests/test_pgdelta_exp91.py` (whose `heavy_trees` it uses), `tests/test_exp92_analysis.py`;
    - pgdelta's modules (`pgdelta/*.py`);
  - every copy is checked;
  - the negative controls are the misplaced-write switch (the tests and the campaign) and F4's `pg_basebackup` runs, which control the bound arithmetic.
- **Rule 17:**
  - two runs per arm; within a session, two runs of 10 s or more differ by up to 2.23% (EXP-85's noise floor, unchanged by EXP-91);
  - between sessions, `pg_basebackup`'s uniform cell spreads 0.44% over seven sessions;
  - F1's bar is 0.06–0.15 above its predicted range, and F2's 0.05–0.14;
  - a deciding ratio within 0.03 of its bar gets a second session.

## Workflow check

| | Check | For this experiment |
|---|---|---|
| 1 | Situation, standard, competitor | Hashing against a re-clone (`pg_basebackup`) beyond memory where many pages change. EXP-91 measured 0.744x at 1 Gbit/s with the syncs on the worker, in one run; this session confirms it or doesn't |
| 2 | The bottleneck | EXP-91's per-second samples and timers: the receiver's syncs in its main loop (with the worker: gone), then the source's second reads from hashing too far ahead (F3) |
| 3 | Systems or algorithm | At 1 Gbit/s systems, near the floor (0.71–0.83x estimated). At full link algorithm: a bound of at least 88 s against a re-clone's 91 s |
| 4 | Papers and prior art | The fast path's syncs on a worker (EXP-68), reused. The window follows integrated prefetching and caching (Cao, Felten, Karlin and Li, SIGMETRICS 1995): a prefetch that evicts a block needed sooner does harm, so the hashing's read-ahead is held to what the cache keeps |
| 5 | Committee-grade | Pre-registered, with the analysis committed first and dry-run. Two runs per arm, a mechanism check (F5), a bound control (F4), negative controls; predictions inferred from EXP-91 |
| 13 | The cascade | Tests, then the local campaign and scale model, then the VMs. Promotion rests on EXP-91's VM data; the proxy pairs are logged |
| 14 | The idea archive | Read. F3 tests its "window held to the cache" row; B's stored digests is the structurally different idea beside it |
| 15 | Held-out workloads | None owed now: no default changes and no claim leaves. A default would need H9–H12 first |
| 16 | Scorer integrity | Files registered before any data; every copy checked; the misplaced-write switch and F4's bound control |
| 17 | The noise bar | 2.23% within a session; F1 and F2 are 0.05–0.15 from their predictions; a deciding ratio within 0.03 of its bar gets a second session |
