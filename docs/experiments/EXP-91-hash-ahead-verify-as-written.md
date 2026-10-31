# EXP-91: Hashing ahead and verifying as written, where many pages change

Status: **pre-registered October 31, 2026, before any scored run.**

## Why

[EXP-90](EXP-90-where-hashing-time-goes.md) measured, second by second on both VMs, where hashing's time goes at 18 GB with 28% of the pages changed (`uniform`). The run took 187 s, 1.70x its slowest resource's own time:
- **The source stopped reading while a send blocked.** One thread hashes a file and then sends the oldest pending file's pages. While dest wrote those, `send()` blocked (93 s in all), and no file was hashed. The source read nothing for 40 s, then read S's unchanged last 9 GB alone: 28 s of only the source's disk busy, and 7 s of nothing.
- **The verification re-read the disk.** In place, `--verify-scope written` re-reads whole every node with more than an eighth of its pages written: 9,041 nodes. B's reading ahead had evicted them, so 4.86 GB came from the disk, and dest's random reads share one operations budget with its random writes.

At 1 Gbit/s the same run lost to a re-clone (1.37x), though its floor there is 0.71–0.83x one: a systems problem. At full link pipelining can at best tie a re-clone, since all of B is read.

## Question

Do the two changes EXP-90 points to close the gap, and does hashing then beat a re-clone at 1 Gbit/s where many pages change?

## The changes (opt-in; no default changes)

**(a) The sender hashes ahead: `--hash-ahead SIZE`** (`pgdelta/sender.py`, `_Planner`).
- A planner thread hashes S's files, compares them with B's node digests and prepares their leaf requests, up to SIZE bytes of files ahead of the file being sent.
- The sending thread takes the planned files in order. Before each file and each run of pages, it sends the leaf requests the planner queued, so only one thread writes to the connection, and a file's requests always precede its pages.
- The pages a file sends may have left the page cache by then, so they are read on `--read-ahead` threads (8 by default with `--hash-ahead`), as the fast path reads its chosen pages.
- What is sent is unchanged; only the order of the frames on the wire changes.
- EXP-90's write-up proposed this as "the sends on their own thread". It is built the other way round, with the hashing on its own thread, because only that keeps a single writer on the connection.

**(b) The receiver verifies each run as it writes it: `--verify-scope as-written`** (in place; `pgdelta/receiver.py`, `_read_back`).
- Each run of pages is read back from the file right after it is written, from where it was meant to go, while it is still in the page cache, and each page's leaf digest is kept.
- At the file's end, every node is composed from those leaves and B's leaf digests (from the leaf requests, answered before the file was written) for its other pages. Nodes no page was written to keep B's node digests, as with `written`, and the file's digest must equal the source's.
- **What the check catches is unchanged.**
  - **In a node whose leaves were asked for,** every page written differed from B's. A run written elsewhere leaves a page of its own as B had it, so its read-back fails, as the file's digest fails with `written`.
  - **In a node sent whole** (a grown or new file's end, or `--high-churn-node-fraction`), pages may equal B's. A run of like pages, zero pages say, written a page away can read back unchanged and overwrite a neighbour instead. So such a node is checked as by `written`, and the pages on either side of each run that touches one are read again at the file's end. An unwritten node holding such a page is re-read whole.
  - The read-back hashes what the file holds, not what arrived, so a write that changes the bytes fails too.
  - A run that reads back short fails as a verification error.
- **The tests:** both negative controls, the review's misplaced run of zero pages, and EXP-32's misplaced-write switch (`tests/test_pgdelta_exp91.py`).

**An independent review before registration** found two defects, both fixed and tested before any data:
- **(a) hid a hung receiver from the stall watch.** With a live source, the sending thread could wait for the planner while the planner waited, unwatched, for B's digests, holding backup mode open without limit. The planner's wait now counts while the sending thread waits for it.
- **(b) as first built missed the misplaced run of zero pages above,** which `written` catches. The neighbour check above closes it.
- **Two minor findings, also fixed:** a failing run waited up to 10 s for the planner, and the fast path's files were verified page by page.

## Design

**Stage 1: the tests** (`tests/test_pgdelta_exp91.py`, 17 tests):
- both changes on every engine configuration;
- hashing-ahead bounds from one byte to 1 GiB, with and without read-ahead;
- a new copy from a backup, and a live source;
- a planning failure;
- the negative controls: a misplaced write, a corrupted write, a misplaced run of zero pages in a node sent whole, and the misplaced-write switch with and without hashing ahead;
- the stall watch seeing the planner's wait.

**Stage 2: local** (the Mac, PostgreSQL 17.11):
- **The correctness campaign:** EXP-32's randomized fault campaign (`experiments/correctness/campaign.py`) with `--pgdelta-args "--verify-scope as-written --hash-ahead SIZE --read-ahead 3"`:
  - 500 counted trials with SIZE 64K (a few files ahead), then 500 with SIZE 64G (everything ahead);
  - a misplaced-write control every 20 trials.
  - **Stop rule:** a wrong output, a control not caught, or a failed repair stops it. 1,000 counted trials or 2 hours, as in EXP-32.
- **The scale model:** `experiments/network/exp91_local.py --scale 100 --repeats 2 --seed 911`.
  - Arms `hash`, `hashA`, `hashV` and `hashAV` in the hot and scatter cells; every copy started and checked.
  - Its hashAV/hash ratio is rule 13's proxy pair. The Mac holds the database in memory, so it can't show the disk effects this targets, and the VM session isn't gated on it (below).

**Stage 3: the VMs.** The team's baseline (`experiments/baseline/run.sh`) as in EXP-90, with `NAME=exp91 SEED=910`:
- the same two `n2d-highcpu-8` VMs, 150 GB balanced disks, PostgreSQL 17.11 at scale 600;
- the hot and uniform patterns;
- one run per arm, each from a fresh copy of B0 with caches dropped, and its own capacity profile;
- the arms, each also on a link shaped to 1 Gbit/s (`_1g`):

| Arm | Sender | Receiver |
|---|---|---|
| `full` | `pg_basebackup` | |
| `hash` | hashing, as EXP-90 | `--fsync main`, `--verify-scope written` (the defaults) |
| `hashA` | `--hash-ahead 64G --read-ahead 8` | as `hash` |
| `hashV` | as `hash` | `--verify-scope as-written` |
| `hashAV` | `--hash-ahead 64G --read-ahead 8` | `--verify-scope as-written` |
| `hashAVw` | as `hashAV` | as `hashAV`, with `--fsync worker` |

That makes 24 runs. Every pgdelta copy is started and compared with the source. The sampler records both VMs every 0.1 s, as in EXP-90.

**Promotion to the VMs (rule 13):**
- The local scale model can't show these effects: in memory nothing is evicted and no disk binds. Rule 13's log puts local timing outside its band beyond memory (EXP-82, EXP-85).
- So the VM session rests on EXP-90's VM measurements of the mechanism, and is gated by stage 1 and stage 2's correctness, not by a local speed-up.

**The analysis** (committed before the data): `analysis/exp91.py`. It reuses EXP-90's bound arithmetic (`analysis/exp90.py`) and its post-hoc view of which machine was busy and dest's operations (`analysis/exp90_overlap.py`), both now registered.

## Predictions

**Inferred from EXP-90's measurements of the uniform run:**
- **Hashing ahead (A)** removes the 28–35 s of the source alone.
- **Verifying as written (V)** removes 4.86 GB of dest's reads, about 40 s of its operations budget.
- **With both,** dest's work is reading B (87 s) with the 273,000 writes inside it (80 s at the mixed point), from the first changed file at about 25 s. The run should then take about 105–120 s, at full link and at 1 Gbit/s alike, where the link needs 42 s for 5 GB.

| # | Prediction | Bar |
|---|---|---|
| E1 (deciding) | **The gap closes:** uniform, full link, `hashAV` ≤ 0.70x `hash` | 0.70 (predicted 0.56–0.64) |
| E2 | **Hashing beats a re-clone at 1 Gbit/s where many pages change:** uniform `hashAV_1g` ≤ 0.92x `full_1g` | 0.92 (predicted 0.69–0.80) |
| E3 | **Each change helps on its own, and the two add up:** uniform, full link, `hashA` and `hashV` each ≤ 0.92x `hash`, and `hashAV` below both | As stated |
| E4 (no harm, and the bound's control) | **Where few pages change, nothing moves:** hot, every new arm within 1.03x `hash` at its rate. **`pg_basebackup` stays at its bound:** ≤ 1.10x T_impl in all four cells | As stated |
| E5 (the mechanism) | Uniform `hashAV`, full link: **at most 10 s of the source's disk alone** (EXP-90: 28), **at most 0.5 GB of dest's reads beyond the database** (EXP-90: 4.86), and **measured / T_impl ≤ 1.30** (EXP-90: 1.70) | As stated |
| Correctness | Every pgdelta copy matches its source (20 on the VMs). No wrong output in the 1,000 local trials, and every misplaced-write control caught | Any miss stops and is reported |

**Reported:**
- `hashAVw` over `hashAV`;
- each run's bound, binding resource, slack and busy-time split;
- the sender's planner and read-ahead counters, and the receiver's read-back counters;
- **the cross-session controls:** `full` and `hash` against EXP-90's 90.80 s and 186.93 s (uniform), within 1% for `full`;
- the local hashAV/hash ratio beside the VMs' (rule 13's proxy pair);
- **the figures:** `exp91-timelines` (the uniform runs, second by second) and `exp91-busy` (every run's seconds by what was busy).

## What follows

- **If E1 and E2 hold:** hashing beats a re-clone at 1 Gbit/s where many pages change, the scorecard's last loss for hashing on a slow link. RESULTS says so. A default change (`--hash-ahead`, `as-written`) would need a held-out session first (rule 15).
- **If E1 fails:** the timelines show what binds next. Candidates are the receiver's syncs, the source's scattered reads, and the order of B's reads and writes.
- **Either way, at full link** hashing can at best tie a re-clone. The structurally different idea, B's digests kept from the refresh that wrote B, stays next in the idea archive.

## The rules this session runs under

- **Rule 13:** the tests, then the local campaign and scale model, then the VMs. The local/VM pair is logged.
- **Rule 15:** no default changes and no claim leaves the project from this session, so no held-out session is owed. A default change would need one.
- **Rule 16:**
  - `analysis/integrity.py register EXP-91` records the analysis (and EXP-90's, which it reuses), the lab, the baseline script, the sampler, the probes, the campaign, the local script and pgdelta's modules;
  - every copy is checked;
  - the negative controls are the misplaced and corrupted writes (tests), and the misplaced-write switch (campaign);
  - E4's `pg_basebackup` runs control the bound arithmetic.
- **Rule 17:**
  - one run per arm;
  - within a session, two runs differ by up to 2.23% (10 s or more);
  - E1's bar is 6–14 points from its prediction, and E2's 12–23;
  - E4's 1.03 is the noise bar plus a margin;
  - a deciding ratio within 0.03 of its bar gets a second session.

## Workflow check

| | Check | For this experiment |
|---|---|---|
| 1 | Situation, standard, competitor | Hashing against a re-clone (`pg_basebackup`) beyond memory where many pages change. EXP-90 measured a loss at both rates (2.06x full link, 1.37x at 1 Gbit/s) |
| 2 | The bottleneck | EXP-90's per-second samples: the source blocked in `send()`, and dest's verification re-reading 4.86 GB that shares the operations budget with its writes |
| 3 | Systems or algorithm | Systems at 1 Gbit/s (floor 0.71–0.83x a re-clone); at full link at best a tie, so the algorithm's floor. E1 and E5 test whether the floor is reached |
| 4 | Papers and prior art | The mentor's batching and pipelining; the fast path's read-ahead (EXP-64b), reused for the pages hashing sends; EXP-35's composed verification, extended to every node |
| 5 | Committee-grade | Pre-registered, with the analysis committed first. Each change also runs alone (E3), with a mechanism check (E5), a control for the bound (E4) and negative controls. The predictions are inferred from EXP-90 |
| 13 | The cascade | Tests, then local correctness and the scale model, then the VMs. Promotion rests on EXP-90's VM measurements, since the local stage can't show disk effects. The proxy pair is logged |
| 14 | The idea archive | Read; both changes are its EXP-91 rows. Structurally different, beside them: B's stored digests, next |
| 15 | Held-out workloads | None owed: opt-in, with no default changed and no claim leaving |
| 16 | Scorer integrity | Registered files; every copy checked; negative controls in the tests and the campaign; E4 controls the bound |
| 17 | The noise bar | 2.23%; the deciding bars are 6–23 points from the predictions |
