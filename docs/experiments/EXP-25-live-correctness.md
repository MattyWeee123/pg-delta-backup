# EXP-25: Does v1's refresh from a live source ever produce a wrong copy? A randomized campaign under load

Status: **done October 25, 2026 (second run 11:19–12:36 local; 1,110 trials). v1's in-place refresh from a live source gave no wrong output in 1,000 counted trials, every one spanning 8–19 WAL segments with a timeline history file: below 0.69% per refresh at 99.9% confidence (0.30% at 95%). Both negative controls met their registered bar: read before `pg_backup_start` caught 55 of 55, read while it runs 50 of 55 (90.9%, against 90%, by one trial).** Pre-registered before any campaign run; two settings revised after the smoke, the design after a code review, and two harness faults fixed during the campaign (all recorded below). Local (a Mac, PostgreSQL 17.11); no VMs. It is EXP-17's campaign moved to T8's case: the source keeps running and writing while pgdelta reads it in backup mode (`--source-kind live`, built for this; [pgdelta/README.md](../../pgdelta/README.md#reading-a-running-server-in-backup-mode---source-kind-live)), and every output is checked independently of pgdelta by recovering a disposable copy of it.

## Question

When v1 refreshes an old copy B in place from a running source S (`pgdelta local --in-place --source-kind live`) while S runs a continuous write load, is the result ever wrong without the tool saying so, across random DDL between refreshes and injected faults on B? Does a refresh killed part-way leave B either as it was, blocked from starting, or (if the kill came after the commit) a correct copy? And do the checks catch a deliberately broken variant that reads the files before `pg_backup_start`, as SPEC section 2 says it must not? *(Revised after the review, before the campaign: or that reads them while `pg_backup_start`'s checkpoint is still writing.)*

## The claim it can support

As in EXP-17: if N counted trials give no wrong output, a refresh drawn from this campaign has a chance of a wrong output below 1 − 0.001^(1/N) ≈ 6.9 / N at 99.9% confidence (3.0 / N at 95%). Here the trials are seeded but not independent: S's load runs on across them and B carries over from one trial to the next. The bound then reads: a refresh whose chance of a wrong output was at least p in every trial would have shown one in N trials with probability at least 1 − (1 − p)^N. It covers this distribution of load, DDL, faults and sizes on this platform, and says nothing about conditions outside it (see the limitations). It holds only if the negative control is caught as registered below.

*Revised after the review, before the campaign (October 25):*
- *It holds only if both negative controls are caught as registered below.*
- *It covers a refresh's WAL range (several segments, the stop LSN's page in a later segment than the start's, a timeline history file) only if at least 90% of the counted refreshes span two WAL segments or more (`summary.json`, `wal_range_covered`). Otherwise the bound is claimed without the WAL range, which then rests on the unit tests.*
- *It does not cover the order of `pg_backup_start`'s return and the first read. On this Mac the immediate checkpoint that pgdelta asks for writes its 800–1,500 dirty buffers in 3–10 ms, before a read of the data directory could even finish listing it (170–390 ms). So a pgdelta that read while `pg_backup_start` still ran would almost always make a correct copy here: in probes of that variant, 2 of 61 copies were caught with the smoke's 1 MB of shared buffers, 0 of 30 with 32 MB and 1 of 25 with 128 MB. That order rests on the code (`LiveSource.start` returns only with the server's answer, and nothing is listed before it) and on the stub test that makes `pg_backup_start` take a second and checks that nothing was read meanwhile. The second negative control shows only that the checks catch what such a read produces when the start checkpoint is still writing as the files are read, as on a slower disk or with larger shared buffers.*

## Setup

- **Template** (made once): `initdb`, a database `bank` with `pgbench -i -s 3` (300,000 accounts, all balances 0), EXP-25's load tables (below), a table `notes` of 20,000 rows, and `amcheck`; about 60 MB. *Revised after the review, before the campaign: `initdb --wal-segsize 1` (1 MiB WAL segments), and the template then moved to timeline 2 by an archive recovery that finds no archive (`recovery.signal`, `restore_command = 'false'`). Each refresh's 8–16 MB of WAL then spans many segments, with a history file to send; with 16 MB segments, 18 of the first two smokes' 20 counted refreshes spanned a single segment, as did all 15 refreshes of the review's own smoke.*
- **S**: an APFS clone of the template, started once with `fsync=off`, `max_wal_size=256MB` and `checkpoint_timeout=30s` (so checkpoints also fall inside some refreshes), and kept running for the whole campaign. *Revised after the smoke run, before the campaign: also `shared_buffers=1MB`, `bgwriter_delay=10ms` and `bgwriter_lru_maxpages=1000`, so the load's pages reach the files while pgdelta reads them (see the smoke run). The copies the checks start use the same settings.* *Revised after the review, before the campaign: `shared_buffers=8MB` instead of 1 MB, so that some of the load's changes are still only in shared buffers when a backup starts, which the second negative control needs; the files still change while they are read (see the review revisions).*
- **The load** ([experiments/correctness/bank.py](../../experiments/correctness/bank.py), after EXP-12's): `pgbench`, 4 clients, open loop at 2,000 transactions per second, for the whole campaign (restarted and recorded if it ends). 99 in 100 transactions commit: each moves money between two accounts and adds exactly 1 to the sum of balances, adds 1 to its client's counter `n` in `load_progress` with `ts = clock_timestamp()`, and appends the ledger row `(client, n, ts)` to `load_ledger`, taking its locks in one order so none deadlock. The other 1 in 100 adds 10^9 to an account and rolls back. In any consistent state, sum(abalance) = sum(n), no balance holds a rolled-back 10^9, and each client's ledger holds its rows without a gap up to its `n`.
- **B**: an APFS clone of the template at the start, then refreshed in place by every trial; each trial's B is the previous trial's output, with this trial's fault applied.

## Each trial (seeded; the seed is the trial number)

1. **DDL on S, between refreshes:** 1–4 random steps from `CREATE TABLE ... AS`, `CREATE UNLOGGED TABLE ... AS`, `DROP TABLE`, `TRUNCATE`, `VACUUM FULL` of a random table (the load's own tables among them, so a relation the load writes gets a new file), `CREATE INDEX`, `REINDEX TABLE`, bulk `UPDATE`, `DELETE` and `INSERT` on `notes`, `CHECKPOINT`, `CREATE DATABASE` (strategy `wal_log` or `file_copy`) and `DROP DATABASE`. Then S's ledger is trimmed to its last 2 minutes. Then the **static digest** is taken: every table the load does not write (row count and md5 of its rows sorted as text; an unlogged table only noted), the public schema's relations with their kinds, persistence and index definitions, and the list of databases. No DDL runs during a refresh, so the refresh cannot change any of it.
2. **A fault on B**, drawn at random:

| Fault | Share | What is done to B before the refresh | Pass means |
|---|---:|---|---|
| none | 40% | – | exit 0 and a correct B |
| diverged | 15% | B is started (it recovers from its own `backup_label`), runs its own bank load for 0.3–1.5 s, gets a table and an `UPDATE` of its own, and is stopped cleanly: an ex-primary's writes | exit 0 and a correct B |
| kill | 25% | the refresh's whole process group is SIGKILLed after a random 0.05–3.0 s (*revised after the smoke run, before the campaign: 0.05–1.0 s*); B is judged as the kill left it (the kill rule below), then the refresh is rerun | the kill rule holds, then exit 0 and a correct B |
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

The bounds of items 3 and 4 hold for any correct copy whatever point between start and stop its recovery ends at; they don't say which transactions in between it holds, and nothing correct would. A check that cannot run on the copy (a query on it fails) counts as a failed check.

## The kill rule, written once from EXP-17's three revisions

EXP-17's registered kill rule was revised three times after its stop rule fired on safe behaviour: an untouched B started (the kill came before pgdelta changed anything), a complete B started (the kill came after the commit, while the process was exiting), and a B holding only pgdelta's marker started (the kill fell between writing the marker and renaming `pg_control`). Each time the rule was narrowed to the property pgdelta documents: B starts only as the old copy or as the complete new one. Here the rule is written to that property from the start, and judged without byte identity with S, which a live S never has:

- At a kill before the process exits, every regular file of B, its WAL and `backup_label` included, is compared with B before the run, ignoring pgdelta's marker and its temporary file at B's root (EXP-17's third revision). If nothing else differs, B is **old**, and no more is asked of it.
- Otherwise a disposable clone of B is started. If it refuses to start, B is **blocked**: a pass. *Revised after the review, before the campaign: only if B is in pgdelta's own blocked state, `global/pg_control` absent with `global/pg_control.pgdelta-incomplete` and the marker `pgdelta-incomplete.json` present. A B that refuses to start in any other state (say, one committed before its WAL and `backup_label` were written) is an unrecoverable output: a **wrong output**. Without this, the rule would score exactly the in-place ordering it guards as a pass.* If it starts, it must pass every check above (items 2–5, 7 and 8), with the harness's launch time and kill time as the bounds, since a killed run's statistics may not exist: then B is **new** (killed after the commit), a pass. A B that starts and fails any check is a **wrong output**.
- The harness records which files differed at each kill (`diff_at_kill`), so any trip of this rule can be judged from the record.

## The negative control

Every 20th trial number (10, 30, 50, …) is the negative control instead of a counted trial: fault none, and pgdelta's deliberately broken variant, reachable only through the environment (`PGDELTA_TEST_READ_BEFORE_START=1`): it lists the data directory and copies its files aside before calling `pg_backup_start`, and sends them from that copy. Pages and transaction status changed between that read and the backup's checkpoint are never replayed, so its outputs should be wrong. It is **caught** when pgdelta exits 0 and any of the checks above fails (a copy that doesn't start counts). A negative-control refresh that doesn't exit 0 is recorded and is neither caught nor missed. The next trial then refreshes the wrong B like any other.

**Registered:** the checks must catch the broken variant in **at least 90%** of the negative-control trials that ran to completion (at least 45 of 50 in a full run). If they catch fewer, the checks are too weak for this load and no bound is claimed from the campaign; the shortfall is reported.

*Revised after the review, before the campaign (October 25):*
- *Two negative controls, each held to the same bar: **at least 90%** of its completed trials caught (at least 45 of 50 each in a full run), or no bound is claimed. In each block of 20 trial numbers, the one half-way through (10, 30, 50, …) is the control above, `before_start`, and the block's last (19, 39, 59, …) the second, `during_start` (`PGDELTA_TEST_READ_BEFORE_START=during`): it sends `pg_backup_start` asking for a spread checkpoint (`fast := false`), lists the data directory and copies its files while that checkpoint runs, and only then reads its answer. Pages the load changed before the checkpoint's redo point that the checkpoint had not yet written when they were copied are read stale and never replayed, and a `pg_xact` page created before the redo point may be missed. A spread checkpoint because an immediate one finishes before the listing does on this Mac (see the claim). Each `during_start` trial takes about 25 s, the spread checkpoint's length, so the 50 of a full run add about 20 minutes.*
- *A broken variant now always fails, with exit 4, even when the receiver committed B (a stray environment variable must not pass for a good copy), so a negative control **ran to completion** when pgdelta exits 4 with the variant named in its statistics (`live.test_variant`) and the receiver's final statistics present, which it sends only after the commit. It is **caught** when it ran to completion and any of the checks fails. Any other exit is recorded, neither caught nor missed, as before.*

## Outcomes and rules

- **A wrong output** (exit 0 with a B that fails a check, or a B caught part-way that starts and fails one) stops the campaign; the trial is kept with its seed and its record. It is reported, its cause found, and the bound is not claimed. Seeds fix the DDL, the faults and the kill delays, not the load's timing, so a reproduction is by its record and by repeating the same circumstances.
- **A failed refresh** (a non-zero exit in a counted trial) also stops the campaign: none of the faults is meant to make pgdelta fail, since each is something a refresh repairs.
- **A cleanup failure** (a pgdelta slot or session left on S after any run, killed runs included) stops the campaign as a defect, reported apart from wrong outputs: it would hold WAL on S indefinitely.
- **A harness error** (a step of the harness itself failing) is recorded and the trial is not counted; ten in a row stop the campaign.
- **The stop rule,** as EXP-17's: run for 3 hours or 1,000 counted trials, whichever comes first; the count and the bound are what that gives.
- *Revised after the review, before the campaign: **the WAL range.** The harness reports, in `summary.json`, how many counted refreshes span two WAL segments or more; below 90% of them, the bound is claimed without the WAL range (see the claim). It reports each negative control's catches the same way (`controls_met`).*
- **Rule 8:** any change to these rules after seeing results is recorded here as a revision, with the evidence, before the campaign is rerun.

## Prediction

No wrong output, no failed refresh and no cleanup failure. Every kill leaves B old, blocked or new. The negative control is caught in every trial (90% registered). *Revised after the review: both negative controls are caught in every trial (90% registered each), and every counted refresh's WAL spans two segments or more (90% registered).*

## Limitations, known before running

- One small cluster (about 60 MB plus the ledger's last 2 minutes), so files are few and short; size-dependent behaviour above a few relation segments wouldn't show.
- One load: 4 clients at 2,000 transactions per second, writing three tables. No DDL runs during a refresh (by design, "between refreshes"), so `CREATE DATABASE`, `DROP DATABASE`, `VACUUM FULL` and the like inside backup mode are not exercised; nor are tablespaces, a standby source, or a timeline switch. *(Revised after the review: S is on timeline 2 from the start, so a history file is sent every time; a switch during a refresh still isn't exercised.)*
- *Added after the review: the order of `pg_backup_start`'s return and the first read can't be exercised on this Mac (see the claim), and the WAL segments are 1 MiB, not the default 16 MiB, so a refresh spanning a single segment is left to the unit tests and the real-server tests.*
- Sender, receiver, S and B on one Mac (APFS), PostgreSQL 17.11. S runs with `fsync=off`: S is never crashed, and what is tested is the copy, not S's durability. Process kills only, not host crashes (T13c).
- Items 3 and 4 compare timestamps from the host's wall clock, taken by PostgreSQL and by pgdelta; a clock step during a trial would weaken them (none is expected on a laptop over a few seconds).
- B is usually one trial old, except after a fault or a negative control; an old B many refreshes behind is not drawn.
- Correctness only: no timings are claimed, and the load's slowdown is T8's second half.

## The smoke run (October 25, before the campaign)

Two smoke runs, each of 15 trial numbers with a negative control every third (`--trials 10 --negative-every 3`: 10 counted trials and 5 negative controls), records in `results/correctness/exp25-smoke*/` (git-ignored). The harness needed no fix to run: no harness error in either.

**The first smoke** (registered settings): 10 of 10 counted trials passed, and 5 of 5 negative controls were caught. Two things in its records made the campaign weaker than it was meant to be:

- **The files hardly changed while pgdelta read them.** In all 15 refreshes no page changed between being hashed and being sent (`pages_changed_after_hash` 0), and at most 3 files grew. With PostgreSQL's default shared buffers the whole cluster stayed in memory: `pg_backup_start`'s checkpoint wrote it out, and the files then changed only at the next checkpoint, 30 s away, while a refresh took 0.48–0.99 s. The live path's hardest cases (a page changed or torn between reads) were barely exercised.
- **Both kills came after the refresh had exited** (at 0.82 and 1.96 s): with refreshes of about 0.5–1.0 s, a delay of 0.05–3.0 s puts about four kills in five after the end.

**Revisions, before the campaign** (rule 8; none changes a check):

1. S, and the copies the checks start, run with `shared_buffers=1MB`, `bgwriter_delay=10ms` and `bgwriter_lru_maxpages=1000`, so the load's pages are written to the files continuously. Checked in a 6-trial probe and the second smoke: 29–157 pages changed between hashing and sending in every counted refresh, at the same load (about 2,000 transactions a second) and refresh time.
2. The kill delay is 0.05–1.0 s instead of 0.05–3.0 s.
3. A clarification of what the harness did from the start: a check that cannot run on the copy (a query on it fails) counts as a failed check.

**The second smoke** (revised settings), about 40 s:

| | Trials | As predicted | Notes |
|---|---:|---:|---|
| none | 3 | 3 correct | |
| diverged (B's own load and DDL first) | 3 | 3 correct | B started and recovered from its previous `backup_label` each time |
| kill, then rerun | 2 | 2 correct | one kill at 0.30 s caught B part-way (6 files differed): it refused to start ("could not find the database system"), and the rerun's B was correct; the other came after the exit |
| corrupt | 1 | 1 correct | |
| truncate | 1 | 1 correct | |
| **counted** | **10** | **10** | refreshes 0.48–0.66 s, of which 0.31–0.45 s in backup mode; 4,057–9,957 of 13,850–18,601 pages sent; one or two WAL segments; `pg_amcheck` clean every time |
| negative control (read before start) | 5 | **5 caught** | every copy started, and each failed several checks at once: rows of `pgbench_accounts` missing (3 to 558 of 300,000), the invariant broken, gaps of 200–300 rows in every client's ledger, and `pg_amcheck` reporting corruption (exit 2) |

In the first smoke one negative control's copy failed on its first query ("could not read blocks 0..0 in file ...: read only 0 of 8192 bytes"), which revision 3 counts as caught; the others were caught as in the second.

What the smoke shows, and doesn't: the harness runs end to end at about 3 s a trial (with EXP-17's extension running on the same Mac at the time, so no timing here is a measurement), so the stop rule's 1,000 counted trials (with about 50 negative controls) take about an hour, inside its 3 hours; the checks do catch the broken variant, by several independent signals each time. Ten trials bound nothing.

### Fixes after a code review (October 25, before the campaign)

A review of the build and of this design found six things; each was checked with a failing test or a probe first. Four changed pgdelta, and three changed this campaign's design (rule 8; the checks themselves are unchanged). The revisions are marked in italics above.

**In pgdelta** (branch `build/t8-live-fixed`):
1. **A receiver that stops held the source's WAL indefinitely.** A live `send` to a receiver that accepts and never answers blocked forever with backup mode and the temporary slot held (seen by the review on a real server; reproduced against the psql stand-in, still blocked after 15 s whether the receiver never answered or stopped reading). Now, from the slot's creation until it is dropped, the sender gives up on a receiver that makes no progress for `--live-timeout` seconds (default 120): a send it doesn't take, or a wait during which nothing arrives. The connection is aborted, `pg_backup_stop` called and the slot dropped (exit 3); over TCP the connection gets keepalives and, on Linux, `TCP_USER_TIMEOUT`. A real-server test does it in under 30 s with `--live-timeout 2`, leaving no slot. The server's `max_slot_wal_keep_size` is documented as the operator's backstop.
2. **A WAL page that changed after hashing was accepted.** A segment rewritten in place (same file, same size) after it was hashed was sent and committed with exit 0. Now any page of the WAL or a history file that differs, when sent, from what was hashed fails the run (exit 4). No real refresh can trip it: `pg_backup_stop` switches to a new segment before it returns.
3. **The broken variant exited 0.** One environment variable made a wrong copy with the same exit status and message as a good one. Now a run with either broken variant warns on stderr when it starts and ends, and exits 4 with status `failed` even when the receiver committed B. An unknown value of the variable is refused (exit 2).
4. **The second variant**, `PGDELTA_TEST_READ_BEFORE_START=during`, for the second negative control.

**In this design:**
1. **The negative control couldn't show the order SPEC section 2 asks for.** It read before `pg_backup_start` was even sent, which is always caught; a read while `pg_backup_start` still ran was almost never caught. Probes here explain why: the immediate start checkpoint writes its dirty buffers in 3–10 ms, before the listing finishes, so such a read makes a correct copy on this Mac (caught 2 of 61 with 1 MB of shared buffers, 0 of 30 with 32 MB, 1 of 25 with 128 MB, all with `fast := true`). The claim now says that this order rests on the code and the stub test, and a second control reads while a spread start checkpoint runs (`fast := false`). It needs changes still in shared buffers at the start: with 8 MB it was caught in 9 of 9 probe trials, while the counted refreshes still saw 30–191 pages change between hashing and sending (0 with 32 MB, which the first smoke's revision 1 was there to avoid). So S now runs with 8 MB. The first control was caught in 12 of 12 probe trials at 8 MB.
2. **The kill rule scored any B that refused to start as blocked.** A B committed before its WAL and `backup_label` were written would have passed. Now it is blocked only in pgdelta's own blocked state, and any other refusal is a wrong output.
3. **The WAL range was hardly exercised.** With 16 MB segments, 18 of the first two smokes' 20 counted refreshes spanned one segment. The template now has 1 MiB segments and is on timeline 2, and the bound covers the WAL range only if at least 90% of counted refreshes span two segments or more.

**The third smoke** (all of the above; `--trials 10 --negative-every 4`, so 18 trial numbers, 8 of them negative controls; records in `results/correctness/exp25-smoke3/`, git-ignored), about 2.5 minutes:

| | Trials | As predicted | Notes |
|---|---:|---:|---|
| none | 5 | 5 correct | |
| diverged | 3 | 3 correct | B recovered from its previous `backup_label` each time |
| kill, then rerun | 1 | 1 correct | the kill (0.67 s) came after the exit |
| corrupt | 1 | 1 correct | |
| **counted** | **10** | **10** | refreshes 0.44–0.77 s, of which 0.28–0.60 s in backup mode; 3,195–10,694 of 12,440–19,212 pages sent; 8–16 WAL segments of 1 MiB each, timeline 2 and one history file every time (`wal_range_covered`: 10 of 10); 13–127 pages changed between hashing and sending; `pg_amcheck` clean every time; no slot or session left |
| negative control, read before start | 4 | **4 caught** | exit 4 each time, as now registered; three copies started and broke the invariant, lost rows of `pgbench_accounts` (54 to 59 of 300,000) and had gaps in every client's ledger, with `pg_amcheck` reporting corruption; the fourth failed on its first query ("could not read blocks 0..0") |
| negative control, read during a spread start | 4 | **4 caught** | exit 4 each time; refreshes 22–27 s (the spread checkpoint); one copy didn't start ("WAL contains references to invalid pages"), and three started with `pg_amcheck` errors in `pgbench_accounts_pkey` or `load_ledger_pkey` (one "unexpected zero page"), one also with a gap in a client's ledger and one with a row of `pgbench_accounts` missing |

A kill-only probe of 8 trials then exercised the revised kill rule: two kills caught B part-way, both in pgdelta's blocked state (judged blocked), two before B changed (old), and four came after the exit; every rerun was correct.

What the third smoke shows: the revised harness runs end to end with no harness error, every counted refresh spans many WAL segments on timeline 2, and both controls are caught. The `during_start` control is caught mostly by `pg_amcheck` and by copies that don't start, and less often by the bank checks, so it depends on `pg_amcheck` more than the first control does. A full run's 50 `during_start` trials add about 20 minutes, so 1,000 counted trials should still fit in the stop rule's 3 hours. Ten trials still bound nothing.

## The campaign stopped at trial 391 (October 25, a harness fault)

- **Before it:** 352 counted trials (144 none, 86 kill, 57 diverged, 37 truncate, 28 corrupt), no wrong output; 39 negative controls (20 read-before-start, 19 read-during-start), all 39 caught.
- **What happened:** trial 390 was a negative control. As designed it left B wrong (its check found, among other faults, an index file `base/16384/17964` of 0 bytes). Every trial refreshes B in place from the previous trial's output, and every fault but one starts by refreshing it, which repairs such a B; trial 391's fault started B first (the diverged fault runs B's own load), and B's own query hit the empty file: a harness error, as the rules count it, not a judged trial. The script then crashed on that record's missing fault field while printing its progress line. The live source was untouched: it has no file 17964 at all.
- **The fix, before resuming:** after every negative control, the harness runs one normal refresh of B (not judged; its exit status is recorded as `repair_exit`, and a non-zero exit is a harness error), so a control's deliberately wrong B is never the next trial's starting point; and the progress line no longer reads a missing field. No check, fault mix or rule changed. The campaign resumes from trial 391 with the rest of its registered budget (648 more counted trials, or the rest of its 3 hours).

## The run stopped again at trial 790, and is restarted on the registered template (October 25)

**What happened.** Trial 790 was a `before_start` negative control. Its copy started, but the checker's first query never ended: it spun at full CPU for 3 h 22 min, inside `_bt_moveright`, following right links of the copy's torn `load_ledger_pkey`. That loop never checks for interrupts: `pg_cancel_backend` returned true and the backend kept spinning, so neither a cancel nor `statement_timeout` could stop it. Meanwhile, pg_amcheck, run with a time limit against the same copy, reported `cross page item order invariant violated for index "load_ledger_pkey"` in under a second. A fresh clone of the same B hung the same way, so the hang is a property of that wrong copy, not chance. No counted trial was affected: each of the 712 before it answered in under a second.

**Fix 1 (harness, no check changed).**
- Every query on a copy has a limit, `COPY_QUERY_SECONDS = 120`; a correct copy answers in under a second.
- A query past the limit is recorded as a problem with the copy. In a counted trial that is a wrong output, which stops the campaign. In a negative control it is a catch.
- pg_amcheck now runs even after a failed or hung query, with its own limit, `AMCHECK_SECONDS = 600`.
- A hung copy is stopped with `-m immediate` (SIGQUIT), which ends the spinning backend.
- Tested end to end on a fresh clone of trial 790's B, with the limit at 15 s:
  - the query hit the limit and was recorded;
  - pg_amcheck reported the index;
  - the copy stopped and was removed, all in 16 s.

**The summary found a second fault: the run used a stale template.** Only 4 of the 712 counted refreshes spanned two WAL segments or more (0.56%). The review revision (above) builds the template with 1 MiB WAL segments and promotes it to timeline 2. But the harness builds a template only when none exists, and the work folder kept the one built at 05:25 by the pre-registered harness: 16 MiB segments, timeline 1 (checked with `pg_controldata`). The review's code changes all applied: the second negative control, the stricter kill rule, the 8 MB shared buffers. Only the template-level revision was missing.

**What the first run is, then.** 712 counted trials, 0 wrong. Both negative controls were caught in 39 of 39 runs each, and the WAL range was not covered: a refresh's WAL sat in one 16 MiB segment, on timeline 1, with no history file. It is reported as its own result: below 6.9/712 = 0.97% at 99.9% confidence for that setup. It is not merged with the second run.

**Fix 2, and the second run.**
- The harness now checks an existing template with `pg_controldata` (segment size and timeline 2) and rebuilds it if it doesn't match. On the stale template the check returned false.
- The campaign is restarted from trial 0 on a fresh template, under the registered stop rule: 1,000 counted trials or 3 hours. Its output goes to `results/correctness/exp25b/`.
- Nothing else changed: the checks, faults, mix, seeds and thresholds are the registered ones. The multi-segment share in its summary shows whether the WAL range is covered.

## Results (the second run, on the registered template)

1,110 trials in 76 minutes: 1,000 counted and 110 negative controls, no harness error. Data: `docs/experiments/data/exp25-summary.csv`; raw `results/correctness/exp25b/`; figure `analysis/exp25.py`.

![1,000 counted refreshes from a live source, and the WAL each one sent](../figures/exp25-live.svg)

*How to read it:* left, every counted trial by the fault injected into B, green when the output passed every check; below them the two broken variants, blue when the checks caught their output. Right, how many WAL segments each counted refresh sent: grey, the first run on the stale template (one 16 MiB segment); orange, this run (8–19 segments of 1 MiB).

| Fault | Counted | Correct | Median refresh |
|---|---:|---:|---:|
| none | 414 | 414 | 0.50 s |
| B diverged (ran its own load and DDL) | 171 | 171 | 0.50 s |
| killed part-way, then rerun | 229 | 229 | 0.51 s |
| B corrupted | 82 | 82 | 0.51 s |
| B truncated or a file deleted | 104 | 104 | 0.50 s |
| **all** | **1,000** | **1,000** | |

- **The source changed under every read.** All 1,000 refreshes saw pages change between hashing and sending (median 75 pages), so every one exercised the WAL replay that makes a live read consistent.
- **The WAL range was covered.** Every counted refresh sent 8–19 WAL segments, with the start and stop LSNs in different segments, and the timeline-2 history file. The registered share was 90%; it was 100%.
- **Kills:** 102 of the 229 killed refreshes were killed before they exited. B was then blocked in pgdelta's documented state 76 times (`global/pg_control` renamed aside, with the marker), untouched 22 times and already complete 4 times; never anything else. The other 127 kills came after the exit. Every rerun was correct.
- **Timing:** pgdelta's own backup-start and backup-stop times lay within the harness's in 1,000 of 1,000 refreshes, so its bank state could be judged against the backup window.
- **The negative controls:**
  - *read before `pg_backup_start`:* **55 of 55 caught**. 43 failed the bank or digest checks, 7 copies didn't start, and 5 copies looped a query until the time limit (pg_amcheck then named the broken index in each).
  - *read while `pg_backup_start` runs:* **50 of 55 caught (90.9%)**. 28 failed the bank or digest checks, 13 didn't start, and 14 were caught only by pg_amcheck. 5 were missed (trials 319, 359, 479, 919 and 1039): their copies started, passed pg_amcheck, and kept the bank invariant.
- **The bound:** no wrong output in N = 1,000 counted trials gives below 1 − 0.001^(1/1000) = **0.69% per refresh at 99.9% confidence** (0.30% at 95%). It covers the WAL range, since the multi-segment share was met, and holds because both controls met their bar.

| # | Prediction | Verdict |
|---|---|---|
| 1 | No wrong output, no failed refresh, no cleanup failure | **Held:** 1,000 of 1,000 correct; no slot or session ever left on S |
| 2 | Every kill leaves B old, blocked or new | **Held:** 22 old, 76 blocked, 4 new |
| 3 | Both negative controls caught in every trial (registered: 90% each) | **Held for the read before start** (55 of 55). **Failed for the read during start** (50 of 55), which still meets its registered 90% |
| 4 | Every counted refresh's WAL spans two segments or more (registered: 90%) | **Held:** 1,000 of 1,000 |

**The first run, reported apart:** on the stale template (16 MiB segments, timeline 1), 712 counted trials, no wrong output; both controls 39 of 39. Without the WAL range, its bound is below 0.97% at 99.9% confidence. It is not merged with the second run.

## What the five missed controls may be

The `during_start` variant reads files while the start checkpoint is still writing, so the pages it reads stale are those the load changed before the checkpoint's redo point that the checkpoint hadn't yet written. Such a copy is wrong only if a stale page is never repaired. But with `full_page_writes` on, the first change to a page after the redo point puts the whole page into the WAL. Recovery then restores it, whatever was read. Under this load, most pages change again within the backup, so some of the variant's copies may contain no unrepaired stale page and be correct. The checks would then have nothing to catch.

Nothing in the five records separates them from the caught ones (read times, pages changed, WAL segments). *Tested afterwards in [EXP-27](EXP-27-control-oracle.md): with an oracle, a correct read of the same backup, each of 3 new misses was physically identical to the correct copy, and the checks caught all 37 wrong copies, flagging no correct one.* A direct test would have the variant also read every file after `pg_backup_start` returns, and count the pages that differ between the two reads, changed before the redo point, and are untouched by any WAL record up to the stop LSN. A missed control with zero such pages was a correct copy; one with any would be a blind spot of the checks.

## What follows

- **T8's correctness half is settled for this setup.** v1's in-place refresh from a live source (backup mode, WAL from start to stop, `backup_label`) gave no wrong output in 1,000 randomized trials under a continuous write load, random DDL between refreshes and injected faults: below 0.69% per refresh at 99.9% confidence, on one Mac with a small cluster (about 60 MB of data; see the limitations). Every part-way kill left B in a state the documented contract allows.
- **The checks are strong for reads before the backup starts, and good but not airtight for reads during it** (50 of 55). *EXP-27 found the misses to be correct copies: the checks caught all 37 wrong copies the variant made.*
- **T8's other half,** v1's speed and its slowdown of a live source with the load's client on its own VM, is T10b's.
