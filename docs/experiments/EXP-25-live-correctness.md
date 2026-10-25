# EXP-25: Does v1's refresh from a live source ever produce a wrong copy? A randomized campaign under load

Status: **pre-registered October 25, 2026, before any campaign run.** Only the smoke run below has run; the campaign itself has not. Local (a Mac, PostgreSQL 17.11); no VMs. It is EXP-17's campaign moved to T8's case: the source keeps running and writing while pgdelta reads it in backup mode (`--source-kind live`, built for this; [pgdelta/README.md](../../pgdelta/README.md#reading-a-running-server-in-backup-mode---source-kind-live)), and every output is checked independently of pgdelta by recovering a disposable copy of it.

## Question

When v1 refreshes an old copy B in place from a running source S (`pgdelta local --in-place --source-kind live`) while S runs a continuous write load, is the result ever wrong without the tool saying so, across random DDL between refreshes and injected faults on B? Does a refresh killed part-way leave B either as it was, blocked from starting, or (if the kill came after the commit) a correct copy? And do the checks catch a deliberately broken variant that reads the files before `pg_backup_start`, as SPEC section 2 says it must not?

## The claim it can support

As in EXP-17: if N counted trials give no wrong output, a refresh drawn from this campaign has a chance of a wrong output below 1 − 0.001^(1/N) ≈ 6.9 / N at 99.9% confidence (3.0 / N at 95%). Here the trials are seeded but not independent: S's load runs on across them and B carries over from one trial to the next. The bound then reads: a refresh whose chance of a wrong output was at least p in every trial would have shown one in N trials with probability at least 1 − (1 − p)^N. It covers this distribution of load, DDL, faults and sizes on this platform, and says nothing about conditions outside it (see the limitations). It holds only if the negative control is caught as registered below.

## Setup

- **Template** (made once): `initdb`, a database `bank` with `pgbench -i -s 3` (300,000 accounts, all balances 0), EXP-25's load tables (below), a table `notes` of 20,000 rows, and `amcheck`; about 60 MB.
- **S**: an APFS clone of the template, started once with `fsync=off`, `max_wal_size=256MB` and `checkpoint_timeout=30s` (so checkpoints also fall inside some refreshes), and kept running for the whole campaign.
- **The load** ([experiments/correctness/bank.py](../../experiments/correctness/bank.py), after EXP-12's): `pgbench`, 4 clients, open loop at 2,000 transactions per second, for the whole campaign (restarted and recorded if it ends). 99 in 100 transactions commit: each moves money between two accounts and adds exactly 1 to the sum of balances, adds 1 to its client's counter `n` in `load_progress` with `ts = clock_timestamp()`, and appends the ledger row `(client, n, ts)` to `load_ledger`, taking its locks in one order so none deadlock. The other 1 in 100 adds 10^9 to an account and rolls back. In any consistent state, sum(abalance) = sum(n), no balance holds a rolled-back 10^9, and each client's ledger holds its rows without a gap up to its `n`.
- **B**: an APFS clone of the template at the start, then refreshed in place by every trial; each trial's B is the previous trial's output, with this trial's fault applied.

## Each trial (seeded; the seed is the trial number)

1. **DDL on S, between refreshes:** 1–4 random steps from `CREATE TABLE ... AS`, `CREATE UNLOGGED TABLE ... AS`, `DROP TABLE`, `TRUNCATE`, `VACUUM FULL` of a random table (the load's own tables among them, so a relation the load writes gets a new file), `CREATE INDEX`, `REINDEX TABLE`, bulk `UPDATE`, `DELETE` and `INSERT` on `notes`, `CHECKPOINT`, `CREATE DATABASE` (strategy `wal_log` or `file_copy`) and `DROP DATABASE`. Then S's ledger is trimmed to its last 2 minutes. Then the **static digest** is taken: every table the load does not write (row count and md5 of its rows sorted as text; an unlogged table only noted), the public schema's relations with their kinds, persistence and index definitions, and the list of databases. No DDL runs during a refresh, so the refresh cannot change any of it.
2. **A fault on B**, drawn at random:

| Fault | Share | What is done to B before the refresh | Pass means |
|---|---:|---|---|
| none | 40% | – | exit 0 and a correct B |
| diverged | 15% | B is started (it recovers from its own `backup_label`), runs its own bank load for 0.3–1.5 s, gets a table and an `UPDATE` of its own, and is stopped cleanly: an ex-primary's writes | exit 0 and a correct B |
| kill | 25% | the refresh's whole process group is SIGKILLed after a random 0.05–3.0 s; B is judged as the kill left it (the kill rule below), then the refresh is rerun | the kill rule holds, then exit 0 and a correct B |
| corrupt | 10% | 1–16 random bytes overwritten in 1–3 random relation files of B | exit 0 and a correct B |
| truncate | 10% | a random relation file of B truncated to a random length, or deleted | exit 0 and a correct B |

3. **The refresh:** `pgdelta local --source S --source-kind live --pg-conn ... --basis B --in-place --pg-controldata ... --stats-json ...`, with the harness's wall clock read just before it is launched and just after it exits.
4. **The checks of "a correct B"**, independent of pgdelta's own verification, on a disposable APFS clone of B that the harness starts (B itself is never started, so the next trial gets it as the refresh left it):
   1. the clone starts, and its log says it recovered from the backup: `completed backup recovery with redo LSN <start> and end LSN <stop>`, with the LSNs pgdelta reported;
   2. the load's invariant holds: sum(abalance) = sum(n), one `load_progress` row per client, the account count unchanged, no rolled-back 10^9;
   3. **nothing committed before `pg_backup_start` is missing:** if S's ledger has a client's row k+1 with a ts before pgdelta's `start_requested_utc` (the moment just before it sent `pg_backup_start`), that client's n in the copy is at least k (row k+1 began only after row k had committed);
   4. **nothing begun after `pg_backup_stop` is present:** each client's last transaction in the copy has a ts before pgdelta's `stopped_utc` (the moment just after `pg_backup_stop` returned);
   5. the copy's rows are S's: each client's last `(n, ts)` is S's ledger row `(n, ts)`, and the copy's ledger holds that client's rows from S's first kept row to n with no gap (a copy that kept B's own transactions, or mixed two histories, fails here);
   6. pgdelta's reported times lie inside the harness's own launch and exit times, so the bounds of items 3 and 4 are at least as sound as the harness's;
   7. the static digest of step 1 is equal (an unlogged table must exist and be empty: recovery resets it);
   8. `pg_amcheck --all --heapallindexed` finds nothing, in every database;
   9. no pgdelta replication slot and no pgdelta session is left on S 10 s after the run ends.

The bounds of items 3 and 4 hold for any correct copy whatever point between start and stop its recovery ends at; they don't say which transactions in between it holds, and nothing correct would.

## The kill rule, written once from EXP-17's three revisions

EXP-17's registered kill rule was revised three times after its stop rule fired on safe behaviour: an untouched B started (the kill came before pgdelta changed anything), a complete B started (the kill came after the commit, while the process was exiting), and a B holding only pgdelta's marker started (the kill fell between writing the marker and renaming `pg_control`). Each time the rule was narrowed to the property pgdelta documents: B starts only as the old copy or as the complete new one. Here the rule is written to that property from the start, and judged without byte identity with S, which a live S never has:

- At a kill before the process exits, every regular file of B, its WAL and `backup_label` included, is compared with B before the run, ignoring pgdelta's marker and its temporary file at B's root (EXP-17's third revision). If nothing else differs, B is **old**, and no more is asked of it.
- Otherwise a disposable clone of B is started. If it refuses to start, B is **blocked**: a pass. If it starts, it must pass every check above (items 2–5, 7 and 8), with the harness's launch time and kill time as the bounds, since a killed run's statistics may not exist: then B is **new** (killed after the commit), a pass. A B that starts and fails any check is a **wrong output**.
- The harness records which files differed at each kill (`diff_at_kill`), so any trip of this rule can be judged from the record.

## The negative control

Every 20th trial number (10, 30, 50, …) is the negative control instead of a counted trial: fault none, and pgdelta's deliberately broken variant, reachable only through the environment (`PGDELTA_TEST_READ_BEFORE_START=1`): it lists the data directory and copies its files aside before calling `pg_backup_start`, and sends them from that copy. Pages and transaction status changed between that read and the backup's checkpoint are never replayed, so its outputs should be wrong. It is **caught** when pgdelta exits 0 and any of the checks above fails (a copy that doesn't start counts). A negative-control refresh that doesn't exit 0 is recorded and is neither caught nor missed. The next trial then refreshes the wrong B like any other.

**Registered:** the checks must catch the broken variant in **at least 90%** of the negative-control trials that ran to completion (at least 45 of 50 in a full run). If they catch fewer, the checks are too weak for this load and no bound is claimed from the campaign; the shortfall is reported.

## Outcomes and rules

- **A wrong output** (exit 0 with a B that fails a check, or a B caught part-way that starts and fails one) stops the campaign; the trial is kept with its seed and its record. It is reported, its cause found, and the bound is not claimed. Seeds fix the DDL, the faults and the kill delays, not the load's timing, so a reproduction is by its record and by repeating the same circumstances.
- **A failed refresh** (a non-zero exit in a counted trial) also stops the campaign: none of the faults is meant to make pgdelta fail, since each is something a refresh repairs.
- **A cleanup failure** (a pgdelta slot or session left on S after any run, killed runs included) stops the campaign as a defect, reported apart from wrong outputs: it would hold WAL on S indefinitely.
- **A harness error** (a step of the harness itself failing) is recorded and the trial is not counted; ten in a row stop the campaign.
- **The stop rule,** as EXP-17's: run for 3 hours or 1,000 counted trials, whichever comes first; the count and the bound are what that gives.
- **Rule 8:** any change to these rules after seeing results is recorded here as a revision, with the evidence, before the campaign is rerun.

## Prediction

No wrong output, no failed refresh and no cleanup failure. Every kill leaves B old, blocked or new. The negative control is caught in every trial (90% registered).

## Limitations, known before running

- One small cluster (about 60 MB plus the ledger's last 2 minutes), so files are few and short; size-dependent behaviour above a few relation segments wouldn't show.
- One load: 4 clients at 2,000 transactions per second, writing three tables. No DDL runs during a refresh (by design, "between refreshes"), so `CREATE DATABASE`, `DROP DATABASE`, `VACUUM FULL` and the like inside backup mode are not exercised; nor are tablespaces, a standby source, or a timeline switch.
- Sender, receiver, S and B on one Mac (APFS), PostgreSQL 17.11. S runs with `fsync=off`: S is never crashed, and what is tested is the copy, not S's durability. Process kills only, not host crashes (T13c).
- Items 3 and 4 compare timestamps from the host's wall clock, taken by PostgreSQL and by pgdelta; a clock step during a trial would weaken them (none is expected on a laptop over a few seconds).
- B is usually one trial old, except after a fault or a negative control; an old B many refreshes behind is not drawn.
- Correctness only: no timings are claimed, and the load's slowdown is T8's second half.

## The smoke run (October 25, before the campaign)

*To be filled in after the smoke run: about 10 counted trials plus negative controls, and every harness fix it needed.*
