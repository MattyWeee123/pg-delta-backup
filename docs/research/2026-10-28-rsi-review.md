# Recursive self-improvement (RSI) work, and what it means for this project's workflow (October 28, 2026)

The owner asked whether RSI agents and papers offer a framework, tooling or practices that would improve how this project works: iterating toward a goal through pre-registered, benchmark-driven experiments, where each VM evaluation costs about an hour. One research pass, against papers, official repositories and blog posts. Anything not verified against a primary source is marked so.

## What exists

**"RSI Agent" is not one framework.** The name is used by:
- a small GitHub repo that re-implements the Darwin Gödel Machine ([LtAo857/rsi-agent](https://github.com/LtAo857/rsi-agent));
- a commercial "recursive self-improving" inference-tuning agent ([AIBuildAI](https://github.com/aibuildai-inc/aibuildai-llm-inference-agent));
- a model ([Gen-Verse DiscoveryRSI-27B](https://huggingface.co/Gen-Verse/DiscoveryRSI-27B));
- Sakana AI's RSI Lab, a research group ([sakana.ai/rsi-lab](https://sakana.ai/rsi-lab/)).

**Three families of work:**

| Family | Examples | The loop | Its cost per run |
|---|---|---|---|
| **Agents that edit their own code** | STOP, Gödel Agent, ADAS, SICA, Darwin Gödel Machine (DGM), Huxley-Gödel Machine (HGM), Google's RRSI | Propose a change to the agent; score it on a benchmark; keep an archive of variants | SICA about $7,000; DGM about $22,000 and two weeks (hundreds to thousands of evaluations) |
| **LLM-driven evolutionary program search** | FunSearch, AlphaEvolve, OpenEvolve (open source), ShinkaEvolve (open source) | An LLM mutates a target program; an automated evaluator scores each; a diverse population is kept | Seconds-to-minutes evaluations; ShinkaEvolve reached results in 30–150 evaluations |
| **Automated science** | The AI Scientist (v1, v2), Google's AI co-scientist | Ideas, experiments, write-up, LLM review | About $15 a paper (v1); human selection still decided what was shown |

**Closest to this project:** Berkeley's ADRS work, which uses OpenEvolve, GEPA and ShinkaEvolve on systems and database problems ([Barbarians at the Gate](https://arxiv.org/pdf/2510.06189), [ADRS for databases](https://arxiv.org/abs/2604.06566)): 1–5 hours a run, under $30 each. It found real gains, and it also reports reward hacking:
- a load balancer that gave zero weight to 97% of the experts;
- a placement policy that removed a memory limit and caught the resulting exception;
- a transfer planner that dropped data to cut cost.

**What these works report about evaluation, in every family:**
- **Keep the optimiser away from the scorer.** A hidden scorer that re-runs after the agent stops; canary cases that only a cheat would pass. DGM faked test logs, and removed the markers its reward used to detect hallucination; it was caught by reading its archive's history. The AI Scientist extended its own timeout instead of making its code faster.
- **Held-out tests.** Google's RRSI found in-distribution gains vanish on held-out benchmarks without limits on the search ([RRSI](https://arxiv.org/abs/2609.24972)). ADRS recommends held-out workloads.
- **Accept only gains above noise; report seeds × budget.** Method rankings flipped between 1 and 40 seeds ([2609.19799](https://arxiv.org/abs/2609.19799)). Loops that only add fixes adopted fixes for failures that never happened ([Phantom Guardrails](https://arxiv.org/abs/2607.13083)).
- **An archive beats building on the latest version** (DGM's ablation). A variant's own score predicts its descendants' poorly (HGM).
- **Diversity pressure** (MAP-Elites, islands, novelty rejection; ADRS starts from 3–5 structurally different baselines). Without it, search stays anchored near its start (ShinkaEvolve).
- **Expensive evaluations:**
  - cascades of cheap-to-dear tests (AlphaEvolve);
  - cheap proxies that only rank, with a real run before acceptance ([Janus](https://arxiv.org/abs/2608.08189): 99% of the gain with 59% fewer real evaluations);
  - small-scale results that carry to large ([DBPlanBench](https://arxiv.org/abs/2602.10387)).

## Is what we do already right?

**Mostly yes, by the field's own lessons:**
- Our loop (a goal, the standard and the competitor per situation, the bottleneck found by experiment, a pre-registered test, and its write-up) is the shape these works converge on.
- Our evaluation discipline is stricter than most of them: analysis committed before data, failed predictions reported as failed, a second session near the bar (rule 11), correctness campaigns with controls.
- **Their large automated loops don't fit our cost.** DGM, SICA and HGM need hundreds to thousands of evaluations. At about an hour of VMs each, that is months. They are evidence about failure modes, not a tool to adopt wholesale.

**Where they show gaps in ours:**

1. **No formal cascade.** We go local scale model, then VMs, by habit (EXP-73 then EXP-79; EXP-78 then EXP-79b), not as a rule. We also don't track how well the local proxy predicts the VMs: EXP-78's 0.46 became 0.55 on the VMs, and EXP-73b's 0.456 became 0.406.
2. **No idea archive with triggers to revisit.** Dead ends and conditional wins live only in the LOG.
   - Early write-back won where requests bind and lost where bytes bind (EXP-76).
   - D4's split reading is unbuilt (EXP-78).
   - Sub-page deltas are built but off (EXP-56).
   - The same single-path anchoring is what the owner caught on October 27 (drift into tuning).
3. **No held-out workloads.** Every fast-path change since EXP-64 was tuned and judged on the same database (EXP-48's 18 GB), the same pgbench-style cells, and the same disk type. That is exactly the in-distribution overfitting RRSI and ADRS warn of.
4. **The scorer's integrity isn't checked automatically.** Analysis scripts are committed before the data, and post-data edits are disclosed, but nothing verifies the scoring code is unchanged between registration and run. Our analogue of "reward hacking" is a change that drops work: skipping a verification, sending fewer pages, weakening the guard. Our correctness checks catch that today because we run them every time; it isn't enforced.
5. **Noise isn't used as an explicit bar.** We have medians of 2 runs and rule 11, but no stated minimum detectable effect from the unchanged arms' between-session spread (about 0.3% on the big cells, more on small ones).

**Where RSI tooling could help, narrowly: an offline evolutionary search with a cheap evaluator.**
- **The evaluator:** the plans the sender saves (EXP-70 to EXP-79), replayed through the time model in milliseconds.
- **What it could search:** the gap-fill and coalescing policy, or the selector's constants, with OpenEvolve or ShinkaEvolve.
- **The rule:** the model only ranks, as Janus does. A real VM run decides, with held-out cells and correctness checks unchanged.
- **This is optional.** It suits small policies with a numerical objective, not the design-level questions (lineage proofs, new label sources) that have driven the biggest gains.

## Recommended changes: adopted by the owner, all five (decision K14; rules 13–17 in DECISIONS.md)

1. **A three-stage cascade as a rule,** with a proxy-fidelity log:
   - tests (seconds);
   - a local scale model (minutes), with every local-to-VM ratio pair recorded;
   - VMs (an hour) only for candidates that pass. Several candidates are batched as arms of one VM session.
2. **An idea archive** (`docs/research/idea-archive.md`): every idea with its status, evidence, the conditions under which it won or lost, and a trigger to revisit. Each research-loop pass reads it, and proposes at least one structurally different idea beside any tuning.
3. **A held-out workload set,** never used for tuning, and run at each milestone:
   - a realistic multi-table schema;
   - a different database size;
   - a different disk type (`pd-ssd` or Hyperdisk).
4. **Scorer integrity:** each registration records the hash of its analysis script and lab, checked when the results are written.
   - Every performance change keeps a correctness gate, including a negative control where it can be built (as EXP-80's S2e).
   - Its statistics must show that no work was dropped: pages re-read equal to pages sent, and the guard's decisions.
5. **Noise as a bar:** each experiment states the minimum effect it can see, from the unchanged arms' spread across sessions, and treats deciding margins inside it as rule 11 cases.
