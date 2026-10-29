# EXP-82c: Summaries on demand on the VMs, on a source that never summarized

Status: **pre-registered October 29, 2026, before any run.** One `ha` VM pair, about 1.3 hours with the data load. It re-runs [EXP-82b](EXP-82b-summaries-off-vms.md)'s on-demand arm, which was void: that source summarized throughout, because `run_study.py`'s base configuration turns `summarize_wal` on. Page-lsn and hashing run beside it, as a second session for page-lsn.

## Question

**On a source that never summarized,** with a replication slot keeping the WAL since the old copy, how long does it take to turn `summarize_wal` on, wait for PostgreSQL's summarizer to build the summaries from the oldest WAL on disk, and refresh by the summaries' fast path? Against hashing, and against page-lsn?

## Design

**EXP-82b's lab and pipeline**, with its fix committed with this registration:
- **The source as one that never summarized:** in `SUMMARIES=0` mode the lab sets `summarize_wal` off with `ALTER SYSTEM`, restarts the source, removes the summaries made during the data load, and checks that the summarizer reports no position, no process and no summary files. Otherwise the lab stops.
- **After each on-demand run,** the setting is set off (not reset, since a reset falls back to the base file's on), the summarizer waited for until it exits, and its summaries removed.

**The setting:** the `ha` set (ext4), EXP-48's 18 GB database, PostgreSQL 17.11, data checksums, BLAKE3 on both, pgdelta at this commit; `SLOT=1`, `STANDBY_TOO=1`, `SERIES=1`.

**The arms**, on B0s (a standby stopped cleanly before the change):
- `sb_hash`: no fast path;
- `sb_lsn`: page-lsn;
- `sb_od`: on demand, timed from before the setting is turned on until the refresh ends.

**Runs:** 2 per arm, shuffled per cell (seed 824); 12 runs. Every copy is started and checked against the source.

**The smoke** at scale 20 (seed 825) decides nothing. It checks:
- the set-up check passed;
- every run exits 0 and every copy matches;
- on demand and page-lsn used their fast paths.

**The command:**

    PATTERNS="hot uniform" ARMS="sb_hash sb_lsn sb_od" STANDBY_TOO=1 SUMMARIES=0 SLOT=1 SERIES=1 OUT=/data/exp04-results/exp82c lab_exp64.sh SOURCE 2 824

**The analysis,** committed before the data: `analysis/exp82c.py`. Its dry run on EXP-82b's runs flags their source as having summarized (prediction 4 fails there, as it must). Data `docs/experiments/data/exp82c-cells.csv`, figure `exp82c-on-demand`.

## Predictions

**What the bound and the local runs imply (inferred):**
- **The WAL since B's redo point** is about 0.4–0.55 GB (hot) and 12 GB (uniform), from EXP-81's and EXP-82b's runs.
- **The summarizer** read 552 MB in about 1.7 s locally, from memory. Here it reads from disk (about 210 MB/s) and decodes every record.
- **Expected on demand:** hot, 3–8 s (0.03–0.09 of hashing's 87.7 s); uniform, 90–160 s (0.48–0.85 of hashing's 188.6 s), so likely slower than page-lsn's 88.6 s.

**Scored:**
1. **Deciding: in hot, on demand takes at most 0.20 of hashing's time** (medians).
2. **In uniform, on demand takes at most 0.90 of hashing's time.**
3. **page-lsn, a second session:** at most 0.70 of hashing's time in uniform and at most 1.05 in hot (EXP-82b: 0.470 and 0.999).
4. **Correctness and the condition:**
   - the source reported no summarizer position at set-up;
   - every run exits 0, and every copy matches;
   - on demand used the summaries' fast path, with summaries, in every run;
   - page-lsn was used in every run ("clean shutdown in recovery").

**Reported, not predicted:**
- on demand against page-lsn in each cell: the crossing of the WAL since B's redo point against \|S\|, which a selector between them would use;
- on demand's two phases, and its summaries and summary rows.

**Rule 11:** a deciding ratio within 0.05 of 0.20 gets a second session.

## The rules this session runs under

- **Rule 13:** the VM stage for on demand, on the bound, as EXP-82 registered (its local promotion failed).
- **Rule 15:** not a milestone.
- **Rule 16:** `analysis/integrity.py register EXP-82c` records the analysis (and EXP-82b's, whose figure it reuses), the lab, the disk sampler and pgdelta's modules. The set-up check is the guard against EXP-82b's defect.
- **Rule 17:** 2.40% (10 s or more), 7.11% under 10 s; the bars are far outside.

## What follows

- **If 1–4 hold:** summaries on demand are measured. Where the WAL since B is kept, switching summaries on at refresh time refreshes local changes far faster than hashing, page-lsn, WAL-G or a re-clone.
- **Then a selector:** choose between on demand and page-lsn by the WAL since B's redo point against \|S\|, from this session's two cells.
- **If 1 fails:** the summarizer's cost is more than the bound says. Its phase timings show where.

## Workflow check

| | Check | For this session |
|---|---|---|
| 1 | Situation, standard, competitor | Summaries off, WAL kept: replay (standard), WAL-G catch-up (competitor, timed in EXP-81 and EXP-82b) |
| 2 | The bottleneck | Reading S (page-lsn) against reading the WAL (on demand) |
| 3 | Systems or algorithm | Algorithm: which record of change to read |
| 4 | Papers and prior art | Research loop 3: the summarizer's start from its source; Percona's bitmaps, WAL-G's delta maps and `pg_probackup` PAGE as the same mechanism |
| 5 | Committee-grade evidence | Pre-registered; EXP-82b's void arm disclosed; the condition checked at set-up |
| 13 | The cascade | The VM stage, on the bound |
| 14 | The idea archive | On demand "timing in test"; B's own WAL for a slot's gap archived |
| 15 | Held-out workloads | Not owed |
| 16 | Scorer integrity | Recorded at registration; the set-up check |
| 17 | The noise bar | As above |
