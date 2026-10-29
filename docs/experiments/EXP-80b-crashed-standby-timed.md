# EXP-80b: A crashed standby beyond memory: the fast path with `--crashed-standby`, against hashing and a re-clone

Status: **pre-registered October 28, 2026, before any run** (00:07 UTC October 29). One `ha` VM pair, about 1.5 hours with the data load. It is EXP-80's VM stage under rule 13.

## Question

**EXP-80 showed `--crashed-standby` is sound:** 100 of 100 randomized crashed standbys came out correct through the fast path, locally, on a 60 MB model. Their end was proven by the minimum recovery point.

**Who needs it:** a standby that crashed, or was killed, and stayed down until the source recycled the WAL it needs (no slot, no archive). It can't catch up by replication. Today its options are:
- **pgdelta:** the fast path refuses it ("in archive recovery"), so pgdelta hashes. Hashing reads all of B, so beyond memory it at best ties a re-clone (EXP-31).
- **A re-clone** with `pg_basebackup`.
- **`pgBackRest --delta`:** it also reads every file (research loop 2).

**The question: on the VMs, beyond memory, how much faster is a crashed standby refreshed with `--crashed-standby` than today's path for it (hashing), and than a re-clone?**

## Design

**The lab:** EXP-70's (`experiments/network/lab_exp64.sh`), with a new mode, `CRASHED=1`, committed with this registration.
- **The setting:** the `ha` set (n2d-highcpu-8, 150 GiB balanced PDs, two zones, us-west1 first), EXP-48's 18 GB database (scale 600), PostgreSQL 17.11, BLAKE3 on both VMs, pgdelta at this commit; `SERIES=1` (both disks sampled every 0.25 s).
- **`CRASHED=1`:** each pattern's B0 is a standby of the source (`pg_basebackup -R`), started on dest before the change. It is killed with `pg_ctl -m immediate` `KILL_AFTER` seconds (30) into the change, while the change goes on to its end on the source. The lab logs B0's state after the kill ("in archive recovery").
- **Each run** starts from a fresh `cp -a` of the crashed B0. Each pgdelta copy is started without its `standby.signal` and its `pgbench_accounts` compared with the source's.

**The cells:** `hot` (its change takes about 64 s on this set) and `uniform` (about 460 s). So the kill falls about midway through hot's change and early in uniform's.

**The arms:**
- **`fast8wf2c`:** the fast path with today's defaults (8 reader threads, the fsync worker, gaps of up to 2 pages filled), plus `--crashed-standby`.
- **`fast8wf2`:** the same without the option. The fast path is refused ("in archive recovery") and pgdelta hashes: today's path for a crashed standby.
- **`full`:** `pg_basebackup`.

**Runs:** 2 per arm, shuffled per cell (seed 800); 12 runs.

**The smoke** (scale 20, `KILL_AFTER=3`, seed 801) decides nothing. It checks the new mode:
- every run exits 0, and every pgdelta copy matches;
- every `fast8wf2c` run used the fast path, and no `fast8wf2` run did;
- the disk series were recorded.

**The command:**

    PATTERNS="hot uniform" ARMS_hot="fast8wf2c fast8wf2 full" ARMS_uniform="fast8wf2c fast8wf2 full" CRASHED=1 SERIES=1 OUT=/data/exp04-results/exp80b lab_exp64.sh SOURCE 2 800

**The analysis,** committed before the data: `analysis/exp80b.py`.
- **Dry run** on EXP-70's runs, where there is no `fast8wf2c` arm and B0 is a base backup: it ran, and predictions 1–4 failed there, as they must.
- **Data** `docs/experiments/data/exp80b-cells.csv`, **figure** `exp80b-crashed`.

## Where the bars come from

From the same set with a cleanly stopped B0, which takes the same fast path:

| Cell | Fast path, fill 2 | Hashing (EXP-64) | `pg_basebackup` | Fast path / hashing | Fast path / `pg_basebackup` |
|---|---|---|---|---|---|
| hot | 1.93 s (EXP-70) | 87.81 s | 90.67 s | 0.022 | 0.021 |
| uniform | 46.59 s (EXP-70); 42.17 s (EXP-79b) | 180.12 s | 91.02 s | 0.23–0.26 | 0.46–0.51 |

**Two things could differ for a crashed standby:**
- **Its redo point** is its last restart point. If the source checkpointed before the kill, the summaries start later and fewer pages are sent.
- **Its pages are partly replayed.** Hashing then finds fewer differing pages; the fast path sends what the summaries name either way.

**Hashing hasn't been timed since EXP-64,** before EXP-68's fsync worker and EXP-77's verifier change.

**So the bars leave room for these, and still decide.** They sit far outside rule 17's noise bar (a 2.27% spread within a session for runs of 10 s or more; 7.7% under 10 s).

## Predictions

1. **Deciding: in `uniform`, `fast8wf2c` takes at most 0.60 of `fast8wf2`'s time** (medians). Expected about 0.25.
2. **In `hot`, at most 0.20.** Expected about 0.02.
3. **`fast8wf2c` takes at most 0.80 of `pg_basebackup`'s time in both cells.** Expected about 0.5 (uniform) and 0.02 (hot).
4. **Correctness, and the guard's decisions (rule 16):**
   - every run exits 0, and every pgdelta copy matches the source;
   - every `fast8wf2c` run took the fast path with the proof "crashed standby";
   - every `fast8wf2` run was refused as "in archive recovery".

**Reported, not predicted:**
- the ratios against the expected values above;
- each run's pages received and re-read. On clean B0s they differ by a fixed amount per cell: 2,888 in EXP-70's and EXP-77's uniform.
- B0's state after each kill, and its redo point against the change's start;
- both disks' I/O time per run.

**Rule 11:** a deciding ratio within 0.05 of 0.60 gets a second session.

## The rules this session runs under

- **Rule 13 (the cascade):** EXP-80 was the local stage, for correctness. This session times the same fast path as the cells above, so its bars come from those VM cells, not from a local ratio. It logs no proxy-fidelity pair.
- **Rule 15 (held out):** not a milestone. If 1–4 hold, a held-out session comes before `--crashed-standby` becomes the fast path's default.
- **Rule 16:** `analysis/integrity.py register EXP-80b` records the analysis, the lab and pgdelta's modules as of this commit; `check` runs at the write-up.
- **Rule 17:** above.

## What follows

- **If 1–4 hold:** `--crashed-standby` is measured beyond memory against hashing and a re-clone. RESULTS and the scorecard say so. The option goes into the next held-out session (rule 15), together with `--selector`, before either becomes a default.
- **If 1 fails:** the fast path on a crashed standby isn't the win the clean cells imply. The runs' disk series and plans show why, and the option stays opt-in.
- **If 4 fails:** the option is withdrawn until the failing run is understood.
