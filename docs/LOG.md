# Project log

A dated record of what was done, why, and what came out of it. Newest entries first.
Experiments have their own write-ups in [docs/experiments/](experiments/).

## 2026-10-24 (evening, later): EXP-12 rechecked; corrections applied

An independent recheck read EXP-12's write-up and its summaries (the ledger, the README, the experiments index and this log) against the raw data, from four sides: numbers, verdicts, summaries and departures from the registered text. 46 findings, 11 major. Each was checked against the run records, the pgbench logs and the sampler's records, and all held, some in part: 35 distinct corrections, listed in [EXP-12](experiments/EXP-12-source-slowdown.md#corrections-after-the-recheck-october-24-2026). No registered verdict changes, no code changed and nothing was rerun, so the curated CSVs and figures are unchanged; the pre-registered and dated pre-run sections are untouched. The main ones:
- **The warm block didn't show a larger miss:** warm `v1proxy`'s net values were within δ of its cold ones (E15 held). Row 7a, hashing, stands because warm `v1proxy` also missed against warm `full`, not because the loss grew.
- **`pg_stat_wal` shows the WAL's syncs slowing during the stall:** in every staged run, and in no other arm, fewer syncs completed during the capture's flush and each took longer on average (0.33–0.55 ms over the three 1-s reads spanning the flush, against 0.15–0.16 in the settles), while total sync time per second over each window stayed at its usual rate. That the load's commits waited on them is the likely reading, not shown in every run; why they slowed stays a hypothesis.
- **The fragile flag goes with the moderate meet and the warm reading too,** whose cells are flagged *client wait* in every run, as the HA caveat and R1 were already marked; and both registered decisions, T5's move and the worker cap, are fragile, not only the proxy's result.
- **The client's wait is read against its controls:** over a window `v1proxy`'s threads waited 0.84–1.02 s, against 0.27–0.74 s for `full` (0.27–0.35 at the same window length) and 0.56–0.69 for one worker, so the total doesn't by itself measure the client's share, since `full` lost nothing measurable; the worst second's wait (0.30–0.38 s against at most 0.10) was 4.5–19 times that second's lost work.
- **One worker's time against a full copy now carries its caveats:** the proxy sent 29.5 MB where v1 itself sent 449 MB here, and the bar is `pg_basebackup`, not the fastest copy measured, whose slowdown waits for T8, a caveat the pre-registration asked for with every result.
- **A6:** the cores reading is a hypothesis for T10b, and one worker lost about 1/60 as much per CPU-second as four (1/58–1/64), not 1/60 to 1/70.
- **In the ledger:** D81 moves from D to C by "contradicted in part", since decision 2 gives it a basis and A6 was judged; D58's note separates backup mode alone, which cost nothing measurable, from the proxy at 4 workers; T12's cost is now about 3.8 VM hours (3.5–4.3), an overrun that awaits the owner.

**The corrections were rechecked in turn,** from three sides (numbers, verdicts, consistency): 22 findings, 10 major, all checked against the same raw data, all held and applied, listed in EXP-12 beside the first recheck's. No registered verdict changes. The main ones: `pg_stat_wal`'s per-second counts lag, so syncs of "up to about a second" can't be read from them and the registered test for blaming the stall on WAL syncs is met only in part; the client's window wait was about 3 times as large for the proxy as for `full` at the same window length, not equal; one worker's time at 2 Gbit/s became about 8.7–10 s by inference, 0.73–0.84 of the full copy, with the session's end sizes, not 8.7; the warm differences are within δ, no change beyond δ shown either way; `full`'s p99 latency at every speed, with its spike as each copy starts, is now stated; and D50's reasons against X again include the moderate meet. A third recheck of those corrections (12 findings, 3 major, all held and applied, no registered verdict changes) widened one worker's time at 2 Gbit/s to about 8.7–11 s (0.73–0.92 of the full copy) by inference, since it adds a difference measured idle to a time under load, still with the session's end sizes, which the runs' own times would lower; gave the load's near-zero time as 1.1–1.3 s of each flush in all; and dropped the claim that the client's window wait grows with a copy's CPU.

## 2026-10-24 (evening): the two-VM limit lifted; a plan to firm T10a's results

The owner asked whether the plan would make EXP-12's fragile conclusions firm, then lifted the limit of two VMs: the project had set it itself, to bound cost and the reach of a mistake, and the GCP project is funded by Google. That limit is why EXP-12's load client ran on the source, which made its deciding rows fragile. The binding limit is now the project's quota, 32 vCPUs across all regions. The ledger's [Firming these results](DECISIONS.md#what-t10a-settled-october-24-2026) records the plan: T10b runs the load's client on a third VM, with the load levels interleaved and EXP-12's pgbench load beside HammerDB; T8 measures v1 itself on a live source the same way; and both run in two sessions on fresh VMs. The other guardrails stay: VMs labelled `purpose=pg-delta-benchmark` in `us-central1`, deleted after their results are fetched, and no change to IAM, firewall, billing or org policies.

## 2026-10-24 (evening): EXP-13's harness built and reviewed, then merged

T12's harness was built on local build branches, as EXP-11's and EXP-12's were, and reviewed: 12 findings, 3 major, all held, recorded in EXP-13's "Analysis readings". One was deferred, not rejected: the parsers' tests use hand-written `tc`, `ss`, `nstat` and stunnel output, and swapping in output captured on the Debian 12 VMs moves into EXP-13's smoke run (a dated rule 8 change there). After EXP-12's write-up, `build/exp13-fixed` merged into main cleanly (721e045); the whole suite passed there (1,407 tests, 7 skipped). Nothing ran on a VM. Its cost is about 4.3 days of harness work and about 3.8 hours of the pair (3.5–4.3), over the ledger's 2 VM hours; the owner hasn't accepted the overrun yet.

## 2026-10-24 (16:51–20:10 UTC): EXP-12 ran; T10a done; VMs deleted

**Ran** on two new `n2-standard-8` VMs in `us-central1-a`, created at 16:51 UTC and deleted at 20:10 UTC:
- **The smoke run,** 16:57–18:18: five invocations, 38 runs. Its sixteen items were recorded in [EXP-12](experiments/EXP-12-source-slowdown.md), dated, before the sweep. Two settings changed before the sweep: chrony was installed on both VMs (item 4's registered remedy, `chronyc makestep`, wasn't available on Debian's image, which runs `systemd-timesyncd`), and, as registered, the sampler's reads went to 0.5 s instead of 0.1. One registered action wasn't taken: loaddb grew, and its fillfactor wasn't changed, under rule 8.
- **The registered sweep,** `exp12-hot`, 18:23–20:08: one trial, 117 runs (103 under the load, 14 on the idle source), with the smoke run's values in the run line (busy load 18,100 transactions per second, moderate 9,000). This trial's capacity was 10.9% below the smoke run's (A12), so the busy load ran at 0.67 of it rather than 0.6.
- The results were fetched and checked equal to the VMs' copies (file counts and bytes, all six result sets) before the VMs were deleted. Nothing was rerun.

**Result** (the registered analysis, run as registered):
- **Every output was correct under load (G4).** 41 outputs of `pg_basebackup` and staged pgdelta taken under the load, and 4 from the idle source, passed `pg_verifybackup`, recovery, the load's balance invariant and consistent cut, coverage, row counts, schema and `pg_amcheck`. The load's sums held: Σabalance = Σn = 88,291,983 logged commits.
- **One `pg_basebackup` stream cost the busy load nothing measurable on O4's two numbers** at 0.5 or 2 Gbit/s or unthrottled. It is O4's bar; rule 3's fastest copy, EXP-11's bound, reads a stopped source and has no slowdown number until T8.
- **Staged pgdelta missed the owner's bar (O4) at every speed:** 1.25–1.35 s of the load's work, and a near-total stall of one second during its capture's flush, when the WAL's syncs slowed. The registered rule R1 fires, so T5 moves ahead of T8. Most of the loss is the capture, which v1 removes.
- **v1's source side, by proxy, missed the bar at 4 workers and met it at 1** (0.05–0.07 s of work and a p95 of 69–132 ms during the copy, against nothing measurable). So v1 needs a source worker cap on busy primaries, and T10b moves ahead of T8 as "source workers by load". At the moderate load, 4 workers met the bar. The HA caveat fires: v1 "slows a busy primary more than `pg_basebackup` does", by proxy at 4 source workers (fragile: its cell is flagged *client wait*).
- **Fragile:** the load's client ran on the source, and the deciding cells where W was measurable are flagged *client wait*, as are the moderate 2 Gbit/s `full` and `v1proxy` cells the Load row rests on and the warm `v1proxy` cell; leaving flagged runs out leaves nothing to judge. The rows stand, as registered, and so both registered decisions are fragile.
- **A6 reads "CPU-seconds is an adequate unit at this load".** By the rule's test its two comparisons split: one source worker's loss per CPU-second was about 1/60 of four's (1/58–1/64), under half, but 4 busy processes (`burn4`) lost 2.4–3.2 times as much per CPU-second as the proxy, above it, so the rule gives "CPU-seconds". Neither shows loss in proportion to CPU-seconds; both are consistent with loss following how many cores run at once, which is a hypothesis for T10b.

**Drift and the owner's decision.** loaddb grew to 11.6 times its size (`load_progress` from 32 to 44,775 pages), against smoke item 14's 5%, as it had in the smoke run. Before the fetch, the owner decided in chat: "Don't rerun now. Apply the committed drift check after the fetch, and propose a rerun with a load_progress fix only if it shows drift." The drift check, defined at 19:46 UTC during the sweep (8488bbc), its script and tests committed at 20:07 UTC (59a9f80), both before the sweep's logs were fetched, found none: the busy load's settles' median p50 went from 0.667 to 0.650 ms and p99 from 1.327 to 1.222; the moderate load's from 0.581 to 0.573 and 0.861 to 0.845. So no rerun is proposed. The growth did make the full copies larger (up to 0.3 GB over the busy block), which is why A10 marks the session shifted against EXP-11 on `full`'s idle time; per MB, `full` took about the same time as in EXP-11.

**In the ledger** ([What T10a settled](DECISIONS.md#what-t10a-settled-october-24-2026)): D50 (4 workers) moves from B to C, D68 (an idle source) from D to C and, after the recheck, D81 (the 1.3× CPU bound) from D to C, each by "contradicted in part"; O4, D03, D04, D10p and D58 get dated notes. The queue is now T12, T5, T10b, T8, by T10a's registered consequences (the moves of T5 and T10b are fragile).

**Cost:** about 3.3 hours of the pair, about 3.3 USD, against the budget of about 3.4 hours. The VMs are deleted. The write-up was rechecked independently afterwards (the entry above).

## 2026-10-24 (afternoon, later): EXP-12's harness built and reviewed

**Built** on local branches, as EXP-11's was: one agent for the load, the arms, the proxy for v1's source side and the correctness checks; one for `analysis/exp12.py`; then a merge with every test run. New: the fixed-rate load and its counters, loaddb, the source-side sampler and clock probe, the eviction that makes reads cold, the snapshot the proxy reads, and the seven negative controls of the correctness check, each failing through its own check on a local PostgreSQL 17.

**Reviewed** by three adversarial reviewers (fidelity to the pre-registration, measurement validity, robustness on the VMs): 17 findings, 3 major, all fixed, each with a regression test checked to fail without its fix. The major ones: a copy its own tool aborted wasn't counted as wrong; a run's 15-s view could reach into the next run; and stopping the proxy's backup-mode session raised an error on Python 3.11 and later.

**Tests:** 1,217, all passing (6 skipped), up from 1,072. The readings the code makes where the text leaves a choice are in EXP-12's "Analysis readings", dated, before any run.

**Not testable on the Mac,** so first exercised by the smoke run: Linux's page-cache eviction and `fincore`, the source-side sampler, and the load's logs on the VMs.

## 2026-10-24 (afternoon): T10a and T12 pre-registered as EXP-12 and EXP-13

**Why these two:** they head the queue re-ranked after T2. T10a measures the owner's bar (O4: a refresh slows a live source no more than `pg_basebackup` does) and checks every output under load. T12 measures DR's links, now v1's first target: what round-trip time, TCP buffers and TLS do to v1's path and to the full copies.

**How they were designed,** as EXP-11 was, before any code for them exists: for each test, two independent drafts (one for realism and decision value, one for measurement validity), merged into one; an adversarial review; a revision; an independent check of the revision; and a final pass. Where a fix offered options, the option that doesn't favour v1 was taken. The reviews and checks found 16 problems in EXP-12 (5 major) and then 9 more (2 major), and 12 in EXP-13 (4 major) and then 13 more (none major). All are answered in each write-up's "Revisions before running". Two examples of what they caught:
- **EXP-12:** a plain-text `pg_dump` has carried a random restrict key since PostgreSQL 17.6, so a schema check by hash would have failed every good copy.
- **EXP-13:** netem counts its child shaper's drops as its own, so a "zero drops" check would have discarded every run with raised TCP buffers.

**Expected results, written down before running:**
- **EXP-12:** the registered rule fires and moves T5 ahead of T8. The proxy for v1's source side most likely misses the bar on the worst drop if the source is busy above 50%, so a cap on source workers would follow (T10b moves up).
- **EXP-13:** with Debian's default TCP buffers, one pgdelta stream is window-limited at 60 ms, so v1 loses scattered changes there; with raised buffers it wins. One TCP stream (D36) would then be contradicted for DR, unless Omni's nodes raise their buffers: a choice for the owner.

**Cost if run:** EXP-12 about 3.45 days of harness work and about 3.4 hours of the VM pair; EXP-13 about 4.3 days and about 3.8 hours (3.5–4.3), over its 3.5-hour cut line and the ledger's 2 VM hours, an overrun the owner hasn't accepted yet. Nothing is built or run.

## 2026-10-24 (later): EXP-11 rechecked; corrections applied

An independent recheck read EXP-11's write-up and its summaries (the ledger, the README, the experiments index and this log) against the raw data: 33 findings, 12 major. Each was checked against the data, and all held, some in part: 25 distinct corrections, listed in [EXP-11](experiments/EXP-11-full-copy-baseline.md#corrections-after-the-recheck-october-24-2026). No registered verdict changes, and the pre-registered sections are untouched. The main ones:
- **The analysis's audit table counted example paths** (33 for `full_nosync`, 3 for `pg_probackup`'s own flush) instead of the audit's counts (992 of 992 files and 1,021 of 1,021 directory changes; 3 files and 2 directory changes). Fixed in `durability_audit.arm_results` and the analysis, with a test (commit d5a2fe6). The registered analysis was rerun as before: only its audit table changed, and the curated CSVs and figures are byte-identical.
- **`pg_probackup`'s slowness** was given a cause the timers don't support; its copy phase alone outlasts one stream's whole stream, for reasons not timed (SSH or its protocol).
- **T5 and T13:** the registered 4-job cut is now stated with the 8-job reading (in the band where they rank equal) and with the session shift it rests on; the ledger no longer says they can't change the HA verdict, and their place in the queue depends on the worker count (D50, T10b).
- **The released-copier claims** now name Debian's default write-back on dest, since one stream with write-back at 64 MiB beat v1; **the crossovers** name their 0.1 ms round-trip time; **D03** stays at B instead of moving to A.

## 2026-10-24 (06:27–09:34 UTC): EXP-11 ran; T2 done; VMs deleted

**Ran** on two new `n2-standard-8` VMs in `us-central1-a`, created at 06:27 UTC and deleted at 09:34 UTC:

| Invocation | UTC | Runs | For |
|---|---|---:|---|
| smoke: `exp11-smoke`, `exp11-smoke70`, `exp11-audit`, `exp11-audit2` | 06:32–07:03 | 89 | EXP-11's twelve smoke items, recorded in its write-up before the sweep |
| `exp11-local` | 07:04–08:53 | 348 | hot, then append: the main block, the extension and the warm-source block |
| `exp11-uniform` | 08:53–09:32 | 120 | uniform: the main block |

Every run of the sweep was verified, and every durable run passed the durability checks. The two invocations took 108 and 39 minutes, against 108 and 38 projected.

**The smoke run's two fixes,** both made before the sweep and recorded in EXP-11:
- **A WAL ticker for `pg_probackup`** (commit 288e26b). On the idle source, `pg_probackup catchup`'s WAL stream noticed the stop point only at its next timer tick, so each catchup idled 2–8 s after `pg_backup_stop`. During `probackup*` runs only, the source now gets one empty WAL message every 0.1 s, as a live primary's WAL would arrive. It changes no table or data file.
- **The syscall audit** stopped on real `strace` output: a `close()` of a descriptor inherited from the parent raised `KeyError`. Fixed with a regression test from the real trace line (commit 43da18c); the rerun audited every arm.

Also from the smoke run: the idle noise of dirty memory was 0.44 MiB, so check (a)'s limit stayed at 4 MiB; and the projection put the session at the 3.5-hour line, so cut 1 applied (`pg_probackup`'s byte diagnostic only in each trial's first repeat).

**Result** (details in [EXP-11](experiments/EXP-11-full-copy-baseline.md), summary in [What T2 settled](DECISIONS.md#what-t2-settled-october-24-2026)). It holds on one VM pair with local NVMe, for 2.1 GB in RAM, on an idle source, at 0.1 ms round-trip time, with v1 and the bound reading a stopped source:
- **The fastest durable full copy** was the parallel copier built as a bound: 1.88 s unthrottled. One `pg_basebackup` stream took 3.8–4.1 s (fsync or syncfs alike), and `pg_probackup catchup -j 4/8` over SSH, the only released parallel tool found, 5.8–6.1 s.
- **EXP-09's HA verdict reverses: out on fast links.** v1's path (stopped source, 4 jobs) takes 1.64–1.65 times as long as the bound for local changes, with no robustness flag. With dest at Debian's default write-back, it is faster than the released copiers tested (0.77–0.78 of one stream with syncfs, 0.79–0.81 of one with fsync, against EXP-09's 0.86). The cut it would need with v1 at 4 jobs, 1.40–1.42 s, is more than T5 and T13 can give together by the registered rule (at 8 jobs it is 1.05–1.07 s, where the rule ranks them equal), so the queue is re-ranked: T12 (DR's links) up to second, T5 and T13 down while the default stays at 4 jobs.
- **Crossovers, at 0.1 ms round-trip time:** v1's path beats the bound below 6.5 Gbit/s for local changes and 5.0 for scattered ones, and, with dest at Debian's default write-back, every released copier at every speed measured. DR's latency is T12's.
- **The session is shifted against EXP-09:** v1 took 14–18% longer for local changes (8–17% for uniform) and one `pg_basebackup` copy 22–30% longer unthrottled (its single TCP stream measured 4–11% slower), so every ratio is within the session.

**Found while writing it up,** and stated in EXP-11 beside the analysis's verdicts, which are unchanged:
- **The start and end reference digests differed in every trial,** in `template1`'s and `postgres`'s statistics catalogs and the commit log (most likely autovacuum; not confirmed), smoke trials included. Smoke item 12 had recorded them as matching. So a `pg_probackup` logical difference would have counted as void, not failed; none occurred.
- **The time until usable can't show where `--no-sync`'s flush goes:** the harness's verification maps every output file with `FIEMAP_FLAG_SYNC`, which writes it back, before starting it.
- **The analysis's E13 counts the negative control:** its "every arm passes the audit" includes `full_nosync`, which must fail. Every durable arm passed.

**Cost:** about 3.1 hours of the pair, at about 1 USD per hour for the two VMs together, so about 3 USD. Both VMs were deleted at 09:34 UTC.

## 2026-10-24 (night): EXP-11's harness built and reviewed

**Built** on local branches, each part by one agent in its own worktree: the core (the method tables, the `pg_basebackup` variants with timestamped phases, the durability checks (a) and (b), the probes, the trial's order with the extension and warm-source blocks, the write-back sysctl and its guard), the parallel copier `parcopy`, the `pg_probackup` arms with the syscall audit, and `analysis/exp11.py`. Then merged, with every test run.

**Reviewed** by three adversarial reviewers (fidelity to the pre-registration, measurement fairness, robustness on the real VMs): 16 findings, 4 major, all fixed with regression tests. The major ones:
- a copy the tool itself aborted was recorded as an error and escaped A4, so a failing arm could still have set a baseline;
- the "near the line" flag missed ratios that decide only together;
- the `pg_probackup` arms depended on a path that exists only when it is built from source;
- check (a) would have ignored the smoke run's idle noise.

**Tests:** 1,067, all passing (6 skipped: Linux-only or binaries missing), up from 864.

**Recorded in EXP-11 before running:** how the harness and the analysis read every rule where the text left a choice (its section "How the harness and the analysis read the rules").

**Not testable on the Mac,** so first exercised by the smoke run: Linux's write-back and `sendfile`, `strace` on real output, and the exact log lines of `pg_basebackup --verbose` and `pg_probackup` 2.5.16, which the parsers were written against from their source code.

## 2026-10-24 (later): T2 pre-registered as EXP-11

**Why T2 next:** EXP-09's HA verdict (v1 at 0.86 of one `pg_basebackup` stream) rests on a single-stream baseline, and rule 3 asks for the fastest durable full copy. T2 measures it.

**How it was designed:** three independent drafts, each from one angle (deployable tools, measurement, risk and scope), merged into one, then reviewed for feasibility and for fairness before any code exists. The reviews found 6 major problems and 15 minor; a check of the revision found 8 more, 2 major. All are answered in [EXP-11](experiments/EXP-11-full-copy-baseline.md)'s "Revisions before running". The ones that change what it can conclude:
- **`pg_probackup catchup` never flushes its streamed WAL or any directory** (from its source code), so its arm ends with a timed `sync -f`, and a syscall audit checks every arm's flushes.
- **"Out" is judged against the fastest copy measured,** as EXP-09 registered, not against released tools only, which would have favoured v1.
- **The HA verdict stays on v1's registered 4-job arm;** v1 at 8 jobs is reported beside it, since the baselines get their best stream count.
- **The deciding unthrottled cells get 10 runs each.**
- **The zstd part moves to T4,** offline; it answers a bytes question on another dataset.

**Expected verdict, written down before running:** Out on fast links against the parallel copier built as a bound, with v1 near the 0.9 line against the released tools. Confirmed is out of reach by arithmetic unless the hardware floor is far higher than probed.

**Cost if run:** about 2.75 days of harness work, then about 3.35 hours of the VM pair. Nothing is built or run yet.

## 2026-10-24 (later): commit history rewritten so the commits link to Matt's GitHub account

**Why:** GitHub showed "Matthew Wei authored and Matthew Wei committed" on most commits, linked to no account. This repository had no `user.email` set, so git used a made-up address from the laptop's hostname, and GitHub can't link an address it can't verify.

**What changed:** the 65 commits with that address (or a second hostname address) now carry Matt's GitHub noreply address, as author and committer, with the committer date set to the author date. Nothing else changed: every commit's files are identical, and teammates' commits keep their own names and addresses. But a commit's ID covers its parents, so 86 of the 91 commits on `main` have new IDs, back to October 9. This repository's git identity is now set, so new commits link too. At Matt's request, `main` was force-pushed on October 24; GitHub now links every commit on it to an account.

**Where old IDs are quoted:** this log, EXP-08 and EXP-10 now cite the new IDs. Teammates' specs, VALIDATION.md's CI records, the benchmark provenance files and the runs' `run.json` keep the IDs they recorded, since those record what actually ran; [the hash map](archive/2026-10-24-commit-hash-map.csv) gives the new ID for each.

## 2026-10-24 (later): four more rechecks; EXP-10's verdict on (b) corrected

**Why:** the first rechecks ran before EXP-10's XFS arm and the write-back rerun existed, and before the ledger, README and index were updated. So a second independent recheck covered those, against the raw sweeps and the write-ups. Each round of corrections was then checked again, because a correction can itself be wrong, and twice one was.

**Second recheck:** 21 problems, 4 major, corrected in commit cf7a740:
- **(b) was judged against its registration.** I had compared the rerun's fsync thread with the rerun's baseline and called (b) held for local changes. The dated note allowed the rerun to replace only the write-back measurement. Judged that way, (b) failed in all three workloads (the better alternative saves 0.68, 0.99 and 0.996 s). The rule's outcome is unchanged: the fsync moves to a thread. For local changes the saving sits on the 1.0 s line in every way of counting it (0.99–1.04 s).
- **The summaries had dropped caveats** that the write-ups keep: pgdelta ran in staged mode in T11, so its edge over `pg_probackup` is not yet shown from a live server; v1's 2.7 s was on a stopped source, and T2's registered prediction would reverse it.
- **Two grades:** D53 (copy-then-patch) goes from A to B, since it ties gap-fill and its XFS advantage is inferred; D87 duplicated D58 and is folded into it. The October 24 scorecard is now 6 A, 6 B, 5 C, 12 D, 3 X and 3 moot.
- **EXP-10 now reports the verifier's wait,** as its revision required.
- **One queue,** not two that disagreed, and a section for T11b.

**Third check, of those corrections** (2 major, 15 minor; commit 1349218):
- **My first explanation of the clone result was wrong.** I had written that about half of the write phase's saving on XFS comes back as the verifier reading cloned pages from disk. For local changes the write phase falls 2.8 s and the transfer only 1.4 s; the final wait for the verifier is only about half of the 1.4 s that doesn't reach the transfer. I replaced it with an explanation the fourth check then rejected (below).
- **The README listed two risks to v1's HA result** where EXP-09 names four (T2, T8, beyond RAM, persistent disks).
- **D01 goes back to C:** its positioning against the incumbents was never measured from a live server, which is T8's. D02's A was scoped to the incremental hand-off.
- Also: the figure compares the ext4 new-copy arms within one sweep; the summaries give pgBackRest's registered time-rule outcome; stale passages in the ledger carry dated notes.

**Fourth check, of the third round's fixes** (6 major, 11 minor; commit 1e3076a):
- **My second explanation still claimed more than the timers show.** It said the extra waiting was mostly hashing B, and that in place's similar wait showed reading cloned pages adds only 0.13–0.18 s. No timer counts disk reads, and the untimed part has no breakdown. EXP-10 now reports only what the timers show: the final wait for the verifier and the untimed time both grow, the background hashing and verifying take longer, and in place on ext4, with no clones, shows most of the same growth.
- **Ledger:** D02's pg_rewind half becomes its own row, D02b; the T9 persistent-disk note predicted the wrong direction (both methods move 2.1 GB through the disk, so the ratio should rise toward 1.0); T10a judges slowdown per source CPU-second; three dates corrected (arm (e) ran October 23).

**Fifth check, of the fourth round's fixes** (2 major, 12 minor; corrected in the next commit):
- **T9's new rule had dropped EXP-09's undecided band** (0.9–1.0) and its "both hot and append" condition; it now uses EXP-09's bands.
- **This log entry had again presented the 0.12–0.18 s difference in the final wait as a bound** on verifying uncached clones. The final wait only measures the verifier's backlog at the end; its busy time rose 0.20–0.27 s against in place. EXP-10 now says the comparison doesn't measure that cost and suggests a few tenths of a second.
- Also: D02b graded C for correctness and D for speed; T11b's ages, WAL rate and every outcome made explicit; T10a's "tracks" given a tolerance and its rule a threshold; two ranges re-rounded from raw values (0.12–0.18 s, 0.24–0.25 s); the README's block-incremental tie now notes its recovery time; pgdelta's README separates skipping fsyncs from skipping re-reads.

**Added:** the figure [exp10-xfs-and-rerun](figures/exp10-xfs-and-rerun.svg); the later sweeps' runs as [exp10-later-runs.csv](experiments/data/exp10-later-runs.csv); `exp10_later` now prints the tables and every derived figure EXP-10's later sections cite, with maxima and ranges.

## 2026-10-24: EXP-08, EXP-09 and EXP-10 done; VMs deleted

**Ran** on two `n2-standard-8` VMs in `us-central1-a`, created about 21:00 UTC on October 23 and deleted at 00:45 UTC on October 24 (about 3.75 hours each):

| Sweep | Runs | For |
|---|---:|---|
| `exp09-10-ext4` | 255 | EXP-09, and EXP-10's ext4 arms |
| `exp08-pgbackrest` | 162 | EXP-08 part B |
| T11 correctness, two WAL arms | 48 | EXP-08 part A |
| `exp10-xfs` | 75 | EXP-10 (d) |
| `exp10-ext4b` | 60 | EXP-10's drift check and the write-back rerun |

Plus two smoke runs. Every output was verified.

**Results:** summarised in [What T3, T8a and T11 settled](DECISIONS.md#what-t3-t8a-and-t11-settled-october-24-2026); details in each write-up.

**Independent rechecks** found 19 problems in EXP-09/10 (9 major) and 18 in EXP-08 (11 major). All were corrected before this entry. They included:
- my wrong explanation of a flagged `pg_statistic` page;
- pgBackRest's recovery time, missing from its totals;
- a bug in the early write-back arm, fixed with a test and rerun as registered.

**Infrastructure:**
- `pg_probackup` had to be built from source: Postgres Pro's repository didn't answer from Google Cloud, and PGDG ships PostgreSQL 18's libpq headers.
- `reformat_dest_ext4.sh` returns the destination to ext4 without recreating the VM.

**Cost** (Cloud Billing report for the project, last 30 days, read at 00:50 UTC on October 24): $18.20 of usage, $16.33 covered by credits, and a $1.87 subtotal not covered by credits. The sweeps above may not have finished posting. About $1 of the usage is E2 cores from two GKE clusters created in the project on September 25–26 for Omni tests, not from these experiments. No VMs, disks or clusters remain in the project.

**Commit history:** at Matt's request, `main`'s history was rewritten on October 23 to drop an attribution line, and force-pushed (see the entry below).

## 2026-10-23 (late night): the code for EXP-08/09/10 built, reviewed and merged; first sweep running

**Built** (each by one agent in its own worktree, then two adversarial reviewers, then fixes with regression tests):
- **Engine** (`pgdelta/inplace.py`, `flush.py`, `datadir.py`):
  - `--in-place` refreshes B itself behind a startup block. `global/pg_control` is moved aside until the commit and a marker file records the run, so PostgreSQL can't start on a half-refreshed copy, and a rerun finishes the job.
  - `--fsync main|writeback|worker` chooses where files are flushed.
  - `--source-kind datadir` reads a cleanly stopped data directory directly, with PostgreSQL 17's base-backup exclusions.
  - The reviews found 14 problems, 3 of them major: hard-linked files could be patched through a shared inode; two in-place runs could race on one B; `--in-place` would empty any directory. All are fixed.
- **Harness:** methods for T3 (five write-path arms), T8a (direct reads, with a new copy and in place), pgBackRest delta restore (three repository variants) and `pg_combinebackup --copy-file-range`, plus an XFS reformat script for the destination. The reviews found 14, 5 major: a reflink clone of B0 on XFS, pgBackRest still compressing on the network, and its recovery WAL going uncounted. All are fixed.
- **T11 runner** (`experiments/incumbents/`) and the scenarios S2b and S2c, with refusal classes, exposed-page counts and stale pages counted after recovery. Two rounds of reviews found 8 and 9 problems; all are fixed.
- **Tests:** 678 before, 863 after, all passing (5 skipped: Linux-only or binaries missing).

**A development run on the Mac changed EXP-08 before its registered run.** `pg_rewind` needs B's own WAL back to the fork, so it now runs in two arms (registered in EXP-08 first).

**Cloud:** `us-central1-b` had no stock, so the two VMs run in `us-central1-a`, with the same machine type and four local NVMe SSDs each. A small smoke run of every new method passed (20 of 20 verified). The main ext4 sweep for EXP-09 and EXP-10 (255 runs) started at 21:07 UTC.

**Analysis:** `analysis/exp09_10.py` was written and dry-run on the smoke data before the registered results existed. It computes every prediction and rule exactly as pre-registered.

## 2026-10-23 (late night): commit history rewritten to drop an attribution line

**Why:** at Matt's request, every commit message on `main` lost its "Co-Authored-By" line naming the AI assistant, and `main` was force-pushed. Nothing else changed: each rewritten commit has the same files, author, date and subject as before.

**What it changes:** the 43 commits from `cc1fb1a` onward have new IDs. Earlier commits keep theirs. Commit IDs recorded before the rewrite map as follows:

| Before | After | Where the old ID appears |
|---|---|---|
| `6936143` | `c5e2296` | the decision ledger's commit, cited in this log |
| `625285e` | `f9348df` | the owner-decisions commit |
| `80f01f5` | `6ec413f` | EXP-08/09/10's first pre-registration (EXP-08 now cites the new ID) |
| `42aa35f` | `fa29cbd` | the revised pre-registrations |
| `c48e853` | `b77f33d` | the code deployed to the VMs for EXP-08/09/10: recorded in each run's `DEPLOYED.json` and `run.json`. The tree hash recorded there is unchanged. |

## 2026-10-23 (night): sponsor questions closed by decision; next, T11, T8a and T3

**Why:** the decision ledger's first test, T1, was a list of questions for the sponsor. Matt went through them and settled each from the project brief and Google's public docs, so nothing waits on the sponsor any more.

**What Omni does today** (public docs, rechecked by an independent agent; [notes](research/notes/alloydb-omni.md)):
- After an unplanned failover, the operator deletes the old primary and seeds a new standby with `pg_basebackup`, which can take hours on a large database.
- After a DR failover, Google's procedure tries a rewind of the former primary and falls back to a full copy.
- No rebuild path keeps a usable copy when it fails: `pg_basebackup` deletes its partial output, a failed `pg_rewind` leaves its target unrecoverable, and Omni's documented remedies are delete-and-recopy.

**Decisions** ([ledger, O1-O9](DECISIONS.md#decisions-of-october-23-2026-t1-closed)):
- Target Omni's HA standby rebuild and DR re-seed; we define the sizes.
- Run only when an old copy exists; otherwise `pg_basebackup`.
- A program on the source is allowed. Its slowdown must be no worse than `pg_basebackup`'s.
- The old copy may be patched in place; a failed refresh leaves it blocked from starting.
- TLS on the wire. No GPL code; pgdelta is to be Apache-2.0 (no LICENSE file yet).
- T3's two staged-only arms are dropped, since v1 has no capture.

**T1's prediction:** "a source program is allowed" held; "B must survive a failed refresh" failed, so in-place patching becomes a candidate default for T3 to decide.

**Also:** [BEN-QUESTIONS.md](BEN-QUESTIONS.md) now lists the decisions and a few non-blocking questions. The October 22 version, whose draft post was never sent, is archived.

**Next, in order:** T11 (the incumbent tools: `pg_rewind`, the catch-up tools, pgBackRest), T8a (pgdelta reading a stopped source directly, no capture), T3 (the destination write path, including in-place patching). Each gets its predictions written down before it runs.

## 2026-10-23 (evening): decision ledger, every design choice graded by its evidence

**Why:** Matt asked whether v0 is the right direction, whether v1 builds on or scraps it, and whether design choices are backed by evidence or added blindly.

**How:** a multi-agent audit, read-only, with nothing re-run.
- It listed every design decision (89) and graded each by its evidence.
- It compared the direction against the research.
- Two adversarial reviewers challenged the draft, and it was revised.
- Its main new numbers were rechecked against the raw data before committing.

**The ledger:** [DECISIONS.md](DECISIONS.md).
- **Direction:** v0's way of deciding what to reuse (keyed fingerprints of every page, every output byte verified) is well supported. The staged capture is contradicted as a performance path. Writing a complete new copy is the largest pgdelta-specific cost, and it was never compared with an alternative.
- **v1:** builds on v0's correctness core. Replace the source path with direct reads in backup mode, and replace the write path. Whether to keep a custom engine or wrap rsync with pgdelta's verification is still open.
- **Evidence coverage:** of the 33 performance-driven decisions, 4 have strong evidence, 4 moderate, 10 weak, 14 none, and 1 is contradicted.
- **New measured fact:** on the VMs, writing O takes 3.3–3.5 s of pgdelta's 4.2–4.8 s transfer, split into copying B (1.6 s) and fsync (1.7 s). Hashing isn't the bottleneck there.
- **Committed timers:** pgdelta's per-phase timers from all 252 pgdelta runs are now in [exp04-pgdelta-timers.csv](experiments/data/exp04-pgdelta-timers.csv).
- **Next:** a ranked queue of experiments, T1 to T14, each with a prediction and a falsifier. It starts with questions for the sponsor and a measurement of how much a refresh slows the source.

## 2026-10-23 (later): closing EXP-06's open question

**Does pipelined verification help at the default of 4 processes?** EXP-06 had left this mixed: one busy re-run with 3 repeats had it costing 0.18 s for uniform.
- **Method:** predictions and a decision rule were recorded first. Then 10 interleaved runs per configuration on the kept EXP-06 pairs ([results](experiments/EXP-06-engine-throughput.md#results-of-the-check-run-october-23-2026)).
- **Result:** pipelined was 0.06–0.13 s (3–7%) faster in all three workloads, with one-sided permutation p = 0.05–0.08 each. Both numeric predictions failed: the effect was smaller than predicted for hot, and present, not absent, for uniform. The decision rule keeps `--verify pipelined` as the default.
- **Other loose ends from the reviews, and how they stand:**
  - EXP-07's C7-against-C6 comparison is closed as "no measurable difference". It changes no decision.
  - EXP-06's thin margins on the busy Mac were superseded by the Linux check and EXP-04/04b, which measured the engine on the VMs.

## 2026-10-23: EXP-04b done; VMs deleted; where the project stands

**EXP-04b** ([write-up](experiments/EXP-04b-default-checksums.md)): the same sweep with PostgreSQL's default CRC32C manifests. 324 runs, all verified.
- **What CRC32C changed:**
  - the full copy and the staged capture halved (8.3 → 4.0 s);
  - `pg_combinebackup` fell 34–38%;
  - pgdelta's and rsync's own transfers were unchanged.
- **Crossovers** (beats a full copy below):

| | Scattered changes | Local changes |
|---|---|---|
| pgdelta | 2.0 Gbit/s | 2.5 Gbit/s |
| rsync | 0.8–1.0 Gbit/s | 3.9 Gbit/s |
| incremental | 3.75 Gbit/s | every speed for hot; append lost narrowly at the unthrottled link |

- **Predictions:** R1–R4 and R7 held. R5 failed for local changes: rsync's crossover came out above the predicted range, where the arithmetic check written before the results put it. R6 held only in part: append lost narrowly at the unthrottled link.
- **A second independent recheck** of the EXP-04b write-up found 3 major and 9 minor overstatements, all fixed. The append result is a loss, not a tie. The hot/append difference is noise in the full copy between trials. Incremental's lead at full speed shrank.
- **Independent recheck:** two reviewers recomputed the EXP-04 and EXP-07 write-ups from the raw data and found 9 major and 22 minor overstatements. All are fixed.
  - N2 failed for every method but pgdelta.
  - N5 held in full under the serial reading.
  - N7 held only in part.
  - EXP-07 measured only the local capture, not every backup.
  - pgdelta's own transfer is the other half of its cost.

**The two VMs were deleted** at 00:05 UTC, after the results were fetched. No instances or disks remain. **GCP access and spend, checked through `gcloud` as Matt (read-only):**
- Project `postgres-delta-backup` is ACTIVE, with billing enabled on the sponsored billing account.
- No instances or disks remain.
- The audit log shows both VMs created at 10:08 UTC on October 22 and deleted at 00:07 UTC on October 23, so they ran 14.0 hours.
- At list prices that's about 15–16 USD: two `n2-standard-8` at about 0.39 USD/hour each, plus 4 local SSDs each at about 0.16 USD/hour.
- **Read from the console's billing reports** (Matt signed in; checked October 23 at about 00:45 UTC):
  - Usage processed so far for October 8–22 is $2.72: N2 core $1.16, N2 RAM $0.76, local SSD $0.72, disks $0.04, networking $0.03.
  - All of it was covered by the sponsored credits, so **$0.00 has been charged.**
  - That is only about 2.5 of the 28 VM-hours. Google posts usage with a delay of up to a day or so, so the rest of October 22 hasn't appeared yet.
  - The list-price estimate above (about 15–16 USD of usage) stands until it posts, and the credits are expected to cover it as well.

**Where the project stands:**
- **pgdelta v0 is correct and sends the minimum page data.** That's evidence from 756 runs between two machines plus the tricky-copy experiment.
- **It isn't yet fast enough to matter on fast links.** Where PostgreSQL's incremental backup can be used, it wins.
- **Where incremental can't be used,** pgdelta wins only for scattered changes on links below about 2 Gbit/s. rsync wins for local changes below about 3.9 Gbit/s. Both lose to a plain full copy above that.
- **Two design directions follow from the measurements:**
  - start the session before the capture, so the destination can hash and copy B while S is taken;
  - read the source's files directly instead of taking S.

## 2026-10-22 (night, cont.): EXP-04 done; the cause found (EXP-07); EXP-04b running

**EXP-04** ([write-up](experiments/EXP-04-network-sweep.md)): 432 runs between the two VMs, all passed and verified.

| | Beats a full copy below |
|---|---|
| pgdelta | 1.1 Gbit/s (scattered changes), 1.5 (local) |
| rsync | 0.6 (scattered), 1.9 (local) |
| PostgreSQL incremental | 3.0 (scattered), every speed (local) |

- **The surprise:** the local backup S that pgdelta and rsync start with took 8.5 s, as long as a whole full copy over the network. A staged method can't win on fast links while that holds.
- **Predictions:** N6 and N7 held. N2 held for pgdelta (within 7% in all 18 cells) but failed for the full copy. N3 and N5 held in part. N1 held in part (overhead 0.1–0.4%, not 2–6%). **N4 failed** (crossovers lower than predicted).
- **Correction to EXP-02:** on the wire, incremental streams only the WAL actually written. pgdelta and rsync must send a 16 MiB segment padded with zeros. So for local changes incremental sends the fewest bytes (17 MB against 29 MB), not pgdelta. A note was added to EXP-02.

**EXP-07** ([write-up](experiments/EXP-07-capture-speed.md)): why the capture was so slow.
- **The harness asked `pg_basebackup` for SHA-256 manifest checksums.** With PostgreSQL's default, CRC32C, the local capture takes 4.1 s instead of 8.6 s. The same setting applied to every full copy, capture and combine in EXP-04; EXP-04b measures how much each changes.
- **Reading the files directly with 4 parallel readers takes 0.73 s** (2.9 GB/s), 5.5× faster than even the CRC32C backup.
- **One mistake, caught and fixed:** the first attempt's direct-read variant listed `pg_wal`, whose old segments PostgreSQL recycled mid-run. It was fixed and re-run, and both attempts are kept.
- **Next design step:** read the source's data files directly, in parallel and in backup mode, instead of writing S. It needs its own correctness experiment (torn pages, WAL replay).

**EXP-04b** ([pre-registration](experiments/EXP-04b-default-checksums.md)): the same sweep with CRC32C manifests.
- Speeds 0.5–5 Gbit/s and unthrottled, 3 repeats.
- Predictions computed from EXP-04's phase times and EXP-07, recorded before the sweep.
- Started 22:24 UTC. The VMs are deleted when it ends.

## 2026-10-22 (overnight): Linux check done; EXP-04 sweep running

**pgdelta gets the same head start as rsync** (recorded in the [EXP-04 write-up](experiments/EXP-04-network-sweep.md) before the sweep):
- rsync copies the old copy at the destination while the source captures S. So now pgdelta's receiver starts during the capture and reads B into memory while it waits.
- It can't fingerprint B early: the key arrives with the sender's first message, by design.
- Any reading that happens between the two timed phases is measured and added to the checks.
- Incremental is now the only method that reads B cold on its critical path. Comparisons with it say so.

**EXP-06's Linux check** (on `pgd-dest`; predictions recorded first): all three held.
- 4 worker processes hash 3.9× faster than 1.
- 8 processes are only 4.5× faster, at 1.8× the CPU, because the 8 vCPUs are 4 cores with two hyperthreads each. So the default of 4 holds on Linux too.
- One VM core hashes at 0.55 GB/s, 2.4× slower than the Mac.

All 676 tests pass on the VM's Python 3.11.

**Full EXP-04 sweep started at 18:13 UTC** at commit `d559b1b`:
- 3 workloads × 8 link speeds × 6 methods × 3 repeats;
- link speeds and method orders shuffled;
- estimated 4–5 hours.

The VMs are deleted once the results are fetched. `analysis/exp04.py` was written and tested on the smoke data, so the figures and verdict numbers come straight from the sweep.

## 2026-10-22 (late night): EXP-06 halves pgdelta's fixed cost; EXP-04 tooling ready

**EXP-06** ([write-up](experiments/EXP-06-engine-throughput.md)), on the Mac, inputs cached:
- **What changed in the engine:**
  - hashing runs in 4 worker processes;
  - each output file is built by copying the old file in bulk and then patching the changed pages;
  - finished files are verified in parallel.
- **Result:** a 2.1 GB refresh with 1% of rows changed went from 4.37 s to 1.40 s for scattered changes, and from about 3.0 s to 1.3 s for local ones. Outputs were byte-identical in all 270 benchmark runs (two passes plus the re-run).
- **Modelled crossover against a full copy:** from 2.8 to 8.6 Gbit/s for scattered changes, and to about 13 Gbit/s for local ones, level with rsync.
- **Five of seven predictions held. Two held only in part:**
  - copy-then-patch helps only scattered changes;
  - 8 processes went over the CPU bound on the Mac's mixed cores, so the default is 4, not the best-measured 8.
- **Still open:** parallel verification's benefit at the default of 4 processes is unproven.
- **The reviews caught two major problems,** both fixed:
  - on Linux, worker CPU would have gone uncounted, because the workers would have been started by a separate server process;
  - an argument for the default rested on a page-cache artefact.
- **Gap found in the method:** reading the inputs once before the runs didn't keep them cached, so every run now re-reads its own inputs first. EXP-05's numbers may include some cold reads.

**EXP-04 fairness fixes are merged** (two review rounds, re-validated on the VMs):
- rsync now pays for copying the old copy and flushing it to disk;
- peak memory is measured per tool;
- link speeds run in random order;
- pgdelta's two engines (the new default and EXP-02's single-threaded one) are pinned by their flags.

**Privacy:** the benchmark records listed other programs running on the Mac. The repository is public, so committed data and write-ups now use generic labels ("desktop application", "macOS system process").

**Next:** give pgdelta's destination the same head start rsync gets: start the receiver, and fingerprint the old copy, while the source takes its backup. Then check the Linux engine on a VM, and run the full EXP-04 sweep.

## 2026-10-22 (night): EXP-05 failed; EXP-06 designed

**EXP-05, parallel hashing with threads** ([write-up](experiments/EXP-05-parallel-hashing.md)): three of four predictions failed.
- Results stay identical for any thread count, but 2–4 threads gain at most 1.2×. **8 threads is 1.5× slower than 1 and uses 3× the CPU.**
- **Cause:** Python's interpreter lock is taken back after every 8 KiB page, and the hand-off costs more than the hash.
- **Second finding:** writing the output is as slow as hashing. With scattered changes, the destination copies tens of thousands of small runs of unchanged pages one by one, and then re-reads everything to verify it. This also explains why EXP-02's uniform 1% runs were slower than 5–10%.
- **The review found a deadlock** in the thread pool's shutdown, and the default of 8 threads is the slowest setting. The branch isn't merged as is.

**Next ([EXP-06](experiments/EXP-06-engine-throughput.md), pre-registered):**
- hash in worker processes;
- build each output file by copying the old file in bulk and then patching the changed pages;
- verify finished files in parallel while the next one is written.

Each change can be switched off, so its share is measured. Target: uniform 1% from 4.55 s to at most 2 s.

**EXP-04 waits for this,** so the sweep on real links measures the engine that would actually ship, alongside one-thread pgdelta. The VMs keep running meanwhile (about 1 USD/hour).

## 2026-10-22 (night): EXP-04 tooling built; fairness fixes before the sweep

**Built and smoke-tested** ([experiments/network/](../experiments/network/README.md)):
- Two `n2-standard-8` VMs in `us-central1-b`, each with 4 striped local NVMe SSDs. Other zones had none in stock with 2 SSDs.
- **Running now, about 1 USD/hour together,** until the sweep is done. They'll then be deleted with `infra/gcp/teardown.sh`.
- The smoke run (scale 5, 30 method runs) passed: every output was verified, staged outputs were byte-identical to S, and B0 was untouched. At that size fixed costs dominate, so its timings only check the tooling.

**The fairness review found 2 major and 4 minor problems**, all being fixed before any real data is collected:
- **rsync's timer was narrower than the others'.** Copying B0 and flushing the copy to disk happened outside it, and rsync didn't flush its own writes. pgdelta, `pg_basebackup` and `pg_combinebackup` all build and flush a complete copy inside their timers. Left as is, rsync would have looked faster than it is.
- **Peak memory was wrong for every non-Python tool:** a child process reports its parent's peak memory from before it started.
- **Minor:**
  - SSH launch overhead counted against the staged methods only;
  - the 16 MiB WAL segment each staged copy must send wasn't recorded separately;
  - link speeds always ran in the same order;
  - one rsync log overwrote the other.

[EXP-04's predictions](experiments/EXP-04-network-sweep.md) were recorded before the smoke run's numbers were looked at.

## 2026-10-22 (evening): EXP-01 and EXP-02 done

**EXP-01, how many pages change** ([write-up](experiments/EXP-01-change-patterns.md)), 37 trials at about 2.1 GB:
- The simple model `1 − (1 − f)^(rows per page)` predicted changed heap pages within 0.4 percentage points at every fraction.
- **Local changes** (hot rows, appends, retention, ranges) left 99% of pages untouched: a refresh sends about 1% of the database.
- **Scattered updates** dirty most of a table once more than about 2% of its rows change. Then no page-level method can avoid resending the table.
- 8 KiB comparison halves the bytes against 64 KiB chunks for scattered changes.
- PostgreSQL's change tracker missed no changed block with checksums on. With checksums off, a read scan changed 100,001 pages through hint bits alone, which the tracker (correctly) ignores.
- Every prediction held, except that index pages changed at about 2f, not f, for clustered updates.

**EXP-02, comparing methods on the same inputs** ([write-up](experiments/EXP-02-local-comparison.md)):
- **Bytes:** pgdelta's page data equalled rsync's (8 KiB blocks) to the byte in all 37 trials, and was 0.5–20% below PostgreSQL incremental, whose extra is exactly its "tracked but unchanged" blocks. Default rsync sent 1.7–2.5× more for scattered changes.
- **Compute is the real difference.** pgdelta spends a nearly fixed 3–5 s per 2.1 GB (hashing every page on one core, three passes, plus writing a full new copy). rsync takes 1.2 s for local changes but up to 10 s for scattered ones.
- **Model of time vs. link speed:** pgdelta beats a full copy below about 2.5 Gbit/s for scattered changes and 5.5 Gbit/s for local ones; rsync below 1–1.6 and about 13 Gbit/s respectively.
- **Wrong predictions:** rsync's checksum traffic back to the source was six times lower than predicted (0.04%), and default rsync's penalty was 1.7×, not ≥2×, at 1% of rows. One open question: pgdelta was slower at 1% of rows than at 5–10%.

**Decision:** pgdelta's fixed hashing cost is the bottleneck, so the next change is parallel hashing ([EXP-05](experiments/EXP-05-parallel-hashing.md), pre-registered before building it). Leaf-fingerprint truncation would cut back-traffic for scattered changes, but at 0.1% of the database it can wait.

**Housekeeping:** figures are now byte-stable across reruns (fixed SVG ids, no timestamps, deterministic line order), so regenerating them only shows in git when the data changes.

## 2026-10-22 (later): Engine built and reviewed; EXP-03 done

**Built, each in its own worktree and each reviewed by a separate agent that tried to break it:**
- **`pgdelta/`, the v0 engine:**
  - keyed BLAKE2b hash tree (8 KiB pages, 1 MiB nodes);
  - framed sender/receiver protocol over pipes or TCP;
  - verified rebuild into a staging folder, renamed into place only after every file checks out.
- **`experiments/change_patterns/`, the EXP-01 study tooling,** plus `compare_methods.py` for EXP-02.
- **`benchmarks/methods.py`, pluggable backup methods,** with two new ones: `rsync_staged` and `pg_delta_staged`.
- **`experiments/tricky_copies/`, the EXP-03 scenarios.**

**Reviews found 4 major and 22 minor problems.** Every one was reproduced before being fixed, then fixed with a regression test that failed first, and the fix was re-reviewed. The majors:
- **Engine:** on macOS's case-insensitive disk, `--output basis/O` could create O *inside* the old copy `Basis`. Placement is now checked by filesystem identity, not path text.
- **Harness:** `pg_verifybackup` doesn't check every file (it skips `postgresql.auto.conf`, `*.signal`, extra WAL), so a wrong rebuild could pass. The harness now also compares the output with the fresh backup file by file.
- **Harness:** an output published as a symlink to the source passed every check. Now refused.
- **Study:** the tracker's per-file block lists were parsed and then thrown away. Now kept in the trial JSON.

The test suite went from 133 to 478 tests, all passing.

**EXP-03 results** ([write-up](experiments/EXP-03-tricky-copies.md)):
- **The tool was correct on all ten tricky old copies:** byte-identical, `pg_verifybackup`, restore and data comparison.
- **LSN skipping went wrong in four of the eight same-cluster scenarios.** After a failover it would have kept 2,371 pages of stale data. Two of my seven pre-registered LSN predictions were wrong; the write-up says so and explains why.

**Smaller decisions:**
- **The identity check is a policy, not a safety requirement.** Every output file is verified against the source, so reuse from a "foreign" old copy can't make the result wrong. `--reuse-any-basis` turns the policy off; across a 17→18 upgrade it reused 53% of pages.
- **Known residual issues, accepted for now (all minor):**
  - In `local` mode, a signal arriving right after publication reports failure even though O is complete; the receipt beside O shows the truth.
  - The harness doesn't re-check that the staged source backup S is unchanged before comparing.
  - The HammerDB driver now waits for the workload before saving the backup record.

**Then ran:** the EXP-01/02 matrix at scale 70 ([run plan](../experiments/change_patterns/run_exp01.sh)); results in the entry above.

**GCP access confirmed** (Matt logged in to `gcloud` with his own account):
- Project `postgres-delta-backup` is active, with billing on the sponsored account and Compute Engine enabled.
- Quotas in us-central1: 200 N2 vCPUs, 24 instances, 8 IP addresses.
- The default VPC allows all internal traffic between VMs.

**Started:** building and validating the EXP-04 two-machine driver on two `n2-standard-8` VMs, plus a fairness review. Every method gets:
- the same timing boundaries;
- shaping in both directions, with the achieved rate verified;
- OS page caches dropped before each timed run;
- randomized method order;
- wire bytes counted from the network interface, not taken from the tools' own reports.

## 2026-10-22: Starting the build

**Goal.** Build the tool end to end and back every claim with an experiment.

**Environment found.**
- **Local machine:** Apple M5 Pro, 15 cores, 24 GB RAM, 764 GB free disk. No Docker, PostgreSQL or `gcloud`. Python 3.14 and Homebrew are present. PostgreSQL 17.11 and 18.6 are being installed with Homebrew; both include the August 2026 visibility-map fix.
- **GCP:** the project `postgres-delta-backup` isn't reachable yet. The browser pane isn't signed in to Google, and there's no `gcloud`. Matt needs to sign in before the two-machine benchmark; everything until then runs locally.

**Plan, in order of what de-risks the project most.**
1. **Change-pattern study.** How many 8 KiB pages, and how many larger chunks, actually change when a given fraction of rows changes, under different workload shapes? If most pages change, the tool can't win, so this goes first.
2. **v0 engine.** Hash tree over 8 KiB pages, a transfer protocol between a sender and receiver process, and a verified rebuild into a new folder. Run locally first, with exact byte accounting.
3. **Local comparison** against a full copy, PostgreSQL incremental backup and rsync, on the same inputs.
4. **Correctness experiments** on tricky old copies: a server that diverged after failover, truncation, `FILE_COPY` databases, enabling checksums, unlogged tables. Each one also checks whether skipping pages by LSN would have been wrong.
5. **Two-machine benchmark on GCP** over throttled links, to find the link speed where the tool starts to win.

**How the work is organised.** The engine, the study tooling and the harness refactor are built in parallel, each in its own git worktree. Each is then reviewed by an independent reviewer that tries to break it before anything is merged.
