# Experiments

Each experiment answers one question that decides something about the tool. Every write-up follows the same shape, so it's clear what was predicted before any data existed:

1. **Question:** what the experiment decides and why it matters.
2. **Hypotheses and predictions:** written down *before* running, with numbers where possible.
3. **Design:** setup, variables, controls, repetitions, and what would count as the prediction being wrong.
4. **Results:** tables and graphs, with links to the raw data.
5. **Conclusion and consequences:** what changes in the design or the plan.
6. **Limitations.**

Raw results go under `results/` (gitignored) while running. Curated result files used for figures are committed next to each write-up, and figures live in [docs/figures/](../figures/).

| # | Question | Status |
|---|---|---|
| [EXP-01](EXP-01-change-patterns.md) | When a fraction of rows changes, what fraction of pages and chunks changes? | **Done:** the model 1 − (1 − f)^(rows per page) held within 0.4 points; local changes touch about 1% of pages, scattered ones most of a table above 2% of rows |
| [EXP-02](EXP-02-local-comparison.md) | On the same inputs, how many bytes and how much compute do full copy, incremental, rsync and the tool need? | **Done:** pgdelta sends exactly the changed pages (equal to rsync at 8 KiB in 37/37 trials), but its fixed ~1 s/GB/pass of hashing caps its win at about 2.5–5.5 Gbit/s in the model |
| [EXP-03](EXP-03-tricky-copies.md) | Does the tool stay correct on tricky old copies, and would LSN skipping? | **Done:** tool correct in 10/10; LSN skipping wrong in 4 of 8 same-cluster scenarios, including stale data after failover |
| [EXP-04](EXP-04-network-sweep.md) | On two machines over throttled links, at what link speed does each method win? | **Done:** 432 runs, all verified. pgdelta beats a full copy below 1.1–1.5 Gbit/s, rsync below 0.6–1.9, incremental at every speed for local changes. The staged capture of S costs as much as a full copy. But the harness's SHA-256 manifests doubled the local capture, and probably the full copy (EXP-07) |
| [EXP-07](EXP-07-capture-speed.md) | Why does the staged capture take as long as a full copy? | **Done:** SHA-256 manifests (a harness setting) doubled the local capture; CRC32C, PostgreSQL's default, halves it. Reading the files directly with 4 readers is another 5.5× faster, mostly from parallelism |
| [EXP-04b](EXP-04b-default-checksums.md) | EXP-04 again with PostgreSQL's default CRC32C manifests | **Done:** 324 runs, all verified. Full copy and capture halve; pgdelta beats a full copy below 2.0 Gbit/s (scattered) and 2.5 (local), rsync below 0.8–1.0 and 3.9. Incremental is the fastest delta method everywhere, but a full copy beats it on the fastest links. No staged method wins at full link speed |
| [EXP-08](EXP-08-incumbent-tools.md) | Ledger test T11: do `pg_rewind`, the catch-up tools and pgBackRest's delta restore do better than pgdelta where it claims its edge? | **Done:** `pg_probackup catchup` returned wrong data on two diverged copies; WAL-G refuses them; `pg_rewind` repairs them when B kept its WAL; pgdelta (staged) correct on all. With a current backup, pgBackRest sends fewer bytes for scattered changes, so the pitch is "no repository needed"; block incremental ties pgdelta's v1 path for local changes on restore time |
| [EXP-09](EXP-09-direct-read.md) | Ledger test T8a, plus v1's whole path: how fast is pgdelta reading the source's files directly, and also patching in place? | **Done:** direct reads cost only 0.14–0.25 s more than the staged path; v1's path takes 2.7 s against a single-stream full copy's 3.1 s for local changes (the HA rule holds against today's copy, provisional until T2). The bottleneck is now the destination's two passes over B |
| [EXP-10](EXP-10-write-path.md) | Ledger test T3: copy-then-patch, fsync placement, verification, reflinks and in-place patching on Linux | **Done:** in place wins (1.6 s faster than v0's new copy); the new-copy mode moves its fsync to a thread and uses clones on XFS (within 0.19 s of in place); copy-then-patch and pipelined verification stay |
| [EXP-05](EXP-05-parallel-hashing.md) | Does hashing in parallel remove pgdelta's compute bottleneck? | **Done, mostly negative:** threads gain at most 1.2× (Python's interpreter lock); 8 threads is 1.5× slower. Writing and re-reading the output is the other half of the cost |
| [EXP-06](EXP-06-engine-throughput.md) | Can pgdelta's fixed cost be halved with worker processes, copy-then-patch and parallel verification? | **Done, mostly positive:** 4 worker processes with all three changes take 1.29–1.40 s for 2.1 GB, 2.3–3.1× faster, at 1.02–1.22× the CPU. 8 processes are faster still but exceed the CPU bound on the Mac, so the default is 4. The margins at 4 are thin on a busy Mac. A follow-up with 10 interleaved runs found pipelined verification 3–7% faster at 4, so it stays the default |
