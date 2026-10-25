# EXP-17: Does v1's refresh ever produce a wrong copy? A randomized fault campaign

Status: **pre-registered October 25, 2026, before running.** Local (a Mac, PostgreSQL 17); no VMs. It is the correctness campaign of the lean plan (rule 11 as clarified October 25): many cheap randomized trials with an independent check of every output, giving a stated bound on the failure rate instead of a handful of runs.

## Question

When v1's path refreshes an old copy B in place from a stopped source S (`pgdelta local --in-place --source-kind datadir`), is the result ever wrong without the tool saying so, across random workloads and injected faults? And does a refresh killed part-way always leave B unstartable until a rerun completes (G1)?

## The claim it can support

If N independent trials give no wrong output, then the probability that a refresh drawn from this trial distribution produces a wrong output is below 1 − 0.001^(1/N) ≈ 6.9 / N with 99.9% confidence (and below 3.0 / N with 95%). The claim holds for this distribution of workloads, faults and sizes on this platform; it says nothing about conditions outside it (a live source, other filesystems, TB sizes), which T8 and T13 cover.

## Each trial (seeded; the seed is the trial number)

1. **S and B** start as APFS clones of one template cluster (`initdb`, `pgbench -i -s 3`, about 50 MB, one system identifier).
2. **S's workload:** S is started and given a random mix: `pgbench` transactions (100–3,000), `CREATE TABLE ... AS`, `DROP TABLE`, `TRUNCATE`, `VACUUM FULL` of a random table (a new relfilenode), `CREATE INDEX`, bulk `UPDATE` and `DELETE` of a random range, and `CHECKPOINT`s, in random order. A logical digest of S is taken (every user table: row count and the md5 of its rows sorted as text; the schema's md5), then S is stopped cleanly.
3. **A fault, drawn at random:**

| Fault | Share | What is done | Pass means |
|---|---:|---|---|
| none | 35% | – | exit 0 and a correct B |
| diverged | 15% | B gets its own random workload first (an ex-primary's writes) | exit 0 and a correct B |
| kill | 25% | the refresh is SIGKILLed after a random 0.02–2.0 s, then rerun | after a kill that came before completion, B refuses to start; after the rerun, exit 0 and a correct B |
| corrupt | 10% | 1–16 random bytes overwritten in 1–3 random relation files of B | exit 0 and a correct B |
| truncate | 10% | a random relation file of B truncated to a random length, or deleted | exit 0 and a correct B |
| foreign | 5% | B is a separate `initdb` with the same workload (another system identifier), run without `--reuse-any-basis` | the refresh refuses (non-zero exit) and B is left byte-for-byte as it was |

4. **"A correct B"** is judged independently of pgdelta's own verification: every regular file of S outside the excluded set (PostgreSQL's base-backup exclusions, which `pgdelta/datadir.py` lists, and `pg_wal/`) is byte-identical in B, B has no relation file S lacks, and B starts and gives S's logical digest exactly.

## Outcomes and rules

- **A wrong output** (exit 0 with a B that fails the check, or a refusal that changed B) stops the campaign; the trial is kept with its seed for reproduction. It is reported, its cause found, and the bound above is not claimed.
- **A harness error** (the template, a server that won't start for reasons outside pgdelta) is recorded and the trial is not counted.
- **The stop rule:** run for 3 hours or 1,000 counted trials, whichever comes first; the count and the bound are what that gives.
- **The prediction:** no wrong output; every killed run that hadn't finished leaves B unstartable.

## Limitations, known before running

A stopped source only (v1's live read is T8). One small cluster (about 50 MB), so files are few and short; size-dependent bugs above a few relation segments wouldn't show. macOS's APFS, not ext4 or XFS. One PostgreSQL version. The faults are the ones listed; a crash of the host (not just the process) isn't simulated, and that remains T13c's.

## The smoke run and the checker's negative controls (October 25, before the campaign)

- **Harness fixes, none in pgdelta:** `pg_ctl` needed its `start` operation; PostgreSQL on macOS needs `LC_ALL` set; the schema digest needed a cast (`relkind::text`); a workload expression overflowed a 32-bit integer; and the loop now stops after 10 harness errors in a row instead of retrying them.
- **Smoke:** 6 trials in 17.5 s (none, corrupt, foreign, kill among them), every one passing; the killed run had not finished and B refused to start until the rerun completed. At about 3 s a trial, the stop rule's 1,000 trials come before its 3 hours.
- **Negative controls on the checker** (a checker that can't fail shows nothing): from an exact copy of S, one bit flipped in a random relation file was caught in 20 of 20 tries; an extra relation file in B was caught; one changed row was caught by the logical digest and by the byte check.
