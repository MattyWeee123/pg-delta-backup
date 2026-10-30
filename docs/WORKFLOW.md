# How each decision is made: the research workflow

This page is the one place where the project's research workflow lives. It combines two things the owner asked for, which are always applied together:
- **the research loop:** the owner's own workflow, October 27, 2026 (rule 12, decision K13);
- **the five changes adopted from the review of recursive self-improvement (RSI) work,** October 28, 2026 (rules 13–17, decision K14). The review and its sources are in [research/2026-10-28-rsi-review.md](research/2026-10-28-rsi-review.md).

[DECISIONS.md](DECISIONS.md) keeps rules 1–11 and points here for rules 12–17. Their numbers are kept, so every reference to "rule 13" and the others elsewhere still holds.

**The goal is open-ended:** keep improving, pass after pass, until pgdelta beats its competitors wherever it can. Finishing an experiment means starting the next pass, never that the work is done.

**Every decision brought to the owner,** whether the next work, a design choice or a default, carries the workflow check at the end of this page.

## The research loop (rule 12): what to work on

The owner, in their words (October 27, 2026; also in [LOG.md](LOG.md)):

> Have we been doing anything novel? Or are you just rehashing the same stuff over and over and over again? The goal of this project was to either win by isolating some set of constraints, which we have, right? You mentioned that we do beat these other approaches when it comes to certain approaches, but I want you to make sure: what is the industry standard for that situation, and do we have competitors? Are we truly better than those competitors? And in the case where we are currently not beating the competition, we should be trying new things based on novel research and discussion, like figuring out where the bottleneck is and realizing: Is this a system's problem or an algorithm problem? Meaning, given our current algorithm, is it even feasibly possible, given the best systems optimizations in the world, to beat this other algorithm? If so, maybe it's a system's problem. If not, then it could even be an algorithm problem. Maybe the issue is this hashing approach. Maybe it's a page labeling approach we should try. Maybe some other algorithm.

> And even better, don't just look at novel research. Try to synthesize information and conduct our own research as well. Maybe two different ideas from two different papers, or the same ideas from a paper, can be reworked potentially better for our use case. Also, definitely try pulling directly from the paper as well if you find something promising.

> All decisions should, at the end of the day, be evidence-driven. You should not simply guess that something is the bottleneck. You should perform experiments to justify it. All decisions must be justified, as in a scientific research setting. All your decisions should be reviewed as if they're being reviewed by a paper committee reviewing your findings.

**Each pass of the loop:**
1. **Situation, standard and competitor.** For each situation, name the industry standard and the strongest competitor, verified from primary sources, and say with a measurement whether pgdelta beats it. The current scorecard: [research/2026-10-28-research-loop-2.md](research/2026-10-28-research-loop-2.md) and the figure `loop2-scorecard`.
2. **The bottleneck, by experiment.** Where pgdelta loses, find why by measurement, never by assumption.
3. **Systems or algorithm.** Could the best possible implementation of the current algorithm beat the competitor? Measured ceilings and lower-bound arithmetic decide:
   - **a systems problem:** optimise;
   - **an algorithm problem:** change the algorithm (hashing, page labelling, or another).
4. **Papers and prior art.** Mine papers and other tools for mechanisms, taken from the sources themselves, and combine or rework them for these constraints. Pull our own research into it as well.
5. **Committee-grade evidence.** Pre-register, test, and write up as for a paper committee. Claims are no stronger than their evidence (rule 11).

**Tuning outside this loop is not progress.**

## The RSI rules (rules 13–17): how each experiment is run

The RSI review found that self-improving loops fail in three ways:
- **gaming the evaluator** (DGM faked test logs; ADRS found reward hacking);
- **overfitting to the tuning workloads** (RRSI's gains vanished on held-out benchmarks);
- **anchoring on one line of ideas** (DGM's and ShinkaEvolve's ablations).

These rules guard against all three at this project's cost of about an hour a VM evaluation.

**13. A cascade, with a log of how well each proxy predicts.**
- **Three stages:** tests (seconds), then a local scale model (minutes), then the VMs (an hour).
- **Promotion to the VMs on local evidence alone** needs a local gain larger than the proxy's measured error. The VM ratio has lain within 0.875–1.185 of the local one, so a local ratio of 0.80 or less.
- **Batching:** candidates that pass are run as arms of one VM session.
- **The log:** every local/VM pair of the same ratio goes into [research/proxy-fidelity.csv](research/proxy-fidelity.csv), plotted by `analysis/proxy_fidelity.py`.

**14. The idea archive.** [research/idea-archive.md](research/idea-archive.md) holds every idea: its status, its evidence, where it won and lost, and a trigger to revisit it.
- Each pass of the loop reads it first.
- Each pass proposes at least one structurally different idea beside any tuning.
- An idea that loses is archived with its conditions, never deleted.

**15. Held-out workloads.** The settings in [research/held-out-workloads.md](research/held-out-workloads.md) not yet spent (H5–H8 since EXP-84 spent H1–H4) are sealed: never used to tune or to decide. A setting a held-out session has used is spent, may then be used to tune, and is replaced.
- They run only at a milestone (before a default changes, or before a claim leaves the project), in a pre-registered session predicted from the tuned results.
- A miss narrows the claim; it is never tuned away.

**16. Scorer integrity.**
- **At registration,** `analysis/integrity.py register` records the hash of every file that scores, runs or is under test, as of the registration commit. The registry is [experiments/registrations.json](experiments/registrations.json).
- **At the write-up,** `check` lists every file changed since. The write-up says why each change alters no score, or re-scores with the registered file.
- **Every performance change keeps a correctness gate:** the copy compared with the source, and no work dropped (the guard's decisions reported; pages re-read against pages sent).
- **A negative control** is built wherever one can be (EXP-80's S2e).

**17. Noise as an explicit bar.** [research/noise-floor.csv](research/noise-floor.csv), refreshed with every VM session by `analysis/noise_floor.py`.
- **Within a session, as of EXP-85:** two runs of a cell differ by up to 2.23% at the 95th percentile for runs of 10 s or more, and 7.11% under 10 s.
- **Between sessions,** `pg_basebackup` in the same cell moves about 0.3%.
- **A deciding effect inside that spread** gets a second session (rule 11), or is reported as a tie.
- **A cross-session comparison** needs its control arm within 1% of the earlier session's.
- **A cell whose runs differ by more than the spread** is checked for interference before it is scored.
- **Each registration cites the values current when it was registered.**

**Optional, not built:** an offline evolutionary search (OpenEvolve-style) over saved plans, with the time model as a cheap evaluator. The model only ranks; the VMs decide. It is in the idea archive.

## The workflow check

Every decision brought to the owner ends with this check: one line per item, saying what was done, or why it doesn't apply.

| | Check | Answer for this decision |
|---|---|---|
| 1 | Situation, the industry standard and the strongest competitor; do we beat them, measured? | |
| 2 | The bottleneck, found by which experiment? | |
| 3 | Systems or algorithm problem, by which measured bound? | |
| 4 | Which papers or other tools were mined, and what was combined? | |
| 5 | Committee-grade: pre-registered? How firm is each claim (measured, inferred, assumed)? | |
| 13 | Which cascade stage is next, and which proxy pair does it log? | |
| 14 | Idea archive read? Which structurally different idea is proposed beside any tuning? | |
| 15 | Is a held-out session owed before this (a default changing, or a claim leaving)? | |
| 16 | Registered files, the correctness gate, the negative control | |
| 17 | The noise bar for the deciding effect | |
