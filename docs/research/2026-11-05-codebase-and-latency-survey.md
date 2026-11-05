# The codebase and the latency chain, surveyed (November 5, 2026)

Written before the next DR session, so that pass does not re-derive any of it. It answers four
questions the owner asked on November 5: what is on each branch of the repository, which
pgdelta paths are actually built, where the original `pg_delta_backup` contract and the
native-increment comparator live, and what the GCP latency sessions have established so far.

Nothing here is a new measurement. Every number is cited to the experiment that produced it.
The last two sections are the only parts that propose anything: six gaps, each with the
evidence that it is open, and the state of the open experiments.

## 1. Branches

Remote: `github.com/MattyWeee123/pg-delta-backup`.

| Ref | Last commit | What it carries |
|---|---|---|
| `origin/main` | October 31 | EXP-89, -89b, -90, -91, -92 and K16 (single read is page-lsn's default) |
| `exp88-slowdown-by-load` | November 5 | EXP-88 stages 1-3 and EXP-93, pre-registered |
| `origin/codex/live-pgdelta-benchmark` | October 29 | superseded; kept for history |
| `origin/codex/native-active-cutoff` | October 29 | superseded; kept for history |
| `pre-rewrite-main-backup` (local) | October 23 | the pre-rewrite tree |
| `benchmark-harness-v0.1` (local) | October 18 | the first harness |

**The working branch and `main` have diverged in the engine, not only in documents.**
`exp88-slowdown-by-load` is 17 commits ahead and 23 behind. The `pgdelta/` difference is 509
inserted lines across `cli.py`, `fastpath.py`, `receiver.py`, `sender.py` and `transport.py`.
Three things exist only on `main`:

- `--hash-ahead` (EXP-91): the sender's hashing runs ahead of the file being sent.
- `--verify-scope as-written` (EXP-91): the receiver verifies each run as it is written.
- `--read-ahead`'s derived default (EXP-92): taken from `--fast-path` or `--hash-ahead`.

**Consequence worth recording:** EXP-93 is pre-registered on the working branch and its driver
patch passes `--hash-ahead`, a flag the engine on that branch does not accept. EXP-93 cannot
run until `main` is merged. This was found by survey, not by a failed run.

## 2. Which pgdelta paths are built

The project does not version pgdelta with release numbers. It tracks **paths**, each with its
own arm vocabulary in the harnesses. Three exist.

### The v1 hashing path (default when no fast path is asked for)

A keyed hash tree over 8 KiB pages, in place, verified (EXP-03, -09, -17, -25). Built arms in
`experiments/network/measure.py`:

| Arm | What it changes |
|---|---|
| `pgdelta_direct_inplace` | v1 as shipped, 4 jobs, `--window 4` |
| `_j8` | 8 jobs on each end (EXP-11) |
| `_bbr` | BBR on pgdelta's own data socket (EXP-24) |
| `_b2`, `_b3` | the keyed hash pinned to BLAKE2b or BLAKE3 (EXP-60) |
| `_z`, `_zs` | zstd on a pool; zstd with sub-page deltas (EXP-45 to -56) |
| `_tls` | through a stunnel pair (EXP-13); `_bbr_b3_tls` is pgdelta's own mutual TLS (EXP-58) |
| `_vw` | `--verify-scope written`, no second pass over B (EXP-31) |
| `_w1`, `_w16`, `_hc` | the request window at 1 and 16, and the high-churn fallback |

**`_w1`, `_w16` and `_hc` are built and have never been run.** They are EXP-13's registered
side arms, on hold since October 25.

### The WAL-summary fast path (`--fast-path summaries`, opt-in)

Labels from PostgreSQL 17's WAL summaries instead of hashing B, behind a lineage guard
(EXP-61 to -64, -79b). Its defaults accumulated over eleven experiments: guard v2 (EXP-62),
`--read-ahead 8` (EXP-64b), fsync on a worker (EXP-68), clean-gap fill up to 2 pages (EXP-70,
-74), the guard's one grouped unsorted query (EXP-73, -78), written pages re-read a run at a
time on the verifier (EXP-76, -77), `--diverged-basis` for an old primary after a failover
(EXP-69, -71) and `--crashed-standby` (EXP-80, -80b, -84). `--selector` stays opt-in pending
the refit EXP-84 asked for.

### The page-LSN fast path (`--fast-path page-lsn`, opt-in)

Selection on the source from page LSNs, WAL-G's and `pg_probackup` DELTA's way, for clusters
with `summarize_wal` off (research loop 3, EXP-82, -82b). `--lsn-single-read` sends each chosen
page from the scanner's buffer and is **page-lsn's default on `main` since K16** (EXP-85,
EXP-87).

### The lab vocabulary

The load drivers (`lab_exp88.sh` and its relatives) name arms differently from `measure.py`:
`none`, `none_nosum` (summaries off), `full` (one `pg_basebackup` stream), `v1`, `v1_j1`,
`v1_j2`, `v1_j4` (explicit worker counts) and `v1_fast` (the summaries fast path). Two
vocabularies for the same engine is a wart; it is recorded here rather than changed.

## 3. The original `pg_delta_backup` contract, and "with increment"

**The contract** is in [SPEC.md](../SPEC.md) section 3, proposed specification v0.3,
September 30 2026:

```text
pg_delta_backup plan     --source-backup S --basis B --format json
pg_delta_backup transfer --source-backup S --basis B --output O
pg_delta_backup verify   --output O
```

with exit codes 0 success, 2 invalid or unsupported input, 3 transfer or I/O failure, 4
verification failure. SPEC.md still marks these as "contracts to implement, not commands
currently provided": the name was never shipped, and the built CLI is `pgdelta`, whose verbs
are `send` and `receive`. Anyone looking for a `pg_delta_backup` function in the tree will not
find one, and that is by design, not drift.

**"With increment"** is PostgreSQL 17's own incremental backup, `pg_basebackup --incremental`
plus `pg_combinebackup`. It is the incumbent this project compares against, and it is built as
a comparator rather than reimplemented:

- `experiments/change_patterns/tracker.py` parses the incremental file header, following
  `basebackup_incremental.c` (writer) and `pg_combinebackup/reconstruct.c` (reader) in
  REL_17_STABLE.
- `measure.py` arms `incremental` and `incremental_cfr` (the latter with
  `pg_combinebackup --copy-file-range`).
- `pgdelta/fastpath.py` chooses what to send "as PostgreSQL's own incremental backup chooses".

Where native incremental's prerequisites hold, SPEC.md section 1 says pgdelta should hand off
to it rather than compete. The measured comparisons: EXP-01 (bytes), EXP-81 (the fast path
against backup chains: 0.29 hot, 0.47 uniform) and **EXP-36 at DR round trips, where native
incremental is not faster than v1** - v1 with BBR won every cell on ext4 (1.14-1.74x) because
`pg_combinebackup` writes a whole new copy; on XFS with `--copy-file-range` the two tied
(0.96-1.10x, provisional under rule 11).

## 4. The GCP latency chain

Every latency result the project has, in the order it was established.

| Experiment | What it settled |
|---|---|
| EXP-04, -04b | the network sweep, and again with PostgreSQL's default backup checksums |
| EXP-11 | how fast a durable full copy goes; one TCP stream did 10.5-15.1 Gbit/s at 0.1 ms |
| **EXP-13** | the big T12 registration. **Its side arms are still on hold**: TLS, the window arms, the high-churn fallback, `pg_probackup`, 1 Gbit/s, uniform 10% |
| **EXP-20** | the deciding DR cells at 20, 60, 120 ms, cubic: **"local changes only"**. Hot 0.31-0.63 of the fastest parallel copy; uniform 1% 1.70-3.20. EXP-13's expectation for scattered changes failed badly (0.57-0.80 expected at 60 ms tuned, 2.36 measured) |
| **EXP-21** | why. **Not** buffers or round trips: cubic's congestion window. v1's page stream is **bimodal** - the same 610 MB either took 2.3 s or about 20 s. In a slow run a burst lost 32 segments 1.5 s in, while the window was still 466 segments; cubic set its threshold there and grew back slowly. **One `pg_basebackup` stream escapes it because it sends 2.1 GB continuously; v1's 610 MB is short and starts with bursts** |
| **EXP-23** | the rate. Cubic fell into the slow mode in 12 of 15 runs at 20 ms and 0 of 15 at 60 ms tuned; **BBR in 0 of 30** |
| **EXP-24** | with BBR on pgdelta's own socket, **v1 holds at every DR round trip, scattered changes included** (0.63-0.75 of the fastest parallel copy on tuned hosts) |
| **EXP-26** | BBR's fairness. A BBR newcomer took 81-96% of the link from cubic traffic wherever the queue was one round trip or less, in 8 of 10 cells. **BBR stays opt-in** |
| **EXP-15** | a real 31 ms Iowa-Oregon link: BBR v1 0.33 (hot) and 0.61 (uniform 1%) of the fastest parallel copy. Cubic's slow mode did not appear there (0 of 10); BBR was 15-21% faster anyway |
| EXP-36 | native incremental at DR round trips (section 3) |
| EXP-45, -46, -47, -52, -53 | zstd at DR: 0.52 of v1 at 0.5 Gbit/s, break-even about 4 Gbit/s |
| EXP-58, -72 | TLS's cost: about 2% at 2 and 5 Gbit/s after the transport fix |
| EXP-60 | BLAKE3: 0.87 of BLAKE2b at 5 Gbit/s |

### The harness, all reusable

- **`experiments/network/run_exp04.py`** - the driver. `--conditions RATE:RTT_MS:HOST`,
  `--methods`, `--repeats`, `--order-seed`, `--tuned-tcp-max-bytes`.
- **`experiments/network/measure.py`** - the arm registry and flag derivation.
- **`infra/gcp/wan.sh`** - the emulated WAN: a `prio` qdisc with a plain band for the control
  path and a delayed band of `netem` before `tbf`, so the delay line stays apart from the
  bottleneck queue. Half the round trip on each VM.
- **`infra/gcp/create.sh`**, `teardown.sh`, `throttle.sh`, `vmset.sh`, `sets/*.sh`.
  `create.sh` enforces `MAX_RUN_HOURS` server-side with `--max-run-duration` and an
  `--instance-termination-action`, so a forgotten session cannot bill for days even when no
  local credential can reach the API.
- **Lab drivers** `lab_exp21.sh`, `lab_exp23.sh`, `lab_exp26.sh` - the pattern for a session
  that sweeps a host setting on both VMs and restores it.
- **`experiments/network/remote.py`** - `fetch NAME --to DIR`.

## 5. Six gaps in the latency evidence

| # | Gap | Why it is open |
|---|---|---|
| **G1** | **The fast path has never been measured at any round trip.** | Every fast-path result, EXP-61 through EXP-92, ran at 0.1 ms or on a 1 Gbit/s HA-type link. [idea-archive.md](idea-archive.md) lists "the fast path at DR round trips" as **open**, trigger "with the next DR session". `measure.py` has **no fast-path arm at all** - all eight v1 arm families are hashing-path. |
| **G2** | **The fast path should be the most latency-exposed arm, and nobody has said so.** | EXP-21's mechanism is that the slow mode hits *short, bursty* transfers. The fast path sends the fewest bytes of any arm - 1.98 s for 18 GB of local changes (EXP-64). By EXP-21's own reasoning its exposure is the worst of the three paths. |
| **G3** | **`--hash-ahead` is EXP-21's proposed fix, built later for another reason, never tested at latency.** | EXP-21's "what follows" says pgdelta "can avoid needing [BBR] by keeping its stream full between files". `--hash-ahead` (EXP-91) does exactly that; it was built to remove a sender stall at 1 Gbit/s. Since EXP-26 left BBR opt-in for unfairness, a sender-side fix that removes the need for BBR is worth more than BBR. |
| **G4** | **D29 and D30 are grade D with latency as their gray area.** | [DECISIONS.md](../DECISIONS.md): D29 "hash 4 files ahead" and D30 "all page-digest requests for a file at once", both "Never varied; latency never added", gray area "20-120 ms round trips, the niche", test T12. The arms `_w1` and `_w16` already exist. |
| **G5** | **EXP-20's and EXP-24's verdicts describe the October 25 engine.** | 23 commits of engine change since, including three new flags and a changed default. Rule 17 requires a control arm within 1% of the earlier session before any cross-session comparison, so a re-run cannot simply be assumed to agree. |
| **G6** | **EXP-13's side arms are still owed.** | 1 Gbit/s, uniform 10% high churn and `pg_probackup` at latency have been on hold since October 25. Recorded, not scheduled. |

## 6. The state of the open experiments

- **EXP-88** - registered October 30; all three stages have run. Raw sessions are now
  consolidated under `results/exp88/` as `stage1` (with `stage1extra`), `stage2` and `stage3`,
  which is the layout `analysis/exp88.py` expects by default. The write-up has no Results or
  Verdict section yet. `docs/experiments/data/exp88-cells.csv` holds stage 1's scored cells
  only.
  - **A near-miss worth recording:** stage 2's 18 runs were briefly thought lost. They had been
    fetched into `results/exp04/sets/exp88s2/exp88s2/`, named by VM set rather than by stage,
    two levels below where a search for `stage2` would look. `results/` is gitignored
    (`.gitignore:15`), so no stage's raw runs are under version control and none of this was
    visible in git. The scored CSV is the committed artefact; the raw runs are local only, in
    one copy. Consolidating the sessions under the layout the scorer expects is what made the
    inventory legible.
- **EXP-93** - registered November 5; blocked on `--hash-ahead` (section 1).
- **Billing** - as of this survey: 0 instances, 0 disks, 0 snapshots, 0 custom images in
  `postgres-delta-backup`. The incident that motivated `MAX_RUN_HOURS` (a stage-2 set that ran
  about 57 hours instead of 2, roughly ten times the session's budget) is the reason this is
  now checked at the start of a session rather than the end.
