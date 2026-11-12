# EXP-95: Is EXP-94's DR win hashing ahead, or just reading ahead?

Status: **pre-registered November 12, 2026, before any run and before any VM for it exists.** One fresh
`n2-standard-8` pair, EXP-13's set under its own name (`infra/gcp/sets/exp95.sh`), **48 scored runs**, about
25 minutes of runs and under an hour of VM time.

This is the 12-run follow-up EXP-94's own write-up asked for, widened to 48 so its controls run in the same
session rather than across one. It settles an attribution, not a verdict: EXP-94 already established **that**
`--hash-ahead 64G --read-ahead 8` is the DR fix. This asks **which half of it** is doing the work.

## Why this experiment

[EXP-94](EXP-94-dr-hash-ahead.md) held its deciding prediction decisively — uniform 1% went to **0.422×** v1 at
20 ms and **0.210×** at 120 ms tuned, reaching every DR verdict row BBR reaches on plain cubic. But two of its
seven predictions failed, and both failed in the same direction:

- **Prediction 3 failed.** The arm was about **twice as fast in hot** (0.477–0.774× v1), where few pages change
  and the registered mechanism says there is nothing to run ahead of. EXP-94's own falsifier text: *"A gain in
  hot would mean the arm is measuring something other than the stall, and would call the deciding reading into
  question."*
- **Prediction 6 failed.** It removed only about **half** the idle leaf wait, against a registered bar of
  three-quarters (0.379–0.819× against 0.25×).

So the arm works and the stated reason for why it works is at best incomplete. The untested co-variable is the
flag that rides along: EXP-94 registered `--read-ahead 8` as *"not an independent choice ... naming it is
documentation, not an extra variable"*, because `--hash-ahead` turns it on implicitly. **That is sound for
running the arm and wrong for attributing its gain.** Reading 8 files ahead should help whether or not many
pages changed — which is exactly the shape of the hot result.

**It is separable with no engine change.** `cli.py:_read_ahead` returns an explicit `--read-ahead` whether or
not `--hash-ahead` is given, so `--read-ahead 8` alone is expressible today. `tests/test_exp95_arm.py` pins
that, because if the resolution rule ever changed, the new arm would silently become v1 and the contrast would
vanish without a failure.

**Why it matters beyond tidiness.** `--hash-ahead` is a candidate default (EXP-94's "what follows"). A default
justified by a mechanism the data does not support is a default that will be wrong in some condition nobody
tested — and the two mechanisms predict different things. Hashing ahead should help only where there is enough
to hash; reading ahead should help wherever file reads sit on the critical path, including at sizes and change
patterns neither session has visited.

## Design

- **VMs:** one fresh `n2-standard-8` pair in `us-central1-a`, 4 local NVMe in RAID 0, ext4, Debian 12, minimum
  platform Intel Cascade Lake — **EXP-13's set, sourced not restated** (`sets/exp95.sh` sources `sets/exp13.sh`),
  so the hardware is identical to EXP-94's and its numbers are comparable. The image is pinned to the one
  EXP-94 ran, `debian-12-bookworm-v20261102`.
- **Conditions: EXP-94's two deciding cells only** — `2:20:default` and `2:120:tuned`. These are not chosen by
  outcome: they are the cells where **cubic was in its slow mode in EXP-94's session**, by EXP-23's mechanical
  rule applied to that session's own runs, and they are therefore the only cells where EXP-94's deciding
  prediction was judged. Running the other four would add 96 runs that answer a question nobody asked.
- **Arms (4):**

  | Arm | Sender | Why it is here |
  |---|---|---|
  | `pgdelta_direct_inplace` | v1 as shipped, cubic | the denominator of every ratio |
  | **`pgdelta_direct_inplace_ra`** | **`--read-ahead 8`, no hashing ahead** | **the arm this experiment exists for** |
  | `pgdelta_direct_inplace_ha` | `--hash-ahead 64G --read-ahead 8` | EXP-94's arm, as the within-session replication gate |
  | `full` | one `pg_basebackup` stream | rule 17's between-session control against EXP-94 |

  `parcopy8` and `parcopy16` are **not** run: this session reports arm-against-arm ratios, not EXP-20's DR
  verdict rows, so `R_fast`'s denominator is not needed. Dropping them saves 24 runs and changes nothing that
  is judged.
- **Workloads:** hot and uniform 1% at scale 70 (~2.1 GB) — EXP-20's and EXP-94's, unchanged. **3 repeats.**
  **Order seed 950**, distinct from EXP-94's 939/940.
- **4 arms × 2 conditions × 3 repeats × 2 workloads = 48 runs.** At EXP-94's measured pace (222 runs in
  91 minutes, 24.6 s a run) about **20 minutes**, plus a smoke.
- **A smoke first:** scale 5, hot, 1 repeat, both conditions, all four arms — 8 runs. It checks the emulation
  and that the new arm's flags are accepted on both ends before the session spends time.
- **Analysis:** `analysis/exp95.py`, committed before any run, which reports each prediction beside its
  registered bar. It reuses `exp94.py`'s record reader — **with the corrected glob**, since reading
  `runs/<sweep>/<name>.json` is exactly what EXP-94's defect got wrong and this experiment's predictions 3
  depends on the same records.

### Run lines

```sh
AT=2:20:default,2:120:tuned
ARMS=full,pgdelta_direct_inplace,pgdelta_direct_inplace_ra,pgdelta_direct_inplace_ha
COMMON="--manifest-checksums CRC32C --dirty-noise-table 5:4,30:4,120:4 --control-port 2222 --link-test-seconds 6 --link-test-warmup 3 --tuned-tcp-max-bytes 134217728"
python3 experiments/network/remote.py --pair exp95 start exp95-smoke -- --patterns hot --hot-percent 1.0 --scale 5 $COMMON --methods $ARMS --repeats 1 --conditions $AT --order-seed 949
python3 experiments/network/remote.py --pair exp95 start exp95 -- --patterns hot,uniform --fractions 0.01 --hot-percent 1.0 $COMMON --methods $ARMS --repeats 3 --conditions $AT --order-seed 950
```

## Predictions

Written before any VM exists. Each says what would prove it wrong.

1. **(Deciding.) Hashing ahead carries most of the gain.** For **uniform 1%** at each condition, the share of
   `_ha`'s gain that `_ra` delivers alone,

   > share = (1 − `_ra`/v1) ÷ (1 − `_ha`/v1)

   is **below 0.35**. *Wrong if it is above 0.65* — then reading ahead carries most of the gain and EXP-94's
   attribution to keeping the stream full is mistaken. A share of **0.35–0.65 is a split result**, reported as
   such, supporting neither claim and leaving `--hash-ahead` without a demonstrated mechanism.
2. **The hot gain is entirely reading ahead.** For **hot** at each condition, `_ra`/v1 and `_ha`/v1 agree within
   **1.05×** of each other. EXP-94's hot gain (0.477–0.774×) cannot be the leaf-digest stall, because hot has
   almost no digests to run ahead of. *Wrong if `_ha` beats `_ra` in hot by more than 1.05×*, which would mean
   hashing ahead helps even where there is nothing to hash — and would point at a third mechanism neither
   session has named.
3. **The mechanism separates on the instrument.** `_ra`'s median `leaf_wait_idle_s` is **at least 0.80×** v1's
   at both conditions: reading ahead does not fill the stream between files, so it should not remove the
   sender's idle wait. *Wrong if it is below 0.80×* — then reading ahead also fills the stream, the two flags
   are not separable by this instrument, and prediction 1's share cannot be read as a mechanism split.
4. **The replication gate.** `_ha`/v1 in uniform 1% reproduces EXP-94's ratios — **0.422 at `2:20:default` and
   0.210 at `2:120:tuned`** — each within rule 17's within-session band for runs of its length. *If the gate
   fails, no other reading here is interpretable* and the session is reported as a failed replication rather
   than as an attribution.
5. **Rule 17, cross-session.** This session's `full` is within **1%** of EXP-94's at both shared cells. EXP-94's
   own gate failed in 3 of 12 cells, so this is a live check, not a formality. If it fails, EXP-94's absolute
   seconds are not compared with this session's; every prediction above is a within-session ratio and survives.
6. **Correctness.** Every run exits 0 with no failed or skipped transaction, and the harness's durability and
   verification checks pass as they did in EXP-94. *Any wrong output stops the session* and no ratio from it is
   publishable.

## What follows

- **If 1 holds and 2 holds:** `--hash-ahead` earns its mechanism for scattered changes, the hot gain is
  explained as read-ahead, and EXP-94's deciding reading stands with its stated cause. `--hash-ahead` stays a
  candidate default and the held-out session (rule 15, H9–H12) is the next and last gate.
- **If 1 fails** (share above 0.65): **`--read-ahead` is the DR fix and `--hash-ahead` is along for the ride.**
  That would be a larger finding than EXP-94's, because reading ahead is the cheaper and simpler flag, and it
  would redirect the default change. EXP-94's write-up would gain a correction, not a retraction: its times are
  right and its attribution was not established.
- **If 3 fails:** the two flags are not separable on this instrument, prediction 1's share is a time split and
  not a mechanism split, and the next step is a sender-side trace rather than another arm.
- **No default changes here either way.** That is the owner's, and rule 15 owes a held-out session first.

## Rules this session runs under

| Rule | Answer |
|---|---|
| 8 | Any amendment is recorded here with its position relative to the runs |
| 11 | A deciding value within 0.05 of its bar earns a second session on a fresh pair |
| 13 | Arm flags pinned by pgdelta's own parser (`tests/test_exp95_arm.py`, 6 tests) before any VM |
| 15 | No default is changed; the held-out session stays owed |
| 16 | Every file that scores, runs or is under test is hashed at this registration's commit, and `check` is quoted in the results |
| 17 | 3 repeats a cell; `full` is the between-session control (prediction 5) and `_ha` the within-session replication gate (prediction 4) |

## Budget

Two `n2-standard-8` with local NVMe for under an hour, in `us-central1-a`: **about $1**, on the same
approximate list prices EXP-94's budget used. `MAX_RUN_HOURS=2` is enforced server-side by
`create.sh --max-run-duration` with an `--instance-termination-action`, so a forgotten pair cannot bill for
days even if no local credential can reach the API — the failure that cap exists for.
